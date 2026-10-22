from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class CacheKey:
    digest: str
    inputs: tuple[str, ...]
    flags: tuple[str, ...]
    version: str = "pcc.c.cache.v1"


def compute_cache_key(paths: list[str], *, flags: list[str] | None = None) -> CacheKey:
    h = hashlib.sha256()
    inputs = tuple(sorted(str(Path(p)) for p in paths))
    for path in inputs:
        h.update(path.encode() + b"\0")
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    flag_tuple = tuple(flags or ())
    for flag in flag_tuple:
        h.update(flag.encode() + b"\0")
    return CacheKey(h.hexdigest(), inputs, flag_tuple)


def explain_cache_miss(old: CacheKey, new: CacheKey) -> list[str]:
    out: list[str] = []
    if old.digest != new.digest:
        out.append("content digest changed")
    if old.inputs != new.inputs:
        out.append("input path set changed")
    if old.flags != new.flags:
        out.append("compile flags changed")
    return out
