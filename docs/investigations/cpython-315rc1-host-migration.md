# Investigation: migrate pcc's default host to CPython 3.15 (rc1, then 3.15.0)

## Status

resolved

## Problem Description

The user requires pcc to track the newest Python and explicitly directs the
repository's host Python to move from CPython 3.13.2 to CPython 3.15.0rc1.
This is a direct migration, not an optional side matrix.  The no-libpython
pcc1 ownership boundary remains unchanged: host 3.15 is the pcc0 builder and
oracle, not a runtime fallback for pcc1.

The current project `.venv` is CPython 3.13.2.  CPython 3.15.0rc1 is not
installed locally.  `pyproject.toml` permits Python >=3.13, but the lock pins
`llvmlite==0.46.0` and currently records wheels only through CPython 3.14, so
dependency compatibility must be resolved explicitly rather than hiding a
3.13 interpreter behind `uv run`.

## Repro

```text
.venv/bin/python --version
Python 3.13.2

python3.15 --version
command unavailable
```

Required result: repository-default `uv run python` and `.venv/bin/python`
report exactly 3.15.0rc1, the locked toolchain imports and focused host/compiler
gates pass, and build receipts name the 3.15 executable/runtime identity.

## Test [CONFIRMED]

The version mismatch above is directly observed.  Dependency and compiler
compatibility tests remain pending until the new interpreter is installed.

## Proposals

- No.1 Install 3.15.0rc1 and atomically replace the project environment [CONFIRMED]
- No.2 Move the host and oracle from 3.15.0rc1 to 3.15.0 [CONFIRMED]

## No.1 Install 3.15.0rc1 and atomically replace the project environment

### Code Change

The local uv 0.10.2 download catalogue predated CPython 3.15rc1.  It was
checksum-verified and upgraded to uv 0.12.7, whose managed catalogue contains
3.15.0rc1.  The exact managed interpreter is pinned in `.python-version` and
the final `.venv` was created in place from it; the old 3.13.2 environment is
only a recoverable build artifact and is not on the project execution path.

MLX has no cp315 wheel or sdist and is not imported by pcc core, so it moved to
an explicit `mlx` extra that fails clearly when requested on an unsupported
interpreter.  llvmlite 0.46's build hook is incompatible with Python 3.15;
llvmlite 0.47 builds from sdist against LLVM 20 and passes a parse/verify probe.
The host/oracle identity moved to CPython 3.15.0rc1 while pcc1's honest language
baseline remains 3.13 until the separate compatibility task proves more.

### CONFIRMED

```text
uv run python -VV
Python 3.15.0rc1 ... [Clang 22.1.3]

C parser                              44 passed
llvm_capi end-to-end                   7 passed
Python multi-file                     41 passed in 101.91s
replacement contract/workloads       47 passed
```

A source-frozen GC0 Stage1 ran under the signed 3.15 Tachyon profiler and
published a complete build receipt:

```text
artifact             build/host-python-315rc1-stage1-profile-v2
host cache tag       cpython-315
host version         3.15.0rc1 candidate 1
pcc1 SHA-256         09d131f88355d1e33e1f020e291c5830d4499cc08fa99fc920edd3dd8646f0e8
linkage              libSystem only
profiled wall/CPU    354.92s / 1212.90s
```

The profiled wall is diagnostic, not the uninstrumented Stage1 performance
baseline.  Tachyon captured 227 process flamegraphs; the aggregate receipt is
`build/host-python-315rc1-tachyon-v2/aggregate.json`.

## Report

The repository-default host and oracle are now exact CPython 3.15.0rc1.  No
3.13/3.14 interpreter or package directory participates in `uv run`, Stage1,
or the focused gates.  This closes host migration only; it does not relabel
pcc1's current language subset as Python 3.15 compatibility.

## No.2 Move the host and oracle from 3.15.0rc1 to 3.15.0

### Code Change

CPython 3.15.0 was released (`3.15.0 (main, Oct 14 2026, 00:56:12) [Clang
23.1.3]`); the maintainer directed the local host to move on 2026-10-20.
The uv 0.12.7 download catalogue predated it; `uv self update` installed uv
0.13.3. `uv python install 3.15.0` retargets the
`cpython-3.15-macos-aarch64-none` minor-version link, so every environment
created with `--python 3.15` moves with it; 3.15.0rc1 stays installed.
`024bfe68a` changes `.python-version`, AGENTS.md, the macOS CI interpreter,
the worker-prior gate's identity and the replacement contract's exact
oracle version. `1bc90dde1` then removes every per-job interpreter choice:
uv reads `.python-version`, setup-python uses `python-version-file`, and the
gate derives the exact `sys.version_info` from `.python-version` on all four
platforms (Linux and Windows had run CPython 3.13.16). The Docker wheel
checks keep `python:3.13` images: they install on the requires-python floor.

Between the tags (331 commits) the public headers change only
`patchlevel.h`, a `modsupport.h` macro semicolon and a MinGW ARM64 thread-id
read; no file pcc derived (`_pydatetime.py`, `html`, `tomllib`) changed.
Eleven fixes touch modules that pcc provides; netrc (gh-155468, an empty
quoted token is not end of input) and configparser (gh-156353, whitespace
delimiters) change observable behavior and are followed in `1e5c7cec5`.

### CONFIRMED

```text
uv lock --check / uv sync --locked --dev   clean; sync installs nothing
runtime archive, source 00b172c3d          rc1 d0c1738c... = 3.15.0 d0c1738c...
                                           (cold builds; capi_syms identical)
focused host files under 3.15.0            1105 passed
oracle-delta program                       rc1 and 3.15.0 differ in exactly
                                           four lines; providers = 3.15.0
Stage1 under 3.15.0 (smac7, 406c1321a)     rc 0, 22 m 46 s (rc1 host: 22 m 24 s)
                                           pcc1 bf8773cd..., runtime a4617931...
worker-prior gate node, that pcc1          1 passed in 96.67 s (budget 400 s)
```

The two deselected host failures (`test_intent_constraints::
test_every_obligation_has_constraints`, `test_native_sysconfig::
test_sysconfig_get_config_var_dispatches_to_compiled_stdlib_provider`) fail
identically at `00b172c3d` under rc1.

### Open

The first CI run with 3.15.0 on Linux and Windows is 39451814729 at
`1bc90dde1`. The CPython conformance `Lib/test` checkout is
`/Users/jiamo/pcc-conformance/cpython-v3.15.0` (tag `v3.15.0`, `d220409`).
