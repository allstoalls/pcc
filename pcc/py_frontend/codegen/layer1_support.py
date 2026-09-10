"""Pure support helpers and static metadata for layer1 codegen."""

from __future__ import annotations

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
    ByteArrayType,
    BytesType,
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
    SetType,
    MemoryViewType,
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
    ValueArrayType,
    ValueClassType,
    While,
    With,
)

_Import = Import
_ImportFrom = ImportFrom


def _import_names_from_stmt(stmt):
    """Return ``((mod_name, as_name), ...)`` preserving ``as_name=None``
    when the user wrote ``import X`` without an ``as`` clause. Callers
    use ``as_name is None`` to distinguish the bare-import case (which
    has special top-level binding semantics for dotted names like
    ``import urllib.parse``) from explicit aliases. The previous
    fallback that defaulted ``as_name`` to ``mod_name`` broke the
    ``import urllib.parse`` → bind ``urllib`` path because the
    downstream check then saw ``as_name='urllib.parse'`` and emitted
    a binding under the leaf name instead of the top-level package.
    """
    pairs = []
    raw_names = getattr(stmt, "names", ())
    if not isinstance(raw_names, (tuple, list)):
        return tuple(pairs)
    for raw_name in raw_names:
        if isinstance(raw_name, (tuple, list)) and len(raw_name) >= 2:
            pairs.append((raw_name[0], raw_name[1]))
        elif isinstance(raw_name, (tuple, list)) and len(raw_name) >= 1:
            pairs.append((raw_name[0], None))
        elif hasattr(raw_name, "asname") or hasattr(raw_name, "name"):
            pairs.append(
                (
                    getattr(raw_name, "name", None),
                    getattr(raw_name, "asname", None),
                )
            )
        elif isinstance(raw_name, str):
            pairs.append((raw_name, None))
    return tuple(pairs)


def _import_from_module_or_empty(stmt) -> str:
    try:
        module = stmt.module
    except AttributeError:
        return ""
    return module or ""


def _import_from_level_or_zero(stmt) -> int:
    try:
        level = stmt.level
    except AttributeError:
        return 0
    return level or 0


def _is_import_stmt(stmt):
    if type(stmt).__name__ in {"Global", "Nonlocal"}:
        return False
    if isinstance(stmt, (Global, Nonlocal)):
        return False
    if type(stmt).__name__ in {"Import", "ImportFrom"}:
        return True
    if isinstance(stmt, (_Import, _ImportFrom)):
        return True
    raw_names = getattr(stmt, "names", ())
    if not raw_names:
        return False
    if not isinstance(raw_names, (tuple, list)):
        return False
    return all(
        isinstance(item, (tuple, list, str)) or hasattr(item, "name")
        for item in raw_names
    )


def _is_import_from_stmt(stmt):
    if type(stmt).__name__ == "ImportFrom":
        return True
    if isinstance(stmt, (_ImportFrom,)):
        return True
    if not _is_import_stmt(stmt):
        return False
    names = getattr(stmt, "names", ())
    return (
        hasattr(stmt, "module")
        and hasattr(stmt, "names")
        and isinstance(names, (tuple, list))
        and bool(names)
    )


def _export_arg(
    name: str,
    annotation=None,
    *,
    kind: str = "pos",
    has_default: bool = False,
):
    return {
        "name": name,
        "kind": kind,
        "annotation": annotation,
        "default": None,
        "has_default": has_default,
    }


def _function_export(return_ty, param_types, call_sig):
    return {
        "kind": "function",
        "return_ty": return_ty,
        "param_types": tuple(param_types),
        "call_sig": tuple(call_sig),
        "box_int_abi": False,
    }


def _class_export(name: str, field_names=(), methods=()):
    return {
        "kind": "class",
        "class_name": name,
        "base_names": (),
        "field_names": tuple(field_names),
        "field_types": (),
        "methods": tuple(methods),
        "box_int_abi": False,
    }


def _module_global_export(value_ty):
    return {
        "kind": "module_global",
        "value_ty": value_ty,
    }


def _str_constant_export(value: str):
    return {
        "kind": "constant",
        "value_kind": "str",
        "value": value,
    }


def _string_tuple_global_export():
    return _module_global_export(("tuple", (("str",),)))


def _dict_str_dyn_global_export():
    return _module_global_export(("dict", ("str",), ("dyn",)))


def _build_py_ast_static_class_fields():
    return {
        "Type": ("name",),
        "IntType": ("name", "width", "signed"),
        "FloatType": ("name", "width"),
        "ComplexType": ("name",),
        "BoolType": ("name",),
        "NoneType": ("name",),
        "StrType": ("name",),
        "BytesType": ("name",),
        "ByteArrayType": ("name",),
        "MemoryViewType": ("name",),
        "ListType": ("name", "elem"),
        "SetType": ("name", "elem"),
        "ValueArrayType": ("name", "elem", "length"),
        "DictType": ("name", "key", "value"),
        "TupleType": ("name", "elems"),
        "FuncType": ("name", "params", "ret"),
        "ClassType": (
            "name",
            "module",
            "fields",
            "bases",
            "properties",
            "valueclass",
        ),
        "ValueClassType": (
            "name",
            "module",
            "fields",
            "bases",
            "properties",
            "valueclass",
            "flattened",
            "nullable_fields",
        ),
        "DynType": ("name",),
        "SourceSpan": ("file", "line", "col", "end_line", "end_col"),
        "Expr": ("span", "ty"),
        "IntLit": ("span", "ty", "value"),
        "FloatLit": ("span", "ty", "value"),
        "ComplexLit": ("span", "ty", "real", "imag"),
        "BoolLit": ("span", "ty", "value"),
        "NoneLit": ("span", "ty"),
        "StrLit": ("span", "ty", "value"),
        "BytesLit": ("span", "ty", "value"),
        "Name": ("span", "ty", "ident"),
        "BinOp": ("span", "ty", "op", "lhs", "rhs"),
        "UnaryOp": ("span", "ty", "op", "operand"),
        "Compare": ("span", "ty", "op", "lhs", "rhs"),
        "BoolExpr": ("span", "ty", "op", "left", "right"),
        "Call": ("span", "ty", "func", "args", "kwargs", "operand_order"),
        "Attr": ("span", "ty", "obj", "name"),
        "Subscript": ("span", "ty", "obj", "idx"),
        "Slice": ("span", "ty", "lo", "hi", "step"),
        "ListExpr": ("span", "ty", "elems"),
        "DictExpr": ("span", "ty", "pairs"),
        "TupleExpr": ("span", "ty", "elems"),
        "IfExpr": ("span", "ty", "cond", "then_e", "else_e"),
        "Lambda": ("span", "ty", "params", "body"),
        "Assign": ("span", "targets", "value", "annotation"),
        "AugAssign": ("span", "target", "op", "value"),
        "ExprStmt": ("span", "expr"),
        "If": ("span", "cond", "body", "else_body"),
        "While": ("span", "cond", "body", "else_body"),
        "For": ("span", "target", "iter", "body", "else_body", "is_async"),
        "Return": ("span", "value"),
        "Pass": ("span",),
        "Break": ("span",),
        "Continue": ("span",),
        "Raise": ("span", "exc", "cause"),
        "Try": ("span", "body", "handlers", "else_body", "finally_body"),
        "ExceptHandler": ("exc_type", "name", "body", "span"),
        "With": ("span", "items", "body", "is_async"),
        "Import": ("span", "names"),
        "ImportFrom": ("span", "module", "names", "level"),
        "Global": ("span", "names"),
        "Nonlocal": ("span", "names"),
        "Delete": ("span", "targets"),
        "Arg": ("name", "annotation", "default", "kind", "has_default"),
        "FuncDef": (
            "span",
            "name",
            "args",
            "return_ty",
            "body",
            "decorators",
            "is_method",
            "is_async",
        ),
        "ClassDef": ("span", "name", "bases", "keywords", "body", "decorators"),
        "Stmt": ("span",),
        "Module": ("name", "body", "docstring"),
    }


_PY_AST_STATIC_CLASS_FIELDS = _build_py_ast_static_class_fields()


def _populate_static_native_exports_0(out):
    out["pcc.py_frontend.py_ast"] = {
        name: _class_export(name, fields)
        for name, fields in _PY_AST_STATIC_CLASS_FIELDS.items()
    }
    out["pcc.py_frontend.export_meta"] = {
        "encode_type": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("ty", ("dyn",)),),
        ),
        "decode_type": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("desc"),),
        ),
    }
    out["pcc.py_frontend.py_ast_contract"] = {
        "PY_AST_FIELD_NAME_OVERRIDES": _dict_str_dyn_global_export(),
    }
    # ``cli_bootstrap.py`` imports the shared parser choices from this module.
    # Keep the standalone fallback probe on the same static surface as the
    # closed-world pcc1 build.
    out["pcc.cli_contract"] = {
        "BACKEND_CHOICES": _string_tuple_global_export(),
        "PYTHON_LIBPYTHON_CHOICES": _string_tuple_global_export(),
        "IR_SCAFFOLD_CHOICES": _string_tuple_global_export(),
        "DIAGNOSTIC_FORMAT_CHOICES": _string_tuple_global_export(),
        "DEFAULT_EMIT_LL": _str_constant_export("__PCC_DEFAULT_LL__"),
        "DEFAULT_PUBLIC_BACKEND": _str_constant_export("self"),
        "cli_input_path": _function_export(
            ("str",),
            (("list", ("str",)),),
            (_export_arg("argv", ("list", ("str",))),),
        ),
    }
    # ``cli_bootstrap.py`` consumes this self-host-safe package contract in
    # standalone as well as closed-world builds.  Keep the imported constants
    # and helpers native in the raw per-module probe; otherwise every call is
    # inferred as ``Dyn`` and silently reintroduces ``py_cpy_*`` despite the
    # same two modules compiling cleanly together.
    out["pcc.package_schema"] = {
        "PACKAGE_MANIFEST_SCHEMA": _str_constant_export("pcc.package-manifest.v1"),
        "PACKAGE_MANIFEST_SCHEMA_VERSION": {
            "kind": "constant",
            "value_kind": "int",
            "value": 1,
        },
        "PCC_CAPI_HEADERS": _string_tuple_global_export(),
        "campaign_profile": _function_export(
            ("dyn",),
            (("str",),),
            (_export_arg("name", ("str",)),),
        ),
        "capability_profile": _function_export(
            ("dict", ("str",), ("dyn",)),
            (("str",), ("bool",), ("bool",), ("bool",)),
            (
                _export_arg("abi_mode", ("str",)),
                _export_arg("has_artifact_scan", ("bool",)),
                _export_arg("links_libpython", ("bool",)),
                _export_arg("uses_cpython_extension_abi", ("bool",)),
            ),
        ),
        "pcc_native_extension_suffix": _function_export(
            ("str",),
            (("str",),),
            (_export_arg("platform_tag", ("str",)),),
        ),
        "pcc_native_wheel_tag": _function_export(
            ("str",),
            (("str",),),
            (_export_arg("platform_tag", ("str",)),),
        ),
        "wheel_tag_fields": _function_export(
            ("list", ("str",)),
            (("str",),),
            (_export_arg("path", ("str",)),),
        ),
    }
    out["pcc.package_metadata_paths"] = {
        "package_metadata_paths": _function_export(
            ("list", ("str",)),
            (("str",),),
            (_export_arg("root", ("str",)),),
        ),
        "package_metadata_member_paths": _function_export(
            ("list", ("str",)),
            (("list", ("str",)),),
            (_export_arg("names", ("list", ("str",))),),
        ),
    }
    # Package environment selection is shared by the host CLI, the compiled
    # bootstrap CLI, and frontend import discovery.  Standalone module probes
    # need the same signatures as the closed-world build; treating these
    # helpers as dynamic silently adds libpython calls to both cli_bootstrap
    # and pipeline.
    out["pcc.package_environment"] = {
        "package_environment_fingerprint": _function_export(("str",), (), ()),
        "apply_locked_environment_resource_defaults": _function_export(
            ("list", ("str",)),
            (),
            (),
        ),
        "default_package_cache": _function_export(
            ("str",),
            (("dyn",),),
            (_export_arg("environ", ("dyn",), has_default=True),),
        ),
        "default_package_site": _function_export(
            ("str",),
            (("dyn",), ("dyn",)),
            (
                _export_arg("environ", ("dyn",), has_default=True),
                _export_arg("target_triple", ("dyn",), has_default=True),
            ),
        ),
        "environment_info_json": _function_export(
            ("str",),
            (("dyn",),),
            (_export_arg("environ", ("dyn",), has_default=True),),
        ),
        "environment_info_text": _function_export(
            ("str",),
            (("dyn",),),
            (_export_arg("environ", ("dyn",), has_default=True),),
        ),
        "package_site_roots": _function_export(
            ("list", ("str",)),
            (("dyn",), ("dyn",)),
            (
                _export_arg("environ", ("dyn",), has_default=True),
                _export_arg("target_triple", ("dyn",), has_default=True),
            ),
        ),
    }
    out["pcc.py_frontend.pipeline_paths"] = {
        "resolve_module_src": _function_export(
            ("dyn",),
            (("str",), ("str",)),
            (_export_arg("root_dir", ("str",)), _export_arg("dotted_name", ("str",))),
        ),
    }
    # The strict AArch64 self path is linked into pcc1 and called in-process;
    # these three signatures keep pipeline.py's independent diagnostic probe
    # on the same native edge as the real closed-world compiler.


def _populate_static_native_exports_1(out):
    out["pcc.backend.self_backend_aarch64_darwin"] = {
        "emit_aarch64_darwin_asm": _function_export(
            ("str",),
            (("str",), ("bool",)),
            (
                _export_arg("ir_text", ("str",)),
                _export_arg("optimize", ("bool",)),
            ),
        ),
    }
    out["pcc.backend.self_backend_parse"] = {
        "parse_self_backend_target_triple": _function_export(
            ("str",),
            (("str",),),
            (_export_arg("ir_text", ("str",)),),
        ),
    }
    # Self-backend lowering helpers are imported by independently compiled
    # emitter modules as well as the closed-world compiler. Keep their raw
    # per-module calls native so correctness hardening does not grow the
    # libpython fallback surface.
    out["pcc.backend.self_backend_ir"] = {
        "_align_to": _function_export(
            ("int",),
            (("int",), ("int",)),
            (
                _export_arg("value", ("int",)),
                _export_arg("alignment", ("int",)),
            ),
        ),
        "_dot_numeric_text_key_id": _function_export(
            ("int",),
            (("str",),),
            (_export_arg("text", ("str",)),),
        ),
        "dot_numeric_text_key_id": _function_export(
            ("int",),
            (("str",),),
            (_export_arg("text", ("str",)),),
        ),
        "text_key_names_equal": _function_export(
            ("bool",),
            (("str",), ("str",)),
            (
                _export_arg("left", ("str",)),
                _export_arg("right", ("str",)),
            ),
        ),
        "text_key_mapping_get": _function_export(
            ("dyn",),
            (("dyn",), ("str",)),
            (
                _export_arg("mapping", ("dyn",)),
                _export_arg("key", ("str",)),
            ),
        ),
        "parsed_function_value_slot": _function_export(
            ("dyn",),
            (("dyn",), ("str",)),
            (
                _export_arg("func", ("dyn",)),
                _export_arg("key", ("str",)),
            ),
        ),
    }
    out["pcc.backend.self_backend_target_match"] = {
        "is_aarch64_darwin_triple": _function_export(
            ("bool",),
            (("str",),),
            (_export_arg("triple", ("str",)),),
        ),
    }
    # ``class_gen._builtin_exception_tag_for_base_name`` calls the shared
    # exception-tag metadata helper; without a static export the standalone
    # per-module compile bridges it via py_cpy_import/getattr/call.
    out["pcc.py_frontend.codegen.builtin_exceptions"] = {
        "builtin_exc_tag_or_missing": _function_export(
            ("int",),
            (("str",),),
            (_export_arg("name", ("str",)),),
        ),
    }
    out["pcc.llvm_capi.ir"] = {
        "IRBuilder_current_instruction_count": _function_export(
            ("int",),
            (("dyn",),),
            (_export_arg("builder", ("dyn",)),),
        ),
        "IRBuilder_instruction_text_at": _function_export(
            ("str",),
            (("dyn",), ("int",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("index", ("int",)),
            ),
        ),
        "IRBuilder_emit_raw": _function_export(
            ("none",),
            (("dyn",), ("str",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("line", ("str",)),
            ),
        ),
        "IRBuilder_next_value": _function_export(
            ("dyn",),
            (("dyn",), ("str",), ("dyn",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("name", ("str",)),
                _export_arg("typ", ("dyn",)),
            ),
        ),
        "IRBuilder_can_inline_error_edge": _function_export(
            ("bool",),
            (("dyn",),),
            (_export_arg("builder", ("dyn",)),),
        ),
        "IRBuilder_try_inline_error_edge": _function_export(
            ("bool",),
            (("dyn",), ("dyn",), ("dyn",), ("int",), ("int",), ("int",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("condition", ("dyn",)),
                _export_arg("error_block", ("dyn",)),
                _export_arg("source_line", ("int",)),
                _export_arg("cleanup_plan_id", ("int",)),
                _export_arg("payload", ("int",), has_default=True),
            ),
        ),
        "IRBuilder_declare_inline_error_landing": _function_export(
            ("bool",),
            (("dyn",), ("dyn",), ("dyn",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("block", ("dyn",)),
                _export_arg("slot", ("dyn",)),
            ),
        ),
    }
    # ``pcc.parse.py_parse`` does ``from . import py_lex as pl`` and
    # invokes ``pl.Lexer(src, filename).tokenize()``. Without static
    # exports the standalone per-module compile resolves the
    # ``py_lex.Lexer`` constructor and ``Lexer.tokenize`` method via
    # ``py_cpy_import`` + ``py_cpy_getattr`` + ``py_cpy_call``, which
    # the off-mode fallback ratchet flags as residual libpython
    # surface.  Mirror the actual classes / functions in
    # ``pcc/parse/py_lex.py``.
    out["pcc.parse.py_lex"] = {
        "Token": _class_export(
            "Token",
            ("kind", "text", "line", "col"),
        ),
        "LexError": _class_export("LexError"),
        "Lexer": _class_export(
            "Lexer",
            (
                "src",
                "_src_len",
                "_debug_bootstrap",
                "filename",
                "pos",
                "line",
                "col",
                "_indent_stack",
                "_paren_depth",
                "_at_line_start",
            ),
            methods=(
                {
                    "name": "__init__",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("str",), ("str",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("src", ("str",)),
                        _export_arg(
                            "filename",
                            ("str",),
                            has_default=True,
                        ),
                    ),
                    "box_int_abi": False,
                },
                {
                    "name": "tokenize",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",),),
                    "call_sig": (_export_arg("self"),),
                    "box_int_abi": False,
                },
            ),
        ),
    }


def _populate_static_native_exports_2(out):
    out["pcc.parse.py_lift"] = {
        "parse_and_lift": _function_export(
            ("dyn",),
            (("str",), ("str",), ("str",)),
            (
                _export_arg("src", ("str",)),
                _export_arg("filename", ("str",)),
                _export_arg("module_name", ("str",)),
            ),
        ),
        # ``pipeline.py``'s ``_closed_world_shallow_lift_module`` and
        # related closed-world helpers reach into ``py_lift`` for the
        # lifter and its top-level entry points.  Static exports here
        # let the standalone per-module compile bind ``_Lifter`` /
        # ``lift_stmt`` / ``lift_expr`` natively instead of falling
        # through to ``py_cpy_import`` + ``py_cpy_getattr`` (counted
        # by the no-libpython fallback ratchet).
        "LiftError": _class_export("LiftError"),
        "_Lifter": _class_export("_Lifter"),
        "lift_module": _function_export(
            ("dyn",),
            (("dyn",), ("str",), ("str",)),
            (
                _export_arg("mod", ("dyn",)),
                _export_arg("filename", ("str",)),
                _export_arg("module_name", ("str",)),
            ),
        ),
        "lift_stmt": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("stmt", ("dyn",)),),
        ),
        "lift_expr": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("expr", ("dyn",)),),
        ),
        "_lift_type": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("node", ("dyn",)),),
        ),
    }


def _populate_static_native_exports_8(out):
    # ``pipeline.py``'s closed-world helpers
    # (``_closed_world_shallow_extract_top_level_assigns`` /
    # ``_closed_world_extract_native_table_for_module`` added in
    # 5a975332) `from ..parse.py_parse import _Assign as _PPAssign`
    # etc. inside their function bodies.  Without static entries here
    # the standalone per-module compile falls back to libpython for
    # each, regressing the ``test_on_mode_per_module_fallbacks_under_ratchet``
    # diagnostic ratchet.  These mirror the actual classes / function
    # in ``pcc/parse/py_parse.py``.
    out["pcc.parse.py_parse"] = dict(
        [
            ("ParseError", _class_export("ParseError")),
            (
                "parse",
                _function_export(
                    ("dyn",),
                    (("str",), ("str",)),
                    (
                        _export_arg("src", ("str",)),
                        _export_arg("filename", ("str",), has_default=True),
                    ),
                ),
            ),
        ]
        + [
            (name, _class_export(name, fields))
            for name, fields in (
                # All ``_<PascalCase>`` dataclass AST nodes defined at
                # top level in pcc/parse/py_parse.py, with their
                # ``dataclasses.fields()`` field-name tuples.
                # Field schemas let py_lift.py's standalone compile
                # type-check attribute access on these nodes (e.g.
                # ``node.elems``, ``node.lhs``) natively instead of
                # falling back to py_cpy_getattr.
                # Regenerate via:
                #   uv run python -c "import dataclasses, pcc.parse.py_parse as pp;
                #   [print(f'    ({n!r}, {tuple(f.name for f in dataclasses.fields(c))!r}),')
                #    for n in sorted(dir(pp))
                #    for c in [getattr(pp, n)]
                #    if isinstance(c, type) and dataclasses.is_dataclass(c)
                #    and len(n) > 1 and n[0] == '_' and n[1].isupper()]"
                ("_Assert", ("test", "msg", "line")),
                ("_Assign", ("target", "value", "annotation", "line")),
                ("_Attr", ("obj", "name", "line")),
                ("_AugAssign", ("target", "op", "value", "line")),
                ("_Await", ("value", "line")),
                ("_BinOp", ("op", "lhs", "rhs", "line")),
                ("_Bool", ("value", "line")),
                ("_BoolOp", ("op", "values", "line")),
                ("_Break", ("line",)),
                ("_Bytes", ("parts", "line")),
                ("_Call", ("func", "args", "line")),
                ("_ClassDef", ("name", "bases", "body", "decorators", "line")),
                ("_Comp", ("kind", "elt", "generators", "line")),
                ("_Compare", ("op", "lhs", "rhs", "line")),
                ("_ComplexNum", ("text", "line")),
                ("_Continue", ("line",)),
                ("_Del", ("targets", "line")),
                ("_Dict", ("keys", "values", "line")),
                ("_DictCompElt", ("key", "value", "line")),
                ("_Expr", ("expr", "line")),
                ("_FString", ("parts", "line")),
                ("_FStringExprParts", ("expr", "conversion", "spec")),
                ("_FStringFormat", ("expr", "conversion", "spec", "line")),
                ("_FStringText", ("text", "is_raw", "line")),
                ("_For", ("target", "iter", "body", "else_body", "line", "is_async")),
                (
                    "_FuncDef",
                    (
                        "name",
                        "params",
                        "body",
                        "line",
                        "decorators",
                        "returns",
                        "is_async",
                    ),
                ),
                ("_Global", ("names", "line")),
                ("_If", ("cond", "body", "else_body", "line")),
                ("_Import", ("names", "line")),
                ("_ImportFrom", ("module", "names", "level", "line")),
                ("_Lambda", ("params", "body", "line")),
                ("_List", ("elems", "line")),
                ("_MatchAs", ("pattern", "name", "line")),
                ("_Module", ("body",)),
                ("_Name", ("ident", "line")),
                ("_None", ("line",)),
                ("_Nonlocal", ("names", "line")),
                ("_Num", ("text", "line", "is_int")),
                ("_Pass", ("line",)),
                ("_Raise", ("exc", "cause", "line")),
                ("_Return", ("value", "line")),
                ("_Set", ("elems", "line")),
                ("_Slice", ("lo", "hi", "step", "line")),
                ("_Starred", ("value", "is_kw", "line")),
                ("_Str", ("parts", "line")),
                ("_Subscript", ("obj", "idx", "line")),
                ("_Ternary", ("then_expr", "cond", "else_expr", "line")),
                ("_Try", ("body", "handlers", "else_body", "finally_body", "line")),
                ("_Tuple", ("elems", "line")),
                ("_UnaryOp", ("op", "operand", "line")),
                ("_While", ("cond", "body", "else_body", "line")),
                ("_With", ("items", "body", "line", "is_async")),
                ("_Yield", ("value", "is_from", "line")),
            )
        ]
    )


def _populate_static_native_exports_3(out):
    # The shared field walk returns native AST objects in a native list.
    # Standalone consumers need that domain across import, iteration and
    # subsequent statement-field reads just as the closed-world build does.
    out["pcc.py_frontend.pipeline_exports"] = {
        "instance_field_assignment_statements": _function_export(
            ("list", ("dyn",)),
            (("dyn",),),
            (_export_arg("body", ("dyn",)),),
        ),
    }
    out["pcc.py_frontend.type_infer"] = {
        "infer_module": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",), ("dyn",), ("dyn",)),
            (
                _export_arg("m", ("dyn",)),
                _export_arg("", kind="kw_only"),
                _export_arg("external_exports", ("dyn",), has_default=True),
                _export_arg("derived_class_map", ("dyn",), has_default=True),
                _export_arg("contextual_host_params", ("dyn",), has_default=True),
            ),
        ),
    }
    # Pure-data module exporting L1_CODEGEN_STATIC_METHODS (literal
    # tuple of method dicts).  Bound here so the standalone compile
    # of ``layer1_support.py`` can import it natively rather than
    # falling through to ``py_cpy_import`` + ``py_cpy_getattr``.
    out["pcc.py_frontend.codegen._l1_codegen_static_methods"] = {
        "L1_CODEGEN_STATIC_METHODS": _module_global_export(("dyn",)),
    }
    # Layer1.py's own top-level imports of ClassLowering,
    # L1CodeGenMixinStack, L1CodegenError, and the two
    # ``_low_ir_*`` helpers.  Without these the standalone compile
    # of layer1.py emits a handful of ``py_cpy_import`` +
    # ``py_cpy_getattr`` calls.  The classes only need their
    # identity (no fields needed for the import-binding fast
    # path); the functions get a permissive dyn signature.
    out["pcc.py_frontend.codegen.class_gen"] = {
        "ClassLowering": _class_export("ClassLowering"),
    }
    out["pcc.py_frontend.codegen.layer1_mixins"] = {
        "L1CodeGenMixinStack": _class_export("L1CodeGenMixinStack"),
    }
    out["pcc.py_frontend.codegen.errors"] = {
        "L1CodegenError": _class_export("L1CodegenError"),
        "CodegenDiagnosticError": _class_export("CodegenDiagnosticError"),
    }
    out["pcc.diagnostics"] = {
        "DiagnosticSpan": _class_export(
            "DiagnosticSpan",
            ("file", "line", "col", "end_line", "end_col"),
        ),
    }
    out["pcc.py_frontend.codegen.user_function_lowering"] = {
        "_low_ir_emit_function_to_llvm": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("low_fn", ("dyn",)),),
        ),
        "_low_ir_lower_typed_int_function": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("fd", ("dyn",)),),
        ),
    }
    # ``pcc/__main__.py`` does ``from pcc.cli_bootstrap import
    # bootstrap_cli_sys_argv_exit; bootstrap_cli_sys_argv_exit()``.
    # Without a static export the standalone per-module compile of
    # ``__main__`` resolves the symbol via ``py_cpy_import`` +
    # ``py_cpy_getattr`` + ``py_cpy_call_noargs`` (4 residual
    # ``py_cpy_*`` calls captured by the on-mode per-module
    # ratchet). The exposed function is no-args, returns None.
    out["pcc.cli_bootstrap"] = {
        "bootstrap_cli_sys_argv_exit": _function_export(
            ("none",),
            (),
            (),
        ),
    }
    # ``cli_bootstrap.py`` keeps only the public dispatch facade; the native
    # pytest harness lives in this closure-safe sibling.  The explicit
    # signature keeps the standalone bootstrap probe from routing the import
    # through libpython while the real multi-file stage1 build supplies the
    # implementation.
    out["pcc.cli_bootstrap_pytest"] = {
        "run_pcc1_pytest": _function_export(
            ("int",),
            (("dyn",), ("str",), ("int", 64, True)),
            (
                _export_arg("argv", ("dyn",)),
                _export_arg("executable", ("str",)),
                _export_arg("timeout_seconds", ("int", 64, True)),
            ),
        ),
    }
    out["pcc.cli_bootstrap_array_core"] = {
        "_run_native_package_array_core_from_pcc1": _function_export(
            ("int",),
            (("dyn",),),
            (_export_arg("module_args", ("dyn",)),),
        ),
    }
    out["pcc.py_frontend.compile_cache"] = {
        "acquire_python_frontend_ir_cache": _function_export(
            ("bool",),
            (("dyn",),),
            (_export_arg("plan", ("dyn",)),),
        ),
        "load_python_frontend_ir_cache": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",)),
            (
                _export_arg("plan", ("dyn",)),
                _export_arg("expected_module_names", ("dyn",)),
            ),
        ),
        "plan_python_frontend_ir_cache": _function_export(
            ("dyn",),
            (
                ("dyn",),
                ("dyn",),
                ("str",),
                ("str",),
                ("str",),
                ("dyn",),
                ("str",),
                ("str",),
            ),
            (
                _export_arg("src_paths", ("dyn",)),
                _export_arg("module_names", ("dyn",)),
                _export_arg("", kind="kw_only"),
                _export_arg("compiler_executable", ("str",)),
                _export_arg("host_python", ("str",)),
                _export_arg("entry_module", ("str",)),
                _export_arg("sibling_inits", ("dyn",)),
                _export_arg("libpython_mode", ("str",)),
                _export_arg("ir_scaffold_mode", ("str",)),
            ),
        ),
        "publish_python_frontend_ir_cache": _function_export(
            ("bool",),
            (("dyn",), ("dyn",)),
            (
                _export_arg("plan", ("dyn",)),
                _export_arg("result", ("dyn",)),
            ),
        ),
        "release_python_frontend_ir_cache": _function_export(
            ("none",),
            (("dyn",),),
            (_export_arg("plan", ("dyn",)),),
        ),
        "wait_python_frontend_ir_cache": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",), ("float",)),
            (
                _export_arg("plan", ("dyn",)),
                _export_arg("expected_module_names", ("dyn",)),
                _export_arg("", kind="kw_only"),
                _export_arg("timeout_seconds", ("float",), has_default=True),
            ),
        ),
    }
    # ``pcc/cli_bootstrap.py`` reaches into pipeline for the two
    # entry points it actually calls.  Static exports here avoid the
    # standalone per-module ``py_cpy_import + py_cpy_getattr`` for
    # ``compile_python`` / ``run_python_multi_codegen_worker`` (the
    # remaining +4 in the ratchet diagnostic).  The kw-only/default
    # signature mirrors ``pcc/py_frontend/pipeline.py:7035`` /
    # ``:7691`` so closed-world callsites still resolve.


def _populate_static_native_exports_4(out):
    out["pcc.py_frontend.pipeline"] = {
        "compile_python": _function_export(
            ("dyn",),
            (
                ("str",),
                ("str",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
                ("dyn",),
            ),
            (
                _export_arg("src_path", ("str",)),
                _export_arg("out_path", ("str",)),
                _export_arg("", kind="kw_only"),
                _export_arg("verbose", ("dyn",), has_default=True),
                _export_arg("emit_llvm_only", ("dyn",), has_default=True),
                _export_arg("libpython_mode", ("dyn",), has_default=True),
                _export_arg("ir_scaffold_mode", ("dyn",), has_default=True),
                _export_arg("backend", ("dyn",), has_default=True),
                _export_arg("recursive_stdlib", ("dyn",), has_default=True),
                _export_arg("python_library", ("dyn",), has_default=True),
                _export_arg("profile", ("dyn",), has_default=True),
            ),
        ),
        "run_python_multi_codegen_worker": _function_export(
            ("dyn",),
            (("str",),),
            (_export_arg("manifest_path", ("str",)),),
        ),
    }
    out["pcc.py_frontend.pipeline_frontend_workers"] = {
        "SOURCE_WORKER_AUTO_SAFE_JOBS": {
            "kind": "constant",
            "value_kind": "int",
            "value": 2,
        },
        "SOURCE_WORKER_AST_OVERSIZED_BYTES": {
            "kind": "constant",
            "value_kind": "int",
            "value": 6000000,
        },
        "compiled_native_auto_jobs": _function_export(
            ("int",),
            (("int",),),
            (_export_arg("jobs", ("int",)),),
        ),
        "compiled_native_export_jobs": _function_export(
            ("int",),
            (("int",),),
            (_export_arg("jobs", ("int",)),),
        ),
        "compiled_native_summary_jobs": _function_export(
            ("int",),
            (("int",),),
            (_export_arg("jobs", ("int",)),),
        ),
        "split_codegen_chunks_by_source_size": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",), ("int",), ("str",), ("int",)),
            (
                _export_arg("src_paths", ("dyn",)),
                _export_arg("chunks", ("dyn",)),
                _export_arg(
                    "threshold_bytes",
                    ("int",),
                    has_default=True,
                ),
                _export_arg("", kind="kw_only"),
                _export_arg("sidecar_dir", ("str",), has_default=True),
                _export_arg(
                    "sidecar_threshold_bytes",
                    ("int",),
                    has_default=True,
                ),
            ),
        ),
    }
    out["pcc.py_frontend.codegen.runtime_abi"] = {
        "declare_runtime": _function_export(
            ("dict", ("str",), ("dyn",)),
            (("dyn",),),
            (_export_arg("module", ("dyn",)),),
        ),
        "declare_runtime_global": _function_export(
            ("dyn",),
            (("dyn",), ("str",)),
            (
                _export_arg("module", ("dyn",)),
                _export_arg("name", ("str",)),
            ),
        ),
        "is_freestanding_gc_runtime_global": _function_export(
            ("bool",),
            (("str",),),
            (_export_arg("symbol", ("str",)),),
        ),
        "is_freestanding_gc_readonly_runtime_import": _function_export(
            ("bool",),
            (("str",),),
            (_export_arg("symbol", ("str",)),),
        ),
        "is_freestanding_gc_cross_object_runtime_import": _function_export(
            ("bool",),
            (("str",), ("str",), ("str",)),
            (
                _export_arg("symbol", ("str",)),
                _export_arg("parameters_source", ("str",)),
                _export_arg("return_source", ("str",)),
            ),
        ),
    }
    # marshal.py reaches into core_helpers for two text-level
    # instruction inspectors.  Static exports close marshal.py's
    # remaining on-mode residual (7 calls) without needing core_helpers
    # in the consumer allowlist (it's CONTEXTUAL_MIXIN, not standalone).
    out["pcc.py_frontend.codegen.core_helpers"] = {
        "_instruction_opname_text": _function_export(
            ("str",),
            (("dyn",),),
            (_export_arg("instr", ("dyn",)),),
        ),
        "_instruction_is_terminator_text": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("instr", ("dyn",)),),
        ),
    }
    # type_infer.py consumes these from pcc.py_frontend.types.  Adding
    # static exports closes the residual ~80 fallbacks in type_infer.py's
    # standalone compile (the rest are dict/list traversals over AST
    # node fields).  Signatures are intentionally permissive — the
    # callers only need name binding, not strict types.
    out["pcc.py_frontend.types"] = {
        "PyFrontendError": _class_export("PyFrontendError"),
        "parse_annotation": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("expr", ("dyn",)),),
        ),
        "type_eq": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",)),
            (_export_arg("a", ("dyn",)), _export_arg("b", ("dyn",))),
        ),
        "is_numeric": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("t", ("dyn",)),),
        ),
        "common_type": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",)),
            (_export_arg("a", ("dyn",)), _export_arg("b", ("dyn",))),
        ),
    }
    out["pcc.py_frontend.codegen.marshal"] = {
        "marshal_to_object": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",), ("dyn",), ("dyn",), ("dyn",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("module", ("dyn",)),
                _export_arg("runtime", ("dyn",)),
                _export_arg("v", ("dyn",)),
                _export_arg("ty", ("dyn",)),
            ),
        ),
        "marshal_from_object": _function_export(
            ("dyn",),
            (("dyn",), ("dyn",), ("dyn",), ("dyn",), ("dyn",)),
            (
                _export_arg("builder", ("dyn",)),
                _export_arg("module", ("dyn",)),
                _export_arg("runtime", ("dyn",)),
                _export_arg("v", ("dyn",)),
                _export_arg("ty", ("dyn",)),
            ),
        ),
    }


def _populate_static_native_exports_5(out):
    out["pcc.py_frontend.codegen.layer1_support"] = {
        "_import_names_from_stmt": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("stmt", ("dyn",)),),
        ),
        "_is_import_stmt": _function_export(
            ("bool",),
            (("dyn",),),
            (_export_arg("stmt", ("dyn",)),),
        ),
        "_is_import_from_stmt": _function_export(
            ("bool",),
            (("dyn",),),
            (_export_arg("stmt", ("dyn",)),),
        ),
        "_default_native_module_exports": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("module_name", ("dyn",)),),
        ),
        "_maybe_fold_str_to_float": _function_export(
            ("dyn",),
            (("str",),),
            (_export_arg("s", ("str",)),),
        ),
        "_dataclass_field_value": _function_export(
            ("dyn",),
            (("dyn",), ("str",), ("dyn",)),
            (
                _export_arg("obj", ("dyn",)),
                _export_arg("field_name", ("str",)),
                _export_arg("default", ("dyn",), has_default=True),
            ),
        ),
        "_dataclass_field_names": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("obj", ("dyn",)),),
        ),
        "_as_native_float": _function_export(
            ("float",),
            (("dyn",),),
            (_export_arg("value", ("dyn",)),),
        ),
        "_same_type_kind": _function_export(
            ("bool",),
            (("dyn",), ("dyn",)),
            (
                _export_arg("a", ("dyn",)),
                _export_arg("b", ("dyn",)),
            ),
        ),
        "_stmt_kind_name": _function_export(
            ("str",),
            (("dyn",),),
            (_export_arg("stmt", ("dyn",)),),
        ),
        "_replace_arg_with_none_default": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("arg", ("dyn",)),),
        ),
        "_zero_initializer_for": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("ir_ty", ("dyn",)),),
        ),
    }
    out["pcc.py_frontend.codegen.host_contract"] = {
        "PROBE_POLICY_STANDALONE": _str_constant_export("standalone"),
        "PROBE_POLICY_CONTEXTUAL_MIXIN": _str_constant_export("contextual-mixin"),
        "L1_CODEGEN_HOST_CLASS": _str_constant_export(
            "pcc.py_frontend.codegen.layer1.L1CodeGen"
        ),
        "L1_CODEGEN_HOST_ATTRS": _string_tuple_global_export(),
        "L1_CODEGEN_HOST_METHODS": _string_tuple_global_export(),
        "l1_codegen_lowering_host_contract": _function_export(
            ("dyn",),
            (),
            (),
        ),
        "per_module_probe_policy": _function_export(
            ("str",),
            (("str",),),
            (_export_arg("module_name", ("str",)),),
        ),
        "contextual_host_for_module": _function_export(
            ("str",),
            (("str",),),
            (_export_arg("module_name", ("str",)),),
        ),
        "contextual_per_module_modules": _function_export(
            ("dyn",),
            (("dyn",),),
            (_export_arg("module_names", ("dyn",)),),
        ),
    }
    out["pcc.py_frontend.codegen.layer1_constants"] = {
        "ANNOTATION_ONLY_IMPORT_MODULES": _string_tuple_global_export(),
        "COMPILE_TIME_ONLY_IMPORT_FROMS": _dict_str_dyn_global_export(),
        "COMPILE_TIME_ONLY_MODULES": _string_tuple_global_export(),
        "EXTERN_SCAFFOLD_MODULES": _string_tuple_global_export(),
        "IR_RUNTIME_COMPAT_MODULE": _str_constant_export("pcc.llvm_capi.compat"),
        "TEST_FACADE_IMPORT_MODULES": _string_tuple_global_export(),
        "UNSAFE_SCAFFOLD_MODULES": _string_tuple_global_export(),
    }
    out["pcc.codegen.c_varargs"] = {
        "postprocess_varargs_ir": _function_export(
            ("str",),
            (("str",),),
            (
                _export_arg("text", ("str",)),
                _export_arg("", kind="kw_only"),
                _export_arg("report", ("dyn",), has_default=True),
            ),
        ),
    }
    out["pcc.py_frontend.codegen.self_module_contracts"] = {
        "IR_SCAFFOLD_CONTRACT": _str_constant_export("ir-scaffold-forced"),
        "PY_AST_FIELD_ORDER_CONTRACT": _str_constant_export(
            "py-ast-field-order"
        ),
        "L1_CODEGEN_HOST_ATTR_CONTRACT": _str_constant_export(
            "l1-codegen-host-attrs"
        ),
        "SELF_MODULE_CONTRACTS": _dict_str_dyn_global_export(),
        "PY_AST_FIELD_OVERRIDE_MODULE": _str_constant_export(
            "pcc.py_frontend.py_ast"
        ),
        "L1_CODEGEN_HOST_ATTR_MODULE": _str_constant_export(
            "pcc.py_frontend.codegen.layer1"
        ),
        "module_has_contract": _function_export(
            ("bool",),
            (("dyn",), ("str",)),
            (
                _export_arg("module_name"),
                _export_arg("contract", ("str",)),
            ),
        ),
        "module_for_class_symbol_contract": _function_export(
            ("dyn",),
            (("str",), ("str",)),
            (
                _export_arg("global_name", ("str",)),
                _export_arg("contract", ("str",)),
            ),
        ),
    }


def _populate_static_native_exports_6(out):
    out["pcc.py_frontend.codegen.layer1"] = {
        "L1CodeGen": _class_export(
            "L1CodeGen",
            (),
            (
                {
                    "name": "__init__",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",), ("bool",), ("str",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("module", ("dyn",)),
                        _export_arg("", kind="kw_only"),
                        _export_arg(
                            "emit_cpy_main_exitcode",
                            ("bool",),
                            has_default=True,
                        ),
                        _export_arg(
                            "ir_scaffold_mode",
                            ("str",),
                            has_default=True,
                        ),
                    ),
                    "box_int_abi": False,
                },
                {
                    "name": "generate",
                    "kind": "instance",
                    "return_ty": ("str",),
                    "param_types": (("dyn",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("module", ("dyn",), has_default=True),
                    ),
                    "box_int_abi": False,
                },
            ),
        ),
    }


def _layer1_entrypoint_methods_0():
    return (
        {
            "name": "__init__",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",), ("bool",), ("str",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("module", ("dyn",)),
                _export_arg("emit_cpy_main_exitcode", ("bool",)),
                _export_arg("ir_scaffold_mode", ("str",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "generate",
            "kind": "instance",
            "return_ty": ("str",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("module", ("dyn",), has_default=True),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_span",
            "kind": "instance",
            "return_ty": ("str",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("node", ("dyn",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_module",
            "kind": "instance",
            "return_ty": ("str",),
            "param_types": (("dyn",),),
            "call_sig": (_export_arg("self"),),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_function",
            "kind": "instance",
            "return_ty": ("str",),
            "param_types": (("dyn",),),
            "call_sig": (_export_arg("self"),),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_is_enabled",
            "kind": "instance",
            "return_ty": ("bool",),
            "param_types": (("dyn",),),
            "call_sig": (_export_arg("self"),),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_set_stmt_context",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("int",), ("str",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("stmt_index", ("int",)),
                _export_arg("stmt_kind", ("str",)),
            ),
            "box_int_abi": False,
        },
    )


def _layer1_entrypoint_methods_1():
    return (
        {
            "name": "_codegen_trace_push",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (
                ("dyn",),
                ("str",),
                ("int",),
                ("str",),
                ("str",),
                ("str",),
            ),
            "call_sig": (
                _export_arg("self"),
                _export_arg("boundary", ("str",)),
                _export_arg("stmt_index", ("int",)),
                _export_arg("stmt_kind", ("str",)),
                _export_arg("expr_kind", ("str",)),
                _export_arg("span", ("str",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_dump",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("exc", ("dyn",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_codegen_trace_set_context_for_expr",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("expr", ("dyn",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_emit_stmts",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("stmts", ("dyn",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_emit_stmt",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("stmt", ("dyn",)),
            ),
            "box_int_abi": False,
        },
        {
            "name": "_emit_expr",
            "kind": "instance",
            "return_ty": ("dyn",),
            "param_types": (("dyn",), ("dyn",)),
            "call_sig": (
                _export_arg("self"),
                _export_arg("expr", ("dyn",)),
            ),
            "box_int_abi": False,
        },
    )



def _populate_static_native_exports_9(out):
    out["pcc.py_frontend.codegen.layer1_entrypoints"] = {
        "L1CodeGenEntrypointMixin": _class_export(
            "L1CodeGenEntrypointMixin",
            (),
            (_layer1_entrypoint_methods_0() + _layer1_entrypoint_methods_1()),
        ),
    }


def _populate_static_native_exports_7(out):
    out["pcc.py_frontend.codegen.layer1_init"] = {
        "Layer1InitMixin": _class_export(
            "Layer1InitMixin",
            (),
            (
                {
                    "name": "_init_l1_state",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",), ("bool",), ("str",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("module", ("dyn",)),
                        _export_arg("emit_cpy_main_exitcode", ("bool",)),
                        _export_arg("ir_scaffold_mode", ("str",)),
                    ),
                    "box_int_abi": False,
                },
            ),
        ),
    }
    out["pcc.py_frontend.codegen.expr_dispatch_lowering"] = {
        "ExprDispatchLoweringMixin": _class_export(
            "ExprDispatchLoweringMixin",
            (),
            (
                {
                    "name": "_emit_dynamic_binary_dunder_call",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",), ("str",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("lhs_expr", ("dyn",)),
                        _export_arg("dunder_name", ("str",)),
                        _export_arg("rhs_expr", ("dyn",)),
                    ),
                    "box_int_abi": False,
                },
                {
                    "name": "_emit_expr_impl",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("expr", ("dyn",)),
                    ),
                    "box_int_abi": False,
                },
            ),
        ),
    }
    out["pcc.py_frontend.codegen.generation_lowering"] = {
        "GenerationLoweringMixin": _class_export(
            "GenerationLoweringMixin",
            (),
            (
                {
                    "name": "_generate_impl",
                    "kind": "instance",
                    "return_ty": ("str",),
                    "param_types": (("dyn",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("module", ("dyn",)),
                    ),
                    "box_int_abi": False,
                },
            ),
        ),
    }
    out["pcc.py_frontend.codegen.stmt_dispatch_lowering"] = {
        "StmtDispatchLoweringMixin": _class_export(
            "StmtDispatchLoweringMixin",
            (),
            (
                {
                    "name": "_emit_stmts_impl",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("stmts", ("dyn",)),
                    ),
                    "box_int_abi": False,
                },
                {
                    "name": "_emit_stmt_impl",
                    "kind": "instance",
                    "return_ty": ("dyn",),
                    "param_types": (("dyn",), ("dyn",)),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("stmt", ("dyn",)),
                    ),
                    "box_int_abi": False,
                },
            ),
        ),
    }
    # ``subprocess.CalledProcessError`` is a stdlib semantic provider, not one
    # of pcc's own frontend exports, so it also lives in the module-independent
    # provider table below and is merged in here for the modules that get the
    # full static table.
    out["subprocess"] = _PCC_NATIVE_STDLIB_SEMANTIC_PROVIDERS["subprocess"]


# Stdlib classes that native lowering instantiates by name.  Native
# ``subprocess.run(..., check=True)`` builds a typed ``CalledProcessError``
# rather than a second subprocess exception model in the C runtime, and it
# emits that call mid-block -- the forward declaration cannot be missing and
# then recovered from.  Which module is being compiled says nothing about
# whether that lowering fires, so this table is looked up independently of
# ``_PCC_FRONTEND_STATIC_NATIVE_MODULES``.  The definition comes from
# ``pcc/py_stdlib/subprocess.py`` in the link closure; this is metadata only.
_PCC_NATIVE_STDLIB_SEMANTIC_PROVIDERS = {
    "subprocess": {
        "CalledProcessError": {
            "kind": "class",
            "owning_module": "subprocess",
            "export_name": "CalledProcessError",
            "class_name": "CalledProcessError",
            "qualified_name": "subprocess.CalledProcessError",
            "base_names": ("Exception",),
            "field_names": ("returncode", "cmd", "output", "stderr"),
            "field_types": (("returncode", ("int", 64, True)),),
            "methods": (
                {
                    "name": "__init__",
                    "symbol": "user_subprocess_CalledProcessError___init__",
                    "kind": "instance",
                    "return_ty": ("none",),
                    "param_types": (
                        ("dyn",),
                        ("int", 64, True),
                        ("dyn",),
                        ("dyn",),
                        ("dyn",),
                    ),
                    "call_sig": (
                        _export_arg("self"),
                        _export_arg("returncode", ("int", 64, True)),
                        _export_arg("cmd"),
                        _export_arg("output", has_default=True),
                        _export_arg("stderr", has_default=True),
                    ),
                    "box_int_abi": False,
                },
            ),
            "box_int_abi": False,
        },
    },
}


def _native_stdlib_semantic_provider(module_name, export_name):
    """The forward declaration for a stdlib class native lowering needs.

    Admitted independently of whether the module being compiled is one of
    pcc's own static-native modules: the older module allow-list silently
    dropped every ``pipeline.py`` split-out (``pipeline_native_link``,
    ``pipeline_pass_driver``, ``pipeline_runtime_archive``,
    ``pipeline_self_backend_*``) as those files were created.
    """
    providers = _PCC_NATIVE_STDLIB_SEMANTIC_PROVIDERS.get(module_name or "", None)
    if providers is None:
        return None
    return providers.get(export_name, None)


def _build_static_native_exports():
    out = {}
    _populate_static_native_exports_0(out)
    _populate_static_native_exports_1(out)
    _populate_static_native_exports_2(out)
    _populate_static_native_exports_8(out)
    _populate_static_native_exports_3(out)
    _populate_static_native_exports_4(out)
    _populate_static_native_exports_5(out)
    _populate_static_native_exports_6(out)
    _populate_static_native_exports_9(out)
    _populate_static_native_exports_7(out)
    return out


_PCC_FRONTEND_STATIC_NATIVE_EXPORTS = _build_static_native_exports()


_PCC_FRONTEND_STATIC_NATIVE_MODULES = frozenset(
    {
        "pcc.py_frontend.pipeline",
        "pcc.py_frontend.pipeline_exports",
        "pcc.py_frontend.pipeline_packages",
        "pcc.py_frontend.pipeline_frontend_parallel",
        "pcc.py_frontend.pipeline_frontend_workers",
        "pcc.py_frontend.compile_cache",
        "pcc.py_frontend.codegen.layer1_support",
        "pcc.py_frontend.codegen.host_contract",
        "pcc.py_frontend.codegen.self_module_contracts",
        "pcc.codegen.c_varargs",
        "pcc.py_frontend.codegen.layer1_constants",
        "pcc.py_frontend.codegen.layer1",
        "pcc.py_frontend.codegen.layer1_entrypoints",
        "pcc.py_frontend.codegen.layer1_init",
        "pcc.py_frontend.codegen.expr_dispatch_lowering",
        "pcc.py_frontend.codegen.generation_lowering",
        "pcc.py_frontend.codegen.stmt_dispatch_lowering",
        "pcc.py_frontend.codegen.class_gen",
        "pcc.py_frontend.codegen.marshal",
        "pcc.py_frontend.type_infer",
        "pcc.py_frontend.types",
        "pcc.cli_bootstrap",
        "pcc.cli_bootstrap_pytest",
        "pcc.cli_bootstrap_array_core",
        "pcc.py_frontend.codegen._l1_codegen_static_methods",
        "pcc.backend.self_backend_aarch64_darwin_flow",
        "pcc.backend.self_backend_aarch64_darwin_regalloc",
        "pcc.backend.self_backend_stackprep",
        "pcc.parse.py_lex",
        "pcc.parse.py_lift",
        "pcc.parse.py_parse",
        "pcc.py_frontend.export_meta",
        "pcc.package_schema",
        "pcc.package_metadata_paths",
        "pcc.__main__",
        "pcc.cli_contract",
    }
)


# Append the auto-generated L1CodeGen mixin-merged method entries
# to L1CodeGen's static export.  Closes the standalone class_gen.py
# fallback regression that comes from class_gen calling
# ``self.parent.<method>`` (where parent is L1CodeGen): in the
# closed-world build ``pipeline.py::_merge_l1_codegen_methods``
# collects these via AST inspection; the standalone path bypasses
# that merge.  Loading the pre-generated literal data at module
# import-time fills the same gap without depending on
# ``inspect.signature`` (which isn't supported in the no-libpython
# bootstrap closure).  Regenerate the data file via
# ``scripts/regen_l1_codegen_static_methods.py``.
from ._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS as _L1_STATIC_METHODS

_layer1_exports = _PCC_FRONTEND_STATIC_NATIVE_EXPORTS.get(
    "pcc.py_frontend.codegen.layer1"
)
if _layer1_exports is not None:
    _l1_info = _layer1_exports.get("L1CodeGen")
    if isinstance(_l1_info, dict):
        _existing = list(_l1_info.get("methods", ()))
        _existing_names = set()
        for _m in _existing:
            if isinstance(_m, dict) and "name" in _m:
                _existing_names.add(_m["name"])
        for _m in _L1_STATIC_METHODS:
            if _m.get("name") not in _existing_names:
                _existing.append(_m)
        _l1_info["methods"] = tuple(_existing)


def _default_native_module_exports(module_name: str | None):
    if module_name not in _PCC_FRONTEND_STATIC_NATIVE_MODULES:
        return None
    return _PCC_FRONTEND_STATIC_NATIVE_EXPORTS


def _maybe_fold_str_to_float(s: str):
    """Compile-time fold ``float("X")`` for X in the small set of
    literal forms that lower trivially to a native ``ir.Constant``.

    Returns ``None`` if ``s`` doesn't match a folded shape so the
    caller can fall through to the dynamic path. Issue 11.A.2: avoids
    pulling libpython for what's structurally a compile-time literal.
    """
    stripped = s.strip()
    lowered = stripped.lower()
    if lowered in ("inf", "+inf", "infinity", "+infinity"):
        return 1e309
    if lowered in ("-inf", "-infinity"):
        return -1e309
    if lowered in ("nan", "+nan", "-nan"):
        inf = 1e309
        return inf - inf
    return _parse_simple_decimal_float(stripped)


def _parse_simple_decimal_float(s: str):
    """Parse the decimal string shapes pcc folds for ``float("...")``.

    This intentionally covers the ordinary decimal grammar only:
    optional sign, digits, optional decimal point, and optional
    exponent. Other spellings stay on the dynamic path. Keeping this
    in pcc-friendly Python avoids pulling CPython into the compiler's
    own self-host closure just to parse a literal.
    """
    n = len(s)
    if n == 0:
        return None
    i = 0
    sign = 1.0
    if s[i] == "+":
        i += 1
    elif s[i] == "-":
        sign = -1.0
        i += 1
    if i >= n:
        return None

    value = 0.0
    saw_digit = False
    while i < n:
        d = _ascii_decimal_digit(s[i])
        if d < 0:
            break
        saw_digit = True
        value = value * 10.0 + d
        i += 1

    if i < n and s[i] == ".":
        i += 1
        place = 0.1
        while i < n:
            d = _ascii_decimal_digit(s[i])
            if d < 0:
                break
            saw_digit = True
            value = value + d * place
            place = place * 0.1
            i += 1

    if not saw_digit:
        return None

    if i < n and (s[i] == "e" or s[i] == "E"):
        i += 1
        exp_sign = 1
        if i < n and s[i] == "+":
            i += 1
        elif i < n and s[i] == "-":
            exp_sign = -1
            i += 1
        exp = 0
        saw_exp_digit = False
        while i < n:
            d = _ascii_decimal_digit(s[i])
            if d < 0:
                break
            saw_exp_digit = True
            exp = exp * 10 + d
            i += 1
        if not saw_exp_digit:
            return None
        if exp > 400:
            if exp_sign > 0:
                return sign * 1e309
            return sign * 0.0
        while exp > 0:
            if exp_sign > 0:
                value = value * 10.0
            else:
                value = value * 0.1
            exp -= 1

    if i != n:
        return None
    return sign * value


def _ascii_decimal_digit(ch: str) -> int:
    c = ord(ch)
    if 48 <= c <= 57:
        return c - 48
    return -1


def _dataclass_field_value(obj, field_name: str, default=None):
    return getattr(obj, field_name, default)


def _dataclass_field_names(obj):
    # This helper is on the self-host hot path.  Avoid probing
    # ``__dataclass_fields__`` first: on the pcc-py runtime, missing
    # attributes allocate and clear AttributeError objects, and this walker
    # also sees many primitive leaves.
    if obj is None:
        return ()
    if (
        isinstance(obj, str)
        or isinstance(obj, int)
        or isinstance(obj, bool)
        or isinstance(obj, float)
        or isinstance(obj, bytes)
    ):
        return ()
    if isinstance(obj, SourceSpan):
        return ("file", "line", "col", "end_line", "end_col")
    if isinstance(obj, IntType) or isinstance(obj, FloatType):
        return ("name", "width")
    if isinstance(obj, BoolType):
        return ("name",)
    if isinstance(obj, NoneType):
        return ("name",)
    if isinstance(obj, StrType):
        return ("name",)
    if isinstance(obj, ListType):
        return ("name", "elem")
    if isinstance(obj, SetType):
        return ("name", "elem")
    if isinstance(obj, ValueArrayType):
        return ("name", "elem", "length")
    if isinstance(obj, DictType):
        return ("name", "key", "value")
    if isinstance(obj, TupleType):
        return ("name", "elems")
    if isinstance(obj, FuncType):
        return ("name", "params", "ret")
    if isinstance(obj, ValueClassType):
        return (
            "name",
            "module",
            "fields",
            "bases",
            "properties",
            "flattened",
            "nullable_fields",
        )
    if isinstance(obj, ClassType):
        return ("name", "module", "fields", "bases", "properties", "valueclass")
    if isinstance(obj, DynType):
        return ("name",)
    if isinstance(obj, Type):
        return ("name",)
    if isinstance(obj, Expr):
        if isinstance(obj, NoneLit):
            return ("span", "ty")
        if (
            isinstance(obj, IntLit)
            or isinstance(obj, FloatLit)
            or isinstance(obj, BoolLit)
            or isinstance(obj, StrLit)
        ):
            return ("span", "ty", "value")
        if isinstance(obj, Name):
            return ("span", "ty", "ident")
        if isinstance(obj, BinOp):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, UnaryOp):
            return ("span", "ty", "op", "operand")
        if isinstance(obj, Compare):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, BoolExpr):
            return ("span", "ty", "op", "left", "right")
        if isinstance(obj, Call):
            return ("span", "ty", "func", "args", "kwargs")
        if isinstance(obj, Attr):
            return ("span", "ty", "obj", "name")
        if isinstance(obj, Subscript):
            return ("span", "ty", "obj", "idx")
        if isinstance(obj, Slice):
            return ("span", "ty", "lo", "hi", "step")
        if isinstance(obj, ListExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, DictExpr):
            return ("span", "ty", "pairs")
        if isinstance(obj, TupleExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, IfExpr):
            return ("span", "ty", "cond", "then_e", "else_e")
        if isinstance(obj, Lambda):
            return ("span", "ty", "params", "body")
    if isinstance(obj, Stmt):
        if isinstance(obj, Assign):
            return ("span", "targets", "value", "annotation")
        if isinstance(obj, AugAssign):
            return ("span", "target", "op", "value")
        if isinstance(obj, ExprStmt):
            return ("span", "expr")
        if isinstance(obj, If):
            return ("span", "cond", "body", "else_body")
        if isinstance(obj, While):
            return ("span", "cond", "body", "else_body")
        if isinstance(obj, For):
            return ("span", "target", "iter", "body", "else_body")
        if isinstance(obj, Return):
            return ("span", "value")
        if isinstance(obj, Pass) or isinstance(obj, Break) or isinstance(obj, Continue):
            return ("span",)
        if isinstance(obj, Raise):
            return ("span", "exc", "cause")
        if isinstance(obj, Try):
            return ("span", "body", "handlers", "else_body", "finally_body")
        if isinstance(obj, With):
            return ("span", "items", "body")
        if isinstance(obj, Import):
            return ("span", "names")
        if isinstance(obj, ImportFrom):
            return ("span", "module", "names", "level")
        if isinstance(obj, Global):
            return ("span", "names")
        if isinstance(obj, Nonlocal):
            return ("span", "names")
        if isinstance(obj, Delete):
            return ("span", "targets")
        if isinstance(obj, FuncDef):
            return (
                "span",
                "name",
                "args",
                "return_ty",
                "body",
                "decorators",
                "is_method",
                "is_async",
            )
        if isinstance(obj, ClassDef):
            return (
                "span",
                "name",
                "bases",
                "keywords",
                "body",
                "decorators",
            )
    if isinstance(obj, Arg):
        return ("name", "annotation", "default", "kind", "has_default")
    if isinstance(obj, ExceptHandler):
        return ("exc_type", "name", "body", "span")
    if isinstance(obj, Module):
        return ("name", "body", "docstring")
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        return fields.keys()
    return ()


def _as_native_float(value) -> float:
    return value


def _type_kind_key(ty: Type) -> str:
    if isinstance(ty, IntType):
        return "IntType"
    if isinstance(ty, FloatType):
        return "FloatType"
    if isinstance(ty, BoolType):
        return "BoolType"
    if isinstance(ty, NoneType):
        return "NoneType"
    if isinstance(ty, StrType):
        return "StrType"
    if isinstance(ty, BytesType):
        return "BytesType"
    if isinstance(ty, ByteArrayType):
        return "ByteArrayType"
    if isinstance(ty, MemoryViewType):
        return "MemoryViewType"
    if isinstance(ty, ListType):
        return "ListType"
    if isinstance(ty, SetType):
        return "SetType"
    if isinstance(ty, ValueArrayType):
        return "ValueArrayType"
    if isinstance(ty, DictType):
        return "DictType"
    if isinstance(ty, TupleType):
        return "TupleType"
    if isinstance(ty, FuncType):
        return "FuncType"
    if isinstance(ty, ValueClassType):
        return "ValueClassType"
    if isinstance(ty, ClassType):
        return "ClassType"
    if isinstance(ty, ComplexType):
        return "ComplexType"
    if isinstance(ty, DynType):
        return "DynType"
    if isinstance(ty, Type):
        return "Type"
    return "unknown"


def _same_type_kind(a: Type, b: Type) -> bool:
    return _type_kind_key(a) == _type_kind_key(b)


def _stmt_kind_name(stmt: Stmt) -> str:
    if isinstance(stmt, FuncDef):
        return "FuncDef"
    if isinstance(stmt, ClassDef):
        return "ClassDef"
    if isinstance(stmt, Import):
        return "Import"
    if isinstance(stmt, ImportFrom):
        return "ImportFrom"
    if isinstance(stmt, Assign):
        return "Assign"
    if isinstance(stmt, AugAssign):
        return "AugAssign"
    if isinstance(stmt, ExprStmt):
        return "ExprStmt"
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
    if isinstance(stmt, Raise):
        return "Raise"
    if isinstance(stmt, Return):
        return "Return"
    if isinstance(stmt, Pass):
        return "Pass"
    if isinstance(stmt, Break):
        return "Break"
    if isinstance(stmt, Continue):
        return "Continue"
    if isinstance(stmt, Delete):
        return "Delete"
    if isinstance(stmt, Global):
        return "Global"
    if isinstance(stmt, Nonlocal):
        return "Nonlocal"
    return "Stmt"


def _replace_arg_with_none_default(arg):
    """Return a copy of ``arg`` with a ``NoneLit`` default when it has
    no explicit default. Used for click-decorated entry functions so
    ``pcc.main()`` at ``if __name__ == "__main__":`` compiles even
    though click normally supplies the runtime values. The synthesized
    default has the arg's declared annotation or DynType otherwise."""
    return _replace(
        arg,
        default=NoneLit(
            span=arg.span if hasattr(arg, "span") else None,
            ty=NoneType(name="None"),
        ),
    )


def _zero_initializer_for(ir_ty):
    kind = type(ir_ty).__name__
    if kind == "IntType":
        return 0
    if kind == "FloatType" or kind == "DoubleType":
        return 0.0
    if kind == "PointerType":
        return None
    return 0
