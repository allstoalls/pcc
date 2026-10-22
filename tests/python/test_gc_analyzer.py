import json

from pcc.diagnostics.gc_analyzer import parse_gc_json_lines, summarize_gc_events


def test_gc_analyzer_counts_gc_events():
    events = parse_gc_json_lines([
        json.dumps({"phase": "gc", "event": "gc_alloc", "type": "PyList"}),
    ])
    assert summarize_gc_events(events)["top_types"][0] == ("PyList", 1)
