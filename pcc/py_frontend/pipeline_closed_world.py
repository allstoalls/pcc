"""Closed-world import, re-export, and shallow-lift helpers.

These helpers construct deterministic metadata inputs for the compilation
driver without owning scheduling, backend selection, or artifact publication.
"""
from __future__ import annotations

import json

from . import pipeline_ast_wire as _pipeline_ast_wire
from . import pipeline_exports as _pipeline_exports
from . import pipeline_paths as _pipeline_paths
from .export_meta import encode_type
from .pipeline_modes import PyPipelineError


_package_parts_for_module = _pipeline_paths.package_parts_for_module
_join_dotted_parts = _pipeline_paths.join_dotted_parts
_py_ast_field_names = _pipeline_ast_wire._py_ast_field_names
_py_ast_field_value = _pipeline_ast_wire._py_ast_field_value
_closed_world_is_node = _pipeline_exports._closed_world_is_node
_native_export_to_wire = _pipeline_exports._native_export_to_wire
_native_export_from_wire = _pipeline_exports._native_export_from_wire


def _resolve_ast_import_from_module(src_path: str, mod_name: str, stmt) -> str:
    """Resolve an AST ImportFrom's source module for closed-world exports."""
    level = _py_ast_field_value(stmt, "level", 0) or 0
    module = _py_ast_field_value(stmt, "module", "") or ""
    if level <= 0:
        return module
    cur_pkg = _package_parts_for_module(src_path, mod_name)
    up = level - 1
    if up > len(cur_pkg):
        return module
    base = cur_pkg[: len(cur_pkg) - up]
    if module:
        return _join_dotted_parts(base + module.split("."))
    return _join_dotted_parts(base)


def _closed_world_star_export_items(src_exports):
    all_info = src_exports.get("__all__")
    all_names = None
    if isinstance(all_info, dict):
        all_names = all_info.get("export_names")
    if all_names is not None:
        items = []
        for export_name in all_names:
            info = src_exports.get(export_name)
            if info is not None:
                items.append((export_name, info))
        return items
    items = []
    for export_name, info in src_exports.items():
        if export_name.startswith("_"):
            continue
        items.append((export_name, info))
    return items


def _closed_world_module_block_assign_targets(stmt):
    from .py_ast import Assign as _Assign
    from .py_ast import ClassDef as _ClassDef
    from .py_ast import For as _For
    from .py_ast import FuncDef as _FuncDef
    from .py_ast import If as _If
    from .py_ast import ListExpr as _ListExpr
    from .py_ast import Name as _Name
    from .py_ast import Try as _Try
    from .py_ast import TupleExpr as _TupleExpr
    from .py_ast import While as _While
    from .py_ast import With as _With

    names = []
    pending = [stmt]
    while pending:
        s = pending.pop()
        if _closed_world_is_node(s, (_FuncDef, _ClassDef)):
            continue
        if _closed_world_is_node(s, _Assign):
            pending_targets = list(_py_ast_field_value(s, "targets", ()))
            while pending_targets:
                target = pending_targets.pop()
                if _closed_world_is_node(target, (_TupleExpr, _ListExpr)):
                    pending_targets.extend(
                        reversed(_py_ast_field_value(target, "elems", ()))
                    )
                    continue
                if _closed_world_is_node(target, _Name):
                    target_name = _py_ast_field_value(target, "ident", "")
                    if target_name:
                        names.append(target_name)
            continue
        if _closed_world_is_node(s, (_If, _While, _For)):
            for child in _py_ast_field_value(s, "else_body", ()):
                pending.append(child)
            for child in _py_ast_field_value(s, "body", ()):
                pending.append(child)
            continue
        if _closed_world_is_node(s, _Try):
            for child in _py_ast_field_value(s, "finally_body", ()):
                pending.append(child)
            for child in _py_ast_field_value(s, "else_body", ()):
                pending.append(child)
            for handler in _py_ast_field_value(s, "handlers", ()):
                for child in _py_ast_field_value(handler, "body", ()):
                    pending.append(child)
            for child in _py_ast_field_value(s, "body", ()):
                pending.append(child)
            continue
        if _closed_world_is_node(s, _With):
            for child in _py_ast_field_value(s, "body", ()):
                pending.append(child)
    return tuple(names)


def _closed_world_dyn_module_global_export(
    mod_name: str,
    target_name: str,
    *,
    box_int_abi: bool | None = None,
) -> dict:
    from .py_ast import DynType as _DynType

    result = {
        "kind": "module_global",
        "owning_module": mod_name,
        "export_name": target_name,
        "value_ty": encode_type(_DynType("dyn")),
    }
    if box_int_abi is not None:
        result["box_int_abi"] = bool(box_int_abi)
    return result


def _identity_list_contains(values, target) -> bool:
    for value in values:
        if value is target:
            return True
    return False


def _flatten_closed_world_class_export_fields(native_exports) -> None:
    """Publish the runtime's inherited-first instance-field order."""

    resolved = []
    visiting = []

    def resolve(info) -> None:
        if _identity_list_contains(resolved, info):
            return
        if _identity_list_contains(visiting, info):
            return
        visiting.append(info)

        names = []
        type_names = []
        type_values = []

        def append_fields(source_info) -> None:
            for field_name in source_info.get("field_names", ()):
                if field_name not in names:
                    names.append(field_name)
            for field_entry in source_info.get("field_types", ()):
                if not isinstance(field_entry, tuple) or len(field_entry) != 2:
                    continue
                field_name = field_entry[0]
                field_type = field_entry[1]
                replaced = False
                index = 0
                while index < len(type_names):
                    if type_names[index] == field_name:
                        type_values[index] = field_type
                        replaced = True
                        break
                    index += 1
                if not replaced:
                    type_names.append(field_name)
                    type_values.append(field_type)

        owning_module = info.get("owning_module", "")
        visible_exports = native_exports.get(owning_module, {})
        for base_name in info.get("base_names", ()):
            base_info = visible_exports.get(base_name)
            if not isinstance(base_info, dict) or base_info.get("kind") != "class":
                continue
            resolve(base_info)
            append_fields(base_info)
        append_fields(info)

        flattened_types = []
        index = 0
        while index < len(type_names):
            flattened_types.append((type_names[index], type_values[index]))
            index += 1
        info["field_names"] = tuple(names)
        info["field_types"] = tuple(flattened_types)
        visiting.pop()
        resolved.append(info)

    for module_exports in native_exports.values():
        for info in module_exports.values():
            if isinstance(info, dict) and info.get("kind") == "class":
                resolve(info)


def _merge_closed_world_reexports(
    parsed_modules, module_names, src_paths, native_exports
):
    """Propagate native function/class/constant exports across package re-exports.

    Real packages commonly expose their public API through ``__init__.py`` using
    ``from .mod import name`` and ``from .mod import *``.  Without modelling
    that in the closed-world table, downstream modules see a missing native
    export and fall back to ``py_cpy_import`` even though both sides are compiled
    in the same native invocation.
    """
    from .py_ast import ImportFrom as _ImportFrom

    module_set = set(module_names)
    module_to_ast = {
        mod_name: ast_mod for mod_name, ast_mod in zip(module_names, parsed_modules)
    }
    module_to_src = {
        mod_name: src_path for mod_name, src_path in zip(module_names, src_paths)
    }

    changed = True
    while changed:
        changed = False
        for mod_name in module_names:
            ast_mod = module_to_ast.get(mod_name)
            src_path = module_to_src.get(mod_name, "")
            if ast_mod is None:
                continue
            exports = native_exports.setdefault(mod_name, {})
            for stmt in _py_ast_field_value(ast_mod, "body", ()):
                if not isinstance(stmt, _ImportFrom):
                    continue
                src_mod = _resolve_ast_import_from_module(src_path, mod_name, stmt)
                if not src_mod:
                    continue

                # ``from . import submodule`` re-exports a module object, not a
                # function/class binding.  Import lowering already handles that
                # by registering native module aliases, so do not invent an
                # export-table entry for it here.
                for attr_name, as_name in _py_ast_field_value(stmt, "names", ()):
                    if attr_name == "*":
                        src_exports = native_exports.get(src_mod)
                        if not src_exports:
                            continue
                        for export_name, info in _closed_world_star_export_items(
                            src_exports
                        ):
                            if export_name in exports:
                                continue
                            exports[export_name] = info
                            changed = True
                        continue
                    local_name = as_name or attr_name
                    src_exports = native_exports.get(src_mod)
                    if src_exports and attr_name in src_exports:
                        if exports.get(local_name) is not src_exports[attr_name]:
                            exports[local_name] = src_exports[attr_name]
                            changed = True
                        continue
                    full_submodule = f"{src_mod}.{attr_name}"
                    if full_submodule in module_set:
                        continue


def _closed_world_reexport_edges(
    parsed_modules, module_names, src_paths, all_module_names
):
    from .py_ast import ImportFrom as _ImportFrom

    module_set = set(all_module_names)
    edges = []
    for mod_name, ast_mod, src_path in zip(module_names, parsed_modules, src_paths):
        if ast_mod is None:
            continue
        for stmt in _py_ast_field_value(ast_mod, "body", ()):
            if not isinstance(stmt, _ImportFrom):
                continue
            src_mod = _resolve_ast_import_from_module(src_path, mod_name, stmt)
            if not src_mod:
                continue
            for attr_name, as_name in _py_ast_field_value(stmt, "names", ()):
                if attr_name == "*":
                    edges.append((mod_name, src_mod, "*", "", True))
                    continue
                local_name = as_name or attr_name
                full_submodule = f"{src_mod}.{attr_name}"
                if full_submodule in module_set:
                    continue
                edges.append((mod_name, src_mod, attr_name, local_name, False))
    return edges


def _closed_world_module_dependencies(
    parsed_modules,
    module_names,
    src_paths,
    all_module_names,
):
    """Publish exact in-bundle import edges from each worker-owned AST."""

    from .py_ast import Import as _Import
    from .py_ast import ImportFrom as _ImportFrom
    from .py_ast import Type as _Type

    known_modules = list(all_module_names)
    rows = []
    for ast_mod, module_name, src_path in zip(
        parsed_modules,
        module_names,
        src_paths,
    ):
        dependencies = []

        def add_dependency(candidate: str) -> None:
            resolved = candidate
            if (
                resolved == "pcc.llvm_capi.compat"
                and resolved not in known_modules
                and "pcc.llvm_capi.ir" in known_modules
            ):
                resolved = "pcc.llvm_capi.ir"
            if (
                resolved in known_modules
                and resolved != module_name
                and resolved not in dependencies
            ):
                dependencies.append(resolved)

        pending = [ast_mod]
        seen = set()
        while pending:
            node = pending.pop()
            if node is None or _closed_world_is_node(node, _Type):
                continue
            if isinstance(node, (tuple, list)):
                pending.extend(node)
                continue
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            if _closed_world_is_node(node, _Import):
                for imported_name, _as_name in _py_ast_field_value(
                    node, "names", ()
                ):
                    parts = imported_name.split(".")
                    end = len(parts)
                    while end > 0:
                        add_dependency(_join_dotted_parts(parts[:end]))
                        end -= 1
            elif _closed_world_is_node(node, _ImportFrom):
                resolved = _resolve_ast_import_from_module(
                    src_path,
                    module_name,
                    node,
                )
                add_dependency(resolved)
                for imported_name, _as_name in _py_ast_field_value(
                    node, "names", ()
                ):
                    if imported_name != "*":
                        add_dependency(
                            _join_dotted_parts([resolved, imported_name])
                        )
            for field_name in _py_ast_field_names(node):
                if field_name in ("annotation", "return_ty", "ty", "span"):
                    continue
                pending.append(_py_ast_field_value(node, field_name, None))
        rows.append((module_name, tuple(dependencies)))
    return tuple(rows)


def _merge_closed_world_reexport_edges(module_names, native_exports, edges):
    changed = True
    while changed:
        changed = False
        for mod_name in module_names:
            exports = native_exports.setdefault(mod_name, {})
            for edge in edges:
                if len(edge) < 5 or edge[0] != mod_name:
                    continue
                _dst_mod, src_mod, attr_name, local_name, is_star = edge
                src_exports = native_exports.get(src_mod)
                if not src_exports:
                    continue
                if is_star:
                    for export_name, info in _closed_world_star_export_items(
                        src_exports
                    ):
                        if export_name in exports:
                            continue
                        exports[export_name] = info
                        changed = True
                    continue
                if (
                    attr_name in src_exports
                    and exports.get(local_name) is not src_exports[attr_name]
                ):
                    exports[local_name] = src_exports[attr_name]
                    changed = True


def _repair_closed_world_default_global_owners(native_exports) -> None:
    """Point exported function defaults through their original module.

    A module may import ``NAME`` and then use it in a method default. The
    closed-world call signature is built before re-export merging, so its
    initial owner is the importing module even though that module has no
    ``.modvar`` definition for the name. Rebind such references after the
    export graph has converged.
    """
    seen: set[int] = set()

    def visit(value) -> None:
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            ref = value.get("default_native_global")
            if isinstance(ref, dict):
                owner = str(ref.get("owning_module", ""))
                name = str(ref.get("name", ""))
                source = native_exports.get(owner, {}).get(name)
                if isinstance(source, dict):
                    source_owner = str(source.get("owning_module", owner))
                    source_name = str(source.get("export_name", name))
                    if source_owner != owner or source_name != name:
                        repaired = {
                            "owning_module": source_owner,
                            "name": source_name,
                        }
                        attrs = ref.get("attrs")
                        if attrs:
                            repaired["attrs"] = tuple(attrs)
                        value["default_native_global"] = repaired
                    # The sentinel is reconstructed in the consuming module.
                    # Preserve a scalar global's semantic type so a raw i64,
                    # i1 or double load is not mistaken for a PyObject*.
                    value_ty = source.get("value_ty")
                    if (
                        source.get("kind") == "module_global"
                        and isinstance(value_ty, (tuple, list))
                        and value_ty
                        and value_ty[0] in ("int", "bool", "float")
                    ):
                        value["default_native_global"]["value_ty"] = value_ty
            for child in value.values():
                visit(child)
            return
        if isinstance(value, (tuple, list)):
            for child in value:
                visit(child)

    visit(native_exports)


def _mark_closed_world_function_object_exports(
    parsed_modules,
    module_names,
    src_paths,
    native_exports,
    known_module_names=None,
):
    """Mark function exports that must exist as runtime objects.

    Direct native calls can use an exported entry point without allocating a
    ``PyFunc``.  Module attribute reads and explicit ``from`` imports cannot:
    they observe a stable Python function object.  Record only those uses so
    metadata-decorated package functions are published when required without
    eagerly wrapping every decorated function in the closed world.
    """
    from .py_ast import Attr as _Attr
    from .py_ast import Import as _Import
    from .py_ast import ImportFrom as _ImportFrom
    from .py_ast import Name as _Name
    from .py_ast import Type as _Type

    # This table is small (one entry per closed-world module), and a list is
    # the reliable self-host projection here.  pcc1's set construction can
    # lose string members, causing relative module imports such as
    # ``from . import provider`` to be mistaken for value imports; later
    # ``provider.fn`` reads then never mark ``fn`` as needing a PyFunc object.
    known_modules = list(known_module_names or native_exports.keys())
    uses = []
    use_keys = set()

    def mark(module_name: str, attr_name: str) -> None:
        key = module_name + "\x00" + attr_name
        if key not in use_keys:
            use_keys.add(key)
            uses.append((module_name, attr_name))
        info = native_exports.get(module_name, {}).get(attr_name)
        if isinstance(info, dict) and info.get("kind") == "function":
            info["needs_object"] = True

    def collect_nodes(root):
        pending = [root]
        seen = set()
        out = []
        while pending:
            node = pending.pop()
            if node is None or isinstance(node, _Type):
                continue
            if isinstance(node, (tuple, list)):
                pending.extend(node)
                continue
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(node)
            for field_name in _py_ast_field_names(node):
                if field_name in ("annotation", "return_ty", "ty", "span"):
                    continue
                pending.append(_py_ast_field_value(node, field_name, None))
        return out

    def attr_parts(node):
        parts = []
        current = node
        while isinstance(current, _Attr):
            parts.append(_py_ast_field_value(current, "name", ""))
            current = _py_ast_field_value(current, "obj", None)
        if not isinstance(current, _Name):
            return None
        parts.append(_py_ast_field_value(current, "ident", ""))
        parts.reverse()
        return parts

    for ast_mod, mod_name, src_path in zip(
        parsed_modules,
        module_names,
        src_paths,
    ):
        module_aliases = {}
        body = _py_ast_field_value(ast_mod, "body", ())
        nodes = collect_nodes(body)
        for node in nodes:
            if isinstance(node, _Import):
                for imported_name, as_name in _py_ast_field_value(
                    node,
                    "names",
                    (),
                ):
                    local_name = as_name or imported_name.split(".")[0]
                    target_module = (
                        imported_name
                        if as_name is not None
                        else imported_name.split(".")[0]
                    )
                    alias_targets = module_aliases.get(local_name)
                    if alias_targets is None:
                        alias_targets = []
                        module_aliases[local_name] = alias_targets
                    if target_module not in alias_targets:
                        alias_targets.append(target_module)
                continue
            if not isinstance(node, _ImportFrom):
                continue
            resolved = _resolve_ast_import_from_module(src_path, mod_name, node)
            for imported_name, as_name in _py_ast_field_value(
                node,
                "names",
                (),
            ):
                if imported_name == "*":
                    mark(resolved, "*")
                    for export_name, info in native_exports.get(resolved, {}).items():
                        if isinstance(info, dict) and info.get("kind") == "function":
                            mark(resolved, export_name)
                    continue
                local_name = as_name or imported_name
                candidate_module = _join_dotted_parts([resolved, imported_name])
                if candidate_module in known_modules:
                    alias_targets = module_aliases.get(local_name)
                    if alias_targets is None:
                        alias_targets = []
                        module_aliases[local_name] = alias_targets
                    if candidate_module not in alias_targets:
                        alias_targets.append(candidate_module)
                    continue
                mark(resolved, imported_name)

        for node in nodes:
            if not isinstance(node, _Attr):
                continue
            parts = attr_parts(node)
            if not parts or len(parts) < 2:
                continue
            for root_module in module_aliases.get(parts[0], ()):
                owner_module = root_module
                if len(parts) > 2:
                    candidate_module = _join_dotted_parts([root_module] + parts[1:-1])
                    if candidate_module in known_modules:
                        owner_module = candidate_module
                mark(owner_module, parts[-1])
    return tuple(uses)


def _apply_closed_world_function_object_uses(native_exports, uses) -> None:
    for module_name, attr_name in uses:
        exports = native_exports.get(module_name, {})
        if attr_name == "*":
            for info in exports.values():
                if isinstance(info, dict) and info.get("kind") == "function":
                    info["needs_object"] = True
            continue
        info = exports.get(attr_name)
        if isinstance(info, dict) and info.get("kind") == "function":
            info["needs_object"] = True


def _closed_world_function_object_exports(native_exports, module_name: str):
    out = {}
    for export_name, info in native_exports.get(module_name, {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("kind") != "function" or not info.get("needs_object"):
            continue
        if info.get("owning_module", module_name) != module_name:
            continue
        out[export_name] = True
    return out


def _write_reexport_edges_wire(path: str, edges, module_dependencies=()) -> None:
    payload = {
        "schema": "pcc.py_frontend.reexport_edges.v1",
        "edges": _native_export_to_wire(edges),
        "module_dependencies": _native_export_to_wire(module_dependencies),
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload))


def _read_reexport_edges_wire(path: str, include_module_dependencies: bool = False):
    with open(path, "r", encoding="utf-8") as f:
        payload = json.loads(f.read())
    if payload.get("schema") != "pcc.py_frontend.reexport_edges.v1":
        raise PyPipelineError("invalid frontend reexport edges file")
    edges = _native_export_from_wire(payload.get("edges", ()))
    if include_module_dependencies:
        dependencies = _native_export_from_wire(
            payload.get("module_dependencies", ())
        )
        return edges, dependencies
    return edges


def _closed_world_shallow_func_body(lifter, raw_func, include_assigns: bool):
    from ..parse.py_parse import _Assign as _PPAssign
    from ..parse.py_parse import _Expr as _PPExpr
    from ..parse.py_parse import _Str as _PPStr

    out = []
    for raw_stmt in raw_func.body:
        raw_type = type(raw_stmt)
        if raw_type is _PPExpr and type(raw_stmt.expr) is _PPStr and not out:
            out.append(lifter.lift_stmt(raw_stmt))
            continue
        if include_assigns and raw_type is _PPAssign:
            out.append(lifter.lift_stmt(raw_stmt))
    return tuple(out)


def _closed_world_shallow_func(lifter, raw_func, body):
    from . import py_ast as _pa

    args_list = []
    for param in raw_func.params:
        args_list.append(lifter._lift_arg(param))
    deco_list = []
    for dec in raw_func.decorators or ():
        deco_list.append(lifter.lift_expr(dec))
    from ..parse.py_lift import _lift_type as _lift_type_for_closed_world

    returns = raw_func.returns
    ret_ty = _lift_type_for_closed_world(returns) if returns is not None else None
    return _pa.FuncDef(
        lifter._span(raw_func.line),
        str(raw_func.name),
        tuple(args_list),
        ret_ty,
        tuple(body),
        tuple(deco_list),
        False,
        bool(raw_func.is_async),
    )


def _closed_world_shallow_lift_module(raw_mod, filename: str, module_name: str):
    """Lift only the AST surface needed by closed-world export discovery.

    Full function bodies are unnecessary for the native export table. Keeping
    only signatures, decorators, docstrings, class-body assignments, and
    ``__init__`` field assignments avoids lifting most sibling modules in
    parallel codegen workers while preserving the py_ast shape consumed by the
    existing export/re-export logic.
    """
    from ..parse.py_lift import _Lifter as _ClosedWorldLifter
    from ..parse.py_parse import _Assign as _PPAssign
    from ..parse.py_parse import _ClassDef as _PPClassDef
    from ..parse.py_parse import _Expr as _PPExpr
    from ..parse.py_parse import _FuncDef as _PPFuncDef
    from ..parse.py_parse import _ImportFrom as _PPImportFrom
    from ..parse.py_parse import _Str as _PPStr
    from . import py_ast as _pa

    lifter = _ClosedWorldLifter(filename)
    body = []
    docstring = None
    for raw_stmt in raw_mod.body:
        raw_type = type(raw_stmt)
        if raw_type is _PPImportFrom:
            body.append(lifter.lift_stmt(raw_stmt))
            continue
        if raw_type is _PPAssign:
            body.append(lifter.lift_stmt(raw_stmt))
            continue
        if raw_type is _PPExpr and type(raw_stmt.expr) is _PPStr:
            lifted_expr = lifter.lift_stmt(raw_stmt)
            if docstring is None:
                try:
                    docstring = lifted_expr.expr.value
                except Exception:
                    docstring = None
            body.append(lifted_expr)
            continue
        if raw_type is _PPFuncDef:
            func_body = _closed_world_shallow_func_body(
                lifter,
                raw_stmt,
                include_assigns=False,
            )
            body.append(
                _closed_world_shallow_func(
                    lifter,
                    raw_stmt,
                    func_body,
                )
            )
            continue
        if raw_type is not _PPClassDef:
            continue

        class_body = []
        for raw_body_stmt in raw_stmt.body:
            body_type = type(raw_body_stmt)
            if body_type is _PPAssign:
                class_body.append(lifter.lift_stmt(raw_body_stmt))
                continue
            if body_type is _PPFuncDef:
                include_assigns = str(raw_body_stmt.name) == "__init__"
                method_body = _closed_world_shallow_func_body(
                    lifter,
                    raw_body_stmt,
                    include_assigns=include_assigns,
                )
                class_body.append(
                    _closed_world_shallow_func(
                        lifter,
                        raw_body_stmt,
                        method_body,
                    )
                )
        bases = []
        keywords = []
        for base in raw_stmt.bases:
            if (
                isinstance(base, tuple)
                and len(base) == 4
                and base[0] == "__pcc_kwarg__"
            ):
                keywords.append((base[1], lifter.lift_expr(base[2])))
                continue
            bases.append(lifter.lift_expr(base))
        decorators = []
        for dec in raw_stmt.decorators:
            decorators.append(lifter.lift_expr(dec))
        body.append(
            _pa.ClassDef(
                lifter._span(raw_stmt.line),
                str(raw_stmt.name),
                tuple(bases),
                tuple(keywords),
                tuple(class_body),
                tuple(decorators),
            )
        )
    return _pa.Module(module_name, tuple(body), docstring)


def _closed_world_is_identity_decorator(stmt) -> bool:
    """Whether a function can only return its first argument unchanged.

    Imported bare decorators are normally semantic and must not be discarded.
    A narrow metadata-decorator shape is safe for native callable publication:
    straight-line expression/assignment side effects followed by
    ``return <first positional argument>``.  This covers decorators that set
    documentation or registration metadata while preserving call identity,
    without treating arbitrary sibling decorators as no-ops.
    """
    from .py_ast import Assign as _Assign
    from .py_ast import ExprStmt as _ExprStmt
    from .py_ast import Name as _Name
    from .py_ast import Return as _Return

    args = _py_ast_field_value(stmt, "args", ())
    if not args:
        return False
    first_arg = args[0]
    if _py_ast_field_value(first_arg, "kind", "") not in ("pos", "pos_only"):
        return False
    first_name = _py_ast_field_value(first_arg, "name", "")
    if not first_name:
        return False
    body = _py_ast_field_value(stmt, "body", ())
    if not body:
        return False
    for prefix_stmt in body[:-1]:
        if not _closed_world_is_node(prefix_stmt, (_ExprStmt, _Assign)):
            return False
    final_stmt = body[-1]
    if not _closed_world_is_node(final_stmt, _Return):
        return False
    return_value = _py_ast_field_value(final_stmt, "value", None)
    return _closed_world_is_node(return_value, _Name) and (
        _py_ast_field_value(return_value, "ident", "") == first_name
    )
