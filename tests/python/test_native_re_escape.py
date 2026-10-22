"""Native ``re.escape(s)`` in no-libpython mode.

``re.escape`` previously fell back to libpython; it now lowers to
``py_re_escape`` (C runtime + pcc-Python port), escaping the CPython 3.7+
special-character set (``()[]{}?*+-|^$\\.&~#`` plus whitespace) and leaving
alphanumerics / ``_`` / ``=`` alone.  Used by package code that builds regexes
from literal text.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_re_escape_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "esc.py"
    exe = tmp_path / "esc.out"
    program = textwrap.dedent("""
        import re

        def main() -> None:
            print(re.escape("a.b*c"))
            print(re.escape("1+1=2"))
            print(re.escape("plain_id"))
            print(re.escape("a b\\tc"))
            print(re.escape("(grp)[set]{n}"))
            print(re.escape(""))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    # The strict libpython_mode="off" compile already proves no fallback; this
    # asserts the escaped output matches CPython exactly.
    assert _run(exe) == cpython
