from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_nested_func_statement_is_noop_after_hoist_self_backend(tmp_path):
    src = tmp_path / "nested_func_statement.py"
    src.write_text(
        textwrap.dedent(
            """
            def outer(value):
                def convert(name, local=value):
                    return str(local) + name
                return convert("!")

            print(outer(7))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "nested_func_statement.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "7!\n"
