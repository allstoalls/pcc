"""``round(int, ndigits)`` returns an int, no-libpython.

The 2-arg `round` always returned `py_float_round_ndigits` (a float), so
`round(12345, -2)` gave `12300.0` instead of `12300` (CPython `round(int, n)`
returns an int). The VALUE was already correct (incl. banker's rounding); the
fix converts the result back to int when the first arg is int-typed
(`call_expression_lowering.py`). Frontend-only; exact for |value| < 2**53.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_round_int_ndigits_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "rnd.py"
    exe = tmp_path / "rnd.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(round(12345, -2))
            print(round(12345, -1))
            print(round(15, -1))
            print(round(25, -1))
            print(round(12345, -3))
            print(round(2, -1))
            print(round(12345, 2))
            print(round(-12345, -2))
            # regressions: round(x) int, round(float, n) float, banker's
            print(round(7))
            print(round(3.14159, 2))
            print(round(2.5))
            print(round(3.5))
            print(round(2.675, 2))

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
