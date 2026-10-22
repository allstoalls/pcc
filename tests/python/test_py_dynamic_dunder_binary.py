from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from pcc1_gate import repo_root

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1


REPO = repo_root()


def test_dynamic_receiver_truediv_compiles_dunder_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_truediv.py"
    src.write_text(textwrap.dedent(
        """
        class PathLike:
            def __truediv__(self, child):
                return "root/" + child

        def identity(value):
            return value

        def join(base, name: str):
            return base / name

        base = identity(PathLike())
        result = join(base, "leaf")
        print(result)
        """
    ), encoding="utf-8")
    exe = tmp_path / "dynamic_truediv.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert exe.exists()


def test_pcc1_dynamic_truediv_unknown_receiver_compile_only(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for dynamic __truediv__ regression"
        )

    src = tmp_path / "dynamic_truediv_unknown.py"
    src.write_text(textwrap.dedent(
        """
        class PathLike:
            def __truediv__(self, child):
                return "root/" + child

        def join(dirname):
            filename = dirname / "source.c"
            print(filename)

        def main():
            join(PathLike())
        main()
        """
    ), encoding="utf-8")
    out = tmp_path / "dynamic_truediv_unknown.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "--emit-llvm=" + str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()


def test_int_builtin_on_class_instance_compiles_dunder_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "class_int_dunder.py"
    src.write_text(textwrap.dedent(
        """
        class Kind:
            def __int__(self):
                return 7

        def identity(value):
            return value

        def coerce(value):
            return int(value)

        result = coerce(identity(Kind()))
        print(result)
        """
    ), encoding="utf-8")
    exe = tmp_path / "class_int_dunder.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert exe.exists()


def test_pcc1_int_builtin_on_class_instance_compile_only(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for int(class instance) regression"
        )

    src = tmp_path / "class_int_dunder_pcc1.py"
    src.write_text(textwrap.dedent(
        """
        class Kind:
            def __int__(self):
                return 7

        def coerce(value):
            return int(value)

        def main():
            print(coerce(Kind()))
        main()
        """
    ), encoding="utf-8")
    out = tmp_path / "class_int_dunder_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "--emit-llvm=" + str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()


def test_class_instance_numeric_rhs_uses_dynamic_mul_dunder_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "class_numeric_rhs_mul.py"
    src.write_text(textwrap.dedent(
        """
        class Factor:
            def __mul__(self, value):
                return value + 5

        def scale(value: Factor):
            return value * 3

        result = scale(Factor())
        print(result)
        """
    ), encoding="utf-8")
    exe = tmp_path / "class_numeric_rhs_mul.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert exe.exists()


def test_pcc1_class_instance_numeric_rhs_compile_only(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for class numeric RHS dunder regression"
        )

    src = tmp_path / "class_numeric_rhs_mul_pcc1.py"
    src.write_text(textwrap.dedent(
        """
        class Factor:
            def __mul__(self, value):
                return value + 5

        def scale(value: Factor):
            return value * 3

        def main():
            print(scale(Factor()))
        main()
        """
    ), encoding="utf-8")
    out = tmp_path / "class_numeric_rhs_mul_pcc1.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "--emit-llvm=" + str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()
