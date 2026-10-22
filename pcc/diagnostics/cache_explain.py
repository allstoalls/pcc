from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class CacheInput:
    path: str
    sha256: str
    size: int

    @classmethod
    def from_path(cls, path: str) -> "CacheInput":
        p = Path(path)
        data = p.read_bytes()
        return cls(str(p), hashlib.sha256(data).hexdigest(), len(data))


@dataclass(frozen=True)
class CacheDecision:
    key: str
    hit: bool
    reason: str
    inputs: tuple[CacheInput, ...]

    def to_json(self) -> dict[str, object]:
        return {"key": self.key, "hit": self.hit, "reason": self.reason, "inputs": [i.__dict__ for i in self.inputs]}


def build_cache_key(inputs: list[CacheInput], *, flags: list[str]) -> str:
    h = hashlib.sha256()
    for item in sorted(inputs, key=lambda x: x.path):
        h.update(item.path.encode())
        h.update(item.sha256.encode())
    for flag in flags:
        h.update(flag.encode())
    return h.hexdigest()


def format_cache_decision(decision: CacheDecision, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps({"schema": "pcc.diagnostics.cache_explain.v1", "decision": decision.to_json()}, indent=2, sort_keys=True)
    return f"{decision.key}: {'hit' if decision.hit else 'miss'}: {decision.reason}"
