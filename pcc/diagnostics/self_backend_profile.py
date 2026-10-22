from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelfBackendPhase:
    name: str
    functions: int = 0
    instructions: int = 0
    spills: int = 0
    duration_ms: float = 0.0


def summarize_self_backend(phases: list[SelfBackendPhase]) -> dict[str, object]:
    return {
        "schema": "pcc.diagnostics.self_backend_profile.v1",
        "total_ms": sum(p.duration_ms for p in phases),
        "total_spills": sum(p.spills for p in phases),
        "phases": [dict(p.__dict__) for p in phases],
    }
