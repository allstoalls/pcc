# Investigation: instance field iteration retains completed task trees

## Status
active

## Problem Description
The gateway's native peak RSS scales with total request count. An isolated
observer shows tracked objects increasing from 28 to 78,083 after 5,000
requests, then by another 78,055 per batch; explicit collection reports zero.
Smaller tasks and generators release normally, but iterating a scope's child
list retains the list and every child task. This is a correctness prerequisite
for further performance work in pcc #188.

## Repro
The minimized fixture stores [Item()] in a dynamically typed Holder.values
field, then binds or iterates that field. After exercise() returns and collect
runs, Item.__del__ must append exactly one release marker as in CPython.
See tests/python/test_instance_field_read_ownership.py.

## Test [CONFIRMED]
The isolated gateway observer reproduces the growth without adding unsafe
imports to the workload module. A first in-module raw observer changed the
compiler's ownership mode and caused an additional probe-only leak; that
variant is rejected as application evidence. With the observer separated,
plain allocation, ordinary generators and simple tasks stay flat. Iterating a
scope adds five tracked objects per single-child scope (three lists, one
generator, one virtual thread). Binding the field before iterating also leaks.

## Proposals
- No.1 record actual field-getter ownership and consume iterable temporaries [pending]

## No.1 record actual field-getter ownership and consume iterable temporaries
### Code Change
py_instance_get_field increments its result reference unconditionally.
The frontend's attribute classifier treats a Dyn field as borrowed, while
for-loop entry does not release an owned source after constructing its iterator.
Record the getter's actual owner at emission, release owned scalar boxes after
unboxing, and transfer/consume iterable owners through the existing rooted
iterator lifetime. Keep class-attribute borrowed paths separate.

### pending
Observe the minimized failure first, then verify dynamic fields, iteration,
generator suspension and finalizers. Do not interpret skipped destruction as
a performance gain, and keep the original handler workload unchanged.

## Update: matched ownership probes and exception exit
The initial `Holder([Item()])` fixture also leaked without reading the field;
it mixed a separate temporary-constructor-argument problem into the test.
The corrected fixture binds Item and its source list first. Untouched/bound
cases now pass, and the direct field-iteration case failed before consuming
its owned source. All three pass after the iterator-root transfer repair.
The real gateway observer falls from +78,055 tracked objects per 5,000-request
invocation to +3. The small residual is still open; no zero-leak claim.

The expanded suspended-iterator gate exposed a separate error-exit owner:
exhaustion, break and return pass GC0–4, but an escaping ValueError retains the
restored generator locals. `_ensure_fn_err_exit` releases only exact-int and
for-target names, although generator entry retains every persisted frame slot.
A generator error must mark it done and release its full preplanned local
ownership ledger before returning the error sentinel. Keep ordinary-function
error cleanup outside this bounded change.

## Update: root-exit repair and focused validation
Calling the full normal-return cleanup from `_ensure_fn_err_exit` was rejected
by precise stack-map analysis: that cleanup and the existing error-exit
back-patcher both left the same root. Keep the established error-root ledger
and extend only its owned-value releases to preplanned generator locals.
The corrected implementation passes 12 focused cases in 22.54 s, including
actual GC0–4 execution for normal reads and suspended iteration exiting via
exhaustion, break, return, ValueError and close, plus the prior handler-close
and first-entry regressions. Four existing attribute ownership canaries pass
in 6.05 s. Full pcc1/self-host qualification is still pending.

## Update: full workload A/B and reproducible lifetime probe [CONFIRMED]
The frozen-source A/B completed 42 runs. At zero wait/C100, control/candidate/
asyncio medians are 53,489.8 / 50,870.8 / 88,804.3 QPS. Correct ownership has a
4.9% throughput cost; process instructions rise from about 272k to 310k per
measured request. Peak RSS falls from 141.31 to 17.48 MiB (asyncio 27.95 MiB).
This is a correctness repair, not an accepted speed improvement.

The retained gateway script benchmarks/lifetime.py derives the full handler
from benchmark_native.py and isolates the unsafe counter in heap_observer.py.
Its control tracked counts are 28/78084/156140/234196; candidate counts are
28/32/36/40 after three 5,000-request invocations plus warmups. This runner
parses its repeat argument in the loop, unlike the earlier hard-coded scratch
probe; report its +4 residual separately from the scratch probe's +3.

HTTP and dashboard host-pcc canaries passed, with the dashboard isolated after
a combined outer timeout (79.56 s). Failure/cancellation/rejected-fork passed
4.36 s. Seven metadata/recorded-bootstrap/knowledge checks passed 2.29 s;
those inspect the previously recorded bootstrap artifacts, not this source's
fixed point. A new Stage1 build is now qualifying the frozen field-owners
source with runtime a8feaa180e6fc84f76c70b33, 2 workers, 8 GiB cap.

## Update: fresh pcc1 application qualification
Stage1 completed in 375.83 s with peak process-tree RSS 5,104,893,952 bytes.
Receipt: build/gateway-stage1-field-owners-20260907/build-receipt.json;
compiler SHA-256 0ff76d8bf13985abfc04c9fe25bcbee5aa3043a8bed29b140f79163deb418bff,
runtime archive 9814efd78e4c6a62ddc07fc54c53573cc28f5dfbe7cf022703d1258bfd70ea2f.
The exact eight field-owner/suspended-iterator regression sources were also
compiled by this pcc1, preserving all test assertions and CPython oracles:
40 actual GC0–4 executions passed in 83.39 s. Adapter/log:
/tmp/pcc_field_pcc1_gate.py and /tmp/pcc_field_pcc1_gate.log; source/executables
remain under build/gateway-field-pcc1-regressions-20260907.

The fresh 90-run pcc/pcc1/asyncio comparison passed all 241,650 requests.
Zero-wait/C100 medians are 49,087.9 / 48,457.6 / 86,611.5 QPS, with peak RSS
7.88 / 7.98 / 27.62 MiB. Report and full latency samples are in gateway
benchmarks/results/2026-09-07-field-owners-three-way.json/.md. This qualifies
the application compilation/execution boundary, not a new Stage2/Stage3 fixed
point or shared-installation promotion.

## Update: pcc1 canaries and fallback guard
The new pcc1 passed all three HTTP/dashboard/failure-cleanup canaries in
292.32 s. Gateway default tests passed: 290, 20 native cases deselected,
3.87 s. The IR fallback suite first stopped at an adjacency assertion that
allowed only three lines between a retained field read and iterator creation;
new root setup legitimately exceeds that distance. The guard now follows the
exact field-result SSA value into py_obj_iter and requires its subsequent
release, while preserving all layout/dynamic-subclass/fallback-count checks.
All eight IR fallback cases pass in 29.07 s. The broader fallback baseline
file is now running; Stage2/Stage3 remain unclaimed.

## Update: broader fallback timeout boundary
The complete test_fallback_baseline.py command reached 20 passing nodes, then
its 120-second outer watchdog expired in test_closure_per_module_codegen_passes.
It emitted no failing assertion and no final summary; it is not a green
full-file gate. No child processes remained. Keep full closure/fixed-point
qualification pending and retain /tmp/pcc_field_fallback_gate.log. The focused
IR fallback suite and native application qualification above remain separate
valid evidence. Subsequent closure checks must be sharded with
PCC_TEST_LIVE_PROGRESS=1 rather than repeat the same full-file time envelope.
