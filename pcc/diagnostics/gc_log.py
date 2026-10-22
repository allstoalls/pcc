
"""GC/runtime log helpers for pcc roadmap §9.4.

The runtime side is intentionally small: logs may be emitted as JSON lines
or as compact text lines.  This module gives both humans and tests a stable
schema to parse, summarize, and validate without depending on a particular
collector implementation.

The canonical JSON-line event shape is::

    {"event": "gc_collect_stop", "thread_id": 1, "tracked": 10, ...}

Text lines use ``key=value`` pairs after the event name::

    gc_collect_stop thread_id=1 tracked=10 collected=3 pause_ms=0.4
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Iterable


_NUMERIC_SUFFIXES = ("_ms", "_ns", "_bytes", "_count")


@dataclass(frozen=True)
class RuntimeLogEvent:
    event: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        payload = {"event": self.event}
        payload.update(self.fields)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_mapping(mapping: dict[str, Any]) -> "RuntimeLogEvent":
        if "event" not in mapping:
            raise ValueError("runtime log event missing 'event'")
        event = str(mapping["event"])
        fields = {str(k): v for k, v in mapping.items() if k != "event"}
        return RuntimeLogEvent(event=event, fields=fields)


def _coerce_value(text: str) -> Any:
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("null", "None"):
        return None
    try:
        if "." not in text and "e" not in text.lower():
            return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_log_line(line: str) -> RuntimeLogEvent | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        return RuntimeLogEvent.from_mapping(json.loads(stripped))
    parts = stripped.split()
    event = parts[0]
    fields: dict[str, Any] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key] = _coerce_value(value)
    return RuntimeLogEvent(event=event, fields=fields)


def parse_log_lines(lines: Iterable[str]) -> list[RuntimeLogEvent]:
    out: list[RuntimeLogEvent] = []
    for line in lines:
        event = parse_log_line(line)
        if event is not None:
            out.append(event)
    return out


@dataclass
class GcLogSummary:
    collections: int = 0
    total_collected: int = 0
    total_pause_ms: float = 0.0
    max_pause_ms: float = 0.0
    allocations: int = 0
    allocated_bytes: int = 0
    weakref_callbacks: int = 0
    finalizers: int = 0
    events_by_name: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "collections": self.collections,
            "total_collected": self.total_collected,
            "total_pause_ms": self.total_pause_ms,
            "max_pause_ms": self.max_pause_ms,
            "allocations": self.allocations,
            "allocated_bytes": self.allocated_bytes,
            "weakref_callbacks": self.weakref_callbacks,
            "finalizers": self.finalizers,
            "events_by_name": dict(sorted(self.events_by_name.items())),
        }


def summarize(events: Iterable[RuntimeLogEvent]) -> GcLogSummary:
    summary = GcLogSummary()
    for ev in events:
        category = str(ev.fields.get("category", "") or "")
        event_key = ev.event if not category else category + "/" + ev.event
        summary.events_by_name[event_key] = summary.events_by_name.get(event_key, 0) + 1

        if ev.event == "gc_collect_stop" or (
            category == "gc" and ev.event == "collect_stop"
        ):
            summary.collections += 1
            summary.total_collected += int(
                ev.fields.get("collected", ev.fields.get("value0", 0)) or 0
            )
            pause = float(ev.fields.get("pause_ms", 0.0) or 0.0)
            summary.total_pause_ms += pause
            if pause > summary.max_pause_ms:
                summary.max_pause_ms = pause
        elif ev.event == "alloc" or (
            category == "alloc" and ev.event == "alloc_object"
        ):
            summary.allocations += 1
            summary.allocated_bytes += int(
                ev.fields.get(
                    "size",
                    ev.fields.get("bytes", ev.fields.get("value0", 0)),
                ) or 0
            )
        elif ev.event == "weakref_callback" or (
            category == "weakref" and ev.event == "callback"
        ):
            summary.weakref_callbacks += 1
        elif ev.event == "finalizer" or (
            category == "finalizer" and ev.event == "call"
        ):
            summary.finalizers += 1
    return summary


def validate_gc_event(ev: RuntimeLogEvent) -> list[str]:
    problems: list[str] = []
    if not ev.event:
        problems.append("event name is empty")
    for key, value in ev.fields.items():
        if key.endswith(_NUMERIC_SUFFIXES) or key in {
            "tracked", "visited", "collected", "uncollectable", "thread_id", "generation",
        }:
            if not isinstance(value, (int, float)):
                problems.append(f"{key} should be numeric")
    if ev.event == "gc_collect_stop":
        for required in ("tracked", "visited", "collected", "pause_ms"):
            if required not in ev.fields:
                problems.append(f"gc_collect_stop missing {required}")
    return problems
