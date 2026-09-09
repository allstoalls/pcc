"""Tuple and zip builtin lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    ClassType,
    DictType,
    DynType,
    ListExpr,
    ListType,
    TupleExpr,
    TupleType,
)
from .bootstrap_trace import bootstrap_trace_enabled
from . import marshal

_I64 = ir.IntType(64)


def _type_name(ty: object) -> str:
    try:
        name = ty.name
    except AttributeError:
        return ""
    return str(name)


def _expr_kind(expr: object) -> str:
    return type(expr).__name__


def _expr_elems(expr: object):
    try:
        return expr.elems
    except AttributeError:
        return None


def _is_literal_tuple_or_list(expr: object) -> bool:
    if isinstance(expr, (ListExpr, TupleExpr)):
        return True
    kind = _expr_kind(expr)
    if kind not in ("ListExpr", "TupleExpr"):
        return False
    return _expr_elems(expr) is not None


def _dict_key_type(ty: object):
    try:
        return ty.key
    except AttributeError:
        return DynType(name="dyn")


class TupleZipLoweringMixin:
    def _maybe_emit_tuple_method(self, expr: Call) -> Optional[ir.Value]:
        """``t.count(x)`` / ``t.index(x[, start[, stop]])`` for a tuple, via the
        runtime py_tuple_count / py_tuple_index[_range] helpers (all return
        i64). index() raises ValueError when absent (or absent within the
        [start, stop) window). Other methods fall back."""
        attr = expr.func
        if not isinstance(attr, Attr):
            return None
        if expr.kwargs:
            return None
        name = attr.name
        if name not in ("count", "index"):
            return None
        n_args = len(expr.args)
        if name == "count":
            if n_args != 1:
                return None
            recv = self._emit_as_object(attr.obj)
            item = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_tuple_count"],
                [recv, item],
                name=self._fresh("tuple.count"),
            )
        # name == "index": accept the 1-arg form and the 2/3-arg
        # index(x, start[, stop]) form.
        if n_args not in (1, 2, 3):
            return None
        recv = self._emit_as_object(attr.obj)
        item = self._emit_as_object(expr.args[0])
        if n_args == 1:
            res = self.builder.call(
                self.runtime["py_tuple_index"],
                [recv, item],
                name=self._fresh("tuple.index"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return res
        # 2/3-arg range form. start is required here; stop is a NULL
        # PyObject* when omitted, which the runtime treats as "to the end".
        null_obj = ir.Constant(ir.IntType(8).as_pointer(), None)
        start = self._emit_as_object(expr.args[1])
        stop = self._emit_as_object(expr.args[2]) if n_args == 3 else null_obj
        res = self.builder.call(
            self.runtime["py_tuple_index_range"],
            [recv, item, start, stop],
            name=self._fresh("tuple.index.range"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        return res

    def _maybe_emit_tuple_builtin(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        """``tuple()`` / ``tuple([a, b])`` — small subset that matches
        pcc's own usage. Literal lists/tuples fold inline into a new
        ``py_tuple_new`` + per-element ``py_tuple_set_item``. Other
        iterable shapes return None (caller surfaces the original
        unknown-builtin error)."""
        if not expr.args:
            n_val = ir.Constant(_I64, 0)
            return self.builder.call(
                self.runtime["py_tuple_new"],
                [n_val],
                name=self._fresh("tuple.new"),
            )
        arg = expr.args[0]
        if isinstance(arg, Call):
            tmp_list = self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("tuple.map.list"),
            )
            mapped = self._maybe_emit_list_from_map_filter(arg, tmp_list)
            if mapped is not None:
                result = self.builder.call(
                    self.runtime["py_tuple_from_splat"],
                    [mapped],
                    name=self._fresh("tuple.from.map"),
                )
                self._gc_release(mapped)
                return result
            self._gc_release(tmp_list)
        elems = _expr_elems(arg)
        if bootstrap_trace_enabled(self.module.name):
            import os
            import sys

            if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE"):
                try:
                    arg_type = type(arg).__name__
                except AttributeError:
                    arg_type = ""
                try:
                    arg_ty = arg.ty
                    arg_ty_type = type(arg_ty).__name__
                except AttributeError:
                    arg_ty = None
                    arg_ty_type = "<missing>"
                sys.stderr.write(
                    "debug: tuple_builtin arg_type="
                    + str(arg_type)
                    + " arg_ty_type="
                    + str(arg_ty_type)
                    + " arg_ty_name="
                    + str(_type_name(arg_ty))
                    + " elems="
                    + str(elems is not None)
                    + "\n"
                )
        if _is_literal_tuple_or_list(arg) and elems is not None:
            n = len(elems)
            n_val = ir.Constant(_I64, n)
            tup = self.builder.call(
                self.runtime["py_tuple_new"],
                [n_val],
                name=self._fresh("tuple.new"),
            )
            for i, el in enumerate(elems):
                v_obj = self._emit_expr_as_pcc_object(el)
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [tup, ir.Constant(_I64, i), v_obj],
                )
            return tup
        # The runtime preserves list/tuple fast paths and consumes other
        # supported objects through their iterator protocol.
        # DictType iterates keys (matching ``tuple(dict)`` semantics);
        # we materialise the keys as a list first then build the tuple
        # over that list.
        arg_ty = arg.ty
        if isinstance(arg_ty, DictType) or _type_name(arg_ty) == "dict":
            src_val = self._emit_expr(arg)
            src_val = self.builder.call(
                self.runtime["py_dict_keys"],
                [src_val],
                name=self._fresh("tuple.dict.keys"),
            )
            arg_ty = ListType(name="list", elem=_dict_key_type(arg_ty))
            n_val = self.builder.call(
                self.runtime["py_list_len"],
                [src_val],
                name=self._fresh("tuple.dict.keys.len"),
            )
            tup = self.builder.call(
                self.runtime["py_tuple_new"],
                [n_val],
                name=self._fresh("tuple.new"),
            )
            fn = self.current_function
            idx_slot = self._alloca_in_entry(_I64, name="tuple.dk.idx")
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            cond_bb = fn.append_basic_block(name=self._fresh("tuple.dk.cond"))
            body_bb = fn.append_basic_block(name=self._fresh("tuple.dk.body"))
            step_bb = fn.append_basic_block(name=self._fresh("tuple.dk.step"))
            end_bb = fn.append_basic_block(name=self._fresh("tuple.dk.end"))
            self.builder.branch(cond_bb)
            self.builder.position_at_end(cond_bb)
            cur = self.builder.load(idx_slot, name=self._fresh("tuple.dk.i"))
            cond = self.builder.icmp_signed(
                "<",
                cur,
                n_val,
                name=self._fresh("tuple.dk.i1"),
            )
            self.builder.cbranch(cond, body_bb, end_bb)
            self.builder.position_at_end(body_bb)
            elem = self.builder.call(
                self.runtime["py_list_get"],
                [src_val, cur],
                name=self._fresh("tuple.dk.elem"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [tup, cur, elem],
            )
            self.builder.branch(step_bb)
            self.builder.position_at_end(step_bb)
            nxt = self.builder.add(
                cur,
                ir.Constant(_I64, 1),
                name=self._fresh("tuple.dk.inc"),
            )
            self.builder.store(nxt, idx_slot)
            self.builder.branch(cond_bb)
            self.builder.position_at_end(end_bb)
            return tup
        if isinstance(arg_ty, (ListType, TupleType, DynType, ClassType)) or _type_name(arg_ty) in (
            "list",
            "tuple",
            "tuple_variadic",
            "dyn",
        ):
            src_val = self._emit_expr(arg)
            src_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                src_val,
                arg_ty,
            )
            # The runtime owns iteration, rooting and failure cleanup for
            # both generators and custom iterators. Keep caller temporaries
            # alive across the call, and propagate exceptions before use.
            self._gc_pin(src_obj)
            result = self.builder.call(
                self.runtime["py_tuple_from_splat"],
                [src_obj],
                name=self._fresh("tuple.from.splat"),
            )
            self._gc_pin(result)
            self._gc_unpin(src_obj)
            self._gc_release_if_owned(src_obj, arg)
            self._gc_unpin(result)
            self._emit_post_call_err_check(getattr(expr, "span", None))
            return result
        return None

    def _maybe_emit_zip_builtin(self, expr: Call) -> Optional[ir.Value]:
        """``zip(a, b, ...)`` materialised as a pcc-native list of tuples.

        The Python frontend already normalises ``for ... in zip(...)`` to
        indexed loops. This builtin path covers value-position uses such
        as ``list(zip(xs, ys))`` and dict construction helpers in the
        bootstrap pipeline without pulling in CPython's iterator object.
        """
        if not expr.args:
            return None
        strict_expr = None
        for kwarg_name, kwarg_value in expr.kwargs:
            if kwarg_name != "strict":
                return None
            strict_expr = kwarg_value
        # ``strict`` is enforced below (ValueError on unequal lengths, as in
        # CPython).  The for-loop rewrite
        # (``for_normalization_lowering._for_iter_is_zip``) declines any
        # kwarg'd zip and routes it here, so this is the single owner of the
        # strict semantics.
        if self._is_starred_unpack(expr.args):
            # zip(*rows): runtime-variadic transpose. The static path below
            # fixes the tuple width at len(expr.args); a *splat needs the
            # runtime number of rows, so route to the py_zip_star helper.
            rows_expr = expr.args[0].args[0]
            rows = self._emit_value_as_pcc_object_or_bridge(
                self._emit_expr(rows_expr),
                rows_expr.ty,
                "zip.star.rows",
            )
            return self.builder.call(
                self.runtime["py_zip_star"],
                [rows],
                name=self._fresh("zip.star"),
            )
        src_objs: list[ir.Value] = []
        lengths: list[ir.Value] = []
        # Fail-closed edges are emitted for DynType sources only; statically
        # known sources cannot raise from len/getitem, and the cost guard in
        # tests/python/test_native_container_builtin_error_paths.py pins
        # that static shapes gain no checks.
        src_can_raise: list[bool] = []
        # ``py_dict_keys`` returns a NEW list ref (py_runtime.h).  Every one
        # materialised below is owned by this expression and must be released
        # at the exit, or ``zip(mapping, ...)`` leaks one list per call.
        owned_dict_keys: list[ir.Value] = []
        for i, arg in enumerate(expr.args):
            raw = self._emit_expr(arg)
            obj = self._emit_value_as_pcc_object_or_bridge(
                raw,
                arg.ty,
                f"zip.arg.{i}.bridge",
            )
            src_can_raise.append(
                isinstance(arg.ty, DynType) or _type_name(arg.ty) == "dyn"
            )
            if isinstance(arg.ty, DictType) or _type_name(arg.ty) == "dict":
                # The loop below walks each source positionally with
                # py_obj_getitem, and for a dict that is a KEY lookup for
                # 0, 1, 2 …  Iterating a mapping yields its keys, so
                # materialise them first — the same normalisation
                # ``tuple(dict)`` above already does.  Without it
                # ``zip(d, ...)`` raised KeyError on a string-keyed dict.
                obj = self.builder.call(
                    self.runtime["py_dict_keys"],
                    [obj],
                    name=self._fresh(f"zip.arg.{i}.dict.keys"),
                )
                owned_dict_keys.append(obj)
            src_objs.append(obj)
            lengths.append(
                self.builder.call(
                    self.runtime["py_obj_len"],
                    [obj],
                    name=self._fresh(f"zip.len.{i}"),
                )
            )
            if src_can_raise[-1]:
                # A dyn source's user __len__ can raise; the keys views
                # acquired so far are live owned references on that edge.
                self._emit_post_call_err_check(
                    expr.span,
                    release_on_error=tuple(owned_dict_keys),
                )
                # py_obj_len returned 0 silently for a dyn-held scalar, so
                # zip(t, 5) answered [] where CPython raises TypeError.
                self._emit_non_iterable_scalar_guard(
                    obj,
                    "zip",
                    release_first=tuple(owned_dict_keys),
                )
        fn = self.current_function
        if strict_expr is not None and len(lengths) > 1:
            strict_i1 = self._emit_condition_value(strict_expr)
            mismatch = ir.Constant(ir.IntType(1), 0)
            for i, n_val in enumerate(lengths[1:], start=1):
                ne_i = self.builder.icmp_signed(
                    "!=",
                    n_val,
                    lengths[0],
                    name=self._fresh(f"zip.strict.ne.{i}"),
                )
                mismatch = self.builder.or_(
                    mismatch,
                    ne_i,
                    name=self._fresh(f"zip.strict.any.{i}"),
                )
            violated = self.builder.and_(
                strict_i1,
                mismatch,
                name=self._fresh("zip.strict.violated"),
            )
            bad_bb = fn.append_basic_block(
                name=self._fresh("zip.strict.bad")
            )
            ok_bb = fn.append_basic_block(name=self._fresh("zip.strict.ok"))
            self.builder.cbranch(violated, bad_bb, ok_bb)
            self.builder.position_at_end(bad_bb)
            for keys in owned_dict_keys:
                self._gc_release(
                    keys, self._release_context_label("zip.strict.bad")
                )
            message = self._ptr_to_cstr(
                self._cstr_global(
                    "zip() arguments have different lengths (strict=True)",
                    ".zip.strict.valueerror",
                )
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 2), message],
                name=self._fresh("zip.strict.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            err_target = (
                self._current_try_err_block() or self._ensure_fn_err_exit()
            )
            self.builder.branch(err_target)
            self.builder.position_at_end(ok_bb)
        min_len = lengths[0]
        for i, n_val in enumerate(lengths[1:], start=1):
            take_n = self.builder.icmp_signed(
                "<",
                n_val,
                min_len,
                name=self._fresh(f"zip.min.cmp.{i}"),
            )
            min_len = self.builder.select(
                take_n,
                n_val,
                min_len,
                name=self._fresh(f"zip.min.{i}"),
            )
        result = self.builder.call(
            self.runtime["py_list_new"],
            [min_len],
            name=self._fresh("zip.list"),
        )
        fn = self.current_function
        idx_slot = self._alloca_in_entry(_I64, name="zip.idx.addr")
        self.builder.store(ir.Constant(_I64, 0), idx_slot)
        cond_bb = fn.append_basic_block(name=self._fresh("zip.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("zip.body"))
        step_bb = fn.append_basic_block(name=self._fresh("zip.step"))
        end_bb = fn.append_basic_block(name=self._fresh("zip.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh("zip.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            min_len,
            name=self._fresh("zip.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh("zip.idx.box"),
        )
        item = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(src_objs))],
            name=self._fresh("zip.item"),
        )
        for i, src_obj in enumerate(src_objs):
            elem = self.builder.call(
                self.runtime["py_obj_getitem"],
                [src_obj, idx_box],
                name=self._fresh(f"zip.elem.{i}"),
            )
            if src_can_raise[i]:
                # A dyn source's getitem can raise (user __getitem__);
                # without this check the walk kept looping, stored NULL
                # into the result tuples, and returned a silently corrupt
                # list.  elem is the raising call's own NULL return
                # (pcc_gc_release is NULL-safe); the partial tuple, index
                # box, result list under construction, and every keys view
                # are live owned references on this edge.
                self._emit_post_call_err_check(
                    expr.span,
                    release_on_error=(
                        elem,
                        item,
                        idx_box,
                        result,
                        *owned_dict_keys,
                    ),
                )
                # py_obj_getitem also returns NULL WITHOUT raising for a
                # missing dict key (py_dict_get is the silent variant) and
                # for unsupported tags; fail closed instead of building a
                # NULL-slotted tuple.
                elem_null = self.builder.icmp_unsigned(
                    "==",
                    elem,
                    ir.Constant(elem.type, None),
                    name=self._fresh(f"zip.elem.null.{i}"),
                )
                badelem_bb = fn.append_basic_block(
                    name=self._fresh(f"zip.elem.bad.{i}")
                )
                elemok_bb = fn.append_basic_block(
                    name=self._fresh(f"zip.elem.ok.{i}")
                )
                self.builder.cbranch(elem_null, badelem_bb, elemok_bb)
                self.builder.position_at_end(badelem_bb)
                for owned in (item, idx_box, result, *owned_dict_keys):
                    self._gc_release(
                        owned, self._release_context_label("zip.elem.bad")
                    )
                message = self._ptr_to_cstr(
                    self._cstr_global(
                        "zip() argument is not indexable from 0..len-1"
                        " (mappings iterate by key in CPython; unsupported"
                        " here for dyn sources)",
                        ".zip.dyn.typeerror",
                    )
                )
                exc = self.builder.call(
                    self.runtime["py_exc_new"],
                    [ir.Constant(_I64, 3), message],
                    name=self._fresh("zip.dyn.exc"),
                )
                self.builder.call(self.runtime["py_raise"], [exc])
                err_target = (
                    self._current_try_err_block()
                    or self._ensure_fn_err_exit()
                )
                self.builder.branch(err_target)
                self.builder.position_at_end(elemok_bb)
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [item, ir.Constant(_I64, i), elem],
            )
            # py_obj_getitem returns a NEW ref and py_tuple_set_item RETAINS
            # what it stores (py_incref on the GC0 path, the balanced
            # pcc_gc_store_ptr otherwise), so this reference is ours to drop.
            self._gc_release(
                elem, self._release_context_label(f"zip.elem.{i}")
            )
        self.builder.call(self.runtime["py_list_append"], [result, item])
        # py_list_append retains as well, so the freshly built tuple is ours.
        self._gc_release(item, self._release_context_label("zip.item"))
        # py_int_from_i64 also returns a NEW ref; py_obj_getitem only reads
        # the index, it does not take it.
        self._gc_release(idx_box, self._release_context_label("zip.idx.box"))
        self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh("zip.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        for keys in owned_dict_keys:
            self._gc_release(keys, self._release_context_label("zip.dict.keys"))
        return result
