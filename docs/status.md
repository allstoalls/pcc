# PCC current status

Updated 2026-10-22. Source: `ikshengmin/pcc` master (`shenming` remote).
Durable receipts for this period (patches, profiles, A/B scripts, repros):
`/Users/jiamo/pcc-recovery-archive/2026-10-13/README.md`; the 2026-10-21
handoff is `/Users/jiamo/pcc-recovery-archive/2026-10-21/README.md`. The 2026-10-07 status
(source preservation, the remote agent's backups) is in git history at
`38357808:docs/status.md`. All ten goals below remain open.

## P0 threaded runtime: threading locks and virtual-thread locks (2026-10-22, Claude)

Plan: `docs/plans/p0-threaded-concurrent-gc-runtime.md`. This change makes
the threading primitives follow CPython 3.15 on the threaded runtime and
blocks waits in the exited state:

- Lock/RLock `acquire(blocking, timeout)`, `locked()`, Event
  `wait(timeout)`, Semaphore and BoundedSemaphore `acquire(blocking,
  timeout)`/`release(n)` and Thread `join(timeout)` convert their arguments
  with CPython's rules (`_PyTime_FromSecondsObject`, ROUND_TIMEOUT) and raise
  its errors. The static lowering (`native_threading.py`) and the runtime
  bound methods (`py_obj_ops_dispatch.py`, through the native signature
  binder) call the same `py_threading_*_args` entries.
- The waits are exited kernel condition waits: no 5 ms polling, a notify
  wakes at once. Joins block on the handle's condition variable instead of
  spinning (`pcc_thread_join`), and `time.sleep` runs exited
  (`pcc_thread_blocking_enter/leave`), so a collection does not wait for a
  sleeping thread.
- An RLock belongs to a virtual thread, not its carrier
  (`PyVirtualThreadObject.identity`); `RLock.acquire()` in a resumable frame
  parks and the release hands the lock over. A release skips a parked waiter
  that can no longer resume.
- Runtime bound methods of locks no longer leak a receiver reference per
  call, and report release errors; they pin receiver and captures across
  their allocations, like `_py_sync_method_bound`.

Tests: `tests/python/test_native_threading_lock_semantics.py` (API program
and four-thread contention program, byte-equal to CPython),
`tests/python/test_virtual_thread_lock_mechanisms.py` (three carriers),
`tests/python/test_threading_condition_stw.py` (the oracle now models exited
kernel waits).

GC4 with several threads (fixed on top of 6cc663bd4). Two zpage races
handed one block to two live objects or dropped live objects from GC4's
accounting:
`pcc_gc_backend4_zpage_register_owner_payload_span` (and unregister,
retarget) moved a page's bump offset (page+64) and used bytes without the
object-graph lock, so a list/dict/set constructor and another thread's
zpage allocation claimed one offset and the span's tail rewind gave the
live block to a third allocation; and a zpage death's delayed freeing note
ran when `pcc_gc_pointer_is_managed(o)` still held after the deallocator,
which in a threaded program could be another thread's new object at the
freed address (noted freeing: never released, so its subgraph's finalizers
were lost, or its block reused while live). The span entry points now take
the re-entrant graph lock; `_py_decref_finish`, `py_instance_dealloc` and
`pcc_gc_tracing_finalize_unreachable` record whether the death noted the
object (`pcc_gc_free_watch_begin/end`) and probe the address only when it
did not. Four threads, `PCC_GC_BACKEND=4`: private `[i, i]` churn died
with [BAD_INCREF] in most runs on 6cc663bd4 and is 10 of 10 exact with the
span fix; with the span fix alone, 400-deep `__del__`/list chains were
wrong in 4 and 5 of 6 runs and `with lock:` in resumable functions in 6 of
6; with both fixes each is 8 of 8 exact, as is the lock contention program.
Tests:
`tests/python/test_gc4_threaded_payload_spans.py` (new: fails with
[BAD_INCREF] on 6cc663bd4), the GC4 cases of
`test_threaded_dealloc_cascades.py` and `test_native_threading_lock_semantics.py`
(no longer xfail).

GC collector thread (GC1/GC2) and the GC substrate suite. The collector
thread that d58306406 added for GC1 advanced any open cycle and ignored
`gc.disable()`; it now pauses while collection is disabled (explicit
`gc.collect()`/`pcc_gc_step` still run), and so do mutator assists.
`pcc_gc_set_backend` joined it only when leaving GC2 and counted it as a
mutator, so a one-thread program could not switch from GC1 to GC3/GC4;
leaving GC1 or GC2 now joins it before the single-mutator check (restarting
it if the switch is refused). Tests:
`tests/python/test_gc_background_collector_honors_disable.py`,
`tests/python/test_gc_collector_thread_backend_switch.py` (both fail on
82070e303). In `test_gc_threading_substrate.py` every node that passed at
195cba989 (before the P0 series) passes again: the two tracing-window probes
that hung now disable automatic collection, and the contracts that named
the threads-off kernel, `pcc_thread_safepoint`, the plain color store or
`pcc_cond_wait` follow the threaded-only runtime. Still failing there, as at
195cba989: the scheduler-queue and C root-store contracts (4), the backedge
safepoint shape, the colored relocation selector, generational referent
promotion, set add/remove/contains (4), strict cext decref, colored
remembered drain, collect-during-update/delete under all five collectors
(the update callback's collect sweeps nothing) and the backend-4 dict update
snapshot (the probe exits -11). Also failing: `test_gc_backend_generational`
(owner-slot walker contract, minor bump arena run) and
`test_gc_granule_map::test_granule_grow_preserves_all_sixteen_keys_per_slab`
(every slab registration refused); not yet compared with 195cba989.

GC0 automatic cycle collection. GC0 (the default backend) collected cycles
only on an explicit `gc.collect()`. Now `py_gc_track` schedules a collection
once tracking has grown by max(threshold0, survivors of the last
collection) and, as CPython 3.12+ does at its eval breaker, the next
application gate of compiled code runs it (`pcc_gc_safepoint`, through the
automatic-step flag the gates already poll); `gc.disable()` and
`gc.set_threshold(0)` keep it off. The default threshold is CPython 3.15's
(2000, 10, 10). GC0 marked reachable objects recursively, so a collection
with a 50,000-deep live list overflowed the C stack (also on 82070e303); it
now marks with an explicit worklist. With 2,000,000 self-referencing
`__del__` objects in a loop, GC0 now finalizes 1,999,995 before the explicit
collect and peaks at 5.7 MB (2.2 GB before; CPython 18 MB); it still takes
18 s against CPython's 0.3 s, almost all in finalizer dispatch. Tests:
`tests/python/test_gc0_automatic_cycle_collection.py`,
`tests/python/test_gc0_deep_structure_collect.py` (both fail on 82070e303).

Backend resets and the collector thread. Since bcdd222c2 a backend switch
owns the collector-thread lifecycle pause (joining a running thread), so it
is refused inside no-park scopes, graph-lock scopes and while owning the
stopped world. A same-backend reset of GC1 or GC2 no longer takes that
pause: the thread keeps running, the graph lock orders the reset against
its batches and the cycle epoch retires a stale finish claim, so a bounded
no-park scope may reset again (the tracing finish-claim probe's window 2,
which returned 19 after bcdd222c2, passes). Such a reset is refused when a
concurrent switch changed the backend first and when the collector thread
itself calls it. Tests: `tests/python/test_gc_cms_worker_lifecycle.py`
(same-backend reset, lost race, collector self-reset).

The kqueue/epoll kernel wait of the vthread IO waitset
(`pcc_io_waitset_wait_block`) now runs exited, as condition waits, joins and
`time.sleep` do. It can block forever, and a stop-the-world waited for the
carrier inside it: with the GC1/GC2 collector thread seeding a cycle, the
gc_production_contract vthread IO waitset probe deadlocked intermittently
(also on 82070e303). Test: `tests/python/test_vthread_io_wait_runs_exited.py`
(hangs on 82070e303).

GC1..GC4 automatic cycle reclamation. GC1..GC4 reclaimed cyclic garbage
only on an explicit `gc.collect()`: their automatic work only marked
(docs/investigations/gc-backend1-auto-step-sweep-debt.md, now corrected),
and GC1/GC2's collector thread began one marking cycle after another. The
reclamation now runs where compiled code reaches an application gate
(`pcc_gc_safepoint`), with the world stopped as for an explicit collect, one
cycle at a time: GC1/GC2's collector thread begins a cycle once debt reaches
the pacing threshold or tracking has grown by max(threshold0, survivors),
never while a completed mark waits for its sweep, and the next gate sweeps
each mark it completes; GC3 runs one cycle from tracking growth, as GC0
does; GC4's automatic step continues a mark to completion within its budget
and the gate sweeps it. `gc.disable()` and `gc.set_threshold(0)` stop all of
it. The pacing threshold applies the pause to the bytes the last mark found
reachable (`pcc_gc_tracing_marked_bytes`), not to the current heap with its
garbage (with the default pause of 1000, GC4 stopped stepping for good), and
a completed cycle pays the debt. GC4's own evacuation relocates only objects
a mark has reached (it copied churned garbage, which its forwarding holds
kept alive), and an explicit GC4 collect marks again after retiring
forwarding edges (it left 4,006 of 50,000 such objects unfinalized). With
2,000,000 self-referencing `__del__` objects, finalized before the explicit
collect, loop time and peak RSS: GC0 1,999,945, 19.5 s, 7.4 MB; GC1
1,944,220, 49 s, 105 MB; GC2 1,951,986, 47 s, 106 MB; GC3 1,999,945, 47 s,
9.2 MB; GC4 1,999,516, 61 s, 606 MB; CPython 0.29 s, 18 MB (before: GC1 and
GC2 over 300 s, GC3 2.9 GB, GC4 4.3 GB). Tests:
`tests/python/test_tracing_gc_automatic_cycle_collection.py` (fails on
2ec86d297), the pacing cases of `test_freestanding_gc_public_collection.py`
and `test_gc_backend_concurrent.py`, `test_gc4_idle_step_debt.py`; five GC4
probes that relocate through the automatic path now first mark their objects,
the state a completed mark leaves. Stale contracts retargeted on the way:
`test_freestanding_gc_barrier_dispatcher.py`'s closure pin (missed
095b286f0's gate-rule externs), the decref death pin in
`test_gc_backend4_production.py` (809d6bf67's atomic or), and
`test_gc_byte_accounting_i64.py`, whose probe returned before any check on
every threaded runtime.

Open:

- Automatic cycle reclamation, what remains: finalizer dispatch dominates
  the time on every backend (about 8 us per `__del__` through
  `py_user_del_dispatch` -> `py_func_call_kwargs`); GC4 peaks at 606 MB on
  the 2,000,000-object loop although it finalizes 1,999,516 automatically
  (not investigated: reclaimed zpage memory is not reused); a GC4 object
  relocated and then dropped keeps its forwarding hold until the edge
  retires, which automatic steps do only when idle; the default pause of
  1000 (the next cycle after nine times the marked heap) only now takes
  effect, unmeasured against other values. The tracing collectors take more
  than a minute (GC1, GC2, GC4) and 30 s (GC3) on a program that builds a
  50,000-deep linked list and a 50,000-deep nested list
  (`tests/python/test_gc0_deep_structure_collect.py`'s program), as on
  82070e303; not remeasured.
- Classes created at run time are never collected: the untracked
  `tests/python/test_native_class_collection.py` (the five-GC run
  c91b89809 left open) creates and drops 300 local classes; CPython collects
  300, pcc 0 on every collector. Cause: `py_class_new` marks every class
  `PY_FLAG_IMMORTAL`, the 2026-07-03 fix of
  docs/investigations/pcc1-tuple-unpack-self-host-str-counter-corruption.md
  (instances hold an uncounted class pointer at offset 16, and a frontend
  over-release had freed pcc1's codegen class). Planned fix, CPython's
  ownership: classes mortal and GC-tracked; `py_instance_new` takes a counted
  reference to the class (valueboxes too, routed to `py_instance_dealloc`),
  released last in `py_instance_dealloc`; a class owns its bases, released
  in `py_class_dealloc`; the MRO array becomes role-3 metadata (GC0 and the
  tracing trial deletion subtract role-2 edges, so every role-2 edge must be
  counted); the force-free loops of `py_gc_collect` and the tracing sweep's
  PASS 2 skip classes, which die by refcount once their holders do; the
  runtime's cached builtin type classes (`pcc_type_cls_*`, most of about 44
  `py_class_new` sites) become explicitly immortal; then user type tags
  (104..65535, never reused, so a process can create at most about 65,000
  classes) are recycled. Exceptions already own their class. Risk: class
  reference imbalances that immortality hid (the July over-release) become
  use-after-free; it needs the pcc1 self-host gate.
- Failing on 82070e303 as well, not addressed:
  `test_freestanding_gc_tracking::test_production_archive_uniquely_owns_tracking_gc0_to_gc4`
  (with every runtime threaded, GC4 skips `py_gc_track`, so its harness
  sees nothing tracked; check `gc.is_tracked` under GC4) and
  `::test_production_archive_tracking_lock_survives_real_pthread_contention`
  (times out after 30 s); `test_gc_effectiveness::test_generator_referencing_self_collected`
  expects `gc.collect() > 0` where CPython 3.15 prints `False` too;
  `test_py_io_waitset::test_live_waitset_retries_eintr_against_absolute_deadline`
  (probe exits 16) and `tests/vthread/test_io_waitset_mirror.py::test_c_abi_io_waitset_semantics_and_capability`
  ("kq-timeout").
- Failing on 2ec86d297 as well, not addressed:
  `test_gc_backend_relocating::test_colored_relocating_selects_unpinned_relocation_set`
  (a second explicit selection takes two objects created by the prints in
  between), `test_gc_backend4_production::test_backend4_thread_start_loads_forwarded_args_slot`
  (exits 12: the started thread's args slot is not the moved tuple) and
  `gc_production_contract/test_valueclass_pointer_payload.py[4]` (the box is
  not in the explicitly selected relocation set). Also failing:
  `gc_production_contract/test_extension_module_state_roots.py` (self link
  refuses native-extension export anchors),
  `test_gc_backend_concurrent::test_concurrent_backend_nonthread_barrier_stores_drain_to_black`
  (needs a threads-off runtime, which no longer exists) and
  `test_gc_byte_accounting_i64::test_native_gc12_byte_counters_cross_32_bit_boundaries`:
  with its guard fixed, the debt and threshold checks pass, but a fresh
  `py_list_new(0)` on GC1/GC2 adds 40 bytes to `pcc_gc_live_bytes` while
  `pcc_gc_object_known_size` reports 0 and its release subtracts nothing.
- `with lock:` in a resumable function enters through `py_context_enter`,
  which blocks its carrier instead of parking; a parking call nested in a
  larger expression (`items.append(lock.acquire())`) fails the self IR
  verifier with an SSA-dominance error (the `vt.join() + 1` class).
- `Condition(lock)` still ignores its lock and the default Condition lock is
  not reentrant; Condition ownership errors, `notify(n)`, `wait_for`,
  `Barrier`, `Timer`, `threading.local` attributes and a `current_thread()`
  object remain to do. `time.sleep` in a virtual thread blocks its carrier.
- Object layouts (for example `PyVirtualThreadObject`) are still defined by
  `pcc/runtime/src/py_internal.h`, and `scripts/gen_port_abi_constants.py`
  reads them with the host `cc`; the layout authority should move to a
  pcc-owned spec.

## IDE Python source-debug objects (in progress)

- Starting source: `fac3c11af` from `shenming/master`. The owned object route
  rejected a debug-bearing return as SSA text (`%sum, !dbg !4`), while the
  same ordinary function emitted a Mach-O object. Existing Python `-g`
  tests used clang for their final DWARF object oracle.
- `self_backend_debug.py` now preserves original function/file/line/column
  metadata separately from semantic operands, with lexical scopes, cached
  identities, dense reachable-block mapping and multi-line switch locations.
  Original filename/directory now come from the selected parser input or
  original spans, rather than an import module name without its `.py` suffix.
- Own checks: 28 focused host-PCC/frontend/parser/owned-object regressions pass.
  This is **not native PCC1 debugging acceptance**. The installed compiler
  still points at a v84 baseline; no current-source bootstrap, native debugger
  session or full GC gate is claimed. The owned lifter currently has line-only
  spans, so precise token columns remain open.
- Indexed transport now retains the original metadata graph/IDs and validates
  each function/block/instruction binding. Debug modules use schema v2 so v1
  readers reject them; ordinary v1 payloads stay unchanged. 33 focused host
  checks pass, including byte-identical debug round trips and corrupted binding
  refusals. Receipt: `/Users/jiamo/pcc-recovery-archive/2026-10-21/ide-debug-codec-69bv8ogd/README.md`.
- AArch64 Darwin now emits owned DWARF4 function/line sections bound to final
  assembler offsets. Structured/text and indexed-cache routes retain identical
  code/metadata; packed addresses reuse local section relocations. Debug modules
  disable code-moving target optimizations; other native targets still refuse.
  58 focused checks pass. LLDB resolves a real Python frontend object source
  breakpoint and maps its address back to the original file/line. This is an
  object inspection, not a running native breakpoint. Source/artifact/consumer
  evidence: `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-dwarf-2pgwiavx/README.md`.
- Ordinary and compact/staged merges now rebase original DWARF4 section offsets
  and retain local code-address relocations. Two/three-source and repeated
  partial links resolve the correct files/addresses in dwarfdump and LLDB.
  62 focused checks pass, including prior descriptor-retirement and ordinary
  relocation regressions. Receipt: `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-link-sg4yqjss/README.md`.
- Owned executable output now keeps a read-only DWARF segment with resolved
  code addresses, valid BSS/file layout and owned signing. Debug leaf functions
  retain FP/LR frames. 64 focused checks pass: native scalar IR fixtures hit
  source breakpoints, step over and show caller frames, then exit with the
  expected result. Ordinary/windowed/staged output bytes match. This backend
  fixture is not a real Python-frontend/native-PCC1 integration; LLDB also warns
  that Python variable inspection lacks a language plugin. Receipt:
  `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-executable-qbzg7kp6/README.md`.
- The first real Python integration exposed application `-g` leaking into the
  shared runtime's owned build, where runtime-only optimization corrupted PHI
  operands with `!dbg`. The owned builder now matches run_runtime_make's scoped
  option isolation and restores application debug settings on success/failure.
  104 checks pass, 1 deselected; a real runtime member emits without debug and
  the application still emits DWARF. Full integration replay remains pending.
  Receipt: `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-runtime-policy-7_1g4v95/README.md`.
- The next real Python replay passed runtime construction and exposed the
  in-process emitter leaving an unknown IR target after selecting its host
  backend. It now reuses the existing selected-target normalization before
  backend admission. Explicit targets stay unchanged; absent declarations
  still refuse. 70 focused checks pass. Receipt:
  `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-target-z4of_fdx/README.md`.
- Real Python integration now passes at compiler source `d6adf16e5`: the
  unchanged add/print program compiles through host PCC's owned backend/linker
  with libpython off, and GC0-GC4 all match CPython (`7`, exit 0). LLDB launches
  the actual native artifact, hits source line 2, shows its caller, steps to
  line 3 and exits normally. Only libSystem is linked; source hashes stayed
  unchanged. The guarded run took 33.387s, peak tree RSS 1,131,511,808 bytes,
  and the checked-in integration regression passes. No owned processes remain.
  Receipt/artifact: `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-real-python-debug-zor6_5sq/README.md`.
  This uses a host PCC compiler, not a rebuilt native PCC1. Module-entry source
  coordinates, locals/types/watch/evaluation, exceptions and full debugger
  protocol/IDE integration remain open; LLDB lacks a Python language plugin.
- Native-build preparation now registers all seven debug record classes in
  the compiler inventory and binds the indexed CompilerInt4 block fact before
  lane access. 49 focused inventory/source checks pass. Peer `8fbdfda18` was
  integrated before publication; its committed checks were not repeated.
  Receipt: `/Users/jiamo/pcc-recovery-archive/2026-10-22/ide-debug-native-records-1kwco11c/README.md`.
- Next: build a current-source native pcc1 and repeat the unchanged acceptance;
  add complete unwind coverage and module-init/local/type/value locations, then validate
  pcc1 break/step/frame/locals/watch/exception behavior. Host CPython debugging
  and translated IntelliJ/PCC-GUI integration remain independent requirements.
- Source snapshots, baseline objects/IR, patch and exact gate output:
  `/Users/jiamo/pcc-recovery-archive/2026-10-21/ide-debug-source-liq3q2lh/README.md`.
  No owned matching test process remains. This is the `pcc-ide.pcc1-python-debug`
  task in the TAG execution queue; the complete IDE/product goal stays open.

## 2026-10-17 Mac local continuation

### 2026-10-21 Claude session

- #82 landed in `ac063182a` (stale pins retargeted in `89e36b24d`):
  zero-argument `super()` and `__class__` read a
  `__class__` cell, one per execution of a class statement, filled after the
  body and before decorators; module classes keep `.class.cell.<mod>.<Class>`.
  An empty cell raises CPython's NameError (`super().x`, `__class__`) or
  RuntimeError (bare `super()`). Local-class methods are closures: enclosing
  bindings and `__class__` are trailing capture parameters carried by the
  function objects the class statement publishes (the hidden
  instance-attribute approximation is gone); capture sources are read in the
  enclosing function's scope, never the class namespace. Bases that are run-time
  values get a run-time composed layout. `super()` takes its receiver from the
  first positional parameter; staticmethods and explicit `super(T, expr)` go
  through the runtime `py_super_getattr` (CPython supercheck and descriptor
  binding); names no compile-time class defines are found in the MRO when the
  call runs; `super().name` works as a value; `super().count()` is no longer
  treated as `str.count`.
- Tests (native, five collectors, compared with CPython 3.15):
  `tests/python/test_local_class_method_closures.py`,
  `tests/python/test_super_runtime_resolution.py`,
  `tests/python/test_function_local_class_execution.py`. Validation: the
  77-file focused regression plus the new files (default selection and the
  pcc0 integration subset). Not run: pcc1-parametrized cases (needs a pcc1
  rebuilt from this source), bootstrap, full five-GC gate.
- Failures already on master, fixed: `test_live_property_lookup.py`'s stub
  host lacked `_property_accessor_takes_protocol_args` (added by `d26f61b06`;
  18 cases, separate commit); `test_local_class_receivers.py` required an
  unbound-local guard that `439d8274e` correctly omits for definitely bound
  class names (`class_values`); `_l1_codegen_static_methods.py` was stale for
  two signatures (`test_l1_codegen_static_method_table_matches_host_contract`)
  and is regenerated with #82.
- Open, found while verifying #82 (native runs, CPython 3.15 compared):
  - Fixed in `9cad5966e` (#82 stage 3a): `object`'s namespace (CPython 3.15
    `vars(object)`) is read where CPython's lookup reaches `object` (the
    super walk at the root, instance getattr before `__getattr__`, the class
    getattr miss), not from the root's attrs dict; a 23-name table filters
    those lookups (`__getattr__` dispatch measured unchanged). `__reduce__`,
    `__reduce_ex__`, `__dir__`, and `__getstate__` for a `__slots__`
    instance raise NotImplementedError. Test:
    `tests/python/test_object_namespace.py`.
  - `super()` through a builtin base: a name the base's CPython namespace
    defines raises NotImplementedError naming the base
    (`BaseException.__str__`, `dict.__setitem__`, `str.upper`); a dict
    subclass reaches get/keys/values/items/pop/setdefault/clear. Next:
    native BaseException, KeyError and dict namespaces. `class X(list)`
    silently drops the `list` base (instances are not lists; `len()` raises
    a bare TypeError); the explicit "no implemented initializer" error stays
    for its `super().__init__`.
  - Fixed in `0fc9543ba`: a known-class receiver whose MRO does not declare a
    method called the one other class declaring that name
    (`Q().__setattr__("v", 5)` ran `P.__setattr__`). Test:
    `tests/python/test_known_class_method_lookup.py`.
  - Fixed in `2420ba524`: the host CPython 3.15.0 final (installed 2026-10-20,
    replacing rc1) moved cProfile into `profiling.tracing` (PEP 799); the
    port follows, and `test_py_stdlib_devtool_closure.py` passes again,
    including its native integration case.
  - Fixed in `a9305caed`: `db1830cf8` writes every class's `__doc__` at
    creation, so exposing the namespace no longer bumped the field-cache
    epoch (two `test_class_lookup_cache_runtime.py` cases failing on
    master).
  - Fixed in `cb40b6988`: two stale test pins (a strict xfail that now
    passes in `test_hasattr_getattr_probe.py`; the `class.captures.instance`
    label `ac063182a` retired in `test_constructor_unpack_binding.py`).
  - Fixed in `8e8aed2f0`: exception classes constructed by name or in
    `raise Cls(...)` take the runtime class call, so `args` survive an
    `__init__` without `super().__init__`, an indirect exception base, and
    `raise Cls(..., key=v)` (which skipped `__init__`); keywords with no
    `__init__` raise CPython's TypeError. Test:
    `tests/python/test_exception_constructor_args.py`.
  - Fixed in `12951e8d3`: `sorted(s.copy())` on a set failed to compile (the
    copy was an unrooted `py_set_new` result filled by `py_set_update`);
    a set-typed `copy()` now takes the slot-owned set algebra call as
    `union()` with no arguments. `test_native_set_methods.py` failed on
    master.
  - Validation for `9cad5966e`..`12951e8d3` (default selection, native, five
    collectors where the tests run them): the 77-file #82 regression, 52
    stage-3a files, 75 dispatch files, 55 exception/constructor files and
    the new tests, 7963 passed, 2 xfailed, deselecting only the master
    failures listed below (each re-run on `9dcf40aec`); the devtool native
    integration case passed. Not run: the rest of the integration subset,
    pcc1-parametrized cases, bootstrap, the full five-GC gate.
  - Open: dict-subclass `pop(k)` and `d[k]` raise
    `KeyError('pop(): key not found')` and `KeyError('key not found')`
    instead of `KeyError(k)`, and `clear()` replaces the backing dict.
  - Measured (1M iterations, five collectors): `__getattr__` dispatch costs
    0.88-4.5 us per access and a `getattr(o, name, default)` miss 0.87-3.7
    us, against CPython's 0.05 and 0.03 us.
  - `super(T, obj)` outside a class body falls back to the CPython builtin
    (strict stub).
  - Fixed in `ae6ecaae4`: `x = v ** 2` with a dynamic `v` returned 0 for float,
    complex and user operands, and `v < 1.0` read a dynamic `v` as a float;
    `56f0fb594` fixed the inlined `map`/`filter` lambda (`test_native_map_filter_lambda`).
  - Still open, work saved in
    `/Users/jiamo/pcc-recovery-archive/2026-10-21/power-typing-wip/`:
    inference types `int ** -1` as int (a function computing `(2 ** -1) + 1`
    prints nothing), `1.5 if c else 2` yields `2.0`, and a type-changing
    augmented assignment (`value = 3; value /= 2`) fails to compile.
    Visible float-capable powers with a dynamic peer still fail closed.
  - pcc's owned `pow` is up to 2 ulp from CPython's libm result
    (`4 ** 1.5` gives `7.999999999999998`, `0.5 ** 2.5` gives
    `0.17677669529663687`); a comparison returns a truth value even when a
    user `__lt__` returns another object (CPython returns that object).
  - #82 stage 3: `__set_name__`/`__init_subclass__` in CPython order,
    metaclass `__classcell__`, per-execution cells for class statements in
    module-level loops. Stage 3a drafts (the `object` namespace and builtin
    base tables) are in the archive directory above.
  - Failing on master, unrelated to these commits (seen in their gates):
    `test_py_lambda_cpython_calls.py::PyLambdaCpythonCallTests::test_lambda_returning_cpython_object_stays_tagged`
    (`libpython_mode="auto"` under the owned runtime: "owned platform
    runtime does not include libpython"),
    `test_owned_descriptor_module_bindings.py` (6 of 9 when run alone),
    `test_exception_constructor_owned_result.py` (6 of 9: the builtin
    exception producer is pinned and passed directly instead of published
    into a root), and, stopping at their first failure,
    `test_exception_raise_lifetime.py`,
    `test_native_exception_context_chaining.py`,
    `test_native_subscript_raise.py`,
    `test_valueclass_constructor_argument_abi.py`,
    `test_virtual_thread_frontend.py::test_virtual_thread_join_parks_and_propagates_outcomes`
    (the same "slot-call Call lacks an immediate owned-result handoff"
    compile error, from a call operand in a generator's return), 12 of 26
    `test_virtual_thread_lowering_ownership.py` IR-model cases (an
    unmodeled `pcc_slot_call_status_report` call on a protected cleanup
    path; an allocated owner not cleared on its NULL error path),
    `test_virtual_thread_sequential_readable_writable_use_platform_reactor`
    (the native program times out), 29 of 36
    `test_virtual_thread_spawn_slot_ownership.py` cases (the same NULL
    error-path owner contract), two `test_virtual_thread_park_effect.py`
    cross-module cases (a parked result prints None),
    the two `attribute` cases of
    `test_native_bytearray_receiver_ownership.py::test_bytearray_receiver_does_not_retain_old_buffers`
    (GC0 retains 1,065,005 bytes over 64 `buffer.data.extend(b'')` calls),
    seven `test_py_stdlib_*_closure.py` integration tests,
    `test_typed_property_replacement_native_unresolved[pcc0]`,
    `test_host_emitted_global_barrier_and_nonlocal_cells`; the pcc1
    compile-only cases need a current pcc1 binary.
- Continuation (same session), each native on five collectors and compared
  with CPython 3.15:
  - `1bf8df737`: NotImplemented has its own type tag (34) and type object
    (`NotImplementedType`); `bool(NotImplemented)` raises as in 3.14+.
  - `7b9e026d6`: `super()` and lookups reach native BaseException, KeyError
    and dict namespaces (the builtin-base item above).
  - `87076e422`: `Cls.method = f`, a newly assigned dunder and `del Cls.m`
    reach every lookup; the class method table (a borrowed alias of the
    attrs dict) kept the compiled original (SIGSEGV on GC0-3). Test:
    `tests/python/test_class_method_rebinding.py`.
  - `76ea24e9c`: a class body whose metaclass defines `__prepare__` and
    `__new__` read globals and builtins through a silent-NULL getitem, so a
    dataclass `field(default_factory=list)` died with "no active exception
    to reraise" (`test_dataclass_inherited_default_identity[prepared_overrides]`).
  - `7dc4627f3`: any()/all() run CPython's iterator protocol (generators,
    sets, strings and user iterables were answered from len/getitem);
    iter() supports the `__getitem__` sequence protocol; hasattr() (crash
    on a non-str name, swallowed every exception), format(), chr(), abs(),
    divmod() and the zero-division text match CPython. Test:
    `tests/python/test_builtin_call_semantics.py`.
  - `d058fe6d8`: GC4's explicit `gc.collect()` never drained the store
    buffer, whose entries own their recorded value, so a value overwritten
    after the last background step never finalized. Test:
    `tests/python/test_gc4_explicit_collect_store_buffer.py`.
  - `08f59c286`: three GC test harnesses failing on master (a stale import
    closure, a CMS-worker harness on the pre-seed mark protocol, a lease test
    linking through `link_args` and running its threaded case unthreaded).
- Open, found by the same differential runs (next, in this order): builtin
  functions as first-class values (`f = len` raises NameError, `builtins.len`
  needs libpython; `repr(abs)` and `type(f)` of every function report
  `object`, so `types.FunctionType` accepts any object); lazy
  `zip`/`map`/`filter`/`enumerate`/`reversed` objects (`zip`/`enumerate`
  build lists; a bare `map(...)` is a NameError; `zip([1], 5)` returns
  `[]`); a statically inherited metaclass whose `__new__` returns another
  type (`class B(A)` calls `A`'s metaclass); `type(cls)` for a class built
  by `super().__new__` in a metaclass; `__module__` from a prepared
  `__name__`; `__prepare__` must return a mapping.
- Next (my ordering, open to redirection): the builtin-base namespaces
  above, #82 stage 3, then the gateway regression (handoff below) and the
  10-20 queue.
- Maintainer decisions today, in their words: GC0 class collection, "10
  不用做。遇到再说。"; the quota-checkpoint rule, "如果是， agents.md 移除。"
  (removed in `8e4cd818e`); "删删除 bisect-wt worktree。" (removed).

### CPython Lib/test conformance and open work (2026-10-21, Claude)

- Harness (task 6): `scripts/cpython_conformance/run.py` (`fdeab9357`,
  `10ab6f7d0`). Every Lib/test module (top-level, packages and their
  `test_*` modules: 857 in v3.15.0) runs under CPython with a minimal
  `test.support` and natively from a replay of the CPython case plan;
  rate = cases succeeding natively / cases succeeding under CPython. Run:
  `uv run python scripts/cpython_conformance/run.py --cpython-src
  /Users/jiamo/pcc-conformance/cpython-v3.15.0 --out <dir outside the repo>
  --runtime-archive <archive> --jobs 3 --resume`; `--modules m1 m2 ...`
  reruns a subset; `summary.md` there ranks compile and case failures by
  cause. Needs a CPython v3.15.0 source tree (the path above on this
  machine) and a runtime archive built from the same tree (`uv run python
  -c "import sys; sys.path.insert(0,'.'); from tests.runtime_build_cache
  import cached_pcc_python_runtime;
  print(cached_pcc_python_runtime()/'libpy_runtime_pcc_py.a')"`); a stale
  one fails every compile with "codegen checksum differs".
- First full native run, source `10ab6f7d0` (identity `4982cd2d7ae59935`,
  93 minutes at `--jobs 3`), receipts in
  `/Users/jiamo/pcc-conformance/runs/2026-10-21-10ab6f7d0/`: **0.56%** --
  166 of the 29,398 cases that succeed under CPython (34,270 collected in
  857 modules) also succeed natively. 371 modules have no baseline (skipped
  by policy: sockets, subprocesses, fork, CPython test extensions,
  resources, GIL-only, doctest-only); 403 of the other 486 do not compile,
  37 stop early.
- The 190 modules whose first-run compile stopped on a method decorator
  (13,556 CPython-passing cases): none compiled at `10ab6f7d0`; 48 compile
  and 67 cases pass at `b3acc095b`; 71 compile and 77 pass at `281ff86f9`
  (identity `41db5aaeb9d4f8be`, receipts
  `/Users/jiamo/pcc-conformance/runs/2026-10-21-281ff86f9-decorators/`).
  Only 9 of the 71 run to completion: 28 stop at import on "No module
  named" (8 more raise SkipTest for one), 9 on a missing module attribute.
  First blockers of the 119 that do not compile (modules, the
  CPython-passing cases they hold -- an upper bound, a module can stop at
  the next blocker): Thread target kwargs 18 (1,678; fixed below),
  multi-file libpython fallback 28 (1,460), slot-call operand without an
  owned result 10 (1,244), StrType to float in test_array 1 (1,043),
  owned-result handoff 6 (990), native class base unavailable 6 (965),
  `__neg__` on a class instance 1 (365), `super().__init__` through a
  foreign base 4 (362), compiled module attribute mutation 4 (339),
  `super().__new__` through a foreign base 5 (228).
- Pushed today: builtin call shapes through the wrappers (`02893728f`),
  arithmetic protocols and CPython 3.15 TypeError texts at object
  boundaries (`11b3c8c3b`), `round()` (`1a8237cb5`), `sum()` (`191639575`),
  complex negation (`9164f6bec`), the `f(*instance)` crash (`8f616f95c`),
  method decorators at class creation (`b3acc095b`); class decorator
  expressions at class creation, with pcc unittest class-level skip marks
  and a static `unittest.main()` that honours them (`8d92407b0`);
  class-body expression statements (`2d0eb4e53`); a nested def's imported
  and builtin decorators applied where it runs (`5b235400d`); the strict
  stub for a function whose CPython value reaches a slot-call operand
  (`97bf453b8`); `raise C` for an exception class held in a variable
  (`c6b7b1cd1`); `import glob` (`d12559992`); two stale infrastructure
  test pins (`de3d4f26d`); a stubbed function's result stays native under
  strict callers (`3defe7403`); natively lowered int results record their
  owner (`c53f3473f`).
- This batch (each commit names its test; native on five collectors against
  CPython output): pwd lookups no longer crash natively (`c9f8d4cdc`);
  threading.Thread kwargs reach the target (`991948c29`); class-body
  compound statements run when the class is created (`7b29a753a`); the class
  module-init function is stubbed under strict no-libpython (`0d196c9d5`); a
  missing module raises ModuleNotFoundError (`683314832`); unpacked method
  arguments on a dynamic receiver (`927646af5`). The class, decorator,
  hoist, threading, str-method, import and strict-stub test files are being
  rerun against them (no new failure so far; the ones found are listed under
  failing on master). Not run: pcc1-parametrized cases, bootstrap, the
  five-GC gate; the harness rerun of these modules is next.
- In progress (Claude, do not duplicate): slot-call operands without an
  owned result and the owned-result handoff, producer by producer. The
  first one each of the 17 modules stops at (traced in the compile
  workers): `slice(...)` as an operand (test_deque, test_list, test_tuple,
  test_userlist, test_userstring), `dict(x)` of a non-dict source
  (test_collections, test_ordered_dict), `str(bytes, encoding)` as a
  receiver (test_codecs, test_zlib), `threading.current_thread()`
  (test_threading) -- these four are fixed in the next batch; still open:
  `pickle.loads(...)` (test_str), a method call on a closure-cell receiver
  in a lambda (test_mailbox), `__class__` in a class attribute initializer
  (test_super), a BinOp keyword value in a decorator call (test_tcl),
  `s.rstrip().splitlines()` as a binary operand (test_tokenize),
  `".".join(map(str, ...))` (test_ipaddress). Also: the recursive stdlib
  probe (`_stdlib_module_compiles`) codegens a
  dependency without the compile's strict no-libpython setting and
  swallows the reason, so a module that compiles strictly is excluded --
  CPython's `genericpath.py` fails the probe (its `map` becomes a CPython
  builtin call) yet compiles as a strict sibling module, and
  test_genericpath, test_ntpath and test_posixpath stop at `import
  genericpath`.
- Open, independent (free to take; native, found by the harness, each with
  a one-line repro):
  - native providers missing at import, by the CPython-passing cases of the
    modules they stop: `array` (4 modules, 348 cases), `doctest` (5, 232),
    `_sqlite3` (3, 100), `tokenize` (81), `pyexpat` (79), `urllib.request`
    (77), `plistlib` (71), `fractions` (2, 67), `_interpreters` (65),
    `timeit` (42), `string` (41), `_multibytecodec`, `dbm.dumb`, `_pyrepl`,
    `_opcode`, `_dbm`, `_ctypes`, `resource`, `atexit`, `select`, `fcntl`,
    `_locale`, `_imp`;
  - module attributes missing natively: `ctypes.create_string_buffer`,
    `ctypes.LittleEndianUnion`, `os.stat`, `os.fsdecode`,
    `contextlib.closing`, `platform.platform`, `math.nextafter`,
    `urllib.parse.urldefrag`, `zoneinfo._tzpath`,
    `collections.abc.KeysView`; `sys.getfilesystemencoding()` has no
    native lowering;
  - str and bytes methods are not attributes natively: a str exposes only
    `count`/`startswith`/`endswith` (`py_str_getattr`, `_str_type_fill`),
    a bytes none, so `getattr(s, "split")`, `f = sep.join`, `s.join(*xs)`,
    `m.format(x)` on a dynamic str raise AttributeError and
    `hasattr("", "join")` is False; `str.join(sep, xs)` through the type
    needs libpython;
  - `threading.current_thread()` returns the thread id (an int), not a
    Thread object (`py_threading_current_thread`), so `.name`, `.ident` and
    identity differ from CPython;
  - `map`/`filter` exist only as compile-time shapes inside `list()` /
    `tuple()` with a lambda, a user function, `str` or `chr`;
    `list(map(len, xs))`, `tuple(map(os.fspath, m))`, `sorted(map(abs,
    xs))`, `next(iter(map(...)))` raise "NameError: name 'map' is not
    defined"; builtin functions are not values (`f = len`); `zip` and
    `enumerate` build lists;
  - native class bases: `collections.abc.MutableMapping` (shelve),
    `io.RawIOBase` (test_io), `unittest.TestCase` in test_random and
    test_selectors ("native class base is unavailable");
    `super().__init__`/`super().__new__` through a stdlib or builtin
    exception base (test_bdb, test_exception_group, test_pathlib);
    `owned __new__ lookup does not implement this builtin allocator` at
    test_copy import;
  - `compiled module attribute mutation requires proven managed provider
    storage` (test_bz2, test_lzma: `compression._common._streams.BUFFER_SIZE`)
    and `... assignment requires a value export` (test_fileinput
    `gzip.open`, test_locale, test_threading_local);
  - test_array: "Layer N cannot coerce StrType to float"; test_statistics:
    `__neg__` for unary minus on a class instance; test_warnings: class
    keyword arguments (metaclass/kw); test_descr: non-literal `__slots__`;
    test_re/test_iter: tuple unpacking of a non-tuple RHS;
  - deep recursion in the runtime's protocols (repr of a deeply nested
    container) overflows the native stack (SIGSEGV, which also loses every
    later case of the module run) where CPython 3.15 raises RecursionError
    from its C-stack bound (`_Py_ReachedRecursionLimit`); the 1,000-level
    Python counter (`py_recursion_enter`) is the wrong limit. Repro:
    test_dictviews `test_deeply_nested_repr`;
  - exception chaining on user exception classes: implicit `__context__` is
    recorded only for builtin `PY_TYPE_EXC` objects
    (`py_exc_set_implicit_context_slots`), and `exc.__context__` on an
    instance of a user `Exception` subclass raises AttributeError;
    `ImportError.name`/`.path` are not set;
  - a zero-parameter function in a class body (hypothesis strategies in
    test_base64): class_gen requires a `self` receiver; a class, walrus,
    `global` or `nonlocal` inside a compound class-body statement raises
    NotImplementedError ("class-body statement ...");
  - method decorator stacks mixing a class_gen decorator with a general one
    (`@classmethod @contextlib.contextmanager`, `@DynamicClassAttribute
    @abc.abstractmethod`) are still refused;
  - `contextlib.contextmanager` is a compiler-owned static `with` lowering;
    pcc's `contextlib.contextmanager` raises NotImplementedError, so a
    decorated function used outside `with`, or under another decorator,
    fails; `functools.wraps` compiles as a no-op on functions (no
    `__name__`/`__wrapped__`); `functools.cache` on a method compiles
    uncached; `abc` is a compile-time-only import, so `abc.abstractproperty`
    or any `abc` attribute without compile-time handling is a NameError;
  - `dir(cls)` raises RuntimeError "native default dir requires complete
    class and builtin namespaces" (49 failing cases in the decorator
    rerun); reflective unittest discovery (`loadTestsFromTestCase` and
    result-driven runs) is not runtime-owned (23);
  - test_bool natively (17/31): `bool(x=1)` does not raise TypeError,
    `(True).real` AttributeError, `bool` is subclassable, `0j != 0j`, an
    isinstance case, bool identity after some operations, no `eval`, no
    `marshal`;
  - failing on master `281ff86f9` (checked on the unchanged tree):
    `test_py_stdlib_unittest_closure.py::test_unittest_and_mock_match_cpython_strict_self_no_libpython`
    (after `mock.patch.object` exits, `Target().value` raises
    AttributeError: the original attribute is not restored);
    `test_py_function_decorators.py::test_imported_bare_decorator_is_metadata_noop`
    (`singledispatch` refused) and `::test_same_module_bare_decorator_still_applies`
    ("slot-call Call lacks an immediate owned-result handoff");
    `test_callback_sync_context.py::test_all_direct_method_adapter_roles`
    (`expose_unbound`, which returns `Worker.parked`, no longer sets
    `PY_FLAG_FUNC_AUTO_PARK` on the published function; `expose` and
    `expose_class` do);
    `test_native_sysconfig.py::test_sysconfig_get_config_var_dispatches_to_compiled_stdlib_provider`
    (the IR reaches `get_config_var` through the generic import method
    call, not `@user_sysconfig_get_config_var`);
    `test_class_lookup_error_cleanup_native.py::test_native_class_lookup_cleanup_restores_error`
    (every case: "pcc self-link mode does not support link arguments:
    .../probe.o");
    `test_gc_threading_substrate.py::test_scheduler_queue_root_transfer_plans_finish_after_outer_graph_unlock`
    (expects one `stack_alloc(128)` in `pcc_gc_store_root`),
    `::test_c_root_store_debug_off_invalid_new_preserves_benign_update` and
    `::test_c_root_store_debug_on_invalid_value_traps_after_expected_boundary`
    (the C probe exits -11);
    `test_hoist_global_cell_boundaries.py::test_host_emitted_global_barrier_and_nonlocal_cells[on]`
    needs `PCC_RUNTIME_ARCHIVE` set to an archive of its own build
    configuration. `test_callback_sync_context.py`'s
    `test_host_frontend_native_callback_context` and
    `test_native_compiler_callback_context` need
    `PCC_CALLBACK_RUNTIME_ARCHIVE` naming a threaded archive (one with
    `freestanding_thread_kernel_pthread.o`).

### 2026-10-21 handoff (session stopped by the maintainer)

- Full handoff with profiles, patches, repros and worktree list:
  `/Users/jiamo/pcc-recovery-archive/2026-10-21/README.md`.
- Gateway throughput regression, open: HEAD 13,155 req/s (77 µs per request)
  against 65,764 (16 µs) at `983254526`. `json.dumps(..., sort_keys=True)`
  reaches the generic dynamic call and the compiled `pcc/stdlib/json.py`
  since `168e4d069`; generic per-operation costs added from `1a59d09df` on
  (foreign-lease slow path for immortal literals, a malloc per scheduler root
  handle, scratch-frame memset, frame notes, TLS). Fix the generic costs; no
  per-module fast paths (see AGENTS.md).
- Uncommitted: GC0 lease guard for immortal objects (`json-wt`, model tests
  only); str Unicode whitespace (`push-wt2`, native run fails at a float error
  path); `tests/python/test_native_class_collection.py` (GC0 keeps classes
  immortal; the maintainer is still evaluating class collection).
- Local-class `super()` segfaults on GC0-GC4 (repros in the archive).
- `pcc/stdlib/json.py` differs from CPython json (escapes, key conversion,
  messages, circular check, missing parameters); the module needs CPython's
  semantics in its general implementation.
- Release dry run 39453533690: sdist and four native wheels green.

### 0.1.8 wheel size (2026-10-20)

- `PCC_BUILD_PCC1=<smac5 pcc1> uv build --wheel` at `5a3414452`:
  `python_cc-0.1.8-py3-none-macosx_12_0_arm64.whl` is 126,479,187 bytes
  (deflate; 451.1 MB unpacked, of which pcc1 403.9 MB -> 115.9 MB). The
  maintainer requested a 300 MB PyPI file limit on 2026-10-18, so size is
  not a 0.1.8 blocker once granted. The 430 MB wheel below predates the
  pcc1 size reductions (pcc1 was 1.69 GB then).

### Host CPython 3.15.0 (2026-10-20)

- Host and oracle are CPython 3.15.0 on every platform (uv 0.13.3;
  3.15.0rc1 stays installed locally). Pushed to shenming: `d3c15423d`
  (subprocess capture), `024bfe68a` (host/oracle 3.15.0, gate identity,
  replacement contract), `1e5c7cec5` (netrc/configparser follow 3.15.0;
  configparser line splitting and `read_file`; sysconfig schemes and
  `prefix`; `os.fspath` messages; native `open(PathLike)`; native
  `sys.prefix` no longer runs `python3 -c`), `fa61609b5` (three stale test
  pins), `c2fd2d5fc` (linkage scan of non-ASCII artifacts), `1bc90dde1`
  (every CI job runs the interpreter `.python-version` names; the Docker
  wheel checks stay on the requires-python floor, 3.13), `ed822fc88` (the
  artifact-export test still pinned `--python 3.13`; CI 39451814729 failed
  it on Linux x86_64). Record:
  `docs/investigations/cpython-315rc1-host-migration.md` No.2.
- Same-source runtime archive under 3.15.0 equals the rc1 build
  (`d0c1738c...`). Stage1 of `406c1321a` (the content of `1e5c7cec5`
  without upstream `1c77ecb72`) under 3.15.0, snapshot `smac7`: rc 0,
  22 m 46 s, pcc1 `bf8773cd...`, runtime `a4617931...`; worker-prior gate
  node passes in 96.7 s; macOS package set 166 passed, 3 failed (two stale
  pins and the linkage regex, fixed in `fa61609b5`/`c2fd2d5fc`; the three
  pass with that pcc1). CI 39452230891 at `ed822fc88` in progress: the first
  run with 3.15.0 on all four platforms.
- Dict/list methods bind on receivers the compiler cannot type
  (configparser continuation lines), and `list.remove` raises ValueError
  where it raised exception tag 12 (ArithmeticError) unchecked. Stage1 of
  that change under 3.15.0, snapshot `smac8`: rc 0, 21 m 57 s, pcc1
  `556578fd...`, runtime `92b1b796...`; gate node 100.8 s; macOS package set
  170 passed (the three earlier build-exec failures included); C3, oracle
  and `open(PathLike)` tests 49 passed. Main checkout commits `95ad2e244`
  (README rewrite, `docs/c-howto.md`, runtime archive notes) and
  `e7a442247` (user `//` dispatch test, GC0-4 green).
- GC0 never frees classes: `py_class_new` marks every class
  `PY_FLAG_IMMORTAL` because instances hold an uncounted class pointer.
  Programs that create classes repeatedly (local classes, `type(...)`,
  `namedtuple(...)` in a loop) grow memory under GC0; GC1-4 reclaim them.
  Known limitation, deliberately not changed now (maintainer 2026-10-21);
  the untracked `test_native_class_collection.py` in the main checkout
  asserts the GC0 case and stays uncommitted.
- A class defined inside a function whose method calls `super()`, used
  from that function, segfaults on GC0-4 (`class_with_super` probe); next.
- Gateway: the newest recorded comparison is 2026-09-26 (pcc1 C1 4.93x,
  C10 1.33x, C100 0.77x of asyncio TaskGroup, a fifth of its RSS); a fresh
  run on the smac8 pcc1 against CPython 3.15.0 is next.
- Gaps found while validating (open, all fail the same before these
  commits): native `str` whitespace is the six ASCII characters (CPython
  also treats `\x1c`-`\x1f`, `\x85`, `\xa0`, `\u1680`, `\u2000`-`\u200a`,
  `\u2028`, `\u2029`, `\u202f`, `\u205f`, `\u3000` as whitespace) in
  `isspace`, argument-less `split`/`strip` and int/float parsing; native
  `repr` does not escape non-printable characters (`'\xa0'` prints raw);
  `str.strip(chars)` compares UTF-8 bytes, so a non-ASCII `chars` can cut a
  character in half;
  provider calls lower as dynamic `import.method` calls instead of direct
  `@user_*` calls since `168e4d069` made imports live (bisected with
  `test_native_sysconfig` dispatch; `test_native_path_protocol` 2 show the
  same shape); the fix is the guarded direct call `f1bcf01e8` gave
  from-imports, applied to `module.function(...)`;
  `native_system._emit_native_sysconfig_call` still routes to
  `py_sysconfig_get_config_var`, which runs `python3 -c`;
  `sys.exec_prefix` forces the libpython fallback;
  `test_intent_constraints::test_every_obligation_has_constraints`
  (`TestCrossBackendDeterminism`); `test_runtime_substrate_spike` 32;
  `tests/test_ci_native_object_regressions.py` 13 fail locally (green in
  CI).
- CPython conformance `Lib/test`: `/Users/jiamo/pcc-conformance/cpython-v3.15.0`
  (tag `v3.15.0`, `d220409`).

### Task queue (2026-10-20, maintainer-directed)

1. CI green on all four platforms with the 400 s prior-worker budget
   (39423549944 at `dd263e9ea`: all gates green, macOS build-exec failed on
   subprocess capture; 39441439531 at `d3c15423d`: macOS build-exec failed
   on `open(PathLike)`; 39451814729 at `1bc90dde1`: Linux x86_64 failed a
   pinned `--python 3.13` argv; 39452230891 at `ed822fc88` in progress),
   then keep optimizing.
2. Fix the 22 failures that fail with and without `2c5326898` (and the four
   unreported `test_compiled_repo_main_*` nodes of the same file):
   `test_py_multi_file_bootstrap_shim` (13: mocks of the removed
   `_link_with_clang`, fake pools without `admission_width`, removed
   libpython mode, hand-written assembly the owned assembler now rejects —
   "label '_main' before any .section", "instruction outside __text",
   "directive '.text' not proven" — and a `pcc_multi` timeout);
   `gcsubstrate_d_store_ptr_capi_pins` (2, pin the retired
   `pcc/runtime/src/py_internal.h`); `test_exception_chaining_wiring` (2);
   `test_native_obj_compare_float::test_runtime_object_comparison_has_one_behavior_owner`;
   `test_native_hashability_contract`; `test_port_abi_constants`
   (dispatch-table counts); `test_native_enum_stdlib` (copies the deleted
   `pcc/ir/support/enum.py`); `test_native_ordering_semantics` (requires
   `PCC_RUNTIME_ARCHIVE`). Each keeps its contract, retargeted to the
   current owner. The earlier pre-existing lists below (IR-shape nodes,
   native samples, GC4 leak counts, `test_gc_root_precision` 13) are part
   of this item.
3. Integration tests entirely on the owned toolchain: postgres (8 of 10
   tests link through `run_translation_units_with_system_cc` or
   `--system-link`; libpgport/libpgcommon, zlib and the server binaries
   come from configure/make with the host cc; the promotion gate's
   postgres-zlib/postgres-cli point at the same tests), Lua
   (`tests/c/test_lua.py` links every pcc-built Lua with `cc`; the
   Makefile and native oracles are cc builds), and the zlib/SQLite
   `freestanding_libc` variants (host cc link). The owned route exists:
   `CEvaluator.run_translation_units_owned` -> `emit_executable`.
4. x86 codegen time (1.5-2.1x aarch64) and the x86 structured-record
   pipeline (the AArch64 concept); first make `pcc1 --target x86_64-...`
   report its real error (`--diagnostic-format sarif` avoids the Enum crash).
5. Native Enum/metaclass semantics (below), subprocess capture, other
   recorded gaps.
6. Semantic compatibility of at least 90% (maintainer goal 2026-10-20),
   measured by a CPython `Lib/test` conformance harness (built 2026-10-21, see
   "CPython Lib/test conformance" above): pcc1 compiles each
   `test_*.py` against pcc's `unittest` and a minimal `test.support`, runs
   it, and compares per-case results with CPython. Known gaps it will
   count: runtime generic aliases (`list[int]`, `Callable[[str], str]`,
   `__class_getitem__` all raise "'type' object is not subscriptable"),
   module globals read inside lambdas (`typing` NameError), Enum/metaclass,
   int/float/list subclasses. Plan: CPython 3.15 `Lib/test` outside the
   repository; pcc1 compiles each module with pcc's `unittest` and a minimal
   `test.support`; a result printer reports every case; pass rate = cases
   passing natively / cases passing under CPython with the same shim.
   Generic aliases as one feature: class subscription falls back to
   `__class_getitem__` (classmethod binding) after the metaclass
   `__getitem__`; a real `types.GenericAlias` (origin, args, parameters,
   repr, eq/hash, call, substitution, `|`, `__mro_entries__`); builtin
   generics; `collections.abc`/`typing` providers that subscribe like
   CPython (both are approximations today: `typing.List` is a marker whose
   repr is "List"). Compiling a file under `pcc/stdlib` as a top-level
   module resolves its `import sys` to the sibling `pcc/stdlib/sys.py`
   (`test_platform_provider_compiles_as_top_level_module_no_libpython`);
   CPython's built-in modules (`sys`, `builtins` and the
   `Modules/Setup.bootstrap` set) must not be shadowed by sibling files.
   pcc's own parser parses 1057 of 1098 CPython
   3.15 stdlib files (96.3%, 88.3% of lines); the rest use PEP 810 `lazy
   import`, PEP 695 `type`/`def f[T]`/`class C[T]`, PEP 750 t-strings, and
   one lexer case (`dataclasses.py:469`, backslash).
7. CI 39423549944 (`dd263e9ea`): worker-prior gates PASS on all four
   platforms; Native wheel jobs PASS on Linux aarch64, Linux x86_64 and
   Windows x86_64 (their native pcc1 gates within the 400 s budget); macOS
   native pcc1 gate PASS (165.7 s) but
   `test_pcc1_build_exec_does_not_need_host_python` fails on
   `subprocess.run(capture_output=True)` returning stdout None. Fix in
   progress: `pcc.unsafe.spawn_process_redirect` (child stdin/stdout/stderr
   on given descriptors plus a working directory: Darwin posix_spawn file
   actions, Linux raw fork/chdir/dup2/execve, Windows CreateProcessW with
   inherited handle duplicates) and `py_subprocess_capture_*` (output to
   O_EXCL|O_CLOEXEC temp files, so a child never blocks on a full pipe);
   the provider's run() handles capture_output/stdout/stderr (PIPE,
   STDOUT, DEVNULL, descriptors), text decoding with universal newlines,
   cwd, env with timeout, FileNotFoundError and TimeoutExpired, and
   CalledProcessError keeps the output. The native `run(..., check=True)`
   statement lane now declines capture_output and fractional timeouts,
   which it used to drop or truncate.

### Gate regressions from the 10-19 pushes and their fixes (2026-10-20)

- Prior-worker native gate budget is 400 s (maintainer decision 2026-10-20,
  was 300 s; "pass first, then keep optimizing"): the gate watchdog
  (`NATIVE_GATE_TIMEOUT_S`, compile plus ten executions) and the native
  compile timeout in `tests/python/conftest.py`. The 300 s figures below
  are the earlier budget.
- CI 39400019947 (`1f6bfccf0`): all four worker-prior gates PASS, both
  x86-64 within the 300 s watchdog. Failed: the three Native wheel jobs on
  `test_ci_native_object_regressions` (workflow contract; peer `79b0d0ec7`)
  and macOS pip dry-run (`package_level` None: inspect reported a freed
  name, so `get_package_target` found nothing; fixed by `61177cb9e`).
- CI 39407829397 (`57110e01a`): every worker-prior preflight failed in
  `test_runtime_abi_chunking` (`0d8586233` made signature chunk 27 hold 51
  entries; `e636ddfd5`), and every native-pcc1 gate failed: pcc1's export
  worker for `os` crashed (SIGSEGV in `py_obj_type_name` from
  `pipeline_ast_wire._closed_world_node_kind`, a freed AST node). Cause:
  `61177cb9e` left a local that may hold a borrowed alias registered only as
  an owned root, so a later `x = other_local` or exact-int store took
  `pcc_gc_store_root_take`, which releases the previous slot value
  unconditionally. `5a3414452` registers it as a borrowed root too
  (`test_native_owned_local_alias_store`, second test). A pcc1 at
  `b4e25509e` reproduced the crash locally; one at `168bd6a39` did not.
  Local Stage1 at `5a3414452` (smac5, 19:35, 403,899,144 bytes, `__text`
  342.8 MiB): `test_size_prior_native_processes_five_collectors[pcc1]`
  passes under the CI guard (90.0 s for the compile and all ten runs, peak
  tree RSS 2.41 GB), and `test_package_inspect.py` passes (pip dry-run
  reports `requests` at `nolibpython_python`).
- Native Enum is a compile-time shortcut, not CPython's: members are the
  raw values (`Plain.A` is the int 1, `Sev.ERROR` the str "error"), so
  `.name`/`.value` work only in the literal form `Cls.MEMBER.value`;
  `repr`, `isinstance`, `Plain.A == 1` (True natively), `Cls(value)`
  (a new instance) and `Cls[name]` differ. In pcc1 this breaks
  `Diagnostic.format_text` (`self.severity.value`), so every diagnostic on
  the full-CLI path (`--target`, `-O`, `--emit-obj`, ...) dies with
  AttributeError and hides the real error: `pcc1 --target
  x86_64-unknown-linux-gnu` on the gate fixture fails this way, while host
  pcc0 compiles it. Generic metaclass gaps underneath: a metaclass
  inherited from a base in another module is ignored (`type(C)` is
  `type`); after a metaclass `__new__` the class body's attributes are
  evaluated again and written over what the metaclass set;
  `super().__new__(mcs, ...)` drops `mcs`; `len(cls)` returns 0 and
  `cls[k]` raises TypeError instead of calling the metaclass's
  `__len__`/`__getitem__`.
- Local parity gate with a pcc1 at `b4e25509e` (smac4): 7 failed, 162
  passed, all in build-exec. Next blocker there: `subprocess.run(...,
  capture_output=True, text=True)` returns stdout/stderr None (the owned
  provider's `run()` never captures; the platform intrinsic has one pipe).
  `os.pathsep.join` and module str constants (`501d71254`), multi-argument
  min/max (`a45edbc01`) and str-subclass equality (`d78e5da22`) were fixed
  on the way. smac4 pcc1: 403,257,016 bytes, `__text` 342.2 MiB, Stage1
  28:25.

- CI dispatch 39281274740 at `297c86729`: Linux aarch64 worker-prior gate
  passed. Failed: macOS native pcc1 (Stage1 below); macOS worker-prior gate
  (`aarch64-regalloc-host` nodes, stale monkeypatch; `63879cf8a`); Linux
  x86_64 `native-pcc0` (x87 tables and prior worker programs print nothing,
  rc 0); Windows `windows-exit-native` (the tar program prints
  `TAR_OPEN_BEGIN`/`TAR_FAILURE OPEN`, rc 0, no traceback; the 0xC0000005
  is gone since `68d379245`). Redispatched at `4950fb833`: run 39299993677.
- Stage1 at `297c86729` (local, 397 s): "branch to '..._parse_instruction_err
  dotframedotlanddot257178dotedgedot1' out of range". Inline error edges
  (`a372d3b58`, default) are `cbnz` to cold stubs after the blocks; that
  function exceeds 1 MiB and the AArch64 text finalizer had no relaxation.
  `053729315` relaxes out-of-range b.cond/cbz/cbnz in `_finalize_text_entries`;
  `73a154cba` (peer) finalizes the relaxed layout before PC-based metadata
  (stack maps, compact unwind) and `d057e51b1` validates raw fixups first.
- Stage1 at `63879cf8a` (2302 s) linked pcc1 but the smoke compile failed the
  ownership audit (rc 97): `py_decref` of an unregistered static int under
  `pcc_gc_store_root` in `codegen.literal_lowering`'s module top
  (`PCC_GC_REFCOUNT_PROVENANCE_PROBE=3` + lldb). `1c0a63ab5` registered only
  the str/float literal pools; `0fe1fba3e` registers the int pool.
- Stage1 at `0fe1fba3e`: rc 0, 2194 s (a contending host test run overlapped
  it), pcc1 422,138,776 bytes. Gate fixture compile with that pcc1, gate
  environment (`measure_prior.py`): 123.6 s, rc 0, peak tree RSS 2.77 GB
  (147.6 s at `827f157d6`, 251 s before). The gate node's executions were not
  rerun.
- x86-64 dropped every inline error edge: the shared `emit_function_blocks`
  never read the kernel's edge table, so errors stopped propagating on Linux
  and Windows x86-64. `4950fb833` emits them (test/jne, cold payload stubs)
  and refuses a target without an edge emitter. Native x86-64 (docker,
  passes off, inline edges on): an exception probe prints CPython's output and
  traceback with rc 1; at `0fe1fba3e` it skipped a `KeyError` handler,
  printed `chain <null>` and `unreachable`, rc 0.
- Windows under Wine (`pcc-wine64:local`, persistent prefix, D: mapped; run
  with `LANG=C.UTF-8`, or Wine fails every non-ASCII file name with ENOENT):
  the uploaded CI `metadata.exe` reproduces `TAR_FAILURE OPEN` rc 0. A
  cross-built text-route exe (no inline edges) opens and preflights the tar,
  then fails `utime` on the read-only member (errno 13); Wine's metadata
  semantics differ from Windows here, so the tar result needs real Windows CI.
- CI 39299993677 at `4950fb833` (first run past Stage1 on every platform):
  pcc1 prior gate passed on macOS arm64 (`[pcc1]` node 260.6 s) and Linux
  aarch64 (278.8 s). Linux x86_64 and Windows x86_64 both hit the 300 s
  budget with the four codegen workers still running (x86 pcc1 is ~4.5-5x
  slower than macOS pcc1; codegen workers started at 44 s and none finished).
  Windows preflight: the tar tests passed; the worker-size prior GC4 growth
  case timed out (one stop-the-world step plus full relocation ranking per
  safepoint; fixed in `0a8806592`). macOS: the native-wheel install passed;
  the pcc1 package parity hard gate stopped at
  `test_pcc1_pip_install_tilelang_extension_tree_direct` (180 s timeout;
  from the repo root, `Requires-Dist: numpy` resolves to
  `projects/numpy-2.4.4`, so pcc1 first compiles its vendored Meson and builds
  numpy; at `8958403fc` the same step failed earlier, on a Meson parse error
  fixed by `d7ed7d143`). Open: x86 register allocation; whether
  cpython-compat installs should build local dependency sources.
- x86-64 code quality (`worker_resource_plan`, same module on both targets):
  AArch64 216,584 instructions with ~20,000 frame accesses; x86-64 257,687
  -> 209,834 after `22b4e1d9a` (frame store->load forwarding), `7230bf1cf`
  (%al only for variadic calls) and `15970f04d` (compare-branch fusion), but
  still 69,084 frame-operand instructions. A block-local write-through cache
  in r12-r15 was tried and dropped: it only replaced already-forwarded
  reloads (slot loads 17,063 -> 18,556, +29k moves); the remaining reloads are
  cross-block values, pointers live across calls and loop-carried PHIs, which
  need function-level allocation as AArch64 has.
- x86 assembler (`x86_64_asm_driver.py`, Linux ELF and Windows COFF): the
  two-pass driver (parse into records, measure, emit) encoded every
  instruction twice, and most of its native time was its own record transport
  (struct-packed spans, generator unpacking, re-slicing). It is now one pass:
  x86 branches are always rel32, so each line is encoded once at its final
  offset; local branches and `.long a-b` are patched at the end and large zero
  fills stay lengths until every reference validates. Objects are
  byte-identical to the frozen driver
  (`tests/python/data/x86_64_asm_driver_two_pass.py.txt`) on the test corpus,
  real Linux/Windows backend output and the 11 MB x86 `worker_resource_plan`
  module. pcc-compiled driver on that module: 19.8 s -> 10.9 s (CPython
  1.39 -> 0.72 s). Docker amd64: 283 host tests, an executed ELF, the
  exception probe (CPython output) and both native-pcc0 nodes pass. The first
  diagnostic is now the first failing line; the old driver reported every
  directive error before any layout error.
- In that native run 36% was the call into `encode_instruction` itself: a
  module-level `from m import f` is a live binding, so every call read it
  through `py_module_attr_get` (two str allocations, two hash lookups) and
  went through `py_obj_call_slots` with an args tuple and a merged kwargs
  dict, for every cross-module function call in pcc1. A sampled coordinator
  IR pass phase spent 25% in the same binder, 20% of it from
  `Instruction.from_text`'s `cls(...)`.
- `f1bcf01e8`: such a call now reads the binding, evaluates each argument
  once into a root, and calls f's native body when the binding is f's plain
  function object (`py_func_slot_has_native_entry` against
  `<f>_native_adapter`); otherwise the same roots feed the dynamic call.
  Providers always define the exported adapters (`pcc.package.install` stops
  at an import pcc1 cannot bind and published none, which broke the first
  Stage1 link). `15449630e`: `cls(...)` constructs the owner directly when the
  closed-world export graph proves it has no subclass. `e87970617`: x86
  operands are split with native searches (5% of the assembler).
  Stage1 with all of them (local macOS, rc 0, 1430 s, pcc1 427,739,208 bytes,
  +1.4%) compiles the gate fixture in 100.6 s instead of 123.4 s.
- CI 39331126169 at `1829e26d8` (single-pass assembler): all four worker-prior
  preflights pass (Windows previously failed its GC4 growth case and timed
  out). The Linux x86_64 and Windows pcc1 gates still time out at 300 s:
  codegen workers start at ~74 s and the longest is still running at 295 s.
  x86 pcc1 runs x86 code without register allocation (~3.5x AArch64's frame
  traffic), so every phase is slower; in the old 11 MB module 4,491
  non-address-taken, single-width, non-SSE slots carry 40k of 45k slot
  accesses.
- x86-64 register passes (2026-10-20, `85e2cd325`..`8b9f8392f`), run over each
  emitted function's text, Linux and Windows alike:
  `x86_64_frame_registers` keeps scalar slots in callee-saved registers
  (rbx/r12-r15, plus rsi/rdi on Win64; interference from the text's own
  liveness; no stack-map root, `lea`'d or mixed-width slot) and drops stores
  nothing reads; `_thread_x86_jumps` branches past jump-only blocks;
  `x86_64_register_copies` forwards register copies and constants and drops
  moves nothing reads (strong liveness, flags tracked); scalar phi edges copy
  without stack temps when no destination overlaps a source.
  `pcc_gc_load_ptr`: 481 -> 238 instructions (AArch64 193).
  CI (pcc1 canary compile, mostly link; Linux x86 / Windows):
  `f1bcf01e8` 46.6 s / 112.0 s, `85e2cd325` 42.2 / 68.5, `4faef9195` 15.9 /
  37.1 (AArch64 24.3). Linux x86 pcc1 gate: timeout at `f1bcf01e8` and
  `85e2cd325`, **288.4 s PASS at `4faef9195`** (CI 39351778017); Linux
  AArch64 231-233 s. Windows at `4faef9195`: Stage1 platform qualification
  failed, the Windows pcc1 crashed (0xC0000005 reading 0x17da5b9003c, after
  codegen of the GC4 canary; gc0-gc3 passed, and GC4 passed at `85e2cd325`),
  so the pcc1 gate did not run. Not reproduced or symbolized yet (no Windows
  pcc1 artifact; see the Windows crash recipe in the memory notes).
  `1c49f0ddf` broke every Win64 frame of 4096+ bytes (the copy pass dropped
  `mov eax, SIZE` before `call __pcc_chkstk`); fixed in `8b9f8392f`.
  CI 39357267830 runs `8b9f8392f`.
- Open: the passes are cheap in CPython (1.2 s for `worker_resource_plan`'s
  50 functions) but ~9 s natively compiled (pcc1 `function_emit` for
  `pipeline_frontend_workers` on Linux x86: 41.6 s -> 61.4 s with the frame
  pass alone): list elements and dict values reach `&`/`|` through
  `py_obj_bitwise_dispatch`, `CompilerIntArena.get_unchecked` pays a frame
  per call. Moving the per-line facts and bitsets into arenas read through
  `native_address()` (as the stack-map liveness does) is the next step; the
  Linux x86 gate passes with 11.6 s to spare.
- Where the Linux x86 pcc1 gate spends its 288 s (CI 39351778017,
  `4faef9195`, 4 vCPU): export+summary 13 s; codegen 13 -> 242 s with four
  workers packed back to back (~920 worker-s); link 31.3 s serial (ELF stage
  inputs 4.4, select 4.9, layout 5.4, resolve/relocate 15.4 for 500,261
  relocations). Worker totals over 16 modules: frontend 184 s, backend 521 s
  (prepare verify 53, layout 49, stack-map plan 56, function emit 354),
  assembly 95 s (parse+encode 79.5). The tail is `hashlib` (93 s) starting
  at 149 s.
- pcc1 runtime costs seen in a natively compiled PE link of the Windows
  canary under GC0 (macOS, 2026-10-20): `struct.unpack_from` 29% (its format
  cache's `dict.get` alone 7%), `py_bytearray_extend` 12% (fresh object and
  a byte-at-a-time copy per append), GC root/lease bookkeeping ~20%
  (`pcc_gc_store_root`, `pcc_gc_foreign_lease_acquire`,
  `pcc_gc_root_copy_lease`), `memset` 6%. Every `dict.get`/set through
  `_dict_slot_set_bound` registered, leased and dropped 16 scratch roots
  while it names 7 (now 7: `3d1ea4b21`; bytes copies use memcpy:
  `bb610f00b`).
- In a pcc1 AArch64 codegen worker (sregs1 pcc1, 2026-10-20) generic runtime
  paths dominate: `py_obj_getattr` 19%, `py_module_attr_get` 11%, GC
  lease/root bookkeeping ~22%, dict slot frames 7%. `module.NAME` reads went
  through both: fresh name strs per lookup, and a class MRO walk on every
  read because the attribute cache recorded ABSENT only when the instance
  dict missed. `8a1ac3e35` caches pinned name keys, `f76ae0b18` records
  ABSENT on dict hits when no class attribute/field/method/`__getattr__`/
  dict-subclass fallback exists: a loop of three imported-module constant
  reads per call went 59.7G -> 45.8G -> 26.0G instructions retired, and
  22.2G once a GC0 foreign lease on an ordinary object only counts.
- Backend routes are not one concept yet. The pcc1 default route (host
  target, frontend workers, IR passes off) emits AArch64 as
  `indexed-native-sections`: hot instruction families become machine records
  at selection, the remaining lines become records one by one, and sections
  are built from records. x86-64 (Linux and Windows) emits
  `indexed-assembly`: every instruction is a text line, the store/load and
  compare-branch peepholes and the three register passes parse those lines,
  the module is joined into one text, and the owned assembler splits,
  parses and encodes it again. Same commit/fixture (`4faef9195`, CI
  39351778017): target-independent phases are 1.4-2.4x slower on x86, but
  object assembly is 94.7 worker-s on x86 against 13.8 on AArch64
  (`worker_resource_plan`: 17.5 s against 2.4 s). Direction: x86 gets the
  same record transport as AArch64 (records from selection, passes on
  records, sections from records; text only for residual lines and the
  oracle), differing only in encodings and machine passes. Step 1 (this
  session): the worker streams each finished x86 function into
  `X86ModuleBuilder` (route `indexed-native-sections`, Linux and Windows),
  byte-identical to the text route; selection still formats lines and the
  builder still parses them. CI 39365361651 (`5e22565c4`): Linux x86 pcc1
  gate PASS 274.7 s (288.4 at `4faef9195`, >300 at `8b9f8392f`); worker
  object assembly 94.7 -> 14.0 s, emit+assembly 449.1 -> 396.7 worker-s,
  all 16 modules `indexed-native-sections`; macOS gate step ~240 s.
  Next: hot families (frame load/store/lea, calls, jumps, immediates) as
  records from selection, passes on records.
- Windows GC4 canary crash is intermittent and predates this session's
  runtime changes: `4faef9195` and `5e22565c4` crashed (CI 39351778017,
  39365361651), `8b9f8392f` passed. Both crashes read +0x3c of the first
  object of a 64 KiB-aligned block (`0x17da5b9003c`, `0x238caa5003c`) in
  pcc1.exe's coordinator after the canary's codegen worker retired, with the
  same 15-frame stack shape. CI's Windows Stage1 is a host-target compile
  with the default Python IR passes, hence the text route, the same route
  as a local `--target x86_64-pc-windows-msvc` cross build. A macOS cross
  build of `5e22565c4` (35 min) does not reproduce CI's layout: 13 of the
  14 CI return RVAs do not follow a call in it, and its `.text` ends before
  one of them. CI uploads no Windows pcc1.exe, so the crash stays
  unsymbolized. The cross-built Windows pcc1 at `892687d08` compiled and
  ran the GC4 canary under Wine once. Under Wine the explicit runtime's
  codegen checksum differs from the host-built archive because
  `bootstrap_source_sha256` hashes `str(path.relative_to(base))`, which
  uses `\` on Windows. Set `PCC_SOURCE_ROOT` and replace every recorded
  `codegen_checksum` with
  `sha256(<bootstrap_source_sha256 over \-separated relative paths> + ":" +
  _runtime_emitter_source_identity(root))`; this formula reproduces both
  recorded values of the `892687d08` snapshot.
- CI 39370398888 (`1d2f48a3f`: name-key cache, ABSENT cache, GC0 lease):
  pcc1 prior gate PASS on Linux x86_64 (node 226.9 s; codegen workers
  187.6 s, link 26.8 s) and Linux aarch64 (202.6 s; 136.7 + 50.1 s);
  macOS gate PASS. `59fd6537a` adds GC0 shortcuts for root copies and
  `dict.get` (a `dict.get` loop 16.54G -> 10.11G instructions). Windows:
  Stage1 PASS (3499 s), canary GC0-GC4 PASS (compile 39.6/95.1/95.2/107.0/
  180.0 s), pcc1 gate TIMEOUT. From its process samples: export+summary
  ended at 33.7 s (Linux 10.6 s), the first codegen worker finished 127 s
  after codegen began (Linux 68 s), and workers were still starting at
  289 s. Linux split: export 0-6.9 s, summary 7.6-10.6 s, codegen
  11.3-187.6 s (643.6 worker-s; `worker_process_pool` 122 s,
  `worker_resource_plan` 109 s), link 26.8 s. The fixture kept pcc1's
  stderr in memory, so the killed Windows run left no worker timing;
  `fb4fb0a91` streams it to `<output>.compile.stderr`. `3018b104f` counts
  GC0 root stores in place. CI 39376986157 runs `fb4fb0a91`.
- CI 39376986157 (`fb4fb0a91`): **Windows pcc1 gate PASS** (node 258.2 s;
  export 0-6.5 s, summary 6.9-10.8 s, codegen 11.4-212.5 s, 737.2
  worker-s; five-collector canary incl. GC4 PASS). Linux aarch64 and macOS
  gates PASS. Linux x86_64 FAIL: codegen workers ran ~30% faster than at
  `1d2f48a3f` (`os` 49.8 s vs 71.6 s), but the `hashlib` worker died at
  `class _SHA256` with "pcc re: native regex engine limit reached" (the
  matcher's recursion passed 8192, `freestanding_re_engine._match`). The
  same CI pcc1 under Docker amd64 compiled `import hashlib` alone fine;
  a full gate compile in Docker is the next repro. Cause not known yet.
- CI 39383157605 (`9e953a1fb`): Linux aarch64 and macOS pcc1 gates PASS
  (codegen ends at 130.6 s and 104.5 s; 415.4 and 274.4 worker-s, against
  428.8 and 400.0 at `1d2f48a3f`). Both x86-64 gates FAIL on the 300 s
  watchdog: Linux codegen ran 14.4-256.1 s (888.8 worker-s; `os` 97.7 s
  against 71.6 s at `1d2f48a3f` and 49.8 s at `fb4fb0a91`) and the ELF link
  was at 207,794 of 500,591 relocations when killed; Windows hit the same
  deadline. Every module slowed by the same 36-40% with no x86 change in the
  range, so runner speed is the likely cause (the receipts record no CPU
  model). Either way x86 codegen has no margin: it is 1.5-2.1x aarch64's.
  macOS `__text` 362.6 MiB at `1d2f48a3f`, 363.0 MiB at `9e953a1fb`; a local
  Stage1 at `168bd6a39` is 402,781,000 bytes (`__text` 341.8 MiB, 26:25).
- Ordering comparisons follow CPython: an unorderable pair (int < str,
  None, complex, dict, set < list, instances whose methods are missing or
  return NotImplemented) raises TypeError instead of comparing equal;
  tuples/lists find the first unequal item with `==`; str/tuple subclasses
  order by payload; list.sort/sorted/min/max propagate the error, and a
  failed list.sort keeps every item (`test_native_ordering_semantics`).
  Found alongside, still open: int/float/list subclasses have no payload
  ordering or arithmetic (`MyInt(5) + 1` raises TypeError, `MyFloat(1.5) +
  1` does not compile); `.count` on a dyn tuple resolves to the str method
  and on a lambda-captured list crashes in `py_str_count_range`; two- and
  three-argument min/max lower dyn operands through the i64 lane
  (`min(1, "a")` returns 0), refuse two str operands at compile time, and
  return float for `min(1, 2.5)`. Failing at `168bd6a39` before these
  changes: GC4 leak counts in `test_min_max_key_ownership` (callable,
  collect, factory, items) and `test_sort_key_ownership` (six shapes);
  `test_runtime_object_comparison_has_one_behavior_owner` since
  `d0b8b6720` (a second runtime-symbol table in the exact-compare path).
- Local parity gate with a pcc1 at `168bd6a39` (smac3): 13 failed, 156
  passed. inspect named every package "os"/"darwin" and pip dry-run lost
  its package level: `package_name = package_name or "package"` in a
  raw-scaffold module released the owned string before storing the same
  pointer back as borrowed (fixed in `61177cb9e`). build-exec stopped at
  `toolchain._find_tool`, a stub because `shutil.which(path=...)` fell back
  to CPython (`0d8586233`). array-core printed a 2-D repr on one line: the
  bootstrap CLI keeps its own array-core copy (`cli_bootstrap_array_core`,
  kept separate because closure calls lower to getattr bridges, see
  `tests/fallback_baseline.json`); its repr now mirrors
  `array_model.array_core_repr`. Still open in that copy: a 3-D literal
  parses as shape [2, 0]. The real `pcc.package.array_core` compiled
  natively by pcc0 matched the host on 15 commands, 3-D included.
  Failing before these changes at `168bd6a39`: 38 IR-shape nodes in
  `test_raw_scaffold_exact_container_subscript_ownership` (18),
  `test_py_for_target_representation_join` (6),
  `test_py_multi_file_compile` (5), `test_owned_ifexpr_local_transfer` (4),
  `gcsubstrate_b_root_store_scheduler_queue` (4) and
  `test_raw_scaffold_regex_ownership` (1); and `test_native_set_inplace_update`
  (6), `test_native_module_attr_pointer_storage` (2),
  `test_native_dyn_tagged_int_binop` (1).
- macOS package parity gate (`-x`): after tilelang (`343bf5a0b`) the
  suite had 12 more failures with a HEAD pcc1, all hidden by `-x`. Fixed:
  the owned `Path` lacked expanduser/mkdir/iterdir/... (`6648deec3`; it
  also fixes `with (expr).attr() as f:` parsing), `==` ignored
  NotImplemented for typed instances and `-> bool` dunders, `key == 1`
  did not compile (`28dcc9fa5`), and inspect named `demo_pkg-0.1`
  (`d49aafd8c`). The owned `ast.literal_eval` accepted only ints and flat
  int tuples (array-core printed `array(None)`); it now parses CPython's
  literal grammar, which exposed native gaps fixed alongside: PEP 515
  underscores in `int()`/`float()` text, `int("010", 0)`, dynamic
  `int - complex`, compile-time complex promotion against non-numbers,
  and `isinstance(x, complex/memoryview)`. The native inspect JSON now
  comes from the host's `inspect_package`/`inspect_artifact`
  (`fc4e97285`). build-plan/build-exec then stopped at
  `metadata._read_meson_boolean_options`, a no-libpython stub: module
  `re.search/match/fullmatch/finditer` with `re.I | re.S` or `flags=` fell
  back to CPython, and native text files refused `errors="ignore"`
  (only strict/surrogateescape: tell()'s offset map was inferred from the
  decoded text). The decoder now reports handler spans, files accept every
  handler and latin-1, and `ascii()` escapes non-ASCII inside any repr
  (`test_native_text_file_error_handlers`, `test_native_ascii_builtin`).
  Still open: ordering comparisons return
  False instead of raising TypeError when both sides answer
  NotImplemented; a complex-typed BinOp passed straight to a call
  (`repr(a - b)`, or returned from a lambda: "slot-call operand has no
  authoritative source or owned result") does not compile, though
  dynamic BinOps as arguments do; a pcc1 built before
  `28dcc9fa5` printed True for `key == 1`. pcc1 also cannot build a missing runtime archive
  (`emit_owned_object` unavailable natively), so local runs need the
  archive at the default path, as CI's Stage1 leaves it.
- Stale source-shape tests, failing at HEAD before this session's changes
  (their pinned text left in `983254526`/`bae3546e2`):
  `gc/test_gc_backend_config_fastpath.py` (2),
  `test_fresh_native_instance_append.py` (2),
  `test_gc_abstraction_surface.py::test_colored_relocating_gc_read_barrier_clears_candidate`.
- Windows is slow mostly in the PE link, not in the workers. Canary at
  `8b9f8392f` (CI 39357267830, GC0): codegen workers done at 3.2 s
  (Linux 2.8 s), then the coordinator alone ran 55 s (Linux 21.3 s, of
  which runtime verify 1.75 s). PE sections were grown by
  `bytearray.extend` per input section and `.reloc` per word; pcc's native
  bytearray copies itself on every extend, so that was quadratic in pcc1.
  `23b4d4a8c` joins the pieces once (budgeted CPython hosts unchanged): a
  natively compiled PE link of the canary from captured inputs went
  9.81 -> 8.06 s on macOS, image byte-identical. What remains there is GC
  protocol: `pcc_gc_store_root` 8.3% (called by compiled code),
  `pcc_gc_load_ptr` 6.8%, leases 6.1%, frame enter/leave 6.6%,
  `pcc_threads_enabled` calls 3.3%.
- Failing at HEAD's runtime ports (2026-10-20, pcc0, macOS):
  `test_native_namespace_projection_behaviors` rebinding/module-override
  (`globals()['sys'] = x` not seen by a later `sys.attr` read) and
  `test_live_property_lookup` five-selector/unresolved-replacement;
  with HEAD's worker, `test_direct_indexed_syscall` zero-capture-fallback
  (both targets), `test_direct_indexed_owned_passes` memory promotion and
  `test_stage1_checkpoint` frontend release (stale frontend stubs);
  `test_live_os_environ_provider` (mapping), `test_owned_os_path_provider`;
  model tests `test_gc_codegen_write_barrier` (4),
  `test_exception_chaining_wiring` (2), `test_class_lookup_cache_runtime`
  (2), `gcsubstrate_d_store_ptr_capi_pins` (1); ~150 cases in 38 model
  tests that touch leases (`test_virtual_thread_*`, `test_buffer_factory_*`,
  `test_bytes_decode_result_roots`, `test_owned_descriptor_module_bindings`,
  `test_freestanding_gc_root_operations` raw closure, ...), identical with
  HEAD's root operations. None runs in CI.
- macOS fails only `test_pcc1_pip_install_tilelang_extension_tree_direct`:
  `Requires-Dist: numpy` resolves to the repository's `projects/numpy-2.4.4`
  through `install._resolve_spec_with_origin`, so the install builds numpy and
  hits the 180 s timeout; the test also expects the tree itself as
  `installs[0]`, which holds only when no dependency resolves. Open decision:
  whether installs build local source copies of transitive dependencies.
- Failing on master, seen while running neighbouring files:
  `test_precise_stackmap_abi.py::test_aarch64_label_offsets_skip_normalizing_ordinary_instructions`
  (`.p2align 3` in `__text` rejected by `measure_text_layout`, since
  `73a154cba`); `test_self_obj_pcc_route.py::test_darwin_default_uses_pcc_writer_without_system_assembler`
  (its fake `emit_object` lacks the new `weak_undefined` keyword);
  `test_py_literal_splat_source_position.py` (asks for libpython, which the
  owned runtime does not include); and on a clean `1829e26d8` checkout:
  `test_container_emitted_ownership.py::test_literal_unwind_consumes_dynamic_string_temporary`
  (GC4 heap grows 26,296 bytes over 200 unwinds, limit 16,384; also at
  `b4cff8885`, before `0a8806592`);
  `test_slot_call_operand_roots.py::test_literal_integer_tree_publishes_every_result_before_cleanup`
  (expects `py_int_from_cstr`; over-lane literals are static since `1c0a63ab5`);
  `test_py_multi_file_compile.py::MultiFileCompileTests::test_borrowed_object_local_rebind_keeps_gc_root`;
  `test_py_class_kwargs_no_init.py::test_no_init_class_call_accepts_keyword_attrs_without_libpython`
  (expects keyword attributes on a class without `__init__`; CPython raises
  TypeError); `test_cross_module_none_return_abi.py::test_cross_module_declarations_match_definitions`
  (no `Helper_reset` definition in the combined IR);
  `test_test_infrastructure_efficiency.py::test_pcc_gate_linux_execution_markers_respect_simulated_platform[linux-Linux-x86_64-True]`.
- Not done: reading live bindings through a cached module dict and a static
  key (`py_module_attr_get` allocates both keys each time; a dozen tests pin
  its name); `ir_mutator._parse_function` matches `_BLOCK_LABEL_RE` per line
  through the generic call (3.6% of that pass phase; passes are off on the
  gate route).
- Code size: a call statement such as `total += check(i, fail_at)` is about
  116 AArch64 instructions with ~20 runtime calls (store_root 4, unpin 2.6,
  frame_leave_lifo 2.3, load_ptr 2, pin/err_occurred/release 1.5 each).
- Open, failing on master and not from these changes: GC4
  `test_dict_literal_temp_release.py[4]` (the splat branch loses the objects
  live across one GC4 cycle: `B21`, `B22`, `B22` at n=50; each shape alone is
  clean; n=10 none lost, n=200 two);
  `test_direct_indexed_syscall.py::test_ordinary_unsafe_write_worker_has_zero_capture_fallback`
  (no `syscall6` record when the test overrides `_target_triple` on a Darwin
  host); `test_direct_indexed_owned_passes.py::test_direct_worker_promotes_memory_before_pco_emission`
  (19 allocas before and after the default passes; independent of inline
  edges).

### Precise stack maps, step 3 (2026-10-19, `e1ec9b422`..`4441a1f48`)

Order chosen: 3 (consumer, differential first) -> 2 (safepoint pruning) -> 1
(compact encoding, shaped by the consumer's lookup) -> 4 (remove the
registered-slot protocol). Step 3 state, AArch64 Darwin:

- A table-enabled (`PCC_PRECISE_STACKMAP_TABLES=1`) Mach-O executable defines
  `_pcc_precise_stackmap_table`; its `main` publishes it to the runtime
  (`pcc_gc_precise_stackmap_table`). ELF (`elf_x86_64.py`, SHA-pinned: needs
  the maintainer's preimage approval) and PE links do not define it yet, so
  `main` publishes only on Darwin.
- `pcc.unsafe.frame_address()` reads x29/rbp. Call safepoints are now recorded
  at the return address (`cc81b34c1`; they followed the result moves before).
- The first walk validates the table once and builds an address index;
  `pcc_gc_visit_precise_frame` binary-searches a return address.
- `PCC_GC_STACKMAP_DIFF=1`: GC3/GC4 explicit collections and GC4 automatic
  steps compare mapped and registered roots both ways (`mismatches`,
  `unmapped`); all zero on the four-deep, root-shape and allocation-churn
  programs.
- `PCC_GC_STACKMAP_ROOTS=1|report`: GC3/GC4 mark and rewrite this thread's
  frame roots through the walk and skip the registered slots in the walked
  stack; the same programs print CPython's output (GC4 moves objects).
- Not covered yet: GC0-GC2, GC3 promotion (mode 2 uses registered stable
  values), pins/leases, other threads' stacks, compiler-sized workloads (needs
  a table-enabled Stage1), ELF/PE. Tests: `tests/python/test_precise_stackmap_table_policy.py`
  (Darwin-gated native cases use `pcc_stackmap_runtime_archive`).
- Step 2 (`4d35eb241`): tables omit call/loop records with no location,
  flag or exception edge (the walk skips such frames anyway): a table-enabled
  churn executable went from 223,637 records / 7.48 MB to 163,822 / 5.57 MB,
  same 385,966 location references. Further pruning by callee (calls that
  cannot collect) is left until step 4: the most frequent callees in the
  runtime are the registered-slot protocol itself (`pcc_gc_store_root` 15k,
  lease acquire/release 12k each, pin/unpin, write-barrier notes), which step
  4 removes with their records.
- Step 1 (`99472792d` format, then the link/runtime commit after it):
  `PCC_PRECISE_STACKMAP_FORMAT=3` makes the final Mach-O link (both the
  generic and the CPython staged route) transcode the merged version 2 table
  into the version 3 runtime table (`precise_stackmap.transcode_runtime_table`:
  8-byte records, deduplicated live sets, int32 slots, a rare table for
  exception/continuation fields). Objects, relocatable merges and semantic
  layout stay version 2; semantic layout refuses a version 3 table. The
  runtime indexes and walks either version. Root-shape program with the
  table-enabled runtime: table 5,674,120 -> 1,719,884 bytes, image 14.9 ->
  10.9 MB (166,700 records share 3,192 live sets); format 2 and 3 pass the
  differential and stack-maps-alone tests. Default stays 2. ELF/PE links keep
  version 2. Incremental Mach-O link images are keyed by the format, and
  inputs with a stack-map section are never layout-patched (the merged table
  is rebuilt, not concatenated). Not run: pcc1/bootstrap with the new linker
  code, a format-3 Stage1.

### pcc1 prior-worker gate: IR size (2026-10-19)

- `Verify the new pcc1 on native prior worker shapes` (300 s) has not passed
  on any platform since it first ran (`c09bf73e7`, 10-15); the 10-14 pass was
  the pcc0 preflight. With pcc0 the fixture is 16 modules, 51.5 MB of IR
  (passes off), 559k instructions, 49% calls; protocol calls dominate
  (`frame_leave_lifo` 34k, `store_root` 22k, foreign leases 29k, pin/unpin
  18k). Module tops were 10.8 MB; the largest function was
  `_pcc_py_module_top_pcc_unsafe` (4.53 MB, 216 trapping defs that never run).
- Capture-free definitions with a static signature (constant defaults,
  literal docstring) are now built by one runtime call,
  `py_func_new_static_def`, instead of ~80 inline calls per `def`: fixture
  IR 51.49 -> 43.68 MB, module tops 10.84 -> 4.40 MB. Five-collector native
  test: `tests/python/test_static_function_definitions.py`.
- Where a pcc1 compile of that fixture spends its time (98422a5b6 Stage1,
  local, contended run of 618 s; uncontended 458 s): frontend codegen 71 s of
  worker CPU (pcc0: ~5 s); emit 322 s in three sequential lanes (oversized
  119 s, medium 80 s, small 123 s); link and codesign ~190 s. Profiles
  (`scripts/pcc_flamegraph.py cpu`, symbols fixed in a5bdde1d8):
  - emit worker: 91% in the pcc-Python regex engine under
    `self_backend_parse._resolve_declaration_type_attributes`; replaced by
    str.find in cf25cbc26;
  - link tail: `_prepare_executable_image` 32%, codesign SHA-256 24%,
    `MachOStruct.pack` 21% (positional nlist packing, 5917b29bf),
    `_Out.__eq__` via per-symbol `list.index` 8% (identity map, d8feef130);
  - other workers: ~40% in GC protocol calls (store_root, leases, frame
    enter/leave, minor-graph lock), 5% `_tlv_get_addr`;
  - cross-module attribute reads through an `Attr` receiver
    (`self.parent.env`) lower to by-name `py_obj_getattr` even when the
    receiver's class is known: the field fast path in attr_load_lowering
    handles only `Name` receivers. L1CodeGen has 227 fields, scanned linearly.
- Local Stage1 of `0272b8f4f` (snapshot `/Users/jiamo/pcc-snapshots/s0272b8f4f`,
  `stage1_driver.py`, passes off): rc 0 in 1531 s; pcc1 424,850,744 bytes
  (98422a5b6: 459,829,544). Includes static defs (510b5e094), the emit and
  link fixes above, the Attr receiver field path (ed25f93d8), static str
  literals without pins (90138fd9b) and `IRBuilder_call9` (362e27bae: the
  nine-argument static def call made every Stage1 since 510b5e094 fail to
  link pcc1 on `_user_pcc_ir_ir_IRBuilder_call9`; this was also the cause of
  the passes-on link failure, not the passes). That pcc1 compiles the prior
  fixture (`measure_prior.py`, uncontended) in 251.5 s, rc 0 (458 s before):
  emit lanes oversized 45.5 s, medium 27.4 s, small 34.5 s (were 119/80/123).
  The gate's own node (10 native executions of the output) was not rerun.
  Not in that Stage1: omitted str defaults bound directly (72f03f44a).
- Still open: `pcc.unsafe`/`pcc.extern` are compiled although their
  from-imports are consumed at compile time (5.8 + 1.5 MB); per-operand
  temporary frames and leases at every call; the x86_64 backend has no
  register allocator (x86_64 pcc1 ~5.4x slower than aarch64); emit throughput
  (assembly text round trip). Local Stage1 of `98422a5b6` (snapshot
  `/Users/jiamo/pcc-snapshots/s98422a5b6`, 2144 s, pcc1 460 MB) is the
  baseline for timing the gate locally.

### pcc1 prior-worker gate: time (2026-10-19 evening)

- Record: `docs/investigations/pcc1-prior-gate-compile-time.md`. Profiler:
  `scripts/profile_compile_phases.py` (guarded compile, worker + link
  samples, `/usr/bin/time` CPU; finds the compiler by executable path).
- Landed: native workers share the phase priors (5a9d434dc; codegen tasks
  were admitted one at a time), shared return exit (14315bddf), direct
  indexed route default for host executables with IR passes off (116c8cfce;
  the gate runs passes off), per-declaration name scan without regex
  (3a334e1e5, fix 7ab406f25), radix relocation order (fd804086c), one-reader
  Mach-O symbol tables (04dd3fdce), emission sink constants (827f157d6),
  relocation records as a list via `macho_obj.new_relocation` (c452f9f90).
- Local prior compile, gate environment, quiet Mac (12 cores): text route
  251-260 s -> v4 Stage1 164 s / 512 s CPU -> v5 (through 827f157d6)
  147.6 s / 450 s CPU: 16 s export/summary, 63 s parallel codegen, 68 s
  serial link (snapshots `/Users/jiamo/pcc-snapshots/sv4`, `sv5`).
- Self time of a pcc1 worker/link is ~45-49% GC roots/barriers/locks, 11-13%
  allocation, 10-11% refcount; the slot-call lease/root-handle protocol is
  21-25% inclusive. Imported constants are live module bindings
  (`py_module_attr_get`, ~14% of a worker).
- CI dispatch 39256241026 at c452f9f90 is the first run with these. Open:
  ELF/PE link not profiled (`elf_x86_64.py` is SHA-pinned by
  `tests/test_elf_*_source_controls.py`); the x86_64 backend has no register
  allocator, so x86_64 Linux/Windows remain the hardest gate platforms.
- CI 39256241026 (c452f9f90), native pcc1 prior: macOS arm64 TIMEOUT (3
  codegen workers until ~230 s, then the coordinator still linking at 300 s;
  tree cap 2.97 GB); Linux aarch64 killed at 300 s in ELF relocation (745k
  relocations, 19/515 sections), codegen done at 246 s with 800 s summed
  worker time; Linux x86_64 also timed out.
- Same hashlib codegen manifest, same Mac, pcc1 vs CPython: 42.3 s vs 4.68 s
  (9x, roughly uniform across phases). A sampled pcc1 worker: 45.7% GC
  roots/allocator index, 7.9% refcount, ~9% the compiler's own code; foreign
  leases 10.7% and `pcc_gc_store_root` 10.1% inclusive. Codegen order follows
  the memory prior, so hashlib (105 s on CI arm) started at 139 s.
- Linux-only: `object_encode` is 181.7 s of the CI arm backend (hashlib 27.2 s
  vs 0.14 s under CPython). Mach-O (`native_object`) and COFF encoders join
  chunks; ELF (`elf_x86_64.emit_relocatable`, `arm64_elf_driver`) appends
  `bytearray += pack(...)` per relocation/symbol. In pcc runtime a bytearray
  has inline storage, so `+=`/`extend` allocate a new object and copy byte by
  byte: O(N^2), and `a += b"y"` rebinds (an alias keeps the old bytes;
  `a.extend` on a dynamic receiver raises AttributeError). Editing
  `elf_x86_64.py` needs approval of its SHA pins; the in-place bytearray
  (separate growable buffer) is the root fix.
- Windows preflight `test_windows_tar_attrs_native` 0xC0000005 (since
  2026-10-15): 8958403fc reports unhandled faults (code, RVA, RBP frames) and
  uploads the test's exe; map RVAs with `.pdata` size alignment against a
  local cross-build. Cause: a program inside a pcc tree compiles both
  `pcc.stdlib._compression_stream` and `_compression_stream`; the
  `except ImportError` alternative won the static binding, so gzip's class
  read an uninitialized base (NULL+0x28 in `py_class_new`); macOS in-tree
  segfaults the same way. Fixed in 68d379245 (`import_alternatives`).
- Native ELF encoding (host-pcc0-compiled `emit_relocatable`, Mac): the
  CI-hot hashlib Linux object (63,411 relocations) took 13.7 s; joined
  chunks (this change, pins repinned with approval) 2.06 s; 191 small
  runtime objects 18.9 s -> 10.2 s with the struct change. CPython: ~0.3 s
  for all 191, so pcc stays ~30x slower there: `Struct.pack` was 47.7%
  (2**64 bigint literal reparsed per field, bigint bounds, a bound method
  per call). Struct now packs without bigints; int literals outside the
  tagged lane become static immortal objects.
- Code size of pcc1 (60 sampled functions): a `bl` every 5.6 instructions;
  calls are GC frame enter/leave 11.2k, store_root 5.5k, leases 5.7k and
  ~13k error-path calls (traceback source, exception swap, cleanup); 4.1%
  `nop` separators from stack-map terminator routes, 7% unconditional `b`.
  Unbound checks on definitely-bound locals are gone (439d8274e); next:
  shared error-path blocks and leave/cleanup exits.
- Pre-existing failures at ed28d685f (same on the baseline snapshot), open:
  `test_exception_constructor_owned_result.py` (6),
  `test_exception_raise_lifetime.py` (3),
  `test_exception_chaining_wiring.py` (2),
  `test_native_exception_context_chaining.py::test_handler_codegen_scopes_active_context_to_function_boundary`,
  `test_ir_py_with_struct_port.py::test_ir_py_shared_float_bits_helpers_are_compiled`.
- Pre-existing failures at ed28d685f, unrelated to these changes:
  `test_dynamic_import.py` (import_module 'math', reload),
  `test_cpython_compat_cext_import.py`,
  `test_native_local_from_import_values.py::...real_ir...`,
  `test_native_package_star_import.py::..._all_dunder_names`,
  `test_recursive_stdlib_import_codegen.py` (3),
  `test_isinstance_imported_type_name_shadows_builtin.py::test_the_guard_reads_import_bindings`.

### 0.1.8 release readiness (2026-10-18)

- macOS arm64 Stage1 at `85650ac4b` (local, `scripts/bootstrap.py --stage 1`,
  auto cap 9.78 GB): rc=0 in 2730 s; the owned link passed `fixups`, so
  `10be0ed89` holds on the real image. pcc1 is 1,686,784,024 bytes (gzip -6
  385 MB; the 10-13 build was 674 MB before precise stack maps), above PyPI's
  default 100 MB per-file limit. It runs `--help`, links only libSystem, and
  compiled a function-bearing program with no host Python/compiler; the
  program prints 42 under GC0-GC4.
- That compile took 108 s, 88 s of it in the owned Mach-O link (host pcc0:
  3.8 s on the same input). `012ec3265` trims the per-relocation work there:
  its Stage1 (2133 s locally) links the same program in 68.6 s (70.6 s total,
  GC0-GC4 print 42). In that pcc1, `_iter_folded_relocation_rows` is 61% of
  the link and ~45% is `Relocation(...)` built through `py_module_attr_get` +
  `py_obj_call_slots` + kwargs merging, while the closed-world compile of the
  same module constructs it statically (`py_instance_new` + direct
  `Relocation.__init__`).
- Root cause of that dynamic construction: in multi-module compiles (CLI
  multi-file programs and Stage1 workers) an imported name or module attribute
  is a live import binding, so `R(...)` / `mod.R(...)` / `mod.f(...)` go
  through `_emit_slot_call_object` (`call_expression_lowering._emit_call`, and
  the `live_import_expr_binding` branch of
  `native_modules._maybe_emit_native_module_alias_call`) before any static
  binding rule runs. Probe built by host pcc0 at `012ec3265` (1M
  constructions of a frozen slots dataclass): imported + keywords 17 s,
  imported + positional 5 s, same-module 1 s; CPython ~1 s. The closed-world
  check (`compile_contextual_per_module_fallback_counts`) compiles the same
  calls statically, so it does not represent Stage1 lowering here. Binding an
  import statically needs a proof that the importer never rebinds the name
  (and an answer for import cycles); not changed. The proposed proof (a
  closed-world write-escape analysis with program-wide disabling) is section 7
  of `docs/research/reconstructed-runtime-research-directions.md`, which also
  ranks the other concepts taken from ahead-of-time compilers (sections 5-12;
  none implemented).
- macOS wheel from `012ec3265` (`PCC_BUILD_PCC1=<that pcc1> uv build --wheel`):
  `python_cc-0.1.7-cp315-cp315-macosx_26_0_arm64.whl`, 430,773,781 bytes.
  `scripts/ci_native_wheel_gate.py` passes. In a fresh venv `import pcc`,
  `python -m pcc` (4 s compile) and `pcc1` (75 s compile) each build a
  class/dict/exception/big-int program that matches CPython on GC0-GC4 and
  links only libSystem. The wheel exceeds PyPI's default 100 MB file limit
  (open release decision). Its tag was inferred from the build interpreter
  and host (`cp315`, `macosx_26_0`); the hook now writes `py3-none-<platform>`
  (`macosx_12_0` from pcc1's minos, `manylinux_2_17` for the static ELF,
  `win_amd64`), and the rebuilt `py3-none-macosx_12_0_arm64` wheel passes
  `ci_native_wheel_gate.py` under Python 3.13 and 3.15.
- Windows at `169bcfca1`: GC0-GC3 compiled and ran the canary; GC4's canary
  compile hit the stage deadline. Linux x86_64 and aarch64 built pcc1 and
  timed out in the pcc1 prior-worker gate (aarch64: one oversized emit worker
  ran 291 s of the 300 s budget).
- CI run 39058771517 at `191984ced`: macOS arm64, Linux x86_64 and Linux
  aarch64 each built Stage1 pcc1 (Linux also passed the five-collector
  canary), then each hit exit 124 at the 300 s budget of
  `test_size_prior_native_processes_five_collectors[pcc1]` (pcc1 compiles the
  driver, then runs it under GC0-GC4; locally a one-function pcc1 compile is
  70 s, mostly the owned Mach-O link). Windows failed inside Stage1/canary
  (`bootstrap_platform.py --gc all --stage 1`, rc=1) and in the preflight
  `tests/python/test_owned_windows_exit.py` run.
- Where that 300 s goes (compile logs in the run's `bootstrap-*` artifacts,
  `PCC_PY_FRONTEND_WORKER_TIMING=1`): the gate compiles
  `tests/fixtures/native/worker_size_priors.py` plus 15 imported modules (os,
  subprocess, hashlib, warnings, pcc worker modules, ...). Linux x86_64 spent
  232 s in the frontend alone; Linux aarch64 finished the frontend in 67 s and
  spent the rest in emit/link. Per module, x86_64 pcc1 is uniformly ~5.4x
  slower than aarch64 pcc1 on the same inputs (`worker_process_pool` codegen
  96.0 s vs 17.9 s; infer 1136 ms vs 208 ms; local macOS arm64 10.4 s), so the
  x86_64 gap is the native compiler as a whole, not one function. Host pcc0
  frontend is linear in a `+` chain (8/16/32/64 terms: 13/24/49/103 ms);
  pcc1 is ~20x that per term (199/412/868/2052 ms).
- The x86_64 self backend has no register allocator:
  `pcc/backend/self_backend_x86_64_linux.py` keeps every SSA value in a stack
  slot ("Every value lives in a stack slot") and `_materialize_value` reloads
  it at each use, while aarch64 (Darwin and Linux) uses
  `self_backend_aarch64_darwin_regalloc.py` (2253 lines). Windows x86_64
  emits through the same `_emit_x86_64_module`. x86_64 pcc1 is itself built
  by this backend, which is consistent with the uniform ~5.4x gap and the
  Windows GC4 canary deadline; how much an x86_64 allocator recovers has not
  been measured.
- The IR is the other half: this program lowers to 65 MB of IR in 16 modules
  (`worker_size_priors` alone, 278 lines: 5.9 MB). `run_case` (90 lines) is
  25,538 instructions: 9,894 calls, of which slot-call/GC protocol dominates
  (`pcc_gc_frame_leave_lifo` 1304, `pcc_slot_call_status_report` 897,
  `pcc_gc_store_root` 895, lease acquire/release 1017, exception frame source
  430) while the Python operations themselves are tens (`py_obj_subscript`
  40, `py_list_append` 40); 6135 bitcasts and 4365 blocks. Locally (M-series,
  wheel pcc1 from `012ec3265`) the frontend finished in ~1 min and the emit
  lane ran > 5 min: emit samples show 58% in the assembly text round trip
  (`AArch64ModuleBuilder._append_line` 35%, `_finalize_text_entries` 20%),
  and shards >= 2 MB (single huge functions) run in the width-2 oversized lane.
  That whole local compile took 884 s wall / 1249 s user and the output runs.
- Native emit width: every compiled (pcc1) emit worker lane was fixed at
  `COMPILED_NATIVE_SAFE_JOBS = 2` regardless of cores or budget, set in
  August when one compiled worker could take several GiB. Replayed shards with
  the `012ec3265` pcc1 (`pcc1 --pcc-self-backend-emit-batch-worker`,
  `/usr/bin/time -l`): 0.82 MB 10.5 s / 149 MB, 0.99 MB 12.7 s / 160 MB,
  1.86 MB 24.2 s / 197 MB, 2.2 MB 27.5 s / 218 MB, 7.3 MB 112 s / 593 MB
  (the 0.82/1.86 MB shards are from the Stage2 module
  `pcc.frontends.python.codegen.call_object_lowering`). That is ~80 MB plus
  <= 0.1 GB per input MB, about a tenth of the 1.3-1.4 GB/MB the oversized
  lane's 7 MB byte cap was calibrated on, and ~78 KB of IR per worker-second.
  `0b8875c79` admits sub-threshold compiled emit workers at 512 MiB each under
  a declared `PCC_WORKER_TREE_BUDGET_BYTES` (after the resident coordinator,
  capped by the CPU width); without a budget the fixed two stays. At ~830
  CPU-s of emit for this program (local speed; ~1.7x more on the aarch64
  runner) a 4-wide lane still cannot fit the 300 s gate by itself.
  Local Stage1 of that change (pre-rebase `804a39ce9` on `28ff7ca97`): rc=0
  in 1992 s. Same pcc1, same fixture compile, byte-identical output: no
  budget 826.2 s, 16 GiB budget
  (`scripts/run_process_tree_sample.py --max-tree-rss-bytes 17179869184`)
  623.7 s, peak tree RSS 1.75 GB. In the budgeted run the frontend took
  ~72 s, the coordinator ~55 s alone, the oversized lane ~256 s with one
  worker, the safe lane 85 s eight-wide, and the link ~150 s; frontend lanes
  also read the budget, so not all of the 202 s is the emit change.
- `739492c54` stops emitting identity pointer casts (`bitcast ptr %x to
  ptr`): they were 148,767 of 722,369 instructions (20.6%) and 19.2% of the
  65 MB of IR in that compile, and the x86_64 backend lowered each to a slot
  load/store. `59cf47f2b` widens oversized compiled emit waves under a
  declared budget (128 MiB plus 128 bytes per input byte per worker). Local
  Stage1 of both (pre-rebase `5fde2ac2d` on `804a39ce9`): rc=0 in 1424 s,
  user 4914 s (previous Stage1 1992 s / 6926 s, partly overlapping other
  local load), pcc1 1,560,845,496 bytes (-7.5%). That pcc1 compiles the
  class/exception/big-int program with no flags in 73 s; it matches CPython
  under GC0-GC4 and links only libSystem. The 16-module fixture under the
  16 GiB budget: 410.1 s (was 623.7 s), peak tree RSS 1.35 GiB: frontend
  61 s, coordinator alone 46 s, oversized lane ~80 s three-wide, safe lane
  ~70 s eight-wide, link ~137 s.
- Tests already red before the identity-cast change (snapshot of
  `804a39ce9` = `28ff7ca97` plus the emit-width config, local CPython
  3.15.0rc1): `tests/python/test_class_definition_defaults.py`
  9/18, `test_py_typed_int_unboxed.py` 7/47,
  `test_virtual_thread_spawn_slot_ownership.py` 27/36,
  `test_py_multi_file_compile.py` 5/46,
  `test_globals_owned_result.py::test_globals_owned_result_ir` (both targets),
  and three native executions in `test_virtual_thread_park_effect.py` where a
  may-park method chain prints `None` instead of `42` (a semantic bug).
  Also red before it (snapshot of `0b8875c79`):
  `test_raw_scaffold_exact_container_subscript_ownership.py` 18/19,
  `test_py_class_export_schema.py` (method registration and cross-module
  schema), `test_generator_result_lexical_root.py` (cleanup predecessors 2 vs
  >= 4), `test_callback_sync_context.py::test_callable_roles_in_actual_threaded_ir`
  ("no matching callable constructor"),
  `test_class_attribute_owner_lifetime.py::test_scalar_class_read_keeps_owner_until_conversion`,
  `test_owned_ifexpr_local_transfer.py::test_owned_ifexpr_ir_normalizes_branches_and_tracks_target`.
- pcc1 under `PCC_GC_BACKEND=4`: the `012ec3265` wheel pcc1 compiling the
  IfExpr canary from `test_owned_ifexpr_local_transfer.py` fails in 5 s with
  "'dict' object has no attribute 'values'"; GC0 compiles in 73 s and GC3 in
  242 s. The current pcc1 GC4 case exceeds that test's 300 s compile budget.
  The `5fde2ac2d` pcc1 under GC4 did not finish that compile in 1200 s.
  `sample` put 77% of it in `pcc_gc_backend4_sweep_deferred_recycles` and
  15.6% in `pcc_gc_backend4_evacuation_page_find` (a linear walk of the
  evacuation list). An lldb walk of the live page list (scratchpad
  `r12/gc4/walk.py`) read the deferred counter at 28 then 287 within 30 s,
  equal to the flagged pages, every one still carrying forwarding entries
  (`page+96 > 0`) and none completable, over 6834-7334 live pages: the sweep
  could never reach zero and walked the whole list on every top-level
  cascade. `7f7f2cd5d` counts only pages the sweep can complete (flag 1) and
  leaves forwarding-blocked pages (flag 2) to forwarding retirement. With
  that runtime, host-compiled programs match CPython under GC0-GC4 and the
  GC4/relocation test files pass apart from tests already red before it
  (`test_freestanding_gc_state.py` global count 201 vs 200,
  `test_gc_backend4_production.py` quarantine source contract). Local Stage1
  with it: rc=0 in 1478 s. Its pcc1 still does not finish the GC4 IfExpr
  compile in 1200 s (it reached the Mach-O link this time); the sweep is gone
  from the profile and the time is now `pcc_gc_backend4_evacuation_page_find`
  59.6% and `pcc_gc_backend4_retarget_mutator_payload_locked` 25.2%.
- Open GC4 issue, measured in that run with lldb (scratchpad `r13/lists.py`,
  `r13/evac.py`): the evacuation page list held 18,108 nodes (18,093
  distinct pages, so 15 duplicates; 266 of them no longer on the live page
  list; 5 live pages on it without `page+108`) against 56,566 live pages; the
  store buffer held 8,384 entries and the remembered set 18,837.
  `evacuation_page_find` walks that list on every allocation fast path, and
  each list/set payload growth walks the whole store buffer and remembered
  set (`pcc_gc_remembered_member_maybe(owner) == 0` could skip the latter).
  Stale nodes naming pages that left the live list are a correctness risk
  for evacuation, not only a cost; replacing the walk with the `page+108`
  flag needs that membership invariant fixed first.
- The next commit after `98c814750` keeps both walks' answers exact and
  skips them: `evacuation_page_find` reuses its single-threaded answer while
  `pcc_gc_backend4_reseed_page_revision` (bumped at every list push/unlink)
  is unchanged, and payload retarget skips both remembered walks when
  `pcc_gc_remembered_member_maybe(owner) == 0`. The GC state count pin is 204
  (the source already held 201 against its 200; three memo globals). With
  that runtime the five-GC programs match CPython and the GC4/relocation
  files pass except `test_gc_backend4_production.py::test_backend4_relocation_stress_stable_ids_and_no_old_addresses`
  (relocation set stuck at 8 for 16 rounds), which fails the same way on a
  snapshot of `0b8875c79`.
- Root cause of that stall (probe in scratchpad `r14/stress*.c`): the eight
  stuck members were empty lists whose copies all failed in
  `pcc_gc_relocation_payload_raw_validate_locked`, because
  `pcc_gc_backend4_zpage_payload_span_preflight_locked` required the 32-byte
  payload to fit after the copy target's page tail, and each retry allocated
  the target into the same nearly full page. The set never emptied, so
  `pcc_gc_backend4_remap_and_retire_stopped_world` never advanced the remap
  epoch and no forwarding retired - the same shape as the pcc1 GC4 compile's
  forwarding-blocked pages and growing evacuation list. The span-placement
  commit admits such copies and publishes their spans as external payload
  (offset -1), as `pcc_gc_backend4_zpage_register_owner_payload_span` does;
  the probe then forwards all 64, empties the set at round 0 and retires all
  forwardings by round 1 (remap epoch 2). `fbd569dee` is that change;
  `6f1e72f76` the find memo and retarget skip. Local Stage1 at `fbd569dee`:
  rc=0 in 1487 s; its pcc1 compiles the class/exception program at GC0 in
  74 s and the output matches CPython under GC0-GC4. Per-node comparison of
  the 162 `test_gc_backend4_production.py` nodes against a `0b8875c79`
  snapshot: identical except the stress test (now passing); 6 fail on both.
- The `fbd569dee` pcc1 still did not finish the GC4 IfExpr compile in 1200 s
  (on a host also running the default suite). A sample at 5 min showed the
  previous hot spots gone and `_backend4_store_buffer_remove` at 60.8%
  (every object free walks both store-buffer queues) plus
  `_backend4_store_buffer_owner_count` at 21.7% (a quadratic distinct-owner
  count on every enqueue, for high-water telemetry). The next change keeps
  an exact owner index for store-buffer entries
  (`pcc_gc_store_buffer_member_*` in `freestanding_gc_index_table.py`):
  frees skip owners it proves absent and the distinct count is the index
  size; a failed index allocation or a dropped medium-flush entry taints the
  epoch, which falls back to the walks until the queues are cleared. With it
  the five-GC programs match CPython and the 162 GC4 production nodes match
  the previous run (same 6 failing on both); `tests/python/test_gc_store_buffer_membership_native.py`
  covers the index (`eb3af24c1`). Local Stage1 and the pcc1 GC4 timing are
  not yet rerun.
- vthread may-park methods: `self.leaf(v) + 1` inside a may-park method
  resumed to `None` (the 42-vs-None failures above). The consumer's output
  slot made `_emit_method_call` require runtime binding, and that dynamic
  call handed the consumer the continuation object instead of driving it.
  Local and external may-park methods in a resumable caller now take the
  static path, whose delegation publishes into the consumer's slot.
  `test_concrete_may_park_method_chain_resumes_to_python_result` passes
  (`160340f19`) and
  the program prints 42 under GC0-GC4. The two cross-module variants still
  print `None`/`41`: the imported class is bound at run time, so the call is
  fully dynamic; fixing that needs a run-time continuation check on dynamic
  calls in resumable callers (as `pcc.virtual_thread.call` does). Among the
  35 vthread/generator test files the remaining failures are red on the
  HEAD snapshot too, including `test_virtual_thread_frontend.py` join
  ("slot-call Call lacks an immediate owned-result handoff").
- Publish workflow dispatch 39099696332 on `db7c8e812`: the macOS job
  failed at once because `scripts/ci_worker_size_prior_gate.py` requires
  the platform baseline Python (3.15.0rc1 on macOS) and the job passed
  `--python 3.13`; the matrix now selects it per platform.
- Publish workflow dispatch 39101937576 on `52bdc0917`: all four wheel jobs
  passed (Stage1, wheel, `ci_native_wheel_gate.py` on the runner and in
  Docker): macos-arm64 72 min, linux-aarch64 53 min, linux-x86_64 102 min,
  windows-x86_64 130 min. The sdist job failed: hatch ran the build hook for
  the sdist target, which built the runtime and pcc1 and hit the 10-minute
  self-compile limit. `f4f06c9a9` returns early for `target_name == "sdist"`
  (`tests/test_wheel_tag.py::test_sdist_target_builds_neither_runtime_nor_pcc1`;
  local `uv build --sdist`: 13.5 MB in 24 s). Not yet re-run in CI.
- Qualification run 39101921646 on `52bdc0917`:
  - macOS arm64 `--phase pcc1`: `test_size_prior_native_processes_five_collectors[pcc1]`
    hit the 300 s guard (status TIMEOUT, rc -15; the largest process was an
    oversized emit worker at 193 s). Same open gap as above.
  - Linux aarch64 five-collector canary: pcc1 compiling the GC4 canary
    finished `artifact_link` (223.5 s) and `artifact_cleanup`, then died
    with SIGSEGV; GC0-GC3 compiled and ran. In the publish run at the same
    commit, the same `--phase stage1` command passed the GC4 canary (link
    224.8 s). So the GC4 crash is intermittent and comes at the end of a
    completed compile. Not reproduced locally.
  - Windows preflight: `test_windows_tar_attributes.py` (0xC0000005, empty
    stdout), the same failure as run 39082284048 on `6f3f42bc6` (before
    `739492c54`). The Windows worker-prior job has failed in every completed
    run since 2026-10-16.
- Mac -> Linux x86_64 cross-compile at `f4f06c9a9`:
  - Runtime: `python -m pcc.frontends.python.owned_runtime_build --target
    x86_64-unknown-linux-gnu`.
  - A two-module program built with `--target x86_64-unknown-linux-gnu`
    prints 42 on a real x86_64 Linux host under GC0-GC4.
  - The cross-built pcc1 is a 1.74 GB static ELF that took 2004 s to build
    locally. On that host it runs `--help`. With no host Python or compiler it
    compiled a one-function program in 190 s, and that program prints 42
    under GC0-GC4.
  - A pcc1 outside a wheel needs `PCC_SOURCE_ROOT` naming a tree with
    `pcc/`, `scripts/bootstrap.py` and `scripts/pcc_link_macho.py`. Those
    two scripts are inputs to the codegen checksum (Mach-O linker identity
    included even for Linux targets). Without them the only diagnostic is
    "current codegen checksum is unavailable", because diagnostics
    deliberately omit paths.
- Default-suite harness bug: `test_bootstrap_performance_manifest.py::test_bootstrap_stage_result_declares_metric_scope_and_pairing_contract`
  ran `run_stage` with the default out dir. That overwrote the checkout's
  `build/bootstrap/pcc1` with the 12-byte text `stage binary`, and every later
  pcc1-gated test in that checkout then failed with "Exec format error".
  The test now uses `tmp_path`.
- Executable size: the precise stack-map table, not code, is most of pcc1.
  - Local macOS Stage1 pcc1 (`stage1_gc4c`, 1,560,831,224 bytes): `__TEXT`
    430 MB, `__pcc_stackmaps` 1,074,289,080 bytes (69%), other data 17 MB.
  - Cross-built x86_64 ELF pcc1 (1,744,596,576 bytes): a 653 MB executable
    segment and a 1,086,307,936-byte read-only segment that holds only
    `.pcc_stackmaps` (`elf_x86_64.py` places it there).
  - Inside the macOS table (scratchpad `smap/parse.py`): 32,326 functions;
    18,776,484 safepoint records of 32 bytes each, 601 MB (13.8M call, 4.2M
    loop, 0.7M exception, 36 continuation); 29,525,446 interned location
    entries of 16 bytes each, 472 MB, holding only 7,716 distinct values.
  - The location slices are interned per object and then concatenated at
    link. The 735,916 slices are already distinct by content (average 40
    entries), so link-time dedup cannot shrink them. The cost is the
    encoding: a per-function slot table plus one bitmask per distinct live
    set, with varint records, would describe the same roots in about a tenth
    of the space (estimate, not built).
- No production code reads that table. `pcc_gc_consume_precise_stackmap` has
  no caller outside its definition and header declaration. Its docstring and
  `self_backend_precise_stackmaps.py` both state that the registered-slot
  protocol is authoritative. Project intent still names stack maps as part
  of the target runtime.
  - Change: objects carry the table only under
    `PCC_PRECISE_STACKMAP_TABLES=1`. Safepoint planning still runs, so the
    emitted code is identical: assembly differs only by the section on
    aarch64-darwin, aarch64-linux and x86_64-linux.
  - The setting is part of the object-cache identity.
  - `tests/python/test_precise_stackmap_table_policy.py` covers it. The
    default and opt-in executables match CPython under GC0-GC4, and only the
    opt-in one contains the `PCCSMAP1` magic.
  - The runtime archive built at this source is 19.8 MB (macOS).
  - Local macOS Stage1 at this change (`scripts/bootstrap.py --stage 1`, git
    snapshot): rc=0 in 1898 s. pcc1 is 478,153,704 bytes, down from
    1,560,831,224. `__TEXT` is still 430.7 MB, `__DATA` 17.1 MB, and there is
    no stack-map section. gzip -1 gives 158 MB, down from 416 MB. With no
    flags, that pcc1 compiled a class/exception/big-int program in 66.6 s;
    the output links only libSystem and matches CPython under GC0-GC4.
    Stage2/Stage3 were not run.
  - Before precise roots become the production walk, the compact encoding
    and pruning of safepoint records for calls that cannot collect are still
    needed.
- `50b8b0979` (remote agent) fixes a defect in the table opt-in: the skipped
  `build_aarch64_stack_map_section` was also where AArch64 packed plans were
  closed, so they were never retired when tables were off. The fix adds
  `tests/python/test_aarch64_stackmap_plan_ownership.py`.
- Module initialisers are 103 MB of the 430 MB pcc1 text: `module_top` is
  73.3 MB across 557 modules and `module_init` 29.7 MB across 371. Symbol
  sizes come from the Stage1 pcc1 `nm` map (scratchpad `smap/textsize.py`).
  - A module-level `def f(a, b=1)` with a docstring cost 373 IR instructions
    (289 calls) in its initialiser. The cause is the five-tuple signature:
    four `py_tuple_new`, a `py_tuple_set_item` per field, and the
    pin/lease/status protocol around each.
  - `8b8b41c0b` builds a signature whose defaults are all str, small int,
    None or bool from one read-only descriptor plus
    `py_static_aggregate_build`. This gives 254 instructions (194 calls) per
    def. `py_static_aggregate_build` gains None/False/True leaves (markers
    10/12/14).
  - The macOS runtime archive drops from 19.8 MB to 17.2 MB.
  - `tests/python/test_static_function_signature.py` matches CPython on
    GC0-GC4. The failing nodes of `test_class_definition_defaults.py` are the
    same nine as on the HEAD snapshot.
  - The remaining ~112 calls per def are nearly all slot-call protocol around
    captures, construction, metadata and `__doc__`.
  - Local macOS Stage1 at `8b8b41c0b`: rc=0 in 2381 s. pcc1 is 458,587,352
    bytes (was 478,153,704); `__TEXT` is 407.1 MB (was 430.7 MB); gzip -1
    gives 150 MB. With no flags it compiles the class/exception/big-int
    program in 60.4 s (was 66.6 s); the output links only libSystem and
    matches CPython under GC0-GC4.
  - Local macOS Stage1 at `99f696d50` (complete git snapshot, runtime built
    inside it): rc=0 in 2041 s. pcc1 is 458,780,328 bytes. It compiles the
    same program in 67.6 s under default-suite load, and GC0-GC4 match
    CPython. The default suite is running against this pcc1 and an archive
    from the same snapshot, with tests refreshed to `e190cc4f3`. `pcc/` and
    the checksum scripts are unchanged since `99f696d50`.
- Default-suite reds fixed on 2026-10-18 (each compared with a snapshot of
  the pre-change commit first):
  - A real GC-safety gap. A fully supplied keyword call of a method with
    definition defaults went through `py_obj_call_method_kwargs`, with an
    unrooted callable, args tuple and kwargs dict. `1cfcf6f98` calls it
    directly when `_statically_bindable_call` holds.
  - Tests left stale by refactors:
    - `test_class_definition_defaults` (9 nodes): function objects now come
      from `py_func_new_signature_slots`, and inherited defaults are copied
      into rooted slots. The error edges are now checked by walking the CFG.
    - divmod owner release.
    - six identity-cast IR shapes.
    - `fcntl` and `pcc_errno_get` added to the stdio boundary pins.
  - Platform assumptions:
    - The APFS surrogate `mkdir` case now expects the same `EILSEQ` as
      CPython.
    - Stage1 checkpoint tests that call `build_identity` or hash the real
      interpreter are gated to x86_64 Linux. The route rules run everywhere
      through a modeled host, and so do the platform producer/consumer tests.
    - The guard CPU-accounting test now gives its worker a large enough CPU
      margin over the guard and launcher overhead.
  - Harness:
    - A test overwrote the checkout's `build/bootstrap/pcc1`.
    - Manifest evidence now has the same schema on every platform.
    - The `mkdir` invalid-integer case no longer uses `pytest.skip`.
  - Still red and understood:
    - `test_layer1_constants_cross_module_static_imports_stay_native`: the
      live-import item below.
    - The per-module fallback ratchet: seven ceilings are stale. The increases
      are cross-module `from pcc... import` and pcc-native stdlib imports,
      and they need the documented recapture once the multi-file totals are
      confirmed 0.
    - 30 `pytest.skip` calls remain in tests.
- Snapshot suite runs: a runtime archive built in the worktree does not
  validate in a snapshot. `codegen_checksum` hashes every
  `.py`/`.c`/`.h`/`.sh` file under `pcc/`, including untracked ones (the
  worktree has `pcc/ir_passes` and `pcc/runtime/_native`). Snapshots also
  need `AGENTS.md`, `run.py` and `UV_NO_SYNC=1`. Without the last, an inner
  `uv run` re-pointed the shared venv's editable install at the snapshot.
- Imported names are always live module lookups. In every managed
  (non-port) module, `_emit_name` checks `live_import_name_slot` before the
  native constant and export bindings. So each read of an imported function,
  class or constant is a `py_module_attr_get` wrapped in the slot-call
  protocol. This is the same cost as the 17x imported-constructor gap above.
  - `test_fallback_baseline.py::test_layer1_constants_cross_module_static_imports_stay_native`
    has been red since `983254526` for this reason: the class attribute
    initializer reads `IR_RUNTIME_COMPAT_MODULE` dynamically instead of
    storing the static `"pcc.ir.compat"`.
  - The live binding is needed in general, because another module may write
    `a.X`. Binding statically needs a closed-world proof that:
    - the importer never rebinds the name (assignment, `global`, `del` or
      star import);
    - no module writes it (attribute store, `setattr` or `globals()`).
  - Not changed yet.
- `cli_bootstrap` had 340 `py_cpy_*` calls in its standalone probe
  (`test_cli_bootstrap_package_schema_static_imports_stay_native`) from the
  function-local `pcc.package.build_exec` / `build_plan` imports. Static
  exports matching the closed-world exports field for field bring it to 0:
  return and parameter types, keyword-only kinds and `box_int_abi`.
  `_function_export` gains a `box_int_abi` argument.
- `L1CodeGen._initial_ir_available` (from `88cb6a02c`) is now in the host
  contract (`test_l1_codegen_host_contract_covers_constructor_state_fields`).
  `test_bootstrap_trace_filter.py` had a prefix mangled by the layout rename.
- Runtime archive builds now run in parallel. The owned build compiled its 181
  modules serially in one process (184 s at 97% of one core).
  - Workers (`--pcc-runtime-module-worker`, through the shared compiler worker
    pool) compile batches, largest sources first. The parent assembles
    members in canonical order. `PCC_RUNTIME_BUILD_JOBS` defaults to up to
    eight.
  - `tests/test_runtime_build_parallel.py` checks that the archive,
    `.capi_syms` and provenance are byte-identical to the serial build, and
    that a failed worker publishes nothing.
  - Full macOS runtime at `eb5ee441f` with the default width: 44.0 s wall at
    597% CPU, under load average 17-30. The serial build took 184 s. All 181
    objects are byte-identical to the serial build of the same compiler
    output.
  - Tests that patch builder internals in the parent pin
    `PCC_RUNTIME_BUILD_JOBS=1`, because workers cannot see the patches:
    `test_owned_runtime_capi_inventory.py`, `test_owned_runtime_selection.py`
    and `test_wheel_owned_runtime.py`. The remote agent did the same for
    `test_owned_runtime_pass_prefix.py` in `724ba09d9`.
- Precise stack-map table, compact encoding estimate over the real 1.07 GB
  table (scratchpad `smap/v3estimate.py`): 143 MB (7.5x). That splits into
  per-function slot tables (2.9 MB), per-function deduplicated live-set
  bitmaps (75 MB; the largest function has 6,093 root slots) and varint
  records (65 MB).
  - The ELF linker does not merge tables. It lays each object's table back
    to back in the metadata segment, so a compact ELF image needs
    `elf_x86_64.py` (SHA-pinned) changes.
- Six IR-shape tests still assumed the identity casts that `739492c54` removed.
  Each failed on HEAD, passed on a snapshot of its parent, and was rewritten
  to accept the direct operand or an alias. Every rewritten assertion was run
  against both the old and the new IR:
  - `test_set_algebra_receiver_ownership.py`
  - `test_ir_scaffold_variadic.py`
  - `test_gc_moving_safepoint_owners.py`
  - `test_super_call_result_roots.py`
  - `test_slot_call_exception_cleanup.py` (used by
    `test_native_container_builtin_error_paths.py`)
  - `test_freestanding_gc_external_resource.py`
  In the last one, a Darwin-only lookup that is a known-null callee on Linux
  used to be called through a `bitcast ptr null`. It is now a direct `call
  null`, which the self backend routes to the runtime's
  `pcc_null_callee_trap`.
- Release publication (`.github/workflows/workflow.yml`, runs on a published
  release in `allstoalls/pcc`, or `workflow_dispatch` to build without
  publishing): sdist plus one `py3-none` wheel per platform (macos-15 arm64,
  ubuntu x86_64/aarch64, windows-2022). Each wheel job builds its own Stage1
  through `ci_worker_size_prior_gate.py --phase stage1`, hands the recorded
  pcc1 to hatch with `scripts/ci_stage1_compiler.py` (receipt PASS, same clean
  commit and source hash, same binary hash), builds the wheel and runs
  `ci_native_wheel_gate.py` on the runner and, for Linux/Windows, inside
  Docker. A failing platform uploads no wheel; publishing needs the sdist and
  at least one wheel. The pcc1 prior-worker gate, suites, five-GC bootstrap
  and the no-libpython audit stay in the qualification workflow and local
  gates. Before this, the Linux/Windows qualification wheel step had no
  `PCC_BUILD_PCC1` and would have re-run hatch's 10-minute self-compile.
- The package version is 0.1.8 (`pyproject.toml`, `uv.lock`; `uv lock
  --check --offline` passes, installed metadata reads 0.1.8). The resync
  removed an undeclared `llvmlite` from the local `.venv`; nothing in the
  lock or the project depends on it.

- macOS Stage1 at `169bcfca1` (CI run 39031828792, artifact
  `bootstrap-macos-arm64`): all codegen finished (3574 s, peak tree RSS
  2.54 GB under the 2.77 GB cap) and the owned Mach-O link reached `fixups`,
  then failed: `_build_seg_info` packed `page_count` > 65535 into a uint16
  (`__text` span 529.6 MiB; `__DATA` file extent > 1 GiB of 16 KiB pages).
  `10be0ed89` bounds the starts table at the last fixup page and moves TLV
  descriptors ahead of DATA. The fixups that remain are the per-function
  addresses in `__pcc_stackmaps` (the pointer-free location table follows
  them), so whether the last one sits below 1 GiB is decided only by a real
  link; the CI run on `e0e6faa81` covers it. A local Stage1 was not run.
- Nested decorated defs: the hoist pass now evaluates decorators whose root
  is an enclosing local or a module-defined (non-imported) binding in the
  enclosing scope, before the defaults, and applies them each time the def
  executes. Before, a local decorator was refused ("Layer 1 does not handle
  decorators") and a module decorator on a nested def never ran. Imported
  decorators keep declaration-time handling. Test:
  `tests/python/test_decorator_application.py::test_nested_decorators_apply_where_the_def_executes`
  (pcc0, GC0-GC4). `constructor_expression_failures` now compiles.
- Type joins: `try` now joins every type that can reach the code after it
  (normal completion; for each handler that falls through, the pre-`try` or
  any partial-body binding unless the handler rebinds; `finally` replaces
  all), and an `if` arm counts a nested `def` as a binding. Before,
  `x = 37` then `try: x = some_function` then `x == 37` failed with "cannot
  coerce FuncType to int". On pcc's own modules this widens 21 locals to dyn
  (scan: `_typeconf_widen_try_join` retargets, 34 events), none in
  `pcc/runtime/`, all on error/observer/config paths or where the old type
  was wrong (`c_codegen.py:3979` `dim`). Test:
  `tests/python/test_try_rebinding_type_join.py` (pcc0, GC0-GC4).
  `constructor_exception_order` and `constructor_default_exception` compile.
  Still open: a decorated nested def that calls itself reaches the
  undecorated function directly.
- The Windows tar native cases (0xC0000005, empty stdout) crash before
  `main()`'s first flushed print. The same generated program compiled on
  macOS extracts and runs to its Windows-specific controls, so the crash is
  in Windows-only module initialization; no Windows host was available here.
- Stale pins updated (each shape re-checked first): function objects are built
  by `py_func_new_signature_slots` since `d0b8b6720` (one construction per
  callable, five tests); the `cell.payload` read publishes into a slot-call
  root and hands off no owner; `mem2reg` moved to `pcc/ir/optimization/` and
  has no nested functions; the sibling-cycle program now captures the later
  sibling's binding (matches CPython natively on GC0-GC4); the capture fixed
  point settles in one pass, so `referenced_sibling_names` runs once per
  nested function. `_module_bound_names` (from `8c7f55e4c`) read handler
  `.name`/`.body` directly; it now uses the stage-safe field accessor.
- `--python-libpython auto` only (the default `off` compiles it): a nested
  function that returns an arithmetic result of calling a captured callable,
  `def outer(f): def wrapped(x): return f(x) * 2`, fails with "slot-call Call
  lacks an immediate owned-result handoff". The captured `f` is a cell read,
  and the libpython-compat call path does not publish into the slot-call sink.
  This is what `test_same_module_bare_decorator_still_applies` hits.
- Still failing on a clean `1694b049f` snapshot:
  `test_hoist_global_cell_boundaries.py::test_host_ir_global_reads_do_not_become_capture_values`
  (both modes), the `_hoist_nested_funcdefs` length check (1810 lines there,
  1833 now, against 1800), `test_py_function_decorators.py::test_same_module_bare_decorator_still_applies`
  ("slot-call Call lacks an immediate owned-result handoff") and
  `::test_imported_bare_decorator_is_metadata_noop` (`singledispatch` refused;
  pcc's native `functools` has no `singledispatch`).
- Starred unpack: a captured `*rest` arrives as its cell subscript and an
  attribute `*obj.tail` was refused ("starred unpack target must be a bare
  name"); both now store like the other unpack targets
  (`test_py_native_closure.py`, the existing captured case plus
  `test_native_starred_unpack_into_captured_and_attribute_targets`, GC0-GC4).
- `test_py_nested_hoist.py::test_nested_hoist_caches_reject_reused_object_id`
  patched `id()` in `hoist_lowering` only, while `hoist_boxing` keyed
  `boxed_function_defs` with the real `id()`, so no def was found to bind a
  cell. The table is now an identity list; with or without the patch the
  probe's IR is identical to the unpatched IR.
- Linux aarch64 and x86_64 at `169bcfca1` both built Stage1 pcc1 and then
  timed out in `ci_worker_size_prior_gate.py --phase pcc1` (300 s): pcc1
  compiling `tests/fixtures/native/worker_size_priors.py` spent 291 s in one
  `--pcc-self-backend-emit-batch-worker ... emit_oversized_0.manifest`
  process (621 MB RSS) on aarch64 (artifact `bootstrap-linux-aarch64`,
  `worker-prior-gate/native-pcc1/result.json`).
- `pip install pcc` installs an unrelated PyPI project ("Predicate collection
  classes"); this distribution is `python-cc`, whose 0.1.7 has no wheels.

## Generated portable-runtime compiler integration

Source baseline `a1da79229ed5a6f934b544f36d4d908eecb05dd2` plus this
milestone. Recovery index (frozen sources, patches, native binaries, command/mode
and runtime receipts):
`/Users/jiamo/pcc-recovery-archive/2026-10-17/portable-runtime/index.json`.

- Temporary `str(...).zfill(width)` results enter the existing output-slot
  protocol before retiring input roots. Tuple-typed stores use the native
  assignment protocol, preserving catchable errors and evaluation/finally order.
  Private module-comprehension unpack slots are registered before publication.
- Public `sys.setrecursionlimit` direct/alias calls use the existing native
  setter with rooted `__index__` conversion and exception checks. The stdlib
  facade exposes the same API; the no-libpython guard remains enabled.
- Native source-hosted self-backend/no-libpython programs match CPython for
  padding/callback order, tuple errors/finally, private comprehension names and
  recursion setter errors/restoration. Fifteen executions at GC0-GC4 pass.
  An unchanged TAG/T2-generated UUID Python program also executes natively;
  dynamic linkage is libSystem only. UUID payloads are compared by format,
  version/variant and count, not unpredictable byte equality.
- The earlier 120-second executable timeouts interrupted cold owned-runtime
  construction (181 modules). A task orchestrator reused only receipt-verified
  objects and completed the same production compile/optimization/emission and
  archive checks. A compiler-identity change correctly rebuilt the objects.
  Production runtime-build checkpoint reuse is still separate unfinished work.
- Compiler ownership for these runs is **host pcc0 frontend, owned self backend,
  libpython off**. The historical installed pcc1 baseline was not replaced.
  New-source pcc1 bootstrap/promotion, full translated Harness/zdb, all Node APIs
  and release qualification remain open; no fixed point or performance claim.

## Harness compiler integration (local source delta)

Baseline `57a64e8735a53dae2782199bfc03d56d3ae75123` plus this source milestone. Frozen sources, patch, actual logs, runtime and native receipts:
`/Users/jiamo/pcc-recovery-archive/2026-10-15/harness-project-closure/index.json`.

- Package/multi-entry collection retains eager cross-package ABI providers with
  stable entry search precedence and bounded optional/lexical/f-string discovery.
  Importlib provider siblings use relative imports under native public names.
  The test runtime cache binds the production compiler checksum.
- Fifty focused cases pass, including two actual linked native inputs at
  GC0–4, five existing native f-string cases, host importlib behavior and cache
  identity checks. One broader integration case remains deselected. Compilation
  uses current host pcc0, self backend and libpython off; this is not a new-source
  native pcc1 bootstrap or promotion claim.
- The unchanged full `/Users/jiamo/my/harness/harness/__main__.py` still refuses
  `raw pointer cannot be a slot-call operand` in `_run_gui`, `gui_app.py:483`.
  No full Harness executable was produced. Keep the existing managed/raw-pointer
  boundary; resolve general typed-call/adapter support without hand-patching
  Harness. An optional `pcc inspect` of the formatted native fixture timed out
  at 20 seconds; OS linkage audit of both new fixtures reports libSystem only.
- Harness is the existing manual Python port and a compile/behavior reference.
  Upstream-to-T2 generated Harness, current-source pcc1 qualification, full GUI
  and complete-product acceptance remain open in the translation goal.
  Application/GUI source bytes are unchanged; owned-process audit is empty.

## 2026-10-15 cloud continuation

- `1c1905` preserves dict/list/set copy dispatch with owned method-result roots.
  Three native pytest nodes passed using a source-matched Linux x64 single-thread
  owned runtime: dict copy/pop under requested GC0-GC4 (10 executions), plus
  the 48-candidate GC4 page-selection case. No full Stage1 or performance claim.
- CI now includes both dict pcc0 nodes in the existing 300-second admitted-runtime gate.
- Windows PE double-charge fix `b1db` awaits CI. macOS `ce73` only fixes input
  lifetime; the initial 3.71 GB stack-map rejection remains open.
- Cloud evidence: `/workspace/shared/pcc-resume-native-20261014/native-v2` (outside the repository).

## 2026-10-15 evening

- `c0e17b72`: the native link-lifetime test's stubs take the linker's
  `archive_selections` keyword; the stubs were stale, not the linker
  (`test_native_object_fastpath.py` natively[pcc0]).
- `495837cb`: a dynamically called module export unboxes to its inferred
  scalar type (`test_native_module_alias_call_does_not_bind_unrelated_class_method`).
- `8c7f55e4`: classes nested in a class body are declared at module level
  ahead of their outer class and bound as its attribute, with CPython's
  `__name__`/`__qualname__`; `tests/python/test_nested_class_definition.py`
  passes natively under GC0-GC4.
- `70f0e38c`: keyword and default calls to known callees bind statically
  when exact: keywords in formal order, omitted defaults None/bool/tagged
  small int. Reordered keywords, str/bytes/tuple/heap-int defaults, `**kwargs`
  callees and every TypeError shape keep the binder. Shape tests that reached
  the binder through a keyword call now use a shape that still needs it; the
  slot-call consumers in 98 test files (`take`, `consume`, ...) gained
  `**extras` for that reason; `test_binder_contract_matrix` pins
  `required_mixed` as a direct call and `reordered_evaluation` as the binder.
  Native GC0-GC4: `test_static_keyword_binding.py` (now with a conditional
  keyword operand), the dynamic-append binder program, the `required_mixed`
  binder-matrix cells. Host: 101 of the 102 affected files on a snapshot of
  these changes (failures there are the pre-existing ones below, plus pcc1
  cells without a provisioned pcc1); `test_valueclass_aggregate_ownership_native.py`
  (only `**extras` added) was stopped unfinished.
- `b085f161`: decorators run once at definition (a call loads the published
  binding; each call used to re-apply them) and a call to a module function
  is accepted as a decorator factory. Its last edit (decorator expressions
  evaluated top to bottom before they are applied) has not run natively yet:
  `tests/python/test_decorator_application.py` with a rebuilt runtime.
- Open, found here: a dynamic `.append()` result passed to a direct call keeps
  one extra reference (positional calls too, so pre-existing; the binder path
  is correct). Repro: `DIRECT_PROGRAM` in
  `tests/python/test_slot_call_dynamic_append_result.py`, pcc0, GC0. The
  no-sink case of `_emit_dyn_list_method_with_runtime_guard` returns a raw PHI
  of a borrowed None and an owned method result, and
  `_emit_generic_dyn_method_call_on_value` never releases the bound method
  from `py_obj_getattr`. Fixed in the commit after `88036dc2` for the list
  route (append takes a result root as `list.extend` does, the other names'
  join is recorded as owned, the method is released): the append cases of
  `tests/python/test_dynamic_method_result_ownership.py` matched CPython on
  GC0; GC1-GC4 and the `direct` variant of
  `test_dynamic_append_owned_result_and_error_native_five_gc` have not run.
  Still open: names the dict route claims first (`copy`, `pop`, ...) leak a
  user method's result the same way (`dyn.pop.result` PHI in
  `dict_lowering.py` is not recorded as owned).
- Pre-existing host failures: `test_lambda_constructor_roots.py`,
  `test_owned_os_descriptors.py` and `test_string_method_result_roots.py`
  are fixed in `17f1819a` (stale shapes; macOS ran the Linux-target model
  with host open flags). `test_bytes_decode_result_roots.py` passes again:
  its `BufferModel` lacked the `memcpy` that `py_bytes_new` uses since
  `bb610f00b`.
- Assigning `__defaults__`/`__kwdefaults__` does not affect calls, through the
  binder as well; the static default fill matches that, and both need a guard
  when the assignment becomes live.
- Python feature matrix (48 programs, pcc0 self backend, no libpython, GC0,
  against CPython, at `304f3926`): 19 equal, 20 differ, 9 fail to compile.
  Programs, runner and results: recovery archive `2026-10-13/featmatrix/`.
  Compile failures: async comprehensions, `except*`, `match`,
  `x = yield from`, lambda forms, class keywords/`__init_subclass__`,
  itertools ("slot-call name has no authoritative registered root"),
  decorator factories and nested classes (both addressed above). Differences:
  decorators re-applied per call, `functools.wraps`/`lru_cache`/`cache`
  compiled as no-ops, `__setattr__` overrides ignored, frozen dataclass
  assignment allowed, Enum, metaclass `super().__new__`, dict view set
  operations, frozenset dict keys, nested f-string format specs,
  `int('1_000')`, `math.isclose`, memoryview slicing, namedtuple `_replace`,
  `__kwdefaults__`, `__getitem__` iteration, non-ASCII case mapping, two
  programs whose `main` compiled as a no-libpython stub.

## 2026-10-15

Commits after `bc7f53a5` (this session): `bad776a3` builtin `type` as a value
(the Windows object-identity failure: `main` was a no-libpython stub) plus
the type-object call and copied class names; `27183c2e`/`ea678a14` user
exception subclasses keep BaseException args, str and repr; `ba9f5aa1`
exception tags as their own numeric domain; `329af921` stale GC4 source pin;
`8d9079d9` quiet successful Mach-O links (macOS empty-stderr tests);
`8fd1be9b` local class `__name__`/`__qualname__`; `110870ba` Mach-O link
retires inputs and source payloads once copied; `2d913a05` one PE stack-map
snapshot copy on CPython.

CI 38584467472 (`9cbcb1e9`) gave the refused link sizes: Windows "PE final
stack-map snapshot" raw=10945047087 retained=8343004237 limit=10201337856;
macOS "Mach-O input stack-map snapshot" raw=3711958786 limit=2485518336, where
the macOS tree cap is the runner's 3.36 GB reclaimable memory minus a 512 MB
reserve. `docs/investigations/stage1-link-memory-budget.md` has the analysis;
`110870ba` and `2d913a05` target those two checks.

Known on these commits:
- `test_classmethod_constructor_consumes_temporary_tuples[pcc0-...tuple]`
  fails under GC4 since `27183c2e`: layout-sensitive exposure of the GC4
  unretired-relocation retention (see the 2026-10-15 section of
  `docs/investigations/gc-backend4-forwarding-retirement-starvation.md`).
- `test_native_object_fastpath.py::
  test_private_link_plan_retires_graph_before_image_allocation_natively[pcc0]`
  failed on `bad776a3` and on these commits ("prepare_executable_object() got
  an unexpected keyword argument 'archive_selections'"): stale test stubs,
  fixed in `c0e17b72`.
- Still open from this session's probes:
  `test_dunder_dispatch_subclass_override` dynamic binary dunders,
  `test_class_definition_defaults` (fails on `bad776a3` too). Nested class
  attributes and the alias-call emit worker are fixed (evening section).
  `test_macho_exec_link`'s data-pointer test was stale since `50889ccd` added
  chained binds for imports in data pointers; it now checks that contract.

## 2026-10-14 afternoon

Between `fa19e4b1` and `ca703ae0` a parallel session pushed 24 commits:
Win64 globals beyond rel32 use ADDR64/SECREL (`b1c2c360`, the PE rel32
failure), merged and deduplicated PE stack maps (`fa5628cc`), oversized-PE
rejection (`69566173`), PE host budgets (`d6f50a7d`, `cc0a4bee`, `ca703ae0`),
GC4 forwarding-shell finalization and idle retirement (`b59142c8`), super-call
result roots (`4a9dbc69`), one `object` identity for builtin and class
predicates (`988bc697`, the isinstance(object) gap), CI Stage1 7200 s
(`0198e205`) and new CI regression steps.

CI at `ca703ae0` (run 38567599226; seven superseded runs were cancelled to
free runners, and this one after its Windows and macOS results; the next
run is 38584467472 for `9cbcb1e9`):
- Worker-prior gate passes on all four platforms (strict closure included);
  the macOS pcc0 jobs and Windows RSS publication pass.
- Linux native: Stage1 passes and the GC0-GC3 native canaries pass (the GC3
  lease fix `c5363d80` holds). The GC4 canary compile exceeds its 6000 s
  deadline: aarch64 GC0 326 s, GC1 747 s, GC2 880 s, GC3 845 s, GC4 > 6000 s
  (run 38530860670). See
  `docs/investigations/gc-backend4-relocation-set-free-walk.md`; `6a242b67`
  removes the per-free relocation-set walk (local probe 186 s -> 16.7 s);
  the page selector's whole-heap scan per selected page remains.
- Windows native: Stage1 compiles every module (codegen done at ~3044 s);
  the PE link stops at "compact stack-map byte budget exceeded: PE final
  stack-map snapshot". The object-identity native test from `988bc697`
  fails on Windows: `main` was compiled as a no-libpython stub
  ("no-libpython function unavailable: program.main").
- macOS native pcc1: Stage1 codegen completes (~4800 s); the Mach-O link stops
  at "compact stack-map byte budget exceeded: Mach-O input stack-map
  snapshot" with a 2.79 GB tree cap and the coordinator at 2.04 GB. `9cbcb1e9`
  adds the refused check's label, estimate, sizes and limit to the PE and
  Mach-O link errors, so the next run shows how far each estimate is over.

Also failing on the `ca703ae0` snapshot (pre-existing, seen during A/B):
`test_runtime_abi_chunking.py` (two chunk-size bounds, 70 and 60 > 50),
`test_freestanding_gc_state.py` (four: 201 state globals, and
`pcc_gc_foreign_lease_active` missing from the runtime ABI registry),
`test_gc_abstraction_surface.py::test_colored_relocating_gc_read_barrier_clears_candidate`,
eight `test_gc_backend4_production.py` cases,
`test_relocation_copy_clears_stale_sweep_verdict_backend4[pcc_python]`, and
`test_foreign_address_leases.py::test_native_gc3_gc4_foreign_address_movement_and_overlap`
(self-link rejects link arguments).

## Recent source changes

- `0f6fa1d9` function-local from-import aliases follow control flow.
- `bbe27219` merged Mach-O `__text` splits at input boundaries (32 MiB chunks)
  so AArch64 thunk islands interleave.
- `510ed001` compile-time-only from-imports (typing, abc, ...) no longer get a
  runtime module global; `TypeAliasType(...)` metadata no longer raises NameError.
- `e62cef29` raw i64 module globals updated through `global n; n += ...` store
  the unboxed value instead of rooting a boxed pointer in the i64 cell.
- `97f3d52f` `self.__class__(kw=..., **mapping)` keeps the parser's operand
  order, so the constructor-unpack path compiles it.
- `3a2530be` the freestanding-directive scan returns early without a marker and
  indexes a character list instead of the source str.
- `28ce3e92` restores the 6 s worker-prior fixture deadline (see decision 2).
- `1c980956` the ELF link reuses a relocatable `.pcc_stackmaps` table's
  validation verdict for its relocated copy (finalize 44.8 s -> 0.9 s on CI).
- `fbe49ada` codegen treats every raw-pointer unsafe intrinsic result as raw;
  fixes the Windows `os.path.abspath` 0xC0000005 (verified on CI Windows).
- `f35eb06f` pcc's json appends/slices whole strings (native loads 2.3x,
  dumps 2.2x faster) and joins escaped surrogate pairs.
- `a21fa121` pcc's json decoder reads NaN/Infinity/-Infinity (Stage2 blocker).
- `3aa13b2a` vthread effect analysis walks each callable once per module pass.
- `68493fcf` str iteration with a byte cursor in the sequence iterator, typed
  `for` loops and typed comprehensions (all three were quadratic on non-ASCII
  strings; the comprehension also left its target without a registered root,
  so `[c.upper() for c in s]` did not compile).
- `de777b21` value-position `enumerate(...)` publishes into the caller's output
  root: `list(enumerate(xs))` compiles again (broken since `97b6bced`,
  2026-10-03) and an owned temporary iterable is released (it leaked every item).
- `263d12af` CI limits (next section): Stage1 watchdog 6000 s, push gate is
  Stage1 plus the five-collector native canary, full chain by manual dispatch,
  strict closure 3600 s (not run on macOS), fixture peer wait 15 s.
  `85825b40` counts its 26 new gate tests in HOST_COUNTS (all three
  worker-prior jobs of run 38436916173 failed on the stale count).
- `52ca3f80` native `os.replace`/`os.unlink`/`os.rmdir` raise CPython's errno
  subclass with errno, strerror, filename(s) (was a bare OSError, so
  `except FileNotFoundError` missed a missing file); the worker RSS publication
  retries an errno-only EACCES on Windows (pcc has no `winerror` yet). Cause of
  the Windows native prior-worker failure in run 38437580834.
- `98ee6010` comprehension targets at module scope and in native lambdas are
  registered roots (`[w.upper() for w in words]` at module scope did not
  compile; the targets were not GC roots there).
- `e06cd523` the errno-only EACCES retry of the worker RSS publication applies
  only to pcc's native runtime on Windows; CPython keeps the winerror retry.
- `aea09dca` a float-typed result of an object call (`sqrt(9)` after
  `from math import sqrt`) is unboxed before `int()`, int conversion and unary
  minus (self IR verifier failure; also fixes the alignment `import_math_alias`
  case: fails on the `e06cd523` snapshot, passes with the fix).
- `ddb5fa70` "PE rel32 out of range" names the symbol, sections, RVAs, addend,
  displacement and the section layout.
- `aa643fb7` the process-tree sampler readmits a reused PID through its owned
  kernel parent (macOS Stage1 abort, CI section).
- `7cc98b33` the tar oracle reads the current IR printer; strict closure CI
  budget 6000 s (CI section).
- `984cb2d4` canary compile workers re-raise after their report
  (`PCC_DEBUG_WORKER_RERAISE`), so a failure prints its file and line.

## CI (follow each pushed commit)

CI limits changed in `263d12af` (maintainer request: "至少能成功"). The push
gate on Linux/Windows is now `bootstrap_platform.py --gc all --stage 1`: Stage1
and the native canary under all five collectors. The Stage2/Stage3 fixed point
is unchanged as a definition and runs by manual `workflow_dispatch` with
`full_chain: true`; it cannot finish in a 6 h hosted job today (Stage2 section).
The workflow still has no `continue-on-error`. Stage1 watchdog 6000 s on all
platforms (projections below), macOS job 180 min, its 45-minute target
recorded as `darwin_target` instead of asserted. Worker-prior strict closure
6000 s since `7cc98b33` (was 3600 s; see below), not run on macOS (needs
~8.6 GB, runner admits ~3.1 GB). Fixture peer wait 15 s (decision 2, applied).

- Runs 38436916173 (`263d12af`) and 38437580834 (`85825b40`), Linux aarch64:
  Stage1 2396 s and 2384 s. The GC0 and GC1 native canaries pass; the GC3
  canary compile fails in a pcc1 codegen worker after `direct indexed emit`
  with `RuntimeError: slot-call owned-result lease failed` in both runs (GC2
  and GC4 were cancelled after it). `pcc_gc_foreign_lease_acquire` returns -1
  only when the published object's node is marked freeing, or for a managed
  non-leaf object without a node, which only GC3/GC4 reject. No local pcc1
  is newer than the lease code (`1a59d09d`, 2026-10-02); `984cb2d4` makes the
  next failure print its location.
- Windows (both runs): Stage1 compiles every module (codegen done at ~4600 s in
  run 38437580834) and the COFF link fails with "PE rel32 out of range";
  `ddb5fa70` reports the reference and layout. macOS pcc1 is ~674 MB
  (`__TEXT` 626 MB), so the image size alone does not explain a 2 GB distance.
- macOS native pcc1 job (`263d12af`): Stage1 aborted at 3804 s with
  `active_missing`: macOS gave a new worker PID 7199, which an earlier worker
  had used, and the sampler excluded every reused PID, so the worker never
  entered the tree state. Fixed in `aa643fb7`. macOS worker-prior passes at
  `85825b40` and `e06cd523`; Windows RSS publication passes at `e06cd523`.
- Strict closure: with 3600 s it reached the tar oracle for the first time
  (aarch64 at 2916 s, module 27/34; x86_64 at 3395 s) and failed there: the
  oracle could not read typed calls (`call ptr (ptr) @f`), bitcast root slots,
  retained table loads and literals held in constant item arrays. Fixed in
  `7cc98b33` (all six modules pass on a local closure). Module verify+emit
  runs at ~2.4-2.5 s per IR MB, so all 34 modules project to ~3200 s
  (aarch64) and ~3700 s (x86_64): over 3600 s, hence the 6000 s budget.
- Linux x86_64 wheels of both runs were still in Stage1 after 85 min (slow
  runners).
- GC3 canary root cause, reproduced locally with pcc0: `sys.stdout`,
  `sys.stderr` and the `object` class root are malloc'd immortals without an
  object node. `pcc_gc_foreign_lease_acquire` gave such an owner a mode guard
  on GC0-2 but on GC3/GC4 only for graph-leaf tags, so passing `sys.stderr`
  as an argument (`phase_timing.report(sys.stderr, ...)` in the worker) raised
  "slot-call owned-result lease failed". Fixed in `c5363d80` (a no-node
  immortal outside the nursery/zpage gets the leaf mode guard; model cases for
  GC0-4 in `test_foreign_address_leases.py`, five-GC native test
  `test_immortal_stream_operand_lease.py`, GC3/4 raised before). A/B over 17
  lease/ownership/five-GC files and the CPython alignment suite: no regression
  (`super_call` fails on both sides, pre-existing). CI for `c5363d80` (run
  38453713062) validates the canary.
- Run 38449385286 (`984cb2d4`): the four macOS pcc0/worker-prior jobs and
  Windows RSS publication pass; Linux/Windows Stage1, strict closure and the
  Windows PE diagnostic were still running at 19:17 UTC. Without the lease fix
  its GC3 canary is expected to fail again (now with the file and line).

Stage1 projections (`stage1_projection.py` in the archive: per-module times
scaled against the complete `1c980956` runs): Linux aarch64 2338-2362 s
measured; Linux x86_64 ~2550-2650 s on fast runners (codegen ends ~2310 s, the
ELF link was cut during relocation) and ~4800-4900 s on slow ones (1.82-1.90x
per module); Windows x86_64 ~3850-4500 s (1.45-1.69x plus the COFF link);
macOS arm64 ~4500-5000 s (per task 0.77x of Linux aarch64, but 1.0-1.7
admitted workers on 3 cores; three workers would make it ~2300 s).

- Run 38423410429 (`a21fa121`), Linux aarch64: Stage1 2361 s. GC0 Stage2
  (2 workers per chain): export 459 tasks 1426 task-s, done at 717 s; summary
  now passes (56 tasks, 385 task-s, done at 973 s); preload-delta done at
  1220 s; codegen: the exclusive calibration of `unsafe_lowering` took 653 s
  (Stage1: 80.5 s), then `self_backend_parse` ran 109 functions in 157 s of
  frontend time (Stage1: 11.2 s for the same functions, 14x) until the 2400 s
  stage deadline. GC1/GC2 were cancelled after the GC0 failure.

- Linux aarch64 Stage1 now fits: 2338.1 s at `1c980956` (run 38411293966,
  link 243.7 s), 2362 s at `fbe49ada`. At `3a2530be` it had missed by ~5 s.
- Stage2 (GC0, aarch64, run 38411293966): the export phase completed for all
  459 tasks by 808 s (1608 task-s, ~2 workers); before `3a2530be` it reached 273
  modules in 2400 s. The summary phase then failed reading the AST wire of
  `layer1_support` (`1e309` -> `Infinity`; fixed in `a21fa121`). Slowest exports:
  `type_infer` 197 s and `unsafe_lowering` 98.7 s (quadratic iteration of
  non-ASCII source in the directive scan, pending fix), `c_parsetab` 55 s
  (not yet profiled: a local sample with the `0f6fa1d9` pcc1-profiling binary
  only showed the scan `3a2530be` already removed; a current pcc1 is needed);
  the median module took a few seconds.
- Linux x86_64 Stage1 is runner-dependent: at `1c980956` all 460 modules were
  compiled by 2383 s and only the link missed the cap; at `0f6fa1d9` only 72
  were compiled. On those 72 modules every phase, including untouched CPython
  frontend work, was 1.7-2.0x faster in the later run (runner difference; the
  CPU model is not recorded in the receipts).
- Windows x86_64 Stage1: 83/460 modules in 2400 s. macOS Stage1: ~70/460
  modules (49% of the work) in 2400 s, limited by memory admission (below).
- Run 38423410429 (`a21fa121`): Stage1 completed modules at the 2400 s cap:
  Linux x86_64 73 (slow runner again), Windows 91, macOS 80 (average 1.33
  workers). Linux aarch64 above.
- The former 2400 s Stage1 cap was ours, not GitHub's (6 h per hosted job);
  `263d12af` raised it to 6000 s.
- Worker-prior gates: strict closure times out (aarch64, x86_64) or hits the
  memory cap (macOS); Windows now passes default-xdist (2023), windows-exit and
  the native width cases; the GC4 growth case fails intermittently on Windows
  and Linux x86_64 with "missing worker marker" (decision 2). Same picture at
  `a21fa121`: strict closure 1800 s timeouts at 9.2 GB peak (Linux), macOS
  `MEMORY_LIMIT` (3.3 GB cap) at 707 s, Windows GC4 growth child gave up after
  exactly the 6 s deadline.

## Stage1 within 2400 s (goal 1 / platform qualification)

- aarch64: codegen workers keep all four slots busy until ~2100 s (90 idle
  slot-seconds per run), then the coordinator-only link runs ~244 s: stage inputs
  ~91 s (object parse/validation in the SHA-pinned `elf_x86_64.py`, stack-map
  validation ~1/3), resolve/relocate ~130 s for 39.6 M relocations, classify
  ~16 s. Margin is 40-60 s and depends on the runner.
- x86_64: needs about the link time (~300 s) on a fast runner; ~3x on a slow
  one. Its route assembles text (`object_assemble` 2352 of 9376 task-s); a
  structured emission route like aarch64's would remove most of that.
- macOS (3-core M1, 7 GB): admission budget 2.29 GB against per-task
  reservations of 1.1-3.6 GB (peaks 0.5-1.9 GB), so for ~1100 s one task ran at
  a time. Three-way parallelism would fit 2400 s and needs ~0.76 GB per task.
- Work volume: in `pcc.backend.macho_link` ~37% of IR lines are error/cleanup
  paths; GC bookkeeping calls far outnumber work calls. Static keyword binding
  (decision 1) cuts IR 5.6-21% on real modules.

## Stage2 / pcc1 throughput (goal 1, goal 6)

- Keyword/default calls to known callees went through the runtime signature
  binder (~50x slower than a direct call). Static binding is implemented but
  uncommitted (archive `patches/static_keyword_binding.patch`, test
  `tests/python/test_static_keyword_binding.py`): five-GC output equals CPython,
  default-object identity preserved, IR 5.6-21% smaller on real modules.
- Since `168e4d06` every call through a module-level native from-import is a
  live module-namespace lookup plus a dynamic call. The live semantics are
  required (`tests/python/test_live_import_namespace_rebinding.py`); a fast path
  needs an identity guard or a namespace watcher. Not started.
- Stage2 export (run 38391246598, GC0): 148 ASCII modules 516 task-s (median
  2.4 s), 124 non-ASCII modules 3055 task-s (max 291 s, the quadratic scan fixed
  in `3a2530be`); Stage1 exported all 460 modules in 66 task-s. A replayed
  export of one ASCII module under pcc1 (archive `exportreplay/`): vthread
  effect analysis 31%, AST JSON wire 26% (json.dumps 22%), parse 19%, lift 6%;
  self time is 46% GC roots/barriers, 18% allocator, 13% refcount, 6% TLS.
  `f35eb06f` makes pcc's json append/slice whole strings (native loads 2.3x,
  dumps 2.2x faster on a 6.4 MB AST wire); the vthread analysis now walks each
  callable once per module pass (pending commit; export tables of 459 modules
  byte-identical, the 460th being the edited module itself).
- In that replay `_py_func_call_kwargs` (the runtime keyword binder) is on
  nearly every call stack; its own self time is 0.2%, its cost shows up as the
  allocation and GC work above. This is the path static keyword binding removes.
- Phase ratio Stage2 (pcc1) / Stage1 (CPython), Linux aarch64 GC0: export
  1426-1598 vs 65 task-s (~22-24x; ~19x without the three quadratic outliers),
  summary 385 vs 37 task-s (~10x, run 38423410429), codegen 8x
  (`unsafe_lowering`, calibrated alone) to 14x (`self_backend_parse`
  functions). Stage1 codegen is 8266 task-s, so Stage2 codegen needs ~66k-116k
  task-s: 4.6-8 h for one collector chain even with all four workers, against
  a 6 h job that also builds Stage1. A five-collector Stage2/Stage3 in CI needs
  pcc1 several times faster on the compiler workload. The measured owner is
  runtime overhead (GC roots/barriers/pins/leases, allocation, refcounting:
  ~85% of self time in the export replay).

## Open decisions for the maintainer

1. Resolved 2026-10-15: shape tests that reached the binder through keyword
   calls now use a shape that still needs it (see the 2026-10-15 evening
   section); the behavior is covered by native five-GC execution.
2. Resolved: the worker-prior fixture peer wait is 15 s in `263d12af` (asked
   for together with the other CI limits). Evidence: with 15 s the x86_64
   native-pcc0 gate passed in runs 38400558239 and 38405592470; with 6 s it
   failed in 3 of 5 runs, in 38411293966 and on Windows in 38423410429.
3. Source-pinned files: `tests/test_elf_*_source_controls.py` SHA-pin
   `pcc/backend/elf_x86_64.py`; any link-loop change needs the preimages updated.
4. A Windows unhandled-exception reporter (code, image offset, stack scan)
   would change `pcc_windows_start`, whose model test
   (`test_owned_windows_exit.py`) needs a new double. No longer needed for the
   `abspath` crash: cross-compiling for Windows and running under Wine with
   `WINEDEBUG=+relay,+seh` located it (archive README).

## Known failures

- Default suite at `f65ce326d` (2026-10-19, isolated snapshot rerun of the
  424 files that failed on 2026-10-18): 141 pass, 277 fail, 1 timeout;
  first failures and logs in
  `/Users/jiamo/pcc-recovery-archive/2026-10-19-default-suite-reds/`. The
  failures are a long tail (stale source pins, changed values, missing
  fixtures), not one shared cause. Earlier runs from a snapshot without
  `PYTHONPATH=<snapshot>` are invalid: test subprocesses imported the live
  worktree.
- Runtime archive and the table policy (found 2026-10-19): automatic local
  archive selection judges staleness by the source codegen identity only, so a
  runtime archive built under `PCC_PRECISE_STACKMAP_TABLES=1` (runtime frames
  with records) is reused by default builds and vice versa. The stack-walk
  differential needs a table-enabled runtime: without one the collector's own
  frames have no records and their registered roots show as `unmapped`. Tests
  use `pcc_stackmap_runtime_archive` (its own cache key). Folding the policy
  into `codegen_checksum` made explicit default archives fail admission for
  table-enabled links; the fix is exact identity for automatic selection and
  same-source admission for explicit archives (not done).
- User-mode raw addresses (found 2026-10-19): outside runtime-port modules a
  pointer intrinsic's result is typed `int`, so an IR pointer with an `int`
  type can be either a boxed int or the raw address. `ptr_eq(load_ptr(here,
  0), above)` with `above` an untyped parameter holding the same address
  returns 0 (two locals, or two parameters, compare correctly); comparing
  `ptr_to_int` values works. Runtime ports (pointer lane) are unaffected. Repro:
  `/private/tmp/claude-501/-Users-jiamo-my-pcc/3a7f74e3-a0d6-4ce6-8c6c-209e5c96345a/scratchpad/frame1/eq.py`
  prints `loaded 0` (copy it before that scratch directory goes away).
- GC4 relocation-set selection (found 2026-10-19):
  `tests/python/test_freestanding_gc_mapped_roots.py::test_backend4_precise_stackmap_consumer_rewrites_exact_frame_location`
  exits 4 because `pcc_gc_select_relocation_set(1)` no longer selects the one
  freshly allocated, rooted object (the harness then never reaches the stack
  map consumer). The selector changed in `627285079`, `d27cfb236` and
  `9f16a58a9` (2026-10-15/16); the file's earlier
  `test_mapped_roots_have_one_strict_visitor_and_split_providers` failure (a
  stale raw-only classification of `py_subs_exc_cache_slot`, fixed with the
  stack-map registration commit) had hidden it under `-x`.
- Class-attribute identity of properties (found 2026-10-18 after `d26f61b06`
  let `test_property_aliases_preserve_lexical_replacements` run past the
  verifier): for `class A: value = property(...); before = value`, pcc gives
  `A.before is A.value` False, `A.__dict__['value'] is A.value` False and
  `type(A.value).__name__ == 'object'` (CPython: True, True, `property`). The
  static class-attribute read returns an object other than the class dict
  entry. Instance use of properties is unaffected. Repro:
  `/private/tmp/claude-501/-Users-jiamo-my-pcc/3a7f74e3-a0d6-4ce6-8c6c-209e5c96345a/scratchpad/desc/run5/alias2.py`
  (copy it before that scratch directory goes away). The same holds for
  ordinary methods: `Holder.method.__doc__` and `Holder().method.__doc__` are
  None while `Holder.__dict__["method"].__doc__` is the docstring.
- Red at `98422a5b6` (each rerun in an unmodified `git archive` snapshot of
  that commit with its own runtime archives, 2026-10-19):
  `test_gc_backend4_production.py` 6 (deallocating quarantine, class lookup of
  a forwarded method slot exits 13, three genzgc selector/fragmentation
  policies, class creation base/MRO spans); `test_gc_update_referents.py` 2;
  `test_cpy_call_argument_ownership.py` 20;
  `test_owned_descriptor_module_bindings.py` 6 (WorkerMemoryError: 512 MiB
  budget refuses the task); `test_py_class_export_schema.py` 2 (method ref
  registration; `pcc.package.acquire`: "native super().__init__ has no
  implemented initializer for this foreign base");
  `test_callback_sync_context.py::...native_callback_context[virtual_park-*]`
  (`vt.result(task) == 42 and seen == [1, 2, 3]` fails). The GC4 ones and the
  mapped-roots consumer exit 4 above share the relocation selector.
- `tests/python/test_fallback_baseline.py`: with `-x` the file stops at
  `test_layer1_constants_cross_module_static_imports_stay_native` (line 759,
  live-import binding), so the ratchets after it had not run for a long time.
  Run on their own (worktree at `4b6162010` plus uncommitted property edits,
  which change compile inputs): total fallbacks pass; per-module actions exceed
  the ceilings for nine modules (`precise_stackmap` 25/15 predates
  `5ccad8082`); contextual per-module compile fails for 26 codegen modules with
  `slot-call CPython name requires an output-slot bridge: <local name>` or
  `slot-call Call lacks an immediate owned-result handoff`. Re-run on a frozen
  snapshot before classifying.
- Windows native-pcc0 width case: `0xC0000005` inside `os.path.abspath`
  (phase witness `abspath` in runs 38273917206, 38375711981, 38405592470).
  Root cause found and fixed in `fbe49ada`: `_path_abspath_build`'s
  `windows_full_path(...) if ... else null()` was lowered as an owned managed
  value (retained, rooted) because codegen's `_UNSAFE_RAW_POINTER_RETURNS`
  lacked 14 raw-pointer intrinsics; after `free(buffer)` the exit path released
  the freed buffer. Reproduced and verified locally by cross-compiling for
  `x86_64-pc-windows-msvc` and running under Wine 8.0 in an amd64 container
  (archive README, "Windows os.path.abspath").
- GC4 (`docs/investigations/gc-backend4-forwarding-retirement-starvation.md`):
  explicit `gc.collect()` never retires forwarding entries, so relocated garbage
  stays alive and its `__del__` never runs (fix parked: retirement itself
  over-releases a live replacement, `test_gc_relocation_sweep_verdict.py` code 23
  pre-existing); `tuple(list of finalizable instances)` + collect aborts with
  `BAD_INCREF`; `test_gc_backend4_production.py` fails a source-text assertion.
  The programs of `test_list_iterator_ownership.py` and
  `test_comprehension_iterator_ownership.py` abort with `BAD_INCREF` under GC4
  for every shape (`list(xs)`, `list(reversed(xs))`, `list(enumerate(xs))`,
  enumerate value); GC0-3 pass. The enumerate shapes first report 2472/2483
  `__del__` calls for 2000 objects, i.e. objects are finalized more than once
  (probe: archive `scripts/gc4_list_shapes_probe.py`). `ys = enumerate(build(n))`
  completed under GC4 before the enumerate fix only because it leaked every
  item; now that the items are freed it aborts like the other shapes.
- `isinstance(x, object)` and `issubclass(A, object)` return False (CPython:
  True): the `object` value is `py_builtin_type_for_tag(-1)` ->
  `pcc_type_cls_object` (a `py_class_new("object")` class), while user class
  MROs end with the distinct `py_object_root_cache` root; non-instances never
  match. Repro: archive `gc3lease/object_identity.py`.
- `test_foreign_address_leases.py::test_native_gc3_gc4_foreign_address_movement_and_overlap`
  fails before running: "pcc self-link mode does not support link arguments"
  (same on the `e06cd523` snapshot).
- `str.upper()`/`str.lower()` map only ASCII letters (`"é".upper()` stays
  `"é"`); non-ASCII case mapping needs Unicode tables in the runtime.
- `[... for i, c in enumerate(s)]` over a non-ASCII str still indexes by
  codepoint per item (`py_obj_getitem_i64`), so it stays quadratic; `for` loops
  over `enumerate(s)` are normalised to a plain `for` and are linear.
- `test_py_for_target_representation_join.py`: 4 tests fail at `a21fa121`
  (a `SimpleNamespace` codegen double lacks `_module_globals`; IR-shape
  assertions on dict iteration).
- `test_worker_resource_plan.py::test_macos_failure_workflow_preserves_narrow_guard_evidence`
  fails since `2a3ad7ff` (2026-10-11): it pins the macOS evidence upload paths,
  and that commit added the worker-prior-gate paths.
- `test_python_cpython_alignment.py` at `a21fa121`: `super_call` ("slot-call
  operand has no authoritative source or owned result: Call"). The
  `import_math_alias` case (boxed `sqrt(9)` result passed where a double was
  expected) is fixed in `aea09dca`.
- Also failing on the `e06cd523` snapshot (A/B of `aea09dca`, identical with
  and without it): `test_boolean_float_conversion.py::test_integer_power_bool_inputs_never_use_bigint_payload`
  (4 cases, `NameError: _int_shift_sign` at `py_int_ops.py:312`; the helper
  came with `6f87072c`), `test_int_builtin_object_projection.py` (3 tests:
  program traceback and `py_int_from_cstr_or_raise` IR-shape expectations),
  `test_int_of_float_exact.py::test_raw_lane_int_of_float_raises_instead_of_garbage[pcc0]`
  (error text "... to C int64" expected), and
  `test_worker_memory_budget_selection.py::test_mac_ci_final_guard_reselects_after_startup_and_records_final_observation[3113851289-0]`
  (the test replaces the shared `subprocess.run` for its guard double, and the
  guard's termination path runs `ps` through it: `list.index` ValueError).
- `test_runtime_iterator_roots.py`: every model case fails with
  `NameError: _TUPLE_ITER_START` since `75b2ddd1`; its loader
  (`test_runtime_getter_roots._GetterMemory.load`) execs only the function
  definitions of `py_iter.py`, not the module constant that commit added.
- `tests/python/test_raw_addresses_are_ints.py::test_raw_addresses_execute_as_ints_on_the_self_backend`:
  `py_bytes_new(_str_utf8("hey"), 3)` yields `b'mpl'` (stale pointer from the
  str -> raw-pointer extern path); independent of the 2026-10-13 commits (A/B
  each). Repro: archive `repros/str_utf8_extern_dangling_probe.py`.
- Host tests failing on a clean HEAD snapshot (pre-existing), among others:
  `test_py_multi_file_compile.py` (borrowed-rebind IR shape, raw-int scalar slot
  count, mock of the removed `_link_with_clang`, static-export constructor arg
  vs live imports, CPython fallback IR for relative imports; the other 41 pass),
  `test_intent_constraints.py` (coverage dropped: TestCrossBackendDeterminism),
  `test_fallback_baseline.py` (340 cli_bootstrap fallbacks),
  `test_hoist_global_cell_boundaries.py`, `test_unsafe_atomic_global_store.py`,
  `test_native_module_global_raw_pointer.py`.
- Also red on clean snapshots of `ed25f93d8`/`0272b8f4f` (2026-10-19, each
  rerun there with the same assertion; not caused by the literal/default
  work): `test_runtime_getter_roots.py` (`_INSTANCE_LOOKUP_VARS` undefined in
  the test's interpreter), `test_raw_scaffold_exact_container_subscript_ownership.py`,
  `test_ir_scaffold_field_contract.py` (37 `self.builder` writes vs pinned 35;
  the two extra come from `02b9bc2d3`/`9007dbd1d`), `test_generator_result_lexical_root.py`
  (2 >= 4), `test_cext_strict_decref_lifecycle.py` (source-text assertion;
  `py_list_new` header conflict), `test_cli_bootstrap_pytest_extract.py`,
  `test_exception_chaining_wiring.py` (2), `test_exception_constructor_owned_result.py`
  (all 7), `test_exception_raise_lifetime.py` (100 == 50),
  `test_extern_c_string_arguments.py` (7 of 31), `test_freestanding_strtoint.py`,
  `test_freestanding_mem_str.py`, `test_gc_threading_substrate.py`,
  `test_gc_abstraction_surface.py::test_colored_relocating_gc_read_barrier_clears_candidate`,
  `test_dict_literal_temp_release.py[4]` (GC4 197 of 200), `test_literal_unpack_slot_ownership.py`
  (`MovingModel` stub lacks `_is_valueclass_payload_type`), `test_native_exception_context_chaining.py`,
  `test_native_str_maketrans.py`, `test_native_str_join_gc_rooting.py`,
  `test_native_tuple_dict_literal_cpy_bridge.py`, `test_py_dict_literal_cpython_key_pairs.py`
  and `test_py_literal_splat_source_position.py` (libpython runtime missing),
  `test_str_builtin_consumes_owned_argument.py`, `test_str_encode_gc_roots.py`,
  `test_floor_division_runtime_contract.py` (`stack_alloc` undefined),
  `test_py_frontend_worker_export.py::test_export_worker_full_lifts_native_stdlib_asyncio`,
  `test_self_backend_call_result_registers.py::test_result_consumed_before_any_call_moves_into_a_pool_register`,
  `test_selfhost_no_over_lane_int_literals.py`
  (19 over-lane literals in the compiler; pcc0-compiled code prints all of
  them correctly, so whether they still misbehave under pcc1 is open: compile
  and run such literals with a Stage1 pcc1).
- Virtual threads: `test_virtual_thread_park_effect.py::
  test_concrete_may_park_method_chain_resumes_to_python_result` prints `None`
  instead of 42 (fails at `1c980956` already; the may_park analysis marks
  handler, Worker.leaf and Worker.middle correctly, so the loss is in lowering
  or the runtime), and `test_cross_module_may_park_uses_generator_abi_and_resumes`
  fails on the same snapshot. Not bisected.
- Stale or environment-gated tests seen while checking `fbe49ada` (all fail
  identically without it): `test_freestanding_variadic_export.py` (5 of 6,
  "pcc.i64 does not implicitly convert ordinary Python int values"),
  `test_freestanding_stdio.py` (13 of 15, need `PCC_RUNTIME_ARCHIVE`),
  `test_freestanding_platform_process.py::test_runtime_archive_process_symbols_are_owned_by_python_port`
  (py_gc_backend.py calls `pcc_platform_abort`), `test_native_json_provider_matches_cpython.py`
  (reads the removed `pcc/ir/support/json.py`),
  `test_symbolic_stackmap_location_budget.py::test_shared_budget_object_links_and_executes`
  on macOS (executes a Linux ELF).

## Repro commands

```bash
# Per-phase Stage1 totals from a CI artifact
python3 /Users/jiamo/pcc-recovery-archive/2026-10-13/scripts/phase_breakdown.py \
  <artifact>/bootstrap-<platform>/platform-qualification/shared/stage1.log
# Tar oracle on a local strict closure (parallel frontend, ~380 s), then on
# the saved per-module sections in ~12 s
uv run python /Users/jiamo/pcc-recovery-archive/2026-10-13/scripts/oracle_closure.py OUT_DIR
uv run python /Users/jiamo/pcc-recovery-archive/2026-10-13/scripts/oracle_sections.py OUT_DIR
# Static keyword binding (host IR shape + five-GC native)
env -u LC_ALL PCC_NO_AUTO_PCC1=1 uv run pytest -x -n0 tests/python/test_static_keyword_binding.py
env -u LC_ALL PCC_NO_AUTO_PCC1=1 uv run pytest -x -n0 -m integration tests/python/test_static_keyword_binding.py
```

Broad pytest sweeps must set `PCC_NO_AUTO_PCC1=1`: `pcc_gate` fixtures otherwise
start an unguarded Stage1 build to provision pcc1.

## Ten-goal acceptance remains open

1. Bootstrap: produce pcc1, run the original C/Python controls, then establish a no-libpython Stage2/Stage3 byte fixed point.
2. Five collectors: finish correctness, concurrency, relocation, GC4 capacity, FRESH_ALLOC, long-term memory, throughput and stability.
3. Platforms: execute macOS ARM64, Linux x86_64, Windows and Linux aarch64 qualification, including the standard macOS 15 3-core M1 / 7 GB / 45-minute job and wheel parity.
4. Ownership: complete compiler/runtime/ABI/provenance, stubs, bindgen, uv-lock and CLI/API consistency without product cc/LLVM/libpython fallbacks.
5. EDG-derived design: complete frontend semantic representation, a single layout authority and per-function compilation-memory management; qualify layered validation and centralized ABI across platforms. Diagnostic RSS guards are not invented acceptance thresholds. External i128 register-edge ABI, x86 dynamic alloca and two-dimensional VLA remain gaps.
6. Build performance: prove Stage2 no slower than Stage1 and a shared five-GC build under six hours. Diagnostic timings do not establish these targets.
7. Tests and cleanup: execute the full original C/Python and integration inventories; finish TLS, archive, freestanding and asynchronous timeout work.
8. Gateway: qualify actual native network/TLS and structured concurrency, then establish performance beyond asyncio.
9. Optimization: qualify tail-call, MADD, peephole, scaffold identity and thread-logging behavior and performance.
10. GUI: complete native app, five-GC, HTTP/TLS, Loader/HMR, typed FFI, interaction and pixel equivalence against Harness 47f943859bef60e4160492346772ded9b24f765a. The existing 160 host passes and 7,412 verified reference blobs do not establish native or pixel completion.
