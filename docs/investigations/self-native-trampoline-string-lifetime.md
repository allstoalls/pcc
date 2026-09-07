# Investigation: native trampoline threading corrupts retained branch strings

## Status
active

## Problem Description
Native self target-on emission differs from host on LLVM O2 py_obj IR. Three
branch strings become exact copies of later valid conditional branches, some
jumping into another function. The old memory-register emitter reproduces it;
it is not introduced by scalar ALU selection. Sized-bytes repair fixed the
separate py_list vector-constant mismatch, but does not fix these branches.

## Repro
pcc/backend/owned_target_pass_driver.py replays the actual threading and
fallthrough helpers on retained input. /tmp/pcc_target_pass_trace_20260907
contains input.s, host/native per-stage snapshots and reduced.s. Delta
debugging reduces 18,236 lines to five in 44 calls (<1s). Native threading turns
an owned `  b L_target` line into just `L_target`; later real-module allocations
reuse it for unrelated b.ne lines. The mismatch is already present immediately
after threading; subsequent pass snapshots remain unchanged.

## Test [CONFIRMED]
Both real-module and five-line native/host differential failures are observed.
A 16-function synthetic trampoline input does NOT reproduce the failure.
The first driver run had an empty-edges file encoded as a newline, triggering
an unrelated index error; an empty file fixes that harness issue. No compiler
ownership change has been made from the current hypothesis.

## Proposals
- No.1 locate the emitted ownership imbalance before changing it [pending]

## No.1 locate the emitted ownership imbalance before changing it
### Code Change
None yet. Capture actual replay-driver compilation IR, then trace branch
creation, list retention, return transfer and later temporary cleanup.

### pending
Exact duplicate strings and opcode changes favor whole-string lifetime
corruption over a wrong target map. Do not rewrite the branch algorithm or
mark classes/strings immortal as a workaround. Existing ownership fixes in
borrowed-local-owned-rebind-consumes-source.md,
native-re-sub-owned-result-raw-scaffold.md and
pcc1-owned-ifexpr-local-transfer.md are already present; tuple ownership
history was read end-to-end and its denied runtime guesses remain denied.

## Update 2026-09-07 — conditional local ownership proven at return

LLDB watches the fresh retargeted string at RC1. A conditional breakpoint
before its fatal release identifies the real LR as _thread_trampoline_branches
+7224; earlier leaf unwinding omitted that frame (main's PC was its return
address, not proof that threading had returned). Logs are watch-detail.log
and release-caller.log under /tmp/pcc_target_pass_trace_v2_20260907.

Actual linked IR exposes a missing retain in _resolve_trampoline_target:
current initially borrows target and its runtime owned flag is false. Only
loop iterations assign an owned mapping value. On the zero-iteration path,
return_lowering treats static membership in _owned_local_names as proof of an
owner and returns the borrow. The caller marks target and resolved as two
owned locals for one reference, then later rebindings can free/reuse storage.
IR at compiler-ir/self_backend_input_1.ll:60013,60029,60058,60176–60179 shows
this path. Error-cleanup concat releases are NOT normal-path double releases.

The ordinary Python regression tests/python/test_conditionally_owned_return.py
also fails before the fix: after returning a borrowed 200K-character string
through a zero-iteration resolver and replacing the result, reading the
original crashes (return -11,1.85s). No target-helper rewrite is needed.

No.2 return according to the physical local's runtime ownership flag [pending]:
return_lowering now retains only the borrowed flag path and transfers an
existing owner on the owned path, joining them as an explicitly owned SSA
value. The flag lookup is tied to the actual alloca; stale same-name metadata
cannot prove ownership. Unsafe and CPython pointer lanes retain their existing
contracts. Validate ordinary zero/nonzero-loop returns across all5 GC, the
native five-line replay, real py_obj emission and existing return-owner gates.
