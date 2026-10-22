from __future__ import annotations

import os

from pcc.driver.cli_core import parse_cli_args


def test_cli_core_parses_observability_flags(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    monkeypatch.delenv("PCC_DIAGNOSTIC_FORMAT", raising=False)
    monkeypatch.delenv("PCC_PROFILE_JSON", raising=False)
    monkeypatch.delenv("PCC_EXPLAIN_FALLBACK", raising=False)
    parsed, status, err = parse_cli_args([
        "--diagnostic-format", "json",
        "--profile-json", str(profile),
        "--explain-fallback",
        "prog.py",
        "arg1",
    ])
    assert status == 0, err
    assert parsed is not None
    assert os.environ["PCC_DIAGNOSTIC_FORMAT"] == "json"
    assert os.environ["PCC_PROFILE_JSON"] == str(profile)
    assert os.environ["PCC_EXPLAIN_FALLBACK"] == "1"


def test_cli_core_rejects_bad_diagnostic_format():
    parsed, status, err = parse_cli_args(["--diagnostic-format", "xml", "prog.py"])
    assert parsed is None
    assert status == 2
    assert "diagnostic-format" in err
