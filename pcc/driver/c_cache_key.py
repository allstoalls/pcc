
"""Content-hash cache keys for pcc C/project compilation.

This supports roadmap C-4: cache keys should be explainable and content-based,
not only mtime/size-based.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CacheInput:
    path: str
    digest: str
    size: int

    @staticmethod
    def from_file(path: str | Path) -> "CacheInput":
        p = Path(path)
        data = p.read_bytes()
        return CacheInput(
            path=str(p),
            digest=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )


@dataclass(frozen=True)
class CompileCacheKey:
    compiler_version: str
    target_triple: str
    opt_level: int
    cpp_args: tuple[str, ...] = ()
    link_args: tuple[str, ...] = ()
    inputs: tuple[CacheInput, ...] = ()

    def material(self) -> dict:
        return {
            "compiler_version": self.compiler_version,
            "target_triple": self.target_triple,
            "opt_level": self.opt_level,
            "cpp_args": list(self.cpp_args),
            "link_args": list(self.link_args),
            "inputs": [inp.__dict__ for inp in self.inputs],
        }

    def digest(self) -> str:
        payload = json.dumps(self.material(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    def explain_miss(self, other: "CompileCacheKey") -> list[str]:
        reasons: list[str] = []
        if self.compiler_version != other.compiler_version:
            reasons.append("compiler_version")
        if self.target_triple != other.target_triple:
            reasons.append("target_triple")
        if self.opt_level != other.opt_level:
            reasons.append("opt_level")
        if self.cpp_args != other.cpp_args:
            reasons.append("cpp_args")
        if self.link_args != other.link_args:
            reasons.append("link_args")
        lhs_inputs = {i.path: i for i in self.inputs}
        rhs_inputs = {i.path: i for i in other.inputs}
        if set(lhs_inputs) != set(rhs_inputs):
            reasons.append("input_set")
        else:
            for path in sorted(lhs_inputs):
                if lhs_inputs[path].digest != rhs_inputs[path].digest:
                    reasons.append(f"input_changed:{path}")
                elif lhs_inputs[path].size != rhs_inputs[path].size:
                    reasons.append(f"input_size:{path}")
        return reasons


def build_cache_key(
    *,
    compiler_version: str,
    target_triple: str,
    opt_level: int,
    source_paths: Iterable[str | Path],
    cpp_args: Iterable[str] = (),
    link_args: Iterable[str] = (),
) -> CompileCacheKey:
    return CompileCacheKey(
        compiler_version=compiler_version,
        target_triple=target_triple,
        opt_level=int(opt_level),
        cpp_args=tuple(str(a) for a in cpp_args),
        link_args=tuple(str(a) for a in link_args),
        inputs=tuple(CacheInput.from_file(p) for p in source_paths),
    )
