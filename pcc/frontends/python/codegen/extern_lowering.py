"""pcc.extern scaffold lowering helpers for L1CodeGen."""
from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Assign, BoolLit, Call, DynType, IntType, Name, RawPointerType, StrLit, TupleExpr
from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, PY_TYPE_STR


_VOID = ir.VoidType()
_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = _I8.as_pointer()

# The collector choice is parsed once and then read by every allocation,
# dealloc and container operation in the runtime.  Both symbols return it
# after an initialized check, so that check is emitted inline and the call
# (with the callee's frame) remains only for the unparsed configuration.
# Freestanding GC objects keep the plain call: their raw-symbol closure is an
# exact contract and the hot callers are ordinary runtime modules.
_GC_BACKEND_QUERY_SYMBOLS = frozenset({"pcc_gc_backend", "pcc_gc_config_ensure"})


class ExternScaffoldMixin:
    _EXTERN_CTYPE_IR = {
        "c_void": _VOID,
        "c_bool": _I1,
        "c_int8": ir.IntType(8),
        "c_int16": ir.IntType(16),
        "c_int32": _I32,
        "c_int": _I32,
        "c_int64": _I64,
        "c_long": _I64,
        "c_uint8": ir.IntType(8),
        "c_uint16": ir.IntType(16),
        "c_uint32": _I32,
        "c_uint64": _I64,
        "c_size_t": _I64,
        "c_float": ir.FloatType(),
        "c_double": _DOUBLE,
        "c_ptr": _CSTR,  # opaque i8*
        "c_str": _CSTR,
        "c_obj": _CSTR,  # PyObject* result marker
        "c_rawptr": _CSTR,  # raw address result marker (typed int)
    }

    def _maybe_register_extern_assign(self, stmt: "Assign") -> bool:
        """If the RHS is a call to the imported ``extern`` factory,
        record the decl and suppress runtime emission. Returns True if
        handled."""
        bindings = getattr(self, "_extern_bindings", {})
        if not bindings:
            return False
        value = stmt.value
        # ``LLVMContextRef = c_ptr`` / ``c_int_alias = c_int`` —
        # module-level alias of an extern-imported type marker. pcc
        # doesn't materialise the marker at runtime, so treat the
        # assignment as a no-op. Also register the alias so later
        # ``LLVMContextRef`` references (e.g. in extern(...) decls)
        # resolve back to the same type marker.
        if (
            isinstance(value, Name)
            and value.ident in bindings
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
        ):
            bindings[stmt.targets[0].ident] = bindings[value.ident]
            return True
        if not isinstance(value, Call) or not isinstance(value.func, Name):
            return False
        if bindings.get(value.func.ident) != "extern":
            return False
        if not value.args:
            return False
        symbol_expr = value.args[0]
        if not isinstance(symbol_expr, StrLit):
            return False
        symbol = symbol_expr.value
        # Parse argtypes tuple and restype from kwargs or positional.
        argtype_exprs: tuple = ()
        restype_name: str = "c_void"
        variadic = False
        for k, kv in value.kwargs:
            if k == "argtypes" and isinstance(kv, TupleExpr):
                argtype_exprs = kv.elems
            elif k == "restype" and isinstance(kv, Name):
                restype_name = kv.ident
            elif k == "variadic" and isinstance(kv, BoolLit):
                variadic = kv.value
        if not argtype_exprs and len(value.args) >= 2:
            a = value.args[1]
            if isinstance(a, TupleExpr):
                argtype_exprs = a.elems
        if restype_name == "c_void" and len(value.args) >= 3:
            rt = value.args[2]
            if isinstance(rt, Name):
                restype_name = rt.ident
        argtype_names: list[str] = []
        for ae in argtype_exprs:
            if not isinstance(ae, Name):
                return False
            argtype_names.append(ae.ident)
        for target in stmt.targets:
            if not isinstance(target, Name):
                continue
            if not hasattr(self, "_extern_decls"):
                self._extern_decls: dict[str, tuple[str, list[str], str, bool]] = {}
            self._extern_decls[target.ident] = (
                symbol,
                argtype_names,
                restype_name,
                variadic,
            )
        return True

    def _emit_extern_call(
        self,
        decl: tuple[str, list[str], str, bool],
        args: tuple,
        call_expr: Call | None = None,
    ) -> ir.Value:
        # Only application c_obj declares a new managed owner. Runtime/port
        # pointer lanes and c_rawptr retain their existing raw ABI contract.
        if decl[2] != "c_obj" or not self._raw_addresses_are_ints():
            return self._emit_extern_call_impl(decl, args)
        sink = self._slot_call_result_sink(call_expr) if call_expr is not None else None
        output = sink if sink is not None else self._new_slot_call_root("extern.object.result")
        previous = self._current_try_err_block()
        saved_cpy = self._cpy_operand_cleanup_block
        target = previous if previous is not None else self._ensure_fn_err_exit()
        if sink is None:
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
        try:
            self._emit_extern_call_impl(decl, args, result_slot=output)
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("extern.object.result.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_extern_call_impl(self, decl, args, result_slot=None) -> ir.Value:
        symbol, argtype_names, restype_name, variadic = decl
        if ("c_str" in argtype_names or "c_obj" in argtype_names) and self._raw_addresses_are_ints():
            return self._emit_c_string_extern_call(decl, args, result_slot=result_slot)
        if (
            symbol in _GC_BACKEND_QUERY_SYMBOLS
            and not args
            and not argtype_names
            and restype_name in ("c_int64", "c_long")
            # Freestanding GC objects keep their exact raw-symbol closure.
            and not getattr(self, "_freestanding_module", False)
        ):
            return self._emit_gc_backend_query(symbol)
        # Build / get the declared function.
        param_tys = [self._EXTERN_CTYPE_IR[n] for n in argtype_names]
        ret_ty = self._EXTERN_CTYPE_IR[restype_name]
        fnty = ir.FunctionType(ret_ty, param_tys, var_arg=variadic)
        fn = self.module.globals.get(symbol)
        if not isinstance(fn, ir.Function):
            fn = ir.Function(self.module, fnty, name=symbol)
            fn.linkage = "external"

        # Marshal each actual arg to the declared IR type. A bare
        # function ``Name`` passed to a ``c_ptr`` extern slot must be
        # lowered as the raw pcc function pointer — not wrapped via
        # ``py_cpy_wrap_pcc_<N>arg`` (which would leak a libpython
        # dependency into the no-libpython runtime archive) and not
        # boxed into a pcc ``PyFunc`` object (which the callee will
        # treat as an opaque pointer and dereference as a fn-ptr).
        ir_args: list[ir.Value] = []
        object_roots: list[tuple[ir.Value, bool]] = []
        object_arg_indices: list[tuple[int, int]] = []
        for i, a in enumerate(args):
            ctype = argtype_names[i] if i < len(argtype_names) else None
            v: ir.Value
            if ctype == "c_ptr" and isinstance(a, Name):
                fn_ir = self.functions.get(a.ident)
                if fn_ir is not None:
                    v = self.builder.bitcast(
                        fn_ir,
                        _CSTR,
                        name=self._fresh(f"extern.{a.ident}.fnptr"),
                    )
                    ir_args.append(v)
                    continue
            v = self._emit_expr_with_cpy_operand_cleanup(
                a,
                (),
                as_object=ctype == "c_obj",
                rooted_pcc_lifetimes=tuple(object_roots),
            )
            if ctype == "c_obj":
                # c_obj is a managed object, never an integer address. Pin it
                # before evaluating another operand or entering foreign code.
                owned = self._owned_release_needed(v, a)
                root = self._enter_container_temp_root(
                    v, self._fresh("extern.object.argument")
                )
                object_roots.append((root, owned))
                object_arg_indices.append((len(object_roots) - 1, i))
            elif i < len(argtype_names):
                want = self._EXTERN_CTYPE_IR[argtype_names[i]]
                if (
                    self._raw_addresses_are_ints()
                    and isinstance(want, ir.IntType)
                    and isinstance(v.type, ir.PointerType)
                    and v not in getattr(self, "_cpy_values", ())
                    and not self._value_is_never_gc_object(v)
                ):
                    # Unboxing can allocate an OverflowError. Keep the source
                    # object alive, and retain every evaluated argument until
                    # the foreign call returns, as an ordinary Python call does.
                    root = self._enter_container_temp_root(
                        v, self._fresh("extern.integer.argument")
                    )
                    object_roots.append((root, self._owned_release_needed(v, a)))
                previous_err = self._current_try_err_block()
                if object_roots:
                    target = previous_err if previous_err is not None else self._ensure_fn_err_exit()
                    self._try_err_block = self._make_cpy_operand_cleanup_block(
                        (), (), target, "extern.argument.cleanup",
                        rooted_pcc_lifetimes=tuple(object_roots),
                    )
                try:
                    v = self._coerce_to_extern(v, a.ty, want, argtype_names[i])
                finally:
                    self._try_err_block = previous_err
            ir_args.append(v)
        for root_index, argument_index in object_arg_indices:
            root_slot, _owned = object_roots[root_index]
            ir_args[argument_index] = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(root_slot)],
                name=self._fresh("extern.object.argument.current"),
            )
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"extern.{symbol}.ret")
        )
        if result_slot is not None:
            previous = self._current_try_err_block()
            saved_cpy = self._cpy_operand_cleanup_block
            target = previous if previous is not None else self._ensure_fn_err_exit()
            if object_roots:
                self._try_err_block = self._make_cpy_operand_cleanup_block(
                    (), (), target, "extern.result.cleanup",
                    rooted_pcc_lifetimes=tuple(object_roots),
                )
                self._cpy_operand_cleanup_block = self._try_err_block
            try:
                result = self.builder.call(fn, ir_args, name=call_name)
                # The declaration, not an annotation or symbol name, proves
                # NEW ownership. Publish before argument cleanup can park.
                self._publish_slot_call_owned(result_slot, result, label="extern c_obj result")
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            self._release_rooted_pcc_lifetimes(tuple(object_roots))
            return self.builder.load(result_slot, name=self._fresh("extern.object.result.current"))
        result = self.builder.call(fn, ir_args, name=call_name)
        if object_roots:
            if restype_name == "c_obj":
                # An owned result may alias an argument. An argument's unpin
                # then clears that same object's pin bit, so a pin alone does
                # not protect the result across the remaining argument cleanup.
                result_root = self._enter_container_temp_root(
                    result, self._fresh("extern.object.result")
                )
                self._release_rooted_pcc_lifetimes(tuple(object_roots))
                result = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(result_root)],
                    name=self._fresh("extern.object.result.current"),
                )
                # Reestablish the pin after possible alias unpins. The extra
                # pair balances metrics and covers root deregistration itself.
                self._gc_pin(result)
                self._leave_container_temp_root(result_root)
                self._gc_unpin(result)
            else:
                self._release_rooted_pcc_lifetimes(tuple(object_roots))
        if restype_name == "c_rawptr" and self._raw_addresses_are_ints():
            # Raw address results are ``int`` outside runtime-port mode.
            result = self.builder.ptrtoint(
                result, _I64, name=self._fresh(f"extern.{symbol}.addr")
            )
        elif restype_name == "c_obj":
            self._note_owned_object_value(result)
        return result

    def _extern_prior_pin(self, value: ir.Value) -> ir.Value:
        """Read a managed object's pin lease without dereferencing immediates."""
        bits = self.builder.ptrtoint(value, _I64)
        tagged = self.builder.icmp_unsigned(
            "!=", self.builder.and_(bits, ir.Constant(_I64, 1)), ir.Constant(_I64, 0)
        )
        nonnull = self.builder.icmp_unsigned("!=", value, ir.Constant(_CSTR, None))
        managed = self.builder.and_(nonnull, self.builder.not_(tagged))
        header = self.current_function.append_basic_block(self._fresh("extern.pin.header"))
        ready = self.current_function.append_basic_block(self._fresh("extern.pin.ready"))
        empty = self.builder._block
        self.builder.cbranch(managed, header, ready)
        self.builder.position_at_end(header)
        byte_pointer = self.builder.bitcast(value, _CSTR, name=self._fresh("extern.pin.bytes"))
        address = self.builder.gep(byte_pointer, [ir.Constant(_I64, PYOBJECTHEADER_FLAGS_OFFSET)])
        flags = self.builder.load(self.builder.bitcast(address, _I32.as_pointer()))
        previous = self.builder.zext(
            self.builder.and_(flags, ir.Constant(_I32, PY_FLAG_GC_PINNED)), _I64
        )
        header_exit = self.builder._block
        self.builder.branch(ready)
        self.builder.position_at_end(ready)
        prior = self.builder.phi(_I64, name=self._fresh("extern.pin.prior"))
        prior.add_incoming(ir.Constant(_I64, 0), empty)
        prior.add_incoming(previous, header_exit)
        return prior

    def _extern_enter_root(self, value, owned: bool, label: str):
        # Save the prior state before acquiring the temporary lease. Aliased
        # arguments acquire in source order and release in reverse order.
        prior = self._extern_prior_pin(value)
        slot = self._enter_container_temp_root(value, self._fresh(label))
        return (slot, owned, prior)

    def _extern_load_root(self, root):
        return self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(root[0])],
            name=self._fresh("extern.argument.current"),
        )

    def _extern_repin_root(self, root):
        # A later operand or a nested call may have temporarily pinned and
        # unpinned an alias. Roots remain authoritative; reestablish our one
        # lease without changing its metric before exposing any raw pointer.
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_lock"], [])
        value = self._extern_load_root(root)
        self._gc_unpin(value)
        self._gc_pin(value)
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_unlock"], [])
        return value

    def _extern_take_root(self, root):
        """Consume a source owner, then transfer the root's retained owner."""
        slot, owned, prior = root
        if owned:
            # store_root retained a distinct reference. Release the expression
            # (or c_obj result) owner while that root still protects the object;
            # then reload because a release can run code or safepoint.
            self._gc_release(self._extern_load_root(root), known_object=True)
        self._extern_repin_root(root)
        if not (self.current_func_def is not None
                and getattr(self, "_runtime_threads_enabled", False)):
            self._emit_gc_frame_leave_lifo_for_slot(slot)
        # Unlike container-root cleanup, this no-park handoff restores the
        # external pin state and clears the slot without a relocation window.
        return self.builder.call(
            self.runtime["pcc_gc_take_pinned_slot"],
            [self._as_gc_ptr(slot), prior],
            name=self._fresh("extern.argument.take"),
        )

    def _extern_release_roots(self, roots):
        for root in reversed(roots):
            value = self._extern_take_root(root)
            # take_pinned_slot transfers rather than decrefs the root owner.
            # Operand cleanup consumes it even when the source was borrowed.
            self._gc_release(value, known_object=True)

    def _extern_release_foreign_leases(self, leases):
        failed = ir.Constant(_I1, 0)
        for root, acquired in reversed(leases):
            status = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_release"],
                [self._as_gc_ptr(root[0]), acquired],
                name=self._fresh("extern.lease.release"),
            )
            bad = self.builder.icmp_signed("<", status, ir.Constant(_I64, 0))
            failed = self.builder.or_(failed, bad)
        return failed

    def _extern_cleanup_block(self, roots, target, leases=()):
        if not roots and not leases:
            return target
        cleanup = self.current_function.append_basic_block(self._fresh("extern.argument.cleanup"))
        saved = self.builder._block
        self.builder.position_at_end(cleanup)
        # An existing exception owns this edge. Release reports status only;
        # never replace the original error with a secondary cleanup failure.
        self._extern_release_foreign_leases(leases)
        self._extern_release_roots(roots)
        self.builder.branch(target)
        self.builder.position_at_end(saved)
        return cleanup

    def _extern_check_lease_cleanup(self, failed, roots):
        pending = self.builder.call(self.runtime["py_err_occurred"], [])
        clear = self.builder.icmp_signed("==", pending, ir.Constant(_I64, 0))
        error = self.current_function.append_basic_block(self._fresh("extern.lease.cleanup.error"))
        ready = self.current_function.append_basic_block(self._fresh("extern.lease.cleanup.ready"))
        self.builder.cbranch(self.builder.and_(failed, clear), error, ready)
        self.builder.position_at_end(error)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
        try:
            self._emit_builtin_exception_and_branch(
                "RuntimeError", "foreign-address lease cleanup failed", None
            )
        finally:
            self._try_err_block = previous
        self.builder.position_at_end(ready)

    def _emit_c_string_extern_call(self, decl, args, result_slot=None):
        """Keep managed c_str/c_obj arguments stable for one foreign call.

        c_str is a Python str argument in application modules. Raw addresses
        use c_rawptr; runtime/port pointer lanes bypass this managed bridge.
        No interior pointer exists while a later operand can allocate or run
        user code. c_obj results stay rooted through every argument cleanup.
        """
        symbol, ctypes, restype, variadic = decl
        ret_ty = self._EXTERN_CTYPE_IR[restype]
        fn = self.module.globals.get(symbol)
        if not isinstance(fn, ir.Function):
            fn = ir.Function(self.module, ir.FunctionType(
                ret_ty, [self._EXTERN_CTYPE_IR[name] for name in ctypes], var_arg=variadic
            ), name=symbol)
            fn.linkage = "external"
        roots = []
        arguments = []
        projections = []
        for index, expr in enumerate(args):
            ctype = ctypes[index] if index < len(ctypes) else None
            if ctype == "c_ptr" and isinstance(expr, Name):
                function = self.functions.get(expr.ident)
                if function is not None:
                    arguments.append(self.builder.bitcast(function, _CSTR))
                    continue
            previous = self._current_try_err_block()
            previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
            target = previous if previous is not None else self._ensure_fn_err_exit()
            cleanup = self._extern_cleanup_block(tuple(roots), target)
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            try:
                value = self._emit_expr_with_cpy_operand_cleanup(
                    expr, (), as_pcc_object=ctype in ("c_str", "c_obj")
                )
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = previous_cpy
            root = None
            if (isinstance(value.type, ir.PointerType)
                    and not isinstance(expr.ty, RawPointerType)
                    and value not in getattr(self, "_cpy_values", ())
                    and not self._value_is_never_gc_object(value)):
                root = self._extern_enter_root(
                    value, self._owned_release_needed(value, expr), "extern.argument.root"
                )
                roots.append(root)
            cleanup = self._extern_cleanup_block(tuple(roots), target)
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            try:
                if ctype == "c_str":
                    tag = self.builder.call(self.runtime["py_obj_type_tag"], [value])
                    valid = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_STR))
                    good = self.current_function.append_basic_block(self._fresh("extern.string.valid"))
                    bad = self.current_function.append_basic_block(self._fresh("extern.string.invalid"))
                    self.builder.cbranch(valid, good, bad)
                    self.builder.position_at_end(bad)
                    self._emit_builtin_exception_and_branch(
                        "TypeError", "c_str argument must be str", expr.span
                    )
                    self.builder.position_at_end(good)
                    projections.append((index, root, value, ctype))
                elif ctype == "c_obj":
                    projections.append((index, root, value, ctype))
                elif ctype is not None:
                    value = self._coerce_to_extern(value, expr.ty, self._EXTERN_CTYPE_IR[ctype], ctype)
                    if root is not None and ctype in ("c_ptr", "c_rawptr"):
                        projections.append((index, root, value, ctype))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = previous_cpy
            arguments.append(value)
        # Counted leases, separate from Boolean pins, survive a callback that
        # pins/unpins the same object. Only validated managed projections enter
        # this API; integer/raw-pointer operands retain their separate contract.
        leases = []
        for index, root, value, ctype in projections:
            if root is None or ctype not in ("c_str", "c_obj"):
                continue
            acquired = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_acquire"],
                [self._as_gc_ptr(root[0])], name=self._fresh("extern.lease.acquire"),
            )
            failed = self.builder.icmp_signed("<", acquired, ir.Constant(_I64, 0))
            error = self.current_function.append_basic_block(self._fresh("extern.lease.acquire.error"))
            ready = self.current_function.append_basic_block(self._fresh("extern.lease.acquire.ready"))
            self.builder.cbranch(failed, error, ready)
            self.builder.position_at_end(error)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            self._try_err_block = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
            try:
                overflow = self.current_function.append_basic_block(self._fresh("extern.lease.overflow"))
                invalid = self.current_function.append_basic_block(self._fresh("extern.lease.invalid"))
                self.builder.cbranch(self.builder.icmp_signed("==", acquired, ir.Constant(_I64, -2)), overflow, invalid)
                self.builder.position_at_end(overflow)
                self._emit_builtin_exception_and_branch("OverflowError", "foreign-address lease count overflow", None)
                self.builder.position_at_end(invalid)
                self._emit_builtin_exception_and_branch("RuntimeError", "foreign-address lease requires a stable managed owner", None)
            finally:
                self._try_err_block = previous
            self.builder.position_at_end(ready)
            leases.append((root, acquired))
        # Pin the complete set before deriving the first interior pointer.
        for root in roots:
            self._extern_repin_root(root)
        for index, root, value, ctype in projections:
            if root is not None:
                value = self._extern_load_root(root)
            if ctype == "c_str":
                value = self.builder.call(self.runtime["py_str_utf8"], [value],
                                          name=self._fresh("extern.string.utf8"))
            elif ctype in ("c_ptr", "c_rawptr"):
                value = self._coerce_to_extern(value, args[index].ty, _CSTR, ctype)
            arguments[index] = value
        if result_slot is not None:
            previous = self._current_try_err_block()
            saved_cpy = self._cpy_operand_cleanup_block
            target = previous if previous is not None else self._ensure_fn_err_exit()
            self._try_err_block = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                result = self.builder.call(fn, arguments, name=self._fresh("extern.result"))
                # Result ownership is independent even when it aliases an
                # argument. Its root survives all alias unpins and callbacks.
                self._publish_slot_call_owned(result_slot, result, label="extern c_obj result")
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            release_failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(release_failed, tuple(roots))
            self._extern_release_roots(tuple(roots))
            return self.builder.load(result_slot, name=self._fresh("extern.result.current"))
        result = self.builder.call(fn, arguments,
                                   name="" if isinstance(ret_ty, ir.VoidType) else self._fresh("extern.result"))
        if restype == "c_obj":
            result_root = self._extern_enter_root(result, True, "extern.result.root")
            prior = result_root[2]
            # If the result aliases an operand, discard this call's temporary
            # leases from the restored state. Earliest acquisition wins.
            for root in reversed(roots):
                alias = self.builder.icmp_unsigned("==", result, self._extern_load_root(root))
                prior = self.builder.select(alias, root[2], prior,
                                             name=self._fresh("extern.result.alias.prior"))
            result_root = (result_root[0], True, prior)
            release_failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(release_failed, tuple(roots) + (result_root,))
            self._extern_release_roots(tuple(roots))
            result = self._extern_take_root(result_root)
            self._note_owned_object_value(result)
        else:
            release_failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(release_failed, tuple(roots))
            self._extern_release_roots(tuple(roots))
            if restype == "c_rawptr":
                result = self.builder.ptrtoint(result, _I64, name=self._fresh("extern.result.address"))
        return result

    def _emit_gc_backend_query(self, symbol: str) -> ir.Value:
        """``symbol()`` (``pcc_gc_backend``/``pcc_gc_config_ensure``) with the
        initialized fast path inline: one flag load, then the selected
        backend, and the call only while the configuration is unparsed."""
        fn = self.module.globals.get(symbol)
        if not isinstance(fn, ir.Function):
            fn = ir.Function(self.module, ir.FunctionType(_I64, []), name=symbol)
            fn.linkage = "external"
        i32_ptr = _I32.as_pointer()
        ready_gv = self._declare_external_global("pcc_gc_config_initialized", _I8)
        ready_ptr = self.builder.bitcast(
            ready_gv, i32_ptr, name=self._fresh("gc.backend.ready.ptr")
        )
        ready = self.builder.load(ready_ptr, name=self._fresh("gc.backend.ready"))
        is_ready = self.builder.icmp_signed(
            "!=", ready, ir.Constant(_I32, 0), name=self._fresh("gc.backend.is_ready")
        )
        fast_block = self.current_function.append_basic_block(
            self._fresh("gc.backend.fast")
        )
        slow_block = self.current_function.append_basic_block(
            self._fresh("gc.backend.slow")
        )
        join_block = self.current_function.append_basic_block(
            self._fresh("gc.backend.join")
        )
        self.builder.cbranch(is_ready, fast_block, slow_block)
        self.builder.position_at_end(fast_block)
        selected_gv = self._declare_external_global("pcc_gc_backend_selected", _I8)
        selected_ptr = self.builder.bitcast(
            selected_gv, i32_ptr, name=self._fresh("gc.backend.selected.ptr")
        )
        selected = self.builder.load(
            selected_ptr, name=self._fresh("gc.backend.selected")
        )
        fast_value = self.builder.sext(
            selected, _I64, name=self._fresh("gc.backend.selected.i64")
        )
        self.builder.branch(join_block)
        self.builder.position_at_end(slow_block)
        slow_value = self.builder.call(fn, [], name=self._fresh("gc.backend.parsed"))
        self.builder.branch(join_block)
        self.builder.position_at_end(join_block)
        result = self.builder.phi(_I64, name=self._fresh("gc.backend"))
        result.add_incoming(fast_value, fast_block)
        result.add_incoming(slow_value, slow_block)
        return result

    def _pointer_or_address_operand(self, v: ir.Value, ty: "Type") -> ir.Value:
        """Lower a pointer-shaped operand from an object, an int address, or
        a dynamic value that may carry a tagged small-int address."""
        v_ty = getattr(v, "type", None)
        if isinstance(v_ty, ir.IntType):
            if v_ty.width != 64:
                v = self.builder.sext(v, _I64, name=self._fresh("extern.addr.sext"))
            return self.builder.inttoptr(v, _CSTR, name=self._fresh("extern.addr.ptr"))
        if (
            isinstance(v_ty, ir.PointerType)
            and isinstance(ty, IntType)
            and self._raw_addresses_are_ints()
        ):
            # A statically ``int`` address in its boxed form (a dyn-ABI helper
            # return, a local joined from two addresses) converts exactly like
            # the i64 lane above: its integer value is the address.
            address = self._to_int64(v, ty)
            self._emit_post_call_err_check(None)
            return self.builder.inttoptr(address, _CSTR, name=self._fresh("extern.int.addr"))
        if (
            isinstance(v_ty, ir.PointerType)
            and isinstance(ty, DynType)
            and self._raw_addresses_are_ints()
        ):
            if not self._ir_type_matches(v_ty, _CSTR):
                v = self.builder.bitcast(v, _CSTR, name=self._fresh("extern.dyn.cast"))
            bits = self.builder.ptrtoint(v, _I64, name=self._fresh("extern.dyn.bits"))
            tag = self.builder.and_(bits, ir.Constant(_I64, 1), name=self._fresh("extern.dyn.tag"))
            is_tagged = self.builder.icmp_unsigned(
                "!=", tag, ir.Constant(_I64, 0), name=self._fresh("extern.dyn.is_tagged")
            )
            untagged = self.builder.ashr(bits, ir.Constant(_I64, 1), name=self._fresh("extern.dyn.untag"))
            address = self.builder.inttoptr(untagged, _CSTR, name=self._fresh("extern.dyn.addr"))
            return self.builder.select(is_tagged, address, v, name=self._fresh("extern.dyn.ptr"))
        return v

    def _coerce_to_extern(
        self,
        v: ir.Value,
        ty: "Type",
        want: ir.Type,
        ctype_name: str,
    ) -> ir.Value:
        """Narrow bridge between pcc-native scalar types and the
        extern declaration's IR type. Handles int→i32/i64 truncate+
        sext, pcc str → i8*, bool zext."""
        if isinstance(want, ir.VoidType):
            return v
        if ctype_name == "c_obj":
            return self._emit_value_as_pcc_object_or_bridge(
                v, ty, "extern.object.boxed"
            )
        if ctype_name in {"c_str", "c_ptr", "c_rawptr"}:
            # A pointer-shaped parameter accepts either an object pointer or a
            # raw address.  Raw addresses are ``int`` in normal mode, so an
            # integer-typed value is converted with ``inttoptr``; a dynamic
            # value may carry a tagged small int holding an address (an
            # untyped helper parameter), which is untagged at run time.
            # pcc str is already i8* (points to PyStrObject); the underlying
            # C string still requires a runtime helper (P6C.1 sharp edge).
            # Pointer-lane modules (ports, freestanding) pass the operand
            # through untouched: their declarations may reuse an earlier ABI
            # declaration of the symbol whose parameter is not a pointer.
            if not self._raw_addresses_are_ints():
                return v
            if ctype_name == "c_rawptr":
                # Declared raw: an untyped value carrying a tagged address is
                # untagged at run time.
                return self._pointer_or_address_operand(v, ty)
            # c_ptr / c_str preserve the legacy pointer projection. A
            # tagged small int IS a legitimate object argument here (an int fd,
            # a boxed count), so no run-time untagging; only a statically
            # int-typed raw address is converted.
            v_ty = getattr(v, "type", None)
            if isinstance(v_ty, ir.IntType):
                if v_ty.width != 64:
                    v = self.builder.sext(v, _I64, name=self._fresh("extern.addr.sext"))
                return self.builder.inttoptr(v, _CSTR, name=self._fresh("extern.addr.ptr"))
            return v
        if isinstance(want, ir.IntType):
            i64 = self._to_int64(v, ty)
            if self._raw_addresses_are_ints() and isinstance(v.type, ir.PointerType):
                # The lane converter raises and returns a sentinel on overflow.
                # That sentinel must never reach a C call or pointer operand.
                self._emit_post_call_err_check(None)
            if (
                self._raw_addresses_are_ints()
                and getattr(ty, "name", "") in ("int", "dyn")
                and 1 < want.width < 64
            ):
                unsigned = ctype_name.startswith("c_uint")
                low = 0 if unsigned else -(1 << (want.width - 1))
                high = (1 << want.width) - 1 if unsigned else (1 << (want.width - 1)) - 1
                too_low = self.builder.icmp_signed("<", i64, ir.Constant(_I64, low))
                too_high = self.builder.icmp_signed(">", i64, ir.Constant(_I64, high))
                outside = self.builder.or_(too_low, too_high)
                bad = self.current_function.append_basic_block(self._fresh("extern.integer.overflow"))
                good = self.current_function.append_basic_block(self._fresh("extern.integer.checked"))
                self.builder.cbranch(outside, bad, good)
                self.builder.position_at_end(bad)
                self._emit_builtin_exception_and_branch(
                    "OverflowError", "Python int too large to convert to " + ctype_name, None
                )
                self.builder.position_at_end(good)
            if want.width == 64:
                return i64
            if want.width < 64:
                return self.builder.trunc(
                    i64,
                    want,
                    name=self._fresh(f"extern.trunc{want.width}"),
                )
            return self.builder.sext(
                i64,
                want,
                name=self._fresh(f"extern.sext{want.width}"),
            )
        if isinstance(want, ir.DoubleType):
            return self._to_double(v, ty)
        return v
