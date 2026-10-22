
from __future__ import annotations

import json

from pcc.diagnostics.gc_log import RuntimeLogEvent, parse_log_line, parse_log_lines, summarize, validate_gc_event


def test_json_log_event_roundtrip_and_summary():
    ev = RuntimeLogEvent("gc_collect_stop", {
        "generation": 0,
        "tracked": 10,
        "visited": 8,
        "collected": 3,
        "pause_ms": 0.5,
    })
    parsed = parse_log_line(ev.to_json_line())
    assert parsed == ev
    summary = summarize([parsed])
    assert summary.collections == 1
    assert summary.total_collected == 3
    assert summary.max_pause_ms == 0.5
    assert validate_gc_event(parsed) == []


def test_text_log_parser_and_alloc_summary():
    events = parse_log_lines([
        "alloc type=PyList size=64 thread_id=1",
        "weakref_callback target=Box",
        "finalizer type=Box",
        "gc_collect_stop generation=0 tracked=4 visited=4 collected=2 pause_ms=1.25",
    ])
    summary = summarize(events)
    assert summary.allocations == 1
    assert summary.allocated_bytes == 64
    assert summary.weakref_callbacks == 1
    assert summary.finalizers == 1
    assert summary.total_collected == 2


def test_validation_reports_missing_required_fields():
    ev = RuntimeLogEvent("gc_collect_stop", {"tracked": "many"})
    problems = validate_gc_event(ev)
    assert "tracked should be numeric" in problems
    assert "gc_collect_stop missing collected" in problems
