"""Comparison and membership lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolExpr,
    BoolLit,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    Compare,
    ComplexType,
    DictType,
    DynType,
    Expr,
    FloatLit,
    FloatType,
    IntLit,
    IntType,
    ListType,
    MemoryViewType,
    Name,
    NoneLit,
    NoneType,
    StrType,
    TupleExpr,
    TupleType,
    Type,
)
from . import marshal
from .freestanding_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_DICT,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = ir.IntType(8).as_pointer()


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


_BUILTIN_TYPE_TAGS = {
    "bool": PY_TYPE_BOOL,
    "int": PY_TYPE_INT,
    "float": PY_TYPE_FLOAT,
    "str": PY_TYPE_STR,
    "list": PY_TYPE_LIST,
    "dict": PY_TYPE_DICT,
    "tuple": PY_TYPE_TUPLE,
    "set": PY_TYPE_SET,
    "bytes": PY_TYPE_BYTES,
    "bytearray": PY_TYPE_BYTEARRAY,
}


class CompareMembershipLoweringMixin:
    def _emit_runtime_object_compare(
        self,
        expr: Compare,
        lhs_obj: ir.Value,
        rhs_obj: ir.Value,
        name_prefix: str,
    ) -> ir.Value:
        """Emit one runtime object-comparison contract.

        Object-vs-object comparisons and DynType ordering used to duplicate
        runtime-symbol selection, the raising-call edge, and bool
        normalization.  Operands are projected by the caller so evaluation
        order and valueclass/CPython boundary policy stay outside this owner.
        """
        runtime_name = {
            "==": "py_obj_eq_value",
            "!=": "py_obj_eq_value",
            "<": "py_obj_lt",
            "<=": "py_obj_le",
            ">": "py_obj_gt",
            ">=": "py_obj_ge",
        }.get(expr.op)
        if runtime_name is None:
            raise NotImplementedError(
                f"Layer 2 does not handle object compare op {expr.op!r}"
            )
        compared = self.builder.call(
            self.runtime[runtime_name],
            [lhs_obj, rhs_obj],
            name=self._fresh(name_prefix + ".cmp"),
        )
        self._emit_post_call_err_check(self._expr_span_or_none(expr))
        result = self.builder.icmp_signed(
            "!=",
            compared,
            ir.Constant(compared.type, 0),
            name=self._fresh(name_prefix + ".cmp.i1"),
        )
        if expr.op == "!=":
            return self.builder.not_(
                result,
                name=self._fresh(name_prefix + ".ne"),
            )
        return result

    def _emit_compare(self, expr: Compare) -> ir.Value:
        builtin_type_cmp = self._emit_builtin_type_name_compare(expr)
        if builtin_type_cmp is not None:
            return builtin_type_cmp

        # Identity against None: pointer compare against @py_None.
        if expr.op in ("is", "is not"):
            return self._emit_identity_compare(expr)
        if expr.op in ("in", "not in"):
            return self._emit_membership(expr)

        if (
            getattr(self, "_freestanding_module", False)
            and expr.op in ("==", "!=", "<", "<=", ">", ">=")
            and any(
                isinstance(operand.ty, IntType)
                and operand.ty.name == "int"
                and not (
                    isinstance(operand, IntLit)
                    and -(1 << 63) <= operand.value <= (1 << 63) - 1
                )
                for operand in (expr.lhs, expr.rhs)
            )
        ):
            raise RuntimeError(
                "freestanding ordinary Python int comparison cannot preserve "
                "arbitrary precision; annotate the machine boundary with "
                "pcc.i64 or pcc.u64"
            )

        # Complex ordering is a hard TypeError in CPython. ``==``/``!=`` on a
        # complex operand are valid (route to the equality paths below); only
        # the relational operators ``<``/``<=``/``>``/``>=`` must raise. Guard
        # here so a complex operand never falls through to the numeric
        # fast paths (``_to_double`` has no complex case; ``_to_int64`` would
        # misread the boxed pointer and yield a garbage bool).
        complex_order = self._emit_complex_ordering_typeerror(expr)
        if complex_order is not None:
            return complex_order

        # ``==``/``!=`` with a complex operand: py_obj_eq has no complex
        # case, so two equal-valued complex boxes fell through to its
        # identity-only default and compared unequal. Compare the
        # (real, imag) component pairs instead.
        complex_eq = self._emit_complex_value_equality(expr)
        if complex_eq is not None:
            return complex_eq

        # Exact pointer-form ints need the source-aware ownership contract
        # even when ordinary locals use the boxed-int ABI.  Check this before
        # the generic boxed branch so fresh bignum operands are pinned across
        # RHS evaluation and released after comparison; borrowed exact locals
        # remain borrowed.
        exact_int_cmp = self._emit_exact_int_compare(expr)
        if exact_int_cmp is not None:
            return exact_int_cmp

        if (
            self._int_exprs_are_boxed()
            and expr.op in ("==", "!=", "<", "<=", ">", ">=")
            and isinstance(expr.lhs.ty, (IntType, BoolType))
            and isinstance(expr.rhs.ty, (IntType, BoolType))
        ):
            lhs = self._emit_expr(expr.lhs)
            lhs_owned = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.lhs)
            )
            lhs_pinned = (
                isinstance(lhs.type, ir.PointerType)
                and lhs not in getattr(self, "_cpy_values", ())
            )
            lhs_cleanup = ()
            if lhs_pinned:
                self._gc_pin(lhs)
                lhs_cleanup = ((lhs, lhs_owned),)
            rhs = self._emit_expr_with_cpy_operand_cleanup(
                expr.rhs,
                (),
                (),
                lhs_cleanup,
            )
            rhs_owned = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
                and self._pcc_pointer_source_is_owned(expr.rhs)
            )
            rhs_pinned = (
                isinstance(rhs.type, ir.PointerType)
                and rhs not in getattr(self, "_cpy_values", ())
            )
            if rhs_pinned:
                self._gc_pin(rhs)
            lhs_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                lhs,
                expr.lhs.ty,
            )
            rhs_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                expr.rhs.ty,
            )
            cmp_i32 = self.builder.call(
                self.runtime["py_int_cmp"],
                [lhs_obj, rhs_obj],
                name=self._fresh("int.obj.cmp"),
            )
            result = self.builder.icmp_signed(
                expr.op,
                cmp_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("int.obj.cmp.i1"),
            )
            if lhs_pinned:
                self._gc_unpin(lhs)
            if lhs_owned:
                self._gc_release(lhs)
            if rhs_pinned:
                self._gc_unpin(rhs)
            if rhs_owned:
                self._gc_release(rhs)
            return result

        valueclass_eq = self._emit_valueclass_payload_eq(expr)
        if valueclass_eq is not None:
            return valueclass_eq

        if expr.op in ("==", "!=") and (
            self._is_valueclass_payload_type(expr.lhs.ty)
            or self._is_valueclass_payload_type(expr.rhs.ty)
        ):
            # Mixed valueclass-vs-anything equality: project both operands
            # (direct constructors become boxed valueboxes, scalars box as
            # usual) and delegate to runtime value equality. Same-class
            # pairs were handled by the payload fast path above.
            lhs_obj = self._emit_expr_as_pcc_object(expr.lhs)
            rhs_obj = self._emit_expr_as_pcc_object(expr.rhs)
            eq = self.builder.call(
                self.runtime["py_obj_eq_value"],
                [lhs_obj, rhs_obj],
                name=self._fresh("value.mixed.eq"),
            )
            eq_i1 = self.builder.icmp_signed(
                "!=",
                eq,
                ir.Constant(_I32, 0),
                name=self._fresh("value.mixed.eq.i1"),
            )
            if expr.op == "!=":
                return self.builder.not_(
                    eq_i1,
                    name=self._fresh("value.mixed.ne"),
                )
            return eq_i1

        # Class-based comparison dunder fast path.
        cmp_dunder = {
            "==": "__eq__",
            "!=": "__ne__",
            "<": "__lt__",
            "<=": "__le__",
            ">": "__gt__",
            ">=": "__ge__",
        }.get(expr.op)
        if cmp_dunder is not None:
            dunder = self._try_dispatch_dunder_unary(expr.lhs, cmp_dunder, (expr.rhs,))
            if dunder is not None:
                if self._ir_type_matches(dunder.type, _I1):
                    return dunder
                if isinstance(dunder.type, ir.IntType) and dunder.type.width > 1:
                    return self.builder.icmp_signed(
                        "!=",
                        dunder,
                        ir.Constant(dunder.type, 0),
                        name=self._fresh("dunder.i1"),
                    )
                if isinstance(dunder.type, ir.PointerType):
                    # Returned PyObject*: run py_obj_truthy to get i1.
                    as_i32 = self.builder.call(
                        self.runtime["py_obj_truthy"],
                        [dunder],
                        name=self._fresh("dunder.truthy"),
                    )
                    return self.builder.trunc(
                        as_i32,
                        _I1,
                        name=self._fresh("dunder.truthy.i1"),
                    )
                return dunder

        lhs_ty = expr.lhs.ty
        rhs_ty = expr.rhs.ty
        lhs_looks_cpy = self._expr_looks_cpython(expr.lhs)
        rhs_looks_cpy = self._expr_looks_cpython(expr.rhs)

        if lhs_looks_cpy or rhs_looks_cpy:
            recv_expr = expr.lhs
            other_expr = expr.rhs
            recv_op = expr.op
            if not lhs_looks_cpy and rhs_looks_cpy:
                recv_expr = expr.rhs
                other_expr = expr.lhs
                recv_op = {
                    "==": "==",
                    "!=": "!=",
                    "<": ">",
                    "<=": ">=",
                    ">": "<",
                    ">=": "<=",
                }.get(expr.op, expr.op)
            method_name = {
                "==": "__eq__",
                "!=": "__ne__",
                "<": "__lt__",
                "<=": "__le__",
                ">": "__gt__",
                ">=": "__ge__",
            }.get(recv_op)
            if method_name is not None:
                if not lhs_looks_cpy and rhs_looks_cpy:
                    # Reflected dispatch uses the RHS dunder, but operand
                    # evaluation remains left-to-right.  Materialize the LHS
                    # first and carry its owned CPython box through RHS
                    # evaluation and method lookup cleanup.
                    other_val = self._emit_expr(other_expr)
                    other_cpy, other_owned = (
                        self._marshal_to_cpython_consuming_source(
                        other_val,
                        other_expr.ty,
                        other_expr,
                        )
                    )
                    self._guard_cpy_value_not_null(other_cpy)
                    live_owned = (other_cpy,) if other_owned else ()
                    recv_val = self._emit_expr_with_cpy_operand_cleanup(
                        recv_expr,
                        live_owned,
                    )
                    self._guard_cpy_value_not_null(recv_val, live_owned)
                    recv_cpy, recv_owned = (
                        self._marshal_to_cpython_consuming_source(
                        recv_val,
                        recv_expr.ty,
                        recv_expr,
                        live_owned,
                        )
                    )
                    self._guard_cpy_value_not_null(recv_cpy, live_owned)
                    result = self._emit_cpy_method_call1_value(
                        recv_cpy,
                        method_name,
                        other_cpy,
                        arg_owned=other_owned,
                        receiver_owned=recv_owned,
                    )
                else:
                    recv_val = self._emit_expr(recv_expr)
                    self._guard_cpy_value_not_null(recv_val)
                    recv_cpy, recv_owned = (
                        self._marshal_to_cpython_consuming_source(
                        recv_val,
                        recv_expr.ty,
                        recv_expr,
                        )
                    )
                    self._guard_cpy_value_not_null(recv_cpy)
                    receiver_live = (recv_cpy,) if recv_owned else ()
                    other_val = self._emit_expr_with_cpy_operand_cleanup(
                        other_expr,
                        receiver_live,
                    )
                    if other_val in getattr(self, "_cpy_values", ()):
                        self._guard_cpy_value_not_null(
                            other_val,
                            receiver_live,
                        )
                    other_cpy, other_owned = (
                        self._marshal_to_cpython_consuming_source(
                        other_val,
                        other_expr.ty,
                        other_expr,
                        receiver_live,
                        )
                    )
                    self._guard_cpy_value_not_null(
                        other_cpy,
                        receiver_live,
                    )
                    result = self._emit_cpy_method_call1_value(
                        recv_cpy,
                        method_name,
                        other_cpy,
                        arg_owned=other_owned,
                        receiver_owned=recv_owned,
                    )
                self._guard_cpy_value_not_null(result)
                as_i32 = self.builder.call(
                    self.runtime["py_cpy_truthy"],
                    [result],
                    name=self._fresh("cpy.cmp.i32"),
                )
                self._guard_cpy_status_not_negative(as_i32, (result,))
                self.builder.call(self.runtime["py_cpy_decref"], [result])
                self._forget_owned_cpy_value(result)
                return self.builder.icmp_signed(
                    "!=",
                    as_i32,
                    ir.Constant(_I32, 0),
                    name=self._fresh("cpy.cmp.i1"),
                )

        if expr.op in ("==", "!="):
            lhs_scalar = isinstance(lhs_ty, (IntType, BoolType, FloatType))
            rhs_scalar = isinstance(rhs_ty, (IntType, BoolType, FloatType))
            if (isinstance(lhs_ty, DynType) and rhs_scalar) or (
                isinstance(rhs_ty, DynType) and lhs_scalar
            ):
                lhs = self._emit_expr(expr.lhs)
                rhs = self._emit_expr(expr.rhs)
                lhs_dyn_obj = isinstance(lhs_ty, DynType) and isinstance(
                    lhs.type,
                    ir.PointerType,
                )
                rhs_dyn_obj = isinstance(rhs_ty, DynType) and isinstance(
                    rhs.type,
                    ir.PointerType,
                )
                if lhs_dyn_obj or rhs_dyn_obj:
                    lhs_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        lhs,
                        lhs_ty,
                    )
                    rhs_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        rhs,
                        rhs_ty,
                    )
                    eq = self.builder.call(
                        self.runtime["py_obj_eq_value"],
                        [lhs_obj, rhs_obj],
                        name=self._fresh("obj.scalar.eq"),
                    )
                    eq_i1 = self.builder.icmp_signed(
                        "!=",
                        eq,
                        ir.Constant(_I32, 0),
                        name=self._fresh("obj.scalar.eq.i1"),
                    )
                    if expr.op == "!=":
                        return self.builder.not_(
                            eq_i1,
                            name=self._fresh("obj.scalar.ne"),
                        )
                    return eq_i1
                if isinstance(lhs_ty, FloatType) or isinstance(rhs_ty, FloatType):
                    lf = self._to_double(lhs, lhs_ty)
                    rf = self._to_double(rhs, rhs_ty)
                    if expr.op == "!=":
                        return self.builder.fcmp_unordered(
                            "!=", lf, rf, name=self._fresh("dyn.scalar.fcmp")
                        )
                    return self.builder.fcmp_ordered(
                        expr.op,
                        lf,
                        rf,
                        name=self._fresh("dyn.scalar.fcmp"),
                    )
                lv = self._to_int64(lhs, lhs_ty)
                rv = self._to_int64(rhs, rhs_ty)
                return self.builder.icmp_signed(
                    expr.op,
                    lv,
                    rv,
                    name=self._fresh("dyn.scalar.icmp"),
                )

        dyn_str_eq = self._emit_dyn_str_equality(expr)
        if dyn_str_eq is not None:
            return dyn_str_eq

        # String equality → runtime py_str_eq fast path. Relational str
        # ops fall through to the generic object compare helpers.
        if (
            isinstance(lhs_ty, StrType)
            and isinstance(rhs_ty, StrType)
            and expr.op in ("==", "!=")
        ):
            return self._emit_owned_string_predicate(expr, False)

        if expr.op in ("==", "!=") and (
            isinstance(lhs_ty, StrType) or isinstance(rhs_ty, StrType)
        ):
            lhs = self._emit_expr(expr.lhs)
            rhs = self._emit_expr(expr.rhs)
            if not isinstance(lhs.type, ir.PointerType):
                lhs = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    lhs,
                    lhs_ty,
                )
            if not isinstance(rhs.type, ir.PointerType):
                rhs = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    rhs,
                    rhs_ty,
                )
            eq = self.builder.call(
                self.runtime["py_obj_eq_value"],
                [lhs, rhs],
                name=self._fresh("obj.str.eq"),
            )
            eq_i1 = self.builder.icmp_signed(
                "!=",
                eq,
                ir.Constant(_I32, 0),
                name=self._fresh("obj.str.eq.i1"),
            )
            if expr.op == "!=":
                return self.builder.not_(
                    eq_i1,
                    name=self._fresh("obj.str.ne"),
                )
            return eq_i1

        # Object-vs-object equality (for two boxed operands): delegate.
        if self._is_object(lhs_ty) and self._is_object(rhs_ty):
            # direct valueclass constructors must project to boxed
            # valueboxes here (value equality), not identity instances
            return self._emit_owned_string_predicate(expr, False, object_compare=True)

        lhs = self._emit_expr(expr.lhs)
        rhs = self._emit_expr(expr.rhs)
        if isinstance(lhs_ty, FloatType) or isinstance(rhs_ty, FloatType):
            lf = self._to_double(lhs, lhs_ty)
            rf = self._to_double(rhs, rhs_ty)
            if expr.op == "!=":
                return self.builder.fcmp_unordered("!=", lf, rf, name=self._fresh("fcmp"))
            return self.builder.fcmp_ordered(expr.op, lf, rf, name=self._fresh("fcmp"))
        if (isinstance(lhs_ty, DynType) or isinstance(rhs_ty, DynType)) and expr.op in (
            "<",
            "<=",
            ">",
            ">=",
        ):
            # A DynType operand may be a float at runtime; the int fast path
            # (_to_int64) would misread the boxed-float pointer. Route through
            # the runtime ordering compare (py_obj_lt/le/gt/ge ->
            # py_obj_cmp_threeway, which is float-aware). Mirrors the DynType
            # arithmetic dispatch in binary_op_lowering. Set operands are
            # DynType too, so ``set <= set`` reaches py_obj_le/lt/gt/ge, which
            # dispatch SET&&SET to py_set_issubset/issuperset (subset order).
            lo = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, lhs, lhs_ty
            )
            ro = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, rhs, rhs_ty
            )
            return self._emit_runtime_object_compare(expr, lo, ro, "dyn")
        lv = self._to_int64(lhs, lhs_ty)
        rv = self._to_int64(rhs, rhs_ty)
        if (
            getattr(lhs_ty, "name", "") == "pcc.u64"
            or getattr(rhs_ty, "name", "") == "pcc.u64"
        ):
            return self.builder.icmp_unsigned(
                expr.op,
                lv,
                rv,
                name=self._fresh("ucmp"),
            )
        return self.builder.icmp_signed(expr.op, lv, rv, name=self._fresh("icmp"))

    def _emit_owned_string_predicate(self, expr: Compare, contains: bool, object_compare: bool = False) -> ir.Value:
        """Consume transient strings and preserve the LHS through RHS effects."""
        lhs = self._emit_expr_as_pcc_object(expr.lhs) if object_compare else self._emit_expr(expr.lhs)
        if not self._owned_release_needed(lhs, expr.lhs):
            # A borrowed global/field may lose its original owner when the
            # RHS runs. A pin prevents movement, but does not replace a ref.
            lhs = self._gc_retain(lhs, name=self._fresh("str.lhs.retain"))
        self._gc_pin(lhs)
        rhs = self._emit_expr_with_cpy_operand_cleanup(
            expr.rhs, (), pinned_pcc=((lhs, True),), as_pcc_object=object_compare,
        )
        rhs_owned = self._owned_release_needed(rhs, expr.rhs)
        if object_compare and not rhs_owned:
            rhs = self._gc_retain(rhs, name=self._fresh("str.rhs.retain"))
            rhs_owned = True
        if rhs_owned:
            self._gc_pin(rhs)
        operands = [rhs, lhs] if contains else [lhs, rhs]
        symbol = "py_str_contains" if contains else "py_str_eq"
        if object_compare:
            symbol = {
                "==": "py_obj_eq_value", "!=": "py_obj_eq_value",
                "<": "py_obj_lt", "<=": "py_obj_le",
                ">": "py_obj_gt", ">=": "py_obj_ge",
            }[expr.op]
        status = self.builder.call(
            self.runtime[symbol], operands, name=self._fresh("str.predicate"),
        )
        if contains or object_compare:
            cleanup = ((lhs, True),)
            if rhs_owned:
                cleanup = ((lhs, True), (rhs, True))
            self._emit_post_call_err_check(
                getattr(expr, "span", None), pinned_release_on_error=cleanup,
            )
        result = self.builder.icmp_signed(
            "!=", status, ir.Constant(status.type, 0), name=self._fresh("str.predicate.i1"),
        )
        self._gc_unpin(lhs)
        self._gc_release(lhs)
        if rhs_owned:
            self._gc_unpin(rhs)
            self._gc_release(rhs)
        if expr.op in ("!=", "not in"):
            result = self.builder.not_(result, name=self._fresh("str.predicate.not"))
        return result

    def _emit_dyn_str_equality(self, expr: Compare) -> Optional[ir.Value]:
        if expr.op not in ("==", "!="):
            return None
        lhs_ty = expr.lhs.ty
        rhs_ty = expr.rhs.ty
        if isinstance(lhs_ty, DynType) and isinstance(rhs_ty, StrType):
            dyn_expr = expr.lhs
            str_expr = expr.rhs
        elif isinstance(lhs_ty, StrType) and isinstance(rhs_ty, DynType):
            dyn_expr = expr.rhs
            str_expr = expr.lhs
        else:
            return None

        # Keep source evaluation order and both operand owners through
        # callbacks. The generic equality contract also permits a non-string
        # dynamic object to implement __eq__ against the string.
        return self._emit_owned_string_predicate(expr, False, object_compare=True)

    def _emit_complex_ordering_typeerror(self, expr: Compare) -> Optional[ir.Value]:
        """Raise ``TypeError`` for ``<``/``<=``/``>``/``>=`` on a complex operand.

        ``complex`` supports ``==``/``!=`` but no ordering. CPython:
        ``'<' not supported between instances of 'complex' and 'complex'``
        (the second operand name reflects its actual type). We emit the raise
        + branch to the active error target and return a dummy ``i1`` in a dead
        continuation so the consumer of the compare result still has an SSA
        value; it is unreachable at runtime.
        """
        if expr.op not in ("<", "<=", ">", ">="):
            return None
        lhs_ty = expr.lhs.ty
        rhs_ty = expr.rhs.ty
        if not isinstance(lhs_ty, ComplexType) and not isinstance(rhs_ty, ComplexType):
            return None
        lhs_name = getattr(lhs_ty, "name", None) or "complex"
        rhs_name = getattr(rhs_ty, "name", None) or "complex"
        message = (
            f"'{expr.op}' not supported between instances of "
            f"'{lhs_name}' and '{rhs_name}'"
        )
        self._emit_builtin_exception_and_branch(
            "TypeError",
            message,
            getattr(expr, "span", None),
            open_dead_continuation=True,
        )
        return ir.Constant(_I1, 0)

    def _emit_complex_value_equality(self, expr: Compare) -> Optional[ir.Value]:
        """``==``/``!=`` when an operand is statically complex.

        CPython compares complex numbers component-wise
        (``complex(1, 2) == complex(1, 2)`` is True). ``py_obj_eq`` has no
        PY_TYPE_COMPLEX case, so without this path two equal-valued boxes
        reached its identity-only default and compared unequal. A
        non-complex int/float/bool side coerces exactly as the runtime's
        py_complex_real/py_complex_imag helpers do (real=value, imag=0.0).
        Non-numeric other sides (str, list, ...) stay on the generic object
        path, which is already unequal-by-type.
        """
        if expr.op not in ("==", "!="):
            return None
        lhs_ty = expr.lhs.ty
        rhs_ty = expr.rhs.ty
        if not isinstance(lhs_ty, ComplexType) and not isinstance(rhs_ty, ComplexType):
            return None
        numeric = (ComplexType, IntType, FloatType, BoolType)
        if not isinstance(lhs_ty, numeric) or not isinstance(rhs_ty, numeric):
            return None
        lhs_obj = self._emit_as_object(expr.lhs)
        rhs_obj = self._emit_as_object(expr.rhs)
        lhs_re = self._emit_complex_component_f64(lhs_obj, "py_complex_real", "re.l")
        lhs_im = self._emit_complex_component_f64(lhs_obj, "py_complex_imag", "im.l")
        rhs_re = self._emit_complex_component_f64(rhs_obj, "py_complex_real", "re.r")
        rhs_im = self._emit_complex_component_f64(rhs_obj, "py_complex_imag", "im.r")
        re_eq = self.builder.fcmp_ordered(
            "==", lhs_re, rhs_re, name=self._fresh("complex.eq.re")
        )
        im_eq = self.builder.fcmp_ordered(
            "==", lhs_im, rhs_im, name=self._fresh("complex.eq.im")
        )
        eq = self.builder.and_(re_eq, im_eq, name=self._fresh("complex.eq"))
        if expr.op == "!=":
            return self.builder.not_(eq, name=self._fresh("complex.ne"))
        return eq

    def _emit_complex_component_f64(
        self, obj: ir.Value, helper: str, label: str
    ) -> ir.Value:
        """One (real or imag) component of ``obj`` as a raw double.

        ``py_complex_real``/``py_complex_imag`` return an owned boxed float
        (and coerce int/float/bool operands); unbox it and release the
        temporary box.
        """
        box = self.builder.call(
            self.runtime[helper],
            [obj],
            name=self._fresh(f"complex.{label}.box"),
        )
        part = self.builder.call(
            self.runtime["py_float_to_f64"],
            [box],
            name=self._fresh(f"complex.{label}"),
        )
        self.builder.call(self.runtime["py_decref"], [box])
        return part

    def _emit_valueclass_payload_eq(self, expr: Compare) -> Optional[ir.Value]:
        if expr.op not in ("==", "!="):
            return None
        lhs_ty = expr.lhs.ty
        rhs_ty = expr.rhs.ty
        if not isinstance(lhs_ty, ClassType) or not isinstance(rhs_ty, ClassType):
            return None
        if not self._is_valueclass_payload_type(lhs_ty):
            return None
        if not self._is_valueclass_payload_type(rhs_ty):
            return None
        if (lhs_ty.module, lhs_ty.name) != (rhs_ty.module, rhs_ty.name):
            return None
        if len(lhs_ty.fields) != len(rhs_ty.fields):
            return None

        lhs_payload = self._maybe_emit_valueclass_constructor_payload(
            lhs_ty,
            expr.lhs,
        )
        lhs = lhs_payload if lhs_payload is not None else self._emit_expr(expr.lhs)
        rhs_payload = self._maybe_emit_valueclass_constructor_payload(
            rhs_ty,
            expr.rhs,
        )
        rhs = rhs_payload if rhs_payload is not None else self._emit_expr(expr.rhs)
        if isinstance(lhs.type, ir.PointerType) or isinstance(rhs.type, ir.PointerType):
            lhs_obj = self._emit_value_as_pcc_object_or_bridge(
                lhs,
                lhs_ty,
                "value.eq.l.obj",
            )
            rhs_obj = self._emit_value_as_pcc_object_or_bridge(
                rhs,
                rhs_ty,
                "value.eq.r.obj",
            )
            eq = self.builder.call(
                self.runtime["py_obj_eq_value"],
                [lhs_obj, rhs_obj],
                name=self._fresh("value.eq.obj"),
            )
            eq_i1 = self.builder.icmp_signed(
                "!=",
                eq,
                ir.Constant(eq.type, 0),
                name=self._fresh("value.eq.obj.i1"),
            )
            if expr.op == "!=":
                return self.builder.not_(eq_i1, name=self._fresh("value.ne.obj"))
            return eq_i1
        acc = self._emit_valueclass_payload_fields_eq(lhs, rhs, lhs_ty)
        if expr.op == "!=":
            return self.builder.not_(acc, name=self._fresh("value.ne"))
        return acc

    def _emit_valueclass_payload_fields_eq(
        self,
        lhs: ir.Value,
        rhs: ir.Value,
        ty: ClassType,
    ) -> ir.Value:
        acc: Optional[ir.Value] = None
        for idx, (_field_name, field_ty) in enumerate(ty.fields):
            lhs_field = self.builder.extract_value(
                lhs,
                [idx],
                name=self._fresh("value.eq.l"),
            )
            rhs_field = self.builder.extract_value(
                rhs,
                [idx],
                name=self._fresh("value.eq.r"),
            )
            field_eq = self._emit_valueclass_payload_field_eq(
                lhs_field,
                rhs_field,
                field_ty,
            )
            if acc is None:
                acc = field_eq
            else:
                acc = self.builder.and_(
                    acc,
                    field_eq,
                    name=self._fresh("value.eq.and"),
                )
        if acc is None:
            acc = ir.Constant(_I1, 1)
        return acc

    def _emit_valueclass_payload_field_eq(
        self,
        lhs_field: ir.Value,
        rhs_field: ir.Value,
        field_ty: Type,
    ) -> ir.Value:
        if (
            isinstance(field_ty, ClassType)
            and bool(getattr(field_ty, "valueclass", False))
            and self._is_valueclass_payload_type(field_ty)
            and not isinstance(lhs_field.type, ir.PointerType)
        ):
            return self._emit_valueclass_payload_fields_eq(
                lhs_field,
                rhs_field,
                field_ty,
            )
        if isinstance(field_ty, FloatType):
            return self.builder.fcmp_ordered(
                "==",
                lhs_field,
                rhs_field,
                name=self._fresh("value.eq.fcmp"),
            )
        if isinstance(lhs_field.type, ir.PointerType):
            obj_eq = self.builder.call(
                self.runtime["py_obj_eq_value"],
                [lhs_field, rhs_field],
                name=self._fresh("value.eq.obj"),
            )
            return self.builder.icmp_signed(
                "!=",
                obj_eq,
                ir.Constant(obj_eq.type, 0),
                name=self._fresh("value.eq.obj.i1"),
            )
        return self.builder.icmp_signed(
            "==",
            lhs_field,
            rhs_field,
            name=self._fresh("value.eq.icmp"),
        )

    def _emit_builtin_type_name_compare(self, expr: Compare) -> Optional[ir.Value]:
        if expr.op not in ("==", "!=", "is", "is not"):
            return None

        def type_call_arg(src: Expr) -> Optional[Expr]:
            if (
                isinstance(src, Call)
                and isinstance(src.func, Name)
                and src.func.ident == "type"
                and len(src.args) == 1
                and not src.kwargs
            ):
                return src.args[0]
            return None

        def builtin_type_tag(src: Expr) -> Optional[int]:
            if isinstance(src, Name):
                return _BUILTIN_TYPE_TAGS.get(src.ident)
            return None

        obj_expr = type_call_arg(expr.lhs)
        tag = builtin_type_tag(expr.rhs)
        if obj_expr is None or tag is None:
            obj_expr = type_call_arg(expr.rhs)
            tag = builtin_type_tag(expr.lhs)
        if obj_expr is None or tag is None:
            return None

        obj = self._emit_expr_as_pcc_object(obj_expr)
        actual = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [obj],
            name=self._fresh("type.tag"),
        )
        eq = self.builder.icmp_signed(
            "==",
            actual,
            ir.Constant(_I64, tag),
            name=self._fresh("type.eq.builtin"),
        )
        if expr.op in ("!=", "is not"):
            return self.builder.not_(eq, name=self._fresh("type.ne.builtin"))
        return eq

    def _emit_identity_compare(self, expr: Compare) -> ir.Value:
        """``is`` / ``is not`` — pointer compare, typically against None.

        Both operands are marshalled to PyObject* and compared as
        pointers. Interning of small ints / bools is handled by the
        runtime (``py_int_from_i64`` returns the canonical global for
        small ints), so ``is`` behaves consistently with CPython on
        those.

        Fast path: if one operand is a NoneLit and the other is a native
        scalar (int/float/bool), the answer is a compile-time constant
        (False for ``is``, True for ``is not``).
        """
        lhs_module = self._native_module_name_for_object_expr(expr.lhs)
        rhs_module = self._native_module_name_for_object_expr(expr.rhs)
        if lhs_module is not None and rhs_module is not None:
            same = lhs_module == rhs_module
            if expr.op == "is not":
                same = not same
            return ir.Constant(_I1, 1 if same else 0)

        # Constant-fold ``<native> is None`` and ``<native> is not None``.
        native_lhs = self._is_native_scalar_type(expr.lhs.ty)
        native_rhs = self._is_native_scalar_type(expr.rhs.ty)
        none_lhs = isinstance(expr.lhs, NoneLit) or isinstance(expr.lhs.ty, NoneType)
        none_rhs = isinstance(expr.rhs, NoneLit) or isinstance(expr.rhs.ty, NoneType)
        if (native_lhs and none_rhs) or (native_rhs and none_lhs):
            # The native value can never be literally the py_None pointer.
            return ir.Constant(_I1, 1 if expr.op == "is not" else 0)

        def identity_temp_needs_release(src: Expr, raw: ir.Value) -> bool:
            if not isinstance(raw.type, ir.PointerType):
                return False
            if self._expr_returns_owned_object(src):
                return True
            return isinstance(src, (IntLit, FloatLit, BoolLit))

        lhs = self._emit_expr(expr.lhs)
        rhs = self._emit_expr(expr.rhs)
        lhs_obj = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, lhs, expr.lhs.ty
        )
        rhs_obj = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, rhs, expr.rhs.ty
        )
        # Compare pointers as integers so the IR is independent of the
        # llvmlite version's pointer-compare support.
        lhs_i = self.builder.ptrtoint(lhs_obj, _I64, name=self._fresh("is.l"))
        rhs_i = self.builder.ptrtoint(rhs_obj, _I64, name=self._fresh("is.r"))
        eq = self.builder.icmp_signed("==", lhs_i, rhs_i, name=self._fresh("is"))
        result = eq
        if expr.op == "is not":
            result = self.builder.not_(eq, name=self._fresh("is_not"))
        if identity_temp_needs_release(expr.lhs, lhs):
            self._gc_release(lhs_obj)
        if identity_temp_needs_release(expr.rhs, rhs):
            self._gc_release(rhs_obj)
        return result

    def _emit_membership(self, expr: Compare) -> ir.Value:
        """``in`` / ``not in`` over str / list / dict / set / tuple."""
        if self._is_os_environ_attr(expr.rhs):
            key = self._emit_membership_needle_object(
                expr.lhs,
                "os.environ.in.key",
            )
            contains_i32 = self.builder.call(
                self.runtime["py_os_environ_contains"],
                [key],
                name=self._fresh("os.environ.in"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            contains = self.builder.icmp_signed(
                "!=",
                contains_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("os.environ.in.i1"),
            )
            if expr.op == "not in":
                return self.builder.not_(
                    contains,
                    name=self._fresh("os.environ.notin"),
                )
            return contains
        if self._expr_looks_cpython(expr.rhs):
            # Python evaluates ``needle in container`` left-to-right even
            # though dispatch ultimately targets container.__contains__.
            # Materialize the needle first, carry its CPython ref through RHS
            # evaluation, then invoke the one-value method helper without
            # evaluating either source expression a second time.
            lhs_value = self._emit_expr(expr.lhs)
            lhs_cpy, lhs_owned = self._marshal_to_cpython_consuming_source(
                lhs_value,
                expr.lhs.ty,
                expr.lhs,
            )
            self._guard_cpy_value_not_null(lhs_cpy)
            lhs_live = (lhs_cpy,) if lhs_owned else ()
            rhs_value = self._emit_expr_with_cpy_operand_cleanup(
                expr.rhs,
                lhs_live,
            )
            if rhs_value in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(rhs_value, lhs_live)
            container_cpy, container_owned = (
                self._marshal_to_cpython_consuming_source(
                    rhs_value,
                    expr.rhs.ty,
                    expr.rhs,
                    lhs_live,
                )
            )
            self._guard_cpy_value_not_null(container_cpy, lhs_live)
            result = self._emit_cpy_method_call1_value(
                container_cpy,
                "__contains__",
                lhs_cpy,
                arg_owned=lhs_owned,
                receiver_owned=container_owned,
            )
            self._guard_cpy_value_not_null(result)
            as_i32 = self.builder.call(
                self.runtime["py_cpy_truthy"],
                [result],
                name=self._fresh("cpy.contains.i32"),
            )
            self._guard_cpy_status_not_negative(as_i32, (result,))
            self.builder.call(self.runtime["py_cpy_decref"], [result])
            self._forget_owned_cpy_value(result)
            contains = self.builder.icmp_signed(
                "!=",
                as_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("cpy.contains.i1"),
            )
            if expr.op == "not in":
                return self.builder.not_(
                    contains,
                    name=self._fresh("cpy.not_in"),
                )
            return contains
        container_ty = expr.rhs.ty
        if (
            isinstance(container_ty, StrType)
            and isinstance(expr.lhs.ty, StrType)
            and not self._expr_looks_cpython(expr.lhs)
        ):
            return self._emit_owned_string_predicate(expr, True)
        weak_dict_kind = self._weak_dict_kind_for_expr(expr.rhs)
        rhs = self._emit_expr(expr.rhs)
        if rhs in getattr(self, "_cpy_values", ()):
            container_cpy, container_owned = self._marshal_to_cpython(
                rhs,
                container_ty,
            )
            result = self._emit_cpy_method_call_src(
                container_cpy,
                "__contains__",
                (expr.lhs,),
                receiver_owned=container_owned,
            )
            self._guard_cpy_value_not_null(result)
            as_i32 = self.builder.call(
                self.runtime["py_cpy_truthy"],
                [result],
                name=self._fresh("cpy.contains.i32"),
            )
            self._guard_cpy_status_not_negative(as_i32, (result,))
            self.builder.call(self.runtime["py_cpy_decref"], [result])
            self._forget_owned_cpy_value(result)
            contains = self.builder.icmp_signed(
                "!=",
                as_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("cpy.contains.i1"),
            )
            if expr.op == "not in":
                return self.builder.not_(
                    contains,
                    name=self._fresh("cpy.not_in"),
                )
            return contains
        if weak_dict_kind == "value":
            key = self._emit_membership_needle_object(
                expr.lhs,
                "weak.value.dict.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_weak_value_dict_contains"],
                [rhs, key],
                name=self._fresh("weak.value.dict.in"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            result = self.builder.icmp_signed(
                "!=",
                res_i32,
                ir.Constant(_I64, 0),
                name=self._fresh("weak.value.dict.in.i1"),
            )
            if expr.op == "not in":
                return self.builder.not_(
                    result,
                    name=self._fresh("weak.value.dict.notin"),
                )
            return result

        if isinstance(container_ty, StrType):
            # Needle is expected to be a pcc str (single char or
            # substring). When the lhs type is DynType (e.g. a
            # comprehension loop variable bound by ``for ch in s``
            # where the comp-scope inference didn't propagate the
            # element type), we still have a ``PyObject*`` — py_str_*
            # helpers tolerate foreign types by length/bytes compare.
            lhs = self._emit_expr(expr.lhs)
            lhs_ty = expr.lhs.ty
            needle = lhs
            if not isinstance(lhs.type, ir.PointerType):
                needle = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, lhs, lhs_ty
                )
            res_i32 = self.builder.call(
                self.runtime["py_str_contains"],
                [rhs, needle],
                name=self._fresh("str.in"),
            )
        elif isinstance(container_ty, ListType):
            needle = self._emit_membership_needle_object(
                expr.lhs,
                "cpy.list.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_list_contains"],
                [rhs, needle],
                name=self._fresh("list.in"),
            )
        elif isinstance(container_ty, DictType):
            key = self._emit_membership_needle_object(
                expr.lhs,
                "cpy.dict.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_dict_contains"],
                [rhs, key],
                name=self._fresh("dict.in"),
            )
        elif isinstance(container_ty, TupleType):
            # Tuple literal fast path: unroll against static elements.
            # General tuple values can use the runtime's generic
            # ``py_obj_contains`` dispatcher, which already handles
            # tuple containers via linear scan.
            if isinstance(expr.rhs, TupleExpr):
                return self._emit_membership_tuple_literal(
                    expr.lhs,
                    expr.rhs,
                    negate=(expr.op == "not in"),
                )
            key = self._emit_membership_needle_object(
                expr.lhs,
                "cpy.tuple.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_obj_contains"],
                [rhs, key],
                name=self._fresh("tuple.in"),
            )
        elif isinstance(container_ty, DynType) or isinstance(container_ty, Type):
            # DynType / imprecise Type container — route through the runtime
            # ``py_obj_contains`` dispatcher. This covers self-host paths
            # where Optional[dict] or getattr(...) inference collapses to the
            # abstract Type base; known concrete str/list/dict/tuple cases
            # above still keep their specialized fast paths.
            key = self._emit_membership_needle_object(
                expr.lhs,
                "cpy.obj.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_obj_contains"],
                [rhs, key],
                name=self._fresh("obj.in"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            result = self.builder.icmp_signed(
                "!=",
                res_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("obj.in.i1"),
            )
            if expr.op == "not in":
                result = self.builder.not_(
                    result,
                    name=self._fresh("obj.notin"),
                )
            return result
        else:
            key = self._emit_membership_needle_object(
                expr.lhs,
                "cpy.obj.in.key",
            )
            res_i32 = self.builder.call(
                self.runtime["py_obj_contains"],
                [rhs, key],
                name=self._fresh("obj.in"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            result = self.builder.icmp_signed(
                "!=",
                res_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("obj.in.i1"),
            )
            if expr.op == "not in":
                result = self.builder.not_(
                    result,
                    name=self._fresh("obj.notin"),
                )
            return result

        # The specialized contains helpers use -1/pending-error for failures.
        # Check the exception channel before interpreting the status as a
        # boolean; ``-1 != 0`` is not a successful membership result.
        self._emit_post_call_err_check(getattr(expr, "span", None))
        res = self.builder.icmp_signed(
            "!=", res_i32, ir.Constant(_I32, 0), name=self._fresh("in.i1")
        )
        if expr.op == "not in":
            return self.builder.not_(res, name=self._fresh("not_in"))
        return res

    def _emit_membership_needle_object(self, expr: Expr, name_hint: str) -> ir.Value:
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            expr.ty,
            expr,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                expr.ty,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass
        value = self._emit_expr(expr)
        return self._emit_value_as_pcc_object_or_bridge(
            value,
            expr.ty,
            name_hint,
        )

    def _emit_membership_tuple_literal(
        self, lhs_expr: Expr, rhs: TupleExpr, negate: bool
    ) -> ir.Value:
        """Unroll ``x in (a, b, c)`` as ``x==a or x==b or x==c``."""
        lhs_obj = self._emit_membership_needle_object(
            lhs_expr,
            "cpy.tup.lit.in.key",
        )
        acc: Optional[ir.Value] = None
        for el in rhs.elems:
            v_obj = self._emit_membership_needle_object(
                el,
                "cpy.tup.lit.in.el",
            )
            eq_i32 = self.builder.call(
                self.runtime["py_obj_eq"],
                [lhs_obj, v_obj],
                name=self._fresh("tup.eq"),
            )
            eq_i1 = self.builder.icmp_signed(
                "!=",
                eq_i32,
                ir.Constant(_I32, 0),
                name=self._fresh("tup.eq.i1"),
            )
            if acc is None:
                acc = eq_i1
            else:
                acc = self.builder.or_(acc, eq_i1, name=self._fresh("tup.or"))
        if acc is None:
            # Empty tuple: ``x in ()`` is always False.
            acc = ir.Constant(_I1, 0)
        if negate:
            return self.builder.not_(acc, name=self._fresh("tup.not_in"))
        return acc

    def _emit_boolexpr(self, expr: BoolExpr) -> ir.Value:
        # Short-circuit via branch. ``and`` / ``or`` return either the
        # left operand or the right operand; only the pure-bool case
        # should collapse to i1.
        fn = self.current_function

        lhs = self._emit_expr(expr.left)
        lhs_is_cpy = lhs in getattr(self, "_cpy_values", ())
        lhs_owned = False
        if lhs_is_cpy:
            self._guard_cpy_value_not_null(lhs)
            lhs_owned = self._cpy_value_is_owned(lhs)
        lhs_b = self._truthy(lhs, expr.left.ty)
        result_ty = expr.ty
        lhs_val = None
        if not isinstance(result_ty, BoolType):
            lhs_val = self._coerce(lhs, expr.left.ty, result_ty)
        elif lhs_is_cpy and lhs_owned:
            # Bool-typed and/or keeps only truthiness, never the operand ref.
            self.builder.call(self.runtime["py_cpy_decref"], [lhs])
            self._forget_owned_cpy_value(lhs)

        rhs_bb = fn.append_basic_block(name=self._fresh("bool.rhs"))
        short_bb = fn.append_basic_block(name=self._fresh("bool.short"))
        end_bb = fn.append_basic_block(name=self._fresh("bool.end"))
        entry_bb = self.builder._block

        if expr.op == "and":
            # if lhs then compute rhs else short-circuit false.
            self.builder.cbranch(lhs_b, rhs_bb, short_bb)
        elif expr.op == "or":
            # if lhs then short-circuit true else compute rhs.
            self.builder.cbranch(lhs_b, short_bb, rhs_bb)
        else:
            raise NotImplementedError(f"Layer 1 bool op {expr.op!r} not supported")

        if not isinstance(result_ty, BoolType):
            # An owned lhs is now branch-managed: the short edge transfers it
            # into the result phi, while the rhs edge discards it before the
            # next source operand is evaluated.
            if lhs_is_cpy and lhs_owned:
                self._forget_owned_cpy_value(lhs)

            self.builder.position_at_end(short_bb)
            short_val = lhs_val
            short_exit = self.builder._block

            self.builder.position_at_end(rhs_bb)
            if lhs_is_cpy and lhs_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [lhs])
            rhs = self._emit_expr(expr.right)
            rhs_is_cpy = rhs in getattr(self, "_cpy_values", ())
            rhs_owned = False
            if rhs_is_cpy:
                self._guard_cpy_value_not_null(rhs)
                rhs_owned = self._cpy_value_is_owned(rhs)
            rhs_val = self._coerce(rhs, expr.right.ty, result_ty)
            rhs_exit = self.builder._block

            cpy_result = lhs_is_cpy or rhs_is_cpy
            if cpy_result:
                self.builder.position_at_end(short_exit)
                if not lhs_is_cpy:
                    short_val, short_owned = (
                        self._marshal_to_cpython_consuming_source(
                        short_val,
                        expr.left.ty,
                        expr.left,
                        )
                    )
                    self._guard_cpy_value_not_null(short_val)
                    if short_owned:
                        self._forget_owned_cpy_value(short_val)
                    else:
                        self.builder.call(
                            self.runtime["py_cpy_incref"],
                            [short_val],
                        )
                elif not lhs_owned:
                    self.builder.call(self.runtime["py_cpy_incref"], [short_val])
                short_exit = self.builder._block

                self.builder.position_at_end(rhs_exit)
                if not rhs_is_cpy:
                    rhs_val, rhs_owned = (
                        self._marshal_to_cpython_consuming_source(
                        rhs_val,
                        expr.right.ty,
                        expr.right,
                        )
                    )
                    self._guard_cpy_value_not_null(rhs_val)
                if rhs_owned:
                    self._forget_owned_cpy_value(rhs_val)
                else:
                    self.builder.call(self.runtime["py_cpy_incref"], [rhs_val])
                rhs_exit = self.builder._block

            self.builder.position_at_end(short_exit)
            self.builder.branch(end_bb)
            self.builder.position_at_end(rhs_exit)
            self.builder.branch(end_bb)

            self.builder.position_at_end(end_bb)
            phi = self.builder.phi(
                self._storage_ir_type(result_ty), name=self._fresh(expr.op)
            )
            phi.add_incoming(short_val, short_exit)
            phi.add_incoming(rhs_val, rhs_exit)
            if cpy_result:
                return self._mark_owned_cpy_value(phi)
            return phi

        self.builder.position_at_end(short_bb)
        self.builder.branch(end_bb)
        short_exit = self.builder._block

        self.builder.position_at_end(rhs_bb)
        rhs = self._emit_expr(expr.right)
        rhs_is_cpy = rhs in getattr(self, "_cpy_values", ())
        if rhs_is_cpy:
            self._guard_cpy_value_not_null(rhs)
        rhs_b = self._truthy(rhs, expr.right.ty)
        if rhs_is_cpy and self._cpy_value_is_owned(rhs):
            self.builder.call(self.runtime["py_cpy_decref"], [rhs])
            self._forget_owned_cpy_value(rhs)
        rhs_exit = self.builder._block
        self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(_I1, name=self._fresh(expr.op))
        if expr.op == "and":
            phi.add_incoming(ir.Constant(_I1, 0), short_exit)
            phi.add_incoming(rhs_b, rhs_exit)
        else:  # "or"
            phi.add_incoming(ir.Constant(_I1, 1), short_exit)
            phi.add_incoming(rhs_b, rhs_exit)
        return phi

    def _emit_boolexpr_as_pcc_object(self, expr: BoolExpr) -> ir.Value:
        # Object-boundary short-circuit keeps Python's selected-operand
        # semantics while avoiding raw valueclass payload phis.
        fn = self.current_function
        dyn_ty = DynType(name="dyn")

        old_prefer_native = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            lhs_obj = self._emit_expr_as_pcc_object(expr.left)
        finally:
            self._prefer_native_callable_values = old_prefer_native
        lhs_b = self._truthy(lhs_obj, dyn_ty)

        rhs_bb = fn.append_basic_block(name=self._fresh("bool.obj.rhs"))
        end_bb = fn.append_basic_block(name=self._fresh("bool.obj.end"))
        entry_bb = self.builder._block

        if expr.op == "and":
            self.builder.cbranch(lhs_b, rhs_bb, end_bb)
        elif expr.op == "or":
            self.builder.cbranch(lhs_b, end_bb, rhs_bb)
        else:
            raise NotImplementedError(f"Layer 1 bool op {expr.op!r} not supported")

        self.builder.position_at_end(rhs_bb)
        old_prefer_native = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            rhs_obj = self._emit_expr_as_pcc_object(expr.right)
        finally:
            self._prefer_native_callable_values = old_prefer_native
        rhs_exit = self.builder._block
        self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh(f"{expr.op}.obj"))
        phi.add_incoming(lhs_obj, entry_bb)
        phi.add_incoming(rhs_obj, rhs_exit)
        return phi
