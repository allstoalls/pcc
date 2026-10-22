"""Attribute-store lowering helpers for L1CodeGen."""

from __future__ import annotations

from pcc.ir.compat import ir

from pcc.frontends.python.export_meta import decode_type
from pcc.frontends.python.py_ast import Attr, DynType, Expr, IntType, Name, Type
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.cpy_import_state import live_import_expr_binding


_I32 = ir.IntType(32)


def live_import_attribute_store(host, target):
    # Keep the existing proven managed-provider cell+dictionary writer.
    # Other imported object receivers must use their actual current value.
    return live_import_expr_binding(host, target.obj) and not (
        isinstance(target.obj, Name)
        and target.obj.ident in getattr(host, "_native_module_aliases", {})
    )


def emit_live_import_attribute_store(host, target, value_root):
    receiver = host._emit_slot_call_operand(target.obj, "import.store.receiver")
    previous = host._current_try_err_block()
    error = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cpy = host._cpy_operand_cleanup_block
    host._try_err_block = host._slot_call_cleanup_block((receiver,), error)
    host._cpy_operand_cleanup_block = host._try_err_block
    try:
        status = host._slot_call_runtime_call(
            "py_obj_setattr", (receiver, value_root),
            suffix_args=(host._attr_name_ptr(host.class_lowering.private_field_key(target.name)),),
            argument_order=(0, 2, 1), span=target.span,
        )
        host._emit_attribute_error_if_status_failed(status, target.name, target.span)
        host._release_slot_call_roots((receiver,))
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy


class AttrStoreLoweringMixin:
    def _typed_instance_field_slot(self, target: Attr):
        """Slot index for ``<Name>.<field>`` when the receiver's class is
        statically known, the field is a declared instance field, and neither
        the class nor any base overrides ``__setattr__``; else ``None``."""
        if not isinstance(target.obj, Name) or not hasattr(self, "class_lowering"):
            return None
        if getattr(self, "_cpy_env_flags", {}).get(target.obj.ident, False):
            return None
        hint = self._class_hint_for_expr(target.obj)
        if hint is None:
            return None
        classes = self.class_lowering.classes
        info = classes.get(hint)
        if info is None:
            return None
        seen: set[str] = set()
        pending = [info]
        while pending:
            current = pending.pop()
            if current.name in seen:
                continue
            seen.add(current.name)
            if "__setattr__" in getattr(current, "methods", {}):
                return None
            for base in getattr(current, "bases_ast", ()) or ():
                base_name = getattr(base, "ident", None)
                base_info = classes.get(base_name) if base_name else None
                if base_info is not None:
                    pending.append(base_info)
        if self._class_has_property(info.name, target.name):
            return None
        return self.class_lowering.lookup_field_index(info, target.name)

    def _emit_attr_store_value(
        self,
        target: Attr,
        value: ir.Value,
        value_ty: Type,
    ) -> None:
        runtime_attr_name = target.name
        lexical_class = self.current_class
        if lexical_class is not None and hasattr(self, "class_lowering"):
            runtime_attr_name = self.class_lowering.mangle_private_attr_name(
                lexical_class,
                target.name,
            )
        if isinstance(target.obj, Name):
            if (
                hasattr(self, "class_lowering")
                and target.obj.ident in self.class_lowering.classes
            ):
                info = self.class_lowering.classes[target.obj.ident]
                metaclass_descr = self._metaclass_data_descriptor_info(
                    info,
                    target.name,
                )
                if metaclass_descr is not None:
                    value_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        value,
                        value_ty,
                    )
                    if self._emit_metaclass_data_descriptor_set(
                        info,
                        target.name,
                        value_obj,
                    ):
                        return
                if self.class_lowering.emit_class_attr_store(
                    info,
                    target.name,
                    value,
                    value_ty,
                ):
                    if not hasattr(self, "_class_attr_runtime_state"):
                        self._class_attr_runtime_state = {}
                    if getattr(self, "_class_attr_mutation_in_loop_depth", 0):
                        state = "unknown"
                    else:
                        state = "live"
                    self._class_attr_runtime_state[(info.name, target.name)] = state
                    return
            if (
                self.current_class is not None
                and target.obj.ident == "cls"
                and self.current_method_kind == "classmethod"
            ):
                cls_obj = self._emit_expr(target.obj)
                value_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    value_ty,
                )
                status = self.builder.call(
                    self.runtime["py_obj_setattr"],
                    [cls_obj, self._attr_name_ptr(runtime_attr_name), value_obj],
                    name=self._fresh(f"cls.setattr.{target.name}.rc"),
                )
                self._emit_attribute_error_if_status_failed(
                    status,
                    target.name,
                    target.span,
                )
                return
            builtin_module = self._native_builtin_module_for_name(target.obj.ident)
            if builtin_module is not None:
                value_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    value_ty,
                )
                gv = self._native_module_attr_global(builtin_module, target.name)
                old_value = self.builder.load(
                    gv,
                    name=self._fresh(f"modattr.{target.name}.old"),
                )
                self._gc_unpin(old_value)
                self._gc_pin(value_obj)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [
                        self._as_gc_ptr(
                            gv,
                            name=self._fresh(f"modattr.{target.name}.slot"),
                        ),
                        value_obj,
                    ],
                )
                return
            native_module = getattr(
                self,
                "_native_module_aliases",
                {},
            ).get(target.obj.ident)
            if native_module is not None:
                info = (self._native_module_exports or {}).get(native_module, {}).get(target.name)
                kind = info.get("kind") if info is not None else None
                if kind not in ("module_global", "constant"):
                    raise NotImplementedError(
                        "compiled module attribute assignment requires a value export: "
                        + native_module + "." + target.name
                    )
                if kind == "constant" and not info.get("has_module_storage"):
                    raise NotImplementedError(
                        "compiled module constant has no provider storage metadata: "
                        + native_module + "." + target.name
                    )
                declared_ty = decode_type(info.get("value_ty", ("dyn",))) or DynType(name="dyn")
                if isinstance(declared_ty, IntType):
                    if "box_int_abi" not in info:
                        raise NotImplementedError("compiled module integer storage ABI is unknown")
                    storage_ty = self._abi_ir_type(declared_ty, box_int_abi=bool(info["box_int_abi"]))
                elif self._is_object(declared_ty):
                    storage_ty = ir.IntType(8).as_pointer()
                else:
                    storage_ty = self._storage_ir_type(declared_ty)
                if not isinstance(storage_ty, ir.PointerType):
                    raise NotImplementedError(
                        "compiled module attribute assignment to raw typed storage is not supported: "
                        + native_module + "." + target.name
                    )
                if info.get("storage_owner") != "managed":
                    raise NotImplementedError(
                        "compiled module attribute mutation requires proven managed provider storage: "
                        + native_module + "." + target.name
                    )
                owning_module = info.get("owning_module", native_module)
                export_name = info.get("export_name", target.name)
                if owning_module != native_module or export_name != target.name:
                    raise NotImplementedError(
                        "compiled reexport attribute mutation needs binding-local storage metadata: "
                        + native_module + "." + target.name
                    )
                symbol = self._module_global_symbol_name(owning_module, export_name)
                gv = self.module.globals.get(symbol)
                if gv is None:
                    gv = ir.GlobalVariable(self.module, storage_ty, name=symbol)
                    gv.linkage = "external"
                elif not self._ir_type_matches(gv.value_type, storage_ty):
                    raise NotImplementedError("compiled module attribute storage ABI mismatch: " + symbol)
                value_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    value_ty,
                )
                # Both RHS and displaced value need an owned, pinned keeper:
                # namespace publication allocates, and dropping the last old
                # reference may run a finalizer which reads both representations.
                new_root = self._enter_container_temp_root(value_obj, "module.attr.new")
                old_value = self.builder.load(gv, name=self._fresh("module.attr.old"))
                old_root = self._enter_container_temp_root(old_value, "module.attr.old")
                value_obj = self.builder.load(new_root, name=self._fresh("module.attr.new.live"))
                old_value = self.builder.load(old_root, name=self._fresh("module.attr.old.live"))
                same = self.builder.icmp_unsigned("==", old_value, value_obj)
                preserve = self.builder.select(same, ir.Constant(ir.IntType(64), 64), ir.Constant(ir.IntType(64), 0))
                # This is the new canonical-global pin; the keeper has a
                # separate metric acquisition. Do not clear the old pin yet.
                self._gc_pin(value_obj)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(gv, name=self._fresh("module.attr.slot")), value_obj],
                )
                self.builder.call(
                    self.runtime["py_module_attr_set"],
                    [self._pooled_cstr_ptr(native_module, ".pcc.attr.binding.module"),
                     self._attr_name_ptr(target.name), value_obj],
                    name=self._fresh("module.attr.publish"),
                )
                # Unregister in LIFO order while both objects remain pinned.
                # In threaded functions these entry slots stay registered
                # until the ordinary function epilogue; take clears the slot.
                persistent = self.current_func_def is not None and getattr(self, "_runtime_threads_enabled", False)
                if not persistent:
                    self._emit_gc_frame_leave_lifo_for_slot(old_root)
                    self._emit_gc_frame_leave_lifo_for_slot(new_root)
                new_owned = self.builder.call(
                    self.runtime["pcc_gc_take_pinned_slot"],
                    [self._as_gc_ptr(new_root), ir.Constant(ir.IntType(64), 64)],
                    name=self._fresh("module.attr.new.take"),
                )
                self._gc_release(new_owned)
                if not isinstance(value.type, ir.PointerType):
                    self._gc_release(new_owned)
                # Publish has finished before either old owner is released.
                # For self-assignment keep the replacement global's PIN bit
                # while balancing both displaced-global and keeper metrics.
                old_live = self.builder.load(old_root, name=self._fresh("module.attr.old.live"))
                self._gc_unpin(old_live)
                old_owned = self.builder.call(
                    self.runtime["pcc_gc_take_pinned_slot"],
                    [self._as_gc_ptr(old_root), preserve],
                    name=self._fresh("module.attr.old.take"),
                )
                self._gc_release(old_owned)
                self._emit_post_call_err_check(target.span)
                return
        # Property setter fast path.
        if isinstance(target.obj, Name):
            hint = self.env_class_hint.get(target.obj.ident)
            if hint is not None:
                hint_info = self.class_lowering.classes.get(hint)
                in_init = (
                    self.current_func_def is not None
                    and self.current_func_def.name == "__init__"
                    and self.current_class is hint_info
                )
                if (
                    hint_info is not None
                    and getattr(hint_info, "dataclass_frozen", False)
                    and target.name in hint_info.field_names
                    and not in_init
                ):
                    self._emit_builtin_exception_and_branch(
                        "AttributeError",
                        "cannot assign to field",
                        target.span,
                    )
                    return
                info = self._resolve_property_setter_mro(hint, target.name)
                if info is not None and not self._property_accessor_takes_protocol_args(
                    info, target.name, "setter", info.property_setters[target.name],
                ):
                    # The runtime property call binds the setter's other
                    # parameters; a field-slot store would bypass it.
                    obj_val = self._emit_expr(target.obj)
                    value_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        value,
                        value_ty,
                    )
                    status = self.builder.call(
                        self.runtime["py_obj_setattr"],
                        [obj_val, self._attr_name_ptr(runtime_attr_name), value_obj],
                        name=self._fresh(f"prop.setattr.{target.name}.rc"),
                    )
                    self._emit_attribute_error_if_status_failed(
                        status,
                        target.name,
                        target.span,
                    )
                    return
                if info is not None:
                    setter_fn = info.property_setters[target.name]
                    obj_val = self._emit_expr(target.obj)
                    if len(setter_fn.args) >= 2:
                        param_ty = setter_fn.args[1].type
                        if isinstance(param_ty, ir.IntType) and param_ty.width == 64:
                            value = self._coerce(value, value_ty, IntType(name="int"))
                        elif isinstance(param_ty, ir.PointerType):
                            value = marshal.marshal_to_object(
                                self.builder,
                                self.module,
                                self.runtime,
                                value,
                                value_ty,
                            )
                    self._call_user(setter_fn, [obj_val, value], "")
                    return
                if self._resolve_property_mro(hint, target.name) is not None:
                    self._emit_builtin_exception_and_branch(
                        "AttributeError",
                        "can't set attribute",
                        target.span,
                    )
                    return
                data_descr = self._class_attr_descriptor_class(
                    hint,
                    target.name,
                )
                if data_descr is not None:
                    _owner_info, desc_info = data_descr
                    if "__set__" in desc_info.methods:
                        obj_val = self._emit_expr(target.obj)
                        value_obj = marshal.marshal_to_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            value,
                            value_ty,
                        )
                        if self._emit_data_descriptor_set(
                            hint,
                            target.name,
                            obj_val,
                            value_obj,
                        ):
                            return

        current_class = self.current_class
        if (
            current_class is not None
            and isinstance(target.obj, Name)
            and target.obj.ident == "self"
        ):
            receiver_class_name = self._self_receiver_class_name()
            receiver_info = None
            if receiver_class_name is not None:
                receiver_info = self.class_lowering.classes.get(receiver_class_name)
            if receiver_info is None:
                receiver_info = current_class
            in_init = (
                self.current_func_def is not None
                and self.current_func_def.name == "__init__"
            )
            if (
                getattr(receiver_info, "dataclass_frozen", False)
                and target.name in receiver_info.field_names
                and not in_init
            ):
                self._emit_builtin_exception_and_branch(
                    "AttributeError",
                    "cannot assign to field",
                    target.span,
                )
                return
            self_val = self.builder.load(self.env["self"][0], name=self._fresh("self"))
            value = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                value,
                value_ty,
            )
            if self._class_has_property(receiver_info.name, target.name):
                # The property's setter (or its missing-setter error) runs
                # even though the store also declared a same-named field.
                status = self.builder.call(
                    self.runtime["py_obj_setattr"],
                    [self_val, self._attr_name_ptr(runtime_attr_name), value],
                    name=self._fresh(f"self.prop.setattr.{target.name}.rc"),
                )
                self._emit_attribute_error_if_status_failed(
                    status,
                    target.name,
                    target.span,
                )
                return
            status = self.class_lowering.emit_self_attr_store(
                receiver_info, target.name, self_val, value
            )
            if status is not None:
                self._emit_attribute_error_if_status_failed(
                    status,
                    target.name,
                    target.span,
                )
            return
        obj = self._emit_expr(target.obj)
        name_ptr = self._attr_name_ptr(runtime_attr_name)
        class_object_hint = None
        if isinstance(target.obj, Name):
            class_object_hint = getattr(self, "env_class_object_hint", {}).get(
                target.obj.ident
            )
        if obj in getattr(self, "_cpy_values", ()) or (
            isinstance(target.obj, Name)
            and getattr(self, "_cpy_env_flags", {}).get(
                target.obj.ident,
                False,
            )
        ):
            cpy_value, owned = self._marshal_to_cpython(value, value_ty)
            self.builder.call(
                self.runtime["py_cpy_setattr"], [obj, name_ptr, cpy_value]
            )
            if owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_value])
            return
        value = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            value_ty,
        )
        slot_index = self._typed_instance_field_slot(target)
        if slot_index is not None:
            # Same primitive ``self.x = v`` has always used: the receiver's
            # class and the field's slot are statically known and no class in
            # the MRO overrides __setattr__, so the string-keyed
            # ``py_obj_setattr`` lookup (3661 instructions per store against
            # CPython's 385) is replaced by a balanced slot store.
            self.builder.call(
                self.runtime["py_instance_set_field"],
                [obj, ir.Constant(_I32, slot_index), value],
            )
            return
        status = self.builder.call(
            self.runtime["py_obj_setattr"],
            [obj, name_ptr, value],
            name=self._fresh(f"setattr.{target.name}.rc"),
        )
        self._emit_attribute_error_if_status_failed(
            status,
            target.name,
            target.span,
        )
        if (
            class_object_hint is not None
            and hasattr(self, "class_lowering")
            and class_object_hint in self.class_lowering.classes
        ):
            if not hasattr(self, "_class_attr_runtime_state"):
                self._class_attr_runtime_state = {}
            state = (
                "unknown"
                if getattr(self, "_class_attr_mutation_in_loop_depth", 0)
                else "live"
            )
            info = self.class_lowering.classes[class_object_hint]
            self._class_attr_runtime_state[(info.name, runtime_attr_name)] = state

    def _emit_attr_store(self, target: Attr, value_expr: Expr) -> None:
        if live_import_attribute_store(self, target):
            value_root = self._emit_slot_call_operand(value_expr, "import.store.value")
            previous = self._current_try_err_block()
            error = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((value_root,), error)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                emit_live_import_attribute_store(self, target, value_root)
                self._release_slot_call_roots((value_root,))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return
        if isinstance(target.obj, Name) and hasattr(self, "class_lowering"):
            info = self.class_lowering.classes.get(target.obj.ident)
            if info is not None and self.class_lowering.uses_live_class_attribute(info, target.name, value_expr.ty):
                self.class_lowering.emit_live_class_attribute_store(target, value_expr, info)
                return
        if (
            isinstance(target.obj, Name)
            and target.obj.ident in getattr(self, "_native_module_aliases", {})
            and self._expr_returns_unsafe_raw_pointer(value_expr)
        ):
            raise NotImplementedError(
                "compiled module attribute assignment cannot store an unsafe raw pointer as a Python object"
            )
        prefer_native_callable = (
            isinstance(value_expr, Name) and value_expr.ident in self.functions
        )
        if prefer_native_callable:
            old_prefer_native = self._prefer_native_callable_values
            self._prefer_native_callable_values = True
            try:
                value = self._emit_expr(value_expr)
            finally:
                self._prefer_native_callable_values = old_prefer_native
        else:
            valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
                value_expr.ty,
                value_expr,
            )
            if valueclass_payload is not None:
                boxed_valueclass = self._emit_valueclass_payload_to_object(
                    valueclass_payload,
                    value_expr.ty,
                    consume_fields=True,
                )
                if boxed_valueclass is not None:
                    self._emit_attr_store_value(
                        target,
                        boxed_valueclass,
                        DynType(name="dyn"),
                    )
                    self._gc_release(
                        boxed_valueclass,
                        self._release_expr_label("owned", value_expr),
                    )
                    return
            value = self._emit_expr(value_expr)
        self._emit_attr_store_value(target, value, value_expr.ty)
        self._gc_release_if_owned(value, value_expr)
