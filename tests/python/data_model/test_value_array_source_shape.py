from __future__ import annotations

import pytest


def _infer(source: str):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer

    ast_mod = parse_and_lift(source, "<value-array>", "value_array_mod")
    return type_infer.infer_module(ast_mod)


def _point_prefix() -> str:
    return (
        "import pcc\n\n"
        "@pcc.valueclass\n"
        "class Point:\n"
        "    x: float\n"
        "    y: float\n\n"
    )


def test_value_array_host_oracle_constructs_indexes_and_fails_closed():
    import pcc

    @pcc.valueclass
    class Point:
        x: float
        y: float

    values = pcc.array[Point, 2](Point(1.0, 2.0), Point(3.0, 4.0))
    assert len(values) == 2
    assert values[0] == Point(1.0, 2.0)
    assert values[-1] == Point(3.0, 4.0)
    with pytest.raises(IndexError):
        _ = values[2]
    with pytest.raises(OverflowError):
        _ = values[1 << 100]
    with pytest.raises(TypeError):
        pcc.array[Point, 2](Point(1.0, 2.0))
    with pytest.raises(TypeError):
        pcc.array[Point, 2](Point(1.0, 2.0), object())


def test_value_array_annotation_constructor_and_index_infer_one_shared_type():
    from pcc.frontends.python.py_ast import FuncDef, Return, ValueArrayType

    typed = _infer(
        _point_prefix()
        + "def pick(values: pcc.array[Point, 2], index: int) -> Point:\n"
        + "    return values[index]\n\n"
        + "values = pcc.array[Point, 2](Point(1.0, 2.0), Point(3.0, 4.0))\n"
        + "picked = pick(values, 1)\n"
    )
    pick = next(
        stmt for stmt in typed.body if isinstance(stmt, FuncDef) and stmt.name == "pick"
    )
    array_ty = pick.args[0].annotation
    assert isinstance(array_ty, ValueArrayType)
    assert array_ty.length == 2
    assert array_ty.elem.name == "Point"
    assert array_ty.elem.valueclass is True
    ret = next(stmt for stmt in pick.body if isinstance(stmt, Return))
    assert ret.value.ty == array_ty.elem
    constructor_assign = typed.body[-2]
    assert constructor_assign.value.ty == array_ty


@pytest.mark.parametrize(
    ("surface", "message"),
    [
        ("pcc.array[Point]", "needs an element type and literal length"),
        ("pcc.array[Point, 0]", "length must be between 1 and 7"),
        ("pcc.array[Point, 8]", "length must be between 1 and 7"),
        ("pcc.array[Point, int]", "length must be an integer literal"),
    ],
)
def test_value_array_annotation_rejects_invalid_shape(surface: str, message: str):
    from pcc.frontends.python.types import PyFrontendError

    source = (
        _point_prefix()
        + f"def take(values: {surface}) -> Point:\n    return Point(1.0, 2.0)\n"
    )
    with pytest.raises(PyFrontendError, match=message):
        _infer(source)


def test_value_array_rejects_ordinary_class_element():
    from pcc.frontends.python.types import PyFrontendError

    source = (
        "import pcc\n\n"
        "class Point:\n"
        "    x: float\n\n"
        "def take(values: pcc.array[Point, 2]) -> Point:\n"
        "    return values[0]\n"
    )
    with pytest.raises(PyFrontendError, match="element type must be a valueclass"):
        _infer(source)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ("Point(1.0, 2.0)", "expects exactly 2 elements"),
        ("Point(1.0, 2.0), 3", "element 2 has type int, expected Point"),
    ],
)
def test_value_array_constructor_rejects_count_and_element_mismatch(
    arguments: str,
    message: str,
):
    from pcc.frontends.python.types import PyFrontendError

    source = _point_prefix() + f"values = pcc.array[Point, 2]({arguments})\n"
    with pytest.raises(PyFrontendError, match=message):
        _infer(source)
