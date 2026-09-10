"""Native ``asyncio`` module lowering helpers for layer-1 codegen."""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import Attr, Call, Expr, FuncDef, Name


_I8 = ir.IntType(8)
_CSTR = _I8.as_pointer()



class NativeAsyncioLoweringMixin:
    def _emit_native_asyncio_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "asyncio"
        ):
            return None
        return self._emit_native_asyncio_value_call(
            "asyncio." + attr.name,
            expr.args,
            expr.kwargs,
        )

    def _emit_native_asyncio_value_call(self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs:
            return None
        if kind == "asyncio.run":
            if len(args) != 1:
                return None
            result = self.builder.call(
                self.runtime["py_await"],
                [self._emit_as_object(args[0])],
                name=self._fresh("asyncio.run"),
            )
            self._emit_post_call_err_check(args[0].span)
            return result
        if kind == "asyncio.sleep":
            if len(args) != 1:
                return None
            return self.builder.call(
                self.runtime["py_asyncio_sleep"],
                [self._emit_as_object(args[0])],
                name=self._fresh("asyncio.sleep"),
            )
        return None

    def _emit_coroutine_from_adapter(self,
        display_name: str,
        adapter: ir.Function,
        args_tuple: ir.Value,
        captures_tuple: Optional[ir.Value] = None,
    ) -> ir.Value:
        if captures_tuple is None:
            captures_tuple = self._emit_empty_tuple("coroutine.captures")
        runner = self.builder.bitcast(
            adapter,
            _CSTR,
            name=self._fresh(f"{display_name}.runner"),
        )
        coro = self.builder.call(
            self.runtime["py_coroutine_new_resumable"],
            [self._attr_name_ptr(display_name), runner, captures_tuple, args_tuple],
            name=self._fresh(f"{display_name}.coroutine"),
        )
        self._gc_release(captures_tuple)
        self._gc_release(args_tuple)
        return coro

    def _emit_async_user_function_call(self,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> ir.Value:
        resolved_args = tuple(
            self._resolve_call_kwargs(
                args,
                kwargs,
                ast_func_def.args,
            )
        )
        runtime_formals = tuple(a for a in ast_func_def.args if a.name != "")
        adapter = self._emit_native_func_adapter(
            name,
            fn,
            runtime_formals,
            (),
            ast_func_def.return_ty,
        )
        args_tuple = self._emit_call_args_tuple(resolved_args)
        return self._emit_coroutine_from_adapter(name, adapter, args_tuple)

    def _emit_await_expr(self, expr: Call) -> ir.Value:
        if len(expr.args) != 1 or expr.kwargs:
            raise NotImplementedError("await expects exactly one expression")
        if getattr(self, "_generator_ctx_stack", ()):
            from .async_await_lowering import emit_resumable_await
            return emit_resumable_await(self, expr)
        source = expr.args[0]
        hint = self._class_hint_for_expr(source)
        if hint is not None:
            await_info = self._resolve_method_mro(hint, "__await__")
            if await_info is not None:
                method_fn = await_info.methods.get("__await__")
                if method_fn is not None:
                    obj_val = self._emit_expr(source)
                    iterator = self._emit_direct_method_call(
                        method_fn,
                        obj_val,
                        await_info,
                        "__await__",
                        (),
                    )
                    result = self.builder.call(
                        self.runtime["py_await"],
                        [iterator],
                        name=self._fresh("await.result"),
                    )
                    self._emit_post_call_err_check(expr.span)
                    return result
        awaitable = self._emit_as_object(source)
        result = self.builder.call(
            self.runtime["py_await"],
            [awaitable],
            name=self._fresh("await.result"),
        )
        self._emit_post_call_err_check(expr.span)
        return result


__all__ = ["NativeAsyncioLoweringMixin"]
