# Investigation: owned IR keeps identity casts and boolean round trips

## Status
active

## Problem Description
The owned optimized runtime IR still contains `bitcast ptr %p to ptr` and
`zext i1 %condition to i64` followed by an equality comparison with zero/one
in the hot incref/decref preparation functions. LLVM O2 removes these shapes.
Self codegen remains about 29.7k QPS on the owned IR versus 34.2k on LLVM-O2
IR before the small-memset improvement. This targets compiler operations,
not runtime algorithms or ordinary Python integer semantics.

## Repro
`tests/python/test_owned_ir_canonicalization.py` fails at the first boolean
round-trip case before the change (`changed` is false).

## Test [CONFIRMED]
Four eq/ne and zero/one combinations recover the original i1 or its logical
inverse. Non-i1 extensions, sign extensions and other constants are retained.
Identity pointer casts must preserve exact SSA token boundaries and assembly
strings/comments. A 128-cast chain must not leave undefined values after a
bounded number of replacement sweeps. 42 owned/legacy semantic gates pass.

## Proposals
- No.1 eliminate proven representation round trips [pending]

## No.1 eliminate proven representation round trips
### Code Change
Owned InstructionSimplify collects i1 zero-extension definitions per function,
folds matching equality tests, and removes same-type scalar/pointer bitcasts.
Alias chains are resolved with memoization before one existing lexical token
replacement, avoiding regex prefix corruption and dangling long-chain names.
Cyclic aliases retain the original function for later validation.

### pending
Compile and run the native optimizer on the extended regression and exact
runtime inputs, then compare same-codegen application artifacts. Native
optimizer execution/time/RSS and complete workload results are required.

## No.1 verdict [CONFIRMED for the owned pipeline]
Extended native optimizer regression passes (23.17 s). Native and host output
are byte-equal on all five exact runtime inputs. Their complete build/check
packet takes 21.49 s and peaks at 1,498,890,240 bytes. Seven rotating runs per
arm: owned control 29,233.1 -> canonicalized 31,675.7 QPS (+8.4%); process
instructions 13.162B -> 11.573B (-12.1%). Same-run LLVM O2 57,525.8 QPS and
asyncio 83,073.7. This is a partial native tier, not LLVM O2 parity.

## No.2 invert integer comparisons through boolean xor [pending]
Hot ownership guards still use `icmp` followed by `xor i1 ..., true`.
LLVM uses the inverse predicate directly. Propose the corresponding integer
comparison simplification, preserving original comparisons with other users
and leaving unrelated boolean producers unchanged. Run semantic/native gates
and a new matched application comparison; do not assume a benefit from shape.
