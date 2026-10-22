"""Exception and error-exit lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.ir.compat import ir
from pcc.ir.ir import (
    IRBuilder_can_inline_error_edge,
    IRBuilder_declare_inline_error_landing,
    IRBuilder_try_inline_error_edge,
)

from pcc.frontends.python.py_ast import (
    Assign,
    Attr,
    Call,
    DynType,
    Expr,
    For,
    If,
    Import,
    Name,
    NoneLit,
    Raise,
    SourceSpan,
    StrLit,
    StrType,
    Try,
    While,
    With,
)
from pcc.frontends.python.codegen.generator_lowering import emit_generator_terminal_frame_clear
from pcc.frontends.python.codegen.local_bound_lowering import local_bound_flag, mark_local_bound

from pcc.frontends.python.codegen.builtin_exceptions import BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG, builtin_exc_tag_or_missing as _builtin_exc_tag_or_missing

_I8 = ir.IntType(8)
_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_OPTIONAL_IMPORT_MISSING_NONE = "pcc.optional_import_missing.None"




class ExceptionLoweringMixin:
    def _begin_handled_exception_scope(self, exception_slot):
        node = self._alloca_in_entry(
            ir.ArrayType(_CSTR, 2), name=self._fresh("handled.context"),
        )
        entry = (self.current_function, node, exception_slot)
        self.builder.call(
            self.runtime["py_handled_context_push"],
            [self._as_gc_ptr(node), self._as_gc_ptr(exception_slot)],
        )
        self._handled_exception_scopes = list(self._handled_exception_scopes) + [entry]
        return entry

    def _emit_handled_exception_scope_exit(self, entry, clear_root: bool = True, binding_slot=None, binding_name=None):
        active = self.builder.call(
            self.runtime["py_handled_context_pop"], [self._as_gc_ptr(entry[1])],
            name=self._fresh("handled.context.popped"),
        )
        if not clear_root:
            return
        if self.current_func_def is None:
            # Module roots use explicit LIFO registration. Their lexical exits
            # must publish identical root-stack state on every CFG edge.
            if binding_name is not None:
                mark_local_bound(self, binding_name, False)
            if binding_slot is not None:
                self._clear_exception_selection_owner_slot(binding_slot)
            self._clear_exception_selection_owner_slot(entry[2])
            return
        clear = self.current_function.append_basic_block(name=self._fresh("handled.context.clear"))
        done = self.current_function.append_basic_block(name=self._fresh("handled.context.done"))
        self.builder.cbranch(
            self.builder.icmp_signed("!=", active, ir.Constant(_I64, 0)), clear, done,
        )
        self.builder.position_at_end(clear)
        if binding_name is not None:
            mark_local_bound(self, binding_name, False)
        if binding_slot is not None:
            self._clear_exception_selection_owner_slot(binding_slot)
        self._clear_exception_selection_owner_slot(entry[2])
        self.builder.branch(done)
        self.builder.position_at_end(done)

    def _emit_suspend_handled_exception_scopes(self):
        for entry in reversed(self._handled_exception_scopes):
            if entry[0] is self.current_function:
                self._emit_handled_exception_scope_exit(entry, False)

    def _emit_resume_handled_exception_scopes(self):
        for entry in self._handled_exception_scopes:
            if entry[0] is not self.current_function:
                continue
            value = self.builder.load(entry[2], name=self._fresh("handled.context.resume.value"))
            enter = self.current_function.append_basic_block(name=self._fresh("handled.context.resume"))
            done = self.current_function.append_basic_block(name=self._fresh("handled.context.resume.done"))
            self.builder.cbranch(
                self.builder.icmp_unsigned("!=", value, ir.Constant(_CSTR, None)), enter, done,
            )
            self.builder.position_at_end(enter)
            self.builder.call(
                self.runtime["py_handled_context_push"],
                [self._as_gc_ptr(entry[1]), self._as_gc_ptr(entry[2])],
            )
            self.builder.branch(done)
            self.builder.position_at_end(done)

    def _emit_chain_pending_handled_exception(self):
        # Incoming throw/close TLS is chained only after generator-owned
        # handled scopes have been reinstalled from their managed frame slots.
        context_slot = self.builder.call(self.runtime["py_handled_exception_slot"], [])
        pending = self.builder.call(self.runtime["py_current_exception"], [])
        pending_slot = self._exception_selection_owner_slot(pending, "handled.resume.pending")
        self.builder.call(
            self.runtime["py_exc_set_implicit_context_slots"],
            [self._as_gc_ptr(pending_slot), context_slot],
        )
        self._clear_exception_selection_owner_slot(pending_slot)

    def _active_handler_exception_for_current_function(self):
        """Return the innermost handler exception owned by this IR function.

        ``L1CodeGen`` is re-entrant while it emits nested functions and class
        methods. Keep an explicit owner beside every handler value so a stale
        outer entry can never cross an LLVM function boundary, including on
        compiled-stage fixed-layout mixin calls.
        """
        active_stack = self._active_handler_excs
        if not active_stack:
            return None
        entry = active_stack[-1]
        if not isinstance(entry, tuple) or len(entry) not in (2, 3):
            return None
        owner, active = entry[0], entry[1]
        if owner is not self.current_function:
            return None
        if len(entry) == 3:
            # A parked handler resumes through a fresh function entry. Read
            # its original exception from the saved, collector-visible slot.
            return self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(
                    entry[2], name=self._fresh("handler.exception.root")
                )],
                name=self._fresh("handler.exception.active"),
            )
        return active

    def _push_try_err_block(self, err_bb):
        prev = self._try_err_block
        self._try_err_block = err_bb
        return prev

    def _restore_try_err_block(self, prev_err_block) -> None:
        self._try_err_block = prev_err_block

    def _current_try_err_block(self):
        return self._try_err_block

    def _emit_exceptional_finally(
        self,
        stmt: Try,
        current_exc: ir.Value,
        outer_err_block,
    ) -> bool:
        """Run exceptional cleanup with the original exception rooted.

        Cleanup must run with clear TLS: nested handlers may clear their own
        exception, and ordinary call checks must not see the unwinding one.
        Generators need a heap-frame slot across suspension; ordinary functions
        use the existing owned-local root and function-exit cleanup contract.
        """
        if not stmt.finally_body:
            return False
        active_roots = []
        if self._generator_ctx_stack:
            ctx = self._generator_ctx_stack[-1]
            hidden = self._generator_finally_exception_name(stmt)
            frame_entry = ctx["frame_slots"].get(hidden)
            if frame_entry is None:
                return False
            exception_slot = frame_entry[1]
            active_roots = ctx["active_exception_unwind_roots"]
        else:
            exception_slot = self._exception_selection_owner_slot(current_exc, "finally.exception")
        root_ptr = self._as_gc_ptr(
            exception_slot,
            name=self._fresh("finally.exception.root"),
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [root_ptr, current_exc],
        )
        scope = self._begin_handled_exception_scope(exception_slot)
        self.builder.call(self.runtime["py_clear_exception"], [])

        cleanup_error_bb = self.current_function.append_basic_block(
            name=self._fresh("finally.cleanup.error")
        )
        saved_err_block = self._push_try_err_block(cleanup_error_bb)
        saved_active_excs = self._active_handler_excs
        self._active_handler_excs = list(saved_active_excs) + [
            (self.current_function, current_exc, exception_slot)
        ]
        active_roots.append(root_ptr)
        def leave_finally_scope():
            self._emit_handled_exception_scope_exit(scope)
        saved_finally = self._finally_stack
        self._finally_stack = list(saved_finally) + [leave_finally_scope]
        try:
            self._emit_stmts(stmt.finally_body)
        finally:
            self._finally_stack = saved_finally
            self._handled_exception_scopes = self._handled_exception_scopes[:-1]
            active_roots.pop()
            self._active_handler_excs = saved_active_excs
            self._restore_try_err_block(saved_err_block)

        if not self._builder_block_is_terminated():
            # Re-derive the slot pointer here: in a may_park state machine
            # the finally body may have resumed into blocks the try-entry
            # cast does not dominate.
            restore_root_ptr = self._as_gc_ptr(
                exception_slot,
                name=self._fresh("finally.exception.root"),
            )
            saved_exc = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), restore_root_ptr],
                name=self._fresh("finally.exception.restore"),
            )
            self.builder.call(self.runtime["py_raise"], [saved_exc])
            self._emit_handled_exception_scope_exit(scope)
            self.builder.branch(outer_err_block)

        self.builder.position_at_end(cleanup_error_bb)
        cleanup_root_ptr = self._as_gc_ptr(
            exception_slot,
            name=self._fresh("finally.exception.root"),
        )
        cleanup_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("finally.cleanup.exception"),
        )
        replacement_slot = self._exception_selection_owner_slot(cleanup_exc, "finally.cleanup")
        saved_exc = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), cleanup_root_ptr],
            name=self._fresh("finally.original.exception"),
        )
        cleanup_nonnull = self.builder.icmp_unsigned(
            "!=",
            cleanup_exc,
            ir.Constant(_CSTR, None),
            name=self._fresh("finally.cleanup.nonnull"),
        )
        saved_nonnull = self.builder.icmp_unsigned(
            "!=",
            saved_exc,
            ir.Constant(_CSTR, None),
            name=self._fresh("finally.original.nonnull"),
        )
        distinct = self.builder.icmp_unsigned(
            "!=",
            cleanup_exc,
            saved_exc,
            name=self._fresh("finally.exception.distinct"),
        )
        set_context = self.builder.and_(
            self.builder.and_(cleanup_nonnull, saved_nonnull),
            distinct,
            name=self._fresh("finally.exception.set_context"),
        )
        context_bb = self.current_function.append_basic_block(
            name=self._fresh("finally.exception.context")
        )
        release_bb = self.current_function.append_basic_block(
            name=self._fresh("finally.exception.release")
        )
        self.builder.cbranch(set_context, context_bb, release_bb)
        self.builder.position_at_end(context_bb)
        self.builder.call(
            self.runtime["py_exc_set_implicit_context_slots"],
            [self._as_gc_ptr(replacement_slot), self._as_gc_ptr(exception_slot)],
        )
        self.builder.branch(release_bb)
        self.builder.position_at_end(release_bb)
        self._clear_exception_selection_owner_slot(replacement_slot)
        self._emit_handled_exception_scope_exit(scope)
        self.builder.branch(outer_err_block)
        return True

    def _maybe_emit_optional_missing_import_try(self, stmt: Try) -> bool:
        if stmt.else_body or stmt.finally_body:
            return False
        if len(stmt.handlers) != 1:
            return False
        handler = stmt.handlers[0]
        exc_type = handler.exc_type
        if not isinstance(exc_type, Name) or exc_type.ident != "ImportError":
            return False
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], Import):
            return False
        assigned_none = {}
        for item in handler.body:
            if not isinstance(item, Assign):
                return False
            if not isinstance(item.value, NoneLit):
                return False
            for target in item.targets:
                if not isinstance(target, Name):
                    return False
                assigned_none[target.ident] = True

        native_table = self._native_module_exports
        missing_locals = []
        for mod_name, as_name in stmt.body[0].names:
            if self._is_test_facade_import_module(mod_name):
                continue
            if (
                mod_name == "typing"
                or mod_name.split(".")[0] in ("__future__", "typing", "abc", "click")
                or mod_name == "pcc.extern"
            ):
                return False
            if mod_name in (
                "builtins",
                "sys",
                "os",
                "time",
                "string",
                "platform",
                "subprocess",
                "tempfile",
                "shutil",
                "shlex",
                "math",
                "json",
                "re",
                "gc",
                "weakref",
                "copy",
                "pickle",
                "threading",
                "pcc.virtual_thread",
                "pcc",
                "inspect",
                "contextlib",
                "warnings",
            ):
                return False
            if native_table is not None and mod_name in native_table:
                return False
            if self._resolve_pcc_native_extension_path(mod_name) is not None:
                return False
            local_name = as_name or mod_name.split(".")[0]
            if local_name not in assigned_none:
                return False
            missing_locals.append(local_name)
        if not missing_locals:
            return False
        for local_name in missing_locals:
            self._register_native_builtin_value_alias(
                local_name,
                _OPTIONAL_IMPORT_MISSING_NONE,
            )
            self._native_module_object_aliases.pop(local_name, None)
            self.env.pop(local_name, None)
            self.env_class_hint.pop(local_name, None)
            self.env_class_object_hint.pop(local_name, None)
            self._cpy_env_flags.pop(local_name, None)
            self._weak_dict_env_flags.pop(local_name, None)
            self._weakref_env_flags.pop(local_name, None)
        return True

    def _emit_raise(self, stmt: Raise) -> None:
        """Lower `raise`: set the TLS exception slot via py_raise,
        then branch to the active error-handler block (either a
        surrounding try's err block or this function's err-exit
        epilogue). No unwinder, no Itanium ABI.
        """
        if stmt.exc is None:
            # Bare ``raise`` inside a handler: py_raise with the
            # currently-pending exception.
            cur = self.builder.call(
                self.runtime["py_current_exception"],
                [],
                name=self._fresh("reraise.exc"),
            )
            active = self._active_handler_exception_for_current_function()
            if active is not None:
                null = ir.Constant(_CSTR, None)
                has_cur = self.builder.icmp_signed(
                    "!=",
                    cur,
                    null,
                    name=self._fresh("reraise.has_cur"),
                )
                cur = self.builder.select(
                    has_cur,
                    cur,
                    active,
                    name=self._fresh("reraise.active"),
                )
            self.builder.call(self.runtime["py_raise"], [cur])
        else:
            exc_val = self._build_exception_value(stmt.exc)
            # py_raise applies dynamic, cycle-safe implicit chaining. This
            # includes helpers called from another function's active handler.
            if stmt.cause is not None:
                cause_val = self._emit_expr(stmt.cause)
                self.builder.call(
                    self.runtime["py_exc_set_cause"], [exc_val, cause_val]
                )
                if self._raise_value_expr_returns_owned_object(stmt.cause):
                    self._gc_release(
                        cause_val, self._release_expr_label("owned", stmt.cause)
                    )
            self.builder.call(self.runtime["py_raise"], [exc_val])
            if self._raise_value_expr_returns_owned_object(stmt.exc):
                self._gc_release(exc_val, self._release_expr_label("owned", stmt.exc))

        frame_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("raise.frame.exc"),
        )
        self._emit_exception_frame(frame_exc, stmt.span)

        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

    def _emit_try(self, stmt: Try) -> None:
        if stmt.handlers and stmt.finally_body:
            # A finally owns every exit from the try/except, including a
            # throw/close injected while a generator is paused in a handler.
            # Preserve spans used by the preplanned generator frame slots.
            handled = Try(
                span=stmt.span,
                body=stmt.body,
                handlers=stmt.handlers,
                else_body=stmt.else_body,
                finally_body=(),
            )
            self._emit_try(Try(
                span=stmt.span,
                body=(handled,),
                handlers=(),
                else_body=(),
                finally_body=stmt.finally_body,
            ))
            return
        if self._maybe_emit_optional_missing_import_try(stmt):
            return
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))

        def try_log(label: str) -> None:
            if not debug_codegen:
                return
            mod_name = self.ast_module.name or "<module>"
            func_name = (
                self.current_func_def.name
                if self.current_func_def is not None
                else "<top>"
            )
            sys.stderr.write(
                "[pcc.frontends.c.codegen] " + mod_name + ":" + func_name + ":try " + label + "\n"
            )

        try_log("begin")
        if not stmt.handlers and not stmt.finally_body:
            try_log("plain body begin")
            self._emit_stmts(stmt.body)
            try_log("plain body end")
            return

        fn = self.current_function
        try_log("blocks begin")
        done_bb = fn.append_basic_block(name=self._fresh("try.done"))
        err_bb = fn.append_basic_block(name=self._fresh("try.err"))
        try_log("blocks end")

        try_log("prev err begin")
        prev_err_block = self._push_try_err_block(err_bb)
        try_log("prev err end")

        try_log("finally stack begin")
        if stmt.finally_body:
            finally_stack = list(getattr(self, "_finally_stack", ()))
            # Return/break/continue execute this suite outside the owning
            # try's error handler; failures must not enter it a second time.
            finally_stack.append(("pcc.finally.body", stmt.finally_body, prev_err_block, len(self._return_cleanup_roots)))
            self._finally_stack = finally_stack
        try_log("finally stack end")
        try:
            try_log("body begin")
            self._emit_stmts(stmt.body)
            try_log("body end")
        finally:
            if stmt.finally_body:
                finally_stack = list(getattr(self, "_finally_stack", ()))
                if finally_stack:
                    finally_stack.pop()
                self._finally_stack = finally_stack

        # Else and normal finally are outside this try's protected body.
        self._restore_try_err_block(prev_err_block)
        try_log("normal exit begin")
        if not self._builder_block_is_terminated():
            if stmt.else_body:
                self._emit_stmts(stmt.else_body)
            if not self._builder_block_is_terminated():
                if stmt.finally_body:
                    self._emit_stmts(stmt.finally_body)
                if not self._builder_block_is_terminated():
                    self.builder.branch(done_bb)
        try_log("normal exit end")

        try_log("restore err begin")
        self._restore_try_err_block(prev_err_block)
        try_log("restore err end")

        try_log("dispatch begin")
        self.builder.position_at_end(err_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("try.cur_exc"),
        )

        if not stmt.handlers:
            outer = prev_err_block or self._ensure_fn_err_exit()
            emitted_rooted_finally = self._emit_exceptional_finally(
                stmt,
                current_exc,
                outer,
            )
            if not emitted_rooted_finally:
                if stmt.finally_body:
                    self._emit_stmts(stmt.finally_body)
            if not self._builder_block_is_terminated():
                self.builder.branch(outer)
            self.builder.position_at_end(done_bb)
            try_log("dispatch end no handlers")
            return

        def body_has_raise(stmts: tuple, *, bare_only: bool) -> bool:
            for item in stmts:
                if isinstance(item, Raise):
                    if not bare_only or item.exc is None:
                        return True
                    continue
                if isinstance(item, If):
                    if body_has_raise(item.body, bare_only=bare_only) or body_has_raise(
                        item.else_body, bare_only=bare_only
                    ):
                        return True
                    continue
                if isinstance(item, While):
                    if body_has_raise(item.body, bare_only=bare_only) or body_has_raise(
                        item.else_body, bare_only=bare_only
                    ):
                        return True
                    continue
                if isinstance(item, For):
                    if body_has_raise(item.body, bare_only=bare_only) or body_has_raise(
                        item.else_body, bare_only=bare_only
                    ):
                        return True
                    continue
                if isinstance(item, Try):
                    if (
                        body_has_raise(item.body, bare_only=bare_only)
                        or body_has_raise(item.else_body, bare_only=bare_only)
                        or body_has_raise(item.finally_body, bare_only=bare_only)
                    ):
                        return True
                    for nested_handler in item.handlers:
                        if body_has_raise(nested_handler.body, bare_only=bare_only):
                            return True
                    continue
                if isinstance(item, With):
                    if body_has_raise(item.body, bare_only=bare_only):
                        return True
            return False

        for i, h in enumerate(stmt.handlers):
            try_log("handler begin")
            test_bb = fn.append_basic_block(name=self._fresh(f"except.test.{i}"))
            body_bb = fn.append_basic_block(name=self._fresh(f"except.body.{i}"))
            if not self._builder_block_is_terminated():
                self.builder.branch(test_bb)
            self.builder.position_at_end(test_bb)

            if h.exc_type is None:
                cond = ir.Constant(_I1, 1)
            else:
                selection_slot = None
                if self._generator_ctx_stack:
                    hidden = self._generator_handler_exception_name(stmt, i)
                    selection_slot = self._generator_ctx_stack[-1]["frame_slots"][hidden][1]
                match_i32 = self._emit_exception_class_match(h.exc_type, selection_slot)
                cond = self.builder.icmp_signed(
                    "!=",
                    match_i32,
                    ir.Constant(_I64, 0),
                    name=self._fresh("exc.matches.i1"),
                )

            if i + 1 < len(stmt.handlers):
                next_test_bb = fn.append_basic_block(
                    name=self._fresh(f"except.next.{i + 1}")
                )
                self.builder.cbranch(cond, body_bb, next_test_bb)
            else:
                propagate_bb = fn.append_basic_block(
                    name=self._fresh("except.propagate")
                )
                self.builder.cbranch(cond, body_bb, propagate_bb)
                next_test_bb = None
                self.builder.position_at_end(propagate_bb)
                current_exc = self.builder.call(
                    self.runtime["py_current_exception"], [],
                    name=self._fresh("except.unmatched.exception"),
                )
                # No handler matched: the finally block must STILL run before
                # the exception propagates to the outer handler (Python
                # guarantees finally always executes). Mirrors the no-handlers
                # path above; without this the finally was silently skipped on
                # the unmatched-exception path.
                outer = prev_err_block or self._ensure_fn_err_exit()
                emitted_rooted_finally = (
                    self._emit_exceptional_finally(
                        stmt,
                        current_exc,
                        outer,
                    )
                )
                if not emitted_rooted_finally:
                    if stmt.finally_body:
                        self._emit_stmts(stmt.finally_body)
                if not self._builder_block_is_terminated():
                    self.builder.branch(outer)

            self.builder.position_at_end(body_bb)
            handler_exc = self.builder.call(
                self.runtime["py_current_exception"], [],
                name=self._fresh("except.handler.exception"),
            )
            # Every handler owns dynamic handled state: a called helper can
            # bare-raise or construct an implicitly chained exception even if
            # there is no syntactic raise or exception binding in this body.
            if self._generator_ctx_stack:
                ctx = self._generator_ctx_stack[-1]
                hidden = self._generator_handler_exception_name(stmt, i)
                handler_slot = ctx["frame_slots"][hidden][1]
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(handler_slot), handler_exc],
                )
            else:
                handler_slot = self._exception_selection_owner_slot(handler_exc, "except.handler")
            scope = self._begin_handled_exception_scope(handler_slot)
            self.builder.call(self.runtime["py_clear_exception"], [])
            binding_slot = None
            if h.name is not None:
                handler_exc = self.builder.call(
                    self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(handler_slot)],
                    name=self._fresh("except.binding.value"),
                )
                if self._generator_ctx_stack:
                    binding_slot = self._generator_ctx_stack[-1]["frame_slots"][h.name][1]
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [self._as_gc_ptr(binding_slot), handler_exc],
                    )
                elif (self.current_func_def is None
                      and getattr(self, "_class_namespace_context", None) is None):
                    binding_slot, binding_ty = self._module_globals[h.name]
                    self._store_module_global_root_value(
                        binding_slot, handler_exc, declared_ty=binding_ty,
                    )
                    self._publish_module_global_assignment(h.name, handler_exc, binding_ty)
                else:
                    binding_slot = self._exception_selection_owner_slot(
                        handler_exc, "except.binding", h.name,
                    )
                self.env[h.name] = (binding_slot, _CSTR, DynType(name="dyn"))
                mark_local_bound(self, h.name)
                binding_names = getattr(self, "_except_binding_names", None)
                if binding_names is not None:
                    binding_names.add(h.name)
            saved_active = self._active_handler_excs
            self._active_handler_excs = list(saved_active) + [
                (self.current_function, handler_exc, handler_slot)
            ]
            binding_name = h.name
            def leave_handler():
                self._emit_handled_exception_scope_exit(
                    scope, True, binding_slot, binding_name,
                )
            saved_finally = self._finally_stack
            self._finally_stack = list(saved_finally) + [leave_handler]
            handler_error = fn.append_basic_block(name=self._fresh("except.handler.error"))
            saved_handler_error = self._push_try_err_block(handler_error)
            try:
                self._emit_stmts(h.body)
            finally:
                self._finally_stack = saved_finally
                self._restore_try_err_block(saved_handler_error)
                self._active_handler_excs = saved_active
                self._handled_exception_scopes = self._handled_exception_scopes[:-1]
            if not self._builder_block_is_terminated():
                leave_handler()
                self.builder.branch(done_bb)
            self.builder.position_at_end(handler_error)
            leave_handler()
            self.builder.branch(saved_handler_error or self._ensure_fn_err_exit())

            if next_test_bb is not None:
                self.builder.position_at_end(next_test_bb)
            try_log("handler end")

        self.builder.position_at_end(done_bb)
        try_log("end")

    def _emit_builtin_exception_and_branch(
        self,
        exc_name: str,
        message: str,
        span: Optional[SourceSpan],
        *,
        open_dead_continuation: bool = False,
    ) -> None:
        tag = _BUILTIN_EXC_TAG[exc_name]
        msg_ptr = self._pooled_cstr_ptr(message, ".exc.msg")
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, tag), msg_ptr],
            name=self._fresh(f"exc.{exc_name}"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        frame_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("raise.frame.exc"),
        )
        self._emit_exception_frame(frame_exc, span)
        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        cont = self.current_function.append_basic_block(
            name=self._fresh("raise.cont"),
        )
        self.builder.position_at_end(cont)
        if not open_dead_continuation:
            self.builder.unreachable()

    def _emit_attribute_error_if_null(
        self,
        value: ir.Value,
        attr_name: str,
        span: Optional[SourceSpan],
    ) -> None:
        missing = self.builder.icmp_signed(
            "==",
            value,
            ir.Constant(_CSTR, None),
            name=self._fresh(f"attr.{attr_name}.missing"),
        )
        err_bb = self.current_function.append_basic_block(
            name=self._fresh(f"attr.{attr_name}.err"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh(f"attr.{attr_name}.ok"),
        )
        self.builder.cbranch(missing, err_bb, ok_bb)
        self.builder.position_at_end(err_bb)
        pending = self.builder.call(
            self.runtime["py_err_occurred"],
            [],
            name=self._fresh(f"attr.{attr_name}.pending"),
        )
        has_pending = self.builder.icmp_signed(
            "!=",
            pending,
            ir.Constant(_I64, 0),
            name=self._fresh(f"attr.{attr_name}.has_pending"),
        )
        propagate_bb = self.current_function.append_basic_block(
            name=self._fresh(f"attr.{attr_name}.propagate"),
        )
        raise_bb = self.current_function.append_basic_block(
            name=self._fresh(f"attr.{attr_name}.raise"),
        )
        self.builder.cbranch(has_pending, propagate_bb, raise_bb)

        self.builder.position_at_end(propagate_bb)
        frame_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("attr.frame.exc"),
        )
        self._emit_exception_frame(frame_exc, span)
        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(raise_bb)
        self._emit_builtin_exception_and_branch(
            "AttributeError",
            f"object has no attribute {attr_name}",
            span,
        )
        self.builder.position_at_end(ok_bb)

    def _emit_attribute_error_if_status_failed(
        self,
        status: ir.Value,
        attr_name: str,
        span: Optional[SourceSpan],
    ) -> None:
        failed = self.builder.icmp_signed(
            "<",
            status,
            ir.Constant(_I64, 0),
            name=self._fresh(f"setattr.{attr_name}.failed"),
        )
        err_bb = self.current_function.append_basic_block(
            name=self._fresh(f"setattr.{attr_name}.err"),
        )
        ok_bb = self.current_function.append_basic_block(
            name=self._fresh(f"setattr.{attr_name}.ok"),
        )
        self.builder.cbranch(failed, err_bb, ok_bb)
        self.builder.position_at_end(err_bb)
        pending = self.builder.call(
            self.runtime["py_err_occurred"],
            [],
            name=self._fresh(f"setattr.{attr_name}.pending"),
        )
        has_pending = self.builder.icmp_signed(
            "!=",
            pending,
            ir.Constant(_I64, 0),
            name=self._fresh(f"setattr.{attr_name}.has_pending"),
        )
        propagate_bb = self.current_function.append_basic_block(
            name=self._fresh(f"setattr.{attr_name}.propagate"),
        )
        raise_bb = self.current_function.append_basic_block(
            name=self._fresh(f"setattr.{attr_name}.raise"),
        )
        self.builder.cbranch(has_pending, propagate_bb, raise_bb)

        self.builder.position_at_end(propagate_bb)
        frame_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("attr.status.frame.exc"),
        )
        self._emit_exception_frame(frame_exc, span)
        err_target = self._current_try_err_block()
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)

        self.builder.position_at_end(raise_bb)
        self._emit_builtin_exception_and_branch(
            "AttributeError",
            f"object has no attribute {attr_name}",
            span,
        )
        self.builder.position_at_end(ok_bb)

    def _build_exception_value(self, exc_expr: Expr) -> ir.Value:
        """Lower a ``raise`` operand expression to a ``PyObject*``.

        Supports ``ExceptionClass("msg")`` / ``ExceptionClass()`` for
        builtin exception names, plus a bare ``Name`` for re-raising an
        already-bound exception.
        """

        def _message_cstr(
            args: tuple[Expr, ...],
            kwargs: tuple[tuple[str, Expr], ...] = (),
        ) -> ir.Value:
            msg_expr: Optional[Expr] = None
            if args:
                msg_expr = args[0]
            else:
                for key, value in kwargs:
                    if key == "message" or key == "msg":
                        msg_expr = value
                        break
            if msg_expr is None:
                return ir.Constant(_CSTR, None)
            first = msg_expr
            if isinstance(first, StrLit):
                return self._pooled_cstr_ptr(first.value, ".exc.msg")
            # direct valueclass constructors project to boxed valueboxes
            # (consistent allocation model; str() output is identical)
            msg_obj = self._emit_expr_as_pcc_object(first)
            msg_str = msg_obj
            if not isinstance(first.ty, StrType):
                msg_str = self.builder.call(
                    self.runtime["py_obj_str"],
                    [msg_obj],
                    name=self._fresh("exc.msg.obj"),
                )
            return self.builder.call(
                self.runtime["py_str_utf8"],
                [msg_str],
                name=self._fresh("exc.msg.cstr"),
            )

        if isinstance(exc_expr, Call) and isinstance(exc_expr.func, Name):
            cls_name = exc_expr.func.ident
            tag = _builtin_exc_tag_or_missing(cls_name)
            if tag == 14 or (34 <= tag <= 48):
                # OSError and every builtin subclass have positional metadata.
                # The ordinary slot-call route preserves every operand and
                # shares construction with aliases and first-class classes.
                return self._emit_slot_call_object(exc_expr, "exc.os")
            if tag == 58 or tag == 59:  # Decode/encode errors have five-field constructors.
                cls = self.builder.call(
                    self.runtime["py_exc_builtin_class"], [ir.Constant(_I64, tag)],
                    name=self._fresh("exc.unicode.class"),
                )
                roots = [self._extern_enter_root(cls, False, "exc.unicode.class")]
                previous = self._current_try_err_block()
                target = previous if previous is not None else self._ensure_fn_err_exit()
                saved_error = self._push_try_err_block(
                    self._extern_cleanup_block(tuple(roots), target),
                )
                saved_cpy_error = self._cpy_operand_cleanup_block
                self._cpy_operand_cleanup_block = self._current_try_err_block()
                try:
                    unpack = self._split_starstar_kwargs_unpack(exc_expr.args)
                    args = exc_expr.args
                    kwargs_expr = None
                    if unpack is not None:
                        args, kwargs_expr = unpack
                    args_tuple = self._emit_dynamic_call_args_tuple(args)
                    args_root = self._extern_enter_root(args_tuple, True, "exc.unicode.args")
                    roots.append(args_root)
                    self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    kwargs_obj = self._emit_dynamic_call_kwargs_object(
                        exc_expr.kwargs, kwargs_expr, self._expr_span_or_none(exc_expr),
                    )
                    kwargs_root = self._extern_enter_root(
                        kwargs_obj, bool(exc_expr.kwargs) or kwargs_expr is not None,
                        "exc.unicode.kwargs",
                    )
                    roots.append(kwargs_root)
                    self._try_err_block = self._extern_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    result = self.builder.call(
                        self.runtime["py_obj_call"],
                        [self._extern_load_root(roots[0]), self._extern_load_root(args_root),
                         self._extern_load_root(kwargs_root)],
                        name=self._fresh("exc.unicode"),
                    )
                    self._emit_post_call_err_check(self._expr_span_or_none(exc_expr))
                    result_root = self._extern_enter_root(result, True, "exc.unicode.result")
                finally:
                    self._restore_try_err_block(saved_error)
                    self._cpy_operand_cleanup_block = saved_cpy_error
                self._extern_release_roots(tuple(roots))
                result = self._extern_take_root(result_root)
                self._note_owned_dynamic_call_value(result)
                return result
            if tag >= 0:
                sink = self._slot_call_result_sink(exc_expr)
                if (sink is not None and not exc_expr.kwargs
                        and len(exc_expr.args) <= 1
                        and not self._has_starred_unpack(exc_expr.args)
                        and self._split_starstar_kwargs_unpack(exc_expr.args) is None):
                    # Publish this known NEW producer into its caller's root.
                    # Keep dispatch in the exception builder: routing through
                    # class-call construction has a different argument ABI.
                    previous = self._current_try_err_block()
                    target = previous if previous is not None else self._ensure_fn_err_exit()
                    saved_cpy = self._cpy_operand_cleanup_block
                    roots = []
                    try:
                        if exc_expr.args and not isinstance(exc_expr.args[0], StrLit):
                            argument = self._emit_slot_call_operand(
                                exc_expr.args[0], "exception.value.argument",
                            )
                            roots.append(argument)
                            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                            self._cpy_operand_cleanup_block = self._try_err_block
                            self._slot_call_runtime_call(
                                "py_exc_new_with_value", (argument,), result_slot=sink,
                                suffix_args=(ir.Constant(_I64, tag),), argument_order=(1, 0),
                                span=exc_expr.span,
                            )
                        else:
                            # Omitted and literal messages use static C strings,
                            # so there is no managed input crossing this call.
                            message = _message_cstr(exc_expr.args)
                            self._slot_call_runtime_call(
                                "py_exc_new", (), result_slot=sink,
                                suffix_args=(ir.Constant(_I64, tag), message), span=exc_expr.span,
                            )
                        self._release_slot_call_roots(tuple(roots))
                        return self.builder.load(sink, name=self._fresh("exception.value.current"))
                    finally:
                        self._try_err_block = previous
                        self._cpy_operand_cleanup_block = saved_cpy
                if (
                    len(exc_expr.args) == 1
                    and not exc_expr.kwargs
                    and not isinstance(exc_expr.args[0], StrLit)
                    and not self._is_starred_unpack_expr(exc_expr.args[0])
                ):
                    # The argument object itself is args[0]: ``SystemExit(3)``
                    # exits with 3 and ``ValueError(3).args == (3,)``; it
                    # used to be stringified into the message.
                    arg_obj = self._emit_expr_as_pcc_object(exc_expr.args[0])
                    result = self.builder.call(
                        self.runtime["py_exc_new_with_value"],
                        [ir.Constant(_I64, tag), arg_obj],
                        name=self._fresh(f"exc.{cls_name}"),
                    )
                else:
                    msg_ptr = _message_cstr(exc_expr.args, exc_expr.kwargs)
                    result = self.builder.call(
                        self.runtime["py_exc_new"],
                        [ir.Constant(_I64, tag), msg_ptr],
                        name=self._fresh(f"exc.{cls_name}"),
                    )
                if sink is not None:
                    self._publish_slot_call_owned(sink, result, label="builtin exception")
                    self._emit_post_call_err_check(exc_expr.span)
                    return self.builder.load(sink, name=self._fresh("exception.value.current"))
                self._note_owned_object_value(result)
                return result
            info = self.class_lowering.classes.get(cls_name)
            if info is not None and self.class_lowering._class_subclasses_exception(info):
                # The runtime class call keeps the positional arguments as
                # ``args`` before __init__ runs (BaseException.__new__) and
                # binds keywords to __init__, as constructing it anywhere else.
                return self._emit_slot_call_object(exc_expr, "raise.construct")
            if info is not None:
                # Construct the instance properly so the user __init__ runs
                # (sets instance attrs, and super().__init__ stores the
                # message/args), then raise that instance. The old
                # py_exc_new_with_class(cls, args[0]) captured only args[0] as a
                # message and skipped __init__, losing attributes and
                # mis-taking a non-message first arg as the message. kwargs are
                # rare for exceptions and stay on the message-only path.
                if not exc_expr.kwargs:
                    return self.class_lowering.emit_instantiate(
                        cls_name, exc_expr.args, self
                    )
                cls_val = self.builder.load(
                    info.global_var,
                    name=self._fresh(f"exc.ucls.{cls_name}"),
                )
                msg_ptr = _message_cstr(exc_expr.args, exc_expr.kwargs)
                return self.builder.call(
                    self.runtime["py_exc_new_with_class"],
                    [cls_val, msg_ptr],
                    name=self._fresh(f"exc.{cls_name}"),
                )
        # ``raise NotImplementedError`` (bare builtin exception name, no
        # call). Instantiate the exception with an empty message.
        if isinstance(exc_expr, Name) and exc_expr.ident not in self.env:
            cls_name = exc_expr.ident
            tag = _builtin_exc_tag_or_missing(cls_name)
            if tag == 59:
                # A bare class raise must still validate its constructor,
                # then propagate the resulting TypeError before py_raise.
                return self._build_exception_value(Call(
                    span=exc_expr.span, ty=DynType(name="dyn"), func=exc_expr,
                    args=(), kwargs=(),
                ))
            if tag >= 0:
                return self.builder.call(
                    self.runtime["py_exc_new"],
                    [ir.Constant(_I64, tag), _message_cstr(())],
                    name=self._fresh(f"exc.{cls_name}"),
                )
        if isinstance(exc_expr, Name) and exc_expr.ident not in self.env:
            cls_name = exc_expr.ident
            info = self.class_lowering.classes.get(cls_name)
            if info is None:
                return self._emit_as_object(exc_expr)
            cls_val = self.builder.load(
                info.global_var,
                name=self._fresh(f"exc.ucls.{cls_name}"),
            )
            return self.builder.call(
                self.runtime["py_exc_new_with_class"],
                [cls_val, _message_cstr(())],
                name=self._fresh(f"exc.{cls_name}"),
            )
        # Fallback: evaluate as an object (e.g. re-raising a bound var).
        return self._emit_as_object(exc_expr)

    def _raise_value_expr_returns_owned_object(self, exc_expr: Expr) -> bool:
        """Whether `_build_exception_value()` produced a new owned PyObject*.

        `py_raise()` retains the object into TLS. When the raise expression
        constructed an exception object just for the raise statement, the
        lowering must release that temporary after `py_raise()` or every caught
        exception remains at refcount 1 after `py_clear_exception()`.
        """
        if isinstance(exc_expr, Call) and isinstance(exc_expr.func, Name):
            cls_name = exc_expr.func.ident
            if _builtin_exc_tag_or_missing(cls_name) >= 0:
                return True
            if self.class_lowering.classes.get(cls_name) is not None:
                return True
        if isinstance(exc_expr, Name) and exc_expr.ident not in self.env:
            cls_name = exc_expr.ident
            if _builtin_exc_tag_or_missing(cls_name) >= 0:
                return True
            if self.class_lowering.classes.get(cls_name) is not None:
                return True
        return self._expr_returns_owned_object(exc_expr)

    def _emit_exception_class_ref(self, expr: Expr) -> ir.Value:
        """Resolve the actual class expression used by an except clause."""
        if isinstance(expr, Name):
            if expr.ident in self.env or expr.ident in self._module_globals:
                return self._emit_as_object(expr)
            tag = _builtin_exc_tag_or_missing(expr.ident)
            if tag >= 0:
                return self.builder.call(
                    self.runtime["py_exc_builtin_class"],
                    [ir.Constant(_I64, tag)],
                    name=self._fresh(f"exc.cls.{expr.ident}"),
                )
            # User class? Look up in class_lowering.
            info = self.class_lowering.classes.get(expr.ident)
            if info is not None:
                return self.builder.load(
                    info.global_var,
                    name=self._fresh(f"exc.ucls.{expr.ident}"),
                )
            # CPython-imported class (``from foo import FooError``).
            # The module env has the class as a ``py_cpy_import``-
            # rooted value; passing it to ``py_exc_matches`` makes the
            # runtime fall through to a type-identity compare. Works
            # for the common "catch a specific user exception" case
            # without requiring a pcc-side ClassInfo.
            cpy_gv = getattr(self, "_cpy_module_env", {}).get(expr.ident)
            if cpy_gv is not None:
                return self.builder.load(
                    cpy_gv,
                    name=self._fresh(f"exc.cpy.{expr.ident}"),
                )
            return self._emit_as_object(expr)
        # Closure conversion represents a captured name as a cell subscript.
        # Python also allows arbitrary expressions here, including calls and
        # indexed class selections. Use the normal expression implementation
        # so its diagnostics and returned-reference contract remain intact.
        return self._emit_as_object(expr)

    def _exception_selection_owner_slot(self, value: ir.Value, label: str, owner_name=None) -> ir.Value:
        """Keep selection state in a collector-visible, independently owned slot.

        The ordinary local-root protocol handles every exceptional function
        exit. Clearing the slot is the final use of its object, so cleanup never
        consumes an SSA pointer after a root-unregister or unpin safepoint.
        """
        hidden = owner_name or self._fresh(label + ".owner")
        slot = self._alloca_in_entry(_CSTR, name=self._fresh(label + ".slot"))
        self._store_entry_initializer(slot, ir.Constant(_CSTR, None))
        self.env[hidden] = (slot, _CSTR, DynType(name="dyn"))
        self._ensure_local_gc_frame_root(hidden, slot, _CSTR, self._gc_one_slot_frame_map())
        self._owned_local_names.add(hidden)
        self._owned_local_has_value.add(hidden)
        owned_flag = self._ensure_owned_local_flag(hidden, slot)
        self.builder.store(ir.Constant(_I1, 1), owned_flag)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [self._as_gc_ptr(slot), value],
        )
        if self.current_func_def is None:
            # Module initializers do not participate in local-root registration.
            # Their selection never suspends, so a scoped LIFO frame suffices.
            self._emit_current_gc_frame_enter_lifo(self._gc_one_slot_frame_map(), slot)
        return slot

    def _clear_exception_selection_owner_slot(self, slot: ir.Value) -> None:
        if self.current_func_def is None:
            for name, (global_slot, _declared_ty) in self._module_globals.items():
                if global_slot is not slot:
                    continue
                # Automatic except-as deletion is idempotent, including when
                # user code already deleted/rebound the name. Publish the
                # unbound state before releasing either namespace owner.
                flag = self._module_global_init_flags[name]
                self.builder.store(ir.Constant(_I1, 0), flag)
                module_name = self._pooled_cstr_ptr(
                    self.ast_module.name or "__main__", ".pcc.except.binding.module",
                )
                self.builder.call(
                    self.runtime["py_module_attr_del"],
                    [module_name, self._attr_name_ptr(name)],
                )
                value = self.builder.load(slot, name=self._fresh("except.global.current"))
                self._gc_unpin(value)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
                )
                return
        if self._generator_ctx_stack:
            ctx = self._generator_ctx_stack[-1]
            for _name, (index, saved_slot) in ctx["frame_slots"].items():
                if saved_slot is slot:
                    # A prior suspension left a second owner in the heap
                    # frame. Retire it before the activation owner so cleanup
                    # and finalizers are observable before the next yield.
                    frame = self.builder.call(
                        self.runtime["pcc_gc_load_ptr"],
                        [ir.Constant(_CSTR, None), self._as_gc_ptr(ctx["frame_root"])],
                        name=self._fresh("cleanup.saved.frame"),
                    )
                    self.builder.call(
                        self._generator_frame_helper("set"),
                        [frame, ir.Constant(_I64, index), self._emit_none_literal()],
                    )
                    break
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
        )
        if self.current_func_def is None:
            self._emit_gc_frame_leave_lifo_for_slot(slot)

    def _emit_exception_class_match(self, expr: Expr, frame_slot=None) -> ir.Value:
        """Evaluate and validate one complete except expression with clear TLS.

        The original exception survives class evaluation and its error edges.
        A generator uses the handler's preplanned frame slot so suspension saves
        the exception and a fresh resume activation can reload it. Ordinary
        functions use an owned local root with balanced exit cleanup.
        """
        original = self.builder.call(
            self.runtime["py_current_exception"], [],
            name=self._fresh("except.match.exception"),
        )
        root = frame_slot
        if root is None:
            root = self._exception_selection_owner_slot(original, "except.match")
        else:
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(root), original],
            )
        scope = self._begin_handled_exception_scope(root)
        self.builder.call(self.runtime["py_clear_exception"], [])
        error_bb = self.current_function.append_basic_block(
            name=self._fresh("except.class.error"),
        )
        saved_error = self._push_try_err_block(error_bb)
        saved_active = self._active_handler_excs
        self._active_handler_excs = list(saved_active) + [
            (self.current_function, original, root)
        ]
        try:
            # Evaluate the entire tuple before validation. Python rejects an
            # invalid later member even when an earlier member would match.
            cls_val = self._emit_exception_class_ref(expr)
            # Validation can allocate TypeError. Keep a returned tuple/class
            # rooted until that call and its cleanup have completed.
            cls_owned = self._owned_release_needed(cls_val, expr)
            cls_root = self._exception_selection_owner_slot(cls_val, "except.class")
            # Transfer the expression's owner while the root remains live.
            # Root registration/store may have relocated the returned object.
            cls_live = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(cls_root)],
                name=self._fresh("except.class.live"),
            )
            if cls_owned:
                self._gc_release(cls_live, self._release_expr_label("owned", expr))
            original = self._active_handler_exception_for_current_function()
            cls_live = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), self._as_gc_ptr(cls_root)],
                name=self._fresh("except.class.match"),
            )
            match = self.builder.call(
                self.runtime["py_exc_match_handler"], [original, cls_live],
                name=self._fresh("exc.matches"),
            )
            self._clear_exception_selection_owner_slot(cls_root)
            self._emit_post_call_err_check(expr.span)
        finally:
            self._restore_try_err_block(saved_error)
            self._active_handler_excs = saved_active
            self._handled_exception_scopes = self._handled_exception_scopes[:-1]
        original = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(root)],
            name=self._fresh("except.match.restore"),
        )
        self.builder.call(self.runtime["py_raise"], [original])
        self._emit_handled_exception_scope_exit(scope)
        continuation = self.builder._block

        self.builder.position_at_end(error_bb)
        replacement = self.builder.call(
            self.runtime["py_current_exception"], [],
            name=self._fresh("except.class.exception"),
        )
        replacement_slot = self._exception_selection_owner_slot(replacement, "except.class.replacement")
        original = self.builder.call(
            self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(root)],
            name=self._fresh("except.class.original"),
        )
        distinct = self.builder.icmp_unsigned(
            "!=", replacement, original,
            name=self._fresh("except.class.distinct"),
        )
        context_bb = self.current_function.append_basic_block(
            name=self._fresh("except.class.context"),
        )
        release_bb = self.current_function.append_basic_block(
            name=self._fresh("except.class.release"),
        )
        self.builder.cbranch(distinct, context_bb, release_bb)
        self.builder.position_at_end(context_bb)
        self.builder.call(
            self.runtime["py_exc_set_implicit_context_slots"],
            [self._as_gc_ptr(replacement_slot), self._as_gc_ptr(root)],
        )
        self.builder.branch(release_bb)
        self.builder.position_at_end(release_bb)
        self._clear_exception_selection_owner_slot(replacement_slot)
        self._emit_handled_exception_scope_exit(scope)
        self.builder.branch(saved_error or self._ensure_fn_err_exit())
        self.builder.position_at_end(continuation)
        return match

    def _traceback_source_text(self, span: SourceSpan) -> str:
        """The stripped source line a traceback frame prints for ``span``."""
        file_name = span.file or "<unknown>"
        source_line = ""
        if file_name and not file_name.startswith("<") and int(span.line) > 0:
            # Read and split each source file once.  Every traceback frame
            # needs one line of it, but re-reading and re-splitting the whole
            # module per frame allocated a fresh list of every source line
            # thousands of times per compile; `splitlines` alone was ~2% of a
            # self-hosted pcc1 run.  An unreadable file caches as empty, which
            # leaves `source_line` empty exactly as the OSError path did.
            source_lines = self._source_file_lines_cache.get(file_name)
            if source_lines is None:
                try:
                    with open(
                        file_name, "r", encoding="utf-8"
                    ) as source_stream:
                        source_lines = source_stream.read().splitlines()
                except OSError:
                    source_lines = []
                self._source_file_lines_cache[file_name] = source_lines
            source_index = int(span.line) - 1
            if source_index >= 0 and source_index < len(source_lines):
                source_line = source_lines[source_index].strip()
        return source_line

    def _traceback_index_for(self, span: SourceSpan) -> int:
        """Index of ``span``'s (line, source text) pair in the module tables."""
        file_name = span.file or "<unknown>"
        line = int(span.line)
        by_line = self._tb_index_by_file.get(file_name)
        if by_line is None:
            by_line = {}
            self._tb_index_by_file[file_name] = by_line
        existing = by_line.get(line)
        if existing is not None:
            return existing
        index = len(self._tb_index_lines)
        self._tb_index_lines.append(line)
        self._tb_index_sources.append(
            self._pooled_cstr_global(
                self._traceback_source_text(span), ".tb.source"
            )
        )
        by_line[line] = index
        return index

    def _direct_frame_landing(
        self,
        err_target: ir.Block,
        span: SourceSpan,
    ) -> tuple:
        """Shared traceback landing for ``err_target`` plus this site's payload.

        Direct/no-text functions keep one landing block per (function, error
        target) instead of one ``err.frame`` block per source line.  The
        landing reads the raise site's table index from an i32 entry slot that
        the inline edge's cold stub (or the explicit cleanup block) stored,
        then records exactly the frame the per-line block recorded.  Returns
        ``(landing_block, payload_slot, payload_index)``.
        """
        parent_fn = self.current_function
        by_target = self._direct_frame_landings.get(id(parent_fn))
        if by_target is None:
            by_target = {}
            self._direct_frame_landings[id(parent_fn)] = by_target
        landing = by_target.get(err_target.name)
        payload = self._traceback_index_for(span)
        if landing is not None:
            return (landing[0], landing[1], payload)
        if self._tb_lines_global is None:
            lines_global = ir.GlobalVariable(
                self.module, ir.ArrayType(_I32, 0), name=".pcc.tb.lines"
            )
            lines_global.linkage = "internal"
            lines_global.global_constant = True
            self._tb_lines_global = lines_global
            sources_global = ir.GlobalVariable(
                self.module, ir.ArrayType(_CSTR, 0), name=".pcc.tb.sources"
            )
            sources_global.linkage = "internal"
            sources_global.global_constant = True
            self._tb_sources_global = sources_global
        slot = self._alloca_in_entry(_I32, name=self._fresh("err.frame.index"))
        landing_bb = parent_fn.append_basic_block(
            name=self._fresh("err.frame.land")
        )
        by_target[err_target.name] = (landing_bb, slot)
        save_block = self.builder._block
        save_pos = self.builder._pos
        self.builder.position_at_end(landing_bb)
        index_value = self.builder.load(
            slot, name=self._fresh("err.frame.index.value")
        )
        exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("err.frame.exc"),
        )
        func_name = "<module>"
        if self.current_func_def is not None:
            func_name = self.current_func_def.name
        func_ptr = self._pooled_cstr_ptr(func_name, ".tb.func")
        file_ptr = self._pooled_cstr_ptr(span.file or "<unknown>", ".tb.file")
        self.builder.call(
            self.runtime["py_exc_append_frame_indexed"],
            [
                exc,
                func_ptr,
                file_ptr,
                self.builder.bitcast(
                    self._tb_lines_global,
                    _CSTR,
                    name=self._fresh("err.frame.lines"),
                ),
                self.builder.bitcast(
                    self._tb_sources_global,
                    _CSTR,
                    name=self._fresh("err.frame.sources"),
                ),
                index_value,
            ],
        )
        self.builder.branch(err_target)
        self.builder.position_at_end(save_block)
        self.builder._pos = save_pos
        IRBuilder_declare_inline_error_landing(self.builder, landing_bb, slot)
        return (landing_bb, slot, payload)

    def _finalize_traceback_index_tables(self) -> None:
        """Give the module traceback tables their final size and contents."""
        lines_global = self._tb_lines_global
        if lines_global is None:
            return
        count = len(self._tb_index_lines)
        lines_type = ir.ArrayType(_I32, count)
        lines_global.value_type = lines_type
        lines_global.type = ir.PointerType(lines_type)
        lines_global.initializer = ir.Constant(
            lines_type,
            [ir.Constant(_I32, value) for value in self._tb_index_lines],
        )
        sources_global = self._tb_sources_global
        sources_type = ir.ArrayType(_CSTR, count)
        sources_global.value_type = sources_type
        sources_global.type = ir.PointerType(sources_type)
        sources_global.initializer = ir.Constant(
            sources_type,
            [
                ir.Constant(_CSTR, source_global)
                for source_global in self._tb_index_sources
            ],
        )

    def _emit_exception_frame(
        self,
        exc: ir.Value,
        span: Optional[SourceSpan],
    ) -> None:
        if span is None:
            return
        func_name = "<module>"
        if self.current_func_def is not None:
            func_name = self.current_func_def.name
        func_ptr = self._pooled_cstr_ptr(func_name, ".tb.func")
        file_name = span.file or "<unknown>"
        file_ptr = self._pooled_cstr_ptr(file_name, ".tb.file")
        source_ptr = self._pooled_cstr_ptr(
            self._traceback_source_text(span), ".tb.source"
        )
        self.builder.call(
            self.runtime["py_exc_append_frame_source"],
            [
                exc,
                func_ptr,
                file_ptr,
                source_ptr,
                ir.Constant(_I32, int(span.line)),
            ],
        )

    def _emit_post_call_err_check(
        self,
        span: Optional[SourceSpan] = None,
        *,
        release_on_error: tuple[ir.Value, ...] = (),
        cpy_release_on_error: tuple[ir.Value, ...] = (),
        rooted_release_on_error: tuple[
            tuple[ir.Value, ir.Value], ...
        ] = (),
        pinned_release_on_error: tuple[tuple[ir.Value, bool], ...] = (),
        lifo_owned_root_slots_on_error: tuple[ir.Value, ...] = (),
    ) -> None:
        """After any call that could raise a Python exception, emit
        `if (py_err_occurred()) goto err_target` where err_target is
        the try's handler block or (fallback) the function's
        err-exit epilogue.

        Suppressed inside @c_abi_export-marked runtime functions: they
        may be invoked while TLS already holds a pending exception
        (e.g. inside the except-handler dispatch), and a spurious
        check would misinterpret that as "the internal helper raised".
        Runtime functions propagate errors via explicit NULL-return
        plus the caller's own check, matching the cc-C runtime.
        """
        cur_fn = self.current_function
        if cur_fn is not None and cur_fn.name in self._c_abi_export_symbols:
            return
        err_target = self._current_try_err_block()
        function_exit_edge = err_target is None
        if function_exit_edge:
            err_target = self._ensure_fn_err_exit()
        err_fn = self.runtime.get("py_err_occurred")
        if err_fn is None:
            # Declare lazily: i64 (void). i64 matches the pcc-Python
            # port's default `int` lowering so the runtime-abi table
            # and the Python-emitted function agree.
            err_ty = ir.FunctionType(_I64, [])
            err_fn = ir.Function(self.module, err_ty, name="py_err_occurred")
            err_fn.linkage = "external"
            self.runtime["py_err_occurred"] = err_fn
        is_err = self.builder.call(
            err_fn,
            [],
            name=self._fresh("err.flag"),
        )
        cmp = self.builder.icmp_signed(
            "!=",
            is_err,
            ir.Constant(_I64, 0),
            name=self._fresh("err.cmp"),
        )
        needs_cleanup = bool(
            release_on_error
            or cpy_release_on_error
            or rooted_release_on_error
            or pinned_release_on_error
            or lifo_owned_root_slots_on_error
        )
        parent_fn = self.current_function
        # Direct/no-text self-backend functions publish the check as one
        # inline exceptional edge: normal execution stays in the current
        # logical block, so no ``call.cont`` block exists.  Every error
        # destination reachable here (function exit, try handler, per-line
        # frame block, explicit cleanup block) starts without PHIs, which is
        # the edge target contract the verifier enforces.  The text/LLVM
        # oracle keeps its historical block creation order below.
        inline_edge = IRBuilder_can_inline_error_edge(self.builder)
        cont = None
        if not inline_edge:
            cont = parent_fn.append_basic_block(name=self._fresh("call.cont"))
        error_dest = err_target
        landing_slot = None
        payload = -1
        if span is not None:
            if inline_edge:
                error_dest, landing_slot, payload = self._direct_frame_landing(
                    err_target, span
                )
            else:
                error_dest = self._ensure_post_call_frame_block(err_target, span)
        cleanup = None
        if needs_cleanup:
            cleanup = parent_fn.append_basic_block(
                name=self._fresh("call.err.cleanup")
            )
        edge_dest = error_dest if cleanup is None else cleanup
        if inline_edge and IRBuilder_try_inline_error_edge(
            self.builder,
            cmp,
            edge_dest,
            0 if span is None else int(span.line),
            0,
            -1 if cleanup is not None else payload,
        ):
            if (
                self._entry_inline_edge_anchor_function is not parent_fn
                and self.builder._block is parent_fn.blocks[0]
            ):
                # Entry-hoisted root enters/initializers must precede the
                # first exceptional edge out of the entry block; see
                # ``_position_at_entry_hoist_point``.
                self._entry_inline_edge_anchor_function = parent_fn
                self._entry_inline_edge_anchor_record = cmp._instr
            if cleanup is not None:
                resume_block = self.builder._block
                resume_pos = self.builder._pos
                self.builder.position_at_end(cleanup)
                self._emit_post_call_error_cleanup(
                    error_dest,
                    release_on_error=release_on_error,
                    cpy_release_on_error=cpy_release_on_error,
                    rooted_release_on_error=rooted_release_on_error,
                    pinned_release_on_error=pinned_release_on_error,
                    lifo_owned_root_slots_on_error=lifo_owned_root_slots_on_error,
                    landing_slot=landing_slot,
                    payload=payload,
                )
                self.builder.position_at_end(resume_block)
                self.builder._pos = resume_pos
            return
        if cont is None:
            cont = parent_fn.append_basic_block(name=self._fresh("call.cont"))
        self.builder.cbranch(cmp, edge_dest, cont)
        if cleanup is not None:
            self.builder.position_at_end(cleanup)
            self._emit_post_call_error_cleanup(
                error_dest,
                release_on_error=release_on_error,
                cpy_release_on_error=cpy_release_on_error,
                rooted_release_on_error=rooted_release_on_error,
                pinned_release_on_error=pinned_release_on_error,
                lifo_owned_root_slots_on_error=lifo_owned_root_slots_on_error,
            )
        self.builder.position_at_end(cont)

    def _emit_post_call_error_cleanup(
        self,
        error_dest: ir.Block,
        *,
        release_on_error: tuple[ir.Value, ...],
        cpy_release_on_error: tuple[ir.Value, ...],
        rooted_release_on_error: tuple[tuple[ir.Value, ir.Value], ...],
        pinned_release_on_error: tuple[tuple[ir.Value, bool], ...],
        lifo_owned_root_slots_on_error: tuple[ir.Value, ...],
        landing_slot=None,
        payload: int = -1,
    ) -> None:
        """Fill the current ``call.err.cleanup`` block and branch on.

        Release order is the historical one: foreign refs, rooted container
        temporaries, LIFO owned root slots, pinned values, then plain owned
        temporaries.  The block is reached either by the text oracle's
        ``cbranch`` or by a direct inline error edge; its body is identical.
        """
        for value in cpy_release_on_error:
            self.builder.call(self.runtime["py_cpy_decref"], [value])
        for value, root_slot in rooted_release_on_error:
            self._leave_container_temp_root(root_slot)
            self.builder.call(self.runtime["pcc_gc_release"], [value])
        for root_slot in lifo_owned_root_slots_on_error:
            root_ptr = self._as_gc_ptr(
                root_slot,
                name=self._fresh("call.err.root.ptr"),
            )
            current = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), root_ptr],
                name=self._fresh("call.err.root.current"),
            )
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [root_ptr, ir.Constant(_CSTR, None)],
            )
            self._emit_gc_frame_leave_lifo_for_slot(root_slot)
            # The slot owned one temporary retain in addition to the
            # callee's returned owned reference. Clearing the slot drops
            # the former; this drops the latter on the exceptional edge.
            self.builder.call(self.runtime["pcc_gc_release"], [current])
        for value, release_owned in pinned_release_on_error:
            self._gc_unpin(value)
            if release_owned and not self._value_is_never_gc_object(value):
                self.builder.call(self.runtime["pcc_gc_release"], [value])
        for value in release_on_error:
            if not self._value_is_never_gc_object(value):
                self.builder.call(self.runtime["pcc_gc_release"], [value])
        if landing_slot is not None and payload >= 0:
            # The direct shared landing reads this site's traceback table
            # index from its slot; an edge straight into the landing stores
            # it from the emitter's cold stub, an explicit cleanup block
            # stores it here.
            self.builder.store(ir.Constant(_I32, payload), landing_slot)
        self.builder.branch(error_dest)

    def _ensure_post_call_frame_block(
        self,
        err_target: ir.Block,
        span: SourceSpan,
    ) -> ir.Block:
        parent_fn = self.current_function
        line = int(span.line)
        # Nested cheap-key dicts, not one five-element tuple key.  This runs
        # after every call that may raise and dominated a self-hosted pcc1
        # compile (31% inclusive): the tuple key allocated a GC object per
        # lookup (registration, object-graph lock, managed-pointer probe) and
        # boxed `span.line`, then hashed five elements through per-element
        # dispatch.  The outer key is `id(parent_fn)` rather than
        # `parent_fn.name` because the mangled LLVM name is ~80 characters and
        # nothing caches a string's hash here, so keying on it re-hashes those
        # bytes on every call — measurably worse than the tuple it replaced.
        # An address below 2**62 is a tagged int, so this allocates nothing.
        # `func_name` and `file_name` drop out of the key: one LLVM function is
        # emitted from one Python def in one source file, so `parent_fn`
        # already determines both.
        by_target = self._post_call_frame_blocks.get(id(parent_fn))
        if by_target is None:
            by_target = {}
            self._post_call_frame_blocks[id(parent_fn)] = by_target
        by_line = by_target.get(err_target.name)
        if by_line is None:
            by_line = {}
            by_target[err_target.name] = by_line
        existing = by_line.get(line)
        if existing is not None:
            return existing

        frame_bb = parent_fn.append_basic_block(
            name=self._fresh("err.frame"),
        )
        by_line[line] = frame_bb
        save_block = self.builder._block
        self.builder.position_at_end(frame_bb)
        exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("err.frame.exc"),
        )
        self._emit_exception_frame(exc, span)
        self.builder.branch(err_target)
        self.builder.position_at_end(save_block)
        return frame_bb

    def _ensure_fn_err_exit(self) -> ir.Block:
        """Return the current function's error-exit epilogue block,
        creating it on first use. The epilogue returns the function's
        sentinel value (NULL for PyObject*, zero for integer and floating
        return types, undef void for void returns).
        """
        fn = self.current_function
        fn_name = fn.name
        existing = self._fn_err_exit_blocks.get(fn_name)
        if existing is not None:
            return existing
        err_bb = fn.append_basic_block(name="err.exit")
        # Position a small builder at err_bb to emit the sentinel return.
        save_block = self.builder._block
        self.builder.position_at_end(err_bb)
        generator_exit = (
            len(self._generator_ctx_stack) > 0
            and len(fn.args) > 0
            and self._generator_ctx_stack[-1].get("resume_function") is fn
        )
        if generator_exit:
            # Generator entry restores every frame slot with an owned read.
            # An escaping exception ends this activation just like return;
            # leaving those owners behind retains the suspended task tree.
            self.builder.call(
                self.runtime["py_gen_set_done"],
                [self._generator_current_object(self._generator_ctx_stack[-1])],
            )
            emit_generator_terminal_frame_clear(self)
        # Keep terminal releases before every frame_leave, including slots
        # discovered after an earlier error edge. Generator completion still
        # precedes callbacks from destroying its locals.
        finish_bb = fn.append_basic_block(name="err.finish")
        self.builder.branch(finish_bb)
        self.builder.position_at_end(finish_bb)
        ret_ty = fn.function_type.return_type
        if isinstance(ret_ty, ir.VoidType):
            self.builder.ret_void()
        elif isinstance(ret_ty, ir.PointerType):
            self.builder.ret(ir.Constant(ret_ty, None))
        elif isinstance(ret_ty, ir.IntType):
            if fn_name == "main":
                exc = self.builder.call(
                    self.runtime["py_current_exception"],
                    [],
                    name=self._fresh("unhandled.exc"),
                )
                # SystemExit(code) exits with its code silently, as in
                # CPython; anything else prints its traceback.
                status = self.builder.call(
                    self.runtime["py_exc_handle_uncaught"],
                    [exc],
                    name=self._fresh("unhandled.status"),
                )
                self.builder.call(self.runtime["py_clear_exception"], [])
                if ret_ty.width != 64:
                    status = self.builder.trunc(
                        status, ret_ty, name=self._fresh("unhandled.status.trunc")
                    )
                self.builder.ret(status)
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))
        elif isinstance(ret_ty, (ir.FloatType, ir.DoubleType)):
            # The caller checks TLS after the call returns. A floating error
            # path must return its typed sentinel rather than trap first.
            self.builder.ret(ir.Constant(ret_ty, 0.0))
        elif isinstance(ret_ty, ir.LiteralStructType):
            self.builder.ret(self._zero_of(ret_ty))
        else:
            # Unsupported return type for error-path sentinel; fall
            # back to `unreachable` so at least the build surface fails
            # obviously if it ever triggers (shouldn't happen for pcc's
            # current emitted types).
            self.builder.unreachable()
        self._fn_err_exit_blocks[fn_name] = err_bb
        self._fn_err_exit_finish_blocks[fn_name] = finish_bb
        # Owned flags are registered by physical (slot, flag) identity, so
        # lexical name restoration cannot lose a previously created owner.
        for owned_entry in self._fn_err_exit_owned_slots.get(fn_name, ()):
            self._patch_fn_err_exit_gc_root_leave(owned_entry[0], owned_entry[1])
        # Back-patch every slot this function already frame-registered.
        # Drive this from the per-slot registry, NOT from name->env lookups:
        # a slot whose env entry was popped or re-bound (e.g. comprehension
        # targets after their env save/restore) still has a live entry-block
        # frame_enter and must be left on the error path too.
        registry: list = []
        if hasattr(self, "_fn_gc_root_slot_registry"):
            if fn_name in self._fn_gc_root_slot_registry:
                registry = self._fn_gc_root_slot_registry[fn_name]
        for entry in registry:
            self._patch_fn_err_exit_gc_root_leave(entry[0], entry[1])
        self.builder.position_at_end(save_block)
        return err_bb
