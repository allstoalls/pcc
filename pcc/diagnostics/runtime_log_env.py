from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
import time


@dataclass(frozen=True)
class RuntimeLogEvent:
    event: str
    phase: str
    thread_id: int
    type_name: str = ""
    size: int = 0
    cause: str = ""
    timestamp_ns: int = 0

    def to_json(self) -> dict[str, object]:
        ts = self.timestamp_ns or time.time_ns()
        return {"event": self.event, "phase": self.phase, "thread_id": self.thread_id, "type": self.type_name, "size": self.size, "cause": self.cause, "timestamp_ns": ts}


def enabled_channels() -> set[str]:
    raw = os.environ.get("PCC_LOG", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def emit_runtime_event(event: RuntimeLogEvent, *, channel: str, stream=None) -> None:
    if channel not in enabled_channels():
        return
    if stream is None:
        stream = sys.stderr
    fmt = os.environ.get("PCC_LOG_FORMAT", "text").strip().lower()
    if fmt == "json":
        stream.write(json.dumps(event.to_json(), sort_keys=True) + "\n")
    else:
        stream.write(f"{event.phase}:{event.event}:type={event.type_name}:size={event.size}:cause={event.cause}\n")
