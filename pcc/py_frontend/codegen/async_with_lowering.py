"""Async context-manager lowering for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import Call, ClassType, DynType, Expr, Name, NoneType, Type, With
from .runtime_abi import declare_runtime_global


_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_RUNTIME_ERROR_TAG = 7
_STOP_ITERATION_TAG = 8


class AsyncWithLoweringMixin:
    def _emit_with(self, stmt: With) -> None:
        """Narrow-subset ``with EXPR as VAR: BODY`` lowering."""
        if getattr(stmt, "is_async", False):
            self._emit_async_with(stmt)
            return
        if len(stmt.items) > 1:
            nested_body = stmt.body
            for item in reversed(stmt.items[1:]):
                nested_body = (
                    With(
                        span=stmt.span,
                        items=(item,),
                        body=nested_body,
                        is_async=stmt.is_async,
                    ),
                )
            self._emit_with(
                With(
                    span=stmt.span,
                    items=(stmt.items[0],),
                    body=nested_body,
                    is_async=stmt.is_async,
                )
            )
            return
        if len(stmt.items) != 1:
            raise NotImplementedError(
                "Layer 1 with-statement only handles a single context expression"
            )
        if self._emit_native_file_with(stmt):
            return
        if self._emit_native_tempdir_with(stmt):
            return
        if self._emit_native_generator_context_with(stmt):
            return
        if self._emit_native_user_context_with(stmt):
            return
        ctx_expr, as_expr = stmt.items[0]
        ctx_val = self._emit_expr(ctx_expr)
        if ctx_val not in getattr(self, "_cpy_values", ()):
            enter_val = self.builder.call(
                self.runtime["py_context_enter"],
                [ctx_val],
                name=self._fresh("with.dynamic.enter"),
            )
            self._emit_post_call_err_check()
            self._emit_native_context_body(stmt, ctx_val, enter_val)
            return
        ctx_owned = self._cpy_value_is_owned(ctx_val)
        self._guard_cpy_value_not_null(ctx_val)

        enter_ptr = self._ptr_to_cstr(
            self._cstr_global("__enter__", ".cpy.attr.__enter__")
        )
        enter_fn = self._mark_owned_cpy_value(
            self.builder.call(
                self.runtime["py_cpy_getattr"],
                [ctx_val, enter_ptr],
                name=self._fresh("with.enter.fn"),
            )
        )
        enter_val = self.builder.call(
            self.runtime["py_cpy_call_noargs"],
            [enter_fn],
            name=self._fresh("with.enter.val"),
        )
        self.builder.call(self.runtime["py_cpy_decref"], [enter_fn])
        self._forget_owned_cpy_value(enter_fn)
        self._guard_cpy_value_not_null(enter_val)

        self._mark_owned_cpy_value(enter_val)

        if as_expr is not None:
            if not isinstance(as_expr, Name):
                raise NotImplementedError("Layer 1 with: as-clause must be a bare name")
            slot = self.env.get(as_expr.ident)
            if slot is None:
                alloca = self._alloca_in_entry(
                    _CSTR,
                    name=f"{as_expr.ident}.addr",
                )
                self.env[as_expr.ident] = (
                    alloca,
                    _CSTR,
                    DynType(name="dyn"),
                )
                slot = self.env[as_expr.ident]
            self.builder.store(enter_val, slot[0])
            if not hasattr(self, "_cpy_env_flags"):
                self._cpy_env_flags = {}
            self._cpy_env_flags[as_expr.ident] = True

        self._emit_stmts(stmt.body)

        if not self._builder_block_is_terminated():
            exit_ptr = self._ptr_to_cstr(
                self._cstr_global("__exit__", ".cpy.attr.__exit__")
            )
            exit_fn = self._mark_owned_cpy_value(
                self.builder.call(
                    self.runtime["py_cpy_getattr"],
                    [ctx_val, exit_ptr],
                    name=self._fresh("with.exit.fn"),
                )
            )
            none_gv = declare_runtime_global(self.module, "py_None")
            none = self.builder.load(none_gv, name=self._fresh("none"))
            cpy_none = self.builder.call(
                self.runtime["py_cpy_from_pcc_obj"],
                [none],
                name=self._fresh("cpy.none"),
            )
            exit_val = self.builder.call(
                self.runtime["py_cpy_call3"],
                [exit_fn, cpy_none, cpy_none, cpy_none],
                name=self._fresh("with.exit.val"),
            )
            self.builder.call(self.runtime["py_cpy_decref"], [cpy_none])
            self.builder.call(self.runtime["py_cpy_decref"], [exit_fn])
            self._forget_owned_cpy_value(exit_fn)
            self._guard_cpy_value_not_null(exit_val)
            self.builder.call(self.runtime["py_cpy_decref"], [exit_val])
            self.builder.call(self.runtime["py_cpy_decref"], [enter_val])
            self._forget_owned_cpy_value(enter_val)
            if ctx_owned:
                self.builder.call(self.runtime["py_cpy_decref"], [ctx_val])
                self._forget_owned_cpy_value(ctx_val)

    def _class_name_for_context_expr(self, expr: Expr) -> Optional[str]:
        if (
            isinstance(expr, Call)
            and isinstance(expr.func, Name)
            and hasattr(self, "class_lowering")
            and expr.func.ident in self.class_lowering.classes
        ):
            return expr.func.ident
        if isinstance(expr, Name):
            return self.env_class_hint.get(expr.ident)
        if isinstance(expr.ty, ClassType):
            return self._ensure_class_type_registered(expr.ty)
        return None

    def _context_expr_generator_contextmanager(self, expr: Expr):
        if not isinstance(expr, Call) or not isinstance(expr.func, Name):
            return None
        try:
            fd = self._find_user_funcdef(expr.func.ident)
        except Exception:
            return None
        if not self._funcdef_has_yield_sentinel(fd):
            return None
        for decorator in fd.decorators:
            qualname = self._decorator_qualname(decorator)
            if qualname in ("contextmanager", "contextlib.contextmanager"):
                return fd
        return None

    def _raise_contextmanager_runtime_error(self, message: str) -> None:
        msg = self._ptr_to_cstr(
            self._cstr_global(message, ".contextmanager.error")
        )
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, _RUNTIME_ERROR_TAG), msg],
            name=self._fresh("contextmanager.err"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])

    def _branch_on_stop_iteration_or_propagate(
        self,
        *,
        after_bb,
        propagate_bb,
        prefix: str,
    ) -> None:
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh(f"{prefix}.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, _STOP_ITERATION_TAG)],
            name=self._fresh(f"{prefix}.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh(f"{prefix}.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh(f"{prefix}.stop_i1"),
        )
        clear_bb = self.current_function.append_basic_block(
            name=self._fresh(f"{prefix}.clear")
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)

        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(after_bb)

    def _emit_native_generator_context_with(self, stmt: With) -> bool:
        ctx_expr, as_expr = stmt.items[0]
        if self._context_expr_generator_contextmanager(ctx_expr) is None:
            return False

        ctx_val = self._emit_expr(ctx_expr)
        enter_val = self.builder.call(
            self.runtime["py_gen_next"],
            [ctx_val],
            name=self._fresh("contextmanager.enter"),
        )
        self._emit_post_call_err_check(stmt.span)
        if as_expr is not None:
            if not isinstance(as_expr, Name):
                raise NotImplementedError(
                    "Layer 1 contextmanager with: as-clause must be a bare name"
                )
            self._store_value_at_name(as_expr, enter_val, as_expr.ty)

        fn = self.current_function
        err_bb = fn.append_basic_block(name=self._fresh("contextmanager.err"))
        after_bb = fn.append_basic_block(name=self._fresh("contextmanager.after"))
        prev_err_block = getattr(self, "_try_err_block", None)
        self._try_err_block = err_bb
        try:
            self._emit_stmts(stmt.body)
        finally:
            self._try_err_block = prev_err_block

        outer = prev_err_block or self._ensure_fn_err_exit()
        null = ir.Constant(_CSTR, None)

        if not self._builder_block_is_terminated():
            exit_val = self.builder.call(
                self.runtime["py_gen_next"],
                [ctx_val],
                name=self._fresh("contextmanager.exit"),
            )
            is_null = self.builder.icmp_unsigned(
                "==",
                exit_val,
                null,
                name=self._fresh("contextmanager.exit.null"),
            )
            stop_check_bb = fn.append_basic_block(
                name=self._fresh("contextmanager.exit.stop_check")
            )
            yielded_again_bb = fn.append_basic_block(
                name=self._fresh("contextmanager.exit.yielded")
            )
            self.builder.cbranch(is_null, stop_check_bb, yielded_again_bb)

            self.builder.position_at_end(yielded_again_bb)
            self._gc_release(exit_val)
            self._raise_contextmanager_runtime_error("generator didn't stop")
            self.builder.branch(outer)

            self.builder.position_at_end(stop_check_bb)
            self._branch_on_stop_iteration_or_propagate(
                after_bb=after_bb,
                propagate_bb=outer,
                prefix="contextmanager.exit",
            )

        self.builder.position_at_end(err_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("contextmanager.throw.exc"),
        )
        throw_val = self.builder.call(
            self.runtime["py_gen_throw"],
            [ctx_val, current_exc],
            name=self._fresh("contextmanager.throw"),
        )
        throw_is_null = self.builder.icmp_unsigned(
            "==",
            throw_val,
            null,
            name=self._fresh("contextmanager.throw.null"),
        )
        throw_stop_check_bb = fn.append_basic_block(
            name=self._fresh("contextmanager.throw.stop_check")
        )
        throw_yielded_bb = fn.append_basic_block(
            name=self._fresh("contextmanager.throw.yielded")
        )
        self.builder.cbranch(throw_is_null, throw_stop_check_bb, throw_yielded_bb)

        self.builder.position_at_end(throw_yielded_bb)
        self._gc_release(throw_val)
        self._raise_contextmanager_runtime_error("generator didn't stop after throw")
        self.builder.branch(outer)

        self.builder.position_at_end(throw_stop_check_bb)
        self._branch_on_stop_iteration_or_propagate(
            after_bb=after_bb,
            propagate_bb=outer,
            prefix="contextmanager.throw",
        )

        self.builder.position_at_end(after_bb)
        return True

    def _emit_native_user_context_with(self, stmt: With) -> bool:
        ctx_expr, as_expr = stmt.items[0]
        class_name = self._class_name_for_context_expr(ctx_expr)
        if class_name is None:
            return False
        enter_info = self._resolve_method_mro(class_name, "__enter__")
        exit_info = self._resolve_method_mro(class_name, "__exit__")
        if enter_info is None or exit_info is None:
            return False
        enter_fn = enter_info.methods.get("__enter__")
        if enter_fn is None or exit_info.methods.get("__exit__") is None:
            return False

        ctx_val = self._emit_expr(ctx_expr)
        enter_val = self._emit_direct_method_call(
            enter_fn,
            ctx_val,
            enter_info,
            "__enter__",
            (),
        )

        self._emit_native_context_body(stmt, ctx_val, enter_val)
        return True

    def _emit_native_context_body(
        self,
        stmt: With,
        ctx_val: ir.Value,
        enter_val: ir.Value,
        cleanup_runtime: Optional[str] = None,
    ) -> None:
        """Emit the body/exit control flow for a pcc-native manager.

        The manager may be statically known (the direct-method path above) or
        a native object whose precise class is only available at runtime.  In
        both cases ``py_context_exit`` owns Python's exception-suppression
        protocol, so the body must use the same unwind graph.
        """
        ctx_expr, as_expr = stmt.items[0]
        context_name = (
            self._generator_with_context_name(stmt.items[0])
            if self._generator_ctx_stack else self._fresh("with.context")
        )
        context_target = Name(span=ctx_expr.span, ty=DynType(name="dyn"), ident=context_name)
        # A private owned local is independent of the user's `as` binding.
        # The generator collector reserves this same name before emission.
        retained_context = self._gc_retain(ctx_val)
        self._store_unpack_target(context_target, retained_context, context_target.ty,
                                  value_is_owned=True)
        self._gc_release_if_owned(ctx_val, ctx_expr)
        if as_expr is not None:
            if not isinstance(as_expr, Name):
                raise NotImplementedError(
                    "Layer 1 native with: as-clause must be a bare name"
                )
            self._store_unpack_target(as_expr, enter_val, as_expr.ty, value_is_owned=True)
        else:
            self._gc_release(enter_val)

        def clear_context():
            self._store_unpack_target(context_target, self._emit_none_literal(), context_target.ty,
                                      value_is_owned=True)
            if self._generator_ctx_stack:
                generator = self._generator_ctx_stack[-1]
                index, _slot = generator["frame_slots"][context_name]
                self.builder.call(self._generator_frame_helper("set"),
                                  [generator["frame"], ir.Constant(_I64, index), self._emit_none_literal()])

        fn = self.current_function
        err_bb = fn.append_basic_block(name=self._fresh("with.err"))
        after_bb = fn.append_basic_block(name=self._fresh("with.after"))
        prev_err_block = getattr(self, "_try_err_block", None)
        def exit_normal():
            # Return/break/continue must run this same exit, just like an
            # enclosing finally. An exception from __exit__ belongs outside
            # this manager rather than re-entering its own exception path.
            saved_err_block = getattr(self, "_try_err_block", None)
            self._try_err_block = prev_err_block
            try:
                context_value = self._emit_name(context_target)
                none_gv = declare_runtime_global(self.module, "py_None")
                none = self.builder.load(none_gv, name=self._fresh("with.none"))
                if cleanup_runtime is None:
                    self.builder.call(
                        self.runtime["py_context_exit"],
                        [context_value, none, none, none],
                        name=self._fresh("with.exit"),
                    )
                else:
                    self.builder.call(self.runtime[cleanup_runtime], [context_value])
                clear_context()
                self._emit_post_call_err_check()
            finally:
                self._try_err_block = saved_err_block
        outer_finallys = list(self._finally_stack)
        self._finally_stack = outer_finallys + [exit_normal]
        self._try_err_block = err_bb
        try:
            self._emit_stmts(stmt.body)
        finally:
            self._try_err_block = prev_err_block
            self._finally_stack = outer_finallys

        if not self._builder_block_is_terminated():
            exit_normal()
            self.builder.branch(after_bb)

        self.builder.position_at_end(err_bb)
        if cleanup_runtime is not None:
            self.builder.call(self.runtime[cleanup_runtime], [self._emit_name(context_target)])
            clear_context()
            self.builder.branch(prev_err_block or self._ensure_fn_err_exit())
            self.builder.position_at_end(after_bb)
            return
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("with.cur.exc"),
        )
        exc_type = self.builder.call(
            self.runtime["py_type_builtin"],
            [current_exc],
            name=self._fresh("with.exc.type"),
        )
        none_gv = declare_runtime_global(self.module, "py_None")
        none = self.builder.load(none_gv, name=self._fresh("with.err.none"))
        suppress = self.builder.call(
            self.runtime["py_context_exit"],
            [self._emit_name(context_target), exc_type, current_exc, none],
            name=self._fresh("with.exit.err"),
        )
        suppress_i1 = self.builder.icmp_signed(
            "!=",
            suppress,
            ir.Constant(_I64, 0),
            name=self._fresh("with.suppress.i1"),
        )
        suppress_bb = fn.append_basic_block(name=self._fresh("with.suppress"))
        propagate_bb = fn.append_basic_block(name=self._fresh("with.propagate"))
        self.builder.cbranch(suppress_i1, suppress_bb, propagate_bb)

        self.builder.position_at_end(suppress_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        clear_context()
        self.builder.branch(after_bb)

        self.builder.position_at_end(propagate_bb)
        clear_context()
        outer = prev_err_block or self._ensure_fn_err_exit()
        self.builder.branch(outer)

        self.builder.position_at_end(after_bb)

    def _emit_async_with(self, stmt: With) -> None:
        if len(stmt.items) != 1:
            raise NotImplementedError(
                "Layer 1 async with only handles a single context expression"
            )
        ctx_expr, as_expr = stmt.items[0]
        class_name = self._class_name_for_context_expr(ctx_expr)
        if class_name is None:
            raise NotImplementedError(
                "Layer 1 async with needs a pcc-native context manager"
            )
        enter_info = self._resolve_method_mro(class_name, "__aenter__")
        exit_info = self._resolve_method_mro(class_name, "__aexit__")
        if enter_info is None or exit_info is None:
            raise NotImplementedError(
                "Layer 1 async with needs __aenter__ and __aexit__"
            )
        enter_fn = enter_info.methods.get("__aenter__")
        exit_fn = exit_info.methods.get("__aexit__")
        if enter_fn is None or exit_fn is None:
            raise NotImplementedError(
                "Layer 1 async with needs __aenter__ and __aexit__"
            )

        ctx_val = self._emit_expr(ctx_expr)
        enter_coro = self._emit_direct_method_call(
            enter_fn,
            ctx_val,
            enter_info,
            "__aenter__",
            (),
        )
        enter_val = self.builder.call(
            self.runtime["py_await"],
            [enter_coro],
            name=self._fresh("async.with.enter"),
        )
        self._emit_post_call_err_check(stmt.span)
        if as_expr is not None:
            if not isinstance(as_expr, Name):
                raise NotImplementedError(
                    "Layer 1 async with: as-clause must be a bare name"
                )
            self._store_value_at_name(as_expr, enter_val, as_expr.ty)

        span = stmt.span
        none_ty = NoneType(name="None")

        def _none_value() -> ir.Value:
            none_gv = declare_runtime_global(self.module, "py_None")
            return self.builder.load(none_gv, name=self._fresh("async.with.none"))

        def _call_exit(
            args: tuple[tuple[ir.Value, Type], ...],
        ) -> ir.Value:
            exit_coro = self._emit_direct_method_value_call(
                exit_fn,
                ctx_val,
                exit_info,
                "__aexit__",
                args,
            )
            result = self.builder.call(
                self.runtime["py_await"],
                [exit_coro],
                name=self._fresh("async.with.exit"),
            )
            self._emit_post_call_err_check(span)
            return result

        fn = self.current_function
        err_bb = fn.append_basic_block(name=self._fresh("async.with.err"))
        after_bb = fn.append_basic_block(name=self._fresh("async.with.after"))
        prev_err_block = getattr(self, "_try_err_block", None)
        self._try_err_block = err_bb
        try:
            self._emit_stmts(stmt.body)
        finally:
            self._try_err_block = prev_err_block

        if not self._builder_block_is_terminated():
            none_args = (
                (_none_value(), none_ty),
                (_none_value(), none_ty),
                (_none_value(), none_ty),
            )
            _call_exit(none_args)
            if not self._builder_block_is_terminated():
                self.builder.branch(after_bb)

        self.builder.position_at_end(err_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("async.with.exc"),
        )
        exc_cls = self.builder.call(
            self.runtime["py_obj_getattr"],
            [current_exc, self._attr_name_ptr("__class__")],
            name=self._fresh("async.with.exc.cls"),
        )
        self.builder.call(self.runtime["py_clear_exception"], [])
        exit_result = _call_exit(
            (
                (exc_cls, DynType(name="dyn")),
                (current_exc, DynType(name="dyn")),
                (_none_value(), none_ty),
            )
        )
        suppress = self._truthy(exit_result, DynType(name="dyn"))
        suppress_bb = fn.append_basic_block(name=self._fresh("async.with.suppress"))
        propagate_bb = fn.append_basic_block(name=self._fresh("async.with.propagate"))
        self.builder.cbranch(suppress, suppress_bb, propagate_bb)

        self.builder.position_at_end(suppress_bb)
        self.builder.branch(after_bb)

        self.builder.position_at_end(propagate_bb)
        self.builder.call(self.runtime["py_raise"], [current_exc])
        outer = prev_err_block or self._ensure_fn_err_exit()
        self.builder.branch(outer)

        self.builder.position_at_end(after_bb)
