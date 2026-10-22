"""``math.factorial(n)`` native, bignum-correct, no-libpython.

Was a libpython fallback. Added a frontend-inline lowering in `native_math.py`
(registered as the `math.factorial` alias): a product loop via the bignum-aware
`py_int_mul` (n! overflows i64 at n>=21), with a negative-arg ValueError guard.
No runtime change.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_math_factorial_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "mf.py"
    exe = tmp_path / "mf.out"
    src.write_text(textwrap.dedent("""
        import math

        def main() -> None:
            print(math.factorial(5))
            print(math.factorial(0))
            print(math.factorial(1))
            print(math.factorial(10))
            print(math.factorial(20))
            print(math.factorial(25))
            print(math.gcd(12, 8))
            try:
                math.factorial(-3)
            except ValueError as e:
                print("VE:", type(e).__name__)

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
