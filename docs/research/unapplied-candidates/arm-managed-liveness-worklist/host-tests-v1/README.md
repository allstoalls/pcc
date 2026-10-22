# Host regression supplement for the ARM liveness worklist

Unapplied, source-reviewed test proposal. All executions remain UNRUN.
This supplement adds one test file; the saved production postimage
`f1882fa4c29ad15c8843d30a9bf4d4bebf5b52307d0dc4daa155157b8d8289fe`
is unchanged. Apply the separately preserved production patch first, then
`tests.patch`, to an ordinary isolated source tree with exact preimage checks.
The production patch/design was preserved in commit
`f6e7fffc35e6542d30a5eebb96dcbd08880e3352`; it is not duplicated here.

## Static scope

The new `tests/python/test_native_liveness_worklist.py` contains 33 expected
host cases. This is a source count, not an executed pytest receipt.

- 24 forward/reverse-layout chain cases, with and without disconnected blocks,
  selecting MANAGED or AMBIGUOUS provenance. Exact call states must match the
  existing independent set-based liveness implementation. Initial row order,
  each edge's single indexing, bounded queue-slot writes and exact synthetic
  visit counts are checked.
- Two one-slot self-loop cases, including duplicate successor occurrences.
  They require three visits: IN changes, then OUT changes, then stability,
  preserving the existing row helper's IN-or-OUT bool contract.
- One zero-tracked case retains all original row validation and allocates no
  worklist owner.
- Two malformed unreachable-block cases, with zero/nonzero tracked domains,
  retain the exact first successor error class/message despite competing
  invalid targets.
- Two injected row/link failures retain exception identity and prove the new
  owner closes before harness cleanup. The link failure is injected at
  `append2` entry; it does not execute a native realloc failure.
- One empty-block-domain case proves no ring access and observes its new
  capacity-one work owner already closed before result construction.
- One real direct indexed inline-error edge uses a call as the trigger and
  compares with a separate explicitly split text-CFG set oracle. Expected
  live-after sets are before={p,q}, trigger={p,q}, after={q}, cleanup={p}.

Every oracle/candidate run uses a fresh kernel because call-state publication
is mutable. Observers are confined to host tests and restored by monkeypatch
contexts. The existing helper's unrelated scratch lacks an exception owner;
the harness releases those captured temporaries only after testing the new
work arena's close, so it does not hide a candidate lifetime failure.

The trigger comparison supplements the existing PHI oracle, whose own CFG
walk includes terminators only. Duplicate-edge cases use legal repeated branch
targets; they do not construct invalid duplicate inline triggers. Unreachable
cases use parsed text because ordinary direct finalization removes unreachable
blocks. Invalid-target cases call the liveness helper after explicit mutation,
so a prior verifier cannot replace the diagnostic under test.

## Proposed bounded host gate

Run only these two files through the already reviewed single-process host
supervision, after preservation, exact source materialization and admission:

`python -m pytest -x -n0 -vv --tb=short tests/python/test_native_liveness_worklist.py tests/python/test_managed_phi_liveness.py`

Static expected selection is 33 new + 18 existing = 51 host cases, with no
native node or actual compiler/build invocation. Save actual collection and
per-node outcomes; require all 51 PASS with no skip/xfail or swallowed audit
denials. Keep 300 seconds, hard AS4GiB/NPROC0, the shared exclusive lock, 4GiB
free reserve, no auto-pcc1/native provisioning and both live supervisor
reservation markers. Complete source inventories must match before/after.
No resource-cap or guard change is proposed.

Passing this host gate would establish only these liveness/control shapes.
Real ARM module mechanism/output comparison, actual native lowering/execution,
whole-pipeline timing and full Stage1 remain separate and UNRUN. Synthetic
linear-work assertions cannot establish current workload savings. Keep the
production design's real-mechanism stop condition before any timing A/B.

Bundled files: `tests.patch`, `README.md`, `manifest.json`. `tests.patch`
contains the complete new test. The separately saved production patch and a
standalone test postimage file are not duplicated in this public supplement.
