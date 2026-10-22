from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Assign, Attr, Call, Name


def test_call_splat_source_shape_is_preserved():
    mod = parser.parse(
        "f(*args, **kwargs)\n",
        "call_splat_probe.py",
    )
    call = mod.body[0].expr
    assert isinstance(call, Call)
    assert isinstance(call.func, Name)
    assert call.func.ident == "f"
    assert len(call.args) == 1
    assert len(call.kwargs) == 1
    assert call.kwargs[0][0] == "**"


def test_module_attr_write_source_shape_is_preserved():
    mod = parser.parse(
        "import m\n"
        "m.x = 3\n",
        "module_attr_probe.py",
    )
    stmt = mod.body[1]
    assert isinstance(stmt, Assign)
    assert isinstance(stmt.targets[0], Attr)
    assert isinstance(stmt.targets[0].obj, Name)
    assert stmt.targets[0].obj.ident == "m"
    assert stmt.targets[0].name == "x"
