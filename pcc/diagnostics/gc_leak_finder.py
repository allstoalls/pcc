from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class LeakFinding:
    kind: str
    detail: str
    severity: str


def analyze_gc_events(events: list[dict[str, object]]) -> list[LeakFinding]:
    findings: list[LeakFinding] = []
    alloc_by_type: dict[str, int] = {}
    freed_by_type: dict[str, int] = {}
    for ev in events:
        typ = str(ev.get("type") or ev.get("type_name") or "unknown")
        if ev.get("event") in ("alloc", "gc_alloc"):
            alloc_by_type[typ] = alloc_by_type.get(typ, 0) + int(ev.get("size", 1) or 1)
        if ev.get("event") in ("free", "gc_free"):
            freed_by_type[typ] = freed_by_type.get(typ, 0) + int(ev.get("size", 1) or 1)
        if ev.get("event") == "finalizer_exception":
            findings.append(LeakFinding("finalizer_exception", str(ev.get("cause", "")), "error"))
        if ev.get("event") == "weakref_order_violation":
            findings.append(LeakFinding("weakref_order", str(ev.get("target", "")), "error"))
    for typ, alloc in alloc_by_type.items():
        freed = freed_by_type.get(typ, 0)
        if alloc > freed * 2 and alloc - freed > 1024:
            findings.append(LeakFinding("allocation_growth", f"{typ}: alloc={alloc} freed={freed}", "warning"))
    return findings


def analyze_gc_log_text(text: str) -> list[LeakFinding]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return analyze_gc_events(events)
