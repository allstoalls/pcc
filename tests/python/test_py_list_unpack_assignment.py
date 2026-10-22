from __future__ import annotations

import subprocess
import textwrap


def test_list_unpack_assignment_target_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "list_unpack_assign.py"
    src.write_text(textwrap.dedent(
        """
        values = [1, 2]
        [left, right] = values
        print(left)
        print(right)
        """
    ), encoding="utf-8")
    exe = tmp_path / "list_unpack_assign.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\n2\n"
