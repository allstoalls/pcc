# pcc for Python — Tutorial

pcc compiles Python source (`.py`) to a native executable using an
LLVM backend. Typed code compiles to direct LLVM IR; untyped or
dynamic code falls back to the CPython C-API at runtime.

This tutorial walks through the subset that is end-to-end functional
as of **2026-04-20** — phase1 (typed MVP) through phase4 (CPython
fallback).

---

## 1. Install and smoke-test

Requirements: LLVM 20 (`clang`, `opt`), CPython 3.13, `make`.

```bash
git clone <repo>
cd pcc
python -m venv .venv && source .venv/bin/activate
# Pipeline auto-builds pcc/runtime/libpy_runtime.a on first use.
```

Hello, world:

```python
# hello.py
def main() -> None:
    print("hello, pcc")

main()
```

```bash
python -m pcc hello.py -o hello
./hello            # → hello, pcc
```

---

## 2. Typed Python (Phase 1 fast path)

Fully-annotated code compiles to LLVM-native IR — no PyObject layer,
no libpython.

```python
def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def main() -> None:
    for i in range(10):
        print(fib(i))


main()
```

```bash
python -m pcc fib.py -o fib
./fib   # 0 1 1 2 3 5 8 13 21 34 (one per line)
```

Exe size stays under 100 KB when no `import` statements are present.

---

## 3. Classes + OOP (Phase 3)

Single + multi inheritance with MRO, `super()`, `isinstance`,
dunders (`__eq__`, `__lt__`, `__add__`, `__call__`, `__getitem__`,
`__len__`), `@property` (getter + setter), `@staticmethod`.

```python
class Vec:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y

    def __add__(self, other: "Vec") -> "Vec":
        return Vec(self.x + other.x, self.y + other.y)


def main() -> None:
    a = Vec(1, 2)
    b = Vec(3, 4)
    c = a + b
    print(c.x)
    print(c.y)


main()
```

---

## 4. Exceptions (Phase 3)

`try` / `except` / `else` / `finally`, `raise X(msg)`, `raise`,
multi-`except`, `except X as e`, nested try, built-in exception
classes (`ValueError`, `KeyError`, `RuntimeError`, ...).

```python
def main() -> None:
    try:
        raise ValueError("bad")
    except ValueError as e:
        print("caught")
        print(str(e))


main()
```

Under the hood: LLVM `invoke` + `landingpad` with Itanium C++ ABI
personality; pcc's runtime `py_raise` `__cxa_throw`s and the
catch-all landingpad dispatches to `py_exc_matches` for class match.

---

## 5. CPython fallback (Phase 4)

Any `import X` triggers a libpython link; module attributes, calls,
subscripts, and iteration route through `PyObject_*` helpers.

```python
import json, os


def main() -> None:
    data = json.loads('[10, 20, 30]')
    total = 0
    for x in data:
        total = total + x
    print(total)
    print(os.path.join("a", "b", "c"))


main()
```

Runs as-is. No type annotations required for stdlib values —
everything coming back from CPython is tagged as a CPython `PyObject *`
and pcc generates the appropriate conversion at each native boundary
(arithmetic, print, bool check, int/float unbox).

---

## 6. Context managers and iteration

```python
import io


def main() -> None:
    with io.StringIO() as buf:
        buf.write("hello")
        print(buf.getvalue())


main()
```

```python
import os


def main() -> None:
    for entry in os.listdir("."):
        print(entry)


main()
```

---

## 7. What does NOT work (yet)

These are tracked gaps, not surprises. Feed them into a future phase:

- **Bytes literals** (`b"..."`) — pcc's AST has no dedicated
  `BytesLit` node; bytes are lowered to `StrLit` with `latin-1`
  decoding, which misbehaves for non-ASCII.
- **Decorators on user functions** (`@functools.lru_cache`).
  Decorators on methods (`@property`, `@staticmethod`, `@classmethod`)
  do work.
- **Generators** (`def gen(): yield 1; yield 2`) — no coroutine
  state machine yet.
- **`*args` / `**kwargs`** in user function definitions.
- **Class-level variables** — `class C: counter = 0` is not yet a
  module-readable binding via `C.counter`.
- **Full C3 linearization** for diamond multi-inheritance `super()`
  chains — the subset covers the common case (single parent or
  left-first ordering).

Everything else in the plan (phase1–4 acceptance criteria) has
end-to-end tests under `tests/py_corpus/phase[1-4]/`.

---

## 8. Benchmarks

```bash
python tests/py_corpus/run_pcc.py --phase phase4 --bench
```

Reports compile time, best-of-3 runtime, and exe size for every
phase4 corpus test. Typed phase1 tests strip the CPython runtime
dependency and come in under 35 KB.

---

## 9. Source map

```
pcc/
  py_frontend/
    parser.py          # .py → pcc AST via stdlib ast
    type_infer.py      # annotation-driven type checking
    codegen/
      layer1.py        # AST → LLVM IR
      class_gen.py     # class + method lowering
      marshal.py       # pcc ↔ LLVM type bridging
      runtime_abi.py   # runtime function declarations
    pipeline.py        # detect .py, link with py_runtime + libpython
  py_runtime/
    src/py_libpython.c # CPython C-API shim (Py_Initialize, import, etc.)
    src/py_class.c     # class + MRO runtime
    src/py_exc.c       # Itanium EH glue + exception classes
    src/py_int.c / py_str.c / ... # native runtime types
tests/
  py_corpus/
    phase1/ phase2/ phase3/ phase4/   # corpus
    run_pcc.py                         # acceptance + bench harness
```
