
from __future__ import annotations

from pcc.diagnostics.heap_snapshot import HeapSnapshot


def test_heap_snapshot_reachability_and_cycle_leak_detection():
    snap = HeapSnapshot.from_json({
        "nodes": [
            {"id": "root", "type": "list", "root": True},
            {"id": "live", "type": "Box"},
            {"id": "a", "type": "Box"},
            {"id": "b", "type": "Box"},
        ],
        "edges": [
            {"src": "root", "dst": "live", "label": "items[0]"},
            {"src": "a", "dst": "b", "label": "peer"},
            {"src": "b", "dst": "a", "label": "peer"},
        ],
    })
    assert snap.reachable_from_roots() == {"root", "live"}
    assert snap.unreachable_nodes() == {"a", "b"}
    assert snap.likely_cycle_leaks() == [{"a", "b"}]


def test_self_cycle_is_reported_as_leak():
    snap = HeapSnapshot.from_json({
        "nodes": [{"id": "x", "type": "list"}],
        "edges": [{"src": "x", "dst": "x"}],
    })
    assert snap.likely_cycle_leaks() == [{"x"}]
