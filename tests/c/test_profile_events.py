from __future__ import annotations

import json

from pcc.diagnostics.profile_events import ProfileRecorder, make_subprocess_event


def test_profile_recorder_schema_and_phase_totals():
    rec = ProfileRecorder()
    rec.set_metadata("backend", "self")
    with rec.phase("parse"):
        rec.increment("tokens", 3)
    with rec.phase("codegen", metadata={"module": "m"}):
        rec.increment("ir_functions", 1)
    payload = rec.to_json()
    assert payload["schema"] == "pcc.profile.v1"
    assert payload["metadata"]["backend"] == "self"
    assert payload["counters"]["tokens"] == 3
    assert "parse" in payload["phase_totals_ms"]
    assert any(e["metadata"].get("module") == "m" for e in payload["events"])


def test_profile_recorder_json_is_machine_readable():
    rec = ProfileRecorder()
    rec.record_event("ir-size", category="ir", metadata={"instructions": 42})
    payload = json.loads(rec.format_json())
    assert payload["events"][0]["category"] == "ir"
    assert payload["events"][0]["metadata"]["instructions"] == 42


def test_subprocess_event_shape():
    event = make_subprocess_event(
        ["cc", "-c", "x.c"], returncode=0, duration_ns=1234, cwd="/tmp"
    )
    payload = event.to_json()
    assert payload["category"] == "subprocess"
    assert payload["metadata"]["argv"][0] == "cc"
    assert payload["metadata"]["returncode"] == 0
