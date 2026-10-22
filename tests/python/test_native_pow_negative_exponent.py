"""``pow(int, negative_int)`` must return a float, no-libpython.

The 2-arg ``pow()`` builtin lowered the int case via ``_emit_binop_int("**")``,
which force-unboxes the ``py_int_pow`` result back to i64 — but a negative
exponent makes ``py_int_pow`` return a *float* (``pow(2, -2) == 0.25``), so the
i64 unbox truncated it to ``0``. The ``**`` operator was already correct (it
keeps the object result). Fix routes the pow() builtin through the same
``_emit_runtime_int_binop_value("**")`` path. Frontend-only.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_pow_negative_exponent_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "pow.py"
    exe = tmp_path / "pow.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(pow(2, -2))
            print(pow(10, -1))
            print(pow(2, -3))
            e = -2
            print(pow(2, e))            # runtime-variable negative exponent
            # regressions: non-negative exponents stay int + usable as int
            print(pow(2, 10))
            print(pow(2, 0))
            y = pow(3, 4)
            print(y + 1)
            print(pow(5, 3))

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


def test_int_literal_pow_folds_without_runtime_pow(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "pow_literal_fold.py"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                print(7 % (2 ** 32))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "pow_literal_fold.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "7\n"

    ll = tmp_path / "pow_literal_fold.ll"
    compile_python(
        str(src),
        str(ll),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    assert "call ptr @py_int_pow" not in ir_text
    assert "call ptr (ptr, ptr) @py_int_pow" not in ir_text
