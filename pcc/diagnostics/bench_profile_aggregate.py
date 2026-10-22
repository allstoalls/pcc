from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class BenchSample:
    name: str
    ms: float


def load_profile_total_ms(path: str) -> BenchSample:
    data = json.loads(Path(path).read_text())
    return BenchSample(str(data.get("metadata", {}).get("scenario", Path(path).stem)), float(data.get("total_ms", 0.0)))


def summarize_profiles(paths: list[str]) -> dict[str, object]:
    samples = [load_profile_total_ms(p) for p in paths]
    ordered = sorted(samples, key=lambda s: s.ms, reverse=True)
    return {"schema": "pcc.diagnostics.bench_profile.summary.v1", "samples": [s.__dict__ for s in ordered], "slowest": ordered[0].__dict__ if ordered else None}
