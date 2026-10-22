from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from pcc.frontends.python.pipeline import compile_python_multi

REPO = Path(__file__).absolute().parents[2]


def test_compiled_enum_provider_publishes_member_name_and_value(tmp_path, monkeypatch):
    main = tmp_path / "main.py"
    main.write_text(
        "from enum import Enum\n"
        "class DisplayMode(Enum):\n"
        '    stdout = "stdout"\n'
        '    dicts = "dicts"\n'
        "print(DisplayMode.stdout.name)\n"
        "print(DisplayMode.stdout.value)\n",
        encoding="utf-8",
    )
    provider = tmp_path / "enum.py"
    shutil.copyfile(REPO / "pcc" / "stdlib" / "enum.py", provider)
    executable = tmp_path / "enum-provider"
    compile_python_multi(
        [str(main), str(provider)],
        str(executable),
        module_names=["main", "enum"],
        entry_module="main",
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )

    run = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["stdout", "stdout"]


def test_native_enum_module_alias_supports_dotted_base(tmp_path, monkeypatch):
    main = tmp_path / "main.py"
    main.write_text(
        "import enum\n"
        "class CopyMode(enum.Enum):\n"
        "    ALWAYS = True\n"
        "    NEVER = False\n"
        "    IF_NEEDED = enum.auto()\n"
        "print(CopyMode.ALWAYS.name)\n"
        "print(CopyMode.IF_NEEDED.value)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "enum-module-alias"
    compile_python_multi(
        [str(main)],
        str(executable),
        module_names=["main"],
        entry_module="main",
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )

    run = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["ALWAYS", "2"]
