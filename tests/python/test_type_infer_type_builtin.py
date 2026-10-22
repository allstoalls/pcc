from __future__ import annotations

from pcc.frontends.python import parser, type_infer
from pcc.frontends.python.py_ast import Assign, Attr, Call, ClassType, StrType


def test_type_builtin_has_class_return_type():
    mod = parser.parse("x = type(1)\n", "type_probe.py")
    inferred = type_infer.infer_module(mod)
    stmt = inferred.body[0]
    assert isinstance(stmt, Assign)
    assert isinstance(stmt.value, Call)
    assert isinstance(stmt.value.ty, ClassType)
    assert stmt.value.ty.name == "type"


def test_type_builtin_name_attribute_is_string():
    mod = parser.parse("x = type(1).__name__\n", "type_probe.py")
    inferred = type_infer.infer_module(mod)
    stmt = inferred.body[0]
    assert isinstance(stmt, Assign)
    assert isinstance(stmt.value, Attr)
    assert isinstance(stmt.value.ty, StrType)
