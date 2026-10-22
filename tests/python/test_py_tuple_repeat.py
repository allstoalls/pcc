from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_tuple_repeat_accepts_dynamic_int_operand_without_libpython(tmp_path):
    src = tmp_path / "tuple_repeat_dynamic.py"
    src.write_text(
        textwrap.dedent(
            """
            def repeat(axis):
                values = (7,) * axis + (9,)
                return values

            out = repeat(3)
            print(len(out))
            print(out[0])
            print(out[3])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "tuple_repeat_dynamic.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["4", "7", "9"]


def test_tuple_repeat_accepts_left_int_operand_without_libpython(tmp_path):
    src = tmp_path / "tuple_repeat_left_int.py"
    src.write_text(
        textwrap.dedent(
            """
            values = 2 * (5,)
            print(len(values))
            print(values[1])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "tuple_repeat_left_int.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["2", "5"]
