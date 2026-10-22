from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class PassDecision:
    name: str
    ran: bool
    reason: str
    duration_ms: float = 0.0


def explain_passes(decisions: list[PassDecision]) -> dict[str, object]:
    return {
        "schema": "pcc.diagnostics.pass_explain.v1",
        "passes": [dict(d.__dict__) for d in decisions],
        "ran": [d.name for d in decisions if d.ran],
        "skipped": [d.name for d in decisions if not d.ran],
    }


def format_pass_explain(decisions: list[PassDecision], *, fmt: str = "text") -> str:
    data = explain_passes(decisions)
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=True)
    return "\n".join(
        f"{d.name}: {'ran' if d.ran else 'skipped'}: {d.reason}"
        for d in decisions
    )
