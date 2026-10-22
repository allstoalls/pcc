# PCC Architecture Teaching Checklist

This document tracks the human's understanding of the whole pcc architecture.
It is a learning artifact, not completion evidence for an engineering slice.

## Session Scope

New teaching objective from 2026-06-05: understand pcc's overall architecture
at implementation-detail level: files, functions, dispatch branches, data flow,
ownership/fallback boundaries, and why each layer is written that way.

The previous `pcc1 --pytest` teaching note remains in
`docs/teaching-session-understanding.md`; this document is now the active
architecture-learning checklist.

## Mastery Checklist

### 1. North Star And System Shape

- [ ] Point to the ordinary CLI entry in `pcc/pcc.py` and explain how it calls
      `pcc/driver/cli_core.py`.
- [ ] Point to the bootstrap CLI entry in `pcc/driver/cli_bootstrap.py` and explain
      why it is separate from the ordinary CLI.
- [ ] Explain pcc's north star in implementation terms: which code paths are
      allowed to use host CPython, which paths are strict no-libpython, and
      which paths are bootstrap-owned.
- [ ] Explain why every claim must name the exact mode, such as host pcc,
      pcc1, no-libpython, libpython auto/on, LLVM backend, or self backend.

### 2. Top-Level Request Flow

- [ ] Trace `pcc hello.c` from `pcc/pcc.py` through `execute_cli(...)` to the
      C evaluator/project collection path.
- [ ] Trace `pcc hello.py` from `pcc/pcc.py` through `execute_cli(...)` to
      `_execute_python_path(...)` and `compile_python(...)`.
- [ ] Explain how `parse_cli_args(...)` normalizes CLI flags into the
      positional tuple consumed by `execute_cli(...)`.
- [ ] Explain what `--backend llvm`, `--backend llvm_capi`, and
      `--backend self` change.
- [ ] Explain what `--python-libpython=off/auto/on` changes.
- [ ] Explain the difference between running, emitting IR, emitting object code,
      linking, and bootstrapping.

### 2A. Entry Implementation Details

- [ ] Explain why `pcc/pcc.py` has a Click entry and a plain fallback entry.
- [ ] Explain why `cli_core.py` has both manual argument parsing and
      `execute_cli(...)`.
- [ ] Explain why `cli_bootstrap.py` avoids relying on full host pytest or
      dynamic Python features in its pcc1-native paths.
- [ ] Explain where `--pytest` is handled in `cli_bootstrap.py`.
- [ ] Explain why `pcc/api.py` is C-oriented and goes through `CEvaluator`,
      while Python compilation goes through `pcc/frontends/python/pipeline.py`.

### 3. C Frontend

- [ ] Explain the C frontend pipeline: CLI/API -> project collection ->
      preprocess/parse -> C semantic lowering -> LLVM/object/executable.
- [ ] Explain why the C frontend is the mature path.
- [ ] Explain the role of `pcc/driver/project.py`, `pcc/frontends/c/parse`, `pcc/frontends/c/codegen`, and
      `pcc/frontends/c/evaluator/c_evaluator.py`.
- [ ] Explain common bug classes such as signedness metadata, translation-unit
      collection, struct/union layout, and fake-libc declarations.

### 4. Python Frontend

- [ ] Explain the Python frontend pipeline: parse/lift -> type inference ->
      closed-world context -> lowering mixins -> runtime calls/backend.
- [ ] Explain why it is experimental and strict by default.
- [ ] Explain why unsupported Python constructs should fail loudly in strict
      no-libpython mode.
- [ ] Explain the role of `pcc/frontends/python/pipeline.py`,
      `pcc/frontends/python/codegen/*_lowering.py`, and `pcc/frontends/c/parse/py_*`.

### 5. Runtime And GC

- [ ] Explain the runtime layering: C-level kernel, shrinking C semantic
      runtime, growing pcc-Python runtime, C-API shim.
- [ ] Explain what heap objects, object headers, refcounts, slots, barriers,
      finalizers, weakrefs, and roots are at a high level.
- [ ] Explain why five GC backends must share one slot/root tracing contract.
- [ ] Explain why C and pcc-Python runtime mirrors must stay synchronized.

### 6. Self Backend And Bootstrap

- [ ] Explain pcc0 -> pcc1 -> pcc2 -> pcc3.
- [ ] Explain why pcc2/pcc3 identity matters.
- [ ] Explain why no-libpython self-backend evidence is stronger than host
      CPython evidence.
- [ ] Explain how `pcc/driver/cli_bootstrap.py`, `scripts/bootstrap.py`, and
      `tests/python/test_pcc_bootstrap_full.py` relate.

### 7. Value Model And Ecosystem

- [ ] Explain ordinary Python identity objects versus opt-in value payloads.
- [ ] Explain value/object projection and why object-boundary projection bugs
      happen.
- [ ] Explain why package support must be generic and not special-cased for
      NumPy/PyTorch/etc.
- [ ] Explain how package, fallback, ABI, and C-extension boundaries differ.

### 8. Debugging And Evidence Discipline

- [ ] Explain why focused regressions come before broad gates.
- [ ] Explain why host tests are not enough for pcc1/self-host claims.
- [ ] Explain why `DONE_WEAK` is often the honest state.
- [ ] Explain which tests or baselines prove each kind of claim.

## Teaching Log

- 2026-06-05: User changed teaching objective from the pcc1 pytest slice to the
  whole pcc architecture. Start at Stage 1: the five major subsystems and why
  they exist.
- 2026-06-05: User clarified that the teaching must be implementation-detail
  precise, not high-level architecture only. Stage 1 is now CLI/bootstrap/API
  entry dispatch with concrete file/function references.
- 2026-06-05: User asked what happens if she does not want to answer. Teaching
  should avoid exam-like pacing and use lower-friction verification options:
  annotated walkthrough, multiple-choice checkpoints, or user-led interruption.
  Final mastery still requires some observable signal from the human.
- 2026-06-05: User chose "you explain." Continue with implementation-level
  walkthrough. Stage 2 covers `compile_python(...)`: closure collection,
  parse/lift, type inference, `L1CodeGen`, IR passes, fallback detection,
  runtime selection, and native linking.

## Stage 2 Implementation Notes: Python Frontend

For a normal `.py` input, the implementation path is:

```text
pcc/pcc.py
  -> pcc/driver/cli_core.py:execute_cli(...)
  -> _execute_python_path(...)
  -> pcc/frontends/python/pipeline.py:compile_python(...)
  -> pcc.frontends.python.py_lift.parse_and_lift(...)
  -> pcc.frontends.python.type_infer.infer_module(...)
  -> pcc.frontends.python.codegen.layer1.L1CodeGen.generate(...)
  -> Python IR pass pipeline
  -> runtime archive selection
  -> LLVM/clang link path or in-repo self backend link path
```

Key implementation detail: if automatic closure collection finds more than one
source module, `compile_python(...)` delegates to `compile_python_multi(...)`
so imports/exports/class metadata can be modeled as a closed world.

Follow-up question covered: "How does Python become AST?"

Implementation path:

```text
compile_python(...)
  -> pcc.frontends.python.py_lift.parse_and_lift(src, filename, module_name)
  -> pcc.frontends.python.py_parse.parse(src, filename)
  -> Parser.parse_module(...)
  -> parser-private nodes such as _Module, _FuncDef, _Assign, _Call, _Name
  -> lift_module(...)
  -> _Lifter.lift_stmt(...) / _Lifter.lift_expr(...)
  -> pcc.frontends.python.py_ast.Module / FuncDef / Assign / Call / Name / ...
```

Important detail: freshly lifted expressions start with `DynType`; the
`type_infer.infer_module(...)` pass fills more precise `ty` fields later.

## Stage 1 Diagnostic Prompt

After the first explanation, the human should be able to answer:

1. Which file is the ordinary `pcc` CLI entry, and which function does it
   eventually call?
2. Which file is the bootstrap/pcc1 CLI entry, and why is it separate?
3. Where does a `.py` input get routed into the Python frontend?
4. Why does `pcc/api.py` use `CEvaluator` instead of the Python frontend?
