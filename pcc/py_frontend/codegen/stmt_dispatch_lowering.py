"""Statement dispatch lowering for L1CodeGen."""

from __future__ import annotations

import os
import sys

from ..py_ast import (
    Assign,
    AugAssign,
    Break,
    ClassDef,
    Continue,
    Delete,
    DynType,
    ExprStmt,
    For,
    FuncDef,
    Global,
    If,
    Import,
    ImportFrom,
    Name,
    Nonlocal,
    Pass,
    Raise,
    Return,
    Stmt,
    Try,
    While,
    With,
)
from .errors import L1CodegenError
from .layer1_support import (
    _is_import_from_stmt,
    _is_import_stmt,
    _stmt_kind_name,
)


def _stmt_has_targets(stmt) -> bool:
    return hasattr(stmt, "targets") and stmt.targets is not None


def _stmt_has_value(stmt) -> bool:
    return hasattr(stmt, "value") and stmt.value is not None


def _stmt_has_target(stmt) -> bool:
    return hasattr(stmt, "target") and stmt.target is not None


def _stmt_has_op(stmt) -> bool:
    return hasattr(stmt, "op") and stmt.op is not None


def _stmt_has_iter(stmt) -> bool:
    return hasattr(stmt, "iter") and stmt.iter is not None


def _stmt_has_body(stmt) -> bool:
    return hasattr(stmt, "body") and stmt.body is not None


def _stmt_has_expr(stmt) -> bool:
    return hasattr(stmt, "expr") and stmt.expr is not None


def _stmt_is_assign(stmt) -> bool:
    return _stmt_has_targets(stmt) and _stmt_has_value(stmt)


def _stmt_is_augassign(stmt) -> bool:
    return _stmt_has_target(stmt) and _stmt_has_op(stmt) and _stmt_has_value(stmt)


def _stmt_is_expr(stmt) -> bool:
    return _stmt_has_expr(stmt)


def _stmt_is_for(stmt) -> bool:
    return _stmt_has_target(stmt) and _stmt_has_iter(stmt) and _stmt_has_body(stmt)


def _stmt_is_return(stmt) -> bool:
    return isinstance(stmt, Return) or (
        _stmt_has_value(stmt)
        and not _stmt_has_targets(stmt)
        and not _stmt_has_target(stmt)
    )


class StmtDispatchLoweringMixin:
    def _loop_finally_base(self) -> int:
        """Depth of the finally stack at loop entry. ``break``/``continue`` run
        the finally blocks pushed *above* this base (those entered inside the
        loop) before jumping, without disturbing finallys enclosing the loop."""
        stack = getattr(self, "_finally_stack", None)
        return len(stack) if stack else 0

    def _run_loop_exit_finallys(self, base: int) -> None:
        """Emit the finally blocks entered inside the current loop (top-down to
        ``base``) on a ``break``/``continue`` exit. Mirrors the return path's
        ``_emit_pending_finally_blocks`` but bounded to the loop scope."""
        stack = getattr(self, "_finally_stack", None)
        if not stack or getattr(self, "_emitting_finally", False):
            return
        prev = self._emitting_finally
        self._emitting_finally = True
        idx = len(stack) - 1
        while idx >= base:
            if self._builder_block_is_terminated():
                break
            self._emit_finally_entry(stack[idx])
            idx -= 1
        self._emitting_finally = prev

    def _emit_stmts_impl(self, stmts: tuple[Stmt, ...]) -> None:
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))
        stmt_index = 0
        for stmt in stmts:
            if self._builder_block_is_terminated():
                # Dead code after a return/raise — silently drop.
                return
            try:
                self._codegen_trace_set_stmt_context(stmt_index, _stmt_kind_name(stmt))
            except Exception:
                pass
            if debug_codegen:
                mod_name = self.ast_module.name or "<module>"
                func_name = (
                    self.current_func_def.name
                    if self.current_func_def is not None
                    else "<top>"
                )
                span = getattr(stmt, "span", None)
                loc = ""
                if span is not None:
                    loc = ":" + str(span.line) + ":" + str(span.col)
                sys.stderr.write(
                    "[pcc.codegen] "
                    + mod_name
                    + ":"
                    + func_name
                    + ":stmt begin "
                    + str(stmt_index)
                    + " "
                    + _stmt_kind_name(stmt)
                    + loc
                    + "\n"
                )
            self._emit_stmt(stmt)
            if debug_codegen:
                mod_name = self.ast_module.name or "<module>"
                func_name = (
                    self.current_func_def.name
                    if self.current_func_def is not None
                    else "<top>"
                )
                sys.stderr.write(
                    "[pcc.codegen] "
                    + mod_name
                    + ":"
                    + func_name
                    + ":stmt end "
                    + str(stmt_index)
                    + " "
                    + _stmt_kind_name(stmt)
                    + "\n"
                )
            stmt_index += 1

    def _emit_stmt_impl(self, stmt: Stmt) -> None:
        if len(self._generator_ctx_stack) > 0:
            if _stmt_is_return(stmt):
                self._emit_generator_return(stmt)
                return
            if _stmt_is_expr(stmt) and not _stmt_is_for(stmt):
                sentinel = self._yield_sentinel_call(stmt.expr)
                if sentinel is not None:
                    _kind, call = sentinel
                    if _kind == "yield_from":
                        self._emit_generator_yield_from(call)
                    else:
                        self._emit_generator_yield(call)
                    return
        if isinstance(stmt, Pass):
            return
        if _stmt_is_assign(stmt):
            self._emit_assign(stmt)
            return
        if _stmt_is_augassign(stmt):
            self._emit_augassign(stmt)
            return
        if _stmt_is_for(stmt):
            self._emit_for(stmt)
            return
        if _stmt_is_return(stmt):
            self._emit_return(stmt)
            return
        if _stmt_is_expr(stmt):
            self._emit_expr_stmt(stmt)
            return
        if isinstance(stmt, Raise):
            self._emit_raise(stmt)
            return
        if isinstance(stmt, Try):
            self._emit_try(stmt)
            return
        if isinstance(stmt, With):
            self._emit_with(stmt)
            return
        if _is_import_stmt(stmt):
            if _is_import_from_stmt(stmt):
                self._emit_import_from(stmt)
            else:
                self._emit_import(stmt)
            return
        if isinstance(stmt, Import):
            self._emit_import(stmt)
            return
        if isinstance(stmt, ImportFrom):
            self._emit_import_from(stmt)
            return
        if isinstance(stmt, If):
            self._emit_if(stmt)
            return
        if isinstance(stmt, While):
            self._emit_while(stmt)
            return
        if isinstance(stmt, (Nonlocal, Global)):
            # pcc has no lexical-scope closure story — ``nonlocal`` /
            # ``global`` declarations are recorded for symbol-table
            # hygiene in CPython, but at the pcc level every read/write
            # of the referenced name already routes to ``self.env`` /
            # ``_module_globals``. Accept the directive as a no-op so
            # solo-compile doesn't abort on sources that include one.
            return
        if isinstance(stmt, Break):
            if not self.loop_stack:
                raise L1CodegenError("break outside loop")
            frame = self.loop_stack[-1]
            break_bb = frame[1]
            # finally blocks entered inside the loop must run before the jump.
            self._run_loop_exit_finallys(frame[2] if len(frame) > 2 else 0)
            if not self._builder_block_is_terminated():
                self.builder.branch(break_bb)
            return
        if isinstance(stmt, Continue):
            if not self.loop_stack:
                raise L1CodegenError("continue outside loop")
            frame = self.loop_stack[-1]
            cont_bb = frame[0]
            self._run_loop_exit_finallys(frame[2] if len(frame) > 2 else 0)
            if not self._builder_block_is_terminated():
                self.builder.branch(cont_bb)
            return
        if isinstance(stmt, Delete):
            self._emit_delete(stmt)
            return
        if isinstance(stmt, FuncDef):
            stmt_fn = self._funcdef_functions.get(id(stmt))
            if stmt_fn is None:
                stmt_fn = self.functions.get(stmt.name)
            # A def nested in a module-scope control-flow block is an
            # executable binding.  Its body was predeclared/emitted by the
            # generation pass; create the callable only when this branch runs
            # and store it in the module namespace.  Function-local nested
            # defs remain closure-hoisted and therefore have no statement-side
            # effect here.
            if self.current_func_def is None and id(stmt) in getattr(
                self, "_module_block_funcdef_ids", set()
            ):
                fn_obj = self._emit_native_func_value(
                    stmt.name,
                    stmt.name,
                    stmt_fn,
                    (),
                )
                self._store_value_at_name(
                    Name(
                        span=stmt.span,
                        ty=DynType(name="dyn"),
                        ident=stmt.name,
                    ),
                    fn_obj,
                    DynType(name="dyn"),
                    value_is_owned=True,
                )
                return

            # An ordinary module-level ``def`` is executable too.  Publish
            # the callable as soon as the statement runs so an import cycle
            # can observe functions defined before the cycle began.  The
            # end-of-module globals synchronization is too late for that
            # partial-initialization boundary.
            #
            # Decorators may replace the callable.  Keep statement-time
            # publication to the same undecorated surface that the former
            # end-of-module synchronization exposed; eagerly wrapping every
            # metadata-decorated package function causes severe IR growth.
            if (
                self.current_func_def is None
                and stmt.name in self._duplicate_module_function_names
            ):
                if stmt_fn is None:
                    raise L1CodegenError(
                        "duplicate function definition has no native body: "
                        + stmt.name
                    )
                decorators = self._func_decorators(stmt)
                semantic_decorators = bool(
                    decorators
                ) and self._decorators_are_native_functions(stmt)
                if semantic_decorators:
                    fn_obj = self._emit_decorated_user_function_value(
                        name=stmt.name,
                        fn=stmt_fn,
                        ast_func_def=stmt,
                    )
                else:
                    fn_obj = self._emit_native_func_value(
                        stmt.name,
                        stmt.name,
                        stmt_fn,
                        (),
                    )
                self._store_value_at_name(
                    Name(
                        span=stmt.span,
                        ty=DynType(name="dyn"),
                        ident=stmt.name,
                    ),
                    fn_obj,
                    DynType(name="dyn"),
                    value_is_owned=True,
                )
                return

            if self.current_func_def is None and stmt.name in self.functions:
                decorators = self._func_decorators(stmt)
                needs_object = bool(
                    getattr(self, "_native_function_object_exports", {}).get(
                        stmt.name,
                        False,
                    )
                )
                # Avoid ``all(generator)`` in this self-hosted codegen path.
                # pcc1 can lose the yielded bool projection and classify a
                # metadata-only decorator as semantic, which suppresses a
                # required runtime function-object publication.
                metadata_only = bool(decorators)
                if metadata_only:
                    for decorator in decorators:
                        if not self._decorator_is_noop_whitelist(decorator):
                            metadata_only = False
                            break
                semantic_decorators = bool(
                    decorators
                ) and self._decorators_are_native_functions(stmt)
                if (
                    not decorators
                    or semantic_decorators
                    or (needs_object and metadata_only)
                ):
                    if semantic_decorators:
                        fn_obj = self._emit_decorated_user_function_value(
                            name=stmt.name,
                            fn=stmt_fn,
                            ast_func_def=stmt,
                        )
                    else:
                        fn_obj = self._emit_native_func_value(
                            stmt.name,
                            stmt.name,
                            stmt_fn,
                            (),
                        )
                    module_name = self.ast_module.name or "__main__"
                    module_name_ptr = self._ptr_to_cstr(
                        self._cstr_global(
                            module_name,
                            f".pcc.def.binding.module.{module_name}",
                        )
                    )
                    self.builder.call(
                        self.runtime["py_module_attr_set"],
                        [
                            module_name_ptr,
                            self._attr_name_ptr(stmt.name),
                            fn_obj,
                        ],
                        name=self._fresh(f"pcc.def.binding.publish.{stmt.name}"),
                    )
                    self._gc_release(fn_obj)
            return
        if isinstance(stmt, ClassDef):
            if self.current_func_def is None:
                self.class_lowering.emit_class_statement_init(stmt)
            else:
                self.class_lowering.emit_local_class_statement_init(stmt)
            return
        raise NotImplementedError(
            f"Layer 1 does not handle statement {_stmt_kind_name(stmt)}"
        )
