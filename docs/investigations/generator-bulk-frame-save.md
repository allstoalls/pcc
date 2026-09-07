# Investigation: batch the compiler's fixed-frame suspension stores

## Status
active

## Problem Description
Continue pcc #188: latest pcc1 handler QPS trails same-run asyncio by 1.79x.
The current application profile has py_list_set on 403 of 2,303 CPU stacks
(17.5% inclusive), mostly from generated suspension frame saves. Removing
that entire owner would have a ceiling near 1.21x; this proposal removes only
its repeated list/shape dispatch, so does not claim to close the whole gap.
Predecessor: vthread-asyncio-throughput-gap.md. Its denied bulk construction
proposal concerns initial allocation; this proposal concerns existing-frame
saves and preserves every slot's ownership write.

## Repro
Compile a generator with several live locals and inspect its suspension path:
_emit_generator_save_frame emits one py_list_set call for every frame slot.
The full gateway uses the same generated path; reference profile and A/B
scripts are retained in pcc-gateway/benchmarks.

## Test [N/A]
New runtime gate will exercise successful GC0 saves and untouched fallback
under GC1–4, with mutable aliases and collection after dropping source owners.
A codegen gate must prove the new bulk call and the old fallback both exist.

## Proposals
- No.1 one GC0 frame-save dispatch with ordinary per-slot barriers [pending]

## No.1 one GC0 frame-save dispatch with ordinary per-slot barriers
### Code Change
The compiler supplies addresses of its already-rooted local slots in a raw
stack array. A runtime helper accepts only GC0 and an exact-size list frame,
checks the frame shape once, and reads each current source slot before storing
through pcc_gc_store_ptr. No value owner is stolen, no collector barrier is
removed, and source locals retain their existing cleanup contract. Other
collectors return unhandled without mutation and use the current generated
py_list_set path. CPython-backed skipped slots retain the old path entirely.
Activation remains opt-in until correctness and application A/B qualification.

### pending
Measure one frozen compiler/runtime with only the bulk-save flag differing.
Check finalizers, mutable aliases, send/throw/close, exception cleanup and actual
GC0–4 execution before accepting any throughput or instruction-count change.

## Update: initial capability gate
The C runtime regression fails at link time with undefined py_gen_frame_save
(0.98 s). This establishes the missing proposed ABI, not a performance verdict.
The test and proposal are checkpointed before implementation.

## Update: runtime and generated execution gates
Both runtime mirrors pass the new alias/fallback test under actual GC0–4
(2 cases, 131.08 s including a new runtime build). Current archive cache:
751e4cc81f5580f07aa2abe0-pcc-py/libpy_runtime_pcc_py.a.

PCC_BULK_GENERATOR_FRAME_SAVE now emits a raw array of local-slot addresses,
one bulk call per suspension, and the unchanged per-slot fallback. The flag
is in frontend cache identity and defaults off. The IR gate failed before
activation, then passed along with existing send/throw/close, finalizer and
field-owner tests (12 cases, 23.53 s). Real TCP under GC0–4, parking finally
and exception context passed (4 cases, 11.77 s). Gateway's native failure/
cancellation/rejected-fork canary passed (4.99 s).

The recommended standalone --python-library closure command rejects both this
source and the frozen pre-change generator_lowering.py because that mode
admits only one source while the package has relative imports. It is not a
new-source codegen failure or a completed bootstrap gate. Fresh pcc1 execution
qualification is still required if the application experiment is accepted.

One frozen compiler and one runtime now compare flag 0/1 over the complete
handler workload, with the previous three optimization flags fixed on.
No performance verdict yet.

## No.1 verdict [DENIED]
The 42-run A/B completed. Zero-wait/C100 control/candidate/asyncio QPS medians
are 48,279.4 / 46,711.8 / 83,278.1. Candidate is 3.25% slower; instructions
per measured request rise 309,692 to 313,035 (+1.08%), and user CPU rises
20.5 to 21.5 us. At 100 ms the three arms are 974.3 / 975.1 / 975.3 QPS.
This does not improve the measured owner. The address-array preparation and
per-slot dispatch inside the helper add work while retaining the original
reference operations. Retain the frozen source/report as experimental evidence,
remove compiler activation, and do not present this as a speed improvement.
Report: gateway benchmarks/results/2026-09-07-bulk-frame-save-ab.json.

## No.2 transfer owned local references into frame slots [pending]
### Code Change
Address the reference protocol rather than batch its existing work. An
ownership-aware slot setter consumes a local's owned reference into the frame
under GC0 and clears its cleanup flag. Borrowed locals and GC1–4 keep the
existing retaining store and cleanup. A reference-consuming GC0 heap barrier
must preserve store logging, old-value release order and self-assignment
semantics. No source values are discarded; frames retain them across suspend.
No auxiliary per-yield address array is needed: each existing setter call
receives the corresponding local-root and ownership-flag addresses directly.

### pending
Gate consumed and borrowed references, same-value stores, overwritten-value
finalizers and all collectors before a new A/B. This targets the paired retain
on frame save and release on local cleanup, not the denied list-check batching.
