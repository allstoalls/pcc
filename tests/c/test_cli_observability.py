from __future__ import annotations

import json

from pcc.driver.cli_observability import (
    emit_exception_diagnostic,
    normalize_diagnostic_format,
    parse_observability_args,
)


def test_parse_observability_args_strips_flags():
    rest, fmt, profile = parse_observability_args([
        "--diagnostic-format=json", "--profile-json", "p.json", "prog.py",
    ])
    assert rest == ["prog.py"]
    assert fmt == "json"
    assert profile == "p.json"


def test_exception_diagnostic_json_is_machine_readable():
    payload = emit_exception_diagnostic(ValueError("bad"), fmt="json", code="PCC-X")
    data = json.loads(payload)
    assert data["diagnostics"][0]["code"] == "PCC-X"


def test_bad_diagnostic_format_fails():
    try:
        normalize_diagnostic_format("xml")
    except ValueError as exc:
        assert "diagnostic-format" in str(exc)
    else:
        raise AssertionError("expected ValueError")
