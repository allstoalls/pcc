# Public API and core tools — P1 work in progress

## Priority and current instruction

The maintainer reconfirmed native pcc1 self-hosting and the five-GC program as
P0. CPython embedding, public tooling and ecosystem packages are P1 and must
not displace that work. Stop expanding the embedding implementation for now.
Do not resume unrelated numeric investigations from the prior paused agents.

The requested architecture is that CPython `import pcc` provides real pcc
capabilities, and pcc-dependent packages can consume native implementations.
This is not import-only compatibility, a test oracle, or a stub API. pcc1 is
the standalone entrypoint and cannot delegate native ownership to CPython.
Core kernels should stay in pcc while their compiler/runtime ABI is evolving.
Prebuilt platform wheels are the desired ordinary installation path; current
sdist installation can compile the toolchain locally and is not that result.

## Narrow implementation already written

- `pcc/artifact_inspect.py`: CPython-callable binary inspection using existing
  Mach-O and finite ELF readers, no subprocess/dlopen. Reports declared load
  commands and explicitly unknown build ownership; does not search embedded
  diagnostic strings for dependencies. Unsupported/malformed shapes fail.
- `pcc/bindgen.py`: native LR C parser produces deterministic scalar/typedef/
  raw-pointer extern prototypes for the documented LP64 targets. Directives,
  aggregate values, callbacks and ambiguous prototypes fail; no output overwrite.
  This generates compiler declarations, not callable CPython C bindings.
- Top-level lazy exports `pcc.inspect_artifact` and `pcc.generate_bindings`.
- Host CLI routes invoke the real APIs. Native CLI routes use the existing
  on-demand compiled-module runner, without adding host-C delegation.
- Tests in `tests/python/test_artifact_inspect.py` and `test_bindgen.py` include
  real CPython API/host CLI execution and `python -S` dependency-denial checks.
  Native dispatcher mocks prove routing only.

## Verification and native boundary

Final focused packet, after parser-error normalization, fixture correction
and formatting: 31 passed, 2 deselected in 1.64 seconds:

```bash
gtimeout 60s env -u LC_ALL uv run pytest -q -x -n0 --tb=short \
  tests/python/test_artifact_inspect.py tests/python/test_bindgen.py \
  tests/python/test_bootstrap_gate_baseline.py
```

The two deselections are the baseline file's gated cases; this is not a fresh
bootstrap chain. No release/stable-toolchain replacement or commit was performed.

Read-only/emit probes are under `build/cli-tools-20260908/`. Per-module
`--python-library --emit-llvm` and native emit-only probes return success but
contain strict stubs. They do NOT prove a broken executable closure: current
`pipeline.py` deliberately omits recursive stdlib closure for plain emit-only
requests. Do not repeat the mistaken inference that those stubs establish
the actual -o failure.

The actual `-o` probe used the installed historical v84 pcc1 plus its matching
immutable runtime archive, with `PCC_HOST_PYTHON` and `PCC_HOST_PCC` set to
`/usr/bin/false`, a 30-second process-group watchdog and 4 GiB RSS cap.
It failed before producing the tool executable:

```
pcc frontend worker failed: Exception: codegen[pcc.backend.elf_x86_64]:
NotImplementedError: Layer 1 slice assignment on type ByteArrayType not supported
```

This is a historical-pcc1 capability observation, not current-source bootstrap
qualification. Importing the shared ELF reader also includes writer functions
in the compiled module. Do not duplicate the parser or rewrite Python semantics
to bypass this; decide between generic bytearray slice support and a shared
read-only format module as part of subsequent P1 work. Current-source pcc1,
generated-binding native execution and full bootstrap remain unverified.

## Tracking

- #195 inspect: P1, implementation partial, native qualification open.
- #196 bindgen: P1, implementation partial, native qualification open.
- #197 CPython capability library: P1 design/implementation queue; not P0.

No pcc-sqlite/pcc-asyncpg repository or package was created. The initial
SQLite request was ambiguous and the user subsequently discussed deployment
design; the asynchronous clarification has not been answered. SQLite can be
embedded from its C amalgamation; asyncpg's direct PostgreSQL protocol route
does not require libpq. These are design directions, not package release proof.
