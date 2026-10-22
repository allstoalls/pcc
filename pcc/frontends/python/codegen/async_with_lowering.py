"""Async context-manager lowering for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Call,
    ClassType,
    DynType,
    Expr,
    Name,
    NoneType,
    RawPointerType,
    Type,
    With,
)
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.generator_context_lowering import (
    emit_generator_context_enter,
    emit_generator_context_exit,
)


_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


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
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = []
        native_context = False
        try:
            if not isinstance(ctx_expr, Call) and not self._expr_looks_cpython(ctx_expr):
                context_root = self._emit_slot_call_operand(ctx_expr, "with.dynamic.manager")
                roots.append(context_root)
                native_context = True
            elif isinstance(ctx_expr, Call):
                # Let the actual producer select its domain. A known foreign
                # call keeps its existing refcount route; a native call must
                # publish into this pre-existing root, never get rooted late.
                context_root = self._new_slot_call_root("with.dynamic.manager")
                roots.append(context_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if not hasattr(self, "_slot_call_result_sinks"):
                    self._slot_call_result_sinks = []
                self._slot_call_result_sinks.append((ctx_expr, context_root, False))
                try:
                    ctx_val = self._emit_expr(ctx_expr)
                finally:
                    _expr, _root, published = self._slot_call_result_sinks.pop()
                if ctx_val in getattr(self, "_cpy_values", ()):
                    if published:
                        raise L1CodegenError("context producer mixed native and CPython ownership")
                    self._release_slot_call_roots(tuple(roots))
                    roots = []
                elif published:
                    native_context = True
                else:
                    raise L1CodegenError("native context producer requires an output-slot handoff")
            else:
                ctx_val = self._emit_expr(ctx_expr)
                if ctx_val not in getattr(self, "_cpy_values", ()):
                    raise L1CodegenError("native context binding requires an authoritative source")
            if native_context:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                enter_root = self._new_slot_call_root("with.dynamic.enter")
                roots.append(enter_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_context_enter", (context_root,), result_slot=enter_root, span=stmt.span,
                )
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        if native_context:
            self._emit_native_context_body(stmt, context_root, enter_root)
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
                raise NotImplementedError(
                    "Layer 1 with: as-clause must be a bare name"
                    f" at {stmt.span.file}:{stmt.span.line}:{stmt.span.col}"
                    f" (got {type(as_expr).__name__})"
                )
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

    def _emit_native_generator_context_with(self, stmt: With) -> bool:
        ctx_expr, _as_expr = stmt.items[0]
        if self._context_expr_generator_contextmanager(ctx_expr) is None:
            return False
        context_root, enter_root = emit_generator_context_enter(self, stmt)
        self._emit_native_context_body(
            stmt, context_root, enter_root, generator_context=True,
        )
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

        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        context_root = self._emit_slot_call_operand(ctx_expr, "with.user.manager")
        roots = [context_root]
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            enter_root = self._new_slot_call_root("with.user.enter")
            roots.append(enter_root)
            cleanup = self._slot_call_cleanup_block(tuple(roots), target)
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            token = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_acquire"], [self._as_gc_ptr(context_root)],
                name=self._fresh("with.user.manager.lease"),
            )
            self._slot_call_check_status(token, "context manager lease", stmt.span)
            self._try_err_block = self._slot_call_cleanup_block((), cleanup, ((context_root, token),))
            self._cpy_operand_cleanup_block = self._try_err_block
            if not hasattr(self, "_slot_call_result_sinks"):
                self._slot_call_result_sinks = []
            self._slot_call_result_sinks.append((None, enter_root, False))
            try:
                self._emit_direct_method_call(
                    enter_fn, self.builder.load(context_root), enter_info, "__enter__", (),
                    result_slot=enter_root,
                )
            finally:
                _expr, _root, published = self._slot_call_result_sinks.pop()
            if not published:
                raise L1CodegenError("native __enter__ producer requires an output-slot handoff")
            released = self.builder.call(
                self.runtime["pcc_gc_foreign_lease_release"], [self._as_gc_ptr(context_root), token],
                name=self._fresh("with.user.manager.release"),
            )
            self._try_err_block = cleanup
            self._cpy_operand_cleanup_block = cleanup
            self._slot_call_check_status(released, "context manager lease release", stmt.span)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        self._emit_native_context_body(stmt, context_root, enter_root)

        return True

    def _emit_native_context_body(
        self,
        stmt: With,
        context_root: ir.Value,
        enter_root: ir.Value,
        cleanup_runtime: Optional[str] = None,
        *,
        generator_context: bool = False,
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
        if self.current_func_def is None and getattr(self, "_class_namespace_context", None) is None:
            # Module root entry has already been emitted. Late context slots
            # need the same registration, guarded so loop iterations and
            # separate conditional bindings do not register the slot twice.
            module_targets = [context_target]
            if isinstance(as_expr, Name):
                module_targets.append(as_expr)
            for binding in module_targets:
                new_global = binding.ident not in self._module_globals
                slot, _declared = self._ensure_module_global_name(binding.ident, binding.ty)
                marker_name = self._module_global_symbol_name(
                    self.ast_module.name or "__main__", binding.ident + ".context.rooted",
                )
                marker = self.module.globals.get(marker_name)
                if not new_global and marker is None:
                    continue
                if marker is None:
                    marker = ir.GlobalVariable(self.module, _I1, name=marker_name)
                    marker.initializer = ir.Constant(_I1, 0)
                register = self.current_function.append_basic_block(self._fresh("with.module.root"))
                ready = self.current_function.append_basic_block(self._fresh("with.module.ready"))
                self.builder.cbranch(self.builder.load(marker), ready, register)
                self.builder.position_at_end(register)
                self.builder.store(ir.Constant(_I1, 1), marker)
                self._emit_current_gc_frame_enter(self._gc_one_slot_frame_map(), slot)
                self.builder.branch(ready)
                self.builder.position_at_end(ready)
        # Producers must already own both values in registered slots. Even
        # when __enter__ returns its manager, these are independent owners.
        # Rebinding an as-target can release its previous value and collect;
        # the rooted handoff moves the current pointer after that callback.
        self._slot_call_root_record(context_root)
        self._slot_call_root_record(enter_root)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = (context_root, enter_root)
        try:
            self._try_err_block = self._slot_call_cleanup_block(roots, target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._store_unpack_root_target(context_target, context_root, context_target.ty)
            if as_expr is not None:
                if not isinstance(as_expr, Name):
                    raise NotImplementedError(
                        "Layer 1 native with: as-clause must be a bare name"
                        f" at {stmt.span.file}:{stmt.span.line}:{stmt.span.col}"
                        f" (got {type(as_expr).__name__})"
                    )
                prior = self.env.get(as_expr.ident)
                if (getattr(self, "_cpy_env_flags", {}).get(as_expr.ident, False)
                        or prior is not None and isinstance(prior[2], RawPointerType)):
                    raise L1CodegenError("native context target cannot reuse a foreign/raw pointer slot")
                self._store_unpack_root_target(as_expr, enter_root, as_expr.ty)
            self._release_slot_call_roots(roots)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

        def clear_context():
            self._store_unpack_target(context_target, self._emit_none_literal(), context_target.ty,
                                      value_is_owned=True)
            if self._generator_ctx_stack:
                generator = self._generator_ctx_stack[-1]
                index, _slot = generator["frame_slots"][context_name]
                self.builder.call(self._generator_frame_helper("set"),
                                  [self._generator_current_frame(generator), ir.Constant(_I64, index),
                                   self._emit_none_literal()])

        fn = self.current_function
        err_bb = fn.append_basic_block(name=self._fresh("with.err"))
        after_bb = fn.append_basic_block(name=self._fresh("with.after"))
        prev_err_block = getattr(self, "_try_err_block", None)
        def exit_normal():
            # Return/break/continue must run this same exit, just like an
            # enclosing finally. An exception from __exit__ belongs outside
            # this manager rather than re-entering its own exception path.
            if generator_context:
                emit_generator_context_exit(self, context_target, clear_context, False, stmt.span)
                self._emit_post_call_err_check(stmt.span)
                return
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
        exit_entry = ("pcc.finally.call", exit_normal, prev_err_block, len(self._return_cleanup_roots))
        outer_finallys = list(self._finally_stack)
        self._finally_stack = outer_finallys + [exit_entry]
        self._try_err_block = err_bb
        try:
            self._emit_stmts(stmt.body)
        finally:
            self._try_err_block = prev_err_block
            self._finally_stack = outer_finallys

        if not self._builder_block_is_terminated():
            self._emit_finally_entry(exit_entry)
            self.builder.branch(after_bb)

        self.builder.position_at_end(err_bb)
        if generator_context:
            suppress = emit_generator_context_exit(
                self, context_target, clear_context, True, stmt.span,
            )
            suppressed = self.builder.icmp_signed("!=", suppress, ir.Constant(_I64, 0))
            self.builder.cbranch(suppressed, after_bb, prev_err_block or self._ensure_fn_err_exit())
            self.builder.position_at_end(after_bb)
            return
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
                    f" at {stmt.span.file}:{stmt.span.line}:{stmt.span.col}"
                    f" (got {type(as_expr).__name__})"
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
