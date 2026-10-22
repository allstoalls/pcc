"""Native ``gc`` module lowering helpers for layer-1 codegen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, Expr, Name


_I32 = ir.IntType(32)
_I64 = ir.IntType(64)



class NativeGcLoweringMixin:
    def _emit_native_gc_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "gc"
        ):
            return None
        return self._emit_native_gc_value_call(
            "gc." + attr.name,
            expr.args,
            expr.kwargs,
        )

    def _emit_native_gc_callbacks_method(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in ("append", "remove") or expr.kwargs:
            return None
        callbacks_expr = attr.obj
        if (
            not isinstance(callbacks_expr, Attr)
            or callbacks_expr.name != "callbacks"
            or not isinstance(callbacks_expr.obj, Name)
            or self._native_builtin_module_for_name(callbacks_expr.obj.ident) != "gc"
            or len(expr.args) != 1
        ):
            return None

        callback = self._emit_weakref_callback_object(expr.args[0])
        helper = (
            "py_gc_callbacks_append"
            if attr.name == "append"
            else "py_gc_callbacks_remove"
        )
        self.builder.call(self.runtime[helper], [callback])
        self._gc_release(callback)
        return self._emit_none_literal()

    def _emit_native_gc_value_call(self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs:
            return None
        if kind == "gc.collect":
            if len(args) > 1:
                return None
            reason = ir.Constant(_I32, -1)
            if len(args) == 1:
                reason_i64 = self._emit_expr_as_i64(args[0])
                reason = self.builder.trunc(
                    reason_i64,
                    _I32,
                    name=self._fresh("gc.collect.reason"),
                )
            collected = self.builder.call(
                self.runtime["pcc_gc_collect"],
                [reason],
                name=self._fresh("gc.collect.count"),
            )
            boxed = self.builder.call(
                self.runtime["py_int_from_i64"],
                [collected],
                name=self._fresh("gc.collect.obj"),
            )
            self._note_owned_object_value(boxed)
            return boxed
        if kind == "gc.disable" and not args:
            self.builder.call(self.runtime["py_gc_disable"], [])
            return self._emit_none_literal()
        if kind == "gc.enable" and not args:
            self.builder.call(self.runtime["py_gc_enable"], [])
            return self._emit_none_literal()
        if kind == "gc.isenabled" and not args:
            enabled = self.builder.call(
                self.runtime["py_gc_is_enabled"],
                [],
                name=self._fresh("gc.isenabled.i64"),
            )
            enabled_i32 = self.builder.trunc(
                enabled,
                _I32,
                name=self._fresh("gc.isenabled.i32"),
            )
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [enabled_i32],
                name=self._fresh("gc.isenabled"),
            )
        if kind == "gc.is_tracked" and len(args) == 1:
            tracked = self.builder.call(
                self.runtime["py_gc_is_tracked"],
                [self._emit_as_object(args[0])],
                name=self._fresh("gc.is_tracked.i64"),
            )
            tracked_i32 = self.builder.trunc(
                tracked,
                _I32,
                name=self._fresh("gc.is_tracked.i32"),
            )
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [tracked_i32],
                name=self._fresh("gc.is_tracked"),
            )
        if kind == "gc.is_finalized" and len(args) == 1:
            return self.builder.call(
                self.runtime["py_bool_from_bit"],
                [ir.Constant(_I32, 0)],
                name=self._fresh("gc.is_finalized"),
            )
        if kind == "gc.get_count" and not args:
            return self._emit_gc_i64_3tuple("py_gc_get_count")
        if kind == "gc.get_threshold" and not args:
            return self._emit_gc_i64_3tuple("py_gc_get_threshold")
        if kind == "gc.get_stats" and not args:
            return self._emit_gc_stats()
        if kind == "gc.freeze" and not args:
            self.builder.call(self.runtime["py_gc_freeze"], [])
            return self._emit_none_literal()
        if kind == "gc.unfreeze" and not args:
            self.builder.call(self.runtime["py_gc_unfreeze"], [])
            return self._emit_none_literal()
        if kind == "gc.immortalize" and len(args) == 1:
            self.builder.call(
                self.runtime["pcc_gc_immortalize"],
                [self._emit_as_object(args[0])],
            )
            return self._emit_none_literal()
        if kind == "gc.get_freeze_count" and not args:
            count = self.builder.call(
                self.runtime["py_gc_get_freeze_count"],
                [],
                name=self._fresh("gc.freeze.count"),
            )
            boxed = self.builder.call(
                self.runtime["py_int_from_i64"],
                [count],
                name=self._fresh("gc.freeze.count.obj"),
            )
            self._note_owned_object_value(boxed)
            return boxed
        if kind == "gc.get_objects" and not args:
            return self.builder.call(
                self.runtime["py_gc_get_objects"],
                [],
                name=self._fresh("gc.objects"),
            )
        if kind == "gc.get_referents" and len(args) == 1:
            return self.builder.call(
                self.runtime["py_gc_get_referents"],
                [self._emit_as_object(args[0])],
                name=self._fresh("gc.referents"),
            )
        if kind == "gc.get_referrers" and len(args) == 1:
            return self.builder.call(
                self.runtime["py_gc_get_referrers"],
                [self._emit_as_object(args[0])],
                name=self._fresh("gc.referrers"),
            )
        if kind == "gc.set_threshold" and 1 <= len(args) <= 3:
            values = [
                ir.Constant(_I32, -1),
                ir.Constant(_I32, -1),
                ir.Constant(_I32, -1),
            ]
            i = 0
            while i < len(args):
                raw_i64 = self._emit_expr_as_i64(args[i])
                values[i] = self.builder.trunc(
                    raw_i64,
                    _I32,
                    name=self._fresh(f"gc.threshold.{i}"),
                )
                i += 1
            self.builder.call(self.runtime["py_gc_set_threshold"], values)
            return self._emit_none_literal()
        return None

    def _emit_gc_i64_3tuple(self, helper_name: str) -> ir.Value:
        # Snapshot the counters before allocating the result tuple.  For
        # gc.get_count() the tuple itself is tracked, so allocating first made
        # the API observe (and report) its own return object.
        values = []
        i = 0
        while i < 3:
            values.append(
                self.builder.call(
                    self.runtime[helper_name],
                    [ir.Constant(_I32, i)],
                    name=self._fresh(f"gc.tuple.{i}.i64"),
                )
            )
            i += 1
        out = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 3)],
            name=self._fresh("gc.tuple"),
        )
        i = 0
        while i < 3:
            obj = self.builder.call(
                self.runtime["py_int_from_i64"],
                [values[i]],
                name=self._fresh(f"gc.tuple.{i}.obj"),
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [out, ir.Constant(_I64, i), obj],
            )
            i += 1
        return out

    def _emit_gc_garbage(self) -> ir.Value:
        return self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("gc.garbage"),
        )

    def _emit_gc_stats(self) -> ir.Value:
        out = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 1)],
            name=self._fresh("gc.stats"),
        )
        stats = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh("gc.stats.dict"),
        )
        for key_name in ("collections", "collected", "uncollectable"):
            key = self._emit_str_literal(key_name)
            value = self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("gc.stats.value"),
            )
            self.builder.call(
                self.runtime["py_dict_set"],
                [stats, key, value],
            )
            self._gc_release(value)
        self.builder.call(
            self.runtime["py_list_append"],
            [out, stats],
        )
        self._gc_release(stats)
        return out


__all__ = ["NativeGcLoweringMixin"]
