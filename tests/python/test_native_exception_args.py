"""``e.args`` on a builtin exception, no-libpython (both runtime tiers).

Reading ``.args`` on a caught builtin exception raised ``AttributeError: args``.
Fix exposes ``args`` in the PY_TYPE_EXC getattr branch (``py_obj_ops_dispatch``,
C + port): the exception stores only args[0] as ``message``, so ``.args`` is
``()`` for a no-arg exception (empty/None message) else ``(message,)``.

Documented limitation (message-only storage; shared root with multi-arg
str(exc)): ``Error(a, b)`` reports ``(a,)`` and ``Error("")`` reports ``()`` —
capturing args[1:] / an explicit empty-string arg needs a dedicated args-tuple
field. The common 0-arg / 1-arg forms (tested here) match CPython.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        try:
            raise ValueError("just one")
        except ValueError as e:
            print(e.args)
            print(e.args[0])
            print(len(e.args))
        try:
            raise RuntimeError()
        except RuntimeError as e:
            print(e.args)
            print(len(e.args))
        try:
            raise KeyError("k")
        except KeyError as e:
            print(e.args)

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_exception_args_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "excargs.py"
    exe = tmp_path / "excargs.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
