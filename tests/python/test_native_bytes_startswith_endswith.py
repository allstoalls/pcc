"""bytes/bytearray ``.startswith()`` / ``.endswith()`` (prefix or tuple),
no-libpython.

These weren't dispatched natively at all (``AttributeError: startswith``). The
runtime ``py_str_startswith`` / ``py_str_endswith`` already read the receiver
and each tuple element through ``stringlike_bytes()`` (bytes/bytearray-aware)
and implement the tuple-of-prefixes case; the fix is pure frontend dispatch in
``method_call_expression_lowering.py`` (box the i64 0/1 as a bool object).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_bytes_startswith_endswith_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "bsw.py"
    exe = tmp_path / "bsw.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(b"hello".startswith((b"he", b"xx")))
            print(b"hello".endswith((b"xx", b"lo")))
            print(b"hello".startswith((b"xx", b"yy")))
            print(b"hello".startswith(b"he"))
            print(b"hello".endswith(b"lo"))
            print(b"hello".startswith(b"xx"))
            ba = bytearray(b"world")
            print(ba.startswith(b"wor"))
            print(ba.endswith((b"ld", b"zz")))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
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
