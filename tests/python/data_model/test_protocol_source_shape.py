from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Call, Compare, Subscript, Assign, Delete


def test_protocol_builtin_source_shapes():
    mod = parser.parse(
        "len(x)\n"
        "bool(x)\n"
        "7 in x\n"
        "x[5]\n"
        "x[1] = 9\n"
        "del x[2]\n",
        "protocol_probe.py",
    )

    assert isinstance(mod.body[0].expr, Call)
    assert isinstance(mod.body[1].expr, Call)
    assert isinstance(mod.body[2].expr, Compare)
    assert isinstance(mod.body[3].expr, Subscript)
    assert isinstance(mod.body[4], Assign)
    assert isinstance(mod.body[4].targets[0], Subscript)
    assert isinstance(mod.body[5], Delete)
    assert isinstance(mod.body[5].targets[0], Subscript)
