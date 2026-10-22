# Runtime-effect category / resource contract plan

## Status

Draft. Do not select this plan while a P0 selected task is in progress unless the
user explicitly reselects the task.

This document is the local bridge between textbook category theory and PCC
engineering. Codex may already know general category theory; this plan tells it
which categorical ideas are relevant to PCC, which code paths instantiate them,
and which tests prove a claim.

## Purpose

Make PCC runtime composition explicit enough for Codex and maintainers to reason
about GC roots, barriers, continuations, scheduler queues, virtual threads, and
GPU host/device boundaries without scattering ad-hoc rules across runtime,
codegen, and tests.

This is the engineering form of a runtime-effect category:

- objects/resources: heap object, heap slot, root slot, frame root,
  continuation root, scheduler queue, virtual thread, host buffer, device buffer,
  pinned region, nogc region;
- morphisms/arrows: runtime ABI calls and selected compiler-lowered operations;
- composition: basic block / function path / suspension-resume path;
- tensor: independent runtime resources such as multiple virtual threads or
  buffers;
- commuting diagrams: relocation/root-rewrite/resume equivalence.

The goal is not to make PCC sound scholarly. The goal is to turn categorical
vocabulary into source-level and test-level obligations that catch real runtime
regressions.

## What Codex should not need from textbooks

Do not paste long explanations of category theory into this prompt or plan.
Assume Codex can recall generic definitions of category, functor, natural
transformation, monoidal category, Kleisli category, and string diagram.

What Codex lacks is the PCC-specific interpretation:

```text
category object        -> runtime resource state or interface
morphism               -> runtime ABI call / lowered operation / checker event
composition            -> path through generated code or runtime transition
identity               -> no-op path preserving resources/effects
tensor                 -> independent parallel resources, not shared slots
functor                -> lowering map from AST/typed IR to runtime-effect arrows
natural transformation -> optimization/rewrite that preserves effect/resource laws
commuting diagram      -> before/after runtime path that has same observable roots/barriers/results
```

Add source references only as routed references. Do not make any book or paper a
startup requirement unless a selected slice explicitly needs it.

## Source map: routed references, not mandatory startup

Use these references as concept anchors when a selected slice crosses the
matching boundary. The local law/gate below is authoritative for PCC work; the
book/paper is background only.

| PCC boundary | Category concept | Routed reference | Why it helps Codex |
|---|---|---|---|
| Component interfaces and resource feasibility | compositional systems, resource theories | `ACT4E-public.pdf`; `An Invitation to Applied Category Theory Seven Sketches in Compositionality...pdf` | Explains why a composed system can be summarized by a smaller interface instead of re-reading all internals. |
| Runtime effects and handlers | Kleisli/category of effectful computations, graded/decorated arrows | `Barr-Wells-ctcs.pdf`; `Category Theory Using Haskell...2025.pdf` | Maps effect annotations to actual compiler/runtime operations. |
| Barriers, roots, and ownership | functors preserving structure; naturality of rewrites | `Category Theory (Steve Awodey).pdf`; `Categories for the Working Mathematician...pdf` | Useful when an optimization moves code across roots/barriers and must preserve the diagram. |
| Scheduler, message passing, virtual threads | actegories, linear/resource-sensitive message passing | `Category-message-passing-2503.19305v1.pdf`; `CaMPL Type Inference.pdf` | Useful for thinking about processes as resources and scheduler queues as visible roots. |
| GPU host/device split and diagrammatic IR | symmetric monoidal categories, string diagrams | `Picturing Quantum Processes...pdf`; `Seven Sketches...pdf` | Useful for host/device transfer, parallel kernel composition, and rewrite laws. |
| Imports/packages/local-to-global consistency | sheaves/sites/local data glued to global behavior | `Sheaves_in_Geometry_and_Logic__MacLane_Moerdijk.pdf` | Deferred; useful only if package/import semantics become a categorical route. |
| Type/proof obligations | categorical logic/type theory | `Categorical Logic.pdf`; `Manin, Logic for Mathematicians.pdf`; `open-logic-complete.pdf` | Deferred; useful only for formal proof or type-system route work. |

Do not copy pages from these references into repository docs. Write the PCC law,
code path, and gate.

## Local categorical dictionary for PCC

### Category: `PCCRuntimeEffect`

Objects are finite summaries of runtime-visible resources:

```text
HeapObject
HeapSlot
RootSlot
FrameRoot
ContinuationRoot
Continuation
SchedulerQueue
VirtualThread
HostBuffer
DeviceBuffer
PinnedRegion
NoGCRegion
```

Morphisms are runtime ABI calls or lowered operations, decorated with finite
effects:

```text
pcc_gc_alloc                 : 1 -> HeapObject           [Alloc, Safepoint]
pcc_gc_load_ptr              : HeapObject x HeapSlot -> HeapObject [ReadBarrier]
pcc_gc_store_ptr             : HeapObject x HeapSlot -> HeapSlot   [WriteBarrier, Retain, Release]
pcc_gc_frame_enter           : RootSlot -> FrameRoot     [RootEnter]
pcc_gc_frame_leave           : FrameRoot -> 1            [RootLeave]
py_continuation_new_typed    : RootSlot -> Continuation x ContinuationRoot
py_virtual_thread_start      : VirtualThread -> SchedulerQueue
py_virtual_thread_run_once   : SchedulerQueue -> VirtualThread
```

The code representation is `pcc/diagnostics/contracts/runtime_effects.py`:

```text
RuntimeResource
RuntimeEffect
RuntimeArrow
RUNTIME_ABI_ARROWS
check_runtime_path(...)
```

### Functor: frontend/codegen to runtime-effect arrows

The future codegen checker should map selected frontend/lowering events to this
category:

```text
Python AST / typed IR / codegen event
    -> runtime ABI call sequence
    -> RuntimeArrow sequence
    -> effect/resource summary
```

Examples:

```text
local object assignment
    -> pcc_gc_frame_enter ; ... ; pcc_gc_frame_leave

self.field = value
    -> pcc_gc_store_ptr

pcc.virtual_thread.sleep_current(ms)
    -> py_virtual_thread_current ; py_virtual_thread_sleep ; generator yield

GPU kernel launch with host array
    -> host_to_device ; gpu_launch ; optional device_to_host/sync
```

### Natural transformation: optimization or lowering rewrite

An optimization is valid only if the diagram commutes at the runtime-effect
level:

```text
old lowering path  ---> runtime effects/resources
      | rewrite                 | same observable contract
new lowering path  ---> runtime effects/resources
```

For PCC, "same observable contract" means at least:

- same Python result/exception behavior;
- no missing read/write barrier;
- same or stronger root coverage at collect/safepoint/suspension boundaries;
- no unbalanced pin/root/continuation root;
- no hidden host/device sync or transfer claim;
- no accidental libpython/CPython-compat boundary in pcc-native mode.

### Monoidal product: independent resources

Use `f tensor g` only when the resources are disjoint or explicitly synchronized.

Good examples:

```text
vthread A ready queue entry  tensor  vthread B ready queue entry
GPU buffer A kernel          tensor  GPU buffer B kernel
```

Bad examples without extra structure:

```text
store to the same HeapSlot in two branches treated as independent
popping the same SchedulerQueue from two carriers without queue synchronization
moving a pcc_gc_store_ptr across pcc_gc_collect as if they commute
```

## PCC runtime-effect laws

Each law has a category-theory name, an engineering meaning, and a Codex action.
When in doubt, the engineering law wins.

### Law R1: slot write factorization

Categorical view:

```text
HeapObject x HeapSlot x HeapObject -> HeapSlot
```

must factor through the collector-visible store arrow unless the path is a
constructor/collector-internal exception.

Engineering rule:

Every heap slot mutation visible to a tracing/moving collector must use
`pcc_gc_store_ptr` or `pcc_gc_store_root`, or be documented as one of:

```text
constructor initialization before publication
collector-internal under STW/clear/relocation repair
raw memory copy that is immediately followed by explicit slot registration/repair
```

Codex action:

When touching `pcc/runtime/src/*.c`, `pcc/runtime/py/*.py`, or codegen that
emits stores, search for raw slot stores and classify them. Do not replace all
raw stores blindly; constructors and collector internals may be correct raw
paths.

Focused gate idea:

```text
test_runtime_effect_category.py::test_gc_slot_operations_encode_barrier_contracts
future raw-slot audit test for selected files
```

### Law R2: moving-GC read factorization

Categorical view:

```text
HeapObject x HeapSlot -> HeapObject
```

must factor through a read barrier when the value may be forwarded or relocated.

Engineering rule:

A PyObject pointer read from a heap slot that will be used by mutator code must
come through `pcc_gc_load_ptr` or `pcc_gc_load_borrowed_ptr`, unless it is a
collector-internal scan with explicit forwarding/repair semantics.

Codex action:

If a backend #3/#4 bug looks like "old address observed" or "attribute missing
after relocation", audit raw slot reads before changing object layout.

### Law R3: frame-root bracket law

Categorical view:

```text
RootSlot -> FrameRoot -> 1
```

must be balanced on all exits.

Engineering rule:

Every `pcc_gc_frame_enter` path must have a matching `pcc_gc_frame_leave` on
normal exits, error exits, early returns, and paths that move ownership to a
continuation.

Codex action:

When changing `ownership_lowering.py`, return lowering, exception lowering, or
owned-local cleanup, add a focused root-bracketing regression and run the
bootstrap/fallback gates required by `docs/goal/goal-prompt.md`.

### Law R4: continuation suspend/resume law

Categorical view:

```text
ActiveFrameRoots -> ContinuationRoot -> ActiveFrameRoots
```

should preserve all live PyObject roots up to relocation rewriting.

Engineering rule:

Suspension must copy/register every live object slot; resume/mount must restore
or rewrite those slots so backend #4 cannot observe stale addresses.

Codex action:

When modifying continuations or virtual-thread generator lowering, verify:

```text
pcc_gc_register_continuation_root
pcc_gc_unregister_continuation_root
pcc_gc_rewrite_continuation_roots
py_continuation_new_typed
py_virtual_thread_resume_generator
```

and route through the coroutine/scheduler GC root tests.

### Law R5: scheduler visibility law

Categorical view:

```text
VirtualThread -> SchedulerQueue -> VirtualThread
```

is not a private control-flow edge; the queue is a GC-visible resource.

Engineering rule:

A queued, sleeping, blocked, or parked virtual thread must remain reachable and
relocation-safe across all five GC backends.

Codex action:

When changing virtual-thread queues or carrier policy, check enqueue/dequeue
effects and run the scheduler-root production gate before claiming completion.

### Law R6: pin boundary law

Categorical view:

```text
HeapObject -> PinnedRegion -> HeapObject
```

must be balanced and observable.

Engineering rule:

Native/foreign/blocking regions that cannot safely expose movable roots must
enter a pin/diagnostic boundary and leave it on every exit.

Codex action:

When adding foreign calls, blocking file/socket operations, or GPU external
handles, ask whether this path pins, roots, copies, or rejects in strict native
mode.

### Law R7: safepoint/collection interference law

Categorical view:

`collect` does not commute freely with allocation, store, root leave, or suspend.

Engineering rule:

Do not move or delete safepoints, allocation calls, root leaves, or stores as a
performance cleanup unless the runtime-effect sequence is proven equivalent.

Codex action:

Any optimization that reorders runtime calls must produce a before/after
effect path and explicitly state which arrows commute.

### Law R8: GPU host/device domain law

Categorical view:

```text
HostBuffer -> DeviceBuffer -> DeviceBuffer -> HostBuffer
```

must make transfers and synchronization explicit.

Engineering rule:

A device kernel consumes device resources. Host buffers require explicit transfer
or a documented unified-memory/pinned-host mode. Metal artifact creation is not
whole-program GPU execution and not MLX support.

Codex action:

When touching GPU lowering, classify every kernel parameter as host/device/pinned
host/unified and avoid hidden sync claims.

### Law R9: mode-labeled functor law

Categorical view:

Different execution modes are different functors from source programs to runtime
behavior.

Engineering rule:

Do not conflate:

```text
host pcc
pcc1
pcc2/pcc3 fixed point
cpython-compat/libpython
pcc-native/no-libpython
LLVM backend
self backend
GPU Metal host/device split
```

Codex action:

Every runtime-effect status update must include the claim boundary and bootstrap
line required by `docs/goal/goal-prompt.md`.

## How Codex should use this during a patch

### Before editing runtime/codegen

Write or update `docs/current-goal-state.md` with:

```text
runtime_effect_contract: taxonomy|checker|runtime-change
selected_law: R1|R2|R3|...
claim_boundary: <what this proves and what it does not prove>
next_gate: <focused gate>
```

### During source discovery

Map the touched operation to one of these buckets:

```text
ABI arrow classification
codegen event path
raw runtime slot read/write audit
root bracket audit
continuation/scheduler route
GPU host/device route
```

### After a focused test changes state

Update `docs/current-goal-state.md` immediately. Do not keep debugging while the
state file describes the previous failure shape.

### Before claiming completion

Report:

```text
runtime_effect_contract: taxonomy|checker|runtime-change
selected_laws: <R-list>
focused_gate: <exact command and result>
bootstrap: passed|failed|not run (<exact reason>)
gc_matrix: not touched|backend-0|all-five|not run (<reason>)
claim_boundary: <what was proved and not proved>
```

## Phase 1: taxonomy only

Add `pcc/diagnostics/contracts/runtime_effects.py`.

Represent:

```text
RuntimeResource
RuntimeEffect
RuntimeArrow
RUNTIME_ABI_ARROWS
```

Classify correctness-critical ABI calls first:

```text
alloc / retain / release
load_ptr / load_borrowed_ptr
store_ptr / store_root
frame_enter / frame_leave
safepoint / collect
pin / unpin
continuation root register/unregister/rewrite
scheduler queue push/pop
virtual-thread park/unpark/sleep/block/run
GPU host/device resource placeholders
```

No runtime behavior changes.

## Phase 2: ABI coverage checker

Compare selected entries in
`pcc.frontends.python.codegen.runtime_abi.RUNTIME_SIGNATURES` with
`RUNTIME_ABI_ARROWS`.

The first coverage gate should not require every telemetry helper. It should
require only correctness-critical symbols.

## Phase 3: codegen event recorder

Add optional codegen-side runtime-effect event recording for existing lowering
calls. Do not change emitted IR in this phase.

Primary hook files:

```text
pcc/frontends/python/codegen/ownership_lowering.py
pcc/frontends/python/codegen/native_virtual_thread.py
```

The recorder should observe calls such as:

```text
pcc_gc_retain / pcc_gc_release
pcc_gc_frame_enter / pcc_gc_frame_leave
pcc_gc_store_root
py_virtual_thread_* lowering
```

## Phase 4: checker gates

Add focused tests for:

```text
frame_enter/frame_leave bracketing
pin/unpin bracketing
store_ptr requires write barrier
load_ptr represents read barrier on moving collectors
suspend/resume paths expose continuation roots
GPU kernel arguments cannot be host buffers without explicit transfer
```

## Phase 5: runtime audit

Only after the checker exists, audit raw slot writes and raw slot reads in:

```text
pcc/runtime/src/*.c
pcc/runtime/py/*.py
```

Classify each raw write/read as:

```text
OK: uses pcc_gc_store_ptr / pcc_gc_store_root
OK: collector-internal under STW / clear / relocation repair
OK: constructor initialization before object publication
SUSPECT: heap slot mutation without barrier
SUSPECT: moving-GC-visible slot read without read barrier
```

## Gates

Taxonomy-only:

```bash
env -u LC_ALL uv run pytest tests/python/test_runtime_effect_category.py -q -n0
```

Codegen hook touched: add the current bootstrap/fallback gates from
`docs/goal/goal-prompt.md` §0.9 and `AGENTS.md` before claiming completion.

Runtime/GC/rooting touched: add the GC production contract plus the all-five
bootstrap matrix or the current dedicated all-five gate named by
`docs/goal/goal-prompt.md` / `docs/current-goal-state.md`.

## Claim boundary

A runtime-effect checker pass proves that the selected runtime operations were
classified or the selected generated path obeyed the modeled contract. It does
not prove collector performance, full Python compatibility, full virtual-thread
production behavior, MLX support, or whole-program GPU execution.
