"""``float()`` of a bignum int must preserve magnitude, no-libpython.

``_to_double`` (coercion_lowering.py) unboxed a boxed ``int`` to ``i64`` before
``sitofp`` — lossy for an arbitrary-precision bignum, so ``float(2**70)``
collapsed to ``0.0`` (small ints and float literals were fine). Fix routes the
boxed-``IntType`` case through ``marshal_from_object(..., FloatType)`` ->
``py_float_to_f64``, which is bignum-aware (``py_bigint_to_double``), matching
the already-correct DynType-pointer branch. Frontend-only.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_float_of_bignum_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "floatbig.py"
    exe = tmp_path / "floatbig.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(float(2 ** 70))
            print(float(10 ** 40))
            print(float(-(2 ** 80)))
            x = 2 ** 100
            print(float(x))
            print((2 ** 70) + 0.5)
            # regressions: small int + negative + float literal unaffected
            print(float(5))
            print(float(-3))
            print(float(3.5))

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
