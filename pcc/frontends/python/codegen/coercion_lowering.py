"""Scalar coercion and truthiness helpers for Layer-1 codegen."""

from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    RawPointerType,
    BoolType,
    ClassType,
    DynType,
    FloatType,
    IntType,
    NoneType,
    Type,
    ValueArrayType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.raw_pointer_provenance import raw_abi_expression_provenance
from pcc.frontends.python.codegen.errors import L1CodegenError

_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()


def _same_type_kind(a: Type, b: Type) -> bool:
    return getattr(a, "name", None) == getattr(b, "name", None)


def _type_name(ty: Type) -> str:
    name = getattr(ty, "name", "")
    if name is None:
        return ""
    return name


def _ir_type_is_pointer(ty) -> bool:
    if isinstance(ty, ir.PointerType):
        return True
    try:
        ty.pointee
        return True
    except AttributeError:
        return False


class CoercionLoweringMixin:
    def _to_int64(self, v: ir.Value, ty: Type) -> ir.Value:
        ty_name = _type_name(ty)
        v_ty = getattr(v, "type", None)
        if isinstance(ty, IntType) or ty_name == "int":
            if self._ir_type_matches(v_ty, _I64):
                return v
            if _ir_type_is_pointer(v_ty):
                return marshal.marshal_from_object(
                    self.builder, self.module, self.runtime, v, ty
                )
            # Should not happen in L1 (always i64), but guard anyway.
            return self.builder.sext(v, _I64, name=self._fresh("sext"))
        if isinstance(ty, BoolType) or ty_name == "bool":
            if self._ir_type_matches(v_ty, _I1):
                return self.builder.zext(v, _I64, name=self._fresh("b2i64"))
            if _ir_type_is_pointer(v_ty):
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    IntType(name="int"),
                )
            return self.builder.zext(v, _I64, name=self._fresh("b2i64"))
        if isinstance(ty, NoneType) or ty_name == "None":
            # Flow-insensitive inference can leave an Optional[int]-like
            # local typed as None even after guards refine the runtime
            # payload to an actual integer.
            if _ir_type_is_pointer(v_ty):
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    IntType(name="int"),
                )
            try:
                v_width = int(v_ty.width)
            except AttributeError:
                v_width = -1
            if v_width > 0:
                if v_width == 64:
                    return v
                if v_width == 1:
                    return self.builder.zext(v, _I64, name=self._fresh("none.b2i64"))
                return self.builder.sext(v, _I64, name=self._fresh("none.sext"))
        if isinstance(ty, FloatType) or ty_name == "float":
            # Python semantic: ``int(3.7) == 3`` (truncate toward zero).
            return self.builder.fptosi(v, _I64, name=self._fresh("f2i"))
        if isinstance(ty, DynType) or ty_name == "dyn":
            # Dynamic values: unbox via ``py_int_to_i64`` when we hold a
            # ``PyObject*``, or pass the native integer through if an
            # earlier coercion already produced one (common for chained
            # binops where the inner result is already ``i64``).
            if isinstance(v.type, ir.PointerType):
                # CPython-backed DynType values use a different unbox
                # path than pcc-native PyObject*.
                if v in getattr(self, "_cpy_values", ()):
                    return self.builder.call(
                        self.runtime["py_cpy_to_i64"],
                        [v],
                        name=self._fresh("cpy.to_i64"),
                    )
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    IntType(name="int"),
                )
            if isinstance(v.type, ir.IntType):
                if v.type.width == 64:
                    return v
                if v.type.width == 1:
                    return self.builder.zext(v, _I64, name=self._fresh("b2i64"))
                return self.builder.sext(v, _I64, name=self._fresh("sext"))
            if isinstance(v.type, (ir.FloatType, ir.DoubleType)):
                return self.builder.fptosi(v, _I64, name=self._fresh("f2i"))
        raise NotImplementedError(f"Layer 1 cannot coerce {type(ty).__name__} to int")

    def _to_double(self, v: ir.Value, ty: Type) -> ir.Value:
        if isinstance(ty, FloatType):
            if isinstance(v.type, ir.PointerType):
                # The annotation says FloatType but the IR value is a
                # PyObject* (e.g. ``float(x)`` lowered via py_cpy_call1
                # before the scaffold path could fold it). Unbox before
                # the caller expects a native ``double``.
                if v in getattr(self, "_cpy_values", ()):
                    return self.builder.call(
                        self.runtime["py_cpy_to_f64"],
                        [v],
                        name=self._fresh("cpy.to_f64"),
                    )
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    FloatType(name="float"),
                )
            return v
        if isinstance(ty, IntType):
            if isinstance(v.type, ir.PointerType):
                # A boxed int may be an arbitrary-precision bignum; marshalling
                # to i64 first truncates it (float(2**70) -> 0.0). py_float_to_f64
                # (reached via marshal_from_object FloatType) handles tagged ints
                # AND bignums (py_bigint_to_double), so go object -> double
                # directly, matching the DynType-pointer branch below.
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    FloatType(name="float"),
                )
            if ty.name == "pcc.u64":
                return self.builder.uitofp(
                    v,
                    _DOUBLE,
                    name=self._fresh("u2f"),
                )
            return self.builder.sitofp(v, _DOUBLE, name=self._fresh("i2f"))
        if isinstance(ty, BoolType):
            if self._ir_type_matches(v.type, _I1):
                return self.builder.uitofp(v, _DOUBLE, name=self._fresh("b2f"))
            if isinstance(v.type, ir.PointerType):
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    FloatType(name="float"),
                )
            return self.builder.uitofp(v, _DOUBLE, name=self._fresh("b2f"))
        if isinstance(ty, DynType):
            if isinstance(v.type, ir.PointerType):
                if v in getattr(self, "_cpy_values", ()):
                    return self.builder.call(
                        self.runtime["py_cpy_to_f64"],
                        [v],
                        name=self._fresh("cpy.to_f64"),
                    )
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    FloatType(name="float"),
                )
            # Raw native scalar (i64 / i1) held in a DynType slot.
            if self._ir_type_matches(v.type, _I64):
                return self.builder.sitofp(v, _DOUBLE, name=self._fresh("i2f"))
            if isinstance(v.type, ir.IntType):
                if v.type.width == 1:
                    return self.builder.uitofp(
                        v,
                        _DOUBLE,
                        name=self._fresh("b2f"),
                    )
                widened = self.builder.sext(
                    v,
                    _I64,
                    name=self._fresh("dyn.sext64"),
                )
                return self.builder.sitofp(
                    widened,
                    _DOUBLE,
                    name=self._fresh("i2f"),
                )
            if isinstance(v.type, ir.DoubleType):
                return v
        raise NotImplementedError(f"Layer 1 cannot coerce {type(ty).__name__} to float")

    def _truthy(self, v: ir.Value, ty: Type) -> ir.Value:
        # Domain wins over the inferred semantic type.  A CPython fallback
        # can produce a real PyObject* while retaining an IntType/BoolType
        # annotation; native truth helpers would read that foreign pointer as
        # a pcc header.  Guard and dispatch through PyObject_IsTrue first.
        if v in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(v)
            i32 = self.builder.call(
                self.runtime["py_cpy_truthy"],
                [v],
                name=self._fresh("cpy.truthy"),
            )
            self._guard_cpy_status_not_negative(
                i32,
                (v,) if self._cpy_value_is_owned(v) else (),
            )
            return self.builder.trunc(
                i32,
                _I1,
                name=self._fresh("cpy.truthy.i1"),
            )
        if isinstance(ty, BoolType):
            if self._ir_type_matches(v.type, _I1):
                return v
            if isinstance(v.type, ir.PointerType):
                i32 = self.builder.call(
                    self.runtime["py_obj_truthy"],
                    [v],
                    name=self._fresh("truthy_obj"),
                )
                self._emit_post_call_err_check(None)
                return self.builder.trunc(i32, _I1, name=self._fresh("truthy_obj_i1"))
            return self.builder.icmp_signed(
                "!=", v, ir.Constant(v.type, 0), name=self._fresh("truthy_int")
            )
        if isinstance(ty, IntType):
            if isinstance(v.type, ir.PointerType):
                truth = self.builder.call(
                    self.runtime["py_obj_truthy"],
                    [v],
                    name=self._fresh("truthy_int_obj"),
                )
                self._emit_post_call_err_check(None)
                return self.builder.icmp_signed(
                    "!=",
                    truth,
                    ir.Constant(truth.type, 0),
                    name=self._fresh("truthy_i"),
                )
            zero = ir.Constant(
                v.type if isinstance(v.type, ir.IntType) else _I64, 0
            )
            return self.builder.icmp_signed("!=", v, zero, name=self._fresh("truthy_i"))
        if isinstance(ty, FloatType):
            zero = ir.Constant(_DOUBLE, 0.0)
            return self.builder.fcmp_unordered(
                "!=", v, zero, name=self._fresh("truthy_f")
            )
        if isinstance(v.type, ir.IntType):
            # A raw machine integer (an extern ``c_int`` result, say) cannot
            # be a boxed object: it is truthy when nonzero.  Boxing it to ask
            # py_obj_truthy was slower and is forbidden in freestanding code.
            return self.builder.icmp_signed(
                "!=", v, ir.Constant(v.type, 0), name=self._fresh("truthy_raw")
            )
        if self._is_object(ty) or isinstance(ty, DynType):
            # CPython-backed values must go through py_cpy_truthy
            # (PyObject_IsTrue) — the pcc py_obj_truthy only knows
            # about pcc's own PyObject layout.
            # Any object: route through py_obj_truthy, which honours
            # container emptiness, None == False, etc.
            obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, v, ty
            )
            i32 = self.builder.call(
                self.runtime["py_obj_truthy"],
                [obj],
                name=self._fresh("truthy_obj"),
            )
            self._emit_post_call_err_check(None)
            return self.builder.trunc(i32, _I1, name=self._fresh("truthy_obj_i1"))
        if isinstance(ty, ClassType) and self._is_valueclass_payload_type(ty) and isinstance(v.type, ir.LiteralStructType):
            output = self._new_slot_call_root("value.truth")
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                self._emit_valueclass_payload_to_object(v, ty, result_slot=output)
                result = self._slot_call_runtime_call("py_obj_truthy", (output,))
                self._release_slot_call_roots((output,))
                return self.builder.trunc(result, _I1, name=self._fresh("value.truth.result"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup
        if isinstance(ty, ClassType):
            # valueclass payloads box (always-truthy valuebox unless a user
            # __bool__/__len__ runs via py_obj_truthy); instance pointers
            # dispatch the same way
            obj = self._emit_valueclass_payload_to_object(v, ty)
            if obj is None and isinstance(v.type, ir.PointerType):
                obj = v
            if obj is not None:
                i32 = self.builder.call(
                    self.runtime["py_obj_truthy"],
                    [obj],
                    name=self._fresh("truthy_obj"),
                )
                self._emit_post_call_err_check(None)
                return self.builder.trunc(i32, _I1, name=self._fresh("truthy_obj_i1"))
        raise NotImplementedError(
            f"Layer 1 cannot compute truthiness of {type(ty).__name__}"
        )

    def _coerce(self, v: ir.Value, from_ty: Type, to_ty: Type, source_expr=None) -> ir.Value:
        """Coerce ``v`` (typed ``from_ty``) to ``to_ty``.

        Covers the L1 scalar matrix plus the L2 object-pass-through and
        native-↔-object marshalling cases.
        """
        if from_ty is None or to_ty is None:
            return v
        if isinstance(from_ty, RawPointerType) or isinstance(to_ty, RawPointerType):
            if isinstance(from_ty, RawPointerType) and isinstance(to_ty, RawPointerType):
                return v
            if (isinstance(to_ty, RawPointerType)
                    and isinstance(from_ty, (DynType, IntType))
                    and source_expr is not None
                    and raw_abi_expression_provenance(self, source_expr)):
                if isinstance(from_ty, IntType):
                    return self._pointer_or_address_operand(v, from_ty)
                if isinstance(v.type, ir.PointerType):
                    return v
            # The existing freestanding implicit ABI maps missing/dyn
            # annotations to ptr, never an object slot or unknown scalar.
            if (getattr(self, "_freestanding_module", False)
                    and isinstance(v.type, ir.PointerType)
                    and isinstance(from_ty, (DynType, RawPointerType))
                    and isinstance(to_ty, (DynType, RawPointerType))):
                return v
            # Runtime helpers use the exact raw NULL constant as an absent
            # pointer/error sentinel. It is not Python None and carries no
            # owner. No non-null raw value or computed cast qualifies.
            if (getattr(self, "_runtime_port_module", False)
                    and isinstance(from_ty, RawPointerType)
                    and isinstance(to_ty, DynType)
                    and isinstance(v, ir.Constant)
                    and isinstance(v.type, ir.PointerType)
                    and v.value is None):
                return v
            span = getattr(source_expr, "span", None)
            where = f" at {span.file}:{span.line}:{span.col}" if span is not None else ""
            raise NotImplementedError(
                "raw pointer cannot cross an implicit Python value boundary; "
                "use ptr_to_int or int_to_ptr explicitly" + where
            )
        if isinstance(from_ty, ValueArrayType) and (
            isinstance(to_ty, DynType) or self._is_object(to_ty)
        ):
            raise L1CodegenError(
                "pcc.array cannot cross an object or Any boundary; "
                "select an element first"
            )
        if isinstance(from_ty, DynType) and isinstance(to_ty, DynType):
            # Inference may widen a method call while its declared native
            # return still produces a registered valueclass payload.  Keep
            # that exact semantic type when crossing the dynamic boundary;
            # a matching physical struct layout is not type provenance.
            source = self._valueclass_payload_source(v)
            if source is not None and self._is_valueclass_payload_type(source[3]):
                from_ty = source[3]
        if (isinstance(to_ty, IntType) and self._int_exprs_are_boxed()
                and to_ty.name not in ("pcc.i64", "pcc.u64")):
            if isinstance(v.type, ir.PointerType):
                return v
            i64 = self._to_int64(v, from_ty)
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [i64],
                name=self._fresh("coerce.int.obj"),
            )
        if _same_type_kind(from_ty, to_ty):
            # Same pcc_py type class. IR-level representations are
            # usually identical — but watch out for inference lying
            # about the payload.
            if (
                isinstance(to_ty, IntType)
                and isinstance(v.type, ir.IntType)
                and not self._ir_type_matches(v.type, _I64)
            ):
                # A typed C-ABI boundary can represent semantic Python
                # ``int`` as i8/i16/i32.  Normalize that physical projection
                # before it enters an i64 expression join (notably a ternary
                # phi); semantic type equality alone does not make the LLVM
                # widths interchangeable.
                return self._to_int64(v, from_ty)
            if self._is_valueclass_payload_type(to_ty):
                payload = self._emit_object_to_valueclass_payload(v, to_ty)
                if payload is not None:
                    return payload
            if isinstance(to_ty, (IntType, BoolType, FloatType)) and isinstance(
                v.type, ir.PointerType
            ):
                # CPython-dispatched call returned PyObject* when
                # inference claimed a native scalar.
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    to_ty,
                )
            if self._is_object(to_ty) and not isinstance(v.type, ir.PointerType):
                # Short-circuit ``x or default`` over two StrType
                # operands bottoms at an i1 from the truthiness test
                # even though both operands and the result are object-
                # typed; box to a ptr before the caller consumes.
                return marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    from_ty,
                )
            return v
        if self._is_valueclass_payload_type(to_ty):
            payload = self._emit_object_to_valueclass_payload(v, to_ty)
            if payload is not None:
                return payload
        # Native -> object marshal.
        if self._is_object(to_ty) and self._is_native_scalar_type(from_ty):
            if isinstance(v.type, ir.PointerType):
                return v
            return marshal.marshal_to_object(
                self.builder, self.module, self.runtime, v, from_ty
            )
        # Object -> native unbox.
        if self._is_native_scalar_type(to_ty) and self._is_object(from_ty):
            # A ``DynType`` value may already carry a native scalar at
            # the IR level (e.g. a BinOp that unboxed its operands);
            # only go through the runtime if we actually hold a
            # ``PyObject*``.
            if isinstance(v.type, ir.PointerType):
                return marshal.marshal_from_object(
                    self.builder, self.module, self.runtime, v, to_ty
                )
            if isinstance(to_ty, IntType):
                return self._to_int64(v, from_ty)
            if isinstance(to_ty, BoolType):
                return self._truthy(v, from_ty)
            if isinstance(to_ty, FloatType):
                return self._to_double(v, from_ty)
            return v
        # Object -> object (e.g. list -> dyn): ptr pass-through.
        if self._is_object(to_ty) and self._is_object(from_ty):
            # Guard: when inference widened to an object type but the
            # concrete IR value is still a native scalar (e.g. a
            # short-circuit ``x or default`` over two objects that
            # bottoms out at an i1 from the empty-str truthiness
            # test), box before continuing so the callee sees a ptr.
            if not isinstance(v.type, ir.PointerType):
                return marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    from_ty,
                )
            return v
        if isinstance(to_ty, FloatType):
            return self._to_double(v, from_ty)
        if isinstance(to_ty, IntType):
            return self._to_int64(v, from_ty)
        if isinstance(to_ty, BoolType):
            return self._truthy(v, from_ty)
        if isinstance(to_ty, NoneType):
            # Caller is expected to discard; leave value intact.
            return v
        if isinstance(to_ty, DynType):
            # Dyn accepts anything; upcast scalars to PyObject* so the
            # generic runtime helpers can handle them uniformly.
            if isinstance(v.type, ir.PointerType):
                return v
            boxed_valueclass = self._emit_valueclass_payload_to_object(v, from_ty)
            if boxed_valueclass is not None:
                return boxed_valueclass
            if self._is_native_scalar_type(from_ty) or isinstance(
                v.type, (ir.IntType, ir.FloatType, ir.DoubleType)
            ):
                return marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, v, to_ty
                )
            return v
        raise NotImplementedError(
            f"Layer 1/2 cannot coerce {type(from_ty).__name__} -> "
            f"{type(to_ty).__name__}"
        )

    def _is_native_scalar_type(self, ty: Type) -> bool:
        return isinstance(ty, (IntType, FloatType, BoolType))

    def _coerce_from_object(self, pyobj: ir.Value, target_ty: Type) -> ir.Value:
        """Unwrap ``pyobj`` into the representation of ``target_ty``.

        Object-typed targets stay as PyObject*; native targets go
        through :func:`marshal.marshal_from_object`.
        """
        if self._is_object(target_ty) or isinstance(target_ty, DynType):
            return pyobj
        if (isinstance(target_ty, IntType) and self._int_exprs_are_boxed()
                and target_ty.name not in ("pcc.i64", "pcc.u64")):
            return pyobj
        if self._is_native_scalar_type(target_ty):
            return marshal.marshal_from_object(
                self.builder, self.module, self.runtime, pyobj, target_ty
            )
        # Unknown target — return the boxed form untouched.
        return pyobj
