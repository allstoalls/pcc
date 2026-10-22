"""Runtime event logging controlled by PCC_LOG/PCC_LOG_FORMAT."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import sys
import time
from typing import Any, Iterable


@dataclass(frozen=True)
class RuntimeEvent:
    event: str
    phase: str
    thread_id: int = 0
    timestamp_ns: int = field(default_factory=time.time_ns)
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "phase": self.phase,
            "thread_id": self.thread_id,
            "timestamp_ns": self.timestamp_ns,
            **self.fields,
        }


def enabled_channels() -> set[str]:
    return {x.strip() for x in os.environ.get("PCC_LOG", "").split(",") if x.strip()}


def should_log(channel: str) -> bool:
    channels = enabled_channels()
    return "all" in channels or channel in channels


def format_event(event: RuntimeEvent, *, fmt: str | None = None) -> str:
    fmt = (fmt or os.environ.get("PCC_LOG_FORMAT", "text")).strip().lower()
    if fmt == "json":
        return json.dumps(event.to_json(), sort_keys=True)
    if fmt == "text":
        extra = " ".join(f"{k}={v}" for k, v in sorted(event.fields.items()))
        return f"{event.timestamp_ns} {event.thread_id} {event.phase} {event.event} {extra}".rstrip()
    raise ValueError(f"unknown PCC_LOG_FORMAT {fmt!r}")


def log_event(channel: str, event: RuntimeEvent, *, stream=None) -> None:
    if not should_log(channel):
        return
    if stream is None:
        stream = sys.stderr
    stream.write(format_event(event) + "\n")


def summarize_events(events: Iterable[RuntimeEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for event in events:
        key = f"{event.phase}:{event.event}"
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))
