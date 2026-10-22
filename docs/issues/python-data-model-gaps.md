# Python data-model gaps — descriptor / generator / context-manager / etc.

**Status:** mostly resolved for D2-D8 as of 2026-05-17. Filed 2026-04-29
alongside `docs/issues/python-semantics-preservation.md`.

Current closure gate:

```bash
bash scripts/run_goal_closure_bundle_gate.sh
```

Latest observed focused result:

- B1-B6 closure: `21 passed`
- D2-D6 closure: `47 passed`
- final-language closure: `32 passed`

D1 descriptor follow-up remains a compatibility-expansion track, but the
generator/async/context/protocol/format/pickle/import/introspection phases no
longer describe the current implementation as absent.

## The problem

`gc-semantics-gap.md` covers the memory-management half of Python's
data model. The other half — descriptor protocol, generators /
coroutines, context managers, full iteration / number / comparison
protocols, formatting, pickle/copy, import-system internals,
introspection — has overlap, partial coverage, and known gaps that
no current plan tracks.

This is the second half of the data-model contract pcc claims to
preserve. Without an explicit plan, gaps surface one at a time
during self-host debugging and get patched ad-hoc, mirroring
exactly the situation that motivated `gc-semantics-gap.md`.

## What's actually there

A quick map of where pcc currently stands on each data-model area:

| Area | Status | Where |
|---|---|---|
| Attribute lookup MRO | focused D1 contract passes: data vs non-data descriptor priority is locked; broader introspection edges still open | `pcc/runtime/src/py_class.c`, `py_obj_ops_dispatch.c`, `tests/test_descriptor_protocol.py` |
| `@property` | getter/setter/read-only focused contract passes; docstring/introspection edges incomplete | runtime + codegen typed-class field access |
| `staticmethod` / `classmethod` | focused dispatch contract passes; `__func__` introspection / subclass-call edge cases incomplete | `class_gen.py` method-kind classification |
| `__slots__` | focused storage/no-`__dict__` contract passes; inheritance and weakref slots still need coverage | `tests/test_descriptor_protocol.py` |
| `__getattribute__` / `__getattr__` | partial (`__getattr__` fallback works, `__getattribute__` overload not honoured) | runtime |
| Generator (`yield`) | focused D2 contract passes: local state, `yield from`, `send`, `throw`, `close`, and return-value `StopIteration` | `generator_lowering.py`, `py_gen.c`, `tests/python/test_generator_protocol.py` |
| `yield from` | focused delegation contract passes | `generator_lowering.py`, `tests/python/test_generator_protocol.py` |
| `async def` / `await` | focused D3 contract passes for coroutine objects, await round trips, `asyncio.run`, `sleep(0)`, user awaitables | `native_asyncio.py`, `py_coroutine.c`, `tests/python/test_async_await.py` |
| Context managers (`with`) | focused D4 contract passes, including multi-manager, suppression, `contextmanager`, and `async with` | `async_with_lowering.py`, `tests/python/test_context_manager_full.py` |
| `async with` | focused contract passes | `async_with_lowering.py`, `tests/python/test_async_await.py`, `tests/python/test_context_manager_full.py` |
| `__format__` / format-spec mini-language | focused D6 contract passes for builtin specs, user `__format__`, and default rejection | `format_lowering.py`, `py_format.c`, `tests/python/test_format_protocol.py` |
| `__reduce__` / pickle support | focused D7 contract passes for native pickle/copy round trips | `native_modules.py`, `py_pickle_copy.c`, `tests/python/test_pickle_copy.py` |
| `__copy__` / `__deepcopy__` | focused D7 contract passes | `native_modules.py`, `py_pickle_copy.c`, `tests/python/test_pickle_copy.py` |
| `importlib.import_module` dynamic | focused D8 contract passes for known native modules and module exports without libpython | `native_modules.py`, `pipeline.py`, `tests/python/test_dynamic_import.py` |
| `inspect.signature` / introspection | focused D8 contract passes for local user functions, getsource, getmro, isfunction/isclass/ismethod | `native_modules.py`, `tests/python/test_inspect_protocol.py` |
| Reflected operators (`__radd__` etc.) | focused D5 protocol edge contract passes | `py_obj_ops_dispatch.c`, `compare_membership_lowering.py`, `tests/python/test_protocol_edges.py` |
| Three-arg `pow(a, b, m)` | focused D5 protocol edge contract passes | `tests/python/test_protocol_edges.py` |
| `iter(callable, sentinel)` 2-arg form | focused D5 protocol edge contract passes | `tests/python/test_protocol_edges.py` |

The takeaway: the "happy path" for each protocol is wired; the
edges aren't. That's a typical state for an in-flight runtime, but
each edge is a self-host trap.

## Concrete user-visible bugs

Examples of code that works under CPython and silently misbehaves
or fails under pcc today:

```python
# 1. data descriptor priority over instance dict
class Field:
    def __get__(self, obj, owner): return "from descriptor"
    def __set__(self, obj, val):   obj.__dict__["_x"] = val

class C:
    x = Field()

c = C()
c.__dict__["x"] = "from instance"
print(c.x)
# CPython: "from descriptor" — data descriptor wins
# pcc:     "from instance"   (current attribute-lookup order is wrong
#                              for data descriptors)
```

```python
# 2. generator state machine
def fib():
    a, b = 0, 1
    while True:
        yield a
        a, b = b, a + b

g = fib()
print(next(g), next(g), next(g))
# CPython: 0 1 1
# pcc:     compile-time error or wrong runtime behaviour
```

```python
# 3. __format__ spec passthrough
class Money:
    def __init__(self, n): self.n = n
    def __format__(self, spec):
        return f"${{:{spec}}}".format(self.n)

print(f"{Money(1234.5):,.2f}")
# CPython: "$1,234.50"
# pcc:     calls __str__, ignores the spec
```

```python
# 4. context manager exception chaining
class CM:
    def __enter__(self): return self
    def __exit__(self, et, ev, tb):
        raise RuntimeError("from __exit__") from ev

try:
    with CM():
        raise ValueError("from body")
except RuntimeError as e:
    print(e.__cause__)
# CPython: ValueError('from body')
# pcc:     ?  (chaining via __cause__ may be lost)
```

```python
# 5. reflected operator fallback
class Half:
    def __radd__(self, other): return other + 0.5

print(1 + Half())
# CPython: 1.5  (int.__add__ returns NotImplemented, falls back to Half.__radd__)
# pcc:     TypeError or wrong dispatch — reflected fallback chain incomplete
```

These are not "rare edge cases". They are the load-bearing
mechanics that make Python's data model feel like Python.

## Comparison with other Python runtimes

| impl | descriptor | generator | async | format spec | pickle | full reflected ops |
|---|---|---|---|---|---|---|
| CPython | yes | yes | yes | yes | yes | yes |
| PyPy | yes | yes | yes | yes | yes | yes |
| GraalPy | yes | yes | yes | yes | yes | yes |
| MicroPython | partial | yes | partial | partial | no | partial |
| Cython (typed `.pyx`) | partial (subset) | yes | yes | partial | n/a | yes |
| Nuitka | yes (delegates to CPython) | yes | yes | yes | yes | yes |
| **pcc** | **partial** | **mostly absent** | **no** | **partial** | **no** | **partial** |

Among AOT Python implementations pcc is closest to MicroPython on
data-model coverage today; its goal is to land where CPython /
PyPy / GraalPy sit.

## Why this isn't blocking yet

The bootstrap closure (pcc compiling itself) avoids most of these:

- pcc's own code uses `@dataclass(frozen=True)` for AST nodes, not
  custom descriptors. The `@dataclass` machinery is implemented
  natively (or imported as a recursive stdlib closure under
  Issue 11.B).
- pcc's source uses `with open(...) as f:` for file I/O — the
  simple context manager shape, no exception chaining
  intricacies.
- pcc has no generator-based code in the bootstrap path; iteration
  is plain `for x in container`.
- pcc doesn't pickle anything.
- pcc doesn't use `inspect.signature` at runtime.

Real-world Python programs hit these immediately. **The gap is
between "pcc can self-host" and "pcc is a Python implementation."**

## Plan

Eight phases, ordered by **how many other phases depend on them**.
Descriptor protocol comes first because attribute lookup is the
root of the data model — every method call, every `@property`,
every bound-method creation goes through it.

Each phase is independently shippable; later phases assume earlier
ones are correct.

### Phase D1 — descriptor protocol (1.5–2 weeks)

**Implementation note 2026-05-02:** the focused
`tests/test_descriptor_protocol.py` gate now passes, including
property getter/setter/read-only, classmethod/staticmethod dispatch,
data vs non-data descriptor lookup order, `__set_name__`, and basic
`__slots__`. D1 remains open for CPython-level introspection and
inheritance edge cases, but the core contract is no longer xfail.

Most central single phase. Fixing this alone resolves Codex Failure
Class 4 (`Module.globals as a property was too dynamic`) and
unlocks a class of self-host bugs that look like attribute access
returning wrong values.

Scope:
- Implement `__get__` / `__set__` / `__delete__` slots on type
  objects with the canonical lookup order:
  `instance.__dict__["x"]` only wins over a class-side **non-data**
  descriptor; **data** descriptors (those with `__set__` or
  `__delete__`) override instance dict.
- `@property` reads/writes/deletes go through the same slots.
- `staticmethod` / `classmethod` produce the bound `__func__`
  attribute and dispatch correctly through subclasses.
- `__set_name__` (PEP 487) called when a descriptor is bound to a
  class.
- `__slots__` minimal support — at least reject `__dict__` access
  on slotted instances and route attribute access to the slot
  array.
- `__getattribute__` overload runs *before* the default lookup;
  `__getattr__` fallback runs *after* a default failure (in that
  exact order).

Acceptance:
- Bug example #1 above (data descriptor priority) returns
  `"from descriptor"` under pcc.
- A new `tests/data_model/test_descriptor_protocol.py` covers
  data vs non-data, `__set_name__` order, `staticmethod` /
  `classmethod` invocation through subclasses, `__getattribute__`
  overload, `__slots__`-only access.
- Existing self-host tests stay green; specifically the
  `tests/test_typed_class_*.py` suite must continue to pass.

Risk: attribute lookup is on the hot path of every Python program.
Wrong ordering doesn't crash — it just returns wrong values. Need
the per-function self-host oracle (proposed in
`self-host-oracle-test-layer.md`) to catch silent regressions.

### Phase D2 — generator state machine (2–3 weeks)

Generators are the second-most fundamental missing primitive.
`yield` is used in idiomatic Python pervasively (`enumerate`-style
helpers, lazy iteration, `contextlib.contextmanager`).

Scope:
- Lower `def f(): ... yield x ...` to a state-machine class with
  `__iter__` / `__next__` / `send` / `throw` / `close` slots.
- Preserve local variable state across yields.
- `yield from` (PEP 380) for delegation including return-value
  propagation via `StopIteration.value`.
- Generator function bodies that mix `yield` and `return value`
  raise `StopIteration(value)` correctly.

Acceptance:
- Bug example #2 (Fibonacci generator) prints `0 1 1`.
- New `tests/data_model/test_generator_*.py` covering: simple
  yield, send / throw / close, yield from, return-value
  propagation, generator-based context managers.
- `contextlib.contextmanager` decorator works (this is the
  load-bearing user of the generator protocol).

Risk: generator codegen is a major frontend addition. The state
machine compilation is well-documented (CPython's bytecode
decomposes it cleanly), but pcc's typed AOT path needs its own
encoding. Likely to surface holes in the type-inference layer for
yield-typed expressions.

### Phase D3 — async / await + asyncio minimum (3–4 weeks)

Builds on D2's state machine. Coroutines are essentially
generators with a different protocol (await instead of next).

Scope:
- `async def` produces a coroutine object with `__await__`
  iterating an internal state machine.
- `await expr` lowers to the standard awaitable protocol
  (`__await__` returns an iterator; the returned iterator's
  `send` / `throw` are driven by the event loop).
- `async for` / `async with` lower to `__aiter__` / `__anext__` /
  `__aenter__` / `__aexit__`.
- Minimum asyncio surface: `asyncio.run`, `asyncio.sleep`,
  `asyncio.gather`, the default event loop. Either as a native
  pcc-Python port or as a recursive-stdlib closure compile.

Acceptance:
- `asyncio.run(asyncio.sleep(0.01))` exits cleanly under pcc.
- `tests/data_model/test_async_*.py` covers: `async def` basic
  return value, `await` propagation, `async for` over
  `__aiter__`-defined iterables, exception propagation through
  awaitables, `asyncio.gather` of trivial coroutines.

Risk: asyncio's event loop is a big surface. The minimum may end
up being just enough that `asyncio.run` works but advanced
features (Streams, subprocess, queues) fall through to CPython.
That's acceptable — explicitly document the sub-surface coverage.

### Phase D4 — context manager full semantics (1 week)

Most of `with` works today. The phase tightens edges around
exception chaining and the async variant.

Scope:
- `__exit__` returning truthy suppresses the exception, falsy
  re-raises. Already mostly correct; lock with tests.
- Exception chaining: an exception raised in `__exit__` chains
  the original via `__context__`; explicit `raise X from Y`
  inside `__exit__` sets `__cause__`.
- Nested `with` clauses (`with a, b, c:`) handle exceptions in
  the right order (later managers exit first; their `__exit__`
  exceptions chain through earlier managers).
- `async with` is unblocked by D3.

Acceptance:
- Bug example #4 (chained exception) keeps `__cause__`.
- `tests/data_model/test_context_manager_*.py` covers the four
  shapes above.
- Existing `with open(...)` self-host paths stay green.

Risk: low. Most of this is small additions to existing exception
handling.

### Phase D5 — full iteration / number / comparison protocols (1 week)

Polish on the protocols that are mostly there.

Scope:
- `iter(callable, sentinel)` 2-arg form.
- Three-arg `pow(a, b, m)` going through `__pow__(self, other,
  modulo=...)`.
- `__divmod__` / `__rdivmod__`.
- `__floor__` / `__ceil__` / `__trunc__` / `__round__` (so
  `math.floor(obj)` etc. work on user classes).
- Reflected operator fallback chain: `a + b` tries `a.__add__(b)`,
  on `NotImplemented` tries `b.__radd__(a)`. Currently partial.
  Lock the full ordering rule (subclass-aware: subclass `__radd__`
  has priority over base `__add__`).
- Rich comparison: total-ordering inference (a class with
  `__lt__` and `__eq__` automatically gets `__le__` /
  `__gt__` / `__ge__` reflections).

Acceptance:
- Bug example #5 (`1 + Half()`) returns `1.5`.
- `tests/data_model/test_protocol_edges.py` covers all four
  protocols.

Risk: low — most of this is filling in dispatch tables.

### Phase D6 — `__format__` and format-spec passthrough (3–4 days)

Scope:
- `str.format` / f-strings call `__format__(self, spec)` on the
  argument; the user-defined class's `__format__` returns the
  formatted string.
- `format()` builtin works on arbitrary objects.
- Default `__format__(self, "")` returns `str(self)`; non-empty
  spec on a class without `__format__` raises `TypeError` (matches
  CPython 3.4+).

Status 2026-05-08:
- Builtin numeric f-string specs covered by
  `tests/test_python_str_methods_parity.py::test_str_fstring_format_spec`
  now pass natively: `.Nf`, `,`, and `0Nd`.
- User-defined `__format__`, `format()` builtin dispatch, and default
  non-empty-spec rejection remain pending.

Acceptance:
- Bug example #3 (`Money` formatter) prints `"$1,234.50"`.
- `tests/data_model/test_format.py` covers user `__format__`,
  default behaviour, spec rejection on classes that don't override.

Risk: low — codegen change at the f-string lowering site, runtime
helper to invoke `__format__`.

### Phase D7 — pickle / copy support (1.5 weeks)

Scope:
- `__reduce__` / `__reduce_ex__` / `__getstate__` / `__setstate__`
  protocol on user classes.
- Native dispatch for `pickle.dumps` / `pickle.loads` of basic
  types (int / str / list / dict / tuple / set / user class via
  `__reduce__`). Or recursive-stdlib closure compile of `pickle.py`.
- `copy.copy` / `copy.deepcopy` likewise.

Acceptance:
- `pickle.loads(pickle.dumps(obj))` round-trips for the common
  cases.
- `copy.deepcopy(nested_dict_of_user_classes)` produces a
  fresh deeply-cloned graph.
- `tests/data_model/test_pickle_copy.py` covers the round trips.

Risk: pickle protocol is large. The minimum is "round-trip basic
types"; full opcode coverage (memos, persistent IDs, etc.) is a
separate follow-up. Document explicitly which protocol versions
are supported.

### Phase D8 — dynamic import + introspection (2 weeks)

Scope:
- `importlib.import_module(name)` uses pcc's existing closure
  walker for known-static names; falls back gracefully for
  truly dynamic names.
- `__import__` hook lookup chain (`sys.meta_path`, finders /
  loaders) at least returns sensible defaults.
- `inspect.signature(callable)` returns a real Signature for
  user-defined functions and bound methods.
- `inspect.getmembers` / `dir(obj)` return the union of
  `__dict__` keys and class-side names.
- `__qualname__` / `__module__` / `__doc__` populated correctly
  on functions and classes.

Acceptance:
- A Python program using `inspect.signature` to introspect a
  user-defined function returns parameter names matching the
  source declaration.
- `tests/data_model/test_introspection.py` covers the above.

Risk: import system internals are subtle. Aim for "covers the
common cases", document divergences, leave full PEP 451 support
as a follow-up.

## Sequencing

Recommended order by dependency:

1. **D1** (descriptor) — root of attribute access; everything else
   depends on attribute lookup being correct.
2. **D2** (generator) — large, but unlocks `contextlib.contextmanager`
   and most lazy-iteration idioms in the stdlib.
3. **D3** (async) — depends on D2's state machine.
4. **D4** (context manager) — independent of D2/D3 for the sync
   case; the async case is gated on D3.
5. **D5** (protocol edges) — independent, can land any time after
   D1.
6. **D6** (format) — independent, can land any time after D1.
7. **D7** (pickle / copy) — depends on D1 (descriptor protocol
   correctness affects `__reduce__` lookup).
8. **D8** (import / introspection) — depends on D1 (introspection
   walks the descriptor tree).

D5 and D6 are good "stretch goal" candidates that can be picked
up between major phases as cooldown work.

## Sequencing relative to other plans

| Plan | Relationship |
|---|---|
| `gc-semantics-gap.md` | Independent — memory semantics axis |
| `self-host-ergonomics.md` | D8 (import) overlaps with the recursive-stdlib closure work; coordinate |
| `python-self-host-no-libpython-runtime-holes.md` | D1 (descriptor) directly addresses Failure Class 4 (`Module.globals as a property`) |
| `self-host-oracle-test-layer.md` | Required prerequisite — without per-function differential the descriptor-protocol regressions are silent |
| `open-bootstrap-issues.md` (Issue 1) | Independent for closure; but D1 raises confidence that pcc-emitted `@property` definitions in user code behave correctly |

## Testing infrastructure

A new test directory `tests/data_model/` with one file per phase:

- `test_descriptor_protocol.py` (D1)
- `test_generator_state.py` / `test_generator_yield_from.py` (D2)
- `test_async_basic.py` / `test_asyncio_minimum.py` (D3)
- `test_context_manager_chaining.py` / `test_async_with.py` (D4)
- `test_protocol_edges.py` (D5)
- `test_format.py` (D6)
- `test_pickle_copy.py` (D7)
- `test_introspection.py` (D8)

Two harnesses run each test:

- **CPython** runs the test directly to assert the test itself is
  correct (asserts CPython produces expected output).
- **Self-host oracle** (per `self-host-oracle-test-layer.md`)
  compiles each test under pcc1 and compares output to CPython.

Tests targeting unimplemented features start as `xfail(strict=True)`.
Flipping `xfail → green` is the metric for each phase landing.

## What stays a non-goal

- **Bytecode-level introspection.** `dis.dis(func)` won't return
  CPython's bytecode — pcc isn't producing bytecode. `inspect`
  paths that read `__code__.co_code` are out of scope.
- **`exec` / `eval` of arbitrary source at runtime.** This needs
  the parser + frontend at runtime, which crosses the Issue 1
  libpython boundary. Tracked separately if it ever becomes a
  goal.
- **C extension API compat (`Py_BuildValue` etc.).** pcc's
  runtime is a Python-level reimplementation, not a CPython C-API
  drop-in. Third-party C extensions would need a wrapper layer;
  out of scope for this plan.
- **`metaclass=` with arbitrary metaclass classes.** Basic
  `type(name, bases, dict)` works; custom metaclasses doing
  `__new__` / `__call__` overrides are a follow-up.

## Open questions

1. **Should D2 generators reuse the self backend's state-machine
   work?** The self backend already has to track liveness across
   labels for codegen; generator state machines need similar
   liveness analysis. There may be a reusable component.

2. **Is asyncio in-scope for pcc, or do we delegate?** D3 includes
   "asyncio minimum", but a full asyncio implementation is its own
   project. Decide explicitly whether pcc ships its own asyncio
   port or treats it as a "use libpython for now" module.

3. **`__slots__` vs `__dict__` storage layout.** Fundamental object
   layout decision. CPython makes `__slots__` an alternative
   storage; pcc could keep `__dict__` as the only storage and
   treat `__slots__` as an attribute-access optimisation hint.
   Decide before D1 lands so the runtime layout is set.

4. **Pickle protocol version target.** Protocol 5 is current;
   protocol 2 is the minimum for cross-version compatibility.
   Decide target before D7 to scope the opcode set.

5. **Per-feature compatibility table for users.** As
   `python-semantics-preservation.md` open question #2 asks,
   eventually pcc needs a per-feature MicroPython-style
   "differences from CPython" table. The data-model phases are
   the right backbone for that table.

## Bottom line

pcc has the GC half of the data model planned (`gc-semantics-gap.md`).
This document plans the rest. Eight phases, descriptor first
because it's the root of attribute lookup; generators / async
next because their absence is the biggest user-facing gap; the
remaining five phases are independent fit-and-finish.

**Total core estimate: 13–17 weeks of focused work for D1-D8**,
not counting documentation and per-feature compatibility tables.
This is a multi-month investment that brings pcc from "can
self-host" to "is a Python implementation" on the data-model
axis.

Cross-references:
- `gc-semantics-gap.md` — memory-semantics axis
- `self-host-ergonomics.md` — developer-experience axis
- `self-host-oracle-test-layer.md` — testing layer required to
  catch regressions across all eight phases
- `python-semantics-preservation.md` — the four-gate framework
  this plan plugs into
- `python-self-host-no-libpython-runtime-holes.md` — Codex's
  empirical bug list, of which Failure Class 4 is exactly D1
