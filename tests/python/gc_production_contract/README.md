# 5-GC common production contract suite

This directory is the **common GC production contract**: Python runtime
semantics that must hold IDENTICALLY under all five production GC backends
(see the 5-GC Production Equality Rule in `docs/goal/goal-prompt.md`, G-track).

Run it under every backend with:

```bash
scripts/run_test_gates.py --gate gc-production-contract
# or a subset:
scripts/run_test_gates.py --gate gc-production-contract --backends "0 3 4"
```

A runtime/GC-touching feature is `DONE_STRONG` / "5-GC production contract pass"
only when every backend (`PCC_GC_BACKEND=0..4`) is green here:

- `#0` refcount + cycle collector
- `#1` incremental tricolor mark-sweep
- `#2` concurrent mark-sweep
- `#3` generational young/old
- `#4` relocating / ZGC-style

**Equal semantics (asserted here, may NOT differ):** object reachability; root
safety; exception/frame survival; container graph safety; weakref / finalizer /
resurrection safety policy; extension object lifetime; ValueBox / value-payload
pointer safety; pcc-native extension module-state roots; virtual-thread
suspended-frame & scheduler-root safety; no-libpython pcc1 behavior.

**Not asserted here (perf — backend-specific profile, MAY differ):** pause
times, throughput, RSS, fragmentation, collection schedule. Backend-specific
algorithm/stress suites (`test_gc_backend*_production.py`, incremental-invariant,
concurrent-worker, generational-remembered-set, relocation/read-barrier, etc.)
are ADDITIONAL and live elsewhere — they are never a substitute for this common
contract.

## Intended contract tests (the program; build incrementally, each green on 0..4)

Status 2026-05-31: this README + the runner are the scaffold; the contract
tests below are the tracked G-track production-bar program, to be ported/added
incrementally (each must pass under all five backends before it counts). Some
equivalents already exist scattered (e.g. coroutine scheduler roots); the
program is to consolidate them here under the uniform 0..4 gate.

- `test_object_lifetime.py`
- `test_cycles.py`
- `test_finalizers.py`
- `test_finalizer_resurrection.py`
- `test_weakref.py`
- `test_container_graphs.py`            # dict/list/tuple/set mutation roots
- `test_exception_traceback_roots.py`
- `test_coroutine_frame_roots.py`       # suspended-frame local survives GC on 0..4
- `test_virtual_thread_scheduler_roots.py` # runtime scheduler queues retain continuation roots
- `test_valuebox_roots.py`                # boxed/nested valueclass pointer payload roots
- `test_valueclass_pointer_payload.py`  # incl. payload-updates-after-relocation on #4
- `test_extension_module_state_roots.py` # PyModule_GetState/m_traverse roots
- `test_native_handle_lifetime.py`       # native FILE* wrapper close/flush lifetime
- `test_gc_collect_reentrancy.py`
- `test_threaded_roots.py`

## New runtime object -> GC Object Contract Checklist (before merge)

Every new runtime object (PyValueBox, CoroutineFrame, VirtualThread, Task,
MemoryView, ExtensionModuleState, NativeHandle, TypedArray, ValueArray, ...)
declares its reference slots ONCE (strong / weak / borrowed / pinned /
movable-updateable / native-handle / value-payload-pointer / frame-local /
scheduler-root + non-traced scalar payloads + finalizer/resurrection behavior),
consumed by all five backends via one `py_obj_visit_slots(obj, visitor)`
contract — never per-type hand-coded graph walkers. The checklist also requires
the five backend gates + C / pcc-Python mirror parity (2 impls x 5 backends) +
the pcc1 no-host test. See the full checklist in `docs/goal/goal-prompt.md`.
