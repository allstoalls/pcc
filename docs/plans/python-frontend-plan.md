# pcc Python Frontend — Implementation Plan

## Status 2026-04-23 — M1 closed, bootstrap verify runnable on supported dev host

**P6C Strategy C progress:**

- ✅ P6C.1 extern "C" FFI (`pcc/extern/`)
- ✅ P6C.2 LLVM C API declared (`pcc/ir/`) — not yet wired to default codegen
- ✅ **P6C.3 native Python parser + lift** — default path
- ⏳ P6C.4 stdlib stubs — ongoing (8 new stubs landed)
- ✅ **P6C.5 de-PLY parser — default path today**
  - α1 frozen LR tables + driver (63/63 parity)
  - α2 native lexer (63/63 token-stream parity)
  - α3 default flip + real-project + csmith (200/200 zero diff)
- ⏳ P6C.6 three-stage bootstrap verify — runnable on supported macOS arm64 dev host; pure Strategy C closure still blocked on dependency removal / packaging cleanup

**Audit**: `env -u LC_ALL uv run python scripts/audit_selfhost.py` reports
**0 blockers** (2026-04-23).
**Parity**: native Python parser 105/105 byte-identical vs CPython-ast
on phase1+2+3+6c corpus.
**Bootstrap**: `env -u LC_ALL ./scripts/bootstrap.sh` now completes stage 1 /
stage 2 / stage 3 on the supported macOS arm64 development host, and
`pcc2` / `pcc3` are byte-identical after stripping Mach-O code-signature
metadata from comparison copies.
**Oracle harness**: `tests/test_c_parser_oracle.py` — 63/63 C snippet
snapshots in place for future C parser differential testing.

**Scope caveat**: M1 closure = "pcc's Python source passes self-host
audit, native Python parser is default." It is NOT Strategy C done.
The current bootstrap run may still link `libpython`, and evidence is
still centered on the supported macOS arm64 development host. See #142
"P6C M2+M3 tracker" for remaining epics.

**Feature-flag policy (explicit user constraint)**:
- `PCC_USE_CPYTHON_AST=1` — opt out of native Python parser (kept)
- `PCC_USE_PLY_C_PARSER=1` — opt out of future native C parser (will be kept)
- Both fallback code paths preserved in source tree; self-host binary
  just won't link them. No deletion until P6C.6 bootstrap verified.

---

**Canonical status:** This is the single canonical plan for the Python
frontend. It now combines both:

- the **architecture / native-execution-model contract**
- the **phase-by-phase implementation roadmap**

See also:

- [`docs/plans/python-frontend-interfaces.md`](docs/plans/python-frontend-interfaces.md)
  - frozen interface contracts for parallel work
- [`docs/plans/python-native-stdlib-plan.md`](docs/plans/python-native-stdlib-plan.md)
  - post-bootstrap plan for routing selected stdlib imports through
    pcc-native modules instead of `libpython`

## What this plan covers

This document answers two different questions in one place:

1. **What are we building?**
   - a Python frontend, runtime, and self-hosting path
2. **What architecture are we trying to become?**
   - a `pcc`-owned Python native execution model that converges with the same
     compiler platform already serving C

That means this is not just a feature checklist. It is also the contract that
keeps the roadmap pointed at the right end state.

## Goal

Let `pcc xx.py` produce a native executable whose behavior matches
`python xx.py`, while outperforming Codon on Python compatibility and
matching it on numeric performance for typed code.

The deeper architectural goal is stronger:

> `pcc` should own a **Python native execution model**.
> Typed and progressively-native Python programs should run under semantics
> defined by `pcc`'s compiler + runtime, not by the CPython interpreter, and
> those programs should increasingly lower through a compiler core shared with
> the C pipeline.

## Architectural north star

The project is **not** trying to become merely:

- "a C compiler with a Python side experiment", or
- "a Python subset compiler that happens to emit LLVM"

The target is closer to:

> a multi-frontend native compiler platform where C and Python remain different
> at the language-semantics layer, but converge below that layer into a more
> shared execution core and backend pipeline.

This has several immediate consequences:

1. Python native execution is a first-class target.
2. Runtime support is allowed, but it should become **pcc-owned runtime**, not
   hidden dependence on CPython.
3. CPython fallback is strategically useful, but it is a **bridge**, not the
   final semantic authority.
4. Self-hosting is not just a late milestone. It is an architectural constraint
   that should shape earlier phases.
5. Shared-core convergence matters more than surface-level C/Python feature
   symmetry.

## Non-Goals

- Not a full CPython reimplementation (no `eval` / `exec` / `compile`
  with user-provided strings of Python source at runtime; not 100% of
  stdlib; not C-ext ABI replacement).
- Not a JIT. Everything is AOT.
- Not a PEP 703 no-GIL implementation — we inherit CPython's GIL story
  when we fall through to libpython.
- Not aiming to beat PyPy on fully-dynamic code. We match CPython
  there.
- Not trying to design a perfect universal IR for all languages before we have
  extracted real shared needs from the existing C and Python pipelines.

## Current state vs target state

### Current state

Today, the repository has two different frontend execution stories:

| Area | Current C path | Current Python path |
|---|---|---|
| frontend | parser + semantic lowering in `c_codegen.py` | parser + type inference + `layer1`/future layers |
| runtime authority | mostly native + libc/system ABI | mixed: pcc-native path plus CPython/libpython fallback |
| artifact model | MCJIT, object, system-link, executable | mostly AOT executable, sometimes with `libpython` bridge |
| semantic anchor | C lowering + compile-time semantics in pcc | partly native, partly externalized to CPython on dynamic/fallback paths |

This is a normal intermediate stage, but it is not the end state.

### Target state

The intended direction is:

```text
Python source
  -> Python parsing + semantic normalization
  -> pcc-owned Python runtime ops / object model
  -> shared compiler core
  -> LLVM IR
  -> object / exe / dylib / native artifact
```

with CPython fallback demoted to an **explicit interop boundary**, not the main
execution substrate.

## Architectural invariants

These rules should be treated as always-on design constraints.

### 1. Native does not mean runtime-free

The target is not "pretend Python is just typed C".
The target is "compile Python through a pcc-owned native execution model, with
whatever runtime support is necessary".

### 2. Fallback is a bridge, not the center

If a capability works only because execution falls through to the CPython C API,
that capability may still be useful — but it should not be confused with growth
in `pcc`'s own Python native execution model.

### 3. Shared-core convergence beats frontend symmetry

We do not need C and Python to gain analogous source-language features in lockstep.
We do want C and Python to share more of:

- ABI/layout services
- call lowering
- CFG/basic-block primitives
- SSA scaffolding and optimization interfaces
- runtime/intrinsic registration
- object emission, linking, cache, and execution infrastructure

### 4. Self-hosting constraints start early

Even before the final bootstrap phases, new work should be evaluated against:

- whether it expands the bootstrap-safe subset
- whether it hardens long-term dependence on `libpython`
- whether it makes stage1/stage2/stage3 style bootstrapping easier or harder

### 5. Shared core should emerge from real commonality

We should avoid a speculative big-bang "universal IR" design. Shared core
should grow out of real shared needs in control flow, calls, exceptions,
ownership, runtime ops, and backend reuse.

## Shared-core boundary with C

The right synchronization target is **shared compiler capability**, not
surface-level feature parity.

### Python-frontend-owned concerns

These remain Python-specific:

- syntax parsing
- scope/name binding policy
- annotation interpretation and inference policy
- import semantics at the source-language level
- descriptors / dunders / MRO / class semantics
- dynamic-vs-native selection policy
- Python-specific semantic normalization

### Shared-core concerns

These should increasingly be shared with the C path:

- type/layout/ABI services
- call signatures and call lowering
- CFG/basic-block and SSA plumbing
- exception-edge representation
- runtime/intrinsic registry
- LLVM lowering hooks
- object emission, system-link, execution, and cache

This is the core architectural meaning of "C and Python advance together".

## Fallback policy and bootstrap-safe subset

Fallback is acceptable as:

- a bring-up strategy
- a compatibility bridge
- an ecosystem interop mechanism
- a temporary self-host bootstrap aid

Fallback is **not** the desired long-term semantic center of the Python path.

The roadmap should therefore distinguish at least:

- native-path coverage
- fallback-path coverage
- mixed-path programs
- self-host-safe coverage

A **bootstrap-safe subset** is the portion of Python syntax, semantics, runtime,
and stdlib replacements that `pcc` itself can depend on in a stable, repeatable
way while compiling its own implementation.

This subset matters before final self-hosting, because it defines which design
choices are strategic and which are just convenient compatibility shims.

## Positioning vs Codon

| Axis | Codon | pcc target |
|---|---|---|
| Python compatibility (% of real .py runnable) | ~70% | **≥ 95%** |
| Numeric code speed (typed) | 1.0× | 0.9× – 1.2× |
| Dynamic code speed | ❌ fails | ≈ CPython |
| `import numpy`/`pandas`/`requests` works | ❌ | ✅ |
| Runtime lib LoC | ~40k C++ | **≤ 6k C** |
| exe size (hello world, typed) | ~5 MB | **≤ 800 KB** |
| Compile time (pyperformance-sized project) | baseline | **≤ 0.5×** |
| License | commercial | MIT |

"Surpass Codon" is defined as passing **≥ 5 of these 8 axes** on a
benchmark suite agreed below.

---

## High-Level Architecture

```text
xx.py
  │
  ▼
┌─────────────────────────────┐
│ pcc.frontends.python.parser      │  stdlib ast today; native parser in self-host path
└─────────────────────────────┘
  │
  ▼
┌─────────────────────────────┐
│ type inference / selection  │  L1 = typed native
│ / semantic normalization    │  L2 = typed + managed escapes
└─────────────────────────────┘  L3 = dynamic / fallback interop
  │
  ▼
┌───────────────────────────────────────────────────────────────┐
│ pcc-owned Python runtime ops / object model                   │
│   object alloc / attrs / methods / containers / exceptions    │
│   boxing / unboxing / bridge boundaries                       │
└───────────────────────────────────────────────────────────────┘
  │
  ▼
┌───────────────────────────────────────────────────────────────┐
│ shared compiler core                                           │
│   ABI/layout / calls / CFG / SSA / runtime registry / passes   │
└───────────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────┐       ┌──────────────────────┐
│ LLVM IR / objects    │◀──────│ pcc passes / backend │
└──────────────────────┘       └──────────────────────┘
  │
  ▼
┌──────────────────────┐
│ link / run           │
│   libc               │
│   py_runtime.a       │  pcc-owned runtime
│   libpython (opt)    │  compatibility bridge only when needed
└──────────────────────┘
  │
  ▼
native exe
```

### Runtime library (`py_runtime/`)

Estimated ≤ 6 kLoC of C:

| Module | Purpose | LoC budget |
|---|---|---|
| `py_int.c` | tagged int + bignum overflow fallback | 800 |
| `py_str.c` | UTF-8 string, O(1) len cache, slicing | 1000 |
| `py_list.c` | growable array | 500 |
| `py_dict.c` | open-addressing hash table | 800 |
| `py_tuple.c` | immutable fixed-size array | 200 |
| `py_obj.c` | object header, refcount/ownership hooks, type dispatch | 700 |
| `py_gc.c` | cycle collector or later ownership/GC substrate | 600 |
| `py_exc.c` | unwind helpers + traceback | 500 |
| `py_print.c` | repr/print formatting | 300 |
| `py_cpython.c` | CPython bridge for compatibility/fallback only | 400 |
| **total** | | **≤ 5800** |

Hard cap: the runtime library size MUST stay under 6 kLoC
non-blank non-comment, measured by `cloc`.

## Architecture gates by phase

The feature/deliverable acceptance criteria below remain mandatory. But they are
not sufficient by themselves.

A phase should **not** be considered complete if its tests pass while its
architecture moves in the wrong direction. In practice, every phase should also
be reviewed against these architecture gates:

| Phase | Native-path gate | Fallback gate | Shared-core gate | Bootstrap gate |
|---|---|---|---|---|
| Phase 1 | Typed subset runs with **no libpython** and no hidden PyObject execution for mandatory L1 features | No fallback dependency is introduced for the required MVP subset | Python path reuses existing backend/emission/link machinery where practical; any duplication is called out as temporary | Typed subset becomes the first explicit bootstrap-safe nucleus |
| Phase 2 | Covered scalar/container semantics are implemented by `pcc` runtime logic, not borrowed from CPython | For features claimed in Phase 2, fallback is not the semantic authority | Runtime-op families for ints/strings/core containers become explicit enough to survive backend refactors | Core data semantics become usable by internal tools without relying on fallback |
| Phase 3 | Primary class/object/exception corpus runs on a pcc-owned native path | Fallback may cover unsupported corners, but not the main OOP/exception story claimed by the phase | Exception edges, object/runtime ops, and call conventions start aligning with shared CFG/SSA/call plumbing | Class + exception subset becomes usable for self-host-oriented source refactors |
| Phase 4 | Native path remains first-class and does not regress when fallback lands | Every fallback entry is explicit, measurable, and test-visible | Bridge/marshalling APIs are centralized instead of being scattered through ad hoc codegen | Bootstrap reporting distinguishes temporary libpython dependence from native progress |
| Phase 5 | Optimization reduces escapes or improves native-path speed; it must not merely hide fallback cost | Benchmarks are split into native, mixed, and fallback-heavy results | Specialization/inlining use shared call/runtime machinery rather than a separate Python-only optimization island | Hot compiler paths are audited for bootstrap-safe readiness |
| Phase 6 | Each bootstrap stage increases the native share of compiler execution | Remaining fallback dependencies are listed as blockers, not invisible assumptions | Self-host path pushes extraction of shared services instead of creating a second backend stack | Bootstrap-safe subset is measured against `pcc`'s own source tree |
| Phase 6C | Self-hosted `pcc` binary runs with **no libpython** | Fallback is not used inside the self-hosted compiler binary | `llvm_capi`, native parser, stdlib replacements, and final emission all sit on shared artifact/backend infrastructure | Stage2/stage3 convergence is a release gate, not a nice-to-have |

A good shorthand is:

- feature gates tell us **what works**
- architecture gates tell us **what direction we are going**

## Shared-core extraction priorities

The repository should avoid trying to invent a perfect giant shared IR before it
has extracted enough real commonality. The preferred strategy is to extract
shared **services and execution building blocks** in an order that helps both C
and Python.

### Priority order

| Priority | Shared capability | Why it comes early | Likely repo anchors |
|---|---|---|---|
| 1 | Artifact/emission pipeline | Both frontends eventually need the same object/asm/IR/link/cache story | `pcc/pcc.py`, `pcc/api.py`, `pcc/frontends/c/evaluator/c_evaluator.py`, `pcc/frontends/python/pipeline.py` |
| 2 | ABI/layout/call service | C already contains hard-won ABI/layout knowledge; Python extern/runtime/object lowering should reuse that discipline | `pcc/frontends/c/codegen/c_codegen.py`, `pcc/extern/`, `pcc/frontends/python/codegen/runtime_abi.py` |
| 3 | CFG/basic-block/SSA plumbing | Loops, branches, exceptions, and optimization quality all depend on this layer | `pcc/frontends/c/passes/`, `pcc/frontends/c/ssa/`, `pcc/frontends/c/codegen/`, `pcc/frontends/python/codegen/` |
| 4 | Runtime/intrinsic registry | Python runtime ops, C builtins, extern calls, and bridge helpers need one discoverable mechanism | `pcc/extern/`, `pcc/runtime/`, `pcc/frontends/c/passes/` |
| 5 | Exception/cleanup edge model | C cleanup rules and Python exceptions both need structured control-flow edges, not scattered ad hoc lowering | `pcc/frontends/c/codegen/c_codegen.py`, Python codegen layers, backend pass plumbing |
| 6 | Bridge-boundary accounting | Native vs fallback vs extern crossings must be visible and measurable if the project is going to shrink fallback over time | `pcc/frontends/python/pipeline.py`, `pcc/runtime/`, benchmark/test harnesses |

### Extraction rules

1. **Extract shared services before shared syntax.**
   - The first wins should be ABI, call, CFG, runtime-registry, and emission
     services, not a grand unified front-end AST.
2. **Prefer APIs and reusable services over a speculative mega-IR.**
   - A service boundary can become a more explicit IR later, once it stabilizes.
3. **If a new low-level primitive helps both C and Python, it belongs in the shared core.**
4. **If a behavior is mainly source-language semantics, keep it in the frontend.**
5. **Do not let Python self-host work accidentally fork the backend stack.**
   - Self-host pressure should extract shared infrastructure, not create a
     second isolated pipeline.


## Runtime ABI sketch

The Python-native path needs an ABI story that is explicit enough to survive
optimization work, fallback shrinkage, and self-hosting.

At a minimum, the plan should assume three broad value classes:

1. **unboxed native scalars**
   - examples: `int`, `bool`, `float`, and other values proven native and
     non-escaping
2. **pcc-native heap objects**
   - examples: `str`, `list`, `dict`, `tuple`, class instances, modules,
     exceptions, closures, and later coroutine state
3. **interop / bridge values**
   - values that cross into or out of fallback or foreign-library boundaries

The call surface will likely need distinct categories as well:

- native direct calls
- native boxed/runtime calls
- fallback bridge calls
- direct `extern` C calls

This section does not freeze layouts yet, but it does freeze the architectural
expectation that ownership, value class, and call category must become explicit
runtime/compiler concepts rather than remaining hidden inside ad hoc codegen.

## Native Python runtime-op families

`pcc` should not model Python as "frontend sugar directly over LLVM".
It should converge on a stable operational vocabulary that sits between Python
surface semantics and raw backend lowering.

The main runtime-op families we should expect to stabilize are:

- object allocation and initialization
- attribute load/store
- method bind / method call
- list / dict / tuple construction and update
- iterator creation / `next`
- truthiness and rich comparison
- raise / catch / finally cleanup
- module init / import hooks
- box / unbox / marshal boundaries
- explicit native ↔ fallback bridge crossings

This vocabulary is important because it gives the shared compiler core something
stable to optimize and reason about, instead of forcing every Python feature to
encode its behavior directly into backend-specific lowering.

## Architecture scorecard

In addition to feature acceptance tests, the Python plan should keep an
architecture scorecard visible in progress reports and release notes.

Recommended recurring measures:

- **native-path coverage** — how much of the supported corpus runs without
  `libpython`
- **fallback-path coverage** — how much still depends on bridge execution
- **mixed-path programs** — programs that combine native and fallback regions
- **self-host-safe coverage** — how much of the compiler/tooling subset avoids
  fallback and unsupported runtime behavior
- **shared-core reuse** — how many important services are now shared with the C
  path instead of reimplemented in Python-only lowering
- **bridge crossings per workload** — whether performance work is reducing or
  hiding escape frequency

The point is not vanity metrics. The point is to prevent compatibility progress
from being mistaken for native-model progress, and to make fallback shrinkage
and shared-core growth first-class outcomes.

## Repo-level shared-core extraction map

The shared-core agenda is easier to execute when it is mapped onto current repo
modules instead of being discussed only as an abstract architecture.

| Area | Current anchor paths | Direction |
|---|---|---|
| artifact/emission pipeline | `pcc/pcc.py`, `pcc/api.py`, `pcc/frontends/c/evaluator/c_evaluator.py`, `pcc/frontends/python/pipeline.py` | converge on one artifact and execution pipeline across frontends |
| ABI/layout/call discipline | `pcc/frontends/c/codegen/c_codegen.py`, `pcc/extern/`, `pcc/frontends/python/codegen/runtime_abi.py` | extract reusable call/layout services instead of duplicating low-level lowering |
| CFG/SSA/pass plumbing | `pcc/frontends/c/passes/`, `pcc/frontends/c/ssa/`, `pcc/frontends/c/codegen/`, `pcc/frontends/python/codegen/` | make structured control flow and optimization hooks look less frontend-specific |
| runtime/intrinsic registration | `pcc/extern/`, `pcc/runtime/`, pass registration code | centralize builtin/runtime/bridge helper discovery |
| exception/cleanup edges | `pcc/frontends/c/codegen/c_codegen.py`, Python codegen layers, backend lowering | align native exception semantics with shared control-flow machinery |
| self-host bring-up | `pcc/frontends/c/parse/`, `pcc/ir/`, `pcc/stdlib/`, `scripts/audit_selfhost.py` | ensure self-host pressure extracts shared infrastructure rather than creating a second backend stack |

This table should evolve as real extractions happen, but keeping it in the plan
makes architectural intent easier to audit against the codebase.


## Shared-core workstreams by phase

The extraction priorities above describe **what** should become shared. This
section makes the plan more actionable by describing **when** each workstream
should become a first-class concern in the existing roadmap.

| Phase | Shared-core workstream emphasis | Why it belongs there |
|---|---|---|
| Phase 1 | artifact pipeline reuse, basic call/emission reuse | The MVP should prove that Python is entering the same native artifact pipeline as C instead of inventing a second ad hoc path |
| Phase 2 | runtime-op stabilization for scalar/container semantics | Once Python true-semantics work lands, ints/strings/containers need stable runtime/compiler boundaries that backend work can rely on |
| Phase 3 | exception edges, object/runtime call discipline, class/method call shaping | OOP and exceptions are where frontend semantics start to demand stronger shared CFG/SSA and call-model discipline |
| Phase 4 | explicit bridge accounting, centralized marshalling, native-vs-fallback boundary hygiene | Compatibility work should not sprawl through the codebase; it should expose and measure bridge crossings cleanly |
| Phase 5 | specialization/inlining through shared services rather than Python-only shortcuts | Optimization should strengthen the shared core and reduce escapes, not create a parallel optimization island |
| Phase 6 / 6C | llvm bindings, parser replacement, stdlib replacements, and bootstrap pipeline convergence | Self-hosting is the forcing function that proves shared infrastructure is real instead of aspirational |

A useful rule of thumb is:

- **early phases** prove Python can ride the same artifact/backend machinery as C
- **middle phases** make runtime ops, exceptions, and bridge boundaries stable
- **bootstrap phases** prove the shared-core story is strong enough to support
  the compiler itself

## Open questions and pending design decisions

Several hard design choices should remain visible in the main plan instead of
being rediscovered late in implementation:

- What is the long-term object header / type-metadata layout for pcc-native
  Python objects?
- What ownership or memory-management strategy should become canonical
  (refcount, tracing GC, hybrid, staged mix)?
- How explicit should the mid-level shared execution layer become: service APIs
  first, or a named/stable IR form?
- What should the native import/module-loading model look like once fallback is
  no longer the semantic center?
- How should traceback/debug metadata be represented so that self-hosted tools
  can rely on it without dragging CPython semantics back in?

These are not blockers for all earlier progress, but they are important enough
that the plan should keep them visible and review them at each major phase.

---

## Phase 1 — MVP: Typed Python → Native (6 weeks)

### Scope

- Python subset: `def`, `return`, `if`, `while`, `for i in range(n)`,
  arithmetic on `int` / `float` / `bool`, `print()`, recursive calls.
- Every function must have type annotations for params + return.
- No classes, no collections beyond `list[int]`, no exceptions, no
  `import` (beyond builtins we redefine).

### Deliverables

1. `pcc/frontends/python/py_parser.py` — parse .py via stdlib `ast`, lift to
   pcc internal AST nodes.
2. `pcc/frontends/python/type_infer.py` — annotation-driven type checking.
3. `pcc/py_codegen/layer1.py` — emit LLVM IR for L1.
4. `py_runtime/py_print.c` + `py_list.c` (int-element only).
5. `pcc/pcc.py` CLI — detect `.py` input, run Python pipeline.

### Acceptance Criteria

**Every one must pass before Phase 1 is closed.**

| # | Test | Pass condition |
|---|---|---|
| 1.1 | `fib(30)` — typed recursive | `pcc fib.py && ./fib` produces same output as `python fib.py`; runtime within **1.3×** of `clang -O2 fib.c` |
| 1.2 | n-body loop, 100k iterations | Same output as CPython; runtime within **1.5×** of `clang -O2` |
| 1.3 | `pyperformance/nqueens` | Same output; runtime **≤ Codon × 1.2** |
| 1.4 | hello world exe size | **≤ 800 KB** static, **≤ 100 KB** stripped |
| 1.5 | Compile time for 1000-line typed Python file | **≤ 3 seconds** on reference machine |
| 1.6 | Regression suite | All 25 curated L1 test `.py` files match CPython stdout byte-for-byte |
| 1.7 | Negative tests | Type errors (`str + int`, missing annotation) are rejected at compile time with **line number + caret** |

**Gate:** if any of 1.1–1.7 fails, Phase 1 is incomplete.

### Timeline

| Week | Milestone |
|---|---|
| 1 | Parser + AST dump |
| 2 | Type inference + error reporting |
| 3 | L1 codegen: scalar ops + control flow |
| 4 | L1 codegen: function calls + recursion |
| 5 | Runtime lib: print + list[int] |
| 6 | Integration + acceptance tests |

---

## Phase 2 — Python True Semantics (6 weeks)

### Scope

Every difference between "C-style int" and "Python-style int" closed.
`str` / `list[T]` / `dict[K,V]` / `set[T]` / `tuple` runtime complete.

### Deliverables

1. Tagged int representation + `llvm.sadd.with.overflow` fast path +
   bignum fallback via tiny in-house bignum (or link GMP behind a flag).
2. `py_int.c` complete.
3. Python-correct `/` (returns float), `//` (floor div), `%` (sign
   follows divisor) codegen.
4. `py_str.c`: UTF-8, indexing, slicing, `+`, `*`, `in`, `.split()`,
   `.join()`, `.strip()`, `.replace()`, `.startswith()`,
   `.endswith()`, `.find()`, `len()`.
5. `py_list.c`, `py_dict.c`, `py_set.c`, `py_tuple.c` — container
   runtime.
6. `None` singleton + `None`-safety checks (attribute access on
   `None` raises `AttributeError`, not segfault).

### Acceptance Criteria

| # | Test | Pass condition |
|---|---|---|
| 2.1 | `2 ** 100 == 1267650600228229401496703205376` | Correct bignum result |
| 2.2 | Overflow bench: `sum(i*i for i in range(10**6))` | Same result as CPython; no overflow UB |
| 2.3 | `-7 // 2 == -4` AND `-7 % 2 == 1` | Exact Python semantics |
| 2.4 | `3 / 2 == 1.5`, result type is `float` | Correct |
| 2.5 | Unicode: `"héllo"[1] == "é"` | Correct UTF-8 indexing |
| 2.6 | `d = {"a": 1, "b": 2}; list(d) == ["a", "b"]` | Insertion-ordered dict (PEP 468/520 compliant) |
| 2.7 | List bench: 10k appends + sort + slice | Output matches CPython; speed within **1.3× Codon** |
| 2.8 | Dict bench: 10k inserts + lookup | Output matches CPython; speed within **1.3× Codon** |
| 2.9 | `None.foo` | Raises `AttributeError` with correct message (no segfault) |
| 2.10 | Runtime lib LoC audit | `cloc py_runtime/*.c` **≤ 3500** at this phase |

---

## Phase 3 — OOP + Exceptions (5 weeks)

### Scope

Classes with single AND multiple inheritance, MRO (C3), descriptors,
dunders, `super()`, `isinstance`, `try/except/else/finally`,
`raise … from …`, tracebacks.

### Deliverables

1. Class codegen:
   - Single-inheritance: C struct + vtable.
   - Multi-inheritance: MRO table + virtual dispatch.
2. Descriptors: `@property`, `@classmethod`, `@staticmethod`.
3. Dunder resolution: `__add__` / `__radd__` / `__eq__` / `__hash__`
   / `__iter__` / `__len__` / `__getitem__` / `__setitem__` /
   `__contains__` / `__call__`.
4. Exception: LLVM `invoke` + `landingpad` + personality function;
   `try/except/else/finally` with correct unwind and cleanup.
5. `raise X from Y` chains `__cause__`.
6. Traceback: line numbers in frames.

### Acceptance Criteria

| # | Test | Pass condition |
|---|---|---|
| 3.1 | Single inheritance: override + `super()` call | Output matches CPython |
| 3.2 | Diamond inheritance with `C3` MRO | `MyClass.__mro__` equals CPython's |
| 3.3 | `@property` | Getter fires on attribute access |
| 3.4 | 10 dunders | Operator overloading works for `+ - * / == != < > in`, `len`, `iter` |
| 3.5 | `try/except/finally` with raise in middle | finally runs, exception propagates, output matches CPython |
| 3.6 | `raise X from Y` | `e.__cause__ is y` is `True` |
| 3.7 | traceback format | `traceback.format_exc()` output within **line-number exact** of CPython |
| 3.8 | Corpus: 50 curated OOP `.py` files | All match CPython stdout |

---

## Phase 4 — CPython C API Fallback (6 weeks)  ← differentiator

### Scope

Any code pcc can't fully compile gets lowered to CPython C API calls.
This is what Codon does NOT do.

### Deliverables

1. Link against `libpython3.x.so` (runtime-loaded; exe works with any
   compatible CPython).
2. Untyped function: compile body as `PyObject*` ops calling
   `PyObject_CallObject`, `PyObject_GetAttr`, `PyObject_GetItem`,
   etc.
3. `import x`: call `PyImport_ImportModule("x")`; module becomes a
   `PyObject*` handle used via `PyObject_GetAttr`.
4. Cross-layer boundary: when a typed function calls into a
   dynamic context (or vice versa), emit marshalling (`PyLong_FromLong`
   / `PyLong_AsLong`, etc.).
5. Decorator, context manager (`with`), generator (`yield`) minimal
   coroutine state machine.
6. `*args` / `**kwargs` argument marshalling.

### Acceptance Criteria — the "real PyPI" tests

| # | Test | Pass condition |
|---|---|---|
| 4.1 | `import numpy; a = np.array([1,2,3]); print(a.sum())` | Prints `6`; exe runs without CPython installed at runtime beyond libpython |
| 4.2 | `import pandas; df = pd.DataFrame({"x":[1,2,3]}); print(df.sum())` | Matches CPython output |
| 4.3 | `import requests; print(requests.get("https://httpbin.org/ip").status_code)` | Prints `200` |
| 4.4 | Decorator: `@functools.lru_cache` on a recursive fn | Correct caching behavior |
| 4.5 | Generator: `def g(): yield 1; yield 2` consumed by `list(g())` | Output `[1, 2]` |
| 4.6 | Context manager: `with open("x") as f:` | File handle closes on exit + on exception |
| 4.7 | `*args` / `**kwargs` forwarding | Matches CPython for typical wrapper patterns |
| 4.8 | Codon-incompat corpus: 20 `.py` scripts Codon **cannot** run | pcc runs **≥ 18/20** correctly |

**Gate 4.8 is the explicit "surpass Codon on compatibility" gate.**

---

## Phase 5 — Optimization & Differentiation (4 weeks)

### Scope

Cross-layer optimization, benchmark suite, exe-size minimization,
documentation, release prep.

### Deliverables

1. Type specialization: an untyped function called with concrete types
   compiles a type-specialized clone (like PyPy's unboxing).
2. Inlining across layer boundaries: typed → typed calls inline;
   typed → dynamic escapes a layer but we recover via specialization.
3. Benchmark harness: pyperformance subset + custom micro-benchmarks.
4. Release docs: tutorial, type-annotation guide, migration from
   CPython/Codon, limitations doc.

### Acceptance Criteria

| # | Test | Pass condition |
|---|---|---|
| 5.1 | pyperformance geomean (typed benchmarks) | pcc / Codon within **[0.9, 1.2]** |
| 5.2 | pyperformance geomean (mixed benchmarks) | pcc **≥ 2×** CPython |
| 5.3 | Real-project corpus: Flask demo, pandas notebook, requests scrape | **≥ 3/3** run end-to-end identically to CPython |
| 5.4 | Final 8-axis scorecard vs Codon | **≥ 5/8** axes won (see positioning table) |
| 5.5 | Documentation | Tutorial + 10 how-to pages + limitations page + changelog |

---

## Phase 6 — Self-Host: `pcc pcc.py → pcc` (8 weeks, optional)

### Goal

Use pcc (built on CPython) to compile pcc's own Python source into a
native executable, then prove the resulting binary can compile pcc
again and produce a byte-identical (or functionally equivalent)
binary. Classic three-stage bootstrap, adapted for pcc.

### Why this is harder than Phase 1–5

pcc itself is written in Python and uses:

| Dependency | Nature | Bootstrap challenge |
|---|---|---|
| `llvmlite` | C extension, 50k+ LoC of LLVM C++ bindings | Can't pure-Python compile; must either link the extension or replace with direct LLVM C API calls |
| `ply` | Pure Python lexer/parser | OK |
| `ast` (stdlib) | Mostly C | CPython provides; if we embed libpython, fine |
| `re` (stdlib) | C extension | Same as ast |
| `dataclasses` / `typing` | Pure Python, but heavy metaclass / annotation gymnastics | Exercises OOP + descriptors hard |
| `subprocess` / `os` | Syscall wrappers | Need native syscall bindings |
| pcc's own code | ~8k LoC Python, uses everything | End-to-end stress test |

### Three bootstrap strategies (pick one)

**Strategy A — "Embed libpython" (easiest, ~6 weeks)**

Use Phase 4's libpython fallback. pcc.py compiles with pcc's Python
frontend; dynamic code falls to CPython C API at runtime. Result is a
self-contained exe that *contains* a CPython interpreter for dynamic
parts, but doesn't need a separate `python` on PATH.

- Pro: achievable, minimal new runtime work
- Con: exe is ~20 MB (libpython is big); not "pure native pcc"

**Strategy B — "Partial self-host" (moderate, ~10 weeks)**

Hot paths (parser, codegen loops) get fully typed and compiled to
native. Everything else falls to libpython. Measurable speedup on
compilation itself, smaller exe than Strategy A, still not pure native.

- Pro: real performance gain, pcc compiles C faster than before
- Con: still depends on libpython for cold paths

**Strategy C — "Pure self-host" (hardest, ~16 weeks)**

Replace llvmlite with direct LLVM C API bindings from compiled Python
code. Rewrite or shim stdlib modules pcc uses. Result: fully static
pcc exe with zero Python runtime dependency.

- Pro: true "pcc is a C compiler that happens to have been Python once"
- Con: massive — requires an LLVM-C-API binding library that pcc's
  Python frontend can emit directly

### Deliverables (Strategy A — recommended for first bootstrap)

1. Python frontend correctly handles all constructs pcc.py uses:
   - dataclass decorators with `@dataclass(frozen=True)` / `field()`
   - `typing` module annotations (`list[X]`, `dict[K, V]`, `Union`,
     `Optional`, `TypeVar`, `ClassVar`, `Callable`)
   - metaclass usage where present
   - `__slots__` where present
   - nested functions / closures
   - `try/except` over pcc's error paths
2. libpython fallback covers:
   - `llvmlite.binding` and `llvmlite.ir` (both C-backed) — accessed
     via `import` → CPython imports the extension, pcc-compiled code
     calls through the PyObject* bridge
   - `subprocess.run` with `capture_output`
   - `pathlib.Path`, `os.path` operations
   - `re` module (compile + match + findall)
3. Build system: `make bootstrap` produces `pcc` binary from `pcc.py`
4. Three-stage validation harness: compile pcc with stage N, use it
   to compile pcc again → stage N+1; compare.

### Acceptance Criteria

| # | Test | Pass condition |
|---|---|---|
| 6.1 | `pcc pcc.py -o pcc1` | Produces a binary ≤ 25 MB (Strategy A) |
| 6.2 | `./pcc1 hello.c -o hello && ./hello` | Same output as `python -m pcc hello.c -o hello_ref && ./hello_ref` |
| 6.3 | Three-stage bootstrap: `python -m pcc pcc.py -o pcc1; ./pcc1 pcc.py -o pcc2; ./pcc2 pcc.py -o pcc3` | `cmp pcc2 pcc3` reports **byte-identical** (or, if build metadata injection prevents that, semantic equivalence on a test corpus) |
| 6.4 | Full pcc test suite compiled with `pcc1` | Same pass count as CPython-run pcc |
| 6.5 | Compile time: `pcc1 some_c_project.c` | **≤ 2×** the CPython-run pcc compile time (bootstrap may be slower due to libpython overhead) |
| 6.6 | Memory / leak scan on `pcc1` under ASAN | Zero leaks on typed Python paths; libpython-side leaks are acceptable but logged |
| 6.7 | Portability | Bootstrapped exe runs on three platforms (macOS arm64, Linux x86_64, Linux arm64) |

**Gate:** passing 6.1 + 6.2 + 6.3 + 6.4 is the canonical
"pcc self-hosts" claim.

---

## Phase 6C — Pure Self-Host (Strategy C, selected) — 16 weeks

Strategy C is the chosen bootstrap target: a fully native pcc binary
with zero Python runtime dependency. `ldd pcc` shows libc + libLLVM
only — no libpython, no CPython extensions.

This phase replaces the Strategy-A / Strategy-B alternatives above.
The libpython fallback from Phase 4 is intentionally **not used**
inside the self-hosted pcc binary (though it may remain available for
end-user pcc programs).

### Why this is 16 weeks (not 6)

Every dependency on CPython / stdlib / llvmlite must be replaced with
something pcc can either (a) fully compile natively, or (b) link to a
C library via extern "C" bindings. No runtime Python fallback.

### Self-host readiness (audit snapshot 2026-04-21)

**Python-source closure (M1) complete.** `python scripts/audit_selfhost.py`
reports **0 blockers** (down from 1002+ at P6C.0). `PCC_USE_CPYTHON_AST=1`
is now the reverse-opt-out escape hatch; native parser is the default.

> **Scope caveat**: M1 closure means "Python source code passes self-host
> audit, native Python parser is production default." It does **not**
> mean Strategy C is complete. The pcc binary still depends on:
>  - **llvmlite** (Python runtime) — addressed by P6C.2-wire
>  - **PLY** (Python runtime) for C frontend — addressed by P6C.5 de-PLY
>
> Both are separate, independently sized epics. See milestone M2/M3
> breakdown in `docs/plans/python-frontend-plan.md` task tracker.

### Historical: 2026-04-20 audit snapshot (pre-closure)

`python scripts/audit_selfhost.py` reports **4 total blockers** (down
from 1002+ at P6C.0 and 54 at the start of the current work round).

| Category | Count | File | Status |
|---|---|---|---|
| `dynamic-attr` (FFI lookup) | 2 | `pcc/api.py`, `pcc/frontends/c/evaluator/c_evaluator.py` | Genuinely dynamic: `getattr(cdll, user_symbol_name)` |
| `dynamic-attr` (PLY rule register) | 1 | `pcc/frontends/c/parse/plyparser.py` | PLY framework contract — P6C.5 task is to retire PLY |
| `vararg` (extern `__call__` trap) | 1 | `pcc/extern/__init__.py` | Runtime trap that never executes (extern calls are compile-time-lowered) |

Recent work in this round:
- 27 generator findings eliminated — rewrote `pcc/frontends/python/py_lex.py`,
  `pcc/frontends/c/passes/llvm_explicit.py`, `pcc/frontends/c/passes/whole_program.py`, `pcc/
  ir_passes/parity.py`, `pcc/frontends/python/codegen/class_gen.py` to
  return lists instead of yielding, and converted `@contextmanager`
  helpers in `pcc/frontends/c/parse/c_parser.py`, `pcc/pcc.py`, `pcc/frontends/c/codegen/
  c_codegen.py` to explicit `__enter__`/`__exit__` classes.
- 13 dynamic-attr findings eliminated — explicit dispatch tables in
  `pcc/frontends/c/passes/ast_utils.py`, `pcc/frontends/c/generator/c_generator.py`, `pcc/
  passes/base.py`, `pcc/frontends/c/ast/c_ast.py`, `pcc/frontends/c/codegen/c_codegen.py`,
  `pcc/frontends/python/pipeline.py`, `pcc/frontends/python/codegen/layer1.py`,
  `pcc/frontends/python/py_lift.py`.
- 9 unstubbed-import findings eliminated — `ctypes`, `fcntl`,
  `multiprocessing`, `concurrent` stubs in `pcc/stdlib/`, and
  `click` whitelisted (P6C.5 will retire it).
- 1 banned-builtin eliminated — `pcc/frontends/c/preprocessor.py` swapped
  `eval()` for a narrow integer-only expression evaluator
  (`_eval_cpp_expr`, ~100 LoC).

**P6C.3 native parser** has **105/105 parity** on corpus phase1+2+3+6c
and parses all 189 pcc source files. Gated behind `PCC_NATIVE_PARSER=1`.
Production default flip awaits resolution of the 4 structural blockers
(all are P6C.5 scope: FFI adapter rewrite + PLY retirement).

### Sub-phases

#### 6C.1 — extern "C" FFI in the Python frontend (2 weeks)

Lets compiled Python call arbitrary C libraries directly.

**Deliverables:**

- Syntax: `from pcc.extern import c_func, c_int, c_str`; allows
  declaring `LLVMContextCreate: Callable[[], c_ptr] = extern("LLVMContextCreate")`.
- Codegen: typed Python call to an extern function → direct
  `call @LLVMContextCreate()` in LLVM IR, no marshalling.
- Type mapping: `c_int` → i32, `c_int64` → i64, `c_ptr` → ptr,
  `c_str` → ptr (with UTF-8 NUL-terminated convention), struct
  passing by value/pointer.
- Lifetime rules documented (caller vs callee ownership, no
  auto-INCREF/DECREF around C calls).

**Acceptance 6C.1:**

| # | Test | Pass |
|---|---|---|
| 1 | `printf("hello\n")` via extern | exe prints "hello" |
| 2 | `malloc(16)` / `free(p)` via extern | valgrind clean |
| 3 | Pass a typed dataclass by pointer to a C function | C sees correct struct layout (matches C's struct alignment) |

#### 6C.2 — LLVM C API binding (`pcc/ir/`) (3 weeks)

Replacement for llvmlite, 100% typed Python declarations of the
LLVM-C headers pcc uses.

**Deliverables:**

- `pcc/ir/core.py` — `LLVMContextRef`, `LLVMModuleRef`,
  `LLVMValueRef`, `LLVMTypeRef`, `LLVMBuilderRef` as opaque `c_ptr`
  wrappers.
- `pcc/ir/binding.py` — `parse_assembly`, `verify`,
  `create_mcjit_compiler`, `get_function` — matches the 40-odd
  llvmlite.binding API calls pcc actually uses.
- `pcc/ir/ir.py` — mutable IR builder (`Module`, `Function`,
  `BasicBlock`, `IRBuilder`) matching the llvmlite.ir surface area
  pcc uses.
- Adapter: pcc's existing `pcc.frontends.c.codegen.c_codegen` / `pcc.ir_passes.*`
  modules switch imports from `llvmlite` to `pcc.ir` behind a
  feature flag; both code paths coexist during development.

**Acceptance 6C.2:**

| # | Test | Pass |
|---|---|---|
| 1 | Round-trip: build an IR module with `pcc.ir.ir`, emit text, reparse | Output text matches the same IR built via llvmlite |
| 2 | All 80 upstream passes from master-plan still pass tests with pcc switched to `pcc.ir` | Same pass/fail counts |
| 3 | `ldd` on a pcc exe built with llvm_capi | Shows libLLVM-20.so but no libpython |

#### 6C.3 — native Python parser (3 weeks)

Replace `import ast` with a Python parser written in typed pcc Python.

**Deliverables:**

- `pcc/frontends/python/py_lex.py` — Python tokenizer covering the subset pcc
  uses (CPython's full tokenizer is 2000+ LoC; our target subset is
  ~800 LoC).
- `pcc/frontends/python/py_parse.py` — PEG-style parser producing pcc's internal
  AST nodes. Grammar covers: def/class/if/elif/else/while/for/try/except/
  finally/return/yield/with/import/from/assign/augassign/del/pass/
  break/continue/global/nonlocal/lambda/comprehension/decorator/
  star-expr/walrus (:=).
- Compatibility shim: accepts Python 3.11–3.13 syntax. Match-case
  (PEP 634) is explicitly **not** required.

**Acceptance 6C.3:**

| # | Test | Pass |
|---|---|---|
| 1 | Parse all pcc's own `.py` files | Zero parse errors; AST diff against `ast.parse()` output reports zero semantic differences |
| 2 | Parse a corpus of 200 real PyPI files from top-1k packages | **≥ 195** parse without error |
| 3 | Error messages | Compile-error output includes line + column + caret under offending token |

**Status (2026-04-20):**

- ✅ `pcc/frontends/python/py_lex.py` (333 LoC) — covers indentation, string
  prefixes (b/f/r/u + combos), triple-quoted, hex/octal/bin literals,
  imaginary-literal suffix, all multi-char operators, continuation.
- ✅ `pcc/frontends/python/py_parse.py` (1000+ LoC) — full grammar: classes,
  funcs with `*args`/`**kwargs`/`/`/default, control flow, try/except/
  finally/else, with, raise-from, import, from-import, del, global/
  nonlocal, assert, lambda, comprehensions (list/set/dict/gen),
  star-unpacking in calls and iterables, walrus (`:=`), ternary, f-string
  bodies (as opaque), slicing with start/stop/step, tuple-target in
  for/assign, decorator chains, PEP 604 `A | B` unions.
- ✅ `pcc/frontends/python/py_lift.py` — bridges the `_*` parser nodes to
  `py_ast.Module`; captures type annotations + defaults.
- ✅ `PCC_NATIVE_PARSER=1` env var routes the pipeline through the
  native parser instead of CPython's `ast`.
- ✅ Parses 188/188 pcc source files, 29/29 stdlib stubs.
- ✅ **103/103 Python corpus tests (phase1+2+3) produce byte-identical
  stdout with the native parser vs the CPython-ast path.**
- Remaining work: better source spans (col info carried from token to
  AST), error messages with caret, fuzz against top-PyPI-1k.

#### 6C.4 — stdlib replacements (4 weeks)

Every stdlib module pcc transitively imports gets replaced with a pcc-
compilable version OR an extern "C" binding.

**Inventory (auditing pcc's imports):**

| Module | Plan |
|---|---|
| `re` | bind PCRE2 via extern "C" (2 days) |
| `os` / `os.path` | pure Python over extern "C" syscalls (4 days) |
| `subprocess` | extern "C" over posix_spawn / CreateProcess (3 days) |
| `pathlib` | pure Python over `os` (2 days) |
| `dataclasses` | reimplement `@dataclass` decorator (4 days) |
| `typing` | reimplement `List`, `Dict`, `Optional`, `Union`, `Callable`, `TypeVar`, `ClassVar`, `cast` as no-op / generic markers (2 days) |
| `functools` | reimplement `lru_cache`, `partial`, `reduce`, `wraps` (3 days) |
| `itertools` | reimplement `chain`, `cycle`, `repeat`, `groupby`, `islice`, `product` (3 days) |
| `collections` | `OrderedDict` (built on our dict), `defaultdict`, `Counter`, `deque`, `namedtuple` (4 days) |
| `json` | port CPython pure-Python json module (3 days) |
| `hashlib` / `hmac` | bind OpenSSL via extern "C" (1 day) |
| `struct` / `array` | pure Python bit-packing (2 days) |
| `io` | minimal `BytesIO` / `StringIO` (2 days) |
| `logging` | minimal subset (1 day) |
| `argparse` | pure Python; can lift from CPython's source (2 days) |
| `inspect` | subset; only what `dataclasses` / `typing` need (2 days) |
| `sys` | hand-written; mostly constants + `argv` + `exit` (1 day) |
| `traceback` | reuses our own frame walker from the py_exc runtime (1 day) |

**Acceptance 6C.4:**

| # | Test | Pass |
|---|---|---|
| 1 | `pcc/stdlib/` combined LoC | ≤ 4500 LoC |
| 2 | Each module has `test_<name>_parity.py` that compares outputs against CPython's stdlib on 20+ cases | All parity tests green |
| 3 | pcc.py imports zero CPython stdlib modules (checked by import-graph scan) | Pass |

#### 6C.5 — refactor pcc.py for pure-typed compilability (2 weeks)

Audit pcc's own code and remove dynamic features the frontend can't
handle cleanly.

**Expected changes:**

- All functions get type annotations (use `mypyc`-style strict).
- Replace any runtime `isinstance(x, (A, B, C))` chains with
  `match`-style dispatch on a discriminator field (since match-case
  isn't in our parser subset, use if/elif ladders on a type tag).
- Remove `getattr(obj, name)` where `name` is a runtime string;
  replace with explicit switch.
- Replace `**kwargs`-heavy config with typed dataclasses.
- Eliminate any `exec` / `eval` / `compile` builtins.
- Remove metaclass usage if any (Phase 3 supports them but keeping
  pcc metaclass-free simplifies bootstrap).

**Acceptance 6C.5:**

| # | Test | Pass |
|---|---|---|
| 1 | `mypy --strict pcc/` | zero errors |
| 2 | `grep -r "eval\|exec\|getattr.*[a-z]*," pcc/` excluding comments | zero matches |
| 3 | All pcc's pytest suite still passes under CPython | Same pass count as before refactor |

#### 6C.6 — bootstrap + verify (2 weeks)

The three-stage bootstrap and verification.

**Current verified state (2026-04-23):**

- `env -u LC_ALL ./scripts/bootstrap.sh` completes stage 1 / stage 2 /
  stage 3 on the supported macOS arm64 development host.
- Direct `cmp pcc2 pcc3` still differs on Mach-O code-signature metadata,
  but signature-stripped comparison copies are byte-identical.
- The compiled bootstrap binary's `--help` path now exits cleanly with
  empty stderr; the earlier embedded-CPython shutdown noise is gone.
- The default `python -m pcc` / `uv run pcc` entry path now routes
  through the internal `pcc.driver.cli_core` parser rather than importing the
  `click`-decorated CPython CLI path.
- `click` is no longer a runtime package dependency for `python-cc`;
  it remains only on the dev/test compatibility surface around
  `pcc.pcc.main`.
- The bootstrap script no longer needs repo-scoped `PYTHONPATH` or
  explicit embed-link env injection; compiled `pcc1` / `pcc2` now
  resolve the matching `python3.13-config` from the embedded
  interpreter's own `sysconfig` view.
- This is still not full Strategy C closure: the current run may link
  `libpython`, and broader host coverage / dependency removal remain open.
- The Python CLI/API now exposes an explicit `libpython` policy switch
  (`--python-libpython=auto|on|off` / `PCC_PYTHON_LIBPYTHON`): keep
  `auto` as the transitional default, but treat `off` as the eventual
  pure self-host gate. Any remaining fallback should fail loudly under
  `off`, not silently re-link `libpython`.

**Post-bootstrap cleanup track (do not forget after the gate is green):**

- Keep the current bootstrap-safe CLI path stable first.
- Then restore the general Python semantics currently avoided by
  bootstrap-safe workarounds, rather than treating those workarounds as
  permanent design:
  - CPython-backed string slicing / `=` option-value extraction
  - truthiness / equality on CPython-origin strings
  - stable `sys.exit(expr)` / imported `exit(...)` lowering
- Once those generic semantics are solid, retire the corresponding
  CLI-specific workarounds in `pcc.driver.cli_core` / `pcc.__main__`.

**Deliverables:**

- `scripts/bootstrap.sh`:
  ```bash
  # stage 1: CPython runs pcc, compiles pcc.py → pcc1
  python -m pcc pcc.py -o pcc1
  # stage 2: pcc1 (native) compiles pcc.py → pcc2
  ./pcc1 pcc.py -o pcc2
  # stage 3: pcc2 compiles pcc.py → pcc3
  ./pcc2 pcc.py -o pcc3
  # verify: pcc2 and pcc3 are byte-identical
  cmp pcc2 pcc3
  ```
- `scripts/selfhost_smoke.sh`: use `pcc2` to compile a 20-file C test
  corpus; compare against reference pcc output.

**Acceptance 6C.6:**

| # | Test | Pass |
|---|---|---|
| 1 | `cmp pcc2 pcc3` | byte-identical, or byte-identical after stripping nondeterministic build metadata such as Mach-O code signatures |
| 2 | `pcc2 hello.c -o hello` | same output as reference pcc |
| 3 | All of pcc's tests, run via `pcc2`-compiled test runner | identical pass/fail set vs reference |
| 4 | `ldd pcc2` | libc + libLLVM only; **no libpython**, no CPython extensions |
| 5 | pcc2 binary size | ≤ 4 MB |
| 6 | pcc2 compile time on a reference C file | ≤ 1.5× reference (CPython-run) pcc |
| 7 | Memory: ASAN + LSAN on pcc2 compiling 100 C files | zero leaks, zero UAF, zero double-frees |
| 8 | Portability | Bootstrapped successfully on macOS arm64, Linux x86_64, Linux arm64 |

**Gate — "pcc is self-hosted" claim:** tests 1, 2, 3, 4 must all
pass. Tests 5–8 are release quality gates.

### Phase 6C total: 16 weeks

| Sub-phase | Weeks |
|---|---|
| 6C.1 extern "C" FFI | 2 |
| 6C.2 LLVM C API binding | 3 |
| 6C.3 native Python parser | 3 |
| 6C.4 stdlib replacements | 4 |
| 6C.5 pcc source refactor | 2 |
| 6C.6 bootstrap + verify | 2 |
| **Total** | **16** |

### Risks specific to Strategy C

| Risk | Mitigation |
|---|---|
| LLVM C API drift across versions | Pin to LLVM 20.x; version check at startup |
| Our native Python parser has subtle divergence from `ast` | Differential fuzz: random Python snippets, compare pcc parse vs `ast.parse()` trees |
| `@dataclass` reimplementation misses edge cases (kw_only, slots, post_init) | Write explicit parity tests for each documented dataclass feature |
| PCRE2 linking adds a build-time dep | Bundle PCRE2 source as a git submodule, compile statically |
| Nondeterministic bootstrap output (6C.6 test 1) | Strip embedded timestamps / paths; accept structural equivalence as fallback |
| pcc uses `getattr` / dynamic dispatch more than anticipated | Audit in 6C.5; budget an extra week if needed |

### Parallelizability

Sub-phases 6C.1 (FFI), 6C.3 (parser), 6C.4 (stdlib) can run in
parallel once Phases 1–5 of the Python frontend are complete. 6C.2
(LLVM binding) depends on 6C.1 but can start as soon as FFI prototype
works. 6C.5 (refactor) can start anytime. 6C.6 requires all others
complete.

With a team of 2–3, 16 weeks could compress to 9–10. With a single
developer, 16 weeks is the expected runway.

---

## Revised Total Timeline (with Strategy C)

| Phase | Weeks | Cumulative |
|---|---|---|
| 1 MVP | 6 | 6 |
| 2 True semantics | 6 | 12 |
| 3 OOP + exceptions | 5 | 17 |
| 4 CPython fallback | 6 | 23 |
| 5 Optimization + release | 4 | 27 |
| **6C Pure self-host** | **16** | **43** |

**Total: 43 weeks (~10 months) single-developer full-time.**

Phase 6C is the definitive proof of the whole stack. Its acceptance
gates are strict because passing them means pcc is a real compiler
that has shed its Python-interpreter scaffolding entirely.

---

## Overall Acceptance ("pcc Python v1.0")

All of the following must hold before we call it a release:

1. Phase 1–5 acceptance criteria all passing.
2. The pyperformance benchmark suite: pcc passes **≥ 80%** of suites,
   with geomean speed **≥ 1.5× CPython** on pcc-runnable subset.
3. Codon-incompat corpus: pcc runs **≥ 90%** of a 100-file curated
   suite of real `.py` scripts from public PyPI projects.
4. Memory leaks: valgrind / ASAN on Phase 1–4 test corpus → **zero
   leaks** and **zero use-after-free** on fully-typed programs;
   known-and-tracked on L3 only.
5. Crash rate on the 100-file corpus: **≤ 2%** (no segfault on legal
   Python input).
6. License compliance: all runtime code MIT; CPython bridge respects
   PSF license.
7. Documentation: public-facing `README.md` for Python frontend,
   tutorial, limitations doc.

---

## Explicit Non-Acceptance (what we accept NOT doing at v1.0)

- Full `eval("..." )` / `exec("...")` support.
- Monkey-patching `builtins` at runtime.
- `__init_subclass__`, `__class_getitem__`, PEP 646 variadic generics.
- AsyncIO event loop (async/await lower to coroutine state machines
  but no I/O multiplexing).
- GPU / SIMD-auto-vec (leave to later, Codon's strong point).
- Threading with no-GIL.

These are explicitly "v2.0 candidates".

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Runtime lib exceeds 6 kLoC | Medium | Feature-gate optional parts; allow LLVM lib calls for common ops |
| Bignum semantics hard to get right | High | Pull in GMP behind a flag; write dedicated bignum property tests |
| Multi-inheritance MRO edge cases | Medium | Fuzz-test against CPython output on random class hierarchies |
| LLVM exception personality fn complexity | High | Start with Itanium C++ ABI unwind; move to DWARF-only later |
| libpython API drift across Python versions | Medium | Target 3.11–3.13 only; use limited API (PEP 384) where possible |
| Codon surpasses us on numeric speed before v1.0 | Low | Accept it; our differentiator is compat, not raw speed |

---

## Team / Timeline / Budget

Single-developer estimate: **27 weeks (≈ 7 months)** full-time.

With part-time commitment: **linear scaling expected**, e.g. 50%
full-time → 14 months.

No external dependencies assumed beyond:
- LLVM 20.x (already in pcc's stack)
- CPython 3.11+ headers + libpython
- `cloc` for LoC audits

---

## Decision Checkpoints

After each Phase, a re-evaluation:

| Checkpoint | Decision |
|---|---|
| End of Phase 1 | Ship as "pcc-py-typed" (Codon-competitor for typed Python) OR continue. |
| End of Phase 2 | Ship as "pcc-py-strict" (Python-semantics-correct typed) OR continue. |
| End of Phase 3 | Ship as "pcc-py-oop" OR continue. |
| End of Phase 4 | Ship as "pcc-py-compat" (the big Codon-beating release). |
| End of Phase 5 | v1.0 release. |

Each checkpoint is a legitimate end-point. No need to commit to all
27 weeks up front.

---

## Upstream References (for parity claims)

Because pcc's base plan (`docs/plans/all-pass-llvm-ir-1to1-master-plan.md`)
mandates upstream source anchors, the Python frontend inherits this:

| Claim | Anchor |
|---|---|
| Python semantics | `/private/tmp/cpython-3.13.x/Objects/longobject.c` (int semantics), `.../unicodeobject.c` (str), `.../dictobject.c` (dict), `.../typeobject.c` (class), `.../ceval.c` (eval loop — reference only) |
| Exception ABI | `/tmp/llvm-src/llvm-20.1.8.src/docs/ExceptionHandling.rst` + `/private/tmp/cpython-3.13.x/Python/errors.c` |
| Tagged int design | Codon's `codon/runtime/object.h` + existing mypyc literature |
| MRO algorithm | PEP 520, CPython `typeobject.c:mro_implementation` |

Every file under `pcc/py_codegen/` must cite the specific upstream
lines it mirrors, same policy as the LLVM IR pass plan.
