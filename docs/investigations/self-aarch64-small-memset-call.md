# Investigation: self AArch64 calls memset for small unaligned zero fills

## Status
active

## Problem Description
The same LLVM-O2 IR emits a gateway runtime that reaches about 34,213 QPS
with self codegen versus 57,554 with LLVM codegen. A 2,295-sample CPU profile
of the self artifact records 137 leaves in memset. The incref/decref prepare
helpers each contain a constant 24-byte llvm.memset with alignment 1. Self
only inlines aligned 32/64/128-byte SIMD cases and emits a full memset call
for this hot shape. No runtime algorithm change is proposed.

## Repro
`tests/c/test_self_backend.py -k small_unaligned_zero_memset` asserts that
bounded constant zero fills emit stores without `bl _memset`. The zero-byte
case fails first before the change. Real profile:
`/tmp/pcc_self_codegen_profile_20260907/native.folded`.

## Test [CONFIRMED]
Observe the pre-change external call in the reduced case. Require byte-exact
execution including unaligned destinations, non-eight-byte lengths and guard
bytes, and retain the existing fallback for volatile/nonzero/large/dynamic
fills. The existing aligned SIMD cases must stay unchanged.

## Proposals
- No.1 inline nonvolatile constant zero fills up to 128 bytes [pending]

## No.1 inline nonvolatile constant zero fills up to 128 bytes
### Code Change
Reuse existing exact-size aggregate chunking and structured memory-instruction
emission to store from xzr/wzr. AArch64 normal-memory unaligned stores are
supported; no store may extend beyond the intrinsic's byte count. This runs
before final stack-map/unwind offsets and supports the packed native route.

### CONFIRMED for the newly covered lowering; limited performance scope
The executable byte-boundary regression found missing halfword instruction
support in the owned assembler. Add LDRH/STRH and LDURH/STURH to scalar word
and text encoding, including w-register checks, and allow direct halfword
instruction capture. The 13-case packet passes, including both structured
emission modes, every modified byte and untouched guards (0.40 s).

Structured/encoder packet: 130 passed, 12 deselected (0.20 s). The old pinned
llvmlite 0.46 corpus gate is unavailable in the current 0.47 environment; it
must not be reported as run. A separate explicit LLVM 20.1.8/llvmlite 0.47
halfword byte differential passes; /tmp/pcc_halfword_reference_20260907.json
records its exact instruction bytes and provenance.

Matched timing, seven repeats / six arms: LLVM-O2-IR self codegen improves
34,344.5 -> 35,170.4 QPS (+2.4%), process instructions -4.2%. Owned-IR self
codegen is flat: 29,763.5 -> 29,840.6 QPS, instructions essentially unchanged.
The owned IR still uses individual stores; LLVM's middle-end creates the
24-byte memset shape. Keep this as correct lowering coverage and an isolated
backend improvement, not as an established owned-pipeline speedup.

Artifacts: /tmp/pcc_small_memset_ab_20260907. Native pcc1 qualification is still
pending; the retained default emission policy has not been promoted.
