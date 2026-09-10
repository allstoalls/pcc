"""Stable JSON wire codec for the typed-Python frontend AST."""

from __future__ import annotations

import json

from .pipeline_modes import PyPipelineError


_PY_AST_WIRE_SCHEMA = "pcc.py_frontend.py_ast.v1"
_PY_AST_WIRE_NODE_KEY = "__pcc_py_ast_v1__"
_PY_AST_WIRE_BYTES_KEY = "__pcc_bytes_v1__"


def _closed_world_node_kind(node) -> str:
    try:
        return type(node).__name__
    except AttributeError:
        return ""


def _py_ast_field_value(obj, field_name, default=None):
    return getattr(obj, field_name, default)


_PY_AST_FIELD_NAME_OVERRIDES = {
    "SourceSpan": ("file", "line", "col", "end_line", "end_col"),
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
    "ClassType": ("name", "module", "fields", "bases", "properties", "valueclass"),
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
    "Stmt": ("span",),
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
    "Module": ("name", "body", "docstring"),
}


def _py_ast_field_names(obj):
    """Return the stable field order for a known typed-Python AST node.

    Closed-world walkers also encounter primitive leaves.  Those deliberately
    have no child fields; unknown values therefore return an empty tuple rather
    than importing the host dataclass implementation.
    """

    return _PY_AST_FIELD_NAME_OVERRIDES.get(_closed_world_node_kind(obj), ())


_PY_AST_BASE_NAME_OVERRIDES = {
    "IntType": ("Type",),
    "FloatType": ("Type",),
    "ComplexType": ("Type",),
    "BoolType": ("Type",),
    "NoneType": ("Type",),
    "StrType": ("Type",),
    "BytesType": ("Type",),
    "ByteArrayType": ("Type",),
    "MemoryViewType": ("Type",),
    "ListType": ("Type",),
    "SetType": ("Type",),
    "DictType": ("Type",),
    "TupleType": ("Type",),
    "FuncType": ("Type",),
    "ClassType": ("Type",),
    "ValueClassType": ("ClassType",),
    "DynType": ("Type",),
    "IntLit": ("Expr",),
    "FloatLit": ("Expr",),
    "ComplexLit": ("Expr",),
    "BoolLit": ("Expr",),
    "NoneLit": ("Expr",),
    "StrLit": ("Expr",),
    "BytesLit": ("Expr",),
    "Name": ("Expr",),
    "BinOp": ("Expr",),
    "UnaryOp": ("Expr",),
    "Compare": ("Expr",),
    "BoolExpr": ("Expr",),
    "Call": ("Expr",),
    "Attr": ("Expr",),
    "Subscript": ("Expr",),
    "Slice": ("Expr",),
    "ListExpr": ("Expr",),
    "DictExpr": ("Expr",),
    "TupleExpr": ("Expr",),
    "IfExpr": ("Expr",),
    "Lambda": ("Expr",),
    "Assign": ("Stmt",),
    "AugAssign": ("Stmt",),
    "ExprStmt": ("Stmt",),
    "If": ("Stmt",),
    "While": ("Stmt",),
    "For": ("Stmt",),
    "Return": ("Stmt",),
    "Pass": ("Stmt",),
    "Break": ("Stmt",),
    "Continue": ("Stmt",),
    "Raise": ("Stmt",),
    "Try": ("Stmt",),
    "With": ("Stmt",),
    "Import": ("Stmt",),
    "ImportFrom": ("Stmt",),
    "Global": ("Stmt",),
    "Nonlocal": ("Stmt",),
    "Delete": ("Stmt",),
    "FuncDef": ("Stmt",),
    "ClassDef": ("Stmt",),
}


_PY_AST_FIELD_TYPE_OVERRIDES = {
    "SourceSpan": {
        "file": "str",
        "line": "int",
        "col": "int",
        "end_line": "int",
        "end_col": "int",
    },
    "Type": {"name": "str"},
    "IntType": {"name": "str", "width": "int", "signed": "bool"},
    "FloatType": {"name": "str", "width": "int"},
    "ComplexType": {"name": "str"},
    "BoolType": {"name": "str"},
    "NoneType": {"name": "str"},
    "StrType": {"name": "str"},
    "BytesType": {"name": "str"},
    "ByteArrayType": {"name": "str"},
    "MemoryViewType": {"name": "str"},
    "ListType": {"name": "str", "elem": "Type"},
    "SetType": {"name": "str", "elem": "Type"},
    "DictType": {"name": "str", "key": "Type", "value": "Type"},
    "TupleType": {"name": "str", "elems": "tuple[Type, ...]"},
    "FuncType": {"name": "str", "params": "tuple[Type, ...]", "ret": "Type"},
    "ClassType": {
        "name": "str",
        "module": "str",
        "fields": "tuple[tuple[str, Type], ...]",
        "bases": "tuple[ClassType, ...]",
        "properties": "tuple[tuple[str, Type], ...]",
        "valueclass": "bool",
    },
    "ValueClassType": {
        "name": "str",
        "module": "str",
        "fields": "tuple[tuple[str, Type], ...]",
        "bases": "tuple[ClassType, ...]",
        "properties": "tuple[tuple[str, Type], ...]",
        "valueclass": "bool",
        "flattened": "bool",
        "nullable_fields": "bool",
    },
    "DynType": {"name": "str"},
    "Expr": {"span": "SourceSpan", "ty": "Type"},
    "IntLit": {"span": "SourceSpan", "ty": "Type", "value": "int"},
    "FloatLit": {"span": "SourceSpan", "ty": "Type", "value": "float"},
    "ComplexLit": {
        "span": "SourceSpan",
        "ty": "Type",
        "real": "float",
        "imag": "float",
    },
    "BoolLit": {"span": "SourceSpan", "ty": "Type", "value": "bool"},
    "NoneLit": {"span": "SourceSpan", "ty": "Type"},
    "StrLit": {"span": "SourceSpan", "ty": "Type", "value": "str"},
    "BytesLit": {"span": "SourceSpan", "ty": "Type", "value": "bytes"},
    "Name": {"span": "SourceSpan", "ty": "Type", "ident": "str"},
    "BinOp": {
        "span": "SourceSpan",
        "ty": "Type",
        "op": "str",
        "lhs": "Expr",
        "rhs": "Expr",
    },
    "UnaryOp": {
        "span": "SourceSpan",
        "ty": "Type",
        "op": "str",
        "operand": "Expr",
    },
    "Compare": {
        "span": "SourceSpan",
        "ty": "Type",
        "op": "str",
        "lhs": "Expr",
        "rhs": "Expr",
    },
    "BoolExpr": {
        "span": "SourceSpan",
        "ty": "Type",
        "op": "str",
        "left": "Expr",
        "right": "Expr",
    },
    "Call": {
        "span": "SourceSpan",
        "ty": "Type",
        "func": "Expr",
        "args": "tuple[Expr, ...]",
        "kwargs": "tuple[tuple[str, Expr], ...]",
        "operand_order": "tuple[tuple[str, int], ...]",
    },
    "Attr": {"span": "SourceSpan", "ty": "Type", "obj": "Expr", "name": "str"},
    "Subscript": {
        "span": "SourceSpan",
        "ty": "Type",
        "obj": "Expr",
        "idx": "Expr",
    },
    "Slice": {
        "span": "SourceSpan",
        "ty": "Type",
        "lo": "Expr",
        "hi": "Expr",
        "step": "Expr",
    },
    "ListExpr": {"span": "SourceSpan", "ty": "Type", "elems": "tuple[Expr, ...]"},
    "DictExpr": {
        "span": "SourceSpan",
        "ty": "Type",
        "pairs": "tuple[tuple[Expr, Expr], ...]",
    },
    "TupleExpr": {"span": "SourceSpan", "ty": "Type", "elems": "tuple[Expr, ...]"},
    "IfExpr": {
        "span": "SourceSpan",
        "ty": "Type",
        "cond": "Expr",
        "then_e": "Expr",
        "else_e": "Expr",
    },
    "Lambda": {
        "span": "SourceSpan",
        "ty": "Type",
        "params": "tuple[Arg, ...]",
        "body": "Expr",
    },
    "Stmt": {"span": "SourceSpan"},
    "Assign": {
        "span": "SourceSpan",
        "targets": "tuple[Expr, ...]",
        "value": "Expr",
        "annotation": "Type",
    },
    "AugAssign": {
        "span": "SourceSpan",
        "target": "Expr",
        "op": "str",
        "value": "Expr",
    },
    "ExprStmt": {"span": "SourceSpan", "expr": "Expr"},
    "If": {
        "span": "SourceSpan",
        "cond": "Expr",
        "body": "tuple[Stmt, ...]",
        "else_body": "tuple[Stmt, ...]",
    },
    "While": {
        "span": "SourceSpan",
        "cond": "Expr",
        "body": "tuple[Stmt, ...]",
        "else_body": "tuple[Stmt, ...]",
    },
    "For": {
        "span": "SourceSpan",
        "target": "Expr",
        "iter": "Expr",
        "body": "tuple[Stmt, ...]",
        "else_body": "tuple[Stmt, ...]",
        "is_async": "bool",
    },
    "Return": {"span": "SourceSpan", "value": "Expr"},
    "Pass": {"span": "SourceSpan"},
    "Break": {"span": "SourceSpan"},
    "Continue": {"span": "SourceSpan"},
    "Raise": {"span": "SourceSpan", "exc": "Expr", "cause": "Expr"},
    "Try": {
        "span": "SourceSpan",
        "body": "tuple[Stmt, ...]",
        "handlers": "tuple[ExceptHandler, ...]",
        "else_body": "tuple[Stmt, ...]",
        "finally_body": "tuple[Stmt, ...]",
    },
    "ExceptHandler": {
        "exc_type": "Expr",
        "name": "str",
        "body": "tuple[Stmt, ...]",
        "span": "SourceSpan",
    },
    "With": {
        "span": "SourceSpan",
        "items": "tuple[tuple[Expr, Expr], ...]",
        "body": "tuple[Stmt, ...]",
        "is_async": "bool",
    },
    "Import": {"span": "SourceSpan", "names": "tuple[tuple[str, str], ...]"},
    "ImportFrom": {
        "span": "SourceSpan",
        "module": "str",
        "names": "tuple[tuple[str, str], ...]",
        "level": "int",
    },
    "Global": {"span": "SourceSpan", "names": "tuple[str, ...]"},
    "Nonlocal": {"span": "SourceSpan", "names": "tuple[str, ...]"},
    "Delete": {"span": "SourceSpan", "targets": "tuple[Expr, ...]"},
    "Arg": {
        "name": "str",
        "annotation": "Type",
        "default": "Expr",
        "kind": "str",
        "has_default": "bool",
    },
    "FuncDef": {
        "span": "SourceSpan",
        "name": "str",
        "args": "tuple[Arg, ...]",
        "return_ty": "Type",
        "body": "tuple[Stmt, ...]",
        "decorators": "tuple[Expr, ...]",
        "is_method": "bool",
        "is_async": "bool",
    },
    "ClassDef": {
        "span": "SourceSpan",
        "name": "str",
        "bases": "tuple[Expr, ...]",
        "keywords": "tuple[tuple[str, Expr], ...]",
        "body": "tuple[Stmt, ...]",
        "decorators": "tuple[Expr, ...]",
    },
    "Module": {"name": "str", "body": "tuple[Stmt, ...]", "docstring": "str"},
}


def _py_ast_field_type_override(class_name: str, field_name: str):
    pairs = ()
    if class_name == "SourceSpan":
        pairs = (
            ("file", "str"),
            ("line", "int"),
            ("col", "int"),
            ("end_line", "int"),
            ("end_col", "int"),
        )
    elif class_name == "Type":
        pairs = (("name", "str"),)
    elif class_name == "Expr":
        pairs = (("span", "SourceSpan"), ("ty", "Type"))
    elif class_name == "Stmt":
        pairs = (("span", "SourceSpan"),)
    elif class_name == "Name":
        pairs = (("span", "SourceSpan"), ("ty", "Type"), ("ident", "str"))
    elif class_name == "Arg":
        pairs = (
            ("name", "str"),
            ("annotation", "Type"),
            ("default", "Expr"),
            ("kind", "str"),
            ("has_default", "bool"),
        )
    elif class_name == "FuncDef":
        pairs = (
            ("span", "SourceSpan"),
            ("name", "str"),
            ("args", "tuple[Arg, ...]"),
            ("return_ty", "Type"),
            ("body", "tuple[Stmt, ...]"),
            ("decorators", "tuple[Expr, ...]"),
            ("is_method", "bool"),
            ("is_async", "bool"),
        )
    elif class_name == "ClassDef":
        pairs = (
            ("span", "SourceSpan"),
            ("name", "str"),
            ("bases", "tuple[Expr, ...]"),
            ("keywords", "tuple[tuple[str, Expr], ...]"),
            ("body", "tuple[Stmt, ...]"),
            ("decorators", "tuple[Expr, ...]"),
        )
    elif class_name == "Assign":
        pairs = (
            ("span", "SourceSpan"),
            ("targets", "tuple[Expr, ...]"),
            ("value", "Expr"),
            ("annotation", "Type"),
        )
    elif class_name == "For":
        pairs = (
            ("span", "SourceSpan"),
            ("target", "Expr"),
            ("iter", "Expr"),
            ("body", "tuple[Stmt, ...]"),
            ("else_body", "tuple[Stmt, ...]"),
            ("is_async", "bool"),
        )
    elif class_name == "Return":
        pairs = (("span", "SourceSpan"), ("value", "Expr"))
    elif class_name == "ExprStmt":
        pairs = (("span", "SourceSpan"), ("expr", "Expr"))
    elif class_name == "If" or class_name == "While":
        pairs = (
            ("span", "SourceSpan"),
            ("cond", "Expr"),
            ("body", "tuple[Stmt, ...]"),
            ("else_body", "tuple[Stmt, ...]"),
        )
    elif class_name == "Call":
        pairs = (
            ("span", "SourceSpan"),
            ("ty", "Type"),
            ("func", "Expr"),
            ("args", "tuple[Expr, ...]"),
            ("kwargs", "tuple[tuple[str, Expr], ...]"),
            ("operand_order", "tuple[tuple[str, int], ...]"),
        )
    elif class_name == "Attr":
        pairs = (
            ("span", "SourceSpan"),
            ("ty", "Type"),
            ("obj", "Expr"),
            ("name", "str"),
        )
    elif class_name == "TupleExpr" or class_name == "ListExpr":
        pairs = (
            ("span", "SourceSpan"),
            ("ty", "Type"),
            ("elems", "tuple[Expr, ...]"),
        )
    if pairs:
        for override_field_name, field_type_text in pairs:
            if override_field_name == field_name:
                return field_type_text
        return None
    for override_class_name, field_map in _PY_AST_FIELD_TYPE_OVERRIDES.items():
        if override_class_name != class_name:
            continue
        for override_field_name, field_type_text in field_map.items():
            if override_field_name == field_name:
                return field_type_text
        return None
    return None


def _py_ast_bytes_to_wire(value):
    items = []
    i = 0
    while i < len(value):
        items.append(int(value[i]))
        i += 1
    return {_PY_AST_WIRE_BYTES_KEY: items}


def _py_ast_to_wire(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return _py_ast_bytes_to_wire(value)
    kind = _closed_world_node_kind(value)
    if kind == "bytes":
        return _py_ast_bytes_to_wire(value)
    if isinstance(value, (tuple, list)):
        return [_py_ast_to_wire(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key)] = _py_ast_to_wire(item)
        return out
    field_names = _PY_AST_FIELD_NAME_OVERRIDES.get(kind)
    if field_names is None:
        raise PyPipelineError("cannot serialize py_ast node kind " + kind)
    fields = {}
    for field_name in field_names:
        fields[field_name] = _py_ast_to_wire(
            _py_ast_field_value(value, field_name, None)
        )
    return {_PY_AST_WIRE_NODE_KEY: kind, "fields": fields}


def _py_ast_wire_bytes(items):
    if items is None:
        return b""
    raw = []
    for item in items:
        raw.append(int(item))
    return bytes(raw)


def _py_ast_wire_field(fields, name: str, default=None):
    if not isinstance(fields, dict):
        return default
    if name not in fields:
        return default
    # One lookup, not two: this runs once per AST field for every node of
    # every module, and each lookup hashes the key and string-compares it.
    # Subscript rather than ``.get`` — ``dict.get`` mis-lowers under pcc1.
    return _py_ast_from_wire(fields[name])


def _py_ast_wire_tuple_field(fields, name: str):
    value = _py_ast_wire_field(fields, name, ())
    return () if value is None else value


def _py_ast_wire_bool_field(fields, name: str, default: bool):
    value = _py_ast_wire_field(fields, name, default)
    return default if value is None else value


def _py_ast_node_replace(node, changes):
    """Rebuild ``node`` with ``changes`` applied, without dataclass reflection.

    ``dataclasses.replace`` needs the host dataclass implementation plus ``**``
    expansion of a dynamically built mapping, and neither is available to the
    self-hosted compiler -- which is why nothing in the closed-world frontend
    uses ``dataclasses.fields``. The wire schema already names every field of
    every node in a stable order, and ``_py_ast_node_from_wire`` already builds
    a node from a field mapping, so an AST rewrite is a read of the old fields
    plus that constructor.
    """

    fields = {}
    for field_name in _py_ast_field_names(node):
        if field_name in changes:
            fields[field_name] = changes[field_name]
        else:
            fields[field_name] = _py_ast_field_value(node, field_name, None)
    return _py_ast_node_from_wire(_closed_world_node_kind(node), fields)


def _py_ast_from_wire(value):
    if isinstance(value, dict):
        if _PY_AST_WIRE_BYTES_KEY in value:
            return _py_ast_wire_bytes(value.get(_PY_AST_WIRE_BYTES_KEY))
        kind = value.get(_PY_AST_WIRE_NODE_KEY)
        if isinstance(kind, str) and kind:
            return _py_ast_node_from_wire(kind, value.get("fields", {}))
        out = {}
        for key, item in value.items():
            out[str(key)] = _py_ast_from_wire(item)
        return out
    if isinstance(value, list):
        return tuple(_py_ast_from_wire(item) for item in value)
    return value


def _py_ast_node_from_wire(kind: str, fields):
    from . import py_ast as _pa

    if kind == "SourceSpan":
        return _pa.SourceSpan(
            _py_ast_wire_field(fields, "file", ""),
            _py_ast_wire_field(fields, "line", 0),
            _py_ast_wire_field(fields, "col", 0),
            _py_ast_wire_field(fields, "end_line", 0),
            _py_ast_wire_field(fields, "end_col", 0),
        )
    if kind == "Type":
        return _pa.Type(_py_ast_wire_field(fields, "name", ""))
    if kind == "IntType":
        return _pa.IntType(
            _py_ast_wire_field(fields, "name", "int"),
            _py_ast_wire_field(fields, "width", 64),
            _py_ast_wire_field(fields, "signed", True),
        )
    if kind == "FloatType":
        return _pa.FloatType(
            _py_ast_wire_field(fields, "name", "float"),
            _py_ast_wire_field(fields, "width", 64),
        )
    if kind == "ComplexType":
        return _pa.ComplexType(_py_ast_wire_field(fields, "name", "complex"))
    if kind == "BoolType":
        return _pa.BoolType(_py_ast_wire_field(fields, "name", "bool"))
    if kind == "NoneType":
        return _pa.NoneType(_py_ast_wire_field(fields, "name", "None"))
    if kind == "StrType":
        return _pa.StrType(_py_ast_wire_field(fields, "name", "str"))
    if kind == "BytesType":
        return _pa.BytesType(_py_ast_wire_field(fields, "name", "bytes"))
    if kind == "ByteArrayType":
        return _pa.ByteArrayType(_py_ast_wire_field(fields, "name", "bytearray"))
    if kind == "MemoryViewType":
        return _pa.MemoryViewType(_py_ast_wire_field(fields, "name", "memoryview"))
    if kind == "ListType":
        return _pa.ListType(
            _py_ast_wire_field(fields, "name", "list"),
            _py_ast_wire_field(fields, "elem"),
        )
    if kind == "SetType":
        return _pa.SetType(
            _py_ast_wire_field(fields, "name", "set"),
            _py_ast_wire_field(fields, "elem"),
        )
    if kind == "DictType":
        return _pa.DictType(
            _py_ast_wire_field(fields, "name", "dict"),
            _py_ast_wire_field(fields, "key"),
            _py_ast_wire_field(fields, "value"),
        )
    if kind == "TupleType":
        return _pa.TupleType(
            _py_ast_wire_field(fields, "name", "tuple"),
            _py_ast_wire_field(fields, "elems", ()),
        )
    if kind == "FuncType":
        return _pa.FuncType(
            _py_ast_wire_field(fields, "name", "func"),
            _py_ast_wire_field(fields, "params", ()),
            _py_ast_wire_field(fields, "ret"),
        )
    if kind == "ClassType":
        return _pa.ClassType(
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "module", ""),
            _py_ast_wire_tuple_field(fields, "fields"),
            _py_ast_wire_tuple_field(fields, "bases"),
            _py_ast_wire_tuple_field(fields, "properties"),
            _py_ast_wire_bool_field(fields, "valueclass", False),
        )
    if kind == "ValueClassType":
        return _pa.ValueClassType(
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "module", ""),
            _py_ast_wire_tuple_field(fields, "fields"),
            _py_ast_wire_tuple_field(fields, "bases"),
            _py_ast_wire_tuple_field(fields, "properties"),
            _py_ast_wire_bool_field(fields, "valueclass", True),
            _py_ast_wire_bool_field(fields, "flattened", True),
            _py_ast_wire_bool_field(fields, "nullable_fields", False),
        )
    if kind == "DynType":
        return _pa.DynType(_py_ast_wire_field(fields, "name", "dyn"))
    if kind == "IntLit":
        return _pa.IntLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "value", 0),
        )
    if kind == "FloatLit":
        return _pa.FloatLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "value", 0.0),
        )
    if kind == "ComplexLit":
        return _pa.ComplexLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "real", 0.0),
            _py_ast_wire_field(fields, "imag", 0.0),
        )
    if kind == "BoolLit":
        return _pa.BoolLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "value", False),
        )
    if kind == "NoneLit":
        return _pa.NoneLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
        )
    if kind == "StrLit":
        return _pa.StrLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "value", ""),
        )
    if kind == "BytesLit":
        return _pa.BytesLit(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "value", b""),
        )
    if kind == "Name":
        return _pa.Name(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "ident", ""),
        )
    if kind == "BinOp":
        return _pa.BinOp(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "op", ""),
            _py_ast_wire_field(fields, "lhs"),
            _py_ast_wire_field(fields, "rhs"),
        )
    if kind == "UnaryOp":
        return _pa.UnaryOp(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "op", ""),
            _py_ast_wire_field(fields, "operand"),
        )
    if kind == "Compare":
        return _pa.Compare(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "op", ""),
            _py_ast_wire_field(fields, "lhs"),
            _py_ast_wire_field(fields, "rhs"),
        )
    if kind == "BoolExpr":
        return _pa.BoolExpr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "op", ""),
            _py_ast_wire_field(fields, "left"),
            _py_ast_wire_field(fields, "right"),
        )
    if kind == "Call":
        return _pa.Call(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "func"),
            _py_ast_wire_field(fields, "args", ()),
            _py_ast_wire_field(fields, "kwargs", ()),
            _py_ast_wire_field(fields, "operand_order", ()),
        )
    if kind == "Attr":
        return _pa.Attr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "obj"),
            _py_ast_wire_field(fields, "name", ""),
        )
    if kind == "Subscript":
        return _pa.Subscript(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "obj"),
            _py_ast_wire_field(fields, "idx"),
        )
    if kind == "Slice":
        return _pa.Slice(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "lo"),
            _py_ast_wire_field(fields, "hi"),
            _py_ast_wire_field(fields, "step"),
        )
    if kind == "ListExpr":
        return _pa.ListExpr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "elems", ()),
        )
    if kind == "DictExpr":
        return _pa.DictExpr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "pairs", ()),
        )
    if kind == "TupleExpr":
        return _pa.TupleExpr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "elems", ()),
        )
    if kind == "IfExpr":
        return _pa.IfExpr(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "cond"),
            _py_ast_wire_field(fields, "then_e"),
            _py_ast_wire_field(fields, "else_e"),
        )
    if kind == "Lambda":
        return _pa.Lambda(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "ty"),
            _py_ast_wire_field(fields, "params", ()),
            _py_ast_wire_field(fields, "body"),
        )
    if kind == "Assign":
        return _pa.Assign(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "targets", ()),
            _py_ast_wire_field(fields, "value"),
            _py_ast_wire_field(fields, "annotation"),
        )
    if kind == "AugAssign":
        return _pa.AugAssign(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "target"),
            _py_ast_wire_field(fields, "op", ""),
            _py_ast_wire_field(fields, "value"),
        )
    if kind == "ExprStmt":
        return _pa.ExprStmt(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "expr"),
        )
    if kind == "If":
        return _pa.If(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "cond"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "else_body", ()),
        )
    if kind == "While":
        return _pa.While(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "cond"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "else_body", ()),
        )
    if kind == "For":
        return _pa.For(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "target"),
            _py_ast_wire_field(fields, "iter"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "else_body", ()),
            _py_ast_wire_field(fields, "is_async", False),
        )
    if kind == "Return":
        return _pa.Return(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "value"),
        )
    if kind == "Pass":
        return _pa.Pass(_py_ast_wire_field(fields, "span"))
    if kind == "Break":
        return _pa.Break(_py_ast_wire_field(fields, "span"))
    if kind == "Continue":
        return _pa.Continue(_py_ast_wire_field(fields, "span"))
    if kind == "Raise":
        return _pa.Raise(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "exc"),
            _py_ast_wire_field(fields, "cause"),
        )
    if kind == "Try":
        return _pa.Try(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "handlers", ()),
            _py_ast_wire_field(fields, "else_body", ()),
            _py_ast_wire_field(fields, "finally_body", ()),
        )
    if kind == "ExceptHandler":
        return _pa.ExceptHandler(
            _py_ast_wire_field(fields, "exc_type"),
            _py_ast_wire_field(fields, "name"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "span"),
        )
    if kind == "With":
        return _pa.With(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "items", ()),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "is_async", False),
        )
    if kind == "Import":
        return _pa.Import(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "names", ()),
        )
    if kind == "ImportFrom":
        return _pa.ImportFrom(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "module", ""),
            _py_ast_wire_field(fields, "names", ()),
            _py_ast_wire_field(fields, "level", 0),
        )
    if kind == "Global":
        return _pa.Global(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "names", ()),
        )
    if kind == "Nonlocal":
        return _pa.Nonlocal(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "names", ()),
        )
    if kind == "Delete":
        return _pa.Delete(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "targets", ()),
        )
    if kind == "Arg":
        return _pa.Arg(
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "annotation"),
            _py_ast_wire_field(fields, "default"),
            _py_ast_wire_field(fields, "kind", "pos"),
            _py_ast_wire_field(fields, "has_default", False),
        )
    if kind == "FuncDef":
        return _pa.FuncDef(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "args", ()),
            _py_ast_wire_field(fields, "return_ty"),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "decorators", ()),
            _py_ast_wire_field(fields, "is_method", False),
            _py_ast_wire_field(fields, "is_async", False),
        )
    if kind == "ClassDef":
        return _pa.ClassDef(
            _py_ast_wire_field(fields, "span"),
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "bases", ()),
            _py_ast_wire_field(fields, "keywords", ()),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "decorators", ()),
        )
    if kind == "Module":
        return _pa.Module(
            _py_ast_wire_field(fields, "name", ""),
            _py_ast_wire_field(fields, "body", ()),
            _py_ast_wire_field(fields, "docstring"),
        )
    raise PyPipelineError("unknown py_ast wire node kind " + kind)


def _write_py_ast_wire(path: str, ast_mod) -> None:
    payload = {"schema": _PY_AST_WIRE_SCHEMA, "module": _py_ast_to_wire(ast_mod)}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload))


def _read_py_ast_wire(path: str):
    with open(path, "r", encoding="utf-8") as f:
        payload = json.loads(f.read())
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if schema != _PY_AST_WIRE_SCHEMA:
        raise PyPipelineError(
            "invalid frontend py_ast wire file " + path + " schema=" + str(schema)
        )
    return _py_ast_from_wire(payload.get("module"))

