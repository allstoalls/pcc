
"""Pass-manager profile and explanation helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class PassEvent:
    name: str
    duration_ms: float
    changed: bool
    skipped: bool = False
    skip_reason: str = ""
    invalidates: tuple[str, ...] = ()


@dataclass
class PassProfile:
    events: list[PassEvent] = field(default_factory=list)

    def add(self, event: PassEvent) -> None:
        self.events.append(event)

    def total_ms(self) -> float:
        return sum(ev.duration_ms for ev in self.events)

    def slowest(self, n: int = 10) -> list[PassEvent]:
        return sorted(self.events, key=lambda ev: ev.duration_ms, reverse=True)[:n]

    def explain(self) -> list[str]:
        lines: list[str] = []
        for ev in self.events:
            if ev.skipped:
                lines.append(f"{ev.name}: skipped ({ev.skip_reason or 'no reason'})")
            else:
                marker = "changed" if ev.changed else "unchanged"
                lines.append(f"{ev.name}: {ev.duration_ms:.3f}ms {marker}")
        return lines

    def to_json(self) -> dict:
        return {
            "total_ms": self.total_ms(),
            "passes": [
                {
                    "name": ev.name,
                    "duration_ms": ev.duration_ms,
                    "changed": ev.changed,
                    "skipped": ev.skipped,
                    "skip_reason": ev.skip_reason,
                    "invalidates": list(ev.invalidates),
                }
                for ev in self.events
            ],
        }
