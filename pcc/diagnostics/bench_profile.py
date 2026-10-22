from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class BenchResult:
    name: str
    command: tuple[str, ...]
    wall_ms: float
    returncode: int


def format_bench_json(results: list[BenchResult]) -> str:
    return json.dumps({
        "schema": "pcc.bench.v1",
        "results": [dict(r.__dict__) for r in results],
        "total_wall_ms": sum(r.wall_ms for r in results),
    }, indent=2, sort_keys=True)
