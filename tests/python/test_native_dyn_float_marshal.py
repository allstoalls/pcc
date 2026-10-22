"""DynType values backed by double IR must marshal to pcc float objects."""
from __future__ import annotations

import subprocess
import textwrap


def test_dyn_float_value_can_enter_object_container(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def ident(x):
                return x

            def main() -> None:
                values = [ident(1.5)]
                print(len(values))

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
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
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\n"
