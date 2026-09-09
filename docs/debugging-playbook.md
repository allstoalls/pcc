# Debugging Playbook

On-demand techniques: use the section relevant to the failure after locating
its current code path. Source, effective configuration and matching execution
are first-hand evidence; historical investigations locate hypotheses and prior
experiments. Check their revisions and later corrections before reusing them.
Section numbers (`§1`..`§12`) stay stable for investigation links.

### 1. Make the failure deterministic first

Don't start by reading all of `c_codegen.py` or `pcc/py_frontend/codegen/`.
Make it repeatable before guessing the cause: fix the random seed; replace
filesystem/time input with constants; run with `-n0`; isolate one test file, not
a whole suite. If the failure is random, removing the randomness IS the first job.

### 2. Compare `pcc` against a reference from the same source

This separates "the program is odd" from "the compiler/runtime lowered it wrong":

| Suspected layer | Reference |
|---|---|
| C codegen / runtime | system C compiler |
| Python frontend / runtime | CPython |
| `llvm_capi` IR builder | `llvmlite` (set `PCC_USE_LLVMLITE_C=1`) |
| Bootstrap stage divergence | the JSON baselines (`tests/bootstrap_gate_baseline.json`, `tests/fallback_baseline.json`) |

### 3. Use `llvmlite` as an oracle for `llvm_capi` parity

This section applies only to an explicitly labeled external reference test.
LLVM/llvmlite cannot execute a compiler product step or satisfy pcc1 ownership;
see the [dependency contract](compiler-contract.md#dependency-ownership-contract-maintainer-directive-2026-09-07).

If the failure looks like a codegen / IR-builder regression in
`pcc/llvm_capi/`, do not guess. Re-run the same minimized repro under
`llvmlite`:

```bash
PCC_USE_LLVMLITE_C=1 env -u LC_ALL uv run pytest \
  'tests/c/test_clang_compat.py::test_unsigned_int_to_float_conversion_uses_unsigned_semantics' -q -n0
```

Shrink to one repro/node → run under default `llvm_capi` → run with
`PCC_USE_LLVMLITE_C=1` → compare compile result, runtime result, and (if needed)
emitted IR → patch only the smallest semantic gap → add one regression test.

Especially effective for missing `IRBuilder` ops (`uitofp`/`fptoui`/`fpext`/
`fneg`), constant `gep`/`bitcast`, opaque-`ptr` typed-pointer semantics, and
function-pointer decay. It is **not** an oracle for the preprocessor, fake-libc,
parser acceptance, or compile-only diagnostics — if both backends fail the same
repro, the bug is above the backend layer.

### 4. Treat short fallback / IR traces as locators only

Short debug context is not root-cause evidence. Before naming a codegen /
no-libpython-fallback root cause from an IR trace, confirm all of: (1) the
enclosing `define ...` function; (2) the actual `py_cpy_*` call instruction;
(3) the helper argument source (e.g. `@.cpy.mod.<name>`); (4) the source
expression/import that produced it. Entry-block `alloca` names and the
`prev`/`prev2` lines from `PCC_DEBUG_BOOTSTRAP_TRACE` are weak clues — do not
patch shared lowering from them alone. If only short context is available, write
"candidate, unconfirmed" and first dump full IR or build a minimized reproducer.

### 5. Shrink the reproducer in stages

Failing integration test → smaller script/input → small harness calling the same
internal code path → pure C/Python expression. Time spent shrinking is repaid
many times over.

### 6. Test hypotheses by substitution, not only inspection

For a suspect large function: copy it into a temporary harness, replace one
helper at a time with the real implementation, reintroduce branches
incrementally. Often faster than staring at 500 lines of IR. Keep probes out of
the repo (see §9).

### 7. Avoid harness mistakes (these look like compiler bugs and are not)

- **Quoting:** pytest node ids with `[`/`]` must be quoted in `zsh` or they glob.
- **Process boundaries:** don't use `uv run python - <<'PY'` when the path uses
  `multiprocessing` spawn — macOS fails with `FileNotFoundError` for `<stdin>`;
  use a real file path or drive through pytest.
- **Stale data:** if a manifest says "native passes, pcc fails", re-run through
  the *current* harness before debugging `pcc`. Manifests drift.
- **Parser cache (mandatory):** after changing parser grammar or lexer token
  sets, **bump the default PLY cache version** in
  [`pcc/parse/c_parser.py`](../pcc/parse/c_parser.py). Otherwise the repo silently
  keeps the old `yacctab`/`lextab` and the parser looks "still broken" after the
  source fix. Then run a focused parser regression first, then one representative
  compile/runtime case — not a large project suite.
- **Long-running:** don't call a run "done" before its final summary (partial
  output is not a result); don't blindly wait — if no final summary in ~5 min,
  name a concrete reason or kill it and investigate the stall.

### 8. Use LLDB for native crash triage, not guesswork

When a compiled stage binary segfaults, LLDB should answer two questions:

1. Which generated/project function first received invalid data?
2. What runtime object did the bad pointer actually point to?

```bash
# Run + backtrace on crash. Always with a hard timeout.
gtimeout 120s env -u LC_ALL -u LC_CTYPE \
  lldb -b \
    -o 'run --ir-scaffold=on --python-libpython=off --backend self pcc/__main__.py -o /tmp/out' \
    -k 'bt all' \
    -- ./pcc1
```

To catch the bad call *before* the crash, add a conditional breakpoint
(`breakpoint set -n <fn> -c "<cond>"`) then `register read` / `memory read` the
suspect object at the stop. Rules:

- Wrap LLDB runs with a hard timeout (see Environment Rules); prefer batch mode
  (`-b`), `-o` for setup/run, `-k` for "after stop".
- Don't stop at the top runtime frame — a crash in `py_str_strip` usually means
  the *caller* passed a bad object; walk to the first `user_*`/project frame.
- Validate object layout with `register read`/`memory read`: the pcc-Python type
  tag is at `obj + 8` (e.g. `PY_TYPE_STR == 4`); confirm offsets against
  `pcc/py_runtime/include/py_runtime.h` and `pcc/py_runtime/src/py_internal.h`.
- Decode the *object*, not just the address (a `str` param that's actually a
  `Value` whose `_ref` points at `"%.6"` is a dispatch/type-flow bug above the
  runtime). LLDB localizes; the fix still needs a minimized regression test.

### 9. Do not stack unverified edits in shared codegen

`pcc/codegen/c_codegen.py` and
`pcc/py_frontend/codegen/{*_lowering,native_*}.py` are shared by almost every
meaningful path. `layer1.py` is now a thin facade; the broad blast radius lives
in the lowering mixins and native module lowering files. A "small local
cleanup" there can break Lua, SQLite, GCC torture, GC backends, and unrelated
parsers at once.

Rules:

- Do not land a broad speculative patch in shared codegen.
- If the change is not backed by a minimized reproducer, keep it in a
  scratch probe, not the repository.
- Every real-project fix must end with at least one minimized regression
  test in `tests/`. Large-project-only validation is not enough.
- After every shared-path edit, run focused regression checks **before** the
  next edit.
- If the first fix attempt does not clearly improve the minimized
  reproducer, stop expanding the patch and go back to reduction.

### 10. Separate data-layout bugs from expression-semantics bugs

- **ABI / layout:** `sizeof`, `offsetof`, fake-libc declarations, struct/union
  layout, object-header offsets. If suspicious, build a `sizeof`/`offsetof` probe
  and compare native vs `pcc`; once layout matches, move on.
- **Expression semantics:** signedness, promotions, comparisons, shifts,
  division/remainder, aggregate copy, control flow.

### 11. Prefer downstream-sensitive regression tests

A good regression test checks an expression in a context where the *next*
operation would be wrong if semantic metadata were lost — e.g. an unsigned
expression followed by `%` with a signed constant, by `>>`, or used in
`< > / %`. LLVM uses the same integer types for signed and unsigned, so the
compiler must preserve signedness intent itself.

### 12. Treat compile-time constant folding as a semantic subsystem

Integer semantics matter in two places: runtime lowering in LLVM IR, and
compile-time evaluation in `_eval_const_expr()`. Fixing only the runtime path is
not enough — runtime unsigned comparisons can be correct while compile-time
casts/ternary folding ignore width/signedness (e.g. `((size_t)(~(size_t)0))`
folds to `-1`), and a real project then compiles with the wrong constant and
fails far away. If a real program fails on a "simple constant", inspect
`_eval_const_expr()` and the macro-expanded source before blaming runtime IR.
