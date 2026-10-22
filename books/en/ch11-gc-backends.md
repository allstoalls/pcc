# Chapter 11: The Five Backends — from Refcounting to Relocation

Chapter 10 covered the skeleton the five collectors share: one selection ABI, one slot contract, one root set, one write/read barrier pair, one production equality rule. This chapter opens the five backends one at a time and watches five algorithms walk on that one skeleton. Each backend is examined through the same questions: who is the reference implementation, what is the core algorithm, which decisions shaped the pcc port, which invariants belong to this backend alone, what is its honest current status — and at least one real case study from [docs/investigations/](../../docs/investigations). Every performance number is quoted as a relative magnitude only, with its measurement date and meaning attached; seconds measured under bootstrap workloads quantify "the tax a collector charges a compiler-shaped mutator," not pause behavior.

> **August 2026 status note.** Policy for all five production collectors is now authored in strict freestanding pcc-Python and included in the production archive; C collectors no longer own production symbols. See the claim boundary in [freestanding five-GC production closure](../../docs/goal/evidence/2026-08-03-freestanding-gc-done-strong.md). The C→pcc-Python comparisons and failures retained in this chapter are differential evidence from the migration, not a claim that two production implementations remain today. Algorithmic equivalence, long-running pause/RSS/fragmentation, and the complete fixed point still require their own mode-labeled gates.

## Chapter Overview: Understand Each Backend in One Sentence First

Chapter 10 answered "what information do all five backends share?" This chapter answers "what does each backend do with that same information?" Do not start by memorizing algorithm names; start with this table:

| Backend | One-sentence intuition | The account it cares about most |
|---|---|---|
| #0 refcount-cycle | Free immediately by refcount, then use cycle collection for dead objects that reference each other. | How many owners each object has; whether tracked containers are reachable only from inside a cycle. |
| #1 incremental-tricolor | Avoid scanning the heap all at once; pay allocation debt by marking objects white/gray/black in small steps. | Mark progress, color bits, and whether a black object now points at a white one. |
| #2 concurrent-mark-sweep | Queue #1-like mark work to a background worker, while today's implementation still uses STW slices for correctness. | Work tickets, write-barrier buffers, the graph lock, and thread safety. |
| #3 generational | Assume most objects die young: allocate in a young area and promote survivors to old. | Whether old objects point at young ones; which slots must be eagerly rewritten after promotion. |
| #4 colored-relocating | Allow objects to move, using forwarding tables and read barriers to heal old addresses. | Who is waiting to move, who has moved, which slots have not healed yet, and whether `id()` remains stable. |

Read every backend section through three questions:

1. **What extra accounting happens at allocation and store time?** #1 records colors, #3 records old→young references, #4 records forwarding and store-buffer state.
2. **What kind of work does one `pcc_gc_step()` advance?** #1/#2 advance marking, #3 advances promotion, #4 advances buffer draining, age progression, and page evacuation.
3. **Where does a missing account break semantics?** #0 can double-free, #1 can miss a live object, #3 can leave a stale promoted edge, and #4 can free an old address.

With that reading order, Chapter 11 is not five unrelated algorithm dumps. It is the same object graph maintained under five different cost structures.

## 11.1 One Skeleton, Five Gaits

Claim hygiene first. The file header of [pcc/runtime/src/py_gc_backend.c](../../pcc/runtime/src/py_gc_backend.c) records the backends' origin honestly: the non-refcount backends "start as selectable skeletons: they reuse the refcount semantics while exposing the barrier/safepoint counters that the real Lua/Go/OCaml/ZGC implementations will drive." Two years of slice work have grown real algorithms onto those skeletons — tricolor stepping, a concurrent worker, minor-heap promotion, forwarding and movement — but the status table in [docs/refs_docs/gc-research/README.md](../../docs/refs_docs/gc-research/README.md) still says plainly that they are **not** equivalent algorithmic ports of Lua, Go, OCaml, or ZGC. They are directions converging toward their references, validated slice by slice. This chapter is written in that voice: each section first states what the reference demands, then what pcc implements today and what it deliberately does not.

Source snapshots of all five reference implementations live in the repository ([docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research/) under `<lang>/`), and repository rules require reading the reference before porting, never re-deriving it:

```text
Backend  Algorithm                      Reference        Snapshot contents (excerpt)
#0       refcount + STW cycle collect   python/          gcmodule.c, gc_free_threading.c
#1       incremental tricolor           lua/             lgc.c, lobject.h, lstate.h
#2       concurrent mark-sweep          go-greentea/     mgcmark.go, mgcsweep.go, mwbbuf.go
#3       generational young/old         ocaml/           minor_gc.c, major_gc.h, gc.h
#4       colored relocating / GenZGC    zgc/             pinned pack from jdk-27+21
                                                         (fetched 2026-05-14, hashes in
                                                         MANIFEST.json)
```

The ZGC snapshot is deliberately pinned to OpenJDK `jdk-27+21`: generational mode became ZGC's default in JDK 23 (JEP 474) and the non-generational mode was removed in JDK 24 (JEP 490), so backend #4 must be evaluated against **generational** ZGC, not against a mode that no longer exists.

Before opening the backends, look once at the entry point they share, so the sections need not repeat it. `gc.collect()` lands in `pcc_gc_collect()` in [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c), which runs two entirely different pipelines:

```text
pcc_gc_collect(reason)
  ├─ #0:           py_gc_collect()            (py_obj_gc.c, STW cycle collection)
  └─ #1/#2/#3/#4:  pcc_stop_the_world()
                    pcc_gc_begin_explicit_tracing_collect()
                    loop { pcc_gc_step(1024) } until a step reports no work
                    pcc_gc_collect_tracing()   (sweep, only if candidates exist)
                    pcc_gc_end_explicit_tracing_collect()
                    pcc_resume_world()
```

On the tracing backends, *marking* hides inside the `pcc_gc_step()` loop and *sweeping* lives in `pcc_gc_collect_tracing()` — set a breakpoint in the wrong half and you will conclude that collection never happened. Inside `pcc_gc_step()`, dispatch on `pcc_gc_selected_backend` routes #1/#2 to the tricolor trace step, #3 to the promotion step, and #4 to its drain→aging→evacuation→trace pipeline. Everything that differs between backends grows after that one dispatch point; everything before it (allocation, barrier entry points, root registration) was Chapter 10.

One commonly misunderstood architectural fact deserves stating up front: **reference counting is alive under all five backends.** `pcc_gc_retain()` is `py_incref()`; the body of `pcc_gc_release()` is `py_decref()` (`py_obj.c`; #3/#4 prepend forwarding resolution, and #3 has a dedicated branch for arena-born objects, see 11.5). Count-reaches-zero immediate reclamation is the common path everywhere. The tracing machinery of #1–#4 is layered *on top of* refcounting and is responsible for what refcounting cannot reclaim — cycles. This is not laziness: deterministic finalizer timing and compatibility with the C-extension reference contract depend on prompt reclamation, and replacing refcounting wholesale would change the object-lifetime language. The cost is that every tracing backend must coexist with a parallel reclaimer — the case study in 11.2 shows the sharpest corner of that coexistence.

The hard gate is the same for every backend: [tests/python/gc/](../../tests/python/gc/) contains `test_pcc_bootstrap_full_gc{0..4}.py`, which runs a full stage1→stage2→stage3 self-host bootstrap once per `PCC_GC_BACKEND` value and requires pcc2 and pcc3 to be byte-identical after normalization. Semantics are pinned by Chapter 10's equality contract; every difference discussed in this chapter lives in algorithm, cadence, and cost.

## 11.2 Backend #0: Refcounting plus a Cycle Collector — Reference: CPython

**Reference.** `gc-research/python/gcmodule.c` is CPython 3.13's generational cycle collector (`gc_collect_main`, `visit_decref`, `move_unreachable`); `gc_free_threading.c` is the PEP 703 free-threaded variant, kept as the reference for a future free-threaded path.

**Core algorithm.** Refcounting is the primary collector: `py_decref` frees at zero, and the vast majority of objects never enter any tracing list. The cycle collector works only on tracked containers (`PY_FLAG_GC_TRACKED`, Chapter 9) and lives in [pcc/runtime/src/py_obj_gc.c](../../pcc/runtime/src/py_obj_gc.c):

```c
// pcc/runtime/src/py_obj_gc.c
int64_t py_gc_collect(void) {
    if (py_gc_collecting) return 0;
    py_gc_collecting = 1;
    py_gc_visit_runtime_roots(py_gc_visit_root_cb, NULL);
    py_gc_subtract_child_refs();
    py_gc_mark_reachable();
    int64_t freed = py_gc_sweep_unreachable();
    py_gc_collecting = 0;
    return freed;
}
```

tracked objects hang on the `py_gc_head` list, accelerated by the `py_gc_node_index` pointer hash; the thresholds have CPython's shape (`py_gc_threshold0 = 700` and friends). `py_gc_collect()` reproduces the skeleton of CPython's algorithm: initialize each node's `gc_refs` to its refcount, subtract internal object-graph edges via `py_gc_subtract_child`, treat any node with `gc_refs > 0` as externally referenced, and propagate reachability from those via `py_gc_mark_reachable`. Runtime roots — frame, continuation, scheduler, and extension-state roots — are injected into the same mark via `pcc_gc_visit_runtime_roots()`: this is the #0-side consumer of Chapter 10's "exactly one root set." Then come the finalizer pass, the resurrection recheck, cycle clearing, and release, in the four-step order of Section 10.6.

```c
// pcc/runtime/src/py_gc_backend.c
void pcc_gc_store_ptr(PyObject *owner, PyObject **slot, PyObject *value) {
    if (pcc_gc_selected_backend == PCC_GC_KIND_INCREMENTAL_TRICOLOR) {
        if (owner != NULL && (owner->flags & PY_FLAG_GC_BLACK) &&
            value != NULL && !(value->flags & (PY_FLAG_GC_BLACK | PY_FLAG_GC_GRAY))) {
            value->flags |= PY_FLAG_GC_GRAY;
            pcc_gc_gray_count++;
        }
    }
    PyObject *old = *slot;
    *slot = value;
    if (value != NULL) py_incref(value);
    if (old != NULL) py_decref(old);
}
```

```c
// pcc/runtime/src/py_gc_backend.c
void pcc_gc_step(int64_t work_limit) {
    switch (pcc_gc_selected_backend) {
    case PCC_GC_KIND_INCREMENTAL_TRICOLOR:
        pcc_gc_step_incremental(work_limit);
        break;
    case PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR:
        pcc_gc_step_generational(work_limit);
        break;
    case PCC_GC_KIND_COLORED_RELOCATING:
        pcc_gc_step_relocating(work_limit);
        break;
    default:
        break;
    }
}
```

The same file also hosts the reflective `gc`-module surface — `py_gc_get_objects()`, `py_gc_get_referents()`, `py_gc_get_referrers()` — built on the same per-type referent visitation that the collector itself uses, which keeps the user-visible object graph and the collector's object graph from drifting apart.

**Porting decisions.** Two are worth naming. First, #0 does not track frame roots by default (`pcc_gc_should_track_frame_roots()`): refcounting already keeps locals alive, so paying a per-frame registration cost buys nothing — one of the few explicit backend differences, and a difference in *whether the information is needed*, not in semantics. Second, the cycle collector consumes the **same** runtime root set as the tracing backends rather than building its own, so a missing-root bug of the 10.7.2 kind is at least the same gap on #0 and on #1–#4, not two different gaps.

**Backend-specific invariants.** The cycle collector must never collect a tracked object whose raw refcount is already zero — such an object is owned by an in-flight `py_decref` (see the case study); the `py_gc_collecting` guard makes a reentrant `gc.collect()` a no-op.

**Known status.** Default backend, rollback reference, the broadest production path (an explicit decision recorded in `gc-backend-selection-matrix.md`), and the fastest cell of the five-backend bootstrap matrix. It is not finished: the selection matrix's backlog for #0 lists auto-pacing for cycle collection (today's thresholds are static CPython-shaped numbers) and deeper weakref/finalizer/resurrection policy parity, and every shared runtime edit must keep #0 green as the non-regression reference — the first duty of a reference backend is to stay boringly green.

**Case study: the rc==0 window, and a deadlock behind it (2026-05-29, `pcc1-threaded-explicit-gc-backend0-double-free-highscale.md`).** When the pcc1 threading gate was hardened to "4 workers × 200 iterations, main thread spamming 100 `gc.collect()` calls," the *reference* backend went red: roughly 10 of 12 runs died with SIGABRT, signed either `py_decref: refcount underflow` or a malloc double-free. LLDB showed the crashing thread inside `pcc_gc_release` on a frame-slot reassignment while the main thread was still *waiting* in `pcc_stop_the_world` — the collector was not running at the instant of the crash, so the object had died in an earlier window. Two debug switches in the cycle collector delivered the verdict: `PCC_GC_DEBUG_LEAK_UNREACHABLE=1` (compute reachability, free nothing) produced zero aborts in 12 runs, and `PCC_GC_DEBUG_FREE_TRACE=1` captured the freed object's fingerprint: `rc=0, in_root=0`. Root cause: a worker thread had decremented a refcount to zero in `py_decref` and then parked at a safepoint *before* `py_gc_untrack`, leaving the object in the tracked list with refcount 0; another thread's STW collection judged it unreachable and freed it; the parked worker then resumed and completed its own free — a double free. The fix is a one-line invariant: when recomputing reachability, skip nodes with `refcount <= 0` (genuine cycle garbage always has a positive count, because cycle members reference each other). Fixing the double free exposed a second bug: `Lock.acquire`'s unbounded `pcc_cond_wait` never passes a safepoint, while the lock's releaser was parked *at* a safepoint — stop-the-world could never assemble. The fix is a loop of "timedwait; unlock; safepoint; relock," and the order is non-negotiable: the first attempt took the safepoint while holding the mutex, and the deadlock came back wearing a different hat. With both fixes, the standalone reproducer passed 25/25. The investigation also recorded a discipline lesson from the middle of the chase: editing `py_runtime/src/*.c` does not rebuild the runtime archive automatically, so every instrumentation result gathered before a forced rebuild was void.

## 11.3 Backend #1: Incremental Tricolor — Reference: Lua 5.4

**Reference.** `gc-research/lua/lgc.c` is Lua 5.4's single-threaded incremental tricolor collector: the mark/atomic/sweep state machine, the `GCdebt` work-debt model, the `gcpause`/`gcstepmul` pacer; `lobject.h` shows the header color bits, `lstate.h` the per-state pacer fields.

**Core algorithm.** The three colors live directly in the object-header flags (`PY_FLAG_GC_WHITE`/`GRAY`/`BLACK`, `py_internal.h`). The pacer is a direct port of Lua's model: `pcc_gc_note_alloc()` accumulates allocated bytes into `pcc_gc_debt_bytes`; when debt crosses the threshold (`PCC_GC_DEFAULT_DEBT_THRESHOLD`, 64 KiB, or `live_bytes × (gcpause − 100) / 100` with `PCC_GC_PAUSE` defaulting to 1000), `pcc_gc_maybe_auto_step()` runs one bounded step with a budget computed by `pcc_gc_budget_from_debt()` (debt divided by `PCC_GC_WORK_BYTES = 64`, scaled by `PCC_GC_STEPMUL`, capped at 65536); afterwards `pcc_gc_discharge_debt()` credits the work done back against the debt. The mark step, `pcc_gc_step_trace_cycle_unlocked()`, advances a cursor along the object registry; each gray object has its children grayed via `pcc_gc_trace_referents()` and is itself turned black. When the cursor completes with a zero gray count, the endgame runs: `pcc_gc_finish_tracing_cycle()` rescans the current root set under a stop-the-world boundary, drains any newly grayed objects, and only then stamps remaining white objects with `PY_FLAG_GC_SWEEP_CANDIDATE`. This corresponds to Lua's atomic phase: roots may change during incremental marking, so the final white verdict must be atomic. The write barrier is Dijkstra-style forward: a black owner storing a white value grays the value (Section 10.4).

**Porting decisions.** Three deliberate departures from Lua. First, there is no explicit gray list (Lua's `gray`/`grayagain`): gray is just a flag bit, and the step rescans the object registry via the cursor until the gray count reaches zero — simpler, no list intrusion into the header, at the cost of rescans; the role of `grayagain` is absorbed by the STW endgame rescan. Second, Lua uses a *backward* barrier for tables (`luaC_barrierback_` re-grays a black table); pcc uses the forward barrier uniformly, because all container stores already funnel through one function, `pcc_gc_store_ptr()`, where forward shading is a single test. Third, automatic steps **mark only, never sweep**: the proposal to sweep existing candidates during auto-steps was DENIED in `gc-backend1-auto-step-sweep-debt.md` — at the time, generated code had not yet proven loop-local containers to the tracing root stack, so sweeping meant freeing live objects; even after `gc-backend1-owned-local-frame-roots.md` wired owned locals to one-slot frame-map roots, sweeping still happens only inside the explicit collect's STW window. One honest limitation: `pcc_gc_maybe_auto_step()` is disabled outright when threads are enabled, so under threads #1 advances only on explicit collection.

**Backend-specific invariants.** Fresh allocations carry `PY_FLAG_GC_FRESH_ALLOC` and are treated as black for one cycle during automatic steps (protection against racing unregistered temporaries), but an explicit `gc.collect()` owns a stop-the-world boundary and a stable root set, so fresh objects must participate as white; debt is zeroed whenever the cycle is inactive and unrequested, so silence cannot accumulate into one giant step.

**Known status.** The README status line reads production-test-green — color bits, frame/module/class roots, write barrier, bounded step, sweep, allocation-debt pacing, explicit collect, finalizer/resurrection fixes, and object-registry performance gates all green; all five bootstrap matrix cells pass. The selection matrix lists it as the most realistic near-term non-default challenger.

**Case study one: a port-only double free (`gc-backend1-pcc-py-runtime-collect-abort.md`, the 2026-05-07 audit series).** The first full five-backend bootstrap matrix audit (`bootstrap-five-gc-matrix.md`) produced an awkward table: all 15 cells compiled, and the entire #1 column aborted at runtime — pcc0, pcc1, and pcc2, same allocation-churn-plus-`gc.collect()` probe, all dead before printing the collect result. Meanwhile the C-runtime #1 gates were green. The root cause was one redundant line in the pcc-Python port: the C runtime registers each object into the tracking list once, from `pcc_gc_alloc()`; the port registered it *again* from `py_gc_track()`, so tracked containers had duplicate entries in the object list. When #1's sweep freed an object it removed only one entry; the surviving stale entry later treated freed memory as an object and freed it again. Deleting the duplicate registration fixed it. The lesson feeds Chapter 14's mirroring discipline directly: **a green C gate is not a green port**, and the thing that surfaced the difference before it reached users was precisely the per-backend full matrix.

**Case study two: the protection that ate explicit collect (2026-05-14, the Update in `gc-backend1-explicit-collect-sweep.md`).** After `PY_FLAG_GC_FRESH_ALLOC` landed, #1's production gate regressed in a new shape: the explicit-collect probes printed `False 0`, and every G1 cycle/finalizer test reported zero finalizer calls. Fresh-allocation protection is correct for automatic steps and wrong for explicit collection — an object that is unreachable the moment it is born must still be collectible inside `gc.collect()`. The fix introduced the explicit tracing-collect mode (`pcc_gc_begin_explicit_tracing_collect()` / `pcc_gc_end_explicit_tracing_collect()`): during explicit collection, root seeding treats fresh objects as white. That mode bit later took a second job as the reentrancy guard described in Chapter 10.

## 11.4 Backend #2: Concurrent Mark-Sweep — Reference: Go

**Reference.** `gc-research/go-greentea/`: `mgcmark.go` (concurrent mark workers, work queues, work stealing), `mgcsweep.go` (concurrent sweep, span cache), and `mwbbuf.go` (the per-P 512-entry write-barrier buffer, flushed to GC work queues in batches).

**Core algorithm.** #2 shares #1's tracing core; everything that differs is about *who advances the work, and when*. `pcc_gc_cms_maybe_start_worker()` starts one detached pthread worker (`pcc_gc_cms_worker_main`). Work travels through a bounded 256-slot global ticket queue with two ticket kinds: a positive ticket is an allocation byte count, which the worker converts into a bounded mark budget via `PCC_GC_WORK_BYTES`; a negative ticket encodes a gray object pointer, which the worker traces directly. The write barrier is a scaled-down port of the `mwbbuf.go` pattern: during an active mark, a stored value not yet shaded is grayed, and its gray ticket goes into a 32-entry thread-local buffer (`pcc_gc_cms_buffer_gray()`); the buffer is flushed to the queue in batches **outside the graph lock** (`pcc_gc_cms_flush_wb_buffer()`), so the queue lock is never taken while holding the graph lock. Allocators perform mutator assists (`pcc_gc_cms_maybe_assist()`, the analogue of Go's assist mechanism, counted in `pcc_gc_cms_mutator_assists`) when debt crosses the threshold.

**Porting decisions.** One honest sentence summarizes them: **the concurrency is in the scheduling, not yet in every instant of marking.** Today's worker takes `pcc_stop_the_world()` and then the graph lock for each ticket, so mark slices actually execute inside STW windows. Go-grade "mutator and marker truly run in parallel" requires per-object atomic coloring and a termination protocol; pcc chose to buy a TSan-clean correctness baseline with a global graph lock plus STW slices first, and converge toward the reference slice by slice. In the same spirit, the barrier shades only the **new value** (a Dijkstra insertion barrier) and does not port Go's hybrid barrier (which also shades the overwritten value) — termination correctness is backstopped by the same STW endgame root rescan as #1. After acquiring the lock, the worker re-checks that #2 is still the selected backend: the process can switch backends while the detached worker is alive, and stale tickets must be discarded, not used to touch the object graph. Sweep remains STW; whether it grows into Go-style concurrent span/object sweeping is recorded as an explicit open decision, not a default promise.

**Backend-specific invariants.** Never touch the object graph without the graph lock; lock-wait paths must call `pcc_thread_safepoint()` (a waiting worker must still cooperate with STW handshakes); switching backends stops and joins the worker via `pcc_gc_cms_stop_worker()`.

**Known status.** README: correctness-green threaded prototype. The verdict document `gc-backend2-3-production-verdict.md` concludes it is "production-ready for pcc's current GIL-style threaded runtime — conservative, TSan-clean, with buffered write barriers — but not a literal Go work-buffer/span-sweep clone," and backs the claim with public verdict telemetry (`pcc_gc_backend2_worker_buffer_score()`, `pcc_gc_backend2_production_score()`) that a native harness must move before the gate passes. The selection matrix positions it as a threaded-correctness candidate, not a default candidate; its missing pieces — the full Go-style work-buffer/drain model and a decision on concurrent sweep — are written down as backlog, not glossed over.

**Case study: a detached worker meets an unlocked object graph (`gc-backend2-cms-worker-instability.md`).** The full GC gate kept failing in the same place: `cms_probe.out` exiting with SIGSEGV (−11); a focused rerun exposed a second symptom — worker trace telemetry stuck at zero. Root cause: the detached worker was traversing and coloring the object graph while the mutator concurrently inserted and removed object nodes, with no synchronization at all between them. The fix introduced the runtime graph lock that all tracing backends now share: object registration, object freeing, barrier coloring, mutator steps, and worker tracing all funnel under one lock, with safepoints inside the wait loop. Worth recording is the restraint: the fix did **not** change the queue algorithm — the worker does not requeue tickets on lock contention, so the single-producer/single-consumer assumption was not silently broken. After the fix, the full gate went from crashing to 168 passed, 17 xfailed. The bug's shape — shared mutable structure plus "it'll probably be fine" lock-free concurrency — is textbook; the reason it belongs in this book is the repair path: buy correctness with a lock first, and leave lock-free cleverness to a future that has TSan gates.

## 11.5 Backend #3: A Generational Minor Heap — Reference: OCaml

**Reference.** `gc-research/ocaml/minor_gc.c`: the domain-local minor heap's bump-pointer allocator, forwarding-style promotion in `oldify_one` (write a forwarding pointer into the header, follow `Field(v, 0)`), and the `caml_ref_table` remembered set. `gc.h` and `major_gc.h` give the interface and the major-heap structures.

**Core algorithm.** Header flags carry the two generations, `PY_FLAG_GC_YOUNG` / `PY_FLAG_GC_OLD`; allocation defaults to young. The full data path:

```text
alloc:   pcc_gc_alloc ─→ pcc_gc_try_minor_alloc ─→ [minor arena block, thread-private,
                           (#3 only, size ≤ MINOR_ALLOC_MAX)     bump allocation]
                                                          │ block full
store:   old owner ← young value ─→ remembered set        ▼
                  (owner-granular, PY_FLAG_GC_REMEMBERED)  pcc_gc_minor_collect_reset
                                                                │
promote: scalar whitelist ──copy out of arena──→ malloc'd old gen + forwarding entry
                                                  + eager slot rewrite
         pointer-bearing types ──flip flags in place──→ OLD (never moved)
reclaim: block live_objects hits zero ──→ pcc_gc_minor_release_block (whole block)
```

The minor heap is a real bump arena: `pcc_gc_alloc()` (`py_obj.c`) first tries `pcc_gc_try_minor_alloc()` — only under backend #3, and only for objects no larger than `PCC_GC_MINOR_ALLOC_MAX` (default 16 bytes, 16-byte aligned); blocks are `PCC_GC_MINOR_HEAP_SIZE` (default 32 MiB) and the current block is thread-private (`owner_thread_id`). A full block triggers `pcc_gc_minor_collect_reset()`, which runs one bounded promotion step. The write barrier is the generational classic: an old owner storing a young value puts the *owner* into the remembered set (`pcc_gc_backend3_remember_owner_unlocked()`, setting `PY_FLAG_GC_REMEMBERED`). The promotion step, `pcc_gc_step_generational_promotion()`, runs in a fixed order: promote the four root families (frame, scheduler, TLS exception, extension-module state), drain remembered owners, then sweep the table promoting all young objects. Promotion itself has two routes. `pcc_gc_generational_oldify_copy()` copies **scalar-whitelisted** types (`pcc_gc_relocate_copy_supported_tag()`: int/float/str/complex/bytes/bytearray/CpyHandle — none carry pcc pointer slots) out of the arena into malloc'd old space, installs a forwarding entry, and marks the source inactive; pointer-bearing types are never copied — they are promoted in place by flipping flags. After a copy-promotion, `pcc_gc_promote_owner_referents()` **eagerly rewrites** the owner's slot to the new address (incref new, decref old); the read barrier only backstops the slots the eager pass missed. Arena memory is reclaimed per block: `pcc_gc_note_object_freeing()` does not individually free nodes with `minor_block != NULL`; when a block's `live_objects` counter reaches zero, `pcc_gc_minor_release_block()` frees the whole block. `pcc_gc_release()` has a dedicated skip branch for "arena-born, promoted, refcount already zero" objects — arena memory does not belong to malloc and must not take the ordinary free path.

**Porting decisions.** Three. First, the remembered set is **owner-granular**, not OCaml's slot-granular table: remember "this old object stored a young reference," and revisit all of its slots at scan time — fewer entries, deduplication by one flag bit, at the cost of rescanning width; `gc-backend3-remembered-slot-rewrite.md` later proved the scan must rewrite the very slot it just traced, or runtime code reading raw slots sees the stale address in the window before the read barrier heals it. Second, **eager slot rewrite beats the lazy read barrier**, for the same reason: pcc's C runtime reads container memory directly in many places, and not every read passes through `pcc_gc_load_ptr()`. AGENTS.md hardens this into a rule: the eager rewrite code must live next to the per-type promotion code in `py_gc_backend.c`, never as a parallel path. Third, **only scalars are copied**: moving pointer-bearing objects drags in the full rewrite obligation for every container, root, and suspended frame; #3 leaves that obligation to #4 and sidesteps it with in-place promotion — a clean complexity boundary between the two backends.

**Backend-specific invariants.** Constructors must preserve the young/minor header flags (`gc-backend3-pcc-py-constructor-header-flags.md`); class metadata's borrowed slots (`methods[i].func`, `del_method`) must participate in promotion without being added to #4's generic trace surface (borrowed slots in a tracer mean double counting); suspended generator frames (`PyGenObject.frame` pointing at a heap frame list) participate in the remembered set and rewrite path like any other owner (`gc-backend3-suspended-generator-frame-slot-rewrite.md`).

**Known status.** As of August 2026, strict freestanding pcc-Python is the sole production owner and the C path remains a differential oracle; bounded ownership gates including thread-local arenas are green. Cross-domain remembered-set sharing, broader threaded object-index synchronization, and long-running performance proof remain open. The selection matrix lists it as the medium-term throughput candidate; in the 2026-05-07 audit's allocation-churn probe it was the only non-default backend that actually moved generational telemetry (`minor_collections=18`).

**Case study: a four-byte string, two invariants (`gc-backend-selection-matrix.md` closure section, gates dated 2026-05-17).** During the selection-matrix closure, #3's pcc1 matrix cell crashed inside `IRBuilder.call`: `_opname_of()` sliced a fresh short string `"call"` on every call; that 16-byte-class object landed in the minor arena, yet was stored into long-lived IR metadata — a textbook old→young edge, outside the remembered-set coverage of the day. The recorded fix made `_opname_of()` return stable opcode literals: kill the edge on the compiler side rather than extend runtime coverage on the spot. The same closure pass caught a second gap: class-metadata promotion missed the borrowed `methods[i].func` and `del_method` slots — C runtime and port were then fixed in lockstep (`gc-backend3-class-metadata-slot-rewrite.md`). Both bugs have one shape: **the correctness boundary of a generational collector is exactly the list of "everything that might hold a young reference."** Miss one line of that list and some class of objects dangles after promotion.

## 11.6 Backend #4: Colored Relocating — Reference: GenZGC

**Reference.** `gc-research/zgc/` is a pinned reference pack from OpenJDK `jdk-27+21` (fetched 2026-05-14; `MANIFEST.json` records upstream paths and SHA-256 hashes): `zForwarding*`/`zRelocationSet*` (forwarding table and relocation set), `zBarrier*` (load barrier), `zStoreBarrierBuffer.*`/`zRemembered*` (generational store barrier and remembered sets), `zPage*` (page allocation), `zGeneration*` (young/old generations).

**Core algorithm.** ZGC's goal is to move objects without growing pauses; pcc's #4 port is organized around four parallel ledgers. **The relocation set**: objects selected for evacuation enter `pcc_gc_relocation_set` and are stamped `PY_FLAG_GC_RELOCATION_CANDIDATE`. **The forwarding table**: `pcc_gc_relocate_copy()` copies the payload per type (`pcc_gc_relocate_copy_payload()`, constrained by the `pcc_gc_colored_relocate_copy_supported_tag()` whitelist — list/tuple/dict/set, instances and user classes, functions/iterators/generators/coroutines/continuations, exceptions/classes/weakrefs/threads/tasks, and more, each type's copy code explicitly handling its pointer slots and remembered-set retargeting), then `pcc_gc_install_forwarding()` records from→to in a side table; the read barrier (Section 10.4) heals slots from it. **The stable identity side table**: `pcc_gc_object_id()` assigns an address-decoupled identity on first `id()` use, so `id()` survives movement — identity semantics must not be betrayed by relocation. **The generational slice**: allocation defaults to young (`pcc_gc_note_object_allocated_sized()`); old→young stores enqueue into a store buffer (entries own a reference to the value), drained in bounded batches by `pcc_gc_step_colored_remembered_roots()`; `pcc_gc_step_colored_generation_aging()` flips surviving young objects to old — the analogue of GenZGC's age advance. Carrying all of this are synthetic ZPages: `pcc_gc_backend4_try_zpage_alloc()` bump-allocates inside pages organized by generation and size class (small ≤ 4 KiB, medium ≤ 64 KiB, large), pages keep remembered-slot bitmaps and 512-byte span cards, and evacuation selects candidate pages (`pcc_gc_backend4_select_relocation_pages()`) and drains them (`pcc_gc_backend4_evacuation_page_drain()`). One #4 step inside `pcc_gc_step()` chains this pipeline: drain the store buffer → age the generation → drain evacuation pages → run an STW tracing cycle when requested.

**Porting decisions.** The biggest: **no colored pointers, no multi-mapping.** ZGC encodes color in pointer metadata bits and uses multi-mapped memory for multiple views of the same page; pcc's pointers must pass through the C-extension ABI untouched and must support `is` and `id()`, so the pointer bits are off limits. Color and candidacy live in header flags and side tables instead, and the comment inside `pcc_gc_step()` says it straight: precisely because a side-table candidate flag substitutes for multi-mapping, the tracing cycle's phase transition stays stop-the-world. Second: **the whitelist errs on the side of not moving** — only types whose payload-copy code has been written and tested per type may ever enter the relocation set; pinned objects (`PY_FLAG_GC_PINNED`) reject forwarding installation (counted in `pcc_gc_relocation_pin_rejects`). Third: **a relocation copy is single-use** (`gc-backend4-relocate-copy-single-forward.md`) — an already-forwarded source refuses a second copy, and a successful copy consumes the relocation-set entry; otherwise a second copy of the same source could hijack the forwarding target and duplicate a stable ID.

**Backend-specific invariants.** Every release path must heal through the read barrier before freeing (see the case study); `pcc_gc_backend4_verify_no_old_addresses()` provides a checkable "no stale addresses remain" assertion; the fragmentation score (`pcc_gc_backend4_fragmentation_score()`) is defined as live evacuation debt — pending relocation-set entries plus live forwarding entries awaiting heals — with stable-ID entries explicitly excluded (they are identity metadata, not debt).

**Known status.** The README status line for #4 is the longest and the most honest: forwarding, read barriers, container relocation, scheduler-root healing, the generational slice, and page-class telemetry are production-facing, but the true GenZGC young/old policy, real page-evacuation-driven fragmentation policy, native-handle (Thread/File) relocation protocols, and the port's threaded mirror flushing remain open. The selection matrix's verdict: the long-term low-pause candidate, not a default because it is the most complex and the least finished. It is also the slowest cell of the bootstrap matrix (see 11.7).

**Case study one: the teardown path that skipped the barrier (`gc-backend4-scheduler-queue-free-read-barrier.md`).** The scheduler queue's pop path dutifully reads entry values through `pcc_gc_load_ptr()`; but **destroying** a queue with un-popped entries released them via `pcc_gc_store_root(..., NULL)` on the raw slot — if a queued object had already been relocated, the free path released the stale source pointer. The probe printed `1, 1, 0`: pop healed, free performed zero barrier forwards. The fix makes `pcc_gc_scheduler_queue_entry_free()` mirror pop: while the entry is still registered as a scheduler root, load the value through the read barrier, then unregister and clear the slot. The generalizable invariant: **for a moving collector, a free is also a read** — every teardown path must pass the barrier exactly like the hot paths do, and teardown paths are precisely the ones tests exercise least.

**Case study two: the "read-barrier tax" hypothesis, denied by sampling (2026-06-10, an Update in `gc-frame-index-entry-pool-perf.md`).** gc4 had long been the slowest matrix cell, and the circulating explanation was "the read-barrier tax over frontend work." Seventy sampling captures over a gc4 stage2 codegen window formally recorded that hypothesis as DENIED: `pcc_gc_load_ptr` self-samples were about 3.7% of non-wait CPU, while GC helpers totaled about 41% — dominated by **index-table maintenance** (object/ptr/frame index inserts and finds, about 27%): the per-allocation, per-trace bookkeeping that keeps every object "ready to move" is gc4's real tax base. The low-risk subset that followed (single-pass hashing on the insert paths plus tightening the load factor) took the gc4 single-file bootstrap gate from 148.31s to 121.07s in a same-day back-to-back measurement (−18.4%, 2026-06-10). The lesson is written into the investigation: optimization decisions above the half-second scale must rest on sampling evidence, not on a plausible narrative — which is why every performance number in this chapter carries a date and a statement of what it measures.

## 11.7 The Selection Matrix: Why the Default Is Still #0

`gc-backend-selection-matrix.md` closes "choose the default" as an explicit decision rather than a deferral: **#0 stays the default.** The full ordering: #0 default (the reference path, the broadest real bootstrap and language coverage, the least policy uncertainty); #1 the best near-term non-default candidate (the simplest non-refcount algorithmic surface); #3 the medium-term throughput candidate (arenas, rewrites, and root coverage all dual-tracked in C and the port); #2 a threaded-correctness candidate, not a default; #4 the long-term low-pause candidate, too complex and incomplete for a default. Rollback is simply unsetting `PCC_GC_BACKEND` or setting it to 0; any backend work that touches shared runtime code must keep the #0 gates green; if a future winner changes the default, CI must run both the new default and the #0 reference gates for at least one release cycle.

The numbers, under claim hygiene, as relative magnitudes only:

- **Allocation-churn probe** (2026-05-07 audit; a pcc2-compiled 200k-allocation program with explicit `gc.collect()`, median of three runs): #0 0.146s baseline; #1 1.11×; #4 1.08×; #3 1.60×; #2 1.79×. Meaning is limited: at default thresholds #4 triggered no relocation at all (its 1.08× is the cost of bookkeeping without work), and #2's 1.79× includes assist and queue overhead; this is a single probe, not a throughput portrait.
- **Bootstrap matrix** (single-job stage2, 2026-06-04 baseline): gc0 ≈ 107s; gc3 226s; gc4 310s. After pooling the index entries (same investigation, verified 2026-06-04): gc3 167s, gc4 230s (≈ −26% each); after single-pass inserts and the load-factor change (2026-06-10, same-day back-to-back): gc4's single-file gate 148.31s → 121.07s. The full matrix (five backends × three stages) varies between 426 and 520 seconds of wall clock within a single day, making cross-day comparison meaningless — the investigation explicitly retired a fuzzy "directory under 200s" target in favor of two comparable metrics: matrix wall clock and per-backend stage2 `compile_python_total`, both measured same-day back-to-back.
- The **meaning** of bootstrap numbers: deep recursion, masses of short-lived objects, frame-root registration on every call — they measure how much tax the collector charges the mutator, not pause distribution or fragmentation over time; those belong to the G-track long-running benchmarks, still under construction as of this writing.

An easily-missed output of the matrix: it is itself an instrument. The first full run of the 15 cells (3 compiler stages × 5 backends) caught #1's port-only crash on the spot (11.3); one bootstrap file per backend means any runtime regression names exactly which backend's stage goes red first. The telemetry columns of the 2026-05-07 audit also show what "the same probe, five backends" buys diagnostically: on a 20k-allocation matrix probe, #0 reported zero tracing steps (as designed), #1 seven bounded steps against a debt of 120, #2 eighty-one steps with visible assist work, #3 twenty steps with minor-arena activity, and #4 one step with no relocation — five different cost signatures from one program, each checkable against what the backend's algorithm is supposed to do at default thresholds. When a backend's signature changes shape without a corresponding code change, that is a regression telling on itself.

Finally, connect the status vocabulary to the backends. The equality contract's vocabulary (Section 10.1) says a feature touching objects, references, or lifetime is `DONE_STRONG` only when it passes the common contract under all five backends, `DONE_WEAK` if only #0 passes, `BACKEND_PARTIAL` in between. That vocabulary and this chapter's backend statuses are two orthogonal axes: the former describes a *feature's* coverage across the five backends; the latter describes a *backend's* distance from its reference. A feature can be `DONE_STRONG` (say, finalizer ordering on a two-node cycle, green across the contract suite) while #2 remains a prototype; conversely, #0 is the production default while the "value-class pointer payloads under #4" contract brick is still an active open investigation at the time of writing. Conflating the two axes is the most common claim-hygiene accident in GC status discussions.

## 11.8 History and Lessons

The backend sections already carried their local stories. To keep this chapter's history section explicit, this section pulls two cross-backend investigations back into named case studies.

### Case study one: the matrix as a differential detector

The matrix exposes a difference; the root cause is usually on one side, outside the shared layer. The port-only double free of 11.3 (C green, port red), the missing exception roots of Section 10.7.2 (#0 green, #1–#4 red), the `_opname_of` short string of 11.5 (only #3 red) — all three failures had the shape "some backends red," and the root causes lived in the port mirror, in frontend ownership lowering, and in compiler metadata lifetime respectively. None lived in the algorithm of the backend that turned red. That is the diagnostic value of the equality contract: the five backends are five detectors with different sensitivities, and the pattern of which cells are red and which are green is itself a fingerprint of the root cause. The investigation discipline — one proposal at a time, run to a CONFIRMED or DENIED verdict — is what reads the fingerprint correctly.

### Case study two: performance hypotheses need an evidence grade

Every performance step needs an evidence grade. The three performance interventions cited in this chapter trace one complete methodological arc. Replacing the frame-index hash with a LIFO shadow stack, on intuition — DENIED, gc3 regressed to a 900-second timeout (the access pattern is slot-granular and non-LIFO, Section 10.7.1). "The read barrier is gc4's main cost," on folklore — DENIED, sampling showed the real tax base was index maintenance (11.6). Pooling index entries and single-pass inserts, on sampling evidence — CONFIRMED, and each time accepted only with five-backend byte identity (pcc2 == pcc3) plus same-day back-to-back timing. The same week added a correction extending the discipline to correctness claims (2026-06-10, same investigation file): someone inferred from call sites alone that `py_list_set` does not incref, designed a "zero refcount traffic" sort on that premise, and a `__del__`-counting probe showed one leaked reference per element — **read the barrier helper's source before asserting its contract.** Performance and correctness obey the same rule: evidence first, conclusions second.

## 11.9 Summary

The five backends are five cost structures under one semantic contract:

1. **#0** — refcounting plus an STW cycle collector (CPython-shaped): deterministic reclamation, minimal bookkeeping, the default and the rollback reference; the cost is that cycles need a collector and pauses grow with the tracked set.
2. **#1** — incremental tricolor (Lua-shaped): bounded steps paced by allocation debt, a forward write barrier, an STW endgame verdict; automatic steps mark only, sweeping belongs to explicit collection.
3. **#2** — concurrent mark-sweep (Go-direction): a detached worker, a bounded ticket queue, a thread-local buffered write barrier, mutator assists; today's mark slices still run under STW windows and the graph lock — the concurrency is in the scheduling, and the reference's instant-by-instant concurrency is the unfinished direction.
4. **#3** — a generational minor heap (OCaml-shaped): thread-private bump arenas, an owner-granular remembered set, scalar copy-promotion plus in-place promotion for pointer-bearing types, eager slot rewrites; arena memory reclaimed per block.
5. **#4** — colored relocating (GenZGC-direction): a relocation set, a forwarding side table, a self-healing read barrier, a stable-ID side table, a store-buffer generational slice, synthetic ZPage evacuation; side tables in place of colored pointers, a whitelist in place of universal movement, STW phase transitions in place of multi-mapping.

The default is #0 — a recorded, explicit decision with a rollback policy and a ranked list of challengers. Every backend's status word is mode-labeled: production-test-green, threaded prototype, production-facing focused gates, advanced surface — each phrase maps to an auditable row of gates in the README and the selection matrix, which is exactly Chapter 1's promise that every claim states what it proves and what it does not.

## Exercises

1. **Verify against source**: read `pcc_gc_step()` in `py_gc_backend.c` and write down, for each of the five backends, what one step does (what does #0 do in this function, and why?). Check your list against Sections 11.2–11.6.
2. **Reference comparison**: read `luaC_step` and the `GCdebt` arithmetic in `gc-research/lua/lgc.c`, compare them with `pcc_gc_budget_from_debt()` and `pcc_gc_discharge_debt()`, identify two deliberate simplifications in pcc's pacer, and argue whether each one's cost is visible under the bootstrap workload.
3. **Verify against source**: read `pcc_gc_generational_oldify_copy()` and explain: (a) why `to_h->refcount` is set to 1 before the payload copy and back to 0 afterwards; (b) why `PY_FLAG_GC_PINNED` is rejected outright; (c) after the source is marked by `pcc_gc_mark_forwarded_source_inactive()`, when and by whom its arena memory is actually released.
4. **Whitelist audit**: compute the set difference between `pcc_gc_relocate_copy_supported_tag()` and `pcc_gc_colored_relocate_copy_supported_tag()`, pick three types present only in the latter, find their copy branches in `pcc_gc_relocate_copy_payload()`, and list what each branch does beyond `memcpy` to move pointer slots safely (reference counts, remembered-set retargeting, index updates).
5. **Design-tradeoff argument**: Section 11.4 says #2's "concurrency is in the scheduling, not in every instant of marking." Suppose the per-ticket STW in the worker were removed: list at least three mechanisms that must be added (hints: atomicity of coloring, a termination protocol, ownership of the `pcc_gc_trace_cursor` shared with #1), and argue whether Go's hybrid barrier or pcc's existing STW endgame root rescan is the better first step, and why.
