# Investigation: self memory selection copies already allocated registers

## Status
active

## Problem Description
The indexed AArch64 memory emitter always materializes addresses into x9 and
values into x10, then copies loaded values into their allocated registers.
Even after register allocation and GEP folding, this retains extra copies and
redundant narrow masks. LLVM emits memory operations using the assigned
registers directly. This does not require a broader allocation/lifetime policy.

## Repro
`tests/c/test_self_backend_aarch64_memory_registers.py` fails before the change:
the load/store chain does not use its allocated x1 directly. Other reduced
cases cover a zero store and a load whose base/result share one register.

## Test [CONFIRMED]
The first reduced load/store case fails on the pre-change fixed x10 output.
Require byte-executed indexed assembly in both target optimization modes,
register pressure and call/PHI exclusions, guards/unaligned addresses and
precise stack-map fragment equality.

## Proposals
- No.1 select existing typed register assignments as memory operands [pending]

## No.1 select existing typed register assignments as memory operands
### Code Change
Add a typed indexed lookup over the existing allocation table. Respect alloca
address priority and retain the allocator's existing register/type range.
Use assigned source/base/destination registers for indirect scalar memory
operations; use xzr/wzr for indirect literal zero stores. Exact alloca stores
retain their existing fast path. LDRB/LDRH already zero-extend, so no extra
move/mask is needed for an allocated result. Keep the old fallback when an
operand needs general materialization; no call-crossing or global allocation.

### pending
Focused tests pass; broader execution, native emission and matched timing
still determine the verdict. Do not claim a performance gain from shape.

## Update 2026-09-07 — review and contended measurement

The focused execution/stack-map gate passed 20 tests and broader backend
coverage passed 362. Three independent read-only code-converge reviews found
no new semantic defect. The plan was narrowed to indirect stores (the exact
alloca-store path is unchanged), and literal-zero selection now also requires
a negative indexed operand ID. An added byte-execution test transfers i16
65535 through an unaligned address, verifies neighboring guards, and writes
a null pointer; both target modes pass. The affected packet passed 29 tests.

The first 42-run timing packet is **DENIED as throughput evidence**: another
project's golangci-lint consumed roughly six cores, and a virtual machine also
ran. QPS varied by over 2x even within an unchanged arm. Retain it only as a
contended diagnostic: gateway benchmarks/results/2026-09-07-self-memory-
registers-contended.json. Process instruction medians fell 10.591B → 10.432B
on owned IR and 10.393B → 10.256B on LLVM-O2 IR. This small instruction-count
reduction is not proof of a QPS gain or LLVM O2 parity. Quiet rerun pending.

## Update 2026-09-07 — native emission and application validation

### CONFIRMED — removes redundant memory instructions; QPS unqualified
Native emitter SHA256 44742eb02a20d28f95ab6ca4332b63fb568e4c0ba68212f21201368c6714c606
produces all five owned-IR PCOs byte-identically to host pcc (report:
/tmp/pcc_native_memory_emission_20260907/report.json). Emission runs with
PATH=/nonexistent and host Python/cc disabled. The emitter was bootstrapped
by standard-library host pcc using an explicitly prebuilt runtime; this is
not full native toolchain construction. All 30 application smoke cases pass
across GC0–4, two IR arms, C1/C100 zero-delay and C100 one-ms delay. This
is application coverage, not a full GC stress or bootstrap fixed-point gate.

The bounded instruction-selection proposal is retained on deterministic
instruction elimination, executed semantics and native byte equivalence. No
throughput-win claim is accepted until the contended run is replaced. Next
separate investigation examines the same extra copies in scalar ALU emission.
