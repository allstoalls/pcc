# Worker cohort half-bands: unapplied source recovery packet

Status: V3 source-only candidate; independent final review pending. No candidate
imports, tests, compiler/native builds, profiling or timing have run. This packet
preserves the original V1 patch, its minimal V2 diagnostic-order correction and
the V3 quotient-scan correction in one artifact set. Production application and execution require their own review.

## Exact scope and recovery

The base production revision is `bf1bf82d8242a60a85a06322ceee97fab5346cc0`,
with PCC subtree `930263bf204e578796c72dab5d4aa31fd7ab27ae`. The retained
ordinary source inventory is SHA-256
`7430a30766c11a59e7185758b725b289388ae131498ce6a8a568bdf10ff773ba`.
Its production subtree matches that revision; unrelated retained documentation,
bootstrap and test files are not a claim of an exact current-master checkout.
Each of the three actual preimages and final postimages is bound in manifest.json.
Verify those preimages against the intended checkout before applying anything.

Apply `candidate-v1.patch`, then `v2-logging-order.patch`, then
`v3-scan-termination.patch`, in that order to an independent ordinary copy. Check every final SHA-256 and mode in manifest.json.
Do not apply V1 alone: it inserts new log fields between two existing fields
whose adjacency is covered by an unchanged diagnostic regression. V2 appends the
new fields after the original monotonic timestamp and preserves the old text
prefix. V2's growing-upper loop can fail to terminate for a positive-infinite
input, although actual artifact sizes are integers. V3 restores the original
size //= 2 loop and adds one regression node. A positive-infinite sum follows
that old loop to a NaN quotient and retains old band 1 without calculating a
midpoint; NaN input dimensions and negative infinity follow the unchanged
recognizer's fallback. Subunit positive finite values retain the old loop's one
step before the new finite split. No type rejection policy or existing test
assertion is changed. Preimages/postimages retained by the author are not
bundled; the three patches reconstruct all changes.

Only these paths change:

- pcc/frontends/python/worker_resource_plan.py
- pcc/frontends/python/worker_process_pool.py
- tests/python/test_worker_codegen_cohorts.py

There is no ARM allocator, OSError, code generation, runtime, object-format or
compiler-cap change in this packet.

## Mechanism and safety contract

The existing recognizer restricts this ordering to uniform independent singleton
codegen tasks with the same execution class, six nonnegative input dimensions,
matching sum/max source and AST sizes, identical shared-export size and count 1.
Unknown shapes and other phases retain the existing fallback order/readiness.
For recognized tasks the size is source bytes plus AST bytes. Each existing
power-of-two interval [L, 2L) is split once at its arithmetic midpoint, 1.5L;
zero retains its own cohort. The comparison uses integers only. The lower and
upper halves have size ratios below 1.5 and 4/3 respectively. Within a cohort,
the original largest-first calibration and stable task-index tie break remain.
One fixed subdivision is a bounded scheduling experiment, not a threshold tuned
to missing CI input vectors or measured performance.

For this uniform inventory, a covering completed vector must be at least as
large in both source and AST bytes, hence in their sum. A smaller completed
cohort therefore cannot cover a larger pending cohort. The existing barrier
prevents a larger cohort from starting before the smaller pending/active cohort
has drained. This limits newly admitted covering donors without filtering or
reducing any observation. It is not a general claim about arbitrary preloaded
same-class history or separate earlier scheduling invocations.

The MAX over all applicable completed observations is unchanged. So are explicit
estimates, incomplete-attempt floors, the 25% plus 128 MiB margin, tree/owner
accounting, hard RSS cap, fresh-state checks, live reservation growth, cancellation,
process retirement, drain and the one bounded retry. Completed observations are
still recorded only after the original successful final-report checks. Task
indices, commands, output paths and compiled results are untouched.

More cohorts may cause more exclusive calibrations, more barriers and worse
throughput. Within-half high-peak forecasts can still serialize a cohort. No
speedup, safely lower memory requirement or Stage1 completion is established.

## Compact evidence for the next ordinary CI

The existing PCC_PY_FRONTEND_WORKER_TIMING switch gates the additional fields.
Its disabled branch returns before task inspection or hashing. Enabled admission
lines add input_count, the first eight numeric inputs, SHA-256 of the execution
class and the cohort identifier (-1 for the existing fallback route). Six values
are sufficient for recognized singleton codegen. Existing phase/module/index/PID,
reservation, available-byte, peak and monotonic fields remain. Batch identity is
still emitted in the existing bounded groups of sixteen modules.

No raw class string, private manifest directory, executable command, environment,
report path, attempt token, source or AST payload is added. The class digest lets
readback group comparable events without publishing the class contents. The
hashlib primitive already appears in this scheduler's attempt-token path, but
this changed enabled call shape and native compiler behavior remain unqualified.

The motivating bf1 Mac run 38022657272 reached its unchanged 2400-second Stage1
deadline. After c_parser retired with measured peak 965,492,736 bytes, its padded
reservation was 1,341,083,648 bytes. Later 49 starts used that reservation. Between
that retirement and the last observed admission, 933.47 of 949.45 seconds had
one active worker. Those are observed occupied durations, not idle time or an
achievable speedup. Two such reservations exceed the observed 2,268,053,504-byte
worker allowance. Raw archive SHA-256 is
`b8a720579524cbf3fbb9e62f113285784306ad13ac2bf622552d52e50d635267`.
The existing logs lack the six input dimensions, so they do not prove that the
new midpoint separates that particular high-peak donor from every later task.
Future ordinary CI fields are needed to test that mechanism. This evidence is
from bf1, not the user's earlier c326 snapshot.

## Small first gate and open qualification

Static source inventory: 42 cohort cases (16 existing plus 26 new) and 15 existing
admission-diagnostic cases. This is not pytest collection or execution evidence.
The proposed single-process gate selects 56 cases with exactly one known real
child-process node excluded and explicitly UNRUN:

```sh
python -m pytest -x -q -n0 -o addopts= \
  tests/python/test_worker_codegen_cohorts.py \
  tests/python/test_worker_admission_diagnostics.py \
  -k 'not test_small_real_children_keep_cohort_barrier_and_original_outputs'
```

The coordinator must use the existing serialized guard: 300 seconds, hard 4 GiB
address space/NPROC=0, 4 GiB free reserve, unchanged source/import checks. The
selected scheduler cases use mocked child lifecycle/reporting; the excluded real
child test remains in source for an admitted process-capable lane or ordinary CI.
Adjusting its and the mock's sizes keeps the original overlap/growth/cancel/retry
witnesses in the same finer cohort. All original lifecycle assertions remain.

New regressions cover midpoint/power-of-two/zero and nonfinite/subunit boundaries, large integers,
stable identities, the componentwise sum proof, unchanged MAX observations,
cohort draining, bounded/privacy-preserving diagnostics and disabled task access.
Native pcc1, real child lifecycle for this version, five-GC behavior, Stage1 and
performance are all UNRUN. After focused review and the bounded host gate, use
normal exact-source CI to establish actual scheduling/peak/deadline behavior.
Do not add offline corpus rounds, raise caps or infer CI speed from synthetic
ordering tests. Coordinate the single active CI launch with other production
changes to avoid cancellation by overlapping pushes.
