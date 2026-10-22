"""Natively lowered int results are owned operands of arithmetic.

sys.getrecursionlimit(), gc.collect() and their siblings box their i64
result with py_int_from_i64 and returned it without recording that NEW
owner, so as a slot-call operand (``sys.getrecursionlimit() - 1``) the
compile failed with "slot-call operand has no authoritative source or owned
result: Call" -- nine CPython Lib/test modules stopped there. The boxing
sites now record the owner, as the other native lowerings do.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import gc
import sys


def values():
    return (
        sys.getrecursionlimit() - 1,
        sys.version_info.major + 0,
        gc.collect() * 0,
        gc.get_freeze_count() + 0,
    )


print(values())
'''


@pytest.mark.integration
def test_native_int_results_are_slot_call_operands(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "native_int_operands.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "native_int_operands"
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
