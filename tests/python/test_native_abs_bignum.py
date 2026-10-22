"""``abs()`` on a bignum int must preserve precision, no-libpython.

The typed-``int`` ``abs()`` lowering reduced its argument to ``i64`` and did the
abs on that native value — lossy for an arbitrary-precision bignum, so
``abs(-(10**40))`` collapsed to ``0`` (tagged small ints and floats were fine).
Fix routes the ``IntType`` branch of ``_emit_abs_builtin`` through the object
runtime ``py_obj_abs`` (the same bignum-correct path ``DynType`` uses), keeping
the exact i64 path only for ``bool`` (always 0/1). Frontend-only.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_abs_bignum_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "absbig.py"
    exe = tmp_path / "absbig.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(abs(-(10 ** 40)))
            print(abs(10 ** 40))
            print(abs(-(2 ** 70)))
            print(abs(2 ** 100))
            # regressions: tagged small int + float + zero unaffected
            print(abs(-5))
            print(abs(5))
            print(abs(-3.5))
            print(abs(0))

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
