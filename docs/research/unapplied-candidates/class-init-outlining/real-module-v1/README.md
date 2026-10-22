# Fresh real-module class-outlining structural gate

This is an unapplied, coordinator-run experiment packet. Its five files do not
change compiler or test source. Execution is **UNRUN** at freeze. The exact V6
candidate has a separate 29/29 focused host PASS; that result does not establish
this real-module result, native behavior, a compiler bootstrap, or a speedup.
The original V4 production TypeError and V5 text-order oracle failure remain
preserved with their respective corrections.

## Inputs and scope

Both arms compile the actual complete `pcc/frontends/c/ast/c_ast.py` independently,
using the pinned source bytes and `arm64-apple-darwin23.6.0`. Each freshly runs
`parse_and_lift`, normal `infer_module`, strict no-libpython library L1 generation,
and the ordinary default owned `mem2reg,sroa` passes. No previous `.pidx` or IR is
an input. The real source imports only `sys` and uses `sys.stdout`; the existing
owned-builtin policy is checked. No external export table, reduced class body,
or fabricated import context is supplied.

Baseline production is commit `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`, pcc tree
`d234174fbac80575ea822f6100f65fe914dd4498`, in the retained inventory beginning
`ea395e18`. That source container has older unrelated test/document artifacts,
so it is not described as a full checkout of that commit. Candidate inventory
`98d5eff5` has only the reviewed two production postimages plus the new focused
test relative to that container; its pcc tree is
`dc63c2a946f533f7b31ce63db2f759a044fdc37f`. All exact file, inventory and codegen
hashes are in `manifest.json`.

The representation is deliberately textual owned IR, with indexed capture and
inline-error capture disabled. The ARM preparation and precise root planner
then consume each freshly parsed raw/default-pass module. This is the normal
host single-module library frontend and owned ARM analysis, not the full CI
parallel/indexed-worker route. `PCC_WITH_THREADS=0` explicitly selects this
module's ordinary no-thread-import/default configuration. Thread-enabled,
native compiler, GC0–4 execution, object/link output, embedding execution and
full Stage1 remain separate unqualified boundaries.

## Required contracts

- Record actual admitted and inline classes independently at both embedding
  entrypoints. Source contains 56 classes; the earlier prediction of 55
  eligible classes is not an observation or required outcome.
- Each admitted class has exactly one physical internal `noinline i32 ()`
  helper, retained after the default owned passes, with exactly one call from
  each original init/top entrypoint. Each invocation still executes a fresh
  class construction. Init and top retain their different guard behavior.
- Each helper loads the existing base global, publishes its own class global,
  retains literal status returns, and adds no Python recursion activation.
  Each call tests success against one and takes the existing `err.exit` path
  on failure. Failure cannot reach a later class helper or its success edge.
- Original top-level dispatch order is recorded. Actual CFG dominance and
  instruction order establish class construction order in both entrypoints,
  and the interleaved `_build_visitor_dispatch` binding remains between Node
  and NodeVisitor. Serialized block layout is not used as execution order.
- Public function signatures and global ABI records must match. Selected
  expanded construction/default/signature/abort operation counts and recursion
  activation counts must match: each shared helper is counted twice for its
  two static call sites. These counts describe code, not dynamic execution.
  Other expanded runtime-call differences are retained for review; per-function
  error/frame machinery need not have identical static counts after outlining.
- Every raw and post-pass function passes the existing owned SSA/type/CFG
  verifier, ordinary ARM stack preparation, and precise root planning.
  Generated instruction/block container ownership is checked on the original
  generated objects before reparsing.
  These checks cannot prove the lineage of a foreign same-spelled host value
  object after it has already been rendered to text.
- A read-only planner observer additionally requires zero active root groups
  at every reachable helper return, including groups with zero locations.
  It traces exact foreign-lease SSA tokens and alias-resolved slots: every
  successful acquisition path must reach exactly one matching cleanup attempt,
  and failed acquisition paths cannot release that token. It uses the real
  kernel and alias APIs. It rejects cycles, inline-error edges, unknown lease
  ABIs, transformed tokens, unresolved origins or its finite traversal budget.
  Unsupported shapes block the gate. This proves emitted cleanup-attempt
  coverage, not successful runtime release or complete GC correctness.
- The observer calls the original planner once, keeps no native owner, defers
  failures until normal cleanup, and restores the hook. The driver closes
  every returned packed plan and every published indexed kernel, including
  unvisited functions after a preparation/planning failure. No planner record-to-text expansion is
  performed merely for diagnostics.

## Predeclared decision and bounds

After both semantic/structural arms pass, the comparator requires at least
**25% fewer complete-module post-default instructions** and **50% fewer
instructions in the largest function**. Both metrics count textual function
instructions including PHIs and terminators. Indexed ordinary instructions,
PHIs, blocks, all function sizes, helper counts, text bytes and raw-phase totals
are retained separately. No percentage is a CPU-time estimate.

Any contract failure stops. Successful contracts below either threshold produce
`HOLD_NO_MATERIAL_IR_REDUCTION`; no timing or extra rounds follow automatically.
Passing both thresholds produces `MECHANISM_THRESHOLD_MET_REVIEW_ONLY`, still
requiring review and separate native/performance admission.

The proposed run is one baseline process and then one candidate process, each
under the existing exclusive single-process supervisor: 300 seconds, hard
4 GiB address space, hard `NPROC=0`, continuous 4 GiB free reserve, and 512 MiB
maximum output growth per arm. Reserve 1 GiB for the pair. Full source byte/mode
inventories are guarded outside each process. Previous arm cleanup, root reap,
audit-denial count and source seals must pass before the next arm. The packet
does not bypass the supervisor or alter native-provisioning reservations.

The coordinator supplies exact source/output locations and the manifest digest:

```sh
python -I -S -B BOOTSTRAP -- real_module_gate.py \
  --manifest-sha256 MANIFEST_SHA256 --source BASELINE_SOURCE \
  --arm baseline --output FRESH_BASELINE_OUTPUT
python -I -S -B BOOTSTRAP -- real_module_gate.py \
  --manifest-sha256 MANIFEST_SHA256 --source CANDIDATE_SOURCE \
  --arm candidate --output FRESH_CANDIDATE_OUTPUT
python -I -S -B compare_results.py --manifest-sha256 MANIFEST_SHA256 \
  --baseline BASELINE_OUTPUT --baseline-result-sha256 BASELINE_RESULT_SHA256 \
  --candidate CANDIDATE_OUTPUT --candidate-result-sha256 CANDIDATE_RESULT_SHA256 \
  --output FRESH_COMPARISON_JSON
```

These show payload arguments only; they are not unsupervised launch permission.
The actual reviewed wrapper pins all five packet files, sources and guards.
Candidate payload and comparison must fit its original 300-second process. There
is no runtime construction, native emission, FFI, subprocess, benchmark,
denied diagnostic reconstruction or production application in this packet.

## V2 harness-only correction

V1 remained unexecuted. Final source review found that preparation publishes all
function kernels before planning, while V1 closed only the current function on
a failure. V2 moves preparation inside the cleanup boundary and also retires
unvisited published kernels. This changes only exceptional harness cleanup;
compiler source, measured counts, contracts, thresholds and resource limits are
unchanged. The frozen V1 manifest is bound below for recovery; no failed run is
being replaced.
