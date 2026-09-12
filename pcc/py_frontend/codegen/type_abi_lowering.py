"""Type and ABI mapping helpers for Layer-1 Python codegen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Arg,
    BoolType,
    ByteArrayType,
    BytesType,
    ClassDef,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    FloatType,
    FuncDef,
    FuncType,
    IntType,
    Import,
    ImportFrom,
    ListType,
    MemoryViewType,
    NoneType,
    StrType,
    TupleType,
    Type,
    ValueArrayType,
)
from .errors import L1CodegenError
from .layer1_support import _import_from_module_or_empty, _stmt_kind_name

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_CSTR = _I8.as_pointer()
_VOID = ir.VoidType()
_ClassDef = ClassDef
_PY_EXC_TYPEERROR = 3


class TypeAbiLoweringMixin:
    def _module_imports_raw_int_scaffold(self) -> bool:
        mod_name = self.ast_module.name or ""
        if mod_name == "pcc" or mod_name.startswith("pcc."):
            return True
        if mod_name == "bootstrap" or mod_name.startswith("bootstrap."):
            return True
        for stmt in self.ast_module.body:
            if isinstance(stmt, ImportFrom):
                import_module = _import_from_module_or_empty(stmt)
                if (
                    self._is_extern_scaffold_import_module(import_module)
                    or import_module == "pcc.unsafe"
                ):
                    return True
            if isinstance(stmt, Import):
                for mod_name, _as_name in stmt.names:
                    if (
                        self._is_extern_scaffold_import_module(mod_name)
                        or mod_name == "pcc.unsafe"
                    ):
                        return True
        return False

    def _module_imports_c_abi_export(self) -> bool:
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))
        stmt_index = 0
        for stmt in self.ast_module.body:
            if debug_codegen:
                sys.stderr.write(
                    "[pcc.codegen] "
                    + (self.ast_module.name or "<module>")
                    + ":module flags c-abi scan "
                    + str(stmt_index)
                    + " "
                    + _stmt_kind_name(stmt)
                    + "\n"
                )
            if isinstance(stmt, FuncDef):
                if debug_codegen:
                    sys.stderr.write(
                        "[pcc.codegen] "
                        + (self.ast_module.name or "<module>")
                        + ":module flags c-abi func decorators\n"
                    )
                decorators = self._func_decorators(stmt)
                if debug_codegen:
                    sys.stderr.write(
                        "[pcc.codegen] "
                        + (self.ast_module.name or "<module>")
                        + ":module flags c-abi func decorator count "
                        + str(len(decorators))
                        + "\n"
                    )
                dec_index = 0
                while dec_index < len(decorators):
                    dec = decorators[dec_index]
                    if self._decorator_c_abi_export_symbol(dec) is not None:
                        return True
                    dec_index += 1
            if isinstance(stmt, _ClassDef):
                if debug_codegen:
                    sys.stderr.write(
                        "[pcc.codegen] "
                        + (self.ast_module.name or "<module>")
                        + ":module flags c-abi class body\n"
                    )
                for class_stmt in stmt.body:
                    if not isinstance(class_stmt, FuncDef):
                        continue
                    decorators = self._func_decorators(class_stmt)
                    if debug_codegen:
                        sys.stderr.write(
                            "[pcc.codegen] "
                            + (self.ast_module.name or "<module>")
                            + ":module flags c-abi method decorator count "
                            + str(len(decorators))
                            + "\n"
                        )
                    dec_index = 0
                    while dec_index < len(decorators):
                        dec = decorators[dec_index]
                        if self._decorator_c_abi_export_symbol(dec) is not None:
                            return True
                        dec_index += 1
            stmt_index += 1
        return False

    def _should_box_python_ints(self) -> bool:
        return not self._module_uses_raw_int_scaffold

    def _int_exprs_are_boxed(self) -> bool:
        return bool(self._box_int_locals)

    def _storage_ir_type(self, ty: Type) -> ir.Type:
        if isinstance(ty, IntType) and self._int_exprs_are_boxed():
            return _CSTR
        return self._map_type(ty)

    def _local_slot_ir_type(self, ident: str, ty: Type) -> ir.Type:
        """The slot shape for a named local.

        A name written through both an unboxed float/bool and an object needs
        the object slot on every edge; ``_storage_ir_type`` only sees the type
        of one binding, so the function-level plan decides.  Without it the
        slot took the first binding's shape and later stores were coerced into
        it -- turning ``x = SomeClass(...)`` after ``x = 1.0`` into
        ``py_float_to_f64`` on the instance.
        """
        if ident in getattr(self, "_planned_object_local_names", set()):
            return _CSTR
        return self._storage_ir_type(ty)

    def _local_slot_decl_type(self, ident: str, ty: Type) -> Type:
        """The semantic type recorded for a named local's slot.

        A planned-object local holds an object on every edge, and which kind
        varies per binding, so the slot's semantic type is ``dyn``.  Keeping
        the first binding's ``float`` would make later reads interpret the
        stored pointer as an unboxed double.
        """
        if ident in getattr(self, "_planned_object_local_names", set()):
            return DynType(name="dyn")
        return ty

    def _abi_ir_type(self, ty: Type, *, box_int_abi: bool) -> ir.Type:
        if box_int_abi and isinstance(ty, IntType):
            return _CSTR
        return self._map_type(ty)

    def _export_box_int_abi(self, info: dict) -> bool:
        return bool(info.get("box_int_abi", self._should_box_python_ints()))

    def _funcdef_uses_boxed_int_abi(
        self,
        fd: FuncDef,
        *,
        c_abi_sym: str | None,
    ) -> bool:
        if c_abi_sym is not None:
            return False
        if not self._should_box_python_ints():
            # ADMISSION DEMOTION (INT-P0-PROJ, slice 1).
            #
            # `_should_box_python_ints()` is False for every `pcc.*` module, so
            # `int` there silently means a raw machine integer.  The project
            # contract says the opposite: `int` is arbitrary-precision, the i64
            # lane is legitimate only as a PROVEN-in-range optimization, and a
            # value-lane overflow must promote rather than wrap.  A module name
            # is an assumption, not a proof.
            #
            # A constructor parameter carries a declared field's value, and a
            # field annotated `int` has no range proof at all -- a machine
            # integer there would have to be spelled `pcc.i64`.  So `__init__`
            # keeps the object projection even in a raw-int-scaffold module.
            # Everything else stays in the i64 lane, so this does not disturb
            # the hot paths.
            #
            # This is what made pcc1 lower every source literal above 2**63-1
            # to 0: the parser builds `pa.IntLit(span, ty, int(e.text, 0))`, and
            # the bignum died on the `value: int` parameter -- the field itself
            # already stores a pointer.
            return fd.name == "__init__"
        return not self._funcdef_uses_unboxed_typed_int_abi(fd)

    def _map_type(self, ty: Type) -> ir.Type:
        """Map a pcc_py :class:`Type` to its LLVM IR representation.

        Phase 1 scalars lower to native types; Phase 2 object types
        (str / list / dict / tuple / None) lower to ``PyObject*`` (an
        opaque pointer).
        """
        if isinstance(ty, IntType):
            # We always lower to i64 in L1 regardless of the declared
            # width; the type-infer layer is expected to have
            # range-checked narrower widths already. The ``width`` field
            # will matter once tagged-int codegen lands in Phase 2.
            return _I64
        if isinstance(ty, FloatType):
            return _DOUBLE
        if isinstance(ty, BoolType):
            return _I1
        if isinstance(ty, ValueArrayType):
            payload_ty = self._value_array_payload_ir_type(ty)
            if payload_ty is not None:
                return payload_ty
        if isinstance(ty, ClassType) and bool(getattr(ty, "valueclass", False)):
            payload_ty = self._valueclass_payload_ir_type(ty)
            if payload_ty is not None:
                return payload_ty
        if isinstance(
            ty,
            (
                StrType,
                BytesType,
                ByteArrayType,
                MemoryViewType,
                ListType,
                DictType,
                TupleType,
                ClassType,
                ComplexType,
            ),
        ):
            return _CSTR  # alias for i8* == PyObject*
        if isinstance(ty, NoneType):
            # None is a PyObject* (points to the global ``py_None``).
            # Using a pointer (not void) lets us store and load None in
            # locals uniformly with other object types.
            return _CSTR
        if isinstance(ty, DynType):
            # A generic PyObject* slot: covers class instances, results
            # of ``MyClass(args)`` construction, attribute fetches, and
            # anything else the type inferer did not narrow.
            return _CSTR
        if isinstance(ty, FuncType):
            # A first-class function value — at L1 the callable is
            # wrapped as a CPython object (lambda lowered to
            # ``operator.<getter>`` or a hoisted pcc FuncDef exposed
            # through PyCFunction wrapping). Either way the local slot
            # holds an opaque PyObject* pointer.
            return _CSTR
        if isinstance(ty, Type) or getattr(ty, "name", None) in (
            "None",
            "dyn",
            "Type",
        ):
            # A bare Type object means inference preserved an opaque runtime
            # type value rather than a concrete pcc scalar/container type.
            # Store it as PyObject* instead of failing the self-host path.
            return _CSTR
        raise NotImplementedError(
            f"Layer 1 does not handle type {type(ty).__name__} "
            f"(name={getattr(ty, 'name', '?')!r})"
        )

    def _value_array_payload_ir_type(self, ty: Type) -> Optional[ir.Type]:
        if not isinstance(ty, ValueArrayType):
            return None
        elem_ir_ty = self._valueclass_payload_ir_type(ty.elem)
        if elem_ir_ty is None:
            return None
        if ty.length == 1:
            return ir.LiteralStructType((elem_ir_ty,))
        if ty.length == 2:
            return ir.LiteralStructType((elem_ir_ty, elem_ir_ty))
        if ty.length == 3:
            return ir.LiteralStructType((elem_ir_ty, elem_ir_ty, elem_ir_ty))
        if ty.length == 4:
            return ir.LiteralStructType(
                (elem_ir_ty, elem_ir_ty, elem_ir_ty, elem_ir_ty)
            )
        if ty.length == 5:
            return ir.LiteralStructType(
                (elem_ir_ty, elem_ir_ty, elem_ir_ty, elem_ir_ty, elem_ir_ty)
            )
        if ty.length == 6:
            return ir.LiteralStructType(
                (
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                )
            )
        if ty.length == 7:
            return ir.LiteralStructType(
                (
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                    elem_ir_ty,
                )
            )
        return None

    def _valueclass_payload_ir_type(self, ty: Type) -> Optional[ir.Type]:
        if not isinstance(ty, ClassType):
            return None
        if not bool(getattr(ty, "valueclass", False)):
            return None
        if len(ty.fields) == 0:
            return None
        field_ir_types: list[ir.Type] = []
        for _field_name, field_ty in ty.fields:
            field_ir_ty = self._valueclass_field_payload_ir_type(field_ty)
            if field_ir_ty is None:
                return None
            field_ir_types.append(field_ir_ty)
        # The scaffold has a dynamic LiteralStructType constructor for a
        # runtime-built list.  The former 1..7 source unroll predated that
        # path and silently changed an eight-field valueclass into the object
        # ABI.  Field *types* may still reject projection above; field count
        # alone must not.
        return ir.LiteralStructType(field_ir_types)

    def _valueclass_field_payload_ir_type(
        self,
        field_ty: Type,
    ) -> Optional[ir.Type]:
        if isinstance(field_ty, IntType):
            return _I64
        if isinstance(field_ty, FloatType):
            return _DOUBLE
        if isinstance(field_ty, BoolType):
            return _I1
        if isinstance(field_ty, ClassType) and bool(
            getattr(field_ty, "valueclass", False)
        ):
            return self._valueclass_payload_ir_type(field_ty)
        if isinstance(
            field_ty,
            (
                StrType,
                BytesType,
                ByteArrayType,
                MemoryViewType,
                ListType,
                DictType,
                TupleType,
                ClassType,
                NoneType,
                DynType,
                FuncType,
                ComplexType,
            ),
        ):
            return _CSTR
        if isinstance(field_ty, Type) or getattr(field_ty, "name", None) in (
            "None",
            "dyn",
            "Type",
        ):
            return _CSTR
        return None

    def _is_valueclass_payload_type(self, ty: Type) -> bool:
        return self._valueclass_payload_ir_type(ty) is not None

    def _valueclass_payload_pointer_field_paths(
        self,
        ty: Type,
        prefix: tuple[int, ...] = (),
    ) -> tuple[tuple[int, ...], ...]:
        if not isinstance(ty, ClassType):
            return ()
        if not bool(getattr(ty, "valueclass", False)):
            return ()
        paths: list[tuple[int, ...]] = []
        for idx, (_field_name, field_ty) in enumerate(ty.fields):
            field_path = prefix + (idx,)
            if self._is_valueclass_payload_type(field_ty):
                paths.extend(
                    self._valueclass_payload_pointer_field_paths(
                        field_ty,
                        field_path,
                    )
                )
                continue
            field_ir_ty = self._valueclass_field_payload_ir_type(field_ty)
            if field_ir_ty is not None and self._ir_type_matches(field_ir_ty, _CSTR):
                paths.append(field_path)
        return tuple(paths)

    def _emit_entry_valueclass_payload_field_slot(
        self,
        payload_alloca: ir.Value,
        field_path: tuple[int, ...],
        name: str,
    ) -> ir.Value:
        fn = self.current_function
        entry = getattr(self, "_current_entry_block", None)
        if entry is None:
            entry = fn.blocks[0]
        saved_block = getattr(self.builder, "_block", None)
        alloca_index = -1
        alloca_prefix = str(payload_alloca) + " = alloca "
        instr_index = 0
        for instr in entry._instrs:
            if str(instr.text).startswith(alloca_prefix):
                alloca_index = instr_index
                break
            instr_index += 1
        if alloca_index >= 0:
            # Field roots are pointers into the payload alloca. They must be
            # entry-local so every exit can leave them, but they also must
            # follow the alloca they GEP from. Do not update the alloca
            # insertion cache here: future allocas should still be free to
            # insert before this non-alloca root setup.
            if alloca_index + 1 < len(entry._instrs):
                self.builder.position_before(entry._instrs[alloca_index + 1])
            else:
                self.builder.position_at_end(entry)
        else:
            insert_before = None
            for instr in entry._instrs:
                if self._instruction_opname_text(instr) != "alloca":
                    insert_before = instr
                    break
            if insert_before is not None:
                self.builder.position_before(insert_before)
            else:
                self.builder.position_at_end(entry)
        indices = [ir.Constant(_I32, 0)]
        for idx in field_path:
            indices.append(ir.Constant(_I32, idx))
        field_slot = self.builder.gep(
            payload_alloca,
            indices,
            inbounds=True,
            name=name,
        )
        self.builder.store(ir.Constant(_CSTR, None), field_slot)
        if saved_block is not None:
            self.builder.position_at_end(saved_block)
        return field_slot

    def _ensure_valueclass_payload_gc_roots(
        self,
        name: str,
        payload_alloca: ir.Value,
        ty: Type,
        *,
        borrowed: bool = True,
    ) -> None:
        if self.current_func_def is None:
            return
        if name in getattr(self, "_current_global_names", set()):
            return
        paths = self._valueclass_payload_pointer_field_paths(ty)
        if not paths:
            return
        fn = self.current_function
        if fn is None:
            return
        if not hasattr(self, "_fn_valueclass_payload_root_slots"):
            self._fn_valueclass_payload_root_slots = {}
        registry = self._fn_valueclass_payload_root_slots.setdefault(fn.name, [])
        frame_map = (
            self._gc_one_slot_borrowed_frame_map()
            if borrowed
            else self._gc_one_slot_frame_map()
        )
        for path in paths:
            already_registered = False
            for registered_alloca, registered_path in registry:
                if registered_alloca is payload_alloca and registered_path == path:
                    already_registered = True
                    break
            if already_registered:
                continue
            path_suffix = "_".join(str(i) for i in path)
            field_slot = self._emit_entry_valueclass_payload_field_slot(
                payload_alloca,
                path,
                self._fresh(f"{name}.value.root.{path_suffix}"),
            )
            registry.append((payload_alloca, path))
            root_name = f"{name}.$valuefield.{path_suffix}"
            self._ensure_local_gc_frame_root(root_name, field_slot, _CSTR, frame_map)

    def _valueclass_field_info(
        self,
        ty: Type,
        attr_name: str,
    ) -> Optional[tuple[int, Type]]:
        if not isinstance(ty, ClassType):
            return None
        if not bool(getattr(ty, "valueclass", False)):
            return None
        for idx, (field_name, field_ty) in enumerate(ty.fields):
            if field_name == attr_name:
                return idx, field_ty
        return None

    def _emit_valueclass_payload_to_object(
        self,
        value: ir.Value,
        ty: Type,
        *,
        consume_fields: bool = False,
    ) -> Optional[ir.Value]:
        if not isinstance(ty, ClassType):
            return None
        if not self._is_valueclass_payload_type(ty):
            return None
        if isinstance(value.type, ir.PointerType):
            return value
        if not hasattr(self, "class_lowering"):
            return None
        class_name = self._ensure_class_type_registered(ty)
        if class_name is None:
            return None
        info = self.class_lowering.classes.get(class_name)
        if info is None:
            return None

        from . import marshal

        cls_ptr = self.builder.load(
            info.global_var,
            name=self._fresh(f"value.{ty.name}.class"),
        )
        inst = self.builder.call(
            self.runtime["py_valuebox_new"],
            [cls_ptr],
            name=self._fresh(f"value.{ty.name}.box"),
        )
        for idx, (_field_name, field_ty) in enumerate(ty.fields):
            field_value = self.builder.extract_value(
                value,
                [idx],
                name=self._fresh(f"value.{ty.name}.box.field{idx}"),
            )
            field_obj = self._emit_valueclass_payload_to_object(
                field_value,
                field_ty,
                consume_fields=consume_fields,
            )
            if field_obj is None:
                field_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    field_value,
                    field_ty,
                )
            self.builder.call(
                self.runtime["py_valuebox_set_field"],
                [inst, ir.Constant(_I32, idx), field_obj],
            )
            if consume_fields and isinstance(field_obj.type, ir.PointerType):
                if field_obj not in getattr(self, "_cpy_values", ()):
                    self._gc_release(
                        field_obj,
                        self._release_context_label("valuebox_field_transfer"),
                    )
        return inst

    def _emit_object_to_valueclass_payload(
        self,
        value: ir.Value,
        ty: Type,
    ) -> Optional[ir.Value]:
        if not isinstance(ty, ClassType):
            return None
        payload_ty = self._valueclass_payload_ir_type(ty)
        if payload_ty is None:
            return None
        if not isinstance(value.type, ir.PointerType):
            if str(value.type) == str(payload_ty):
                return value
            return None

        from . import marshal

        if not hasattr(self, "class_lowering"):
            return None
        class_name = self._ensure_class_type_registered(ty)
        if class_name is None:
            return None
        info = self.class_lowering.classes.get(class_name)
        if info is None:
            return None
        cls_ptr = self.builder.load(
            info.global_var,
            name=self._fresh(f"value.{ty.name}.unbox.class"),
        )
        is_instance = self.builder.call(
            self.runtime["py_obj_isinstance"],
            [value, cls_ptr],
            name=self._fresh(f"value.{ty.name}.unbox.isinstance"),
        )
        is_instance_ok = self.builder.icmp_signed(
            "!=",
            is_instance,
            ir.Constant(_I64, 0),
            name=self._fresh(f"value.{ty.name}.unbox.isinstance.ok"),
        )
        fn = self.current_function
        fail_bb = fn.append_basic_block(
            name=self._fresh(f"value.{ty.name}.unbox.typeerror"),
        )
        ok_bb = fn.append_basic_block(
            name=self._fresh(f"value.{ty.name}.unbox.ok"),
        )
        self.builder.cbranch(is_instance_ok, ok_bb, fail_bb)

        self.builder.position_at_end(fail_bb)
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [
                ir.Constant(_I64, _PY_EXC_TYPEERROR),
                self._ptr_to_cstr(
                    self._cstr_global(
                        f"expected {ty.name} valueclass instance",
                        self._fresh(f".err.value.{ty.name}.unbox"),
                    )
                ),
            ],
            name=self._fresh(f"value.{ty.name}.unbox.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(ok_bb)

        payload_slot = self._alloca_in_entry(
            payload_ty,
            name=self._fresh(f"value.{ty.name}.unbox.tmp"),
        )
        zero = ir.Constant(_I32, 0)
        for idx, (_field_name, field_ty) in enumerate(ty.fields):
            field_obj = self.builder.call(
                self.runtime["py_valuebox_get_field"],
                [value, ir.Constant(_I32, idx)],
                name=self._fresh(f"value.{ty.name}.unbox.field{idx}.obj"),
            )
            field_value = self._emit_object_to_valueclass_payload(field_obj, field_ty)
            if field_value is None:
                field_value = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    field_obj,
                    field_ty,
                )
            field_ptr = self.builder.gep(
                payload_slot,
                [zero, ir.Constant(_I32, idx)],
                inbounds=True,
                name=self._fresh(f"value.{ty.name}.unbox.field{idx}"),
            )
            self.builder.store(field_value, field_ptr)
        return self.builder.load(
            payload_slot,
            name=self._fresh(f"value.{ty.name}.unbox.payload"),
        )

    def _is_scalar(self, ty: Type) -> bool:
        return isinstance(ty, (IntType, FloatType, BoolType))

    def _is_object(self, ty: Type) -> bool:
        if isinstance(ty, ValueArrayType):
            if self._value_array_payload_ir_type(ty) is not None:
                return False
        if isinstance(ty, ClassType) and bool(getattr(ty, "valueclass", False)):
            if self._is_valueclass_payload_type(ty):
                return False
        if isinstance(ty, Type) and not isinstance(ty, (IntType, FloatType, BoolType)):
            return True
        if getattr(ty, "name", None) in ("None", "dyn", "Type"):
            return True
        return isinstance(
            ty,
            (
                StrType,
                BytesType,
                ByteArrayType,
                MemoryViewType,
                ListType,
                DictType,
                TupleType,
                ClassType,
                NoneType,
                DynType,
                FuncType,
                ComplexType,
            ),
        )

    def _param_ir_and_bind_type(
        self,
        arg,
        *,
        require_annotation: bool,
        owner_name: str,
        box_int_params: bool = False,
    ) -> tuple[ir.Type, Type | None]:
        """Return the IR param type plus the env-binding type for ``arg``.

        ``*args`` and ``**kwargs`` lower as ordinary PyObject* params
        carrying a tuple / dict value respectively. That keeps function
        bodies compilable even before full L3 vararg semantics land.
        """
        if arg.kind in ("pos", "pos_only", "kw_only"):
            try:
                annotation = arg.annotation
            except AttributeError:
                annotation = None
            if annotation is None:
                if require_annotation:
                    raise L1CodegenError(
                        f"Layer 1 requires an annotation on parameter "
                        f"{arg.name!r} of function {owner_name!r}"
                    )
                return _CSTR, DynType(name="dyn")
            if box_int_params and isinstance(annotation, IntType):
                return _CSTR, annotation
            return self._map_type(annotation), annotation
        if arg.kind == "*args":
            return _CSTR, TupleType(name="tuple", elems=())
        if arg.kind == "**kwargs":
            return _CSTR, DictType(
                name="dict",
                key=StrType(name="str"),
                value=DynType(name="dyn"),
            )
        raise NotImplementedError(
            f"Layer 1 parameter kind {arg.kind!r} "
            f"(in function {owner_name!r}) not supported"
        )

    # -- nested-def hoisting -------------------------------------------
