from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_recursive_guard_decorator_factory_compiles_no_libpython_self_backend(tmp_path):
    src = tmp_path / "recursive_guard_decorator.py"
    src.write_text(
        textwrap.dedent(
            """
            def _recursive_guard(fillvalue="..."):
                def decorating_function(f):
                    return f
                return decorating_function

            @_recursive_guard()
            def render(value):
                return "value=" + str(value)

            print(render(7))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "recursive_guard_decorator.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "value=7\n"


def test_errstate_style_decorator_factory_compiles_no_libpython_self_backend(tmp_path):
    src = tmp_path / "errstate_decorator.py"
    src.write_text(
        textwrap.dedent(
            """
            class Module:
                pass

            np = Module()

            class Box:
                @np.errstate(over="ignore", invalid="ignore")
                def store(self, index, value):
                    print(value)

            Box().store(1, 5)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "errstate_decorator.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "5\n"
