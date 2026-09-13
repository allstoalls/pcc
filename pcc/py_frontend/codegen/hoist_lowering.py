"""Nested function hoisting helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from dataclasses import replace as _replace

from ..py_ast import (
    Arg,
    Assign,
    Attr,
    AugAssign,
    BinOp,
    BoolExpr,
    BoolLit,
    BoolType,
    Break,
    Call,
    ClassDef,
    ClassType,
    Compare,
    Continue,
    Delete,
    DictExpr,
    DictType,
    DynType,
    ExceptHandler,
    Expr,
    ExprStmt,
    FloatLit,
    FloatType,
    For,
    FuncDef,
    FuncType,
    Global,
    If,
    IfExpr,
    Import,
    ImportFrom,
    IntLit,
    IntType,
    Lambda,
    ListExpr,
    ListType,
    Module,
    Name,
    Nonlocal,
    NoneLit,
    NoneType,
    Pass,
    Raise,
    Return,
    Slice,
    SourceSpan,
    Stmt,
    StrLit,
    StrType,
    Subscript,
    Try,
    TupleExpr,
    TupleType,
    Type,
    UnaryOp,
    While,
    With,
)

_Assign = Assign
_Attr = Attr
_AugAssign = AugAssign
_BinOp = BinOp
_BoolExpr = BoolExpr
_Break = Break
_Call = Call
_ClassDef = ClassDef
_Compare = Compare
_Continue = Continue
_Delete = Delete
_DictExpr = DictExpr
_DynType = DynType
_ExprStmt = ExprStmt
_ExprStmt2 = ExprStmt
_For = For
_FuncDef = FuncDef
_GL = Global
_Global = Global
_If = If
_IfExpr = IfExpr
_Import = Import
_ImportFrom = ImportFrom
_ImportFromStmt = ImportFrom
_ImportStmt = Import
_Lambda = Lambda
_ListExpr = ListExpr
_Name = Name
_NL = Nonlocal
_Nonlocal = Nonlocal
_Pass = Pass
_Raise = Raise
_Return = Return
_Slice = Slice
_Subscript = Subscript
_TopImport = Import
_TopImportFrom = ImportFrom
_Try = Try
_TupleExpr = TupleExpr
_UnaryOp = UnaryOp
_While = While
_With = With
_DYN = DynType(name="dyn")

from .hoist_analysis import (
    _PY_BUILTINS_NS,
    _dataclass_field_names,
    _dataclass_field_value,
    _import_names_from_stmt,
    _is_import_from_stmt,
    _is_import_stmt,
    append_name_once,
    body_augassigns_free_name,
    body_reads_self,
    body_returns_name,
    body_uses_name_as_value,
    clone_funcdef,
    copy_name_map,
    copy_names,
    extend_names_once,
    filter_capture_names,
    filter_self_capture_names,
    hoist_stat_inc,
    is_discard_capture_name,
    module_may_need_hoist_fast,
    name_in,
    update_name_map,
    write_hoist_profile,
)
from .hoist_boxing import box_outer_body, collect_scope_bindings, function_boxed_names, scope_declared_names
from .hoist_free_names import compute_free_names as analyze_free_names
from .hoist_predicates import (
    body_has_yield,
    body_needs_nested_rewrite,
    hoist_stmt_kind,
)


def _hoist_decorator_name(dec):
    if isinstance(dec, _Name):
        return dec.ident
    if isinstance(dec, _Attr):
        base = _hoist_decorator_name(dec.obj)
        if base:
            return base + "." + dec.name
        return dec.name
    if isinstance(dec, _Call):
        return _hoist_decorator_name(dec.func)
    return None


def _hoist_method_kind(fd) -> str:
    kind = "instance"
    for dec in getattr(fd, "decorators", ()):
        name = _hoist_decorator_name(dec)
        if name == "staticmethod" or (
            name is not None and name.endswith(".staticmethod")
        ):
            return "static"
        if name == "classmethod" or (
            name is not None and name.endswith(".classmethod")
        ):
            kind = "classmethod"
    return kind


def _hoist_log(enabled: bool, mod_name: str, label: str) -> None:
    if not enabled:
        return
    sys.stderr.write("[pcc.hoist] " + mod_name + ":" + label + "\n")


def _hoist_cache_key(prefix: str, fd, names=(), extra: str = "") -> str:
    key = prefix + ":" + str(id(fd)) + ":" + extra
    for name in names:
        key = key + "|" + str(name)
    return key


def _hoist_cache_key4(prefix: str, fd, names_a, names_b, names_c) -> str:
    key = prefix + ":" + str(id(fd))
    for name in names_a:
        key = key + "|a:" + str(name)
    for name in names_b:
        key = key + "|b:" + str(name)
    for name in names_c:
        key = key + "|c:" + str(name)
    return key


class _HoistLoweringPass:
    def _hoist_nested_funcdefs(self) -> list:
        """Walk every top-level FuncDef / ClassDef method body and
        collect any nested FuncDef to the module's top-level body.

        The nested ``def`` is renamed ``__nested_<outer>_<name>`` and
        re-attached to ``self.ast_module.body``. No closure conversion
        — if the hoisted function reads an outer local, codegen
        surfaces the usual ``unbound name`` error at its first
        reference. Common use in pcc's own sources (regex callbacks,
        comparator helpers) doesn't capture anything, so the hoist
        alone buys a lot.

        Also rewrites same-scope ``Call(Name(<inner>), ...)`` sites
        in the outer body to route through the new hoisted symbol.
        Returns the list of hoisted FuncDef nodes (caller re-declares
        them via ``_declare_user_function`` during the normal scan).
        """
        debug_hoist = bool(os.environ.get("PCC_DEBUG_HOIST"))

        ast_module = self.ast_module
        mod_name = ast_module.name or "<module>"

        hoisted: list = []
        hoisted_capture_params = self._hoisted_capture_params
        hoisted_capture_params.clear()
        hoisted_class_capture_params = self._hoisted_class_capture_params
        hoisted_class_capture_params.clear()
        hoisted_enclosing_class = self._hoisted_enclosing_class
        hoisted_enclosing_class.clear()
        hoisted_enclosing_method_kind = self._hoisted_enclosing_method_kind
        hoisted_enclosing_method_kind.clear()
        closure_boxed_params = self._closure_boxed_params
        closure_boxed_params.clear()
        hoist_wrap_caps = self._hoist_wrap_caps
        hoist_wrap_caps.clear()
        generator_func_names = self._generator_func_names
        hoist_profile_path = os.environ.get("PCC_HOIST_PROFILE_PATH", "").strip()
        hoist_profile_enabled = bool(hoist_profile_path)
        hoist_stats = {
            "compute_free_names_calls": 0,
            "compute_free_names_cache_hits": 0,
            "called_sibling_names_calls": 0,
            "called_sibling_names_cache_hits": 0,
            "referenced_sibling_names_calls": 0,
            "referenced_sibling_names_cache_hits": 0,
            "sibling_effective_free_names_calls": 0,
            "sibling_effective_free_names_cache_hits": 0,
        }
        may_need_hoist = module_may_need_hoist_fast(ast_module.body)
        if not may_need_hoist:
            _hoist_log(debug_hoist, mod_name, "module skip")
            write_hoist_profile(
                hoist_profile_enabled,
                hoist_profile_path,
                hoist_stats,
            )
            return hoisted
        compute_free_names_cache = {}
        boxed_function_defs = {}

        def function_binds_cell(fd):
            return boxed_function_defs.get(id(fd)) is fd

        module_scope_names_base_items = []

        def add_module_scope_target_names(t):
            if isinstance(t, _Name):
                append_name_once(module_scope_names_base_items, t.ident)
            elif (
                isinstance(t, _Call)
                and isinstance(t.func, _Name)
                and t.func.ident in ("*", "__starred__")
                and t.args
            ):
                add_module_scope_target_names(t.args[0])
            elif isinstance(t, _TupleExpr):
                for e in t.elems:
                    add_module_scope_target_names(e)

        for module_scope_stmt in ast_module.body:
            if isinstance(module_scope_stmt, (_FuncDef, _ClassDef)):
                append_name_once(module_scope_names_base_items, module_scope_stmt.name)
            elif _is_import_stmt(module_scope_stmt):
                if _is_import_from_stmt(module_scope_stmt):
                    for imported_name, as_name in _import_names_from_stmt(
                        module_scope_stmt,
                    ):
                        if imported_name == "*":
                            continue
                        append_name_once(
                            module_scope_names_base_items,
                            as_name or imported_name,
                        )
                elif isinstance(
                    module_scope_stmt, (_TopImport, _ImportStmt)
                ) or not hasattr(
                    module_scope_stmt,
                    "module",
                ):
                    for imported_mod_name, as_name in _import_names_from_stmt(
                        module_scope_stmt,
                    ):
                        bound = as_name or imported_mod_name.split(".", 1)[0]
                        if bound:
                            append_name_once(module_scope_names_base_items, bound)
            elif isinstance(module_scope_stmt, _Assign):
                for target in module_scope_stmt.targets:
                    add_module_scope_target_names(target)
        module_scope_names_base = tuple(module_scope_names_base_items)

        def existing_top_or_hoisted_names(include_classes=False):
            out = []
            for h in hoisted:
                append_name_once(out, h.name)
            for s in ast_module.body:
                if isinstance(s, _FuncDef):
                    append_name_once(out, s.name)
                elif include_classes and isinstance(s, _ClassDef):
                    append_name_once(out, s.name)
            return out

        def analyze_names(
            fd,
            excluded,
            own_name=None,
            outer_scope_names=(),
        ):
            return analyze_free_names(
                fd,
                excluded,
                own_name,
                outer_scope_names,
                module_scope_names_base,
                existing_top_or_hoisted_names,
                compute_free_names_cache,
                hoist_profile_enabled,
                hoist_stats,
            )

        def mutable_captures_in_fd(fd, excluded):
            free = analyze_names(fd, excluded)
            mutated = list(scope_declared_names(fd.body, False, True))
            if not free and not mutated:
                return ()

            def walk(node):
                if isinstance(node, _Assign):
                    for target in node.targets:
                        if isinstance(target, _Name) and name_in(
                            free,
                            target.ident,
                        ):
                            append_name_once(mutated, target.ident)
                    walk(_dataclass_field_value(node, "value"))
                    return
                if isinstance(node, _AugAssign):
                    if isinstance(node.target, _Name) and name_in(
                        free,
                        node.target.ident,
                    ):
                        append_name_once(mutated, node.target.ident)
                    walk(node.value)
                    return
                for slot in _dataclass_field_names(node):
                    value = _dataclass_field_value(node, slot, None)
                    if isinstance(value, tuple):
                        for item in value:
                            walk(item)
                    else:
                        walk(value)

            for stmt in fd.body:
                walk(stmt)
            return tuple(mutated)

        def collect_all_mutable_captures(body):
            boxed = []
            for stmt in body:
                if isinstance(stmt, _FuncDef):
                    extend_names_once(boxed, mutable_captures_in_fd(stmt, ()))
                    extend_names_once(
                        boxed,
                        collect_all_mutable_captures(stmt.body),
                    )
                    continue
                for slot in _dataclass_field_names(stmt):
                    value = _dataclass_field_value(stmt, slot, None)
                    if isinstance(value, tuple):
                        for item in value:
                            if _dataclass_field_names(item) and isinstance(
                                item,
                                _FuncDef,
                            ):
                                extend_names_once(
                                    boxed,
                                    mutable_captures_in_fd(item, ()),
                                )
                                extend_names_once(
                                    boxed,
                                    collect_all_mutable_captures(item.body),
                                )
            return tuple(boxed)

        def collect_first_class_closure_captures(body):
            boxed = []

            def walk_block(stmts):
                for stmt in stmts:
                    if isinstance(stmt, _FuncDef):
                        if body_uses_name_as_value(stmts, stmt.name):
                            extend_names_once(boxed, analyze_names(stmt, ()))
                        walk_block(stmt.body)
                        continue
                    if isinstance(stmt, _If):
                        walk_block(stmt.body)
                        walk_block(stmt.else_body)
                        continue
                    if isinstance(stmt, _While):
                        walk_block(stmt.body)
                        walk_block(stmt.else_body)
                        continue
                    if isinstance(stmt, _For):
                        walk_block(stmt.body)
                        walk_block(stmt.else_body)
                        continue
                    if isinstance(stmt, _Try):
                        walk_block(stmt.body)
                        for handler in stmt.handlers:
                            walk_block(
                                _dataclass_field_value(handler, "body", ())
                            )
                        walk_block(stmt.else_body)
                        walk_block(stmt.finally_body)
                        continue
                    if isinstance(stmt, _With):
                        walk_block(stmt.body)

            walk_block(body)
            return tuple(boxed)

        def boxed_capture_names(body):
            boxed = []
            extend_names_once(boxed, collect_all_mutable_captures(body))
            extend_names_once(
                boxed,
                collect_first_class_closure_captures(body),
            )
            return tuple(boxed)

        def body_reads_free_names(fd, excluded):
            return bool(analyze_names(fd, excluded))


        def rewrite_body(
            stmts,
            rename_map,
            scope_names,
            enclosing_class_name=None,
            enclosing_method_kind=None,
        ):
            """Return a new body tuple with nested defs stripped out
            and inner-name Call sites rewritten through rename_map."""
            _hoist_log(debug_hoist, mod_name, "rewrite_body begin")
            locally_bound_names_cache = {}
            called_sibling_names_cache = {}
            referenced_sibling_names_cache = {}
            forwarded_value_capture_names_cache = {}
            sibling_effective_free_names_cache = {}
            # Pre-scan: pretend every sibling nested FuncDef already
            # hoisted so mutual-recursive siblings don't capture each
            # other as free vars. Each will get its real hoisted name
            # inserted when the main loop reaches it.
            prescan_map = copy_name_map(rename_map)
            for st in stmts:
                if (
                    isinstance(st, _FuncDef) and st.name not in prescan_map
                    and not function_binds_cell(st)
                ):
                    # Placeholder value — actual hoisted name assigned
                    # during the hoist branch below. Existence in the
                    # map is what ``compute_free_names``' excluded
                    # argument needs.
                    prescan_map[st.name] = (f"__nested_{st.name}", ())
            _hoist_log(debug_hoist, mod_name, "rewrite_body prescan done")
            sibling_names = []
            for st in stmts:
                if isinstance(st, _FuncDef) and not function_binds_cell(st):
                    append_name_once(sibling_names, st.name)

            def filter_sibling_capture_names(names, current_sibling_names):
                out = []
                for name in names:
                    if not name_in(current_sibling_names, name):
                        out.append(name)
                return tuple(out)

            def filter_renamed_capture_names(names):
                out = []
                for name in names:
                    discard = False
                    for key, mapped in rename_map.items():
                        if isinstance(mapped, str) and mapped.startswith("__pcc_closure_value_"):
                            continue
                        mapped_name = mapped[0] if isinstance(mapped, tuple) else mapped
                        if name == key or name == mapped_name:
                            discard = True
                            break
                    if not discard:
                        out.append(name)
                return tuple(out)

            def locally_bound_names(fd):
                """Names that are already available inside ``fd`` without
                capturing them from the enclosing scope."""
                cache_key = _hoist_cache_key("local", fd)
                cached_entry = locally_bound_names_cache.get(cache_key)
                if cached_entry is not None and cached_entry[1] is fd:
                    return cached_entry[0]
                bound = []
                for a in fd.args:
                    if a.name != "":
                        append_name_once(bound, a.name)
                extend_names_once(bound, collect_scope_bindings(fd.body))
                result = tuple(bound)
                locally_bound_names_cache[cache_key] = (result, fd)
                return result

            def called_sibling_names(fd, current_sibling_names):
                """Collect sibling nested defs called directly from
                ``fd``. Their own captures must be threaded through the
                caller once the call site is rewritten to the hoisted
                form with synthetic capture kwargs."""
                cache_key = _hoist_cache_key("called", fd)
                cached_entry = called_sibling_names_cache.get(cache_key)
                if cached_entry is not None and cached_entry[1] is fd:
                    hoist_stat_inc(
                        hoist_profile_enabled,
                        hoist_stats,
                        "called_sibling_names_cache_hits",
                    )
                    return cached_entry[0]
                hoist_stat_inc(
                    hoist_profile_enabled,
                    hoist_stats,
                    "called_sibling_names_calls",
                )
                out = []
                local_bound = locally_bound_names(fd)

                def walk(x):
                    if x is None:
                        return
                    if isinstance(x, tuple):
                        for it in x:
                            walk(it)
                        return
                    if isinstance(x, (_FuncDef, _ClassDef)):
                        return
                    if isinstance(x, _Call):
                        fname = getattr(x.func, "ident", None)
                        if (
                            isinstance(x.func, _Name)
                            and isinstance(fname, str)
                            and name_in(current_sibling_names, fname)
                            and not name_in(local_bound, fname)
                        ):
                            append_name_once(out, fname)
                        walk(x.func)
                        for arg in x.args:
                            walk(arg)
                        for _key, value in x.kwargs:
                            walk(value)
                        return
                    if isinstance(x, _Attr):
                        walk(x.obj)
                        return
                    if isinstance(x, _Subscript):
                        walk(x.obj)
                        walk(x.idx)
                        return
                    if isinstance(x, _Slice):
                        walk(x.lo)
                        walk(x.hi)
                        walk(x.step)
                        return
                    if isinstance(x, _BinOp):
                        walk(x.lhs)
                        walk(x.rhs)
                        return
                    if isinstance(x, _UnaryOp):
                        walk(x.operand)
                        return
                    if isinstance(x, _Compare):
                        walk(x.lhs)
                        walk(x.rhs)
                        return
                    if isinstance(x, _BoolExpr):
                        walk(x.left)
                        walk(x.right)
                        return
                    if isinstance(x, _IfExpr):
                        walk(x.cond)
                        walk(x.then_e)
                        walk(x.else_e)
                        return
                    if isinstance(x, (_ListExpr, _TupleExpr)):
                        for item in x.elems:
                            walk(item)
                        return
                    if isinstance(x, _DictExpr):
                        for key, value in x.pairs:
                            walk(key)
                            walk(value)
                        return
                    if isinstance(x, _Lambda):
                        walk(x.body)
                        return
                    if isinstance(x, _Assign):
                        for target in x.targets:
                            walk(target)
                        walk(x.value)
                        return
                    if isinstance(x, _AugAssign):
                        walk(x.target)
                        walk(x.value)
                        return
                    if isinstance(x, _ExprStmt):
                        walk(x.expr)
                        return
                    if isinstance(x, _Return):
                        walk(x.value)
                        return
                    if isinstance(x, _If):
                        walk(x.cond)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _While):
                        walk(x.cond)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _For):
                        walk(x.target)
                        walk(x.iter)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _Try):
                        walk(x.body)
                        for handler in x.handlers:
                            walk(
                                _dataclass_field_value(
                                    handler,
                                    "exc_type",
                                    None,
                                )
                            )
                            walk(_dataclass_field_value(handler, "body", ()))
                        walk(x.else_body)
                        walk(x.finally_body)
                        return
                    if isinstance(x, _With):
                        for ctx_expr, as_var in x.items:
                            walk(ctx_expr)
                            walk(as_var)
                        walk(x.body)
                        return
                    if isinstance(x, _Raise):
                        walk(x.exc)
                        walk(x.cause)
                        return
                    if isinstance(x, _Delete):
                        for target in x.targets:
                            walk(target)

                for s in fd.body:
                    walk(s)
                result = tuple(out)
                called_sibling_names_cache[cache_key] = (result, fd)
                return result

            def referenced_sibling_names(fd, current_sibling_names):
                """Collect sibling nested defs referenced by ``fd`` in any
                expression position.

                ``called_sibling_names`` is intentionally narrow so call-site
                rewriting only fires for direct calls. Capture propagation has
                to be wider: a sibling used as a callback, stored in a local,
                or called inside an expression still needs its captures made
                available in the referencing function's environment.
                """
                cache_key = _hoist_cache_key("referenced", fd)
                cached_entry = referenced_sibling_names_cache.get(cache_key)
                if cached_entry is not None and cached_entry[1] is fd:
                    hoist_stat_inc(
                        hoist_profile_enabled,
                        hoist_stats,
                        "referenced_sibling_names_cache_hits",
                    )
                    return cached_entry[0]
                hoist_stat_inc(
                    hoist_profile_enabled,
                    hoist_stats,
                    "referenced_sibling_names_calls",
                )
                out = []

                def walk(x):
                    if x is None:
                        return
                    if isinstance(x, tuple):
                        for it in x:
                            walk(it)
                        return
                    if isinstance(x, (_FuncDef, _ClassDef)):
                        return
                    if isinstance(x, _Name) and name_in(current_sibling_names, x.ident):
                        if x.ident != fd.name:
                            append_name_once(out, x.ident)
                        return
                    if isinstance(x, _Call):
                        walk(x.func)
                        for arg in x.args:
                            walk(arg)
                        for _key, value in x.kwargs:
                            walk(value)
                        return
                    if isinstance(x, _Attr):
                        walk(x.obj)
                        return
                    if isinstance(x, _Subscript):
                        walk(x.obj)
                        walk(x.idx)
                        return
                    if isinstance(x, _Slice):
                        walk(x.lo)
                        walk(x.hi)
                        walk(x.step)
                        return
                    if isinstance(x, _BinOp):
                        walk(x.lhs)
                        walk(x.rhs)
                        return
                    if isinstance(x, _UnaryOp):
                        walk(x.operand)
                        return
                    if isinstance(x, _Compare):
                        walk(x.lhs)
                        walk(x.rhs)
                        return
                    if isinstance(x, _BoolExpr):
                        walk(x.left)
                        walk(x.right)
                        return
                    if isinstance(x, _IfExpr):
                        walk(x.cond)
                        walk(x.then_e)
                        walk(x.else_e)
                        return
                    if isinstance(x, (_ListExpr, _TupleExpr)):
                        for item in x.elems:
                            walk(item)
                        return
                    if isinstance(x, _DictExpr):
                        for key, value in x.pairs:
                            walk(key)
                            walk(value)
                        return
                    if isinstance(x, _Lambda):
                        walk(x.body)
                        return
                    if isinstance(x, _Assign):
                        for target in x.targets:
                            walk(target)
                        walk(x.value)
                        return
                    if isinstance(x, _AugAssign):
                        walk(x.target)
                        walk(x.value)
                        return
                    if isinstance(x, _ExprStmt):
                        walk(x.expr)
                        return
                    if isinstance(x, _Return):
                        walk(x.value)
                        return
                    if isinstance(x, _If):
                        walk(x.cond)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _While):
                        walk(x.cond)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _For):
                        walk(x.target)
                        walk(x.iter)
                        walk(x.body)
                        walk(x.else_body)
                        return
                    if isinstance(x, _Try):
                        walk(x.body)
                        for handler in x.handlers:
                            walk(
                                _dataclass_field_value(
                                    handler,
                                    "exc_type",
                                    None,
                                )
                            )
                            walk(_dataclass_field_value(handler, "body", ()))
                        walk(x.else_body)
                        walk(x.finally_body)
                        return
                    if isinstance(x, _With):
                        for ctx_expr, as_var in x.items:
                            walk(ctx_expr)
                            walk(as_var)
                        walk(x.body)
                        return
                    if isinstance(x, _Raise):
                        walk(x.exc)
                        walk(x.cause)
                        return
                    if isinstance(x, _Delete):
                        for target in x.targets:
                            walk(target)

                for s in fd.body:
                    walk(s)
                result = tuple(out)
                referenced_sibling_names_cache[cache_key] = (result, fd)
                return result

            def forwarded_value_capture_names(
                fd,
                excluded_names,
                outer_scope_names,
                outer_local_bound,
            ):
                """Captures needed by nested defs used as first-class values.

                When ``fd`` returns or stores a nested def (instead of
                calling it directly), the outer hoisted wrapper must carry
                any outer-scope captures that nested def still needs. This
                is the ``make_body_for -> body`` shape in layer1's own
                comprehension helpers.
                """
                cache_key = _hoist_cache_key4(
                    "forwarded",
                    fd,
                    excluded_names,
                    outer_scope_names,
                    outer_local_bound,
                )
                cached_entry = forwarded_value_capture_names_cache.get(cache_key)
                if cached_entry is not None and cached_entry[1] is fd:
                    return cached_entry[0]
                out = []
                for inner_fd in fd.body:
                    if not isinstance(inner_fd, _FuncDef):
                        continue
                    inner_local_bound = locally_bound_names(inner_fd)
                    inner_free = analyze_names(
                        inner_fd,
                        excluded_names,
                        outer_scope_names=outer_scope_names,
                    )
                    inner_forwarded = forwarded_value_capture_names(
                        inner_fd,
                        excluded_names,
                        outer_scope_names,
                        inner_local_bound,
                    )
                    inner_needed = []
                    extend_names_once(inner_needed, inner_free)
                    for fv in inner_forwarded:
                        if not name_in(inner_local_bound, fv):
                            append_name_once(inner_needed, fv)
                    if not body_uses_name_as_value(fd.body, inner_fd.name):
                        continue
                    for fv in inner_needed:
                        if name_in(outer_scope_names, fv) and not name_in(
                            outer_local_bound, fv
                        ):
                            append_name_once(out, fv)
                result = tuple(out)
                forwarded_value_capture_names_cache[cache_key] = (result, fd)
                return result

            excluded_prescan = []
            for k, v in prescan_map.items():
                append_name_once(excluded_prescan, k)
                if isinstance(v, tuple):
                    append_name_once(excluded_prescan, v[0])
                else:
                    append_name_once(excluded_prescan, v)

            def sibling_funcdef(name):
                for sibling_stmt in stmts:
                    if isinstance(sibling_stmt, _FuncDef) and sibling_stmt.name == name:
                        return sibling_stmt
                return None

            def sibling_effective_free_names(fd, seen_names):
                cache_key = _hoist_cache_key("effective", fd)
                cached_entry = sibling_effective_free_names_cache.get(cache_key)
                if cached_entry is not None and cached_entry[1] is fd:
                    hoist_stat_inc(
                        hoist_profile_enabled,
                        hoist_stats,
                        "sibling_effective_free_names_cache_hits",
                    )
                    return cached_entry[0]
                hoist_stat_inc(
                    hoist_profile_enabled,
                    hoist_stats,
                    "sibling_effective_free_names_calls",
                )
                out = []
                extend_names_once(
                    out,
                    analyze_names(
                        fd,
                        excluded_prescan,
                        outer_scope_names=scope_names,
                    ),
                )
                local_bound = locally_bound_names(fd)
                extend_names_once(
                    out,
                    forwarded_value_capture_names(
                        fd,
                        excluded_prescan,
                        scope_names,
                        local_bound,
                    ),
                )
                result = tuple(sorted(out))
                sibling_effective_free_names_cache[cache_key] = (result, fd)
                return result

            effective_free_names: dict[str, tuple] = {}
            _hoist_log(debug_hoist, mod_name, "rewrite_body effective begin")
            for st in stmts:
                if not isinstance(st, _FuncDef):
                    continue
                effective_free_names[st.name] = sibling_effective_free_names(
                    st,
                    () if function_binds_cell(st) else (st.name,),
                )
            _hoist_log(debug_hoist, mod_name, "rewrite_body effective seed done")
            # Fixed-point propagation for sibling nested functions.
            # If one nested function calls or otherwise references
            # another sibling, the caller must carry every outer-scope
            # capture needed by the callee. Otherwise a later value
            # wrapper (e.g. ``sorted(xs, key=_pred_sort_key)`` where
            # ``_pred_sort_key`` calls ``_branch_arm_priority``) tries
            # to materialize the callee's capture in the caller body
            # and finds it unbound.
            changed_effective = True
            while changed_effective:
                changed_effective = False
                for st in stmts:
                    if not isinstance(st, _FuncDef):
                        continue
                    current = []
                    extend_names_once(
                        current,
                        effective_free_names.get(st.name, ()),
                    )
                    local_bound = locally_bound_names(st)
                    deps = []
                    extend_names_once(deps, called_sibling_names(st, sibling_names))
                    extend_names_once(deps, referenced_sibling_names(st, sibling_names))
                    for dep in deps:
                        dep_caps = effective_free_names.get(dep, ())
                        for fv in dep_caps:
                            if (
                                name_in(scope_names, fv)
                                and not name_in(local_bound, fv)
                                and not is_discard_capture_name(fv)
                            ):
                                append_name_once(current, fv)
                    current_tuple = tuple(sorted(current))
                    current_tuple = filter_capture_names(current_tuple)
                    current_tuple = filter_sibling_capture_names(
                        current_tuple,
                        sibling_names,
                    )
                    current_tuple = filter_renamed_capture_names(current_tuple)
                    if current_tuple != effective_free_names.get(st.name, ()):
                        effective_free_names[st.name] = current_tuple
                        changed_effective = True
            _hoist_log(debug_hoist, mod_name, "rewrite_body fixedpoint done")
            for sibling_name in sibling_names:
                mapped = prescan_map.get(sibling_name)
                if isinstance(mapped, tuple):
                    raw_capture_names = effective_free_names.get(sibling_name, ())
                    capture_names = filter_capture_names(raw_capture_names)
                    capture_names = filter_sibling_capture_names(
                        capture_names,
                        sibling_names,
                    )
                    capture_names = filter_renamed_capture_names(capture_names)
                    prescan_map[sibling_name] = (
                        mapped[0],
                        tuple(sorted(capture_names)),
                    )
            new_stmts = []
            stmt_debug_idx = 0
            for st in stmts:
                _hoist_log(
                    debug_hoist,
                    mod_name,
                    "rewrite stmt " + str(stmt_debug_idx) + " " + hoist_stmt_kind(st),
                )
                stmt_debug_idx += 1
                synthesized_lambda_func = False
                # ``name = lambda params: body`` — rewrite to a regular
                # nested ``def name(params): return body`` statement so
                # the lambda lifting falls out of the existing FuncDef
                # hoist (closure conversion + recursive-call rewrite
                # already apply). Matches Python's equivalence for
                # name-bound lambdas.
                lambda_target_ident = ""
                if (
                    isinstance(st, _Assign)
                    and isinstance(st.value, _Lambda)
                    and len(st.targets) == 1
                    and isinstance(st.targets[0], _Name)
                ):
                    lambda_target_ident = _dataclass_field_value(
                        st.targets[0],
                        "ident",
                        "",
                    )
                if (
                    lambda_target_ident
                    and isinstance(st, _Assign)
                    and isinstance(st.value, _Lambda)
                    and len(st.targets) == 1
                    and isinstance(st.targets[0], _Name)
                ):
                    lam = st.value
                    # Give each lambda param a DynType annotation when
                    # none was declared (lambdas usually omit them),
                    # matching the annotation gate in
                    # ``_declare_user_function``.
                    params_list = []
                    for a in lam.params:
                        param = a
                        if a.annotation is None:
                            param = _replace(a, annotation=_DYN)
                        params_list.append(param)
                    params = tuple(params_list)
                    return_stmt = Return(span=st.span, value=lam.body)
                    body_tuple = (return_stmt,)
                    fd_stmt = FuncDef(
                        span=st.span,
                        name=lambda_target_ident,
                        args=params,
                        return_ty=_DYN,
                        body=body_tuple,
                        is_async=False,
                        decorators=(),
                    )
                    lambda_free_names = tuple(
                        sorted(
                            analyze_names(
                                fd_stmt,
                                excluded_prescan,
                                outer_scope_names=scope_names,
                            )
                        )
                    )
                    lambda_free_names = filter_capture_names(lambda_free_names)
                    lambda_free_names = filter_sibling_capture_names(
                        lambda_free_names,
                        sibling_names,
                    )
                    lambda_free_names = filter_renamed_capture_names(lambda_free_names)
                    if not lambda_free_names:
                        hoist_name = f"__nested_{fd_stmt.name}"
                        suffix = 0
                        existing = existing_top_or_hoisted_names(False)
                        final_name = hoist_name
                        while name_in(existing, final_name):
                            suffix += 1
                            final_name = f"{hoist_name}_{suffix}"
                        hoisted_capture_params[final_name] = ()
                        hoisted_fd = clone_funcdef(
                            fd_stmt,
                            final_name,
                            params,
                            _DYN,
                            fd_stmt.body,
                        )
                        hoisted.append(hoisted_fd)
                        rename_map[fd_stmt.name] = (final_name, ())
                        continue
                    st = fd_stmt
                    synthesized_lambda_func = True
                if not synthesized_lambda_func and isinstance(st, _ClassDef):
                    # Hoist nested ClassDef to module top level so
                    # ``_declare_user_function`` / ``emit_methods`` can
                    # process it via the standard class path. Nested
                    # instance methods sometimes read outer locals
                    # (e.g. ``ctx`` in a pass-local helper class); for
                    # those, rewrite bare capture reads to hidden
                    # instance attrs and attach the values at
                    # instantiation time. This is a one-shot closure
                    # approximation, but it unblocks the self-host
                    # survey on current nested-helper-class patterns.
                    existing = existing_top_or_hoisted_names(True)
                    hoist_name = st.name
                    suffix = 0
                    while name_in(existing, hoist_name):
                        suffix += 1
                        hoist_name = f"{st.name}_nest{suffix}"
                    class_free_names = []
                    new_class_body = []
                    for body_stmt in st.body:
                        if isinstance(body_stmt, _FuncDef):
                            recv_name = None
                            for a in body_stmt.args:
                                if a.name != "":
                                    recv_name = a.name
                                    break
                            method_free_names = []
                            for fv in analyze_names(
                                body_stmt,
                                excluded_prescan,
                                outer_scope_names=scope_names,
                            ):
                                if name_in(
                                    scope_names, fv
                                ) and not is_discard_capture_name(fv):
                                    append_name_once(method_free_names, fv)
                            method_free = tuple(method_free_names)
                            if recv_name is not None and method_free:
                                cap_names = method_free

                                def rewrite_cap_node(x):
                                    if x is None:
                                        return x
                                    if isinstance(x, (_FuncDef, _ClassDef)):
                                        return x
                                    if isinstance(x, tuple):
                                        rewritten_items = []
                                        for it in x:
                                            rewritten_items.append(rewrite_cap_node(it))
                                        return tuple(rewritten_items)
                                    if isinstance(x, Name) and name_in(
                                        cap_names, x.ident
                                    ):
                                        return Attr(
                                            span=x.span,
                                            ty=_DYN,
                                            obj=Name(
                                                span=x.span,
                                                ty=_DYN,
                                                ident=recv_name,
                                            ),
                                            name=f"__pcc_cap_{x.ident}",
                                        )
                                    fields = tuple(_dataclass_field_names(x))
                                    if fields:
                                        replacements = {}
                                        for slot in fields:
                                            v = _dataclass_field_value(x, slot, None)
                                            new_v = rewrite_cap_node(v)
                                            if new_v != v:
                                                replacements[slot] = new_v
                                        if replacements:
                                            return _replace(x, **replacements)
                                    return x

                                rewritten_body_items = []
                                for s in body_stmt.body:
                                    rewritten_body_items.append(rewrite_cap_node(s))
                                body_stmt = _replace(
                                    body_stmt,
                                    body=tuple(rewritten_body_items),
                                )
                                extend_names_once(class_free_names, method_free)
                        new_class_body.append(body_stmt)
                    hoisted_cd = _replace(
                        st,
                        name=hoist_name,
                        body=tuple(new_class_body),
                    )
                    # Keep the rewritten ClassDef in the enclosing body as
                    # an executable statement.  Its methods are declared
                    # from the module-level copy below, but Python constructs
                    # a fresh class object (including bases, namespace and
                    # decorators) every time execution reaches the statement.
                    # Dropping it here turned a function-local class into a
                    # one-shot module global and lost per-call identity and
                    # closure captures.
                    new_stmts.append(hoisted_cd)
                    hoisted.append(hoisted_cd)
                    # Presence in this table distinguishes a synthetic local
                    # class from a source-level module class even when the
                    # class captures no outer names.
                    hoisted_class_capture_params[hoist_name] = tuple(
                        sorted(class_free_names)
                    )
                    if hoist_name != st.name:
                        rename_map[st.name] = hoist_name
                    continue
                if synthesized_lambda_func or isinstance(st, _FuncDef):
                    # Closure conversion threads ``self`` through as
                    # just another free-var capture when the nested
                    # def is inside a method body. The hoisted symbol
                    # lands at module scope with ``self`` as a
                    # trailing DynType param; ``self.<attr>`` reads
                    # then resolve via the DynType attribute path
                    # (``py_cpy_getattr`` / ``py_obj_getattr``) at
                    # runtime — slower than the compile-time class
                    # layout path but correct, and good enough to
                    # unblock solo-compile on the affected files.
                    binds_cell = function_binds_cell(st)
                    needs_binding = binds_cell or body_uses_name_as_value(stmts, st.name)
                    if needs_binding:
                        # Filled with the final symbol and captures below.
                        hoist_wrap_caps[st.name] = {
                            "original_arity": sum(1 for a in st.args if a.name != ""),
                            "free_names": (),
                            "hoisted_name": None,
                        }
                    # Mutable-capture path: if the nested def mutates
                    # a free variable (``nonlocal X; X += 1`` pattern),
                    # the outer body has already been preprocessed by
                    # ``box_outer_body`` to box that name into a
                    # 1-element list. Every read/write of X in both
                    # outer and inner goes through ``X[0]`` subscript
                    # lookups, so closure-by-value is now correct —
                    # the list reference is shared.
                    # ``prescan_map`` already includes every sibling
                    # nested FuncDef's (original and hoisted) names —
                    # so mutual-recursive siblings don't capture each
                    # other as free vars. Also folds in outer-scope
                    # hoisted siblings that live in ``rename_map``.
                    excluded = []
                    for k, v in prescan_map.items():
                        append_name_once(excluded, k)
                        if isinstance(v, tuple):
                            append_name_once(excluded, v[0])
                        else:
                            append_name_once(excluded, v)
                    local_bound = locally_bound_names(st)
                    free_names = tuple(
                        sorted(
                            effective_free_names.get(
                                st.name,
                                analyze_names(
                                    st,
                                    excluded,
                                    outer_scope_names=scope_names,
                                ),
                            )
                        )
                    )
                    free_names = filter_capture_names(free_names)
                    free_names = filter_sibling_capture_names(
                        free_names,
                        sibling_names,
                    )
                    free_names = filter_renamed_capture_names(free_names)
                    # Pick the hoisted symbol early so self-referential
                    # free-var detection works.
                    hoist_name = f"__nested_{st.name}"
                    suffix = 0
                    existing = existing_top_or_hoisted_names(False)
                    final_name = hoist_name
                    while name_in(existing, final_name):
                        suffix += 1
                        final_name = f"{hoist_name}_{suffix}"
                    free_names = filter_self_capture_names(
                        free_names,
                        "" if binds_cell else st.name,
                        final_name,
                    )
                    # Each lexical owner creates its own cells. Captured
                    # parameters of nested factories need entry-time boxing
                    # under the hoisted function name used by codegen.
                    inner_source_body = box_outer_body(
                        st.body,
                        final_name,
                        tuple(a.name for a in st.args if a.name != ""),
                        function_boxed_names(st, boxed_capture_names(st.body)),
                        closure_boxed_params,
                        boxed_function_defs,
                    )
                    while True:
                        # Closure conversion: prepend the free vars as
                        # extra trailing arguments with DynType annotation.
                        # Default to None so CPython-side kwarg fills
                        # aren't required, matching Python's no-default
                        # model for captured variables.
                        cap_args_list = []
                        for fv in free_names:
                            cap_args_list.append(
                                Arg(
                                    name=fv,
                                    annotation=_DYN,
                                    default=None,
                                    kind="pos",
                                    has_default=True,
                                )
                            )
                        cap_args = tuple(cap_args_list)
                        # Insert the captures BEFORE the first bare ``*``
                        # kw-only separator so the caller's trailing
                        # positional rewrite (appends captures after
                        # regular positionals) still maps correctly. A
                        # nested def like ``def f(a, b, *, k=1):`` stays
                        # ``def __nested_f(a, b, _cap1, _cap2, *, k=1)``
                        # rather than ending up with positional args
                        # after the kw-only separator, which
                        # ``_resolve_call_kwargs`` would reject.
                        orig_args = tuple(st.args)
                        split_idx = len(orig_args)
                        for i, a in enumerate(orig_args):
                            if a.name == "":
                                split_idx = i
                                break
                        new_args = (
                            orig_args[:split_idx] + cap_args + orig_args[split_idx:]
                        )
                        # Inside the inner body, recursive self-calls need
                        # to forward the same captured values. Seed the
                        # inner rename_map with the conversion entry so
                        # ``rewrite_expr`` rewrites the self-call's
                        # ``Call(Name(<inner_name>), args)`` to include
                        # the free vars as trailing positional args.
                        inner_map = copy_name_map(prescan_map)
                        update_name_map(inner_map, rename_map)
                        if not binds_cell:
                            inner_map[st.name] = (final_name, free_names)
                        inner_scope = copy_names(scope_names)
                        for a in st.args:
                            if a.name != "":
                                append_name_once(inner_scope, a.name)
                        extend_names_once(
                            inner_scope,
                            collect_scope_bindings(inner_source_body),
                        )
                        inner_body = rewrite_body(
                            inner_source_body,
                            inner_map,
                            inner_scope,
                            enclosing_class_name,
                            enclosing_method_kind,
                        )
                        forwarded = []
                        for fv in analyze_names(
                            clone_funcdef(
                                st,
                                st.name,
                                st.args,
                                st.return_ty,
                                inner_body,
                            ),
                            excluded,
                            outer_scope_names=scope_names,
                        ):
                            if (
                                name_in(scope_names, fv)
                                and not name_in(local_bound, fv)
                                and not is_discard_capture_name(fv)
                            ):
                                append_name_once(forwarded, fv)
                        extend_names_once(
                            forwarded,
                            forwarded_value_capture_names(
                                st,
                                excluded,
                                scope_names,
                                local_bound,
                            ),
                        )
                        for dep in called_sibling_names(st, sibling_names):
                            mapped = rename_map.get(dep)
                            if mapped is None:
                                continue
                            mapped_name = (
                                mapped[0] if isinstance(mapped, tuple) else mapped
                            )
                            dep_caps = hoisted_capture_params.get(
                                mapped_name,
                                (),
                            )
                            for fv in dep_caps:
                                if (
                                    name_in(scope_names, fv)
                                    and not name_in(local_bound, fv)
                                    and not is_discard_capture_name(fv)
                                ):
                                    append_name_once(forwarded, fv)
                        widened_names = []
                        extend_names_once(widened_names, free_names)
                        extend_names_once(widened_names, forwarded)
                        widened = tuple(sorted(widened_names))
                        widened = filter_self_capture_names(
                            widened,
                            "" if binds_cell else st.name,
                            final_name,
                        )
                        widened = filter_sibling_capture_names(
                            widened,
                            sibling_names,
                        )
                        widened = filter_renamed_capture_names(widened)
                        if widened == free_names:
                            break
                        free_names = widened
                    # Update the adapter-wrap metadata (if this def was
                    # flagged at the body_uses_name_as_value gate).
                    cap_entry = hoist_wrap_caps.get(st.name)
                    if cap_entry is not None:
                        cap_entry["free_names"] = tuple(free_names)
                        cap_entry["hoisted_name"] = final_name
                        cap_entry["original_name"] = st.name
                        # Mirror under the hoisted name so
                        # ``_emit_name(<hoisted>)`` can find the
                        # entry when the rewrite_expr bare-Name
                        # rewrite has already swapped the ident.
                        hoist_wrap_caps[final_name] = cap_entry
                    hoisted_capture_params[final_name] = tuple(free_names)
                    if enclosing_class_name is not None:
                        hoisted_enclosing_class[final_name] = enclosing_class_name
                    if enclosing_method_kind is not None:
                        hoisted_enclosing_method_kind[final_name] = (
                            enclosing_method_kind
                        )
                    hoisted_fd = clone_funcdef(
                        st,
                        final_name,
                        new_args,
                        st.return_ty,
                        inner_body,
                    )
                    hoisted.append(hoisted_fd)
                    if binds_cell:
                        new_stmts.append(_Assign(
                            span=st.span,
                            targets=(_Subscript(
                                span=st.span, ty=_DYN,
                                obj=_Name(span=st.span, ty=_DYN, ident=st.name),
                                idx=IntLit(span=st.span, ty=IntType(name="int"), value=0),
                            ),),
                            value=_Name(span=st.span, ty=_DYN, ident=final_name),
                            annotation=None,
                        ))
                    elif needs_binding:
                        # Materialize once when this def executes. Re-reading
                        # the hoisted symbol made distinct function objects,
                        # dropping attributes assigned before a later return.
                        binding = "__pcc_closure_value_" + final_name
                        while name_in(scope_names, binding):
                            binding = binding + "_"
                        new_stmts.append(_Assign(
                            span=st.span,
                            targets=(_Name(span=st.span, ty=_DYN, ident=binding),),
                            value=_Name(span=st.span, ty=_DYN, ident=final_name),
                            annotation=None,
                        ))
                        append_name_once(scope_names, binding)
                        rename_map[st.name] = binding
                    else:
                        rename_map[st.name] = (final_name, free_names)
                    continue
                new_stmts.append(
                    rewrite_stmt(
                        st,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    )
                )
            return tuple(new_stmts)

        def rewrite_stmt(
            stmt,
            rename_map,
            scope_names,
            enclosing_class_name,
            enclosing_method_kind,
        ):
            if isinstance(stmt, _If):
                return _replace(
                    stmt,
                    cond=rewrite_expr(stmt.cond, rename_map),
                    body=rewrite_body(
                        stmt.body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                )
            if isinstance(stmt, _While):
                return _replace(
                    stmt,
                    cond=rewrite_expr(stmt.cond, rename_map),
                    body=rewrite_body(
                        stmt.body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                )
            if isinstance(stmt, _For):
                return _replace(
                    stmt,
                    iter=rewrite_expr(stmt.iter, rename_map),
                    body=rewrite_body(
                        stmt.body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                )
            if isinstance(stmt, _Try):
                new_handlers = []
                for h in stmt.handlers:
                    new_handlers.append(
                        _replace(
                            h,
                            body=rewrite_body(
                                _dataclass_field_value(h, "body", ()),
                                rename_map,
                                scope_names,
                                enclosing_class_name,
                                enclosing_method_kind,
                            ),
                        )
                    )
                return _replace(
                    stmt,
                    body=rewrite_body(
                        stmt.body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    finally_body=rewrite_body(
                        stmt.finally_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                    handlers=tuple(new_handlers),
                )
            if isinstance(stmt, _With):
                return _replace(
                    stmt,
                    body=rewrite_body(
                        stmt.body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                    ),
                )
            if isinstance(stmt, _ExprStmt):
                return _replace(stmt, expr=rewrite_expr(stmt.expr, rename_map))
            if isinstance(stmt, _Assign):
                new_targets = []
                for target in stmt.targets:
                    new_targets.append(rewrite_expr(target, rename_map))
                return _replace(
                    stmt,
                    targets=tuple(new_targets),
                    value=rewrite_expr(stmt.value, rename_map),
                )
            if isinstance(stmt, _AugAssign):
                return _replace(
                    stmt,
                    target=rewrite_expr(stmt.target, rename_map),
                    value=rewrite_expr(stmt.value, rename_map),
                )
            if isinstance(stmt, _Return):
                if stmt.value is None:
                    return stmt
                return _replace(
                    stmt,
                    value=rewrite_expr(stmt.value, rename_map),
                )
            # ``del expr[idx]`` / ``del expr.attr`` — the expr and idx
            # may contain a nested Call to a hoisted sibling. Walk
            # each target through rewrite_expr so the rename lands.
            from ..py_ast import Delete as _Delete

            if isinstance(stmt, _Delete):
                new_targets = []
                for target in stmt.targets:
                    new_targets.append(rewrite_expr(target, rename_map))
                return _replace(
                    stmt,
                    targets=tuple(new_targets),
                )
            return stmt

        def rewrite_expr_tuple(items, rename_map):
            out = []
            for item in items:
                out.append(rewrite_expr(item, rename_map))
            return tuple(out)

        def rewrite_kwargs(items, rename_map):
            out = []
            for key, value in items:
                out.append((key, rewrite_expr(value, rename_map)))
            return tuple(out)

        def rewrite_expr(expr, rename_map):
            if isinstance(expr, _Call):
                # Only rewrite the Call's callee slot — leaves non-call
                # ``Name`` references (e.g. passing ``repl`` as a value
                # to ``re.sub(repl, ...)``) unrewritten so they fail
                # as ``unbound name 'repl'`` at the original call site,
                # which is a more honest error than ``unbound name
                # '__nested_repl'``. First-class function values are a
                # separate, larger feature.
                new_func = expr.func
                extra_kwargs: tuple = ()
                if isinstance(new_func, _Name) and new_func.ident in rename_map:
                    mapped = rename_map[new_func.ident]
                    if isinstance(mapped, tuple):
                        final_name, free_names = mapped
                    else:
                        final_name, free_names = mapped, ()
                    new_func = _replace(new_func, ident=final_name)
                    # Closure conversion: pass each captured var as a
                    # keyword argument with the capture's own name. The
                    # hoisted function has a synthetic param of the
                    # same ident, so kwarg-by-name fills that slot and
                    # does NOT collide with any user-supplied kwarg
                    # whose formal is closer to the front of the param
                    # list (``_specialize(clones, take_true=True)``
                    # would otherwise treat a positional capture as the
                    # 2nd positional param ``take_true`` and then the
                    # kwarg would duplicate). Name-based passing lets
                    # ``_resolve_call_kwargs`` resolve each by formal
                    # ident, regardless of the capture's position in
                    # the hoisted signature.
                    # Each capture value is ``Name(fv)`` resolved in the
                    # outer (caller) scope. If the outer rename_map has
                    # a hoisted sibling of the same name — e.g.
                    # ``exact_alias_source`` has itself been hoisted —
                    # the capture value needs to be the hoisted sibling
                    # name so ``_emit_name`` resolves it; otherwise a
                    # bare ``Name(original)`` would be unbound.
                    extra_items = []
                    for fv in free_names:
                        mapped_capture = rename_map.get(fv)
                        if mapped_capture is not None:
                            if isinstance(mapped_capture, tuple):
                                target = mapped_capture[0]
                            else:
                                target = mapped_capture
                        else:
                            target = fv
                        extra_items.append(
                            (
                                fv,
                                Name(span=expr.span, ty=_DYN, ident=target),
                            )
                        )
                    extra_kwargs = tuple(extra_items)
                else:
                    new_func = rewrite_expr(new_func, rename_map)
                    extra_kwargs = ()
                return _replace(
                    expr,
                    func=new_func,
                    args=rewrite_expr_tuple(expr.args, rename_map),
                    kwargs=rewrite_kwargs(expr.kwargs, rename_map) + extra_kwargs,
                )
            # Bare Name at value position: if it matches a hoisted
            # nested def in rename_map, rewrite to the hoisted symbol
            # so downstream ``_emit_name`` finds the function in
            # ``self.functions`` and can wrap it as a CPython callable
            # via ``py_cpy_wrap_pcc_1arg``. This converts patterns
            # like ``pattern.sub(repl, text)`` / ``am.register(K, repl)``
            # from ``unbound name`` to a usable callable.
            if isinstance(expr, _Name) and expr.ident in rename_map:
                mapped = rename_map[expr.ident]
                target = mapped[0] if isinstance(mapped, tuple) else mapped
                return _replace(expr, ident=target)
            if isinstance(expr, _Attr):
                return _replace(expr, obj=rewrite_expr(expr.obj, rename_map))
            if isinstance(expr, _Subscript):
                return _replace(
                    expr,
                    obj=rewrite_expr(expr.obj, rename_map),
                    idx=rewrite_expr(expr.idx, rename_map),
                )
            if isinstance(expr, _Slice):
                return _replace(
                    expr,
                    lo=rewrite_expr(expr.lo, rename_map),
                    hi=rewrite_expr(expr.hi, rename_map),
                    step=rewrite_expr(expr.step, rename_map),
                )
            if isinstance(expr, _BinOp):
                return _replace(
                    expr,
                    lhs=rewrite_expr(expr.lhs, rename_map),
                    rhs=rewrite_expr(expr.rhs, rename_map),
                )
            if isinstance(expr, _UnaryOp):
                return _replace(expr, operand=rewrite_expr(expr.operand, rename_map))
            if isinstance(expr, _Compare):
                return _replace(
                    expr,
                    lhs=rewrite_expr(expr.lhs, rename_map),
                    rhs=rewrite_expr(expr.rhs, rename_map),
                )
            if isinstance(expr, _BoolExpr):
                return _replace(
                    expr,
                    left=rewrite_expr(expr.left, rename_map),
                    right=rewrite_expr(expr.right, rename_map),
                )
            if isinstance(expr, _IfExpr):
                return _replace(
                    expr,
                    cond=rewrite_expr(expr.cond, rename_map),
                    then_e=rewrite_expr(expr.then_e, rename_map),
                    else_e=rewrite_expr(expr.else_e, rename_map),
                )
            if isinstance(expr, (_ListExpr, _TupleExpr)):
                return _replace(expr, elems=rewrite_expr_tuple(expr.elems, rename_map))
            if isinstance(expr, _DictExpr):
                pairs = []
                for key, value in expr.pairs:
                    pairs.append(
                        (
                            rewrite_expr(key, rename_map),
                            rewrite_expr(value, rename_map),
                        )
                    )
                return _replace(expr, pairs=tuple(pairs))
            if isinstance(expr, _Lambda):
                inner_map = copy_name_map(rename_map)
                for p in expr.params:
                    if p.name in inner_map:
                        del inner_map[p.name]
                return _replace(expr, body=rewrite_expr(expr.body, inner_map))
            return expr

        def transform_generator_body(fd):
            """Mark generator functions for lazy state-machine lowering."""
            if not body_has_yield(fd.body):
                return fd
            generator_func_names.add(fd.name)
            return clone_funcdef(fd, fd.name, fd.args, _DYN, fd.body)

        new_top_body = []
        for stmt in ast_module.body:
            if isinstance(stmt, _FuncDef):
                _hoist_log(debug_hoist, mod_name, "func begin " + stmt.name)
                stmt = transform_generator_body(stmt)
                if body_needs_nested_rewrite(stmt.body):
                    # Pre-pass: box any outer locals that nested defs
                    # mutate as free vars. After boxing, the hoist can
                    # safely closure-convert-by-value — the list reference
                    # is shared between outer and inner.
                    scope_names = []
                    for a in stmt.args:
                        if a.name != "":
                            append_name_once(scope_names, a.name)
                    boxed_body = box_outer_body(
                        stmt.body,
                        stmt.name,
                        tuple(scope_names),
                        function_boxed_names(stmt, boxed_capture_names(stmt.body)),
                        closure_boxed_params,
                        boxed_function_defs,
                    )
                    extend_names_once(scope_names, collect_scope_bindings(boxed_body))
                    new_body = rewrite_body(boxed_body, {}, scope_names)
                else:
                    _hoist_log(debug_hoist, mod_name, "func skip " + stmt.name)
                    new_body = stmt.body
                new_top_body.append(
                    clone_funcdef(
                        stmt,
                        stmt.name,
                        stmt.args,
                        stmt.return_ty,
                        new_body,
                    )
                )
                _hoist_log(debug_hoist, mod_name, "func end " + stmt.name)
            elif isinstance(stmt, _ClassDef):
                # Rewrite each method's body.
                new_methods = []
                for m in stmt.body:
                    if isinstance(m, _FuncDef):
                        _hoist_log(
                            debug_hoist,
                            mod_name,
                            "method begin " + stmt.name + "." + m.name,
                        )
                        m = transform_generator_body(m)
                        scope_names = []
                        for a in m.args:
                            if a.name != "":
                                append_name_once(scope_names, a.name)
                        if body_needs_nested_rewrite(m.body):
                            method_owner_name = stmt.name + "." + m.name
                            boxed_body = box_outer_body(
                                m.body,
                                method_owner_name,
                                tuple(scope_names),
                                function_boxed_names(m, boxed_capture_names(m.body)),
                                closure_boxed_params,
                                boxed_function_defs,
                            )
                            extend_names_once(
                                scope_names,
                                collect_scope_bindings(boxed_body),
                            )
                            new_body = rewrite_body(
                                boxed_body,
                                {},
                                scope_names,
                                stmt.name,
                                _hoist_method_kind(m),
                            )
                        else:
                            _hoist_log(
                                debug_hoist,
                                mod_name,
                                "method skip " + stmt.name + "." + m.name,
                            )
                            new_body = m.body
                        new_methods.append(
                            clone_funcdef(
                                m,
                                m.name,
                                m.args,
                                m.return_ty,
                                new_body,
                            )
                        )
                        _hoist_log(
                            debug_hoist,
                            mod_name,
                            "method end " + stmt.name + "." + m.name,
                        )
                    else:
                        new_methods.append(m)
                new_top_body.append(_replace(stmt, body=tuple(new_methods)))
            else:
                new_top_body.append(stmt)
        new_top_body.extend(hoisted)
        ast_module = _replace(
            ast_module,
            body=tuple(new_top_body),
        )
        setattr(self, "ast_module", ast_module)
        setattr(self, "_ast_body", ast_module.body)
        write_hoist_profile(
            hoist_profile_enabled,
            hoist_profile_path,
            hoist_stats,
        )
        return hoisted

    # -- user-function declaration / definition -----------------------

    # ------------------------------------------------------- statements


def hoist_nested_funcdefs(codegen):
    """Run nested-function hoisting without widening Layer1's MRO."""
    return _HoistLoweringPass._hoist_nested_funcdefs(codegen)
