"""Builtin wrapper discovery distinguishes assignment targets from reads."""

import pytest

from pcc.frontends.python import py_ast, py_lift, py_parse


@pytest.mark.parametrize("source, names", [
    ("id = 0", ()),
    ("class Compiler:\n    id = 'gcc'", ()),
    ("def function():\n    id = 0", ()),
    ("id: int = 0", ()),
    ("id: int", ()),
    ("id, len = values", ()),
    ("(id, [len, *ord]) = values", ()),
    ("builtins.id = value", ()),
    ("obj.id = value", ()),
    ("obj[id] = value", ("id",)),
    ("obj[id:len] = value", ("id", "len")),
    ("id.member = value", ("id",)),
    ("builtins.id.member = value", ("id",)),
    ("obj[builtins.id] = value", ("id",)),
    ("id = id", ("id",)),
    ("id: int = id", ("id",)),
    ("first = id = 0", ()),
    ("first = second = id = 0", ()),
    ("first = id = len", ("len",)),
    ("first = (id := len)", ("len",)),
    ("obj[(id := len)] = value", ("len",)),
    ("id += value", ("id",)),
    ("builtins.id += value", ("id",)),
    ("def function(argument=id):\n    pass", ("id",)),
    ("function = lambda argument=id: argument", ("id",)),
    ("@id\ndef function():\n    pass", ("id",)),
    ("from builtins import id", ("id",)),
    ("value = builtins.id", ("id",)),
    ("value = id()", ()),
    ("obj[id()] = value", ()),
    ("value = function(id)", ("id",)),
    # This pass distinguishes positions, not scopes; reads stay conservative.
    ("class Compiler:\n    id = 'gcc'\n    value = id", ("id",)),
])
def test_assignment_targets_do_not_request_builtin_values(source, names):
    source += "\n"
    filename = "<builtin-discovery>"
    raw = py_lift.lift_module(
        py_parse.parse(source, filename=filename), filename, "discovery",
    )
    finished = py_lift.parse_and_lift(source, filename, "discovery")
    expected = (
        ["__pcc_builtin_exactly_one"]
        + ["__pcc_builtin_value_" + name for name in names]
        if names else []
    )
    assert [
        (type(stmt), stmt.name, stmt.span.file)
        for stmt in finished.body[:len(expected)]
    ] == [(py_ast.FuncDef, name, "<pcc-builtins>") for name in expected]
    assert finished.body[len(expected):] == raw.body
