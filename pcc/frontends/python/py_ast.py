"""pcc_py AST node definitions (frozen v0.1).

See Section 2 of docs/plans/python-frontend-interfaces.md for the
authoritative contract. These dataclasses are ``frozen=True`` — no
mutation after construction. Type inference runs as a separate pass
that constructs fresh nodes.
"""

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
class RawPointerType(Type):
    """Compiler-unmanaged pointer view, never an automatic GC owner/root.

    Runtime-port C ABIs may use this view for a manually managed PyObject
    address; it does not assert that the underlying allocation is foreign.
    """

    pass


@dataclass(frozen=True)
class IntType(Type):  # name = "int"
    width: int = 64  # tagged default; 32 for explicit i32, etc.
    signed: bool = True


@dataclass(frozen=True)
class FloatType(Type):  # name = "float"
    width: int = 64


@dataclass(frozen=True)
class ComplexType(Type):  # name = "complex"
    pass


@dataclass(frozen=True)
class BoolType(Type):
    pass  # name = "bool"


@dataclass(frozen=True)
class NoneType(Type):
    pass  # name = "None"


@dataclass(frozen=True)
class StrType(Type):
    pass  # name = "str"


@dataclass(frozen=True)
class BytesType(Type):
    pass  # name = "bytes"


@dataclass(frozen=True)
class ByteArrayType(Type):
    pass  # name = "bytearray"


@dataclass(frozen=True)
class MemoryViewType(Type):
    pass  # name = "memoryview"


@dataclass(frozen=True)
class ListType(Type):
    elem: Type


@dataclass(frozen=True)
class SetType(Type):
    """First-class set/frozenset projection for native lowering."""

    elem: Type


@dataclass(frozen=True)
class ValueArrayType(Type):
    """Fixed-length pcc-owned array of one valueclass payload type."""

    elem: Type
    length: int


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
    # ``@property`` declarations: name → declared return type. Kept
    # separate from ``fields`` so callers cannot pass a property name
    # to a positional constructor, and from method tables so
    # ``c.prop()`` doesn't accidentally type-check. See
    # docs/investigations/pcc-py-type-infer-property-return-type.md.
    properties: tuple[tuple[str, Type], ...] = ()
    valueclass: bool = False


@dataclass(frozen=True)
class ValueClassType(ClassType):
    """Opt-in identity-free class type for the Valhalla value model."""

    flattened: bool = True
    nullable_fields: bool = False


@dataclass(frozen=True)
class DynType(Type):
    pass  # name = "dyn"; fallback when untyped


# -- Expressions -------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    span: SourceSpan
    ty: Type


@dataclass(frozen=True)
class IntLit(Expr):
    # A source literal has no bound (``0xFFFFFFFFFFFFFFFF``, ``10 ** 30``
    # folded), so this is deliberately not ``int``: in pcc's own raw-int
    # build an ``int`` field reads through the i64 lane, which raised on
    # (and formerly zeroed) every literal beyond it -- Stage2 stopped in
    # ``_emit_expr_as_i64`` on c_codegen.py's 64-bit masks.
    value: object


@dataclass(frozen=True)
class FloatLit(Expr):
    value: float


@dataclass(frozen=True)
class ComplexLit(Expr):
    real: float
    imag: float


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True)
class NoneLit(Expr):
    pass


@dataclass(frozen=True)
class StrLit(Expr):
    value: str


@dataclass(frozen=True)
class BytesLit(Expr):
    value: bytes


@dataclass(frozen=True)
class Name(Expr):
    ident: str


@dataclass(frozen=True)
class BinOp(Expr):
    op: str  # "+", "-", "*", "/", "//", "%", "**",
    # "&", "|", "^", "<<", ">>"
    lhs: Expr
    rhs: Expr


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str  # "-", "+", "~", "not"
    operand: Expr


@dataclass(frozen=True)
class Compare(Expr):
    op: str  # "==", "!=", "<", "<=", ">", ">=", "is", "is not", "in", "not in"
    lhs: Expr
    rhs: Expr


@dataclass(frozen=True)
class BoolExpr(Expr):
    op: str  # "and", "or"
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Call(Expr):
    func: Expr
    args: tuple[Expr, ...]
    kwargs: tuple[tuple[str, Expr], ...] = ()
    # Source order across the separately stored positional/starred and
    # keyword operands.  Each entry is ("arg" | "kw", index).  The native
    # parser retains this order, while splitting args/kwargs without this
    # metadata loses cases such as ``f(**m(), x=g())``.
    operand_order: tuple[tuple[str, int], ...] = ()
    # A set display is represented by a synthetic set(ListExpr(...)) call.
    # Keep that syntax provenance: unlike an explicit sequence constructor,
    # it does not resolve a user binding named set or evaluate a list first.
    is_set_literal: bool = False


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
class ListExpr(Expr):
    elems: tuple[Expr, ...]


@dataclass(frozen=True)
class DictExpr(Expr):
    pairs: tuple[tuple[Expr, Expr], ...]


@dataclass(frozen=True)
class TupleExpr(Expr):
    elems: tuple[Expr, ...]


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
    targets: tuple[Expr, ...]  # Name/Attr/Subscript
    value: Expr
    annotation: Optional[Type] = None
    # A declaration-only annotation carries an inert value placeholder.
    # Explicit ``= None`` is a real initializer and must remain distinct.
    has_value: bool = True


def assignment_storage_annotation(annotation: Optional[Type], value: Expr,
                                  has_value: bool) -> Optional[Type]:
    """Keep annotation metadata separate from a nullable scalar value lane.

    Ordinary Python annotations do not prevent explicit None initialization.
    Raw machine scalar names are deliberately excluded from this widening.
    """
    if (has_value and isinstance(value, NoneLit) and annotation is not None
            and annotation.name in ("int", "float", "bool", "complex")):
        return DynType(name="dyn")
    return annotation


@dataclass(frozen=True)
class AugAssign(Stmt):
    target: Expr
    op: str  # "+=", etc.
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
    is_async: bool = False


@dataclass(frozen=True)
class Return(Stmt):
    value: Optional[Expr]


@dataclass(frozen=True)
class Pass(Stmt):
    pass


@dataclass(frozen=True)
class Break(Stmt):
    pass


@dataclass(frozen=True)
class Continue(Stmt):
    pass


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
    is_async: bool = False


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
    has_default: bool = False


@dataclass(frozen=True)
class FuncDef(Stmt):
    name: str
    args: tuple[Arg, ...]
    return_ty: Optional[Type]
    body: tuple[Stmt, ...]
    decorators: tuple[Expr, ...] = ()
    is_method: bool = False
    is_async: bool = False
    # Inference replaces a missing return annotation with DynType; retain
    # whether that type came from an explicit source contract.
    has_return_annotation: bool = False
    # Set by the defining module's verified native pointer-lane directive.
    manual_pointer_abi: bool = False


@dataclass(frozen=True)
class ClassDef(Stmt):
    name: str
    bases: tuple[Expr, ...]
    keywords: tuple[tuple[str, Expr], ...]  # for metaclass=
    body: tuple[Stmt, ...]
    decorators: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class Module:
    name: str
    body: tuple[Stmt, ...]
    docstring: Optional[str] = None
