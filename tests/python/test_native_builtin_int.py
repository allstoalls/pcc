"""Native dispatch for ``int(x)`` on dynamic runtime values."""
from __future__ import annotations

import subprocess
import textwrap


def test_dyn_int_add_result_marshal_does_not_emit_noop_sext(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    ll = tmp_path / "prog.ll"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def ident(x):
                return x

            def main() -> None:
                print(ident(1) + 2)

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    compile_python(
        str(src),
        str(ll),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    ir_text = ll.read_text(encoding="utf-8")
    assert "sext i64" not in ir_text
    assert "m.dyn.sext64" not in ir_text

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
    assert run.stdout == "3\n"


def test_dyn_int_builtin_handles_tagged_int_without_str_path(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            def ident(x):
                return x

            def main() -> None:
                print(int(ident(21)))
                print(int(ident(True)))
                print(int(ident(3.7)))
                print(int(ident("42")))

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
    assert run.stdout == "21\n1\n3\n42\n"


def test_cpython_dyn_int_builtin_uses_cpython_number_protocol(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            """
            from decimal import Decimal

            def main() -> None:
                print(int(Decimal("6")))

            if __name__ == "__main__":
                main()
            """
        ).lstrip()
    , encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="auto",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "6\n"
