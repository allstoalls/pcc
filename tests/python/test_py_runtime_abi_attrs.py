from __future__ import annotations

from tests.owned_ir_validation import verify_ir_text


from pcc.ir.compat import ir_py as ir
from pcc.frontends.python.codegen.runtime_abi import declare_runtime


def test_runtime_declarations_emit_optimization_attrs():
    module = ir.Module(name="runtime_attrs_probe")

    runtime = declare_runtime(module)

    assert "py_bool_from_bit" in runtime
    text = str(module)
    assert (
        "declare external ptr @py_bool_from_bit(i32) "
        "nounwind readnone willreturn"
    ) in text
    assert (
        "declare external i64 @py_list_len(ptr) "
        "nounwind readonly willreturn"
    ) in text
    verify_ir_text(text)


def test_dce_removes_unused_readonly_runtime_call():
    from pcc.frontends.python.ir_pass_pipeline import run_python_ir_pass_pipeline

    ir_text = """
declare i64 @py_list_len(ptr) nounwind readonly willreturn

define i32 @main(ptr %p) {
entry:
  %n = call i64 @py_list_len(ptr %p)
  ret i32 0
}
"""

    out = run_python_ir_pass_pipeline(
        ir_text, pass_names=("dce",), module_name="probe",
    )

    assert "call i64 @py_list_len" not in out
    verify_ir_text(out)
