from __future__ import annotations

import os
import subprocess

import pytest


@pytest.mark.integration
def test_builtin_type_imports_are_native_without_libpython(tmp_path):
    source = tmp_path / "main.py"
    source.write_text(
        "from builtins import (\n"
        "    bool as Bool,\n"
        "    bytes as Bytes,\n"
        "    complex as Complex,\n"
        "    float as Float,\n"
        "    int as Int,\n"
        "    object as Object,\n"
        "    str as Str,\n"
        ")\n"
        "print(Bool is bool)\n"
        "print(Bytes is bytes)\n"
        "print(Complex is complex)\n"
        "print(Float is float)\n"
        "print(Int is int)\n"
        "print(Object is object)\n"
        "print(Str is str)\n"
        "view = memoryview(b'abc')\n"
        "print(memoryview is type(view))\n",
        encoding="utf-8",
    )
    executable = tmp_path / "main_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.splitlines() == ["True"] * 8


@pytest.mark.integration
def test_super_type_value_is_stable_and_hashable_without_libpython(tmp_path):
    source = tmp_path / "super_type_value.py"
    source.write_text(
        "registry = {}\n"
        "registry[super] = 'registered'\n"
        "def pickle_super(obj):\n"
        "    return super, ()\n"
        "token, args = pickle_super(None)\n"
        "print(super in registry)\n"
        "print(registry[super])\n"
        "print(token is super)\n"
        "print(len(args))\n",
        encoding="utf-8",
    )
    executable = tmp_path / "super_type_value_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    compile_result = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    run_result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout.splitlines() == ["True", "registered", "True", "0"]
