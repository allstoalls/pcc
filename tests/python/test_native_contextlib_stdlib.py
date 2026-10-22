from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pcc.frontends.python.pipeline import compile_python_multi

_REPO_ROOT = Path(__file__).absolute().parents[2]


def test_nullcontext_import_and_enter_result_no_libpython(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(
        "from contextlib import nullcontext\n"
        "token = object()\n"
        "with nullcontext(token) as value:\n"
        "    print(value is token)\n"
        "with nullcontext() as value:\n"
        "    print(value is None)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "main_bin"
    provider = _REPO_ROOT / "pcc" / "stdlib" / "contextlib.py"
    compile_python_multi(
        [str(source), str(provider)],
        str(executable),
        entry_module="main",
        module_names=["main", "contextlib"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.splitlines() == ["True", "True"]


def test_dynamic_cross_module_context_manager_no_libpython(tmp_path):
    provider = tmp_path / "provider.py"
    provider.write_text(
        "class Manager:\n"
        "    def __enter__(self):\n"
        "        print('enter')\n"
        "        return 41\n"
        "\n"
        "    def __exit__(self, exc_type, exc, tb):\n"
        "        print('exit')\n"
        "\n"
        "manager = Manager()\n",
        encoding="utf-8",
    )
    source = tmp_path / "main.py"
    source.write_text(
        "import provider\n"
        "with provider.manager as value:\n"
        "    print(value + 1)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "main_bin"
    compile_python_multi(
        [str(source), str(provider)],
        str(executable),
        entry_module="main",
        module_names=["main", "provider"],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.splitlines() == ["enter", "42", "exit"]
