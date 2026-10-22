# GC semantics gap — what's left after the 2026-04-29 → 2026-05-08 push

**Status:** open. Original snapshot 2026-04-29 (refcount-only); rewritten
2026-05-09 after the GC backend / `__del__` / weakref / cycle-collector
work landed. The 2026-04-29 version of this document predates the
implementation and should not be relied on.

## Where we are now

CPython's GC contract is no longer "all stubbed". Production state on
backend #0 (refcount + STW cycle collector):

| Surface | 2026-04-29 status | Current status |
|---|---|---|
| Refcount | yes (CPython-shape) | unchanged |
| Cycle collector | no-op stub | **runs** under `pcc_gc_collect()` (`py_obj_gc.c:464`): STW gate → `recompute_reachability` → `maybe_finalize_unreachable` → `weakref_invalidate` → `clear_referents` → free |
| `__del__` finalizers | not dispatched | **dispatched** at `py_instance_dealloc` (`py_class.c:677`) via `py_user_del_dispatch` (`py_dunder.c:146`); resurrection and `FINALIZED` flag handling in place |
| `weakref` | absent | **native** (`py_weakref.c`): `py_weakref_new`, `py_weakref_call`, `py_weakref_invalidate`; hooked from instance dealloc and cycle clear |
| Atomic refcount | no | yes under `PCC_WITH_THREADS=1`; non-atomic in the default build |
| `gc.callbacks` | not wired | wired (Backend #1 traces pinned callback list via `pcc_gc_alloc`-routed `py_func_new`) |

Pluggable GC backend slots (selected via `PCC_GC_BACKEND` env, enum in
`pcc/runtime/include/py_obj.h::PCC_GC_KIND_*`) — current production-gated
state matches `goal.md` and `pcc.diagnostics.runtime_report`:

| Slot | Algorithm | Reference | Status |
|---|---|---|---|
| #0 | refcount + STW cycle collector | CPython (`docs/refs_docs/gc-research/python/`) | production / default |
| #1 | incremental tricolor mark-sweep | Lua 5.4 (`docs/refs_docs/gc-research/lua/`) | production-gated: pacer/debt/root/finalizer focused gates green |
| #2 | concurrent mark-sweep | Go greentea (`docs/refs_docs/gc-research/go-greentea/`) | production-gated: worker/assist/STW mark termination, buffered barrier, lifecycle, and TSan stress green |
| #3 | generational | OCaml (`docs/refs_docs/gc-research/ocaml/`) | production-gated: minor arena, copy-oldification, slot-aware eager rewrite, pcc-Python parity, and cross-domain remembered-slot gates green |
| #4 | colored relocating / modern GenZGC target | ZGC (`docs/refs_docs/gc-research/zgc/`, OpenJDK `jdk-27+21`) | production-gated for pcc's current forwarding/read-barrier/relocation slices plus first GenZGC store-buffer drain/backlog/duplicate-skip/high-water/owner-fanout/owner-count telemetry with reset reseeding, small/medium page-class evacuation-selector count/bytes/page-pressure plus evacuation-backlog telemetry with pending relocation-set reset reseeding and clear semantics, and telemetry-epoch idempotent large-object defer count/bytes slices; still open for full GenZGC young/old policy, real ZPage evacuation, remembered-set bitmap/true store-buffer batching, fragmentation policy, and complete reference-updating coverage |

## What's still missing (the actual gap)

This is the list that should drive future work. None of these are stubs;
all are partial/in-progress and will surface on real workloads.

### 1. Cycle collector pacing

`pcc_gc_collect()` runs only when the user calls `gc.collect()` or when
allocation thresholds in some backends fire it explicitly. CPython's
generational pacing (`gc.set_threshold()`) is not yet replicated on
backend #0. Long-running programs that do not call `gc.collect()` will
accumulate cyclic garbage until they do.

### 2. Finalizer / weakref edge cases

- `__del__` resurrection paths exist but `gc.garbage` semantics (objects
  whose `__del__` made them reachable again) are minimal.
- `weakref` callbacks fire on dealloc but `weakref.WeakValueDictionary`,
  `WeakSet`, and weak-method binding are not all native; the runtime
  has the pieces (`py_weakref_invalidate`) but the Python-level
  containers may still go through the libpython fallback.
- Per-finalizer "exception → unraisable warning" plumbing exists
  (`py_clear_exception()` after `meth(o)` in `py_dunder.c`) but does
  not yet surface `sys.unraisablehook`-style diagnostics.

### 3. Free-threaded mutable container races

Concurrent mutation of shared `list` / `dict` / instance fields under a
user-level `threading.Lock` from multiple pthreads is **not yet
correct**: a tight `counts[0] = counts[0] + 1` loop loses updates;
`list.append` from multiple threads can crash. The substrate is in
place (atomic refcount under `PCC_WITH_THREADS=1`, real pthread locks,
real pthreads, BoC `Cown`), but the box/unbox and container-mutation
paths still need a barrier audit. Tracking issue:
[`docs/issues/gc-threading-substrate.md`](gc-threading-substrate.md).

### 4. Backend #2 future algorithmic deepening

Correctness + TSan are green for the pcc production contract. Future
algorithmic deepening can still make it closer to Go:
- richer Go-style work-buffer / drain policy beyond the buffered barrier MVP
- less conservative concurrent span / object sweep
- mark-termination scheduling under heavier mutator load

### 5. Backend #3 future algorithmic deepening

Slot-aware eager rewrite covers list/tuple/dict/set/instance/Task,
scheduler roots, and a cross-domain remembered-slot gate. Future
algorithmic deepening can still improve:
- forwarded-minor source cleanup policy beyond inactive-marking
- broader pcc-Python threaded object-index / object-list stress
- generator/coroutine stackless heap-frame model (this is the large
  blocker shared with the user-mode-scheduling track)

### 6. Backend #4 future algorithmic deepening

Reference-bearing relocation covers list/tuple/dict/set/instance/Task and
scheduler queue read-barriers. Live evacuation debt is now measured as
relocation-set + forwarding-entry pressure and drops after read-barrier repair.
The first GenZGC-alignment slice also records old-to-young stores through a
backend-4 store barrier and processes remembered owners before relocation
work. Future algorithmic deepening can still add:
- a literal ZHeap-style page allocator / evacuation policy
- a real GenZGC remembered-set / store-buffer data structure
- broader mutator / collector concurrent relocation stress
- relocation-target phase-progress / unbounded-loop hardening (initial
  phase-bit defence landed, more work needed)

### 7. User-mode scheduling (cross-cutting)

Suspended generator/coroutine/Task frames need to be heap objects so
backends #1–#4 can trace and update them. Initial Task object
(`PY_TYPE_TASK`) and scheduler-queue substrate
(`PccGcSchedulerQueue`) landed; the full stackless task state machine,
runnable/timer/IO/wakeup/await-chain queues, and cross-backend matrix
in `tests/test_gc_coroutine_roots.py` are partial. See
`docs/refs_docs/gc-research/user-mode-scheduling/README.md` for the
reference design.

## Impact on bootstrap

None of the above blocks `pcc1 → pcc2 → pcc3` byte-identical
reproduction. The bootstrap closure does not build cycles, does not
use `__del__` semantically, does not depend on weakref dispatch, and
runs single-threaded. The frozen evidence is in
[`tests/bootstrap_gate_baseline.json`](../../tests/bootstrap_gate_baseline.json).

## Pointers

- Code: `pcc/runtime/src/py_obj_gc.c`,
  `pcc/runtime/src/py_class.c`, `pcc/runtime/src/py_dunder.c`,
  `pcc/runtime/src/py_weakref.c`,
  `pcc/runtime/src/py_gc_backend.c`,
  `pcc/runtime/include/py_obj.h`.
- Reference implementations:
  [`docs/refs_docs/gc-research/`](../refs_docs/gc-research/).
- Backend #0..#4 progress log: `goal.md` (running progress notes).
- Companion issues:
  [`docs/issues/gc-pluggable-backend.md`](gc-pluggable-backend.md),
  [`docs/issues/gc-threading-substrate.md`](gc-threading-substrate.md).
