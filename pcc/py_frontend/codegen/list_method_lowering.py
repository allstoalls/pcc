"""List method lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BinOp,
    BoolLit,
    Call,
    ClassType,
    DynType,
    Expr,
    IntLit,
    Lambda,
    ListType,
    Name,
    StrType,
    Subscript,
    TupleExpr,
    Type,
    UnaryOp,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_LIST

_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()

# Sentinel "end" for the 2-arg ``list.index(x, start)`` form: py_list_index_range
# clamps any ``end > length`` down to ``length``, so a value safely larger than
# any realizable list length means "to the end of the list".
_LIST_INDEX_END_SENTINEL = 1 << 62

# No-arg str methods usable as an inline ``key=lambda s: s.<m>()`` over a list
# of str (case-insensitive sort etc.); each maps to ``py_str_<m>``.
_NOARG_STR_KEY_METHODS = frozenset(
    {"lower", "upper", "casefold", "title", "capitalize", "swapcase", "strip"}
)

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
        "copy",
    }
)


def _list_method_box(host, e: Expr) -> ir.Value:
    return host._emit_expr_as_pcc_object(e)


class ListMethodLoweringMixin:
    def _maybe_emit_list_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in _DYN_LIST_METHOD_NATIVE:
            return None
        return self._emit_dyn_list_method_with_runtime_guard(expr)

    def _emit_generic_dyn_method_call_on_value(
        self,
        recv_obj: ir.Value,
        attr_name: str,
        expr: Call,
    ) -> ir.Value:
        method_obj = self.builder.call(
            self.runtime["py_obj_getattr"],
            [recv_obj, self._attr_name_ptr(attr_name)],
            name=self._fresh(f"dyn.attr.{attr_name}"),
        )
        self._emit_attribute_error_if_null(method_obj, attr_name, expr.func.span)
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
            name=self._fresh(f"dyn.method.{attr_name}"),
        )
        if args_owned:
            self._gc_release(args_tuple)
        if expr.kwargs or kwargs_expr is not None:
            self._gc_release(kwargs_obj)
        self._emit_post_call_err_check(expr.span)
        return result

    def _emit_dyn_list_native_method(
        self,
        recv: ir.Value,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        name = attr.name

        sort_key_spec = None
        sort_reverse_const = False
        if expr.kwargs:
            if name != "sort":
                return None
            for k, v in expr.kwargs:
                if k == "reverse" and isinstance(v, BoolLit):
                    sort_reverse_const = bool(v.value)
                elif k == "key":
                    sort_key_spec = self._key_spec_from_callable(
                        v,
                        DynType(name="dyn"),
                    )
                    if sort_key_spec is None:
                        sort_key_spec = ("callable", self._emit_as_object(v))
                else:
                    return None
        if any(
            isinstance(a, Call)
            and isinstance(a.func, Name)
            and a.func.ident in ("*", "__starred__")
            for a in expr.args
        ):
            return None

        if name == "sort":
            if expr.args:
                return None
            working = self._begin_list_sort_transaction(recv)
            if sort_key_spec is not None:
                self._emit_list_sort_by_key(working, sort_key_spec)
                return self._finish_list_sort_transaction(
                    recv, working, sort_reverse_const
                )
            sorted_list = self.builder.call(
                self.runtime["py_obj_sorted"],
                [working],
                name=self._fresh("dyn.list.sort.sorted"),
            )
            self._gc_release(working)
            return self._finish_list_sort_transaction(
                recv, sorted_list, sort_reverse_const
            )

        if name == "append":
            if len(expr.args) != 1:
                return None
            item = _list_method_box(self, expr.args[0])
            item_root = ir.Constant(_CSTR, None)
            item_for_call = item
            if isinstance(item.type, ir.PointerType) and item not in getattr(
                self, "_cpy_values", ()
            ):
                item_root = self._enter_container_temp_root(item, "dyn.list.item")
                item_for_call = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [
                        ir.Constant(_CSTR, None),
                        self._as_gc_ptr(
                            item_root,
                            name=self._fresh("dyn.list.item.root.ptr"),
                        ),
                    ],
                    name=self._fresh("dyn.list.item.current"),
                )
            self.builder.call(self.runtime["py_list_append"], [recv, item_for_call])
            item_after_append = item_for_call
            if not isinstance(item_root, ir.Constant):
                item_after_append = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [
                        ir.Constant(_CSTR, None),
                        self._as_gc_ptr(
                            item_root,
                            name=self._fresh("dyn.list.item.release.root.ptr"),
                        ),
                    ],
                    name=self._fresh("dyn.list.item.release.current"),
                )
            if self._container_store_temp_needs_release(
                expr.args[0],
                expr.args[0].ty,
                False,
                item,
            ):
                self._gc_release(item_after_append)
            self._leave_container_temp_root(item_root)
            return self._emit_none_literal()

        if name == "extend":
            if len(expr.args) != 1:
                return None
            self.builder.call(
                self.runtime["py_list_extend"],
                [recv, _list_method_box(self, expr.args[0])],
            )
            return self._emit_none_literal()

        if name == "insert":
            if len(expr.args) != 2:
                return None
            idx_val = self._emit_expr_as_i64(expr.args[0])
            self.builder.call(
                self.runtime["py_list_insert"],
                [recv, idx_val, _list_method_box(self, expr.args[1])],
            )
            return self._emit_none_literal()

        if name == "pop":
            if len(expr.args) == 0:
                idx_val = ir.Constant(_I64, -1)
            elif len(expr.args) == 1:
                idx_val = self._emit_expr_as_i64(expr.args[0])
            else:
                return None
            popped = self.builder.call(
                self.runtime["py_list_pop"],
                [recv, idx_val],
                name=self._fresh("dyn.list.pop"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return popped

        if name == "remove":
            if len(expr.args) != 1:
                return None
            self.builder.call(
                self.runtime["py_list_remove"],
                [recv, _list_method_box(self, expr.args[0])],
            )
            return self._emit_none_literal()

        if name == "clear":
            if len(expr.args) != 0:
                return None
            self.builder.call(self.runtime["py_list_clear"], [recv])
            return self._emit_none_literal()

        if name == "index":
            if len(expr.args) < 1 or len(expr.args) > 3:
                return None
            if len(expr.args) == 1:
                idx = self.builder.call(
                    self.runtime["py_list_index"],
                    [recv, _list_method_box(self, expr.args[0])],
                    name=self._fresh("dyn.list.index.i64"),
                )
            else:
                start_val = self._emit_expr_as_i64(expr.args[1])
                if len(expr.args) == 3:
                    end_val = self._emit_expr_as_i64(expr.args[2])
                else:
                    end_val = ir.Constant(_I64, _LIST_INDEX_END_SENTINEL)
                idx = self.builder.call(
                    self.runtime["py_list_index_range"],
                    [
                        recv,
                        _list_method_box(self, expr.args[0]),
                        start_val,
                        end_val,
                    ],
                    name=self._fresh("dyn.list.index.range.i64"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [idx],
                name=self._fresh("dyn.list.index"),
            )

        if name == "count":
            if len(expr.args) != 1:
                return None
            count = self.builder.call(
                self.runtime["py_list_count"],
                [recv, _list_method_box(self, expr.args[0])],
                name=self._fresh("dyn.list.count.i64"),
            )
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [count],
                name=self._fresh("dyn.list.count"),
            )

        if name == "reverse":
            if len(expr.args) != 0:
                return None
            self.builder.call(self.runtime["py_list_reverse"], [recv])
            return self._emit_none_literal()

        if name == "copy":
            if len(expr.args) != 0:
                return None
            return self.builder.call(
                self.runtime["py_list_copy"],
                [recv],
                name=self._fresh("dyn.list.copy"),
            )

        return None

    def _dyn_list_method_shape_supported(self, expr: Call) -> bool:
        attr = expr.func
        assert isinstance(attr, Attr)
        name = attr.name
        if any(
            isinstance(a, Call)
            and isinstance(a.func, Name)
            and a.func.ident in ("*", "__starred__")
            for a in expr.args
        ):
            return False
        if expr.kwargs:
            if name != "sort":
                return False
            for k, v in expr.kwargs:
                if k == "reverse" and isinstance(v, BoolLit):
                    continue
                if (
                    k == "key"
                    and self._key_spec_from_callable(
                        v,
                        DynType(name="dyn"),
                    )
                    is not None
                ):
                    continue
                return False
        if name == "sort":
            return not expr.args
        if name == "index":
            # list.index(x[, start[, end]]) — 1..3 positional args.
            return 1 <= len(expr.args) <= 3 and not expr.kwargs
        if name in ("append", "extend", "remove", "count"):
            return len(expr.args) == 1 and not expr.kwargs
        if name == "insert":
            return len(expr.args) == 2 and not expr.kwargs
        if name == "pop":
            return len(expr.args) <= 1 and not expr.kwargs
        if name in ("clear", "reverse", "copy"):
            return not expr.args and not expr.kwargs
        return False

    def _emit_dyn_list_method_with_runtime_guard(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in _DYN_LIST_METHOD_NATIVE:
            return None
        if not self._dyn_list_method_shape_supported(expr):
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
            name=self._fresh("dyn.list.recv.tag"),
        )
        is_list = self.builder.icmp_signed(
            "==",
            tag,
            ir.Constant(_I64, PY_TYPE_LIST),
            name=self._fresh("dyn.list.recv.is_list"),
        )

        fn = self.current_function
        list_bb = fn.append_basic_block(name=self._fresh("dyn.list.native"))
        generic_bb = fn.append_basic_block(name=self._fresh("dyn.list.generic"))
        done_bb = fn.append_basic_block(name=self._fresh("dyn.list.done"))
        self.builder.cbranch(is_list, list_bb, generic_bb)

        self.builder.position_at_end(list_bb)
        list_result = self._emit_dyn_list_native_method(recv, expr)
        if list_result is None:
            return None
        list_exit = self.builder.block
        self._gc_release_if_owned(recv, attr.obj)
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        generic_result = self._emit_generic_dyn_method_call_on_value(
            recv,
            attr.name,
            expr,
        )
        generic_exit = self.builder.block
        self._gc_release_if_owned(recv, attr.obj)
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_CSTR, name=self._fresh("dyn.list.result"))
        result.add_incoming(list_result, list_exit)
        result.add_incoming(generic_result, generic_exit)
        return result

    def _maybe_emit_list_method(
        self,
        expr: Call,
        list_ty: ListType,
    ) -> Optional[ir.Value]:
        """Dispatch selected ``list`` methods directly to runtime helpers
        (libpython-free). Returns None when the method isn't in the fast
        path so callers can fall through to the generic dispatch."""
        attr = expr.func
        assert isinstance(attr, Attr)
        sort_key_spec = None
        sort_reverse_const = False
        if expr.kwargs:
            if attr.name != "sort":
                return None  # generic path handles or errors
            for k, v in expr.kwargs:
                if k == "reverse" and isinstance(v, BoolLit):
                    sort_reverse_const = bool(v.value)
                elif k == "key":
                    sort_key_spec = self._key_spec_from_callable(v, list_ty.elem)
                    if sort_key_spec is None:
                        sort_key_spec = ("callable", self._emit_as_object(v))
                else:
                    return None
        # Starred argument (``lst.method(*args)``) — bail to generic
        # CPython dispatch which handles splats via py_cpy_call_list.
        if any(
            isinstance(a, Call)
            and isinstance(a.func, Name)
            and a.func.ident in ("*", "__starred__")
            for a in expr.args
        ):
            return None
        name = attr.name
        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            return self._emit_cpy_method_call_src(
                recv,
                name,
                expr.args,
                kwargs=expr.kwargs,
            )

        if name == "sort":
            if expr.args:
                return None
            # sort(key=<supported inline callable>, reverse=<bool const>):
            # sort in place, then optionally reverse. Unsupported key callables
            # and non-constant reverse were already bounced by the kwargs guard.
            working = self._begin_list_sort_transaction(recv)
            if sort_key_spec is not None:
                self._emit_list_sort_by_key(working, sort_key_spec)
                return self._finish_list_sort_transaction(
                    recv, working, sort_reverse_const
                )
            elem_hint = None
            if isinstance(attr.obj, Name):
                elem_hint = self.env_list_elem_class_hint.get(attr.obj.ident)
            if elem_hint is not None:
                self._emit_list_sort_with_dunder_lt(
                    working,
                    elem_hint,
                    list_ty.elem,
                )
                return self._finish_list_sort_transaction(
                    recv, working, sort_reverse_const
                )
            sorted_list = self.builder.call(
                self.runtime["py_obj_sorted"],
                [working],
                name=self._fresh("list.sort.sorted"),
            )
            self._gc_release(working)
            return self._finish_list_sort_transaction(
                recv, sorted_list, sort_reverse_const
            )

        if name == "append":
            if len(expr.args) != 1:
                return None
            if isinstance(attr.obj, Name):
                item_kind = self._threading_constructor_kind_for_expr(
                    expr.args[0]
                ) or self._threading_kind_for_receiver_expr(expr.args[0])
                if item_kind is not None:
                    current_kind = self._threading_list_elem_flags.get(attr.obj.ident)
                    if current_kind is None or current_kind == item_kind:
                        self._threading_list_elem_flags[attr.obj.ident] = item_kind
                    else:
                        self._threading_list_elem_flags.pop(attr.obj.ident, None)
            item_expr = expr.args[0]
            self._last_fresh_direct_native_ctor_value = None
            item = _list_method_box(self, item_expr)
            fresh_ctor_value = self._last_fresh_direct_native_ctor_value
            self._last_fresh_direct_native_ctor_value = None
            fresh_ctor_admitted = item is fresh_ctor_value
            ctor_info = None
            ctor_name = ""
            ctor_has_captures = False
            ctor_has_custom_new = False
            ctor_has_nonobject_base = False
            if (
                fresh_ctor_admitted
                and isinstance(item_expr, Call)
                and isinstance(item_expr.func, Name)
                and not item_expr.kwargs
                and isinstance(item_expr.ty, ClassType)
                and not item_expr.ty.valueclass
            ):
                ctor_name = item_expr.func.ident
                class_aliases = getattr(self, "_class_aliases", {})
                ctor_is_alias = (
                    ctor_name in class_aliases
                    and class_aliases[ctor_name] != ctor_name
                )
                if ctor_name in self.class_lowering.classes:
                    ctor_info = self.class_lowering.classes[ctor_name]
                if ctor_info is not None:
                    ctor_has_custom_new = "__new__" in ctor_info.methods
                    for ctor_base in ctor_info.bases_ast:
                        if not (
                            isinstance(ctor_base, Name)
                            and ctor_base.ident == "object"
                        ):
                            # Fail closed instead of walking an imported/local
                            # MRO here.  The shared MRO helper still contains a
                            # dict.get missing-key path known to mis-lower in
                            # pcc1; a non-object base can also inherit __new__.
                            ctor_has_nonobject_base = True
                class_capture_map = getattr(
                    self,
                    "_hoisted_class_capture_params",
                    {},
                )
                if ctor_name in class_capture_map:
                    ctor_has_captures = bool(class_capture_map[ctor_name])
                fresh_ctor_admitted = (
                    not ctor_is_alias
                    and ctor_info is not None
                    and not getattr(ctor_info, "valueclass", False)
                    and getattr(ctor_info, "metaclass_name", None) is None
                    and getattr(ctor_info, "owning_module", None)
                    in (None, self.ast_module.name)
                    and not ctor_has_captures
                    and not ctor_has_custom_new
                    and not ctor_has_nonobject_base
                )
            else:
                fresh_ctor_admitted = False
            recv_slot = None
            recv_is_cpy = False
            if isinstance(attr.obj, Name):
                if attr.obj.ident in self.env:
                    recv_slot = self.env[attr.obj.ident]
                cpy_env_flags = getattr(self, "_cpy_env_flags", {})
                if attr.obj.ident in cpy_env_flags:
                    recv_is_cpy = bool(cpy_env_flags[attr.obj.ident])
            stable_rooted_recv = (
                isinstance(attr.obj, Name)
                and recv_slot is not None
                and isinstance(recv_slot[1], ir.PointerType)
                and attr.obj.ident in getattr(self, "_gc_rooted_local_names", set())
                and attr.obj.ident not in getattr(self, "_current_global_names", set())
                and not recv_is_cpy
            )
            item_root = ir.Constant(_CSTR, None)
            item_for_call = item
            item_needs_release = self._container_store_temp_needs_release(
                expr.args[0],
                expr.args[0].ty,
                False,
                item,
            )
            if (
                isinstance(item.type, ir.PointerType)
                and item not in getattr(self, "_cpy_values", ())
                # The root exists so the post-append RELEASE of an owned item
                # temporary reloads a pointer the append may have relocated.
                # A borrowed item (a rooted local, a module global) is never
                # released here and stays alive through its own slot, so the
                # root, its reload and the leave were pure protocol.
                and item_needs_release
                # A value already proven not to be a GC object -- a tagged
                # small-int literal, recorded by literal lowering -- needs no
                # temporary root: pin/unpin/store_root all begin by testing
                # is_tagged_int and returning, so the calls are pure overhead.
                # Measured: `lst.append(i)` emitted 23 GC operations per loop
                # iteration for one append, and pin+unpin+store_root were 37%
                # of the loop's samples against 5.6% for the append itself.
                # Only covers literals; the variable case is handled by the
                # tagged fast paths inside the runtime barriers themselves.
                # This is NOT keyed on the static type being `int`: `int` is
                # arbitrary-precision, so a large value is a heap bignum that
                # must still be rooted.  Only the proven-tagged registry is safe.
                and not self._value_is_never_gc_object(item)
            ):
                item_root = self._enter_container_temp_root(item, "list.item")
                item_for_call = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [
                        ir.Constant(_CSTR, None),
                        self._as_gc_ptr(
                            item_root,
                            name=self._fresh("list.item.root.ptr"),
                        ),
                    ],
                    name=self._fresh("list.item.current"),
                )
            append_recv = recv
            append_runtime_name = "py_list_append"
            if fresh_ctor_admitted and stable_rooted_recv:
                # The constructor can safepoint and relocate the receiver.
                # Reload the same stable local root after evaluating the item;
                # non-local/compound receivers stay on the generic path rather
                # than being re-evaluated out of Python's source order.
                append_recv = self._emit_name(attr.obj)
                append_runtime_name = "py_list_append_fresh_native_instance"
            self.builder.call(
                self.runtime[append_runtime_name],
                [append_recv, item_for_call],
            )
            item_after_append = item_for_call
            if not isinstance(item_root, ir.Constant):
                item_after_append = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [
                        ir.Constant(_CSTR, None),
                        self._as_gc_ptr(
                            item_root,
                            name=self._fresh("list.item.release.root.ptr"),
                        ),
                    ],
                    name=self._fresh("list.item.release.current"),
                )
            if item_needs_release:
                self._gc_release(item_after_append)
            self._leave_container_temp_root(item_root)
            self._gc_release_if_owned(recv, attr.obj)
            return self._emit_none_literal()
        if name == "extend":
            if len(expr.args) != 1:
                return None
            self.builder.call(
                self.runtime["py_list_extend"],
                [recv, _list_method_box(self, expr.args[0])],
            )
            return self._emit_none_literal()
        if name == "insert":
            if len(expr.args) != 2:
                return None
            idx_val = self._emit_expr_as_i64(expr.args[0])
            self.builder.call(
                self.runtime["py_list_insert"],
                [recv, idx_val, _list_method_box(self, expr.args[1])],
            )
            return self._emit_none_literal()
        if name == "pop":
            if len(expr.args) == 0:
                idx_val = ir.Constant(_I64, -1)
            elif len(expr.args) == 1:
                idx_val = self._emit_expr_as_i64(expr.args[0])
            else:
                return None
            popped = self.builder.call(
                self.runtime["py_list_pop"],
                [recv, idx_val],
                name=self._fresh("list.pop"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            if not isinstance(list_ty.elem, DynType):
                return marshal.marshal_from_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    popped,
                    list_ty.elem,
                )
            return popped
        if name == "remove":
            if len(expr.args) != 1:
                return None
            self.builder.call(
                self.runtime["py_list_remove"],
                [recv, _list_method_box(self, expr.args[0])],
            )
            return self._emit_none_literal()
        if name == "clear":
            if len(expr.args) != 0:
                return None
            self.builder.call(self.runtime["py_list_clear"], [recv])
            return self._emit_none_literal()
        if name == "index":
            if len(expr.args) < 1 or len(expr.args) > 3:
                return None
            if len(expr.args) == 1:
                return self.builder.call(
                    self.runtime["py_list_index"],
                    [recv, _list_method_box(self, expr.args[0])],
                    name=self._fresh("list.index"),
                )
            start_val = self._emit_expr_as_i64(expr.args[1])
            if len(expr.args) == 3:
                end_val = self._emit_expr_as_i64(expr.args[2])
            else:
                end_val = ir.Constant(_I64, _LIST_INDEX_END_SENTINEL)
            res = self.builder.call(
                self.runtime["py_list_index_range"],
                [recv, _list_method_box(self, expr.args[0]), start_val, end_val],
                name=self._fresh("list.index.range"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        if name == "count":
            if len(expr.args) != 1:
                return None
            return self.builder.call(
                self.runtime["py_list_count"],
                [recv, _list_method_box(self, expr.args[0])],
                name=self._fresh("list.count"),
            )
        if name == "reverse":
            if len(expr.args) != 0:
                return None
            self.builder.call(self.runtime["py_list_reverse"], [recv])
            return self._emit_none_literal()
        if name == "copy":
            if len(expr.args) != 0:
                return None
            new_list = self.builder.call(
                self.runtime["py_list_copy"],
                [recv],
                name=self._fresh("list.copy"),
            )
            self._gc_release_if_owned(recv, attr.obj)
            return new_list
        return None

    def _begin_list_sort_transaction(self, recv: ir.Value) -> ir.Value:
        working = self.builder.call(
            self.runtime["py_list_copy"],
            [recv],
            name=self._fresh("list.sort.working"),
        )
        # CPython exposes an empty receiver during key/comparison callbacks.
        # The working copy owns the original elements, so clear cannot run an
        # element finalizer before sorting begins.
        self.builder.call(self.runtime["py_list_clear"], [recv])
        return working

    def _finish_list_sort_transaction(
        self,
        recv: ir.Value,
        ordered: ir.Value,
        reverse: bool,
    ) -> ir.Value:
        if reverse:
            self.builder.call(self.runtime["py_list_reverse"], [ordered])
        mutated_len = self.builder.call(
            self.runtime["py_list_len"],
            [recv],
            name=self._fresh("list.sort.mutated.len"),
        )
        mutated = self.builder.icmp_signed(
            "!=",
            mutated_len,
            ir.Constant(_I64, 0),
            name=self._fresh("list.sort.mutated"),
        )
        none_obj = self._emit_none_literal()
        self.builder.call(
            self.runtime["py_list_set_slice"],
            [recv, none_obj, none_obj, none_obj, ordered],
        )
        self._gc_release(ordered)

        fn = self.current_function
        mutated_bb = fn.append_basic_block(
            name=self._fresh("list.sort.mutated.error")
        )
        ok_bb = fn.append_basic_block(name=self._fresh("list.sort.ok"))
        self.builder.cbranch(mutated, mutated_bb, ok_bb)
        self.builder.position_at_end(mutated_bb)
        message = self._ptr_to_cstr(
            self._cstr_global(
                "list modified during sort",
                ".list.sort.mutated.error",
            )
        )
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 2), message],
            name=self._fresh("list.sort.mutated.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        err_target = (
            getattr(self, "_try_err_block", None)
            or self._ensure_fn_err_exit()
        )
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)
        return none_obj

    def _emit_list_sort_with_dunder_lt(
        self,
        recv: ir.Value,
        elem_hint: str,
        elem_ty: Type,
    ) -> ir.Value:
        lt_info = self._resolve_method_mro(elem_hint, "__lt__")
        if lt_info is None:
            return self._emit_none_literal()
        lt_fn = lt_info.methods["__lt__"]

        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_list_len"],
            [recv],
            name=self._fresh("list.sort.len"),
        )
        i_slot = self._alloca_in_entry(_I64, name="list.sort.i.addr")
        j_slot = self._alloca_in_entry(_I64, name="list.sort.j.addr")
        cur_slot = self._alloca_in_entry(_CSTR, name="list.sort.cur.addr")
        self.builder.store(ir.Constant(_I64, 1), i_slot)

        outer_cond = fn.append_basic_block(name=self._fresh("list.sort.cond"))
        outer_body = fn.append_basic_block(name=self._fresh("list.sort.body"))
        inner_cond = fn.append_basic_block(name=self._fresh("list.sort.inner"))
        inner_cmp = fn.append_basic_block(name=self._fresh("list.sort.cmp"))
        inner_shift = fn.append_basic_block(name=self._fresh("list.sort.shift"))
        inner_done = fn.append_basic_block(name=self._fresh("list.sort.place"))
        outer_step = fn.append_basic_block(name=self._fresh("list.sort.step"))
        done_bb = fn.append_basic_block(name=self._fresh("list.sort.done"))

        self.builder.branch(outer_cond)
        self.builder.position_at_end(outer_cond)
        i_val = self.builder.load(i_slot, name=self._fresh("list.sort.i"))
        outer_ok = self.builder.icmp_signed(
            "<",
            i_val,
            n_val,
            name=self._fresh("list.sort.outer_ok"),
        )
        self.builder.cbranch(outer_ok, outer_body, done_bb)

        self.builder.position_at_end(outer_body)
        cur = self.builder.call(
            self.runtime["py_list_get"],
            [recv, i_val],
            name=self._fresh("list.sort.cur"),
        )
        self.builder.store(cur, cur_slot)
        self.builder.store(i_val, j_slot)
        self.builder.branch(inner_cond)

        self.builder.position_at_end(inner_cond)
        j_val = self.builder.load(j_slot, name=self._fresh("list.sort.j"))
        j_gt_zero = self.builder.icmp_signed(
            ">",
            j_val,
            ir.Constant(_I64, 0),
            name=self._fresh("list.sort.j_gt_zero"),
        )
        self.builder.cbranch(j_gt_zero, inner_cmp, inner_done)

        self.builder.position_at_end(inner_cmp)
        j_prev = self.builder.sub(
            j_val,
            ir.Constant(_I64, 1),
            name=self._fresh("list.sort.j_prev"),
        )
        prev = self.builder.call(
            self.runtime["py_list_get"],
            [recv, j_prev],
            name=self._fresh("list.sort.prev"),
        )
        cur = self.builder.load(cur_slot, name=self._fresh("list.sort.cur2"))
        less = self._emit_direct_method_value_call(
            lt_fn,
            cur,
            lt_info,
            "__lt__",
            ((prev, elem_ty),),
        )
        if less.type is not _I1:
            if isinstance(less.type, ir.IntType):
                less = self.builder.icmp_signed(
                    "!=",
                    less,
                    ir.Constant(less.type, 0),
                    name=self._fresh("list.sort.less_i1"),
                )
            else:
                truth = self.builder.call(
                    self.runtime["py_obj_truthy"],
                    [less],
                    name=self._fresh("list.sort.less_truthy"),
                )
                less = self.builder.icmp_signed(
                    "!=",
                    truth,
                    ir.Constant(_I32, 0),
                    name=self._fresh("list.sort.less_truthy_i1"),
                )
        self.builder.cbranch(less, inner_shift, inner_done)

        self.builder.position_at_end(inner_shift)
        self.builder.call(self.runtime["py_list_set"], [recv, j_val, prev])
        self.builder.store(j_prev, j_slot)
        self.builder.branch(inner_cond)

        self.builder.position_at_end(inner_done)
        j_final = self.builder.load(j_slot, name=self._fresh("list.sort.j_final"))
        cur_final = self.builder.load(cur_slot, name=self._fresh("list.sort.cur3"))
        self.builder.call(
            self.runtime["py_list_set"],
            [recv, j_final, cur_final],
        )
        self.builder.branch(outer_step)

        self.builder.position_at_end(outer_step)
        i_next = self.builder.add(
            self.builder.load(i_slot, name=self._fresh("list.sort.i2")),
            ir.Constant(_I64, 1),
            name=self._fresh("list.sort.i_next"),
        )
        self.builder.store(i_next, i_slot)
        self.builder.branch(outer_cond)

        self.builder.position_at_end(done_bb)
        return self._emit_none_literal()

    def _sorted_key_spec_from_lambda(self, key_lambda, elem_ty=None):
        """Detect a simple single-param ``key=`` lambda we can inline without
        first-class-function boxing. Returns ('self', None) for
        ``lambda x: x``, ('attr', (parts,...)) for ``lambda x: x.a.b``,
        ('index', N) for ``lambda x: x[N]`` (int literal),
        ('strmethod', name) for ``lambda s: s.lower()`` when ``elem_ty`` is str,
        ('tuple', (subspec,...)) for ``lambda x: (x.a, x.b)`` (multi-key sort),
        or None (caller falls through to the libpython path). ``elem_ty`` is the
        iterable's element type (enables the str-method shape only on str)."""
        if not isinstance(key_lambda, Lambda):
            return None
        if len(key_lambda.params) != 1 or key_lambda.params[0].name == "":
            return None
        param = key_lambda.params[0].name
        body = key_lambda.body
        # Tuple key: (x.a, x.b) -> compare element keys lexicographically (the
        # runtime py_obj_lt already orders tuples). Each component must itself
        # be a simple attr/index subspec (no str-method components).
        if isinstance(body, TupleExpr):
            if not body.elems:
                return None
            subspecs = []
            for el in body.elems:
                sub = self._simple_key_subspec(el, param)
                if sub is None:
                    return None
                subspecs.append(sub)
            return ("tuple", tuple(subspecs))
        return self._simple_key_subspec(body, param, elem_ty)

    def _key_spec_from_callable(self, key_expr, elem_ty=None):
        """Return an inline key spec for key callables supported natively.

        This is deliberately a small whitelist. Lambdas are reduced to simple
        structural specs; builtin ``len`` maps to a runtime length key. Anything
        else stays a first-class callable gap and must fall through.
        """
        if isinstance(key_expr, Lambda):
            return self._sorted_key_spec_from_lambda(key_expr, elem_ty)
        if isinstance(key_expr, Name) and key_expr.ident == "len":
            return ("len", None)
        return None

    def _simple_key_subspec(self, expr, param, elem_ty=None):
        """('self', None) for ``param`` / ('attr', parts) for ``param.a.b`` /
        ('index', N) for ``param[N]`` (int literal) / ('sum', subspec) for
        ``sum(param[N])`` / ('strmethod', name) for a no-arg str method
        ``param.lower()`` when ``elem_ty`` is str / ('neg', subspec) for
        ``-param`` / ``-param.a`` / ``-param[N]``
        (descending key), else None. The scalar key shapes that compose a tuple
        key or stand alone."""
        # Unary negation of a self/attr/index key -> descending sort by that
        # key (e.g. key=lambda v: -v, key=lambda kv: -kv[1], or as a tuple
        # component (-kv[1], kv[0])). Emitted as generic 0 - key (py_obj_sub),
        # correct for int+float.
        if isinstance(expr, UnaryOp) and expr.op == "-":
            inner = self._simple_key_subspec(expr.operand, param, elem_ty)
            if inner is not None and inner[0] in ("self", "attr", "index"):
                return ("neg", inner)
            return None
        if isinstance(expr, Name) and expr.ident == param:
            return ("self", None)
        parts: list[str] = []
        cur = expr
        while isinstance(cur, Attr):
            parts.append(cur.name)
            cur = cur.obj
        if isinstance(cur, Name) and cur.ident == param and parts:
            parts.reverse()
            return ("attr", tuple(parts))
        if (
            isinstance(expr, Subscript)
            and isinstance(expr.obj, Name)
            and expr.obj.ident == param
            and isinstance(expr.idx, IntLit)
        ):
            return ("index", expr.idx.value)
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and expr.func.ident == "sum"
            and len(expr.args) == 1
            and not expr.kwargs
        ):
            inner = self._simple_key_subspec(expr.args[0], param, elem_ty)
            if inner is not None and inner[0] in ("self", "attr", "index"):
                return ("sum", inner)
            return None
        # No-arg str method on a str element: lambda s: s.lower() etc. Gated on
        # str elements so py_str_<m> is safe; non-str / unknown -> None (the
        # caller falls back to libpython, which handles it correctly).
        if (
            isinstance(elem_ty, StrType)
            and isinstance(expr, Call)
            and not expr.args
            and not expr.kwargs
            and isinstance(expr.func, Attr)
            and isinstance(expr.func.obj, Name)
            and expr.func.obj.ident == param
            and expr.func.name in _NOARG_STR_KEY_METHODS
        ):
            return ("strmethod", expr.func.name)
        return None

    def _emit_key_of(self, obj, key_spec):
        """Inline the simple key extraction described by ``key_spec`` against
        element object ``obj`` (PyObject*), returning the key PyObject*."""
        kind, data = key_spec
        if kind == "self":
            return obj
        if kind == "callable":
            args = self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, 1)],
                name=self._fresh("sortkey.call.args"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [args, ir.Constant(_I64, 0), obj],
            )
            result = self.builder.call(
                self.runtime["py_obj_call"],
                [data, args, self._emit_none_literal()],
                name=self._fresh("sortkey.call"),
            )
            self._gc_release(args)
            self._emit_post_call_err_check()
            return result
        if kind == "tuple":
            # Build a tuple of the component keys; py_obj_lt orders tuples
            # lexicographically. Mirrors the tuple-literal lowering (no extra
            # incref before py_tuple_set_item).
            tup = self.builder.call(
                self.runtime["py_tuple_new"],
                [ir.Constant(_I64, len(data))],
                name=self._fresh("sortkey.tuple"),
            )
            for i, subspec in enumerate(data):
                sub = self._emit_key_of(obj, subspec)
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [tup, ir.Constant(_I64, i), sub],
                )
            return tup
        if kind == "attr":
            cur = obj
            for part in data:
                cur = self.builder.call(
                    self.runtime["py_obj_getattr"],
                    [cur, self._attr_name_ptr(part)],
                    name=self._fresh("sortkey.attr"),
                )
            return cur
        if kind == "strmethod":
            # No-arg str method (gated to str elements at spec time):
            # py_str_<m>(obj) returns the transformed str key.
            return self.builder.call(
                self.runtime["py_str_" + data],
                [obj],
                name=self._fresh("sortkey.strmethod"),
            )
        if kind == "len":
            length = self.builder.call(
                self.runtime["py_obj_len"],
                [obj],
                name=self._fresh("sortkey.len.raw"),
            )
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [length],
                name=self._fresh("sortkey.len"),
            )
        if kind == "sum":
            src = self._emit_key_of(obj, data)
            return self._emit_sum_via_iter(src, None, None)
        if kind == "neg":
            # Descending key: 0 - subkey via the generic py_obj_sub (handles
            # int and float keys). ``data`` is the wrapped attr/index subspec.
            sub = self._emit_key_of(obj, data)
            zero = self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("sortkey.neg.zero"),
            )
            return self.builder.call(
                self.runtime["py_obj_sub"],
                [zero, sub],
                name=self._fresh("sortkey.neg"),
            )
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, data)],
            name=self._fresh("sortkey.idx.box"),
        )
        return self.builder.call(
            self.runtime["py_obj_getitem"],
            [obj, idx_box],
            name=self._fresh("sortkey.item"),
        )

    def _emit_sorted_with_key_lambda(self, expr, key_lambda, reverse_const):
        """Build the fresh list required by ``sorted(xs, key=callable)``.

        Structural lambda/builtin keys retain their inline extraction path.
        Other native callable values use the same ``py_obj_call`` key path as
        ``list.sort(key=callable)`` instead of falling through to libpython.
        """
        elem_ty = (
            expr.args[0].ty.elem if isinstance(expr.args[0].ty, ListType) else None
        )
        key_spec = self._key_spec_from_callable(key_lambda, elem_ty)
        if key_spec is None:
            key_spec = ("callable", self._emit_as_object(key_lambda))
        src_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            self._emit_expr(expr.args[0]),
            expr.args[0].ty,
        )
        new_list = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("sorted.key.copy"),
        )
        self.builder.call(self.runtime["py_list_extend"], [new_list, src_obj])
        self._emit_list_sort_by_key(new_list, key_spec)
        if reverse_const:
            self.builder.call(self.runtime["py_list_reverse"], [new_list])
        return new_list

    def _emit_list_sort_by_key(self, recv, key_spec):
        """Sort ``recv`` by ``key_spec`` in ``O(n log n)`` comparisons.

        Emits a Schwartzian transform: build ``(key(elem), index, elem)``
        tuples, hand them to ``py_obj_sorted`` — the runtime's bottom-up stable
        merge sort — and write the third field back. Tuple comparison is
        lexicographic, so ordering is by key with the original index breaking
        ties, which is exactly the stable order the insertion-sort path below
        produced.

        This replaces that insertion sort on the hot path.  Insertion sort costs
        ``n**2 / 2`` comparisons and evaluates the key TWICE per comparison, so
        a 354-element sort ran ~124000 key calls; stack-map planning does 12186
        such sorts for one oversized module, i.e. ~1.5 billion key calls.  The
        transform needs ``n`` key calls plus ~``n log n`` tuple comparisons.
        Measured under pcc1 on that shape: **13787 ms -> 260 ms**, identical
        output.  (The runtime's keyless `py_obj_sorted` had the same fix applied
        for the same reason; see its comment about insertion sort dominating
        codegen-worker profiles.)
        """
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_list_len"], [recv], name=self._fresh("sortkey.n")
        )
        pairs = self.builder.call(
            self.runtime["py_list_new"], [n_val], name=self._fresh("sortkey.pairs")
        )
        # ---- build pass: pairs[i] = (key(recv[i]), i, recv[i])
        bi_slot = self._alloca_in_entry(_I64, name="sortkey.bi.addr")
        self.builder.store(ir.Constant(_I64, 0), bi_slot)
        bcond = fn.append_basic_block(name=self._fresh("sortkey.bcond"))
        bbody = fn.append_basic_block(name=self._fresh("sortkey.bbody"))
        bdone = fn.append_basic_block(name=self._fresh("sortkey.bdone"))
        self.builder.branch(bcond)
        self.builder.position_at_end(bcond)
        bi = self.builder.load(bi_slot, name=self._fresh("sortkey.bi"))
        self.builder.cbranch(
            self.builder.icmp_signed("<", bi, n_val, name=self._fresh("sortkey.bcmp")),
            bbody,
            bdone,
        )
        self.builder.position_at_end(bbody)
        bi_cur = self.builder.load(bi_slot, name=self._fresh("sortkey.bi.cur"))
        elem = self.builder.call(
            self.runtime["py_list_get"],
            [recv, bi_cur],
            name=self._fresh("sortkey.elem"),
        )
        key_val = self._emit_key_of(elem, key_spec)
        idx_obj = self.builder.call(
            self.runtime["py_int_from_i64"],
            [bi_cur],
            name=self._fresh("sortkey.idx"),
        )
        triple = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 3)],
            name=self._fresh("sortkey.triple"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [triple, ir.Constant(_I64, 0), key_val],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [triple, ir.Constant(_I64, 1), idx_obj],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [triple, ir.Constant(_I64, 2), elem],
        )
        # append, not set: py_list_new(n) makes a length-0 list with capacity n,
        # so an indexed store would be out of range and silently drop every
        # element.  py_list_append and py_tuple_set_item both take their own
        # reference, so the four locals built here are all released.
        self.builder.call(self.runtime["py_list_append"], [pairs, triple])
        self._gc_release(triple)
        self._gc_release(idx_obj)
        self._gc_release(key_val)
        self._gc_release(elem)
        self.builder.store(
            self.builder.add(bi_cur, ir.Constant(_I64, 1), name=self._fresh("sortkey.bnext")),
            bi_slot,
        )
        self.builder.branch(bcond)
        self.builder.position_at_end(bdone)
        ordered = self.builder.call(
            self.runtime["py_obj_sorted"], [pairs], name=self._fresh("sortkey.ordered")
        )
        # ---- write-back pass: recv[i] = ordered[i][2]
        wi_slot = self._alloca_in_entry(_I64, name="sortkey.wi.addr")
        self.builder.store(ir.Constant(_I64, 0), wi_slot)
        wcond = fn.append_basic_block(name=self._fresh("sortkey.wcond"))
        wbody = fn.append_basic_block(name=self._fresh("sortkey.wbody"))
        wdone = fn.append_basic_block(name=self._fresh("sortkey.wdone"))
        self.builder.branch(wcond)
        self.builder.position_at_end(wcond)
        wi = self.builder.load(wi_slot, name=self._fresh("sortkey.wi"))
        self.builder.cbranch(
            self.builder.icmp_signed("<", wi, n_val, name=self._fresh("sortkey.wcmp")),
            wbody,
            wdone,
        )
        self.builder.position_at_end(wbody)
        wi_cur = self.builder.load(wi_slot, name=self._fresh("sortkey.wi.cur"))
        got = self.builder.call(
            self.runtime["py_list_get"],
            [ordered, wi_cur],
            name=self._fresh("sortkey.got"),
        )
        elem_back = self.builder.call(
            self.runtime["py_tuple_get_known"],
            [got, ir.Constant(_I64, 2)],
            name=self._fresh("sortkey.back"),
        )
        self.builder.call(self.runtime["py_list_set"], [recv, wi_cur, elem_back])
        self._gc_release(elem_back)
        self._gc_release(got)
        self.builder.store(
            self.builder.add(wi_cur, ir.Constant(_I64, 1), name=self._fresh("sortkey.wnext")),
            wi_slot,
        )
        self.builder.branch(wcond)
        self.builder.position_at_end(wdone)
        self._gc_release(ordered)
        self._gc_release(pairs)

    def _emit_list_insertion_sort_by_key(self, recv, key_spec):
        """In-place insertion sort of list ``recv`` ordering by
        ``py_obj_lt(key(elem), key(prev))``. Mirrors
        _emit_list_sort_with_dunder_lt's CFG, swapping the per-pair compare.

        Superseded on the hot path by :meth:`_emit_list_sort_by_key`; kept
        because it needs no temporaries and remains the reference for the
        stable ordering that method must reproduce."""
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_list_len"], [recv], name=self._fresh("sortkey.len")
        )
        i_slot = self._alloca_in_entry(_I64, name="sortkey.i.addr")
        j_slot = self._alloca_in_entry(_I64, name="sortkey.j.addr")
        cur_slot = self._alloca_in_entry(_CSTR, name="sortkey.cur.addr")
        self.builder.store(ir.Constant(_I64, 1), i_slot)
        outer_cond = fn.append_basic_block(name=self._fresh("sortkey.ocond"))
        outer_body = fn.append_basic_block(name=self._fresh("sortkey.obody"))
        inner_cond = fn.append_basic_block(name=self._fresh("sortkey.icond"))
        inner_cmp = fn.append_basic_block(name=self._fresh("sortkey.icmp"))
        inner_shift = fn.append_basic_block(name=self._fresh("sortkey.ishift"))
        inner_done = fn.append_basic_block(name=self._fresh("sortkey.iplace"))
        outer_step = fn.append_basic_block(name=self._fresh("sortkey.ostep"))
        done_bb = fn.append_basic_block(name=self._fresh("sortkey.odone"))

        self.builder.branch(outer_cond)
        self.builder.position_at_end(outer_cond)
        i_val = self.builder.load(i_slot, name=self._fresh("sortkey.i"))
        outer_ok = self.builder.icmp_signed(
            "<", i_val, n_val, name=self._fresh("sortkey.outer_ok")
        )
        self.builder.cbranch(outer_ok, outer_body, done_bb)

        self.builder.position_at_end(outer_body)
        cur = self.builder.call(
            self.runtime["py_list_get"], [recv, i_val], name=self._fresh("sortkey.cur")
        )
        self.builder.store(cur, cur_slot)
        self.builder.store(i_val, j_slot)
        self.builder.branch(inner_cond)

        self.builder.position_at_end(inner_cond)
        j_val = self.builder.load(j_slot, name=self._fresh("sortkey.j"))
        j_gt_zero = self.builder.icmp_signed(
            ">", j_val, ir.Constant(_I64, 0), name=self._fresh("sortkey.j_gt_zero")
        )
        self.builder.cbranch(j_gt_zero, inner_cmp, inner_done)

        self.builder.position_at_end(inner_cmp)
        j_prev = self.builder.sub(
            j_val, ir.Constant(_I64, 1), name=self._fresh("sortkey.j_prev")
        )
        prev = self.builder.call(
            self.runtime["py_list_get"],
            [recv, j_prev],
            name=self._fresh("sortkey.prev"),
        )
        cur = self.builder.load(cur_slot, name=self._fresh("sortkey.cur2"))
        key_cur = self._emit_key_of(cur, key_spec)
        key_prev = self._emit_key_of(prev, key_spec)
        less_i64 = self.builder.call(
            self.runtime["py_obj_lt"],
            [key_cur, key_prev],
            name=self._fresh("sortkey.cmp.lt"),
        )
        less = self.builder.icmp_signed(
            "!=",
            less_i64,
            ir.Constant(less_i64.type, 0),
            name=self._fresh("sortkey.cmp.i1"),
        )
        self.builder.cbranch(less, inner_shift, inner_done)

        self.builder.position_at_end(inner_shift)
        self.builder.call(self.runtime["py_list_set"], [recv, j_val, prev])
        self.builder.store(j_prev, j_slot)
        self.builder.branch(inner_cond)

        self.builder.position_at_end(inner_done)
        j_final = self.builder.load(j_slot, name=self._fresh("sortkey.j_final"))
        cur_final = self.builder.load(cur_slot, name=self._fresh("sortkey.cur3"))
        self.builder.call(self.runtime["py_list_set"], [recv, j_final, cur_final])
        self.builder.branch(outer_step)

        self.builder.position_at_end(outer_step)
        i_next = self.builder.add(
            self.builder.load(i_slot, name=self._fresh("sortkey.i2")),
            ir.Constant(_I64, 1),
            name=self._fresh("sortkey.i_next"),
        )
        self.builder.store(i_next, i_slot)
        self.builder.branch(outer_cond)

        self.builder.position_at_end(done_bb)
