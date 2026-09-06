# Investigation: avoid full generator frames on nonparking factory paths

## Status
active

## Problem Description
Gateway's normal TaskScope.fork/close calls pay for generator frames because
their cold cleanup paths may park. After three adjacent sub-5% optimizations,
the measured owner must change rather than selecting more refcount helpers.
Track the application objective in pcc #188.

## Repro
Gateway benchmarks/layers.py derives diagnostic programs from the unchanged
request workload. At C100 / zero wait, five rotated repeats measured:
TaskScope 39,266.7 QPS / 339,683 instructions per request; explicit structured
child joins/cleanup 62,125.3 / 213,667; asyncio 86,322.4 / 175,284. The JSON-only
ablation reaches 141,280.9 but deliberately removes child tasks/waits and is not
a valid application comparison. All 20 runs validated their output/counts.

## Test [CONFIRMED]
The five host-model diagnostic tests verify identical payloads, the declared
wait shape and failure cancellation/draining. New compiler/runtime feature
tests are required before changing gateway's implementation.

The completed-result runtime test first failed at link time because
py_gen_completed was absent. The initial helper passed C/pcc-Python GC0–4
ownership/protocol checks (2 cases, 115.86 s). Adding injected-exception
coverage exposed replacement of throw(ValueError) with StopIteration; the
resume helper now preserves a pending exception and marks the generator done.
The C mirror then passed all five collectors (8.27 s); the updated Python
mirror and factory execution are being qualified together.

## Proposals
- No.1 explicit continuation factories with cheap completed results [pending]

## No.1 explicit continuation factories with cheap completed results
### Code Change
Add a compiler-recognized continuation_factory decorator and two native
operations: continuation(fn, *args) constructs a proven resumable function's
continuation; completed(value) constructs a completed continuation without a
list frame. A factory retains the existing may_park call ABI but emits its
body as an ordinary function. Callers continue driving the returned generator
through the existing protocol. Fast fork/close paths need no full heap frame;
cold paths return a deferred continuation that performs the existing cleanup.

Reuse PyGenObject's arbitrary managed frame/userdata field and normal
StopIteration protocol. Do not change object layout or relax GC barriers.
The initial API admits ordinary instance factory methods with inferred/Any
returns, requires closed-world ordinary resumable continuation targets, and
rejects free-function factories, implicit special-method factories and direct
parking calls. Instance methods are not admitted as spawn targets; eager
factory execution in the spawning task would violate task context. Gateway
caller syntax must remain unchanged.

### pending
Validate completed-value ownership under GC0–4 in both runtime mirrors;
validate fast/slow/error factory calls, method/import boundaries and caller
cancellation. Then compare the full gateway workload with factory paths
enabled/disabled using fixed sources/runtime and both compiler entries.
The diagnostic scope overhead is ~37% of instructions (ceiling ~1.59x if
entirely removed); this first slice may recover only part of it. It is not
claimed to close the entire asyncio gap by itself.

## Update: compiler boundary implemented
The continuation/completed operations participate in may_park propagation and
keep the existing generator-pointer call ABI. Factory method bodies use the
normal emitter; the actual fast body calls py_gen_completed directly and has
no generated resume function. An IR gate proves this body shape, and three
negative gates reject control operations outside factories and direct parking
within them. Intrinsic-owned values use the ordinary root/ownership paths.
The new codegen method was added to the host contract and generated method
metadata. The per-module and whole-self-host boundaries remain pending.

## Update: application A/B and native-compiler qualification
Both runtime mirrors and the factory fast/slow/error execution gate pass
GC0–4 (7 pytest cases, 109.76 s, including the negative compiler gates).
Gateway's existing failure/cancellation/rejected-fork native canary passes
with host pcc (4.52 s), and its default suite passes 290 tests.

The full source-frozen gateway A/B completed 42 runs. At zero wait/C100,
seven-repeat median QPS rises from 42,473.8 (40,777.5–42,945.2) to 47,228.3
(45,835.7–47,915.7), +11.2%. Process instructions per measured request fall
from 339,596 to 302,743 (-10.9%); user CPU falls 23.5 to 21 microseconds.
Same-run asyncio is 91,354.7 QPS. At 100 ms, medians are 926.5 / 932.9 /
938.8 QPS. Source/runtime/input identities stayed fixed. Raw report:
gateway benchmarks/results/2026-09-07-continuation-factory-ab.json.

The factory/compiler changes are an application-performance checkpoint,
not yet a pcc1 result. A fresh compiler is being built from a complete frozen
1,104-file closure with runtime a41b98774078b3848a1c1927-pcc-py. The initial
qualification attempt rejected a snapshot missing fake-libc headers before
compilation; the corrected input uses build_source_files rather than a manual
subset. Application canaries and a new three-way comparison follow the build.

## Update: fresh pcc1 application gates passed
The complete frozen-source build succeeded. Compiler SHA-256:
53978d6bf7dbf812004ef7e83faeabfcfa43c1ff9d4e9d87c74e47847dbd2037.
Its receipt is build/gateway-stage1-factories-20260907-v2/build-receipt.json;
the owned process-tree wrapper completed below the 8 GiB cap (4.89 GB peak).
The new pcc1 independently compiled and executed local HTTP, dashboard and
the native failure/cancellation/rejected-fork fixture: 3 passed in 282.21 s.
Metadata/recorded-bootstrap checks passed (7 cases, 2 deselected, 2.44 s).
Full new-source Stage2/Stage3 fixed-point qualification remains pending;
these are pcc1 application and Stage1 claims, not a new fixed-point claim.
