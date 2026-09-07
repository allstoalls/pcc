# Runtime optimizer checkpoint (2026-09-07)

Continue allstoalls/pcc#188: optimize actual gateway handler throughput toward
exceeding asyncio. User authorizes changes and commit/push in both ~/my/pcc
and ~/my/pcc-gateway. Python remains 3.15.0rc1. Shared ~/.local/bin/pcc1 is
still the historical v84 installation; do not promote without installer gates.

## Current result and artifact

Latest gateway report: benchmarks/results/2026-09-07-runtime-o2-three-way.json/.md.
90 runs / 241,650 valid requests. Zero wait/C100: pcc 57,469.9, pcc1 57,662.8,
asyncio 85,437.0 QPS. pcc1 gap 1.48x, peak RSS 7.95 versus 27.83 MiB.
At C10 both native arms exceed asyncio. This remains the batch/barrier
workload, not continuous in-flight replenishment or HTTP/socket QPS.

Private compiler: build/gateway-stage1-runtime-o2-20260907/pcc1 in core;
SHA-256 2b08f3a7aac1c6a69a13ba7ee0014856d9e83fac2f48b5b9a0946909f25a4d7b.
Receipt/source-snapshot/runtime-bundle remain beside it. Wrapper:
/tmp/pcc_gateway_runtime_o2_pcc1. It enables the three previously accepted
first-entry/completed-result/direct-generator flags for application tests.
Normal optimized runtime cache: 0c07182db3ffebe0338c29ec-pcc-py;
archive SHA-256 1092c06a9c3bc55ed7ba7dd589113414710b42834449563f25b85e8f3dbeffdd.

## What changed

ir_to_obj previously emitted LLVM objects without a full module optimizer,
after bounded mem2reg/sroa frontend cleanup. Its normal runtime build now runs
LLVM O2 for exactly py_obj, py_list, py_gen, py_gc_backend and
freestanding_gc_index_table. Other callers/modules retain their prior path.
Runtime codegen checksums now also bind emitter source and LLVM version;
frontend identity deliberately excludes pcc/tools, so it was insufficient.

Controlled runtime-only A/Bs: first three modules +12.1% QPS; adding the last
two +6.6% in a separate comparison. Do not multiply these into a new measured
number. scripts/reoptimize_runtime_ir.py reproduces the diagnostic from exact
existing runtime IR and verifies only the selected archive members changed.
It keeps source provenance and uses llvmlite, not host C compilation.

Three frame experiments failed to materially improve throughput. Bulk dispatch
regressed 3.25% (source 34c3d139); save-only transfer was flat/slightly slower
(source 6d1a7aa3); bidirectional transfer was +0.21% with overlapping ranges
(source b43dc61e). Application activation of all three is withdrawn and negative
IR tests enforce that. Runtime helpers remain diagnostic oracles. Do not
reintroduce the flags or repeat adjacent frame-helper tuning as a speed fix.

## Verification / pending boundaries

- Optimizer/provenance 52 passed; archive consumers 24 passed after sharding
  an outer-timeout run (not a failing assertion).
- Normal rebuilt runtime: 21 native generator/field/ownership cases passed.
- Fresh pcc1: 8 source cases / 40 GC0–4 executions passed; 3 native application
  canaries passed. Logs /tmp/pcc_runtime_o2_pcc1_gates.log and
  /tmp/pcc_runtime_o2_pcc1_examples.log.
- Gateway default: 290 passed, 20 integration cases deselected.
- IR fallback + recorded bootstrap: 10 passed, 2 unavailable deselected.
- New-source Stage2/Stage3 fixed point and full installer qualification remain
  pending. The previous full fallback file timed out in per-module closure;
  shard phases with PCC_TEST_LIVE_PROGRESS=1 instead of repeating that envelope.

## Next useful work

Read runtime-module-optimizer-throughput.md and generator-bulk-frame-save.md,
plus required playbook/denied-experiment pages. Profile the new optimized
application before selecting another candidate. The previous baseline's
freestanding_allocator alone owned 504/2303 CPU leaves (21.9%), including
object-start validation. It defines malloc/calloc/free/realloc: full optimizer
activation requires explicit no-builtin/libcall-recursion validation. It is
excluded from the production O2 policy deliberately. Keep the self backend,
GC/ownership guarantees and source/runtime receipts intact.

The earlier self/LLVM application comparison was only backend switching.
An explicit application Clang -O2 A/B also had no gain; runtime optimization
was the productive boundary. Do not confuse these with compiler-build speed.
