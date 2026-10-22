from __future__ import annotations

import re
import subprocess
import sys
import textwrap

import pytest


def _source() -> str:
    return textwrap.dedent("""
        import pcc

        @pcc.valueclass
        class Point:
            x: float
            y: float

        def make_points() -> pcc.array[Point, 2]:
            return pcc.array[Point, 2](Point(1.0, 2.0), Point(3.0, 4.0))

        def pick_second(values: pcc.array[Point, 2]) -> Point:
            return values[1]

        point = pick_second(make_points())
        print(point.x + point.y)
        """)


def _generate_ir(source: str) -> str:
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    ast_mod = parse_and_lift(source, "<value-array-projection>", "value_array_mod")
    typed = type_infer.infer_module(ast_mod)
    cg = layer1.L1CodeGen(typed, ir_scaffold_mode="on")
    return str(cg.generate(typed))


def test_value_array_uses_nested_aggregate_abi_on_self(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = _source()
    ir_text = _generate_ir(source)
    point_payload = r"\{ double, double \}"
    array_payload = rf"\{{ {point_payload}, {point_payload} \}}"

    assert re.search(
        rf"define external {array_payload} @user_value_array_mod_make_points\(\)",
        ir_text,
    ), ir_text
    assert re.search(
        rf"define external {point_payload} @user_value_array_mod_pick_second"
        rf"\({array_payload} %values\)",
        ir_text,
    ), ir_text
    for name in ("make_points", "pick_second"):
        body_start = ir_text.index(f"@user_value_array_mod_{name}")
        body_end = ir_text.index("\n}", body_start)
        body = ir_text[body_start:body_end]
        assert "@py_list_new" not in body, body
        assert "@py_instance_new" not in body, body
        assert "@py_valuebox_new" not in body, body
    assert "extractvalue { { double, double }, { double, double } }" in ir_text

    src = tmp_path / "value_array_projection.py"
    exe = tmp_path / "value_array_projection"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout == "7.0\n"


def test_value_array_checked_index_and_element_escape_match_host_default_and_explicit_self(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: float
            y: float

        def make_points() -> pcc.array[Point, 2]:
            return pcc.array[Point, 2](Point(1.0, 2.0), Point(3.0, 4.0))

        def pick(values: pcc.array[Point, 2], index: int) -> Point:
            return values[index]

        def escape(values: pcc.array[Point, 2], index: int) -> Any:
            return values[index]

        print(pick(make_points(), -1).x)
        print(pick(make_points(), 0).y)
        boxed = escape(make_points(), 1)
        alias = boxed
        print(boxed.x + boxed.y)
        print(boxed is alias)
        try:
            pick(make_points(), 2)
        except IndexError:
            print("index-error")
        try:
            pick(make_points(), -3)
        except IndexError:
            print("negative-index-error")
        try:
            pick(make_points(), 1 << 100)
        except OverflowError:
            print("overflow-error")
        """)
    ir_text = _generate_ir(source)
    pick_start = ir_text.index("@user_value_array_mod_pick")
    pick_end = ir_text.index("\n}", pick_start)
    pick_body = ir_text[pick_start:pick_end]
    escape_start = ir_text.index("@user_value_array_mod_escape")
    escape_end = ir_text.index("\n}", escape_start)
    escape_body = ir_text[escape_start:escape_end]

    assert "@py_list_new" not in pick_body, pick_body
    assert "@py_instance_new" not in pick_body, pick_body
    assert "@py_valuebox_new" not in pick_body, pick_body
    assert "@py_valuebox_new" in escape_body, escape_body

    src = tmp_path / "value_array_checked_index.py"
    src.write_text(source, encoding="utf-8")
    outputs = []
    host = subprocess.run(
        [sys.executable, str(src)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    outputs.append(host.stdout)
    for backend in (None, "self"):
        exe = tmp_path / f"value_array_checked_index_{backend}"
        compile_python(
            str(src),
            str(exe),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend=backend,
        )
        proc = subprocess.run(
            [str(exe)],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        outputs.append(proc.stdout)

    expected = (
        "3.0\n"
        "2.0\n"
        "7.0\n"
        "True\n"
        "index-error\n"
        "negative-index-error\n"
        "overflow-error\n"
    )
    assert outputs == [expected, expected, expected]


def test_value_array_itself_cannot_escape_to_any():
    from pcc.frontends.python.codegen.errors import L1CodegenError

    source = textwrap.dedent("""
        from typing import Any

        import pcc

        @pcc.valueclass
        class Point:
            x: float
            y: float

        def bad_escape() -> Any:
            return pcc.array[Point, 2](Point(1.0, 2.0), Point(3.0, 4.0))
        """)
    with pytest.raises(
        L1CodegenError,
        match="pcc.array cannot cross an object or Any boundary",
    ):
        _generate_ir(source)
