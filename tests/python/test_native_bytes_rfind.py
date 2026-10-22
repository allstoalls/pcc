"""bytes/bytearray ``.rfind()`` (highest match index), no-libpython (both tiers).

Mirrors `py_bytes_find` but scans backward; byte-based, returns an int index
(or -1). Added in both tiers — cc (`src/py_bytes.c`) and the pcc-Python port
(`py/py_obj_stubs.py`, the bytes-method source for the default archive) — plus
frontend dispatch. Accepts a sub-bytes or a single byte value; `b"x".rfind(b"")`
returns len, like CPython.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"abcabc".rfind(b"bc"))
        print(b"hello".rfind(b"l"))
        print(b"hello".rfind(b"x"))
        print(b"aaa".rfind(b"a"))
        print(b"abc".rfind(b""))
        print(b"hello".rfind(108))
        print(b"abcabc".find(b"bc"))
        ba = bytearray(b"x.y.z")
        print(ba.rfind(b"."))

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_rfind_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "brf.py"
    exe = tmp_path / "brf.out"
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
