"""Free-name analysis for nested-function hoisting.

All mutable caches and module-scope inputs are explicit. This keeps the
analysis independent of the Layer1 codegen inheritance graph.
"""

from __future__ import annotations

from ..py_ast import (
    Assign as _Assign,
    AugAssign as _AugAssign,
    Call as _Call,
    ClassDef as _ClassDef,
    For as _For,
    FuncDef as _FuncDef,
    Global as _GL,
    If as _If,
    Lambda as _Lambda,
    Name as _Name,
    Nonlocal as _NL,
    Try as _Try,
    TupleExpr as _TupleExpr,
    While as _While,
    With as _With,
)
from .hoist_analysis import (
    _PY_BUILTINS_NS,
    _dataclass_field_names,
    _dataclass_field_value,
    _import_names_from_stmt,
    _is_import_from_stmt,
    _is_import_stmt,
    append_name_once,
    copy_names,
    extend_names_once,
    filter_capture_names,
    hoist_stat_inc,
    name_in,
)


def _hoist_cache_key4(prefix, fd, names_a, names_b, names_c):
    key = prefix + ":" + str(id(fd))
    for name in names_a:
        key = key + "|a:" + str(name)
    for name in names_b:
        key = key + "|b:" + str(name)
    for name in names_c:
        key = key + "|c:" + str(name)
    return key


def compute_free_names(
    fd,
    excluded,
    own_name,
    outer_scope_names,
    module_scope_names_base,
    existing_top_or_hoisted_names,
    cache,
    profile_enabled,
    stats,
):
    """Return the sorted tuple of Name idents that ``fd``'s
    body reads but aren't bound in its param list, a local
    assignment, a module-level symbol, a Python builtin, its
    own self-reference, or one of the ``excluded`` names.

    Callers that only want a bool can use
    ``bool(compute_free_names(...))``. Closure conversion
    uses the actual name set to append synthetic params."""
    cache_key = _hoist_cache_key4(
        "free",
        fd,
        excluded,
        outer_scope_names,
        (own_name or "",),
    )
    cached_entry = cache.get(cache_key)
    if cached_entry is not None and cached_entry[1] is fd:
        hoist_stat_inc(
            profile_enabled,
            stats,
            "compute_free_names_cache_hits",
        )
        return cached_entry[0]
    hoist_stat_inc(
        profile_enabled,
        stats,
        "compute_free_names_calls",
    )
    param_names = []
    for a in fd.args:
        if a.name != "":
            append_name_once(param_names, a.name)
    assigned_names = []
    module_names = []
    for module_name in module_scope_names_base:
        # An enclosing function local shadows a same-named module
        # binding.  The nested function must therefore capture the
        # lexical local instead of resolving the module symbol.
        if not name_in(outer_scope_names, module_name):
            append_name_once(module_names, module_name)
    extend_names_once(module_names, excluded)
    if own_name is not None:
        append_name_once(module_names, own_name)
    # A recursive function name can instead be an enclosing lexical cell.
    # The hoister excludes direct symbol recursion explicitly; such a cell
    # must remain capturable, including when later rebound by the owner.
    if not name_in(outer_scope_names, fd.name):
        append_name_once(module_names, fd.name)

    from ..py_ast import TupleExpr as _TupleExpr

    def add_target_names(t):
        if isinstance(t, _Name):
            append_name_once(assigned_names, t.ident)
        elif (
            isinstance(t, _Call)
            and isinstance(t.func, _Name)
            and t.func.ident in ("*", "__starred__")
            and t.args
        ):
            add_target_names(t.args[0])
        elif isinstance(t, _TupleExpr):
            for e in t.elems:
                add_target_names(e)

    # Names declared ``nonlocal`` / ``global`` in the body are
    # explicit outer-scope references, not local bindings —
    # even an ``X = v`` assignment to such a name writes to
    # the outer binding, not a local. Track them and exclude
    # from ``assigned_names``.
    nonlocal_names = []
    global_names = []
    from ..py_ast import (
        Global as _GL,
        Import as _ImportStmt,
        ImportFrom as _ImportFromStmt,
        Nonlocal as _NL,
    )

    def collect_nonlocal_global(stmts):
        for s in stmts:
            if isinstance(s, _NL):
                extend_names_once(nonlocal_names, s.names)
            elif isinstance(s, _GL):
                extend_names_once(global_names, s.names)
            elif isinstance(s, _If):
                collect_nonlocal_global(s.body)
                collect_nonlocal_global(s.else_body)
            elif isinstance(s, _For):
                collect_nonlocal_global(s.body)
            elif isinstance(s, _While):
                collect_nonlocal_global(s.body)
            elif isinstance(s, _With):
                collect_nonlocal_global(s.body)
            elif isinstance(s, _Try):
                collect_nonlocal_global(s.body)
                collect_nonlocal_global(s.else_body)
                collect_nonlocal_global(s.finally_body)
                for h in s.handlers:
                    collect_nonlocal_global(
                        _dataclass_field_value(h, "body", ())
                    )

    collect_nonlocal_global(fd.body)
    extend_names_once(module_names, global_names)

    def collect_assigned(stmts):
        for s in stmts:
            if isinstance(s, _Assign):
                for t in s.targets:
                    if isinstance(t, _Name) and name_in(
                        nonlocal_names, t.ident
                    ):
                        continue
                    add_target_names(t)
            elif isinstance(s, _AugAssign):
                # AugAssign requires the target to already be
                # bound; it doesn't create a local on its own.
                # Don't add to assigned_names so the free-var
                # walker treats the read-modify-write as a
                # capture if no pure Assign provides the
                # binding first.
                pass
            elif isinstance(s, _For):
                add_target_names(s.target)
                collect_assigned(s.body)
            elif isinstance(s, _If):
                collect_assigned(s.body)
                collect_assigned(s.else_body)
            elif isinstance(s, _While):
                collect_assigned(s.body)
            elif isinstance(s, _With):
                for _ctx_expr, as_var in s.items:
                    if as_var is not None:
                        add_target_names(as_var)
                collect_assigned(s.body)
            elif isinstance(s, _Try):
                collect_assigned(s.body)
                collect_assigned(s.else_body)
                collect_assigned(s.finally_body)
                for h in s.handlers:
                    handler_name = _dataclass_field_value(h, "name", "")
                    if handler_name:
                        append_name_once(assigned_names, handler_name)
                    collect_assigned(
                        _dataclass_field_value(h, "body", ())
                    )
            elif _is_import_stmt(s):
                if _is_import_from_stmt(s):
                    for imported_name, as_name in _import_names_from_stmt(s):
                        if imported_name == "*":
                            continue
                        append_name_once(
                            assigned_names,
                            as_name or imported_name,
                        )
                else:
                    for mod_name, asname in _import_names_from_stmt(s):
                        bound = asname or mod_name.split(".", 1)[0]
                        if bound:
                            append_name_once(assigned_names, bound)
            elif isinstance(s, (_FuncDef, _ClassDef)):
                # A nested ``def`` / ``class`` binds its name
                # in the enclosing scope. Uses of that name in
                # sibling statements are local references, not
                # captures from an even-further-outer scope.
                # Don't recurse into its body — that has its
                # own local scope.
                append_name_once(assigned_names, s.name)

    collect_assigned(fd.body)

    # Parser sentinel names the codegen special-cases at Call
    # position (comprehensions, walrus, yield). They never
    # refer to a user binding, so never count as a capture.
    sentinel_ns = (
        "__listcomp__",
        "_list_comp",
        "_gen_comp",
        "__genexpr__",
        "__setcomp__",
        "_set_comp",
        "__dictcomp__",
        "_dict_comp",
        "_walrus",
        "__walrus__",
        "_yield",
        "__yield__",
        "_yield_from",
        "__yield_from__",
        "__await__",
        "_gen_clause",
        "__starred__",
        # dataclasses.replace aliases are lowered as a native
        # codegen helper, not a runtime callable capture.
        "replace",
        "_replace",
        # Treat bare ``_`` as a discard binding for closure
        # analysis. The pcc codebase uses it pervasively in
        # tuple-unpack / loop-target throwaway positions; if it
        # leaks into a propagated capture set, hoisted sibling
        # calls end up demanding an outer ``_`` binding that
        # doesn't semantically exist.
        "_",
        "*",
        "**",
    )
    local_scope = []
    extend_names_once(local_scope, param_names)
    extend_names_once(local_scope, assigned_names)
    extend_names_once(local_scope, module_names)
    extend_names_once(local_scope, sentinel_ns)
    builtins_ns = _PY_BUILTINS_NS
    # Membership index for the walk.  The scope and builtin sets are fixed for
    # the whole body walk, and the compiled per-Name ``name_in`` list scan over
    # hundreds of module-scope names was measured at the top of a class_gen
    # worker profile (each loop iteration pays the pointer-load barrier and
    # refcount protocol).  Idents are short, so dict hashing is cheap.
    # ``bound`` stays a list: comprehension clauses rebind it mid-walk.
    resolved_scope = {}
    for scope_name in local_scope:
        resolved_scope[scope_name] = True
    for scope_name in builtins_ns:
        resolved_scope[scope_name] = True
    free = []

    def _collect_target_names(t, acc):
        if isinstance(t, _Name):
            append_name_once(acc, t.ident)
            return
        for slot in _dataclass_field_names(t):
            # span/ty are metadata records (SourceSpan / structural types);
            # they can never contain a Name and their generic traversal costs
            # several dynamic lookups per scalar field.
            if slot == "span" or slot == "ty":
                continue
            v = _dataclass_field_value(t, slot, None)
            if isinstance(v, tuple):
                for it in v:
                    _collect_target_names(it, acc)

    def _call_ident(expr):
        return getattr(expr, "ident", None)

    def _is_call_node(expr):
        return isinstance(expr, _Call) or (
            hasattr(expr, "func")
            and hasattr(expr, "args")
            and hasattr(expr, "kwargs")
        )

    def _has_gen_clause(node):
        if isinstance(node, _TupleExpr):
            for it in node.elems:
                if _has_gen_clause(it):
                    return True
            return False
        if _is_call_node(node):
            return _call_ident(node.func) == "_gen_clause"
        return False

    def _iter_comp_nodes(raw):
        if raw is None:
            return tuple()
        if isinstance(raw, _TupleExpr):
            raw = raw.elems
        if isinstance(raw, (list, tuple)):
            out = []
            for it in raw:
                if isinstance(it, (list, tuple, _TupleExpr)):
                    for nested in _iter_comp_nodes(it):
                        out.append(nested)
                else:
                    out.append(it)
            return tuple(out)
        return (raw,)

    def _collect_gen_clauses(raw):
        if raw is None:
            return tuple()
        out = []
        for node in _iter_comp_nodes(raw):
            if _is_call_node(node) and _call_ident(node.func) == "_gen_clause":
                out.append(node)
                continue
            if not _is_call_node(node):
                continue
            # Defensive path for malformed/foreign parser outputs where
            # a synthetic `_gen_clause` tuple slips through as plain args.
            if _call_ident(node.func) == "_gen_clause":
                out.append(node)
        return tuple(out)

    def _decompose_gen_clause(clause):
        if _is_call_node(clause) and _call_ident(clause.func) == "_gen_clause":
            target = clause.args[0] if len(clause.args) >= 1 else None
            iter_expr = clause.args[1] if len(clause.args) >= 2 else None
            ifs_expr = clause.args[2] if len(clause.args) >= 3 else None
            return target, iter_expr, ifs_expr
        if _is_call_node(clause):
            return None
        if isinstance(clause, _TupleExpr):
            target = clause.elems[0] if len(clause.elems) >= 1 else None
            iter_expr = clause.elems[1] if len(clause.elems) >= 2 else None
            ifs_expr = clause.elems[2] if len(clause.elems) >= 3 else None
            return target, iter_expr, ifs_expr
        if isinstance(clause, (tuple, list)) and len(clause) >= 1:
            target = clause[0]
            iter_expr = clause[1] if len(clause) >= 2 else None
            ifs_expr = clause[2] if len(clause) >= 3 else None
            return target, iter_expr, ifs_expr
        return None

    def walk(x, bound=None):
        # Fast bail for the metadata scalars the generic field loop still
        # yields (None, strings, numbers): each would otherwise pay the full
        # isinstance/call-shape gauntlet below just to reach the empty
        # reflection guard.
        if x is None:
            return
        if isinstance(x, (str, int, float, bool, bytes)):
            return
        if bound is None:
            bound = ()
        if isinstance(x, (_FuncDef, _ClassDef)):
            # Nested defs/classes introduce their own local
            # scope. Their bodies must not contribute free vars
            # to the enclosing function.
            return
        if isinstance(x, tuple):
            # Plain tuples show up in places like
            # ``Call.kwargs = ((name, Expr), ...)``; recurse so
            # kwarg values still participate in free-var
            # analysis.
            for it in x:
                walk(it, bound)
            return
        if isinstance(x, _Name):
            if resolved_scope.get(x.ident) is None and not name_in(
                bound, x.ident
            ):
                append_name_once(free, x.ident)
            return
        if _is_call_node(x):
            # Use getattr-with-default rather than direct .ident
            # access: pcc-py self-host's isinstance dispatch can
            # return True against ``Name`` for a base ``Expr``
            # instance that does NOT actually have ``ident``,
            # causing AttributeError under ``self.builder.X``-
            # style chains. Falling back to None lets the
            # generic recursion handle the foreign-shape Call.
            fname = _call_ident(x.func)
            if fname is None:
                for gen_arg in x.args[1:]:
                    if _has_gen_clause(gen_arg):
                        # Self-host may lose ident on calls that
                        # still carry explicit _gen_clause operands.
                        fname = "_list_comp"
                        break
            if fname is None:
                for slot in _dataclass_field_names(x):
                    if slot == "span" or slot == "ty":
                        continue
                    v = _dataclass_field_value(x, slot, None)
                    if isinstance(v, tuple):
                        for it in v:
                            walk(it, bound)
                    else:
                        walk(v, bound)
                return
            if (
                fname
                in (
                    "_list_comp",
                    "_set_comp",
                    "_gen_comp",
                    "__listcomp__",
                    "__setcomp__",
                    "__genexpr__",
                )
                and x.args
            ):
                gen_sources = (
                    x.args[1:] if not fname.startswith("__") else (x.args[-1],)
                )
                clauses = _collect_gen_clauses(gen_sources)
                comp_bound = copy_names(bound)
                for clause in clauses:
                    spec = _decompose_gen_clause(clause)
                    if spec is None:
                        continue
                    target, _, _ = spec
                    if target is not None:
                        _collect_target_names(target, comp_bound)
                walk(x.args[0], comp_bound)
                running_bound = copy_names(bound)
                for clause in clauses:
                    spec = _decompose_gen_clause(clause)
                    if spec is None:
                        continue
                    target, iter_expr, ifs_expr = spec
                    if iter_expr is not None:
                        walk(iter_expr, running_bound)
                    if target is not None:
                        _collect_target_names(target, running_bound)
                    if ifs_expr is not None:
                        walk(ifs_expr, running_bound)
                return
            if fname in ("_dict_comp", "__dictcomp__") and x.args:
                gen_sources = (
                    x.args[1:] if not fname.startswith("__") else (x.args[-1],)
                )
                clauses = _collect_gen_clauses(gen_sources)
                comp_bound = copy_names(bound)
                for clause in clauses:
                    spec = _decompose_gen_clause(clause)
                    if spec is None:
                        continue
                    target, _, _ = spec
                    if target is not None:
                        _collect_target_names(target, comp_bound)
                walk(x.args[0], comp_bound)
                if fname == "__dictcomp__":
                    if len(x.args) >= 2:
                        walk(x.args[1], comp_bound)
                running_bound = copy_names(bound)
                for clause in clauses:
                    spec = _decompose_gen_clause(clause)
                    if spec is None:
                        continue
                    target, iter_expr, ifs_expr = spec
                    if iter_expr is not None:
                        walk(iter_expr, running_bound)
                    if target is not None:
                        _collect_target_names(target, running_bound)
                    if ifs_expr is not None:
                        walk(ifs_expr, running_bound)
                return
            if fname == "_gen_clause" and x.args:
                # _gen_clause(target, iter, (ifs,))
                target = x.args[0]
                new_bound = copy_names(bound)
                _collect_target_names(target, new_bound)
                for a in x.args[1:]:
                    walk(a, new_bound)
                return
        if isinstance(x, _Lambda):
            lambda_bound = copy_names(bound)
            for p in x.params:
                if p.name != "":
                    append_name_once(lambda_bound, p.name)
            walk(x.body, lambda_bound)
            return
        for slot in _dataclass_field_names(x):
            if slot == "span" or slot == "ty":
                continue
            v = _dataclass_field_value(x, slot, None)
            if isinstance(v, tuple):
                for it in v:
                    walk(it, bound)
            else:
                walk(v, bound)

    for s in fd.body:
        walk(s)
    extend_names_once(free, nonlocal_names)
    result = filter_capture_names(tuple(sorted(free)))
    # The cache key contains ``id(fd)`` because frontend AST records are not
    # hashable.  Retain the exact owner with the result: otherwise a rewritten
    # FuncDef can be released, its address can be reused, and an unrelated
    # nested function can inherit the stale capture set.
    cache[cache_key] = (result, fd)
    return result
