"""Dict builtin and method lowering helpers for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    DictExpr,
    DictType,
    DynType,
    Expr,
    Name,
    StrLit,
    StrType,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_DICT, PY_TYPE_LIST


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


class DictLoweringMixin:
    def _maybe_emit_dict_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in _DYN_DICT_METHOD_NATIVE:
            return None
        if attr.name == "pop":
            return self._emit_dyn_pop_method_with_runtime_guard(expr)
        dict_ty = DictType(
            name="dict",
            key=DynType(name="dyn"),
            value=DynType(name="dyn"),
        )
        return self._maybe_emit_dict_method(expr, dict_ty)

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

        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            return self._emit_cpy_method_call_src(
                recv,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
            )

        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [recv],
            name=self._fresh("dyn.pop.recv.tag"),
        )
        is_list = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_LIST),
            name=self._fresh("dyn.pop.recv.is_list"),
        )

        fn = self.current_function
        list_bb = fn.append_basic_block(name=self._fresh("dyn.pop.list"))
        non_list_bb = fn.append_basic_block(name=self._fresh("dyn.pop.non_list"))
        dict_bb = fn.append_basic_block(name=self._fresh("dyn.pop.dict"))
        generic_bb = fn.append_basic_block(name=self._fresh("dyn.pop.generic"))
        done_bb = fn.append_basic_block(name=self._fresh("dyn.pop.done"))
        self.builder.cbranch(is_list, list_bb, non_list_bb)

        self.builder.position_at_end(list_bb)
        if len(expr.args) <= 1:
            if len(expr.args) == 0:
                idx_val = ir.Constant(_I64, -1)
            else:
                idx_val = self._emit_expr_as_i64(expr.args[0])
            list_result = self.builder.call(
                self.runtime["py_list_pop"],
                [recv, idx_val],
                name=self._fresh("dyn.pop.list.result"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
        else:
            list_result = self._emit_generic_dyn_method_call_on_value(
                recv,
                attr.name,
                expr,
            )
        list_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(non_list_bb)
        is_dict = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_DICT),
            name=self._fresh("dyn.pop.recv.is_dict"),
        )
        self.builder.cbranch(is_dict, dict_bb, generic_bb)

        self.builder.position_at_end(dict_bb)
        if len(expr.args) == 1:
            dict_result = self.builder.call(
                self.runtime["py_dict_pop"],
                [recv, _dict_method_box(self, expr.args[0])],
                name=self._fresh("dyn.pop.dict.result"),
            )
            self._emit_post_call_err_check(expr.span)
        elif len(expr.args) == 2:
            k_obj = _dict_method_box(self, expr.args[0])
            default_obj = _dict_method_box(self, expr.args[1])
            existing = self.builder.call(
                self.runtime["py_dict_get"],
                [recv, k_obj],
                name=self._fresh("dyn.pop.dict.get"),
            )
            self._emit_post_call_err_check(expr.span)
            null_p = ir.Constant(_CSTR, None)
            is_missing = self.builder.icmp_signed(
                "==",
                existing,
                null_p,
                name=self._fresh("dyn.pop.dict.miss"),
            )
            hit_bb = fn.append_basic_block(name=self._fresh("dyn.pop.dict.hit"))
            miss_bb = fn.append_basic_block(name=self._fresh("dyn.pop.dict.miss"))
            dict_join_bb = fn.append_basic_block(
                name=self._fresh("dyn.pop.dict.join"),
            )
            self.builder.cbranch(is_missing, miss_bb, hit_bb)
            self.builder.position_at_end(hit_bb)
            self.builder.call(
                self.runtime["py_dict_del"],
                [recv, k_obj],
                name=self._fresh("dyn.pop.dict.del"),
            )
            self._emit_post_call_err_check(expr.span)
            hit_exit = self.builder.block
            self.builder.branch(dict_join_bb)
            self.builder.position_at_end(miss_bb)
            miss_exit = self.builder.block
            self.builder.branch(dict_join_bb)
            self.builder.position_at_end(dict_join_bb)
            dict_result = self.builder.phi(
                _CSTR,
                name=self._fresh("dyn.pop.dict.result"),
            )
            dict_result.add_incoming(existing, hit_exit)
            dict_result.add_incoming(default_obj, miss_exit)
        else:
            dict_result = self._emit_generic_dyn_method_call_on_value(
                recv,
                attr.name,
                expr,
            )
        dict_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        generic_result = self._emit_generic_dyn_method_call_on_value(
            recv,
            attr.name,
            expr,
        )
        generic_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_CSTR, name=self._fresh("dyn.pop.result"))
        result.add_incoming(list_result, list_exit)
        result.add_incoming(dict_result, dict_exit)
        result.add_incoming(generic_result, generic_exit)
        return result

    def _emit_owned_dict_get(self, expr: Call, recv: ir.Value) -> ir.Value:
        # Reserve the result root below operand roots. This preserves strict
        # LIFO cleanup and keeps an aliased default/result live while earlier
        # operands (including user keys with finalizers) are released.
        result_root = self._enter_container_temp_root(
            ir.Constant(_CSTR, None), self._fresh("dict.get.result"),
        )
        roots = [(result_root, False)]
        recv_root = self._enter_container_temp_root(recv, self._fresh("dict.get.recv"))
        roots.append((recv_root, self._owned_release_needed(recv, expr.func.obj)))
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
    ) -> Optional[ir.Value]:
        """Dispatch selected ``dict`` methods directly to runtime helpers."""
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs and attr.name != "update":
            return None  # d.update(k=v, ...) is handled below; others fall back
        name = attr.name
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
                return self._emit_owned_dict_get(expr, recv)
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
        """``dict()`` → empty dict. ``dict(k1=v1, k2=v2)`` → set
        each kwarg. ``dict(another_dict)`` where arg is DictType
        → shallow copy via iterator-over-keys.
        Iterable-of-pairs form isn't supported yet."""
        new_dict = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh("dict.new"),
        )
        # kwargs form
        if not expr.args and expr.kwargs:
            for kw_name, kw_expr in expr.kwargs:
                k_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    self._emit_str_literal(kw_name),
                    StrType(name="str"),
                )
                v = self._emit_expr(kw_expr)
                v_obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    kw_expr.ty,
                )
                self.builder.call(
                    self.runtime["py_dict_set"],
                    [new_dict, k_obj, v_obj],
                )
                self._emit_post_call_err_check(expr.span)
            return new_dict
        if not expr.args:
            return new_dict
        arg = expr.args[0]
        arg_ty = arg.ty
        if isinstance(arg_ty, DictType) or isinstance(arg_ty, DynType):
            # Shallow copy of a dict — iterate keys, get values,
            # insert into the new dict.
            src_val = self._emit_expr(arg)
            src_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                src_val,
                arg_ty,
            )
            keys_list = self.builder.call(
                self.runtime["py_dict_keys"],
                [src_obj],
                name=self._fresh("dict.copy.keys"),
            )
            fn = self.current_function
            if isinstance(arg_ty, DynType):
                # py_dict_keys on a NON-dict returns NULL without raising
                # (py_dict.c), so a dyn-held non-mapping silently produced an
                # empty dict here.  CPython raises TypeError; fail closed.
                keys_null = self.builder.icmp_unsigned(
                    "==",
                    keys_list,
                    ir.Constant(keys_list.type, None),
                    name=self._fresh("dict.copy.keys.null"),
                )
                bad_bb = fn.append_basic_block(
                    name=self._fresh("dict.copy.notmapping")
                )
                ok_bb = fn.append_basic_block(
                    name=self._fresh("dict.copy.keys.ok")
                )
                self.builder.cbranch(keys_null, bad_bb, ok_bb)
                self.builder.position_at_end(bad_bb)
                message = self._ptr_to_cstr(
                    self._cstr_global(
                        "dict() argument is not iterable",
                        ".dict.copy.typeerror",
                    )
                )
                exc = self.builder.call(
                    self.runtime["py_exc_new"],
                    [ir.Constant(_I64, 3), message],
                    name=self._fresh("dict.copy.exc"),
                )
                self.builder.call(self.runtime["py_raise"], [exc])
                err_target = (
                    self._current_try_err_block()
                    or self._ensure_fn_err_exit()
                )
                self.builder.branch(err_target)
                self.builder.position_at_end(ok_bb)
            n_val = self.builder.call(
                self.runtime["py_obj_len"],
                [keys_list],
                name=self._fresh("dict.copy.len"),
            )
            idx_slot = self._alloca_in_entry(
                _I64,
                name="dict.copy.idx.addr",
            )
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            cond_bb = fn.append_basic_block(
                name=self._fresh("dict.copy.cond"),
            )
            body_bb = fn.append_basic_block(
                name=self._fresh("dict.copy.body"),
            )
            step_bb = fn.append_basic_block(
                name=self._fresh("dict.copy.step"),
            )
            end_bb = fn.append_basic_block(
                name=self._fresh("dict.copy.end"),
            )
            self.builder.branch(cond_bb)
            self.builder.position_at_end(cond_bb)
            cur = self.builder.load(idx_slot, name=self._fresh("idx"))
            cond = self.builder.icmp_signed(
                "<",
                cur,
                n_val,
                name=self._fresh("cond.i1"),
            )
            self.builder.cbranch(cond, body_bb, end_bb)
            self.builder.position_at_end(body_bb)
            k_elem = self.builder.call(
                self.runtime["py_list_get"],
                [keys_list, cur],
                name=self._fresh("dict.copy.key"),
            )
            v_elem = self.builder.call(
                self.runtime["py_dict_get"],
                [src_obj, k_elem],
                name=self._fresh("dict.copy.val"),
            )
            # On this edge v_elem is the raising call's own NULL return
            # (pcc_gc_release is NULL-safe); k_elem and the keys view are
            # live owned references that the error exit must drop.
            self._emit_post_call_err_check(
                expr.span,
                release_on_error=(v_elem, k_elem, keys_list),
            )
            self.builder.call(
                self.runtime["py_dict_set"],
                [new_dict, k_elem, v_elem],
            )
            # py_list_get and py_dict_get both return NEW refs
            # (py_runtime.h), and py_dict_set retains what it stores rather
            # than stealing.  Without these two releases every entry of the
            # copy leaked one key and one value.
            #
            # Released BEFORE the error check, not after: pcc_gc_release does
            # not touch the exception TLS, so doing it first makes the
            # raising edge out of py_dict_set drop these references too.
            self._gc_release(
                v_elem, self._release_context_label("dict.copy.val")
            )
            self._gc_release(
                k_elem, self._release_context_label("dict.copy.key")
            )
            self._emit_post_call_err_check(
                expr.span,
                release_on_error=(keys_list,),
            )
            self.builder.branch(step_bb)
            self.builder.position_at_end(step_bb)
            nxt = self.builder.add(
                cur,
                ir.Constant(_I64, 1),
                name=self._fresh("idx.next"),
            )
            self.builder.store(nxt, idx_slot)
            self.builder.branch(cond_bb)
            self.builder.position_at_end(end_bb)
            # py_dict_keys returns a NEW list ref; end_bb is this loop's only
            # exit.
            self._gc_release(
                keys_list, self._release_context_label("dict.copy.keys")
            )
            return new_dict
        return None
