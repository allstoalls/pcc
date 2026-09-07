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
