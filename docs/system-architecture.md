# pcc System Architecture

This document explains how `pcc` is structured as a compiler and build toolchain, which subsystems own which responsibilities, and how source code moves from input files to executable output.

`pcc` is a Python-implemented compiler centered around two frontends:

- a **C frontend** built on preprocessing + parsing + semantic lowering to LLVM IR
- an **experimental Python frontend** that lifts Python source into a typed internal AST and lowers that through LLVM as well

The project is intentionally more than a toy compiler: it includes project collection, multi-translation-unit builds, system-link workflows, a pass framework, runtime support, compile caching, benchmark harnesses, and large third-party integration tests.

---

## 1. Architectural goals

`pcc` is optimized for a particular style of development:

1. **Use Python for compiler implementation velocity**
2. **Use LLVM for backend quality and portability**
3. **Keep the pipeline inspectable** at every stage
4. **Validate against real software**, not only tiny toy inputs
5. **Support both interactive use and batch/project builds**

That leads to a layered architecture where orchestration, frontend semantics, optimization, and final code emission are clearly separated.

---

## 2. High-level view

```text
                    +----------------------+
                    |   CLI / Python API   |
                    | pcc.pcc / pcc.api    |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    | Project collection   |
                    | pcc.driver.project          |
                    +-----+-----------+----+
                          |           |
                 C input  |           |  Python input
                          v           v
              +----------------+   +-------------------+
              | C frontend     |   | Python frontend   |
              | preprocess     |   | parse             |
              | parse          |   | type infer        |
              | codegen        |   | LLVM codegen      |
              +--------+-------+   +---------+---------+
                       |                     |
                       +----------+----------+
                                  |
                                  v
                    +------------------------------+
                    | Pass framework + LLVM backend |
                    | High / Mid / Low / Backend   |
                    +---------------+--------------+
                                    |
                                    v
                  +------------------------------------------+
                  | Execution / emission targets             |
                  | MCJIT | system cc | object | asm | llvm  |
                  +-------------------+----------------------+
                                      |
                                      v
                  +------------------------------------------+
                  | Tests, integrations, benchmarks          |
                  | tests/ projects/ bench/ benchmarks/      |
                  +------------------------------------------+
```

At a high level, `pcc` has five major layers:

| Layer | Primary paths | Responsibility |
|---|---|---|
| Entry surfaces | `pcc/pcc.py`, `pcc/api.py` | CLI UX and Python API |
| Build orchestration | `pcc/driver/project.py` | Collect files, infer source sets, prepare make-driven builds |
| Frontends | `pcc/frontends/c/evaluator/`, `pcc/frontends/c/codegen/`, `pcc/frontends/python/` | Parse and lower C/Python into LLVM IR |
| Optimization / analysis | `pcc/frontends/c/passes/`, parts of `pcc/frontends/c/codegen/` | AST analysis, IR shaping, LLVM pipeline selection |
| Runtime / validation | `pcc/runtime/`, `tests/`, `projects/`, `bench*/` | Runtime support, correctness validation, performance measurement |

---

## 3. Entry surfaces

### 3.1 CLI: `pcc/pcc.py`

The `pcc` command is the main operator-facing interface.

It is responsible for:

- dispatching between **C mode** and **Python mode** based on input file type
- normalizing CLI options such as:
  - optimization level
  - `--separate-tus`
  - `--system-link`
  - `--sources-from-make`
  - `--depends-on`
  - `--cpp-arg`
  - `--link-arg`
  - `--emit-obj` / `--emit-asm` / `--emit-llvm`
- calling project/source collection helpers
- invoking the evaluator or Python pipeline

### 3.2 Library API: `pcc/api.py`

The public Python API exposes two high-level surfaces:

- `build(...)` — compile one or more C sources into an object, shared library, or executable
- `module(...)` — compile C code and load it into Python through `ctypes`

This layer turns the compiler into an embeddable toolchain instead of a CLI-only program.

### 3.3 Programmatic evaluator: `pcc/frontends/c/evaluator/c_evaluator.py`

`CEvaluator` is the central orchestration object for the C toolchain.

It owns:

- preprocessing and parser setup
- compilation of single or multiple translation units
- LLVM optimization dispatch
- MCJIT execution
- object emission and system-link runs
- compile cache integration

> Note: the directory name `evaluater/` is historical and kept for compatibility.

---

## 4. Build orchestration and source collection

### 4.1 `pcc/driver/project.py`

This module decides **what to compile** before the compiler decides **how to compile it**.

It supports several source collection modes:

| Mode | Description | Typical use |
|---|---|---|
| Single-file | compile one `.c` file | small programs, reduced reproducers |
| Directory merge | concatenate a directory into one large TU | fast/simple project experiments |
| `--separate-tus` | compile each `.c` independently | more realistic C semantics |
| `--sources-from-make GOAL` | derive the real source set from `make -n` | existing upstream projects |
| `--depends-on PATH[=GOAL]` | build a driver file plus dependency sources | library integration tests |
| `--prepare-cmd` / `--ensure-make-goal` | run preparatory steps | generated headers, configured trees |

This separation is important because large-project support in `pcc` is not just “the compiler parses bigger files”. It also includes a lightweight project-collection layer that can follow real build systems.

### 4.2 Translation-unit model

`project.py` uses a small immutable `TranslationUnit` structure:

- `name`
- `path`
- `source`

That keeps the rest of the pipeline independent from filesystem scanning details. Once translation units are collected, later stages work with explicit source payloads.

---

## 5. C frontend architecture

The C frontend is the most mature part of the system.

### 5.1 Pipeline overview

```text
C source / project input
  -> project collection
  -> preprocessing
  -> parsing to C AST
  -> HighTier analysis into PassContext
  -> semantic lowering in CCodeGenerator
  -> IR post-processing / metadata
  -> LLVM optimization
  -> MCJIT execution or object emission / system link
```

### 5.2 Preprocessing

Primary logic lives in `pcc/frontends/c/evaluator/c_evaluator.py`.

`pcc` can use the host C preprocessor (`cc -E`) while steering it toward a pycparser-friendly environment via:

- shipped fake libc headers in `utils/fake_libc_include/`
- user include directories
- explicit `--cpp-arg` flags
- make-derived CPP flag collection from `pcc/driver/project.py`

This hybrid approach is one of the reasons `pcc` can handle larger real-world projects than a purely toy frontend.

### 5.3 Parsing

Core parser-related code lives under:

- `pcc/frontends/c/parse/`
- `pcc/frontends/c/lex/`
- `pcc/frontends/c/ast/`
- vendored `pcc/frontends/c/ply/`

The parser produces the C AST that later semantic/codegen stages consume.

### 5.4 Semantic lowering: `pcc/frontends/c/codegen/c_codegen.py`

`CCodeGenerator` is the main semantic engine for C.

This module owns the hard parts of C lowering, including:

- type handling and layout-sensitive decisions
- expression lowering
- integer signedness tracking beyond raw LLVM integer bit widths
- control flow lowering
- global/local initialization
- aggregate handling
- constant-expression evaluation
- debug metadata hooks

A key design detail is that **LLVM integer types alone are not enough to preserve C signedness semantics**. `pcc` therefore tracks signed/unsigned intent explicitly in codegen state and helper functions.

### 5.5 Compile-time semantics

`_eval_const_expr()` inside `pcc/frontends/c/codegen/c_codegen.py` is a semantic subsystem in its own right.

This matters because correctness bugs can appear in either of two places:

- runtime lowering to LLVM IR
- compile-time folding of constants/macros/casts

Large-project regressions frequently depend on both being correct.

---

## 6. Pass framework and optimization tiers

The pass framework is implemented in `pcc/frontends/c/passes/`.

The project explicitly models optimization in four tiers:

| Tier | Responsibility | Typical implementation |
|---|---|---|
| HighTier | read-only AST analysis | fills `PassContext` |
| MidTier | better codegen decisions | codegen consumes `PassContext` |
| LowTier | LLVM IR text shaping / metadata | IR post-processing |
| BackendTier | LLVM module optimization | O1/O2/O3 or explicit LLVM pipelines |

### 6.1 Why this layering exists

Instead of treating “optimization” as only a backend concern, `pcc` splits it into:

- **source-aware analysis** before lowering
- **semantic-aware lowering choices** during codegen
- **IR cleanup/metadata** after codegen
- **LLVM backend optimization** at the end

This lets the project ask more precise questions such as:

- what improvements come from `pcc`'s own passes?
- what improvements only appear after LLVM O2/O3?
- which high-tier analyses are worth their compile-time cost?

### 6.2 LLVM pipeline control

`pcc` can:

- use llvmlite's standard optimization profiles
- expand LLVM `default<O1/O2/O3>` into explicit textual pipelines
- allow targeted pass disabling/enabling for experiments

Relevant code lives in:

- `pcc/frontends/c/passes/llvm_text_pipeline.py`
- `pcc/frontends/c/passes/llvm_builtin_registry.py`
- `pcc/frontends/c/passes/registry.py`

---

## 7. LLVM backend, execution, and emission

The LLVM backend is orchestrated mostly from `pcc/frontends/c/evaluator/c_evaluator.py`.

### 7.1 Execution modes

`pcc` supports multiple ways to consume generated LLVM IR:

| Mode | How it works | Best for |
|---|---|---|
| MCJIT in-process | parse IR and execute directly | small programs, evaluator usage |
| MCJIT via subprocess | isolate Darwin teardown issues | multi-TU runs on macOS |
| system-link | emit objects, link with host `cc` | large real projects, realistic binaries |
| object / asm / llvm emission | emit build artifacts | debugging, inspection, cross-compilation workflows |

### 7.2 Why `--system-link` exists

MCJIT is convenient for interactive evaluation, but large multi-object programs often want normal host linking behavior. `--system-link` lets `pcc` behave more like a real compiler driver:

1. compile each TU to object code
2. call the host linker/compiler driver
3. run the final binary

This is especially important for projects like PostgreSQL `libpq` and nginx.

---

## 8. Caching model

`pcc` has two distinct cache layers for C execution.

### 8.1 In-memory JIT cache

Inside `CEvaluator`, repeated execution of the same source/entrypoint can reuse:

- execution engine
- parsed module
- function pointer

This is the fast path for repeated evaluator calls in one Python process.

### 8.2 On-disk compile cache

`pcc` also keeps a translation-unit disk cache keyed by:

- source content after preprocessing context is accounted for
- compiler fingerprint/version inputs
- optimization/pass signature

The goal is to skip repeated front-end work across process boundaries and repeated project builds.

---

## 9. Python frontend architecture

The Python frontend is under `pcc/frontends/python/` and `pcc/runtime/`.

It is more experimental than the C frontend, but it already has a clear architecture.

### 9.1 Pipeline overview

```text
Python source
  -> stdlib ast parse
  -> lift into pcc Python AST
  -> annotation-driven type inference
  -> LLVM IR generation
  -> clang link
  -> py_runtime archive
  -> optional CPython embed flags when imports are present
```

### 9.2 Main modules

| Path | Responsibility |
|---|---|
| `pcc/frontends/python/parser.py` | parse Python using stdlib `ast`, lift into frozen internal AST |
| `pcc/frontends/python/py_ast.py` | internal Python AST model |
| `pcc/frontends/python/type_infer.py` | assign/refine types across the AST |
| `pcc/frontends/python/codegen/layer1.py` | lower typed Python AST to LLVM IR |
| `pcc/frontends/python/pipeline.py` | orchestrate parse → infer → codegen → link |
| `pcc/runtime/` | native runtime support archive |
| `pcc/extern/` | direct extern-C bridge for pure native calls |
| `pcc/stdlib/` | pcc-side stdlib shims/helpers |

### 9.3 Native path vs CPython fallback

The Python frontend intentionally supports two execution styles:

- **typed/native path**: annotated Python lowers directly to LLVM IR without a PyObject runtime
- **CPython fallback path**: if the program uses `import`, the final link can embed libpython and route dynamic behavior through CPython support shims

This gives the project a practical continuum instead of forcing a false choice between “fully static subset” and “full Python semantics immediately”.

### 9.4 `pcc.extern`

`pcc.extern` is a key bridge for pure-native Python programs.

It allows code like:

- declare a C symbol
- give it a type signature
- call it from Python source
- lower it to a direct native call in LLVM IR

That is the basis for the project's self-hosting and FFI-oriented experiments.

---

## 10. Runtime and support libraries

### 10.1 Fake libc headers

`utils/fake_libc_include/` provides a minimal libc header surface that keeps preprocessing/parser flows manageable.

This layer is not just a convenience. It is part of the frontend compatibility contract for many C inputs.

### 10.2 Python runtime archive

`pcc/runtime/` builds `libpy_runtime.a`, which provides the runtime symbols used by the Python frontend's generated programs.

### 10.3 System libraries

For C builds and `module(...)`, system libraries are linked explicitly via normal link arguments such as:

- `-lz`
- `-lm`
- project-provided static archives

This keeps `pcc` aligned with conventional toolchain behavior.

---

## 11. Validation architecture: tests, projects, and benchmarks

A major part of `pcc`'s architecture is outside the compiler core.

### 11.1 Focused tests: `tests/`

The test suite covers several layers:

- parser and semantic regressions
- minimized bug reproductions
- project integration harnesses
- external corpus comparisons against native compilers

Important suites include:

- `tests/test_c_testsuite.py`
- `tests/test_clang_c.py`
- `tests/test_gcc_torture_execute.py`
- `tests/test_lua.py`
- `tests/test_sqlite.py`
- `tests/test_postgres.py`
- `tests/test_nginx.py`
- `tests/test_zlib.py`, `tests/test_lz4.py`, `tests/test_zstd.py`, `tests/test_pcre.py`
- Python corpus and runtime tests under `tests/py_corpus/`

### 11.2 Real project trees: `projects/`

`projects/` contains vendored or staged third-party software used as realistic stress targets, such as:

- Lua
- PCRE
- zlib
- SQLite
- PostgreSQL
- nginx
- OpenSSL
- lz4 / zstd / readline

This is a deliberate part of the development model: semantic bugs are often discovered through real programs first, then minimized into focused regressions.

### 11.3 Benchmarks

There are two main benchmark layers:

- `bench/` — microbenchmark matrix and pass analysis
- `benchmarks/` — standalone executable suite, compile/exec/total metrics

These measure both correctness-adjacent behavior and optimization cost/benefit.

---

## 12. Repository map

The following map is the fastest way to orient yourself in the repo:

| Path | What it is |
|---|---|
| `pcc/pcc.py` | CLI entrypoint |
| `pcc/api.py` | public Python build/module API |
| `pcc/driver/project.py` | source collection and build orchestration |
| `pcc/frontends/c/evaluator/c_evaluator.py` | C compilation / execution coordinator |
| `pcc/frontends/c/codegen/c_codegen.py` | core C semantic lowering |
| `pcc/frontends/c/passes/` | pass framework and LLVM pipeline control |
| `pcc/frontends/c/parse/`, `pcc/frontends/c/lex/`, `pcc/frontends/c/ast/` | C parser frontend pieces |
| `pcc/frontends/python/` | Python frontend |
| `pcc/runtime/` | Python runtime archive |
| `pcc/extern/` | extern-C bridge for Python frontend |
| `pcc/stdlib/` | pcc-side stdlib support |
| `utils/fake_libc_include/` | fake libc headers |
| `tests/` | regression and integration tests |
| `projects/` | real third-party project inputs |
| `bench/`, `benchmarks/` | benchmark harnesses |
| `docs/investigations/` | deep dives into real debugging sessions |
| `docs/plans/` | design/planning documents |

---

## 13. Extension points

If you want to extend `pcc`, these are the main seams:

| Goal | Where to start |
|---|---|
| Add a CLI workflow | `pcc/pcc.py`, `pcc/driver/project.py` |
| Fix C semantic lowering | `pcc/frontends/c/codegen/c_codegen.py` |
| Add/adjust C parsing | `pcc/frontends/c/parse/`, `pcc/frontends/c/lex/` |
| Add source-aware optimization | `pcc/frontends/c/passes/` + codegen hooks |
| Add Python syntax support | `pcc/frontends/python/parser.py` |
| Improve Python typing | `pcc/frontends/python/type_infer.py` |
| Improve Python codegen | `pcc/frontends/python/codegen/layer1.py` |
| Add native runtime helpers | `pcc/runtime/`, `pcc/extern/`, `pcc/stdlib/` |
| Add project integrations | `projects/` + `tests/test_<project>.py` |

---

## 14. Current trade-offs and boundaries

`pcc` is powerful, but its architecture also reflects explicit trade-offs:

- The **C frontend is the most production-like subsystem**.
- The **Python frontend is experimental** and intentionally staged.
- Separate-TU builds do **not** currently imply cross-translation-unit optimization.
- Some project compatibility still depends on explicit build flags recovered from upstream makefiles or passed by hand.
- Fake-libc and host-toolchain interaction are part of real-world correctness, not incidental implementation details.
- On macOS, some large MCJIT flows are isolated in subprocesses because of LLVM/llvmlite teardown behavior.

Those trade-offs are visible in the code because the repository is optimized for correctness debugging and architecture iteration, not just for presenting a minimal polished surface.

---

## 15. Suggested reading order

If you are new to the project, this sequence works well:

1. `README.md`
2. `AGENTS.md`
3. `pcc/pcc.py`
4. `pcc/driver/project.py`
5. `pcc/frontends/c/evaluator/c_evaluator.py`
6. `pcc/frontends/c/codegen/c_codegen.py`
7. `pcc/frontends/c/passes/__init__.py`
8. `docs/investigations/` for real debugging case studies

For Python frontend work, add:

1. `docs/python-tutorial.md`
2. `docs/python-howto.md`
3. `pcc/frontends/python/pipeline.py`
4. `pcc/frontends/python/parser.py`
5. `pcc/frontends/python/type_infer.py`
6. `pcc/frontends/python/codegen/layer1.py`

---

## 16. One-sentence summary

`pcc` is a Python-implemented compiler platform whose architecture deliberately combines frontend semantics, build orchestration, pass experimentation, LLVM backends, and large-project validation into one inspectable system.
