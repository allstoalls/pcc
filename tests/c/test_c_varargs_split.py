from __future__ import annotations


def test_c_varargs_split_rewrites_and_reports():
    from pcc.frontends.c.codegen.c_varargs import build_report, postprocess_varargs_ir

    ir = '''declare i32 @"__pcc_va_arg_1"(ptr %ap)
define i32 @f(ptr %ap) {
entry:
  %x = call i32 @"__pcc_va_arg_1"(ptr %ap)
  ret i32 %x
}
'''
    rewrites = []
    out = postprocess_varargs_ir(ir, report=rewrites)
    assert "__pcc_va_arg" not in out
    assert "%x = va_arg ptr %ap, i32" in out
    report = build_report(rewrites).to_json()
    assert report["count"] == 1
    assert report["rewrites"][0]["result_type"] == "i32"


def test_c_codegen_exposes_postprocess_report():
    from pcc.frontends.c.codegen.c_codegen import postprocess_ir_text_with_report

    ir = '''declare i64 @__pcc_va_arg_2(ptr %ap)
define i64 @f(ptr %ap) {
entry:
  %x = call i64 @__pcc_va_arg_2(ptr %ap)
  ret i64 %x
}
'''
    out, report = postprocess_ir_text_with_report(ir)
    assert "va_arg ptr %ap, i64" in out
    assert report.to_json()["count"] == 1


def test_varargs_rewrite_handles_typed_calls_and_quoted_ssa_names():
    from pcc.frontends.c.codegen.c_varargs import postprocess_varargs_ir

    ir = '''declare double @__pcc_va_arg_17(ptr %ap)
define double @f(ptr %ap) {
entry:
  %"value with spaces" = call double (ptr) @__pcc_va_arg_17(ptr %"cursor with spaces")
  ret double %"value with spaces"
}
'''
    rewrites = []
    out = postprocess_varargs_ir(ir, report=rewrites)
    assert (
        '%"value with spaces" = va_arg ptr %"cursor with spaces", double'
        in out
    )
    assert len(rewrites) == 1
    assert rewrites[0].helper == "__pcc_va_arg_17"
    assert rewrites[0].arg_value == '%"cursor with spaces"'


def test_varargs_rewrite_leaves_non_helper_ir_byte_identical():
    from pcc.frontends.c.codegen.c_varargs import postprocess_varargs_ir

    ir = "define i64 @plain(i64 %x) {\n  ret i64 %x\n}\n"
    assert postprocess_varargs_ir(ir) == ir


def test_varargs_rewrite_does_not_delete_similar_non_numeric_symbol():
    from pcc.frontends.c.codegen.c_varargs import postprocess_varargs_ir

    ir = "declare i64 @__pcc_va_arg_helper(ptr %x)\n"
    assert postprocess_varargs_ir(ir) == ir
