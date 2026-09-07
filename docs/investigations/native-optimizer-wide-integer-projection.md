# Investigation: native optimizer masks truncate in the machine-int projection

## Status
active

## Problem Description
After native optimizer lifetime fixes, the real py_gen result is structurally
valid but changes four -1 return constants into 0. The shared constant folder
computes (1 << width) - 1 using the legacy pcc.* machine-int projection. Its
intermediate mask needs more than the target width; raw 64-bit arithmetic is
not a valid implementation of that Python arbitrary-precision calculation.

Predecessor: native-re-sub-owned-result-raw-scaffold.md.

## Repro
Extend test_compiled_simplifier_preserves_each_function with i64 sub 0,1,
i128 add 18446744073709551615,1, and signed overflow. Before the fix it fails
in 16.01 s: the first two results are 0 and 1 instead of -1 and
18446744073709551616. Both malformed-value outputs can still pass an IR
structural verifier, so verification alone cannot prove constant-fold semantics.

## Test [CONFIRMED]
The extended native regression observes both incorrect constants. A separate
probe using `one: object = 1` and object-returning int parsing reproduces
CPython's full 63/64/128-bit masks and decimal parsing under the raw scaffold.

## Proposals
- No.1 use the existing integer object projection for arbitrary-width data
  while retaining bounded integer widths/counters [pending]

## No.1 preserve wide values through native calls
### Code Change
The owned integer_fold_contract uses object-projected integer arguments and
results for bit patterns and intermediates; power_of_two explicitly starts
from an object-projected one. Widths remain integer control values. The scalar
consumers parse integer data through object-returning helpers and delegate
signed/unsigned normalization to that shared contract. Scale arithmetic also
preserves wide payloads. No namespace exception or external arithmetic owner
is introduced.

### pending
The extended native regression and existing scalar/constant-fold semantic
packet pass 190 tests (24.14 s). Exact real-runtime output comparison and
larger-width/operation coverage are the next gates. No commit/push.
