# Investigation: pcc1 misses the 300 s prior-gate compile on ARM CI

## Status
active — 2026-10-19. Breakdown and two admission causes measured; the
native-worker prior fix is committed (5a9d434dc) but not yet executed inside a
native pcc1; direct-indexed default, emit-lane overlap, link tail and tail
merging are open below.

## Problem Description
`scripts/ci_worker_size_prior_gate.py --phase pcc1` runs
`tests/python/test_worker_size_prior_processes.py::test_size_prior_native_processes_five_collectors[pcc1]`:
pcc1 compiles `tests/fixtures/native/worker_size_priors.py` (16 modules with
its closure) and the binary then runs two cases under five collectors, all
inside one 300 s pytest guard. At b58f3bf55 (run 39213676872) the macOS arm64
job started native-pcc1 at 08:06:04 and was killed at 08:11:06, still emitting
(11 processes); the linux-aarch64 native-pcc1 step failed the same way. The
hosted macOS runner has 3 cores and a 2-4 GiB tree budget
(`--auto-tree-rss-ceiling-bytes 4294967296`); only this one node runs, so the
compile owns the runner.

## Repro
Local, Apple Silicon (12 cores), quiet machine, guard at 16 GiB:

```bash
python /Users/jiamo/pcc-snapshots/measure_prior.py <snapshot> <snapshot>/build/bootstrap/pcc1 <out> 1800
```

`measure_prior.py` (outside the repo) applies `ci_worker_size_prior_gate.environment()`,
the snapshot's runtime archive, `PCC_RUNTIME_CC/PCC_HOST_PYTHON/PCC_HOST_PCC=/usr/bin/false`,
and runs pcc1 under `scripts/run_process_tree_sample.py --max-tree-rss-bytes 16 GiB`.

## Test [CONFIRMED]
pcc1 built from 0272b8f4f (424,850,744 bytes): 251.5 s and, re-measured later
under the same conditions, 259.6 s. Timeline of one run (text route, from
`compile.stderr` phase events and `samples.tsv`):

```text
frontend (export/summary/codegen workers)   ~67 s, codegen one module at a time
split                                        ~2 s
emit lane oversized (1 item, 1 job)          49 s   other 11 cores idle
emit lane medium (15 items, 8 jobs)          31 s
emit lane small (40 items, 8 jobs)           35 s
single-process tail after the last emit      ~78 s  (link and finish; unprofiled)
```

Emit-worker CPU, summed from worker phase events: emit 389 s, assemble 81 s,
encode 20 s (492 s). On 3 cores the emit work alone is ~164 s.

All 56 emit items log `packed_emit_assemble outcome=declined`:
`emit_packed_object_in_process` handles only x86_64 Linux, so AArch64 renders
assembly text and assembles it again (the 81 s).

## Admission serializes pcc1's own frontend workers [CONFIRMED]
Under a tree budget, `resource_tasks_for_commands` gave the export/summary/
codegen startup priors only to CPython's `python -m pcc` workers. pcc1's own
workers had none, so a task ran only after an exclusive calibration unless a
completed sample covered all six input dimensions. Source and AST sizes do not
move together (e.g. pcc.unsafe 39.5k/662k, worker_size_priors 12.3k/670k,
worker_resource_plan 33.8k/987k), so 13 of 16 codegen tasks calibrated alone
while peaking at 93-393 MiB against a 16 GiB budget. Without a budget the
compiled codegen pool is capped at 2 jobs.

Fix (5a9d434dc): the compiler's own worker prefix — CPython `-m pcc`, or a
native pcc's own executable — gets the same models. Against them the native
peaks of four local compiles (135 completions) padded to at most 0.70
(export), 0.60 (summary) and 1.08 (codegen) of the forecast. Pending: a
Stage1 built from this source, then the prior compile.

## Direct-indexed route is not the CLI default [CONFIRMED]
Bootstrap sets `PCC_DIRECT_INDEXED_KERNEL_CAPTURE=1` and `..._EMIT=1`, so
Stage1/Stage2 never render LLVM text; a plain `pcc1 app.py -o app` (and this
gate) does, and the emit workers parse it back
([predecessor](pcc1-frontend-direct-indexed-kernel-plane.md)). With both
variables set, the same pcc1 compiled the fixture correctly
(`llvm text render elapsed_ms=0`, `fallback_records=0`) but emission then
runs inside each codegen worker, so under the serialized admission above the
whole compile was serial (still running at 392 s, contended by a Stage1).
A default for host executables (explicit variables win; text stays for
LLVM/library output, cross targets, GPU and libpython modes) is written but
uncommitted until it is measured together with the admission fix.

## Code size: common tails [CONFIRMED estimate]
Scanning the 0272b8f4f pcc1 AArch64 code (PC-relative operands normalized to
absolute targets; blocks split at terminators and branch targets):
`_emit_call` (4.79 MB) has 0.8% whole-block duplicates but up to 22.7% in
suffixes shared by blocks that end in the same jump; the 300 largest functions
(130 MB of 373.5 MB) show 1.6 MB and 27.9 MB (21.4%). This is an upper bound
(greedy suffix count). A BranchFolder-style tail merge in the self backend
must keep one `L_pcc_smap_*` call record per merged call and merge only when
the records match.

## Proposals
- No.1 native worker priors [committed 5a9d434dc; native run pending]
- No.2 direct-indexed default for host executables [pending measurement]
- No.3 run the oversized emit lane alongside the other lanes [pending; text route only]
- No.4 profile the ~78 s single-process tail with `--profile-json` [pending]
- No.5 AArch64 packed emit (skip text assembly) [see aarch64-direct-instruction-capture-order.md]
- No.6 common-tail merging in the self backend [pending design]
