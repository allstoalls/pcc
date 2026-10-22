"""Pure AST predicates used by nested-function hoisting."""

from __future__ import annotations

from pcc.frontends.python.py_ast import Assign, Attr, AugAssign, BinOp, BoolExpr, Break, Call, ClassDef, Compare, Continue, Delete, DictExpr, ExprStmt, For, FuncDef, Global, If, IfExpr, Import, ImportFrom, Lambda, ListExpr, Name, Nonlocal, Pass, Raise, Return, Slice, Subscript, Try, TupleExpr, UnaryOp, While, With
from pcc.frontends.python.codegen.hoist_analysis import _dataclass_field_names, _dataclass_field_value


_YIELD_SENTINELS = (
    "_yield",
    "_yield_from",
    "__yield__",
    "__yield_from__",
)


def _expr_has_yield(expr):
    if expr is None:
        return False
    if (
        isinstance(expr, Call)
        and isinstance(expr.func, Name)
        and expr.func.ident in _YIELD_SENTINELS
    ):
        return True
    if isinstance(expr, Call):
        if _expr_has_yield(expr.func):
            return True
        for arg in expr.args:
            if _expr_has_yield(arg):
                return True
        for _key, value in expr.kwargs:
            if _expr_has_yield(value):
                return True
        return False
    if isinstance(expr, Attr):
        return _expr_has_yield(expr.obj)
    if isinstance(expr, Subscript):
        return _expr_has_yield(expr.obj) or _expr_has_yield(expr.idx)
    if isinstance(expr, Slice):
        return (
            _expr_has_yield(expr.lo)
            or _expr_has_yield(expr.hi)
            or _expr_has_yield(expr.step)
        )
    if isinstance(expr, BinOp):
        return _expr_has_yield(expr.lhs) or _expr_has_yield(expr.rhs)
    if isinstance(expr, UnaryOp):
        return _expr_has_yield(expr.operand)
    if isinstance(expr, Compare):
        return _expr_has_yield(expr.lhs) or _expr_has_yield(expr.rhs)
    if isinstance(expr, BoolExpr):
        return _expr_has_yield(expr.left) or _expr_has_yield(expr.right)
    if isinstance(expr, (ListExpr, TupleExpr)):
        for item in expr.elems:
            if _expr_has_yield(item):
                return True
        return False
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            if _expr_has_yield(key) or _expr_has_yield(value):
                return True
        return False
    if isinstance(expr, IfExpr):
        return (
            _expr_has_yield(expr.cond)
            or _expr_has_yield(expr.then_e)
            or _expr_has_yield(expr.else_e)
        )
    if isinstance(expr, Lambda):
        return False
    return False


def _stmt_has_yield(stmt):
    if isinstance(stmt, (FuncDef, ClassDef)):
        return False
    if isinstance(stmt, ExprStmt):
        return _expr_has_yield(stmt.expr)
    if isinstance(stmt, Assign):
        if _expr_has_yield(stmt.value):
            return True
        for target in stmt.targets:
            if _expr_has_yield(target):
                return True
        return False
    if isinstance(stmt, AugAssign):
        return _expr_has_yield(stmt.target) or _expr_has_yield(stmt.value)
    if isinstance(stmt, Return):
        return _expr_has_yield(stmt.value)
    if isinstance(stmt, If):
        return (
            _expr_has_yield(stmt.cond)
            or body_has_yield(stmt.body)
            or body_has_yield(stmt.else_body)
        )
    if isinstance(stmt, While):
        return (
            _expr_has_yield(stmt.cond)
            or body_has_yield(stmt.body)
            or body_has_yield(stmt.else_body)
        )
    if isinstance(stmt, For):
        return (
            _expr_has_yield(stmt.target)
            or _expr_has_yield(stmt.iter)
            or body_has_yield(stmt.body)
            or body_has_yield(stmt.else_body)
        )
    if isinstance(stmt, Try):
        if body_has_yield(stmt.body):
            return True
        for handler in stmt.handlers:
            if body_has_yield(
                _dataclass_field_value(handler, "body", ())
            ):
                return True
        return body_has_yield(stmt.else_body) or body_has_yield(stmt.finally_body)
    if isinstance(stmt, With):
        for context_expr, as_var in stmt.items:
            if _expr_has_yield(context_expr) or _expr_has_yield(as_var):
                return True
        return body_has_yield(stmt.body)
    if isinstance(stmt, Raise):
        return _expr_has_yield(stmt.exc) or _expr_has_yield(stmt.cause)
    if isinstance(stmt, Delete):
        for target in stmt.targets:
            if _expr_has_yield(target):
                return True
    return False


def body_has_yield(stmts):
    """Return whether this lexical body contains a yield sentinel."""
    for stmt in stmts:
        if _stmt_has_yield(stmt):
            return True
    return False


def _expr_needs_nested_rewrite(expr):
    if expr is None:
        return False
    if isinstance(expr, Lambda):
        return True
    if isinstance(expr, tuple):
        for item in expr:
            if _expr_needs_nested_rewrite(item):
                return True
        return False
    if isinstance(expr, Call):
        if _expr_needs_nested_rewrite(expr.func):
            return True
        for item in expr.args:
            if _expr_needs_nested_rewrite(item):
                return True
        for _key, value in expr.kwargs:
            if _expr_needs_nested_rewrite(value):
                return True
        return False
    if isinstance(expr, Attr):
        return _expr_needs_nested_rewrite(expr.obj)
    if isinstance(expr, Subscript):
        return _expr_needs_nested_rewrite(expr.obj) or _expr_needs_nested_rewrite(
            expr.idx
        )
    if isinstance(expr, Slice):
        return (
            _expr_needs_nested_rewrite(expr.lo)
            or _expr_needs_nested_rewrite(expr.hi)
            or _expr_needs_nested_rewrite(expr.step)
        )
    if isinstance(expr, BinOp):
        return _expr_needs_nested_rewrite(expr.lhs) or _expr_needs_nested_rewrite(
            expr.rhs
        )
    if isinstance(expr, UnaryOp):
        return _expr_needs_nested_rewrite(expr.operand)
    if isinstance(expr, Compare):
        return _expr_needs_nested_rewrite(expr.lhs) or _expr_needs_nested_rewrite(
            expr.rhs
        )
    if isinstance(expr, BoolExpr):
        return _expr_needs_nested_rewrite(expr.left) or _expr_needs_nested_rewrite(
            expr.right
        )
    if isinstance(expr, IfExpr):
        return (
            _expr_needs_nested_rewrite(expr.cond)
            or _expr_needs_nested_rewrite(expr.then_e)
            or _expr_needs_nested_rewrite(expr.else_e)
        )
    if isinstance(expr, (ListExpr, TupleExpr)):
        for item in expr.elems:
            if _expr_needs_nested_rewrite(item):
                return True
        return False
    if isinstance(expr, DictExpr):
        for key, value in expr.pairs:
            if _expr_needs_nested_rewrite(key) or _expr_needs_nested_rewrite(value):
                return True
    return False


def _stmt_needs_nested_rewrite(stmt):
    if isinstance(stmt, (FuncDef, ClassDef)):
        return True
    if isinstance(stmt, If):
        return (
            _expr_needs_nested_rewrite(stmt.cond)
            or body_needs_nested_rewrite(stmt.body)
            or body_needs_nested_rewrite(stmt.else_body)
        )
    if isinstance(stmt, While):
        return (
            _expr_needs_nested_rewrite(stmt.cond)
            or body_needs_nested_rewrite(stmt.body)
            or body_needs_nested_rewrite(stmt.else_body)
        )
    if isinstance(stmt, For):
        return (
            _expr_needs_nested_rewrite(stmt.target)
            or _expr_needs_nested_rewrite(stmt.iter)
            or body_needs_nested_rewrite(stmt.body)
            or body_needs_nested_rewrite(stmt.else_body)
        )
    if isinstance(stmt, Try):
        if body_needs_nested_rewrite(stmt.body):
            return True
        if body_needs_nested_rewrite(stmt.else_body):
            return True
        if body_needs_nested_rewrite(stmt.finally_body):
            return True
        for handler in stmt.handlers:
            if _expr_needs_nested_rewrite(
                _dataclass_field_value(handler, "exc_type", None)
            ):
                return True
            if body_needs_nested_rewrite(
                _dataclass_field_value(handler, "body", ())
            ):
                return True
        return False
    if isinstance(stmt, With):
        for context_expr, as_var in stmt.items:
            if _expr_needs_nested_rewrite(context_expr) or _expr_needs_nested_rewrite(
                as_var
            ):
                return True
        return body_needs_nested_rewrite(stmt.body)
    if isinstance(stmt, Assign):
        for target in stmt.targets:
            if _expr_needs_nested_rewrite(target):
                return True
        return _expr_needs_nested_rewrite(stmt.value)
    if isinstance(stmt, AugAssign):
        return _expr_needs_nested_rewrite(stmt.target) or _expr_needs_nested_rewrite(
            stmt.value
        )
    if isinstance(stmt, ExprStmt):
        return _expr_needs_nested_rewrite(stmt.expr)
    if isinstance(stmt, Return):
        return _expr_needs_nested_rewrite(stmt.value)
    if isinstance(stmt, Raise):
        return _expr_needs_nested_rewrite(stmt.exc) or _expr_needs_nested_rewrite(
            stmt.cause
        )
    if isinstance(stmt, Delete):
        for target in stmt.targets:
            if _expr_needs_nested_rewrite(target):
                return True
        return False
    if isinstance(stmt, (Import, ImportFrom, Global, Nonlocal)):
        return False
    if isinstance(stmt, (Pass, Break, Continue)):
        return False
    for slot in _dataclass_field_names(stmt):
        if slot != "span" and _expr_needs_nested_rewrite(
            _dataclass_field_value(stmt, slot, None)
        ):
            return True
    return False


def body_needs_nested_rewrite(stmts):
    """Return whether a body needs nested-def or lambda rewriting."""
    for stmt in stmts:
        if _stmt_needs_nested_rewrite(stmt):
            return True
    return False


def hoist_stmt_kind(stmt):
    """Stable debug label for the hoist rewrite trace."""
    if isinstance(stmt, If):
        return "If"
    if isinstance(stmt, While):
        return "While"
    if isinstance(stmt, For):
        return "For"
    if isinstance(stmt, Try):
        return "Try"
    if isinstance(stmt, With):
        return "With"
    if isinstance(stmt, Assign):
        return "Assign"
    if isinstance(stmt, AugAssign):
        return "AugAssign"
    if isinstance(stmt, Return):
        return "Return"
    if isinstance(stmt, ExprStmt):
        return "ExprStmt"
    if isinstance(stmt, FuncDef):
        return "FuncDef"
    if isinstance(stmt, ClassDef):
        return "ClassDef"
    return "Stmt"
