"""Builtin type/attribute helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir
from pcc.runtime.py.py_abi_constants import PY_TYPE_STR

from pcc.frontends.python.py_ast import (
    Attr,
    Call,
    DictType,
    DynType,
    Expr,
    ListType,
    Name,
    StrLit,
    StrType,
    TupleExpr,
    TupleType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.errors import L1CodegenError

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


class BuiltinTypeAttrLoweringMixin:
    def _emit_getattr_builtin(self, expr: Call) -> ir.Value:
        """``getattr(obj, name)`` / ``getattr(obj, name, default)``.
        CPython-backed receivers go through the real CPython builtin so
        module objects and the three-arg default form keep Python
        semantics. Native receivers use authoritative operand/result roots;
        only AttributeError selects an explicitly evaluated default.
        """
        native_module_getattr = self._maybe_emit_native_module_getattr(expr)
        if native_module_getattr is not None:
            return native_module_getattr
        if self._expr_looks_cpython(expr.args[0]):
            fn_val = self._load_cpython_builtin("getattr")
            return self._emit_cpy_func_call(
                fn_val,
                "getattr",
                tuple(expr.args),
            )
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("getattr.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(expr.args[0], "getattr.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            name_expr = expr.args[1]
            name_root = None
            if not isinstance(name_expr, StrLit):
                name_root = self._emit_slot_call_operand(name_expr, "getattr.name")
                roots.append(name_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            # A default is an ordinary argument: evaluate it before lookup,
            # even when the attribute exists or the eventual lookup fails.
            default = None
            if len(expr.args) == 3:
                default = self._emit_slot_call_operand(expr.args[2], "getattr.default")
                roots.append(default)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            pending = self._new_slot_call_root("getattr.exception")
            roots.append(pending)
            attribute_error = None
            if default is not None:
                attribute_error = self._new_slot_call_root("getattr.attribute_error")
                roots.append(attribute_error)
            cleanup = self._slot_call_cleanup_block(tuple(roots), target)
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            if attribute_error is not None:
                # Copy the authoritative builtin cache, independent of a
                # user's binding named AttributeError. Prime before lookup
                # so cache allocation cannot replace its pending exception.
                self.builder.call(self.runtime["py_exc_builtin_class"], [ir.Constant(_I64, 6)])
                self._emit_post_call_err_check(expr.span)
                cache = self.builder.call(self.runtime["py_subs_exc_cache_slot"], [ir.Constant(_I64, 6)])
                self._slot_call_copy_source(attribute_error, cache, span=expr.span)
            if name_root is not None:
                # Validate only after every source argument has run. A name
                # is a native string value, never an implicit str() coercion.
                name_tag = self._slot_call_runtime_call(
                    "py_obj_type_tag", (name_root,), span=expr.span,
                )
                valid_name = self.current_function.append_basic_block(self._fresh("getattr.name.valid"))
                invalid_name = self.current_function.append_basic_block(self._fresh("getattr.name.invalid"))
                self.builder.cbranch(
                    self.builder.icmp_signed("==", name_tag, ir.Constant(_I64, PY_TYPE_STR)),
                    valid_name, invalid_name,
                )
                self.builder.position_at_end(invalid_name)
                self._emit_builtin_exception_and_branch(
                    "TypeError", "attribute name must be string", expr.span,
                )
                self.builder.position_at_end(valid_name)
            name_token = None
            if name_root is None:
                name_ptr = self._attr_name_ptr(name_expr.value)
            else:
                # The C string is an interior pointer. Keep its string owner
                # leased through lookup, including receiver lease acquisition,
                # user callbacks and publication of the returned object.
                name_token = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_acquire"],
                    [self._as_gc_ptr(name_root)], name=self._fresh("getattr.name.lease"),
                )
                self._slot_call_check_status(name_token, "getattr name lease", expr.span)
                self._try_err_block = self._slot_call_cleanup_block(
                    (), cleanup, ((name_root, name_token),),
                )
                self._cpy_operand_cleanup_block = self._try_err_block
                name_ptr = self.builder.call(
                    self.runtime["py_str_utf8"], [self.builder.load(name_root)],
                    name=self._fresh("getattr.name.utf8"),
                )
            # The shared producer immediately stores the NEW result in the
            # output root, then transfers TLS into pending before releasing
            # argument leases. No raw result crosses a check or finalizer.
            self._slot_call_runtime_call(
                "py_obj_getattr", (receiver,), result_slot=output,
                suffix_args=(name_ptr,), span=expr.span, exception_slot=pending,
            )
            if name_root is not None:
                released = self.builder.call(
                    self.runtime["pcc_gc_foreign_lease_release"],
                    [self._as_gc_ptr(name_root), name_token],
                    name=self._fresh("getattr.name.release"),
                )
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                self._slot_call_check_status(released, "getattr name lease release", expr.span)
            failed = self.current_function.append_basic_block(self._fresh("getattr.failed"))
            present = self.current_function.append_basic_block(self._fresh("getattr.present"))
            done = self.current_function.append_basic_block(self._fresh("getattr.done"))
            self.builder.cbranch(
                self.builder.icmp_unsigned(
                    "!=", self.builder.load(pending), ir.Constant(_CSTR, None),
                ),
                failed, present,
            )
            self.builder.position_at_end(failed)
            if default is not None:
                matched = self._slot_call_runtime_call(
                    "py_exc_matches", (pending, attribute_error), span=expr.span,
                )
                use_default = self.current_function.append_basic_block(self._fresh("getattr.use_default"))
                propagate = self.current_function.append_basic_block(self._fresh("getattr.propagate"))
                self.builder.cbranch(
                    self.builder.icmp_signed("!=", matched, ir.Constant(_I64, 0)),
                    use_default, propagate,
                )
                self.builder.position_at_end(use_default)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(output), ir.Constant(_CSTR, None)],
                )
                self._slot_call_copy_source(output, default, span=expr.span)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(pending), ir.Constant(_CSTR, None)],
                )
                self.builder.branch(done)
                self.builder.position_at_end(propagate)
            # Restore exactly the selecting exception. The cleanup block
            # protects that owner while receiver/name/default finalizers run.
            self.builder.call(self.runtime["py_clear_exception"], [])
            self.builder.call(
                self.module.globals["py_tls_exc_swap_slot"], [self._as_gc_ptr(pending)],
            )
            self.builder.branch(cleanup)
            self.builder.position_at_end(present)
            invalid_result = self.current_function.append_basic_block(self._fresh("getattr.invalid_result"))
            valid_result = self.current_function.append_basic_block(self._fresh("getattr.valid_result"))
            self.builder.cbranch(
                self.builder.icmp_unsigned(
                    "==", self.builder.load(output), ir.Constant(_CSTR, None),
                ),
                invalid_result, valid_result,
            )
            self.builder.position_at_end(invalid_result)
            # We use the raising lookup ABI for both arities. NULL without a
            # pending exception is a broken producer, not an attribute miss.
            self._emit_builtin_exception_and_branch(
                "RuntimeError", "getattr returned NULL without setting an exception", expr.span,
            )
            self.builder.position_at_end(valid_result)
            self.builder.branch(done)
            self.builder.position_at_end(done)
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
            if sink is not None:
                return self.builder.load(output, name=self._fresh("getattr.current"))
            return self._take_slot_call_root(output)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_attr_name_ptr_arg(self, expr: Expr, label: str) -> ir.Value:
        if isinstance(expr, StrLit):
            return self._attr_name_ptr(expr.value)
        raw = self._emit_expr(expr)
        obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            expr.ty,
        )
        return self.builder.call(
            self.runtime["py_str_utf8"],
            [obj],
            name=self._fresh(label),
        )

    def _emit_owned_literal_attribute_mutation(self, expr, runtime_name):
        """Keep mutation operands owned, then publish its ordinary None value."""
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("attribute.mutation.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            obj = self._emit_slot_call_operand(expr.args[0], "attribute.mutation.target")
            roots.append(obj)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # Literal names are immutable static C strings. Dynamic names
            # retain their explicit existing route until its string owner and
            # runtime type validation have an equivalent slot contract.
            name_ptr = self._attr_name_ptr(expr.args[1].value)
            operands = (obj,)
            order = ()
            if runtime_name == "py_obj_setattr":
                value = self._emit_slot_call_operand(expr.args[2], "attribute.mutation.value")
                roots.append(value)
                operands = (obj, value)
                order = (0, 2, 1)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            status = self._slot_call_runtime_call(
                runtime_name, operands, suffix_args=(name_ptr,),
                argument_order=order, span=expr.span,
            )
            self._emit_attribute_error_if_status_failed(status, "attribute", expr.span)
            # None is a borrowed canonical singleton at its literal producer.
            # Acquire this expression's owner explicitly, and publish it before
            # operand cleanup can run arbitrary finalizers or relocate values.
            result = self._gc_retain(self._emit_none_literal())
            self._publish_slot_call_owned(output, result, label="attribute mutation None")
            self._slot_call_note_published(output)
            self._release_slot_call_roots(tuple(roots[1:] if sink is None else roots))
            if sink is not None:
                return self.builder.load(output, name=self._fresh("attribute.mutation.none"))
            return self._take_slot_call_root(output)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_setattr_builtin(self, expr: Call) -> ir.Value:
        if (isinstance(expr.args[1], StrLit)
                and not self._expr_looks_cpython(expr.args[0])
                and not self._is_valueclass_payload_type(expr.args[0].ty)
                and not (isinstance(expr.args[0], Name)
                         and expr.args[0].ident in getattr(self, "_native_module_aliases", {}))):
            return self._emit_owned_literal_attribute_mutation(expr, "py_obj_setattr")
        target = expr.args[0]
        if isinstance(target, Name):
            module_name = getattr(self, "_native_module_aliases", {}).get(target.ident)
            if module_name is not None:
                name_ptr = self._emit_attr_name_ptr_arg(
                    expr.args[1],
                    "setattr.module.name",
                )
                value = self._emit_expr_as_pcc_object(expr.args[2])
                module_name_ptr = self._ptr_to_cstr(
                    self._cstr_global(
                        module_name,
                        f".setattr.module.{module_name}",
                    )
                )
                status = self.builder.call(
                    self.runtime["py_module_attr_set"],
                    [module_name_ptr, name_ptr, value],
                    name=self._fresh("setattr.module.rc"),
                )
                self._emit_attribute_error_if_status_failed(
                    status,
                    "module attribute",
                    expr.span,
                )
                return self._emit_none_literal()
        obj = self._emit_as_object(expr.args[0])
        name_ptr = self._emit_attr_name_ptr_arg(
            expr.args[1],
            "setattr.name",
        )
        value = self._emit_expr_as_pcc_object(expr.args[2])
        status = self.builder.call(
            self.runtime["py_obj_setattr"],
            [obj, name_ptr, value],
            name=self._fresh("setattr.rc"),
        )
        self._emit_attribute_error_if_status_failed(
            status,
            "attribute",
            expr.span,
        )
        return self._emit_none_literal()

    def _emit_delattr_builtin(self, expr: Call) -> ir.Value:
        if (isinstance(expr.args[1], StrLit)
                and not self._expr_looks_cpython(expr.args[0])
                and not self._is_valueclass_payload_type(expr.args[0].ty)
                and not (isinstance(expr.args[0], Name)
                         and expr.args[0].ident in getattr(self, "_native_module_aliases", {}))):
            return self._emit_owned_literal_attribute_mutation(expr, "py_obj_delattr")
        obj = self._emit_as_object(expr.args[0])
        name_ptr = self._emit_attr_name_ptr_arg(
            expr.args[1],
            "delattr.name",
        )
        self.builder.call(
            self.runtime["py_obj_delattr"],
            [obj, name_ptr],
            name=self._fresh("delattr.rc"),
        )
        return self._emit_none_literal()

    def _emit_type_builtin(self, expr: Call) -> ir.Value:
        """``type(obj)`` — returns the runtime class PyObject*.
        Uses ``py_obj_getattr(obj, "__class__")`` which the runtime
        resolves on any pcc-native object. For a CPython value (libpython
        mode), native ``py_obj_getattr`` mishandles the real CPython object
        (returns a bogus value), so route ``__class__`` through
        ``py_cpy_getattr`` and tag the resulting CPython type as a cpy value
        (so ``type(x).__name__`` etc. also dispatch through libpython).
        Inert in no-libpython mode (``_cpy_values`` is empty)."""
        if not self._expr_looks_cpython(expr.args[0]):
            # py_obj_getattr's __class__ branch already delegates to this
            # exact NEW-reference ABI before any user attribute override.
            return self._emit_owned_unary_runtime_call(expr, "py_type_builtin")
        obj_val = self._emit_as_object(expr.args[0])
        if obj_val in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(obj_val)
            obj_owned = self._cpy_value_is_owned(obj_val)
            cpy_name = self._ptr_to_cstr(
                self._cstr_global("__class__", ".cpy.attr.__class__")
            )
            cls = self.builder.call(
                self.runtime["py_cpy_getattr"],
                [obj_val, cpy_name],
                name=self._fresh("cpy.type"),
            )
            if obj_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [obj_val])
                self._forget_owned_cpy_value(obj_val)
            self._mark_owned_cpy_value(cls)
            self._guard_cpy_value_not_null(cls)
            return cls
        name_ptr = self._attr_name_ptr("__class__")
        return self.builder.call(
            self.runtime["py_obj_getattr"],
            [obj_val, name_ptr],
            name=self._fresh("type"),
        )

    def _emit_len_call(self, expr: Call) -> ir.Value:
        """``len(x)`` → type-specialised runtime call.

        For typed containers we dispatch to the type-specific runtime
        helper (``py_list_len`` etc.); otherwise we go through the
        generic ``py_obj_len``.
        """
        if len(expr.args) != 1:
            raise L1CodegenError(f"len() takes exactly 1 arg, got {len(expr.args)}")
        arg = expr.args[0]
        weak_dict_kind = self._weak_dict_kind_for_expr(arg)
        # Class-based ``__len__`` fast path.
        dunder = self._try_dispatch_dunder_unary(
            arg, "__len__", (), result_slot=self._slot_call_result_sink(expr),
        )
        if dunder is not None:
            return dunder
        obj = self._emit_expr(arg)
        # CPython-backed value: dispatch through py_cpy_len (PyObject_Length).
        if obj in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(obj)
            result = self.builder.call(
                self.runtime["py_cpy_len"],
                [obj],
                name=self._fresh("cpy.len"),
            )
            if self._cpy_value_is_owned(obj):
                self.builder.call(self.runtime["py_cpy_decref"], [obj])
                self._forget_owned_cpy_value(obj)
            return result
        if weak_dict_kind == "value":
            return self.builder.call(
                self.runtime["py_weak_value_dict_len"],
                [obj],
                name=self._fresh("weak.value.dict.len"),
            )
        if weak_dict_kind == "key":
            return self.builder.call(
                self.runtime["py_weak_key_dict_len"],
                [obj],
                name=self._fresh("weak.key.dict.len"),
            )
        aty = arg.ty
        # ``len`` consumes its operand: an owned temporary (a call result, a
        # literal, a concatenation) must be released here or it leaks once per
        # evaluation -- ``len(f(i))`` with an exact-list ``f`` retained 116 MB
        # over 300k calls (tests/python/test_ownership_str_iadd_and_len_call_result.py).
        typed_len = {
            ListType: "py_list_len",
            StrType: "py_str_len",
            DictType: "py_dict_len",
            TupleType: "py_tuple_len",
        }
        for len_ty, helper in typed_len.items():
            if isinstance(aty, len_ty):
                result = self.builder.call(
                    self.runtime[helper], [obj], name=self._fresh(helper[3:])
                )
                self._gc_release_if_owned(obj, arg)
                return result
        # Fallback through the builtin's runtime: an object with __len__
        # answers it; an unsized object raises CPython's TypeError (the
        # internal py_obj_len would silently answer 0).
        boxed = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, obj, aty
        )
        result = self.builder.call(
            self.runtime["py_builtin_len"], [boxed], name=self._fresh("obj.len")
        )
        self._gc_release_if_owned(boxed, arg)
        self._emit_post_call_err_check(self._expr_span_or_none(expr))
        return result

    def _emit_str_builtin(self, expr: Call) -> ir.Value:
        """``str(x)`` -> owned PCC string, matching CPython's new-ref result."""
        if len(expr.args) in (2, 3) and not expr.kwargs:
            # str(object, encoding[, errors]) decodes into the caller's output
            # root, as bytes.decode() does.
            operands = [("encoding", expr.args[1])]
            if len(expr.args) == 3:
                operands.append(("errors", expr.args[2]))
            return self._emit_owned_decode(expr, expr.args[0], operands, False)
        if len(expr.args) != 1:
            raise NotImplementedError("str() with multi-arg not supported")
        arg = expr.args[0]
        if not self._expr_looks_cpython(arg):
            return self._emit_owned_text_conversion(expr, "py_obj_str")
        if isinstance(arg.ty, StrType):
            v = self._emit_expr(arg)
            if v in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(v)
                result = self.builder.call(
                    self.runtime["py_cpy_to_pcc_str"],
                    [v],
                    name=self._fresh("cpy.to_pcc_str"),
                )
                if self._cpy_value_is_owned(v):
                    self.builder.call(self.runtime["py_cpy_decref"], [v])
                    self._forget_owned_cpy_value(v)
                self._guard_cpy_value_not_null(result)
                self._emit_post_call_err_check(getattr(arg, "span", None))
                return result
            if self._expr_returns_owned_object(arg):
                return v
            return self._gc_retain(v, name=self._fresh("str.retain"))
        if self._expr_looks_cpython(arg):
            v = self._emit_expr(arg)
            if v in getattr(self, "_cpy_values", ()):
                self._guard_cpy_value_not_null(v)
                result = self.builder.call(
                    self.runtime["py_cpy_to_pcc_str"],
                    [v],
                    name=self._fresh("cpy.to_pcc_str"),
                )
                if self._cpy_value_is_owned(v):
                    self.builder.call(self.runtime["py_cpy_decref"], [v])
                    self._forget_owned_cpy_value(v)
                self._guard_cpy_value_not_null(result)
                self._emit_post_call_err_check(getattr(arg, "span", None))
                return result
        boxed = self._emit_expr_as_pcc_object(arg)
        result = self.builder.call(
            self.runtime["py_obj_str"], [boxed], name=self._fresh("obj.str")
        )
        # py_obj_str borrows its operand, so an owned temporary produced by the
        # argument expression is consumed here.  Release it exactly once
        # (symmetric with repr/ascii/hash); the shared helper leaves a borrowed
        # operand alone, so str(name) gains no release.
        self._gc_release_if_owned(boxed, arg)
        return self.builder.call(
            self.runtime["pcc_gc_resolve_owned_ptr"],
            [result],
            name=self._fresh("obj.str.resolve"),
        )

    def _emit_enumerate_builtin(self, expr: Call) -> Optional[ir.Value]:
        """Value-position ``enumerate(iterable[, start])`` -> eager
        (index, item) tuple list via the C-only ``py_enumerate_list``
        helper (the for-loop form never reaches here — for
        normalization desugars it to a counter). Returns None for
        shapes this lowering does not cover so the caller falls
        through unchanged.

        The list is published straight into the caller's output root (or a
        root of its own) and the iterable is an owned slot-call operand, as
        in ``_emit_reversed_builtin``: ``list(enumerate(xs))`` hands its
        operand slot to this producer, and an owned temporary iterable is
        released with the operand roots."""
        start_expr = None
        if len(expr.args) == 1 and not expr.kwargs:
            pass
        elif len(expr.args) == 2 and not expr.kwargs:
            start_expr = expr.args[1]
        elif (
            len(expr.args) == 1
            and len(expr.kwargs) == 1
            and expr.kwargs[0][0] == "start"
        ):
            start_expr = expr.kwargs[0][1]
        else:
            return None
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("enumerate.result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            source = self._emit_slot_call_operand(expr.args[0], "enumerate.source")
            roots.append(source)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if start_expr is not None:
                start_val = self._emit_expr_as_i64(start_expr)
            else:
                start_val = ir.Constant(_I64, 0)
            # py_obj_iter raises TypeError for non-iterables; iteration can
            # propagate pending exceptions.
            self._slot_call_runtime_call(
                "py_enumerate_list", (source,), result_slot=output,
                suffix_args=(start_val,), span=getattr(expr, "span", None),
            )
            self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("enumerate.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    # Encoding-name sets mirror the ``str.encode`` lowering in
    # ``string_method_lowering.py`` so the two-arg ``bytes``/``bytearray``
    # constructors accept exactly the encodings the encode helpers already
    # cover natively; every other encoding falls through to libpython.
    _UTF8_ENCODING_NAMES = ("utf-8", "utf8", "UTF-8", "UTF8")
    _LATIN1_ENCODING_NAMES = ("latin-1", "latin1")

    def _bytes_encoding_helper_for_arg(self, encoding_expr: Expr) -> Optional[str]:
        """Return the runtime encode helper name for a ``bytes(str, encoding)``
        encoding literal, or ``None`` when the encoding is dynamic / unsupported
        (caller then falls through to the libpython fallback)."""
        if not isinstance(encoding_expr, StrLit):
            return None
        if encoding_expr.value in self._UTF8_ENCODING_NAMES:
            return "py_str_utf8_encode"
        if encoding_expr.value in self._LATIN1_ENCODING_NAMES:
            return "py_str_latin1_encode"
        return None

    def _emit_str_encode_to_bytes(
        self,
        str_expr: Expr,
        helper: str,
        label: str,
        as_bytearray: bool = False,
    ) -> ir.Value:
        """Retain roots and counted address leases across both codec calls."""
        src = self._emit_expr_as_pcc_object(str_expr)
        source_root = self._extern_enter_root(
            src, self._owned_release_needed(src, str_expr), label + ".source"
        )
        roots = [source_root]
        previous = self._current_try_err_block()
        previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
        target = previous if previous is not None else self._ensure_fn_err_exit()
        leases = []
        try:
            leases = self._encode_acquire_root_leases(roots, target, str_expr.span)
            result = self.builder.call(
                self.runtime[helper], [self._extern_load_root(source_root)],
                name=self._fresh(label),
            )
            self._emit_post_call_err_check(str_expr.span)
            if as_bytearray:
                encoded_root = self._encode_capture_result_root(result, roots, label + ".encoded")
                roots.append(encoded_root)
                leases = self._encode_acquire_root_leases(
                    roots, target, str_expr.span, tuple(leases), 1,
                )
                result = self.builder.call(
                    self.runtime["py_bytearray_from_obj"], [self._extern_load_root(encoded_root)],
                    name=self._fresh(label + ".bytearray"),
                )
                self._emit_post_call_err_check(str_expr.span)
            result_root = self._encode_capture_result_root(result, roots, label + ".result")
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = previous_cpy
        release_failed = self._extern_release_foreign_leases(tuple(leases))
        self._extern_check_lease_cleanup(release_failed, tuple(roots) + (result_root,))
        self._extern_release_roots(tuple(roots))
        result = self._extern_take_root(result_root)
        self._note_owned_object_value(result)
        return result

    def _emit_bytes_family_builtin(
        self,
        expr: Call,
        name: str,
    ) -> Optional[ir.Value]:
        if expr.kwargs or name not in ("bytes", "bytearray", "memoryview"):
            return None
        helper = None
        encoded = False
        if len(expr.args) == 1:
            helper = {
                "bytes": "py_bytes_from_obj",
                "bytearray": "py_bytearray_from_obj",
                "memoryview": "py_memoryview_new",
            }[name]
        elif len(expr.args) == 2 and name != "memoryview" and isinstance(expr.args[0].ty, StrType):
            helper = self._bytes_encoding_helper_for_arg(expr.args[1])
            encoded = helper is not None
        elif not expr.args and name != "memoryview":
            helper = "py_bytes_new" if name == "bytes" else "py_bytearray_from_obj"
        if helper is None:
            return None
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(name + ".result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if not expr.args:
                suffix = (ir.Constant(_CSTR, None),)
                if name == "bytes":
                    suffix += (ir.Constant(_I64, 0),)
                self._slot_call_runtime_call(
                    helper, (), result_slot=output, suffix_args=suffix, span=expr.span,
                )
            else:
                source = self._emit_slot_call_operand(expr.args[0], name + ".source")
                roots.append(source)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                encoded_root = output
                if encoded and name == "bytearray":
                    encoded_root = self._new_slot_call_root(name + ".encoded")
                    roots.append(encoded_root)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                # bytes(existing_bytes) aliases its argument with an additional
                # reference. Publish that separate owner before retiring either
                # operand lease or source owner; bytearray/view return NEW too.
                self._slot_call_runtime_call(
                    helper, (source,), result_slot=encoded_root, span=expr.span,
                )
                if encoded and name == "bytearray":
                    self._slot_call_runtime_call(
                        "py_bytearray_from_obj", (encoded_root,),
                        result_slot=output, span=expr.span,
                    )
                self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh(name + ".current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
