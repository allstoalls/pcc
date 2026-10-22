from __future__ import annotations

import re

from pcc.frontends.python.py_ast import (
    Attr,
    ClassType,
    DynType,
    Name,
    SourceSpan,
    StrLit,
    Subscript,
    TupleExpr,
)
from pcc.frontends.python.types import parse_annotation


SPAN = SourceSpan("<test>", 1, 1, 1, 1)
DYN = DynType("dyn")


def name(ident: str) -> Name:
    return Name(SPAN, DYN, ident)


def dotted(*parts: str):
    expr = name(parts[0])
    for part in parts[1:]:
        expr = Attr(SPAN, DYN, expr, part)
    return expr


def sub(obj, idx):
    return Subscript(SPAN, DYN, obj, idx)


def tuple_expr(*elems):
    return TupleExpr(SPAN, DYN, elems)


def test_dotted_class_annotation_preserves_module_and_leaf():
    ty = parse_annotation(dotted("ir", "IRBuilder"))
    assert isinstance(ty, ClassType)
    assert ty.module == "ir"
    assert ty.name == "IRBuilder"


def test_typing_optional_unwraps_dotted_payload():
    ty = parse_annotation(sub(dotted("typing", "Optional"), dotted("ir", "Function")))
    assert isinstance(ty, ClassType)
    assert ty.module == "ir"
    assert ty.name == "Function"


def test_optional_unwraps_dotted_payload():
    ty = parse_annotation(sub(name("Optional"), dotted("ir", "IRBuilder")))
    assert isinstance(ty, ClassType)
    assert ty.module == "ir"
    assert ty.name == "IRBuilder"


def test_string_optional_annotation_unwraps_payload():
    ty = parse_annotation(StrLit(SPAN, DYN, "Optional[ir.Block]"))
    assert isinstance(ty, ClassType)
    assert ty.module == "ir"
    assert ty.name == "Block"


def test_union_with_none_unwraps_single_payload():
    ty = parse_annotation(
        sub(dotted("typing", "Union"), tuple_expr(dotted("ir", "Value"), name("None")))
    )
    assert isinstance(ty, ClassType)
    assert ty.module == "ir"
    assert ty.name == "Value"


def test_class_type_from_dotted_has_no_libpython_fallback(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    out = tmp_path / "types.ll"
    compile_python_multi(
        ["pcc/frontends/python/py_ast.py", "pcc/frontends/python/types.py"],
        str(out),
        emit_llvm_only=True,
        module_names=["pcc.frontends.python.py_ast", "pcc.frontends.python.types"],
        entry_module="pcc.frontends.python.types",
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = out.read_text(encoding="utf-8")
    match = re.search(
        r"define\s+[^\n]*@user_pcc_frontends_python_types__class_type_from_dotted"
        r"\([^)]*\)[^{]*\{(?P<body>.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, ir_text
    assert "py_cpy_" not in match.group("body")
