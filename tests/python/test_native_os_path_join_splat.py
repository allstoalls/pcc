"""``os.path.join(a, *parts[, "trail"])`` lowers natively.

The native dispatch builds a fresh PyList by emitting one
``py_list_append`` for each positional arg and one ``py_list_extend``
for each splat arg, then hands the list to ``py_os_path_join``.

Negative case: a splat whose inner type is unknown (DynType) keeps
falling back, so the dispatch stays narrow and provably correct.
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
def test_join_splat_list(mode):
    program = textwrap.dedent(
        """
        import os.path

        def f(root: str, parts: list[str]) -> str:
            return os.path.join(root, *parts)
        """
    )
    ir = _compile_to_ll(program, f"join_splat_list_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_join" in body, body
    assert "@py_list_extend" in body, body
    assert "cpy.get.join" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_join_splat_tuple_with_trail(mode):
    program = textwrap.dedent(
        """
        import os.path

        def f(a: str, parts: tuple[str, ...]) -> str:
            return os.path.join(a, *parts, "trail.txt")
        """
    )
    ir = _compile_to_ll(program, f"join_splat_tuple_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_join" in body, body
    assert "@py_list_extend" in body, body
    # The trailing literal should still get py_list_append.
    assert "@py_list_append" in body, body
    assert "cpy.get.join" not in body, body


def test_join_splat_dyn_arg_falls_back():
    """Splat of a DynType arg can't be proven safe (might be a CPython
    value), so the dispatch declines and falls back to the cpy path.
    Locks the safety bound."""
    program = textwrap.dedent(
        """
        import os.path

        def f(a, parts):
            return os.path.join(a, *parts)
        """
    )
    ir = _compile_to_ll(program, "join_splat_dyn", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_join" not in body, body
    assert "cpy.fn.join" in body, body


def test_join_bridges_cpython_path_value():
    """A CPython-backed path value can still feed the native join helper.

    This locks the chain-breaker: the CPython value is converted at the
    path argument boundary instead of forcing the whole os.path.join call
    back through CPython.
    """
    program = textwrap.dedent(
        """
        import os
        import pathlib

        def f() -> str:
            tmp = pathlib.PurePath("tmp")
            return os.path.join(tmp, "x.txt")
        """
    )
    ir = _compile_to_ll(program, "join_cpy_path_value", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "@py_os_path_join" in body, body
    assert "cpy.fn.join" not in body, body
