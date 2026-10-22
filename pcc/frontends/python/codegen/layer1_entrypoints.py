"""Public entrypoint wrappers for ``L1CodeGen``."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.diagnostics import DiagnosticSpan
from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Expr, Module, Stmt
from pcc.frontends.python.codegen.expr_dispatch_lowering import ExprDispatchLoweringMixin
from pcc.frontends.python.codegen.errors import CodegenDiagnosticError
from pcc.frontends.python.codegen.generation_lowering import GenerationLoweringMixin
from pcc.frontends.python.codegen.layer1_init import Layer1InitMixin
from pcc.frontends.python.codegen.stmt_dispatch_lowering import StmtDispatchLoweringMixin


class L1CodeGenEntrypointMixin:
    def __init__(
        self,
        module: Module,
        emit_cpy_main_exitcode: bool = False,
        ir_scaffold_mode: str = "off",
    ):
        Layer1InitMixin._init_l1_state(
            self,
            module,
            emit_cpy_main_exitcode,
            ir_scaffold_mode,
        )

    def generate(self, module: Optional[Module] = None) -> str:
        return L1CodeGenEntrypointMixin._generate_with_diagnostics(
            self, module, False,
        )

    def _generate_initial_module(self) -> str:
        """Lower a fresh worker-owned instance without discarding its IR.

        Only the isolated worker uses this route, before any generation and
        without changing the constructor AST or debug-info environment.
        """
        return L1CodeGenEntrypointMixin._generate_with_diagnostics(
            self, self.ast_module, True,
        )

    def _generate_with_diagnostics(
        self, module: Optional[Module], reuse_initial_ir: bool,
    ) -> str:
        initial_ir_available = self._initial_ir_available
        # A failed attempt is not a fresh instance, including failures during
        # target/trace setup. Public generate() consumes this capability too.
        self._initial_ir_available = False
        if self._target_triple:
            self.module.triple = self._target_triple
        elif sys.platform.startswith("linux") or sys.platform == "win32":
            from pcc.frontends.python.pipeline_targets import host_target_triple
            self.module.triple = host_target_triple()
        if self._codegen_trace_is_enabled():
            target_module = self.ast_module if module is None else module
            self._codegen_trace_push(
                "module",
                -1,
                "",
                "",
                self._codegen_trace_span(target_module),
            )
        try:
            if reuse_initial_ir and not initial_ir_available:
                raise RuntimeError("initial module generation requires a fresh codegen instance")
            return GenerationLoweringMixin._generate_impl(
                self, module, reuse_initial_ir,
            )
        except BaseException as exc:
            self._codegen_trace_dump(exc)
            if self._codegen_trace_is_enabled():
                if isinstance(exc, CodegenDiagnosticError):
                    raise
                diagnostic_span = None
                ring = self._codegen_trace_ring
                if len(ring) > 0:
                    ring_index = len(ring) - 1
                    capacity = self._codegen_trace_capacity
                    if capacity > 0 and len(ring) >= capacity:
                        ring_index = (self._codegen_trace_next - 1) % len(ring)
                    diagnostic_span = ring[ring_index][6]
                raise CodegenDiagnosticError(
                    str(exc) or type(exc).__name__,
                    diagnostic_span,
                    type(exc).__name__,
                ) from exc
            raise

    def _codegen_trace_span(self, node: object):
        try:
            span = node.span  # type: ignore[attr-defined]
        except Exception:
            return None
        if span is None:
            return None
        try:
            return DiagnosticSpan(
                file=str(span.file),
                line=int(span.line),
                col=int(span.col),
                end_line=int(span.end_line),
                end_col=int(span.end_col),
            )
        except Exception:
            return None

    def _codegen_trace_module(self) -> str:
        mod_name = ""
        try:
            mod_name = self.ast_module.name  # type: ignore[attr-defined]
        except Exception:
            mod_name = ""
        if mod_name:
            return mod_name
        return "<module>"

    def _codegen_trace_function(self) -> str:
        if self.current_func_def is None:
            return "<top>"
        return self.current_func_def.name

    def _codegen_trace_is_enabled(self) -> bool:
        try:
            return bool(self._codegen_trace_enabled)  # type: ignore[attr-defined]
        except Exception:
            return False

    def _codegen_trace_set_stmt_context(self, stmt_index: int, stmt_kind: str) -> None:
        if not self._codegen_trace_is_enabled():
            return
        try:
            self._codegen_current_stmt_index = stmt_index
            self._codegen_current_stmt_kind = stmt_kind
        except Exception:
            pass

    def _codegen_trace_push(
        self,
        boundary: str,
        stmt_index: int,
        stmt_kind: str,
        expr_kind: str,
        span,
    ) -> None:
        if not self._codegen_trace_is_enabled():
            return
        capacity = int(getattr(self, "_codegen_trace_capacity", 0))
        if capacity <= 0:
            return
        ring = getattr(self, "_codegen_trace_ring", None)
        if not isinstance(ring, list):
            return
        trace_next = int(getattr(self, "_codegen_trace_next", 0))
        if trace_next < 0:
            trace_next = 0
        entry = (
            boundary,
            self._codegen_trace_module(),
            self._codegen_trace_function(),
            str(stmt_index),
            str(stmt_kind),
            str(expr_kind),
            span,
        )
        if len(ring) < capacity:
            ring.append(entry)
            self._codegen_trace_next = (trace_next + 1) % capacity  # type: ignore[attr-defined]
            return
        trace_next = trace_next % capacity
        ring[trace_next] = entry
        self._codegen_trace_next = (trace_next + 1) % capacity  # type: ignore[attr-defined]

    def _codegen_trace_dump(self, exc: BaseException) -> None:
        if not self._codegen_trace_is_enabled():
            return
        if bool(getattr(self, "_codegen_trace_diagnosed", False)):
            return
        try:
            self._codegen_trace_diagnosed = True  # type: ignore[attr-defined]
        except Exception:
            pass

        def _write_line(line: str) -> None:
            try:
                sys.stderr.write(line)
            except Exception:
                try:
                    os.write(2, line.encode("utf-8"))
                except Exception:
                    pass

        module_name = self._codegen_trace_module()
        function_name = self._codegen_trace_function()
        exc_type = type(exc).__name__
        _write_line(
            "PCC_CODEGEN_EXCEPTION type="
            + exc_type
            + " module="
            + module_name
            + " function="
            + function_name
            + "\n"
        )
        # Under a self-compiled stage the runtime keeps one frame per error
        # check the exception crossed, and that trail is the only way to see
        # WHERE a failing lookup sits.  Printing it needs ``traceback`` (a
        # stdlib shim the stage1 closure must not grow by) or a ``pcc.extern``
        # declaration (which the same-package walker would also link in), so
        # it is not emitted here.  Recipe, from
        # docs/investigations/pcc1-host-mixin-state-outside-contract-aliases-slots.md:
        # temporarily ``import traceback``, then ``try: raise exc / except
        # BaseException: write(traceback.format_exc())`` -- the re-raise makes
        # the object the handled exception again, so its accumulated frames
        # print.  The context line below comes before ``str(exc)``, which has
        # segfaulted a corrupted stage.
        try:
            _write_line(
                "PCC_CODEGEN_EXCEPTION_CONTEXT "
                + "stmt_index="
                + str(getattr(self, "_codegen_current_stmt_index", -1))
                + " stmt_kind="
                + str(getattr(self, "_codegen_current_stmt_kind", ""))
                + " expr_kind="
                + str(getattr(self, "_codegen_current_expr_kind", ""))
                + "\n"
            )
        except Exception:
            pass
        try:
            _write_line(
                "PCC_CODEGEN_EXCEPTION_DETAIL message="
                + str(exc)
                + " args="
                + repr(getattr(exc, "args", ()))
                + "\n"
            )
        except Exception:
            pass
        try:
            current_stmt = self._codegen_current_stmt_index
        except Exception:
            current_stmt = -1
        try:
            current_stmt_kind = self._codegen_current_stmt_kind
        except Exception:
            current_stmt_kind = ""
        try:
            current_expr_kind = self._codegen_current_expr_kind
        except Exception:
            current_expr_kind = ""
        try:
            _write_line(
                "PCC_CODEGEN_EXCEPTION_CONTEXT "
                + "stmt_index="
                + str(current_stmt)
                + " stmt_kind="
                + current_stmt_kind
                + " expr_kind="
                + current_expr_kind
                + "\n"
            )
        except Exception:
            pass
        ring = getattr(self, "_codegen_trace_ring", [])
        if not isinstance(ring, list):
            ring = []
        if len(ring) == 0:
            return
        capacity = int(getattr(self, "_codegen_trace_capacity", len(ring)))
        if capacity <= 0 or len(ring) < capacity:
            ordered = ring
        else:
            ordered = []
            i = int(getattr(self, "_codegen_trace_next", 0))
            if i < 0:
                i = 0
            for _ in range(len(ring)):
                ordered.append(ring[i])
                i = (i + 1) % len(ring)
        for idx in range(len(ordered)):
            (
                boundary,
                b_module,
                b_function,
                b_stmt_index,
                b_stmt_kind,
                b_expr_kind,
                b_span,
            ) = ordered[idx]
            span_text = ""
            if b_span is not None:
                span_text = b_span.format_compact()
            line = (
                "PCC_CODEGEN_BREADCRUMB "
                + str(idx)
                + " "
                + boundary
                + " module="
                + b_module
                + " function="
                + b_function
                + " stmt_index="
                + b_stmt_index
                + " stmt_kind="
                + b_stmt_kind
                + " expr_kind="
                + b_expr_kind
                + " span="
                + span_text
                + "\n"
            )
            _write_line(line)

    def _codegen_trace_set_context_for_expr(self, expr: Expr) -> None:
        try:
            self._codegen_current_expr_kind = type(expr).__name__
        except Exception:
            self._codegen_current_expr_kind = ""

    def _emit_stmts(self, stmts: tuple[Stmt, ...]) -> None:
        StmtDispatchLoweringMixin._emit_stmts_impl(self, stmts)

    def _emit_stmt(self, stmt: Stmt) -> None:
        # Marker BEFORE _di_locate: the debug-info location stamp is the first
        # thing every statement goes through, so a failure there would leave any
        # later marker unwritten and look like "the loop never ran".
        # `span` (not `line`) is the attribute every statement carries; the
        # earlier marker used `.line` and broke stage1 with
        # "'If' object has no attribute 'line'".
        _sp = getattr(stmt, "span", None)
        _loc = "?"
        if _sp is not None:
            _loc = str(_sp.line) + ":" + str(_sp.col)
        self._prologue_mark("pre_di " + str(type(stmt).__name__) + " @" + _loc)
        self._di_locate(stmt)
        self._prologue_mark("post_di " + str(type(stmt).__name__) + " @" + _loc)
        if self._codegen_trace_is_enabled():
            stmt_kind = ""
            stmt_index = self._codegen_current_stmt_index
            try:
                stmt_kind = type(stmt).__name__
            except Exception:
                stmt_kind = ""
            self._codegen_current_stmt_kind = stmt_kind
            self._codegen_trace_push(
                "stmt",
                stmt_index,
                stmt_kind,
                "",
                self._codegen_trace_span(stmt),
            )
        payload_scope = self._statement_uses_managed_valueclass(stmt)
        previous_error = self._try_err_block
        previous_cleanup = self._cpy_operand_cleanup_block
        scope_function = self.current_function
        temporary_start = len(self._valueclass_payload_temporaries.get(scope_function.name, ()))
        scope_error = None
        scope_target = None
        if payload_scope:
            scope_target = self._current_try_err_block()
            if scope_target is None:
                scope_target = self._ensure_fn_err_exit()
            scope_error = scope_function.append_basic_block(self._fresh("value.statement.error"))
            self._try_err_block = scope_error
            self._cpy_operand_cleanup_block = scope_error
        try:
            StmtDispatchLoweringMixin._emit_stmt_impl(self, stmt)
        except BaseException as exc:
            self._codegen_trace_dump(exc)
            raise
        finally:
            self._try_err_block = previous_error
            self._cpy_operand_cleanup_block = previous_cleanup
        if payload_scope:
            temporaries = self._valueclass_payload_temporaries.get(scope_function.name, ())[temporary_start:]
            roots = tuple(root for slot, _ty in temporaries for root in self._valueclass_payload_owned_roots(slot))
            if not self._builder_block_is_terminated():
                self._release_slot_call_roots(roots)
            continuation = self.builder._block
            cleanup = self._slot_call_cleanup_block(roots, scope_target)
            self.builder.position_at_end(scope_error)
            self.builder.branch(cleanup)
            self.builder.position_at_end(continuation)

    def _emit_expr(self, expr: Expr) -> ir.Value:
        if self._codegen_trace_is_enabled():
            expr_kind = ""
            try:
                expr_kind = type(expr).__name__
            except Exception:
                expr_kind = ""
            self._codegen_current_expr_kind = expr_kind
            self._codegen_trace_set_context_for_expr(expr)
            self._codegen_trace_push(
                "expr",
                self._codegen_current_stmt_index,
                self._codegen_current_stmt_kind,
                expr_kind,
                self._codegen_trace_span(expr),
            )
        try:
            return ExprDispatchLoweringMixin._emit_expr_impl(self, expr)
        except BaseException as exc:
            self._codegen_trace_dump(exc)
            raise


__all__ = ["L1CodeGenEntrypointMixin"]
