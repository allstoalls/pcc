# GC threading substrate

**Status:** design committed, substrate implemented behind the existing
runtime ABI. A minimal native-backed `threading` shim is now wired through
layer1 for `Thread`, `Lock`, `RLock`, `Event`, `Condition`, and `Semaphore`;
full free-threaded Python object semantics remain guarded.

This document turns the research notes in
`docs/research/gc-multi-thread-research.md` into an implementation track
that fits the existing 5-backend GC abstraction from
`docs/issues/gc-pluggable-backend.md` and `docs/research/gc-survey/`.

---

## Decision

pcc has **one shared threading substrate**, not a separate pluggable
threading axis.

The substrate is intentionally small:

```text
pthread / single-thread fallback
  -> stable runtime thread id
  -> atomic refcount helper hooks
  -> mutex / cond wrappers for future GC workers
  -> cooperative safepoint / stop-the-world gate
  -> pcc_gc_safepoint() polls that gate
```

The GC backend remains the pluggable axis:

| backend | how it will consume the substrate |
|---|---|
| `refcount-cycle` | atomic or future biased refcount; STW cycle collection |
| `incremental-tricolor` | safepoint pacing; no worker thread required |
| `concurrent-mark-sweep` | background mark worker + write-barrier protocol |
| `generational-minor-major` | future per-thread/domain minor allocation |
| `colored-relocating` | load barrier + relocation worker + pin protocol |

This matches the survey conclusion: CPython, Go, OCaml, and HotSpot all
build on the same OS-thread/atomic/safepoint substrate; they differ in
GC semantics, not in a user-selectable thread runtime.

---

## What landed

### Public diagnostics/control ABI

The runtime exports a minimal stable surface:

```c
int64_t pcc_threads_enabled(void);
int64_t pcc_current_thread_id(void);
int64_t pcc_refcount_strategy(void);
void    pcc_thread_safepoint(void);
int64_t pcc_stop_the_world(void);
int64_t pcc_resume_world(void);
```

`pcc_gc_safepoint()` now calls `pcc_thread_safepoint()` before advancing
GC work. Existing single-threaded programs keep the same behavior: the
default build has `PCC_WITH_THREADS=0`, so the thread gate is a no-op and
`pcc_current_thread_id()` returns a stable synthetic id.

### Wired into backends

The substrate is now used on real GC paths rather than only exported as an
unused ABI:

| backend | integration point |
|---|---|
| `#0 refcount-cycle` | `pcc/runtime/src/py_obj_gc.c::py_gc_collect` (around the update_refs / subtract_refs / mark / dealloc cycle, ~L400-L475) and `pcc/runtime/py/py_obj_gc.py::py_gc_collect` (~L405-L485) wrap collection with `pcc_stop_the_world()` / `pcc_resume_world()`. |
| `#1 incremental-tricolor` | `pcc/runtime/src/py_gc_backend.c::pcc_gc_step_trace_cycle` and `pcc/runtime/py/py_gc_backend.py::_step_tracing` poll safepoints while processing gray objects; the final white-to-sweep cut runs under STW. |
| `#2 concurrent-mark-sweep` | Currently shares the #1 tracing core plus unconditional barrier graying. It has substrate-safe mark/sweep phases but not a real background worker yet. |
| `#3 generational-minor-major` | `pcc/runtime/src/py_gc_backend.c::pcc_gc_step` (~L455-L505) and `pcc/runtime/py/py_gc_backend.py::pcc_gc_step` backend-3 branch (~L540-L575) poll `pcc_thread_safepoint()` between bounded promotion batches. |
| `#4 colored-relocating / GenZGC target` | `pcc_gc_load_ptr` / `pcc_gc_note_relocation_read` cover the current pcc read-barrier/relocation substrate, and `pcc_gc_store_ptr` now records GenZGC-style old-to-young owner+slot+value store-buffer entries with drain/backlog/duplicate-skip/high-water/owner-fanout/owner-count telemetry and reset reseeding. Backend #4 also exposes first small/medium page-class evacuation-selector count/bytes/page-pressure plus evacuation-backlog telemetry with pending relocation-set reset reseeding / clear semantics and telemetry-epoch idempotent large-object defer count/bytes seams. The upstream reference is OpenJDK `jdk-27+21` GenZGC under `docs/refs_docs/gc-research/zgc/`; pcc still needs full young/old policy, remembered-set bitmap / act-once policy, true store-buffer batching, real ZPage evacuation, fragmentation policy, and complete reference-updating coverage before backend #4 is production-complete. |

These are intentionally backend-internal uses of the one shared substrate.
They do not create a second pluggable threading axis. Python-level threading
is native-backed for simple APIs, but containers and GC side tables are not
yet fully free-threaded.

### Internal substrate ABI

The GC/runtime side gets opaque wrappers:

```c
PccThreadHandle / PccMutex / PccCond
pcc_thread_start / join / detach
pcc_mutex_new / lock / unlock / free
pcc_cond_new / wait / signal / broadcast / free
pcc_refcount_incref / pcc_refcount_decref
```

They compile to no-op/single-thread fallbacks unless the runtime is built
with `PCC_WITH_THREADS=1`.

### Refcount strategy staging

The public enum reserves the four strategies from the research doc:

```c
PCC_REFCOUNT_STRATEGY_NONATOMIC = 0
PCC_REFCOUNT_STRATEGY_ATOMIC    = 1
PCC_REFCOUNT_STRATEGY_BIASED    = 2
PCC_REFCOUNT_STRATEGY_DEFERRED  = 3
```

BIASED and DEFERRED now build through a conservative side-table
implementation keyed by the refcount slot.  That is a correctness/testing
bridge, not the final PEP 703 fast path.  A deliberate `PyObjectHeader`
migration is still required because the
current header layout is relied on by the pcc-Python runtime port and
low-level tests:

```text
offset 0  refcount
offset 8  type_tag
offset 12 flags
```

That migration should happen as a dedicated ABI event, not as a hidden
side effect of adding pthread support.

---

## Build knobs

Default single-threaded build:

```bash
make -C pcc/runtime
```

Thread substrate build:

```bash
make -C pcc/runtime PCC_WITH_THREADS=1
```

Explicit atomic refcount without enabling pthread wrappers:

```bash
make -C pcc/runtime PCC_REFCOUNT_KIND=1
```

When `PCC_WITH_THREADS=1` and no explicit `PCC_REFCOUNT_KIND` is set,
the substrate defaults the refcount strategy to `ATOMIC`. This is still
not a complete free-threaded Python runtime: containers, GC object lists,
and Python-visible `threading` APIs need their own synchronization
protocols before user code is allowed to run in parallel.

---

## Why this is intentionally not `threading.Thread` yet

The research doc identifies a 7-layer dependency chain. This patch lands
layers 2, 4, and 7 foundations, and an optional layer-1 pthread wrapper:

1. thread spawn primitive — internal wrapper plus minimal `threading.Thread`
2. atomic memory operations — landed for refcounts
3. GIL or free-threaded object/container protocol — **not complete**
4. TLS for runtime thread identity — landed
5. `threading` module native API — partial: Thread/Lock/RLock/Event/Condition/Semaphore
6. `concurrent.futures` parallelism — **not landed**
7. GC concurrency protocol — safepoint/STW gate landed; backend worker
   protocols are future work

The current `threading.Thread` support is deliberately conservative:
default builds run targets synchronously, while `PCC_WITH_THREADS=1`
uses pthreads.  It is not yet a promise that all Python object mutation
is free-threaded. The standard-library `concurrent` fallback therefore
stays sequential until the object/container protocol is complete.

---

## Next implementation steps

1. Add a coarse runtime lock or PEP-703-style critical sections around
   list/dict/set mutation when `PCC_WITH_THREADS=1`.
2. Lock or shard `pcc_gc_objects`, `pcc_gc_frames`, and the refcount-cycle
   GC index table.
3. Make `pcc_stop_the_world()` part of `pcc_gc_collect()` for the
   backends that scan precise roots under concurrent mutators.
4. Add biased refcount in a separate `PyObjectHeader` ABI migration.
5. Only after the above, bind a native `threading` module.

This keeps the current runtime safe-by-default while giving the GC
backends a real substrate to grow into.
