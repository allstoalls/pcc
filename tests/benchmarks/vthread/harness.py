"""T-P0-VTHREAD-1M-GATE first slice — bounded virtual-thread scale MEASUREMENT
harness over a self-contained *logical* scheduler model.

This module is a **measurement harness**, not a scheduler, not the runtime, and
not a 1M-readiness claim. It exercises a small, self-contained in-harness model
of the three virtual-thread scheduler waitsets that the real C runtime
(``pcc/runtime/src/pcc_threads.c``) owns:

    * a ready queue          (enqueue / dequeue)           <- ``pcc_vthread_enqueue_locked`` / ``pcc_vthread_dequeue_locked``
    * a deadline-sorted timer (insert / expire)            <- ``pcc_vthread_timer_add_locked`` / ``py_virtual_thread_poll_timers``
    * an IO waitset          (add wait / mark ready)       <- ``pcc_vthread_poll_add_locked`` / ``py_virtual_thread_poll_io``

For each ``N`` virtual threads it reports **deterministic logical counters**
only:

    * ``enqueue_ops`` / ``dequeue_ops``          -- ready-queue logical op counts
    * ``timer_insert_ops`` / ``timer_expire_ops`` -- timer logical op counts
    * ``io_wait_add_ops`` / ``io_ready_ops``      -- io-waitset logical op counts
    * ``peak_live_set``                            -- an RSS *surrogate*: the max
      number of simultaneously-live logical scheduler nodes (NOT bytes, NOT RSS)
    * ``gc_pause_count``                           -- a GC-pause-count *surrogate*:
      how many times a bounded live-set threshold was crossed (NOT a real GC
      pause, NOT a latency)
    * ``carrier_count``                            -- the number of logical
      carrier queues the ready work is round-robin distributed across (mirrors
      ``pcc_vthread_carrier_count`` / ``pcc_vthread_carrier_queues`` in
      ``pcc_threads.c``)
    * ``carrier_max_load`` / ``carrier_min_load``  -- the most- and least-loaded
      carrier's round-robin enqueue count (mirrors the ``pcc_vthread_next_carrier_enqueue
      % pcc_vthread_carrier_queue_count`` push distribution)
    * ``carrier_load_imbalance``                   -- ``carrier_max_load -
      carrier_min_load``; a work-STEALING-imbalance logical COUNT (NOT a latency,
      NOT bytes). For a perfect round-robin it is 0 when the carrier count divides
      ``N`` and 1 otherwise.
    * ``work_steal_ops``                           -- a work-stealing *surrogate*:
      the number of ready nodes that fall beyond the even per-carrier floor share
      and would be stolen by an idle carrier (mirrors the
      ``(own + offset) % n_queues`` steal loop that bumps
      ``pcc_vthread_carrier_steal_count``). Equals ``N % carrier_count``; 0 iff the
      carrier count divides ``N`` (perfectly balanced) or there is a single
      carrier (all work stays local). A COUNT, never a rate.

Mode taxonomy (see :class:`VThreadMode`):

    * ``small-ci``      -> RUNS by default at a bounded ``N`` (default 10_000).
      Everything measured is a deterministic logical count over the in-harness
      model. No wall-clock, no RSS bytes, no latency.
    * ``large-manual``  -> N = 1_000_000. Does NOT run in CI. Gated OFF unless
      the environment variable ``PCC_VTHREAD_1M=1`` is set. Even when it runs it
      still only produces the same *logical* counters, not real RSS/latency.
    * ``real-runtime``  -> ALWAYS ``SKIPPED_WITH_REASON`` *in this logical
      entrypoint*. The separately gated ``scripts/run_vthread_1m_gate.py`` owns
      the live production-C-runtime measurement. This harness cannot relabel
      its logical counts as that result.

CLAIM BOUNDARY (read before citing any number this harness emits):

    A ``small-ci`` / ``large-manual`` result is a mode-labeled set of
    DETERMINISTIC LOGICAL COUNTS over a self-contained scheduler model running
    in ONE host Python process. ``peak_live_set`` is a live-node COUNT used as
    an RSS surrogate and ``gc_pause_count`` is a threshold-crossing COUNT used
    as a GC-pause surrogate. Neither is bytes, neither is time. This harness
    proves that the LOGICAL scheduler model's counters scale predictably with
    ``N`` and are deterministic. It does NOT prove 1M-vthread readiness, real
    RSS, real enqueue/dequeue/timer/IO latency, real GC pause behavior, or any
    virtual-thread performance completion. Those require the ``real-runtime``
    mode, which is always skipped here with an exact missing-capability reason.

Standalone-importable: ``import tests.benchmarks.vthread.harness``.

Design and full claim boundary: ``docs/design/pcc-vthread-1m-gate.md``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

# --------------------------------------------------------------------------
# Mode taxonomy
# --------------------------------------------------------------------------
MODE_SMALL_CI = "small-ci"
MODE_LARGE_MANUAL = "large-manual"
MODE_REAL_RUNTIME = "real-runtime"

# The full ordered taxonomy the harness knows about.
ALL_MODES: tuple[str, ...] = (
    MODE_SMALL_CI,
    MODE_LARGE_MANUAL,
    MODE_REAL_RUNTIME,
)

# Bounded N for each measurable mode. small-ci runs by default; large-manual is
# gated OFF unless PCC_VTHREAD_1M=1 is set in the environment.
DEFAULT_SMALL_CI_N = 10_000
LARGE_MANUAL_N = 1_000_000

# Default logical carrier-queue count for the work-stealing dimension. Mirrors
# the shape of pcc_vthread_carrier_count in pcc_threads.c: the ready work is
# round-robin distributed across this many logical carrier queues, and any node
# beyond the even floor share is modelled as a work-steal surrogate. This is an
# arbitrary-but-fixed model default (like the 1/3 timer, 1/5 IO ratios); the
# point of the dimension is that the imbalance/steal counts are deterministic and
# track the round-robin distribution, not that the count matches any real host.
DEFAULT_CARRIER_COUNT = 4

# Modes that can produce logical-model measurements through this entry point.
# Production runtime measurements are delegated to
# scripts/run_vthread_1m_gate.py and are intentionally not mixed into this
# deterministic model manifest.
MEASURABLE_MODES: frozenset[str] = frozenset({MODE_SMALL_CI, MODE_LARGE_MANUAL})

# Environment gate that opts the large-manual mode in. Absent / not "1" -> the
# large-manual mode is SKIPPED_WITH_REASON, never silently run in CI.
LARGE_MANUAL_ENV = "PCC_VTHREAD_1M"

# Result status vocabulary. Kept as plain strings so a manifest round-trips
# through JSON without custom encoders (mirrors tests/benchmarks/dist).
STATUS_MEASURED = "MEASURED"
STATUS_SKIPPED = "SKIPPED_WITH_REASON"

_VALID_STATUS = frozenset({STATUS_MEASURED, STATUS_SKIPPED})

# A MEASURED result must not smuggle a timing / RSS-bytes / latency metric in —
# this harness emits logical counts and count-surrogates only. These are matched
# as SUBSTRINGS of a lowercased metric key, so composite keys like ``latency_ms``,
# ``rss_bytes``, or ``pause_ns`` are all rejected, not just the bare tokens.
_BANNED_METRIC_SUBSTRINGS: tuple[str, ...] = (
    "second", "latency", "throughput", "ops_per_sec",
    "wall_clock", "speedup", "bytes", "_mb", "gbps",
    "_ms", "_ns", "nanosec", "microsec", "millisec",
)


class VThreadBenchError(Exception):
    """Raised for an unknown mode, invalid status, or malformed manifest blob."""


# The exact missing-capability reason for real-runtime, and the gated-off reason
# for large-manual when its env gate is not set. These are mode-labeled
# sentences (never empty) — the taxonomy's whole point is that a skip says
# *what capability is missing / why it did not run*, not merely "not supported".
def real_runtime_skip_reason() -> str:
    return (
        "real-runtime is not owned by this logical host-Python entrypoint: this "
        "harness measures only the LOGICAL scheduler model and cannot produce "
        "RSS/latency/GC-pause numbers. Use the separately gated current-machine "
        "runner scripts/run_vthread_1m_gate.py, which links py_virtual_thread_* "
        "from pcc/runtime/src/pcc_threads.c and emits source-bound GC0..4 "
        "production-runtime metrics"
    )


def large_manual_gated_off_reason() -> str:
    return (
        f"large-manual (N={LARGE_MANUAL_N}) gated off: set {LARGE_MANUAL_ENV}=1 to "
        f"run the 1_000_000-vthread LOGICAL model manually. It is never run in "
        f"CI by default because even when run it produces only logical counts "
        f"(not real RSS/latency), and the run is intentionally reserved for a "
        f"deliberate manual invocation"
    )


def large_manual_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True iff the large-manual mode is opted in via the env gate."""
    src = os.environ if env is None else env
    return src.get(LARGE_MANUAL_ENV) == "1"


def n_for_mode(mode: str, small_ci_n: int = DEFAULT_SMALL_CI_N) -> int:
    """The bounded virtual-thread count for a measurable mode."""
    if mode == MODE_SMALL_CI:
        if small_ci_n <= 0:
            raise VThreadBenchError(f"small-ci N must be positive, got {small_ci_n}")
        return small_ci_n
    if mode == MODE_LARGE_MANUAL:
        return LARGE_MANUAL_N
    raise VThreadBenchError(
        f"mode {mode!r} has no measurable N; measurable modes: {sorted(MEASURABLE_MODES)}"
    )


# --------------------------------------------------------------------------
# The self-contained LOGICAL scheduler model
# --------------------------------------------------------------------------
# This is deliberately NOT the real runtime. It is a tiny in-harness model of
# the three waitsets the C scheduler owns, tracking only logical op counts and
# a live-node count. It opens no fds, spawns no threads, allocates no runtime
# objects, and reports no time.
@dataclass
class _LogicalScheduler:
    """A minimal, deterministic logical model of the vthread scheduler waitsets.

    Mirrors the operation *shape* of ``pcc_threads.c``:

        * ready queue  : enqueue -> dequeue (FIFO)
        * timer queue  : deadline-sorted insert -> expire when deadline <= now
        * io waitset   : add (fd, deadline) -> mark ready

    Every op increments a counter. ``live`` is the number of logical scheduler
    nodes currently outstanding (in any waitset or the ready queue); its running
    maximum is the ``peak_live_set`` RSS surrogate. ``gc_pause_count`` counts how
    many times ``live`` crossed a bounded threshold upward — a stand-in for how
    often a real collector might have been triggered by live-set pressure.
    """

    enqueue_ops: int = 0
    dequeue_ops: int = 0
    timer_insert_ops: int = 0
    timer_expire_ops: int = 0
    io_wait_add_ops: int = 0
    io_ready_ops: int = 0

    live: int = 0
    peak_live_set: int = 0
    gc_pause_count: int = 0

    # Bounded live-set threshold: each upward crossing is one pause surrogate.
    live_set_threshold: int = 2048

    # Work-stealing dimension: the number of logical carrier queues that ready
    # enqueues are round-robin distributed across (mirrors
    # pcc_vthread_carrier_queue_count). ``_carrier_loads`` holds the per-carrier
    # enqueue count; the next-carrier round-robin cursor mirrors
    # pcc_vthread_next_carrier_enqueue.
    carrier_count: int = DEFAULT_CARRIER_COUNT
    _carrier_loads: list[int] = field(default_factory=list)
    _next_carrier: int = 0

    def __post_init__(self) -> None:
        if self.carrier_count <= 0:
            raise VThreadBenchError(
                f"carrier_count must be positive, got {self.carrier_count}"
            )
        # One load counter per carrier queue, all starting empty.
        self._carrier_loads = [0] * self.carrier_count

    def _touch_live(self, delta: int) -> None:
        before = self.live
        self.live += delta
        if self.live > self.peak_live_set:
            self.peak_live_set = self.live
        # Count an upward crossing of the bounded threshold as a GC-pause surrogate.
        if delta > 0 and before < self.live_set_threshold <= self.live:
            self.gc_pause_count += 1

    def enqueue_ready(self) -> None:
        self.enqueue_ops += 1
        self._touch_live(+1)
        # Round-robin the ready node onto the next carrier queue, exactly as
        # pcc_vthread_push_ready_entry_locked does with
        # pcc_vthread_next_carrier_enqueue % pcc_vthread_carrier_queue_count.
        self._carrier_loads[self._next_carrier] += 1
        self._next_carrier = (self._next_carrier + 1) % self.carrier_count

    def dequeue_ready(self) -> None:
        self.dequeue_ops += 1
        self._touch_live(-1)

    def timer_insert(self) -> None:
        self.timer_insert_ops += 1
        self._touch_live(+1)

    def timer_expire(self) -> None:
        self.timer_expire_ops += 1
        # Expiry moves a node from timer -> ready (net live unchanged), then it
        # is dequeued below by the caller; model expiry as -1 here so the
        # subsequent dequeue accounting stays balanced.
        self._touch_live(-1)

    def io_wait_add(self) -> None:
        self.io_wait_add_ops += 1
        self._touch_live(+1)

    def io_ready(self) -> None:
        self.io_ready_ops += 1
        self._touch_live(-1)

    def as_metrics(self) -> dict[str, int]:
        # Carrier / work-stealing-imbalance dimension. Every ready enqueue was
        # round-robin routed to a carrier, so sum(_carrier_loads) == enqueue_ops.
        # For a perfect round-robin the imbalance (max - min) is 0 when the
        # carrier count divides enqueue_ops and 1 otherwise; the work-steal
        # surrogate is the surplus beyond the even floor share (enqueue_ops %
        # carrier_count) — the nodes an idle carrier would steal. Both are pure
        # COUNTS (no time, no bytes), mirroring pcc_vthread_carrier_steal_count.
        loads = self._carrier_loads
        carrier_max_load = max(loads) if loads else 0
        carrier_min_load = min(loads) if loads else 0
        floor_share = self.enqueue_ops // self.carrier_count
        work_steal_ops = self.enqueue_ops - floor_share * self.carrier_count
        return {
            "enqueue_ops": self.enqueue_ops,
            "dequeue_ops": self.dequeue_ops,
            "timer_insert_ops": self.timer_insert_ops,
            "timer_expire_ops": self.timer_expire_ops,
            "io_wait_add_ops": self.io_wait_add_ops,
            "io_ready_ops": self.io_ready_ops,
            "peak_live_set": self.peak_live_set,
            "gc_pause_count": self.gc_pause_count,
            "carrier_count": self.carrier_count,
            "carrier_max_load": carrier_max_load,
            "carrier_min_load": carrier_min_load,
            "carrier_load_imbalance": carrier_max_load - carrier_min_load,
            "work_steal_ops": work_steal_ops,
        }


def simulate_carrier_distribution(n: int, carrier_count: int) -> dict[str, int]:
    """Deterministic round-robin carrier distribution for ``n`` ready enqueues.

    Mirrors the shape of ``pcc_vthread_push_ready_entry_locked`` in
    ``pcc_threads.c``: the i-th ready node is pushed onto carrier
    ``i % carrier_count`` (the runtime uses ``pcc_vthread_next_carrier_enqueue %
    pcc_vthread_carrier_queue_count``). Returns only pure logical COUNTS:

        * ``carrier_count``          -- the number of carrier queues
        * ``carrier_max_load`` / ``carrier_min_load`` -- most/least loaded carrier
        * ``carrier_load_imbalance`` -- ``max - min`` (0 iff the count divides
          ``n``, else 1 for a perfect round-robin)
        * ``work_steal_ops``         -- surplus beyond the even floor share,
          ``n % carrier_count`` (the nodes an idle carrier would steal; 0 for a
          single carrier or a perfectly-divisible distribution), a work-STEALING
          surrogate that mirrors ``pcc_vthread_carrier_steal_count``.

    This is the same distribution the full :func:`simulate_logical_scheduler`
    applies to its ready-queue enqueues; it is exposed standalone so the carrier
    dimension can be asserted directly against its closed form.
    """
    if n <= 0:
        raise VThreadBenchError(f"virtual-thread count must be positive, got {n}")
    if carrier_count <= 0:
        raise VThreadBenchError(
            f"carrier_count must be positive, got {carrier_count}"
        )
    floor_share = n // carrier_count
    surplus = n - floor_share * carrier_count  # == n % carrier_count
    # The first ``surplus`` carriers receive floor_share + 1, the rest floor_share.
    carrier_max_load = floor_share + (1 if surplus else 0)
    carrier_min_load = floor_share  # surplus < carrier_count, so some carrier is exactly at the floor
    return {
        "carrier_count": carrier_count,
        "carrier_max_load": carrier_max_load,
        "carrier_min_load": carrier_min_load,
        "carrier_load_imbalance": carrier_max_load - carrier_min_load,
        "work_steal_ops": surplus,
    }


def simulate_logical_scheduler(
    n: int,
    *,
    live_set_threshold: int = 2048,
    carrier_count: int = DEFAULT_CARRIER_COUNT,
) -> dict[str, int]:
    """Run the deterministic logical scheduler model for ``n`` virtual threads.

    The workload is fully deterministic and mirrors a realistic mix: every
    virtual thread is enqueued-then-dequeued at least once; every third thread
    parks on a timer that later expires; every fifth thread blocks on an IO
    waitset that later becomes ready. The exact ratios are an arbitrary but
    fixed model choice — the point of this slice is that the counters scale
    *predictably* with ``n``, not that the ratios match any real workload.

    ``carrier_count`` selects how many logical carrier queues the ready enqueues
    are round-robin distributed across (see :func:`simulate_carrier_distribution`
    and ``DEFAULT_CARRIER_COUNT``); it adds the work-stealing-imbalance dimension
    (``carrier_max_load`` / ``carrier_min_load`` / ``carrier_load_imbalance`` /
    ``work_steal_ops``) to the returned metrics, all pure logical COUNTS.

    Returns a metrics dict of pure logical counts + the two surrogate counts +
    the carrier / work-steal dimension.
    """
    if n <= 0:
        raise VThreadBenchError(f"virtual-thread count must be positive, got {n}")

    sched = _LogicalScheduler(
        live_set_threshold=live_set_threshold,
        carrier_count=carrier_count,
    )

    for i in range(n):
        # Every vthread starts by being made ready and run once.
        sched.enqueue_ready()

        if i % 3 == 0:
            # Parks on a timer, which later expires and re-readies it.
            sched.timer_insert()
            sched.timer_expire()
        elif i % 5 == 0:
            # Blocks on an IO waitset, which later becomes ready.
            sched.io_wait_add()
            sched.io_ready()

        # The run loop dequeues the ready thread to execute it.
        sched.dequeue_ready()

    return sched.as_metrics()


# --------------------------------------------------------------------------
# Result + manifest data model (mirrors tests/benchmarks/dist)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class VThreadBenchResult:
    """The outcome of running one mode of the harness.

    ``status`` is :data:`STATUS_MEASURED` or :data:`STATUS_SKIPPED`. When
    measured, ``metrics`` holds mode-labeled *logical counts* + the two surrogate
    counts (never a wall-clock / RSS-bytes / latency number); ``reason`` is empty.
    When skipped, ``metrics`` is empty and ``reason`` is a non-empty, mode-labeled
    sentence naming the missing capability / gate.
    """

    mode: str
    status: str
    n: int = 0
    reason: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ALL_MODES:
            raise VThreadBenchError(
                f"unknown mode {self.mode!r}; known: {sorted(ALL_MODES)}"
            )
        if self.status not in _VALID_STATUS:
            raise VThreadBenchError(
                f"invalid status {self.status!r}; expected one of {sorted(_VALID_STATUS)}"
            )
        if self.status == STATUS_SKIPPED:
            if not self.reason.strip():
                raise VThreadBenchError(
                    f"mode {self.mode!r} is SKIPPED_WITH_REASON but reason is empty"
                )
            if self.metrics:
                raise VThreadBenchError(
                    f"skipped mode {self.mode!r} must carry no metrics, got {dict(self.metrics)}"
                )
        if self.status == STATUS_MEASURED:
            if self.reason:
                raise VThreadBenchError(
                    f"measured mode {self.mode!r} must not carry a skip reason, got {self.reason!r}"
                )
            if self.n <= 0:
                raise VThreadBenchError(
                    f"measured mode {self.mode!r} must carry a positive N, got {self.n}"
                )
            # Guard the claim boundary: no timing / RSS-bytes / latency metric may
            # be smuggled into a MEASURED result. Only logical counts + the two
            # named surrogate counts are allowed. Substring match so composite
            # keys (``latency_ms``, ``rss_bytes``, ``pause_ns``) are also rejected.
            leaked = sorted(
                str(k)
                for k in self.metrics
                if any(bad in str(k).lower() for bad in _BANNED_METRIC_SUBSTRINGS)
            )
            if leaked:
                raise VThreadBenchError(
                    f"measured mode {self.mode!r} leaks a timing/RSS/latency metric "
                    f"{leaked}; this harness emits logical counts only"
                )

    @property
    def measured(self) -> bool:
        return self.status == STATUS_MEASURED

    @property
    def skipped(self) -> bool:
        return self.status == STATUS_SKIPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "n": self.n,
            "reason": self.reason,
            "metrics": dict(self.metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VThreadBenchResult":
        return cls(
            mode=str(data["mode"]),
            status=str(data["status"]),
            n=int(data.get("n", 0)),
            reason=str(data.get("reason", "")),
            metrics=dict(data.get("metrics", {})),
        )


def measured(mode: str, n: int, metrics: Mapping[str, Any]) -> VThreadBenchResult:
    return VThreadBenchResult(mode, STATUS_MEASURED, n=n, metrics=dict(metrics))


def skipped(mode: str, reason: str) -> VThreadBenchResult:
    return VThreadBenchResult(mode, STATUS_SKIPPED, reason=reason)


# --------------------------------------------------------------------------
# Per-mode runners
# --------------------------------------------------------------------------
def run_mode(
    mode: str,
    *,
    small_ci_n: int = DEFAULT_SMALL_CI_N,
    carrier_count: int = DEFAULT_CARRIER_COUNT,
    env: Mapping[str, str] | None = None,
) -> VThreadBenchResult:
    """Run one mode of the harness and return its :class:`VThreadBenchResult`.

    * ``small-ci``     -> MEASURED at ``small_ci_n``.
    * ``large-manual`` -> MEASURED at N=1_000_000 iff the env gate is set,
      otherwise SKIPPED_WITH_REASON (gated off).
    * ``real-runtime`` -> always SKIPPED_WITH_REASON.

    ``carrier_count`` selects the number of logical carrier queues for the
    work-stealing-imbalance dimension (see :func:`simulate_logical_scheduler`).
    """
    if mode == MODE_SMALL_CI:
        n = n_for_mode(MODE_SMALL_CI, small_ci_n=small_ci_n)
        return measured(
            mode, n, simulate_logical_scheduler(n, carrier_count=carrier_count)
        )

    if mode == MODE_LARGE_MANUAL:
        if not large_manual_enabled(env):
            return skipped(mode, large_manual_gated_off_reason())
        n = n_for_mode(MODE_LARGE_MANUAL)
        return measured(
            mode, n, simulate_logical_scheduler(n, carrier_count=carrier_count)
        )

    if mode == MODE_REAL_RUNTIME:
        return skipped(mode, real_runtime_skip_reason())

    raise VThreadBenchError(f"unknown mode {mode!r}; known: {sorted(ALL_MODES)}")


_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class VThreadBenchManifest:
    """A full harness run: the mode taxonomy plus one result per mode.

    Round-trips stably through JSON (``to_json`` / ``from_json``). The manifest
    is the auditable artifact: a reader can see, for every mode, whether it was
    MEASURED (with logical counts) or SKIPPED_WITH_REASON (with the exact
    missing capability / gate reason).
    """

    harness: str
    results: tuple[VThreadBenchResult, ...]
    version: int = _MANIFEST_VERSION
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.harness:
            raise VThreadBenchError("manifest harness name must be non-empty")
        seen = [r.mode for r in self.results]
        if len(seen) != len(set(seen)):
            raise VThreadBenchError(f"manifest has duplicate mode results: {seen}")

    def result_for(self, mode: str) -> VThreadBenchResult:
        for r in self.results:
            if r.mode == mode:
                return r
        raise VThreadBenchError(f"no result for mode {mode!r} in manifest")

    def measured_modes(self) -> tuple[str, ...]:
        return tuple(r.mode for r in self.results if r.measured)

    def skipped_modes(self) -> tuple[str, ...]:
        return tuple(r.mode for r in self.results if r.skipped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "harness": self.harness,
            "detail": dict(self.detail),
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VThreadBenchManifest":
        version = int(data.get("version", _MANIFEST_VERSION))
        if version != _MANIFEST_VERSION:
            raise VThreadBenchError(
                f"unsupported manifest version {version!r}; expected {_MANIFEST_VERSION}"
            )
        results = tuple(VThreadBenchResult.from_dict(r) for r in data.get("results", []))
        return cls(
            harness=str(data["harness"]),
            results=results,
            version=version,
            detail=dict(data.get("detail", {})),
        )

    @classmethod
    def from_json(cls, blob: str) -> "VThreadBenchManifest":
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise VThreadBenchError(f"manifest blob is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


def run_all(
    *,
    small_ci_n: int = DEFAULT_SMALL_CI_N,
    carrier_count: int = DEFAULT_CARRIER_COUNT,
    env: Mapping[str, str] | None = None,
) -> VThreadBenchManifest:
    """Run every mode in the taxonomy and return an auditable manifest.

    ``small-ci`` measures, ``large-manual`` measures only under the env gate
    (else skips-with-reason), ``real-runtime`` always skips-with-reason.

    ``carrier_count`` selects the number of logical carrier queues for the
    work-stealing-imbalance dimension of the measured modes.
    """
    results = tuple(
        run_mode(mode, small_ci_n=small_ci_n, carrier_count=carrier_count, env=env)
        for mode in ALL_MODES
    )
    return VThreadBenchManifest(
        harness="vthread-1m-gate",
        results=results,
        detail={
            "small_ci_n": small_ci_n,
            "large_manual_n": LARGE_MANUAL_N,
            "large_manual_env": LARGE_MANUAL_ENV,
            "carrier_count": carrier_count,
            "note": (
                "logical scheduler-model counters only; peak_live_set is an RSS "
                "surrogate (node count, not bytes); gc_pause_count is a GC-pause "
                "surrogate (threshold crossings, not real pauses); carrier_count / "
                "carrier_load_imbalance / work_steal_ops are work-stealing logical "
                "COUNTS over a round-robin carrier distribution (not real steals, "
                "not latency); this logical entry point delegates production "
                "runtime measurement to scripts/run_vthread_1m_gate.py"
            ),
        },
    )
