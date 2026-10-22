from __future__ import annotations

from collections import Counter
import json
from typing import Iterable


def parse_gc_json_lines(lines: Iterable[str]) -> list[dict[str, object]]:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        category = str(event.get("category", "") or "")
        name = str(event.get("event", "") or "")
        if (
            category in {"gc", "alloc", "weakref", "finalizer", "refcount"}
            or event.get("phase") == "gc"
            or name.startswith("gc_")
        ):
            out.append(event)
    return out


def summarize_gc_events(events: list[dict[str, object]]) -> dict[str, object]:
    def event_key(e: dict[str, object]) -> str:
        category = str(e.get("category", "") or "")
        name = str(e.get("event", "") or "")
        if category:
            return category + "/" + name
        return name

    counts = Counter(event_key(e) for e in events)
    types = Counter(
        str(e.get("type") or e.get("object_type") or e.get("value1"))
        for e in events if e.get("type") or e.get("object_type") or e.get("value1")
    )
    allocated_bytes = sum(
        int(e.get("size", e.get("bytes", e.get("value0", 0))) or 0)
        for e in events
        if event_key(e) in {"alloc/alloc_object", "alloc"}
    )
    return {
        "count": len(events),
        "events": dict(counts),
        "top_types": types.most_common(10),
        "allocated_bytes": allocated_bytes,
    }
