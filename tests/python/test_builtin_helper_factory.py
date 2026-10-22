"""Builtin helper parsing returns only validated function definitions."""

import pytest

from pcc.frontends.python import builtin_function_values as values
from pcc.frontends.python.py_ast import FuncDef


@pytest.mark.parametrize("name, source", [
    (values.BUILTIN_HELPER_PREFIX + name, source)
    for name, source in values._HELPERS.items()
] + [
    (values.BUILTIN_VALUE_PREFIX + name, definition[1])
    for name, definition in values._SOURCES.items()
])
def test_builtin_helper_factory_validates_and_reuses_function(name, source, monkeypatch):
    monkeypatch.setattr(values, "_PARSED", {})

    function = values._parse_function(name, source)

    assert isinstance(function, FuncDef)
    assert function.name == name
    assert function.span.file == values.BUILTIN_SOURCE_FILE
    assert values._PARSED[name] is function
    # The cache contract is keyed by name, not by the later source argument.
    assert values._parse_function(name, "pass\n") is function


@pytest.mark.parametrize("source", [
    "pass\n",
    "value = 1\n",
    "class Helper:\n    pass\n",
    "'module docstring'\ndef helper():\n    pass\n",
])
def test_builtin_helper_factory_rejects_nonfunction_without_caching(source, monkeypatch):
    monkeypatch.setattr(values, "_PARSED", {})
    name = "helper"

    with pytest.raises(TypeError, match="must start with a function definition"):
        values._parse_function(name, source)

    assert name not in values._PARSED
    function = values._parse_function(name, "def helper():\n    pass\n")
    assert isinstance(function, FuncDef)
    assert values._PARSED[name] is function


def test_builtin_helper_factory_passes_closed_world_type_inference():
    from pcc.frontends.python import py_ast, py_lift
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    dependencies = [py_ast, py_lift, values]
    parsed, exports, derived = build_closed_world_context(
        [module.__file__ for module in dependencies],
        [module.__name__ for module in dependencies],
    )
    external = {
        name: value for name, value in exports.items()
        if name != values.__name__
    }
    typed = infer_module(
        parsed[-1], external_exports=external, derived_class_map=derived,
    )
    factory = next(
        stmt for stmt in typed.body
        if isinstance(stmt, FuncDef) and stmt.name == "_parse_function"
    )
    assert factory.return_ty.name == "FuncDef"
