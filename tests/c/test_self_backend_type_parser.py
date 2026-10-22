"""Focused contracts for the structural self-backend LLVM type parser."""

from __future__ import annotations

import ast
import inspect

import pytest

import pcc.backend.self_backend_parse as parser
from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import (
    _tokenize_ir_type,
    extract_leading_type_token,
    parse_ir_type,
    parse_self_backend_module,
    strip_typed_initializer,
)


def _direct_call_names(function) -> set[str]:
    tree = ast.parse(inspect.getsource(function))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _diagnostic_instruction(function, index: int = 0):
    return get_indexed_function_kernel(function).diagnostic_instruction(0, index)


def test_type_surfaces_share_the_token_parser_source_contract() -> None:
    assert "_parse_ir_type_tokens" in _direct_call_names(parser._parse_type)
    assert "_next_ir_type_token" in _direct_call_names(
        parser._parse_ir_type_tokens
    )
    assert "_next_ir_type_token" in _direct_call_names(parser._tokenize_ir_type)
    assert "split_top_level" not in _direct_call_names(parser._parse_type)
    assert "_parse_ir_type_prefix" in _direct_call_names(
        parser._extract_leading_type_token
    )
    assert "_parse_ir_type_list" in _direct_call_names(
        parser._parse_call_signature
    )
    assert "split_top_level" not in _direct_call_names(
        parser._parse_call_signature
    )


def test_ir_type_tokenizer_keeps_quoted_names_and_nesting_structural() -> None:
    tokens = _tokenize_ir_type(
        '{ [2 x { i8, <4 x i16> }], %"pair, quoted"* } trailing'
    )
    spellings = [token[1] for token in tokens]

    assert '%"pair, quoted"' in spellings
    assert spellings[:6] == ["{", "[", "2", "x", "{", "i8"]
    assert spellings[-4:] == ['%"pair, quoted"', "*", "}", "trailing"]
    quoted = next(token for token in tokens if token[1] == '%"pair, quoted"')
    assert quoted[0] == "atom"
    assert quoted[3] - quoted[2] == len('%"pair, quoted"')


def test_parse_ir_type_recurses_through_supported_nested_shapes() -> None:
    i8 = TypeDesc("int", 8)
    i16 = TypeDesc("int", 16)
    pair = TypeDesc(
        "struct",
        fields=(TypeDesc("int", 64), TypeDesc("int", 64)),
    )
    nested = TypeDesc(
        "struct",
        fields=(i8, TypeDesc("array", count=4, elem=i16)),
    )
    expected = TypeDesc(
        "struct",
        fields=(
            TypeDesc("array", count=2, elem=nested),
            TypeDesc("ptr", pointee=pair),
        ),
    )

    assert (
        parse_ir_type("{ [2 x { i8, <4 x i16> }], { i64, i64 }* }")
        == expected
    )


def test_extract_leading_type_uses_structural_parser_boundary() -> None:
    source = (
        "{ [2 x { i8, i16 }], <4 x i32> } "
        "zeroinitializer, align 16"
    )

    type_text, remainder = extract_leading_type_token(source)

    assert type_text == "{ [2 x { i8, i16 }], <4 x i32> }"
    assert remainder == "zeroinitializer, align 16"
    assert parse_ir_type(type_text) == TypeDesc(
        "struct",
        fields=(
            TypeDesc(
                "array",
                count=2,
                elem=TypeDesc(
                    "struct",
                    fields=(TypeDesc("int", 8), TypeDesc("int", 16)),
                ),
            ),
            TypeDesc("array", count=4, elem=TypeDesc("int", 32)),
        ),
    )


def test_leading_type_parser_does_not_materialize_initializer_tokens() -> None:
    source = '[4096 x i8] c"unterminated initializer is value-layer syntax'

    assert extract_leading_type_token(source) == (
        "[4096 x i8]",
        'c"unterminated initializer is value-layer syntax',
    )


def test_typed_initializer_stripping_uses_the_same_nested_type_boundary() -> None:
    initializer = "{ { i64 1, i64 2 }, [i8 3, i8 4] }"
    typed = "{ { i64, i64 }, [2 x i8] } " + initializer

    assert strip_typed_initializer(typed) == initializer
    assert strip_typed_initializer(initializer) == initializer


def test_module_parser_shares_structural_types_across_incident_surfaces() -> None:
    ir_text = r'''
target triple = "arm64-apple-darwin23.6.0"
%unused_opaque = type opaque
%unused_packed = type <{ i8, i32 }>
%pair = type { i64, i64 }
%envelope = type { [2 x %pair], { i8, <4 x i16> } }
@nested = internal global { [2 x { i8, i16 }], <4 x i32> } zeroinitializer, align 16
@vector = internal global <4 x i32> zeroinitializer

define %envelope @identity(%envelope %value) {
entry:
  ret %envelope %value
}

define i64 @caller({ [2 x { i8, i16 }], <4 x i32> } %payload) {
entry:
  %r = call i64 ({ [2 x { i8, i16 }], <4 x i32> }, i64, ...) @consume({ [2 x { i8, i16 }], <4 x i32> } %payload, i64 9)
  ret i64 %r
}
'''.strip()

    module = parse_self_backend_module(ir_text)
    literal = parse_ir_type("{ [2 x { i8, i16 }], <4 x i32> }")
    pair = TypeDesc(
        "struct",
        name="%pair",
        fields=(TypeDesc("int", 64), TypeDesc("int", 64)),
    )
    envelope = TypeDesc(
        "struct",
        name="%envelope",
        fields=(
            TypeDesc("array", count=2, elem=pair),
            TypeDesc(
                "struct",
                fields=(
                    TypeDesc("int", 8),
                    TypeDesc("array", count=4, elem=TypeDesc("int", 16)),
                ),
            ),
        ),
    )

    assert module.globals_[0].type == literal
    assert module.globals_[0].alignment == 16
    assert module.globals_[1].type == TypeDesc(
        "array",
        count=4,
        elem=TypeDesc("int", 32),
    )
    assert module.functions[0].ret_type == envelope
    assert module.functions[0].args[0].type == envelope
    kernel = get_indexed_function_kernel(module.functions[1])
    call = kernel.diagnostic_instruction(0, 0)
    assert call.kind == "call"
    assert call.data[4] == ((literal, "payload"), (TypeDesc("int", 64), "9"))
    assert call.data[5:] == (2, True, (0, 0))


def test_module_parser_interns_repeated_leaf_types_by_identity() -> None:
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"

@first = internal global ptr null
@second = internal global ptr null
@typed_first = internal global i64* null
@typed_second = internal global i64* null

declare i64 @sum(i64, i64)

define i64 @caller(i64 %left, i64 %right) {
entry:
  %result = call i64 @sum(i64 %left, i64 %right)
  ret i64 %result
}
'''.strip()

    module = parse_self_backend_module(ir_text)
    first_ptr = module.globals_[0].type
    second_ptr = module.globals_[1].type
    typed_first_ptr = module.globals_[2].type
    typed_second_ptr = module.globals_[3].type
    function = module.functions[0]
    call = _diagnostic_instruction(function)

    assert first_ptr is second_ptr
    assert first_ptr.pointee is second_ptr.pointee
    assert typed_first_ptr is typed_second_ptr
    assert typed_first_ptr.pointee is function.ret_type
    assert function.ret_type is function.args[0].type
    assert function.args[0].type is function.args[1].type
    assert function.args[0].type is call.data[1]
    assert function.args[0].type is call.data[4][0][0]
    assert call.data[4][0][0] is call.data[4][1][0]


def test_call_parser_preserves_exact_pointer_argument_alignments() -> None:
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"

declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i1)

define void @copy(ptr %dst, ptr %src) {
entry:
  call void @llvm.memcpy.p0.p0.i64(ptr align 16 %dst, ptr align 8 %src, i64 32, i1 false)
  ret void
}
'''.strip()

    call = _diagnostic_instruction(parse_self_backend_module(ir_text).functions[0])

    assert call.kind == "call"
    assert call.data[4] == (
        (TypeDesc("ptr", pointee=TypeDesc("void")), "dst"),
        (TypeDesc("ptr", pointee=TypeDesc("void")), "src"),
        (TypeDesc("int", 64), "32"),
        (TypeDesc("int", 1), "0"),
    )
    assert call.data[7] == (16, 8, 0, 0)


def test_call_parser_canonicalizes_boolean_argument_aliases() -> None:
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"

declare void @consume_bools(i1, i1)

define void @caller() {
entry:
  call void @consume_bools(i1 false, i1 true)
  ret void
}
'''.strip()

    call = _diagnostic_instruction(parse_self_backend_module(ir_text).functions[0])

    assert call.kind == "call"
    assert call.data[4] == (
        (TypeDesc("int", 1), "0"),
        (TypeDesc("int", 1), "1"),
    )


def test_call_parser_rejects_non_power_of_two_argument_alignment() -> None:
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"

declare void @sink(ptr)

define void @caller(ptr %value) {
entry:
  call void @sink(ptr align 3 %value)
  ret void
}
'''.strip()

    with pytest.raises(BackendUnavailable, match="invalid alignment 3"):
        parse_self_backend_module(ir_text)


@pytest.mark.parametrize(
    "type_text",
    [
        "{ i64, [2 x i8]",
        "{ i64,, i8 }",
        "[two x i8]",
        "<{ i64, i64 }>",
        "ptr addrspace(1)",
        "i64 (i32)*",
    ],
)
def test_type_parser_rejects_unsupported_or_malformed_shapes(type_text: str) -> None:
    with pytest.raises(BackendUnavailable):
        parse_ir_type(type_text)


@pytest.mark.parametrize(
    "typed_value",
    [
        "ptr addrspace(1) %value",
        "i64 (i32)* %callback",
    ],
)
def test_leading_type_boundary_does_not_hide_unsupported_suffixes(
    typed_value: str,
) -> None:
    with pytest.raises(BackendUnavailable):
        extract_leading_type_token(typed_value)


def test_named_type_parser_rejects_malformed_nested_definition() -> None:
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"
%broken = type { i64,, { i8, i8 } }

define void @main() {
entry:
  ret void
}
'''.strip()

    with pytest.raises(BackendUnavailable):
        parse_self_backend_module(ir_text)


@pytest.mark.parametrize(
    "definition",
    [
        "opaque",
        "<{ i8, i32 }>",
    ],
)
def test_unsupported_named_type_declarations_fail_closed_when_referenced(
    definition: str,
) -> None:
    ir_text = f'''
target triple = "arm64-apple-darwin23.6.0"
%unsupported = type {definition}

define void @consume(%unsupported %value) {{
entry:
  ret void
}}
'''.strip()

    with pytest.raises(BackendUnavailable):
        parse_self_backend_module(ir_text)



# This oracle reverses only the two call-site edits. It executes the original
# alloca/GEP paths, including their type-ID publication and error ordering.
def _original_result_pointer_hot_parser():
    tree = ast.parse(inspect.getsource(parser._parse_indexed_hot_instruction))

    class OriginalPointers(ast.NodeTransformer):
        def __init__(self):
            self.seen = []

        def visit_Call(self, node):
            if (isinstance(node.func, ast.Name)
                    and node.func.id == "_indexed_result_pointer_type"):
                assert len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                assert ast.unparse(node.keywords[0]) == "type_context=type_context"
                self.seen.append(node.args[0].id)
                return ast.copy_location(ast.Call(
                    func=ast.Attribute(value=node.args[0], attr="ptr", ctx=ast.Load()),
                    args=[], keywords=[],
                ), node)
            return self.generic_visit(node)

    restore = OriginalPointers()
    tree = restore.visit(tree)
    assert restore.seen == ["alloca_type", "current_type"]
    namespace = dict(vars(parser))
    exec(compile(ast.fix_missing_locations(tree), "<original-result-pointers>", "exec"), namespace)
    return namespace["_parse_indexed_hot_instruction"]


def _pointer_result_ir(body, *, arguments=""):
    return ('target triple = "arm64-apple-darwin23.6.0"\n'
            + 'define i64 @pointer_result(' + arguments + ') {\nentry:\n'
            + body + '\n  ret i64 7\n}\n')


_POINTER_RESULT_BODIES = (
    "  %a = alloca i64\n  %b = alloca i64\n"
    "  %g = getelementptr i64, ptr %a, i64 0\n"
    "  %h = getelementptr i64, ptr %b, i64 0\n"
    "  store i64 3, ptr %g\n  %v = load i64, ptr %h",
    "  %a = alloca [2 x [3 x i64]]\n"
    "  %g = getelementptr [2 x [3 x i64]], ptr %a, i64 0, i64 1\n"
    "  %b = alloca [2 x [3 x i64]]\n"
    "  %h = getelementptr [2 x [3 x i64]], ptr %b, i64 0, i64 0\n"
    "  %s = getelementptr [3 x i64], ptr %g, i64 0, i64 1",
    "  %a = alloca ptr\n  %b = alloca i64*\n"
    "  %g = getelementptr ptr, ptr %a, i64 0\n"
    "  %h = getelementptr i64*, ptr %b, i64 0",
    "  %a = alloca { i32, double }\n"
    "  %g = getelementptr { i32, double }, ptr %a, i64 0, i32 1\n"
    "  %b = alloca double\n  %h = getelementptr double, ptr %b, i64 0",
)


def _pointer_result_snapshot(module):
    snapshots = []
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        columns = (
            "block_facts", "instruction_facts", "instruction_kind_ids",
            "instruction_metadata", "instruction_record_dest_ids",
            "instruction_record_scalars", "gep_index_scalars", "gep_scalars",
            "instruction_overflow_use_ids", "terminator_scalars",
            "value_scalars", "definition_positions", "used_value_ids",
            "type_scalars", "type_field_ids",
        )
        snapshots.append((
            function.name, function.ret_type, function.args,
            list(kernel.types), list(kernel.value_names), list(kernel.block_names),
            [(field, getattr(kernel, field).diagnostic_values()) for field in columns],
            [kernel.diagnostic_instruction(block, index)
             for block in range(len(kernel.block_names))
             for index in range(kernel.instruction_count(block))],
        ))
    return snapshots


@pytest.mark.parametrize("token", [
    "void", "i1", "i8", "i16", "i32", "i64", "i128",
    "float", "double", "x86_fp80", "ptr",
])
def test_result_pointer_reuses_only_context_owned_leaf_identity(token):
    from pcc.backend.self_backend_ir import TypeParseContext

    contexts = [TypeParseContext(target_triple="x86_64-unknown-linux-gnu") for _ in range(2)]
    pointers = []
    for context in contexts:
        leaf = parser._canonical_leaf_type(token, type_context=context)
        before = len(context.pointer_type_cache)
        first = parser._indexed_result_pointer_type(leaf, type_context=context)
        second = parser._indexed_result_pointer_type(leaf, type_context=context)
        assert first is second and first == leaf.ptr() and first.pointee is leaf
        assert context.pointer_type_cache[id(leaf)] == (leaf, first)
        assert len(context.pointer_type_cache) == before + 1
        pointers.append(first)
    assert pointers[0] == pointers[1] and pointers[0] is not pointers[1]
    assert pointers[0].pointee is not pointers[1].pointee


def test_result_pointer_fallback_keeps_unowned_aggregate_and_typed_pointer_fresh():
    from pcc.backend.self_backend_ir import TypeParseContext

    context = TypeParseContext()
    canonical = parser._canonical_leaf_type("i64", type_context=context)
    opaque = parser._canonical_leaf_type("ptr", type_context=context)
    unusual = parser._canonical_leaf_type("i37", type_context=context)
    leading_zero = parser._canonical_leaf_type("i064", type_context=context)
    cases = [TypeDesc("int", 64), unusual, leading_zero,
             TypeDesc("array", count=2, elem=canonical),
             TypeDesc("struct", fields=(canonical,)), canonical.ptr(), opaque.ptr()]
    before = dict(context.pointer_type_cache)
    for leaf in cases:
        first = parser._indexed_result_pointer_type(leaf, type_context=context)
        second = parser._indexed_result_pointer_type(leaf, type_context=context)
        assert first == second == leaf.ptr() and first is not second
        assert first.pointee is leaf and id(leaf) not in context.pointer_type_cache
    assert context.pointer_type_cache == before
    assert parser._indexed_result_pointer_type(canonical).pointee is canonical


def test_result_pointer_subclasses_and_nonbuiltin_fields_keep_original_ptr_contract():
    from pcc.backend.self_backend_ir import TypeParseContext

    events = []
    failure = RuntimeError("subclass ptr failure")

    class Leaf(TypeDesc):
        def ptr(self):
            events.append("ptr")
            raise failure

    class Kind(str):
        def __eq__(self, _other):
            raise AssertionError("new kind comparison")

    class Width(int):
        def __eq__(self, _other):
            raise AssertionError("new width comparison")
        def __str__(self):
            raise AssertionError("new width formatting")

    context = TypeParseContext()
    leaf = Leaf("int", 64)
    context.type_cache["i64"] = leaf
    with pytest.raises(RuntimeError) as actual:
        parser._indexed_result_pointer_type(leaf, type_context=context)
    assert actual.value is failure and events == ["ptr"]
    for value in (TypeDesc(Kind("int"), 64), TypeDesc("int", Width(64)), TypeDesc("int", True)):
        assert parser._indexed_result_pointer_type(value, type_context=context).pointee is value
    assert context.pointer_type_cache == {}


@pytest.mark.parametrize("body", _POINTER_RESULT_BODIES, ids=["scalar", "aggregate", "pointer", "field"])
def test_result_pointer_small_parser_preserves_ids_and_semantics(monkeypatch, body):
    text = _pointer_result_ir(body)
    actual = parse_self_backend_module(text)
    with monkeypatch.context() as control:
        control.setattr(parser, "_parse_indexed_hot_instruction", _original_result_pointer_hot_parser())
        expected = parse_self_backend_module(text)
    assert _pointer_result_snapshot(actual) == _pointer_result_snapshot(expected)


def test_result_pointer_first_gep_keeps_pointer_before_pointee_type_ids(monkeypatch):
    text = _pointer_result_ir("  %g = getelementptr i32, ptr %p, i64 0", arguments="ptr %p")
    actual = parse_self_backend_module(text)
    with monkeypatch.context() as control:
        control.setattr(parser, "_parse_indexed_hot_instruction", _original_result_pointer_hot_parser())
        expected = parse_self_backend_module(text)
    assert _pointer_result_snapshot(actual) == _pointer_result_snapshot(expected)
    kernel = get_indexed_function_kernel(actual.functions[0])
    result_id = kernel.value_type_id(kernel.value_id("g"))
    assert kernel.types[result_id].pointee == TypeDesc("int", 32)
    assert result_id < kernel.types.index(TypeDesc("int", 32))


@pytest.mark.parametrize("body", [
    "  %g = getelementptr i64, ptr %p, i64 0, i64 1",
    "  %g = getelementptr { i64, i64 }, ptr %p, i64 0, i64 %n",
    "  %g = getelementptr { i64, i64 }, ptr %p, i64 0, i64 5",
    "  %g = getelementptr i64, ptr addrspace(1) %p, i64 0",
], ids=["scalar-index", "variable-field", "field-bounds", "address-space"])
def test_result_pointer_preserves_parser_failure_order(monkeypatch, body):
    text = _pointer_result_ir(body, arguments="ptr %p, i64 %n")
    with pytest.raises((BackendUnavailable, IndexError)) as actual:
        parse_self_backend_module(text)
    with monkeypatch.context() as control:
        control.setattr(parser, "_parse_indexed_hot_instruction", _original_result_pointer_hot_parser())
        with pytest.raises((BackendUnavailable, IndexError)) as expected:
            parse_self_backend_module(text)
    assert type(actual.value) is type(expected.value)
    assert str(actual.value) == str(expected.value)


def test_result_pointer_repeated_scalar_constructs_one_wrapper(monkeypatch):
    body = "\n".join(
        f"  %a{index} = alloca i64\n  %g{index} = getelementptr i64, ptr %a{index}, i64 0"
        for index in range(12)
    )
    text = _pointer_result_ir(body)
    original_init = TypeDesc.__init__
    counts = []
    for hot_parser in (parser._parse_indexed_hot_instruction, _original_result_pointer_hot_parser()):
        made = []
        def count(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            if self.kind == "ptr" and self.pointee == TypeDesc("int", 64):
                made.append(1)
        with monkeypatch.context() as control:
            control.setattr(parser, "_parse_indexed_hot_instruction", hot_parser)
            control.setattr(TypeDesc, "__init__", count)
            module = parse_self_backend_module(text)
        assert len(module.functions) == 1
        counts.append(len(made))
    assert counts == [1, 24]


def test_result_pointer_fresh_aggregates_do_not_acquire_context_owners(monkeypatch):
    import weakref

    text = _pointer_result_ir("\n".join(
        f"  %a{index} = alloca [2 x [3 x i64]]\n"
        f"  %g{index} = getelementptr [2 x [3 x i64]], ptr %a{index}, i64 0, i64 0"
        for index in range(8)
    ))
    original_prefix = parser._parse_ir_type_prefix
    counts = []
    for hot_parser in (parser._parse_indexed_hot_instruction, _original_result_pointer_hot_parser()):
        owners = []
        def prefix(*args, **kwargs):
            result = original_prefix(*args, **kwargs)
            value = result[0]
            if value.kind == "array" and value.count == 2:
                owners.append((weakref.ref(value), weakref.ref(value.elem)))
            return result
        with monkeypatch.context() as control:
            control.setattr(parser, "_parse_indexed_hot_instruction", hot_parser)
            control.setattr(parser, "_parse_ir_type_prefix", prefix)
            module = parse_self_backend_module(text)
        context = module.type_context
        assert all(entry[0].kind not in ("array", "struct")
                   for entry in context.pointer_type_cache.values())
        alive = [tuple(reference() is not None for reference in pair) for pair in owners]
        assert len(owners) >= 8 and sum(root for root, _inner in alive) < len(owners)
        counts.append((alive, len(context.pointer_type_cache), _pointer_result_snapshot(module)))
    assert counts[0] == counts[1]


def test_result_pointer_modules_do_not_share_canonical_wrappers():
    text = _pointer_result_ir(_POINTER_RESULT_BODIES[0])
    modules = [parse_self_backend_module(text) for _ in range(2)]
    pointers = []
    for module in modules:
        context = module.type_context
        leaf = context.type_cache["i64"]
        pointer = context.pointer_type_cache[id(leaf)][1]
        kernel = get_indexed_function_kernel(module.functions[0])
        assert pointer is kernel.types[kernel.value_type_id(kernel.value_id("a"))]
        pointers.append(pointer)
    assert modules[0].type_context is not modules[1].type_context
    assert pointers[0] == pointers[1] and pointers[0] is not pointers[1]


@pytest.mark.parametrize("body", _POINTER_RESULT_BODIES[:2], ids=["scalar", "aggregate-fallback"])
def test_result_pointer_preserves_indexed_assembly_and_object_bytes(tmp_path, monkeypatch, body):
    # Normal CI only: the source-author/local pure selection excludes this node.
    from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
    from pcc.backend.self_backend_verify import verify_parsed_module
    from pcc.backend.self_backend_aarch64_darwin import (
        emit_aarch64_darwin_indexed_module,
        emit_aarch64_darwin_indexed_transport,
    )
    from pcc.backend.native_object import encode_native_object_from_sections

    text = _pointer_result_ir(body)
    outputs = []
    for index, hot_parser in enumerate((parser._parse_indexed_hot_instruction,
                                        _original_result_pointer_hot_parser())):
        with monkeypatch.context() as control:
            control.setattr(parser, "_parse_indexed_hot_instruction", hot_parser)
            module = parse_self_backend_module(text)
            verify_parsed_module(module)
            path = tmp_path / f"pointer-{index}.pidx"
            encode_indexed_module_file(str(path), module)
            assembly = emit_aarch64_darwin_indexed_module(module, optimize=False)
            transport = emit_aarch64_darwin_indexed_transport(parse_self_backend_module(text), optimize=False)
        sections, undefined = transport.assemble_sections()
        assert transport.native_finalized and transport.fallback_instruction_count == 0
        encoded = encode_native_object_from_sections(sections, undefined=undefined)
        outputs.append((path.read_bytes(), assembly, sections, undefined, encoded))
    assert outputs[0] == outputs[1]
