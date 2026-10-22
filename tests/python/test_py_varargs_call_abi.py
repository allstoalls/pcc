from __future__ import annotations

import subprocess
import textwrap


def test_call_to_function_with_unused_varargs_does_not_overrun_abi(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "varargs_call_abi.py"
    src.write_text(
        textwrap.dedent("""
        def visit(value, *args, **kwargs):
            print(value)

        visit(7, 8, named=9)
        """),
        encoding="utf-8",
    )
    exe = tmp_path / "varargs_call_abi.out"
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
    assert run.stdout == "7\n"


def test_generator_forward_unpack_with_kwonly_separator_compiles(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "generator_forward_kwonly.py"
    src.write_text(
        textwrap.dedent("""
            def target(value=None, *, named=None):
                return value + named

            def forwarding(*args, **kwargs):
                token = target(*args, **kwargs)
                yield token

            print(next(forwarding(40, named=2)))
            """),
        encoding="utf-8",
    )
    exe = tmp_path / "generator_forward_kwonly.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert exe.is_file()
