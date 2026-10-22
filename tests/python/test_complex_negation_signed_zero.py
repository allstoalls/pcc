"""Negating a complex flips the sign bit of each part, as in CPython.

py_complex_neg and py_complex_conjugate computed 0.0 - part, which turns a
zero part into +0.0: -0j printed 0j, -(1+0j) printed (-1+0j) and
(0j).conjugate() printed 0j. Compared with CPython on five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import math


def parts(z):
    return (math.copysign(1.0, z.real), math.copysign(1.0, z.imag))


values = [0j, -0j, 1 + 0j, complex(0.0, -0.0), complex(-0.0, 0.0), complex(-0.0, -0.0),
          complex(2.5, -3.0), complex(float("inf"), 0.0)]
for z in values:
    print(repr(z), repr(-z), parts(-z), repr(z.conjugate()), parts(z.conjugate()))
w = -0j
print(repr(w), repr(-w), repr(-(-w)), sum([1], w), sum([], w))
'''


@pytest.mark.integration
def test_complex_negation_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "complex_negation.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "complex_negation"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
