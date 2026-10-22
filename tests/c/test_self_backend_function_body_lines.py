from __future__ import annotations

import ast
import inspect
import textwrap
import weakref

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_parse as parser
from pcc.backend.self_backend_parse import (
    _iter_function_defs,
    parse_self_backend_module,
)


_TWO_FUNCTION_IR = textwrap.dedent(
    """
    target triple = "arm64-apple-macosx13.0.0"

    define i64 @first(
        i64 %value
    ) {
    entry:

      ; retained comment line
      %next = add i64 %value, 1
      ret i64 %next
    }

    define void @second() {
    entry:
      ret void
    }
    """
)


def test_function_definition_scan_preserves_multiline_body_text() -> None:
    definitions = _iter_function_defs(_TWO_FUNCTION_IR)

    assert len(definitions) == 2
    first_header, first_body = definitions[0]
    second_header, second_body = definitions[1]
    assert first_header.startswith("define i64 @first(")
    assert second_header == "define void @second() {"
    assert isinstance(first_body, str)
    assert isinstance(second_body, str)
    assert first_body.splitlines() == [
        "entry:",
        "",
        "  ; retained comment line",
        "  %next = add i64 %value, 1",
        "  ret i64 %next",
    ]
    assert second_body.splitlines() == ["entry:", "  ret void"]


def test_function_body_handoff_filters_empty_and_comment_lines() -> None:
    module = parse_self_backend_module(_TWO_FUNCTION_IR)
    first = module.functions[0]
    kernel = first.indexed_kernel

    assert kernel.block_names == ["entry"]
    assert kernel.block_fact(0).second == 1


def test_transferred_body_lines_preserve_parser_output() -> None:
    module = parse_self_backend_module(_TWO_FUNCTION_IR)

    assert [function.name for function in module.functions] == ["first", "second"]
    assert [len(function.indexed_kernel.block_names) for function in module.functions] == [
        1,
        1,
    ]


def test_function_definition_scan_keeps_unterminated_diagnostic() -> None:
    with pytest.raises(BackendUnavailable, match="unterminated function body"):
        _iter_function_defs("define void @broken() {\nentry:\n  ret void\n")


def _restore_retained_parse_inputs(tree):
    """Restore only the old private-list traversal and local seed lifetime."""
    function, = tree.body
    assert isinstance(function, ast.FunctionDef) and function.name == "_parse_functions"
    starts = [index for index, node in enumerate(function.body)
              if isinstance(node, ast.Assign) and ast.unparse(node) == "definitions = _iter_function_defs(ir_text)"]
    assert len(starts) == 1
    index = starts[0]
    scan, reverse, loop = function.body[index:index + 3]
    assert ast.unparse(reverse) == "definitions.reverse()"
    assert isinstance(loop, ast.While) and ast.unparse(loop.test) == "definitions" and not loop.orelse
    take = loop.body[0]
    assert ast.unparse(take) == "header_text, body_text = definitions.pop()"
    releases = {"indexed_seed = None", "header_text = ''", "body_text = ''"}
    assert sum(ast.unparse(node) in releases for node in loop.body) == 3
    body = [node for node in loop.body[1:] if ast.unparse(node) not in releases]
    function.body[index:index + 3] = [ast.For(
        target=take.targets[0], iter=scan.value, body=body, orelse=[], type_comment=None,
    )]
    return ast.fix_missing_locations(tree)


def _retaining_parse_functions():
    tree = _restore_retained_parse_inputs(ast.parse(inspect.getsource(parser._parse_functions)))
    namespace = dict(vars(parser))
    exec(compile(tree, "<original-parser-input-lifetimes>", "exec"), namespace)
    return namespace["_parse_functions"]


_LIFETIME_IR = (
    _TWO_FUNCTION_IR,
    'target triple = "arm64-apple-macosx13.0.0"\n'
    'define i32 @first() { ret i32 42 }\ndefine void @last() { ret void }\n',
    'target triple = "arm64-apple-macosx13.0.0"\n'
    'define i64 @"quoted.name"(\n i64 %"value"\n) {\nentry:\n ret i64 %"value"\n}\n',
    'target triple = "arm64-apple-macosx13.0.0"\n'
    '@slot = global i64 9\ndeclare i64 @callee(i64)\n'
    'define i64 @first(i1 %cond) {\nentry:\n br i1 %cond, label %yes, label %no\n'
    'yes:\n %value = load i64, ptr @slot\n br label %join\n'
    'no:\n br label %join\njoin:\n %selected = phi i64 [ %value, %yes ], [ 2, %no ]\n'
    ' %result = call i64 @callee(i64 %selected)\n ret i64 %result\n}\n'
    'define void @last() {\nentry:\n ret void\n}\n',
)


@pytest.mark.parametrize("text", _LIFETIME_IR, ids=["multiline", "single-line", "quoted", "cfg-call-global"])
def test_retired_parser_inputs_preserve_verified_indexed_bytes(tmp_path, monkeypatch, text):
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
    from pcc.backend.self_backend_verify import verify_parsed_module

    original = _retaining_parse_functions()
    actual = parse_self_backend_module(text)
    verify_parsed_module(actual)
    actual_path = tmp_path / "actual.pidx"
    encode_indexed_module_file(str(actual_path), actual)
    with monkeypatch.context() as control:
        control.setattr(parser, "_parse_functions", original)
        expected = parse_self_backend_module(text)
        verify_parsed_module(expected)
    expected_path = tmp_path / "expected.pidx"
    encode_indexed_module_file(str(expected_path), expected)
    assert [function.name for function in actual.functions] == [function.name for function in expected.functions]
    assert actual_path.read_bytes() == expected_path.read_bytes()


@pytest.mark.parametrize("text,message", [
    ('define i32 @"bad-name"() { ret i32 1 }\ndefine void @late() {\nentry:\n ret void\n',
     "unterminated function body"),
    ('define i32 @"bad-first"() { ret i32 1 }\ndefine void @"bad-second"() { ret void }\n',
     "bad-first"),
    ('define i32 @first() {\nentry:\n %bad = unsupported i32 1\n ret i32 %bad\n}\n',
     "self backend"),
], ids=["complete-scan-first", "symbol-order", "instruction-failure"])
def test_retired_parser_inputs_preserve_failure_type_and_priority(monkeypatch, text, message):
    text = 'target triple = "arm64-apple-macosx13.0.0"\n' + text
    original = _retaining_parse_functions()
    with pytest.raises(BackendUnavailable, match=message) as actual:
        parse_self_backend_module(text)
    with monkeypatch.context() as control:
        control.setattr(parser, "_parse_functions", original)
        with pytest.raises(BackendUnavailable) as expected:
            parse_self_backend_module(text)
    assert type(actual.value) is type(expected.value)
    assert str(actual.value) == str(expected.value)


def test_parser_retires_consumed_definitions_and_frozen_seed(monkeypatch):
    scan, parse_blocks = parser._iter_function_defs, parser._parse_blocks
    definitions, seeds, visits = [], [], []

    class TrackedSeed(parser.IndexedFunctionSeed):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seeds.append(weakref.ref(self))

    def scanned(text):
        result = scan(text)
        definitions.append(result)
        return result

    def blocks(name, *args, **kwargs):
        if visits:
            assert seeds[0]() is None
        visits.append(name)
        assert len(definitions[0]) == 2 - len(visits)
        return parse_blocks(name, *args, **kwargs)

    monkeypatch.setattr(parser, "IndexedFunctionSeed", TrackedSeed)
    monkeypatch.setattr(parser, "_iter_function_defs", scanned)
    monkeypatch.setattr(parser, "_parse_blocks", blocks)
    module = parse_self_backend_module(_TWO_FUNCTION_IR)
    assert visits == ["first", "second"] and definitions == [[]]
    assert len(seeds) == 2 and all(reference() is None for reference in seeds)
    assert all(function.indexed_seed is None for function in module.functions)
    assert all(function.indexed_kernel.indexed_call_plane is None for function in module.functions)
    # The first kernel remains readable after retiring both construction seeds.
    first, second = (function.indexed_kernel for function in module.functions)
    assert first.block_names == second.block_names == ["entry"]
    assert first.block_fact(0).second == 1 and second.block_fact(0).second == 0


def _original_function_prescan(ir_text: str) -> list[tuple[str, str]]:
    """Split ``define`` bodies from module text.

    The emitted layout opens the body at the end of the header line and
    closes it on a line of its own; that stays a line-at-a-time scan.  A
    header line that does not end in ``{`` is scanned for braces outside
    quoted strings, so a body opened mid-line, or written on the same line
    (``define i32 @f() { ret i32 42 }``), is found; a struct type in the
    parameter list sits inside parentheses and is not mistaken for it.
    """
    defs: list[tuple[str, str]] = []
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = False
    in_body = False
    paren_depth = 0
    for line in ir_text.splitlines():
        if not in_header and not in_body:
            if not line.startswith("define "):
                continue
            header_lines = []
            paren_depth = 0
            in_header = True
        if in_header:
            if line.rstrip().endswith("{"):
                header_lines.append(line)
                in_header = False
                in_body = True
                continue
            scanned = parser._scan_function_line(line, paren_depth, 0)
            paren_depth = scanned[0]
            open_index = scanned[2]
            if open_index < 0:
                header_lines.append(line)
                continue
            header_lines.append(line[: open_index + 1])
            close_index = scanned[3]
            if close_index >= 0:
                defs.append(
                    (
                        "\n".join(header_lines),
                        line[open_index + 1 : close_index].strip(),
                    )
                )
                header_lines = []
                in_header = False
                continue
            rest = line[open_index + 1 :].strip()
            body_lines = [rest] if rest else []
            in_header = False
            in_body = True
            continue
        if line == "}":
            defs.append(("\n".join(header_lines), "\n".join(body_lines)))
            header_lines = []
            body_lines = []
            in_body = False
            continue
        body_lines.append(line)
    if in_header or in_body:
        raise BackendUnavailable("self backend saw unterminated function body")
    return defs


_PRESCAN_SHAPES = (
    _TWO_FUNCTION_IR,
    'define i32 @one() { ret i32 42 }\ndefine void @two() { ret void }',
    'define void @mid() { entry:\n  ret void\n}',
    'define void @"brace{.name}"(ptr %"arg}name") {\nentry:\n ret void\n}',
    'define {i64, i64} @pair(\n {i64, i64} %value\n) {\nentry:\n ret {i64, i64} %value\n}',
    'define void @nested() {\nentry:\n ; { nested comment }\n %x = call { i64, i64 } @pair()\n ret void\n}',
    '; define void @ignored() {\n@text = constant [4 x i8] c"{}\\00"\n'
    'define void @ok() {\nentry:\n ; } and { are comments\n ret void\n}',
    '; no functions\n@global = global i64 9',
    'define void @incomplete(\n i64 %value',
    'define void @early() { ret void }\ndefine void @late() {\nentry:\n ret void',
)
_LINE_BOUNDARIES = ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def _prescan_result(scan, text):
    try:
        return ("definitions", scan(text))
    except BackendUnavailable as error:
        return ("error", type(error), str(error))


@pytest.mark.parametrize("text", _PRESCAN_SHAPES, ids=(
    "multiline", "inline", "midline", "quoted-braces", "aggregate-header",
    "nested-body", "comments", "empty", "unterminated-header", "unterminated-body",
))
def test_function_prescan_retirement_preserves_all_splitlines_boundaries(text):
    for boundary in _LINE_BOUNDARIES:
        for trailing in (False, True):
            sample = boundary.join(text.splitlines()) + (boundary if trailing else "")
            actual = _prescan_result(_iter_function_defs, sample)
            expected = _prescan_result(_original_function_prescan, sample)
            assert actual == expected
            if actual[0] == "definitions":
                assert [(header.encode("utf-8"), body.encode("utf-8"))
                        for header, body in actual[1]] == [
                    (header.encode("utf-8"), body.encode("utf-8")) for header, body in expected[1]
                ]
            else:
                assert actual[2] == "self backend saw unterminated function body"


@pytest.mark.parametrize("kind", ("shared-list", "custom-list", "iterator", "ephemeral-iterable", "split-error", "next-error"))
def test_function_prescan_subclass_keeps_original_iteration_and_shared_list(kind):
    def exercise(scan):
        events = []
        shared = ["define void @f() {", "entry:", " ret void", "}"]
        original = tuple(shared)
        failure = RuntimeError("split-or-next sentinel")

        class CustomList(list):
            def __iter__(self):
                events.append("iter")
                for item in super().__iter__():
                    events.append(("next", item))
                    yield item
            def __setitem__(self, key, value):
                raise AssertionError("caller-owned list must not be mutated")

        class Text(str):
            def splitlines(self):
                events.append("splitlines")
                if kind == "split-error":
                    raise failure
                if kind == "shared-list":
                    return shared
                if kind == "custom-list":
                    return CustomList(shared)
                def stream():
                    events.append("iter")
                    for index, item in enumerate(shared):
                        if kind == "next-error" and index == 2:
                            events.append("next-error")
                            raise failure
                        events.append(("next", item))
                        yield item
                if kind == "ephemeral-iterable":
                    class Ephemeral:
                        def __iter__(self):
                            events.append("iterable-iter")
                            return stream()
                        def __del__(self):
                            events.append("iterable-released")
                    return Ephemeral()
                return stream()

        try:
            result = ("definitions", scan(Text("unused overridden source")))
        except RuntimeError as error:
            assert error is failure
            result = ("error", type(error), str(error))
        assert tuple(shared) == original
        if kind == "ephemeral-iterable":
            assert events[:4] == ["splitlines", "iterable-iter", "iterable-released", "iter"]
        return result, events

    assert exercise(_iter_function_defs) == exercise(_original_function_prescan)


def _prescan_with_observed_splitlines(scan, splitlines):
    # Replace just the allocation expression to observe otherwise-private
    # line owners. The actual ownership predicate and scanner stay intact.
    tree = ast.parse(inspect.getsource(scan))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node) == "ir_text.splitlines()"]
    assert len(calls) == (2 if scan is _iter_function_defs else 1)
    for call in calls:
        call.func = ast.Name(id="observed_splitlines", ctx=ast.Load())
        call.args = [ast.Name(id="ir_text", ctx=ast.Load())]
    ast.fix_missing_locations(tree)
    scope = dict(scan.__globals__)
    scope["observed_splitlines"] = splitlines
    exec(compile(tree, "<observed-function-prescan-lines>", "exec"), scope)
    return scope[scan.__name__]


def test_function_prescan_retires_consumed_line_owners_before_scan_finishes():
    text = ("; ignored preface\ndefine void @first() {\nentry:\n ret void\n}\n"
            "define void @second() {\nentry:\n ret void\n}\n")

    def exercise(scan):
        refs, visits, writes, held, completed_owners = [], [], [], [], []

        class Line(str):
            pass

        class Lines(list):
            def __iter__(self):
                for index in range(len(self)):
                    visits.append((index, sum(bool(item) for item in self[:index])))
                    if index == 5:
                        completed_owners.append(sum(reference() is not None for reference in refs[:4]))
                    yield self[index]
            def __setitem__(self, index, value):
                writes.append((index, value))
                super().__setitem__(index, value)

        def splitlines(source):
            assert type(source) is str and source == text
            lines = Lines(Line(line) for line in source.splitlines())
            refs.extend(weakref.ref(line) for line in lines[:])
            held.append(lines)
            return lines

        observed = _prescan_with_observed_splitlines(scan, splitlines)
        result = observed(text)
        assert len(held) == 1
        return result, visits, writes, held[0], [reference() is not None for reference in refs], completed_owners

    actual, original = exercise(_iter_function_defs), exercise(_original_function_prescan)
    count = len(text.splitlines())
    assert actual[0] == original[0]
    assert actual[1] == [(index, 0) for index in range(count)]
    assert original[1] == [(index, index) for index in range(count)]
    assert actual[2] == [(index, "") for index in range(count)] and original[2] == []
    assert actual[3] == [""] * count and len(original[3]) == count
    assert not any(actual[4]) and all(original[4])
    assert actual[5] == [0] and original[5] == [4]
