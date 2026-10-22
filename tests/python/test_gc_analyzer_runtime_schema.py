from __future__ import annotations

from pcc.diagnostics.gc_analyzer import parse_gc_json_lines, summarize_gc_events


def test_gc_analyzer_accepts_current_runtime_jsonl_schema():
    events = parse_gc_json_lines([
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"alloc","event":"alloc_object","value0":40,"value1":5}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"gc","event":"store_ptr","value0":0,"value1":0}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"gc","event":"collect_stop","value0":2,"value1":0}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"refcount","event":"free","value0":0,"value1":5}',
        '{"schema":"pcc.diagnostics.runtime_log.v1","category":"dispatch","event":"call","value0":0,"value1":0}',
    ])

    # The GC analyzer intentionally keeps GC-adjacent object-lifetime events
    # and ignores unrelated dispatch events.
    assert len(events) == 4
    summary = summarize_gc_events(events)
    assert summary["events"]["alloc/alloc_object"] == 1
    assert summary["events"]["gc/store_ptr"] == 1
    assert summary["events"]["gc/collect_stop"] == 1
    assert summary["events"]["refcount/free"] == 1
    assert summary["allocated_bytes"] == 40


def test_gc_analyzer_keeps_legacy_gc_event_filter():
    events = parse_gc_json_lines(['{"event":"gc_collect_stop","collected":1}'])
    assert summarize_gc_events(events)["events"] == {"gc_collect_stop": 1}
