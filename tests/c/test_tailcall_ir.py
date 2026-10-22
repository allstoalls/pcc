import json

from pcc.ir.optimization.tailcall_ir import analyze_self_tailcalls, format_tailcall_report


def test_tailcall_detector_finds_self_call():
    text = "define i64 @fact(i64 %n) {\nentry:\n  %x = call i64 @fact(i64 %n)\n  ret i64 %x\n}\n"
    assert analyze_self_tailcalls(text)[0].function == "fact"


def test_tailcall_report_schema():
    assert json.loads(format_tailcall_report(""))["schema"] == "pcc.tailcall.v1"
