from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_matmul_lowers_to_native_object_protocol(tmp_path):
    src = tmp_path / "matmul_protocol.py"
    src.write_text(
        textwrap.dedent(
            """
            class MatrixLike:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __matmul__(self, other) -> int:
                    return self.value + other.value

            print(MatrixLike(20) @ MatrixLike(22))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "matmul_protocol.out"

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


def test_self_binary_dunder_dispatches_inside_method_self_backend(tmp_path):
    src = tmp_path / "self_binary_dunder.py"
    src.write_text(
        textwrap.dedent(
            """
            class Number:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __mul__(self, other):
                    return Number(self.value * other)

                def inplace(self, other):
                    return self * other

            print(Number(7).inplace(6).value)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "self_binary_dunder.out"

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


def test_self_power_dunder_dispatches_inside_method_self_backend(tmp_path):
    src = tmp_path / "self_power_dunder.py"
    src.write_text(
        textwrap.dedent(
            """
            class Number:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __pow__(self, other):
                    return Number(self.value ** other)

                def power(self, other):
                    return self ** other

            print(Number(2).power(5).value)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "self_power_dunder.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "32\n"
