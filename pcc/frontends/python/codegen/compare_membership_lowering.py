"""Comparison and membership lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    RawPointerType,
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
    Slice,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
    IfExpr,
)
from pcc.frontends.python.codegen.method_call_lowering import (
    _method_pointer_provenance,
    _method_source_arg_type,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.unary_call_lowering import (
    folded_int_literal, is_i64_int_literal,
)
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_BOOL, PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_DICT, PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_TUPLE

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

# ``a <op> b`` is ``b <mirrored op> a``.
_MIRRORED_COMPARE_OP = {
    "==": "==",
    "!=": "!=",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}


class CompareMembershipLoweringMixin:
    def _emit_runtime_object_compare(
        self,
        expr: Compare,
        lhs_obj: ir.Value,
        rhs_obj: ir.Value,
        name_prefix: str,
        pinned_release_on_error=(),
        roots=None,
    ) -> ir.Value:
        """Emit one runtime object-comparison contract.

        Object-vs-object comparisons and DynType ordering used to duplicate
        runtime-symbol selection, the raising-call edge, and bool
        normalization.  Operands are projected by the caller so evaluation
        order and valueclass/CPython boundary policy stay outside this owner;
        a caller holding pinned operands passes their error-edge cleanup.
        A caller whose operands live in owning slot roots passes ``roots``
        instead of raw operands, and the call leases them for its duration.
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
        # Two tagged ints, ``(value << 1) | 1``, are exact ints in
        # [-2**62, 2**62 - 1]; their words order and compare equal exactly
        # as the values do, and int's comparisons cannot be overridden.
        # Decide that pair inline; every other pair keeps the runtime
        # protocol below (bigints, floats, bools, user dunders).
        if roots is not None:
            lhs_word = self.builder.load(roots[0], name=self._fresh(name_prefix + ".cmp.lhs"))
            rhs_word = self.builder.load(roots[1], name=self._fresh(name_prefix + ".cmp.rhs"))
        else:
            lhs_word, rhs_word = lhs_obj, rhs_obj
        lhs_bits = self.builder.ptrtoint(lhs_word, _I64, name=self._fresh(name_prefix + ".cmp.lbits"))
        rhs_bits = self.builder.ptrtoint(rhs_word, _I64, name=self._fresh(name_prefix + ".cmp.rbits"))
        both_bits = self.builder.and_(lhs_bits, rhs_bits, name=self._fresh(name_prefix + ".cmp.tags"))
        both_tagged = self.builder.icmp_unsigned(
            "!=",
            self.builder.and_(both_bits, ir.Constant(_I64, 1)),
            ir.Constant(_I64, 0),
            name=self._fresh(name_prefix + ".cmp.tagged"),
        )
        fast_block = self.current_function.append_basic_block(self._fresh("int.cmp.fast"))
        slow_block = self.current_function.append_basic_block(self._fresh("int.cmp.slow"))
        done_block = self.current_function.append_basic_block(self._fresh("int.cmp.done"))
        self.builder.cbranch(both_tagged, fast_block, slow_block)
        self.builder.position_at_end(fast_block)
        fast = self.builder.icmp_signed(
            expr.op, lhs_bits, rhs_bits, name=self._fresh(name_prefix + ".cmp.fast")
        )
        fast_end = self.builder.block
        self.builder.branch(done_block)
        self.builder.position_at_end(slow_block)
        if roots is not None:
            compared = self._slot_call_runtime_call(runtime_name, roots, span=expr.span)
        else:
            compared = self.builder.call(
                self.runtime[runtime_name],
                [lhs_obj, rhs_obj],
                name=self._fresh(name_prefix + ".cmp"),
            )
            self._emit_post_call_err_check(
                self._expr_span_or_none(expr),
                pinned_release_on_error=pinned_release_on_error,
            )
        result = self.builder.icmp_signed(
            "!=",
            compared,
            ir.Constant(compared.type, 0),
            name=self._fresh(name_prefix + ".cmp.i1"),
        )
        if expr.op == "!=":
            result = self.builder.not_(
                result,
                name=self._fresh(name_prefix + ".ne"),
            )
        slow_end = self.builder.block
        self.builder.branch(done_block)
        self.builder.position_at_end(done_block)
        joined = self.builder.phi(_I1, name=self._fresh(name_prefix + ".cmp.result"))
        joined.add_incoming(fast, fast_end)
        joined.add_incoming(result, slow_end)
        return joined

    def _str_char_subscript_operand(self, operand: Expr) -> Optional[Subscript]:
        """``operand`` when it is ``s[i]`` on a ``str`` with an ``int`` index."""
        if isinstance(operand, Subscript):
            if (
                isinstance(operand.obj.ty, StrType)
                and not isinstance(operand.idx, Slice)
                and isinstance(operand.idx.ty, IntType)
            ):
                return operand
        return None

    def _emit_str_char_projection_compare(self, expr: Compare) -> Optional[ir.Value]:
        """``s[i] == "c"`` / ``!=`` / ``in "abc"`` / ``not in`` as a code point test.

        ``s[i]`` of a str is exactly one character, so these tests need only its
        code point: the one-character string is never built (the value-model
        projection of a character).  Evaluation order and the IndexError are
        those of ``s[i]``; the literal side has no effects.
        """
        subscript = None
        literal = None
        if expr.op in ("==", "!="):
            subscript = self._str_char_subscript_operand(expr.lhs)
            literal = expr.rhs
            if subscript is None:
                subscript = self._str_char_subscript_operand(expr.rhs)
                literal = expr.lhs
            if (
                subscript is None
                or not isinstance(literal, StrLit)
                or len(literal.value) != 1
            ):
                return None
        elif expr.op in ("in", "not in"):
            subscript = self._str_char_subscript_operand(expr.lhs)
            literal = expr.rhs
            if subscript is None or not isinstance(literal.ty, StrType):
                return None
        else:
            return None
        obj = self._emit_expr(subscript.obj)
        index = self._emit_expr_as_i64(subscript.idx)
        code = self.builder.call(
            self.runtime["py_str_codepoint_at"],
            [obj, index],
            name=self._fresh("str.char.code"),
        )
        self._emit_post_call_err_check(getattr(subscript, "span", None))
        self._gc_release_if_owned(obj, subscript.obj)
        hit: ir.Value = ir.Constant(_I1, 0)
        if isinstance(literal, StrLit):
            for value in sorted({ord(ch) for ch in literal.value}):
                is_code = self.builder.icmp_signed(
                    "==", code, ir.Constant(_I64, value), name=self._fresh("str.char.is")
                )
                hit = self.builder.or_(hit, is_code, name=self._fresh("str.char.hit"))
        else:
            # ``s[i] in t`` for any str ``t`` (a module-level character set,
            # a parameter): its current value, scanned for the code point.
            haystack = self._emit_expr(literal)
            found = self.builder.call(
                self.runtime["py_str_contains_codepoint"],
                [haystack, code],
                name=self._fresh("str.char.found"),
            )
            self._gc_release_if_owned(haystack, literal)
            hit = self.builder.icmp_signed(
                "!=", found, ir.Constant(_I64, 0), name=self._fresh("str.char.hit")
            )
        if expr.op in ("!=", "not in"):
            hit = self.builder.xor(hit, ir.Constant(_I1, 1), name=self._fresh("str.char.miss"))
        return hit

    def _emit_compare(self, expr: Compare) -> ir.Value:
        if isinstance(expr.lhs.ty, RawPointerType) or isinstance(expr.rhs.ty, RawPointerType):
            manual = (getattr(self, "_runtime_port_module", False)
                      or getattr(self, "_freestanding_module", False))
            types_ok = (isinstance(expr.lhs.ty, RawPointerType) and isinstance(expr.rhs.ty, RawPointerType)) or (
                manual and isinstance(expr.lhs.ty, (RawPointerType, DynType))
                and isinstance(expr.rhs.ty, (RawPointerType, DynType)))
            if not types_ok or expr.op not in ("==", "!=", "is", "is not"):
                raise NotImplementedError("raw pointer comparison requires compatible pointer views")
            lhs = self._emit_expr(expr.lhs)
            rhs = self._emit_expr(expr.rhs)
            if not isinstance(lhs.type, ir.PointerType) or not isinstance(rhs.type, ir.PointerType):
                raise NotImplementedError("raw pointer comparison requires ptr ABI values")
            op = "==" if expr.op == "is" else "!=" if expr.op == "is not" else expr.op
            return self.builder.icmp_unsigned(op, lhs, rhs, name=self._fresh("raw.ptr.compare"))
        builtin_type_cmp = self._emit_builtin_type_name_compare(expr)
        if builtin_type_cmp is not None:
            return builtin_type_cmp

        char_cmp = self._emit_str_char_projection_compare(expr)
        if char_cmp is not None:
            return char_cmp

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
                and not is_i64_int_literal(operand)
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
            # A dunder returning an object may answer NotImplemented; only the
            # dynamic comparison continues that protocol (the reflected
            # dunder once, then identity for ==/!= or TypeError for ordering).
            # Truth-testing the answer here made ``key == "a"`` True.
            target = self._dunder_dispatch_target(expr.lhs, cmp_dunder)
            if target is not None and isinstance(
                target[1].methods[cmp_dunder].function_type.return_type,
                ir.PointerType,
            ):
                cmp_dunder = None
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

        exact_mixed = False
        if not (getattr(self, "_freestanding_module", False)
                or getattr(self, "_runtime_port_module", False)):
            if (expr.op in ("<", "<=", ">", ">=")
                    and (isinstance(lhs_ty, DynType) or isinstance(rhs_ty, DynType))
                    and (isinstance(lhs_ty, FloatType) or isinstance(rhs_ty, FloatType))):
                # A dyn operand can hold any object: CPython's rich comparison
                # answers (user dunders, TypeError for unorderable pairs); a
                # float unbox would read a complex, tuple or None as a number.
                exact_mixed = True
            for operand, other_ty in ((expr.lhs, rhs_ty), (expr.rhs, lhs_ty)):
                if not isinstance(other_ty, (DynType, FloatType)):
                    continue
                if self._int_expr_needs_exact_object_boundary(operand):
                    exact_mixed = True
                elif (isinstance(other_ty, DynType) and isinstance(operand.ty, IntType)
                      and operand.ty.name == "int"):
                    literal = folded_int_literal(operand)
                    # Even an i64-range literal can allocate when boxed for a
                    # dynamic comparison. Root the borrowed operand before
                    # that allocation, and consume the temporary integer.
                    if literal is not None and not (
                        self._STATIC_TAGGED_INT_MIN <= literal <= self._STATIC_TAGGED_INT_MAX
                    ):
                        exact_mixed = True
        if exact_mixed:
            # A mixed comparison is an object boundary for an unbounded
            # Python int. Emitting it in the scaffold i64 lane first changes
            # +2**63 into -2**63 before either dynamic or float comparison.
            # Keep both operands in authoritative roots through evaluation,
            # comparison callbacks and cleanup; literal trees must use the
            # exact producer even when this module normally uses raw ints.
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            roots = []
            try:
                for operand in (expr.lhs, expr.rhs):
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    if self._slot_call_literal_integer_kind(operand):
                        root = self._emit_slot_call_literal_integer(operand, "compare.exact.int")
                    else:
                        root = self._emit_slot_call_operand(operand, "compare.exact.operand")
                    roots.append(root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                result = self._emit_runtime_object_compare(
                    expr, None, None, "exact.mixed", roots=tuple(roots),
                )
                self._release_slot_call_roots(tuple(roots))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return result

        if expr.op in ("==", "!="):
            lhs_scalar = isinstance(lhs_ty, (IntType, BoolType, FloatType))
            rhs_scalar = isinstance(rhs_ty, (IntType, BoolType, FloatType))
            if (isinstance(lhs_ty, DynType) and rhs_scalar) or (
                isinstance(rhs_ty, DynType) and lhs_scalar
            ):
                # Dynamic equality consumes Python objects. Project ordinary
                # integer expressions before evaluation so a raw-ABI module
                # cannot narrow an intermediate bignum through i64 arithmetic.
                # A freestanding module is exempt: its contract forbids every
                # managed-runtime reference, and the guard at the top of this
                # method already rejects the out-of-range literals that make
                # narrowing possible, so there is no bignum here to preserve.
                # Without this, `extern_returning_dyn(...) != 0` boxed the 0
                # and the whole runtime archive stopped building.
                project_ints = not getattr(self, "_freestanding_module", False)
                lhs = (
                    self._emit_expr_as_pcc_object(expr.lhs)
                    if project_ints
                    and isinstance(lhs_ty, IntType)
                    and lhs_ty.name == "int"
                    else self._emit_expr(expr.lhs)
                )
                rhs = (
                    self._emit_expr_as_pcc_object(expr.rhs)
                    if project_ints
                    and isinstance(rhs_ty, IntType)
                    and rhs_ty.name == "int"
                    else self._emit_expr(expr.rhs)
                )
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
                    return self._emit_float_scalar_compare(
                        expr.op, lhs, lhs_ty, rhs, rhs_ty, "dyn.scalar.fcmp"
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
        if (
            isinstance(lhs.type, ir.IntType)
            and isinstance(rhs.type, ir.IntType)
            and (isinstance(lhs_ty, DynType) or isinstance(rhs_ty, DynType))
        ):
            # An extern ABI integer has no boxed-object interpretation even
            # when static inference left its source type dynamic.
            lv = self._to_int64(lhs, lhs_ty)
            rv = self._to_int64(rhs, rhs_ty)
            if getattr(lhs_ty, "name", "") == "pcc.u64" or getattr(rhs_ty, "name", "") == "pcc.u64":
                return self.builder.icmp_unsigned(expr.op, lv, rv, name=self._fresh("raw.ucmp"))
            return self.builder.icmp_signed(expr.op, lv, rv, name=self._fresh("raw.icmp"))
        if isinstance(lhs_ty, FloatType) or isinstance(rhs_ty, FloatType):
            return self._emit_float_scalar_compare(
                expr.op, lhs, lhs_ty, rhs, rhs_ty, "fcmp"
            )
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
        if isinstance(lhs_ty, ClassType) or isinstance(rhs_ty, ClassType):
            # A class instance against a scalar (``key == 1``) has no integer
            # reading: box both and let the runtime protocol answer (user
            # dunders, NotImplemented, then identity or the builtin order).
            lo = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, lhs, lhs_ty
            )
            ro = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, rhs, rhs_ty
            )
            return self._emit_runtime_object_compare(expr, lo, ro, "class")
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

    def _emit_float_scalar_compare(
        self,
        op: str,
        lhs: ir.Value,
        lhs_ty: Type,
        rhs: ir.Value,
        rhs_ty: Type,
        name: str,
    ) -> ir.Value:
        """Compare two numeric scalars, at least one of them a float.

        CPython orders an int against a float by exact value
        (``float_richcompare``).  Converting the int to a double first
        rounded ``2**53 + 1`` onto ``2**53.0`` and ``10**30`` onto ``1e30``,
        so both compared equal.  An int operand a double cannot carry -- a
        raw i64 or a boxed object -- takes the exact compare; a float pair, a
        bool or a narrower machine int keeps the plain fcmp.
        """
        if isinstance(rhs_ty, FloatType) and self._fcmp_operand_is_wide_int(
            lhs, lhs_ty
        ):
            return self._emit_int_float_compare(
                op, lhs, self._to_double(rhs, rhs_ty), name
            )
        if isinstance(lhs_ty, FloatType) and self._fcmp_operand_is_wide_int(
            rhs, rhs_ty
        ):
            return self._emit_int_float_compare(
                _MIRRORED_COMPARE_OP[op], rhs, self._to_double(lhs, lhs_ty), name
            )
        lf = self._to_double(lhs, lhs_ty)
        rf = self._to_double(rhs, rhs_ty)
        if op == "!=":
            return self.builder.fcmp_unordered("!=", lf, rf, name=self._fresh(name))
        return self.builder.fcmp_ordered(op, lf, rf, name=self._fresh(name))

    def _fcmp_operand_is_wide_int(self, value: ir.Value, ty: Type) -> bool:
        if not isinstance(ty, (IntType, DynType)):
            return False
        if isinstance(value.type, ir.PointerType):
            # A CPython-backed value keeps its ``_to_double`` unbox.
            return value not in getattr(self, "_cpy_values", ())
        # pcc.u64 keeps its uitofp: type inference rejects a raw int meeting
        # a float, so only the signed lane arrives here.
        return (
            self._ir_type_matches(value.type, _I64)
            and getattr(ty, "name", "") != "pcc.u64"
        )

    def _emit_int_float_compare(
        self,
        op: str,
        value: ir.Value,
        f: ir.Value,
        name: str,
    ) -> ir.Value:
        """``value <op> f`` by exact value, for an int ``value``, double ``f``."""
        if isinstance(value.type, ir.PointerType):
            # A boxed int (or dyn object): the runtime three-way compare in
            # place of the py_float_to_f64 unbox.  -1 / 0 / 1, or 2
            # (PY_OBJ_CMP_UNORDERED) when f is NaN.
            order = self.builder.call(
                self.runtime["py_int_f64_cmp"],
                [value, f],
                name=self._fresh(name + ".order"),
            )
            if op == ">=":
                # 0 or 1: unsigned, both -1 and 2 are above 1.
                return self.builder.icmp_unsigned(
                    "<=",
                    order,
                    ir.Constant(order.type, 1),
                    name=self._fresh(name),
                )
            # ==, != and <= test the order against 0 directly.
            pred = op
            want = 0
            if op == "<":
                pred = "=="
                want = -1
            elif op == ">":
                pred = "=="
                want = 1
            return self.builder.icmp_signed(
                pred,
                order,
                ir.Constant(order.type, want),
                name=self._fresh(name),
            )
        # A raw i64, branch-free.  sitofp is monotone and f is a double, so
        # d < f proves value < f and d > f proves value > f; only a tie
        # (d == f, so f is an integer) compares value with f's exact integer
        # t.  2**63 is above every i64 and never reaches fptosi, where it is
        # poison.
        d = self.builder.sitofp(value, _DOUBLE, name=self._fresh(name + ".d"))
        lt = self.builder.fcmp_ordered("<", d, f, name=self._fresh(name + ".lt"))
        gt = self.builder.fcmp_ordered(">", d, f, name=self._fresh(name + ".gt"))
        eq = self.builder.fcmp_ordered("==", d, f, name=self._fresh(name + ".eq"))
        big = self.builder.fcmp_ordered(
            "==",
            f,
            ir.Constant(_DOUBLE, 9223372036854775808.0),
            name=self._fresh(name + ".big"),
        )
        exact = self.builder.and_(
            eq,
            self.builder.not_(big, name=self._fresh(name + ".small")),
            name=self._fresh(name + ".exact"),
        )
        in_range = self.builder.select(
            exact,
            f,
            ir.Constant(_DOUBLE, 0.0),
            name=self._fresh(name + ".f"),
        )
        t = self.builder.fptosi(in_range, _I64, name=self._fresh(name + ".t"))
        tie_op = "==" if op in ("==", "!=") else op
        tie = self.builder.icmp_signed(
            tie_op, value, t, name=self._fresh(name + ".tie")
        )
        if op in ("==", "!="):
            same = self.builder.and_(exact, tie, name=self._fresh(name + ".same"))
            if op == "!=":
                return self.builder.not_(same, name=self._fresh(name))
            return same
        if op in ("<", "<="):
            # At 2**63 every i64 is below f.
            below = self.builder.or_(big, tie, name=self._fresh(name + ".below"))
            return self.builder.or_(
                lt,
                self.builder.and_(eq, below, name=self._fresh(name + ".at")),
                name=self._fresh(name),
            )
        return self.builder.or_(
            gt,
            self.builder.and_(exact, tie, name=self._fresh(name + ".at")),
            name=self._fresh(name),
        )

    def _emit_str_tag_fast_compare(
        self,
        expr: Compare,
        lhs: ir.Value,
        rhs: ir.Value,
        dynamic: ir.Value,
        cleanup,
    ) -> ir.Value:
        """``==``/``!=`` between a str and a dynamic object.

        A dynamic operand whose exact tag is ``str`` compares by value with
        ``py_str_eq``, which neither raises nor runs user code.  Anything
        else (a str subclass, an object defining ``__eq__``) takes the
        generic runtime comparison with its raising edge.
        """
        tag = self.builder.call(
            self.runtime["py_obj_type_tag"], [dynamic],
            name=self._fresh("str.predicate.tag"),
        )
        is_str = self.builder.icmp_signed(
            "==", tag, ir.Constant(tag.type, PY_TYPE_STR),
            name=self._fresh("str.predicate.is_str"),
        )
        fn = self.current_function
        exact_bb = fn.append_basic_block(name=self._fresh("str.predicate.exact"))
        generic_bb = fn.append_basic_block(name=self._fresh("str.predicate.generic"))
        done_bb = fn.append_basic_block(name=self._fresh("str.predicate.done"))
        self.builder.cbranch(is_str, exact_bb, generic_bb)

        self.builder.position_at_end(exact_bb)
        status = self.builder.call(
            self.runtime["py_str_eq"], [lhs, rhs],
            name=self._fresh("str.predicate.eq"),
        )
        exact = self.builder.icmp_signed(
            "!=", status, ir.Constant(status.type, 0),
            name=self._fresh("str.predicate.eq.i1"),
        )
        if expr.op == "!=":
            exact = self.builder.not_(exact, name=self._fresh("str.predicate.eq.not"))
        self.builder.branch(done_bb)
        exact_end = self.builder.block

        self.builder.position_at_end(generic_bb)
        generic = self._emit_runtime_object_compare(
            expr, lhs, rhs, "str.predicate", pinned_release_on_error=cleanup,
        )
        self.builder.branch(done_bb)
        generic_end = self.builder.block

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_I1, name=self._fresh("str.predicate.result"))
        result.add_incoming(exact, exact_end)
        result.add_incoming(generic, generic_end)
        return result

    def _emit_owned_string_predicate(
        self,
        expr: Compare,
        contains: bool,
        object_compare: bool = False,
        dynamic_operand: str = "",
    ) -> ir.Value:
        """Consume transient strings and preserve the LHS through RHS effects.

        ``dynamic_operand`` ("lhs"/"rhs") names the dynamic side of an object
        comparison whose other side is statically a str.
        """
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
        cleanup = ((lhs, True),)
        if rhs_owned:
            cleanup = ((lhs, True), (rhs, True))
        if object_compare:
            # One owner for runtime object comparison (symbol, raising edge,
            # bool normalization and ``!=``); this path only adds ownership.
            if dynamic_operand:
                dynamic = lhs if dynamic_operand == "lhs" else rhs
                result = self._emit_str_tag_fast_compare(
                    expr, lhs, rhs, dynamic, cleanup,
                )
            else:
                result = self._emit_runtime_object_compare(
                    expr, lhs, rhs, "str.predicate", pinned_release_on_error=cleanup,
                )
            self._gc_unpin(lhs)
            self._gc_release(lhs)
            if rhs_owned:
                self._gc_unpin(rhs)
                self._gc_release(rhs)
            return result
        operands = [rhs, lhs] if contains else [lhs, rhs]
        symbol = "py_str_contains" if contains else "py_str_eq"
        status = self.builder.call(
            self.runtime[symbol], operands, name=self._fresh("str.predicate"),
        )
        if contains:
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
            dynamic_operand = "lhs"
        elif isinstance(lhs_ty, StrType) and isinstance(rhs_ty, DynType):
            dynamic_operand = "rhs"
        else:
            return None

        # Keep source evaluation order and both operand owners through
        # callbacks. The generic equality contract also permits a non-string
        # dynamic object to implement __eq__ against the string; an exact
        # str compares natively.
        return self._emit_owned_string_predicate(
            expr, False, object_compare=True, dynamic_operand=dynamic_operand,
        )

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
            if raw in getattr(self, "_cpy_values", ()):
                return False
            # An emitter-recorded owner (a dynamic ``functions[0](x)`` call
            # result, say) is owned whatever the AST shape suggests; leaving
            # it out leaked the operand of every such identity test.
            if self._value_is_owned_object(raw):
                return True
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

    _MEMBERSHIP_CONSTANT_LITERALS = (IntLit, BoolLit, FloatLit, NoneLit, StrLit)

    def _membership_tuple_literal_is_constant(self, rhs: TupleExpr) -> bool:
        """True when unrolling ``x in (...)`` observes what Python observes.

        ``x in (a, b, c)`` builds the tuple -- evaluating every element --
        and then compares left to right, stopping at the first match.  The
        unrolled or-chain also evaluates every element, but it compares all
        of them, so the two differ only when a comparison is observable.
        Against builtin literals it is not, and then the tuple never has to
        exist: ``pcrel not in (0, 1)`` runs 7.3 million times in one Stage2
        link, and routing it through py_obj_contains allocates, pins and
        releases a tuple on every one of them.
        """
        for element in rhs.elems:
            if not isinstance(element, self._MEMBERSHIP_CONSTANT_LITERALS):
                return False
        return True

    def _emit_membership(self, expr: Compare) -> ir.Value:
        """``in`` / ``not in`` over str / list / dict / set / tuple."""
        if (
            isinstance(expr.rhs, TupleExpr)
            and self._membership_tuple_literal_is_constant(expr.rhs)
        ):
            return self._emit_membership_tuple_literal(
                expr.lhs,
                expr.rhs,
                negate=(expr.op == "not in"),
                span=getattr(expr, "span", None),
            )
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

        # Evaluate each operand once, in Python order. Preserve the original
        # domain until the container is known: a late CPython container must
        # receive the original CPython needle, not a converted copy.
        valueclass_needle = self._emit_valueclass_constructor_needle(expr.lhs)
        if valueclass_needle is not None:
            lhs_raw = valueclass_needle
        else:
            lhs_raw = self._emit_expr(expr.lhs)
        lhs_is_cpy = lhs_raw in getattr(self, "_cpy_values", ())
        cpy_live = ()
        pcc_live = ()
        if lhs_is_cpy:
            self._guard_cpy_value_not_null(lhs_raw)
            lhs = lhs_raw
            if not self._cpy_value_is_owned(lhs):
                self.builder.call(self.runtime["py_cpy_incref"], [lhs])
                self._mark_owned_cpy_value(lhs)
            cpy_live = (lhs,)
        else:
            if valueclass_needle is not None:
                lhs = valueclass_needle
            else:
                lhs = self._emit_value_as_pcc_object_or_bridge(
                    lhs_raw, expr.lhs.ty, "membership.needle",
                )
            if lhs is lhs_raw and not self._owned_release_needed(lhs, expr.lhs):
                lhs = self._gc_retain(lhs, name=self._fresh("membership.lhs.retain"))
            self._gc_pin(lhs)
            pcc_live = ((lhs, True),)
        rhs_raw = self._emit_expr_with_cpy_operand_cleanup(
            expr.rhs, cpy_live, pinned_pcc=pcc_live,
        )
        if rhs_raw in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(
                rhs_raw, cpy_live, pinned_pcc_on_error=pcc_live,
            )
            if not self._cpy_value_is_owned(rhs_raw):
                self.builder.call(self.runtime["py_cpy_incref"], [rhs_raw])
                self._mark_owned_cpy_value(rhs_raw)
            if lhs_is_cpy:
                lhs_cpy, lhs_owned = lhs, True
            else:
                lhs_cpy, lhs_owned = self._marshal_to_cpython(lhs, DynType(name="dyn"))
                self._guard_cpy_value_not_null(
                    lhs_cpy, (rhs_raw,), pinned_pcc_on_error=pcc_live,
                )
                self._gc_unpin(lhs)
                self._gc_release(lhs)
            result = self._emit_cpy_method_call1_value(
                rhs_raw, "__contains__", lhs_cpy,
                arg_owned=lhs_owned, receiver_owned=True,
            )
            self._guard_cpy_value_not_null(result)
            status = self.builder.call(
                self.runtime["py_cpy_truthy"], [result], name=self._fresh("cpy.contains.i32"),
            )
            self._guard_cpy_status_not_negative(status, (result,))
            self.builder.call(self.runtime["py_cpy_decref"], [result])
            self._forget_owned_cpy_value(result)
        else:
            rhs = self._emit_value_as_pcc_object_or_bridge(
                rhs_raw, container_ty, "membership.container",
                cpy_owned_on_error=cpy_live, pinned_pcc_on_error=pcc_live,
            )
            if rhs is rhs_raw and not self._owned_release_needed(rhs, expr.rhs):
                rhs = self._gc_retain(rhs, name=self._fresh("membership.rhs.retain"))
            self._gc_pin(rhs)
            if lhs_is_cpy:
                lhs = self._emit_value_as_pcc_object_or_bridge(
                    lhs, expr.lhs.ty, "membership.cpy.needle",
                    pinned_pcc_on_error=((rhs, True),),
                )
                self._gc_pin(lhs)
            if weak_dict_kind == "value":
                helper = "py_weak_value_dict_contains"
            elif isinstance(container_ty, StrType):
                helper = "py_str_contains"
            elif isinstance(container_ty, ListType):
                helper = "py_list_contains"
            elif isinstance(container_ty, DictType):
                helper = "py_dict_contains"
            else:
                # Tuple literals have already been evaluated, including all
                # element effects. Runtime contains preserves identity and
                # comparison short-circuiting without emitting them again.
                helper = "py_obj_contains"
            status = self.builder.call(
                self.runtime[helper], [rhs, lhs], name=self._fresh("native.contains"),
            )
            self._emit_post_call_err_check(
                getattr(expr, "span", None),
                pinned_release_on_error=((lhs, True), (rhs, True)),
            )
            self._gc_unpin(rhs)
            self._gc_release(rhs)
            self._gc_unpin(lhs)
            self._gc_release(lhs)
        contains = self.builder.icmp_signed(
            "!=", status, ir.Constant(status.type, 0), name=self._fresh("in.i1"),
        )
        if expr.op == "not in":
            return self.builder.not_(contains, name=self._fresh("not_in"))
        return contains

    def _emit_valueclass_constructor_needle(self, expr: Expr) -> Optional[ir.Value]:
        """A direct valueclass constructor as an owned valuebox, else None.

        The needle of a membership test is a value: it must hash and compare
        by fields like the valueboxes already in the container, which an
        identity instance built by ``__init__`` does not.
        """
        payload = self._maybe_emit_valueclass_constructor_payload(expr.ty, expr)
        if payload is None:
            return None
        boxed = self._emit_valueclass_payload_to_object(payload, expr.ty)
        if boxed is None:
            # The constructor arguments are already evaluated; emitting the
            # expression again would run their effects twice.
            raise NotImplementedError(
                f"valueclass needle {getattr(expr.ty, 'name', '?')!r} has a "
                "payload but no valuebox"
            )
        self._note_owned_object_value(boxed)
        return boxed

    def _emit_membership_needle_object(self, expr: Expr, name_hint: str) -> ir.Value:
        boxed_valueclass = self._emit_valueclass_constructor_needle(expr)
        if boxed_valueclass is not None:
            return boxed_valueclass
        value = self._emit_expr(expr)
        return self._emit_value_as_pcc_object_or_bridge(
            value,
            expr.ty,
            name_hint,
        )

    # Needle types whose ``==`` against a builtin literal runs no user code
    # and cannot raise, so the comparisons are unobservable.
    _MEMBERSHIP_BUILTIN_NEEDLE_TYPES = (
        IntType, BoolType, FloatType, StrType, BytesType, NoneType,
    )

    def _emit_native_int_tuple_membership(
        self, lhs_expr: Expr, value: ir.Value, rhs: TupleExpr
    ) -> Optional[ir.Value]:
        """``n in (0, 1)`` for a native int/bool ``n`` as integer compares.

        None unless the needle is already a native integer and every element
        is an int/bool literal that fits in i64.  ``True == 1`` in Python, so
        bool and int compare by value.
        """
        if not isinstance(lhs_expr.ty, (IntType, BoolType)):
            return None
        if value in getattr(self, "_cpy_values", ()):
            return None
        if not isinstance(value.type, ir.IntType) or value.type.width > 64:
            return None
        constants: list[int] = []
        for element in rhs.elems:
            if isinstance(element, BoolLit):
                constants.append(1 if bool(element.value) else 0)
            elif isinstance(element, IntLit):
                literal = int(element.value)
                # Bounds stay inside i64: pcc1 lowers its own source, and a
                # 2**63 literal (``-9223372036854775808``) overflows there.
                if literal < -9223372036854775807 or literal > 9223372036854775807:
                    return None
                constants.append(literal)
            else:
                return None
        needle = value
        if value.type.width == 1:
            needle = self.builder.zext(value, _I64, name=self._fresh("tup.in.b2i"))
        elif value.type.width < 64:
            needle = self.builder.sext(value, _I64, name=self._fresh("tup.in.sext"))
        acc: ir.Value = ir.Constant(_I1, 0)
        for constant in constants:
            hit = self.builder.icmp_signed(
                "==", needle, ir.Constant(_I64, constant),
                name=self._fresh("tup.in.is"),
            )
            acc = self.builder.or_(acc, hit, name=self._fresh("tup.in.or"))
        return acc

    def _emit_membership_tuple_literal(
        self, lhs_expr: Expr, rhs: TupleExpr, negate: bool, span=None
    ) -> ir.Value:
        """``x in (a, b, c)`` against literals, without building the tuple.

        Python compares left to right and stops at the first match.  A
        builtin needle cannot observe that, so its tests are a flat chain
        (or native integer compares); any other needle may run ``__eq__``,
        so it branches out on the first match and checks for a raised error
        after each comparison.  A needle object created here is released.
        """
        needle = self._emit_valueclass_constructor_needle(lhs_expr)
        if needle is not None:
            lhs_obj = needle
            owned = True
        else:
            value = self._emit_expr(lhs_expr)
            native = self._emit_native_int_tuple_membership(lhs_expr, value, rhs)
            if native is not None:
                if negate:
                    return self.builder.not_(native, name=self._fresh("tup.not_in"))
                return native
            lhs_obj = self._emit_value_as_pcc_object_or_bridge(
                value, lhs_expr.ty, "cpy.tup.lit.in.key",
            )
            owned = lhs_obj is not value or self._owned_release_needed(
                value, lhs_expr
            )
        observable = needle is not None or not isinstance(
            lhs_expr.ty, self._MEMBERSHIP_BUILTIN_NEEDLE_TYPES
        )
        if len(rhs.elems) == 0:
            # ``x in ()`` is always False; the needle was still evaluated.
            acc: ir.Value = ir.Constant(_I1, 0)
        elif not observable:
            acc = ir.Constant(_I1, 0)
            for el in rhs.elems:
                v_obj = self._emit_membership_needle_object(el, "cpy.tup.lit.in.el")
                eq_i32 = self.builder.call(
                    self.runtime["py_obj_eq"],
                    [lhs_obj, v_obj],
                    name=self._fresh("tup.eq"),
                )
                eq_i1 = self.builder.icmp_signed(
                    "!=", eq_i32, ir.Constant(_I32, 0),
                    name=self._fresh("tup.eq.i1"),
                )
                acc = self.builder.or_(acc, eq_i1, name=self._fresh("tup.or"))
        else:
            cleanup = ((lhs_obj, True),) if owned else ()
            if owned:
                self._gc_pin(lhs_obj)
            fn = self.current_function
            found_bb = fn.append_basic_block(name=self._fresh("tup.in.found"))
            done_bb = fn.append_basic_block(name=self._fresh("tup.in.done"))
            for el in rhs.elems:
                v_obj = self._emit_membership_needle_object(el, "cpy.tup.lit.in.el")
                eq_i32 = self.builder.call(
                    self.runtime["py_obj_eq"],
                    [lhs_obj, v_obj],
                    name=self._fresh("tup.eq"),
                )
                self._emit_post_call_err_check(span, pinned_release_on_error=cleanup)
                eq_i1 = self.builder.icmp_signed(
                    "!=", eq_i32, ir.Constant(_I32, 0),
                    name=self._fresh("tup.eq.i1"),
                )
                next_bb = fn.append_basic_block(name=self._fresh("tup.in.next"))
                self.builder.cbranch(eq_i1, found_bb, next_bb)
                self.builder.position_at_end(next_bb)
            self.builder.branch(done_bb)
            miss_end = self.builder.block
            self.builder.position_at_end(found_bb)
            self.builder.branch(done_bb)
            self.builder.position_at_end(done_bb)
            result = self.builder.phi(_I1, name=self._fresh("tup.in.result"))
            result.add_incoming(ir.Constant(_I1, 1), found_bb)
            result.add_incoming(ir.Constant(_I1, 0), miss_end)
            acc = result
            if owned:
                self._gc_unpin(lhs_obj)
        if owned and lhs_obj not in getattr(self, "_cpy_values", ()):
            self._gc_release(lhs_obj)
        if negate:
            return self.builder.not_(acc, name=self._fresh("tup.not_in"))
        return acc

    def _claim_boolexpr_pcc_operand(
        self,
        value: ir.Value,
        source: Expr,
        *,
        newly_owned: bool = False,
    ):
        """Give a managed Boolean operand exactly one expression owner.

        Physical boxing and the emitted-value ledger outrank AST shape. Raw
        ABI values and CPython pointers keep their existing ownership domain.
        The owner starts before truth testing: a user __bool__ can rebind the
        local/global/container which supplied a borrowed operand.
        """
        if getattr(self, "_freestanding_module", False) or (
            self._expr_returns_unsafe_raw_pointer(source)
        ):
            return value, False
        provenance = _method_pointer_provenance(
            self,
            value,
            _method_source_arg_type(self, source),
            source_expr=source,
            newly_owned=newly_owned or self._value_is_owned_object(value),
        )
        if not provenance[1]:
            return value, False
        if not provenance[3]:
            value = self._gc_retain(value, name=self._fresh("bool.operand.retain"))
        self._note_owned_object_value(value)
        return value, True

    def _truthy_boolexpr_operand(self, value, source_ty, pcc_owned: bool):
        """Keep the operand live through truth callbacks and their error edge."""
        if not pcc_owned:
            return self._truthy(value, source_ty)
        self._gc_pin(value)
        old_error = self._current_try_err_block()
        target = old_error if old_error is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (),
            (),
            target,
            "bool.truth.error",
            ((value, True),),
        )
        try:
            result = self._truthy(value, DynType(name="dyn"))
        finally:
            self._try_err_block = old_error
        self._gc_unpin(value)
        return result

    def _coerce_boolexpr_operand(self, value, source, result_ty, pcc_owned: bool):
        old_error = self._current_try_err_block()
        if pcc_owned:
            self._gc_pin(value)
            target = old_error if old_error is not None else self._ensure_fn_err_exit()
            self._try_err_block = self._make_cpy_operand_cleanup_block(
                (),
                (),
                target,
                "bool.coerce.error",
                ((value, True),),
            )
        try:
            coerced = self._coerce(value, source.ty, result_ty, source)
        finally:
            self._try_err_block = old_error
        if pcc_owned:
            self._gc_unpin(value)
            if isinstance(coerced.type, ir.PointerType):
                # Native pointer-to-pointer coercions preserve the object;
                # a representation alias transfers the same single owner.
                self._note_owned_object_value(coerced)
                return coerced, True
            # A scalar result no longer carries the source object's owner.
            self._gc_release(value)
            return coerced, False
        return self._claim_boolexpr_pcc_operand(
            coerced,
            source,
            newly_owned=(
                isinstance(coerced.type, ir.PointerType)
                and not isinstance(value.type, ir.PointerType)
            ),
        )

    def _bridge_boolexpr_operand(self, value, source, pcc_owned: bool):
        if not pcc_owned:
            return self._marshal_to_cpython_consuming_source(
                value,
                source.ty,
                source,
            )
        # This is a proven native object, including a borrowed Name promoted
        # above. The source-shape bridge cannot consume that new owner, and a
        # scalar-typed pointer must not take its foreign-pointer passthrough.
        self._gc_pin(value)
        converted, converted_owned = self._marshal_to_cpython(
            value,
            DynType(name="dyn"),
        )
        self._guard_cpy_value_not_null(
            converted,
            pinned_pcc_on_error=((value, True),),
        )
        self._gc_unpin(value)
        self._gc_release(value)
        return converted, converted_owned

    def _emit_boolexpr_pcc_object_operand(self, source: Expr):
        """Project one operand while preserving evidence of a newly made box."""
        if isinstance(source.ty, IntType):
            exact = self._maybe_emit_exact_int_object(source)
            if exact is not None:
                return self._claim_boolexpr_pcc_operand(exact, source)
        if isinstance(source, IfExpr):
            value = self._emit_if_expr_as_pcc_object(source)
            return self._claim_boolexpr_pcc_operand(value, source)
        if isinstance(source, BoolExpr):
            value = self._emit_boolexpr_as_pcc_object(source)
            return self._claim_boolexpr_pcc_operand(value, source)
        payload = self._maybe_emit_valueclass_constructor_payload(source.ty, source)
        if payload is not None:
            boxed = self._emit_valueclass_payload_to_object(
                payload,
                source.ty,
                consume_fields=True,
            )
            if boxed is not None:
                return self._claim_boolexpr_pcc_operand(
                    boxed,
                    source,
                    newly_owned=True,
                )
        value = self._emit_expr(source)
        source_is_cpy = value in getattr(self, "_cpy_values", ())
        boxed = self._emit_value_as_pcc_object_or_bridge(
            value,
            source.ty,
            "bool.obj.bridge",
        )
        return self._claim_boolexpr_pcc_operand(
            boxed,
            source,
            newly_owned=(
                source_is_cpy
                or not isinstance(value.type, ir.PointerType)
                or boxed is not value
            ),
        )

    def _emit_boolexpr(self, expr: BoolExpr) -> ir.Value:
        # Short-circuit via branch. ``and`` / ``or`` return either the
        # left operand or the right operand; only the pure-bool case
        # should collapse to i1.
        fn = self.current_function

        lhs = self._emit_expr(expr.left)
        lhs_is_cpy = lhs in getattr(self, "_cpy_values", ())
        lhs_owned = False
        lhs_pcc_owned = False
        if lhs_is_cpy:
            self._guard_cpy_value_not_null(lhs)
            lhs_owned = self._cpy_value_is_owned(lhs)
        else:
            lhs, lhs_pcc_owned = self._claim_boolexpr_pcc_operand(lhs, expr.left)
        lhs_b = self._truthy_boolexpr_operand(lhs, expr.left.ty, lhs_pcc_owned)
        result_ty = expr.ty
        lhs_val = None
        if not isinstance(result_ty, BoolType):
            lhs_val, lhs_pcc_owned = self._coerce_boolexpr_operand(
                lhs, expr.left, result_ty, lhs_pcc_owned,
            )
        elif lhs_is_cpy and lhs_owned:
            # Bool-typed and/or keeps only truthiness, never the operand ref.
            self.builder.call(self.runtime["py_cpy_decref"], [lhs])
            self._forget_owned_cpy_value(lhs)
        elif lhs_pcc_owned:
            self._gc_release(lhs)

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
            elif lhs_pcc_owned:
                self._gc_release(lhs_val)
            rhs = self._emit_expr(expr.right)
            rhs_is_cpy = rhs in getattr(self, "_cpy_values", ())
            rhs_owned = False
            rhs_pcc_owned = False
            if rhs_is_cpy:
                self._guard_cpy_value_not_null(rhs)
                rhs_owned = self._cpy_value_is_owned(rhs)
            else:
                rhs, rhs_pcc_owned = self._claim_boolexpr_pcc_operand(rhs, expr.right)
            rhs_val, rhs_pcc_owned = self._coerce_boolexpr_operand(
                rhs, expr.right, result_ty, rhs_pcc_owned,
            )
            rhs_exit = self.builder._block

            cpy_result = lhs_is_cpy or rhs_is_cpy
            if cpy_result:
                self.builder.position_at_end(short_exit)
                if not lhs_is_cpy:
                    short_val, short_owned = (
                        self._bridge_boolexpr_operand(
                            short_val,
                            expr.left,
                            lhs_pcc_owned,
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
                        self._bridge_boolexpr_operand(
                            rhs_val,
                            expr.right,
                            rhs_pcc_owned,
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
            if lhs_pcc_owned and rhs_pcc_owned:
                self._note_owned_object_value(phi)
            return phi

        self.builder.position_at_end(short_bb)
        self.builder.branch(end_bb)
        short_exit = self.builder._block

        self.builder.position_at_end(rhs_bb)
        rhs = self._emit_expr(expr.right)
        rhs_is_cpy = rhs in getattr(self, "_cpy_values", ())
        rhs_pcc_owned = False
        if rhs_is_cpy:
            self._guard_cpy_value_not_null(rhs)
        else:
            rhs, rhs_pcc_owned = self._claim_boolexpr_pcc_operand(rhs, expr.right)
        rhs_b = self._truthy_boolexpr_operand(rhs, expr.right.ty, rhs_pcc_owned)
        if rhs_is_cpy and self._cpy_value_is_owned(rhs):
            self.builder.call(self.runtime["py_cpy_decref"], [rhs])
            self._forget_owned_cpy_value(rhs)
        elif rhs_pcc_owned:
            self._gc_release(rhs)
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
            lhs_obj, lhs_owned = self._emit_boolexpr_pcc_object_operand(expr.left)
        finally:
            self._prefer_native_callable_values = old_prefer_native
        lhs_b = self._truthy_boolexpr_operand(lhs_obj, dyn_ty, lhs_owned)

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
        if lhs_owned:
            self._gc_release(lhs_obj)
        old_prefer_native = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            rhs_obj, rhs_owned = self._emit_boolexpr_pcc_object_operand(expr.right)
        finally:
            self._prefer_native_callable_values = old_prefer_native
        rhs_exit = self.builder._block
        self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh(f"{expr.op}.obj"))
        phi.add_incoming(lhs_obj, entry_bb)
        phi.add_incoming(rhs_obj, rhs_exit)
        if lhs_owned and rhs_owned:
            self._note_owned_object_value(phi)
        return phi
