"""Exception and error-exit lowering helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir
from pcc.llvm_capi.ir import (
    IRBuilder_can_inline_error_edge,
    IRBuilder_declare_inline_error_landing,
    IRBuilder_try_inline_error_edge,
)

from ..py_ast import (
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
    TupleExpr,
    While,
    With,
)
from .builtin_exceptions import (
    BUILTIN_EXC_TAG as _BUILTIN_EXC_TAG,
    builtin_exc_tag_or_missing as _builtin_exc_tag_or_missing,
)

_I8 = ir.IntType(8)
_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_OPTIONAL_IMPORT_MISSING_NONE = "pcc.optional_import_missing.None"


def _inline_error_edge_env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_DIRECT_INLINE_ERROR_EDGE_ENABLED = (
    _inline_error_edge_env_enabled("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE")
    and _inline_error_edge_env_enabled("PCC_DIRECT_INDEXED_KERNEL_CAPTURE")
    and _inline_error_edge_env_enabled("PCC_DIRECT_INDEXED_KERNEL_EMIT")
)


class ExceptionLoweringMixin:
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

    def _emit_generator_exceptional_finally(
        self,
        stmt: Try,
        current_exc: ir.Value,
        outer_err_block,
    ) -> bool:
        """Run a generator ``finally`` with its unwinding exception rooted.

        Resume points receive thrown-in cancellation through TLS.  Leaving that
        old exception pending while emitting cleanup makes every ordinary
        post-call check mistake it for a new cleanup failure.  Move it into the
        generator's managed frame slot, clear TLS for cleanup, then restore it
        unless cleanup raised a replacement exception.

        The slot is part of the heap frame rather than a C stack temporary, so
        even a forbidden cleanup yield cannot leave an updateable GC root
        pointing into a returned carrier stack. ``py_gen_close`` will reject
        that yield and the terminal task-failure path releases the frame.
        """
        if not stmt.finally_body or not self._generator_ctx_stack:
            return False
        ctx = self._generator_ctx_stack[-1]
        hidden = self._generator_finally_exception_name(stmt)
        frame_entry = ctx["frame_slots"].get(hidden)
        if frame_entry is None:
            return False
        root_ptr = self._as_gc_ptr(
            frame_entry[1],
            name=self._fresh("finally.exception.root"),
        )
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [root_ptr, current_exc],
        )
        self.builder.call(self.runtime["py_clear_exception"], [])

        cleanup_error_bb = self.current_function.append_basic_block(
            name=self._fresh("finally.cleanup.error")
        )
        saved_err_block = self._push_try_err_block(cleanup_error_bb)
        active_roots = ctx["active_exception_unwind_roots"]
        active_roots.append(root_ptr)
        try:
            self._emit_stmts(stmt.finally_body)
        finally:
            active_roots.pop()
            self._restore_try_err_block(saved_err_block)

        if not self._builder_block_is_terminated():
            # Re-derive the slot pointer here: in a may_park state machine
            # the finally body may have resumed into blocks the try-entry
            # cast does not dominate.
            restore_root_ptr = self._as_gc_ptr(
                frame_entry[1],
                name=self._fresh("finally.exception.root"),
            )
            saved_exc = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [ir.Constant(_CSTR, None), restore_root_ptr],
                name=self._fresh("finally.exception.restore"),
            )
            self.builder.call(self.runtime["py_raise"], [saved_exc])
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [restore_root_ptr, ir.Constant(_CSTR, None)],
            )
            self.builder.branch(outer_err_block)

        self.builder.position_at_end(cleanup_error_bb)
        cleanup_root_ptr = self._as_gc_ptr(
            frame_entry[1],
            name=self._fresh("finally.exception.root"),
        )
        cleanup_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("finally.cleanup.exception"),
        )
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
            self.runtime["py_exc_set_context"],
            [cleanup_exc, saved_exc],
        )
        self.builder.branch(release_bb)
        self.builder.position_at_end(release_bb)
        self.builder.call(
            self.runtime["pcc_gc_store_root"],
            [cleanup_root_ptr, ir.Constant(_CSTR, None)],
        )
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
            # PEP 3134 implicit chaining: when a new exception is raised while
            # a handler exception is active (`raise Y` inside `except X:`), the
            # new exception's __context__ is the exception being handled. The
            # runtime `py_raise` auto-chains from TLS, but pcc clears TLS at
            # handler entry (py_clear_exception) and tracks the active handler
            # exception only in `_active_handler_excs`, so TLS is NULL here and
            # the runtime auto-chain never fires. Set __context__ explicitly
            # from the active handler exception. CPython sets __context__ even
            # when an explicit `raise ... from ...` cause is present (it only
            # flips __suppress_context__), so this runs regardless of cause.
            self._emit_set_implicit_exception_context(exc_val)
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

    def _emit_set_implicit_exception_context(self, exc_val: ir.Value) -> None:
        """Set ``exc_val.__context__`` to the active handler exception
        (PEP 3134 implicit chaining) when raising a new exception inside
        an ``except`` handler.

        The active handler exception lives in ``_active_handler_excs``
        (its top is the currently-handled exception). We guard against a
        self-cycle: re-raising the caught exception by name (``raise e``)
        would make ``exc_val`` identical to the active exception, and
        CPython does not set ``__context__`` to the exception itself.
        The runtime ``py_exc_set_context`` writes the ``context`` slot
        unconditionally, so the identity guard is emitted here.
        """
        active = self._active_handler_exception_for_current_function()
        if active is None:
            return
        # Only chain when the new exception is a distinct object from the
        # exception being handled (avoid __context__ self-reference).
        distinct = self.builder.icmp_signed(
            "!=",
            exc_val,
            active,
            name=self._fresh("exc.ctx.distinct"),
        )
        fn = self.current_function
        set_bb = fn.append_basic_block(name=self._fresh("exc.ctx.set"))
        cont_bb = fn.append_basic_block(name=self._fresh("exc.ctx.cont"))
        self.builder.cbranch(distinct, set_bb, cont_bb)
        self.builder.position_at_end(set_bb)
        self.builder.call(self.runtime["py_exc_set_context"], [exc_val, active])
        self.builder.branch(cont_bb)
        self.builder.position_at_end(cont_bb)

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
                "[pcc.codegen] " + mod_name + ":" + func_name + ":try " + label + "\n"
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
            finally_stack.append(stmt.finally_body)
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
            emitted_rooted_finally = self._emit_generator_exceptional_finally(
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
            elif isinstance(h.exc_type, TupleExpr):
                cond = None
                for sub in h.exc_type.elems:
                    cls_val = self._emit_exception_class_ref(sub)
                    match_i32 = self.builder.call(
                        self.runtime["py_exc_matches"],
                        [current_exc, cls_val],
                        name=self._fresh("exc.matches"),
                    )
                    this = self.builder.icmp_signed(
                        "!=",
                        match_i32,
                        ir.Constant(_I64, 0),
                        name=self._fresh("exc.matches.i1"),
                    )
                    cond = (
                        this
                        if cond is None
                        else self.builder.or_(
                            cond,
                            this,
                            name=self._fresh("exc.or"),
                        )
                    )
                assert cond is not None
            else:
                cls_val = self._emit_exception_class_ref(h.exc_type)
                match_i32 = self.builder.call(
                    self.runtime["py_exc_matches"],
                    [current_exc, cls_val],
                    name=self._fresh("exc.matches"),
                )
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
                # No handler matched: the finally block must STILL run before
                # the exception propagates to the outer handler (Python
                # guarantees finally always executes). Mirrors the no-handlers
                # path above; without this the finally was silently skipped on
                # the unmatched-exception path.
                outer = prev_err_block or self._ensure_fn_err_exit()
                emitted_rooted_finally = (
                    self._emit_generator_exceptional_finally(
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
            handler_exc = current_exc
            # Retain the handled exception across the handler body when:
            #  - it is name-bound (`except X as e:`), or
            #  - the body re-raises bare (`raise`), or
            #  - the body raises a NEW exception (`raise Y`): PEP 3134 implicit
            #    chaining sets the new exception's __context__ to the exception
            #    being handled, so it must be kept alive and tracked in
            #    `_active_handler_excs` for `_emit_set_implicit_exception_context`.
            retain_handler_exc = h.name is not None or body_has_raise(
                h.body, bare_only=False
            )
            handler_slot = None
            if retain_handler_exc:
                if self._generator_ctx_stack:
                    ctx = self._generator_ctx_stack[-1]
                    hidden = self._generator_handler_exception_name(stmt, i)
                    handler_slot = ctx["frame_slots"][hidden][1]
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [self._as_gc_ptr(handler_slot), handler_exc],
                    )
                else:
                    handler_exc = self._gc_retain(handler_exc)
            self.builder.call(self.runtime["py_clear_exception"], [])
            binding_slot = None
            if h.name is not None:
                if handler_slot is not None:
                    # Use the planned frame slot, not a fresh stack local
                    # that the generator save/restore loop cannot see.
                    slot = self._generator_ctx_stack[-1]["frame_slots"][h.name][1]
                    handler_exc = self.builder.load(handler_slot)
                    self.builder.call(
                        self.runtime["pcc_gc_store_root"],
                        [self._as_gc_ptr(slot), handler_exc],
                    )
                else:
                    slot = self._alloca_in_entry(_CSTR, name=f"{h.name}.addr")
                    self.builder.store(handler_exc, slot)
                self.env[h.name] = (slot, _CSTR, DynType(name="dyn"))
                binding_slot = slot
                # Mark `e` as an except-binding so a later `saved = e` GC-roots
                # `saved` (the surviving reference once the handler's retain is
                # released at handler end). Otherwise the borrowed-copy local is
                # not a frame root and the tracing collect sweeps the exception's
                # message. See gc-5backend-exception-referent-roots-no-libpython.md.
                binding_names = getattr(self, "_except_binding_names", None)
                if binding_names is not None:
                    binding_names.add(h.name)
            active_excs = list(self._active_handler_excs)
            if retain_handler_exc:
                if handler_slot is not None:
                    active_excs.append((self.current_function, handler_exc, handler_slot))
                else:
                    active_excs.append((self.current_function, handler_exc))
                self._active_handler_excs = active_excs
            if stmt.finally_body:
                finally_stack = list(getattr(self, "_finally_stack", ()))
                finally_stack.append(stmt.finally_body)
                self._finally_stack = finally_stack
            try:
                self._emit_stmts(h.body)
            finally:
                if stmt.finally_body:
                    finally_stack = list(getattr(self, "_finally_stack", ()))
                    if finally_stack:
                        finally_stack.pop()
                    self._finally_stack = finally_stack
                if retain_handler_exc:
                    active_excs.pop()
                    if active_excs:
                        self._active_handler_excs = active_excs
                    else:
                        self._active_handler_excs = []
            if not self._builder_block_is_terminated():
                if stmt.finally_body:
                    self._emit_stmts(stmt.finally_body)
                if not self._builder_block_is_terminated():
                    if handler_slot is not None:
                        self.builder.call(
                            self.runtime["pcc_gc_store_root"],
                            [self._as_gc_ptr(handler_slot), ir.Constant(_CSTR, None)],
                        )
                    elif retain_handler_exc:
                        release_value = handler_exc
                        if binding_slot is not None:
                            # A may_park handler body may resume into a block
                            # the retain does not dominate; the binding slot
                            # is the frame-visible home of the same reference.
                            release_value = self.builder.load(
                                binding_slot,
                                name=self._fresh(f"{h.name}.release"),
                            )
                        self._gc_release(release_value)
                    self.builder.branch(done_bb)

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
                return self._pooled_cstr_ptr("", ".exc.msg")
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
            if tag >= 0:
                msg_ptr = _message_cstr(exc_expr.args, exc_expr.kwargs)
                return self.builder.call(
                    self.runtime["py_exc_new"],
                    [ir.Constant(_I64, tag), msg_ptr],
                    name=self._fresh(f"exc.{cls_name}"),
                )
            info = self.class_lowering.classes.get(cls_name)
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
        """Build a PyObject* for an exception class used in
        ``except <Expr>:``. Supports a bare builtin Name, a local user
        class, or a CPython-imported class (routed through the
        libpython fallback). Falls back to the builtin ``Exception``
        class when the name can't be resolved — catches strictly more
        than requested, but lets pcc continue compiling files that
        reference exception classes declared in modules not yet
        reachable on the self-host path."""
        if isinstance(expr, Name):
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
            # Fall back to the generic ``Exception`` base so the except
            # clause still compiles. Runtime semantics are broader than
            # CPython's, but the goal here is self-host compile coverage
            # — the narrow type match is recovered once the referenced
            # exception class reaches pcc's ClassInfo registry.
            return self.builder.call(
                self.runtime["py_exc_builtin_class"],
                [ir.Constant(_I64, 1)],
                name=self._fresh(f"exc.cls.fallback.{expr.ident}"),
            )
        # Attribute access: ``except json.JSONDecodeError:`` etc. Fall
        # back to the generic Exception class so the clause at least
        # compiles. Runtime match is broader than CPython would do;
        # recovering the narrow type match requires exposing the
        # imported class's CPython PyTypeObject through py_exc_matches,
        # which is a separate runtime extension.
        try:
            from ..py_ast import Attr as _AttrExpr  # local import
        except Exception:
            _AttrExpr = None
        if _AttrExpr is not None and isinstance(expr, _AttrExpr):
            return self.builder.call(
                self.runtime["py_exc_builtin_class"],
                [ir.Constant(_I64, 1)],
                name=self._fresh(f"exc.cls.attr_fallback.{expr.name}"),
            )
        raise NotImplementedError(
            f"Layer 1 except-clause class expression {type(expr).__name__} "
            "not supported"
        )

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

    def _emit_non_iterable_scalar_guard(
        self,
        src_obj: ir.Value,
        builtin_name: str,
        release_first: tuple[ir.Value, ...] = (),
    ) -> None:
        """Fail closed when a dyn-held source is a scalar that a positional
        walk would silently treat as empty.

        ``py_obj_len`` returns 0 WITHOUT raising for non-sized objects
        (py_obj_ops_dispatch.c), so ``any(x)`` / ``zip(t, x)`` over a
        dyn-held int/float/bool/None silently answered False / [] where
        CPython raises TypeError.  The guard rejects only tags that are
        never iterable; instances (which may implement __len__/__getitem__)
        and every container tag pass through untouched.
        """
        from .freestanding_abi_constants import (
            PY_TYPE_BOOL,
            PY_TYPE_FLOAT,
            PY_TYPE_INT,
            PY_TYPE_NONE,
        )

        fn = self.current_function
        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [src_obj],
            name=self._fresh(f"{builtin_name}.src.tag"),
        )
        scalar = ir.Constant(ir.IntType(1), 0)
        for tag_value in (
            PY_TYPE_INT,
            PY_TYPE_FLOAT,
            PY_TYPE_BOOL,
            PY_TYPE_NONE,
        ):
            is_tag = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, tag_value),
                name=self._fresh(f"{builtin_name}.tag.eq"),
            )
            scalar = self.builder.or_(
                scalar,
                is_tag,
                name=self._fresh(f"{builtin_name}.tag.scalar"),
            )
        bad_bb = fn.append_basic_block(
            name=self._fresh(f"{builtin_name}.scalar.bad")
        )
        ok_bb = fn.append_basic_block(
            name=self._fresh(f"{builtin_name}.scalar.ok")
        )
        self.builder.cbranch(scalar, bad_bb, ok_bb)
        self.builder.position_at_end(bad_bb)
        for owned in release_first:
            self.builder.call(self.runtime["pcc_gc_release"], [owned])
        message = self._ptr_to_cstr(
            self._cstr_global(
                builtin_name + "() argument is not iterable",
                f".{builtin_name}.scalar.typeerror",
            )
        )
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 3), message],
            name=self._fresh(f"{builtin_name}.scalar.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        err_target = (
            self._current_try_err_block() or self._ensure_fn_err_exit()
        )
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)

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
        inline_edge = (
            _DIRECT_INLINE_ERROR_EDGE_ENABLED
            and IRBuilder_can_inline_error_edge(self.builder)
        )
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
            if release_owned:
                self.builder.call(self.runtime["pcc_gc_release"], [value])
        for value in release_on_error:
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
        sentinel value (NULL for PyObject*, 0 for integer return
        types, undef void for void returns).
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
            and self._generator_ctx_stack[-1]["gen"] is fn.args[0]
        )
        if generator_exit:
            # Generator entry restores every frame slot with an owned read.
            # An escaping exception ends this activation just like return;
            # leaving those owners behind retains the suspended task tree.
            self.builder.call(self.runtime["py_gen_set_done"], [fn.args[0]])
        # Function-level exact-int representation planning registers every
        # such local and its owned flag before body emission.  Release the
        # currently-owned object on the shared error epilogue before the root
        # frame is left; otherwise an exception in a later exact operation
        # leaks the initializer/previous branch value.  Keep this finite and
        # representation-specific rather than changing the historical cleanup
        # policy for unrelated locals in this slice.
        exact_flags = getattr(self, "_exact_int_env_flags", {})
        for_target_names = getattr(self, "_for_target_owned_names", set())
        for local_name in sorted(getattr(self, "_owned_local_names", set())):
            if (
                not generator_exit
                and not exact_flags.get(local_name, False)
                and local_name not in for_target_names
            ):
                continue
            slot = self.env.get(local_name)
            if slot is None:
                continue
            local_alloca, local_ir_ty, _local_decl_ty = slot
            if not isinstance(local_ir_ty, ir.PointerType):
                continue
            owned_flag = self._ensure_owned_local_flag(
                local_name,
                local_alloca,
            )
            is_owned = self.builder.load(
                owned_flag,
                name=self._fresh(local_name + ".err.owned"),
            )
            current = self.builder.call(
                self.runtime["pcc_gc_load_ptr"],
                [
                    ir.Constant(_CSTR, None),
                    self._as_gc_ptr(
                        local_alloca,
                        name=self._fresh(local_name + ".err.gc.slot"),
                    ),
                ],
                name=self._fresh(local_name + ".err.current"),
            )
            release_value = self.builder.select(
                is_owned,
                current,
                ir.Constant(_CSTR, None),
                name=self._fresh(local_name + ".err.release.value"),
            )
            self._gc_release(
                release_value,
                self._release_context_label("local:" + local_name),
            )
            self.builder.store(ir.Constant(_I1, 0), owned_flag)
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
                self.builder.call(
                    self.runtime["py_exc_print_unhandled"],
                    [exc],
                )
                self.builder.call(self.runtime["py_clear_exception"], [])
                self.builder.ret(ir.Constant(ret_ty, 1))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))
        elif isinstance(ret_ty, ir.LiteralStructType):
            self.builder.ret(self._zero_of(ret_ty))
        else:
            # Unsupported return type for error-path sentinel; fall
            # back to `unreachable` so at least the build surface fails
            # obviously if it ever triggers (shouldn't happen for pcc's
            # current emitted types).
            self.builder.unreachable()
        self._fn_err_exit_blocks[fn_name] = err_bb
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
