# Investigation: self emits ordinary calls for proven scalar tail positions

## Status
active

## Problem Description
After native correctness repair, the matched C100 handler comparison is
33,639.1 QPS for owned IR/self, 36,390.2 for LLVM O2 IR/self, 57,475.7 for
LLVM O2, and 81,537.9 for asyncio. The self parser recognizes `tail call` but
discards the marker; no tail jump emitter exists. Calls in final position
still spill/reload the result and return through another frame.

A structural scan finds 198 owned-IR and 104 LLVM-IR scalar final calls in
functions without explicit allocas; their stack-map plans have no root
locations. Counts precede stricter width/protocol/ABI eligibility checks.
Raw scan: /tmp/pcc_tail_candidate_count.json. This is opportunity evidence,
not a throughput result.

## Repro
A scalar wrapper containing a direct call followed immediately by returning
its result emits BL plus a result spill/reload and return epilogue.

## Test [CONFIRMED]
The original direct-call shape test fails on BL (0.14s). Execute argument forwarding and deep tail recursion
in both target modes. Preserve stack arguments, varargs, aggregate ABI,
local-address lifetimes, frame protocols and non-tail return behavior.

## Proposals
- No.1 bounded direct scalar tail lowering with empty root plans [pending]

## No.1 bounded direct scalar tail lowering with empty root plans
### Code Change
Plan only with target optimization enabled, for nonvariadic functions without
allocas, frame protocols or LLVM intrinsics, and without root locations,
managed reloads or exceptional successors in their stack-map plans. Require
a final direct nonvariadic ordinary call and immediate matching return;
restrict arguments to at most eight i32/i64/pointer registers and the return
to i32/i64/pointer/void. Reuse argument lowering, restore the frame and its
return-address state, then branch directly; suppress the redundant return.
Retain safepoint metadata validation. Do not infer eligibility from assembly
shape or enable a fallback when a checked ABI premise fails.

### pending
Prove semantics and native transfer, then same-source repeated timing.

## Reference
[LLVM call instruction contract](https://llvm.org/docs/LangRef.html#call-instruction)
defines tail as a hint and prohibits callee access to caller-local allocas
(except separately specified byval handling). The proposed generic final-call
optimization uses a stricter no-alloca/finite-ABI boundary and preserves pcc's
additional GC invariants. It does not implement LLVM musttail or full tail ABI.

## Update — metadata boundary and execution

The first candidate emitted the complete tail transfer as the call's machine
code, leaving the existing post-call metadata anchor at function end. The
unchanged final stack-map validator rejected this out-of-range position. The
call now emits argument preparation, its empty metadata anchor remains inside
the frame, and the return terminator restores the frame and branches. No
verifier rule was weakened.

49 focused tests pass, including 100,000 recursive tail steps, eight-register
argument forwarding, normal-mode controls and noneligible stack/vararg/local-
address/non-tail cases. Only ordinary flags-zero calls qualify; exception
polls, continuations and loop safepoints keep their existing lowering. Broader
regression, native emission and throughput remain pending.

## Update — ordinary return-PC records do not describe sibling transfers

Broader coverage found zero-argument calls have no argument instructions, so
entry and the retained call anchor coalesced. Keeping an ordinary post-call
record for a transfer that never returns to this frame is the wrong model.
After proving eligibility with complete root plans, rebuild only affected
plans, omitting ordinary return-PC records for the planned sibling transfers.
Entry and all other records remain, and the existing strict final-PC verifier
is unchanged. Reset target plans before every emission so reused modules do
not carry tail decisions into optimization-off output. Old temporary packed
plans are explicitly closed after replacement.

The revised broader backend/precise-map/codec packet passes404 tests in7.47s.
Zero-argument and reused-module controls are added next. Target-specific tail
IDs are not serialized as an LLVM IR promise. No measured throughput gain yet.
