"""Assignment statement lowering helpers for L1CodeGen."""

from __future__ import annotations

import sys
import os
from pcc.llvm_capi.compat import ir

from ..py_ast import (
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
    DictExpr,
    DictType,
    DynType,
    Expr,
    FloatType,
    FuncType,
    IfExpr,
    IntType,
    ListExpr,
    ListType,
    MemoryViewType,
    Name,
    NoneType,
    Slice,
    SetType,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
)
from . import marshal
from .errors import L1CodegenError

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

        payload_slot = self._alloca_in_entry(
            payload_ty,
            name=self._fresh(f"value.{class_name}.tmp"),
        )
        zero = ir.Constant(ir.IntType(32), 0)
        for idx, ((_field_name, field_ty), arg_expr) in enumerate(
            zip(fields, resolved_args)
        ):
            if self._is_valueclass_payload_type(field_ty):
                nested_payload = self._maybe_emit_valueclass_constructor_payload(
                    field_ty,
                    arg_expr,
                )
                if nested_payload is not None:
                    field_value = nested_payload
                else:
                    raw_value = self._emit_expr(arg_expr)
                    field_value = self._coerce(raw_value, arg_expr.ty, field_ty)
            elif isinstance(field_ty, IntType):
                field_value = self._emit_expr_as_i64(arg_expr)
            else:
                raw_value = self._emit_expr(arg_expr)
                if isinstance(field_ty, FloatType):
                    field_value = self._to_double(raw_value, arg_expr.ty)
                elif isinstance(field_ty, BoolType):
                    field_value = self._truthy(raw_value, arg_expr.ty)
                else:
                    field_value = self._coerce(raw_value, arg_expr.ty, field_ty)
            field_ptr = self.builder.gep(
                payload_slot,
                [zero, ir.Constant(ir.IntType(32), idx)],
                inbounds=True,
                name=self._fresh(f"value.{class_name}.field{idx}"),
            )
            self.builder.store(field_value, field_ptr)
        return self.builder.load(
            payload_slot,
            name=self._fresh(f"value.{class_name}.payload"),
        )

    def _emit_assign(self, stmt: Assign) -> None:
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
        if self._expr_is_ir_builder_ctor(stmt.value):
            self._ir_builder_env_flags[target.ident] = True
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
            stmt.annotation if stmt.annotation is not None else target.ty
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

        target_ty = stmt.annotation if stmt.annotation is not None else target.ty
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
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            valueclass_target_ty,
            stmt.value,
        )
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
            target_ty = stmt.annotation if stmt.annotation is not None else target.ty
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
                value = self._coerce(value, stmt.value.ty, declared_ty)
            if value in getattr(self, "_cpy_values", ()):
                self._cpy_module_flags[target.ident] = True
                is_cpy_value = True
            else:
                self._cpy_module_flags.pop(target.ident, None)
                is_cpy_value = False
            value_is_owned = exact_int_value is not None or (
                self._raw_scaffold_object_rhs_is_owned(stmt.value)
                and self._expr_returns_owned_object(stmt.value)
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
                else self._storage_ir_type(local_target_ty)
            )
            init_null = isinstance(ir_ty, ir.PointerType) and self._ir_type_matches(
                ir_ty, _CSTR
            )
            alloca = self._alloca_in_entry(
                ir_ty,
                name=f"{target.ident}.addr",
                init_null=init_null,
            )
            self.env[target.ident] = (alloca, ir_ty, local_target_ty)
            slot = self.env[target.ident]

        alloca, ir_ty, declared_ty = slot
        target_storage_ty = (
            _CSTR
            if (boxed_int_target or exact_int_value is not None)
            else self._storage_ir_type(local_target_ty)
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
            # type instead of the first assignment's type.
            declared_ty = local_target_ty
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
        else:
            if not forced_exact_int_target:
                self._exact_int_env_flags.pop(target.ident, None)
            value = self._coerce(value, stmt.value.ty, declared_ty)
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
        if manages_owned_local and not exact_root_store:
            self._emit_release_owned_local_if_flagged(target.ident, alloca)
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
        else:
            self.builder.store(value, alloca)
        if self._is_valueclass_payload_type(declared_ty):
            self._ensure_valueclass_payload_gc_roots(target.ident, alloca, declared_ty)
        if (
            rhs_local_copy_is_owned
            or rhs_emitted_value_is_owned
            or rhs_native_file_is_owned
            or exact_int_result_is_owned
        ):
            self._owned_local_names.add(target.ident)
            self._ensure_owned_local_gc_root(target.ident, alloca, ir_ty)
        else:
            self._mark_owned_local_if_object(target.ident, ir_ty, stmt.value)
        if target.ident in self._owned_local_names:
            flag = self._ensure_owned_local_flag(target.ident, alloca)
            self.builder.store(ir.Constant(_I1, 1), flag)
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
            safe_fresh_names = True
            i = 0
            while i < len(target.elems):
                lhs = target.elems[i]
                if not _assign_is_name(lhs):
                    safe_fresh_names = False
                    break
                ident = lhs.ident
                if ident in self.env:
                    safe_fresh_names = False
                    break
                if ident in getattr(self, "_current_global_names", set()):
                    safe_fresh_names = False
                    break
                if ident in getattr(self, "_current_param_names", set()):
                    safe_fresh_names = False
                    break
                j = 0
                while j < i:
                    prev = target.elems[j]
                    if _assign_is_name(prev) and prev.ident == ident:
                        safe_fresh_names = False
                        break
                    j += 1
                if not safe_fresh_names:
                    break
                i += 1
            if safe_fresh_names:
                i = 0
                while i < len(target.elems):
                    elem = rhs.elems[i]
                    self._store_unpack_target(
                        target.elems[i],
                        self._emit_expr(elem),
                        elem.ty,
                    )
                    i += 1
                return

            rhs_vals: list = []
            rhs_tys: list = []
            rhs_owned: list[bool | None] = []
            for index, e in enumerate(rhs.elems):
                lhs = target.elems[index]
                planned_exact_int = (
                    isinstance(lhs, Name)
                    and lhs.ident
                    in getattr(self, "_planned_exact_int_local_names", set())
                    and isinstance(lhs.ty, IntType)
                    and isinstance(e.ty, (IntType, BoolType))
                )
                if planned_exact_int:
                    # Destructuring evaluates every RHS before publishing any
                    # target.  Preserve that ordering, but evaluate an element
                    # destined for a planned exact-int slot directly in the
                    # object projection.  Emitting the generic scalar value
                    # first (for example ``1 << 70``) would overflow in i64
                    # before the later store had a chance to box it.
                    rhs_vals.append(self._emit_exact_int_operand_object(e))
                    rhs_owned.append(self._pcc_pointer_source_is_owned(e))
                else:
                    rhs_vals.append(self._emit_expr(e))
                    rhs_owned.append(None)
                rhs_tys.append(e.ty)
            i = 0
            while i < len(target.elems):
                self._store_unpack_target(
                    target.elems[i],
                    rhs_vals[i],
                    rhs_tys[i],
                    value_is_owned=rhs_owned[i],
                )
                i += 1
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
                # Marshal the PyObject* back to the declared element
                # type so downstream stores see a native value when
                # possible.
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
        if not isinstance(rest_inner, Name):
            raise NotImplementedError(
                "Layer 1 starred unpack target must be a bare name"
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
        op_bare = stmt.op.rstrip("=")
        if isinstance(stmt.target, Name):
            if (
                isinstance(stmt.target.ty, IntType)
                and getattr(self, "_exact_int_env_flags", {}).get(
                    stmt.target.ident,
                    False,
                )
            ) or isinstance(stmt.target.ty, (StrType, BytesType)):
                # Reuse the exact Assign path so lhs pinning, RHS error
                # cleanup, owned-result replacement, root barriers, and the
                # local owned flag stay identical to ``x = x <op> rhs``.
                # str/bytes are immutable, so ``+=`` IS ``x = x + rhs``; the
                # generic augassign store below never released the previous
                # owned value (a 20k-char ``cur += ch`` loop retained 299 MB,
                # tests/python/test_ownership_str_iadd_and_len_call_result.py).
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
                and isinstance(declared_ty, (DynType, ClassType))
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
            if (
                isinstance(target.obj, Name)
                and target.obj.ident == "self"
                and isinstance(target.ty, (IntType, FloatType))
            ):
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
