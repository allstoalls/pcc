# Self-host oracle: a per-function differential test layer

**Status:** open. Filed 2026-04-29 in response to
`docs/investigations/python-self-host-no-libpython-runtime-holes.md`.

## The problem

Codex's no-libpython investigation surfaced seven independent
self-host runtime bugs in a single afternoon — all of them
invisible to the existing test suite. The investigation listed six
end-to-end gates that should be added (link gate, compile gate,
object-model gate, parser side-effect gate, module-initializer
gate, error-propagation gate). Those are necessary.

But the existing test pyramid is already missing a layer **inside**
those end-to-end gates: there is no test that compares **the
behaviour of a single pcc-internal method when called under CPython
vs called under pcc1**. Every Codex-discovered bug lives precisely
in that gap.

This document proposes adding that missing layer — a per-function
differential oracle modelled on the same pattern pcc already uses
for the pass framework, the self backend, and the runtime archive
oracle.

## Why differential testing is the right shape

pcc already validates correctness through differential testing in
three places:

| Subsystem | Reference | Subject | Differential gate |
|---|---|---|---|
| Pass framework | LLVM-backed IR | each pass's emitted IR | byte-equal IR |
| Self backend | LLVM-backed object | self-backend object | behavioural equivalence + 4054 cases |
| Runtime oracle | cc-built `libpy_runtime.a` | pcc-built C / pcc-built pcc-Python ports | byte-equal stdout/stderr/exit |
| Bootstrap fix-point | CPython-hosted pcc | pcc1-hosted pcc | pcc2 ≡ pcc3 byte-equal |

The pattern is the same in each row: **a known-working version is
declared the reference; the new implementation is asserted to
agree with it**. This pattern works because pcc is a compiler — by
construction, there is always at least one path (the LLVM-backed
or CPython-hosted one) that is known to behave correctly, and that
path can be used as the reference for any new path.

The four rows above lock semantics at progressively coarser
granularities. None of them tests the **inside** of a single
self-host run.

## The gap: per-function self-host differential

The Codex investigation found bugs of this exact shape:

```python
# pcc/ir/ir.py, called from _emit_user_function:
return_ty = fn.function_type.return_type
# Under CPython: return_ty is a populated ir.IntType / ir.PointerType / ...
# Under pcc1:    return_ty is NULL → _zero_of() raises
```

Same source, same call site. The behavioural divergence appears
only when the host swaps from CPython to pcc1. None of the four
existing differential gates can catch this:

- Pass framework gate: doesn't run, this is frontend code
- Self backend gate: replaces emission, not the IR object model
- Runtime oracle gate: diffs the runtime archive impls, not the
  frontend's host
- Bootstrap fix-point: would catch divergence between pcc1 and
  pcc2 only if pcc1 successfully completes a self-compile — but
  pcc1 currently exits 0 with no output, so the gate is satisfied
  vacuously

The missing axis is **host of the same code**. CPython hosts pcc
correctly today (otherwise no bootstrap would have ever worked).
pcc1 hosts pcc with bugs. We need a gate that runs the same
internal pcc code under both hosts and asserts equivalence.

## Design proposal

A new test directory `tests/self_host_oracle/` containing
stand-alone Python programs, each exercising one specific pcc
internal method or property. A harness compiles each program with
pcc1 and runs it, then runs the same program under CPython, then
asserts byte-equal stdout.

### Example program

```python
# tests/self_host_oracle/module_str_basics.py
"""Lock ir.Module.__str__() behaviour across CPython and pcc1.

A freshly created Module with one function should produce the
same IR text whether the host running this program is CPython
or a pcc1 binary.
"""
from pcc.ir import ir

m = ir.Module(name="probe")
fn_ty = ir.FunctionType(ir.VoidType(), [])
fn = ir.Function(m, fn_ty, name="empty")
bb = fn.append_basic_block(name="entry")
b = ir.IRBuilder(bb)
b.ret_void()

# Probe several invariants in one program.
print("module_name:", m.name)
print("function_count:", len(list(m.functions)))
print("entry_terminated:", bb.is_terminated)
print("ir_text_starts_with_module:", str(m).startswith("; ModuleID"))
print("ir_text_contains_define:", "define void @empty" in str(m))
```

### Harness shape

Mirror of `tests/test_runtime_oracle_diff.py`:

```python
# tests/test_self_host_oracle_diff.py
@pytest.mark.parametrize("program", _self_host_corpus_programs())
def test_pcc1_matches_cpython(pcc1_binary, program, tmp_path):
    cpython_out = subprocess.check_output(
        [sys.executable, str(program)],
        timeout=30,
    )
    target_exe = tmp_path / f"{program.stem}.out"
    subprocess.check_call(
        [pcc1_binary, "--python-libpython", "off",
         str(program), "-o", str(target_exe)],
        timeout=120,
    )
    pcc1_out = subprocess.check_output([str(target_exe)], timeout=30)
    assert cpython_out == pcc1_out, (
        f"divergence on {program.name}:\n"
        f"  cpython: {cpython_out!r}\n"
        f"  pcc1:    {pcc1_out!r}"
    )
```

`pcc1_binary` is a session fixture that builds pcc1 once with
`--python-libpython off --backend self`. If that build fails, the
whole module skips with a clear "pcc1 not buildable on this host"
message — the existing link gate (Codex's gate #1) is the
prerequisite.

### Initial corpus

Each program targets one Failure Class from the investigation:

| Program | Failure class targeted | Probes |
|---|---|---|
| `module_str_basics.py` | 6 (object-model invariants) | `ir.Module.__str__()`, `m.name`, `m.functions` |
| `function_type_return.py` | 6 | `fn.function_type.return_type` is non-NULL after construction |
| `block_terminated.py` | 6 | `bb.is_terminated` flips after `ret_void()` |
| `builder_append_block.py` | 5 (`append_basic_block` receiver identity) | builder vs function as receiver, owner relation preserved |
| `parser_chained_calls.py` | 3 (chained helper side effects) | `self._expect(...).text` evaluates inner call once |
| `parser_module_globals.py` | 2 (parser globals safety) | `frozenset({...})` keyword set behaves correctly |
| `module_init_order.py` | 7 (top-init scaffold dependency order) | initialization sees its dependencies populated |

Each program is short (~20 lines), self-contained, and prints a
deterministic line per probe. Adding a new probe is appending a
`print(...)` line.

### What this gate gives us

Any time a pcc-compiled internal method diverges from its CPython
behaviour, the diff names the program (so it names the failure
class) and the probe (so it points at the specific method).
Failure messages look like:

```
divergence on module_str_basics.py:
  cpython: b'module_name: probe\nfunction_count: 1\n...'
  pcc1:    b'module_name: probe\nfunction_count: 0\n...'
```

That is enough information to know "the problem is
`Module.functions`" without needing lldb.

## Phased plan

### Phase O0 — prerequisites (this is what unblocks the rest)

Codex's investigation already lists this as gate #1:

- Build pcc1 with `--python-libpython off --backend self` reliably
  on macOS arm64
- Verify `otool -L` shows no libpython
- Run `pcc1 --help` cleanly under a hard timeout

If this fails, every per-function probe fails too — the gate has
to come first.

### Phase O1 — minimum viable corpus (1–2 days)

Three programs covering the failures Codex already found:

- `tiny_compile.py` — `def main(): return 0` end-to-end through
  pcc1, asserts compiled-binary exits 0. **This is the test that
  fails today; making it pass closes Codex's active blocker.**
- `module_str_basics.py` — covers Failure Class 6
- `function_type_return.py` — covers Failure Class 6 again, more
  specific

Each test should be **xfail today**, **green after Codex's
in-flight fix lands**. Flipping xfail → green is the metric for
"Codex's debug session is done".

### Phase O2 — full corpus aligned to Failure Classes (1 week)

One program per Failure Class. Each program should fail
deterministically when the corresponding bug is reintroduced.
This is the regression-prevention layer.

When this phase is complete, `tests/self_host_oracle/` contains 7
programs, the harness runs all of them, and every program is
green. This is the gate that prevents the next debugging session
from rediscovering the same bug class.

### Phase O3 — coverage expansion (ongoing)

Add a new program every time a self-host bug is discovered. The
discipline: never fix a self-host bug without first writing a
probe that fails on the bug. The probe goes in
`tests/self_host_oracle/` and stays as a regression gate.

### Phase O4 — multi-host expansion (after Linux x86\_64 lands)

Once Linux pcc1 builds, the harness should run the same corpus
under that pcc1 too. Three-way divergence (CPython vs pcc1-darwin
vs pcc1-linux) catches platform-specific self-host bugs.

## Relationship to existing tests

```
existing layers (validated):
  ├── unit tests              (CPython runs pcc, checks emitted IR shape)
  ├── runtime oracle          (cc-build / pcc-build / pcc-py-build runtime
  │                             archives produce byte-equal output for the
  │                             same compiled program)
  ├── bootstrap fix-point     (pcc2 ≡ pcc3 byte-equal — covers the closed
  │                             loop, not the contents)
  └── pass / self-backend
        differential          (LLVM reference vs new path — IR equality)

missing layer (this proposal):
  └── self-host oracle        (CPython-hosted pcc vs pcc1-hosted pcc —
                                per-function behaviour equality)

Codex's six end-to-end gates from investigation doc:
  build / compile / object-model / parser / module-init / error-prop
  → these are the integration layer above per-function self-host oracle
```

The self-host oracle sits between unit tests and bootstrap fix-
point. It's narrower than bootstrap (single method, not whole
binary) and broader than unit tests (covers the host-swap axis the
unit tests don't).

## Why this layer wasn't already there

The investigation doc names the cause directly: most existing
tests exercise the shape **CPython runs pytest → calls pcc
frontend → checks emitted IR**. They don't exercise the shape
**CPython builds pcc1 → pcc1 runs without libpython → pcc1
hosts the frontend**. The host-swap axis was implicit in the
bootstrap fix-point but never instrumented at sub-program
granularity.

Now that there is a known-working CPython reference path *and* a
nearly-working pcc1 path, the differential test is straightforward
to wire up — we just hadn't done it.

## Open questions

1. **Harness build cost.** Building pcc1 takes time (full
   bootstrap stage). The session-scoped `pcc1_binary` fixture
   amortises one build per test session, but local dev iteration
   may want to cache pcc1 across sessions. Worth deciding before
   the corpus grows past 20 programs.

2. **Where probes assertions live.** Two designs:
   - Probe programs print state, harness diffs stdout. Simple,
     debuggable, matches runtime oracle.
   - Probe programs `assert ...` in-process and exit nonzero on
     failure. Stronger guarantee but harder to debug because the
     pcc1 binary's error-propagation path is itself one of the
     things being tested (Codex Failure Class 4).

   Recommendation: **start with stdout-diff** (Codex Failure Class
   4 says exit codes are unreliable today; stdout diff sidesteps
   that). Migrate to in-process asserts after the
   error-propagation gate lands.

3. **xfail vs skip during development.** When pcc1 can't even
   build, should every per-function probe `skip` or `xfail`?
   - `skip`: keeps the suite passing, hides the regression
   - `xfail`: surfaces the regression but doesn't block

   Recommendation: **xfail with `strict=True`** so a green run
   reverts to red the moment pcc1 starts producing wrong output —
   that's exactly the moment a per-function probe should re-engage.

4. **Should this share infrastructure with `runtime_oracle/`?**
   The harness shape is identical; the test programs live in
   different directories because the axis differs. Could either
   share fixtures or stay separate. The corpus doesn't overlap, so
   keeping separate directories with a shared utility module is
   probably cleanest.

5. **CI cost.** Running pcc1 under all probes is on the order of
   seconds per program. Full corpus of 20 programs runs in <1 min.
   This is in budget for default CI; no opt-in flag needed.

## Cross-references

- `docs/investigations/python-self-host-no-libpython-runtime-holes.md`
  — Codex's seven-failure-class investigation that motivated this
  layer
- `docs/issues/python-semantics-preservation.md` — explains the
  four-gate semantic-preservation model; this proposal adds a
  fifth gate at finer granularity
- `docs/issues/open-bootstrap-issues.md` — Issue 1 closure path;
  this layer is the gate that turns "Issue 1 closed" from a count
  metric into an executable assertion
- `docs/issues/gc-semantics-gap.md` — separate axis (memory
  semantics, not host-swap correctness); the per-function oracle
  shape can extend to lock GC semantics under host-swap once those
  phases land

## Bottom line

pcc already differential-tests every other major subsystem. The
self-host runtime is the one place that doesn't have its own
differential gate, and that's exactly where Codex found seven bugs
in one afternoon. This proposal extends the existing pattern (pass
/ self backend / runtime oracle / bootstrap fix-point) to cover
the host-swap axis at per-function granularity.

It's not new infrastructure — it's reusing a pattern pcc already
proves works. The cost is one harness file plus a corpus directory
that grows one entry per debugging session. The benefit is that
**every Codex-discovered bug from this week becomes a regression
gate**, and the next self-host bug points at a single method
instead of requiring lldb.
