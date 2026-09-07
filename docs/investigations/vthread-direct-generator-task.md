# Investigation: remove the redundant typed continuation around generator tasks

## Status
active

## Problem Description
Generator-backed spawn currently creates a generator, a typed continuation,
and a virtual-thread object. The typed continuation contains only slot zero,
which points at that generator. Its constructor separately allocates a
24-byte stack-chunk descriptor and a slot array, in addition to the managed
continuation object. This duplicates the resumable owner for every real task.
Continue application throughput work in pcc #188.

## Repro
The current gateway has three actual tasks per request (request plus two
children). Native generator spawn emits py_continuation_new_typed with one
slot and py_virtual_thread_resume_generator, which immediately reads slot zero.
The new C-probe-backed test attaches a generator directly to a virtual thread
and requires two resumes, the correct return value, cancellation before entry,
failure publication and empty scheduler waitsets under GC0–4.

## Test [CONFIRMED]
tests/python/test_vthread_direct_generator.py failed under control GC0: the
task was completed without executing its generator (exit 4). The corrected C
runtime passed all five collectors in 8.24 s. Both runtime mirrors plus
compiler/factory execution gates then passed (8 cases, 109.61 s).

## Proposals
- No.1 let virtual threads execute an owned generator directly [pending]

## No.1 let virtual threads execute an owned generator directly
### Code Change
Reuse the existing virtual-thread continuation pointer as a generic managed
execution owner. run_once handles GEN as well as CONTINUATION, and both routes
share the existing generator-resume/cancellation logic. No object layout or
GC slot visitor changes are needed. The compiler can then omit the typed
continuation descriptor/object/slot-array for generator-backed spawn only;
ordinary native callbacks retain their existing typed continuation path.

### pending
This removes three allocations per generator task and the slot-zero adapter
lookup. It addresses the measured generated-call/task owner, whose cost remains
between the JSON-only diagnostic floor and the full handler. The precise
end-to-end share must be established by controlled application A/B; allocation
counts alone are not a speed claim. Validate both runtime mirrors, collector
roots, cancellation/failure, timers and fd readiness before accepting a gain.

## Update: application gate readiness
The compiler's PCC_DIRECT_GENERATOR_TASKS flag now omits the single-slot typed
continuation only for generator-backed spawn; its IR gate counts two typed
continuations before versus one after when a normal callback is also spawned.
The flag is part of frontend cache identity and remains off by default.

Sequential real TCP under GC0–4, dynamic callback/plain-generator separation,
live values/finally and parent exception routing passed (4 cases, 12.86 s).
Gateway's native failure/cancellation/rejected-fork canary passed (4.49 s).
An application A/B uses one frozen compiler and runtime a8feaa180e6fc84f76c70b33,
with first-entry and completed-result optimizations fixed on in both arms.
The only comparison switch is direct generator ownership. No speed verdict yet.

## Update: controlled application result [CONFIRMED]
The 42-run full-workload A/B completed with seven rotating repeats per wait
level. Zero-wait/C100 medians: control 50,862.8, candidate 52,908.8, asyncio
88,979.1 QPS. Candidate gains 4.0%; whole-process instructions per measured
request fall 281,946 to 272,178 (3.5%), user CPU 19.5 to 18.5 us, and peak RSS
171.20 to 141.31 MiB. At 100 ms the corresponding QPS are 937.6/942.1/940.5.
All samples remain in pcc-gateway benchmarks/results/2026-09-07-direct-generator-ab.json.
The flag remains opt-in; new pcc1 qualification is pending. This accepts the
bounded allocation reduction, not an asyncio win. A subsequent ownership
investigation found retained task trees in field iteration; memory and QPS
must be remeasured after that correctness repair before claiming service
lifetime efficiency (instance-field-iteration-owner-leak.md).
