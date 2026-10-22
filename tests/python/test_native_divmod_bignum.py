"""``divmod()`` on bignum ints must be exact, no-libpython.

The integer ``divmod`` path reduced both operands to ``i64`` via
``_emit_expr_as_i64`` before the floor-div/mod — lossy for a bignum, so
``divmod(10**20, 7)`` returned ``(0, 0)``. Fix routes through
``py_obj_floordiv``/``py_obj_mod`` (int//int delegates to the bignum-aware
``py_int_floordiv``/``py_int_mod``), matching the ``//`` and ``%`` operators,
with the same NULL->ZeroDivisionError surfacing. Frontend-only.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_divmod_bignum_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "divmodbig.py"
    exe = tmp_path / "divmodbig.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(divmod(10 ** 20, 7))
            print(divmod(2 ** 70, 3))
            print(divmod(-(10 ** 20), 7))      # floor semantics across bignum
            print(divmod(10 ** 30, 10 ** 10))
            # regressions: small int + negative + float
            print(divmod(17, 5))
            print(divmod(-17, 5))
            print(divmod(7.5, 2))
            # zero division stays catchable
            try:
                divmod(10 ** 20, 0)
            except ZeroDivisionError as e:
                print("ZD:", type(e).__name__)

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
