from pcc.ir.optimization.tailcall_accumulator import rewrite_accumulator_tailcalls


def test_rewrite_accumulator_tailcalls_changes_ir_text():
    ir = """define i64 @fact(i64 %n, i64 %acc) {
entry:
  %next = call i64 @fact(i64 %n, i64 %acc)
  ret i64 %next
}
"""
    new_ir, rewrites = rewrite_accumulator_tailcalls(ir)
    assert rewrites and rewrites[0].rewritten
    assert "pcc.tailcall.accumulator" in new_ir
    assert "call i64 @fact" not in new_ir

    assert "%n = phi i64" in new_ir
    assert "%acc = phi i64" in new_ir
    assert "pcc.tailcall.preheader:" in new_ir
