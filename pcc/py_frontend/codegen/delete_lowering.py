"""Delete-statement lowering for L1CodeGen."""
from __future__ import annotations

from pcc.llvm_capi.compat import ir

from ..py_ast import Attr, ByteArrayType, ClassType, Delete, DictType, DynType, IntType, ListType, Name, Slice, Subscript
from . import marshal
from .runtime_abi import declare_runtime_global


_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_CSTR = _I8.as_pointer()


class DeleteLoweringMixin:
    def _unbind_module_global(self, name: str) -> None:
        """Unbind a module-level name deleted by ``del``.

        Mirrors the module teardown protocol in
        ``module_lifecycle_lowering`` rather than clearing the slot by hand:
        a CPython-compatible global is released with ``py_cpy_decref``, and a
        pcc global is unpinned and then cleared through ``pcc_gc_store_root``,
        which drops the old reference itself.  Storing null directly would
        leave ``PY_FLAG_GC_PINNED`` and its telemetry behind and would bypass
        the GC3/GC4 slot write barrier.

        Deleting a name that is not currently bound raises ``NameError``, as
        Python requires, instead of silently succeeding.
        """
        # Only a name that actually denotes the module global here may be
        # unbound.  A function-local ``del x`` that merely shares a name with a
        # module global must not touch it -- this is the same scope test the
        # assignment paths use.
        if not (
            self.current_func_def is None
            or name in getattr(self, "_current_global_names", set())
        ):
            return
        entry = getattr(self, "_module_globals", {}).get(name)
        if entry is None:
            return
        gv, _declared_ty = entry
        flag = getattr(self, "_module_global_init_flags", {}).get(name)
        if flag is not None:
            # ``del x`` on an unbound or already-deleted global is an error.
            self._emit_module_global_bound_check(name, None)

        if self._cpy_module_flags.get(name, False):
            value = self.builder.load(
                gv, name=self._fresh(f"del.global.cpy.{name}")
            )
            self.builder.store(ir.Constant(value.type, None), gv)
            self.builder.call(self.runtime["py_cpy_decref"], [value])
        else:
            value_type = getattr(gv, "value_type", None)
            if isinstance(value_type, ir.PointerType):
                value = self.builder.load(
                    gv, name=self._fresh(f"del.global.value.{name}")
                )
                self._gc_unpin(value)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [
                        self._as_gc_ptr(
                            gv, name=self._fresh(f"del.global.slot.{name}")
                        ),
                        ir.Constant(value_type, None),
                    ],
                )
        if flag is not None:
            self.builder.store(ir.Constant(_I1, 0), flag)
        ast_module = getattr(self, "ast_module", None)
        if ast_module is not None:
            module_name_ptr = self._pooled_cstr_ptr(
                ast_module.name or "__main__",
                ".pcc.module.del.name",
            )
            self.builder.call(
                self.runtime["py_module_attr_del"],
                [module_name_ptr, self._attr_name_ptr(name)],
                name=self._fresh(f"del.global.{name}"),
            )

    def _emit_delete(self, stmt: Delete) -> None:
        """Lower ``del x`` / ``del d[k]`` / ``del xs[i]``."""
        for target in stmt.targets:
            if isinstance(target, Name):
                slot_info = self.env.get(target.ident)
                if (
                    target.ident in getattr(self, "_owned_local_names", set())
                    and slot_info is not None
                ):
                    alloca, ir_ty, _decl_ty = slot_info
                    if isinstance(ir_ty, ir.PointerType) and self._ir_type_matches(
                        ir_ty, _CSTR
                    ):
                        if target.ident in self._owned_local_has_value:
                            old = self.builder.load(
                                alloca,
                                name=self._fresh(f"del.{target.ident}"),
                            )
                            self._gc_release(old)
                            self.builder.store(ir.Constant(_CSTR, None), alloca)
                        self._discard_owned_local_gc_root(target.ident, alloca)
                        self._owned_local_names.discard(target.ident)
                        self._owned_local_has_value.discard(target.ident)
                self.env.pop(target.ident, None)
                if hasattr(self, "_cpy_env_flags"):
                    self._cpy_env_flags.pop(target.ident, None)
                if hasattr(self, "env_class_hint"):
                    self.env_class_hint.pop(target.ident, None)
                self._clear_native_module_object_alias(target.ident)
                self._weak_dict_env_flags.pop(target.ident, None)
                self._weakref_env_flags.pop(target.ident, None)
                self._unbind_module_global(target.ident)
                continue
            if isinstance(target, Subscript):
                if self._emit_native_os_environ_delitem(target):
                    continue
                if isinstance(target.idx, Slice):
                    obj = self._emit_expr(target.obj)
                    obj_ty = target.obj.ty
                    if obj not in getattr(self, "_cpy_values", ()):
                        lo_obj = self._emit_slice_bound_object(target.idx.lo)
                        hi_obj = self._emit_slice_bound_object(target.idx.hi)
                        step_obj = self._emit_slice_bound_object(target.idx.step)
                        if isinstance(obj_ty, ListType):
                            self.builder.call(
                                self.runtime["py_list_del_slice"],
                                [obj, lo_obj, hi_obj, step_obj],
                            )
                            return
                        if isinstance(obj_ty, ByteArrayType):
                            self.builder.call(
                                self.runtime["py_bytearray_del_slice"],
                                [obj, lo_obj, hi_obj, step_obj],
                            )
                            return
                        if isinstance(obj_ty, DynType):
                            self.builder.call(
                                self.runtime["py_obj_del_slice"],
                                [obj, lo_obj, hi_obj, step_obj],
                            )
                            return
                        raise NotImplementedError(
                            f"Layer 1 slice delete on type "
                            f"{type(obj_ty).__name__} not supported"
                        )

                    obj_cpy = self.builder.call(
                        self.runtime["py_cpy_from_pcc_obj"],
                        [obj],
                        name=self._fresh("cpy.del.obj"),
                    )
                    slice_fn = self._load_cpython_builtin("slice")

                    def _as_cpy(e):
                        if e is None:
                            gv = declare_runtime_global(
                                self.module,
                                "py_None",
                            )
                            none = self.builder.load(gv, name="none")
                            return self.builder.call(
                                self.runtime["py_cpy_from_pcc_obj"],
                                [none],
                                name=self._fresh("cpy.none"),
                            )
                        v = self._emit_expr(e)
                        obj_v = marshal.marshal_to_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            v,
                            e.ty,
                        )
                        return self.builder.call(
                            self.runtime["py_cpy_from_pcc_obj"],
                            [obj_v],
                            name=self._fresh("cpy.slice.arg"),
                        )

                    lo_cpy = _as_cpy(target.idx.lo)
                    hi_cpy = _as_cpy(target.idx.hi)
                    step_cpy = _as_cpy(target.idx.step)
                    slice_obj = self.builder.call(
                        self.runtime["py_cpy_call3"],
                        [slice_fn, lo_cpy, hi_cpy, step_cpy],
                        name=self._fresh("cpy.slice"),
                    )
                    delitem_gv = self._cstr_global(
                        "__delitem__",
                        ".cpy.attr.__delitem__",
                    )
                    delitem_fn = self.builder.call(
                        self.runtime["py_cpy_getattr"],
                        [obj_cpy, self._ptr_to_cstr(delitem_gv)],
                        name=self._fresh("cpy.delitem.fn"),
                    )
                    self.builder.call(
                        self.runtime["py_cpy_call1"],
                        [delitem_fn, slice_obj],
                        name=self._fresh("cpy.delitem"),
                    )
                    continue
                obj = self._emit_expr(target.obj)
                obj_ty = target.obj.ty
                if isinstance(obj_ty, (DictType, ListType, ClassType, DynType)):
                    idx_obj = self._emit_subscript_key_object(target.idx)
                    release_on_error = []
                    if self._owned_release_needed(idx_obj, target.idx):
                        release_on_error.append(idx_obj)
                    if self._owned_release_needed(obj, target.obj):
                        release_on_error.append(obj)
                    self.builder.call(
                        self.runtime["py_obj_delete_subscript"],
                        [obj, idx_obj],
                    )
                    self._emit_post_call_err_check(
                        target.span, release_on_error=tuple(release_on_error)
                    )
                    self._gc_release_if_owned(idx_obj, target.idx)
                    self._gc_release_if_owned(obj, target.obj)
                    continue
                raise NotImplementedError(
                    f"Layer 1 'del' on subscript with container type "
                    f"{type(obj_ty).__name__} not yet wired"
                )
            if isinstance(target, Attr):
                if isinstance(target.obj, Name):
                    if (
                        hasattr(self, "class_lowering")
                        and target.obj.ident in self.class_lowering.classes
                    ):
                        class_info = self.class_lowering.classes[target.obj.ident]
                        if self._emit_metaclass_data_descriptor_delete(
                            class_info,
                            target.name,
                            target.span,
                        ):
                            continue
                    hint = self.env_class_hint.get(target.obj.ident)
                    if hint is not None:
                        info = self._resolve_property_deleter_mro(hint, target.name)
                        if info is not None:
                            deleter_fn = info.property_deleters[target.name]
                            obj_val = self._emit_expr(target.obj)
                            self._call_user(deleter_fn, [obj_val], "")
                            continue
                        if self._resolve_property_mro(hint, target.name) is not None:
                            self._emit_builtin_exception_and_branch(
                                "AttributeError",
                                "can't delete attribute",
                                target.span,
                            )
                            continue
                obj = self._emit_as_object(target.obj)
                status = self.builder.call(
                    self.runtime["py_obj_delattr"],
                    [obj, self._attr_name_ptr(target.name)],
                    name=self._fresh(f"delattr.{target.name}.rc"),
                )
                self._emit_attribute_error_if_status_failed(
                    status,
                    target.name,
                    target.span,
                )
                if (
                    isinstance(target.obj, Name)
                    and hasattr(self, "class_lowering")
                    and target.obj.ident in self.class_lowering.classes
                ):
                    if not hasattr(self, "_class_attr_runtime_state"):
                        self._class_attr_runtime_state = {}
                    class_info = self.class_lowering.classes[target.obj.ident]
                    if getattr(self, "_class_attr_mutation_in_loop_depth", 0):
                        state = "unknown"
                    else:
                        state = "deleted"
                    self._class_attr_runtime_state[
                        (class_info.name, target.name)
                    ] = state
                continue
            raise NotImplementedError(
                f"Layer 1 'del' on {type(target).__name__} target not supported"
            )
