"""bytes/bytearray ``.partition(sep)`` -> 3-tuple, no-libpython (both tiers).

Added `py_bytes_partition` BOTH tiers (cc `src/py_bytes.c` + pcc-Python port
`py/py_obj_stubs.py`, mirroring `py_str_partition` with same-family parts) plus
frontend dispatch. Returns ``(before, sep, after)`` on the first occurrence,
else ``(copy-of-whole, b'', b'')``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"a=b=c".partition(b"="))
        print(b"key:val".partition(b":"))
        print(b"nosep".partition(b","))
        print(b"=lead".partition(b"="))
        print(b"trail=".partition(b"="))
        print(b"x::y".partition(b"::"))
        ba = bytearray(b"p|q")
        print(ba.partition(b"|"))

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_partition_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bp.py"
    exe = tmp_path / "bp.out"
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
