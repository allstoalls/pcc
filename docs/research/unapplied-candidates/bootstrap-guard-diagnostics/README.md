# Bootstrap guard diagnostics candidate

Status: **Source-only candidate; tests UNRUN.** This packet preserves work for
review and qualification. The patch has not been applied to production.

## Scope

- Replay captured compiler stdout/stderr before final guard-receipt validation,
  so an interrupted guard does not hide the compiler's diagnostics.
- Distinguish a missing receipt or an incomplete RUNNING/selection receipt from
  malformed data or an out-of-bounds selected cap. Include the receipt location
  and observed guard return code. Receipt failures continue to fail closed.
- Align the existing macOS artifact-preservation test with the workflow's
  current step name and `always()` condition. Preserve assertions for the same
  six narrow evidence paths and configured resource bounds.

The workflow, memory caps, admission policy, timeouts, and permissions are not
changed. The test fixtures simulate receipts and process completion; they do
not launch a compiler.

## Exact baseline and files

The production-code baseline is commit
`818187ba7dfff54df589b93bc10594f87786fe22`, tree
`603baabe2e9d6508b4af2d7e5c1f489d8f9b8562`. The three preimages also match the
published source at `fe054fd02931c1e616a9c287b6d32362290b5e1d`.
`manifest.json` records full baseline/candidate hashes and the patch identity.

The patch changes only:

- `scripts/bootstrap.py`
- `tests/python/test_worker_memory_budget_selection.py`
- `tests/python/test_worker_resource_plan.py`

## Validation boundary

Only byte-identity, syntax parsing, source review by the author, and whitespace
checks have been performed. Focused tests, native execution, and macOS
execution are UNRUN. The manifest lists the focused test selectors for a
separately admitted run against an ordinary source tree. Existing successful
admission and preflight-rejection tests should remain in that run.

Do not treat this source-preservation packet as a passing test receipt or a
validated fix. Review and qualification must precede production application.
