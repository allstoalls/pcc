"""Type and ABI mapping helpers for Layer-1 Python codegen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    RawPointerType,
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
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1_support import _import_from_module_or_empty, _stmt_kind_name

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
                    "[pcc.frontends.c.codegen] "
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
                        "[pcc.frontends.c.codegen] "
                        + (self.ast_module.name or "<module>")
                        + ":module flags c-abi func decorators\n"
                    )
                decorators = self._func_decorators(stmt)
                if debug_codegen:
                    sys.stderr.write(
                        "[pcc.frontends.c.codegen] "
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
                        "[pcc.frontends.c.codegen] "
                        + (self.ast_module.name or "<module>")
                        + ":module flags c-abi class body\n"
                    )
                for class_stmt in stmt.body:
                    if not isinstance(class_stmt, FuncDef):
                        continue
                    decorators = self._func_decorators(class_stmt)
                    if debug_codegen:
                        sys.stderr.write(
                            "[pcc.frontends.c.codegen] "
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
        if isinstance(ty, IntType) and ty.name == "int" and self._int_exprs_are_boxed():
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
        if box_int_abi and isinstance(ty, IntType) and ty.name == "int":
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
        if (
            c_abi_sym is not None
            or self._freestanding_module
            or self._runtime_port_module
        ):
            return False
        if not fd.is_method and self._native_boxed_int_functions.get(fd.name, False):
            return True
        if not self._should_box_python_ints():
            # Module names and unsafe imports are not integer range proofs.
            # Preserve the ordinary Python integer ABI on constructors and
            # every signature carrying an unbounded Python int. Explicit
            # machine types and no-int scaffold signatures keep their lanes.
            python_int_signature = fd.name == "__init__"
            if isinstance(fd.return_ty, IntType) and fd.return_ty.name == "int":
                python_int_signature = True
            for arg in fd.args:
                annotation = arg.annotation
                if isinstance(annotation, IntType) and annotation.name == "int":
                    python_int_signature = True
            if not python_int_signature:
                return False
        return not self._funcdef_uses_unboxed_typed_int_abi(fd)

    def _map_type(self, ty: Type) -> ir.Type:
        """Map a pcc_py :class:`Type` to its LLVM IR representation.

        Phase 1 scalars lower to native types; Phase 2 object types
        (str / list / dict / tuple / None) lower to ``PyObject*`` (an
        opaque pointer).
        """
        if isinstance(ty, RawPointerType):
            return _CSTR
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
            if isinstance(field_ty, RawPointerType):
                continue
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
        instr_index = 0
        for instr in entry._instrs:
            if self._record_defines_value(instr, payload_alloca):
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
        """Register actual managed leaves, with ownership attached to storage.

        Parameters borrow their caller's leaves. Every constructed or copied
        local payload owns each managed leaf independently; a struct copy is
        never evidence that the copied pointers carry new references.
        """
        if name in getattr(self, "_current_global_names", set()):
            return
        fn = self.current_function
        if fn is None:
            return
        registry = self._fn_valueclass_payload_root_slots.setdefault(fn.name, [])
        for path in self._valueclass_payload_pointer_field_paths(ty):
            if any(record[0] is payload_alloca and record[1] == path for record in registry):
                continue
            suffix = "_".join(str(index) for index in path)
            field_slot = self._emit_entry_valueclass_payload_field_slot(
                payload_alloca, path, self._fresh(name + ".value.root." + suffix),
            )
            root_name = name + ".$valuefield." + suffix
            frame_map = self._gc_one_slot_borrowed_frame_map() if borrowed else self._gc_one_slot_frame_map()
            self._ensure_local_gc_frame_root(
                root_name, field_slot, _CSTR, frame_map, allow_module=True,
            )
            registry.append((payload_alloca, path, field_slot, borrowed))
            if not borrowed:
                flag = self._ensure_owned_local_flag(root_name, field_slot, allow_module=True)
                self._slot_call_root_records.append((field_slot, flag, False))
                self._ensure_valueclass_error_owner()
                # A loop can reach an earlier emitted return after a later
                # lexical producer ran on its preceding iteration. New leaf
                # owners need retroactive disposal, as well as frame leave.
                for site in self._fn_gc_root_exit_sites.get(fn.name, ()):
                    anchor = next((item for item in site._instrs if self._instruction_opname_text(item) != "phi"), None)
                    if anchor is not None:
                        saved = self.builder._block
                        self.builder.position_before(anchor)
                        self.builder.store(ir.Constant(_I1, 0), flag)
                        self.builder.call(self.runtime["pcc_gc_store_root"], [self._as_gc_ptr(field_slot), ir.Constant(_CSTR, None)])
                        self.builder.position_at_end(saved)

    def _ensure_valueclass_error_owner(self):
        """Keep the selecting exception alive while payload finalizers run."""
        function = self.current_function
        if function.name in self._fn_valueclass_error_slots:
            return
        error = self._ensure_fn_err_exit()
        finish = self._fn_err_exit_finish_blocks[function.name]
        slot = self._alloca_in_entry(_CSTR, name=self._fresh("value.error.exception"), init_null=True)
        self._fn_valueclass_error_slots[function.name] = slot
        self._ensure_local_gc_frame_root(self._fresh("value.error.owner"), slot, _CSTR, allow_module=True)
        swap = self.module.globals.get("py_tls_exc_swap_slot")
        if swap is None:
            swap = ir.Function(self.module, ir.FunctionType(ir.VoidType(), [_CSTR]), name="py_tls_exc_swap_slot")
        saved = self.builder._block
        self.builder.position_before(error._instrs[0])
        self.builder.call(swap, [self._as_gc_ptr(slot)])
        self.builder.position_before(finish._instrs[0])
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.call(swap, [self._as_gc_ptr(slot)])
        self.builder.position_at_end(saved)

    def _valueclass_payload_source(self, value):
        record = self._valueclass_payload_source_index.get(id(value))
        if record is not None and record[0] is value:
            return record
        return None

    def _load_valueclass_payload(self, slot, ty, path=(), module_source=False):
        address = slot
        if path:
            indices = [ir.Constant(_I32, 0)]
            indices.extend(ir.Constant(_I32, index) for index in path)
            address = self.builder.gep(slot, indices, inbounds=True, name=self._fresh("value.payload.address"))
        value = self.builder.load(address, name=self._fresh("value.payload.current"))
        record = (value, slot, path, ty, module_source)
        self._valueclass_payload_sources.append(record)
        self._valueclass_payload_source_index[id(value)] = record
        return value

    def _new_owned_valueclass_payload(self, ty, label):
        slot = self._alloca_in_entry(self._valueclass_payload_ir_type(ty), name=self._fresh(label))
        self._ensure_valueclass_payload_gc_roots(self._fresh(label + ".owner"), slot, ty, borrowed=False)
        self._valueclass_payload_temporaries.setdefault(self.current_function.name, []).append((slot, ty))
        # One static producer can execute repeatedly in a comprehension or
        # loop. Its previous independent owner must be gone before reuse.
        self._clear_owned_valueclass_payload(slot)
        return slot

    def _valueclass_payload_owned_roots(self, slot):
        return tuple(record[2] for record in self._fn_valueclass_payload_root_slots.get(
            self.current_function.name, (),
        ) if record[0] is slot and not record[3])

    def _clear_owned_valueclass_payload(self, slot):
        self._release_slot_call_roots(self._valueclass_payload_owned_roots(slot))

    def _copy_valueclass_payload(self, destination, value, ty, module_destination=False):
        """Copy scalars and acquire exactly one owner for each managed leaf."""
        source = self._valueclass_payload_source(value)
        if source is None:
            if self._valueclass_payload_pointer_field_paths(ty):
                raise L1CodegenError("managed valueclass copy requires its producer-owned payload slot")
            self.builder.store(value, destination)
            return
        if source[1] is destination and not source[2]:
            return
        self._copy_valueclass_payload_fields(destination, (), value, ty, module_destination)

    def _copy_valueclass_payload_fields(self, destination, destination_path, value, ty, module_destination=False):
        source = self._valueclass_payload_source(value)
        if source is None:
            raise L1CodegenError("nested valueclass copy requires a payload source")
        for index, (_name, field_ty) in enumerate(ty.fields):
            dst_path = destination_path + (index,)
            src_path = source[2] + (index,)
            if self._is_valueclass_payload_type(field_ty):
                nested = self._load_valueclass_payload(source[1], field_ty, src_path, source[4])
                self._copy_valueclass_payload_fields(destination, dst_path, nested, field_ty, module_destination)
            elif isinstance(self._valueclass_field_payload_ir_type(field_ty), ir.PointerType) and not isinstance(field_ty, RawPointerType):
                src, borrowed = self._slot_call_valueclass_field_source(source[1], src_path, source[4])
                dst = self._module_global_valueclass_payload_field_slot(destination, dst_path, name="value.copy.global") if module_destination else self._slot_call_valueclass_field_source(destination, dst_path, False)[0]
                self.builder.call(self.runtime["pcc_gc_store_root"], [self._as_gc_ptr(dst), ir.Constant(_CSTR, None)])
                self._slot_call_copy_source(dst, src, borrowed)
                if not module_destination:
                    self._slot_call_note_published(dst)
            else:
                src_indices = [ir.Constant(_I32, 0)] + [ir.Constant(_I32, item) for item in src_path]
                dst_indices = [ir.Constant(_I32, 0)] + [ir.Constant(_I32, item) for item in dst_path]
                src = self.builder.gep(source[1], src_indices, inbounds=True, name=self._fresh("value.copy.scalar.source"))
                dst = self.builder.gep(destination, dst_indices, inbounds=True, name=self._fresh("value.copy.scalar.destination"))
                self.builder.store(self.builder.load(src), dst)

    def _emit_owned_valueclass_cleanup(self, skip_payload=None):
        roots = tuple(record[2] for record in self._fn_valueclass_payload_root_slots.get(
            self.current_function.name, (),
        ) if not record[3] and record[0] is not skip_payload)
        self._release_slot_call_roots(roots)

    def _statement_uses_managed_valueclass(self, stmt):
        pending = [stmt]
        while pending:
            node = pending.pop()
            if node is None or isinstance(node, (str, int, float, bool)):
                continue
            if isinstance(node, (tuple, list)):
                pending.extend(node)
                continue
            ty = self._valueclass_payload_expr_type(node)
            if ty is not None and self._valueclass_payload_pointer_field_paths(ty):
                return True
            # Function/class bodies own their statements separately. This
            # walk covers only this statement's evaluated operands/defaults.
            for attr in ("value", "expr", "lhs", "rhs", "left", "right", "operand", "cond",
                         "then_e", "else_e", "obj", "idx", "lo", "hi", "step", "func", "args",
                         "kwargs", "elems", "pairs", "targets", "target", "iter", "exc", "cause",
                         "items", "default", "decorators"):
                child = getattr(node, attr, None)
                if child is not None:
                    pending.append(child)
        return False

    def _emit_valueclass_return(self, value, ty):
        """Secure return leaves before finally and local owner destruction."""
        if not self._valueclass_payload_pointer_field_paths(ty):
            self._emit_pending_finally_blocks()
            if not self._builder_block_is_terminated():
                self._emit_owned_local_cleanup()
                self.builder.ret(value)
            return
        output = self._new_owned_valueclass_payload(ty, "value.return")
        handoff = self._alloca_in_entry(self._valueclass_payload_ir_type(ty), name=self._fresh("value.return.handoff"))
        self._copy_valueclass_payload(output, value, ty)
        self._emit_pending_finally_blocks()
        if self._builder_block_is_terminated():
            return
        self._emit_owned_local_cleanup(skip_payload=output)
        roots = self._valueclass_payload_owned_roots(output)
        paths = self._valueclass_payload_pointer_field_paths(ty)
        pins = []
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_lock"], [])
        for index, root in enumerate(roots):
            current = self.builder.call(self.runtime["pcc_gc_load_ptr"], [ir.Constant(_CSTR, None), self._as_gc_ptr(root)])
            prior = self._extern_prior_pin(current)
            self._gc_pin(current)
            pins.append((root, prior, paths[index]))
        self.builder.call(self.runtime["pcc_py_gc_minor_graph_unlock"], [])
        for root in reversed(roots):
            self._emit_gc_frame_leave_for_slot(root)
        self.builder.store(self.builder.load(output), handoff)
        first_handoff = None
        # Reverse the pin nesting too: two leaves may alias the same object.
        # Each take restores the pin state observed before its matching pin.
        # No operation after the first take can park or invoke Python.
        for root, prior, path in reversed(pins):
            _slot, flag, _lifo = self._slot_call_root_record(root)
            if flag is not None:
                self.builder.store(ir.Constant(_I1, 0), flag)
            current = self.builder.call(self.runtime["pcc_gc_take_pinned_slot"], [self._as_gc_ptr(root), prior],
                                        name=self._fresh("value.return.take"))
            if first_handoff is None:
                first_handoff = self.builder._block._instrs[-1]
            indices = [ir.Constant(_I32, 0)] + [ir.Constant(_I32, index) for index in path]
            field = self.builder.gep(handoff, indices, inbounds=True, name=self._fresh("value.return.field"))
            self.builder.store(current, field)
        self._return_handoff_sites.append((self.current_function, self.builder._block, first_handoff))
        self.builder.ret(self.builder.load(handoff, name=self._fresh("value.return.payload")))

    def _emit_valueclass_payload_expr(self, expr, ty):
        """Select a target-typed payload without re-evaluating its expression."""
        if not self._is_valueclass_payload_type(ty):
            return None
        payload = self._maybe_emit_valueclass_constructor_payload(ty, expr)
        if payload is not None:
            return payload
        from pcc.frontends.python.py_ast import Call, Name

        if isinstance(expr, Name):
            entry = self.env.get(expr.ident)
            global_entry = self._module_globals.get(expr.ident)
            actual = entry[1] if entry is not None else (None if global_entry is None else global_entry[0].value_type)
            if isinstance(actual, ir.LiteralStructType):
                return self._emit_expr(expr)
        direct_call = isinstance(expr, Call) and not self._expr_looks_cpython(expr)
        source = self._new_slot_call_root("value.unbox.source") if direct_call else self._emit_slot_call_operand(expr, "value.unbox.source")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block((source,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            if direct_call:
                if self._expr_returns_unsafe_raw_pointer(expr):
                    raise L1CodegenError("raw pointer cannot become a valueclass object projection")
                self._slot_call_result_sinks.append((expr, source, False))
                try:
                    value = self._emit_expr(expr)
                finally:
                    _expression, _slot, published = self._slot_call_result_sinks.pop()
                if isinstance(value.type, ir.LiteralStructType):
                    record = self._valueclass_payload_source(value)
                    if record is None and self._valueclass_payload_pointer_field_paths(ty):
                        raise L1CodegenError("native valueclass call requires a producer-owned aggregate result")
                    self._release_slot_call_roots((source,))
                    return value if record is None else self._load_valueclass_payload(record[1], ty, record[2], record[4])
                if not published:
                    if not self._owned_release_needed(value, expr):
                        raise L1CodegenError("boxed valueclass call requires an owning output-slot producer")
                    self._publish_slot_call_owned(source, value, label="valueclass call result")
            payload = self._emit_valueclass_payload_from_root(source, ty)
            self._release_slot_call_roots((source,))
            record = self._valueclass_payload_source(payload)
            return self._load_valueclass_payload(record[1], ty, record[2], record[4])
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup

    def _emit_valueclass_payload_from_root(self, source, ty):
        """Project one existing object owner into independently owned leaves."""
        self._slot_call_root_record(source)
        payload_type = self._valueclass_payload_ir_type(ty)
        if payload_type is None:
            raise L1CodegenError("rooted valueclass projection requires a concrete payload type")
        class_name = self._ensure_class_type_registered(ty)
        info = self.class_lowering.classes.get(class_name)
        if info is None:
            raise L1CodegenError("rooted valueclass projection requires its registered class")
        output = self._new_owned_valueclass_payload(ty, "value.unbox.payload")
        output_roots = self._valueclass_payload_owned_roots(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        cls = self._new_slot_call_root("value.unbox.class")
        self._try_err_block = self._slot_call_cleanup_block(output_roots + (cls,), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            self._slot_call_copy_source(cls, info.global_var)
            matches = self._slot_call_runtime_call("py_obj_isinstance", (source, cls))
            good = self.builder.icmp_signed("!=", matches, ir.Constant(_I64, 0))
            ready = self.current_function.append_basic_block(self._fresh("value.unbox.ready"))
            wrong = self.current_function.append_basic_block(self._fresh("value.unbox.wrong.type"))
            self.builder.cbranch(good, ready, wrong)
            self.builder.position_at_end(wrong)
            self._emit_builtin_exception_and_branch("TypeError", "expected " + ty.name + " valueclass instance", None)
            self.builder.position_at_end(ready)
            self._release_slot_call_roots((cls,))
            payload_cleanup = self._slot_call_cleanup_block(output_roots, target)
            self._try_err_block = payload_cleanup
            self._cpy_operand_cleanup_block = payload_cleanup
            from pcc.frontends.python.codegen import marshal

            for index, (_name, field_ty) in enumerate(ty.fields):
                field = self._new_slot_call_root("value.unbox.field")
                field_cleanup = self._slot_call_cleanup_block((field,), payload_cleanup)
                self._try_err_block = field_cleanup
                self._cpy_operand_cleanup_block = field_cleanup
                self._slot_call_runtime_call("py_valuebox_get_field", (source,), result_slot=field,
                                             suffix_args=(ir.Constant(_I32, index),))
                if self._is_valueclass_payload_type(field_ty):
                    nested = self._emit_valueclass_payload_from_root(field, field_ty)
                    self._copy_valueclass_payload_fields(output, (index,), nested, field_ty)
                    nested_source = self._valueclass_payload_source(nested)
                    self._clear_owned_valueclass_payload(nested_source[1])
                elif isinstance(field_ty, RawPointerType):
                    raise L1CodegenError("Python object cannot implicitly become a raw valueclass pointer")
                elif isinstance(self._valueclass_field_payload_ir_type(field_ty), ir.PointerType):
                    destination = self._slot_call_valueclass_field_source(output, (index,), False)[0]
                    status = self.builder.call(self.runtime["pcc_gc_root_move"], [self._as_gc_ptr(destination), self._as_gc_ptr(field)])
                    self._slot_call_check_status(status, "valueclass unbox field move")
                    self._slot_call_note_published(destination)
                else:
                    token = self.builder.call(self.runtime["pcc_gc_foreign_lease_acquire"], [self._as_gc_ptr(field)])
                    self._slot_call_check_status(token, "valueclass scalar projection lease")
                    self._try_err_block = self._slot_call_cleanup_block((), field_cleanup, ((field, token),))
                    self._cpy_operand_cleanup_block = self._try_err_block
                    scalar = marshal.marshal_from_object(self.builder, self.module, self.runtime,
                                                         self.builder.load(field), field_ty)
                    self._emit_post_call_err_check(None)
                    released = self.builder.call(self.runtime["pcc_gc_foreign_lease_release"], [self._as_gc_ptr(field), token])
                    self._try_err_block = field_cleanup
                    self._cpy_operand_cleanup_block = field_cleanup
                    self._slot_call_check_status(released, "valueclass scalar projection release")
                    destination = self.builder.gep(output, [ir.Constant(_I32, 0), ir.Constant(_I32, index)], inbounds=True)
                    self.builder.store(scalar, destination)
                self._release_slot_call_roots((field,))
                self._try_err_block = payload_cleanup
                self._cpy_operand_cleanup_block = payload_cleanup
            return self._load_valueclass_payload(output, ty)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup

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
        result_slot=None,
    ) -> Optional[ir.Value]:
        source = self._valueclass_payload_source(value)
        if source is not None:
            ty = source[3]
        if not self._is_valueclass_payload_type(ty):
            return None
        if isinstance(value.type, ir.PointerType):
            return value
        if source is None:
            if self._valueclass_payload_pointer_field_paths(ty):
                raise L1CodegenError("valueclass boxing requires a producer-owned payload slot")
            slot = self._alloca_in_entry(self._valueclass_payload_ir_type(ty), name=self._fresh("value.scalar.payload"))
            self.builder.store(value, slot)
            source = (value, slot, (), ty, False)
        root = self._emit_slot_call_valueclass_field(
            source[1], source[2], ty, source[4], "value.box", None,
        )
        # consume_fields was a syntax guess about all leaves. Ownership now
        # belongs to actual payload slots and is discharged by their scope.
        if result_slot is not None:
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((root,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                status = self.builder.call(self.runtime["pcc_gc_root_move"], [self._as_gc_ptr(result_slot), self._as_gc_ptr(root)])
                self._slot_call_check_status(status, "valueclass boxed result move")
                self._slot_call_note_published(result_slot)
                self._release_slot_call_roots((root,))
                return self.builder.load(result_slot, name=self._fresh("value.box.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup
        return self._take_slot_call_root(root)

    def _emit_object_to_valueclass_payload(self, value: ir.Value, ty: Type, source_root=None) -> Optional[ir.Value]:
        payload_type = self._valueclass_payload_ir_type(ty)
        if payload_type is None:
            return None
        if not isinstance(value.type, ir.PointerType):
            if str(value.type) != str(payload_type):
                return None
            if self._valueclass_payload_pointer_field_paths(ty) and self._valueclass_payload_source(value) is None:
                raise L1CodegenError("managed valueclass conversion requires its producer-owned payload slot")
            return value
        if source_root is None:
            raise L1CodegenError("boxed valueclass projection requires an authoritative object root before evaluation")
        return self._emit_valueclass_payload_from_root(source_root, ty)

    def _is_scalar(self, ty: Type) -> bool:
        return isinstance(ty, (IntType, FloatType, BoolType, RawPointerType))

    def _is_object(self, ty: Type) -> bool:
        if isinstance(ty, RawPointerType):
            return False
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
            if box_int_params and isinstance(annotation, IntType) and annotation.name == "int":
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
