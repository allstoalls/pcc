"""Unary and residual call helper lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    RawPointerType,
    Attr,
    BoolLit,
    BoolType,
    Call,
    ClassType,
    ComplexType,
    DynType,
    Expr,
    FloatType,
    IntLit,
    IntType,
    Name,
    SetType,
    SourceSpan,
    StrLit,
    StrType,
    Subscript,
    Type,
    UnaryOp,
)
from pcc.frontends.python.codegen import marshal

_DOUBLE = ir.DoubleType()
_CPY_BUILTIN_TYPE_NAMES = frozenset(
    {
        "BaseException",
        "Exception",
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "set",
        "str",
        "tuple",
        "type",
    }
)


# Members the builtin bases' native namespaces provide (py_class.py,
# _builtin_namespace_names). ``dict.get(d, k)``, ``Exception.__str__(e)``
# and the like resolve them on the native type object; another name keeps
# the CPython route, which --python-libpython=off reports at compile time.
NATIVE_EXCEPTION_NAMESPACE_MEMBERS = frozenset(("__init__", "__str__", "__repr__", "add_note"))
NATIVE_DICT_NAMESPACE_MEMBERS = frozenset((
    "__init__", "__getitem__", "__setitem__", "__delitem__", "__contains__",
    "__len__", "__iter__", "__repr__", "__eq__", "__ne__", "__lt__", "__le__",
    "__gt__", "__ge__", "__or__", "__ror__", "__ior__", "get", "keys",
    "values", "items", "pop", "popitem", "setdefault", "update", "clear",
    "copy",
))
_NATIVE_BUILTIN_TYPE_MEMBERS = {
    "dict": NATIVE_DICT_NAMESPACE_MEMBERS,
    "BaseException": NATIVE_EXCEPTION_NAMESPACE_MEMBERS,
    "Exception": NATIVE_EXCEPTION_NAMESPACE_MEMBERS,
}


def folded_int_literal(expr: Expr) -> object:
    if isinstance(expr, IntLit) and isinstance(expr.value, int) and not isinstance(expr.value, bool):
        return expr.value
    if isinstance(expr, UnaryOp) and expr.op in ("-", "~", "+"):
        value: object = folded_int_literal(expr.operand)
        if value is not None:
            if expr.op == "-":
                return -value
            if expr.op == "~":
                return ~value
            return value
    return None


def is_i64_int_literal(expr: Expr) -> bool:
    value = folded_int_literal(expr)
    return value is not None and -(1 << 63) <= value <= (1 << 63) - 1


class UnaryCallLoweringMixin:
    def _slot_call_unary_runtime(self, expr):
        """Choose the generic object protocol only for ordinary Python values."""
        runtime_name = {"+": "py_obj_pos", "-": "py_obj_neg", "~": "py_obj_invert"}.get(expr.op)
        if (runtime_name is None or getattr(self, "_freestanding_module", False)
                or getattr(self, "_runtime_port_module", False)):
            return None
        function = self.current_function
        if function is not None and (
                function.name in getattr(self, "_manual_pointer_abi_functions", ())
                or function.name in getattr(self, "_c_abi_export_symbols", ())):
            return None
        for ty in (expr.ty, expr.operand.ty):
            if isinstance(ty, IntType) and (
                    ty.name != "int" or ty.width != 64 or not ty.signed):
                return None
            if isinstance(ty, FloatType) and (ty.name != "float" or ty.width != 64):
                return None
            if self._is_valueclass_payload_type(ty):
                return None
        # The existing runtime does not emit Python 3.15's ~bool warning.
        # Preserve the prior explicit boundary for statically known booleans.
        if expr.op == "~" and isinstance(expr.operand.ty, BoolType):
            return None
        if self._expr_looks_cpython(expr):
            return None
        if (self._expr_returns_unsafe_raw_pointer(expr)
                or self._expr_returns_unsafe_raw_pointer(expr.operand)):
            return None
        return runtime_name

    def _emit_slot_call_unary(self, expr, label, runtime_name):
        """Root the operand and publish the owned runtime result before cleanup."""
        output = self._new_slot_call_root(label + ".result")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            operand = self._emit_slot_call_operand(expr.operand, label + ".operand")
            roots.append(operand)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                runtime_name, (operand,), result_slot=output, span=expr.span,
            )
            self._guard_cpy_value_not_null(self.builder.load(output))
            self._release_slot_call_roots((operand,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup
        return output

    def _emit_unary(self, expr: UnaryOp) -> ir.Value:
        if expr.op == "not":
            truth = self._emit_condition_value(expr.operand)
            return self.builder.not_(truth, name=self._fresh("not"))
        ty = expr.operand.ty
        folded = folded_int_literal(expr)
        if folded is not None:
            # A signed or inverted integer literal is a compile-time constant
            # (``return 0 if ok else -1``); fold it instead of emitting int
            # arithmetic that freestanding code would have to annotate.
            return self._emit_expr(IntLit(span=expr.span, ty=expr.ty, value=folded))
        if (
            getattr(self, "_freestanding_module", False)
            and isinstance(ty, IntType)
            and ty.name == "int"
            and expr.op in ("+", "-", "~")
        ):
            raise RuntimeError(
                "freestanding ordinary Python int unary arithmetic cannot "
                "preserve arbitrary precision; annotate the machine boundary "
                "with pcc.i64 or pcc.u64"
            )
        if expr.op in ("-", "~") and isinstance(ty, ClassType):
            # CPython dispatches __neg__/__invert__; the numeric coercion
            # below crashed with "cannot coerce ClassType to int" (found on
            # numpy _pep440's `-Infinity`). Dispatch BEFORE emitting the
            # operand: the dunder helper emits the receiver itself.
            dunder_name = "__neg__" if expr.op == "-" else "__invert__"
            dunder = self._try_dispatch_dunder_unary(
                expr.operand,
                dunder_name,
                (),
            )
            if dunder is not None:
                return dunder
            raise NotImplementedError(
                f"Layer 1 cannot resolve {dunder_name} for unary "
                f"{expr.op!r} on a class instance"
            )
        operand = self._emit_expr(expr.operand)
        operand_is_cpy = operand in getattr(self, "_cpy_values", ())
        if operand_is_cpy and expr.op in ("+", "-", "~"):
            # Preserve CPython numeric protocol semantics (including custom
            # dunders and arbitrary-precision ints) without a new runtime ABI:
            # acquire the unary method and reuse the existing guarded no-arg
            # callable contract.  _emit_cpy_attr consumes only a fresh
            # receiver; borrowed CPython locals stay borrowed.
            dunder_name = {
                "+": "__pos__",
                "-": "__neg__",
                "~": "__invert__",
            }[expr.op]
            fn_val = self._emit_cpy_attr(operand, dunder_name)
            return self._emit_cpy_func_call(fn_val, dunder_name, ())
        if (
            expr.op in ("+", "-", "~")
            and isinstance(operand.type, ir.PointerType)
            and not isinstance(ty, (IntType, BoolType, FloatType, ComplexType))
        ):
            # A dynamically-typed operand can be an int, a float, a complex or
            # an instance with __neg__: dispatch at run time.  The numeric
            # fallthrough below reads the operand through the i64 lane, which
            # returned the int 0 for ``-f`` on a dyn float -- pcc1's
            # ``_bits_to_float64`` produced 0.0 for every negative double.
            return self._emit_dynamic_unary(expr, operand)
        if expr.op == "+":
            return operand
        if expr.op == "-":
            if isinstance(ty, ComplexType):
                obj = marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, operand, ty
                )
                return self.builder.call(
                    self.runtime["py_complex_neg"],
                    [obj],
                    name=self._fresh("complex.neg"),
                )
            if isinstance(ty, FloatType):
                return self.builder.fneg(
                    self._f64_from_emitted(operand, expr.operand), name=self._fresh("fneg")
                )
            if (self._int_exprs_are_boxed() and isinstance(ty, (IntType, BoolType))
                    and not (isinstance(ty, IntType) and ty.name in ("pcc.i64", "pcc.u64"))):
                operand_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    operand,
                    ty,
                )
                operand_owned = (
                    isinstance(operand.type, ir.PointerType)
                    and operand not in getattr(self, "_cpy_values", ())
                    and self._pcc_pointer_source_is_owned(expr.operand)
                )
                operand_pinned = (
                    isinstance(operand.type, ir.PointerType)
                    and operand not in getattr(self, "_cpy_values", ())
                )
                operand_cleanup = ()
                if operand_pinned:
                    self._gc_pin(operand)
                    operand_cleanup = ((operand, operand_owned),)
                result = self.builder.call(
                    self.runtime["py_int_neg"],
                    [operand_obj],
                    name=self._fresh("int.obj.neg"),
                )
                self._emit_post_call_err_check(
                    None,
                    pinned_release_on_error=operand_cleanup,
                )
                self._guard_cpy_value_not_null(
                    result,
                    pinned_pcc_on_error=operand_cleanup,
                )
                if operand_pinned:
                    self._gc_unpin(operand)
                if operand_owned:
                    self._gc_release(operand)
                return result
            ival = self._to_int64(operand, ty)
            return self.builder.neg(ival, name=self._fresh("neg"))
        if expr.op == "~":
            if (self._int_exprs_are_boxed() and isinstance(ty, (IntType, BoolType))
                    and not (isinstance(ty, IntType) and ty.name in ("pcc.i64", "pcc.u64"))):
                operand_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    operand,
                    ty,
                )
                minus_one = self._emit_int_literal_object(-1)
                operand_owned = (
                    isinstance(operand.type, ir.PointerType)
                    and operand not in getattr(self, "_cpy_values", ())
                    and self._pcc_pointer_source_is_owned(expr.operand)
                )
                operand_pinned = (
                    isinstance(operand.type, ir.PointerType)
                    and operand not in getattr(self, "_cpy_values", ())
                )
                operand_cleanup = ()
                if operand_pinned:
                    self._gc_pin(operand)
                    operand_cleanup = ((operand, operand_owned),)
                result = self.builder.call(
                    self.runtime["py_int_xor"],
                    [operand_obj, minus_one],
                    name=self._fresh("int.obj.invert"),
                )
                self._emit_post_call_err_check(
                    None,
                    pinned_release_on_error=operand_cleanup,
                )
                self._guard_cpy_value_not_null(
                    result,
                    pinned_pcc_on_error=operand_cleanup,
                )
                if operand_pinned:
                    self._gc_unpin(operand)
                if operand_owned:
                    self._gc_release(operand)
                return result
            ival = self._to_int64(operand, ty)
            return self.builder.not_(ival, name=self._fresh("bnot"))

        raise NotImplementedError(f"Layer 1 unary {expr.op!r} not supported")

    def _emit_dynamic_unary(self, expr: UnaryOp, operand: ir.Value) -> ir.Value:
        runtime_name = {
            "+": "py_obj_pos",
            "-": "py_obj_neg",
            "~": "py_obj_invert",
        }[expr.op]
        operand_owned = (
            operand not in getattr(self, "_cpy_values", ())
            and self._pcc_pointer_source_is_owned(expr.operand)
        )
        operand_pinned = operand not in getattr(self, "_cpy_values", ())
        operand_cleanup = ()
        if operand_pinned:
            self._gc_pin(operand)
            operand_cleanup = ((operand, operand_owned),)
        result = self.builder.call(
            self.runtime[runtime_name],
            [operand],
            name=self._fresh("dyn.unary"),
        )
        self._emit_post_call_err_check(
            self._expr_span_or_none(expr),
            pinned_release_on_error=operand_cleanup,
        )
        self._guard_cpy_value_not_null(
            result,
            pinned_pcc_on_error=operand_cleanup,
        )
        if operand_pinned:
            self._gc_unpin(operand)
        if operand_owned:
            self._gc_release(operand)
        return result

    # -- Compare -------------------------------------------------------

    # -- BoolExpr ------------------------------------------------------

    # -- Call ----------------------------------------------------------

    def _call_user(
        self,
        fn: ir.Value,
        args_ir: list[ir.Value],
        call_name: str,
        span: Optional[SourceSpan] = None,
        root_result: bool = False,
        pinned_arg_temps: tuple[tuple[ir.Value, bool], ...] = (),
        result_slot=None,
        aggregate_result_ty=None,
    ) -> ir.Value:
        """Call a user function; after the call, check py_err_occurred()
        and branch to the active error-propagation block if a Python
        exception is pending. When we're inside a try block that block
        is the except-dispatch (self._try_err_block); otherwise it is
        the enclosing function's err-exit epilogue.

        This replaces an earlier Itanium-ABI design that used `invoke`
        + landingpad to route exceptions via libc++abi. Return-code
        style (CPython ceval.c) is portable, debuggable, and keeps
        libc++abi out of the runtime link.
        """
        # Direct declarations and typed indirect callees carry the same ABI
        # signature through different owners. A bitcast Value has its
        # FunctionType in the pointer type; it is not a Function declaration.
        function_type = None
        if isinstance(fn, ir.Function):
            function_type = fn.function_type
        elif isinstance(fn.type, ir.PointerType):
            function_type = fn.type.pointee
        if not isinstance(function_type, ir.FunctionType):
            raise TypeError("user call requires a declared function signature")
        return_type = function_type.return_type
        # An operand computed before a park boundary may have been defined in
        # a block that does not dominate this one (may_park splits a function
        # at every park).  Global-backed operands are re-derived here; every
        # other value passes through unchanged.
        args_ir = [self._value_available_at_insertion_point(a) for a in args_ir]
        publication_cleanup = None
        if (result_slot is not None and pinned_arg_temps
                and isinstance(return_type, ir.PointerType)):
            error_target = self._current_try_err_block()
            if error_target is None:
                error_target = self._ensure_fn_err_exit()
            # Build this edge before the call, so its returned owner still
            # reaches the output slot without intervening instructions.
            publication_cleanup = self._make_cpy_operand_cleanup_block(
                (), (), error_target, "call.publication.arguments.cleanup",
                pinned_arg_temps,
            )
        # An indirect pointer's SSA name is not a declaration identity. Its
        # caller must supply semantic aggregate metadata; a struct ABI alone
        # cannot establish a valueclass layout or an ownership contract.
        if aggregate_result_ty is None and isinstance(fn, ir.Function):
            declared = self._native_symbol_funcdefs.get(fn.name)
            if declared is None and isinstance(return_type, ir.LiteralStructType):
                for info in self.class_lowering.classes.values():
                    for method_name, method in info.methods.items():
                        if method is fn:
                            declared = self.class_lowering._find_method_def(info.name, method_name)
                            if declared is not None:
                                self._native_symbol_funcdefs[fn.name] = declared
                            break
                    if declared is not None:
                        break
            aggregate_result_ty = None if declared is None else declared.return_ty
        aggregate_output = None
        if isinstance(return_type, ir.LiteralStructType) and self._is_valueclass_payload_type(aggregate_result_ty):
            aggregate_output = self._new_owned_valueclass_payload(aggregate_result_ty, "value.call.result")
            self._clear_owned_valueclass_payload(aggregate_output)
        # Earlier aggregate arguments remain in their authoritative slots while
        # later operands evaluate. Reload immediately at the actual ABI call.
        current_args = []
        for argument in args_ir:
            source = self._valueclass_payload_source(argument)
            if source is not None:
                argument = self._load_valueclass_payload(source[1], source[3], source[2], source[4])
            current_args.append(argument)
        result = self.builder.call(fn, current_args, name=call_name)
        if aggregate_output is not None:
            self.builder.store(result, aggregate_output)
            for field_slot in self._valueclass_payload_owned_roots(aggregate_output):
                self._slot_call_note_published(field_slot)
        root_slot = None
        root_ptr = None
        if result_slot is not None and isinstance(result.type, ir.PointerType):
            # The expression consumer registered this empty owning output
            # before any operand evaluation. Publish at the actual return
            # instruction, ahead of error checks and argument cleanup.
            previous = self._current_try_err_block()
            saved_cpy = self._cpy_operand_cleanup_block
            try:
                if publication_cleanup is not None:
                    self._try_err_block = publication_cleanup
                    self._cpy_operand_cleanup_block = publication_cleanup
                self._publish_slot_call_owned(result_slot, result, label="user call")
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        if (
            root_result
            and result_slot is None
            and isinstance(result.type, ir.PointerType)
            and not getattr(self, "_suppress_implicit_gc_roots", False)
            and not (self.ast_module.name or "").startswith("pcc.runtime.py.")
        ):
            # Entry-block alloca, NOT builder.alloca at the call site: a
            # call inside a loop body re-executes a body-positioned alloca
            # every iteration and the stack is only reclaimed at function
            # exit, so hot loops overflow the 8MB stack after ~500K
            # iterations (shared-refcount contention benchmark, SIGSEGV in
            # py_incref prologue). The null re-store below stays at the
            # call site: the slot is nulled again after every use, so the
            # store is a no-op on re-entry but keeps the first-use
            # store_root from decref'ing garbage.
            root_slot = self._alloca_in_entry(
                result.type,
                self._fresh("call.ret.root"),
            )
            self.builder.store(ir.Constant(result.type, None), root_slot)
            root_ptr = self._as_gc_ptr(
                root_slot,
                name=self._fresh("call.ret.root.ptr"),
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [root_ptr, result],
            )
            self._emit_current_gc_frame_enter_lifo(
                self._gc_one_slot_frame_map(),
                root_slot,
            )
        self._emit_post_call_err_check(
            span,
            pinned_release_on_error=pinned_arg_temps,
            lifo_owned_root_slots_on_error=(
                (root_slot,) if root_slot is not None else ()
            ),
        )
        if root_slot is not None and root_ptr is not None:
            result = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(result.type, None), root_ptr],
                name=self._fresh("call.ret.current"),
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [root_ptr, ir.Constant(result.type, None)],
            )
            self._emit_gc_frame_leave_lifo_for_slot(root_slot)
        if (
            root_result
            and isinstance(result.type, ir.PointerType)
            and not getattr(self, "_suppress_borrowed_return_retain", False)
        ):
            # The declared user-function ABI returns one object owner. The
            # expression can still have an imprecise Dyn type, and a GC-root
            # reload creates a different SSA value. Preserve the producer's
            # ownership proof so assignments and borrowing consumers release
            # that owner instead of treating this result as a raw pointer.
            # Runtime-library helpers suppress automatic return retains and
            # manage raw/borrowed results explicitly. Their pointer ABI alone
            # does not prove an owner that the caller may consume.
            self._note_owned_object_value(result)
        if aggregate_output is not None:
            return self._load_valueclass_payload(aggregate_output, aggregate_result_ty)
        return result

    def _emit_arg_for_abi_param(
        self,
        ast_arg: Expr,
        target_ty: Type,
        param_ir_ty: ir.Type,
    ) -> ir.Value:
        self._last_call_arg_owned_temp = False
        if isinstance(target_ty, IntType):
            if isinstance(param_ir_ty, ir.PointerType):
                raw = None
                if isinstance(ast_arg.ty, IntType) and ast_arg.ty.name == "int":
                    # The callee requires the object projection, regardless
                    # of the caller's local scalar optimization policy.
                    # Field/subscript reads already own exact Python ints;
                    # scalar lowering followed by reboxing would first narrow
                    # their values through a checked i64 conversion.
                    raw = self._maybe_emit_exact_int_object(ast_arg)
                elif self._int_expr_needs_exact_object_boundary(ast_arg):
                    raw = self._emit_exact_int_operand_object(ast_arg)
                if raw is None:
                    raw = self._emit_expr(ast_arg)
                if raw in getattr(self, "_cpy_values", ()):
                    self._last_call_arg_owned_temp = True
                    return self._emit_value_as_pcc_object_or_bridge(
                        raw,
                        ast_arg.ty,
                        "cpy.arg.to_pcc",
                    )
                if isinstance(raw.type, ir.PointerType):
                    self._last_call_arg_owned_temp = (
                        self._owned_release_needed(raw, ast_arg)
                        or self._pcc_pointer_source_is_owned(ast_arg)
                    )
                    return raw
                self._last_call_arg_owned_temp = True
                return self.builder.call(
                    self.runtime["py_int_from_i64"],
                    [self._to_int64(raw, ast_arg.ty)],
                    name=self._fresh("exact.int.arg.box"),
                )
            if isinstance(param_ir_ty, ir.IntType):
                if param_ir_ty.width == 1:
                    raw = self._emit_expr(ast_arg)
                    return self._truthy(raw, ast_arg.ty)
                value = self._emit_expr_as_i64(ast_arg)
                if param_ir_ty.width < 64:
                    return self.builder.trunc(
                        value,
                        param_ir_ty,
                        name=self._fresh("abi.arg.trunc"),
                    )
                if param_ir_ty.width > 64:
                    return self.builder.sext(
                        value,
                        param_ir_ty,
                        name=self._fresh("abi.arg.extend"),
                    )
                return value
        if isinstance(target_ty, BoolType):
            if isinstance(param_ir_ty, ir.IntType) and param_ir_ty.width == 1:
                raw = self._emit_expr(ast_arg)
                return self._truthy(raw, ast_arg.ty)
        if (self._is_valueclass_payload_type(target_ty)
                and str(param_ir_ty) == str(self._valueclass_payload_ir_type(target_ty))):
            # A direct aggregate parameter needs the initialized payload,
            # not an ordinary instance followed by valuebox field reads.
            # Pointer-bearing fields need owners across later operands; do
            # not apply this scalar (possibly nested) shortcut to that shape.
            payload = self._emit_valueclass_payload_expr(ast_arg, target_ty)
            if payload is not None:
                return payload
        if self._is_object(target_ty) and isinstance(ast_arg, Call):
            payload = self._maybe_emit_valueclass_constructor_payload(
                ast_arg.ty,
                ast_arg,
            )
            if payload is not None:
                boxed_valueclass = self._emit_valueclass_payload_to_object(
                    payload,
                    ast_arg.ty,
                    consume_fields=True,
                )
                if boxed_valueclass is not None:
                    self._last_call_arg_owned_temp = True
                    return boxed_valueclass
                coerced_payload = self._coerce(payload, ast_arg.ty, target_ty, ast_arg)
                if (
                    isinstance(param_ir_ty, ir.PointerType)
                    and isinstance(coerced_payload.type, ir.PointerType)
                    and not isinstance(payload.type, ir.PointerType)
                ):
                    self._last_call_arg_owned_temp = True
                return coerced_payload
        if (
            isinstance(param_ir_ty, ir.PointerType)
            and isinstance(ast_arg, Name)
            and (
                self._name_returns_owned_function_value(ast_arg.ident)
                or self._name_returns_native_builtin_callable_value(ast_arg.ident)
            )
        ):
            v = self._emit_expr_with_native_callable_values(ast_arg)
            return self._coerce(v, ast_arg.ty, target_ty, ast_arg)
        v = self._emit_expr(ast_arg)
        if isinstance(param_ir_ty, ir.PointerType) and self._is_object(target_ty):
            source_ty = ast_arg.ty
            if isinstance(ast_arg, Name):
                env_entry = self.env.get(ast_arg.ident)
                if env_entry is not None and len(env_entry) >= 3:
                    source_ty = env_entry[2]
            boxed_valueclass = self._emit_valueclass_payload_to_object(v, source_ty)
            if boxed_valueclass is not None:
                self._last_call_arg_owned_temp = True
                return boxed_valueclass
        if v in getattr(self, "_cpy_values", ()) and isinstance(
            param_ir_ty,
            ir.PointerType,
        ):
            if isinstance(target_ty, RawPointerType):
                raise NotImplementedError("CPython object cannot implicitly become a raw pointer")
            self._last_call_arg_owned_temp = True
            return self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [v],
                name=self._fresh("cpy.arg.to_pcc"),
            )
        coerced = self._coerce(v, ast_arg.ty, target_ty, ast_arg)
        if isinstance(target_ty, RawPointerType):
            # A numeric address converted to ptr is not a new Python box.
            self._last_call_arg_owned_temp = False
            return coerced
        if (
            isinstance(param_ir_ty, ir.PointerType)
            and isinstance(coerced.type, ir.PointerType)
        ):
            if not isinstance(v.type, ir.PointerType):
                # Native -> object coercion creates a call-owned temporary.
                # The method/function lowering path pins it across later
                # operands and must release it on both the success and
                # exception edges.
                self._last_call_arg_owned_temp = True
            elif (
                self._owned_release_needed(v, ast_arg)
                or self._pcc_pointer_source_is_owned(ast_arg)
            ):
                # The argument expression itself produced a fresh reference --
                # ``f(T())``, ``f([T()])``.  Nothing else owns it, so the call
                # boundary consumes it.  A borrowed argument such as a plain
                # name is left alone by the same classifier the IntType branch
                # above already relies on.
                self._last_call_arg_owned_temp = True
        return coerced

    def _emit_arg_for_abi_param_with_cleanup(
        self,
        ast_arg: Expr,
        target_ty: Type,
        param_ir_ty: ir.Type,
        pinned_pcc: tuple[tuple[ir.Value, bool], ...],
    ) -> ir.Value:
        """Evaluate one ABI argument while unwinding earlier pointer args."""
        if not pinned_pcc:
            return self._emit_arg_for_abi_param(ast_arg, target_ty, param_ir_ty)
        previous_cpy_cleanup = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        previous_pcc_target = self._current_try_err_block()
        pcc_target = previous_pcc_target
        if pcc_target is None:
            pcc_target = self._ensure_fn_err_exit()
        pcc_cleanup = self._make_cpy_operand_cleanup_block(
            (),
            (),
            pcc_target,
            "abi.arg.pcc.cleanup",
            pinned_pcc,
        )
        cpy_target = previous_cpy_cleanup
        if cpy_target is None:
            cpy_target = pcc_target
        if cpy_target is pcc_target:
            cpy_cleanup = pcc_cleanup
        else:
            cpy_cleanup = self._make_cpy_operand_cleanup_block(
                (),
                (),
                cpy_target,
                "abi.arg.cpy.cleanup",
                pinned_pcc,
            )
        self._try_err_block = pcc_cleanup
        self._cpy_operand_cleanup_block = cpy_cleanup
        try:
            return self._emit_arg_for_abi_param(
                ast_arg,
                target_ty,
                param_ir_ty,
            )
        finally:
            self._try_err_block = previous_pcc_target
            self._cpy_operand_cleanup_block = previous_cpy_cleanup

    def _is_native_set_dyn(self, ty: Type) -> bool:
        return isinstance(ty, SetType)

    _DYN_LIST_METHOD_NATIVE = frozenset(
        {
            "append",
            "extend",
            "insert",
            "pop",
            "remove",
            "index",
            "sort",
            "clear",
        }
    )

    _DYN_DICT_METHOD_NATIVE = frozenset(
        {
            "get",
            "keys",
            "values",
            "items",
            "setdefault",
            "pop",
        }
    )

    _DYN_SET_METHOD_NATIVE = frozenset(
        {
            "add",
            "remove",
            "discard",
            "update",
        }
    )

    def _maybe_emit_builtin_type_method(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Handle ``int.__new__(cls, x)``-style builtin type dispatch.

        Without this guard, the generic "any class declaring the
        method" fallback can accidentally resolve ``int.__new__`` to a
        user class's own ``__new__`` in the same module.
        """
        attr = expr.func
        assert isinstance(attr, Attr)
        if not isinstance(attr.obj, Name):
            return None
        builtin_name = attr.obj.ident
        if builtin_name not in _CPY_BUILTIN_TYPE_NAMES:
            return None
        if (builtin_name == "str"
                and attr.name in ("count", "startswith", "endswith")
                and self._name_returns_native_builtin_callable_value(builtin_name)):
            # These descriptors belong to the canonical native str namespace.
            # Resolve the callable before evaluating operands and preserve the
            # positional-only runtime binder, including explicit receivers.
            return self._emit_slot_call_object(expr, "str." + attr.name)
        if attr.name == "__new__":
            if builtin_name == "str":
                # The allocator must receive the requested class. Rewriting
                # str.__new__(cls, x) as str(x) discards subclass identity.
                return self._emit_slot_call_object(expr, "str.__new__")
            ctor_args = expr.args
            if (
                ctor_args
                and isinstance(ctor_args[0], Name)
                and ctor_args[0].ident == "cls"
                and builtin_name == "object"
                and self.current_class is not None
            ):
                return self.class_lowering.emit_instantiate(
                    self.current_class.name,
                    (),
                    self,
                )
            if (
                ctor_args
                and isinstance(ctor_args[0], Name)
                and ctor_args[0].ident in ("cls", "self")
            ):
                ctor_args = ctor_args[1:]
            return self._emit_call(
                Call(
                    span=getattr(expr, "span", None),
                    ty=expr.ty,
                    func=Name(
                        span=getattr(attr.obj, "span", None),
                        ty=attr.obj.ty,
                        ident=builtin_name,
                    ),
                    args=ctor_args,
                    kwargs=expr.kwargs,
                )
            )
        if (
            builtin_name == "dict"
            and attr.name == "fromkeys"
            and 1 <= len(expr.args) <= 2
            and not expr.kwargs
        ):
            # Both runtime arguments stay in authoritative slots until the
            # NEW dict is published, including an omitted immortal None.
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("dict.fromkeys.result")
                roots.append(output)
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                iterable = self._emit_slot_call_operand(expr.args[0], "dict.fromkeys.iterable")
                roots.append(iterable)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if len(expr.args) == 2:
                    value = self._emit_slot_call_operand(expr.args[1], "dict.fromkeys.value")
                    roots.append(value)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                else:
                    value = self._new_slot_call_root("dict.fromkeys.value")
                    roots.append(value)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._publish_slot_call_owned(value, self._emit_none_literal(), label="fromkeys default")
                self._slot_call_runtime_call(
                    "py_dict_fromkeys", (iterable, value), result_slot=output, span=expr.span,
                )
                self._release_slot_call_roots((iterable, value))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            if sink is not None:
                return self.builder.load(output, name=self._fresh("dict.fromkeys.current"))
            return self._take_slot_call_root(output)
        if builtin_name == "int" and attr.name == "from_bytes":
            if self._has_unpacked_operands(expr.args, expr.kwargs):
                return None
            # Bind the Python signature without changing source evaluation
            # order: bytes and byteorder are positional-or-keyword, signed
            # is keyword-only. Keep every evaluated owner pinned until both
            # binding and conversion finish (including a signed.__bool__ call).
            operands = list(expr.args)
            bytes_index = 0 if operands else -1
            order_index = 1 if len(operands) >= 2 else -1
            signed_index = -1
            binding_error = ""
            if len(operands) > 2:
                binding_error = "from_bytes() takes at most 2 positional arguments"
            for keyword, operand in expr.kwargs:
                index = len(operands)
                operands.append(operand)
                if keyword == "bytes":
                    if bytes_index >= 0:
                        binding_error = "from_bytes() got multiple values for argument 'bytes'"
                    bytes_index = index
                elif keyword == "byteorder":
                    if order_index >= 0:
                        binding_error = "from_bytes() got multiple values for argument 'byteorder'"
                    order_index = index
                elif keyword == "signed":
                    if signed_index >= 0:
                        binding_error = "from_bytes() got multiple values for argument 'signed'"
                    signed_index = index
                else:
                    binding_error = "from_bytes() got an unexpected keyword argument '" + keyword + "'"
            if bytes_index < 0 and not binding_error:
                binding_error = "from_bytes() missing required argument 'bytes'"
            if order_index < 0:
                order_index = len(operands)
                operands.append(StrLit(span=expr.span, ty=StrType(name="str"), value="big"))

            roots = []
            previous = self._current_try_err_block()
            previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
            target = previous if previous is not None else self._ensure_fn_err_exit()
            leases = []
            try:
                for operand in operands:
                    cleanup = self._extern_cleanup_block(tuple(roots), target)
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    value = self._emit_expr_with_cpy_operand_cleanup(
                        operand, (), as_pcc_object=True,
                    )
                    roots.append(self._extern_enter_root(
                        value, self._owned_release_needed(value, operand), "from_bytes.argument.root",
                    ))
                cleanup = self._extern_cleanup_block(tuple(roots), target)
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                if binding_error:
                    message = self._pooled_cstr_ptr(binding_error, ".from_bytes.binding_error")
                    exc = self.builder.call(
                        self.runtime["py_exc_new"], [ir.Constant(ir.IntType(64), 3), message],
                        name=self._fresh("from_bytes.binding.exc"),
                    )
                    self.builder.call(self.runtime["py_raise"], [exc])
                    self._gc_release(exc)
                    self._emit_post_call_err_check(expr.span)
                    self._extern_release_roots(tuple(roots))
                    return self._emit_none_literal()
                # A traced slot follows moves while evaluating later operands.
                # Counted leases additionally protect raw ABI arguments across
                # callbacks that clear an aliased object's Boolean pin flag.
                for root in roots:
                    acquired = self.builder.call(
                        self.runtime["pcc_gc_foreign_lease_acquire"],
                        [self._as_gc_ptr(root[0])], name=self._fresh("from_bytes.lease.acquire"),
                    )
                    failed = self.builder.icmp_signed("<", acquired, ir.Constant(ir.IntType(64), 0))
                    error = self.current_function.append_basic_block(self._fresh("from_bytes.lease.error"))
                    ready = self.current_function.append_basic_block(self._fresh("from_bytes.lease.ready"))
                    self.builder.cbranch(failed, error, ready)
                    self.builder.position_at_end(error)
                    cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    overflow = self.current_function.append_basic_block(self._fresh("bytes.lease.overflow"))
                    invalid = self.current_function.append_basic_block(self._fresh("bytes.lease.invalid"))
                    self.builder.cbranch(
                        self.builder.icmp_signed("==", acquired, ir.Constant(ir.IntType(64), -2)), overflow, invalid,
                    )
                    self.builder.position_at_end(overflow)
                    self._emit_builtin_exception_and_branch(
                        "OverflowError", "integer byte conversion address lease overflow", expr.span,
                    )
                    self.builder.position_at_end(invalid)
                    self._emit_builtin_exception_and_branch(
                        "RuntimeError", "integer byte conversion requires a stable managed owner", expr.span,
                    )
                    self.builder.position_at_end(ready)
                    leases.append((root, acquired))
                cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                signed_value = None
                if signed_index >= 0:
                    signed_expr = operands[signed_index]
                    if isinstance(signed_expr, BoolLit):
                        if signed_expr.value:
                            signed_value = ir.Constant(ir.IntType(64), 1)
                    else:
                        signed_value = self.builder.call(
                            self.runtime["py_obj_truthy"], [self._extern_load_root(roots[signed_index])],
                            name=self._fresh("from_bytes.signed.truth"),
                        )
                        self._emit_post_call_err_check(expr.span)
                call_args = [self._extern_load_root(roots[bytes_index]), self._extern_load_root(roots[order_index])]
                helper = "py_int_from_bytes"
                if signed_value is not None:
                    helper = "py_int_from_bytes_signed"
                    call_args.append(signed_value)
                result = self.builder.call(self.runtime[helper], call_args, name=self._fresh("int.from_bytes"))
                self._emit_post_call_err_check(expr.span)
                result_root = self._extern_enter_root(result, True, "from_bytes.result.root")
                # Root teardown may call finalizers and move the result. The
                # shared take helper consumes the source owner exactly once,
                # then transfers the root owner after restoring prior pin state.
                prior = result_root[2]
                for root in reversed(roots):
                    alias = self.builder.icmp_unsigned("==", self._extern_load_root(result_root), self._extern_load_root(root))
                    prior = self.builder.select(alias, root[2], prior)
                result_root = (result_root[0], True, prior)
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = previous_cpy
            release_failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(release_failed, tuple(roots) + (result_root,))
            self._extern_release_roots(tuple(roots))
            result = self._extern_take_root(result_root)
            self._note_owned_object_value(result)
            return result
        if (
            builtin_name == "str"
            and attr.name == "maketrans"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            # Native str.maketrans(x, y) -> {ord(x[i]):ord(y[i])} (2-arg form);
            # avoids the libpython fallback. Raises ValueError on length
            # mismatch. Owned operands, the NEW dict published before cleanup.
            return self._emit_owned_operand_runtime_call(expr, "py_str_maketrans", 2)
        if (
            builtin_name == "bytes"
            and attr.name == "maketrans"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            return self._emit_owned_operand_runtime_call(expr, "py_bytes_maketrans", 2)
        if (
            builtin_name == "bytes"
            and attr.name == "fromhex"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            return self._emit_owned_operand_runtime_call(expr, "py_bytes_fromhex", 1)
        if (
            builtin_name == "float"
            and attr.name == "fromhex"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # Native float.fromhex(str) — parses a hexadecimal floating-point
            # string exactly like CPython (see py_float_fromhex.c). Avoids the
            # libpython fallback so it works under --python-libpython=off.
            # Raises ValueError / OverflowError through the owned operand
            # runtime call's error path.
            return self._emit_owned_operand_runtime_call(expr, "py_float_fromhex", 1)
        if (
            builtin_name == "bytes"
            and attr.name == "translate"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            return self._emit_owned_operand_runtime_call(expr, "py_bytes_translate", 2)
        native_members = _NATIVE_BUILTIN_TYPE_MEMBERS.get(builtin_name)
        if (native_members is not None and attr.name in native_members
                and builtin_name not in self.env
                and builtin_name not in self._module_globals):
            # The member lives in the builtin's native namespace: resolve it
            # on the native type object, then bind the call's operands.
            return self._emit_slot_call_object(expr, builtin_name + "." + attr.name)
        fn_val = self._load_cpython_builtin(builtin_name)
        return self._emit_cpy_method_call_src(
            fn_val,
            attr.name,
            expr.args,
            kwargs=expr.kwargs,
        )

    def _try_dispatch_dunder_unary(
        self,
        host_expr: "Expr",
        dunder_name: str,
        arg_exprs: tuple["Expr", ...],
        result_slot=None,
    ) -> Optional[ir.Value]:
        """If ``host_expr`` is a Name bound to a hinted class that
        defines ``dunder_name`` (via MRO), emit the direct method call
        with ``arg_exprs`` and return the result. Otherwise return None.
        """
        target = self._dunder_dispatch_target(host_expr, dunder_name)
        if target is None:
            return None
        receiver_expr, info = target
        obj_val = self._emit_direct_method_receiver(receiver_expr, info, dunder_name)
        method_fn = info.methods[dunder_name]
        return self._emit_direct_method_call(
            method_fn,
            obj_val,
            info,
            dunder_name,
            arg_exprs,
            result_slot=result_slot,
        )

    def _dunder_dispatch_target(self, host_expr: "Expr", dunder_name: str):
        """``(receiver_expr, class_info)`` that ``_try_dispatch_dunder_unary``
        would call ``dunder_name`` on, or None. Emits nothing."""
        if isinstance(host_expr, Subscript):
            host = host_expr.obj
        else:
            host = host_expr
        hint = None
        receiver_expr = host
        if isinstance(host, Name):
            hint = self.env_class_hint.get(host.ident)
            if hint is None and host.ident in ("self", "cls"):
                current_class = getattr(self, "current_class", None)
                if current_class is not None:
                    hint = current_class.name
        if hint is None:
            hint = self._class_hint_for_expr(host_expr)
            receiver_expr = host_expr
        if hint is None:
            return None
        info = self._resolve_method_mro(hint, dunder_name)
        if info is None:
            return None
        # The hint names a compatible class, not the receiver's runtime class.
        # A subclass that overrides the dunder must win -- `value.type ==
        # _CSTR` on an ``ir.Type`` hint statically called ``Type.__eq__``
        # (type and text only) for a ``PointerType`` receiver, so pcc1 took
        # every pointer type as ``i8*``.  Leave such receivers, and hints the
        # class graph cannot vouch for, to the dynamic protocol.
        hint_info = self.class_lowering.classes.get(hint)
        if hint_info is None or self.class_lowering.method_overridden_by_subclass(
            hint_info, dunder_name
        ):
            return None
        return receiver_expr, info
