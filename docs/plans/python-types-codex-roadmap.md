# Python types & data-model roadmap — Codex execution plan

**Date:** 2026-05-08.
**Audience:** Codex (and any AI agent picking up the next ready task).
**Authoritative source-of-truth surfaces this consolidates:**

- `docs/issues/python-data-model-gaps.md` — Phases D1–D8 (descriptor → introspection)
- `pcc_multi_year_roadmap.md` §5.1 — full required compatibility surface
- `docs/python-limitations.md` — built-in-type and CLI gap inventory
- `docs/python-scorecard.md` — current corpus pass-rate baseline (2026-04-20: 106/137)
- `docs/issues/gc-semantics-gap.md` — memory-model dependencies (cycle GC, weakref, finalizer)

This doc does **not** re-state the per-phase scope and bug examples already in
`python-data-model-gaps.md`. It sequences, adds the missing surfaces from
the multi-year roadmap §5, and turns each phase into a Codex task block.

---

## Why a separate "types" plan

The phrase "implement Python types" in pcc context spans three layers and they
have been tracked separately:

1. **Built-in concrete types** (`int`, `float`, `str`, `bytes`, `list`, `dict`,
   `set`, `tuple`, `bool`, `None`) — the `pcc/runtime/src/py_*.c` family.
   Mostly there; gaps in `bytes` literal, `list.sort` method, untyped-bignum
   formatting (see `docs/python-limitations.md` §"Python runtime types").
2. **Data-model protocols** (descriptor, generator, async, context-manager,
   number/comparison/iteration, `__format__`, pickle/copy, introspection) —
   tracked as Phases D1–D8 in `python-data-model-gaps.md`. **D1 closed**
   (focused contract); D2–D8 open.
3. **Type-system surface** (metaclasses, `__slots__` inheritance, `typing`
   runtime, `Protocol`, `TypeVar`, `Generic`, `dataclass(frozen=True)`,
   reflection: `getattr`/`setattr`/`dir`/`type(x)`, weakrefs) — partially
   listed in §5.1 of the multi-year roadmap, not phased anywhere yet.

Codex needs one ordering across all three. This doc provides it.

---

## Status snapshot — 2026-05-08

| Layer | Item | State | Owner doc |
|---|---|---|---|
| Built-in | `bytes` literal `b"..."` | ❌ AST node missing — falls back to latin-1 `str` | `python-limitations.md` |
| Built-in | `list.sort()` user-visible method | ❌ pending | `python-limitations.md` |
| Built-in | bignum format on `print(10**40)` | ⚠️ unverified | `python-limitations.md` |
| Built-in | `type(x)` / `type(x).__name__` | ❌ pending | `python-limitations.md` |
| Built-in | dunder `__iter__` / `__next__` on user class | ✅ parity green | `python-limitations.md` |
| Built-in | dunder `__hash__` / `__str__` on user class | ❌ pending | `python-limitations.md` |
| Built-in | `__dunder` name mangling (`_ClassName__x`) | ❌ pending | `python-limitations.md` |
| Built-in | Class-level variable read/write (`Cls.count`) | ❌ pending | `python-limitations.md` |
| Built-in | `raise X from Y` (`__cause__`) | ❌ pending | `python-limitations.md` |
| Built-in | full C3 linearization for diamond `super()` | ❌ pending | `python-limitations.md` |
| Built-in | traceback frames in codegen | ⚠️ runtime ready, codegen not wired | `python-limitations.md` |
| Built-in | `module.attr = val` write | ✅ native builtin modules | `python-limitations.md` |
| Built-in | `module.fn(*args)` / `**kwargs` splat | ❌ pending | `python-limitations.md` |
| Built-in | decorators with runtime effects on user fn | ❌ pending | `python-limitations.md` |
| Data-model | D1 descriptor protocol | ✅ focused contract closed; introspection edges open | `python-data-model-gaps.md` |
| Data-model | D2 generator state machine | ❌ pending | `python-data-model-gaps.md` |
| Data-model | D3 async / await + asyncio min | ❌ pending (depends D2) | `python-data-model-gaps.md` |
| Data-model | D4 context manager full semantics | ❌ pending (depends D3 for `async with`) | `python-data-model-gaps.md` |
| Data-model | D5 iteration / number / comparison polish | ❌ pending | `python-data-model-gaps.md` |
| Data-model | D6 `__format__` and format-spec | ⚠️ builtin numeric f-string specs green; user `__format__` pending | `python-data-model-gaps.md` |
| Data-model | D7 pickle / copy | ❌ pending (depends D8 import) | `python-data-model-gaps.md` |
| Data-model | D8 dynamic import + introspection | ❌ pending | `python-data-model-gaps.md` |
| Type-system | Metaclasses (`class C(metaclass=M)`) | ❌ pending | this doc, T1 |
| Type-system | `weakref.ref` runtime | ❌ pending (depends GC-4) | `gc-semantics-gap.md` |
| Type-system | `typing` runtime: `TypeVar`, `Generic`, `Protocol` | ⚠️ ignored at runtime; checked statically only | this doc, T2 |
| Type-system | `dataclass(frozen=True)` semantics | ✅ used in pcc bootstrap | `pcc/stdlib/dataclasses.py` |
| Type-system | `dataclass(frozen=False)` setattr through `__init__` | ⚠️ default-None slot bug (memory: feedback_pcc_dataclass_default_none_setattr) | this doc, T3 |
| Type-system | C-extension ABI (load `.so`) | ⚠️ via libpython only; no native ABI | `pcc_multi_year_roadmap.md` §5.1 |
| Type-system | Concurrent mutation under user `Lock` | ❌ lost-update bug (Task #5) | tasks.md, this doc, T0 |

Legend: ✅ done, ⚠️ partial, ❌ pending.

---

## Sequencing

Dependency edges (read top-down):

```
T0 concurrency-correctness  -- blocks all multi-thread acceptance
        |
        v
B1 bytes literal --,
B2 type(x) builtin -+--> B3 class-var read/write -> B4 user-class dunders (iter/hash/str)
                    |
                    +--> B5 raise-from / traceback frames
                    |
                    +--> B6 module.attr writes / kwarg splat
        |
        v
D1 descriptor (closed, edge follow-up only)
        |
        v
D2 generator -----> D3 async/await -----> D4 context-manager (full)
        |                                       |
        +--> D5 iter/num/cmp polish ------------+
        |
        +--> D6 format / format-spec
        |
        v
D8 dynamic import + introspection ---> D7 pickle/copy
        |
        v
T1 metaclass --> T2 typing runtime --> T3 mutable dataclass setattr --> T4 weakref (also needs GC-4)
        |
        v
T5 native C-extension ABI (research, not first-year scope)
```

Blockers:

- **T0 must land first.** Until concurrent mutation under a user `Lock` is correct,
  any thread-related acceptance (D2 generator's state machine under contention,
  D3 asyncio loop, T1 metaclass instance creation in multi-threaded code) is
  meaningless. See investigation log in this doc's tail.
- **D7 depends on D8** — `pickle` needs dynamic import to load custom reducers.
- **T4 weakref** depends on GC-4 from `gc-semantics-gap.md`.

---

## Codex task blocks

Each block is the minimum a single Codex run should pick up. Format:

> **ID** — title
> *Dep:* prerequisite IDs.
> *Scope:* what the task changes.
> *Files:* file paths Codex will likely edit.
> *Acceptance:* test path that gates the task.
> *Out of scope:* what to refuse to expand into.

---

### T0 — fix concurrent-mutation lost-update under user `Lock`

*Dep:* none.
*Scope:* root-cause + fix for `lock.acquire(); counts[0] = counts[0] + 1; lock.release()` losing
50%+ of updates with 4 threads at N≥1000 iters. C-level paths
(`pcc_mutex_lock`, `py_threading_lock_acquire` PyObject API, the same RMW
sequence in C) all serialize correctly at 4000/4000. The Python codegen
emission path through `user_threading_Lock_acquire` →
`py_instance_get_field(self, 0)` → `py_threading_lock_acquire(_ptr)` does not.
Strongest current hypothesis: `py_instance_get_field` reads `inst->fields[idx]`
with no acquire ordering and high-contention reads return a stale or
neighbouring value, so `py_threading_lock_acquire(NULL)` returns -1 and
`Lock.acquire`'s `False` return is silently ignored.
*Files:*
- `pcc/runtime/src/py_class.c` (`py_instance_get_field`)
- `pcc/runtime/src/py_threading.c` (`py_threading_lock_acquire`,
  `py_threading_lock_release` — add abort-on-NULL to validate hypothesis)
- `pcc/stdlib/threading.py` (`Lock.acquire` — raise on `_lock_acquire == -1`
  rather than silently returning `False`)
- `pcc/frontends/python/codegen/...` (emit a return-value check on
  `Lock.acquire` that raises and aborts)
*Acceptance:*
- New `tests/test_threading_concurrent_mutation.py`:
  - 4 threads, 1000 iters each, `counts[0] = counts[0] + 1` under one `Lock` →
    `counts[0] == 4000` for 100 consecutive runs.
  - 4 threads, 1000 iters each, `list.append` under one `Lock` → length 4000,
    no SIGABRT for 100 consecutive runs.
- `tests/test_boc_threading_proof.py` continues to pass (the embarrassingly-parallel
  speedup proof must not regress).
*Out of scope:* GC-4 weakref work; sub-interpreter design.

---

### B1 — `bytes` literal AST node + native `bytes` type

*Dep:* T0.
*Scope:* introduce a dedicated `BytesLit` AST node so `b"non-ascii \xff"` does
not lower as latin-1 `str`. Wire codegen to emit a native `PyBytesObject`
literal; thread through type-infer; add `bytes.__len__` / `bytes.__getitem__`
basics that pcc already exposes for `str`.
*Files:*
- `pcc/frontends/python/py_ast.py` (add `BytesLit`)
- `pcc/frontends/c/parse/py_*` (lex literal `b"..."` / `rb"..."`)
- `pcc/frontends/python/type_infer.py` (`BytesType`)
- `pcc/frontends/python/codegen/layer1.py` (emit literal)
- `pcc/runtime/src/py_bytes.c` (already exists; verify the literal helper)
*Acceptance:*
- `tests/data_model/test_bytes_literal.py` covers ASCII, non-ASCII, mixed
  with str, indexing, slicing, `len()`.
- `b"\xff" == bytes([0xff])` is `True`.
*Out of scope:* `bytearray`, buffer protocol, `memoryview`.

---

### B2 — `type(x)` builtin + `type(x).__name__`

*Dep:* T0.
*Scope:* native `type()` returns the runtime class object; `.__name__` returns
the source-declared name. Already wired for class objects internally; this
exposes it as a user builtin.
*Files:*
- `pcc/frontends/python/codegen/layer1.py` (recognise the builtin call)
- `pcc/runtime/src/py_class.c` (`py_type_of_object` already exists; add
  user-facing entry)
- `pcc/stdlib/builtins.py` (if present) or codegen-direct dispatch
*Acceptance:*
- `tests/data_model/test_type_builtin.py`: `type(1) is int`, `type([])` is
  `list`, `type(C())` is `C`, `type(C()).__name__ == "C"`.
*Out of scope:* `type(name, bases, dict)` 3-arg form (lives in T1 metaclass).

---

### B3 — class-level variable read/write

*Dep:* B2.
*Scope:* `Cls.count` reads and writes the class-level `count` slot rather
than failing or routing through libpython. `@classmethod` full support
unblocks once this lands.
*Files:*
- `pcc/frontends/python/codegen/class_gen.py`
- `pcc/runtime/src/py_class.c` (class-level field slots)
*Acceptance:*
- `tests/data_model/test_classvar.py`: shared mutable counter across
  instances; subclass shadowing; `Cls.count = ...` mutates the slot
  visible to all instances.
*Out of scope:* metaclass-driven class-attribute hooks (T1).

---

### B4 — user-class `__iter__` / `__next__` / `__hash__` / `__str__`

*Dep:* B3.
*Scope:* dispatch user-defined dunders for iteration (`for x in c`),
hashing (`hash(c)`, `c in set/dict`), and stringification (`str(c)`,
`f"{c}"`).
*Status 2026-05-08:* user-class `__iter__` / `__next__` for `for` loops is
green in `tests/test_python_class_features_parity.py::test_class_user_iter`.
`__hash__` / `__str__` protocol edge coverage remains part of the broader B4
roadmap acceptance.
*Files:*
- `pcc/runtime/src/py_obj_ops_dispatch.c`
- `pcc/frontends/python/codegen/layer1.py` (iter-protocol lowering for user types)
*Acceptance:*
- `tests/data_model/test_user_dunders.py`: user iterator class with
  `__iter__` returning self and `__next__` raising `StopIteration`;
  user `__hash__` makes the class usable as `dict` key; user `__str__`
  is honoured by `print()` and f-string.
*Out of scope:* `__format__` mini-language (D6).

---

### B5 — `raise X from Y` / `raise Y inside except X` / traceback frames

*Dep:* T0.
*Scope:* `__cause__` chain via `raise X from Y`, implicit `__context__`
chain when raising inside an `except` block, and codegen that calls the
existing runtime helper `py_exc_append_frame` so tracebacks have frames.
*Files:*
- `pcc/frontends/python/codegen/layer1.py` (lower `raise from` / record frame)
- `pcc/runtime/src/py_exc_objects.c`
- `pcc/runtime/src/py_exc_traceback.c` (mostly there; codegen wiring)
*Acceptance:*
- `tests/data_model/test_exception_chaining.py` covers both `__cause__` and
  `__context__` shapes.
- `tests/data_model/test_traceback_frames.py` confirms emitted tracebacks
  show source line numbers for at least 3 frames deep.
*Out of scope:* `sys.exc_info()` global; `traceback.format_exc` parity (lives
  in D8).

---

### B6 — module-attribute writes; `*args` / `**kwargs` splat at module call

*Dep:* T0.
*Scope:* `module.attr = val` writes the module slot natively; `module.fn(*a)`
and `module.fn(**kw)` lower to the existing tuple/dict-vararg call path that
already handles 4+ positional args.
*Status 2026-05-08:* native builtin module attribute writes are implemented and
covered by `tests/test_python_module_imports_parity.py::test_module_attribute_write`.
Call splat remains pending.
*Files:*
- `pcc/frontends/python/codegen/layer1.py`
- `pcc/runtime/src/py_module.c` (or wherever module slots live; check)
*Acceptance:*
- `tests/data_model/test_module_assignment.py`: `mod.x = 1; assert mod.x == 1`.
- `tests/data_model/test_call_splat.py`: `f(*args)`, `f(**kw)`, `f(*a, **k)`.
*Out of scope:* import hooks (D8).

---

### D2 — generator state machine

Inherits scope and acceptance from `python-data-model-gaps.md` §"Phase D2".

*Dep:* B4.
*Out of scope flag for Codex:* do not implement async (`async def`) inside this
phase even if the state machine could absorb it; keep coroutines for D3 to
prevent overscope.

---

### D3 — async / await + asyncio minimum

Inherits scope from `python-data-model-gaps.md` §"Phase D3".

*Dep:* D2.

---

### D4 — context-manager full semantics

Inherits scope from `python-data-model-gaps.md` §"Phase D4".

*Dep:* D3 (so `async with` is unblocked alongside).

---

### D5 — iteration / number / comparison protocol polish

Inherits scope from `python-data-model-gaps.md` §"Phase D5".

*Dep:* B4.

---

### D6 — `__format__` and format-spec passthrough

Inherits scope from `python-data-model-gaps.md` §"Phase D6".

*Dep:* B4.
*Status 2026-05-08:* the Python parity f-string numeric specs are green:
`.Nf`, `,`, and `0Nd` lower to native helpers.  Full user `__format__` and
`format()` builtin dispatch remain in the broader D6 scope.

---

### D8 — dynamic import + introspection

Inherits scope from `python-data-model-gaps.md` §"Phase D8".

*Dep:* B6.

---

### D7 — pickle / copy support

Inherits scope from `python-data-model-gaps.md` §"Phase D7".

*Dep:* D8.

---

### T1 — metaclasses (`class C(metaclass=M)`)

*Dep:* D1, B3.
*Scope:* `class C(metaclass=M)` calls `M.__call__(name, bases, ns)` to build
the class object; default metaclass for non-`type` bases follows CPython's
"derive metaclass from bases" rule. `type(name, bases, dict)` 3-arg form
is the same code path. ABCMeta and EnumMeta (the only metaclasses pcc's
own corpus uses) work end-to-end.
*Files:*
- `pcc/frontends/python/codegen/class_gen.py` (metaclass argument lowering)
- `pcc/runtime/src/py_class.c` (metaclass dispatch on class creation)
*Acceptance:*
- `tests/data_model/test_metaclass.py`: explicit `class C(metaclass=Trace):`;
  `Enum` subclass; `ABCMeta`-based abstract method.
- Existing `tests/test_typed_class_*.py` stay green.
*Out of scope:* `__init_subclass__` (separate task; PEP 487 piece).

---

### T2 — `typing` runtime: `TypeVar`, `Generic`, `Protocol`

*Dep:* T1.
*Scope:* `typing.TypeVar` and `Generic[T]` evaluate at runtime (currently
ignored / falls back to libpython). `Protocol` runtime check via
`@runtime_checkable` works for `isinstance()`. Static typing-checker behaviour
is unchanged — pcc already strips most `typing` annotations to types in
`type_infer.py`; this is the runtime-side completion.
*Files:*
- `pcc/stdlib/typing.py` (currently absent or thin — verify)
- `pcc/runtime/src/py_class.c` (Protocol structural-isinstance)
*Acceptance:*
- `tests/data_model/test_typing_runtime.py`: `class L(list, Generic[T])` works;
  `@runtime_checkable Protocol` passes structural `isinstance`.
*Out of scope:* `typing.get_type_hints` reflection (lives in D8).

---

### T3 — mutable-dataclass setattr fix

*Dep:* D1.
*Scope:* the known pcc1-runtime bug "writing to a default-`None` dataclass
slot via `obj.field = ...` clobbers a neighbouring slot" (memory:
`feedback_pcc_dataclass_default_none_setattr.md`). Investigate root cause
in `py_instance_set_field` slot indexing and fix; add regression test.
*Files:*
- `pcc/runtime/src/py_class.c` (`py_instance_set_field` and slot-index
  derivation)
- `pcc/stdlib/dataclasses.py`
*Acceptance:*
- `tests/data_model/test_mutable_dataclass.py`: a dataclass with two
  default-`None` slots; assigning one does not corrupt the other; round-trip
  read/write of all slots.
*Out of scope:* full dataclass `__post_init__` parity, comparison generation.

---

### T4 — `weakref.ref`

*Dep:* T1, GC-4 (from `gc-semantics-gap.md`).
*Scope:* `weakref.ref(obj)` returns a callable that yields `obj` until `obj`
is collected, then yields `None`. Hooks into the runtime's per-object weakref
slot list cleared during dealloc.
*Files:*
- `pcc/runtime/src/py_weakref.c` (new)
- `pcc/runtime/src/py_obj.c` (extend dealloc to walk weakref list)
- `pcc/stdlib/weakref.py` (thin shim)
*Acceptance:*
- `tests/data_model/test_weakref_basic.py`: bug example #4 from
  `gc-semantics-gap.md` returns `None` after `del obj`.
*Out of scope:* `WeakValueDictionary` / `WeakSet` (separate tasks).

---

### T5 — native C-extension ABI loader (research only)

*Dep:* T1, GC-4, GC-5.
*Scope:* design only — write a 1-page memo (`docs/research/c-extension-abi.md`)
describing what it would take to load a CPython C extension `.so` without
linking libpython. **Do not implement.** This is here so Codex doesn't
accidentally start work on it before the prerequisites are in place; the
multi-year roadmap §5.1 lists it as a long-term goal, not a near-term task.
*Acceptance:* the memo lands; no code change.

---

## Next ready task

As of 2026-05-08 the next ready task for Codex is **T0** — concurrent-mutation
lost-update under user `Lock`. All other tasks are gated on T0 or on already-
identified prerequisite phases.

The investigation already narrowed the bug to the
`user_threading_Lock_acquire` → `py_instance_get_field` →
`py_threading_lock_acquire` chain. C-level direct calls work; Python-emitted
calls do not. Reproducer steps for Codex to verify before changing anything:

```bash
# Build runtime threaded
cd pcc/runtime && make -B PCC_WITH_THREADS=1 libpy_runtime.a && cd -

# Compile the reproducer
PCC_RUNTIME_CC=cc PCC_RUNTIME_HIGH=c PCC_WITH_THREADS=1 \
  uv run python -c "from pcc.frontends.python.pipeline import compile_python; compile_python('/tmp/incr_test.py', '/tmp/incr_test.out', ir_scaffold_mode='on', libpython_mode='off')"

# Reproduce the bug — should be 4000, observed 1300–2000
for i in 1 2 3; do /tmp/incr_test.out; done
```

The reproducer file is the same `/tmp/incr_test.py` used in the
investigation; if missing, recreate it from the snippet at the top of T0
above (4 threads, `counts[0] = counts[0] + 1` 1000 iters each under one
`Lock`).

---

## Notes for Codex

- **Run targeted tests, not the full suite.** Per repo convention
  (`feedback_no_full_tests.md`), use the per-phase test path under
  `tests/data_model/` for the active task. Full suite goes through CI.
- **Use `pytest -n0`** when debugging multi-thread tests so xdist's worker
  parallelism does not interleave with the bug under investigation.
- **No scope creep.** Each task block lists "Out of scope". Refuse to expand.
  If new work appears, file a follow-up entry under the relevant section
  rather than absorbing it into the active task.
- **Touch one phase at a time.** The dependency graph at the top is a hard
  DAG. Do not start D3 with D2 incomplete, etc.
- **Update `docs/python-scorecard.md` corpus number** at the end of each
  phase that lands. The 2026-04-20 baseline is 106/137; each phase has a
  prediction ("D2 should add ~10 tests by enabling generator-based corpus
  cases") and the post-phase actual number is the proof of progress.
- **GC dependency.** T4 weakref and the cycle-leak class of bugs are gated
  on GC-4 from `gc-semantics-gap.md`. Do not stub them; raise
  `NotImplementedError` in the runtime so the gap is visible in tests.
