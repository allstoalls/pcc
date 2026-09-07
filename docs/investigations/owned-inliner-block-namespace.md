# Investigation: multi-block inliner loses block names and return namespaces

## Status
active

## Problem Description
Integer-comparison inversion makes a previously oversized runtime helper
eligible for bounded multi-block inlining. The resulting py_gc_backend IR
contains `br label %9961.i`: the renamer stripped `while.cond.9961` to its
last dotted component. Both LLVM and self reject this invalid label. The
same shortening also reuses labels across two calls; two-exit inlining uses
a hard-coded `r1` PHI that can collide with a caller argument or prior clone.

## Repro
`/tmp/pcc_inverse_cmp_ab_20260907/py_gc_backend.ll`, at the
`user_py_gc_backend__relocation_reset_finish` definition. LLVM parse reports
`expected instruction opcode` at `%9961.i`; self rejects that branch in the
middle of a block. Host/native optimizer output is equal but equally invalid.
The first reduced unit harness initially lacked the self parser's required
target triple; this harness error is not counted as the root-cause red.

## Test [CONFIRMED]
The real five-module input produces the deterministic malformed label in both
optimizers. Reduced gates exercise dotted names, two calls, and a caller
argument named r1. A related return-shape guard retains a caller's unrelated
return instead of substituting the callee's return value.

## Proposals
- No.1 preserve complete names and allocate collision-free clone namespaces [pending]

## No.1 preserve complete names and allocate collision-free clone namespaces
### Code Change
Retain the whole original block name, use a legal named form for numeric
labels, and check the caller's block/value namespace before choosing a short
upstream-style name. Fall back to a unique call prefix on collisions. The
entry block maps by its position, not a dotted suffix comparison. Return
PHIs also use the shared namespace check. Restrict the call/return rewrite
to a return of the assigned call result (void and bare unused calls retain
the existing handling). No malformed IR is repaired by relaxing a verifier.

### pending
The host/semantic inline packet is being rerun. Require a fresh native
optimizer and exact five-module parse/execute success before new timing.
