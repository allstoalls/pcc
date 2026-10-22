"""Refcount strategy metadata for build/test planning."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefcountStrategy:
    kind: int
    name: str
    requires_threads: bool
    header_layout: str
    production_target: bool
    notes: str


STRATEGIES: tuple[RefcountStrategy, ...] = (
    RefcountStrategy(0, "NONATOMIC", False, "classic-refcount", True,
                     "Default single-threaded strategy."),
    RefcountStrategy(1, "ATOMIC", True, "classic-refcount", True,
                     "Atomic fetch-add/sub path for threaded builds."),
    RefcountStrategy(2, "BIASED", True, "pep703-biased-header", True,
                     "Production target uses ob_tid/local/shared fields."),
    RefcountStrategy(3, "DEFERRED", True, "deferred-queue", True,
                     "Production target uses queue + flush protocol."),
)


def all_strategies() -> tuple[RefcountStrategy, ...]:
    return STRATEGIES


def by_kind(kind: int) -> RefcountStrategy:
    for strategy in STRATEGIES:
        if strategy.kind == kind:
            return strategy
    raise KeyError(kind)


def make_env(kind: int, *, with_threads: bool = True) -> dict[str, str]:
    strategy = by_kind(kind)
    if strategy.requires_threads and not with_threads:
        raise ValueError(f"{strategy.name} requires PCC_WITH_THREADS=1")
    return {
        "PCC_WITH_THREADS": "1" if with_threads else "0",
        "PCC_REFCOUNT_KIND": str(kind),
    }


def validate_matrix() -> None:
    kinds = [s.kind for s in STRATEGIES]
    if kinds != [0, 1, 2, 3]:
        raise AssertionError(f"expected refcount kinds 0..3, got {kinds!r}")
    if by_kind(2).header_layout != "pep703-biased-header":
        raise AssertionError("BIASED must target the PEP 703 header layout")
    if by_kind(3).header_layout != "deferred-queue":
        raise AssertionError("DEFERRED must target queue/flush semantics")
