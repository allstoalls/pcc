# Chapter 10: The Five-GC Architecture and the Equality Contract

A runtime that ships five garbage-collection backends owes the reader a defense: five collectors means five times the correctness surface, and the characteristic GC bug is irreproducible memory corruption. This chapter explains why pcc accepts that cost, and what architecture keeps the cost contained — one collector-selection ABI, one set of object-graph slot rules, one root-registration interface, one write/read barrier pair, and a "production equality rule" that pins all five backends to the same Python semantics. The internals of each backend (tricolor pacing, concurrent marking, generational promotion, colored relocation) belong to Chapter 11; this chapter covers only the skeleton the backends share, and why that skeleton must never grow a second copy.

## Chapter Overview: Reduce GC to Four Questions First

Do not start this topic from algorithm names such as "tricolor," "generational," or "relocating." That drops the reader straight into backend internals. Any GC can first be reduced to four questions:

1. **Where does the search for live objects start?** These entry points are roots: current function locals, suspended generator frames, scheduler queues, the current TLS exception, and C-extension module state.
2. **Which edges does the search follow?** Every `PyObject *` field inside an object is an edge; this chapter calls it a slot: list elements, instance dictionaries, an exception's `message`/`cause`/`context`, task state slots.
3. **When the program mutates the object graph, how does the collector find out?** A write barrier records "a new object was stored into this slot"; a read barrier repairs "this slot points at an address that may already have moved."
4. **When and by which policy does reclamation happen?** Only here do the backend algorithms enter: #0 is refcount-first, #1 marks in small steps, #2 delegates marking work to a thread, #3 splits young from old, and #4 allows objects to move.

A minimal object graph is enough to see the four questions:

```text
frame root
   │
   ▼
saved ──slot[0]──▶ exception ──message──▶ [1, 2, 3]
```

If `saved` is not registered as a root, the whole chain is invisible to the tracing backends. If `exception.message` is missing from slot traversal, the message can be cleared incorrectly. If `saved[0] = exception` bypasses the write barrier, #3/#4 may miss that an old object now points at a young object. If #4 has moved the exception and the read path bypasses the read barrier, the slot keeps the stale address. These four failure classes map directly onto the rest of this chapter: roots, the slot contract, write barriers, and read barriers. The source names can be read the same way: `pcc_gc_frame_enter()` is root registration, `pcc_gc_trace_referents()` walks slots, `pcc_gc_store_ptr()` is the write-barrier entry, `pcc_gc_load_ptr()` is the read-barrier entry, and `pcc_gc_step()` is where backend policy begins.

So this chapter is not asking you to memorize five collectors at once. Read it in this order: first ask how a live object is seen, then whether every object edge is enumerated, then whether edge changes and address changes are recorded, and only then compare how the five backends consume that shared information.

## 10.1 The Problem and the Design Space: Why Five Collectors

Start with why not one. Mainstream runtimes each bet on a single memory-management strategy: CPython on reference counting plus a cycle collector, Go on concurrent mark-sweep, OCaml on a generational minor heap, ZGC on colored-pointer relocation, Lua on incremental tricolor. Each bet is a different trade across the same long-running metrics — pause time, RSS, throughput, fragmentation as it evolves over hours — and each trade is welded into its runtime's object model. You cannot measure them against each other on the same program, the same semantics, the same object graph.

pcc's thesis (see Chapter 1) lists the "five-GC comparative runtime" as one of its five differentiators: not one collector plus four toys, but a research program — one compiled artifact, switched among five collection strategies by an environment variable, compared under the same bootstrap workload and the same semantic contract. The directory [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research/) holds source for the five reference implementations (CPython, Lua, Go, OCaml, ZGC), and repository rules require reading the reference before porting, not re-deriving it.

The dominant risk of this program is not that one algorithm is implemented wrong. It is that **the five backends each evolve their own notion of what is reachable and what is alive**. Once there are two sets of object-graph rules, the same program sees different object lifetimes on different backends, and the differences surface in the worst possible form: a use-after-free that only one backend exhibits, an attribute that goes missing on exactly one collector. The 5-GC Production Equality Rule in [goal-prompt.md](../../docs/goal/goal-prompt.md) is the institutional answer to that risk:

- **Semantics are a hard requirement and may not differ across backends**: object reachability, root safety, exception and frame survival, container-graph safety, the weakref/finalizer/resurrection policy, extension-object lifetime, value-class pointer-payload safety, virtual-thread suspended-frame and scheduler-root safety.
- **Performance is a reportable difference and may differ**: pauses, throughput, RSS, fragmentation profile, and collection schedule are each backend's own portrait.
- **The status vocabulary is enforced**: a runtime feature touching objects, references, or lifetime is `DONE_STRONG` only when it passes the common contract under `PCC_GC_BACKEND=0..4`. Passing only #0 is `DONE_WEAK`; passing some-but-not-all is `BACKEND_PARTIAL`. `#0 is the default ≠ #0 is the only production backend`; `#1–#4 selectable ≠ experimental`.

This rule is not documentation rhetoric. Section 10.7 shows how the first brick of its contract suite caught a real use-after-free that crashed three backends, and a missing root whose root cause lived in the frontend's code generation.

## 10.2 One ABI Surface: `PCC_GC_KIND_*` and Runtime Selection

The five backends exist as one enum in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h):

```c
enum {
    PCC_GC_KIND_REFCOUNT_CYCLE = 0,
    PCC_GC_KIND_INCREMENTAL_TRICOLOR = 1,
    PCC_GC_KIND_CONCURRENT_MARK_SWEEP = 2,
    PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR = 3,
    PCC_GC_KIND_COLORED_RELOCATING = 4
};
```

The pcc-Python mirror, [pcc/runtime/py/py_gc_backend.py](../../pcc/runtime/py/py_gc_backend.py), opens with a deliberate note that the names are algorithmic, not project-branded: refcount-cycle, incremental-tricolor, concurrent-mark-sweep, generational-minor-major, colored-relocating. Selection happens at runtime: `pcc_gc_init_config()` in `py_gc_backend.c` parses the `PCC_GC_BACKEND` environment variable (default 0) on first entry into a GC path, and `pcc_gc_set_backend()` allows in-process switching. The consequence: **one binary runs under five collection strategies**. That is what makes the five-backend bootstrap matrix ([tests/python/gc/](../../tests/python/gc/) with `test_pcc_bootstrap_full_gc{0..4}.py`, a full stage1→stage2→stage3 self-host bootstrap per backend) feasible as engineering — not five build products, just five runs.

The more consequential design decision is written in the GC-interface comment in `py_runtime.h`: the family `pcc_gc_alloc` / `pcc_gc_retain` / `pcc_gc_release` / `pcc_gc_load_ptr` / `pcc_gc_store_ptr` "are the memory-management ABI that codegen should target," and future tracing/generational/moving collectors "must preserve this surface rather than teaching codegen about their internals." `py_incref` / `py_decref` are explicitly positioned as refcount-shaped compatibility shims that "should not be treated as the foundational ABI for new code." The alternative — having the frontend emit different barrier sequences per backend — would multiply the backend count into the codegen test matrix and make "one binary, five modes" impossible. pcc instead folds all backend dispatch inside the runtime functions, at the cost of a backend check on every slot access. Section 10.4 shows what that check looks like.

## 10.3 One Set of Object-Graph Rules: The Slot Trace/Update Contract

### 10.3.1 Why there can be only one

A collector needs two capabilities over the object graph: **traversal** (visit: given an object, enumerate all of its pointer slots) and **update** (rewrite a slot to a new address — the relocating backends' requirement). The set of pointer slots for each runtime type — list, dict, instance, generator, task, exception — is that type's "shape" in the object graph.

Here "slot" is not an abstract term. It is a location in a runtime structure that can point at another Python object. `items[i]` in a list is a slot; a key/value pair in a dict table is a slot; an exception's message is a slot; a generator's saved heap frame is a slot. The collector does not understand the semantic sentence "the exception message matters." It asks an object to hand over every pointer slot. Miss one slot and the target may be reclaimed; miss one update after movement and a stale address remains in the graph.

If that shape is declared in two production implementations, it drifts; and drift does not fail at compile time. It manifests as one backend missing a slot during marking (the object is wrongly reclaimed) or missing one during relocation (a dangling pointer). AGENTS.md states this as a hard rule: all five backends consume **one pcc-Python-owned** slot trace/update contract (the current unified entry is `pcc_gc_visit_object_slots`, together with root, frame, and native-handle registration), while C is a differential oracle. The G-track in [goal-prompt.md](../../docs/goal/goal-prompt.md) spells out the mechanism: every runtime object type declares its reference slots **once**—strong, weak, borrowed, pinned, movable, native-handle, value-payload-pointer, frame-local, or scheduler-root—and the five backends consume that declaration for reachability, barriers, work buffers, remembered sets, promotion, forwarding, and slot updates.

### 10.3.2 The Honest Current State: the Production Contract Is Now pcc-Python

The [freestanding five-GC production closure](../../docs/goal/evidence/2026-08-03-freestanding-gc-done-strong.md) of 2026-08-03 superseded this section's earlier snapshot. Production collector policy is no longer maintained as two large mirrors in `src/py_gc_backend.c` and `py/py_gc_backend.py`; it is split among `freestanding_gc_*` modules. `pcc_gc_visit_object_slots()` in `freestanding_gc_object_slots.py` is the unified object-slot entry, handling core containers, fixed owners, weak references, continuations, classes, C-extension objects, and instances. Root and frame registries, the common mark cycle, backend-0 collection, incremental/concurrent scheduling, generational promotion, and relocation/ZPage mechanics likewise have named pcc-Python owners.

The C collector source remains, but the production link map contains no C-owned collector definition. Its role is differential oracle, not a second policy owner. The common contract suite; weakref, finalizer, resurrection, suspended-frame, C-extension-root, and relocation gates; five-backend long runs; and current-source GC0–GC4 pcc1→pcc2→pcc3 fixed points jointly bound the claim. A new pointer-bearing type changes the unified pcc-Python slot owner; the C oracle should receive the corresponding differential update but may not re-enter the production archive.

### 10.3.3 Layering: Indexes, Allocation, and Atomics Are Not a Permanent C Kernel

Earlier source treated the hash tables behind `pcc_gc_frame_index_insert`, `pcc_gc_object_index_find`, and the `pcc_gc_forwarding_index_*` family as C-only kernel facilities. Current `freestanding_gc_index_table.py` exports those ABIs and owns open addressing, rehashing, backward-shift deletion, and frame/object/forwarding/ZPage indexes; `src/py_gc_index_table.c` remains a differential oracle. `freestanding_allocator.py` likewise owns allocation policy. Only raw memory, atomics, page/syscall operations, and host-ABI entry remain compiler intrinsics—not hash policy or object-graph semantics. Chapter 14 develops the complete no-C/libc layering.

## 10.4 The Barriers: Who `pcc_gc_store_ptr` and `pcc_gc_load_ptr` Serve

Generated code touches object pointer slots through exactly two functions ([pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c)):

```c
PyObject *pcc_gc_load_ptr(PyObject *owner, PyObject **slot);
void      pcc_gc_store_ptr(PyObject *owner, PyObject **slot, PyObject *value);
```

If you remember only one sentence: the write barrier answers "I just changed an edge; who must know?", and the read barrier answers "I am reading an edge; has its target moved?" Refcounting needs the write side to balance old and new values. Generational and relocating collectors need the write side to record old→young edges. Moving collectors need the read side to turn stale addresses into current addresses. Neither obligation should be scattered through call sites, so pcc funnels object-slot reads and writes through these two functions.

### The write side

For **all** backends, `pcc_gc_store_ptr()` first carries an obligation independent of any GC algorithm: the **balanced refcount contract** — incref the new value, write the slot, decref the old value. Container stores such as `py_list_append` are therefore reference-balanced, and the ownership reasoning of Chapter 9 depends on it. (A correction recorded on 2026-06-10 is the cautionary tale: someone inferred from call sites alone that "`py_list_set` does not incref," designed a sort optimization around that, and leaked references. The lesson, written into the investigation: **read the barrier helper's source before asserting its refcount contract.**)

Before the balanced store, `pcc_gc_note_slot_write_barrier()` (`py_gc_backend.c`) dispatches the write-barrier semantics by backend:

- **#1, incremental tricolor**: if the owner is already black and the new value is white, gray the value — the classic shade-on-store that maintains the tricolor invariant (no black object may point directly at a white one);
- **#2, concurrent mark-sweep**: while marking is active, any stored value not yet gray is grayed and pushed into a thread-local buffered write barrier (`pcc_gc_cms_buffer_gray()`, flushed in batches to the work queue) — the port of Go's buffered write barrier;
- **#3, generational**: only when an **old-generation owner stores a young value**, remember the owner (`pcc_gc_backend3_remember_owner_unlocked()`, object-granularity, setting `PY_FLAG_GC_REMEMBERED`) — the remembered set;
- **#4, colored relocating**: under the same old→young condition, enqueue (owner, slot, value) into a store buffer;
- **#0, refcounting**: the write-barrier portion does nothing; only the balanced store remains.

`pcc_gc_store_root()` is the same contract for global root slots (with a NULL owner, #1/#2/#4 gray the value directly while marking is active).

### The read side

`pcc_gc_load_ptr()` works for **#3 and #4 only**: these backends move objects (#3's minor-heap promotion copies, #4's page evacuation), so a slot may hold a stale address. The read barrier consults the forwarding table (`pcc_gc_note_relocation_read()`); if the object has moved, it returns the new address **and writes it back into the slot** (incref new, decref old) — a self-healing read barrier, so each slot pays the forwarding lookup once. Two variants exist: `pcc_gc_load_borrowed_ptr()` heals the slot without touching refcounts (borrowed semantics), and `pcc_gc_resolve_owned_ptr()` resolves an owned reference already held in a register. On #0/#1/#2 the read barrier degenerates to a plain load.

### Why a violation explodes on some backends only

AGENTS.md warns that a raw write `obj->slot = x` bypassing the barrier "works on backend #0 but breaks #3/#4." That is not because #3/#4 are fragile — it is because #0 happens not to need the information the barrier carries, so the violation goes **undetected** there. One raw write means: #3 never sees an old→young reference (and wrongly reclaims the young object at the next minor collection); #4 leaves behind a stale address that will never self-heal after relocation. The equality contract converts this class of latent violation from "crashes in production someday" into "the five-backend matrix fails now." Every barrier event is also metered (`PCC_GC_COUNTER_WRITE_BARRIERS` / `READ_BARRIERS` and the rest, read through `pcc_gc_telemetry()`), turning "how much barrier cost does this backend actually pay" from an impression into a measurement.

## 10.5 The Root Set: Frame, Continuation, Scheduler, and Extension-State Roots

A tracing collector's correctness begins at the roots. pcc has four root sources, all registered into `py_gc_backend.c` and all consumed by the same functions: `pcc_gc_gray_current_roots()` seeds gray for the tracing backends, and `pcc_gc_visit_runtime_roots()` hands **the same root set** to backend #0's cycle collector (`py_gc_recompute_reachability()` in `py_obj_gc.c` calls it). The root set, too, exists only once — it is the slot contract's counterpart in the root dimension.

Roots are not fields inside ordinary heap objects. They are heap entry points from outside the heap. Current C-stack locals, suspended coroutine frames on the heap, scheduler queues, and C-extension state are not ordinary Python containers, so the collector cannot discover them merely by traversing object slots. They must be registered explicitly. When one root family is missing, the symptom often looks like "an object's interior field was cleared incorrectly," but the true cause is that the entire object chain was never reached from an entry point.

### Frame roots: slot-granularity, non-LIFO, hence a hash

Compiled functions describe their local-variable roots as a **frame map**, format v0 documented in the `py_runtime.h` comment: `frame_map` points at a signed int32 slot count (positive = owning roots, negative = borrowed roots), `slots` points at a contiguous `PyObject *` array; a NULL map means "no roots." The runtime entry points are `pcc_gc_frame_enter()` / `pcc_gc_frame_leave()`, landing in `pcc_gc_note_frame_enter()` / `pcc_gc_note_frame_leave()`: enter allocates a `PccGcFrameNode`, links it into the active-frame list, **and inserts it into the `pcc_gc_frame_index` hash keyed by the slots pointer**; leave removes by key. Which locals enter the frame map is decided by the frontend's ownership lowering — `_ensure_owned_local_gc_root` (in the ownership path of [pcc/frontends/python/codegen/](../../pcc/frontends/python/codegen/)) registers the slots. Section 10.7.2 shows what happens when that half of the contract has a missing corner.

Why a hash and not a stack? Because **frame-root enter/leave happens at slot granularity, not function granularity, and the order is not LIFO**. Codegen emits `pcc_gc_note_frame_leave(slots)` at multiple sites — return paths, tuple printing, owned-local cleanup, unary-call wrappers — so registrations and deregistrations of the same logical frame interleave. A 2026-06 investigation ([docs/investigations/gc-frame-index-entry-pool-perf.md](../../docs/investigations/gc-frame-index-entry-pool-perf.md)) proved it the hard way: replacing the hash with a LIFO shadow stack (linear-scan fallback for non-top frames) regressed gc3's stage2 bootstrap from about 226 seconds to a **900-second timeout** — over a ~300-deep recursive-descent parser, the "fallback" scan became the common path and the structure went O(n²). The hash is O(1) regardless of where the target sits and is the **correct structure**; the real cost (a malloc per frame) was later fixed with an entry pool (Section 10.7.1). The `dup_next` chain in `PccGcFrameNode` handles re-registration of the same slots address, and `pcc_gc_root_slot_count_from_map()` defends against `INT32_MIN` and absurd slot counts. One more detail: `pcc_gc_should_track_frame_roots()` shows backend #0 does not track frame roots by default (refcounting does not need them) and turns them on only when #0 is selected explicitly through `pcc_gc_set_backend()` — one of the few explicit backend differences, and it is a difference in *what information is needed*, not in semantics.

### Continuation roots: suspended coroutine frames

Virtual threads and generators move a suspended function's locals into a heap continuation chunk (`PyContinuationObject`'s `stack_chunk`). Those slots remain roots while the function is off the C stack: `pcc_gc_register_continuation_root()` registers them in the same frame-map format, `pcc_gc_trace_continuation_roots()` grays them during marking, and `pcc_gc_rewrite_continuation_roots()` rewrites stale addresses after relocation. A suspended frame must not only be *seen*; under #4 it must also be *rewritten*.

### Scheduler roots: a queued task is not garbage

A task that is ready but not yet running may be referenced by nothing user-visible — it lives only in a scheduler queue. `PccGcSchedulerQueue` therefore builds root registration into the queue operations themselves: `pcc_gc_scheduler_queue_push()` calls `pcc_gc_scheduler_root_register()` on each entry's value slot, and `pcc_gc_scheduler_queue_pop_into()` resolves any forwarding before unregistering. For as long as the queue holds it, every queued value is a collector-visible root.

### Extension-state roots and the TLS exception root

C extensions hold module state via `PyModuleDef.m_size`, and a `PyObject *` inside that state is a raw C slot — the collector can neither assume it honors barriers nor rewrite it under #4 (it is not a pcc-owned updateable slot). The narrow policy settled by the 2026-05-31 investigation (`gc-5backend-extension-module-state-roots-no-libpython.md`): enumerate those references through the extension's own `m_traverse`, and **pin them before visiting them as roots** — give up moving such objects rather than risk rewriting a raw slot. `pcc_capi_visit_extension_module_state_roots()` is called from both the root-seeding and root-visiting paths. Similarly, the in-flight exception in TLS is a root (and #3 additionally promotes it during minor collection via `pcc_gc_promote_tls_exception_root()`).

## 10.6 No Backend May Win by Weakening

The equality rule carries a reverse constraint that is easy to underrate, stated in AGENTS.md as obligation 6: **none of the five backends may win by weakening finalizers, weakrefs, resurrection, suspended coroutine frames, scheduler queues, C-extension references, or value payloads**. The temptation is real. Finalizers, resurrection, and reentrancy during collection are the hardest corners of any tracing collector, and "this backend does not support resurrection in `__del__`" would make an implementation an order of magnitude simpler and its benchmarks prettier. pcc closes that road: a semantic weakening is not an optimization; it is a failing grade.

The structure that carries these semantics is the tracing sweep's **two-phase (in practice four-step) reclamation**, `pcc_gc_sweep_unreachable()` in `py_gc_backend.c` (port mirror `_sweep_unreachable`). Each step corresponds to a real failure that the contract suite later pinned:

1. **PASS 0 — finalizers before any destruction**: run `py_user_del_dispatch()` on every unreachable object first, while its fields are intact. This aligns with CPython's PEP 442; before it landed, #1–#4 either never ran `__del__` for cycle members at all, or ran it after the fields had been cleared (`gc-5backend-cycle-finalizer-not-run-no-libpython.md`, fixed 2026-05-31). `PY_FLAG_FINALIZED` guarantees at-most-once dispatch so the later dealloc does not re-enter it (resurrection cycles; see Chapter 9).
2. **Resurrection recheck**: a finalizer may store `self` somewhere reachable. `pcc_gc_recheck_reachability_after_finalizers()` re-seeds and re-marks, removing resurrected objects from the sweep candidates — otherwise the sweep clears and frees a live object (`gc-5backend-finalizer-resurrection-no-libpython.md`).
3. **PASS 1 — clear without freeing**: `pcc_gc_clear_unreachable()` first calls `py_weakref_invalidate()` (weak references die with their referent; and note that `_trace_referents` visits only a weakref's callback slot, deliberately not its referent — a weakref must not keep its target alive, which is itself part of the contract), then `pcc_gc_clear_referents()` breaks the cycles; `pcc_gc_clear_slot()` skips the decref of any sibling that is still a sweep candidate.
4. **PASS 2 — free uniformly**: `pcc_gc_finalize_unreachable()` releases the already-cleared objects. Clear and free must be separate phases — interleaving them once gave three backends a use-after-free on the simplest two-node cycle (Section 10.7.3).

The same spirit covers the remaining items. A `gc.collect()` called from inside a finalizer may neither crash nor be banned: the reentrancy guard at the top of `pcc_gc_collect()` (`py_obj.c`) makes a collect-during-collect a no-op, matching CPython's `gc.collecting` semantics (`gc-5backend-reentrant-collect-during-finalizer-no-libpython.md`). A dropped native file handle must be closed and flushed before the wrapper is freed (`PY_TYPE_FILE` gained a type-specific deallocator; `gc-5backend-native-file-handle-lifetime-no-libpython.md`). And value-class pointer payloads under #4 relocation are, at the time of writing, an **open** investigation (`gc-5backend-valueclass-pointer-payload-roots-no-libpython.md`, status active) — stated plainly per claim hygiene: that contract brick is not yet closed on all backends.

## 10.7 History and Lessons

### 10.7.1 The frame-root index: a rejected "optimization" and the real cost behind it (2026-06-04 through 06-10)

**Symptom**: byte-identical bootstrap held on all five backends, but gc3's stage2 took ~226 s and gc4 ~310 s against gc0's ~107 s — a pure performance gap, with sampling pointing at frame-root bookkeeping.

**Wrong hypothesis**: the `frame_index` hash (insert/remove plus a malloc per entry) is too expensive; a LIFO shadow stack will fix it. **Experiment and verdict**: with the shadow stack in place, gc3 regressed to a 900-second timeout — the slot-granular, non-LIFO interleaving of frame enter/leave made the "fallback" linear scan the common path, O(n²) over the ~300-deep recursive-descent parser. The proposal is recorded as DENIED in the investigation and was reverted.

**Real root cause and the right fix**: the cost was never the lookup; it was the per-entry malloc/free. `py_gc_index_table.c` gained a free-list pool for `PccGcPtrIndexEntry` (deep recursion re-enters at similar depths, so entries recycle well): gc3 226→167 s, gc4 310→230 s, with five-backend byte identity preserved.

**The story did not end there**: review found the pool had introduced a data race. The index tables' design contract is "caller holds the GC graph lock," but the identity-index insert reached from `pcc_gc_object_id()` runs unlocked — and now shared one free-list with the locked paths. Plain malloc/free had been internally thread-safe; the pool broke that. Worse, the original "five-backend byte-identical" verification had run single-job, so the threaded path was never exercised. The repair: a `_Thread_local` free-list (no shared state), a `PCC_GC_PTR_INDEX_FREE_CAP` bound so a spike cannot pin RSS at its high-water mark, and `pcc_gc_ptr_index_tls_pool_drain()` on thread exit so a thread-churning long-running service does not leak. The full matrix re-verified green on 2026-06-10.

**Invariants left behind**: frame roots are slot-granular and non-LIFO, so `frame_index` must support O(1) removal at arbitrary positions — no stacks, no linked lists; a performance fix may change the allocation strategy but not the correctness structure; and a correctness claim must state which build configuration verified it (a single-job run proves nothing about thread safety).

### 10.7.2 Exception-referent roots: the root cause was in the frontend, not the runtime (2026-05-31)

**Symptom**: an exception caught by `except` and saved into a local — after `gc.collect()`, `str(saved)` returns `[1, 2, 3]` on #0 but `<null>` on #1–#4. The exception shell survives (refcount keeps it), but its message field has been cleared.

**Two denied hypotheses**: first, a missing slot in the runtime's trace — but `message` is in `_trace_referents`'s exception case and is stored through `pcc_gc_store_ptr`. Second, a missing `py_gc_track` at allocation — adding it changed nothing (`<null>` persisted), and evidence showed backends 1–4 already index every object at `pcc_gc_alloc` time; the edit was reverted under the "no unverified runtime edits left behind" discipline (DENIED). A third inference — a transient refcount-zero during propagation untracking the object — was killed by direct evidence: `pcc_gc_note_object_freeing(exc)` fires only inside collect, never during propagation.

**The decisive discriminator**: bind two locals in the *same* except handler — `s_list = [7, 8, 9]` and `s_exc = e`. After collect, `s_list` survives on all five backends; `s_exc` survives only on #0. Same frame, same handler: the frame-root machinery is fine; **`s_exc` simply is not in the frame map**.

**The real root cause (frontend)**: the only channel by which a local becomes a GC root is ownership lowering's `_ensure_owned_local_gc_root`, which registers a root only when the right-hand side is an owned (new) reference. `s_exc = e` is a borrowed-source copy (the assignment increfs, but the RHS expression is borrowed), so no root is registered; and the source `e` is the except binding, which `exception_lowering.py` materializes as a bare alloca-and-store with no root registration, releasing its retain at handler end. After the handler, nothing roots the exception; the tracing mark sweeps it and clears its message. #0 never consults the tracing list and survives on refcounts alone. The fix landed in the frontend: record except-binding names and root locals assigned from them; `test_exception_roots.py` flipped from xfail to a hard five-backend gate.

**Invariant left behind**: the object-graph contract spans both sides of the compiler — **root registration is the frontend's half of the contract; the runtime only consumes it**. A failure shaped "passes on #0, fails on #1–#4" must not be presumed a runtime bug: the frontend's lowering decides which slots are roots, and #0's refcounting will mask a missing one. The episode is also a demonstration of the investigation workflow — one proposal at a time, run to CONFIRMED or DENIED: two plausible runtime fixes were eliminated by evidence before the real cause surfaced.

### 10.7.3 The object-lifetime contract: one shared-path fix closed three backends (2026-05-31)

The first brick of the contract suite (basic lifetime, a two-node cycle, nested containers, each followed by `gc.collect()`) falsified the then-current "five production-equal backends" claim on its first run: #0/#4 passed; #3 crashed on the **simplest possible cycle** with `[BAD_INCREF] tag=-1`; #1/#2 aborted on the nested-container step. The LLDB backtrace showed a textbook cycle-collector use-after-free: the sweep cleared **and freed** each unreachable object in a single pass, so clearing x's slot decref'd its cycle sibling y to zero and freed it immediately; the sweep then finalized the already-freed y. This is precisely why CPython keeps the unreachable set alive during the clear phase. One fix — the two-phase clear-then-free (PASS 1 clears referents while keeping the sweep-candidate flag, so `pcc_gc_clear_slot` skips decrefs of still-pending siblings; PASS 2 frees) — closed #1, #2, and #3 simultaneously, because all three route `gc.collect` through the same tracing sweep; C and port were mirrored together. The lesson complements the other two stories: a shared path means one bug masquerades as three backends' separate ailments — and one root-caused fix has triple leverage. What made the bug appear deterministically, before bootstrap and in a minimal test, was the contract suite itself.

## 10.8 Summary

The whole point of the five-GC architecture is to **compare collection algorithms without comparing semantics**. pcc separates what may vary from what may not. Algorithms, pacing, and cost profiles differ per backend (Chapter 11); the following five things exist exactly once and are pinned by gates:

1. **The selection ABI**: the `PCC_GC_KIND_*` enum plus `PCC_GC_BACKEND` runtime selection; codegen targets the `pcc_gc_*` surface, and backend internals are invisible to the frontend.
2. **The object-graph rules**: a per-type slot trace/update contract. The target is single-point declaration (`py_obj_visit_slots`); the present reality is several per-type walkers plus mirror discipline plus the contract suite — the gap stated honestly.
3. **The root set**: frame roots (slot-granular, non-LIFO, the `frame_index` hash), continuation roots, scheduler roots, extension-state roots — one root set consumed by the tracing backends and by #0's cycle collector alike; the frontend half of root registration belongs to ownership lowering.
4. **The barrier pair**: `pcc_gc_store_ptr` with its all-backend balanced store plus per-backend write barriers (#1 shading, #2 buffered shading, #3 remembered set, #4 store buffer); `pcc_gc_load_ptr` as the self-healing read barrier for #3/#4.
5. **The equality rule**: identical semantics across all five is a hard requirement; no backend may win by weakening finalizers, weakrefs, resurrection, suspended frames, scheduler queues, or extension references; `DONE_STRONG` requires all five green.

The three case studies each pin one invariant: a data structure must be faithful to the true shape of its access pattern (non-LIFO forbids a stack); root registration is a frontend obligation whose absence #0 will mask; and on a shared path, both bugs and fixes multiply. The next chapter opens the five backends one by one and watches five algorithms walk on this one skeleton.

## Exercises

1. **Read and verify**: read `pcc_gc_store_ptr()` in [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) and write down what it does to the new and old values. Use that to argue that `py_list_append` is reference-balanced, and design (without running) a small program with a `__del__` counter that would verify it under `PCC_GC_BACKEND=0`.
2. **Read and verify**: read `pcc_gc_note_slot_write_barrier()` in `py_gc_backend.c` and state, for each of the five backends, the precise condition under which the function returns early having done nothing. Explain why on #0 it is effectively a no-op, and why that is not a semantic difference.
3. **Format walk-through**: using the frame-map v0 comment in `py_runtime.h`, write out the memory layout of a frame map with two borrowed root slots. Then read `pcc_gc_root_slot_count_from_map()` and explain what each of its two defensive branches (the `INT32_MIN` check and the slot-count cap) protects against.
4. **Mirror audit**: compare `pcc_gc_trace_referents()` in `py_gc_backend.c` with `_trace_referents` in `py_gc_backend.py` for any three type tags, checking that both sides visit the same slot sets (note the port uses raw offsets). If you were adding a new runtime type with two pointer slots, list every mandatory edit point named in this chapter.
5. **Design-tradeoff argument**: the end of [docs/investigations/gc-frame-index-entry-pool-perf.md](../../docs/investigations/gc-frame-index-entry-pool-perf.md) records a design sketch for rewriting the index tables with open addressing. Read the comments about the entry pool and the locking boundary in [pcc/runtime/src/py_gc_index_table.c](../../pcc/runtime/src/py_gc_index_table.c), then argue: which "who holds the lock, who does not" boundary must the rewrite preserve? And why is byte identity (pcc2 == pcc3) *not* a required gate for this change, while the five-backend contract suite *is*?
