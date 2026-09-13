# Investigation: HEAD ad60403d drops the entry module's trailing `main()` call — every self-backend program ending in `main()` silently does nothing (exit 0, no output)

## Status
resolved — root cause is commit `ad60403d` ("pcc: fix main() return value as
process exit code (self backend)"), which filters the user's module-level
`main()` call out of the emitted entry body but left the replacement
adapter-invocation branch as `elif False:  # TEMP disabled for bisect` (and
its early adapter lookup ran before the adapter global exists, so it could
never have fired anyway). Fixed by emitting a direct zero-arg call to the
user's `main` at the end of `@main` with proper err-check and exit-code
mapping. 13 of the 14 red tests in the multi-file gates were this bug; the
14th was a stale assertion updated for the c079c05a symbol move (see Notes).

## Problem Description
`env -u LC_ALL uv run pytest tests/python/test_py_multi_file_compile.py
tests/python/test_py_multi_file_bootstrap_shim.py -q -n0` failed 14 of 133
tests at worktree state 2026-08-07 (all failures shaped "binary runs, exit 0,
expected stdout missing"). These are long-established gates (files last
touched 2026-07-22 / 2026-08-01), so something recent regressed them.

## Repro
```bash
printf 'def main() -> None:\n    print("none-main")\n\nmain()\n' > /tmp/nonemain.py
env -u LC_ALL uv run python scripts/pcc_multi.py --entry nonemain \
  --out /tmp/nonemain_bin --backend self --python-libpython off /tmp/nonemain.py
/tmp/nonemain_bin           # BUG: prints nothing, rc=0. Expected: "none-main", rc=0.
```
IR-level fingerprint (via `--emit-llvm`): `@main` binds the `main` function
object and immediately runs module finis + `ret i32 0`; no
`call ... @user_<mod>_main` exists anywhere in `@main`.

## Test [CONFIRMED]
Observed under the pytest command above (14 failures, e.g.
`test_native_relative_import_from_concrete_module_still_binds_export`:
stdout `''` != `'7\n'`, rc=0) and under the minimized repro. Attribution
checks all CONFIRMED before touching the fix:
- reverting this session's `returns_none` edits: still fails (not this session);
- `PCC_RUNTIME_CC=cc`: still fails (not a port-vs-C runtime drift);
- fresh `~/.cache/pcc/test-artifacts/runtime-builds`: still fails (not a
  stale-archive artifact);
- `git show ad60403d`: the commit adds `_is_user_main_call_stmt` + body
  filtering + a dead `elif False:  # TEMP disabled for bisect` branch, and
  its message admits "end-to-end verification blocked by system load".

## Proposals
- No.1 Implement the commit's intent properly (direct tail call + exit-code
  mapping) instead of reverting it                    [CONFIRMED]

## No.1 direct tail call + exit-code mapping
### Code Change
`pcc/py_frontend/codegen/module_lifecycle_lowering.py`:
- `_is_user_main_call_stmt` now requires a zero-arg, kwarg-free `main()`.
- `_emit_program_main` pops only a TRAILING `main()` statement (mid-body
  calls emit normally), and only when `emit_cpy_main_exitcode` is off (the
  cpython-compat path was also silently dropping the call before this fix).
- Removed the broken early adapter lookup and the `elif False` block.
- New `_emit_trailing_main_exit_code`: calls `self.functions["main"]`
  directly after the body; `void` return -> exit 0; `iN` return -> trunc/zext
  to i32; `ptr` (boxed) return -> `py_None` check then `py_int_to_i64`
  unbox; `_emit_post_call_err_check` after each call so an exception from
  `main()` reaches the unhandled-exception path (exit 1). If a zero-arg
  direct call cannot be formed (arity mismatch, no local `main`), the
  original statement is emitted unchanged (exit 0) — fail-open to the old
  behavior, never a dropped call.
### CONFIRMED
- Minimized repros: `-> None` main prints and exits 0; `-> int` main prints
  and exits with its return value (3) — the original commit's intended
  feature now actually works.
- `tests/python/test_py_multi_file_compile.py` +
  `tests/python/test_py_multi_file_bootstrap_shim.py`: 133 passed
  (was 14 failed / 119 passed).

## Notes
- The 14th failure (`test_py_gc_backend_runtime_file_compiles_without_
  libpython_fallback`) was distinct: commit c079c05a moved the
  `pcc_gc_note_frame_enter` definition from `py_gc_backend.py` to
  `freestanding_gc_frame_registry.py` but the same commit's test still
  asserted the marker in the old file. Updated the test to compile the new
  home (keeping the original no-self-rooting assertion and adding the
  no-fallback check for the registry file). `git log -S` confirms c079c05a
  removed the symbol from py_gc_backend.py.
- Pattern note for future sessions: two consecutive HEAD commits (ad60403d,
  c079c05a) shipped with "verification blocked by system load" /
  gate-red-at-commit states. When an established gate is red, diff the most
  recent commits touching the failing subsystem BEFORE suspecting the
  working tree.
- Exit-code semantics are a self-backend pcc convention (mode-labeled):
  CPython itself exits 0 regardless of a top-level `main()`'s return value.
  `python_libpython=off --backend self` programs ending in a trailing
  zero-arg `main()` now map `None -> 0`, `int -> value`; a non-int non-None
  return raises through `py_int_to_i64` and exits via the unhandled path.

## 2026-09-13: a distinct silent entry failure from threading owner inference

The earlier trailing-call repair is present. A new reproduction using
`test_asyncio_stdlib_objects_compile_without_libpython` still exited 0 with
empty stdout on GC0 and GC4. Frozen source `source-candidate-b17` and the
candidate with explicit `c_obj` return ownership both reproduced it, so the
ownership change did not introduce this failure. Evidence is under
`/private/tmp/pcc-owned-perf-20260912-rbrioqup/asyncio-extern-owner`.

The binary contained `user_program_main__gen_resume`: ordinary `main` had
become a generator factory. Worker-level instrumentation recorded
`main.is_async=False`, `main` in the may-park set, and a receiver named `event`
with class name `Event` and no threading constructor hints. The effect analysis
ignored `ClassType.module`, treating imported `asyncio.Event.wait` as a native
`threading.Event.wait`. The trailing-call path invoked the resulting factory,
so the user body never ran. Parent-only instrumentation observed nothing
because actual code generation ran in worker processes; its empty trace is
not negative evidence.

Both native threading method selection and effect analysis now reject a known
non-threading class owner. The original asyncio scenario passes. A separate
external `Event` class executes its `wait` method and prints 42 under GC0..4,
and a real native threading event still parks and resumes a virtual thread
under all five GCs (`threading-owner-green.stdout`,
`threading-owner-positive.stdout`). This fixes the erroneous classification;
it does not establish general generator-return exit-code handling or full
asyncio semantics. A new full pcc1 build is still required for qualification.
