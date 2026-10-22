"""Arithmetic-fault safety for the no-libpython Python frontend.

Source: *Low-Level Software Security for Compiler Developers* — an
unguarded division/modulo fault is a denial-of-service / undefined-behavior
hazard at the machine level. In Python the safe behavior is a catchable
``ZeroDivisionError`` (and ``ValueError`` for a negative shift count) rather
than a hardware trap or a wrong result. pcc has multiple integer-division
lowering paths (see the repo notes on the six division lowering sites); these
tests pin that every form raises rather than faulting.
"""
from __future__ import annotations

import pytest

# NOTE: each branch uses a DISTINCT sink local. Reusing one local for the
# float result of `/` and the int result of `//`/`%`/`<<` triggers a separate
# IR type-confusion codegen bug, covered by test_py_local_type_confusion.py.
PROGRAM = r"""
def main() -> int:
    try:
        a = 5 / 0
        print("TRUEDIV0_NORAISE")
    except ZeroDivisionError:
        print("TRUEDIV0_RAISES")
    try:
        b = 5 // 0
        print("FLOORDIV0_NORAISE")
    except ZeroDivisionError:
        print("FLOORDIV0_RAISES")
    try:
        c = 5 % 0
        print("MOD0_NORAISE")
    except ZeroDivisionError:
        print("MOD0_RAISES")
    try:
        e = 1 << -1
        print("NEGSHIFT_NORAISE")
    except ValueError:
        print("NEGSHIFT_RAISES")
    return 0


main()
"""


@pytest.fixture(scope="module")
def out(compile_and_run):
    r = compile_and_run(PROGRAM, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_truediv_by_zero_raises(out):
    assert "TRUEDIV0_RAISES" in out


def test_floordiv_by_zero_raises(out):
    assert "FLOORDIV0_RAISES" in out


def test_mod_by_zero_raises(out):
    assert "MOD0_RAISES" in out


def test_negative_shift_count_raises_value_error(out):
    assert "NEGSHIFT_RAISES" in out
