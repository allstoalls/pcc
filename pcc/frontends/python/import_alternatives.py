"""Static bindings for imports guarded by ``except ImportError``.

``try: from a import X`` followed by ``except ImportError: from b import X``
binds ``X`` from ``a`` whenever ``a`` imports; the handler runs only after
that import raised. A compiler that binds such names statically must follow
the first alternative the closure provides, not the textually last import:
otherwise code uses ``b``'s never-initialized class or function while the
program actually executed ``a``.

Only a handler's own top-level ``from ... import`` of a name its try body also
imports at top level is a fallback. Every other import keeps its binding.
"""

from __future__ import annotations

from pcc.frontends.python.py_ast import (
    ClassDef,
    For,
    FuncDef,
    If,
    ImportFrom,
    Name,
    Try,
    TupleExpr,
    While,
    With,
)

# Exception classes whose handlers also run when an import raises.
_IMPORT_ERROR_CATCHERS = ("ImportError", "ModuleNotFoundError", "Exception", "BaseException")


def import_statement_key(stmt) -> tuple:
    """Identify an import across AST rewrites, which keep source spans."""
    span = stmt.span
    return (span.file, span.line, span.col)


def _handler_catches_import_error(handler) -> bool:
    exc_type = handler.exc_type
    if exc_type is None:
        return True
    if isinstance(exc_type, Name):
        return exc_type.ident in _IMPORT_ERROR_CATCHERS
    if isinstance(exc_type, TupleExpr):
        for element in exc_type.elems:
            if isinstance(element, Name) and element.ident in _IMPORT_ERROR_CATCHERS:
                return True
    return False


def import_fallbacks(statements) -> dict:
    """Map each fallback import's key to ``{local_name: (primary, attr)}``.

    ``primary`` is the try body's ``ImportFrom`` binding the same local name
    and ``attr`` the name it imports; callers decide whether that primary
    resolves inside the closure.
    """
    fallbacks: dict = {}
    pending = list(statements)
    while pending:
        stmt = pending.pop()
        if isinstance(stmt, Try):
            primaries: dict = {}
            for body_stmt in stmt.body:
                if isinstance(body_stmt, ImportFrom):
                    for attr_name, as_name in body_stmt.names:
                        if attr_name != "*":
                            primaries[as_name or attr_name] = (body_stmt, attr_name)
            for handler in stmt.handlers:
                if primaries and _handler_catches_import_error(handler):
                    for handler_stmt in handler.body:
                        if not isinstance(handler_stmt, ImportFrom):
                            continue
                        names: dict = {}
                        for attr_name, as_name in handler_stmt.names:
                            local_name = as_name or attr_name
                            if local_name in primaries:
                                names[local_name] = primaries[local_name]
                        if names:
                            fallbacks[import_statement_key(handler_stmt)] = names
                pending.extend(handler.body)
            pending.extend(stmt.body)
            pending.extend(stmt.else_body)
            pending.extend(stmt.finally_body)
        elif isinstance(stmt, (If, While, For)):
            pending.extend(stmt.body)
            pending.extend(stmt.else_body)
        elif isinstance(stmt, (With, FuncDef, ClassDef)):
            pending.extend(stmt.body)
    return fallbacks

