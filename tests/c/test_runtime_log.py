from __future__ import annotations

import json

from pcc.diagnostics.runtime_log import RuntimeEvent, format_event, summarize_events


def test_runtime_event_json():
    data = json.loads(format_event(RuntimeEvent("alloc", "gc", fields={"size": 8}), fmt="json"))
    assert data["event"] == "alloc"
    assert data["size"] == 8


def test_runtime_event_summary():
    summary = summarize_events([RuntimeEvent("x", "gc"), RuntimeEvent("x", "gc")])
    assert summary["gc:x"] == 2
