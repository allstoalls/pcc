"""``os.path.dirname(x)`` lowers to ``py_os_path_dirname``.

Mirrors the existing ``basename`` / ``join`` dispatch in
``_emit_native_os_path_call``. The helper itself is dual-implemented:
``src/py_os_path.c`` (C baseline) + ``py/py_os_path.py`` (pcc-Python
port) — both are exported as the same ``py_os_path_dirname`` C ABI
symbol so the runtime archive variant doesn't matter.

Behavioural correctness is exercised separately through the runtime
oracle programs; this file only asserts that codegen routes the call
to the native helper instead of falling back to ``py_cpy_getattr`` +
``py_cpy_call1``.
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
def test_dirname_dispatches_to_native_helper(mode):
    program = textwrap.dedent(
        """
        import os.path

        def f(p: str) -> str:
            return os.path.dirname(p)
        """
    )
    ir = _compile_to_ll(program, f"path_dirname_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_dirname" in body, body
    assert "cpy.get.dirname" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dirname_via_intermediate_local(mode):
    """``os.path.dirname`` accepts a local-bound intermediate."""
    program = textwrap.dedent(
        """
        import os.path

        def g(a: str, b: str) -> str:
            joined: str = os.path.join(a, b)
            return os.path.dirname(joined)
        """
    )
    ir = _compile_to_ll(program, f"path_dirname_chain_{mode}", mode=mode)
    body = _function_body(ir, "g")
    assert body is not None
    assert "@py_os_path_dirname" in body, body
    assert "@py_os_path_join" in body, body
    assert "cpy.get.dirname" not in body, body
    assert "cpy.get.join" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dirname_directly_chained_with_native_call(mode):
    """``os.path.dirname(os.path.abspath(p))`` — chained native calls.
    The arg-stays-native guard now recognises that ``os.path.X(...)``
    returns a pcc-native PyObject*, so the outer call dispatches
    natively without forcing fallback."""
    program = textwrap.dedent(
        """
        import os.path

        def h(p: str) -> str:
            return os.path.dirname(os.path.abspath(p))
        """
    )
    ir = _compile_to_ll(program, f"path_chain_direct_{mode}", mode=mode)
    body = _function_body(ir, "h")
    assert body is not None
    assert "@py_os_path_dirname" in body, body
    assert "@py_os_path_abspath" in body, body
    assert "cpy.get.dirname" not in body, body
    assert "cpy.get.abspath" not in body, body
    assert "cpy.get.path" not in body, body
