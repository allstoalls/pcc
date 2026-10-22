from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_starred_unpack_from_sys_version_info_tuple(tmp_path):
    src = tmp_path / "starred_sys_version_info.py"
    src.write_text(
        textwrap.dedent(
            """
            import sys

            major, minor, *rest = sys.version_info
            print(major)
            print(minor)
            print(len(rest))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "starred_sys_version_info.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["3", "15", "1"]


def test_starred_unpack_from_dynamic_sequence(tmp_path):
    src = tmp_path / "starred_dynamic.py"
    src.write_text(
        textwrap.dedent(
            """
            def passthrough(value):
                return value

            first, *middle, last = passthrough((1, 2, 3, 4))
            print(first)
            print(middle[0])
            print(middle[1])
            print(last)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "starred_dynamic.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "2", "3", "4"]
