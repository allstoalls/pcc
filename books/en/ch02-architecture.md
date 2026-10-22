# Chapter 2: Architecture Overview

Chapter 1 stated pcc's thesis and its seven obligations; this chapter supplies the spatial map: which layer of the repository each of those commitments lives in, and whose hands a source file passes through on its way from the command line to an executable. pcc is a cohabitation of "two compilers and one runtime" — the mature C frontend, the experimental typed-Python frontend, and a native runtime with five GC backends — sharing a CLI, project collection, backend selection, and caching infrastructure while each owning a complete pipeline. This chapter looks only at the skeleton: one diagram per pipeline, and for every component an answer to "why does it sit here, and where is its boundary," with the details deferred to later chapters (parsing and the evaluator in Chapter 3, C semantic lowering in Chapter 4, the three tiers of the Python frontend in Chapters 5 and 6, the runtime and GC in Chapters 7 through 11, the backends in Chapters 12 and 13, the bootstrap in Chapter 15). When you finish this chapter you should be able to place any file in the repository onto this map and say which segment of which pipeline it belongs to.

## Chapter Overview: Treat the Repository as a Pipeline

This chapter is easy to get lost in because many directories appear at once. Read it as the path from source text to a runnable artifact: entry points collect inputs, frontends establish semantics, lowering produces IR, backends emit target artifacts, and the runtime carries Python object semantics.

- The C path and Python path are not islands; both eventually meet backend, linking, and runtime boundaries.
- CLI, project discovery, frontend, codegen, runtime, and backend are responsibility layers, not decorative directory names.
- When a path appears, first ask which layer it belongs to, then ask whether it is allowed to make decisions for another layer.

## 2.1 The Problem and the Design Space: Why One Repository Holds Two Compilers

Packing a C compiler and a Python compiler into the same codebase looks at first like historical baggage. It is in fact a structural choice. The three subsystems test one another and form a closed loop:

1. **The C frontend is the mature reference.** It compiles and runs real projects at the level of Lua, SQLite, PostgreSQL `libpq`, zlib, PCRE, and OpenSSL (the [README.md](../../README.md) status table), supplying the whole repository with a baseline for "what quality a compiler ought to be."
2. **The Python frontend is the bootstrap track.** It is experimental, and its goal is not to cover all of Python but to compile pcc's own source into native binaries — the `pcc0/host → pcc1 → pcc2 → pcc3` fixed point (Chapter 15).
3. **The runtime is consumed by both pipelines.** The Python path links it as an archive (`libpy_runtime*.a`). The current production `libpy_runtime_pcc_py.a` is assembled only from pcc-compiled semantic and freestanding Python objects, while the C frontend continues to compile C/oracle paths and verify the shared ABI. Both frontends therefore constrain runtime trust, but no longer share one production C implementation.

This loop explains several repository disciplines that would otherwise seem eccentric. Mode-labeled claims are not documentation politeness; they are a structural necessity. The same command, `pcc x.py`, is host pcc when it runs on the host CPython and pcc1 when it runs as a compiled artifact, and the two have different capability boundaries ([README.md](../../README.md) is explicit that pcc1 currently handles C inputs as a **compatibility shell that delegates to the host `pcc`**, not as pcc1 natively executing `c_evaluator.py`); `--python-libpython=off` and `auto` produce binaries with different dependency surfaces; `--backend llvm` and `--backend self` have different execution roots. Every component that appears in this chapter is labeled with the mode space it belongs to.

In the design space, pcc gives three coexisting answers to "in what form should a compiler exist," each with a reason the others cannot replace: a gcc-style command-line driver (interactive use, project integration); an embeddable Python library API (`pcc.build`/`pcc.module`, for host Python programs); and a bootstrap CLI that is **itself compiled**. The third is pcc's distinctive source of constraints: the CLI code is not merely "glue that calls the compiler" — it is itself a compile target, and that in turn dictates how it must be written (Section 2.2).

## 2.2 The CLI Layer: Three Entry Points and Why Each Exists

### 2.2.1 The Installed Entry Point and the click Wrapper

The `pcc` command installed by `pip install python-cc` is wired through `[project.scripts]` in [pyproject.toml](../../pyproject.toml) to `pcc.driver.cli_launcher:main`. [pcc/driver/cli_launcher.py](../../pcc/driver/cli_launcher.py) is 22 lines in total:

```python
# pcc/driver/cli_launcher.py
def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    from pcc.driver.cli_core import cli_main

    return cli_main(list(argv))
```

Its docstring states a position outright: "The public command intentionally stays on the full CPython-hosted CLI. The native bootstrap compiler is exposed separately as `pcc1`." The public command runs on the host CPython with the full CLI; the native bootstrap compiler is shipped separately as `pcc1` (the wheel build hook `hatch_build.py` self-compiles [pcc/__main__.py](../../pcc/__main__.py) to produce a native `pcc1` that travels with the wheel). The launcher does exactly one thing: forward to `cli_main` in [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py).

[pcc/pcc.py](../../pcc/pcc.py) is another thin shell: `_build_click_main()` performs a runtime `__import__("click")` and wraps `_click_entry` with click's decorators, one by one, into a command object with completion and help; when click is unavailable, it falls back to `_plain_main`, which is the same `cli_main`. The "decorators applied by hand inside a function" style is not a stylistic quirk — it makes click an optional dependency. Without it, the CLI works just the same.

### 2.2.2 The Hand-Written Argument Parser Is Written for the Bootstrap

The real parsing logic lives in `parse_cli_args` in [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py): a hand-written `while i < len(argv)` loop, two branches per flag (`--flag=value` and `--flag value`), returning one enormous tuple. No argparse, no click. The reason can be read off the details of the same file: the scoped environment-variable overrider `_temporary_env` is an explicit class rather than a `@contextmanager`, with a comment stating it is "to keep the self-host audit clean"; sequence copying uses a hand-written `_copy_seq` instead of the slicing idiom; strings are uniformly normalized with `(value or "") + ""`. These are the idioms of the bootstrap-compilable subset — `cli_core.py` belongs to the file set covered by the self-host audit ([scripts/audit_selfhost.py](../../scripts/audit_selfhost.py)) and is part of the target closure for pcc1 one day executing the C driver path natively, even though that step is not done today (the [README.md](../../README.md) status table lists it as future work).

The dispatch order of `cli_main` is itself an architecture diagram: `-m MODULE` is intercepted first (the host module runs via `runpy`, with `pip`/`pip3` rewritten to `pcc.package.pip_shim`, Chapter 17); `-h/--help` comes next; then `parse_cli_args`; finally, routing by path suffix — `.py` enters the Python pipeline, everything else the C pipeline. For a `.py` input without `-o`, the compiled artifact is written into a temporary directory and run as a subprocess, with the exit code passed through. Contrast this with the in-process MCJIT execution that is the C single-file default (Section 2.3.4): the Python path has had exactly one execution semantics from day one — a real process running a real binary.

### 2.2.3 cli_bootstrap: The CLI That Gets Compiled

[pcc/__main__.py](../../pcc/__main__.py) is a few lines of code:

```python
# pcc/__main__.py
from pcc.driver.cli_bootstrap import bootstrap_cli_sys_argv_exit


if __name__ == "__main__":
    bootstrap_cli_sys_argv_exit()
```

This is the entry to the bootstrap chain — the three stages of [scripts/bootstrap.sh](../../scripts/bootstrap.sh) compile [pcc/__main__.py](../../pcc/__main__.py). [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py) (roughly seven thousand lines) is the CLI that pcc1/pcc2/pcc3 actually run: Python inputs are compiled by the binary itself; C and project inputs are, in the words of its own help text, "delegated to the full host pcc CLI" (the host entry can be overridden with `PCC_HOST_PCC`); and a `--pytest` subcommand lets pcc1 launch the repository's test suite (it delegates to `env -u LC_ALL uv run pytest` and sets `PCC1_BINARY` so that pcc1-specific test cases get the current binary).

Why not make `cli_core` the bootstrap entry directly? Because the two have different dependency closures. `cli_core` must import `CEvaluator`, `project.py`, and the other C-path modules — a closure that cannot compile itself today; the closure of `cli_bootstrap` is deliberately narrowed to the Python pipeline plus the delegation logic. Multi-file bootstrap compilation is carried by a separate entry, [scripts/pcc_multi.py](../../scripts/pcc_multi.py) — it wraps `pipeline.compile_python_multi` and itself uses `pcc.extern` for its exit logic, written to the same "will be compiled by pcc" standard.

### 2.2.4 The Critical Flags

Three flags decide which mode space a Python compilation lands in, and every default leans toward the strict side ([README.md](../../README.md) and `pipeline.py`'s `_resolve_libpython_mode`/`_resolve_ir_scaffold_mode` agree):

| Flag | Default | Semantics |
|---|---|---|
| `--python-libpython` | `off` | `off`: any program that would need the CPython fallback is a hard error; `auto`: link `libpython` only when a fallback is detected; `on`: always allow and link the fallback surface. Environment variable `PCC_PYTHON_LIBPYTHON`. |
| `--ir-scaffold` | `on` | `on`: closed-world IR-builder lowering (the main self-host path); unimplemented methods **error clearly instead of silently falling back** (the `_resolve_ir_scaffold_mode` docstring, verbatim); `off`: the compatibility escape hatch to the older lowering path; `auto` normalizes to `on`. |
| `--backend` | `llvm` | One of `llvm`, `llvm_capi`, `self`; environment variable `PCC_BACKEND` (Section 2.5.1). |

The C path's flag family is organized around project shape: `--separate-tus`, `--sources-from-make GOAL`, `--depends-on PATH[=GOAL]`, `--system-link`, `--jobs N` (when given explicitly it must be paired with multiple inputs or system-link, otherwise it is an error), `--cpp-arg`/`--link-arg`, `--prepare-cmd`/`--ensure-make-goal`, plus the emission family `--emit-llvm/--emit-asm/--emit-obj` and the cross-compilation flag `--target TRIPLE` (`--target` must be paired with an emission mode or `--system-link`). The diagnostics surface is shared by both pipelines: `--diagnostic-format text|json|sarif`, `--profile-json PATH`, and `--explain-fallback`, conveyed through environment variables to the `observed_compile` wrapper layer in `pcc.diagnostics.compile_observability`.

One detail deserves to be called out by name: on the C path, `--backend self` clamps the default `-O2` down to 0 (`cli_core._effective_self_backend_opt_level`) unless `PCC_SELF_BACKEND_VECTORIZE` is set:

```python
# pcc/driver/cli_core.py
def _effective_self_backend_opt_level(backend, opt_level: int) -> int:
    backend_name = (backend or os.environ.get("PCC_BACKEND", "") or "").strip().lower()
    if (
        backend_name == "self"
        and int(opt_level) > 0
        and not _self_backend_vectorize_requested()
    ):
        return 0
    return int(opt_level)
```

The comment explains why: the self backend does not yet fully lower LLVM's vectorized pointer stores (such as Lua's `<4 x ptr>` strcache broadcast). This is a small specimen of "honesty before benchmarks": rather than let vectorized IR blow up on the self backend, degrade openly and leave an explicit switch.

## 2.3 The C Path: From Source Collection to Four Execution Roots

```text
pcc hello.c | pcc proj/ [--separate-tus | --sources-from-make GOAL | --depends-on ...]
        |
        v
pcc/driver/cli_core.py        cli_main -> parse_cli_args -> execute_cli
        |
        v
pcc/driver/project.py         source collection (this chapter, 2.3.1; mechanics in Chapter 3)
   merged:  collect_project()            -> one merged source, main file last
   multi :  collect_translation_units()  -> [TranslationUnit(name,path,source)...]
   flags :  collect_cpp_args()           -> -D/-I/... recovered from make dry runs
        |
        v
pcc/frontends/c/evaluator/c_evaluator.py   once per TU (--jobs process-pool parallelism;
                               on-disk artifact cache)
   _preprocess_translation_unit_source   cc -E + fake libc | built-in preprocess
   make_c_parser().parse                 -> C AST
   PassPipeline.run_high_tier            AST analysis -> PassContext
   CCodeGenerator.generate_code       semantic lowering -> LLVM IR (Chapter 4)
   postprocess_ir_text + run_low_tier    IR text post-processing
                                         (va_arg-only exemption, Chapter 12)
        |
        +---------------+----------------+---------------------+
        v               v                v                     v
   in-process MCJIT  subprocess MCJIT  --system-link        --backend self
   single-TU         Darwin multi-TU   emit_object, then    self-backend
   evaluate(),       JSON result       system cc links,     emission (Ch. 13),
   CFUNCTYPE call    handed back       real-process run     real-process run
        |
   --emit-llvm / --emit-asm / --emit-obj (emit_compiled_units; cross via --target)
```

### 2.3.1 Source Collection and the Four Compile Modes (project.py)

[pcc/driver/project.py](../../pcc/driver/project.py) turns "a path" into "the things to compile," with the output normalized to the immutable `TranslationUnit(name, path, source)`. There are four modes (the Compile Modes section of [AGENTS.md](../../AGENTS.md) is the authoritative table):

1. **Single file**: `pcc hello.c`; the whole file read in is one TU.
2. **Directory merge (merged, the default for directory inputs)**: `_collect_directory()` collects `*.c` non-recursively, sorts them, and stitches them into one large source text with `// --- filename ---` comment lines, the file containing `main()` placed last. The `main` test, `_has_main()`, does a coarse regex filter first, then a real preprocessing pass to confirm, so a `main` excluded by `#if` is not misjudged.
3. **`--separate-tus`**: the same set of files, each its own TU, linked at the module layer; exactly one `main` is enforced, and `compile_translation_units` raises on duplicate external definitions across TUs (`_raise_if_duplicate_external_definitions`).
4. **`--sources-from-make GOAL`**: dry-run archaeology against make, recovering the participating `.c` files and the `-D/-U/-I/-include` family of flags from real compile command lines. `_scan_make_goal()` tries `-n`, `-n clean`, and `-nB` in that order (the comment explains that `-nB` goes last because forcing a rebuild of every prerequisite can trigger expensive or fragile reconfiguration rules), plus `Makefile.in` detection, per-target `make -n -W src obj.o` probes, and a pure-Python Makefile-parsing fallback. The mechanics and limits are in Chapter 3, Section 3.6; it can only recover flags the build system **actually says out loud** — that boundary is the subject of the case study in 2.6.1.

`--depends-on PATH[=GOAL]` layers constraints on top of multi-input mode: the dependency inputs may contain no `main` at all, the primary input must contain exactly one; dependency units are ordered first, the primary unit last. `--prepare-cmd` and `--ensure-make-goal` run preparation commands before collection (generating headers, prebuilding libraries); `run_prepare_commands` strips `LC_ALL` from the subprocess environment.

### 2.3.2 Why Merged TU Is the Directory Default

This is the first design question the chapter blueprint names. Merged mode turns the whole directory into **one** translation unit: one preprocessing pass, one parse, one code generation, one LLVM module, with no cross-TU symbol coordination and no link layer — for a compiler that grew out of a single-file evaluator, this is the smallest possible increment toward directory support. Placing `main` last puts the entry function's call sites after the definitions in the other files, reducing the dependence on complete header prototypes; `#include "x.h"` is left to the preprocessor as usual. [docs/system-architecture.md](../../docs/system-architecture.md) positions merged mode as "fast/simple project experiments" and labels `--separate-tus` as "more realistic C semantics" — that pair of phrases is the entire tradeoff:

- Merged mode **erases TU-boundary semantics**. Two `static` functions with the same name in different files are mutually invisible in real C; merged, they become a redefinition. The reuse of a `struct` tag across files surfaces in another guise (the Common Pitfalls section of [AGENTS.md](../../AGENTS.md) lists tag reuse as a repeat offender). And the duplicate-external-definition check exists only on the multi-TU path.
- Conversely, separate mode pays coordination costs: symbol deduplication, module linking, and (on Darwin) the multi-module MCJIT lifecycle problems (2.3.4).

So the logic of the default is this: the typical scenario for a directory input is "run a handful of files as one program," and merged is the fastest and simplest way to do that; real projects should not be using the bare directory mode in the first place — they go through `--sources-from-make` (the source list is owned by the build system) or `--separate-tus` (semantic fidelity). One engineering discipline is parasitic on this default: directory mode sweeps in **every** `.c` in the directory, so never drop temporary probe files into real project directories (the environment rules in [AGENTS.md](../../AGENTS.md) prohibit it in writing).

### 2.3.3 The Evaluator: A Five-Stage Per-TU Pipeline and Its Caches

`CEvaluator` in [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) is the C path's conductor. Every TU passes through `_compile_translation_unit_artifact_job`: preprocessing (`_preprocess_translation_unit_source`, borrowing the system `cc -E` plus fake-libc shaping, or falling back to the built-in `preprocess`) → parsing (`make_c_parser().parse`) → HighTier AST analysis passes (filling a `PassContext`) → `CCodeGenerator.generate_code` semantic lowering → `postprocess_ir_text` and the LowTier IR passes. The product is a serializable artifact dictionary: `ir_text`, `return_type`, `external_defs`, `func_return_types`, and pass reports. Serializability is not decoration — it simultaneously supports the disk cache and the `ProcessPoolExecutor` cross-process parallelism behind `--jobs`.

The cache has three layers, every key mixed with `backend_signature` (the backend's identity, Section 2.5.1) and the optimization/pass signatures: the in-process `_jit_cache` (source-text hash straight to a function pointer); the native `.so` disk cache (`_build_native_cache`/`_load_native_cache`, reducing a cold start to a `ctypes.CDLL`); and the TU artifact disk cache (`_compile_cache_key`, which also folds in the compiler's own fingerprint `_compiler_cache_fingerprint()` against staleness). The layer-by-layer details are in Chapter 3; from this chapter remember only this: **the design of the cache keys is the design of the mode boundaries** — switching the backend, the pass selection, or the target triple must each invalidate naturally.

### 2.3.4 The Four Execution Roots

The same artifact list has four ways out, with the selection logic concentrated in `execute_cli` and `CEvaluator`:

1. **In-process MCJIT** (the single-file default): `evaluate()` feeds the IR to `llvm.create_mcjit_compiler`, takes the address of `main` (or any `entry`) via `CFUNCTYPE`, and calls it directly — the compiler process is the execution process. This is also the basis of the library API's `module(...)` form.
2. **Subprocess MCJIT** (the Darwin multi-TU default): on macOS, `evaluate_compiled_translation_units` switches to a spawned subprocess, `_run_linked_mcjit_worker`, with results handed back through a JSON file. The reason is written in the `finally` comment of the in-process path: "MCJIT disposal is unstable for some large multi-TU programs" — if destruction is unstable, isolate the whole engine in a disposable process; a SIGSEGV after correct output no longer takes down the host (the history is the SQLite story in Chapter 3).
3. **`--system-link`**: `run_translation_units_with_system_cc` optimizes each module with the repository-managed LLVM and emits native object files directly via `target_machine.emit_object`, then the system `cc` links them into a real executable that runs — note that the IR text does **not** pass through the system compiler's hands (the IR Fix Policy in [AGENTS.md](../../AGENTS.md) records this tightening).
4. **`--backend self`**: `_run_compiled_translation_units_self_backend`; LLVM leaves the stage entirely, the self backend emits (Chapter 13), and the result likewise runs as a real process.

`--emit-llvm/--emit-asm/--emit-obj` is a fifth, "no execution" exit: `emit_compiled_units` merges the modules with `link_in` and then emits; paired with `--target`, it is cross-compilation.

### 2.3.5 The Library API: api.py

[pcc/api.py](../../pcc/api.py) packages the C toolchain as an embeddable library: `build(sources, ..., kind="exe"|"sharedlib"|"object")` returns a `BuildArtifact` (artifact path, exported symbols, pass reports, IR text); `module(...)` is `build(kind="sharedlib")` plus `ctypes.CDLL` plus attribute binding, so that `m.add(3, 4)` calls straight into the compiled artifact. One corner is worth noticing: `Module.__getattr__` fetches symbols with the `self._lib[name]` subscript rather than `getattr`, and the comment states this is to avoid triggering the bootstrap audit's dynamic-attribute rule — while also labeling `Module` as a host-CPython integration surface: pcc's bootstrap CLI never loads shared libraries at runtime. Once again, a bootstrap constraint seeps into the writing style of code that has nothing to do with the bootstrap.

## 2.4 The Python Path: From .py to a no-libpython Executable

```text
pcc app.py [-o out] [--emit-llvm] [--backend llvm|self]
           [--python-libpython off|auto|on] [--ir-scaffold on|off|auto]
        |
        v
pcc/driver/cli_core.py (host) / pcc/driver/cli_bootstrap.py (pcc1, itself a compiled artifact)
   observed_compile(compile_python, ...)    diagnostic-format/profile/
                                            fallback-explanation wrapper
        |
        v
pcc/frontends/python/pipeline.py :: compile_python
   closure      _collect_relative_module_closure (relative imports; same-package
   collection   absolute imports when the entry is __main__; recursive in off
                mode) + recursive stdlib -> hands off to the multi-file path
   ABI check    _validate_package_site_no_libpython_abi (extension-ABI gate
                for site packages)
   parse        pcc.frontends.python.py_parse + py_lift (bootstrap-safe; the CPython ast
                escape hatch has been removed)
   type infer   type_infer.infer_module (Chapter 5)
   codegen      codegen.layer1.L1CodeGen.generate (facade + mixins, Chapter 6)
                -> LLVM IR text
   IR passes    _apply_python_ir_pass_pipeline
        |
        +-- --emit-llvm: write the .ll and stop
        v
   fallback     _module_needs_libpython (AST) + _ir_needs_libpython (scan for
   decision     py_cpy_* call sites)
                -> _finalize_libpython_mode: off = hard error (with reasons);
                   auto = as needed; on = always
        |
        v
   runtime      _ensure_runtime (PCC_RUNTIME_CC x PCC_RUNTIME_HIGH x
   archive      needs_libpython selects libpy_runtime*.a; staleness detection,
                Makefile rebuild; Chapter 14)
        |
        +-----------------------------+
        v                             v
   llvm backend: clang links     self backend: host-Python subprocess emits
   the .ll + runtime archive     asm/obj (PCC_HOST_PYTHON; pcc.backend.* stays
                                 out of the closure); system cc links;
                                 codesign -> mv -> verify -> barrier
        |
        v
   native executable (without -o, compiled into a temp directory, run as a
   subprocess, exit code passed through)
```

### 2.4.1 Entry and Closure Collection

`compile_python(src_path, out_path, ...)` is the single-file entry, but "single file" is only the shape of the request, not the shape of the compilation. It first collects the module closure: `_collect_relative_module_closure` chases relative imports; when the entry module's name ends in `.__main__`, it also pulls in same-package absolute imports; under `--python-libpython=off` it recurses over same-package absolute imports. Then `_filter_ir_scaffold_closure` filters by scaffold mode, and `_validate_package_site_no_libpython_abi` runs extension-ABI checks on sources from site packages (rejecting CPython ABI artifacts mixed into a pcc-native closure, Chapter 17). When the source uses the native stdlib in strict mode, recursive stdlib expansion is forced on — pcc's own ports under [pcc/stdlib/](../../pcc/stdlib) take priority, with the host probed only when a module is not found there (2.4.5). A closure of more than one file hands off to `compile_python_multi`, which splits the closure by module and parallelizes code generation across worker processes (`_python_frontend_jobs` defaults to automatic parallelism, capped at 10 — the comment records the measurement: on the bootstrap closure, 8 to 10 workers dominate, and at 12 the gains start losing to process and IO contention). One reflexive detail: the worker executable is resolved by `_python_frontend_worker_executable`, and in a compiled pcc1 it is pcc1 itself — **the compiled compiler re-execs itself as its own code-generation worker**.

### 2.4.2 The Three Frontend Tiers and the De-libpython-ed Parser

The single-module trunk has three tiers: `pcc.frontends.python.py_lift.parse_and_lift` (source text → pcc's own AST), `type_infer.infer_module` (type inference, Chapter 5), and `codegen.layer1.L1CodeGen.generate` (lowering to LLVM IR text; `layer1.py` has been split into a facade plus a family of mixins, Chapter 6). `pipeline.py` keeps a comment at the parse call site as a historical boundary stone: `pcc.frontends.python.py_parse + py_lift` is the bootstrap-safe parse path, and the earlier escape hatch that borrowed CPython's `ast` module "kept a libpython import edge alive in the compiled pipeline" — it has been removed. The same judgment recurs throughout: any host dependency edge left behind in the compiled artifact is a hole in the bootstrap closure.

### 2.4.3 The Fallback Decision: Two Probes and a Three-State Finalizer

The three states of `--python-libpython` converge in `_finalize_libpython_mode`, while "does this need a fallback" is decided by two probes: the AST-level `_module_needs_libpython` (do imports still go through the CPython bridge), plus the IR-level `_ir_needs_libpython` — which scans the generated IR for **call sites** of `py_cpy_*`. The comment explains why a `\bcall` pattern rather than a naive text search: the `declare` stubs for the runtime helper symbols are emitted unconditionally, and only a real call proves the artifact will actually take the fallback. In `off` mode, a positive detection raises `PyPipelineError`, with the error message carrying a list of reasons ("imports still lower through CPython fallback", "generated IR still calls py_cpy_* helpers") and suggesting `auto/on` instead. **Fail loudly, with named reasons** — this is where the obligations of Chapter 1 land on the pipeline; and the per-module fallback counts are ratchet-locked by [tests/fallback_baseline.json](../../tests/fallback_baseline.json), permitted only to go down.

### 2.4.4 The Runtime Archive and the Two Link Roots

`_ensure_runtime` selects the runtime archive along three dimensions: `PCC_RUNTIME_CC`, `PCC_RUNTIME_HIGH`, and whether the libpython bridge is needed. The default lands on `libpy_runtime_pcc_py.a`; an existing archive must pass staleness and provenance checks or be rebuilt through the Makefile. The August 2026 production rule is stronger than the old “high-level ports replace C” model: `LIB_PCC_PY` archives only `PCC_PY_OBJECTS`, including both semantic `PY_MODULES` and `FREESTANDING_PY_MODULES`. C source remains as oracle material and does not enter production merely because it exists under `src/`. Chapter 14 distinguishes no-libpython, pcc-Python runtime ownership, and Linux zero-libc.

There are two link roots, dispatched by `_link_native`: `llvm` goes through `_link_with_clang` (first passing `_clang_link_compatible_python_ir`, which downgrades the newer LLVM memory-effect attributes into a form clang can swallow); `self` goes through `_link_with_self_backend`, whose publication sequence deserves quoting in full — `codesign --force -s -` on a temporary file, an atomic rename via `/bin/mv -f`, `codesign --verify`, and finally a publication barrier (`/bin/sync` or one complete read of the artifact). The provenance of this ritual is the case study of 2.6.2. Note that the Python path's emission backends accept only `llvm` and `self` (`_resolve_native_backend` errors explicitly on `llvm_capi`): `llvm_capi` is a choice of IR-construction layer, not an emitter of Python executables.

### 2.4.5 Why Host Queries Go Through Subprocesses, Not In-Process Calls

This is the second design question the blueprint names. Every "ask the host" action in the Python pipeline is a subprocess: `_host_find_spec_origin` spawns `python3 -c` to probe a stdlib module's source path; the self backend's emitter is cross-process wholesale — `_emit_self_asm_via_host_python` writes the IR into a temporary file and uses `PCC_HOST_PYTHON` (which by default probes `.venv/bin/python3`, then falls back to `python3`) to run an inline script, `_SELF_BACKEND_HOST_CODE`, which **inside the host process** imports `pcc.backend.self_backend_dispatch` and emits assembly, the results returned via stdout/TSV files.

An in-process call would obviously be faster — why not use it? Because the compiled artifact's import closure *is* the bootstrap closure. If pcc1 imported `pcc.backend.*` in-process, those modules would instantly become "sources pcc must be able to compile," and they are not in the self-hosting subset today — the result would be `py_cpy_*` fallback edges re-entering the stage1 closure, and the no-libpython property collapsing silently. [AGENTS.md](../../AGENTS.md) writes this down as a hard rule: `_link_with_self_backend` must not reintroduce compile-stage imports or calls of `pcc.backend.*`; the long-term answer is to compile those backend modules natively, not to grow the in-process CPython fallback. The subprocess boundary pays an extra dividend: **falsifiability** — point `PCC_HOST_PYTHON` at `/bin/false`, and any plea to the host surfaces immediately. That is exactly the evidence method of the package gates (Chapter 17). The costs are real, of course: process startup overhead, and the fragility of file-and-text protocols; to amortize the former, the cross-process emitter carries an object cache keyed by a hash of the backend's source (`PCC_SELF_BACKEND_OBJECT_CACHE`).

## 2.5 The Three Backends and the Repository Map

### 2.5.1 Backend Selection: Explicit Opt-In and Cache Identity

[pcc/backend/__init__.py](../../pcc/backend/__init__.py) maintains the backend registry `_BACKEND_TABLE`: `llvm` (semver tag `llvmlite-default`, the default), `llvm_capi` (the in-repo LLVM-C construction layer, Chapter 12), and `self` (`self-aarch64-asm-v0`). In the table, `self` carries `supported: False`, yet its capability list reads `emit-asm`/`emit-object`/`run-native-via-system-cc`/`aarch64-darwin-mvp` — the combination encodes a precise meaning: "**usable, but it must be named explicitly**." By default, `resolve_backend` raises `BackendUnavailable` for an unsupported backend, and only when the user has explicitly written `self` on the CLI or in `PCC_BACKEND` (decided by `backend_request_allows_unimplemented`) does it pass. Experimental status is encoded at the level of the type system, not buried in documentation. `backend_signature` threads the backend's identity into every compilation cache key, so switching backends can never collide in the cache. The companion to all this is obligation 4's prohibition ([AGENTS.md](../../AGENTS.md)): no silent fallback to LLVM after `--backend=self` — a backend selection is a mode declaration, not a performance hint.

### 2.5.2 The Repository Map

The authoritative full table is the Repository Map in [AGENTS.md](../../AGENTS.md); the table below is this chapter's condensed view, organized by "which segment of which pipeline":

| Path | Position in the pipelines |
|---|---|
| [pcc/driver/cli_launcher.py](../../pcc/driver/cli_launcher.py), [pcc/pcc.py](../../pcc/pcc.py), [pcc/driver/cli_core.py](../../pcc/driver/cli_core.py) | Host CLI: installed entry → click wrapper → hand-written parsing and dispatch |
| [pcc/driver/cli_bootstrap.py](../../pcc/driver/cli_bootstrap.py), [pcc/__main__.py](../../pcc/__main__.py), [scripts/pcc_multi.py](../../scripts/pcc_multi.py) | Bootstrap CLI: the pcc1/pcc2/pcc3 entry and the multi-file compile entry |
| [pcc/api.py](../../pcc/api.py) | C-path library API (`build`/`module`) |
| [pcc/driver/project.py](../../pcc/driver/project.py) | C source collection: directory / merged / make dry-run / dependent projects |
| [pcc/frontends/c/evaluator/c_evaluator.py](../../pcc/frontends/c/evaluator/c_evaluator.py) | C evaluator: preprocess → parse → IR → optimize → four execution roots |
| [pcc/frontends/c/parse/c_parser.py](../../pcc/frontends/c/parse/c_parser.py), [pcc/frontends/c/codegen/c_codegen.py](../../pcc/frontends/c/codegen/c_codegen.py) | C parsing (Chapter 3) and C semantic lowering (Chapter 4) |
| [pcc/frontends/python/py_parse.py](../../pcc/frontends/python/py_parse.py), `py_lift.py`, [pcc/frontends/python/](../../pcc/frontends/python) | Python parsing/lifting, type inference, lowering (Chapters 5, 6) |
| [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) | Python pipeline conductor: closure, fallback decision, linking, publication |
| [pcc/runtime/](../../pcc/runtime) | Runtime: semantic/freestanding pcc-Python production owners + C oracles + five GCs (Chapters 7–11, 14) |
| GUI framework | Declarative GUI kernel and product canary (Chapter 20); moved to [allstoalls/pcc-gui](https://github.com/allstoalls/pcc-gui) on 2026-09-06 |
| [pcc/ir/](../../pcc/ir), [pcc/backend/](../../pcc/backend) | LLVM-C construction layer (Chapter 12) and the self backend (Chapter 13) |
| [pcc/extern/](../../pcc/extern), [pcc/unsafe/](../../pcc/unsafe) | The two tools for writing low-level code in pcc-Python (Chapter 14) |
| [utils/fake_libc_include/](../../utils/fake_libc_include) | Fake libc headers (Chapter 3) |
| [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json), [tests/fallback_baseline.json](../../tests/fallback_baseline.json) | Authoritative bootstrap and fallback baselines (Chapter 15) |

## 2.6 History and Lessons

All three stories are about **boundaries**: the first draws the line between the CLI and the build system, the second the line between the linker and the loader, and the third proves that the CLI itself stands on the compiled side of a boundary.

### 2.6.1 The Limits of make Dry-Run Archaeology: Who Owns a Flag

(Source: [docs/investigations/make-derived-cpp-flags-vs-explicit-project-config.md](../../docs/investigations/make-derived-cpp-flags-vs-explicit-project-config.md), first committed 2026-03-28.)

**Symptom and question.** Once pcc gained `--cpp-arg` and the make-derived flag capability, an unavoidable asymmetry appeared: `--sources-from-make` could cover PCRE completely, but not Lua, zlib, or SQLite — the latter three still required hand-supplied flags. It looked like an incomplete inference implementation.

**The evidence chain.** The investigation checked the dry-run output of all four projects item by item. PCRE's compile command lines really do contain `-DHAVE_CONFIG_H -I.`; the inference "is not guessing, just reusing build inputs the project itself declares." Lua's `make -n lua` output has `-DLUA_USE_LINUX` but not the `-DLUA_USE_JUMPTABLE=0` and `-DLUA_NOBUILTIN` that pcc needs — because those two are not decisions of Lua's build system at all, but **compatibility choices** made by the pcc user to route around compiler paths pcc does not yet support. zlib's tree was unconfigured; its top-level Makefile only prints "Please use ./configure first." — there are no compile commands to excavate. SQLite's command shape is `--depends-on .../sqlite3.c`; there is not even a make goal.

**The real root cause.** Not incomplete inference, but a problem wrongly lumped into one class. The rule the investigation distilled became the design axiom of this layer: "Build-system-derived inference can only recover configuration that the build system has already made concrete." Explicit `-D` flags were then split into two classes: **build-configuration flags** (the `HAVE_CONFIG_H` family, whose ideal source is post-configure build metadata) and **compiler-compatibility flags** (the `LUA_NOBUILTIN` family, which belong to pcc's current limitations and should be explicit, visible, and documented).

**The invariant left behind.** The investigation spells out, in writing, what would constitute a regression in design quality: "if the path contains `sqlite`, add `-DSQLITE_THREADSAFE=0`" — injecting flags by project name. This prohibition is the same principle as obligation 3's ban on `if package == "numpy"`, projected onto the C side. The layering as it now stands: the build system owns project configuration, the CLI exposes explicit user choices, and the compiler implements C semantics. A compatibility flag has only one legitimate way to disappear: fix the compiler, then delete the flag from the command line.

### 2.6.2 A Freshly Linked Mach-O Cannot Be Exec'd Immediately: Publication Is a Pipeline Stage

(Source: [docs/investigations/self-backend-mach-o-stage-publish-race.md](../../docs/investigations/self-backend-mach-o-stage-publish-race.md), updated through 2026-05-15; status: active follow-up remains.)

**Symptom.** During development of GC backend #4 (of the five), the strict bootstrap gate regressed intermittently: `bootstrap.sh` exited with code 139 — stage2 had just produced `pcc2`, and stage3, executing it immediately, took a SIGSEGV. Yet the very same binary **succeeded when rerun**, and succeeded under LLDB.

**Wrong hypothesis and its falsification.** Intermittent failure plus the bootstrap chain — the first instinct is a Python-frontend semantic error or heap corruption introduced by the GC changes. The two pieces of evidence, success-on-rerun and success-under-LLDB, rule out a stable semantic bug: the problem points at the publication boundary, "freshly linked executable → immediate exec."

**Root cause and the fix chain.** The self backend's link path originally let `cc` write the final output path directly. On macOS arm64, exec'ing a file whose contents are in place but whose loader/signature state has not yet settled produces exactly this shape. The fix was forced out step by step, each rung with a recorded failure: atomic rename alone (`mv -f`) was not enough; rename plus ad-hoc signing reduced the failure rate but still reproduced; the boundary that finally held was **forced system verification after signing** — the current sequence in `_finish_self_backend_executable`: `codesign --force -s -` on the temporary file, publication via `/bin/mv -f`, `codesign --verify` to force the loader's side to observe the final Mach-O, plus one more publication barrier.

**Architectural reflexivity.** Midway, an `os.replace()`-based atomic publish was attempted — and rejected, because the strict bootstrap immediately reported a no-libpython fallback appearing in `pcc.frontends.python.pipeline`: `pipeline.py` must itself be compiled by pcc1, so the idioms available to it are constrained by the very gate it guards. The final implementation had to take the already-supported subprocess boundary (`/bin/mv`). The repair technique is selected by the architecture of the thing being repaired — a closed loop peculiar to self-hosting systems.

**The honest ending.** The investigation's 2026-05-15 update records that even with `--verify` in place, one more stage3 crash reproduced — and its crash report pointed at `py_decref`, not the loader. The publication-boundary fix remains useful, but the "stage3 crash class" has not been proven closed; the follow-up was handed to a separate investigation. Half the value of a case study is the fix; the other half is refusing to record "the symptom disappeared" as "the root cause is closed."

### 2.6.3 Four Fallbacks in a Two-Line Entry Script: The CLI Is Itself a Compile Target

(Source: [docs/investigations/python-pcc-main-static-export-cli-bootstrap.md](../../docs/investigations/python-pcc-main-static-export-cli-bootstrap.md), 2026-05-28, resolved.)

[pcc/__main__.py](../../pcc/__main__.py) is two lines of code: import `bootstrap_cli_sys_argv_exit`, call it. Compiled standalone, however, it emitted 4 `py_cpy_*` calls — `ensure_init`, `import`, `getattr`, `call_noargs`: a complete "import the function through CPython, then call it" fallback chain. The root cause is banal in an instructive way: `pcc.driver.cli_bootstrap` was on the **consumer whitelist** for static native modules, but had no corresponding entry in the **export table**, so the symbol could not bind; and `pcc.__main__` itself was registered in neither table. The fix was to add a function export for `bootstrap_cli_sys_argv_exit` to `layer1_support.py` and register `pcc.__main__`; the module's fallback count in the baseline went 4 → 0 and was locked there by [tests/fallback_baseline.json](../../tests/fallback_baseline.json). Two lessons. First, in a no-libpython architecture, **an entry script is not configuration; it is a compile target** — two lines of code must pass the closure audit like everything else. Second, the value of the fallback ratchet is precisely that it leaves "files that could not possibly have a problem" nowhere to hide — those 4 fallbacks, had they not been counted per module, would have hidden forever inside a binary that linked successfully.

## 2.7 Summary

pcc's architecture compresses into four sentences. **The two pipelines share one shell:** the CLI, project collection, the backend registry, and the cache and diagnostics layers are common; C and Python part ways at the path-suffix split in `execute_cli`. **The C path is characterized by the diversity of its execution roots:** the same TU artifact can be called directly under in-process MCJIT, isolated into a subprocess, linked by the system cc into a real program, or handed to the self backend — and the differences between modes (merged vs. separate, JIT vs. link) are not implementation details but semantic boundaries, with bugs taking different shapes on either side. **The Python path is characterized by closures and the fallback decision:** from module closure collection, through the dual-probe fallback detection and the three-state `--python-libpython` convergence, to the runtime-archive matrix and the publication ritual, every segment answers the same question — what, exactly, does this binary depend on? **The bootstrap drags the CLI itself into the set of compile targets:** the hand-written argument parser, the explicit context-manager class, the subprocessed host queries, the attribute access rewritten by audit rules — all of these "inelegances" are the price of a single constraint: every line of the compiler's driver code will eventually be compiled by the compiler itself. The chapters that follow zoom in along these two pipeline diagrams, segment by segment.

## Exercises

1. **(Source reading)** Trace the complete call chain of `pcc hello.c -- a b`: from `cli_core.cli_main` to the line `fptr(argc, argv)` inside `CEvaluator.evaluate()`. At which layer does `prog_args` turn from a Python list into C's `argv` array? Why does a call carrying `prog_args` never enter `_jit_cache`? (Read the fast-path condition in `evaluate()` and give the semantic reason.)
2. **(Source reading)** `project.py::_scan_make_goal` orders its three dry-run attempts as `-n`, `-n clean`, `-nB`, and the comments give the reason `-nB` goes last. Construct a minimal Makefile for which the `-n` dry run produces no compile commands while `-n clean` does (hint: targets already up to date); then explain what extra cost `-nB` would incur on that project.
3. **(Design tradeoff)** List two classes of bugs that are exposed only in merged mode and behave correctly in separate mode, and two classes exposed only in separate mode and masked by merged mode (hints: `static` linkage; the effect of `// --- filename ---` concatenation on line-number diagnostics; on which path `_raise_if_duplicate_external_definitions` actually runs). From this, argue why "merged mode runs" cannot serve as a claim that "this project is supported."
4. **(Experiment)** Write a small program that triggers the libpython fallback (for instance, by using an unsupported dynamic idiom), and compile it with `--python-libpython=off` and with `auto` plus `--explain-fallback`. Against the source of `_finalize_libpython_mode`, explain which probe produces each of the two possible entries in the reasons list; then use `--emit-llvm` to verify that the `py_cpy_*` call sites scanned by `_ir_needs_libpython` really exist.
5. **(Design tradeoff argument)** Suppose someone proposes that pcc1 simply `import pcc.backend.self_backend_dispatch` and emit assembly in-process, saving the subprocess overhead. State which gate this change would breach **first** (name the concrete test file), and argue why "bringing `pcc.backend.*` into the self-hosting subset" is the right path while "growing the in-process fallback" is not — your argument should cite obligation 4 from Chapter 1 and the evidence method of Section 2.4.5 (`PCC_HOST_PYTHON=/bin/false`).
