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
