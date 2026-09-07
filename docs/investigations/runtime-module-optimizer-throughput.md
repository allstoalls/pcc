# Investigation: runtime object emission omits full module optimization

## Status
active

## Problem Description
Continue pcc #188 after three frame protocol candidates fail to materially
improve the full workload. The latest published pcc1 gap is 1.79x. The runtime
owns almost all sampled leaves; py_obj alone has 677/2303 (29.4%), py_list
101/2303 (4.4%), py_gc_backend 254/2303 (11.0%), and the GC index table
95/2303 (4.1%). These are disjoint leaf counts mapped through the exact
baseline archive's nm inventory. Predecessor: generator-bulk-frame-save.md.

## Repro
pcc/tools/ir_to_obj.py verifies IR, creates a target machine and emits an
object. It does not run LLVM's full module pipeline. Runtime source emission
uses the frontend's bounded default mem2reg/sroa cleanup. The earlier self/
LLVM application comparison only changed emission backend; a new explicit
Clang -O2 application comparison also has no gain (51,647.0 / 51,338.8 QPS).
That does not bound optimizing the runtime's helper call chains.

## Test [CONFIRMED]
The pilot consumes the exact existing IR from the baseline runtime archive,
checks every input IR against its receipt, optimizes selected modules with
LLVM 20.1.8's O2 module pipeline, and emits objects with the existing llvmlite
target-machine path. It rebuilds/verifies archive provenance and asserts that
only selected members changed. It changes no Python runtime source and uses
no host C compiler. scripts/reoptimize_runtime_ir.py retains a complete report.

## Proposals
- No.1 optimize py_obj, py_list and py_gen [CONFIRMED for host application]
- No.2 extend to py_gc_backend and the GC index table [pending]

## No.1 optimize py_obj, py_list and py_gen
### Code Change
Diagnostic re-emission of only these three objects, preserving all other
archive member bytes. Source runtime: a8feaa180e6fc84f76c70b33-pcc-py.
Candidate: ~/.cache/pcc/gateway-optimization-20260907/runtime-ir-o2-v2.
The report retains optimizer level/version, input and output hashes, and an
exact optimizer-script.py snapshot. Initial attempt rejected the logical
source-path prefix; v2 uses the provenance module's canonical path resolver.

### CONFIRMED
The candidate passes eight field/suspended-iterator sources under GC0–4
(40 executions, 27.48 s) and the gateway failure/cancellation/rejected-fork
canary (7.72 s). Source and compiler selection are bound to the frozen
field-owners tree, whose runtime source matches the baseline archive.

All 42 full-workload A/B runs pass. Zero-wait/C100 medians are 49,193.0 /
55,135.9 / 86,648.8 QPS (control/candidate/asyncio), a 12.1% native gain.
Instructions/request fall 309,673 to 287,679 (7.1%); user CPU falls 20 to
18 us. At 100 ms: 950.1 / 960.2 / 960.7 QPS. Raw report is gateway
benchmarks/results/2026-09-07-runtime-ir-o2-ab.json. This is an application
runtime gain; fresh pcc1 application checks and production-build integration
are still pending. It does not close the whole asyncio gap.

## No.2 extend to py_gc_backend and the GC index table
### Code Change
The exact baseline nm inventory maps pointer validation/freeing to
py_gc_backend.o and GC-index removal to freestanding_gc_index_table.o.
Optimize those two additional modules with the same pipeline. The allocator
is deliberately excluded at this step: it defines malloc/calloc/free and
requires a separate no-builtin/libcall recursion check before optimization.

### pending
Candidate runtime-ir-o2-five has exactly the five selected changed members.
Run the same GC/application gates before its matched A/B. Do not label this
an accepted gain from IR-size changes alone.

## No.2 verdict [CONFIRMED for host application]
Five-module runtime passes 8 sources / 40 GC0–4 executions (26.43 s) and the
native cleanup canary (7.43 s). Its 42-run A/B uses the three-module optimized
runtime as control. Zero-wait/C100 medians are 55,401.7 / 59,032.4 / 88,549.8
QPS (control/candidate/asyncio), a further 6.6% gain. Instructions/request
fall 287,548 to 276,886; user CPU falls 18 to 17 us. At 100 ms: 937.9 /
940.0 / 946.6 QPS. Report: gateway benchmarks/results/2026-09-07-runtime-ir-o2-five-ab.json.
Do not multiply gains from different reports into a newly measured number.

## Update: normal build integration
ir_to_obj now selects the O2 module pipeline for exactly the five qualified
runtime sources when invoked with their runtime/source provenance metadata.
Other object-emission callers and libc/allocator implementations keep the old
path. This is a fixed, source-versioned policy, not an ambient compiler flag.
Runtime object codegen checksums now include the emitter implementation and
LLVM version, because frontend bootstrap identity deliberately excludes
pcc/tools. Otherwise changed optimizer policy could reuse old object receipts.
New tests cover the policy boundary, real emitted execution, elimination of
helper stack work, and cache invalidation. Normal runtime rebuild and pcc1
application qualification are the next gates; no installation promotion yet.

## Update: optimizer/provenance gates
A combined 90-second test invocation stopped at its outer timeout after 62
passing nodes, without a failing assertion or final summary. The two file
shards then completed: optimizer/provenance 52 passed (56.22 s), runtime
archive consumers 24 passed (72.44 s). No stale object/manifest bypass was
introduced. A normal cached runtime rebuild now drives the native generator/
field/ownership regressions with the fixed five-module policy.

## Update: normal runtime artifact
The regular cached build produces 0c07182db3ffebe0338c29ec-pcc-py/
libpy_runtime_pcc_py.a with the fixed five-module optimizer policy. Native
field/generator/ownership regressions pass (21 cases, 140.64 s including the
build). Prior frame experiments remain callable diagnostic oracles, while
normal generator code uses the original frame path; negative IR tests enforce
that the withdrawn flags do not activate them.

Source is frozen at ~/.cache/pcc/gateway-optimization-20260907/runtime-optimized/compiler.
A fresh Stage1 uses that source and regular optimized archive, two workers,
and an 8 GiB process-tree cap. Shared installation remains unchanged.

## Update: fresh pcc1 and final application comparison
Stage1 completed in 387.74 s, peak tree RSS 5,026,562,048 bytes. Directory:
build/gateway-stage1-runtime-o2-20260907; compiler SHA-256
2b08f3a7aac1c6a69a13ba7ee0014856d9e83fac2f48b5b9a0946909f25a4d7b.
The normal runtime archive SHA-256 is
1092c06a9c3bc55ed7ba7dd589113414710b42834449563f25b85e8f3dbeffdd.

Fresh pcc1 passes all eight field/suspended-iterator source cases under GC0–4
(40 executions, 77.43 s), and HTTP/dashboard/failure-cleanup canaries
(3 cases, 261.24 s). Gateway default tests: 290 passed, 20 integration cases
deselected (3.66 s). IR fallback plus recorded-bootstrap checks: 10 passed,
2 unavailable cases deselected (31.99 s). These recorded bootstrap checks are
not a new-source Stage2/Stage3 fixed point.

The normal-build 90-run comparison validates all 241,650 requests. Zero-wait/
C100 medians are 57,469.9 / 57,662.8 / 85,437.0 QPS (pcc/pcc1/asyncio),
with peak RSS 7.97 / 7.95 / 27.83 MiB. At C10: 55,892.2 / 55,807.9 /
48,116.3 QPS. The remaining C100 pcc1 gap is 1.48x; do not claim an asyncio
win. Gateway report: benchmarks/results/2026-09-07-runtime-o2-three-way.json/.md.
Shared installation and new-source Stage2/Stage3 qualification remain pending.

## Update: profile after the accepted optimizer policy
The fresh pcc1 application's 1M-request diagnostic completed. Its CPU profile
has 2,302 samples: object-start validation is the leaf on 368 (16.0%),
py_decref_prepare on 174, pointer-is-managed on 136, py_incref_prepare on 103,
and py_decref_finish on 71. These are sampled shares, not cross-run absolute
cost comparisons. Remaining helper chains and allocator validation warrant
further attribution; do not reopen the rejected frame micro-experiments.
Profile and folded stacks: gateway benchmarks/results/2026-09-07-runtime-o2-profile.json/.folded.
