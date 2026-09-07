from __future__ import annotations

import subprocess
import pytest

from pcc.py_frontend import compiled_default_passes, pipeline


_SCALAR_IR = """\
define i64 @probe(i64 %arg) {
entry:
  %slot = alloca i64, align 8
  store i64 %arg, ptr %slot, align 8
  %value = load i64, ptr %slot, align 8
  ret i64 %value
}
"""

_STRUCT_IR = """\
define i64 @probe(i64 %arg) {
entry:
  %pair = alloca { i64, ptr }, align 8
  %first = getelementptr inbounds { i64, ptr }, ptr %pair, i32 0, i32 0
  store i64 %arg, ptr %first, align 8
  %value = load i64, ptr %first, align 8
  ret i64 %value
}
"""


def test_compiled_default_tier_promotes_straight_line_scalar_slot():
    out = compiled_default_passes.run_compiled_default_tier(
        _SCALAR_IR,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    )

    assert "alloca i64" not in out
    assert "store i64" not in out
    assert "load i64" not in out
    assert "ret i64 %arg" in out


def test_compiled_default_tier_dangling_definition_guard_is_fail_closed():
    assert compiled_default_passes._references_removed_definitions(
        ["  ret i64 %loaded\n"], ["slot", "loaded"]
    ) is True
    assert compiled_default_passes._references_removed_definitions(
        ["  ret i64 %argument\n"], ["slot", "loaded"]
    ) is False


def test_compiled_default_tier_splits_bounded_literal_struct():
    out = compiled_default_passes.run_compiled_default_tier(
        _STRUCT_IR,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    )

    assert "alloca { i64, ptr }" not in out
    assert "getelementptr" not in out
    assert "ret i64 %arg" in out


def test_entry_block_single_store_promotes_across_control_flow():
    """An entry-block store with no second store is proved, not unproved.

    The entry block dominates every block, so a non-escaping scalar slot
    written exactly once there and read later observes exactly that value.
    The tier used to decline this and therefore promoted nothing at all on
    real runtime functions, where every parameter spill has this shape.
    """
    source = """\
define i64 @probe(i1 %cond, i64 %arg) {
entry:
  %slot = alloca i64
  store i64 %arg, ptr %slot
  br i1 %cond, label %read, label %read
read:
  %value = load i64, ptr %slot
  ret i64 %value
}
"""

    out = compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    )
    assert "alloca" not in out
    assert "load" not in out
    assert "ret i64 %arg" in out


def test_second_store_in_another_block_stays_unproved():
    """Two stores need a phi; without one the tier must leave the slot alone."""
    source = """\
define i64 @probe(i1 %cond, i64 %arg, i64 %other) {
entry:
  %slot = alloca i64
  store i64 %arg, ptr %slot
  br i1 %cond, label %alt, label %read
alt:
  store i64 %other, ptr %slot
  br label %read
read:
  %value = load i64, ptr %slot
  ret i64 %value
}
"""

    assert compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    ) == source


def test_a_load_before_the_entry_store_stays_unproved():
    """The store must precede every entry-block load.

    The load here reads the slot before it is written, so the stored value is
    not what it observes.  The case is cross-block so only the entry-block
    promotion could act on it; it must decline. (A single-block version of the
    same shape is promoted to ``undef`` by the single-block scan, which is
    correct LLVM semantics for reading an uninitialised slot.)
    """
    source = """\
define i64 @probe(i1 %cond, i64 %arg) {
entry:
  %slot = alloca i64
  %early = load i64, ptr %slot
  store i64 %arg, ptr %slot
  br i1 %cond, label %read, label %read
read:
  %late = load i64, ptr %slot
  %sum = add i64 %early, %late
  ret i64 %sum
}
"""

    assert compiled_default_passes._promote_entry_single_store(
        source.splitlines(keepends=True)
    ) == source.splitlines(keepends=True)


def test_a_store_outside_the_entry_block_stays_unproved():
    """A single store that is not in the entry block does not dominate the
    loads without a dominator tree the owned tier does not have yet."""
    source = """\
define i64 @probe(i1 %cond, i64 %arg) {
entry:
  %slot = alloca i64
  br i1 %cond, label %write, label %read
write:
  store i64 %arg, ptr %slot
  br label %read
read:
  %value = load i64, ptr %slot
  ret i64 %value
}
"""

    assert compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    ) == source


def test_compiled_default_tier_rejects_alloca_address_escape():
    source = """\
define ptr @probe() {
entry:
  %slot = alloca ptr
  store ptr %slot, ptr %slot
  %value = load ptr, ptr %slot
  ret ptr %value
}
"""

    assert compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=False,
    ) == source


def test_compiled_default_tier_strict_mode_skips_actual_cpython_call():
    source = (
        "declare ptr @py_cpy_import(ptr)\n\n"
        "define i64 @probe(ptr %name, i64 %arg) {\n"
        "entry:\n"
        "  %slot = alloca i64\n"
        "  store i64 %arg, ptr %slot\n"
        "  %module = call ptr @py_cpy_import(ptr %name)\n"
        "  %value = load i64, ptr %slot\n"
        "  ret i64 %value\n"
        "}\n"
    )

    assert compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=True,
    ) == source


def test_compiled_default_tier_strict_mode_allows_cpython_declaration_only():
    source = "declare ptr @py_cpy_import(ptr)\n\n" + _SCALAR_IR

    out = compiled_default_passes.run_compiled_default_tier(
        source,
        ["mem2reg", "sroa"],
        strict_no_libpython=True,
    )

    assert "declare ptr @py_cpy_import(ptr)" in out
    assert "alloca" not in out


def test_self_default_pass_tier_never_starts_host_subprocess(monkeypatch):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("bounded self pass tier escaped to host Python")

    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.setattr(pipeline.subprocess, "run", unexpected_run)

    out = pipeline._apply_python_ir_pass_pipeline(
        _SCALAR_IR,
        module_name="probe",
        default_raw="default",
        strict_no_libpython=True,
    )

    assert "alloca" not in out
    assert "ret i64 %arg" in out


def test_self_default_pass_batch_preserves_input_order_without_host(monkeypatch):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("bounded self pass batch escaped to host Python")

    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.setattr(pipeline.subprocess, "run", unexpected_run)

    out = pipeline._apply_python_ir_pass_pipeline_many(
        [("first", _SCALAR_IR), ("second", _STRUCT_IR)],
        default_raw="default",
        strict_no_libpython=True,
    )

    assert [name for name, _text in out] == ["first", "second"]
    assert all("alloca" not in text for _name, text in out)


def test_explicit_owned_pass_does_not_use_host_boundary(monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("owned pass escaped to host Python")

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "dce")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    source = 'define i64 @probe(i64 %arg) {\nentry:\n  %dead = add i64 %arg, 1\n  ret i64 %arg\n}\n'
    result = pipeline._apply_python_ir_pass_pipeline(
        source, module_name="probe", default_raw="default",
    )
    assert "%dead" not in result


def test_unsupported_self_pass_fails_without_host_fallback(monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("unsupported self pass escaped to host Python")

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "licm")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    with pytest.raises(pipeline.PyPipelineError, match="self optimizer does not own"):
        pipeline._apply_python_ir_pass_pipeline(
            _SCALAR_IR,
            module_name="probe",
            default_raw="default",
        )


def _two_pass_reference(ir_text: str) -> str:
    """The historical shape: one full traversal per transform.

    Kept as an executable oracle so the fused single-traversal rewrite in
    ``_rewrite_functions`` cannot drift from ``sroa(mem2reg(module))``.
    """

    def one_pass(text, transform):
        out: list[str] = []
        function_lines: list[str] = []
        in_function = False
        for line in str(text).splitlines(keepends=True):
            if not in_function and line.lstrip().startswith("define "):
                in_function = True
                function_lines = [line]
                continue
            if in_function:
                function_lines.append(line)
                if line.strip() == "}":
                    out.extend(transform(function_lines))
                    function_lines = []
                    in_function = False
                continue
            out.append(line)
        if function_lines:
            out.extend(function_lines)
        return "".join(out)

    once = one_pass(ir_text, compiled_default_passes._mem2reg_function)
    return one_pass(once, compiled_default_passes._sroa_function)


def _mem2reg_all_candidates_reference(lines: list[str]) -> list[str]:
    """`_mem2reg_function` with its historical every-line-x-every-candidate scan.

    The classification loop used to visit every alloca candidate for every line
    and ask `_contains_ssa_name`.  On one real emitted module that was 38.5M
    (line, candidate) pairs against 252k lines -- a 152x amplification, and
    under a self-hosted pcc1 each of those pairs is a dict lookup plus a
    provenance-checked object touch.  The live version instead tokenizes each
    line's maximal `%name` tokens and looks up only those.  That is only sound
    while the two agree exactly, so the old shape stays here as an oracle.
    """
    m = compiled_default_passes
    block_ids = m._block_ids(lines)
    blocked = m._control_flow_blocks(lines, block_ids)
    candidates: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines):
        parsed = m._parse_alloca(line)
        if parsed is None:
            continue
        name, ty, _indent = parsed
        if not m._is_scalar_type(ty):
            continue
        candidates[name] = {
            "line": index,
            "type": ty,
            "block": block_ids[index],
            "events": [],
            "safe": block_ids[index] not in blocked,
        }
    if not candidates:
        return lines

    for index, line in enumerate(lines):
        store = m._parse_store(line)
        load = m._parse_load(line)
        for name, candidate in candidates.items():
            if not candidate["safe"]:
                continue
            if index == candidate["line"]:
                continue
            if not m._contains_ssa_name(line, name):
                continue
            if block_ids[index] != candidate["block"]:
                candidate["safe"] = False
                continue
            if store is not None:
                store_ty, value, pointer = store
                if pointer == name and store_ty == candidate["type"]:
                    if m._contains_ssa_name(value, name):
                        candidate["safe"] = False
                        continue
                    candidate["events"].append((index, "store", value))
                    continue
            if load is not None:
                result, load_ty, pointer = load
                if pointer == name and load_ty == candidate["type"]:
                    candidate["events"].append((index, "load", result))
                    continue
            candidate["safe"] = False

    removed: set[int] = set()
    removed_definitions: list[str] = []
    replacements: dict[str, str] = {}
    for _name, candidate in candidates.items():
        if not candidate["safe"]:
            continue
        removed.add(int(candidate["line"]))
        removed_definitions.append(str(_name))
        current_value = "undef"
        events = list(candidate["events"])
        events.sort(key=lambda item: int(item[0]))
        for index, kind, payload in events:
            removed.add(int(index))
            if kind == "store":
                current_value = str(payload)
            else:
                replacements[str(payload)] = current_value
                removed_definitions.append(str(payload))

    if not removed:
        return lines
    replacements = m._resolved_replacements(replacements)
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in removed:
            continue
        out.append(m._replace_ssa_names(line, replacements))
    if m._references_removed_definitions(out, removed_definitions):
        return lines
    return out


def _many_alloca_function(slots: int, filler: int) -> str:
    """A `define` shaped like real emitted IR: many scalar slots, interleaved
    stores/loads, unrelated lines that mention other `%` names, plus the
    shapes that must stay unpromoted (escape, cross-block, struct)."""
    out = ["define i64 @wide(i64 %arg) {\n", "entry:\n"]
    for i in range(slots):
        out.append(f"  %s{i} = alloca i64, align 8\n")
    out.append("  %agg = alloca { i64, ptr }, align 8\n")
    for i in range(slots):
        out.append(f"  store i64 %arg, ptr %s{i}, align 8\n")
        for f in range(filler):
            out.append(f"  %t{i}_{f} = add i64 %arg, {f}\n")
        out.append(f"  %v{i} = load i64, ptr %s{i}, align 8\n")
    # An escape and a cross-block use must survive the inversion unchanged.
    out.append("  store ptr %s0, ptr %agg, align 8\n")
    out.append("  br label %next\n")
    out.append("next:\n")
    out.append("  %late = load i64, ptr %s1, align 8\n")
    out.append("  ret i64 %late\n")
    out.append("}\n")
    return "".join(out)


def test_mem2reg_token_scan_matches_the_all_candidates_scan():
    cases = [
        _SCALAR_IR,
        _STRUCT_IR,
        # Cross-block use and alloca-address escape: both must stay unpromoted.
        "define i64 @probe(i1 %cond, i64 %arg) {\nentry:\n"
        "  %slot = alloca i64\n  store i64 %arg, ptr %slot\n"
        "  br i1 %cond, label %read, label %read\nread:\n"
        "  %value = load i64, ptr %slot\n  ret i64 %value\n}\n",
        "define ptr @probe() {\nentry:\n  %slot = alloca ptr\n"
        "  store ptr %slot, ptr %slot\n  %value = load ptr, ptr %slot\n"
        "  ret ptr %value\n}\n",
        _many_alloca_function(1, 0),
        _many_alloca_function(6, 2),
        _many_alloca_function(40, 3),
        # Names that are prefixes of each other: %s1 vs %s10 vs %s100 is the
        # exact case a naive substring scan gets wrong.
        _many_alloca_function(120, 1),
    ]
    for text in cases:
        lines = text.splitlines(keepends=True)
        # The reference models the single-block scan, so compare against that
        # core rather than the composition that also runs the entry-block
        # single-store promotion.
        assert compiled_default_passes._mem2reg_single_block_function(lines) == (
            _mem2reg_all_candidates_reference(lines)
        ), text[:200]


def test_ssa_name_tokens_agree_with_contains_ssa_name():
    m = compiled_default_passes
    lines = [
        "  %v1 = load i64, ptr %s10, align 8\n",
        "  store ptr %s0, ptr %agg, align 8\n",
        "  %x = call i64 @f(ptr %y, i64 %z)\n",
        "  br i1 %c, label %then, label %else\n",
        "  ret void\n",
        "  %% \n",
        "%",
        "",
        "  %a.b.c = add i64 %a, 1\n",
    ]
    probes = ["v1", "s1", "s10", "s0", "s", "agg", "x", "y", "z", "c", "then",
              "else", "a", "a.b.c", "b", ""]
    for line in lines:
        tokens = set(m._ssa_names_in(line))
        for probe in probes:
            if probe == "":
                continue
            assert (probe in tokens) == m._contains_ssa_name(line, probe), (
                line, probe, sorted(tokens)
            )


def test_fused_traversal_matches_two_separate_passes():
    multi_function = _SCALAR_IR + "\n" + _STRUCT_IR + "\n" + _SCALAR_IR
    for text in (_SCALAR_IR, _STRUCT_IR, multi_function):
        assert compiled_default_passes._rewrite_functions(text) == (
            _two_pass_reference(text)
        )


def test_fused_traversal_preserves_non_function_and_truncated_text():
    preamble = 'target triple = "arm64-apple-macosx"\n@g = global i64 0\n'
    truncated = "define i64 @half(i64 %a) {\nentry:\n  ret i64 %a\n"
    for text in (preamble, preamble + truncated, ""):
        assert compiled_default_passes._rewrite_functions(text) == (
            _two_pass_reference(text)
        )


def test_rewrite_functions_is_called_directly_not_as_a_value():
    """A transform passed as a value lowers to the dynamic call path.

    ``py_obj_call`` plus a native adapter runs once per ``define``; keeping
    the calls direct is what removed ~65% of a self-hosted pcc1 compile.
    """
    source = compiled_default_passes.__file__
    with open(source, "r", encoding="utf-8") as stream:
        text = stream.read()
    assert "_rewrite_functions(text, _mem2reg_function)" not in text
    assert "_rewrite_functions(current, _sroa_function)" not in text
    assert "_sroa_function(_mem2reg_function(function_lines))" in text
