from pcc.driver.pass_env_decisions import explain_pass_selection


def test_pass_env_decisions_reports_disabled_and_allowlist():
    result = explain_pass_selection(["mem2reg", "dce"], ["mem2reg"], ["dce"], 2)
    text = result.format()
    assert "mem2reg: ran" in text
    assert "dce: skipped" in text
