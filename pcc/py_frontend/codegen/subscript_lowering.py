"""Subscript and slice lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    DictType,
    DynType,
    Expr,
    IntLit,
    IntType,
    ListExpr,
    ListType,
    MemoryViewType,
    Name,
    Slice,
    StrLit,
    StrType,
    Subscript,
    TupleExpr,
    TupleType,
    Type,
    ValueArrayType,
)
from . import marshal
from .runtime_abi import declare_runtime_global

_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


def _same_type_kind(a: Type, b: Type) -> bool:
    return type(a) is type(b)


class SubscriptLoweringMixin:
    def _emit_value_array_subscript_load(self, expr: Subscript) -> ir.Value:
        array_ty = expr.obj.ty
        payload = self._emit_expr(expr.obj)
        index_value = self._emit_expr(expr.idx)

        if isinstance(index_value.type, ir.PointerType):
            if index_value in getattr(self, "_cpy_values", ()):
                index_i64 = self.builder.call(
                    self.runtime["py_cpy_to_i64"],
                    [index_value],
                    name=self._fresh("value.array.cpy.index"),
                )
                self._emit_post_call_err_check(expr.span)
                self._gc_release_if_owned(index_value, expr.idx)
            else:
                overflow_slot = self._alloca_in_entry(
                    ir.IntType(32),
                    name=self._fresh("value.array.index.overflow"),
                )
                self.builder.store(ir.Constant(ir.IntType(32), 0), overflow_slot)
                index_i64 = self.builder.call(
                    self.runtime["py_int_to_i64"],
                    [index_value, overflow_slot],
                    name=self._fresh("value.array.index"),
                )
                overflow = self.builder.load(
                    overflow_slot,
                    name=self._fresh("value.array.index.overflow.flag"),
                )
                overflowed = self.builder.icmp_signed(
                    "!=",
                    overflow,
                    ir.Constant(ir.IntType(32), 0),
                    name=self._fresh("value.array.index.overflowed"),
                )
                self._gc_release_if_owned(index_value, expr.idx)
                overflow_bb = self.current_function.append_basic_block(
                    name=self._fresh("value.array.index.overflow"),
                )
                converted_bb = self.current_function.append_basic_block(
                    name=self._fresh("value.array.index.converted"),
                )
                self.builder.cbranch(overflowed, overflow_bb, converted_bb)

                self.builder.position_at_end(overflow_bb)
                overflow_exc = self.builder.call(
                    self.runtime["py_exc_new"],
                    [
                        ir.Constant(_I64, 15),
                        self._pooled_cstr_ptr(
                            "pcc.array index does not fit in a signed 64-bit integer",
                            ".value.array.index.overflow.msg",
                        ),
                    ],
                    name=self._fresh("value.array.index.overflow.exc"),
                )
                self.builder.call(self.runtime["py_raise"], [overflow_exc])
                self._gc_release(overflow_exc)
                frame_exc = self.builder.call(
                    self.runtime["py_current_exception"],
                    [],
                    name=self._fresh("value.array.index.overflow.frame"),
                )
                self._emit_exception_frame(frame_exc, expr.span)
                err_target = self._current_try_err_block()
                if err_target is None:
                    err_target = self._ensure_fn_err_exit()
                self.builder.branch(err_target)
                self.builder.position_at_end(converted_bb)
        else:
            index_i64 = self._to_int64(index_value, expr.idx.ty)
            self._gc_release_if_owned(index_value, expr.idx)
        is_negative = self.builder.icmp_signed(
            "<",
            index_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("value.array.index.negative"),
        )
        from_end = self.builder.add(
            index_i64,
            ir.Constant(_I64, array_ty.length),
            name=self._fresh("value.array.index.from_end"),
        )
        normalized = self.builder.select(
            is_negative,
            from_end,
            index_i64,
            name=self._fresh("value.array.index.normalized"),
        )
        at_least_zero = self.builder.icmp_signed(
            ">=",
            normalized,
            ir.Constant(_I64, 0),
            name=self._fresh("value.array.index.nonnegative"),
        )
        below_length = self.builder.icmp_signed(
            "<",
            normalized,
            ir.Constant(_I64, array_ty.length),
            name=self._fresh("value.array.index.below_length"),
        )
        in_bounds = self.builder.and_(
            at_least_zero,
            below_length,
            name=self._fresh("value.array.index.in_bounds"),
        )
        bounds_ok_bb = self.current_function.append_basic_block(
            name=self._fresh("value.array.index.bounds_ok"),
        )
        bounds_error_bb = self.current_function.append_basic_block(
            name=self._fresh("value.array.index.bounds_error"),
        )
        self.builder.cbranch(in_bounds, bounds_ok_bb, bounds_error_bb)

        self.builder.position_at_end(bounds_error_bb)
        bounds_exc = self.builder.call(
            self.runtime["py_exc_new"],
            [
                ir.Constant(_I64, 5),
                self._pooled_cstr_ptr(
                    "pcc.array index out of range",
                    ".value.array.index.bounds.msg",
                ),
            ],
            name=self._fresh("value.array.index.bounds.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [bounds_exc])
        self._gc_release(bounds_exc)
        frame_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("value.array.index.bounds.frame"),
        )
        self._emit_exception_frame(frame_exc, expr.span)
        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(bounds_ok_bb)
        elem_ir_ty = self._valueclass_payload_ir_type(array_ty.elem)
        result_slot = self._alloca_in_entry(
            elem_ir_ty,
            name=self._fresh("value.array.index.result"),
        )
        done_bb = self.current_function.append_basic_block(
            name=self._fresh("value.array.index.done"),
        )
        candidate = 0
        while candidate < array_ty.length:
            match_bb = self.current_function.append_basic_block(
                name=self._fresh(f"value.array.index.match{candidate}"),
            )
            next_bb = self.current_function.append_basic_block(
                name=self._fresh(f"value.array.index.next{candidate}"),
            )
            matches = self.builder.icmp_signed(
                "==",
                normalized,
                ir.Constant(_I64, candidate),
                name=self._fresh(f"value.array.index.is{candidate}"),
            )
            self.builder.cbranch(matches, match_bb, next_bb)

            self.builder.position_at_end(match_bb)
            if candidate == 0:
                selected = self.builder.extract_value(payload, [0])
            elif candidate == 1:
                selected = self.builder.extract_value(payload, [1])
            elif candidate == 2:
                selected = self.builder.extract_value(payload, [2])
            elif candidate == 3:
                selected = self.builder.extract_value(payload, [3])
            elif candidate == 4:
                selected = self.builder.extract_value(payload, [4])
            elif candidate == 5:
                selected = self.builder.extract_value(payload, [5])
            else:
                selected = self.builder.extract_value(payload, [6])
            self.builder.store(selected, result_slot)
            self.builder.branch(done_bb)

            self.builder.position_at_end(next_bb)
            candidate += 1

        fallback = self.builder.extract_value(payload, [0])
        self.builder.store(fallback, result_slot)
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        return self.builder.load(
            result_slot,
            name=self._fresh("value.array.index.value"),
        )

    def _emit_index_expr_as_i64(self, expr: Expr) -> ir.Value:
        value = self._emit_expr(expr)
        if value in getattr(self, "_cpy_values", ()):
            self._guard_cpy_value_not_null(value)
            result = self.builder.call(
                self.runtime["py_cpy_to_i64"],
                [value],
                name=self._fresh("cpy.index.to_i64"),
            )
            if self._cpy_value_is_owned(value):
                self.builder.call(self.runtime["py_cpy_decref"], [value])
                self._forget_owned_cpy_value(value)
            return result
        if isinstance(expr.ty, (IntType, BoolType)):
            return self._to_int64(value, expr.ty)
        obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            value,
            expr.ty,
        )
        idx = self.builder.call(
            self.runtime["py_obj_index_i64"],
            [obj],
            name=self._fresh("index"),
        )
        self._emit_post_call_err_check(expr.span)
        return idx

    def _emit_exact_container_subscript_load_object(
        self,
        expr: Subscript,
        obj: ir.Value,
    ) -> Optional[tuple]:
        """Emit the raw object result for exact list/tuple/dict getitem.

        Both ordinary expression lowering and the exact-int object boundary
        need this behavior.  Keep runtime-symbol selection, raising helpers,
        the post-call exception edge, and receiver ownership in one place;
        callers only decide whether to coerce the returned object.

        Returns ``(raw_object, semantic_element_type_or_none, None, None)``
        or ``None`` when the receiver is outside this finite exact-container
        family.  The raw object is a NEW reference the caller owns.  An owned
        key or receiver temporary is released here, with the result pinned
        across the release so a collector the release triggers cannot move
        it.  (The two trailing slots are the retired result-root protocol:
        store_root, frame_enter_lifo, reload, store_root(null),
        frame_leave_lifo around every subscript, whether or not anything was
        released in between.)
        """
        obj_ty = expr.obj.ty
        span = getattr(expr, "span", None)
        elem_ty: Optional[Type] = None
        key_obj: Optional[ir.Value] = None
        if isinstance(obj_ty, ListType):
            idx = self._emit_index_expr_as_i64(expr.idx)
            got = self.builder.call(
                self.runtime["py_list_getitem"],
                [obj, idx],
                name=self._fresh("subscript.list.getitem"),
            )
            elem_ty = obj_ty.elem
        elif isinstance(obj_ty, TupleType):
            idx = self._emit_index_expr_as_i64(expr.idx)
            got = self.builder.call(
                self.runtime["py_tuple_getitem"],
                [obj, idx],
                name=self._fresh("subscript.tuple.getitem"),
            )
            if obj_ty.elems:
                first = obj_ty.elems[0]
                if all(_same_type_kind(item, first) for item in obj_ty.elems):
                    elem_ty = first
        elif isinstance(obj_ty, DictType):
            key_obj = self._emit_subscript_key_object(expr.idx)
            got = self.builder.call(
                self.runtime["py_dict_getitem"],
                [obj, key_obj],
                name=self._fresh("subscript.dict.getitem"),
            )
            elem_ty = obj_ty.value
        else:
            return None

        # A key produced by another getitem/call is a NEW reference that this
        # site consumes (``d[keys[i]]`` never released it: a key with
        # ``__del__`` was never finalized).
        release_key = key_obj is not None and self._owned_release_needed(
            key_obj, expr.idx
        )
        release_receiver = self._owned_release_needed(obj, expr.obj)
        release_on_error = tuple(
            value
            for value, needed in ((key_obj, release_key), (obj, release_receiver))
            if needed
        )
        # These public getitem variants raise IndexError/KeyError.  Keep the
        # exception edge paired with symbol selection so no caller can regress
        # to a silent NULL result.
        self._emit_post_call_err_check(span, release_on_error=release_on_error)
        if not release_key and not release_receiver:
            # Nothing is released between here and the caller's use of the
            # result, so nothing can run a collector on this thread.
            return got, elem_ty, None, None
        # Pin the NEW result while the operand temporaries are released: a
        # release may run a finalizer or a collection, and a relocating
        # backend must not move an object held only in this register.
        self._gc_pin(got)
        if release_key:
            self._gc_release_if_owned(key_obj, expr.idx)
        self._gc_release_if_owned(obj, expr.obj)
        self._gc_unpin(got)
        return got, elem_ty, None, None

    def _emit_subscript_store(self, target: Subscript, value_expr: Expr) -> None:
        rhs = self._emit_expr_as_pcc_object(value_expr)
        self._emit_subscript_store_value(
            target,
            rhs,
            DynType(name="dyn"),
            release_expr=value_expr,
        )

    def _emit_subscript_store_value(
        self,
        target: Subscript,
        rhs: ir.Value,
        value_ty: Type,
        release_expr: Optional[Expr] = None,
    ) -> None:
        obj = self._emit_expr(target.obj)
        obj_ty = target.obj.ty
        idx_expr = target.idx
        rhs_obj = marshal.marshal_to_object(
            self.builder, self.module, self.runtime, rhs, value_ty
        )

        def release_rhs() -> None:
            if release_expr is not None:
                self._gc_release_if_owned(rhs, release_expr)

        if isinstance(idx_expr, Slice):
            if obj not in getattr(self, "_cpy_values", ()):
                lo_obj = self._emit_slice_bound_object(idx_expr.lo)
                hi_obj = self._emit_slice_bound_object(idx_expr.hi)
                step_obj = self._emit_slice_bound_object(idx_expr.step)
                if isinstance(obj_ty, ListType):
                    self.builder.call(
                        self.runtime["py_list_set_slice"],
                        [obj, lo_obj, hi_obj, step_obj, rhs_obj],
                    )
                    release_rhs()
                    return
                if isinstance(obj_ty, ByteArrayType):
                    self.builder.call(
                        self.runtime["py_bytearray_set_slice"],
                        [obj, lo_obj, hi_obj, step_obj, rhs_obj],
                    )
                    # The store reports a bad replacement type/size through
                    # the pending-exception channel.
                    self._emit_post_call_err_check(target.span)
                    release_rhs()
                    return
                if isinstance(obj_ty, (ClassType, DynType)):
                    self.builder.call(
                        self.runtime["py_obj_set_slice"],
                        [obj, lo_obj, hi_obj, step_obj, rhs_obj],
                    )
                    # Extension mp_ass_subscript/sq_ass_item failures are
                    # reported through the pending-exception channel.  Check
                    # it before continuing so a rejected slice store cannot
                    # leave a partially initialized numerical container in
                    # use by later code.
                    self._emit_post_call_err_check(target.span)
                    release_rhs()
                    return
                raise NotImplementedError(
                    f"Layer 1 slice assignment on type "
                    f"{type(obj_ty).__name__} not supported"
                )

            def _as_cpy_obj(obj_v: ir.Value) -> ir.Value:
                if obj_v in getattr(self, "_cpy_values", ()):
                    return obj_v
                return self.builder.call(
                    self.runtime["py_cpy_from_pcc_obj"],
                    [obj_v],
                    name=self._fresh("cpy.slice.obj"),
                )

            def _slice_bound(e):
                if e is None:
                    gv = declare_runtime_global(self.module, "py_None")
                    none = self.builder.load(gv, name=self._fresh("none"))
                    return self.builder.call(
                        self.runtime["py_cpy_from_pcc_obj"],
                        [none],
                        name=self._fresh("cpy.none"),
                    )
                v = self._emit_expr(e)
                obj_v = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    v,
                    e.ty,
                )
                return _as_cpy_obj(obj_v)

            obj_cpy = _as_cpy_obj(obj)
            rhs_cpy = _as_cpy_obj(rhs_obj)
            slice_fn = self._load_cpython_builtin("slice")
            lo_cpy = _slice_bound(idx_expr.lo)
            hi_cpy = _slice_bound(idx_expr.hi)
            step_cpy = _slice_bound(idx_expr.step)
            slice_obj = self.builder.call(
                self.runtime["py_cpy_call3"],
                [slice_fn, lo_cpy, hi_cpy, step_cpy],
                name=self._fresh("cpy.slice"),
            )
            setitem_gv = self._cstr_global(
                "__setitem__",
                ".cpy.attr.__setitem__",
            )
            setitem_fn = self.builder.call(
                self.runtime["py_cpy_getattr"],
                [obj_cpy, self._ptr_to_cstr(setitem_gv)],
                name=self._fresh("cpy.setitem.fn"),
            )
            self.builder.call(
                self.runtime["py_cpy_call2"],
                [setitem_fn, slice_obj, rhs_cpy],
                name=self._fresh("cpy.setitem"),
            )
            release_rhs()
            return
        if obj in getattr(self, "_cpy_values", ()):
            cpy_key, key_owned = self._marshal_to_cpython(
                self._emit_expr(idx_expr),
                idx_expr.ty,
            )
            cpy_val, val_owned = self._marshal_to_cpython(
                rhs_obj,
                value_ty,
            )
            self.builder.call(
                self.runtime["py_cpy_setitem"],
                [obj, cpy_key, cpy_val],
            )
            if key_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_key])
            if val_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_val])
            release_rhs()
            return
        weak_dict_kind = self._weak_dict_kind_for_expr(target.obj)
        if weak_dict_kind == "value":
            key_obj = self._emit_as_object(idx_expr)
            self.builder.call(
                self.runtime["py_weak_value_dict_set"],
                [obj, key_obj, rhs_obj],
                name=self._fresh("weak.value.dict.set"),
            )
            # the internal py_weakref_new can raise (TypeError on
            # valueclass payloads); without this check the pending
            # exception skips enclosing try/except blocks
            self._emit_post_call_err_check(target.span)
            release_rhs()
            return
        if weak_dict_kind == "key":
            key_obj = self._emit_as_object(idx_expr)
            self.builder.call(
                self.runtime["py_weak_key_dict_set"],
                [obj, key_obj, rhs_obj],
                name=self._fresh("weak.key.dict.set"),
            )
            # see weak-value note above
            self._emit_post_call_err_check(target.span)
            release_rhs()
            return
        if isinstance(obj_ty, ListType):
            idx_i64 = self._emit_index_expr_as_i64(idx_expr)
            # User-visible store contract: out-of-range raises catchable
            # IndexError (py_list_set stays the internal non-raising setter).
            self.builder.call(
                self.runtime["py_list_setitem"], [obj, idx_i64, rhs_obj]
            )
            self._emit_post_call_err_check(target.span)
            release_rhs()
            return
        if isinstance(obj_ty, DictType):
            key_obj = self._emit_subscript_key_object(idx_expr)
            self.builder.call(self.runtime["py_dict_set"], [obj, key_obj, rhs_obj])
            # ``py_dict_set`` has a void ABI and reports rejected keys through
            # the pending-exception channel.  Branch before the next source
            # statement so an unhashable key is catchable instead of leaving
            # a stale TypeError behind a seemingly successful assignment.
            self._emit_post_call_err_check(target.span)
            # The dict retained the key; a key temporary (``d[keys[i]] = v``)
            # is a NEW reference this site must consume.
            self._gc_release_if_owned(key_obj, idx_expr)
            release_rhs()
            return
        if isinstance(obj_ty, TupleType):
            raise NotImplementedError(
                "tuples are immutable - subscript-assignment not allowed"
            )
        if isinstance(idx_expr.ty, (IntType, BoolType)):
            idx_i64 = self._emit_index_expr_as_i64(idx_expr)
            self.builder.call(
                self.runtime["py_obj_setitem_i64"],
                [obj, idx_i64, rhs_obj],
                name=self._fresh("obj.setitem.i64"),
            )
            self._emit_post_call_err_check(target.span)
            release_rhs()
            return
        key_obj = self._emit_subscript_key_object(idx_expr)
        self.builder.call(self.runtime["py_obj_setitem"], [obj, key_obj, rhs_obj])
        self._emit_post_call_err_check(target.span)
        self._gc_release_if_owned(key_obj, idx_expr)
        release_rhs()

    def _emit_slice_bound_object(self, expr: Optional[Expr]) -> ir.Value:
        """Emit a slice bound as a pcc PyObject*."""
        if expr is None:
            gv = declare_runtime_global(self.module, "py_None")
            return self.builder.load(gv, name=self._fresh("none"))
        return self._emit_as_object(expr)

    def _emit_slice_object_expr(self, sl: Slice) -> ir.Value:
        """Emit a Python ``slice`` object for expression contexts.

        Runtime slicing helpers consume loose ``lo`` / ``hi`` / ``step``
        bounds, but tuple-index paths such as ``obj[:, None]`` need a real
        slice object as an element of the index tuple.  Build that object with
        the native runtime: C-extension ``mp_subscript`` sees the same object
        through the C-API slice bridge, without importing CPython's builtins.
        """
        result = self.builder.call(
            self.runtime["py_slice_new"],
            [
                self._emit_slice_bound_object(sl.lo),
                self._emit_slice_bound_object(sl.hi),
                self._emit_slice_bound_object(sl.step),
            ],
            name=self._fresh("slice.expr"),
        )
        self._emit_post_call_err_check(getattr(sl, "span", None))
        return result

    def _emit_slice_load(self, expr: Subscript) -> ir.Value:
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))

        def slice_log(label: str) -> None:
            if not debug_codegen:
                return
            mod_name = self.ast_module.name or "<module>"
            func_name = (
                self.current_func_def.name
                if self.current_func_def is not None
                else "<top>"
            )
            sys.stderr.write(
                "[pcc.codegen] " + mod_name + ":" + func_name + ":slice " + label + "\n"
            )

        slice_log("begin")
        sl = expr.idx
        assert isinstance(sl, Slice)
        slice_log("emit obj begin")
        obj = self._emit_expr(expr.obj)
        slice_log("emit obj end")
        if obj in getattr(self, "_cpy_values", ()):
            live_owned = self._begin_cpy_operand_evaluation(obj)
            obj_owned = bool(live_owned)

            # Loading the internal ``slice`` callable can itself fail.  Chain
            # that failure through cleanup of a fresh receiver and any outer
            # CPython operand scope rather than bypassing both.
            previous_cpy_cleanup = getattr(
                self,
                "_cpy_operand_cleanup_block",
                None,
            )
            if live_owned:
                cleanup_target = previous_cpy_cleanup
                if cleanup_target is None:
                    cleanup_target = self._ensure_fn_err_exit()
                self._cpy_operand_cleanup_block = (
                    self._make_cpy_operand_cleanup_block(
                        tuple(live_owned),
                        (),
                        cleanup_target,
                        "cpy.slice.callable.cleanup",
                    )
                )
            try:
                slice_fn = self._load_cpython_builtin("slice")
            finally:
                self._cpy_operand_cleanup_block = previous_cpy_cleanup
            self._guard_cpy_value_not_null(slice_fn, tuple(live_owned))
            live_owned.append(slice_fn)

            def _bound_cpy(e: Optional[Expr]) -> tuple[ir.Value, bool]:
                if e is None:
                    gv = declare_runtime_global(self.module, "py_None")
                    none = self.builder.load(gv, name=self._fresh("none"))
                    value = self._mark_owned_cpy_value(
                        self.builder.call(
                            self.runtime["py_cpy_from_pcc_obj"],
                            [none],
                            name=self._fresh("cpy.none"),
                        )
                    )
                    self._guard_cpy_value_not_null(value, tuple(live_owned))
                    live_owned.append(value)
                    return value, True
                return self._emit_checked_cpython_call_arg(
                    e,
                    live_owned,
                )

            lo_cpy, lo_owned = _bound_cpy(sl.lo)
            hi_cpy, hi_owned = _bound_cpy(sl.hi)
            step_cpy, step_owned = _bound_cpy(sl.step)
            slice_obj = self.builder.call(
                self.runtime["py_cpy_call3"],
                [slice_fn, lo_cpy, hi_cpy, step_cpy],
                name=self._fresh("cpy.slice"),
            )
            if lo_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [lo_cpy])
                self._forget_owned_cpy_value(lo_cpy)
            if hi_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [hi_cpy])
                self._forget_owned_cpy_value(hi_cpy)
            if step_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [step_cpy])
                self._forget_owned_cpy_value(step_cpy)
            self.builder.call(self.runtime["py_cpy_decref"], [slice_fn])
            self._forget_owned_cpy_value(slice_fn)
            self._mark_owned_cpy_value(slice_obj)
            self._guard_cpy_value_not_null(
                slice_obj,
                (obj,) if obj_owned else (),
            )
            result = self.builder.call(
                self.runtime["py_cpy_getitem"],
                [obj, slice_obj],
                name=self._fresh("cpy.slice.getitem"),
            )
            self.builder.call(self.runtime["py_cpy_decref"], [slice_obj])
            self._forget_owned_cpy_value(slice_obj)
            if obj_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [obj])
                self._forget_owned_cpy_value(obj)
            self._mark_owned_cpy_value(result)
            self._guard_cpy_value_not_null(result)
            return result
        slice_log("obj ty begin")
        obj_ty = expr.obj.ty
        slice_log("obj ty end")

        def _bound(e: Optional[Expr]) -> ir.Value:
            if e is None:
                slice_log("bound none")
                return ir.Constant(_CSTR, None)
            slice_log("bound expr emit begin")
            v = self._emit_expr(e)
            slice_log("bound expr emit end")
            slice_log("bound marshal begin")
            return marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                v,
                e.ty,
            )

        slice_log("lo begin")
        lo = _bound(sl.lo)
        slice_log("lo end")
        slice_log("hi begin")
        hi = _bound(sl.hi)
        slice_log("hi end")
        slice_log("step begin")
        step = _bound(sl.step)
        slice_log("step end")
        if isinstance(obj_ty, ListType):
            helper = "py_list_slice"
        elif isinstance(obj_ty, TupleType):
            helper = "py_tuple_slice"
        elif isinstance(obj_ty, StrType):
            helper = "py_str_slice"
        elif isinstance(obj_ty, (BytesType, ByteArrayType, MemoryViewType)):
            helper = "py_bytes_slice"
        elif isinstance(obj_ty, (DynType, ClassType)):
            # py_obj_slice dispatches __getitem__(slice(...)) for user
            # instances (ClassType / dyn instance).
            helper = "py_obj_slice"
        else:
            raise NotImplementedError(
                f"Layer 1 slice on type {type(obj_ty).__name__} not supported"
            )
        slice_log("call begin")
        return self.builder.call(
            self.runtime[helper],
            [obj, lo, hi, step],
            name=self._fresh("slice"),
        )

    def _maybe_emit_runtime_dict_lookup(
        self,
        expr: Subscript,
    ) -> Optional[ir.Function]:
        if not isinstance(expr.obj, Attr):
            return None
        if expr.obj.name != "runtime":
            return None
        if not isinstance(expr.obj.obj, Name) or expr.obj.obj.ident != "self":
            return None
        if not isinstance(expr.idx, StrLit):
            return None
        name = expr.idx.value
        fn = self.runtime.get(name)
        if isinstance(fn, ir.Function):
            return fn
        return None

    def _maybe_emit_cpython_subscript_key_literal(
        self,
        expr: Expr,
    ) -> Optional[ir.Value]:
        if isinstance(expr, Name) and expr.ident in (
            "bool",
            "int",
            "float",
            "str",
            "list",
            "dict",
            "tuple",
            "bytes",
            "bytearray",
        ):
            # A CPython-owned typing key must contain CPython type objects.
            # Emitting the normal pcc builtin type here creates a foreign
            # pointer inside a real CPython list/tuple (for example
            # ``Callable[[str], str]``), which crashes when CPython later
            # decrefs that container.
            return self._load_cpython_builtin(expr.ident)
        if isinstance(expr, ListExpr):
            ops: list[tuple[str, ir.Value, Type]] = []
            for el in expr.elems:
                if (
                    isinstance(el, Call)
                    and isinstance(el.func, Name)
                    and el.func.ident == "*"
                    and len(el.args) == 1
                ):
                    return None
                nested = self._maybe_emit_cpython_subscript_key_literal(el)
                value = nested if nested is not None else self._emit_expr(el)
                ops.append(("append", value, el.ty))
            return self._emit_cpython_list_ops(ops)
        if isinstance(expr, TupleExpr):
            ops: list[tuple[str, ir.Value, Type]] = []
            for el in expr.elems:
                if (
                    isinstance(el, Call)
                    and isinstance(el.func, Name)
                    and el.func.ident in ("*", "__starred__")
                    and len(el.args) == 1
                ):
                    return None
                nested = self._maybe_emit_cpython_subscript_key_literal(el)
                value = nested if nested is not None else self._emit_expr(el)
                ops.append(("append", value, el.ty))
            return self._emit_cpython_tuple_ops(ops)
        return None

    def _emit_subscript_load(self, expr: Subscript) -> ir.Value:
        if isinstance(expr.idx, Slice):
            return self._emit_slice_load(expr)
        if isinstance(expr.obj.ty, ValueArrayType):
            return self._emit_value_array_subscript_load(expr)
        native_module_name = self._native_module_name_for_object_expr(expr)
        if native_module_name is not None:
            return self._emit_native_module_placeholder(native_module_name)
        dunder = self._try_dispatch_dunder_unary(expr, "__getitem__", (expr.idx,))
        if dunder is not None:
            return dunder
        native_os_environ_item = self._emit_native_os_environ_subscript(expr)
        if native_os_environ_item is not None:
            return native_os_environ_item

        obj = self._emit_expr(expr.obj)
        if obj in getattr(self, "_cpy_values", ()):
            live_owned = self._begin_cpy_operand_evaluation(obj)
            obj_owned = bool(live_owned)
            literal_key = self._maybe_emit_cpython_subscript_key_literal(expr.idx)
            if literal_key is not None:
                cpy_key, owned = literal_key, True
                self._guard_cpy_value_not_null(cpy_key, tuple(live_owned))
                live_owned.append(cpy_key)
            else:
                cpy_key, owned = self._emit_checked_cpython_call_arg(
                    expr.idx,
                    live_owned,
                )
            result = self.builder.call(
                self.runtime["py_cpy_getitem"],
                [obj, cpy_key],
                name=self._fresh("cpy.getitem"),
            )
            if owned:
                self.builder.call(self.runtime["py_cpy_decref"], [cpy_key])
                self._forget_owned_cpy_value(cpy_key)
            if obj_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [obj])
                self._forget_owned_cpy_value(obj)
            self._mark_owned_cpy_value(result)
            self._guard_cpy_value_not_null(result)
            return result
        obj_ty = expr.obj.ty
        exact_container = self._emit_exact_container_subscript_load_object(expr, obj)
        if exact_container is not None:
            got, elem_ty, _root, _root_ptr = exact_container
            unboxes_native_scalar = (
                elem_ty is not None
                and self._is_native_scalar_type(elem_ty)
                and not (isinstance(elem_ty, IntType) and self._int_exprs_are_boxed())
            )
            if unboxes_native_scalar:
                # Exact-container public getitem helpers return a NEW object
                # reference.  Native scalar coercion consumes only its value,
                # so balance the original owner after the synchronous unbox.
                coerced = self._coerce_from_object(got, elem_ty)
                self._gc_release(
                    got,
                    self._release_context_label("exact-container-subscript"),
                )
                return coerced
            if elem_ty is None:
                return got
            return self._coerce_from_object(got, elem_ty)
        if isinstance(obj_ty, StrType):
            idx_obj = self._emit_as_object(expr.idx)
            result = self.builder.call(
                self.runtime["py_str_index"],
                [obj, idx_obj],
                name=self._fresh("str.idx"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            self._gc_release_if_owned(obj, expr.obj)
            return result
        if isinstance(obj_ty, (BytesType, ByteArrayType, MemoryViewType)):
            idx_obj = self._emit_as_object(expr.idx)
            got = self.builder.call(
                self.runtime["py_bytes_getitem"],
                [obj, idx_obj],
                name=self._fresh("bytes.idx"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            result = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                got,
                expr.ty,
            )
            self._gc_release_if_owned(obj, expr.obj)
            return result
        # Dynamic operand: the raising subscript entry points convert the
        # getitem primitives' silent NULL (missing key, out-of-range index,
        # non-subscriptable object) into KeyError/IndexError/TypeError, and the
        # post-call check routes it to the handler or function exit like every
        # other raise-capable call.  An unconditionally owned operand
        # temporary is released on the exceptional edge as well.
        release_on_error = (
            (obj,) if self._value_is_owned_object(obj) else ()
        )
        if isinstance(expr.idx.ty, (IntType, BoolType)):
            idx_i64 = self._emit_index_expr_as_i64(expr.idx)
            result = self.builder.call(
                self.runtime["py_obj_subscript_i64"],
                [obj, idx_i64],
                name=self._fresh("obj.subscript.i64"),
            )
            self._note_owned_object_value(result)
            self._emit_post_call_err_check(
                getattr(expr, "span", None),
                release_on_error=release_on_error,
            )
            self._gc_release_if_owned(obj, expr.obj)
            return result
        key_obj = self._emit_subscript_key_object(expr.idx)
        result = self.builder.call(
            self.runtime["py_obj_subscript"],
            [obj, key_obj],
            name=self._fresh("obj.subscript"),
        )
        self._note_owned_object_value(result)
        self._emit_post_call_err_check(
            getattr(expr, "span", None),
            release_on_error=release_on_error,
        )
        self._gc_release_if_owned(obj, expr.obj)
        return result

    def _emit_subscript_key_object(self, expr: Expr) -> ir.Value:
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            expr.ty,
            expr,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                expr.ty,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass
        return self._emit_as_object(expr)
