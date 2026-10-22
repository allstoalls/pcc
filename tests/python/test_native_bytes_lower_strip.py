"""bytes/bytearray ``.lower()`` and no-arg ``.strip()``, no-libpython (both tiers).

These weren't dispatched natively. Added `py_bytes_lower` / `py_bytes_strip`,
mirrored across BOTH tiers — the cc runtime (`src/py_bytes.c`) and the
pcc-Python port (`py/py_obj_stubs.py`, which provides the bytes methods in the
default no-libpython archive) — plus frontend dispatch. `lower` maps ASCII A-Z;
`strip` (no-arg) drops leading/trailing ASCII whitespace. `strip(chars)` still
falls back.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(b"ABC".lower())
        print(b"Hello World".lower())
        print(b"  hi  ".strip())
        print(b"\\t\\n x \\r\\n".strip())
        print(b"nows".strip())
        print(b"".strip())
        print(b"abc".upper())
        ba = bytearray(b"  Mixed  ")
        print(ba.lower())
        print(ba.strip())

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bytes_lower_strip_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bm.py"
    exe = tmp_path / "bm.out"
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
