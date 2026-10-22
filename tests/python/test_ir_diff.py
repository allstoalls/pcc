
from __future__ import annotations

from pcc.diagnostics.ir_diff import IrSummary, diff_ir


def test_ir_summary_counts_functions_and_calls():
    ir = """
@G = global i64 0
define ptr @main() {
entry:
  %x = call ptr @foo()
  ret ptr %x
}
define ptr @foo() {
entry:
  ret ptr null
}
"""
    summary = IrSummary.parse(ir)
    assert set(summary.functions) == {"main", "foo"}
    assert summary.functions["main"].calls == ("foo",)
    assert "G" in summary.globals


def test_ir_diff_reports_structural_changes():
    lhs = "define void @f() {\n  ret void\n}\n"
    rhs = "define void @f() {\n  call void @g()\n  ret void\n}\ndefine void @g() {\n  ret void\n}\n"
    diff = diff_ir(lhs, rhs)
    assert not diff.is_empty()
    assert diff.extra_functions == ("g",)
    assert diff.changed_instruction_counts == (("f", 1, 2),)
