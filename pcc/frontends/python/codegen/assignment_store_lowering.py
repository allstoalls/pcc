"""Assignment storage helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Attr, DynType, Expr, IntType, Name, NoneLit, NoneType, Slice,
    Subscript, TupleExpr, Type,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.local_bound_lowering import mark_local_bound
from pcc.frontends.python.codegen.cpy_import_state import (
    live_import_expr_binding,
    retire_live_import_staging,
)
from pcc.frontends.python.codegen.attr_store_lowering import (
    emit_live_import_attribute_store,
    live_import_attribute_store,
)

_I8 = ir.IntType(8)
_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class AssignmentStoreLoweringMixin:
    def _publish_module_global_assignment(
        self,
        name: str,
        value: ir.Value,
        declared_ty: Type,
        *,
        is_cpy_value: bool = False,
        value_is_owned: bool = False,
    ) -> None:
        """Expose a rooted global assignment through the module object.

        Callers must install pointer values in the registered module-global
        root before entering this helper: ``py_module_attr_set`` allocates and
        can run a tracing collection.  Releasing an owned RHS here, after the
        module dictionary has retained it, keeps that temporary live across
        the publication call without leaking its original reference.
        """
        if is_cpy_value or self._is_valueclass_payload_type(declared_ty):
            return
        published = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            declared_ty,
        )
        module_name = self.ast_module.name or "__main__"
        module_name_ptr = self._pooled_cstr_ptr(
            module_name,
            ".pcc.assign.binding.module",
        )
        self.builder.call(
            self.runtime["py_module_attr_set"],
            [module_name_ptr, self._attr_name_ptr(name), published],
            name=self._fresh(f"pcc.assign.binding.publish.{name}"),
        )
        if value_is_owned and isinstance(value.type, ir.PointerType):
            self._gc_release(
                value,
                self._release_context_label("module_store_tmp"),
            )
        if not isinstance(value.type, ir.PointerType):
            self._gc_release(
                published,
                self._release_context_label("module_publish_box"),
            )
        # The dictionary owns this binding. The registered global remains an
        # empty staging slot so replacement/deletion can finalize its old value.
        retire_live_import_staging(self, name)

    def _coerce_unpack_name_like(self, lhs: Expr) -> Expr:
        if isinstance(lhs, Name):
            return lhs
        try:
            ident = lhs.ident
        except AttributeError:
            return lhs
        if ident is None:
            return lhs
        try:
            span = lhs.span
        except AttributeError:
            span = None
        try:
            ty = lhs.ty
        except AttributeError:
            ty = DynType(name="dyn")
        return Name(
            span=span,
            ty=ty,
            ident=ident,
        )

    def _store_unpack_root_target(self, lhs: Expr, root, value_ty: Type) -> None:
        """Publish a retained literal element without exposing a stale SSA value."""
        lhs = self._coerce_unpack_name_like(lhs)
        if isinstance(lhs, TupleExpr):
            previous = self._current_try_err_block()
            error = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            items = []
            try:
                for index in range(len(lhs.elems)):
                    item = self._new_slot_call_root("unpack.nested.item")
                    items.append(item)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(items), error)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call(
                        "py_obj_getitem_i64", (root,), result_slot=item,
                        suffix_args=(ir.Constant(_I64, index),), span=lhs.span,
                    )
                for index, target in enumerate(lhs.elems):
                    self._store_unpack_root_target(target, items[index], DynType(name="dyn"))
                self._release_slot_call_roots(tuple(items))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return
        if isinstance(lhs, Attr) and live_import_attribute_store(self, lhs):
            emit_live_import_attribute_store(self, lhs, root)
            return
        if isinstance(lhs, Subscript) and live_import_expr_binding(self, lhs.obj):
            receiver = self._emit_slot_call_operand(lhs.obj, "import.item.receiver")
            previous = self._current_try_err_block()
            error = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            roots = [receiver]
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error)
                self._cpy_operand_cleanup_block = self._try_err_block
                if isinstance(lhs.idx, Slice):
                    for part in (lhs.idx.lo, lhs.idx.hi, lhs.idx.step):
                        if part is None:
                            part = NoneLit(span=lhs.idx.span, ty=NoneType(name="None"))
                        roots.append(self._emit_slot_call_operand(part, "import.item.bound"))
                        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error)
                        self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call("py_obj_set_slice", tuple(roots) + (root,), span=lhs.span)
                else:
                    key = self._emit_slot_call_operand(lhs.idx, "import.item.key")
                    roots.append(key)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call("py_obj_assign_subscript", (receiver, key, root), span=lhs.span)
                self._release_slot_call_roots(tuple(roots))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return
        if isinstance(lhs, Name):
            entry = self.env.get(lhs.ident)
            global_entry = self._module_globals.get(lhs.ident)
            declared = entry[2] if entry is not None else (global_entry[1] if global_entry is not None else lhs.ty)
            physical = entry[1] if entry is not None else (global_entry[0].value_type if global_entry is not None else self._local_slot_ir_type(lhs.ident, declared))
            if self._is_valueclass_payload_type(declared) and isinstance(physical, ir.LiteralStructType):
                payload = self._emit_valueclass_payload_from_root(root, declared)
                self._store_unpack_target(lhs, payload, declared, value_is_owned=False)
                self._clear_owned_valueclass_payload(self._valueclass_payload_source(payload)[1])
                return
        local_name = isinstance(lhs, Name) and not (
            lhs.ident in self._module_globals
            and (self.current_func_def is None or lhs.ident in self._current_global_names)
        )
        if local_name:
            slot = self.env.get(lhs.ident)
            ir_ty = slot[1] if slot is not None else self._local_slot_ir_type(lhs.ident, lhs.ty)
            if isinstance(ir_ty, ir.PointerType) and self._ir_type_matches(ir_ty, _CSTR):
                if slot is None:
                    alloca = self._alloca_in_entry(_CSTR, name=lhs.ident + ".addr", init_null=True)
                    declared_ty = self._local_slot_decl_type(lhs.ident, lhs.ty)
                    self.env[lhs.ident] = (alloca, _CSTR, declared_ty)
                else:
                    alloca, _ir_ty, declared_ty = slot
                # A borrowed parameter's registration has a different tracing
                # contract. Rebound parameters normally have a prepared owned
                # slot already; keep that distinction physical if one remains.
                if (lhs.ident in getattr(self, "_borrowed_gc_rooted_local_names", ())
                        and self._owned_local_flag_for(lhs.ident, alloca) is None):
                    alloca = self._alloca_in_entry(_CSTR, name=lhs.ident + ".unpack.addr", init_null=True)
                    self.env[lhs.ident] = (alloca, _CSTR, declared_ty)
                self._ensure_local_gc_frame_root(lhs.ident, alloca, _CSTR)
                self._emit_release_owned_local_if_flagged(lhs.ident, alloca)
                # An unowned prior binding is borrowed: empty the destination
                # without consuming its source owner's reference.
                self.builder.store(ir.Constant(_CSTR, None), alloca)
                moved = self.builder.call(
                    self.runtime["pcc_gc_root_move"],
                    [self._as_gc_ptr(alloca), self._as_gc_ptr(root)],
                    name=self._fresh("unpack.literal.move"),
                )
                self._slot_call_check_status(moved, "literal assignment move", lhs.span)
                self._cpy_env_flags.pop(lhs.ident, None)
                self.env_class_hint.pop(lhs.ident, None)
                self._owned_local_names.add(lhs.ident)
                self._owned_local_has_value.add(lhs.ident)
                if lhs.ident in getattr(self, "_current_param_names", set()):
                    self._for_target_owned_names.add(lhs.ident)
                flag = self._ensure_owned_local_flag(lhs.ident, alloca)
                self.builder.store(ir.Constant(_I1, 1), flag)
                if lhs.ident in getattr(self, "_planned_exact_int_local_names", set()):
                    self._exact_int_env_flags[lhs.ident] = True
                mark_local_bound(self, lhs.ident)
                return
        # Scalar conversion and container/global stores can run arbitrary
        # callbacks. A counted address lease keeps their raw operand valid;
        # the temporary root continues owning it through success or failure.
        token = self.builder.call(
            self.runtime["pcc_gc_foreign_lease_acquire"], [self._as_gc_ptr(root)],
            name=self._fresh("unpack.literal.lease"),
        )
        self._slot_call_check_status(token, "literal assignment lease", lhs.span)
        previous = self._current_try_err_block()
        saved_cpy = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((), previous, ((root, token),))
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            value = self.builder.load(root, name=self._fresh("unpack.literal.current"))
            self._store_unpack_target(lhs, value, value_ty, value_is_owned=False)
            released = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_release"], [self._as_gc_ptr(root), token],
                name=self._fresh("unpack.literal.release"),
            )
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
            self._slot_call_check_status(released, "literal assignment lease release", lhs.span)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _store_unpack_target(
        self,
        lhs: Expr,
        value: ir.Value,
        value_ty: Type,
        value_is_owned: Optional[bool] = None,
    ) -> None:
        lhs = self._coerce_unpack_name_like(lhs)
        if value_is_owned is None:
            unpack_value_is_owned = self._unpack_target_value_is_owned(value_ty)
        else:
            unpack_value_is_owned = value_is_owned
        if isinstance(lhs, Subscript):
            self._store_value_at_subscript(
                lhs,
                value,
                value_ty,
                release_value=unpack_value_is_owned,
            )
            return
        if isinstance(lhs, Attr):
            self._store_value_at_attr(
                lhs,
                value,
                value_ty,
                release_value=unpack_value_is_owned,
            )
            return
        if isinstance(lhs, Name):
            module_global_target = lhs.ident in self._module_globals and (
                self.current_func_def is None or lhs.ident in self._current_global_names
            )
            if module_global_target:
                # Module destructuring has one binding, not a shadow local
                # plus a global.  Creating an alloca here made subsequent
                # statements in the same module read the still-null local
                # even though the unpack value had been published globally.
                self._store_value_at_name(
                    lhs,
                    value,
                    value_ty,
                    value_is_owned=unpack_value_is_owned,
                )
                return
            slot = self.env.get(lhs.ident)
            target_ty = lhs.ty
            # The function-level representation plan is authoritative here.
            # A destructuring target can be re-inferred as Dyn from the
            # iterable element even though the same local was already joined
            # to one exact-int object slot at function entry.  Re-checking the
            # statement-local AST type would bypass the rooted replacement
            # protocol and either store an i64 into a ptr alloca or install an
            # owned pointer with no write barrier.
            planned_exact_int = lhs.ident in getattr(
                self,
                "_planned_exact_int_local_names",
                set(),
            )
            if slot is None and target_ty is not None:
                if self._is_object(target_ty):
                    ir_ty = self._local_slot_ir_type(lhs.ident, target_ty)
                    if isinstance(ir_ty, ir.PointerType) and self._ir_type_matches(
                        ir_ty, _CSTR
                    ):
                        alloca = self._alloca_in_entry(
                            ir_ty,
                            name=f"{lhs.ident}.addr",
                            init_null=True,
                        )
                        self.env[lhs.ident] = (alloca, ir_ty, target_ty)
                        # Private comprehension locals also live in module
                        # initializers. Register their empty physical slot
                        # before unpacking can publish an owned item into it.
                        self._ensure_local_gc_frame_root(
                            lhs.ident, alloca, ir_ty, allow_module=True,
                        )
                        slot = self.env[lhs.ident]
            if planned_exact_int:
                self._store_value_at_name(
                    lhs,
                    value,
                    value_ty,
                    value_is_owned=unpack_value_is_owned,
                )
                return
            slot_for_release = slot
            if slot_for_release is not None and unpack_value_is_owned:
                alloca, ir_ty, _decl_ty = slot_for_release
                if (
                    isinstance(ir_ty, ir.PointerType)
                    and self._ir_type_matches(ir_ty, _CSTR)
                    and lhs.ident not in getattr(self, "_current_global_names", set())
                    and lhs.ident not in getattr(self, "_current_param_names", set())
                    and not getattr(self, "_cpy_env_flags", {}).get(lhs.ident, False)
                ):
                    self._emit_release_owned_local_if_flagged(lhs.ident, alloca)
            self._store_value_at_name(
                lhs,
                value,
                value_ty,
                value_is_owned=unpack_value_is_owned,
            )
            if not unpack_value_is_owned:
                if slot_for_release is not None:
                    alloca, _ir_ty, _decl_ty = slot_for_release
                    self._discard_owned_local_gc_root(lhs.ident, alloca)
                self._owned_local_names.discard(lhs.ident)
                self._owned_local_has_value.discard(lhs.ident)
            self._mark_owned_local_for_unpack_target(
                lhs,
                value_ty,
                unpack_value_is_owned,
            )
            slot_after_store = self.env.get(lhs.ident)
            flag_alloca = None
            if slot_after_store is not None:
                flag_alloca = slot_after_store[0]
            if lhs.ident in self._owned_local_names:
                flag = self._ensure_owned_local_flag(lhs.ident, flag_alloca)
                self.builder.store(ir.Constant(_I1, 1), flag)
                self._owned_local_has_value.add(lhs.ident)
            else:
                flag = self._owned_local_flag_for(lhs.ident, flag_alloca)
                if flag is not None:
                    self.builder.store(ir.Constant(_I1, 0), flag)
                self._owned_local_has_value.discard(lhs.ident)
            return
        lhs_kind = type(lhs).__name__
        if isinstance(lhs, TupleExpr) or lhs_kind == "TupleExpr":
            for i, sub in enumerate(lhs.elems):
                idx_box = self.builder.call(
                    self.runtime["py_int_from_i64"],
                    [ir.Constant(_I64, i)],
                    name=self._fresh("unpack.nested.idx.box"),
                )
                elem = self.builder.call(
                    self.runtime["py_obj_getitem"],
                    [value, idx_box],
                    name=self._fresh(f"unpack.nested.{i}"),
                )
                self._store_unpack_target(
                    sub,
                    elem,
                    DynType(name="dyn"),
                    value_is_owned=True,
                )
            return
        raise NotImplementedError(
            "Layer 1 tuple-unpack target kind " + lhs_kind + " not supported"
        )

    def _store_value_at_name(
        self,
        target: Name,
        value: ir.Value,
        value_ty: Type,
        *,
        value_is_owned: bool = False,
    ) -> None:
        """Store a pre-computed SSA value to a local / module global."""
        self.env_class_hint.pop(target.ident, None)
        if not hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags = {}
        if value in getattr(self, "_cpy_values", ()):
            self._cpy_env_flags[target.ident] = True
        else:
            self._cpy_env_flags.pop(target.ident, None)

        module_globals = self._module_globals
        if (
            self.current_func_def is not None
            and target.ident in self._current_global_names
        ):
            self._ensure_module_global_name(target.ident, target.ty)
        if target.ident in module_globals and (
            self.current_func_def is None or target.ident in self._current_global_names
        ):
            gv, declared_ty = module_globals[target.ident]
            boxed_module_int = (
                isinstance(declared_ty, IntType)
                and isinstance(gv.value_type, ir.PointerType)
            )
            if boxed_module_int:
                if not isinstance(value.type, ir.PointerType):
                    value = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        value,
                        value_ty,
                    )
            else:
                value = self._coerce(value, value_ty, declared_ty)
            if value in getattr(self, "_cpy_values", ()):
                self._cpy_module_flags[target.ident] = True
                is_cpy_value = True
            else:
                self._cpy_module_flags.pop(target.ident, None)
                is_cpy_value = False
            self._store_module_global_root_value(
                gv,
                value,
                declared_ty=declared_ty,
                value_is_owned=False,
                is_cpy_value=is_cpy_value,
            )
            self._publish_module_global_assignment(
                target.ident,
                value,
                declared_ty,
                is_cpy_value=is_cpy_value,
                value_is_owned=value_is_owned,
            )
            return

        slot = self.env.get(target.ident)
        if slot is None:
            target_ty = target.ty
            if not (
                self._is_scalar(target_ty)
                or self._is_object(target_ty)
                or self._is_valueclass_payload_type(target_ty)
            ):
                raise NotImplementedError(
                    f"Layer 1 tuple-unpack target {target.ident!r} has "
                    f"unsupported type {type(target_ty).__name__}"
                )
            ir_ty = self._local_slot_ir_type(target.ident, target_ty)
            init_null = isinstance(ir_ty, ir.PointerType) and self._ir_type_matches(
                ir_ty, _CSTR
            )
            alloca = self._alloca_in_entry(
                ir_ty,
                name=f"{target.ident}.addr",
                init_null=init_null,
            )
            self.env[target.ident] = (
                alloca,
                ir_ty,
                self._local_slot_decl_type(target.ident, target_ty),
            )
            slot = self.env[target.ident]

        alloca, _ir_ty, declared_ty = slot
        # Membership already means the function-level analysis proved this
        # semantic int local needs the object projection on every edge.  The
        # current unpack target may carry Dyn after iterable inference, so its
        # local AST type cannot override that established slot contract.
        planned_exact_int = target.ident in getattr(
            self,
            "_planned_exact_int_local_names",
            set(),
        )
        if planned_exact_int:
            if not (
                isinstance(_ir_ty, ir.PointerType)
                and self._ir_type_matches(_ir_ty, _CSTR)
            ):
                raise NotImplementedError(
                    "planned exact-int local does not have object storage: "
                    + target.ident
                )
            incoming = value
            incoming_owned = bool(value_is_owned)
            if incoming in getattr(self, "_cpy_values", ()):
                incoming = self._emit_value_as_pcc_object_or_bridge(
                    incoming,
                    value_ty,
                    target.ident + ".exact.unpack.bridge",
                )
                incoming_owned = True
            elif not isinstance(incoming.type, ir.PointerType):
                incoming = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    incoming,
                    value_ty,
                )
                incoming_owned = True
            if not incoming_owned:
                incoming = self._gc_retain(
                    incoming,
                    name=self._fresh(target.ident + ".exact.unpack.retain"),
                )
            # The slot now owns a pcc PyInt object even when the unpacked
            # value originated in CPython.  Leaving the foreign-domain flag
            # behind makes the next Name load reinterpret this pcc pointer as
            # a CPython PyObject*.
            self._cpy_env_flags.pop(target.ident, None)
            self._gc_pin(incoming)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(alloca), incoming],
            )
            self._gc_unpin(incoming)
            # Root-store retained the replacement for the local slot and
            # released its previous owner.  Consume the owned temporary
            # supplied by the unpack expression (or the retain above).
            self._gc_release(
                incoming,
                self._release_context_label(
                    "exact-unpack-incoming:" + target.ident
                ),
            )
            self.env[target.ident] = (alloca, _CSTR, target.ty)
            self._exact_int_env_flags[target.ident] = True
            self._owned_local_names.add(target.ident)
            self._owned_local_has_value.add(target.ident)
            self._ensure_owned_local_gc_root(target.ident, alloca, _CSTR)
            flag = self._ensure_owned_local_flag(target.ident, alloca)
            self.builder.store(ir.Constant(_I1, 1), flag)
            mark_local_bound(self, target.ident)
            return
        if (
            target.ident in getattr(self, "_planned_object_local_names", set())
            and isinstance(_ir_ty, ir.PointerType)
            and self._ir_type_matches(_ir_ty, _CSTR)
        ):
            # The slot owns the common object representation. Raw scalars
            # need boxing; an existing object must not be truth-unboxed just
            # because this binding's semantic type is Boolean.
            if not isinstance(value.type, ir.PointerType):
                value = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    value_ty,
                )
        else:
            value = self._coerce(value, value_ty, declared_ty)
        if self._is_valueclass_payload_type(declared_ty) and isinstance(_ir_ty, ir.LiteralStructType):
            self._ensure_valueclass_payload_gc_roots(target.ident, alloca, declared_ty, borrowed=False)
            self._copy_valueclass_payload(alloca, value, declared_ty)
        else:
            self.builder.store(value, alloca)
        mark_local_bound(self, target.ident)

    def _store_value_at_subscript(
        self,
        target: Subscript,
        value: ir.Value,
        value_ty: Type,
        *,
        release_value: bool = False,
    ) -> None:
        """Runtime subscript store given a pre-computed value."""
        # ``os.environ[key] = value`` store hook (native_os.py): CPython
        # mapping semantics via py_os_environ_setitem instead of a
        # generic py_obj_setitem on the (non-native) os.environ object.
        if self._emit_native_os_environ_setitem_value(
            target,
            value,
            value_ty,
            release_value=release_value,
        ):
            return
        obj = self._emit_expr(target.obj)
        k_obj = self._emit_subscript_key_object(target.idx)
        v_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            value_ty,
        )
        self.builder.call(
            self.runtime["py_obj_setitem"],
            [obj, k_obj, v_obj],
        )
        # py_obj_setitem raises for user-visible failures (out-of-range list
        # store -> IndexError); without this check the pending exception
        # skips the enclosing try/except and corrupts later dispatch.
        self._emit_post_call_err_check(target.span)
        if release_value and isinstance(v_obj.type, ir.PointerType):
            if v_obj not in getattr(self, "_cpy_values", ()):
                self._gc_release(v_obj)

    def _store_value_at_attr(
        self,
        target: Attr,
        value: ir.Value,
        value_ty: Type,
        *,
        release_value: bool = False,
    ) -> None:
        """Runtime attribute store given a pre-computed value."""
        obj = self._emit_expr(target.obj)
        v_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            value_ty,
        )
        name_ptr = self._attr_name_ptr(target.name)
        status = self.builder.call(
            self.runtime["py_obj_setattr"],
            [obj, name_ptr, v_obj],
            name=self._fresh(f"setattr.{target.name}.rc"),
        )
        self._emit_attribute_error_if_status_failed(
            status,
            target.name,
            target.span,
        )
        if release_value and isinstance(v_obj.type, ir.PointerType):
            if v_obj not in getattr(self, "_cpy_values", ()):
                self._gc_release(v_obj)
