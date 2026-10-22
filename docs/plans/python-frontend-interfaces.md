# pcc Python Frontend — Interface Contracts

**Status:** Frozen at v0.1 for parallel agent dispatch.
**Purpose:** Every agent working on Python-frontend tasks reads this
doc and treats its APIs as authoritative. No agent may change these
contracts unilaterally; updates require coordinator approval.

---

## 1. Directory Layout

```
pcc/
├── py_frontend/           ← Phase 1–3 Python frontend
│   ├── __init__.py
│   ├── parser.py          ← .py → pcc_py AST
│   ├── py_ast.py          ← AST node dataclasses
│   ├── type_infer.py      ← annotation + inference
│   ├── types.py           ← pcc_py Type class hierarchy
│   └── codegen/
│       ├── __init__.py
│       ├── layer1.py      ← typed → native LLVM IR
│       ├── layer2.py      ← inferred types
│       ├── layer3.py      ← dynamic → PyObject*
│       └── runtime_abi.py ← function signature table
│
├── py_runtime/            ← C runtime (compiled to py_runtime.a)
│   ├── include/py_runtime.h
│   ├── src/
│   │   ├── py_obj.c
│   │   ├── py_int.c
│   │   ├── py_str.c
│   │   ├── py_list.c
│   │   ├── py_dict.c
│   │   ├── py_tuple.c
│   │   ├── py_gc.c
│   │   ├── py_exc.c
│   │   ├── py_print.c
│   │   └── py_cpython.c   ← libpython bridge (Phase 4; not used in self-host)
│   └── Makefile
│
├── py_stdlib/             ← Phase 6C.4 stdlib replacements
│   ├── re.py
│   ├── os.py
│   ├── subprocess.py
│   ├── dataclasses.py
│   ├── typing.py
│   └── ... (18 modules)
│
├── llvm_capi/             ← Phase 6C.2 llvmlite replacement
│   ├── __init__.py
│   ├── core.py            ← LLVMContextRef etc.
│   ├── binding.py         ← llvmlite.binding equivalents
│   └── ir.py              ← llvmlite.ir equivalents
│
└── extern/                ← Phase 6C.1 FFI
    ├── __init__.py
    └── _ffi_types.py      ← c_int, c_ptr, c_str etc.
```

---

## 2. pcc_py AST (frozen v0.1)

```python
# pcc/frontends/python/py_ast.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Union

@dataclass(frozen=True)
class SourceSpan:
    """Line/column range for diagnostics."""
    file: str
    line: int
    col: int
    end_line: int
    end_col: int

# -- Types -------------------------------------------------------------------

@dataclass(frozen=True)
class Type:
    """Base; every type has a name."""
    name: str

@dataclass(frozen=True)
class IntType(Type):    # name = "int"
    width: int = 64     # tagged default; 32 for explicit i32, etc.
    signed: bool = True
@dataclass(frozen=True)
class FloatType(Type):  # name = "float"
    width: int = 64
@dataclass(frozen=True)
class BoolType(Type):   pass   # name = "bool"
@dataclass(frozen=True)
class NoneType(Type):   pass   # name = "None"
@dataclass(frozen=True)
class StrType(Type):    pass   # name = "str"
@dataclass(frozen=True)
class ListType(Type):
    elem: Type
@dataclass(frozen=True)
class DictType(Type):
    key: Type
    value: Type
@dataclass(frozen=True)
class TupleType(Type):
    elems: tuple[Type, ...]
@dataclass(frozen=True)
class FuncType(Type):
    params: tuple[Type, ...]
    ret: Type
@dataclass(frozen=True)
class ClassType(Type):
    module: str
    fields: tuple[tuple[str, Type], ...] = ()
    bases: tuple["ClassType", ...] = ()
@dataclass(frozen=True)
class DynType(Type):    pass   # name = "dyn"; fallback when untyped

# -- Expressions -------------------------------------------------------------

@dataclass(frozen=True)
class Expr:
    span: SourceSpan
    ty: Type

@dataclass(frozen=True)
class IntLit(Expr):      value: int
@dataclass(frozen=True)
class FloatLit(Expr):    value: float
@dataclass(frozen=True)
class BoolLit(Expr):     value: bool
@dataclass(frozen=True)
class NoneLit(Expr):     pass
@dataclass(frozen=True)
class StrLit(Expr):      value: str
@dataclass(frozen=True)
class Name(Expr):        ident: str
@dataclass(frozen=True)
class BinOp(Expr):
    op: str              # "+", "-", "*", "/", "//", "%", "**",
                         # "&", "|", "^", "<<", ">>"
    lhs: Expr
    rhs: Expr
@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str              # "-", "+", "~", "not"
    operand: Expr
@dataclass(frozen=True)
class Compare(Expr):
    op: str              # "==", "!=", "<", "<=", ">", ">=", "is", "is not", "in", "not in"
    lhs: Expr
    rhs: Expr
@dataclass(frozen=True)
class BoolExpr(Expr):
    op: str              # "and", "or"
    left: Expr
    right: Expr
@dataclass(frozen=True)
class Call(Expr):
    func: Expr
    args: tuple[Expr, ...]
    kwargs: tuple[tuple[str, Expr], ...] = ()
@dataclass(frozen=True)
class Attr(Expr):
    obj: Expr
    name: str
@dataclass(frozen=True)
class Subscript(Expr):
    obj: Expr
    idx: Expr
@dataclass(frozen=True)
class Slice(Expr):
    lo: Optional[Expr]
    hi: Optional[Expr]
    step: Optional[Expr]
@dataclass(frozen=True)
class ListExpr(Expr):    elems: tuple[Expr, ...]
@dataclass(frozen=True)
class DictExpr(Expr):    pairs: tuple[tuple[Expr, Expr], ...]
@dataclass(frozen=True)
class TupleExpr(Expr):   elems: tuple[Expr, ...]
@dataclass(frozen=True)
class IfExpr(Expr):
    cond: Expr
    then_e: Expr
    else_e: Expr
@dataclass(frozen=True)
class Lambda(Expr):
    params: tuple["Arg", ...]
    body: Expr

# -- Statements --------------------------------------------------------------

@dataclass(frozen=True)
class Stmt:
    span: SourceSpan

@dataclass(frozen=True)
class Assign(Stmt):
    targets: tuple[Expr, ...]   # Name/Attr/Subscript
    value: Expr
    annotation: Optional[Type] = None
@dataclass(frozen=True)
class AugAssign(Stmt):
    target: Expr
    op: str                      # "+=", etc.
    value: Expr
@dataclass(frozen=True)
class ExprStmt(Stmt):
    expr: Expr
@dataclass(frozen=True)
class If(Stmt):
    cond: Expr
    body: tuple[Stmt, ...]
    else_body: tuple[Stmt, ...] = ()
@dataclass(frozen=True)
class While(Stmt):
    cond: Expr
    body: tuple[Stmt, ...]
    else_body: tuple[Stmt, ...] = ()
@dataclass(frozen=True)
class For(Stmt):
    target: Expr
    iter: Expr
    body: tuple[Stmt, ...]
    else_body: tuple[Stmt, ...] = ()
@dataclass(frozen=True)
class Return(Stmt):
    value: Optional[Expr]
@dataclass(frozen=True)
class Pass(Stmt):    pass
@dataclass(frozen=True)
class Break(Stmt):   pass
@dataclass(frozen=True)
class Continue(Stmt): pass
@dataclass(frozen=True)
class Raise(Stmt):
    exc: Optional[Expr]
    cause: Optional[Expr]
@dataclass(frozen=True)
class Try(Stmt):
    body: tuple[Stmt, ...]
    handlers: tuple["ExceptHandler", ...]
    else_body: tuple[Stmt, ...]
    finally_body: tuple[Stmt, ...]
@dataclass(frozen=True)
class ExceptHandler:
    exc_type: Optional[Expr]
    name: Optional[str]
    body: tuple[Stmt, ...]
    span: SourceSpan
@dataclass(frozen=True)
class With(Stmt):
    items: tuple[tuple[Expr, Optional[Expr]], ...]  # (ctx, as_var?)
    body: tuple[Stmt, ...]
@dataclass(frozen=True)
class Import(Stmt):
    names: tuple[tuple[str, Optional[str]], ...]  # (module, asname?)
@dataclass(frozen=True)
class ImportFrom(Stmt):
    module: str
    names: tuple[tuple[str, Optional[str]], ...]
    level: int = 0
@dataclass(frozen=True)
class Global(Stmt):
    names: tuple[str, ...]
@dataclass(frozen=True)
class Nonlocal(Stmt):
    names: tuple[str, ...]
@dataclass(frozen=True)
class Delete(Stmt):
    targets: tuple[Expr, ...]

# -- Top-level & declarations -----------------------------------------------

@dataclass(frozen=True)
class Arg:
    name: str
    annotation: Optional[Type]
    default: Optional[Expr]
    kind: str  # "pos", "kw_only", "pos_only", "*args", "**kwargs"

@dataclass(frozen=True)
class FuncDef(Stmt):
    name: str
    args: tuple[Arg, ...]
    return_ty: Optional[Type]
    body: tuple[Stmt, ...]
    decorators: tuple[Expr, ...] = ()
    is_method: bool = False
    is_async: bool = False

@dataclass(frozen=True)
class ClassDef(Stmt):
    name: str
    bases: tuple[Expr, ...]
    keywords: tuple[tuple[str, Expr], ...]   # for metaclass=
    body: tuple[Stmt, ...]
    decorators: tuple[Expr, ...] = ()

@dataclass(frozen=True)
class Module:
    name: str
    body: tuple[Stmt, ...]
    docstring: Optional[str] = None
```

**Rule:** AST nodes are `frozen=True` dataclasses. No mutation after
construction. Type annotations are either on `Arg`/`Assign`/`FuncDef`
explicitly, or filled in by `type_infer` in a separate pass that
constructs fresh nodes (no `__setattr__` on frozen).

---

## 3. Runtime Library C ABI (frozen v0.1)

```c
/* pcc/runtime/include/py_runtime.h */
#ifndef PY_RUNTIME_H
#define PY_RUNTIME_H

#include <stdint.h>
#include <stddef.h>

/* Opaque PyObject; concrete definition lives in py_obj.c */
typedef struct PyObject PyObject;

/* Type tag values — used in PyObject header and tagged int */
enum {
    PY_TYPE_NONE    = 0,
    PY_TYPE_BOOL    = 1,
    PY_TYPE_INT     = 2,    /* bignum; non-tagged form */
    PY_TYPE_FLOAT   = 3,
    PY_TYPE_STR     = 4,
    PY_TYPE_LIST    = 5,
    PY_TYPE_DICT    = 6,
    PY_TYPE_TUPLE   = 7,
    PY_TYPE_SET     = 8,
    PY_TYPE_FUNC    = 9,
    PY_TYPE_CLASS   = 10,
    PY_TYPE_INSTANCE= 11,
    PY_TYPE_EXC     = 12,
    PY_TYPE_USER    = 100   /* user-defined classes >= this */
};

/* Every PyObject has this header prefix. */
typedef struct {
    int64_t refcount;
    int32_t  type_tag;
    int32_t  flags;        /* bit 0 = immortal, bit 1 = gc-tracked, ... */
} PyObjectHeader;

/* ---- INCREF/DECREF ----------------------------------------------------- */
void py_incref(PyObject *o);
void py_decref(PyObject *o);

/* ---- None -------------------------------------------------------------- */
extern PyObject *const py_None;

/* ---- Bool -------------------------------------------------------------- */
extern PyObject *const py_True;
extern PyObject *const py_False;
PyObject *py_bool_from_bit(int b);           /* b: 0 or 1 */

/* ---- Tagged int (fast path) + bignum (slow path) ---------------------- */
/* Tagged: low bit = 1 means tagged int; real value is (val >> 1).
 * Non-tagged: regular PyObject* with PY_TYPE_INT header. */
PyObject *py_int_from_i64(int64_t v);
int64_t   py_int_to_i64(PyObject *o, int *overflow);   /* returns 0 on overflow */
PyObject *py_int_add(PyObject *a, PyObject *b);
PyObject *py_int_sub(PyObject *a, PyObject *b);
PyObject *py_int_mul(PyObject *a, PyObject *b);
PyObject *py_int_floordiv(PyObject *a, PyObject *b);   /* Python floor semantics */
PyObject *py_int_truediv(PyObject *a, PyObject *b);    /* returns float */
PyObject *py_int_mod(PyObject *a, PyObject *b);        /* Python sign semantics */
PyObject *py_int_pow(PyObject *a, PyObject *b);
PyObject *py_int_neg(PyObject *a);
PyObject *py_int_and(PyObject *a, PyObject *b);
PyObject *py_int_or(PyObject *a, PyObject *b);
PyObject *py_int_xor(PyObject *a, PyObject *b);
PyObject *py_int_shl(PyObject *a, PyObject *b);
PyObject *py_int_shr(PyObject *a, PyObject *b);
int       py_int_cmp(PyObject *a, PyObject *b);        /* -1, 0, 1 */

/* ---- Float ------------------------------------------------------------- */
PyObject *py_float_from_f64(double v);
double    py_float_to_f64(PyObject *o);
PyObject *py_float_add(PyObject *a, PyObject *b);
/* ... sub, mul, div, mod, pow, neg, cmp ... */

/* ---- Str --------------------------------------------------------------- */
PyObject *py_str_new(const char *utf8, int64_t byte_len);
int64_t   py_str_len(PyObject *s);             /* in codepoints */
int64_t   py_str_byte_len(PyObject *s);        /* in UTF-8 bytes */
const char *py_str_utf8(PyObject *s);          /* borrowed, NUL-terminated */
PyObject *py_str_concat(PyObject *a, PyObject *b);
PyObject *py_str_repeat(PyObject *s, PyObject *n);
PyObject *py_str_slice(PyObject *s, PyObject *lo, PyObject *hi, PyObject *step);
PyObject *py_str_index(PyObject *s, PyObject *i);    /* returns single-char str */
int       py_str_eq(PyObject *a, PyObject *b);
int       py_str_contains(PyObject *s, PyObject *sub);
int64_t   py_str_find(PyObject *s, PyObject *sub);   /* -1 if not found */
PyObject *py_str_upper(PyObject *s);
PyObject *py_str_lower(PyObject *s);
PyObject *py_str_strip(PyObject *s);
PyObject *py_str_split(PyObject *s, PyObject *sep);  /* returns list */
PyObject *py_str_join(PyObject *sep, PyObject *list);
PyObject *py_str_replace(PyObject *s, PyObject *old, PyObject *new);
int       py_str_startswith(PyObject *s, PyObject *prefix);
int       py_str_endswith(PyObject *s, PyObject *suffix);

/* ---- List -------------------------------------------------------------- */
PyObject *py_list_new(int64_t initial_capacity);
void      py_list_append(PyObject *lst, PyObject *item);
PyObject *py_list_get(PyObject *lst, int64_t i);     /* new ref */
void      py_list_set(PyObject *lst, int64_t i, PyObject *item);
int64_t   py_list_len(PyObject *lst);
PyObject *py_list_slice(PyObject *lst, PyObject *lo, PyObject *hi, PyObject *step);
PyObject *py_list_concat(PyObject *a, PyObject *b);
void      py_list_extend(PyObject *a, PyObject *b);
void      py_list_insert(PyObject *lst, int64_t i, PyObject *item);
PyObject *py_list_pop(PyObject *lst, int64_t i);
void      py_list_remove(PyObject *lst, PyObject *item);
int       py_list_contains(PyObject *lst, PyObject *item);
int64_t   py_list_index(PyObject *lst, PyObject *item);

/* ---- Dict -------------------------------------------------------------- */
PyObject *py_dict_new(void);
void      py_dict_set(PyObject *d, PyObject *k, PyObject *v);
PyObject *py_dict_get(PyObject *d, PyObject *k);     /* NULL if missing */
PyObject *py_dict_get_default(PyObject *d, PyObject *k, PyObject *def);
int       py_dict_contains(PyObject *d, PyObject *k);
int       py_dict_del(PyObject *d, PyObject *k);     /* returns -1 on missing */
int64_t   py_dict_len(PyObject *d);
PyObject *py_dict_keys(PyObject *d);                 /* list */
PyObject *py_dict_values(PyObject *d);               /* list */
PyObject *py_dict_items(PyObject *d);                /* list of tuples */

/* ---- Tuple ------------------------------------------------------------- */
PyObject *py_tuple_new(int64_t n);
void      py_tuple_set_item(PyObject *t, int64_t i, PyObject *item); /* during construction only */
PyObject *py_tuple_get(PyObject *t, int64_t i);
int64_t   py_tuple_len(PyObject *t);

/* ---- Set --------------------------------------------------------------- */
PyObject *py_set_new(void);
void      py_set_add(PyObject *s, PyObject *item);
int       py_set_contains(PyObject *s, PyObject *item);
int       py_set_remove(PyObject *s, PyObject *item);
int64_t   py_set_len(PyObject *s);

/* ---- Generic object ops ----------------------------------------------- */
PyObject *py_obj_call(PyObject *callable, PyObject *args_tuple, PyObject *kwargs_dict);
PyObject *py_obj_getattr(PyObject *o, const char *name);
int       py_obj_setattr(PyObject *o, const char *name, PyObject *v);
PyObject *py_obj_getitem(PyObject *o, PyObject *k);
int       py_obj_setitem(PyObject *o, PyObject *k, PyObject *v);
int64_t   py_obj_len(PyObject *o);
int       py_obj_truthy(PyObject *o);                /* 0 or 1 */
int       py_obj_eq(PyObject *a, PyObject *b);
int64_t   py_obj_hash(PyObject *o);
PyObject *py_obj_repr(PyObject *o);
PyObject *py_obj_str(PyObject *o);
int       py_obj_isinstance(PyObject *o, PyObject *cls);

/* ---- Printing ---------------------------------------------------------- */
void py_print(PyObject *o);                 /* writes repr + "\n" to stdout */
void py_print_many(PyObject *args_tuple, PyObject *sep, PyObject *end);

/* ---- Exceptions (Phase 3) --------------------------------------------- */
void py_raise(PyObject *exc);                       /* sets thread-local current exc */
PyObject *py_current_exception(void);
void py_clear_exception(void);
PyObject *py_exc_new(int32_t type_tag, const char *msg);

/* ---- GC ---------------------------------------------------------------- */
void py_gc_init(void);
void py_gc_collect(void);
void py_gc_track(PyObject *o);
void py_gc_untrack(PyObject *o);

#endif /* PY_RUNTIME_H */
```

---

## 4. LLVM IR Conventions

- **Naming:**
  - `py_*` prefix = runtime lib function.
  - `user_<module>_<name>` prefix = user-defined function from .py.
  - `%py.` prefix for SSA values holding PyObject pointers.
  - `%i.` / `%f.` / `%b.` for native-typed SSA values.

- **Types:**
  - `i64` = Python `int` in fast path (tagged).
  - `double` = `float`.
  - `i1` = `bool`.
  - `ptr` = PyObject* / generic pointer.
  - `void` = returns nothing.

- **Calling convention:**
  - All runtime calls use C calling convention.
  - Caller owns returned PyObject* unless docstring says "borrowed".
  - Functions returning `PyObject*` may return `null` on error; caller
    must check `py_current_exception()`.

- **Control flow:**
  - Each `def` is one LLVM function.
  - Exception-unwinding calls use `invoke` with matching `landingpad`
    in the containing try-block.

---

## 5. extern "C" FFI Syntax (Phase 6C.1)

```python
# pcc/extern/_ffi_types.py
from typing import TypeVar, Generic

class c_int:   pass     # i32
class c_int64: pass     # i64
class c_uint:  pass     # u32
class c_float: pass     # f32
class c_double: pass    # f64
class c_ptr:   pass     # opaque ptr
class c_str:   pass     # ptr to UTF-8 NUL-terminated

def extern(symbol: str): ...   # decorator/factory returns a callable
```

Example in user code:

```python
from pcc.extern import extern, c_int, c_str

printf: Callable[[c_str, ...], c_int] = extern("printf")

def main() -> None:
    printf("hello %d\n", 42)
```

Codegen: `call i32 (ptr, ...) @printf(ptr @.hello_fmt, i32 42)` — no
marshalling, direct.

---

## 6. LLVM C API Binding Surface (Phase 6C.2)

```python
# pcc/ir/binding.py
from .core import LLVMContextRef, LLVMModuleRef

class Module:
    @classmethod
    def parse_assembly(cls, ir_text: str) -> "Module": ...
    def verify(self) -> None: ...
    def __str__(self) -> str: ...
    # Iterators matching llvmlite.binding.ModuleRef
    @property
    def functions(self) -> list["Function"]: ...
    @property
    def global_variables(self) -> list["GlobalVariable"]: ...

class Function:
    name: str
    is_declaration: bool
    @property
    def blocks(self) -> list["BasicBlock"]: ...
    @property
    def arguments(self) -> list["Argument"]: ...

class BasicBlock:
    name: str
    @property
    def instructions(self) -> list["Instruction"]: ...

class Instruction:
    opcode: str
    type: str
    def __str__(self) -> str: ...

class Argument:
    name: str
    type: str

class GlobalVariable:
    name: str
```

All other llvmlite.binding APIs pcc uses must be listed in the
coordinator-maintained `llvm_capi_surface.md` audit doc before any
agent implements them.

---

## 7. Layer Discipline

Every expression and every statement is tagged with one of three
execution tiers during codegen:

- **L1 (typed fast path):** all operands have native types; direct
  LLVM ops.
- **L2 (typed with some Py objects):** some operands are PyObject*
  (e.g. a list element), others are native. Marshalling at boundaries.
- **L3 (dynamic):** all operands are PyObject*; all ops dispatch via
  runtime lib.

**Rule for codegen agents:** if you can't prove all operands are
native, fall to L2. If you can't prove anything, fall to L3. Never
guess — emit a runtime call.

---

## 8. Error Reporting Convention

Every compile error is a `PyFrontendError` subclass with:

```python
@dataclass
class PyFrontendError(Exception):
    span: SourceSpan
    message: str
    hint: Optional[str] = None

    def format(self) -> str:
        """
        fib.py:3:5: error: cannot infer type of 'x'
           x = some_dynamic()
           ^
        hint: add an annotation: x: int = some_dynamic()
        """
```

Agents must raise `PyFrontendError` (or a subclass) for every user-
visible compile failure. No bare `raise RuntimeError` from user input.

---

## 9. Test Corpus Convention

Each acceptance test is a triple:

```
tests/py_corpus/<phase>/<name>/
  source.py         ← input
  expected.stdout   ← expected output when running via CPython
  expected.status   ← expected exit code (usually 0)
  pcc.flags         ← (optional) extra flags for pcc
```

Harness compares pcc-produced binary's stdout+exit against
`expected.*`.

---

## 10. What Agents May / May Not Do

**May:**
- Implement anything under their assigned module / file.
- Add new AST node types **if accompanied by a PR-style note to this
  contract** for coordinator review.
- Add new runtime library functions **if their signature is added
  here first.**

**Must not:**
- Change any signature in this contract.
- Assume any un-documented function exists.
- Touch another agent's files.
- Run tests (per current sprint: "skip tests").

---

## 11. Version

Contract version: **0.1** (2026-04-19).
Any upgrade bumps the minor; agents must re-read before dispatch.
