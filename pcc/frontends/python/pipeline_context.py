"""Closed-world contextual exports and fallback diagnostics.

This module owns the semantic context shared by multi-file compilation and
standalone per-module fallback probes.  The pipeline module re-exports the
public facade names so existing callers keep a stable API.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_ATTRS
from pcc.frontends.python.codegen.typed_int_bounded_proof import compute_bounded_int_abi_function_names
from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
from pcc.frontends.python.codegen.vthread_effect_analysis import annotate_closed_world_vthread_effect_summaries, annotate_closed_world_vthread_effects, build_closed_world_vthread_effect_summary, closed_world_vthread_effect_export_surface, read_closed_world_vthread_effect_summary, write_closed_world_vthread_effect_summary
from pcc.frontends.python.export_meta import encode_type
from pcc.frontends.python.private_names import mangle_private_name
from pcc.frontends.python.raw_pointer_types import raw_pointer_annotation_names, verified_c_abi_export_symbol
from pcc.frontends.python.pipeline_freestanding import (
    source_declares_freestanding_module,
    source_declares_runtime_port_module,
)
from pcc.frontends.python.pipeline_ast_wire import _PY_AST_BASE_NAME_OVERRIDES, _PY_AST_FIELD_NAME_OVERRIDES, _py_ast_field_type_override
from pcc.frontends.python.pipeline_closed_world import _closed_world_dyn_module_global_export, _closed_world_function_object_exports, _closed_world_is_identity_decorator, _flatten_closed_world_class_export_fields, _closed_world_module_block_assign_targets, _closed_world_module_def_counts, _closed_world_shallow_lift_module, _mark_closed_world_function_object_exports, _merge_closed_world_reexports, _repair_closed_world_default_global_owners
from pcc.frontends.python.pipeline_exports import instance_field_assignment_statements, _class_is_dataclass, _class_is_valueclass, _closed_world_is_node, _export_annotation_or_none, export_default_factory_name as _export_default_factory_name, _export_call_sig, _export_module_defines_str_default_cells, _expand_local_valueclass_export_refs, _export_func_uses_unboxed_typed_int_abi, _export_literal_value_or_none, _export_method_symbol, _export_param_types, _export_return_ty_or_none, _export_return_type, _export_returns_none, _export_static_all_names, _export_static_literal_type, _normalise_export_annotation_text
from pcc.frontends.python.pipeline_exports import export_dataclass_factory_default
from pcc.frontends.python.pipeline_exports import _export_func_has_python_int_signature
from pcc.frontends.python.pipeline_import_policy import dataclasses_field_binding_names
from pcc.frontends.python.pipeline_closed_world import (
    qualified_class_base_name,
    resolve_class_base_export,
    _closed_world_boxed_int_functions,
)
from pcc.frontends.python.pipeline_exports import annotation_module_bindings, _qualify_export_annotation_refs
from pcc.frontends.python.pipeline_libpython import ast_field_value as _py_ast_field_value
from pcc.frontends.python.pipeline_profile import profile_begin as _profile_begin, profile_counter as _profile_counter, profile_end as _profile_end


def build_closed_world_context(
    src_paths,
    module_names,
    profile: Optional[dict] = None,
    lift_indices=None,
    merge_exports: bool = True,
    allow_local_int_abi_proofs: bool | None = None,
):
    """Build the class/export context for closed-world Python compiles.

    Contextual per-module probes need the same semantic model as the
    multi-file self-host path: a mixin method's ``self`` is the final
    host class (currently ``L1CodeGen``), not the standalone mixin class.
    This helper returns the parsed modules, native export table, and
    inverse base-to-derived map required to run that inference model
    outside ``compile_python_multi``.
    """
    _profile_counter(profile, "build_closed_world_context_entered", len(src_paths))
    import_t = _profile_begin(profile)
    from pcc.frontends.python.py_ast import Assign as _Assign
    from pcc.frontends.python.py_ast import assignment_storage_annotation
    from pcc.frontends.python.py_ast import Attr as _Attr
    from pcc.frontends.python.py_ast import BinOp as _BinOp
    from pcc.frontends.python.py_ast import BoolLit as _BoolLit
    from pcc.frontends.python.py_ast import BoolType as _BoolType
    from pcc.frontends.python.py_ast import Call as _Call
    from pcc.frontends.python.py_ast import ClassDef as _ClassDef
    from pcc.frontends.python.py_ast import ClassType as _ClassType
    from pcc.frontends.python.py_ast import Compare as _Compare
    from pcc.frontends.python.py_ast import DictExpr as _DictExpr
    from pcc.frontends.python.py_ast import DynType as _DynType
    from pcc.frontends.python.py_ast import ExprStmt as _ExprStmt
    from pcc.frontends.python.py_ast import FuncDef as _FuncDef
    from pcc.frontends.python.py_ast import FloatLit as _FloatLit
    from pcc.frontends.python.py_ast import FloatType as _FloatType
    from pcc.frontends.python.py_ast import Import as _Import
    from pcc.frontends.python.py_ast import ImportFrom as _ImportFrom
    from pcc.frontends.python.py_ast import IntLit as _IntLit
    from pcc.frontends.python.py_ast import IntType as _IntType
    from pcc.frontends.python.py_ast import ListExpr as _ListExpr
    from pcc.frontends.python.py_ast import Name as _Name
    from pcc.frontends.python.py_ast import NoneLit as _NoneLit
    from pcc.frontends.python.py_ast import StrLit as _StrLit
    from pcc.frontends.python.py_ast import StrType as _StrType
    from pcc.frontends.python.py_ast import Subscript as _Subscript
    from pcc.frontends.python.py_ast import TupleExpr as _TupleExpr
    from pcc.frontends.python.py_ast import UnaryOp as _UnaryOp
    from pcc.frontends.python.pipeline_exports import _export_signed_int_literal_or_none

    def known_module_scalar_expr(expr, known_names) -> str:
        """Scalar storage inferred through prior module-top bindings."""
        if _closed_world_is_node(expr, _BoolLit):
            return "bool"
        if _closed_world_is_node(expr, _IntLit):
            return "int"
        if _closed_world_is_node(expr, _FloatLit):
            return "float"
        if _closed_world_is_node(expr, _Name):
            return known_names.get(_py_ast_field_value(expr, "ident", ""), "")
        op = _py_ast_field_value(expr, "op", "")
        if _closed_world_is_node(expr, _UnaryOp):
            if op == "not":
                return "bool"
            operand = known_module_scalar_expr(_py_ast_field_value(expr, "operand", None), known_names)
            if op in ("+", "-") and operand:
                return "float" if operand == "float" else "int"
            if op == "~" and operand in ("int", "bool"):
                return "int"
            return ""
        if _closed_world_is_node(expr, _Compare):
            if op in ("is", "is not", "in", "not in"):
                return "bool"
            left = known_module_scalar_expr(_py_ast_field_value(expr, "lhs", None), known_names)
            right = known_module_scalar_expr(_py_ast_field_value(expr, "rhs", None), known_names)
            return "bool" if left and right else ""
        if not _closed_world_is_node(expr, _BinOp):
            return ""
        left = known_module_scalar_expr(_py_ast_field_value(expr, "lhs", None), known_names)
        right = known_module_scalar_expr(_py_ast_field_value(expr, "rhs", None), known_names)
        if not left or not right:
            return ""
        if op == "/":
            return "float"
        if op in ("+", "-", "*", "//", "%"):
            return "float" if "float" in (left, right) else "int"
        if "float" not in (left, right) and op in ("&", "|", "^", "<<", ">>"):
            if left == right == "bool" and op in ("&", "|", "^"):
                return "bool"
            return "int"
        return ""

    _profile_end(profile, "build_closed_world_context_import_py_ast", import_t)
    import_t = _profile_begin(profile)
    from pcc.frontends.python.py_lift import finish_lifted_module as _finish_lifted_module
    from pcc.frontends.python.py_lift import lift_module as _lift_module
    from pcc.frontends.python.py_parse import parse as _parse_python_module

    _profile_end(profile, "build_closed_world_context_import_py_lift", import_t)

    lift_index_set = None
    if lift_indices is not None:
        lift_index_set = {}
        for lift_index in lift_indices:
            lift_index_set[int(lift_index)] = True

    parsed_modules = []
    native_exports = {}
    module_index = 0
    for src, mod_name in zip(src_paths, module_names):
        module_t = _profile_begin(profile)
        parse_t = _profile_begin(profile)
        with open(src, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            raw_mod = _parse_python_module(source, filename=src)
        except Exception as ex:
            from pcc.frontends.python.py_lift import LiftError as _LiftError

            raise _LiftError(
                "parse failed for "
                + mod_name
                + " in "
                + src
                + ": "
                + type(ex).__name__
                + ": "
                + str(ex)
            )
        _profile_end(profile, "build_closed_world_context_parse", parse_t, mod_name)
        lift_t = _profile_begin(profile)
        if lift_index_set is None or module_index in lift_index_set:
            ast_mod = _finish_lifted_module(_lift_module(raw_mod, src, mod_name))
        else:
            ast_mod = _finish_lifted_module(_closed_world_shallow_lift_module(raw_mod, src, mod_name))
        _profile_end(profile, "build_closed_world_context_lift", lift_t, mod_name)
        parsed_modules.append(ast_mod)
        exports = {}
        known_scalar_names = {}
        class_field_defs = {}
        class_init_field_defs = {}
        class_field_names = {}
        top_level_func_names = set()
        top_level_class_names = set()
        ast_body = _py_ast_field_value(ast_mod, "body", ())
        raw_pointer_names = raw_pointer_annotation_names(ast_mod)
        str_default_cells = _export_module_defines_str_default_cells(ast_body)
        manual_pointer_abi = source_declares_freestanding_module(source) or source_declares_runtime_port_module(source)
        typing_metadata_bindings = {}
        typing_module_aliases = set()
        typing_metadata_exports = (
            "Any",
            "Callable",
            "ClassVar",
            "Dict",
            "Final",
            "Generic",
            "Iterable",
            "Iterator",
            "List",
            "Literal",
            "Mapping",
            "NoReturn",
            "Optional",
            "Protocol",
            "Sequence",
            "Set",
            "SupportsIndex",
            "Tuple",
            "Type",
            "TypeAlias",
            "TypeAliasType",
            "TypedDict",
            "TypeVar",
            "Union",
        )
        for stmt in ast_body:
            if _closed_world_is_node(stmt, _ImportFrom) and (
                _py_ast_field_value(stmt, "module", "") == "typing"
                and not _py_ast_field_value(stmt, "level", 0)
            ):
                for attr_name, as_name in _py_ast_field_value(stmt, "names", ()):
                    if attr_name in typing_metadata_exports:
                        typing_metadata_bindings[as_name or attr_name] = attr_name
                continue
            if not _closed_world_is_node(stmt, _Import):
                continue
            for imported_module, as_name in _py_ast_field_value(stmt, "names", ()):
                if imported_module == "typing":
                    typing_module_aliases.add(as_name or "typing")

        def is_typing_metadata_expr(expr):
            if _closed_world_is_node(expr, _Name):
                return (
                    _py_ast_field_value(expr, "ident", "") in typing_metadata_bindings
                )
            if _closed_world_is_node(expr, _Attr):
                obj = _py_ast_field_value(expr, "obj", None)
                return (
                    _closed_world_is_node(obj, _Name)
                    and _py_ast_field_value(obj, "ident", "") in typing_module_aliases
                    and _py_ast_field_value(expr, "name", "") in typing_metadata_exports
                )
            if _closed_world_is_node(expr, _Subscript):
                return is_typing_metadata_expr(_py_ast_field_value(expr, "obj", None))
            if _closed_world_is_node(expr, _Call):
                return is_typing_metadata_expr(_py_ast_field_value(expr, "func", None))
            if (
                _closed_world_is_node(expr, _BinOp)
                and _py_ast_field_value(
                    expr,
                    "op",
                    "",
                )
                == "|"
            ):
                return is_typing_metadata_expr(
                    _py_ast_field_value(expr, "lhs", None)
                ) or is_typing_metadata_expr(_py_ast_field_value(expr, "rhs", None))
            if _closed_world_is_node(expr, _TupleExpr):
                for elem in _py_ast_field_value(expr, "elems", ()):
                    if is_typing_metadata_expr(elem):
                        return True
            return False

        for stmt in ast_body:
            if _closed_world_is_node(stmt, _FuncDef):
                top_level_func_names.add(_py_ast_field_value(stmt, "name", ""))
            elif _closed_world_is_node(stmt, _ClassDef):
                top_level_class_names.add(_py_ast_field_value(stmt, "name", ""))
        module_def_counts = _closed_world_module_def_counts(ast_body)

        def decorator_root_name(expr):
            if _closed_world_is_node(expr, _Call):
                return decorator_root_name(_py_ast_field_value(expr, "func", None))
            current = expr
            while _closed_world_is_node(current, _Attr):
                current = _py_ast_field_value(current, "obj", None)
            if _closed_world_is_node(current, _Name):
                return _py_ast_field_value(current, "ident", "")
            return ""

        partial_decorator_factories = set()
        for stmt in ast_body:
            if not _closed_world_is_node(stmt, _Assign):
                continue
            targets = _py_ast_field_value(stmt, "targets", ())
            if len(targets) != 1 or not _closed_world_is_node(targets[0], _Name):
                continue
            value = _py_ast_field_value(stmt, "value", None)
            if not _closed_world_is_node(value, _Call):
                continue
            partial_func = _py_ast_field_value(value, "func", None)
            is_partial = (
                _closed_world_is_node(partial_func, _Name)
                and _py_ast_field_value(partial_func, "ident", "") == "partial"
            ) or (
                _closed_world_is_node(partial_func, _Attr)
                and _py_ast_field_value(partial_func, "name", "") == "partial"
            )
            if is_partial:
                partial_decorator_factories.add(
                    _py_ast_field_value(targets[0], "ident", "")
                )

        def has_semantic_native_decorator(stmt):
            for decorator in _py_ast_field_value(stmt, "decorators", ()):
                if _closed_world_is_node(decorator, _Call):
                    if decorator_root_name(decorator) in partial_decorator_factories:
                        return True
                    continue
                if _closed_world_is_node(decorator, _Name):
                    if (
                        _py_ast_field_value(decorator, "ident", "")
                        in top_level_func_names
                    ):
                        return True
            return False

        module_uses_raw_int_scaffold = (
            mod_name == "pcc"
            or mod_name.startswith("pcc.")
            or mod_name == "bootstrap"
            or mod_name.startswith("bootstrap.")
        )
        if not module_uses_raw_int_scaffold:
            raw_int_scaffold_modules = (
                "pcc.extern",
                "pcc.ir",
                "pcc.ir.compat",
                "pcc.unsafe",
            )
            for module_stmt in ast_body:
                if _closed_world_is_node(module_stmt, _ImportFrom):
                    imported_module = _py_ast_field_value(module_stmt, "module", "")
                    if imported_module in raw_int_scaffold_modules:
                        module_uses_raw_int_scaffold = True
                        break
                if _closed_world_is_node(module_stmt, _Import):
                    for imported_module, _alias in _py_ast_field_value(
                        module_stmt, "names", ()
                    ):
                        if imported_module in raw_int_scaffold_modules:
                            module_uses_raw_int_scaffold = True
                            break
                    if module_uses_raw_int_scaffold:
                        break
        module_box_int_abi = not module_uses_raw_int_scaffold
        local_int_proofs_closed = (
            len(module_names) == 1
            if allow_local_int_abi_proofs is None
            else allow_local_int_abi_proofs
        )
        bounded_int_functions = (
            compute_bounded_int_abi_function_names(ast_mod)
            if local_int_proofs_closed else []
        )
        freestanding_int_abi = manual_pointer_abi
        for stmt in ast_body:
            if _closed_world_is_node(stmt, _FuncDef):
                function_box_int_abi = module_box_int_abi or _export_func_has_python_int_signature(stmt)
                if (
                    freestanding_int_abi
                    or bool(verified_c_abi_export_symbol(ast_mod, stmt))
                    or _export_func_uses_unboxed_typed_int_abi(
                        stmt, _py_ast_field_value(stmt, "name", "") in bounded_int_functions
                    )
                ):
                    function_box_int_abi = False
                docstring = None
                stmt_body = _py_ast_field_value(stmt, "body", ())
                if (
                    stmt_body
                    and _closed_world_is_node(stmt_body[0], _ExprStmt)
                    and _closed_world_is_node(
                        _py_ast_field_value(stmt_body[0], "expr", None),
                        _StrLit,
                    )
                ):
                    docstring = _export_literal_value_or_none(
                        _py_ast_field_value(stmt_body[0], "expr", None)
                    )
                stmt_name = _py_ast_field_value(stmt, "name", "")
                stmt_args = _py_ast_field_value(stmt, "args", ())
                exports[stmt_name] = {
                    "kind": "function",
                    "owning_module": mod_name,
                    "export_name": stmt_name,
                    "return_ty": _export_return_type(_export_return_ty_or_none(stmt), raw_pointer_names),
                    "returns_none": _export_returns_none(
                        _export_return_ty_or_none(stmt)
                    ),
                    "param_types": _export_param_types(stmt_args, raw_pointer_names),
                    "call_sig": _export_call_sig(
                        stmt_args,
                        mod_name,
                        top_level_func_names,
                        raw_pointer_names,
                        str_default_cells,
                    ),
                    "is_async": bool(_py_ast_field_value(stmt, "is_async", False)),
                    "has_return_annotation": bool(_py_ast_field_value(stmt, "has_return_annotation", False)),
                    "manual_pointer_abi": manual_pointer_abi,
                    "manual_pointer_abi_symbol": verified_c_abi_export_symbol(ast_mod, stmt) if manual_pointer_abi else "",
                    "box_int_abi": function_box_int_abi,
                    "docstring": docstring,
                    # One module-level definition has the one native adapter
                    # ``user_<module>_<name>_native_adapter``.
                    "single_definition": module_def_counts.get(stmt_name, 0) == 1,
                }
                if has_semantic_native_decorator(stmt):
                    # The public module binding is the decorator result, not
                    # the undecorated ``user_<module>_<name>`` entry point.
                    # Cross-module callers must load that stable object.
                    exports[stmt_name]["semantic_decorator"] = True
                    exports[stmt_name]["needs_object"] = True
                if _closed_world_is_identity_decorator(stmt):
                    exports[stmt_name]["identity_decorator"] = True
                continue

            if _closed_world_is_node(stmt, _Assign):
                if not _py_ast_field_value(stmt, "has_value", True):
                    continue
                stmt_targets = _py_ast_field_value(stmt, "targets", ())
                if len(stmt_targets) != 1 or not _closed_world_is_node(
                    stmt_targets[0], _Name
                ):
                    for target_name in _closed_world_module_block_assign_targets(stmt):
                        exports[target_name] = _closed_world_dyn_module_global_export(
                            mod_name,
                            target_name,
                            box_int_abi=module_box_int_abi,
                        )
                    continue
                target_name = _py_ast_field_value(stmt_targets[0], "ident", "")
                value = _py_ast_field_value(stmt, "value", None)
                static_value_ty = _export_static_literal_type(value)
                scalar_kind = known_module_scalar_expr(value, known_scalar_names)
                if static_value_ty is None or _closed_world_is_node(static_value_ty, _DynType):
                    if scalar_kind == "int":
                        static_value_ty = _IntType("int")
                    elif scalar_kind == "bool":
                        static_value_ty = _BoolType("bool")
                    elif scalar_kind == "float":
                        static_value_ty = _FloatType("float")
                known_scalar_names.pop(target_name, None)
                if scalar_kind:
                    known_scalar_names[target_name] = scalar_kind
                annotation = _py_ast_field_value(stmt, "annotation", None)
                annotation_name = _py_ast_field_value(annotation, "name", "")
                if typing_metadata_bindings.get(
                    annotation_name
                ) == "TypeAlias" or is_typing_metadata_expr(value):
                    exports[target_name] = {
                        "kind": "typing_metadata",
                        "owning_module": mod_name,
                        "export_name": target_name,
                    }
                    typing_metadata_bindings[target_name] = "alias"
                    continue
                if _closed_world_is_node(value, _StrLit):
                    literal_value = _export_literal_value_or_none(value)
                    if literal_value is None:
                        continue
                    exports[target_name] = {
                        "kind": "constant",
                        "owning_module": mod_name,
                        "export_name": target_name,
                        "value_kind": "str",
                        "value": literal_value,
                    }
                elif _closed_world_is_node(value, _IntLit):
                    literal_value = _export_literal_value_or_none(value)
                    if literal_value is None:
                        continue
                    exports[target_name] = {
                        "kind": "constant",
                        "owning_module": mod_name,
                        "export_name": target_name,
                        "value_kind": "int",
                        "value": int(literal_value),
                    }
                elif _closed_world_is_node(value, _BoolLit):
                    literal_value = _export_literal_value_or_none(value)
                    if literal_value is None:
                        continue
                    exports[target_name] = {
                        "kind": "constant",
                        "owning_module": mod_name,
                        "export_name": target_name,
                        "value_kind": "bool",
                        "value": bool(literal_value),
                    }
                elif _export_signed_int_literal_or_none(value) is not None:
                    # ``FLOOR = -7`` is a UnaryOp over a literal, not an
                    # IntLit; without this it became an untyped module global
                    # whose raw i64 storage the importer read as a tagged
                    # object pointer.
                    exports[target_name] = {
                        "kind": "constant",
                        "owning_module": mod_name,
                        "export_name": target_name,
                        "value_kind": "int",
                        "value": _export_signed_int_literal_or_none(value),
                    }
                elif _closed_world_is_node(value, _NoneLit):
                    exports[target_name] = {
                        "kind": "constant",
                        "owning_module": mod_name,
                        "export_name": target_name,
                        "value_kind": "none",
                        "value": None,
                    }
                else:
                    value_ty = static_value_ty
                    if value_ty is None and _closed_world_is_node(annotation, _StrType):
                        # ``pathsep: str = ";" if ... else ":"`` stores a str
                        # object under either view; the annotation lets an
                        # importer type ``os.pathsep`` as str.
                        value_ty = annotation
                    if value_ty is None and value is not None:
                        # Computed module-top binding (e.g. ``V = 5 + 3``,
                        # ``V = f() + 8``).  pcc cannot statically type the RHS,
                        # but the binding is a real module global: the module's
                        # init code computes it and stores into the
                        # ``.modvar.<mod>.<name>`` slot (confirmed in IR).
                        # Register as DynType so cross-package ``mod.V`` resolves
                        # via the extern module-global load instead of falling
                        # back to ``py_obj_getattr`` on the module-name string,
                        # which raised AttributeError.  Mirrors the Name/Attr
                        # DynType treatment in ``_export_static_literal_type``.
                        # See docs/investigations/
                        # python-package-init-computed-module-attr-no-libpython.md
                        from pcc.frontends.python.py_ast import DynType as _DynType

                        value_ty = _DynType("dyn")
                    if value_ty is not None:
                        exports[target_name] = {
                            "kind": "module_global",
                            "owning_module": mod_name,
                            "export_name": target_name,
                            "value_ty": encode_type(value_ty),
                            "box_int_abi": module_box_int_abi,
                        }
                constant_export = exports.get(target_name)
                if constant_export is not None and constant_export.get("kind") == "constant":
                    # Literal exports are still mutable Python module bindings.
                    # Record their provider storage, rather than guessing the
                    # provider's integer ABI from a consuming module's policy.
                    storage_ty = annotation if annotation is not None else static_value_ty
                    if storage_ty is not None:
                        constant_export["value_ty"] = encode_type(storage_ty)
                        boxes_int = module_box_int_abi
                        if _closed_world_is_node(storage_ty, _IntType):
                            literal_int = _export_signed_int_literal_or_none(value)
                            if literal_int is not None and (
                                literal_int < -(1 << 63) or literal_int > (1 << 63) - 1
                            ):
                                boxes_int = True
                        constant_export["box_int_abi"] = boxes_int
                        constant_export["has_module_storage"] = True
                binding_export = exports.get(target_name)
                if binding_export is not None and binding_export.get("kind") in ("constant", "module_global"):
                    # A pointer-shaped ABI is not proof of a PyObject owner:
                    # unsafe malloc/extern results may also be Dyn/ptr. Admit
                    # only source productions whose managed ownership is known.
                    managed_storage = _closed_world_is_node(
                        value, (_StrLit, _NoneLit, _ListExpr, _TupleExpr, _DictExpr)
                    )
                    if binding_export.get("value_kind") == "int":
                        managed_storage = bool(binding_export.get("box_int_abi", False))
                    binding_export["storage_owner"] = "managed" if managed_storage else "unknown"
                if target_name == "__all__" and target_name in exports:
                    all_names = _export_static_all_names(value)
                    if all_names is not None:
                        exports[target_name]["export_names"] = all_names
                continue

            for target_name in _closed_world_module_block_assign_targets(stmt):
                known_scalar_names.pop(target_name, None)
                if target_name in exports:
                    continue
                exports[target_name] = _closed_world_dyn_module_global_export(
                    mod_name,
                    target_name,
                    box_int_abi=module_box_int_abi,
                )

            if not _closed_world_is_node(stmt, _ClassDef):
                continue

            stmt_name = str(_py_ast_field_value(stmt, "name", ""))
            stmt_bases = _py_ast_field_value(stmt, "bases", ())
            stmt_body = _py_ast_field_value(stmt, "body", ())
            class_is_dataclass = _class_is_dataclass(stmt)
            class_is_valueclass = _class_is_valueclass(stmt)
            class_uses_declared_fields = (
                class_is_dataclass or class_is_valueclass
            )
            field_names = []
            field_defs = []
            init_field_defs = []
            constructor_field_names = set()
            constructor_none_fields = set()
            constructor_value_fields = set()
            declared_field_annotations = {}
            for declared_stmt in stmt_body:
                if (
                    _closed_world_is_node(declared_stmt, _FuncDef)
                    and _py_ast_field_value(declared_stmt, "name", "") == "__init__"
                ):
                    constructor_body = _py_ast_field_value(declared_stmt, "body", ())
                    for constructor_stmt in instance_field_assignment_statements(constructor_body):
                        constructor_targets = _py_ast_field_value(constructor_stmt, "targets", ())
                        if not constructor_targets:
                            constructor_target = _py_ast_field_value(constructor_stmt, "target", None)
                            constructor_targets = (constructor_target,) if constructor_target is not None else ()
                        pending_constructor_targets = list(constructor_targets)
                        while pending_constructor_targets:
                            constructor_target = pending_constructor_targets.pop()
                            if _closed_world_is_node(constructor_target, (_TupleExpr, _ListExpr)):
                                pending_constructor_targets.extend(
                                    _py_ast_field_value(constructor_target, "elems", ())
                                )
                                continue
                            constructor_obj = _py_ast_field_value(constructor_target, "obj", None)
                            if (
                                _closed_world_is_node(constructor_target, _Attr)
                                and _closed_world_is_node(constructor_obj, _Name)
                                and _py_ast_field_value(constructor_obj, "ident", "") == "self"
                            ):
                                constructor_field_name = mangle_private_name(
                                    stmt_name,
                                    _py_ast_field_value(constructor_target, "name", ""),
                                )
                                constructor_field_names.add(constructor_field_name)
                                constructor_value = _py_ast_field_value(
                                    constructor_stmt, "value", None
                                )
                                if _closed_world_is_node(constructor_value, _NoneLit):
                                    constructor_none_fields.add(constructor_field_name)
                                else:
                                    constructor_value_fields.add(constructor_field_name)
                    continue
                if not _closed_world_is_node(declared_stmt, _Assign):
                    continue
                declared_ann = _export_annotation_or_none(declared_stmt)
                if declared_ann is None:
                    continue
                for declared_target in _py_ast_field_value(declared_stmt, "targets", ()):
                    if _closed_world_is_node(declared_target, _Name):
                        declared_name = mangle_private_name(
                            stmt_name, _py_ast_field_value(declared_target, "ident", ""),
                        )
                        declared_field_annotations[declared_name] = assignment_storage_annotation(
                            declared_ann, declared_stmt.value, declared_stmt.has_value,
                        ) if not class_is_valueclass else declared_ann
            for base_expr in stmt_bases:
                if not _closed_world_is_node(base_expr, _Name):
                    continue
                base_ident = _py_ast_field_value(base_expr, "ident", "")
                for inherited_name in class_field_names.get(base_ident, ()):
                    if inherited_name not in field_names:
                        field_names.append(inherited_name)
                for inherited in class_field_defs.get(base_ident, ()):
                    field_defs.append(inherited)
                for inherited_init in class_init_field_defs.get(base_ident, ()):
                    init_field_defs.append(inherited_init)

            methods = []
            for body_stmt in stmt_body:
                if _closed_world_is_node(body_stmt, _Assign):
                    body_value = (
                        _py_ast_field_value(body_stmt, "value", None)
                        if _py_ast_field_value(body_stmt, "has_value", True)
                        else None
                    )
                    if class_is_dataclass and body_value is not None:
                        body_value = export_dataclass_factory_default(
                            body_value, dataclasses_field_binding_names(ast_mod),
                        )
                    for target in _py_ast_field_value(body_stmt, "targets", ()):
                        if (
                            _closed_world_is_node(target, _Name)
                            and _py_ast_field_value(target, "ident", "") == "__slots__"
                        ):
                            slot_names = []
                            if _closed_world_is_node(body_value, _StrLit):
                                slot_value = _export_literal_value_or_none(body_value)
                                if slot_value is not None:
                                    slot_names.append(slot_value)
                            elif _closed_world_is_node(
                                body_value,
                                (_TupleExpr, _ListExpr),
                            ):
                                for slot_elem in _py_ast_field_value(
                                    body_value,
                                    "elems",
                                    (),
                                ):
                                    if _closed_world_is_node(slot_elem, _StrLit):
                                        slot_value = _export_literal_value_or_none(
                                            slot_elem
                                        )
                                        if slot_value is not None:
                                            slot_names.append(slot_value)
                            for slot_name in slot_names:
                                slot_name = mangle_private_name(stmt_name, slot_name)
                                if (
                                    slot_name not in ("__dict__", "__weakref__")
                                    and slot_name not in field_names
                                ):
                                    field_names.append(slot_name)
                        if class_uses_declared_fields and _closed_world_is_node(
                            target, _Name
                        ):
                            target_ident = mangle_private_name(
                                stmt_name, _py_ast_field_value(target, "ident", ""),
                            )
                            body_ann = _export_annotation_or_none(body_stmt)
                            if class_is_dataclass and body_ann is None:
                                # Dataclasses only turn annotated class-body
                                # assignments into instance fields. Preserve
                                # unannotated constants as class attributes in
                                # the exported constructor/schema too.
                                continue
                            if not class_is_valueclass:
                                body_ann = assignment_storage_annotation(
                                    body_ann, body_value if body_value is not None else body_stmt.value,
                                    body_stmt.has_value,
                                )
                            if target_ident not in field_names:
                                field_names.append(target_ident)
                            declared_field_def = {
                                "name": target_ident,
                                "annotation": body_ann,
                                "default": body_value,
                                "has_default": body_value is not None,
                            }
                            field_defs.append(declared_field_def)
                            init_field_defs.append(declared_field_def)
                    continue

                if not _closed_world_is_node(body_stmt, _FuncDef):
                    continue

                body_stmt_name = str(_py_ast_field_value(body_stmt, "name", ""))
                body_stmt_args = _py_ast_field_value(body_stmt, "args", ())
                body_stmt_body = _py_ast_field_value(body_stmt, "body", ())
                body_stmt_decorators = _py_ast_field_value(body_stmt, "decorators", ())

                if body_stmt_args and _py_ast_field_value(body_stmt_args[0], "name", "") == "self":
                    init_param_anns = {}
                    for arg in body_stmt_args:
                        arg_name = _py_ast_field_value(arg, "name", "")
                        if arg_name in ("", "self", "cls"):
                            continue
                        arg_ann = _export_annotation_or_none(arg)
                        if arg_ann is not None:
                            init_param_anns[arg_name] = arg_ann
                    for init_stmt in instance_field_assignment_statements(body_stmt_body):
                        init_value = _py_ast_field_value(init_stmt, "value", None)
                        inferred_ann = _export_annotation_or_none(init_stmt)
                        if (
                            inferred_ann is None
                            and _closed_world_is_node(init_value, _Name)
                            and _py_ast_field_value(init_value, "ident", "")
                            in init_param_anns
                        ):
                            inferred_ann = init_param_anns[
                                _py_ast_field_value(init_value, "ident", "")
                            ]
                        if (
                            inferred_ann is None
                            and _closed_world_is_node(init_value, _Call)
                        ):
                            init_callee = _py_ast_field_value(
                                init_value, "func", None
                            )
                            if _closed_world_is_node(init_callee, _Name):
                                init_class_name = _py_ast_field_value(
                                    init_callee, "ident", ""
                                )
                                if init_class_name in top_level_class_names:
                                    inferred_ann = _ClassType(
                                        init_class_name,
                                        mod_name,
                                        (),
                                        (),
                                    )
                        assignment_targets = _py_ast_field_value(init_stmt, "targets", ())
                        if not assignment_targets:
                            single_target = _py_ast_field_value(init_stmt, "target", None)
                            assignment_targets = (single_target,) if single_target is not None else ()
                        pending_targets = list(reversed(assignment_targets))
                        while pending_targets:
                            target = pending_targets.pop()
                            if _closed_world_is_node(
                                target,
                                (_TupleExpr, _ListExpr),
                            ):
                                pending_targets.extend(
                                    reversed(
                                        _py_ast_field_value(target, "elems", ())
                                    )
                                )
                                continue
                            target_obj = _py_ast_field_value(target, "obj", None)
                            if not (
                                _closed_world_is_node(target, _Attr)
                                and _closed_world_is_node(target_obj, _Name)
                                and _py_ast_field_value(target_obj, "ident", "")
                                == "self"
                            ):
                                continue
                            target_name = mangle_private_name(
                                stmt_name, _py_ast_field_value(target, "name", ""),
                            )
                            if target_name not in field_names:
                                field_names.append(target_name)
                            # Constructor ownership is independent of method
                            # source order and of whether exports can infer its
                            # RHS type. Unknown constructor types stay unknown.
                            if (
                                body_stmt_name != "__init__"
                                and target_name in constructor_field_names
                                and target_name not in declared_field_annotations
                            ):
                                continue
                            # An ordinary class annotation supplies a type only
                            # after an instance write discovers the field; it
                            # does not turn every class attribute into a slot.
                            field_ann = declared_field_annotations.get(target_name, inferred_ann)
                            if field_ann is None:
                                continue
                            field_def = {
                                "name": target_name,
                                "annotation": field_ann,
                                "default": None,
                                "has_default": False,
                            }
                            field_already_defined = False
                            for existing_field_def in field_defs:
                                if existing_field_def["name"] == target_name:
                                    field_already_defined = True
                                    break
                            if not field_already_defined:
                                field_defs.append(field_def)
                            # Runtime field metadata includes every method's
                            # writes. Synthetic dataclass constructor metadata
                            # includes only declarations and constructor fields.
                            if body_stmt_name == "__init__":
                                init_field_known = False
                                for init_field_def in init_field_defs:
                                    if init_field_def["name"] == target_name:
                                        init_field_known = True
                                        break
                                if not init_field_known:
                                    init_field_defs.append(field_def)

                kind = "instance"
                for dec in body_stmt_decorators:
                    if _closed_world_is_node(dec, _Name):
                        dec_ident = _py_ast_field_value(dec, "ident", "")
                        if dec_ident == "staticmethod":
                            kind = "static"
                        elif dec_ident == "classmethod":
                            kind = "classmethod"
                        elif dec_ident == "property":
                            kind = "property_getter"
                methods.append(
                    {
                        "name": body_stmt_name,
                        "symbol": _export_method_symbol(
                            mod_name,
                            stmt_name,
                            body_stmt_name,
                            top_level_func_names,
                        ),
                        "kind": kind,
                        "return_ty": _export_return_type(
                            _export_return_ty_or_none(body_stmt), raw_pointer_names
                        ),
                        "returns_none": _export_returns_none(
                            _export_return_ty_or_none(body_stmt)
                        ),
                        "param_types": _export_param_types(body_stmt_args, raw_pointer_names),
                        "call_sig": _export_call_sig(
                            body_stmt_args,
                            mod_name,
                            top_level_func_names,
                            raw_pointer_names,
                            str_default_cells,
                        ),
                        "is_async": bool(
                            _py_ast_field_value(body_stmt, "is_async", False)
                        ),
                        "has_return_annotation": bool(_py_ast_field_value(body_stmt, "has_return_annotation", False)),
                        "manual_pointer_abi": manual_pointer_abi,
                        "manual_pointer_abi_symbol": verified_c_abi_export_symbol(ast_mod, body_stmt) if manual_pointer_abi else "",
                        "box_int_abi": (
                            module_box_int_abi
                            if class_is_valueclass
                            else (
                                (module_box_int_abi or _export_func_has_python_int_signature(body_stmt))
                                and not freestanding_int_abi
                                and not verified_c_abi_export_symbol(ast_mod, body_stmt)
                            )
                        ),
                    }
                )

            # Match local field inference before publishing or inheriting the
            # schema. A cleanup None and an installed value share object
            # storage; neither source order nor the first typed write proves
            # a permanent concrete field type. Copy inherited definitions so
            # a subclass's nullable write cannot change its base's schema.
            if not class_is_valueclass:
                for field_index, field_def in enumerate(field_defs):
                    field_name = field_def["name"]
                    if (
                        field_name in constructor_none_fields
                        and field_name in constructor_value_fields
                    ):
                        nullable_field_def = dict(field_def)
                        nullable_field_def["annotation"] = _DynType(name="dyn")
                        field_defs[field_index] = nullable_field_def
            class_field_defs[stmt_name] = tuple(field_defs)
            class_init_field_defs[stmt_name] = tuple(init_field_defs)
            class_field_names[stmt_name] = tuple(field_names)
            init_method_exists = False
            for method in methods:
                if method["name"] == "__init__":
                    init_method_exists = True
                    break
            if class_is_dataclass and init_field_defs and not init_method_exists:
                init_sig = [
                    {
                        "name": "self",
                        "kind": "pos",
                        "annotation": None,
                        "default": None,
                        "has_default": False,
                    }
                ]
                init_param_types = [("dyn",)]
                for field in init_field_defs:
                    field_ann = field.get("annotation")
                    init_sig.append(
                        {
                            "name": field["name"],
                            "kind": "pos",
                            "annotation": (
                                encode_type(field_ann)
                                if field_ann is not None
                                else None
                            ),
                            "default": export_dataclass_factory_default(field["default"], dataclasses_field_binding_names(ast_mod)),
                            "has_default": field["has_default"],
                        }
                    )
                    field_default = field["default"]
                    default_function = _py_ast_field_value(field_default, "func", None)
                    is_factory_default = (
                        _py_ast_field_value(default_function, "ident", "") == "__pcc_dataclass_factory_default__"
                        and _py_ast_field_value(_py_ast_field_value(field_default, "span", None), "file", "") == "<pcc-dataclass-factory>"
                    )
                    if field_default is not None and not _closed_world_is_node(
                        field_default, (_IntLit, _FloatLit, _StrLit, _BoolLit, _NoneLit),
                    ) and not is_factory_default:
                        # Imported calls use the published constructor's
                        # captured signature, not a re-evaluated default AST.
                        init_sig[-1]["default_native_global"] = {
                            "owning_module": mod_name, "name": stmt_name,
                            "attrs": (field["name"],),
                        }
                    init_factory = _export_default_factory_name(field["default"])
                    if init_factory is not None:
                        init_sig[-1]["default_factory"] = init_factory
                        init_sig[-1]["has_default"] = True
                    init_param_types.append(
                        encode_type(field_ann) if field_ann is not None else ("dyn",)
                    )
                methods.append(
                    {
                        "name": "__init__",
                        "symbol": _export_method_symbol(
                            mod_name,
                            stmt_name,
                            "__init__",
                            top_level_func_names,
                        ),
                        "kind": "instance",
                        "return_ty": ("none",),
                        "param_types": tuple(init_param_types),
                        "call_sig": tuple(init_sig),
                        "box_int_abi": module_box_int_abi if class_is_valueclass else not freestanding_int_abi,
                    }
                )

            if (
                mod_name == "pcc.frontends.python.codegen.layer1"
                and stmt_name == "L1CodeGen"
            ):
                for host_attr_name in L1_CODEGEN_HOST_ATTRS:
                    if host_attr_name not in field_names:
                        field_names.append(host_attr_name)

            if mod_name == "pcc.frontends.python.py_ast":
                override_names = _PY_AST_FIELD_NAME_OVERRIDES.get(str(stmt_name))
                if override_names is not None and tuple(field_names) != tuple(
                    override_names
                ):
                    field_names = list(override_names)
                    field_defs = []
                    for override_name in override_names:
                        field_defs.append(
                            {
                                "name": override_name,
                                "annotation": None,
                                "default": None,
                                "has_default": False,
                            }
                        )

            field_types_table = []
            for field_def in field_defs:
                ann = field_def.get("annotation")
                if ann is not None:
                    field_types_table.append(
                        (
                            field_def["name"],
                            encode_type(ann),
                        )
                    )
            if mod_name == "pcc.frontends.python.py_ast":
                field_types_table = []
                for field_name in field_names:
                    field_type_text = _py_ast_field_type_override(
                        str(stmt_name),
                        field_name,
                    )
                    if field_type_text is None:
                        continue
                    field_ty = _normalise_export_annotation_text(field_type_text)
                    if field_ty is not None:
                        field_types_table.append(
                            (
                                field_name,
                                encode_type(field_ty),
                            )
                        )
            base_names = []
            for base in stmt_bases:
                base_ident = _py_ast_field_value(base, "ident", "")
                if _closed_world_is_node(base, _Name) and base_ident != "object":
                    base_names.append(base_ident)
                elif _closed_world_is_node(base, _Attr):
                    qualified_base = qualified_class_base_name(base, ast_body, stmt, mod_name, src)
                    if qualified_base:
                        base_names.append(qualified_base)
            if mod_name == "pcc.frontends.python.py_ast":
                override_bases = _PY_AST_BASE_NAME_OVERRIDES.get(str(stmt_name))
                if override_bases is not None and tuple(base_names) != tuple(
                    override_bases
                ):
                    base_names = list(override_bases)
            dynamic_field_layout = False
            for key, value in _py_ast_field_value(stmt, "keywords", ()):
                if key == "metaclass":
                    dynamic_field_layout = True
            exports[stmt_name] = {
                "kind": "class",
                "owning_module": mod_name,
                "export_name": stmt_name,
                "class_name": stmt_name,
                "qualified_name": f"{mod_name}.{stmt_name}",
                "base_names": tuple(base_names),
                "field_names": tuple(field_names),
                "field_types": tuple(field_types_table),
                "dynamic_field_layout": dynamic_field_layout,
                "methods": tuple(methods),
                "valueclass": class_is_valueclass,
                "box_int_abi": module_box_int_abi,
            }
        # Only verified module imports can qualify an annotation prefix.
        # Do this before sharing export dictionaries with re-exporters: their
        # identically spelled aliases belong to a different lexical owner.
        annotation_modules = annotation_module_bindings(ast_mod)
        if annotation_modules:
            for exported in exports.values():
                _qualify_export_annotation_refs(exported, annotation_modules)
        _expand_local_valueclass_export_refs(mod_name, exports)
        native_exports[mod_name] = exports
        _profile_end(profile, "build_closed_world_context_module", module_t, mod_name)
        module_index += 1

    if merge_exports:
        _merge_closed_world_reexports(
            parsed_modules,
            module_names,
            src_paths,
            native_exports,
        )
        # Qualified aliases and re-exports use the same owning-module identity
        # as class lowering, before inherited layouts traverse the graph.
        for owner_module, owner_exports in native_exports.items():
            for class_info in owner_exports.values():
                if not isinstance(class_info, dict) or class_info.get("kind") != "class":
                    continue
                canonical_bases = []
                for base_name in class_info.get("base_names", ()):
                    resolved_base = resolve_class_base_export(native_exports, owner_module, base_name)
                    if "." in base_name and resolved_base is not None:
                        base_module, base_info = resolved_base
                        base_name = base_info.get("owning_module", base_module) + "." + base_info["class_name"]
                    canonical_bases.append(base_name)
                class_info["base_names"] = tuple(canonical_bases)
        _flatten_closed_world_class_export_fields(native_exports)
        _repair_closed_world_default_global_owners(native_exports)
        _merge_l1_mixin_stack_methods(native_exports)
        _merge_l1_codegen_methods(native_exports)
        # Imported annotation shells cannot be expanded until re-export
        # bindings have converged.  The defining module's first pass handles
        # local classes; this second idempotent pass resolves provider methods
        # that annotate returns/arguments with a valueclass imported from a
        # sibling module while preserving that class's real owning module.
        for visible_module_name, visible_exports in native_exports.items():
            _expand_local_valueclass_export_refs(
                visible_module_name,
                visible_exports,
            )

    # Effect discovery must see the final public binding graph.  In
    # particular, ``from package import parked`` resolves only after package
    # ``__init__`` re-exports have converged.  Publishing the fixed point
    # before that merge silently gives the caller a normal ABI while the leaf
    # has a generator ABI.
    annotate_closed_world_vthread_effects(
        parsed_modules,
        module_names,
        native_exports,
    )

    _mark_closed_world_function_object_exports(
        parsed_modules,
        module_names,
        src_paths,
        native_exports,
    )

    derived_class_map = _closed_world_derived_class_map(native_exports)
    return parsed_modules, native_exports, derived_class_map


def _closed_world_derived_class_map(native_exports):
    base_to_derived = {}
    for derived_mod, exports in native_exports.items():
        for class_name, info in exports.items():
            if not isinstance(info, dict) or info.get("kind") != "class":
                continue
            for base_name in info.get("base_names", ()):
                base_to_derived.setdefault(base_name, []).append(
                    (derived_mod, class_name)
                )
    derived_class_map = {}
    for base_name, derived_list in base_to_derived.items():
        if len(derived_list) == 1:
            derived_class_map[base_name] = derived_list[0]
    return derived_class_map


def _merge_l1_mixin_stack_methods(native_exports):
    stack_exports = native_exports.get("pcc.frontends.python.codegen.layer1_mixins")
    if not stack_exports:
        return
    stack_info = stack_exports.get("L1CodeGenMixinStack")
    if not isinstance(stack_info, dict) or stack_info.get("kind") != "class":
        return
    methods = []
    seen = {}
    for method in stack_info.get("methods", ()):
        name = method.get("name") if isinstance(method, dict) else None
        if name is not None:
            seen[name] = True
        methods.append(method)
    for base_name in stack_info.get("base_names", ()):
        for _module_name, exports in native_exports.items():
            base_info = exports.get(base_name)
            if not isinstance(base_info, dict) or base_info.get("kind") != "class":
                continue
            for method in base_info.get("methods", ()):
                name = method.get("name") if isinstance(method, dict) else None
                if name is not None and name in seen:
                    continue
                if name is not None:
                    seen[name] = True
                methods.append(method)
            break
    stack_info["methods"] = tuple(methods)


def _merge_l1_codegen_methods(native_exports):
    layer1_exports = native_exports.get("pcc.frontends.python.codegen.layer1")
    if not layer1_exports:
        return
    l1_info = layer1_exports.get("L1CodeGen")
    if not isinstance(l1_info, dict) or l1_info.get("kind") != "class":
        return
    methods = []
    seen = {}
    for method in l1_info.get("methods", ()):
        name = method.get("name") if isinstance(method, dict) else None
        if name is not None:
            seen[name] = True
        methods.append(method)
    for base_name in l1_info.get("base_names", ()):
        for _module_name, exports in native_exports.items():
            base_info = exports.get(base_name)
            if not isinstance(base_info, dict) or base_info.get("kind") != "class":
                continue
            for method in base_info.get("methods", ()):
                name = method.get("name") if isinstance(method, dict) else None
                if name is not None and name in seen:
                    continue
                if name is not None:
                    seen[name] = True
                methods.append(method)
            break
    l1_info["methods"] = tuple(methods)


def _contextual_host_export_surface(native_exports):
    """Return the compact schema needed to compile L1CodeGen mixin methods."""

    host_module = "pcc.frontends.python.codegen.layer1"
    host_name = "L1CodeGen"
    host_exports = native_exports.get(host_module)
    class_exports = native_exports.get("pcc.frontends.python.codegen.class_gen")
    if not isinstance(host_exports, dict) or not isinstance(class_exports, dict):
        raise ValueError("contextual codegen host exports are unavailable")
    host_info = host_exports.get(host_name)
    class_info = class_exports.get("ClassInfo")
    lowering_info = class_exports.get("ClassLowering")
    for info in (host_info, class_info, lowering_info):
        if not isinstance(info, dict) or info.get("kind") != "class":
            raise ValueError("contextual codegen host class export is unavailable")
    host_surface = {}
    host_surface[host_name] = host_info
    class_surface = {}
    class_surface["ClassInfo"] = class_info
    class_surface["ClassLowering"] = lowering_info
    out = {}
    out[host_module] = host_surface
    out["pcc.frontends.python.codegen.class_gen"] = class_surface
    return out


def _contextual_host_params_for_module(ast_mod, module_name: str):
    """Return helper-function host params that should type as L1CodeGen.

    This is deliberately narrow. It only applies inside codegen modules and
    only to top-level helpers whose first parameter is named ``host``. That
    gives future layer1 helper extractions an explicit host-context path
    without changing ordinary user/program inference.
    """
    module_name = str(module_name or "")
    if not module_name.startswith("pcc.frontends.python.codegen."):
        return None
    out = {}
    from pcc.frontends.python.py_ast import FuncDef as _FuncDef

    for stmt in _py_ast_field_value(ast_mod, "body", ()) or ():
        if not _closed_world_is_node(stmt, _FuncDef):
            continue
        args = _py_ast_field_value(stmt, "args", ()) or ()
        if not args:
            continue
        first = args[0]
        if _py_ast_field_value(first, "name", "") == "host":
            out[_py_ast_field_value(stmt, "name", "")] = ("host",)
    if not out:
        return None
    return out


def count_py_cpy_fallback_calls(ir_text: str) -> int:
    # Preserve the splitlines per-line predicate without a full line list.
    # Marker-bearing lines are sparse in native IR; inspect each at most once.
    count = 0
    size = len(ir_text)
    position = 0
    line_breaks = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    while position < size:
        marker = ir_text.find("@py_cpy_", position)
        if marker < 0:
            break
        line_start = marker
        while line_start > 0 and ir_text[line_start - 1] not in line_breaks:
            line_start -= 1
        line_end = marker + len("@py_cpy_")
        while line_end < size and ir_text[line_end] not in line_breaks:
            line_end += 1
        if ir_text.find("call ", line_start, line_end) >= 0:
            count += 1
        position = line_end + 1
    return count


def _copy_native_module_exports(exports):
    out = {}
    if exports is None:
        return out
    for key in exports:
        out[key] = exports[key]
    return out


def _module_uses_default_native_exports(module_name: str) -> bool:
    return _default_native_module_exports(module_name) is not None


PROBE_POLICY_STANDALONE = "standalone"


def compile_contextual_per_module_fallback_counts(
    src_paths,
    module_names,
    contextual_modules,
    *,
    ir_scaffold_mode: str,
    strict_no_libpython: bool = False,
    emit_ir_dir: Optional[str] = None,
    entry_module: Optional[str] = None,
):
    """Return ``py_cpy_*`` call counts for modules under closed-world context.

    This is the diagnostic counterpart to ``compile_python_multi``. It
    compiles selected modules one at a time with the same export table as the
    full closed-world compile. Contextual mixin Modules additionally receive
    their real host self-type; ordinary closed-world Modules receive sibling
    schemas without any host binding. Raw single-file probing lacks both.
    """
    from pcc.frontends.python.type_infer import infer_module as _infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen as _L1CodeGen

    wanted = []
    for mod_name in contextual_modules:
        wanted.append(mod_name)
    parsed_modules, native_exports, derived_class_map = build_closed_world_context(
        src_paths, module_names, profile=None
    )
    out = {}
    for ast_mod, mod_name in zip(parsed_modules, module_names):
        should_compile = False
        for wanted_name in wanted:
            if mod_name == wanted_name:
                should_compile = True
                break
        if not should_compile:
            continue
        try:
            external_exports = {}
            for k, v in native_exports.items():
                if k != mod_name:
                    external_exports[k] = v
            typed_mod = _infer_module(
                ast_mod,
                external_exports=external_exports,
                derived_class_map=derived_class_map,
                contextual_host_params=_contextual_host_params_for_module(
                    ast_mod,
                    mod_name,
                ),
            )
            codegen = _L1CodeGen(
                typed_mod,
                emit_cpy_main_exitcode=False,
                ir_scaffold_mode=ir_scaffold_mode,
            )
            codegen._strict_no_libpython = strict_no_libpython
            codegen._prefer_native_callable_values = strict_no_libpython
            for source_path, source_module_name in zip(src_paths, module_names):
                if source_module_name == mod_name:
                    codegen._module_source_path = os.path.abspath(source_path)
                    break
            if entry_module is not None:
                codegen._skip_program_main = mod_name != entry_module
            if _module_uses_default_native_exports(mod_name):
                codegen_exports = _copy_native_module_exports(
                    codegen._native_module_exports
                )
            else:
                codegen_exports = {}
            for k, v in native_exports.items():
                if k != mod_name:
                    codegen_exports[k] = v
            codegen._native_module_exports = codegen_exports
            codegen._native_function_object_exports = (
                _closed_world_function_object_exports(native_exports, mod_name)
            )
            codegen._native_boxed_int_functions = _closed_world_boxed_int_functions(
                native_exports, mod_name,
            )
            ir_text = str(codegen.generate(typed_mod))
            # The serialized text is independent of the completed frontend.
            codegen = None
            out[mod_name] = count_py_cpy_fallback_calls(ir_text)
            if emit_ir_dir is not None:
                # ponytail: caller must pre-create emit_ir_dir. os.makedirs has
                # no no-libpython lowering, and this debug-only IR-dump path
                # would otherwise reintroduce a py_cpy_* fallback into
                # pipeline.py's own per-module ratchet (this function is a
                # test/diagnostic helper — no production caller passes
                # emit_ir_dir). Native os.makedirs is tracked separately.
                ir_name = mod_name.replace(".", "_") + ".ll"
                with open(
                    os.path.join(emit_ir_dir, ir_name),
                    "w",
                    encoding="utf-8",
                ) as f:
                    f.write(ir_text)
        except Exception as exc:
            out[mod_name] = -1
            detail = type(exc).__name__ + ": " + (str(exc) or type(exc).__name__)
            sys.stderr.write("pcc contextual failure " + mod_name + ": " + detail + "\n")
            if emit_ir_dir is not None:
                error_name = mod_name.replace(".", "_") + ".error.txt"
                with open(os.path.join(emit_ir_dir, error_name), "w", encoding="utf-8") as stream:
                    stream.write(detail + "\n")
        finally:
            # Do not overlap the next module with obsolete frontend/text owners.
            codegen = None
            ir_text = ""
    return out
