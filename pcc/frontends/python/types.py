"""Type hierarchy utilities for the pcc Python frontend.

Utility helpers over the ``Type`` dataclass hierarchy defined in
``pcc.frontends.python.py_ast``.  This module intentionally does *not* redefine
the ``Type`` dataclasses — it only imports them and provides helpers such
as annotation parsing, equality, numeric promotion, and a handful of
module-level singleton constants.

See ``docs/plans/python-frontend-interfaces.md`` section 2 for the frozen
AST/Type definitions and section 8 for the error-reporting convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pcc.frontends.python.py_ast import (
    RawPointerType,
    Attr,
    BoolType,
    ByteArrayType,
    BytesType,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    Expr,
    FloatType,
    FuncType,
    IntLit,
    IntType,
    ListType,
    ListExpr,
    MemoryViewType,
    Name,
    NoneLit,
    NoneType,
    SetType,
    SourceSpan,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
    ValueArrayType,
)

# ---------------------------------------------------------------------------
# Module-level singleton type constants.
#
# These are the canonical "Phase 1 default" instances of each primitive
# type.  Equality is structural (dataclass __eq__), so comparing against
# these constants is safe even if other parts of the compiler construct
# their own instances with the same fields.
# ---------------------------------------------------------------------------

TYPE_INT: IntType = IntType(name="int", width=64, signed=True)
TYPE_I64: IntType = IntType(name="pcc.i64", width=64, signed=True)
TYPE_U64: IntType = IntType(name="pcc.u64", width=64, signed=False)
TYPE_FLOAT: FloatType = FloatType(name="float", width=64)
TYPE_COMPLEX: ComplexType = ComplexType(name="complex")
TYPE_BOOL: BoolType = BoolType(name="bool")
TYPE_NONE: NoneType = NoneType(name="None")
TYPE_STR: StrType = StrType(name="str")
TYPE_BYTES: BytesType = BytesType(name="bytes")
TYPE_BYTEARRAY: ByteArrayType = ByteArrayType(name="bytearray")
TYPE_MEMORYVIEW: MemoryViewType = MemoryViewType(name="memoryview")
TYPE_DYN: DynType = DynType(name="dyn")
TYPE_SET: SetType = SetType(name="set", elem=TYPE_DYN)
TYPE_FROZENSET: SetType = SetType(name="frozenset", elem=TYPE_DYN)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@dataclass
class PyFrontendError(Exception):
    """Base class for user-visible compile failures in the Python frontend.

    Matches the convention documented in
    ``docs/plans/python-frontend-interfaces.md`` section 8.
    """

    span: Optional[SourceSpan]
    message: str
    hint: Optional[str] = None

    def __post_init__(self) -> None:
        # Let ``Exception`` carry a printable message for convenience when
        # the error propagates untouched.
        super().__init__(self.format())

    def format(self) -> str:
        """Render a short diagnostic string.

        The fancier "source excerpt + caret" form lives in the driver; this
        method is sufficient for logging and tests.
        """
        if self.span is not None:
            loc = f"{self.span.file}:{self.span.line}:{self.span.col}"
        else:
            loc = "<unknown>"
        out = f"{loc}: error: {self.message}"
        if self.hint:
            out += f"\nhint: {self.hint}"
        return out


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------

# Map of plain Python builtin-name annotations to their Phase 1 types.
_BUILTIN_NAMED_TYPES: dict[str, Type] = {
    "int": TYPE_INT,
    "i64": TYPE_I64,
    "u64": TYPE_U64,
    "pcc.i64": TYPE_I64,
    "pcc.u64": TYPE_U64,
    "float": TYPE_FLOAT,
    "complex": TYPE_COMPLEX,
    "bool": TYPE_BOOL,
    "str": TYPE_STR,
    "bytes": TYPE_BYTES,
    "bytearray": TYPE_BYTEARRAY,
    "memoryview": TYPE_MEMORYVIEW,
    "None": TYPE_NONE,
    "NoneType": TYPE_NONE,
    "object": TYPE_DYN,
    "Any": TYPE_DYN,
    "set": TYPE_SET,
    "frozenset": TYPE_FROZENSET,
}


def _name_ident(expr: Expr) -> Optional[str]:
    """Return the identifier if ``expr`` is a bare ``Name``, else ``None``."""
    if isinstance(expr, Name):
        return expr.ident
    return None


def _dotted_name(expr: Expr) -> Optional[str]:
    ident = _name_ident(expr)
    if ident is not None:
        return ident
    if isinstance(expr, Attr):
        prefix = _dotted_name(expr.obj)
        if prefix:
            return prefix + "." + expr.name
    return None


def _class_type_from_dotted(name: str) -> ClassType:
    if "." in name:
        last_dot = -1
        i = 0
        while i < len(name):
            if name[i] == ".":
                last_dot = i
            i += 1
        module = name[:last_dot]
        leaf = name[last_dot + 1 :]
        return ClassType(name=leaf, module=module, fields=(), bases=())
    return ClassType(name=name, module="", fields=(), bases=())


def _parse_string_annotation(text: str) -> Optional[Type]:
    text = text.strip()
    if text.startswith("Optional[") and text.endswith("]"):
        return _class_type_from_dotted(text[len("Optional[") : -1].strip())
    return None


def parse_annotation(expr: Expr) -> Type:
    """Convert a Python annotation AST node into a ``Type`` instance.

    Supports:

    * Bare names such as ``int``, ``float``, ``bool``, ``str``, ``None``.
    * Subscripted generics ``list[T]``, ``dict[K, V]``, ``tuple[T1, T2, ...]``.
    * ``Optional[T]`` → treated as ``T`` for Phase 1 (nullability lives
      outside the type system until Phase 2).
    * Anything unrecognised falls back to ``DynType``.
    """

    # Bare name annotation: int, float, bool, str, None, MyClass, ...
    ident = _dotted_name(expr)
    if ident is not None:
        if ident in _BUILTIN_NAMED_TYPES:
            return _BUILTIN_NAMED_TYPES[ident]
        # User-defined class name — we do not have the class body yet in
        # Phase 1, so record it as a ClassType with no fields/bases.  The
        # inference pass can refine later once class defs are collected.
        return _class_type_from_dotted(ident)

    if isinstance(expr, StrLit) and expr.value:
        parsed = _parse_string_annotation(expr.value)
        if parsed is not None:
            return parsed
        if expr.value in _BUILTIN_NAMED_TYPES:
            return _BUILTIN_NAMED_TYPES[expr.value]
        return _class_type_from_dotted(expr.value)

    # A NoneLit used as an annotation (``-> None`` parses that way in some
    # frontends; robustness only).
    if isinstance(expr, NoneLit):
        return TYPE_NONE

    # Subscripted generic: list[int], dict[str, int], tuple[int, str], ...
    if isinstance(expr, Subscript):
        head = _dotted_name(expr.obj)
        idx = expr.idx

        # Collect the index expressions (Subscript holds a single Expr; for
        # multi-arg generics the parser may lower this as a TupleExpr).
        if isinstance(idx, TupleExpr):
            idx_exprs = tuple(idx.elems)
        else:
            idx_exprs = (idx,)

        if head == "list" or head == "List":
            if len(idx_exprs) == 1:
                return ListType(name="list", elem=parse_annotation(idx_exprs[0]))
            return ListType(name="list", elem=TYPE_DYN)

        if head in ("pcc.array", "array"):
            if len(idx_exprs) != 2:
                raise PyFrontendError(
                    expr.span,
                    "pcc.array needs an element type and literal length",
                    "use pcc.array[ValueClass, N] with N between 1 and 7",
                )
            length_expr = idx_exprs[1]
            if not isinstance(length_expr, IntLit):
                raise PyFrontendError(
                    length_expr.span,
                    "pcc.array length must be an integer literal",
                    "write a literal length between 1 and 7",
                )
            length = int(length_expr.value)
            if length < 1 or length > 7:
                raise PyFrontendError(
                    length_expr.span,
                    "pcc.array length must be between 1 and 7",
                    "the selected self-backend aggregate ABI supports lengths 1..7",
                )
            return ValueArrayType(
                name="pcc.array",
                elem=parse_annotation(idx_exprs[0]),
                length=length,
            )

        if head in ("pcc.i64_buffer", "i64_buffer"):
            if len(idx_exprs) != 1:
                raise PyFrontendError(
                    expr.span,
                    "pcc.i64_buffer needs one literal length",
                    "use pcc.i64_buffer[N] with N between 1 and 1048576",
                )
            length_expr = idx_exprs[0]
            if not isinstance(length_expr, IntLit):
                raise PyFrontendError(
                    length_expr.span,
                    "pcc.i64_buffer length must be an integer literal",
                    "write a literal length between 1 and 1048576",
                )
            length = int(length_expr.value)
            if length < 1 or length > 1_048_576:
                raise PyFrontendError(
                    length_expr.span,
                    "pcc.i64_buffer length must be between 1 and 1048576",
                    "choose a bounded fixed-length specialization candidate",
                )
            # The runtime representation deliberately remains exact immutable
            # bytes.  Encoding the fixed element count in the semantic type
            # name lets ordinary BytesType ABI/ownership paths stay valid.
            return BytesType(name="pcc.i64_buffer[" + str(length) + "]")

        if head == "dict" or head == "Dict":
            if len(idx_exprs) == 2:
                return DictType(
                    name="dict",
                    key=parse_annotation(idx_exprs[0]),
                    value=parse_annotation(idx_exprs[1]),
                )
            return DictType(name="dict", key=TYPE_DYN, value=TYPE_DYN)

        if head in ("set", "Set", "frozenset", "FrozenSet"):
            elem = parse_annotation(idx_exprs[0]) if len(idx_exprs) == 1 else TYPE_DYN
            name = "frozenset" if head in ("frozenset", "FrozenSet") else "set"
            return SetType(name=name, elem=elem)

        if head == "tuple" or head == "Tuple":
            return TupleType(
                name="tuple",
                elems=tuple(parse_annotation(e) for e in idx_exprs),
            )

        if head in ("Optional", "typing.Optional"):
            if len(idx_exprs) == 1:
                # Phase 1: drop the optionality, keep the payload type.
                return parse_annotation(idx_exprs[0])
            return TYPE_DYN

        if head in ("Union", "typing.Union"):
            non_none = []
            for e in idx_exprs:
                parsed = parse_annotation(e)
                if isinstance(parsed, NoneType):
                    continue
                non_none.append(e)
            if len(non_none) == 1:
                return parse_annotation(non_none[0])
            return TYPE_DYN

        if head == "Callable":
            # Callable[[A, B], R] — best-effort.
            if len(idx_exprs) == 2:
                params_expr, ret_expr = idx_exprs
                if isinstance(params_expr, ListExpr):
                    params = tuple(parse_annotation(p) for p in params_expr.elems)
                else:
                    params = ()
                return FuncType(
                    name="callable",
                    params=params,
                    ret=parse_annotation(ret_expr),
                )
            return TYPE_DYN

        # Unknown generic — fall through to dynamic.
        return TYPE_DYN

    # Attribute annotations like ``typing.List[int]`` are not unwrapped
    # for Phase 1; treat as dynamic.
    return TYPE_DYN


# ---------------------------------------------------------------------------
# Type predicates and comparisons
# ---------------------------------------------------------------------------


def _type_name(ty: Type) -> str:
    try:
        return ty.name
    except AttributeError:
        return ""


def _class_type_module(ty: Type) -> str:
    try:
        module = ty.module
    except AttributeError:
        return ""
    return module or ""


def _class_type_fields(ty: Type):
    try:
        fields = ty.fields
    except AttributeError:
        return ()
    return fields or ()


def _class_type_bases(ty: Type):
    try:
        bases = ty.bases
    except AttributeError:
        return ()
    return bases or ()


def type_eq(a: Type, b: Type) -> bool:
    """Structural equality on ``Type`` instances.

    Keep this explicit instead of relying on dataclass ``__eq__``. The
    self-hosted runtime does not yet provide CPython-complete generated
    dunder equality for frozen dataclasses, and type inference must not
    depend on that dynamic path.
    """
    if a is b:
        return True
    if isinstance(a, RawPointerType) or isinstance(b, RawPointerType):
        return isinstance(a, RawPointerType) and isinstance(b, RawPointerType)
    primitive_name = (
        a.name == "int"
        or a.name == "float"
        or a.name == "bool"
        or a.name == "complex"
        or a.name == "str"
        or a.name == "bytes"
        or a.name == "bytearray"
        or a.name == "memoryview"
        or a.name == "None"
        or a.name == "dyn"
    )
    if a.name == b.name and primitive_name:
        return True
    if isinstance(a, IntType) or isinstance(b, IntType):
        if not (isinstance(a, IntType) and isinstance(b, IntType)):
            return False
        return a.name == b.name
    if isinstance(a, FloatType) or isinstance(b, FloatType):
        if not (isinstance(a, FloatType) and isinstance(b, FloatType)):
            return False
        return a.name == b.name
    if isinstance(a, ComplexType) or isinstance(b, ComplexType):
        return (
            isinstance(a, ComplexType)
            and isinstance(b, ComplexType)
            and a.name == b.name
        )
    if isinstance(a, BoolType) or isinstance(b, BoolType):
        return isinstance(a, BoolType) and isinstance(b, BoolType) and a.name == b.name
    if isinstance(a, NoneType) or isinstance(b, NoneType):
        return isinstance(a, NoneType) and isinstance(b, NoneType) and a.name == b.name
    if isinstance(a, StrType) or isinstance(b, StrType):
        return isinstance(a, StrType) and isinstance(b, StrType) and a.name == b.name
    if isinstance(a, BytesType) or isinstance(b, BytesType):
        return (
            isinstance(a, BytesType) and isinstance(b, BytesType) and a.name == b.name
        )
    if isinstance(a, ByteArrayType) or isinstance(b, ByteArrayType):
        return (
            isinstance(a, ByteArrayType)
            and isinstance(b, ByteArrayType)
            and a.name == b.name
        )
    if isinstance(a, MemoryViewType) or isinstance(b, MemoryViewType):
        return (
            isinstance(a, MemoryViewType)
            and isinstance(b, MemoryViewType)
            and a.name == b.name
        )
    if isinstance(a, DynType) or isinstance(b, DynType):
        return isinstance(a, DynType) and isinstance(b, DynType) and a.name == b.name
    if isinstance(a, ListType) or isinstance(b, ListType):
        if not (isinstance(a, ListType) and isinstance(b, ListType)):
            return False
        return a.name == b.name and type_eq(a.elem, b.elem)
    if isinstance(a, SetType) or isinstance(b, SetType):
        if not (isinstance(a, SetType) and isinstance(b, SetType)):
            return False
        return a.name == b.name and type_eq(a.elem, b.elem)
    if isinstance(a, ValueArrayType) or isinstance(b, ValueArrayType):
        if not (isinstance(a, ValueArrayType) and isinstance(b, ValueArrayType)):
            return False
        return a.name == b.name and a.length == b.length and type_eq(a.elem, b.elem)
    if isinstance(a, DictType) or isinstance(b, DictType):
        if not (isinstance(a, DictType) and isinstance(b, DictType)):
            return False
        return a.name == b.name and type_eq(a.key, b.key) and type_eq(a.value, b.value)
    if isinstance(a, TupleType) or isinstance(b, TupleType):
        if not (isinstance(a, TupleType) and isinstance(b, TupleType)):
            return False
        if a.name != b.name or len(a.elems) != len(b.elems):
            return False
        i = 0
        while i < len(a.elems):
            if not type_eq(a.elems[i], b.elems[i]):
                return False
            i += 1
        return True
    if isinstance(a, FuncType) or isinstance(b, FuncType):
        if not (isinstance(a, FuncType) and isinstance(b, FuncType)):
            return False
        if a.name != b.name or len(a.params) != len(b.params):
            return False
        i = 0
        while i < len(a.params):
            if not type_eq(a.params[i], b.params[i]):
                return False
            i += 1
        return type_eq(a.ret, b.ret)
    if isinstance(a, ClassType) or isinstance(b, ClassType):
        if not (isinstance(a, ClassType) and isinstance(b, ClassType)):
            return False
        a_name = _type_name(a)
        b_name = _type_name(b)
        a_fields = _class_type_fields(a)
        b_fields = _class_type_fields(b)
        a_bases = _class_type_bases(a)
        b_bases = _class_type_bases(b)
        if (
            a_name != b_name
            or _class_type_module(a) != _class_type_module(b)
            or len(a_fields) != len(b_fields)
            or len(a_bases) != len(b_bases)
        ):
            return False
        i = 0
        while i < len(a_fields):
            a_field_name, a_ty = a_fields[i]
            b_field_name, b_ty = b_fields[i]
            if a_field_name != b_field_name or not type_eq(a_ty, b_ty):
                return False
            i += 1
        i = 0
        while i < len(a_bases):
            if not type_eq(a_bases[i], b_bases[i]):
                return False
            i += 1
        return True
    return a.name == b.name


def is_numeric(t: Type) -> bool:
    """True if ``t`` participates in arithmetic promotion.

    ``bool`` counts as numeric because Python treats booleans as a
    subclass of ``int``.  ``DynType`` is *not* numeric — dynamic operands
    force a dynamic result.
    """
    return isinstance(t, (IntType, FloatType, BoolType, ComplexType))


def _is_int_like(t: Type) -> bool:
    return isinstance(t, (IntType, BoolType))


def value_join_type(a: Type, b: Type) -> Type:
    """The static type of one slot that holds either an ``a`` or a ``b`` value.

    List, dict, tuple and set literals, ``a if c else b`` and ``a or b``
    keep each operand's own object: ``[1, 2.5]`` holds an int and a float,
    ``1 if c else 2.5`` is the int 1 and ``[1, True]`` keeps the bool.
    Unlike :func:`common_type` (arithmetic promotion) a join never
    converts. Equal types stay; two ints or two floats keep their existing
    width/lane rules; any other pair is dynamic.
    """
    if type_eq(a, b):
        return a
    if isinstance(a, IntType) and isinstance(b, IntType):
        return common_type(a, b)
    if isinstance(a, FloatType) and isinstance(b, FloatType):
        return common_type(a, b)
    return TYPE_DYN


def common_type(a: Type, b: Type) -> Type:
    """Return the arithmetic-promotion result type for ``a`` and ``b``.

    Phase 1 rules:

    * ``int`` + ``int``    → ``int`` (width = max(a.width, b.width), signed if either is signed).
    * ``bool`` + ``bool``  → ``int`` (Python semantics: ``True + True == 2``).
    * ``int`` + ``float``  → ``float``.
    * ``float`` + ``float``→ ``float`` (wider width wins).
    * ``str`` + ``str``    → ``str``.
    * otherwise            → ``DynType``.
    """

    # Complex wins over every other number. Against anything else (str, a
    # container, a dynamic value) the result is dynamic: ``[1j, "x"]`` is
    # not a list of complex.
    if isinstance(a, ComplexType) or isinstance(b, ComplexType):
        if is_numeric(a) and is_numeric(b):
            return TYPE_COMPLEX
        return TYPE_DYN
    # Float wins over anything int-like.
    if isinstance(a, FloatType) and isinstance(b, FloatType):
        return FloatType(name="float", width=max(a.width, b.width))
    if isinstance(a, FloatType) and _is_int_like(b):
        return a
    if isinstance(b, FloatType) and _is_int_like(a):
        return b

    # Two int-likes.
    if _is_int_like(a) and _is_int_like(b):
        a_raw = isinstance(a, IntType) and a.name in ("pcc.i64", "pcc.u64")
        b_raw = isinstance(b, IntType) and b.name in ("pcc.i64", "pcc.u64")
        if a_raw or b_raw:
            # Raw machine lanes never arise from implicit promotion.  The
            # inference pass contextually types integer *literals* before it
            # calls ``common_type``; any remaining raw/Python-int or signed/
            # unsigned mixture requires an explicit conversion.
            if not (a_raw and b_raw) or a.name != b.name:
                return TYPE_DYN
            return a
        aw = getattr(a, "width", 64)
        bw = getattr(b, "width", 64)
        asg = getattr(a, "signed", True)
        bsg = getattr(b, "signed", True)
        return IntType(
            name="int",
            width=max(aw, bw),
            signed=asg or bsg,
        )

    # Strings.
    if isinstance(a, StrType) and isinstance(b, StrType):
        return TYPE_STR

    # Same nominal type (e.g. two NoneTypes, or identical class types).
    if type_eq(a, b):
        return a

    return TYPE_DYN


__all__ = [
    "TYPE_INT",
    "TYPE_I64",
    "TYPE_U64",
    "TYPE_FLOAT",
    "TYPE_COMPLEX",
    "TYPE_BOOL",
    "TYPE_NONE",
    "TYPE_STR",
    "TYPE_BYTES",
    "TYPE_BYTEARRAY",
    "TYPE_MEMORYVIEW",
    "TYPE_SET",
    "TYPE_FROZENSET",
    "TYPE_DYN",
    "PyFrontendError",
    "parse_annotation",
    "type_eq",
    "is_numeric",
    "common_type",
]
