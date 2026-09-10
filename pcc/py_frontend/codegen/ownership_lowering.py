"""Owned-local and GC-root helper lowering for Layer-1 codegen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BinOp,
    BoolLit,
    BoolType,
    BytesLit,
    Call,
    ClassType,
    DictExpr,
    DictType,
    DynType,
    Expr,
    FloatType,
    IntType,
    ListExpr,
    ListType,
    Name,
    NoneLit,
    NoneType,
    SetType,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
)

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_CSTR = _I8.as_pointer()

_UNSAFE_RAW_POINTER_RETURNS = frozenset(
    {
        "malloc",
        "cstr",
        "global_addr",
        "function_addr",
        "global_load_ptr",
        "calloc",
        "realloc",
        "ptr_add",
        "stack_alloc",
        "int_to_ptr",
        "null",
        "tag_int",
        "load_ptr",
        "memset",
        "memcpy",
        "memmove",
        "getenv",
        "target_sys_platform",
        "target_platform_machine",
        "call_ptr1",
        "call_ptr0",
        "call_ptr2",
        "call_ptr4",
        "call_ptr3",
        "dynamic_library_open",
        "dynamic_library_open_global",
        "dynamic_library_symbol",
        "darwin_libsystem_symbol",
        "darwin_errno_location",
        "page_alloc",
    }
)


class OwnershipLoweringMixin:
    def _gc_retain(self, obj: ir.Value, name: str = "", known_object: bool = False) -> ir.Value:
        helper = "pcc_gc_retain"
        if self._known_object_refcounts and (known_object or self._value_is_owned_object(obj)):
            helper = "pcc_gc_retain_known"
        return self.builder.call(
            self.runtime[helper],
            [obj],
            name=name or self._fresh("gc.retain"),
        )

    def _release_context_label(self, kind: str) -> str:
        fn_name = "<module>"
        if self.current_func_def is not None:
            fn_name = self.current_func_def.name
        cls_name = getattr(self.current_class, "name", "")
        if cls_name:
            return f"{kind}:{cls_name}.{fn_name}"
        return f"{kind}:{fn_name}"

    def _release_expr_label(self, kind: str, expr: Expr) -> str:
        label = f"{self._release_context_label(kind)}:{type(expr).__name__}"
        span = getattr(expr, "span", None)
        if span is not None:
            label += f":{span.file}:{span.line}:{span.col}"
        return label

    def _debug_check_release(self, obj: ir.Value, label: str) -> None:
        if not self._debug_release_checks:
            return
        if "pcc_debug_check_release" not in self.runtime:
            return
        name_gv = self._cstr_global(
            label,
            self._fresh(".release.name"),
        )
        self.builder.call(
            self.runtime["pcc_debug_check_release"],
            [self._ptr_to_cstr(name_gv), obj],
        )

    def _note_never_gc_object(self, value: ir.Value) -> None:
        """Record a value that provably cannot be a GC-managed object.

        Two kinds qualify:

        * materialized tagged small ints -- `(v << 1) | 1` reinterpreted as a
          pointer, an immediate rather than a heap object;
        * the immortal singletons `py_None` / `py_True` / `py_False`, which
          carry `PY_FLAG_IMMORTAL` and are never collected.

        Both make `pcc_gc_pin` / `unpin` / `release` no-ops at run time (each
        starts by testing for a tagged immediate or an immortal flag and
        returning), so emitting the call buys nothing and still costs the GC
        managed-pointer probe that profiles as a top leaf of a stage2 build.
        Measured over 60 real modules, singleton-sourced barriers are ~3800 of
        79766 barrier arguments.
        Membership is tracked by identity rather than inspected from the IR
        so the test stays exact — a wrong answer here would drop a real
        barrier.
        """
        if not hasattr(self, "_never_gc_object_values"):
            self._never_gc_object_values = set()
        self._never_gc_object_values.add(value)

    def _value_is_never_gc_object(self, value: ir.Value) -> bool:
        return value in getattr(self, "_never_gc_object_values", ())

    def _gc_pin(self, obj: ir.Value) -> None:
        # pcc_gc_pin returns immediately for a tagged immediate, so emitting
        # the call for one is pure overhead — and it also spares the GC
        # managed-pointer table the lookup, which profiles as the hottest
        # leaf of a `pcc1 -> pcc2` build.
        if self._value_is_never_gc_object(obj):
            return
        self.builder.call(self.runtime["pcc_gc_pin"], [obj])

    def _gc_unpin(self, obj: ir.Value) -> None:
        if self._value_is_never_gc_object(obj):
            return
        self.builder.call(
            self.runtime["pcc_gc_unpin"],
            [self._value_available_at_insertion_point(obj)],
        )

    def _note_global_backed_value(self, value: ir.Value, source) -> None:
        """Record that ``value`` was loaded from module-level ``source``.

        The entry keeps a reference to ``value`` itself so the ``id`` key can
        never be reused by a freed object (the recorded id-keyed cache hazard).
        """
        table = getattr(self, "_global_backed_values", None)
        if table is None:
            table = {}
            self._global_backed_values = table
        table[id(value)] = (source, value)

    def _value_available_at_insertion_point(self, value: ir.Value) -> ir.Value:
        """Return ``value``, re-deriving it here when it may not dominate.

        A cleanup site can be reached from a resume block that the defining
        block does not dominate (may_park state machines split a function at
        every park).  For a value loaded from a module-level global -- a class
        object receiver, for instance -- the load is pure and the global is
        stable for the image lifetime, so re-emitting it here is exact.
        """
        table = getattr(self, "_global_backed_values", None)
        if not table:
            return value
        entry = table.get(id(value))
        if entry is None or entry[1] is not value:
            return value
        return self.builder.load(entry[0], name=self._fresh("gc.reload"))

    def _gc_release(self, obj: ir.Value, label: Optional[str] = None, known_object: bool = False) -> None:
        if getattr(self, "_freestanding_module", False):
            # A freestanding module has no allocator, no GC service and no
            # managed object, so a release here is not merely unnecessary: the
            # emitted-IR verification rejects any managed-runtime reference in
            # such a module, which is how this surfaced. `bzero` discards the
            # `c_ptr` that `memset` returns; the discarded pointer was
            # classified as an owned object, and the release it emitted made
            # `freestanding_mem_str.py` -- and therefore every from-scratch
            # runtime rebuild -- fail to compile. Not emitting the call makes
            # that error impossible by construction rather than diagnosed
            # after the fact.
            return
        if self._value_is_never_gc_object(obj):
            # pcc_gc_release starts with `ptr_is_null(o) or is_tagged_int(o)`
            # and returns, so this call is a no-op for a tagged immediate.
            return
        self._debug_check_release(
            obj,
            label or self._release_context_label("release"),
        )
        helper = "pcc_gc_release"
        if self._known_object_refcounts and (known_object or self._value_is_owned_object(obj)):
            helper = "pcc_gc_release_known"
        self.builder.call(self.runtime[helper], [obj])

    def _note_owned_object_value(self, value: ir.Value) -> None:
        """Record an emitted pcc object value that carries one owner.

        Expression shape is insufficient at representation joins: an IfExpr
        can select a retained borrowed local on one edge and a fresh call
        result on the other.  The emitting Module normalizes those edges and
        records the resulting SSA value here so assignment and borrowing
        consumers transfer/release the actual owner instead of guessing from
        the outer AST node.
        """
        self._owned_dynamic_call_values.add(value)

    def _value_is_owned_object(self, value: ir.Value) -> bool:
        if getattr(self, "_freestanding_module", False):
            return False
        return value in self._owned_dynamic_call_values

    def _note_owned_dynamic_call_value(self, value: ir.Value) -> None:
        """Record that *value* came from ``py_obj_call`` on the dynamic path.

        The expression-shape classifier below cannot decide this: an
        ``obj.method()`` on a DynType receiver may be intercepted by any of a
        dozen native emitters that return borrowed values, or fall through to
        generic dispatch.  Only the emitter knows which happened, and
        ``py_obj_call`` unconditionally returns a NEW reference, so the emitter
        records the value here instead of the classifier guessing from the AST.
        Mirrors the existing ``_cpy_values`` / ``_owned_cpy_values`` split.
        """
        self._note_owned_object_value(value)

    def _value_is_owned_dynamic_call(self, value: ir.Value) -> bool:
        return self._value_is_owned_object(value)

    def _owned_release_needed(self, obj: ir.Value, source_expr: Expr) -> bool:
        """True when ``_gc_release_if_owned(obj, source_expr)`` emits a release.

        Callers that root a fresh result only so a following release cannot
        pull it from under a relocating collector ask this first: when no
        release follows, the root frame is pure protocol.
        """
        if obj is None or not isinstance(obj.type, ir.PointerType):
            return False
        if obj in getattr(self, "_cpy_values", ()):
            return False
        if self._value_is_owned_object(obj):
            return True
        if not self._raw_scaffold_object_rhs_is_owned(source_expr):
            return False
        return self._expr_returns_owned_object(source_expr)

    def _gc_release_if_owned(self, obj: ir.Value, source_expr: Expr) -> None:
        if not self._owned_release_needed(obj, source_expr):
            return
        # An emitter-recorded owner is unconditionally owned and not inferable
        # from the AST shape.  Discarding this reference is what leaked the
        # whole result object of every dynamic method call used as a statement.
        label = "owned.emitted" if self._value_is_owned_object(obj) else "owned"
        self._gc_release(obj, self._release_expr_label(label, source_expr))

    def _native_re_call_returns_owned_object(self, expr) -> bool:
        """True when *expr* is a call the native ``re`` lowering will emit.

        Every ``py_re_*`` runtime helper returns a NEW reference (or the
        immortal ``py_None``, for which a release is a no-op), but the
        classifier below saw only a DynType Call and answered "not owned", so
        no release was ever emitted: **every native re match leaked its whole
        Match object** — instance, six method funcs, six captures tuples, six
        name strings.  A 300k-iteration ``pat.match(...)`` loop leaked 1.76 GB,
        and the self-backend IR parser runs one match per operand, which is
        what drove an oversized stage2 emit worker past 24 GB.

        The shape conditions here MUST mirror the native emitters'
        applicability gates in ``native_text_modules``: if the emitter falls
        through to a generic path, this must answer False.
        """
        if expr.kwargs:
            return False
        func = expr.func
        nargs = len(expr.args)
        if isinstance(func, Attr) and isinstance(func.obj, Name):
            root = func.obj.ident
            if self._native_builtin_module_for_name(root) == "re":
                if func.name in ("match", "search", "fullmatch"):
                    return 2 <= nargs <= 3
                if func.name == "findall":
                    return 2 <= nargs <= 3
                if func.name == "sub":
                    return 3 <= nargs <= 4
                if func.name == "escape":
                    return nargs == 1
                # ``re.split`` and ``re.compile`` have emitter gates that
                # depend on evaluated flag/pattern constants and can fall
                # through to generic lowering; classifying them here could
                # over-release a borrowed fallback result.  They are cold
                # (module scope) next to the per-operand match calls.
                return False
            if (
                func.name in ("match", "search", "findall")
                and nargs == 1
                and self._native_re_compile_alias_for_name(root) is not None
            ):
                return True
            return False
        if isinstance(func, Name):
            kind = self._native_builtin_value_for_name(func.ident)
            if kind in ("re.match", "re.search", "re.fullmatch"):
                return 2 <= nargs <= 3
        return False

    def _expr_returns_owned_object(self, expr: Expr) -> bool:
        if getattr(self, "_freestanding_module", False):
            # Every pointer in a freestanding module is raw: the module
            # contract gives it no allocator, no GC service and no managed
            # object, and the emitted-IR verification rejects any
            # managed-runtime reference in one. Answering "owned" here is what
            # made `found = pcc_strchrnul(...)` in `strchr` look like an owner
            # and drove the whole owned-assignment sequence -- root frame,
            # slot load, release -- into a module that cannot link any of it.
            # `_UNSAFE_RAW_POINTER_RETURNS` below only names unsafe
            # intrinsics, so it can never cover a module's own c_abi_exports;
            # this is the one place that decides for all of them.
            return False
        if self._expr_returns_unsafe_raw_pointer(expr):
            return False
        expr_ty = getattr(expr, "ty", None)
        if expr_ty is not None and self._is_valueclass_payload_type(expr_ty):
            return True
        if isinstance(expr, Call):
            native_call = self._native_builtin_value_kind_for_expr(expr.func)
            if (
                native_call == "os._pcc_sha256_file_hex"
                or native_call == "os._pcc_sha256_file_hex_bounded"
            ):
                return True
            if (
                native_call == "pcc.virtual_thread.spawn"
                or native_call == "pcc.virtual_thread.continuation"
                or native_call == "pcc.virtual_thread.completed"
                or native_call == "pcc.virtual_thread.call"
                or native_call == "pcc.virtual_thread.join"
                or native_call == "pcc.virtual_thread.current"
                or native_call == "pcc.virtual_thread.result"
                or native_call == "pcc.virtual_thread.exception"
                or native_call == "pcc.virtual_thread.mpsc"
                or native_call == "pcc.virtual_thread.oneshot"
                or native_call == "pcc.virtual_thread.sender_clone"
                or native_call == "pcc.virtual_thread.recv"
                or native_call == "pcc.virtual_thread.select2"
                or native_call == "pcc.virtual_thread.tcp_recv"
            ):
                # These intrinsics return a new/retained object reference even
                # though their public Any result is represented as DynType.
                # Keep scalar/None vthread operations out of this list: cancel,
                # send, close, outcome, state, sleep and block_on_fd do not
                # transfer an object owner to their caller.  Join, channel
                # constructors/clone and receive/select result tuples do.
                return True
            if self._weakref_constructor_kind_for_expr(expr) is not None:
                return True
            if self._weakref_call_expr_returns_owned_object(expr):
                return True
            if self._native_re_call_returns_owned_object(expr):
                return True
            if isinstance(expr.func, Name) and expr.func.ident == "__await__":
                return True
            if isinstance(expr_ty, (NoneType, BoolType, IntType, FloatType)):
                return False
            if expr_ty is not None and not isinstance(expr_ty, DynType):
                return self._is_object(expr_ty)
            if isinstance(expr.func, Name) and expr.func.ident == "_dataclass_field_value":
                return False
            if isinstance(expr.func, Attr):
                if self._builtin_container_method_returns_owned_object(expr):
                    return True
                method_ret_ty = self._method_call_return_type(expr)
                if method_ret_ty is not None:
                    return self._user_return_type_is_owned_object(method_ret_ty)
            if isinstance(expr.func, Name):
                callee = expr.func.ident
                if (
                    hasattr(self, "class_lowering")
                    and callee in self.class_lowering.classes
                ):
                    return True
                if callee in getattr(self, "functions", {}):
                    try:
                        ast_func_def = self._find_user_funcdef(callee)
                    except Exception:
                        ast_func_def = None
                    if ast_func_def is not None:
                        if callee in getattr(
                            self, "_generator_func_names", set()
                        ) or self._funcdef_has_yield_sentinel(ast_func_def):
                            return True
                        return self._user_return_type_is_owned_object(
                            ast_func_def.return_ty
                        )
                    return False
                env_entry = self.env.get(callee)
                if env_entry is not None and len(env_entry) >= 3:
                    callable_ty = env_entry[2]
                    if self._is_object(callable_ty):
                        return True
                if callee in {
                    "bytes",
                    "dict",
                    "frozenset",
                    "list",
                    "set",
                    "str",
                    "tuple",
                }:
                    return True
                if callee == "next" and len(expr.args) == 1:
                    return True
            func_ty = getattr(expr.func, "ty", None)
            if isinstance(func_ty, ClassType):
                return True
            return False
        if isinstance(expr, Name) and self._name_returns_owned_function_value(
            expr.ident
        ):
            return True
        if isinstance(
            expr,
            (
                ListExpr,
                DictExpr,
                TupleExpr,
                StrLit,
                BytesLit,
                Subscript,
            ),
        ):
            return True
        if isinstance(expr, BinOp) and isinstance(
            expr.ty,
            (StrType, ListType, TupleType, DynType),
        ):
            return True
        if isinstance(expr, Attr):
            return self._attr_expr_returns_owned_object(expr)
        return False

    def _builtin_container_method_returns_owned_object(
        self, expr: Call
    ) -> bool:
        """True for builtin list/dict/set methods whose native lowering returns
        a NEW owned reference of DynType.

        The shape classifier answers "not owned" for a DynType method-call
        result, so these were never released: `x = box.pop()` / `d.get(k)` /
        `s.pop()` leaked their whole result object (delayed or missed
        finalization). Every method listed here returns a uniformly owned ref
        on ALL edges -- verified against the runtime and the native lowerings:
        py_list_pop transfers owned; py_dict_get_default increfs its default
        (so dict.get is owned on hit AND miss); dict.setdefault increfs the
        default on its miss edge; py_dict_pop/py_dict_popitem/py_set_pop
        transfer owned. `dict.pop(k, default)` is DELIBERATELY EXCLUDED: its
        two-arg lowering returns the BORROWED default on the miss edge, so
        classifying it owned would over-release the caller's default. The gates
        (receiver type + name + arg count) mirror the native emitters' own
        applicability so this never claims owned for a path that fell through
        to generic borrowed dispatch.
        """
        func = expr.func
        if not isinstance(func, Attr):
            return False
        if getattr(expr, "kwargs", None):
            return False
        recv_ty = getattr(func.obj, "ty", None)
        name = func.name
        nargs = len(expr.args)
        if isinstance(recv_ty, ListType):
            return name == "pop" and nargs in (0, 1)
        if isinstance(recv_ty, DictType):
            if name == "get" and nargs in (1, 2):
                return True
            if name == "setdefault" and nargs in (1, 2):
                return True
            if name == "pop" and nargs == 1:
                return True
            if name == "popitem" and nargs == 0:
                return True
            return False
        if isinstance(recv_ty, SetType):
            return name == "pop" and nargs == 0
        return False

    def _return_type_is_owned_object(self, ty: Optional[Type]) -> bool:
        if ty is None:
            return False
        if isinstance(ty, (NoneType, BoolType, IntType, FloatType, DynType)):
            return False
        return self._is_object(ty)

    def _user_return_type_is_owned_object(self, ty: Optional[Type]) -> bool:
        if ty is None or isinstance(ty, DynType):
            return True
        return self._return_type_is_owned_object(ty)

    def _method_call_return_type(self, expr: Call) -> Optional[Type]:
        func = expr.func
        if not isinstance(func, Attr):
            return None
        receiver_hint = self._class_hint_for_expr(func.obj)
        if receiver_hint is None:
            receiver_ty = getattr(func.obj, "ty", None)
            if isinstance(receiver_ty, ClassType):
                receiver_hint = self._ensure_class_type_registered(receiver_ty)
                if receiver_hint is None:
                    receiver_hint = receiver_ty.name
        if receiver_hint is None:
            return None
        owner_info = self._resolve_method_mro(receiver_hint, func.name)
        if owner_info is None:
            owner_info = self.class_lowering.classes.get(receiver_hint)
        if owner_info is None:
            return None
        fd = self.class_lowering._find_method_def(owner_info.name, func.name)
        if fd is None:
            return None
        return fd.return_ty

    def _attr_expr_returns_owned_object(self, expr: Attr) -> bool:
        """Return true for attribute loads known to produce a new ref."""
        obj_ty = getattr(expr.obj, "ty", None)
        if self._is_valueclass_payload_type(obj_ty):
            return False
        if isinstance(expr.obj, Name):
            slot = self.env.get(expr.obj.ident)
            if slot is not None:
                _alloca, _ir_ty, declared_ty = slot
                if self._is_valueclass_payload_type(declared_ty):
                    return False
            if hasattr(self, "class_lowering") and expr.name is not None:
                hinted = self.env_class_hint.get(expr.obj.ident)
                if hinted is not None:
                    info = self.class_lowering.classes.get(hinted)
                    if (
                        info is not None
                        and info.valueclass
                        and expr.name in info.field_names
                    ):
                        return True
                else:
                    for info in self.class_lowering.classes.values():
                        if (
                            info.valueclass
                            and expr.name in info.field_names
                        ):
                            return True
            hint = self._class_hint_for_expr(expr.obj)
            if hint is not None:
                property_owner = self._resolve_property_mro(hint, expr.name)
                if property_owner is not None:
                    getter_def = self.class_lowering._find_method_def(
                        property_owner.name,
                        expr.name,
                    )
                    if getter_def is None:
                        return True
                    return self._user_return_type_is_owned_object(
                        getter_def.return_ty
                    )

        current_class = self.current_class
        if (
            current_class is not None
            and isinstance(expr.obj, Name)
            and expr.obj.ident == "self"
        ):
            field_idx = self.class_lowering.lookup_field_index(current_class, expr.name)
            if field_idx is not None:
                field_ty = getattr(current_class, "field_types", {}).get(expr.name)
                if field_ty is None or isinstance(field_ty, DynType):
                    return False
                return self._is_object(field_ty)
            if (
                self.class_lowering.lookup_class_attr(current_class, expr.name)
                is not None
            ):
                return False

        if isinstance(expr.obj, Name):
            hint = self._class_hint_for_expr(expr.obj)
            if hint is not None:
                class_info = self.class_lowering.classes.get(hint)
                if (
                    class_info is not None
                    and self.class_lowering.lookup_field_index(class_info, expr.name)
                    is not None
                ):
                    field_ty = getattr(class_info, "field_types", {}).get(expr.name)
                    if field_ty is None or isinstance(field_ty, DynType):
                        return False
                    return self._is_object(field_ty)
        if isinstance(expr.obj, (Attr, Call, Subscript)):
            return True
        return False

    def _name_returns_owned_function_value(self, ident: str) -> bool:
        if ident in self.env:
            return False
        if ident in getattr(self, "_module_globals", {}):
            return False
        if ident in self.functions:
            return True
        direct_hoist = f"__nested_{ident}"
        if direct_hoist in self.functions:
            return True
        matches = [
            name for name in self.functions if name.startswith(f"{direct_hoist}_")
        ]
        return len(matches) == 1

    def _expr_returns_unsafe_raw_pointer(self, expr: Expr) -> bool:
        if not isinstance(expr, Call):
            return False
        if not isinstance(expr.func, Name):
            return False
        intrinsic = self._unsafe_intrinsic_for_name(expr.func.ident)
        return intrinsic in _UNSAFE_RAW_POINTER_RETURNS

    def _container_store_temp_needs_release(
        self,
        expr: Expr,
        value_ty: Type,
        is_cpy: bool,
        value: Optional[ir.Value] = None,
    ) -> bool:
        if is_cpy:
            return True
        if isinstance(expr, (BoolLit, NoneLit, StrLit)):
            # Literal strings are internal immortal globals; bool and None
            # box/load the runtime's immortal singleton globals.  A container
            # borrows those stable addresses, so there is no fresh temporary
            # owner to consume.
            return False
        # Native method dispatch may prove a NEW result even when inference
        # leaves the expression dynamic, and only the emitter knows it. Kept
        # to the case its own reasoning describes -- a DYNAMIC expression --
        # because as an unconditional first answer it also overrode every
        # deliberate False below. That over-released a container literal's
        # value when the store had already consumed the owner: a module
        # registering itself as `{"source": <self>}` then reached refcount 0
        # while its own slot still pointed at it, and `py_dealloc_dict` tripped
        # the fail-closed `pcc_debug_bad_dict_slot` guard on every from-scratch
        # runtime build.
        if (
            value is not None
            and isinstance(value_ty, DynType)
            and self._value_is_owned_object(value)
        ):
            return True
        if self._expr_returns_owned_object(expr):
            return True
        if isinstance(value_ty, IntType) and isinstance(expr, Name):
            if self._int_expr_needs_exact_object_boundary(expr):
                # Exact-int locals are pointer-form borrowed loads.  Container
                # stores retain them just like every other borrowed object;
                # only the freshly boxed/raw scalar and fresh exact-expression
                # lanes below need a balancing release.
                return False
            # Any other name whose slot already holds an object pointer -- an
            # ``int`` parameter above all -- is a borrowed load too.  Treating
            # it as a fresh box released ``a`` after ``[a, a]`` appended it: a
            # no-op for a tagged small int, an over-release that freed the
            # caller's bignum while the list still referenced it.
            slot = self.env.get(expr.ident)
            if slot is not None:
                return not isinstance(slot[1], ir.PointerType)
            module_global = self._module_globals.get(expr.ident)
            if module_global is not None:
                return not isinstance(module_global[0].value_type, ir.PointerType)
        return isinstance(value_ty, (IntType, FloatType, BoolType, NoneType))

    def _pcc_pointer_source_needs_pin(self, expr: Expr) -> bool:
        """Whether a native pointer result can move during later evaluation.

        String literals are emitted as internal PY_FLAG_IMMORTAL globals;
        bool and None literals resolve to the runtime's immortal singleton
        globals.  Their addresses are fixed for the image lifetime, so
        pinning each occurrence only duplicates cleanup IR.  Every other
        pointer source remains conservative: dynamic strings and all heap
        objects still pin across allocation/safepoint-capable operations.
        """
        return not isinstance(expr, (BoolLit, NoneLit, StrLit))

    def _pcc_pointer_source_is_owned(self, expr: Expr) -> bool:
        """Whether a pointer-form expression result carries a fresh pcc ref.

        Most semantic ``int`` expressions use the raw i64 lane, so the generic
        object-ownership classifier intentionally returns false for them.
        Exact-int boundaries are different: literals/operations/calls produce
        fresh bignum objects, while an exact-int ``Name`` loads a borrowed ref
        from its rooted local slot.
        """
        if not self._raw_scaffold_object_rhs_is_owned(expr):
            return False
        if self._expr_returns_owned_object(expr):
            return True
        if isinstance(expr.ty, (IntType, BoolType)) and not isinstance(
            expr,
            Name,
        ):
            # This predicate is used only after codegen has observed an actual
            # pointer value.  In that representation, a non-Name int/bool
            # expression is a fresh boxed object even when its value would fit
            # the raw scalar lane.  Exact-int Names remain borrowed local loads.
            return True
        return False

    def _raw_scaffold_object_rhs_is_owned(self, expr: Expr) -> bool:
        if not self._module_uses_raw_int_scaffold:
            return True
        if isinstance(expr, Call):
            # The raw integer ABI does not turn Python regex results into
            # borrowed pointers. Keep the same emitter-proven NEW-ref rule
            # used below for ordinary modules (including immortal None).
            if self._native_re_call_returns_owned_object(expr):
                return True
            native_call = self._native_builtin_value_kind_for_expr(expr.func)
            if (
                native_call == "os._pcc_sha256_file_hex"
                or native_call == "os._pcc_sha256_file_hex_bounded"
            ):
                return True
            if (
                native_call == "pcc.virtual_thread.spawn"
                or native_call == "pcc.virtual_thread.continuation"
                or native_call == "pcc.virtual_thread.completed"
                or native_call == "pcc.virtual_thread.call"
                or native_call == "pcc.virtual_thread.join"
                or native_call == "pcc.virtual_thread.current"
                or native_call == "pcc.virtual_thread.result"
                or native_call == "pcc.virtual_thread.exception"
                or native_call == "pcc.virtual_thread.mpsc"
                or native_call == "pcc.virtual_thread.oneshot"
                or native_call == "pcc.virtual_thread.sender_clone"
                or native_call == "pcc.virtual_thread.recv"
                or native_call == "pcc.virtual_thread.select2"
            ):
                # Mirror _expr_returns_owned_object even in C-ABI-exporting
                # raw-scaffold modules.  These native calls still return an
                # ordinary owned PyObject pointer whose local owner must be
                # consumed on rebind/exit.
                return True
        # Runtime ports / bootstrap modules that export C ABI symbols
        # usually manage object refs by hand. For ordinary user scripts
        # that import pcc.extern just to reach native APIs, track only
        # object-producing expressions we can identify with reasonable
        # confidence.
        if isinstance(expr, Subscript):
            receiver_ty = getattr(expr.obj, "ty", None)
            if isinstance(receiver_ty, (ListType, TupleType, DictType)):
                # Exact-container public getitem helpers return a NEW ref even
                # when a C-ABI/raw-scaffold module otherwise applies its
                # conservative result-type filter.  The exact receiver shape
                # is the ownership proof for Dyn and object projections alike.
                return True
        if self._module_has_c_abi_export:
            return isinstance(expr, (ListExpr, DictExpr, TupleExpr, StrLit, BytesLit))
        if isinstance(expr, (ListExpr, DictExpr, TupleExpr, StrLit, BytesLit)):
            return True
        if isinstance(expr, Subscript):
            return self._return_type_is_owned_object(getattr(expr, "ty", None))
        if self._expr_returns_unsafe_raw_pointer(expr):
            return False
        if not isinstance(expr, Call):
            return self._expr_returns_owned_object(expr)
        if self._weakref_constructor_kind_for_expr(
            expr
        ) is not None or self._weakref_call_expr_returns_owned_object(expr):
            return True
        if isinstance(expr.func, Attr):
            expr_ty = getattr(expr, "ty", None)
            return self._return_type_is_owned_object(expr_ty)

        if isinstance(expr.func, Name):
            callee = expr.func.ident
            if callee == "next" and len(expr.args) == 1:
                return True
            if (
                hasattr(self, "class_lowering")
                and callee in self.class_lowering.classes
            ):
                return True
            if callee in getattr(self, "functions", {}):
                return self._expr_returns_owned_object(expr)

        func_ty = getattr(expr.func, "ty", None)
        if isinstance(func_ty, ClassType):
            # Class constructors always produce Python objects.
            return True

        if isinstance(expr.func, Name) and expr.func.ident in {
            "list",
            "dict",
            "tuple",
            "set",
            "frozenset",
            "str",
            "bytes",
        }:
            # Keep this in sync with the builtin-constructor set in
            # _expr_returns_owned_object: omitting one (str was missing
            # until 2026-06-12) makes raw-scaffold modules skip
            # release-on-rebind for that constructor's results and leak
            # one object per call.
            return True

        return False

    def _valueclass_payload_expr_fields_are_owned(self, expr: Expr) -> bool:
        expr_ty = getattr(expr, "ty", None)
        if expr_ty is None or not self._is_valueclass_payload_type(expr_ty):
            return False
        if not isinstance(expr, Call) or not isinstance(expr.func, Name):
            return False
        if not hasattr(self, "class_lowering"):
            return False
        class_name = self._resolve_class_alias(expr.func.ident)
        info = self.class_lowering.classes.get(class_name)
        return info is not None and bool(getattr(info, "valueclass", False))

    def _mark_owned_local_if_object(
        self,
        name: str,
        ir_ty: ir.Type,
        expr: Optional[Expr] = None,
    ) -> None:
        if self.current_func_def is None:
            return
        if expr is not None and not self._raw_scaffold_object_rhs_is_owned(expr):
            return
        if name in getattr(self, "_current_global_names", set()):
            return
        if name in getattr(self, "_current_param_names", set()):
            return
        if not isinstance(ir_ty, ir.PointerType):
            return
        if not self._ir_type_matches(ir_ty, _CSTR):
            return
        if expr is not None and not self._expr_returns_owned_object(expr):
            return
        self._owned_local_names.add(name)
        slot = self.env.get(name)
        if slot is not None:
            alloca, _slot_ir_ty, _decl_ty = slot
            self._ensure_owned_local_gc_root(name, alloca, ir_ty)

    def _as_gc_ptr(
        self,
        value: ir.Value,
        *,
        name: str = "",
    ) -> ir.Value:
        if value.type == _CSTR:
            return value
        cast_name = name
        if cast_name == "":
            cast_name = self._fresh("gc.ptr")
        return self.builder.bitcast(
            value,
            _CSTR,
            name=cast_name,
        )

    def _emit_entry_gc_frame_enter(
        self,
        frame_map: ir.Value,
        slots: ir.Value,
    ) -> None:
        # Reuse self.builder rather than constructing a local
        # ``tmp_builder = ir.IRBuilder(entry)``: under pcc-py self-host
        # the local-variable IRBuilder alias didn't reliably register in
        # ``_ir_builder_env_flags``, causing ``tmp_builder.bitcast(...)``
        # to fall through scaffold dispatch and pick ``Value.bitcast``
        # (1 positional arg) instead of the 3-arg IRBuilder.bitcast,
        # producing "Value.bitcast: too many positional args" during the
        # pcc1→pcc2 stage. Save/restore the builder's insertion point
        # around the entry-block emission.
        saved_block = self.builder._block
        self._position_at_entry_hoist_point()
        frame_map_ptr = frame_map
        if frame_map_ptr.type != _CSTR:
            frame_map_ptr = self.builder.bitcast(
                frame_map,
                _CSTR,
                name=self._fresh("gc.frame.map.ptr"),
            )
        slots_ptr = slots
        if slots_ptr.type != _CSTR:
            slots_ptr = self.builder.bitcast(
                slots,
                _CSTR,
                name=self._fresh("gc.frame.slots.ptr"),
            )
        self.builder.call(
            self.runtime["pcc_gc_frame_enter"],
            [frame_map_ptr, slots_ptr],
        )
        self.builder.position_at_end(saved_block)

    def _emit_current_gc_frame_enter(
        self,
        frame_map: ir.Value,
        slots: ir.Value,
    ) -> None:
        frame_map_ptr = frame_map
        if frame_map_ptr.type != _CSTR:
            frame_map_ptr = self.builder.bitcast(
                frame_map,
                _CSTR,
                name=self._fresh("gc.frame.map.ptr"),
            )
        slots_ptr = slots
        if slots_ptr.type != _CSTR:
            slots_ptr = self.builder.bitcast(
                slots,
                _CSTR,
                name=self._fresh("gc.frame.slots.ptr"),
            )
        self.builder.call(
            self.runtime["pcc_gc_frame_enter"],
            [frame_map_ptr, slots_ptr],
        )

    def _emit_current_gc_frame_enter_lifo(
        self,
        frame_map: ir.Value,
        slots: ir.Value,
    ) -> None:
        frame_map_ptr = frame_map
        if frame_map_ptr.type != _CSTR:
            frame_map_ptr = self.builder.bitcast(
                frame_map,
                _CSTR,
                name=self._fresh("gc.frame.lifo.map.ptr"),
            )
        slots_ptr = slots
        if slots_ptr.type != _CSTR:
            slots_ptr = self.builder.bitcast(
                slots,
                _CSTR,
                name=self._fresh("gc.frame.lifo.slots.ptr"),
            )
        self.builder.call(
            self.runtime["pcc_gc_frame_enter_lifo"],
            [frame_map_ptr, slots_ptr],
        )

    def _gc_one_slot_frame_map(self) -> ir.GlobalVariable:
        name = ".pcc.gc.frame.map.1"
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        gv = ir.GlobalVariable(self.module, _I32, name=name)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(_I32, 1)
        return gv

    def _gc_one_slot_borrowed_frame_map(self) -> ir.GlobalVariable:
        name = ".pcc.gc.frame.map.borrowed.1"
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        gv = ir.GlobalVariable(self.module, _I32, name=name)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(_I32, -1)
        return gv

    def _ensure_owned_local_gc_root(
        self,
        name: str,
        alloca: ir.Value,
        ir_ty: ir.Type,
    ) -> None:
        if self.current_func_def is None:
            return
        if name in getattr(self, "_current_global_names", set()):
            return
        if (
            name in getattr(self, "_current_param_names", set())
            and not getattr(self, "_exact_int_env_flags", {}).get(name, False)
            and name not in getattr(self, "_for_target_owned_names", set())
        ):
            return
        self._ensure_local_gc_frame_root(
            name,
            alloca,
            ir_ty,
            self._gc_one_slot_frame_map(),
        )

    def _ensure_borrowed_local_gc_root(
        self,
        name: str,
        alloca: ir.Value,
        ir_ty: ir.Type,
    ) -> None:
        self._ensure_local_gc_frame_root(
            name,
            alloca,
            ir_ty,
            self._gc_one_slot_borrowed_frame_map(),
        )
        if name in getattr(self, "_gc_rooted_local_names", set()):
            if not hasattr(self, "_borrowed_gc_rooted_local_names"):
                self._borrowed_gc_rooted_local_names = set()
            self._borrowed_gc_rooted_local_names.add(name)

    def _ensure_local_gc_frame_root(
        self,
        name: str,
        alloca: ir.Value,
        ir_ty: ir.Type,
        frame_map: ir.Value | None = None,
    ) -> None:
        if getattr(self, "_freestanding_module", False):
            # Freestanding modules have no GC service, so there is nothing for
            # a root frame to report to and every pointer in them is raw. The
            # emitted-IR verification rejects any managed-runtime reference in
            # such a module, and this is where that came from: a `c_ptr`
            # returned by a sibling export and bound to a local (`found =
            # pcc_strchrnul(...)` in `strchr`) looked like a managed object, so
            # `pcc_gc_frame_enter` was emitted and `freestanding_mem_str.py`
            # stopped compiling. `_UNSAFE_RAW_POINTER_RETURNS` only names
            # unsafe intrinsics, so it cannot cover a module's own exports.
            return
        if self.current_func_def is None:
            return
        if name in getattr(self, "_current_global_names", set()):
            return
        if not isinstance(ir_ty, ir.PointerType):
            return
        if not self._ir_type_matches(ir_ty, _CSTR):
            return
        fn = self.current_function
        if fn is None:
            return
        # Registration is ALLOCA-keyed per function, not name-keyed. A local
        # name can be re-bound to a fresh alloca inside one function (scope
        # pop -> new slot); the old name-keyed dedup skipped frame_enter for
        # the new alloca while owned-flag management kept releasing through
        # it. Moving backends (#3/#4) then never healed that slot during
        # relocation remap, and the flag-guarded release decref'd a stale
        # pre-relocation pointer once the source memory was reused
        # (pproxy pcc1 worker BAD_INCREF, gc3/gc4).
        #
        # Dedup by OBJECT IDENTITY, never by value-name strings: alloca
        # name uniquification timing differs between the host compiler and
        # the self-hosted stages, and a name-string key made pcc1 emit a
        # different enter/leave count than pcc0/pcc2 (pcc2/pcc3 byte drift).
        if not hasattr(self, "_fn_gc_root_slot_registry"):
            self._fn_gc_root_slot_registry = {}
        registry = self._fn_gc_root_slot_registry.setdefault(fn.name, [])
        for entry in registry:
            if entry[1] is alloca:
                if name not in getattr(self, "_gc_rooted_local_names", set()):
                    self._gc_rooted_local_names.add(name)
                return
        if frame_map is None:
            frame_map = self._gc_one_slot_frame_map()
        self._emit_entry_gc_frame_enter(frame_map, alloca)
        registry.append((name, alloca))
        self._gc_rooted_local_names.add(name)
        if not hasattr(self, "_gc_rooted_local_order"):
            self._gc_rooted_local_order = []
        self._gc_rooted_local_order.append(name)
        self._patch_fn_err_exit_gc_root_leave(name, alloca)
        # Entry enters always run, so every exit must leave every slot:
        # retro-patch this slot's leave into exit sites whose cleanup was
        # emitted before this slot existed (e.g. an early `return` lowered
        # before a later re-binding created this alloca).
        if hasattr(self, "_fn_gc_root_exit_sites"):
            if fn.name in self._fn_gc_root_exit_sites:
                for site in self._fn_gc_root_exit_sites[fn.name]:
                    self._insert_gc_frame_leave_before_terminator(site, alloca)

    def _emit_gc_frame_leave_for_slot(self, alloca: ir.Value) -> None:
        self.builder.call(
            self.runtime["pcc_gc_frame_leave"],
            [
                self._as_gc_ptr(
                    alloca,
                    name=self._fresh("gc.frame.leave.ptr"),
                )
            ],
        )

    def _emit_gc_frame_leave_lifo_for_slot(self, alloca: ir.Value) -> None:
        self.builder.call(
            self.runtime["pcc_gc_frame_leave_lifo"],
            [
                self._as_gc_ptr(
                    alloca,
                    name=self._fresh("gc.frame.lifo.leave.ptr"),
                )
            ],
        )

    def _insert_gc_frame_leave_before_terminator(
        self,
        block: ir.Block,
        alloca: ir.Value,
    ) -> bool:
        terminator = None
        for instr in reversed(block._instrs):
            if self._instruction_is_terminator(instr):
                terminator = instr
                break
        if terminator is None:
            return False
        save_block = self.builder._block
        self.builder.position_before(terminator)
        self._emit_gc_frame_leave_for_slot(alloca)
        self.builder.position_at_end(save_block)
        return True

    def _patch_fn_err_exit_gc_root_leave(
        self,
        name: str,
        alloca: ir.Value,
    ) -> None:
        fn = self.current_function
        if fn is None:
            return
        err_bb = self._fn_err_exit_blocks.get(fn.name)
        if err_bb is None:
            return
        # Dedup by SLOT IDENTITY, not by local name (a re-bound name's
        # second alloca needs its own err-exit leave) and not by value-name
        # string (name uniquification timing differs between host and
        # self-hosted stages and would drift the emitted leave count).
        if not hasattr(self, "_fn_err_exit_gc_root_slots"):
            self._fn_err_exit_gc_root_slots = {}
        patched = self._fn_err_exit_gc_root_slots.setdefault(fn.name, [])
        for done in patched:
            if done is alloca:
                return
        if not self._insert_gc_frame_leave_before_terminator(err_bb, alloca):
            return
        patched.append(alloca)

    def _discard_owned_local_gc_root(self, name: str, alloca: ir.Value) -> None:
        # Compile-time bookkeeping only — do NOT emit a mid-function
        # frame_leave. The slot is an entry-block alloca (valid for the
        # whole call, always object-or-null), so staying registered is safe;
        # the per-slot registry emits the balancing leave at every function
        # exit. A mid-function leave here unregistered slots that re-entered
        # owned management on a later path/iteration, leaving them invisible
        # to the moving-GC relocation remap (gc3/gc4 stale-release UAF).
        if name not in getattr(self, "_gc_rooted_local_names", set()):
            return
        self._gc_rooted_local_names.discard(name)
        if hasattr(self, "_borrowed_gc_rooted_local_names"):
            self._borrowed_gc_rooted_local_names.discard(name)

    def _ensure_owned_local_flag(
        self,
        name: str,
        alloca: Optional[ir.Value] = None,
    ) -> ir.Value:
        if not hasattr(self, "_owned_local_flag_allocas"):
            self._owned_local_flag_allocas = {}
        flag = self._owned_local_flag_slots.get(name)
        flag_alloca = self._owned_local_flag_allocas.get(name)
        if flag is not None and (alloca is None or flag_alloca is alloca):
            return flag
        flag = self._alloca_in_entry(_I1, name=f"{name}.owned")
        self._store_entry_initializer(flag, ir.Constant(_I1, 0))
        self._owned_local_flag_slots[name] = flag
        if alloca is not None:
            self._owned_local_flag_allocas[name] = alloca
        else:
            self._owned_local_flag_allocas.pop(name, None)
        return flag

    def _owned_local_flag_for(
        self,
        name: str,
        alloca: Optional[ir.Value] = None,
    ) -> Optional[ir.Value]:
        flag = self._owned_local_flag_slots.get(name)
        if flag is None or alloca is None:
            return flag
        flag_alloca = getattr(self, "_owned_local_flag_allocas", {}).get(name)
        if flag_alloca is alloca:
            return flag
        return None

    def _emit_release_owned_local_if_flagged(
        self,
        name: str,
        alloca: ir.Value,
    ) -> None:
        flag = self._ensure_owned_local_flag(name, alloca)
        is_owned = self.builder.load(
            flag,
            name=self._fresh(f"{name}.owned.load"),
        )
        fn = self.current_function
        release_bb = fn.append_basic_block(name=self._fresh(f"{name}.owned.release"))
        cont_bb = fn.append_basic_block(name=self._fresh(f"{name}.owned.cont"))
        self.builder.cbranch(is_owned, release_bb, cont_bb)
        self.builder.position_at_end(release_bb)
        old_value = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [
                ir.Constant(_CSTR, None),
                self._as_gc_ptr(
                    alloca,
                    name=self._fresh(f"{name}.release.gc.slot"),
                ),
            ],
            name=self._fresh(f"{name}.release.current"),
        )
        self._gc_release(old_value, self._release_context_label(f"local:{name}"), True)
        self.builder.store(ir.Constant(_I1, 0), flag)
        self.builder.branch(cont_bb)
        self.builder.position_at_end(cont_bb)

    def _unpack_target_value_is_owned(self, value_ty: Type) -> bool:
        # Dynamic unpack sites can carry either a real PyObject* or a native
        # pointer-shaped value (for example dataclass / AST string fields in
        # the bootstrap compiler). Without stronger element ownership metadata,
        # treating DynType unpack results as owned PyObject*s is unsafe.
        return not isinstance(value_ty, DynType)

    def _mark_owned_local_for_unpack_target(
        self,
        target: Name,
        value_ty: Type,
        value_is_owned: Optional[bool] = None,
    ) -> None:
        """Mark a tuple-unpack ``Name`` target as owning the
        assignment source value.

        Unpacked elements come from APIs returning new refs (tuple/list
        getitem), so local-name bindings should be released on scope
        exit just like regular assignments.
        """
        if value_is_owned is None:
            value_is_owned = self._unpack_target_value_is_owned(value_ty)
        if not value_is_owned:
            return
        if self.current_func_def is None:
            return
        if target.ident in getattr(self, "_current_global_names", set()):
            return
        if target.ident in getattr(self, "_current_param_names", set()):
            return
        slot = self.env.get(target.ident)
        if slot is None:
            return
        _alloca, ir_ty, _decl_ty = slot
        if not isinstance(ir_ty, ir.PointerType):
            return
        if not self._ir_type_matches(ir_ty, _CSTR):
            return
        self._owned_local_names.add(target.ident)
        self._ensure_owned_local_gc_root(target.ident, _alloca, ir_ty)

    def _release_existing_owned_local(self, name: str) -> None:
        if name not in getattr(self, "_owned_local_names", set()):
            return
        slot = self.env.get(name)
        if slot is None:
            return
        alloca, ir_ty, _decl_ty = slot
        if not isinstance(ir_ty, ir.PointerType):
            return
        if not self._ir_type_matches(ir_ty, _CSTR):
            return
        if name in self._owned_local_has_value:
            self._emit_release_owned_local_if_flagged(name, alloca)
        self._discard_owned_local_gc_root(name, alloca)
        self._owned_local_names.discard(name)
        self._owned_local_has_value.discard(name)

    def _maybe_emit_discard_assignment(self, target: Name, value_expr: Expr) -> bool:
        if target.ident != "_" or self.current_func_def is None:
            return False
        if not self._raw_scaffold_object_rhs_is_owned(value_expr):
            return False
        if not self._expr_returns_owned_object(value_expr):
            return False
        self._release_existing_owned_local(target.ident)
        value = self._emit_expr(value_expr)
        self._gc_release_if_owned(value, value_expr)
        self.env.pop(target.ident, None)
        self.env_class_hint.pop(target.ident, None)
        self.env_list_elem_class_hint.pop(target.ident, None)
        if hasattr(self, "_cpy_env_flags"):
            self._cpy_env_flags.pop(target.ident, None)
        self._weak_dict_env_flags.pop(target.ident, None)
        return True

    def _emit_owned_local_cleanup(self, skip_name: Optional[str] = None) -> None:
        if self.current_func_def is None:
            return
        # In raw-int-scaffold mode, object-local ownership handling is
        # conservative for C-ABI-exporting/runtime modules, but user
        # scripts can still accumulate tracked locals from object-producing
        # assignments.
        for name in sorted(getattr(self, "_owned_local_names", set())):
            if name in getattr(self, "_current_global_names", set()):
                continue
            if (
                name in getattr(self, "_current_param_names", set())
                and not getattr(self, "_exact_int_env_flags", {}).get(name, False)
                and name not in getattr(self, "_for_target_owned_names", set())
            ):
                continue
            slot = self.env.get(name)
            if slot is None:
                continue
            alloca, ir_ty, _declared_ty = slot
            if not isinstance(ir_ty, ir.PointerType):
                continue
            if not self._ir_type_matches(ir_ty, _CSTR):
                continue
            if name in self._owned_local_has_value and name != skip_name:
                self._emit_release_owned_local_if_flagged(name, alloca)
        rooted_names = getattr(self, "_gc_rooted_local_names", set())
        root_order = list(getattr(self, "_gc_rooted_local_order", []))
        for name in sorted(rooted_names.difference(set(root_order))):
            root_order.append(name)
        emitted_root_unpins: set[str] = set()
        for name in reversed(root_order):
            if name in emitted_root_unpins:
                continue
            if name not in rooted_names:
                continue
            emitted_root_unpins.add(name)
            if name in getattr(self, "_owned_local_names", set()):
                continue
            if name in getattr(self, "_current_global_names", set()):
                continue
            slot = self.env.get(name)
            if slot is None:
                continue
            alloca, ir_ty, _declared_ty = slot
            if not isinstance(ir_ty, ir.PointerType):
                continue
            if not self._ir_type_matches(ir_ty, _CSTR):
                continue
            if name in getattr(self, "_pinned_gc_rooted_local_names", set()):
                pinned = self.builder.load(
                    alloca,
                    name=self._fresh("gc.root.cleanup.unpin"),
                )
                self.builder.call(
                    self.runtime["pcc_gc_unpin"],
                    [self._value_available_at_insertion_point(pinned)],
                )
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [
                        self._as_gc_ptr(
                            alloca,
                            name=self._fresh("gc.root.cleanup.ptr"),
                        ),
                        ir.Constant(_CSTR, None),
                    ],
                )
        # Leave every slot this function ever frame-registered, newest
        # first, exactly once per slot. Leaves are driven by the per-slot
        # registry (not by name-keyed sets resolved through env) so a name
        # re-bound to a fresh alloca leaves BOTH slots and the enter/leave
        # ledger stays balanced on every exit path.
        fn = self.current_function
        registry: list = []
        if fn is not None and hasattr(self, "_fn_gc_root_slot_registry"):
            if fn.name in self._fn_gc_root_slot_registry:
                registry = self._fn_gc_root_slot_registry[fn.name]
        emitted_slot_leaves: list = []
        for entry in reversed(registry):
            entry_alloca = entry[1]
            already = False
            for done in emitted_slot_leaves:
                if done is entry_alloca:
                    already = True
            if already:
                continue
            emitted_slot_leaves.append(entry_alloca)
            self._emit_gc_frame_leave_for_slot(entry_alloca)
        # Record this exit so slots registered AFTER this cleanup was
        # emitted retro-patch their leave into it (their entry enter always
        # runs, so this exit must leave them too).
        if fn is not None:
            if not hasattr(self, "_fn_gc_root_exit_sites"):
                self._fn_gc_root_exit_sites = {}
            sites = self._fn_gc_root_exit_sites.setdefault(fn.name, [])
            block = self.builder._block
            recorded = False
            for site in sites:
                if site is block:
                    recorded = True
            if not recorded:
                sites.append(block)

    def _store_entry_initializer(self, ptr: ir.Value, value: ir.Value) -> None:
        # Use self.builder rather than ``tmp_builder = ir.IRBuilder(entry)``:
        # the local-variable IRBuilder alias doesn't reliably register in
        # _ir_builder_env_flags under pcc-py self-host, so method calls on
        # it fall through scaffold dispatch into CPython fallback (see
        # _emit_entry_gc_frame_enter for the same pattern).
        saved_block = self.builder._block
        self._position_at_entry_hoist_point()
        self.builder.store(value, ptr)
        self.builder.position_at_end(saved_block)

    def _position_at_entry_hoist_point(self) -> None:
        """Position the builder where entry-hoisted protocol code belongs.

        The text oracle splits the entry block at the first raise-capable
        call, so "before the entry terminator" lands ahead of every
        exceptional path.  A direct/no-text function keeps executing in the
        same logical block past its inline error edges; there the same point
        is immediately before the first edge's trigger, otherwise before the
        terminator, otherwise the end of the block.
        """
        fn = self.current_function
        entry = fn.blocks[0]
        anchor = None
        if self._entry_inline_edge_anchor_function is fn:
            anchor = self._entry_inline_edge_anchor_record
        if anchor is not None:
            self.builder.position_before(anchor)
            return
        terminator = None
        for instr in reversed(entry._instrs):
            if self._instruction_is_terminator(instr):
                terminator = instr
                break
        if terminator is not None:
            self.builder.position_before(terminator)
        else:
            self.builder.position_at_end(entry)
