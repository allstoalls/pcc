# Investigation: self scalar ALU selection copies allocated registers

## Status
active

## Problem Description
After the memory-operand change (see self-aarch64-memory-register-copies.md),
32/64-bit integer binary operations still materialize both operands in x9/x10
and compute in x11, then copy into an existing allocated result register.
The matched same-IR matrix establishes a codegen instruction-count gap;
profile hotspots include reference-count preparation and pointer validation.
Their saved IR contains small add/sub and bitwise operations. This proposal
changes instruction selection only, not the runtime algorithm or allocation
lifetimes. It must not be sold as full LLVM O2 equivalence.

## Repro
A load/load/sub/store chain should consume its assigned registers directly;
a load/add-1/store chain should use an AArch64 immediate operand.

## Test [CONFIRMED]
The focused load/load/sub/store test fails on the original x9/x10/x11
sequence; /tmp/pcc_alu_registers_red.log records the observed failure.
Execute every integer binary operation for i32/i64, including signed and
unsigned division/remainder, negative and boundary immediates, live operands
and result/input aliases, in both target optimization modes. Preserve
narrow signed-operation fallback and existing MADD fusion.

## Proposals
- No.1 select existing registers and bounded add/sub immediates [pending]

## No.1 select existing registers and bounded add/sub immediates
### Code Change
Parameterize indexed ALU emitter register numbers with unchanged defaults.
For exact i32/i64, use typed existing assignments and preserve materializer
fallback for unsupported expressions. For add/sub with a signed literal in
-4095..4095, select the immediate encoding (flip add/sub for a negative
literal). No zero-register use in the immediate Rn/SP field. Remainder must
compute its quotient in scratch x11 separately before writing a potentially
aliased allocated destination. Narrow integer operations retain old behavior.

### pending
Require minimized execution, sensitive backend integration, native emission
equality, then the same-source before/after and LLVM O2 comparison.
