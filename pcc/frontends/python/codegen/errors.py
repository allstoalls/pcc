"""Shared codegen exceptions."""
from __future__ import annotations

class L1CodegenError(Exception):
    """Raised when L1 cannot handle an AST shape it should have."""


class CPythonValueRequired(L1CodegenError):
    """A native lowering met a CPython-domain value it has no bridge for.

    The value came from CPython-backed code, so under strict no-libpython
    compilation the function holding it becomes the fail-closed stub
    (_strict_stub_user_function_with_cpy_fallback) instead of failing the
    whole compile; otherwise this is an ordinary codegen error.
    """


class CodegenDiagnosticError(Exception):
    """Codegen failure carrying the exact span used by structured diagnostics."""

    def __init__(
        self,
        message: str,
        diagnostic_span,
        original_exception_type: str,
    ) -> None:
        super().__init__(message)
        self.diagnostic_span = diagnostic_span
        self.original_exception_type = original_exception_type
