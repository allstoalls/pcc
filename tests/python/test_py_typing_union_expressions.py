from __future__ import annotations

import subprocess
import textwrap


def test_pep604_type_union_expression_compiles_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typing_union_expr.py"
    src.write_text(textwrap.dedent(
        """
        class Left:
            pass

        class Right:
            pass

        Alias = Left | Right
        print("ok")
        """
    ), encoding="utf-8")
    exe = tmp_path / "typing_union_expr.out"
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
    assert run.stdout == "ok\n"
