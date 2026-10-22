# Class-init outline candidate: reviewed V4 successor

This is an unapplied source/test candidate. Independent source review is
complete for these bytes. All PCC tests, compiler execution, native behavior,
mechanism and performance measurements remain UNRUN. Source clearance permits
a proposed small structural gate, not production activation or native claims.

## Bundle and exact recovery

The public bundle contains only successor.patch, README.md and manifest.json.
The successor is a cumulative delta from the V1 recovery postimages preserved
at commit `8ee714b914a1a70cd8e6950389f7fb38fbe3c370`, under
`docs/research/unapplied-candidates/class-init-outlining/candidate-v1/`.
Intermediate V2/V3 authoring drafts are not required.

Start from production commit `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`
and verify the manifest's two production preimage hashes and absence of the
new test file. In an ordinary isolated source copy, apply V1 production.patch,
V1 tests.patch, then this successor.patch. Verify all three final postimages
and every unchanged baseline file. No runtime or executable is bundled.

The bound source container has inventory
`ea395e18a173ec4172c8badd68bbccb53a66390e4c130b8a78f6702fbc177635`;
its pcc subtree `d234174fbac80575ea822f6100f65fe914dd4498` matches the
production commit. Its older unrelated tests/docs are not relabeled as that
complete checkout. No OSError, x86 bridge or ARM worklist changes are included.

## Scope and reviewed corrections

The candidate changes two production files and adds one host test file:

- generation_lowering.py records original direct module-class positions before
  closure hoisting.
- class_gen.py emits one internal noinline i32 status helper for each admitted
  closed-literal class. Both external init/top entrypoints remain; main keeps
  its ABI. Every actual call still constructs fresh classes, callables and
  signature/default containers with the original operations and literal
  singleton identities. There is no runtime object cache or skipped definition.
- tests/python/test_class_init_outlining.py contains 27 intended parametrized
  host structural nodes. They have not been collected or executed.

The original _emit_class_init body and local-class entrypoint are byte-identical
to the baseline method source. Helpers use independent physical root and
cleanup state, retain module traceback attribution, add no Python recursion
activation, and return failure status without relying solely on PCC TLS.
Broader dynamic/default/capture/metaclass/decorator and special-layout classes
retain their original inline route. No eligibility expansion is included.

V1's original decorator exclusion could be lost when declaration stripped a
no-op decorator. V4 records that exclusion before normalization and tests
@final. Generation failures now dump diagnostic state before restoring the
caller, matching existing function/method lowering. The test corrections use
the actual statement-dispatch owner, tolerate the outer diagnostic after
current_function becomes None, remove the bool-based trace environment, assert
effective threading mode, and require eq-1 status branches to the real error
exit. Production bytes are unchanged from the independently cleared V2
production delta; later repairs affect only tests. The V1 snapshot remains
unchanged and records its original review-pending limitation.

## Proposed bounded first gate

After separate coordinator admission, use the unchanged sole-process guard and
shared lock: 300 seconds, hard AS4GiB/NPROC0, 4GiB free reserve, a fresh result
directory and complete before/after source seals. The intended command is:

`python -m pytest -x -n0 -vv --tb=short tests/python/test_class_init_outlining.py`

The file uses real owned parsing, inference, L1 generation, IR verification and
default owned passes, plus both inline modes. It compares against the existing
inline lowering through a scoped test hook. The 27 nodes cover retained public
ABI, shared body/call counts, status edges, selected root/constructor operations,
source order, inferred annotations, 12 excluded shapes, generation-state
restoration and three injected structural error channels. Host source imports
do not require native compilation, runtime provisioning, ctypes or subprocess
execution. The configured textual route is explicit.

Stop on the first failure and retain its result. This packet does not request
a larger suite, real heavy module, runtime build or native run. Those gates
need separate review/admission after this structural gate passes.

## Remaining qualification boundaries

The reviewed design is saved in commit
`775e5e44038f173b6658a36c5b899477588cb54d`. The real c_ast source census
identified 55 candidate shapes, but actual typed eligibility, IR reduction and
whole-pipeline benefit for this implementation are unmeasured. Counts alone
are not speedup evidence. New function boundaries intentionally change object
layout, PCs and stackmap identities, so byte-identical objects are not the
acceptance contract. Public behavior, complete valid root/relocation metadata,
fresh identities and source-frame/cleanup semantics need actual qualification.

The new ClassLowering field and helper methods have not been exercised by the
native compiler's closed-world layout/call graph. Runtime execution, repeated
embedding entrypoints, exception identity/finalizers, all five GC backends,
pcc1 and full Stage1 remain UNRUN. No production readiness is claimed.
