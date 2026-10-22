from __future__ import annotations

from pcc.diagnostics.gc_log import parse_log_lines, summarize


def test_gc_log_summary_understands_current_pcc_runtime_json_schema():
    events = parse_log_lines([
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"alloc","event":"alloc_object","value0":40,"value1":5}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"alloc","event":"alloc_object","value0":56,"value1":6}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"gc","event":"collect_stop","value0":3,"value1":0}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"weakref","event":"callback","value0":0,"value1":0}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"finalizer","event":"call","value0":0,"value1":0}',
    ])

    summary = summarize(events).as_dict()
    assert summary["allocations"] == 2
    assert summary["allocated_bytes"] == 96
    assert summary["collections"] == 1
    assert summary["total_collected"] == 3
    assert summary["weakref_callbacks"] == 1
    assert summary["finalizers"] == 1
    assert summary["events_by_name"]["alloc/alloc_object"] == 2
    assert summary["events_by_name"]["gc/collect_stop"] == 1


def test_gc_log_summary_keeps_legacy_event_names_working():
    events = parse_log_lines([
        "alloc size=10",
        "gc_collect_stop tracked=2 visited=2 collected=1 pause_ms=0.5",
        "weakref_callback target=abc",
        "finalizer target=abc",
    ])
    summary = summarize(events).as_dict()
    assert summary["allocations"] == 1
    assert summary["allocated_bytes"] == 10
    assert summary["collections"] == 1
    assert summary["total_collected"] == 1
    assert summary["weakref_callbacks"] == 1
    assert summary["finalizers"] == 1
