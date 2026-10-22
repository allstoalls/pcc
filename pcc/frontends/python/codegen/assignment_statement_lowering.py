"""Assignment statement lowering helpers for L1CodeGen."""

from __future__ import annotations

from pcc.frontends.python.codegen.cpy_import_state import (
    live_import_expr_binding,
    live_import_name_slot,
)
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_SET

import sys
import os
from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Assign,
    Attr,
    AugAssign,
    BinOp,
    BoolExpr,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    ComplexType,
    DictExpr,
    DictType,
    DynType,
    Expr,
    ExprStmt,
    FloatType,
    FuncType,
    IfExpr,
    IntType,
    ListExpr,
    ListType,
    MemoryViewType,
    Name,
    RawPointerType,
    NoneType,
    Slice,
    SetType,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.py_ast import assignment_storage_annotation

_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()


def _assign_has_attr(obj, name: str) -> bool:
    return hasattr(obj, name)


def _assign_dataclass_field_names(obj):
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is None:
        return ()
    return fields.keys()


def _assign_expr_type_name(obj) -> str:
    try:
        return str(obj.ty.name)
    except AttributeError:
        return ""


def _assign_type_name(ty) -> str:
    try:
        return str(ty.name)
    except AttributeError:
        return ""


def _assign_is_name(obj) -> bool:
    return isinstance(obj, Name) or _assign_has_attr(obj, "ident")


def _assign_is_tuple_or_list_expr(obj) -> bool:
    if isinstance(obj, (TupleExpr, ListExpr)):
        return True
    ty_name = _assign_expr_type_name(obj)
    return _assign_has_attr(obj, "elems") and ty_name in (
        "tuple",
        "tuple_variadic",
        "list",
    )


def _assign_is_tuple_expr(obj) -> bool:
    if isinstance(obj, TupleExpr):
        return True
    ty_name = _assign_expr_type_name(obj)
    return _assign_has_attr(obj, "elems") and (
        ty_name == "tuple" or ty_name == "tuple_variadic"
    )


def _assign_is_subscript(obj) -> bool:
    return isinstance(obj, Subscript) or (
        _assign_has_attr(obj, "obj") and _assign_has_attr(obj, "idx")
    )


def _assign_is_attr(obj) -> bool:
    return isinstance(obj, Attr) or (
        _assign_has_attr(obj, "obj") and _assign_has_attr(obj, "name")
    )


def _assign_update_dict_literal_pair(
    dict_expr: DictExpr,
    key_expr: StrLit,
    value_expr: Expr,
) -> DictExpr:
    updated_pairs = []
    replaced = False
    for existing_key, existing_value in dict_expr.pairs:
        if isinstance(existing_key, StrLit) and existing_key.value == key_expr.value:
            updated_pairs.append((existing_key, value_expr))
            replaced = True
        else:
            updated_pairs.append((existing_key, existing_value))
    if not replaced:
        updated_pairs.append((key_expr, value_expr))
    return DictExpr(
        span=dict_expr.span,
        ty=dict_expr.ty,
        pairs=tuple(updated_pairs),
    )


def _assign_is_dyn_type(ty) -> bool:
    return isinstance(ty, DynType) or _assign_type_name(ty) == "dyn"


def _assign_is_list_type(ty) -> bool:
    return isinstance(ty, ListType) or (
        _assign_type_name(ty) == "list" and _assign_has_attr(ty, "elem")
    )


def _assign_is_tuple_type(ty) -> bool:
    name = _assign_type_name(ty)
    return isinstance(ty, TupleType) or (
        _assign_has_attr(ty, "elems") and (name == "tuple" or name == "tuple_variadic")
    )


def _assign_tuple_elems(ty) -> tuple:
    if not _assign_is_tuple_type(ty):
        return ()
    try:
        return tuple(ty.elems)
    except AttributeError:
        return ()


def _assign_list_elem(ty):
    try:
        return ty.elem
    except AttributeError:
        return DynType(name="dyn")


def _emit_live_import_augassign(host, stmt, op):
    """Keep the loaded binding alive across RHS callbacks and replacement."""
    code = {"+": 0, "-": 1, "*": 2, "/": 3, "//": 4, "%": 5}.get(op)
    declared = host._module_globals[stmt.target.ident][1]
    set_update = (isinstance(declared, SetType)
                  and isinstance(stmt.value.ty, (SetType, DynType))
                  and op in ("|", "&", "-", "^"))
    if code is None and not set_update:
        if (op == "**" and not isinstance(stmt.target.ty, (FloatType, ComplexType))
                and not isinstance(stmt.value.ty, (FloatType, ComplexType))):
            host._guard_integer_power_operand_provenance(stmt.target, stmt.value)
        combined = BinOp(span=stmt.span, ty=DynType(name="dyn"), op=op,
                         lhs=stmt.target, rhs=stmt.value)
        result = host._emit_slot_call_operand(combined, "import.augassign.result")
        roots = [result]
    else:
        result = host._new_slot_call_root("import.augassign.result")
        roots = [result]
    previous = host._current_try_err_block()
    target = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cpy = host._cpy_operand_cleanup_block
    try:
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
        host._cpy_operand_cleanup_block = host._try_err_block
        if code is not None or set_update:
            for expr in (stmt.target, stmt.value):
                roots.append(host._emit_slot_call_operand(expr, "import.augassign.input"))
                host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
                host._cpy_operand_cleanup_block = host._try_err_block
            if set_update:
                tags = [host._slot_call_runtime_call("py_obj_type_tag", (slot,), span=stmt.span)
                        for slot in roots[1:]]
                valid = host.builder.and_(
                    host.builder.icmp_signed("==", tags[0], ir.Constant(_I64, PY_TYPE_SET)),
                    host.builder.icmp_signed("==", tags[1], ir.Constant(_I64, PY_TYPE_SET)),
                )
                ready = host.current_function.append_basic_block(host._fresh("import.set.ready"))
                bad = host.current_function.append_basic_block(host._fresh("import.set.bad"))
                host.builder.cbranch(valid, ready, bad)
                host.builder.position_at_end(bad)
                host._emit_builtin_exception_and_branch("TypeError", "set binary operator requires set operands", stmt.span)
                host.builder.position_at_end(ready)
                helper = {"|": "py_set_update", "&": "py_set_intersection_update",
                          "-": "py_set_difference_update", "^": "py_set_symmetric_difference_update"}[op]
                host._slot_call_runtime_call(helper, tuple(roots[1:]), span=stmt.span)
                host._slot_call_copy_source(result, roots[1], span=stmt.span)
            else:
                host._slot_call_runtime_call(
                    "py_obj_inplace_op", tuple(roots[1:]), result_slot=result,
                    suffix_args=(ir.Constant(_I64, code),), span=stmt.span,
                )
        host._store_unpack_root_target(stmt.target, result, DynType(name="dyn"))
        host._release_slot_call_roots(tuple(roots))
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy


def _emit_live_import_sequence_unpack(host, stmt, target):
    """Own sequence elements before target evaluation can replace bindings."""
    source = host._emit_slot_call_operand(stmt.value, "import.unpack.source")
    roots = [source]
    previous = host._current_try_err_block()
    error = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cpy = host._cpy_operand_cleanup_block
    try:
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), error)
        host._cpy_operand_cleanup_block = host._try_err_block
        if isinstance(stmt.value.ty, SetType):
            count = host._slot_call_runtime_call("py_obj_len", (source,), span=stmt.span)
            valid = host.builder.icmp_signed("==", count, ir.Constant(_I64, len(target.elems)))
            ready = host.current_function.append_basic_block(host._fresh("import.unpack.set.ready"))
            bad = host.current_function.append_basic_block(host._fresh("import.unpack.set.bad"))
            host.builder.cbranch(valid, ready, bad)
            host.builder.position_at_end(bad)
            host._emit_builtin_exception_and_branch("ValueError", "cannot unpack set: arity mismatch", stmt.span)
            host.builder.position_at_end(ready)
            sequence = host._new_slot_call_root("import.unpack.set.items")
            roots.append(sequence)
            host._try_err_block = host._slot_call_cleanup_block(tuple(roots), error)
            host._cpy_operand_cleanup_block = host._try_err_block
            host._slot_call_runtime_call("py_list_new", (), result_slot=sequence,
                                        suffix_args=(ir.Constant(_I64, 0),), span=stmt.span)
            host._slot_call_runtime_call("py_list_extend", (sequence, source), span=stmt.span)
            source = sequence
        item_start = len(roots)
        for index in range(len(target.elems)):
            item = host._new_slot_call_root("import.unpack.item")
            roots.append(item)
            host._try_err_block = host._slot_call_cleanup_block(tuple(roots), error)
            host._cpy_operand_cleanup_block = host._try_err_block
            host._slot_call_runtime_call(
                "py_obj_getitem_i64", (source,), result_slot=item,
                suffix_args=(ir.Constant(_I64, index),), span=stmt.span,
            )
        element_types = _assign_tuple_elems(stmt.value.ty)
        for index, lhs in enumerate(target.elems):
            element_type = element_types[index] if len(element_types) == len(target.elems) else DynType(name="dyn")
            host._store_unpack_root_target(lhs, roots[item_start + index], element_type)
        host._release_slot_call_roots(tuple(roots))
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy


class AssignmentStatementLoweringMixin:
    def _literal_self_method_dispatch_entries(self, dict_expr: DictExpr):
        if not isinstance(dict_expr, DictExpr) or not dict_expr.pairs:
            return None
        receiver_class = self._self_receiver_class_name()
        current_class = getattr(self, "current_class", None)
        if receiver_class is None and current_class is not None:
            receiver_class = current_class.name
        if receiver_class is None or "self" not in self.env:
            return None

        entries = []
        seen_keys = set()
        for key_expr, value_expr in dict_expr.pairs:
            if not isinstance(key_expr, StrLit):
                return None
            if key_expr.value in seen_keys:
                return None
            seen_keys.add(key_expr.value)
            if not isinstance(value_expr, Attr):
                return None
            if not isinstance(value_expr.obj, Name) or value_expr.obj.ident != "self":
                return None
            method_info = self._resolve_method_mro(receiver_class, value_expr.name)
            if method_info is None or value_expr.name not in method_info.methods:
                return None
            if method_info.method_kinds.get(value_expr.name, "instance") != "instance":
                return None
            entries.append(
                (
                    key_expr.value,
                    method_info,
                    method_info.methods[value_expr.name],
                    value_expr.name,
                )
            )
        return tuple(entries)

    def _literal_dispatch_name_uses_are_call_only(self, name: str) -> bool:
        current_func = getattr(self, "current_func_def", None)
        body = getattr(current_func, "body", None)
        if body is None:
            return False

        seen_dispatch_call = False
        saw_bad_use = False

        def visit(node) -> None:
            nonlocal seen_dispatch_call, saw_bad_use
            if saw_bad_use or node is None:
                return
            if isinstance(node, Name):
                if node.ident == name:
                    saw_bad_use = True
                return
            if isinstance(node, Call):
                func = node.func
                if (
                    isinstance(func, Subscript)
                    and isinstance(func.obj, Name)
                    and func.obj.ident == name
                ):
                    seen_dispatch_call = True
                    visit(func.idx)
                    for arg in node.args:
                        visit(arg)
                    for _kw_name, kw_expr in node.kwargs:
                        visit(kw_expr)
                    return
            if isinstance(node, Assign):
                for target in node.targets:
                    if not (isinstance(target, Name) and target.ident == name):
                        visit(target)
                visit(node.value)
                return
            if isinstance(node, AugAssign):
                visit(node.target)
                visit(node.value)
                return
            if isinstance(node, (tuple, list)):
                for item in node:
                    visit(item)
                return
            for field_name in _assign_dataclass_field_names(node):
                if field_name in ("span", "ty", "annotation"):
                    continue
                visit(getattr(node, field_name))

        visit(body)
        return seen_dispatch_call and not saw_bad_use

    def _maybe_emit_virtual_literal_dispatch_assign(
        self,
        target: Name,
        value_expr: Expr,
        target_ty,
    ) -> bool:
        if not isinstance(value_expr, DictExpr):
            return False
        if self.env.get(target.ident) is not None:
            return False
        if self._literal_self_method_dispatch_entries(value_expr) is None:
            return False
        if not self._literal_dispatch_name_uses_are_call_only(target.ident):
            return False
        alloca = self._alloca_in_entry(
            _CSTR,
            name=f"{target.ident}.addr",
            init_null=True,
        )
        self.env[target.ident] = (alloca, _CSTR, target_ty)
        self.builder.store(ir.Constant(_CSTR, None), alloca)
        virtual = getattr(self, "_virtual_literal_dict_expr_bindings", None)
        if virtual is None:
            self._virtual_literal_dict_expr_bindings = set()
            virtual = self._virtual_literal_dict_expr_bindings
        virtual.add(target.ident)
        self._cpy_env_flags.pop(target.ident, None)
        self._weak_dict_env_flags.pop(target.ident, None)
        self._weakref_env_flags.pop(target.ident, None)
        self._owned_local_names.discard(target.ident)
        self._owned_local_has_value.discard(target.ident)
        return True

    def _maybe_emit_valueclass_constructor_payload(
        self,
        target_ty,
        value_expr,
    ):
        if not self._is_valueclass_payload_type(target_ty):
            return None
        if not isinstance(value_expr, Call):
            return None
        if not isinstance(value_expr.func, Name):
            return None
        if not hasattr(self, "class_lowering"):
            return None
        class_name = self._resolve_class_alias(value_expr.func.ident)
        info = self.class_lowering.classes.get(class_name)
        if info is None or not bool(getattr(info, "valueclass", False)):
            return None
        payload_ty = self._valueclass_payload_ir_type(target_ty)
        if payload_ty is None:
            return None

        resolved_args = value_expr.args
        if value_expr.kwargs:
            init_fd = self.class_lowering._find_method_def(class_name, "__init__")
            if init_fd is None:
                mro_info = self._resolve_method_mro(class_name, "__init__")
                if mro_info is not None:
                    init_fd = self.class_lowering._find_method_def(
                        mro_info.name,
                        "__init__",
                    )
            if init_fd is None:
                return None
            resolved_args = tuple(
                self._resolve_call_kwargs(
                    value_expr.args,
                    value_expr.kwargs,
                    init_fd.args,
                    skip_self=True,
                )
            )

        fields = tuple(getattr(target_ty, "fields", ()))
        if len(resolved_args) != len(fields):
            return None

        payload_slot = self._new_owned_valueclass_payload(target_ty, "value." + class_name + ".constructor")
        roots = self._valueclass_payload_owned_roots(payload_slot)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        self._try_err_block = self._slot_call_cleanup_block(roots, target)
        self._cpy_operand_cleanup_block = self._try_err_block
        try:
            zero = ir.Constant(ir.IntType(32), 0)
            for index, ((_field_name, field_ty), arg_expr) in enumerate(zip(fields, resolved_args)):
                field_ptr = self.builder.gep(payload_slot, [zero, ir.Constant(ir.IntType(32), index)],
                                             inbounds=True, name=self._fresh("value.constructor.field"))
                if self._is_valueclass_payload_type(field_ty):
                    nested = self._emit_valueclass_payload_expr(arg_expr, field_ty)
                    self._copy_valueclass_payload_fields(payload_slot, (index,), nested, field_ty)
                elif isinstance(field_ty, IntType):
                    self.builder.store(self._emit_expr_as_i64(arg_expr), field_ptr)
                elif isinstance(field_ty, FloatType):
                    raw = self._emit_expr(arg_expr)
                    self.builder.store(self._to_double(raw, arg_expr.ty), field_ptr)
                    self._gc_release_if_owned(raw, arg_expr)
                elif isinstance(field_ty, BoolType):
                    raw = self._emit_expr(arg_expr)
                    self.builder.store(self._truthy(raw, arg_expr.ty), field_ptr)
                    self._gc_release_if_owned(raw, arg_expr)
                elif isinstance(field_ty, RawPointerType):
                    raw = self._emit_expr(arg_expr)
                    self.builder.store(self._coerce(raw, arg_expr.ty, field_ty, arg_expr), field_ptr)
                else:
                    source = self._emit_slot_call_operand(arg_expr, "value.constructor.field")
                    destination = self._slot_call_valueclass_field_source(payload_slot, (index,), False)[0]
                    field_error = self._try_err_block
                    field_cleanup = self._cpy_operand_cleanup_block
                    self._try_err_block = self._slot_call_cleanup_block((source,), field_error)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    status = self.builder.call(self.runtime["pcc_gc_root_move"], [self._as_gc_ptr(destination), self._as_gc_ptr(source)])
                    self._slot_call_check_status(status, "valueclass constructor field move", arg_expr.span)
                    self._slot_call_note_published(destination)
                    self._release_slot_call_roots((source,))
                    self._try_err_block = field_error
                    self._cpy_operand_cleanup_block = field_cleanup
            return self._load_valueclass_payload(payload_slot, target_ty)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup

    def _emit_assign(self, stmt: Assign) -> None:
        from pcc.frontends.python.codegen.local_bound_lowering import mark_bound_target
        if not stmt.has_value:
            # ``receiver.attr: T`` and ``receiver[key]: T`` evaluate the
            # receiver/key, but never load or store the annotated location.
            for target in stmt.targets:
                if isinstance(target, (Attr, Subscript)):
                    self._emit_expr_stmt(ExprStmt(stmt.span, target.obj))
                if isinstance(target, Subscript):
                    if isinstance(target.idx, Slice):
                        for part in (target.idx.lo, target.idx.hi, target.idx.step):
                            if part is not None:
                                self._emit_expr_stmt(ExprStmt(stmt.span, part))
                    else:
                        self._emit_expr_stmt(ExprStmt(stmt.span, target.idx))
            return
        unpack_name = ""
        if (
            _assign_is_name(stmt.value)
            and stmt.value.ident in self._for_unpack_temporary_names
            and stmt.value.ident in getattr(self, "_owned_local_names", set())
        ):
            unpack_name = stmt.value.ident
        if unpack_name:
            self._emit_for_unpack_assign(stmt, unpack_name)
        else:
            self._emit_assign_unchecked(stmt)
        for target in stmt.targets:
            mark_bound_target(self, target)

    def _emit_for_unpack_assign(self, stmt: Assign, name: str) -> None:
        """End a normalized loop item's ownership after its unpack.

        Keep the registered slot and flag for the next iteration. Both a
        successful unpack and an exception caught outside the loop must drop
        this compiler-only reference before user code continues.
        """
        slot = self.env[name]
        outer_err = getattr(self, "_try_err_block", None)
        error_bb = self.current_function.append_basic_block(
            name=self._fresh("for.unpack.error")
        )
        done_bb = self.current_function.append_basic_block(
            name=self._fresh("for.unpack.done")
        )
        self._try_err_block = error_bb
        self._emit_assign_unchecked(stmt)
        self._try_err_block = outer_err
        if not self._builder_block_is_terminated():
            self._emit_release_owned_local_if_flagged(name, slot[0])
            self.builder.branch(done_bb)
        self.builder.position_at_end(error_bb)
        # Disposing the tuple can invoke a weakref callback or finalizer,
        # whose unraisable boundary clears TLS. Keep the selecting exception
        # in the existing rooted owner protocol while that cleanup runs.
        pending = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("for.unpack.exception"),
        )
        exception_slot = self._exception_selection_owner_slot(
            pending, "for.unpack.exception"
        )
        self.builder.call(self.runtime["py_clear_exception"], [])
        self._emit_release_owned_local_if_flagged(name, slot[0])
        self.builder.call(self.runtime["py_clear_exception"], [])
        pending = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(exception_slot)],
            name=self._fresh("for.unpack.exception.restore"),
        )
        self.builder.call(self.runtime["py_raise"], [pending])
        self._clear_exception_selection_owner_slot(exception_slot)
        self.builder.branch(
            outer_err if outer_err is not None else self._ensure_fn_err_exit()
        )
        self.builder.position_at_end(done_bb)

    def _emit_assign_unchecked(self, stmt: Assign) -> None:
        if len(stmt.targets) != 1:
            raise NotImplementedError(
                "Layer 1 does not handle tuple-unpacking assignment"
            )
        target = stmt.targets[0]

        if len(self._generator_ctx_stack) > 0:
            sentinel = self._yield_sentinel_call(stmt.value)
            if sentinel is not None:
                kind, call = sentinel
                if kind != "yield":
                    raise NotImplementedError(
                        "assignment from yield from is not implemented yet"
                    )
                sent = self._emit_generator_yield_expr(call)
                self._store_unpack_target(target, sent, DynType(name="dyn"))
                return

        # Tuple-unpacking assignment: ``a, b = x, y`` where the RHS is a
        # matching TupleExpr literal. Lower to a sequence of plain
        # assignments; Python semantics require that the whole RHS be
        # evaluated before any LHS is bound, which we mimic by emitting
        # every RHS into an SSA value first and only then storing.
        if _assign_is_tuple_or_list_expr(target):
            return self._emit_tuple_unpack_assign(stmt, target)

        # Subscript target: ``lst[i] = v`` / ``d[k] = v``.
        if _assign_is_subscript(target):
            # ``os.environ[key] = value`` store hook (native_os.py):
            # CPython mapping semantics via py_os_environ_setitem.
            if self._emit_native_os_environ_setitem_store(target, stmt.value):
                return
            if isinstance(target.obj, Name):
                tracked_dict = self._literal_dict_expr_bindings.get(target.obj.ident)
                if tracked_dict is not None and isinstance(target.idx, StrLit):
                    updated_dict = _assign_update_dict_literal_pair(
                        tracked_dict,
                        target.idx,
                        stmt.value,
                    )
                    for tracked_name, tracked_value in tuple(
                        self._literal_dict_expr_bindings.items()
                    ):
                        if tracked_value == tracked_dict:
                            self._literal_dict_expr_bindings[tracked_name] = (
                                updated_dict
                            )
                elif target.obj.ident in self._literal_dict_expr_bindings:
                    self._literal_dict_expr_bindings = {}
                getattr(self, "_virtual_literal_dict_expr_bindings", set()).discard(
                    target.obj.ident
                )
            self._emit_subscript_store(target, stmt.value)
            return

        # Attribute target: currently only ``self.<attr> = value`` inside
        # a method body. Delegates to the class lowering helper which
        # uses the per-class field layout when known and falls back to
        # ``py_obj_setattr`` otherwise.
        if _assign_is_attr(target):
            self._emit_attr_store(target, stmt.value)
            return

        if not _assign_is_name(target):
            raise NotImplementedError(
                f"Layer 1/2 assignment target must be Name or Subscript; got "
                f"{type(target).__name__}"
            )
        if not hasattr(self, "_inspect_signature_aliases"):
            self._inspect_signature_aliases = {}
        if not hasattr(self, "_inspect_fullargspec_aliases"):
            self._inspect_fullargspec_aliases = {}
        self._inspect_signature_aliases.pop(target.ident, None)
        self._inspect_fullargspec_aliases.pop(target.ident, None)
        literal_dict_expr = None
        if isinstance(stmt.value, DictExpr):
            literal_dict_expr = stmt.value
        elif isinstance(stmt.value, Name):
            literal_dict_expr = self._literal_dict_expr_bindings.get(stmt.value.ident)
        self._literal_dict_expr_bindings.pop(target.ident, None)
        getattr(self, "_virtual_literal_dict_expr_bindings", set()).discard(
            target.ident
        )
        if literal_dict_expr is not None:
            self._literal_dict_expr_bindings[target.ident] = literal_dict_expr

        if self._maybe_emit_discard_assignment(target, stmt.value):
            return

        # ``my_fn = extern("symbol", ...)`` — pcc.extern scaffold
        # declaration. No runtime IR emitted; just record the decl.
        if self._maybe_register_extern_assign(stmt):
            return

        typevar_name = self._typing_typevar_name(stmt.value)
        if typevar_name is not None:
            target_ident = target.ident
            self._typing_typevar_aliases[target_ident] = typevar_name
            self._native_builtin_value_aliases.pop(target_ident, None)
            self._native_module_object_aliases.pop(target_ident, None)
            self.env.pop(target_ident, None)
            self.env_class_hint.pop(target_ident, None)
            self.env_class_object_hint.pop(target_ident, None)
            self._cpy_env_flags.pop(target_ident, None)
            self._weak_dict_env_flags.pop(target_ident, None)
            self._weakref_env_flags.pop(target_ident, None)
            return

        optional_arg_name = self._typing_optional_arg_name(stmt.value)
        if optional_arg_name is not None:
            target_ident = target.ident
            self._typing_optional_aliases[target_ident] = optional_arg_name
            self._native_builtin_value_aliases.pop(target_ident, None)
            self._native_module_object_aliases.pop(target_ident, None)
            self.env.pop(target_ident, None)
            self.env_class_hint.pop(target_ident, None)
            self.env_class_object_hint.pop(target_ident, None)
            self._cpy_env_flags.pop(target_ident, None)
            self._weak_dict_env_flags.pop(target_ident, None)
            self._weakref_env_flags.pop(target_ident, None)
            return

        if self._typing_type_alias_annotation(
            stmt.annotation
        ) or self._typing_metadata_alias_expr(stmt.value):
            target_ident = target.ident
            self._typing_metadata_aliases.add(target_ident)
            self._native_builtin_value_aliases.pop(target_ident, None)
            self._native_module_object_aliases.pop(target_ident, None)
            self.env.pop(target_ident, None)
            self.env_class_hint.pop(target_ident, None)
            self.env_class_object_hint.pop(target_ident, None)
            self._cpy_env_flags.pop(target_ident, None)
            self._weak_dict_env_flags.pop(target_ident, None)
            self._weakref_env_flags.pop(target_ident, None)
            return

        imported_native_module = self._native_literal_dunder_import_module(
            stmt.value
        )
        if imported_native_module is not None:
            if self._is_native_builtin_dynamic_module(imported_native_module):
                self._register_native_builtin_module_alias(
                    target.ident,
                    imported_native_module,
                )
                self._clear_native_module_object_alias(target.ident)
            else:
                self._register_native_module_object_alias(
                    target.ident,
                    imported_native_module,
                )
                self._clear_native_builtin_module_alias(target.ident)
            self._clear_native_builtin_value_alias(target.ident)
            self.env.pop(target.ident, None)
            self.env_class_hint.pop(target.ident, None)
            self.env_class_object_hint.pop(target.ident, None)
            if hasattr(self, "_cpy_env_flags"):
                self._cpy_env_flags.pop(target.ident, None)
            self._weak_dict_env_flags.pop(target.ident, None)
            self._weakref_env_flags.pop(target.ident, None)
            return
        self._clear_native_module_object_alias(target.ident)
        self._clear_native_builtin_module_alias(target.ident)

        builtin_alias_kind = self._native_builtin_value_kind_for_expr(stmt.value)
        if builtin_alias_kind is not None:
            self._register_native_builtin_value_alias(
                target.ident,
                builtin_alias_kind,
            )
            self.env.pop(target.ident, None)
            self.env_class_hint.pop(target.ident, None)
            self.env_class_object_hint.pop(target.ident, None)
            if hasattr(self, "_cpy_env_flags"):
                self._cpy_env_flags.pop(target.ident, None)
            self._weak_dict_env_flags.pop(target.ident, None)
            self._weakref_env_flags.pop(target.ident, None)
            module_global_alias = target.ident in self._module_globals and (
                self.current_func_def is None
                or target.ident in self._current_global_names
            )
            if not module_global_alias:
                return
            # A module-level alias is both useful compiler metadata and a real
            # Python binding.  Falling out of this branch materializes and
            # publishes the builtin object below.  Returning here left the
            # corresponding ``.modvar`` null, so a sibling's
            # ``from mod import alias`` raised AttributeError even though the
            # assignment had executed (for example ``binary_type = bytes``).
        else:
            self._clear_native_builtin_value_alias(target.ident)

        if getattr(self, "current_func_def", None) is None:
            static_re_flags = self._native_re_static_flags_value(stmt.value)
            re_flag_aliases = getattr(self, "_native_re_static_flag_aliases", None)
            if re_flag_aliases is None:
                self._native_re_static_flag_aliases = {}
                re_flag_aliases = self._native_re_static_flag_aliases
            if static_re_flags is None:
                re_flag_aliases.pop(target.ident, None)
            else:
                re_flag_aliases[target.ident] = static_re_flags

        re_compile_alias = self._native_re_compile_alias_info(stmt.value)
        current_func = getattr(self, "current_func_def", None)
        re_scope_body = (
            None if current_func is None else getattr(current_func, "body", None)
        )
        if (
            re_compile_alias is not None
            and (re_compile_alias[1] & ~26) == 0
            and (self._re_engine_subset_supported(re_compile_alias[0]))
        ):
            # Engine-subset flags==0 patterns get a REAL pattern object from
            # the re.compile expression lowering (py_re_compile_obj), so the
            # variable must be a normal assignment: skipping emission here
            # left the modvar null for any use the compile-time rewriting
            # did not intercept (e.g. method calls lowered inside function
            # bodies), which surfaced as AttributeError at runtime.
            re_compile_alias = None
        if re_compile_alias is not None and (
            current_func is None or re_scope_body is not None
        ):
            if self._native_re_compile_alias_uses_are_safe(
                target.ident,
                stmt,
                re_scope_body,
            ):
                if current_func is None:
                    re_aliases = getattr(self, "_native_re_compile_aliases", None)
                    if re_aliases is None:
                        self._native_re_compile_aliases = {}
                        re_aliases = self._native_re_compile_aliases
                    re_aliases[target.ident] = re_compile_alias
                else:
                    re_aliases = getattr(
                        self,
                        "_native_re_compile_local_aliases",
                        None,
                    )
                    if re_aliases is None:
                        self._native_re_compile_local_aliases = {}
                        re_aliases = self._native_re_compile_local_aliases
                    re_aliases[(id(current_func), target.ident)] = re_compile_alias
                self.env.pop(target.ident, None)
                self.env_class_hint.pop(target.ident, None)
                self.env_class_object_hint.pop(target.ident, None)
                if hasattr(self, "_cpy_env_flags"):
                    self._cpy_env_flags.pop(target.ident, None)
                self._weak_dict_env_flags.pop(target.ident, None)
                self._weakref_env_flags.pop(target.ident, None)
                return
        if current_func is None:
            getattr(self, "_native_re_compile_aliases", {}).pop(target.ident, None)
        else:
            re_aliases = getattr(self, "_native_re_compile_local_aliases", None)
            if re_aliases is not None:
                re_aliases[(id(current_func), target.ident)] = None

        if not hasattr(self, "_ir_builder_env_flags"):
            self._ir_builder_env_flags = {}
        if isinstance(stmt.value, Name):
            kind = self._scaffold_local_kind(stmt.value.ident)
        elif isinstance(stmt.value, Attr):
            kind = self._scaffold_field_receiver_kind(stmt.value)
        else:
            kind = self._scaffold_result_class_for_call(stmt.value)
        if kind:
            self._ir_builder_env_flags[target.ident] = kind
        else:
            self._ir_builder_env_flags.pop(target.ident, None)

        # Track class hint for ``p = MyClass(args)`` so that ``p.method()``
        # can dispatch to ``MyClass``'s method even when type inference
        # labels ``p`` as ``DynType``.
        if isinstance(stmt.value, Call) and isinstance(stmt.value.func, Name):
            callee = stmt.value.func.ident
            if (
                hasattr(self, "class_lowering")
                and callee in self.class_lowering.classes
            ):
                self.env_class_hint[target.ident] = callee
            else:
                self.env_class_hint.pop(target.ident, None)
        elif isinstance(target.ty, ClassType):
            hint = self._ensure_class_type_registered(target.ty)
            if hint is not None:
                self.env_class_hint[target.ident] = hint
            else:
                self.env_class_hint.pop(target.ident, None)
        else:
            # Any other RHS invalidates the class hint.
            self.env_class_hint.pop(target.ident, None)
        class_object_hint = None
        if hasattr(self, "class_lowering"):
            class_object_hint = self._class_object_hint_for_expr(stmt.value)
        if class_object_hint is not None:
            self.env_class_object_hint[target.ident] = class_object_hint
        else:
            self.env_class_object_hint.pop(target.ident, None)
        list_elem_hint = self._list_elem_class_hint_for_expr(stmt.value)
        if list_elem_hint is not None:
            self.env_list_elem_class_hint[target.ident] = list_elem_hint
        else:
            self.env_list_elem_class_hint.pop(target.ident, None)
        target_ty_for_hints = (
            assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value)
            if stmt.annotation is not None else target.ty
        )
        threading_elem_kind = self._threading_list_elem_kind_for_type(
            target_ty_for_hints
        ) or self._threading_list_elem_kind_for_expr(stmt.value)
        if threading_elem_kind is not None:
            self._threading_list_elem_flags[target.ident] = threading_elem_kind
        else:
            self._threading_list_elem_flags.pop(target.ident, None)
        threading_kind = self._threading_constructor_kind_for_expr(stmt.value)
        if threading_kind is not None:
            self._threading_env_flags[target.ident] = threading_kind
        else:
            self._threading_env_flags.pop(target.ident, None)
        weak_dict_kind = self._weak_dict_constructor_kind_for_expr(stmt.value)
        if weak_dict_kind is not None:
            self._weak_dict_env_flags[target.ident] = weak_dict_kind
        else:
            self._weak_dict_env_flags.pop(target.ident, None)
        weakref_kind = self._weakref_constructor_kind_for_expr(stmt.value)
        if weakref_kind is not None:
            self._weakref_env_flags[target.ident] = True
        else:
            self._weakref_env_flags.pop(target.ident, None)
        inspect_signature = self._inspect_signature_metadata_for_call(stmt.value)
        if inspect_signature is not None:
            self._inspect_signature_aliases[target.ident] = inspect_signature
        inspect_fullargspec = self._inspect_fullargspec_metadata_for_call(stmt.value)
        if inspect_fullargspec is not None:
            self._inspect_fullargspec_aliases[target.ident] = inspect_fullargspec

        target_ty = assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value) if stmt.annotation is not None else target.ty
        # A later assignment can lose its concrete inferred type while the
        # binding still uses a valueclass aggregate. Select that actual slot's
        # projection before any RHS preparation, including scalar boxing, so
        # the producer is evaluated once into the required rooted boundary.
        global_slot = self._module_globals.get(target.ident)
        if global_slot is not None and (
            self.current_func_def is None
            or target.ident in self._current_global_names
        ):
            if (isinstance(global_slot[0].value_type, ir.LiteralStructType)
                    and self._is_valueclass_payload_type(global_slot[1])):
                target_ty = global_slot[1]
        else:
            existing_slot = self.env.get(target.ident)
            if (existing_slot is not None
                    and isinstance(existing_slot[1], ir.LiteralStructType)
                    and self._is_valueclass_payload_type(existing_slot[2])):
                target_ty = existing_slot[2]
        if self._maybe_emit_virtual_literal_dispatch_assign(
            target,
            stmt.value,
            target_ty,
        ):
            return
        forced_exact_int_target = isinstance(target_ty, IntType) and bool(
            getattr(self, "_exact_int_env_flags", {}).get(target.ident, False)
            or target.ident
            in getattr(self, "_planned_exact_int_local_names", set())
        )
        boxed_int_target = isinstance(target_ty, IntType) and (
            self._int_exprs_are_boxed() or forced_exact_int_target
        )
        exact_int_value = None
        if boxed_int_target and isinstance(stmt.value.ty, (IntType, BoolType)):
            exact_int_value = self._emit_exact_int_operand_object(stmt.value)
        elif boxed_int_target and self._is_walrus_sentinel(stmt.value):
            # Chained assignment is lifted as ``outer = _walrus(inner, rhs)``
            # and the sentinel itself is Dyn-typed.  Its real RHS still needs
            # the exact projection, while evaluating the sentinel performs
            # the hidden target stores exactly once.
            exact_int_value = self._emit_walrus(stmt.value)
        elif isinstance(
            target_ty, IntType
        ) and self._int_expr_needs_exact_object_boundary(stmt.value):
            exact_int_value = self._maybe_emit_exact_int_object(stmt.value)
        valueclass_target_ty = target_ty
        if not self._is_valueclass_payload_type(
            valueclass_target_ty
        ) and self._is_valueclass_payload_type(stmt.value.ty):
            valueclass_target_ty = stmt.value.ty
        valueclass_payload = self._emit_valueclass_payload_expr(stmt.value, valueclass_target_ty)
        if valueclass_payload is not None:
            value = valueclass_payload
            local_target_ty = valueclass_target_ty
        elif exact_int_value is not None:
            value = exact_int_value
            local_target_ty = target_ty
        elif isinstance(target_ty, FuncType) and isinstance(stmt.value.ty, FuncType):
            value = self._emit_expr_with_native_callable_values(stmt.value)
            local_target_ty = target_ty
        elif (
            self._is_object(target_ty)
            and isinstance(stmt.value, IfExpr)
            and self._is_valueclass_payload_type(stmt.value.ty)
        ):
            value = self._emit_expr_as_pcc_object(stmt.value)
            local_target_ty = target_ty
        elif self._is_object(target_ty) and isinstance(stmt.value, BoolExpr):
            value = self._emit_expr_as_pcc_object(stmt.value)
            local_target_ty = target_ty
        elif self._is_object(target_ty) and isinstance(stmt.value.ty, IntType):
            # Select the Python integer object before scalar lowering can
            # discard an out-of-lane value (notably int(object) / int(str)).
            # Keep the ordinary object-local ownership path: the producer's
            # ledger distinguishes new results from borrowed integer objects.
            value = self._emit_expr_as_pcc_object(stmt.value)
            local_target_ty = target_ty
        else:
            value = self._emit_expr(stmt.value)
            local_target_ty = target_ty

        rhs_native_file_is_owned = value in self._native_file_values
        if rhs_native_file_is_owned:
            self._native_file_env_flags[target.ident] = True
        else:
            self._native_file_env_flags.pop(target.ident, None)
        if value in getattr(self, "_native_fileinput_values", ()):
            self._native_fileinput_env_flags[target.ident] = True
        else:
            self._native_fileinput_env_flags.pop(target.ident, None)

        # Track "this local holds a CPython PyObject*" so subsequent
        # loads of the variable keep the tag, letting _to_int64 /
        # print / compare dispatch via the libpython helpers.
        if not hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags = {}
        if value in getattr(self, "_cpy_values", ()):
            self._cpy_env_flags[target.ident] = True
        else:
            self._cpy_env_flags.pop(target.ident, None)
        if not hasattr(self, "_exact_int_env_flags"):
            self._exact_int_env_flags = {}

        # If this is a module-level global (seeded in the first pass),
        # write into the module variable and skip the local alloca
        # path. Guard on being inside the synthetic ``main`` body —
        # user-defined functions may still shadow with a local of the
        # same name, which is what the env fallback below handles.
        module_globals = self._module_globals
        if (
            self.current_func_def is not None
            and target.ident in self._current_global_names
        ):
            target_ty = assignment_storage_annotation(stmt.annotation, stmt.value, stmt.has_value) if stmt.annotation is not None else target.ty
            self._ensure_module_global_name(target.ident, target_ty)
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
                        stmt.value.ty,
                    )
            else:
                value = self._coerce(value, stmt.value.ty, declared_ty, stmt.value)
            if value in getattr(self, "_cpy_values", ()):
                self._cpy_module_flags[target.ident] = True
                is_cpy_value = True
            else:
                self._cpy_module_flags.pop(target.ident, None)
                is_cpy_value = False
            value_is_owned = exact_int_value is not None or self._owned_release_needed(
                value,
                stmt.value,
            )
            self._store_module_global_root_value(
                gv,
                value,
                declared_ty=declared_ty,
                value_is_owned=False,
                is_cpy_value=is_cpy_value,
                raw_pointer=self._expr_returns_unsafe_raw_pointer(stmt.value),
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
            # First assignment — allocate.
            if not (
                self._is_scalar(local_target_ty)
                or self._is_object(local_target_ty)
                or self._is_valueclass_payload_type(local_target_ty)
            ):
                raise NotImplementedError(
                    f"Layer 1/2 cannot allocate variable "
                    f"{target.ident!r} of type {type(local_target_ty).__name__}"
                )
            ir_ty = (
                _CSTR
                if (boxed_int_target or exact_int_value is not None)
                else self._local_slot_ir_type(target.ident, local_target_ty)
            )
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
                self._local_slot_decl_type(target.ident, local_target_ty),
            )
            slot = self.env[target.ident]

        alloca, ir_ty, declared_ty = slot
        target_storage_ty = (
            _CSTR
            if (boxed_int_target or exact_int_value is not None)
            else self._local_slot_ir_type(target.ident, local_target_ty)
        )
        if (
            forced_exact_int_target
            and isinstance(ir_ty, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
        ):
            # A planned local can temporarily carry a Dyn/object for-target
            # binding and later return to its exact-int projection.  Keep the
            # entry-allocated pointer slot, but restore the semantic type now;
            # otherwise the assignment stores a valid PyInt* while subsequent
            # Name loads still follow the Dyn path and the exact-int flag that
            # the loop cleared is never re-established.
            declared_ty = local_target_ty
            self.env[target.ident] = (alloca, ir_ty, declared_ty)
        if (
            not (boxed_int_target or exact_int_value is not None)
            and self._ir_type_matches(ir_ty, target_storage_ty)
            and _assign_type_name(declared_ty) != _assign_type_name(local_target_ty)
        ):
            # Python locals are rebindable. When the storage ABI stays the
            # same PyObject* shape, keep the existing alloca but update the
            # codegen type so later loads/augassigns use the current inferred
            # type instead of the first assignment's type. A planned mixed
            # local keeps its authoritative object-storage semantic type.
            declared_ty = self._local_slot_decl_type(target.ident, local_target_ty)
            self.env[target.ident] = (alloca, ir_ty, declared_ty)
        if (
            (boxed_int_target or exact_int_value is not None)
            and not self._ir_type_matches(ir_ty, _CSTR)
            and exact_int_value is not None
        ):
            declared_ty = local_target_ty
            alloca = self._alloca_in_entry(
                _CSTR,
                name=f"{target.ident}.obj.addr",
                init_null=True,
            )
            self.env[target.ident] = (alloca, _CSTR, declared_ty)
            ir_ty = _CSTR
        if isinstance(declared_ty, IntType) and isinstance(ir_ty, ir.PointerType):
            if not isinstance(value.type, ir.PointerType):
                value = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    value,
                    stmt.value.ty,
                )
            self._exact_int_env_flags[target.ident] = True
        elif (
            target.ident in getattr(self, "_planned_object_local_names", set())
            and isinstance(ir_ty, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
        ):
            self._exact_int_env_flags.pop(target.ident, None)
            if not isinstance(value.type, ir.PointerType):
                # The planner selected an object slot, so scalar boxing is a
                # real object producer even though the RHS still has its
                # float/bool semantic type. Publish that owner before old-local
                # cleanup can collect, then move it into the registered local.
                output = self._new_slot_call_root(target.ident + ".scalar.box")
                previous = self._current_try_err_block()
                error = previous if previous is not None else self._ensure_fn_err_exit()
                saved_cpy = self._cpy_operand_cleanup_block
                self._try_err_block = self._slot_call_cleanup_block((output,), error)
                self._cpy_operand_cleanup_block = self._try_err_block
                try:
                    value = marshal.marshal_to_object(
                        self.builder, self.module, self.runtime, value, stmt.value.ty,
                    )
                    self._publish_slot_call_owned(output, value, label="local scalar box")
                    # A successful lease does not establish that allocation
                    # succeeded. Preserve the previous binding on boxing error.
                    self._emit_post_call_err_check(stmt.span)
                    current = self.builder.load(output, name=self._fresh("local.scalar.current"))
                    failed = self.builder.icmp_unsigned("==", current, ir.Constant(_CSTR, None))
                    error_bb = self.current_function.append_basic_block(self._fresh("local.scalar.error"))
                    ready_bb = self.current_function.append_basic_block(self._fresh("local.scalar.ready"))
                    self.builder.cbranch(failed, error_bb, ready_bb)
                    self.builder.position_at_end(error_bb)
                    self._emit_builtin_exception_and_branch(
                        "MemoryError", "out of memory boxing local scalar", stmt.span,
                    )
                    self.builder.position_at_end(ready_bb)
                    self._ensure_owned_local_gc_root(target.ident, alloca, ir_ty)
                    self._emit_release_owned_local_if_flagged(target.ident, alloca)
                    # A prior borrowed binding owns no reference to release.
                    # Clear that alias before the owning root move.
                    self.builder.store(ir.Constant(_CSTR, None), alloca)
                    status = self.builder.call(
                        self.runtime["pcc_gc_root_move"],
                        [self._as_gc_ptr(alloca), self._as_gc_ptr(output)],
                        name=self._fresh(target.ident + ".scalar.move"),
                    )
                    self._owned_local_names.add(target.ident)
                    self._owned_local_has_value.add(target.ident)
                    flag = self._ensure_owned_local_flag(target.ident, alloca)
                    self.builder.store(ir.Constant(_I1, 1), flag)
                    self._slot_call_check_status(status, "local scalar owner move", stmt.span)
                    self._release_slot_call_roots((output,))
                finally:
                    self._try_err_block = previous
                    self._cpy_operand_cleanup_block = saved_cpy
                return
        else:
            if not forced_exact_int_target:
                self._exact_int_env_flags.pop(target.ident, None)
            value = self._coerce(value, stmt.value.ty, declared_ty, stmt.value)
        rhs_local_copy_is_owned = False
        exact_int_name_source_is_borrowed = False
        if exact_int_value is not None and isinstance(stmt.value, Name):
            source_slot = self.env.get(stmt.value.ident)
            exact_int_name_source_is_borrowed = bool(
                source_slot is not None
                and isinstance(source_slot[1], ir.PointerType)
            )
            if not exact_int_name_source_is_borrowed:
                module_global = self._module_globals.get(stmt.value.ident)
                if module_global is not None:
                    global_storage_ty = getattr(
                        module_global[0],
                        "value_type",
                        None,
                    )
                    exact_int_name_source_is_borrowed = isinstance(
                        global_storage_ty,
                        ir.PointerType,
                    )
        exact_int_name_copy = (
            exact_int_value is not None
            and isinstance(stmt.value, Name)
            and exact_int_name_source_is_borrowed
            and isinstance(value.type, ir.PointerType)
            and value not in getattr(self, "_cpy_values", ())
        )
        if (
            isinstance(stmt.value, Name)
            and (
                stmt.value.ident in getattr(self, "_owned_local_names", set())
                or stmt.value.ident in getattr(self, "_except_binding_names", set())
                or exact_int_name_copy
            )
            and isinstance(value.type, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
            and value not in getattr(self, "_cpy_values", ())
        ):
            # `except ... as e` stores a retained handler exception but does
            # not register `e` as a normal owned local because handler cleanup
            # releases it at block exit. A surviving copy (`saved = e`) must
            # take its own ref and then use the normal owned-local root path.
            value = self._gc_retain(
                value,
                name=self._fresh(target.ident + ".local.copy.retain"),
            )
            rhs_local_copy_is_owned = True
        exact_int_result_is_owned = exact_int_value is not None and not (
            isinstance(stmt.value, Name) and exact_int_name_source_is_borrowed
        )
        rhs_emitted_value_is_owned = self._value_is_owned_object(value)
        rhs_is_safe_owned_in_raw = (
            rhs_local_copy_is_owned
            or rhs_emitted_value_is_owned
            or self._raw_scaffold_object_rhs_is_owned(stmt.value)
            or exact_int_result_is_owned
        )
        rhs_returns_owned_object = (
            rhs_local_copy_is_owned
            or rhs_emitted_value_is_owned
            or rhs_native_file_is_owned
            or self._expr_returns_owned_object(stmt.value)
            or exact_int_result_is_owned
        )
        in_raw_scaffold = self._module_uses_raw_int_scaffold
        # In raw-scaffold mode, only enable owned-local management when the
        # local was already tracked (e.g. previously bound to a tracked
        # local) OR the new RHS is an object-producing expression.
        # Modules with explicit C-ABI exports keep manual runtime ref
        # management and thus still get conservative coverage.
        manages_owned_local = (
            isinstance(ir_ty, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
            and (
                target.ident not in getattr(self, "_current_param_names", set())
                or forced_exact_int_target
            )
            and target.ident not in getattr(self, "_current_global_names", set())
            and (
                target.ident in getattr(self, "_owned_local_names", set())
                # This statement can run again after a later statement in the
                # loop has made the same slot owned. Emit the flag protocol
                # at the first borrowed store too, rather than depending on
                # which assignment was visited first during code generation.
                or (
                    bool(self.loop_stack)
                    and not getattr(self, "_suppress_implicit_gc_roots", False)
                    and value not in getattr(self, "_cpy_values", ())
                    and self._is_object(stmt.value.ty)
                    and not self._expr_returns_unsafe_raw_pointer(stmt.value)
                )
                or (
                    rhs_returns_owned_object
                    and (not in_raw_scaffold or rhs_is_safe_owned_in_raw)
                )
            )
        )
        exact_root_store = (
            manages_owned_local
            and (exact_int_result_is_owned or rhs_local_copy_is_owned)
            # A borrowed alias can occupy this slot before its first owned
            # assignment. store_root_take releases the previous slot value
            # unconditionally; that would consume the caller's reference.
            # Such locals need the normal ownership-flag-guarded replacement.
            and target.ident not in getattr(
                self, "_borrowed_gc_rooted_local_names", set()
            )
            and isinstance(value.type, ir.PointerType)
            and value not in getattr(self, "_cpy_values", ())
            and self._ir_type_matches(ir_ty, _CSTR)
        )
        # exact_root_store transfers the fresh value's reference into the slot
        # with one pcc_gc_store_root_take call; the previous pin / store_root /
        # unpin / release quartet cost four runtime calls per exact-int
        # assignment (tests/python/test_exact_int_loop_protocol_ratchet.py).
        owned_alias_kept = None
        if manages_owned_local and not exact_root_store:
            # A borrowed value may be the very object the slot owns
            # (``x = x or default`` in a raw-scaffold module selects the old
            # value without a reference); the slot then keeps its reference.
            incoming = None
            if (
                not rhs_returns_owned_object
                and isinstance(value.type, ir.PointerType)
                and self._ir_type_matches(ir_ty, _CSTR)
                and value not in getattr(self, "_cpy_values", ())
            ):
                incoming = value
            owned_alias_kept = self._emit_release_owned_local_if_flagged(
                target.ident, alloca, incoming=incoming,
            )
        if manages_owned_local:
            self._owned_local_names.discard(target.ident)
        if (
            manages_owned_local
            and rhs_returns_owned_object
            and not exact_root_store
            and isinstance(value.type, ir.PointerType)
            and self._ir_type_matches(ir_ty, _CSTR)
            and value not in getattr(self, "_cpy_values", ())
        ):
            value = self.builder.call(
                self.runtime["pcc_gc_resolve_owned_ptr"],
                [value],
                name=self._fresh(target.ident + ".owned.resolve"),
            )
        if (
            not exact_root_store
            and
            (
                manages_owned_local
                or target.ident in getattr(self, "_gc_rooted_local_names", set())
            )
            and isinstance(value.type, ir.PointerType)
            and value not in getattr(self, "_cpy_values", ())
        ):
            self.builder.call(
                self.runtime["pcc_gc_note_write_barrier"],
                [ir.Constant(_CSTR, None), value],
            )
        if (
            isinstance(value.type, ir.IntType)
            and isinstance(ir_ty, ir.IntType)
            and value.type.width != ir_ty.width
        ):
            if value.type.width < ir_ty.width:
                is_unsigned = False
                rhs_type = getattr(stmt.value, "type", None)
                if isinstance(rhs_type, IntType) and not rhs_type.signed:
                    is_unsigned = True
                if is_unsigned:
                    value = self.builder.zext(value, ir_ty, name=self._fresh("zext"))
                else:
                    value = self.builder.sext(value, ir_ty, name=self._fresh("sext"))
            else:
                value = self.builder.trunc(value, ir_ty, name=self._fresh("trunc"))
        if exact_root_store:
            self.builder.call(
                self.runtime["pcc_gc_store_root_take"],
                [self._as_gc_ptr(alloca), value],
            )
        elif self._is_valueclass_payload_type(declared_ty) and isinstance(ir_ty, ir.LiteralStructType):
            # Rebinding a parameter must not change its borrowed physical
            # slots into owning slots. Give the new binding its own storage.
            registry = self._fn_valueclass_payload_root_slots.get(self.current_function.name, ())
            if any(record[0] is alloca and record[3] for record in registry):
                alloca = self._alloca_in_entry(ir_ty, name=self._fresh(target.ident + ".owned.payload"))
                self.env[target.ident] = (alloca, ir_ty, declared_ty)
            self._ensure_valueclass_payload_gc_roots(target.ident, alloca, declared_ty, borrowed=False)
            self._copy_valueclass_payload(alloca, value, declared_ty)
        else:
            self.builder.store(value, alloca)
        if (
            rhs_local_copy_is_owned
            or rhs_emitted_value_is_owned
            or rhs_native_file_is_owned
            or exact_int_result_is_owned
        ):
            self._owned_local_names.add(target.ident)
            self._ensure_owned_local_gc_root(target.ident, alloca, ir_ty)
        elif owned_alias_kept is not None:
            # Ownership is now decided at run time: keep the flag protocol so
            # every later release and exit consults it. The slot may hold the
            # borrowed value, so it is also a borrowed root: a later
            # exact_root_store would otherwise release it unconditionally.
            self._owned_local_names.add(target.ident)
            self._ensure_owned_local_gc_root(target.ident, alloca, ir_ty)
            self._ensure_borrowed_local_gc_root(target.ident, alloca, ir_ty)
        else:
            self._mark_owned_local_if_object(target.ident, ir_ty, stmt.value)
        if target.ident in self._owned_local_names:
            flag = self._ensure_owned_local_flag(target.ident, alloca)
            self.builder.store(
                owned_alias_kept if owned_alias_kept is not None else ir.Constant(_I1, 1),
                flag,
            )
            self._owned_local_has_value.add(target.ident)
        else:
            flag = self._owned_local_flag_for(target.ident, alloca)
            if flag is not None:
                self.builder.store(ir.Constant(_I1, 0), flag)
            self._owned_local_has_value.discard(target.ident)
            if (
                isinstance(stmt.value, Name)
                and stmt.value.ident in getattr(self, "_except_binding_names", set())
                and isinstance(ir_ty, ir.PointerType)
                and self._ir_type_matches(ir_ty, _CSTR)
                and target.ident not in getattr(self, "_current_param_names", set())
                and target.ident not in getattr(self, "_current_global_names", set())
            ):
                # `saved = e` borrows a caught exception (`e` is an except
                # binding). Once the handler's retain is released at handler end,
                # `saved` is the surviving reference; it MUST be a GC root or the
                # tracing collect (#1/#2/#3/#4) never grays the exception and
                # sweeps its message. See
                # gc-5backend-exception-referent-roots-no-libpython.md.
                self._ensure_owned_local_gc_root(target.ident, alloca, ir_ty)
            elif (
                not getattr(self, "_suppress_implicit_gc_roots", False)
                and isinstance(value.type, ir.PointerType)
                and self._ir_type_matches(ir_ty, _CSTR)
                and value not in getattr(self, "_cpy_values", ())
                and self._is_object(stmt.value.ty)
                and not self._expr_returns_unsafe_raw_pointer(stmt.value)
            ):
                # Ownership and rootability are separate. A borrowed object
                # local must remain an updateable GC root if it lives across
                # later allocations; only the owned flag controls release.
                self._ensure_borrowed_local_gc_root(target.ident, alloca, ir_ty)
            else:
                self._discard_owned_local_gc_root(target.ident, alloca)

    def _emit_destructured_literal_element(self, elem: Expr):
        """Evaluate one element of ``a, b = <e0>, <e1>`` with its ownership.

        Each element is stored through the tuple-unpack protocol, whose default
        ("any non-Dyn value is owned", Dyn is borrowed) is written for
        ``py_tuple_get`` results.  A ``Name`` element that is itself an owned
        local loads a *borrowed* reference to a managed object, so binding it
        as owned made two locals own one reference, and binding it as borrowed
        left the target's loop-carried owned flag from the previous iteration in
        place, so the next owned store released the borrowed pointer.  Either
        way the object was freed while the source still owned it:
        ``symbol, offset = item, 0`` in ``arm64_asm_driver._append_line``
        (``item`` a comprehension element, hence Dyn) followed by
        ``symbol = symbol.strip()`` released ``item``'s string, and ``item``'s
        cleanup then reached a dead cell.  Mirror plain ``x = y``: a copy of an
        owned local (or an ``except ... as e`` binding) takes its own reference,
        emitted before any target is stored so ``a, b = b, a`` also survives
        the release of the old values.  Every other element keeps the existing
        protocol.
        """
        value = self._emit_expr(elem)
        if not isinstance(value.type, ir.PointerType):
            return value, None
        if value in getattr(self, "_cpy_values", ()):
            return value, None
        if isinstance(elem, Name) and (
            elem.ident in getattr(self, "_owned_local_names", set())
            or elem.ident in getattr(self, "_except_binding_names", set())
        ):
            return (
                self._gc_retain(
                    value,
                    name=self._fresh(elem.ident + ".unpack.copy.retain"),
                ),
                True,
            )
        return value, None

    def _emit_tuple_unpack_assign(
        self,
        stmt: Assign,
        target: TupleExpr,
    ) -> None:
        """Lower ``a, b = <rhs>`` into pair-wise name/subscript/attr
        assigns.

        Two RHS shapes are handled:

        * ``TupleExpr`` literal — each elem evaluated in source order,
          then bound into the corresponding target.
        * Any expression whose inferred type is ``TupleType`` with the
          correct arity — value is evaluated once, then each target is
          assigned from ``py_tuple_get(result, i)`` marshaled back to
          the declared element type.

        Anything else (e.g. list RHS, unknown iterable) remains
        unsupported.
        """
        star_indices = [
            i for i, lhs in enumerate(target.elems) if self._is_starred_unpack_expr(lhs)
        ]
        if star_indices:
            return self._emit_starred_unpack_assign(stmt, target, star_indices)

        rhs = stmt.value
        payload_target = any(self._is_valueclass_payload_type(getattr(lhs, "ty", None)) for lhs in target.elems)
        payload_element = any(self._is_valueclass_payload_type(item) for item in _assign_tuple_elems(rhs.ty))
        if (payload_target or payload_element) and (not _assign_is_tuple_expr(rhs) or any(
                self._is_starred_unpack_expr(elem) for elem in rhs.elems)):
            _emit_live_import_sequence_unpack(self, stmt, target)
            return
        live_target = any(
            (isinstance(lhs, (Attr, Subscript)) and live_import_expr_binding(self, lhs.obj))
            or (isinstance(lhs, Name) and live_import_name_slot(self, lhs.ident) is not None)
            for lhs in target.elems
        )
        if live_target and (not _assign_is_tuple_expr(rhs) or any(
                self._is_starred_unpack_expr(elem) for elem in rhs.elems)):
            _emit_live_import_sequence_unpack(self, stmt, target)
            return
        if _assign_is_tuple_expr(rhs) and any(
            self._is_starred_unpack_expr(e) for e in rhs.elems
        ):
            tup_val = self._emit_expr(rhs)
            elem_ty = DynType(name="dyn")
            for i, lhs in enumerate(target.elems):
                elem_obj = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [tup_val, ir.Constant(_I64, i)],
                    name=self._fresh(f"unpack.splat.{i}"),
                )
                self._store_unpack_target(
                    lhs,
                    elem_obj,
                    elem_ty,
                    value_is_owned=True,
                )
            self._gc_release_if_owned(tup_val, rhs)
            return
        if _assign_is_tuple_expr(rhs):
            if len(rhs.elems) != len(target.elems):
                raise L1CodegenError(
                    f"tuple unpack arity mismatch: {len(target.elems)} "
                    f"targets, {len(rhs.elems)} values"
                )
            # Python evaluates the complete RHS before publishing any target,
            # including fresh names. A later evaluation can allocate, relocate
            # earlier results, or raise, so managed elements remain independent
            # authoritative owners until the assignment phase finishes.
            previous = self._current_try_err_block()
            error_target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            roots = []
            rhs_vals = []
            rhs_roots = []
            rhs_owned = []
            try:
                for index, elem in enumerate(rhs.elems):
                    lhs = target.elems[index]
                    planned_exact_int = (
                        isinstance(lhs, Name)
                        and lhs.ident in getattr(self, "_planned_exact_int_local_names", set())
                        and isinstance(elem.ty, (IntType, BoolType))
                    )
                    managed = (
                        (self._is_object(elem.ty) or self._is_valueclass_payload_type(elem.ty) or planned_exact_int
                         or isinstance(lhs, (Attr, Subscript)) and live_import_expr_binding(self, lhs.obj))
                        and not getattr(self, "_freestanding_module", False)
                        and not self._expr_returns_unsafe_raw_pointer(elem)
                        and not self._expr_looks_cpython(elem)
                    )
                    if managed:
                        root = self._emit_slot_call_operand(elem, "unpack.literal")
                        roots.append(root)
                        rhs_roots.append(root)
                        rhs_vals.append(None)
                        rhs_owned.append(True)
                        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
                        self._cpy_operand_cleanup_block = self._try_err_block
                    else:
                        value, owned = self._emit_destructured_literal_element(elem)
                        rhs_vals.append(value)
                        rhs_roots.append(None)
                        rhs_owned.append(owned)
                for index, lhs in enumerate(target.elems):
                    root = rhs_roots[index]
                    if root is not None:
                        self._store_unpack_root_target(lhs, root, rhs.elems[index].ty)
                    else:
                        self._store_unpack_target(
                            lhs, rhs_vals[index], rhs.elems[index].ty,
                            value_is_owned=rhs_owned[index],
                        )
                self._release_slot_call_roots(tuple(roots))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            return

        rhs_ty = rhs.ty
        rhs_elems = _assign_tuple_elems(rhs_ty)
        if _assign_is_tuple_type(rhs_ty) and len(rhs_elems) == len(target.elems):
            tup_val = self._emit_expr(rhs)
            tup_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                tup_val,
                rhs_ty,
            )
            for i, (lhs, elem_ty) in enumerate(zip(target.elems, rhs_elems)):
                idx_val = ir.Constant(_I64, i)
                elem_obj = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [tup_obj, idx_val],
                    name=self._fresh(f"tup.{i}"),
                )
                # Ordinary int elements already carry their exact Python
                # value. Let the target's planned storage choose whether a
                # checked machine conversion is required; unboxing here
                # first loses the bignum before an object slot can keep it.
                native_val = elem_obj
                ordinary_int = isinstance(elem_ty, IntType) and elem_ty.name == "int"
                if not _assign_is_dyn_type(elem_ty) and not ordinary_int:
                    native_val = marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        elem_obj,
                        elem_ty,
                    )
                self._store_unpack_target(
                    lhs,
                    native_val,
                    elem_ty,
                    value_is_owned=True,
                )
            self._gc_release_if_owned(tup_obj, rhs)
            return

        if isinstance(rhs_ty, SetType):
            # Sets are iterable but deliberately not subscriptable.  Check the
            # exact unpack arity before materialising iteration order into a
            # temporary list, then reuse the normal owned-result stores.
            set_val = self._emit_expr(rhs)
            set_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                set_val,
                rhs_ty,
            )
            actual_n = self.builder.call(
                self.runtime["py_obj_len"],
                [set_obj],
                name=self._fresh("unpack.set.len"),
            )
            arity_ok = self.builder.icmp_signed(
                "==",
                actual_n,
                ir.Constant(_I64, len(target.elems)),
                name=self._fresh("unpack.set.arity.ok"),
            )
            fn = self.current_function
            arity_ok_bb = fn.append_basic_block(
                name=self._fresh("unpack.set.arity.match")
            )
            arity_bad_bb = fn.append_basic_block(
                name=self._fresh("unpack.set.arity.mismatch")
            )
            self.builder.cbranch(arity_ok, arity_ok_bb, arity_bad_bb)
            self.builder.position_at_end(arity_bad_bb)
            arity_msg = self._ptr_to_cstr(
                self._cstr_global(
                    "cannot unpack set: arity mismatch "
                    f"(expected {len(target.elems)})",
                    ".set.unpack.arity",
                )
            )
            arity_exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 2), arity_msg],
                name=self._fresh("unpack.set.arity.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [arity_exc])
            arity_err_target = (
                getattr(self, "_try_err_block", None) or self._ensure_fn_err_exit()
            )
            self.builder.branch(arity_err_target)
            self.builder.position_at_end(arity_ok_bb)

            unpack_list = self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("unpack.set.list"),
            )
            self._emit_list_append_via_iter(
                unpack_list,
                set_obj,
                getattr(rhs, "span", None),
            )
            elem_ty = rhs_ty.elem
            for i, lhs in enumerate(target.elems):
                elem_obj = self.builder.call(
                    self.runtime["py_list_get"],
                    [unpack_list, ir.Constant(_I64, i)],
                    name=self._fresh(f"unpack.set.{i}"),
                )
                native_val = elem_obj
                if not _assign_is_dyn_type(elem_ty):
                    native_val = marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        elem_obj,
                        elem_ty,
                    )
                self._store_unpack_target(
                    lhs,
                    native_val,
                    elem_ty,
                    value_is_owned=True,
                )
            self._gc_release(
                unpack_list,
                self._release_context_label("unpack.set.list"),
            )
            self._gc_release_if_owned(set_obj, rhs)
            return

        # DynType / ListType / TupleType-with-unknown-arity RHS:
        # assume a runtime sequence (any ``py_obj_getitem`` /
        # ``py_list_get``-friendly container). Indices are generated at
        # runtime regardless of the element count, so a mismatch
        # between the declared TupleType arity and the target arity
        # just means the inferer was imprecise (e.g. ``tuple(seq)`` at
        # runtime; inferer gave a TupleType of arbitrary size).
        if _assign_is_tuple_type(rhs_ty) and len(rhs_elems) != len(target.elems):
            rhs_ty = DynType(name="dyn")
        if _assign_is_dyn_type(rhs_ty) or _assign_is_list_type(rhs_ty):
            tup_val = self._emit_expr(rhs)
            elem_ty = (
                _assign_list_elem(rhs_ty)
                if _assign_is_list_type(rhs_ty)
                else DynType(name="dyn")
            )
            use_list_get = _assign_is_list_type(rhs_ty)
            for i, lhs in enumerate(target.elems):
                if use_list_get:
                    elem_obj = self.builder.call(
                        self.runtime["py_list_get"],
                        [tup_val, ir.Constant(_I64, i)],
                        name=self._fresh(f"unpack.{i}"),
                    )
                else:
                    idx_box = self.builder.call(
                        self.runtime["py_int_from_i64"],
                        [ir.Constant(_I64, i)],
                        name=self._fresh("unpack.idx.box"),
                    )
                    elem_obj = self.builder.call(
                        self.runtime["py_obj_getitem"],
                        [tup_val, idx_box],
                        name=self._fresh(f"unpack.{i}"),
                    )
                native_val = elem_obj
                if not _assign_is_dyn_type(elem_ty):
                    native_val = marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        elem_obj,
                        elem_ty,
                    )
                self._store_unpack_target(
                    lhs,
                    native_val,
                    elem_ty,
                    value_is_owned=True,
                )
            self._gc_release_if_owned(tup_val, rhs)
            return

        raise NotImplementedError(
            "Layer 1 tuple-unpacking supports a TupleExpr RHS or an "
            "expression whose inferred type is a concrete tuple; "
            f"got {type(rhs).__name__} of type {rhs_ty}"
        )

    def _emit_starred_unpack_assign(
        self,
        stmt: Assign,
        target: TupleExpr,
        star_indices: list[int],
    ) -> None:
        if len(star_indices) != 1:
            raise NotImplementedError(
                "Layer 1 starred unpack assignment supports one starred target"
            )
        rhs = stmt.value
        rhs_ty = rhs.ty
        if not isinstance(rhs_ty, (DynType, ListType, TupleType)):
            raise NotImplementedError(
                "Layer 1 starred unpack assignment requires a list, tuple, "
                "or dynamic sequence RHS"
            )
        star_i = star_indices[0]
        before = star_i
        after = len(target.elems) - star_i - 1
        seq = self._emit_expr(rhs)
        if isinstance(rhs_ty, ListType):
            n_val = self.builder.call(
                self.runtime["py_list_len"],
                [seq],
                name=self._fresh("unpack.star.len"),
            )
            elem_ty: Type = rhs_ty.elem
        elif isinstance(rhs_ty, TupleType):
            n_val = self.builder.call(
                self.runtime["py_tuple_len"],
                [seq],
                name=self._fresh("unpack.star.len"),
            )
            elem_ty = DynType(name="dyn")
        else:
            n_val = self.builder.call(
                self.runtime["py_obj_len"],
                [seq],
                name=self._fresh("unpack.star.len"),
            )
            elem_ty = DynType(name="dyn")

        def load_elem(idx_val: ir.Value, label: str) -> ir.Value:
            if isinstance(rhs_ty, ListType):
                return self.builder.call(
                    self.runtime["py_list_get"],
                    [seq, idx_val],
                    name=self._fresh(label),
                )
            if isinstance(rhs_ty, TupleType):
                return self.builder.call(
                    self.runtime["py_tuple_get"],
                    [seq, idx_val],
                    name=self._fresh(label),
                )
            idx_box = self.builder.call(
                self.runtime["py_int_from_i64"],
                [idx_val],
                name=self._fresh(label + ".idx"),
            )
            return self.builder.call(
                self.runtime["py_obj_getitem"],
                [seq, idx_box],
                name=self._fresh(label),
            )

        i = 0
        while i < before:
            elem = load_elem(
                ir.Constant(_I64, i),
                f"unpack.star.pre.{i}",
            )
            native_val = elem
            if not isinstance(elem_ty, DynType):
                native_val = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    elem,
                    elem_ty,
                )
            self._store_unpack_target(
                target.elems[i],
                native_val,
                elem_ty,
                value_is_owned=True,
            )
            i += 1

        rest_target = target.elems[star_i]
        assert isinstance(rest_target, Call)
        rest_inner = rest_target.args[0]
        # A captured name arrives as its cell subscript; store it like the
        # other unpack targets.
        if not isinstance(rest_inner, (Name, Subscript, Attr)):
            raise NotImplementedError(
                "Layer 1 starred unpack target must be a name, subscript or attribute"
            )
        lo_obj = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, before)],
            name=self._fresh("unpack.star.lo"),
        )
        hi_i64 = self.builder.sub(
            n_val,
            ir.Constant(_I64, after),
            name=self._fresh("unpack.star.hi.i64"),
        )
        hi_obj = self.builder.call(
            self.runtime["py_int_from_i64"],
            [hi_i64],
            name=self._fresh("unpack.star.hi"),
        )
        rest_list = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("unpack.star.rest"),
        )

        fn = self.current_function
        rest_idx_slot = self._alloca_in_entry(
            _I64,
            name="unpack.star.rest.idx.addr",
        )
        self.builder.store(ir.Constant(_I64, before), rest_idx_slot)
        rest_cond_bb = fn.append_basic_block(name=self._fresh("unpack.star.cond"))
        rest_body_bb = fn.append_basic_block(name=self._fresh("unpack.star.body"))
        rest_end_bb = fn.append_basic_block(name=self._fresh("unpack.star.end"))
        self.builder.branch(rest_cond_bb)
        self.builder.position_at_end(rest_cond_bb)
        rest_cur = self.builder.load(
            rest_idx_slot,
            name=self._fresh("unpack.star.idx"),
        )
        rest_keep = self.builder.icmp_signed(
            "<",
            rest_cur,
            hi_i64,
            name=self._fresh("unpack.star.keep"),
        )
        self.builder.cbranch(rest_keep, rest_body_bb, rest_end_bb)
        self.builder.position_at_end(rest_body_bb)
        rest_elem = load_elem(rest_cur, "unpack.star.rest.elem")
        self.builder.call(
            self.runtime["py_list_append"],
            [rest_list, rest_elem],
            name=self._fresh("unpack.star.append"),
        )
        rest_next = self.builder.add(
            rest_cur,
            ir.Constant(_I64, 1),
            name=self._fresh("unpack.star.next"),
        )
        self.builder.store(rest_next, rest_idx_slot)
        self.builder.branch(rest_cond_bb)
        self.builder.position_at_end(rest_end_bb)

        # Keep the boxed bounds alive until after the dynamic rest loop; the
        # generic path above may still need them for GC-rooted temporaries.
        self._gc_release(lo_obj)
        self._gc_release(hi_obj)
        self._store_unpack_target(
            rest_inner,
            rest_list,
            ListType(name="list", elem=elem_ty),
            value_is_owned=True,
        )

        j = 0
        while j < after:
            target_idx = star_i + 1 + j
            src_idx = self.builder.add(
                hi_i64,
                ir.Constant(_I64, j),
                name=self._fresh(f"unpack.star.post.idx.{j}"),
            )
            elem = load_elem(src_idx, f"unpack.star.post.{j}")
            native_val = elem
            if not isinstance(elem_ty, DynType):
                native_val = marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    elem,
                    elem_ty,
                )
            self._store_unpack_target(
                target.elems[target_idx],
                native_val,
                elem_ty,
                value_is_owned=True,
            )
            j += 1

        self._gc_release_if_owned(seq, rhs)

    def _emit_augassign(self, stmt: AugAssign) -> None:
        from pcc.frontends.python.codegen.local_bound_lowering import check_local_bound
        if isinstance(stmt.target, Name):
            check_local_bound(self, stmt.target)
        op_bare = stmt.op.rstrip("=")
        if isinstance(stmt.target, Name):
            if live_import_name_slot(self, stmt.target.ident) is not None:
                _emit_live_import_augassign(self, stmt, op_bare)
                return
            if (
                isinstance(stmt.target.ty, IntType)
                and getattr(self, "_exact_int_env_flags", {}).get(
                    stmt.target.ident,
                    False,
                )
            ) or isinstance(stmt.target.ty, (StrType, BytesType, ByteArrayType)):
                # Reuse the exact Assign path so lhs pinning, RHS error
                # cleanup, owned-result replacement, root barriers, and the
                # local owned flag stay identical to ``x = x <op> rhs``.
                # str/bytes are immutable, so ``+=`` IS ``x = x + rhs``; the
                # generic augassign store below never released the previous
                # owned value (a 20k-char ``cur += ch`` loop retained 299 MB,
                # tests/python/test_ownership_str_iadd_and_len_call_result.py).
                # The current bytearray helper also returns a replacement;
                # that NEW reference needs the same owned-slot transfer.
                combined = BinOp(
                    span=stmt.span,
                    ty=stmt.target.ty,
                    op=op_bare,
                    lhs=stmt.target,
                    rhs=stmt.value,
                )
                self._emit_assign(
                    Assign(
                        span=stmt.span,
                        targets=(stmt.target,),
                        value=combined,
                        annotation=stmt.target.ty,
                    )
                )
                return
            slot = self.env.get(stmt.target.ident)
            module_global_target = stmt.target.ident in self._module_globals and (
                self.current_func_def is None
                or stmt.target.ident in self._current_global_names
            )
            if slot is None and not module_global_target:
                raise L1CodegenError(
                    f"augassign to undefined name {stmt.target.ident!r}"
                )
            if module_global_target:
                cur = self._emit_name(stmt.target)
                _gv, declared_ty = self._module_globals[stmt.target.ident]
            else:
                alloca, _ir_ty, declared_ty = slot
                cur = self.builder.load(
                    alloca,
                    name=self._fresh(stmt.target.ident),
                )
            # Match the types actually passed to the numeric dispatcher;
            # a float/complex RHS selects that route even for an int target.
            if (op_bare == "**" and not isinstance(declared_ty, (FloatType, ComplexType))
                    and not isinstance(stmt.value.ty, (FloatType, ComplexType))
                    and not self._expr_looks_cpython(stmt.target)
                    and not self._expr_looks_cpython(stmt.value)):
                self._guard_integer_power_operand_provenance(stmt.target, stmt.value)
            # If the augassign target holds a CPython value (e.g. ``a`` after
            # ``a = np.ones(3)``), the freshly loaded ``cur`` is a new SSA value
            # that is not in ``_cpy_values`` even though the variable is cpy
            # (cpy-ness for NAMES is tracked in ``_cpy_env_flags``, not per-SSA).
            # Tag the load so the binop below routes through ``py_cpy_binop``
            # (libpython) instead of the native ``+`` dispatch, which raises
            # ``TypeError: unsupported operand`` on a real CPython object. Inert
            # in no-libpython (no cpy names => target never looks cpy).
            if self._expr_looks_cpython(stmt.target) and isinstance(
                cur.type, ir.PointerType
            ):
                if not hasattr(self, "_cpy_values"):
                    self._cpy_values = set()
                self._cpy_values.add(cur)
            rhs = self._emit_expr(stmt.value)
            # CPython augmented assignment tries type(a).__iop__ FIRST;
            # the plain binop only runs when __iop__ is missing or
            # returns NotImplemented. Dyn pointer targets route through
            # py_obj_inplace_op (instances dispatch __iadd__ etc.);
            # everything else keeps the static binop path.
            _inplace_code = {
                "+": 0,
                "-": 1,
                "*": 2,
                "/": 3,
                "//": 4,
                "%": 5,
            }.get(op_bare)
            if (
                isinstance(declared_ty, SetType)
                and isinstance(stmt.value.ty, (SetType, DynType))
                and op_bare in ("|", "&", "-", "^")
            ):
                rhs_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    rhs,
                    stmt.value.ty,
                )
                result = self._emit_checked_set_inplace_values(
                    op_bare,
                    cur,
                    rhs_obj,
                    getattr(stmt, "span", None),
                )
                self._gc_release_if_owned(rhs_obj, stmt.value)
            elif (
                _inplace_code is not None
                and isinstance(declared_ty, (DynType, ClassType, FuncType))
                and isinstance(cur.type, ir.PointerType)
                and cur not in getattr(self, "_cpy_values", ())
            ):
                rhs_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    rhs,
                    stmt.value.ty,
                )
                result = self.builder.call(
                    self.runtime["py_obj_inplace_op"],
                    [cur, rhs_obj, ir.Constant(_I64, _inplace_code)],
                    name=self._fresh("augassign.inplace"),
                )
                # __iop__/binop can raise; without this check the
                # pending exception skips enclosing try/except blocks
                self._emit_post_call_err_check(getattr(stmt, "span", None))
            else:
                result = self._emit_binop_value(
                    op_bare,
                    cur,
                    declared_ty,
                    rhs,
                    stmt.value.ty,
                    result_ty=declared_ty,
                )
            result = self._coerce(result, declared_ty, declared_ty)
            self._store_value_at_name(
                stmt.target,
                result,
                declared_ty,
                value_is_owned=(
                    isinstance(result.type, ir.PointerType)
                    and result not in getattr(self, "_cpy_values", ())
                ),
            )
            return
        if isinstance(stmt.target, Subscript):
            # These branches erase both operand types to dyn before their
            # numeric dispatcher, regardless of the inferred element type.
            if op_bare == "**":
                self._guard_integer_power_operand_provenance(stmt.target, stmt.value)
            # ``d[k] += rhs`` → d[k] = d[k] <op> rhs
            obj_val = self._emit_expr(stmt.target.obj)
            obj_ty = stmt.target.obj.ty
            if isinstance(stmt.target.idx, Slice):
                lo_obj = self._emit_slice_bound_object(stmt.target.idx.lo)
                hi_obj = self._emit_slice_bound_object(stmt.target.idx.hi)
                step_obj = self._emit_slice_bound_object(stmt.target.idx.step)
                obj_as_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    obj_val,
                    obj_ty,
                )
                cur_obj = self.builder.call(
                    self.runtime["py_obj_slice"],
                    [obj_as_obj, lo_obj, hi_obj, step_obj],
                    name=self._fresh("augassign.slice.cur"),
                )
                rhs = self._emit_expr(stmt.value)
                rhs_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    rhs,
                    stmt.value.ty,
                )
                result_raw = self._emit_binop_value(
                    op_bare,
                    cur_obj,
                    DynType(name="dyn"),
                    rhs_obj,
                    DynType(name="dyn"),
                    result_ty=DynType(name="dyn"),
                )
                if not self._ir_type_matches(result_raw.type, _CSTR):
                    result_raw = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        result_raw,
                        IntType(name="int"),
                    )
                self.builder.call(
                    self.runtime["py_obj_set_slice"],
                    [obj_as_obj, lo_obj, hi_obj, step_obj, result_raw],
                )
                self._emit_post_call_err_check(getattr(stmt, "span", None))
                return
            # Exact list receivers use the raising typed accessors with a
            # single i64 index evaluation; everything else keeps the generic
            # object-key dispatchers. Load and store branch together so the
            # inplace/binop middle stays shared.
            idx_i64 = None
            idx_obj = None
            if isinstance(obj_ty, ListType) and isinstance(
                stmt.target.idx.ty, (IntType, BoolType)
            ):
                idx_i64 = self._emit_index_expr_as_i64(stmt.target.idx)
            else:
                idx_obj = self._emit_subscript_key_object(stmt.target.idx)
            obj_as_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                obj_val,
                obj_ty,
            )
            if idx_i64 is not None:
                cur_obj = self.builder.call(
                    self.runtime["py_list_getitem"],
                    [obj_as_obj, idx_i64],
                    name=self._fresh("augassign.list.cur"),
                )
            else:
                cur_obj = self.builder.call(
                    self.runtime["py_obj_getitem"],
                    [obj_as_obj, idx_obj],
                    name=self._fresh("augassign.cur"),
                )
            # The load raises for out-of-range/missing keys (IndexError /
            # KeyError) BEFORE the RHS is evaluated (CPython aug-assign
            # order); without this check the pending exception skips the
            # enclosing try/except and the inplace op runs on NULL.
            self._emit_post_call_err_check(getattr(stmt, "span", None))
            rhs = self._emit_expr(stmt.value)
            rhs_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                stmt.value.ty,
            )
            _ip_code = {
                "+": 0,
                "-": 1,
                "*": 2,
                "/": 3,
                "//": 4,
                "%": 5,
            }.get(op_bare)
            if _ip_code is not None:
                # CPython tries type(cur).__iop__ first (instances);
                # plain values fall through to the binary dispatchers
                # inside py_obj_inplace_op.
                result_raw = self.builder.call(
                    self.runtime["py_obj_inplace_op"],
                    [cur_obj, rhs_obj, ir.Constant(_I64, _ip_code)],
                    name=self._fresh("augassign.inplace"),
                )
                self._emit_post_call_err_check(getattr(stmt, "span", None))
            else:
                result_raw = self._emit_binop_value(
                    op_bare,
                    cur_obj,
                    DynType(name="dyn"),
                    rhs_obj,
                    DynType(name="dyn"),
                    result_ty=DynType(name="dyn"),
                )
            # Box if not already a PyObject* (Dyn int binops return
            # i64).
            if not self._ir_type_matches(result_raw.type, _CSTR):
                result_raw = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    result_raw,
                    IntType(name="int"),
                )
            if idx_i64 is not None:
                self.builder.call(
                    self.runtime["py_list_setitem"],
                    [obj_as_obj, idx_i64, result_raw],
                )
            else:
                self.builder.call(
                    self.runtime["py_obj_setitem"],
                    [obj_as_obj, idx_obj, result_raw],
                )
            # The store-back shares the raising user-visible contract
            # (out-of-range list store -> IndexError, user __setitem__ can
            # raise).
            self._emit_post_call_err_check(getattr(stmt, "span", None))
            return
        if isinstance(stmt.target, Attr):
            target = stmt.target
            # A typed scalar field on ``self`` must use the same
            # receiver-aware field layout as ordinary ``self.x`` loads and
            # stores.  This matters for methods defined on a mixin: the
            # lexical class does not own the composed receiver's fixed field
            # slots, and generic ``py_obj_getattr`` cannot recover those slots
            # by name.  The old dynamic-only path made L1CodeGen's inherited
            # ``self._tmp_counter += 1`` load NULL inside pcc1.
            typed_self = (
                isinstance(target.obj, Name)
                and target.obj.ident == "self"
                and isinstance(target.ty, (IntType, FloatType))
            )
            # Only the typed-self branch keeps these numeric types. Other
            # attribute branches pass dyn/dyn, including float annotations.
            if op_bare == "**" and (not typed_self or (
                    not isinstance(target.ty, (FloatType, ComplexType))
                    and not isinstance(stmt.value.ty, (FloatType, ComplexType)))):
                self._guard_integer_power_operand_provenance(target, stmt.value)
            if typed_self:
                cur = self._emit_attr(target)
                rhs = self._emit_expr(stmt.value)
                result = self._emit_binop_value(
                    op_bare,
                    cur,
                    target.ty,
                    rhs,
                    stmt.value.ty,
                    result_ty=target.ty,
                )
                result = self._coerce(result, target.ty, target.ty)
                self._emit_attr_store_value(target, result, target.ty)
                return

            if isinstance(target.obj, Name) and target.obj.ident == "self":
                cur = self._emit_attr(target)
                cur_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    cur,
                    target.ty,
                )
                rhs = self._emit_expr(stmt.value)
                rhs_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    rhs,
                    stmt.value.ty,
                )
                _ip_code = {
                    "+": 0,
                    "-": 1,
                    "*": 2,
                    "/": 3,
                    "//": 4,
                    "%": 5,
                }.get(op_bare)
                if _ip_code is not None:
                    result_obj = self.builder.call(
                        self.runtime["py_obj_inplace_op"],
                        [cur_obj, rhs_obj, ir.Constant(_I64, _ip_code)],
                        name=self._fresh("augassign.self.inplace"),
                    )
                    self._emit_post_call_err_check(getattr(stmt, "span", None))
                else:
                    result_obj = self._emit_binop_value(
                        op_bare,
                        cur_obj,
                        DynType(name="dyn"),
                        rhs_obj,
                        DynType(name="dyn"),
                        result_ty=DynType(name="dyn"),
                    )
                    if not self._ir_type_matches(result_obj.type, _CSTR):
                        result_obj = marshal.marshal_to_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            result_obj,
                            IntType(name="int"),
                        )
                self._emit_attr_store_value(
                    target,
                    result_obj,
                    DynType(name="dyn"),
                )
                return

            # Dynamic/object attribute augmented assignment keeps Python's
            # get/in-place-op/set protocol (including descriptors).
            obj_val = self._emit_expr(target.obj)
            name_ptr = self._attr_name_ptr(target.name)
            cur_obj = self.builder.call(
                self.runtime["py_obj_getattr"],
                [obj_val, name_ptr],
                name=self._fresh("augassign.attr.cur"),
            )
            rhs = self._emit_expr(stmt.value)
            rhs_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                rhs,
                stmt.value.ty,
            )
            _ip_code = {
                "+": 0,
                "-": 1,
                "*": 2,
                "/": 3,
                "//": 4,
                "%": 5,
            }.get(op_bare)
            if _ip_code is not None:
                # CPython tries type(cur).__iop__ first (instances);
                # plain values fall through to the binary dispatchers
                # inside py_obj_inplace_op.
                result_raw = self.builder.call(
                    self.runtime["py_obj_inplace_op"],
                    [cur_obj, rhs_obj, ir.Constant(_I64, _ip_code)],
                    name=self._fresh("augassign.inplace"),
                )
                self._emit_post_call_err_check(getattr(stmt, "span", None))
            else:
                result_raw = self._emit_binop_value(
                    op_bare,
                    cur_obj,
                    DynType(name="dyn"),
                    rhs_obj,
                    DynType(name="dyn"),
                    result_ty=DynType(name="dyn"),
                )
            if not self._ir_type_matches(result_raw.type, _CSTR):
                result_raw = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    result_raw,
                    IntType(name="int"),
                )
            self.builder.call(
                self.runtime["py_obj_setattr"],
                [obj_val, name_ptr, result_raw],
            )
            return
        raise NotImplementedError(
            f"Layer 1 augassign target type "
            f"{type(stmt.target).__name__} not supported"
        )
