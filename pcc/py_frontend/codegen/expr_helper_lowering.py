"""Misc expression helper lowering for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolLit,
    BoolType,
    DynType,
    Expr,
    FloatType,
    IntLit,
    IntType,
    Name,
    StrLit,
    Subscript,
    Type,
)
from . import marshal


_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()


def emit_python_floordiv_i64_unchecked(
    builder,
    a: ir.Value,
    b: ir.Value,
    name_prefix: str,
) -> ir.Value:
    """Emit Python signed floor division after the caller rejects ``b == 0``.

    Both the regular unboxed-int lowering and the pure low-IR lowering use
    this arithmetic core.  Zero-division handling remains with the caller
    because only the regular path has an exception exit; low IR admits this
    helper only for a divisor already proven to be a non-zero literal.
    """
    zero = ir.Constant(a.type, 0)
    one = ir.Constant(a.type, 1)
    q = builder.sdiv(a, b, name=name_prefix + ".q")
    r = builder.srem(a, b, name=name_prefix + ".r")
    r_nz = builder.icmp_signed("!=", r, zero, name=name_prefix + ".r_nz")
    r_neg = builder.icmp_signed("<", r, zero, name=name_prefix + ".r_neg")
    b_neg = builder.icmp_signed("<", b, zero, name=name_prefix + ".b_neg")
    sign_diff = builder.xor(r_neg, b_neg, name=name_prefix + ".sign_diff")
    need_fix = builder.and_(r_nz, sign_diff, name=name_prefix + ".need_fix")
    q_minus_1 = builder.sub(q, one, name=name_prefix + ".q_fix")
    return builder.select(need_fix, q_minus_1, q, name=name_prefix)


class ExprHelperLoweringMixin:
    def _join_reversed_strs(self, parts: list[str]) -> str:
        rev: list[str] = []
        i = len(parts) - 1
        while i >= 0:
            rev.append(parts[i])
            i -= 1
        return ".".join(rev)

    def _tuple_elems_are_uniform(
        self,
        elems: tuple[Type, ...],
        first: Type,
    ) -> bool:
        i = 0
        while i < len(elems):
            if elems[i] != first:
                return False
            i += 1
        return True

    def _lambda_simple_subscript(self, expr, param_name):
        """If ``expr`` is ``Name(param)[IntLit|StrLit]``, return the
        Python literal value. Else None."""
        if not isinstance(expr, Subscript):
            return None
        if not (isinstance(expr.obj, Name) and expr.obj.ident == param_name):
            return None
        if isinstance(expr.idx, IntLit):
            return expr.idx.value
        if isinstance(expr.idx, StrLit):
            return expr.idx.value
        return None


    # -- Comprehensions -----------------------------------------------


    def _emit_comprehension_innermost(
        self,
        kind: str,
        container: ir.Value,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        expressions = (key_expr, val_expr) if kind == "dict" else (elt_expr,)
        roots = []
        operands = [container]
        for expression in expressions:
            value = self._emit_expr_with_cpy_operand_cleanup(
                expression, (), as_pcc_object=True,
                rooted_pcc_lifetimes=tuple(roots),
            )
            owned = self._owned_release_needed(value, expression) or (
                self._container_store_temp_needs_release(expression, expression.ty, False)
            )
            root = self._enter_container_temp_root(value, self._fresh("comp.element"))
            roots.append((root, owned))
            operands.append(value)
        # Insertion retains its operands. Balance the expression owners on
        # success and on hash/equality errors; keep a dict key alive while its
        # value is evaluated (which may rebind the key's source or raise).
        old_err = self._current_try_err_block()
        target = old_err if old_err is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), target, "comp.element.error", rooted_pcc_lifetimes=tuple(roots),
        )
        try:
            fn_name = "py_dict_set" if kind == "dict" else (
                "py_list_append" if kind == "list" else "py_set_add"
            )
            self.builder.call(self.runtime[fn_name], operands)
            self._emit_post_call_err_check(getattr(expressions[0], "span", None))
        finally:
            self._try_err_block = old_err
        self._release_rooted_pcc_lifetimes(tuple(roots))

    def _emit_comprehension_level(
        self,
        kind: str,
        container: ir.Value,
        generators: list,
        tuple_unpacks: list,
        idx: int,
        elt_expr,
        key_expr,
        val_expr,
    ) -> None:
        if idx >= len(generators):
            self._emit_comprehension_innermost(
                kind,
                container,
                elt_expr,
                key_expr,
                val_expr,
            )
            return
        self._emit_comprehension_generator(
            kind,
            container,
            generators,
            tuple_unpacks,
            idx,
            elt_expr,
            key_expr,
            val_expr,
        )













    def _emit_as_object(self, expr: Expr) -> ir.Value:
        """Emit ``expr`` and marshal the result to PyObject*."""
        if isinstance(expr.ty, IntType):
            exact = self._maybe_emit_exact_int_object(expr)
            if exact is not None:
                return exact
        v = self._emit_expr(expr)
        boxed_valueclass = self._emit_valueclass_payload_to_object(v, expr.ty)
        if boxed_valueclass is not None:
            return boxed_valueclass
        return marshal.marshal_to_object(
            self.builder, self.module, self.runtime, v, expr.ty
        )


    # -- BinOp ---------------------------------------------------------






    def _python_floordiv_i64(self, a: ir.Value, b: ir.Value) -> ir.Value:
        """Python-correct signed floor division on i64."""
        b_is_zero = self.builder.icmp_signed(
            "==", b, ir.Constant(_I64, 0), name=self._fresh("fdiv_bz")
        )
        self._emit_zero_division_check(
            b_is_zero, "division by zero"
        )
        return emit_python_floordiv_i64_unchecked(
            self.builder,
            a,
            b,
            self._fresh("floordiv"),
        )

    def _python_mod_i64(self, a: ir.Value, b: ir.Value) -> ir.Value:
        """Python-correct signed mod on i64; sign follows divisor.

        ``r = a srem b; if (r != 0) && ((r < 0) != (b < 0)) then r = r + b``.
        """
        b_is_zero = self.builder.icmp_signed(
            "==", b, ir.Constant(_I64, 0), name=self._fresh("mod_bz")
        )
        self._emit_zero_division_check(
            b_is_zero, "division by zero"
        )
        r = self.builder.srem(a, b, name=self._fresh("r"))
        zero = ir.Constant(_I64, 0)
        r_nz = self.builder.icmp_signed("!=", r, zero, name=self._fresh("r_nz"))
        r_neg = self.builder.icmp_signed("<", r, zero, name=self._fresh("r_neg"))
        b_neg = self.builder.icmp_signed("<", b, zero, name=self._fresh("b_neg"))
        sign_diff = self.builder.xor(r_neg, b_neg, name=self._fresh("sign_diff"))
        need_fix = self.builder.and_(r_nz, sign_diff, name=self._fresh("need_fix"))
        r_plus_b = self.builder.add(r, b, name=self._fresh("r_fix"))
        return self.builder.select(need_fix, r_plus_b, r, name=self._fresh("mod"))

    def _get_floor_intrinsic(self) -> ir.Function:
        name = "llvm.floor.f64"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_DOUBLE, [_DOUBLE])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn

    def _get_ceil_intrinsic(self) -> ir.Function:
        name = "llvm.ceil.f64"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_DOUBLE, [_DOUBLE])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn

    def _get_rint_function(self) -> ir.Function:
        # libm rint(): round to nearest, ties to even (the default FP rounding
        # mode), which matches CPython's round() banker's rounding. Used
        # instead of floor(x + 0.5), which rounds half away from zero.
        name = "rint"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_DOUBLE, [_DOUBLE])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn

    def _get_fmod_function(self) -> ir.Function:
        name = "fmod"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_DOUBLE, [_DOUBLE, _DOUBLE])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn

    def _get_pow_function(self) -> ir.Function:
        name = "pow"
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.Function):
            return existing
        fnty = ir.FunctionType(_DOUBLE, [_DOUBLE, _DOUBLE])
        fn = ir.Function(self.module, fnty, name=name)
        fn.linkage = "external"
        return fn

    # -- UnaryOp -------------------------------------------------------

    def _emit_expr_as_i64(self, expr: Expr) -> ir.Value:
        """Emit an expression and coerce the result to ``i64``.

        Accepts native int/bool (fast path) and object-typed integers
        (via ``py_int_to_i64``, for e.g. a ``dict`` value that was typed
        as int but materialised as PyObject*).
        """
        if isinstance(expr, IntLit):
            return ir.Constant(_I64, int(expr.value))
        if isinstance(expr, BoolLit):
            return ir.Constant(_I64, 1 if bool(expr.value) else 0)
        value = self._emit_expr(expr)
        if value in getattr(self, "_cpy_values", ()):
            return self.builder.call(
                self.runtime["py_cpy_to_i64"],
                [value],
                name=self._fresh("cpy.to_i64"),
            )
        if isinstance(expr.ty, IntType):
            if self._ir_type_matches(value.type, _I64):
                return value
            if isinstance(value.type, ir.PointerType):
                return marshal.marshal_from_object(
                    self.builder, self.module, self.runtime, value, expr.ty
                )
            return self.builder.sext(value, _I64, name=self._fresh("sext64"))
        if isinstance(expr.ty, BoolType):
            if self._ir_type_matches(value.type, _I1):
                return self.builder.zext(value, _I64, name=self._fresh("b2i"))
            if isinstance(value.type, ir.PointerType):
                i = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    IntType(name="int"),
                )
                return i
            return self.builder.zext(value, _I64, name=self._fresh("b2i"))
        if isinstance(expr.ty, FloatType):
            return self.builder.fptosi(value, _I64, name=self._fresh("f2i"))
        if isinstance(expr.ty, DynType) or self._is_object(expr.ty):
            # Go through the runtime.
            boxed = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, value, expr.ty
            )
            return marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                boxed,
                IntType(name="int"),
            )
        raise NotImplementedError(
            f"Layer 1 cannot reduce {type(expr.ty).__name__} to i64"
        )
