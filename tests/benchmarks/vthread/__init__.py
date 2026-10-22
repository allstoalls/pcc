"""tests/benchmarks/vthread — bounded virtual-thread scale MEASUREMENT harness
for the T-P0-VTHREAD-1M-GATE track (first slice).

This is a **measurement** harness over a self-contained *logical* scheduler
model, not a scheduler, not the runtime, and not a 1M-readiness claim. It mirrors
the operation shape of the three waitsets the real C runtime owns
(``pcc/runtime/src/pcc_threads.c``): a ready queue, a deadline-sorted timer
queue, and an IO waitset — and reports **deterministic logical counters** plus
two named *surrogate* counters.

Mode-mode taxonomy (see :class:`VThreadMode` / the mode constants in ``harness``):

* ``small-ci``     -> RUNS by default at a bounded ``N`` (default 10_000). All
  numbers are deterministic logical counts: no wall-clock, no RSS bytes, no
  latency.
* ``large-manual`` -> N = 1_000_000. Does NOT run in CI; gated OFF unless
  ``PCC_VTHREAD_1M=1``. Even when run it produces the same logical counters.
* ``real-runtime`` -> ALWAYS ``SKIPPED_WITH_REASON`` in this logical entrypoint.
  The separately gated ``scripts/run_vthread_1m_gate.py`` owns the production C
  runtime's live-machine RSS/latency/GC-pause result.

CLAIM BOUNDARY: measurement target only. ``peak_live_set`` is an RSS surrogate
(live-node COUNT, not bytes); ``gc_pause_count`` is a GC-pause surrogate
(threshold-crossing COUNT, not real pauses). This harness proves the logical
model's counters scale predictably with ``N`` and are deterministic. It does
NOT prove 1M-vthread readiness, real RSS, real latency, real GC pauses, or any
virtual-thread performance completion.

Design and full claim boundary: ``docs/design/pcc-vthread-1m-gate.md``.
"""
from __future__ import annotations

from .harness import (
    ALL_MODES,
    DEFAULT_CARRIER_COUNT,
    DEFAULT_SMALL_CI_N,
    LARGE_MANUAL_N,
    LARGE_MANUAL_ENV,
    MEASURABLE_MODES,
    MODE_SMALL_CI,
    MODE_LARGE_MANUAL,
    MODE_REAL_RUNTIME,
    STATUS_MEASURED,
    STATUS_SKIPPED,
    VThreadBenchError,
    VThreadBenchResult,
    VThreadBenchManifest,
    large_manual_enabled,
    large_manual_gated_off_reason,
    real_runtime_skip_reason,
    n_for_mode,
    measured,
    skipped,
    simulate_carrier_distribution,
    simulate_logical_scheduler,
    run_mode,
    run_all,
)

__all__ = [
    "ALL_MODES",
    "DEFAULT_CARRIER_COUNT",
    "DEFAULT_SMALL_CI_N",
    "LARGE_MANUAL_N",
    "LARGE_MANUAL_ENV",
    "MEASURABLE_MODES",
    "MODE_SMALL_CI",
    "MODE_LARGE_MANUAL",
    "MODE_REAL_RUNTIME",
    "STATUS_MEASURED",
    "STATUS_SKIPPED",
    "VThreadBenchError",
    "VThreadBenchResult",
    "VThreadBenchManifest",
    "large_manual_enabled",
    "large_manual_gated_off_reason",
    "real_runtime_skip_reason",
    "n_for_mode",
    "measured",
    "skipped",
    "simulate_carrier_distribution",
    "simulate_logical_scheduler",
    "run_mode",
    "run_all",
]
