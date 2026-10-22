"""Definite assignment of function locals for UnboundLocalError checks.

A load may skip its boundness check only when its name is bound on every path
that reaches it. The analysis is conservative where Python lets a binding
vanish or be skipped: ``del`` and ``except ... as`` cleanup unbind, a loop head
meets every back edge (a later iteration can see an earlier ``del``), a handler
starts from the ``try`` entry minus everything the body could delete, and a
``with`` body may be cut short by a suppressing ``__exit__``. Walrus and
comprehension bindings never count, and loads inside nested scopes are never
marked. ``None`` stands for an unreachable point.
"""

from __future__ import annotations

from pcc.frontends.python.py_ast import (
    Assign,
    AugAssign,
    Break,
    Call,
    ClassDef,
    Continue,
    Delete,
    ExprStmt,
    For,
    FuncDef,
    Global,
    If,
    Import,
    ImportFrom,
    Lambda,
    ListExpr,
    Name,
    Nonlocal,
    Pass,
    Raise,
    Return,
    Try,
    TupleExpr,
    While,
    With,
)
from pcc.frontends.python.codegen.hoist_analysis import _dataclass_field_names, _dataclass_field_value

# Synthetic calls whose operands belong to another scope or bind conditionally.
_SCOPED_CALLS = ("_list_comp", "_dict_comp", "_set_comp", "_gen_comp", "_walrus", "__walrus__")
_STAR_CALLS = ("*", "__starred__")
_SKIPPED_FIELDS = ("span", "ty", "annotation", "return_ty")


def load_key(name):
    """Identify one source load; a synthesized node without a span has none."""
    span = name.span
    if span is None:
        return None
    return (name.ident, span.line, span.col, span.end_line, span.end_col)


class _Context:
    def __init__(self, excluded: set) -> None:
        self.safe: set = set()
        self.excluded = excluded
        self.suppress = 0
        # One [break states, continue states] pair per enclosing loop.
        self.loops: list = []


def definitely_bound_loads(fd) -> set:
    """Keys of loads (and ``del`` targets) of names bound on every path."""
    ctx = _Context(_nested_nonlocal_names(fd.body))
    state: set = set()
    for arg in fd.args:
        if arg.name:
            state.add(arg.name)
    _block(fd.body, state, ctx)
    return ctx.safe


def _meet(left, right):
    if left is None:
        if right is None:
            return None
        return set(right)
    if right is None:
        return set(left)
    return left & right


def _block(stmts, state, ctx):
    current = state
    for stmt in stmts:
        if current is None:
            return None
        current = _stmt(stmt, current, ctx)
    return current


def _stmt(stmt, state, ctx):
    if isinstance(stmt, Assign):
        if stmt.has_value:
            _expr(stmt.value, state, ctx)
        out = set(state)
        for target in stmt.targets:
            _target_loads(target, state, ctx)
            if stmt.has_value:
                _bind_target(target, out)
        return out
    if isinstance(stmt, AugAssign):
        out = set(state)
        if isinstance(stmt.target, Name):
            _mark(stmt.target, state, ctx)
            out.add(stmt.target.ident)
        else:
            _target_loads(stmt.target, state, ctx)
        _expr(stmt.value, state, ctx)
        return out
    if isinstance(stmt, ExprStmt):
        _expr(stmt.expr, state, ctx)
        return state
    if isinstance(stmt, If):
        _expr(stmt.cond, state, ctx)
        return _meet(_block(stmt.body, state, ctx), _block(stmt.else_body, state, ctx))
    if isinstance(stmt, While):
        return _loop(stmt, state, ctx, None)
    if isinstance(stmt, For):
        _expr(stmt.iter, state, ctx)
        return _loop(stmt, state, ctx, stmt.target)
    if isinstance(stmt, Return):
        if stmt.value is not None:
            _expr(stmt.value, state, ctx)
        return None
    if isinstance(stmt, Raise):
        if stmt.exc is not None:
            _expr(stmt.exc, state, ctx)
        if stmt.cause is not None:
            _expr(stmt.cause, state, ctx)
        return None
    if isinstance(stmt, Break):
        if ctx.loops:
            ctx.loops[-1][0].append(state)
        return None
    if isinstance(stmt, Continue):
        if ctx.loops:
            ctx.loops[-1][1].append(state)
        return None
    if isinstance(stmt, (Pass, Global, Nonlocal)):
        return state
    if isinstance(stmt, Import):
        out = set(state)
        for module_name, alias in stmt.names:
            out.add(alias or module_name.split(".", 1)[0])
        return out
    if isinstance(stmt, ImportFrom):
        out = set(state)
        for attr_name, alias in stmt.names:
            if attr_name != "*":
                out.add(alias or attr_name)
        return out
    if isinstance(stmt, (FuncDef, ClassDef)):
        out = set(state)
        out.add(stmt.name)
        return out
    if isinstance(stmt, Delete):
        out = set(state)
        for target in stmt.targets:
            _delete_target(target, out, ctx)
        return out
    if isinstance(stmt, With):
        return _with(stmt, state, ctx)
    if isinstance(stmt, Try):
        return _try(stmt, state, ctx)
    # An unknown statement binds nothing provable and may unbind anything
    # it deletes.
    return state - _deleted_names((stmt,))


def _loop(stmt, state, ctx, target):
    entry = state
    done = False
    while not done:
        ctx.suppress += 1
        frame = _loop_pass(stmt, entry, ctx, target)
        ctx.suppress -= 1
        back = frame[2]
        for continued in frame[1]:
            back = _meet(back, continued)
        new_entry = entry if back is None else entry & back
        done = new_entry == entry
        entry = new_entry
    frame = _loop_pass(stmt, entry, ctx, target)
    out = _block(stmt.else_body, entry, ctx)
    for broken in frame[0]:
        out = _meet(out, broken)
    return out


def _loop_pass(stmt, entry, ctx, target):
    frame = [[], []]
    ctx.loops.append(frame)
    if isinstance(stmt, While):
        _expr(stmt.cond, entry, ctx)
        body_in = entry
    else:
        _target_loads(target, entry, ctx)
        body_in = set(entry)
        _bind_target(target, body_in)
    end = _block(stmt.body, body_in, ctx)
    ctx.loops.pop()
    return [frame[0], frame[1], end]


def _with(stmt, state, ctx):
    current = state
    for context_expr, as_var in stmt.items:
        _expr(context_expr, current, ctx)
        if as_var is not None:
            _target_loads(as_var, current, ctx)
            current = set(current)
            _bind_target(as_var, current)
    body_out = _block(stmt.body, current, ctx)
    # A suppressing __exit__ resumes after the statement from any point of
    # the body; only the entry bindings it cannot delete survive that edge.
    return _meet(body_out, current - _deleted_names(stmt.body))


def _try(stmt, state, ctx):
    marks = []
    for frame in ctx.loops:
        marks.append((len(frame[0]), len(frame[1])))
    body_deleted = _deleted_names(stmt.body)
    body_out = _block(stmt.body, state, ctx)
    out = None
    if body_out is not None:
        out = _block(stmt.else_body, body_out, ctx)
    handler_in = state - body_deleted
    handler_deleted: set = set()
    for handler in stmt.handlers:
        if handler.exc_type is not None:
            _expr(handler.exc_type, handler_in, ctx)
        entry = handler_in
        if handler.name:
            entry = set(handler_in)
            entry.add(handler.name)
            handler_deleted.add(handler.name)
        handler_deleted |= _deleted_names(handler.body)
        handler_out = _block(handler.body, entry, ctx)
        if handler_out is not None and handler.name:
            handler_out = handler_out - {handler.name}
        out = _meet(out, handler_out)
    if not stmt.finally_body:
        return out
    # Every exit path runs the finally body; mark it from the smallest entry.
    finally_entry = state - body_deleted - handler_deleted - _deleted_names(stmt.else_body)
    _block(stmt.finally_body, finally_entry, ctx)
    finally_deleted = _deleted_names(stmt.finally_body)
    index = 0
    while index < len(marks):
        frame = ctx.loops[index]
        for slot in (0, 1):
            position = marks[index][slot]
            while position < len(frame[slot]):
                frame[slot][position] = frame[slot][position] - finally_deleted
                position += 1
        index += 1
    if out is None:
        return None
    ctx.suppress += 1
    out = _block(stmt.finally_body, out, ctx)
    ctx.suppress -= 1
    return out


def _mark(name, state, ctx) -> None:
    if ctx.suppress == 0 and name.ident in state and name.ident not in ctx.excluded:
        key = load_key(name)
        if key is not None:
            ctx.safe.add(key)


def _expr(expr, state, ctx) -> None:
    pending = [expr]
    while pending:
        node = pending.pop()
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            continue
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, Name):
            _mark(node, state, ctx)
            continue
        if isinstance(node, (Lambda, FuncDef, ClassDef)):
            continue
        if isinstance(node, Call) and isinstance(node.func, Name) and node.func.ident in _SCOPED_CALLS:
            continue
        for field in _dataclass_field_names(node):
            if field not in _SKIPPED_FIELDS:
                pending.append(_dataclass_field_value(node, field, None))


def _target_loads(target, state, ctx) -> None:
    """Loads a store target performs before binding (object/index operands)."""
    if isinstance(target, Name):
        return
    if isinstance(target, (TupleExpr, ListExpr)):
        for child in target.elems:
            _target_loads(child, state, ctx)
        return
    if isinstance(target, Call) and isinstance(target.func, Name) and target.func.ident in _STAR_CALLS:
        for child in target.args:
            _target_loads(child, state, ctx)
        return
    _expr(target, state, ctx)


def _bind_target(target, out: set) -> None:
    if isinstance(target, Name):
        out.add(target.ident)
    elif isinstance(target, (TupleExpr, ListExpr)):
        for child in target.elems:
            _bind_target(child, out)
    elif isinstance(target, Call) and isinstance(target.func, Name) and target.func.ident in _STAR_CALLS:
        for child in target.args:
            _bind_target(child, out)


def _delete_target(target, out: set, ctx) -> None:
    if isinstance(target, Name):
        _mark(target, out, ctx)
        out.discard(target.ident)
    elif isinstance(target, (TupleExpr, ListExpr)):
        for child in target.elems:
            _delete_target(child, out, ctx)
    else:
        _expr(target, out, ctx)


def _deleted_names(stmts) -> set:
    """Names any statement here can unbind: ``del`` and handler cleanup."""
    names: set = set()
    pending = list(stmts)
    while pending:
        node = pending.pop()
        if isinstance(node, Delete):
            targets = list(node.targets)
            while targets:
                target = targets.pop()
                if isinstance(target, Name):
                    names.add(target.ident)
                elif isinstance(target, (TupleExpr, ListExpr)):
                    targets.extend(target.elems)
        elif isinstance(node, Try):
            pending.extend(node.body)
            pending.extend(node.else_body)
            pending.extend(node.finally_body)
            for handler in node.handlers:
                if handler.name:
                    names.add(handler.name)
                pending.extend(handler.body)
        elif isinstance(node, (If, While, For)):
            pending.extend(node.body)
            pending.extend(node.else_body)
        elif isinstance(node, With):
            pending.extend(node.body)
    return names


def _nested_nonlocal_names(stmts) -> set:
    """Locals a nested function may rebind or delete through ``nonlocal``."""
    names: set = set()
    pending = list(stmts)
    while pending:
        node = pending.pop()
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            continue
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, Nonlocal):
            for name in node.names:
                names.add(name)
            continue
        for field in _dataclass_field_names(node):
            if field not in _SKIPPED_FIELDS:
                pending.append(_dataclass_field_value(node, field, None))
    return names
