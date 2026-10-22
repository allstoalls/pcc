# GC abstraction layer + pluggable backend

**Status:** active implementation track. The `pcc_gc_*` ABI surface is
now present in the runtime header, C runtime, pcc-Python runtime port,
and Python frontend codegen. The first concrete backend remains
CPython-style refcount; the roadmap now commits to all five reference
designs under `docs/research/gc-survey/`, implemented in dependency
order rather than as isolated experiments.

Parent track: `docs/issues/gc-semantics-gap.md`. This doc is the
sub-track that defines the **interface** and backend order. Collector
algorithm details live in `docs/research/gc-survey/`.

---

## Why pcc can do this when CPython can't

CPython is locked into reference counting because `Py_INCREF` /
`Py_DECREF` are part of the public C extension ABI. Thousands of
extensions (numpy, pillow, lxml, …) rely on the exact refcount
discipline. The faster-cpython team has explored alternatives for
years; extension compatibility blocks every path forward.

pcc's situation is structurally different:

| dimension | CPython | pcc |
|---|---|---|
| extension API | `Py_INCREF` is public ABI | no public extension API |
| refcount call sites | thousands of `.c` files | codegen + `pcc/runtime/` only |
| compatibility constraint | numpy etc. cannot break | only Python language semantics |

We control the entire stack — codegen, runtime, link strategy. The
only thing we owe users is **observable Python semantics**, not any
specific memory-management mechanism. PyPy proved this works (PyPy
runs Python under tracing GC, no refcount); Jython / IronPython /
GraalPy use their host VM's GC; HPy is the in-progress effort to give
CPython itself a non-refcount-bound extension story.

---

## Reference design — what HotSpot does

OpenJDK HotSpot exposes its GC interface through a small set of
hooks. Every collector (Serial / Parallel / G1 / ZGC / Shenandoah)
implements these:

1. `alloc(size, type_tag) -> obj_ptr` — object creation
2. `write_barrier(field_addr, new_val)` — fired on every heap pointer
   store
3. `read_barrier(addr) -> val` — fired on every heap pointer load
   (only ZGC / Shenandoah; cheaper collectors skip this)
4. `safepoint()` — global synchronization point for concurrent GC
5. `root_scan(callback)` — iterate all root references for the
   marker
6. `finalize(obj)` — finalizer dispatch
7. `weakref_register(obj, ref)` / `weakref_invalidate(ref)` —
   weakref bookkeeping

Selection is at JVM startup with `-XX:+UseG1GC` style flags. The hot
path is the same regardless — just the hook implementations differ.

The relevant property for pcc: **HotSpot codegen does not bake any
specific GC into the JIT output**. It calls into the GC interface,
and the interface implementation is replaceable.

---

## What pcc emits today

Today every codegen-emitted object op materializes refcount calls
inline. Sample IR for `obj.field = x`:

```llvm
call void @py_incref(ptr %x)             ; protect new value
%old = load ptr, ptr %field_addr         ; capture previous value
store ptr %x, ptr %field_addr            ; commit assignment
call void @py_decref(ptr %old)           ; release previous value
```

Sample IR for `local = expr`:

```llvm
%v = call ptr @some_runtime_op(...)      ; produces a +1 refcount
call void @py_decref(ptr %old_local)     ; if rebinding
store ptr %v, ptr %local_slot
```

These are scattered across hundreds of codegen sites in
`pcc/frontends/python/codegen/layer1.py` and the runtime in
`pcc/runtime/src/*.c` + `pcc/runtime/py/*.py`. Each site
hand-codes the refcount discipline.

This is **the inlined refcount baked into IR** — the exact
arrangement that would block CPython from switching GCs at the API
level.

---

## Proposed abstraction (one new layer, no behavior change today)

Replace the inlined refcount calls with calls to a thin GC
abstraction. The refcount implementation becomes one entry in the
table; future tracing / generational implementations slot in beside
it without touching codegen.

### Hook set

```c
/* Allocation */
PyObject *pcc_gc_alloc(int64_t size, int32_t type_tag);

/* Heap reference assignment — covers obj.field = x and tuple/list
 * setitem. Replaces the inlined incref(new) + decref(old). */
void pcc_gc_assign_field(PyObject *obj, int64_t offset, PyObject *value);

/* Heap reference read — refcount/non-moving GCs make this a no-op,
 * concurrent moving GCs (ZGC / Shenandoah) inject a barrier here. */
PyObject *pcc_gc_read_field(PyObject *obj, int64_t offset);

/* Local variable assignment — covers Python-level rebinding inside
 * a function frame. Today this is a decref(old) + incref(new); under
 * tracing this is just a store. */
void pcc_gc_assign_local(PyObject **slot, PyObject *value);

/* Stack frame lifecycle for root-set tracking. Refcount ignores
 * these; tracing GCs use them to find roots. */
void pcc_gc_root_register(PyObject **slots, int32_t count);
void pcc_gc_root_unregister(PyObject **slots, int32_t count);

/* Safepoint — refcount no-op; concurrent collectors poll here for
 * pause requests. Codegen emits at loop back-edges and at every
 * call site that might block. */
void pcc_gc_safepoint(void);

/* Explicit collection request (gc.collect()). */
int64_t pcc_gc_collect(void);

/* Finalizer dispatch. Refcount calls this on last decref; tracing
 * GC calls it during sweep. Caller-side semantics are identical. */
void pcc_gc_finalize(PyObject *obj);

/* Weakref bookkeeping. */
void pcc_gc_weakref_register(PyObject *obj, PyObject *weakref);
void pcc_gc_weakref_invalidate(PyObject *weakref);
```

Roughly 10 entry points. Every collector implements them.

### Implementation table

```c
typedef struct {
    PyObject *(*alloc)(int64_t, int32_t);
    void (*assign_field)(PyObject *, int64_t, PyObject *);
    PyObject *(*read_field)(PyObject *, int64_t);
    void (*assign_local)(PyObject **, PyObject *);
    void (*root_register)(PyObject **, int32_t);
    void (*root_unregister)(PyObject **, int32_t);
    void (*safepoint)(void);
    int64_t (*collect)(void);
    void (*finalize)(PyObject *);
    void (*weakref_register)(PyObject *, PyObject *);
    void (*weakref_invalidate)(PyObject *);
} PccGcOps;

extern const PccGcOps *pcc_gc;  /* selected at link time */
```

Build-time selection: link one of `gc_refcount.o` / `gc_tracing.o`
/ `gc_generational.o`, each providing `pcc_gc` as a pointer to its
ops table. Or — simpler — direct call binding via macros and let LTO
inline.

---

## Migration plan (non-disruptive)

The migration is a **rename + indirection**, not a rewrite. Behavior
is preserved at every step.

**Step 1: introduce abstraction without changing behavior**
- Add `pcc/runtime/include/py_gc_abstract.h` declaring the hook
  interface.
- Add `pcc/runtime/src/gc_refcount.c` whose implementations are
  trivial wrappers around the existing `py_incref` / `py_decref` /
  `py_obj_alloc`. e.g. `pcc_gc_assign_field` is the inlined sequence
  pcc emits today.
- Verify byte-identical bootstrap (the abstraction is a pass-through
  rename).

**Step 2: switch codegen to call the abstraction**
- `pcc/frontends/python/codegen/layer1.py` emits `pcc_gc_assign_*` calls
  instead of inlined `py_incref` / `py_decref` / `store` triples.
- Same byte-identical bootstrap requirement: with `gc_refcount.c`
  selected, output should be functionally equivalent (small IR text
  differences are OK; runtime semantics must match).

**Step 3: lock the surface**
- Add a test (`tests/test_gc_abstraction_surface.py`) that scans
  codegen output for any remaining direct `py_incref` / `py_decref`
  call sites outside `gc_refcount.c`. Failure means someone bypassed
  the abstraction.

After step 3, refcount is still the only collector, but the
**interface is locked**. Future collectors are pure runtime work —
no codegen changes needed.

---

## Five-backend implementation roadmap

The implementation target is not "one abstract interface plus maybe a
second collector". The GC survey is now the backend list:

| order | backend | source doc | why this order |
|---|---|---|---|
| 1 | CPython-style refcount + cycle collector | `docs/research/gc-survey/01-cpython-original.md` | Default Python semantics: deterministic `__del__`, stable `id`, immediate non-cycle reclamation. pcc already has the refcount half. |
| 2 | Lua-style incremental tricolor | `docs/research/gc-survey/04-lua.md` | Closest non-refcount fit for single-threaded pcc: non-moving, step-based, no worker-thread prerequisite. |
| 3 | Go-style concurrent mark-sweep / Green Tea | `docs/research/gc-survey/03-go-greentea.md` | Non-moving and production proven, but needs multi-threading, stack maps, and a real write-barrier buffer. |
| 4 | OCaml-style generational multicore | `docs/research/gc-survey/05-ocaml.md` | Best reference for domain-local minor heaps and precise stack maps; gated on multi-threaded pcc and generational layout. |
| 5 | ZGC-style colored-pointer moving collector | `docs/research/gc-survey/02-zgc.md` | Aspirational low-pause backend. Requires read barriers, forwarding tables, pinning semantics, stack maps, and id-indirection or non-moving restrictions. |

### Implementation snapshot — 2026-05-02

The first implementation pass has landed the shared public ABI and
executable backend state machines, but only the CPython-style
refcount/cycle path is intended as Python's default runtime semantics.

Implemented now:

- `pcc_gc_*` public ABI in the C runtime, pcc-Python runtime archive,
  frontend ABI table, and focused surface tests.
- Algorithmic backend selector names:
  `refcount-cycle`, `incremental-tricolor`,
  `concurrent-mark-sweep`, `generational-minor-major`, and
  `colored-relocating`.
- Refcount/cycle default hooks, weakref invalidation, and
  `ReferenceError` preservation for dead proxy access.
- Tracing backend root-frame registration, object-list tracking,
  mark-color transitions, referent tracing for the core container
  shapes, write-barrier graying, bounded `pcc_gc_step`, and sweep
  candidate marking.
- Generational backend young/old flags, remembered-set marking on
  old-to-young stores, and bounded promotion work.
- Colored-relocating backend pin/unpin accounting, relocation
  candidate marking, and read-barrier candidate clearing.

Still not complete:

- Alternative backends do not yet reclaim unreachable storage.
- Go-style concurrent marking is not concurrent until runtime threads,
  stack maps, and worker synchronization exist.
- OCaml-style backend does not yet have domain-local minor heaps.
- ZGC-style backend does not move objects yet and therefore has no
  forwarding table or stable-id indirection.
- pcc-Python runtime root tracking is intentionally simpler than the C
  runtime frame stack and still needs parity work.

The order matters. Every later backend must fit the same `pcc_gc_*`
surface, but the default backend must remain CPython-style until the
runtime can prove Python-observable semantics for finalizers, weakrefs,
`id()`, and resource cleanup under alternative collectors.

### RM-GC-1 — CPython-style default backend

This is the current implementation line.

- Keep refcount in `pcc_gc_retain` / `pcc_gc_release`.
- Keep `pcc_gc_load_ptr` as a plain load.
- Keep `pcc_gc_store_ptr` as `retain(new); store; release(old)`.
- Wire Python-visible `gc.collect()` to `pcc_gc_collect`.
- Add one generation list first, then grow to CPython-like three
  generations.
- Implement `update_refs` / `subtract_refs` / `move_unreachable`
  from the CPython survey.
- Add finalizer and weakref ordering before freeing.

Exit criteria:
- `gc.collect()` works with no libpython.
- Simple object/list/dict/closure cycles are reclaimed.
- Weakref callbacks run before `__del__`.
- CPython-compatible refcount timing remains true for non-cycles.

### RM-GC-2 — Lua-style incremental backend

This is the first non-refcount backend because it keeps the same
single-threaded, non-moving assumptions as today's pcc runtime.

- Add collector color bits and a global object list.
- Replace refcount-specific `store_ptr` behavior with a forward
  write barrier when this backend is selected.
- Implement a bounded-step tricolor state machine.
- Trigger `pcc_gc_safepoint` at loop backedges and blocking calls.
- Reuse RM-GC-1 finalizer/weakref machinery.

Exit criteria:
- The backend can run all no-libpython runtime oracle tests.
- `gc.collect()` forces a full cycle.
- Step budget telemetry shows no unbounded pause on allocation-heavy
  programs.

### RM-GC-3 — Go-style concurrent mark-sweep backend

This backend is valuable only after pcc has real runtime
multi-threading support.

- Add size-class/span allocator.
- Add per-thread allocation cache.
- Add precise pointer maps for object layouts.
- Turn `pcc_gc_store_ptr` into a write-barrier-buffer append.
- Add mark workers and lazy sweep.
- Add stack maps through `pcc_gc_frame_enter`.

Exit criteria:
- Non-moving address stability preserves `id()`.
- Write barrier stress tests preserve tricolor invariants.
- Multi-threaded allocation/collection tests pass.

### RM-GC-4 — OCaml-style generational multicore backend

This backend follows Go-style infrastructure but adds a young
generation and per-domain minor heaps.

- Add minor heap allocation path.
- Add remembered set for old-to-young pointers.
- Add promotion into shared major heap.
- Add major incremental mark stack with bounded fallback.
- Decide whether compaction is disabled by default or hidden behind
  id-indirection.

Exit criteria:
- Minor collections are bounded by young survivor size.
- Major collections are incremental.
- Python `id()` stays stable.

### RM-GC-5 — ZGC-style moving/color backend

This is a research backend, not a default target until pcc has stack
maps, read barriers, and pinning.

- Make `pcc_gc_load_ptr(owner, slot)` a real load barrier.
- Add forwarding table and relocation set.
- Add pin/unpin semantics for exposed raw pointers and FFI.
- Add colored-pointer or side-table state.
- Add id-indirection or statically restrict movement.

Exit criteria:
- Pointer loads self-heal after relocation.
- Pinned objects never move.
- `id()` is stable for moved objects.
- Concurrent collection has bounded stop-the-world phases.

## Implementation roadmap (post-abstraction)

These are the collectors the abstraction makes possible. Each is
optional and independent.

### Phase A — refcount + cycle (current G1 plan)

The existing G0-G5 plan from `gc-semantics-gap.md` is implemented as
the first concrete `PccGcOps` table. G1 (tricolor cycle collector)
plugs into the same hook set:

- `assign_field` writes the refcount + cycle-suspect bit.
- `collect()` runs the tricolor scan over cycle-suspect roots.
- `finalize` is called from the cycle reclamation path.

No real change from the existing G1 plan — just expressed through
the new interface.

### Phase B — non-moving tracing GC (Boehm-style)

A mark-sweep collector that scans the heap for reachability. Doesn't
move objects, so `id(obj)` stays stable.

- `alloc` becomes a region allocator (depends on G6 region work).
- `assign_field` is a plain pointer store + card-marking write
  barrier.
- `assign_local` is a plain pointer store (no refcount).
- `root_register` / `root_unregister` are real — collector needs the
  stack root set.
- `safepoint` is a poll for "GC requested" flag.
- `collect` runs mark + sweep.
- `finalize` runs during sweep (caveat: timing differs from
  refcount, see hard constraints below).

Estimated cost: 2-3 months solo work on top of the abstraction
(without it, would be much more).

### Phase C — generational

Add young / old generation, promote on survival.
- `alloc` allocates in the young region.
- `assign_field` includes a remembered-set entry when an old object
  points into young.
- `collect` does a young-only collection, full collection on
  fragmentation threshold.

Estimated cost: 4-6 months on top of Phase B.

### Phase D — concurrent / incremental

Map onto Phase G7 / G8 from the existing GC research track. Now they
have a well-defined interface to plug into.

---

## Hard constraints

The interface choice is constrained by Python's observable semantics.
These are not implementation details; user code depends on them.

### `__del__` deterministic timing

Refcount fires `__del__` immediately at last-decref. Tracing GC fires
it on the next collection — "eventually", no time bound. User code
relies on this for resource cleanup:

```python
f = open("/tmp/x")
process(f)
del f                # CPython: file closed here
                     # Java-style GC: file closed... eventually
```

Java deprecated finalizers because of exactly this. Python
historically promotes `with` for resource scoping but can't break
the `__del__` contract — too much code uses it.

**Mitigation under tracing GC:** keep refcount for deterministic
finalization, run tracing only for cycles. This is a "hybrid" GC,
similar to CPython's current arrangement but with the tracing piece
swappable. The interface accommodates both — deterministic-finalize
implementations (`gc_refcount`, `gc_hybrid`) call `finalize` from
`assign_*` hooks; pure-tracing (`gc_tracing`) calls it from sweep.

### `id(obj)` stability

`id(obj) == id(obj)` must hold for the entire lifetime of `obj`.
Moving collectors (compacting, copying generational young) violate
this directly.

**Mitigations:**
- Non-moving tracing (Boehm style) — no compaction, ID stays stable
  by construction.
- ID indirection table — every object gets a permanent ID slot,
  remapped on move. Per-object cost.
- Restrict moving to objects with no `id()` exposure — hard to prove
  statically.

The interface is moving-agnostic (read barriers exist for that
reason); the constraint is on which implementations are admissible.

### `weakref` callback timing

CPython's contract: weakref invalidation runs *before* the target's
finalizer. Refcount achieves this via dedicated weakref slot
clearing in dealloc. Tracing GCs typically do it during the sweep
phase, but ordering needs care.

The interface's `weakref_invalidate` hook lets each collector
schedule the callback consistent with the language spec. The
sequencing within the abstract interface is:

```
on object death:
  pcc_gc_weakref_invalidate(...)   # all weakrefs first
  pcc_gc_finalize(obj)             # then __del__
  release storage
```

### Multi-threading

Refcount in concurrent context needs atomic incref/decref (CPython
3.13 free-threaded does this; ~30% perf hit). Tracing GCs handle
multi-threading naturally via per-thread alloc + global GC sync.

The interface is multi-thread agnostic: `pcc_gc_assign_*` semantics
say nothing about whether the underlying writes are atomic, and a
multi-thread `gc_tracing.c` can use the same hook signatures with
per-thread alloc internally.

---

## Cost / benefit

**Cost (interface only, Step 1+2+3 above):**
- The first ABI slice has landed in `pcc_gc_*`.
- Remaining cost is parity work: removing direct refcount escapes,
  expanding pcc-Python frame-root tracking, and making each alternative
  backend own real reclamation rather than only state transitions.

**Cost (each new collector):**
- Phase B (tracing): 2-3 months
- Phase C (generational): 4-6 months on top of B
- Phase D (concurrent): G7/G8 from existing plan, now plugged in
  cleanly

**Benefit:**
- Future-proof: adding a 2nd collector is a runtime-only change. No
  codegen disruption ever again.
- Multi-threading story: the per-collector hook layer accommodates
  the hard concurrency choice (atomic refcount vs tracing) without
  user-code or codegen rework.
- Research platform: pcc can host GC research (concurrent /
  generational / colored-pointer experiments) as alternative `gc_*.c`
  files. This was already a goal in `gc-semantics-gap.md` G6-G10;
  the abstraction is the missing infrastructure piece.
- Embedded / specialized: small-target builds could ship a region-
  only collector with no cycle work.

**Anti-benefit (real cost of doing this):**
- Adds one indirection layer; LTO has to inline it back. With LTO
  off, ~5-10% runtime cost. With LTO on, near-zero.
- Compilation overhead: codegen has to track ownership transfers
  more carefully (currently inlined refcount lets codegen be sloppy
  in spots — abstraction tightens the contract).
- Migration risk: every codegen change of this size has bugs. The
  byte-identical bootstrap test catches functional regressions, not
  perf regressions.

---

## When to do this

This work is now committed. The old question was whether to pay the
abstraction cost; the current question is how far each backend must go
before it is allowed outside tests. The answer is strict: only
`refcount-cycle` is production/default until the alternative backend can
prove Python-observable semantics for finalizers, weakrefs, stable
`id()`, and no-libpython bootstrap execution.

The non-default backends are valuable now as interface pressure tests.
They should stay selectable, covered by focused gates, and isolated from
default Python semantics until their missing collector mechanics are
filled in.

---

## Open questions

1. **Hook granularity.** Is `assign_field` enough, or do we need
   separate hooks for tuple set / list set / dict set / instance
   field? Probably enough at the layer1 level, but the runtime ports
   in `pcc/runtime/py/*.py` may need finer-grained barriers.

2. **LTO assumption.** The abstraction collapses into inlined
   refcount only with LTO. Without LTO (e.g. `-O0` self-host
   bootstrap) we pay the indirection. Do we accept this for
   non-default builds?

3. **`pcc.unsafe` interplay.** `pcc.unsafe.untag_int` etc. bypass
   refcount because they're known-non-heap. The abstraction has to
   keep the same fast paths or we lose RM-P5's tagged-int win. The
   GC ops table can have a `is_managed_pointer(ptr) -> bool`
   short-circuit, but every hot path needs review.

4. **Bootstrap byte-identity.** If we land the abstraction without
   changing the underlying refcount semantics, can we keep
   `pcc2`/`pcc3` byte-identical? Probably yes, but the rename pass
   is large enough that bootstrap regression risk is real. Phased
   landing with frequent `cmp pcc2 pcc3` is essential.

5. **Naming.** `pcc_gc_*` is the obvious choice but conflicts with
   the planned `gc.collect()` Python-level module name. Maybe
   `pcc_mm_*` (memory management) is cleaner. Bikeshed in PR.

---

## Status

| step | scope | done? |
|---|---|---|
| design (this doc) | sketch interface + migration plan | ✅ |
| commitment to do | gated on G6.5 inclusion in roadmap | ✅ |
| step 1 — public ABI | `pcc_gc_*` API in `py_runtime.h` and frontend ABI table | ✅ |
| step 2 — codegen ownership calls | layer1 owned-ref retain/release targets `pcc_gc_*` instead of direct `py_incref`/`py_decref` | ✅ |
| step 3 — surface lock | focused tests assert public ABI and no direct layer1 refcount surface | ✅ |
| Phase A | CPython-style refcount + cycle collector | ✅ default backend |
| Phase B | incremental tricolor / non-moving tracing contract | partial: root/frame tracing, object table, write barrier, bounded step, sweep-candidate marking |
| Phase C | concurrent mark-sweep contract | partial: shared tracing core, unconditional CMS write barrier, bounded safepoints; real threads/stack maps pending |
| Phase D | generational minor/major contract | partial: young/old flags, remembered set, promotion step; real minor allocator/domain heaps pending |
| Phase E | colored relocating contract | partial: pin/unpin, relocation candidates, read barrier; real moving storage/id indirection pending |

`tests/test_gc_abstraction_surface.py` is the current focused gate.
It covers all five backend selectors in no-libpython binaries and
checks list/tuple/dict/instance/coroutine tracing, old-to-young
promotion, and colored relocating read-barrier state. The non-default
backends remain experimental until they own real reclamation with
precise roots and Python-observable finalizer/weakref/id semantics.
