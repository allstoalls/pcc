"""Dict builtin and method lowering helpers for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Attr, Call, DictExpr, DictType, DynType, Expr, Name, NoneLit, NoneType,
    StrLit, StrType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.freestanding_abi_constants import (
    PY_TYPE_DICT,
    PY_TYPE_LIST,
    PY_TYPE_SET,
    PY_TYPE_TUPLE,
)


_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()

_DYN_DICT_METHOD_NATIVE = frozenset(
    {
        "get",
        "keys",
        "values",
        "items",
        "setdefault",
        "pop",
        "copy",
    }
)


def _dict_method_box(host, e: Expr) -> ir.Value:
    return host._emit_expr_as_pcc_object(e)


def _emit_rooted_dict_method(self, expr):
    """Publish a dict result or a user-method result before any cleanup."""
    method_name = expr.func.name
    root_label = ('dict.view' if method_name in ('keys', 'values', 'items')
                  else 'dict.' + method_name)
    previous = self._current_try_err_block()
    target = previous if previous is not None else self._ensure_fn_err_exit()
    saved_cpy = self._cpy_operand_cleanup_block
    sink = self._slot_call_result_sink(expr)
    output = sink
    roots = []
    if output is None:
        output = self._new_slot_call_root(root_label + '.result')
        roots.append(output)
    try:
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        receiver = self._emit_slot_call_operand(expr.func.obj, root_label + '.receiver')
        roots.append(receiver)
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        tag = self._slot_call_runtime_call('py_obj_type_tag', (receiver,), span=expr.span)
        is_dict = self.builder.icmp_signed('==', tag, ir.Constant(_I64, PY_TYPE_DICT))
        native_match = is_dict
        if method_name == 'copy':
            # The dict route claims dynamic copy before list/set dispatch.
            # All three exact builtin containers use py_copy_copy; their
            # runtime getattr surface need not manufacture a bound copy.
            # Other types still resolve their actual method, even though
            # copy.copy() itself also accepts tuples, scalars and instances.
            for type_tag in (PY_TYPE_LIST, PY_TYPE_SET):
                matches = self.builder.icmp_signed('==', tag, ir.Constant(_I64, type_tag))
                native_match = self.builder.or_(native_match, matches)
        native_bb = self.current_function.append_basic_block(self._fresh(root_label + '.native'))
        generic_bb = self.current_function.append_basic_block(self._fresh(root_label + '.generic'))
        done_bb = self.current_function.append_basic_block(self._fresh(root_label + '.done'))
        self.builder.cbranch(native_match, native_bb, generic_bb)

        self.builder.position_at_end(native_bb)
        if method_name == 'pop':
            native_roots = list(roots)
            key = self._emit_slot_call_operand(expr.args[0], root_label + '.key')
            native_roots.append(key)
            self._try_err_block = self._slot_call_cleanup_block(tuple(native_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            default = self._emit_slot_call_operand(expr.args[1], root_label + '.default')
            native_roots.append(default)
            self._try_err_block = self._slot_call_cleanup_block(tuple(native_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                'py_dict_get', (receiver, key), result_slot=output, span=expr.span,
            )
            current = self.builder.load(output, name=self._fresh(root_label + '.selected'))
            missing = self.builder.icmp_signed('==', current, ir.Constant(_CSTR, None))
            hit_bb = self.current_function.append_basic_block(self._fresh(root_label + '.hit'))
            miss_bb = self.current_function.append_basic_block(self._fresh(root_label + '.miss'))
            join_bb = self.current_function.append_basic_block(self._fresh(root_label + '.join'))
            self.builder.cbranch(missing, miss_bb, hit_bb)
            self.builder.position_at_end(hit_bb)
            # py_dict_get returns an owner. Keep it published while deletion
            # hashes the key again and may raise or run a finalizer.
            self._slot_call_runtime_call('py_dict_del', (receiver, key), span=expr.span)
            self.builder.branch(join_bb)
            self.builder.position_at_end(miss_bb)
            # The default is borrowed from its operand root. Copy its owner
            # into the still-empty output before retiring either operand.
            self._slot_call_copy_source(output, default, span=expr.span)
            self.builder.branch(join_bb)
            self.builder.position_at_end(join_bb)
            self._release_slot_call_roots((key, default))
        else:
            runtime_name = ('py_copy_copy' if method_name == 'copy'
                            else 'py_dict_' + method_name)
            self._slot_call_runtime_call(
                runtime_name, (receiver,), result_slot=output, span=expr.span,
            )
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        generic_roots = list(roots)
        callable_root = self._new_slot_call_root(root_label + '.callable')
        generic_roots.append(callable_root)
        self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        # Python resolves the user method before evaluating its arguments.
        self._slot_call_runtime_call(
            'py_obj_getattr', (receiver,), result_slot=callable_root,
            suffix_args=(self._attr_name_ptr(expr.func.name),), span=expr.span,
        )
        current_method = self.builder.load(callable_root, name=self._fresh(root_label + '.method.current'))
        self._emit_attribute_error_if_null(current_method, expr.func.name, expr.span)
        args = self._emit_slot_call_args_tuple(expr.args, root_label + '.args')
        generic_roots.append(args)
        self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        kwargs = self._emit_slot_call_kwargs_object((), None, expr.span, root_label + '.kwargs', callable_root)
        generic_roots.append(kwargs)
        self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        status = self.builder.call(
            self.runtime['py_obj_call_slots'],
            [self._as_gc_ptr(callable_root), self._as_gc_ptr(args),
             self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
            name=self._fresh(root_label + '.generic.invoke'),
        )
        self._slot_call_note_published(output)
        self._slot_call_check_status(status, 'dictionary ' + method_name + ' method call', expr.span)
        self._emit_post_call_err_check(expr.span)
        self._release_slot_call_roots((callable_root, args, kwargs))
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block
        self._release_slot_call_roots((receiver,))
    finally:
        self._try_err_block = previous
        self._cpy_operand_cleanup_block = saved_cpy
    if sink is not None:
        return self.builder.load(output, name=self._fresh(root_label + '.current'))
    return self._take_slot_call_root(output)


class DictLoweringMixin:
    def _dict_get_uses_owned_slots(self, expr):
        if expr.func.name not in ("get", "setdefault") or expr.kwargs or len(expr.args) not in (1, 2):
            return False
        if self._expr_looks_cpython(expr.func.obj):
            return False
        for argument in expr.args:
            if (isinstance(argument, Call) and isinstance(argument.func, Name)
                    and argument.func.ident in ("*", "__starred__", "**")):
                return False
        return True

    def _emit_rooted_dict_get(self, expr):
        method_name = expr.func.name
        root_label = "dict." + method_name
        runtime_name = ("py_dict_setdefault_slots" if method_name == "setdefault"
                        else "py_dict_get_default_slots")
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root(root_label + '.result')
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(expr.func.obj, root_label + '.receiver')
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            tag = self._slot_call_runtime_call('py_obj_type_tag', (receiver,), span=expr.span)
            is_dict = self.builder.icmp_signed('==', tag, ir.Constant(_I64, PY_TYPE_DICT))
            native_bb = self.current_function.append_basic_block(self._fresh(root_label + '.native'))
            generic_bb = self.current_function.append_basic_block(self._fresh(root_label + '.generic'))
            done_bb = self.current_function.append_basic_block(self._fresh(root_label + '.done'))
            self.builder.cbranch(is_dict, native_bb, generic_bb)

            self.builder.position_at_end(native_bb)
            native_roots = list(roots)
            self._try_err_block = self._slot_call_cleanup_block(tuple(native_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            key = self._emit_slot_call_operand(expr.args[0], root_label + '.key')
            native_roots.append(key)
            self._try_err_block = self._slot_call_cleanup_block(tuple(native_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            default_expr = expr.args[1] if len(expr.args) == 2 else NoneLit(span=expr.span, ty=NoneType(name='None'))
            default = self._emit_slot_call_operand(default_expr, root_label + '.default')
            native_roots.append(default)
            self._try_err_block = self._slot_call_cleanup_block(tuple(native_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self.builder.call(
                self.runtime[runtime_name],
                [self._as_gc_ptr(receiver), self._as_gc_ptr(key),
                 self._as_gc_ptr(default), self._as_gc_ptr(output)],
                name=self._fresh(root_label + '.invoke'),
            )
            self._slot_call_note_published(output)
            self._slot_call_check_status(status, 'dictionary ' + method_name, expr.span)
            self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots((key, default))
            self.builder.branch(done_bb)

            self.builder.position_at_end(generic_bb)
            generic_roots = list(roots)
            callable_root = self._new_slot_call_root(root_label + '.callable')
            generic_roots.append(callable_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # Python resolves the user method before evaluating its arguments.
            self._slot_call_runtime_call(
                'py_obj_getattr', (receiver,), result_slot=callable_root,
                suffix_args=(self._attr_name_ptr(method_name),), span=expr.span,
            )
            current_method = self.builder.load(callable_root, name=self._fresh(root_label + '.method.current'))
            self._emit_attribute_error_if_null(current_method, method_name, expr.span)
            args = self._emit_slot_call_args_tuple(expr.args, root_label + '.args')
            generic_roots.append(args)
            self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            kwargs = self._emit_slot_call_kwargs_object((), None, expr.span, root_label + '.kwargs', callable_root)
            generic_roots.append(kwargs)
            self._try_err_block = self._slot_call_cleanup_block(tuple(generic_roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self.builder.call(
                self.runtime['py_obj_call_slots'],
                [self._as_gc_ptr(callable_root), self._as_gc_ptr(args),
                 self._as_gc_ptr(kwargs), self._as_gc_ptr(output)],
                name=self._fresh(root_label + '.generic.invoke'),
            )
            self._slot_call_note_published(output)
            self._slot_call_check_status(status, method_name + ' method call', expr.span)
            self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots((callable_root, args, kwargs))
            self.builder.branch(done_bb)

            self.builder.position_at_end(done_bb)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._release_slot_call_roots((receiver,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh(root_label + '.current'))
        return self._take_slot_call_root(output)

    def _maybe_emit_dict_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if self._dict_get_uses_owned_slots(expr):
            return self._emit_rooted_dict_get(expr)
        if (attr.name in ("keys", "values", "items", "copy") and not expr.args
                and not expr.kwargs and not self._expr_looks_cpython(attr.obj)):
            return _emit_rooted_dict_method(self, expr)
        if attr.name == "update" and len(expr.args) == 1 and not expr.kwargs:
            if not self._has_unpacked_operands(expr.args, expr.kwargs):
                return self._emit_shared_container_update(expr)
        if attr.name not in _DYN_DICT_METHOD_NATIVE:
            return None
        if attr.name == "pop":
            return self._emit_dyn_pop_method_with_runtime_guard(expr)
        if not self._expr_looks_cpython(attr.obj):
            # Unsupported builtin shapes can still be valid user methods.
            # The ordinary callable path preserves their owned result and
            # evaluates the bound method before any argument or unpacking.
            return self._emit_slot_call_object(expr, label="dict.method")
        dict_ty = DictType(
            name="dict",
            key=DynType(name="dyn"),
            value=DynType(name="dyn"),
        )
        # ``get``/``keys``/``copy``/``setdefault`` are ordinary user method
        # names too.  Without the tag test the py_dict_* helper ran against a
        # user instance and quietly returned nothing; see dyn_method_guard.
        return self._emit_dyn_container_method_with_tag_guard(
            expr,
            (PY_TYPE_DICT,),
            lambda recv: self._maybe_emit_dict_method(
                expr, dict_ty, recv=recv, recv_borrowed=True
            ),
            "dyn.dict",
        )

    def _emit_shared_container_update(self, expr: Call) -> ir.Value:
        """Own a one-source dict/set update, preserving other method results."""
        if self._expr_looks_cpython(expr.func.obj):
            return self._emit_cpy_method_call_src(
                self._emit_expr(expr.func.obj), "update", expr.args,
                kwargs=expr.kwargs, operand_order=expr.operand_order,
            )
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("update.result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver = self._emit_slot_call_operand(expr.func.obj, "update.receiver")
            roots.append(receiver)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            tag = self._slot_call_runtime_call("py_obj_type_tag", (receiver,), span=expr.span)
            is_dict = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_DICT))
            fn = self.current_function
            dict_bb = fn.append_basic_block(self._fresh("update.dict"))
            other_bb = fn.append_basic_block(self._fresh("update.other"))
            done_bb = fn.append_basic_block(self._fresh("update.done"))
            self.builder.cbranch(is_dict, dict_bb, other_bb)
            self.builder.position_at_end(dict_bb)
            source = self._emit_slot_call_operand(expr.args[0], "update.source")
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots) + (source,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self.builder.call(self.runtime["py_dict_update_slots"],
                [self._as_gc_ptr(receiver), self._as_gc_ptr(source)],
                name=self._fresh("update.dict.status"))
            self._slot_call_check_status(status, "dictionary update", expr.span)
            self._emit_post_call_err_check(expr.span)
            self._publish_slot_call_owned(output, self._emit_none_literal(), label="dictionary update")
            self._release_slot_call_roots((source,))
            self.builder.branch(done_bb)
            self.builder.position_at_end(other_bb)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            # Set dispatch or ordinary getattr happens before arguments. The
            # child borrows these slots, leaving their retirement to this owner.
            self._emit_set_algebra_call(
                expr, dynamic=True, receiver_slot=receiver, output_slot=output,
            )
            self.builder.branch(done_bb)
            self.builder.position_at_end(done_bb)
            self._release_slot_call_roots((receiver,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("update.current"))
        return self._take_slot_call_root(output)

    def _emit_dyn_pop_method_with_runtime_guard(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name != "pop" or expr.kwargs or len(expr.args) > 2:
            return None
        if self.current_function is None:
            return None
        if self._expr_looks_cpython(attr.obj):
            return self._emit_cpy_method_call_src(
                self._emit_expr(attr.obj), attr.name, expr.args, kwargs=expr.kwargs,
            )
        if (self._has_starred_unpack(expr.args)
                or self._split_starstar_kwargs_unpack(expr.args) is not None):
            return self._emit_slot_call_object(expr, label="dict.pop")
        if self._dyn_list_method_shape_supported(expr):
            return self._emit_owned_list_pop(expr)
        return _emit_rooted_dict_method(self, expr)

    def _emit_owned_dict_get(
        self,
        expr: Call,
        recv: ir.Value,
        recv_borrowed: bool = False,
    ) -> ir.Value:
        # Reserve the result root below operand roots. This preserves strict
        # LIFO cleanup and keeps an aliased default/result live while earlier
        # operands (including user keys with finalizers) are released.
        result_root = self._enter_container_temp_root(
            ir.Constant(_CSTR, None), self._fresh("dict.get.result"),
        )
        roots = [(result_root, False)]
        recv_root = self._enter_container_temp_root(recv, self._fresh("dict.get.recv"))
        # A receiver handed in by the caller is borrowed: the caller evaluated
        # it and releases it once on every path.  Claiming it here too dropped
        # the reference twice per ``d.get(...)``, so a dict reached through a
        # dyn receiver was freed while still in use and later reads saw
        # whatever object reused the slot -- "'tuple' object has no attribute
        # 'get'" from a variable that was a dict three calls earlier.  Root it
        # for liveness either way; only take ownership when we evaluated it.
        roots.append(
            (
                recv_root,
                False
                if recv_borrowed
                else self._owned_release_needed(recv, expr.func.obj),
            )
        )
        operands = [recv]
        for arg in expr.args:
            value = self._emit_expr_with_cpy_operand_cleanup(
                arg, (), as_pcc_object=True, rooted_pcc_lifetimes=tuple(roots),
            )
            if self._pcc_pointer_source_needs_pin(arg) and not self._value_is_never_gc_object(value):
                root = self._enter_container_temp_root(value, self._fresh("dict.get.arg"))
                owned = self._owned_release_needed(value, arg) or self._pcc_pointer_source_is_owned(arg)
                roots.append((root, owned))
            operands.append(value)
        if len(operands) == 2:
            operands.append(self._emit_none_literal())
        old_err = self._current_try_err_block()
        target = old_err if old_err is not None else self._ensure_fn_err_exit()
        self._try_err_block = self._make_cpy_operand_cleanup_block(
            (), (), target, "dict.get.error.cleanup", rooted_pcc_lifetimes=tuple(roots),
        )
        try:
            result = self.builder.call(
                self.runtime["py_dict_get_default"], operands, name=self._fresh("dict.get"),
            )
            self._emit_post_call_err_check(expr.span)
        finally:
            self._try_err_block = old_err
        self._gc_pin(result)
        self.builder.call(self.runtime["pcc_gc_store_root"], [self._as_gc_ptr(result_root), result])
        self._release_rooted_pcc_lifetimes(tuple(roots[1:]))
        result = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(result_root)],
            name=self._fresh("dict.get.current"),
        )
        self._leave_container_temp_root(result_root)
        self._note_owned_object_value(result)
        return result

    def _maybe_emit_dict_method(
        self,
        expr: Call,
        dict_ty: DictType,
        recv: Optional[ir.Value] = None,
        recv_borrowed: bool = False,
    ) -> Optional[ir.Value]:
        """Dispatch selected ``dict`` methods directly to runtime helpers."""
        attr = expr.func
        assert isinstance(attr, Attr)
        if self._has_unpacked_operands(expr.args, expr.kwargs):
            return None  # the generic method call binds unpacked arguments
        if expr.kwargs and attr.name != "update":
            return None  # d.update(k=v, ...) is handled below; others fall back
        name = attr.name
        if recv is None and self._dict_get_uses_owned_slots(expr):
            return self._emit_rooted_dict_get(expr)
        if (recv is None and name in ("keys", "values", "items", "copy")
                and not expr.args and not expr.kwargs
                and not self._expr_looks_cpython(attr.obj)):
            return _emit_rooted_dict_method(self, expr)
        if (recv is None and name == "pop" and len(expr.args) == 2
                and not expr.kwargs and not self._has_starred_unpack(expr.args)
                and self._split_starstar_kwargs_unpack(expr.args) is None
                and not self._expr_looks_cpython(attr.obj)):
            return _emit_rooted_dict_method(self, expr)
        if recv is None:
            recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            return self._emit_cpy_method_call_src(
                recv,
                name,
                expr.args,
                kwargs=expr.kwargs,
            )

        if name == "copy":
            if expr.args:
                return None
            return self.builder.call(
                self.runtime["py_copy_copy"],
                [recv],
                name=self._fresh("dict.copy"),
            )

        if name == "get":
            if len(expr.args) in (1, 2):
                return self._emit_owned_dict_get(expr, recv, recv_borrowed)
            return None
        if name == "keys":
            if expr.args:
                return None
            return self.builder.call(
                self.runtime["py_dict_keys"],
                [recv],
                name=self._fresh("dict.keys"),
            )
        if name == "values":
            if expr.args:
                return None
            return self.builder.call(
                self.runtime["py_dict_values"],
                [recv],
                name=self._fresh("dict.values"),
            )
        if name == "items":
            if expr.args:
                return None
            return self.builder.call(
                self.runtime["py_dict_items"],
                [recv],
                name=self._fresh("dict.items"),
            )
        if name == "popitem":
            if expr.args:
                return None
            result = self.builder.call(
                self.runtime["py_dict_popitem"],
                [recv],
                name=self._fresh("dict.popitem"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if name == "clear":
            if expr.args:
                return None
            self.builder.call(
                self.runtime["py_dict_clear"],
                [recv],
                name=self._fresh("dict.clear"),
            )
            return self._emit_none_literal()
        if name == "update":
            if len(expr.args) > 1:
                return None
            # named keyword pairs only — a ** splat (empty/`*`-prefixed key)
            # falls back to the generic path.
            for kname, _kv in (expr.kwargs or ()):
                if not kname or kname.startswith("*"):
                    return None
            if expr.args:
                source_expr = expr.args[0]
                source = _dict_method_box(self, source_expr)
                self.builder.call(
                    self.runtime["py_dict_update"],
                    [recv, source],
                    name=self._fresh("dict.update"),
                )
                # py_dict_update borrows its source.  Consume a fresh mapping
                # before branching through the error check so both success and
                # failure paths balance the temporary owner.
                self._gc_release_if_owned(source, source_expr)
                self._emit_post_call_err_check(expr.span)
            for kname, kv in (expr.kwargs or ()):
                self.builder.call(
                    self.runtime["py_dict_set"],
                    [
                        recv,
                        self._emit_str_literal(kname),
                        _dict_method_box(self, kv),
                    ],
                    name=self._fresh("dict.update.kw"),
                )
                self._emit_post_call_err_check(expr.span)
            return self._emit_none_literal()
        if name == "setdefault" and len(expr.args) in (1, 2):
            # ``d.setdefault(k, default)`` — if ``k`` exists, return
            # its value; otherwise insert and return ``default``.
            # ``d.setdefault(k)`` is the 1-arg form: ``default`` is
            # ``None`` (CPython inserts ``{k: None}`` and returns None).
            # Compile to: existing = py_dict_get(d, k); if existing is
            # NULL then py_dict_set(d, k, default); existing = default;
            # return existing.
            one_arg = len(expr.args) == 1
            k_obj = _dict_method_box(self, expr.args[0])
            if one_arg:
                # 1-arg form: default is ``None``. Load the immortal
                # singleton here (borrowed); it is incref'd on the miss
                # branch only, where it is actually inserted+returned, so
                # the phi result is an *owned* reference on both edges
                # (the hit edge returns the owned ref from py_dict_get),
                # mirroring the ownership contract of py_dict_get_default.
                default_obj = self._emit_none_literal()
            else:
                default_obj = _dict_method_box(self, expr.args[1])
            fn = self.current_function
            existing = self.builder.call(
                self.runtime["py_dict_get"],
                [recv, k_obj],
                name=self._fresh("setdefault.get"),
            )
            # NULL is also the hash-failure sentinel.  Preserve TypeError
            # before the ordinary missing-key branch inserts the default.
            self._emit_post_call_err_check(expr.span)
            null_p = ir.Constant(_CSTR, None)
            is_missing = self.builder.icmp_signed(
                "==",
                existing,
                null_p,
                name=self._fresh("setdefault.miss"),
            )
            miss_bb = fn.append_basic_block(
                name=self._fresh("setdefault.miss"),
            )
            join_bb = fn.append_basic_block(
                name=self._fresh("setdefault.join"),
            )
            cur_bb = self.builder._block
            self.builder.cbranch(is_missing, miss_bb, join_bb)
            self.builder.position_at_end(miss_bb)
            if one_arg:
                # Make the returned None an owned reference (only on the
                # miss edge; a hit returns py_dict_get's owned value).
                self.builder.call(
                    self.runtime["py_incref"],
                    [default_obj],
                )
            self.builder.call(
                self.runtime["py_dict_set"],
                [recv, k_obj, default_obj],
            )
            self._emit_post_call_err_check(expr.span)
            miss_exit = self.builder._block
            self.builder.branch(join_bb)
            self.builder.position_at_end(join_bb)
            phi = self.builder.phi(
                _CSTR,
                name=self._fresh("setdefault.result"),
            )
            phi.add_incoming(default_obj, miss_exit)
            phi.add_incoming(existing, cur_bb)
            return phi
        if name == "pop" and len(expr.args) == 1:
            result = self.builder.call(
                self.runtime["py_dict_pop"],
                [recv, _dict_method_box(self, expr.args[0])],
                name=self._fresh("dict.pop"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if name == "pop" and len(expr.args) == 2:
            k_obj = _dict_method_box(self, expr.args[0])
            default_obj = _dict_method_box(self, expr.args[1])
            fn = self.current_function
            existing = self.builder.call(
                self.runtime["py_dict_get"],
                [recv, k_obj],
                name=self._fresh("dict.pop.get"),
            )
            self._emit_post_call_err_check(expr.span)
            null_p = ir.Constant(_CSTR, None)
            is_missing = self.builder.icmp_signed(
                "==",
                existing,
                null_p,
                name=self._fresh("dict.pop.miss"),
            )
            hit_bb = fn.append_basic_block(name=self._fresh("dict.pop.hit"))
            miss_bb = fn.append_basic_block(name=self._fresh("dict.pop.miss"))
            join_bb = fn.append_basic_block(name=self._fresh("dict.pop.join"))
            self.builder.cbranch(is_missing, miss_bb, hit_bb)
            self.builder.position_at_end(hit_bb)
            self.builder.call(
                self.runtime["py_dict_del"],
                [recv, k_obj],
                name=self._fresh("dict.pop.del"),
            )
            self._emit_post_call_err_check(expr.span)
            hit_exit = self.builder._block
            self.builder.branch(join_bb)
            self.builder.position_at_end(miss_bb)
            miss_exit = self.builder._block
            self.builder.branch(join_bb)
            self.builder.position_at_end(join_bb)
            phi = self.builder.phi(_CSTR, name=self._fresh("dict.pop.result"))
            phi.add_incoming(existing, hit_exit)
            phi.add_incoming(default_obj, miss_exit)
            return phi
        return None
    def _maybe_emit_dict_builtin(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """Build an empty/keyword dict or shallow mapping copy in owned slots."""
        # Decide eligibility before evaluating or allocating anything. The
        # separate iterable/unpack lowering owns forms outside this helper.
        if len(expr.args) > 1 or self._has_unpacked_operands(expr.args, expr.kwargs):
            return None
        # A source that is not statically a dict or dynamic -- a list or
        # tuple of pairs, an instance with keys(), any iterable -- goes
        # through the owned update transaction, which resolves the mapping
        # protocol or iterates pairs as dict(x) does.
        protocol_source = bool(expr.args) and not isinstance(expr.args[0].ty, (DictType, DynType))
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("dict.constructor.result")
            roots.append(output)
        operands = []
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            source = None
            if expr.args:
                source = self._emit_slot_call_operand(expr.args[0], "dict.constructor.source")
                roots.append(source)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            # Python evaluates every argument before invoking the constructor.
            # Keep keyword values live even while a mapping key invokes __hash__.
            for keyword, operand in expr.kwargs:
                key_expr = StrLit(span=expr.span, ty=StrType(name="str"), value=keyword)
                key = self._emit_slot_call_operand(key_expr, "dict.constructor.keyword")
                roots.append(key)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                value = self._emit_slot_call_operand(operand, "dict.constructor.value")
                roots.append(value)
                operands.append((key, value))
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call("py_dict_new", (), result_slot=output, span=expr.span)
            if source is not None and protocol_source:
                status = self.builder.call(
                    self.runtime["py_dict_update_slots"],
                    [self._as_gc_ptr(output), self._as_gc_ptr(source)],
                    name=self._fresh("dict.constructor.update.status"),
                )
                self._slot_call_check_status(status, "dictionary protocol insertion", expr.span)
                self._emit_post_call_err_check(expr.span)
            elif source is not None:
                keys = self._new_slot_call_root("dict.copy.keys")
                roots.append(keys)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call("py_dict_keys", (source,), result_slot=keys, span=expr.span)
                fn = self.current_function
                end_bb = fn.append_basic_block(name=self._fresh("dict.copy.end"))
                if isinstance(expr.args[0].ty, DynType):
                    # A non-mapping returns NULL without setting exception TLS.
                    current_keys = self.builder.load(keys, name=self._fresh("dict.copy.keys.current"))
                    missing = self.builder.icmp_unsigned("==", current_keys, ir.Constant(_CSTR, None))
                    bad_bb = fn.append_basic_block(self._fresh("dict.copy.notmapping"))
                    ready_bb = fn.append_basic_block(self._fresh("dict.copy.keys.ok"))
                    self.builder.cbranch(missing, bad_bb, ready_bb)
                    self.builder.position_at_end(bad_bb)
                    # A dynamic source can implement the mapping protocol or
                    # yield pairs without being an exact list/tuple. Let the
                    # owned update transaction resolve keys()/__getitem__ or
                    # iteration, retaining its roots and exception semantics.
                    status = self.builder.call(
                        self.runtime["py_dict_update_slots"],
                        [self._as_gc_ptr(output), self._as_gc_ptr(source)],
                        name=self._fresh("dict.constructor.protocol.status"),
                    )
                    self._slot_call_check_status(status, "dictionary protocol insertion", expr.span)
                    self._emit_post_call_err_check(expr.span)
                    self.builder.branch(end_bb)
                    self.builder.position_at_end(ready_bb)
                count = self._slot_call_runtime_call("py_obj_len", (keys,), span=expr.span)
                index = self._alloca_in_entry(_I64, name="dict.copy.idx.addr")
                self.builder.store(ir.Constant(_I64, 0), index)
                cond_bb = fn.append_basic_block(self._fresh("dict.copy.cond"))
                body_bb = fn.append_basic_block(self._fresh("dict.copy.body"))
                self.builder.branch(cond_bb)
                self.builder.position_at_end(cond_bb)
                current = self.builder.load(index, name=self._fresh("dict.copy.index"))
                self.builder.cbranch(self.builder.icmp_signed("<", current, count), body_bb, end_bb)
                self.builder.position_at_end(body_bb)
                key = self._new_slot_call_root("dict.copy.key")
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots) + (key,), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_list_get", (keys,), result_slot=key, suffix_args=(current,), span=expr.span,
                )
                value = self._new_slot_call_root("dict.copy.value")
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots) + (key, value), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_dict_get", (source, key), result_slot=value, span=expr.span,
                )
                status = self.builder.call(
                    self.runtime["py_dict_set_slots"],
                    [self._as_gc_ptr(output), self._as_gc_ptr(key), self._as_gc_ptr(value)],
                    name=self._fresh("dict.copy.set.status"),
                )
                self._slot_call_check_status(status, "dictionary copy insertion", expr.span)
                self._emit_post_call_err_check(expr.span)
                self._release_slot_call_roots((key, value))
                self.builder.store(self.builder.add(current, ir.Constant(_I64, 1)), index)
                self.builder.branch(cond_bb)
                self.builder.position_at_end(end_bb)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._release_slot_call_roots((keys,))
                roots.pop()
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            for key, value in operands:
                status = self.builder.call(
                    self.runtime["py_dict_set_slots"],
                    [self._as_gc_ptr(output), self._as_gc_ptr(key), self._as_gc_ptr(value)],
                    name=self._fresh("dict.constructor.set.status"),
                )
                self._slot_call_check_status(status, "dictionary keyword insertion", expr.span)
                self._emit_post_call_err_check(expr.span)
            self._release_slot_call_roots(tuple(roots if sink is not None else roots[1:]))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if sink is not None:
            return self.builder.load(output, name=self._fresh("dict.constructor.current"))
        return self._take_slot_call_root(output)
