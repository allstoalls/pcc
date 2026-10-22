from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_list_repeat_accepts_dynamic_int_left_operand(tmp_path):
    src = tmp_path / "list_repeat_dynamic_left.py"
    src.write_text(
        textwrap.dedent(
            """
            def repeat(count):
                return count * [None]

            values = repeat(3)
            print(len(values))
            print(values[0] is None)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "list_repeat_dynamic_left.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["3", "True"]
