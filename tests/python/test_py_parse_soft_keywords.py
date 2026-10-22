from __future__ import annotations

from pcc.frontends.python import py_lift, py_parse


def test_match_soft_keyword_can_be_plain_assignment():
    mod = py_parse.parse(
        "match = 1\n" "case = match + 2\n",
        filename="<soft-keyword>",
    )

    assert len(mod.body) == 2
    assert all(type(stmt).__name__ == "_Assign" for stmt in mod.body)


def test_match_statement_still_parses_as_compound_statement():
    mod = py_parse.parse(
        "value = 1\n" "match value:\n" "    case 1:\n" "        result = 10\n",
        filename="<match-stmt>",
    )

    assert any(type(stmt).__name__ == "_If" for stmt in mod.body)


def test_closed_world_function_parameter_kinds_follow_separators():
    raw = py_parse.parse(
        "def f(a, /, b, *args, c, **kwargs):\n" "    pass\n",
        filename="<parameter-kinds>",
    )
    module = py_lift.lift_module(raw, "<parameter-kinds>", "sample")
    args = [arg for arg in module.body[0].args if arg.name]

    assert [(arg.name, arg.kind) for arg in args] == [
        ("a", "pos_only"),
        ("b", "pos"),
        ("args", "*args"),
        ("c", "kw_only"),
        ("kwargs", "**kwargs"),
    ]
