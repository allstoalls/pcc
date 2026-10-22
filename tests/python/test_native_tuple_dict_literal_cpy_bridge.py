"""Tuple/dict literals bridge CPython values at native container boundaries.

These tests lock the same chain-breaker as the list literal tests: a
CPython-backed value can be converted into a pcc object and inserted into
a pcc-native tuple/dict without forcing the whole literal onto the legacy
CPython construction path.

Safety bounds stay narrow: a terminal splat of a CPython iterable still falls
back, and a single CPython-backed dict key keeps the CPython dict path so key
identity and hash semantics are not silently changed by recursive marshalling.
Multi-pair CPython-key displays fail closed until each insertion can happen
before evaluation of the following pair.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        libpython_mode="auto",
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


@pytest.mark.parametrize("mode", ["off", "on"])
def test_tuple_literal_with_cpy_value_bridges(mode):
    program = textwrap.dedent(
        """
        import decimal

        def f() -> tuple:
            x = decimal.Decimal("1")
            return (x, "tail")
        """
    )
    ir = _compile_to_ll(program, f"tuple_lit_bridge_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_tuple_new" in body, body
    assert "@py_tuple_set_item" in body, body
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "cpy.tuple" not in body, body


def test_tuple_splat_cpy_iterable_still_falls_back():
    # ``os.environ`` is native now, so its keys are a pcc list; a decimal's
    # DecimalTuple is still a CPython iterable.
    program = textwrap.dedent(
        """
        import decimal

        def f() -> tuple:
            cpy_iter = decimal.Decimal("1.5").as_tuple()
            return (*cpy_iter,)
        """
    )
    ir = _compile_to_ll(program, "tuple_splat_cpy", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    assert "cpy.list.extend" in body, body
    assert "cpy.tuple" in body, body
    assert "@py_cpy_to_pcc_obj" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_tuple_concat_with_dyn_rhs_stays_native(mode):
    program = textwrap.dedent(
        """
        def f(a: tuple[str, ...], b) -> object:
            return a + tuple(b)
        """
    )
    ir = _compile_to_ll(program, f"tuple_concat_native_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_tuple_concat" in body, body
    assert "cpy.tup.add" not in body, body
    assert "@py_cpy_call1" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dict_literal_with_cpy_value_bridges(mode):
    program = textwrap.dedent(
        """
        import decimal

        def f() -> dict:
            x = decimal.Decimal("1")
            return {"tmp": x, "suffix": "x"}
        """
    )
    ir = _compile_to_ll(program, f"dict_lit_bridge_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_new" in body, body
    assert "@py_dict_set" in body, body
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "@py_cpy_setitem" not in body, body


def test_dict_literal_cpy_key_still_falls_back():
    program = textwrap.dedent(
        """
        import decimal

        def f() -> dict:
            x = decimal.Decimal("1")
            return {x: "value"}
        """
    )
    ir = _compile_to_ll(program, "dict_lit_cpy_key", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    assert "cpy.dict" in body, body
    assert "@py_cpy_setitem" in body, body
    assert "@py_cpy_to_pcc_obj" not in body, body
