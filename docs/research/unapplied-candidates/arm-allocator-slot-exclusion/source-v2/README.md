# AArch64 allocator slot exclusions: test-only V2 successor

Status: unapplied test-only successor; independent review pending. The exact
V2 host gate, mechanism measurement, timing, native and CI remain UNRUN.
Production bytes are identical to V1. Preservation is not qualification. The
separate numeric-data-batching production revision is running its ordinary CI.

## Source and recovery

Base production commit: `bf1bf82d8242a60a85a06322ceee97fab5346cc0`.
Its complete `pcc` Git tree is
`930263bf204e578796c72dab5d4aa31fd7ab27ae` and the qualified source inventory
used for these preimages is
`7430a30766c11a59e7185758b725b289388ae131498ce6a8a568bdf10ff773ba`.
That retained inventory is an older full snapshot with the exact production
`pcc` subtree, not a claim that every documentation/test file equals the current
remote root. This patch changes one existing production file and adds one new
host test. It does not include any earlier experimental allocator, liveness,
record-cache, label-suffix or x86-bridge candidate.

The public packet contains `candidate.patch`, `manifest.json` and this README.
Verify each listed preimage before applying the patch to an ordinary independent
copy; verify both postimages afterward. Do not apply it to a sealed input tree.
The patch is the complete recovery artifact; preimage/postimage directories are
local authoring conveniences and are not bundled public files.

## Preserved V1 failure and precise correction

V1 is preserved at `c3263977301823c2198fe22c2c1c09aa14519885` under
`docs/research/unapplied-candidates/arm-allocator-slot-exclusion/source-v1/`.
Its sole admitted host gate reported **42 PASS / 1 FAIL / 1 UNRUN** from 44
collected cases, with zero audit denials and clean termination. The exact
payload result SHA-256 is `68287e30475e5c7eca16d24508b0645d762de8817cdf7671436fbeaf0474c137`. That failed gate and source are retained.
V2 is a new artifact, not a correction to that historical outcome.

The first inline-error fixture stopped at its setup assertion
`kernel.value_id("base") >= 0`, before either tested facts invocation. Source
classification shows the unchanged owned builder deliberately assigns named
results a function-wide suffix: `IRBuilder._next` in `pcc/ir/ir.py` lines
2185–2205 makes `base.<serial>`. `DirectIndexedFunctionBuilder._dest_value_id`
in `pcc/ir/direct_indexed_kernel.py` lines 289–303 interns the Value's final
`_direct_name` and records its `_direct_value_id`. Querying the hint `base`
therefore does not identify that produced value.

The only V1-to-V2 source delta replaces the literal lookup with
`kernel.value_id(base._direct_name)`, retains the nonnegative-ID assertion,
and adds equality with `base._direct_value_id`. The same two inline-edge
cases must still exercise the original full facts, filtered facts, exact
edge-call count, first error identity and retained intervals. No assertion was
removed; no production file, fixture IR, mode, selected count or resource cap
changed. The earlier 42 passing cases do not imply the final two cases passed.

Naming-contract source SHA-256 identities:
- `pcc/ir/ir.py`: `821d3c0232a8daeae7c63d927586b80f6775f6aa03c3147aa7e6b462707ed39b`
- `pcc/ir/direct_indexed_kernel.py`: `c522df0dacc0afa61f6a38aad957b8deb918bdd21a78d36b543c4c5dff33883c`

The complete patch below is against the original bf1bf82d base, not against an
already-applied V1 tree. The preserved V1-to-V2 relation is test-only.

## Evidence and narrow proposed change

The preserved phase attribution for production `d2334624` is in
`docs/research/unapplied-candidates/indexed-phase-timing/ci-readback/` at commit
`50d6a0a0ba6a07f88356105be2c17542b514b4e8`. It reports completed-worker
`function_setup` totals of 391.542 seconds on Mac ARM and 883.153 seconds on
Linux ARM, respectively 12.89% and 13.63% of those workers' codegen totals.
These are overlapping-worker sums, not Stage1 wall time. Setup includes reload
destination collection, register allocation and prologue construction. It does
not isolate `_function_level_facts` or its backward walks. No observed fraction
of that time is assigned to this candidate.

There is one `_function_level_facts` call per function allocator invocation.
The repeated work is the helper's separate predecessor traversal for every used
SSA value. All three interval consumers reject a missing/negative spill slot or
an active reload offset before consulting solved liveness. A value with such a
slot therefore cannot contribute an allocation interval. Stackmap reload slots
must stay spilled because the reload updates the slot after a relocating GC.

The candidate leaves the complete instruction/use scan, PHI-edge scan, CFG and
inline-error-edge scan, predecessor construction, barrier discovery and later
candidate selection in their original order. After seeding the original pending
list, it omits only the backward walk of a value proven ineligible by frozen
slot facts. Values with no cross-block pending work do not call the new predicate.
The textual `last_use` entry and all keys remain. Each walk has a value-specific
visited stamp, so omitting one value cannot shorten another value's interval.
The existing `None` call form still returns full unfiltered facts; the allocator
passes its already-fixed reload policy explicitly.

A bounds-valid value with no slot, a negative slot offset, or an offset in the
active reload set supplies negative evidence. Unknown IDs, incomplete scalar
record tables, out-of-range slot IDs and non-integer fields/policies keep the
original walk. The predicate does not read types, project instruction data,
mutate slot records, change the root plan, or release an arena. No exception is
caught or converted. The source relies on the existing `CompilerIntArena`
length/storage invariant; it does not claim recovery from corrupted raw memory
or externally inconsistent private arena internals.

The reload membership set is local to this call and bounded by the supplied
reload-offset list. It avoids adding a values-times-reloads linear scan. No
module/global cache is added. The new scalar reads avoid constructing a
`CompilerInt4` merely to inspect its slot field. The existing register writes
only modify a separate register column; slot assignments are complete before
stackmap planning and allocator entry.

## Semantic boundaries and explicit risks

- Do not move the existing instruction eligibility branch earlier:
  `instruction_data` and `intern_type` own diagnostic projections and type
  interning whose encounter order must remain unchanged.
- Do not introduce a universal integer-width or recorded-type filter. Ordinary
  instruction results admit wider integer types than argument/PHI/call paths,
  and ICMP has its own recorded-result rule. Such values can affect pressure.
- PHIs, PHI inputs, arguments, calls, backedges, inline error successors, MADD
  operand extensions, alias groups and frame decisions keep their existing
  rules. All managed-root and ownership plans are unchanged inputs.
- Partial liveness is safe only inside the slot-rejecting allocator contract.
  The full-facts helper form remains available; do not reuse the filtered facts
  for GC roots, stackmaps or other consumers without an independent proof.
- This adds a private helper, a typed optional parameter, plain-integer guards,
  and a call-local set. Their actual native closed-world lowering, exception
  behavior and pcc1 execution have not been qualified.
- Allocation-failure timing may differ because a bounded temporary set is new.
  Actual skipped walk count, predecessor visits, coverage and overhead remain
  unknown. Removing all setup would only bound the whole setup share above;
  this candidate does not remove all setup and has no claimed speedup.

## Focused proof prepared, not executed

`tests/python/test_aarch64_allocator_slot_exclusion.py` has 44 statically counted
host cases. It uses real scalar arenas and small ordinary prepared/indexed
backend inputs, including both ARM triples; it launches no subprocess, compiler
executable, runtime build or native program. Planned coverage:

- 17 strict negative-evidence/unknown-field cases with no arena mutation;
- two loop/PHI cases comparing every retained interval and all common facts,
  preserving helper lifetime and the original full-facts result;
- four incomplete-table/unknown-policy/invalid-slot fallbacks;
- 18 complete packed-object and allocator-state comparisons: three fixtures,
  Darwin/Linux ARM, and callee-saved/function-level/block-local modes;
- one preserved late diagnostic/type-projection encounter test;
- two real direct-indexed inline-error-edge/first-error cases.

The object comparisons include managed reload-bearing code and assert that the
actual reload policy is exercised. They use optimize=False; active MADD and
tail-call plans are not covered by these fixtures. They compare register mappings, scalar/slot/
type tables, diagnostic counters, callee saves, fusion/frame state, sections,
symbol/relocation payloads and the complete packed object. No native behavior is
inferred from those host comparisons.

A coordinator may select the single new test file after source review and
admission, using the existing pinned host interpreter, strict single-process
bootstrap, exclusive lock and unchanged 300-second / 4-GiB AS / NPROC=0 guard,
with a 4-GiB free reserve. The bounded pytest payload is:

```
python -m pytest -x -n0 -vv --tb=short -o addopts= tests/python/test_aarch64_allocator_slot_exclusion.py
```

Do not execute this command without its established external guard. This packet
does not itself admit execution. Existing loop/call-result regression neighbors
can be selected separately by the coordinator; they are not silently included.

If focused equivalence passes, the smallest next question is actual skipped
per-value walks and predecessor visits on one already-authorized, correct-target
ordinary host module, alongside exact object/root/relocation equality. The
mechanism must first demonstrate material coverage before any timing or broader
qualification is proposed. No new profile, full Stage1, runtime build, module
regeneration or platform-restricted diagnostic is part of this packet.
