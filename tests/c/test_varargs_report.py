import json

from pcc.frontends.c.varargs_report import VarargsRewrite, VarargsRewriteReport


def test_varargs_report_json():
    report = VarargsRewriteReport()
    report.add(VarargsRewrite("__pcc_va_arg_1", "i32", "ptr", "%ap"))
    assert json.loads(report.format_json())["count"] == 1
