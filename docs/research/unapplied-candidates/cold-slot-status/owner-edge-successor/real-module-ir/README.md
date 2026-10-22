# Real c_ast library-module structural experiment

This is an unapplied, coordinator-run experiment for the existing cold
slot-status reporter candidate. It changes no production or test source.
The original candidate patch and owner-edge successor remain byte-identical.

## Input and scope

The actual input is `pcc/frontends/c/ast/c_ast.py`, module
`pcc.frontends.c.ast.c_ast`: 28,044 bytes, 942 lines, 56 classes and 113
function/method definitions. Its SHA256 is
`21eacf1370cec218ea7ff97092ee47a96752505067caad2699f20239c74f2b58`.
Both canonical exact818 production source and the materialized candidate
contain these same bytes.

The complete input imports only `sys` and uses only `sys.stdout`.
Current production import policy owns that module directly; attribute
lowering emits `py_sys_stream_object(1)`. No compiled sibling provider or
synthetic export table is required. The normal single-source emit-only
library frontend is the scope. This does not recreate the lost full Stage1
context or measure its c_parser_actions worker.

Both arms use actual parse/lift, inference and L1 generation with
`ir_scaffold_mode="on"`, strict no-libpython, native callable values, the
actual source path, an identical explicit host target, and library mode.
Normal sibling/export/root defaults remain in force. The baseline uses the
reviewed former inline method in the candidate frontend; its AST body is
checked against the exact canonical production method. It is not a second
independent compiler build.

The post-pass tier is the real owned default `mem2reg,sroa`, including the
full owned mem2reg dispatcher. This differs from the earlier small probe's
explicit inlining stress tier. Existing host tests already cover both owned
inlining modes; this experiment verifies the reporter remains internal,
noinline and returning normally after default passes.

## Checks and artifacts

- Raw and post-pass owned parser/verifier acceptance; zero actual py_cpy
  calls and zero strict.nolib.stub bodies. Strict stubs cannot pass as a
  complete real-module result.
- Every observed real status-producing call retains its signed-negative
  comparison and distinct success/error successors. The unique producer is
  resolved within the same owning function: legitimate lease-cleanup branches
  may separate a call from its status check.
- The candidate calls the single reporter only in those failure blocks,
  then takes its original cleanup edge. Existing cleanup blocks are checked
  for unchanged contents during each observation. Both arms compare local
  alpha-renamed cleanup instruction sequences, preserving repeated-value
  relationships within each block. After default passes only, a baseline PHI
  may list both the error and report predecessor. The comparison drops the
  report incoming edge only when the surviving error edge exists and carries
  the exact same scalar value. All other PHI edges and instructions remain.
  Raw comparisons do not use this normalization; emitted IR is never edited.
  Unique cleanup programs are hashed and retained once to bound repetition.
- The message and source-frame arguments match the actual operation, source
  function, file and line. Baseline inline pending-exception/report branches
  and candidate caller branches are checked separately.
- Public function signatures, noreturn declarations/definitions, non-report
  runtime call counts and per-site cleanup shapes match. The helper has
  internal `void(ptr,ptr,ptr,ptr,i32,i1)` ABI, no noreturn attribute, no
  unreachable instruction and a normal `ret void`.
- The expected removal is exactly two per-site cold blocks, offset by the
  helper's four blocks, and one dead unreachable block per site. The raw
  and default-pass reporter protocol call deltas are checked explicitly.

Raw/post-pass IR, actual status witnesses, full structural contracts and a
durable phase/result JSON are retained. One arm is released before generating
the next. Counters and observer-weighted durations describe this experiment;
they do not prove native semantics or a Stage1/runtime speedup. Local
alpha-renaming is structural evidence, not a full program-equivalence proof.

## Bounded coordinator command

Use the already reviewed single-process bootstrap and guard, after the
preceding gate is terminal. It must enforce hard NPROC=0, hard AS=4 GiB,
the exact interpreter and bootstrap identities, audit denials, known-root
pidfd wait/cleanup, the shared exclusive lock, and the current 4 GiB free-space reserve.
Keep one 300-second bound for the whole two-arm experiment. Do not invoke
the driver directly or widen a failed boundary. The coordinator must verify
both full source inventories before and after; the driver's hash checks
supplement rather than replace that source seal.

Payload arguments:

```text
real_module_ir.py --source CANDIDATE_SOURCE --canonical CANONICAL_SOURCE --output FRESH_OUTPUT
```

The materialized candidate has `source-manifest.json` beside its `source`
directory; canonical source has `materialized-source-manifest.json` beside
its `source` directory. Their exact identities and changed-file hashes are
bound in `manifest.json`. All source roots are read-only inputs. Estimated
output reservation is 256 MiB in addition to the 4 GiB free-space reserve; this is capacity
planning, not a measured artifact size or additional execution budget.

Execution status when frozen: UNRUN. Native runtime/executable behavior,
default parallel workers, full Stage1 and fixed-point qualification remain
separate gates. General native supervision is still a prerequisite for those
executions.
