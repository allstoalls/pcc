"""C language layout facts must follow the selected owned execution target."""

import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.c_abi_layout import builtin_integer_is_unsigned, builtin_scalar_layout, c_size_type_name
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.frontends.c.codegen.c_types import get_ir_type_from_names, resolve_node_type
from pcc.frontends.c.evaluator.c_evaluator import _compile_preprocessed_translation_unit_artifact
from pcc.frontends.c.parse import make_c_parser
from pcc.frontends.c.passes import PassContext, PassPipeline
from pcc.frontends.c.passes.escape_analysis import EscapeAnalysisPass
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass
from pcc.frontends.c.preprocessor import preprocess
from pcc.frontends.c.ssa.builder import SSABuilder
from pcc.frontends.c.ssa.ir import SSAValue


TARGETS = (
    ("arm64-apple-darwin", 8, 4, "double", 8, 8),
    ("x86_64-unknown-linux-gnu", 8, 4, "x86_fp80", 16, 16),
    ("aarch64-unknown-linux-gnu", 8, 4, "fp128", 16, 16),
    ("x86_64-pc-windows-msvc", 4, 2, "double", 8, 8),
)


C_CONTEXT_PROGRAM = '''from pcc.ir import ir
class TranslationUnit:
    def __init__(self):
        self.module = ir.Module()
        self.module.context = ir.Context()
def main():
    left = TranslationUnit()
    right = TranslationUnit()
    assert left.module.context is not right.module.context
    print("C_CONTEXT_OWNER_OK")
main()
'''


HOST_C_PROGRAM = r'''typedef __WCHAR_TYPE__ wchar_t;
struct Packet { char head; long number; wchar_t letter; void *pointer; };
long next(long value) { return value + 1; }
int compare(long left, unsigned int right) { return left < right; }
_Bool truth(int value) { return value; }
int main(void) {
    long (*callback)(long) = next;
    int values[3];
    if (sizeof(long) != __SIZEOF_LONG__) return 1;
    if (sizeof(wchar_t) != __SIZEOF_WCHAR_T__) return 2;
    if (sizeof(long double) != __SIZEOF_LONG_DOUBLE__) return 3;
    if (sizeof(sizeof(int)) != 8 || sizeof(&values[2] - &values[0]) != 8) return 4;
    if (callback(41) != 42) return 5;
    if (sizeof(_Bool) != 1 || !truth(2) || truth(0)) return 6;
#if __SIZEOF_LONG__ == 8
    if (!compare(-1, 1U)) return 7;
    if (sizeof(struct Packet) != 32) return 9;
#else
    if (compare(-1, 1U)) return 7;
    if (sizeof(struct Packet) != 24) return 9;
#endif
#ifdef __CHAR_UNSIGNED__
    if ((char)-1 < 0) return 8;
#else
    if (!((char)-1 < 0)) return 8;
#endif
    return 0;
}
'''


def _body(text, name):
    match = re.search(r"^define[^\n]*@" + re.escape(name) + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, name
    return match[1]


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_target_scalar_facts_agree_with_codegen_and_ssa(target, long_size, wchar_size, format_name, ld_size, ld_align):
    codegen = CCodeGenerator()
    codegen.set_target_text(target, "")
    for spelling, size, alignment in (("long", long_size, long_size), ("wchar_t", wchar_size, wchar_size), ("long double", ld_size, ld_align)):
        names = spelling.split()
        layout = builtin_scalar_layout(names, target)
        physical = get_ir_type_from_names(names, target)
        assert (layout.size, layout.alignment) == (size, alignment)
        assert (codegen._ir_type_size(physical), codegen._ir_type_align(physical)) == (size, alignment)
        assert SSABuilder._builtin_type_size(names, target) == size
        assert SSABuilder._builtin_type_align(names, target) == alignment
    assert str(get_ir_type_from_names(["long", "double"], target)) == format_name
    permuted = builtin_scalar_layout(["volatile", "double", "long"], target)
    assert (permuted.size, permuted.alignment) == (ld_size, ld_align)


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_target_reaches_high_tier_sizeof_and_object(target, long_size, wchar_size, format_name, ld_size, ld_align):
    source = preprocess("""
        typedef __WCHAR_TYPE__ wchar_t;
        int long_size(void) { return sizeof(long); }
        int wchar_size(void) { return sizeof(wchar_t); }
        int extended_size(void) { return sizeof(long double); }
        int nested_size(void) { return sizeof(sizeof(int)); }
        int long_bits(long value) { return value < 0; }
    """, target_triple=target)
    context = PassContext()
    pipeline = PassPipeline()
    pipeline.add_high(EscapeAnalysisPass())
    pipeline.add_high(SSABootstrapPass())
    artifact = _compile_preprocessed_translation_unit_artifact(
        "target_contract", source, pass_pipeline=pipeline, pass_ctx=context,
        target_triple=target,
    )
    assert context.target_triple == target
    assert context.get_var("long_bits", "value").bit_width == long_size * 8
    for function, value in (("long_size", long_size), ("wchar_size", wchar_size), ("extended_size", ld_size), ("nested_size", 8)):
        assert function in context.ssa_functions
        assert context.ssa_functions[function].blocks[-1].terminator.value.value == value
        assert "trunc i64 " + str(value) + " to i32" in _body(artifact["ir_text"], function)
    assert emit_owned_object(artifact["ir_text"], target)


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_function_pointer_recursion_preserves_target(target, long_size, wchar_size, format_name, ld_size, ld_align):
    tree = make_c_parser().parse("long (*callback)(long);")
    pointer = resolve_node_type(tree.ext[0].type, target)
    assert pointer.pointee.return_type.width == long_size * 8
    assert pointer.pointee.args[0].width == long_size * 8


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_target_unsigned_conversion_and_size_types(target, long_size, wchar_size, format_name, ld_size, ld_align):
    builder = SSABuilder(target)
    left = SSAValue(name="signed", type_name="long")
    right = SSAValue(name="unsigned", type_name="unsigned int")
    common = "long" if long_size == 8 else "unsigned long"
    assert builder._binary_result_type("+", left, right) == common
    assert SSABuilder._binary_result_type("+", right, left, target_triple=target) == common
    pointer = SSAValue(name="pointer", type_name="int*")
    assert builtin_scalar_layout(builder._binary_result_type("-", pointer, pointer).split(), target).size == 8
    assert builtin_scalar_layout(c_size_type_name(target).split(), target).size == 8
    assert SSABuilder._int_literal_type_name("2147483648L", target) == ("long" if long_size == 8 else "long long")
    assert SSABuilder._int_literal_type_name("1LL", target) == "long long"
    assert SSABuilder._int_literal_type_name("0b" + "1" * 32, target) == "unsigned int"


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_target_predefines_and_signedness(target, long_size, wchar_size, format_name, ld_size, ld_align):
    processed = preprocess("__SIZEOF_LONG__ __SIZEOF_WCHAR_T__ __SIZEOF_LONG_DOUBLE__ __LDBL_MANT_DIG__", target_triple=target)
    expected_mantissa = {"double": 53, "x86_fp80": 64, "fp128": 113}[format_name]
    assert processed.split() == [str(long_size), str(wchar_size), str(ld_size), str(expected_mantissa)]
    codegen = CCodeGenerator()
    codegen.set_target_text(target, "")
    plain_unsigned = target.startswith("aarch64-")
    assert codegen._is_unsigned_type_names(["char"]) is plain_unsigned
    assert builtin_integer_is_unsigned(["char"], target) is plain_unsigned
    assert codegen._is_unsigned_type_names(["signed", "char"]) is False
    assert codegen._is_unsigned_type_names(["unsigned", "char"]) is True


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_extended_struct_layout_matches_ir_storage(target, long_size, wchar_size, format_name, ld_size, ld_align):
    tree = make_c_parser().parse("struct Shape { char head; long double value; char tail; }; int probe(void) { return sizeof(struct Shape); }")
    builder = SSABuilder(target)
    builder.index_file_scope(tree)
    codegen = CCodeGenerator()
    codegen.set_target_text(target, "")
    physical = codegen._resolve_ast_type(tree.ext[0].type)
    size = 3 * ld_align
    assert builder._ast_type_size(tree.ext[0].type) == size
    assert builder._ast_type_align(tree.ext[0].type) == ld_align
    assert codegen._ir_type_size(physical) == size
    assert codegen._ir_type_align(physical) == ld_align


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
@pytest.mark.parametrize("use_ssa", (False, True), ids=("ast", "ssa"))
def test_selected_target_controls_hot_compare_and_pointer_difference(target, long_size, wchar_size, format_name, ld_size, ld_align, use_ssa):
    pipeline = PassPipeline()
    if use_ssa:
        pipeline.add_high(SSABootstrapPass())
    context = PassContext()
    artifact = _compile_preprocessed_translation_unit_artifact(
        "hot_abi", """
        int compare(long left, unsigned int right) { return left < right; }
        int difference_width(int *left, int *right) { return sizeof(left - right); }
        int plain_char(char value) { return value < 0; }
        """, pass_pipeline=pipeline, pass_ctx=context, target_triple=target,
    )
    text = artifact["ir_text"]
    comparison = _body(text, "compare")
    assert ("icmp slt i64" if long_size == 8 else "icmp ult i32") in comparison
    difference = _body(text, "difference_width")
    assert ("trunc i64 8 to i32" in difference or "ret i32 8" in difference)
    assert ("icmp ult i8" if target.startswith("aarch64-") else "icmp slt i8") in _body(text, "plain_char") or (
        target.startswith("aarch64-") and "icmp slt i32" in _body(text, "plain_char") and "zext i8" in _body(text, "plain_char")
    ) or ("icmp slt i32" in _body(text, "plain_char") and "sext i8" in _body(text, "plain_char"))
    if use_ssa:
        assert "compare" in context.ssa_functions
        assert "difference_width" in context.ssa_functions
    assert emit_owned_object(text, target)


def test_target_switch_clears_old_ssa_before_high_tier():
    from pcc.frontends.c.passes.base import ASTPass
    class ObserveTarget(ASTPass):
        name = "observe-target"
        def run(self, ast, context):
            assert context.target_triple == "x86_64-pc-windows-msvc"
            assert context.ssa_functions == {}
    pipeline = PassPipeline()
    pipeline.add_high(ObserveTarget())
    pipeline.add_high(SSABootstrapPass())
    context = PassContext(target_triple="arm64-apple-darwin")
    context.ssa_functions["old_target"] = object()
    artifact = _compile_preprocessed_translation_unit_artifact(
        "switch_abi", "int probe(void) { return sizeof(long); }",
        pass_pipeline=pipeline, pass_ctx=context, target_triple="x86_64-pc-windows-msvc",
    )
    assert "trunc i64 4 to i32" in _body(artifact["ir_text"], "probe")
    assert emit_owned_object(artifact["ir_text"], "x86_64-pc-windows-msvc")


def test_translation_unit_contexts_do_not_retain_other_targets():
    first = CCodeGenerator(translation_unit_name="first")
    first.set_target_text("x86_64-unknown-linux-gnu", "")
    tree = make_c_parser().parse("struct Foreign { long double member; };")
    first._resolve_ast_type(tree.ext[0].type)
    assert first.module.context.identified_types
    second = CCodeGenerator(translation_unit_name="second")
    second.set_target_text("arm64-apple-darwin", "")
    assert first.module.context is not second.module.context
    assert second.module.context.identified_types == {}
    second.generate_code(make_c_parser().parse("int probe(void) { return sizeof(long); }"))
    text = str(second.module)
    assert "Foreign" not in text and "x86_fp80" not in text
    assert emit_owned_object(text, "arm64-apple-darwin")


@pytest.mark.parametrize("target", tuple(row[0] for row in TARGETS))
def test_native_context_constructor_shape_reaches_owned_emitter(tmp_path, monkeypatch, target):
    from pcc.frontends.python.pipeline import compile_python
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "c_context_owner.py"
    output = tmp_path / "c_context_owner.ll"
    source.write_text(C_CONTEXT_PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on", target_triple=target)
    text = output.read_text()
    constructor = _body(text, "user_c_context_owner_TranslationUnit___init__")
    assert "@user_pcc_ir_ir_scaffold_Module___init__(" in constructor
    assert "@user_pcc_ir_ir_scaffold_Context(" in constructor
    assert "strict.nolib.stub" not in constructor
    assert emit_owned_object(text, target)


def test_windows_gnu_long_double_keeps_extended_format():
    target = "x86_64-w64-windows-gnu"
    physical = get_ir_type_from_names(["long", "double"], target)
    codegen = CCodeGenerator()
    codegen.set_target_text(target, "")
    assert str(physical) == "x86_fp80"
    assert codegen._ir_type_size(physical) == codegen._ir_type_align(physical) == 16
    assert preprocess("__SIZEOF_LONG_DOUBLE__ __LDBL_MANT_DIG__", target_triple=target).split() == ["16", "64"]


def test_unknown_target_fails_before_high_tier():
    with pytest.raises(ValueError, match="unsupported C target ABI"):
        _compile_preprocessed_translation_unit_artifact("unknown", "int main(void) { return 0; }", target_triple="mystery-target")


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
def test_named_aggregate_target_layout_reaches_owned_object(target, long_size, wchar_size, format_name, ld_size, ld_align):
    source = preprocess("""
        typedef __WCHAR_TYPE__ wchar_t;
        struct Packet { char head; long number; wchar_t letter; void *pointer; };
        int packet_size(void) { return sizeof(struct Packet); }
        int packet_align(void) { return _Alignof(struct Packet); }
        int pointer_offset(void) {
            return (int)((char *)&((struct Packet *)0)->pointer - (char *)0);
        }
    """, target_triple=target)
    artifact = _compile_preprocessed_translation_unit_artifact(
        "packet", source, pass_pipeline=PassPipeline(), target_triple=target,
    )
    text = artifact["ir_text"]
    size = 32 if long_size == 8 else 24
    assert "ret i32 " + str(size) in _body(text, "packet_size") or "trunc i64 " + str(size) in _body(text, "packet_size")
    assert "ret i32 8" in _body(text, "packet_align") or "trunc i64 8" in _body(text, "packet_align")
    pointer = _body(text, "pointer_offset")
    assert "inttoptr i64 " + str(24 if long_size == 8 else 16) + " to ptr" in pointer
    assert re.search(r"^%[^\n]*Packet[^\n]*= type", text, re.M)
    assert emit_owned_object(text, target)


@pytest.mark.parametrize("use_ssa", (False, True), ids=("ast", "ssa"))
def test_bool_storage_uses_truth_conversion(use_ssa):
    pipeline = PassPipeline()
    if use_ssa:
        pipeline.add_high(SSABootstrapPass())
    artifact = _compile_preprocessed_translation_unit_artifact(
        "bool_abi", "_Bool constant = (_Bool)2; _Bool truth(int value) { return value; } int width(void) { return sizeof(_Bool); }",
        pass_pipeline=pipeline, target_triple="arm64-apple-darwin",
    )
    text = artifact["ir_text"]
    assert re.search(r"@constant =[^\n]*global i1 1", text)
    assert "icmp ne i32" in _body(text, "truth")
    assert "trunc i32" not in _body(text, "truth")
    assert "trunc i64 1 to i32" in _body(text, "width")
    assert emit_owned_object(text, "arm64-apple-darwin")


@pytest.mark.parametrize("target,long_size,wchar_size,format_name,ld_size,ld_align", TARGETS)
@pytest.mark.parametrize("use_ssa", (False, True), ids=("ast", "ssa"))
def test_integer_literal_type_queries_share_target(target, long_size, wchar_size, format_name, ld_size, ld_align, use_ssa):
    pipeline = PassPipeline()
    if use_ssa:
        pipeline.add_high(SSABootstrapPass())
    artifact = _compile_preprocessed_translation_unit_artifact(
        "literal_abi", """
        int one_long(void) { return sizeof(1L); }
        int two_long(void) { return sizeof(1LL); }
        int wide_decimal(void) { return sizeof(2147483648); }
        int hex_unsigned(void) { return sizeof(0xffffffff); }
        int extended_literal(void) { return sizeof(1.0L); }
        """, pass_pipeline=pipeline, target_triple=target,
    )
    for name, expected in (("one_long", long_size), ("two_long", 8), ("wide_decimal", 8), ("hex_unsigned", 4), ("extended_literal", ld_size)):
        assert "trunc i64 " + str(expected) + " to i32" in _body(artifact["ir_text"], name)
    assert emit_owned_object(artifact["ir_text"], target)


@pytest.mark.integration
def test_host_changed_c_abi_shapes_execute(tmp_path):
    from pcc.driver.cli_core import cli_main
    source = tmp_path / "host_abi.c"
    binary = tmp_path / "host_abi"
    source.write_text(HOST_C_PROGRAM)
    assert cli_main(["--backend", "self", "--no-cache", str(source), "-o", str(binary)]) == 0
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout == result.stderr == ""
