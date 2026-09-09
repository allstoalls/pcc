# AGENTS.md

**Project direction:** [Project Intent](docs/project-intent.md) is the standing
north star. Read it before changing direction or trading away a requirement.
Code establishes current behavior; it does not redefine the intended contract.

## Start with current code

Current source, effective configuration and execution of its artifacts are
first-hand evidence. Documents are requirements or historical navigation;
they do not prove the current implementation. User instructions take priority.

1. Read this short guide, inspect `git status --short`, and locate the relevant
   entrypoint, implementation and tests with `rg`. Trace the path actually used.
2. Search `docs/investigations/INDEX.md` or `docs/knowledge/` by the concrete
   symbol/symptom. Read the matching experiment and its later corrections only;
   expand when needed. Do not preload all knowledge pages or whole handoffs.
3. Check a historical claim's source revision, command, options and artifacts
   against current code before reusing it. `[CONFIRMED]`, `[DENIED]`, "current"
   and "uncommitted" describe that recorded run, not today's worktree. If the
   prerequisite changed, record the difference and re-test the relevant claim.
4. Continue the user's active task. GitHub issues in `allstoalls/pcc` and the
   sibling repositories track work; dated handoffs and retired goal documents
   are not task queues. Preserve unfinished scope when handling a short subtask.
   Resume parent work after the subtask unless the user stops or replaces it.
   Record new actionable work in the relevant issue. Before a long pause,
   leave a dated handoff with source identity, unfinished state and repro steps.

## Project contracts

These are required outcomes, not assertions that migration is complete. See
[compiler-contract.md](docs/compiler-contract.md) when changing these boundaries.

- Host pcc uses only CPython and its standard library. Native pcc1 has no
  external LLVM/libLLVM/llvmlite, cc/clang/gcc, host Python/libpython or third-party
  dependency, including C preprocessing, optimization, runtime construction,
  object emission, linking, archiving, signing, cache checks and installation.
  A subprocess or bundled external tool does not change its execution owner.
  Existing external routes are migration defects; fail explicitly at gaps.
- The production default is the owned self backend. C is a first-class pcc1
  capability. Reuse and complete owned implementations; no silent fallback.
- `pcc`, `python -m pcc` and `pcc1` share public command semantics. Python script
  and `-m` execution must compile/run native code. CPython `import pcc` APIs
  must execute real capabilities; import-only stubs are not implementation.
  Native self-host, five-GC and maintainer-directed CLI parity are P0;
  embedding/ecosystem breadth must not displace them.
- Preserve ordinary Python/C semantics, arbitrary-precision Python integers,
  identity, exceptions, finalizers, weakrefs and resurrection. Value layouts
  and fixed-width machine types are explicit. No package-specific shortcuts.
- Preserve all five GC backends and their shared slot/root contract. Keep C
  and pcc-Python migration mirrors differential-equal; the production runtime
  target is pcc-Python over owned intrinsics and the platform ABI.
- The self-host chain is host pcc0 -> pcc1 -> pcc2 -> pcc3. A stage1 build is
  not a new-source fixed point. Distinguish host/native, self-backed/LLVM-backed,
  no-libpython/libpython and pcc-native/cpython-compat evidence.
  Classify drift as semantic, IR-text, class-layout, object-model,
  backend nondeterminism, link metadata, perf-only or diagnostic.
- An external LLVM/cc oracle is allowed only as a labeled reference experiment.
  It cannot satisfy a pcc1 ownership claim. Device execution, ecosystem interop
  and distributed execution each require their own real boundary proof.

## Shared work and changes

- Preserve other work. Re-read targets before narrow edits; no destructive
  restore/reset/revert/stash/clean or discarding branch switches without explicit
  user direction. Do not commit/push unless authorized in the current session.
- Do not investigate other sessions through author/timestamp/process heuristics.
  Check relevant input identities for measurement stability, not attribution.
- Fix the generic implementation. Enumerate repeated shapes and check enclosing
  owners/counts before replacement. Run `git diff --check` and focused checks
  after each semantic change; do not stack speculative shared-codegen edits.
- Do not remove unrelated tests, projects or evidence while fixing code.
  Do not change packaging/release metadata outside the requested release work.
  Do not delete documents, plans, tests or project directories unless the user
  explicitly names the files/directories to remove. Cleanup does not authorize
  weakening project intent or maintainer contracts.
  Documentation cleanup should remove stale authority and duplicate guidance,
  keeping experiment provenance searchable. Avoid new running-log documents.
- Prefer existing tools; search [development-tools.md](docs/development-tools.md)
  and `scripts/` before writing a reusable profiler, harness or inventory.
- Keep partial work and skipped gates explicit. Before claiming completion,
  inspect remaining fallbacks and open boundaries against the requested scope.
  Read back every artifact cited as produced. No silent idle periods: report
  concrete progress or blockers within 60 seconds during ongoing work.

## Compiler performance: prove the selected pipeline first

- Record actual entry -> effective pass order -> implementation/owner -> emitter
  -> link/runtime artifact. Check default/early-return branches, skips, passes
  already applied to inputs and cache identity. Benchmark dispatch must match
  production or state the intentional difference.
- Check pass effects on real hot IR before local tuning: allocas, loads/stores,
  PHIs, cross-block/loop promotion and reasons eligible slots remain. A named
  pass, successful compile or host/native output agreement can still be a no-op.
- Execution ownership, IR representation/transport and algorithm coverage are
  different axes. `native_ir` can parse/serialize text; `memory` transport is
  not a memory-optimization pass. Neither establishes LLVM independence.
- Compare optimizers with identical pre-pass IR and one emitter. Compare
  backends with identical post-pass IR and target options. Freeze and hash
  source, effective options, inputs, outputs, compiler/runtime and cache receipts.
- Profile the actual working processes, then verify the proposed mechanism in
  source/IR. Record the end-to-end gap, measured owner share and plausible gain.
  Against a >=1.5x gap, three candidates each below 5% measured gain or below
  10% Amdahl ceiling require reassessing the owner;
  close known pipeline gaps before attributing the remainder to architecture.
- Execute emitted programs and preserve the five-GC/ownership contracts.
  Instruction counts, byte equality and QPS support different claims.
  The [memory-pass audit](docs/knowledge/2026-09-08-optimizer-pipeline-audit.md)
  is a dated example, not extra mandatory startup reading.

## Validation

- At each user-approved quota checkpoint, record weekly quota remaining. Check
  it during work and before heavy runs. After a drop of 10 percentage points
  (or an earlier user-specified threshold), stop work, clean up owned processes,
  save evidence and a handoff, and wait for the user's review before continuing.
  Status questions and interruptions do not reset this checkpoint. Use observed
  quota data; never claim to monitor a reading that is unavailable.
- Use `env -u LC_ALL uv run ...`; the checked-in `.python-version` is the host
  Python selection (currently 3.15.0rc1). Use timeouts for tests/builds/probes.
- Every pytest invocation needs `-x`. Use `-n0 -vv --tb=short` for diagnosis.
  Long runs need durable live node/failure logs and a process-group watchdog;
  stop/clean up only your own children after timeout. Never report dots as green.
- First run the smallest regression, then the original integration scenario and
  sensitive subsystem checks. A pcc1 fix requires native emitted execution of
  the changed shape (include a function, compile with `-o`, then run).
- Heavy builds and performance runs share `build/.pcc-performance.lock` and
  require a tree-RSS cap, frozen inputs and isolated outputs. Do not edit their
  input closure or run contending bootstrap/runtime builds during measurement.
- Broad suites/five-GC bootstrap are final qualification after focused checks
  and source stability, not diagnosis. Build dependent stages in order and stop
  at the first failure. Do not repeatedly widen timeouts to hide a regression.
- Critical parser/frontend/codegen/runtime/bootstrap changes need the relevant
  native/bootstrap gate; do not claim fixed point without stage1->stage2->stage3.
  Before commit qualification, run bootstrap/fallback baseline checks. Baseline
  JSON files are recorded receipts: validate their identity, not just their label.
- See [validation-workflow.md](docs/validation-workflow.md) for scoped gates,
  watchdogs and correctness invariants before those operations. A full GC gate
  must explicitly select integration tests, for example:

```bash
gtimeout 1800s env -u LC_ALL uv run pytest -x -vv -m integration tests/python/gc/test_pcc_bootstrap_full_gc*.py
```

## Source and documentation routing

| Task | First-hand source / tests; additional procedure when needed |
|---|---|
| CLI/defaults/public API | `pcc/__main__.py`, `pcc/cli_bootstrap.py`, `pcc/cli_core.py`, `pcc/__init__.py`; inspect current dispatch |
| Python lowering/ownership | `pcc/py_frontend/codegen/`, `pcc/py_runtime/`; [debugging playbook](docs/debugging-playbook.md) |
| Passes/self codegen | `pcc/py_frontend/pipeline_pass_driver.py`, `pcc/native_ir/`, `pcc/backend/`; performance rules above |
| C semantics | `pcc/codegen/c_codegen.py`, `pcc/parse/`, `tests/c/`; check signedness and constant evaluation separately |
| GC/ABI | `pcc/py_runtime/py/`, `src/`, `include/`, `tests/python/gc/`; verify layouts/barriers in code, consult relevant upstream reference |
| Bootstrap/performance | `scripts/run_pcc_*`, current receipts and tests; [tool index](docs/development-tools.md) |
| Investigation history | Search [INDEX](docs/investigations/INDEX.md); use [investigation workflow](docs/investigation-workflow.md) when recording an experiment |

When editing an investigation, keep commands, source/artifact identities,
observations, corrections and current open questions together. Regenerate:

```bash
env -u LC_ALL uv run python scripts/regen_investigations_index.py
env -u LC_ALL uv run python scripts/distill_investigations.py
```

Keep AGENTS.md short: stable contracts and routing only. Implementation facts
belong in code/tests; historical results belong beside their experiment.
The [migration audit](docs/agents-migration-audit.md) provides the old-to-new
coverage map and a reproducible check; it is not additional startup reading.
