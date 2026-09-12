"""Expression dispatch lowering for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BinOp,
    BoolExpr,
    BoolLit,
    BoolType,
    BytesLit,
    Call,
    ClassType,
    Compare,
    ComplexLit,
    DictExpr,
    DynType,
    Expr,
    FloatLit,
    FloatType,
    IfExpr,
    IntType,
    IntLit,
    Lambda,
    ListExpr,
    Name,
    NoneLit,
    Slice,
    StrLit,
    Subscript,
    TupleExpr,
    UnaryOp,
)
from .layer1_support import _as_native_float
from .runtime_abi import declare_runtime_global


_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_NATIVE_DEFAULT_FUNC_SENTINEL = "__pcc_native_default_func_ref__"
_NATIVE_DEFAULT_GLOBAL_SENTINEL = "__pcc_native_default_global_ref__"


def _is_class_type_for_expr_dispatch(ty) -> bool:
    if isinstance(ty, ClassType):
        return True
    return type(ty).__name__ in ("ClassType", "ValueClassType")


def _expr_dispatch_kind_name(expr: Expr) -> str:
    try:
        return type(expr).__name__
    except Exception:
        return ""


def _expr_has_attr(expr: Expr, name: str) -> bool:
    return hasattr(expr, name)


def _expr_type_name(expr: Expr) -> str:
    try:
        return expr.ty.name
    except AttributeError:
        return ""


def _expr_is_name(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Name) or (kind == "Name") or _expr_has_attr(expr, "ident")


def _expr_is_list(expr: Expr, kind: str) -> bool:
    if isinstance(expr, ListExpr) or kind == "ListExpr":
        return True
    return _expr_has_attr(expr, "elems") and _expr_type_name(expr) == "list"


def _expr_is_tuple(expr: Expr, kind: str) -> bool:
    if isinstance(expr, TupleExpr) or kind == "TupleExpr":
        return True
    ty_name = _expr_type_name(expr)
    return _expr_has_attr(expr, "elems") and (
        ty_name == "tuple" or ty_name == "tuple_variadic"
    )


def _expr_is_dict(expr: Expr, kind: str) -> bool:
    return isinstance(expr, DictExpr) or kind == "DictExpr" or _expr_has_attr(
        expr, "pairs"
    )


def _expr_is_call(expr: Expr, kind: str) -> bool:
    return (
        isinstance(expr, Call)
        or kind == "Call"
        or (
            _expr_has_attr(expr, "func")
            and _expr_has_attr(expr, "args")
            and _expr_has_attr(expr, "kwargs")
        )
    )


def _expr_native_default_func_ref(expr: Expr):
    if not isinstance(expr, Call):
        return None
    func = expr.func
    if not isinstance(func, Name) or func.ident != _NATIVE_DEFAULT_FUNC_SENTINEL:
        return None
    if len(expr.args) != 2 or expr.kwargs:
        return None
    module_expr = expr.args[0]
    name_expr = expr.args[1]
    if not isinstance(module_expr, StrLit) or not isinstance(name_expr, StrLit):
        return None
    return module_expr.value, name_expr.value


def _expr_native_default_global_ref(expr: Expr):
    if not isinstance(expr, Call):
        return None
    func = expr.func
    if not isinstance(func, Name) or func.ident != _NATIVE_DEFAULT_GLOBAL_SENTINEL:
        return None
    if len(expr.args) != 2 or expr.kwargs:
        return None
    module_expr = expr.args[0]
    name_expr = expr.args[1]
    if not isinstance(module_expr, StrLit) or not isinstance(name_expr, StrLit):
        return None
    return module_expr.value, name_expr.value


def _expr_is_attr(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Attr) or kind == "Attr" or (
        _expr_has_attr(expr, "obj") and _expr_has_attr(expr, "name")
    )


def _expr_is_subscript(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Subscript) or kind == "Subscript" or (
        _expr_has_attr(expr, "obj") and _expr_has_attr(expr, "idx")
    )


def _expr_is_slice(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Slice) or kind == "Slice" or (
        _expr_has_attr(expr, "lo")
        and _expr_has_attr(expr, "hi")
        and _expr_has_attr(expr, "step")
    )


def _fold_str_literal_concat(expr: Expr):
    """``StrLit + StrLit`` -> one ``StrLit``, else ``None``.

    Deliberately narrow: both sides must already be string literals, so there
    is no user ``__add__`` to honour and no runtime type to discover. Chained
    literals fold left-to-right because the inner ``BinOp`` is folded first by
    the same check on the way down.
    """
    if getattr(expr, "op", None) != "+":
        return None
    lhs = getattr(expr, "lhs", None)
    rhs = getattr(expr, "rhs", None)
    if not isinstance(lhs, StrLit) or not isinstance(rhs, StrLit):
        return None
    # Keep the left literal's span and its already-inferred StrType: the folded
    # node stands where the expression started and its type cannot change.
    return StrLit(span=lhs.span, ty=lhs.ty, value=lhs.value + rhs.value)


def _expr_is_binop(expr: Expr, kind: str) -> bool:
    if isinstance(expr, BinOp) or kind == "BinOp":
        return True
    if not (
        _expr_has_attr(expr, "lhs")
        and _expr_has_attr(expr, "rhs")
        and _expr_has_attr(expr, "op")
    ):
        return False


def _dynamic_binop_needs_exact_int_operand(expr: BinOp, operand: Expr) -> bool:
    """Whether a dynamic object operation must consume an exact int object.

    The ordinary ``int`` projection may exceed i64 even when this particular
    nested expression is statically typed as ``IntType``.  Once its parent is
    a dynamic object operation, emitting the child to raw i64 and boxing it
    afterwards loses overflow before the runtime ever sees the value.  Raw
    ``pcc.i64``/``pcc.u64`` lanes deliberately keep their machine semantics.
    """
    if expr.op not in (
        "+", "-", "*", "/", "//", "%", "&", "|", "^", "<<", ">>"
    ):
        return False
    if not (
        isinstance(expr.ty, DynType)
        or isinstance(expr.lhs.ty, DynType)
        or isinstance(expr.rhs.ty, DynType)
    ):
        return False
    operand_ty = operand.ty
    if isinstance(operand_ty, BoolType):
        return True
    return isinstance(operand_ty, IntType) and operand_ty.name == "int"
    try:
        return expr.op not in (
            "==",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
            "is",
            "is not",
            "in",
            "not in",
        )
    except AttributeError:
        return False


def _expr_is_compare(expr: Expr, kind: str) -> bool:
    if isinstance(expr, Compare) or kind == "Compare":
        return True
    if not (
        _expr_has_attr(expr, "lhs")
        and _expr_has_attr(expr, "rhs")
        and _expr_has_attr(expr, "op")
    ):
        return False
    try:
        return expr.op in (
            "==",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
            "is",
            "is not",
            "in",
            "not in",
        )
    except AttributeError:
        return False


def _expr_is_bool(expr: Expr, kind: str) -> bool:
    return isinstance(expr, BoolExpr) or kind == "BoolExpr" or (
        _expr_has_attr(expr, "left")
        and _expr_has_attr(expr, "right")
        and _expr_has_attr(expr, "op")
    )


def _expr_is_unary(expr: Expr, kind: str) -> bool:
    return isinstance(expr, UnaryOp) or kind == "UnaryOp" or (
        _expr_has_attr(expr, "operand") and _expr_has_attr(expr, "op")
    )


def _expr_is_if(expr: Expr, kind: str) -> bool:
    return isinstance(expr, IfExpr) or kind == "IfExpr" or (
        _expr_has_attr(expr, "cond")
        and _expr_has_attr(expr, "then_e")
        and _expr_has_attr(expr, "else_e")
    )


def _expr_is_lambda(expr: Expr, kind: str) -> bool:
    return isinstance(expr, Lambda) or kind == "Lambda" or (
        _expr_has_attr(expr, "params") and _expr_has_attr(expr, "body")
    )


class ExprDispatchLoweringMixin:
    def _emit_dynamic_binary_dunder_call(
        self,
        lhs_expr: Expr,
        dunder_name: str,
        rhs_expr: Expr,
    ) -> ir.Value:
        recv_obj = self._emit_as_object(lhs_expr)
        rhs_obj = self._emit_as_object(rhs_expr)
        result = self.builder.call(
            self.runtime["py_obj_call_method1"],
            [recv_obj, self._attr_name_ptr(dunder_name), rhs_obj],
            name=self._fresh(f"dyn.dunder.{dunder_name}.call"),
        )
        self._emit_attribute_error_if_null(
            result,
            dunder_name,
            getattr(lhs_expr, "span", None),
        )
        return result

    def _emit_expr_impl(self, expr: Expr) -> ir.Value:
        if isinstance(expr, IntLit):
            if self._int_exprs_are_boxed():
                return self._emit_int_literal_object(int(expr.value))
            return ir.Constant(_I64, int(expr.value))
        if isinstance(expr, FloatLit):
            return ir.Constant(_DOUBLE, _as_native_float(expr.value))
        if isinstance(expr, ComplexLit):
            return self.builder.call(
                self.runtime["py_complex_new"],
                [
                    ir.Constant(_DOUBLE, _as_native_float(expr.real)),
                    ir.Constant(_DOUBLE, _as_native_float(expr.imag)),
                ],
                name=self._fresh("complex.lit"),
            )
        if isinstance(expr, BoolLit):
            return ir.Constant(_I1, 1 if bool(expr.value) else 0)
        if isinstance(expr, NoneLit):
            return self._emit_none_literal()
        if isinstance(expr, StrLit):
            return self._emit_str_literal(expr.value)
        if isinstance(expr, BytesLit):
            return self._emit_bytes_literal(expr.value)
        expr_kind = _expr_dispatch_kind_name(expr)
        if _expr_is_list(expr, expr_kind):
            return self._emit_list_literal(expr)
        if _expr_is_dict(expr, expr_kind):
            return self._emit_dict_literal(expr)
        if _expr_is_tuple(expr, expr_kind):
            return self._emit_tuple_literal(expr)
        if _expr_is_slice(expr, expr_kind):
            return self._emit_slice_object_expr(expr)
        if _expr_is_name(expr, expr_kind):
            return self._emit_name(expr)
        if _expr_is_subscript(expr, expr_kind):
            return self._emit_subscript_load(expr)
        if _expr_is_attr(expr, expr_kind):
            return self._emit_attr(expr)
        if _expr_is_binop(expr, expr_kind):
            # Fold ``"a" + "b"`` to one literal.  CPython's peephole does this,
            # so a loop containing a literal concatenation costs it nothing
            # while pcc allocated and concatenated on every iteration:
            # measured 7 ms on CPython against 95 ms under pcc1 for 300k
            # iterations of `"item" + "x"`, the widest gap of any operation in
            # the per-operation benchmark. Folding is only valid for two string
            # literals -- anything else can have a __add__ or a runtime type.
            folded = _fold_str_literal_concat(expr)
            if folded is not None:
                return self._emit_expr(folded)
            # Class-based arithmetic dunder fast path: ``a + b`` on a
            # hinted class with ``__add__`` dispatches there before
            # falling back to numeric coercion. Mirrors the compare
            # path in ``_emit_compare``.
            arith_dunder = {
                "+": "__add__",
                "-": "__sub__",
                "*": "__mul__",
                "/": "__truediv__",
                "//": "__floordiv__",
                "%": "__mod__",
                "**": "__pow__",
            }.get(expr.op)
            if arith_dunder is not None:
                dunder = self._try_dispatch_dunder_unary(
                    expr.lhs, arith_dunder, (expr.rhs,)
                )
                if dunder is not None:
                    reflected_dunder = {
                        "+": "__radd__",
                        "-": "__rsub__",
                        "*": "__rmul__",
                        "/": "__rtruediv__",
                        "//": "__rfloordiv__",
                        "%": "__rmod__",
                        "**": "__rpow__",
                    }.get(expr.op)
                    if reflected_dunder is not None and isinstance(
                        dunder.type, ir.PointerType
                    ):
                        parent_fn = self.current_function
                        notimpl_gv = declare_runtime_global(
                            self.module, "py_NotImplemented"
                        )
                        notimpl = self.builder.load(
                            notimpl_gv, name=self._fresh("notimplemented")
                        )
                        is_notimpl = self.builder.icmp_unsigned(
                            "==",
                            dunder,
                            notimpl,
                            name=self._fresh("notimplemented.cmp"),
                        )
                        reflected_block = parent_fn.append_basic_block(
                            name=self._fresh("binop.reflected")
                        )
                        done_block = parent_fn.append_basic_block(
                            name=self._fresh("binop.done")
                        )
                        direct_block = self.builder.block
                        self.builder.cbranch(
                            is_notimpl, reflected_block, done_block
                        )
                        self.builder.position_at_end(reflected_block)
                        reflected = self._try_dispatch_dunder_unary(
                            expr.rhs, reflected_dunder, (expr.lhs,)
                        )
                        if reflected is None or reflected.type != dunder.type:
                            reflected = dunder
                        self.builder.branch(done_block)
                        reflected_incoming = self.builder.block
                        self.builder.position_at_end(done_block)
                        phi = self.builder.phi(
                            dunder.type, name=self._fresh("binop.dunder")
                        )
                        phi.add_incoming(dunder, direct_block)
                        phi.add_incoming(reflected, reflected_incoming)
                        return phi
                    return dunder
                if _is_class_type_for_expr_dispatch(expr.lhs.ty):
                    return self._emit_dynamic_binary_dunder_call(
                        expr.lhs,
                        arith_dunder,
                        expr.rhs,
                    )
                reflected_dunder = {
                    "+": "__radd__",
                    "-": "__rsub__",
                    "*": "__rmul__",
                    "/": "__rtruediv__",
                    "//": "__rfloordiv__",
                    "%": "__rmod__",
                    "**": "__rpow__",
                }.get(expr.op)
                if (
                    reflected_dunder is not None
                    and _is_class_type_for_expr_dispatch(expr.rhs.ty)
                    and not (
                        expr.op == "%"
                        and self._is_valueclass_payload_type(expr.rhs.ty)
                    )
                ):
                    # str %% valueclass falls through to the str-mod branch
                    # below with a projected operand; valueclasses define no
                    # user __rmod__, and the reflected receiver path would
                    # materialize an identity instance.
                    return self._emit_dynamic_binary_dunder_call(
                        expr.rhs,
                        reflected_dunder,
                        expr.lhs,
                    )
                # ``/`` on a DynType operand (e.g. ``obj.attr / n``) is handled
                # generically by py_obj_truediv in _emit_binop_value below: a
                # DynType may box a number at runtime, so it must not route to
                # the __truediv__ dunder (a tagged int has no such attribute).
            if (
                expr.op == "**"
                and isinstance(expr.lhs, IntLit)
                and isinstance(expr.rhs, IntLit)
                and expr.rhs.value >= 0
            ):
                folded = pow(expr.lhs.value, expr.rhs.value)
                if -(1 << 63) <= folded <= (1 << 63) - 1:
                    return ir.Constant(_I64, folded)

            dict_keys_result = self._maybe_emit_dict_keys_view_binop(expr)
            if dict_keys_result is not None:
                return dict_keys_result

            lhs_as_exact_object = _dynamic_binop_needs_exact_int_operand(
                expr, expr.lhs
            )
            rhs_as_exact_object = _dynamic_binop_needs_exact_int_operand(
                expr, expr.rhs
            )
            lhs = (
                self._emit_as_object(expr.lhs)
                if lhs_as_exact_object
                else self._emit_expr(expr.lhs)
            )
            lhs_cpy_owned = False
            if lhs in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(lhs)
                lhs_cpy_owned = self._cpy_value_is_owned(lhs)
            lhs_release_owned = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.lhs)
            )
            lhs_pin = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
            )
            # A GC-quiet rhs (literal, bound name, slot read) cannot allocate,
            # raise or move anything, so the lhs needs no pin across its
            # evaluation.  An inline tagged-int route pins its operands only
            # inside the slow block; the caller then defers every pin to it.
            # Measured on ``for x in xs: total += x``: the unconditional
            # lhs/rhs/result pin+unpin were 24% of the loop's samples.
            rhs_quiet = self._exact_int_operand_is_gc_quiet(expr.rhs)
            defer_pins = (
                not lhs_cpy_owned
                and self._binop_route_defers_pins(
                    expr.op, expr.lhs.ty, expr.rhs.ty, expr.ty
                )
            )
            lhs_pinned_early = lhs_pin and not rhs_quiet and not defer_pins
            if lhs_pinned_early:
                # The RHS is evaluated before binary dispatch.  Pin even a
                # borrowed native lhs: a moving collector updates its rooted
                # slot, not this already-loaded SSA pointer.  Cleanup releases
                # only fresh values; borrowed pointers are merely unpinned.
                self._gc_pin(lhs)
            lhs_pinned_cleanup = (
                ((lhs, lhs_release_owned),) if lhs_pinned_early else ()
            )
            if expr.op == "%" and self._is_valueclass_payload_type(expr.rhs.ty):
                # direct valueclass constructors in %-format operands project
                # to boxed valueboxes, not identity instances
                rhs = self._emit_expr_as_pcc_object(expr.rhs)
            else:
                if lhs_cpy_owned or lhs_pinned_early:
                    rhs = self._emit_expr_with_cpy_operand_cleanup(
                        expr.rhs,
                        (lhs,) if lhs_cpy_owned else (),
                        (),
                        lhs_pinned_cleanup,
                        as_object=rhs_as_exact_object,
                    )
                else:
                    rhs = (
                        self._emit_as_object(expr.rhs)
                        if rhs_as_exact_object
                        else self._emit_expr(expr.rhs)
                    )
            if rhs in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(
                    rhs,
                    (lhs,) if lhs_cpy_owned else (),
                    (),
                    lhs_pinned_cleanup,
                )
            rhs_release_owned = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.rhs)
            )
            rhs_pin = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
            )
            if defer_pins and rhs in getattr(self, "_cpy_values", ()):
                defer_pins = False
            slow_pins: tuple[ir.Value, ...] = ()
            if defer_pins:
                if lhs_pin:
                    slow_pins = slow_pins + (lhs,)
                if rhs_pin:
                    slow_pins = slow_pins + (rhs,)
            else:
                if lhs_pin and not lhs_pinned_early:
                    self._gc_pin(lhs)
                if rhs_pin:
                    self._gc_pin(rhs)
            pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = ()
            if lhs_pin:
                pinned_pcc_on_error = pinned_pcc_on_error + ((lhs, lhs_release_owned),)
            if rhs_pin:
                pinned_pcc_on_error = pinned_pcc_on_error + (
                    (rhs, rhs_release_owned),
                )
            result = self._emit_binop_value(
                expr.op,
                lhs,
                expr.lhs.ty,
                rhs,
                expr.rhs.ty,
                result_ty=expr.ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )
            # The result pin guards the fresh result only while an owned
            # operand is released below (a release may run a finalizer or a
            # collection); without a release there is nothing to guard.
            result_pin = False
            if (
                (lhs_release_owned or rhs_release_owned)
                and isinstance(result.type, ir.PointerType)
                and result not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr)
            ):
                self._gc_pin(result)
                result_pin = True
            if lhs_pin and not defer_pins:
                self._gc_unpin(lhs)
            if lhs_release_owned:
                self._gc_release(lhs)
            elif not lhs_pin:
                self._gc_release_if_owned(lhs, expr.lhs)
            if rhs_pin and not defer_pins:
                self._gc_unpin(rhs)
            if rhs_release_owned:
                self._gc_release(rhs)
            elif not rhs_pin:
                self._gc_release_if_owned(rhs, expr.rhs)
            if result_pin:
                self._gc_unpin(result)
            return result
        if _expr_is_unary(expr, expr_kind):
            return self._emit_unary(expr)
        if _expr_is_compare(expr, expr_kind):
            return self._emit_compare(expr)
        if _expr_is_bool(expr, expr_kind):
            return self._emit_boolexpr(expr)
        if _expr_is_call(expr, expr_kind):
            native_default_func = _expr_native_default_func_ref(expr)
            if native_default_func is not None:
                return self._emit_native_default_func_ref(
                    native_default_func[0],
                    native_default_func[1],
                )
            native_default_global = _expr_native_default_global_ref(expr)
            if native_default_global is not None:
                resolved = self._emit_native_default_global_ref(
                    native_default_global[0],
                    native_default_global[1],
                    expr.span,
                )
                if resolved is not None:
                    return resolved
                # Not resolvable through the exports table — fall back to
                # the pre-sentinel behavior (emit the bare Name in the
                # caller's context).
                return self._emit_expr(
                    Name(
                        span=expr.span,
                        ty=expr.ty,
                        ident=native_default_global[1],
                    )
                )
            return self._emit_call(expr)
        if _expr_is_if(expr, expr_kind):
            return self._emit_if_expr(expr)
        # Simple lambda -> CPython ``operator`` callable. Covers the
        # common ``sorted(xs, key=lambda x: x.attr)`` and
        # ``sorted(xs, key=lambda x: x[i])`` idioms that dominate
        # pcc's own source (method / subscript getters used as sort
        # keys).
        #
        # The native function object is tried FIRST when native callables are
        # preferred (``libpython_mode="off"``).  Otherwise the operator
        # shortcut claims the shape and imports CPython's ``operator`` module:
        # ``pcc/passes/base.py`` builds a module-scope dict of ten
        # ``lambda pm: pm.add_<x>_pass()`` values, which became ten
        # ``py_cpy_import``/``getattr``/``call1`` sequences in that module's
        # top-level code -- and module-level code is outside the strict
        # no-libpython stub projection, so it failed the whole compile.
        if _expr_is_lambda(expr, expr_kind):
            prefer_native = bool(
                getattr(self, "_prefer_native_callable_values", False)
            )
            if prefer_native:
                native = self._maybe_emit_native_lambda_func(expr)
                if native is not None:
                    return native
            simple = self._maybe_emit_simple_lambda(expr)
            if simple is not None:
                return simple
            if not prefer_native:
                native = self._maybe_emit_native_lambda_func(expr)
                if native is not None:
                    return native
            # Fall back to the general lambda-wrap path: hoist the
            # lambda body into a dedicated pcc FuncDef and wrap the
            # function pointer as a CPython PyCFunction via
            # ``py_cpy_wrap_pcc_1arg``.
            wrapped = self._maybe_emit_lambda_wrap(expr)
            if wrapped is not None:
                return wrapped
        raise NotImplementedError(
            f"Layer 1 does not handle expression {type(expr).__name__}"
        )
