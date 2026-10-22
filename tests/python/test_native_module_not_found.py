"""A module the native program cannot import raises ModuleNotFoundError.

CPython's import system raises ModuleNotFoundError, an ImportError subclass,
for a module it cannot find, from ``import`` and from ``from ... import``.
pcc's strict no-libpython import raised a plain ImportError, so ``except
ModuleNotFoundError`` missed it and ``type(exc).__name__`` differed.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
try:
    import _pcc_absent_provider
except ModuleNotFoundError as caught:
    print(type(caught).__name__, caught, isinstance(caught, ImportError))


def load():
    from _pcc_absent_provider import thing
    return thing


try:
    load()
except ImportError as caught:
    print(type(caught).__name__, caught)
'''


@pytest.mark.integration
def test_missing_module_raises_module_not_found_error_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "missing_module.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout.splitlines()[0] == (
        "ModuleNotFoundError No module named '_pcc_absent_provider' True"
    )
    binary = tmp_path / "missing_module"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
