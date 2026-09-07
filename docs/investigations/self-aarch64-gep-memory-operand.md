# Investigation: self AArch64 materializes single-use field addresses

## Status
active

## Problem Description
The same LLVM-O2 IR executes roughly twice as many process instructions with
self codegen as LLVM codegen. After owned identity-cast cleanup, the five hot
runtime modules contain 1,109 adjacent constant byte-GEP/memory candidates
with offsets within -256..255 and block-local last use at the memory access.
Current self emission materializes each GEP and then its result again for the
load/store instead of using the AArch64 memory instruction's offset field.

## Repro
The first case in `tests/c/test_self_backend_aarch64_address_folding.py`
fails before the change: an eight-byte field load lacks `ldr x10, [x9, #8]`.

## Test [CONFIRMED]
Require signed/unsigned offset selection, sign extension of small GEP index
types, no folding for extra uses or unencodable offsets, and actual indexed
PCO execution of loads/stores with surrounding guards. Preserve volatile,
atomic, PHI, stackmap and overlapping value/address behavior.

## Proposals
- No.1 fold a proven adjacent byte GEP into its sole memory use [pending]

## No.1 fold a proven adjacent byte GEP into its sole memory use
### Code Change
Use the existing indexed last-use and type/operand records. Restrict to a
single constant i8-element GEP followed by a scalar load/store; reject a store
that also uses the address as its value. Leave both instruction metadata
positions in the normal emission loop while eliding only address code. Reuse
the existing load/store/result-publication implementation with a checked base
and encodable offset. No source-level or runtime algorithm change.

### pending
Gate exact execution and metadata, then measure matched native emission.
