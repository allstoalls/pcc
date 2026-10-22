"""CPython bridge primitive helpers for L1CodeGen."""
from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import ByteArrayType, BoolType, BytesType, ClassType, DictType, DynType, Expr, FloatType, FuncType, IntType, ListType, MemoryViewType, NoneType, SetType, StrType, TupleType, Type
from pcc.frontends.python.codegen import marshal


_I64 = ir.IntType(64)


def _is_none_type_for_cpython_bridge(ty: Type) -> bool:
    if isinstance(ty, NoneType):
        return True
    # pcc1 self-host can cross module/class identity boundaries while still
    # preserving the frontend type object's nominal class/name.  Do not make
    # the CPython bridge depend solely on ``isinstance`` for the None sentinel.
    if type(ty).__name__ == "NoneType":
        return True
    return getattr(ty, "name", "") in ("None", "NoneType")


class CpyBridgeLoweringMixin:
    def _mark_cpy_value(self, value: ir.Value) -> ir.Value:
        # A CPython object is always carried as ``PyObject *`` at this ABI.
        # Call-result analysis is deliberately conservative and can identify
        # a helper body such as ``return hasattr(...)`` as CPython-shaped even
        # when the helper's declared/native return lane is ``i1``.  Never let
        # that source-level heuristic retag a scalar SSA value: downstream
        # truthiness/refcount lowering would otherwise pass the scalar to
        # ``py_cpy_*`` pointer APIs and grow the self-host fallback closure.
        if not isinstance(value.type, ir.PointerType):
            return value
        self._cpy_values.add(value)
        return value

    def _mark_owned_cpy_value(self, value: ir.Value) -> ir.Value:
        """Tag a CPython-domain SSA value carrying one new reference."""
        if not isinstance(value.type, ir.PointerType):
            return value
        self._cpy_values.add(value)
        self._owned_cpy_values.add(value)
        return value

    def _cpy_value_is_owned(self, value: ir.Value) -> bool:
        return value in self._owned_cpy_values

    def _forget_owned_cpy_value(self, value: ir.Value) -> None:
        """Record that a new reference was released or transferred."""
        self._owned_cpy_values.discard(value)

    def _emit_cpy_attr(self, obj_val: ir.Value, name: str) -> ir.Value:
        self._guard_cpy_value_not_null(obj_val)
        attr_ptr = self._ptr_to_cstr(self._cstr_global(name, f".cpy.attr.{name}"))
        val = self.builder.call(
            self.runtime["py_cpy_getattr"],
            [obj_val, attr_ptr],
            name=self._fresh(f"cpy.get.{name}"),
        )
        # The getattr result is a new reference and retains any receiver state
        # it needs.  Consume only a fresh expression receiver here; imported
        # module globals and CPython locals remain borrowed.
        if self._cpy_value_is_owned(obj_val):
            self.builder.call(self.runtime["py_cpy_decref"], [obj_val])
            self._forget_owned_cpy_value(obj_val)
        return self._mark_owned_cpy_value(val)

    def _load_cpython_builtin(self, name: str) -> ir.Value:
        import os
        import sys

        module_name = ""
        try:
            module_name = self.ast_module.name
        except AttributeError:
            module_name = ""
        function_name = "<module>"
        current_func_def = getattr(self, "current_func_def", None)
        if current_func_def is not None:
            function_name = current_func_def.name
        strict_stub_filter = os.environ.get(
            "PCC_DEBUG_STRICT_NOLIB_STUB", ""
        ).strip()
        qualified_name = str(module_name) + "." + str(function_name)
        if strict_stub_filter and (
            strict_stub_filter == "1" or strict_stub_filter in qualified_name
        ):
            sys.stderr.write(
                "[pcc.strict-nolib-fallback] "
                + qualified_name
                + ": builtin="
                + str(name)
                + "\n"
            )
        if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
            sys.stderr.write(
                "debug: load_cpython_builtin module="
                + str(module_name)
                + " name="
                + str(name)
                + "\n"
            )
        mod_name_gv = self._cstr_global(
            "builtins",
            ".cpy.builtins_modname",
        )
        mod_val = self.builder.call(
            self.runtime["py_cpy_import"],
            [self._ptr_to_cstr(mod_name_gv)],
            name=self._fresh("cpy.builtins"),
        )
        self._guard_cpy_value_not_null(mod_val)
        attr_gv = self._cstr_global(
            name,
            f".cpy.builtin.{name}",
        )
        fn_val = self.builder.call(
            self.runtime["py_cpy_getattr"],
            [mod_val, self._ptr_to_cstr(attr_gv)],
            name=self._fresh(f"cpy.builtin.{name}"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [mod_val])
        return self._mark_owned_cpy_value(fn_val)

    def _load_cpython_builtin_with_cleanup(
        self,
        name: str,
        live_owned: tuple[ir.Value, ...],
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        """Load a builtin while chaining failure through live-ref cleanup."""
        if not live_owned and not pinned_pcc:
            return self._load_cpython_builtin(name)
        previous_cleanup = getattr(self, "_cpy_operand_cleanup_block", None)
        target = previous_cleanup
        if target is None:
            target = self._current_try_err_block()
        if target is None:
            target = self._ensure_fn_err_exit()
        cleanup = self._make_cpy_operand_cleanup_block(
            live_owned,
            (),
            target,
            "cpy.builtin.cleanup",
            pinned_pcc,
        )
        self._cpy_operand_cleanup_block = cleanup
        try:
            return self._load_cpython_builtin(name)
        finally:
            self._cpy_operand_cleanup_block = previous_cleanup

    def _emit_cpy_attr_with_cleanup(
        self,
        obj_val: ir.Value,
        attr_name: str,
        live_owned: tuple[ir.Value, ...],
        rooted_pcc: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc: tuple[tuple[ir.Value, bool], ...] = (),
    ) -> ir.Value:
        """Load a CPython attribute while unwinding surrounding temporaries."""
        if not live_owned and not rooted_pcc and not pinned_pcc:
            return self._emit_cpy_attr(obj_val, attr_name)
        previous_cleanup = getattr(self, "_cpy_operand_cleanup_block", None)
        target = previous_cleanup
        if target is None:
            target = self._current_try_err_block()
        if target is None:
            target = self._ensure_fn_err_exit()
        cleanup = self._make_cpy_operand_cleanup_block(
            live_owned,
            rooted_pcc,
            target,
            "cpy.attr.cleanup",
            pinned_pcc,
        )
        self._cpy_operand_cleanup_block = cleanup
        try:
            return self._emit_cpy_attr(obj_val, attr_name)
        finally:
            self._cpy_operand_cleanup_block = previous_cleanup

    def _emit_value_as_pcc_object_or_bridge(
        self,
        value: ir.Value,
        value_ty: Type,
        name_hint: str,
        *,
        consume_valueclass_payload_fields: bool = False,
        cpy_owned_on_error: tuple[ir.Value, ...] = (),
        rooted_pcc_on_error: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        pcc_release_on_error: tuple[ir.Value, ...] = (),
        result_slot=None,
    ) -> ir.Value:
        if value in self._cpy_values:
            value_owned = self._cpy_value_is_owned(value)
            cleanup_owned = list(cpy_owned_on_error)
            if value_owned and not any(item is value for item in cleanup_owned):
                cleanup_owned.append(value)
            cleanup_owned_tuple = tuple(cleanup_owned)
            self._guard_cpy_value_not_null(
                value,
                cleanup_owned_tuple,
                rooted_pcc_on_error,
                pinned_pcc_on_error,
                pcc_release_on_error,
            )
            if result_slot is not None:
                # The bridge returns a new PCC owner, regardless of whether
                # its CPython source is borrowed or owned. Publish at this
                # producer boundary, before a null guard, source decref, or
                # fallible lease operation can park. A CPython pointer itself
                # never enters the managed output slot.
                previous = self._current_try_err_block()
                pcc_target = previous if previous is not None else self._ensure_fn_err_exit()
                saved_cpy = self._cpy_operand_cleanup_block
                cpy_target = saved_cpy if saved_cpy is not None else pcc_target
                cleanups = []
                for target in (pcc_target, cpy_target):
                    if cleanups and target is pcc_target:
                        cleanups.append(cleanups[0])
                        continue
                    cleanup = self.current_function.append_basic_block(
                        name=self._fresh("cpy.bridge.output.cleanup"),
                    )
                    saved_block = self.builder.block
                    self.builder.position_at_end(cleanup)
                    for owned in cleanup_owned_tuple:
                        self.builder.call(self.runtime["py_cpy_decref"], [owned])
                    for pcc_value, root_slot in rooted_pcc_on_error:
                        self._leave_container_temp_root(root_slot)
                        self._gc_release(pcc_value)
                    for pcc_value, release_owned in pinned_pcc_on_error:
                        self._gc_unpin(pcc_value)
                        if release_owned:
                            self._gc_release(pcc_value)
                    for pcc_value in pcc_release_on_error:
                        self._gc_release(pcc_value)
                    self.builder.branch(target)
                    self.builder.position_at_end(saved_block)
                    cleanups.append(cleanup)
                self._try_err_block = cleanups[0]
                self._cpy_operand_cleanup_block = cleanups[1]
                try:
                    bridged = self.builder.call(
                        self.runtime["py_cpy_to_pcc_obj"], [value],
                        name=self._fresh(name_hint),
                    )
                    self._publish_slot_call_owned(
                        result_slot, bridged, label="CPython object bridge",
                    )
                    current = self.builder.load(
                        result_slot, name=self._fresh(name_hint + ".current"),
                    )
                    self._guard_cpy_value_not_null(current)
                    if value_owned:
                        self.builder.call(self.runtime["py_cpy_decref"], [value])
                        self._forget_owned_cpy_value(value)
                finally:
                    self._try_err_block = previous
                    self._cpy_operand_cleanup_block = saved_cpy
                return self.builder.load(
                    result_slot, name=self._fresh(name_hint + ".result"),
                )
            bridged = self.builder.call(
                self.runtime["py_cpy_to_pcc_obj"],
                [value],
                name=self._fresh(name_hint),
            )
            self._guard_cpy_value_not_null(
                bridged,
                cleanup_owned_tuple,
                rooted_pcc_on_error,
                pinned_pcc_on_error,
                pcc_release_on_error,
            )
            if value_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [value])
                self._forget_owned_cpy_value(value)
            return bridged
        # Native payload boxing is fallible too. Its nested slot/lease error
        # edges must retire the caller's already-live container and operands,
        # just as the CPython bridge above does. A post-boxing error check is
        # too late: those edges have already selected their unwind target.
        payload_cleanup = isinstance(value.type, ir.LiteralStructType) and (
            cpy_owned_on_error or rooted_pcc_on_error
            or pinned_pcc_on_error or pcc_release_on_error
        )
        previous = self._current_try_err_block()
        saved_cpy = self._cpy_operand_cleanup_block
        if payload_cleanup:
            pcc_target = previous if previous is not None else self._ensure_fn_err_exit()
            cpy_target = saved_cpy if saved_cpy is not None else pcc_target
            pointer_type = ir.IntType(8).as_pointer()
            exception_slot = self._alloca_in_entry(
                pointer_type, name=self._fresh("value.box.unwind.exception"),
                init_null=True,
            )
            # This is an entry-registered root, not a LIFO operand: cleanup
            # must be able to leave the outer container frames while retaining
            # the selecting exception across reentrant finalizers.
            self._ensure_local_gc_frame_root(
                self._fresh("value.box.unwind.owner"), exception_slot,
                pointer_type, allow_module=True,
            )
            swap_exception = self.module.globals.get("py_tls_exc_swap_slot")
            if swap_exception is None:
                swap_exception = ir.Function(
                    self.module, ir.FunctionType(ir.VoidType(), [pointer_type]),
                    name="py_tls_exc_swap_slot",
                )
            cleanups = []
            for target in (pcc_target, cpy_target):
                if cleanups and target is pcc_target:
                    cleanups.append(cleanups[0])
                    continue
                cleanup = self.current_function.append_basic_block(
                    name=self._fresh("value.box.unwind.cleanup"),
                )
                saved_block = self.builder._block
                self.builder.position_at_end(cleanup)
                self.builder.call(swap_exception, [self._as_gc_ptr(exception_slot)])
                for owned in reversed(cpy_owned_on_error):
                    self.builder.call(self.runtime["py_cpy_decref"], [owned])
                for pcc_value, root_slot in reversed(rooted_pcc_on_error):
                    # Keep the original owner until the root has cleared and
                    # unpinned it; a pin does not prevent terminal decref.
                    self._leave_container_temp_root(root_slot)
                    self._gc_release(pcc_value)
                for pcc_value, release_owned in reversed(pinned_pcc_on_error):
                    self._gc_unpin(pcc_value)
                    if release_owned:
                        self._gc_release(pcc_value)
                for pcc_value in reversed(pcc_release_on_error):
                    self._gc_release(pcc_value)
                self.builder.call(self.runtime["py_clear_exception"], [])
                self.builder.call(swap_exception, [self._as_gc_ptr(exception_slot)])
                self.builder.branch(target)
                self.builder.position_at_end(saved_block)
                cleanups.append(cleanup)
            self._try_err_block = cleanups[0]
            self._cpy_operand_cleanup_block = cleanups[1]
        try:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                value,
                value_ty,
                consume_fields=consume_valueclass_payload_fields,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass
            return marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                value,
                value_ty,
            )
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _marshal_to_cpython(self, v: ir.Value, ty: Type) -> tuple[ir.Value, bool]:
        """Convert a pcc-native value to a CPython PyObject*.

        Returns (cpython_value, owned) — ``owned`` is True when the
        caller must decref the result after use. Borrowed values
        (already-CPython DynType) return False.
        """
        # IR-level guard that fires before the declared-type dispatch:
        # if we actually hold a native-scalar payload (double / float /
        # int) we need to box, regardless of what inference claimed.
        # This catches cases where the type inferrer widens to ``int``
        # or a class annotation but the IR value is a raw double from
        # a ``load double, ptr %f.addr``.
        if isinstance(v.type, (ir.DoubleType, ir.FloatType)):
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_f64"],
                    [v],
                    name=self._fresh("cpy.from_f64.ir"),
                ),
                True,
            )
        if isinstance(v.type, ir.IntType):
            if v.type.width == 1:
                i64 = self.builder.zext(v, _I64, name=self._fresh("cpy.b2i64.ir"))
            elif v.type.width == 64:
                i64 = v
            elif v.type.width < 64:
                i64 = self.builder.sext(
                    v,
                    _I64,
                    name=self._fresh("cpy.sext64.ir"),
                )
            else:
                i64 = self.builder.trunc(
                    v,
                    _I64,
                    name=self._fresh("cpy.trunc64.ir"),
                )
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_i64"],
                    [i64],
                    name=self._fresh("cpy.from_i64.ir"),
                ),
                True,
            )
        if v in getattr(self, "_cpy_values", ()):
            return v, self._cpy_value_is_owned(v)
        if isinstance(ty, (IntType, BoolType)) and isinstance(
            v.type,
            ir.PointerType,
        ):
            # Exact Python integers live in a pcc bignum object.  Converting
            # through py_int_to_i64 would silently truncate the semantic int;
            # let the universal object bridge preserve arbitrary precision.
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_pcc_obj"],
                    [v],
                    name=self._fresh("cpy.from_pcc_int"),
                ),
                True,
            )
        if isinstance(ty, IntType) or isinstance(ty, BoolType):
            i64 = self._to_int64(v, ty)
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_i64"],
                    [i64],
                    name=self._fresh("cpy.from_i64"),
                ),
                True,
            )
        if isinstance(ty, FloatType):
            if isinstance(v.type, ir.PointerType):
                # CPython fallback paths like ``float(x)`` already
                # return a CPython ``float`` object; forwarding that
                # object is correct, and avoids trying to re-box a ptr
                # as though it were a native ``double``.
                return v, False
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_f64"],
                    [v],
                    name=self._fresh("cpy.from_f64"),
                ),
                True,
            )
        if isinstance(ty, StrType):
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_pccstr"],
                    [v],
                    name=self._fresh("cpy.from_pccstr"),
                ),
                True,
            )
        if _is_none_type_for_cpython_bridge(ty):
            # None → CPython's Py_None (borrowed ref from the universal
            # converter on a pcc py_None). Use the same converter so we
            # don't have to teach codegen about the CPython Py_None sym.
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_pcc_obj"],
                    [v],
                    name=self._fresh("cpy.from_pcc_none"),
                ),
                True,
            )
        if isinstance(
            ty,
            (
                ListType,
                DictType,
                TupleType,
                SetType,
                BytesType,
                ByteArrayType,
                MemoryViewType,
            ),
        ):
            # pcc-native object containers / byte buffers — rebuild as a
            # CPython object.
            # The universal converter walks the pcc object via type tag
            # and recurses through nested containers.
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_pcc_obj"],
                    [v],
                    name=self._fresh(f"cpy.from_pcc_{type(ty).__name__.lower()[:-4]}"),
                ),
                True,
            )
        # DynType with a native integer / float / pointer payload: pick
        # the marshaller that matches the IR type we actually hold.
        if isinstance(ty, DynType):
            if isinstance(v.type, ir.IntType):
                if v.type.width == 1:
                    # bool → CPython bool via int(0/1).
                    i64 = self.builder.zext(v, _I64, name=self._fresh("b2i64"))
                else:
                    i64 = (
                        v
                        if v.type.width == 64
                        else self.builder.sext(
                            v,
                            _I64,
                            name=self._fresh("sext64"),
                        )
                    )
                return (
                    self.builder.call(
                        self.runtime["py_cpy_from_i64"],
                        [i64],
                        name=self._fresh("cpy.from_i64.dyn"),
                    ),
                    True,
                )
            if isinstance(v.type, (ir.FloatType, ir.DoubleType)):
                return (
                    self.builder.call(
                        self.runtime["py_cpy_from_f64"],
                        [v],
                        name=self._fresh("cpy.from_f64.dyn"),
                    ),
                    True,
                )
            if isinstance(v.type, ir.PointerType):
                # Native pcc object (not already a CPython ref) —
                # rebuild the corresponding CPython object by walking
                # the runtime type tag.
                return (
                    self.builder.call(
                        self.runtime["py_cpy_from_pcc_obj"],
                        [v],
                        name=self._fresh("cpy.from_pcc.dynobj"),
                    ),
                    True,
                )
        # ClassType / instance values are PyObject* in pcc-native form,
        # so route through the same py_cpy_from_pcc_obj bridge as the
        # DynType-pointer case. Without this clause, isinstance type
        # narrowing would surface ``Layer 1 cannot marshal ClassType``
        # at sites where the narrowed variable feeds a CPython call.
        if isinstance(ty, ClassType) and isinstance(v.type, ir.PointerType):
            return (
                self.builder.call(
                    self.runtime["py_cpy_from_pcc_obj"],
                    [v],
                    name=self._fresh("cpy.from_pcc.cls"),
                ),
                True,
            )
        if isinstance(ty, FuncType) and isinstance(v.type, ir.PointerType):
            # Function values are already opaque callable objects at
            # runtime. CPython-backed call paths use this for callback
            # arguments such as ``re.sub(..., repl, text)`` during
            # host-only pass self-compile probes.
            return v, False
        raise NotImplementedError(
            f"Layer 1 cannot marshal {type(ty).__name__} to CPython yet"
        )

    def _marshal_to_cpython_consuming_source(
        self,
        value: ir.Value,
        value_ty: Type,
        source_expr: Expr,
        cpy_owned_on_error: tuple[ir.Value, ...] = (),
        rooted_pcc_on_error: tuple[tuple[ir.Value, ir.Value], ...] = (),
        pinned_pcc_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        pcc_release_on_error: tuple[ir.Value, ...] = (),
    ) -> tuple[ir.Value, bool]:
        """Bridge a value and consume only a provably fresh pcc source.

        ``py_cpy_from_pcc_*`` creates an independent CPython reference; it
        does not steal the pcc object it walks.  Fresh expression results must
        therefore stay pinned during conversion and be released on both the
        NULL and success edges.  Borrowed locals/globals are pinned only: a
        moving collector updates their root slot, not an already-loaded SSA
        pointer, but the bridge must not consume their reference.

        Literal builders that pre-pin several operands deliberately keep using
        ``_marshal_to_cpython`` directly so their batch cleanup owns the source
        lifetime instead of this single-expression contract.
        """
        source_pinned = (
            isinstance(value.type, ir.PointerType)
            and value not in getattr(self, "_cpy_values", ())
            and not isinstance(value_ty, FuncType)
        )
        source_owned = (
            source_pinned
            and self._pcc_pointer_source_is_owned(source_expr)
        )
        if source_pinned:
            self._gc_pin(value)
        cpy_value, cpy_owned = self._marshal_to_cpython(value, value_ty)
        if not source_pinned:
            return cpy_value, cpy_owned
        if cpy_value is value:
            # A passthrough does not create an independent reference, so keep
            # the original ownership for the caller to consume.
            self._gc_unpin(value)
            return cpy_value, source_owned
        self._guard_cpy_value_not_null(
            cpy_value,
            cpy_owned_on_error,
            rooted_pcc_on_error,
            pinned_pcc_on_error + ((value, source_owned),),
            pcc_release_on_error,
        )
        self._gc_unpin(value)
        if source_owned:
            self._gc_release(value)
        return cpy_value, cpy_owned
