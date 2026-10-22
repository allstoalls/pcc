"""Executable capability table for pcc's five GC backends."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GcBackendCapability:
    backend_id: int
    name: str
    production_default: bool
    concurrent: bool
    moving: bool
    uses_write_barrier: bool
    uses_read_barrier: bool
    uses_safepoints: bool
    notes: str


BACKENDS: tuple[GcBackendCapability, ...] = (
    GcBackendCapability(0, "refcount-cycle", True, False, False, False, False, True,
                        "Default backend; refcount plus STW cycle collection."),
    GcBackendCapability(1, "incremental-tricolor", False, False, False, True, False, True,
                        "Incremental tracing state machine with bounded steps."),
    GcBackendCapability(2, "concurrent-mark-sweep", False, True, False, True, False, True,
                        "Uses tracing semantics; background worker is the production target."),
    GcBackendCapability(3, "generational-minor-major", False, False, False, True, False, True,
                        "Young/old flags and remembered-set path; minor heap remains explicit work."),
    GcBackendCapability(4, "colored-relocating", False, False, True, True, True, True,
                        "Forwarding-table/read-barrier backend target; multi-mapping stays out of scope."),
)


def all_backends() -> tuple[GcBackendCapability, ...]:
    return BACKENDS


def by_id(backend_id: int) -> GcBackendCapability:
    for backend in BACKENDS:
        if backend.backend_id == backend_id:
            return backend
    raise KeyError(backend_id)


def production_backends() -> tuple[GcBackendCapability, ...]:
    return tuple(b for b in BACKENDS if b.production_default)


def validate_capabilities() -> None:
    ids = [b.backend_id for b in BACKENDS]
    if ids != [0, 1, 2, 3, 4]:
        raise AssertionError(f"expected backend ids 0..4, got {ids!r}")
    if len(production_backends()) != 1 or production_backends()[0].backend_id != 0:
        raise AssertionError("only backend 0 may be production/default today")
    if not by_id(4).uses_read_barrier:
        raise AssertionError("colored relocating backend must advertise read barrier")
    if not by_id(2).concurrent:
        raise AssertionError("CMS backend must advertise concurrent target")
