"""bytes/bytearray ``.count(sub)`` (non-overlapping occurrences), no-libpython.

Byte-based, mirrors `py_bytes_find`'s needle handling but tallies every
non-overlapping match; returns an i64 count that the DynType-width-64 marshal
path boxes as an int (same flow as `.find`/`.rfind`). Added in both tiers — cc
(`src/py_bytes.c`) and the pcc-Python port (`py/py_obj_stubs.py`, the
bytes-method source for the default archive) — plus frontend dispatch. Accepts
a sub-bytes or a single byte value. CPython semantics exercised here:

* non-overlapping: ``b"aaaa".count(b"aa") == 2``
* empty sub-bytes counts len+1 positions: ``b"abc".count(b"") == 4``
* single byte value counts each matching byte
* no match / needle longer than receiver -> 0
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"banana".count(b"a"))
        print(b"banana".count(b"na"))
        print(b"aaaa".count(b"aa"))
        print(b"aaaaa".count(b"aa"))
        print(b"banana".count(b"z"))
        print(b"mississippi".count(b"ss"))
        print(b"abc".count(b""))
        print(b"".count(b""))
        print(b"ab".count(b"abc"))
        print(b"aXbXcX".count(88))
        ba = bytearray(b"banana")
        print(ba.count(b"a"))
        print(ba.count(97))

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_count_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bcount.py"
    exe = tmp_path / "bcount.out"
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
