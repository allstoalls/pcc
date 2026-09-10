"""Direct/static Python method call lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import BoolType, Call, DynType, Expr, FloatType, IntType, Name, Type
from . import marshal
from .errors import L1CodegenError
from .freestanding_abi_constants import PY_TYPE_FUNC
from .generator_lowering import emit_generator_may_park_child


def _method_param_ir_type(host, method_fn, index: int, label: str) -> ir.Type:
    """Return the emitted method's real ABI parameter type.

    The inferred ``FuncDef`` annotation is semantic information; it is not an
    ABI.  In particular, Python ``int`` parameters may be emitted either as a
    boxed pointer or as an i64 depending on the module boundary.  Method calls
    must therefore marshal against the already-declared LLVM function instead
    of reconstructing the ABI from the annotation alone.
    """
    try:
        ir_args = method_fn.args
    except AttributeError:
        ir_args = ()
    ir_arg = ir_args[index] if index < len(ir_args) else None
    param_ir_ty = host._function_arg_ir_type_or_none(method_fn, index, ir_arg)
    if param_ir_ty is None:
        raise L1CodegenError(
            f"{label}: method argument {index} has no declared ABI slot"
        )
    return param_ir_ty


def _method_abi_type_matches(host, actual: ir.Type, expected: ir.Type) -> bool:
    """Compare emitted opaque-pointer ABI types without losing provenance.

    The general codegen matcher must distinguish tracked pointees so typed
    loads/stores can insert the required casts.  A call boundary, however,
    emits LLVM opaque ``ptr`` and only the address space is ABI-significant.
    """
    if actual is expected:
        return actual is not None
    if isinstance(actual, ir.PointerType) and isinstance(expected, ir.PointerType):
        return getattr(actual, "addrspace", 0) == getattr(expected, "addrspace", 0)
    if isinstance(actual, ir.LiteralStructType) and isinstance(expected, ir.LiteralStructType):
        if actual.packed != expected.packed or len(actual.elements) != len(expected.elements):
            return False
        index = 0
        while index < len(actual.elements):
            if not _method_abi_type_matches(host, actual.elements[index], expected.elements[index]):
                return False
            index += 1
        return True
    if isinstance(actual, ir.ArrayType) and isinstance(expected, ir.ArrayType):
        return actual.count == expected.count and _method_abi_type_matches(
            host, actual.element, expected.element,
        )
    return host._ir_type_matches(actual, expected)


def _method_semantic_arg_type(declared_ty, param_ir_ty: ir.Type) -> Type:
    """Keep semantic type information while making native ABI slots honest."""
    if declared_ty is not None and not isinstance(declared_ty, DynType):
        return declared_ty
    if isinstance(param_ir_ty, ir.IntType):
        if param_ir_ty.width == 1:
            return BoolType(name="bool")
        return IntType(name="int")
    if isinstance(param_ir_ty, (ir.FloatType, ir.DoubleType)):
        return FloatType(name="float")
    return DynType(name="dyn")


def _method_source_arg_type(host, arg_expr: Expr) -> Type:
    """Return the best semantic type for method-argument provenance.

    ``Name.ty`` can retain the expression-site widening even when the local
    slot has a more precise type.  Provenance must use that slot type when it
    is available: a pointer-form ``int``/object is GC-managed, while a truly
    dynamic pointer in a raw-scaffold module may be an opaque native address.
    """
    source_ty = arg_expr.ty
    if isinstance(arg_expr, Name):
        env_entry = host.env.get(arg_expr.ident)
        if env_entry is not None and len(env_entry) >= 3:
            source_ty = env_entry[2]
    return source_ty


def _method_pointer_provenance(
    host,
    value: ir.Value,
    source_ty: Type,
    *,
    source_expr=None,
    newly_owned: bool = False,
):
    """Classify one ABI value as managed/raw/owned.

    The returned tuple is ``(value, managed, raw, owned)``.  ``managed`` and
    ``raw`` are mutually exclusive.  CPython-domain pointers are neither: the
    existing cpy ownership path remains their owner.

    A ``Dyn`` target alone is not evidence that a pointer is raw.  Ordinary
    Python modules use ``Dyn`` for managed objects, so those values must remain
    pinned across later argument evaluation and the call.  Raw-scaffold and
    freestanding modules are the exceptional domain: an imprecise ``Dyn``
    source there can be ``stack_alloc``/``int_to_ptr``/an opaque ABI handle and
    must never reach ``pcc_gc_pin``.  A newly-created box is managed even in
    that domain.
    """
    if not isinstance(value.type, ir.PointerType):
        return (value, False, False, False)
    if value in getattr(host, "_cpy_values", ()):
        return (value, False, False, False)

    explicit_raw = False
    if source_expr is not None:
        explicit_raw = host._expr_returns_unsafe_raw_pointer(source_expr)
    raw_context = bool(
        isinstance(source_ty, DynType)
        and (
            getattr(host, "_freestanding_module", False)
            or getattr(host, "_module_has_c_abi_export", False)
            or getattr(host, "_module_uses_raw_int_scaffold", False)
        )
    )
    raw = bool(not newly_owned and (explicit_raw or raw_context))
    owned = bool(newly_owned)
    if source_expr is not None and not raw and not owned:
        owned = bool(host._pcc_pointer_source_is_owned(source_expr))
        if not owned and isinstance(source_ty, (IntType, FloatType, BoolType)):
            # In a raw-scaffold module the generic ownership predicate is
            # intentionally conservative.  Once ABI lowering has produced a
            # pointer for a non-Name scalar expression, however, it is either
            # a fresh box/bignum or an immortal tagged/singleton value.  It is
            # therefore safe and necessary to balance it as call-owned.
            owned = not isinstance(source_expr, Name)
    return (value, not raw, raw, owned)


def _method_managed_receiver_provenance(host, value: ir.Value):
    """Class receivers are managed even in modules that also use raw pointers."""
    if not isinstance(value.type, ir.PointerType):
        return (value, False, False, False)
    if value in getattr(host, "_cpy_values", ()):
        return (value, False, False, False)
    return (value, True, False, False)


def _method_pin_arg_provenance(host, provenance):
    value, managed, raw, owned = provenance
    if managed and raw:
        raise L1CodegenError("method argument cannot be both managed and raw")
    if raw and owned:
        raise L1CodegenError("raw method argument cannot carry pcc ownership")
    if owned and not managed:
        raise L1CodegenError("owned method argument must be GC-managed")
    if managed:
        host.builder.call(host.runtime["pcc_gc_pin"], [value])
    return (value, managed, raw, owned)


def _method_pinned_arg_cleanup(provenance):
    cleanup: list[tuple[ir.Value, bool]] = []
    for value, managed, _raw, owned in provenance:
        if managed:
            cleanup.append((value, owned))
    return tuple(cleanup)


def _method_emit_ast_args(
    host,
    method_fn,
    label: str,
    arg_exprs: tuple[Expr, ...],
    declared,
    *,
    param_offset: int,
    initial_provenance=(),
):
    """Evaluate and marshal method arguments against the emitted ABI.

    Returns the values, their semantic types (for the dynamic method-table
    branch), and managed pointer temporaries pinned across later argument
    evaluation and the call.  A ``Dyn`` borrowed pointer is deliberately not
    assumed to be a managed object: freestanding modules also use opaque raw
    pointers under that semantic type.
    """
    values: list[ir.Value] = []
    value_types: list[Type] = []
    provenance = list(initial_provenance)
    for index, arg_expr in enumerate(arg_exprs):
        declared_ty = declared[index].annotation if index < len(declared) else None
        param_index = index + param_offset
        param_ir_ty = _method_param_ir_type(host, method_fn, param_index, label)
        target_ty = _method_semantic_arg_type(declared_ty, param_ir_ty)
        value = host._emit_arg_for_abi_param_with_cleanup(
            arg_expr,
            target_ty,
            param_ir_ty,
            _method_pinned_arg_cleanup(tuple(provenance)),
        )
        if not _method_abi_type_matches(host, value.type, param_ir_ty):
            raise L1CodegenError(
                f"{label}: argument {index} lowered as {value.type}, "
                f"but the emitted method ABI requires {param_ir_ty}"
            )
        arg_provenance = _method_pointer_provenance(
            host,
            value,
            _method_source_arg_type(host, arg_expr),
            source_expr=arg_expr,
            newly_owned=bool(
                getattr(host, "_last_call_arg_owned_temp", False)
            ),
        )
        provenance.append(_method_pin_arg_provenance(host, arg_provenance))
        values.append(value)
        value_types.append(target_ty)
    return tuple(values), tuple(value_types), tuple(provenance)


def _method_release_arg_provenance(host, provenance) -> None:
    for value, managed, raw, owned in provenance:
        if managed:
            host.builder.call(
                host.runtime["pcc_gc_unpin"],
                [host._value_available_at_insertion_point(value)],
            )
            if owned:
                host._gc_release(
                    value,
                    host._release_context_label("method_call_arg"),
                )
        elif raw and owned:
            raise L1CodegenError("raw method argument cannot carry pcc ownership")


def _method_unflatten_call_shape(declared, arg_values):
    """Recover a Python call shape from ABI-flattened method arguments.

    ``_resolve_call_kwargs`` hands back one value per declared formal in formal
    order: a keyword-only formal lands in a positional slot, ``*args`` holds a
    packed tuple and ``**kwargs`` a packed dict. That is exactly what the
    native ABI wants. The dynamic ``py_func_call`` path re-packs those slots
    into a Python argument tuple instead, where a keyword-only formal has to
    arrive by name and the packed slots have to be splatted, or the runtime
    binder rejects the call.

    Returns ``(positional, keywords, var_positional, var_keyword)``. Shapes
    ``_resolve_call_kwargs`` does not produce one-to-one are returned flat and
    unchanged, so the caller keeps its existing behaviour for them.
    """
    flat = (list(arg_values), [], None, None)
    if len(declared) != len(arg_values):
        return flat
    positional: list = []
    keywords: list = []
    var_positional = None
    var_keyword = None
    for formal, value in zip(declared, arg_values):
        kind = formal.kind
        if kind == "**kwargs":
            if var_keyword is not None:
                return flat
            var_keyword = value
        elif var_keyword is not None:
            return flat
        elif kind == "kw_only":
            keywords.append((formal.name, value))
        elif kind == "*args":
            if var_positional is not None or keywords:
                return flat
            var_positional = value
        elif kind in ("pos", "pos_only"):
            if var_positional is not None or keywords:
                return flat
            positional.append(value)
        else:
            return flat
    return positional, keywords, var_positional, var_keyword


def _method_coerce_value_for_abi(
    host,
    value: ir.Value,
    value_ty: Type,
    target_ty: Type,
    param_ir_ty: ir.Type,
    label: str,
    index: int,
) -> tuple[ir.Value, bool]:
    """Marshal an already-emitted value to a direct method's real ABI."""
    newly_owned = False
    if isinstance(param_ir_ty, ir.PointerType):
        if isinstance(value.type, ir.PointerType):
            result = value
        else:
            result = marshal.marshal_to_object(
                host.builder,
                host.module,
                host.runtime,
                value,
                value_ty,
            )
            newly_owned = True
    elif isinstance(param_ir_ty, ir.IntType):
        if param_ir_ty.width == 1:
            result = host._truthy(value, value_ty)
        else:
            result = host._to_int64(value, value_ty)
            if param_ir_ty.width < 64:
                result = host.builder.trunc(
                    result,
                    param_ir_ty,
                    name=host._fresh("method.arg.trunc"),
                )
            elif param_ir_ty.width > 64:
                result = host.builder.sext(
                    result,
                    param_ir_ty,
                    name=host._fresh("method.arg.extend"),
                )
    elif isinstance(param_ir_ty, (ir.FloatType, ir.DoubleType)):
        result = host._to_double(value, value_ty)
        if isinstance(param_ir_ty, ir.FloatType) and isinstance(
            result.type, ir.DoubleType
        ):
            result = host.builder.fptrunc(
                result,
                param_ir_ty,
                name=host._fresh("method.arg.fptrunc"),
            )
    else:
        result = host._coerce(value, value_ty, target_ty)
    if not _method_abi_type_matches(host, result.type, param_ir_ty):
        raise L1CodegenError(
            f"{label}: argument {index} lowered as {result.type}, "
            f"but the emitted method ABI requires {param_ir_ty}"
        )
    return result, newly_owned


class MethodCallLoweringMixin:
    def _method_arg_prefers_native_callable_value(self, expr: Expr) -> bool:
        if not isinstance(expr, Name):
            return False
        if self._name_returns_owned_function_value(expr.ident):
            return True
        return self._name_returns_native_builtin_callable_value(expr.ident)

    def _emit_method_arg_as_pcc_object(self, expr: Expr) -> ir.Value:
        if self._method_arg_prefers_native_callable_value(expr):
            value = self._emit_expr_with_native_callable_values(expr)
            return marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                value,
                expr.ty,
            )
        return self._emit_expr_as_pcc_object(expr)

    def _emit_data_descriptor_get(
        self,
        class_name: str,
        attr_name: str,
        obj_val: ir.Value,
    ):
        desc = self._class_attr_descriptor_class(class_name, attr_name)
        if desc is None:
            return None
        owner_info, desc_info = desc
        if "__get__" not in desc_info.methods:
            return None
        if "__set__" not in desc_info.methods and "__delete__" not in desc_info.methods:
            return None
        attr_entry = owner_info.class_attrs.get(attr_name)
        if attr_entry is None:
            return None
        desc_obj = self.builder.load(
            attr_entry[0],
            name=self._fresh(f"descr.{attr_name}"),
        )
        owner_cls = self.builder.load(
            owner_info.global_var,
            name=self._fresh(f"descr.owner.{owner_info.name}"),
        )
        return self._call_user(
            desc_info.methods["__get__"],
            [desc_obj, obj_val, owner_cls],
            self._fresh(f"descr.get.{attr_name}"),
            None,
        )

    def _emit_data_descriptor_set(
        self,
        class_name: str,
        attr_name: str,
        obj_val: ir.Value,
        value_obj: ir.Value,
    ) -> bool:
        desc = self._class_attr_descriptor_class(class_name, attr_name)
        if desc is None:
            return False
        owner_info, desc_info = desc
        setter = desc_info.methods.get("__set__")
        if setter is None:
            return False
        attr_entry = owner_info.class_attrs.get(attr_name)
        if attr_entry is None:
            return False
        desc_obj = self.builder.load(
            attr_entry[0],
            name=self._fresh(f"descr.{attr_name}"),
        )
        self._call_user(
            setter,
            [desc_obj, obj_val, value_obj],
            "",
            None,
        )
        return True

    def _emit_static_method_call(
        self,
        method_fn: ir.Function,
        info,
        method_name: str,
        arg_exprs: tuple[Expr, ...],
        kwargs: tuple = (),
        park_expr: Optional[Call] = None,
    ) -> ir.Value:
        """Lower ``ClassName.staticmethod(args)`` without any receiver
        and with argument coercion honouring declared annotations."""
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        # Always resolve kwargs + fill defaults so trailing params
        # with defaults land even when the call omitted them.
        if ast_fd is not None:
            try:
                arg_exprs = tuple(
                    self._resolve_call_kwargs(
                        arg_exprs,
                        kwargs,
                        ast_fd.args,
                    )
                )
            except L1CodegenError as e:
                raise L1CodegenError(
                    f"staticmethod {info.name}.{method_name}: {e}"
                ) from e
        elif kwargs:
            raise NotImplementedError(
                f"staticmethod {info.name}.{method_name} with kwargs "
                "needs a FuncDef to resolve parameter names"
            )
        declared = ast_fd.args if ast_fd else ()
        label = f"staticmethod {info.name}.{method_name}"
        args_ir, _, arg_provenance = _method_emit_ast_args(
            self,
            method_fn,
            label,
            arg_exprs,
            declared,
            param_offset=0,
        )
        ret_ty = method_fn.function_type.return_type
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"{info.name}.{method_name}.ret")
        )
        local_method_may_park = (
            ast_fd is not None
            and id(ast_fd)
            in getattr(self, "_vthread_may_park_method_ids", set())
        ) or info.name + "." + method_name in getattr(
            self,
            "_vthread_may_park_method_keys",
            set(),
        )
        external_method_may_park = method_name in getattr(
            info,
            "may_park_methods",
            set(),
        )
        method_may_park = local_method_may_park or external_method_may_park
        result = self._call_user(
            method_fn,
            list(args_ir),
            call_name,
            root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
            pinned_arg_temps=_method_pinned_arg_cleanup(arg_provenance),
        )
        if method_may_park:
            if park_expr is None:
                raise L1CodegenError(
                    "may_park staticmethod call has no continuation call site: "
                    + info.name
                    + "."
                    + method_name
                )
            effect_name = info.name + "." + method_name
            if external_method_may_park and getattr(info, "owning_module", None):
                effect_name = info.owning_module + "." + effect_name
            return emit_generator_may_park_child(
                self,
                park_expr,
                effect_name,
                result,
                arg_provenance,
            )
        _method_release_arg_provenance(self, arg_provenance)
        return result

    def _emit_direct_method_call(
        self,
        method_fn: ir.Function,
        self_val: ir.Value,
        info,
        method_name: str,
        arg_exprs: tuple[Expr, ...],
        kwargs: tuple = (),
        park_expr: Optional[Call] = None,
    ) -> ir.Value:
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        if ast_fd is not None and ast_fd.args:
            receiver_ty = ast_fd.args[0].annotation
            if (
                receiver_ty is not None
                and self._is_valueclass_payload_type(receiver_ty)
                and isinstance(self_val.type, ir.PointerType)
            ):
                payload = self._emit_object_to_valueclass_payload(
                    self_val,
                    receiver_ty,
                )
                if payload is not None:
                    self_val = payload
        args_ir: list[ir.Value] = [self_val]
        # Always resolve positional → full arg list so defaults land on
        # omitted trailing params, not just when kwargs were supplied.
        # The earlier ``if kwargs:`` gate let calls like
        # ``self._mark(action, node)`` — where ``_mark(self, action,
        # node, detail="")`` has a default — slip through with only
        # 2 SSA operands, then clang rejected the resulting call as
        # ``not enough parameters specified``.
        if ast_fd is not None:
            try:
                arg_exprs = tuple(
                    self._resolve_call_kwargs(
                        arg_exprs,
                        kwargs,
                        ast_fd.args,
                        skip_self=True,
                    )
                )
            except L1CodegenError as e:
                span = None
                if arg_exprs:
                    span = getattr(arg_exprs[0], "span", None)
                where = (
                    f" at {span.file}:{span.line}:{span.col}"
                    if span is not None
                    else ""
                )
                raise L1CodegenError(f"{info.name}.{method_name}{where}: {e}") from e
        elif kwargs:
            raise NotImplementedError(
                f"method {info.name}.{method_name} with kwargs needs a "
                "FuncDef to resolve parameter names"
            )
        # Skip the receiver (``self`` / ``cls``) and the bare ``*``
        # kw-only separator when zipping against ``arg_exprs`` (which
        # ``_resolve_call_kwargs`` has already filtered).
        declared = [a for a in ast_fd.args[1:] if a.name != ""] if ast_fd else []
        if ast_fd is not None and ast_fd.is_async:
            values: list[tuple[ir.Value, Type]] = [
                (self_val, DynType(name="dyn")),
            ]
            for arg_expr in arg_exprs:
                values.append((self._emit_expr(arg_expr), arg_expr.ty))
            args_tuple = self._emit_object_tuple_from_values(
                tuple(values),
                name=f"{info.name}.{method_name}.async.args",
            )
            original_args = tuple(a for a in ast_fd.args if a.name != "")
            adapter = self._emit_native_func_adapter(
                f"{info.name}_{method_name}_async",
                method_fn,
                original_args,
                (),
                ast_fd.return_ty,
            )
            return self._emit_coroutine_from_adapter(
                f"{info.name}.{method_name}",
                adapter,
                args_tuple,
            )
        label = f"method {info.name}.{method_name}"
        receiver_provenance = _method_pin_arg_provenance(
            self,
            _method_managed_receiver_provenance(self, self_val),
        )
        emitted_args, _, arg_provenance = _method_emit_ast_args(
            self,
            method_fn,
            label,
            arg_exprs,
            declared,
            param_offset=1,
            initial_provenance=(receiver_provenance,),
        )
        args_ir.extend(emitted_args)
        ret_ty = method_fn.function_type.return_type
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"{info.name}.{method_name}.ret")
        )
        local_method_may_park = (
            ast_fd is not None
            and id(ast_fd)
            in getattr(self, "_vthread_may_park_method_ids", set())
        ) or info.name + "." + method_name in getattr(
            self,
            "_vthread_may_park_method_keys",
            set(),
        )
        external_method_may_park = method_name in getattr(
            info,
            "may_park_methods",
            set(),
        )
        method_may_park = local_method_may_park or external_method_may_park
        result = self._call_user(
            method_fn,
            args_ir,
            call_name,
            root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
            pinned_arg_temps=_method_pinned_arg_cleanup(arg_provenance),
        )
        if method_may_park:
            if park_expr is None:
                raise L1CodegenError(
                    "may_park method call has no continuation call site: "
                    + info.name
                    + "."
                    + method_name
                )
            effect_name = info.name + "." + method_name
            if external_method_may_park and getattr(info, "owning_module", None):
                effect_name = info.owning_module + "." + effect_name
            return emit_generator_may_park_child(
                self,
                park_expr,
                effect_name,
                result,
                arg_provenance,
            )
        _method_release_arg_provenance(self, arg_provenance)
        return result

    def _emit_static_method_ptr_call(
        self,
        method_ptr: ir.Value,
        method_fn: ir.Function,
        info,
        method_name: str,
        arg_exprs: tuple[Expr, ...],
        kwargs: tuple = (),
    ) -> ir.Value:
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        if ast_fd is not None:
            arg_exprs = tuple(
                self._resolve_call_kwargs(
                    arg_exprs,
                    kwargs,
                    ast_fd.args,
                )
            )
        elif kwargs:
            raise NotImplementedError(
                f"staticmethod {info.name}.{method_name} with kwargs "
                "needs a FuncDef to resolve parameter names"
            )
        declared = ast_fd.args if ast_fd else ()
        label = f"staticmethod {info.name}.{method_name}"
        args_ir, _, arg_provenance = _method_emit_ast_args(
            self,
            method_fn,
            label,
            arg_exprs,
            declared,
            param_offset=0,
        )
        callee = self.builder.bitcast(
            method_ptr,
            method_fn.type,
            name=self._fresh(f"{info.name}.{method_name}.super.fn"),
        )
        ret_ty = method_fn.function_type.return_type
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"{info.name}.{method_name}.ret")
        )
        result = self._call_user(
            callee,
            list(args_ir),
            call_name,
            root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
            pinned_arg_temps=_method_pinned_arg_cleanup(arg_provenance),
        )
        _method_release_arg_provenance(self, arg_provenance)
        return result

    def _emit_direct_method_ptr_call(
        self,
        method_ptr: ir.Value,
        method_fn: ir.Function,
        self_val: ir.Value,
        info,
        method_name: str,
        arg_exprs: tuple[Expr, ...],
        kwargs: tuple = (),
    ) -> ir.Value:
        args_ir: list[ir.Value] = [self_val]
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        if ast_fd is not None:
            arg_exprs = tuple(
                self._resolve_call_kwargs(
                    arg_exprs,
                    kwargs,
                    ast_fd.args,
                    skip_self=True,
                )
            )
        elif kwargs:
            raise NotImplementedError(
                f"method {info.name}.{method_name} with kwargs needs a "
                "FuncDef to resolve parameter names"
            )
        declared = [a for a in ast_fd.args[1:] if a.name != ""] if ast_fd else []
        label = f"method {info.name}.{method_name}"
        receiver_provenance = _method_pin_arg_provenance(
            self,
            _method_managed_receiver_provenance(self, self_val),
        )
        emitted_args, emitted_types, arg_provenance = _method_emit_ast_args(
            self,
            method_fn,
            label,
            arg_exprs,
            declared,
            param_offset=1,
            initial_provenance=(receiver_provenance,),
        )
        args_ir.extend(emitted_args)
        arg_values = list(zip(emitted_args, emitted_types))
        callee = self.builder.bitcast(
            method_ptr,
            method_fn.type,
            name=self._fresh(f"{info.name}.{method_name}.super.fn"),
        )
        ret_ty = method_fn.function_type.return_type
        if isinstance(ret_ty, (ir.PointerType, ir.VoidType)):
            is_func = self.builder.icmp_signed(
                "==",
                self.builder.call(
                    self.runtime["py_obj_type_tag"],
                    [method_ptr],
                    name=self._fresh(f"{info.name}.{method_name}.super.tag"),
                ),
                ir.Constant(ir.IntType(64), PY_TYPE_FUNC),
                name=self._fresh(f"{info.name}.{method_name}.super.is_func"),
            )
            parent_fn = self.current_function
            raw_bb = parent_fn.append_basic_block(
                name=self._fresh(f"{info.name}.{method_name}.super.raw")
            )
            func_bb = parent_fn.append_basic_block(
                name=self._fresh(f"{info.name}.{method_name}.super.pyfunc")
            )
            done_bb = parent_fn.append_basic_block(
                name=self._fresh(f"{info.name}.{method_name}.super.done")
            )
            self.builder.cbranch(is_func, func_bb, raw_bb)

            self.builder.position_at_end(func_bb)
            (
                pos_values,
                kw_values,
                var_pos_value,
                var_kw_value,
            ) = _method_unflatten_call_shape(declared, arg_values)
            slot_prefix = f"{info.name}.{method_name}.super.pyfunc"

            def _as_object(pair):
                return marshal.marshal_to_object(
                    self.builder, self.module, self.runtime, pair[0], pair[1]
                )

            full_args = self._emit_object_tuple_from_values(
                ((self_val, DynType(name="dyn")),) + tuple(pos_values),
                name=f"{slot_prefix}.args",
            )
            if var_pos_value is not None:
                spread = self.builder.call(
                    self.runtime["py_tuple_concat"],
                    [full_args, _as_object(var_pos_value)],
                    name=self._fresh(f"{slot_prefix}.args.spread"),
                )
                self._gc_release(full_args)
                full_args = spread
            kwargs_obj = None
            if kw_values or var_kw_value is not None:
                kwargs_obj = self.builder.call(
                    self.runtime["py_dict_new"],
                    [],
                    name=self._fresh(f"{slot_prefix}.kwargs"),
                )
                if var_kw_value is not None:
                    self.builder.call(
                        self.runtime["py_dict_update"],
                        [kwargs_obj, _as_object(var_kw_value)],
                    )
                for kw_name, kw_pair in kw_values:
                    self.builder.call(
                        self.runtime["py_dict_set"],
                        [
                            kwargs_obj,
                            self._emit_str_literal(kw_name),
                            _as_object(kw_pair),
                        ],
                    )
            func_ret_name = (
                ""
                if isinstance(ret_ty, ir.VoidType)
                else self._fresh(f"{slot_prefix}.ret")
            )
            if kwargs_obj is None:
                func_result = self.builder.call(
                    self.runtime["py_func_call"],
                    [method_ptr, full_args],
                    name=func_ret_name,
                )
            else:
                func_result = self.builder.call(
                    self.runtime["py_func_call_kwargs"],
                    [method_ptr, full_args, kwargs_obj],
                    name=func_ret_name,
                )
                self._gc_release(kwargs_obj)
            self._gc_release(full_args)
            self._emit_post_call_err_check(
                None,
                pinned_release_on_error=_method_pinned_arg_cleanup(
                    arg_provenance
                ),
            )
            if isinstance(ret_ty, ir.VoidType):
                self._gc_release(func_result)
            func_exit = self.builder.block
            self.builder.branch(done_bb)

            self.builder.position_at_end(raw_bb)
            raw_call_name = (
                ""
                if isinstance(ret_ty, ir.VoidType)
                else self._fresh(f"{info.name}.{method_name}.ret")
            )
            raw_result = self._call_user(
                callee,
                args_ir,
                raw_call_name,
                root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
                pinned_arg_temps=_method_pinned_arg_cleanup(arg_provenance),
            )
            raw_exit = self.builder.block
            self.builder.branch(done_bb)

            self.builder.position_at_end(done_bb)
            if isinstance(ret_ty, ir.VoidType):
                _method_release_arg_provenance(self, arg_provenance)
                return self._emit_none_literal()
            result = self.builder.phi(
                ret_ty,
                name=self._fresh(f"{info.name}.{method_name}.super.ret"),
            )
            result.add_incoming(func_result, func_exit)
            result.add_incoming(raw_result, raw_exit)
            _method_release_arg_provenance(self, arg_provenance)
            return result
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"{info.name}.{method_name}.ret")
        )
        result = self._call_user(
            callee,
            args_ir,
            call_name,
            root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
            pinned_arg_temps=_method_pinned_arg_cleanup(arg_provenance),
        )
        _method_release_arg_provenance(self, arg_provenance)
        return result

    def _emit_direct_method_value_call(
        self,
        method_fn: ir.Function,
        self_val: ir.Value,
        info,
        method_name: str,
        arg_values: tuple[tuple[ir.Value, Type], ...],
    ) -> ir.Value:
        args_ir: list[ir.Value] = [self_val]
        ast_fd = self.class_lowering._find_method_def(info.name, method_name)
        declared = [a for a in ast_fd.args[1:] if a.name != ""] if ast_fd else []
        if ast_fd is not None and ast_fd.is_async:
            values = ((self_val, DynType(name="dyn")),) + arg_values
            args_tuple = self._emit_object_tuple_from_values(
                values,
                name=f"{info.name}.{method_name}.async.args",
            )
            original_args = tuple(a for a in ast_fd.args if a.name != "")
            adapter = self._emit_native_func_adapter(
                f"{info.name}_{method_name}_async",
                method_fn,
                original_args,
                (),
                ast_fd.return_ty,
            )
            return self._emit_coroutine_from_adapter(
                f"{info.name}.{method_name}",
                adapter,
                args_tuple,
            )
        label = f"method {info.name}.{method_name}"
        arg_provenance = [
            _method_pin_arg_provenance(
                self,
                _method_managed_receiver_provenance(self, self_val),
            )
        ]
        for i, (v, v_ty) in enumerate(arg_values):
            declared_ty = declared[i].annotation if i < len(declared) else None
            param_ir_ty = _method_param_ir_type(
                self,
                method_fn,
                i + 1,
                label,
            )
            target_ty = _method_semantic_arg_type(declared_ty, param_ir_ty)
            v, newly_owned = _method_coerce_value_for_abi(
                self,
                v,
                v_ty,
                target_ty,
                param_ir_ty,
                label,
                i,
            )
            value_provenance = _method_pointer_provenance(
                self,
                v,
                v_ty,
                newly_owned=newly_owned,
            )
            arg_provenance.append(
                _method_pin_arg_provenance(self, value_provenance)
            )
            args_ir.append(v)
        ret_ty = method_fn.function_type.return_type
        call_name = (
            ""
            if isinstance(ret_ty, ir.VoidType)
            else self._fresh(f"{info.name}.{method_name}.ret")
        )
        result = self._call_user(
            method_fn,
            args_ir,
            call_name,
            root_result=ast_fd is not None and self._is_object(ast_fd.return_ty),
            pinned_arg_temps=_method_pinned_arg_cleanup(tuple(arg_provenance)),
        )
        _method_release_arg_provenance(self, tuple(arg_provenance))
        return result
