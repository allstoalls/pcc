"""``isinstance`` / ``issubclass`` helper bodies for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BinOp,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    DictType,
    DynType,
    Expr,
    FloatType,
    IntLit,
    IntType,
    ListType,
    Name,
    NoneLit,
    NoneType,
    SetType,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
)
from .builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from .errors import L1CodegenError
from .freestanding_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_DICT,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_NONE,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)
from .runtime_abi import declare_runtime_global

_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_BUILTIN_TYPE_MATCHERS = {
    "str": StrType,
    "int": IntType,
    "float": FloatType,
    "bool": BoolType,
    "list": ListType,
    "dict": DictType,
    "tuple": TupleType,
    "set": SetType,
    "bytes": BytesType,
    "bytearray": ByteArrayType,
    "NoneType": NoneType,
}
_BUILTIN_TYPE_TAGS = {
    "NoneType": PY_TYPE_NONE,
    "bool": PY_TYPE_BOOL,
    "int": PY_TYPE_INT,
    "float": PY_TYPE_FLOAT,
    "str": PY_TYPE_STR,
    "list": PY_TYPE_LIST,
    "dict": PY_TYPE_DICT,
    "tuple": PY_TYPE_TUPLE,
    "set": PY_TYPE_SET,
    "FunctionType": PY_TYPE_FUNC,
    "bytes": PY_TYPE_BYTES,
    "bytearray": PY_TYPE_BYTEARRAY,
}


def emit_code_type_runtime_isinstance_impl(
    host,
    obj_expr: Expr,
    obj_val: Optional[ir.Value] = None,
) -> ir.Value:
    """Match native ``__code__`` objects against ``types.CodeType``."""
    if obj_val is None:
        obj_val = host._emit_as_object(obj_expr)
    code_cls_gv = declare_runtime_global(host.module, "py_func_code_class_cache")
    code_cls = host.builder.load(
        code_cls_gv,
        name=host._fresh("code.class"),
    )
    raw = host.builder.call(
        host.runtime["py_obj_isinstance"],
        [obj_val, code_cls],
        name=host._fresh("isinstance.code"),
    )
    return host.builder.icmp_signed(
        "!=",
        raw,
        ir.Constant(_I64, 0),
        name=host._fresh("isinstance.code.i1"),
    )


def compile_time_isinstance_impl(
    host,
    obj_expr: Expr,
    class_ident: str,
) -> Optional[ir.Value]:
    """Resolve ``isinstance(x, BuiltinType)`` at compile time when the
    operand's static type is known."""
    if class_ident not in _BUILTIN_TYPE_MATCHERS:
        return None
    matcher = _BUILTIN_TYPE_MATCHERS[class_ident]
    ty = obj_expr.ty
    if isinstance(ty, DynType):
        return None
    return ir.Constant(_I1, 1 if isinstance(ty, matcher) else 0)


def emit_builtin_runtime_isinstance_impl(
    host,
    obj_expr: Expr,
    class_ident: str,
    obj_val: Optional[ir.Value] = None,
) -> Optional[ir.Value]:
    if class_ident not in _BUILTIN_TYPE_TAGS:
        return None
    tag = _BUILTIN_TYPE_TAGS[class_ident]
    if obj_val is None:
        obj_val = host._emit_as_object(obj_expr)
    actual = host.builder.call(
        host.runtime["py_obj_type_tag"],
        [obj_val],
        name=host._fresh("obj.type_tag"),
    )
    return host.builder.icmp_signed(
        "==",
        actual,
        ir.Constant(_I64, tag),
        name=host._fresh("builtin.isinstance"),
    )


def ir_scaffold_class_symbol_impl(host, expr: Expr) -> Optional[str]:
    """Return the ``pcc.llvm_capi.ir`` class name for ``ir.X``."""
    if not host._ir_scaffold_enabled():
        return None
    if not isinstance(expr, Attr):
        return None
    symbol = host._ir_module_symbol_target(expr)
    if symbol is None:
        return None
    return symbol


def emit_ir_scaffold_isinstance_impl(
    host,
    obj_val: ir.Value,
    class_name: str,
) -> ir.Value:
    g_name = ".class.pcc_llvm_capi_ir." + class_name
    existing = host.module.globals.get(g_name)
    if existing is None:
        gv = ir.GlobalVariable(host.module, _CSTR, name=g_name)
        gv.linkage = "external"
    else:
        gv = existing
    cls_ptr = host.builder.load(
        gv,
        name=host._fresh("ir.cls." + class_name),
    )
    res_i64 = host.builder.call(
        host.runtime["py_isinstance"],
        [obj_val, cls_ptr],
        name=host._fresh("ir.isinstance." + class_name),
    )
    return host.builder.icmp_signed(
        "!=",
        res_i64,
        ir.Constant(_I64, 0),
        name=host._fresh("ir.isinstance.i1"),
    )


def maybe_emit_issubclass_builtin_impl(host, expr: Call) -> Optional[ir.Value]:
    lhs, rhs = expr.args
    if (
        isinstance(lhs, Name)
        and isinstance(rhs, Name)
        and hasattr(host, "class_lowering")
    ):
        sub_name = host._resolve_class_alias(lhs.ident)
        sup_name = host._resolve_class_alias(rhs.ident)
        if sub_name in host.class_lowering.classes and (
            sup_name == "object" or sup_name in host.class_lowering.classes
        ):
            return ir.Constant(
                _I1,
                1 if host._class_is_subclass(sub_name, sup_name) else 0,
            )
    derived_obj = host._emit_as_object(lhs)
    cls_obj = host._emit_as_object(rhs)
    result = host.builder.call(
        host.runtime["py_obj_issubclass"],
        [derived_obj, cls_obj],
        name=host._fresh("issubclass"),
    )
    host._emit_post_call_err_check(getattr(expr, "span", None))
    return host.builder.icmp_signed(
        "!=",
        result,
        ir.Constant(_I64, 0),
        name=host._fresh("issubclass.bool"),
    )


def class_is_subclass_impl(host, sub_name: str, sup_name: str) -> bool:
    if sub_name == sup_name or sup_name == "object":
        return True
    visited: set[str] = set()
    queue = [sub_name]
    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        info = host.class_lowering.classes.get(name)
        if info is None:
            continue
        for base_expr in info.bases_ast:
            if not isinstance(base_expr, Name):
                continue
            base_name = host._resolve_class_alias(base_expr.ident)
            if base_name == sup_name:
                return True
            if base_name != "object":
                queue.append(base_name)
    return False


def _isinstance_classinfo_as_tuple(class_arg: Expr) -> Expr:
    """Rewrite a PEP 604 ``A | B`` classinfo into the equivalent ``(A, B)``.

    ``isinstance(x, int | str)`` has been accepted by CPython since 3.10 and
    means exactly ``isinstance(x, (int, str))``; the union object is built and
    then thrown away.  The tuple branch below already ORs each member's test,
    so flattening here is the whole feature -- ``pcc/package/uv_lock_sync.py``
    reached the "second argument must be a bare class name..." error on
    ``isinstance(node, ast.List | ast.Tuple)``.

    Only a ``|`` chain is flattened.  Anything else is returned untouched so
    an unrecognized classinfo still reaches the existing diagnostics.
    """
    if not isinstance(class_arg, BinOp) or class_arg.op != "|":
        return class_arg
    members: list = []
    work: list = [class_arg]
    while work:
        node = work.pop()
        if isinstance(node, BinOp) and node.op == "|":
            # Right first, so popping yields left-to-right source order.
            work.append(node.rhs)
            work.append(node.lhs)
            continue
        members.append(node)
    elems = tuple(members)
    return TupleExpr(
        span=class_arg.span,
        ty=TupleType(name="tuple", elems=tuple(m.ty for m in elems)),
        elems=elems,
    )


def emit_isinstance_call_impl(
    host,
    expr: Call,
) -> ir.Value:
    if len(expr.args) != 2:
        raise L1CodegenError("isinstance expects exactly two arguments")
    class_arg = _isinstance_classinfo_as_tuple(expr.args[1])

    def class_name_from_expr(e: Expr) -> tuple[Optional[str], Optional[str]]:
        ir_symbol = host._ir_scaffold_class_symbol(e)
        if isinstance(e, Name):
            return e.ident, None
        if isinstance(e, Attr):
            if isinstance(e.obj, Name):
                host._ensure_native_module_alias_class_export(
                    e.obj.ident,
                    e.name,
                )
            return e.name, ir_symbol
        if (
            isinstance(e, Call)
            and isinstance(e.func, Name)
            and e.func.ident == "type"
            and len(e.args) == 1
            and not e.kwargs
            and isinstance(e.args[0], NoneLit)
        ):
            return "NoneType", None
        return None, None

    def is_dynamic_type_call(e: Expr) -> bool:
        return (
            isinstance(e, Call)
            and isinstance(e.func, Name)
            and e.func.ident == "type"
            and len(e.args) == 1
            and not e.kwargs
        )

    def emit_dynamic_classinfo_isinstance(
        obj_val: ir.Value,
        classinfo_expr: Expr,
    ) -> ir.Value:
        if (
            isinstance(classinfo_expr, Subscript)
            and isinstance(classinfo_expr.obj, Name)
            and isinstance(classinfo_expr.idx, IntLit)
            and classinfo_expr.idx.value == 0
        ):
            # Closure conversion represents a captured/rebound value as a
            # one-element list cell and rewrites reads to ``name[0]``. Going
            # through generic Subscript dunder discovery here recursively
            # re-enters isinstance lowering for captured classinfo. Load the
            # well-defined cell representation directly.
            cell = host._emit_expr(classinfo_expr.obj)
            cls_val = host.builder.call(
                host.runtime["py_list_getitem"],
                [cell, ir.Constant(_I64, 0)],
                name=host._fresh("isinstance.classinfo.cell"),
            )
        else:
            cls_val = host._emit_expr(classinfo_expr)
        raw = host.builder.call(
            host.runtime["py_obj_isinstance"],
            [obj_val, cls_val],
            name=host._fresh("obj.isinstance"),
        )
        return host.builder.icmp_signed(
            "!=",
            raw,
            ir.Constant(_I64, 0),
            name=host._fresh("obj.isinstance.i1"),
        )

    # Tuple form ``isinstance(x, (A, B, C))`` -> OR of per-class checks.
    if isinstance(class_arg, TupleExpr):
        if not class_arg.elems:
            host._emit_as_object(expr.args[0])
            return ir.Constant(_I1, 0)
        names: list[Optional[str]] = []
        ir_class_names: list[Optional[str]] = []
        dynamic_classinfos: list[Optional[Expr]] = []
        for e in class_arg.elems:
            name, ir_symbol = class_name_from_expr(e)
            if name is None:
                if not is_dynamic_type_call(e):
                    raise NotImplementedError(
                        "isinstance tuple form requires bare class names "
                        "module.name chains, type(None), or type(expr); got "
                        f"{type(e).__name__}"
                    )
                dynamic_classinfos.append(e)
            else:
                dynamic_classinfos.append(None)
            names.append(name)
            ir_class_names.append(ir_symbol)
        acc: Optional[ir.Value] = None
        obj_val: Optional[ir.Value] = None
        for idx, nm in enumerate(names):
            dynamic_classinfo = dynamic_classinfos[idx]
            if dynamic_classinfo is not None:
                if obj_val is None:
                    obj_val = host._emit_as_object(expr.args[0])
                ct = emit_dynamic_classinfo_isinstance(
                    obj_val,
                    dynamic_classinfo,
                )
                acc = (
                    ct
                    if acc is None
                    else host.builder.or_(
                        acc,
                        ct,
                        name=host._fresh("isinstance_or"),
                    )
                )
                continue
            assert nm is not None
            nm = host._resolve_class_alias(nm)
            ir_symbol = ir_class_names[idx]
            if ir_symbol is not None:
                if obj_val is None:
                    obj_val = host._emit_as_object(expr.args[0])
                ct = host._emit_ir_scaffold_isinstance(
                    obj_val,
                    ir_symbol,
                )
            else:
                ct = host._compile_time_isinstance(expr.args[0], nm)
            if ct is None:
                if nm in _BUILTIN_TYPE_TAGS:
                    if obj_val is None:
                        obj_val = host._emit_as_object(expr.args[0])
                    ct = host._emit_builtin_runtime_isinstance(
                        expr.args[0],
                        nm,
                        obj_val,
                    )
                elif nm in host.class_lowering.classes:
                    if obj_val is None:
                        obj_val = host._emit_as_object(expr.args[0])
                    ct = host.class_lowering.emit_isinstance(obj_val, nm)
                elif nm in _BUILTIN_EXC_TAG:
                    if obj_val is None:
                        obj_val = host._emit_as_object(expr.args[0])
                    exc_cls_val = host.builder.call(
                        host.runtime["py_exc_builtin_class"],
                        [ir.Constant(_I64, _BUILTIN_EXC_TAG[nm])],
                        name=host._fresh("isinstance.exc_cls"),
                    )
                    exc_raw = host.builder.call(
                        host.runtime["py_obj_isinstance"],
                        [obj_val, exc_cls_val],
                        name=host._fresh("isinstance.exc"),
                    )
                    ct = host.builder.icmp_signed(
                        "!=",
                        exc_raw,
                        ir.Constant(_I64, 0),
                        name=host._fresh("isinstance.exc.i1"),
                    )
                elif nm == "slice":
                    if obj_val is None:
                        obj_val = host._emit_as_object(expr.args[0])
                    slice_raw = host.builder.call(
                        host.runtime["py_obj_is_slice"],
                        [obj_val],
                        name=host._fresh("isinstance.slice"),
                    )
                    ct = host.builder.icmp_signed(
                        "!=",
                        slice_raw,
                        ir.Constant(_I64, 0),
                        name=host._fresh("isinstance.slice.i1"),
                    )
                elif nm == "CodeType":
                    ct = emit_code_type_runtime_isinstance_impl(
                        host,
                        expr.args[0],
                        obj_val,
                    )
                else:
                    ct = ir.Constant(_I1, 0)
            acc = (
                ct
                if acc is None
                else host.builder.or_(
                    acc,
                    ct,
                    name=host._fresh("isinstance_or"),
                )
            )
        assert acc is not None
        return acc

    # ``mod.Class`` second-arg: use tail token as the class name.
    cls_ident, ir_symbol = class_name_from_expr(class_arg)
    if cls_ident is None:
        if is_dynamic_type_call(class_arg) or isinstance(class_arg, Subscript):
            obj_val = host._emit_as_object(expr.args[0])
            return emit_dynamic_classinfo_isinstance(obj_val, class_arg)
        span = getattr(class_arg, "span", None)
        where = ""
        if span is not None:
            where = f" at {span.file}:{span.line}:{span.col}"
        detail = ""
        if isinstance(class_arg, Subscript):
            obj = class_arg.obj
            idx_expr = class_arg.idx
            detail = (
                f" (obj={type(obj).__name__}:{getattr(obj, 'ident', '')}, "
                f"idx={type(idx_expr).__name__}:"
                f"{getattr(idx_expr, 'value', '')})"
            )
        raise NotImplementedError(
            "isinstance second argument must be a bare class name, "
            "a tuple of bare class names, a module.attr chain, type(None), "
            f"or type(expr); got {type(class_arg).__name__}{detail}{where}"
        )
    cls_ident = host._resolve_class_alias(cls_ident)
    protocol_check = host._maybe_emit_protocol_isinstance(
        expr.args[0],
        cls_ident,
    )
    if protocol_check is not None:
        return protocol_check
    ct = host._compile_time_isinstance(expr.args[0], cls_ident)
    if ct is not None:
        return ct
    ct = host._emit_builtin_runtime_isinstance(expr.args[0], cls_ident)
    if ct is not None:
        return ct
    if ir_symbol is not None:
        obj_val = host._emit_as_object(expr.args[0])
        return host._emit_ir_scaffold_isinstance(obj_val, ir_symbol)
    if cls_ident not in host.class_lowering.classes:
        if cls_ident == "slice":
            # isinstance(x, slice): slices are instances of the runtime
            # pcc_slice_cls (not a distinct type tag), so route to the
            # dedicated predicate. Otherwise this fell through to constant
            # False, breaking the common __getitem__(slice) dispatch idiom.
            obj_val = host._emit_as_object(expr.args[0])
            raw = host.builder.call(
                host.runtime["py_obj_is_slice"],
                [obj_val],
                name=host._fresh("isinstance.slice"),
            )
            return host.builder.icmp_signed(
                "!=",
                raw,
                ir.Constant(_I64, 0),
                name=host._fresh("isinstance.slice.i1"),
            )
        if cls_ident == "CodeType":
            return emit_code_type_runtime_isinstance_impl(
                host,
                expr.args[0],
            )
        if cls_ident in _BUILTIN_EXC_TAG:
            # Builtin exception class (ValueError, KeyError, ...) — match the
            # object's exc_class MRO at runtime. Previously this fell through to
            # the constant-False return below, so isinstance(ValueError('x'),
            # ValueError) was always False. Use ``in`` + subscript (self-host
            # safe, like ``nm in _BUILTIN_TYPE_TAGS`` below), not ``.get()``
            # which pcc1 mis-lowers to a KeyError-raising getitem.
            obj_val = host._emit_as_object(expr.args[0])
            cls_val = host.builder.call(
                host.runtime["py_exc_builtin_class"],
                [ir.Constant(_I64, _BUILTIN_EXC_TAG[cls_ident])],
                name=host._fresh("isinstance.exc_cls"),
            )
            raw = host.builder.call(
                host.runtime["py_obj_isinstance"],
                [obj_val, cls_val],
                name=host._fresh("isinstance.exc"),
            )
            return host.builder.icmp_signed(
                "!=",
                raw,
                ir.Constant(_I64, 0),
                name=host._fresh("isinstance.exc.i1"),
            )
        if isinstance(class_arg, Name) and (
            class_arg.ident in host.env
            or class_arg.ident in getattr(host, "_module_globals", {})
        ):
            obj_val = host._emit_as_object(expr.args[0])
            cls_val = host._emit_expr(class_arg)
            raw = host.builder.call(
                host.runtime["py_obj_isinstance"],
                [obj_val, cls_val],
                name=host._fresh("obj.isinstance"),
            )
            return host.builder.icmp_signed(
                "!=",
                raw,
                ir.Constant(_I64, 0),
                name=host._fresh("obj.isinstance.i1"),
            )
        host._emit_as_object(expr.args[0])
        return ir.Constant(_I1, 0)
    obj_val = host._emit_as_object(expr.args[0])
    return host.class_lowering.emit_isinstance(obj_val, cls_ident)


class IsinstanceLoweringMixin:
    # Keep these methods host-owned on L1CodeGen.  The self-hosted stage
    # compiler still relies on the concrete L1CodeGen method table for
    # compiler-internal AST dispatch such as ``isinstance(stmt, Return)``.
    # The bodies above keep the helper logic explicit for contextual
    # per-module probing while this mixin keeps layer1.py small.

    def _compile_time_isinstance(
        self,
        obj_expr: Expr,
        class_ident: str,
    ) -> Optional[ir.Value]:
        return compile_time_isinstance_impl(
            self,
            obj_expr,
            class_ident,
        )

    def _emit_builtin_runtime_isinstance(
        self,
        obj_expr: Expr,
        class_ident: str,
        obj_val: Optional[ir.Value] = None,
    ) -> Optional[ir.Value]:
        return emit_builtin_runtime_isinstance_impl(
            self,
            obj_expr,
            class_ident,
            obj_val,
        )

    def _ir_scaffold_class_symbol(self, expr: Expr) -> Optional[str]:
        return ir_scaffold_class_symbol_impl(self, expr)

    def _emit_ir_scaffold_isinstance(
        self,
        obj_val: ir.Value,
        class_name: str,
    ) -> ir.Value:
        return emit_ir_scaffold_isinstance_impl(self, obj_val, class_name)

    def _maybe_emit_issubclass_builtin(self, expr: Call) -> Optional[ir.Value]:
        return maybe_emit_issubclass_builtin_impl(self, expr)

    def _class_is_subclass(self, sub_name: str, sup_name: str) -> bool:
        return class_is_subclass_impl(self, sub_name, sup_name)

    def _emit_isinstance_call(self, expr: Call) -> ir.Value:
        return emit_isinstance_call_impl(self, expr)
