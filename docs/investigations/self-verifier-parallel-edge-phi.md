# Investigation: self verifier rejects valid PHIs with parallel CFG edges

## Status
active

## Problem Description
The same-IR self/LLVM O2 runtime codegen comparison stops before emission:
LLVM O2's py_obj `_gc_graph_leaf_tag` switches nine cases to one return block.
Its return PHI has nine equal incoming values from entry. The self verifier
incorrectly treats any repeated predecessor as malformed. This is a compiler
IR contract gap, not evidence of runtime performance.

## Repro
`tests/c/test_self_backend_verifier.py -k parallel_edge` reduces the real shape
to two switch edges and equal PHI inputs. It fails with
`self IR verifier [phi-predecessors] ... repeats predecessor 'entry'`.
The real diagnostic log is
`/tmp/pcc_ir_codegen_matrix_20260907/llvm-ir-self-codegen-py_obj.log`.

## Test [CONFIRMED]
The first reduced acceptance case fails before the correction. LLVM's
[verifier contract](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/llvm/lib/IR/Verifier.cpp)
requires one incoming per predecessor edge and equal incoming values for
repeated predecessor blocks. Existing missing-predecessor, dominance and
type checks must remain enforced. Add positive switch/conditional-branch
cases and negative multiplicity/differing-value cases; execute emitted code.

## Proposals
- No.1 compare edge multiplicity and equal incoming values [pending]

## No.1 compare edge multiplicity and equal incoming values
### Code Change
Retain the unique predecessor graph for dominance. At PHI validation, derive
edge counts from the indexed successor records, require exact multiplicity
and equal values for repeated edges, and retain definition/type/dominance
checks. Do not discard duplicate entries or loosen malformed IR acceptance.

### CONFIRMED
The verifier now checks multiplicity and equal values while preserving the
unique predecessor graph used by dominance. The parser must also retain a
constant conditional branch when both targets are equal; collapsing it early
otherwise removes one edge without updating the PHI. Other established
constant-branch folding remains unchanged. The extended verifier/parser/
indexed packet passes 39 tests (0.72 s).

All five exact LLVM-O2 runtime modules now emit and link through self codegen.
The full matrix validates 700,000 responses; a separate 300,000-request native
profile also completes. These executions validate the actual parallel-switch
shape in py_obj. Native pcc1 qualification of the revised verifier is pending.
