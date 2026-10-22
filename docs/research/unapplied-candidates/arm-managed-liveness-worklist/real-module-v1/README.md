# Real ARM managed-liveness mechanism and output gate

Source-only, UNRUN proposal. This packet applies no production changes. It
observes the separately preserved one-file ARM worklist candidate. The latest
51-case host result is preserved in commit
`c649d993cc0f15f112ae83fc0020d25f8236c9ca`; that result establishes the tested
helper/control shapes, not current real-module work reduction or performance.

## One real target-specific input

Use the complete `pcc/frontends/c/ast/c_ast.py`, SHA256
`21eacf1370cec218ea7ff97092ee47a96752505067caad2699f20239c74f2b58`.
The driver checks its only import is `sys`, its only sys attribute is `stdout`,
and actual owned-import policy needs no compiled provider for sys. Normal
parse/lift, inference and L1 library generation retain complete module content,
ordinary export defaults, implicit GC roots and borrowed-return ownership.
No fabricated sibling exports or incomplete multi-module context is supplied.

Set `arm64-apple-darwin23.6.0` before generation, including both the generator
and module target fields. Consume the direct module owned by `generate`; do
not recapture its empty text return. Require zero fallback and no libpython
edge/stub. Save the complete module through the ordinary indexed process codec
before target preparation. Both fresh processes decode that same new pidx and
check its target and preparation receipt. An old x86 pidx is never retagged or
reused. This is a full single-source library context, not the 456-module Stage1
export context or parallel worker route.

For the Mac direct native-object route, production
`pipeline_frontend_worker_execution.py` calls
`emit_aarch64_darwin_indexed_transport(module, optimize=False,
structured_instructions=True)`, then `assemble_sections` and
`encode_native_object_from_sections`. This gate uses those same calls. It
requires the transport to be native-finalized with no residual lines or
fallback instructions. The shared ARM planner selects
`build_stack_map_plans(target="aarch64-darwin")`, which calls the actual
`_native_managed_liveness`. Linux AArch64 currently uses a different object
transport; this proposal does not silently substitute it. Host cross-compiling
this target is not Mac-device or native-pcc1 performance evidence.

Effective flags are pinned: direct capture/emission/fused uses and zero fallback
on; validation/text-control/release frontend/inline-error capture off; Python
IR passes and target passes off, target-pass transport text, code profile empty.
Inline-error capture remains its current default-off shape here; focused host
coverage separately exercises an actual inline trigger. optimize=False prevents
tail-call replanning, and the gate requires one observed native liveness
invocation per emitted function, with no missing/duplicate names.

## Actual counts and ownership boundary

Observers wrap the existing liveness entry and existing row-transfer method.
They call each original function once and return its unchanged result. The
row method must run only inside that active, nonreentrant liveness owner.
No root/provenance algorithm, row equation, PHI seed, publication, resource cap
or compiler source is altered by the harness.

Record each function/invocation's:

- B blocks and E successor occurrences, including duplicate and inline edges
- T actually tracked MANAGED/AMBIGUOUS value IDs and W=ceil(T/30)
- Complete prepared CFG/inline-trigger and provenance contracts
- Actual row visits, changed-row count and per-block visit counts for W>0
- Baseline K only when W>0 and B>0: require K complete reverse-order sweeps
  and no changes in the final sweep
- Candidate's exact first reverse sweep and subsequent visits
- Zero-width row count; require B calls and record no inferred block IDs or K
- Every call's complete state contract, as described below

The row-work proxy is:

`sum over actual row visits of W * (2 + outdegree)`

One unit represents a word visited by row copy, successor union or row
convergence. It is not a count of CPU operations. It excludes queue/index
construction, allocation, PHI seeding, the final backwards scan and all other
code generation. Per-function rows and degree histograms let the comparator
recompute the aggregate rather than trusting a summary alone. W=0 contributes
zero dense work and retains the original candidate path.

The packed call auxiliary column has two meanings, selected by
`CALL_FLAG_FRAME_PROTOCOL`. For those protocol calls it is a root-state ID;
it must not be decoded as a liveness record. The observer hashes protocol IDs
before and after liveness and requires them unchanged. For every other call,
including calls later skipped by stackmap emission, it captures the exact
state ID and ordered value IDs/names immediately after liveness returns.
State/overflow bounds are checked. The result is returned to the ordinary
planner, which retains responsibility for closing it. Origins are read-only
and must also be identical before/after. Complete call records are retained
as JSONL and byte-compared across arms.

## Output and stop rule, fixed before execution

Run one baseline arm and one candidate arm. Require all of the following:

1. Identical fresh pidx, generation CFG, prepared CFG, value/provenance
   contracts, function/invocation coverage and complete call-state bytes.
2. Identical packed object bytes and complete normalized decoded object and
   stackmap contracts. Object comparison covers sections, symbols,
   relocations, final instruction offsets, roots/reloads and compact unwind;
   compact-unwind presence is checked, but this is not an independent unwind
   interpreter. Decoded contracts are hashed as a complete JSON stream rather
   than retaining redundant large JSON copies. Full object bytes remain.
3. Positive real tracked domain and positive baseline dense-row work.
4. At least 25% aggregate dense-row-work reduction:
   `100 * candidate_work <= 75 * baseline_work`.
5. No increase in aggregate row visits or positive-word successor visits.

Failure of any semantic/equality/coverage check stops the gate as FAIL.
Output equality with insufficient work reduction yields
`HOLD_NO_MATERIAL_ROW_WORK_REDUCTION`. Stop there without timing rounds,
capacity tuning, runtime builds or larger-module substitutions. A mechanism
PASS means only eligibility for a separate timing review; it does not launch
or authorize that experiment. Later uncounted whole-pipeline benefit remains
subject to its own order/variance controls and at least 5% materiality floor.

Observer-weighted elapsed time is retained only to explain resource use.
Neither reduced row counts nor those durations establish solver time share,
whole-codegen speed, native performance or full Stage1 completion.

## Source, execution and preservation boundaries

The full baseline inventory is `ea395e18...`; its complete pcc subtree
`d234174fbac80575ea822f6100f65fe914dd4498` matches published production02b9.
That retained full snapshot has older unrelated tests/docs/bootstrap and is
not relabeled an exact02b9 checkout. The candidate inventory `6e6b6d03...`
differs only in the saved production file and new host test. Candidate pcc tree
`c291620d0277e22b7dffb731ba38a20a8ea59283` and compiler checksum `71a7d53a...`
remain exactly those used by the 51-PASS gate. No x86 bridge or OSError work is
included. The manifest records full hashes, and the coordinator must verify
complete source inventories before/after in addition to the driver's targeted
pins and imported-module checks.

Prepare once, then emit baseline and candidate in separate fresh processes.
The driver interface is:

```text
real_module_gate.py --manifest-sha256 MANIFEST_SHA --operation prepare
  --source BASELINE_SOURCE --arm baseline --input BASELINE_SOURCE/pcc/frontends/c/ast/c_ast.py
  --input-sha256 C_AST_SHA --output FRESH_PREPARATION

real_module_gate.py --manifest-sha256 MANIFEST_SHA --operation emit
  --source ARM_SOURCE --arm baseline|candidate --input FRESH_PREPARATION/input.pidx
  --input-sha256 SEALED_PIDX_SHA --prepared-result FRESH_PREPARATION/result.json
  --prepared-result-sha256 PREPARATION_RESULT_SHA --output FRESH_ARM

compare_results.py --manifest-sha256 MANIFEST_SHA
  --baseline BASELINE_ARM/result.json --baseline-sha256 BASELINE_RESULT_SHA
  --candidate CANDIDATE_ARM/result.json --candidate-sha256 CANDIDATE_RESULT_SHA
  --output FRESH_COMPARISON_JSON
```

Every prepare/emit process keeps 300 seconds, hard AS4GiB/NPROC0, the existing
single-process bootstrap/pidfd lifecycle, shared exclusive lock and continuous
4GiB free reserve. Retain no-auto-pcc1/no-native-provisioning and both live
supervisor reservation variables. No ctypes/FFI/process capability is added.
Stop at the first timeout/guard/audit/source failure and preserve the result.
Plan 1GiB additional output reservation for the new pidx, both objects, call
streams and receipts; this is an estimate, not an increased process budget.
No runtime build, native execution or full Stage1 is requested by this packet.

Bundled files: `real_module_gate.py`, `compare_results.py`, `README.md` and
`manifest.json`. Compiler/test patches and old execution outputs are separately
preserved and are not duplicated here. Independent review and root admission
must finish before the sole execution coordinator runs any stage.
