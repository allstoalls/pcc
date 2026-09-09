# Investigation: virtual-thread handler throughput trails asyncio by 9.7x

## Status
active

## Problem Description
The user rejects the gateway's zero-wait concurrency-100 baseline and requires
optimization now. Track work in https://github.com/allstoalls/pcc/issues/188.
The objective is the complete request path with unchanged results, cancellation
and ownership, not a scheduler microbenchmark with application work removed.

## Repro
In the sibling pcc-gateway checkout, run `uv run python benchmarks/compare.py`
with a qualified pcc1. The 2026-09-06 M2 Max baseline is 8,671.8 / 8,844.1 /
85,873.9 requests/s for host pcc / pcc1 / CPython 3.15.0rc1 asyncio, no wait,
100 concurrency. At 100 ms wait it is 909.2 / 908.5 / 973.9 requests/s.
Raw results and 241,650 checked requests are retained in that repository.

## Test [CONFIRMED]
The full three-arm baseline completed 90 runs. Both compiler entries passed the
local HTTP/dashboard executable canaries. The native failure-cleanup canary
also passed under candidate c5ae2affdb02.

## Profiling
Use the existing tools and the shared performance lock. The signed
`.venv/bin/python-tachyon` is CPython 3.15.0rc1 with the debugger entitlement.
Tachyon `--mode=cpu --opcodes --native --flamegraph` profiled asyncio; aggregate
with `scripts/pcc_tachyon_aggregate.py`. For the native image use
`scripts/pcc_flamegraph.py cpu <pid> 3 --exact-pid`, which validates image
identity, resolves its own text symbols and excludes blocked leaves.

The valid native profile contains 981 on-CPU samples: 548 (55.9%) in
GC/pointer/refcount leaves, 195 (19.9%) in clock/syscall leaves. Generator-next
is on 628 call paths. IO poll is on 63 paths and waitset monotonic time on 106;
these overlapping counts must not be added. Eliminating every GC leaf would
have an upper bound of about 2.27x, so closing the full 9.7x gap requires
reducing work at its callers as well. The 50,000-request profiled run completed
at 8,722 QPS. The asyncio profile has 1,704 self samples and opcode data.

An earlier ad-hoc sample taken during Stage2 included many kevent waiting
stacks and its 150,000-request target did not finish within the watchdog.
It is rejected as comparative performance/CPU evidence. Do not attribute its
waiting samples as CPU time or reuse its timeout as a proven scheduler defect.

## Proposals
- No.1 bypass zero-timeout IO polling when no fd waiters exist [CONFIRMED]
- No.2 elide repeated retaining stores of the same reference under GC0 [CONFIRMED]
- No.3 initialize fixed-size generator frames in one operation [DENIED as a speed claim]
- No.4 skip placeholder frame reads on first generator entry [CONFIRMED for host application; pcc1 pending]

## No.1 bypass zero-timeout IO polling when no fd waiters exist
### Code Change
Both runtime mirrors return zero under the scheduler lock when timeout is
zero and no fd waiter exists. Positive/infinite waits and nonempty waiters
retain the existing path.

### Gates
`tests/python/test_vthread_empty_io_poll.py` interposes kevent and checks that
100 empty nonblocking polls make no kernel calls. Existing sequential fd
readiness, timer, join, cancellation and native scope canaries must still pass.
Run both C and pcc-Python runtime mirrors, with a source/runtime-bound A/B of
the original benchmark. No threshold or workload change is permitted.

### CONFIRMED
The interposed-kevent test failed with 101 calls before the patch, then passed
for both runtime mirrors with zero calls. Existing fd/timer/join/cancel and
gateway compiler regressions passed: 22 cases in 84.83 seconds.

The alternating runtime A/B used one compiler and the same input source.
Zero-wait C100 median QPS rose from 8,758.9 to 36,556.9 (4.17x); 100 ms C100
rose from 909.6 to 965.3. This is recorded in the gateway's
`benchmarks/results/2026-09-07-empty-io-poll-ab.json`. All 169 other runtime
IR modules match after replacing only their temporary source-directory
string; py_virtual_thread_runtime is the sole remaining changed module.

The QPS improvement exceeds the on-CPU profile fraction because that profile
deliberately removes kevent waits. Its CPU percentages do not bound wall-time
throughput improvement from removing those waits. Whole-process CPU includes
startup and final formatting of the latency array, whereas QPS excludes them.
Do not label the 4.17x QPS gain a 4.17x CPU improvement.

## No.2 elide repeated retaining stores of the same reference under GC0
### Code Change
GC0's pcc_gc_store_ptr and pcc_gc_store_root return after the store log when
the slot already contains value. That edge already owns the same reference;
retain/release would leave the lifetime and refcount unchanged. GC1–4 keep
their tracing/relocation barriers. Ownership-transferring store_root_take is
unchanged because it must consume the caller's additional reference even
on self-assignment.

The new ownership test passed against the control C and pcc-Python archives,
checking separate local/container/root owners, repeated stores, and the
distinct take contract. The post-empty-IO native profile has 2,499 samples,
including 1,583 (63.3%) in GC/ownership leaves; granule object-start checks
alone account for 477 samples. The unchanged-store subset still needs an
isolated performance verdict.

### CONFIRMED
Both runtime variants passed the ownership and empty-IO regressions (four
cases). Existing generated heap-store barrier telemetry and generator owned
return regressions passed (three cases). The isolated five-repeat runtime A/B
raised zero-wait C100 median throughput from 36,739.3 to 38,239.2 QPS (4.1%).
The 100 ms result remained effectively unchanged (963.7 to 965.0 QPS).
Raw results: gateway `benchmarks/results/2026-09-07-self-store-ab.json`.
This bounded gain is retained; it does not close the remaining asyncio gap.

## No.3 initialize fixed-size generator frames in one operation
### Code Change
Generator factories know their complete frame size, but currently create an
empty list and append one argument or None for every frame slot. This repeats
capacity checks, growth and retaining stores for placeholders. Add
py_gen_frame_new to allocate an ordinary list frame at its final capacity and
populate immortal None slots before GC publication. The compiler will then
store only actual arguments through the ordinary list barriers. The object
layout, frame load/save path and mutable-value ownership remain unchanged.

The C and pcc-Python implementations preserve payload-span registration and
the normal track/publish sequence. A focused test covers sizes 0, 1 and 33,
mutating a slot and collecting with each of GC0–4. Compiler activation and
an alternating control/candidate comparison remain pending.

### Validation
The new helper gate first failed at link time because the symbol did not
exist. The initial C implementation needed the standard malloc declaration;
that build failure is not performance evidence. Runtime validation is in
progress. This targets the frame-construction caller rather than disabling
pointer safety checks; no speed claim is made before the A/B completes.

## Update 2026-09-07: current application baseline and bulk-frame verdict

The optimized application three-way run is published in pcc-gateway commit
61cc568. Both host pcc and pcc1 rebuilt the workload with the same fixed v5
compiler source and optimized runtime archive (SHA-256 514ed8bf2d4b...). All
90 runs / 241,650 requests passed. Zero-wait C100 medians are 22,092.9 /
22,241.0 / 42,647.1 QPS; the current same-run pcc1 gap is 1.92x. System load
varied and is retained in the report. These are application execution timings,
not compiler Stage1/Stage2 speed measurements.

### No.3 DENIED as a speed claim
The fixed-size frame helper passed C and pcc-Python runtime checks under all
five collectors; generator regression/protocol checks also passed. Its first
A/B was noisy and did not establish a gain. A later frozen-source diagnostic
with 20,000 requests/run and a same-run asyncio witness reduced process
instructions/request from 353,997 to 340,787 (3.7%), but user CPU/request stayed
43 microseconds and QPS ranges overlapped. Keep the helper and regression
coverage, with compiler activation disabled by default. Do not repeat this as
an accepted throughput optimization.

### Application profile and the core million-task benchmark
The newly captured optimized pcc1 application's native on-CPU profile has
2,488 samples; 2,249 pass through py_gen_next (90.4%). Request resume is on
610 paths (24.5%), batch resume on 210 (8.4%). GC/ownership leaves dominate;
granule object-start checks alone account for 412 samples (16.6%). This is the
compiled gateway workload, not a profile of the compiler building itself.
Gateway artifact: benchmarks/build/profile-optimized-pcc1-20260907.

The earlier core 1M gate's 1,493,625 tasks/s result is a different boundary:
tests/benchmarks/vthread/vthread_real_runtime.c creates py_virtual_thread_new
with py_None, then times poll_ready and manually calls complete. Its `resume`
metric is ready-queue removal, not execution of a generated Python frame. It
uses the C runtime, while the gateway benchmark uses the pcc-Python runtime.
The result proves scheduler capacity, not the complete Python handler cost.

### Next bounded hypothesis: first-entry frame restoration
Generator factories initialize every nonargument frame slot to immortal None,
but the generated resume function calls py_list_get on every slot even on its
first entry. That does list checks and retaining reads for placeholders. Test
initializing only those local stack slots directly to None when state is zero,
while restoring arguments and all slots after a suspension as before. Keep
the same owned local flags, GC roots, save path and cleanup semantics.

The measured architectural owner is generated resumable application calls,
including the request resume's 24.5% share (ceiling 1.32x even if removed
entirely). The placeholder-read subset is not yet separately timed; this
bounded experiment must have an application A/B verdict and is not presented
as sufficient to close the 1.92x gap. No generator-local borrowing or GC
barrier removal is authorized by this hypothesis.

## No.4 skip placeholder frame reads on first generator entry
### Code Change
With diagnostic PCC_GENERATOR_FIRST_ENTRY_INIT=1, the resume prologue loads
arguments normally and initializes nonargument local roots to immortal None.
One state-zero branch skips restoring those placeholders. Subsequent entries
load every saved local as before. Owned flags, root registration, frame saves
and cleanup are unchanged. The flag is included in frontend cache identity;
it remains off by default until the application A/B is accepted.

### pending
The prerequisite protocol test first exposed an existing missing finally on
handler close, reduced and corrected separately in
generator-handler-close-skips-finally.md. The first-entry optimization is now
being checked with that regression under GC0–4 before a frozen-source A/B.

### Validation update
The new protocol regression passed with the optimization off/on under GC0–4
(2 pytest cases / 10 executions, 15.62 s). The emitted-IR cost gate reduces
first-entry py_list_get calls from three frame slots to the one actual
argument. The existing 12 vthread gateway cases completed successfully with
the flag on. A combined extended run hit its watchdog in Clang linking;
faulthandler located pipeline_native_link.link_with_clang rather than Python
execution. The complete nine-case generator protocol suite then passed with
the application target's self backend (58.00 s). No placeholders means no
first-entry optimization branch is emitted.

The first A/B attempt was invalid before any requests: the copied source root
lacked the AGENTS.md marker required by _repo_root_for_link. A new source-bound
run uses a complete 1,066-file snapshot, one fixed e05708b... application
runtime in both arms, seven rotating repeats, 20,000 zero-wait requests/run,
summary output and a CPython asyncio witness. No speed verdict yet.

### CONFIRMED — bounded host-compiled application gain
After the user stopped other CPU-intensive programs, a fresh normal-output
comparison completed all 42 runs / 441,000 requests. Seven rotating repeats
used one frozen compiler and one runtime archive. Zero-wait C100 median QPS
rose from 37,943.8 (37,556.3–38,440.7) to 39,368.2 (38,333.4–39,885.4), +3.8%.
The asyncio witness measured 86,549.5 QPS. At 100 ms, control/candidate/asyncio
were 966.2 / 966.4 / 973.8 QPS. Gateway report:
benchmarks/results/2026-09-07-first-entry-idle-ab.json.

The earlier summary-output run is incomplete because native min/max over the
dynamic float latency array returned zero; the actual raw latencies remained
correct. This is reduced separately in minmax-dynamic-float-integer-fold.md.
Whole-process CPU/instructions in normal-output mode include expensive final
latency-array formatting and must not be called request-only CPU.

Retain the measured first-entry optimization behind its diagnostic flag until
fresh pcc1 application qualification. It does not close the ~2.2x same-run
asyncio gap. Three adjacent candidates now have gains below 5%; per the
convergence rule, stop selecting neighboring reference-count helpers. The
next performance proposal must address the generated resumable-call owner
(frame construction/completion and lifetime) as a whole, with caller evidence.

## Update: after field-owner repair, profile the actual frame saves
The corrected full-workload A/B is 50,870.8 QPS versus same-run asyncio
88,804.3; details and the 17.48 MiB peak RSS are recorded in
instance-field-iteration-owner-leak.md. A fresh 2,303-sample native profile
places py_gen_next on 2,098 stacks and py_list_set on 403 (17.5% inclusive).
Granule object-start validation is the leaf on 326 samples. These counts
must not be added, and the profiled 1M-request run is not the throughput
comparison. The compiler's _emit_generator_save_frame calls the full list
setter once per persisted local at each suspension, then releases those
local owners and retains them again at resume. Future work should address
that generated suspension/state-transfer cost with GC/finalizer semantics
preserved. The earlier bulk-frame-construction proposal remains denied as a
speed claim; this profile concerns saves of an already existing frame.

## No.5 replace self application emission with LLVM [DENIED as a speed fix]
### Code Change
No production source change. The gateway A/B runner now admits explicitly
named self/LLVM application backends. One frozen field-owner compiler source,
one runtime archive, the same full handler and all three existing optimization
flags are held fixed; only application machine-code emission differs.
The runtime archive's py_obj member was already emitted by llvmlite's target
machine, so this experiment does not compare two runtime implementations.

### DENIED
All 42 runs passed. Zero-wait/C100 seven-repeat median QPS:
self 51,056.2, LLVM 51,288.2 (+0.45%), asyncio 89,852.4. Process instructions
per measured request: 309,574 / 305,041 / 175,335; native user CPU is 19.5 us
in both arms. At 100 ms: 927.7 / 928.8 / 940.3 QPS. Raw evidence is gateway
benchmarks/results/2026-09-07-self-llvm-application-ab.json.
This does not establish a meaningful throughput gain or close the gap.
Do not redirect the task to Stage1/Stage2 compiler-build optimization or
re-label the LLVM oracle as the self-backend result. The fresh application
profile and this controlled comparison point to the frontend-generated
continuation/frame/ownership workload as the next owner to reduce.

## Update: batch width confounds the concurrency curve
The user asked why pcc wins at C1 and loses at C100. The native throughput
itself rises: pcc1 31,741.6 to 48,457.6 QPS. Asyncio rises 9,255.1 to 86,611.5.
In both scripts C is the number of request tasks created in one batch, followed
by an all-request barrier before the next batch. Each request has two child
tasks; execution still uses one carrier/event-loop thread. C is not a CPU-thread
or socket count, and the workload does not continuously replenish completions.

The gateway batch_costs.py diagnostic fits batch_time = fixed + C * incremental
using C10/C100 from the current report. Approximate microsecond coefficients:
pcc1 fixed 8.6, incremental 20.55; asyncio fixed 96.7, incremental 10.58.
These are explanatory two-point fits, not independently timed cost components.
The fit predicts asyncio C1 at 107.28 us versus observed 108.05 us.

An instrumented counting-only run of the unchanged asyncio batch function
observes exactly 700 selector.select(0) calls for 100 batches at every C in
1/10/100. That is 7 polls per batch, or 7/0.7/0.07 per request. The installed
CPython 3.15.0rc1 BaseEventLoop._run_once source unconditionally polls its
selector before draining that iteration's ready callbacks. This supports
amortization of fixed event-loop/polling work; it does not prove that all
96.7 fitted microseconds are selector time. Raw analysis and counter output:
pcc-gateway benchmarks/results/2026-09-07-batch-costs.json.

Implication: the C1 advantage does not establish cheaper incremental native
task execution. The larger-C cost remains consistent with the measured frame/
ownership overhead. A sustained in-flight replenishment benchmark must separate
concurrency from batch barriers before making steady-service scaling claims.
Keep this as an additional workload mode, not a replacement for prior results.

## Update — 2026-09-08 the provenance prerequisite is already satisfied here; the new owner is the continuation-root scan

### The counter the prerequisite needed now exists

`runtime-module-optimizer-throughput.md` recorded that the next step was not an
optimization but a re-measurement, and that the measurement needed a permanent
counter which did not exist: `pcc_gc_metric_add` is `static` in
`py_gc_backend.c` with no port equivalent, and the gateway links the port
archive.

`PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS = 116` closes that. It counts "a
refcount operation reached a pointer that is not a managed object", written at
the `!py_pointer_can_have_header(o)` early return in both refcount-prepare
mirrors (`py_obj.c` and `py_obj.py`), read through `pcc_gc_telemetry` in both,
zeroed by `pcc_gc_telemetry_reset` in both. The port reaches the C-side global
through `global_addr`, so the symbol also had to be registered in
`FREESTANDING_GC_I64_GLOBALS` (`codegen/runtime_abi.py`) — a freestanding
module may only address globals the runtime ABI declares, and without that
registration the closure check rejects the module with "freestanding module
emitted managed-runtime reference".

**Positive control**, because a counter that reads zero proves nothing until it
is shown to register a non-zero:

```
5 x pcc_gc_release(malloc(64))   -> 5
+ 3 x pcc_gc_retain(same)        -> 8
```

### Test [CONFIRMED] — zero on the gateway workload

The instrumented program is `benchmark_native.py` itself, the exact source the
benchmark's native arms compile, with the counter printed into its JSON result.
Default (port) runtime, the three generator switches on:

```
concurrency=1     requests=200     unmanaged_refcount_ops=0
concurrency=10    requests=2000    unmanaged_refcount_ops=0
concurrency=100   requests=20000   unmanaged_refcount_ops=0
```

So on this workload the prerequisite is **already satisfied**. The 213
operations that made Phase B's denial stick were counted on a pcc1 *compile*,
not on the gateway; those two workloads do not share the defect. This does not
reopen Phase B as written — dropping the probe globally still broke pcc1 in
Stage2, and that evidence stands — but it removes the "unknown size" that the
prerequisite was blocked on for this workload.

### Test [CONFIRMED] — the 48% provenance attribution is stale

`scripts/pcc_profile.py`, 20 s, 1535 samples, 2.9% outside the image, on the
same program at concurrency 100:

```
12.8%  pcc_gc_unregister_continuation_root
10.0%  pcc_gc_granule_is_object_start
 5.1%  pcc_gc_index_py_find_slot
 4.6%  _py_incref_prepare
 4.6%  _py_decref_prepare
 2.8%  py_float_to_f64
 2.4%  pcc_gc_pointer_is_managed
 2.1%  pcc_gc_store_ptr
 2.1%  pcc_allocator_initialize_small
 2.0%  _py_decref_finish
 1.0%  _ptr_can_have_header
 0.9%  _pointer_is_managed_no_lock
```

The provenance family (granule start + index find + pointer_is_managed +
can_have_header + no_lock) is **19.4%**, not the recorded ~48%. Phase A closed
most of it. Chasing the probe further is now a sub-20% ceiling, and the
recorded rule about matching optimization scale to the goal gap applies: the
gap to asyncio at C100 is 1.73x, so a candidate under that ceiling cannot
close it.

### The new owner, and why it fits the shape of the gap

`pcc_gc_unregister_continuation_root` is the largest single self-time consumer
and it is a **linear scan of the continuation-root list under the graph lock**
(`freestanding_gc_root_registry.py:216`, mirrored at
`py_gc_backend.c:16008`): it walks from `pcc_gc_continuation_root_head`
comparing each node's `slots` against the argument. Registration pushes at the
head and returns `void`, so the caller keeps nothing that would let removal
skip the walk.

At concurrency 100 there are ~200 live continuation roots (two child tasks per
request), and every await completion walks that list, so the cost per batch is
quadratic in concurrency. That is the same shape as the batch-cost fit recorded
above — pcc1 incremental 20.55 us against asyncio's 10.58 us, with pcc1's
fixed per-batch cost 11x better — and it explains the C1-win / C100-loss
crossover mechanically rather than by attribution.

### Next: the in-repo precedent, not a new design

The frame-root registry already solved exactly this problem for exactly this
key. `pcc_gc_frame_leave` is O(1): `pcc_gc_frame_index_find((void *)slots)`
then `pcc_gc_frame_node_unlink` plus `pcc_gc_frame_index_remove`, over
doubly-linked nodes and a `slots`-keyed index with a preallocation plan
(`pcc_gc_frame_index_plan_capacity` / `_plan_commit`) so it never mallocs under
the graph lock. `reference_gc_frame_roots_need_hash` records why the hash was
required there: frame roots are not released in LIFO order. Continuation roots
across interleaved requests are not either.

Two candidate shapes, in preference order:

1. Have `pcc_gc_register_continuation_root` return its node and store it in
   `PyContinuationStackChunk`, making removal O(1) with no index at all. There
   are exactly two unregister call sites per mirror
   (`py_coroutine.c:393` and `:605`; `py_coroutine.py:493` and `:762`) and all
   four already hold the stack chunk. Cost: a new field in
   `PyContinuationStackChunk`, which is a C/port layout mirror and therefore
   the recurring drift-bug class — both mirrors and any `_Static_assert` must
   move together.
2. Give continuation roots their own `slots`-keyed index alongside the frame
   index. No layout change and no ABI change, at the cost of duplicating the
   index/preallocation machinery.

Either must keep the public `pcc_gc_unregister_continuation_root(slots)` entry
point working, because `runtime_abi.py` and `runtime_effects.py` both declare
it and compiled code may reference it.

## Update — 2026-09-08 correction: the 12.8% continuation-scan attribution was a sampling artifact

### The bad measurement, and the check that would have caught it

The profile in the previous update reported
`pcc_gc_unregister_continuation_root` at **12.8%** self time, top of the table,
and concluded it was the new architectural owner. That number is wrong.

The sampled run was `benchmark_native.py` at concurrency 100 with 4000 rounds,
which completes in about 10.4 s. It was sampled for **20 s**, so the process
exited roughly halfway through the window and only 1535 samples were collected
(a later window-filling run of the same program collects 15000-19000). The
captured set was therefore weighted toward process teardown, where
`py_dealloc_continuation` unregisters thousands of continuation roots back to
back and the list walk genuinely does dominate. Steady request service does not
look like that.

The discrepancy was visible at the time — 1535 samples against 5240 for a
comparable window — and was noted and not chased. The cheap check is to make
the run outlast the sampling window, or to compare sample counts before
believing a share.

### Test [CONFIRMED] — steady-state profile, both arms

`scripts/pcc_profile.py`, 25 s, 19141 samples, 3.2% outside the image, on a
15000-round run so the process outlives the window:

```
11.5%  pcc_gc_granule_is_object_start
 5.4%  _py_decref_prepare
 4.9%  _py_incref_prepare
 4.0%  pcc_gc_pointer_is_managed
 3.7%  pcc_gc_index_py_find_slot
 2.6%  pcc_gc_store_ptr
 2.3%  pcc_allocator_initialize_small
 1.9%  _py_decref_finish
 1.8%  pcc_py_gc_minor_graph_unlock
 1.5%  py_decref
 1.5%  pcc_py_gc_minor_graph_lock
 1.4%  pcc_gc_load_ptr
 1.4%  py_incref
 1.4%  _ptr_can_have_header
 1.1%  pcc_gc_store_root
 1.1%  _pointer_is_managed_no_lock
 1.0%  memset / io_cancel / calloc / pcc_gc_release
```

`pcc_gc_unregister_continuation_root` does not appear in the top 20 **in
either arm**, including the arm that still uses the O(n) walk. So it was never
a steady-state cost.

Corrected shares. The provenance family — `granule_is_object_start` +
`pointer_is_managed` + `index_py_find_slot` + `_ptr_can_have_header` +
`_pointer_is_managed_no_lock` — is **21.7%**, and it is the largest family.
The refcount prepare/finish/incref/decref family is 15.1%. The previous
update's "19.4%" was derived from the same bad profile; the direction it drew
from that number happened to be right, the number was not.

### The O(1) continuation-root handle: [DENIED] as a speed claim, retained as a structure change

Written and measured. Registration now returns its node
(`pcc_gc_register_continuation_root_node`), the node carries a `prev` pointer,
`PyContinuationStackChunk` keeps it at `[24]`, and removal is
`pcc_gc_unregister_continuation_root_node` — an O(1) unlink instead of a walk.
Both mirrors, with `_Static_assert`s pinning the two layouts against the
pcc-Python literal offsets. `pcc_gc_continuation_root_unlink_locked` follows
the `pcc_gc_scheduler_root_unlink_locked` pattern that scheduler roots have
used all along; scheduler roots were already doubly linked with a
register-handle entry point, so this is convergence, not a new idea.

`PCC_CONTINUATION_ROOT_HANDLE=0` makes unmount not retain the node, so removal
falls back to the walk. One archive, one binary, two arms differing in exactly
that, which is what an A/B of this change needs.

Alternating A/B, unmeasured warmups, median of five paired repeats, both arms
asserting identical request counts and a zero unmanaged-refcount counter:

```
concurrency    walk O(n)     handle O(1)    ratio
1                 27,379          27,645   1.0097
10                38,092          37,845   0.9935
100               36,871          37,713   1.0228
```

Noise. Nothing near the 12.8% the bad profile promised, which is the expected
outcome once that attribution is known to be an artifact. Recorded as denied
**as a speed claim**.

It is retained rather than reverted, under the acceptance rule for a
representation change below 1.05: output is exact, no arm regresses beyond
noise, and it removes a named architectural debt — an O(n) walk under the graph
lock. It also makes the one place the walk *was* hot genuinely cheaper: mass
`py_dealloc_continuation` at teardown, which is what the bad profile
accidentally measured.

### What this leaves as the target

Provenance at 21.7%, with the counter from the previous update reading **0**
across 22,200 gateway requests — every one of those probes answers "yes,
managed". So on this workload the probe is pure tax, and the prerequisite that
blocked narrowing it is satisfied here.

State the ceiling honestly: removing the family outright bounds the gain at
1/(1 - 0.217) = **1.28x**, against a 1.73x gap at concurrency 100. It is the
largest single family and it is now provably all-hits, but it cannot close the
gap alone. Any plan that claims otherwise is mis-scoped.

## Update — 2026-09-08 granule span cache: 2.7-3.3% fewer instructions, and the cost model it disproves

### Change

`pcc_gc_granule_is_object_start` (pcc-Python only; the C entry point in
`py_gc_index_table.c` is a `return -1` stub, so the granule map has no C
mirror) now probes a 256-entry direct-mapped cache of span pointers before
walking the granule radix, and fills it from a successful walk.

Why caching a bare span pointer is safe with no key array and no 16-byte
atomic: a key's span binding is permanent — this file has no
unregister/unbind/free path — and span arenas are immortal allocator metadata,
so an entry can never point at freed memory. A stale or colliding entry is
rejected by checking `ptr` against the **span's own base**, and kind, stride,
count, base alignment, exact cell alignment and the LIVE lifecycle word are all
still verified downstream in their original order. An 8-byte aligned slot
cannot tear and correctness depends on no second field.

`pcc_allocator_granule_span_cache_set_fill(0)` clears the cache and stops
filling, so the control arm runs the walk. One archive, one binary, two arms.
The control arm still pays one load of a zero slot and one compare, which
biases against the cache.

### Test [CONFIRMED] — instructions per request

QPS could not resolve this. Two full alternating A/B runs on the same binary
disagreed in **sign** (-4.05% then +1.11% at concurrency 100) with a 24%
min-to-max spread inside one arm, because the machine was loaded with builds
and the effect is ~1% of wall time. Recorded so the next reader does not spend
another hour on QPS for an effect this size.

`/usr/bin/time -lp` instructions retired, marginal cost per request as
`(I(2N) - I(N)) / N` so startup, warmup and machine load all cancel:

```
concurrency   radix walk   span cache   ratio
1              493,743.8    477,384.4   0.9669
10             332,716.3    322,823.0   0.9703
100            325,462.2    316,822.1   0.9735
```

Consistent 2.7-3.3% fewer instructions per request, identical output and a
zero unmanaged-refcount counter in both arms. Retained under the
representation-change acceptance rule: exact output, a deterministic signal
that improves rather than regresses, and it removes real work.

### [DENIED] the cost model, not the change

The design predicted the family would fall from 21.7% to about 8%, on the
premise that the walk is "five serially dependent acquire loads" and therefore
exposed latency. The instruction measurement says the walk is worth 2.7-3.3%
of a whole request, not most of the predicate's 11.5%.

The premise was wrong in a specific and reusable way: the radix's root, L2 and
L3 nodes are a handful of lines that stay resident, so three of those five
loads are L1 hits and cost almost nothing. What remains inside the predicate —
the integer division `carve_offset // stride`, a dozen branches, the span field
loads and the final acquire load of the LIVE word — is the bulk. Counting
dependent loads is not a cost model; only the leaf and span loads actually miss.

Do not re-derive a "collapse the radix" fix from load counts. A reserved
contiguous object region (which would make provenance a subtract and a compare
with no loads at all) is still the architecturally right end state, but it now
has to be justified by the branch and division cost, not by the walk, and it
needs a new freestanding intrinsic: the boundary table exposes only
`page_alloc -> mmap` and `page_free -> munmap`, with no mprotect, no address
hint and no MAP_FIXED.

The division cannot simply be dropped. Its purpose is exact cell alignment, and
the LIVE magic word alone does not replace it: for a pointer inside a live cell
but not at its start, `ptr - 48` reads payload bytes of the previous cell,
which are data-dependent and could equal the magic. Strides carry a 48-byte
header (64, 80, 112, 176, 304, 560, 1072, 2096, 4144, 8240, 16432) so none is a
power of two.

### Where this leaves the 1.73x

Two changes this session, both real and both small: continuation-root removal
O(n) -> O(1) (noise on speed, structural), and this (2.7-3.3% instructions).
Neither moves a 1.73x gap, and per the scale-matching rule three consecutive
sub-5% candidates mean the owner is wrong, not the candidates.

The owner is that the request path does per-object GC bookkeeping at all:
provenance 21.7% + refcount prepare/finish 15.1% + barriers 5.1% + graph
lock/unlock 4.6% = 46.5%, against a 49% per-request reduction needed for
parity. So the next step is not another micro-fix inside the predicate. It is
the recorded prerequisite, now measurable: run
`PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS` on a **pcc1 compile** workload, which
is where the 213 non-managed operations (22 on `py_set_dummy`, ~190 stray
`pcc_gc_release` from compiled ownership cleanup) were counted before Phase A.
The gateway reads 0. If a compile reads 0 too, the frontend's belief that an
operand is an object is verified rather than hoped, and emitting a
provenance-free refcount on statically-proven operands stops being the thing
Phase B was denied for. That measurement needs a stage1 pcc1 built from this
source.

## Update — 2026-09-08 measured ceilings for three levers, and a sizing tool that reported zero for every real module

Three consecutive candidates measured under 5%, which is the condition the
scale-matching guardrail names for stopping and sizing the vertical slice
instead of picking a fourth adjacent helper. So this update sizes levers rather
than writing one.

### The sizing tool was reporting zero for everything [CONFIRMED]

`scripts/pcc_root_elision_sizing.py` printed `store_root=0` for the gateway
module, which plainly contains 72 `pcc_gc_store_root` calls. Same for
`py_obj.ll`, which the self backend compiles every build.

Cause: `parse_self_backend_module` stopped populating
`ParsedFunction.blocks` when the indexed kernel landed. Bodies live in the
packed representation and a consumer asks for the legacy projection through
`IndexedFunctionKernel.materialize_legacy_blocks`
(`self_backend_kernel.py:3293`), with `release_block_projections` clearing it
again. The tool's `if not func.blocks: continue` therefore skipped every
function of every real module. Its ten existing tests all hand
`function_sizing` a hand-built `ParsedFunction`, so none of them ever
exercised the path from IR text to sizing.

Fixed by materializing the projection before sizing, plus
`test_a_parsed_module_reaches_function_sizing_at_all`, which parses IR text and
asserts the store_root site is found. A tool that answers "0" where it means
"not measured" is worse than no tool, and this one had been quoted as the
instrument for the root-elision lever.

### Measured ceilings

```
lever                                        ceiling        basis
granule span cache (done)                    2.7-3.3%       instructions/request
root elision, allocation-free windows        3/51 = 5.9%    fixed tool, gateway module
immortal-singleton barrier elision           0.2-0.5%       3800/79766 barrier args
                                                            x 5.1% barrier self time
```

Parity at concurrency 100 needs about a **49%** cut in per-request work
(20.55 us to 10.5 us). None of the three is that, and root elision -- the lever
this update set out to size -- is 5.9% on the workload that matters.

### The owner, stated from measurement

Our refcount operation is an order of magnitude more expensive than CPython's,
and that is the whole gap. `Py_INCREF` is an increment. `py_incref` is
prepare + finish, and prepare begins with `pcc_gc_granule_is_object_start`
because the runtime cannot assume its argument is an object. CPython never
pays that: its C API is typed and its codegen never hands a non-object to
`Py_INCREF`.

So the lever with the right ceiling is the one the prerequisite work just
unblocked: a provenance-free refcount for operands the frontend proves are
objects. Ceiling is the provenance family (21.7%) plus the part of
prepare/finish (15.1%) that exists only to carry the probe's results — call it
25-30%, so 1.35-1.43x. Still not 1.73x alone, but it is the only candidate
above 10% that has been found, and the counter
(`PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS`) now reads 0 on both measured
workloads, which is what makes the frontend's proof checkable instead of
hopeful.

What must NOT happen: emitting the trusted-lane call without the ratchet. The
runtime already has the shape — `pcc_gc_incref_fresh_native_instance`
(`py_obj.c:566`, `py_obj.py:540`) skips provenance for `PY_TYPE_INSTANCE` and
user classes and falls back to `py_incref` for any other tag, and its single
caller is `pcc_gc_store_ptr_fresh_native_instance`. Widening that lane is the
slice; the counter staying at zero across the gateway, the set/cleanup probe
and a pcc1 compile is the acceptance condition.

## Update — 2026-09-08 [DENIED] fusing the provenance call chain into refcount prepare

### Code Change

Applied and reverted. The refcount fast path reaches the granule predicate
through four pcc-compiled levels:

```
_py_incref_prepare / _py_decref_prepare
  -> _ptr_can_have_header
     -> pcc_gc_pointer_is_managed
        -> pcc_gc_granule_is_object_start
```

`pcc_gc_pointer_is_managed`'s first act is that probe and it returns 1
immediately on a positive, so the prepare paths were changed to call the probe
directly and fall back to the full chain only on a non-positive. The premise
was the granule predicate's own docstring, which records that its internal
five-call chain was fused into straight-line code because "each call pays frame
and root bookkeeping under this compiler's cost model" — the layer above it had
never been fused the same way.

### Result: [DENIED], deterministic regression

Instructions per request, `(I(2N) - I(N)) / N`, two binaries differing only in
this change:

```
concurrency   before        fused         ratio
1             478,708.7     490,070.3     1.0237
10            324,864.7     328,736.9     1.0119
100           316,526.4     321,539.5     1.0158
```

### Mechanism, which is the reusable part

The probe's positive answer is **narrower** than
`pcc_gc_pointer_is_managed`'s. `pcc_gc_granule_is_object_start` answers only
for object-family slab cells; a large object or an allocator `calloc` fallback
is managed but returns -1, and those keep per-object provenance registration
instead. So every managed object outside a small-size-class slab now paid the
granule probe **twice** — once in the new fast path and once inside the
fallback chain — plus the graph lock and the historical chain.

Two lessons, both already recorded in different words and both re-earned here:

* Call-count reduction is not a cost model on this path. This is the second
  time in one session that a change derived from structural reasoning about
  call/load counts measured negative; the first was the granule span cache's
  "five dependent loads" premise.
* Before short-circuiting a predicate with a cheaper one, check that the
  cheaper one's positive set is a **superset** of the traffic, not a subset.
  A narrower fast path turns every miss into double work.

A fusion could only pay if the fallback did not repeat the probe, which means a
`pcc_gc_pointer_is_managed` variant that skips it. That is a further change
whose premise — that call count matters here — has now measured negative twice,
so it is not the next thing to write.


## Update — 2026-09-09: compiler-proven object references, qualification in progress

The current qualified native handler profile (gateway receipt
`2026-09-09-current-native-profile.json`, binary `6419c5bd...`) has 3,842
samples: provenance 22.31%, refcount preparation/finish 18.01%, graph locks
7.44%, barriers 6.61%, disjoint leaf total 54.37%. This is the measured
owner for this iteration; the earlier string-retention work concerns compiler
memory and is not evidence of a request-throughput improvement.

An external observer reads unmanaged-reference counter 116 from the exact
named Mach-O image at process exit. Its positive control deliberately passes
one malloc pointer and reports 1. The prior qualified gateway workload and
pcc1 compilation of `benchmark_native.py` report 0. Missing observer output
is an error, never interpreted as zero. The broader full-compiler and full
HTTP paths still require this check for the new implementation.

The candidate adds opt-in `PCC_KNOWN_OBJECT_REFS` (default 0, included in
frontend cache identity). Proven NEW results and owned local slots select
known-object retain/release. Compiler-private generator frames select object
slot access; freestanding/runtime/C-ABI generators retain the general path.
GC0 uses the existing refcount primitive and terminal release finish, avoiding
provenance lookup and the prepare record on ordinary nonterminal operations.
GC1–4 retain the original checked operations, roots and barriers. A diagnostic
setter routes the known-reference APIs through the checked path to audit the
compiler proof. Foreign/raw pointers remain outside the known-object contract.

Preliminary 7-repeat, rotating, concurrency-100, zero-wait results:

| Candidate | Control QPS | Candidate QPS | asyncio QPS | Instructions/request control → candidate |
|---|---:|---:|---:|---:|
| First known-reference/frame implementation | 47,003 | 51,193 | 80,546 | 321,147 → 294,732 |
| Compact nonterminal reference operations | 46,398 | 52,574 | 84,237 | 319,123 → 282,565 |

The compact candidate gains 13.3% QPS and reduces instructions 11.5%; asyncio
is still 1.60x faster in this workload. Both application arms use the self
backend and the same runtime. Runtime object emission is an external LLVM
reference. These are handler batches, not HTTP or HTTPS throughput.

**Qualification correction:** the incremental runtime combined original
objects with two newly compiled modules; its receipts have mixed compiler
checksums, and the Stage1 harness correctly rejected it. The module receipt
writer also imported a different compiler tree from the actual compile
command. The raw executable comparison remains recorded, but it is not a
qualified runtime or native pcc1 result. A complete frozen-source runtime
rebuild is in progress; no checksum gate was weakened or installed binary
replaced. The diagnostic unchecked ceiling remains explicitly unaccepted.

Before that complete rebuild, 14 focused compiler/cache tests passed,
including verification enabled/disabled and GC0–4 emitted execution for
aliases, weakrefs, finalizers, resurrection and generator suspension. Twenty
C/Python mirror cases also passed across the five collectors. These receipts
apply to the incremental candidate; the coherent rebuild needs its own gates.
Artifacts and reproduction scripts are under gateway
`benchmarks/build/2026-09-09-refcount-specialization/`; comparison receipts are
`2026-09-09-known-frame-ab.json` and `2026-09-09-compact-ref-ab.json`.


### Coherent runtime and native execution (2026-09-09)

The complete 170-member archive was rebuilt from the frozen qualification
source. Every member records compiler checksum `a92b2af96ce95efe8ded06c440b8f8349f0ba8cab8f3ec814a858fcfe4db594c`;
archive SHA-256 is `0dd5e14ab490b21f3fff5ec8e90373fc0e5b463c23164a6be51ebff4dfbbd0ca`.
The isolated Stage1 build succeeded in 371.22s at 6,810,632,192 bytes peak
process-tree RSS. This is Stage1/scoped native validation, not a fixed point,
and the runtime still uses the labeled external LLVM object emitter.

With that native pcc1 compiling both arms against the same coherent archive,
7 rotating repeats at C100/zero wait give **45,459 → 52,314 QPS (+15.1%)**;
instructions/request **320,544 → 283,748 (-11.5%)**. Same-run asyncio is
82,778 QPS. Receipt: gateway `2026-09-09-compact-native-ab.json`. Nineteen
native compiler regressions pass, including direct fast-path and verification
modes across GC0–4. C/Python refcount and private-frame differential cases
also pass. Failure propagation, sibling cancellation and rejected-fork cleanup
pass with the actual structured gateway program on all five collectors.

A startup diagnostic invokes the verification setter before main in the exact
named native image. Its poison-pointer positive control fails without checking
and reports one unmanaged operation with checking. Native compiler execution
(compiling the benchmark), 20,000 measured handler requests plus warmups, and
4,000 set insert/delete cycles each report zero unmanaged operations. This
establishes the checked proof only for these named paths, not all possible
compiler inputs.

The updated native profile has 3,366 samples: provenance 19.55%, refcounts
(including the new APIs) 20.29%, graph locks 5.94%, barriers 4.99%. Together
50.77% of leaf samples remain in object management. `py_gen_frame_set` occurs
in 581 stacks (17.26%), including terminal-release work. Optimized tail calls
can omit callers, so inclusive counts do not provide an exact optimization
ceiling. Direct frame-resident locals are a possible structural follow-up,
requiring evidence on eliminated save/restore ownership traffic and preservation
of GC1–4 roots/relocation; do not repeat the denied bulk-save/transfer experiments
or assert that all frame costs are removable.

### HTTP counter exposed an existing waiter-initialization defect

Both the previously qualified HTTP binary and the new candidate report one
unmanaged operation in the local HTTP functional probe. LLDB stops at
`_note_unmanaged_refcount_op → _py_decref_prepare → _waiter_clear` in
`py_threading.py`. Fresh `malloc(32)` storage was passed to a retaining root
replacement, which attempted to decref its uninitialized first word. The C
mirror correctly initializes that word with a raw NULL store.

The Python mirror now uses `store_ptr(node, 0, null())`. Audited callers either
initialize fresh memory, reuse a node whose root was already released and
unregistered by `_waiter_pop`, or recycle an empty node after root-registration
failure. The root-owning enqueue/pop operations keep their original barriers.
A deterministic poisoned-node harness fails before the fix (counter delta 1,
exit 4). The new test also checks that arbitrary raw bits equal to a live
object address do not consume an owner. It passes across GC0–4. The existing
waiter-pool test now exercises both complete C and Python runtimes: all ten
collector/mirror cases pass. This correction is a correctness fix, not a
claimed source of handler QPS improvement. The HTTP replay after rebuilding
the corrected application runtime is tracked in the gateway waiter audit.


### Final waiter replay and comparison

The corrected application runtime (`3aa7eb02a23e...`) passes the native local
HTTP probe on all five collectors. The exact previously failing probe now
reports **zero** unmanaged reference operations with verification enabled.
Native compilation takes 185.50s at 2,268,479,488 bytes tree RSS. This is the
local HTTP codec/channel/lifecycle probe, not a live socket or HTTPS result.
The compiler binary is unchanged (`788acb37ebab...`), so its linked runtime
and the corrected application archive are deliberately recorded separately.

Final gateway `2026-09-09-compact-final-three-way.json/.md` has 90 validated
runs. Zero-wait pcc1 QPS at C1/C10/C100: 36,013 / 50,244 / 52,213; asyncio:
8,509 / 48,693 / 83,211. C10 leads 3.2% in this run, with non-overlapping
five-run ranges; C100 still has a 1.59x gap. The 294 default gateway tests pass
(20 integration cases deselected). Installed pcc1 and the default-off flag
remain unchanged. Full self-host fixed point, threaded product/live HTTP and
real HTTPS gates remain outside this qualification. Preserve those limits
before any default promotion.
