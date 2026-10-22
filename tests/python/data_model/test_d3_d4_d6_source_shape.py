from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import FuncDef, With, Call, Name


def test_async_def_source_shape_is_visible():
    mod = parser.parse(
        "async def f():\n"
        "    return 1\n",
        "async_probe.py",
    )
    fn = mod.body[0]
    assert isinstance(fn, FuncDef)
    assert fn.is_async is True


def test_with_source_shape_is_visible():
    mod = parser.parse(
        "with cm as value:\n"
        "    pass\n",
        "with_probe.py",
    )
    stmt = mod.body[0]
    assert isinstance(stmt, With)
    assert len(stmt.items) == 1


def test_format_call_source_shape_is_visible():
    mod = parser.parse(
        "format(x, 'x')\n",
        "format_probe.py",
    )
    call = mod.body[0].expr
    assert isinstance(call, Call)
    assert isinstance(call.func, Name)
    assert call.func.ident == "format"
