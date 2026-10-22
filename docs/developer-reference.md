# Developer reference

Compiler modes, packages, runtime controls and subsystem notes.
For the introductory example, start with the [README](../README.md).

## Installation and packages

### PyPI and native installation boundaries

`python-cc` is the distribution name; `pcc` is its host-Python command, while
`pcc1` is a separately built native compiler. Installing a distribution does
not by itself establish either native self-hosting or full compatibility.
The current source's release wheel hook requires both a matching runtime
archive and a native `pcc1`; failure is fatal. It defaults to owned construction
and does **not** retry LLVM/clang. Editable development installation skips those
release-only builds. There is no `python-cc[no-libpython]` extra.

Release wheels are tagged `py3-none-<platform>` (`macosx_12_0_arm64`,
`manylinux_2_17_x86_64`, `manylinux_2_17_aarch64`, `win_amd64`); each bundles
the `pcc1` its release job built in Stage1 and passed through the installed-wheel
gate (`.github/workflows/workflow.yml`). A platform without a published wheel
falls back to the sdist, whose build fails within hatch's 10-minute pcc1
self-compile unless `PCC_BUILD_PCC1` names an already verified pcc1.

The macOS arm64 source installer in
[`scripts/install_pcc1_toolchain.py`](../scripts/install_pcc1_toolchain.py) stages
and validates a toolchain before activation. Its help was checked on the host;
an installation was not executed in this audit. Do not treat historical
installation receipts as a qualified compiler for the current source.

### One installed compiler for multiple projects

`pcc-gateway` and `pcc-gui` use **`~/.local/bin/pcc1` through PATH**. This is the
stable installation entry; a core checkout's `build/bootstrap/pcc1` is a
development artifact and must not be used as the shared installation.

Versioned toolchains live under `~/.local/share/pcc/toolchains/`. Each candidate
contains its native compiler, matching runtime and helper sources, and
validation receipts. Build and test a candidate in its own directory; only
after qualification may the stable entry switch atomically. Keep the previous
toolchain for rollback. Rebuilding the core or another application must not
overwrite the installed toolchain in place.

Only use this shared entry after its source, runtime and native execution
have been qualified. An arbitrary existing `pcc1` on PATH is not evidence of
a current installation. The source-based quick start below does not use it.

The September 6 installation receipt records the **v84 historical baseline**,
not the 0.1.8 release candidate. It does not establish today's installed compiler.
See [historical investigation context](status.md).
`scripts/install_pcc1_toolchain.py` implements staged initial installation and
refuses to replace an
existing entry. Use `--stage-only` to prepare a later candidate. The same tool's
`--qualify`, `--promote` and `--rollback` commands bind validation receipts to a
versioned installation and preserve the previous version during atomic
switching; `--help` documents the receipt protocol. Qualification requires the
default and integration suites, fresh pcc1/bootstrap checks and clean package
installation. New application features may need that newer compiler.

The native command surface is inspectable through its help once a matching
compiler is available. Interactive REPL support remains unimplemented; the
source emits an explicit unsupported-capability diagnostic.

### Install application packages into one environment

The compiler installation and third-party packages have different lifetimes.
Use the same package command for local framework checkouts and registry
packages; manually copying folders into site-packages is not the normal
installation workflow.

The native package-install command surface exists, but clean acquisition,
installation and native application execution are separate gates and were not
rerun for this README audit. A successful install alone does not qualify an
application. Environment selection is implemented with these rules:

| Environment | Package installation and import location |
|---|---|
| Ordinary shell, no virtual environment | `~/.local/share/pcc/environments/<compatibility-tag>/site-packages` |
| Active `VIRTUAL_ENV` | That virtual environment's pcc overlay; inspect `selected_site_packages` with `python3.15 -m pcc env info --json`. |
| Explicit `PCC_ENVIRONMENT` / data-root override | The explicitly selected pcc environment, reported by `env info`. |

Run installation and compilation in the same environment. `uv run` or an
activated Python virtual environment can change `VIRTUAL_ENV`, so it can
select a different pcc package site. Local-directory installation and registry
installation must obey this equally. `PCC_PACKAGE_SITE` is an explicit extra
source-search override for development, not a separate package manager.

The default native environment is identified by pcc's ABI and target platform,
without a Python version in the directory name. For example,
`pcc_native_v1-arm64_apple_darwin-pcc_native` means:

| Component | Meaning |
|---|---|
| `pcc_native_v1` | Version 1 of pcc's native runtime ABI |
| `arm64_apple_darwin` | macOS on arm64 |
| `pcc_native` | Native package ABI mode |

The source reports **Python 3.15** as its semantic target; it is reported separately
as compatibility metadata and its qualification is still in progress. A later
Python target such as 3.16 can retain the native package path when pcc's ABI
and platform remain compatible. Python-dependent lock selections are refreshed
when the target changes. Explicit CPython compatibility modes keep versioned
environment identities because their extension ABI is version-sensitive.

The build host's Python version is separate. Users normally use pcc's default;
`PCC_PACKAGE_TARGET_PYTHON` is an explicit package-selection override, not a
switch that implements another Python runtime. Existing `py311` environments
remain untouched and can be selected explicitly with `PCC_ENVIRONMENT`; use
the matching package-install command surface for the selected environment.
A target version does not claim that
every Python feature or CPython extension ABI is implemented.

`python3.15 -m pcc env info` is authoritative for the actual path. Current GUI/gateway
source-install and NumPy native integration remain under qualification. Installation success alone does not prove application execution or full
NumPy compatibility; see their separate validation boundaries below.

## Compiler modes and tools

### Command modes

- `python3.15 -m pcc` runs the compiler on host CPython (`pcc0`). An installed
  `pcc` launcher enters the same dispatcher.
- `pcc1` is a native build of the compiler. It requires its own matching build
  and execution receipts. Creating a Stage1 binary does not establish a
  self-hosted fixed point.
- For Python inputs, `-o PATH` writes an executable for explicit execution.
  Prefer this form while compile-and-run and module-runner boundaries are
  still being qualified.
- `--backend self` is the public backend selection. `--backend llvm` is invalid.
  `--emit-llvm=PATH` emits IR only; it proves neither linking nor execution.
- `--python-libpython off` is the default strict mode. `auto` and `on` are
  explicit compatibility surfaces and cannot support a no-libpython claim.
- `--ir-scaffold on` is the default compiler-IR lowering path. It does not mean
  that all Python behavior is supported. Capability gaps should produce an
  explanatory diagnostic; silent fallback is outside the intended contract.

### Runtime archive

The first native Python compilation builds pcc's runtime library and caches it
at `pcc/runtime/build_owned/<target>/<configuration>/libpy_runtime_pcc_py.a`.
`<configuration>` is `single-atomic`, or `threads-atomic` with
`PCC_WITH_THREADS=1`. A matching packaged runtime, when installed, is used
first. To build the archive ahead of time at a chosen path:

```bash
uv run python -m pcc.frontends.python.owned_runtime_build \
  --output build/runtime/libpy_runtime_pcc_py.a
PCC_RUNTIME_ARCHIVE="$PWD/build/runtime/libpy_runtime_pcc_py.a" \
  uv run pcc hello.py -o hello
```

Keep the provenance sidecar files next to the archive. pcc rejects an explicit
archive whose source, codegen, target or configuration (`PCC_WITH_THREADS`,
`PCC_REFCOUNT_KIND`) does not match the compile. This builds the runtime
library only, not `pcc1`.

### Python library and inspection tools (in progress)

Native self-hosting and five-GC correctness remain the primary qualification
work. Host APIs, package tooling and embedding have their own execution
boundaries. `import pcc` alone is not evidence that those capabilities run.

The direct host binding generator was executed on this source using the
following small LP64 prototype. Run from the source checkout; the output
folder is fresh so no existing file is overwritten:

```bash
work="$(mktemp -d "${TMPDIR:-/tmp}/pcc-bindgen.XXXXXX")"
printf 'int abs(int value);\n' > "$work/api.h"
python3.15 -B -m pcc.frontends.c.bindgen \
    "$work/api.h" -o "$work/bindings.py" --target x86_64-unknown-linux-gnu
cat "$work/bindings.py"
```

This generates `pcc.extern` declarations for compilation. It does not load C
functions into CPython or prove native `pcc1 bindgen` execution. The public
`pcc bindgen` and `pcc inspect` commands route through the native module runner;
they are not interchangeable with directly calling their host modules.

`pcc.inspect_artifact` reads thin little-endian Mach-O64 and the supported
ELF64 x86-64 object/static-executable subset without executing the artifact.
It reports declared load dependencies, not transitive dependencies, runtime
`dlopen`, or build ownership. Dynamic ELF/PIE and universal Mach-O remain
unsupported. `pcc.generate_bindings` accepts expanded LP64 scalar/pointer
prototypes; preprocessing, aggregate values and callbacks need other support.
See [inspect #195](https://github.com/allstoalls/pcc/issues/195),
[bindgen #196](https://github.com/allstoalls/pcc/issues/196), and
[embedding #197](https://github.com/allstoalls/pcc/issues/197).

### C compilation and ownership boundaries

The C frontend, C build/module APIs, object writers and explicit host-link
routes are present in source. Their tests and historical real-project runs
are separate from the Python quick start. C input examples require actual C
sources and their project-specific corpus/build prerequisites. The repository includes
the Lua, PCRE and SQLite sources used by historical examples; their corpus
commands have not been revalidated for this revision.

The current dispatcher enters the C frontend in process. This source route is
not yet proof that a freshly built native `pcc1` executes the entire C closure.
An explicit host-link or compatibility route cannot satisfy an owned/native
claim. No current-source external LLVM backend is offered by the CLI.

The default owned runtime builder, emitter and linker must be evaluated as a
complete path. A matching runtime manifest verifies source, codegen, target and
configuration; the absence of `libpython` says nothing by itself about libc,
OS APIs, compiler ownership or performance. Read
[compiler contracts](compiler-contract.md) for the required boundaries.

Import search starts at an entry file's directory and walks to a recognized
project root (`pcc-package.json`, `pyproject.toml`, `setup.py`, or `.git`).
`PCC_PACKAGE_SITE` and the selected package environment are additional source
locations. Every imported package still has to fit the implemented semantics
and ABI; discovery of a package is not compatibility proof.

### NumPy and other packages

Historical NumPy 2.4.x experiments cover narrow import/version, construction,
scalar-addition and element-access shapes. They do not qualify the artifacts
now selected for a Python 3.15 target, all NumPy operations, general dependency
resolution, PEP 517 isolation or arbitrary CPython extensions. Current-source
clean acquisition/build/import and native application execution remain open.

Package acquisition, environment selection, extension building and native
import are distinct mechanisms. CPython-ABI artifacts remain rejected in
pcc-native mode. The relevant integration gates are
[`test_numpy_l4_pcc1_gate.py`](../tests/integration/test_numpy_l4_pcc1_gate.py),
[`test_numpy_l5_pcc1_gate.py`](../tests/integration/test_numpy_l5_pcc1_gate.py), and
[`test_pcc1_default_package_environment.py`](../tests/integration/test_pcc1_default_package_environment.py).

## Architecture

The complete overview fits the page width by default. Click the diagram to
open the scalable image. The CPU frontends share an IR/backend layer; the
accelerator lane remains a separate, bounded path. The dashed link is the
explicitly enabled libpython compatibility bridge.

[![pcc architecture: C and typed Python frontends, shared backends, native runtime, self-host chain and bounded Metal path](architecture/pcc-overview.svg)](architecture/pcc-overview.svg)

See the [detailed pipeline and repository map](architecture/01-overview.md)
for the implementation behind each layer.

| Layer | Main paths | Role |
|---|---|---|
| CLI | `pcc/driver/cli_launcher.py`, `pcc/__main__.py`, `pcc/driver/cli_bootstrap.py`, `pcc/driver/cli_core.py` | Shared public dispatcher and full C/project option routing. |
| Public API | `pcc/api.py`, `pcc/frontends/c/evaluator/c_evaluator.py` | Embeddable C build/evaluate/module APIs. |
| Project collection | `pcc/driver/project.py` | Directory scanning, make-derived source sets, dependency projects, TU setup. |
| C frontend | `pcc/frontends/c/lex/`, `pcc/frontends/c/parse/`, `pcc/frontends/c/codegen/`, `pcc/frontends/c/evaluator/` | C preprocessing, parsing, semantic lowering, execution/emission. |
| Python frontend | `pcc/frontends/python/`, `pcc/frontends/c/parse/py_*` | Python parse/lift, type inference, native lowering, CPython fallback decisions. |
| Runtime | `pcc/runtime/`, `pcc/extern/`, `pcc/unsafe/` | Runtime objects, extern-C bridge, low-level intrinsics. |
| Backends | `pcc/ir/`, `pcc/backend/` | Owned IR construction, optimization and native backend. |

See [AGENTS.md](../AGENTS.md) for the full repository map and maintainer workflow.

## Capabilities

### C frontend

The C frontend implements C99-oriented parsing
and semantic lowering; scalars, pointers, arrays, structs, unions, enums,
typedefs, function pointers, control flow, casts, arithmetic, bitwise/shift ops,
and variadics; preprocessing with macro expansion and conditional compilation;
merged-directory builds, separate translation units, make-derived source
selection, dependency projects, compile caching, and host linking; LLVM IR /
owned object / assembly / executable workflows; and explicit signedness
tracking on top of LLVM integer types (compile-time constant evaluation and
runtime lowering as separate semantic paths).

The repository includes the historical Lua/PCRE example sources. Those
commands remain unverified for this revision. Check each project's source
configuration, Make requirements and explicit host-tool/reference boundaries
before running its C corpus gate.

### Python frontend

The Python frontend is experimental. Its source and test corpus cover typed
functions/locals, dynamically represented values, classes, exceptions, dunder
methods and selected standard-library paths. Coverage of a type or construct
in some tests is not full compatibility for every combination of Python
behavior. Direct C declarations use `pcc.extern`; low-level runtime code uses
`pcc.unsafe`. Explicit libpython compatibility modes are separate from the
strict native goal.

Compiling the compiler itself stresses a much larger closure than a small
application. Current bootstrap gaps must be reproduced against the exact
source; older subset restrictions are not a substitute for those results.
The semantic requirements remain in [Project Intent](project-intent.md).
NumPy's historical package-boundary work includes the
[target-replay investigation](investigations/m2-numpy-package-target-replay.md);
the intentional CPython-ABI extension rejection is tested in
[`test_package_extension_abi.py`](../tests/python/test_package_extension_abi.py).

The current CLI routes C/project inputs to the full C frontend in the same
process. That implementation change still needs execution through a matching
native `pcc1`; host dispatch or parser tests cannot establish it.

### Self backend

The in-tree backend is the public default, and `self` is the only accepted
backend choice. It contains target-specific emitters, object writers and
linkers; target support must be qualified by actual emitted execution, not
by accepting a target triple or writing an object file. The current-source
native evidence above is scoped to x86_64 Linux host-pcc0 compilation.

LLVM's published IR semantics and historical reference comparisons inform the
implementation. That does not provide an available `--backend llvm` route,
universal target coverage, or a current-source self-hosting result.

### Freestanding runtime and libc ownership (in progress)

`--python-libpython=off` means the emitted program does not embed or bridge to
CPython. It does **not** by itself mean that the program is libc-free. pcc's
stronger ownership target is to compile allocation, object headers, atomics,
syscalls, threads, safepoints, stack maps, all five GC implementations, dynamic
loading, and extension ABI entrypoints from freestanding pcc-Python. Raw memory,
atomic, syscall, and host-ABI operations remain compiler-owned machine
intrinsics; existing C and vendored libc implementations remain differential
oracles rather than the intended production runtime.

The historical zero-libc artifact is deliberately narrow: in host-pcc0
Python-frontend, x86_64 Linux self-backend, no-libpython mode,
`freestanding_linux_start.py` produces a static ELF process-entry tracer with a
pcc-Python `_start`, raw `write`, and raw `exit_group`. Its gate observes no
`PT_INTERP`, no `DT_NEEDED`, no undefined symbols, and no hand-written C,
assembly, or libc runtime object. This does not yet prove the complete Python
runtime, five-GC matrix, C frontend, or a pcc1 Linux cross-compile. See the
[bounded evidence](https://github.com/allstoalls/pcc/blob/2574f5857e89ed177b1b704409d6f05d577c15da/docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md)
and the [remaining runtime-closure investigation](investigations/freestanding-runtime-final-no-c-closure.md).

The platform goals are intentionally different: the Linux
static closure targets zero C/libc runtime dependencies, while Darwin may call
an explicitly enumerated libSystem ABI and must not be described as zero-libc.

### GPU kernels (Metal, experimental)

macOS/Metal only, requiring the Xcode Metal toolchain; a missing toolchain or
device is an explicit skip, never silent success.

Annotate a kernel with `@gpu.kernel` and select the Metal backend; compilation
emits the host executable plus a `.metallib` sidecar. The supported subset is
small (elementwise vector-add-shaped kernels) and lowers through the canonical
route `Kernel IR → validate_kernel() → TIRx-compatible freeze → Metal finalize →
launch package`, not ad-hoc AST-to-Metal translation.

```python
# vec_add.py
from pcc import gpu

@gpu.kernel
def add(a: gpu.ptr_f32, b: gpu.ptr_f32, out: gpu.ptr_f32, n: gpu.u32):
    i = gpu.thread_id_x()
    if i < n:
        out[i] = a[i] + b[i]
```

This is a source illustration, not an executed host program or a current
GPU launch receipt. Compilation and on-device launch were not rerun in this
Linux audit.

The `pcc.kernel_ir` library API builds kernel modules directly for shapes the
decorator subset does not cover (tiled/simdgroup GEMM, split-K with atomics,
transposed operands, edge tails). **TVM / TIRx / TileLang are semantic
references, never runtime dependencies** — pcc does not import, link, or execute
TVM, TileLang, or torch anywhere on this route (the same "oracle, not owner"
rule the self backend applies to LLVM). Usable seams: `import_tilelang_source()`
parses a strict TileLang Python-DSL subset into Kernel IR (unknown constructs
fail closed); `lower_to_plain_tir()` freezes tile primitives to a TIRx-shaped
plain-TIR form; `project_to_tir_shape()` is a golden comparison oracle with no
TVM import. GPU evidence is claim-leveled (`GPU_LEVEL_0`..`GPU_LEVEL_6`); the
route contract and level definitions are in
[docs/design/pcc-gpu-next-work.md](design/pcc-gpu-next-work.md).

Kernel/oracle tests live in `tests/kernel`; actual device gates live in
`tests/gpu_hardware`. Read their hardware and opt-in requirements before
selecting them. Deselection or a skipped device gate is not execution proof.

### Native GUI (macOS, experimental)

The declarative GUI framework (layout, elements and controls, window events,
binding, text and images, theme/animation, composition-tree kernel, components,
keyed render/commit, compiled style utilities, typed commands, application
lifecycle) and its macOS canary, a dual-pane file comparison app, now live in
https://github.com/allstoalls/pcc-gui. It is an ordinary pcc package: an
application does `import pcc_gui` and `pcc1` compiles the framework into the
program. A matching compiler and separately installed framework are
prerequisites; current-source native GUI execution is not qualified here. Its
Metal/AppKit window bridge (`pcc_gui/native/pcc_gui_metal_render_bridge.m`) is
maintained in that repository; the core has no GUI code. The GUI still uses AppKit,
Metal, libSystem and a clang-built Objective-C bridge dylib, so **no-libpython
GUI does not mean zero-libc GUI**.

## Bootstrap

The required chain is:

```text
host CPython runs pcc0 -> pcc1
pcc1 compiles the same source -> pcc2
pcc2 compiles the same source -> pcc3
compare pcc2 and pcc3 byte-for-byte; they must be identical
```

[`scripts/bootstrap.py`](../scripts/bootstrap.py) implements staged construction,
reuse and comparison. Its `--help` was executed against this source. Help and
source support for `--stage`, `--from-stage`, `--out-dir` and `--reuse-stage1`
do not establish that those builds complete on every platform. Full bootstrap
is substantial qualification work, not part of the minimal Python example.

A Stage1 artifact must first compile and execute the maintained native controls.
Only then can Stage2/Stage3 and a fixed point be claimed. No such complete
current-source result is claimed here. Historical macOS arm64 receipts in
[`tests/bootstrap_gate_baseline.json`](../tests/bootstrap_gate_baseline.json) and
[`tests/fallback_baseline.json`](../tests/fallback_baseline.json) retain their
original source and mode boundaries.

The native `--pytest` subset is different from host pytest; it does not provide
all pytest option semantics or replace the default/integration release suites.
Use the maintained full-GC bootstrap tests under [`tests/python/gc/`](../tests/python/gc/)
only with matching staged artifacts and the validation workflow's resource gates.

## Garbage collection

pcc ships **five GC backend slots**, informed by reference
implementations documented in tree under
[docs/refs_docs/gc-research/](refs_docs/gc-research/) so the algorithm reads
alongside pcc's port. Select one at process start with a single value, for example
`PCC_GC_BACKEND=0`; valid values are `0`, `1`, `2`, `3`, and `4`.
`PCC_GC_BACKEND=0..4` is not a valid shell range or collector selection.
Backend #0 is the default and rollback reference.

| Slot | Algorithm | Reference | Status |
|---|---|---|---|
| **#0** | refcount + STW cycle | CPython | Default/reference collector; selected current-source regressions passed. Full runtime qualification remains open. |
| **#1** | incremental tricolor mark-sweep | Lua 5.4 | Selectable; selected regressions passed. Pacing, finalizers/resurrection and broader workloads remain separate gates. |
| **#2** | concurrent mark-sweep | Go (greentea) | Selectable; selected regressions passed. Concurrent work-buffer, sweep and long-running behavior need broader proof. |
| **#3** | generational young/old | OCaml | Selectable; selected regressions passed. Cross-domain/threaded graphs and workload performance remain open. |
| **#4** | colored relocating / GenZGC | ZGC (OpenJDK) | Selected aggregate regressions passed; older concurrency crashes remain open. Historical bootstrap/long-run receipts do not establish current crash-free operation. |

The per-backend bootstrap matrix is intended to check all five against the
selected source; its current-source execution is still required.
The runtime also
ships a long-running measurement surface — pause count/sum/max + histogram, RSS
and allocator heap bridges, and four steady-state workloads under
[benchmarks/python/](../benchmarks/python/) — because the north-star obligation is
efficiency **over time**, not single-shot speed. Historical runs helped identify frontend ownership leaks and GC4 defects;
those fixes do not close the remaining concurrency and retention work.

**Threading:** the threaded runtime uses atomic refcounts and native thread
primitives without a CPython GIL. Real parallel execution, object-lifetime
correctness and performance still require their own workload gates. The [threading shim](../pcc/stdlib/threading.py) is backed by
`pthread_*`, and [`boc.py`](../pcc/stdlib/boc.py) provides behavior-oriented
concurrency (`Cown` + a `locked` context manager that acquires cowns in
canonical order). This ordering addresses that lock-acquisition pattern; it
is not a general deadlock-freedom guarantee for applications. Historical
threaded speedups are workload/platform-specific and were not remeasured here.

**Compatibility beyond bootstrap:** finalizers, resurrection, weakrefs,
suspended coroutine frames, native handles and threaded object graphs have
their own backend-specific gates under `tests/python/` and
`tests/python/gc_production_contract/`. A compiler fixed point is not proof of
all those application behaviors. Keep C-runtime and pcc-Python-runtime results,
and single-threaded and `PCC_WITH_THREADS=1` results, separately labeled.

## Virtual threads, effects, and proof checks

An active track to make suspended continuations, scheduler queues, timer/IO
waitsets, and GC roots explicit enough that every park/resume path can be checked
against the runtime contract. The implementation registers continuation and scheduler queues as
GC-visible roots for the collectors, with an opaque-handle register
API and bounded ready/waiter/timer/IO node pools. A
[2026-07-16 C-runtime gate](https://github.com/allstoalls/pcc/blob/2574f5857e89ed177b1b704409d6f05d577c15da/docs/goal/evidence/2026-07-16-vthread-1m-production-runtime.md)
completed one million virtual-thread objects across GC0..4 on macOS arm64,
including timers and 1,000 waiters on one pipe. That receipt does not prove a
million sockets, the pcc-Python runtime's equality, or arbitrary application
suspension. Current plain-`def` may-park call sites and external gateway
execution remain under correctness investigation.

A small executable category/effect/proof checker (`pcc/diagnostics/contracts/category.py`,
`pcc/diagnostics/contracts/runtime_effects.py`) models runtime composition and classifies ABI calls
(GC barriers, frame/continuation roots, park/resume, GPU boundaries) as effect
events. It supports scoped proof-carrying claims but is not a dependent-type
proof system and does not prove the compiler correct. Remaining work is tracked
as [GitHub issues](https://github.com/allstoalls/pcc/issues) labelled
`task-board` (the migrated `T-P0-VTHREAD-*` and `R-P1-*` rows).

## Testing

Development tests require [uv](https://docs.astral.sh/uv/) and the locked dev
dependencies (`pytest`, `pytest-xdist` and the other entries in `pyproject.toml`).
`uv sync --locked` is the source-development setup, not a qualified PyPI/native
installation. That dependency-install step was not rerun for this audit.
Use the pinned interpreter in `.python-version`.

The project defaults to six workers and `-m 'not integration'`. An integration
file path does not override that marker: explicitly select `-m integration`.
In particular, the old SQLite command without that selection only exercised a
collection check, not the runtime integration tests. External project corpora,
hardware and opt-in fixtures have additional prerequisites; missing ones may
cause deselection and must be reported separately from executed passes.

Follow [validation-workflow.md](validation-workflow.md) for frozen sources,
matching runtime/compiler artifacts, the shared heavy-build lock, time/RSS
bounds, live logs and complete node accounting. Use `-x` for host pytest;
diagnosis normally also uses `-n0 -vv --tb=short`. GNU `timeout` is the Linux
command; macOS needs an explicitly provisioned equivalent such as coreutils'
`gtimeout`. The former blanket timeouts and full-suite snippets are not a
portable recipe or an already completed release gate.

Only a final successful pytest summary establishes a completed selected gate.
Collection, dots, deselected tests and an unfinished CI run are not passes.
The [Status](verification-status.md) section separates executed, failed and still-open work.

## Benchmarks

Tooling lives under `benchmarks/`. `bench_pcc1.py` measures an existing native
compiler. Its linkage probe rejects a detected `libpython` dependency unless
explicitly allowed, but an unavailable/failing platform linkage tool can leave
the result unknown. Benchmark success alone is not a no-libpython proof.

The benchmark script's `--help` was executed, but no benchmark was run in this
audit. Supply a matching, already qualified native compiler to `--pcc1`;
a default or historical `build/bootstrap/pcc1` path is not proof of identity.
`--include-self-compile` builds pcc2 and checks its help; it does not prove that
pcc2 can compile another program or establish a three-stage fixed point.

Measure a program compiled by `pcc` against CPython with
`benchmarks/bench_py_runtime.py`. Current Python-frontend runtime speed is not
yet a CPython replacement; this bench guards the unboxed-loop and startup work.
Performance claims should be scoped to a benchmarked workload class and record
correctness, fallback mode, allocation behavior, timing, and whether the binary
linked `libpython`.

## Repository map

| Path | Role |
|---|---|
| `pcc/driver/cli_launcher.py`, `pcc/__main__.py`, `pcc/driver/cli_bootstrap.py` | Installed host command, module entrypoint and shared dispatcher. |
| `pcc/api.py`, `pcc/driver/project.py` | C build/module APIs and source collection. |
| `pcc/frontends/c/evaluator/c_evaluator.py`, `pcc/frontends/c/codegen/c_codegen.py` | C compile/evaluate/link and main C lowering. |
| `pcc/frontends/python/` | Python type inference and native lowering. |
| `pcc/runtime/` | Runtime archive sources (C) and pcc-Python ports. |
| GUI framework | Moved to https://github.com/allstoalls/pcc-gui (`import pcc_gui`, compiled by pcc1 with the application). |
| `pcc/backend/`, `pcc/ir/` | Owned native backend and shared IR infrastructure. |
| `pcc/kernel_ir/`, `pcc/gpu_gc/`, `pcc/dist/` | GPU kernel IR, GPU-GC seam, local-only distributed oracles. |
| `pcc/extern/`, `pcc/unsafe/` | Python→C extern decls and low-level intrinsics. |
| `utils/fake_libc_include/` | Fake libc headers used by the C frontend. |
| `mac_diff_app`, `harness` | GUI examples, now under `examples/` in https://github.com/allstoalls/pcc-gui. |
| `tests/`, `projects/`, `benchmarks/` | Regression/corpus/integration tests, stress targets, perf tooling. |

## Environment controls

CLI flags are preferred where an option has both CLI and environment forms.

General compiler:

| Variable | Values | Effect |
|---|---|---|
| `PCC_BACKEND` | `self` | Owned backend used when `--backend` is unset. |
| `PCC_PYTHON_LIBPYTHON` | `auto`, `on`, `off` | Default Python fallback policy; unset means `off`. |
| `PCC_IR_SCAFFOLD` | `off`, `on`, `auto` | Default for the closed-world Python IR scaffold; unset means `on`. |
| `PCC_COMPILE_CACHE_DIR` / `PCC_DISABLE_COMPILE_CACHE` | path / truthy | Override or disable the TU compile cache. |
| `PCC_USE_PLY_C_PARSER` | `1` | Use the legacy PLY C parser instead of the native one. |

Runtime, GC, and bootstrap:

| Variable | Values | Effect |
|---|---|---|
| `PCC_GC_BACKEND` | `0`..`4` | Select the GC backend at startup: 0 refcount+cycle (default), 1 incremental, 2 concurrent, 3 generational, 4 colored-relocating. |
| `PCC_WITH_THREADS` | `1` | Select threaded runtime construction. The owned builder separately records `PCC_REFCOUNT_KIND` (default `atomic`). Match both when reusing an archive. |
| `PCC_RUNTIME_BUILD` | `owned`, `make` | Defaults to owned runtime construction. `make` is an explicit CPython-host reference route, rejected by native pcc1. |
| `PCC_RUNTIME_ARCHIVE` | absolute archive path | Checkout-built archives must match source, codegen, target and configuration. Installed-wheel archives use their verified wheel stamp and target/configuration checks. |
| `PCC_HOST_PYTHON` | command | Explicit host/compiler-worker configuration; inspect the executed route before making native ownership claims. |
| `PCC_BOOTSTRAP_OUT_DIR` | path | `scripts/bootstrap.py` output directory. |

`PCC_RUNTIME_CC` and `PCC_RUNTIME_HIGH` are not current runtime-owner
selectors. `PCC_WITH_LIBPYTHON` is a C preprocessor definition used by the
explicit compatibility archive target, not a general environment toggle.

Pass and diagnostic controls (`PCC_DISABLE_PASSES`, `PCC_DUMP_BAD_IR`,
`PCC_DEBUG_*`, `PCC_PROBE_*`, …) are documented in [AGENTS.md](../AGENTS.md).

### Locale troubleshooting

Ordinary `uv sync` and `uv run` commands do not require clearing `LC_ALL`.
Keep a working locale unchanged. If a command reports a locale or encoding
error, compare that command with `env -u LC_ALL ...` and check `locale` and
`locale -a` for settings supported by the host. Use the override only when it
resolves the diagnosed problem; `LANG` or other `LC_*` settings may also need
correction. Python's UTF-8 mode does not guarantee that native tools accept
the same locale settings.

## Documentation

Current work is tracked as
[GitHub issues](https://github.com/allstoalls/pcc/issues). Read
[`docs/knowledge/`](knowledge/README.md) before changing the compiler: it
holds the denied experiments, the confirmed root causes and the symptom routing
distilled from [`docs/investigations/`](investigations/INDEX.md). The
retired goal protocol is archived under
[`docs/archive/goal/`](archive/goal/); see
[`docs/goal/README.md`](goal/README.md) for what moved where.

| Topic | Path |
|---|---|
| Architecture background | [docs/system-architecture.md](system-architecture.md) |
| Python tutorial / how-to / older limitation notes (check their dates) | [docs/python-tutorial.md](python-tutorial.md), [docs/python-howto.md](python-howto.md), [docs/python-limitations.md](python-limitations.md) |
| Python semantic contract / historical NumPy work | [Project Intent](project-intent.md), [NumPy target-replay investigation](investigations/m2-numpy-package-target-replay.md) |
| Freestanding runtime / libc ownership | [docs/investigations/freestanding-runtime-final-no-c-closure.md](investigations/freestanding-runtime-final-no-c-closure.md), [Linux zero-libc tracer evidence](https://github.com/allstoalls/pcc/blob/2574f5857e89ed177b1b704409d6f05d577c15da/docs/goal/evidence/2026-08-03-linux-zero-libc-python-start.md) |
| Declarative GUI and macOS canary | https://github.com/allstoalls/pcc-gui (`docs/gui-declarative-absorption.md` there) |
| GPU route contract and Kernel IR | [docs/design/pcc-gpu-next-work.md](design/pcc-gpu-next-work.md), [docs/design/pcc-kernel-ir.md](design/pcc-kernel-ir.md) |
| Investigation reports | [docs/investigations/](investigations/) |
| Contributor / agent notes | [AGENTS.md](../AGENTS.md) |

The design and implementation are also written up as a book (Chinese and
English, 18 chapters + appendices) under [books/](../books/).

## Development

Source metadata requires Python >=3.13; current development and this README's
host checks use CPython 3.15.0rc1. Read [AGENTS.md](../AGENTS.md),
[Project Intent](project-intent.md) and the relevant validation workflow
before changing frontend, codegen, runtime or bootstrap behavior.

Compiler changes should include a minimized regression and the original
integration scenario. Preserve unsupported and unrun boundaries in reports;
source code, current configuration and executed artifacts take precedence over
old README claims or historical receipts.
