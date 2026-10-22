# Python semantics preservation in pcc — without bytecode

**Status:** living document. Snapshot 2026-04-29.

## The question

pcc compiles `.py` source to **native machine code** (Mach-O / ELF
object → linker → executable). It does not produce CPython
bytecode. A reasonable first reaction is: if pcc skips the bytecode
representation, how does it preserve Python semantics?

This document lays out where the semantics actually come from, the
four independent checks that lock them in, and the gaps that are
explicitly known.

## Bytecode is an implementation detail, not a semantic definition

Python's language reference defines the *behaviour* of `a + b`,
`for x in xs:`, `try / except`, attribute access, descriptor
protocols, etc. CPython implements those by parsing source to AST,
compiling AST to bytecode, and executing the bytecode in a
fetch-decode-execute loop in `ceval.c`. That bytecode + interpreter
is one of many possible implementation paths; PyPy compiles
RPython to JITted machine code, GraalPy compiles to JVM bytecode,
Jython compiles to JVM bytecode (different bytecode), MicroPython
compiles to a different bytecode again.

What every Python implementation must preserve is the **observable
behaviour** described by the language reference and validated by
the Python test suite, not the specific bytecode CPython uses.
"Skipping bytecode" is a path choice. It does not by itself
introduce semantic divergence.

## Two semantic layers, two implementation surfaces

CPython splits Python semantics across two layers, each implemented
in a separate part of the codebase:

| Layer | Defined by | CPython's implementation | pcc's implementation |
|---|---|---|---|
| **Language semantics** — control flow, expression evaluation order, scope rules, exception propagation, `for`/`while`/`try`/`finally`, comprehensions, generator/coroutine state machines | Python language reference | bytecode + `ceval.c` interpreter loop | codegen lowers AST directly to native code that performs the same observable steps |
| **Data model semantics** — `PyObject*` identity, `__add__` / `__getattr__` MRO dispatch, container `__contains__` / `__iter__`, refcount, descriptor protocol, exceptions as objects | Python language reference + data model docs | C runtime in `Objects/*.c` (`PyDict_*`, `PyList_*`, `PyNumber_*`, ...) | pcc's own runtime: `pcc/runtime/src/*.c` and the pcc-Python ports in `pcc/runtime/py/*.py` (`py_int`, `py_str`, `py_list`, `py_dict`, `py_obj_ops_*`) |

pcc is therefore **two from-scratch implementations** of the same
two surfaces CPython covers, plus a codegen that wires them
together. Both surfaces are written from documented behaviour, not
ported from the CPython source.

## How a single expression flows through

Worked example — `a + b` where both are user-typed `DynType`:

**CPython** (3.13):

```
LOAD_FAST   'a'         # push a (PyObject*) onto the value stack
LOAD_FAST   'b'         # push b
BINARY_OP   'add'       # pop b, pop a, call PyNumber_Add(a, b),
                        # push result
```

`ceval.c` decodes `BINARY_OP` and calls the runtime function
`PyNumber_Add`, which walks the type's `tp_as_number->nb_add` slot
through the MRO to find `__add__` / `__radd__`.

**pcc** (typed Python frontend, ON-mode codegen):

```llvm
%a = load ptr, ptr %a.addr
%b = load ptr, ptr %b.addr
%result = call ptr @py_obj_add(ptr %a, ptr %b)
store ptr %result, ptr %tmp.addr
```

The native code calls pcc's runtime function `py_obj_add`, which
internally walks the same MRO `__add__` / `__radd__` lookup and
returns the result. **The bytecode dispatch step is gone, but every
semantic point that bytecode encoded — operand order, `__add__`
lookup, refcount discipline, exception-on-`TypeError`, return
value — is preserved by the codegen-emitted call into the runtime.**

The same pattern holds for every Python construct. `for x in xs:`
becomes calls to `py_iter` + `py_iter_next`. `obj.attr` becomes
`py_obj_getattr`. `try / except` becomes basic blocks with
return-code-style exception checks plus thread-local exception
state. Each lowering rule is documented in `pcc/frontends/python/codegen/`.

## Four independent checks that lock semantics in

Semantics are not "asserted by hand" anywhere. They are validated
by four orthogonal mechanisms, each catching different failure
modes.

### Check 1 — Runtime contract mirrors CPython by design

Every runtime function in `pcc/runtime/include/py_runtime.h` is
specified to behave the same way the corresponding CPython API
behaves. Examples:

- `PyObject` header layout: `refcount` + `type_tag` + `flags` —
  same shape as CPython's `_PyObject_HEAD`.
- `PyDictObject`: open-addressing probe table + insertion-ordered
  entries array, matching CPython 3.6+ compact dict.
- `PyListObject`: growable `PyObject*` array with `length` and
  `capacity`, matching CPython.
- Tagged int + bignum two-state: identical to CPython's tagged
  small int + heap `PyLongObject` distinction.
- Exception model: thread-local current exception read via
  `py_err_occurred()`, matching `PyErr_Occurred()` semantics.

Each entry in `RUNTIME_SIGNATURES` (`pcc/frontends/python/codegen/runtime_abi.py`)
maps 1:1 to a prototype in `py_runtime.h`, and each prototype
documents the contract. **The implementation is rewritten; the
contract is mirrored.**

### Check 2 — Cross-archive byte-equal oracle

The oracle harness (`tests/test_runtime_oracle_diff.py`) runs each
`tests/runtime_oracle/*_basics.py` corpus program through three
runtime archive variants:

- `libpy_runtime.a`        — cc-built C runtime (baseline)
- `libpy_runtime_pcc.a`    — pcc compiles the same C runtime
- `libpy_runtime_pcc_py.a` — pcc compiles the pcc-Python ports

For every program × variant combination the harness asserts byte-
identical stdout, stderr, and exit code. Any runtime behaviour
divergence fails the gate immediately.

This is **differential testing**: the harness doesn't need a prior
definition of "what's correct"; it only needs three independently
authored implementations to agree. Two of those implementations
(`pcc-C` and `pcc-py`) share no source with the cc-built reference
beyond the `.h` contract.

The current corpus covers int / str / list / dict / set / tuple /
class / exception / print / os / file basics. Adding
`gc_*_basics.py` is the planned hook for locking GC semantics as
they land (see `docs/issues/gc-semantics-gap.md`).

### Check 3 — Bootstrap fix-point byte-equality

pcc compiles itself in three nested invocations:

```
CPython runs pcc/__main__.py → produces pcc1
pcc1 compiles pcc/__main__.py → produces pcc2
pcc2 compiles pcc/__main__.py → produces pcc3
```

After Mach-O code-signature normalisation, **pcc2 is byte-identical
to pcc3**. This proves a strong invariant: pcc's codegen is a
fix-point — once a pcc binary compiles itself once, the second
self-compile produces an identical binary.

What this gives us:

- **Determinism.** Every Python construct that pcc itself uses
  produces deterministic native output. If the codegen of `for x
  in xs:` were wrong in a way that depended on observation order,
  pcc1 and pcc2 would diverge.
- **Self-consistency.** pcc compiling pcc under CPython produces
  the same observable behaviour as pcc compiling pcc under pcc.
  Because pcc is a multi-thousand-line typed Python program
  exercising most of the language, this single equality covers
  most of the language semantics pcc claims to support.

What it does *not* directly prove: that pcc agrees with CPython on
every Python program. The fix-point covers the program pcc itself
exercises; programs outside pcc's source need the corpus checks
(Check 2 + Check 4) to lock them.

### Check 4 — Python program corpus

`tests/runtime_oracle/` carries ~12 `*_basics.py` end-to-end
programs running through the oracle harness. The README also
references "177 end-to-end programs across 5 phases" — real Python
programs that pcc compiles to native binaries and whose output is
diffed against CPython. Both sources contribute test coverage
beyond pcc's own self-compile shape.

These programs are written by hand to exercise specific language
constructs (descriptors, `with` statements, comprehensions, exception
chaining, etc.) and provide concrete, debuggable failures when a
semantic regresses.

## Comparison with other Python implementations

| Implementation | Path | Semantic preservation mechanism |
|---|---|---|
| **CPython** | bytecode + interpreter | source of truth |
| **PyPy** | RPython VM → JIT-compiled native code | own runtime, validated against CPython test suite + tracing JIT correctness proofs |
| **GraalPy / Jython** | source → JVM bytecode | mostly reuses CPython sources for stdlib; semantic alignment via test suite |
| **Nuitka** | source → C → cc → exe; wraps CPython runtime | calls CPython API directly, so semantics are CPython's by construction |
| **mypyc** | typed source → C → cc → exe; wraps CPython runtime | typed optimisations; semantic fallback to CPython for non-fast-path code |
| **Cython** | `.pyx` (typed Python subset) → C → cc → exe | constrained subset matched against documented semantics |
| **MicroPython** | source → own bytecode → small interpreter | own runtime; targets embedded subsetcompliance documented case-by-case |
| **pcc** | source → LLVM IR or self-backend → native exe; own runtime | runtime contract mirrors CPython + cross-archive differential + bootstrap fix-point + program corpus |

The closest analogue is **PyPy**: separate from-scratch Python
runtime implementation, validated against the language test suite
rather than ported from CPython. pcc commits more to AOT (PyPy is
JIT) but uses the same shape of "rewrite + validate" approach to
semantic preservation.

The contrast with **Nuitka / mypyc** is meaningful: those projects
preserve semantics by *calling* the CPython runtime, so they
inherit CPython's behaviour for free but also inherit its libpython
dependency. pcc preserves semantics by *re-implementing* the runtime
under the same contract, so it can drop the libpython link entirely
once Issue 1 closes.

## Known semantic gaps (explicit)

pcc does not (yet) preserve every Python semantic. Calling these
out so the contract is honest:

1. **GC semantics.** Refcount works; cycle collection is a stub,
   `__del__` is not dispatched, weakrefs are not implemented,
   refcount is non-atomic (not thread-safe).
   - See [docs/issues/gc-semantics-gap.md](gc-semantics-gap.md) for
     the full plan and current contract.

2. **Data-model protocols beyond GC.** Descriptor protocol
   (`__get__` / `__set__` data-vs-non-data priority), generators
   (`yield`, `yield from`), async / await, full context-manager
   exception chaining, format-spec passthrough (`__format__`),
   pickle / copy support, dynamic import, and `inspect`-style
   introspection are all partial or absent. Some metaclass
   behaviour, `__init_subclass__`, runtime `__class_getitem__`,
   and PEP 544 protocol matching are also incomplete. Programs
   depending on these may fall through to the CPython bridge
   under `--python-libpython=auto` and behave correctly there;
   under `--python-libpython=off` they fail at compile time.
   - See [docs/issues/python-data-model-gaps.md](python-data-model-gaps.md)
     for the eight-phase plan covering descriptor protocol,
     generators, async, context managers, protocol edges,
     formatting, pickle/copy, and dynamic import / introspection.

3. **Threading.** Runtime is not thread-safe (refcount is non-
   atomic, no GIL-equivalent, container types unsynchronised).
   Multi-threaded Python programs are not currently a target.
   - See `docs/issues/gc-semantics-gap.md` Phase G4 for the
     prerequisite.

4. **`gc` module.** Stub — `gc.collect()` / `gc.get_referrers()`
   etc. don't work natively yet. Falls through to CPython under
   auto mode.

5. **Some standard library modules.** With `--python-libpython=off`
   pcc rejects modules whose source pcc cannot compile. Under auto
   mode they go through `py_cpy_import` and behave correctly but
   pull libpython.
   - See `docs/issues/self-host-ergonomics.md` for the recursive
     stdlib compile work.

6. **Floating-point edge cases.** pcc's `py_float` matches IEEE 754
   double semantics. CPython-specific float repr corner cases
   (e.g. `repr(0.1)` short-form algorithm differences) may differ
   in the last place.

These are gaps in coverage, not bugs in the parts that *are*
implemented. The parts that are implemented are locked by Checks 1-4
above.

## What "preserved Python semantics" means in pcc's claim

Concretely, pcc claims:

- The Python program corpus that pcc currently compiles produces
  the same stdout / stderr / return code as CPython would, modulo
  the gaps listed above.
- The cross-archive oracle (Check 2) actively rejects any drift
  between three independent runtime implementations.
- The bootstrap fix-point (Check 3) actively rejects any non-
  determinism in pcc's self-compile path.
- New runtime functions follow the same "mirror the CPython
  contract, validate via oracle" pattern. The pattern is mechanised
  enough that adding a new helper has a known shape (see
  `docs/issues/self-host-ergonomics.md` Insight 1 for the
  helper-tier framework discussion).

pcc does **not** claim:

- That pcc currently passes the full CPython language test suite.
  The corpus is much smaller than that today.
- That pcc agrees with CPython on every undocumented behaviour.
  CPython has thousands of behaviours that aren't in the language
  reference; pcc may diverge on those silently.
- That every fast-path lowering matches CPython's bytecode order
  of evaluation byte-for-byte. Side effects in unusual nesting may
  surface in different orders. (No known case in the corpus, but
  there is no formal proof.)

## How to reproduce the checks

```bash
# Check 2: cross-archive byte-equal oracle
pytest tests/test_runtime_oracle_diff.py -v

# Check 3: bootstrap fix-point byte-equality
uv run python scripts/bootstrap.py
# pcc2 ≡ pcc3 byte-for-byte (Mach-O must not be normalised into agreement)

# Check 4: Python program corpus end-to-end
pytest tests/ -k "runtime_oracle"
```

For Check 1 (runtime contract review) the entry point is
`pcc/runtime/include/py_runtime.h` for the prototype list and
`pcc/frontends/python/codegen/runtime_abi.py` for the codegen-side
mirror.

## Open questions

1. **Should pcc run the upstream Python language test suite?**
   The current corpus is small. Bringing up CPython's `test_*`
   modules under pcc would dramatically expand semantic coverage
   but would also surface long-tail divergences that take time to
   investigate. This is a Phase 6 task in the README roadmap.

2. **What's the right way to document semantic divergence?**
   Currently the gaps are listed in this doc and in
   `gc-semantics-gap.md`. As pcc grows, a per-feature compatibility
   table (similar to MicroPython's "differences from CPython"
   document) becomes useful.

3. **Should pcc fail compile-time on programs whose semantics it
   can't preserve?** Today `--python-libpython=off` rejects
   programs needing CPython fallback; `auto` accepts them and
   delegates to libpython. A future stricter mode could reject any
   program that crosses a known-divergent semantic boundary
   (`__del__`, weakref, metaclass tricks).

## Bottom line

pcc preserves Python semantics through **four independent gates**:

- **Contract mirror** (every runtime fn matches a documented
  CPython behaviour),
- **Cross-archive differential** (three implementations must agree
  byte-for-byte),
- **Bootstrap fix-point** (pcc compiling itself produces a stable
  binary),
- **Program corpus** (real Python programs, real CPython diff).

Bytecode is irrelevant to this. Skipping bytecode is faster (no
fetch-decode-execute overhead) and saves one indirection layer; it
does not weaken the semantic guarantee, because semantics live in
*the runtime contract and the codegen lowering rules*, not in
bytecode.

The gaps are explicit. Filling them is sequenced under the
`docs/issues/gc-semantics-gap.md` and
`docs/issues/self-host-ergonomics.md` plans, with the long-term goal
that pcc covers what CPython covers, validated by the same four
checks scaled up.
