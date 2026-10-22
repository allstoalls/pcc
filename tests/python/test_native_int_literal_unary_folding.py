"""Unary ``-``/``~``/``+`` on an integer literal folds at compile time.

``return 0 if ok else -1`` used to lower the literal through int arithmetic
on a plain Python ``int`` operand.  A freestanding module cannot carry that
operand (``RuntimeError: freestanding ordinary Python int unary arithmetic
cannot preserve arbitrary precision``), and the machine-int paths paid an
instruction for a constant.  The fold must keep the exact Python value, so
the corpus is compared against CPython on every GC backend.
"""

from __future__ import annotations

import os
import subprocess
import sys


_PROGRAM = '''def pick(flag):
    return 0 if flag else -1


def invert(flag):
    return 0 if flag else ~0


def plus(flag):
    return 0 if flag else +7


def minus_zero(flag):
    return 0 if flag else -0


def nested(flag):
    # ``-(1)`` folds; ``~(-1)`` keeps its unary operand and must still work.
    return -(1) if flag else ~(-1)


def main():
    for flag in (False, True):
        print(
            pick(flag), invert(flag), plus(flag), minus_zero(flag), nested(flag)
        )
    big = -123456789012345678901234567890
    print(big, -big, ~big, +big)
    print(-0 == 0, ~0, +0)

main()
'''


def _cpython():
    ran = subprocess.run(
        [sys.executable, "-c", _PROGRAM],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def _pcc(tmp_path, compiler, archive, backends):
    source = tmp_path / "literal_folding.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "literal_folding"
    compiler(
        str(source),
        str(binary),
        backend="self",
        libpython_mode="off",
        runtime_archive=str(archive),
    )
    outputs = []
    for backend in backends:
        ran = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stderr}"
        outputs.append(ran.stdout)
    return outputs


def test_folded_integer_literals_match_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler
):
    expected = _cpython()
    for output in _pcc(
        tmp_path, python_program_compiler, pcc_runtime_archive, range(5)
    ):
        assert output == expected
