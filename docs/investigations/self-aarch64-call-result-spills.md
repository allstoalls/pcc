# Investigation: self AArch64 spills block-local integer call results

## Status
active

## Problem Description
The matched LLVM-O2-IR runtime emits about twice as many process instructions
with self codegen as with LLVM codegen. The self allocator rejects all call
results, even an integer used entirely after its producing call and before
another call. The indexed generic-call emitter also writes results directly
to stack parts, bypassing the existing register projection helpers.

## Repro
`tests/c/test_self_backend_aarch64_regalloc.py::test_aarch64_call_barrier_spills_touching_values_but_allows_post_call_locals`
expects the locally consumed integer result to receive a register while the
value live across the call remains spilled.

## Test [CONFIRMED]
The reduced allocation gate fails before editing the allocator. Require actual
call-result execution and preserve crossing-call, argument, pointer, aggregate,
PHI and register-pressure boundaries. A call's result starts after its clobber;
the same exception must not be applied to call operands or later calls.

## Proposals
- No.1 allocate bounded integer call-result intervals [DENIED as a default]

## No.1 allocate bounded integer call-result intervals
### Code Change
Admit only integer results up to 64 bits with the existing block-local
last-use proof. Ignore the producing call as a clobber for that result only.
Use the existing indexed scalar result publication helper. Retain all slots
and do not extend this to cross-block or cross-call allocation.

### DENIED as a default performance change
The initial candidate passed allocation tests but failed real application
initialization with `AttributeError: TaskScope`. The chosen slot helper did
not publish into allocated registers. An external callee returning x0=33 but
clobbering x1=99 reduced the failure; register-aware publication fixed it.
Intrinsic call results were excluded because they have other publication
owners. Corrected candidate passes 22 gates and both runtime application arms.

Seven rotating repetitions per arm (42 runs): owned-IR QPS 29,636.2 control
versus 29,013.3 candidate (-2.1%), with only 0.26% fewer process instructions.
LLVM-O2-IR self codegen was 35,196.0 versus 36,007.7; same-run LLVM reference
57,141.1 and asyncio 83,209.1. This does not justify enabling the change for
the current self pipeline. Source changes to allocation/publication were
withdrawn surgically; retain the external ABI execution regression.

Artifacts: /tmp/pcc_call_result_ab_v2_20260907, timing.json and build-report.json.
Failed first attempt remains /tmp/pcc_call_result_ab_20260907. Candidate patch
is /tmp/pcc_call_result_candidate_with_memset.patch (also includes the earlier,
separate small-memset improvement; do not apply it wholesale). Revisit only
with new evidence of a materially different IR/allocator context.
