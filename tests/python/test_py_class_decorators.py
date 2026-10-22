from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_set_module_style_class_decorator_is_metadata_noop(tmp_path):
    src = tmp_path / "class_decorator.py"
    src.write_text(
        textwrap.dedent(
            """
            def set_module(name):
                return name

            @set_module("pkg")
            class C:
                def __init__(self, x: int) -> None:
                    self.x = x

            print(C(42).x)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "class_decorator.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"
