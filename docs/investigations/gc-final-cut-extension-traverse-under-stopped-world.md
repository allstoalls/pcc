# Investigation: the tracing final cut runs extension traverse under a stopped world

## Status
active

## Problem Description
`pcc_gc_complete_claimed_tracing_cycle` releases the object-graph lock before
calling a C extension's `tp_traverse`, but it still holds a **stopped world**.
Every other thread is parked, so any traverse callback that waits on another
thread cannot complete, and the collector deadlocks.

The relevant window in `pcc/py_runtime/src/py_gc_backend.c`, starting at
`static int pcc_gc_complete_claimed_tracing_cycle` (line 13812):

```c
    if (owns_stopped_world == 0) {
        if (pcc_stop_the_world() != 0) { /* release token */ return 0; }
        acquired_stopped_world = 1;
    }
    ...
    pcc_gc_graph_lock();
    /* claim: pcc_gc_trace_extension_roots_pending = 3 */
    pcc_gc_graph_unlock();          /* graph lock released -- correct */

    if (visit_extension_roots) {
        pcc_capi_visit_extension_module_state_roots(   /* USER CODE, world stopped */
            pcc_gc_trace_final_extension_state_root, &extension_ctx);
    }
```

`pcc_stop_the_world` is cooperative and unbounded
(`pcc/py_runtime/src/pcc_threads.c:418`): it waits until
`pcc_parked_thread_count >= pcc_live_thread_count - 1` with no timeout. A
mutator parks by reaching a safepoint, and `pcc_current_thread_id` is one, so a
well-behaved mutator that the traverse then joins is guaranteed to be parked
for the whole callback.

This is not the same defect as the sibling one in the seed step. There the
probe's own thread never polled a safepoint and so could never park, which is
the mutator's side of the contract; the fix was in the probe. Here the mutator
parks correctly and the collector is the one holding a resource across
arbitrary user code, so no probe change can help.

## Repro
`tests/python/test_gc_backend_generational.py::test_final_trace_extension_traverse_runs_after_graph_unlock`
and its `test_pcc_python_final_trace_...` sibling, under any GC backend
selection (the probe calls `pcc_gc_set_backend` itself):

```bash
gtimeout 900s env -u LC_ALL PCC_GC_BACKEND=0 uv run pytest -q -x -n0 \
  "tests/python/test_gc_backend_generational.py::test_final_trace_extension_traverse_runs_after_graph_unlock"
# Failed: final extension probe timeout; stderr=b'SWIFJG'
```

The probe writes one stderr byte per phase. `SWIFJG` ends at `J`
(`probe_traverse` entered `pthread_join`) and `G` (the contender entered
`pcc_gc_object_is_known`), then stops.

## Test [CONFIRMED]
Observed 2026-09-08. Sampling the hung probe directly gives both halves of the
circular wait:

```
main thread
  pcc_gc_step -> pcc_gc_step_trace_cycle -> pcc_gc_step_trace_cycle
    -> pcc_gc_complete_claimed_tracing_cycle
      -> pcc_capi_visit_extension_module_state_roots
        -> probe_traverse
          -> _pthread_join                      <- waits for the contender

contender thread
  raw_lock_contender
    -> pcc_gc_object_is_known
      -> pcc_current_thread_id
        -> _pthread_cond_wait                   <- parked for the stopped world
```

Attribution: pre-existing, and independent of the owned-mem2reg work in
[runtime-module-optimizer-throughput](runtime-module-optimizer-throughput.md).
The probe is a C program compiled by `cc` and linked against the runtime
archive; disabling the IR passes entirely
(`PCC_PYTHON_IR_PASSES=off`) reproduces the hang unchanged. `git log -S
'owns_stopped_world' -- pcc/py_runtime/src/py_gc_backend.c` names exactly one
commit, `f597f612`, which is also the newest commit on the test file, so the
stopped world entered this path there.

The same commit's seed step has the same shape
(`pcc_gc_complete_mark_cycle_seed`, line 13684: `pcc_stop_the_world()` then
`pcc_gc_seed_roots()`), so it is the second instance of one pattern, not two
unrelated sites.

## Proposals
- No.1 resume the world across the extension callback, then re-stop and
  revalidate [pending]
- No.2 claim the finish token before stopping the world at all [pending]

## No.1 resume the world across the extension callback, then re-stop and revalidate
### Code Change
Not written. Shape:

```c
    if (visit_extension_roots) {
        if (acquired_stopped_world) (void)pcc_resume_world();
        pcc_capi_visit_extension_module_state_roots(...);
        if (acquired_stopped_world && pcc_stop_the_world() != 0) {
            /* clear this exact token and return 0, as the entry path does */
        }
    }
```

The function already re-validates everything after the callback: the
`ready_to_drain` predicate re-checks `pending == 3`, the claim epoch and
backend, the cycle epoch, the selected backend and `mark_active` under the
graph lock. A reset or successor claimant during the open window therefore
lands on the existing failure path rather than a torn final cut. When the
caller already owned the stopped world (`owns_stopped_world != 0`) this cannot
help and the limitation has to be stated rather than papered over.

### Why it is not applied yet
The callback passed in is `pcc_gc_trace_final_extension_state_root`, which
publishes gray objects. Running it with mutators live is exactly what a
concurrent tracer does and the incremental tricolor backend has the write
barriers for it, but this path is reachable for every backend selection and the
barrier obligations of the other four were not read. Landing this without that
reading would be a speculative change in shared GC code, which this repository
has paid for before. Required before acceptance: the barrier contract for each
backend that can reach the final cut, then
`PCC_GC_BACKEND=0..4` runs of `tests/python/test_gc_*.py`.

## No.2 claim the finish token before stopping the world at all
### Code Change
Not written. Take the graph lock, claim `pending = 3`, release the lock, run
the extension traverse with the world running, and only then stop the world for
the drain and the final cut. This removes the resume/re-stop pair and leaves one
stop-the-world window that contains no user code. It reorders more of the
function than No.1, so No.1 is the first candidate to measure.

## Update: the stopped world across the callback is deliberate, so this is a design conflict (2026-09-08)

The same function states the intent in a comment, inside the `ready_to_drain`
branch that follows the callback:

```c
        /* Roots can change while #1/#2 tracing runs incrementally or
         * concurrently. Rescan under the stopped-world cut, then release only
         * the graph lock for callback-capable whole-gray slices. */
        pcc_gc_gray_current_roots();
```

"release only the graph lock for callback-capable slices" is exactly the
behaviour that deadlocks, and it is written down as the design. So this is not
an oversight to patch: the test asserts that a mutator can make progress during
the extension traverse, the implementation asserts that only the graph lock is
released, and the two cannot both hold. One of them has to change, and the
implementation is the newest commit's deliberate choice.

Proposal No.1 is therefore **not applied**. Applying it would silently overturn
a commented design decision in `f597f612` from concurrent work, which is the
case this repository's guardrails say to surface instead of taking.

Backend reachability, which any resolution has to respect
(`pcc_gc_step`, line 14415 onward):

```
INCREMENTAL_TRICOLOR (1)   pcc_gc_step_trace_cycle, no caller stop-the-world
CONCURRENT_MARK_SWEEP (2)  pcc_gc_step_trace_cycle, no caller stop-the-world
GENERATIONAL (3)           only under pcc_gc_explicit_collect_active
COLORED_RELOCATING (4)     one path wraps the whole trace cycle in
                           pcc_stop_the_world by design: "Colored relocation
                           changes the interpretation of read-barrier state.
                           Keep the phase transition STW" (line 14502).
                           Its explicit-collect path does not.
```

Backends 1 and 2 mark with mutators live by construction, so resuming the world
across the callback is consistent with their barriers. Backend 4's commented
requirement means a resume must never apply when the caller already owns the
world, and its explicit-collect path needs its own answer. A fix that ignores
this split would trade one backend's correctness for another's liveness.

The decision needed from a maintainer is which contract wins:

1. Extension `tp_traverse` may run with mutators live. Then proposal No.1 or
   No.2 lands for backends 1 and 2, backend 4's explicit-collect path needs a
   separate answer, and the "release only the graph lock" comment is wrong.
2. Extension `tp_traverse` runs under a stopped world. Then a traverse callback
   may not wait on another thread, that restriction belongs in the public
   extension contract next to `pcc_capi_visit_extension_module_state_roots`,
   and `test_final_trace_extension_traverse_runs_after_graph_unlock` plus its
   `test_pcc_python_final_trace_...` sibling encode a guarantee the runtime does
   not offer and have to be rewritten.

Note that the sibling seed-step defect was a genuine probe bug and is fixed:
the extension-traverse probe's contender now polls `pcc_thread_safepoint()` in
its busy-wait loop, so `test_initial_trace_extension_traverse_runs_after_graph_unlock`
and its pcc-Python sibling pass in 1.37 s instead of timing out at 20 s. That
fix aligned a hand-written C mutator with the cooperative-safepoint contract in
`py_runtime.h`; it overturned nothing. This remaining failure is the opposite
situation and needs the decision above.
