# Chapter 1: Introduction — Owning Python Execution

This book describes the design and implementation of pcc: a compiler and runtime system written in Python, built to compile Python to native code, and ultimately to compile itself. This first chapter deliberately contains no mechanism. It answers a single question: why does this system exist, and what discipline keeps it from quietly turning into something else? Nearly every design decision in the seventeen chapters that follow traces back to three anchors introduced here: the thesis (make Python execution ownable), the seven obligations (turn the thesis into checkable rules), and claim hygiene (always separate what has been proven from what has not). If you read only one chapter, read this one; if you want to challenge a design decision in a later chapter, come back here first and check whether it has drifted off the north star.

## Chapter Overview: Start with "Owning Execution"

This chapter is the contract for the rest of the book. Do not read it as a feature list; read it as three questions every later chapter must answer: can this code run without handing control back to CPython, has Python semantics been weakened, and is the mode of each claim labeled clearly?

- "Acceleration" asks whether the result is faster; "owning execution" also asks who emits code, who owns the runtime, and who is responsible for fallback boundaries.
- The five dividing lines explain how pcc differs from ordinary Python accelerators.
- The seven obligations are the ruler used later in the book, especially for mode labels, fallback honesty, and the bootstrap fixed point.

## 1.1 The Problem and the Design Space: Why an Accelerator Is Not the Goal

"Make Python faster" is a crowded design space. The established routes fall into three families: replace the interpreter and attach a just-in-time compiler (PyPy); compile annotated Python into C extension modules that run inside the CPython process (Cython, mypyc); or compile the whole program ahead of time to C whose generated code still calls into CPython's object runtime (Nuitka). Each of these routes is legitimate on its own terms, and each makes the same implicit choice: **ownership of execution stays with the CPython runtime (or with an opaque JIT).** The artifact is either an extension module that must be loaded into CPython, or an interpreter process carrying its own JIT. In neither case does the user end up holding a native artifact that can be independently audited, independently deployed, and reproduced by the same toolchain that built it.

pcc asks a different question: **who owns Python's execution?** The repository's design contract (the Project Intent section of [AGENTS.md](../../AGENTS.md)) states the thesis bluntly: pcc exists to give Python a native, auditable, self-hostable, no-libpython execution path. The goal is **not** to make selected Python programs faster — it is to make Python execution *ownable*: compiled, inspectable, bootstrappable, package-aware, runtime-extensible, and honest about every fallback boundary. Each adjective in that list is load-bearing. "Compiled" means an ahead-of-time native artifact, not a warmed-up JIT process. "Inspectable" means the IR, the object layouts, and the runtime contracts can be read and audited. "Bootstrappable" means the toolchain can reproduce itself, which Section 1.2 turns into a precise three-stage criterion. "Honest about every fallback boundary" means that when the system cannot prove Python semantics natively, it says so — loudly, with an error code — rather than degrading silently.

The most consequential sentence in that contract is pcc's stance on performance: **performance is a consequence of proven semantics, never a license to weaken Python behavior.** That sentence forecloses an entire family of familiar shortcuts. An accelerator may special-case popular libraries, may be "close enough" on corner semantics, may silently fall back to a slow path on unsupported idioms without telling anyone — because an accelerator's success metric is a benchmark number. pcc rejects that route, and not out of fastidiousness: its success metric is different. A system that claims to *own* execution, but whose semantics are stitched together from special cases and whose boundaries are papered over with silent fallbacks, makes every one of its claims unverifiable — and at that point the system has no reason to exist. The hard rules that follow from this stance recur throughout the book: no package-name special cases such as `if package == "numpy"` (Section 1.8.2 tells the story behind that rule), no silent fallback to LLVM after `--backend=self`, and no weakening of finalizers, weak references, or ownership semantics just to turn a gate green.

The same honesty applies to pcc's own current state. The status table in [README.md](../../README.md) is explicit: the C frontend is the mature part of the repository, validated against real projects — Lua, SQLite, PostgreSQL `libpq`, zlib, lz4, zstd, PCRE, OpenSSL, readline, nginx — while the typed-Python frontend is **experimental**. Unsupported Python idioms fail loudly by default, and only route through the CPython bridge when `--python-libpython=auto/on` is passed explicitly. pcc is not a drop-in replacement for Clang or CPython; it is a research compiler with practical integration tests. Every later chapter stands on that premise.

## 1.2 The Thesis and the Five Dividing Lines

Take away the five items below and pcc degenerates into yet another speedup tool; with them, it is a system rebuilding Python execution ownership. [AGENTS.md](../../AGENTS.md) lists them as the differentiators that must not decay into decoration:

```text
1. pcc1 -> pcc2 -> pcc3 self-hosted fixed point
2. five-GC comparative runtime (refcount/cycle, incremental, concurrent,
   generational, relocating) — a research program, not one collector
3. opt-in value model — identity-free immutable payloads for hot paths,
   with no theft of ordinary-class semantics
4. self backend as a first-class execution root
   (LLVM is oracle, not owner)
5. long-running runtime efficiency (pause / RSS / throughput /
   fragmentation over time, not single-shot compile+run speed)
```

**The self-hosted fixed point.** The bootstrap stage names are fixed vocabulary: `pcc0` is the host Python running the repository source; `pcc1` is the first native compiler binary it produces; `pcc1` compiles `pcc2`, and `pcc2` compiles `pcc3`:

```text
pcc0/host -> pcc1    pcc can produce a compiler
pcc1      -> pcc2    the produced compiler can reproduce the compiler
pcc2      -> pcc3    stable pcc2/pcc3 == a self-hosted fixed point
```

The fixed point is more than a byte compare. Byte-identical `pcc2` and `pcc3` is evidence that pcc's Python semantics, runtime, code generation, object model, backend, and diagnostics are coherent enough to reproduce themselves — nondeterminism in any layer amplifies into a visible difference between two self-compilations. The authoritative current state is frozen in [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) (captured 2026-05-01, the Issue 1 closure evidence): on macOS arm64, all three stages of both the LLVM chain and the self-backend chain link no `libpython`; on the strict path (`--backend self --python-libpython=off --ir-scaffold=on`), the IR emitted for `pcc2`/`pcc3` is byte-identical with zero `py_cpy_*` calls, and the binaries are byte-identical after Mach-O signature removal. Notice that every qualifier in that sentence — platform, backend, mode, comparison method — is deliberate; that is the claim hygiene of Section 1.4 at work. The full bootstrap story is Chapter 15.

**The five-GC comparative runtime.** The runtime ships five GC backend slots, selected at process startup through the `PCC_GC_BACKEND` environment variable. The enum lives in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h):

```c
// pcc/runtime/include/py_runtime.h
enum {
    PCC_GC_KIND_REFCOUNT_CYCLE = 0,
    PCC_GC_KIND_INCREMENTAL_TRICOLOR = 1,
    PCC_GC_KIND_CONCURRENT_MARK_SWEEP = 2,
    PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR = 3,
    PCC_GC_KIND_COLORED_RELOCATING = 4
};
```

Each backend mirrors a real reference implementation — CPython, Lua 5.4, Go (greentea), OCaml, ZGC — and the reference sources are kept in tree under [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research) so the port can be read next to the original. This is a comparative research program: five collectors running over one object-graph contract, and none of them is allowed to win by weakening semantics. Backend #0 remains the default and rollback reference (the decision is recorded in [docs/investigations/gc-backend-selection-matrix.md](../../docs/investigations/gc-backend-selection-matrix.md)). The architecture and the per-backend treatments are Chapters 10 and 11.

**The opt-in value model.** Value classes are explicitly opted-in, identity-free immutable payloads for hot paths; ordinary classes keep their full identity semantics (`id`/`is`/weakref/`__dict__`/mutation/subclassing/finalizers). What pcc borrows from Java's Project Valhalla is the **projection** model — separating semantic type from physical representation — and emphatically not Java's fixed-width wrapping `int`. See Chapter 16.

**The self backend as a first-class execution root.** The in-tree [pcc/backend/](../../pcc/backend) is an LLVM-free native emission path (currently covering subsets of AArch64 Darwin and x86_64 Linux). Its obligation is to be an execution root, not a demo: no silent fallback to LLVM after `--backend=self`. LLVM's role is oracle — a reference to validate against — not owner. See Chapter 13.

**Long-running efficiency.** The performance pcc cares about is pause time, RSS, throughput, and fragmentation over hours of runtime, not the stopwatch number of a single compile-and-run. This directly shapes how the GC and the runtime are measured (Chapters 10 and 11).

## 1.3 The Seven Obligations

The thesis lands through seven obligations. Each is operationalized by a track and gates in [goal-prompt.md](../../docs/goal/goal-prompt.md); here is the book's-eye view, with mechanisms deferred to their chapters.

1. **Compatibility claims must be mode-labeled.** A claim must say which mode produced it: host pcc ≠ pcc1; cpython-compat ≠ pcc-native; libpython ≠ no-libpython; LLVM backend ≠ self backend; stage1 ≠ the pcc1→pcc2→pcc3 fixed point. Section 1.4 expands on this.

2. **Performance must be proven.** A "C-like" claim requires IR-shape evidence, a runtime benchmark, and a slow path that preserves Python semantics when the assumptions fail. pcc does not claim that arbitrary dynamic Python reaches C speed — only the parts whose semantics are stable enough to lower natively. The obligation also demands recording violations honestly: the repository once carried one **confirmed violation of this obligation** — typed-int ABI paths for unboxed `+`/`*`/`<<` over explicitly annotated `int` silently wrapped on i64 overflow. The complete dossier, from confirmation through ruling to the fix (2026-06-17), is treated in Chapter 16.

3. **Ecosystem support must be generic.** NumPy, PyTorch, pandas, Arrow, and SciPy are integration targets, never compiler special cases. No `if package == "numpy"`; fix the reusable mechanism (install/import/ABI/buffer/capsule/build surface) and add a regression test for the generic feature. See Chapter 17 and Section 1.8.2.

4. **The self backend must become a first-class execution root.** No permanent LLVM dependency, and no silent fallback after `--backend=self`. See Chapter 13.

5. **The pcc1/pcc2/pcc3 fixed point is a contract.** Differences between stages must be **classified** (semantic / IR-text / class-layout / object-model / backend nondeterminism / link metadata / perf-only / diagnostic), never patched around. pcc2/pcc3 stability is a core correctness signal. See Chapter 15.

6. **Runtime design is part of the research goal.** The five GC backends are a comparative program; none may win by weakening finalizers, weak references, resurrection, suspended coroutine frames, scheduler queues, C-extension references, or value payloads. Efficiency is measured as a long-running property. See Chapters 10 and 11.

7. **The value model is the performance bridge, not a syntax gimmick.** Ordinary classes keep identity; value classes are opt-in, identity-free payloads with explicit boxing/unboxing, identity-escape diagnostics, GC tracing of pointer-bearing payloads, and a self-backend aggregate/scalar ABI. The obligation extends to `int` itself: `int` is Python's arbitrary-precision **semantic** type, with a value projection (the tagged small-int lane) and an object projection (the boxed bignum); value-lane overflow must deoptimize or promote, never wrap. Raw machine integers are the **explicit** `pcc.i64`/`pcc.u64` types — where wrap/trap/checked/saturating behavior is written into the type — or a proven-in-range internal optimization, never the silent default meaning of `int`. See Chapter 16.

These seven are not a vision list; they are a veto. When a change would trade one of them away for a local win — a faster benchmark, a greener gate, a smaller diff, a bootstrap made to pass by rewriting the construct it failed on — the contract requires stopping and surfacing the tradeoff instead of taking it silently. The obligations also explain why this book devotes a full chapter to engineering method (Chapter 18): in a system whose central claims are reproducibility claims, the test and investigation machinery is not support tooling; it is part of the design.

## 1.4 Honesty as Architecture: Mode-Labeled Claims

Writing "honesty" into an architecture sounds like smuggling a morals clause into a technical document. pcc's situation makes it an engineering necessity: the system carries too many orthogonal execution modes at once. The compiler itself may run under host CPython (host pcc) or as a bootstrap artifact (pcc1); Python input may take the strict no-libpython path or the explicit CPython bridge; the backend may be `llvm`, `llvm_capi`, or `self`; the package acceptance surface splits into cpython-compat and pcc-native; bootstrap evidence splits into stage1 and the full fixed point. In a space like this, an unlabeled claim ("pcc can run NumPy now") is not information — it is noise, and it routes subsequent engineering decisions in the wrong direction.

[goal-prompt.md](../../docs/goal/goal-prompt.md) §0.10 therefore writes claim hygiene as a table of inequalities; every claim must map onto a gate:

```text
host pcc pass          != pcc1 pass
cpython-compat pass    != pcc-native pass
libpython mode pass    != no-libpython pass
fake package pass      != real package pass
array-core pass        != import numpy pass
stage1 self-backed     != pcc1->pcc2->pcc3 self-backed
metadata exists        != runtime implementation complete
microbenchmark win     != whole-program performance win
```

The same document requires that the project's two slogans always appear as a pair; any document that keeps only the first may be overclaiming:

```text
Write Python. Run Native.
C-like speed where Python semantics can be proven.
Exact Python semantics everywhere supported.
```

The enforcement body for claim hygiene is not editorial review; it is a machine-checkable evidence hierarchy. The authority order in the repository is: **current focused tests, bootstrap gates, and JSON baselines outrank all prose.** Bootstrap status is owned by [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json) and enforced by [tests/python/test_bootstrap_gate_baseline.py](../../tests/python/test_bootstrap_gate_baseline.py); the no-libpython fallback surface is owned by [tests/fallback_baseline.json](../../tests/fallback_baseline.json), which forms a ratchet that is only allowed to tighten; each of the five GC backends has its own full three-stage bootstrap gate in `tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py`. The audit snapshot at the top of [docs/current-goal-state.md](../../docs/current-goal-state.md) records evidence row by row, and completion itself is graded — many entries carry the label `DONE_WEAK` (focused gates plus bootstrap verification passed, without claiming the full boundary), with an explicit list of what is *not* being claimed: not complete escape-boundary coverage, not complete dataclasses support, not a finished value model.

Sometimes claim hygiene takes the form of **deliberately keeping a failure**. Strict pcc-native mode rejects CPython-ABI extension artifacts with the error code `PCC-PKG-004` (the focused gate is [tests/python/test_package_extension_abi.py](../../tests/python/test_package_extension_abi.py)):

```python
# pcc/package/linkage.py
def _diagnostic_for_cpython_extension_abi(path: str) -> dict[str, object]:
    return {
        "code": "PCC-PKG-004",
        "message": (
            "native artifact name declares a CPython extension ABI; "
            "pcc-native mode requires a pcc-native extension ABI or a source rebuild"
        ),
        "path": path,
    }
```

[README.md](../../README.md) says outright that this blocker is intentional: it prevents a CPython ABI artifact from being misreported as pcc-native NumPy support. A system designed for demos would quietly open that path; a system designed for verifiability turns it into an explicit error code.

## 1.5 The Runtime in Four Layers: Moving Production into pcc-Python

No-libpython means only that an artifact does not depend on CPython; it does not automatically mean zero-libc. pcc's long-term contract is stronger than a minimized C kernel. The production runtime—including allocation, threads, safepoints, all five collectors, the libc-like substrate, and C-API ABI shims—moves into strict freestanding pcc-Python. The compiler retains only machine intrinsics for raw memory, atomics, system calls, and host ABI operations. C and vendored-libc sources remain differential oracles but ultimately leave the production link.

```text
compiler intrinsics   KEEP: raw memory / atomics / syscall / machine ABI
freestanding pcc-Py   GROW: allocator / threads / safepoints / GC / ABI shims
semantic pcc-Python   GROW: list / dict / str / dunder / exception / import
C/libc sources        REMOVE from production; retain as attributed oracles
```

The platform boundary must be labeled separately. Linux targets a supported static closure with no production C/libc object, `PT_INTERP`, `DT_NEEDED`, or undefined symbol. Darwin deliberately enters the operating system through named libSystem ABI calls and must not be called zero-libc. All five collectors consume one production pcc-Python slot trace/update and root/frame/native-handle contract; C participates only as a differential oracle. Chapter 14 develops the evidence ladder from no-libpython through pcc-Python ownership to zero-libc; Chapter 10 covers the slot contract.

## 1.6 Positioning: How pcc Differs from PyPy, Cython, Nuitka, and mypyc

Placed in the coordinate system of existing tools, the difference is not "who is faster" but which objective function each system optimizes. The descriptions below follow each project's public positioning and are not disparagement — every one of these tools is, on its own goals, far more mature than today's pcc.

- **PyPy** is an alternative interpreter with a tracing JIT, aiming at full-language compatibility. Execution happens inside the JIT process. It answers "how do existing Python programs get faster without modification" extremely well, but the artifact is not an independently auditable ahead-of-time native binary.
- **Cython** compiles an annotated superset of Python into C extension modules. The artifact runs inside the CPython process, linked against libpython; execution ownership remains entirely with CPython.
- **mypyc** compiles type-annotated Python into C extension modules, likewise hosted on the CPython runtime. Of the four it is closest to pcc's typed frontend in its input constraints, but the target artifact is fundamentally different.
- **Nuitka** performs whole-program ahead-of-time compilation, but the generated C still calls into CPython's object runtime. It eliminates bytecode interpretation; it does not (and does not intend to) replace the runtime itself.

pcc's axis is a different one: **not "faster Python inside CPython" but "Python execution without CPython"** — no-libpython native artifacts, a self-hosted fixed point, replaceable backends (LLVM and self), a five-GC comparative laboratory, and a methodology that locks all of those claims behind gates. The cost must be stated just as plainly: each tool above supports a far larger Python surface today than pcc's typed frontend does. Outside its native subset pcc fails loudly by default, and the self-host path requires pcc's own sources to avoid a long list of dynamic idioms — runtime `getattr`/`setattr`, several generator forms, decorators with runtime effects, dynamic imports, and more (see the limitations list in [README.md](../../README.md) and Chapter 5). The reason to choose pcc is not that it has already won; it is that it is betting on an axis nobody else is betting on.

## 1.7 One Mission, Not Two

pcc states an industrial thesis ("adopt pcc where native artifacts, no-libpython deployment, package-aware diagnostics, and hot-path specialization beat CPython") and an academic thesis ("a Python-authored compiler self-hosts into a no-libpython fixed point while exposing a disciplined runtime laboratory") at the same time. The contract insists this is one mission, not two, because each side feeds the other:

```text
industrial failures are research data    research artifacts are industrial trust
------------------------------------    -------------------------------------
import failure  -> C-API/ABI gap         fixed-point bootstrap -> reproducibility
Linux deploy    -> self-backend          five-GC matrix        -> runtime
   failure         target gap                                     credibility
long-running    -> GC/runtime            valueclass benchmarks -> performance
   regression      benchmark                                      proof
perf miss       -> value-model gap       package ABI reports   -> ecosystem trust
```

Section 1.8.2 makes this structure concrete: one real NumPy import failure decomposes into a series of generic mechanism gaps (the warnings module, typing markers, path operations, a regex subset, ...), and each gap's fix becomes a reusable compiler capability rather than a NumPy patch. Read in one direction, that is industrial work: a package a user wants gets closer to importing. Read in the other direction, it is research output: a measured map of exactly which import-machinery semantics a no-libpython Python still lacks. Neither reading survives without the other — which is why the hinge connecting the two theses is a single rule: **every claim must say exactly what it proves and what it does not prove.**

The declarative GUI follows the same logic. It does not bring back a webview or JavaScript runtime: pcc-Python owns composition, scheduling, events, styles, commands, and lifecycle, while AppKit/CoreGraphics/Metal remain named native ABI boundaries. Readable source and a machine contract exist today, while formal acceptance tasks remain open; Chapter 20 develops the mechanism and its claim boundary.

## 1.8 History and Lessons

Every chapter of this book ends with real investigations from [docs/investigations/](../../docs/investigations). Chapter 1's two stories are both about claim hygiene itself — one about how an overclaim was discovered and institutionally corrected, the other about how the "no package-name special cases" rule operates against a real package.

### 1.8.1 The Value Model "Implemented Through V6" Overclaim (May 2026)

**Symptom.** The investigation file [docs/investigations/python-valhalla-value-model-actual-state.md](../../docs/investigations/python-valhalla-value-model-actual-state.md) (updates dated 2026-05-19 through 2026-05-24) opens with the finding: the value-model plan and status report claimed the Valhalla-inspired track was implemented through V6.

**Wrong assumption.** Scaffolding had been mistaken for implementation. [pcc/value_model.py](../../pcc/value_model.py) really does contain host-side dataclasses named `ValuePayload`, `ValueBox`, `SpecializedArray`, and `GenericSpecialization` — names that map one-to-one onto the planned V1–V6, and that look finished.

**Evidence chain.** Code inspection said otherwise: only the V0 slice was actually wired into type inference and class lowering ([pcc/frontends/python/py_ast.py](../../pcc/frontends/python/py_ast.py) defines `ValueClassType`; [pcc/frontends/python/type_infer.py](../../pcc/frontends/python/type_infer.py) recognizes `@pcc.valueclass`). V1 had no direct LLVM struct ABI and no IR-shape gate proving hot paths avoid object allocation; V4's `pcc.array[Point]` contiguous payload runtime did not exist; V5's monomorphization and type-tuple cache did not exist; V6's hot-object migration and allocation benchmarks did not exist. The plan's expected V1–V4 tests were partially absent before the investigation opened.

**Real root cause.** The status surface did not distinguish "metadata exists" from "runtime implementation complete" — which is precisely the line `metadata exists != runtime implementation complete` in the §0.10 table; this incident is where it comes from.

**Fix and the invariant left behind.** The fix was not to delete the optimistic sentences but to make honesty an API: `value_model_status()` now reports `implemented_through` (at the time, the V1 scalar-payload subset), `scaffolding_through == "V6"`, `production_runtime is False`, and a `not_implemented` list enumerating the missing runtime, codegen, GC, specialization, and benchmark work:

```python
# pcc/value_model.py
def value_model_status() -> dict[str, object]:
    return {
        "implemented_through": (
            "V1-direct-scalar-and-nested-payload-eq-checked-marshal-"
            "v2-pointer-and-nested-dyn-boundary-partial"
        ),
        "scaffolding_through": "V6",
        "production_runtime": False,
        "marker": "@pcc.valueclass",
    }
```

Status itself became a testable assertion target. The story has a recursive footnote: the first patch adding V0 source-shape diagnostics itself pushed the no-libpython fallback count for `pcc.frontends.python.type_infer` from the baseline 846 up to 951 (ratchet cap 888) and was caught on the spot by [tests/python/test_fallback_baseline.py](../../tests/python/test_fallback_baseline.py) — even code written *in the service of honesty* must pass the same ratchet. The repair (routing diagnostic construction through one `_raise_frontend_error` helper) brought the count back to 851. The lesson: overclaiming is not a character flaw, it is a missing-instrumentation problem; once the instrumentation exists, the overclaim is caught by a test instead of by a reader.

### 1.8.2 Real NumPy's First Import: Where the `if package == "numpy"` Ban Comes From (2026-05-27)

**Context and symptom.** [docs/investigations/numpy-first-import-libpython-fallback.md](../../docs/investigations/numpy-first-import-libpython-fallback.md) tracks the live boundary on the B-P0-PKG track: a freshly bootstrapped pcc1, running with `PCC_HOST_PYTHON=/usr/bin/false` (the host Python made unusable, ruling out any host-side cheating), compiles a program containing `import numpy` against the repository-local real NumPy 2.4.4 source. The investigation begins by correcting a claim drift: the opt-in gate `tests/python/test_package_import_path.py::test_pcc1_real_numpy_first_import_boundary_opt_in` was still asserting a long-superseded blocker (a `NoneType` marshal failure), while the actual current boundary was `PCC-PY-COMPILE-001`: the multi-file compile still requires libpython fallback, with residual `py_cpy_*` emission concentrated in modules such as `numpy.f2py.symbolic` and `numpy.f2py.func2subr`. The first fix (Proposal No.1) was to make the gate assert the **current failure shape** — a passing test whose content is a precise record of where the system still fails.

**Wrong assumption and diagnostic discipline.** The first short debug trace showed only two IR lines before `py_cpy_ensure_init()` and was insufficient; a full IR dump located the first fallback edge inside `user_numpy___getattr__` — `numpy/__init__.py`'s `__getattr__` importing `warnings`. The investigation left a diagnostic guardrail behind: a two-line `PCC_DEBUG_BOOTSTRAP_TRACE` context is a locator, not root-cause evidence; before touching code, confirm the enclosing `define`, the actual `py_cpy_*` call, and the argument source from full IR.

**Method: fix only generic mechanisms.** Every subsequent fallback shrink is a generic compiler capability, each labeled in the investigation as containing no NumPy-specific branch: `import warnings` registered as a native builtin module alias ([pcc/frontends/python/codegen/import_lowering.py](../../pcc/frontends/python/codegen/import_lowering.py), mirroring the existing narrow [pcc/stdlib/warnings.py](../../pcc/stdlib/warnings.py) shim); literal-only `textwrap.dedent(...)` constant folding (dynamic strings still fall back — no pretense of full textwrap compatibility); folding `typing.TYPE_CHECKING` to false at codegen time and accepting metadata keywords on `TypeVar(..., covariant=True)` — which took `numpy._typing._nested_sequence` from 10 `py_cpy_*` call sites to 0; native lowering for `os.path.getsize` and `Path(...).suffix`; a native subset for direct `re.match`/`re.search` and the `re.I`/`re.S` constants; and `re.compile(...).match/search` lowered to a runtime helper returning a real `PY_TYPE_FUNC` object — described in the investigation as a bound-method boundary, *not a fake truthy regex object*. The guarded `findall` subset accepts only the two literal patterns actually observed in package code, and states plainly that unsupported patterns **keep the existing fallback path; they are not replaced with a fake empty list**. Every evidence block ends with the same qualifier: this is fallback-surface shrinkage only; it does not prove a successful `import numpy`. The fallback-helper count in `numpy.f2py.crackfortran` fell measurably — 1555 to 1302 to 1228 to 1220 — progress expressed as numbers, claims capped at the unchanged boundary.

**A real package as research data.** The same investigation exposed two generic bugs with no NumPy content at all (Proposal No.3): the shallow lifter used by parallel export workers mis-sent class-header keywords (`class _DTypeDict(_DTypeDictBase, total=False)`) into expression lifting and crashed; and the cross-worker export wire format serialized non-literal defaults (`dtype=int`, `axis=-1`, `keepdims=np._NoValue`) as missing, turning them into required parameters. Both were fixed in the generic paths of [pcc/frontends/python/pipeline.py](../../pcc/frontends/python/pipeline.py) and validated with a fresh full three-stage bootstrap. This is the loop of Section 1.7 closing: an industrial failure (importing a real package) yields research data (semantic-equivalence defects in the frontend's parallelization).

**The invariant left behind.** Why ban `if package == "numpy"`? Because this investigation shows what the special case actually costs: a package-name branch can turn a gate green without bringing any mechanism into existence — the next package fails at exactly the same gap, while the status board already says "NumPy supported." The generic-mechanism route is slower, but every step is monotone: the fallback ratchet only tightens, the gate records the current failure shape, and evidence lines up with claims one for one. The Package/NumPy Claim Hygiene section of [AGENTS.md](../../AGENTS.md) codifies the practice: install success is not import success; a synthetic package with the same name does not count; cpython-compat evidence must not impersonate pcc-native evidence.

## 1.9 Summary

pcc's thesis is to make Python execution ownable: native, auditable, self-hostable, no-libpython. The thesis stands on five dividing lines — the bootstrap fixed point, the five-GC comparative runtime, the opt-in value model, the self backend as a first-class execution root, and long-running efficiency — and on seven obligations that turn them into daily rules. Performance, in this system, is a consequence of proven semantics; honesty is not documentation etiquette but an architectural component implemented with inequality tables, JSON baselines, fallback ratchets, and gates. The target implementation language of the production runtime is pcc-Python: the freestanding layer owns low-level facilities, the semantic layer owns Python behavior, compiler intrinsics express only the machine boundary, and C/libc becomes oracle material. Against existing tools, pcc bets on the axis of "Python execution without CPython," and accepts the present reality that its typed frontend remains an experimental subset. The two case studies show the same thing from both sides: what the instrumentation does when claim hygiene is violated, and what the discipline produces when it is obeyed — a real failure converted into a chain of reusable capabilities. The chapters that follow expand every noun in this chapter into mechanism, and keep testing this discipline in their own History and Lessons sections.

## Exercises

1. **(Read the source.)** Open [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h), find the five members of the `PCC_GC_KIND_*` enum, and locate each backend's reference implementation directory under [docs/refs_docs/gc-research/](../../docs/refs_docs/gc-research). Which backend is the default, and which investigation document records that decision?

2. **(Read the baseline.)** Read [tests/bootstrap_gate_baseline.json](../../tests/bootstrap_gate_baseline.json). Write three claims it **can** prove, in mode-labeled language (naming platform, backend, and comparison method), then two superficially similar claims it **cannot** prove (for example, anything involving Linux, or runtime performance).

3. **(Claim hygiene.)** Pick any two rows of the §0.10 inequality table and give a concrete scenario for each in which the left side holds and the right side does not. State which gate would have to pass to upgrade the left-side evidence into the right-side claim.

4. **(Read the investigation.)** Work through the 2026-05-27 sections of [docs/investigations/numpy-first-import-libpython-fallback.md](../../docs/investigations/numpy-first-import-libpython-fallback.md). List at least four of the "generic mechanism" fixes, and for each, identify which non-NumPy programs would have been denied that capability if it had been implemented as a NumPy special case. Why does the guarded `findall` subset refuse to substitute an empty list for unsupported patterns?

5. **(Design-tradeoff argument.)** Suppose someone proposes a `--fast-numpy` flag: when an import of NumPy is detected, enable a set of NumPy-specific lowering shortcuts in exchange for a demonstrable import success. Evaluate the proposal against this chapter's seven obligations, one by one: which does it violate, in what form, and is there an equivalent benefit path that violates none of them?
