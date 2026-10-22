from __future__ import annotations

import json


def test_rewrite_simple_void_self_tailcall_changes_ir():
    from pcc.ir.optimization.tailcall_ir import rewrite_simple_void_self_tailcalls

    src = '''
define void @spin(i64 %n) {
entry:
  %next = sub i64 %n, 1
  call void @spin(i64 %next)
  ret void
}
'''
    result = rewrite_simple_void_self_tailcalls(src)
    assert result.rewritten is True
    assert "call void @spin" not in result.ir_text
    assert "; pcc.tailcall.self\n  br label %entry" in result.ir_text
    assert "pcc.tailcall.preheader:" in result.ir_text
    assert "%n = phi i64 [ %pcc.tailcall.arg.0, %pcc.tailcall.preheader ], [ %next, %entry ]" in result.ir_text
    assert result.candidates[0].rewritten is True


def test_value_return_tailcall_is_reported_but_not_lied_about():
    from pcc.ir.optimization.tailcall_ir import rewrite_simple_void_self_tailcalls

    src = '''
define i64 @fact_tail(i64 %n, i64 %acc) {
entry:
  %next = sub i64 %n, 1
  %acc2 = mul i64 %acc, %n
  %r = call i64 @fact_tail(i64 %next, i64 %acc2)
  ret i64 %r
}
'''
    result = rewrite_simple_void_self_tailcalls(src)
    assert result.ir_text == src
    assert result.candidates
    assert result.candidates[0].rewritten is False
    assert "phi-loop" in result.candidates[0].reason


def test_tailcall_rewrite_report_is_json():
    from pcc.ir.optimization.tailcall_ir import rewrite_simple_void_self_tailcalls

    result = rewrite_simple_void_self_tailcalls('''
define void @f() {
entry:
  call void @f()
  ret void
}
''')
    data = json.loads(result.report_json())
    assert data["schema"] == "pcc.tailcall.rewrite.v1"
    assert data["candidates"][0]["function"] == "f"
    assert data["candidates"][0]["rewritten"] is True

