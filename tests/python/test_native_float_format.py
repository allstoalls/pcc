"""Native float format-spec support under strict no-libpython (run-based).

`format_float_builtin` in `pcc/runtime/src/py_format.c` previously parsed
only `,`, `.precision`, and a bare `f`/`e` type — it raised
``ValueError: unsupported format specifier`` on ANY width/align/sign/zero-pad,
so the extremely common ``f"{x:8.3f}"`` / ``f"{x:>10.2f}"`` / ``f"{x:08.2f}"``
specs failed at runtime under ``--python-libpython=off``. The formatter now
mirrors ``format_int_builtin``'s align/sign/zero-pad/width handling.

These tests COMPILE + RUN under ``--backend self --python-libpython=off`` (which
hard-errors on any residual ``py_cpy_*`` fallback) and assert the exact output,
so a green run proves the float formatter is native and matches CPython.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _run_pcc_program(tmp_path: Path, source: str) -> str:
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=300, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_float_format_width_precision_native_no_libpython(tmp_path):
    # The core bug: width + precision together (previously raised).
    out = _run_pcc_program(
        tmp_path,
        "x = 3.14159\n"
        "print(f'{x:.2f}')\n"
        "print(f'{x:8.3f}')\n"
        "print(f'{x:>10.2f}|')\n"
        "print(f'{x:<10.2f}|')\n"
        "print(f'{x:^10.2f}|')\n"
        "print(f'{x:08.2f}')\n",
    )
    assert out.split("\n")[:6] == [
        "3.14",
        "   3.142",
        "      3.14|",
        "3.14      |",
        "   3.14   |",
        "00003.14",
    ], out


def test_float_format_sign_comma_sci_native_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "x = 3.14159\n"
        "print(f'{x:+.2f}')\n"
        "print(f'{x:e}')\n"
        "print(f'{x:10.2e}')\n"
        "print(f'{1234.5:,.2f}')\n"
        "print(f'{-2.5:08.2f}')\n"
        "print(f'{-2.5:+.1f}')\n",
    )
    assert out.split("\n")[:6] == [
        "+3.14",
        "3.141590e+00",
        "  3.14e+00",
        "1,234.50",
        "-0002.50",
        "-2.5",
    ], out


def test_float_format_percent_native_no_libpython(tmp_path):
    # The `%` conversion: value*100, fixed-point precision, trailing '%',
    # with width/align applied to the whole thing (previously raised).
    out = _run_pcc_program(
        tmp_path,
        "print(f'{0.5:.1%}')\n"
        "print(f'{0.5:%}')\n"
        "print(f'{0.1234:.2%}')\n"
        "print(f'{-0.5:.0%}')\n"
        "print(f'{0.5:>8.1%}|')\n"
        "print(f'{1.5:.0%}')\n",
    )
    assert out.split("\n")[:6] == [
        "50.0%",
        "50.000000%",
        "12.34%",
        "-50%",
        "   50.0%|",
        "150%",
    ], out
