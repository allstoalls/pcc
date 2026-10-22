import json

from pcc.diagnostics.pass_explain import PassDecision, format_pass_explain


def test_pass_explain_json():
    data = json.loads(format_pass_explain([PassDecision("mem2reg", True, "default")], fmt="json"))
    assert data["ran"] == ["mem2reg"]
