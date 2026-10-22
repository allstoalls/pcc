"""Closure-cell boxing transforms for nested-function hoisting.

These helpers operate only on typed AST nodes.  The hoist pass supplies its
free-name analyzer and the per-codegen boxed-parameter table explicitly, so
this module does not depend on the broad Layer1 codegen object.
"""

from __future__ import annotations

from dataclasses import replace as _replace

from pcc.frontends.python.py_ast import (
    Assign,
    AugAssign,
    BoolLit,
    BoolType,
    Call,
    ClassDef,
    Delete,
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
    Lambda,
    TupleExpr,
    TupleType,
    ListExpr,
    ListType,
    Name,
    Raise,
    Return,
    StrLit,
    StrType,
    Subscript,
    Try,
    While,
    With,
)
from pcc.frontends.python.codegen.hoist_analysis import _dataclass_field_names, _dataclass_field_value, _is_import_from_stmt, _import_names_from_stmt, append_name_once, clone_funcdef, name_in


_DYN = DynType(name="dyn")

# These are typed-AST operations, not Python-callable magic names. Neither
# identifier can be spelled by a Python source Name.
CELL_READ = "<pcc.cell.read>"
CELL_UNBOUND = "<pcc.cell.unbound>"
CELL_CAPTURE = "<pcc.cell.capture>"


def cell_capture_key(name):
    return ".pcc.capture." + name


def local_method_capture_key(class_name, fd):
    """Key of one method definition of a function-local class.

    A property's getter, setter and deleter share a name, so the source
    position tells the definitions apart.
    """
    span = fd.span
    if span is None:
        return (class_name, fd.name, 0, 0)
    return (class_name, fd.name, span.line, span.col)



def _cell_unbound(span):
    return Call(span=span, ty=_DYN,
                func=Name(span=span, ty=_DYN, ident=CELL_UNBOUND), args=())


def _cell_read(name, span, suffix="", free=False):
    return Call(
        span=span, ty=_DYN,
        func=Name(span=span, ty=_DYN, ident=CELL_READ),
        args=(Name(span=span, ty=_DYN, ident=_cell_ident(name, suffix)),
              StrLit(span=span, ty=StrType(name="str"), value=name),
              BoolLit(span=span, ty=BoolType(name="bool"), value=free)),
    )


def _cell_clear(name, span):
    return Assign(span=span, targets=(_cell_slot(name, span, _DYN),),
                  value=_cell_unbound(span), annotation=None)


_COMPREHENSION_SENTINELS = (
    "__listcomp__",
    "__setcomp__",
    "__genexpr__",
    "__dictcomp__",
    "_list_comp",
    "_set_comp",
    "_gen_comp",
    "_dict_comp",
)

# Prefix of the cell a comprehension binds, once per execution, for a target
# that a lambda inside it captures.  See ``rewrite_comprehension_cells``.
COMPREHENSION_CELL_PREFIX = "__pcc_cell_"

_CELL_SKIP_FIELDS = ("span", "ty", "annotation", "return_ty")


def collect_scope_bindings(stmts):
    """Return binding/deletion names in this lexical owner.

    Function, class and lambda bodies own their bindings. Their executable
    headers belong here; comprehension iteration targets belong to the
    comprehension, while its assignment expressions bind this owner.
    """
    bindings = []

    def walk(node):
        if node is None or _is_scalar(node):
            return
        if isinstance(node, (tuple, list)):
            for item in node:
                walk(item)
            return
        if isinstance(node, Lambda):
            for arg in node.params:
                walk(_dataclass_field_value(arg, "default", None))
            return
        if isinstance(node, FuncDef):
            append_name_once(bindings, node.name)
            walk(node.decorators)
            for arg in node.args:
                walk(_dataclass_field_value(arg, "default", None))
            return
        if isinstance(node, ClassDef):
            append_name_once(bindings, node.name)
            walk(node.bases)
            walk(node.keywords)
            walk(node.decorators)
            return
        if isinstance(node, (Assign, Delete)):
            for target in node.targets:
                _target_names(target, bindings)
        elif isinstance(node, AugAssign):
            _target_names(node.target, bindings)
        elif isinstance(node, For):
            _target_names(node.target, bindings)
        elif isinstance(node, With):
            for _, as_var in node.items:
                if as_var is not None:
                    _target_names(as_var, bindings)
        elif isinstance(node, Import):
            for mod_name, asname in node.names:
                bound = asname or mod_name.split(".", 1)[0]
                if bound:
                    append_name_once(bindings, bound)
        elif _is_import_from_stmt(node):
            for imported_name, asname in _import_names_from_stmt(node):
                if imported_name != "*":
                    append_name_once(bindings, asname or imported_name)
        if isinstance(node, Try):
            for handler in node.handlers:
                handler_name = _dataclass_field_value(handler, "name", "")
                if handler_name:
                    append_name_once(bindings, handler_name)
        if _is_walrus(node):
            _target_names(node.args[0], bindings)
            walk(node.args[1])
            return
        for field in _dataclass_field_names(node):
            if field not in _CELL_SKIP_FIELDS:
                walk(_dataclass_field_value(node, field, None))

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


def _collect_import_providers(node, providers, others):
    """Split this owner's bindings into from-import providers and the rest.

    Mirrors ``collect_scope_bindings``: nested function, class and lambda
    bodies own their bindings, while their headers execute here.
    """
    if node is None or _is_scalar(node):
        return
    if isinstance(node, (tuple, list)):
        for item in node:
            _collect_import_providers(item, providers, others)
        return
    if isinstance(node, Lambda):
        for arg in node.params:
            _collect_import_providers(
                _dataclass_field_value(arg, "default", None), providers, others)
        return
    if isinstance(node, FuncDef):
        append_name_once(others, node.name)
        _collect_import_providers(node.decorators, providers, others)
        for arg in node.args:
            _collect_import_providers(
                _dataclass_field_value(arg, "default", None), providers, others)
        return
    if isinstance(node, ClassDef):
        append_name_once(others, node.name)
        _collect_import_providers(node.bases, providers, others)
        _collect_import_providers(node.keywords, providers, others)
        _collect_import_providers(node.decorators, providers, others)
        return
    if _is_import_from_stmt(node):
        prefix = (str(getattr(node, "level", 0) or 0) + ":"
                  + str(getattr(node, "module", "") or "") + ":")
        for imported_name, bound_name in _import_names_from_stmt(node):
            if imported_name == "*":
                continue
            keys = providers.get(bound_name)
            if keys is None:
                providers[bound_name] = [prefix + imported_name]
            else:
                append_name_once(keys, prefix + imported_name)
        return
    if isinstance(node, (Assign, Delete)):
        for target in node.targets:
            _target_names(target, others)
    elif isinstance(node, AugAssign):
        _target_names(node.target, others)
    elif isinstance(node, For):
        _target_names(node.target, others)
    elif isinstance(node, With):
        for _, as_var in node.items:
            if as_var is not None:
                _target_names(as_var, others)
    elif isinstance(node, Import):
        for mod_name, asname in node.names:
            bound = asname or mod_name.split(".", 1)[0]
            if bound:
                append_name_once(others, bound)
    if isinstance(node, Try):
        for handler in node.handlers:
            handler_name = _dataclass_field_value(handler, "name", "")
            if handler_name:
                append_name_once(others, handler_name)
    if _is_walrus(node):
        _target_names(node.args[0], others)
        _collect_import_providers(node.args[1], providers, others)
        return
    for field in _dataclass_field_names(node):
        if field not in _CELL_SKIP_FIELDS:
            _collect_import_providers(
                _dataclass_field_value(node, field, None), providers, others)


def function_import_conflict_names(fd):
    """Names whose function-local from-imports need a runtime binding.

    A from-import of a native sibling export is otherwise a static alias that
    every later use resolves to, independent of control flow.  That is only
    sound while each binding of the name in this function names one provider.
    Another provider, any other binding, a parameter, or a global/nonlocal
    declaration makes the executing binding the only correct source.
    """
    providers = {}
    others = []
    for arg in fd.args:
        if arg.name:
            append_name_once(others, arg.name)
    for name in scope_declared_names(fd.body, True, True):
        append_name_once(others, name)
    _collect_import_providers(fd.body, providers, others)
    conflicts = []
    for name, keys in providers.items():
        if len(keys) > 1 or name_in(others, name):
            append_name_once(conflicts, name)
    return tuple(conflicts)


def stable_capture_parameters(body, param_names):
    """Entry-bound parameters remain bound unless a lexical operation clears them.

    This deliberately over-approximates descendant deletions/handler cleanup:
    shadowing can box an extra parameter, but cannot retain an unsafe by-value
    direct-call capture. Ordinary locals need a stronger definite-assignment
    proof and therefore do not use this fast path.
    """
    unbound = []
    pending = list(body)
    while pending:
        node = pending.pop()
        if node is None or _is_scalar(node):
            continue
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, Delete):
            for target in node.targets:
                _target_names(target, unbound)
        if isinstance(node, Try):
            for handler in node.handlers:
                name = _dataclass_field_value(handler, "name", None)
                if name:
                    append_name_once(unbound, name)
        for field in _dataclass_field_names(node):
            if field not in _CELL_SKIP_FIELDS:
                pending.append(_dataclass_field_value(node, field, None))
    return tuple(name for name in param_names if not name_in(unbound, name))


def function_boxed_names(fd, requested):
    local = function_local_bindings(fd)
    needed = list(requested)
    lambda_reads = []
    _names_read_in_lambdas(fd.body, (), False, lambda_reads)
    stable_params = stable_capture_parameters(fd.body, tuple(arg.name for arg in fd.args))
    for name in lambda_reads:
        if not name_in(stable_params, name):
            append_name_once(needed, name)
    return tuple(name for name in needed if name_in(local, name))


def _is_scalar(node):
    return (
        isinstance(node, str)
        or isinstance(node, bool)
        or isinstance(node, int)
        or isinstance(node, float)
        or isinstance(node, bytes)
    )


def _is_starred(node):
    return (
        isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.ident in ("*", "__starred__")
        and len(node.args) > 0
    )


def _is_walrus(node):
    return (
        isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.ident in ("_walrus", "__walrus__")
        and len(node.args) == 2
    )


def _span_suffix(span):
    return (
        "_" + str(_dataclass_field_value(span, "line", 0))
        + "_" + str(_dataclass_field_value(span, "col", 0))
    )


def _cell_ident(name, suffix):
    if not suffix:
        return name
    return COMPREHENSION_CELL_PREFIX + name + suffix


def _cell_slot(ident, span, ty):
    """``ident[0]``: the cell element, as a store target or a read."""
    return Subscript(
        span=span,
        ty=ty,
        obj=Name(span=span, ty=_DYN, ident=ident),
        idx=IntLit(span=span, ty=IntType(name="int"), value=0),
    )


def _without_names(names, removed):
    if not removed:
        return names
    kept = []
    for name in names:
        if not name_in(removed, name):
            kept.append(name)
    return tuple(kept)


def _target_names(target, out):
    """Collect the plain names a binding target binds (attr/subscript bind none)."""
    if isinstance(target, Name):
        append_name_once(out, target.ident)
    elif isinstance(target, (TupleExpr, ListExpr)):
        for elem in target.elems:
            _target_names(elem, out)
    elif _is_starred(target):
        _target_names(target.args[0], out)


def _comprehension_parts(node):
    """Split a comprehension sentinel call into its heads and clauses.

    Returns ``(heads, clauses)``: ``heads`` are the element expressions, each
    clause is ``(target, iter, ifs, source_clause)``.  Both parser forms are
    accepted; ``_rebuild_comprehension`` reassembles the same form.  Returns
    None for anything that is not a well-formed comprehension.
    """
    if not isinstance(node, Call) or not isinstance(node.func, Name):
        return None
    ident = node.func.ident
    if not name_in(_COMPREHENSION_SENTINELS, ident):
        return None
    clauses = []
    if ident.startswith("__"):
        head_count = 1
        if ident == "__dictcomp__":
            head_count = 2
        if len(node.args) != head_count + 1:
            return None
        gens = node.args[head_count]
        if not isinstance(gens, TupleExpr):
            return None
        for clause in gens.elems:
            if not isinstance(clause, TupleExpr) or len(clause.elems) != 4:
                return None
            clauses.append((clause.elems[0], clause.elems[1], clause.elems[2], clause))
        return tuple(node.args[:head_count]), tuple(clauses)
    if len(node.args) < 2:
        return None
    for clause in node.args[1:]:
        if not (
            isinstance(clause, Call)
            and isinstance(clause.func, Name)
            and clause.func.ident == "_gen_clause"
            and len(clause.args) == 3
        ):
            return None
        clauses.append((clause.args[0], clause.args[1], clause.args[2], clause))
    return tuple(node.args[:1]), tuple(clauses)


def _rebuild_comprehension(node, heads, clauses):
    span = node.span
    if node.func.ident.startswith("__"):
        packed = []
        for target, iter_expr, ifs, source in clauses:
            if source is None:
                packed.append(TupleExpr(
                    span=span,
                    ty=_DYN,
                    elems=(target, iter_expr, ifs, BoolLit(span=span, ty=_DYN, value=False)),
                ))
            else:
                packed.append(_replace(
                    source, elems=(target, iter_expr, ifs) + tuple(source.elems[3:])
                ))
        gens = node.args[len(heads)]
        return _replace(node, args=tuple(heads) + (_replace(gens, elems=tuple(packed)),))
    calls = []
    for target, iter_expr, ifs, source in clauses:
        if source is None:
            calls.append(Call(
                span=span,
                ty=_DYN,
                func=Name(span=span, ty=_DYN, ident="_gen_clause"),
                args=(target, iter_expr, ifs),
                kwargs=(),
            ))
        else:
            calls.append(_replace(source, args=(target, iter_expr, ifs)))
    return _replace(node, args=tuple(heads) + tuple(calls))


def _box_expr(expr, boxed, suffix="", free=False):
    """Rewrite reads of boxed names through their one-element cell list.

    Lambda parameters and comprehension targets bind names of their own, so
    a boxed name they shadow is left alone inside that scope.  ``suffix``
    selects the comprehension cell names instead of the names themselves.
    Returns the node itself when nothing inside it changes.
    """
    if expr is None or not boxed or _is_scalar(expr):
        return expr
    if isinstance(expr, tuple):
        items = []
        changed = False
        for item in expr:
            new_item = _box_expr(item, boxed, suffix, free=free)
            if new_item is not item:
                changed = True
            items.append(new_item)
        if changed:
            return tuple(items)
        return expr
    if isinstance(expr, Name):
        if name_in(boxed, expr.ident):
            return _cell_read(expr.ident, expr.span, suffix, free)
        return expr
    if _is_walrus(expr):
        return _replace(expr, args=(
            _box_store_target(expr.args[0], boxed, free),
            _box_expr(expr.args[1], boxed, suffix, free),
        ))
    if isinstance(expr, Lambda):
        return _box_lambda(expr, boxed, suffix, free)
    parts = _comprehension_parts(expr)
    if parts is not None:
        return _box_comprehension(expr, parts, boxed, suffix, free)
    fields = _dataclass_field_names(expr)
    if not fields:
        return expr
    new_fields = {}
    for slot in fields:
        if slot == "span" or slot == "ty":
            continue
        value = _dataclass_field_value(expr, slot, None)
        new_value = _box_expr(value, boxed, suffix, free=free)
        if new_value is not value:
            new_fields[slot] = new_value
    if new_fields:
        return _replace(expr, **new_fields)
    return expr


def _box_lambda(expr, boxed, suffix, free):
    # Defaults run in the enclosing scope; the parameters shadow the body.
    params = []
    shadow = []
    changed = False
    for param in expr.params:
        default = _dataclass_field_value(param, "default", None)
        new_default = _box_expr(default, boxed, suffix, free=free)
        if new_default is not default:
            param = _replace(param, default=new_default)
            changed = True
        params.append(param)
        if param.name:
            append_name_once(shadow, param.name)
    for name in collect_scope_bindings((Return(span=expr.span, value=expr.body),)):
        append_name_once(shadow, name)
    body = _box_expr(expr.body, _without_names(boxed, shadow), suffix, free=True)
    if body is not expr.body:
        changed = True
    if changed:
        return _replace(expr, params=tuple(params), body=body)
    return expr


def _box_comprehension(expr, parts, boxed, suffix, free):
    heads, clauses = parts
    visible = boxed
    new_clauses = []
    changed = False
    # Generator-expression bodies own a deferred scope; PEP 709 list/set/dict
    # bodies remain inlined in the lexical owner. Their first iterable always
    # evaluates here, before entering the deferred scope.
    deferred = isinstance(expr.func, Name) and expr.func.ident in ("_gen_comp", "__genexpr__")
    body_free = free or deferred
    first = True
    for target, iter_expr, ifs, source in clauses:
        # The first iterable evaluates in the enclosing scope; every later
        # part sees the comprehension targets bound so far.
        new_iter = _box_expr(iter_expr, visible, suffix, free=free if first else body_free)
        first = False
        bound = []
        _target_names(target, bound)
        visible = _without_names(visible, bound)
        new_target = _box_target_reads(target, visible, suffix, body_free)
        new_ifs = _box_expr(ifs, visible, suffix, free=body_free)
        if new_iter is not iter_expr or new_target is not target or new_ifs is not ifs:
            changed = True
        new_clauses.append((new_target, new_iter, new_ifs, source))
    new_heads = _box_expr(heads, visible, suffix, free=body_free)
    if new_heads is not heads:
        changed = True
    if changed:
        return _rebuild_comprehension(expr, new_heads, tuple(new_clauses))
    return expr


def _box_target_reads(target, boxed, suffix, free=False):
    """Rewrite the reads inside a binding target; the bound names stay."""
    if isinstance(target, Name):
        return target
    if isinstance(target, (TupleExpr, ListExpr)):
        elems = []
        changed = False
        for elem in target.elems:
            new_elem = _box_target_reads(elem, boxed, suffix, free)
            if new_elem is not elem:
                changed = True
            elems.append(new_elem)
        if changed:
            return _replace(target, elems=tuple(elems))
        return target
    if _is_starred(target):
        inner = _box_target_reads(target.args[0], boxed, suffix, free)
        if inner is not target.args[0]:
            return _replace(target, args=(inner,) + tuple(target.args[1:]))
        return target
    return _box_expr(target, boxed, suffix, free=free)


def _box_store_target(target, boxed, free):
    if isinstance(target, Name) and name_in(boxed, target.ident):
        return _cell_slot(target.ident, target.span, target.ty)
    if isinstance(target, (TupleExpr, ListExpr)):
        return _replace(target, elems=tuple(
            _box_store_target(elem, boxed, free) for elem in target.elems))
    if _is_starred(target) or _is_walrus(target):
        return _replace(target, args=tuple(
            _box_store_target(arg, boxed, free) for arg in target.args))
    return _box_expr(target, boxed, free=free)


def _bind_through_temps(target, boxed, stores, free):
    if isinstance(target, Name):
        if not name_in(boxed, target.ident):
            return target
        temp = _replace(
            target, ident="__pcc_boxed_" + target.ident + _span_suffix(target.span)
        )
        stores.append(Assign(
            span=target.span,
            targets=(_cell_slot(target.ident, target.span, target.ty),),
            value=temp,
            annotation=None,
        ))
        return temp
    if isinstance(target, (TupleExpr, ListExpr)):
        elems = []
        for elem in target.elems:
            elems.append(_bind_through_temps(elem, boxed, stores, free))
        return _replace(target, elems=tuple(elems))
    if _is_starred(target):
        inner = _bind_through_temps(target.args[0], boxed, stores, free)
        return _replace(target, args=(inner,) + tuple(target.args[1:]))
    return _box_expr(target, boxed, free=free)


def _box_bound_target(target, boxed, free):
    """Bind a loop or ``with`` target through fresh names, then fill the cells.

    Layer 1 binds these targets only as plain names (or tuples of them), so
    a boxed name cannot become ``name[0]`` in place.  The returned stores
    must run first in the body.
    """
    stores = []
    new_target = _bind_through_temps(target, boxed, stores, free)
    return new_target, tuple(stores)


def _box_with(stmt, boxed, boxed_function_defs, free):
    items = []
    for index in range(len(stmt.items)):
        context_expr, as_var = stmt.items[index]
        new_context = _box_expr(context_expr, boxed, free=free)
        if as_var is None:
            items.append((new_context, None))
            continue
        new_var, stores = _box_bound_target(as_var, boxed, free)
        items.append((new_context, new_var))
        if stores:
            # Later items may read the name just bound, so they nest under the
            # cell stores: a multi-item ``with`` is nested ``with`` statements.
            rest = stmt.items[index + 1:]
            body = stmt.body
            if rest:
                body = (_replace(stmt, items=tuple(rest)),)
            return _replace(
                stmt,
                items=tuple(items),
                body=stores + _box_stmts(body, boxed, boxed_function_defs, free),
            )
    return _replace(
        stmt,
        items=tuple(items),
        body=_box_stmts(stmt.body, boxed, boxed_function_defs, free),
    )


def _box_handlers(handlers, boxed, boxed_function_defs, free):
    out = []
    for handler in handlers:
        body = _box_stmts(
            _dataclass_field_value(handler, "body", ()), boxed, boxed_function_defs, free)
        exc_type = _box_expr(_dataclass_field_value(handler, "exc_type", None), boxed, free=free)
        name = _dataclass_field_value(handler, "name", None)
        if name and name_in(boxed, name):
            # Like a loop target: bind a fresh name, then fill the cell.
            span = _dataclass_field_value(handler, "span", None)
            temp = "__pcc_boxed_" + name + _span_suffix(span)
            store = Assign(
                span=span,
                targets=(_cell_slot(name, span, _DYN),),
                value=Name(span=span, ty=_DYN, ident=temp),
                annotation=None,
            )
            cleanup = Try(span=span, body=body, handlers=(), else_body=(),
                          finally_body=(_cell_clear(name, span),))
            out.append(_replace(handler, exc_type=exc_type, name=temp,
                                body=(store, cleanup)))
        else:
            out.append(_replace(handler, exc_type=exc_type, body=body))
    return tuple(out)


def _box_delete(stmt, boxed, free):
    out = []
    kept = []
    for target in stmt.targets:
        if isinstance(target, (TupleExpr, ListExpr)):
            if kept:
                out.append(_replace(stmt, targets=tuple(kept)))
                kept = []
            out.extend(_box_delete(_replace(stmt, targets=target.elems), boxed, free))
            continue
        if isinstance(target, Name) and name_in(boxed, target.ident):
            if kept:
                out.append(_replace(stmt, targets=tuple(kept)))
                kept = []
            # Check before the store: deleting an already-empty cell raises.
            # The single payload store publishes unbound before old-value
            # finalizers can re-enter a closure and read this same cell.
            out.append(ExprStmt(span=stmt.span,
                                 expr=_cell_read(target.ident, stmt.span, free=free)))
            out.append(_cell_clear(target.ident, stmt.span))
        else:
            kept.append(_box_expr(target, boxed, free=free))
    if kept:
        out.append(_replace(stmt, targets=tuple(kept)))
    return tuple(out)


def _box_class_header(stmt, boxed, free):
    # Bases, keywords and decorators evaluate in the enclosing scope.
    bases = _box_expr(stmt.bases, boxed, free=free)
    keywords = _box_expr(stmt.keywords, boxed, free=free)
    decorators = _box_expr(stmt.decorators, boxed, free=free)
    body = []
    changed = False
    for member in stmt.body:
        if isinstance(member, FuncDef):
            # Class locals do not enclose method bodies. Only the method's
            # own locals/globals shadow the enclosing function's cells.
            shadowed = function_local_bindings(member) + scope_declared_names(member.body, True, False)
            inherited = tuple(name for name in boxed if not name_in(shadowed, name))
            method_body = _box_stmts(member.body, inherited, free=True)
            if method_body != member.body:
                member = _replace(member, body=method_body)
                changed = True
        body.append(member)
    if (not changed and bases is stmt.bases and keywords is stmt.keywords
            and decorators is stmt.decorators):
        return stmt
    return _replace(stmt, bases=bases, keywords=keywords, decorators=decorators,
                    body=tuple(body))


def _box_import(stmt, boxed):
    """Bind a boxed import target through a temporary, then store the cell.

    Imports count as scope bindings, so a captured import name is boxed; left
    alone, the statement never wrote the cell and closures remained unbound.  An
    unaliased ``import a.b`` binds package ``a``, whose object a non-native
    package ``__init__`` cannot provide; it keeps the old binding.
    """
    is_import = isinstance(stmt, Import)
    span = stmt.span
    # One temporary per statement: import bindings are static, so two imports
    # of one boxed name (if/else branches) must not share it.
    suffix = _span_suffix(span)
    names, stores = [], []
    for source, asname in stmt.names:
        bound = asname or (source.split(".", 1)[0] if is_import else source)
        dotted = is_import and asname is None and "." in source
        if source == "*" or dotted or not name_in(boxed, bound):
            names.append((source, asname))
            continue
        stores.append((bound, "__pcc_boxed_import_" + bound + suffix))
        names.append((source, stores[-1][1]))
    if not stores:
        return (stmt,)
    cells = tuple(
        Assign(span=span, targets=(Subscript(
            span=span, ty=_DYN, obj=Name(span=span, ty=_DYN, ident=bound),
            idx=IntLit(span=span, ty=IntType(name="int"), value=0)),),
            value=Name(span=span, ty=_DYN, ident=temp))
        for bound, temp in stores
    )
    return (_replace(stmt, names=tuple(names)),) + cells


def _box_stmts(stmts, boxed, boxed_function_defs=None, free=False):
    """Rewrite reads and writes of boxed names through their cell list."""
    out = []
    for stmt in stmts:
        if isinstance(stmt, Assign):
            new_value = _box_expr(stmt.value, boxed, free=free)
            new_targets = []
            for target in stmt.targets:
                new_targets.append(_box_store_target(target, boxed, free))
            out.append(
                _replace(
                    stmt,
                    targets=tuple(new_targets),
                    value=new_value,
                )
            )
            continue
        if isinstance(stmt, AugAssign):
            if isinstance(stmt.target, Name) and name_in(boxed, stmt.target.ident):
                out.append(ExprStmt(span=stmt.span,
                    expr=_cell_read(stmt.target.ident, stmt.span, free=free)))
            new_value = _box_expr(stmt.value, boxed, free=free)
            out.append(
                _replace(
                    stmt,
                    target=_box_store_target(stmt.target, boxed, free),
                    value=new_value,
                )
            )
            continue
        if isinstance(stmt, If):
            out.append(
                _replace(
                    stmt,
                    cond=_box_expr(stmt.cond, boxed, free=free),
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs, free),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs, free),
                )
            )
            continue
        if isinstance(stmt, While):
            out.append(
                _replace(
                    stmt,
                    cond=_box_expr(stmt.cond, boxed, free=free),
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs, free),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs, free),
                )
            )
            continue
        if isinstance(stmt, For):
            target, stores = _box_bound_target(stmt.target, boxed, free)
            out.append(
                _replace(
                    stmt,
                    target=target,
                    iter=_box_expr(stmt.iter, boxed, free=free),
                    body=stores + _box_stmts(stmt.body, boxed, boxed_function_defs, free),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs, free),
                )
            )
            continue
        if isinstance(stmt, Try):
            out.append(
                _replace(
                    stmt,
                    body=_box_stmts(stmt.body, boxed, boxed_function_defs, free),
                    else_body=_box_stmts(stmt.else_body, boxed, boxed_function_defs, free),
                    finally_body=_box_stmts(stmt.finally_body, boxed, boxed_function_defs, free),
                    handlers=_box_handlers(stmt.handlers, boxed, boxed_function_defs, free),
                )
            )
            continue
        if isinstance(stmt, With):
            out.append(_box_with(stmt, boxed, boxed_function_defs, free))
            continue
        if isinstance(stmt, ExprStmt):
            out.append(_replace(stmt, expr=_box_expr(stmt.expr, boxed, free=free)))
            continue
        if isinstance(stmt, Return):
            if stmt.value is None:
                out.append(stmt)
            else:
                out.append(_replace(stmt, value=_box_expr(stmt.value, boxed, free=free)))
            continue
        if isinstance(stmt, Raise):
            out.append(
                _replace(
                    stmt,
                    exc=_box_expr(stmt.exc, boxed, free=free),
                    cause=_box_expr(stmt.cause, boxed, free=free),
                )
            )
            continue
        if isinstance(stmt, Delete):
            out.extend(_box_delete(stmt, boxed, free))
            continue
        if isinstance(stmt, Import) or _is_import_from_stmt(stmt):
            out.extend(_box_import(stmt, boxed))
            continue
        if isinstance(stmt, FuncDef):
            # A child binding shadows this scope's cell. Its own cells are
            # allocated when that function is hoisted, under its final name.
            shadowed = function_local_bindings(stmt) + scope_declared_names(stmt.body, True, False)
            inherited = tuple(name for name in boxed if not name_in(shadowed, name))
            # Defaults and decorators evaluate here, when the def runs.
            args = []
            for arg in stmt.args:
                default = _dataclass_field_value(arg, "default", None)
                new_default = _box_expr(default, boxed, free=free)
                if new_default is not default:
                    arg = _replace(arg, default=new_default)
                args.append(arg)
            rewritten = clone_funcdef(
                stmt, stmt.name, tuple(args), stmt.return_ty,
                _box_stmts(stmt.body, inherited, boxed_function_defs, True),
            )
            decorators = _box_expr(stmt.decorators, boxed, free=free)
            if decorators is not stmt.decorators:
                rewritten = _replace(rewritten, decorators=decorators)
            if boxed_function_defs is not None and name_in(boxed, stmt.name):
                # A def binds its closure cell just like an assignment. Keep
                # the AST alive with its identity until hoisting publishes it.
                boxed_function_defs.append(rewritten)
            out.append(rewritten)
            continue
        if isinstance(stmt, ClassDef):
            rewritten = _box_class_header(stmt, boxed, free)
            if boxed_function_defs is not None and name_in(boxed, stmt.name):
                # A class statement binds its cell like a def does.
                boxed_function_defs.append(rewritten)
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
    body = rewrite_comprehension_cells(body)
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
                    elems=(_cell_unbound(span),),
                ),
                annotation=None,
            )
        )
    return tuple(sentinels) + rewritten


# -- late binding: names lambdas capture that are rebound afterwards --------


def _names_read_in_lambdas(node, bound, inside, out):
    """Collect names read inside a lambda within ``node``.

    ``bound`` holds the names that a lambda parameter or an inner
    comprehension target rebinds on the way down; ``inside`` turns true below
    a lambda.  A lambda's own defaults are not reads of that lambda.
    """
    if node is None or _is_scalar(node):
        return
    if isinstance(node, tuple):
        for item in node:
            _names_read_in_lambdas(item, bound, inside, out)
        return
    if isinstance(node, Name):
        if inside and not name_in(bound, node.ident):
            append_name_once(out, node.ident)
        return
    if isinstance(node, Lambda):
        inner = bound
        for param in node.params:
            _names_read_in_lambdas(
                _dataclass_field_value(param, "default", None), bound, inside, out
            )
            if param.name:
                inner = inner + (param.name,)
        _names_read_in_lambdas(node.body, inner, True, out)
        return
    parts = _comprehension_parts(node)
    if parts is not None:
        heads, clauses = parts
        visible = bound
        index = 0
        for target, iter_expr, ifs, _source in clauses:
            if index == 0:
                _names_read_in_lambdas(iter_expr, bound, inside, out)
            else:
                _names_read_in_lambdas(iter_expr, visible, inside, out)
            names = []
            _target_names(target, names)
            visible = visible + tuple(names)
            _names_read_in_lambdas(ifs, visible, inside, out)
            index += 1
        _names_read_in_lambdas(heads, visible, inside, out)
        return
    for slot in _dataclass_field_names(node):
        if slot == "span" or slot == "ty":
            continue
        _names_read_in_lambdas(_dataclass_field_value(node, slot, None), bound, inside, out)


class _LateBindingScan:
    """Binding sites and lambda creations of one body, in evaluation order."""

    def __init__(self):
        self.clock = 0
        self.bindings = {}
        self.lambdas = []

    def tick(self):
        self.clock = self.clock + 1
        return self.clock

    def bind(self, name, loops):
        sites = self.bindings.get(name)
        if sites is None:
            sites = []
            self.bindings[name] = sites
        sites.append((self.tick(), loops))


def _shares_loop(left, right):
    for loop in left:
        if name_in(right, loop):
            return True
    return False


def _scan_expr(scan, expr, loops, shadow):
    if expr is None or _is_scalar(expr) or isinstance(expr, Name):
        return
    if isinstance(expr, tuple):
        for item in expr:
            _scan_expr(scan, item, loops, shadow)
        return
    if isinstance(expr, Lambda):
        # Defaults run when the lambda is created, in this scope.
        for param in expr.params:
            _scan_expr(scan, _dataclass_field_value(param, "default", None), loops, shadow)
        free = []
        _names_read_in_lambdas(expr, shadow, False, free)
        scan.lambdas.append((tuple(free), scan.tick(), loops))
        return
    if _is_walrus(expr):
        _scan_expr(scan, expr.args[1], loops, shadow)
        if isinstance(expr.args[0], Name):
            scan.bind(expr.args[0].ident, loops)
        return
    parts = _comprehension_parts(expr)
    if parts is not None:
        heads, clauses = parts
        # Everything after the first iterable repeats once per element.
        inner = loops + (scan.tick(),)
        visible = shadow
        index = 0
        for target, iter_expr, ifs, _source in clauses:
            if index == 0:
                _scan_expr(scan, iter_expr, loops, shadow)
            else:
                _scan_expr(scan, iter_expr, inner, visible)
            names = []
            _target_names(target, names)
            visible = visible + tuple(names)
            _scan_expr(scan, ifs, inner, visible)
            index += 1
        _scan_expr(scan, heads, inner, visible)
        return
    for slot in _dataclass_field_names(expr):
        if slot == "span" or slot == "ty":
            continue
        _scan_expr(scan, _dataclass_field_value(expr, slot, None), loops, shadow)


def _scan_target(scan, target, loops):
    _scan_expr(scan, target, loops, ())
    names = []
    _target_names(target, names)
    for name in names:
        scan.bind(name, loops)


def _scan_stmts(scan, stmts, loops):
    for stmt in stmts:
        _scan_stmt(scan, stmt, loops)


def _scan_stmt(scan, stmt, loops):
    if isinstance(stmt, Assign):
        _scan_expr(scan, stmt.value, loops, ())
        for target in stmt.targets:
            _scan_target(scan, target, loops)
    elif isinstance(stmt, AugAssign):
        _scan_expr(scan, stmt.value, loops, ())
        _scan_target(scan, stmt.target, loops)
    elif isinstance(stmt, ExprStmt):
        _scan_expr(scan, stmt.expr, loops, ())
    elif isinstance(stmt, Return):
        _scan_expr(scan, stmt.value, loops, ())
    elif isinstance(stmt, Raise):
        _scan_expr(scan, stmt.exc, loops, ())
        _scan_expr(scan, stmt.cause, loops, ())
    elif isinstance(stmt, Delete):
        for target in stmt.targets:
            _scan_target(scan, target, loops)
    elif isinstance(stmt, If):
        _scan_expr(scan, stmt.cond, loops, ())
        _scan_stmts(scan, stmt.body, loops)
        _scan_stmts(scan, stmt.else_body, loops)
    elif isinstance(stmt, While):
        inner = loops + (scan.tick(),)
        _scan_expr(scan, stmt.cond, inner, ())
        _scan_stmts(scan, stmt.body, inner)
        _scan_stmts(scan, stmt.else_body, loops)
    elif isinstance(stmt, For):
        _scan_expr(scan, stmt.iter, loops, ())
        inner = loops + (scan.tick(),)
        _scan_target(scan, stmt.target, inner)
        _scan_stmts(scan, stmt.body, inner)
        _scan_stmts(scan, stmt.else_body, loops)
    elif isinstance(stmt, With):
        for context_expr, as_var in stmt.items:
            _scan_expr(scan, context_expr, loops, ())
            if as_var is not None:
                _scan_target(scan, as_var, loops)
        _scan_stmts(scan, stmt.body, loops)
    elif isinstance(stmt, Try):
        _scan_stmts(scan, stmt.body, loops)
        for handler in stmt.handlers:
            _scan_expr(scan, _dataclass_field_value(handler, "exc_type", None), loops, ())
            name = _dataclass_field_value(handler, "name", None)
            if name:
                scan.bind(name, loops)
            _scan_stmts(scan, _dataclass_field_value(handler, "body", ()), loops)
        _scan_stmts(scan, stmt.else_body, loops)
        _scan_stmts(scan, stmt.finally_body, loops)
    elif isinstance(stmt, FuncDef):
        # The body is its own scope; defaults and decorators run here.
        for arg in stmt.args:
            _scan_expr(scan, _dataclass_field_value(arg, "default", None), loops, ())
        _scan_expr(scan, stmt.decorators, loops, ())
        scan.bind(stmt.name, loops)
    elif isinstance(stmt, ClassDef):
        _scan_expr(scan, stmt.bases, loops, ())
        _scan_expr(scan, stmt.keywords, loops, ())
        _scan_expr(scan, stmt.decorators, loops, ())
        scan.bind(stmt.name, loops)
    elif isinstance(stmt, Import):
        for mod_name, asname in stmt.names:
            bound = asname or mod_name.split(".", 1)[0]
            if bound:
                scan.bind(bound, loops)
    elif _is_import_from_stmt(stmt):
        for imported_name, asname in _import_names_from_stmt(stmt):
            if imported_name != "*":
                scan.bind(asname or imported_name, loops)


def late_bound_lambda_captures(stmts):
    """Names a lambda in ``stmts`` captures that the body can rebind later.

    A pcc lambda copies each captured value when it is created; a CPython
    closure reads the variable's shared cell when it is called.  The two
    agree unless the name is bound again after the lambda exists: by a later
    statement, or anywhere in a loop that also creates the lambda.  Boxing
    those names makes the copied value the shared cell.  Callers keep only
    the names local to the function.
    """
    scan = _LateBindingScan()
    _scan_stmts(scan, stmts, ())
    out = []
    for free, position, loops in scan.lambdas:
        for name in free:
            if name_in(out, name):
                continue
            sites = scan.bindings.get(name)
            if sites is None:
                continue
            for site_position, site_loops in sites:
                if site_position > position or _shares_loop(loops, site_loops):
                    out.append(name)
                    break
    return tuple(out)


# -- comprehension cells ------------------------------------------------------


def _cell_bound_target(target, cells, suffix):
    if isinstance(target, Name):
        if name_in(cells, target.ident):
            return _cell_slot(_cell_ident(target.ident, suffix), target.span, target.ty)
        return target
    if isinstance(target, (TupleExpr, ListExpr)):
        elems = []
        for elem in target.elems:
            elems.append(_cell_bound_target(elem, cells, suffix))
        return _replace(target, elems=tuple(elems))
    if _is_starred(target):
        inner = _cell_bound_target(target.args[0], cells, suffix)
        return _replace(target, args=(inner,) + tuple(target.args[1:]))
    return _box_expr(target, cells, suffix)


def _comprehension_with_cells(node):
    parts = _comprehension_parts(node)
    if parts is None:
        return node
    heads, clauses = parts
    targets = []
    for target, _iter_expr, _ifs, _source in clauses:
        _target_names(target, targets)
    captured = []
    index = 0
    for _target, iter_expr, ifs, _source in clauses:
        if index > 0:
            _names_read_in_lambdas(iter_expr, (), False, captured)
        _names_read_in_lambdas(ifs, (), False, captured)
        index += 1
    _names_read_in_lambdas(heads, (), False, captured)
    cells = []
    for name in targets:
        if name_in(captured, name) and not name.startswith(COMPREHENSION_CELL_PREFIX):
            append_name_once(cells, name)
    if not cells:
        return node
    cells.sort()
    span = node.span
    suffix = _span_suffix(span)
    new_clauses = []
    for name in cells:
        cell_list = ListExpr(
            span=span,
            ty=ListType(name="list", elem=DynType(name="dyn")),
            elems=(_cell_unbound(span),),
        )
        new_clauses.append((
            Name(span=span, ty=_DYN, ident=_cell_ident(name, suffix)),
            TupleExpr(
                span=span,
                ty=TupleType(name="tuple", elems=(cell_list.ty,)),
                elems=(cell_list,),
            ),
            TupleExpr(span=span, ty=_DYN, elems=()),
            None,
        ))
    active = ()
    index = 0
    for target, iter_expr, ifs, source in clauses:
        # The first iterable still evaluates before the comprehension scope.
        if index > 0:
            iter_expr = _box_expr(iter_expr, active, suffix)
        bound = []
        _target_names(target, bound)
        for name in bound:
            if name_in(cells, name) and not name_in(active, name):
                active = active + (name,)
        new_clauses.append((
            _cell_bound_target(target, cells, suffix),
            iter_expr,
            _box_expr(ifs, active, suffix),
            source,
        ))
        index += 1
    return _rebuild_comprehension(node, _box_expr(heads, active, suffix), tuple(new_clauses))


def _cells_node(node):
    if node is None or _is_scalar(node):
        return node
    if isinstance(node, tuple):
        items = []
        changed = False
        for item in node:
            new_item = _cells_node(item)
            if new_item is not item:
                changed = True
            items.append(new_item)
        if changed:
            return tuple(items)
        return node
    fields = _dataclass_field_names(node)
    if not fields:
        return node
    new_fields = {}
    for slot in fields:
        if name_in(_CELL_SKIP_FIELDS, slot):
            continue
        value = _dataclass_field_value(node, slot, None)
        new_value = _cells_node(value)
        if new_value is not value:
            new_fields[slot] = new_value
    if new_fields:
        node = _replace(node, **new_fields)
    if isinstance(node, Call):
        return _comprehension_with_cells(node)
    return node


def rewrite_comprehension_cells(stmts):
    """Give each comprehension target a lambda captures a per-run cell.

    A comprehension is its own scope, so CPython closures over one of its
    targets share a single cell per comprehension run and all see the last
    value: every entry of ``[lambda: v for v in xs]`` returns ``xs[-1]``.
    pcc lambdas copy the values they capture, so such a target is stored
    through a cell list that a leading clause binds once per run::

        [lambda: __pcc_cell_v_L_C[0]
         for __pcc_cell_v_L_C in ([<unbound>],)
         for __pcc_cell_v_L_C[0] in xs]

    The first source iterable stays unrewritten: CPython evaluates it in the
    enclosing scope.
    """
    return _cells_node(stmts)
