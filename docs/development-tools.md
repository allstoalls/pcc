# Development tools

Search this index and `scripts/` for the operation you need; inspect the
script's current argument parser before running it. This is an on-demand
locator, not a startup reading list or proof that a tool's claim is qualified.
Do not assume every script supports `--help`.

Prefer an existing tool. Extend it with a regression when it cannot observe
the needed boundary. Reusable probes belong in `scripts/` with focused tests;
keep one-run scratch out of the repository root and source collection paths.
Performance/build runs follow [validation-workflow.md](validation-workflow.md).

| Tool | Use it for |
|---|---|
| `scripts/install_pcc1_toolchain.py` | Bootstrap the initial stable `~/.local/bin/pcc1` from a matching successful Stage1/Stage2 receipt, copied source/runtime and an isolated host-helper environment. Runs an installed native canary before creating the entry; refuses to replace an existing command. This is baseline installation, not release qualification. |
| `scripts/pytest_live_report.py` | Opt-in pytest plugin (`-p scripts.pytest_live_report --pcc-live-report PATH`) that writes incremental JSONL node reports and failure tracebacks, including xdist controller reports, before the final summary. Refuses to overwrite prior evidence. |
| `scripts/pcc_profile.py <pid> [secs]` | Sample a live pcc/pcc1 and rank **self** time by function. Reads the symbol table from the sampled process's own executable, derives the slide from the image `sample` reports, and counts only frames in that image. `--binary` is a *check*: a mismatch is an error, not an override. Follows the pid down to the busiest leaf, so passing a `gtimeout`/`sh` wrapper's pid still profiles the compiler. |
| `scripts/pcc_flamegraph.py <mode>` | Flame graph with **caller** attribution, self-contained SVG + folded stacks. `cpu`/`heap`/`peak <pid>` profile a native pcc1/pcc2 out-of-process (`sample`, `malloc_history`; heap/peak need the target launched with `MallocStackLogging=1`). Native modes normally follow the busiest child; use `--exact-pid` to retain an explicitly selected coordinator, with the same executable-identity checks. `host --argv <pcc cmdline>` profiles host CPython by injecting a `sitecustomize.py` sampler, so the coordinator **and every worker** self-profile with the build's parallelism untouched; add `--memory` for `tracemalloc` bytes-by-traceback. Blocked frames are excluded on both sides so the two graphs share one estimator and can actually be compared. |
| `scripts/pcc_tachyon_aggregate.py <dir>` | Aggregate CPython 3.15 `profiling.sampling` flamegraph HTML across coordinator and worker processes. Reports cross-process self samples by Python file/line/function, frame opcode counts, per-process sample quality, and an optional JSON receipt. Use after a Tachyon `--subprocesses --mode=cpu --opcodes --flamegraph` Stage1 run. |
| `scripts/run_pcc_stage1_build.py` | Build one isolated stage1 arm and emit source-manifest/runtime/compiler receipts consumed by the A/B runner. Use it for both arms so “single variable” is machine-checked rather than asserted after the build. |
| `scripts/run_pcc_stage_ab.py` | Run adjacent alternating source-frozen Stage1 or Stage1+Stage2 pairs under one performance lock. Each arm gets an initially empty writable private pycache; receipts treat user+sys as timed-tree CPU, wall as a paired observation, and coordinator hardware counters as diagnostic only. |
| `scripts/run_pcc_stage2_from_receipt.py` | Run one source-frozen Stage2 from an existing successful Stage1 receipt without rebuilding A/B arms. It verifies the pcc1/runtime/source identities, reuses the Stage A/B process-tree sampler and linkage checks, holds the performance lock, and writes a terminal single-arm receipt. |
| `scripts/run_pcc_compile_ab.py` | Darwin receipt-bound pcc1 compile A/B runner for an **optimization slice**: private compiler/input/runtime snapshots, a common frozen baseline host-helper control, balanced unmeasured warmups, alternating matched inputs, process-group watchdogs, `/usr/bin/time -lp` CPU/RSS/instruction counters, byte/output/linkage checks, and an incremental JSON manifest. Use it instead of hand-running candidate/control pairs. `ACCEPT` exits 0; a valid measured `DENY` exits 2. Its result does **not** prove host→pcc1 versus pcc1→pcc2 bootstrap parity, fixed point, or five-GC equality. |
| `scripts/run_process_tree_sample.py` | Run one long command under the shared performance lock with a process-group watchdog, durable stdout/stderr, 250ms synchronized descendant RSS samples, live progress, optional Darwin launch preflight, and a hard aggregate-RSS circuit breaker. Safety-capped runs fail closed on a one-second process-table deadline; receipts retain full argv and worker-manifest paths for the largest process. Use it when `/usr/bin/time -lp` process-local counters are insufficient for aggregate compiler-worker memory. |
| `scripts/run_pcc_deferred_link.py` | Run a versioned pcc-owned Mach-O link plan after a compiled pcc1 coordinator exits. Bootstrap uses it to prevent the coordinator allocator high water from overlapping the assembler/linker tree; it invokes `pcc_link_macho.py`, never a system-link fallback, and retains a result receipt without deleting plan-supplied paths. |
| `scripts/run_pcc_link_ab.py` | Receipt-bound A/B for the owned Darwin linker. Assembles one frozen `.s` set once, reuses identical `.pco` and archives in balanced control/candidate links, holds the performance lock, records `/usr/bin/time -lp` counters incrementally, runs every output with `--help`, and requires byte-identical images plus source/archive stability. It isolates assembler/linker work; it does not measure self-backend IR-to-assembly emit or prove a bootstrap fixed point. |
| `scripts/pcc_root_elision_sizing.py <module.ll>` | Read-only sizing for allocation-point root elision on the REAL parsed IR/CFG (`parse_self_backend_module`). Knows the three window facts that each produced a wrong number once: readers are `pcc_gc_load_ptr` calls (never plain loads), a re-store ends the window (slots are reused), one dirty path kills. Contract: `tests/python/test_root_elision_sizing_tool.py`. |
| `scripts/pcc_record_inventory.py <module.ll>` | Read-only parse-to-emit inventory for compiler-internal record/container and indexed-kernel projections. Its fail-closed class contract AST-discovers every top-level `self_backend*.py` class, rejects unclassified/stale families, and keeps every concrete class visible to the stage graph. Acquires the performance lock and emits a source-hashed JSON receipt for before/after native-data-plane gates. |
| `scripts/pcc_sample_aggregate.py` | Aggregate/categorize an **already captured** `sample(1)` text file by symbol name. Complements `pcc_profile.py`, which does the capture and address resolution. |
| `scripts/pcc_emit_rank.py` | Rank a frozen Stage2 object-input manifest by fresh pcc emit-worker wall/CPU/instructions/RSS under the performance lock. Emits an incremental manifest and per-item assembly receipts; use it to identify the real medium/safe critical item instead of assuming IR byte size predicts cost. |
| `scripts/pcc_structured_instruction_inventory.py` | Decode frozen indexed-module sidecars and count every AArch64 instruction still using the text assembler. Holds the performance lock and persists source-hashed per-module progress, so packed-instruction migrations can require zero normal-path fallback without a Stage2 run. |
| `scripts/pcc_preload_compare.py` | Compare the current complete class-preload index against two AST-extracted baseline functions on a retained native-exports wire. Requires semantic equality and insertion-order JSON byte equality, rejects input drift or an existing output, and writes source/wire/index hashes plus counts. Host correctness evidence only; not a speed or pcc1 claim. |
| `scripts/replay_pcc_codegen_worker.py` | Replay one retained `codegen_worker.v4` Stage2 manifest with a chosen pcc1. It rewrites only result/artifact paths, restores the receipt-bound Stage2 environment, records identities, and `exec`s through `/usr/bin/time -lp`; wrap it in `run_process_tree_sample.py` for the performance lock, timeout and tree-RSS guard. |
| `scripts/bootstrap_profile_report.py` | Turn `PCC_BOOTSTRAP_PROFILE_DIR` per-stage JSON into phase totals. Use before profiling to learn *which phase* to profile. |
| `scripts/pcc_explain_cache.py` | Why a cache entry missed. First stop for "it rebuilt everything again". |
| `scripts/pcc_explain_fallback.py` | Why a module needed the libpython fallback. |
| `scripts/pcc_ir_diff.py` | Structural IR diff — use instead of `diff` when asking "did my change alter codegen?" |
| `scripts/pcc_passes_explain.py` | What each backend pass did to a function. |
| `scripts/pcc_gc_viewer.py`, `scripts/pcc_trace_viewer.py` | GC state / runtime trace inspection. |
| `scripts/probe_stage1_closure.py` | Is a module inside the no-libpython stage1 closure. |
| `scripts/probe_stage1_closure_on_mode.py --module NAME --mode off\|on\|both --emit-ir-dir DIR` | Select exact tightened-closure modules for standalone fallback attribution. Writes source-hashed IR/error receipts with the existing action/plumbing/target classification; this is frontend IR evidence, not contextual or native execution proof. |
| `scripts/pcc_link_macho.py`, `scripts/pcc_link_elf.py` | Assemble/link a self-backend `.s` with pcc's own toolchain. |
| `scripts/check_layer1_ownership.py` | Enforce that `layer1.py` stays a facade. |
| `scripts/regen_investigations_index.py` | Mandatory after editing `docs/investigations/*.md`. |
| `scripts/pcc_per_op_cost_table.py --out-dir DIR` | Per-operation cost table, pcc-compiled runtime vs CPython: one operation per counted loop, `/usr/bin/time -lp` instructions and wall at N and 2N, `(2N-N)/N` cancels startup, outputs must match. The compass for the per-op runtime gap behind Stage2/Stage1; add a benchmark to `BENCHMARKS` rather than writing a one-off probe. |
| `scripts/distill_investigations.py` | Regenerate `docs/knowledge/` from `docs/investigations/`; `--check` fails when stale. |

For the documentation migration itself,
[`check_agent_guide_migration.py`](../scripts/check_agent_guide_migration.py)
checks the pinned old guide, exact contract moves, section accounting and
local link paths. Its semantic-review boundary is recorded in
[`agents-migration-audit.md`](agents-migration-audit.md).

## Profiling checks

Resolve samples against the sampled executable's own symbols and image slide.
Follow the working child process, not a shell/watchdog wrapper. Separate leaf
self time from cumulative call trees and exclude blocked samples consistently
between arms. Count only the call graph, not its repeated flat summary.
CPython 3.15 sampling must include subprocesses without reducing baseline
parallelism. Track aggregate descendant RSS and platform footprint; pcc's own
allocator means libmalloc history usually shows arena refills, not individual
Python objects. Use source, lowered IR and a controlled experiment to test the
mechanism suggested by the profile.
