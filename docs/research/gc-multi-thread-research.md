# GC + Multi-thread Research

This document consolidates the design discussion for pcc's pluggable GC
and multi-threading story. It builds on:

- `docs/issues/gc-semantics-gap.md` (G0–G5 core path; G6–G10 research)
- `docs/issues/gc-pluggable-backend.md` (5-backend abstraction)
- `docs/research/gc-survey/` (CPython / Lua / Go-greentea / OCaml / ZGC reference notes)
- Reference source code under `/tmp/gc-research/<project>/`

The intent is **not** a delivery plan — it's the technical reasoning
behind why pcc's current 5 GC backends and (planned) threading model
are designed the way they are, with concrete evidence from runtime
code and reference implementations.

---

## 1. The 5 GC backend abstraction

### 1.1 Backends and reference impls

```
PCC_GC_KIND_REFCOUNT_CYCLE         = 0  ↔  CPython gc.c + gc_free_threading.c
PCC_GC_KIND_INCREMENTAL_TRICOLOR   = 1  ↔  Lua lgc.c
PCC_GC_KIND_CONCURRENT_MARK_SWEEP  = 2  ↔  Go-greentea mgc.go + mgcmark.go
PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR = 3  ↔  OCaml minor_gc.c + major_gc.c
PCC_GC_KIND_COLORED_RELOCATING     = 4  ↔  ZGC zMark.cpp + zBarrier.cpp
```

Names are intentionally **algorithmic, not project-branded**
(`tests/test_gc_abstraction_surface.py::test_gc_backend_kinds_are_algorithmic_not_project_branded`)
but each maps directly to a reference under `/tmp/gc-research/`. The
research source provides the design oracle; pcc's backend should
converge toward each project's algorithmic shape (not its
implementation details).

### 1.2 Completion vs reference (as of 2026-05-04)

| # | Algorithm | Reference LOC | pcc LOC (share) | Completion |
|---|---|---|---|---|
| 0 | refcount-cycle | 1993 (CPython gc.c) | 484 + 120 = 604 | ~85% |
| 1 | incremental-tricolor | 1743 (lgc.c) | shared in 682 (`py_gc_backend.c`) | ~50% |
| 2 | concurrent-mark-sweep | 7000+ (Go runtime) | shared in 682 | ~15% |
| 3 | generational-minor-major | 3134 (minor+major gc) | shared in 682 | ~25% |
| 4 | colored-relocating | 2542 (ZGC core) | shared in 682 | ~10% |

### 1.3 What's actually implemented vs what's just shape

**Backend 0 (refcount-cycle)** is the only fully usable backend:

- Classic update_refs / subtract_refs / mark_reachable / clear-cycles
- Container traversal: list / tuple / dict / set / func / iter / gen /
  coroutine / **exception** / instance
- O(1) hash index for `py_gc_find_node` (replaced O(N) linked-list scan)
- Native `gc` module API: `collect / disable / enable / get_count /
  get_threshold / set_threshold / is_tracked`
- Single-generation only (CPython has 3); `get_referrers` /
  `get_objects` xfailed

**Backends 1–4 are skeletons sharing one 682-line file.** Each
implements:
- Color flags (`WHITE`, `GRAY`, `BLACK`, `YOUNG`, `OLD`, `PINNED`,
  `RELOCATION_CANDIDATE`, `SWEEP_CANDIDATE`)
- A common `pcc_gc_step(budget)` that branches on the selected
  backend
- Telemetry counters (allocations, write/read barriers, safepoints,
  pin balance, work steps)
- `pcc_gc_objects` linked-list (only populated for non-refcount
  backends)
- Frame-root registration (`pcc_gc_frame_enter / leave`)

**None of backends 1–4 actually free or move objects** in their own
algorithm. The fallback to backend 0's refcount-cycle path is what
keeps memory bounded. Specifically:

- **#1 / #2** — sweep_unreachable wired but not exercised because
  default allocator (`py_list_new` etc.) doesn't enter
  `pcc_gc_objects`. Only test programs using `pcc_gc_alloc` extern
  reach it.
- **#3 generational** — YOUNG → OLD aging works; REMEMBERED set
  promotion works. **But there is no minor or major collection.**
  Aged-into-OLD objects are never reclaimed by the generational
  algorithm; #0's refcount path tears them down.
- **#4 colored-relocating** — read barrier counts loads; step marks
  candidates. **No forwarding pointer, no relocation, no concurrent
  collector.** ZGC's three signature features (colored pointers,
  multi-mapping, concurrent relocate) are all absent.

### 1.4 Open algorithmic holes per backend

| # | What's missing (vs reference) |
|---|---|
| 0 | 3-generation; `get_referrers` / `get_objects`; `gc.garbage` list |
| 1 | WHITE0 / WHITE1 atomic-phase invariant; codegen-inserted write barriers; debt-based pacing; `__gc` finalizer integration |
| 2 | Real concurrency (no thread spawned!); parallel mark workers; mutator mark assist; span-based allocator; pacer |
| 3 | Real minor GC (no bump-pointer young heap, no copy); real major GC; nothing freed by the backend itself |
| 4 | Colored pointers; multi-mapping; load barrier with pointer fixup; forwarding table; relocation; concurrent relocate |

---

## 2. The multi-threading gap

The 4 non-refcount backends advertise concurrency / parallelism
features they cannot implement, because **pcc has no multi-threading
infrastructure at all**.

### 2.1 Evidence

```bash
# pthread bindings
grep "pthread_create\|pthread_join\|pthread_mutex" pcc/runtime + codegen + stdlib
# → 0 hits

# atomic ops in pcc's own runtime
grep "__atomic_\|atomic_fetch\|stdatomic" pcc/runtime
# → 1 hit, only in py_libpython.c (CPython bridge)

# GIL or equivalent
grep "PyGIL\|pcc_gil\|gil_take" pcc/
# → 0 hits

# threading module native dispatch
grep '"threading"' pcc/frontends/python/codegen/layer1.py
# → 0 hits
```

`pcc/runtime/src/py_obj.c:125` shows refcount is non-atomic:

```c
void py_incref(PyObject *o) { ... h->refcount++; }
void py_decref(PyObject *o) { ... if (--h->refcount > 0) return; }
```

Two threads running decref simultaneously on the same object → race
→ double-free or leaked refcount.

`pcc/stdlib/concurrent.py:1` is explicit:

> "sequential-fallback `concurrent.futures` skeleton ... For self-host
> we degrade to sequential execution"

### 2.2 The 7-layer gap

Threading isn't a single missing primitive — it's a stack of 7
mutually-dependent pieces:

```
1. Thread spawn primitive          (pthread / clone — none)
   ↓
2. Atomic memory operations         (none in pcc's own runtime)
   ↓
3. GIL or equivalent                (no GIL, no per-object locks)
   ↓
4. TLS for Python user state        (only exception state; no threading.local())
   ↓
5. threading module native API      (Thread / Lock / Event / ... none)
   ↓
6. concurrent.futures / multi-proc  (sequential stub)
   ↓
7. GC concurrency protocol          (no safepoints, no write/read barriers
                                     in codegen, no STW protocol)
```

**Each layer depends on layers below it.** Adding layer 5 alone
(`threading.Thread`) without layer 1 (pthread) is impossible; without
layers 2–3 it's incorrect (refcount races).

### 2.3 Why it's missing

Three reasons, in order of weight:

1. **Deliberate scope**. pcc's bootstrap goal is "compile pcc itself
   + small Python programs". Single-threaded suffices. codex's
   parallel emit work (P-1.A) sidesteps via `subprocess.Popen`
   multi-process — pcc binary itself stays single-thread.

2. **Compounding complexity**. Adding pthread alone is useless:
   needs atomic refcount + thread-safe containers + GIL or
   fine-grained locks + safepoint codegen. Each isolated layer
   makes the others moot.

3. **Strategic punt**. `gc-pluggable-backend.md` G4 (atomic
   refcount): "gated on having a multi-threading story (not
   currently a goal); defer."

---

## 3. The four mainstream threading models

### 3.1 Python — two coexisting modes

| Mode | Characteristic | Where |
|---|---|---|
| Classic GIL | One thread runs Python at a time; refcount non-atomic, protected by GIL; threads good for I/O concurrency only | CPython 3.0–3.13 default, 3.14 default |
| 3.13t / 3.14t free-threaded (PEP 703 + 779) | GIL removed; biased + atomic refcount; per-object container locks; ~10% single-thread regression | CPython 3.13t (experimental), 3.14t (officially supported, `python3.14t` build) |

PEP 779 makes free-threading **supported but not default**. The `t`
suffix on the binary name (`python3.14t`) selects the
`--disable-gil`-built interpreter. Both binaries coexist with
separate wheel selection (`cp314` vs `cp314t`).

PEP 703 free-threading mechanics (from
`/tmp/gc-research/python/gc_free_threading.c`):

```c
struct PyObject {
    uintptr_t ob_tid;          // owning thread id
    uint32_t  ob_ref_local;    // owner-only fast path (non-atomic)
    uint32_t  ob_ref_shared;   // cross-thread (atomic)
};

// hot path:
if (op->ob_tid == _Py_ThreadId())   // ① register-cheap tid compare
    op->ob_ref_local++;              // ② non-atomic ++
else
    _Py_atomic_fetch_add(&op->ob_ref_shared, _SHIFT);
```

### 3.2 Go — M:N goroutines

- Goroutines: 2 KB initial stack, grows; userspace scheduled
- GMP scheduler: M goroutines on N OS threads (`GOMAXPROCS = cores`)
- Cooperative yield at function calls, channels, GC safepoints
- Channels for sync; happens-before via channel ops + sync package
- Concurrent tricolor GC with write barrier (no refcount)
- Native code (cgo) blocks goroutine scheduling; `runtime.LockOSThread`
  pins explicitly

### 3.3 OCaml 5 — domains + effects

From `/tmp/gc-research/ocaml/minor_gc.c:125`:

```c
void caml_set_minor_heap_size (asize_t wsize)
{
  if (domain_state->young_ptr != domain_state->young_end) {
    caml_minor_collection();
  }
  ...
}

// minor_gc.c:773
static void caml_stw_empty_minor_heap (caml_domain_state* domain, ...) {
  caml_stw_empty_minor_heap_no_major_slice(...);
}
```

- **Domain** = 1 OS thread + own minor heap (bump-pointer)
- Domain count limited (typically num_cores)
- Major heap shared across domains, concurrent mark-sweep
- C11 atomics + `caml_global_barrier()` for cross-domain sync
- Effect handlers for fine-grained concurrency *within* a domain
- Pre-OCaml-5 had only green threads (cooperative, single-core)

### 3.4 Java 21 — virtual threads (Project Loom)

- **Virtual thread** = JVM-managed lightweight unit (KB scale)
- **Carrier thread** = real OS thread in `ForkJoinPool.commonPool()`,
  default count = `availableProcessors()`
- Mount/unmount: blocking ops cause VT to detach from carrier; carrier
  free to run another VT; VT re-mounts when ready (possibly on
  different carrier)
- Stack stored on heap; mount/unmount = swap stack + register state
- API unchanged: `Thread.ofVirtual().start(...)`
- Scaling: millions of VTs on `N = cores` carriers
- **Real parallelism = N (carrier count)**, NOT VT count
- **Pinning**: `synchronized` blocks, JNI calls, native methods pin
  the VT to its carrier (degrades to 1:1)

### 3.5 OCaml domain vs Python 3.14t — fundamentally same threading layer

After reading both reference sources, the threading **infrastructure**
is essentially identical:

| Dimension | OCaml 5 domain | Python 3.14t |
|---|---|---|
| Thread unit | 1:1 pthread | 1:1 pthread |
| Stack | OS stack ~1MB | OS stack ~1MB |
| Memory model | C11 atomics | C11 atomics |
| Sync primitives | pthread mutex/cond + atomic | pthread mutex/cond + atomic |
| Safepoint | compiler-inserted poll | bytecode + STW poll |
| STW protocol | global_barrier | _PyEval_StopTheWorld |
| **Differences** | **GC algorithm only** | |
| Heap | per-domain minor + shared major | shared single heap |
| Refcount | none | biased + atomic |
| Cycle handling | none (no refcount, no cycles) | STW cycle collector |
| Write barrier | minor → major refs | cross-thread refcount merge |

**Conclusion**: differences live entirely in the GC layer. Threading
itself is the same `pthread + C11 atomics + safepoint + STW`
substrate.

---

## 4. Threading is not a separate pluggable axis

Given §3.5, attempting to make threading model pluggable separately
from GC backend is wasted abstraction. Three axes properly separated:

```
┌──── Axis 1: OS thread primitive (build-time single choice) ──────┐
│  pthread 1:1  /  M:N scheduler  /  Loom-style carrier+VT          │
│  → pcc choice: pthread 1:1 (matches all 4 mainstream languages    │
│    underneath; complexity is in upper layers, not here)           │
└────────────────────────────────────────────────────────────────────┘

┌──── Axis 2: User-visible thread API (fixed) ─────────────────────┐
│  Python `threading.Thread / Lock / RLock / Event / ...`           │
│  → pcc requirement: Python compatibility                          │
└────────────────────────────────────────────────────────────────────┘

┌──── Axis 3: GC + memory semantics (5 pluggable backends) ────────┐
│  #0 refcount-cycle: biased refcount + STW cycle GC (Py 3.14t)     │
│  #1 incremental-tricolor: write barrier + step (Lua)              │
│  #2 concurrent-mark-sweep: bg mark + write barrier (Go)           │
│  #3 generational: per-thread minor + shared major (OCaml domain)  │
│  #4 colored-relocating: load barrier + relocation (ZGC)           │
└────────────────────────────────────────────────────────────────────┘
```

Axes 1 and 2 are fixed. Axis 3 is the pluggable surface — and the
threading-interaction differences (does the backend need bg threads?
where does it stop the world? does it need a per-thread heap?) are
**internal to each backend**, all using the same axis-1 substrate.

This mirrors what the 4 mainstream languages do: they share the same
underlying `pthread + atomic + safepoint` plumbing; they differ in
how their GC uses it.

---

## 5. Why VT (Loom) cannot replace 3.14t free-threading directly

Hypothetical: implement `import threading` in pcc as Loom-style
virtual threads, keep the Python user-facing API unchanged. Three
hard incompatibilities make this unworkable.

### 5.1 PEP 703 biased refcount needs stable OS thread id

PEP 703's hot path reads `_Py_ThreadId()`. On modern CPUs this is
**one register read** (`gs:[0]` on x86, `mrs x0, tpidr_el0` on
arm64). 99% of incref/decref hit this fast path because objects are
mostly accessed by their owning thread.

Under VT mount/unmount:

```
T0: VT-A on carrier-1 allocates obj O → ob_tid = carrier_1_tid (1234)
T1: VT-A blocks → unmount → carrier-1 free
T2: scheduler mounts VT-A on carrier-3 (tid 5678)
T3: VT-A increfs O:
    o->ob_tid (1234) ≠ _Py_ThreadId() (5678) → atomic slow path
```

99% fast-path optimization → 0% fast-path. Every incref now atomic.

The "fix" — use VT-id instead of OS-tid — requires:
- Carrier TLS slot pointing to current VT (extra indirection)
- Update on every mount/unmount (added context-switch cost)

The added overhead consumes the biased savings. **This is precisely
why Java/Loom uses tracing GC instead of refcount** — refcount is
fundamentally incompatible with mount/unmount.

### 5.2 Extern call → forced pinning

VT mount/unmount only works if the scheduler can serialize the VT's
stack. C compiler doesn't emit stack-frame-layout metadata sufficient
for that:

```c
PyListObject *py_list_new(int64_t cap) {
    PyListObject *lst = malloc(sizeof(PyListObject));
    //                  ^^^^^^ inside malloc:
    //   - callee-saved regs r12/r15/x19 saved to OS stack
    //   - call _malloc_internal
    //   - _malloc_internal calls _mmap (kernel)
    //   - kernel adds OS thread to wait queue
    //   - real suspend!
}
```

Once the call descends into C, the scheduler can't unmount until the
C frame returns. The OS thread (carrier) is **truly suspended** by
the kernel.

pcc's call density into C runtime is much higher than Java's JNI
density:

```python
def hot_loop(items):
    total = 0
    for x in items:                 # py_iter_new (extern)
        total = total + x.value     # py_obj_getattr → py_int_add (extern)
    return total                    # py_int_from_i64 (extern)
```

99% of every Python statement is sitting inside a C frame. Java VTs
benefit from a high JVM-bytecode density. pcc has the inverse density
profile.

### 5.3 PEP 703 memory model assumes stable thread identity

Critical sections, ownership transfer, `ob_ref_shared` ordering — all
assume "thread" is a stable OS-pthread identity. VT mount/unmount
breaks this.

Specific failure: ownership confusion under shared carrier:
```
VT-A on carrier-1: alloc obj O, ob_tid = carrier_1_tid
VT-A unmount, VT-B mount on carrier-1
VT-B increfs O: ob_tid == carrier_1_tid == _Py_ThreadId()
  → VT-B takes fast path! (wrong: VT-A owned the object)
Meanwhile VT-A on carrier-3 also increfs O:
  → atomic slow path
Two paths racing on ob_ref_local → corruption.
```

Fixing this needs ownership transfer protocols at every mount —
expensive enough to wipe out the bias.

Java/Loom dodges this by **not using refcount**. Java's memory model
(JMM) is built on monitors and `volatile`/`synchronized` — these bind
to lock objects, not thread identity, so VT context switches don't
break invariants.

### 5.4 The "API-preserving but semantics-different" trap

You can preserve `threading.Thread(...).start()` syntactically while
swapping the runtime. The result:

- Code compiles and runs
- Performance is worse than 3.14t (biased fast path dead, every
  extern call pins)
- Memory model differs from PEP 703 in subtle ways (ownership
  ordering, critical-section semantics)
- `threading.local()` semantics either cost mount/unmount swap or
  break per-VT expectations

It's "Python-shaped wrapper around a different concurrency model."
Worse than picking a model and committing to it.

---

## 6. Why pcc's extern density is structurally high

Aside from the threading question — pcc's call density into C
runtime is itself a design observation worth recording.

### 6.1 Every basic op enters C

```python
xs = []                # → py_list_new       (extern, 8 init steps)
xs.append(x)           # → py_list_append    (extern)
print(xs)              # → py_print_many     (extern)
y = obj.attr           # → py_obj_getattr    (extern)
i = a + b              # → py_int_add        (extern, unless tagged-int fast path)
```

This is **standard for any Python implementation with a C runtime**
(CPython, Cython, MicroPython all do this). It's not pcc-specific
laziness. The reason:

`py_list_new` performs ~8 sequential operations:
1. malloc(sizeof(PyListObject)) → may touch mmap
2. h.refcount = 1
3. h.type_tag = PY_TYPE_LIST
4. h.flags = 0
5. capacity = cap
6. length = 0
7. items = malloc(cap * 8) ← **second malloc**
8. py_gc_track(lst) ← hash-table insert into GC side index

Inlining 50 instructions × 100 alloc sites = code-size explosion.

### 6.2 Java's TLAB + tracing-GC simplifies allocation

Java `new ArrayList()` JIT-inlines to ~8 instructions:

```asm
mov   rdi, [tls + TLAB_TOP_OFFSET]       ; ① TLAB current ptr
lea   rsi, [rdi + 24]                     ; ② current + size
cmp   rsi, [tls + TLAB_END_OFFSET]        ; ③ overflow check
ja    slow_path                           ; ④ rare extend
mov   [tls + TLAB_TOP_OFFSET], rsi        ; ⑤ commit ptr
mov   [rdi + 0], MARK_WORD_INITIAL        ; ⑥ header (mark word)
mov   [rdi + 8], CLASS_PTR_ARRAYLIST      ; ⑦ header (klass)
mov   dword [rdi + 16], 10                ; ⑧ initial fields
```

Three contributing factors:
1. **TLAB**: per-thread bump-pointer pre-allocated 256KB buffer.
   No per-object malloc, no syscall. Fast path is pure register
   arithmetic.
2. **Object header is 16 bytes fixed** (mark word + klass pointer).
   No refcount, no type tag separate from klass, no GC-tracked flag.
3. **Tracing GC needs no per-allocation registration.** Allocator
   doesn't call into GC; GC discovers new objects via root scanning
   on the next cycle.

GraalVM Native Image (Java AOT) inlines the same fast path. Binary
grows ~1.5–2× over JIT mode but remains manageable.

### 6.3 What pcc could borrow

| Optimization | Cost | Allocation speedup | Python compatibility |
|---|---|---|---|
| TLAB per-thread bump allocator | 2–3 weeks runtime work | 5–10× | Compatible |
| Combine type_tag + flags into 32-bit klass-shaped header | low | marginal | Compatible |
| Drop py_gc_track per-alloc; lazy GC discovery | high (requires tracing GC for cycles) | 2× | **Breaks refcount-cycle backend** |
| Drop refcount entirely | catastrophic (PEP 442 `__del__` timing) | 1.5× | **Breaks Python semantics** |
| Layer1 inline alloc fast path | medium | match Java | depends on above |

A non-breaking phase-1 path:
- Add TLAB bump allocator in runtime
- `py_list_new` fast path: TLAB bump + 6 field init, **defer
  py_gc_track to lazy GC-time discovery**
- Layer1 inlines the ~10-instruction fast path; slow path stays as
  function call

Estimated 3 weeks; brings allocation cost to GraalVM-class levels
without breaking refcount-cycle backend.

---

## 7. AOT vs JIT positioning

### 7.1 Where pcc beats JITs

- **Zero startup latency** — no warm-up. Critical for CLI / serverless / embedded.
- **Predictability** — no deopt, no trace-cache eviction, no JIT
  pause spikes. Important for soft-realtime.
- **Memory** — no trace cache, no type profile storage, no deopt
  metadata. Same code 5–10× smaller working set than PyPy.
- **AOT binary** — statically linkable, embeddable, deployable as a
  single artifact.
- **LLVM optimization passes** — mem2reg, sroa, function-attrs, ADCE
  apply to the whole program at compile time.

### 7.2 Where JITs (especially PyPy) beat pcc

- **Runtime type specialization** — PyPy sees that `x` is always
  `int` at this site, generates int-only code. pcc only knows what
  static types say.
- **Cross-function trace inlining** — PyPy traces follow control
  flow across function boundaries. pcc inlining is local.
- **Adaptive specialization** — different inputs trigger different
  versions. pcc emits one version per function.
- **Polymorphic inline cache** — dynamic attribute access can be
  ~native speed when the receiver type stabilizes.

### 7.3 Real numbers from pcc

From `tasks.md` / `docs/issues/performance-gaps.md`:

- task 102: bootstrap-time ratio **2.83×** vs host CPython (gate
  fail) — pcc compiling itself is 2.83× slower than CPython
  compiling pcc source. This includes pcc's own runtime emit cost,
  not just the compiled code's runtime.
- task 72: tagged-int inline fast path delivered **75× speedup** for
  `py_int_mul/mod/cmp` — typed-integer code is pcc's strongest
  region.

The pattern: **typed code → pcc wins big; dyn-fallback code → pcc
roughly matches CPython interpreted, behind PyPy steady-state**.

---

## 8. Recommended path forward

### 8.1 Threading model decision

**Adopt CPython's PEP 703 free-threading model directly** (skip
phase-1 GIL). Rationale:

- pcc has no third-party C extension ecosystem to break (CPython's
  primary migration cost)
- pcc runtime is ~5K lines vs CPython's hundreds of thousands —
  atomic refcount migration is bounded
- Two platforms (macOS arm64 + Linux x86_64) with well-defined
  C11 atomics
- No bytecode compatibility burden

Estimated work: ~3 months (vs CPython's multi-year project).

### 8.2 Single threading layer, GC-internal differentiation

```
shared layer (one impl, all backends use it):
  pthread_create / pthread_join
  pthread_mutex / pthread_cond
  __atomic_fetch_add / compare_exchange
  __thread / pthread_setspecific (TLS)
  pcc_safepoint() — compiler-inserted poll
  pcc_stop_the_world() / pcc_resume_world()

each GC backend uses the layer differently:
  #0 refcount-cycle:
      biased refcount via ob_tid + ob_ref_local + ob_ref_shared
      STW cycle collector
  #3 generational (OCaml-domain shape):
      per-thread minor heap (bump-pointer)
      global barrier for STW minor
      shared major heap with concurrent mark-sweep
  #2 concurrent-mark-sweep (Go shape):
      background mark thread
      write barrier on object stores
      brief STW only at mark termination
  #4 colored-relocating (ZGC shape):
      background relocate thread
      load barrier with pointer fixup
      mostly no STW
```

### 8.3 Refcount strategy as a sub-axis (mirror of GC backend)

```c
enum {
    PCC_REFCOUNT_NONATOMIC = 0,    // single-thread fallback
    PCC_REFCOUNT_ATOMIC    = 1,    // naive atomic_fetch_add/sub
    PCC_REFCOUNT_BIASED    = 2,    // PEP 703 biased (default)
    PCC_REFCOUNT_DEFERRED  = 3,    // PEP 703 + deferred for immortals
};
```

Compile-time selection (`PCC_REFCOUNT_STRATEGY=biased`); same
PyObjectHeader layout across strategies. Allows staged delivery
mirroring CPython's PEP 703 evolution: atomic first, then biased,
then deferred.

### 8.4 Allocator path in parallel

Add TLAB-style bump allocator independent of threading work
(§6.3). Brings allocation cost to GraalVM-class. Combined with
`py_gc_track` lazy registration, allows layer1 to inline alloc fast
path without binary explosion.

### 8.5 Out of scope — research-only

- Go-style M:N userspace scheduler — fundamentally divergent from
  Python `threading` semantics; revisit only if asyncio backend
  needs goroutine-shaped concurrency
- Loom-style virtual threads — requires stdlib I/O rewrite
  (multi-month) AND incompatible with PEP 703 refcount; skip
- ZGC-style colored pointers — multi-mapping not available on macOS;
  pcc's tagged-int already uses the low pointer bit; further bit
  pressure unattractive

---

## 9. Decision matrix

| Question | Decision |
|---|---|
| Should pcc support real multi-threading? | Yes, eventually. Currently scoped out. |
| Which threading model? | PEP 703 free-threading (skip GIL phase) |
| OS thread primitive? | pthread 1:1 (matches all 4 reference languages underneath) |
| User API? | Python `threading.*` (compatibility requirement) |
| Threading-model pluggable? | No. The GC backend is the pluggable surface; threading is its substrate. |
| Refcount strategy pluggable? | Yes. 4 strategies, compile-time selection, mirrors GC backend abstraction. |
| Allocator pluggable? | Future work. Initial path: add TLAB fast path; keep slow path as today. |
| Adopt OCaml domain semantics for #3 backend? | Yes — per-thread minor heap is a clean fit, and the threading layer is shared with #0. |
| Adopt Loom virtual threads? | No. Pinning density on pcc's C-runtime path defeats the optimization. |
| Adopt Go goroutines? | No (user-facing). Internal scheduler ideas may inform asyncio. |

---

## 10. Open questions

1. **GIL-equivalent or skip directly to free-threading?** Current
   recommendation: skip. But this front-loads ~3 months of work
   before any threading is usable. Phase-1 GIL would deliver basic
   threading in ~2 weeks at the cost of disposable code (GIL gets
   removed in phase 2).

2. **TLAB before or after threading?** Allocator improvements are
   independent. Could land before threading and benefit
   single-threaded perf immediately (task 102 bootstrap gate).
   Recommended: parallel tracks.

3. **How to validate concurrent GC correctness?** No reference
   implementation in the no-libpython closure. Likely path: compare
   against PyPy / CPython 3.14t under the same workloads, plus
   synthetic stress (race-detector on a writeable critical sections,
   ThreadSanitizer build).

4. **Codegen barrier insertion timing.** Write barriers for
   concurrent GC must be emitted at every object-pointer store.
   Doing this incrementally (only on backends that need it) requires
   per-backend codegen mode. Doing it always pays the cost on backend
   #0 (which doesn't need it). Recommended: per-backend codegen
   mode flag, mirrors `_module_uses_raw_int_scaffold` shape.

5. **Should #3 (generational) implement OCaml's domain model
   verbatim or adapt?** OCaml's per-domain minor heap is the most
   complete reference. Direct port works on macOS arm64 + Linux
   x86_64. Open question: how many domains? CPython 3.14t doesn't
   have an equivalent; OCaml caps at core count. For pcc, "as many
   as `threading.Thread` instances" is incompatible (could be
   thousands). Likely answer: keep small fixed domain count, route
   user threads onto domains via work-stealing (similar to Loom
   carriers, but with per-carrier minor heap — a hybrid).

---

## Appendix B — pcc runtime substrate wiring

The first implementation slice corresponding to §4 and §8.2 is now in the
pcc runtime tree:

| pcc path | role |
|---|---|
| `pcc/runtime/src/pcc_threads.c` | Shared single-thread/pthread substrate: stable thread id, atomic refcount helpers, mutex/cond wrappers, safepoint, STW gate. |
| `pcc/runtime/src/py_obj.c::pcc_gc_safepoint` | Polls `pcc_thread_safepoint()` before advancing backend work. |
| `pcc/runtime/src/py_obj_gc.c::py_gc_collect` | Backend #0 wraps the cycle-collector update/subtract/mark/dealloc phases in `pcc_stop_the_world()` / `pcc_resume_world()`. |
| `pcc/runtime/src/py_gc_backend.c::pcc_gc_step` | Backend #3 polls `pcc_thread_safepoint()` at bounded young/remembered promotion boundaries. |
| `pcc/runtime/py/py_obj.py`, `py_obj_gc.py`, `py_gc_backend.py` | pcc-Python runtime archive mirrors the same substrate calls so the no-libpython closure stays aligned with the C runtime. |

This is deliberately still below the Python `threading` API. It makes the
single substrate visible to GC internals without adding a second runtime axis.

## Appendix A — Reference source paths

```
/tmp/gc-research/python/gc.c                  — CPython classic GIL GC
/tmp/gc-research/python/gc_free_threading.c   — CPython PEP 703 GC
/tmp/gc-research/lua/lgc.c                    — Lua incremental tricolor
/tmp/gc-research/go-greentea/mgc.go           — Go runtime concurrent GC
/tmp/gc-research/ocaml/minor_gc.c             — OCaml 5 minor (per-domain)
/tmp/gc-research/ocaml/major_gc.c             — OCaml 5 major (concurrent)
/tmp/gc-research/ocaml/shared_heap.c          — OCaml 5 shared heap + STW
docs/refs_docs/gc-research/zgc/zMark.cpp      — OpenJDK jdk-27+21 ZGC concurrent mark
docs/refs_docs/gc-research/zgc/zBarrier.cpp   — OpenJDK jdk-27+21 ZGC load barrier
docs/refs_docs/gc-research/zgc/zGeneration.cpp — OpenJDK jdk-27+21 GenZGC young/old policy entry
docs/refs_docs/gc-research/zgc/zRememberedSet.cpp — OpenJDK jdk-27+21 GenZGC remembered-set reference
```

## Appendix B — pcc current GC source paths

```
pcc/runtime/src/py_obj_gc.c           — backend #0 cycle collector (484 lines)
pcc/runtime/src/py_gc_index_table.c   — hash index (120 lines)
pcc/runtime/src/py_gc_backend.c       — backends #1-#4 shared (682 lines)
pcc/runtime/py/py_obj_gc.py           — pcc-Python port of #0 (609 lines)
pcc/runtime/py/py_gc_backend.py       — pcc-Python port of #1-#4 (808 lines)
pcc/runtime/include/py_runtime.h      — public ABI (pcc_gc_*)
pcc/frontends/python/codegen/runtime_abi.py   — frontend ABI table for runtime helpers
```
