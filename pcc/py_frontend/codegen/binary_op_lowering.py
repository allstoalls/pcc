"""Binary operation lowering helpers for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    Expr,
    FloatType,
    FuncType,
    IntType,
    ListType,
    MemoryViewType,
    SetType,
    StrType,
    TupleType,
    Type,
)
from . import marshal
from .errors import L1CodegenError


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = ir.IntType(8).as_pointer()


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


def _raw_int_name(ty: Type) -> str:
    name = getattr(ty, "name", "")
    if name == "pcc.i64" or name == "pcc.u64":
        return name
    return ""


class BinaryOpLoweringMixin:
    _INLINE_TAGGED_BINOPS = ("+", "-", "*", "&", "|", "^")

    def _binop_route_defers_pins(
        self,
        op: str,
        lhs_ty: Type,
        rhs_ty: Type,
        result_ty: Type,
    ) -> bool:
        """True when ``_emit_binop_value`` reaches an inline tagged-int route.

        Those two routes (``_emit_runtime_int_binop_value`` for boxed
        int/bool operands, ``_emit_dyn_tagged_int_object_binop`` for a
        DynType operand) pin operands only inside their slow block, so the
        caller must not pin them up front.  Mirrors the routing order of
        ``_emit_binop_value``: every earlier branch is keyed on a container,
        text, set, float or complex operand type, none of which may appear.
        """
        if op not in self._INLINE_TAGGED_BINOPS:
            return False
        numeric = (IntType, BoolType)
        if (
            self._int_exprs_are_boxed()
            and isinstance(result_ty, IntType)
            and isinstance(lhs_ty, numeric)
            and isinstance(rhs_ty, numeric)
        ):
            return True
        if not (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            return False
        for ty in (lhs_ty, rhs_ty, result_ty):
            if not isinstance(ty, (DynType, IntType, BoolType)):
                return False
        return True

    def _emit_binop_value(
        self,
        op: str,
        lhs: ir.Value,
        lhs_ty: Type,
        rhs: ir.Value,
        rhs_ty: Type,
        result_ty: Type,
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        slow_pins: tuple[ir.Value, ...] = (),
    ) -> ir.Value:
        """Lower ``lhs <op> rhs``; ``slow_pins`` are operands the caller left
        unpinned so that an inline tagged route pins them only around its
        slow call.  Any other route pins them around its whole lowering."""
        if not slow_pins or self._binop_route_defers_pins(op, lhs_ty, rhs_ty, result_ty):
            return self._emit_binop_value_routed(
                op, lhs, lhs_ty, rhs, rhs_ty, result_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )
        for pinned in slow_pins:
            self._gc_pin(pinned)
        result = self._emit_binop_value_routed(
            op, lhs, lhs_ty, rhs, rhs_ty, result_ty,
            pinned_pcc_on_error=pinned_pcc_on_error,
        )
        for pinned in reversed(slow_pins):
            self._gc_unpin(pinned)
        return result

    def _emit_binop_value_routed(
        self,
        op: str,
        lhs: ir.Value,
        lhs_ty: Type,
        rhs: ir.Value,
        rhs_ty: Type,
        result_ty: Type,
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        slow_pins: tuple[ir.Value, ...] = (),
    ) -> ir.Value:
        # CPython values (libpython mode): a binary operator where either
        # operand is a CPython object (e.g. numpy arrays, ``a + b``) must
        # dispatch through libpython's PyNumber_* (``py_cpy_binop``); the
        # native/object ops below do not handle real CPython objects and would
        # raise ``TypeError: unsupported operand``. The other operand is
        # marshalled to CPython (a cpy value stays borrowed; a native int/float
        # is boxed). Inert in no-libpython mode (``_cpy_values`` is empty).
        _cpy_vals = getattr(self, "_cpy_values", ())
        if _cpy_vals and (lhs in _cpy_vals or rhs in _cpy_vals):
            _cpy_op = {
                "+": 0, "-": 1, "*": 2, "/": 3, "//": 4, "%": 5, "**": 6,
                "@": 7,
            }.get(op)
            if _cpy_op is not None:
                rhs_original_owned = (
                    rhs in _cpy_vals and self._cpy_value_is_owned(rhs)
                )
                lhs_c, _l = self._marshal_to_cpython(lhs, lhs_ty)
                self._guard_cpy_value_not_null(
                    lhs_c,
                    (rhs,) if rhs_original_owned else (),
                    (),
                    pinned_pcc_on_error,
                )
                rhs_c, _r = self._marshal_to_cpython(rhs, rhs_ty)
                self._guard_cpy_value_not_null(
                    rhs_c,
                    (lhs_c,) if _l else (),
                    (),
                    pinned_pcc_on_error,
                )
                result = self.builder.call(
                    self.runtime["py_cpy_binop"],
                    [ir.Constant(ir.IntType(64), _cpy_op), lhs_c, rhs_c],
                    name=self._fresh("cpy.binop"),
                )
                if _l:
                    self.builder.call(self.runtime["py_cpy_decref"], [lhs_c])
                    self._forget_owned_cpy_value(lhs_c)
                if _r:
                    self.builder.call(self.runtime["py_cpy_decref"], [rhs_c])
                    self._forget_owned_cpy_value(rhs_c)
                self._mark_owned_cpy_value(result)
                self._guard_cpy_value_not_null(
                    result,
                    (),
                    (),
                    pinned_pcc_on_error,
                )
                return result
        # Phase 2 object ops (str concat / repeat, list concat). Keeping
        # the dispatch here lets augassign (``s += "x"``, ``lst += ...``)
        # take the same code path as the value-form expression.
        if op == "+" and isinstance(lhs_ty, StrType) and isinstance(rhs_ty, StrType):
            return self.builder.call(
                self.runtime["py_str_concat"],
                [lhs, rhs],
                name=self._fresh("str.concat"),
            )
        if op == "+" and (
            (
                isinstance(lhs_ty, (BytesType, ByteArrayType))
                and isinstance(rhs_ty, (BytesType, ByteArrayType, DynType))
            )
            or (
                isinstance(rhs_ty, (BytesType, ByteArrayType))
                and isinstance(lhs_ty, DynType)
            )
        ):
            result = self.builder.call(
                self.runtime["py_bytes_concat"],
                [lhs, rhs],
                name=self._fresh("bytes.concat"),
            )
            # The runtime returns a NEW bytes/bytearray. Register the actual
            # owner so assignment and discarded expressions consume it even
            # when their static result type is more precise than DynType.
            self._note_owned_object_value(result)
            return result
        if op == "%":
            numeric_static = isinstance(lhs_ty, (IntType, BoolType, FloatType)) and isinstance(
                rhs_ty, (IntType, BoolType, FloatType)
            )
            if not numeric_static:
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
                mod_res = self.builder.call(
                    self.runtime["py_obj_mod"],
                    [lhs_obj, rhs_obj],
                    name=self._fresh("obj.mod"),
                )
                # py_obj_mod sets TypeError on bad operands (err-check branches
                # those away); on an int zero divisor it returns NULL *without*
                # raising, so a surviving NULL → ZeroDivisionError.
                self._emit_post_call_err_check(None)
                self._emit_zero_division_if_null(
                    mod_res, "division by zero"
                )
                return mod_res
        if op == "%" and isinstance(lhs_ty, StrType):
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
            return self.builder.call(
                self.runtime["py_str_mod"],
                [lhs_obj, rhs_obj],
                name=self._fresh("str.mod"),
            )
        if (
            op == "*"
            and isinstance(lhs_ty, StrType)
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            rhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, rhs, rhs_ty
            )
            return self.builder.call(
                self.runtime["py_str_repeat"],
                [lhs, rhs_obj],
                name=self._fresh("str.rep"),
            )
        if (
            op == "*"
            and isinstance(rhs_ty, StrType)
            and isinstance(lhs_ty, (IntType, BoolType))
        ):
            lhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, lhs, lhs_ty
            )
            return self.builder.call(
                self.runtime["py_str_repeat"],
                [rhs, lhs_obj],
                name=self._fresh("str.rep"),
            )
        if (
            op == "*"
            and isinstance(lhs_ty, (BytesType, ByteArrayType))
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            return self.builder.call(
                self.runtime["py_bytes_repeat"],
                [lhs, self._to_int64(rhs, rhs_ty)],
                name=self._fresh("bytes.rep"),
            )
        if (
            op == "*"
            and isinstance(rhs_ty, (BytesType, ByteArrayType))
            and isinstance(lhs_ty, (IntType, BoolType))
        ):
            return self.builder.call(
                self.runtime["py_bytes_repeat"],
                [rhs, self._to_int64(lhs, lhs_ty)],
                name=self._fresh("bytes.rep"),
            )
        if (
            op == "*"
            and isinstance(lhs_ty, (BytesType, ByteArrayType))
            and isinstance(rhs_ty, DynType)
        ):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_bytes_repeat"],
                [lhs, n_i64],
                name=self._fresh("bytes.rep.dyn"),
            )
        if (
            op == "*"
            and isinstance(rhs_ty, (BytesType, ByteArrayType))
            and isinstance(lhs_ty, DynType)
        ):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                lhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_bytes_repeat"],
                [rhs, n_i64],
                name=self._fresh("bytes.rep.dyn"),
            )
        if (
            op == "*"
            and isinstance(lhs_ty, ListType)
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            n_i64 = self._to_int64(rhs, rhs_ty)
            return self.builder.call(
                self.runtime["py_list_repeat"],
                [lhs, n_i64],
                name=self._fresh("list.rep"),
            )
        if (
            op == "*"
            and isinstance(rhs_ty, ListType)
            and isinstance(lhs_ty, (IntType, BoolType))
        ):
            n_i64 = self._to_int64(lhs, lhs_ty)
            return self.builder.call(
                self.runtime["py_list_repeat"],
                [rhs, n_i64],
                name=self._fresh("list.rep"),
            )
        # Narrow fallback: ``ListType * DynType`` where the DynType
        # payload is a runtime integer. Unbox the Dyn to i64 and
        # route through py_list_repeat. Covers ``[x] * some_dyn``
        # where typing didn't pin DynType as IntType.
        if op == "*" and isinstance(lhs_ty, ListType) and isinstance(rhs_ty, DynType):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_list_repeat"],
                [lhs, n_i64],
                name=self._fresh("list.rep.dyn"),
            )
        if op == "*" and isinstance(rhs_ty, ListType) and isinstance(lhs_ty, DynType):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                lhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_list_repeat"],
                [rhs, n_i64],
                name=self._fresh("list.rep.dyn"),
            )
        if (
            op == "*"
            and isinstance(lhs_ty, TupleType)
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            return self.builder.call(
                self.runtime["py_tuple_repeat"],
                [lhs, self._to_int64(rhs, rhs_ty)],
                name=self._fresh("tup.rep"),
            )
        if (
            op == "*"
            and isinstance(rhs_ty, TupleType)
            and isinstance(lhs_ty, (IntType, BoolType))
        ):
            return self.builder.call(
                self.runtime["py_tuple_repeat"],
                [rhs, self._to_int64(lhs, lhs_ty)],
                name=self._fresh("tup.rep"),
            )
        if op == "*" and isinstance(lhs_ty, TupleType) and isinstance(rhs_ty, DynType):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_tuple_repeat"],
                [lhs, n_i64],
                name=self._fresh("tup.rep.dyn"),
            )
        if op == "*" and isinstance(rhs_ty, TupleType) and isinstance(lhs_ty, DynType):
            n_i64 = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                lhs,
                IntType(name="int"),
            )
            return self.builder.call(
                self.runtime["py_tuple_repeat"],
                [rhs, n_i64],
                name=self._fresh("tup.rep.dyn"),
            )
        if op == "+" and (
            (isinstance(lhs_ty, ListType) and isinstance(rhs_ty, (ListType, DynType)))
            or (isinstance(rhs_ty, ListType) and isinstance(lhs_ty, DynType))
        ):
            # ListType + (ListType | DynType) or DynType + ListType —
            # ``py_list_concat`` accepts any pcc PyObject* and builds a
            # new list. The Dyn side is trusted to be list-shaped at
            # runtime (mirrors CPython's ``+`` which would raise at
            # runtime for a non-list).
            return self.builder.call(
                self.runtime["py_list_concat"],
                [lhs, rhs],
                name=self._fresh("list.concat"),
            )
        if op == "+" and (
            (
                isinstance(lhs_ty, TupleType)
                and isinstance(rhs_ty, (TupleType, DynType, ClassType))
            )
            or (
                isinstance(rhs_ty, TupleType)
                and isinstance(lhs_ty, (DynType, ClassType))
            )
        ):
            # TupleType + (TupleType | DynType) stays native. The DynType
            # side is trusted to be tuple-shaped at runtime, mirroring the
            # existing ListType + DynType fast path above.
            return self.builder.call(
                self.runtime["py_tuple_concat"],
                [lhs, rhs],
                name=self._fresh("tup.concat"),
            )

        if op == "@":
            # Python's matrix-multiply operator is a normal data-model
            # protocol. Keep this generic: lower to ``lhs.__matmul__(rhs)``
            # through pcc-native object getattr/call rather than adding any
            # package-specific array handling.
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
            return self.builder.call(
                self.runtime["py_user_matmul_dispatch"],
                [lhs_obj, rhs_obj],
                name=self._fresh("obj.matmul"),
            )

        if op == "+" and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            return self._emit_dyn_tagged_int_object_binop(
                op, lhs, lhs_ty, rhs, rhs_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )

        if op == "/" and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            # True division on a dynamically-typed operand: a DynType value may
            # box an int/float at runtime, so it cannot use the static
            # _to_double + fdiv path below. py_obj_truediv divides numeric
            # operands as doubles (always yielding a float) and defers
            # non-numeric operands to the __truediv__ dunder.
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
            td_res = self.builder.call(
                self.runtime["py_obj_truediv"],
                [lhs_obj, rhs_obj],
                name=self._fresh("obj.truediv"),
            )
            # py_obj_truediv returns NULL + sets ZeroDivisionError on a zero
            # divisor; branch so ``try/except ZeroDivisionError`` can catch it.
            self._emit_post_call_err_check(None)
            return td_res

        if (
            op in ("|", "&", "-", "^")
            and (
                isinstance(lhs_ty, SetType)
                and isinstance(rhs_ty, (SetType, DynType))
                or isinstance(rhs_ty, SetType)
                and isinstance(lhs_ty, (SetType, DynType))
            )
        ):
            lhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, lhs, lhs_ty
            )
            rhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, rhs, rhs_ty
            )
            return self._emit_checked_set_binary_values(
                op, lhs_obj, rhs_obj, None
            )

        # Generic DynType `-` / `*`, mirroring the `+` (py_obj_add) and `/`
        # (py_obj_truediv) paths above. Placed AFTER the native-set block so set
        # difference (`set - set`) and the list/tuple repetition paths (handled
        # earlier) still win. py_obj_sub / py_obj_mul dispatch by runtime tag
        # (int->py_int_*, float->py_float_* which coerces the other numeric
        # operand, seq*int->repetition for mul). Without this, a boxed-float
        # DynType operand (e.g. ``obj.attr - n`` / ``obj.attr * n`` where attr
        # is a float) fell to the i64 path and misread the boxed pointer.
        if op == "-" and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            return self._emit_dyn_tagged_int_object_binop(
                op, lhs, lhs_ty, rhs, rhs_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )

        if op == "//" and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            lhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, lhs, lhs_ty
            )
            rhs_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, rhs, rhs_ty
            )
            fdiv_res = self.builder.call(
                self.runtime["py_obj_floordiv"],
                [lhs_obj, rhs_obj],
                name=self._fresh("obj.floordiv"),
            )
            # py_obj_floordiv raises (TypeError / user dunder errors) for most
            # error cases, but INT // INT delegates to py_int_floordiv, which
            # returns NULL *without* raising on a zero divisor (the raise is
            # deferred to the caller, as for py_obj_mod). So a surviving NULL
            # after the error check is a zero divisor → ZeroDivisionError.
            self._emit_post_call_err_check(None)
            self._emit_zero_division_if_null(fdiv_res, "division by zero")
            return fdiv_res

        if op == "*" and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            return self._emit_dyn_tagged_int_object_binop(
                op, lhs, lhs_ty, rhs, rhs_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )

        if (
            op == "|"
            and (
                isinstance(lhs_ty, (DictType, DynType))
                and isinstance(rhs_ty, (DictType, DynType))
            )
            and (isinstance(lhs_ty, DictType) or isinstance(rhs_ty, DictType))
        ):
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
            out = self.builder.call(
                self.runtime["py_dict_new"],
                [],
                name=self._fresh("dict.union"),
            )
            self.builder.call(
                self.runtime["py_dict_update"],
                [out, lhs_obj],
                name=self._fresh("dict.union.left"),
            )
            self.builder.call(
                self.runtime["py_dict_update"],
                [out, rhs_obj],
                name=self._fresh("dict.union.right"),
            )
            return out

        if op == "|" and (
            isinstance(lhs_ty, (ClassType, FuncType))
            or isinstance(rhs_ty, (ClassType, FuncType))
        ):
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
            out = self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, 2)],
                name=self._fresh("type.union"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, 0), lhs_obj],
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, 1), rhs_obj],
            )
            return out

        if op in ("&", "|", "^", "<<", ">>") and (
            isinstance(result_ty, DynType)
            or isinstance(lhs_ty, DynType)
            or isinstance(rhs_ty, DynType)
        ):
            return self._emit_dyn_tagged_int_object_binop(
                op, lhs, lhs_ty, rhs, rhs_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )

        _complex_binop = {
            "+": "py_complex_add",
            "-": "py_complex_sub",
            "*": "py_complex_mul",
            "/": "py_complex_div",
            # ``**`` routes to a dedicated runtime helper that mirrors
            # CPython's ``_Py_c_pow`` (integer fast path + exp/log/cos/sin
            # general path). Without this the complex operand would fall
            # through to ``_to_int64`` below and raise PCC-PY-COMPILE-001
            # "cannot coerce ComplexType to int" at compile time.
            "**": "py_complex_pow",
        }.get(op)
        if _complex_binop is not None and (
            isinstance(lhs_ty, ComplexType)
            or isinstance(rhs_ty, ComplexType)
            or isinstance(result_ty, ComplexType)
        ):
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
            result = self.builder.call(
                self.runtime[_complex_binop],
                [lhs_obj, rhs_obj],
                name=self._fresh("complex.binop"),
            )
            # py_complex_div and py_complex_pow both raise ZeroDivisionError
            # on a zero divisor / zero-to-negative-or-complex-power.
            if op in ("/", "**"):
                self._emit_post_call_err_check(None)
            return result

        if (
            self._int_exprs_are_boxed()
            and isinstance(result_ty, IntType)
            and isinstance(lhs_ty, (IntType, BoolType))
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            return self._emit_runtime_int_binop_value(
                op,
                lhs,
                lhs_ty,
                rhs,
                rhs_ty,
                pinned_pcc_on_error=pinned_pcc_on_error,
                slow_pins=slow_pins,
            )

        if (
            getattr(self, "_freestanding_module", False)
            and isinstance(result_ty, IntType)
            and result_ty.name == "int"
            and isinstance(lhs_ty, (IntType, BoolType))
            and isinstance(rhs_ty, (IntType, BoolType))
        ):
            raise RuntimeError(
                "freestanding ordinary Python int arithmetic cannot preserve "
                "arbitrary precision; annotate the machine boundary with "
                "pcc.i64 or pcc.u64"
            )

        # Shortcut: bitwise ops + shifts are integer-only.
        if op in ("&", "|", "^", "<<", ">>"):
            lv = self._to_int64(lhs, lhs_ty)
            rv = self._to_int64(rhs, rhs_ty)
            if op == "&":
                return self.builder.and_(lv, rv, name=self._fresh("and"))
            if op == "|":
                return self.builder.or_(lv, rv, name=self._fresh("or"))
            if op == "^":
                return self.builder.xor(lv, rv, name=self._fresh("xor"))
            raw_name = _raw_int_name(result_ty)
            if raw_name:
                # Explicit machine lanes use the target's modulo-64 shift
                # count instead of Python's unbounded shift semantics.
                if (
                    isinstance(rv, ir.Constant)
                    and isinstance(rv.value, int)
                ):
                    rv = ir.Constant(_I64, rv.value & 63)
                else:
                    rv = self.builder.and_(
                        rv,
                        ir.Constant(_I64, 63),
                        name=self._fresh("raw.shift.count"),
                    )
            else:
                self._emit_negative_shift_count_check(rv)
            if op == "<<":
                return self.builder.shl(lv, rv, name=self._fresh("shl"))
            if op == ">>":
                if raw_name == "pcc.u64":
                    return self.builder.lshr(
                        lv,
                        rv,
                        name=self._fresh("lshr"),
                    )
                return self.builder.ashr(lv, rv, name=self._fresh("ashr"))

        # Python ``/`` always returns float even if both operands are
        # integers.
        if op == "/":
            lf = self._to_double(lhs, lhs_ty)
            rf = self._to_double(rhs, rhs_ty)
            rf_is_zero = self.builder.fcmp_ordered(
                "==", rf, ir.Constant(_DOUBLE, 0.0), name=self._fresh("td_bz")
            )
            self._emit_zero_division_check(rf_is_zero, "division by zero")
            return self.builder.fdiv(lf, rf, name=self._fresh("fdiv"))

        # Pick the result's IR type: float if either operand is float.
        if isinstance(lhs_ty, FloatType) or isinstance(rhs_ty, FloatType):
            lf = self._to_double(lhs, lhs_ty)
            rf = self._to_double(rhs, rhs_ty)
            return self._emit_binop_float(op, lf, rf)

        # String ops: ``s * n`` / ``n * s`` → ``py_str_repeat``;
        # ``s + t`` → ``py_str_concat``. Any Dyn operand is boxed
        # via the marshal helper so the runtime's py_str_* helpers
        # see PyObject*.
        if op == "*" and (isinstance(lhs_ty, StrType) or isinstance(rhs_ty, StrType)):
            if isinstance(lhs_ty, StrType):
                s_val, s_ty = lhs, lhs_ty
                n_val, n_ty = rhs, rhs_ty
            else:
                s_val, s_ty = rhs, rhs_ty
                n_val, n_ty = lhs, lhs_ty
            s_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                s_val,
                s_ty,
            )
            n_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                n_val,
                n_ty,
            )
            return self.builder.call(
                self.runtime["py_str_repeat"],
                [s_obj, n_obj],
                name=self._fresh("str.repeat"),
            )
        if op == "+" and (
            (isinstance(lhs_ty, StrType) or isinstance(rhs_ty, StrType))
            and (
                isinstance(lhs_ty, (StrType, DynType))
                and isinstance(rhs_ty, (StrType, DynType))
            )
        ):
            l_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                lhs,
                lhs_ty,
            )
            r_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                rhs_ty,
            )
            return self.builder.call(
                self.runtime["py_str_concat"],
                [l_obj, r_obj],
                name=self._fresh("str.concat"),
            )

        # Integer (and bool-as-int) path.
        lv = self._to_int64(lhs, lhs_ty)
        rv = self._to_int64(rhs, rhs_ty)
        return self._emit_binop_int(op, lv, rv, result_ty)

    def _emit_raw_int_division_guard(
        self,
        lhs: ir.Value,
        rhs: ir.Value,
        *,
        signed: bool,
    ) -> None:
        """Trap deterministic raw divide UB without managed-runtime edges."""
        zero = ir.Constant(_I64, 0)
        invalid = self.builder.icmp_unsigned(
            "==",
            rhs,
            zero,
            name=self._fresh("raw.div.zero"),
        )
        if signed:
            lhs_min = self.builder.icmp_signed(
                "==",
                lhs,
                ir.Constant(_I64, -(1 << 63)),
                name=self._fresh("raw.div.min"),
            )
            rhs_neg_one = self.builder.icmp_signed(
                "==",
                rhs,
                ir.Constant(_I64, -1),
                name=self._fresh("raw.div.negone"),
            )
            overflow = self.builder.and_(
                lhs_min,
                rhs_neg_one,
                name=self._fresh("raw.div.overflow"),
            )
            invalid = self.builder.or_(
                invalid,
                overflow,
                name=self._fresh("raw.div.invalid"),
            )
        trap_bb = self.current_function.append_basic_block(
            name=self._fresh("raw.div.trap")
        )
        cont_bb = self.current_function.append_basic_block(
            name=self._fresh("raw.div.cont")
        )
        self.builder.cbranch(invalid, trap_bb, cont_bb)
        self.builder.position_at_end(trap_bb)
        trap = self.module.globals.get("llvm.trap")
        if not isinstance(trap, ir.Function):
            trap = ir.Function(
                self.module,
                ir.FunctionType(ir.VoidType(), []),
                name="llvm.trap",
            )
        self.builder.call(trap, [])
        self.builder.unreachable()
        self.builder.position_at_end(cont_bb)

    def _emit_binop_int(
        self,
        op: str,
        lv: ir.Value,
        rv: ir.Value,
        result_ty: Type,
    ) -> ir.Value:
        raw_name = _raw_int_name(result_ty)
        if op == "+":
            return self.builder.add(lv, rv, name=self._fresh("add"))
        if op == "-":
            return self.builder.sub(lv, rv, name=self._fresh("sub"))
        if op == "*":
            return self.builder.mul(lv, rv, name=self._fresh("mul"))
        if op == "//":
            if raw_name:
                signed = raw_name == "pcc.i64"
                self._emit_raw_int_division_guard(lv, rv, signed=signed)
                if signed:
                    return self.builder.sdiv(
                        lv,
                        rv,
                        name=self._fresh("raw.sdiv"),
                    )
                return self.builder.udiv(
                    lv,
                    rv,
                    name=self._fresh("raw.udiv"),
                )
            return self._python_floordiv_i64(lv, rv)
        if op == "%":
            if raw_name:
                signed = raw_name == "pcc.i64"
                self._emit_raw_int_division_guard(lv, rv, signed=signed)
                if signed:
                    return self.builder.srem(
                        lv,
                        rv,
                        name=self._fresh("raw.srem"),
                    )
                return self.builder.urem(
                    lv,
                    rv,
                    name=self._fresh("raw.urem"),
                )
            return self._python_mod_i64(lv, rv)
        if op == "**":
            if raw_name:
                raise L1CodegenError(
                    "raw pcc.i64/pcc.u64 exponentiation is not yet supported; "
                    "use an explicit checked machine helper"
                )
            # Route through the runtime ``py_int_pow`` helper. Both
            # operands box first, then unbox the result back to i64.
            lbox = self.builder.call(
                self.runtime["py_int_from_i64"],
                [lv],
                name=self._fresh("pow.l"),
            )
            rbox = self.builder.call(
                self.runtime["py_int_from_i64"],
                [rv],
                name=self._fresh("pow.r"),
            )
            pow_obj = self.builder.call(
                self.runtime["py_int_pow"],
                [lbox, rbox],
                name=self._fresh("int.pow"),
            )
            return marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                pow_obj,
                IntType(name="int"),
            )
        raise NotImplementedError(f"Layer 1 int binop {op!r} not supported")
    def _emit_runtime_int_binop_value(
        self,
        op: str,
        lhs: ir.Value,
        lhs_ty: Type,
        rhs: ir.Value,
        rhs_ty: Type,
        *,
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        slow_pins: tuple[ir.Value, ...] = (),
    ) -> ir.Value:
        fn_name = {
            "+": "py_int_add",
            "-": "py_int_sub",
            "*": "py_int_mul",
            "//": "py_int_floordiv",
            "%": "py_int_mod",
            "**": "py_int_pow",
            "&": "py_int_and",
            "|": "py_int_or",
            "^": "py_int_xor",
            "<<": "py_int_shl",
            ">>": "py_int_shr",
        }.get(op)
        if fn_name is None:
            raise NotImplementedError(f"Layer 1 int binop {op!r} not supported")
        if op == "<<" or op == ">>":
            self._emit_negative_shift_count_check(
                self._to_int64(rhs, rhs_ty),
                pinned_release_on_error=pinned_pcc_on_error,
            )
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
        inline = self._emit_inline_tagged_int_binop_or_call(
            op,
            lhs_obj,
            rhs_obj,
            fn_name,
            slow_pins=slow_pins,
            slow_err_check=True,
            slow_err_cleanup=pinned_pcc_on_error,
        )
        if inline is not None:
            return inline
        for pinned in slow_pins:
            self._gc_pin(pinned)
        result = self.builder.call(
            self.runtime[fn_name],
            [lhs_obj, rhs_obj],
            name=self._fresh("int.obj"),
        )
        self._emit_post_call_err_check(
            None,
            pinned_release_on_error=pinned_pcc_on_error,
        )
        if op == "//" or op == "%":
            # py_int_floordiv / py_int_mod return NULL (no exception) on a zero
            # divisor; surface ZeroDivisionError so user try/except can catch it.
            self._emit_zero_division_if_null(
                result,
                "division by zero",
                pinned_release_on_error=pinned_pcc_on_error,
            )
        else:
            self._guard_cpy_value_not_null(
                result,
                pinned_pcc_on_error=pinned_pcc_on_error,
            )
        for pinned in reversed(slow_pins):
            self._gc_unpin(pinned)
        return result
    def _emit_negative_shift_count_check(
        self,
        rv: ir.Value,
        *,
        pinned_release_on_error: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> None:
        # A statically non-negative count cannot take Python's ValueError
        # edge.  Besides avoiding dead control flow, this is required by the
        # freestanding subset: fixed ABI bitfield extraction must not acquire
        # a managed exception-runtime dependency merely because Python shifts
        # reject dynamic negative counts.
        if (
            isinstance(rv, ir.Constant)
            and isinstance(rv.type, ir.IntType)
            and isinstance(rv.value, int)
            and rv.value >= 0
        ):
            return
        fn = self.current_function
        if fn is None:
            raise L1CodegenError("shift lowering requires an active function")
        is_negative = self.builder.icmp_signed(
            "<",
            rv,
            ir.Constant(_I64, 0),
            name=self._fresh("shift.neg"),
        )
        ok_bb = fn.append_basic_block(
            name=self._fresh("shift.ok"),
        )
        err_bb = fn.append_basic_block(
            name=self._fresh("shift.err"),
        )
        self.builder.cbranch(is_negative, err_bb, ok_bb)
        self.builder.position_at_end(err_bb)
        for value, release_owned in pinned_release_on_error:
            self._gc_unpin(value)
            if release_owned:
                self._gc_release(value)
        self._emit_builtin_exception_and_branch(
            "ValueError",
            "negative shift count",
            None,
        )
        self.builder.position_at_end(ok_bb)
    def _emit_zero_division_check(
        self,
        is_zero: ir.Value,
        msg: str,
        *,
        pinned_release_on_error: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> None:
        """Raise ZeroDivisionError when ``is_zero`` (an i1) is true.

        Python raises ``ZeroDivisionError`` for ``/``, ``//`` and ``%`` with a
        zero divisor; the unboxed ``sdiv``/``srem``/``fdiv`` lowerings have no
        such trap (signed integer division by zero is UB — ARM64 SDIV silently
        yields 0 — and float division yields inf), so the divisor must be
        checked explicitly before the operation.
        """
        fn = self.current_function
        if fn is None:
            raise L1CodegenError("division lowering requires an active function")
        ok_bb = fn.append_basic_block(name=self._fresh("div.ok"))
        err_bb = fn.append_basic_block(name=self._fresh("div.zero"))
        self.builder.cbranch(is_zero, err_bb, ok_bb)
        self.builder.position_at_end(err_bb)
        for value, release_owned in pinned_release_on_error:
            self._gc_unpin(value)
            if release_owned:
                self._gc_release(value)
        self._emit_builtin_exception_and_branch(
            "ZeroDivisionError",
            msg,
            None,
        )
        self.builder.position_at_end(ok_bb)
    def _emit_zero_division_if_null(
        self,
        result: ir.Value,
        msg: str,
        *,
        pinned_release_on_error: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> None:
        """Raise ZeroDivisionError when a boxed ``//``/``%`` result is NULL.

        ``py_int_mod`` / ``py_int_floordiv`` (and ``py_obj_mod`` which delegates
        to them) return NULL *without* setting an exception when the divisor is
        zero — the runtime comment defers the raise to the caller. So after the
        usual ``py_err_occurred`` check has filtered out the genuinely-raised
        cases (TypeError on bad operands, etc.), a remaining NULL reliably means
        integer division/modulo by zero.
        """
        is_null = self.builder.icmp_signed(
            "==",
            result,
            ir.Constant(result.type, None),
            name=self._fresh("divres_null"),
        )
        self._emit_zero_division_check(
            is_null,
            msg,
            pinned_release_on_error=pinned_release_on_error,
        )
    def _emit_dyn_tagged_int_object_binop(
        self,
        op: str,
        lhs: ir.Value,
        lhs_ty: Type,
        rhs: ir.Value,
        rhs_ty: Type,
        *,
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        slow_pins: tuple[ir.Value, ...] = (),
    ) -> ir.Value:
        """Lower a DynType integer/object binop with one fast/slow contract.

        ``slow_pins`` are pinned only around the ``py_obj_*`` slow call (the
        inline tagged fast path cannot move or raise); ``pinned_pcc_on_error``
        is released on the slow call's error edge."""
        runtime_fn = {
            "+": "py_obj_add",
            "-": "py_obj_sub",
            "*": "py_obj_mul",
            "&": "py_obj_and",
            "|": "py_obj_or",
            "^": "py_obj_xor",
            "<<": "py_obj_lshift",
            ">>": "py_obj_rshift",
        }.get(op)
        if runtime_fn is None:
            raise L1CodegenError(f"unsupported DynType tagged binop: {op}")

        lhs_obj = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, lhs, lhs_ty
        )
        rhs_obj = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, rhs, rhs_ty
        )
        # The py_obj_* slow paths can raise through user dunders or unsupported
        # operands; the inline tagged fast path cannot, so the check lives in
        # the slow block (an enclosing try/except still observes it).
        result = self._emit_inline_tagged_int_binop_or_call(
            op,
            lhs_obj,
            rhs_obj,
            runtime_fn,
            slow_pins=slow_pins,
            slow_err_check=True,
            slow_err_cleanup=pinned_pcc_on_error,
        )
        if result is None:
            for pinned in slow_pins:
                self._gc_pin(pinned)
            result = self.builder.call(
                self.runtime[runtime_fn],
                [lhs_obj, rhs_obj],
                name=self._fresh("obj.binop"),
            )
            self._emit_post_call_err_check(
                None,
                pinned_release_on_error=pinned_pcc_on_error,
            )
            for pinned in reversed(slow_pins):
                self._gc_unpin(pinned)
        return result

    def _emit_inline_tagged_int_binop_or_call(
        self,
        op: str,
        lhs_obj: ir.Value,
        rhs_obj: ir.Value,
        fn_name: str,
        *,
        slow_pins: tuple[ir.Value, ...] = (),
        slow_err_check: bool = False,
        slow_err_cleanup: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value | None:
        """Inline tagged fast path with the runtime call as the slow path.

        ``slow_pins`` are pinned only around the slow call (the fast path
        never leaves this function, so nothing can relocate its operands),
        and with ``slow_err_check`` the ``py_err_occurred`` probe and the
        NULL guard are emitted inside the slow block instead of after the
        join: the fast path cannot raise.  Measured on ``total += i``: the
        join-side check and the unconditional pins were part of a 1288
        instruction loop iteration (CPython: 718).
        """
        if op not in ("+", "-", "*", "&", "|", "^"):
            return None

        ptr_one = ir.Constant(_I64, 1)
        lhs_bits = self.builder.ptrtoint(
            lhs_obj,
            _I64,
            name=self._fresh("tag.l.bits"),
        )
        rhs_bits = self.builder.ptrtoint(
            rhs_obj,
            _I64,
            name=self._fresh("tag.r.bits"),
        )
        lhs_tag = self.builder.icmp_signed(
            "==",
            self.builder.and_(
                lhs_bits,
                ptr_one,
                name=self._fresh("tag.l.low"),
            ),
            ptr_one,
            name=self._fresh("tag.l.ok"),
        )
        rhs_tag = self.builder.icmp_signed(
            "==",
            self.builder.and_(
                rhs_bits,
                ptr_one,
                name=self._fresh("tag.r.low"),
            ),
            ptr_one,
            name=self._fresh("tag.r.ok"),
        )
        both_tagged = self.builder.and_(
            lhs_tag,
            rhs_tag,
            name=self._fresh("tag.both"),
        )

        fn = self.current_function
        if fn is None:
            return None
        fast_bb = fn.append_basic_block(
            name=self._fresh("int.tag.fast"),
        )
        slow_bb = fn.append_basic_block(
            name=self._fresh("int.tag.slow"),
        )
        join_bb = fn.append_basic_block(
            name=self._fresh("int.tag.join"),
        )
        self.builder.cbranch(both_tagged, fast_bb, slow_bb)

        self.builder.position_at_end(fast_bb)
        lhs_val = self.builder.ashr(
            lhs_bits,
            ptr_one,
            name=self._fresh("tag.l.val"),
        )
        rhs_val = self.builder.ashr(
            rhs_bits,
            ptr_one,
            name=self._fresh("tag.r.val"),
        )
        if op == "+":
            raw = self.builder.add(
                lhs_val,
                rhs_val,
                name=self._fresh("tag.add"),
            )
        elif op == "-":
            raw = self.builder.sub(
                lhs_val,
                rhs_val,
                name=self._fresh("tag.sub"),
            )
        elif op == "*":
            pair_type = ir.LiteralStructType([_I64, _I1])
            intrinsic_name = "llvm.smul.with.overflow.i64"
            intrinsic = self.module.globals.get(intrinsic_name)
            if intrinsic is None:
                intrinsic = ir.Function(
                    self.module,
                    ir.FunctionType(pair_type, [_I64, _I64]),
                    name=intrinsic_name,
                )
            pair = self.builder.call(
                intrinsic,
                [lhs_val, rhs_val],
                name=self._fresh("tag.mul.ov"),
            )
            raw = self.builder.extract_value(
                pair,
                [0],
                name=self._fresh("tag.mul"),
            )
            mul_overflow = self.builder.extract_value(
                pair,
                [1],
                name=self._fresh("tag.mul.overflow"),
            )
        elif op == "&":
            raw = self.builder.and_(
                lhs_val,
                rhs_val,
                name=self._fresh("tag.and"),
            )
        elif op == "|":
            raw = self.builder.or_(
                lhs_val,
                rhs_val,
                name=self._fresh("tag.or"),
            )
        else:
            raw = self.builder.xor(
                lhs_val,
                rhs_val,
                name=self._fresh("tag.xor"),
            )
        if op != "*":
            mul_overflow = None

        if op in ("+", "-", "*"):
            min_tagged = ir.Constant(_I64, -(1 << 62))
            max_tagged = ir.Constant(_I64, (1 << 62) - 1)
            ge_min = self.builder.icmp_signed(
                ">=",
                raw,
                min_tagged,
                name=self._fresh("tag.ge_min"),
            )
            le_max = self.builder.icmp_signed(
                "<=",
                raw,
                max_tagged,
                name=self._fresh("tag.le_max"),
            )
            fits = self.builder.and_(
                ge_min,
                le_max,
                name=self._fresh("tag.fits"),
            )
            if mul_overflow is not None:
                no_overflow = self.builder.not_(
                    mul_overflow,
                    name=self._fresh("tag.mul.no_overflow"),
                )
                fits = self.builder.and_(
                    no_overflow,
                    fits,
                    name=self._fresh("tag.mul.fits"),
                )
            tag_bb = fn.append_basic_block(
                name=self._fresh("int.tag.pack"),
            )
            self.builder.cbranch(fits, tag_bb, slow_bb)
            self.builder.position_at_end(tag_bb)

        tag_bits = self.builder.or_(
            self.builder.shl(
                raw,
                ptr_one,
                name=self._fresh("tag.shift"),
            ),
            ptr_one,
            name=self._fresh("tag.bits"),
        )
        fast_result = self.builder.inttoptr(
            tag_bits,
            _CSTR,
            name=self._fresh("tag.ptr"),
        )
        fast_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(slow_bb)
        for pinned in slow_pins:
            self._gc_pin(pinned)
        slow_result = self.builder.call(
            self.runtime[fn_name],
            [lhs_obj, rhs_obj],
            name=self._fresh("int.obj"),
        )
        if slow_err_check:
            self._emit_post_call_err_check(
                None,
                pinned_release_on_error=slow_err_cleanup,
            )
            self._guard_cpy_value_not_null(
                slow_result,
                pinned_pcc_on_error=slow_err_cleanup,
            )
        for pinned in reversed(slow_pins):
            self._gc_unpin(pinned)
        slow_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh("int.tag.result"))
        phi.add_incoming(fast_result, fast_exit)
        phi.add_incoming(slow_result, slow_exit)
        return phi
    def _emit_binop_float(self, op: str, lv: ir.Value, rv: ir.Value) -> ir.Value:
        if op == "+":
            return self.builder.fadd(lv, rv, name=self._fresh("fadd"))
        if op == "-":
            return self.builder.fsub(lv, rv, name=self._fresh("fsub"))
        if op == "*":
            return self.builder.fmul(lv, rv, name=self._fresh("fmul"))
        if op == "//":
            # Python float-floor div: floor(a / b). ``x // 0.0`` raises
            # ZeroDivisionError (fdiv would otherwise yield inf/nan).
            self._emit_zero_division_check(
                self.builder.fcmp_ordered(
                    "==", rv, ir.Constant(_DOUBLE, 0.0),
                    name=self._fresh("ffloor_bz"),
                ),
                "division by zero",
            )
            q = self.builder.fdiv(lv, rv, name=self._fresh("fdiv_q"))
            # Inline llvm.floor.f64 intrinsic.
            floor_fn = self._get_floor_intrinsic()
            return self.builder.call(floor_fn, [q], name=self._fresh("ffloor"))
        if op == "%":
            # ``x % 0.0`` raises ZeroDivisionError (fmod would yield nan).
            self._emit_zero_division_check(
                self.builder.fcmp_ordered(
                    "==", rv, ir.Constant(_DOUBLE, 0.0),
                    name=self._fresh("fmod_bz"),
                ),
                "division by zero",
            )
            # Python float mod uses fmod + correction; simplest is to
            # call libc ``fmod`` and adjust sign.
            fmod_fn = self._get_fmod_function()
            r = self.builder.call(fmod_fn, [lv, rv], name=self._fresh("fmod"))
            # Correct sign: if (r != 0) and (sign(r) != sign(b)) → r += b.
            zero_f = ir.Constant(_DOUBLE, 0.0)
            r_nz = self.builder.fcmp_ordered(
                "!=", r, zero_f, name=self._fresh("fmod_nz")
            )
            r_neg = self.builder.fcmp_ordered(
                "<", r, zero_f, name=self._fresh("fmod_r_neg")
            )
            b_neg = self.builder.fcmp_ordered(
                "<", rv, zero_f, name=self._fresh("fmod_b_neg")
            )
            sign_diff = self.builder.xor(
                r_neg, b_neg, name=self._fresh("fmod_sign_diff")
            )
            need_fix = self.builder.and_(r_nz, sign_diff, name=self._fresh("fmod_fix"))
            corrected = self.builder.fadd(r, rv, name=self._fresh("fmod_corr"))
            return self.builder.select(
                need_fix, corrected, r, name=self._fresh("fmod_res")
            )
        if op == "**":
            pow_fn = self._get_pow_function()
            return self.builder.call(pow_fn, [lv, rv], name=self._fresh("fpow"))
        raise NotImplementedError(f"Layer 1 float binop {op!r} not supported")
