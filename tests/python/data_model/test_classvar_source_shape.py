from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Assign, Attr, ClassDef, IntLit, Name


def test_parser_preserves_class_level_assignment_shape():
    mod = parser.parse(
        "class C:\n"
        "    count = 1\n"
        "C.count = 2\n",
        "classvar_probe.py",
    )

    cls = mod.body[0]
    update = mod.body[1]
    assert isinstance(cls, ClassDef)
    assert isinstance(cls.body[0], Assign)
    assert isinstance(cls.body[0].targets[0], Name)
    assert cls.body[0].targets[0].ident == "count"
    assert isinstance(cls.body[0].value, IntLit)

    assert isinstance(update, Assign)
    assert isinstance(update.targets[0], Attr)
    assert isinstance(update.targets[0].obj, Name)
    assert update.targets[0].obj.ident == "C"
    assert update.targets[0].name == "count"
