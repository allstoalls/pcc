from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_module_global_augassign_uses_module_storage(tmp_path):
    src = tmp_path / "module_augassign.py"
    src.write_text(
        textwrap.dedent(
            """
            xs = [1]
            xs += [2, 3]

            print(len(xs))
            print(xs[2])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "module_augassign.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["3", "3"]
