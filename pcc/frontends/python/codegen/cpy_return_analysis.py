"""CPython result-space analysis helpers for Layer-1 calls."""

from __future__ import annotations

from typing import Optional

from pcc.frontends.python.py_ast import Attr, BinOp, Call, Expr, For, If, Lambda, Name, Return, Stmt, Subscript, Try, While, With
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.cpy_import_state import live_import_name_slot

_CPY_BUILTIN_FALLBACK = frozenset(
    {
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "vars",
        "locals",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
    }
)


class CpyReturnAnalysisMixin:
    def _expr_looks_cpython(self, expr: Expr) -> bool:
        """Best-effort predicate for expressions that already produce a
        CPython PyObject* at runtime."""
        if isinstance(expr, Name):
            # A live managed import is loaded from the executing native
            # module dictionary. A different import of the same package root
            # can leave historical CPython alias metadata behind, but that
            # metadata does not describe this binding or its call results.
            # Match name/call lowering; a distinct lexical local still uses
            # its own representation through live_import_name_slot's check.
            if live_import_name_slot(self, expr.ident) is not None:
                return False
            if expr.ident in getattr(self, "_cpy_module_env", {}):
                return True
            if expr.ident in getattr(self, "_cpy_star_module_env", {}):
                return True
            if expr.ident in self._native_module_aliases:
                return False
            if getattr(self, "_cpy_module_flags", {}).get(expr.ident, False):
                return True
            if getattr(self, "_cpy_env_flags", {}).get(expr.ident, False):
                return True
            return False
        if isinstance(expr, Attr):
            if self._native_module_expr_export_info(expr.obj, expr.name) is not None:
                return False
            return self._expr_looks_cpython(expr.obj)
        if isinstance(expr, BinOp):
            # A binary op with a CPython operand produces a CPython object
            # (py_cpy_binop dispatches over libpython PyNumber_*), so the
            # result is itself cpy. Lets ``(a + b).sum()`` route the chained
            # method through libpython. Inert in no-libpython (no cpy modules
            # => operands never look cpy => returns False).
            return self._expr_looks_cpython(expr.lhs) or self._expr_looks_cpython(
                expr.rhs
            )
        if isinstance(expr, Subscript):
            return self._expr_looks_cpython(expr.obj)
        if isinstance(expr, Call):
            if isinstance(expr.func, Name):
                if (expr.func.ident == "dir" and expr.args
                        and not self._iterator_builtin_is_shadowed("dir")):
                    # Native reflection owns the list and each yielded name.
                    # Only an explicitly foreign operand keeps the CPython ABI.
                    return self._expr_looks_cpython(expr.args[0])
                if expr.func.ident == "getattr" and expr.args:
                    return self._expr_looks_cpython(expr.args[0])
                if expr.func.ident == "type" and len(expr.args) == 1:
                    # ``type(cpy)`` resolves to the real CPython type object
                    # (lowered as ``py_cpy_getattr(x, "__class__")``), so its
                    # attribute loads — ``type(a).__name__`` etc. — must route
                    # through libpython as well. Inert in no-libpython, where
                    # the argument never looks cpy.
                    return self._expr_looks_cpython(expr.args[0])
                if expr.func.ident in _CPY_BUILTIN_FALLBACK:
                    return True
                return self._expr_looks_cpython(expr.func)
            if isinstance(expr.func, Attr):
                if (
                    self._native_module_expr_export_info(
                        expr.func.obj,
                        expr.func.name,
                    )
                    is not None
                ):
                    return False
                return self._expr_looks_cpython(expr.func.obj)
            return False
        return False

    def _collect_return_exprs(self, stmts: tuple[Stmt, ...]) -> list[Expr]:
        """Best-effort recursive return collector for a user function."""
        out: list[Expr] = []

        pending = [stmts]
        while pending:
            block = pending.pop()
            for stmt in block:
                if isinstance(stmt, Return):
                    if stmt.value is not None:
                        out.append(stmt.value)
                    continue
                if isinstance(stmt, (If, While, For)):
                    pending.append(stmt.else_body)
                    pending.append(stmt.body)
                    continue
                if isinstance(stmt, Try):
                    pending.append(stmt.finally_body)
                    pending.append(stmt.else_body)
                    for handler in stmt.handlers:
                        pending.append(handler.body)
                    pending.append(stmt.body)
                    continue
                if isinstance(stmt, With):
                    pending.append(stmt.body)
        return out

    def _callable_expr_returns_cpython(self, expr: Expr) -> bool:
        """Best-effort predicate for callables whose call result stays
        in CPython object space."""
        if getattr(self, "_strict_no_libpython", False):
            # See _user_func_returns_cpython: such a callee is a stub.
            return False
        if isinstance(expr, Lambda):
            return self._expr_looks_cpython(expr.body)
        if isinstance(expr, Name):
            try:
                return self._user_func_returns_cpython(
                    self._find_user_funcdef(expr.ident),
                )
            except L1CodegenError:
                return False
        return False

    def _return_expr_looks_cpython(
        self,
        expr: Expr,
        call_arg_map: dict[str, Expr],
    ) -> bool:
        if self._expr_looks_cpython(expr):
            return True
        if isinstance(expr, Call) and isinstance(expr.func, Name):
            if expr.func.ident == "getattr" and expr.args:
                return self._expr_looks_cpython(expr.args[0])
            actual = call_arg_map.get(expr.func.ident)
            if actual is not None:
                return self._callable_expr_returns_cpython(actual)
        return False

    def _user_func_returns_cpython(
        self,
        ast_fd,
        formals=(),
        actual_args: Optional[list[Expr]] = None,
    ) -> bool:
        """Return True when ``ast_fd`` obviously returns a CPython value.

        This is intentionally narrow; it exists to preserve CPython
        result tagging across direct calls to hoisted nested helpers
        synthesized from lambdas / local defs, plus thin wrappers like
        ``_timed(..., fn)`` that return ``fn()``.

        Strict no-libpython compilation replaces a body that produces a
        CPython value with the fail-closed stub
        (_strict_stub_user_function_with_cpy_fallback), so the call raises
        and never returns one: its result keeps the native object ABI.
        """
        if getattr(self, "_strict_no_libpython", False):
            return False
        ret_exprs = self._collect_return_exprs(getattr(ast_fd, "body", ()))
        if not ret_exprs:
            return False
        call_arg_map: dict[str, Expr] = {}
        if actual_args is not None:
            for formal, actual in zip(formals, actual_args):
                if getattr(formal, "name", ""):
                    call_arg_map[formal.name] = actual
        return all(
            self._return_expr_looks_cpython(expr, call_arg_map) for expr in ret_exprs
        )
