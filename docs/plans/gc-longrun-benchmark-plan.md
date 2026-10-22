# GC long-running benchmark plan (G-P3-LONGRUN)

North-star obligation 6's second half: measure pause / RSS /
throughput / fragmentation **over time** for all five GC backends.
External audit 2026-06-12 confirmed this is the only north-star clause
with no evidence accrued in its own terms (compile/bootstrap walls and
single-shot BoC throughput runs do not qualify). This plan is the
design-first deliverable for the `G-P3-LONGRUN` board row; no
implementation slice may start before its harness shape is agreed.

## Measurement-surface inventory (2026-06-12 survey)

| Metric | Raw material today | Gap |
|---|---|---|
| Fragmentation | EXISTS for backend 4: `pcc_gc_backend4_zpage_population` metrics (capacity-used bytes, allocated-used bytes, sparse-page counts) behind `pcc_gc_telemetry(metric)`. | Backends 0-3 have no equivalent surface; needs a per-backend definition (e.g. free-list holes for #0, heap-block occupancy for #1/#2, page stats for #3). |
| Pause | NONE — collect/step paths have no wall-clock instrumentation. | Add monotonic timing around stop-the-world sections / incremental steps, exported as count+sum+max (and a small fixed histogram) via new telemetry metrics. Must be near-zero-cost when telemetry is off. |
| RSS over time | NONE. | Needs an OS bridge (`getrusage(RUSAGE_SELF).ru_maxrss` + current RSS via `mach_task_basic_info` on Darwin / `/proc/self/statm` on Linux) exposed as a runtime helper the workload can poll. |
| Throughput | Workload-reported (ops/sec printed by the benchmark itself). | None — convention only. |

## Harness shape

- Workloads are **pcc-compiled no-libpython binaries** (strict mode,
  `--backend self`), one source file per workload under
  `benchmarks/python/longrun_*.py`, runnable under any
  `PCC_GC_BACKEND=0..4`.
- Workload set (steady-state, minutes-scale, deterministic seeds):
  1. `longrun_churn` — high allocation/death rate of small mixed
     objects (lists/dicts/strs/instances), stable live-set.
  2. `longrun_growshrink` — sawtooth live-set (grow N, drop half,
     repeat) to exercise fragmentation and return-to-OS behavior.
  3. `longrun_pointer_mutator` — heavy inter-object pointer mutation
     over a stable graph (write-barrier and remembered-set pressure).
  4. `longrun_finalizers` — finalizer/weakref churn (semantic-pressure
     long run; doubles as a leak canary via `__del__` counting).
- In-process sampling loop: every K iterations the workload polls the
  telemetry/RSS helpers and appends a CSV line
  (`elapsed_ms, rss_kb, pauses_n, pause_sum_us, pause_max_us, frag_bytes, ops`);
  output goes to stdout or a file given by argv. No host-Python
  involvement at runtime.
- Gates: a `tests/python/longrun/` smoke tier runs each workload for a
  SHORT bounded window (seconds) on all five backends asserting (a)
  clean exit, (b) monotonic telemetry sanity, (c) leak canary == 0 —
  CI-friendly. The minutes-scale runs are a manual/profiled tier
  invoked by a script (`scripts/gc_longrun.sh`), never by default
  pytest (AGENTS timeout discipline).
- Claim shape: per-backend time-series artifacts; comparisons are
  same-host same-day; NO "collector X is better" claims from single
  workloads — report per-workload profiles (pause-sensitive vs
  throughput-sensitive vs footprint-sensitive) per the five-GC
  research framing.

## Sequencing (each its own slice with the usual gates)

1. Pause telemetry in the C kernel (timing + counters + histogram,
   off-by-default flag; both-tier mirror NOT needed if kept C-only
   behind the existing telemetry entry — decide at implementation).
2. RSS helper (C-only, Darwin first; Linux variant lands with
   S-P2-LINUX).
3. `longrun_churn` workload + smoke gate on backends 0..4.
4. Remaining workloads; fragmentation definitions for backends 0-3.
5. First measurement report (docs/reports/), feeding obligation-6
   evidence.

## Non-goals

No collector ranking claims, no default-pytest minutes-scale runs, no
weakening of finalizer/weakref/barrier semantics for better numbers
(5-GC Production Equality Rule), no Linux numbers before S-P2-LINUX
provides a gated host.

## Slice 1 progress (2026-06-12)

Pause telemetry LANDED (C kernel): `pcc_gc_record_pause` now records
count + sum + a 4-bucket histogram (<100us / <1ms / <10ms / >=10ms)
alongside the existing max, exported as
`PCC_GC_COUNTER_PAUSE_{COUNT,SUM_US,HIST_*}` (metrics 32-37) with
reset support; endpoint-only atomics, zero hot-path cost, always-on.
Gate: `tests/python/test_gc_pause_telemetry.py` — C harness drives an
alloc/collect workload per tracing backend (1-4) and asserts
count>0, sum>=max, hist-sum==count, reset-zeroes (4 passed).
KNOWN LIMIT recorded: the only `pcc_gc_record_pause` call site today
is the tracing-step path — backend 0's cycle collect is NOT yet timed
(its refcount steady-state has no STW pauses; the explicit cycle
collect should get an endpoint in the next slice), so backend-0
columns stay empty until then. Matrix/battery result in goal-state.

## Slice 2 progress (2026-06-12)

RSS bridge LANDED: `pcc/runtime/src/py_os_rss.c` (C-only,
OBJ_PY_CC_HELPERS) exports `pcc_os_current_rss_bytes` (Darwin:
mach_task_basic_info.resident_size; Linux: /proc/self/statm resident
pages x page size — UNTESTED until S-P2-LINUX) and
`pcc_os_peak_rss_bytes` (getrusage ru_maxrss; bytes on macOS, KB->bytes
on Linux), -1 on failure; runtime-ABI registered for pcc-compiled
workloads. Gate: `tests/python/test_os_rss_helper.py` — C harness
asserts >1MB live RSS, peak>=current, and growth visibility across a
touched 32MB burst (1 passed; assertions deliberately loose — the OS
owns the numbers). Matrix/battery result in goal-state. Next: slice 3
(`longrun_churn` workload + bounded smoke gate on backends 0..4),
plus the backend-0 cycle-collect pause endpoint from slice 1's limit.

## Slice 3 progress (2026-06-12)

`longrun_churn` workload + bounded smoke tier LANDED:
`benchmarks/python/longrun_churn.py` (strict no-libpython
self-backend; fixed 2048-object live ring with continuous
replacement; samples every 200 rounds via user-program `pcc.extern`
access to `pcc_gc_telemetry`/RSS/monotonic helpers — verified
working — printing the planned CSV line; argv-bounded). Smoke gate
`tests/python/test_longrun_smoke.py`: one compile, short window on
ALL five backends, asserting clean exit / well-formed 7-field CSV /
non-negative telemetry / live RSS / monotonic ops / no corruption
sentinel — 5 passed (measurement-surface checks only, no collector
assertions). First REAL SIGNAL already visible in a gc3 probe: with
collect debt not yet reached (pause_n stayed 0), RSS grew 4MB->34MB
over 600 rounds despite the constant live set — exactly the class of
behavior the minutes-scale tier is built to characterize (NOT a claim;
short-window observation). Combined gate session (contract + all three
G-P3 gates) 140 passed; no compiled-path change in this slice so the
five-GC matrix was not rerun (the pause/RSS slices each carried their
own). Remaining: backend-0 cycle-collect pause endpoint, workloads
2-4, fragmentation definitions for backends 0-3, scripts/gc_longrun.sh
manual tier, first measurement report.

## Backend-0 pause endpoint (2026-06-12, closes slice 1's known limit)

`pcc_gc_collect`'s backend-0 branch (both tiers) now times the
explicit cycle collect via the exported
`pcc_gc_record_explicit_pause` wrapper (the static record helper made
public for callers outside py_gc_backend.c; the port uses the
`pcc_runtime_monotonic_us` extern). The pause-telemetry gate is
extended to ALL FIVE backends (5 passed). Backend 0's refcount
steady-state remains pause-free by design — only the explicit cycle
collect registers. Matrix result in goal-state.

## Correction (2026-06-12): slice 1 was missing its PORT mirror — caught by the contract suite

The backend-0 endpoint's new `pcc_gc_record_explicit_pause` extern in
the py_obj.py port exposed that slice 1 had only landed on the C tier:
default-mode links the PORT py_gc_backend (PY_MODULES), which had no
such symbol -> 120 contract errors (`ld: Undefined symbols:
_pcc_gc_record_explicit_pause`). Mirrored now: substrate i32 globals
(pause_count/sum/hist0-3 — i32 like the existing max_pause global;
sum_us can saturate after ~35min accumulated pauses, recorded limit,
C tier is i64), py_gc_telemetry.py metric 32-37 dispatch,
py_gc_backend.py `_record_pause` count/sum/hist mirror + exported
`pcc_gc_record_explicit_pause` + reset zeroing. LESSON: a C-side
telemetry slice on a PY_MODULES-mirrored subsystem is incomplete
without the port mirror even when the C-harness gate passes — the
gate linked the C archive, hiding the gap until a port-side caller
appeared. Re-verified: the previously-erroring contract case + all
five pause gates -> 6 passed; full contract + matrix rerun recorded
in goal-state.

## Workload 2 + manual tier (2026-06-12)

`longrun_growshrink` landed (sawtooth live-set: grow to 4096 Blobs,
drop the even half, repeat; same CSV contract) — gc4 quick run shows
the expected RSS ramp under unreclaimed sawtooth at smoke scale. The
smoke gate is parametrized to BOTH workloads x all five backends
(10 passed). `scripts/gc_longrun.sh` provides the manual minutes-scale
tier (per-(workload,backend) CSV series under an output dir; never
default pytest). Remaining: pointer-mutator + finalizer workloads,
fragmentation definitions for backends 0-3, first measurement report.

## Workload 4 (finalizers) landed (2026-06-12)

`longrun_finalizers` (continuous __del__/weakref churn; 8th CSV field
is the created-minus-finalized canary gap) landed; gc0/3/4 quick runs
all end with gap == 1 — exactly the loop-variable-keeps-last-object
semantics, asserted <= 2 in the smoke gate. Smoke now covers THREE
workloads x five backends (15 passed); the manual tier script gained a
finalizers stage (FIN_ROUNDS, default 100k). Remaining: workload 3
(pointer mutator), fragmentation definitions for backends 0-3, first
measurement report.

## Workload 3 (pointer mutator) landed — workload set COMPLETE (2026-06-12)

`longrun_pointer_mutator` (fixed 4096-node graph, deterministic
stride rewiring of next/buddy pointers — write-barrier/remembered-set
pressure with minimal allocation; integrity spot-check) landed;
gc0/3/4 quick runs clean. Smoke gate now covers ALL FOUR planned
workloads x five backends (20 passed); the manual tier script runs
all four (PM_ROUNDS, default 200k). The workload set from the original
design is COMPLETE. Remaining: fragmentation definitions for backends
0-3, and the first measurement report (manual-tier run + analysis
into docs/reports/) — the obligation-6 evidence loop's last two steps.

## First measurement report (2026-06-12)

`docs/reports/gc-longrun-first-report.md` — the first obligation-6
evidence in its own terms (20 series, final-sample table,
per-workload reads, scope limits). Headline short-window signals:
churn RSS stratification gc0 183MB / gc1-3 ~940MB / gc4 2069MB with a
2.3s->10.2s wall spread; first real pause histograms (gc1/gc2
auto-steps, ~650 pauses, max 20-243us); finalizer canary == 1
everywhere. The initially-suspected backends-3/4
endpoint gap was CORRECTED same-day: `pcc_gc_step` times all backends
uniformly in both tiers; the empty columns reflect trigger-policy
differences under short windows (a research observation, queued for
the minutes-scale characterization).
Remaining queue updated accordingly.

## Fragmentation definitions for backends 0-3 (2026-06-12)

Backends 0-3 are malloc-backed, so their fragmentation/overhead axis is
defined at the allocator level via a new C-only helper
(`pcc/runtime/src/py_os_heap.c`, OBJ_PY_CC_HELPERS, runtime-ABI
registered):

```text
pcc_os_heap_in_use_bytes    bytes currently handed to the program
pcc_os_heap_capacity_bytes  bytes the allocator holds from the OS
frag/overhead proxy          capacity - in_use   (ratio: 1 - use/cap)
```

Darwin uses `malloc_zone_statistics(NULL, ...)` (all zones); the Linux
`mallinfo2` branch ships UNTESTED until S-P2-LINUX. Backend 4 keeps its
richer zpage capacity/allocated telemetry; these helpers make the same
axis observable on 0-3 and on gc4's malloc-fallback share.

All four workloads now emit the two fields in their CSV (uniform
positions: ...,ops,heap_in_use,heap_capacity[,canary_gap]), and the
smoke gate asserts in_use > 0 and capacity >= in_use on every backend.
Gates: `tests/python/test_os_heap_stats.py` (C harness, retained-burst
growth + free shrink) 1 passed; smoke 19/20 (the 1 red is the
documented backend-4 exit crash, unrelated). First sample (2000-round
churn, NOT a claim): gc0 in_use 2.9MB / cap 9.8MB (71% overhead), gc3
4.3/10.1 (57%) — small-heap warmup numbers; minutes-scale comparison
belongs in the next report revision.

This closes the last surface item from the original design. Remaining
G-P3 queue: quiet-host pinned-conditions re-run for the report, and
the backend-4 crash blocking `[4-churn]` (tracked in
`docs/investigations/gc-backend4-churn-exit-list-item-uaf.md`).
