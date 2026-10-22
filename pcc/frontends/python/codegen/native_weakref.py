"""Native ``weakref`` module lowering helpers."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, Expr, Lambda, Name



class NativeWeakrefLoweringMixin:
    def _emit_native_weakref_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "weakref"
        ):
            return None
        return self._emit_native_weakref_value_call(
            "weakref." + attr.name,
            expr.args,
            expr.kwargs,
        )

    def _emit_native_weakref_value_call(self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs:
            return None
        if kind == "weakref.WeakValueDictionary":
            if args:
                return None
            return self.builder.call(
                self.runtime["py_weak_value_dict_new"],
                [],
                name=self._fresh("weak.value.dict"),
            )
        if kind == "weakref.WeakKeyDictionary":
            if args:
                return None
            return self.builder.call(
                self.runtime["py_weak_key_dict_new"],
                [],
                name=self._fresh("weak.key.dict"),
            )
        if not (
            kind == "weakref.ref" and len(args) in (1, 2)
            or kind == "weakref.proxy" and len(args) == 1
        ):
            return None

        # The runtime borrows both arguments. A captured name is an owned
        # cell read, just like a call/subscript result: keep that owner in a
        # root through callback evaluation, then retire it after publication.
        previous = self._current_try_err_block()
        error_target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        saved_preference = self._prefer_native_callable_values
        output = self._new_slot_call_root(kind + ".result")
        roots = [output]
        operands = []
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
            self._cpy_operand_cleanup_block = self._try_err_block
            referent = self._emit_slot_call_operand(args[0], kind + ".target")
            roots.append(referent)
            operands.append(referent)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if len(args) == 2:
                # Use ordinary callable construction's rooted NEW-result
                # contract for named, lambda, captured and computed callbacks.
                self._prefer_native_callable_values = True
                callback = self._emit_slot_call_operand(args[1], kind + ".callback")
                self._prefer_native_callable_values = saved_preference
                roots.append(callback)
                operands.append(callback)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
                self._cpy_operand_cleanup_block = self._try_err_block
            omitted = () if len(args) == 2 else (ir.Constant(ir.IntType(8).as_pointer(), None),)
            self._slot_call_runtime_call(
                "py_weakref_new", tuple(operands), result_slot=output,
                suffix_args=omitted, span=args[0].span,
            )
            # Releasing a temporary target can run its finalizer and the new
            # weakref's callback. The result remains rooted across both.
            self._release_slot_call_roots(tuple(operands))
            return self._take_slot_call_root(output)
        finally:
            self._prefer_native_callable_values = saved_preference
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _weak_dict_constructor_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, Call) or expr.kwargs or expr.args:
            return None
        kind = self._native_builtin_value_kind_for_expr(expr.func)
        if kind == "weakref.WeakValueDictionary":
            return "value"
        if kind == "weakref.WeakKeyDictionary":
            return "key"
        return None

    def _weak_dict_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if isinstance(expr, Name):
            return getattr(self, "_weak_dict_env_flags", {}).get(expr.ident)
        return None

    def _weakref_constructor_kind_for_expr(self, expr: Expr) -> Optional[str]:
        if not isinstance(expr, Call) or expr.kwargs:
            return None
        kind = self._native_builtin_value_kind_for_expr(expr.func)
        if kind == "weakref.ref" and len(expr.args) in (1, 2):
            return "ref"
        return None

    def _weakref_call_expr_returns_owned_object(self, expr: Expr) -> bool:
        if not isinstance(expr, Call) or expr.args or expr.kwargs:
            return False
        if isinstance(expr.func, Name):
            return bool(
                getattr(self, "_weakref_env_flags", {}).get(expr.func.ident, False)
            )
        return False

    def _emit_weakref_callback_object(self, expr: Expr) -> ir.Value:
        if isinstance(expr, Name):
            fn_ir = self.functions.get(expr.ident)
            if fn_ir is not None:
                return self._emit_native_func_value(expr.ident, expr.ident, fn_ir, ())
        if isinstance(expr, Lambda):
            native_lambda = self._emit_native_lambda_callback_object(expr)
            if native_lambda is not None:
                return native_lambda
        return self._emit_as_object(expr)


__all__ = ["NativeWeakrefLoweringMixin"]
