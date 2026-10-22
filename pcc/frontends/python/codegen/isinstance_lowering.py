"""``isinstance`` / ``issubclass`` helper bodies for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, BinOp, BoolType, ByteArrayType, BytesType, Call, ClassType, ComplexType, DictType, DynType, Expr, FloatType, IntLit, IntType, ListType, Name, NoneLit, NoneType, SetType, StrType, Subscript, TupleExpr, TupleType
from pcc.frontends.python.codegen.builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_BOOL, PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_CLASS, PY_TYPE_COMPLEX, PY_TYPE_DICT, PY_TYPE_FLOAT, PY_TYPE_FUNC, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_NONE, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_TUPLE
from pcc.frontends.python.codegen.hoist_boxing import CELL_READ
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global

_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_BUILTIN_TYPE_MATCHERS = {
    "str": StrType,
    "int": IntType,
    "float": FloatType,
    "complex": ComplexType,
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
    "complex": PY_TYPE_COMPLEX,
    "memoryview": PY_TYPE_MEMORYVIEW,
    "type": PY_TYPE_CLASS,
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
    # ClassType describes the Python class, not an exact builtin layout.
    # Native immutable subclasses keep that class and are checked by the
    # subclass-aware runtime predicates below.
    if isinstance(ty, DynType) or (class_ident in ("str", "tuple") and isinstance(ty, ClassType)):
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
    evaluated_operand = obj_val is None
    if evaluated_operand:
        obj_val = host._emit_as_object(obj_expr)
    if class_ident in ("str", "tuple"):
        helper = "py_str_check" if class_ident == "str" else "py_tuple_check"
        raw = host.builder.call(host.runtime[helper], [obj_val])
        if evaluated_operand:
            host._gc_release_if_owned(obj_val, obj_expr)
        return host.builder.icmp_signed("!=", raw, ir.Constant(_I64, 0))
    if class_ident == "type":
        cls_val = host.builder.call(
            host.runtime["py_builtin_type_for_tag"], [ir.Constant(_I64, PY_TYPE_CLASS)],
            name=host._fresh("isinstance.type.class"),
        )
        raw = host.builder.call(host.runtime["py_obj_isinstance"], [obj_val, cls_val])
        host.builder.call(host.runtime["py_decref"], [cls_val])
        if evaluated_operand:
            host._gc_release_if_owned(obj_val, obj_expr)
        return host.builder.icmp_signed("!=", raw, ir.Constant(_I64, 0))
    actual = host.builder.call(
        host.runtime["py_obj_type_tag"],
        [obj_val],
        name=host._fresh("obj.type_tag"),
    )
    # The runtime predicate borrows. An enclosing tuple check supplies its
    # own operand and retires it after all members have inspected it.
    if evaluated_operand:
        host._gc_release_if_owned(obj_val, obj_expr)
    return host.builder.icmp_signed(
        "==",
        actual,
        ir.Constant(_I64, tag),
        name=host._fresh("builtin.isinstance"),
    )


def ir_scaffold_class_symbol_impl(host, expr: Expr) -> Optional[str]:
    """Return the ``pcc.ir.ir`` class name for ``ir.X``."""
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
    g_name = ".class.pcc_ir_ir." + class_name
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
    # Class names can be rebound or shadowed by parameters. Read both actual
    # values; keep the first alive while a tuple/property classinfo allocates.
    previous_error = host._current_try_err_block()
    target = previous_error if previous_error is not None else host._ensure_fn_err_exit()
    roots = []
    try:
        for argument, label in ((lhs, "issubclass.derived"), (rhs, "issubclass.classinfo")):
            value = host._emit_as_object(argument)
            owned = (
                host._value_is_owned_object(value)
                or host._owned_release_needed(value, argument)
                or host._pcc_pointer_source_is_owned(argument)
            )
            roots.append(host._extern_enter_root(value, owned, label))
            host._try_err_block = host._extern_cleanup_block(tuple(roots), target)
        result = host.builder.call(
            host.runtime["py_obj_issubclass"],
            [host._extern_load_root(roots[0]), host._extern_load_root(roots[1])],
            name=host._fresh("issubclass"),
        )
        host._emit_post_call_err_check(getattr(expr, "span", None))
    finally:
        host._try_err_block = previous_error
        host._extern_release_roots(tuple(roots))
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


def _name_is_imported_into_module(host, ident: str) -> bool:
    """True when this module binds ``ident`` with an import statement.

    ``from x import NoneType`` and ``import x as NoneType`` both shadow a
    builtin type name for the whole module without creating an ``env`` entry
    or a module global, so neither of the other two checks sees them.
    """
    module = getattr(host, "ast_module", None)
    if module is None:
        return False
    for stmt in getattr(module, "body", ()) or ():
        names = getattr(stmt, "names", None)
        if not names or not isinstance(names, tuple):
            continue
        if type(stmt).__name__ not in ("Import", "ImportFrom"):
            continue
        for entry in names:
            if not isinstance(entry, tuple) or not entry:
                continue
            bound = entry[1] if len(entry) > 1 and entry[1] else entry[0]
            if bound == ident:
                return True
    return False


def _name_is_declared_class_in_module(host, ident: str) -> bool:
    module = getattr(host, "ast_module", None)
    for stmt in getattr(module, "body", ()) or ():
        if type(stmt).__name__ == "ClassDef" and stmt.name == ident:
            return True
    return False


def emit_isinstance_call_impl(
    host,
    expr: Call,
) -> ir.Value:
    previous_error = host._current_try_err_block()
    # The operand is evaluated once. Classinfo factories and builtin-class
    # cache misses can allocate before the predicate consumes it.
    operand_root = [None]
    operand_owned = [False]

    def current_operand():
        if operand_root[0] is None:
            value = host._emit_as_object(expr.args[0])
            operand_owned[0] = (
                host._value_is_owned_object(value)
                or host._owned_release_needed(value, expr.args[0])
                or host._pcc_pointer_source_is_owned(expr.args[0])
            )
            operand_root[0] = host._enter_container_temp_root(
                value, host._fresh("isinstance.operand"),
            )
            target = previous_error if previous_error is not None else host._ensure_fn_err_exit()
            host._try_err_block = host._make_cpy_operand_cleanup_block(
                (), (), target, "isinstance.operand.unwind",
                rooted_pcc_lifetimes=((operand_root[0], operand_owned[0]),),
            )
        return host.builder.call(
            host.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), host._as_gc_ptr(operand_root[0])],
            name=host._fresh("isinstance.operand.current"),
        )

    try:
        return _emit_isinstance_call_body_impl(host, expr, current_operand)
    finally:
        host._try_err_block = previous_error
        if operand_root[0] is not None:
            host._release_rooted_pcc_lifetimes(((operand_root[0], operand_owned[0]),))


def _emit_isinstance_call_body_impl(host, expr: Call, current_operand) -> ir.Value:
    if len(expr.args) != 2:
        raise L1CodegenError("isinstance expects exactly two arguments")
    class_arg = _isinstance_classinfo_as_tuple(expr.args[1])

    def class_name_from_expr(e: Expr) -> tuple[Optional[str], Optional[str], bool]:
        ir_symbol = host._ir_scaffold_class_symbol(e)
        if isinstance(e, Name):
            return e.ident, None, False
        if isinstance(e, Attr):
            if ir_symbol is not None:
                return ir_symbol, ir_symbol, False
            if isinstance(e.obj, Name):
                class_info = host._ensure_native_module_alias_class_export(
                    e.obj.ident,
                    e.name,
                )
                if class_info is not None:
                    # The registry qualifies collisions between modules that
                    # export the same leaf class name. Keep that resolved
                    # owner for scalar and tuple classinfo instead of looking
                    # up another module's pre-existing short-name binding.
                    return class_info.name, None, True
            owner = host._native_module_name_for_object_expr(e.obj)
            if owner in ("types", "pcc.stdlib.types") and e.name in (
                "FunctionType", "NoneType", "CodeType",
            ):
                return e.name, None, False
            if (
                owner == "builtins"
                and e.name == "object"
                and host._native_module_attr_global_if_exists(owner, e.name) is not None
            ):
                return None, None, False
            if owner == "builtins" and (
                e.name in _BUILTIN_TYPE_TAGS
                or e.name in _BUILTIN_EXC_TAG
                or e.name in ("slice", "object")
            ):
                return e.name, None, False
            # A field, property, or foreign module export is a classinfo
            # value. Its last token cannot establish builtin type identity.
            return None, None, False
        if (
            isinstance(e, Call)
            and isinstance(e.func, Name)
            and e.func.ident == "type"
            and len(e.args) == 1
            and not e.kwargs
            and isinstance(e.args[0], NoneLit)
        ):
            return "NoneType", None, False
        return None, None, False

    def is_dynamic_type_call(e: Expr) -> bool:
        return (
            isinstance(e, Call)
            and isinstance(e.func, Name)
            and e.func.ident == "type"
            and len(e.args) == 1
            and not e.kwargs
        )

    def is_closure_cell_read(e: Expr) -> bool:
        # Hoisting rewrites a captured Name to this compiler-only operation.
        # It is still a classinfo value read, not an arbitrary source call.
        # Normal expression lowering validates the marker's arguments and
        # preserves owning-local versus free-variable boundness errors.
        return (
            isinstance(e, Call)
            and isinstance(e.func, Name)
            and e.func.ident == CELL_READ
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
            cls_val = host._emit_as_object(classinfo_expr)
        # Cell reads, attributes and subscripts can return a new owner. Keep
        # the classinfo in an authoritative root until the borrowing predicate
        # and its error check finish, then retire that owner on either edge.
        # The root helper preserves any pre-existing pin when obj is cls.
        cls_owned = (
            host._value_is_owned_object(cls_val)
            or host._owned_release_needed(cls_val, classinfo_expr)
            or host._pcc_pointer_source_is_owned(classinfo_expr)
        )
        cls_root = host._extern_enter_root(
            cls_val, cls_owned, "isinstance.classinfo",
        )
        previous_error = host._current_try_err_block()
        target = previous_error if previous_error is not None else host._ensure_fn_err_exit()
        host._try_err_block = host._extern_cleanup_block((cls_root,), target)
        try:
            raw = host.builder.call(
                host.runtime["py_obj_isinstance"],
                [current_operand(), host._extern_load_root(cls_root)],
                name=host._fresh("obj.isinstance"),
            )
            host._emit_post_call_err_check(getattr(classinfo_expr, "span", None))
        finally:
            host._try_err_block = previous_error
            host._extern_release_roots((cls_root,))
        return host.builder.icmp_signed(
            "!=",
            raw,
            ir.Constant(_I64, 0),
            name=host._fresh("obj.isinstance.i1"),
        )

    def name_has_runtime_binding(element: Expr) -> bool:
        return isinstance(element, Name) and (
            element.ident in host.env
            or element.ident in getattr(host, "_module_globals", {})
            or element.ident in getattr(host, "_native_builtin_value_aliases", {})
            or _name_is_imported_into_module(host, element.ident)
            or _name_is_declared_class_in_module(host, element.ident)
        )

    # Keep proven class identities on the existing static routes. A tuple
    # containing a value read must first evaluate *all* its members, left to
    # right, before the runtime starts its short-circuit class checks. Testing
    # each member while evaluating the next one loses that ordering and folds
    # ordinary bound names such as ``kind`` to false. Normal tuple lowering
    # also owns nested tuples, element temporaries and exceptional cleanup.
    if isinstance(class_arg, TupleExpr):
        if not class_arg.elems:
            current_operand()
            return ir.Constant(_I1, 0)
        names: list[str] = []
        ir_class_names: list[Optional[str]] = []
        declared_classinfos: list[bool] = []
        for element in class_arg.elems:
            name, ir_symbol, declared_classinfo = class_name_from_expr(element)
            if name is None or (ir_symbol is None and name_has_runtime_binding(element)):
                return emit_dynamic_classinfo_isinstance(current_operand(), class_arg)
            name = host._resolve_class_alias(name)
            if (
                ir_symbol is None
                and not declared_classinfo
                and name not in _BUILTIN_TYPE_TAGS
                and name not in _BUILTIN_EXC_TAG
                and name not in ("slice", "CodeType", "object")
                and name not in host.class_lowering.classes
            ):
                return emit_dynamic_classinfo_isinstance(current_operand(), class_arg)
            names.append(name)
            ir_class_names.append(ir_symbol)
            declared_classinfos.append(declared_classinfo)
        acc: Optional[ir.Value] = None
        obj_val: Optional[ir.Value] = None
        for idx, nm in enumerate(names):
            ir_symbol = ir_class_names[idx]
            if declared_classinfos[idx]:
                if obj_val is None:
                    obj_val = current_operand()
                ct = host.class_lowering.emit_isinstance(obj_val, nm)
            elif ir_symbol is not None:
                if obj_val is None:
                    obj_val = current_operand()
                ct = host._emit_ir_scaffold_isinstance(
                    obj_val,
                    ir_symbol,
                )
            elif nm == "object":
                # There is no object-only tag: all Python values qualify.
                # Evaluation and ownership still happen exactly once.
                if obj_val is None:
                    obj_val = current_operand()
                ct = ir.Constant(_I1, 1)
            else:
                ct = host._compile_time_isinstance(expr.args[0], nm)
            if ct is None:
                if nm in _BUILTIN_TYPE_TAGS:
                    if obj_val is None:
                        obj_val = current_operand()
                    ct = host._emit_builtin_runtime_isinstance(
                        expr.args[0],
                        nm,
                        obj_val,
                    )
                elif nm in host.class_lowering.classes:
                    if obj_val is None:
                        obj_val = current_operand()
                    ct = host.class_lowering.emit_isinstance(obj_val, nm)
                elif nm in _BUILTIN_EXC_TAG:
                    if obj_val is None:
                        obj_val = current_operand()
                    exc_cls_val = host.builder.call(
                        host.runtime["py_exc_builtin_class"],
                        [ir.Constant(_I64, _BUILTIN_EXC_TAG[nm])],
                        name=host._fresh("isinstance.exc_cls"),
                    )
                    exc_raw = host.builder.call(
                        host.runtime["py_obj_isinstance"],
                        [current_operand(), exc_cls_val],
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
                        obj_val = current_operand()
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
                    obj_val = current_operand()
                    ct = emit_code_type_runtime_isinstance_impl(
                        host,
                        expr.args[0],
                        obj_val,
                    )
            assert ct is not None
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

    cls_ident, ir_symbol, declared_classinfo = class_name_from_expr(class_arg)
    if cls_ident is None:
        if is_dynamic_type_call(class_arg) or is_closure_cell_read(class_arg) or isinstance(class_arg, (Subscript, Attr)):
            obj_val = current_operand()
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
    if ir_symbol is not None:
        obj_val = current_operand()
        return host._emit_ir_scaffold_isinstance(obj_val, ir_symbol)
    if declared_classinfo:
        obj_val = current_operand()
        return host.class_lowering.emit_isinstance(obj_val, cls_ident)
    protocol_check = host._maybe_emit_protocol_isinstance(
        expr.args[0],
        cls_ident,
    )
    if protocol_check is not None:
        return protocol_check
    if (
        ir_symbol is None
        and name_has_runtime_binding(class_arg)
    ):
        # A module/local binding shadows the builtin tag tables: a bare
        # ``NoneType`` name imported from a user module (e.g. the py_ast
        # descriptor class) must dispatch to the imported class object,
        # while the syntactic ``type(None)`` form (a Call, not a Name)
        # keeps the builtin PY_TYPE_NONE check below.
        #
        # ``from py_ast import NoneType`` binds the name without putting it in
        # ``env`` or ``_module_globals``, so the guard used to miss exactly the
        # case it was written for.  pcc's own ``type_infer`` then compiled
        # ``isinstance(known_field_ty, NoneType)`` -- the test that decides
        # whether a field first seen as ``self.x = None`` may be widened by a
        # later write -- into a builtin ``is None`` check, which is False for a
        # ``NoneType()`` descriptor.  Under CPython that line is real Python
        # and answers True, so host pcc widened the field and pcc1 did not:
        # stage2 died with "Layer 1 slice on type NoneType not supported" on
        # ``pcc/frontends/c/ply/lex.py``'s ``self.lexdata[lexpos:]``.
        obj_val = current_operand()
        return emit_dynamic_classinfo_isinstance(obj_val, class_arg)
    if cls_ident == "object":
        current_operand()
        return ir.Constant(_I1, 1)
    # These two were computed-and-discarded when the shadowing guard above
    # landed, which left every builtin-type check falling through to the
    # class-table path and answering False: `isinstance("x", str)`,
    # `isinstance(7, int)`, `isinstance(b"", bytes)` and `isinstance(str, type)`
    # all returned False, and `struct.unpack_from` raised
    # `TypeError: Struct() argument 1 must be a str` on its own format literal.
    ct = host._compile_time_isinstance(expr.args[0], cls_ident)
    if ct is not None:
        return ct
    ct = host._emit_builtin_runtime_isinstance(expr.args[0], cls_ident, current_operand())
    if ct is not None:
        return ct
    if cls_ident not in host.class_lowering.classes:
        if cls_ident == "slice":
            # isinstance(x, slice): slices are instances of the runtime
            # pcc_slice_cls (not a distinct type tag), so route to the
            # dedicated predicate. Otherwise this fell through to constant
            # False, breaking the common __getitem__(slice) dispatch idiom.
            obj_val = current_operand()
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
                current_operand(),
            )
        if cls_ident in _BUILTIN_EXC_TAG:
            # Builtin exception class (ValueError, KeyError, ...) — match the
            # object's exc_class MRO at runtime. Previously this fell through to
            # the constant-False return below, so isinstance(ValueError('x'),
            # ValueError) was always False. Use ``in`` + subscript (self-host
            # safe, like ``nm in _BUILTIN_TYPE_TAGS`` below), not ``.get()``
            # which pcc1 mis-lowers to a KeyError-raising getitem.
            obj_val = current_operand()
            cls_val = host.builder.call(
                host.runtime["py_exc_builtin_class"],
                [ir.Constant(_I64, _BUILTIN_EXC_TAG[cls_ident])],
                name=host._fresh("isinstance.exc_cls"),
            )
            raw = host.builder.call(
                host.runtime["py_obj_isinstance"],
                [current_operand(), cls_val],
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
            obj_val = current_operand()
            cls_val = host._emit_expr(class_arg)
            raw = host.builder.call(
                host.runtime["py_obj_isinstance"],
                [current_operand(), cls_val],
                name=host._fresh("obj.isinstance"),
            )
            return host.builder.icmp_signed(
                "!=",
                raw,
                ir.Constant(_I64, 0),
                name=host._fresh("obj.isinstance.i1"),
            )
        if isinstance(class_arg, Attr):
            # ``isinstance(e, self._KINDS)`` reads a class attribute holding
            # a class or a tuple of classes; its tail names no class, so it is
            # a value, not a ``module.Class`` chain.  This used to fold to
            # False: pcc1 compiled ``_membership_tuple_literal_is_constant``
            # that way and emitted a runtime tuple for every ``x in ("a",
            # "b")`` the host compiler unrolls.
            obj_val = current_operand()
            return emit_dynamic_classinfo_isinstance(obj_val, class_arg)
        current_operand()
        return ir.Constant(_I1, 0)
    obj_val = current_operand()
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
