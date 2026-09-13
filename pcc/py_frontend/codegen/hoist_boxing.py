"""Closure-cell boxing transforms for nested-function hoisting.

These helpers operate only on typed AST nodes.  The hoist pass supplies its
free-name analyzer and the per-codegen boxed-parameter table explicitly, so
this module does not depend on the broad Layer1 codegen object.
"""

from __future__ import annotations

from dataclasses import replace as _replace

from ..py_ast import (
    Assign,
    AugAssign,
    Call,
    ClassDef,
    Global,
    Nonlocal,
    DynType,
    ExprStmt,
    For,
    FuncDef,
    If,
    IntLit,
    IntType,
    Import,
    TupleExpr,
    ListExpr,
    ListType,
    Name,
    NoneLit,
    NoneType,
    Return,
    Subscript,
    Try,
    While,
    With,
)
from .hoist_analysis import (
    _dataclass_field_names,
    _dataclass_field_value,
    _is_import_from_stmt,
    _import_names_from_stmt,
    append_name_once,
    clone_funcdef,
    name_in,
)


_DYN = DynType(name="dyn")


def collect_scope_bindings(stmts):
    """Return names bound somewhere in the current lexical scope."""
    bindings = []

    def add_target_names(t):
        if isinstance(t, Name):
            append_name_once(bindings, t.ident)
        elif (
            isinstance(t, Call)
            and isinstance(t.func, Name)
            and t.func.ident in ("*", "__starred__")
            and t.args
        ):
            add_target_names(t.args[0])
        elif isinstance(t, TupleExpr):
            for e in t.elems:
                add_target_names(e)

    def walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, Assign):
                for target in stmt.targets:
                    add_target_names(target)
            elif isinstance(stmt, For):
                add_target_names(stmt.target)
            elif isinstance(stmt, With):
                for _, as_var in stmt.items:
                    if as_var is not None:
                        add_target_names(as_var)
            elif isinstance(stmt, Import):
                for mod_name, asname in stmt.names:
                    bound = asname or mod_name.split(".", 1)[0]
                    if bound:
                        append_name_once(bindings, bound)
            elif _is_import_from_stmt(stmt):
                for imported_name, asname in _import_names_from_stmt(stmt):
                    if imported_name != "*":
                        append_name_once(bindings, asname or imported_name)
            elif isinstance(stmt, (FuncDef, ClassDef)):
                append_name_once(bindings, stmt.name)
            if isinstance(stmt, (If, While, For, With, Try)):
                for field in ("body", "else_body", "finally_body"):
                    walk(_dataclass_field_value(stmt, field, ()))
                for handler in _dataclass_field_value(stmt, "handlers", ()):
                    handler_name = _dataclass_field_value(handler, "name", "")
                    if handler_name:
                        append_name_once(bindings, handler_name)
                    walk(_dataclass_field_value(handler, "body", ()))

    walk(stmts)
    return tuple(bindings)



def scope_declared_names(stmts, include_global, include_nonlocal):
    out = []
    for stmt in stmts:
        if ((include_global and isinstance(stmt, Global))
                or (include_nonlocal and isinstance(stmt, Nonlocal))):
            for name in stmt.names:
                append_name_once(out, name)
        elif isinstance(stmt, (If, While, For, With, Try)):
            for field in ("body", "else_body", "finally_body"):
                block = _dataclass_field_value(stmt, field, ())
                for name in scope_declared_names(block, include_global, include_nonlocal):
                    append_name_once(out, name)
            for handler in _dataclass_field_value(stmt, "handlers", ()):
                for name in scope_declared_names(
                        _dataclass_field_value(handler, "body", ()), include_global, include_nonlocal):
                    append_name_once(out, name)
    return tuple(out)


def function_local_bindings(fd):
    external = scope_declared_names(fd.body, True, True)
    names = []
    for arg in fd.args:
        if arg.name and not name_in(external, arg.name):
            append_name_once(names, arg.name)
    for name in collect_scope_bindings(fd.body):
        if not name_in(external, name):
            append_name_once(names, name)
    return tuple(names)


def function_boxed_names(fd, requested):
    local = function_local_bindings(fd)
    return tuple(name for name in requested if name_in(local, name))

def _box_expr(expr, boxed):
    """Rewrite reads of boxed names through their one-element cell list."""
    int_ty = IntType(name="int")

    def go(node):
        if node is None:
            return node
        if isinstance(node, tuple):
            return tuple(go(item) for item in node)
        if isinstance(node, Name) and node.ident in boxed:
            return Subscript(
                span=node.span,
                ty=_DYN,
                obj=_replace(node, ty=_DYN),
                idx=IntLit(span=node.span, ty=int_ty, value=0),
            )
        if isinstance(node, Call):
            new_args = []
            for arg in node.args:
                new_args.append(go(arg))
            new_kwargs = []
            for key, value in node.kwargs:
                new_kwargs.append((key, go(value)))
            return _replace(
                node,
                func=go(node.func),
                args=tuple(new_args),
                kwargs=tuple(new_kwargs),
            )
        fields = _dataclass_field_names(node)
        if not fields:
            return node
        new_fields = {}
        for slot in fields:
            value = _dataclass_field_value(node, slot, None)
            if slot == "span":
                continue
            new_fields[slot] = go(value)
        if new_fields:
            return _replace(node, **new_fields)
        return node

    return go(expr)


def _box_stmts(stmts, boxed, boxed_function_defs=None):
    """Rewrite reads and writes of boxed names through their cell list."""
    int_ty = IntType(name="int")

    def make_sub(name_ident, span, ty):
        return Subscript(
            span=span,
            ty=ty,
            obj=Name(span=span, ty=_DYN, ident=name_ident),
            idx=IntLit(span=span, ty=int_ty, value=0),
        )

    def box_target(target):
        if isinstance(target, Name) and target.ident in boxed:
            return make_sub(target.ident, target.span, target.ty)
        return _box_expr(target, boxed)

    out = []
    for stmt in stmts:
        if isinstance(stmt, Assign):
            new_value = _box_expr(stmt.value, boxed)
            new_targets = []
            for target in stmt.targets:
                new_targets.append(box_target(target))
            out.append(
                _replace(
                    stmt,
                    targets=tuple(new_targets),
                    value=new_value,
                )
            )
            continue
        if isinstance(stmt, AugAssign):
            new_value = _box_expr(stmt.value, boxed)
            out.append(
                _replace(
                    stmt,
                    target=box_target(stmt.target),
                    value=new_value,
                )
            )
            continue
        if isinstance(stmt, If):
            out.append(
                _replace(
                    stmt,
                    cond=_box_expr(stmt.cond, boxed),
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs),
                )
            )
            continue
        if isinstance(stmt, While):
            out.append(
                _replace(
                    stmt,
                    cond=_box_expr(stmt.cond, boxed),
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs),
                )
            )
            continue
        if isinstance(stmt, For):
            out.append(
                _replace(
                    stmt,
                    target=box_target(stmt.target),
                    iter=_box_expr(stmt.iter, boxed),
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs),
                )
            )
            continue
        if isinstance(stmt, Try):
            new_handlers = []
            for handler in stmt.handlers:
                new_handlers.append(
                    _replace(
                        handler,
                        body=_box_stmts(
                            _dataclass_field_value(handler, "body", ()),
                            boxed,
                            boxed_function_defs,
                        ),
                    )
                )
            out.append(
                _replace(
                    stmt,
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs),
                    finally_body=_box_stmts(stmt.finally_body, boxed, boxed_function_defs),
                    handlers=tuple(new_handlers),
                )
            )
            continue
        if isinstance(stmt, With):
            out.append(_replace(stmt, body=_box_stmts(stmt.body, boxed, boxed_function_defs)))
            continue
        if isinstance(stmt, ExprStmt):
            out.append(_replace(stmt, expr=_box_expr(stmt.expr, boxed)))
            continue
        if isinstance(stmt, Return):
            if stmt.value is None:
                out.append(stmt)
            else:
                out.append(_replace(stmt, value=_box_expr(stmt.value, boxed)))
            continue
        if isinstance(stmt, FuncDef):
            # A child binding shadows this scope's cell. Its own cells are
            # allocated when that function is hoisted, under its final name.
            shadowed = function_local_bindings(stmt) + scope_declared_names(stmt.body, True, False)
            inherited = tuple(name for name in boxed if not name_in(shadowed, name))
            rewritten = clone_funcdef(
                stmt, stmt.name, stmt.args, stmt.return_ty,
                _box_stmts(stmt.body, inherited, boxed_function_defs),
            )
            if boxed_function_defs is not None and name_in(boxed, stmt.name):
                # A def binds its closure cell just like an assignment. Keep
                # the AST alive with its identity until hoisting publishes it.
                boxed_function_defs[id(rewritten)] = rewritten
            out.append(rewritten)
            continue
        out.append(stmt)
    return tuple(out)


def box_outer_body(
    body,
    owner_name,
    param_names,
    boxed_names,
    closure_boxed_params,
    boxed_function_defs=None,
):
    """Apply pcc's list-cell closure representation to one outer body."""
    filtered = []
    for name in boxed_names:
        if name != "__class__":
            append_name_once(filtered, name)
    boxed = tuple(filtered)
    if not boxed:
        return body

    boxed_param_names = []
    for name in boxed:
        if name_in(param_names, name):
            boxed_param_names.append(name)
    boxed_params = tuple(boxed_param_names)
    if boxed_params:
        closure_boxed_params[owner_name] = boxed_params

    rewritten = _box_stmts(body, boxed, boxed_function_defs)
    span = body[0].span if body else None
    sentinels = []
    for name in sorted(boxed):
        if name_in(param_names, name):
            continue
        sentinels.append(
            Assign(
                span=span,
                targets=(Name(span=span, ty=_DYN, ident=name),),
                value=ListExpr(
                    span=span,
                    ty=ListType(name="list", elem=DynType(name="dyn")),
                    elems=(NoneLit(span=span, ty=NoneType(name="None")),),
                ),
                annotation=None,
            )
        )
    return tuple(sentinels) + rewritten
