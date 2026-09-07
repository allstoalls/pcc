# AGENTS.md

This file is for humans and AI agents working in this repository.

## Read Next

Startup route for active goal work and direct human task intake:

1. Read this file first for repository rules and safety constraints.
2. Read `docs/knowledge/denied-experiments.md` before proposing any fix. Every
   line there is a change somebody already wrote, measured and disproved.
3. Read `docs/knowledge/README.md` for the rest of the distilled knowledge
   (confirmed root causes, symptom routing, dated handoffs).
4. Take work from GitHub issues in the repository you are changing
   (`allstoalls/pcc`, `allstoalls/pcc-gui`, `allstoalls/pcc-gateway`); use
   `gh issue list` and `gh issue view`. There is no task-board file: it retired
   on 2026-09-06 and its unfinished rows became issues (see
   `docs/goal/README.md` and `docs/task-board-migration-2026-09-06.md`).
5. Use `docs/investigations/INDEX.md` to find relevant prior investigations
   before opening or continuing a non-trivial bug. Then **read the matching
   file end to end, including every `[DENIED]` verdict and every "did not
   help" note, before writing code** — those sections record fixes already
   written, measured, and disproved, and re-deriving one wastes a full
   rebuild/measure cycle on a change known not to work. See the Investigation
   Workflow section below for what this rule keeps catching.
6. **Task-conditional, required — not optional reference.** The moment the task
   becomes *debugging a failure*, read and follow
   [`docs/debugging-playbook.md`](docs/debugging-playbook.md) before guessing.
   The moment you *open or continue an investigation*, read and follow
   [`docs/investigation-workflow.md`](docs/investigation-workflow.md) first.
   These two were split out of this file to stay under the context budget;
   the split lowered their resident-in-context cost, **not** their authority.

## Dependency ownership contract (maintainer directive, 2026-09-07)

**Host pcc depends only on CPython and the Python standard library. Native
pcc1 requires no external toolchain or language-runtime dependencies.** This
contract applies to the entire supported workflow, including C processing,
runtime construction, optimization, object emission, linking, cache checks,
bootstrap and installed use. It overrides historical implementation notes
below that describe external execution owners.

- Remove LLVM, libLLVM, llvmlite, cc/clang/gcc and equivalent dependencies
  from the product paths. Host pcc must also remove third-party Python
  dependencies such as pycparser, PLY and cffi; internal pcc implementations
  may use CPython and its standard library only.
- pcc1 must not invoke host Python/CPython, load libpython, or require
  externally installed compiler, preprocessor, assembler, linker, archiver
  or signing tools. These operations must use pcc-owned native code, compiled
  into pcc1 or supplied as part of the pcc toolchain. Moving an external
  dependency into a subprocess, build script, cache helper, bundled LLVM
  library or compatibility wrapper does not remove the dependency.
- C is part of this contract: preprocessing, parsing, semantic analysis,
  optimization, code generation, assembly and linking must be owned by pcc.
  C inputs, runtime builds and extension builds are not exceptions. Do not
  route C processing to host cc or LLVM, or label that route self-hosted.
- Reuse and complete pcc's existing implementations. A Python-authored
  algorithm that still uses LLVM to parse, verify, optimize or emit its IR
  has not completed this migration. Host execution of pcc's own code satisfies
  the host-pcc contract only when it uses the standard library; the same host
  subprocess remains an unfinished dependency in pcc1.
- Existing external routes are migration defects to track and eliminate,
  not evidence that those dependencies are necessary. Fail explicitly at an
  unimplemented boundary; never silently restore an external owner to make a
  test pass or improve a benchmark. Preserve Python/C semantics and all GC
  contracts while completing the native path.
- LLVM/cc measurements may be retained as explicitly labeled external
  reference experiments. They cannot be required by the shipped compiler or
  counted as proof of pcc1-owned optimization. Prove the owned route with
  dependency-denial checks and execution of its emitted programs; checking
  only dynamic library links or a successful compiler exit is insufficient.

The target OS's kernel/platform ABI is the execution boundary, not a license
to rely on an external compiler or language runtime. This is the required
end state, not a claim that every current implementation already meets it.

## Working agreement

There is no single-file task queue and no goal-mode protocol. Work is tracked
as GitHub issues; durable knowledge is tracked as documents. The task board
(`docs/goal/task-board.yaml`, 490 rows) and its per-slice evidence directory
(`docs/goal/evidence/`, 924 files) were removed on 2026-09-06 because they had
become a running log that every session had to re-read. Their content lives on:
the 241 unfinished rows are issues (labels `priority:*`, `status:*`,
`task-board`), the last board and every evidence file remain in git history at
commit `2574f585`, and the protocol documents are in `docs/archive/goal/`.

### Where each kind of information belongs

| Kind | Home |
|---|---|
| Work to do, and its state | GitHub issues in the repository being changed |
| What was already tried and disproved | `docs/knowledge/denied-experiments.md` (generated) |
| Established failure mechanisms | `docs/knowledge/confirmed-root-causes.md` (generated) |
| Which investigation covers a symptom | `docs/knowledge/symptom-routing.md` (generated) |
| How a verdict was reached, in order | `docs/investigations/<slug>.md` |
| Uncommitted state and blockers at a handoff | `docs/knowledge/<date>-session-handoff.md` |
| Authoritative bootstrap/fallback state | `tests/bootstrap_gate_baseline.json`, `tests/fallback_baseline.json` |

Regenerate the three generated pages after editing any investigation; a test
(`tests/test_knowledge_pages_are_current.py`) fails when they are stale:

```bash
env -u LC_ALL uv run python scripts/distill_investigations.py
```

### Taking and finishing work

- **Pick up an issue** rather than inventing scope. `gh issue list --label
  priority:P0`, then `gh issue view <n>`. A migrated issue carries the old
  row's open boundary, exit criteria and required gates verbatim; those are
  still the definition of done.
- **New actionable work becomes an issue** as soon as it is described, so it is
  visible to the next session: `gh issue create --title ... --body ...`. Do not
  leave it only in chat or in a document.
- **Report honestly.** An issue is closed when its listed gates prove its full
  claim. If part of the work is done, say which part and leave it open. The old
  board's `DONE_WEAK` state exists to be avoided, not reproduced.
- **Write the durable half.** When a slice teaches something that would cost
  another session a rebuild cycle to rediscover, put it in the investigation for
  that symptom (a `## Update` block, including any `[DENIED]` verdict) so the
  generated pages pick it up. Do not create a new per-slice evidence file tree.
- **A handoff is a document, not a chat message.** Before a long pause, write
  `docs/knowledge/<date>-session-handoff.md`: uncommitted state, blockers with
  their minimal reproducer, and decisions a reader cannot infer from the diff.

### Continuation requests

`继续`, `continue`, `continue the work` and equivalents mean: look at the open
issues for the repository you are in, continue the one already in progress
(or the highest-priority ready one), and keep going across issue boundaries.
Stop when the work is done, when a human says stop, or when you are blocked on
something only a human can decide. There is no `resume`/`finish-check` command
to run and no exit-code gate on finalization.

## Project Intent (north star — read before changing direction)

> This section is the top-level design contract. It exists to keep autonomous
> work aligned: when a change would trade away one of the obligations below for
> a local win — a faster benchmark, a greener gate, a smaller diff, a passing
> bootstrap by rewrite — **stop and surface the tradeoff instead of taking it
> silently.** This section is the *why*; `docs/archive/goal/goal-prompt-through-2026-09-06.md` is the *how*
> (tracks, gates, claim hygiene, prohibitions). If you find yourself weakening
> Python semantics, mislabeling a mode, or special-casing a package to make
> progress, you are off the north star — re-read this section.

**Thesis.** pcc exists to give Python a native, auditable, self-hostable,
no-libpython execution path. The goal is **not** merely to make selected Python
programs faster — it is to make Python execution *ownable*: compiled,
inspectable, self-hostable, package-aware, runtime-extensible, and honest about
every fallback boundary. pcc treats performance as a **consequence of proven
semantics, never a license to weaken Python behavior.**

**What separates pcc from a Python accelerator.** Six things. Without them pcc
is just another speedup tool; with them it is a system rebuilding Python
*execution ownership*. Do not let any of these decay into decoration:

```text
1. pcc1 -> pcc2 -> pcc3 self-hosted fixed point
2. five-GC comparative runtime (refcount/cycle, incremental, concurrent,
   generational, relocating) — a research program, not one collector
3. opt-in value model — identity-free immutable payloads for hot paths, with no
   theft of ordinary-class semantics (Java's Project Valhalla is a conceptual
   reference only, not pcc's brand or design constraint)
4. self-backend owns execution (LLVM may only be an external reference)
5. long-running runtime efficiency (pause / RSS / throughput / fragmentation
   over time, not single-shot compile+run speed)
6. complete Python execution ownership — implement every missing surface in
   pcc and remove every CPython/libpython/LLVM/host/C-owner or hidden fallback;
   until a surface is implemented, fail closed with an explicit capability
   diagnostic rather than silently changing execution owner
```

**The fixed point is more than a byte compare.** It is evidence that pcc's
Python semantics, runtime, codegen, object model, backend, and diagnostics are
coherent enough to reproduce themselves:

```text
pcc0/host -> pcc1     pcc can produce a compiler
pcc1      -> pcc2     the produced compiler can reproduce the compiler
pcc2      -> pcc3     stable pcc2/pcc3 == a self-hosted fixed point
```

**Seven obligations.** Each is operationalized by a track + gates in
`docs/archive/goal/goal-prompt-through-2026-09-06.md`; the one-line form here is the guardrail, and the
parenthetical is where it is actually enforced:

```text
1. Compatibility must be mode-labeled. A claim must say which mode produced it:
     host pcc != pcc1   |   cpython-compat != pcc-native
     libpython != no-libpython   |   LLVM-backed != self-backed
     stage1 != pcc1->pcc2->pcc3 fixed point
   (`docs/archive/goal/goal-prompt-through-2026-09-06.md` §0.10 claim hygiene, §9.2 mode boundaries)

2. Performance must be proven. C-like claims require IR-shape evidence + runtime
   benchmark + a slow path that preserves Python semantics when assumptions fail.
   pcc does not claim arbitrary dynamic Python becomes C-speed — only the parts
   whose semantics are stable enough to lower natively. (C-track, §16)

3. Ecosystem support must be generic. NumPy / PyTorch / pandas / Arrow / SciPy
   are integration targets, never compiler special cases. No `if package ==
   "numpy"`; fix the reusable mechanism (install/import/ABI/buffer/capsule/
   build-surface) and regress the generic feature. (B-track, §9.1, §14)

4. Self-backend must become a first-class execution root, not a forever-LLVM
   dependency. No silent fallback to LLVM after --backend=self. (S-track, §10)

5. The pcc1/pcc2/pcc3 fixed point is a contract. Differences are *classified*
   (semantic / IR-text / class-layout / object-model / backend nondeterminism /
   link metadata / perf-only / diagnostic), not patched around. pcc2/pcc3
   stability is a core correctness signal. (§0.10, §19.2)

6. Runtime design is part of the research goal. The five GC backends are a
   comparative program; none may win by weakening finalizers, weakrefs,
   resurrection, suspended coroutine frames, scheduler queues, C-extension
   refs, or value payloads. Measure efficiency as a long-running property.
   (G-track/§12, T-track/§13)

7. The value model is the performance bridge, not a syntax gimmick. Ordinary
   classes keep identity (id / is / weakref / __dict__ / mutation / subclass /
   finalizer / dynamic attrs). Value classes are opt-in, identity-free payloads
   with explicit boxing/unboxing, identity-escape diagnostics, GC tracing of
   pointer-bearing payloads, and self-backend aggregate/scalar ABI. (The concept
   is the obligation; "Valhalla" is only the reference it was distilled from.)
   What pcc borrows from Valhalla is the PROJECTION model (semantic type vs
   physical representation; value/object projection; boxing bridge; optimization
   never changes semantics) — NOT Java's fixed-width `int` wrap. This applies to
   `int` itself: `int` is a Python arbitrary-precision SEMANTIC type with a value
   projection (tagged small-int lane) and an object projection (boxed bignum);
   value-lane overflow must deopt/promote, never wrap. Raw machine integers are
   the EXPLICIT `pcc.i64`/`pcc.u64` type (where wrap/trap/checked/saturating is
   written in the type), or a proven-in-range internal optimization — never the
   silent default meaning of `int`. (value model / V-track, §11)
```

**One mission, not two.** Industrial failures are research data (import failure
-> C-API/ABI gap; Linux deploy failure -> self-backend target gap; long-running
service regression -> GC/runtime benchmark; perf miss -> value-model gap), and
research artifacts are industrial trust (fixed-point bootstrap -> reproducibility;
five-GC matrix -> runtime credibility; valueclass benchmarks -> performance
proof; package ABI reports -> ecosystem trust). The industrial thesis ("adopt
pcc where native artifacts, no-libpython deploy, package-aware diagnostics, and
hot-path specialization beat CPython") and the academic thesis ("a
Python-authored compiler self-hosts into a no-libpython fixed point while
exposing a disciplined runtime laboratory") reinforce each other. **Every claim
must say exactly what it proves and what it does not prove.**

**Accelerator execution is an extension of the ownership thesis, not a sixth
mission — and not the overclaim it is easy to make.** The repo already contains
a real GPU/Metal thread (`pcc/kernel_ir/`, `pcc/gpu_gc/`, `pcc/dist/`) that the
five pillars above did not name; this paragraph gives it an honest home so the
intent stops lagging the code. It belongs to the same "ownable execution"
thesis: the target is **native accelerator execution ownership** — a
host/device-split kernel IR, no-libpython device launch, and GC-aware external
resource lifetime — with **TVM/TIRx and TileLang used as oracles/reference
shapes, never as runtime owners** (the same relationship the value model has to
Valhalla and the self-backend has to LLVM). What is actually proven today is
narrow and must be stated at its claim level (`pcc/kernel_ir/gpu_claims.py`
levels 0-6): a real Metal kernel-IR path with on-device result proofs for small
fixed-shape kernels, **local-machine and hardware-gated**. What is **not**
provided: whole-program GPU, executing `import tvm` / `import tilelang`
(only a fail-closed parser of a TileLang-*shaped* DSL subset exists), external
framework interop, and any real distributed/ds4 runtime (`gpu_gc`/`dist` are
CPU oracles). This thread **must not displace the self-host -> 5-GC -> value ->
runtime-efficiency spine**: it is M5 breadth, and calling a GPU slice "done"
requires the same mode-labeled claim hygiene as every pillar.

**Runtime layering: the production runtime is authored in pcc-Python, including
the low-level kernel.** The long-term goal is not a smaller hand-written C
runtime. Allocation, object headers, atomics/refcount barriers, platform
syscalls, threading primitives, dynamic loading, extension ABI entrypoints,
safepoints/stack maps, and all five GC implementations migrate to a strict
freestanding pcc-Python subset and are compiled by pcc into native code. The
machine boundary is compiler-owned raw-memory/syscall/atomic intrinsics and a
specified ABI, not a permanently hand-maintained C kernel. Existing C and
vendored libc sources are transition implementations and differential oracles;
they are not the final production dependency. Distinguish these layers:

```text
compiler intrinsics   KEEP: raw memory, atomics, syscall/host-ABI entry and
                      machine operations; no Python object semantics.
freestanding pcc-Py   GROW: allocator, threads, safepoints, GC, libc-like
                      substrate and ABI shims; no heap/boxing/GC dependency
                      while bootstrapping those facilities themselves.
semantic pcc-Python   GROW: list/dict/str/dunder/exception/import semantics.
C/libc sources        REMOVE from the production dependency after differential
                      and fixed-point gates; retain only as attributed oracles.
```

This is stronger than no-libpython: the final Linux zero-libc claim requires no
production C/libc runtime dependency either. Darwin may still enter the OS
through named libSystem ABI calls and must not be labeled zero-libc. The
**5-GC Production Equality Rule** (`docs/archive/goal/goal-prompt-through-2026-09-06.md`, G-track) still
requires every backend to consume ONE slot-based trace/update contract
(`py_obj_visit_slots` / `py_obj_update_slot` / root + frame + native-handle
registration). During migration the C oracle and pcc-Python implementation must
stay differential-equal; completion removes the C implementation from the
production link rather than preserving two implementations indefinitely.

## Project Summary

`pcc` is two compilers and one runtime in one repo:

1. A **C frontend** with historical `pycparser`/LLVM/`llvmlite` routes that
   must be removed under the Dependency ownership contract above. It runs real
   third-party projects (Lua, SQLite, PostgreSQL `libpq`, zlib, lz4, zstd,
   PCRE, OpenSSL, readline, nginx).
2. An experimental **typed-Python frontend** (`pcc/py_frontend/` +
   `pcc/py_runtime/`) used for the self-host / bootstrap track.
3. A **runtime** (`pcc/py_runtime/src/*.c` and pcc-Python ports under
   `pcc/py_runtime/py/`) with five pluggable GC backends.

Most fixes in this repository are not parser bugs; they are **semantic** bugs
that only show up when expressions are combined, lowered to LLVM IR, and then
exercised by real programs. The C-side and Python-side both follow this
pattern.

The fastest way to get useful results:

1. Reproduce with a tiny program when possible.
2. Compare against a known-good reference (native compiler for C; CPython for
   Python; `llvmlite` for `llvm_capi` parity).
3. Add one small regression test and one realistic integration confirmation.
4. Do not consider a real-project bug "fixed" until the minimized regression
   exists in `tests/` and passes without the project harness.

The fastest way to create expensive regressions is the opposite:

1. touch `c_codegen.py`, any `pcc/py_frontend/codegen/*_lowering.py` mixin, or
   any `pcc/py_frontend/codegen/native_*.py` file with a broad speculative
   change
2. skip the smallest reproducer
3. delay regression checks until after several edits have stacked up

Do not do that here.


## Environment Rules

- Use `uv run ...` for Python entrypoints. Do not rely on bare `python`; local
  `pyenv` state may not match the repository.
- Use `env -u LC_ALL` in command examples. It is required for Codex because
  Codex may set `LC_ALL=C`, which can break Python locale handling; it is
  harmless for other users. See https://github.com/openai/codex/issues/14723.

  ```bash
  gtimeout 120s env -u LC_ALL uv run pytest -q -x
  env -u LC_ALL uv run pcc hello.c
  ```

- While debugging one failure, prefer `-n0` so xdist does not hide ordering or
  temp-file problems. This is a debugging rule, not a blanket validation rule.
  For independent bootstrap / GC matrix validation, do **not** serialize the
  whole matrix with `-n0`; let pytest-xdist use its configured workers, or run
  one explicitly chosen backend file with `-n0` when localizing that backend.
  A five-backend bootstrap matrix should normally look like:

  ```bash
  gtimeout 1800s env -u LC_ALL uv run pytest -q -x -m integration tests/python/gc/test_pcc_bootstrap_full_gc*.py
  ```

  Use the single-backend form only for focused diagnosis:

  ```bash
  gtimeout 360s env -u LC_ALL uv run pytest -q -x -n0 -m integration tests/python/gc/test_pcc_bootstrap_full_gc4.py
  ```

  Long bootstrap gates must provide meaningful progress updates: name the exact
  command, backend/stage if known, elapsed time, and whether new output has
  appeared. If a long gate is interrupted or times out, immediately check for
  and terminate leftover `pytest`, `bootstrap.sh`, `pcc`, `pcc1`, `pcc2`, and
  `pcc3` children. A run without a final pytest summary is not green evidence,
  no matter how many progress dots were printed.
- **Every pytest invocation must stop at the first failure or error.** Pass
  `-x` (or the exact equivalent `--maxfail=1`) on every pytest command,
  including focused, default, integration, bootstrap, and GC gates. Do not let
  a suite continue after the first `F` or `E`. The only exception is an
  explicit human request in the current conversation to collect multiple
  failures; a task document, historical command, or desire for broader
  evidence is not enough to override this rule. With xdist, stop scheduling
  new work at the first reported failure and preserve the first failing node's
  traceback before doing anything else.
- **Never spend a long pytest run producing dots-only evidence.** Before any
  pytest command expected to run longer than 60 seconds, arrange durable,
  incremental failure evidence outside pytest's end-of-session summary. At a
  minimum, run with node IDs visible (for example `-vv`) and persist the live
  output to a named artifact; for a diagnostic run also use `-x --tb=short` so
  the first current failure is reported promptly. A broad run that may reach
  its watchdog must use an incremental report hook/logger that records each
  failed node ID and traceback as the report arrives. If that facility is not
  available, shard the suite into commands that fit their watchdog instead of
  launching the broad run. `F`/`E` counts and percentage progress without node
  IDs and tracebacks are not useful failure evidence and must never be reported
  as if they were. Leave enough outer-watchdog grace for pytest to flush its
  report and summary; if the measured suite cannot finish within that budget,
  stop and shard it before rerunning rather than merely widening the timeout.
- **Broad suites are final evidence, never a discovery mechanism or a way to
  decide what to implement next.** Do not start the default suite, full
  integration suite, five-GC matrix, or a full `pcc1 -> pcc2 -> pcc3` chain
  until the implementation slice being closed has met all of these readiness
  conditions: its known code changes are complete; its focused regression has
  passed with `-x`; every agent editing an overlapping subsystem has reported
  source-stable; no relevant source or generated artifact is changing; and the
  exact frozen source identity plus expected time/cache envelope has been
  recorded. If any condition is false, continue implementation or run one
  focused diagnostic only. Never launch a broad gate merely because a task
  card lists it, because progress feels slow, or because accumulated edits need
  "a general check." After readiness, build bootstrap stages in dependency
  order (`pcc0 -> pcc1`, then `pcc1 -> pcc2`, then `pcc2 -> pcc3`) and stop at
  the first failed stage; do not hide stage causality inside an unrelated full
  pytest run.
- **A completed tool call is not ongoing work.** Once no shell/tool/agent job is
  running, send a concrete progress update or end the turn within 60 seconds.
  Do not remain silent in model-side analysis; if the next action is not ready,
  report the exact completed state and yield. A multi-hour `Model interrupted
  to submit` interval with no live child process is an agent stall, not task
  progress, and must never be described as compilation or investigation time.
- **Do not use the full five-GC bootstrap matrix as a diagnostic loop.** Any
  change under `pcc/` invalidates the content-addressed bootstrap source hash
  and can force five cold `pcc1 -> pcc2 -> pcc3` chains. Before launching the
  matrix, read the routed bootstrap-performance/timeout investigations, inspect
  existing stage profiles and success manifests, and measure one explicitly
  chosen backend or an emit-only/profile probe. If one backend exceeds its
  documented budget, or the projected cold matrix cannot fit its outer
  watchdog, optimize/localize that regression first. Do not repeat the matrix
  or widen its timeout to compensate. Run the full matrix once, only after the
  focused fix and scheduler tests are green, as final cross-backend evidence.
- Use ripgrep (`rg`) or your agent's built-in code search for source discovery.
- Do not leave temporary `.c`/`.py` files inside real project directories.
  Directory-based source collection can accidentally compile them.
- **Never drop scratch files in the repo root.** When you need a throwaway
  artifact, in order of preference: (1) write a real test under `tests/` —
  that captures the case as a regression and survives; (2) use an existing
  fixed build/cache directory (e.g. the per-test artifact dir under
  `~/.cache/pcc/test-artifacts/`, or `cache_dir` in the function you're
  working on); (3) only if neither fits, use `/tmp/`. The top-level
  directory is never the right place — it pollutes `git status`,
  pre-commit hooks pick it up, and other agents see the noise.
- **Every Bash invocation must have an explicit timeout. No exceptions.**
  This applies to **all** runs — `uv run pytest`, `uv run pcc`,
  `scripts/bootstrap.sh`, `/tmp/<name>` probes, LLDB sessions, anything
  that forks a child process. The default is a small timeout you expect
  the command to finish well under; **only widen it when you can name a
  specific reason** the run will legitimately take longer (e.g. "the current
  9517-case non-integration suite reached only 93% at 900s on a cold
  current-source run → use 1200s", "stage1 self-host bootstrap is ~4–5 min →
  use 360s"). "I'll just let it run" is not a reason. Re-measure these
  envelopes after build/cache changes; an old shorter estimate is not a reason
  to repeat a gate that cannot produce a final summary.
  Without a timeout, a hung collection / codegen infinite loop / runaway
  probe silently burns the foreground turn and can leave zombie children
  pegging CPU for hours (this has happened — ~120 CPU-hours lost once).
  Before ending a session, `ps aux | grep <your-probe-name>` and `kill`
  any leftover children. Prefer the Bash tool's `timeout` field when
  available. When shelling out on macOS, do not assume GNU `timeout`
  exists; use `gtimeout` only if it is installed, or use a wrapper that
  keeps a parent watchdog alive, forks the target into its own process
  group, and kills that process group on expiry.
- **Do not use `perl -e 'alarm shift; exec @ARGV' ...` or any
  alarm-then-`exec` wrapper as a timeout for heavy commands.** That pattern
  has already failed in this repository after `exec`, leaving an already
  exec'd `pcc1` running for 11+ minutes. A valid fallback timeout wrapper
  must keep the watchdog parent alive, create a child process group
  (`setpgrp`/`setsid`), and on expiry send `TERM` then `KILL` to the child
  process group. After any timed-out bootstrap/compiler run, verify with
  `ps` that no `pcc`, `pcc1`, `pytest`, or named probe child survived.
- **Never `git revert`, `git checkout -- <path>`, `git restore`, `git reset
  --hard`, `git clean -f`, `git stash`, branch switches that discard local
  edits, or any `--force`/`--hard` flag that drops uncommitted state — unless
  the user explicitly asks for it.** Multiple agents and humans share this
  working directory; a unilateral rewind silently deletes another agent's
  in-flight work that has not yet been committed. If you believe a previous
  change is wrong, leave it in place and discuss with the user, or stage a
  *new* commit that supersedes it.
- **Do not investigate or police other sessions through Git history, commit
  timestamps, reflogs, process-CWD inspection, or similar heuristics.** The
  shared-working-directory warning is a preservation rule, not an exclusivity
  requirement. Treat the current filesystem/worktree as authoritative, re-read
  a target before applying a narrow patch, and continue unless an actual
  overlapping edit conflict prevents safe progress. A request to work solo
  means do not spawn or contact subagents; it does not require proving that no
  other human or process can write to the repository. Source-stability checks
  required before broad gates may compare relevant file identities, but must
  not turn into author attribution or session surveillance.
- **Do not perform file-overwrite operations via git (including revert/restore
  checkout/reset --hard/clean/stash or equivalent) unless explicitly requested
  by the user.** This applies to all working-tree files touched in this
  repository.
- **Do not `git commit` unless the user asks.** During investigation you may
  *describe* a sequence ("commit investigation file → run experiment → revert
  → record outcome → commit again"), but you may not run the commits without
  explicit user approval. The Investigation Workflow's "save before each
  experiment" idiom is a prescription for the user to follow, not for the
  agent to execute autonomously.
- **Do not delete documents, plans, investigations, tests, or project
  directories unless the user explicitly names the files/directories to
  remove.** Treat `rm`, scripted deletion, and bulk cleanup of docs as
  destructive operations, even when git could recover them.


## Autonomous Convergence Guardrails

These rules apply to long-running optimization, migration, bootstrap, and
task-board work.  They prevent local progress from being mistaken for goal
completion.

- **A requested conceptual closure is binding.** If the human says to finish a
  concept, remove every hot-path instance of that concept before closing the
  row.  A remaining representation may be called cold/diagnostic/unsupported
  only when all four facts are proven: it is unreachable on the supported
  normal path, a counter or inventory records zero normal-path uses, its lazy
  adapter has an exact regression test, and the task evidence names it.  Low
  Amdahl share, construction-only lifetime, or inconvenient implementation
  size does not turn an unfinished hot representation into completed work.
  A partially satisfied issue stays open, with the remainder written down.

- **Match optimization scale to the goal gap.** Before selecting a performance
  candidate, record the end-to-end gap, the candidate owner's measured share,
  and its Amdahl ceiling.  When the goal gap is at least 1.5x and three
  consecutive candidates each measure below 5% or have a ceiling below 10%,
  stop selecting adjacent helpers.  Re-profile the complete path, write the
  architectural owner and a vertical slice, and resume only with work capable
  of changing that owner.  A structural closure task may still remove a small
  family, but must not be presented as the performance solution.

- **A gate must execute the boundary it is cited for.** `--emit-llvm` proves
  frontend emission only; it does not prove self-backend verification,
  assembly, linking, startup, or execution.  Before accepting a closure gate,
  inspect the relevant generated function and reject `strict.nolib.stub`,
  unavailable placeholders, or a smoke input that omits the changed shape.
  A pcc1 claim needs a pcc1-compiled feature canary through `-o` and execution,
  or an explicitly equivalent direct worker/verifier replay.

- **One expensive failure must close the whole failure class before retry.**
  After a Stage1/Stage2/bootstrap run exposes an ABI, projection, ownership,
  or helper-shape defect, scan the complete changed subsystem for the same
  pattern, add a source-shape or behavioral ratchet, and run the cheapest real
  execution-boundary gate.  Do not spend another long build merely to discover
  the next occurrence of the same pattern.  If a wrapper swallows stderr,
  replay the failing worker directly before editing.  Do not assume a
  repository script supports `--help`; inspect its parser/usage first.

- **Acceptance thresholds are claim-specific, not a universal 1.05 rule.** A
  speed claim needs controlled runtime evidence.  A required representation
  migration may be retained below 1.05 only when output/diagnostics are exact,
  deterministic CPU/instruction/memory signals show no meaningful regression,
  and it removes a named architectural debt.  Stable regressions are denied no
  matter how elegant the representation is.  Record structural and speed
  claims separately.

- **Patch repeated shapes with counted, enclosing context.** Before changing a
  repeated initializer/helper pattern, use `rg` to enumerate every match and
  inspect their enclosing functions/classes.  The patch context must identify
  the intended owner; after applying it, re-enumerate matches and run
  `git diff --check`.  A successful `apply_patch` response does not prove it
  changed the intended occurrence.

- **Do not explain a regression as a correctness tax without attribution.**
  A slower stage may be called required correctness work only when a
  same-source phase receipt identifies the added work and a correctness test
  proves it was previously omitted.  Otherwise it is an unexplained
  regression and remains an active investigation.

- **Artifact and handoff claims require readback.** Before telling the human a
  file, receipt, binary, report, or handoff exists, check the exact path and
  read enough of it to verify identity/status.  Intended output paths and
  interrupted commands are not artifacts.

- **Completion requires a fresh contradiction audit.** Immediately before
  closing an issue, rerun its inventory command and enumerate every remaining
  family, fallback, compatibility adapter, skipped gate, and open boundary.
  Any item that contradicts the issue title or its exit criteria keeps the
  issue open.  A milestone summary or one successful finite slice does not
  authorize stopping while listed work remains.

- **Native-data-plane growth is fail-closed.** Every new top-level class in
  `pcc/backend/self_backend*.py` must be classified by
  `scripts/pcc_record_inventory.py` as a native value/arena, semantic or phase
  shell, diagnostic projection, or target/control class.  An unclassified or
  stale entry is a test failure, and every concrete classified class remains
  visible to the stage graph so category choice cannot hide a reachable
  object.  Direct diagnostic-record constructor sites are also count- and
  owner-registered; adding a call in an existing or new function fails unless
  its parse/diagnostic/legacy/oracle policy is named.  New diagnostic adapters
  must increment their family counter and remain zero on the supported normal
  path.  Run
  `tests/python/test_pcc_record_inventory_tool.py` for every changed
  self-backend record family; do not update the contract merely to silence the
  gate without naming the representation and normal-path policy.


## Interrupting tasks

A short operational request received while another task is unfinished is an
interrupting subtask, not a replacement.

- Push it onto the active task stack.
- Complete and verify the interrupting task.
- Automatically resume every unfinished parent task.
- Do not end the turn until the parent task is answered.
- Replace or abandon the parent task only when the user explicitly says:
  “stop”, “cancel the previous task”, or “switch to this instead”.
- Before the final response, list all active requests and verify each is complete.


## Repository Map

| Path | Role |
|---|---|
| `pcc/pcc.py`, `pcc/cli_core.py` | CLI entrypoints |
| `pcc/cli_bootstrap.py` | Bootstrap-stage CLI used by `pcc1`/`pcc2`/`pcc3` |
| `pcc/api.py` | `build(...)` / `module(...)` Python API for C |
| `pcc/project.py` | Directory collection, `--sources-from-make`, TU selection |
| `pcc/evaluater/c_evaluator.py` | C preprocess/parse/IR/optimize/execute |
| `pcc/codegen/c_codegen.py` | Main C semantic lowering (~most C bugs land here) |
| `pcc/parse/c_parser.py` | C parser (PLY-based; bump cache version on grammar/lexer changes) |
| `pcc/py_frontend/` | Python typed-frontend; type infer + native lowering |
| `pcc/py_frontend/codegen/layer1.py` | Thin facade for Python frontend lowering |
| `pcc/py_frontend/codegen/*_lowering.py` | Main Python lowering mixins; add focused behavior here instead of growing `layer1.py` |
| `pcc/py_frontend/codegen/native_*.py` | Native module lowering (gc, threading, asyncio, system, os, math, modules, weakref, ...) |
| `pcc/py_runtime/src/*.c` | Native runtime in C (objects, GC, threads, exceptions, ...) |
| `pcc/py_runtime/py/*.py` | pcc-Python runtime ports (mirror of C; for self-host) |
| `pcc/py_runtime/include/py_runtime.h` | Public runtime header: object header, type tags, `PCC_GC_KIND_*` enum |
| `pcc/py_runtime/src/py_internal.h` | Runtime-internal object layouts such as `PyClassObject` |
| `pcc/llvm_capi/` | In-repo IR builder plus legacy LLVM bindings; external bindings/fallbacks are migration debt |
| `pcc/backend/` | Experimental LLVM-free self backend (AArch64 Darwin, x86_64 Linux subsets) |
| `pcc/kernel_ir/` | Kernel-only GPU IR, TIRx-like freeze, Metal finalization, launch packages, HMM/fence, and DLPack/tensor ownership slices |
| `pcc/gpu_gc/` | GPU-GC metadata/oracle and external-resource lifetime seam; CPU-only unless a focused gate proves runtime integration |
| `pcc/dist/` | Local-only distributed session, transport, collective, sharding, and KV oracles; not localhost TCP or multi-Mac execution by default |
| `pcc/extern/`, `pcc/unsafe/` | Python→C extern decls; compiler-recognized intrinsics |
| `utils/fake_libc_include/` | Fake libc headers (host ABI / decl mismatches surface here) |
| `tests/` | Unit, parity, integration regression coverage |
| `tests/py_corpus/phase*/` | End-to-end Python corpus retained from the earlier phase taxonomy. These tests still run; the phase framework is no longer the active task board. See current priorities in the repository's GitHub issues. |
| `tests/python/test_self_host_oracle_diff.py` | Core Python semantic oracle / pcc1-pcc2 parity ratchet |
| `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` | Full stage1→stage2→stage3 self-backend bootstrap gate, one file per GC backend (shared helpers live in `tests/python/test_pcc_bootstrap_full.py`) |
| `tests/bootstrap_gate_baseline.json` | **Authoritative bootstrap state** (Issue 1 closure evidence) |
| `tests/fallback_baseline.json` | **Authoritative no-libpython fallback state** |
| `scripts/bootstrap.sh` | macOS arm64 three-stage bootstrap entry |
| `scripts/pcc_multi.py` | Experimental multi-file Python entry |
| `projects/lua-5.5.0/` | Real-program stress target |
| `docs/refs_docs/gc-research/` | Reference impls for the 5 GC backends (Lua, Go, OCaml, ZGC, CPython) |
| `docs/knowledge/` | Distilled decision pages (denied experiments, confirmed causes, symptom routing) and dated handoffs |
| `docs/archive/goal/` | The retired goal protocol and its last state snapshot |
| `docs/design/pcc-gpu-next-work.md` | Durable GPU / TVM-TIRx / Metal / GPU-GC / distributed / ds4 route contract, reference pins, and GPU claim levels |
| `docs/investigations/INDEX.md` | Index of investigation docs; keep it current when investigation docs change |
| `scripts/distill_investigations.py` | Regenerate the `docs/knowledge/` decision pages from the investigations |


## Dev Tools — check here before writing a probe

`scripts/` already holds ~55 tools. Several were re-derived from scratch by
successive agents because nothing indexed them, which costs a rewrite *and* the
tokens to think it through again. **Run `ls scripts/` before writing any
throwaway probe**, and add a row here when you add a tool worth keeping.

**Prefer the repository's own toolset as the default execution path.** Before
hand-writing an A/B harness, profiler, cache inspector, IR differ, bootstrap
reporter, or one-off parser, inspect this table and `scripts/` and use the
closest existing tool. If the tool is almost sufficient, extend it generically
instead of cloning its logic into a throwaway script. If a repository tool is
wrong, misleading, or cannot observe the required boundary, first capture the
defect with the smallest focused regression, then fix the tool and use the
fixed version for the investigation. Do not silently work around a tool bug and
report ad-hoc output as durable evidence. Keep additions bounded and reusable,
index worthwhile new tools here, and preserve the same timeout, live-progress,
mode-label, and artifact rules that apply to compiler/test commands.
When writing a script-shaped tool, probe, benchmark harness, evidence parser,
or build orchestrator that can plausibly be used twice, put it under
`scripts/`, give it a focused test, and add it to this table. Reserve `/tmp`
for genuinely disposable one-run scratch; do not leave reusable tooling hidden
in chat commands, build artifacts, or an agent-specific temporary directory.

Performance tools do not replace source-stable coordination. All standalone
performance measurements and heavy bootstrap builds share the advisory lock
`build/.pcc-performance.lock`; use a repository tool that acquires it, or
acquire the same lock before starting equivalent work. Before changing a
performance tool or a host-helper closure (`pcc/backend`, runtime provenance,
or another source tree imported by pcc1), inspect the lock owner and do not
edit while a measurement is active. A unique output directory is still
required; the lock protects machine load and mutable helper inputs, not output
paths.

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

**Profiling rules these tools encode — the failures they came from:**

- **A symbol table must come from the binary you sampled.** Two `nm` dumps taken
  the same day from different builds shared only their first 3 entries; the
  resulting profile claimed `fseek` was 10% of a workload that never seeks.
  `pcc_profile.py` re-derives symbols from the sampled binary for this reason;
  if you resolve addresses by hand, print both mtimes first.
- **`sample`'s counts are tree-cumulative, not self time.** Reading them as self
  time makes every caller look like a hot leaf. Subtract immediate children
  (`pcc_profile.py` does).
- **Derive the slide; never hardcode `0x100000000`.** A real pcc1 sample loaded
  at `0x104ad0000`. `slide = image load address - __TEXT vmaddr`, both read from
  the artifacts at hand (`Binary Images` section, `otool -l`).
- **Sampling a `gtimeout`/`sh` wrapper resolves to plausible nonsense.** Frames
  from the wrapper against pcc1's symbol table produced `_pcc_platform_waitpid`
  at 53% — a real 112-byte function, tight symbol spacing, no gap artifact, and
  completely fictional. `$!` after `gtimeout X cmd &` is *gtimeout*, not `cmd`.
- **`Physical footprint` in the sample header is the memory number to watch** —
  RSS read 1.9 GB while the footprint was 54.4 GB.
- **Parse only the `Call graph:` section.** `sample` also prints a FLAT
  top-of-stack summary indented about four columns. Folding both together
  invents shallow call paths — one run's heaviest "stack" came out as
  `Thread;start;pcc_gc_managed_pointer_find_slot`, which cannot happen — and
  double-counts every sample.
- **pcc allocates through its own allocator**, so `malloc_history` sees bulk
  `pcc_allocator_refill_small` -> `mmap` refills, not per-object allocations.
  The heap graph therefore answers "which path drove the allocator to grow",
  not "who allocated this object". Per-object attribution needs the runtime to
  capture a stack per allocation.


## Compile Modes

Be explicit about which mode you are debugging.

- **Single-file**: compile exactly one `.c` / `.py` file.
- **Merged directory** (default for directory inputs): concatenates selected
  `.c` files into one large translation unit.
- **`--separate-tus`**: compile each `.c` as its own TU, link at the LLVM /
  module layer.
- **`--sources-from-make GOAL`**: use `make -nB GOAL` to discover `.c` files.
  Recovers preprocessor flags only when the build system actually emits them;
  flags only documented in headers are not inferable.

Use the Lua commands from the current test or investigation context instead of keeping long examples in startup memory.


## Python Frontend / Bootstrap

The Python frontend is a separate subsystem from the mature C path. C-side
debugging rules do not automatically cover Python lowering, runtime fallback,
or bootstrap behavior.

### Bootstrap stage names

`scripts/bootstrap.sh --backend self` builds a staged compiler chain:

- `pcc0`: host Python running repository source.
- `pcc1`: first native compiler binary produced by `pcc0`.
- `pcc2`: compiler binary produced by `pcc1`.
- `pcc3`: compiler binary produced by `pcc2`.

The full self-host gate requires `pcc1` to compile `pcc2`, `pcc2` to compile
`pcc3`, and the stage outputs/baselines to match the relevant bootstrap
contracts. Do not describe a self-host fix as complete from a local toy repro
alone.

### Critical CLI knobs

| Flag | Default | Meaning |
|---|---|---|
| `--python-libpython` | `off` | `off`: hard error if codegen would need a CPython fallback. `auto`: link `libpython` only when fallback was needed. `on`: always allow/link the fallback surface. |
| `--ir-scaffold` | `on` | Only affects compiling pcc's own IR-builder code (scaffolding `pcc.llvm_capi.compat` out of the link, the matching libpython decision, native `ir.*` lowering). `auto` resolves to `on`; `off` is an older escape hatch. An application never needs it. |
| `--backend` | `llvm` | `llvm`, `llvm_capi`, or `self`. Strict self-host requires `--backend self`. |

Strict no-libpython self-host invocation:

```bash
env -u LC_ALL uv run pcc --backend self --python-libpython=off --ir-scaffold=on \
  pcc/__main__.py -o /tmp/pcc1_probe
```

Multi-file/bootstrap entry:

```bash
env -u LC_ALL uv run python scripts/pcc_multi.py --entry pkg.main \
  --out out_bin pkg/main.py ...
```

### Authoritative current state (do not invent)

- **Bootstrap byte-identity** (Issue 1 closure 2026-05-01):
  [`tests/bootstrap_gate_baseline.json`](tests/bootstrap_gate_baseline.json)
- **No-libpython fallback ratchet**:
  [`tests/fallback_baseline.json`](tests/fallback_baseline.json)
- These JSON files are the source of truth. `docs/issues/open-bootstrap-issues.md`
  is a historical tracker that may lag.

### Bootstrap regression discipline

When a gate that has been green for a long time regresses, treat that as
evidence that the current work is suspect until proven otherwise. Do not answer
with a local patch story before doing the causality audit.

Required sequence for bootstrap / pcc1 / no-libpython regressions:

1. Identify the first failing boundary in mode-labeled terms
   (`pcc0 -> pcc1` fallback, `pcc1 -> pcc2` runtime crash,
   `pcc2/pcc3` byte drift, no-host link failure, etc.).
2. List the recent touched subsystems that could plausibly own that boundary
   before changing more code. For codegen/runtime changes, assume your recent
   change is a prime suspect until IR/source/debugger evidence rules it out.
3. Separate stacked failures. If fixing the first boundary exposes a second
   crash, write them as two failures with two evidence chains; do not collapse
   them into one guessed root cause.
4. Do not weaken runtime or GC semantics to localize a bootstrap failure. In
   particular, do not disable GC tracking, barriers, owned-local cleanup,
   finalizers, or libpython rejection just to make a stage pass. Such changes
   are semantic changes, not diagnostics.
5. For ownership failures, verify the callee/caller reference contract before
   touching cleanup. Function calls return owned references; returning a
   borrowed local, parameter, module global, field, or singleton must retain in
   the callee rather than making the caller stop releasing owned results.
6. Host-side tests are not bootstrap proof. A Python-frontend/codegen/runtime
   fix that affects `pcc/py_frontend/codegen/`, `pcc/py_frontend/type_infer.py`,
   `pcc/py_runtime/`, or bootstrap entrypoints must include a focused
   regression and a pcc1/bootstrap gate appropriate to the touched path before
   it is described as fixed.
7. Debug instrumentation must be clearly tagged, recorded in the investigation,
   and removed or promoted to a deliberate tested feature before finishing. Do
   not leave temporary runtime probes that change archive staleness, link
   shape, or stage behavior.

### Dedicated gates for Python-frontend / bootstrap edits

```bash
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_py_multi_file_compile.py tests/python/test_py_multi_file_bootstrap_shim.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/c/test_llvm_capi_ir_parity.py tests/c/test_llvm_capi_end_to_end.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_fallback_baseline.py tests/python/test_ir_py_fallback_baseline.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_bootstrap_gate_baseline.py -q -x -n0
```

### Active self-host / package work

The active multi-stage plan is to compile pcc's runtime in pcc-Python and
shrink the libpython surface, while the current active goal prioritizes the
package/import path when it blocks real `pip install` / `import` scenarios.
Use `docs/archive/goal/goal-prompt-through-2026-09-06.md` for the goal contract and
the repository's GitHub issues for the current selected task, evidence, and
investigation routing. Older plans such as
`docs/plans/python-runtime-no-c-plan.md` are background, not the active task
board.

### Subprocess vs in-process boundaries

Some bootstrap host queries intentionally use subprocess boundaries
(`PCC_HOST_PYTHON` or `python3`) instead of in-process libpython calls. In
particular, `_link_with_self_backend` must not reintroduce compiled-stage
imports/calls of `pcc.backend.*`; that brings `py_cpy_*` back into the
stage1 closure. The long-term target is to compile those backend modules
natively, not to grow in-process CPython fallback again.


## Runtime & GC Backends

The runtime ships **five GC backend slots** selected at runtime via
`PCC_GC_BACKEND` (enum at `pcc/py_runtime/include/py_runtime.h::PCC_GC_KIND_*`,
values 0..4). The current default decision is recorded in
`docs/investigations/gc-backend-selection-matrix.md`: backend #0 remains the
default and rollback reference.

Detailed backend status lives in the repository's GitHub issues and routed GC investigations. Startup needs only the invariants below.


Rules when working on GC code:

- **Read the reference before patching.** `docs/refs_docs/gc-research/<lang>/` has
  the full upstream implementation; the pcc port is meant to mirror it. Do
  not re-derive — read.
- **Each backend gets its own focused gate**, not a shared one. Example:
  ```bash
  gtimeout 120s env -u LC_ALL PCC_GC_BACKEND=1 uv run pytest -x -n0 tests/python/test_gc_*.py
  gtimeout 120s env -u LC_ALL PCC_GC_BACKEND=2 PCC_WITH_THREADS=1 uv run pytest -x -n0 tests/python/test_gc_*.py
  gtimeout 120s env -u LC_ALL PCC_GC_BACKEND=3 uv run pytest -x -n0 tests/python/test_gc_*.py
  ```
- **Do not let backend X regress backend #0.** `PCC_GC_BACKEND=0` (default) is
  the reference; any new backend must keep stage2 / stage3 green.
- **One backend per PR** with one investigation file under
  `docs/investigations/`. No multi-backend bundle commits.
- **Keep migration mirrors differential-equal, then remove the C production
  dependency.** Most GC code currently has a C source file under
  `pcc/py_runtime/src/` and a pcc-Python port under `pcc/py_runtime/py/`.
  They must stay in sync while both exist; the end state links the
  freestanding pcc-Python implementation and retains C only as an oracle.

The `layer1.py` split has landed: `layer1.py` is now a facade, with behavior
split across focused `*_lowering.py` mixins plus `native_*.py` modules. When
adding Python lowering, choose the narrowest existing mixin or native module;
do not grow `layer1.py` again.


## Evidence Discipline (read before any measure/verify loop)

Every rule here was written after the same failure repeated in one session. The
common root is **treating something that looks like evidence as evidence**. Each
rule names the cheap check that would have caught it.

- **Never infer about new code from an old artifact.** A binary, `.o`, `.ll`,
  manifest, cache entry, or temp directory older than your edit tells you
  nothing about your edit. Check the timestamps before drawing a conclusion:
  ```bash
  stat -f '%Sm %N' -t '%d %H:%M' build/bootstrap/pcc1 <the-file-you-changed>
  ```
  Cost when skipped: a batch manifest produced at 12:51 by a `pcc1` built at
  12:17 was used to conclude that a fix made at 12:55 "did not take effect".
  See also `pcc/py_runtime/build_py/*.o`, which is keyed by SOURCE hash and
  does not invalidate when the compiler changes.

- **Never trial-and-error through a tool that swallows errors.** Get the error
  out FIRST, then debug. `scripts/bootstrap.sh` discards worker stderr and
  reports an unrelated downstream symptom ("linker has no inputs"); eight
  hypotheses were tested against it at 6-10 minutes each, all wrong, while the
  real message — `no-libpython function unavailable: run_worker_commands` —
  appeared instantly when `pcc1` was invoked directly. If a failure path has no
  diagnostic, **adding the diagnostic is the first fix**, not a detour.

- **Host-green is not pcc1-green, and it never will be.** Anything under
  `pcc/py_frontend/`, `pcc/backend/`, or `pcc/py_runtime/py/` is compiled by pcc
  itself and obeys a smaller language. Measured in one session: `wait -n` (macOS
  ships bash 3.2), `isinstance(x, str)` / `"\0" in x` rejecting plain decimal
  strings, a comprehension reusing a name already planned as an exact-int local,
  and `tempfile` / `shlex` / `os.mkfifo` simply being unavailable in the
  no-libpython closure. **Run the 30-second closure check before anything
  longer:**
  ```bash
  env -u LC_ALL uv run pcc --backend self --python-libpython=off \
      --ir-scaffold=on --python-library --emit-llvm=/tmp/chk.ll <the-file>
  ```
  If it fails, first re-run it on the `git show HEAD:<file>` version — several
  files do not compile in the closure at HEAD either, and that is not your bug.

- **Build the fast loop before the slow one.** In escalating cost:
  ```text
  ~30 s   closure check (above)
  ~5 s    pcc1-compiled microbenchmark for a cost-model question
  ~2 min  ./build/bootstrap/pcc1 ... pcc/__main__.py   (real errors, no wrapper)
  ~10 min scripts/bootstrap.sh --stage 1
  40 min+ full cold chain -- FINAL CONFIRMATION ONLY
  ```
  Never answer a question at a higher tier than it needs. Batch several verified
  changes into one expensive run rather than paying it per change.

- **Look up the historical number before calling a current one normal.** The
  repo records its own baselines (`docs/investigations/*.md`,
  `docs/issues/performance-gaps.md`, migrated issue bodies). A stage2 at
  90 minutes and a cold stage1 at 589 s were both treated as "how slow it is"
  until a search found 434 s and 311 s recorded days earlier — they were
  regressions, and knowing that changes where you look.

- **Do not edit source a running measurement depends on.** Workers spawned
  later import the edited module mid-run and silently corrupt the result. Wait,
  or prototype in a scratch copy and apply afterwards.

- **A smoke input that omits the feature proves nothing.** The chained pcc1
  smoke check used `print(1)`. A module with no function has no phi node, which
  turned out to be the *only* shape that still compiled -- pcc1 could not build
  any program containing a `def` for a full day, invisibly, because the smoke
  input dodged the broken path. Every pcc1 gate also stopped at `--emit-llvm`,
  which returns *before* the self-backend, so the emit path had no pcc1-side
  coverage at all. A smoke input must contain a function definition and must
  compile through to a binary with `-o`, then run.

- **Never run pcc1/bootstrap tests while a bootstrap stage is in flight.** They
  trigger a runtime-archive `make` and fight over `build_py/`. The compile
  "failure" that comes back is contention, not a defect, and it can corrupt the
  concurrent stage.

- **An `id()`-keyed cache must keep its keyed objects alive.** Free one and the
  allocator can reuse the address, so a stale fingerprint *hits* and returns
  another key's answer. "Distinct objects just miss, so it can only lose an
  optimization" is wrong, and was written into a comment here before the bug was
  found. Store the keys with the answer: `entry = (answer, tuple(keys))`. Expect
  host-green/pcc1-red from any such structure -- the host often keeps the keys
  alive incidentally.

- **When the only surface is an empty error message, fix the message first.**
  `PCC-PY-COMPILE-001 ... exception_type=Exception` with no text cost four
  probed-and-denied hypotheses before the cause was reachable. Two small
  diagnostics made it a five-minute job: `str(exc) or type(exc).__name__` at the
  pipeline's re-raise sites, and `PCC_DEBUG_SELF_BACKEND_TRACE` phase markers in
  the emitter. Prefer cheap probes against the *existing* binary (seconds) over
  a rebuild (~8 min) when testing a hypothesis about pcc1 behavior.

- **`sample` names the leaf; only `cProfile` names the caller.** A stripped pcc1
  gives `sample` nothing but C leaves, and acting on them is how you optimize the
  wrong function: the leaf `_dot_numeric_text_key_id` was genuinely 100M calls,
  but memoizing it measured 1.6x because the real defect was a caller scanning a
  whole dict on every miss (fixed: 1.89x, byte-identical output). Get Python-level
  attribution on a representative input before editing. A microbenchmark that
  contradicts a profile is evidence, not noise -- record the denial.

- **Instrument the real path before forming a hypothesis.** This is the single
  highest-value rule in this section, measured: locating where a value was lost
  took **five wrong guesses over several hours** (five different constructor
  lowering paths, each one plausible), then **one probe** that made the losing
  site print its own call stack found it in five minutes. The same day, four
  "obviously it must be X" hypotheses (isinstance mis-answering, dataclass slots,
  lost exception args, `dict.get` mis-lowering) were each killed by a
  seconds-long probe. A mechanism that explains the symptom is **not** an
  established mechanism. Make the code say where it went; do not reason about
  where it should have gone.

- **Validate in the environment the bug lives in, and measure the thing you
  claim.** Byte-identical output proves *semantics*, not *speed*. A callee
  signature cache produced identical IR and was accepted on that basis; it made
  pcc1 **11% slower** (6.87 s -> 7.61 s) because `getattr` in pcc-compiled code
  walks an MRO and hashes a string where CPython does one C-level dict hit.
  Likewise, five integer-lowering fixes all verified green on the host while
  pcc1 stayed broken, because the host parses with CPython and never executes
  the path being fixed. **A fix aimed at pcc1 requires a pcc1 number.**

- **No conclusion from a measurement without a control.** "My change leaks" was
  asserted three times from arms that differed in more than the change:
  bignum-vs-no-bignum rather than change-vs-no-change. The real control -- the
  same loop shape producing bignums through a path the work never touched --
  grew identically (29 MB -> 80 MB vs 26 MB -> 76 MB), so nothing supported the
  attribution. Either run a control arm or isolate one variable; a single
  before/after number across two different builds is not evidence.

- **Prove an old file corresponds to the current binary before using it as
  evidence.** Four separate wrong conclusions in one session came from this:
  a `.s` from a stale temp directory (retracted a whole line-count analysis); a
  `git show HEAD:` baseline taken while HEAD itself had moved mid-session (so
  "mine vs HEAD" was mine vs mine); an `nm` symbol table from an earlier pcc1
  build, whose first **3** entries were all that still matched, which produced a
  confident "`fseek` is 10%" for a workload that makes no seek calls; and an
  `ls -dt` temp directory picked up *after a failed run*, reporting the previous
  run's numbers as the new ones. Diff the symbol table, stat the file, or make
  the script refuse to report when the run it measured did not succeed.

- **"Unreachable" does not mean "removable."** Operand cleanup blocks in a pure
  constant-table module were 63.1% of its 72100 basic blocks and provably could
  not be entered, so the edge was dropped. The build failed:
  `self precise stack-map analysis ... managed root state disagrees at block`.
  Those blocks carry `pcc_gc_store_root` / `pcc_gc_frame_leave_lifo` and
  participate in the managed-root state the precise stack-map analysis
  reconciles at every join. Before deleting anything a static analysis can see,
  ask what invariant it maintains -- and note the follow-up guess ("then do not
  open the root") was also wrong, because that root exists for **GC** safety
  during element allocation, not for exception safety.

- **Never handicap the control arm to fit your tool.** Profiling host pcc with
  a main-thread sampler shows ~95% `subprocess._try_wait`, because the work is
  in child processes. Forcing the build serial so the main thread does the work
  makes the sampler happy and makes the *measurement worthless*: a pcc0-vs-pcc1
  comparison against a deliberately slowed pcc0 flatters pcc1. Fix the tool
  (profile the processes that work) instead of slowing the subject.

- **An "on-CPU" graph that counts blocked frames is not an on-CPU graph.** A
  coordinator blocked in `waitpid` burns no CPU but exists for the whole build,
  so summing its blocked samples with the workers' working samples adds
  wall-clock across processes and makes every percentage meaningless — one run
  read "65% `_try_wait`". Exclude blocking leaves on **both** the native and
  host sides, or the two graphs use different estimators and cannot be
  compared at all.

- **Check the parallelism knob before concluding "X cannot parallelize".**
  `jobs()` returns 1 when `n_modules <= 1`, so a single-file input serializes
  *every* front end. Measuring one file and concluding that pcc1 lacks worker
  support was wrong: the scheduler is the same Python source pcc1 compiles, and
  `is_native_worker_executable` accepts a Mach-O pcc1 as its own worker.

- **Your own edits invalidate the measurement environment.** Editing anything
  under `pcc/py_runtime/py/` makes every subsequent compile rebuild the runtime
  archive, and that `make` shows up inside the profile as a huge `waitpid`
  block. Warm the archive before profiling, and check the child process names
  (`ps -Ao pid=,ppid=,comm=`) before attributing a wait to compiler work.

- **A compile A/B must run the produced binaries and compare their output.**
  Checking the compiler's exit code alone once reported a false **5.9x
  speedup**: the "fast" arm was segfaulting quickly on every input. The Amdahl
  ceiling of the phase being optimized is itself an alarm — a reading above it
  means the measurement is broken, not that the change is great.

- **A replace-all edit needs a count assertion per intended site.** One
  sed-style `replace()` whose pattern also matched inside the replacement's own
  definition rewrote a helper's first line into a call to itself. The resulting
  source-level infinite recursion then masqueraded, in order, as a GC-soundness
  bug and as a backend miscompile (`bl <+0>` in the disassembly was pcc1
  faithfully compiling the typo), and cost two full exoneration experiments to
  un-attribute. Assert the expected occurrence count for every site touched,
  and never let the pattern overlap the replacement text.

- **One change per expensive verification.** Three changes landed together cost
  a full bisection when the run failed. If a batch is unavoidable, make each
  change individually revertible and bisect by disabling, not by rewriting.

## Debugging Playbook

**Before debugging any non-trivial failure, read and follow
[`docs/debugging-playbook.md`](docs/debugging-playbook.md) — required
procedure, not optional reference.** The 12 titles below are the index so you
know what exists and so `§N` references resolve; the linked file holds the
actual procedure (commands, oracle steps, LLDB recipe). Splitting it out of
this always-loaded file lowered its context cost, not its authority. Section
numbers are stable, so `§9` / `§12` referenced from investigations still
resolve. The techniques:

1. Make the failure deterministic first
2. Compare `pcc` against a reference from the same source
3. Use `llvmlite` as an oracle for `llvm_capi` parity
4. Treat short fallback / IR traces as locators only
5. Shrink the reproducer in stages
6. Test hypotheses by substitution, not only inspection
7. Avoid harness mistakes (these look like compiler bugs and are not)
8. Use LLDB for native crash triage, not guesswork
9. Do not stack unverified edits in shared codegen
10. Separate data-layout bugs from expression-semantics bugs
11. Prefer downstream-sensitive regression tests
12. Treat compile-time constant folding as a semantic subsystem


## C Codegen Invariants — Signedness

The repository lowers both `int` and `unsigned int` to LLVM `i32`. Signedness
is tracked **separately** in `pcc/codegen/c_codegen.py`.

Helpers:

- `_tag_unsigned`
- `_clear_unsigned`
- `_is_unsigned_val`
- `_convert_int_value`
- `_usual_arithmetic_conversion`
- `_shift_operand_conversion`

When you add or change an expression form, ask:

1. Does this produce an integer result?
2. If yes, should the result remain unsigned?
3. Will that result later feed `%`, `/`, `>>`, comparisons, or another
   arithmetic conversion?

Classic failure mode: the immediate value bits are correct but the returned
IR value is no longer marked unsigned, so a later operator uses `sdiv`,
`srem`, `ashr`, or signed comparison. This is easy to miss with toy tests
and shows up quickly in Lua, libc-heavy code, and control-flow-heavy
programs.


## Python Codegen / Runtime Invariants

The Python side has its own object-shape invariants that bugs cluster
around. Confirm them in source before guessing.

### Object header

- All heap objects start with `PyObjectHeader` (defined in
  `pcc/py_runtime/include/py_runtime.h` /
  `pcc/py_runtime/src/py_internal.h`).
- `refcount` at `obj + 0`, `type_tag` at `obj + 8` (int32). Type tag values
  in `enum PyTypeTag` (e.g. `PY_TYPE_STR == 4`, `PY_TYPE_LIST`,
  `PY_TYPE_DICT`, `PY_TYPE_INSTANCE`, `PY_TYPE_TASK`).
- Header `flags` carries `PY_FLAG_FINALIZED`, `PY_FLAG_GC_TRACKED`,
  `PY_FLAG_IMMORTAL`, etc. Checking and writing these is `__atomic_*` under
  `PCC_WITH_THREADS=1`.

### `PyClassObject` layout

- 120 bytes total. `del_method` is at offset 96, `attrs` at offset 104, and
  `metaclass` at offset 112. The pcc-Python mirror in
  `pcc/py_runtime/py/py_class.py` must match the C `PyClassObject` in
  `pcc/py_runtime/src/py_internal.h` exactly. Layout drift between them is a
  recurring class of bug.

### Refcount / GC discipline

- Read pointer slots through `pcc_gc_load_ptr()` and write them through
  `pcc_gc_store_ptr()`. These are barriers for backend #3 (generational
  forwarding) and #4 (relocation read barrier). Plain `obj->slot = x` works
  on backend #0 but breaks #3/#4.
- For generational backend #3 / colored-relocating #4, eager slot rewrite
  for owned items lives next to the per-type promotion code in
  `py_gc_backend.c`; do not invent a parallel path.
- `py_user_del_dispatch()` is invoked from `py_instance_dealloc()`. Setting
  `PY_FLAG_FINALIZED` after dispatch is what prevents resurrection cycles
  from re-entering the finalizer.

### Exception model

- `py_raise(exc)` stores in TLS and returns normally. Generated code must
  check `py_err_occurred()` after calls that may raise and branch to the
  error path. There is **no** Itanium-style stack unwinding; missing the
  check turns into "compile succeeded with no output" — see
  `docs/investigations/python-self-host-no-libpython-runtime-holes.md`.

### When this bites you

If a real-program failure looks like "object has no attribute X but the
class clearly defines X", it is almost always one of:

1. layout drift between C `PyClassObject` and `py_class.py`
2. missing `pcc_gc_load_ptr()` barrier on backend #3/#4
3. missing `py_err_occurred()` check after a raising call
4. the instance's **class object was freed by a stray over-release** — a
   self-host codegen ownership bug decrefs a *borrowed* reference as if owned,
   and because `py_class_new` does **not** mark user classes `PY_FLAG_IMMORTAL`
   (only the `object` root is immortal), the class hits refcount 0 and is
   `py_class_dealloc`'d + zeroed. Then `_lookup_field_index` reads `n_fields==0`
   and every `getattr` on any instance of that class fails ("attr not found"),
   or a moving backend segfaults in `class_lookup_in_mro`. Diagnose by
   watchpointing the class object's `n_fields` (`cls+72`, where
   `cls = *(inst+16)`) for a 119→0 transition and reading the `py_decref` stack.
   Fatal on all backends; the host compiler hides it. See
   `docs/investigations/pcc1-tuple-unpack-self-host-str-counter-corruption.md`.

Check in that order before suspecting frontend codegen. When one self-host
over-release corrupts a *shared* object (a class, an interned singleton), the
first visible symptom is usually far from the buggy release — chase the freed
object, not the innocent reader.


## Common Pitfalls

The recurring failure classes are: signedness metadata loss in C codegen, static/incomplete-array lowering, struct/union tag reuse, casted function-pointer globals, object-layout drift between C and pcc-Python runtime mirrors, missing GC barriers, missing `py_err_occurred()` checks, stale parser caches, and directory-mode probes accidentally compiled as source. Use `docs/investigations/INDEX.md` for the detailed historical routing instead of keeping the full table in startup memory.



## Testing & Definition of Done

For every semantic bug fix:

1. Add a focused regression test in `tests/`.
2. Confirm the original realistic reproducer is fixed.
3. Run the focused gates that cover the touched subsystem. Full-suite runs are
   optional by default and should be reserved for broad shared-path changes,
   release/commit qualification, or explicit user request.
4. Any commit-related completion must pass the bootstrap gate tests listed below.

For changes in shared parser/codegen paths, a stricter gate:

1. Run the smallest reproducer first.
2. Run one existing sensitive integration check for the same bug class
   before making another edit.
3. Only then continue or broaden the patch.

For critical bootstrap paths, the self-host must be proven green before the
work is considered fixed. This includes changes to `pcc/parse/py_lift.py`,
`pcc/parse/py_parse.py`, `pcc/py_frontend/py_ast.py`,
`pcc/py_frontend/pipeline.py`, `pcc/py_frontend/type_infer.py`,
`pcc/py_frontend/codegen/`, `pcc/cli_bootstrap.py`, the self backend, or any
runtime object/class/dataclass semantics used by those paths. A local
minimized reproducer is necessary but not sufficient: run the relevant
bootstrap command or `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py` and
do not claim the fix if stage1→stage2→stage3 is not demonstrated.

Recommended focused gates for high-risk changes:

```bash
gtimeout 120s env -u LC_ALL uv run pytest tests/c/test_c_parser.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest 'tests/c/test_lua.py::test_onelua_compile_and_link' -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest 'tests/c/test_lua.py::test_pcc_runtime_matches_native[math.lua]' -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/c/test_lz4.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/integration/test_sqlite.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/c/test_unsigned_loads.py -q -x -n0
```

Python frontend / bootstrap gates: see *Python Frontend / Bootstrap* above.

GC backend gates: see *Runtime & GC Backends* above.

**Definition of Done** — before stopping, all of:

- [ ] smallest reproducer passes
- [ ] original integration scenario passes
- [ ] temporary debug edits and probe files removed
- [ ] regression test exists in `tests/`
- [ ] focused gates covering the touched subsystem pass; if a full suite is
  skipped, say exactly which subset was run and why it is sufficient for the
  current change
- [ ] bootstrap validation commands required for the submission were run successfully

Commit-level bootstrap validation (mandatory before considering work complete):

```bash
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_bootstrap_gate_baseline.py -q -x -n0
gtimeout 120s env -u LC_ALL uv run pytest tests/python/test_fallback_baseline.py tests/python/test_ir_py_fallback_baseline.py -q -x -n0
```

Repair principle: prefer elegant fixes (API/ABI correctness, boundary semantics, readability, and maintainability). When possible, prioritize bootstrap-driven capability first and then add compatibility patches as follow-up.


## Package / NumPy Claim Hygiene

`B-P0-PKG` work is about real package install/import behavior, not a command
shape that only looks similar.

- `pcc1 -m pip install numpy ...` succeeds only when the real package artifact
  is installed into the target site and its package metadata is usable.
- `import numpy` is a separate gate. Do not claim NumPy support from install
  success alone, from an array-core-only test, or from a synthetic package
  named `numpy`.
- Do not add package-name special cases such as `if package == "numpy"` to make
  a gate pass. Fix the generic package/import/ABI/lowering behavior and add a
  focused regression for the generic feature.
- Native extension ABI compatibility and no-libpython behavior are separate
  claims. Keep `pcc-native` rejection, `cpython-compat` acceptance, and
  `PCC_HOST_PYTHON=/bin/false` evidence distinct.
- See the full claim-hygiene table in `docs/archive/goal/goal-prompt-through-2026-09-06.md` §0.10 for related
  distinctions such as host pcc vs pcc1, libpython mode vs no-libpython, fake
  package vs real package, and stage1 vs pcc1→pcc2→pcc3.
- Current package priority and known blockers live in
  the repository's GitHub issues; the active protocol lives in
  `docs/archive/goal/goal-prompt-through-2026-09-06.md`.


## Platform Gotchas (macOS)

Keep platform-specific command details in focused tests or investigations. Startup reminder: macOS has no `/dev/full`, Lua builds need macOS flags, and Darwin multi-TU MCJIT teardown crashes can be lifecycle issues after correct runtime output.


## IR Fix Policy

Semantic bugs belong in parser / codegen source logic.
`postprocess_ir_text()` is acceptable **only** for narrow lowering gaps that
the IR builder cannot directly express.

- Do not hide CFG, type, or signedness bugs with text rewrites.
- Currently, the only acceptable remaining text-level lowering is the
  `va_arg` path. Anything else is a source-level compiler bug, fix it
  before serializing IR.
- The historical `run_translation_units_with_system_cc()` path uses
  repository-managed LLVM to optimize and emit objects. That is an unresolved
  external dependency, even without a text-IR handoff. Replace its owner with
  pcc's own implementation; do not reintroduce a handoff to cc/clang/LLVM.


## Investigation Workflow (mandatory for any non-trivial bug)

Open a written investigation for anything more involved than a one-line fix,
so the next agent can pick it up without re-reading the chat log. **Before you
create or continue any `docs/investigations/*.md` file, read and follow
[`docs/investigation-workflow.md`](docs/investigation-workflow.md) first** — it
holds the mandatory-sections template and the three modes (Repro / Continue /
Report), and they are required, not optional. The enforceable guardrails, kept
inline so they bind even if the linked file is skipped:

- **Discover first.** Scan [`docs/investigations/INDEX.md`](docs/investigations/INDEX.md)
  before opening a new file; if the symptom matches, continue that file via a
  `## Update` block or link to it as predecessor. One investigation = one file
  under `docs/investigations/<specific-slug>.md`; existing files are historical
  record — never delete or rewrite them.
- **Read the matching investigation END TO END before writing any code, and
  read its `[DENIED]` and "did not help" sections first.** They are the most
  valuable part of the file and the part agents skip. Every one of them is a
  fix somebody already wrote, measured, and disproved — re-deriving it costs a
  full rebuild/measure cycle and produces a change that is known not to work.
  These docs are long on purpose. Skimming the headings is not reading them.
  If you are about to make a change that an investigation already recorded as
  refuted, you must either cite new evidence that overturns that verdict, or
  not make the change. Concrete recurring examples this rule exists for:
  - A `sample`/profiler "direct child" is often a **grandchild** whose parent
    frame the tail-call pass elided. Read the real lowered IR for the function
    (from the bootstrap's own `self_backend_module_*.ll`, not a standalone
    `--python-library` emit, which can lower a closure to a `raise` stub)
    before believing a caller attribution or optimizing the wrong callee.
  - A profile tells you **where to look**, not what to change. Changes made
    from profile shape alone have repeatedly measured zero or negative;
    the wins came from reading the code and the IR at the spot the profile
    pointed to.
  - `pcc/py_runtime/build_py/*.o` is keyed by **source** hash and does not
    invalidate when the compiler changes, so a codegen change measured without
    wiping the objects measures nothing.
- **Regenerate the index** after adding/editing any `docs/investigations/*.md`:
  ```bash
  env -u LC_ALL uv run python scripts/regen_investigations_index.py
  ```
- **Agents must not `git commit` unless the user explicitly asks** — the
  "save before each experiment" idiom is for the user-driven workflow.
- **One proposal at a time**, run to a `[CONFIRMED]`/`[DENIED]` verdict; no
  silent "while I was in there" fixes.
- `## Test [CONFIRMED]` means the failure was observed under the listed
  command, not optimism. `## Status` is mandatory and final. Two distinct
  bugs → two files.


## Maintainer Notes

Packaging/release notes are maintainer workflow, not active agent startup context. Consult project docs or ask before changing release metadata.
