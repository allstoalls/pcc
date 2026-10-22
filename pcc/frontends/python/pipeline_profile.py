"""Phase-level profiling helpers for the Python frontend pipeline.

This is the real profiling substrate used by CLI wrappers: it records the
phase names required by the roadmap without forcing a flag-day rewrite of
``pipeline.py``. Callers can wrap individual phase callables or the whole
``compile_python`` entry while the pipeline is gradually instrumented inside.
"""
from __future__ import annotations

import gc
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pcc.diagnostics.profile_events import ProfileRecorder

ROADMAP_PHASES = (
    "parse",
    "type_infer",
    "source_closure",
    "ir_generation",
    "ir_verify",
    "optimization_passes",
    "self_backend_lower",
    "register_allocation",
    "assembly_emit",
    "object_emit",
    "link",
    "runtime_archive",
    "stdlib_resolution",
    "subprocess",
)


@dataclass(frozen=True)
class PhaseResult:
    name: str
    value: Any


def run_profiled_phase(
    recorder: ProfileRecorder,
    name: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> PhaseResult:
    if name not in ROADMAP_PHASES:
        recorder.increment("unknown_phase")
    with recorder.phase(name, metadata={"argc": len(args), "kwargs": sorted(kwargs)}):
        value = fn(*args, **kwargs)
    return PhaseResult(name=name, value=value)


def seed_expected_phase_counters(recorder: ProfileRecorder) -> None:
    for name in ROADMAP_PHASES:
        recorder.increment("phase.expected." + name, 0)


def profile_now_ms() -> int:
    """Return the monotonic clock value used by dictionary profiles."""

    return int(time.monotonic() * 1000.0)


def profile_events(profile):
    """Return the mutable event list, creating it on first use."""

    if profile is None:
        return None
    events = profile.get("events")
    if events is None:
        events = []
        profile["events"] = events
    return events


def profile_totals(profile):
    """Return the mutable phase-total map, creating it on first use."""

    if profile is None:
        return None
    totals = profile.get("phase_totals_ms")
    if totals is None:
        totals = {}
        profile["phase_totals_ms"] = totals
    return totals


def profile_counters(profile):
    """Return the mutable counter map, creating it on first use."""

    if profile is None:
        return None
    counters = profile.get("counters")
    if counters is None:
        counters = {}
        profile["counters"] = counters
    return counters


def profile_begin(profile) -> int:
    if profile is None:
        return 0
    return profile_now_ms()


def profile_end(
    profile,
    name: str,
    start_ms: int,
    detail: Optional[str] = None,
) -> None:
    if profile is None:
        return
    elapsed = profile_now_ms() - start_ms
    events = profile_events(profile)
    if events is not None:
        event = {"name": name, "ms": elapsed}
        if detail is not None:
            event["detail"] = detail
        events.append(event)
    totals = profile_totals(profile)
    if totals is not None:
        totals[name] = totals.get(name, 0) + elapsed


def profile_counter(profile, name: str, value) -> None:
    counters = profile_counters(profile)
    if counters is not None:
        counters[name] = value


def profiled_gc_collect(
    profile,
    name: str,
    *,
    allocations_owned_by_current_process: bool = True,
) -> int:
    """Collect local garbage and record the process-ownership decision."""

    started = profile_begin(profile)
    collected = 0
    if allocations_owned_by_current_process:
        collected = int(gc.collect())
    profile_end(profile, name, started)
    profile_counter(profile, name + "_objects", collected)
    profile_counter(
        profile,
        name + "_skipped",
        0 if allocations_owned_by_current_process else 1,
    )
    return collected
