# pcc1 compatibility runner (`--python-libpython=auto`)

Task row: `PKG-P0-PCC1-COMPAT-RUNNER`.

This document defines the pcc1 *compatibility runner* contract and the manifest
it emits. It exists so the difference between the strict research default and
the ecosystem-compatibility mode is stated once, precisely, and cannot be
silently blurred into a "pcc supports native packages" claim.

## Contract

pcc1 module execution is invoked as one of:

```
pcc1 -m <module> [args...]                         # strict default (off)
pcc1 --python-libpython=off  -m <module> [args...] # strict default (explicit)
pcc1 --python-libpython=auto -m <module> [args...] # compatibility runner
pcc1 --python-libpython=on   -m <module> [args...] # compatibility runner
```

The leading `--python-libpython=<mode>` (or the space form
`--python-libpython <mode>`) is recognized only when it appears BEFORE `-m`
and the mode is one of `off`, `auto`, `on`. Anything else is not treated as a
module run and falls through to the normal compile / host-delegation paths.

- `--python-libpython=off` (and plain `-m ...`) is the **strict research
  default**: pcc-native, no-libpython. Behavior is exactly as before this task
  landed. No manifest line is emitted.
- `--python-libpython=auto` / `--python-libpython=on` select the generic
  **cpython-compat runner**. pcc1 emits one stable manifest line, then invokes
  `PCC_COMPAT_PYTHON -m <module> ...` (falling back to `PCC_HOST_PYTHON`, then
  `python3`). This is an explicit subprocess owner boundary; it is not native
  pcc execution and it does not link libpython into pcc1.

The compatibility runner is for ecosystem compatibility and pcc1-like-Python
execution. It is **NOT** a pcc-native or no-libpython package support claim.
`native_package_claim` is therefore always `false`, in every mode.

## Manifest

When mode is `auto` or `on`, pcc1 writes exactly one line to stderr:

```
PCC1_COMPAT_RUNNER_MANIFEST: {"requested_execution_mode": "cpython-compat", "execution_mode": "cpython-compat", "python_libpython_mode": "auto", "allows_libpython_fallback": true, "links_libpython": false, "native_package_claim": false}
```

For `off` (and plain `-m`) no manifest line is emitted.

The manifest has exactly six keys:

- `requested_execution_mode`: `"cpython-compat"` for `auto`/`on`,
  `"pcc-native"` for `off`.
- `execution_mode`: `"cpython-compat"` for `auto`/`on`; the requested module
  executed under the explicit CPython subprocess owner.
- `python_libpython_mode`: the parsed mode (`auto`, `on`, or `off`).
- `allows_libpython_fallback`: `true` only for `auto`/`on`, because those modes
  explicitly leave the pcc-native execution root for CPython compatibility.
- `links_libpython`: factual process/linkage measurement for this manifest;
  currently `false` because the manifest is emitted by the no-libpython pcc1
  binary before any package linkage/import report is measured.
- `native_package_claim`: always `false`.

## Honesty constraint (read this before citing the manifest)

The pcc1 binary itself is built **no-libpython**. `execution_mode` records the
CPython subprocess that actually owns `auto`/`on` module execution. Do not turn
that external ownership into `links_libpython: true`; the pcc1 artifact remains
unlinked, and linkage/import facts belong to package reports.

Consequently:

- `--python-libpython=off` is the strict research default and stays the default
  everywhere. It must never be weakened to make an ecosystem package "work".
- Emitting the compat manifest, or a package importing under the compat runner,
  is never evidence of pcc-native / no-libpython package support.
  `native_package_claim` is always `false`, and any claim of native package
  support must come from the separate no-libpython / pcc-native gates, not from
  this runner.
- Do not add package-name special cases to make the compat runner "support" a
  package. The runner is a generic mode label, not a per-package switch.

## Implementation pointers

All in `pcc/driver/cli_bootstrap.py` (compiled no-libpython into pcc1, so the manifest
is serialized with the file's hand-rolled `_json_*` helpers, never the `json`
module):

- `_module_request_libpython_mode(argv) -> (is_module_request, mode, module_argv)`
  parses the optional leading flag and returns the argv slice starting at `-m`.
- `compat_runner_manifest(mode) -> dict` / `compat_runner_manifest_json(mode)
  -> str` produce the manifest.
- `_run_python_module_from_pcc1_with_mode(module_argv, mode)` emits the manifest
  for `auto`/`on`, then invokes the generic CPython subprocess; `off` alone
  defers to `_run_python_module_from_pcc1`.
- `bootstrap_cli_main` routes recognized module requests through the mode-aware
  wrapper.

## Gates

Fast in-process unit tests (no build, no pcc1):

```bash
env -u LC_ALL uv run pytest -q -n0 tests/python/test_pcc1_compat_runner.py \
  -k "manifest or parse or unit"
```

Required binary gate (owner runs after building pcc1):

```bash
gtimeout 900s env -u LC_ALL uv run pcc --backend self --python-libpython=off \
  --ir-scaffold=on pcc/__main__.py -o build/bootstrap-compat-runner-pcc1/pcc1
gtimeout 300s env -u LC_ALL PCC_CURRENT_PCC1=build/bootstrap-compat-runner-pcc1/pcc1 \
  uv run pytest -q -n0 \
  tests/python/test_pcc1_compat_runner.py::test_pcc1_python_libpython_auto_module_runs_through_compatibility_path
```
