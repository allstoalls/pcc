"""Nested function hoisting helpers for L1CodeGen."""

from __future__ import annotations

import os
import sys
from dataclasses import replace as _replace

from pcc.frontends.python.py_ast import Arg, Assign, Attr, AugAssign, BinOp, BoolExpr, BoolLit, BoolType, Break, Call, ClassDef, ClassType, Compare, Continue, Delete, DictExpr, DictType, DynType, ExceptHandler, Expr, ExprStmt, FloatLit, FloatType, For, FuncDef, FuncType, Global, If, IfExpr, Import, ImportFrom, IntLit, IntType, Lambda, ListExpr, ListType, Module, Name, Nonlocal, NoneLit, NoneType, Pass, Raise, Return, Slice, SourceSpan, Stmt, StrLit, StrType, Subscript, Try, TupleExpr, TupleType, Type, UnaryOp, While, With

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

from pcc.frontends.python.codegen.hoist_analysis import _PY_BUILTINS_NS, _dataclass_field_names, _dataclass_field_value, _import_names_from_stmt, _is_import_from_stmt, _is_import_stmt, append_name_once, body_augassigns_free_name, body_reads_self, body_returns_name, body_uses_name_as_value, clone_funcdef, copy_name_map, copy_names, extend_names_once, filter_capture_names, filter_self_capture_names, hoist_stat_inc, is_discard_capture_name, module_may_need_hoist_fast, name_in, update_name_map, write_hoist_profile
from pcc.frontends.python.codegen.hoist_boxing import (
    CELL_READ,
    CELL_UNBOUND,
    local_method_capture_key,
    box_outer_body,
    collect_scope_bindings,
    function_boxed_names,
    function_local_bindings,
    late_bound_lambda_captures,
    scope_declared_names,
    stable_capture_parameters,
)
from pcc.frontends.python.codegen.hoist_free_names import compute_free_names as analyze_free_names
from pcc.frontends.python.codegen.hoist_predicates import body_has_yield, body_needs_nested_rewrite, hoist_stmt_kind
from pcc.frontends.python.codegen.decorator_lowering import (
    C_ABI_EXPORT_DECORATORS,
    lowered_at_declaration,
)


def _hoist_lexical_scope_names(fd, body, inherited):
    """Names available as lexical captures below this function scope.

    A global declaration is a barrier for descendants as well as this body.
    Nonlocals retain the enclosing cell; their writes do not create locals.
    Inspect the rewritten body too, so generated cell/import temporaries stay
    available without reinstating source-level global bindings.
    """
    globals_here = scope_declared_names(fd.body, True, False)
    external = scope_declared_names(fd.body, True, True)
    names = [name for name in inherited if not name_in(globals_here, name)]
    extend_names_once(names, function_local_bindings(fd))
    for name in collect_scope_bindings(body):
        if not name_in(external, name):
            append_name_once(names, name)
    return names


def _hoist_uses_class_cell(node):
    """Whether a function body reads ``super`` or ``__class__``.

    CPython's symtable counts a ``super`` load in a function as a use of the
    implicit ``__class__`` cell, and nested functions and lambdas inherit
    it. A nested class body binds its own cell, so it is not searched.
    """
    if node is None or isinstance(node, (str, int, float, bool, bytes)):
        return False
    if isinstance(node, (tuple, list)):
        for item in node:
            if _hoist_uses_class_cell(item):
                return True
        return False
    if isinstance(node, _Name):
        return node.ident == "super" or node.ident == "__class__"
    if isinstance(node, _ClassDef):
        return (_hoist_uses_class_cell(node.bases)
                or _hoist_uses_class_cell(node.keywords)
                or _hoist_uses_class_cell(node.decorators))
    for field in _dataclass_field_names(node):
        if field in ("span", "ty", "annotation", "return_ty"):
            continue
        if _hoist_uses_class_cell(_dataclass_field_value(node, field, None)):
            return True
    return False


def _hoist_class_cell_reads(node):
    """Read the ``__class__`` capture through its cell.

    Inside a function-local class method ``__class__`` is a free variable:
    reading it before the class exists raises CPython's free-variable
    NameError. Nested class bodies own their cell and are left alone.
    """
    if node is None or isinstance(node, (str, int, float, bool, bytes)):
        return node
    if isinstance(node, tuple):
        changed = False
        items = []
        for item in node:
            new_item = _hoist_class_cell_reads(item)
            changed = changed or new_item is not item
            items.append(new_item)
        return tuple(items) if changed else node
    if isinstance(node, _Name):
        if node.ident != "__class__":
            return node
        return _Call(
            span=node.span, ty=_DYN,
            func=_Name(span=node.span, ty=_DYN, ident=CELL_READ),
            args=(_Name(span=node.span, ty=_DYN, ident="__class__"),
                  StrLit(span=node.span, ty=StrType(name="str"), value="__class__"),
                  BoolLit(span=node.span, ty=BoolType(name="bool"), value=True)),
        )
    if isinstance(node, _ClassDef):
        return node
    if isinstance(node, _Call) and isinstance(node.func, _Name) and node.func.ident == CELL_READ:
        return node
    replacements = {}
    for field in _dataclass_field_names(node):
        if field in ("span", "ty", "annotation", "return_ty"):
            continue
        value = _dataclass_field_value(node, field, None)
        new_value = _hoist_class_cell_reads(value)
        if new_value is not value:
            replacements[field] = new_value
    if replacements:
        return _replace(node, **replacements)
    return node


def _hoist_nested_scope_funcdefs(body):
    """List immediate lexical child functions, including control-flow bodies."""
    children = []
    for statement in body:
        if isinstance(statement, _FuncDef):
            children.append(statement)
        elif isinstance(statement, (_If, _While, _For)):
            children.extend(_hoist_nested_scope_funcdefs(statement.body))
            children.extend(_hoist_nested_scope_funcdefs(statement.else_body))
        elif isinstance(statement, _Try):
            children.extend(_hoist_nested_scope_funcdefs(statement.body))
            for handler in statement.handlers:
                children.extend(_hoist_nested_scope_funcdefs(
                    _dataclass_field_value(handler, "body", ()),
                ))
            children.extend(_hoist_nested_scope_funcdefs(statement.else_body))
            children.extend(_hoist_nested_scope_funcdefs(statement.finally_body))
        elif isinstance(statement, _With):
            children.extend(_hoist_nested_scope_funcdefs(statement.body))
    return tuple(children)


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


def _hoist_module_scope_names(body):
    """Names a module body binds, and the subset its imports bind."""
    names = []
    import_names = []

    def add_target_names(t):
        if isinstance(t, _Name):
            append_name_once(names, t.ident)
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

    for statement in body:
        if isinstance(statement, (_FuncDef, _ClassDef)):
            append_name_once(names, statement.name)
        elif _is_import_stmt(statement):
            if _is_import_from_stmt(statement):
                for imported_name, as_name in _import_names_from_stmt(
                    statement,
                ):
                    if imported_name == "*":
                        continue
                    append_name_once(
                        names,
                        as_name or imported_name,
                    )
                    append_name_once(
                        import_names,
                        as_name or imported_name,
                    )
            elif isinstance(
                statement, (_TopImport, _ImportStmt)
            ) or not hasattr(
                statement,
                "module",
            ):
                for imported_mod_name, as_name in _import_names_from_stmt(
                    statement,
                ):
                    bound = as_name or imported_mod_name.split(".", 1)[0]
                    if bound:
                        append_name_once(names, bound)
                        append_name_once(import_names, bound)
        elif isinstance(statement, _Assign):
            for target in statement.targets:
                add_target_names(target)
    return tuple(names), import_names


def _hoist_executable_decorators(fd, scope_names, module_names, import_names):
    """The decorators a nested def applies where its definition executes.

    Python applies every decorator to the new function object when the def
    executes. Function declaration lowers some imported or builtin ones itself
    (lowered_at_declaration); a run of those at the bottom of the stack stays
    on the hoisted def and the decorators above it apply. A stack with an
    ordinary decorator below one of those applies whole, so a decorator with
    no runtime implementation raises when the def runs, not at compile time.
    C-ABI exports always stay.
    """
    def declared(dec):
        name = _hoist_decorator_name(dec)
        if name is None:
            return False
        root = name.split(".", 1)[0]
        if name_in(scope_names, root) or (
            name_in(module_names, root) and not name_in(import_names, root)
        ):
            return False
        return lowered_at_declaration(name)

    decorators = tuple(getattr(fd, "decorators", ()))
    index = 0
    while index < len(decorators) and not declared(decorators[index]):
        index += 1
    kept = decorators[index:]
    if any(_hoist_decorator_name(dec) in C_ABI_EXPORT_DECORATORS for dec in kept):
        return decorators[:index]
    if all(declared(dec) for dec in kept):
        return decorators[:index]
    return decorators


def _hoist_decorated_definition(fd, hoisted_name, decorators, scope_names):
    """Assignments for rewritten decorator values, and the decorated value.

    Python evaluates decorator expressions top to bottom before the defaults
    and function object, then applies them bottom to top.
    """
    statements = []
    names = []
    for decorator in decorators:
        name = "__pcc_decorator_" + hoisted_name + "_" + str(len(names))
        while name_in(scope_names, name):
            name = name + "_"
        statements.append(_Assign(
            span=fd.span,
            targets=(_Name(span=fd.span, ty=_DYN, ident=name),),
            value=decorator,
            annotation=None,
        ))
        append_name_once(scope_names, name)
        names.append(name)
    value = _Name(span=fd.span, ty=_DYN, ident=hoisted_name)
    index = len(names)
    while index > 0:
        index -= 1
        value = _Call(
            span=fd.span,
            ty=_DYN,
            func=_Name(span=fd.span, ty=_DYN, ident=names[index]),
            args=(value,),
        )
    return tuple(statements), value


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


def _hoist_has_source_defaults(fd) -> bool:
    for arg in fd.args:
        if arg.name and arg.has_default:
            return True
    return False


def _hoist_body_shadow_names(fd):
    """Bindings that stop enclosing-name rewrites in this function body.

    Defaults and decorators still belong to the enclosing scope. Nonlocals
    keep its cells; explicit globals resolve the module binding instead.
    """
    names = list(function_local_bindings(fd))
    extend_names_once(names, scope_declared_names(fd.body, True, False))
    return tuple(names)


def _hoist_reads_name_at_statement(node, target):
    """A read that executes here, excluding deferred function/lambda bodies."""
    if node is None or isinstance(node, (str, int, float, bool, bytes)):
        return False
    if isinstance(node, (tuple, list)):
        return any(_hoist_reads_name_at_statement(item, target) for item in node)
    if isinstance(node, _Name):
        return node.ident == target
    if isinstance(node, (_FuncDef, _Lambda)):
        if isinstance(node, _FuncDef):
            if _hoist_reads_name_at_statement(node.decorators, target):
                return True
            args = node.args
        else:
            args = node.params
        return any(_hoist_reads_name_at_statement(arg.default, target) for arg in args)
    for field in _dataclass_field_names(node):
        if field not in ("span", "ty", "annotation", "return_ty"):
            if _hoist_reads_name_at_statement(_dataclass_field_value(node, field, None), target):
                return True
    return False


def _hoist_source_binding_is_dynamic(stmts, fd):
    """Only one unconditional, preceding def may use the internal direct ABI.

    Binding/deletion targets use the shared lexical inventory. A definition
    inside a statement block, another write, or a prior read needs the source
    local to exist independently of the hoisted implementation symbol.
    """
    seen_definition = False
    for stmt in stmts:
        if isinstance(stmt, _FuncDef) and stmt.name == fd.name:
            if seen_definition:
                return True
            if _hoist_reads_name_at_statement(stmt, fd.name):
                return True
            seen_definition = True
            continue
        if name_in(collect_scope_bindings((stmt,)), fd.name):
            return True
        if not seen_definition and _hoist_reads_name_at_statement(stmt, fd.name):
            return True
    return not seen_definition


def _hoist_class_binding_is_static(stmts, class_name):
    """Whether ``class_name`` is bound only by one top-level class statement.

    Then a later class statement in the same body naming it as a base gets
    exactly that class definition's objects, so its layout is known when
    compiling. Any other binding makes the name a run-time value.
    """
    seen = False
    for stmt in stmts:
        if isinstance(stmt, _ClassDef) and stmt.name == class_name:
            if seen:
                return False
            seen = True
            continue
        if name_in(collect_scope_bindings((stmt,)), class_name):
            return False
    return seen


def _hoist_class_cell_statement(span, class_cell):
    """``<class_cell> = [<unbound>]``: one ``__class__`` cell per execution."""
    return _Assign(
        span=span,
        targets=(_Name(span=span, ty=_DYN, ident=class_cell),),
        value=_ListExpr(
            span=span,
            ty=ListType(name="list", elem=_DYN),
            elems=(_Call(
                span=span, ty=_DYN,
                func=_Name(span=span, ty=_DYN, ident=CELL_UNBOUND),
                args=(),
            ),),
        ),
        annotation=None,
    )


def _hoist_runtime_base_flags(bases, scope_names, static_classes, body_key):
    """Per base: True when it is a value the class statement reads as it runs.

    A local, a parameter or a cell is such a value (CPython evaluates bases
    when the statement runs); a class statement bound once at the top of the
    same body, a module class or a builtin keeps a compile-time layout.
    """
    flags = []
    for base in bases:
        runtime = False
        if isinstance(base, _Name):
            if (name_in(scope_names, base.ident)
                    and (body_key, base.ident) not in static_classes):
                runtime = True
        elif (isinstance(base, _Call) and isinstance(base.func, _Name)
                and base.func.ident == CELL_READ):
            runtime = True
        flags.append(runtime)
    return tuple(flags)


def _hoist_add_capture_params(method, cap_names):
    """Append a closure method's capture parameters after its own.

    Only the function object's adapter supplies them, by name, so the source
    ABI indices stay unchanged for calls through the method table.
    """
    cap_args = tuple(
        Arg(name=name, annotation=_DYN, default=None, kind="pos", has_default=True)
        for name in cap_names
    )
    return _replace(method, args=method.args + cap_args)


def _hoist_class_cell_name(cd, hoist_name, scope_names):
    """A fresh enclosing local for the class's ``__class__`` cell, or ""."""
    needed = False
    for member in cd.body:
        if isinstance(member, _FuncDef) and _hoist_uses_class_cell(member.body):
            needed = True
            break
    if not needed:
        return ""
    name = "__pcc_class_cell_" + hoist_name
    while name_in(scope_names, name):
        name = name + "_"
    return name


def _hoist_method_name_map(rename_map, method, class_body):
    """Enclosing renames still visible inside one method body."""
    method_map = copy_name_map(rename_map)
    for name in _hoist_body_shadow_names(method):
        method_map.pop(name, None)
    for name in scope_declared_names(class_body, True, False):
        method_map.pop(name, None)
    return method_map


def _hoist_method_captures(free, forwarded, scope_names, local_bound, class_cell):
    """A closure method's capture parameters and their enclosing sources.

    Enclosing names it reads or that its nested defs reach through it, then
    the class's ``__class__`` cell when it uses ``super`` or ``__class__``.
    """
    captures = []
    for name in free:
        if (name_in(scope_names, name) and name != "__class__"
                and not is_discard_capture_name(name)):
            append_name_once(captures, name)
    for name in forwarded:
        if (name_in(scope_names, name) and name != "__class__"
                and not name_in(local_bound, name)
                and not is_discard_capture_name(name)):
            append_name_once(captures, name)
    names = list(sorted(captures))
    sources = list(names)
    if class_cell:
        names.append("__class__")
        sources.append(class_cell)
    return names, sources


def _hoist_definition_needs_binding(stmts, fd) -> bool:
    """Whether source calls require the callable's public Python signature.

    Hidden capture parameters belong only to proven plain positional direct
    calls. Expanded/keyword/invalid-arity source calls use a real callable.
    """
    if (_hoist_has_source_defaults(fd)
            or _hoist_source_binding_is_dynamic(stmts, fd)
            or body_uses_name_as_value(stmts, fd.name)):
        return True
    positional_count = 0
    plain_parameters = True
    for arg in fd.args:
        if not arg.name:
            continue
        if arg.kind not in ("pos", "pos_only"):
            plain_parameters = False
        positional_count += 1
    pending = list(stmts)
    while pending:
        node = pending.pop()
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            continue
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        if isinstance(node, _FuncDef):
            # Defaults/decorators execute in the enclosing scope. Locals and
            # globals in a descendant body can instead shadow this binding.
            if body_uses_name_as_value(node.decorators, fd.name):
                return True
            pending.extend(node.decorators)
            for arg in node.args:
                if body_uses_name_as_value((arg.default,), fd.name):
                    return True
                pending.append(arg.default)
            if name_in(_hoist_body_shadow_names(node), fd.name):
                continue
            if body_uses_name_as_value(node.body, fd.name):
                return True
            pending.extend(node.body)
            continue
        if isinstance(node, _Call) and isinstance(node.func, _Name) and node.func.ident == fd.name:
            if node.kwargs or not plain_parameters or len(node.args) != positional_count:
                return True
            for arg in node.args:
                if (
                    isinstance(arg, _Call)
                    and isinstance(arg.func, _Name)
                    and arg.func.ident in ("*", "__starred__", "**")
                ):
                    return True
        for field in _dataclass_field_names(node):
            if field in ("span", "ty", "annotation", "return_ty"):
                continue
            pending.append(_dataclass_field_value(node, field, None))
    return False


def _hoist_method_self_type(fd):
    """The inferred class type of a method's ``self``, or None."""
    if not fd.args:
        return None
    first = fd.args[0]
    if first.name != "self":
        return None
    annotation = first.annotation
    if annotation is None or isinstance(annotation, DynType):
        return None
    return annotation


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
        """Hoist implementation bodies while preserving executable bindings.

        Proven ordinary positional calls use the internal capture ABI.
        Defaults, function values, conditional definitions and dynamic
        bindings create a callable when the source definition executes.
        Captured mutable names share lexical cells. Return implementation
        nodes for the caller's normal declaration/emission pass.
        """
        debug_hoist = bool(os.environ.get("PCC_DEBUG_HOIST"))

        ast_module = self.ast_module
        mod_name = ast_module.name or "<module>"

        hoisted: list = []
        hoisted_capture_params = self._hoisted_capture_params
        hoisted_capture_params.clear()
        hoisted_class_capture_params = self._hoisted_class_capture_params
        hoisted_class_capture_params.clear()
        hoisted_method_capture_params = self._hoisted_method_capture_params
        hoisted_method_capture_params.clear()
        local_class_cells = self._local_class_cells
        local_class_cells.clear()
        local_class_name_cells = self._local_class_name_cells
        local_class_name_cells.clear()
        local_class_runtime_bases = self._local_class_runtime_bases
        local_class_runtime_bases.clear()
        # (id of a function body, hoisted class) for class statements that
        # bind their name exactly once at the top of that body.
        static_local_classes = set()
        hoisted_enclosing_class = self._hoisted_enclosing_class
        hoisted_enclosing_class.clear()
        hoisted_enclosing_method_kind = self._hoisted_enclosing_method_kind
        hoisted_enclosing_method_kind.clear()
        closure_boxed_params = self._closure_boxed_params
        closure_boxed_params.clear()
        hoisted_qualnames = self._hoisted_qualnames
        hoisted_qualnames.clear()
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
        flattened = _lift_nested_classes(ast_module, hoisted_qualnames)
        if flattened is not ast_module:
            ast_module = flattened
            setattr(self, "ast_module", ast_module)
            setattr(self, "_ast_body", ast_module.body)
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
        # Cell-binding defs by identity; no id() key can alias another def.
        boxed_function_defs = []
        # The inferred type of the enclosing method's ``self`` while one
        # method's body is rewritten.  A mixin method is typed with its
        # composed host class; a hoisted closure that captures ``self``
        # must keep that type, or ``self.attr`` lowers against the mixin's
        # own field layout and reads the wrong slot.
        enclosing_self_type = [None]

        def function_binds_cell(fd):
            for boxed_def in boxed_function_defs:
                if boxed_def is fd:
                    return True
            return False

        module_scope_names_base, module_import_names = _hoist_module_scope_names(ast_module.body)

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

        def collect_first_class_closure_captures(body, outer_scope_names, stable_params):
            boxed = []

            def walk_block(stmts, lexical_names, call_scope_body):
                for stmt in stmts:
                    if isinstance(stmt, _ClassDef):
                        # A local class can outlive this activation. Its
                        # methods retain the same lexical cells as sibling
                        # functions, even when no sibling forces boxing.
                        for member in stmt.body:
                            if isinstance(member, _FuncDef):
                                extend_names_once(boxed, analyze_names(
                                    member, (), outer_scope_names=lexical_names))
                        continue
                    if isinstance(stmt, _FuncDef):
                        captures = analyze_names(stmt, (), outer_scope_names=lexical_names)
                        first_class = _hoist_definition_needs_binding(call_scope_body, stmt)
                        # Direct calls must pass an empty cell without reading
                        # its payload: the child may catch NameError itself or
                        # never execute the free-variable read. Only entry-
                        # bound parameters with no possible unbinding have a
                        # sufficient proof to retain direct by-value capture.
                        extend_names_once(boxed, tuple(
                            name for name in captures
                            if first_class or not name_in(stable_params, name)
                        ))
                        child_names = _hoist_lexical_scope_names(
                            stmt, stmt.body, lexical_names,
                        )
                        walk_block(stmt.body, tuple(child_names), stmt.body)
                        continue
                    if isinstance(stmt, _If):
                        walk_block(stmt.body, lexical_names, call_scope_body)
                        walk_block(stmt.else_body, lexical_names, call_scope_body)
                        continue
                    if isinstance(stmt, _While):
                        walk_block(stmt.body, lexical_names, call_scope_body)
                        walk_block(stmt.else_body, lexical_names, call_scope_body)
                        continue
                    if isinstance(stmt, _For):
                        walk_block(stmt.body, lexical_names, call_scope_body)
                        walk_block(stmt.else_body, lexical_names, call_scope_body)
                        continue
                    if isinstance(stmt, _Try):
                        walk_block(stmt.body, lexical_names, call_scope_body)
                        for handler in stmt.handlers:
                            walk_block(
                                _dataclass_field_value(handler, "body", ()),
                                lexical_names,
                                call_scope_body,
                            )
                        walk_block(stmt.else_body, lexical_names, call_scope_body)
                        walk_block(stmt.finally_body, lexical_names, call_scope_body)
                        continue
                    if isinstance(stmt, _With):
                        walk_block(stmt.body, lexical_names, call_scope_body)

            walk_block(body, outer_scope_names, body)
            return tuple(boxed)

        def boxed_capture_names(body, outer_scope_names, param_names):
            boxed = []
            extend_names_once(boxed, collect_all_mutable_captures(body))
            extend_names_once(
                boxed,
                collect_first_class_closure_captures(
                    body, outer_scope_names, stable_capture_parameters(body, param_names)),
            )
            extend_names_once(boxed, late_bound_lambda_captures(body))
            return tuple(boxed)

        def body_reads_free_names(fd, excluded):
            return bool(analyze_names(fd, excluded))


        def rewrite_body(
            stmts,
            rename_map,
            scope_names,
            enclosing_class_name=None,
            enclosing_method_kind=None,
            qual_prefix="",
            call_scope_body=None,
        ):
            """Return a new body tuple with nested defs stripped out
            and inner-name Call sites rewritten through rename_map."""
            if call_scope_body is None:
                call_scope_body = stmts
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
                        if isinstance(mapped, str) and (
                            mapped.startswith("__pcc_closure_value_") or mapped == key
                        ):
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
                result = function_local_bindings(fd)
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
                """Carry descendant captures through every lexical ancestor.

                A direct child call needs its captures just as an escaped
                callable does. Control flow does not introduce a new scope;
                explicit global declarations still block lexical forwarding.
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
                child_scope = _hoist_lexical_scope_names(
                    fd, fd.body, outer_scope_names,
                )
                for inner_fd in _hoist_nested_scope_funcdefs(fd.body):
                    inner_local_bound = locally_bound_names(inner_fd)
                    inner_free = analyze_names(
                        inner_fd,
                        excluded_names,
                        outer_scope_names=child_scope,
                    )
                    inner_forwarded = forwarded_value_capture_names(
                        inner_fd,
                        excluded_names,
                        child_scope,
                        inner_local_bound,
                    )
                    inner_needed = []
                    extend_names_once(inner_needed, inner_free)
                    for fv in inner_forwarded:
                        if not name_in(inner_local_bound, fv):
                            append_name_once(inner_needed, fv)
                    for fv in inner_needed:
                        if (name_in(child_scope, fv)
                                and name_in(outer_scope_names, fv)
                                and not name_in(outer_local_bound, fv)):
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
                    # The assignment executes even when the lambda has no
                    # captures. Share the def path so its defaults and object
                    # identity are established once at this source position.
                    st = fd_stmt
                    synthesized_lambda_func = True
                if not synthesized_lambda_func and isinstance(st, _ClassDef):
                    # A function-local class statement stays executable: each
                    # time it runs it builds a fresh class from its bases,
                    # namespace and decorators.  The module-level copy below
                    # only declares the method bodies.  Each method is a
                    # closure converted like a nested def: the enclosing
                    # cells it reads, and the class's ``__class__`` cell when
                    # it uses ``super`` or ``__class__``, become trailing
                    # capture parameters that the function objects published
                    # by the class statement carry.  One ``__class__`` cell
                    # per execution is shared by that execution's methods;
                    # class lowering fills it once the body has run.
                    existing = existing_top_or_hoisted_names(True)
                    hoist_name = st.name
                    suffix = 0
                    while name_in(existing, hoist_name):
                        suffix += 1
                        hoist_name = f"{st.name}_nest{suffix}"
                    new_class_body = []
                    class_body_map = copy_name_map(rename_map)
                    for class_name in collect_scope_bindings(st.body):
                        class_body_map.pop(class_name, None)
                    for class_name in scope_declared_names(st.body, True, False):
                        class_body_map.pop(class_name, None)
                    class_cell = _hoist_class_cell_name(st, hoist_name, scope_names)
                    if class_cell:
                        append_name_once(scope_names, class_cell)
                        new_stmts.append(_hoist_class_cell_statement(st.span, class_cell))
                        local_class_cells[hoist_name] = class_cell
                    for body_stmt in st.body:
                        if not isinstance(body_stmt, _FuncDef):
                            new_class_body.append(rewrite_stmt(
                                body_stmt, class_body_map, scope_names,
                                enclosing_class_name, enclosing_method_kind,
                                qual_prefix, st.body,
                            ))
                            continue
                        method = body_stmt
                        method_map = _hoist_method_name_map(rename_map, method, st.body)
                        uses_class_cell = bool(class_cell) and _hoist_uses_class_cell(method.body)
                        method_body = method.body
                        if uses_class_cell:
                            method_body = _hoist_class_cell_reads(method_body)
                        params = tuple(a.name for a in method.args if a.name != "")
                        method_body = box_outer_body(
                            method_body, hoist_name + "." + method.name, params,
                            function_boxed_names(method, boxed_capture_names(
                                method.body, function_local_bindings(method), params)),
                            closure_boxed_params, boxed_function_defs,
                        )
                        method_scope = _hoist_lexical_scope_names(
                            method, method_body,
                            tuple(scope_names) + (("__class__",) if uses_class_cell else ()),
                        )
                        # Defs nested in the method reach enclosing cells
                        # through it, so the method captures them too.
                        boxed_method = clone_funcdef(
                            method, method.name, method.args, method.return_ty, method_body,
                        )
                        method_local_bound = locally_bound_names(boxed_method)
                        forwarded = forwarded_value_capture_names(
                            boxed_method, excluded_prescan, scope_names, method_local_bound,
                        )
                        saved_self_type = enclosing_self_type[0]
                        enclosing_self_type[0] = _hoist_method_self_type(method)
                        method_body = rewrite_body(
                            method_body,
                            method_map,
                            method_scope,
                            hoist_name,
                            _hoist_method_kind(method),
                            qual_prefix + st.name + "." + method.name + ".<locals>.",
                            method.body,
                        )
                        enclosing_self_type[0] = saved_self_type
                        args = tuple(
                            _replace(arg, default=rewrite_expr(arg.default, class_body_map))
                            for arg in method.args
                        )
                        method = _replace(
                            clone_funcdef(method, method.name, args, method.return_ty, method_body),
                            decorators=rewrite_expr_tuple(method.decorators, class_body_map),
                        )
                        cap_names, cap_sources = _hoist_method_captures(
                            analyze_names(method, excluded_prescan, outer_scope_names=scope_names),
                            forwarded, scope_names, method_local_bound,
                            class_cell if uses_class_cell else "",
                        )
                        if cap_names:
                            method = _hoist_add_capture_params(method, cap_names)
                            hoisted_method_capture_params[
                                local_method_capture_key(hoist_name, method)
                            ] = (tuple(cap_names), tuple(cap_sources))
                        new_class_body.append(method)
                    hoisted_bases = rewrite_expr_tuple(st.bases, rename_map)
                    runtime_flags = _hoist_runtime_base_flags(
                        hoisted_bases, scope_names, static_local_classes, id(call_scope_body),
                    )
                    if any(runtime_flags):
                        local_class_runtime_bases[hoist_name] = runtime_flags
                    hoisted_cd = _replace(
                        st,
                        name=hoist_name,
                        body=tuple(new_class_body),
                        bases=hoisted_bases,
                        keywords=rewrite_kwargs(st.keywords, rename_map),
                        decorators=rewrite_expr_tuple(st.decorators, rename_map),
                    )
                    # Keep the rewritten ClassDef in the enclosing body as
                    # an executable statement: Python constructs a fresh
                    # class object (bases, namespace, decorators) every time
                    # execution reaches it.
                    new_stmts.append(hoisted_cd)
                    hoisted.append(hoisted_cd)
                    # Presence in this table marks a synthetic local class,
                    # even one whose methods capture nothing.
                    hoisted_class_capture_params[hoist_name] = ()
                    # CPython's __qualname__ (``outer.<locals>.Name``); the
                    # hoisted symbol may carry a ``_nestN`` suffix.
                    hoisted_qualnames[hoist_name] = qual_prefix + st.name
                    if function_binds_cell(st):
                        # Its methods read the class's own name, so the
                        # name is a cell: the statement stores into it.
                        local_class_name_cells[hoist_name] = st.name
                    else:
                        if hoist_name != st.name:
                            rename_map[st.name] = hoist_name
                        if _hoist_class_binding_is_static(call_scope_body, st.name):
                            static_local_classes.add((id(call_scope_body), hoist_name))
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
                    executable_decorators = _hoist_executable_decorators(
                        st, scope_names, module_scope_names_base, module_import_names,
                    )
                    needs_binding = (
                        binds_cell
                        or bool(executable_decorators)
                        or _hoist_definition_needs_binding(call_scope_body, st)
                    )
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
                    if (name_in(scope_names, "__class__")
                            and not name_in(free_names, "__class__")
                            and _hoist_uses_class_cell(st.body)):
                        # A def inside a local-class method inherits the
                        # method's ``__class__`` cell for super().
                        free_names = tuple(sorted(free_names + ("__class__",)))
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
                        function_boxed_names(
                            st, boxed_capture_names(st.body, function_local_bindings(st),
                                                    tuple(a.name for a in st.args if a.name))
                        ),
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
                            cap_annotation = _DYN
                            if fv == "self" and enclosing_self_type[0] is not None:
                                cap_annotation = enclosing_self_type[0]
                            cap_args_list.append(
                                Arg(
                                    name=fv,
                                    annotation=cap_annotation,
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
                        # Defaults execute in the enclosing scope before the
                        # function's parameters or its new binding exist.
                        orig_args = tuple(
                            _replace(arg, default=rewrite_expr(arg.default, rename_map))
                            for arg in st.args
                        )
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
                        shadow_names = _hoist_body_shadow_names(st)
                        for name in shadow_names:
                            inner_map.pop(name, None)
                        if not binds_cell and not name_in(shadow_names, st.name):
                            inner_map[st.name] = (final_name, free_names)
                        inner_scope = _hoist_lexical_scope_names(
                            st, inner_source_body, scope_names,
                        )
                        inner_body = rewrite_body(
                            inner_source_body,
                            inner_map,
                            inner_scope,
                            enclosing_class_name,
                            enclosing_method_kind,
                            qual_prefix + st.name + ".<locals>.",
                            st.body,
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
                    # CPython's __qualname__ for the nested def, e.g.
                    # ``outer.<locals>.inner``; the hoisted symbol name is
                    # internal (``__nested_inner``).
                    hoisted_qualnames[final_name] = qual_prefix + (
                        "<lambda>" if synthesized_lambda_func else st.name
                    )
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
                    definition_value = _Name(span=st.span, ty=_DYN, ident=final_name)
                    if executable_decorators:
                        hoisted_fd = _replace(
                            hoisted_fd,
                            decorators=st.decorators[len(executable_decorators):],
                        )
                        rewritten_decorators = []
                        for decorator in executable_decorators:
                            rewritten_decorators.append(rewrite_expr(decorator, rename_map))
                        decorator_statements, definition_value = _hoist_decorated_definition(
                            st, final_name, rewritten_decorators, scope_names,
                        )
                        new_stmts.extend(decorator_statements)
                    hoisted.append(hoisted_fd)
                    if binds_cell:
                        new_stmts.append(_Assign(
                            span=st.span,
                            targets=(_Subscript(
                                span=st.span, ty=_DYN,
                                obj=_Name(span=st.span, ty=_DYN, ident=st.name),
                                idx=IntLit(span=st.span, ty=IntType(name="int"), value=0),
                            ),),
                            value=definition_value,
                            annotation=None,
                        ))
                    elif needs_binding:
                        # Materialize once when this def executes. Re-reading
                        # the hoisted symbol made distinct function objects,
                        # dropping attributes assigned before a later return.
                        if _hoist_source_binding_is_dynamic(call_scope_body, st):
                            # Reads before a def and all later writes/deletes
                            # must share the same source local and bound bit.
                            binding = st.name
                        else:
                            binding = "__pcc_closure_value_" + final_name
                            while name_in(scope_names, binding):
                                binding = binding + "_"
                        new_stmts.append(_Assign(
                            span=st.span,
                            targets=(_Name(span=st.span, ty=_DYN, ident=binding),),
                            value=definition_value,
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
                        qual_prefix,
                        call_scope_body,
                    )
                )
            return tuple(new_stmts)

        def rewrite_stmt(
            stmt,
            rename_map,
            scope_names,
            enclosing_class_name,
            enclosing_method_kind,
            qual_prefix="",
            call_scope_body=None,
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
                        qual_prefix,
                        call_scope_body,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                        qual_prefix,
                        call_scope_body,
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
                        qual_prefix,
                        call_scope_body,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                        qual_prefix,
                        call_scope_body,
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
                        qual_prefix,
                        call_scope_body,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                        qual_prefix,
                        call_scope_body,
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
                                qual_prefix,
                                call_scope_body,
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
                        qual_prefix,
                        call_scope_body,
                    ),
                    else_body=rewrite_body(
                        stmt.else_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                        qual_prefix,
                        call_scope_body,
                    ),
                    finally_body=rewrite_body(
                        stmt.finally_body,
                        rename_map,
                        scope_names,
                        enclosing_class_name,
                        enclosing_method_kind,
                        qual_prefix,
                        call_scope_body,
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
                        qual_prefix,
                        call_scope_body,
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
            from pcc.frontends.python.py_ast import Delete as _Delete

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
                capture_order = list(expr.operand_order)
                for index in range(len(extra_kwargs)):
                    capture_order.append(("capture", len(expr.kwargs) + index))
                return _replace(
                    expr,
                    func=new_func,
                    args=rewrite_expr_tuple(expr.args, rename_map),
                    kwargs=rewrite_kwargs(expr.kwargs, rename_map) + extra_kwargs,
                    operand_order=tuple(capture_order),
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
                lambda_fd = FuncDef(
                    span=expr.span, name="", args=expr.params, return_ty=_DYN,
                    body=(Return(span=expr.span, value=expr.body),),
                    is_async=False, decorators=(),
                )
                for name in function_local_bindings(lambda_fd):
                    inner_map.pop(name, None)
                params = tuple(_replace(arg, default=rewrite_expr(arg.default, rename_map))
                               for arg in expr.params)
                return _replace(expr, params=params, body=rewrite_expr(expr.body, inner_map))
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
                        function_boxed_names(
                            stmt, boxed_capture_names(stmt.body, function_local_bindings(stmt),
                                                      tuple(a.name for a in stmt.args if a.name))
                        ),
                        closure_boxed_params,
                        boxed_function_defs,
                    )
                    scope_names = _hoist_lexical_scope_names(stmt, boxed_body, ())
                    new_body = rewrite_body(
                        boxed_body,
                        {},
                        scope_names,
                        None,
                        None,
                        stmt.name + ".<locals>.",
                        stmt.body,
                    )
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
                                function_boxed_names(
                                    m, boxed_capture_names(m.body, function_local_bindings(m),
                                                          tuple(a.name for a in m.args if a.name))
                                ),
                                closure_boxed_params,
                                boxed_function_defs,
                            )
                            scope_names = _hoist_lexical_scope_names(m, boxed_body, ())
                            enclosing_self_type[0] = _hoist_method_self_type(m)
                            new_body = rewrite_body(
                                boxed_body,
                                {},
                                scope_names,
                                stmt.name,
                                _hoist_method_kind(m),
                                stmt.name + "." + m.name + ".<locals>.",
                                m.body,
                            )
                            enclosing_self_type[0] = None
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


def _lift_nested_classes(module, qualnames):
    """Declare classes nested in module-level class bodies at module level.

    ``class Inner`` inside ``class Outer`` runs within the outer body and
    becomes an attribute of ``Outer``. Class lowering declares only module
    classes and skipped the nested statement, so ``Outer.Inner`` raised
    AttributeError and a method call on ``Outer.Inner()`` crashed codegen.
    Each nested class now precedes its outer class at module level under its
    own name (suffixed when that name is already bound), records
    ``Outer.Inner`` as its qualified name, and the outer body binds the
    attribute to it. Unlike CPython, statements of the outer body that come
    before the nested class run after the nested class body.
    """
    taken = set()
    _module_bound_names(module.body, taken)
    body = []
    changed = False
    for stmt in module.body:
        if isinstance(stmt, ClassDef):
            lifted, stmt = _lift_class_members(stmt, stmt.name, taken, qualnames)
            if lifted:
                body.extend(lifted)
                changed = True
        body.append(stmt)
    if not changed:
        return module
    return _replace(module, body=tuple(body))


def _module_bound_names(body, taken):
    """Collect module-scope names bound by ``body``, including in blocks."""
    for stmt in body:
        if isinstance(stmt, (ClassDef, FuncDef)):
            taken.add(stmt.name)
        elif isinstance(stmt, Assign):
            for target in stmt.targets:
                _target_bound_names(target, taken)
        elif isinstance(stmt, AugAssign):
            _target_bound_names(stmt.target, taken)
        elif isinstance(stmt, (Import, ImportFrom)):
            for _name, bound in _import_names_from_stmt(stmt):
                if bound:
                    taken.add(bound.split(".")[0])
        elif isinstance(stmt, For):
            _target_bound_names(stmt.target, taken)
            _module_bound_names(stmt.body, taken)
            _module_bound_names(stmt.else_body, taken)
        elif isinstance(stmt, (If, While)):
            _module_bound_names(stmt.body, taken)
            _module_bound_names(stmt.else_body, taken)
        elif isinstance(stmt, With):
            for _ctx, as_var in stmt.items:
                if as_var is not None:
                    _target_bound_names(as_var, taken)
            _module_bound_names(stmt.body, taken)
        elif isinstance(stmt, Try):
            _module_bound_names(stmt.body, taken)
            for handler in stmt.handlers:
                handler_name = _dataclass_field_value(handler, "name", None)
                if handler_name:
                    taken.add(handler_name)
                _module_bound_names(_dataclass_field_value(handler, "body", ()), taken)
            _module_bound_names(stmt.else_body, taken)
            _module_bound_names(stmt.finally_body, taken)


def _target_bound_names(target, taken):
    if isinstance(target, Name):
        taken.add(target.ident)
    elif isinstance(target, (TupleExpr, ListExpr)):
        for element in target.elems:
            _target_bound_names(element, taken)
    elif (isinstance(target, Call) and isinstance(target.func, Name)
          and target.func.ident == "__starred__" and target.args):
        _target_bound_names(target.args[0], taken)


def _lift_class_members(cd, qualname, taken, qualnames):
    lifted = []
    members = []
    for item in cd.body:
        if not isinstance(item, ClassDef):
            members.append(item)
            continue
        inner_qualname = qualname + "." + item.name
        inner_lifted, inner = _lift_class_members(item, inner_qualname, taken, qualnames)
        name = item.name
        suffix = 0
        while name in taken:
            suffix += 1
            name = item.name + "_nest" + str(suffix)
        taken.add(name)
        qualnames[name] = inner_qualname
        lifted.extend(inner_lifted)
        lifted.append(_replace(inner, name=name))
        members.append(Assign(
            span=item.span,
            targets=(Name(span=item.span, ty=_DYN, ident=item.name),),
            value=Name(span=item.span, ty=_DYN, ident=name),
        ))
    if not lifted:
        return [], cd
    return lifted, _replace(cd, body=tuple(members))


def hoist_nested_funcdefs(codegen):
    """Run nested-function hoisting without widening Layer1's MRO."""
    return _HoistLoweringPass._hoist_nested_funcdefs(codegen)
