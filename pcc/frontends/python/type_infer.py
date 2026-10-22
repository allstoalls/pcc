"""Annotation-driven type inference for the pcc Python frontend.

This pass walks a parsed :class:`pcc.frontends.python.py_ast.Module`, fills in
the ``ty`` field on every expression, and rewrites ``Arg``/``Assign``/
``FuncDef`` nodes whose annotations have been resolved from surface
``Expr`` form into first-class :class:`Type` instances.

The AST nodes are ``frozen=True`` dataclasses, so this pass never
mutates in place; instead it produces fresh nodes via
:func:`dataclasses.replace`.

Phase 1 scope (see ``docs/plans/python-frontend-plan.md`` section
"Phase 1"):

* Every ``FuncDef`` argument with an ``annotation`` gets that type;
  missing annotations default to ``DynType``.
* Return type comes from ``return_ty`` when present; otherwise ``DynType``.
* Local assignments use ``Assign.annotation`` when provided, else the
  inferred RHS type.
* Literals map to their native types.
* ``Name`` lookup walks the local scope, then params, then module
  globals, then builtins.
* Arithmetic ``BinOp`` uses :func:`pcc.frontends.python.types.common_type`.
  ``str + str`` stays ``str``; everything else that is not numeric or
  string falls back to ``DynType`` for Phase 1.
* ``Compare`` and ``BoolExpr`` always produce ``BoolType``.
* ``Call`` returns the callee's annotated return type when the callee
  is a known :class:`FuncDef`; otherwise ``DynType``.
* ``ListExpr`` uses the common type of its elements; empty list →
  ``list[dyn]``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from pcc.frontends.python.unsafe_intrinsic_policy import (
    UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS,
)

from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_ATTRS, L1_CODEGEN_HOST_METHODS, PROBE_POLICY_CONTEXTUAL_MIXIN, per_module_probe_policy
from pcc.frontends.python.export_meta import decode_type, encode_type, encode_type_memo
from pcc.frontends.python.py_ast import (
    RawPointerType,
    Arg,
    Assign,
    AugAssign,
    Attr,
    BinOp,
    BoolExpr,
    BoolLit,
    BoolType,
    Break,
    ByteArrayType,
    BytesLit,
    BytesType,
    Call,
    ClassDef,
    ClassType,
    ComplexLit,
    ComplexType,
    Compare,
    Continue,
    Delete,
    DictExpr,
    DictType,
    DynType,
    ExceptHandler,
    Expr,
    ExprStmt,
    FloatLit,
    FloatType,
    For,
    FuncDef,
    FuncType,
    Global,
    If,
    IfExpr,
    Import,
    ImportFrom,
    IntLit,
    IntType,
    Lambda,
    ListExpr,
    ListType,
    MemoryViewType,
    Module,
    Name,
    NoneLit,
    NoneType,
    SetType,
    Nonlocal,
    Pass,
    Raise,
    Return,
    Slice,
    SourceSpan,
    Stmt,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Try,
    Type,
    UnaryOp,
    ValueArrayType,
    ValueClassType,
    While,
    With,
)
from pcc.frontends.python.import_alternatives import import_fallbacks, import_statement_key
from pcc.frontends.python.raw_pointer_types import raw_pointer_annotation_names, resolve_raw_pointer_annotation
from pcc.frontends.python.types import (
    PyFrontendError,
    common_type,
    is_numeric,
    parse_annotation,
    type_eq,
    value_join_type,
)

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
TYPE_UNSAFE_I64X4: ValueClassType = ValueClassType(
    name="UnsafeI64x4",
    module="pcc.unsafe",
    fields=(
        ("first", TYPE_I64),
        ("second", TYPE_I64),
        ("third", TYPE_I64),
        ("fourth", TYPE_I64),
    ),
    bases=(),
    properties=(),
    valueclass=True,
    flattened=True,
    nullable_fields=False,
)
TYPE_SET: SetType = SetType(name="set", elem=TYPE_DYN)
TYPE_FROZENSET: SetType = SetType(name="frozenset", elem=TYPE_DYN)

_RAW_INT_NAMES = ("pcc.i64", "pcc.u64")


def _is_raw_int_type(ty: Type) -> bool:
    return isinstance(ty, IntType) and ty.name in _RAW_INT_NAMES


def _raw_int_constant_value(expr: Expr) -> tuple[bool, int]:
    """Return a source integer constant without treating variables as casts."""
    if isinstance(expr, IntLit):
        return (True, expr.value)
    if isinstance(expr, UnaryOp) and expr.op in ("+", "-", "~"):
        found, value = _raw_int_constant_value(expr.operand)
        if found:
            if expr.op == "-":
                value = -value
            elif expr.op == "~":
                value = ~value
            return (True, value)
    if isinstance(expr, BinOp) and expr.op in (
        "+",
        "-",
        "*",
        "//",
        "%",
        "&",
        "|",
        "^",
        "<<",
        ">>",
    ):
        lhs_found, lhs = _raw_int_constant_value(expr.lhs)
        rhs_found, rhs = _raw_int_constant_value(expr.rhs)
        if lhs_found and rhs_found:
            if expr.op == "+":
                return (True, lhs + rhs)
            if expr.op == "-":
                return (True, lhs - rhs)
            if expr.op == "*":
                return (True, lhs * rhs)
            if expr.op == "//" and rhs != 0:
                return (True, lhs // rhs)
            if expr.op == "%" and rhs != 0:
                return (True, lhs % rhs)
            if expr.op == "&":
                return (True, lhs & rhs)
            if expr.op == "|":
                return (True, lhs | rhs)
            if expr.op == "^":
                return (True, lhs ^ rhs)
            if expr.op == "<<" and 0 <= rhs <= 4096:
                return (True, lhs << rhs)
            if expr.op == ">>" and 0 <= rhs <= 4096:
                return (True, lhs >> rhs)
    return (False, 0)


def _raw_int_bounds(ty: IntType) -> tuple[int, int]:
    if ty.name == "pcc.u64":
        return (0, (1 << 64) - 1)
    return (-(1 << 63), (1 << 63) - 1)


def _validate_freestanding_plain_int_expr(expr: Expr) -> None:
    """Reject semantic ``int`` literals that cannot stay in the raw lane.

    Raw ``pcc.i64``/``pcc.u64`` contexts are applied during inference before
    this validation runs.  A remaining plain ``int`` literal therefore has no
    fixed-width contract.  Values outside signed i64 would otherwise reach the
    managed bignum constructor and only fail later in the freestanding IR
    publication guard.
    """
    if isinstance(expr, IntLit):
        if (
            isinstance(expr.ty, IntType)
            and expr.ty.name == "int"
            and (expr.value < -(1 << 63) or expr.value > (1 << 63) - 1)
        ):
            _raise_frontend_error(
                expr.span,
                f"freestanding ordinary Python int literal {expr.value} "
                "exceeds the proven i64 lane; use explicit pcc.i64 or pcc.u64",
                "use an in-range literal or annotate the machine value explicitly",
            )
        return
    if isinstance(expr, (BinOp, Compare)):
        _validate_freestanding_plain_int_expr(expr.lhs)
        _validate_freestanding_plain_int_expr(expr.rhs)
        return
    if isinstance(expr, BoolExpr):
        _validate_freestanding_plain_int_expr(expr.left)
        _validate_freestanding_plain_int_expr(expr.right)
        return
    if isinstance(expr, UnaryOp):
        _validate_freestanding_plain_int_expr(expr.operand)
        return
    if isinstance(expr, Call):
        _validate_freestanding_plain_int_expr(expr.func)
        for arg in expr.args:
            _validate_freestanding_plain_int_expr(arg)
        for _name, value in expr.kwargs:
            _validate_freestanding_plain_int_expr(value)
        return
    if isinstance(expr, Attr):
        _validate_freestanding_plain_int_expr(expr.obj)
        return
    if isinstance(expr, Subscript):
        _validate_freestanding_plain_int_expr(expr.obj)
        _validate_freestanding_plain_int_expr(expr.idx)
        return
    if isinstance(expr, Slice):
        for value in (expr.lo, expr.hi, expr.step):
            if value is not None:
                _validate_freestanding_plain_int_expr(value)
        return
    if isinstance(expr, (ListExpr, TupleExpr)):
        for value in expr.elems:
            _validate_freestanding_plain_int_expr(value)
        return
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            _validate_freestanding_plain_int_expr(key)
            _validate_freestanding_plain_int_expr(value)
        return
    if isinstance(expr, IfExpr):
        _validate_freestanding_plain_int_expr(expr.cond)
        _validate_freestanding_plain_int_expr(expr.then_e)
        _validate_freestanding_plain_int_expr(expr.else_e)
        return
    if isinstance(expr, Lambda):
        for arg in expr.params:
            if arg.default is not None:
                _validate_freestanding_plain_int_expr(arg.default)
        _validate_freestanding_plain_int_expr(expr.body)


def _validate_freestanding_plain_int_stmts(body: tuple[Stmt, ...]) -> None:
    for stmt in body:
        if isinstance(stmt, Assign):
            for target in stmt.targets:
                _validate_freestanding_plain_int_expr(target)
            _validate_freestanding_plain_int_expr(stmt.value)
        elif isinstance(stmt, AugAssign):
            _validate_freestanding_plain_int_expr(stmt.target)
            _validate_freestanding_plain_int_expr(stmt.value)
        elif isinstance(stmt, ExprStmt):
            _validate_freestanding_plain_int_expr(stmt.expr)
        elif isinstance(stmt, (If, While)):
            _validate_freestanding_plain_int_expr(stmt.cond)
            _validate_freestanding_plain_int_stmts(stmt.body)
            _validate_freestanding_plain_int_stmts(stmt.else_body)
        elif isinstance(stmt, For):
            _validate_freestanding_plain_int_expr(stmt.target)
            _validate_freestanding_plain_int_expr(stmt.iter)
            _validate_freestanding_plain_int_stmts(stmt.body)
            _validate_freestanding_plain_int_stmts(stmt.else_body)
        elif isinstance(stmt, Return) and stmt.value is not None:
            _validate_freestanding_plain_int_expr(stmt.value)
        elif isinstance(stmt, Raise):
            if stmt.exc is not None:
                _validate_freestanding_plain_int_expr(stmt.exc)
            if stmt.cause is not None:
                _validate_freestanding_plain_int_expr(stmt.cause)
        elif isinstance(stmt, Try):
            _validate_freestanding_plain_int_stmts(stmt.body)
            for handler in stmt.handlers:
                if handler.exc_type is not None:
                    _validate_freestanding_plain_int_expr(handler.exc_type)
                _validate_freestanding_plain_int_stmts(handler.body)
            _validate_freestanding_plain_int_stmts(stmt.else_body)
            _validate_freestanding_plain_int_stmts(stmt.finally_body)
        elif isinstance(stmt, With):
            for context, target in stmt.items:
                _validate_freestanding_plain_int_expr(context)
                if target is not None:
                    _validate_freestanding_plain_int_expr(target)
            _validate_freestanding_plain_int_stmts(stmt.body)
        elif isinstance(stmt, Delete):
            for target in stmt.targets:
                _validate_freestanding_plain_int_expr(target)
        elif isinstance(stmt, FuncDef):
            for arg in stmt.args:
                if arg.default is not None:
                    _validate_freestanding_plain_int_expr(arg.default)
            for decorator in stmt.decorators:
                _validate_freestanding_plain_int_expr(decorator)
            _validate_freestanding_plain_int_stmts(stmt.body)
        elif isinstance(stmt, ClassDef):
            for base in stmt.bases:
                _validate_freestanding_plain_int_expr(base)
            for _name, value in stmt.keywords:
                _validate_freestanding_plain_int_expr(value)
            for decorator in stmt.decorators:
                _validate_freestanding_plain_int_expr(decorator)
            _validate_freestanding_plain_int_stmts(stmt.body)


def _contextualize_raw_int_constant(expr: Expr, ty: IntType) -> Expr:
    """Give an in-range integer literal the surrounding raw lane type."""
    if isinstance(expr, IfExpr):
        then_e = _contextualize_raw_int_constant(expr.then_e, ty)
        else_e = _contextualize_raw_int_constant(expr.else_e, ty)
        if _is_raw_int_type(then_e.ty) and _is_raw_int_type(else_e.ty):
            return replace(expr, then_e=then_e, else_e=else_e, ty=ty)
        return expr
    found, value = _raw_int_constant_value(expr)
    if not found:
        return expr
    low, high = _raw_int_bounds(ty)
    if value < low or value > high:
        _raise_frontend_error(
            expr.span,
            f"integer literal {value} does not fit {ty.name}",
            f"use a value in the inclusive range [{low}, {high}]",
        )
    if isinstance(expr, BinOp):
        lhs = _contextualize_raw_int_constant(expr.lhs, ty)
        rhs = _contextualize_raw_int_constant(expr.rhs, ty)
        if _is_raw_int_type(lhs.ty) and _is_raw_int_type(rhs.ty):
            return replace(expr, lhs=lhs, rhs=rhs, ty=ty)
        return expr
    if isinstance(expr, UnaryOp):
        operand = expr.operand
        if not isinstance(operand, IntLit):
            operand = _contextualize_raw_int_constant(operand, ty)
        return replace(
            expr,
            operand=replace(operand, ty=ty),
            ty=ty,
        )
    return replace(expr, ty=ty)


def _contextualize_raw_int_operands(
    lhs: Expr,
    rhs: Expr,
    span: SourceSpan,
) -> tuple[Expr, Expr]:
    """Permit raw/literal arithmetic, reject implicit Python-int conversion."""
    lhs_raw = _is_raw_int_type(lhs.ty)
    rhs_raw = _is_raw_int_type(rhs.ty)
    if not lhs_raw and not rhs_raw:
        return (lhs, rhs)
    if lhs_raw and rhs_raw:
        if lhs.ty.name != rhs.ty.name:
            _raise_frontend_error(
                span,
                f"cannot mix {lhs.ty.name} and {rhs.ty.name} without an explicit conversion",
                "use one fixed-width signedness for the whole operation",
            )
        return (lhs, rhs)
    if lhs_raw and isinstance(rhs.ty, (IntType, BoolType)):
        typed_rhs = _contextualize_raw_int_constant(rhs, lhs.ty)
        if _is_raw_int_type(typed_rhs.ty):
            return (lhs, typed_rhs)
    if rhs_raw and isinstance(lhs.ty, (IntType, BoolType)):
        typed_lhs = _contextualize_raw_int_constant(lhs, rhs.ty)
        if _is_raw_int_type(typed_lhs.ty):
            return (typed_lhs, rhs)
    raw_ty = lhs.ty if lhs_raw else rhs.ty
    _raise_frontend_error(
        span,
        f"{raw_ty.name} does not implicitly convert ordinary Python int values",
        "annotate the other value with the same raw type or use an in-range literal",
    )
    return (lhs, rhs)

_CLASS_LOWERING_HOST_METHODS = (
    "_find_method_def",
    "_load_bases_array",
    "_normalize_native_class_bases",
    "declare_class",
    "declare_extern_class",
    "emit_class_attr_load",
    "emit_class_attr_store",
    "emit_class_statement_init",
    "emit_instantiate",
    "emit_isinstance",
    "emit_local_class_statement_init",
    "emit_methods",
    "emit_module_init",
    "emit_self_attr_load",
    "emit_self_attr_store",
    "emit_super_lookup",
    "lookup_class_attr",
    "lookup_field_index",
)


def _make_list_type(elem: Type) -> ListType:
    return ListType(name="list", elem=elem)


def _make_dict_type(key: Type, value: Type) -> DictType:
    return DictType(name="dict", key=key, value=value)


def _make_tuple_type(name: str, elems: tuple[Type, ...]) -> TupleType:
    return TupleType(name=name, elems=elems)


def _canonical_dyn_type(ty: object) -> Optional[Type]:
    """Canonicalize a DynType across compiled-module class boundaries.

    A self-hosted compiler can receive an AST/type object whose ``DynType``
    class was materialized by another compiled module.  Python-level
    ``isinstance(ty, DynType)`` is then not a reliable discriminator even
    though the wire variant and fields are valid.  Normalize that boundary
    once, rather than teaching individual inference rules to inspect
    ``DynType.name`` or keep syntax side tables.

    Older parser snapshots also encode ``set``/``frozenset`` annotations as
    ``DynType(name=...)``.  Promote those legacy encodings to the first-class
    ``SetType`` projection here.
    """
    if not isinstance(ty, DynType) and ty.__class__.__name__ != "DynType":
        return None
    name = getattr(ty, "name", "dyn") or "dyn"
    if name == "set":
        return TYPE_SET
    if name == "frozenset":
        return TYPE_FROZENSET
    return DynType(name=name)


def _annotation_or_none(node):
    try:
        return node.annotation
    except AttributeError:
        return None


def _tuple_elem_type(ty: TupleType) -> Type:
    if ty.elems:
        acc = ty.elems[0]
        for elem in ty.elems[1:]:
            acc = value_join_type(acc, elem)
        return acc
    return TYPE_DYN


def _list_type_elem(ty: Type) -> Optional[Type]:
    if isinstance(ty, ListType):
        return ty.elem
    try:
        name = ty.name
    except AttributeError:
        return None
    if name != "list":
        return None
    try:
        return ty.elem
    except AttributeError:
        return None


def _dict_type_parts(ty: Type) -> Optional[tuple[Type, Type]]:
    if isinstance(ty, DictType):
        return (ty.key, ty.value)
    try:
        name = ty.name
    except AttributeError:
        return None
    if name != "dict":
        return None
    try:
        key = ty.key
        value = ty.value
    except AttributeError:
        return None
    return (key, value)


def _tuple_from_iterable_type(ty: Type) -> TupleType:
    if isinstance(ty, TupleType):
        return ty
    list_elem = _list_type_elem(ty)
    if list_elem is not None:
        return TupleType(name="tuple_variadic", elems=(list_elem,))
    if isinstance(ty, StrType):
        return TupleType(name="tuple_variadic", elems=(TYPE_STR,))
    return TupleType(name="tuple_variadic", elems=(TYPE_DYN,))


def _tuple_concat_type(a: TupleType, b: TupleType) -> TupleType:
    if not a.elems and not b.elems:
        return TupleType(name="tuple", elems=())
    return TupleType(
        name="tuple_variadic",
        elems=(value_join_type(_tuple_elem_type(a), _tuple_elem_type(b)),),
    )


def _make_func_type(params: tuple[Type, ...], ret: Type) -> FuncType:
    return FuncType(name="callable", params=params, ret=ret)


def _is_none_type(ty: Type) -> bool:
    if isinstance(ty, NoneType):
        return True
    return ty.name == "None" or ty.name == "NoneType"


def _make_class_type(
    name: str,
    module: str,
    fields: tuple[tuple[str, Type], ...],
    bases: tuple[ClassType, ...],
    properties: tuple[tuple[str, Type], ...] = (),
    valueclass: bool = False,
) -> ClassType:
    return ClassType(
        name,
        module,
        fields,
        bases,
        properties,
        valueclass,
    )


# ---------------------------------------------------------------------------
# Builtin symbol table
#
# Phase 1 only recognises a tiny slice of builtins.  Each entry maps an
# identifier to the ``Type`` you get when the name appears in a value
# position (for callables that is a ``FuncType``).
# ---------------------------------------------------------------------------

_BUILTIN_TYPES: dict[str, Type] = {
    "True": TYPE_BOOL,
    "False": TYPE_BOOL,
    "None": TYPE_NONE,
    # Common builtin callables — Phase 1 treats them as dynamic so the
    # driver can route them through the runtime library.  Future phases
    # refine to concrete ``FuncType`` entries.
    "print": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_NONE),
    "len": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_INT),
    "range": FuncType(name="callable", params=(TYPE_INT,), ret=TYPE_DYN),
    "int": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_INT),
    "float": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_FLOAT),
    "str": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_STR),
    "bool": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_BOOL),
    "type": FuncType(
        name="callable",
        params=(TYPE_DYN,),
        ret=ClassType("type", "", (("__name__", TYPE_STR),), ()),
    ),
    "set": FuncType(
        name="callable",
        params=(TYPE_DYN,),
        ret=TYPE_SET,
    ),
    "globals": FuncType(
        name="callable",
        params=(),
        ret=DictType(name="dict", key=TYPE_STR, value=TYPE_DYN),
    ),
    "frozenset": FuncType(
        name="callable",
        params=(TYPE_DYN,),
        ret=TYPE_FROZENSET,
    ),
    "chr": FuncType(name="callable", params=(TYPE_INT,), ret=TYPE_STR),
    "abs": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_DYN),
    "min": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_DYN),
    "max": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_DYN),
    "sum": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_DYN),
    "slice": FuncType(name="callable", params=(TYPE_DYN,), ret=DynType(name="slice")),
    "__await__": FuncType(name="callable", params=(TYPE_DYN,), ret=TYPE_DYN),
}

# Pointer-producing intrinsics.  Outside runtime-port (freestanding) mode a
# raw C address is typed as ``int``: it then rides the exact/tagged integer
# lanes and can never reach the object refcount protocol as a pointer, which is
# what lets the runtime stop probing pointer provenance on every incref/decref
# (docs/investigations/pcc1-stage2-emit-throughput-and-memory.md, 2026-09-06).
# ``tag_int`` is excluded: it produces an object representation on purpose.
_POINTER_INTRINSICS: frozenset[str] = frozenset({
    "malloc", "calloc", "realloc", "cstr", "global_addr", "function_addr",
    "global_load_ptr", "ptr_add", "int_to_ptr", "null", "frame_address", "load_ptr", "memset",
    "memcpy", "memmove", "getcwd", "stack_alloc", "dynamic_library_open",
    "dynamic_library_open_global", "dynamic_library_symbol",
    "darwin_libsystem_symbol", "page_alloc", "va_arg_ptr", "va_cursor",
    "uname_field", "initial_environ", "call_ptr0", "call_ptr1", "call_ptr2",
    "call_ptr3", "call_ptr4", "call_ptr_ptr_i64", "call_ptr_ptr_ptr_i32",
    "call_ptr_ptr_ptr_i64_ptr", "call_ptr_i64_i64", "darwin_errno_location",
    "dynamic_library_close", "getenv", "va_start",
})


def _unsafe_intrinsic_return_type(ctx: "_InferCtx", name: str) -> Optional[Type]:
    """Table lookup plus the normal-mode raw-address-as-int projection."""
    ret_ty = _UNSAFE_INTRINSIC_RETURN_TYPES.get(name)
    if ret_ty is None:
        if name in _POINTER_INTRINSICS and not ctx.pointer_lane:
            return TYPE_INT
        return None
    if ctx.freestanding:
        if type_eq(ret_ty, TYPE_INT):
            return TYPE_I64
        return ret_ty
    if name in _POINTER_INTRINSICS and type_eq(ret_ty, TYPE_DYN) and not ctx.pointer_lane:
        return TYPE_INT
    return ret_ty


_UNSAFE_INTRINSIC_RETURN_TYPES: dict[str, Type] = {
    "malloc": TYPE_DYN,
    "cstr": TYPE_DYN,
    "global_addr": TYPE_DYN,
    "function_addr": TYPE_DYN,
    "global_load_ptr": TYPE_DYN,
    "global_store_ptr": TYPE_NONE,
    "abi_constant": TYPE_INT,
    "define_global_i8": TYPE_NONE,
    "define_global_i32": TYPE_NONE,
    "define_global_i64": TYPE_NONE,
    "define_global_header": TYPE_NONE,
    "define_global_ptr_null": TYPE_NONE,
    "define_thread_local_ptr_null": TYPE_NONE,
    "define_thread_local_i32": TYPE_NONE,
    "define_global_ptr_to_global": TYPE_NONE,
    "define_global_cstr": TYPE_NONE,
    "define_global_ptr_array": TYPE_NONE,
    "define_global_null_ptr_array": TYPE_NONE,
    "define_global_i32_array": TYPE_NONE,
    "define_global_i64_array": TYPE_NONE,
    "define_global_struct_words": TYPE_NONE,
    "calloc": TYPE_DYN,
    "realloc": TYPE_DYN,
    "free": TYPE_NONE,
    "ptr_add": TYPE_DYN,
    "ptr_diff": TYPE_INT,
    "int_to_ptr": TYPE_DYN,
    "ptr_to_int": TYPE_INT,
    "wrapping_mul_i64": TYPE_INT,
    "logical_shift_right_i64": TYPE_INT,
    "logical_shift_left_i64": TYPE_INT,
    "unsigned_div_i64": TYPE_INT,
    "unsigned_rem_i64": TYPE_INT,
    "unsigned_greater_i64": TYPE_BOOL,
    "mul_overflow_i64": TYPE_BOOL,
    "float_to_i64": TYPE_INT,
    "i64_to_float": TYPE_FLOAT,
    "f64_div": TYPE_FLOAT,
    "f64_signbit": TYPE_INT,
    "f64_bits": TYPE_INT,
    "call_c_abi": TYPE_NONE,
    "c_abi_sizeof": TYPE_INT,
    "c_abi_alignof": TYPE_INT,
    "f64_pair_make": TYPE_COMPLEX,
    "f64_pair_first": TYPE_FLOAT,
    "f64_pair_second": TYPE_FLOAT,
    "ptr_eq": TYPE_BOOL,
    "ptr_is_null": TYPE_BOOL,
    "is_tagged_int": TYPE_BOOL,
    "tag_int": TYPE_DYN,
    "untag_int": TYPE_INT,
    "load_i64": TYPE_INT,
    "load_i64x4": TYPE_UNSAFE_I64X4,
    "load_i64x4_strided": TYPE_UNSAFE_I64X4,
    "load_i32": TYPE_INT,
    "load_i8": TYPE_INT,
    "load_ptr": TYPE_DYN,
    "load_f64": TYPE_FLOAT,
    "store_i64": TYPE_NONE,
    "store_i32": TYPE_NONE,
    "store_i8": TYPE_NONE,
    "store_ptr": TYPE_NONE,
    "store_f64": TYPE_NONE,
    "memset": TYPE_DYN,
    "memcpy": TYPE_DYN,
    "memmove": TYPE_DYN,
    "write": TYPE_INT,
    "read": TYPE_INT,
    "close": TYPE_INT,
    "seek_file": TYPE_INT,
    "pwrite_file": TYPE_INT,
    "truncate_file": TYPE_INT,
    "open_readonly": TYPE_INT,
    "open_file": TYPE_INT,
    "open_file_flags": TYPE_INT,
    "mkdir_at": TYPE_INT,
    "rename_file": TYPE_INT,
    "chmod_file": TYPE_INT,
    "utime_file": TYPE_INT,
    "sync_file": TYPE_INT,
    "socket_open": TYPE_INT,
    "socket_connect": TYPE_INT,
    "socket_bind": TYPE_INT,
    "socket_listen": TYPE_INT,
    "socket_setsockopt": TYPE_INT,
    "socket_getsockopt": TYPE_INT,
    "fd_control": TYPE_INT,
    "file_flock": TYPE_INT,
    "file_lock_region": TYPE_INT,
    "eventfd_create": TYPE_INT,
    "socket_send": TYPE_INT,
    "socket_recv": TYPE_INT,
    "socket_accept": TYPE_INT,
    "socket_shutdown": TYPE_INT,
    "socket_sockname": TYPE_INT,
    "socket_peername": TYPE_INT,
    "poll_fd": TYPE_INT,
    "poll_readable_pair": TYPE_INT,
    "getcwd": TYPE_DYN,
    "readlink": TYPE_INT,
    "mkdir": TYPE_INT,
    "unlinkat": TYPE_INT,
    "uname": TYPE_INT,
    "uname_field": TYPE_DYN,
    "cpu_query": TYPE_INT,
    "clock_gettime": TYPE_INT,
    "nanosleep": TYPE_INT,
    "waitpid": TYPE_INT,
    "kill": TYPE_INT,
    "process_exit": TYPE_NONE,
    "spawn_process": TYPE_INT,
    "spawn_process_pipe": TYPE_INT,
    "spawn_process_redirect": TYPE_INT,
    "stack_alloc": TYPE_DYN,
    "strlen": TYPE_INT,
    "getenv": TYPE_DYN,
    "setenv": TYPE_INT,
    "unsetenv": TYPE_INT,
    "access": TYPE_INT,
    "stat_kind": TYPE_INT,
    "lstat_kind": TYPE_INT,
    "stat_mtime": TYPE_FLOAT,
    "stat_size": TYPE_INT,
    "is_symlink": TYPE_INT,
    "call_ptr1": TYPE_DYN,
    "call_ptr0": TYPE_DYN,
    "call_void_i32": TYPE_NONE,
    "call_void_ptr0": TYPE_NONE,
    "call_void_ptr1": TYPE_NONE,
    "call_void_ptr_i64_ptr": TYPE_NONE,
    "call_ptr2": TYPE_DYN,
    "call_ptr4": TYPE_DYN,
    "call_ptr3": TYPE_DYN,
    "call_i64_i64_ptr": TYPE_INT,
    "call_i32_ptr1": TYPE_INT,
    "call_i32_ptr_i64": TYPE_INT,
    "call_i32_ptr_i32": TYPE_INT,
    "call_i32_ptr_i32_i32": TYPE_INT,
    "call_i32_ptr_i32_i32_i32": TYPE_INT,
    "call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32": TYPE_INT,
    "call_i32_i32_ptr_i64": TYPE_INT,
    "call_i32_i64_i64_ptr": TYPE_INT,
    "call_i32_i64_i32_i64": TYPE_INT,
    "call_i64_ptr1": TYPE_INT,
    "call_i64_ptr2": TYPE_INT,
    "call_i64_ptr_i64_ptr": TYPE_INT,
    "call_i64_ptr_i64_i64": TYPE_INT,
    "call_variadic_i64_ptr_i64_ptr": TYPE_INT,
    "call_variadic_i64_ptr_i64_i64": TYPE_INT,
    "call_variadic_i32_ptr_i32_ptr": TYPE_INT,
    "call_variadic_i32_ptr_i32_i64": TYPE_INT,
    "call_i64_ptr_i64_ptr_i64": TYPE_INT,
    "call_i64_ptr_i64_i64_ptr": TYPE_INT,
    "call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool": TYPE_INT,
    "call_i64_ptr_ptr_ptr_ptr_ptr_bool": TYPE_INT,
    "dynamic_library_open": TYPE_DYN,
    "dynamic_library_open_global": TYPE_DYN,
    "dynamic_library_symbol": TYPE_DYN,
    "darwin_libsystem_symbol": TYPE_DYN,
    "dynamic_library_close": TYPE_INT,
    "kevent_call": TYPE_INT,
    "epoll_create1": TYPE_INT,
    "epoll_ctl": TYPE_INT,
    "epoll_wait": TYPE_INT,
    "atomic_load_i32": TYPE_INT,
    "atomic_load_i64": TYPE_INT,
    "atomic_store_i32": TYPE_NONE,
    "atomic_store_i64": TYPE_NONE,
    "atomic_rmw_i32": TYPE_INT,
    "atomic_rmw_i64": TYPE_INT,
    "atomic_cas_i32": TYPE_INT,
    "atomic_cas_i64": TYPE_INT,
    "atomic_fence": TYPE_NONE,
    "atomic_test_and_set": TYPE_INT,
    "atomic_clear": TYPE_NONE,
    "windows_full_path": TYPE_DYN,
    "windows_real_path": TYPE_DYN,
    "windows_tempdir_create": TYPE_INT,
    "windows_tempdir_remove_owned": TYPE_INT,
    "windows_tempdir_close_owned": TYPE_INT,
    "directory_open": TYPE_DYN,
    "directory_next": TYPE_DYN,
    "directory_entry_type": TYPE_INT,
    "directory_error": TYPE_INT,
    "directory_close": TYPE_INT,
    "syscall6": TYPE_INT,
    "linux_set_thread_pointer": TYPE_INT,
    "page_alloc": TYPE_DYN,
    "page_free": TYPE_INT,
    "va_arg_i64": TYPE_INT,
    "va_arg_i32": TYPE_INT,
    "va_arg_u32": TYPE_INT,
    "va_arg_ptr": TYPE_DYN,
    "va_arg_f64": TYPE_FLOAT,
    "va_cursor": TYPE_DYN,
    "va_end": TYPE_NONE,
}

# These zero-argument signatures do not need pcc.unsafe's trap provider.
# Keep the existing pointer-lane/freestanding projection in
# _unsafe_intrinsic_return_type; "pointer" is its unprojected dynamic type.
for _unsafe_name, _unsafe_result in UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS.items():
    if _unsafe_result == "int":
        _UNSAFE_INTRINSIC_RETURN_TYPES[_unsafe_name] = TYPE_INT
    elif _unsafe_result == "none":
        _UNSAFE_INTRINSIC_RETURN_TYPES[_unsafe_name] = TYPE_NONE
    else:
        _UNSAFE_INTRINSIC_RETURN_TYPES[_unsafe_name] = TYPE_DYN



# ---------------------------------------------------------------------------
# Scope plumbing
# ---------------------------------------------------------------------------


class _Scope:
    """Lexical scope chain for type lookup.

    Scopes are walked in order: local → enclosing params → module
    globals → builtins (builtins live as a fallback in ``_lookup``).
    """

    __slots__ = ("bindings", "parent", "class_objects")

    def __init__(self, parent: Optional["_Scope"] = None) -> None:
        self.bindings: dict[str, Type] = {}
        self.class_objects: set[str] = set()
        if parent:
            self.parent: Optional[_Scope] = parent
        else:
            self.parent = None

    def _find_local(self, name: str) -> int:
        if self.bindings.get(name) is not None:
            return 0
        return -1

    def define(self, name: str, ty: Type) -> None:
        self.bindings[name] = ty
        self.class_objects.discard(name)

    def update(self, name: str, ty: Type) -> None:
        """Update or insert; used for assignment re-typing."""
        self.define(name, ty)

    def lookup_local(self, name: str) -> Optional[Type]:
        return self.bindings.get(name)

    def lookup(self, name: str) -> Optional[Type]:
        scope: Optional[_Scope] = self
        while scope:
            found = scope.bindings.get(name)
            if found is not None:
                return found
            scope = scope.parent
        return None

    def is_class_object(self, name: str) -> bool:
        scope: Optional[_Scope] = self
        while scope:
            if name in scope.bindings:
                return name in scope.class_objects
            scope = scope.parent
        return False


# ---------------------------------------------------------------------------
# Inference context
# ---------------------------------------------------------------------------


class _InferCtx:
    """Shared state while inferring one module."""

    module: Module
    module_name: str
    freestanding: bool
    globals: _Scope
    func_types: dict[str, FuncType]
    external_exports: dict
    derived_class_map: dict
    unique_external_class_preload: object
    contextual_host_params: dict
    dataclasses_replace_aliases: set[str]
    import_fallbacks: dict
    functools_module_aliases: set[str]
    weakref_value_aliases: set[str]
    pcc_module_aliases: set[str]
    pcc_i64_buffer_aliases: set[str]
    unsafe_intrinsic_aliases: set[str]
    pcc_guarded_i64_dot_aliases: set[str]
    pcc_guarded_loop_counter_aliases: set[str]
    class_types: dict[str, ClassType]
    module_attr_types: dict[str, Type]
    annotation_module_bindings: dict[str, str]
    _l1_codegen_host_type: Optional[ClassType]
    _preload_dependency_modules: list[str]
    _record_preload_dependencies: bool
    _attribute_interceptors: Optional[set[str]]

    def __init__(
        self,
        module: Module,
        external_exports: Optional[dict] = None,
        derived_class_map: Optional[dict] = None,
        unique_external_class_preload=None,
        contextual_host_params: Optional[dict] = None,
    ) -> None:
        self.module = module
        try:
            self.module_name = module.name or ""
        except AttributeError:
            self.module_name = ""
        self.freestanding = False
        self.runtime_port = False
        for module_stmt in module.body:
            if not isinstance(module_stmt, Assign):
                continue
            if not isinstance(module_stmt.value, BoolLit) or not module_stmt.value.value:
                continue
            for target in module_stmt.targets:
                if isinstance(target, Name) and target.ident == "__pcc_freestanding__":
                    self.freestanding = True
                if isinstance(target, Name) and target.ident == "__pcc_runtime_port__":
                    self.runtime_port = True
        # Pointer-lane modules (freestanding kernels and runtime ports) keep
        # raw pointers as pointer values; application modules type raw
        # addresses as ``int`` so they can never enter the object protocol.
        self.pointer_lane = self.freestanding or self.runtime_port
        # Module-level globals (functions, top-level vars).
        self.globals: _Scope = _Scope(parent=None)
        # Map from function name to its (possibly refined) ``FuncType``.
        self.func_types: dict[str, FuncType] = {}
        # Multi-file compile: ``{dotted_mod: {name: export_info}}``
        # where ``export_info`` matches the pipeline pre-pass shape
        # (kind, param_types, return_ty, class metadata). Consulted by
        # ImportFrom handling so cross-module function/class types
        # flow into this module's scope at inference time rather than
        # collapsing to DynType.
        self.external_exports = external_exports or {}
        # Multi-file compile: ``{base_class_name: (derived_module,
        # derived_class_name)}``. When a class C in this module is the
        # sole base of some derived class D anywhere in the closure,
        # methods on C are inferred with ``self_ty=D`` so cross-module
        # mixin patterns (``class NativeXxxMixin: def m(self):
        # self.builder. ...``) resolve fields against D's full schema
        # instead of C's empty one. Built once in the multi-file
        # pipeline and shared across every module's _InferCtx.
        self.derived_class_map = derived_class_map or {}
        self.unique_external_class_preload = unique_external_class_preload
        self.contextual_host_params = contextual_host_params or {}
        # Module-level type aliases (``Instruction = dict[Engine, list]``,
        # ``Engine = Literal[...]``). An annotation naming one of these
        # must resolve to the aliased type, not to a phantom
        # ``ClassType`` — codegen would otherwise emit instance-field
        # access on plain dict/list values. Seeded by
        # ``_prepopulate_module_scope``.
        self.type_aliases: dict[str, Type] = {}
        self._alias_resolving: set[str] = set()
        # ``from dataclasses import replace as ...`` is common across
        # the frontend passes. Track the local aliases explicitly so
        # call-result inference can preserve the first argument's type
        # instead of collapsing the whole expression to DynType.
        self.dataclasses_replace_aliases: set[str] = set()
        # ``except ImportError`` alternatives must not replace a try-body
        # import that resolves in the closure: only the try import runs.
        self.import_fallbacks: dict = import_fallbacks(module.body)
        # Syntax-level module aliases stay reliable in a self-hosted
        # inference pass even when a recursive provider export temporarily
        # gives ``functools.partial`` a ClassType.  The lowered value is a
        # runtime PyFuncObject, not an instance of the provider's scaffold
        # ``class partial``.
        self.functools_module_aliases: set[str] = set()
        # ``pcc.extern`` is a compile-time declaration surface.  Track the
        # imported factory and C type-marker aliases so an ``extern(...)``
        # binding can carry the ABI-compatible return type into ordinary
        # call inference.  Codegen already keeps the equivalent alias map in
        # ``_extern_bindings``; without this frontend mirror a libm call emits
        # ``double`` IR while the typed AST still claims it is a class object.
        self.extern_factory_aliases: set[str] = set()
        # Local names bound to ``pcc.extern.ExternFn``.  Used as an annotation
        # it is a declaration marker like ``extern`` itself, not the result
        # type of calling the declared symbol.
        self.extern_fn_type_aliases: set[str] = set()
        self.extern_ctype_aliases: dict[str, str] = {}
        self.raw_pointer_annotation_names = raw_pointer_annotation_names(module)
        # ``from weakref import ref/proxy as ...`` names are identity
        # observers just like ``weakref.ref``/``weakref.proxy``. Track
        # only imports resolved to the real weakref module so ordinary
        # user functions named ``ref`` are not treated specially.
        self.weakref_value_aliases: set[str] = set()
        self.pcc_module_aliases: set[str] = set()
        self.pcc_i64_buffer_aliases: set[str] = set()
        self.unsafe_intrinsic_aliases: set[str] = set()
        self.pcc_guarded_i64_dot_aliases: set[str] = set()
        self.pcc_guarded_loop_counter_aliases: set[str] = set()
        # Locals of the function currently being inferred that receive a
        # 1-arg ``d.setdefault(k)`` call somewhere in its body. CPython
        # inserts ``None`` for a missing key, so an *inferred* scalar-valued
        # dict binding for such a name must widen its value type to ``dyn``
        # (annotated bindings are the user's contract and stay untouched).
        # Set/restored per-FuncDef in ``_infer_funcdef``.
        self.setdefault_none_widen_names: set = set()
        self.class_types: dict[str, ClassType] = {}
        # ``alias.NAME`` -> type of a provider's str global, as
        # ``from module import NAME`` already binds it.
        self.module_attr_types: dict[str, Type] = {}
        from pcc.frontends.python.pipeline_exports import annotation_module_bindings
        self.annotation_module_bindings = annotation_module_bindings(module)
        self._l1_codegen_host_type: Optional[ClassType] = None
        self._preload_dependency_modules = []
        self._record_preload_dependencies = False
        self._attribute_interceptors = None

    # -- helpers -----------------------------------------------------------

    def register_class_type(self, local_name: str, ty: ClassType) -> None:
        """Register a schema-bearing class type under local and stable names."""
        self.class_types[local_name] = ty
        self.class_types[ty.name] = ty
        ty_module = _class_type_module(ty)
        if ty_module:
            self.class_types[f"{ty_module}.{ty.name}"] = ty

    def l1_codegen_host_type(self) -> ClassType:
        """Synthetic type for helper functions that receive L1CodeGen host.

        This is intentionally opt-in via ``contextual_host_params``. It is
        the type-inference half of contextual host extraction: helpers can
        see ``host._fresh`` / ``host.builder`` as known fields instead of
        collapsing to ``DynType`` immediately. Codegen direct host calls are
        a separate step.
        """
        cached = self._l1_codegen_host_type
        if cached is not None:
            return cached

        class_lowering_ty = _make_class_type(
            "ClassLowering",
            "pcc.frontends.python.codegen.class_gen",
            (),
            (),
        )
        ir_builder_ty = _make_class_type(
            "IRBuilder",
            "pcc.ir.ir",
            (),
            (),
        )
        method_returns = {
            "_fresh": TYPE_STR,
            "_ir_scaffold_enabled": TYPE_BOOL,
            "_class_is_subclass": TYPE_BOOL,
        }
        attr_types = {
            "builder": ir_builder_ty,
            "class_lowering": class_lowering_ty,
        }
        fields: list[tuple[str, Type]] = []
        for attr_name in L1_CODEGEN_HOST_ATTRS:
            fields.append((attr_name, attr_types.get(attr_name, TYPE_DYN)))
        for method_name in L1_CODEGEN_HOST_METHODS:
            fields.append(
                (
                    method_name,
                    _make_func_type(
                        (TYPE_DYN,), method_returns.get(method_name, TYPE_DYN)
                    ),
                )
            )
        host_ty = _make_class_type(
            "L1CodeGen",
            "pcc.frontends.python.codegen.layer1",
            tuple(fields),
            (),
        )
        self._l1_codegen_host_type = host_ty
        self.register_class_type("L1CodeGen", host_ty)
        return host_ty

    def resolve_annotation(self, ann: object) -> Type:
        """Normalise an ``annotation`` field into a ``Type``.

        Parser implementations may attach either a ``Type`` directly (if
        they already resolved the annotation) or an ``Expr`` describing
        the raw annotation AST.  We accept both.
        """
        if ann is None:
            return TYPE_DYN
        canonical_dyn = _canonical_dyn_type(ann)
        if canonical_dyn is not None:
            return canonical_dyn
        if isinstance(ann, Type):
            # During bootstrap, parser variants can sometimes emit the raw
            # ``Type`` base object for annotations that should normally be
            # ``IntType``/``ClassType``/etc. Treat the bare base as
            # equivalent to unknown annotation and keep the check permissive.
            if ann.__class__ is Type:
                return TYPE_DYN
            # Some bootstrap snapshots materialize a shadow ``Type`` class
            # under a different module and pass it through as an annotation
            # object with name ``Type``. That class name collides with the
            # semantic base type and would otherwise fail return-checking.
            if ann.__class__.__name__ == "Type" and ann.name == "Type":
                return TYPE_DYN
            resolved = self.resolve_type_refs(ann)
            # If the parser produced an unnamed type shim, treat it as
            # dynamic rather than a hard annotation failure.
            if isinstance(resolved, Type) and not resolved.name:
                return TYPE_DYN
            return _validate_value_array_type(resolved, None)
        if isinstance(ann, Expr):
            return _validate_value_array_type(
                self.resolve_type_refs(parse_annotation(ann)), ann.span
            )
        # Unknown annotation payload — be defensive, don't crash.
        return TYPE_DYN

    def resolve_type_refs(self, ty: Type) -> Type:
        """Resolve ``ClassType`` refs inside annotations.

        Both parsers can preserve an unknown annotation name as
        ``ClassType(name, fields=())`` before inference has seen the
        corresponding class body. Once the module class table exists,
        replace those shells with the schema-bearing class type and
        recurse through container annotations.
        """
        if isinstance(ty, ClassType):
            pointer_ty = resolve_raw_pointer_annotation(ty, self.raw_pointer_annotation_names)
            if isinstance(pointer_ty, RawPointerType):
                return pointer_ty
            ty_module = _class_type_module(ty)
            ty_fields = _class_type_fields(ty)
            ty_bases = _class_type_bases(ty)
            if not ty_module and not ty_fields and not ty_bases:
                if (
                    ty.name == "L1CodeGen"
                    and _ctx_module_name(self) == "pcc.frontends.python.codegen.class_gen"
                ):
                    return self.l1_codegen_host_type()
                if ty.name == "list":
                    return _make_list_type(TYPE_DYN)
                if ty.name == "dict":
                    return _make_dict_type(TYPE_DYN, TYPE_DYN)
                if ty.name == "tuple":
                    return _make_tuple_type("tuple_variadic", (TYPE_DYN,))
                if ty.name == "bytes":
                    return TYPE_BYTES
                if ty.name == "bytearray":
                    return TYPE_BYTEARRAY
                if ty.name == "memoryview":
                    return TYPE_MEMORYVIEW
                if ty.name == "set":
                    return TYPE_SET
                if ty.name == "frozenset":
                    return TYPE_FROZENSET
            if ty_module:
                found = self.class_types.get(f"{ty_module}.{ty.name}")
                if found is not None and (
                    _class_type_module(found) == ty_module
                    or ty_module.split(".", 1)[0] in self.annotation_module_bindings
                ):
                    return found
            else:
                # A qualified annotation identifies its owner. A missing
                # module alias cannot be repaired by an unrelated local or
                # uniquely preloaded class with the same leaf name.
                found = self.class_types.get(ty.name)
                if found is not None:
                    return found
            if not ty_module and not ty_fields and not ty_bases:
                alias = self.type_aliases.get(ty.name)
                if alias is not None and ty.name not in self._alias_resolving:
                    self._alias_resolving.add(ty.name)
                    try:
                        return self.resolve_type_refs(alias)
                    finally:
                        self._alias_resolving.discard(ty.name)
            fields = tuple(
                (name, self.resolve_type_refs(field_ty)) for name, field_ty in ty_fields
            )
            bases = tuple(self.resolve_type_refs(base) for base in ty_bases)
            if fields == ty_fields and bases == ty_bases:
                return ty
            return _make_class_type(ty.name, ty_module, fields, bases)
        if isinstance(ty, ListType):
            elem = self.resolve_type_refs(ty.elem)
            if elem == ty.elem:
                return ty
            return _make_list_type(elem)
        if isinstance(ty, ValueArrayType):
            elem = self.resolve_type_refs(ty.elem)
            if elem == ty.elem:
                return ty
            return ValueArrayType(name=ty.name, elem=elem, length=ty.length)
        if isinstance(ty, DictType):
            key = self.resolve_type_refs(ty.key)
            value = self.resolve_type_refs(ty.value)
            if key == ty.key and value == ty.value:
                return ty
            return _make_dict_type(key, value)
        if isinstance(ty, TupleType):
            elems = tuple(self.resolve_type_refs(e) for e in ty.elems)
            if elems == ty.elems:
                return ty
            return _make_tuple_type(ty.name, elems)
        if isinstance(ty, FuncType):
            params = tuple(self.resolve_type_refs(p) for p in ty.params)
            ret = self.resolve_type_refs(ty.ret)
            if params == ty.params and ret == ty.ret:
                return ty
            return _make_func_type(params, ret)
        return ty

    def lookup_name(self, scope: _Scope, ident: str) -> Type:
        """Resolve a bare name, falling through to builtins."""
        found = scope.lookup(ident)
        if found is not None:
            return found
        # Module globals are reachable via the scope chain, so we only
        # need the builtin table here.
        builtin = _BUILTIN_TYPES.get(ident)
        if builtin is not None:
            return builtin
        return TYPE_DYN


# ---------------------------------------------------------------------------
# Expression inference
#
# Every helper returns a *new* expression node whose ``ty`` field has
# been filled in (or replaced with a more precise type).
# ---------------------------------------------------------------------------


def _with_ty(node: Expr, ty: Type) -> Expr:
    """Return ``node`` with its ``ty`` field replaced by ``ty``."""
    return replace(node, ty=ty)


def _name_ident(node: object) -> Optional[str]:
    """Return identifier from a Name node across AST snapshot variants."""
    ident = getattr(node, "ident", None)
    if ident is None:
        ident = getattr(node, "id", None)
    return ident


def _ctx_module_name(ctx: _InferCtx) -> str:
    try:
        return ctx.module_name or ""
    except AttributeError:
        pass
    try:
        return ctx.module.name or ""
    except AttributeError:
        return ""


def _flatten_bool_expr(expr: BoolExpr) -> tuple[Expr, ...]:
    op = expr.op
    values: list[Expr] = []
    stack: list[Expr] = [expr]
    while stack:
        cur = stack.pop()
        if isinstance(cur, BoolExpr) and cur.op == op:
            stack.append(cur.right)
            stack.append(cur.left)
        else:
            values.append(cur)
    return tuple(values)


def _bool_result_type(left_ty: Type, right_ty: Type) -> Type:
    result_ty = value_join_type(left_ty, right_ty)
    if isinstance(left_ty, BoolType) and isinstance(right_ty, BoolType):
        return TYPE_BOOL
    return result_ty


def _build_balanced_typed_bool_expr(
    span: SourceSpan,
    op: str,
    values: tuple[Expr, ...],
) -> Expr:
    if len(values) == 1:
        return values[0]
    mid = len(values) // 2
    left = _build_balanced_typed_bool_expr(span, op, values[:mid])
    right = _build_balanced_typed_bool_expr(span, op, values[mid:])
    return BoolExpr(
        span,
        _bool_result_type(left.ty, right.ty),
        op,
        left,
        right,
    )


def _is_walrus_sentinel_call(expr: Expr) -> bool:
    return (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and _name_ident(expr.func) in ("_walrus", "__walrus__")
        and len(expr.args) == 2
    )


def _infer_walrus_assignment_target(
    ctx: _InferCtx,
    scope: _Scope,
    target: Expr,
    bind_ty: Type,
) -> Expr:
    """Type one real or chained walrus target from its already-typed RHS."""
    if isinstance(target, Name):
        ident = _name_ident(target)
        if ident is not None:
            scope.update(ident, bind_ty)
        return _with_ty(target, bind_ty)
    if _is_walrus_sentinel_call(target):
        # ``a = b = c`` represents the hidden targets as a sentinel tree.
        # Both arguments here are targets; neither is a value expression.
        left = _infer_walrus_assignment_target(
            ctx,
            scope,
            target.args[0],
            bind_ty,
        )
        right = _infer_walrus_assignment_target(
            ctx,
            scope,
            target.args[1],
            bind_ty,
        )
        return replace(
            target,
            func=_with_ty(target.func, TYPE_DYN),
            args=(left, right),
            ty=bind_ty,
        )
    return _infer_expr(ctx, scope, target)


def _infer_expr(ctx: _InferCtx, scope: _Scope, expr: Expr) -> Expr:
    # Literals -----------------------------------------------------------
    if isinstance(expr, IntLit):
        return _with_ty(expr, TYPE_INT)
    if isinstance(expr, FloatLit):
        return _with_ty(expr, TYPE_FLOAT)
    if isinstance(expr, ComplexLit):
        return _with_ty(expr, TYPE_COMPLEX)
    if isinstance(expr, BoolLit):
        return _with_ty(expr, TYPE_BOOL)
    if isinstance(expr, NoneLit):
        return _with_ty(expr, TYPE_NONE)
    if isinstance(expr, StrLit):
        return _with_ty(expr, TYPE_STR)
    if isinstance(expr, BytesLit):
        return _with_ty(expr, TYPE_BYTES)

    # Name lookup --------------------------------------------------------
    if isinstance(expr, Name):
        ident = _name_ident(expr)
        if ident is None:
            _raise_frontend_error(
                expr.span,
                "internal name node missing identifier",
                "upgrade the parser/frontend AST to use ident field",
            )
        scope_cur: Optional[_Scope] = scope
        while scope_cur:
            found = scope_cur.bindings.get(ident)
            if found is not None:
                return _with_ty(expr, found)
            scope_cur = scope_cur.parent
        builtin = _BUILTIN_TYPES.get(ident)
        if builtin is not None:
            return _with_ty(expr, builtin)
        return _with_ty(expr, TYPE_DYN)

    # Binary arithmetic --------------------------------------------------
    if isinstance(expr, BinOp):
        lhs = _infer_expr(ctx, scope, expr.lhs)
        rhs = _infer_expr(ctx, scope, expr.rhs)
        lhs, rhs = _contextualize_raw_int_operands(lhs, rhs, expr.span)
        op = expr.op
        ty = _binop_result(op, lhs.ty, rhs.ty, expr.span)
        return replace(expr, lhs=lhs, rhs=rhs, ty=ty)

    # Unary --------------------------------------------------------------
    if isinstance(expr, UnaryOp):
        operand = _infer_expr(ctx, scope, expr.operand)
        op = expr.op
        if op == "not":
            ty: Type = TYPE_BOOL
        elif op == "~":
            ty = operand.ty if isinstance(operand.ty, (IntType, BoolType)) else TYPE_DYN
            if isinstance(operand.ty, BoolType):
                ty = TYPE_INT
        else:  # "+" / "-"
            if is_numeric(operand.ty):
                # Promote bool to int under unary +/-.
                ty = TYPE_INT if isinstance(operand.ty, BoolType) else operand.ty
            else:
                ty = TYPE_DYN
        return replace(expr, operand=operand, ty=ty)

    # Comparisons + boolean ops --------------------------------------------
    if isinstance(expr, Compare):
        lhs = _infer_expr(ctx, scope, expr.lhs)
        rhs = _infer_expr(ctx, scope, expr.rhs)
        if expr.op not in ("is", "is not", "in", "not in"):
            lhs, rhs = _contextualize_raw_int_operands(lhs, rhs, expr.span)
        op = expr.op
        if op in ("is", "is not") and (
            _is_valueclass_type(lhs.ty) or _is_valueclass_type(rhs.ty)
        ):
            _raise_frontend_error(
                expr.span,
                "identity comparison is not supported for valueclass payloads in strict mode",
                "compare valueclass fields with == or explicitly box before observing identity",
            )
        return replace(expr, lhs=lhs, rhs=rhs, ty=TYPE_BOOL)
    if isinstance(expr, BoolExpr):
        raw_values = _flatten_bool_expr(expr)
        typed_values = tuple(_infer_expr(ctx, scope, v) for v in raw_values)
        return _build_balanced_typed_bool_expr(expr.span, expr.op, typed_values)

    # Calls --------------------------------------------------------------
    if isinstance(expr, Call):
        if expr.is_set_literal:
            # The parser's container syntax is independent of a user binding
            # named ``set``. Do not resolve or invoke the synthetic callee.
            new_args = tuple(_infer_expr(ctx, scope, arg) for arg in expr.args)
            elem_ty = TYPE_DYN
            if len(new_args) == 1:
                elem_ty = _element_type_of(ctx.resolve_type_refs(new_args[0].ty))
            return replace(
                expr, func=_with_ty(expr.func, TYPE_DYN), args=new_args,
                ty=SetType(name="set", elem=elem_ty),
            )
        if _is_walrus_sentinel_call(expr):
            # The lift encodes both ``x := rhs`` and chained assignment with
            # a Dyn-typed sentinel.  Infer the RHS first (Python evaluation
            # order), bind every hidden target to that semantic type, and
            # make the expression itself return the same type.
            value = _infer_expr(ctx, scope, expr.args[1])
            target = _infer_walrus_assignment_target(
                ctx,
                scope,
                expr.args[0],
                value.ty,
            )
            return replace(
                expr,
                func=_with_ty(expr.func, TYPE_DYN),
                args=(target, value),
                ty=value.ty,
            )
        callee = _infer_expr(ctx, scope, expr.func)
        comprehension_args = _infer_comprehension_args(ctx, scope, expr)
        if comprehension_args is not None:
            new_args = comprehension_args
        else:
            new_args = tuple(_infer_expr(ctx, scope, a) for a in expr.args)
        new_kwargs = tuple((k, _infer_expr(ctx, scope, v)) for (k, v) in expr.kwargs)
        callee_ident = _name_ident(expr.func) if isinstance(expr.func, Name) else None
        if ctx.freestanding and callee_ident in ctx.unsafe_intrinsic_aliases:
            new_args = tuple(
                _contextualize_raw_int_constant(arg, TYPE_I64)
                for arg in new_args
            )
        if isinstance(callee.ty, FuncType):
            contextual_args: list[Expr] = []
            for index, arg in enumerate(new_args):
                param_ty = (
                    callee.ty.params[index]
                    if index < len(callee.ty.params)
                    else TYPE_DYN
                )
                if _is_raw_int_type(param_ty):
                    arg = _contextualize_raw_int_constant(arg, param_ty)
                    if (
                        isinstance(arg.ty, (IntType, BoolType))
                        and not _is_raw_int_type(arg.ty)
                    ):
                        _raise_frontend_error(
                            arg.span,
                            f"argument {index + 1} for {param_ty.name} is an ordinary Python int",
                            "annotate the argument with the same raw type or pass an in-range literal",
                        )
                contextual_args.append(arg)
            new_args = tuple(contextual_args)
        value_array_ty = _value_array_type_from_surface(ctx, expr.func)
        if value_array_ty is not None:
            if new_kwargs:
                _raise_frontend_error(
                    expr.span,
                    "pcc.array construction does not accept keyword arguments",
                    "pass exactly the declared number of positional valueclass elements",
                )
            if len(new_args) != value_array_ty.length:
                _raise_frontend_error(
                    expr.span,
                    f"pcc.array expects exactly {value_array_ty.length} elements",
                    "make the constructor argument count match the declared length",
                )
            for index, arg in enumerate(new_args):
                arg_ty = ctx.resolve_type_refs(arg.ty)
                if not type_eq(arg_ty, value_array_ty.elem):
                    _raise_frontend_error(
                        arg.span,
                        f"pcc.array element {index + 1} has type {arg_ty.name}, "
                        f"expected {value_array_ty.elem.name}",
                        "all elements must use the exact declared valueclass type",
                    )
            return replace(
                expr,
                func=callee,
                args=new_args,
                kwargs=new_kwargs,
                ty=value_array_ty,
            )
        i64_buffer_ty = _i64_buffer_type_from_surface(ctx, expr.func)
        if i64_buffer_ty is not None:
            length = _i64_buffer_length_from_type(i64_buffer_ty)
            if new_kwargs:
                _raise_frontend_error(
                    expr.span,
                    "pcc.i64_buffer construction does not accept keyword arguments",
                    "pass exactly the declared number of positional int elements",
                )
            if len(new_args) != length:
                _raise_frontend_error(
                    expr.span,
                    f"pcc.i64_buffer[{length}] expects exactly {length} elements",
                    "make the constructor argument count match its literal length",
                )
            for index, arg in enumerate(new_args):
                if not isinstance(ctx.resolve_type_refs(arg.ty), IntType):
                    _raise_frontend_error(
                        arg.span,
                        f"pcc.i64_buffer element {index + 1} must be an exact int",
                        "convert the value to int before constructing the typed buffer",
                    )
            return replace(
                expr,
                func=callee,
                args=new_args,
                kwargs=new_kwargs,
                ty=i64_buffer_ty,
            )

        pcc_intrinsic = _pcc_intrinsic_call_kind(ctx, expr.func)
        if pcc_intrinsic == "guarded_i64_dot":
            if new_kwargs or len(new_args) != 2:
                _raise_frontend_error(
                    expr.span,
                    "pcc.guarded_i64_dot expects two positional typed buffers",
                    "pass two pcc.i64_buffer[N] values with the same N",
                )
            left_n = _i64_buffer_length_from_type(new_args[0].ty)
            right_n = _i64_buffer_length_from_type(new_args[1].ty)
            if left_n < 1 or right_n < 1 or left_n != right_n:
                _raise_frontend_error(
                    expr.span,
                    "pcc.guarded_i64_dot requires matching pcc.i64_buffer[N] operands",
                    "construct both operands with the same literal buffer length",
                )
            return replace(
                expr,
                func=callee,
                args=new_args,
                kwargs=new_kwargs,
                ty=IntType(name="int"),
            )
        if pcc_intrinsic == "guarded_loop_counter":
            valid_counters = (
                "candidate",
                "guard_hit",
                "guard_miss",
                "overflow",
                "scalar_fallback",
                "fast_result",
            )
            if (
                new_kwargs
                or len(new_args) != 1
                or not isinstance(new_args[0], StrLit)
                or new_args[0].value not in valid_counters
            ):
                _raise_frontend_error(
                    expr.span,
                    "pcc.guarded_loop_counter requires one known counter literal",
                    "use candidate, guard_hit, guard_miss, overflow, "
                    "scalar_fallback, or fast_result",
                )
            return replace(
                expr,
                func=callee,
                args=new_args,
                kwargs=new_kwargs,
                ty=IntType(name="int"),
            )
        # Valhalla projection rule: value projections are identity-free,
        # and weak references are an identity-lifetime observation (the
        # CPython analogue: ``weakref.ref(3)`` raises TypeError). A box
        # created at the call boundary would have an unpredictable
        # lifetime, so reject statically-known valueclass arguments at
        # compile time like the ``is`` diagnostic above.
        if (
            new_args
            and _is_valueclass_type(new_args[0].ty)
            and (
                (
                    isinstance(callee, Attr)
                    and callee.name in ("ref", "proxy")
                    and isinstance(callee.obj, Name)
                    and _name_ident(callee.obj) == "weakref"
                )
                or (
                    isinstance(callee, Name)
                    and (_name_ident(callee) or "") in ctx.weakref_value_aliases
                )
            )
        ):
            _raise_frontend_error(
                expr.span,
                "cannot create a weak reference to a valueclass payload",
                "valueclass payloads are identity-free; use an ordinary "
                "class when weak referencing is required",
            )
        # Comprehension sentinels: synthesise a concrete container type
        # so downstream ``for`` loops / subscripts see a real ListType /
        # DictType / SetType instead of plain DynType.
        if isinstance(callee, Name):
            sentinel = _name_ident(callee)
            if sentinel is None:
                _raise_frontend_error(
                    expr.span,
                    "call target missing identifier",
                    "upgrade the parser/frontend AST to use Name-style identifiers",
                )
            if sentinel in (
                "_list_comp",
                "__listcomp__",
                "_gen_comp",
                "__genexpr__",
            ):
                elt = new_args[0] if new_args else None
                elt_ty = ctx.resolve_type_refs(elt.ty) if elt is not None else TYPE_DYN
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=ListType(name="list", elem=elt_ty),
                )
            if sentinel in ("_set_comp", "__setcomp__"):
                elt = new_args[0] if new_args else None
                elt_ty = ctx.resolve_type_refs(elt.ty) if elt is not None else TYPE_DYN
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=SetType(name="set", elem=elt_ty),
                )
            if sentinel in ("_dict_comp", "__dictcomp__"):
                # Native: first arg is TupleExpr(k, v). CPython-AST:
                # first two args are key/val exprs.
                if sentinel == "_dict_comp" and new_args:
                    kv = new_args[0]
                    if isinstance(kv, TupleExpr) and len(kv.elems) == 2:
                        k_ty = ctx.resolve_type_refs(kv.elems[0].ty)
                        v_ty = ctx.resolve_type_refs(kv.elems[1].ty)
                        return replace(
                            expr,
                            func=callee,
                            args=new_args,
                            kwargs=new_kwargs,
                            ty=DictType(name="dict", key=k_ty, value=v_ty),
                        )
                if sentinel == "__dictcomp__" and len(new_args) >= 2:
                    k_ty = ctx.resolve_type_refs(new_args[0].ty)
                    v_ty = ctx.resolve_type_refs(new_args[1].ty)
                    return replace(
                        expr,
                        func=callee,
                        args=new_args,
                        kwargs=new_kwargs,
                        ty=DictType(name="dict", key=k_ty, value=v_ty),
                    )
        # Known-return-type builtins: ``sum`` returns int, ``len`` returns
        # int, ``min``/``max``/``abs`` return the operand type family.
        if isinstance(callee, Name):
            bname = _name_ident(callee)
            if bname is None:
                _raise_frontend_error(
                    expr.span,
                    "call target missing identifier",
                    "upgrade the parser/frontend AST to use Name-style identifiers",
                )
            builtin_unshadowed = scope.lookup(bname) is None
            if builtin_unshadowed and new_args and _is_valueclass_type(new_args[0].ty):
                if bname == "vars":
                    _raise_frontend_error(
                        expr.span,
                        "vars() is not supported for valueclass payloads in strict mode",
                        "valueclass payloads have no instance dictionary; explicitly "
                        "box into an ordinary identity object if a dictionary is required",
                    )
                if bname == "setattr" or bname == "delattr":
                    _raise_frontend_error(
                        expr.span,
                        "attribute mutation is not supported for valueclass payloads in strict mode",
                        "valueclass payloads are immutable; construct a new value or use "
                        "an ordinary identity class",
                    )
                if (
                    (bname == "getattr" or bname == "hasattr")
                    and len(new_args) >= 2
                    and isinstance(new_args[1], StrLit)
                ):
                    identity_attr = new_args[1].value
                    if identity_attr == "__dict__":
                        _raise_frontend_error(
                            expr.span,
                            "valueclass payload has no instance dictionary",
                            "remove the __dict__ access or explicitly box into an "
                            "ordinary identity object",
                        )
                    if identity_attr == "__weakref__":
                        _raise_frontend_error(
                            expr.span,
                            "valueclass payload has no weak reference slot",
                            "use an ordinary identity class when weak referencing is required",
                        )
            extern_ty = _extern_factory_call_type(
                ctx,
                callee,
                new_args,
                new_kwargs,
            )
            if extern_ty is not None:
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=extern_ty,
                )
            if bname in ctx.dataclasses_replace_aliases and len(new_args) == 1:
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=new_args[0].ty,
                )
            if bname == "copy" and len(new_args) == 1 and not new_kwargs:
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=ctx.resolve_type_refs(new_args[0].ty),
                )
            if bname == "sum":
                # sum() answers with the type its items and start produce
                # at run time (sum([]) is 0, sum([0.5]) 0.5, sum(rows, [])
                # a list); annotations do not prove what a list holds.
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_DYN,
                )
            if bname == "divmod":
                numeric_args = len(new_args) == 2 and all(
                    isinstance(a.ty, (IntType, BoolType, FloatType))
                    for a in new_args
                )
                dm_type: Type = TYPE_DYN
                if numeric_args:
                    dm_elem: Type = IntType(name="int")
                    if any(isinstance(a.ty, FloatType) for a in new_args):
                        dm_elem = TYPE_FLOAT
                    dm_type = TupleType(name="tuple", elems=(dm_elem, dm_elem))
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=dm_type,
                )
            if bname == "pow":
                # pow(b, e) is ``b ** e``: int for ints, float when either is
                # a float. A modulus keeps int only for three ints (float and
                # complex refuse it); a None modulus, a Python class's
                # __pow__/__rpow__ or any other operand answers at runtime.
                ty: Type = TYPE_DYN
                if len(new_args) == 2 and not new_kwargs:
                    if all(isinstance(a.ty, (IntType, BoolType, FloatType)) for a in new_args):
                        ty = IntType(name="int")
                        if any(isinstance(a.ty, FloatType) for a in new_args):
                            ty = TYPE_FLOAT
                elif len(new_args) == 3 and not new_kwargs:
                    if all(isinstance(a.ty, (IntType, BoolType)) for a in new_args):
                        ty = IntType(name="int")
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=ty,
                )
            if bname in ("iter", "next"):
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_DYN,
                )
            if bname == "__await__":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_DYN,
                )
            if bname == "type" and len(new_args) == 3:
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_DYN,
                )
            if bname == "int":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=IntType(name="int"),
                )
            if bname == "bool":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_BOOL,
                )
            if bname == "float":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_FLOAT,
                )
            if bname == "complex":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_COMPLEX,
                )
            if bname == "__pcc_format_spec":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_STR,
                )
            if bname in ("setattr", "delattr"):
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=NoneType(name="None"),
                )
            if bname == "str":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_STR,
                )
            if bname == "bytes":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_BYTES,
                )
            if bname == "bytearray":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_BYTEARRAY,
                )
            if bname == "memoryview":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_MEMORYVIEW,
                )
            if bname == "tuple":
                if not new_args:
                    ty = TupleType(name="tuple", elems=())
                else:
                    ty = _tuple_from_iterable_type(
                        ctx.resolve_type_refs(new_args[0].ty)
                    )
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=ty,
                )
            if bname in ("sorted", "reversed"):
                elem_ty: Type = TYPE_DYN
                if new_args:
                    src_ty = ctx.resolve_type_refs(new_args[0].ty)
                    if isinstance(src_ty, ListType):
                        elem_ty = ctx.resolve_type_refs(src_ty.elem)
                    elif isinstance(src_ty, TupleType):
                        elem_ty = ctx.resolve_type_refs(_tuple_elem_type(src_ty))
                    elif isinstance(src_ty, DictType):
                        elem_ty = ctx.resolve_type_refs(src_ty.key)
                    elif isinstance(src_ty, StrType):
                        elem_ty = TYPE_STR
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=ListType(name="list", elem=elem_ty),
                )
            if bname == "chr":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_STR,
                )
            if bname in ("any", "all"):
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_BOOL,
                )
            if bname == "issubclass":
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_BOOL,
                )
            if bname == "abs":
                if new_args and isinstance(
                    new_args[0].ty,
                    (IntType, FloatType, BoolType),
                ):
                    return replace(
                        expr,
                        func=callee,
                        args=new_args,
                        kwargs=new_kwargs,
                        ty=new_args[0].ty,
                    )
            if bname in ("repr",):
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_STR,
                )
            if bname in ("hash", "id"):
                if (
                    bname == "id"
                    and builtin_unshadowed
                    and new_args
                    and _is_valueclass_type(new_args[0].ty)
                ):
                    _raise_frontend_error(
                        expr.span,
                        "id() is not supported for valueclass payloads in strict mode",
                        "explicitly box the value before observing identity",
                    )
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=IntType(name="int"),
                )
            if bname in ("min", "max") and new_args:
                # Single-arg iterable form: result is the iterable's
                # element type. Multi-arg form: common type of args.
                if len(new_args) == 1:
                    a0_ty = ctx.resolve_type_refs(new_args[0].ty)
                    if isinstance(a0_ty, ListType):
                        acc = ctx.resolve_type_refs(a0_ty.elem)
                    elif isinstance(a0_ty, TupleType) and a0_ty.elems:
                        acc = ctx.resolve_type_refs(a0_ty.elems[0])
                        for e in a0_ty.elems[1:]:
                            acc = value_join_type(acc, ctx.resolve_type_refs(e))
                    elif isinstance(a0_ty, DynType):
                        acc = DynType(name="dyn")
                    elif isinstance(a0_ty, StrType):
                        acc = TYPE_STR
                    else:
                        acc = IntType(name="int")
                    # min/max return the selected element object. A float
                    # projection would rebox it and lose identity (and may
                    # coerce a selected int from a mixed numeric iterable).
                    if isinstance(acc, (FloatType, BoolType)):
                        acc = DynType(name="dyn")
                else:
                    # The result is one of the arguments: a static type holds
                    # only when every argument has it (min(1, 2.5) is int 1).
                    acc = ctx.resolve_type_refs(new_args[0].ty)
                    for a in new_args[1:]:
                        a_ty = ctx.resolve_type_refs(a.ty)
                        if type(a_ty) is not type(acc) or a_ty != acc:
                            acc = DynType(name="dyn")
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=acc,
                )

        # Method-call result inference for known typed-container methods
        # so chained calls stay on the pcc-native fast paths without
        # needing an annotation hint at every site.
        if isinstance(callee, Attr):
            recv_ty = ctx.resolve_type_refs(callee.obj.ty)
            method = callee.name
            inferred = _container_method_return_type(recv_ty, method)
            if inferred is None and isinstance(recv_ty, ClassType):
                inferred = _external_method_return_type(
                    ctx,
                    recv_ty,
                    method,
                    isinstance(callee.obj, Name)
                    and scope.is_class_object(callee.obj.ident),
                )
            if (
                inferred is not None
                and isinstance(recv_ty, DictType)
                and method in ("get", "setdefault")
                and len(new_args) == 1
                and not new_kwargs
                and _typeconf_storage_class(inferred) != "object"
            ):
                # 1-arg get/setdefault returns None for a missing key; a
                # native-scalar static result type cannot represent that.
                inferred = TYPE_DYN
            if inferred is not None:
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=inferred,
                )
            # ``functools.partial`` is represented by the native runtime as a
            # PyFuncObject, even when recursive multi-file inference can see
            # the pcc-Python provider's ``class partial`` export.  Keeping the
            # provider ClassType here makes a later ``bound(...)`` emit a
            # direct call to ``partial.__call__`` with that PyFuncObject as
            # ``self``.  Preserve the callable's dynamic runtime projection.
            if (
                method == "partial"
                and isinstance(callee.obj, Name)
                and _name_ident(callee.obj) in ctx.functools_module_aliases
            ):
                return replace(
                    expr,
                    func=callee,
                    args=new_args,
                    kwargs=new_kwargs,
                    ty=TYPE_DYN,
                )
        ret_ty = _call_result_type(ctx, callee)
        return replace(
            expr,
            func=callee,
            args=new_args,
            kwargs=new_kwargs,
            ty=ret_ty,
        )

    # Attribute / subscript / slice (Phase 1: opaque → dyn) --------------
    if isinstance(expr, Attr):
        obj = _infer_expr(ctx, scope, expr.obj)
        obj_ty = ctx.resolve_type_refs(obj.ty)
        if _is_valueclass_type(obj_ty):
            if expr.name == "__dict__":
                _raise_frontend_error(
                    expr.span,
                    "valueclass payload has no instance dictionary",
                    "remove the __dict__ access or explicitly box into an "
                    "ordinary identity object",
                )
            if expr.name == "__weakref__":
                _raise_frontend_error(
                    expr.span,
                    "valueclass payload has no weak reference slot",
                    "use an ordinary identity class when weak referencing is required",
                )
        # Module-aliased class reference: ``alias.ClassName`` where the
        # ``import alias`` statement registered the module's exports.
        # Returning the ClassType here lets the call-result inference
        # treat ``alias.ClassName(args)`` as a constructor and type the
        # result as a ClassType instance. Without this, stdlib-walked
        # modules (``import pathlib``) bottom out at DynType.
        parts = [expr.name]
        head = expr.obj
        while isinstance(head, Attr):
            parts.append(head.name)
            head = head.obj
        if isinstance(head, Name):
            obj_ident = _name_ident(head)
            if obj_ident is not None:
                parts.append(obj_ident)
                parts.reverse()
                qualified = ".".join(parts)
                qty = ctx.class_types.get(qualified)
                if isinstance(qty, ClassType):
                    return replace(expr, obj=obj, ty=qty)
                attr_ty = ctx.module_attr_types.get(qualified)
                if attr_ty is not None:
                    return replace(expr, obj=obj, ty=attr_ty)
        # Bucket 1: when the receiver is a known class type with
        # field declarations, look up the field's declared type.
        # Walks the MRO (bases) so inherited fields resolve too.
        if isinstance(obj_ty, ClassType):
            # @property declarations take precedence over the generic
            # DynType fallback so downstream typed-method dispatch
            # (e.g. ``c.name.rfind('.')`` where ``name`` is a str-typed
            # property) routes through the native runtime instead of
            # ``py_cpy_getattr``. See
            # docs/investigations/pcc-py-type-infer-property-return-type.md.
            attr_ty = _lookup_class_attr_type(obj_ty, expr.name)
            if attr_ty is not None:
                return replace(expr, obj=obj, ty=ctx.resolve_type_refs(attr_ty))
        if isinstance(obj_ty, ComplexType) and expr.name in ("real", "imag"):
            return replace(expr, obj=obj, ty=TYPE_FLOAT)
        return replace(expr, obj=obj, ty=TYPE_DYN)

    if isinstance(expr, Subscript):
        obj = _infer_expr(ctx, scope, expr.obj)
        idx = _infer_expr(ctx, scope, expr.idx)
        obj_ty = ctx.resolve_type_refs(obj.ty)
        # ``xs[lo:hi]`` — slicing returns a new container of the same
        # kind: list → list[elem], str → str, tuple → tuple (element
        # types preserved but arity unknown, use ``tuple_variadic``).
        if isinstance(idx, Slice):
            if isinstance(obj_ty, ListType):
                ty = obj_ty
            elif isinstance(obj_ty, StrType):
                ty = TYPE_STR
            elif isinstance(obj_ty, (BytesType, ByteArrayType, MemoryViewType)):
                ty = TYPE_BYTES
            elif isinstance(obj_ty, TupleType):
                if obj_ty.elems:
                    ty = TupleType(
                        name="tuple_variadic",
                        elems=(ctx.resolve_type_refs(_tuple_elem_type(obj_ty)),),
                    )
                else:
                    ty = TupleType(name="tuple", elems=())
            else:
                ty = TYPE_DYN
            return replace(expr, obj=obj, idx=idx, ty=ty)
        if isinstance(obj_ty, ListType):
            ty = ctx.resolve_type_refs(obj_ty.elem)
        elif isinstance(obj_ty, ValueArrayType):
            ty = ctx.resolve_type_refs(obj_ty.elem)
        elif isinstance(obj_ty, TupleType) and obj_ty.elems:
            # Phase 1: if all element types agree, use that; else dyn.
            first = ctx.resolve_type_refs(obj_ty.elems[0])
            ty = (
                first
                if all(type_eq(first, ctx.resolve_type_refs(e)) for e in obj_ty.elems)
                else TYPE_DYN
            )
        elif isinstance(obj_ty, DictType):
            ty = ctx.resolve_type_refs(obj_ty.value)
        elif isinstance(obj_ty, StrType):
            ty = TYPE_STR
        elif isinstance(obj_ty, (BytesType, ByteArrayType, MemoryViewType)):
            ty = TYPE_INT
        else:
            ty = TYPE_DYN
        return replace(expr, obj=obj, idx=idx, ty=ty)

    if isinstance(expr, Slice):
        lo = _infer_expr(ctx, scope, expr.lo) if expr.lo is not None else None
        hi = _infer_expr(ctx, scope, expr.hi) if expr.hi is not None else None
        step = _infer_expr(ctx, scope, expr.step) if expr.step is not None else None
        return replace(expr, lo=lo, hi=hi, step=step, ty=TYPE_DYN)

    # Container literals ------------------------------------------------
    if isinstance(expr, ListExpr):
        new_elems = tuple(_infer_expr(ctx, scope, e) for e in expr.elems)
        if not new_elems:
            list_ty: Type = ListType(name="list", elem=TYPE_DYN)
        else:
            acc = ctx.resolve_type_refs(new_elems[0].ty)
            for el in new_elems[1:]:
                acc = value_join_type(acc, ctx.resolve_type_refs(el.ty))
            if isinstance(acc, NoneType):
                # `[None]` describes as little as `[]` above: it says where a
                # slot starts, not what it holds. Typing the element NoneType
                # made every read of such a box NoneType too, so the mutable
                # cell idiom -- `_BOX = [None]` with `_BOX[0] = value`
                # elsewhere -- pushed each reader onto the CPython fallback:
                # asyncio's `get_event_loop()` returned NoneType and
                # `loop.create_task(...)` then misdispatched to the
                # module-level function of the same name.
                acc = TYPE_DYN
            list_ty = ListType(name="list", elem=acc)
        return replace(expr, elems=new_elems, ty=list_ty)

    if isinstance(expr, TupleExpr):
        new_elems = tuple(_infer_expr(ctx, scope, e) for e in expr.elems)
        tup_ty = TupleType(
            name="tuple",
            elems=tuple(ctx.resolve_type_refs(e.ty) for e in new_elems),
        )
        return replace(expr, elems=new_elems, ty=tup_ty)

    if isinstance(expr, DictExpr):
        new_pairs = tuple(
            (_infer_expr(ctx, scope, k), _infer_expr(ctx, scope, v))
            for (k, v) in expr.pairs
        )
        if not new_pairs:
            dict_ty: Type = DictType(name="dict", key=TYPE_DYN, value=TYPE_DYN)
        else:
            key_ty = ctx.resolve_type_refs(new_pairs[0][0].ty)
            val_ty = ctx.resolve_type_refs(new_pairs[0][1].ty)
            for k, v in new_pairs[1:]:
                key_ty = value_join_type(key_ty, ctx.resolve_type_refs(k.ty))
                val_ty = value_join_type(val_ty, ctx.resolve_type_refs(v.ty))
            dict_ty = DictType(name="dict", key=key_ty, value=val_ty)
        return replace(expr, pairs=new_pairs, ty=dict_ty)

    # Ternary ``a if c else b`` ----------------------------------------
    if isinstance(expr, IfExpr):
        cond = _infer_expr(ctx, scope, expr.cond)
        then_e = _infer_expr(ctx, scope, expr.then_e)
        else_e = _infer_expr(ctx, scope, expr.else_e)
        then_e, else_e = _contextualize_raw_int_operands(
            then_e,
            else_e,
            expr.span,
        )
        ty = value_join_type(then_e.ty, else_e.ty)
        if ctx.pointer_lane:
            # An explicit unmanaged arm selects the manual pointer view.
            # Known managed values and ordinary Python joins stay strict.
            if isinstance(then_e.ty, RawPointerType) and isinstance(else_e.ty, DynType):
                ty = then_e.ty
            elif isinstance(else_e.ty, RawPointerType) and isinstance(then_e.ty, DynType):
                ty = else_e.ty
        return replace(expr, cond=cond, then_e=then_e, else_e=else_e, ty=ty)

    # Lambda — Phase 1 leaves the body untyped; return a dyn FuncType.
    if isinstance(expr, Lambda):
        # Resolve annotations on the lambda params (usually absent).
        param_types = tuple(ctx.resolve_annotation(p.annotation) for p in expr.params)
        lam_ty = FuncType(name="callable", params=param_types, ret=TYPE_DYN)
        return _with_ty(expr, lam_ty)

    # Unknown expression node — leave as dyn.  This keeps the pass total.
    return _with_ty(expr, TYPE_DYN)


def _binop_result(op: str, a: Type, b: Type, span: SourceSpan) -> Type:
    """Type-of for ``a op b``.

    Phase 1 focuses on numeric + string.  Bitwise ops on ints stay int;
    division (``/``) promotes to float; everything else follows
    :func:`common_type`.
    """
    # String concatenation / repetition.
    if op == "+":
        if (isinstance(a, ComplexType) or isinstance(b, ComplexType)) and is_numeric(a) and is_numeric(b):
            return TYPE_COMPLEX
        if isinstance(a, StrType) and isinstance(b, StrType):
            return TYPE_STR
        if isinstance(a, TupleType) and isinstance(b, TupleType):
            return _tuple_concat_type(a, b)
        if isinstance(a, ListType) and isinstance(b, ListType):
            return ListType(name="list", elem=value_join_type(a.elem, b.elem))
    if op == "%":
        if isinstance(a, StrType):
            return TYPE_STR
    if op == "*":
        # str * int or int * str → str
        if isinstance(a, StrType) and isinstance(b, (IntType, BoolType)):
            return TYPE_STR
        if isinstance(b, StrType) and isinstance(a, (IntType, BoolType)):
            return TYPE_STR
    if op == "+" and (
        (isinstance(a, StrType) and is_numeric(b))
        or (isinstance(b, StrType) and is_numeric(a))
    ):
        return TYPE_DYN

    # Reject obvious mismatches early with a friendly error.
    if op in ("+", "-", "*", "/", "//", "%", "**"):
        if isinstance(a, StrType) and is_numeric(b):
            if op not in ("*", "%"):
                _raise_frontend_error(
                    span,
                    f"unsupported operand type(s) for {op}: 'str' and numeric",
                    "use str() or explicit conversion",
                )
        if isinstance(b, StrType) and is_numeric(a):
            if op not in ("*", "%"):
                _raise_frontend_error(
                    span,
                    f"unsupported operand type(s) for {op}: numeric and 'str'",
                    "use str() or explicit conversion",
                )

    # A complex operand promotes only another number; a dynamic partner
    # keeps the result dynamic for the runtime dispatch.
    if (isinstance(a, ComplexType) or isinstance(b, ComplexType)) and is_numeric(a) and is_numeric(b):
        return TYPE_COMPLEX
    # True division always returns float for numeric operands.
    if op == "/" and is_numeric(a) and is_numeric(b):
        return TYPE_FLOAT

    if (
        op in ("&", "|", "-", "^")
        and isinstance(a, SetType)
        and isinstance(b, SetType)
    ):
        return SetType(name=a.name, elem=value_join_type(a.elem, b.elem))

    # Bitwise / shift on int-like operands stays int.
    if op in ("&", "|", "^", "<<", ">>"):
        if isinstance(a, (IntType, BoolType)) and isinstance(b, (IntType, BoolType)):
            # Bool <<>> anything else returns int (Python promotes).
            promoted = common_type(a, b)
            if _is_raw_int_type(promoted):
                return promoted
            return TYPE_INT
        return TYPE_DYN

    # Power: int ** int → int; anything touching float → float.
    if op == "**":
        if isinstance(a, FloatType) or isinstance(b, FloatType):
            return TYPE_FLOAT
        if is_numeric(a) and is_numeric(b):
            promoted = common_type(a, b)
            if _is_raw_int_type(promoted):
                return promoted
            return TYPE_INT

    # Default arithmetic promotion.
    if is_numeric(a) and is_numeric(b):
        return common_type(a, b)

    return TYPE_DYN


def _call_result_type(ctx: _InferCtx, callee: Expr) -> Type:
    """Best-effort return type for a ``Call`` whose callee has been typed."""
    # Direct by name: look up user-defined function.
    if isinstance(callee, Name):
        callee_ident = _name_ident(callee)
        if callee_ident is None:
            return TYPE_DYN
        ft = ctx.func_types.get(callee_ident)
        if ft is not None:
            return ft.ret
        # Fall through to the callee's own type (may be a FuncType from
        # builtins or a user definition captured via a local binding).
    if isinstance(callee.ty, FuncType):
        return callee.ty.ret
    if isinstance(callee.ty, ClassType):
        return callee.ty
    return TYPE_DYN


def _external_attribute_interceptors(ctx: _InferCtx) -> set[str]:
    """Cache the complete hook graph once for this inference context."""
    cached = ctx._attribute_interceptors
    if cached is not None:
        return cached
    from pcc.frontends.python.codegen.class_override_index import (
        build_export_attribute_interceptors,
    )

    exports = dict(ctx.external_exports)
    local_exports = dict(exports.get(ctx.module_name, {}))
    for stmt in ctx.module.body:
        if not isinstance(stmt, ClassDef):
            continue
        bases = []
        for base in _class_bases_from_def(ctx, stmt):
            bases.append(_class_type_module(base) + "." + base.name)
        local_exports[stmt.name] = {
            "kind": "class",
            "owning_module": ctx.module_name,
            "class_name": stmt.name,
            "base_names": tuple(bases),
            "methods": tuple(
                {"name": member.name}
                for member in stmt.body if isinstance(member, FuncDef)
            ),
        }
    exports[ctx.module_name] = local_exports
    cached = build_export_attribute_interceptors(exports)
    ctx._attribute_interceptors = cached
    return cached


def _external_method_return_type(
    ctx: _InferCtx,
    receiver: ClassType,
    method_name: str,
    receiver_is_class_object: bool = False,
) -> Optional[Type]:
    module_name = _class_type_module(receiver)
    if not module_name:
        return None
    module_exports = ctx.external_exports.get(module_name)
    if not module_exports:
        return None
    class_info = module_exports.get(receiver.name)
    if not (
        isinstance(class_info, dict)
        and class_info.get("kind") == "class"
    ):
        return None
    for method in class_info.get("methods", ()):
        if method.get("name") != method_name:
            continue
        # Instance lookup may return a completely different callable. Its
        # result must stay boxed even if the declared method returns a
        # float/bool (or another statically known type). Class-object lookup
        # does not invoke an instance's __getattribute__.
        if (
            not receiver_is_class_object
            and module_name + "." + receiver.name
            in _external_attribute_interceptors(ctx)
        ):
            return TYPE_DYN
        memo: dict[tuple[str, str], ClassType] = {}
        return _resolve_export_type_refs(
            ctx,
            module_name,
            module_exports,
            memo,
            _annotation_to_type(decode_type(method.get("return_ty"))),
        )
    return None


def _extern_ctype_value_type(ctype_name: str) -> Type:
    """Return a pcc type only when the C and pcc IR lanes are compatible.

    Narrow C integers and ``float`` still need return-side ABI widening, so
    they deliberately remain dynamic here.  ``double`` and signed ``int64``
    already match pcc's native float/int representations exactly.
    """
    if ctype_name == "c_double":
        return TYPE_FLOAT
    if ctype_name in ("c_int64", "c_long"):
        return TYPE_INT
    if ctype_name == "c_bool":
        return TYPE_BOOL
    if ctype_name == "c_void":
        return TYPE_NONE
    if ctype_name == "c_rawptr":
        # A raw address result is an int outside runtime-port mode so it can
        # never enter the object refcount protocol as a pointer.
        return TYPE_INT
    return TYPE_DYN


def _extern_factory_call_type(
    ctx: _InferCtx,
    callee: Expr,
    args: tuple[Expr, ...],
    kwargs: tuple[tuple[str, Expr], ...],
) -> Optional[FuncType]:
    """Recover an ``extern`` declaration's callable type from its markers."""
    if not isinstance(callee, Name):
        return None
    callee_ident = _name_ident(callee)
    if callee_ident not in ctx.extern_factory_aliases:
        return None

    argtypes_expr: Optional[Expr] = args[1] if len(args) >= 2 else None
    restype_expr: Optional[Expr] = args[2] if len(args) >= 3 else None
    for key, value in kwargs:
        if key == "argtypes":
            argtypes_expr = value
        elif key == "restype":
            restype_expr = value

    params: list[Type] = []
    if isinstance(argtypes_expr, TupleExpr):
        for marker in argtypes_expr.elems:
            marker_name = _name_ident(marker) if isinstance(marker, Name) else None
            canonical = ctx.extern_ctype_aliases.get(marker_name or "", "")
            if ctx.pointer_lane and canonical in ("c_obj", "c_rawptr"):
                canonical = "c_ptr"
            params.append(_extern_ctype_value_type(canonical))

    restype_name = "c_void"
    if isinstance(restype_expr, Name):
        marker_name = _name_ident(restype_expr)
        restype_name = ctx.extern_ctype_aliases.get(marker_name or "", "")
    if ctx.pointer_lane and restype_name in ("c_obj", "c_rawptr"):
        # Pointer-lane modules keep every pointer result in the pointer lane;
        # the object/raw split only exists for application modules.
        restype_name = "c_ptr"
    if not ctx.pointer_lane and restype_name in ("c_ptr", "c_str"):
        # Fail closed: the frontend cannot tell a PyObject* result from raw
        # memory, and a raw address typed as an object would defeat the
        # provenance guarantee the runtime refcount paths rely on.
        _raise_frontend_error(
            callee.span,
            "extern return type " + restype_name + " is ambiguous between a "
            "Python object and a raw address",
            "declare the return as c_obj (PyObject*) or c_rawptr (raw address)",
        )
    ret_ty = _extern_ctype_value_type(restype_name)
    if ctx.freestanding:
        raw_params = []
        for param in params:
            raw_params.append(TYPE_I64 if type_eq(param, TYPE_INT) else param)
        params = raw_params
        if type_eq(ret_ty, TYPE_INT):
            ret_ty = TYPE_I64
    return FuncType(
        name="callable",
        params=tuple(params),
        ret=ret_ty,
    )


def _expr_contains_yield_sentinel(expr: Expr) -> bool:
    if isinstance(expr, Call):
        if isinstance(expr.func, Name) and expr.func.ident in (
            "_yield",
            "__yield__",
            "_yield_from",
            "__yield_from__",
        ):
            return True
        if _expr_contains_yield_sentinel(expr.func):
            return True
        for arg in expr.args:
            if _expr_contains_yield_sentinel(arg):
                return True
        for _name, value in expr.kwargs:
            if _expr_contains_yield_sentinel(value):
                return True
        return False
    if isinstance(expr, Attr):
        return _expr_contains_yield_sentinel(expr.obj)
    if isinstance(expr, BinOp):
        return _expr_contains_yield_sentinel(expr.lhs) or _expr_contains_yield_sentinel(
            expr.rhs
        )
    if isinstance(expr, UnaryOp):
        return _expr_contains_yield_sentinel(expr.operand)
    if isinstance(expr, Compare):
        return _expr_contains_yield_sentinel(expr.lhs) or _expr_contains_yield_sentinel(
            expr.rhs
        )
    if isinstance(expr, BoolExpr):
        return _expr_contains_yield_sentinel(
            expr.left
        ) or _expr_contains_yield_sentinel(expr.right)
    if isinstance(expr, Subscript):
        return _expr_contains_yield_sentinel(expr.obj) or _expr_contains_yield_sentinel(
            expr.idx
        )
    if isinstance(expr, Slice):
        for part in (expr.lo, expr.hi, expr.step):
            if part is not None and _expr_contains_yield_sentinel(part):
                return True
        return False
    if isinstance(expr, ListExpr):
        return any(_expr_contains_yield_sentinel(item) for item in expr.elems)
    if isinstance(expr, TupleExpr):
        return any(_expr_contains_yield_sentinel(item) for item in expr.elems)
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            if _expr_contains_yield_sentinel(key) or _expr_contains_yield_sentinel(
                value
            ):
                return True
        return False
    if isinstance(expr, IfExpr):
        return (
            _expr_contains_yield_sentinel(expr.cond)
            or _expr_contains_yield_sentinel(expr.then_e)
            or _expr_contains_yield_sentinel(expr.else_e)
        )
    if isinstance(expr, Lambda):
        return _expr_contains_yield_sentinel(expr.body)
    return False


def _stmt_contains_yield_sentinel(stmt: Stmt) -> bool:
    if isinstance(stmt, Assign):
        if _expr_contains_yield_sentinel(stmt.value):
            return True
        return any(_expr_contains_yield_sentinel(target) for target in stmt.targets)
    if isinstance(stmt, AugAssign):
        return _expr_contains_yield_sentinel(
            stmt.target
        ) or _expr_contains_yield_sentinel(stmt.value)
    if isinstance(stmt, ExprStmt):
        return _expr_contains_yield_sentinel(stmt.expr)
    if isinstance(stmt, Return):
        return stmt.value is not None and _expr_contains_yield_sentinel(stmt.value)
    if isinstance(stmt, Raise):
        return (stmt.exc is not None and _expr_contains_yield_sentinel(stmt.exc)) or (
            stmt.cause is not None and _expr_contains_yield_sentinel(stmt.cause)
        )
    if isinstance(stmt, Delete):
        return any(_expr_contains_yield_sentinel(target) for target in stmt.targets)
    if isinstance(stmt, If):
        return (
            _expr_contains_yield_sentinel(stmt.cond)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.body)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.else_body)
        )
    if isinstance(stmt, While):
        return (
            _expr_contains_yield_sentinel(stmt.cond)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.body)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.else_body)
        )
    if isinstance(stmt, For):
        return (
            _expr_contains_yield_sentinel(stmt.target)
            or _expr_contains_yield_sentinel(stmt.iter)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.body)
            or any(_stmt_contains_yield_sentinel(item) for item in stmt.else_body)
        )
    if isinstance(stmt, With):
        for ctx_expr, as_var in stmt.items:
            if _expr_contains_yield_sentinel(ctx_expr):
                return True
            if as_var is not None and _expr_contains_yield_sentinel(as_var):
                return True
        return any(_stmt_contains_yield_sentinel(item) for item in stmt.body)
    if isinstance(stmt, Try):
        if any(_stmt_contains_yield_sentinel(item) for item in stmt.body):
            return True
        if any(_stmt_contains_yield_sentinel(item) for item in stmt.else_body):
            return True
        if any(_stmt_contains_yield_sentinel(item) for item in stmt.finally_body):
            return True
        for handler in stmt.handlers:
            if handler.exc_type is not None and _expr_contains_yield_sentinel(
                handler.exc_type
            ):
                return True
            if any(_stmt_contains_yield_sentinel(item) for item in handler.body):
                return True
        return False
    if isinstance(stmt, FuncDef):
        return False
    if isinstance(stmt, ClassDef):
        return False
    return False


def _funcdef_contains_yield_sentinel(fn: FuncDef) -> bool:
    return any(_stmt_contains_yield_sentinel(stmt) for stmt in fn.body)


def _stmt_returns_notimplemented(stmt: Stmt) -> bool:
    """``return NotImplemented`` in this statement, outside nested defs."""
    if isinstance(stmt, Return):
        return isinstance(stmt.value, Name) and stmt.value.ident == "NotImplemented"
    if isinstance(stmt, (If, While, For)):
        return any(_stmt_returns_notimplemented(item) for item in stmt.body) or any(
            _stmt_returns_notimplemented(item) for item in stmt.else_body
        )
    if isinstance(stmt, With):
        return any(_stmt_returns_notimplemented(item) for item in stmt.body)
    if isinstance(stmt, Try):
        for block in (stmt.body, stmt.else_body, stmt.finally_body):
            if any(_stmt_returns_notimplemented(item) for item in block):
                return True
        return any(
            _stmt_returns_notimplemented(item)
            for handler in stmt.handlers
            for item in handler.body
        )
    return False


def _funcdef_returns_notimplemented(fn: FuncDef) -> bool:
    return any(_stmt_returns_notimplemented(stmt) for stmt in fn.body)


# ---------------------------------------------------------------------------
# Control-flow join widening (SEC-P1-TYPECONF)
# ---------------------------------------------------------------------------
#
# When one simple local is assigned differing storage classes on the two arms
# of an ``if``/``else`` (e.g. ``float`` on one branch and ``int`` on the
# other), the two arms lower into different basic blocks that share a single
# stack slot. The slot's IR type is fixed by whichever branch the inferencer
# happened to type last, so the other branch stores an incompatible value into
# it -- a float bit-pattern lands in a ``ptr`` slot (or vice-versa) and the
# post-join read comes back as ``<null>``. That is a type confusion in the
# sense of *Low-Level Software Security for Compiler Developers*: a value of
# one type is observed through a slot typed for another.
#
# The fix is a *forced-object widening* layered on top of the existing
# shared-scope inference: after both arms have been inferred exactly as before
# (no change to what either arm sees while it is inferred, and no child-scope /
# selective-propagation rework -- that variant broke pcc1->pcc2->pcc3), inspect
# the already-typed arms. For any simple local whose two candidate types would
# occupy *different* storage slots, rebind it in the shared scope to the
# generic boxed-object type (``DynType`` -> ``PyObject*``). Both arms then box
# into one uniform ``ptr`` slot and the join reads a real object. Names that
# already share a slot (both objects, or a numeric pair the store path coerces)
# are left untouched so the common case keeps its unboxed representation.


def _typeconf_storage_class(ty: Type) -> str:
    """Bucket ``ty`` by the storage slot the L1 codegen would give it.

    The buckets mirror ``type_abi_lowering._map_type``: ``int``/``bool`` lower
    to an integer slot, ``float`` to a double slot, and every object-shaped
    type (str/bytes/list/dict/tuple/None/class/dyn/func/complex/...) to a
    ``PyObject*`` slot. A ``dyn`` (already object) never needs widening.
    """
    if isinstance(ty, FloatType):
        return "float"
    if isinstance(ty, (IntType, BoolType)):
        # int and bool share the numeric-scalar family; the assignment store
        # path already coerces bool<->int, so they do not force widening.
        return "int"
    # Everything else -- str/bytes/list/dict/tuple/None/class/func/complex and
    # DynType itself -- lives in a PyObject* slot.
    return "object"


def _typeconf_container_joined_with_unknown(a: Type, b: Type) -> bool:
    """True when one arm specialises to list/tuple and the other is unknown.

    That pair shares the PyObject* slot, so no storage widening is required,
    but the container type authorises an unchecked indexed-iteration lowering
    that the unknown side cannot honour.
    """
    def _is_plain_dyn(ty: Type) -> bool:
        return isinstance(ty, DynType) and ty.name == "dyn"

    def _is_container(ty: Type) -> bool:
        return isinstance(ty, (ListType, TupleType))

    if _is_container(a) and _is_plain_dyn(b):
        return True
    return _is_container(b) and _is_plain_dyn(a)


def _typeconf_join_type(a: Optional[Type], b: Optional[Type]) -> Optional[Type]:
    """Return a widened type when arms ``a`` and ``b`` need a unified slot.

    Returns ``TYPE_DYN`` when the two candidate types would occupy different
    storage slots (a genuine slot-ABI conflict), and ``None`` when no widening
    is required (identical types, or two object-shaped types that already share
    the single ``PyObject*`` slot).
    """
    if a is None or b is None:
        return None
    if type_eq(a, b):
        return None
    class_a = _typeconf_storage_class(a)
    class_b = _typeconf_storage_class(b)
    if class_a == class_b:
        # Same storage family. Two objects already share one ptr slot; two
        # numerics (int/bool) share the coercing store path. No confusion.
        return None
    # Different storage families across the branches (float-vs-int,
    # scalar-vs-object, ...). Force a single boxed-object slot so both arms
    # box uniformly rather than aliasing incompatible raw payloads.
    return TYPE_DYN


def _typeconf_collect_arm_assignments(body: tuple, out: dict, annotated: dict) -> None:
    """Record ``name -> last-assigned type`` for simple locals in one arm.

    Only top-level ``Assign``/``AugAssign`` statements whose target is a plain
    ``Name`` are considered; the shared slot confusion this guards against only
    arises for a simple rebindable local. Assignments nested inside further
    control flow within the arm are intentionally skipped: this pass runs at
    every ``If`` node, so an inner ``If`` widens its own join before the outer
    one observes the (already-widened) binding through the scope.

    ``annotated`` collects names carrying an explicit annotation on this arm.
    An annotated local has a fixed declared type that the store path honours,
    so it must never be silently force-widened -- an incompatible annotated
    reassignment is a real type error, not a slot-ABI join to paper over.
    """
    for s in body:
        if isinstance(s, Assign):
            has_ann = _annotation_or_none(s) is not None
            for tgt in s.targets:
                if isinstance(tgt, Name):
                    ident = _name_ident(tgt)
                    if ident is not None:
                        out[ident] = tgt.ty
                        if has_ann:
                            annotated[ident] = True
        elif isinstance(s, AugAssign):
            tgt = s.target
            if isinstance(tgt, Name):
                ident = _name_ident(tgt)
                if ident is not None:
                    out[ident] = tgt.ty
        elif isinstance(s, FuncDef):
            # A nested def rebinds its name to a function object (an object
            # slot); the hoist pass emits that store through a dyn binding.
            out[s.name] = _make_func_type((), TYPE_DYN)


def _typeconf_collect_try_candidates(body: tuple, out: dict, annotated: dict) -> None:
    """Record every type a simple local takes at the top level of ``body``.

    A try body can stop after any statement, so each binding (not only the
    last) can reach a handler.
    """
    for s in body:
        last: dict = {}
        _typeconf_collect_arm_assignments((s,), last, annotated)
        for name in last:
            if name not in out:
                out[name] = []
            out[name].append(last[name])


def _typeconf_block_falls_through(block: tuple) -> bool:
    """Conservatively, whether control can leave ``block`` at its end."""
    if not block:
        return True
    return not isinstance(block[-1], (Return, Raise, Continue, Break))


def _typeconf_widen_try_join(
    scope: _Scope,
    pre_types: dict,
    body: tuple,
    handlers: tuple,
    else_body: tuple,
    finally_body: tuple,
) -> tuple:
    """Force-widen locals whose ``try`` exits disagree on slot ABI.

    Normal completion carries the last body/``else`` binding. A handler that
    falls through carries its own last binding, or else the pre-``try`` type or
    any partial-body binding. A ``finally`` binding replaces every exit.
    Conflicting locals become ``DynType`` in the shared scope and their
    top-level assignment targets in every block are retargeted to ``DynType``.
    """
    annotated: dict = {}
    normal: dict = {}
    _typeconf_collect_arm_assignments(body, normal, annotated)
    _typeconf_collect_arm_assignments(else_body, normal, annotated)
    partial: dict = {}
    _typeconf_collect_try_candidates(body, partial, annotated)
    handler_exits: list = []
    for handler in handlers:
        if _typeconf_block_falls_through(handler.body):
            last: dict = {}
            _typeconf_collect_arm_assignments(handler.body, last, annotated)
            handler_exits.append(last)
    finally_last: dict = {}
    _typeconf_collect_arm_assignments(finally_body, finally_last, annotated)
    names: list = []
    for table in [normal, finally_last] + handler_exits:
        for name in table:
            if name not in names:
                names.append(name)
    if handler_exits:
        for name in partial:
            if name not in names:
                names.append(name)
    widen: dict = {}
    for name in names:
        if annotated.get(name) or name in finally_last:
            continue
        pre_ty = pre_types.get(name)
        exits: list = []
        exits.append(normal.get(name, pre_ty))
        for last in handler_exits:
            if name in last:
                exits.append(last[name])
            else:
                exits.append(pre_ty)
                for ty in partial.get(name, ()):
                    exits.append(ty)
        types: list = []
        for ty in exits:
            if ty is not None:
                types.append(ty)
        index = 1
        while index < len(types):
            if _typeconf_join_type(types[0], types[index]) is not None:
                widen[name] = True
            index += 1
        if widen.get(name):
            scope.update(name, TYPE_DYN)
    if not widen:
        return (body, handlers, else_body, finally_body)
    new_handlers: list = []
    for handler in handlers:
        new_handlers.append(replace(handler, body=_typeconf_retarget_arm(handler.body, widen)))
    return (
        _typeconf_retarget_arm(body, widen),
        tuple(new_handlers),
        _typeconf_retarget_arm(else_body, widen),
        _typeconf_retarget_arm(finally_body, widen),
    )


def _typeconf_retarget_arm(body: tuple, widen: dict) -> tuple:
    """Rebind conflicting simple-local assignment targets to ``DynType``.

    ``widen`` maps ``name -> True`` for the locals whose control-flow join
    forced an object slot. Rewriting the assignment *target* type (not the
    RHS) makes the store path allocate one uniform ``PyObject*`` slot and box
    each arm's payload into it, so the merged read observes a real object
    rather than a raw ``double``/``i64`` aliased through a ``ptr`` slot.
    """
    changed = False
    new_body: list = []
    for s in body:
        if isinstance(s, Assign):
            new_targets: list = []
            tgt_changed = False
            for tgt in s.targets:
                if isinstance(tgt, Name):
                    ident = _name_ident(tgt)
                    if (
                        ident is not None
                        and widen.get(ident)
                        and not isinstance(tgt.ty, DynType)
                    ):
                        new_targets.append(_with_ty(tgt, TYPE_DYN))
                        tgt_changed = True
                        continue
                new_targets.append(tgt)
            if tgt_changed:
                new_body.append(replace(s, targets=tuple(new_targets)))
                changed = True
            else:
                new_body.append(s)
        else:
            new_body.append(s)
    if not changed:
        return body
    return tuple(new_body)


def _typeconf_widen_if_join(
    scope: _Scope,
    pre_types: dict,
    body: tuple,
    else_body: tuple,
) -> tuple:
    """Force-widen shared-scope locals whose two arms disagree on slot ABI.

    ``pre_types`` is the type each involved local had *before* the ``if`` (used
    when a name is assigned on only one arm, so the join is arm-type vs the
    fall-through type). ``body``/``else_body`` are the already-inferred arms.

    Returns the (possibly rewritten) ``(body, else_body)`` arms: the scope
    binding for each widened local is set to ``DynType`` and its conflicting
    assignment targets are retargeted to ``DynType`` so the store path builds a
    single boxed-object slot.
    """
    then_assigned: dict = {}
    else_assigned: dict = {}
    annotated: dict = {}
    _typeconf_collect_arm_assignments(body, then_assigned, annotated)
    _typeconf_collect_arm_assignments(else_body, else_assigned, annotated)

    names: list = []
    for name in then_assigned:
        if name not in names:
            names.append(name)
    for name in else_assigned:
        if name not in names:
            names.append(name)

    widen: dict = {}
    for name in names:
        if annotated.get(name):
            # Annotated locals keep their declared slot; a conflicting
            # annotated reassignment is caught elsewhere as a type error.
            continue
        then_ty = then_assigned.get(name)
        else_ty = else_assigned.get(name)
        # A name assigned on only one arm joins against its pre-``if`` type on
        # the fall-through path. If it was undefined before the ``if`` there is
        # no cross-arm confusion (the other path leaves it unbound), so skip.
        pre_ty = pre_types.get(name)
        cand_then = then_ty if then_ty is not None else pre_ty
        cand_else = else_ty if else_ty is not None else pre_ty
        if cand_then is None or cand_else is None:
            continue
        widened = _typeconf_join_type(cand_then, cand_else)
        if widened is not None:
            scope.update(name, widened)
            widen[name] = True
        elif _typeconf_container_joined_with_unknown(cand_then, cand_else):
            # Same storage slot, but a *container-specialised* type joined
            # with an unknown one.  `_typeconf_join_type` answers a storage
            # question ("do the arms need one boxed slot"); a list/tuple and a
            # `dyn` already share the PyObject* slot, so it reports no
            # widening -- and the arm's ListType/TupleType stayed in scope.
            #
            # `for x in xs` then lowers to `py_list_len`/`py_tuple_get` with
            # no runtime check (`for_loop_lowering._emit_for_list_index`), so
            # `if xs is None: xs = <tuple field>` -- the ordinary default-
            # argument idiom -- made a caller-supplied generator get read
            # through the tuple layout: garbage rows, then SIGSEGV.
            #
            # Only the recorded type widens; the arms keep their storage, and
            # the rule is confined to container-vs-unknown so struct-backed
            # bindings are untouched.
            scope.update(name, TYPE_DYN)

    if not widen:
        return (body, else_body)
    new_body = _typeconf_retarget_arm(body, widen)
    new_else = _typeconf_retarget_arm(else_body, widen)
    return (new_body, new_else)


# ---------------------------------------------------------------------------
# Statement inference
# ---------------------------------------------------------------------------


def _bind_for_tuple_target(
    ctx: _InferCtx,
    scope: _Scope,
    target: TupleExpr,
    elem_ty: Type,
) -> Expr:
    """Bind a for-loop tuple-unpack target's names into ``scope``.

    Mirrors the single-name for-target rule: a name's type is its unpacked
    slot type, joined to dyn when a pre-loop binding of a different type
    exists (empty-iterable edge keeps the old value).  Nested tuples recurse;
    non-name elements (subscripts, attributes) keep their inferred form and
    bind nothing, exactly as before.
    """
    resolved = ctx.resolve_type_refs(elem_ty)
    # Bind every element as DYN, deliberately NOT as its precise slot type.
    # Before this fix these names were never bound at all and resolved through
    # `lookup_name`'s TYPE_DYN fallback, so a DYN binding is observably
    # identical for every non-shadowed name -- the entire behavior change is
    # confined to the bug being fixed (a binding now exists, so it shadows
    # the enclosing method's recursion seed).  Precise per-slot types were
    # tried first: host-side gates all passed (51 green) but the stage1
    # pcc1 could no longer compile even the two-line smoke -- a host-green/
    # pcc1-red divergence typed-int lane changes are known to cause.  Slot
    # precision is its own slice with its own stage gate, not a rider.
    # Class-typed slots are precise (pointers stay pointers); every other
    # slot keeps the dyn binding described above.
    slot_types: tuple[Type, ...] = tuple(TYPE_DYN for _ in target.elems)
    if isinstance(resolved, TupleType) and len(resolved.elems) == len(target.elems):
        slot_types = tuple(
            _class_only(ctx.resolve_type_refs(e)) for e in resolved.elems
        )
    new_elems: list[Expr] = []
    for element, slot_ty in zip(target.elems, slot_types):
        if isinstance(element, Name):
            element_ident = _name_ident(element)
            if element_ident is not None:
                pre_loop_ty = scope.lookup(element_ident)
                bound_ty = slot_ty
                if pre_loop_ty is not None and not type_eq(pre_loop_ty, slot_ty):
                    bound_ty = TYPE_DYN
                scope.update(element_ident, bound_ty)
                new_elems.append(_with_ty(element, bound_ty))
                continue
            new_elems.append(_with_ty(element, slot_ty))
        elif isinstance(element, TupleExpr):
            new_elems.append(
                _bind_for_tuple_target(ctx, scope, element, slot_ty)
            )
        else:
            new_elems.append(_infer_expr(ctx, scope, element))
    return replace(target, elems=tuple(new_elems), ty=resolved)


def _infer_stmt(ctx: _InferCtx, scope: _Scope, stmt: Stmt) -> Stmt:
    if isinstance(stmt, FuncDef):
        return _infer_funcdef(ctx, scope, stmt)

    if isinstance(stmt, Assign):
        return _infer_assign(ctx, scope, stmt)

    if isinstance(stmt, AugAssign):
        target = _infer_expr(ctx, scope, stmt.target)
        value = _infer_expr(ctx, scope, stmt.value)
        target, value = _contextualize_raw_int_operands(
            target,
            value,
            stmt.span,
        )
        # Re-bind the target's type to the promoted result so subsequent
        # statements see the refined type.
        if isinstance(stmt.target, Name):
            new_ty = _binop_result(stmt.op[:-1], target.ty, value.ty, stmt.span)
            target_ident = _name_ident(stmt.target)
            if target_ident is not None:
                scope.update(target_ident, new_ty)
        return replace(stmt, target=target, value=value)

    if isinstance(stmt, ExprStmt):
        expr = _infer_expr(ctx, scope, stmt.expr)
        return replace(stmt, expr=expr)

    if isinstance(stmt, Return):
        if stmt.value is None:
            return stmt
        value = _infer_expr(ctx, scope, stmt.value)
        return replace(stmt, value=value)

    if isinstance(stmt, If):
        cond = _infer_expr(ctx, scope, stmt.cond)
        body_scope = _narrow_scope_for_cond(ctx, scope, cond)
        # Snapshot the pre-``if`` types of every simple local either arm might
        # rebind, so a name assigned on only one arm can be joined against its
        # fall-through type. Captured before inference runs so it reflects the
        # state both arms actually branch from.
        pre_types: dict = {}
        pre_annotated: dict = {}
        _typeconf_collect_arm_assignments(stmt.body, pre_types, pre_annotated)
        _typeconf_collect_arm_assignments(stmt.else_body, pre_types, pre_annotated)
        pre_names: list = []
        for name in pre_types:
            pre_names.append(name)
        for name in pre_names:
            pre_types[name] = scope.lookup(name)
        body = tuple(_infer_stmt(ctx, body_scope, s) for s in stmt.body)
        else_body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.else_body)
        # Forced-object widening (SEC-P1-TYPECONF): if the two arms leave a
        # shared local in incompatible storage slots, rebind it to a boxed
        # object slot so the control-flow join reads a real value, not a
        # type-confused raw payload. Applied to the shared ``scope`` only;
        # the arms were inferred exactly as before this pass existed.
        body, else_body = _typeconf_widen_if_join(scope, pre_types, body, else_body)
        return replace(stmt, cond=cond, body=body, else_body=else_body)

    if isinstance(stmt, While):
        cond = _infer_expr(ctx, scope, stmt.cond)
        body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.body)
        else_body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.else_body)
        return replace(stmt, cond=cond, body=body, else_body=else_body)

    if isinstance(stmt, For):
        iter_e = _infer_expr(ctx, scope, stmt.iter)
        # Phase 1: loop variable type = element type of the iterable if
        # we can discover it; else dyn.
        elem_ty = ctx.resolve_type_refs(
            _element_type_of(ctx.resolve_type_refs(iter_e.ty))
        )
        if isinstance(elem_ty, DynType):
            # Call forms (enumerate/zip/dict views): adopt class-typed
            # results only -- tuple targets keep class slots, names classes.
            call_elem = ctx.resolve_type_refs(
                _iteration_element_type(ctx, scope, iter_e)
            )
            if isinstance(stmt.target, TupleExpr):
                elem_ty = _class_only_slots(call_elem)
            else:
                elem_ty = _class_only(call_elem)
        if (
            isinstance(iter_e, Call)
            and isinstance(iter_e.func, Name)
            and _name_ident(iter_e.func) in ("range", "xrange")
        ):
            # ``range`` is represented as Dyn at the builtin-call boundary,
            # but its iteration value is always a Python int.  Preserve that
            # semantic fact here so an ordinary pre-bound int target keeps
            # the native-i64 loop lane.
            elem_ty = TYPE_INT
        target = _infer_expr(ctx, scope, stmt.target)
        if isinstance(stmt.target, Name):
            target_ident = _name_ident(stmt.target)
            if target_ident is not None:
                pre_loop_ty = scope.lookup(target_ident)
                target_ty = elem_ty
                if pre_loop_ty is not None and not type_eq(pre_loop_ty, elem_ty):
                    # A Python for-target is unchanged when the iterable is
                    # empty and rebound on every non-empty iteration.  Those
                    # two control-flow edges therefore need one representation
                    # join.  Dyn is the honest boxed projection when the
                    # incoming value and element type differ; lowering must
                    # migrate the pre-loop value into that slot rather than
                    # replacing it with NULL or storing a pointer into the old
                    # scalar alloca.
                    target_ty = TYPE_DYN
                scope.update(target_ident, target_ty)
            else:
                target_ty = elem_ty
            target = _with_ty(stmt.target, target_ty)
        elif isinstance(stmt.target, TupleExpr):
            # Tuple-unpack targets were never bound into the scope at all, so
            # each element name resolved through the enclosing chain.  Mostly
            # that fell through to dyn and accidentally worked -- but a method
            # whose OWN name matches an element found the recursion seed
            # (`param_scope.define(fn.name, ft)` in `_infer_funcdef`) and
            # inferred 'callable', rejecting legal Python:
            #     def value_id(self, name):
            #         for existing, value_id in self.rows: return value_id
            # Bind every element like the single-name branch above: per-slot
            # types when the element type is a tuple of matching arity, dyn
            # otherwise, with the same pre-loop representation join.
            target = _bind_for_tuple_target(ctx, scope, stmt.target, elem_ty)
        body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.body)
        else_body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.else_body)
        return replace(
            stmt,
            target=target,
            iter=iter_e,
            body=body,
            else_body=else_body,
        )

    if isinstance(stmt, Raise):
        exc = _infer_expr(ctx, scope, stmt.exc) if stmt.exc is not None else None
        cause = _infer_expr(ctx, scope, stmt.cause) if stmt.cause is not None else None
        return replace(stmt, exc=exc, cause=cause)

    if isinstance(stmt, Try):
        # Pre-``try`` types of every simple local the statement rebinds, for
        # the exception edges that leave a body binding unexecuted.
        pre_types: dict = {}
        pre_annotated: dict = {}
        _typeconf_collect_try_candidates(stmt.body, pre_types, pre_annotated)
        for handler in stmt.handlers:
            _typeconf_collect_arm_assignments(handler.body, pre_types, pre_annotated)
        _typeconf_collect_arm_assignments(stmt.else_body, pre_types, pre_annotated)
        _typeconf_collect_arm_assignments(stmt.finally_body, pre_types, pre_annotated)
        pre_names: list = []
        for name in pre_types:
            pre_names.append(name)
        for name in pre_names:
            pre_types[name] = scope.lookup(name)
        body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.body)
        handlers = tuple(_infer_handler(ctx, scope, h) for h in stmt.handlers)
        else_body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.else_body)
        finally_body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.finally_body)
        body, handlers, else_body, finally_body = _typeconf_widen_try_join(
            scope, pre_types, body, handlers, else_body, finally_body,
        )
        return replace(
            stmt,
            body=body,
            handlers=handlers,
            else_body=else_body,
            finally_body=finally_body,
        )

    if isinstance(stmt, With):
        new_items = []
        for ctx_expr, as_var in stmt.items:
            new_ctx = _infer_expr(ctx, scope, ctx_expr)
            if as_var is not None:
                new_as = _infer_expr(ctx, scope, as_var)
                if isinstance(as_var, Name):
                    as_var_ident = _name_ident(as_var)
                    if as_var_ident is not None:
                        scope.update(as_var_ident, TYPE_DYN)
            else:
                new_as = None
            new_items.append((new_ctx, new_as))
        body = tuple(_infer_stmt(ctx, scope, s) for s in stmt.body)
        return replace(stmt, items=tuple(new_items), body=body)

    if isinstance(stmt, Delete):
        targets = tuple(_infer_expr(ctx, scope, t) for t in stmt.targets)
        return replace(stmt, targets=targets)

    if isinstance(stmt, ClassDef):
        # Phase 1 does not type the body of classes; leave the class
        # node alone but still walk the body so nested funcs get typed.
        # Annotation declarations retain type metadata without binding a
        # class-body name. Initializer presence is a separate syntax fact.
        method_arg_overrides: dict[str, dict[int, Type]] = {}
        final_body: tuple[Stmt, ...] = ()
        forwardable = _class_forwardable_params(stmt.body)
        for _round in range(4):
            class_scope = _Scope(parent=scope)
            new_body: list = []
            for s in stmt.body:
                if (
                    isinstance(s, Assign)
                    and not s.has_value
                    and len(s.targets) == 1
                    and isinstance(s.targets[0], Name)
                ):
                    new_body.append(_infer_assign(ctx, class_scope, s))
                    continue
                if (isinstance(s, Assign) and s.has_value
                        and _annotation_or_none(s) is not None
                        and isinstance(s.value, NoneLit)):
                    # Class annotations do not enforce a runtime type. Keep
                    # explicit None initializers as real class assignments,
                    # including annotations naming scalar types.
                    new_body.append(s)
                    continue
                if isinstance(s, FuncDef):
                    self_ty = ctx.class_types.get(stmt.name)
                    # Mixin self_ty propagation: if the current class is a
                    # base of exactly one derived class anywhere in the
                    # multi-file closure, type-infer the mixin's method
                    # bodies with ``self_ty=derived_class`` so cross-module
                    # ``self.X`` resolves against the derived class's full
                    # field schema. The mixin's IR still lives in the mixin
                    # module — only the type used for resolution changes.
                    derived_entry = ctx.derived_class_map.get(stmt.name)
                    if derived_entry is not None:
                        derived_mod, derived_name = derived_entry
                        derived_ty = ctx.class_types.get(
                            f"{derived_mod}.{derived_name}"
                        )
                        if derived_ty is None:
                            derived_ty = ctx.class_types.get(derived_name)
                        if derived_ty is not None:
                            self_ty = derived_ty
                    if (
                        self_ty is not None
                        and per_module_probe_policy(_ctx_module_name(ctx))
                        == PROBE_POLICY_CONTEXTUAL_MIXIN
                        and self_ty.name != "L1CodeGen"
                    ):
                        self_ty = ctx.l1_codegen_host_type()
                    new_body.append(
                        _infer_funcdef(
                            ctx,
                            class_scope,
                            s,
                            self_ty=self_ty,
                            arg_overrides=method_arg_overrides.get(s.name),
                        )
                    )
                    continue
                new_body.append(_infer_stmt(ctx, class_scope, s))
            final_body = tuple(new_body)
            collected = _collect_self_method_arg_overrides(
                ctx, final_body, forwardable, method_arg_overrides,
            )
            method_arg_overrides, changed = _merge_method_arg_overrides(
                method_arg_overrides,
                collected,
            )
            if not changed:
                break
        return replace(stmt, body=final_body)

    # Import/Global/Nonlocal/Pass/Break/Continue — mostly pass through.
    if isinstance(stmt, Import):
        for mod_name, as_name in stmt.names:
            if mod_name == "pcc":
                local_name = as_name or "pcc"
                ctx.pcc_module_aliases.add(local_name)
                scope.update(local_name, DynType(name="module:pcc"))
                continue
            if mod_name == "math":
                local_name = as_name or mod_name.split(".", 1)[0]
                scope.update(local_name, DynType(name="module:math"))
                continue
            if mod_name == "functools":
                local_name = as_name or mod_name.split(".", 1)[0]
                scope.update(local_name, DynType(name="module:functools"))
                ctx.functools_module_aliases.add(local_name)
            # Cross-module class registration: when ``mod_name`` was
            # supplied through the multi-file pre-pass (or pulled in by
            # the recursive_stdlib walker), eagerly bind each exported
            # class under the qualified key ``<alias>.<ClassName>`` so
            # ``alias.Class(args)`` types as an instance constructor.
            # Without this, ``import pathlib; pathlib.PurePath(...)``
            # bottoms out at ``DynType`` and downstream property
            # accesses (``p.name``) fall back to ``py_obj_getattr``,
            # silently returning the property descriptor instead of
            # invoking the getter. See investigation
            # pcc-py-type-infer-property-return-type.md.
            _bind_external_module_exports(ctx, mod_name, as_name or mod_name)
        return stmt

    # For ImportFrom against a registered native sibling module we
    # bind the imported names in the current scope to the remote
    # function / class type so downstream call-site and attribute
    # inference picks the concrete type rather than DynType.
    if isinstance(stmt, ImportFrom):
        resolved = _resolve_relative_module(
            _import_from_module_or_empty(stmt),
            _import_from_level_or_zero(stmt),
            _ctx_module_name(ctx),
            stmt.span.file,
        )
        if resolved == "dataclasses":
            for attr_name, as_name in stmt.names:
                if attr_name != "replace":
                    continue
                local_name = as_name or attr_name
                ft = FuncType(
                    name="callable",
                    params=(TYPE_DYN,),
                    ret=TYPE_DYN,
                )
                ctx.dataclasses_replace_aliases.add(local_name)
                scope.update(local_name, ft)
                ctx.func_types[local_name] = ft
        if resolved == "weakref":
            for attr_name, as_name in stmt.names:
                if attr_name not in ("ref", "proxy"):
                    continue
                local_name = as_name or attr_name
                ctx.weakref_value_aliases.add(local_name)
        if resolved == "math":
            for attr_name, as_name in stmt.names:
                if attr_name not in ("floor", "ceil", "sqrt", "trunc", "gcd"):
                    continue
                local_name = as_name or attr_name
                ret_ty: Type = TYPE_FLOAT if attr_name == "sqrt" else TYPE_INT
                params = (TYPE_DYN, TYPE_DYN) if attr_name == "gcd" else (TYPE_DYN,)
                ft = FuncType(
                    name="callable",
                    params=params,
                    ret=ret_ty,
                )
                scope.update(local_name, ft)
                ctx.func_types[local_name] = ft
        if resolved == "asyncio":
            for attr_name, as_name in stmt.names:
                if attr_name not in ("run", "sleep"):
                    continue
                local_name = as_name or attr_name
                ft = FuncType(
                    name="callable",
                    params=(TYPE_DYN,),
                    ret=TYPE_DYN,
                )
                scope.update(local_name, ft)
                ctx.func_types[local_name] = ft
        if resolved == "pcc.unsafe":
            for attr_name, as_name in stmt.names:
                ret_ty = _unsafe_intrinsic_return_type(ctx, attr_name)
                if ret_ty is None:
                    continue
                local_name = as_name or attr_name
                ctx.unsafe_intrinsic_aliases.add(local_name)
                ft = FuncType(
                    name="callable",
                    params=(TYPE_DYN,),
                    ret=ret_ty,
                )
                scope.update(local_name, ft)
                ctx.func_types[local_name] = ft
        if resolved == "pcc":
            for attr_name, as_name in stmt.names:
                local_name = as_name or attr_name
                if attr_name == "i64_buffer":
                    ctx.pcc_i64_buffer_aliases.add(local_name)
                    scope.update(local_name, TYPE_DYN)
                elif attr_name == "guarded_i64_dot":
                    ctx.pcc_guarded_i64_dot_aliases.add(local_name)
                    ft = FuncType(
                        name="callable",
                        params=(TYPE_BYTES, TYPE_BYTES),
                        ret=TYPE_INT,
                    )
                    scope.update(local_name, ft)
                    ctx.func_types[local_name] = ft
                elif attr_name == "guarded_loop_counter":
                    ctx.pcc_guarded_loop_counter_aliases.add(local_name)
                    ft = FuncType(
                        name="callable",
                        params=(TYPE_STR,),
                        ret=TYPE_INT,
                    )
                    scope.update(local_name, ft)
                    ctx.func_types[local_name] = ft
        if resolved == "pcc.extern":
            for attr_name, as_name in stmt.names:
                local_name = as_name or attr_name
                if attr_name == "extern":
                    ctx.extern_factory_aliases.add(local_name)
                elif attr_name == "ExternFn":
                    ctx.extern_fn_type_aliases.add(local_name)
                elif attr_name.startswith("c_"):
                    ctx.extern_ctype_aliases[local_name] = attr_name
        _bind_ir_compat_module_alias(ctx, scope, resolved, stmt.names)
        if ctx.external_exports:
            _bind_external_import_exports(
                ctx, scope, resolved, _import_names_not_shadowed(ctx, stmt),
            )
        if resolved == "functools":
            for attr_name, as_name in stmt.names:
                if attr_name != "partial":
                    continue
                local_name = as_name or attr_name
                ft = FuncType(
                    name="callable",
                    params=(TYPE_DYN,),
                    ret=TYPE_DYN,
                )
                scope.update(local_name, ft)
                ctx.func_types[local_name] = ft
        return stmt
    if isinstance(stmt, (Import, Global, Nonlocal, Pass, Break, Continue)):
        return stmt

    # Anything unhandled: return unchanged rather than crash.
    return stmt


def _infer_handler(
    ctx: _InferCtx, scope: _Scope, handler: ExceptHandler
) -> ExceptHandler:
    exc_type = (
        _infer_expr(ctx, scope, handler.exc_type)
        if handler.exc_type is not None
        else None
    )
    if handler.name is not None:
        scope.update(handler.name, TYPE_DYN)
    body = tuple(_infer_stmt(ctx, scope, s) for s in handler.body)
    return replace(handler, exc_type=exc_type, body=body)


def _element_type_of(ty: Type) -> Type:
    if isinstance(ty, ListType):
        return ty.elem
    if isinstance(ty, SetType):
        return ty.elem
    if isinstance(ty, DictType):
        return ty.key
    if isinstance(ty, TupleType):
        if ty.elems:
            first = ty.elems[0]
            if all(type_eq(first, e) for e in ty.elems):
                return first
        return TYPE_DYN
    if isinstance(ty, StrType):
        return TYPE_STR
    return TYPE_DYN


def _class_only(ty: Type) -> Type:
    """``ty`` when it is a class, else dyn (class-typed precision only)."""
    if isinstance(ty, ClassType):
        return ty
    return TYPE_DYN


def _class_only_slots(ty: Type) -> Type:
    """A tuple element type with only its class-typed slots kept precise."""
    if isinstance(ty, TupleType) and ty.elems:
        return TupleType(name="tuple", elems=tuple(_class_only(e) for e in ty.elems))
    return _class_only(ty)


def _iteration_element_type(ctx: "_InferCtx", scope: "_Scope", iter_e: Expr) -> Type:
    """Element type of iterating ``iter_e`` (already inferred).

    Beyond ``_element_type_of``: ``enumerate``/``zip`` yield tuples of their
    arguments' elements, dict views yield keys/values/(key, value), and
    ``reversed``/``sorted`` yield their argument's elements.  The call forms
    were plain dyn, so ``for i, section in enumerate(sections)`` read every
    ``section.field`` through a dynamic getattr in pcc1.  Only class-typed
    results are used by the callers (see ``_class_only``).
    """
    base = _element_type_of(ctx.resolve_type_refs(iter_e.ty))
    if not isinstance(iter_e, Call) or iter_e.kwargs:
        return base
    func = iter_e.func
    if isinstance(func, Name):
        name = _name_ident(func)
        if name is None or scope.lookup(name) is not None:
            return base
        args = iter_e.args
        if name == "enumerate" and 1 <= len(args) <= 2:
            inner = _element_type_of(ctx.resolve_type_refs(args[0].ty))
            return TupleType(name="tuple", elems=(TYPE_INT, inner))
        if name == "zip" and args:
            return TupleType(
                name="tuple",
                elems=tuple(
                    _element_type_of(ctx.resolve_type_refs(a.ty)) for a in args
                ),
            )
        if name in ("reversed", "sorted") and len(args) == 1:
            return _element_type_of(ctx.resolve_type_refs(args[0].ty))
        return base
    if isinstance(func, Attr) and not iter_e.args:
        receiver = ctx.resolve_type_refs(func.obj.ty)
        if isinstance(receiver, DictType):
            if func.name == "items":
                return TupleType(name="tuple", elems=(receiver.key, receiver.value))
            if func.name == "keys":
                return receiver.key
            if func.name == "values":
                return receiver.value
    return base


_CPY_COMPREHENSION_SENTINELS = ("__listcomp__", "__setcomp__", "__genexpr__", "__dictcomp__")
_NATIVE_COMPREHENSION_SENTINELS = ("_list_comp", "_set_comp", "_gen_comp", "_dict_comp")


def _bind_comprehension_target(
    ctx: "_InferCtx", comp_scope: "_Scope", target: Expr, iter_typed: Expr
) -> Expr:
    elem = ctx.resolve_type_refs(_iteration_element_type(ctx, comp_scope, iter_typed))
    if isinstance(target, Name):
        ident = _name_ident(target)
        bound = _class_only(elem)
        if ident is not None:
            comp_scope.define(ident, bound)
        return _with_ty(target, bound)
    if isinstance(target, TupleExpr):
        return _bind_for_tuple_target(ctx, comp_scope, target, elem)
    return _infer_expr(ctx, comp_scope, target)


def _infer_comprehension_args(
    ctx: "_InferCtx", scope: "_Scope", expr: Call
) -> Optional[tuple[Expr, ...]]:
    """Infer a comprehension sentinel's arguments in the comprehension scope.

    Generator targets were never bound, so the element expression resolved a
    target through the enclosing scope: dyn at best (every ``p.field`` a
    dynamic getattr in pcc1), an unrelated outer binding of the same name at
    worst.  Iterables and targets are inferred first, in a child scope, then
    the element.  Both the CPython-AST form (host) and the native parser's
    ``_gen_clause`` form (pcc1) take the same route, so the fixed point holds.
    Targets gain class types only (``_class_only``).
    """
    if not isinstance(expr.func, Name):
        return None
    sentinel = _name_ident(expr.func)
    if sentinel is None or scope.lookup(sentinel) is not None:
        return None
    comp_scope = _Scope(parent=scope)
    if sentinel in _CPY_COMPREHENSION_SENTINELS:
        element_count = 2 if sentinel == "__dictcomp__" else 1
        if len(expr.args) != element_count + 1:
            return None
        generators = expr.args[element_count]
        if not isinstance(generators, TupleExpr):
            return None
        for clause in generators.elems:
            if not isinstance(clause, TupleExpr) or len(clause.elems) != 4:
                return None
        new_clauses = []
        for clause in generators.elems:
            target, iter_e, ifs, is_async = clause.elems
            iter_typed = _infer_expr(ctx, comp_scope, iter_e)
            target_typed = _bind_comprehension_target(ctx, comp_scope, target, iter_typed)
            new_clauses.append(replace(clause, elems=(
                target_typed,
                iter_typed,
                _infer_expr(ctx, comp_scope, ifs),
                _infer_expr(ctx, comp_scope, is_async),
            )))
        elements = tuple(
            _infer_expr(ctx, comp_scope, a) for a in expr.args[:element_count]
        )
        return elements + (replace(generators, elems=tuple(new_clauses)),)
    if sentinel in _NATIVE_COMPREHENSION_SENTINELS:
        if len(expr.args) < 2:
            return None
        for clause in expr.args[1:]:
            if not (
                isinstance(clause, Call)
                and isinstance(clause.func, Name)
                and _name_ident(clause.func) == "_gen_clause"
                and len(clause.args) == 3
                and not clause.kwargs
            ):
                return None
        new_clauses = []
        for clause in expr.args[1:]:
            target, iter_e, ifs = clause.args
            iter_typed = _infer_expr(ctx, comp_scope, iter_e)
            target_typed = _bind_comprehension_target(ctx, comp_scope, target, iter_typed)
            new_clauses.append(replace(
                clause,
                func=_with_ty(clause.func, TYPE_DYN),
                args=(target_typed, iter_typed, _infer_expr(ctx, comp_scope, ifs)),
            ))
        element = _infer_expr(ctx, comp_scope, expr.args[0])
        return (element,) + tuple(new_clauses)
    return None


def _type_from_isinstance_arg(
    ctx: _InferCtx,
    expr: Expr,
) -> Optional[Type]:
    """Resolve ``isinstance``'s second argument when it is one type.

    Tuple forms describe a union. The frontend has no union type yet, so
    we deliberately leave those unnarrowed instead of guessing.
    """
    if isinstance(expr, Name):
        expr_ident = _name_ident(expr)
        if expr_ident == "tuple":
            return TupleType(name="tuple_variadic", elems=(TYPE_DYN,))
        if expr_ident == "list":
            return ListType(name="list", elem=TYPE_DYN)
        if expr_ident == "dict":
            return DictType(name="dict", key=TYPE_DYN, value=TYPE_DYN)
        if expr_ident == "str":
            return TYPE_STR
        if expr_ident == "int":
            return TYPE_INT
        if expr_ident == "bool":
            return TYPE_BOOL
        if expr_ident == "float":
            return TYPE_FLOAT
        if expr_ident is not None and ctx.external_exports:
            module_exports = ctx.external_exports.get("pcc.frontends.python.py_ast")
            if module_exports is not None:
                ref_info = module_exports.get(expr_ident)
                if isinstance(ref_info, dict) and ref_info.get("kind") == "class":
                    return _class_type_from_export(
                        ctx,
                        "pcc.frontends.python.py_ast",
                        ref_info,
                        module_exports,
                        {},
                    )
        ty = ctx.resolve_type_refs(expr.ty)
        if isinstance(ty, ClassType):
            return ty
        found = ctx.class_types.get(expr_ident) if expr_ident is not None else None
        if found is not None:
            return found
    return None


def _narrow_scope_for_cond(ctx: _InferCtx, scope: _Scope, cond: Expr) -> _Scope:
    if isinstance(cond, BoolExpr) and cond.op == "and":
        narrowed_left = _narrow_scope_for_cond(ctx, scope, cond.left)
        return _narrow_scope_for_cond(ctx, narrowed_left, cond.right)
    return _narrow_scope_for_isinstance(ctx, scope, cond)


def _narrow_scope_for_isinstance(
    ctx: _InferCtx,
    scope: _Scope,
    cond: Expr,
) -> _Scope:
    if not (
        isinstance(cond, Call)
        and isinstance(cond.func, Name)
        and _name_ident(cond.func) == "isinstance"
        and len(cond.args) == 2
        and not cond.kwargs
        and isinstance(cond.args[0], Name)
    ):
        return scope
    candidate = _type_from_isinstance_arg(ctx, cond.args[1])
    if candidate is None:
        return scope
    var_name = _name_ident(cond.args[0])
    if var_name is None:
        return scope
    current = scope.lookup(var_name)
    if current is None:
        return scope
    if isinstance(current, DynType):
        narrowed = _Scope(parent=scope)
        narrowed.update(var_name, candidate)
        return narrowed
    if not (
        isinstance(current, ClassType)
        and isinstance(candidate, ClassType)
        and _class_type_assignable(current, candidate)
    ):
        return scope
    narrowed = _Scope(parent=scope)
    narrowed.update(var_name, candidate)
    return narrowed


def _import_from_module_or_empty(stmt) -> str:
    try:
        module = stmt.module
    except AttributeError:
        return ""
    return module or ""


def _import_from_level_or_zero(stmt) -> int:
    try:
        level = stmt.level
    except AttributeError:
        return 0
    return level or 0


def _class_type_name(ty) -> str:
    try:
        return ty.name
    except AttributeError:
        return ""


def _class_type_module(ty) -> str:
    try:
        module = ty.module
    except AttributeError:
        return ""
    return module or ""


def _class_type_fields(ty):
    try:
        fields = ty.fields
    except AttributeError:
        return ()
    return fields or ()


def _class_type_bases(ty):
    try:
        bases = ty.bases
    except AttributeError:
        return ()
    return bases or ()


def _class_key_seen(
    seen_modules: list[str],
    seen_names: list[str],
    module: str,
    name: str,
) -> bool:
    i = 0
    while i < len(seen_names):
        if seen_names[i] == name and seen_modules[i] == module:
            return True
        i += 1
    return False


def _class_mro_list(cls_ty: ClassType) -> list[ClassType]:
    """Return ``cls_ty`` and its bases breadth-first, guarding cycles.

    This is intentionally list-based rather than a generator plus ``set``:
    self-hosted pcc runs this in a very hot type-inference path, and the
    generator/set shape allocates heavily under the pcc-Python runtime.
    """
    seen_modules: list[str] = []
    seen_names: list[str] = []
    queue: list[ClassType] = [cls_ty]
    out: list[ClassType] = []
    idx = 0
    while idx < len(queue):
        cur = queue[idx]
        idx += 1
        cur_name = _class_type_name(cur)
        cur_module = _class_type_module(cur)
        if _class_key_seen(seen_modules, seen_names, cur_module, cur_name):
            continue
        seen_modules.append(cur_module)
        seen_names.append(cur_name)
        out.append(cur)
        bases = _class_type_bases(cur)
        i = 0
        while i < len(bases):
            queue.append(bases[i])
            i += 1
    return out


def _iter_class_mro(cls_ty: ClassType):
    for cur in _class_mro_list(cls_ty):
        yield cur


def _lookup_class_attr_type(cls_ty: ClassType, attr_name: str) -> Optional[Type]:
    mro = _class_mro_list(cls_ty)
    i = 0
    while i < len(mro):
        cur = mro[i]
        for pname, pty in getattr(cur, "properties", ()):
            if pname == attr_name:
                return pty
        i += 1

    i = 0
    while i < len(mro):
        cur = mro[i]
        cur_name = _class_type_name(cur)
        cur_module = _class_type_module(cur)
        if (
            cur_name == "ClassLowering"
            and cur_module == "pcc.frontends.python.codegen.class_gen"
        ):
            if attr_name == "classes":
                return _make_dict_type(TYPE_STR, TYPE_DYN)
            if attr_name in _CLASS_LOWERING_HOST_METHODS:
                return _make_func_type((TYPE_DYN,), TYPE_DYN)
        for fname, fty in _class_type_fields(cur):
            if fname == attr_name:
                return fty
        i += 1
    return None


def _lookup_class_field(cls_ty: ClassType, field_name: str) -> Optional[Type]:
    mro = _class_mro_list(cls_ty)
    i = 0
    while i < len(mro):
        cur = mro[i]
        cur_name = _class_type_name(cur)
        cur_module = _class_type_module(cur)
        if (
            cur_name == "ClassLowering"
            and cur_module == "pcc.frontends.python.codegen.class_gen"
        ):
            if field_name == "classes":
                return _make_dict_type(TYPE_STR, TYPE_DYN)
            if field_name in _CLASS_LOWERING_HOST_METHODS:
                return _make_func_type((TYPE_DYN,), TYPE_DYN)
        for fname, fty in _class_type_fields(cur):
            if fname == field_name:
                return fty
        i += 1
    return None


def _lookup_class_property(cls_ty: ClassType, prop_name: str) -> Optional[Type]:
    """MRO walk for ``@property`` declarations. Mirrors
    ``_lookup_class_field`` but searches ``ClassType.properties``."""
    mro = _class_mro_list(cls_ty)
    i = 0
    while i < len(mro):
        cur = mro[i]
        for pname, pty in getattr(cur, "properties", ()):
            if pname == prop_name:
                return pty
        i += 1
    return None


def _class_bases_from_def(ctx: _InferCtx, stmt: ClassDef) -> tuple[ClassType, ...]:
    from pcc.frontends.python.pipeline_closed_world import qualified_class_base_name, resolve_class_base_export

    bases: list[ClassType] = []
    for base_expr in stmt.bases:
        if isinstance(base_expr, Attr):
            # Prepopulation runs before ordinary import-statement inference.
            # Resolve the source's qualified binding against the export table
            # now, retaining its owning module even when leaf names collide.
            base_name = qualified_class_base_name(
                base_expr, ctx.module.body, stmt, _ctx_module_name(ctx), stmt.span.file,
            )
            resolved = resolve_class_base_export(ctx.external_exports, _ctx_module_name(ctx), base_name)
            if resolved is not None:
                owner, exported = resolved
                bases.append(_class_type_from_export(ctx, owner, exported, ctx.external_exports[owner], {}))
            continue
        if not isinstance(base_expr, Name):
            continue
        base_ident = _name_ident(base_expr)
        if base_ident is None:
            continue
        if base_ident == "object":
            continue
        base_ty = ctx.class_types.get(base_ident)
        if isinstance(base_ty, ClassType):
            bases.append(base_ty)
        else:
            bases.append(
                _make_class_type(
                    base_ident,
                    _ctx_module_name(ctx),
                    (),
                    (),
                )
            )
    return tuple(bases)


def _append_field(
    fields: list[tuple[str, Type]],
    name: str,
    field_ty: Type,
) -> None:
    for i, (existing, _old_ty) in enumerate(fields):
        if existing == name:
            fields[i] = (name, field_ty)
            return
    fields.append((name, field_ty))


def _is_property_decorator(dec) -> bool:
    """True if ``dec`` is the ``@property`` decorator.

    Accepts the bare ``Name("property")`` form and the qualified
    ``Attr(Name("builtins"), "property")`` form. ``@<name>.setter`` /
    ``.deleter`` are intentionally NOT recognised — Gap 2 is read-only
    property support; setter/deleter are out of scope until needed by
    pcc's self-host surface.
    """
    if isinstance(dec, Name):
        return _name_ident(dec) == "property"
    if isinstance(dec, Attr):
        return (
            dec.name == "property"
            and isinstance(dec.obj, Name)
            and _name_ident(dec.obj) == "builtins"
        )
    return False


def _simple_decorator_name(dec) -> Optional[str]:
    if isinstance(dec, Name):
        return _name_ident(dec)
    if isinstance(dec, Attr) and isinstance(dec.obj, Name):
        obj_ident = _name_ident(dec.obj)
        if obj_ident is None:
            return None
        return f"{obj_ident}.{dec.name}"
    if isinstance(dec, Call):
        return _simple_decorator_name(dec.func)
    return None


def _class_has_valueclass_decorator(stmt: ClassDef) -> bool:
    for dec in stmt.decorators:
        if _simple_decorator_name(dec) in ("valueclass", "pcc.valueclass"):
            return True
    return False


def _raise_frontend_error(
    span: Optional[SourceSpan],
    message: str,
    hint: str,
) -> None:
    raise PyFrontendError(span, message, hint)


def _is_valueclass_type(ty: Type) -> bool:
    if isinstance(ty, ClassType):
        return ty.valueclass
    return False


def _validate_value_array_type(
    ty: Type,
    span: Optional[SourceSpan],
) -> Type:
    if not isinstance(ty, ValueArrayType):
        return ty
    if ty.length == -1:
        _raise_frontend_error(
            span,
            "pcc.array needs an element type and literal length",
            "use pcc.array[ValueClass, N] with N between 1 and 7",
        )
    if ty.length == -2:
        _raise_frontend_error(
            span,
            "pcc.array length must be an integer literal",
            "write a literal length between 1 and 7",
        )
    if ty.length < 1 or ty.length > 7:
        _raise_frontend_error(
            span,
            "pcc.array length must be between 1 and 7",
            "the selected self-backend aggregate ABI supports lengths 1..7",
        )
    if not _is_valueclass_type(ty.elem):
        _raise_frontend_error(
            span,
            "pcc.array element type must be a valueclass",
            "decorate the element class with @pcc.valueclass",
        )
    return ty


def _value_array_type_from_surface(
    ctx: _InferCtx,
    surface: Expr,
) -> Optional[ValueArrayType]:
    if not isinstance(surface, Subscript):
        return None
    if _simple_decorator_name(surface.obj) not in ("pcc.array", "array"):
        return None
    parsed = parse_annotation(surface)
    resolved = ctx.resolve_type_refs(parsed)
    checked = _validate_value_array_type(resolved, surface.span)
    if isinstance(checked, ValueArrayType):
        return checked
    return None


_I64_BUFFER_TYPE_PREFIX = "pcc.i64_buffer["


def _i64_buffer_length_from_type(ty: Type) -> int:
    if not isinstance(ty, BytesType):
        return -1
    name = ty.name
    if not name.startswith(_I64_BUFFER_TYPE_PREFIX) or not name.endswith("]"):
        return -1
    digits = name[len(_I64_BUFFER_TYPE_PREFIX) : -1]
    if not digits:
        return -1
    value = 0
    for ch in digits:
        code = ord(ch) - 48
        if code < 0 or code > 9:
            return -1
        value = value * 10 + code
    if value < 1 or value > 1_048_576:
        return -1
    return value


def _pcc_surface_attr(ctx: _InferCtx, surface: Expr) -> Optional[str]:
    if isinstance(surface, Name):
        ident = _name_ident(surface)
        if ident in ctx.pcc_i64_buffer_aliases:
            return "i64_buffer"
        if ident in ctx.pcc_guarded_i64_dot_aliases:
            return "guarded_i64_dot"
        if ident in ctx.pcc_guarded_loop_counter_aliases:
            return "guarded_loop_counter"
        return None
    if isinstance(surface, Attr) and isinstance(surface.obj, Name):
        root = _name_ident(surface.obj)
        if root in ctx.pcc_module_aliases and surface.name in (
            "i64_buffer",
            "guarded_i64_dot",
            "guarded_loop_counter",
        ):
            return surface.name
    return None


def _pcc_intrinsic_call_kind(ctx: _InferCtx, surface: Expr) -> Optional[str]:
    kind = _pcc_surface_attr(ctx, surface)
    if kind in ("guarded_i64_dot", "guarded_loop_counter"):
        return kind
    return None


def _i64_buffer_type_from_surface(
    ctx: _InferCtx,
    surface: Expr,
) -> Optional[BytesType]:
    if not isinstance(surface, Subscript):
        return None
    if _pcc_surface_attr(ctx, surface.obj) != "i64_buffer":
        return None
    if not isinstance(surface.idx, IntLit):
        _raise_frontend_error(
            surface.idx.span,
            "pcc.i64_buffer length must be an integer literal",
            "write a literal length between 1 and 1048576",
        )
    length = int(surface.idx.value)
    if length < 1 or length > 1_048_576:
        _raise_frontend_error(
            surface.idx.span,
            "pcc.i64_buffer length must be between 1 and 1048576",
            "choose a bounded fixed-length specialization candidate",
        )
    return BytesType(name=_I64_BUFFER_TYPE_PREFIX + str(length) + "]")


def _valueclass_type_refs(
    ty: Type,
    valueclass_names: set[str],
    module_name: str,
) -> set[str]:
    refs: set[str] = set()
    if isinstance(ty, ClassType):
        ty_name = _class_type_name(ty)
        ty_module = _class_type_module(ty)
        same_module = ty_module == "" or ty_module == module_name
        if same_module and ty_name in valueclass_names:
            refs.add(ty_name)
        for _field_name, field_ty in _class_type_fields(ty):
            refs.update(_valueclass_type_refs(field_ty, valueclass_names, module_name))
        return refs
    if isinstance(ty, ListType):
        return _valueclass_type_refs(ty.elem, valueclass_names, module_name)
    if isinstance(ty, ValueArrayType):
        return _valueclass_type_refs(ty.elem, valueclass_names, module_name)
    if isinstance(ty, DictType):
        refs.update(_valueclass_type_refs(ty.key, valueclass_names, module_name))
        refs.update(_valueclass_type_refs(ty.value, valueclass_names, module_name))
        return refs
    if isinstance(ty, TupleType):
        for elem_ty in ty.elems:
            refs.update(_valueclass_type_refs(elem_ty, valueclass_names, module_name))
    return refs


def _validate_valueclass_recursion(ctx: _InferCtx, module: Module) -> None:
    valueclass_defs: dict[str, ClassDef] = {}
    for stmt in module.body:
        if isinstance(stmt, ClassDef) and _class_has_valueclass_decorator(stmt):
            valueclass_defs[stmt.name] = stmt
    if not valueclass_defs:
        return

    module_name = module.name or ""
    valueclass_names = set(valueclass_defs)
    graph: dict[str, set[str]] = {}
    for name, stmt in valueclass_defs.items():
        refs: set[str] = set()
        for _field_name, field_ty in _class_fields_from_def(ctx, stmt):
            refs.update(_valueclass_type_refs(field_ty, valueclass_names, module_name))
        graph[name] = refs

    visiting: set[str] = set()
    visited: set[str] = set()

    for name in valueclass_defs:
        stack = [(name, (), False)]
        while stack:
            cur_name, path, expanded = stack.pop()
            if expanded:
                visiting.discard(cur_name)
                visited.add(cur_name)
                continue
            if cur_name in visiting:
                _raise_frontend_error(
                    valueclass_defs[cur_name].span,
                    "recursive valueclass payload is not supported",
                    "break the cycle with a normal identity class or box the recursive edge explicitly",
                )
            if cur_name in visited:
                continue
            visiting.add(cur_name)
            stack.append((cur_name, path, True))
            refs = tuple(graph.get(cur_name, ()))
            ref_i = len(refs) - 1
            while ref_i >= 0:
                ref = refs[ref_i]
                stack.append((ref, path + (cur_name,), False))
                ref_i -= 1


def _slots_contains_identity_slot(expr: Expr) -> Optional[str]:
    if isinstance(expr, StrLit):
        if expr.value == "__dict__" or expr.value == "__weakref__":
            return expr.value
        return None
    if isinstance(expr, TupleExpr):
        for item in expr.elems:
            slot = _slots_contains_identity_slot(item)
            if slot is not None:
                return slot
    return None


def _validate_valueclass_shape(ctx: _InferCtx, stmt: ClassDef) -> None:
    """Reject source shapes the current boxed V0 valueclass subset cannot honor."""
    for base in stmt.bases:
        if not (isinstance(base, Name) and _name_ident(base) == "object"):
            _raise_frontend_error(
                base.span,
                f"valueclass {stmt.name!r} cannot subclass another class in the current V0 subset",
                "remove the base class or use a normal identity class",
            )

    for body_stmt in stmt.body:
        if isinstance(body_stmt, FuncDef):
            if body_stmt.name == "__del__":
                _raise_frontend_error(
                    body_stmt.span,
                    f"valueclass {stmt.name!r} cannot define __del__",
                    "valueclass instances are identity-free; move finalization to an owning identity object",
                )
            continue

        if isinstance(body_stmt, Assign):
            for target in body_stmt.targets:
                if not isinstance(target, Name):
                    continue
                target_ident = _name_ident(target)
                if target_ident is None:
                    continue
                if target_ident == "__dict__" or target_ident == "__weakref__":
                    _raise_frontend_error(
                        target.span,
                        f"valueclass {stmt.name!r} cannot declare {target_ident}",
                        "valueclass instances do not support instance dictionaries or weakrefs in the current V0 subset",
                    )
                if target_ident == "__slots__":
                    slot = _slots_contains_identity_slot(body_stmt.value)
                    if slot is not None:
                        _raise_frontend_error(
                            body_stmt.span,
                            f"valueclass {stmt.name!r} cannot include {slot} in __slots__",
                            "valueclass instances do not support instance dictionaries or weakrefs in the current V0 subset",
                        )
                    continue
                if _annotation_or_none(body_stmt) is None:
                    _raise_frontend_error(
                        target.span,
                        f"valueclass field {stmt.name}.{target_ident} needs an explicit type annotation",
                        "declare the field as 'name: Type' or make the class a normal identity class",
                    )

    for body_stmt in stmt.body:
        if isinstance(body_stmt, FuncDef):
            if body_stmt.name != "__init__":
                continue
            arg_types: dict[str, Type] = {}
            for arg in body_stmt.args:
                if arg.name == "" or arg.name == "self" or arg.name == "cls":
                    continue
                arg_types[arg.name] = ctx.resolve_annotation(_annotation_or_none(arg))
            for init_stmt in body_stmt.body:
                if isinstance(init_stmt, Assign):
                    explicit_ty: Optional[Type] = None
                    init_annotation = _annotation_or_none(init_stmt)
                    if init_annotation is not None:
                        explicit_ty = ctx.resolve_annotation(init_annotation)
                    for target in init_stmt.targets:
                        if isinstance(target, Attr):
                            target_obj = target.obj
                            if not isinstance(target_obj, Name):
                                continue
                            if _name_ident(target_obj) != "self":
                                continue
                            field_ty = explicit_ty
                            if field_ty is None and isinstance(init_stmt.value, Name):
                                field_ty = arg_types.get(_name_ident(init_stmt.value))
                            if field_ty is None or isinstance(field_ty, DynType):
                                _raise_frontend_error(
                                    target.span,
                                    f"valueclass field {stmt.name}.{target.name} needs a typed initializer",
                                    "annotate the __init__ parameter or the self-field assignment",
                                )


def _class_properties_from_def(
    ctx: _InferCtx, stmt: ClassDef
) -> tuple[tuple[str, Type], ...]:
    """Collect ``@property`` declarations on a class body.

    Returns ``(name, return_ty)`` pairs. ``return_ty`` is taken from
    the getter's declared return annotation; missing annotation falls
    back to ``DynType``. See
    ``docs/investigations/pcc-py-type-infer-property-return-type.md``.
    """
    out: list[tuple[str, Type]] = []
    for body_stmt in stmt.body:
        if not isinstance(body_stmt, FuncDef):
            continue
        decorators = getattr(body_stmt, "decorators", ()) or ()
        if not any(_is_property_decorator(d) for d in decorators):
            continue
        ret_ty = ctx.resolve_annotation(body_stmt.return_ty)
        out.append((body_stmt.name, ret_ty))
    return tuple(out)


def _init_field_rhs_type(ctx: _InferCtx, scope: _Scope, value: Expr) -> Optional[Type]:
    """Conservative RHS typing for ``self.x = ...`` inside ``__init__``.

    This feeds only class field schemas.  Keep the accepted shapes narrow:
    constructor calls, ``copy(arg)``, literals, containers, and comprehension
    sentinels whose result type is already handled by ``_infer_expr``.  Avoid
    arbitrary method calls here; turning those into fixed instance fields can
    change ordinary dynamic-class behavior.
    """
    if isinstance(
        value,
        (
            Name,
            IntLit,
            FloatLit,
            ComplexLit,
            BoolLit,
            NoneLit,
            StrLit,
            BytesLit,
            ListExpr,
            TupleExpr,
            DictExpr,
        ),
    ):
        return ctx.resolve_type_refs(_infer_expr(ctx, scope, value).ty)
    if isinstance(value, Call) and isinstance(value.func, Name):
        callee_name = _name_ident(value.func) or ""
        if (
            callee_name == "copy"
            or callee_name in ctx.class_types
            or callee_name
            in (
                "_list_comp",
                "__listcomp__",
                "_dict_comp",
                "__dictcomp__",
                "_set_comp",
                "__setcomp__",
            )
        ):
            return ctx.resolve_type_refs(_infer_expr(ctx, scope, value).ty)
    return None


def _class_fields_from_def(
    ctx: _InferCtx, stmt: ClassDef
) -> tuple[tuple[str, Type], ...]:
    from pcc.frontends.python.pipeline_exports import instance_field_assignment_statements
    from pcc.frontends.python.pipeline_exports import _class_is_dataclass, export_dataclass_factory_default
    from pcc.frontends.python.pipeline_import_policy import dataclasses_field_binding_names
    from pcc.frontends.python.py_ast import assignment_storage_annotation
    import os
    import sys

    trace_fields = os.environ.get("PCC_DEBUG_FIELD_INFER", "") == "1"

    fields: list[tuple[str, Type]] = []
    nullable_scalar_fields: set[str] = set()
    constructor_none_fields: set[str] = set()
    constructor_value_fields: set[str] = set()
    for body_stmt in stmt.body:
        if trace_fields:
            print("FIELD body", stmt.name, type(body_stmt).__name__, getattr(body_stmt, "name", ""), file=sys.stderr)
        if isinstance(body_stmt, Assign):
            body_annotation = _annotation_or_none(body_stmt)
            if body_annotation is None:
                continue
            field_ty = ctx.resolve_annotation(body_annotation)
            if not _class_has_valueclass_decorator(stmt):
                default = body_stmt.value
                if _class_is_dataclass(stmt) and body_stmt.has_value:
                    default = export_dataclass_factory_default(default, dataclasses_field_binding_names(ctx.module))
                storage_ty = assignment_storage_annotation(
                    field_ty, default if default is not None else body_stmt.value, body_stmt.has_value,
                )
                if storage_ty is not field_ty:
                    for target in body_stmt.targets:
                        if isinstance(target, Name):
                            nullable_scalar_fields.add(target.ident)
                field_ty = storage_ty
            for target in body_stmt.targets:
                if isinstance(target, Name):
                    target_ident = _name_ident(target)
                    if target_ident is not None:
                        _append_field(fields, target_ident, field_ty)
        elif (
            isinstance(body_stmt, FuncDef) and body_stmt.args
            and body_stmt.args[0].name == "self"
        ):
            if trace_fields:
                print("FIELD enter", stmt.name, body_stmt.name, file=sys.stderr)
            arg_types = {
                arg.name: ctx.resolve_annotation(_annotation_or_none(arg))
                for arg in body_stmt.args
                if arg.name not in ("", "self", "cls")
            }
            init_scope = _Scope(parent=ctx.globals)
            for arg_name, arg_ty in arg_types.items():
                init_scope.define(arg_name, arg_ty)
            for init_stmt in instance_field_assignment_statements(body_stmt.body):
                if trace_fields:
                    print("FIELD assignment", body_stmt.name, type(init_stmt).__name__, file=sys.stderr)
                explicit_ty: Optional[Type] = None
                init_annotation = _annotation_or_none(init_stmt)
                if init_annotation is not None:
                    explicit_ty = ctx.resolve_annotation(init_annotation)
                init_targets = (
                    init_stmt.targets if isinstance(init_stmt, Assign)
                    else (init_stmt.target,)
                )
                pending_targets = list(reversed(init_targets))
                while pending_targets:
                    target = pending_targets.pop()
                    if isinstance(target, (TupleExpr, ListExpr)):
                        pending_targets.extend(reversed(target.elems))
                        continue
                    if (
                        not isinstance(target, Attr)
                        or not isinstance(target.obj, Name)
                        or _name_ident(target.obj) != "self"
                    ):
                        continue
                    if trace_fields:
                        print("FIELD target", body_stmt.name, target.name, file=sys.stderr)
                    if body_stmt.name == "__init__":
                        if isinstance(init_stmt.value, NoneLit):
                            constructor_none_fields.add(target.name)
                        else:
                            constructor_value_fields.add(target.name)
                    # Method writes contribute field order, but must not
                    # replace constructor/declaration types with a cleanup
                    # sentinel (e.g. an exhausted list replaced by ()).
                    widen_from_none = False
                    if body_stmt.name != "__init__":
                        field_known = False
                        known_field_ty = None
                        for known_name, _known_ty in fields:
                            if known_name == target.name:
                                field_known = True
                                known_field_ty = _known_ty
                                if trace_fields:
                                    print("FIELD match", known_name, type(_known_ty).__name__, type(known_field_ty).__name__, isinstance(known_field_ty, NoneType), file=sys.stderr)
                                break
                        # `self._x = None` in the constructor does not
                        # describe the field, it only says where it starts.
                        # Skipping later writes left such a field NoneType,
                        # and every use of it then went through the CPython
                        # fallback: `self._loop.create_task(...)` in asyncio
                        # and `self.server` in the gateway's DNS driver each
                        # took their whole enclosing function down to a
                        # no-libpython stub. A write of a real type widens it;
                        # a sentinel write still cannot erase a real
                        # constructor type, because that type is not NoneType.
                        if field_known and not isinstance(
                            known_field_ty, NoneType
                        ):
                            if trace_fields:
                                print("FIELD skip", target.name, file=sys.stderr)
                            continue
                        widen_from_none = field_known
                    field_ty = explicit_ty
                    if field_ty is None and isinstance(init_stmt.value, Name):
                        field_ty = arg_types.get(_name_ident(init_stmt.value))
                    if field_ty is None:
                        field_ty = _init_field_rhs_type(
                            ctx, init_scope, init_stmt.value
                        )
                    if field_ty is None:
                        # An unrecognized RHS (such as an if-expression or
                        # adopted attribute) adds a missing field, but cannot
                        # erase a declaration or earlier constructor type.
                        for known_name, known_ty in fields:
                            if known_name == target.name:
                                field_ty = known_ty
                                break
                        if field_ty is None:
                            field_ty = DynType(name="dyn")
                    if widen_from_none:
                        # A field the constructor only initialises to None and
                        # that some other method writes is, by construction, a
                        # field whose value changes; Dyn is the supertype both
                        # states satisfy and is what routes the attribute
                        # through native dynamic dispatch instead of the None
                        # conversion. The write's own type is not required to
                        # be useful: `self._loop = get_running_loop()` returns
                        # a list-box element that is itself NoneType, and
                        # demanding a better type there left the whole
                        # TaskGroup on the CPython fallback.
                        field_ty = DynType(name="dyn")
                    if trace_fields:
                        print("FIELD append", target.name, type(field_ty).__name__, widen_from_none, file=sys.stderr)
                    if target.name in nullable_scalar_fields:
                        field_ty = TYPE_DYN
                    _append_field(fields, target.name, field_ty)
    # This schema describes every constructor path, not its final source
    # statement. A cleanup/disabled branch can assign None after a native
    # object was installed, and a later assignment can replace an initial
    # None. Both states require object storage and dynamic native dispatch.
    # Keep this join aligned with the closed-world exported field schema.
    if not _class_has_valueclass_decorator(stmt):
        for field_name in constructor_none_fields:
            if field_name in constructor_value_fields:
                _append_field(fields, field_name, TYPE_DYN)
    return tuple(fields)


def _class_type_from_export(
    ctx: _InferCtx,
    module_name: str,
    info: dict,
    module_exports: dict,
    memo: dict[tuple[str, str], ClassType],
) -> ClassType:
    from pcc.frontends.python.pipeline_closed_world import resolve_class_base_export

    class_name = info["class_name"]
    owning_module = info.get("owning_module", module_name)
    if not owning_module:
        owning_module = module_name
    if ctx._record_preload_dependencies:
        if module_name not in ctx._preload_dependency_modules:
            ctx._preload_dependency_modules.append(module_name)
        if owning_module not in ctx._preload_dependency_modules:
            ctx._preload_dependency_modules.append(owning_module)
    owner_exports = module_exports
    if owning_module != module_name:
        maybe_owner_exports = ctx.external_exports.get(owning_module)
        if maybe_owner_exports is not None:
            owner_exports = maybe_owner_exports
    key = (owning_module, class_name)
    cached = memo.get(key)
    if cached is not None:
        return cached

    is_valueclass = bool(info.get("valueclass", False))
    placeholder = _make_class_type(
        class_name,
        owning_module,
        (),
        (),
        valueclass=is_valueclass,
    )
    memo[key] = placeholder

    base_names = _py_ast_static_base_names_for_export(owning_module, class_name)
    if not base_names:
        base_names = tuple(info.get("base_names", ()))
    bases: list[ClassType] = []
    for base_name in base_names:
        resolved_base = resolve_class_base_export(ctx.external_exports, owning_module, base_name)
        if resolved_base is not None:
            base_module, base_info = resolved_base
            bases.append(
                _class_type_from_export(
                    ctx,
                    base_module,
                    base_info,
                    ctx.external_exports[base_module],
                    memo,
                )
            )
        else:
            bases.append(_make_class_type(base_name, owning_module, (), ()))

    static_fields = _py_ast_static_fields_for_export(owning_module, class_name)
    if not static_fields:
        static_fields = _llvm_ir_static_fields_for_export(
            owning_module, class_name
        )
    if static_fields:
        field_type_map = {
            fname: _resolve_export_type_refs(
                ctx,
                owning_module,
                owner_exports,
                memo,
                field_ty,
            )
            for fname, field_ty in static_fields
        }
    else:
        field_type_map = {
            fname: _resolve_export_type_refs(
                ctx,
                owning_module,
                owner_exports,
                memo,
                _annotation_to_type(decode_type(field_ty)),
            )
            for fname, field_ty in info.get("field_types", ())
        }
    field_names = tuple(info.get("field_names", ()))
    if field_names:
        fields = tuple(
            (fname, field_type_map.get(fname, TYPE_DYN)) for fname in field_names
        )
    else:
        fields = tuple(field_type_map.items())
    cls_ty = _make_class_type(
        class_name,
        owning_module,
        fields,
        tuple(bases),
        valueclass=is_valueclass,
    )
    memo[key] = cls_ty
    # Resolving a qualified export does not execute an unqualified import.
    # Explicit from-imports and unique-class preloads bind their own aliases;
    # the recursive resolver must not overwrite a same-named local class.
    ctx.class_types[owning_module + "." + class_name] = cls_ty
    return cls_ty


def _py_ast_ref(name: str) -> ClassType:
    return _make_class_type(name, "pcc.frontends.python.py_ast", (), ())


def _py_ast_tuple_of(ty: Type) -> TupleType:
    return _make_tuple_type("tuple", (ty,))


def _py_ast_static_fields_for_export(
    module_name: str,
    class_name: str,
) -> tuple[tuple[str, Type], ...]:
    if module_name != "pcc.frontends.python.py_ast":
        return ()
    span = _py_ast_ref("SourceSpan")
    ty = _py_ast_ref("Type")
    expr = _py_ast_ref("Expr")
    stmt = _py_ast_ref("Stmt")
    arg = _py_ast_ref("Arg")
    if class_name == "SourceSpan":
        return (
            ("file", TYPE_STR),
            ("line", TYPE_INT),
            ("col", TYPE_INT),
            ("end_line", TYPE_INT),
            ("end_col", TYPE_INT),
        )
    if class_name == "Type":
        return (("name", TYPE_STR),)
    if class_name == "IntType":
        return (("name", TYPE_STR), ("width", TYPE_INT), ("signed", TYPE_BOOL))
    if class_name == "FloatType":
        return (("name", TYPE_STR), ("width", TYPE_INT))
    if class_name in (
        "ComplexType",
        "BoolType",
        "NoneType",
        "StrType",
        "BytesType",
        "ByteArrayType",
        "MemoryViewType",
        "DynType",
    ):
        return (("name", TYPE_STR),)
    if class_name == "ListType":
        return (("name", TYPE_STR), ("elem", ty))
    if class_name == "DictType":
        return (("name", TYPE_STR), ("key", ty), ("value", ty))
    if class_name == "TupleType":
        return (("name", TYPE_STR), ("elems", _py_ast_tuple_of(ty)))
    if class_name == "FuncType":
        return (
            ("name", TYPE_STR),
            ("params", _py_ast_tuple_of(ty)),
            ("ret", ty),
        )
    if class_name == "ClassType":
        return (
            ("name", TYPE_STR),
            ("module", TYPE_STR),
            ("fields", _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, ty)))),
            ("bases", _py_ast_tuple_of(_py_ast_ref("ClassType"))),
            (
                "properties",
                _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, ty))),
            ),
            ("valueclass", TYPE_BOOL),
        )
    if class_name == "ValueClassType":
        return (
            ("name", TYPE_STR),
            ("module", TYPE_STR),
            ("fields", _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, ty)))),
            ("bases", _py_ast_tuple_of(_py_ast_ref("ClassType"))),
            (
                "properties",
                _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, ty))),
            ),
            ("valueclass", TYPE_BOOL),
            ("flattened", TYPE_BOOL),
            ("nullable_fields", TYPE_BOOL),
        )
    if class_name == "Expr":
        return (("span", span), ("ty", ty))
    if class_name == "Stmt":
        return (("span", span),)
    if class_name == "Name":
        return (("span", span), ("ty", ty), ("ident", TYPE_STR))
    if class_name == "IntLit":
        return (("span", span), ("ty", ty), ("value", TYPE_INT))
    if class_name == "FloatLit":
        return (("span", span), ("ty", ty), ("value", TYPE_FLOAT))
    if class_name == "ComplexLit":
        return (
            ("span", span),
            ("ty", ty),
            ("real", TYPE_FLOAT),
            ("imag", TYPE_FLOAT),
        )
    if class_name == "BoolLit":
        return (("span", span), ("ty", ty), ("value", TYPE_BOOL))
    if class_name == "NoneLit":
        return (("span", span), ("ty", ty))
    if class_name == "StrLit":
        return (("span", span), ("ty", ty), ("value", TYPE_STR))
    if class_name == "BytesLit":
        return (("span", span), ("ty", ty), ("value", TYPE_BYTES))
    if class_name == "BinOp" or class_name == "Compare":
        return (
            ("span", span),
            ("ty", ty),
            ("op", TYPE_STR),
            ("lhs", expr),
            ("rhs", expr),
        )
    if class_name == "UnaryOp":
        return (
            ("span", span),
            ("ty", ty),
            ("op", TYPE_STR),
            ("operand", expr),
        )
    if class_name == "BoolExpr":
        return (
            ("span", span),
            ("ty", ty),
            ("op", TYPE_STR),
            ("left", expr),
            ("right", expr),
        )
    if class_name == "Subscript":
        return (("span", span), ("ty", ty), ("obj", expr), ("idx", expr))
    if class_name == "Slice":
        return (
            ("span", span),
            ("ty", ty),
            ("lo", expr),
            ("hi", expr),
            ("step", expr),
        )
    if class_name == "DictExpr":
        return (
            ("span", span),
            ("ty", ty),
            ("pairs", _py_ast_tuple_of(_make_tuple_type("tuple", (expr, expr)))),
        )
    if class_name == "IfExpr":
        return (
            ("span", span),
            ("ty", ty),
            ("cond", expr),
            ("then_e", expr),
            ("else_e", expr),
        )
    if class_name == "Lambda":
        return (
            ("span", span),
            ("ty", ty),
            ("params", _py_ast_tuple_of(arg)),
            ("body", expr),
        )
    if class_name == "Arg":
        return (
            ("name", TYPE_STR),
            ("annotation", ty),
            ("default", expr),
            ("kind", TYPE_STR),
            ("has_default", TYPE_BOOL),
        )
    if class_name == "FuncDef":
        return (
            ("span", span),
            ("name", TYPE_STR),
            ("args", _py_ast_tuple_of(arg)),
            ("return_ty", ty),
            ("body", _py_ast_tuple_of(stmt)),
            ("decorators", _py_ast_tuple_of(expr)),
            ("is_method", TYPE_BOOL),
            ("is_async", TYPE_BOOL),
        )
    if class_name == "ClassDef":
        return (
            ("span", span),
            ("name", TYPE_STR),
            ("bases", _py_ast_tuple_of(expr)),
            ("keywords", _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, expr)))),
            ("body", _py_ast_tuple_of(stmt)),
            ("decorators", _py_ast_tuple_of(expr)),
        )
    if class_name == "Assign":
        return (
            ("span", span),
            ("targets", _py_ast_tuple_of(expr)),
            ("value", expr),
            ("annotation", ty),
            ("has_value", TYPE_BOOL),
        )
    if class_name == "AugAssign":
        return (
            ("span", span),
            ("target", expr),
            ("op", TYPE_STR),
            ("value", expr),
        )
    if class_name == "For":
        return (
            ("span", span),
            ("target", expr),
            ("iter", expr),
            ("body", _py_ast_tuple_of(stmt)),
            ("else_body", _py_ast_tuple_of(stmt)),
            ("is_async", TYPE_BOOL),
        )
    if class_name == "Return":
        return (("span", span), ("value", expr))
    if class_name in ("Pass", "Break", "Continue"):
        return (("span", span),)
    if class_name == "ExprStmt":
        return (("span", span), ("expr", expr))
    if class_name == "If" or class_name == "While":
        return (
            ("span", span),
            ("cond", expr),
            ("body", _py_ast_tuple_of(stmt)),
            ("else_body", _py_ast_tuple_of(stmt)),
        )
    if class_name == "Raise":
        return (("span", span), ("exc", expr), ("cause", expr))
    if class_name == "Try":
        return (
            ("span", span),
            ("body", _py_ast_tuple_of(stmt)),
            ("handlers", _py_ast_tuple_of(_py_ast_ref("ExceptHandler"))),
            ("else_body", _py_ast_tuple_of(stmt)),
            ("finally_body", _py_ast_tuple_of(stmt)),
        )
    if class_name == "ExceptHandler":
        return (
            ("exc_type", expr),
            ("name", TYPE_STR),
            ("body", _py_ast_tuple_of(stmt)),
            ("span", span),
        )
    if class_name == "With":
        return (
            ("span", span),
            ("items", _py_ast_tuple_of(_make_tuple_type("tuple", (expr, expr)))),
            ("body", _py_ast_tuple_of(stmt)),
            ("is_async", TYPE_BOOL),
        )
    if class_name == "Import":
        return (
            ("span", span),
            (
                "names",
                _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, TYPE_STR))),
            ),
        )
    if class_name == "ImportFrom":
        return (
            ("span", span),
            ("module", TYPE_STR),
            (
                "names",
                _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, TYPE_STR))),
            ),
            ("level", TYPE_INT),
        )
    if class_name == "Global" or class_name == "Nonlocal":
        return (("span", span), ("names", _py_ast_tuple_of(TYPE_STR)))
    if class_name == "Delete":
        return (("span", span), ("targets", _py_ast_tuple_of(expr)))
    if class_name == "Call":
        return (
            ("span", span),
            ("ty", ty),
            ("func", expr),
            ("args", _py_ast_tuple_of(expr)),
            ("kwargs", _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, expr)))),
            ("operand_order", _py_ast_tuple_of(_make_tuple_type("tuple", (TYPE_STR, TYPE_INT)))),
            ("is_set_literal", TYPE_BOOL),
        )
    if class_name == "Attr":
        return (("span", span), ("ty", ty), ("obj", expr), ("name", TYPE_STR))
    if class_name == "TupleExpr" or class_name == "ListExpr":
        return (("span", span), ("ty", ty), ("elems", _py_ast_tuple_of(expr)))
    if class_name == "Module":
        return (
            ("name", TYPE_STR),
            ("body", _py_ast_tuple_of(stmt)),
            ("docstring", TYPE_STR),
        )
    return ()


def _py_ast_static_base_names_for_export(
    module_name: str,
    class_name: str,
) -> tuple[str, ...]:
    if module_name != "pcc.frontends.python.py_ast":
        return ()
    if class_name in (
        "IntType",
        "FloatType",
        "ComplexType",
        "BoolType",
        "NoneType",
        "StrType",
        "BytesType",
        "ByteArrayType",
        "MemoryViewType",
        "ListType",
        "DictType",
        "TupleType",
        "FuncType",
        "ClassType",
        "DynType",
    ):
        return ("Type",)
    if class_name == "ValueClassType":
        return ("ClassType",)
    if class_name in (
        "IntLit",
        "FloatLit",
        "ComplexLit",
        "BoolLit",
        "NoneLit",
        "StrLit",
        "BytesLit",
        "Name",
        "BinOp",
        "UnaryOp",
        "Compare",
        "BoolExpr",
        "Call",
        "Attr",
        "Subscript",
        "Slice",
        "ListExpr",
        "DictExpr",
        "TupleExpr",
        "IfExpr",
        "Lambda",
    ):
        return ("Expr",)
    if class_name in (
        "Assign",
        "AugAssign",
        "ExprStmt",
        "If",
        "While",
        "For",
        "Return",
        "Pass",
        "Break",
        "Continue",
        "Raise",
        "Try",
        "With",
        "Import",
        "ImportFrom",
        "Global",
        "Nonlocal",
        "Delete",
        "FuncDef",
        "ClassDef",
    ):
        return ("Stmt",)
    return ()


def _llvm_ir_ref(class_name: str) -> ClassType:
    return _make_class_type(class_name, "pcc.ir.ir", (), ())


def _llvm_ir_static_fields_for_export(
    module_name: str,
    class_name: str,
) -> tuple[tuple[str, Type], ...]:
    if module_name != "pcc.ir.ir":
        return ()

    ty = _llvm_ir_ref("Type")
    value = _llvm_ir_ref("Value")
    module = _llvm_ir_ref("Module")
    function_type = _llvm_ir_ref("FunctionType")
    function = _llvm_ir_ref("Function")
    global_variable = _llvm_ir_ref("GlobalVariable")
    argument = _llvm_ir_ref("Argument")
    block = _llvm_ir_ref("Block")
    instruction = _llvm_ir_ref("InstructionRecord")
    function_attrs = _llvm_ir_ref("FunctionAttributes")

    value_fields = (
        ("type", ty),
        ("_ref", TYPE_STR),
        ("_instr", TYPE_STR),
        ("_flags", _make_list_type(TYPE_STR)),
        ("_is_unsigned", TYPE_BOOL),
        ("_pcc_unsigned_pointee", TYPE_BOOL),
        ("_pcc_unsigned_return", TYPE_BOOL),
    )
    if class_name == "Value":
        return value_fields
    if class_name == "Argument":
        return (
            ("type", ty),
            ("index", TYPE_INT),
            ("_name", TYPE_STR),
            ("_ref", TYPE_STR),
        )
    if class_name == "InstructionRecord":
        return (("text", TYPE_STR), ("opname", TYPE_STR), ("block", block))
    if class_name == "Block":
        return (
            ("parent", function),
            ("function", function),
            ("name", TYPE_STR),
            ("_instrs", _make_list_type(instruction)),
            ("_text_lines", _make_list_type(TYPE_STR)),
            ("_terminated", TYPE_BOOL),
        )
    if class_name == "Function":
        return value_fields + (
            ("module", module),
            ("ftype", function_type),
            ("function_type", function_type),
            ("name", TYPE_STR),
            ("blocks", _make_list_type(block)),
            ("args", _make_tuple_type("tuple_variadic", (argument,))),
            ("_name_counter", TYPE_INT),
            ("_block_counter", TYPE_INT),
            ("_name_registry", _make_dict_type(TYPE_STR, TYPE_INT)),
            ("linkage", TYPE_STR),
            ("attributes", function_attrs),
            ("calling_convention", TYPE_STR),
        )
    if class_name == "GlobalVariable":
        return value_fields + (
            ("value_type", ty),
            ("name", TYPE_STR),
            ("linkage", TYPE_STR),
            ("global_constant", TYPE_BOOL),
            ("initializer", TYPE_DYN),
            ("addrspace", TYPE_INT),
            ("section", TYPE_STR),
            ("align", TYPE_INT),
            ("unnamed_addr", TYPE_STR),
        )
    if class_name == "Module":
        return (
            ("name", TYPE_STR),
            ("triple", TYPE_STR),
            ("data_layout", TYPE_STR),
            ("_functions", _make_list_type(function)),
            ("_globals", _make_list_type(global_variable)),
            ("globals", _make_dict_type(TYPE_STR, TYPE_DYN)),
            ("_named_metadata", _make_dict_type(TYPE_STR, TYPE_DYN)),
            ("context", TYPE_DYN),
            ("_name_counters", _make_dict_type(TYPE_STR, TYPE_INT)),
        )
    if class_name == "IRBuilder":
        return (
            ("_block", block),
            ("_pos", TYPE_INT),
            ("_fn", function),
            ("block", block),
            ("function", function),
        )
    if class_name == "FunctionType":
        return (
            ("return_type", ty),
            ("args", _make_tuple_type("tuple_variadic", (ty,))),
            ("var_arg", TYPE_BOOL),
        )
    if class_name == "PointerType":
        return (("pointee", ty), ("addrspace", TYPE_INT))
    if class_name == "IntType":
        return (("width", TYPE_INT),)
    return ()


def _resolve_export_type_refs(
    ctx: _InferCtx,
    module_name: str,
    module_exports: dict,
    memo: dict[tuple[str, str], ClassType],
    ty: Type,
) -> Type:
    if isinstance(ty, ClassType):
        ty_module = _class_type_module(ty)
        ty_fields = _class_type_fields(ty)
        ty_bases = _class_type_bases(ty)
        if ty_module and not ty_fields and not ty_bases:
            qualified_exports = ctx.external_exports.get(ty_module)
            if qualified_exports is not None:
                ref_info = qualified_exports.get(ty.name)
                if isinstance(ref_info, dict) and ref_info.get("kind") == "class":
                    return _class_type_from_export(
                        ctx, ty_module, ref_info, qualified_exports, memo,
                    )
        if (
            (not ty_module or ty_module == module_name)
            and not ty_fields
            and not ty_bases
        ):
            ref_info = module_exports.get(ty.name)
            if isinstance(ref_info, dict) and ref_info.get("kind") == "class":
                return _class_type_from_export(
                    ctx,
                    module_name,
                    ref_info,
                    module_exports,
                    memo,
                )
        # An imported signature belongs to its defining module. Resolve its
        # lexical class binding before looking at the consumer's type table.
        if ty_module:
            found = ctx.class_types.get(ty_module + "." + ty.name)
            if found is not None and _class_type_module(found) == ty_module:
                return found
            # A provider's unresolved prefix is never a consumer import alias.
            return ty
        return ctx.resolve_type_refs(ty)
    if isinstance(ty, ListType):
        elem = _resolve_export_type_refs(
            ctx, module_name, module_exports, memo, ty.elem
        )
        if elem == ty.elem:
            return ty
        return _make_list_type(elem)
    if isinstance(ty, DictType):
        key = _resolve_export_type_refs(ctx, module_name, module_exports, memo, ty.key)
        value = _resolve_export_type_refs(
            ctx,
            module_name,
            module_exports,
            memo,
            ty.value,
        )
        if key == ty.key and value == ty.value:
            return ty
        return _make_dict_type(key, value)
    if isinstance(ty, TupleType):
        elems = tuple(
            _resolve_export_type_refs(ctx, module_name, module_exports, memo, elem)
            for elem in ty.elems
        )
        if elems == ty.elems:
            return ty
        return _make_tuple_type(ty.name, elems)
    if isinstance(ty, FuncType):
        params = tuple(
            _resolve_export_type_refs(ctx, module_name, module_exports, memo, param)
            for param in ty.params
        )
        ret = _resolve_export_type_refs(ctx, module_name, module_exports, memo, ty.ret)
        if params == ty.params and ret == ty.ret:
            return ty
        return _make_func_type(params, ret)
    return ty


def _bind_external_module_exports(
    ctx: _InferCtx,
    module_name: str,
    local_name: str,
) -> None:
    """Bind qualified class spellings without introducing bare leaf names."""
    module_exports = ctx.external_exports.get(module_name)
    if module_exports is None:
        return
    memo: dict[tuple[str, str], ClassType] = {}
    for export_name, info in module_exports.items():
        if not isinstance(info, dict):
            continue
        kind = info.get("kind")
        if kind == "constant" or kind == "module_global":
            # A str-valued provider global types ``alias.NAME`` as str, so
            # ``os.pathsep.join(...)`` takes the native str method path.
            is_str = info.get("value_kind") == "str"
            if not is_str and kind == "module_global":
                is_str = isinstance(_annotation_to_type(decode_type(info.get("value_ty"))), StrType)
            if is_str:
                ctx.module_attr_types[local_name + "." + export_name] = TYPE_STR
            continue
        if kind != "class":
            continue
        cls_ty = _class_type_from_export(ctx, module_name, info, module_exports, memo)
        # Re-exports can rename a class; the visible key and canonical
        # class_name serve different purposes and must both be preserved.
        ctx.class_types[local_name + "." + export_name] = cls_ty


def _import_names_not_shadowed(ctx: _InferCtx, stmt: ImportFrom) -> tuple:
    """Drop fallback names whose try-body import binds from the closure."""
    alternatives = ctx.import_fallbacks.get(import_statement_key(stmt))
    if not alternatives:
        return stmt.names
    kept = []
    for attr_name, as_name in stmt.names:
        primary = alternatives.get(as_name or attr_name)
        if primary is not None:
            primary_stmt = primary[0]
            primary_module = _resolve_relative_module(
                _import_from_module_or_empty(primary_stmt),
                _import_from_level_or_zero(primary_stmt),
                _ctx_module_name(ctx),
                primary_stmt.span.file,
            )
            if primary[1] in ctx.external_exports.get(primary_module, {}):
                continue
        kept.append((attr_name, as_name))
    return tuple(kept)


def _bind_external_import_exports(
    ctx: _InferCtx,
    scope: _Scope,
    resolved_module: str,
    names: tuple[tuple[str, Optional[str]], ...],
) -> None:
    module_exports = ctx.external_exports.get(resolved_module, {})

    memo: dict[tuple[str, str], ClassType] = {}
    for info in module_exports.values():
        if isinstance(info, dict) and info.get("kind") == "class":
            _class_type_from_export(
                ctx,
                resolved_module,
                info,
                module_exports,
                memo,
            )

    for attr_name, as_name in names:
        local_name = as_name or attr_name
        info = module_exports.get(attr_name)
        if info is None:
            # ``from package import child as alias`` also works when only
            # the child module, rather than its package, is in the closure.
            _bind_external_module_exports(
                ctx, resolved_module + "." + attr_name, local_name,
            )
            continue
        if info["kind"] == "function":
            owning_module = info.get("owning_module", resolved_module) or resolved_module
            owner_exports = ctx.external_exports.get(owning_module, module_exports)
            param_tys = tuple(
                _resolve_export_type_refs(
                    ctx,
                    owning_module,
                    owner_exports,
                    memo,
                    _annotation_to_type(decode_type(t)),
                )
                for t in info["param_types"]
            )
            intrinsic_ret_ty = None
            if resolved_module == "pcc.unsafe":
                intrinsic_ret_ty = _unsafe_intrinsic_return_type(ctx, attr_name)
            if intrinsic_ret_ty is not None:
                # ``pcc.unsafe`` functions are CPython fail-loud stubs whose
                # source annotations intentionally use ``Any``.  The compiler
                # intrinsic table owns their real native projection; a
                # closed-world export must not overwrite it with ``DynType``.
                ret_ty = intrinsic_ret_ty
            else:
                ret_ty = _resolve_export_type_refs(
                    ctx,
                    owning_module,
                    owner_exports,
                    memo,
                    _annotation_to_type(decode_type(info["return_ty"])),
                )
            ft = _make_func_type(param_tys, ret_ty)
            scope.update(local_name, ft)
            ctx.func_types[local_name] = ft
        elif info["kind"] == "class":
            cls_ty = _class_type_from_export(
                ctx,
                resolved_module,
                info,
                module_exports,
                memo,
            )
            scope.update(local_name, cls_ty)
            scope.class_objects.add(local_name)
            ctx.register_class_type(local_name, cls_ty)
        elif info["kind"] == "module_global":
            value_ty = _resolve_export_type_refs(
                ctx,
                resolved_module,
                module_exports,
                memo,
                _annotation_to_type(decode_type(info.get("value_ty"))),
            )
            scope.update(local_name, value_ty)
        elif info["kind"] == "typing_metadata":
            scope.update(local_name, TYPE_DYN)
        elif info["kind"] == "constant":
            value_kind = info.get("value_kind")
            if value_kind == "str":
                scope.update(local_name, TYPE_STR)
            elif value_kind == "int":
                scope.update(
                    local_name,
                    TYPE_I64 if ctx.freestanding else TYPE_INT,
                )
            elif value_kind == "bool":
                scope.update(local_name, TYPE_BOOL)
            elif value_kind == "none":
                scope.update(local_name, TYPE_NONE)


def _bind_ir_compat_module_alias(
    ctx: _InferCtx,
    scope: _Scope,
    resolved_module: str,
    names: tuple[tuple[str, Optional[str]], ...],
) -> None:
    """Bind ``from pcc.ir.compat import ir`` to real IR exports.

    The source spelling is a compile-time compatibility facade, but ON-mode
    closed-world builds link the concrete ``pcc.ir.ir`` provider.
    Type inference must mirror that replacement so annotations such as
    ``ir.IRBuilder`` resolve to the exported class schema instead of a shell
    ``ClassType(module="ir")``.
    """
    if resolved_module != "pcc.ir.compat":
        return
    if not ctx.external_exports:
        return
    module_exports = ctx.external_exports.get("pcc.ir.ir")
    if not module_exports:
        return
    memo: dict[tuple[str, str], ClassType] = {}
    for attr_name, as_name in names:
        # ``ir_c`` / ``ir_py`` / ``ir_passes`` are the per-subsystem spellings
        # (the C codegen imports ``ir_c as ir``); ON mode links the same
        # provider for all of them.
        if attr_name not in ("ir", "ir_c", "ir_py", "ir_passes"):
            continue
        local_name = as_name or attr_name
        for info in module_exports.values():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            cls_ty = _class_type_from_export(
                ctx,
                "pcc.ir.ir",
                info,
                module_exports,
                memo,
            )
            ctx.class_types[f"{local_name}.{cls_ty.name}"] = cls_ty
        scope.update(local_name, DynType(name="module:pcc.ir.ir"))


def _preload_unique_external_classes(ctx: _InferCtx) -> None:
    indexed_preload = ctx.unique_external_class_preload
    if indexed_preload is not None:
        if not isinstance(indexed_preload, dict):
            raise ValueError("invalid indexed external class preload")
        descriptors = indexed_preload.get("types")
        key_rows = indexed_preload.get("keys")
        base_key_rows = indexed_preload.get("base_keys")
        drop_keys = indexed_preload.get("drop_keys", ())
        set_key_rows = indexed_preload.get("set_keys", ())
        if not isinstance(descriptors, tuple):
            raise ValueError("invalid indexed external class preload")
        if key_rows is None:
            key_rows = base_key_rows
        if (
            not isinstance(key_rows, tuple)
            or not isinstance(drop_keys, tuple)
            or not isinstance(set_key_rows, tuple)
        ):
            raise ValueError("invalid indexed external class preload")
        decoded_types = []
        for descriptor in descriptors:
            ty = decode_type(descriptor)
            if not isinstance(ty, ClassType):
                raise ValueError("indexed external class preload is not a class")
            decoded_types.append(ty)
        dropped = list(drop_keys)
        for row in key_rows:
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not isinstance(row[1], int)
                or row[1] < 0
                or row[1] >= len(decoded_types)
            ):
                raise ValueError("invalid indexed external class preload key")
            if row[0] not in dropped:
                ctx.class_types[row[0]] = decoded_types[row[1]]
        for row in set_key_rows:
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not isinstance(row[1], int)
                or row[1] < 0
                or row[1] >= len(decoded_types)
            ):
                raise ValueError("invalid indexed external class preload key")
            ctx.class_types[row[0]] = decoded_types[row[1]]
        return
    by_name: dict[str, list[tuple[str, dict, dict]]] = {}
    for module_name, module_exports in ctx.external_exports.items():
        for info in module_exports.values():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            by_name.setdefault(info["class_name"], []).append(
                (module_name, info, module_exports)
            )
    memo: dict[tuple[str, str], ClassType] = {}
    for class_name, entries in by_name.items():
        if len(entries) != 1:
            continue
        module_name, info, module_exports = entries[0]
        cls_ty = _class_type_from_export(
            ctx,
            module_name,
            info,
            module_exports,
            memo,
        )
        ctx.register_class_type(class_name, cls_ty)


def build_unique_external_class_preload(external_exports):
    """Freeze the repeated unique-class scan into dense type IDs once."""

    ctx = _InferCtx(Module(name="", body=()), external_exports=external_exports)
    ctx._record_preload_dependencies = True
    _preload_unique_external_classes(ctx)
    descriptors = []
    descriptor_ids = {}
    identity_ids = {}
    # Nested class types repeat across these encodings; ctx keeps them alive.
    encode_memo = {}
    key_rows = []
    for key, ty in ctx.class_types.items():
        identity = id(ty)
        cached = identity_ids.get(identity)
        if cached is not None and cached[0] is ty:
            type_id = cached[1]
        else:
            descriptor = encode_type_memo(ty, encode_memo)
            type_id = descriptor_ids.get(descriptor)
            if type_id is None:
                type_id = len(descriptors)
                descriptors.append(descriptor)
                descriptor_ids[descriptor] = type_id
            # Keep the keyed object alive; this memo lasts only for this
            # frozen preload, never across root-dependent reconstruction.
            identity_ids[identity] = (ty, type_id)
        key_rows.append((key, type_id))
    return {
        "types": tuple(descriptors),
        "keys": tuple(key_rows),
        "dependencies": tuple(sorted(ctx._preload_dependency_modules)),
    }


def build_unique_external_class_preload_index(external_exports, root_deltas=None):
    """Build a common preload plus sparse exact per-root deltas.

    ``root_deltas(roots, global_by_key)`` may compute the per-root deltas
    elsewhere (worker processes): it returns ``{root: preload_root_delta(...)}``
    for the given roots.  Type ids are assigned here, in root order, either
    way, so the index is identical to the serial build.
    """

    global_preload, sensitive_roots, global_by_key = _preload_index_plan(
        external_exports
    )
    if root_deltas is None:
        deltas = {}
        for root_module in sensitive_roots:
            deltas[root_module] = preload_root_delta(
                external_exports, root_module, global_by_key
            )
    else:
        deltas = root_deltas(sensitive_roots, global_by_key)
    return assemble_preload_index(
        external_exports, global_preload, sensitive_roots, deltas
    )


def preload_global_by_key(global_preload):
    global_by_key = {}
    for key, type_id in global_preload["keys"]:
        global_by_key[key] = global_preload["types"][type_id]
    return global_by_key


def preload_root_delta(external_exports, root_module, global_by_key):
    """``(drop_keys, ((key, descriptor), ...))`` for one sensitive root."""
    external_for_root = {}
    for module_name, module_exports in external_exports.items():
        if module_name != root_module:
            external_for_root[module_name] = module_exports
    preload = build_unique_external_class_preload(external_for_root)
    root_by_key = {}
    for key, local_type_id in preload["keys"]:
        root_by_key[key] = preload["types"][local_type_id]
    drop_keys = []
    for key in global_by_key:
        if key not in root_by_key:
            drop_keys.append(key)
    set_rows = []
    for key, local_type_id in preload["keys"]:
        descriptor = preload["types"][local_type_id]
        if global_by_key.get(key) == descriptor:
            continue
        set_rows.append((key, descriptor))
    return tuple(drop_keys), tuple(set_rows)


def assemble_preload_index(external_exports, global_preload, sensitive_roots, deltas):
    descriptors = list(global_preload["types"])
    descriptor_ids = {}
    for type_id, descriptor in enumerate(descriptors):
        descriptor_ids[descriptor] = type_id
    sensitive = set(sensitive_roots)
    roots = {}
    for root_module in external_exports:
        if root_module not in sensitive:
            roots[root_module] = ((), ())
            continue
        drop_keys, set_rows = deltas[root_module]
        set_key_rows = []
        for key, descriptor in set_rows:
            type_id = descriptor_ids.get(descriptor)
            if type_id is None:
                type_id = len(descriptors)
                descriptors.append(descriptor)
                descriptor_ids[descriptor] = type_id
            set_key_rows.append((key, type_id))
        roots[root_module] = (tuple(drop_keys), tuple(set_key_rows))
    return {
        "types": tuple(descriptors),
        "base_keys": tuple(global_preload["keys"]),
        "roots": roots,
    }


def _preload_index_plan(external_exports):
    """The global preload, the roots (in export order) whose preload may
    differ from it, and its key -> descriptor map."""
    global_preload = build_unique_external_class_preload(external_exports)
    global_by_key = preload_global_by_key(global_preload)

    class_counts = {}
    module_class_counts = {}
    for module_name, module_exports in external_exports.items():
        local_counts = {}
        for info in module_exports.values():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            class_name = info.get("class_name")
            if not isinstance(class_name, str) or not class_name:
                continue
            class_counts[class_name] = class_counts.get(class_name, 0) + 1
            local_counts[class_name] = local_counts.get(class_name, 0) + 1
        module_class_counts[module_name] = local_counts
    sensitive_modules = list(global_preload["dependencies"])
    for module_name, local_counts in module_class_counts.items():
        for class_name, removed_count in local_counts.items():
            total_count = class_counts.get(class_name, 0)
            if total_count == 1 or total_count - removed_count == 1:
                if module_name not in sensitive_modules:
                    sensitive_modules.append(module_name)
                break

    sensitive = set(sensitive_modules)
    sensitive_roots = []
    for root_module in external_exports:
        if root_module in sensitive:
            sensitive_roots.append(root_module)
    return global_preload, sensitive_roots, global_by_key


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def _infer_assign(ctx: _InferCtx, scope: _Scope, stmt: Assign) -> Assign:
    from pcc.frontends.python.py_ast import assignment_storage_annotation
    if not stmt.has_value:
        # A bare annotation neither reads a name nor overwrites its current
        # binding/type. Attribute/subscript receivers still execute, however.
        targets = []
        declaration_ty = ctx.resolve_annotation(stmt.annotation)
        for target in stmt.targets:
            if isinstance(target, Name):
                current = scope.lookup_local(target.ident)
                if _is_raw_int_type(declaration_ty):
                    # Explicit machine lanes remain type declarations even
                    # before their first value store. Boundness is separate.
                    current = declaration_ty
                    scope.update(target.ident, declaration_ty)
                targets.append(_with_ty(target, current or TYPE_DYN))
            else:
                targets.append(_infer_expr(ctx, scope, target))
        return replace(stmt, targets=tuple(targets))
    value = _infer_expr(ctx, scope, stmt.value)
    ann_ty = ctx.resolve_annotation(stmt.annotation)
    storage_ann = assignment_storage_annotation(ann_ty, stmt.value, stmt.has_value)
    existing_raw_ty: Optional[IntType] = None
    if stmt.annotation is None:
        for target in stmt.targets:
            if not isinstance(target, Name):
                continue
            current_ty = scope.lookup(target.ident)
            if not _is_raw_int_type(current_ty):
                continue
            if existing_raw_ty is None:
                existing_raw_ty = current_ty
            elif existing_raw_ty.name != current_ty.name:
                _raise_frontend_error(
                    stmt.span,
                    "one assignment cannot rebind mixed raw integer lanes",
                    "split the assignment or use one explicit raw type",
                )
        if existing_raw_ty is not None:
            value = _contextualize_raw_int_constant(value, existing_raw_ty)
            _check_assign_compatible(existing_raw_ty, value.ty, stmt.span)
    if _is_raw_int_type(ann_ty):
        value = _contextualize_raw_int_constant(value, ann_ty)
    extern_marker_annotation = (
        isinstance(value.ty, FuncType)
        and isinstance(ann_ty, ClassType)
        and (
            ann_ty.name == "extern"
            or ann_ty.name in ctx.extern_fn_type_aliases
        )
        and isinstance(stmt.value, Call)
        and isinstance(stmt.value.func, Name)
        and _name_ident(stmt.value.func) in ctx.extern_factory_aliases
    )
    preserve_value_storage = False
    if extern_marker_annotation:
        # ``x: extern = extern(...)`` and ``x: ExternFn = extern(...)`` both
        # use the annotation as a declaration marker, not as the runtime
        # result type of calling ``x``.  ``pcc/ir/__init__.py`` spells
        # every one of its ~200 LLVM-C declarations the second way, which is
        # the accurate annotation -- ``extern()`` does return an ``ExternFn``.
        bind_ty = value.ty
    elif existing_raw_ty is not None:
        bind_ty = existing_raw_ty
    elif storage_ann is not ann_ty:
        bind_ty = storage_ann
    elif stmt.annotation is None or isinstance(ann_ty, DynType):
        # An absent annotation is a syntax-level fact and must not depend on
        # runtime class identity.  In a self-hosted fixed-layout compiler the
        # imported ``TYPE_DYN`` singleton can cross a compiled-module class
        # boundary where ``isinstance(ann_ty, DynType)`` is false; the explicit
        # ``stmt.annotation is None`` keeps an unannotated binding on the
        # ``bind_ty = value.ty`` path instead of falling through to the
        # annotation-compat branch and erasing the RHS type.
        bind_ty = value.ty
    else:
        _check_assign_compatible(ann_ty, value.ty, stmt.span)
        bind_ty = ann_ty
        if (
            scope is not ctx.globals
            and all(isinstance(target, Name) for target in stmt.targets)
            and isinstance(ann_ty, ClassType)
            and _class_type_is_unresolved_shell(ann_ty)
            and isinstance(value.ty, ClassType)
            and not _class_type_is_unresolved_shell(value.ty)
        ):
            # An opaque local annotation is not a class definition or an
            # object projection. After the ordinary compatibility check,
            # retain the RHS's known identity/layout rather than replacing
            # an imported value payload with an empty annotation shell.
            # Qualified/resolved annotations never take this path.
            bind_ty = value.ty
            preserve_value_storage = True

    if (
        isinstance(ann_ty, DynType)
        and isinstance(bind_ty, DictType)
        and _typeconf_storage_class(bind_ty.value) != "object"
        and any(
            isinstance(tgt, Name)
            and _name_ident(tgt) in ctx.setdefault_none_widen_names
            for tgt in stmt.targets
        )
    ):
        # This local later receives a 1-arg ``setdefault`` (inserts None);
        # an inferred native-scalar value type cannot represent that —
        # ``is None`` on the stored slot would constant-fold to False.
        bind_ty = replace(bind_ty, value=TYPE_DYN)
        value = replace(value, ty=bind_ty)

    new_targets = []
    for tgt in stmt.targets:
        if isinstance(tgt, Name):
            tgt_ident = _name_ident(tgt)
            if tgt_ident is not None:
                scope.update(tgt_ident, bind_ty)
            new_targets.append(_with_ty(tgt, bind_ty))
        elif (
            isinstance(tgt, TupleExpr)
            and isinstance(bind_ty, TupleType)
            and len(bind_ty.elems) == len(tgt.elems)
            and all(isinstance(e, Name) for e in tgt.elems)
        ):
            # Tuple unpack ``a, b = <tuple>``: propagate each tuple element
            # type to its sub-target Name so the unpacked variable is typed
            # like a direct assignment. Without this, a float element bound to
            # an untyped sub-target was stored/read with a mismatched type
            # (only ints/strs happened to work by default; floats gave <null>).
            # Restricted to flat all-Name targets; nested/starred/subscript/
            # attr targets and arity mismatches fall back to plain inference.
            sub_targets = []
            for sub, elem_ty in zip(tgt.elems, bind_ty.elems):
                sub_ident = _name_ident(sub)
                if sub_ident is not None:
                    scope.update(sub_ident, elem_ty)
                sub_targets.append(_with_ty(sub, elem_ty))
            new_targets.append(replace(tgt, elems=tuple(sub_targets), ty=bind_ty))
        elif isinstance(tgt, TupleExpr) and (
            isinstance(bind_ty, (DynType, ListType))
            or (isinstance(bind_ty, TupleType) and not bind_ty.elems)
        ):
            # A list or unknown iterable still assigns every target. Reading
            # targets as expressions here retained stale None/callable types
            # from earlier bindings instead of recording the new values.
            # Reuse the conservative nested-target binder; precise known
            # tuple element types keep the existing path above.
            new_targets.append(_bind_for_tuple_target(ctx, scope, tgt, bind_ty))
        else:
            new_targets.append(_infer_expr(ctx, scope, tgt))
    # Preserve the resolved annotation as a ``Type`` in the node so the
    # codegen layer doesn't have to re-parse it.
    new_annotation = (
        bind_ty
        if preserve_value_storage
        else value.ty
        if extern_marker_annotation
        else ann_ty if not isinstance(ann_ty, DynType) else stmt.annotation
    )
    return replace(
        stmt,
        targets=tuple(new_targets),
        value=value,
        annotation=new_annotation,
    )


def _check_assign_compatible(ann: Type, rhs: Type, span: SourceSpan) -> None:
    """Raise ``PyFrontendError`` if ``rhs`` is incompatible with ``ann``."""
    if isinstance(ann, DynType) or isinstance(rhs, DynType):
        return
    if type_eq(ann, rhs):
        return
    # Allow implicit int→float promotion at assignment.
    if isinstance(ann, FloatType) and isinstance(rhs, (IntType, BoolType)):
        return
    # Allow bool→int.
    if isinstance(ann, IntType) and isinstance(rhs, BoolType):
        return
    # Allow None on any non-numeric annotation. ``Optional[T]``
    # unwraps to ``T`` at parse time only for non-primitive ``T``
    # (see ``pcc/frontends/python/py_lift.py``). Numeric ``Optional[int]`` /
    # ``Optional[float]`` / ``Optional[bool]`` stay as DynType because
    # pcc has no nullable representation for unboxed numerics, so the
    # exclusion below preserves a real correctness check rather than
    # a documentation gap.
    if _is_none_type(rhs) and not isinstance(ann, (IntType, FloatType, BoolType)):
        return
    # Container element subsumption with DynType.
    if _is_assignable(ann, rhs):
        return
    _raise_frontend_error(
        span,
        f"cannot assign value of type {rhs.name!r} to variable annotated as {ann.name!r}",
        "add an explicit cast or relax the annotation",
    )


# ---------------------------------------------------------------------------
# FuncDef
# ---------------------------------------------------------------------------


def _collect_setdefault_none_expr(expr, found) -> None:
    """Add receiver names of 1-arg ``<name>.setdefault(key)`` calls in
    ``expr`` to ``found``. Traversal mirrors
    ``_expr_contains_yield_sentinel`` (explicit node dispatch — the
    self-hosted compiler has no dataclass field reflection)."""
    if isinstance(expr, Call):
        if (
            isinstance(expr.func, Attr)
            and expr.func.name == "setdefault"
            and isinstance(expr.func.obj, Name)
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            ident = _name_ident(expr.func.obj)
            if ident is not None:
                found.add(ident)
        _collect_setdefault_none_expr(expr.func, found)
        for arg in expr.args:
            _collect_setdefault_none_expr(arg, found)
        for _name, value in expr.kwargs:
            _collect_setdefault_none_expr(value, found)
        return
    if isinstance(expr, Attr):
        _collect_setdefault_none_expr(expr.obj, found)
        return
    if isinstance(expr, BinOp):
        _collect_setdefault_none_expr(expr.lhs, found)
        _collect_setdefault_none_expr(expr.rhs, found)
        return
    if isinstance(expr, UnaryOp):
        _collect_setdefault_none_expr(expr.operand, found)
        return
    if isinstance(expr, Compare):
        _collect_setdefault_none_expr(expr.lhs, found)
        _collect_setdefault_none_expr(expr.rhs, found)
        return
    if isinstance(expr, BoolExpr):
        _collect_setdefault_none_expr(expr.left, found)
        _collect_setdefault_none_expr(expr.right, found)
        return
    if isinstance(expr, Subscript):
        _collect_setdefault_none_expr(expr.obj, found)
        _collect_setdefault_none_expr(expr.idx, found)
        return
    if isinstance(expr, Slice):
        for part in (expr.lo, expr.hi, expr.step):
            if part is not None:
                _collect_setdefault_none_expr(part, found)
        return
    if isinstance(expr, ListExpr):
        for item in expr.elems:
            _collect_setdefault_none_expr(item, found)
        return
    if isinstance(expr, TupleExpr):
        for item in expr.elems:
            _collect_setdefault_none_expr(item, found)
        return
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            _collect_setdefault_none_expr(key, found)
            _collect_setdefault_none_expr(value, found)
        return
    if isinstance(expr, IfExpr):
        _collect_setdefault_none_expr(expr.cond, found)
        _collect_setdefault_none_expr(expr.then_e, found)
        _collect_setdefault_none_expr(expr.else_e, found)
        return
    if isinstance(expr, Lambda):
        _collect_setdefault_none_expr(expr.body, found)
        return


def _collect_setdefault_none_stmt(stmt, found) -> None:
    if isinstance(stmt, Assign):
        _collect_setdefault_none_expr(stmt.value, found)
        for target in stmt.targets:
            _collect_setdefault_none_expr(target, found)
        return
    if isinstance(stmt, AugAssign):
        _collect_setdefault_none_expr(stmt.target, found)
        _collect_setdefault_none_expr(stmt.value, found)
        return
    if isinstance(stmt, ExprStmt):
        _collect_setdefault_none_expr(stmt.expr, found)
        return
    if isinstance(stmt, Return):
        if stmt.value is not None:
            _collect_setdefault_none_expr(stmt.value, found)
        return
    if isinstance(stmt, Raise):
        if stmt.exc is not None:
            _collect_setdefault_none_expr(stmt.exc, found)
        if stmt.cause is not None:
            _collect_setdefault_none_expr(stmt.cause, found)
        return
    if isinstance(stmt, Delete):
        for target in stmt.targets:
            _collect_setdefault_none_expr(target, found)
        return
    if isinstance(stmt, If):
        _collect_setdefault_none_expr(stmt.cond, found)
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        for item in stmt.else_body:
            _collect_setdefault_none_stmt(item, found)
        return
    if isinstance(stmt, While):
        _collect_setdefault_none_expr(stmt.cond, found)
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        for item in stmt.else_body:
            _collect_setdefault_none_stmt(item, found)
        return
    if isinstance(stmt, For):
        _collect_setdefault_none_expr(stmt.target, found)
        _collect_setdefault_none_expr(stmt.iter, found)
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        for item in stmt.else_body:
            _collect_setdefault_none_stmt(item, found)
        return
    if isinstance(stmt, With):
        for ctx_expr, as_var in stmt.items:
            _collect_setdefault_none_expr(ctx_expr, found)
            if as_var is not None:
                _collect_setdefault_none_expr(as_var, found)
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        return
    if isinstance(stmt, Try):
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        for item in stmt.else_body:
            _collect_setdefault_none_stmt(item, found)
        for item in stmt.finally_body:
            _collect_setdefault_none_stmt(item, found)
        for handler in stmt.handlers:
            if handler.exc_type is not None:
                _collect_setdefault_none_expr(handler.exc_type, found)
            for item in handler.body:
                _collect_setdefault_none_stmt(item, found)
        return
    if isinstance(stmt, FuncDef):
        # A nested def may mutate an enclosing function's dict local
        # through a closure; include its body so the outer binding widens.
        for item in stmt.body:
            _collect_setdefault_none_stmt(item, found)
        return


def _collect_setdefault_none_receivers(body) -> set:
    """Names that receive a 1-arg ``<name>.setdefault(key)`` call in ``body``.

    ``dict.setdefault`` with no default inserts ``None`` for a missing key,
    so an inferred scalar-valued dict binding for these receivers must widen
    its value type to ``dyn`` (see ``_infer_assign``).
    """
    found: set = set()
    for stmt in body:
        _collect_setdefault_none_stmt(stmt, found)
    return found


def _is_starred_arg_expr(expr: Expr) -> bool:
    return (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and expr.func.ident in ("*", "__starred__")
    )


_BINDING_EXPR_SENTINELS = (
    "__listcomp__",
    "__setcomp__",
    "__genexpr__",
    "__dictcomp__",
)


def _bound_target_names(target: Expr, bound: set) -> bool:
    """Add the names an assignment target binds; False for an unmodelled shape."""
    if isinstance(target, Name):
        ident = _name_ident(target)
        if ident is not None:
            bound.add(ident)
        return True
    if isinstance(target, (TupleExpr, ListExpr)):
        for item in target.elems:
            if not _bound_target_names(item, bound):
                return False
        return True
    if _is_starred_arg_expr(target):
        for item in target.args:
            if not _bound_target_names(item, bound):
                return False
        return True
    return isinstance(target, (Attr, Subscript))


def _expr_bound_names(expr, bound: set) -> bool:
    """Names ``expr`` binds: lambda parameters, comprehension and walrus targets."""
    if expr is None:
        return True
    if isinstance(
        expr,
        (Name, IntLit, FloatLit, ComplexLit, BoolLit, NoneLit, StrLit, BytesLit),
    ):
        return True
    if isinstance(expr, Lambda):
        for param in expr.params:
            bound.add(param.name)
        return _expr_bound_names(expr.body, bound)
    if isinstance(expr, Call):
        sentinel = ""
        if isinstance(expr.func, Name):
            sentinel = _name_ident(expr.func) or ""
        if sentinel == "__walrus__" and len(expr.args) > 0:
            if not _bound_target_names(expr.args[0], bound):
                return False
        if sentinel in _BINDING_EXPR_SENTINELS and len(expr.args) > 0:
            clauses = expr.args[len(expr.args) - 1]
            if not isinstance(clauses, TupleExpr):
                return False
            for clause in clauses.elems:
                if not isinstance(clause, TupleExpr) or len(clause.elems) == 0:
                    return False
                if not _bound_target_names(clause.elems[0], bound):
                    return False
        if not _expr_bound_names(expr.func, bound):
            return False
        for arg in expr.args:
            if not _expr_bound_names(arg, bound):
                return False
        for _kw, value in expr.kwargs:
            if not _expr_bound_names(value, bound):
                return False
        return True
    if isinstance(expr, Attr):
        return _expr_bound_names(expr.obj, bound)
    if isinstance(expr, Subscript):
        return _expr_bound_names(expr.obj, bound) and _expr_bound_names(
            expr.idx, bound
        )
    if isinstance(expr, Slice):
        return (
            _expr_bound_names(expr.lo, bound)
            and _expr_bound_names(expr.hi, bound)
            and _expr_bound_names(expr.step, bound)
        )
    if isinstance(expr, BinOp):
        return _expr_bound_names(expr.lhs, bound) and _expr_bound_names(
            expr.rhs, bound
        )
    if isinstance(expr, UnaryOp):
        return _expr_bound_names(expr.operand, bound)
    if isinstance(expr, Compare):
        return _expr_bound_names(expr.lhs, bound) and _expr_bound_names(
            expr.rhs, bound
        )
    if isinstance(expr, BoolExpr):
        return _expr_bound_names(expr.left, bound) and _expr_bound_names(
            expr.right, bound
        )
    if isinstance(expr, (ListExpr, TupleExpr)):
        for item in expr.elems:
            if not _expr_bound_names(item, bound):
                return False
        return True
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            if not _expr_bound_names(key, bound):
                return False
            if not _expr_bound_names(value, bound):
                return False
        return True
    if isinstance(expr, IfExpr):
        return (
            _expr_bound_names(expr.cond, bound)
            and _expr_bound_names(expr.then_e, bound)
            and _expr_bound_names(expr.else_e, bound)
        )
    return False


def _stmts_bound_names(stmts, bound: set) -> bool:
    for stmt in stmts:
        if not _stmt_bound_names(stmt, bound):
            return False
    return True


def _stmt_bound_names(stmt: Stmt, bound: set) -> bool:
    """Names ``stmt`` can bind; False for a statement the scan does not model."""
    if isinstance(stmt, Assign):
        for target in stmt.targets:
            if not _bound_target_names(target, bound):
                return False
            if not _expr_bound_names(target, bound):
                return False
        return _expr_bound_names(stmt.value, bound)
    if isinstance(stmt, AugAssign):
        return (
            _bound_target_names(stmt.target, bound)
            and _expr_bound_names(stmt.target, bound)
            and _expr_bound_names(stmt.value, bound)
        )
    if isinstance(stmt, ExprStmt):
        return _expr_bound_names(stmt.expr, bound)
    if isinstance(stmt, Return):
        return _expr_bound_names(stmt.value, bound)
    if isinstance(stmt, Raise):
        return _expr_bound_names(stmt.exc, bound) and _expr_bound_names(
            stmt.cause, bound
        )
    if isinstance(stmt, (Pass, Break, Continue)):
        return True
    if isinstance(stmt, (If, While)):
        return (
            _expr_bound_names(stmt.cond, bound)
            and _stmts_bound_names(stmt.body, bound)
            and _stmts_bound_names(stmt.else_body, bound)
        )
    if isinstance(stmt, For):
        return (
            _bound_target_names(stmt.target, bound)
            and _expr_bound_names(stmt.target, bound)
            and _expr_bound_names(stmt.iter, bound)
            and _stmts_bound_names(stmt.body, bound)
            and _stmts_bound_names(stmt.else_body, bound)
        )
    if isinstance(stmt, With):
        for ctx_expr, as_var in stmt.items:
            if not _expr_bound_names(ctx_expr, bound):
                return False
            if as_var is not None:
                if not _bound_target_names(as_var, bound):
                    return False
                if not _expr_bound_names(as_var, bound):
                    return False
        return _stmts_bound_names(stmt.body, bound)
    if isinstance(stmt, Try):
        if not _stmts_bound_names(stmt.body, bound):
            return False
        for handler in stmt.handlers:
            if handler.name is not None:
                bound.add(handler.name)
            if not _expr_bound_names(handler.exc_type, bound):
                return False
            if not _stmts_bound_names(handler.body, bound):
                return False
        return _stmts_bound_names(stmt.else_body, bound) and _stmts_bound_names(
            stmt.finally_body, bound
        )
    if isinstance(stmt, Delete):
        for target in stmt.targets:
            if not _bound_target_names(target, bound):
                return False
            if not _expr_bound_names(target, bound):
                return False
        return True
    if isinstance(stmt, (Global, Nonlocal)):
        for name in stmt.names:
            bound.add(name)
        return True
    if isinstance(stmt, Import):
        for module_name, as_name in stmt.names:
            if as_name is not None:
                bound.add(as_name)
            else:
                bound.add(module_name.split(".", 1)[0])
        return True
    if isinstance(stmt, ImportFrom):
        for name, as_name in stmt.names:
            bound.add(as_name if as_name is not None else name)
        return True
    return False


def _forwardable_method_params(fn: FuncDef) -> dict:
    """Unannotated parameters ``fn`` never rebinds: name -> (method, slot).

    Every bare load of such a name in the body is the parameter's own value,
    so a self-call passing it has exactly the parameter's type.  A name bound
    anywhere in the body (including a lambda parameter or comprehension
    target that shadows it) is excluded; decorated methods, whose first
    parameter need not be ``self``, and bodies with a statement the scan
    does not model forward nothing.
    """
    if len(fn.decorators) > 0:
        return {}
    bound: set = set()
    if not _stmts_bound_names(fn.body, bound):
        return {}
    params: dict = {}
    index = 1
    while index < len(fn.args):
        arg = fn.args[index]
        if (
            arg.annotation is None
            and (arg.kind == "pos" or arg.kind == "pos_only")
            and arg.name not in bound
        ):
            params[arg.name] = (fn.name, index)
        index += 1
    return params


def _class_forwardable_params(class_body) -> dict:
    """``_forwardable_method_params`` for each uniquely named method."""
    counts: dict = {}
    for stmt in class_body:
        if isinstance(stmt, FuncDef):
            counts[stmt.name] = counts.get(stmt.name, 0) + 1
    result: dict = {}
    for stmt in class_body:
        if isinstance(stmt, FuncDef) and counts.get(stmt.name, 0) == 1:
            params = _forwardable_method_params(stmt)
            if len(params) > 0:
                result[stmt.name] = params
    return result


def _record_self_method_call_arg_types(
    ctx: _InferCtx,
    out: dict[str, dict[int, Type]],
    method_name: str,
    args: tuple[Expr, ...],
    kwargs: tuple = (),
    param_names: tuple[str, ...] = (),
    forward=None,
) -> None:
    """Join one ``self.method(...)`` call's argument types into ``out``.

    Every observation joins: a ``DynType`` argument (the value may be
    anything) widens the parameter to dyn rather than being ignored, and a
    starred argument or a keyword that names no known parameter poisons the
    whole method (slot ``-1``).  Only when every call agrees on one static
    type may the parameter be typed; skipping unknowns let one typed caller
    type a parameter that a recursive call feeds a tuple, and
    ``isinstance(node, tuple)`` then folded to False.

    An argument that is a parameter the calling method forwards unchanged
    (``forward``, see ``_forwardable_method_params``) is not joined with its
    current type, which before that parameter is typed is only this round's
    dyn default; it is recorded as a dependency on the parameter's own slot
    and solved by ``_resolve_forwarded_self_method_args``.
    """
    method_slots = out.setdefault(method_name, {})
    for arg_index, arg in enumerate(args):
        if _is_starred_arg_expr(arg):
            method_slots[-1] = TYPE_DYN
            return
        if _record_forwarded_self_method_arg(
            forward, method_name, arg_index + 1, arg
        ):
            continue
        _join_self_method_arg_slot(
            method_slots, arg_index + 1, ctx.resolve_type_refs(arg.ty)
        )
    for kw_name, value in kwargs:
        param_index = -1
        position = 0
        while position < len(param_names):
            if param_names[position] == kw_name:
                param_index = position
                break
            position += 1
        if kw_name is None or param_index < 0:
            method_slots[-1] = TYPE_DYN
            return
        if _record_forwarded_self_method_arg(
            forward, method_name, param_index, value
        ):
            continue
        _join_self_method_arg_slot(
            method_slots, param_index, ctx.resolve_type_refs(value.ty)
        )


def _record_forwarded_self_method_arg(
    forward, method_name: str, param_index: int, arg: Expr
) -> bool:
    """Record ``arg`` as forwarding a caller parameter; False if it is not one."""
    if forward is None or not isinstance(arg, Name):
        return False
    params = forward[0]
    deps = forward[1]
    ident = _name_ident(arg)
    if ident is None or ident not in params:
        return False
    target = (method_name, param_index)
    sources = deps.get(target)
    if sources is None:
        sources = []
        deps[target] = sources
    sources.append(params[ident])
    return True


def _join_self_method_arg_slot(
    method_slots: dict[int, Type], param_index: int, arg_ty: Type
) -> None:
    existing = method_slots.get(param_index)
    if existing is None:
        method_slots[param_index] = arg_ty
    elif isinstance(existing, DynType):
        return
    elif isinstance(arg_ty, DynType) or not type_eq(existing, arg_ty):
        method_slots[param_index] = TYPE_DYN


def _literal_self_method_dict(expr: Expr) -> tuple[str, ...]:
    if not isinstance(expr, DictExpr):
        return ()
    methods: list[str] = []
    for _key, value in expr.pairs:
        if (
            isinstance(value, Attr)
            and isinstance(value.obj, Name)
            and _name_ident(value.obj) == "self"
        ):
            methods.append(value.name)
        else:
            return ()
    return tuple(methods)


def _collect_self_method_arg_types_expr(
    ctx: _InferCtx,
    expr: Expr,
    method_dicts: dict[str, tuple[str, ...]],
    out: dict[str, dict[int, Type]],
    param_names: dict[str, tuple[str, ...]],
    forward=None,
) -> None:
    if isinstance(expr, Call):
        if (
            isinstance(expr.func, Attr)
            and isinstance(expr.func.obj, Name)
            and _name_ident(expr.func.obj) == "self"
        ):
            _record_self_method_call_arg_types(
                ctx, out, expr.func.name, expr.args, expr.kwargs,
                param_names.get(expr.func.name, ()),
                forward,
            )
        elif isinstance(expr.func, Subscript) and isinstance(expr.func.obj, Name):
            dispatch_name = _name_ident(expr.func.obj)
            for method_name in method_dicts.get(dispatch_name or "", ()):
                _record_self_method_call_arg_types(
                    ctx, out, method_name, expr.args, expr.kwargs,
                    param_names.get(method_name, ()),
                    forward,
                )
        _collect_self_method_arg_types_expr(ctx, expr.func, method_dicts, out, param_names, forward)
        for arg in expr.args:
            _collect_self_method_arg_types_expr(ctx, arg, method_dicts, out, param_names, forward)
        for _name, value in expr.kwargs:
            _collect_self_method_arg_types_expr(ctx, value, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, Attr):
        _collect_self_method_arg_types_expr(ctx, expr.obj, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, BinOp):
        _collect_self_method_arg_types_expr(ctx, expr.lhs, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.rhs, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, UnaryOp):
        _collect_self_method_arg_types_expr(ctx, expr.operand, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, Compare):
        _collect_self_method_arg_types_expr(ctx, expr.lhs, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.rhs, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, BoolExpr):
        _collect_self_method_arg_types_expr(ctx, expr.left, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.right, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, Subscript):
        _collect_self_method_arg_types_expr(ctx, expr.obj, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.idx, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, Slice):
        for part in (expr.lo, expr.hi, expr.step):
            if part is not None:
                _collect_self_method_arg_types_expr(ctx, part, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, ListExpr):
        for item in expr.elems:
            _collect_self_method_arg_types_expr(ctx, item, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, TupleExpr):
        for item in expr.elems:
            _collect_self_method_arg_types_expr(ctx, item, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            _collect_self_method_arg_types_expr(ctx, key, method_dicts, out, param_names, forward)
            _collect_self_method_arg_types_expr(ctx, value, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, IfExpr):
        _collect_self_method_arg_types_expr(ctx, expr.cond, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.then_e, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, expr.else_e, method_dicts, out, param_names, forward)
        return
    if isinstance(expr, Lambda):
        _collect_self_method_arg_types_expr(ctx, expr.body, method_dicts, out, param_names, forward)
        return


def _collect_self_method_arg_types_stmt(
    ctx: _InferCtx,
    stmt: Stmt,
    method_dicts: dict[str, tuple[str, ...]],
    out: dict[str, dict[int, Type]],
    param_names: dict[str, tuple[str, ...]],
    forward=None,
) -> None:
    if isinstance(stmt, Assign):
        for target in stmt.targets:
            if isinstance(target, Name):
                methods = _literal_self_method_dict(stmt.value)
                target_name = _name_ident(target)
                if target_name is not None and methods:
                    method_dicts[target_name] = methods
        _collect_self_method_arg_types_expr(ctx, stmt.value, method_dicts, out, param_names, forward)
        for target in stmt.targets:
            _collect_self_method_arg_types_expr(ctx, target, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, AugAssign):
        _collect_self_method_arg_types_expr(ctx, stmt.target, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, stmt.value, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, ExprStmt):
        _collect_self_method_arg_types_expr(ctx, stmt.expr, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, Return):
        if stmt.value is not None:
            _collect_self_method_arg_types_expr(ctx, stmt.value, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, Raise):
        if stmt.exc is not None:
            _collect_self_method_arg_types_expr(ctx, stmt.exc, method_dicts, out, param_names, forward)
        if stmt.cause is not None:
            _collect_self_method_arg_types_expr(ctx, stmt.cause, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, Delete):
        for target in stmt.targets:
            _collect_self_method_arg_types_expr(ctx, target, method_dicts, out, param_names, forward)
        return
    if isinstance(stmt, If):
        _collect_self_method_arg_types_expr(ctx, stmt.cond, method_dicts, out, param_names, forward)
        for item in stmt.body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for item in stmt.else_body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        return
    if isinstance(stmt, While):
        _collect_self_method_arg_types_expr(ctx, stmt.cond, method_dicts, out, param_names, forward)
        for item in stmt.body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for item in stmt.else_body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        return
    if isinstance(stmt, For):
        _collect_self_method_arg_types_expr(ctx, stmt.target, method_dicts, out, param_names, forward)
        _collect_self_method_arg_types_expr(ctx, stmt.iter, method_dicts, out, param_names, forward)
        for item in stmt.body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for item in stmt.else_body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        return
    if isinstance(stmt, With):
        for ctx_expr, as_var in stmt.items:
            _collect_self_method_arg_types_expr(ctx, ctx_expr, method_dicts, out, param_names, forward)
            if as_var is not None:
                _collect_self_method_arg_types_expr(ctx, as_var, method_dicts, out, param_names, forward)
        for item in stmt.body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        return
    if isinstance(stmt, Try):
        for item in stmt.body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for item in stmt.else_body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for item in stmt.finally_body:
            _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        for handler in stmt.handlers:
            if handler.exc_type is not None:
                _collect_self_method_arg_types_expr(
                    ctx, handler.exc_type, method_dicts, out, param_names, forward
                )
            for item in handler.body:
                _collect_self_method_arg_types_stmt(ctx, item, dict(method_dicts), out, param_names, forward)
        return


def _collect_self_method_arg_overrides(
    ctx: _InferCtx,
    class_body: tuple[Stmt, ...],
    forwardable=None,
    previous=None,
) -> dict[str, dict[int, Type]]:
    """Joined argument types of every ``self.method(...)`` call in the class.

    Dyn slots (and the ``-1`` poison slot) are kept: the merge needs them to
    widen an override, and a later round must not re-narrow it.
    ``forwardable`` maps each method to the parameters it forwards unchanged
    (``_class_forwardable_params``); ``previous`` holds the overrides the
    bodies were typed with this round.
    """
    param_names: dict[str, tuple[str, ...]] = {}
    for stmt in class_body:
        if isinstance(stmt, FuncDef):
            param_names[stmt.name] = tuple(a.name for a in stmt.args)
    out: dict[str, dict[int, Type]] = {}
    deps: dict = {}
    for stmt in class_body:
        if not isinstance(stmt, FuncDef):
            continue
        method_dicts: dict[str, tuple[str, ...]] = {}
        forward = None
        if forwardable is not None:
            params = forwardable.get(stmt.name)
            if params is not None:
                forward = (params, deps)
        for body_stmt in stmt.body:
            _collect_self_method_arg_types_stmt(
                ctx, body_stmt, method_dicts, out, param_names, forward
            )
    if len(deps) > 0:
        _resolve_forwarded_self_method_args(
            out, deps, previous if previous is not None else {}
        )
    return out


def _forwarded_source_type(source, out, previous, values, present):
    """The type a forwarded parameter has in this round's body, or None.

    None means the source slot is observed but not solved yet.  A source no
    call observes, or one whose method is poisoned or whose override is dyn,
    is a dyn parameter.
    """
    if source not in present:
        return TYPE_DYN
    method_name = source[0]
    index = source[1]
    current_slots = out.get(method_name)
    if current_slots is not None and -1 in current_slots:
        return TYPE_DYN
    previous_slots = previous.get(method_name)
    if previous_slots is not None:
        if -1 in previous_slots:
            return TYPE_DYN
        if isinstance(previous_slots.get(index), DynType):
            return TYPE_DYN
    return values.get(source)


def _join_forwarded_slot(current, ty):
    if ty is None:
        return current
    if current is None:
        return ty
    if isinstance(current, DynType):
        return current
    if isinstance(ty, DynType) or not type_eq(current, ty):
        return TYPE_DYN
    return current


def _resolve_forwarded_self_method_args(
    out: dict[str, dict[int, Type]],
    deps: dict,
    previous: dict,
) -> None:
    """Give each forwarded argument the type of the parameter it forwards.

    ``self.m(p)``, with ``p`` a parameter the caller never rebinds, passes
    exactly ``p``'s value, and ``p``'s type is itself the join of the calls
    feeding it.  The slots are solved together, least first, so a chain of
    forwards is typed in one round instead of freezing the dyn default of a
    not-yet-typed parameter.  A slot fed only through a forwarding cycle has
    no typed source; its parameter stays dyn, and so does everything
    forwarded from it.
    """
    values: dict = {}
    present: set = set()
    for method_name, slots in out.items():
        for index, ty in slots.items():
            if index >= 0:
                values[(method_name, index)] = ty
                present.add((method_name, index))
    for target in deps:
        present.add(target)
    while True:
        changed = False
        for target, sources in deps.items():
            current = values.get(target)
            joined = current
            for source in sources:
                joined = _join_forwarded_slot(
                    joined,
                    _forwarded_source_type(source, out, previous, values, present),
                )
            if joined is None:
                continue
            if current is None or (
                not isinstance(current, DynType) and isinstance(joined, DynType)
            ):
                values[target] = joined
                changed = True
        if changed:
            continue
        untyped = [slot for slot in present if values.get(slot) is None]
        if len(untyped) == 0:
            break
        for slot in untyped:
            values[slot] = TYPE_DYN
    for target in deps:
        ty = values.get(target)
        if ty is not None:
            out.setdefault(target[0], {})[target[1]] = ty


def _merge_method_arg_overrides(
    old: dict[str, dict[int, Type]],
    new: dict[str, dict[int, Type]],
) -> tuple[dict[str, dict[int, Type]], bool]:
    """Monotone join of per-round observations into the class's overrides.

    A slot only ever moves from absent to a type to dyn.  Dyn entries stay in
    the result so a widening survives into the next round; ``_infer_funcdef``
    types a parameter only from a non-dyn slot of an unpoisoned method.
    """
    merged = {name: dict(slots) for name, slots in old.items()}
    changed = False
    for method_name, slots in new.items():
        dst = merged.setdefault(method_name, {})
        for index, ty in slots.items():
            existing = dst.get(index)
            if existing is None:
                dst[index] = ty
                changed = True
            elif isinstance(existing, DynType):
                continue
            elif isinstance(ty, DynType) or not type_eq(existing, ty):
                dst[index] = TYPE_DYN
                changed = True
    return merged, changed


def _method_override_preserves_object_abi(ty: Type) -> bool:
    """Keep local method observations from narrowing an unannotated ABI.

    Closed-world exports describe a missing annotation as Dyn. Calls observed
    through ``self`` are useful object-shape hints, but do not cover callers
    outside this class. They cannot turn that pointer parameter into a scalar
    or value payload: a consumer still passes the original Python object.
    """
    if isinstance(ty, ClassType) and bool(getattr(ty, "valueclass", False)):
        return False
    return isinstance(
        ty,
        (
            DynType, StrType, BytesType, ByteArrayType, MemoryViewType,
            ListType, SetType, DictType, TupleType, ClassType, ComplexType,
            NoneType, FuncType,
        ),
    )


def _infer_funcdef(
    ctx: _InferCtx,
    scope: _Scope,
    fn: FuncDef,
    *,
    self_ty: Optional[ClassType] = None,
    arg_overrides: Optional[dict[int, Type]] = None,
) -> FuncDef:
    # Resolve argument annotations up-front.
    new_args: list[Arg] = []
    param_scope = _Scope(parent=scope)
    host_param_names = ctx.contextual_host_params.get(fn.name, ())
    for index, a in enumerate(fn.args):
        ty = ctx.resolve_annotation(a.annotation)
        if (
            self_ty is not None
            and index == 0
            and a.name in ("self", "cls")
            and a.annotation is None
        ):
            ty = self_ty
            self_ty_name = _class_type_name(self_ty)
            self_ty_module = _class_type_module(self_ty)
            if isinstance(self_ty, ClassType) and (
                (
                    self_ty_name == "L1CodeGen"
                    and self_ty_module == "pcc.frontends.python.codegen.layer1"
                )
                or (
                    self_ty_name == "L1CodeGenMixinStack"
                    and self_ty_module == "pcc.frontends.python.codegen.layer1_mixins"
                )
                or (
                    self_ty_name == "L1CodeGenEntrypointMixin"
                    and self_ty_module == "pcc.frontends.python.codegen.layer1_entrypoints"
                )
            ):
                ty = ctx.l1_codegen_host_type()
        if a.annotation is None and a.name in host_param_names:
            ty = ctx.l1_codegen_host_type()
        if (
            arg_overrides is not None
            and a.annotation is None
            and index in arg_overrides
            and -1 not in arg_overrides
            and not isinstance(arg_overrides[index], DynType)
        ):
            inferred_arg = ctx.resolve_type_refs(arg_overrides[index])
            if _method_override_preserves_object_abi(inferred_arg):
                ty = inferred_arg
        default = (
            _infer_expr(ctx, param_scope, a.default)
            if a.default is not None
            else None
        )
        if default is not None and _is_raw_int_type(ty):
            default = _contextualize_raw_int_constant(default, ty)
        new_args.append(replace(a, annotation=ty, default=default))
        # Python variadic annotations describe each supplied value, while
        # the body receives a tuple or dict. Keep the formal annotation for
        # signature metadata; only the local binding has the carrier type.
        # Unannotated and unresolved annotations (including unsupported
        # Unpack forms) retain their existing dynamic inference.
        local_ty = ty
        if a.annotation is not None and not isinstance(ty, DynType):
            if a.kind == "*args":
                local_ty = TupleType(name="tuple_variadic", elems=(ty,))
            elif a.kind == "**kwargs":
                local_ty = DictType(name="dict", key=TYPE_STR, value=ty)
        param_scope.define(a.name, local_ty)

    ret_ty = ctx.resolve_annotation(fn.return_ty)
    # ``-> bool`` on a rich comparison is a typing convention: the method may
    # still answer NotImplemented, which a bool return would turn into True
    # (``rank == "x"`` was True). Keep such a result boxed.
    if isinstance(ret_ty, BoolType) and _funcdef_returns_notimplemented(fn):
        ret_ty = TYPE_DYN
    if fn.is_async:
        func_ret_ty = DynType(name="coroutine")
    elif _funcdef_contains_yield_sentinel(fn):
        func_ret_ty = DynType(name="generator")
    else:
        func_ret_ty = ret_ty

    # Record the function's full type in the module-level table *before*
    # walking the body so recursive calls see it.
    ft = FuncType(
        name="callable",
        params=tuple(
            a.annotation if isinstance(a.annotation, Type) else TYPE_DYN
            for a in new_args
        ),
        ret=func_ret_ty,
    )
    ctx.func_types[fn.name] = ft
    # Also expose the function by name so lookups inside the body find
    # the binding.  Outer scope (module or enclosing def) keeps a copy
    # through ``scope.update`` below.
    param_scope.define(fn.name, ft)
    scope.update(fn.name, ft)

    saved_widen_names = ctx.setdefault_none_widen_names
    ctx.setdefault_none_widen_names = _collect_setdefault_none_receivers(fn.body)
    new_body_items: list[Stmt] = []
    for body_stmt in fn.body:
        new_body_items.append(_infer_stmt(ctx, param_scope, body_stmt))
    new_body = tuple(new_body_items)
    ctx.setdefault_none_widen_names = saved_widen_names

    new_body = _contextualize_raw_int_returns(new_body, ret_ty)

    # Type-check ``return`` against ``ret_ty``.
    if not fn.is_async and not isinstance(ret_ty, DynType):
        _check_returns(new_body, ret_ty)

    return FuncDef(
        span=fn.span,
        name=fn.name,
        args=tuple(new_args),
        return_ty=ret_ty,
        body=new_body,
        decorators=fn.decorators,
        is_method=fn.is_method,
        is_async=fn.is_async,
        has_return_annotation=fn.has_return_annotation,
        manual_pointer_abi=ctx.pointer_lane,
    )


def _container_method_return_type(
    recv_ty: Type,
    method: str,
) -> Optional[Type]:
    """Known return types for the typed-container fast-path methods.

    Keeps ``method`` result typed so that chained calls like
    ``s.strip().upper()`` stay on the pcc-native dispatch path rather
    than falling through to CPython — which would break the libpython
    free-standing guarantee.
    """
    if isinstance(recv_ty, StrType):
        if method == "count":
            return IntType(name="int")
        if method in (
            "isdigit",
            "isalpha",
            "isspace",
            "isalnum",
            "isupper",
            "islower",
            "isascii",
            "isidentifier",
            "isprintable",
            "isnumeric",
            "isdecimal",
            "istitle",
        ):
            return BoolType(name="bool")
        if method == "encode":
            return BytesType(name="bytes")
        if method in (
            "upper",
            "lower",
            "strip",
            "lstrip",
            "rstrip",
            "replace",
            "join",
            "split",
            "splitlines",
        ):
            if method in ("split", "splitlines"):
                return ListType(name="list", elem=StrType(name="str"))
            if method == "join":
                return StrType(name="str")
            return StrType(name="str")
        if method in ("startswith", "endswith"):
            return BoolType(name="bool")
        if method == "find":
            return IntType(name="int")
    if isinstance(recv_ty, (BytesType, ByteArrayType)):
        if method == "decode":
            return StrType(name="str")
        if method == "upper":
            return recv_ty
    if isinstance(recv_ty, ByteArrayType):
        if method == "pop":
            return IntType(name="int")
        if method in ("append", "extend", "insert"):
            return NoneType(name="None")
    if isinstance(recv_ty, DynType) and recv_ty.name == "module:math":
        if method in ("floor", "ceil", "trunc", "gcd"):
            return TYPE_INT
        if method == "sqrt":
            return TYPE_FLOAT
    if isinstance(recv_ty, ListType):
        if method == "copy":
            return recv_ty
        if method == "pop":
            return recv_ty.elem
        if method == "index":
            return IntType(name="int")
        if method in ("append", "extend", "insert", "remove", "sort"):
            return NoneType(name="None")
    if isinstance(recv_ty, DictType):
        if method == "copy":
            return recv_ty
        if method in ("get", "pop", "setdefault"):
            return recv_ty.value
        if method == "keys":
            return ListType(name="list", elem=recv_ty.key)
        if method == "values":
            return ListType(name="list", elem=recv_ty.value)
        if method == "items":
            return ListType(
                name="list",
                elem=TupleType(
                    name="tuple",
                    elems=(recv_ty.key, recv_ty.value),
                ),
            )
    if isinstance(recv_ty, SetType):
        if method == "copy":
            return recv_ty
        if method in ("issubset", "issuperset"):
            return BoolType(name="bool")
    return None


def _tuple_type_parts(ty: Type) -> Optional[tuple[str, tuple[Type, ...]]]:
    if isinstance(ty, TupleType):
        return (ty.name, ty.elems)
    try:
        name = ty.name
    except AttributeError:
        return None
    if name not in ("tuple", "tuple_variadic"):
        return None
    try:
        elems = ty.elems
    except AttributeError:
        return None
    try:
        len(elems)
    except Exception:
        return None
    return (name, elems)


def _is_assignable(declared: Type, got: Type) -> bool:
    """Return True if ``got`` is assignable to a slot declared ``declared``.

    Beyond ``type_eq``, this permits ``DynType`` to flow into any
    declared slot (forward subsumption, mirroring the callsite checks
    elsewhere) and recursively descends into ``TupleType`` / ``ListType``
    / ``DictType`` so that e.g. ``tuple[dyn, bool]`` satisfies
    ``tuple[str, bool]`` — which happens whenever a local was typed
    dynamically by a side assignment but the annotation is concrete.
    """
    if isinstance(got, DynType) or isinstance(declared, DynType):
        return True
    if type_eq(declared, got):
        return True
    if _is_none_type(got) and not isinstance(declared, (IntType, FloatType, BoolType)):
        return True
    if _builtin_container_name_assignable(declared, got):
        return True
    if isinstance(declared, ClassType):
        if _runtime_type_object_assignable(declared, got):
            return True
        if _builtin_container_class_assignable(declared, got):
            return True
        if isinstance(got, ClassType):
            return _class_type_assignable(declared, got)
    declared_tuple = _tuple_type_parts(declared)
    got_tuple = _tuple_type_parts(got)
    if declared_tuple is not None and got_tuple is not None:
        declared_name, declared_elems = declared_tuple
        got_name, got_elems = got_tuple
        # ``tuple[T, ...]`` — variadic declared tuple matches any
        # tuple whose every element is assignable to ``T``.
        if declared_name == "tuple_variadic" and declared_elems:
            elem_ty = declared_elems[0]
            return all(_is_assignable(elem_ty, g) for g in got_elems)
        # A variadic-got flowing into a fixed-arity declared form is
        # treated conservatively: assignable when the got's element
        # type subsumes every declared slot.
        if got_name == "tuple_variadic" and got_elems:
            got_elem = got_elems[0]
            return all(_is_assignable(d, got_elem) for d in declared_elems)
        if len(declared_elems) != len(got_elems):
            return False
        i = 0
        while i < len(declared_elems):
            if not _is_assignable(declared_elems[i], got_elems[i]):
                return False
            i += 1
        return True
    declared_list_elem = _list_type_elem(declared)
    got_list_elem = _list_type_elem(got)
    if declared_list_elem is not None and got_list_elem is not None:
        return _is_assignable(declared_list_elem, got_list_elem)
    if isinstance(declared, SetType) and isinstance(got, SetType):
        return declared.name == got.name and _is_assignable(declared.elem, got.elem)
    declared_dict = _dict_type_parts(declared)
    got_dict = _dict_type_parts(got)
    if declared_dict is not None and got_dict is not None:
        declared_key, declared_value = declared_dict
        got_key, got_value = got_dict
        return _is_assignable(declared_key, got_key) and _is_assignable(
            declared_value, got_value
        )
    return False


def _builtin_container_name_assignable(declared: Type, got: Type) -> bool:
    name = getattr(declared, "name", "")
    if name == "list" and _list_type_elem(got) is not None:
        return True
    if name == "dict" and _dict_type_parts(got) is not None:
        return True
    if name == "tuple" and _tuple_type_parts(got) is not None:
        return True
    return False


def _runtime_type_object_assignable(declared: ClassType, got: Type) -> bool:
    """Compatibility for pcc's meta-level Type objects.

    The frontend type lattice uses ``IntType`` / ``NoneType`` instances
    both to describe runtime Python values and as the objects returned by
    helpers such as ``parse_annotation() -> Type``. When annotations like
    ``Type`` / ``NoneType`` are preserved as ``ClassType`` refs, those
    existing meta values must remain assignable.
    """
    if declared.name == "Type" and isinstance(got, Type):
        return True
    if declared.name == "IntType" and isinstance(got, IntType):
        return True
    if declared.name == "FloatType" and isinstance(got, FloatType):
        return True
    if declared.name == "BoolType" and isinstance(got, BoolType):
        return True
    if declared.name == "NoneType" and _is_none_type(got):
        return True
    if declared.name == "StrType" and isinstance(got, StrType):
        return True
    if declared.name == "ListType" and isinstance(got, ListType):
        return True
    if declared.name == "DictType" and isinstance(got, DictType):
        return True
    if declared.name == "TupleType" and isinstance(got, TupleType):
        return True
    if declared.name == "FuncType" and isinstance(got, FuncType):
        return True
    if declared.name == "ClassType" and isinstance(got, ClassType):
        return True
    if declared.name == "DynType" and isinstance(got, DynType):
        return True
    return False


def _builtin_container_class_assignable(declared: ClassType, got: Type) -> bool:
    if declared.name == "list" and isinstance(got, ListType):
        return True
    if declared.name == "dict" and isinstance(got, DictType):
        return True
    if declared.name == "tuple" and isinstance(got, TupleType):
        return True
    return False


def _class_type_is_unresolved_shell(ty: ClassType) -> bool:
    return (
        not _class_type_module(ty)
        and not _class_type_fields(ty)
        and not _class_type_bases(ty)
    )


def _class_type_assignable(declared: ClassType, got: ClassType) -> bool:
    declared_name = _class_type_name(declared)
    declared_module = _class_type_module(declared)
    got_name = _class_type_name(got)
    got_module = _class_type_module(got)
    if declared_name == got_name and (
        declared_module == got_module or not declared_module or not got_module
    ):
        return True
    if (
        declared_name == got_name
        and declared_module
        in (
            "pcc.frontends.python.py_ast",
            "pcc.frontends.python.types",
        )
        and got_module
        in (
            "pcc.frontends.python.py_ast",
            "pcc.frontends.python.types",
        )
    ):
        return True
    # An annotation imported from a module whose schema is not available
    # (the per-module self-compile probe runs without external_exports)
    # must behave like the old DynType path. Preserve strict subclass
    # checks only once at least one side carries real schema/module data.
    if _class_type_is_unresolved_shell(declared) or _class_type_is_unresolved_shell(
        got
    ):
        return True
    for base in _class_type_bases(got):
        if _class_type_assignable(declared, base):
            return True
    return False


def _contextualize_raw_int_returns(
    body: tuple[Stmt, ...],
    ret_ty: Type,
) -> tuple[Stmt, ...]:
    if not _is_raw_int_type(ret_ty):
        return body
    out: list[Stmt] = []
    for stmt in body:
        if isinstance(stmt, Return) and stmt.value is not None:
            out.append(
                replace(
                    stmt,
                    value=_contextualize_raw_int_constant(stmt.value, ret_ty),
                )
            )
        elif isinstance(stmt, If):
            out.append(
                replace(
                    stmt,
                    body=_contextualize_raw_int_returns(stmt.body, ret_ty),
                    else_body=_contextualize_raw_int_returns(
                        stmt.else_body,
                        ret_ty,
                    ),
                )
            )
        elif isinstance(stmt, While):
            out.append(
                replace(
                    stmt,
                    body=_contextualize_raw_int_returns(stmt.body, ret_ty),
                    else_body=_contextualize_raw_int_returns(
                        stmt.else_body,
                        ret_ty,
                    ),
                )
            )
        elif isinstance(stmt, For):
            out.append(
                replace(
                    stmt,
                    body=_contextualize_raw_int_returns(stmt.body, ret_ty),
                    else_body=_contextualize_raw_int_returns(
                        stmt.else_body,
                        ret_ty,
                    ),
                )
            )
        elif isinstance(stmt, Try):
            handlers = []
            for handler in stmt.handlers:
                handlers.append(
                    replace(
                        handler,
                        body=_contextualize_raw_int_returns(
                            handler.body,
                            ret_ty,
                        ),
                    )
                )
            out.append(
                replace(
                    stmt,
                    body=_contextualize_raw_int_returns(stmt.body, ret_ty),
                    handlers=tuple(handlers),
                    else_body=_contextualize_raw_int_returns(
                        stmt.else_body,
                        ret_ty,
                    ),
                    finally_body=_contextualize_raw_int_returns(
                        stmt.finally_body,
                        ret_ty,
                    ),
                )
            )
        elif isinstance(stmt, With):
            out.append(
                replace(
                    stmt,
                    body=_contextualize_raw_int_returns(stmt.body, ret_ty),
                )
            )
        else:
            out.append(stmt)
    return tuple(out)


def _check_returns(body: tuple[Stmt, ...], ret_ty: Type) -> None:
    for s in body:
        if isinstance(s, Return):
            if s.value is None:
                if not _is_none_type(ret_ty):
                    _raise_frontend_error(
                        s.span,
                        f"function annotated to return {ret_ty.name!r} but returns no value",
                        f"return a value of type {ret_ty.name!r}",
                    )
            else:
                vty = s.value.ty
                if isinstance(vty, DynType) or isinstance(ret_ty, DynType):
                    continue
                if type_eq(ret_ty, vty):
                    continue
                # ``Optional[T]`` is unwrapped to ``T`` at parse time
                # (see ``pcc/frontends/python/py_lift.py``). A bare ``return None``
                # against any non-``NoneType`` annotation is treated as
                # the ``Optional[T]`` legitimate-None branch. This
                # preserves Python's documented ``Optional[T]`` ≡
                # ``T | None`` semantics without introducing a Union
                # type into Phase 1.
                if _is_none_type(vty):
                    continue
                if isinstance(ret_ty, FloatType) and isinstance(
                    vty, (IntType, BoolType)
                ):
                    continue
                if isinstance(ret_ty, IntType) and isinstance(vty, BoolType):
                    continue
                if _is_assignable(ret_ty, vty):
                    continue
                _raise_frontend_error(
                    s.span,
                    f"return type mismatch: expected {ret_ty.name!r}, got {vty.name!r}",
                    "change the annotation or convert the value",
                )
        elif isinstance(s, If):
            _check_returns(s.body, ret_ty)
            _check_returns(s.else_body, ret_ty)
        elif isinstance(s, While):
            _check_returns(s.body, ret_ty)
            _check_returns(s.else_body, ret_ty)
        elif isinstance(s, For):
            _check_returns(s.body, ret_ty)
            _check_returns(s.else_body, ret_ty)
        elif isinstance(s, Try):
            _check_returns(s.body, ret_ty)
            for h in s.handlers:
                _check_returns(h.body, ret_ty)
            _check_returns(s.else_body, ret_ty)
            _check_returns(s.finally_body, ret_ty)
        elif isinstance(s, With):
            _check_returns(s.body, ret_ty)


# ---------------------------------------------------------------------------
# Module entrypoint
# ---------------------------------------------------------------------------


def _prepopulate_module_scope(ctx: _InferCtx, module: Module) -> None:
    """Seed module scope with imports, class schemas, and function signatures.

    This lets forward references and mutual recursion work: every ``def``
    at module scope is registered with its annotated (or ``dyn``) signature
    first, and every local class name is registered as a schema-bearing
    ``ClassType`` before function bodies resolve annotations.
    """
    if ctx.external_exports:
        _preload_unique_external_classes(ctx)

    if ctx.external_exports:
        for stmt in module.body:
            if isinstance(stmt, Import):
                for module_name, alias in stmt.names:
                    _bind_external_module_exports(ctx, module_name, alias or module_name)
            if isinstance(stmt, ImportFrom):
                resolved = _resolve_relative_module(
                    _import_from_module_or_empty(stmt),
                    _import_from_level_or_zero(stmt),
                    _ctx_module_name(ctx),
                    stmt.span.file,
                )
                _bind_ir_compat_module_alias(ctx, ctx.globals, resolved, stmt.names)
                _bind_external_import_exports(ctx, ctx.globals, resolved, stmt.names)

    for stmt in module.body:
        if isinstance(stmt, ClassDef):
            cls_ty = _make_class_type(
                stmt.name,
                module.name or "",
                (),
                (),
                valueclass=_class_has_valueclass_decorator(stmt),
            )
            ctx.register_class_type(stmt.name, cls_ty)
            ctx.globals.define(stmt.name, cls_ty)
            ctx.globals.class_objects.add(stmt.name)

    _validate_valueclass_recursion(ctx, module)

    for stmt in module.body:
        if isinstance(stmt, ClassDef):
            if _class_has_valueclass_decorator(stmt):
                _validate_valueclass_shape(ctx, stmt)
            bases = _class_bases_from_def(ctx, stmt)
            fields = _class_fields_from_def(ctx, stmt)
            properties = _class_properties_from_def(ctx, stmt)
            cls_ty = _make_class_type(
                stmt.name,
                module.name or "",
                fields,
                bases,
                properties,
                valueclass=_class_has_valueclass_decorator(stmt),
            )
            ctx.register_class_type(stmt.name, cls_ty)
            ctx.globals.define(stmt.name, cls_ty)
            ctx.globals.class_objects.add(stmt.name)

    for stmt in module.body:
        if not isinstance(stmt, Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, Name):
            continue
        # A PEP 604 union is a `BinOp`, not a `Subscript`, so
        # `LinkInput = bytes | NativeObject | ...` was never recorded and every
        # `value: LinkInput` became a phantom `ClassType("LinkInput")`.  The
        # symptom is silent: `isinstance(value, bytes)` compiles against that
        # phantom class and answers False for real bytes, which is how the
        # owned linker rejected its own archive members under self-host.
        # Inline `value: bytes | X` already resolves, so only the alias form
        # was missing.
        is_union = isinstance(stmt.value, BinOp) and stmt.value.op == "|"
        if not isinstance(stmt.value, Subscript) and not is_union:
            continue
        # Module-level type alias: ``Instruction = dict[Engine, list]``,
        # ``Engine = Literal[...]``. Record so annotations naming the
        # alias resolve to the aliased type instead of a phantom
        # ``ClassType`` (which would make codegen emit instance-field
        # access on plain container values). ``parse_annotation`` returns
        # ``dyn`` for runtime subscripts (``row = matrix[i]``), so those
        # are never recorded; ``Literal[...]`` is recorded as ``dyn``
        # explicitly since its parse is also ``dyn``.
        if is_union:
            # Record even when the union parses to `dyn`: the name denotes a
            # *type*, and "unknown type" is the sound answer.  Skipping it
            # left the phantom `ClassType(alias)` in place, which is the
            # failure mode this whole table exists to prevent -- the same
            # reason `Literal[...]` is recorded as `dyn` just below.
            ctx.type_aliases[target.ident] = parse_annotation(stmt.value)
            continue
        head_obj = stmt.value.obj
        head = None
        if isinstance(head_obj, Name):
            head = head_obj.ident
        elif isinstance(head_obj, Attr) and isinstance(head_obj.obj, Name):
            head = head_obj.obj.ident + "." + head_obj.name
        if head in ("Literal", "typing.Literal"):
            ctx.type_aliases[target.ident] = TYPE_DYN
            continue
        parsed = parse_annotation(stmt.value)
        if not isinstance(parsed, DynType):
            ctx.type_aliases[target.ident] = parsed

    for stmt in module.body:
        if isinstance(stmt, FuncDef):
            params = tuple(ctx.resolve_annotation(a.annotation) for a in stmt.args)
            ret = ctx.resolve_annotation(stmt.return_ty)
            ft = _make_func_type(params, ret)
            ctx.func_types[stmt.name] = ft
            ctx.globals.define(stmt.name, ft)


def _resolve_relative_module(
    module: Optional[str],
    level: int,
    current: Optional[str],
    current_file: Optional[str] = None,
) -> str:
    """Mirror of ``layer1._resolve_relative_import``. Needed at
    inference time so cross-module exports lookup uses the
    absolute dotted name."""
    level = level or 0
    if level == 0:
        return module or ""
    cur = current or ""
    parts = cur.split(".") if cur else []
    current_file = (current_file or "").replace("\\", "/")
    is_package_init = current_file == "__init__.py" or current_file.endswith(
        "/__init__.py"
    )
    package_parts = parts if is_package_init else parts[:-1]
    up = level - 1
    if up > len(package_parts):
        return module or ""
    base_parts = package_parts[: len(package_parts) - up]
    if module:
        return ".".join(base_parts + [module])
    return ".".join(base_parts)


def _annotation_to_type(value) -> Type:
    """Normalize an annotation field (already-resolved Type or raw
    Expr) into a Type. Mirrors ``_InferCtx.resolve_annotation``."""
    if value is None:
        return TYPE_DYN
    if isinstance(value, Type):
        return value
    if isinstance(value, Expr):
        return parse_annotation(value)
    return TYPE_DYN


def infer_module(
    m: Module,
    *,
    external_exports=None,
    derived_class_map=None,
    unique_external_class_preload=None,
    contextual_host_params=None,
) -> Module:
    """Run type inference over an entire module and return a new ``Module``.

    The returned module has every expression's ``ty`` filled in with the
    best type Phase 1 could determine (``DynType`` where we cannot).
    Annotations on ``Arg``/``Assign``/``FuncDef`` that were parsed as
    surface ``Expr`` nodes are replaced with resolved ``Type`` instances.

    ``external_exports`` is the multi-file compile pre-pass table
    ``{dotted_module: {name: export_info}}`` — when supplied,
    ``from .sibling import fn`` bindings are typed from the
    sibling's exported ``FuncType``/``ClassType`` instead of
    falling through to ``DynType`` at call sites.

    ``derived_class_map`` is the inverse base→derived table built by
    the multi-file pipeline: for every base class with a unique
    derived class in the closure, the value is ``(derived_module,
    derived_class_name)``. Mixin methods get type-inferred with
    ``self_ty=derived_class`` so cross-module ``self.X`` resolves
    against the derived class's full field schema. Single-file
    compiles pass ``None``.

    ``contextual_host_params`` is an opt-in helper extraction hook:
    ``{function_name: ("host", ...)}`` marks those unannotated params as
    the synthetic ``L1CodeGen`` host type so helper modules can type
    ``host._fresh`` / ``host.builder`` without immediately falling to
    ``DynType``.
    """

    from pcc.frontends.python.async_context_lowering import lower_async_contexts
    m = lower_async_contexts(m)
    ctx = _InferCtx(
        m,
        external_exports=external_exports,
        derived_class_map=derived_class_map,
        unique_external_class_preload=unique_external_class_preload,
        contextual_host_params=contextual_host_params,
    )
    _prepopulate_module_scope(ctx, m)
    new_body = []
    for stmt in tuple(m.body):
        typed_stmt = _infer_stmt(ctx, ctx.globals, stmt)
        new_body.append(typed_stmt)
    new_body = tuple(new_body)
    if ctx.freestanding:
        _validate_freestanding_plain_int_stmts(new_body)
    return replace(m, body=new_body)


__all__ = ["infer_module", "PyFrontendError"]
