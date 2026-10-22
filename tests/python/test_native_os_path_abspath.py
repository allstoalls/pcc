"""``os.path.abspath(p)`` lowers to ``py_os_path_abspath``.

The native helper is a *simplified* abspath: absolute paths are
returned as-is; relative paths get cwd prepended via ``getcwd``. It
does NOT collapse ``.``, ``..``, or repeated ``//`` (CPython's
posixpath.abspath does, via posixpath.normpath). Pipeline.py callers
don't feed unnormalized inputs, so the simpler shape is sufficient
for the bootstrap closure; the full normpath algorithm is a follow-
up if downstream hashes/dict-keys regress.
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
def test_abspath_dispatches_to_native(mode):
    program = textwrap.dedent(
        """
        import os.path

        def f(p: str) -> str:
            return os.path.abspath(p)
        """
    )
    ir = _compile_to_ll(program, f"abspath_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_path_abspath" in body, body
    assert "cpy.get.abspath" not in body, body
