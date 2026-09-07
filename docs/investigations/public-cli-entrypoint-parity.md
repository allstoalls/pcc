# Investigation: public pcc entrypoints disagree on command execution

## Status
active — P0 entry convergence, separate from concurrent performance work

## Problem Description

The maintainer requires pcc, python -m pcc and native pcc1 to expose the same
command semantics: self by default, direct full C compilation, compiled Python
scripts/modules/tools, and consistent options/environment/errors. The installed
console script currently enters cli_core, while the module/native bootstrap
entry enters cli_bootstrap. The former interprets ordinary -m modules and the
latter delegates C requests to a host interpreter subprocess.

Read prior routing evidence in python-pcc-main-static-export-cli-bootstrap.md
before changing the bootstrap input: pcc.__main__ has a zero-fallback static
export contract. The current change keeps that input intact. cli_core.py and
cli_bootstrap.py already contain earlier inspect/bindgen work; preserve it.

## Repro

```bash
gtimeout 60s env -u LC_ALL uv run pytest -q -x -n0 --tb=short \
  tests/python/test_public_cli_parity.py
```

## Test [CONFIRMED]

The first test ran the actual environment's pcc script and CPython -m pcc with
--help. Both returned zero but produced different stdout. The packet stopped
at that first failure in 0.33 seconds. Additional cases cover environment,
errors, no runpy module interpretation, in-process C dispatch and source/output
classification.

## Proposals

- No.1 Share public dispatch and directly call the existing C frontend [pending]

## No.1 Share public dispatch and directly call the existing C frontend

### Code Change

Route cli_launcher through the bootstrap-safe public dispatcher while retaining
the bootstrap input. Replace the C host subprocess with a direct call into the
complete C CLI implementation, supplying the common default backend. Classify
the source independently of -o/flag values so output.c cannot turn a Python
compile into a C request. Keep full native-C ownership as #171's gate; a host
dispatch test is not native-C proof. Update the historical divergence contract
and README only to match verified behavior.

### Pending

Run focused parity tests, then actual source/module/C execution. Before any
bootstrap gate, record readiness/source identity and respect the shared
performance lock. No performance code or optimizer policy changes are part
of this proposal. Do not claim native C closure from dispatcher equivalence.
