# Post-Bootstrap Native Stdlib Plan

**Status:** proposed follow-on plan  
**Start condition:** begins after P6C.6 stage-2/stage-3 bootstrap is
repeatable enough that stdlib work is no longer blocked on basic
self-host bring-up  
**Problem statement:** today most `import` statements still lower through
`py_cpy_import(...)`, so compatibility is high but the semantic
authority remains CPython for imported modules

## Goal

Make a curated subset of Python standard-library imports resolve and run
through `pcc`'s own frontend + runtime + native module loader, without
linking `libpython`.

This plan is intentionally narrower than "replace the whole CPython
stdlib". The target is:

- self-host-safe stdlib support for the subset `pcc` itself needs
- a growing allowlist of native-capable stdlib modules for user code
- a clear import boundary where unsupported modules still fall back to
  CPython on purpose

## Non-goals

- Not a full CPython stdlib reimplementation.
- Not a C-extension ABI replacement.
- Not support for import hooks, `zipimport`, dynamic code loading, or
  arbitrary plugin/module discovery.
- Not removal of CPython fallback for every `import`.

## Current state

Today the Python pipeline has these relevant properties:

- plain `import` / `from ... import ...` still lower through
  `py_cpy_import` unless the module is compile-time-only or a known
  native sibling in multi-file mode
- `compile_python_multi(...)` already supports native sibling
  cross-module imports and can stay libpython-free for those cases
- `pcc/stdlib/` already contains replacement modules, but they are
  not yet treated as first-class native import targets during normal
  lowering
- many `pcc/stdlib/` modules are still `skeleton` or `stub` surface
  areas rather than production-ready implementations

This means `pcc` already has the seed of a native module system, but not
yet a complete native stdlib import path.

## Design principles

### 1. Keep the boundary explicit

Every imported module should be classified as one of:

- `compile_time_only`
- `native_stdlib`
- `native_user_module`
- `cpython_fallback`

Do not blur these categories at runtime.

### 2. Native stdlib is allowlist-driven

Native stdlib support should expand module-by-module behind explicit
routing, tests, and "no libpython" verification. Do not switch all
stdlib imports over at once.

### 3. Source compatibility is secondary to semantic ownership

A module counts as "native" only when its import, initialization,
namespace behavior, and runtime operations are owned by `pcc` end to
end. A thin wrapper that still depends on CPython object semantics does
not close the architectural gap.

### 4. Bootstrap subset first, generality second

The first success condition is not "users can import everything". It is
"the self-host / bootstrap subset no longer needs CPython stdlib for its
normal control path."

## Module tiers

The plan should track native stdlib in four tiers:

| Tier | Meaning | Examples |
|---|---|---|
| 0 | compile-time-only imports; no runtime module object emitted | `__future__`, `typing` |
| 1 | pure/native Python module, compiled and linked by `pcc` | `string`, `base64`, parts of `json`, `pathlib` |
| 2 | native module with extern-backed runtime bindings | `math`, `time`, `re`, `hashlib` |
| 3 | intentionally remains CPython fallback | modules depending on CPython-only behavior or extension ABI |

Tier 1 and Tier 2 are the actual scope of this plan.

## Required substrate

Native stdlib support is blocked on five technical layers. Each must be
made explicit rather than inferred from the current sibling-module path.

### A. Import resolution

`pcc` needs a deterministic resolver that can answer, for every import:

- is this compile-time-only?
- is it a native user module passed in this compile?
- is it a native stdlib module shipped by `pcc`?
- otherwise should it fall through to CPython?

This requires:

The user-facing import spelling must remain CPython-compatible. A native
stdlib port is selected by the resolver, not by asking users to rewrite
programs:

```python
import os          # correct: pcc may lower this to native os helpers
import struct      # correct: pcc may use pcc/ir/support/struct.py
import gc          # correct: pcc may lower this to pcc_gc_* helpers
```

Do **not** introduce `import std.os` or `import pcc.ir.support.os` as the
normal surface for standard-library behavior. Those spellings can exist
only for pcc-private implementation modules or debugging hooks. If a
program runs on CPython with `import os`, the pcc-native route should
preserve that spelling and decide at compile time whether the provider is:

- a compile-time-only marker,
- a builtin native module dispatch,
- a `pcc/ir/support/<name>.py` port,
- a user module in the compile closure,
- or an explicit CPython fallback when fallback mode is enabled.

- dotted-name resolution
- relative import normalization
- package vs module resolution
- stable module-name-to-source lookup

### B. Native module object model

`pcc` currently has native function/class cross-module binding, but
general stdlib support needs real module semantics:

- one namespace object per loaded module
- global attribute lookup
- optional attribute writes where Python semantics require them
- `from m import x` binding against that namespace
- parent package -> child submodule attachment
- import cache semantics similar to a minimal `sys.modules`

### C. Module initialization order

The runtime needs a native story for:

- execute module top-level code once
- guard against duplicate initialization
- define when package `__init__` runs relative to submodules
- preserve predictable side-effect order

### D. Runtime features exercised by stdlib code

A stdlib module may be syntactically simple yet still require runtime
coverage beyond the current self-host subset:

- iterators and iterator protocol
- context managers
- decorators used as runtime values
- richer exception behavior
- dynamic attribute and call fallback where unavoidable
- builtin constructors (`int`, `list`, `tuple`, `set`, etc.)

### E. Packaging / distribution

If a module is native stdlib, the compiled program must know where it
comes from. There are only two acceptable models:

- ship the `pcc` stdlib source tree on disk and compile/load it through
  the native module path
- embed the selected stdlib sources/artifacts in the output binary

"Search the host CPython install" is not acceptable for a native module.

## Milestones

### M1. Native import router

**Goal:** make import routing explicit and testable.

Deliverables:

- add a native-stdlib resolver ahead of `py_cpy_import`
- define the first `native_stdlib_allowlist`
- keep compile-time-only handling separate from runtime imports
- teach single-file and multi-file Python paths to consult the same
  resolver

Gate:

- new focused import-routing tests show that allowlisted stdlib modules
  no longer lower through `py_cpy_import`
- non-allowlisted modules still do

### M2. Native module registry + cache

**Goal:** make native stdlib modules behave like actual modules rather
than just symbol bags.

Deliverables:

- minimal runtime module object / namespace representation
- one-time init guard per module
- module cache keyed by dotted name
- `from m import x` and `import a.b` semantics against native modules
- package-to-submodule binding for dotted imports

Gate:

- `import urllib.parse` binds `urllib`
- repeated imports do not rerun top-level side effects
- `from pkg.mod import fn` and `pkg.mod.fn` agree for native packages

### M3. Route `pcc/stdlib/` as modules

**Goal:** stop treating `pcc/stdlib/` as documentation-only stubs and
compile them as real import targets.

Deliverables:

- map stdlib names to files under `pcc/stdlib/`
- compile selected stdlib modules as extra native modules in the same
  build graph
- let native stdlib imports participate in the same pre-pass/export
  collection used by native user modules
- keep unsupported stdlib modules on fallback

Gate:

- a small allowlist such as `dataclasses`, `functools`, `itertools`,
  `collections`, `string`, `json`, `math`, `re` compiles and links
  without `libpython`

### M4. Fill runtime gaps required by Tier 1 / Tier 2 modules

**Goal:** close the semantic holes that keep stdlib replacements from
being usable.

Priority runtime work:

- builtin constructor helpers (`int`, `list`, `tuple`, `set`)
- richer module-attribute access and assignment
- iterator / generator-adjacent helpers needed by `itertools`,
  `contextlib`, and `collections`
- `with` / exception-path behavior needed by `contextlib` and I/O-facing
  helpers
- bytes / string edge cases that block `json`, `base64`, `urllib.parse`

Gate:

- each new runtime helper lands with one focused regression and one
  stdlib integration confirmation

### M5. Tiered stdlib rollout

Roll out native stdlib in explicit families rather than by file count.

### M5a. Bootstrap/core family

Modules:

- `typing` (already compile-time-only)
- `dataclasses`
- `abc`
- `functools`
- `itertools`
- `collections`
- `contextlib`

Success condition:

- bootstrap/self-host path no longer needs CPython for these modules

### M5b. Pure/native utility family

Modules:

- `string`
- `base64`
- `urllib.parse`
- `pathlib`
- `json`
- `copy`
- `warnings`

Success condition:

- representative user scripts importing these modules remain
  libpython-free

### M5c. Extern-backed systems/math family

Modules:

- `math`
- `time`
- `re`
- `hashlib`
- selected `os` / `sys` / `io` surface actually exercised by pcc

Success condition:

- extern-backed modules behave natively with no CPython import path

### M5d. Explicit fallback family

Modules expected to stay on CPython fallback until proven otherwise:

- `subprocess`
- `multiprocessing`
- `concurrent`
- `ctypes`
- modules requiring CPython-specific extension behavior

Success condition:

- fallback remains correct and explicit; no accidental "half-native"
  state

### M6. Packaging and embedding

**Goal:** make native stdlib delivery reproducible.

Deliverables:

- define where native stdlib source/artifacts live in installed wheels
- ensure produced binaries can locate the shipped native stdlib
- optionally add a later embedding mode so the selected stdlib ships
  inside the executable rather than beside it

Gate:

- fresh environment run does not consult host CPython stdlib for
  allowlisted native modules

## Test policy

Every native-stdlib step needs three kinds of tests:

1. import-routing regression
2. module-level integration behavior
3. no-libpython verification

Recommended gates:

```bash
env -u LC_ALL uv run pytest tests/test_py_multi_file_compile.py tests/test_py_multi_file_bootstrap_shim.py -q -n0
env -u LC_ALL uv run pytest tests/test_py_multi_file_compile.py -q -n0
env -u LC_ALL uv run python tests/py_corpus/run_pcc.py --phase phase4
```

As new coverage lands, add dedicated native-stdlib suites such as:

- `tests/test_py_native_import_router.py`
- `tests/test_py_native_stdlib_modules.py`
- `tests/test_py_native_stdlib_no_libpython.py`

For each module promoted to `native_stdlib`, add:

- one small direct test
- one realistic use-site
- one artifact-level `otool -L` / `ldd` confirmation where applicable

## Exit criteria

This plan is considered successful when all of the following are true:

- stage-2/stage-3 self-host runs do not require CPython stdlib for their
  supported path
- the bootstrap-safe stdlib subset resolves through `pcc/stdlib/`
  natively
- a documented allowlist of user-facing stdlib modules imports
  libpython-free
- unsupported modules still fall back to CPython explicitly and
  predictably
- `docs/python-limitations.md` is updated to describe the native/fallback
  boundary accurately

## Sequencing note

Do not wait for a grand "full native stdlib" moment. The right cadence
is:

1. add resolver support
2. promote one module family
3. add import + integration + no-libpython tests
4. move to the next family

That keeps the architecture honest and avoids another large hidden
dependency on `libpython`.
