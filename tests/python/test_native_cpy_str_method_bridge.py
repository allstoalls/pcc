"""CPython string results bridge back into native str method dispatch."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
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


def test_cpython_str_receiver_bridges_for_native_methods():
    program = textwrap.dedent(
        """
        import subprocess

        def f():
            return subprocess.check_output(
                ["uname", "-m"], encoding="utf-8",
            ).strip().lower().split("-")
        """
    )
    ir = _compile_to_ll(program, "cpy_str_receiver_bridge", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "@py_str_lower" in body, body
    assert "@py_str_split" in body, body
    assert "cpy.fn.lower" not in body, body
    assert "cpy.fn.split" not in body, body


def test_native_listdir_loop_var_uses_native_str_method():
    program = textwrap.dedent(
        """
        import os

        def f(path: str) -> int:
            count = 0
            for name in os.listdir(path):
                if name.endswith(".py"):
                    count = count + 1
            return count
        """
    )
    ir = _compile_to_ll(program, "native_listdir_loop_var_str", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_listdir" in body, body
    assert "@py_str_endswith" in body, body
    assert "cpy.fn.listdir" not in body, body
    assert "cpy.fn.endswith" not in body, body


def test_cpython_str_result_equality_bridges_to_native_compare():
    program = textwrap.dedent(
        """
        import subprocess

        def f() -> bool:
            return subprocess.check_output(
                ["uname", "-m"], encoding="utf-8",
            ).strip() == "arm64"
        """
    )
    ir = _compile_to_ll(program, "cpy_str_compare_bridge", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "@py_obj_type_tag" in body, body
    assert "@py_str_eq" in body, body
    assert "cpy.fn.__eq__" not in body, body
    assert "cpy.attr.__eq__" not in body, body
