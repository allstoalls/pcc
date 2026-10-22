from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_object_local_rebind_updates_codegen_type_for_augassign_self_backend(tmp_path):
    src = tmp_path / "object_local_rebind.py"
    src.write_text(
        textwrap.dedent(
            """
            def build():
                header = ["{"]
                header.append("'x': 1")
                header.append("}")
                header = "".join(header)
                header += " "
                return header

            print(build())
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "object_local_rebind.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "{'x': 1} \n"
