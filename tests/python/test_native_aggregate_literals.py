"""Aggregate payload masks must retain Python integer precision natively."""

import os
from pathlib import Path
import subprocess
import sys


def test_native_aggregate_literals_match_host(tmp_path, pcc_diagnostic_runtime_archive):
    from pcc.py_frontend.pipeline import compile_python

    source = Path(__file__).resolve().parents[2] / "pcc/backend/owned_literal_driver.py"
    binary = tmp_path / "aggregate_literals"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_diagnostic_runtime_archive))
    cases = [("int", "8", "0", "255"), ("int", "32", "0", "4294967295"),
             ("int", "64", "0", "1"), ("int", "64", "0", "-1"),
             ("int", "64", "0", "9223372036854775809"),
             ("int", "128", "0", "18446744073709551617"), ("int", "128", "0", "-1"),
             ("array", "64", "4", "<i64 0, i64 1, i64 2, i64 3>"),
             ("ptr", "0", "0", "inttoptr (i64 9223372036854775809 to ptr)")]
    for case in cases:
        expected = subprocess.run([sys.executable, str(source), *case], capture_output=True, text=True, timeout=30)
        assert expected.returncode == 0, expected.stderr
        result = subprocess.run([str(binary), *case], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PATH="/nonexistent", PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == expected.stdout, case
