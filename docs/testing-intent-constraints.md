# Testing the Project Intent — industrial methods → pcc obligations

This note records how industrial language/compiler projects bind *intent* to
enforceable tests, and maps each technique onto pcc's seven design obligations
(AGENTS.md "Project Intent"). It is the design rationale behind
[`tests/python/test_intent_constraints.py`](../tests/python/test_intent_constraints.py),
which turns the north-star obligations into runnable constraints — independent
of whether each capability is fully implemented.

> Provenance note: the source-fetch pass of the research run hit the org's
> monthly web-fetch spend limit, so the citations below are stated from
> established compiler-engineering knowledge (cutoff 2026-01) rather than
> freshly re-fetched primary URLs. The *test design* is what matters here and
> is fully validated against this repo; refresh the citations when fetch budget
> is available.

## Why "intent as test constraint"

A capability that is only described in prose decays silently. Every mature
toolchain instead encodes its load-bearing invariants as tests that go *red* on
violation. The recurring techniques:

| Technique | Representative tooling | Invariant it locks |
|---|---|---|
| Self-host fixed point / reproducible build | GCC `make compare` (stage2 vs stage3 object identity); Go `toolchain2 == toolchain3` byte-identity; Rust stage0→1→2 bootstrap + reproducibility diff | "A correct compiler reproduces itself." A stageN/stageN+1 divergence is a *classified correctness signal*, not noise. |
| Differential / oracle testing | CPython as semantics oracle; csmith / EMI for C compilers; "compile twice and diff" | Compiled program ≡ reference interpreter / second compiler. |
| Golden / snapshot IR tests | LLVM `lit` + `FileCheck`; Rust `compiletest` UI/stderr snapshots | Emitted IR / diagnostics keep an asserted *shape* (e.g. no boxing on a hot path). |
| Conformance suites | Java Compatibility Kit (JCK)/TCK + jtreg; CPython `Lib/test`; Go spec tests | Implementation matches the language spec, package-agnostically. |
| Multi-collector GC torture | Go `GODEBUG=gccheckmark=1`; HotSpot GC test modes; write-barrier verification | GC correctness holds across *every* collector, not just the default. |
| Semantics-preserving optimization proof | Alive2 / translation validation; metamorphic testing | An optimization never changes observable behaviour; a slow path preserves semantics when a fast-path assumption fails. |
| ABI / FFI / extension compat | symbol/ABI regression suites | Extension/FFI surface stays binary-compatible. |
| "No silent fallback" negative test | mode/feature assertions | The system did *not* take a forbidden path. |

## Mapping to the seven obligations

Each obligation gets a test class in `test_intent_constraints.py`. Tests split
into two tiers:

* **Tier A — fast contract/lint locks** (marker `intent`, run by default):
  pure file reads — CLI mode flags, baseline-JSON shape, "no package
  special-casing" lint, the 5-GC enum, the single slot-barrier contract,
  fixed-point byte-identity record, the difference-classification taxonomy, the
  value-model public API. These cost milliseconds and stay green.
* **Tier B — heavy behavioural constraints** (marker `integration`, excluded by
  default): actual compile / differential / multi-backend runs.

| # | Obligation | Tier A locks | Tier B constraints | Industrial analog |
|---|---|---|---|---|
| 1 | Compatibility mode-labeled | mode flags exist; invalid mode is a loud error; baseline keeps llvm/self + per-stage `links_libpython` distinct | — | reproducibility manifest tags artifact by toolchain |
| 2 | Performance proven | unboxed-hotpath tests assert IR shape (`py_instance_new` absent) | slow path ≡ CPython (differential) | FileCheck + Alive2 + benchmarks |
| 3 | Ecosystem generic | no `== "numpy"`-style branch in frontend; C kernel names no ecosystem package | — | package-agnostic conformance suites |
| 4 | Self-backend first-class | a self-backend gate exists | `--backend self` produces a *running* binary; `=off` never silently links libpython | "no silent fallback" negative test |
| 5 | Fixed-point contract | pcc2==pcc3 byte-identity recorded for *both* backends; 8 difference categories documented | (heavy stage chain lives in `test_bootstrap_gate_baseline.py`) | GCC `make compare` / Go toolchain identity |
| 6 | Five-GC comparative | exactly 5 backends; one slot trace/update contract (`pcc_gc_load_ptr`/`store_ptr`/`visit_runtime_roots`); per-backend gate file 0..4 | finalizer+weakref+cycle program identical across PCC_GC_BACKEND=0..4 | Go gccheckmark / HotSpot GC modes |
| 7 | Value model | `valueclass`/`ValueBox`/`ValuePayload` exported; explicit `c_int64`/`c_uint64` raw types; identity regression home exists | `int` value-lane overflow promotes to bignum (never wraps) | Valhalla projection + CPython conformance |

## Running

```bash
# fast intent locks (also part of the default suite)
uv run pytest tests/python/test_intent_constraints.py -m intent -n0

# heavy behavioural constraints
uv run pytest tests/python/test_intent_constraints.py -m integration -n0
```

## Current status (2026-06-19, macOS arm64)

Green ≠ "the intent is implemented." Green means *that constraint's sampled
slice* holds. The suite is allowed to contain red (`xfail`) constraints when an
intent gap has a runnable reproducer, but the current live xfail marker set is
empty. 30 tests cannot constrain an intent this large; the suite is built as a
**parametrized differential conformance corpus** (the CPython/Rust/JCK approach)
so it scales with the intent rather than sampling one point per obligation.

**351 cases total; 0 live xfail markers.** (grew from an initial 28 → 121 → 197
→ 280 → 348 → 349 → 350 → 351 as the corpus was broadened and xfails were
promoted or replaced by executable boundary locks; it scales with the intent
rather than sampling one point per obligation. A self-completeness guard,
`test_every_obligation_has_constraints`, fails if any obligation's class or
corpus is removed.)

* **25 Tier-A/default locks** (`-m intent`, run by default) — static contracts:
  the three mode axes + invalid-mode rejection, mode-labeled fallback baseline,
  the 5 mode boundaries documented; IR-shape evidence harness + runtime
  benchmark harness; "no package special-casing" lint over frontend, CLI,
  codegen, **and** the C kernel; byte-identity recorded for both backends + the
  8 difference categories + the pcc0..3 stage chain named; exactly 5 GC backends
  with all five names + the single slot barrier contract + per-backend gate
  files + the production-contract suite; value-model opt-in API + explicit raw
  int types + identity regression home.
* **326 Tier-B constraints** (`-m integration`) are collected:
  * `TestPythonSemanticsDifferential` — ~118 idioms diffed against CPython:
    arithmetic, strings (methods/format/`%`/f-string align/slicing), lists /
    dicts / sets (operators, comprehensions, slicing-with-step, `**`-merge,
    `setdefault`, `frozenset`), tuples + nested/star unpacking, control flow
    (`for/while`-`else`, `global`/`nonlocal`, closures), builtins
    (`range`/`hex`/`chr`/`pow`/`sum`/`bool`/`type`/`isinstance`/…),
    functions/`*args`/`**kwargs`/defaults, classes (inheritance, `super`,
    `@property`, `@classmethod`/`@staticmethod`, `__eq__`/`__hash__`/`__len__`/
    `__getitem__`/`__iter__`/`__call__`/`__contains__`/`__lt__`, multiple
    inheritance), generators incl. `.send`, context managers, and the full
    **error model** (`AttributeError`/`ValueError`/custom/`raise from`/`assert`/
    bare re-raise/`finally`-on-raise/`StopIteration`/float-div-zero).
  * `TestObligation7Behavioural` — ordinary-class identity corpus (8) + big-int
    arbitrary-precision corpus (7) diffed against CPython; value-class
    value-semantics corpus (equality/field/sum/nesting, golden); the
    `id()`-on-valueclass identity-escape diagnostic.
  * `TestObligation6GCEquality` — 12 GC-semantics programs (finalizer, weakref,
    cycle, nested cycle, dict/set/tuple-held refs, self-reference, deep nesting,
    del-in-loop, exception-held ref, **resurrection**), each required identical
    across PCC_GC_BACKEND=0..4.
  * `TestMetamorphicDifferential` — 60 deterministically-generated (seeded)
    random nested arithmetic/comparison/conditional programs, each diffed
    against CPython (csmith/EMI-style; reproduce a failure with
    `_gen_program(<seed>)`). Coverage scales by combinatorial depth.
  * `TestCrossBackendDeterminism` — 23 programs compiled under **both**
    `--backend llvm` and `--backend self`; `llvm == self == CPython` required
    (Obl. 4: self is a faithful execution root, LLVM is oracle not owner).
  * `TestObligation4SelfBackendBehavioural` — self-backend is a running root;
    `=off` never silently links libpython.
  * `TestObligation3PackageRoundTrip` — a generic pure-Python package can be
    installed, imported, and executed under strict self/no-libpython, and a
    CPython-extension ABI artifact is rejected explicitly at `PCC-PKG-004`.
  * `TestPythonSemanticsDifferential.test_stdlib_breadth_matches_cpython` — 14
    stdlib/extended-builtin idioms (math.pi/pow, str.partition/rsplit/translate,
    format-spec, bytes.decode, frozenset dict key, nested genexprs).
* **0 Tier-B gaps are currently marked `xfail`.** The former run=False NumPy
  frontier was split into executable generic-package and `PCC-PKG-004`
  boundary locks. This does **not** claim `import numpy` works in pcc-native
  no-libpython mode; that remains a C-extension ABI / NumPy C-API frontier.

Recently promoted gaps include higher-order callable values, key functions,
builtin callable `map`, `functools.reduce`/`partial`, decorator `*args`,
mixed-type `TypeError`, native math/bytes/complex coverage, bignum-preserving
`sum`, pcc1 smoke list/dict/set, valueclass field/genexpr access, generator-frame
owned-ref balance, C hardening checks, and `round(2.675, 2)` binary-float
rounding, plus native `threading.Thread().start()` when Thread objects flow
through list comprehensions, `append(th)`, and for-loop targets, and the
pcc2/pcc3 difference-classifier mechanism, plus the executable package
round-trip / CPython-extension ABI boundary split.

### Boundaries — what this suite does NOT prove

Honest scope so green is not over-read:

* **Not exhaustive.** Python is a large language; the differential corpus +
  metamorphic fuzzer cover the deterministic core broadly, not every construct.
  The fuzzer is constrained to a proven-green grammar (it stress-tests *depth*,
  not new constructs).
* **Heavy bootstrap evidence lives elsewhere.** pcc2==pcc3 byte-identity, the
  full stage1→2→3 chain, and the no-libpython fallback ratchet are owned by
  `test_bootstrap_gate_baseline.py`, `tests/python/gc/test_pcc_bootstrap_full_gc*`
  , and the fallback baselines. This file asserts the *contracts* (recorded
  byte-identity, mode labels), not the multi-minute rebuild.
* **Value-model differential uses golden, not a live CPython oracle** (importing
  `pcc` under bare CPython pulls llvmlite), so value-class expected outputs are
  hardcoded Python-semantic values.
* **GC equality samples observable output**, not heap invariants; long-running
  pause/RSS/fragmentation bounds are still future surface (existing
  `test_gc_performance.py` / `test_longrun_smoke.py` cover some of it).
* **No `run=False` gaps are currently live.** Future frontiers should prefer an
  executable characterization or an opt-in real-project gate over a permanent
  non-running xfail.

### Why this is considered "complete enough"

Every one of the seven obligations (plus the C-kernel layering rule) now has:
static contract locks **and** behavioural differential constraints; multi-backend
(llvm/self) determinism and the 5-GC matrix are exercised; and a
self-completeness guard prevents silent shrinkage. The remaining frontiers are
feature work outside the current live xfail set, heavier
harnesses (random-grammar fuzzing of new constructs, IR golden assertions, GC
long-run bounds), or evidence owned by the bootstrap gates — all already pointed
to above. Growth from here is adding executable frontiers and widening the
fuzzer grammar, both mechanical given the corpus structure.

### Mislabeling guard (a finding worth recording)

Several intents I initially *assumed* were unimplemented turned out to already
work and were therefore made green constraints, not `xfail`: first-class
functions (`f = add`), lambdas, `map()` over a *user* function, `filter()`,
resurrection equality across all five GC backends, and `int` value-lane overflow
promotion (the typed-int wrap hole was already closed — see
`tests/python/test_native_typed_int_overflow.py`). Every "verified red" entry in
`TestIntentGaps` was reproduced first. Lesson: probe before marking — in both
directions.

## Extending these constraints (intentionally left as future surface)

The corpus covers the *deterministic* slice broadly; the deeper surface is still
open and each item is a natural next batch of cases:

* **Obl. 2/7** — a *metamorphic* differential harness (csmith-style randomly
  generated typed-Python programs diffed against CPython), plus an Alive2-style
  check that the unboxed lowering is observably equal to the boxed lowering for
  the same source. The current corpus is hand-written, not generated.
* **Obl. 5** — broaden the difference classifier beyond its current guarded
  heuristic: when pcc2 ≠ pcc3, assert the diff falls into exactly one of the 8
  categories and route ambiguous diffs to a human-owned diagnostic category.
* **Obl. 6** — extend the 5-backend equality corpus to suspended coroutine
  frames, scheduler queues, C-extension refs, and pointer-bearing value
  payloads; add a long-running efficiency *bound* (pause / RSS / fragmentation
  over time, beyond the existing smoke/perf tests), the property AGENTS.md calls
  the real GC metric.
* **Obl. 3** — a real `pip install` + `import` + execute round-trip for a
  non-special-cased pcc-native extension package, then the larger NumPy
  C-extension ABI / NumPy C-API frontier.
* **no-libpython** — close the verified gaps (function value crossing a call
  boundary → `sorted(key=)`/`map(builtin)`/`functools.reduce`/decorator
  forwarding all derive from it) and the `sum()`-reduction bignum wrap; each has
  the remaining package frontier waiting to flip.
