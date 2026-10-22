from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_range_value_materializes_native_sequence_for_zip(tmp_path):
    src = tmp_path / "range_value_zip.py"
    src.write_text(
        textwrap.dedent(
            """
            axes = range(3)
            total = 0
            for axis, width in zip(axes, [10, 20, 30]):
                total += axis + width
            print(total)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "range_value_zip.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "63\n"


def test_range_value_supports_negative_step(tmp_path):
    src = tmp_path / "range_value_negative.py"
    src.write_text(
        textwrap.dedent(
            """
            values = range(5, 0, -2)
            print(len(values))
            print(values[0])
            print(values[2])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "range_value_negative.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["3", "5", "1"]


def test_range_builtin_alias_stays_native_no_libpython(tmp_path):
    src = tmp_path / "range_value_alias.py"
    src.write_text(
        textwrap.dedent(
            """
            saved_range = range
            values = saved_range(1, 6, 2)
            print(values[0] + values[1] + values[2])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "range_value_alias.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "9\n"
