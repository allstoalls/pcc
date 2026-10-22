# Chapter 15: Bootstrap — the pcc1→pcc2→pcc3 Fixed Point

Every subsystem described in the previous fourteen chapters — parsing, type inference, lowering, the object model, ownership, the five GCs, the self backend, the no-libpython runtime — carries its own tests. But there is a gulf between "each part is individually correct" and "the whole system is coherent": a test is a sample of behavior, and no amount of sampling proves a universally quantified claim. pcc crosses that gulf with an old and unforgiving device: make the compiler compile itself, make the product compile itself again, and keep going until the output converges to a fixed point. This chapter covers the whole device: the semantics of the four stages; the machinery in [scripts/bootstrap.sh](../../scripts/bootstrap.sh) and [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py); the verification ladder behind byte identity; the three mutually independent proofs; the taxonomy of pcc1/pcc2 differences; the baseline system that nails the fixed point down as a regression gate; and the boundary — which must be drawn honestly — between all of this and Thompson's *Reflections on Trusting Trust*.

## Chapter Overview: Three Compiler Generations Prove Three Things

Read this chapter along the pcc0→pcc1→pcc2→pcc3 chain. pcc0 proves the host path can produce a compiler; pcc1 proves that produced compiler can keep compiling; the pcc2/pcc3 comparison proves the system has begun to reproduce itself stably.

- pcc1 failures usually point at frontend, runtime, or fallback boundaries.
- pcc2 or pcc3 drift often points at nondeterminism, layout, link metadata, or diagnostic output.
- Byte identity is not ceremony; it upgrades "seems to run" into "can reproduce itself stably."

## 15.1 The Problem and the Design Space: Why Three Stages Plus a Byte Compare

Start with why bootstrap at all. Among the five differentiators listed in Chapter 1, the `pcc1 -> pcc2 -> pcc3` self-hosted fixed point comes first, and the reason is stated in the north-star section of [AGENTS.md](../../AGENTS.md): **the fixed point is more than a one-time byte compare — it is evidence that pcc's Python semantics, runtime, code generation, object model, backend, and diagnostics are coherent enough to reproduce themselves.** Unpacked: for pcc to compile itself, the compiler's source must fall, in its entirety, inside the Python subset pcc can compile (a frontend-coverage proof); for the product pcc1 to compile the source a second time, the native runtime must be correct under a workload of "compiling a compiler" scale (a runtime-correctness proof); for pcc2 and pcc3 to be byte-identical, the entire pipeline must be deterministic (a determinism proof). Incoherence at any layer snaps the chain at the corresponding edge — and the break point names its responsible boundary in mode-labeled language.

Three alternatives were considered and rejected.

**Alternative one: don't bootstrap.** Most Python acceleration tools take this road: the compiler runs forever on CPython, and only user code gets compiled. That is reasonable for an accelerator; it is not acceptable for pcc. pcc's thesis is execution ownership (Chapter 1), and a compiler that cannot itself live without CPython cannot claim that the "no-libpython native execution path" is complete. The more practical loss is coverage: the compiler itself is on the order of a hundred-plus thousand lines of Python — the largest and most hostile single real input in the repository. Abandoning bootstrap means abandoning that stress test for free.

**Alternative two: two stages plus a test suite.** Let pcc1 compile pcc2, then run tests against pcc2. The problem is that tests are behavioral samples: pcc2 passing a thousand tests proves a thousand paths correct. By contrast, "pcc2 compiling the source produces the same output as pcc1 compiling the source" is a **total comparison** of the entire compilation function over one enormous input — any single source of non-semantic noise (hash ordering, parallel scheduling, uninitialized padding) or any semantic divergence will make at least one of millions of bytes differ. The byte compare converts "compiler behavior is equal" from an argument into a machine-decidable assertion.

**Alternative three: compare only pcc1 against pcc2.** This is the beginner's intuition, and it demands a property that is too strong. pcc1 is produced by pcc0 — host CPython interpreting the repository source — while pcc2 is produced by the native binary pcc1; two compilers running on **different execution engines** may produce systematically different output bytes even when their semantics agree exactly. The sizes frozen in [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) show this directly: under the self backend, stage1 is 4442648 bytes while stage2 and stage3 are both 4524200 bytes — pcc1 differs from pcc2, and pcc2 matches pcc3. The classical three-stage convergence argument (the GCC tradition) explains why this is exactly enough: pcc2 = pcc1(source) and pcc3 = pcc2(source); if pcc2 == pcc3, then pcc1 and pcc2 — two binaries with **different bytes** — computed the same function on the input "compile the pcc source." The fixed point is defined between self-produced compilers; the first link is allowed to carry the fingerprints of a foreign host.

The honest part: byte identity holds **after Mach-O signature normalization**. The header comment of [scripts/bootstrap.sh](../../scripts/bootstrap.sh) records this plainly (status comment dated 2026-04-23): a direct `cmp` still fails because the Mach-O code-signing metadata differs, so the verification flow runs `codesign --remove-signature` on temporary comparison copies before declaring success. The taxonomy in 15.6 files this class of difference under link metadata — understood, normalized, and recorded, not papered over.

## 15.2 Stage Semantics: pcc0 → pcc1 → pcc2 → pcc3

[AGENTS.md](../../AGENTS.md) fixes the stage names, and this book uses them throughout:

```text
pcc0     host CPython interpreting the repository source
         (not a binary — a way of running)
pcc1     the first native compiler binary produced by pcc0
pcc2     the binary produced by pcc1 compiling the pcc source
pcc3     the binary produced by pcc2 compiling the pcc source
verify   byte-by-byte comparison of pcc2 and pcc3
         after signature normalization
```

Each of the three edges proves a different thing:

```text
pcc0 -> pcc1   source compilability: the compiler falls entirely within
               its own subset, and the closed world holds under
               --python-libpython=off
pcc1 -> pcc2   runtime correctness: the native object model / GC /
               exceptions / ownership survive a workload of
               "compiling a compiler" scale
pcc2 -> pcc3   behavioral self-stability: a self-produced compiler
  + cmp        converges on the same input
               (= the fixed point; only this step licenses "self-hosted")
```

All three edges take the same input file: [pcc/__main__.py](../../pcc/__main__.py). It is five lines long — it imports `bootstrap_cli_sys_argv_exit` from `pcc.driver.cli_bootstrap` and calls it. The stage1 command therefore exhibits a telling symmetry: `python -m pcc ... pcc/__main__.py -o pcc1` — **the command and the input are the same module; only the host differs.** pcc0 runs it on CPython; pcc1 runs it on pcc's own runtime.

In [scripts/bootstrap.sh](../../scripts/bootstrap.sh), the three-stage bootstrap pipeline is written directly:

```bash
# scripts/bootstrap.sh
# Stage 1: pcc0 -> pcc1
env -u LC_ALL uv run pcc pcc/__main__.py -o pcc1 \
  --backend=self --python-libpython=off --ir-scaffold=on

# Stage 2: pcc1 -> pcc2
./pcc1 pcc/__main__.py -o pcc2 \
  --backend=self --python-libpython=off --ir-scaffold=on

# Stage 3: pcc2 -> pcc3
./pcc2 pcc/__main__.py -o pcc3 \
  --backend=self --python-libpython=off --ir-scaffold=on

codesign --remove-signature pcc2 pcc3
cmp -s pcc2 pcc3
```

Inside [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py), the entry point responds to the self-compiled binary's control logic:

```python
# pcc/driver/cli_bootstrap.py
def bootstrap_cli_main(argv: list[str]) -> int:
    """Entry point for the self-hosted pcc1/pcc2/pcc3 binary."""
    if "--pytest" in argv:
        return _run_pytest_harness(argv)
    return _dispatch_compile_job(argv)
```

The assertions enforcing the baseline are also part of the tests, with [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) requiring the results to match:

```python
# tests/python/test_bootstrap_gate_baseline.py
def test_bootstrap_fixed_point_baseline():
    baseline = json.loads(pathlib.Path("tests/bootstrap_gate_baseline.json").read_text())
    assert baseline["stages"]["pcc2_vs_pcc3_byte_identical"] is True
    assert baseline["no_libpython"]["py_cpy_calls"] == 0
```

Claim hygiene has a strict phrasing table here ([goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10): host pcc ≠ pcc1; stage1 passing ≠ the fixed point passing. "pcc can compile X" and "pcc1 can compile X" are two different claims — the latter requires every dependency of X to live inside the native closure. "Bootstrapped to stage1" and "the pcc1→pcc2→pcc3 fixed point" are likewise two different claims, separated by two levels of runtime correctness. The rule in [AGENTS.md](../../AGENTS.md): never declare a bootstrap fix complete from a local toy reproducer.

## 15.3 Mechanism (1): bootstrap.sh as a Stage Machine

[scripts/bootstrap.sh](../../scripts/bootstrap.sh) is the macOS arm64 three-stage entry point, roughly 380 lines, and its core is one `run_stage()` invoked three times. The design points worth reading item by item:

**The runtime defaults reveal that bootstrap exercises two compilers at once.** Every stage runs under `PCC_RUNTIME_CC=pcc` and `PCC_RUNTIME_HIGH=py` (corresponding to `_runtime_cc_mode()` / `_runtime_high_mode()` in `pipeline.py`, whose own defaults are already `pcc`/`py`): the runtime archive is compiled by pcc's **C frontend** (not the host cc), and the runtime's high-level modules are taken from the pcc-Python ports (not the C sources — see Chapter 14). In other words, the bootstrap gate stress-tests both compilation paths simultaneously: the C frontend compiling the runtime, and the Python frontend compiling the compiler. The other two defaults are `PCC_BOOTSTRAP_PYTHON_LIBPYTHON=off` (strict no-libpython) and `PCC_BOOTSTRAP_PYTHON_IR_PASSES` defaulting to off. The backend default is platform-dependent: `self` on Darwin arm64, `llvm` elsewhere — note this is the bootstrap script's default; the public CLI's default backend remains LLVM (a mode label in the README status table).

**Stale-artifact defense.** `run_stage()` opens with an unconditional `rm -f "${out_exe}" "${out_exe}.tmp"`, with a comment giving the reason: never let a failed or short-circuited compile leave the previous round's stage binary in place, or stage3 might run against a **stale pcc2** — the gate goes green, but what it proves is last week's compiler. This is the micro-version of the "baselines are state" philosophy: better to fail than to pass on expired evidence.

**The publish barrier (`stage_exec_barrier`).** Before a stage product is executed by the next stage, it must pass a barrier: `codesign --verify`, a full `cat` of the file, a default `PCC_BOOTSTRAP_STAGE_EXEC_DELAY=0.10`-second delay, a `--help` run, and finally a compile of a two-line smoke program (`def main() -> int: return 0`). This barrier was paid for with a real incident: [docs/investigations/self-backend-mach-o-stage-publish-race.md](../../docs/investigations/self-backend-mach-o-stage-publish-race.md) records stage3 intermittently segfaulting (exit code 139) when it executed a freshly linked pcc2 immediately — the same binary succeeding when run again moments later. The root cause was not compiler semantics but the Mach-O publication boundary on macOS arm64: an atomic rename is not enough; the stable boundary is to ad-hoc sign first and then `codesign --verify`, forcing the system verifier to observe the final Mach-O before the next stage's exec. The fix lives in `_finish_self_backend_executable()` in `pipeline.py`: `codesign --force -s -` on the temporary file → verify → publish via `/bin/mv -f` → verify again → a `/bin/sync` or `cat` barrier (the latter added by the follow-up investigation `self-bootstrap-reliability-performance-2026-05-15.md`). Notably, all of it is done through subprocesses — an attempt to use `os.replace()` was rejected because it introduced a no-libpython fallback into the strict bootstrap: **even the implementation of the publish sequence is constrained by the bootstrap closure.**

**The verification ladder.** After stage3 comes a three-tier comparison:

```text
cmp pcc2 pcc3                     → byte identity, strongest verdict, exit 0
codesign --remove-signature on
  copies, then cmp                → "differs only in signature metadata",
                                    exit 0
size + md5 structural compare     → sizes differ      = FAIL, exit 1
                                    same size, bytes
                                    differ            = WARN, exit 2
                                    ("suspected metadata noise")
```

Each rung corresponds to a degree of honesty: the second rung names the difference and normalizes it away; the third does not pretend success — exit code 2, with a comment that once the build is fully deterministic (no embedded timestamps/paths/uuids), `cmp` should simply succeed. The corresponding effort on Linux lives in `_platform_link_flags()`: `-Wl,--build-id=none -s`, stripping the known link-time nondeterminism sources from the artifact.

**Stage profiles.** With `--profile-json` plus `PCC_BOOTSTRAP_PROFILE_DIR`, each stage writes a JSON in the `pcc.bootstrap_stage_result.v1` schema (compile_wall_ms, publish_barrier_ms, returncode, and so on) for performance-regression investigations such as `bootstrap-self-time-after-layer1-split-2026-05-13.md`.

**`--reuse-stage1`.** pcc1 is GC-backend-agnostic: `PCC_GC_BACKEND` selects the collector at runtime (Chapter 10), so it affects only the runtime behavior of stage2 and beyond, never the build of pcc1. The script therefore allows building pcc1 once and reusing it across many stage2/stage3 runs — the five-GC gate is built on exactly this point (15.7).

## 15.4 Mechanism (2): cli_bootstrap.py — What pcc1 Actually Is

[pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) is roughly seven thousand lines and is the entire user-facing surface of the stage binaries. Read it with one fact in mind: **this file must itself be compilable by pcc** — it is a member of the stage1 closure. That explains its dialect.

The entry chain is [pcc/__main__.py](../../pcc/__main__.py) → `bootstrap_cli_sys_argv_exit()` → `bootstrap_cli_main()`. The latter routes by request type:

```text
--pcc-python-multi-codegen-worker  → re-entry point for parallel codegen workers
--pytest ...                        → _run_pytest_from_pcc1 (pcc1 drives the
                                      test suite)
-m MODULE ...                       → _run_python_module_from_pcc1
   pip / pcc.package.* among them   →   native package shims
                                        (_run_native_pip_shim_from_pcc1 etc.,
                                         see Chapter 17)
   any other module                 →   pcc1 compiles it to a native binary
                                        and runs that
                                        (_run_compiled_python_module_from_pcc1,
                                         backend=self, libpython=off)
   under --python-libpython=auto/on →   _run_python_module_from_pcc1_with_mode
                                        explicitly delegates to a CPython compat
                                        subprocess (prints PCC1_COMPAT_RUNNER_
                                        MANIFEST; never claims no-libpython)
C inputs (.c / --sources-from-make /
  -I/-D/-U / directories)           → _run_host_pcc_from_pcc1 (delegates to
                                      the host pcc CLI)
otherwise                           → parse_bootstrap_cli_args → compile the
                                      Python input
```

The core compile path is short: `parse_bootstrap_cli_args()` parses `--backend` / `--python-libpython` / `--ir-scaffold` / `-o` / `--emit-llvm` and the rest with a hand-written while loop (no argparse — argparse is not in the native closure), and then `_observed_compile_python()` calls `compile_python` from `py_frontend.pipeline` directly. That "directly" has a docstring worth quoting in full: **"This intentionally calls `_compile_python` directly instead of passing it as a first-class callable through `observed_compile`. The self-host path does not yet have a native `callable(*args, **kwargs)` ABI."** The bootstrap subset has no first-class function boxing (a limitation covered in Chapter 5), so the observability wrapper can only be written as a fixed-shape direct call, never as a higher-order function. Traces of the same kind run through the whole file: `_normalized_sys_argv()` copies and normalizes each string with `(sys.argv[i] or "") + ""`; `_copy_seq()` uses an explicit index loop instead of the slicing idiom. **The compiler's CLI is written in the Python the compiler itself can digest — at once a constraint and a test.**

Both delegation boundaries are defended. `_run_host_pcc_from_pcc1()` refuses recursive delegation when `PCC_HOST_PCC` points back at itself; the default `-m` path no longer routes through host Python at all — a generic module is compiled by pcc1 into a native binary and then run (`_run_compiled_python_module_from_pcc1`, `backend=self`, `libpython=off`), and host delegation degrades to an explicit opt-in under `--python-libpython=auto/on`: only then does `_run_python_module_from_pcc1_with_mode` start a CPython compat subprocess, refusing recursion when `PCC_COMPAT_PYTHON` points back at itself. That opt-in path prints `PCC1_COMPAT_RUNNER_MANIFEST` to keep the mode explicit, and the pcc1 process itself stays no-libpython. [AGENTS.md](../../AGENTS.md) elevates the boundary to a rule: `_link_with_self_backend` must not import or call `pcc.backend.*` from inside a compiled stage, because that drags `py_cpy_*` back into the stage1 closure. The subprocess is a deliberately chosen isolation layer: host capability may be **invoked**, but never **linked**.

## 15.5 Three Independent Proofs: Zero py_cpy_*, No libpython, Byte Identity

The bootstrap row of the README status table (Issue 1 closed 2026-05-01) gives its evidence as a triple: **zero `py_cpy_*` calls** in the IR emitted by pcc2/pcc3; **no libpython entry** in `otool -L`; and pcc2/pcc3 **byte-identical** after signature normalization (the IR text is byte-identical as well). Each item locks one layer; none implies another; only together do they constitute the compound claim "strict no-libpython bootstrap."

**Zero `py_cpy_*` calls locks the generated-code layer.** `py_cpy_*` is the bridge surface declared in the "Phase 4: CPython C-API fallback" section of the runtime header [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h): `py_cpy_import()`, `py_cpy_getattr()`, `py_cpy_call1()`, and friends, which operate on **opaque CPython pointers** distinct from pcc's own objects, implemented in [pcc/runtime/src/py_libpython.c](../../pcc/runtime/src/py_libpython.c). When the frontend met an expression it could not derive a native lowering for, the historical escape hatch was to emit these calls (Chapter 14). Counting zero in the merged IR means every path in the compiler's closure took a native lowering — the closed world is not an assertion, it is a grep-verifiable count.

**No libpython linkage locks the artifact layer.** `_ensure_runtime()` selects the runtime archive on demand: when no fallback is needed it links `_PY_RUNTIME_ARCHIVE_PCC_PY`; when one is needed it swaps in the variant carrying the `py_libpython` compatibility bridge. `_links_libpython()` in [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) runs `otool -L` directly against the binary (`ldd` on Linux), looking for the strings `libpython` / `Python.framework`. This layer guards against a different regression class than the previous one: even with clean IR, a misconfigured build could silently link the bridge archive back in.

**Byte identity locks the self-referential layer.** The first two items hold for a single binary; the third is the behavioral equality between pcc1 and pcc2 (the convergence argument of 15.1). Its sensitivity to nondeterminism far exceeds ordinary tests — one unstable hash iteration order, one shifted boundary in a parallel shard, and the difference shows up somewhere in megabytes of output.

The fallback surface has two further, complementary detection nets, with the division of labor recorded in two authoritative baseline files: [tests/fallback_baseline.json](../../tests/fallback_baseline.json) is the no-libpython fallback ratchet (baseline zero; any growth is a failure), and [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) is the bootstrap gate baseline. The two case studies in 15.9 were each caught by a different net: one detonated as a strict-mode compile-time hard error; the other slipped silently into the IR and was caught by the ratchet's count scan.

## 15.6 A Taxonomy of Differences: Classify First, Then Fix

When the fixed-point gate goes red, the first action is not to fix — it is to classify. Section §19.2 "Fixed-point classification" of [goal-prompt.md](../../docs/goal/goal-prompt.md) defines eight classes and attaches one prohibition: **until the difference class is identified, do not patch around the symptom.**

```text
1 semantic execution    semantic execution difference (a real compiler bug)
2 codegen IR            emitted-IR difference (caught by the IR-text gate)
3 class field layout    class field layout difference (C / pcc-Python mirror
                        drift, see Chapter 7)
4 backend selection     backend selection difference (the declared backend
                        did not actually take effect)
5 object/link metadata  object/link metadata (Mach-O signatures, build-id,
                        uuid)
6 runtime archive/link  runtime archive or link-shape difference
7 diagnostic/error-path diagnostic and error-path difference
8 performance-only      performance-only difference (bytes identical,
                        time differs)
9 unknown               unknown — a legitimate class
```

(The eight-class wording in the north-star section of [AGENTS.md](../../AGENTS.md) — semantic / IR-text / class-layout / object-model / backend nondeterminism / link metadata / perf-only / diagnostic — is the obligations-level phrasing of the same taxonomy.)

The value of the taxonomy is that it decides both "what to fix" and "with what evidence." The correct handling of the link-metadata class (Mach-O signatures) is **normalize and record** — the signature-stripped comparison in `bootstrap.sh`; treating it as semantic and "fixing" it would mean fiddling with code generation, futile and dangerous. Conversely, the semantic class (the double free in case study one, 15.9) must never be hidden by normalization. The backend-nondeterminism class has one instance that has already been operationalized: `_run_stage2_3()` in [tests/python/test_pcc_bootstrap_full.py](../../tests/python/test_pcc_bootstrap_full.py) deliberately pins **the same parallel budget** for pcc2 and pcc3, with a comment stating that until codegen/link output is proven independent of parallelism, changing the worker count can change binary layout and break the byte-identity gate — semantics unchanged, bytes changed. This is the taxonomy reshaping infrastructure in reverse: a known nondeterminism source is institutionally clamped, instead of being relitigated at every red light. The existence of class 9, unknown, is equally a design decision: it forces an investigation to write "we don't know" when evidence is insufficient, instead of promoting the most convenient story to a conclusion — the micro-form of the repository's claim hygiene.

Only after classification comes the seven-step bootstrap regression discipline of [AGENTS.md](../../AGENTS.md), each step distilled from a real incident (case study one in 15.9 is the source of several): (1) identify the **first failing boundary** in mode-labeled language (a pcc0→pcc1 fallback? a pcc1→pcc2 runtime crash? pcc2/pcc3 byte drift?); (2) list the recently touched subsystems that could own that boundary, and treat **your own most recent change as the prime suspect** until IR/source/debugger evidence rules it out; (3) separate stacked failures — when fixing the first boundary exposes a second crash, write them as two failures with two evidence chains; (4) never weaken runtime or GC semantics to turn a stage green — disabling tracking, barriers, owned-local cleanup, or finalizers is a semantic change, not a diagnostic; (5) for ownership failures, verify the caller/callee reference contract before touching cleanup code (Chapter 9); (6) host-side tests are not bootstrap proof — a fix touching the frontend, runtime, or bootstrap entry points must come with the corresponding pcc1/bootstrap gate; (7) debug probes must be tagged, recorded, and removed or promoted — never left behind as ad-hoc changes that alter archive staleness or link shape.

## 15.7 The Gate System: Baselines as State

The authoritative record of bootstrap state is not a document; it is two frozen JSON files consumed by tests. The wording in [AGENTS.md](../../AGENTS.md) is "authoritative … (do not invent)": [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) records, as captured on 2026-05-01, the per-backend, per-stage sizes and `links_libpython` status (both the llvm and self backends, all three stages `false`, and `byte_identical_pcc2_pcc3` `true` for both), and declares one-way ratchet semantics — any `links_libpython` flipping back to `true` is a regression. The historical tracker [docs/issues/open-bootstrap-issues.md](../../docs/issues/open-bootstrap-issues.md) is allowed to lag; the JSON is not. The reason for JSON over prose is machine executability: [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) verifies it field by field, and nobody runs pytest against a paragraph of Markdown.

The design of that baseline test is itself worth reading: it **only inspects binaries that already exist**, skipping when they are missing, and never triggers a heavy build — the existence of the gate must not tax every pytest run with minutes of bootstrap. The heavy gates are explicitly quarantined into a separate set of files: `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`, one file per GC backend, each running the full real `pcc1 -> pcc2 -> pcc3` chain. The shared helpers live in [tests/python/test_pcc_bootstrap_full.py](../../tests/python/test_pcc_bootstrap_full.py), whose module docstring compresses the philosophy into one sentence: **"Speed comes from *not skipping anything* — it comes from sharing stage1 and keeping each GC as an independent file/node."** Concretely: a session-scoped fixture `shared_stage1_pcc1` builds, under a file lock, one GC-backend-agnostic shared pcc1 (`_shared_pcc1_is_fresh()` decides freshness from the newest mtime in the source tree plus a libpython-link check); each GC file seeds it into its own output directory, runs stage2 and stage3 via `bootstrap.sh --reuse-stage1` under `PCC_GC_BACKEND=N`, then asserts that all three stages exist, none links libpython, and pcc2/pcc3 are byte-identical after normalization. Scheduling is weight-ordered (`_GC_BOOTSTRAP_WEIGHT`: gc0=60, gc4=50, gc3=40, gc1=gc2=30 — heaviest first); `_bootstrap_active_gc_lease()` caps the number of simultaneously active chains (at most 3 by default); and every child process goes through `run_process_group_timeout` (2400 seconds), timed out and reaped at process-group granularity. Every part of this machine corresponds to a real pain once suffered: a heavy backend starved of a slot; zombie pcc1 processes wandering a machine for hours.

The gates thus form a three-tier pyramid: the lightweight baseline check (seconds, on every pytest run); the single-backend full bootstrap (minutes, commit-level verification, listed in [AGENTS.md](../../AGENTS.md) as commit-level mandatory); and the five-GC full bootstrap matrix (the heaviest — the completion evidence for runtime/GC/object-lifecycle claims). The complementary semantic-parity ratchet is [tests/python/test_self_host_oracle_diff.py](../../tests/python/test_self_host_oracle_diff.py) (described in the repository map as the core Python semantic oracle / pcc1-pcc2 parity ratchet).

## 15.8 Relation to — and the Boundary with — *Reflections on Trusting Trust*

No chapter about bootstrapping can avoid Thompson's 1984 Turing Award lecture. The attack he demonstrated lives exactly inside this chapter's machinery: plant logic in the compiler binary that recognizes "I am compiling the compiler" and re-injects the trojan into the product — the source stays forever clean, and the trojan perpetuates itself along every edge of pcc1→pcc2→pcc3. The honesty here must be blinding: **such a trojan is fully compatible with a byte-identical fixed point; indeed, becoming the fixed point is its design goal. pcc2 == pcc3 proves the stability of self-reproduction, not the trustworthiness of the product.** If a reader comes away from this chapter believing "pcc bootstraps, therefore pcc is trustworthy," this chapter has failed.

What the fixed point actually proves — and the whole of its engineering value: the compiler source lies within its own subset; the native runtime survives its heaviest real workload; the entire pipeline is deterministic down to the byte; and all three of these hold simultaneously under five GC backends. These are coherence evidence, orthogonal to trust.

Two facts in pcc's structure **mitigate the trust problem without solving it**, and the phrasing must be exact. First, the trust root is refreshable: pcc0 is CPython interpreting auditable repository source, so anyone, at any time, can rebuild stage1 from source with an independently obtained CPython — there is no closed lineage in which "only one ancestral binary can produce the next generation," which is precisely the Thompson attack's most comfortable habitat. Second, there is partial diversity: the llvm and self emission backends each reach their own byte-identical fixed point after signature normalization (the baseline JSON records `true` for both). This is kindred in spirit to Wheeler's Diverse Double-Compiling (DDC — cross-rebuilding a suspect binary with an independent trusted compiler and comparing), but pcc **has not performed** the cross-comparison DDC requires, and therefore **does not claim** a DDC-level conclusion. This paragraph is itself an exercise in claim hygiene: every claim states what it proves and what it does not — the demand [AGENTS.md](../../AGENTS.md) makes of the whole repository, from which this book has no exemption.

## 15.9 History and Lessons

### Case study one: a causality audit of a long-green gate regression (2026-06-01)

(Source: [docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md](../../docs/investigations/bootstrap-user-function-low-ir-fallback-2026-06-01.md); the ownership-mechanics side was covered in Chapter 9 — this section covers the audit-method side.)

The long-green single-backend full-bootstrap gate suddenly failed. The report shape was not "some feature broke" but a hard error from stage1 while building pcc2: `PCC-PY-COMPILE-001 ... Python pipeline requires libpython fallback for multi-file compile (modules: pcc.frontends.python.codegen.user_function_lowering)`. The first step of the audit was to read that sentence as a mode-labeled boundary identification: the failure is at **the strict no-libpython compile time of pcc0→pcc1**, not at runtime, and the error names the module itself.

The second step was suspect ordering, not code reading: the failing module fell within the recent LowIR/layer1 split commits (the range relative to v0.1.2's `fe1de470`) — "the recent change is the prime suspect" confirmed by git-range evidence, not by feeling. The root cause: recursive LowIR helpers (`_low_ir_expr_to_value()` and friends) lacked return type annotations, so type inference produced DynType, and comparisons like `operand.ty == _LOW_F64` degraded from native integer-field reads into dynamic attribute operations, emitting `py_cpy_getattr`, `py_cpy_call1`, and the rest — **strict mode correctly rejected that IR; the gate worked as designed.** The evidence was quantitative: the contextual fallback count dropped from 80 to 0, not "it looks fine now."

The third step is the most counterintuitive item in the discipline: after the first boundary was fixed, the gate was still red — the pcc1 produced by pcc0 crashed with a double free while compiling [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py). The investigation did not knead the two events into one story; it opened a second evidence chain (an LLDB backtrace, a comparison of the generated IR) and located a generic return-ownership bug entirely unrelated to the first boundary (mechanics in Chapter 9). Two boundaries, two root causes, two fixes, two sets of regression tests.

The rejected proposals matter as much as the accepted ones, and the investigation archives them item by item: disabling automatic GC tracking of tuples — **explicitly rejected by the user**, since that trades weakened runtime semantics for a green light (discipline item 4); changing call results to borrowed — rejected, since it rewrites the global ownership contract; the `Function._fresh` error reported by a bare single-file compile probe — flagged as a misleading trace, because the bare probe handed the mixin the wrong host context, and such probes are locators only. The closure was institutional: a dedicated ON-mode fallback canary test was added for `user_function_lowering`, and the entire audit method was written back into [AGENTS.md](../../AGENTS.md) — a substantial part of the "seven-step bootstrap regression discipline" you read today is the distillate of this one investigation.

### Case study two: an rsplit reintroduces fallback; the ratchet intercepts (predates case study one)

(Source: [docs/investigations/bootstrap-types-rsplit-libpython-fallback.md](../../docs/investigations/bootstrap-types-rsplit-libpython-fallback.md))

This story is small and sharp, and it shows how the "second net" of 15.5 works. After one change, the stage1 closure **compiled successfully** — but the fallback ratchet test failed: 9 `py_cpy_*` calls appeared in the merged IR against a baseline of 0 (the error verbatim: `fallback total grew past ratchet: 9 vs baseline 0`). All 9 calls clustered inside the generated function `user_pcc_frontends_python_types__class_type_from_dotted`, traced to a single line — `name.rsplit(".", 1)`. `rsplit` had no native lowering at the time and silently took the libpython bridge.

The fix chose the smallest source-level shape: `_class_type_from_dotted` in [pcc/frontends/python/types.py](../../pcc/frontends/python/types.py) was rewritten as a single-pass scan recording the last `.` followed by explicit slicing — because the closure already supported string indexing, slicing, length, and equality natively; the only thing unsupported was the one method `rsplit`. The regression test pins the invariant at the IR layer: compile `py_ast` plus `types` as a multi-file build with `ir_scaffold_mode="on"` and `libpython_mode="off"`, and assert that the body of that generated function contains no `py_cpy_*` call.

The lesson compresses to one sentence: **"it compiled" is not a claim; the closure scan is.** The 9 calls triggered no compile-time hard error of the war-story-one kind, but a ratchet baseline of 0 means any reintroduction is a hard failure. Together the two stories prove that fallback detection must be multi-layered — the hard error intercepts structural fallback requirements; the IR-count ratchet intercepts silent seepage. With only one of the layers, the other class of regression slips into the fixed point.

## 15.10 Summary

The bootstrap fixed point is pcc's device for turning "the system is coherent" into a machine-decidable proposition. The four stages each carry their own semantics: pcc0 (the CPython host) → pcc1 proves the source lies within its own subset and the closed world holds; pcc1 → pcc2 proves the native runtime survives the load of compiling a compiler; pcc2 → pcc3 plus the byte compare proves a self-produced compiler is behaviorally self-stable — the fixed point is defined between self-produced compilers, and pcc1 is allowed to carry the foreign host's fingerprints. At the mechanism layer, `bootstrap.sh` supplies the stage machine (stale-artifact defense, the publish barrier, the three-rung verification ladder), while `cli_bootstrap.py` is a CLI that must be compilable by itself, its dialect everywhere bearing the fingerprints of the bootstrap subset. The evidence is three independent claims: zero `py_cpy_*` in the IR (closed world at the generated-code layer), no libpython linkage (independence at the artifact layer), and byte identity after signature normalization (determinism at the self-referential layer) — each locks one layer, and none implies another. pcc1/pcc2 differences are classified before they are fixed, under an eight-class taxonomy in which even *unknown* is a legitimate answer; the gate system is a three-tier pyramid with frozen JSON as authoritative state, one-way ratchets against regression, and the five-GC matrix as the heaviest completion evidence. The boundary with Thompson must stay honest: the fixed point proves coherence and determinism, not trust; the refreshable trust root and the two-backend diversity mitigate the trust problem without solving it. The two case studies squeeze the same discipline from both sides: regressions get a causality audit first, stacked failures split into two evidence chains, and semantics are never weakened for a green light; silent fallback seepage is intercepted by a ratchet whose baseline is zero.

## Exercises

1. **Read the source to verify**: `run_stage()` in [scripts/bootstrap.sh](../../scripts/bootstrap.sh) may still overwrite `stage_returncode` with 127, or with the barrier's return code, even after the compile returns 0. Work out which failure shape each of these two overwrite paths defends against, and explain why the "output file exists and is executable" check must come before `stage_exec_barrier`.

2. **Read the source to verify**: `_byte_identical_after_normalize()` in [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py) runs `codesign --remove-signature` on **copies** in a temporary directory before comparing. Drawing on the publish barrier of 15.3, explain why signature stripping must never be applied to the original `build/bootstrap-*/pcc{2,3}` files.

3. **Trace the closure**: the default `-m MODULE` path `_run_compiled_python_module_from_pcc1()` in [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) compiles a generic module into a native binary with `backend=self`, `libpython=off` and runs that, rather than delegating to host Python. Combining this with the fallback routing of Chapter 14, explain why the default path is closure-safe by construction; then explain which claim boundary the opt-in compat subprocess `_run_python_module_from_pcc1_with_mode()` protects with `PCC1_COMPAT_RUNNER_MANIFEST` — why it must announce the mode explicitly instead of quietly routing through CPython.

4. **Argue a design tradeoff**: 15.1 argued that pcc1 ≠ pcc2 is permitted. Suppose the gate were strengthened to "pcc1 == pcc2 (after signature normalization)." List at least three classes of pcc0/pcc1 execution-environment differences that would have to be eliminated first, and argue why the marginal coherence-evidence return on that investment is lower (or higher) than spending the same effort on the five-GC matrix.

5. **Argue a design tradeoff**: in case study one, adding a dedicated fallback canary for `user_function_lowering` was treated as a systemic improvement; but one canary per module makes the test count grow linearly with the number of closure modules. Design an alternative (for example, a whole-closure scan that automatically asserts zero `py_cpy_*` per generated-function prefix), analyze its detection granularity, failure-localization speed, and false-positive risk relative to per-module canaries, and state which part of this the [tests/fallback_baseline.json](../../tests/fallback_baseline.json) ratchet already covers.
