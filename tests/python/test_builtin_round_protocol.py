"""round() runs CPython's builtin_round_impl, float.__round__ and int.__round__.

round(x) converted x to a double, took rint and truncated it into an i64, and
round(x, n) rounded a double through "%.nf" text (n <= 200) or x / 10**-n:
an int or bool beyond 2**53 lost digits, a negative ndigits on an int went
through a float, NaN and the infinities produced garbage instead of raising,
a class's __round__ was never called, a str or None was read as a double,
None as ndigits was an i64 and an index object was not asked for __index__.
py_builtin_round asks the number's type: float.__round__ (half to even into
an exact int, or _Py_dg_dtoa mode 3 rounding of the exact binary value with
NDIGITS_MAX 323 / NDIGITS_MIN -308 and "rounded value too large to
represent"), int.__round__ (_PyLong_DivmodNear for a negative ndigits) or the
class's own __round__, and CPython's TypeError for a type without one. The
program is compared with CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class R:
    def __repr__(self):
        return "R()"

    def __round__(self, ndigits=None):
        return ("R.round", ndigits)


class NoRound:
    pass


class Index:
    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__, error)


def typed_float(value: float, ndigits: int):
    return round(value), round(value, ndigits)


def typed_int(value: int, ndigits: int):
    return round(value), round(value, ndigits)


floats = [0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 2.675, 0.125, 0.375, 1e300, -1e300,
          0.1, 123.456, -0.0, 0.0, -0.4, 5e-324, 1.7976931348623157e308, 1e22, 4.35]
for value in floats:
    attempt("round(" + repr(value) + ")", lambda: round(value))
    attempt("round(" + repr(value) + ", None)", lambda: round(value, None))
    for ndigits in (0, 1, 2, -1, -2, 15, 300, 323, 324, -308, -309, 400, -400):
        attempt("round(" + repr(value) + ", " + str(ndigits) + ")", lambda: round(value, ndigits))
ints = [0, 5, 15, 25, 35, -25, -35, 12345, -12345, 10 ** 30 + 5 * 10 ** 10, -(10 ** 30) - 15 * 10 ** 9, True, False]
for value in ints:
    attempt("round(" + repr(value) + ")", lambda: round(value))
    for ndigits in (-1, -2, -11, 0, 2, None, True):
        attempt("round(" + repr(value) + ", " + repr(ndigits) + ")", lambda: round(value, ndigits))
attempt("inf", lambda: round(float("inf")))
attempt("-inf", lambda: round(float("-inf")))
attempt("nan", lambda: round(float("nan")))
attempt("inf 2", lambda: round(float("inf"), 2))
attempt("-inf -2", lambda: round(float("-inf"), -2))
attempt("nan 2", lambda: round(float("nan"), 2))
attempt("max -308", lambda: round(1.7976931348623157e308, -308))
attempt("-max -308", lambda: round(-1.7976931348623157e308, -308))
attempt("user", lambda: round(R()))
attempt("user digits", lambda: round(R(), 3))
attempt("user None", lambda: round(R(), None))
attempt("no round", lambda: round(NoRound()))
attempt("str", lambda: round("x"))
attempt("complex", lambda: round(1j))
attempt("None", lambda: round(None))
attempt("list digits", lambda: round([], 2))
attempt("float ndigits", lambda: round(2.5, 1.0))
attempt("int float ndigits", lambda: round(25, 1.0))
attempt("str ndigits", lambda: round(2.5, "1"))
attempt("index float", lambda: round(1234.5678, Index(-2)))
attempt("index int", lambda: round(1250, Index(-2)))
attempt("index float result", lambda: round(1234.5678, Index(1.5)))
attempt("huge ndigits", lambda: round(2.5, 10 ** 30))
attempt("huge negative ndigits", lambda: round(2.5, -(10 ** 30)))
attempt("huge negative ndigits neg", lambda: round(-2.5, -(10 ** 30)))
attempt("huge int ndigits", lambda: round(25, 10 ** 30))
attempt("keyword number", lambda: round(number=2.5))
attempt("keyword ndigits", lambda: round(2.567, ndigits=2))
attempt("keyword both", lambda: round(ndigits=1, number=2.25))
attempt("no arguments", lambda: round())
attempt("three arguments", lambda: round(1, 2, 3))
arguments = (2.675, 2)
attempt("star", lambda: round(*arguments))
function = round
attempt("value", lambda: function(3.5))
attempt("value digits", lambda: function(3.14159, 3))
attempt("value in comprehension", lambda: [function(x) for x in [0.5, 1.5, 2.5, 3.5]])
attempt("typed float", lambda: typed_float(2.675, 2))
attempt("typed float negative", lambda: typed_float(-1234.5, -1))
attempt("typed int", lambda: typed_int(12345, -2))
attempt("typed int big", lambda: typed_int(2 ** 80, -5))
mixed = [2.5, 3, R(), "s"]
for item in mixed:
    attempt("dyn " + repr(item), lambda: round(item))
    attempt("dyn digits " + repr(item), lambda: round(item, 1))
total = 0
for step in range(1000):
    total = total + round(step * 0.5)
print("sum", total)
'''


@pytest.mark.integration
def test_round_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "round_protocol.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "round_protocol"
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
