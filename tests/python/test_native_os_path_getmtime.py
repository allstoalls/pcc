"""``os.path.getmtime(p)`` lowers to ``py_os_path_getmtime``.

Returns the boxed PyObject* form of the file's last-modification time
in seconds since the epoch, mirroring CPython's API. The helper is
dual-implemented (``src/py_os_path.c`` + ``py/py_os_path.py``); both
funnel through the new ``py_path_stat_mtime`` substrate primitive
which keeps struct timespec layout out of the pcc-Python source.
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
def test_getmtime_dispatches_to_native(mode):
    program = textwrap.dedent(
        """
        import os.path

        def f(p: str) -> float:
            return os.path.getmtime(p)
        """
    )
    ir = _compile_to_ll(program, f"getmtime_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_getmtime" in body, body
    assert "cpy.get.getmtime" not in body, body
