"""Method-call expression lowering for L1CodeGen."""

from __future__ import annotations

import os

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Attr,
    BinOp,
    BoolLit,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    Expr,
    FloatType,
    IntLit,
    IntType,
    ListType,
    MemoryViewType,
    Name,
    NoneType,
    SetType,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_STR
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.builtin_exceptions import builtin_exc_tag_or_missing

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_PYOBJ = ir.IntType(8).as_pointer()
_PY_EXC_RUNTIMEERROR = 7
_DYN_LIST_METHOD_NATIVE = frozenset(
    {
        "append",
        "extend",
        "insert",
        "pop",
        "remove",
        "index",
        "count",
        "sort",
        "clear",
    }
)
_DYN_DICT_METHOD_NATIVE = frozenset(
    {
        "get",
        "keys",
        "values",
        "items",
        "setdefault",
        "pop",
        "popitem",
    }
)
_DYN_SET_METHOD_NATIVE = frozenset(
    {
        "add",
        "remove",
        "discard",
        "update",
        "pop",
    }
)
_STR_METHOD_NATIVE = frozenset(
    {
        "upper",
        "lower",
        "strip",
        "lstrip",
        "rstrip",
        "split",
        "join",
        "replace",
        "find",
        "count",
        "encode",
        "startswith",
        "endswith",
        "splitlines",
        "isdigit",
        "isalpha",
        "isspace",
        "isalnum",
        "isupper",
        "islower",
        "isascii",
        "isidentifier",
        "isprintable",
        "isnumeric",
        "isdecimal",
        "istitle",
    }
)
_BYTES_METHOD_NATIVE = frozenset(
    {
        "decode",
        "find",
        "hex",
        "upper",
        "translate",
        "replace",
    }
)
_RE_MATCH_OBJECT_METHOD_NATIVE = frozenset(
    {
        "group",
        "groups",
        "groupdict",
        "start",
        "end",
        "span",
    }
)


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


def _method_node_kind(node) -> str:
    if node is None:
        return ""
    return type(node).__name__


def _method_is_name(node) -> bool:
    return isinstance(node, Name) or _method_node_kind(node) == "Name"


def _method_ident(node) -> Optional[str]:
    try:
        ident = node.ident
    except AttributeError:
        return None
    if ident is None:
        return None
    return str(ident)


def _method_first_arg_name(class_lowering, class_name: str, method_name: str) -> str:
    fd = class_lowering._find_method_def(class_name, method_name)
    if fd is None:
        return ""
    try:
        args = fd.args
    except AttributeError:
        return ""
    if not args:
        return ""
    try:
        return str(args[0].name)
    except AttributeError:
        return ""


class MethodCallExpressionLoweringMixin:
    def _class_attr_needs_runtime_lookup(self, class_info, attr_name: str) -> bool:
        if class_info is None:
            return False
        if self.class_lowering.lookup_class_attr(class_info, attr_name) is not None:
            return True
        class_attr_state = getattr(
            self,
            "_class_attr_runtime_state",
            {},
        ).get((class_info.name, attr_name))
        return class_attr_state in ("live", "unknown", "deleted")

    def _direct_virtual_dispatch_enabled(self) -> bool:
        """Route an overridden ``self.foo(...)`` through one MRO lookup.

        A method some subclass overrides cannot be called directly, so it took
        the general attribute protocol: an MRO walk for ``__getattribute__``,
        one for ``__getattr__``, a field-index scan and one for the name; then
        a bound-method object with a two-element captures tuple; then the
        caller's argument tuple; then a second tuple inside
        ``_instance_bound_method_entry`` to prepend the receiver -- four
        allocations and four MRO-shaped scans per call, all refcounted down
        again afterwards.  Profiling pcc1 compiling a module puts roughly 40%
        of self time in that protocol and another 13% in the refcount and
        deallocation traffic it creates.

        Only one thing there is genuinely dynamic: which class the receiver
        turns out to be.  ``py_instance_method_call_direct`` keeps that lookup
        and drops the rest, given an argument tuple that already holds the
        receiver at index 0.

        Off by default while it is being qualified.
        """
        return bool(getattr(self, "_direct_virtual_method_calls", False))

    def _direct_virtual_dispatch_is_sound(self, attr_name: str) -> bool:
        """True when the closed-world class graph cannot redirect ``attr_name``.

        The runtime entry resolves the name straight out of the receiver's MRO,
        which is what ``self.foo()`` means only while nothing intercepts
        attribute access and no field shadows the name.  Both are closed-world
        questions, and this is the same graph walk
        ``method_overridden_by_subclass`` already uses -- so the check stays
        with the compiler rather than costing a scan at every call.
        """
        class_lowering = getattr(self, "class_lowering", None)
        if class_lowering is None:
            return False
        if attr_name.startswith("__") and attr_name.endswith("__"):
            # Dunders reach the instance through their own protocols.
            return False
        for info in class_lowering.classes.values():
            methods = getattr(info, "methods", {}) or {}
            if "__getattribute__" in methods or "__getattr__" in methods:
                return False
            # __slots__ needs no check of its own: it is a class-body
            # assignment, so it never appears in `methods`, and
            # ClassLowering._declare_slots appends each slot to
            # `field_names` -- which the next test already rejects.
            if attr_name in (getattr(info, "field_names", ()) or ()):
                return False
        # A native extension class outside this closure could still intercept
        # attribute access, so the receiver has to be a class this unit owns.
        return not bool(getattr(self, "_native_module_exports", None))

    def _emit_virtual_method_call(
        self,
        obj_expr: Expr,
        attr_name: str,
        args: tuple[Expr, ...],
        span,
    ) -> ir.Value:
        """``self.foo(a, b)`` as one lookup-and-call on a receiver-first tuple."""
        elems = (obj_expr,) + tuple(args)
        full_args = self._emit_tuple_literal(TupleExpr(
            span=span,
            ty=TupleType(
                name="tuple",
                elems=tuple(getattr(e, "ty", None) for e in elems),
            ),
            elems=elems,
        ))
        # The receiver is lowered a second time here: `elems[0]` above already
        # emitted it inside the tuple.  `_method_is_name` gates this path, so
        # the repeat is a plain Name load with no effects to duplicate -- but
        # the two values carry independent ownership state, and a Name whose
        # load ever produces a fresh object (an exact-int object lane, a
        # valueclass box) would leave one of them unconsumed.  Emit it once
        # and share it before this path is qualified.
        receiver = self._emit_expr(obj_expr)
        name_ptr = self._attr_name_ptr(attr_name)
        result = self.builder.call(
            self.runtime["py_instance_method_call_direct"],
            [receiver, name_ptr, full_args],
            name=self._fresh(f"virtual.{attr_name}"),
        )
        # It tail-calls py_func_call_kwargs, so the result is a NEW reference,
        # the same category every py_obj_call site records.  Without this the
        # shape classifier guesses from the AST instead.
        self._note_owned_dynamic_call_value(result)
        self._gc_release(full_args)
        self._emit_post_call_err_check(span)
        return result

    def _emit_overridden_method_call(
        self,
        obj_expr: Expr,
        attr_name: str,
        args: tuple[Expr, ...],
        kwargs: tuple,
        span,
        call_expr: Optional[Call] = None,
    ) -> ir.Value:
        """The override fallback: one MRO lookup when that is provably enough."""
        if call_expr is not None and self._slot_call_result_sink(call_expr) is not None:
            return self._emit_slot_call_object(call_expr, "method." + attr_name)
        if (
            self._direct_virtual_dispatch_enabled()
            and not kwargs
            and not self._has_unpacked_operands(args, kwargs)
            and _method_is_name(obj_expr)
            and self._direct_virtual_dispatch_is_sound(attr_name)
        ):
            return self._emit_virtual_method_call(obj_expr, attr_name, args, span)
        return self._emit_callable_attribute_call(
            obj_expr,
            attr_name,
            args,
            kwargs,
            span,
        )

    def _emit_closure_method_call(self, obj_expr, attr_name, args, kwargs, span, call_expr):
        """Call a function-local class closure method through its function object.

        Its native entry takes the class statement's captures as trailing
        parameters, so a direct call cannot supply them: the attribute lookup
        finds the function object that the defining execution published.
        """
        if call_expr is not None and self._slot_call_result_sink(call_expr) is not None:
            return self._emit_slot_call_object(call_expr, "method." + attr_name)
        return self._emit_callable_attribute_call(obj_expr, attr_name, args, kwargs, span)

    def _emit_callable_attribute_call(
        self,
        obj_expr: Expr,
        attr_name: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        span,
        call_expr: Optional[Call] = None,
    ) -> ir.Value:
        if call_expr is not None and self._slot_call_result_sink(call_expr) is not None:
            # Preserve the original Call identity through lookup and invocation.
            # The shared object-call ABI roots the callable before its arguments
            # and publishes the NEW result before retiring any of those owners.
            return self._emit_slot_call_object(call_expr, "method." + attr_name)
        obj_val = self._emit_expr(obj_expr)
        return self._emit_callable_attribute_call_on_value(
            obj_val, attr_name, args, kwargs, span
        )

    def _emit_callable_attribute_call_on_value(
        self,
        obj_val: ir.Value,
        attr_name: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        span,
    ) -> ir.Value:
        name_ptr = self._attr_name_ptr(attr_name)
        if not kwargs and self._split_starstar_kwargs_unpack(args) is None:
            return self._emit_loaded_method_call(
                obj_val, name_ptr, attr_name, args, span
            )
        # Keywords too take the loaded-method route: binding a method object
        # (captures tuple plus a copied signature) for a single call was most
        # of `builder.call(..., name=...)` in a native frontend worker.
        self_slot = self._alloca_in_entry(
            _PYOBJ, self._fresh(f"method.self.{attr_name}"), init_null=True
        )
        callable_obj = self.builder.call(
            self.runtime["py_obj_load_method"],
            [obj_val, name_ptr, self_slot],
            name=self._fresh(f"callable.attr.{attr_name}"),
        )
        self._emit_post_call_err_check(span)
        kwdict_unpack = self._split_starstar_kwargs_unpack(args)
        arg_exprs = args
        kwargs_expr = None
        if kwdict_unpack is not None:
            arg_exprs, kwargs_expr = kwdict_unpack
        args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
        kwargs_obj = self._emit_dynamic_call_kwargs_object(
            kwargs,
            kwargs_expr,
            span,
        )
        self_val = self.builder.load(
            self_slot, name=self._fresh(f"method.self.{attr_name}.val")
        )
        result = self.builder.call(
            self.runtime["py_obj_call_method_kwargs"],
            [callable_obj, self_val, args_tuple, kwargs_obj],
            name=self._fresh(f"callable.attr.{attr_name}.call"),
        )
        self._gc_release(args_tuple)
        if kwargs:
            self._gc_release(kwargs_obj)
        self._gc_release(callable_obj)
        self._emit_post_call_err_check(span)
        return result

    def _emit_loaded_method_call(
        self,
        obj_val: ir.Value,
        name_ptr: ir.Value,
        attr_name: str,
        args: tuple[Expr, ...],
        span,
        *,
        attribute_error_span=None,
        dynamic_result: bool = False,
    ) -> ir.Value:
        """Positional ``obj.name(args)`` without allocating a bound method.

        ``py_obj_load_method`` resolves ``obj.name`` before the arguments are
        evaluated, as the attribute load did; for a plain class function it
        returns the function and the receiver instead of a bound object.
        """
        self_slot = self._alloca_in_entry(
            _PYOBJ, self._fresh(f"method.self.{attr_name}"), init_null=True
        )
        callable_obj = self.builder.call(
            self.runtime["py_obj_load_method"],
            [obj_val, name_ptr, self_slot],
            name=self._fresh(f"callable.attr.{attr_name}"),
        )
        if attribute_error_span is not None:
            self._emit_attribute_error_if_null(
                callable_obj, attr_name, attribute_error_span
            )
        else:
            self._emit_post_call_err_check(span)
        args_tuple = self._emit_dynamic_call_args_tuple(args)
        self_val = self.builder.load(
            self_slot, name=self._fresh(f"method.self.{attr_name}.val")
        )
        result = self.builder.call(
            self.runtime["py_obj_call_method"],
            [callable_obj, self_val, args_tuple],
            name=self._fresh(f"callable.attr.{attr_name}.call"),
        )
        self._gc_release(args_tuple)
        self._gc_release(callable_obj)
        self._emit_post_call_err_check(span)
        if dynamic_result:
            self._note_owned_dynamic_call_value(result)
        return result

    def _resumable_may_park_method(self, owner, method_name: str) -> bool:
        """Whether a resumable caller delegates to this method's continuation."""
        if len(getattr(self, "_generator_ctx_stack", ())) == 0:
            return False
        if owner.name + "." + method_name in getattr(
            self, "_vthread_may_park_method_keys", set(),
        ):
            return True
        return method_name in getattr(owner, "may_park_methods", set())

    def _emit_method_call(self, expr: Call) -> ir.Value:
        """Lower ``obj.method(args)`` using the class method registry.

        Fast path: if ``obj`` is a Name bound in the local env to a
        ``DynType`` instance of a known class in the current module,
        and ``method`` is a direct member of that class (no MRO
        walking), dispatch to the declared pcc method function. The
        generic ``py_obj_call_method`` path is used otherwise.
        """
        attr = expr.func
        assert isinstance(attr, Attr)

        if (
            attr.name == "__str__"
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "str"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            value = self._emit_as_object(expr.args[0])
            tag = self.builder.call(
                self.runtime["py_obj_type_tag"],
                [value],
                name=self._fresh("str.__str__.tag"),
            )
            is_str = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_STR),
                name=self._fresh("str.__str__.is_str"),
            )
            ok_bb = self.current_function.append_basic_block(
                name=self._fresh("str.__str__.ok")
            )
            err_bb = self.current_function.append_basic_block(
                name=self._fresh("str.__str__.err")
            )
            self.builder.cbranch(is_str, ok_bb, err_bb)
            self.builder.position_at_end(err_bb)
            self._emit_builtin_exception_and_branch(
                "TypeError",
                "descriptor '__str__' requires a 'str' object",
                self._expr_span_or_none(expr),
            )
            self.builder.position_at_end(ok_bb)
            return self.builder.call(
                self.runtime["py_obj_str"],
                [value],
                name=self._fresh("str.__str__"),
            )

        if (
            attr.name == "__call__"
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "type"
            and len(expr.args) >= 1
        ):
            callable_obj = self._emit_as_object(expr.args[0])
            call_args = expr.args[1:]
            kwdict_unpack = self._split_starstar_kwargs_unpack(call_args)
            arg_exprs = call_args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                self._expr_span_or_none(expr),
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [callable_obj, args_tuple, kwargs_obj],
                name=self._fresh("type.call"),
            )
            self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result

        if (
            attr.name == "__new__"
            and _method_is_name(attr.obj)
            and self._native_builtin_value_for_name(_method_ident(attr.obj)) == "builtins.object"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            cls_obj = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_instance_new"], [cls_obj],
                name=self._fresh("object.new"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        if (
            attr.name == "__new__"
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "type"
        ):
            if len(expr.args) >= 4:
                return self._emit_owned_dynamic_type_constructor(expr, expr.args[1:4])
            return self._emit_none_literal()
        if (
            attr.name == "__getattribute__"
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "object"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            obj_val = self._emit_as_object(expr.args[0])
            name_ptr = self._emit_attr_name_ptr_arg(
                expr.args[1],
                "object.getattribute.name",
            )
            return self.builder.call(
                self.runtime["py_obj_getattr_default"],
                [obj_val, name_ptr],
                name=self._fresh("object.getattribute"),
            )

        # Path A scaffold dispatch (Issue 1). ON mode routes
        # ``self.builder.X(...)`` and similar IRBuilder method calls
        # through the closed-world lowering instead of py_cpy_* dynamic
        # dispatch. Method coverage is added incrementally; anything
        # not yet implemented raises ScaffoldUnsupportedError with a
        # clear message rather than silently falling back.
        if self._ir_scaffold_enabled():
            scaffold_value = self._maybe_emit_ir_scaffold_call(expr)
            if scaffold_value is not None:
                return scaffold_value

        # Decide from signature/receiver metadata before loading a receiver
        # or evaluating any call operand. Descriptor binding remains a runtime
        # getattr of the original receiver, so static/class/unbound methods
        # keep their Python behavior on the shared signature-binding path.
        class_object = self._class_object_hint_for_expr(attr.obj)
        receiver_class = class_object or self._class_hint_for_expr(attr.obj)
        if receiver_class is not None:
            binding_owner = self._resolve_method_mro(receiver_class, attr.name)
            binding_fd = self._native_class_method_def(binding_owner, attr.name)
            if binding_owner is not None and not binding_owner.valueclass:
                kind = binding_owner.method_kinds.get(attr.name, "instance")
                bound_receiver = kind == "classmethod" or kind != "static" and class_object is None
                # An instance's __getattribute__ runs before argument
                # evaluation, even when its target method has a proven direct
                # signature. A compatible subclass can introduce the hook.
                receiver_info = self.class_lowering.classes.get(receiver_class)
                attribute_hook = class_object is None and (
                    self._resolve_method_mro(receiver_class, "__getattribute__") is not None
                    or (receiver_info is not None
                        and self.class_lowering.method_overridden_by_subclass(
                            receiver_info, "__getattribute__"
                        ))
                )
                if attribute_hook:
                    # The hook can replace the declared method with any
                    # callable, including one with a different return kind.
                    result = self._emit_slot_call_object(expr, attr.name + ".intercepted")
                    self._note_owned_dynamic_call_value(result)
                    return result
                # A resumable caller delegates to a may-park method's
                # continuation, and that delegation publishes the child's
                # result into the consumer's slot (take_generator_frame_result).
                # Runtime binding would hand the consumer the continuation
                # object itself: ``self.leaf(v) + 1`` resumed to None.
                sink_requires_object = self._slot_call_result_sink(expr) is not None
                if sink_requires_object and self._resumable_may_park_method(
                    binding_owner, attr.name,
                ):
                    sink_requires_object = False
                if self._ordinary_call_needs_runtime_binding(
                    expr, binding_fd, bound_receiver, sink_requires_object,
                ):
                    binding_fn = binding_owner.methods.get(attr.name)
                    if binding_fn is not None:
                        return self._emit_runtime_bound_user_call(
                            expr, binding_owner.name + "." + attr.name, binding_fn,
                        )

        if attr.name in ("format", "format_map"):
            if self._resolve_str_literal_value(attr.obj) is not None:
                native = self._maybe_emit_str_method(expr)
                if native is not None:
                    return native

        # Typed-container method dispatch on pcc-native containers —
        # stays on pcc runtime so the produced binary has no libpython
        # dep. Only a curated method set is recognised; anything else
        # surfaces as NotImplementedError rather than falling through
        # to the generic CPython helper.
        obj_ty0 = attr.obj.ty
        if (self._has_unpacked_operands(expr.args, expr.kwargs)
                and not self._expr_looks_cpython(attr.obj)
                and (isinstance(obj_ty0, (ListType, TupleType, DictType, StrType, BytesType, ByteArrayType))
                     or self._is_native_set_dyn(obj_ty0))):
            # The typed fast paths take fixed operands. The generic call binds
            # the unpacked arguments against the receiver's method object, as
            # for a dynamic receiver, instead of a CPython method call.
            return self._emit_slot_call_object(expr, "method." + attr.name)
        if isinstance(obj_ty0, (BytesType, ByteArrayType)) and attr.name in ("strip", "lstrip", "rstrip"):
            native = self._emit_owned_strip_method(expr)
            if native is not None:
                return native
        if isinstance(obj_ty0, ListType):
            native = self._maybe_emit_list_method(expr, obj_ty0)
            if native is not None:
                return native
        if isinstance(obj_ty0, TupleType):
            native = self._maybe_emit_tuple_method(expr)
            if native is not None:
                return native
        if isinstance(obj_ty0, DictType):
            native = self._maybe_emit_dict_method(expr, obj_ty0)
            if native is not None:
                return native
        if self._is_native_set_dyn(obj_ty0):
            native = self._maybe_emit_set_method(expr)
            if native is not None:
                return native
        if isinstance(obj_ty0, StrType):
            native = self._maybe_emit_str_method(expr)
            if native is not None:
                return native
        if (
            isinstance(obj_ty0, ComplexType)
            and attr.name == "conjugate"
            and not expr.args
            and not expr.kwargs
        ):
            # A rooted receiver and the NEW result published into the
            # consumer's root: repr(z.conjugate()) is an object operand.
            return self._emit_owned_operand_runtime_call(
                expr, "py_complex_conjugate", 1, operand_exprs=(attr.obj,),
            )
        if (
            isinstance(obj_ty0, DynType)
            and obj_ty0.name == "coroutine"
            and attr.name == "close"
            and not expr.args
            and not expr.kwargs
        ):
            # Keep the receiver owned through close and publish its actual
            # return value before checking errors or retiring that owner.
            return self._emit_owned_unary_runtime_call(expr, "py_coroutine_close")
        native_threading_method = self._maybe_emit_threading_instance_method(expr)
        if native_threading_method is not None:
            return native_threading_method
        builtin_type_call = self._maybe_emit_builtin_type_method(expr)
        if builtin_type_call is not None:
            return builtin_type_call
        native_inspect_call = self._maybe_emit_native_inspect_call(expr)
        if native_inspect_call is not None:
            return native_inspect_call
        native_module_alias_call = self._maybe_emit_native_module_alias_call(expr)
        if native_module_alias_call is not None:
            return native_module_alias_call
        native_os_call = self._emit_native_os_call(expr)
        if native_os_call is not None:
            return native_os_call
        native_platform_call = self._emit_native_platform_call(expr)
        if native_platform_call is not None:
            return native_platform_call
        native_builtin_module_call = self._emit_native_builtin_module_call(expr)
        if native_builtin_module_call is not None:
            return native_builtin_module_call
        native_math_call = self._emit_native_math_call(expr)
        if native_math_call is not None:
            return native_math_call
        native_json_call = self._emit_native_json_call(expr)
        if native_json_call is not None:
            return native_json_call
        native_re_call = self._emit_native_re_call(expr)
        if native_re_call is not None:
            return native_re_call
        native_gc_callbacks_method = self._emit_native_gc_callbacks_method(expr)
        if native_gc_callbacks_method is not None:
            return native_gc_callbacks_method
        native_gc_call = self._emit_native_gc_call(expr)
        if native_gc_call is not None:
            return native_gc_call
        native_weakref_call = self._emit_native_weakref_call(expr)
        if native_weakref_call is not None:
            return native_weakref_call
        native_asyncio_call = self._emit_native_asyncio_call(expr)
        if native_asyncio_call is not None:
            return native_asyncio_call
        native_threading_call = self._emit_native_threading_call(expr)
        if native_threading_call is not None:
            return native_threading_call
        native_virtual_thread_call = self._emit_native_virtual_thread_call(expr)
        if native_virtual_thread_call is not None:
            return native_virtual_thread_call
        native_subprocess_call = self._emit_native_subprocess_call(expr)
        if native_subprocess_call is not None:
            return native_subprocess_call
        native_shutil_call = self._emit_native_shutil_call(expr)
        if native_shutil_call is not None:
            return native_shutil_call
        native_shlex_call = self._emit_native_shlex_call(expr)
        if native_shlex_call is not None:
            return native_shlex_call
        native_sysconfig_call = self._emit_native_sysconfig_call(expr)
        if native_sysconfig_call is not None:
            return native_sysconfig_call
        native_os_environ_call = self._emit_native_os_environ_call(expr)
        if native_os_environ_call is not None:
            return native_os_environ_call
        native_sys_stream_call = self._emit_native_sys_stream_call(expr)
        if native_sys_stream_call is not None:
            return native_sys_stream_call
        native_open = self._emit_native_open_call(expr)
        if native_open is not None:
            return native_open
        native_file_method = self._emit_native_file_method(expr)
        if native_file_method is not None:
            return native_file_method
        native_fileinput_ctor = self._emit_native_fileinput_call(expr)
        if native_fileinput_ctor is not None:
            return native_fileinput_ctor
        native_fileinput_method = self._emit_native_fileinput_method(expr)
        if native_fileinput_method is not None:
            return native_fileinput_method
        native_sys_exit_call = self._emit_native_sys_exit_call(expr)
        if native_sys_exit_call is not None:
            return native_sys_exit_call
        native_os_path_call = self._emit_native_os_path_call(expr)
        if native_os_path_call is not None:
            return native_os_path_call
        if isinstance(attr.obj, Name):
            builtin_module = self._native_builtin_module_for_name(attr.obj.ident)
            if (
                builtin_module == "builtins"
                and attr.name == "int"
                and 1 <= len(expr.args) <= 2
            ):
                result = self._maybe_emit_int_builtin(expr)
                if result is not None:
                    return result
            if (
                builtin_module == "builtins"
                and attr.name == "str"
                and len(expr.args) == 1
                and not expr.kwargs
            ):
                return self._emit_str_builtin(expr)
            builtin_value = self._native_builtin_value_for_name(attr.obj.ident)
            if builtin_value == "os.path":
                return self._emit_cpy_method_call_src(
                    self._emit_cpy_attr(
                        self._emit_cpython_module_value("os"),
                        "path",
                    ),
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )

        compiled_builtin_call = self._maybe_emit_native_builtin_compiled_call(expr)
        if compiled_builtin_call is not None:
            return compiled_builtin_call

        # Case -1: ``<CPython value>.method(args)``.
        #
        # Chained access (``os.path.join``) lowers the inner attr chain
        # through ``_emit_attr``, which already routes through
        # ``py_cpy_getattr`` whenever the root is an imported module or
        # a CPython-flagged local. If the resulting SSA value lands in
        # ``_cpy_values``, dispatch the method call through libpython.
        if isinstance(attr.obj, Name):
            builtin_module = self._native_builtin_module_for_name(attr.obj.ident)
            if builtin_module is not None:
                if (
                    builtin_module == "builtins"
                    and attr.name == "int"
                    and 1 <= len(expr.args) <= 2
                ):
                    result = self._maybe_emit_int_builtin(expr)
                    if result is not None:
                        return result
                return self._emit_cpy_method_call_src(
                    self._emit_cpython_module_value(builtin_module),
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )
            cpy_gv = getattr(self, "_cpy_module_env", {}).get(attr.obj.ident)
            if cpy_gv is not None:
                return self._emit_cpy_method_call_src(
                    self.builder.load(cpy_gv, name=self._fresh("cpy.mod")),
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )
            if getattr(self, "_cpy_env_flags", {}).get(attr.obj.ident, False):
                if attr.name in _STR_METHOD_NATIVE:
                    native = self._maybe_emit_str_method_via_dyn(expr)
                    if native is not None:
                        return native
                return self._emit_cpy_method_call_src(
                    self._emit_expr(attr.obj),
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )
        if isinstance(attr.obj, Attr):
            # Evaluate the chain eagerly; if the result was tagged as a
            # CPython value (e.g. ``os.path``), dispatch there.
            chain_val = self._emit_expr(attr.obj)
            if chain_val in getattr(self, "_cpy_values", ()):
                return self._emit_cpy_method_call_src(
                    chain_val,
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )
        # ``super(...)`` is recognised below (Case 0) before anything reads
        # its value: a receiver-type guess keyed on the method name
        # (``super().count()`` as ``str.count``) would evaluate builtin super.
        super_receiver = self._is_method_super_call(attr.obj)
        if (
            attr.name == "strftime" and len(expr.args) == 1 and not expr.kwargs
            and not super_receiver
        ):
            # Native datetime/time-like ``obj.strftime(fmt)`` for values whose
            # concrete class is not visible at the call site (for example a
            # py_stdlib cross-module classmethod result). Evaluate the receiver
            # for side effects, then delegate formatting to the runtime clock.
            self._emit_as_object(attr.obj)
            result = self.builder.call(
                self.runtime["py_time_strftime"],
                [self._emit_as_object(expr.args[0])],
                name=self._fresh("dyn.strftime"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if isinstance(attr.obj, Call) and not super_receiver:
            # A method call on a CPython-call RESULT (e.g.
            # ``numpy.arange(10).sum()``): the result is a real CPython object,
            # so the method must dispatch through libpython. The bare-``Attr``
            # branch above does not fire because the receiver is a ``Call``, and
            # the native fall-through below would use ``py_obj_getattr`` (pcc's
            # object model) on the real CPython object -> AttributeError. Detect
            # the cpy call STRUCTURALLY (callable is a CPython-module attribute)
            # so a non-matching ``Call`` receiver is not pre-emitted (avoids
            # double-evaluating/calling it in the native path); the cpy path
            # emits the receiver exactly once. Inert in no-libpython mode (no
            # CPython modules => never matches => bootstrap unaffected).
            cfunc = attr.obj.func
            if (
                isinstance(cfunc, Attr)
                and isinstance(cfunc.obj, Name)
                and (
                    cfunc.obj.ident in getattr(self, "_cpy_module_env", {})
                    or getattr(self, "_cpy_env_flags", {}).get(cfunc.obj.ident, False)
                )
            ):
                return self._emit_cpy_method_call_src(
                    self._emit_expr(attr.obj),
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    operand_order=expr.operand_order,
                )
            call_receiver_hint = self._class_hint_for_expr(attr.obj)
            if (
                call_receiver_hint is None
                and isinstance(attr.obj.ty, DynType)
                and attr.name in _BYTES_METHOD_NATIVE
            ):
                native = self._maybe_emit_bytes_method_via_dyn(expr)
                if native is not None:
                    return native
            if (
                call_receiver_hint is None
                and isinstance(attr.obj.ty, DynType)
                and attr.name in _STR_METHOD_NATIVE
            ):
                native = self._maybe_emit_str_method_via_dyn(expr)
                if native is not None:
                    return native
        native_re_match_receiver = False
        if isinstance(attr.obj, Call) and attr.name in _RE_MATCH_OBJECT_METHOD_NATIVE:
            cfunc = attr.obj.func
            if isinstance(cfunc, Attr) and isinstance(cfunc.obj, Name):
                root_name = cfunc.obj.ident
                if (
                    cfunc.name in ("match", "search", "fullmatch")
                    and self._native_builtin_module_for_name(root_name) == "re"
                ):
                    native_re_match_receiver = True
                elif (
                    cfunc.name in ("match", "search")
                    and self._native_re_compile_alias_for_name(root_name) is not None
                ):
                    native_re_match_receiver = True
        if (
            isinstance(attr.obj, (BinOp, Subscript, Call))
            and self._expr_looks_cpython(attr.obj)
            and not native_re_match_receiver
        ):
            # A method call on a BINARY-OP, SUBSCRIPT, or (deep-chain) CALL result
            # that is itself a CPython value (e.g. ``(a + b).sum()``,
            # ``a[1:4].sum()``, or ``np.arange(4).reshape(2,2).sum()`` on numpy):
            # the binop (``py_cpy_binop``) / subscript / chained call yields a real
            # CPython object, so the method must dispatch through libpython. Detect
            # STRUCTURALLY (the receiver is cpy via ``_expr_looks_cpython``, which
            # recurses through BinOp operands, Subscript objects, and Call funcs)
            # so a non-cpy receiver is not pre-emitted; the cpy path emits the
            # receiver exactly once. The narrow Call-receiver branch above is a
            # fast path for the simple ``module.fn(...).method()`` shape and fires
            # first; this generalised branch additionally catches DEEPER chains
            # whose inner callable is itself a Call (where ``cfunc.obj`` is not a
            # Name). Inert in no-libpython (no CPython modules => never looks cpy
            # => bootstrap unaffected). Without this the native fall-through would
            # run ``py_obj_getattr`` (pcc's object model) on the real CPython
            # object -> AttributeError; the stored form ``c = a[1:4]; c.sum()``
            # already worked via the assignment's cpy-value tagging.
            return self._emit_cpy_method_call_src(
                self._emit_expr(attr.obj),
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
            )

        # Generator intrinsics. The ``send``/``throw``/``close`` names are
        # common user methods, so only short-circuit when inference marked the
        # receiver as a native generator. A generic DynType receiver must use
        # normal dynamic dispatch.
        gen_intrinsic_ok = (
            isinstance(attr.obj.ty, DynType) and attr.obj.ty.name == "generator"
        )
        gen_runtime_name = ""
        if (
            gen_intrinsic_ok
            and attr.name == "send"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            gen_runtime_name = "py_gen_send"
        elif (
            gen_intrinsic_ok
            and attr.name == "throw"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            gen_runtime_name = "py_gen_throw"
        elif (
            gen_intrinsic_ok
            and attr.name == "close"
            and not expr.args
            and not expr.kwargs
        ):
            gen_runtime_name = "py_gen_close"
        if gen_runtime_name:
            # These runtime producers return a NEW yield/return owner, or
            # NULL with TLS set. Keep the receiver and explicit argument in
            # independent roots, and publish the result before error checks
            # or lease release can park. close() can return the generator's
            # return value, so it has the same ownership boundary as send().
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root(gen_runtime_name + ".result")
                roots.append(output)
            operands = []
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                receiver = self._emit_slot_call_operand(attr.obj, gen_runtime_name + ".receiver")
                roots.append(receiver)
                operands.append(receiver)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                for argument_expr in expr.args:
                    argument = self._emit_slot_call_operand(argument_expr, gen_runtime_name + ".argument")
                    roots.append(argument)
                    operands.append(argument)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    gen_runtime_name, tuple(operands), result_slot=output, span=expr.span,
                )
                self._release_slot_call_roots(tuple(operands))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("gen.result.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy

        # Case 0: ``super().method(args)`` inside a method body.
        # Resolve the method by walking the current class's declared
        # bases. The ``self`` argument is forwarded unchanged.
        current_class = self.current_class
        if super_receiver:
            super_args = attr.obj.args
            from_class = current_class
            if len(super_args) == 2:
                from_expr = super_args[0]
                if not isinstance(from_expr, Name):
                    from_class = None
                else:
                    local_class_hints = getattr(self, "env_class_object_hint", {})
                    if from_expr.ident in local_class_hints:
                        from_class_name = local_class_hints[from_expr.ident]
                    elif from_expr.ident in self.env:
                        from_class_name = None
                    else:
                        from_class_name = self._resolve_class_alias(from_expr.ident)
                    from_class = self.class_lowering.classes.get(from_class_name)
            if (
                from_class is not None
                and attr.name == "__new__"
                and "type" in self.class_lowering._class_declared_base_names(from_class)
                and len(expr.args) >= 4
                and not expr.kwargs
            ):
                # ``super().__new__(mcls, name, bases, ns)`` in a
                # metaclass subclass is ``type.__new__``.  Builtin ``type``
                # has no ClassInfo entry, so the ordinary super lookup below
                # cannot resolve it and historically returned NULL.  Route
                # the foreign builtin base through the same native class
                # constructor used for an explicit ``type.__new__`` call.
                return self._emit_owned_dynamic_type_constructor(expr, expr.args[1:4])
            if (from_class is not None and attr.name == "__new__"
                    and self._class_has_tuple_base(from_class)):
                return self._emit_tuple_super_new_slots(expr, from_class, super_args)
            if from_class is not None and attr.name == "__new__":
                # Follow a single-inheritance chain until either a user class
                # actually defines ``__new__`` or the chain reaches builtin
                # object.  Looking only at the immediate base made
                # ``class SingletonBase(Root): super().__new__(cls)`` return
                # NULL when Root did not override object.__new__; the later
                # class-attribute store then surfaced a misleading
                # AttributeError.  Keep multiple/foreign bases on the generic
                # super path because they require runtime C3 lookup.
                delegates_to_object = False
                candidate = from_class
                seen_super_new_classes: set[str] = set()
                while candidate is not None:
                    candidate_name = getattr(candidate, "name", "")
                    if candidate_name in seen_super_new_classes:
                        break
                    seen_super_new_classes.add(candidate_name)
                    base_names = self.class_lowering._class_declared_base_names(
                        candidate
                    )
                    if len(base_names) == 0 or base_names == ("object",):
                        delegates_to_object = True
                        break
                    if len(base_names) != 1:
                        break
                    base_name = base_names[0]
                    if base_name == "object":
                        delegates_to_object = True
                        break
                    base_info = self.class_lowering.classes.get(base_name)
                    if base_info is None:
                        # The base is not registered in this module -- the
                        # usual case for a cross-module class reached through
                        # an imported module object, where only the leaf is
                        # declared extern.  Falling through left the generic
                        # super path to resolve ``__new__`` by name, which
                        # found the *current* class again: pcc1 recursed at
                        # pcc/ir/ir.py:207 until ``cls`` decayed to a
                        # bare object.  An invisible base cannot show a user
                        # ``__new__``, so delegate to the allocator, which is
                        # what ``object.__new__`` does.
                        delegates_to_object = True
                        break
                    if "__new__" in base_info.methods:
                        break
                    candidate = base_info
                if delegates_to_object and len(expr.args) == 1 and not expr.kwargs:
                    # ``super().__new__(cls)`` for an ordinary Python class is
                    # builtin ``object.__new__``. ``object`` has no ClassInfo,
                    # so the generic foreign-super fallback below cannot
                    # resolve a callable and historically returned NULL. Use
                    # the same native allocator as normal class construction,
                    # preserving the runtime class argument for subclasses.
                    cls_obj = self._emit_as_object(expr.args[0])
                    result = self.builder.call(
                        self.runtime["py_instance_new"],
                        [cls_obj],
                        name=self._fresh("super.object.new"),
                    )
                    self._emit_post_call_err_check(self._expr_span_or_none(expr))
                    return result
            if from_class is not None and attr.name == "__init__":
                declared_bases = self.class_lowering._class_declared_base_names(from_class)
                if len(declared_bases) > 1 and any(
                    base == "dict" or builtin_exc_tag_or_missing(base) >= 0
                    for base in declared_bases
                ):
                    raise L1CodegenError(
                        "native super().__init__ for a mixed builtin-base MRO "
                        "requires builtin-base descriptor lookup"
                    )
            if (
                from_class is not None
                and from_class.dynamic_layout
                and attr.name != "__new__"
            ):
                # Its bases are run-time values: no compile-time parent.
                return self._emit_dynamic_super_call(expr, attr.name, from_class, super_args)
            if from_class is None and len(super_args) == 2 and attr.name != "__new__":
                return self._emit_dynamic_super_call(expr, attr.name, None, super_args)
            parent_info = (
                self._resolve_super_method(from_class, attr.name)
                if from_class is not None
                else None
            )
            if parent_info is not None:
                current_method_kind = (
                    getattr(self, "current_method_kind", None) or "instance"
                )
                if (
                    getattr(self, "current_func_def", None) is not None
                    and self.current_func_def.name in current_class.method_kinds
                ):
                    current_method_kind = current_class.method_kinds.get(
                        self.current_func_def.name,
                        current_method_kind,
                    )
                receiver_param = self._super_receiver_param(from_class)
                receiver_name = receiver_param.name if receiver_param is not None else ""
                if self._super_receiver_needs_runtime_check(
                    super_args, current_method_kind, receiver_name,
                ) or (
                    parent_info.method_kinds.get(attr.name, "instance") == "static"
                    and self.class_lowering.method_name_is_closure(parent_info, attr.name)
                ):
                    # CPython's supercheck decides from the receiver's value
                    # whether it is an instance or a class; a closure
                    # staticmethod's captures live in its published object.
                    return self._emit_dynamic_super_call(
                        expr, attr.name, from_class, super_args,
                        declared=(
                            parent_info.methods[attr.name],
                            self.class_lowering._find_method_def(parent_info.name, attr.name),
                        ),
                    )
                if len(super_args) == 2:
                    recv_expr = super_args[1]
                    recv_val = self._emit_expr(recv_expr)
                    recv_class_name = (
                        (
                            getattr(self, "env_class_object_hint", {}).get(
                                recv_expr.ident
                            )
                            or self._resolve_class_alias(recv_expr.ident)
                        )
                        if isinstance(recv_expr, Name)
                        else None
                    )
                    receiver_is_local_class_object = isinstance(
                        recv_expr, Name
                    ) and recv_expr.ident in getattr(self, "env_class_object_hint", {})
                    receiver_is_named_class = (
                        isinstance(recv_expr, Name)
                        and recv_class_name in self.class_lowering.classes
                        and (
                            recv_expr.ident not in self.env
                            or receiver_is_local_class_object
                        )
                    )
                    receiver_is_class = (
                        current_method_kind == "classmethod"
                        and isinstance(recv_expr, Name)
                        and recv_expr.ident == receiver_name
                    ) or receiver_is_named_class
                else:
                    recv_slot = self.env.get(receiver_name)
                    if recv_slot is None:
                        raise L1CodegenError(
                            f"super() receiver {receiver_name!r} not bound in "
                            f"{self.current_func_def.name!r}"
                        )
                    recv_val = self.builder.load(
                        recv_slot[0], name=self._fresh(receiver_name)
                    )
                    receiver_is_class = current_method_kind == "classmethod"
                super_start_cls = self.class_lowering.emit_super_start_class(
                    recv_val,
                    receiver_is_class,
                )
                method_ptr = self.class_lowering.emit_super_lookup_from_start(
                    from_class,
                    super_start_cls,
                    attr.name,
                    getattr(expr, "span", None),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                kind = parent_info.method_kinds.get(attr.name, "instance")
                if kind == "static":
                    method_fn = parent_info.methods[attr.name]
                    return self._emit_static_method_ptr_call(
                        method_ptr,
                        method_fn,
                        parent_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        result_slot=self._slot_call_result_sink(expr),
                    )
                if kind == "classmethod":
                    method_fn = parent_info.methods[attr.name]
                    return self._emit_direct_method_ptr_call(
                        method_ptr,
                        method_fn,
                        super_start_cls,
                        parent_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        result_slot=self._slot_call_result_sink(expr),
                    )
                method_fn = parent_info.methods[attr.name]
                return self._emit_direct_method_ptr_call(
                    method_ptr,
                    method_fn,
                    recv_val,
                    parent_info,
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    result_slot=self._slot_call_result_sink(expr),
                )
            if from_class is not None and attr.name not in ("__init__", "__new__"):
                # No compile-time class defines the name: resolve it in the
                # receiver's MRO when the call runs, as CPython does. The
                # runtime path evaluates the receiver once and raises
                # CPython's RuntimeError for a zero-argument super() in a
                # function without parameters.
                return self._emit_dynamic_super_call(expr, attr.name, from_class, super_args)
            if attr.name in ("__init__", "__new__"):
                if attr.name == "__init__":
                    base = self._foreign_super_initializer_base(from_class)
                    if base == "dict" or base == "BaseException":
                        return self._emit_foreign_super_init_slots(
                            expr, from_class, super_args, base,
                        )
                    if base == "object" and not super_args and not expr.args and not expr.kwargs:
                        return self._emit_none_literal()
                    if base == "object" or self._super_mro_is_runtime_complete(from_class):
                        # Every base is in the run-time MRO, which ends at
                        # object's namespace: CPython's lookup and errors.
                        return self._emit_dynamic_super_call(expr, attr.name, from_class, super_args)
                raise L1CodegenError(
                    "native super()." + attr.name
                    + " has no implemented initializer for this foreign base"
                )

        # Case 1: ``self.method(...)`` inside a method body of the
        # currently-lowered class. Try the method on the class itself,
        # then walk the declared bases.
        if (
            current_class is not None
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "self"
        ):
            receiver_class_name = self._self_receiver_class_name()
            if receiver_class_name is None:
                receiver_class_name = current_class.name
            method_info = self._resolve_method_mro(receiver_class_name, attr.name)
            if method_info is not None:
                if attr.name not in method_info.methods:
                    return self._emit_overridden_method_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                receiver_info = self.class_lowering.classes.get(
                    receiver_class_name
                )
                # This body is emitted once and shared by ``current_class``
                # and every subclass that inherits it, so the runtime
                # receiver is any class in that subtree -- not just the one
                # inference pinned to ``self``.  ``_SHA256.hexdigest`` was
                # lowered with ``self`` inferred as ``_SHA224`` (which
                # inherits it), passed the override check because nothing
                # derives from ``_SHA224``, and emitted a direct
                # ``bl _SHA224.digest``.  A ``_SHA256`` receiver then ran it
                # and got a 28-byte digest where 32 was due -- silently, and
                # for every caller of ``hashlib.sha256(...).hexdigest()``.
                # Direct dispatch is sound only when every class the body can
                # run under resolves the name to the same function.
                override_roots = [receiver_info]
                if current_class is not None and current_class is not receiver_info:
                    override_roots.append(current_class)
                if any(
                    root is not None
                    and self.class_lowering.method_overridden_by_subclass(
                        root, attr.name
                    )
                    for root in override_roots
                ):
                    return self._emit_overridden_method_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                if current_class is not None and current_class is not receiver_info:
                    lexical_info = self._resolve_method_mro(
                        current_class.name, attr.name
                    )
                    if lexical_info is not method_info:
                        return self._emit_overridden_method_call(
                            attr.obj,
                            attr.name,
                            expr.args,
                            expr.kwargs,
                            expr.span,
                            call_expr=expr,
                        )
                if self.class_lowering.method_name_is_closure(method_info, attr.name):
                    return self._emit_closure_method_call(
                        attr.obj, attr.name, expr.args, expr.kwargs, expr.span, expr,
                    )
                kind = method_info.method_kinds.get(attr.name, "instance")
                if kind == "static":
                    # ``self.static_method(args)`` — Python lets you
                    # call staticmethods via the instance; drop the
                    # self receiver and dispatch as a plain class call.
                    method_fn = method_info.methods[attr.name]
                    return self._emit_static_method_call(
                        method_fn,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                if kind == "classmethod":
                    # A classmethod reached through ``self`` binds to the
                    # receiver's class (``type(self)``), or to the receiver
                    # itself inside a classmethod.
                    current_kind = getattr(self, "current_method_kind", None) or "instance"
                    if (
                        getattr(self, "current_func_def", None) is not None
                        and self.current_func_def.name in current_class.method_kinds
                    ):
                        current_kind = current_class.method_kinds.get(
                            self.current_func_def.name, current_kind,
                        )
                    receiver = self._emit_expr(attr.obj)
                    cls_ptr = self.class_lowering.emit_super_start_class(
                        receiver, current_kind == "classmethod",
                    )
                    self._emit_post_call_err_check(getattr(expr, "span", None))
                    method_fn = method_info.methods[attr.name]
                    return self._emit_direct_method_call(
                        method_fn,
                        cls_ptr,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                self_val = self._emit_direct_method_receiver(attr.obj, method_info, attr.name)
                method_fn = method_info.methods[attr.name]
                try:
                    return self._emit_direct_method_call(
                        method_fn,
                        self_val,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                except L1CodegenError as e:
                    if "too many positional args" not in str(e):
                        raise
                    return self._emit_static_method_call(
                        method_fn,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )

        # Case 1b: ``cls.method(...)`` inside a ``@classmethod`` body.
        # ``cls`` is the class itself, so dispatch exactly like
        # ``ClassName.method`` — respect the target method's kind and
        # pass the class pointer as the receiver for classmethods /
        # drop it for staticmethods.
        if (
            current_class is not None
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "cls"
        ):
            method_info = self._resolve_method_mro(current_class.name, attr.name)
            if method_info is not None:
                if self.class_lowering.method_overridden_by_subclass(
                    current_class, attr.name
                ):
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                kind = method_info.method_kinds.get(attr.name, "instance")
                if kind == "instance":
                    cls_ptr = self.builder.load(
                        method_info.global_var,
                        name=self._fresh(".cls.recv"),
                    )
                    self._note_global_backed_value(cls_ptr, method_info.global_var)
                    method_fn = method_info.methods[attr.name]
                    try:
                        return self._emit_direct_method_call(
                            method_fn,
                            cls_ptr,
                            method_info,
                            attr.name,
                            expr.args,
                            kwargs=expr.kwargs,
                            park_expr=expr,
                        )
                    except L1CodegenError as e:
                        if "too many positional args" not in str(e):
                            raise
                        return self._emit_static_method_call(
                            method_fn,
                            method_info,
                            attr.name,
                            expr.args,
                            kwargs=expr.kwargs,
                            park_expr=expr,
                        )
                if kind == "static":
                    method_fn = method_info.methods[attr.name]
                    return self._emit_static_method_call(
                        method_fn,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                if kind == "classmethod":
                    cls_ptr = self.builder.load(
                        method_info.global_var,
                        name=self._fresh(".cls.recv"),
                    )
                    self._note_global_backed_value(cls_ptr, method_info.global_var)
                    method_fn = method_info.methods[attr.name]
                    return self._emit_direct_method_call(
                        method_fn,
                        cls_ptr,
                        method_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                # Instance method called via ``cls`` — pcc can't
                # construct a bound instance, so treat as a static
                # dispatch on the class. Matches CPython's
                # ``ClassName.instance_method(instance, …)`` when the
                # user reaches for it this way; the first arg is then
                # the real ``self``.
                method_fn = method_info.methods[attr.name]
                return self._emit_static_method_call(
                    method_fn,
                    method_info,
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    park_expr=expr,
                )

        # Case 2: ``ClassName.method(...)`` — direct static/classmethod
        # dispatch on a bare class reference (no instance).
        obj_ident = _method_ident(attr.obj)
        if _method_is_name(attr.obj) and obj_ident in self.class_lowering.classes:
            class_info = self.class_lowering.classes[obj_ident]
            info = self._resolve_method_mro(obj_ident, attr.name)
            class_attr_known = (
                self.class_lowering.lookup_class_attr(class_info, attr.name) is not None
            )
            class_attr_state = getattr(
                self,
                "_class_attr_runtime_state",
                {},
            ).get((class_info.name, attr.name))
            class_attr_runtime_candidate = (
                class_attr_known
                or class_attr_state == "live"
                or class_attr_state == "unknown"
                or class_attr_state == "deleted"
            )
            if (
                class_attr_runtime_candidate
                and info is not None
                and attr.name in info.methods
            ):
                callable_obj = self._emit_attr(attr)
                kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
                arg_exprs = expr.args
                kwargs_expr = None
                if kwdict_unpack is not None:
                    arg_exprs, kwargs_expr = kwdict_unpack
                args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
                kwargs_obj = self._emit_dynamic_call_kwargs_object(
                    expr.kwargs,
                    kwargs_expr,
                    expr.span,
                )
                result = self.builder.call(
                    self.runtime["py_obj_call"],
                    [callable_obj, args_tuple, kwargs_obj],
                    name=self._fresh(f"callable.attr.{attr.name}.call"),
                )
                self._gc_release(args_tuple)
                if expr.kwargs:
                    self._gc_release(kwargs_obj)
                self._gc_release(callable_obj)
                self._emit_post_call_err_check(expr.span)
                return result
            if class_attr_runtime_candidate:
                return self._emit_callable_attribute_call(
                    attr.obj,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                    expr.span,
                    call_expr=expr,
                )
            call_receiver = None
            if info is None:
                metaclass_name = getattr(class_info, "metaclass_name", None)
                if metaclass_name is not None:
                    meta_info = self.class_lowering.classes.get(metaclass_name)
                    if meta_info is not None and attr.name in meta_info.methods:
                        info = meta_info
                        call_receiver = self.builder.load(
                            class_info.global_var,
                            name=self._fresh(".meta.cls.recv"),
                        )
                        self._note_global_backed_value(call_receiver, class_info.global_var)
            if info is not None:
                kind = info.method_kinds.get(attr.name, "instance")
                if (
                    kind == "instance"
                    and _method_first_arg_name(
                        self.class_lowering,
                        info.name,
                        attr.name,
                    )
                    == "cls"
                ):
                    kind = "classmethod"
                if kind == "static":
                    method_fn = info.methods[attr.name]
                    return self._emit_static_method_call(
                        method_fn,
                        info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                if kind == "classmethod":
                    if call_receiver is not None:
                        cls_ptr = call_receiver
                    else:
                        cls_ptr = self.builder.load(
                            class_info.global_var,
                            name=self._fresh(".cls.recv"),
                        )
                        self._note_global_backed_value(cls_ptr, class_info.global_var)
                    method_fn = info.methods[attr.name]
                    return self._emit_direct_method_call(
                        method_fn,
                        cls_ptr,
                        info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                # Explicit base-class instance dispatch such as
                # ``Base.method(self, ...)`` should pass the caller's
                # explicit receiver as the first positional argument.
                method_fn = info.methods[attr.name]
                if call_receiver is not None:
                    return self._emit_direct_method_call(
                        method_fn,
                        call_receiver,
                        info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                return self._emit_static_method_call(
                    method_fn,
                    info,
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    park_expr=expr,
                )
            # ``KnownClass.method(...)`` where the method is not resolvable
            # on the natively-known MRO (e.g. the base class is
            # CPython-backed, as in numpy's ``GnuFCompiler(FCompiler)``).
            # The receiver is a CLASS object, so the name-scan instance
            # fallbacks below must not run — they would match an unrelated
            # same-named method and pass the class as ``self``. Dispatch
            # dynamically instead: getattr on the class object, then call
            # with all explicit args (CPython unbound-call semantics).
            extern_ret = self._maybe_emit_class_lowering_extern_method(expr)
            if extern_ret is not None:
                return extern_ret
            return self._emit_callable_attribute_call(
                attr.obj,
                attr.name,
                expr.args,
                expr.kwargs,
                expr.span,
                call_expr=expr,
            )

        native_external = self._maybe_emit_class_lowering_extern_method(expr)
        if native_external is not None:
            return native_external

        if isinstance(attr.obj, Attr):
            class_export = self._native_module_expr_export_info(
                attr.obj.obj,
                attr.obj.name,
            )
            if (
                class_export is not None
                and isinstance(class_export[1], dict)
                and class_export[1].get("kind") == "class"
            ):
                receiver_hint = self._class_hint_for_expr(attr.obj)
                if receiver_hint is not None:
                    info = self._resolve_method_mro(receiver_hint, attr.name)
                    if info is not None and attr.name in info.methods:
                        kind = info.method_kinds.get(attr.name, "instance")
                        method_fn = info.methods[attr.name]
                        if kind == "static":
                            return self._emit_static_method_call(
                                method_fn,
                                info,
                                attr.name,
                                expr.args,
                                kwargs=expr.kwargs,
                                park_expr=expr,
                            )
                        if kind == "classmethod":
                            cls_ptr = self.builder.load(
                                info.global_var,
                                name=self._fresh(".cls.recv"),
                            )
                            self._note_global_backed_value(cls_ptr, info.global_var)
                            return self._emit_direct_method_call(
                                method_fn,
                                cls_ptr,
                                info,
                                attr.name,
                                expr.args,
                                kwargs=expr.kwargs,
                                park_expr=expr,
                            )
                        return self._emit_static_method_call(
                            method_fn,
                            info,
                            attr.name,
                            expr.args,
                            kwargs=expr.kwargs,
                            park_expr=expr,
                        )

        # Case 3: ``other_obj.method(...)`` — first try the class hint
        # recorded at assignment time, walking up the MRO of that
        # class for the first definition of the method. Fall back to
        # the first class in the module that declares the method so
        # single-class programs keep working when the hint is missing.
        if isinstance(attr.obj, Name):
            hint = self.env_class_hint.get(attr.obj.ident)
            if hint is not None:
                info = self._resolve_method_mro(hint, attr.name)
                if info is not None:
                    if attr.name not in info.methods:
                        return self._emit_callable_attribute_call(
                            attr.obj,
                            attr.name,
                            expr.args,
                            expr.kwargs,
                            expr.span,
                            call_expr=expr,
                        )
                    receiver_info = self.class_lowering.classes.get(hint)
                    if (
                        receiver_info is not None
                        and self.class_lowering.method_overridden_by_subclass(
                            receiver_info, attr.name
                        )
                    ):
                        return self._emit_callable_attribute_call(
                            attr.obj,
                            attr.name,
                            expr.args,
                            expr.kwargs,
                            expr.span,
                            call_expr=expr,
                        )
                    kind = info.method_kinds.get(attr.name, "instance")
                    if kind == "static":
                        method_fn = info.methods[attr.name]
                        return self._emit_static_method_call(
                            method_fn,
                            info,
                            attr.name,
                            expr.args,
                            kwargs=expr.kwargs,
                            park_expr=expr,
                        )
                    obj_val = self._emit_direct_method_receiver(attr.obj, info, attr.name)
                    if kind == "classmethod":
                        obj_val = self.builder.load(
                            info.global_var, name=self._fresh(".cls.recv")
                        )
                    method_fn = info.methods[attr.name]
                    return self._emit_direct_method_call(
                        method_fn,
                        obj_val,
                        info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                class_info = self.class_lowering.classes.get(hint)
                if class_info is not None and self._class_attr_needs_runtime_lookup(
                    class_info, attr.name
                ):
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )

        receiver_hint = self._class_hint_for_expr(attr.obj)
        if receiver_hint is not None:
            info = self._resolve_method_mro(receiver_hint, attr.name)
            if info is not None:
                if attr.name not in info.methods:
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                receiver_info = self.class_lowering.classes.get(receiver_hint)
                if (
                    receiver_info is not None
                    and self.class_lowering.method_overridden_by_subclass(
                        receiver_info, attr.name
                    )
                ):
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                kind = info.method_kinds.get(attr.name, "instance")
                if kind == "static":
                    self._emit_expr(attr.obj)
                    method_fn = info.methods[attr.name]
                    return self._emit_static_method_call(
                        method_fn,
                        info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )
                obj_val = self._emit_direct_method_receiver(attr.obj, info, attr.name)
                if kind == "classmethod":
                    obj_val = self.builder.load(
                        info.global_var, name=self._fresh(".cls.recv")
                    )
                method_fn = info.methods[attr.name]
                return self._emit_direct_method_call(
                    method_fn,
                    obj_val,
                    info,
                    attr.name,
                    expr.args,
                    kwargs=expr.kwargs,
                    park_expr=expr,
                )
            class_info = self.class_lowering.classes.get(receiver_hint)
            if class_info is not None:
                # No class in the receiver's MRO declares the method, so it
                # is looked up on the receiver's run-time type (a subclass,
                # object's namespace, __getattr__), as CPython does. Another
                # class that happens to declare the name is not this
                # receiver's method.
                return self._emit_callable_attribute_call(
                    attr.obj,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                    expr.span,
                    call_expr=expr,
                )

        if isinstance(attr.obj, Name):
            # Last closed-world fallback: any class declaring the method.
            # Keep this after receiver_hint so typed loop variables like
            # ``for gv in self._globals: gv.render()`` dispatch to the
            # receiver's class instead of the first same-named method found.
            # Guard: if the receiver has a known primitive shape
            # (set/list/dict/str/...) the dispatch above already handled
            # it (or returned None on purpose). Falling through to "any
            # class with that method name" would let an unrelated
            # registered class (e.g. ``ir.IRBuilder.add``) shadow the
            # primitive's method, which is wrong now that more
            # cross-module classes are exported.
            receiver_is_unhinted_module_global = (
                attr.obj.ident in getattr(self, "_module_globals", {})
                and self.env_class_hint.get(attr.obj.ident) is None
                and attr.obj.ident not in self.class_lowering.classes
            )
            receiver_is_module_alias = (
                attr.obj.ident in getattr(self, "_native_module_aliases", {})
                or attr.obj.ident in getattr(self, "_native_module_object_aliases", {})
                or self._native_builtin_module_for_name(attr.obj.ident) is not None
            )
            obj_ty_for_guard = attr.obj.ty
            # A container receiver's method never belongs to a user class, even
            # when exactly one class defines a method of that name: ``s.clear()``
            # on a set called ``Bag.clear`` with the set as ``self``.  Scalar
            # and None types stay out of this list: a global initialised to
            # None and rebound later still infers as NoneType, and its method
            # calls resolve through the unique class (the self-hosted backend's
            # instruction-record buffer is one such receiver).
            if isinstance(
                obj_ty_for_guard,
                (
                    ListType,
                    DictType,
                    TupleType,
                    StrType,
                    BytesType,
                    ByteArrayType,
                    MemoryViewType,
                    SetType,
                ),
            ):
                obj_ty_for_guard = None
            elif isinstance(obj_ty_for_guard, DynType) and (
                attr.name in _DYN_LIST_METHOD_NATIVE
                or attr.name in _DYN_DICT_METHOD_NATIVE
                or attr.name in _DYN_SET_METHOD_NATIVE
                or attr.name in _STR_METHOD_NATIVE
            ):
                obj_ty_for_guard = None
            elif isinstance(obj_ty_for_guard, DynType) and obj_ty_for_guard.name in (
                "set",
                "frozenset",
            ):
                obj_ty_for_guard = None
            elif isinstance(obj_ty_for_guard, DynType):
                obj_ty_for_guard = None
            else:
                obj_ty_for_guard = "ok"
            if (
                obj_ty_for_guard is not None
                and not receiver_is_unhinted_module_global
                and not receiver_is_module_alias
            ):
                candidate_info = None
                candidate_count = 0
                for info in self.class_lowering.classes.values():
                    if attr.name in info.methods:
                        candidate_info = info
                        candidate_count += 1
                        if candidate_count > 1:
                            break
                if candidate_count > 1:
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
                if candidate_info is not None:
                    kind = candidate_info.method_kinds.get(
                        attr.name, "instance"
                    )
                    if kind == "static":
                        method_fn = candidate_info.methods[attr.name]
                        return self._emit_static_method_call(
                            method_fn,
                            candidate_info,
                            attr.name,
                            expr.args,
                            kwargs=expr.kwargs,
                            park_expr=expr,
                        )
                    obj_val = self._emit_direct_method_receiver(attr.obj, candidate_info, attr.name)
                    if kind == "classmethod":
                        obj_val = self.builder.load(
                            candidate_info.global_var,
                            name=self._fresh(".cls.recv"),
                        )
                        self._note_global_backed_value(obj_val, candidate_info.global_var)
                    method_fn = candidate_info.methods[attr.name]
                    return self._emit_direct_method_call(
                        method_fn,
                        obj_val,
                        candidate_info,
                        attr.name,
                        expr.args,
                        kwargs=expr.kwargs,
                        park_expr=expr,
                    )

        if (
            attr.name in ("__init__", "__new__")
            and isinstance(attr.obj, Name)
            and attr.obj.ident in getattr(self, "_module_globals", {})
            and self.env_class_hint.get(attr.obj.ident) is None
            and attr.obj.ident not in self.class_lowering.classes
        ):
            return self._emit_none_literal()

        # Last-resort CPython fallback: when the receiver is a DynType
        # value (typical for foreign / imported-module classes whose
        # annotations such as ``llvm.ModuleRef`` resolve to DynType at
        # type-inference time), dispatch the method via
        # ``PyObject_CallMethod``. This unlocks ``module.verify()``
        # and similar idioms without requiring a CPython-class registry
        # on the pcc side.
        #
        # Typed containers (list / dict / tuple / str) deliberately do
        # *not* fall through here: pcc's pure-self-host story requires
        # that typed-collection methods stay on pcc-native runtime paths
        # so the produced binary has no libpython dependency. Missing
        # methods there surface as NotImplementedError so we can add a
        # dedicated fast path rather than silently pulling libpython in.
        obj_ty = attr.obj.ty
        expanded_int_bytes_call = False
        if attr.name == "to_bytes":
            expanded_int_bytes_call = self._has_unpacked_operands(expr.args, expr.kwargs)
        if (
            attr.name == "to_bytes"
            and isinstance(obj_ty, (IntType, DynType, BoolType))
            and not expanded_int_bytes_call
        ):
            # One binder for inferred ints and dynamic integer results. Keep
            # the receiver and all source-order operands owned and pinned while
            # the runtime performs __index__ and signed.__bool__ conversion.
            operands = [attr.obj] + list(expr.args)
            length_index = 1 if expr.args else -1
            order_index = 2 if len(expr.args) >= 2 else -1
            signed_index = -1
            binding_error = ""
            if len(expr.args) > 2:
                binding_error = "to_bytes() takes at most 2 positional arguments"
            for keyword, operand in expr.kwargs:
                index = len(operands)
                operands.append(operand)
                if keyword == "length":
                    if length_index >= 0:
                        binding_error = "to_bytes() got multiple values for argument 'length'"
                    length_index = index
                elif keyword == "byteorder":
                    if order_index >= 0:
                        binding_error = "to_bytes() got multiple values for argument 'byteorder'"
                    order_index = index
                elif keyword == "signed":
                    if signed_index >= 0:
                        binding_error = "to_bytes() got multiple values for argument 'signed'"
                    signed_index = index
                else:
                    binding_error = "to_bytes() got an unexpected keyword argument '" + keyword + "'"
            if length_index < 0:
                length_index = len(operands)
                operands.append(IntLit(span=expr.span, ty=IntType(name="int"), value=1))
            if order_index < 0:
                order_index = len(operands)
                operands.append(StrLit(span=expr.span, ty=StrType(name="str"), value="big"))
            if signed_index < 0:
                signed_index = len(operands)
                operands.append(BoolLit(span=expr.span, ty=BoolType(name="bool"), value=False))
            roots = []
            previous = self._current_try_err_block()
            previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
            target = previous if previous is not None else self._ensure_fn_err_exit()
            leases = []
            try:
                for operand in operands:
                    cleanup = self._extern_cleanup_block(tuple(roots), target)
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    value = self._emit_expr_with_cpy_operand_cleanup(
                        operand, (), as_pcc_object=True,
                    )
                    roots.append(self._extern_enter_root(
                        value, self._owned_release_needed(value, operand), "to_bytes.argument.root",
                    ))
                cleanup = self._extern_cleanup_block(tuple(roots), target)
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                if binding_error:
                    message = self._pooled_cstr_ptr(binding_error, ".to_bytes.binding_error")
                    exc = self.builder.call(
                        self.runtime["py_exc_new"], [ir.Constant(ir.IntType(64), 3), message],
                        name=self._fresh("to_bytes.binding.exc"),
                    )
                    self.builder.call(self.runtime["py_raise"], [exc])
                    self._gc_release(exc)
                    self._emit_post_call_err_check(expr.span)
                    self._extern_release_roots(tuple(roots))
                    return self._emit_none_literal()
                # A traced slot follows moves while evaluating later operands.
                # Counted leases additionally protect raw ABI arguments across
                # callbacks that clear an aliased object's Boolean pin flag.
                for root in roots:
                    acquired = self.builder.call(
                        self.runtime["pcc_gc_foreign_lease_acquire"],
                        [self._as_gc_ptr(root[0])], name=self._fresh("to_bytes.lease.acquire"),
                    )
                    failed = self.builder.icmp_signed("<", acquired, ir.Constant(ir.IntType(64), 0))
                    error = self.current_function.append_basic_block(self._fresh("to_bytes.lease.error"))
                    ready = self.current_function.append_basic_block(self._fresh("to_bytes.lease.ready"))
                    self.builder.cbranch(failed, error, ready)
                    self.builder.position_at_end(error)
                    cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    overflow = self.current_function.append_basic_block(self._fresh("bytes.lease.overflow"))
                    invalid = self.current_function.append_basic_block(self._fresh("bytes.lease.invalid"))
                    self.builder.cbranch(
                        self.builder.icmp_signed("==", acquired, ir.Constant(ir.IntType(64), -2)), overflow, invalid,
                    )
                    self.builder.position_at_end(overflow)
                    self._emit_builtin_exception_and_branch(
                        "OverflowError", "integer byte conversion address lease overflow", expr.span,
                    )
                    self.builder.position_at_end(invalid)
                    self._emit_builtin_exception_and_branch(
                        "RuntimeError", "integer byte conversion requires a stable managed owner", expr.span,
                    )
                    self.builder.position_at_end(ready)
                    leases.append((root, acquired))
                cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                result = self.builder.call(
                    self.runtime["py_int_to_bytes_args"],
                    [self._extern_load_root(roots[0]), self._extern_load_root(roots[length_index]),
                     self._extern_load_root(roots[order_index]), self._extern_load_root(roots[signed_index])],
                    name=self._fresh("int.to_bytes"),
                )
                self._emit_post_call_err_check(expr.span)
                result_root = self._extern_enter_root(result, True, "to_bytes.result.root")
                # Root teardown may call finalizers and move the result. The
                # shared take helper consumes the source owner exactly once,
                # then transfers the root owner after restoring prior pin state.
                prior = result_root[2]
                for root in reversed(roots):
                    alias = self.builder.icmp_unsigned("==", self._extern_load_root(result_root), self._extern_load_root(root))
                    prior = self.builder.select(alias, root[2], prior)
                result_root = (result_root[0], True, prior)
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = previous_cpy
            release_failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(release_failed, tuple(roots) + (result_root,))
            self._extern_release_roots(tuple(roots))
            result = self._extern_take_root(result_root)
            self._note_owned_object_value(result)
            return result
        if isinstance(obj_ty, DynType):
            if attr.name == "clear" and not expr.args and not expr.kwargs:
                self.builder.call(
                    self.runtime["py_obj_clear"],
                    [self._emit_as_object(attr.obj)],
                    name=self._fresh("obj.clear"),
                )
                # Any receiver other than list/dict/set/bytearray takes the
                # ordinary method call inside py_obj_clear and may raise.
                self._emit_post_call_err_check(expr.span)
                return self._emit_none_literal()
            native = self._maybe_emit_dict_method_via_dyn(expr)
            if native is not None:
                return native
            native = self._maybe_emit_list_method_via_dyn(expr)
            if native is not None:
                return native
            native = self._maybe_emit_set_method_via_dyn(expr)
            if native is not None:
                return native
            native = self._maybe_emit_bytes_method_via_dyn(expr)
            if native is not None:
                return native
            # DynType receiver: when the method is a known pcc-native
            # str helper, dispatch through the runtime (assumes the
            # value really is a str at runtime — matches CPython
            # behaviour which would raise AttributeError on type
            # mismatch; we emit a probable crash). Keeps the binary
            # libpython-free for the common ``DynType str result``
            # idiom (function return + splitlines / rstrip / …).
            native = self._maybe_emit_str_method_via_dyn(expr)
            if native is not None:
                return native
            # DynType numeric methods on a boxed value (e.g. a float from
            # py_obj_truediv, or a boxed int): handle .is_integer()/.bit_length()
            # natively before the getattr-based dynamic dispatch (which would
            # AttributeError, since boxed float/int have no method table).
            # py_float_is_integer / py_int_bit_length read the object by tag.
            # (Same "assume the runtime type matches the method" pragmatism as
            # the str_method_via_dyn path above.)
            if attr.name == "is_integer" and not expr.args and not expr.kwargs:
                obj = self._emit_as_object(attr.obj)
                i64v = self.builder.call(
                    self.runtime["py_float_is_integer"],
                    [obj],
                    name=self._fresh("dyn.is_integer"),
                )
                return self.builder.icmp_signed(
                    "!=",
                    i64v,
                    ir.Constant(_I64, 0),
                    name=self._fresh("dyn.is_integer.i1"),
                )
            if attr.name == "bit_length" and not expr.args and not expr.kwargs:
                obj = self._emit_as_object(attr.obj)
                return self.builder.call(
                    self.runtime["py_int_bit_length"],
                    [obj],
                    name=self._fresh("dyn.bit_length"),
                )
            if attr.name == "bit_count" and not expr.args and not expr.kwargs:
                obj = self._emit_as_object(attr.obj)
                return self.builder.call(
                    self.runtime["py_int_bit_count"],
                    [obj],
                    name=self._fresh("dyn.bit_count"),
                )
            if self._slot_call_result_sink(expr) is not None:
                return self._emit_slot_call_object(expr, "method." + attr.name)
            recv_obj = self._emit_as_object(attr.obj)
            if not expr.kwargs and self._split_starstar_kwargs_unpack(expr.args) is None:
                return self._emit_loaded_method_call(
                    recv_obj,
                    self._attr_name_ptr(attr.name),
                    attr.name,
                    expr.args,
                    expr.span,
                    attribute_error_span=attr.span,
                    dynamic_result=True,
                )
            method_obj = self.builder.call(
                self.runtime["py_obj_getattr"],
                [recv_obj, self._attr_name_ptr(attr.name)],
                name=self._fresh(f"dyn.attr.{attr.name}"),
            )
            self._emit_attribute_error_if_null(method_obj, attr.name, attr.span)
            kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
            arg_exprs = expr.args
            kwargs_expr = None
            if kwdict_unpack is not None:
                arg_exprs, kwargs_expr = kwdict_unpack
            args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
            kwargs_obj = self._emit_dynamic_call_kwargs_object(
                expr.kwargs,
                kwargs_expr,
                expr.span,
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [method_obj, args_tuple, kwargs_obj],
                name=self._fresh(f"dyn.method.{attr.name}"),
            )
            self._gc_release(args_tuple)
            if expr.kwargs or kwargs_expr is not None:
                self._gc_release(kwargs_obj)
            # `py_obj_getattr` returns a NEW reference on every path: fields and
            # `__dict__`/`__class__` incref, the dynamic-attr path goes through
            # `py_dict_get` (which increfs), a descriptor `__get__` yields an
            # owned result, and a plain method builds a fresh bound object via
            # `py_instance_bind_method`.  `py_obj_call` only borrows the
            # callable, so this frame owns `method_obj` and must consume it.
            # Without this release EVERY dynamic method call leaked its bound
            # method object: two million `obj.method()` calls whose method
            # returns None (so the result cannot be the leak) grew RSS to
            # 2.8 GB.  Release before the error check so the unwind path does
            # not leak it either.
            self._gc_release(method_obj)
            self._emit_post_call_err_check(expr.span)
            self._note_owned_dynamic_call_value(result)
            return result
        if (
            isinstance(obj_ty, ByteArrayType)
            and attr.name == "clear"
            and not expr.args
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            self.builder.call(self.runtime["py_bytearray_clear"], [recv])
            self._gc_release_if_owned(recv, attr.obj)
            self._emit_post_call_err_check(expr.span)
            return self._emit_none_literal()
        if (
            isinstance(obj_ty, ByteArrayType)
            and attr.name == "pop"
            and len(expr.args) <= 1
            and not expr.kwargs
        ):
            # bytearray.pop([index]) removes and returns the byte at index
            # (default last) as an int, shrinking the receiver in place. No-arg
            # form passes py_None; the runtime treats a None/non-int index as
            # "last element". Raises IndexError on empty / out-of-range.
            recv = self._emit_expr(attr.obj)
            if expr.args:
                index_obj = self._emit_as_object(expr.args[0])
            else:
                index_obj = self._emit_slice_bound_object(None)
            result = self.builder.call(
                self.runtime["py_bytearray_pop"],
                [recv, index_obj],
                name=self._fresh("bytearray.pop"),
            )
            self._emit_post_call_err_check(expr.span)
            if expr.args:
                self._gc_release_if_owned(index_obj, expr.args[0])
            return result
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "rfind"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            needle = self._emit_as_object(expr.args[0])
            fn = "py_bytes_find" if attr.name == "find" else "py_bytes_rfind"
            return self.builder.call(
                self.runtime[fn],
                [recv, needle],
                name=self._fresh(f"bytes.{attr.name}"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "find"
            and 1 <= len(expr.args) <= 3
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            arguments = [self._emit_as_object(arg) for arg in expr.args]
            live = [(recv, attr.obj)] + list(zip(arguments, expr.args))
            owned = tuple(value for value, source in live if self._owned_release_needed(value, source))
            begin = ir.Constant(_I64, 0)
            end = ir.Constant(_I64, 9223372036854775807)
            # Evaluate every argument before invoking __index__, matching
            # ordinary call evaluation even when a bound has side effects.
            if len(arguments) >= 2:
                begin = self.builder.call(self.runtime["py_slice_index_i64"],
                                          [arguments[1], begin], name=self._fresh("bytes.find.start"))
                self._emit_post_call_err_check(expr.span, release_on_error=owned)
            if len(arguments) == 3:
                end = self.builder.call(self.runtime["py_slice_index_i64"],
                                        [arguments[2], end], name=self._fresh("bytes.find.end"))
                self._emit_post_call_err_check(expr.span, release_on_error=owned)
            result = self.builder.call(
                self.runtime["py_bytes_find_range"],
                [recv, arguments[0], begin, end],
                name=self._fresh("bytes.find.range"),
            )
            for value, source in live:
                self._gc_release_if_owned(value, source)
            self._emit_post_call_err_check(expr.span)
            return result
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "count"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # bytes/bytearray .count(sub): number of non-overlapping matches of
            # a sub-bytes or single byte value. py_bytes_count reads the
            # receiver + needle via bytes_data()/byte_from_obj() (same as
            # find/rfind) and returns an i64 count without raising, so no
            # py_err_occurred() check is needed. The raw i64 is boxed as an int
            # object by the DynType-width-64 marshal path (expr.ty is DynType),
            # mirroring how bytes .find/.rfind results already flow.
            recv = self._emit_expr(attr.obj)
            needle = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_bytes_count"],
                [recv, needle],
                name=self._fresh("bytes.count"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name in ("startswith", "endswith")
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # bytes/bytearray .startswith/.endswith(prefix-or-tuple-of-prefixes).
            # py_str_startswith / py_str_endswith read the receiver and each
            # tuple element via stringlike_bytes() (handles bytes/bytearray) and
            # already implement the tuple-of-prefixes case; they return i64 0/1.
            # Box as a bool object (result type is unmodelled -> object).
            recv = self._emit_expr(attr.obj)
            needle = self._emit_as_object(expr.args[0])
            fn = {
                "startswith": "py_str_startswith",
                "endswith": "py_str_endswith",
            }[attr.name]
            i64v = self.builder.call(
                self.runtime[fn],
                [recv, needle],
                name=self._fresh(f"bytes.{attr.name}"),
            )
            bit = self.builder.icmp_signed(
                "!=",
                i64v,
                ir.Constant(_I64, 0),
                name=self._fresh(f"bytes.{attr.name}.bit"),
            )
            bit32 = self.builder.zext(
                bit,
                _I32,
                name=self._fresh(f"bytes.{attr.name}.i32"),
            )
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [bit32],
                name=self._fresh(f"bytes.{attr.name}.bool"),
            )
        if isinstance(obj_ty, (BytesType, ByteArrayType)) and attr.name == "decode":
            if not self._expr_looks_cpython(attr.obj):
                owned = self._emit_owned_bytes_decode(expr, False)
                if owned is not None:
                    return owned
            # decode() defaults to utf-8, and pcc str is utf-8 internally, so an
            # explicit "utf-8" encoding (+ optional supported errors mode) stays
            # in the native runtime. Other encodings / error modes fall back.
            encoding_arg = None
            errors_arg = None
            ok = True
            if len(expr.args) >= 1:
                encoding_arg = expr.args[0]
            if len(expr.args) >= 2:
                errors_arg = expr.args[1]
            if len(expr.args) > 2:
                ok = False
            for kname, kval in expr.kwargs or ():
                if kname == "encoding" and encoding_arg is None:
                    encoding_arg = kval
                elif kname == "errors" and errors_arg is None:
                    errors_arg = kval
                else:
                    ok = False
            if not ok:
                raise NotImplementedError(
                    "bytes.decode() accepts at most encoding and errors"
                )
            recv = self._emit_expr(attr.obj)
            operands = []
            if expr.args:
                operands.append(("encoding", expr.args[0]))
            if len(expr.args) == 2:
                operands.append(("errors", expr.args[1]))
            operands.extend(expr.kwargs)
            return self._emit_bytes_decode_call(recv, attr.obj, operands, expr.span)
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "join"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # b"sep".join(list_or_tuple_of_bytes) -> one O(n) allocation.
            # Publish into the consumer's root before retiring the input owners.
            if not self._expr_looks_cpython(attr.obj):
                return self._emit_owned_native_join_call(expr)
            recv = self._emit_expr(attr.obj)
            return self._emit_native_bytes_join(recv, expr.args[0], "bytes")
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "hex"
            and not expr.args
            and not expr.kwargs
        ):
            # bytes.hex() -> lowercase two-hex-digits-per-byte string, the
            # receiver an owned operand and the NEW str published before
            # cleanup ("B:" + data.hex()).
            return self._emit_owned_unary_runtime_call(expr, "py_bytes_hex")
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name in ("upper", "lower")
            and not expr.args
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            fn = "py_bytes_upper" if attr.name == "upper" else "py_bytes_lower"
            return self.builder.call(
                self.runtime[fn],
                [recv],
                name=self._fresh(f"bytes.{attr.name}"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name in ("ljust", "rjust")
            and len(expr.args) in (1, 2)
            and not expr.kwargs
        ):
            padding = self._emit_owned_padding_method(expr)
            if padding is not None:
                return padding
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and not expr.kwargs
            and (
                (attr.name in ("strip", "lstrip", "rstrip") and len(expr.args) <= 1)
                or (attr.name in ("ljust", "rjust") and len(expr.args) in (1, 2))
            )
        ):
            # These byte-family ABIs return NEW objects, including unchanged
            # values. Keep operands owned across later evaluation and width
            # callbacks; publish the result before releasing those owners.
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("bytes.method.result")
                roots.append(output)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            operands = []
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                for argument in (attr.obj,) + tuple(expr.args):
                    operand = self._emit_slot_call_operand(argument, "bytes.method.argument")
                    operands.append(operand)
                    roots.append(operand)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                runtime_name = "py_bytes_" + attr.name
                arguments = tuple(operands)
                suffix = ()
                order = ()
                if attr.name in ("ljust", "rjust"):
                    width = self.builder.call(
                        self.runtime["py_index_i64_checked_slots"],
                        [self._as_gc_ptr(operands[1])],
                        name=self._fresh("bytes.method.width"),
                    )
                    self._emit_post_call_err_check(expr.span)
                    arguments = (operands[0],)
                    suffix = (width, ir.Constant(ir.PointerType(ir.IntType(8)), None))
                    if len(expr.args) == 2:
                        arguments = (operands[0], operands[2])
                        suffix = (width,)
                        order = (0, 2, 1)
                elif expr.args:
                    runtime_name += "_chars"
                self._slot_call_runtime_call(
                    runtime_name, arguments, result_slot=output,
                    suffix_args=suffix, argument_order=order, span=expr.span,
                )
                self._release_slot_call_roots(tuple(operands))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh("bytes.method.current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "split"
            and len(expr.args) in (1, 2)
            and not expr.kwargs
        ):
            # Both split ABIs return a NEW list. Preserve the receiver and each
            # argument through later evaluation, then publish the list before
            # lease release, pending-error checks, or operand disposal.
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root("bytes.split.result")
                roots.append(output)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            operands = []
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                receiver = self._emit_slot_call_operand(attr.obj, "bytes.split.receiver")
                roots.append(receiver)
                operands.append(receiver)
                for index, argument in enumerate(expr.args):
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    label = "bytes.split.separator" if index == 0 else "bytes.split.limit"
                    operand = self._emit_slot_call_operand(argument, label)
                    roots.append(operand)
                    operands.append(operand)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                runtime_name = "py_bytes_split" if len(expr.args) == 1 else "py_bytes_split_max"
                self._slot_call_runtime_call(
                    runtime_name, tuple(operands), result_slot=output, span=expr.span,
                )
                self._guard_cpy_value_not_null(self.builder.load(output))
                self._release_slot_call_roots(tuple(operands))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup
            if sink is not None:
                return self.builder.load(output, name=self._fresh("bytes.split.output"))
            return self._take_slot_call_root(output)
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "partition"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # bytes/bytearray .partition(sep) -> (before, sep, after) tuple.
            recv = self._emit_expr(attr.obj)
            sep = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_bytes_partition"],
                [recv, sep],
                name=self._fresh("bytes.partition"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "translate"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            table_obj = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_bytes_translate"],
                [recv, table_obj],
                name=self._fresh("bytes.translate"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "replace"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            recv = self._emit_expr(attr.obj)
            old_obj = self._emit_as_object(expr.args[0])
            new_obj = self._emit_as_object(expr.args[1])
            result = self.builder.call(
                self.runtime["py_bytes_replace"],
                [recv, old_obj, new_obj],
                name=self._fresh("bytes.replace"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if isinstance(obj_ty, StrType):
            # Typed-str receiver with a method not on the pcc-native
            # fast path (``encode`` / ``rsplit`` / ``format`` / …).
            # Pcc's pure-self-host policy prefers a native helper here,
            # but to let the solo-compile survey advance we route the
            # unknown method through the libpython fallback and leave
            # a TODO. The emitted binary then links libpython for this
            # specific use. Revisit and add the helper once the call
            # shows up in the self-host critical path.
            native = self._maybe_emit_str_method_via_dyn(expr)
            if native is not None:
                return native
            raw_val = self._emit_expr(attr.obj)
            cpy_val, owned = self._marshal_to_cpython_consuming_source(
                raw_val,
                obj_ty,
                attr.obj,
            )
            result = self._emit_cpy_method_call_src(
                cpy_val,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
                receiver_owned=owned,
            )
            return result
        if isinstance(obj_ty, NoneType):
            # Flow-insensitive inference can label a mutable module global by
            # its initial ``None`` even after a guarded branch has installed a
            # native instance.  The slot, not that stale expression type, owns
            # the runtime value.  In a closed no-libpython build, dispatch it
            # through the native object model; an actual None still raises
            # AttributeError there.
            if (
                getattr(self, "_strict_no_libpython", False)
                and isinstance(attr.obj, Name)
                and attr.obj.ident in getattr(self, "_module_globals", {})
                and not getattr(self, "_cpy_env_flags", {}).get(
                    attr.obj.ident, False
                )
                and isinstance(
                    self._module_globals[attr.obj.ident][0].value_type,
                    ir.PointerType,
                )
            ):
                return self._emit_callable_attribute_call(
                    attr.obj,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                    expr.span,
                    call_expr=expr,
                )
            # Flow-insensitive inference can leave a guarded Optional[str]
            # receiver as ``NoneType`` inside branches like
            # ``x.strip() if x is not None else None``. Marshal the
            # runtime value to a CPython object and dispatch there so a
            # real str still works while an actual None preserves
            # CPython's AttributeError behavior.
            raw_val = self._emit_expr(attr.obj)
            cpy_val, owned = self._marshal_to_cpython_consuming_source(
                raw_val,
                obj_ty,
                attr.obj,
            )
            result = self._emit_cpy_method_call_src(
                cpy_val,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
                receiver_owned=owned,
            )
            return result
        if isinstance(obj_ty, (ListType, DictType, TupleType)):
            # Typed-collection receiver whose method isn't on the pcc-
            # native fast path (e.g. ``.reverse`` / ``.clear`` /
            # ``.update``). Fall through to the CPython fallback the
            # same way StrType does: pulls libpython for that specific
            # use but lets the solo-compile survey advance. Revisit
            # with a native helper when the call reaches the
            # self-host critical path.
            raw_val = self._emit_expr(attr.obj)
            cpy_val, owned = self._marshal_to_cpython_consuming_source(
                raw_val,
                obj_ty,
                attr.obj,
            )
            result = self._emit_cpy_method_call_src(
                cpy_val,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
                receiver_owned=owned,
            )
            return result
        if (
            isinstance(obj_ty, (FloatType, DynType))
            and attr.name == "is_integer"
            and not expr.args
            and not expr.kwargs
        ):
            # Native float.is_integer() — avoid the libpython fallback below so
            # it works under --python-libpython=off. Returns a bool (i1).
            # DynType is included so a boxed float from a dynamic expression
            # (e.g. ``(total / count).is_integer()`` where ``/`` is py_obj_truediv
            # -> a boxed float) works; py_float_is_integer reads the object via
            # py_float_to_f64 (float/int/bool). (A user class defining its own
            # is_integer would route here too, but that is vanishingly rare.)
            obj = self._emit_as_object(attr.obj)
            i64v = self.builder.call(
                self.runtime["py_float_is_integer"],
                [obj],
                name=self._fresh("float.is_integer"),
            )
            return self.builder.icmp_signed(
                "!=",
                i64v,
                ir.Constant(_I64, 0),
                name=self._fresh("float.is_integer.i1"),
            )
        if (
            isinstance(obj_ty, (IntType, DynType))
            and attr.name == "bit_length"
            and not expr.args
            and not expr.kwargs
        ):
            # Native int.bit_length() — avoid the libpython fallback so it works
            # under --python-libpython=off. Returns the bit count as an int (i64),
            # exact for bignums (py_int_bit_length). DynType included so a boxed
            # int from a dynamic expression works (py_int_bit_length reads the
            # object's tag: tagged int / bignum).
            obj = self._emit_as_object(attr.obj)
            return self.builder.call(
                self.runtime["py_int_bit_length"],
                [obj],
                name=self._fresh("int.bit_length"),
            )
        if (
            isinstance(obj_ty, (IntType, DynType))
            and attr.name == "bit_count"
            and not expr.args
            and not expr.kwargs
        ):
            # Native int.bit_count() — sibling of bit_length; avoid the libpython
            # fallback so it works under --python-libpython=off. Returns the
            # population count (set bits of abs(value)) as an int (i64), exact
            # for bignums (py_int_bit_count popcounts each limb). DynType included
            # so a boxed int from a dynamic expression works (py_int_bit_count
            # reads the object's tag: tagged int / bignum).
            obj = self._emit_as_object(attr.obj)
            return self.builder.call(
                self.runtime["py_int_bit_count"],
                [obj],
                name=self._fresh("int.bit_count"),
            )
        if isinstance(obj_ty, (IntType, FloatType, BoolType)):
            # Numeric method call (``int.to_bytes``, ``float.is_integer``,
            # ``bool.conjugate``, etc.) — box to a CPython object and
            # dispatch through the libpython fallback. Pulls libpython
            # for that specific use. The numeric value is marshalled
            # through the appropriate boxer so CPython sees a proper
            # Py_Long / Py_Float.
            raw_val = self._emit_expr(attr.obj)
            cpy_val, owned = self._marshal_to_cpython_consuming_source(
                raw_val,
                obj_ty,
                attr.obj,
            )
            return self._emit_cpy_method_call_src(
                cpy_val,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
                receiver_owned=owned,
            )
        if isinstance(obj_ty, ClassType):
            receiver_hint = self._class_hint_for_expr(attr.obj)
            if receiver_hint is not None:
                class_info = self.class_lowering.classes.get(receiver_hint)
                if class_info is not None:
                    # A mixin can call a helper supplied only by the runtime
                    # subclass. Its native receiver remains native even when
                    # the lexical class has no declaration for that helper.
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                        call_expr=expr,
                    )
            # An unresolved imported annotation shell still uses the explicit
            # compatibility path; it supplies no native class/domain evidence.
            if (self._slot_call_result_sink(expr) is not None
                    and not self._expr_looks_cpython(attr.obj)):
                return self._emit_slot_call_object(expr, "method." + attr.name)
            raw_val = self._emit_expr(attr.obj)
            if raw_val not in getattr(self, "_cpy_values", ()) and (
                getattr(self, "_strict_no_libpython", False)
                or obj_ty.name == "type"
            ):
                # No CPython value can reach a strict build, and ``type(x)``
                # of a native value is a native class (``type(cm).__aenter__
                # (cm)`` in async-with).  The compatibility path would stub
                # the whole function under --python-libpython=off.
                return self._emit_callable_attribute_call_on_value(
                    raw_val,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                    expr.span,
                )
            cpy_val, owned = self._marshal_to_cpython_consuming_source(
                raw_val,
                obj_ty,
                attr.obj,
            )
            result = self._emit_cpy_method_call_src(
                cpy_val,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
                operand_order=expr.operand_order,
                receiver_owned=owned,
            )
            return result

        return self._emit_callable_attribute_call(
            attr.obj,
            attr.name,
            expr.args,
            expr.kwargs,
            expr.span,
            call_expr=expr,
        )

    def _super_mro_is_runtime_complete(self, info):
        """Whether every base of ``info`` is an entry of its run-time MRO.

        User classes, ``object``, ``str``, ``tuple`` and the builtin
        exceptions are MRO entries; another builtin base (``list``, ``set``,
        ...) is dropped from the native class, so a super() lookup would skip
        its namespace.
        """
        if info is None:
            return False
        seen = set()
        pending = [info]
        while pending:
            current = pending.pop()
            if current.name in seen:
                continue
            seen.add(current.name)
            names = self.class_lowering._class_declared_base_names(current)
            if not names and getattr(current, "bases_ast", ()):
                return False
            for name in names:
                name = self._resolve_class_alias(name)
                parent = self.class_lowering.classes.get(name)
                if parent is not None:
                    pending.append(parent)
                elif name not in ("object", "str", "tuple") and builtin_exc_tag_or_missing(name) < 0:
                    return False
        return True

    def _foreign_super_initializer_base(self, info):
        """Identify a foreign terminal base, never infer it from call syntax.

        Native class metadata omits builtin bases. A single-inheritance chain
        is unambiguous here; mixed foreign MROs require a runtime builtin-base
        descriptor and must not silently acquire exception semantics.
        """
        seen = set()
        while info is not None and info.name not in seen:
            seen.add(info.name)
            names = self.class_lowering._class_declared_base_names(info)
            if not names:
                if getattr(info, "bases_ast", ()):
                    return None
                return "object"
            if len(names) != 1:
                return None
            name = self._resolve_class_alias(names[0])
            parent = self.class_lowering.classes.get(name)
            if parent is not None:
                info = parent
                continue
            if name == "dict" or name == "object":
                return name
            if builtin_exc_tag_or_missing(name) >= 0:
                return "BaseException"
            return None
        return None

    def _is_method_super_call(self, expr) -> bool:
        """Whether ``expr`` is ``super()`` or ``super(T, obj)`` in a method."""
        return (
            self.current_class is not None
            and isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == "super"
            and len(expr.args) in (0, 2)
            and not expr.kwargs
        )

    def _super_receiver_param(self, from_class):
        """The parameter zero-argument ``super()`` takes its receiver from.

        CPython uses the frame's first positional parameter; a function with
        none raises ``RuntimeError("super(): no arguments")``. A closure
        method's trailing capture parameters are not source parameters.
        """
        fd = getattr(self, "current_func_def", None)
        if fd is None:
            return None
        captures = ()
        if from_class is not None:
            captures, _sources = self.class_lowering.local_method_captures(from_class.name, fd)
        for arg in fd.args:
            if arg.name and arg.kind in ("pos", "pos_only") and arg.name not in captures:
                return arg
        return None

    def _super_receiver_needs_runtime_check(self, super_args, method_kind, receiver_name):
        """Whether only the receiver's run-time value decides its role.

        The direct path knows that an instance method's receiver is an
        instance and a classmethod's is a class. A staticmethod parameter or
        an arbitrary ``super(T, expr)`` operand can hold either, which
        CPython's supercheck tells apart when the call runs.
        """
        if not receiver_name or method_kind == "static":
            return True
        if len(super_args) == 2:
            receiver = super_args[1]
            return not (isinstance(receiver, Name) and receiver.ident == receiver_name)
        return False

    def _emit_super_bound_attribute(self, super_args, attr_name, from_class, span):
        """``super(...).name``: CPython's super lookup and binding, a NEW ref.

        Returns None after emitting ``RuntimeError("super(): no arguments")``
        for a zero-argument ``super()`` without a positional parameter. The
        name is mangled in the lexical class, as CPython compiles it.
        """
        owned_from = False
        if len(super_args) == 2:
            from_val = self._emit_expr(super_args[0])
            owned_from = self._owned_release_needed(from_val, super_args[0])
            recv_val = self._emit_expr(super_args[1])
        else:
            receiver_param = self._super_receiver_param(from_class)
            if receiver_param is None:
                self._emit_builtin_exception_and_branch("RuntimeError", "super(): no arguments", span)
                return None
            if self.env.get(receiver_param.name) is None:
                raise L1CodegenError(
                    "super() receiver " + receiver_param.name + " is not bound in "
                    + self.current_func_def.name
                )
            recv_val = self._emit_name(Name(span=span, ty=DynType(name="dyn"), ident=receiver_param.name))
            from_val, owned_from = self.class_lowering.emit_defining_class(from_class, span)
        runtime_name = attr_name
        if self.current_class is not None:
            runtime_name = self.class_lowering.mangle_private_attr_name(self.current_class, attr_name)
        bound = self.builder.call(
            self.runtime["py_super_getattr"],
            [ir.Constant(_CSTR, None), from_val, recv_val, self._attr_name_ptr(runtime_name)],
            name=self._fresh("super.attr." + attr_name),
        )
        if owned_from:
            self._gc_release(from_val, self._release_context_label("super.from"))
        self._emit_post_call_err_check(span)
        return bound

    def _emit_super_attribute_value(self, expr):
        """``super(...).name`` read as a value: the bound attribute."""
        from_class = self.current_class
        bound = self._emit_super_bound_attribute(expr.obj.args, expr.name, from_class, expr.span)
        if bound is None:
            return self._emit_none_literal()
        if isinstance(expr.ty, (IntType, FloatType, BoolType)):
            scalar = marshal.marshal_from_object(
                self.builder, self.module, self.runtime, bound, expr.ty,
            )
            self._gc_release(bound, self._release_context_label("super.attr." + expr.name))
            return scalar
        self._note_owned_dynamic_call_value(bound)
        return bound

    def _emit_dynamic_super_call(self, expr, attr_name, from_class, super_args, declared=None):
        """``super(...).name(args)`` resolved when it runs, as in CPython.

        For a parent that compiling cannot name: a class whose bases are
        run-time values, a name no declared base defines, or an explicit
        ``super(T, obj)`` whose ``T`` is not a known class; and for a known
        parent whose receiver role only its value decides. The runtime finds
        the start type from the receiver, searches the MRO after the defining
        class, binds the attribute and the call takes ordinary arguments.
        ``declared`` is the known parent's ``(function, FuncDef)``: its scalar
        result is unboxed as the direct call would return it.
        """
        span = getattr(expr, "span", None)
        bound = self._emit_super_bound_attribute(super_args, attr_name, from_class, span)
        if bound is None:
            return self._emit_none_literal()
        kwdict_unpack = self._split_starstar_kwargs_unpack(expr.args)
        arg_exprs = expr.args
        kwargs_expr = None
        if kwdict_unpack is not None:
            arg_exprs, kwargs_expr = kwdict_unpack
        args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
        kwargs_obj = self._emit_dynamic_call_kwargs_object(expr.kwargs, kwargs_expr, span)
        result = self.builder.call(
            self.runtime["py_obj_call_method_kwargs"],
            [bound, ir.Constant(_CSTR, None), args_tuple, kwargs_obj],
            name=self._fresh("super.call." + attr_name),
        )
        sink = self._slot_call_result_sink(expr)
        if sink is not None:
            # A consumer's operand root takes the NEW result at once.
            self._publish_slot_call_owned(sink, result, label="super call")
        self._gc_release(args_tuple)
        if expr.kwargs or kwargs_expr is not None:
            self._gc_release(kwargs_obj)
        self._gc_release(bound)
        self._emit_post_call_err_check(span)
        if sink is not None:
            return self.builder.load(sink, name=self._fresh("super.call.current"))
        if declared is not None:
            method_fn, method_fd = declared
            ret_ty = method_fn.function_type.return_type
            if method_fd is not None and not isinstance(ret_ty, (ir.PointerType, ir.VoidType)):
                scalar = marshal.marshal_from_object(
                    self.builder, self.module, self.runtime, result, method_fd.return_ty,
                )
                self._gc_release(result)
                return scalar
        self._note_owned_dynamic_call_value(result)
        return result

    def _emit_defining_class_into(self, origin, from_class, span):
        """Publish zero-argument super()'s ``__class__`` into ``origin``."""
        defining, owned = self.class_lowering.emit_defining_class(from_class, span)
        if owned:
            self._publish_slot_call_owned(origin, defining, label="super __class__")
            return
        borrowed = self._alloca_in_entry(_CSTR, name=self._fresh("super.class.cell"), init_null=True)
        self.builder.store(defining, borrowed)
        self._slot_call_copy_source(origin, borrowed, borrowed=True, span=span)

    def _emit_foreign_super_init_slots(self, expr, from_class, super_args, base):
        """Expand a builtin-base initializer into authoritative caller roots."""
        fd = getattr(self, "current_func_def", None)
        if not super_args and (fd is None or not fd.args):
            raise L1CodegenError("super() initializer has no receiver parameter")
        method_kind = getattr(self, "current_method_kind", None) or "instance"
        if self.current_class is not None and fd is not None:
            method_kind = self.current_class.method_kinds.get(fd.name, method_kind)
        if not super_args and method_kind == "static":
            self._emit_builtin_exception_and_branch("RuntimeError", "super(): no arguments", expr.span)
            return self._emit_none_literal()
        receiver_expr = (super_args[1] if super_args else Name(
            span=expr.span, ty=DynType(name="dyn"), ident=fd.args[0].name or "self",
        ))
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = []
        try:
            if super_args:
                origin = self._emit_slot_call_operand(super_args[0], "super.init.from")
            else:
                origin = self._new_slot_call_root("super.init.from")
            roots.append(origin)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if not super_args:
                self._emit_defining_class_into(origin, from_class, expr.span)
            receiver = self._emit_slot_call_operand(receiver_expr, "super.init.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            checked = self.builder.call(
                self.runtime["py_builtin_super_validate_slots"],
                [self._as_gc_ptr(receiver), self._as_gc_ptr(origin)],
                name=self._fresh("super.init.validate"),
            )
            self._slot_call_check_status(checked, "super binding", expr.span)
            self._emit_post_call_err_check(expr.span)
            positional, keywords = self._slot_call_split_operands(expr)
            args = self._emit_slot_call_args_tuple(positional, "super.init.args")
            roots.append(args)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            kwargs = self._emit_slot_call_kwargs_object(keywords, None, expr.span, "super.init.kwargs")
            roots.append(kwargs)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            helper = ("py_dict_subclass_init_slots" if base == "dict"
                      else "py_exception_subclass_init_slots")
            status = self.builder.call(
                self.runtime[helper],
                [self._as_gc_ptr(receiver), self._as_gc_ptr(origin),
                 self._as_gc_ptr(args), self._as_gc_ptr(kwargs)],
                name=self._fresh("super.init.invoke"),
            )
            self._slot_call_check_status(status, "builtin-base initialization", expr.span)
            self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots(tuple(roots))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return self._emit_none_literal()


    def _class_has_tuple_base(self, info):
        from pcc.frontends.python.pipeline_exports import annotation_module_bindings

        # Method bodies can be emitted before runtime import statements have
        # populated value aliases. Reuse the existing source-verified import
        # binding analysis; unknown/rebound globals stay conservative.
        bindings = annotation_module_bindings(self.ast_module)
        pending = [info]
        seen = set()
        while pending:
            current = pending.pop()
            if current.name in seen:
                continue
            seen.add(current.name)
            for base in self.class_lowering._class_declared_base_names(current):
                name = self._resolve_class_alias(base)
                parent = self.class_lowering.classes.get(name)
                if parent is not None:
                    pending.append(parent)
                elif (self._native_builtin_value_for_name(base) == "builtins.tuple"
                        or bindings.get(base) == "builtins.tuple"):
                    return True
        return False

    def _emit_tuple_super_new_slots(self, expr, from_class, super_args):
        fd = getattr(self, "current_func_def", None)
        if not super_args and (fd is None or not fd.args):
            raise L1CodegenError("super() allocator has no receiver parameter")
        if len(super_args) not in (0, 2):
            raise L1CodegenError("super() allocator requires zero or two arguments")
        receiver_expr = super_args[1] if super_args else Name(
            span=expr.span, ty=DynType(name="dyn"), ident=fd.args[0].name,
        )
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        output = self._new_slot_call_root("super.new.result")
        roots = [output]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if super_args:
                origin = self._emit_slot_call_operand(super_args[0], "super.new.from")
            else:
                origin = self._new_slot_call_root("super.new.from")
            roots.append(origin)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if not super_args:
                self._emit_defining_class_into(origin, from_class, expr.span)
            receiver = self._emit_slot_call_operand(receiver_expr, "super.new.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            callable_root = self._new_slot_call_root("super.new.callable")
            roots.append(callable_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self.builder.call(
                self.runtime["py_super_new_lookup_slots"],
                [self._as_gc_ptr(receiver), self._as_gc_ptr(origin), self._as_gc_ptr(callable_root)],
                name=self._fresh("super.new.lookup"),
            )
            self._slot_call_note_published(callable_root)
            self._slot_call_check_status(status, "super allocator lookup", expr.span)
            self._emit_post_call_err_check(expr.span)
            # Python resolves the attribute before evaluating call operands.
            positional, keywords = self._slot_call_split_operands(expr)
            args = self._emit_slot_call_args_tuple(positional, "super.new.args")
            roots.append(args)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            kwargs = self._emit_slot_call_kwargs_object(keywords, None, expr.span, "super.new.kwargs", callable_root)
            roots.append(kwargs)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self.builder.call(
                self.runtime["py_obj_call_slots"],
                [self._as_gc_ptr(callable_root), self._as_gc_ptr(args),
                 self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                name=self._fresh("super.new.invoke"),
            )
            self._slot_call_note_published(output)
            self._slot_call_check_status(status, "super allocator call", expr.span)
            self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots(tuple(roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        return self._take_slot_call_root(output)
