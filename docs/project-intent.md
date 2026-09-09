# Project Intent

The standing project direction and semantic obligations, preserved from the
original AGENTS.md section. Read before changing project direction or trading
a requirement for a local improvement. [Compiler contracts](compiler-contract.md)
specify the dependency/public-entry boundaries; [AGENTS.md](../AGENTS.md)
specifies the working procedure.

The goals below remain requirements. Implementation-status observations in
this original text describe its recorded revision (`d87f8594`); verify current
capability in source and matching execution. References to the retired goal
protocol identify the historical track/gate mapping, not an active task queue.

> This section is the top-level design contract. It exists to keep autonomous
> work aligned: when a change would trade away one of the obligations below for
> a local win — a faster benchmark, a greener gate, a smaller diff, a passing
> bootstrap by rewrite — **stop and surface the tradeoff instead of taking it
> silently.** This section is the *why*; `docs/archive/goal/goal-prompt-through-2026-09-06.md` is the *how*
> (tracks, gates, claim hygiene, prohibitions). If you find yourself weakening
> Python semantics, mislabeling a mode, or special-casing a package to make
> progress, you are off the north star — re-read this section.

**Thesis.** pcc exists to give Python a native, auditable, self-hostable,
no-libpython execution path. The goal is **not** merely to make selected Python
programs faster — it is to make Python execution *ownable*: compiled,
inspectable, self-hostable, package-aware, runtime-extensible, and honest about
every fallback boundary. pcc treats performance as a **consequence of proven
semantics, never a license to weaken Python behavior.**

**What separates pcc from a Python accelerator.** Six things. Without them pcc
is just another speedup tool; with them it is a system rebuilding Python
*execution ownership*. Do not let any of these decay into decoration:

```text
1. pcc1 -> pcc2 -> pcc3 self-hosted fixed point
2. five-GC comparative runtime (refcount/cycle, incremental, concurrent,
   generational, relocating) — a research program, not one collector
3. opt-in value model — identity-free immutable payloads for hot paths, with no
   theft of ordinary-class semantics (Java's Project Valhalla is a conceptual
   reference only, not pcc's brand or design constraint)
4. self-backend owns execution (LLVM may only be an external reference)
5. long-running runtime efficiency (pause / RSS / throughput / fragmentation
   over time, not single-shot compile+run speed)
6. complete Python execution ownership — implement every missing surface in
   pcc and remove every CPython/libpython/LLVM/host/C-owner or hidden fallback;
   until a surface is implemented, fail closed with an explicit capability
   diagnostic rather than silently changing execution owner
```

**The fixed point is more than a byte compare.** It is evidence that pcc's
Python semantics, runtime, codegen, object model, backend, and diagnostics are
coherent enough to reproduce themselves:

```text
pcc0/host -> pcc1     pcc can produce a compiler
pcc1      -> pcc2     the produced compiler can reproduce the compiler
pcc2      -> pcc3     stable pcc2/pcc3 == a self-hosted fixed point
```

**Seven obligations.** Each is operationalized by a track + gates in
`docs/archive/goal/goal-prompt-through-2026-09-06.md`; the one-line form here is the guardrail, and the
parenthetical is where it is actually enforced:

```text
1. Compatibility must be mode-labeled. A claim must say which mode produced it:
     host pcc != pcc1   |   cpython-compat != pcc-native
     libpython != no-libpython   |   LLVM-backed != self-backed
     stage1 != pcc1->pcc2->pcc3 fixed point
   (`docs/archive/goal/goal-prompt-through-2026-09-06.md` §0.10 claim hygiene, §9.2 mode boundaries)

2. Performance must be proven. C-like claims require IR-shape evidence + runtime
   benchmark + a slow path that preserves Python semantics when assumptions fail.
   pcc does not claim arbitrary dynamic Python becomes C-speed — only the parts
   whose semantics are stable enough to lower natively. (C-track, §16)

3. Ecosystem support must be generic. NumPy / PyTorch / pandas / Arrow / SciPy
   are integration targets, never compiler special cases. No `if package ==
   "numpy"`; fix the reusable mechanism (install/import/ABI/buffer/capsule/
   build-surface) and regress the generic feature. (B-track, §9.1, §14)

4. Self-backend must become a first-class execution root, not a forever-LLVM
   dependency. No silent fallback to LLVM after --backend=self. (S-track, §10)

5. The pcc1/pcc2/pcc3 fixed point is a contract. Differences are *classified*
   (semantic / IR-text / class-layout / object-model / backend nondeterminism /
   link metadata / perf-only / diagnostic), not patched around. pcc2/pcc3
   stability is a core correctness signal. (§0.10, §19.2)

6. Runtime design is part of the research goal. The five GC backends are a
   comparative program; none may win by weakening finalizers, weakrefs,
   resurrection, suspended coroutine frames, scheduler queues, C-extension
   refs, or value payloads. Measure efficiency as a long-running property.
   (G-track/§12, T-track/§13)

7. The value model is the performance bridge, not a syntax gimmick. Ordinary
   classes keep identity (id / is / weakref / __dict__ / mutation / subclass /
   finalizer / dynamic attrs). Value classes are opt-in, identity-free payloads
   with explicit boxing/unboxing, identity-escape diagnostics, GC tracing of
   pointer-bearing payloads, and self-backend aggregate/scalar ABI. (The concept
   is the obligation; "Valhalla" is only the reference it was distilled from.)
   What pcc borrows from Valhalla is the PROJECTION model (semantic type vs
   physical representation; value/object projection; boxing bridge; optimization
   never changes semantics) — NOT Java's fixed-width `int` wrap. This applies to
   `int` itself: `int` is a Python arbitrary-precision SEMANTIC type with a value
   projection (tagged small-int lane) and an object projection (boxed bignum);
   value-lane overflow must deopt/promote, never wrap. Raw machine integers are
   the EXPLICIT `pcc.i64`/`pcc.u64` type (where wrap/trap/checked/saturating is
   written in the type), or a proven-in-range internal optimization — never the
   silent default meaning of `int`. (value model / V-track, §11)
```

**One mission, not two.** Industrial failures are research data (import failure
-> C-API/ABI gap; Linux deploy failure -> self-backend target gap; long-running
service regression -> GC/runtime benchmark; perf miss -> value-model gap), and
research artifacts are industrial trust (fixed-point bootstrap -> reproducibility;
five-GC matrix -> runtime credibility; valueclass benchmarks -> performance
proof; package ABI reports -> ecosystem trust). The industrial thesis ("adopt
pcc where native artifacts, no-libpython deploy, package-aware diagnostics, and
hot-path specialization beat CPython") and the academic thesis ("a
Python-authored compiler self-hosts into a no-libpython fixed point while
exposing a disciplined runtime laboratory") reinforce each other. **Every claim
must say exactly what it proves and what it does not prove.**

**Accelerator execution is an extension of the ownership thesis, not a sixth
mission — and not the overclaim it is easy to make.** The repo already contains
a real GPU/Metal thread (`pcc/kernel_ir/`, `pcc/gpu_gc/`, `pcc/dist/`) that the
five pillars above did not name; this paragraph gives it an honest home so the
intent stops lagging the code. It belongs to the same "ownable execution"
thesis: the target is **native accelerator execution ownership** — a
host/device-split kernel IR, no-libpython device launch, and GC-aware external
resource lifetime — with **TVM/TIRx and TileLang used as oracles/reference
shapes, never as runtime owners** (the same relationship the value model has to
Valhalla and the self-backend has to LLVM). What is actually proven today is
narrow and must be stated at its claim level (`pcc/kernel_ir/gpu_claims.py`
levels 0-6): a real Metal kernel-IR path with on-device result proofs for small
fixed-shape kernels, **local-machine and hardware-gated**. What is **not**
provided: whole-program GPU, executing `import tvm` / `import tilelang`
(only a fail-closed parser of a TileLang-*shaped* DSL subset exists), external
framework interop, and any real distributed/ds4 runtime (`gpu_gc`/`dist` are
CPU oracles). This thread **must not displace the self-host -> 5-GC -> value ->
runtime-efficiency spine**: it is M5 breadth, and calling a GPU slice "done"
requires the same mode-labeled claim hygiene as every pillar.

**Runtime layering: the production runtime is authored in pcc-Python, including
the low-level kernel.** The long-term goal is not a smaller hand-written C
runtime. Allocation, object headers, atomics/refcount barriers, platform
syscalls, threading primitives, dynamic loading, extension ABI entrypoints,
safepoints/stack maps, and all five GC implementations migrate to a strict
freestanding pcc-Python subset and are compiled by pcc into native code. The
machine boundary is compiler-owned raw-memory/syscall/atomic intrinsics and a
specified ABI, not a permanently hand-maintained C kernel. Existing C and
vendored libc sources are transition implementations and differential oracles;
they are not the final production dependency. Distinguish these layers:

```text
compiler intrinsics   KEEP: raw memory, atomics, syscall/host-ABI entry and
                      machine operations; no Python object semantics.
freestanding pcc-Py   GROW: allocator, threads, safepoints, GC, libc-like
                      substrate and ABI shims; no heap/boxing/GC dependency
                      while bootstrapping those facilities themselves.
semantic pcc-Python   GROW: list/dict/str/dunder/exception/import semantics.
C/libc sources        REMOVE from the production dependency after differential
                      and fixed-point gates; retain only as attributed oracles.
```

This is stronger than no-libpython: the final Linux zero-libc claim requires no
production C/libc runtime dependency either. Darwin may still enter the OS
through named libSystem ABI calls and must not be labeled zero-libc. The
**5-GC Production Equality Rule** (`docs/archive/goal/goal-prompt-through-2026-09-06.md`, G-track) still
requires every backend to consume ONE slot-based trace/update contract
(`py_obj_visit_slots` / `py_obj_update_slot` / root + frame + native-handle
registration). During migration the C oracle and pcc-Python implementation must
stay differential-equal; completion removes the C implementation from the
production link rather than preserving two implementations indefinitely.
