# Self Backend Bootstrap Default Plan

Related plans:

- `docs/plans/self-backend-translation-plan.md`
- `docs/plans/python-frontend-plan.md`
- `docs/plans/archive/p6c6-bootstrap-spike-report.md`
- `docs/plans/llvmcapi-beta4-backlog.md`

## Purpose

Move the supported macOS arm64 bootstrap path from "LLVM/clang native
emission by default" toward "self backend native emission by default", without
claiming a global LLVM replacement before the evidence exists.

This is a supported-host bootstrap-default plan, not a global default plan.

## Current Boundary

Current three-stage bootstrap status before this plan:

- `scripts/bootstrap.sh` can complete stage 1 / stage 2 / stage 3 on the
  supported macOS arm64 development host.
- `pcc2` and `pcc3` can verify byte-identical after normalizing Mach-O
  code-signature metadata.
- The bootstrap entrypoint still uses the Python frontend's LLVM IR path and
  then links `.ll` files through `clang`.
- `pcc/driver/cli_bootstrap.py` currently accepts `--backend` only for compatibility
  and does not thread that selection into native emission.
- The C-mode self backend has `--backend=self` coverage, `--emit-asm`,
  system-assembler-backed `--emit-obj`, and system-link runtime gates, but it
  is not yet connected to Python bootstrap native emission.

Implementation status as of 2026-04-27:

- `compile_python()` and `compile_python_multi()` accept a native-emission
  backend selection.
- `pcc/driver/cli_bootstrap.py`, `pcc/driver/cli_core.py`, and `scripts/pcc_multi.py` thread
  `--backend` into Python native emission.
- `PCC_BACKEND=self` and `--backend=self` explicitly route Python `.ll` through
  the self backend; self failures are surfaced and do not fall back to LLVM.
- `scripts/bootstrap.sh` defaults to `self` on the supported macOS arm64 host
  and keeps `--backend llvm` as the explicit escape hatch. Other hosts keep the
  LLVM default.
- `scripts/run_self_backend_bootstrap_gate.py` compares LLVM and self
  bootstrap runs, captures binary size, libpython linkage, `--help` latency,
  small benchmark compile/run geomeans, and enforces `2.0x` default thresholds.
- The current supported-host smoke still links libpython; this plan still does
  not claim a pure self-hosted runtime boundary.

## Promotion Ladder

1. Current state: explicit opt-in C-mode self backend.
2. First target: Python frontend can choose self for `.ll -> native exe`.
3. Next target: stage-1 self-backed bootstrap gate.
4. Next target: stage-1 / stage-2 / stage-3 self-backed bootstrap gate.
5. Promotion target: supported macOS arm64 bootstrap defaults to self with LLVM
   as an explicit escape hatch.
6. Later target: supported-host general default, after separate correctness and
   performance evidence.

## Token Budget

These are planning estimates for AI-assisted implementation, not engineering
calendar estimates.

| Phase | Scope | Token Estimate |
|---|---|---:|
| 1 | Add Python frontend `.ll -> native` backend selection, preserving LLVM/clang fallback | 20k-40k |
| 2 | Make self backend consume the Python frontend `.ll` shape and run a minimal Python executable | 60k-150k |
| 3 | Build `pcc1` with CPython-hosted pcc while native emission uses self; fix first IR/runtime/link blockers | 150k-400k |
| 4 | Run `pcc1 -> pcc2 -> pcc3`; fix closure, determinism, runtime, and bootstrap boundary blockers | 300k-800k |
| 5 | Add performance gate, promotion runner, docs, and default-switch criteria | 40k-100k |

Expected total:

- optimistic path: `600k-900k` tokens,
- realistic path: `1M-1.8M` tokens,
- worst case if Python frontend IR exposes large new self-backend gaps: `2M+`
  tokens.

## Phase 1: Backend Selection For Python Native Emission

Goal:

- let Python frontend linking choose `llvm` or `self`,
- keep zero-env bootstrap behavior unchanged until promotion,
- make the choice visible in CLI, env, tests, and logs.

Tasks:

- Add a native-emission backend parameter to `compile_python()` and
  `compile_python_multi()`.
- Thread `--backend` from `pcc/driver/cli_bootstrap.py` instead of ignoring it.
- Accept `PCC_BACKEND=self` for bootstrap only as an explicit opt-in, not as a
  default flip.
- Split the current fixed `_link_with_clang()` path into:
  - LLVM/clang `.ll -> native` path,
  - self backend `.ll -> asm -> cc/link` path.
- Keep LLVM fallback explicit; do not silently fall back after self emission
  starts.
- Add focused tests that prove:
  - `--backend=llvm` keeps old behavior,
  - `--backend=self` reaches the self emitter,
  - self emitter failure propagates and does not fall back to LLVM.

Initial gate:

```bash
env -u LC_ALL uv run pytest tests/test_py_multi_file_compile.py tests/test_py_multi_file_bootstrap_shim.py -q -n0
```

Exit criteria:

- Python bootstrap CLI can route native emission to self in a minimal controlled
  test.
- Default bootstrap behavior remains LLVM/clang unless explicitly requested.

## Phase 2: Minimal Python Frontend IR Consumption

Goal:

- run the smallest Python-produced executable through the self backend on the
  supported macOS arm64 host.

Tasks:

- Emit `.ll` for a minimal Python input and feed the exact IR text into self
  backend dispatch.
- Classify all first failures into:
  - unsupported IR syntax,
  - unsupported global/data initializer,
  - unsupported call/runtime boundary,
  - unsupported ABI or relocation form,
  - unsupported linker/runtime archive interaction.
- For every blocker, add one focused self-backend regression before fixing.
- Avoid broad self backend rewrites unless a minimized Python IR reproducer
  proves the gap.

Candidate first probes:

```bash
env -u LC_ALL uv run python -m pcc hello.py -o /tmp/pcc_py_llvm
PCC_BACKEND=self env -u LC_ALL uv run python -m pcc hello.py -o /tmp/pcc_py_self
```

Exit criteria:

- A minimal Python executable links and runs with native emission through self.
- The test proves self emitter involvement, not only successful output.

## Phase 3: Stage-1 Self-Backed Bootstrap Gate

Goal:

- CPython-hosted pcc builds `pcc1`, but native emission uses self instead of the
  fixed LLVM/clang path.

Tasks:

- Add a bootstrap script flag or companion runner:
  - `scripts/bootstrap.sh --backend self --stage 1`, or
  - `scripts/run_self_backend_bootstrap_gate.py --stage 1`.
- Keep the LLVM-backed stage-1 baseline in the same runner for direct
  comparison.
- Capture and report:
  - compile/link wall time,
  - binary size,
  - `pcc1 --help` result,
  - whether libpython was linked,
  - first failing IR/runtime symbol if the gate fails.
- Add timeout protection for every produced binary smoke.

Exit criteria:

- `pcc1` is produced with self-backed native emission.
- `pcc1 --help` exits successfully under a hard timeout.
- LLVM-backed stage-1 remains available as an escape hatch.

## Phase 4: Three-Stage Self-Backed Bootstrap

Goal:

- run `CPython-hosted pcc -> pcc1 -> pcc2 -> pcc3` with self as the native
  emission backend on the supported host.

Tasks:

- Thread the backend choice through the compiled `pcc1` / `pcc2` bootstrap CLI.
- Run stage 2 and stage 3 with the same backend policy used for stage 1.
- Verify `pcc2` and `pcc3` using the same Mach-O signature-normalized compare
  policy as the current LLVM-backed bootstrap.
- If bytes differ beyond signature metadata, classify the source:
  - nondeterministic object/link metadata,
  - nondeterministic symbol or section order,
  - runtime archive ordering,
  - self backend codegen nondeterminism,
  - Python frontend/codegen nondeterminism.

Exit criteria:

- self-backed `pcc2` and `pcc3` verify under the supported-host compare policy.
- Failures are reduced to deterministic, tracked blockers instead of ad hoc
  bootstrap logs.

## Phase 5: Performance And Default Promotion

Goal:

- avoid a correctness-only default flip that creates a permanent performance
  tax.

Tasks:

- Add a self-vs-LLVM bootstrap benchmark runner.
- Measure:
  - stage wall time,
  - produced binary startup/help latency,
  - binary size,
  - representative runtime smoke latency,
  - compile/link geomean over accepted small benchmarks.
- Report geomean and worst-case outliers.
- Keep global default unchanged until broader C workload and performance gates
  meet the main self-backend promotion criteria.

Bootstrap-default acceptance:

- correctness promotion gate is green,
- self-backed stage-1 / stage-2 / stage-3 bootstrap gate is green,
- bootstrap wall time is no worse than `2.0x` LLVM-backed baseline unless a
  documented exception exists,
- produced bootstrap binary startup/help/runtime smoke is no worse than `2.0x`
  LLVM-backed baseline,
- any benchmark slower than `3.0x` is blocking unless explicitly marked as a
  non-bootstrap workload issue.

## Non-Goals

- Do not claim global LLVM replacement from this plan alone.
- Do not require a hand-written Mach-O object writer before the bootstrap
  default experiment; asm-first plus host assembler/linker is acceptable for the
  first supported-host gate.
- Do not silently fall back to LLVM after `self` has been explicitly selected.
- Do not mix Linux x86_64 promotion into this plan; that target has its own
  plan.

## Immediate Task Queue

1. Add `backend` plumbing to `compile_python()` and `compile_python_multi()`.
2. Stop ignoring `--backend` in `pcc/driver/cli_bootstrap.py`; validate it through the
   existing backend resolver.
3. Add a self native-emission branch that consumes Python frontend IR text and
   calls the self backend emitter.
4. Add a focused test proving Python bootstrap `--backend=self` reaches the self
   emitter.
5. Add a minimal Python executable self-backed smoke test.
6. Add a stage-1 self-backed bootstrap runner with LLVM baseline comparison.
7. Record the first unsupported Python IR shape as a minimized self-backend
   regression.
8. Iterate blockers until stage 1 is green.
9. Extend the runner to stage 2 / stage 3 and signature-normalized compare.
10. Add bootstrap performance thresholds before changing any default.
