"""Attribute load lowering helpers for L1CodeGen."""

from __future__ import annotations

from pcc.frontends.python.codegen.cpy_import_state import (
    live_import_expr_binding,
    live_import_name_slot,
)

from pcc.ir.compat import ir
from pcc.driver.python_target import (
    PYTHON_TARGET_FULL_VERSION, PYTHON_TARGET_VERSION_INFO,
    PYTHON_TARGET_MAJOR, PYTHON_TARGET_MINOR, PYTHON_TARGET_MICRO,
)
from pcc.frontends.python.py_ast import BoolLit, IntLit, NoneLit, StrLit

from pcc.frontends.python.py_ast import Attr, BinOp, BoolType, ByteArrayType, BytesType, Call, ClassType, DictType, DynType, Expr, FloatType, IntType, ListType, MemoryViewType, Name, NoneType, StrType, Subscript, TupleType, Type
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.class_model_lowering import _class_type_like
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.native_os import native_os_descriptor_constant
from pcc.frontends.python.codegen.local_bound_lowering import check_local_bound
from pcc.frontends.python.py_ast import RawPointerType
from pcc.frontends.python.codegen.generator_lowering import emit_function_auto_park_role
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_MATH_INF = 1e308 * 10.0
_MATH_NAN = (1e308 * 10.0) * 0.0


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


class AttrLoadLoweringMixin:
    def _native_namespace_projection(self, expr):
        """Resolve only compiler-native namespace provenance, never spelling."""
        base = expr.obj
        while isinstance(base, Attr):
            base = base.obj
        if not isinstance(base, Name):
            return None
        if base.ident in self.env or base.ident in self._module_globals:
            return None
        export = self._native_module_object_export_info(expr.obj, expr.name)
        if export is not None:
            value_ty = export[1].get("value_ty", ())
            if value_ty and value_ty[0] == "raw_pointer":
                return ("raw",)
        # A real imported object must be read through its live binding; its
        # metadata alias does not authorize bypassing rebinding or descriptors.
        if base.ident in getattr(self, "_native_extension_module_env", {}):
            return None
        alias = getattr(self, "_native_module_aliases", {}).get(base.ident)
        if alias is not None and alias in getattr(self, "_sibling_module_inits", ()):
            return None
        module = self._native_builtin_module_for_name(base.ident)
        if module is None and self._native_builtin_value_kind_for_expr(expr.obj) == "os.path":
            module = "os.path"
        if isinstance(expr.obj, Name) and module is not None:
            override = self._native_module_attr_global_if_exists(module, expr.name)
            if override is not None:
                return ("source", override)
        if export is not None and export[0] != self.ast_module.name:
            if export[1].get("kind") in ("constant", "module_global", "function", "class"):
                return ("module", export[0], expr.name)
        if module is None:
            return None
        if isinstance(expr.obj, Attr):
            if (module == "sys" and isinstance(expr.obj.obj, Name)
                    and expr.obj.name == "implementation"
                    and self._native_module_attr_global_if_exists("sys", "implementation") is None):
                if expr.name == "name":
                    return ("str", "pcc")
                if expr.name == "cache_tag":
                    return ("none", None)
            if (module == "sys" and isinstance(expr.obj.obj, Name)
                    and expr.obj.name == "version_info"
                    and self._native_module_attr_global_if_exists("sys", "version_info") is None):
                versions = {"major": PYTHON_TARGET_MAJOR, "minor": PYTHON_TARGET_MINOR,
                            "micro": PYTHON_TARGET_MICRO}
                if expr.name in versions:
                    return ("int", versions[expr.name])
            if not (module == "os" and isinstance(expr.obj.obj, Name)
                    and expr.obj.name == "path"):
                return None
            module = "os.path"
        if module == "builtins" and expr.name == "object":
            return ("runtime", "py_builtin_type_for_tag", (ir.Constant(_I64, -1),))
        if module == "builtins" and self._builtin_function_wrapper(expr.name) is not None:
            return ("builtin_function", expr.name)
        if module in ("os", "os.path"):
            windows = self._target_sys_platform_text() == "win32"
            constants = {"sep": "\\" if windows else "/", "pathsep": ";" if windows else ":",
                         "curdir": ".", "pardir": "..", "extsep": ".",
                         "devnull": "nul" if windows else "/dev/null"}
            if module == "os":
                constants["name"] = "nt" if windows else "posix"
                constants["linesep"] = "\r\n" if windows else "\n"
            if expr.name in constants:
                return ("str", constants[expr.name])
            if expr.name == "altsep":
                return ("str", "/") if windows else ("none", None)
            access = {"F_OK": 0, "X_OK": 1, "W_OK": 2, "R_OK": 4}
            if module == "os" and expr.name in access:
                return ("int", access[expr.name])
        if module == "tempfile" and expr.name == "TemporaryDirectory":
            return ("runtime", "py_tempdir_type", ())
        if module == "os":
            provider = {"fdopen": "py_file_fdopen_function", "open": "py_os_open_function",
                        "close": "py_os_close_function", "mkdir": "py_os_mkdir_function",
                        "pwrite": "py_os_pwrite_function", "ftruncate": "py_os_ftruncate_function"}.get(expr.name)
            if provider is not None:
                if "os" in getattr(self, "_sibling_module_inits", ()):
                    return ("module", "os", expr.name)
                return ("runtime", provider, ())
            flag = native_os_descriptor_constant(expr.name, self._target_sys_platform_text())
            if flag is not None:
                if "os" in getattr(self, "_sibling_module_inits", ()):
                    return ("module", "os", expr.name)
                return ("int", flag)
        if module == "tempfile" and expr.name == "mkdtemp":
            return ("runtime", "py_tempfile_mkdtemp_function", ())
        if module == "tempfile" and expr.name == "NamedTemporaryFile":
            return ("runtime", "py_namedtempfile_function", ())
        if module == "sys":
            if expr.name == "argv":
                return ("argv",)
            if expr.name == "version_info":
                return ("version_info",)
            if expr.name in ("stdin", "stdout", "stderr"):
                fd = {"stdin": 0, "stdout": 1, "stderr": 2}[expr.name]
                return ("runtime", "py_sys_stream_object", (ir.Constant(_I64, fd),))
            if expr.name in ("prefix", "base_prefix"):
                return ("runtime", "py_sys_prefix_str", (ir.Constant(_I64, expr.name == "base_prefix"),))
            calls = {"executable": "py_sys_executable_str", "platform": "py_sys_platform_str",
                     "path": "py_sys_path_list", "modules": "py_sys_modules"}
            if expr.name in calls:
                return ("runtime", calls[expr.name], ())
            if expr.name == "version":
                return ("str", PYTHON_TARGET_FULL_VERSION + " (pcc self-host)")
            if expr.name == "byteorder":
                return ("str", "little")
            if expr.name in ("maxunicode", "maxsize"):
                return ("int", 0x10FFFF if expr.name == "maxunicode" else (1 << 63) - 1)
        return None

    def _emit_slot_call_namespace_attribute(self, expr, label):
        projection = self._native_namespace_projection(expr)
        if projection is None:
            return None
        if projection[0] == "raw":
            raise L1CodegenError("raw module export cannot be a managed namespace operand")
        base = expr.obj
        while isinstance(base, Attr):
            base = base.obj
        check_local_bound(self, base)
        kind = projection[0]
        if kind == "module":
            return self._emit_slot_call_module_value(projection[1], projection[2], expr.span, label)
        if kind == "argv":
            return self._emit_slot_call_program_argv(expr.span, label)
        if kind == "version_info":
            values = tuple(IntLit(span=expr.span, ty=IntType(name="int"), value=value)
                           for value in PYTHON_TARGET_VERSION_INFO)
            return self._emit_slot_call_sequence(values, label, True)
        if kind == "str":
            literal = StrLit(span=expr.span, ty=StrType(name="str"), value=projection[1])
            return self._emit_slot_call_operand(literal, label)
        if kind == "int":
            literal = IntLit(span=expr.span, ty=IntType(name="int"), value=projection[1])
            return self._emit_slot_call_operand(literal, label)
        if kind == "none":
            literal = NoneLit(span=expr.span, ty=NoneType(name="None"))
            return self._emit_slot_call_operand(literal, label)
        if kind == "builtin_function":
            # ``builtins.len`` is the same object as the builtin ``len``.
            output = self._new_slot_call_root(label)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                value = self._emit_builtin_function_value(
                    projection[1], self._builtin_function_wrapper(projection[1]),
                )
                self._publish_slot_call_owned(output, value, label="builtin function")
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return output
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((output,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            if kind == "source":
                # These exact module-attribute globals are registered by
                # module_global_lowering and retained by module lifecycle.
                self._slot_call_copy_source(output, projection[1], span=expr.span)
                current = self.builder.load(output, name=self._fresh("namespace.attribute.current"))
                self._emit_attribute_error_if_null(current, expr.name, expr.span)
            elif kind == "runtime":
                self._slot_call_runtime_call(projection[1], (), result_slot=output,
                                             suffix_args=projection[2], span=expr.span)
            else:
                raise L1CodegenError("unknown native namespace producer contract")
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return output

    def _valueclass_payload_expr_type(self, expr: Expr) -> Type | None:
        if isinstance(expr, Name):
            slot = self.env.get(expr.ident)
            if slot is not None:
                return slot[2]
            return expr.ty
        if isinstance(expr, Attr):
            owner_ty = self._valueclass_payload_expr_type(expr.obj)
            if owner_ty is not None and self._is_valueclass_payload_type(owner_ty):
                field_info = self._valueclass_field_info(owner_ty, expr.name)
                if field_info is not None:
                    return field_info[1]
        return getattr(expr, "ty", None)

    def _slot_call_valueclass_field_source(self, payload_slot, path, module_source):
        """Find the physical root already registered for one payload field."""
        if module_source:
            # The exact aggregate global and declared field path were matched
            # by the caller. Module lifecycle registers these same fields.
            return self._module_global_valueclass_payload_field_slot(
                payload_slot, path, name="value.attribute.module.source",
            ), False
        registry = getattr(self, "_fn_valueclass_payload_root_slots", {}).get(
            self.current_function.name, (),
        )
        roots = getattr(self, "_fn_gc_root_slot_registry", {}).get(
            self.current_function.name, (),
        )
        for record in registry:
            if record[0] is payload_slot and record[1] == path:
                source, borrowed = record[2], record[3]
                if any(root[1] is source for root in roots):
                    return source, borrowed
        raise L1CodegenError("valueclass pointer field has no authoritative registered payload slot")

    def _emit_slot_call_valueclass_field(self, payload_slot, path, field_ty, module_source, label, span):
        if isinstance(field_ty, RawPointerType):
            raise L1CodegenError("raw valueclass field cannot be a managed slot-call operand")
        output = self._new_slot_call_root(label)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((output,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            if self._is_valueclass_payload_type(field_ty):
                class_name = self._ensure_class_type_registered(field_ty)
                info = self.class_lowering.classes.get(class_name)
                if info is None:
                    raise L1CodegenError("valueclass field boxing requires its registered class")
                cls = self._new_slot_call_root(label + ".class")
                self._try_err_block = self._slot_call_cleanup_block((output, cls), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_copy_source(cls, info.global_var, span=span)
                self._slot_call_runtime_call("py_valuebox_new", (cls,), result_slot=output, span=span)
                self._release_slot_call_roots((cls,))
                self._try_err_block = self._slot_call_cleanup_block((output,), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                for index, (_name, item_ty) in enumerate(field_ty.fields):
                    item = self._emit_slot_call_valueclass_field(
                        payload_slot, path + (index,), item_ty, module_source,
                        label + ".field", span,
                    )
                    self._try_err_block = self._slot_call_cleanup_block((output, item), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call(
                        "py_valuebox_set_field", (output, item),
                        suffix_args=(ir.Constant(_I32, index),), argument_order=(0, 2, 1), span=span,
                    )
                    self._release_slot_call_roots((item,))
                    self._try_err_block = self._slot_call_cleanup_block((output,), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
            else:
                field_ir_ty = self._valueclass_field_payload_ir_type(field_ty)
                if isinstance(field_ir_ty, ir.PointerType):
                    source, borrowed = self._slot_call_valueclass_field_source(
                        payload_slot, path, module_source,
                    )
                    self._slot_call_copy_source(output, source, borrowed, span)
                elif isinstance(field_ir_ty, (ir.IntType, ir.FloatType, ir.DoubleType)):
                    indices = [ir.Constant(_I32, 0)]
                    indices.extend(ir.Constant(_I32, index) for index in path)
                    source = self.builder.gep(payload_slot, indices, inbounds=True,
                                              name=self._fresh("value.attribute.scalar.slot"))
                    scalar = self.builder.load(source, name=self._fresh("value.attribute.scalar"))
                    boxed = marshal.marshal_to_object(self.builder, self.module, self.runtime, scalar, field_ty)
                    self._publish_slot_call_owned(output, boxed, label="valueclass scalar field boxing")
                else:
                    raise L1CodegenError("valueclass field has no supported object projection")
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup
        return output

    def _emit_slot_call_valueclass_name(self, expr, label):
        """Box an actual aggregate binding through its registered field roots."""
        if not isinstance(expr, Name) or live_import_name_slot(self, expr.ident) is not None:
            return None
        entry = self.env.get(expr.ident)
        global_entry = self._module_globals.get(expr.ident)
        module_source = False
        if entry is not None:
            payload_slot, actual_ir_ty, payload_ty = entry
            module_source = global_entry is not None and global_entry[0] is payload_slot
        elif global_entry is not None:
            payload_slot, payload_ty = global_entry
            actual_ir_ty = payload_slot.value_type
            module_source = True
        else:
            return None
        # Object-projected Names already have an authoritative ordinary root.
        # An annotation alone cannot turn that pointer into an aggregate.
        if isinstance(actual_ir_ty, ir.PointerType) or not self._is_valueclass_payload_type(payload_ty):
            return None
        payload_ir_ty = self._valueclass_payload_ir_type(payload_ty)
        if not isinstance(actual_ir_ty, ir.LiteralStructType) or str(actual_ir_ty) != str(payload_ir_ty):
            raise L1CodegenError("valueclass name requires its actual aggregate payload slot")
        check_local_bound(self, expr)
        if getattr(self, "_cpy_env_flags", {}).get(expr.ident, False):
            raise L1CodegenError("CPython valueclass binding requires an explicit output-slot bridge")
        if module_source:
            if getattr(self, "_cpy_module_flags", {}).get(expr.ident, False):
                raise L1CodegenError("CPython valueclass global requires an explicit output-slot bridge")
            if self._module_global_needs_bound_check(expr.ident):
                self._emit_module_global_bound_check(expr.ident, expr)
        # The empty path boxes the whole payload, without consuming its
        # fields or returning a raw box across field-allocation cleanup.
        return self._emit_slot_call_valueclass_field(
            payload_slot, (), payload_ty, module_source, label, expr.span,
        )

    def _emit_slot_call_valueclass_attribute(self, expr, label):
        owner_ty = self._valueclass_payload_expr_type(expr.obj)
        if owner_ty is None or not self._is_valueclass_payload_type(owner_ty):
            return None
        field = self._valueclass_field_info(owner_ty, expr.name)
        if field is None:
            return None
        if self._expr_looks_cpython(expr):
            raise L1CodegenError("CPython valueclass field requires an explicit output-slot bridge")
        field_ty = field[1]
        base = expr
        path = ()
        while isinstance(base, Attr):
            enclosing = self._valueclass_payload_expr_type(base.obj)
            item = self._valueclass_field_info(enclosing, base.name)
            if item is None:
                break
            path = (item[0],) + path
            base = base.obj
        payload_ty = self._valueclass_payload_expr_type(base)
        payload_ir_ty = self._valueclass_payload_ir_type(payload_ty)
        payload_slot = None
        module_source = False
        if isinstance(base, Name):
            check_local_bound(self, base)
            entry = self.env.get(base.ident)
            global_entry = self._module_globals.get(base.ident)
            if entry is not None:
                payload_slot, actual_ir_ty, payload_ty = entry
                if getattr(self, "_cpy_env_flags", {}).get(base.ident, False):
                    raise L1CodegenError("CPython valueclass binding requires an explicit output-slot bridge")
                module_source = global_entry is not None and global_entry[0] is payload_slot
            elif global_entry is not None:
                payload_slot, payload_ty = global_entry
                actual_ir_ty = payload_slot.value_type
                module_source = True
            if payload_slot is not None:
                if isinstance(actual_ir_ty, ir.PointerType):
                    # A boxed binding takes ordinary rooted getattr below.
                    # Decide before evaluating the receiver; never retry it.
                    return None
                if not isinstance(actual_ir_ty, ir.LiteralStructType) or str(actual_ir_ty) != str(payload_ir_ty):
                    raise L1CodegenError("valueclass attribute requires its actual aggregate payload slot")
                if module_source:
                    if getattr(self, "_cpy_module_flags", {}).get(base.ident, False):
                        raise L1CodegenError("CPython valueclass global requires an explicit output-slot bridge")
                    if self._module_global_needs_bound_check(base.ident):
                        self._emit_module_global_bound_check(base.ident, base)
        if payload_slot is None:
            payload = self._emit_valueclass_payload_expr(base, payload_ty)
            if payload is None:
                return None
            source = self._valueclass_payload_source(payload)
            if source is None:
                raise L1CodegenError("valueclass attribute lost its producer-owned payload source")
            return self._emit_slot_call_valueclass_field(source[1], source[2] + path, field_ty, source[4], label, expr.span)
        return self._emit_slot_call_valueclass_field(payload_slot, path, field_ty,
                                                     module_source, label, expr.span)

    def _metaclass_data_descriptor_info(self, class_info, attr_name: str):
        metaclass_name = getattr(class_info, "metaclass_name", None)
        if metaclass_name is None:
            return None
        meta_info = self.class_lowering.classes.get(metaclass_name)
        if meta_info is None:
            return None
        if attr_name in meta_info.properties:
            return ("property", meta_info, None)
        desc = self._class_attr_descriptor_class(metaclass_name, attr_name)
        if desc is None:
            return None
        owner_info, desc_info = desc
        if "__get__" not in desc_info.methods:
            return None
        if "__set__" not in desc_info.methods and "__delete__" not in desc_info.methods:
            return None
        return ("descriptor", owner_info, desc_info)

    def _emit_metaclass_data_descriptor_get(
        self,
        class_info,
        attr_name: str,
    ):
        desc = self._metaclass_data_descriptor_info(class_info, attr_name)
        if desc is None:
            return None
        kind, owner_info, desc_info = desc
        cls_obj = self.class_lowering._load_class_object(
            class_info, f"metaclass.descr.obj.{class_info.name}",
        )
        if kind == "property":
            getter = owner_info.properties[attr_name]
            if not self._property_accessor_takes_protocol_args(owner_info, attr_name, "getter", getter):
                return None
            return self._call_user(
                getter,
                [cls_obj],
                self._fresh(f"metaclass.prop.get.{attr_name}"),
                None,
            )
        attr_entry = owner_info.class_attrs.get(attr_name)
        if attr_entry is None:
            return None
        desc_obj = self.builder.load(
            attr_entry[0],
            name=self._fresh(f"metaclass.descr.{attr_name}"),
        )
        meta_cls = self.class_lowering._load_class_object(
            self.class_lowering.classes[
                getattr(class_info, "metaclass_name")
            ], f"metaclass.descr.owner.{class_info.name}",
        )
        return self._call_user(
            desc_info.methods["__get__"],
            [desc_obj, cls_obj, meta_cls],
            self._fresh(f"metaclass.descr.get.{attr_name}"),
            None,
        )

    def _emit_metaclass_data_descriptor_set(
        self,
        class_info,
        attr_name: str,
        value_obj: ir.Value,
    ) -> bool:
        desc = self._metaclass_data_descriptor_info(class_info, attr_name)
        if desc is None:
            return False
        kind, owner_info, desc_info = desc
        cls_obj = self.class_lowering._load_class_object(
            class_info, f"metaclass.descr.set.obj.{class_info.name}",
        )
        if kind == "property":
            setter = owner_info.property_setters.get(attr_name)
            if setter is None:
                self._emit_builtin_exception_and_branch(
                    "AttributeError",
                    "can't set attribute",
                    None,
                    open_dead_continuation=True,
                )
                return True
            if not self._property_accessor_takes_protocol_args(owner_info, attr_name, "setter", setter):
                return False
            self._call_user(setter, [cls_obj, value_obj], "")
            return True
        setter = desc_info.methods.get("__set__")
        if setter is None:
            return False
        attr_entry = owner_info.class_attrs.get(attr_name)
        if attr_entry is None:
            return False
        desc_obj = self.builder.load(
            attr_entry[0],
            name=self._fresh(f"metaclass.descr.set.{attr_name}"),
        )
        self._call_user(setter, [desc_obj, cls_obj, value_obj], "")
        return True

    def _emit_metaclass_data_descriptor_delete(
        self,
        class_info,
        attr_name: str,
        span,
    ) -> bool:
        desc = self._metaclass_data_descriptor_info(class_info, attr_name)
        if desc is None:
            return False
        kind, owner_info, desc_info = desc
        cls_obj = self.class_lowering._load_class_object(
            class_info, f"metaclass.descr.del.obj.{class_info.name}",
        )
        if kind == "property":
            deleter = owner_info.property_deleters.get(attr_name)
            if deleter is None:
                self._emit_builtin_exception_and_branch(
                    "AttributeError",
                    "can't delete attribute",
                    span,
                    open_dead_continuation=True,
                )
                return True
            if not self._property_accessor_takes_protocol_args(owner_info, attr_name, "deleter", deleter):
                return False
            self._call_user(deleter, [cls_obj], "")
            return True
        deleter = desc_info.methods.get("__delete__")
        if deleter is None:
            return False
        attr_entry = owner_info.class_attrs.get(attr_name)
        if attr_entry is None:
            return False
        desc_obj = self.builder.load(
            attr_entry[0],
            name=self._fresh(f"metaclass.descr.del.{attr_name}"),
        )
        self._call_user(deleter, [desc_obj, cls_obj], "")
        return True

    def _emit_bound_self_method_value(
        self,
        info,
        method_name: str,
        method_fn: ir.Function,
        self_val: ir.Value,
    ) -> ir.Value:
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        runtime_args = tuple(a for a in ast_fd.args if a.name != "") if ast_fd else ()
        user_args = runtime_args[1:] if runtime_args else ()
        adapter_name = (
            f"user_{self._module_symbol_suffix(self.ast_module.name or 'mod')}"
            f"_{info.name}_{method_name}_bound_adapter"
        )
        existing = self.module.globals.get(adapter_name)
        if isinstance(existing, ir.Function):
            adapter = existing
        else:
            adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
            adapter = ir.Function(self.module, adapter_ty, name=adapter_name)
            adapter.linkage = "internal"
            saved_builder = self.builder
            entry = adapter.append_basic_block(name="entry")
            self.builder = ir.IRBuilder(entry)

            bound_self = self.builder.call(
                self.runtime["py_tuple_get"],
                [adapter.args[0], ir.Constant(_I64, 0)],
                name="bound.self",
            )
            forwarded: list[ir.Value] = [bound_self]
            for i, ast_arg in enumerate(user_args):
                arg_obj = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[1], ir.Constant(_I64, i)],
                    name=f"arg.{i}",
                )
                param_idx = i + 1
                param_ir_ty = method_fn.args[param_idx].type
                target_ty = ast_arg.annotation or DynType(name="dyn")
                if isinstance(param_ir_ty, ir.PointerType):
                    forwarded.append(arg_obj)
                else:
                    forwarded.append(
                        marshal.marshal_from_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            arg_obj,
                            target_ty,
                        )
                    )

            ret_ty = method_fn.function_type.return_type
            if isinstance(ret_ty, ir.VoidType):
                self.builder.call(method_fn, forwarded)
                none_gv = declare_runtime_global(self.module, "py_None")
                self.builder.ret(self.builder.load(none_gv, name="none"))
            else:
                result = self.builder.call(method_fn, forwarded, name="result")
                if isinstance(ret_ty, ir.PointerType):
                    self.builder.ret(result)
                else:
                    boxed = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        result,
                        ast_fd.return_ty if ast_fd is not None else DynType(name="dyn"),
                    )
                    self.builder.ret(boxed)
            self.builder = saved_builder

        captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 1)],
            name=self._fresh("bound.method.captures"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [captures, ir.Constant(_I64, 0), self_val],
        )
        signature = self._emit_cached_native_func_signature(
            user_args,
            f"{self.ast_module.name or 'mod'}.{info.name}.{method_name}.bound",
        )
        wrapped_captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh(f"bound.{method_name}.signature.wrapper"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 0), captures],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 1), signature],
        )
        fn_obj = self.builder.call(
            self.runtime["py_func_new_bound"],
            [adapter, wrapped_captures, self._attr_name_ptr(method_name), self_val],
            name=self._fresh(f"bound.{method_name}.func"),
        )
        if ast_fd is not None:
            emit_function_auto_park_role(self, ast_fd, fn_obj)
        self._gc_release(captures)
        self._gc_release(signature)
        self._gc_release(wrapped_captures)
        return fn_obj

    def _emit_bound_class_method_value(
        self,
        owner_info,
        method_name: str,
        method_fn: ir.Function,
        receiver_cls: ir.Value,
    ) -> ir.Value:
        ast_fd = self.class_lowering._find_method_def(owner_info.name, method_name)
        runtime_args = tuple(a for a in ast_fd.args if a.name != "") if ast_fd else ()
        user_args = runtime_args[1:] if runtime_args else ()
        adapter_name = (
            f"user_{self._module_symbol_suffix(self.ast_module.name or 'mod')}"
            f"_{owner_info.name}_{method_name}_classmethod_attr_adapter"
        )
        existing = self.module.globals.get(adapter_name)
        if isinstance(existing, ir.Function):
            adapter = existing
        else:
            adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
            adapter = ir.Function(self.module, adapter_ty, name=adapter_name)
            adapter.linkage = "internal"
            saved_builder = self.builder
            entry = adapter.append_basic_block(name="entry")
            self.builder = ir.IRBuilder(entry)

            bound_cls = self.builder.call(
                self.runtime["py_tuple_get"],
                [adapter.args[0], ir.Constant(_I64, 0)],
                name="bound.cls",
            )
            forwarded: list[ir.Value] = [bound_cls]
            for i, ast_arg in enumerate(user_args):
                arg_obj = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[1], ir.Constant(_I64, i)],
                    name=f"arg.{i}",
                )
                param_idx = i + 1
                param_ir_ty = method_fn.args[param_idx].type
                target_ty = ast_arg.annotation or DynType(name="dyn")
                if isinstance(param_ir_ty, ir.PointerType):
                    forwarded.append(arg_obj)
                else:
                    forwarded.append(
                        marshal.marshal_from_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            arg_obj,
                            target_ty,
                        )
                    )

            ret_ty = method_fn.function_type.return_type
            if isinstance(ret_ty, ir.VoidType):
                self.builder.call(method_fn, forwarded)
                none_gv = declare_runtime_global(self.module, "py_None")
                self.builder.ret(self.builder.load(none_gv, name="none"))
            else:
                result = self.builder.call(method_fn, forwarded, name="result")
                if isinstance(ret_ty, ir.PointerType):
                    self.builder.ret(result)
                else:
                    boxed = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        result,
                        ast_fd.return_ty if ast_fd is not None else DynType(name="dyn"),
                    )
                    self.builder.ret(boxed)
            self.builder = saved_builder

        captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 1)],
            name=self._fresh("bound.classmethod.captures"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [captures, ir.Constant(_I64, 0), receiver_cls],
        )
        signature = self._emit_cached_native_func_signature(
            user_args,
            f"{self.ast_module.name or 'mod'}.{owner_info.name}.{method_name}.classmethod",
        )
        wrapped_captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh(f"bound.{method_name}.classmethod.signature.wrapper"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 0), captures],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 1), signature],
        )
        fn_obj = self.builder.call(
            self.runtime["py_func_new_bound"],
            [
                adapter,
                wrapped_captures,
                self._attr_name_ptr(method_name),
                receiver_cls,
            ],
            name=self._fresh(f"bound.{method_name}.classmethod.func"),
        )
        if ast_fd is not None:
            emit_function_auto_park_role(self, ast_fd, fn_obj)
        self._gc_release(captures)
        self._gc_release(signature)
        self._gc_release(wrapped_captures)
        return fn_obj

    def _emit_static_method_value(
        self,
        owner_info,
        method_name: str,
        method_fn: ir.Function,
        cache: bool = True,
    ) -> ir.Value:
        # The class namespace owns the function behind the staticmethod
        # descriptor. Reusing it preserves identity and the normal signature
        # binder for positional arguments, defaults and keyword arguments.
        cls = self.class_lowering._load_class_object(
            owner_info, self._fresh("staticmethod.class")
        )
        value = self.builder.call(
            self.runtime["py_class_getattr"], [cls, self._attr_name_ptr(method_name)],
            name=self._fresh("staticmethod.value"),
        )
        self._emit_post_call_err_check(None)
        self._note_owned_object_value(value)
        return value

    def _emit_unbound_instance_method_value(
        self,
        owner_info,
        method_name: str,
        method_fn: ir.Function,
        cache: bool = True,
    ) -> ir.Value:
        """``Class.method``: the object the class namespace holds.

        CPython returns that function itself (``function.__get__(None,
        cls)``), so aliases and the namespace entry are one object, a
        property or other descriptor stays itself, and a function-local
        class method keeps the captures of its function object.
        """
        cls = self.class_lowering._load_class_object(
            owner_info, self._fresh("unbound.method.class")
        )
        value = self.builder.call(
            self.runtime["py_class_getattr"], [cls, self._attr_name_ptr(method_name)],
            name=self._fresh("unbound." + method_name + ".value"),
        )
        self._emit_post_call_err_check(None)
        self._note_owned_object_value(value)
        return value

    def _maybe_emit_valueclass_payload_attr(self, expr: Attr):
        declared_ty = self._valueclass_payload_expr_type(expr.obj)
        if not self._is_valueclass_payload_type(declared_ty):
            return None
        field_info = self._valueclass_field_info(declared_ty, expr.name)
        if field_info is None:
            return None
        index, field_ty = field_info
        base = expr.obj
        while isinstance(base, Attr):
            base = base.obj
        physical = None
        if isinstance(base, Name):
            entry = self.env.get(base.ident)
            global_entry = self._module_globals.get(base.ident)
            physical = entry[1] if entry is not None else (None if global_entry is None else global_entry[0].value_type)
        if isinstance(physical, ir.LiteralStructType):
            payload = self._emit_expr(expr.obj)
        else:
            payload = self._emit_valueclass_payload_expr(expr.obj, declared_ty)
            if payload is None:
                return None
        source = self._valueclass_payload_source(payload)
        if source is not None and self._is_valueclass_payload_type(field_ty):
            return self._load_valueclass_payload(source[1], field_ty, source[2] + (index,), source[4])
        if (source is not None
                and isinstance(self._valueclass_field_payload_ir_type(field_ty), ir.PointerType)
                and not isinstance(field_ty, RawPointerType)):
            root = self._emit_slot_call_valueclass_field(source[1], source[2] + (index,), field_ty, source[4], "value.attribute", expr.span)
            return self._take_slot_call_root(root)
        return self.builder.extract_value(payload, [index], name=self._fresh("value.attribute.scalar"))

    def _maybe_emit_valueclass_payload_attr_from_dyn(self, expr: Attr):
        if not isinstance(expr.obj, Name):
            return None
        if not hasattr(self, "class_lowering"):
            return None
        if expr.name is None:
            return None
        candidates: list[tuple[object, int]] = []
        hinted = self.env_class_hint.get(expr.obj.ident)
        if hinted is not None:
            info = self.class_lowering.classes.get(hinted)
            if info is not None and info.valueclass and expr.name in info.field_names:
                candidates.append((info, info.field_names.index(expr.name)))
        if not candidates:
            for info in self.class_lowering.classes.values():
                if not info.valueclass:
                    continue
                if expr.name in info.field_names:
                    candidates.append((info, info.field_names.index(expr.name)))
        if not candidates:
            return None
        obj = self._emit_expr(expr.obj)
        if not isinstance(obj.type, ir.PointerType):
            return None

        done_bb = self.current_function.append_basic_block(
            name=self._fresh(f"value.{expr.obj.ident}.{expr.name}.done"),
        )
        incoming: list[tuple[ir.Value, object]] = []
        for info, field_idx in candidates:
            cls_ptr = self.builder.load(
                info.global_var,
                name=self._fresh(f"value.{info.name}.class"),
            )
            is_instance = self.builder.call(
                self.runtime["py_obj_isinstance"],
                [obj, cls_ptr],
                name=self._fresh(f"value.{expr.obj.ident}.class-match.{info.name}"),
            )
            is_instance_i1 = self.builder.icmp_unsigned(
                "!=",
                is_instance,
                ir.Constant(_I64, 0),
                name=self._fresh(f"value.{expr.obj.ident}.{info.name}.isinstance"),
            )
            match_bb = self.current_function.append_basic_block(
                name=self._fresh(
                    f"value.{expr.obj.ident}.{expr.name}.{info.name}.match"
                ),
            )
            next_bb = self.current_function.append_basic_block(
                name=self._fresh(
                    f"value.{expr.obj.ident}.{expr.name}.{info.name}.next"
                ),
            )
            self.builder.cbranch(is_instance_i1, match_bb, next_bb)

            self.builder.position_at_end(match_bb)
            value_field = self.builder.call(
                self.runtime["py_valuebox_get_field"],
                [obj, ir.Constant(_I32, field_idx)],
                name=self._fresh(
                    f"value.{expr.obj.ident}.{expr.name}.{info.name}.field"
                ),
            )
            self.builder.branch(done_bb)
            incoming.append((value_field, self.builder.block))

            self.builder.position_at_end(next_bb)

        fallback = self.builder.call(
            self.runtime["py_obj_getattr"],
            [obj, self._attr_name_ptr(expr.name)],
            name=self._fresh(f"value.{expr.obj.ident}.{expr.name}.fallback.value"),
        )
        self.builder.branch(done_bb)
        incoming.append((fallback, self.builder.block))

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(
            _CSTR,
            name=self._fresh(f"value.{expr.obj.ident}.{expr.name}.picked"),
        )
        for value, block in incoming:
            result.add_incoming(value, block)
        self._emit_attribute_error_if_null(result, expr.name, expr.span)
        if isinstance(expr.ty, (IntType, FloatType, BoolType)):
            native_result = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                result,
                expr.ty,
            )
            self._gc_release(result, self._release_expr_label("valuebox_attr", expr))
            return native_result
        return result

    def _emit_attr(self, expr: Attr) -> ir.Value:
        if not isinstance(expr.ty, RawPointerType):
            if live_import_expr_binding(self, expr.obj):
                value = self._take_slot_call_root(
                    self._emit_slot_call_attribute(expr, "import.attribute"),
                )
                if isinstance(expr.ty, IntType) and expr.ty.name == "int":
                    return value
                return self._unbox_scalar_attr_result(value, expr.ty)
            namespace = self._emit_slot_call_namespace_attribute(expr, "namespace.attribute")
            if namespace is not None:
                return self._take_slot_call_root(namespace)
        if self._is_method_super_call(expr.obj):
            return self._emit_super_attribute_value(expr)
        runtime_attr_name = expr.name
        scaffold_symbol = self._maybe_emit_ir_scaffold_symbol_value(expr)
        if scaffold_symbol is not None:
            return scaffold_symbol
        uname_attr = self._emit_native_os_uname_attr(expr)
        if uname_attr is not None:
            return uname_attr
        lexical_class = self.current_class
        if lexical_class is not None and hasattr(self, "class_lowering"):
            runtime_attr_name = self.class_lowering.mangle_private_attr_name(
                lexical_class,
                expr.name,
            )
        if isinstance(expr.obj, Name) and hasattr(self, "class_lowering"):
            info = self.class_lowering.classes.get(expr.obj.ident)
            if info is None:
                hint = self._class_hint_for_expr(expr.obj)
                if hint is not None:
                    info = self.class_lowering.classes.get(hint)
            if info is None and expr.obj.ident == "self":
                info = self.current_class
            if (
                info is not None
                and self.class_lowering.lookup_class_attr(info, runtime_attr_name) is not None
                and self.class_lowering.uses_live_class_attribute(info, runtime_attr_name)
            ):
                return self.class_lowering.emit_live_class_attribute(expr)
        if (
            expr.name == "__new__"
            and isinstance(expr.obj, Name)
            and self._name_returns_native_builtin_callable_value(expr.obj.ident)
        ):
            # Direct ``int.__new__(...)`` calls have a dedicated lowering, but
            # stdlib modules also treat ``int.__new__`` as a first-class value
            # (notably ``copyreg`` during import). Materialise a real native
            # callable rather than asking the runtime builtin-type ClassInfo
            # for a descriptor it does not own.
            builtin_name = expr.obj.ident
            if builtin_name == "str":
                return self._take_slot_call_root(
                    self._emit_slot_call_attribute(expr, "str.__new__.descriptor")
                )
            builtin_type = self._emit_native_builtin_callable_value(builtin_name)
            if builtin_type is not None:
                adapter_name = f"__pcc_builtin_type_{builtin_name}_dunder_new"
                adapter = self.module.globals.get(adapter_name)
                if not isinstance(adapter, ir.Function):
                    adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
                    adapter = ir.Function(
                        self.module,
                        adapter_ty,
                        name=adapter_name,
                    )
                    adapter.linkage = "internal"
                    adapter_builder = ir.IRBuilder(
                        adapter.append_basic_block(name="entry")
                    )
                    captures_arg, args_arg = adapter.args
                    argc = adapter_builder.call(
                        self.runtime["py_tuple_len"],
                        [args_arg],
                        name="argc",
                    )
                    has_cls = adapter_builder.icmp_signed(
                        ">=",
                        argc,
                        ir.Constant(_I64, 1),
                        name="has.cls",
                    )
                    check_cls_bb = adapter.append_basic_block(name="check.cls")
                    arity_error_bb = adapter.append_basic_block(name="arity.error")
                    adapter_builder.cbranch(
                        has_cls,
                        check_cls_bb,
                        arity_error_bb,
                    )

                    adapter_builder.position_at_end(arity_error_bb)
                    self._emit_native_builtin_callable_type_error(
                        adapter_builder,
                        f"{builtin_name}.__new__() needs a class argument",
                        builtin_name,
                        "dunder_new_arity",
                    )

                    adapter_builder.position_at_end(check_cls_bb)
                    captured_type = adapter_builder.call(
                        self.runtime["py_tuple_get"],
                        [captures_arg, ir.Constant(_I64, 0)],
                        name="captured.type",
                    )
                    requested_cls = adapter_builder.call(
                        self.runtime["py_tuple_get"],
                        [args_arg, ir.Constant(_I64, 0)],
                        name="requested.cls",
                    )
                    exact_cls = adapter_builder.icmp_unsigned(
                        "==",
                        captured_type,
                        requested_cls,
                        name="exact.cls",
                    )
                    call_bb = adapter.append_basic_block(name="call")
                    subclass_error_bb = adapter.append_basic_block(
                        name="subclass.error"
                    )
                    adapter_builder.cbranch(
                        exact_cls,
                        call_bb,
                        subclass_error_bb,
                    )

                    adapter_builder.position_at_end(subclass_error_bb)
                    adapter_builder.call(
                        self.runtime["py_decref"],
                        [requested_cls],
                    )
                    adapter_builder.call(
                        self.runtime["py_decref"],
                        [captured_type],
                    )
                    self._emit_native_builtin_callable_type_error(
                        adapter_builder,
                        f"{builtin_name}.__new__() native builtin subclasses "
                        "are not implemented",
                        builtin_name,
                        "dunder_new_subclass",
                    )

                    adapter_builder.position_at_end(call_bb)
                    one = adapter_builder.call(
                        self.runtime["py_int_from_i64"],
                        [ir.Constant(_I64, 1)],
                        name="slice.one",
                    )
                    none_gv = declare_runtime_global(self.module, "py_None")
                    none_obj = adapter_builder.load(none_gv, name="none")
                    ctor_args = adapter_builder.call(
                        self.runtime["py_tuple_slice"],
                        [args_arg, one, none_obj, none_obj],
                        name="ctor.args",
                    )
                    result = adapter_builder.call(
                        self.runtime["py_obj_call"],
                        [captured_type, ctor_args, none_obj],
                        name="result",
                    )
                    adapter_builder.call(
                        self.runtime["py_decref"],
                        [ctor_args],
                    )
                    adapter_builder.call(self.runtime["py_decref"], [one])
                    adapter_builder.call(
                        self.runtime["py_decref"],
                        [requested_cls],
                    )
                    adapter_builder.call(
                        self.runtime["py_decref"],
                        [captured_type],
                    )
                    adapter_builder.ret(result)

                captures = self.builder.call(
                    self.runtime["py_tuple_new"],
                    [ir.Constant(_I64, 1)],
                    name=self._fresh(f"{builtin_name}.__new__.captures"),
                )
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [captures, ir.Constant(_I64, 0), builtin_type],
                )
                fn_obj = self.builder.call(
                    self.runtime["py_func_new_named"],
                    [adapter, captures, self._attr_name_ptr("__new__")],
                    name=self._fresh(f"{builtin_name}.__new__.func"),
                )
                self._gc_release(captures)
                return fn_obj
        if expr.name == "join" and isinstance(expr.obj.ty, StrType):
            adapter_name = "__pcc_bound_str_join"
            adapter = self.module.globals.get(adapter_name)
            if not isinstance(adapter, ir.Function):
                adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
                adapter = ir.Function(self.module, adapter_ty, name=adapter_name)
                adapter.linkage = "internal"
                saved_builder = self.builder
                self.builder = ir.IRBuilder(adapter.append_basic_block("entry"))
                sep = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[0], ir.Constant(_I64, 0)],
                    name="str.join.sep",
                )
                argc = self.builder.call(
                    self.runtime["py_tuple_len"],
                    [adapter.args[1]],
                    name="str.join.argc",
                )
                valid = self.builder.icmp_signed(
                    "==",
                    argc,
                    ir.Constant(_I64, 1),
                    name="str.join.arity.ok",
                )
                ok_bb = adapter.append_basic_block("arity.ok")
                err_bb = adapter.append_basic_block("arity.err")
                self.builder.cbranch(valid, ok_bb, err_bb)
                self.builder.position_at_end(err_bb)
                self._emit_native_builtin_callable_type_error(
                    self.builder,
                    "str.join() takes exactly one argument",
                    "str_join",
                    "arity",
                )
                self.builder.position_at_end(ok_bb)
                iterable = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[1], ir.Constant(_I64, 0)],
                    name="str.join.iterable",
                )
                result = self.builder.call(
                    self.runtime["py_str_join"],
                    [sep, iterable],
                    name="str.join.result",
                )
                self.builder.call(self.runtime["py_decref"], [iterable])
                self.builder.call(self.runtime["py_decref"], [sep])
                self.builder.ret(result)
                self.builder = saved_builder
            sep = self._emit_as_object(expr.obj)
            captures = self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, 1)],
                name=self._fresh("str.join.captures"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [captures, ir.Constant(_I64, 0), sep],
            )
            fn_obj = self.builder.call(
                self.runtime["py_func_new_named"],
                [adapter, captures, self._attr_name_ptr("str.join")],
                name=self._fresh("str.join.bound"),
            )
            self._gc_release(captures)
            return fn_obj
        inspect_alias_attr = self._maybe_emit_inspect_alias_attr(expr)
        if inspect_alias_attr is not None:
            return inspect_alias_attr
        typing_alias_name = self._maybe_emit_typing_alias_name_attr(expr)
        if typing_alias_name is not None:
            return typing_alias_name
        enum_member_attr = self._maybe_emit_enum_member_attr(expr)
        if enum_member_attr is not None:
            return enum_member_attr
        if (
            isinstance(expr.obj, Attr)
            and isinstance(expr.obj.obj, Name)
            and expr.obj.name == "version_info"
            and self._native_builtin_module_for_name(expr.obj.obj.ident) == "sys"
        ):
            version_part = self._emit_sys_version_info_attr(expr.name)
            if version_part is not None:
                return version_part
        native_module_type_name = self._maybe_emit_native_module_type_name(expr)
        if native_module_type_name is not None:
            return native_module_type_name
        pathlib_suffix_attr = self._emit_native_pathlib_suffix_attr(expr)
        if pathlib_suffix_attr is not None:
            return pathlib_suffix_attr
        re_compile_method_attr = self._emit_native_re_compile_method_attr(expr)
        if re_compile_method_attr is not None:
            return re_compile_method_attr
        if self._native_builtin_value_kind_for_expr(expr.obj) == "os.path":
            # Target OS path constants. These are
            # plain literals — emitting them natively keeps ``os.path.<const>``
            # off the libpython fallback (generic B-P0-PKG fallback shrink).
            if expr.name == "sep":
                return self._emit_str_literal("\\" if self._target_sys_platform_text() == "win32" else "/")
            if expr.name == "extsep":
                return self._emit_str_literal(".")
            if expr.name == "pathsep":
                return self._emit_str_literal(";" if self._target_sys_platform_text() == "win32" else ":")
            # NOTE: ``os.path.defpath`` is intentionally NOT lowered — its value
            # is platform/build-variable (observed ``/bin:/usr/bin`` here vs the
            # CPython posixpath source literal ``:/bin:/usr/bin``), so a
            # hardcoded constant would silently diverge from the host. Leave it
            # to fall back rather than emit a possibly-wrong value.
            if expr.name == "devnull":
                return self._emit_str_literal("nul" if self._target_sys_platform_text() == "win32" else "/dev/null")
            if expr.name == "curdir":
                return self._emit_str_literal(".")
            if expr.name == "pardir":
                return self._emit_str_literal("..")
            if expr.name == "altsep":
                return self._emit_str_literal("/") if self._target_sys_platform_text() == "win32" else self._emit_none_literal()
        if (
            expr.name == "__name__"
            and isinstance(expr.obj, Call)
            and isinstance(expr.obj.func, Name)
            and expr.obj.func.ident == "type"
            and len(expr.obj.args) == 1
            and not self._expr_looks_cpython(expr.obj.args[0])
        ):
            # NOTE: skip this native ``type(x).__name__`` fast path when ``x`` is
            # a CPython value (e.g. a numpy array): ``py_obj_type_name`` uses
            # pcc's native type model and mishandles a real CPython object,
            # silently failing. For cpy args fall through to the cpy Call-receiver
            # branch below, which routes ``type(a).__name__`` via libpython
            # (``py_cpy_getattr`` on the real type object). Inert in no-libpython.
            # Inference is not an exact runtime-type proof. Folding from it
            # also drops evaluation of calls such as type(produce()).__name__.
            obj_val = self._emit_expr_as_pcc_object(expr.obj.args[0])
            result = self.builder.call(
                self.runtime["py_obj_type_name"],
                [obj_val],
                name=self._fresh("type.name"),
            )
            self._note_owned_dynamic_call_value(result)
            self._gc_release_if_owned(obj_val, expr.obj.args[0])
            return result
        module_object_export = self._native_module_object_export_info(
            expr.obj,
            expr.name,
        )
        if module_object_export is not None:
            module_name, info = module_object_export
            return self._emit_native_module_export_value(
                module_name,
                expr.name,
                info,
            )
        if expr.name == "__annotations__" and isinstance(expr.obj, Name):
            annotations = self._emit_function_annotations_dict(expr.obj.ident)
            if annotations is not None:
                return annotations
        valueclass_attr = self._maybe_emit_valueclass_payload_attr(expr)
        if valueclass_attr is not None:
            return valueclass_attr
        valueclass_dyn_attr = self._maybe_emit_valueclass_payload_attr_from_dyn(expr)
        if valueclass_dyn_attr is not None:
            return valueclass_dyn_attr
        alias_export = self._native_module_expr_export_info(
            expr.obj,
            expr.name,
        )
        if alias_export is not None:
            module_name, info = alias_export
            kind = info.get("kind")
            if kind == "class":
                if isinstance(expr.obj, Name):
                    class_info = self._ensure_native_module_alias_class_export(
                        expr.obj.ident,
                        expr.name,
                    )
                else:
                    class_info = None
                if class_info is not None:
                    return self.builder.load(
                        class_info.global_var,
                        name=self._fresh(f"cls.{expr.name}"),
                    )
            elif kind == "function":
                return self._emit_native_module_export_value(
                    module_name,
                    expr.name,
                    info,
                )
            elif kind == "constant":
                return self._emit_native_module_constant_or_override(
                    module_name,
                    expr.name,
                    info,
                )
            elif kind == "module_global":
                # ``mod.attr`` where ``attr`` is a module-level variable
                # (e.g. ``__all__ = [...]``) of a sibling-compiled module.
                # Without this branch the Attr lowering fell through to
                # the generic ``py_obj_getattr(<module_name_string>, attr)``
                # path, which failed because the module-alias is the
                # module's NAME STRING in pcc's compile (a string has no
                # ``__all__``).  Route through the existing
                # ``.modvar.<mod>.<attr>`` extern emitted by
                # ``_declare_native_module_extern_global``.  Surfaced by
                # numpy/__init__.py:681 ``set(_mat.__all__)`` where ``_mat``
                # aliases ``numpy.matrixlib``.  See investigation
                # ``docs/investigations/python-native-module-alias-module-global-attr-attribute-error.md``.
                return self._emit_native_module_global_attr_load(
                    module_name,
                    expr.name,
                    info,
                    expr.span,
                )
        if isinstance(expr.obj, Name):
            alias_export = self._native_module_alias_export_info(
                expr.obj.ident,
                expr.name,
            )
            if alias_export is not None:
                module_name, info = alias_export
                kind = info.get("kind")
                if kind == "class":
                    class_info = self._ensure_native_module_alias_class_export(
                        expr.obj.ident,
                        expr.name,
                    )
                    if class_info is not None:
                        return self.builder.load(
                            class_info.global_var,
                            name=self._fresh(f"cls.{expr.name}"),
                        )
                elif kind == "function":
                    return self._emit_native_module_export_value(
                        module_name,
                        expr.name,
                        info,
                    )
                elif kind == "constant":
                    return self._emit_native_module_constant_or_override(
                        module_name,
                        expr.name,
                        info,
                    )
                elif kind == "module_global":
                    # Same fix as above; see comment there.
                    return self._emit_native_module_global_attr_load(
                        module_name,
                        expr.name,
                        info,
                        expr.span,
                    )
            alias_module = getattr(self, "_native_module_aliases", {}).get(
                expr.obj.ident
            )
            if alias_module is not None:
                module_name_ptr = self._ptr_to_cstr(
                    self._cstr_global(
                        alias_module,
                        f".pcc.compiled.attr.module.{alias_module}",
                    )
                )
                dynamic_attr = self.builder.call(
                    self.runtime["py_module_attr_get"],
                    [module_name_ptr, self._attr_name_ptr(runtime_attr_name)],
                    name=self._fresh(f"compiled.module.{alias_module}.{expr.name}"),
                )
                self._emit_attribute_error_if_null(
                    dynamic_attr,
                    expr.name,
                    expr.span,
                )
                return dynamic_attr
            builtin_module = self._native_builtin_module_for_name(expr.obj.ident)
            if builtin_module is not None:
                builtin_attr = self._emit_native_builtin_module_attr(
                    builtin_module,
                    expr.name,
                )
                if builtin_attr is not None:
                    return builtin_attr
                module_attr = self._emit_native_module_attr_load(
                    builtin_module,
                    expr.name,
                    expr.span,
                )
                if module_attr is not None:
                    return module_attr
                if builtin_module == "math":
                    if expr.name in ("pi", "e", "tau", "inf", "nan"):
                        return self._emit_native_module_constant(
                            {
                                "value_kind": "float",
                                "value": {
                                    "pi": 3.141592653589793,
                                    "e": 2.718281828459045,
                                    "tau": 6.283185307179586,
                                    "inf": _MATH_INF,
                                    "nan": _MATH_NAN,
                                }[expr.name],
                            },
                        )
                if builtin_module == "string":
                    if expr.name == "ascii_lowercase":
                        return self._emit_str_literal("abcdefghijklmnopqrstuvwxyz")
                    if expr.name == "ascii_uppercase":
                        return self._emit_str_literal("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                    if expr.name == "ascii_letters":
                        return self._emit_str_literal(
                            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        )
                    if expr.name == "digits":
                        return self._emit_str_literal("0123456789")
                    if expr.name == "hexdigits":
                        return self._emit_str_literal("0123456789abcdefABCDEF")
                    if expr.name == "octdigits":
                        return self._emit_str_literal("01234567")
                    if expr.name == "punctuation":
                        return self._emit_str_literal(
                            "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
                        )
                    if expr.name == "whitespace":
                        return self._emit_str_literal(" \t\n\r\x0b\x0c")
                    if expr.name == "printable":
                        return self._emit_str_literal(
                            "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ "
                            "\t\n\r\x0b\x0c"
                        )
                if builtin_module == "re":
                    _RE_CONSTS = {
                        "I": 2,
                        "IGNORECASE": 2,
                        "M": 8,
                        "MULTILINE": 8,
                        "S": 16,
                        "DOTALL": 16,
                        "X": 64,
                        "VERBOSE": 64,
                    }
                    if expr.name in _RE_CONSTS:
                        return self.builder.call(
                            self.runtime["py_int_from_i64"],
                            [ir.Constant(_I64, _RE_CONSTS[expr.name])],
                            name=self._fresh(f"re.{expr.name}"),
                        )
                if builtin_module == "sys" and expr.name == "argv":
                    return self._emit_program_argv_list()
                if builtin_module == "sys" and expr.name == "executable":
                    return self.builder.call(
                        self.runtime["py_sys_executable_str"],
                        [],
                        name=self._fresh("sys.executable"),
                    )
                if builtin_module == "sys" and expr.name in (
                    "stdin",
                    "stdout",
                    "stderr",
                ):
                    # Value-position standard streams (``destination = sys.stderr``,
                    # ``print(..., file=out)``): a real file object over fd 0/1/2.
                    # Direct stream methods such as sys.stdin.readline() still
                    # lower through native_system.py.  This used to be the string
                    # "<sys.stderr>", so every stored stream failed on .write.
                    fd = {"stdin": 0, "stdout": 1, "stderr": 2}[expr.name]
                    return self.builder.call(
                        self.runtime["py_sys_stream_object"],
                        [ir.Constant(_I64, fd)],
                        name=self._fresh(f"sys.{expr.name}"),
                    )
                if builtin_module == "sys" and expr.name in ("prefix", "base_prefix"):
                    return self.builder.call(
                        self.runtime["py_sys_prefix_str"],
                        [
                            ir.Constant(
                                _I64,
                                1 if expr.name == "base_prefix" else 0,
                            )
                        ],
                        name=self._fresh(f"sys.{expr.name}"),
                    )
                if builtin_module == "sys" and expr.name == "platform":
                    return self.builder.call(
                        self.runtime["py_sys_platform_str"],
                        [],
                        name=self._fresh("sys.platform"),
                    )
                if builtin_module == "sys" and expr.name == "path":
                    # Closed-world ``sys.path`` — a list containing cwd
                    # as its only entry. Sufficient for ``sys.path[0]`` /
                    # ``len(sys.path)`` style probes; full CPython
                    # site-packages discovery would require boot-time
                    # import-machinery wiring that pcc's closed-world
                    # programs don't run.
                    return self.builder.call(
                        self.runtime["py_sys_path_list"],
                        [],
                        name=self._fresh("sys.path"),
                    )
                if builtin_module == "sys" and expr.name == "version_info":
                    return self._emit_sys_version_info_tuple()
                if builtin_module == "sys" and expr.name == "version":
                    return self._emit_str_literal(
                        PYTHON_TARGET_FULL_VERSION + " (pcc self-host)"
                    )
                if builtin_module == "gc" and expr.name == "garbage":
                    return self._emit_gc_garbage()
                if builtin_module == "gc" and expr.name == "callbacks":
                    return self.builder.call(
                        self.runtime["py_gc_callbacks_list"],
                        [],
                        name=self._fresh("gc.callbacks"),
                    )
                if builtin_module == "os":
                    # POSIX access(2) mode constants — same on every
                    # platform pcc supports (F_OK=0, X_OK=1, W_OK=2,
                    # R_OK=4). Emit as direct PyInt constants instead
                    # of routing through CPython's os module.
                    _OS_ACCESS_CONSTS = {
                        "F_OK": 0,
                        "X_OK": 1,
                        "W_OK": 2,
                        "R_OK": 4,
                    }
                    if expr.name in _OS_ACCESS_CONSTS:
                        return self.builder.call(
                            self.runtime["py_int_from_i64"],
                            [ir.Constant(_I64, _OS_ACCESS_CONSTS[expr.name])],
                            name=self._fresh(f"os.{expr.name}"),
                        )
                    if expr.name == "name":
                        return self._emit_str_literal("nt" if self._target_sys_platform_text() == "win32" else "posix")
                    # Constants follow the compilation target, including cross builds.
                    if expr.name == "sep":
                        return self._emit_str_literal("\\" if self._target_sys_platform_text() == "win32" else "/")
                    if expr.name == "curdir":
                        return self._emit_str_literal(".")
                    if expr.name == "pardir":
                        return self._emit_str_literal("..")
                    if expr.name == "extsep":
                        return self._emit_str_literal(".")
                    if expr.name == "devnull":
                        return self._emit_str_literal("nul" if self._target_sys_platform_text() == "win32" else "/dev/null")
                    if expr.name == "linesep":
                        return self._emit_str_literal("\r\n" if self._target_sys_platform_text() == "win32" else "\n")
                    if expr.name == "altsep":
                        return self._emit_str_literal("/") if self._target_sys_platform_text() == "win32" else self._emit_none_literal()
                    if expr.name == "pathsep":
                        return self._emit_str_literal(";" if self._target_sys_platform_text() == "win32" else ":")
                compiled_export = self._native_builtin_compiled_export_info(
                    expr.obj.ident,
                    expr.name,
                )
                if compiled_export is not None:
                    compiled_module, info = compiled_export
                    return self._emit_native_module_export_value(
                        compiled_module,
                        expr.name,
                        info,
                    )
                return self._emit_cpy_attr(
                    self._emit_cpython_module_value(builtin_module),
                    expr.name,
                )
            if (
                hasattr(self, "class_lowering")
                and expr.obj.ident in self.class_lowering.classes
            ):
                info = self.class_lowering.classes[expr.obj.ident]
                metaclass_descr = self._emit_metaclass_data_descriptor_get(
                    info,
                    expr.name,
                )
                if metaclass_descr is not None:
                    if isinstance(expr.ty, (IntType, FloatType, BoolType)):
                        return marshal.marshal_from_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            metaclass_descr,
                            expr.ty,
                        )
                    return metaclass_descr
                method_owner = self._resolve_method_mro(info.name, expr.name)
                class_attr_known = (
                    self.class_lowering.lookup_class_attr(info, expr.name) is not None
                )
                class_attr_state = getattr(
                    self,
                    "_class_attr_runtime_state",
                    {},
                ).get((info.name, expr.name))
                class_attr_runtime_candidate = (
                    class_attr_known
                    or class_attr_state == "live"
                    or class_attr_state == "unknown"
                    or class_attr_state == "deleted"
                )
                if method_owner is not None and expr.name in method_owner.methods:
                    method_kind = method_owner.method_kinds.get(expr.name, "instance")
                    method_fn = method_owner.methods[expr.name]
                    if class_attr_runtime_candidate:
                        if method_kind == "static":
                            static_fallback = self._emit_static_method_value(
                                method_owner,
                                expr.name,
                                method_fn,
                                cache=False,
                            )
                        elif method_kind == "classmethod":
                            receiver_cls = self.class_lowering._load_class_object(
                                info, f"cls.{expr.obj.ident}.method.recv",
                            )
                            static_fallback = self._emit_bound_class_method_value(
                                method_owner,
                                expr.name,
                                method_fn,
                                receiver_cls,
                            )
                        else:
                            static_fallback = self._emit_unbound_instance_method_value(
                                method_owner,
                                expr.name,
                                method_fn,
                                cache=False,
                            )
                        cls_obj = self.class_lowering._load_class_object(
                            info, f"cls.{expr.obj.ident}.attr.recv",
                        )
                        runtime_value = self.builder.call(
                            self.runtime["py_obj_getattr"],
                            [cls_obj, self._attr_name_ptr(runtime_attr_name)],
                            name=self._fresh(f"cls.{expr.obj.ident}.{expr.name}"),
                        )
                        self._emit_attribute_error_if_null(
                            runtime_value,
                            expr.name,
                            expr.span,
                        )
                        raw_method_ptr = self.builder.bitcast(
                            method_fn,
                            _CSTR,
                            name=self._fresh(f"raw.{expr.name}.method"),
                        )
                        runtime_is_raw_method = self.builder.icmp_unsigned(
                            "==",
                            runtime_value,
                            raw_method_ptr,
                            name=self._fresh(f"raw.{expr.name}.selected"),
                        )
                        result = self.builder.select(
                            runtime_is_raw_method,
                            static_fallback,
                            runtime_value,
                            name=self._fresh(f"cls.{expr.obj.ident}.{expr.name}.value"),
                        )
                        if isinstance(expr.ty, (IntType, FloatType, BoolType)):
                            return marshal.marshal_from_object(
                                self.builder,
                                self.module,
                                self.runtime,
                                result,
                                expr.ty,
                            )
                        return result
                    if method_kind == "static":
                        return self._emit_static_method_value(
                            method_owner,
                            expr.name,
                            method_fn,
                        )
                    elif method_kind == "classmethod":
                        receiver_cls = self.class_lowering._load_class_object(
                            info, f"cls.{expr.obj.ident}.method.recv",
                        )
                        return self._emit_bound_class_method_value(
                            method_owner,
                            expr.name,
                            method_fn,
                            receiver_cls,
                        )
                    else:
                        return self._emit_unbound_instance_method_value(
                            method_owner,
                            expr.name,
                            method_fn,
                        )
                cls_obj = self.class_lowering._load_class_object(
                    info, f"cls.{expr.obj.ident}.attr.recv",
                )
                result = self.builder.call(
                    self.runtime["py_obj_getattr"],
                    [cls_obj, self._attr_name_ptr(runtime_attr_name)],
                    name=self._fresh(f"cls.{expr.obj.ident}.{expr.name}"),
                )
                self._emit_attribute_error_if_null(result, expr.name, expr.span)
                if isinstance(expr.ty, (IntType, FloatType, BoolType)):
                    return marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        result,
                        expr.ty,
                    )
                return result
        # CPython-backed fast path: if the object evaluates to a
        # CPython ``PyObject*`` (either bound directly via a Name in
        # _cpy_module_env / _cpy_env_flags, or through a nested
        # ``a.b.c`` chain where an inner node is CPython), route the
        # attribute load through py_cpy_getattr. Otherwise fall
        # through to the pcc native path.
        if isinstance(expr.obj, Name):
            cpy_gv = getattr(self, "_cpy_module_env", {}).get(expr.obj.ident)
            if cpy_gv is not None:
                mod_val = self.builder.load(
                    cpy_gv, name=self._fresh(f"cpy.{expr.obj.ident}")
                )
                return self._emit_cpy_attr(mod_val, expr.name)
            if getattr(self, "_cpy_env_flags", {}).get(expr.obj.ident, False):
                obj_val = self._emit_expr(expr.obj)
                return self._emit_cpy_attr(obj_val, expr.name)
        if isinstance(expr.obj, (Attr, Subscript, Call)) and self._expr_looks_cpython(
            expr.obj
        ):
            chain_val = self._emit_expr(expr.obj)
            if chain_val in getattr(self, "_cpy_values", ()):
                return self._emit_cpy_attr(chain_val, expr.name)
            self._gc_release_if_owned(chain_val, expr.obj)
        if isinstance(expr.obj, BinOp) and self._expr_looks_cpython(expr.obj):
            # Attribute load on a BINARY-OP result that is itself a CPython value
            # (e.g. ``(a + b).dtype`` on numpy arrays): ``py_cpy_binop`` yields a
            # real CPython object, so the attribute must load via libpython. The
            # ``(Attr, Subscript, Call)`` branch above does not cover ``BinOp``;
            # gate STRUCTURALLY on ``_expr_looks_cpython`` (an operand is cpy) so
            # a non-cpy BinOp receiver is NOT pre-emitted here (avoids a double
            # eval of the binop in the native fall-through). Inert in no-libpython
            # (no CPython modules => never looks cpy => bootstrap unaffected).
            # Mirrors the BinOp-receiver method-call branch.
            chain_val = self._emit_expr(expr.obj)
            if chain_val in getattr(self, "_cpy_values", ()):
                return self._emit_cpy_attr(chain_val, expr.name)

        # Dynamically typed properties on ordinary objects use their live
        # descriptor. Typed results and explicit layouts retain their ABI.
        if isinstance(expr.obj, Name):
            hint = self.env_class_hint.get(expr.obj.ident)
            if hint is not None:
                info = self._resolve_property_mro(hint, expr.name)
                if info is not None:
                    # Classify the receiver, not an inherited declaration's
                    # owner. Typed results need a separate object-ABI design;
                    # their annotations cannot justify unboxing replacements.
                    hinted_info = self.class_lowering.classes.get(hint)
                    if (
                        isinstance(expr.ty, DynType)
                        and hinted_info is not None
                        and self.class_lowering.uses_live_class_attribute(
                            hinted_info, expr.name, expr.ty,
                        )
                    ):
                        return self.class_lowering.emit_live_class_attribute(expr)
                    getter = info.properties[expr.name]
                    obj_val = self._emit_expr(expr.obj)
                    if not self._property_accessor_takes_protocol_args(info, expr.name, "getter", getter):
                        # The runtime property call binds the getter's
                        # other parameters; a field-slot read would bypass it.
                        return self._emit_runtime_attr_read(expr, obj_val, runtime_attr_name)
                    return self.builder.call(
                        getter,
                        [obj_val],
                        name=self._fresh(f"prop.{expr.name}"),
                    )
                data_descr = self._class_attr_descriptor_class(
                    hint,
                    expr.name,
                )
                if data_descr is not None:
                    _owner_info, desc_info = data_descr
                    if "__get__" in desc_info.methods and (
                        "__set__" in desc_info.methods
                        or "__delete__" in desc_info.methods
                    ):
                        obj_val = self._emit_expr(expr.obj)
                        got = self._emit_data_descriptor_get(
                            hint,
                            expr.name,
                            obj_val,
                        )
                        if got is not None:
                            return got
                    # Non-data descriptors are lower priority than the
                    # instance dictionary. Let the generic runtime path
                    # try the instance dict instead of returning the
                    # class attribute object directly.
                    if "__get__" in desc_info.methods:
                        class_info = None
                    else:
                        class_info = self.class_lowering.classes.get(hint)
                else:
                    class_info = self.class_lowering.classes.get(hint)
                if (
                    class_info is not None
                    and self.class_lowering.lookup_field_index(
                        class_info,
                        expr.name,
                    )
                    is not None
                    and not self._class_has_property(class_info.name, expr.name)
                ):
                    # Python lookup precedence is data descriptor, then the
                    # instance, then a non-data/class attribute. A PEP 526
                    # annotation can leave a same-named class metadata slot
                    # while __init__ supplies the real instance field. The
                    # old class-attr-first fast path loaded that null metadata
                    # slot and made neighboring self-host state appear lost
                    # (notably _InferCtx.globals).
                    obj_val = self._emit_expr(expr.obj)
                    return self._unbox_scalar_attr_result(
                        self.class_lowering.emit_self_attr_load(
                            class_info,
                            expr.name,
                            obj_val,
                        ),
                        expr.ty,
                    )
                if class_info is not None and not (
                    self.class_lowering.class_attr_overridden_by_subclass(
                        class_info,
                        expr.name,
                    )
                ):
                    # A static class-attr load is only sound when no subclass
                    # overrides the attribute; the hinted local may hold a
                    # subclass instance. Otherwise fall through to the runtime
                    # MRO lookup below.
                    class_attr = self.class_lowering.emit_class_attr_load(
                        class_info,
                        expr.name,
                    )
                    class_attr_state = getattr(
                        self,
                        "_class_attr_runtime_state",
                        {},
                    ).get((class_info.name, expr.name))
                    class_attr_runtime_candidate = class_attr_state in (
                        "live",
                        "unknown",
                        "deleted",
                    )
                    if class_attr is not None and not class_attr_runtime_candidate:
                        return self._unbox_scalar_attr_result(class_attr, expr.ty)

        # Fast path for ``self.<attr>`` inside a method body: use the
        # declared-field index when known, otherwise fall through to the
        # generic ``py_obj_getattr`` call.
        current_class = self.current_class
        if (
            current_class is not None
            and isinstance(expr.obj, Name)
            and expr.obj.ident == "self"
        ):
            receiver_class_name = self._self_receiver_class_name()
            receiver_info = None
            if receiver_class_name is not None:
                receiver_info = self.class_lowering.classes.get(receiver_class_name)
            if receiver_info is None:
                receiver_info = current_class
            # Apply the same dynamic-result and receiver-layout boundary.
            info_p = self._resolve_property_mro(receiver_info.name, expr.name)
            if info_p is not None:
                if isinstance(expr.ty, DynType) and self.class_lowering.uses_live_class_attribute(
                    receiver_info, expr.name, expr.ty,
                ):
                    return self.class_lowering.emit_live_class_attribute(expr)
                getter = info_p.properties[expr.name]
                self_val = self.builder.load(
                    self.env["self"][0], name=self._fresh("self")
                )
                if not self._property_accessor_takes_protocol_args(info_p, expr.name, "getter", getter):
                    return self._emit_runtime_attr_read(expr, self_val, runtime_attr_name)
                return self.builder.call(
                    getter,
                    [self_val],
                    name=self._fresh(f"self.prop.{expr.name}"),
                )
            self_val = self.builder.load(self.env["self"][0], name=self._fresh("self"))
            method_fn = receiver_info.methods.get(expr.name)
            if method_fn is not None:
                return self._emit_bound_self_method_value(
                    receiver_info,
                    expr.name,
                    method_fn,
                    self_val,
                )
            return self._unbox_scalar_attr_result(
                self.class_lowering.emit_self_attr_load(
                    receiver_info, expr.name, self_val
                ),
                expr.ty,
            )
        if (
            current_class is not None
            and isinstance(expr.obj, Name)
            and expr.obj.ident == "cls"
            and self.current_method_kind == "classmethod"
        ):
            cls_val = self._emit_expr(expr.obj)
            result = self.builder.call(
                self.runtime["py_obj_getattr"],
                [cls_val, self._attr_name_ptr(runtime_attr_name)],
                name=self._fresh(f"cls.attr.{expr.name}"),
            )
            self._emit_attribute_error_if_null(result, expr.name, expr.span)
            if isinstance(expr.ty, (IntType, FloatType, BoolType)):
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    result,
                    expr.ty,
                )
            return result

        # A receiver whose class is known statically -- a hinted local, or an
        # attribute chain such as ``self.parent.env`` whose inferred type is a
        # (possibly cross-module) class -- reads a declared field by index
        # instead of a by-name lookup over every field name.
        if isinstance(expr.obj, Name) or (
            isinstance(expr.obj, Attr) and _class_type_like(expr.obj.ty) is not None
        ):
            hint = self._class_hint_for_expr(expr.obj)
            if hint is not None:
                class_info = self.class_lowering.classes.get(hint)
                if (
                    class_info is not None
                    and self.class_lowering.lookup_field_index(
                        class_info,
                        expr.name,
                    )
                    is not None
                    and not self._class_has_property(class_info.name, expr.name)
                ):
                    obj_val = self._emit_expr(expr.obj)
                    field_value = self._unbox_scalar_attr_result(
                        self.class_lowering.emit_self_attr_load(
                            class_info,
                            expr.name,
                            obj_val,
                        ),
                        expr.ty,
                    )
                    if isinstance(expr.obj, Attr):
                        # The receiver's own read produced an owner (a field
                        # getter returns a new reference); release it as the
                        # by-name read does once the field is loaded.
                        self._gc_release_if_owned(obj_val, expr.obj)
                    return field_value

        obj = self._emit_expr(expr.obj)
        return self._emit_runtime_attr_read(expr, obj, runtime_attr_name)

    def _emit_runtime_attr_read(self, expr: Attr, obj: ir.Value, runtime_attr_name: str) -> ir.Value:
        # Any object goes through py_obj_getattr; if the object is
        # ``None`` at runtime the runtime lib raises AttributeError —
        # that's the correct Python semantic (no segfault).
        name_ptr = self._attr_name_ptr(runtime_attr_name)
        result = self.builder.call(
            self.runtime["py_obj_getattr"],
            [obj, name_ptr],
            name=self._fresh(f"attr.{expr.name}"),
        )
        self._emit_attribute_error_if_null(result, expr.name, expr.span)
        if isinstance(expr.ty, (IntType, FloatType, BoolType)):
            native_result = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                result,
                expr.ty,
            )
            # py_obj_getattr returns a NEW reference and marshal only
            # unpacks (marshal.py contains no releases); once the scalar is
            # extracted the boxed result is ours to drop.
            self._gc_release(
                result, self._release_context_label(f"attr.{expr.name}")
            )
            self._gc_release_if_owned(obj, expr.obj)
            return native_result
        # Register the NEW reference at the emitter — the only place that
        # knows the raising getattr ran rather than the borrowed field
        # read.  Consumers release through _gc_release_if_owned, whose
        # AST-shape classifier says not-owned for Attr; the registry
        # overrides it (same mechanism as the dynamic-call path).  See
        # docs/goal/evidence/2026-08-25-attr-getattr-ownership-investigation.md.
        self._note_owned_dynamic_call_value(result)
        self._gc_release_if_owned(obj, expr.obj)
        return result

    def _unbox_scalar_attr_result(self, result: ir.Value, result_ty: Type) -> ir.Value:
        """Normalize boxed instance/class slots to the inferred scalar ABI.

        Instance fields and class attributes are stored as ``PyObject*`` even
        when inference knows the expression is an int, float, or bool.  The
        generic getattr path already performs this conversion; keep the known-
        class fast paths on the same boundary so their consumers never receive
        a pointer where LLVM requires a native scalar.
        """
        if isinstance(result_ty, (IntType, FloatType, BoolType)):
            native_result = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                result,
                result_ty,
            )
            if self._value_is_owned_object(result):
                self._gc_release(result, self._release_context_label("attr.scalar"))
            return native_result
        return result
