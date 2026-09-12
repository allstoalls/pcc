"""Method-call expression lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BinOp,
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
    IntType,
    ListType,
    MemoryViewType,
    Name,
    NoneType,
    StrLit,
    StrType,
    Subscript,
    TupleType,
    Type,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_STR
from .errors import L1CodegenError

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
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

    def _emit_callable_attribute_call(
        self,
        obj_expr: Expr,
        attr_name: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        span,
    ) -> ir.Value:
        obj_val = self._emit_expr(obj_expr)
        name_ptr = self._attr_name_ptr(attr_name)
        callable_obj = self.builder.call(
            self.runtime["py_obj_getattr"],
            [obj_val, name_ptr],
            name=self._fresh(f"callable.attr.{attr_name}"),
        )
        kwdict_unpack = self._split_starstar_kwargs_unpack(args)
        arg_exprs = args
        kwargs_expr = None
        if kwdict_unpack is not None:
            arg_exprs, kwargs_expr = kwdict_unpack
        args_owned = not self._is_starred_unpack(arg_exprs)
        args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
        kwargs_obj = self._emit_dynamic_call_kwargs_object(
            kwargs,
            kwargs_expr,
            span,
        )
        result = self.builder.call(
            self.runtime["py_obj_call"],
            [callable_obj, args_tuple, kwargs_obj],
            name=self._fresh(f"callable.attr.{attr_name}.call"),
        )
        if args_owned:
            self._gc_release(args_tuple)
        if kwargs:
            self._gc_release(kwargs_obj)
        self._gc_release(callable_obj)
        self._emit_post_call_err_check(span)
        return result

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
            args_owned = not self._is_starred_unpack(arg_exprs)
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
            if args_owned:
                self._gc_release(args_tuple)
            if expr.kwargs:
                self._gc_release(kwargs_obj)
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result

        if (
            attr.name == "__new__"
            and _method_is_name(attr.obj)
            and _method_ident(attr.obj) == "type"
        ):
            if len(expr.args) >= 4:
                name_obj = self._emit_as_object(expr.args[1])
                bases_obj = self._emit_as_object(expr.args[2])
                ns_obj = self._emit_as_object(expr.args[3])
                result = self.builder.call(
                    self.runtime["py_class_new_from_objects"],
                    [name_obj, bases_obj, ns_obj],
                    name=self._fresh("type.new"),
                )
                self._emit_post_call_err_check(self._expr_span_or_none(expr))
                return result
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
            recv = self._emit_as_object(attr.obj)
            return self.builder.call(
                self.runtime["py_complex_conjugate"],
                [recv],
                name=self._fresh("complex.conjugate"),
            )
        if (
            isinstance(obj_ty0, DynType)
            and obj_ty0.name == "coroutine"
            and attr.name == "close"
            and not expr.args
            and not expr.kwargs
        ):
            return self.builder.call(
                self.runtime["py_coroutine_close"],
                [self._emit_as_object(attr.obj)],
                name=self._fresh("coroutine.close"),
            )
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
        if attr.name == "strftime" and len(expr.args) == 1 and not expr.kwargs:
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
        if isinstance(attr.obj, Call):
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
        if (
            gen_intrinsic_ok
            and attr.name == "send"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            gen_obj = self._emit_as_object(attr.obj)
            value_obj = self._emit_as_object(expr.args[0])
            send_res = self.builder.call(
                self.runtime["py_gen_send"],
                [gen_obj, value_obj],
                name=self._fresh("gen.send"),
            )
            # py_gen_send raises (thrown-in exceptions, GeneratorExit
            # escapes, errors inside the generator body); without
            # this check they skip enclosing try/except blocks
            self._emit_post_call_err_check(expr.span)
            return send_res
        if (
            gen_intrinsic_ok
            and attr.name == "throw"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            gen_obj = self._emit_as_object(attr.obj)
            exc_obj = self._emit_as_object(expr.args[0])
            throw_res = self.builder.call(
                self.runtime["py_gen_throw"],
                [gen_obj, exc_obj],
                name=self._fresh("gen.throw"),
            )
            # py_gen_throw raises (thrown-in exceptions, GeneratorExit
            # escapes, errors inside the generator body); without
            # this check they skip enclosing try/except blocks
            self._emit_post_call_err_check(expr.span)
            return throw_res
        if (
            gen_intrinsic_ok
            and attr.name == "close"
            and not expr.args
            and not expr.kwargs
        ):
            gen_obj = self._emit_as_object(attr.obj)
            close_res = self.builder.call(
                self.runtime["py_gen_close"],
                [gen_obj],
                name=self._fresh("gen.close"),
            )
            # py_gen_close raises (thrown-in exceptions, GeneratorExit
            # escapes, errors inside the generator body); without
            # this check they skip enclosing try/except blocks
            self._emit_post_call_err_check(expr.span)
            return close_res

        # Case 0: ``super().method(args)`` inside a method body.
        # Resolve the method by walking the current class's declared
        # bases. The ``self`` argument is forwarded unchanged.
        current_class = self.current_class
        if (
            current_class is not None
            and isinstance(attr.obj, Call)
            and isinstance(attr.obj.func, Name)
            and attr.obj.func.ident == "super"
            and len(attr.obj.args) in (0, 2)
            and not attr.obj.kwargs
        ):
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
                name_obj = self._emit_as_object(expr.args[1])
                bases_obj = self._emit_as_object(expr.args[2])
                ns_obj = self._emit_as_object(expr.args[3])
                result = self.builder.call(
                    self.runtime["py_class_new_from_objects"],
                    [name_obj, bases_obj, ns_obj],
                    name=self._fresh("super.type.new"),
                )
                self._emit_post_call_err_check(self._expr_span_or_none(expr))
                return result
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
                        # pcc/llvm_capi/ir.py:207 until ``cls`` decayed to a
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
            parent_info = (
                self._resolve_super_method(from_class, attr.name)
                if from_class is not None
                else None
            )
            if parent_info is not None:
                receiver_name = "self"
                if (
                    getattr(self, "current_func_def", None) is not None
                    and self.current_func_def.args
                ):
                    receiver_name = self.current_func_def.args[0].name or "self"
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
                if current_method_kind == "static" and len(super_args) == 0:
                    msg = self._pooled_cstr_ptr(
                        "super(): no arguments",
                        ".super_static_error",
                    )
                    exc = self.builder.call(
                        self.runtime["py_exc_new"],
                        [ir.Constant(_I64, _PY_EXC_RUNTIMEERROR), msg],
                        name=self._fresh("super.static.error"),
                    )
                    self.builder.call(self.runtime["py_raise"], [exc])
                    self._emit_post_call_err_check(getattr(expr, "span", None))
                    return self._emit_none_literal()
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
                )
            if from_class is not None and attr.name not in ("__init__", "__new__"):
                receiver_name = "self"
                if (
                    getattr(self, "current_func_def", None) is not None
                    and self.current_func_def.args
                ):
                    receiver_name = self.current_func_def.args[0].name or "self"
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
                if current_method_kind == "static" and len(super_args) == 0:
                    msg = self._pooled_cstr_ptr(
                        "super(): no arguments",
                        ".super_static_error",
                    )
                    exc = self.builder.call(
                        self.runtime["py_exc_new"],
                        [ir.Constant(_I64, _PY_EXC_RUNTIMEERROR), msg],
                        name=self._fresh("super.static.error"),
                    )
                    self.builder.call(self.runtime["py_raise"], [exc])
                    self._emit_post_call_err_check(getattr(expr, "span", None))
                    return self._emit_none_literal()
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
                self.class_lowering.emit_super_lookup_from_start(
                    from_class,
                    super_start_cls,
                    attr.name,
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return self._emit_none_literal()
            # Parent is a foreign base (e.g. ``Exception``) not tracked
            # by pcc's ClassInfo registry. For the well-known dunders
            # (``__init__`` / ``__new__``) we fall through quietly —
            # pcc-emitted classes already have their ctor state
            # populated by ``_pcc_py_module_init_*``, and calling an
            # unknown foreign super is typically only used for its
            # side effects which have no equivalent on the pcc side.
            if attr.name in ("__init__", "__new__"):
                # super().__init__(*args) to a builtin Exception base: the call
                # itself is a no-op on the pcc side, but BaseException stores
                # the args tuple on the instance, and str(e) / e.args read it.
                # Persist args so a raised user-exception-subclass instance
                # behaves like an exception (otherwise str(e) -> <null>,
                # e.args -> AttributeError).
                if attr.name == "__init__":
                    self._emit_store_exception_args(expr.args)
                return ir.Constant(_CSTR, None)

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
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
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
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                    )
                if current_class is not None and current_class is not receiver_info:
                    lexical_info = self._resolve_method_mro(
                        current_class.name, attr.name
                    )
                    if lexical_info is not method_info:
                        return self._emit_callable_attribute_call(
                            attr.obj,
                            attr.name,
                            expr.args,
                            expr.kwargs,
                            expr.span,
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
                    recv_slot = self.env.get("cls")
                    if recv_slot is not None:
                        cls_ptr = self.builder.load(
                            recv_slot[0],
                            name=self._fresh(".cls.recv"),
                        )
                    else:
                        cls_ptr = self.builder.load(
                            current_class.global_var,
                            name=self._fresh(".cls.recv"),
                        )
                        self._note_global_backed_value(cls_ptr, current_class.global_var)
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
                self_val = self.builder.load(
                    self.env["self"][0], name=self._fresh("self")
                )
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
                args_owned = not self._is_starred_unpack(arg_exprs)
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
                if args_owned:
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
                    obj_val = self._emit_expr(attr.obj)
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
                obj_val = self._emit_expr(attr.obj)
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
            if class_info is not None and self._class_attr_needs_runtime_lookup(
                class_info, attr.name
            ):
                return self._emit_callable_attribute_call(
                    attr.obj,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                    expr.span,
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
                    obj_val = self._emit_expr(attr.obj)
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
        if isinstance(obj_ty, DynType):
            if attr.name == "clear" and not expr.args and not expr.kwargs:
                self.builder.call(
                    self.runtime["py_obj_clear"],
                    [self._emit_as_object(attr.obj)],
                    name=self._fresh("obj.clear"),
                )
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
            if attr.name == "to_bytes" and len(expr.args) == 2 and not expr.kwargs:
                # n.to_bytes(length, byteorder) — unsigned form; raises
                # OverflowError/ValueError per CPython (err check
                # required). signed= falls through to dynamic dispatch
                # (rejected honestly under --python-libpython=off).
                obj = self._emit_as_object(attr.obj)
                length_val = self._emit_expr_as_i64(expr.args[0])
                order_obj = self._emit_as_object(expr.args[1])
                result = self.builder.call(
                    self.runtime["py_int_to_bytes"],
                    [obj, length_val, order_obj],
                    name=self._fresh("dyn.to_bytes"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return result
            recv_obj = self._emit_as_object(attr.obj)
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
            args_owned = not self._is_starred_unpack(arg_exprs)
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
            if args_owned:
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
            encoding = (
                self._emit_expr_as_pcc_object(encoding_arg)
                if encoding_arg is not None
                else self._emit_str_literal("utf-8")
            )
            errors = (
                self._emit_expr_as_pcc_object(errors_arg)
                if errors_arg is not None
                else self._emit_str_literal("strict")
            )
            return self.builder.call(
                self.runtime["py_bytes_decode_with_encoding"],
                [recv, encoding, errors],
                name=self._fresh("bytes.decode"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "join"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # b"sep".join(list_or_tuple_of_bytes) -> one O(n) allocation.
            recv = self._emit_expr(attr.obj)
            return self._emit_native_bytes_join(recv, expr.args[0], "bytes")
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "hex"
            and not expr.args
            and not expr.kwargs
        ):
            # bytes.hex() -> lowercase two-hex-digits-per-byte string.
            recv = self._emit_expr(attr.obj)
            return self.builder.call(
                self.runtime["py_bytes_hex"],
                [recv],
                name=self._fresh("bytes.hex"),
            )
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
            and attr.name in ("strip", "lstrip", "rstrip")
            and not expr.args
            and not expr.kwargs
        ):
            # No-arg ASCII-whitespace strip, all three sides.  `lstrip`/
            # `rstrip` used to fall through to the generic attribute path and
            # die with "'bytes' object has no attribute 'rstrip'" -- which is
            # how the self-hosted archive reader (`macho_archive` trims a
            # 16-byte ar member name with `header[0:16].rstrip()`) failed once
            # pcc1 reached it.  `strip(chars)` still falls back.
            recv = self._emit_expr(attr.obj)
            return self.builder.call(
                self.runtime["py_bytes_" + attr.name],
                [recv],
                name=self._fresh("bytes." + attr.name),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name in ("strip", "lstrip", "rstrip")
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # strip(chars): trim any byte in `chars` from the chosen side(s).
            # pcc1 reaches this through its own archive reader, which trims a
            # member name with `.rstrip(b"/")` and a long-name entry with
            # `.rstrip(b"\0")`; without it the call fell through to the
            # dynamic attribute path and raised "'bytes' object has no
            # attribute 'rstrip'".
            recv = self._emit_expr(attr.obj)
            chars = self._emit_expr(expr.args[0])
            return self.builder.call(
                self.runtime["py_bytes_" + attr.name + "_chars"],
                [recv, chars],
                name=self._fresh("bytes." + attr.name + ".chars"),
            )
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "split"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            # bytes/bytearray .split(sep): list of same-family pieces. Empty sep
            # raises ValueError (py_bytes_split), so err-check after. No-arg
            # whitespace split still falls back.
            recv = self._emit_expr(attr.obj)
            sep = self._emit_as_object(expr.args[0])
            result = self.builder.call(
                self.runtime["py_bytes_split"],
                [recv, sep],
                name=self._fresh("bytes.split"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name in ("ljust", "rjust")
            and len(expr.args) in (1, 2)
            and not expr.kwargs
        ):
            # `bytes.ljust(width[, fill])`: pcc1 writes every Mach-O segment
            # and section name with `name.encode().ljust(16, b"\0")`, which
            # had no lowering and reached the dynamic attribute path as
            # "'bytes' object has no attribute 'ljust'".
            recv = self._emit_expr(attr.obj)
            width = self._emit_expr_as_i64(expr.args[0])
            if len(expr.args) == 2:
                fill = self._emit_as_object(expr.args[1])
            else:
                fill = ir.Constant(ir.PointerType(ir.IntType(8)), None)
            result = self.builder.call(
                self.runtime["py_bytes_" + attr.name],
                [recv, width, fill],
                name=self._fresh("bytes." + attr.name),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if (
            isinstance(obj_ty, (BytesType, ByteArrayType))
            and attr.name == "split"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            # .split(sep, maxsplit): stop after `maxsplit` cuts.  pcc1 reads
            # its own Mach-O members through `raw.split(b"\0", 1)[0]`, which
            # fell through to the dynamic attribute path and raised
            # "'bytes' object has no attribute 'split'".
            recv = self._emit_expr(attr.obj)
            sep = self._emit_as_object(expr.args[0])
            limit = self._emit_as_object(expr.args[1])
            result = self.builder.call(
                self.runtime["py_bytes_split_max"],
                [recv, sep, limit],
                name=self._fresh("bytes.split.max"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
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
        if (
            isinstance(obj_ty, (IntType, DynType))
            and attr.name == "to_bytes"
            and len(expr.args) == 2
            and not expr.kwargs
        ):
            # Native int.to_bytes(length, byteorder) — unsigned form;
            # exact for bignums (py_int_to_bytes walks the limbs).
            # Raises OverflowError/ValueError per CPython, so the
            # post-call err check is required. signed= falls through
            # to the fallback (rejected honestly under
            # --python-libpython=off).
            obj = self._emit_as_object(attr.obj)
            length_val = self._emit_expr_as_i64(expr.args[0])
            order_obj = self._emit_as_object(expr.args[1])
            result = self.builder.call(
                self.runtime["py_int_to_bytes"],
                [obj, length_val, order_obj],
                name=self._fresh("int.to_bytes"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
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
                if class_info is not None and self._class_attr_needs_runtime_lookup(
                    class_info, attr.name
                ):
                    return self._emit_callable_attribute_call(
                        attr.obj,
                        attr.name,
                        expr.args,
                        expr.kwargs,
                        expr.span,
                    )
            # A schema-bearing local class should have matched one of
            # the direct method cases above. If it did not, or the type
            # is only an unresolved imported annotation shell, preserve
            # Python semantics by dispatching the runtime object through
            # CPython instead of treating the annotation as a closed
            # pcc class registry entry.
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

        return self._emit_callable_attribute_call(
            attr.obj,
            attr.name,
            expr.args,
            expr.kwargs,
            expr.span,
        )

    def _emit_store_exception_args(self, args) -> None:
        """Persist ``self.args = tuple(args)`` for ``super().__init__(*args)``
        to a builtin Exception base.

        The super-init call is a no-op on the pcc side, but BaseException stores
        the constructor args on the instance; ``str(e)`` derives its message
        from them and ``e.args`` returns the tuple. Without this, a raised user
        exception-subclass instance has no message (``str(e)`` -> ``<null>``)
        and no ``args`` attribute.
        """
        fd = getattr(self, "current_func_def", None)
        if fd is None or not fd.args:
            return
        self_name = fd.args[0].name or "self"
        recv_slot = self.env.get(self_name)
        if recv_slot is None:
            return
        self_val = self.builder.load(recv_slot[0], name=self._fresh("exc.self"))
        n = len(args)
        tup = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=self._fresh("exc.args.tuple"),
        )
        idx = 0
        for a in args:
            a_obj = self._emit_as_object(a)
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [tup, ir.Constant(_I64, idx), a_obj],
            )
            idx += 1
        name_ptr = self._pooled_cstr_ptr("args", ".exc.args.name")
        self.builder.call(
            self.runtime["py_instance_setattr"],
            [self_val, name_ptr, tup],
        )
