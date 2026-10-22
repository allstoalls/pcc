# Disassemble D0 Corpus

Status: D0 corpus selection. No `pcc disassemble` CLI, source map emitter, or
binary-only decompiler is implemented by this report.

The first useful tier is D: typed-AST to Python source to typed-AST round-trip.
This corpus is deliberately small and should grow only when the D gate reports
structural diffs clearly enough for failures to be actionable.

## Tier D Starter Corpus

Use all phase1 Python corpus source files:

- `tests/py_corpus/phase1/arith_floordiv_neg/source.py`
- `tests/py_corpus/phase1/arith_truediv/source.py`
- `tests/py_corpus/phase1/for_range_step/source.py`
- `tests/py_corpus/phase1/mutual_recursion/source.py`
- `tests/py_corpus/phase1/nested_function_calls/source.py`
- `tests/py_corpus/phase1/tuple_unpack/source.py`

Add these small self-host/compiler files once D1 can parse and emit their node
coverage without host-only shortcuts:

- `pcc/frontends/python/py_lift.py`
- `pcc/frontends/python/py_ast.py`
- `pcc/frontends/python/type_infer.py`

## Acceptance Policy

- Tier D passes only when canonical typed-AST equality holds between original
  source and recompiled emitted source.
- Textual Python identity is not required.
- Comments, formatting, and local variable names may differ after
  canonicalization.
- Any unsupported node must appear in the D1/D2 missing-node report, not be
  silently dropped.
- pcc1 evidence is required before any D result is used as a release claim.

## Current Result

No pass count exists yet. This report only closes the D0 planning item:
selection of the starter corpus and claim boundaries for future tests.
