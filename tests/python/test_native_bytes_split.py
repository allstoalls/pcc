"""bytes/bytearray ``.split(sep)`` -> list of same-family pieces, no-libpython.

Added `py_bytes_split` BOTH tiers (cc `src/py_bytes.c` + pcc-Python port
`py/py_obj_stubs.py`) mirroring `py_str_split` but producing bytes/bytearray
parts, plus frontend dispatch. Non-empty separator only; empty separator raises
ValueError (catchable); no-arg whitespace split still falls back.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"a,b,c".split(b","))
        print(b"a,,b".split(b","))
        print(b"abc".split(b","))
        print(b"x::y::z".split(b"::"))
        print(b",a,".split(b","))
        print(b"".split(b","))
        ba = bytearray(b"p-q-r")
        print(ba.split(b"-"))
        try:
            b"abc".split(b"")
        except ValueError as e:
            print("ZD:", type(e).__name__)

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_split_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bs.py"
    exe = tmp_path / "bs.out"
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
