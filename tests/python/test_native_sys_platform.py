"""``sys.platform`` lowers to ``py_sys_platform_str`` (a constant).

The platform string ("darwin"/"linux"/"win32"/...) is picked at C
compile time, so reading sys.platform takes one runtime call and
allocates a fresh PyStr each time — no CPython import or dynamic
attribute access.
"""
from __future__ import annotations

import re
import subprocess
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
def test_sys_platform_dispatches_to_native(mode):
    program = textwrap.dedent(
        """
        import sys

        def f() -> str:
            return sys.platform
        """
    )
    ir = _compile_to_ll(program, f"plat_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_platform_str" in body, body
    assert "cpy.get.platform" not in body, body
    assert "cpy.import.sys" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_sys_platform_compare_eq(mode):
    program = textwrap.dedent(
        """
        import sys

        def is_darwin() -> bool:
            return sys.platform == "darwin"
        """
    )
    ir = _compile_to_ll(program, f"plat_eq_{mode}", mode=mode)
    body = _function_body(ir, "is_darwin")
    assert body is not None
    assert "@py_sys_platform_str" in body, body
    assert "cpy.get.platform" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_sys_executable_dispatches_to_native(mode):
    program = textwrap.dedent(
        """
        import sys

        def f() -> str:
            return sys.executable
        """
    )
    ir = _compile_to_ll(program, f"sys_executable_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_executable_str" in body, body
    assert "cpy.get.executable" not in body, body
    assert "cpy.import.sys" not in body, body


def test_sys_executable_can_feed_native_os_path():
    program = textwrap.dedent(
        """
        import os
        import sys

        def f() -> str:
            return os.path.dirname(sys.executable)
        """
    )
    ir = _compile_to_ll(program, "sys_executable_os_path", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_sys_executable_str" in body, body
    assert "@py_os_path_dirname" in body, body
    assert "cpy.fn.dirname" not in body, body
    assert "cpy.get.executable" not in body, body


def test_sys_executable_runtime_uses_program_argv0(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            import sys

            def main() -> None:
                print(sys.executable.endswith("prog.out"))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "True\n"
