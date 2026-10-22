"""``os.path.isfile(p)`` / ``os.path.isdir(p)`` lower to native helpers.

Both wrap ``py_path_stat_kind`` (a low-level C primitive in
``py_os_substrate.c`` that exposes a platform-portable struct stat
classifier — 0/1/2/3 for missing/file/dir/other). The user-visible
helpers themselves are dual-implemented:
- ``src/py_os_path.c`` for the C baseline archive
- ``py/py_os_path.py`` for the pcc-Python port archive

Both export the same C ABI symbol so the runtime variant doesn't
matter at the call site.
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


@pytest.mark.parametrize("method,helper", [
    ("isfile", "py_os_path_isfile"),
    ("isdir",  "py_os_path_isdir"),
])
@pytest.mark.parametrize("mode", ["off", "on"])
def test_dispatches_to_native_helper(method, helper, mode):
    program = textwrap.dedent(
        f"""
        import os.path

        def f(p: str) -> bool:
            return os.path.{method}(p)
        """
    )
    ir = _compile_to_ll(program, f"{method}_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert f"@{helper}" in body, body
    assert f"cpy.get.{method}" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_isfile_under_truthy_check(mode):
    """``if os.path.isfile(p):`` must keep the native dispatch even
    inside a control-flow predicate context (truthy conversion is
    applied to the i32 return value, not the abstract method shape)."""
    program = textwrap.dedent(
        """
        import os.path

        def g(p: str) -> int:
            if os.path.isfile(p):
                return 1
            return 0
        """
    )
    ir = _compile_to_ll(program, f"isfile_truthy_{mode}", mode=mode)
    body = _function_body(ir, "g")
    assert body is not None
    assert "@py_os_path_isfile" in body, body
    assert "cpy.get.isfile" not in body, body
