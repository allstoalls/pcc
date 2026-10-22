"""An int compares against a float by exact value, as in CPython.

Both the runtime (py_obj_eq_value / _cmp_threeway, the dyn path) and the typed
lowering converted the int to a double and compared doubles, so
``9007199254740993 == 9007199254740992.0`` and ``10**30 == 1e30`` were True
and ``9007199254740992.0 < 9007199254740993`` was False.  The runtime now
calls ``py_int_f64_cmp``; the typed lowering calls it for a boxed int and
emits an exact branch-free sequence for a raw i64 (``unsafe-i64`` below
exercises that lane).  ``float(int)`` is correctly rounded too: the old
digit-at-a-time conversion rounded twice (``float(2**96 + 2**43 + 1)`` came
out one ulp low).

Columns are ``==  !=  <  <=  >  >=`` for the int on the left, then the float
on the left, through dyn functions, typed ``(int, float)`` functions and
(lane rows) the packed ``lane`` functions.
"""

import os
import subprocess
import sys

import pytest


PROGRAM = '''NAN = float("nan")
INF = float("inf")


def d_eq(a, b):
    return a == b


def d_ne(a, b):
    return a != b


def d_lt(a, b):
    return a < b


def d_le(a, b):
    return a <= b


def d_gt(a, b):
    return a > b


def d_ge(a, b):
    return a >= b


def flags(ops):
    s = ""
    for x in ops:
        if x:
            s = s + "T"
        else:
            s = s + "F"
    return s


def dyn(a, b):
    return flags([d_eq(a, b), d_ne(a, b), d_lt(a, b), d_le(a, b), d_gt(a, b), d_ge(a, b)])


def typed(a: int, b: float) -> str:
    return flags([a == b, a != b, a < b, a <= b, a > b, a >= b])


def typed_rev(b: float, a: int) -> str:
    return flags([b == a, b != a, b < a, b <= a, b > a, b >= a])


def typed_bool(a: bool, b: float) -> str:
    return flags([a == b, a != b, a < b, a <= b, a > b, a >= b])


def lane(a: int, b: float) -> int:
    # The six results as bits; a raw i64 under PCC_PYTHON_TYPED_INT_ABI=unsafe-i64.
    r = 0
    if a == b:
        r = r + 1
    if a != b:
        r = r + 2
    if a < b:
        r = r + 4
    if a <= b:
        r = r + 8
    if a > b:
        r = r + 16
    if a >= b:
        r = r + 32
    return r


def lane_rev(b: float, a: int) -> int:
    r = 0
    if b == a:
        r = r + 1
    if b != a:
        r = r + 2
    if b < a:
        r = r + 4
    if b <= a:
        r = r + 8
    if b > a:
        r = r + 16
    if b >= a:
        r = r + 32
    return r


def unpack(r):
    return flags([r & 1, r & 2, r & 4, r & 8, r & 16, r & 32])


def lane_row(label, a, b, packed, packed_rev):
    print(label, dyn(a, b), dyn(b, a), typed(a, b), typed_rev(b, a),
          unpack(packed), unpack(packed_rev))


def wide_row(label, a, b):
    print(label, dyn(a, b), dyn(b, a), typed(a, b), typed_rev(b, a))


def bool_row(label, a, b):
    print(label, dyn(a, b), dyn(b, a), typed_bool(a, b), typed(a, b))


def as_floats(values):
    return [float(v) for v in values]


def times_one(values):
    return [v * 1.0 for v in values]


def halves(values):
    return [v / 2 for v in values]


def main():
    # Integers the i64 lane can hold; lane()/lane_rev() take literal i64s.
    lane_row("2**53+1:2.0**53", 9007199254740993, 9007199254740992.0,
             lane(9007199254740993, 9007199254740992.0),
             lane_rev(9007199254740992.0, 9007199254740993))
    lane_row("2**53:2.0**53", 9007199254740992, 9007199254740992.0,
             lane(9007199254740992, 9007199254740992.0),
             lane_rev(9007199254740992.0, 9007199254740992))
    lane_row("2**53-1:2.0**53", 9007199254740991, 9007199254740992.0,
             lane(9007199254740991, 9007199254740992.0),
             lane_rev(9007199254740992.0, 9007199254740991))
    lane_row("-2**53-1:-2.0**53", -9007199254740993, -9007199254740992.0,
             lane(-9007199254740993, -9007199254740992.0),
             lane_rev(-9007199254740992.0, -9007199254740993))
    lane_row("2**63-1:2.0**63", 9223372036854775807, 9223372036854775808.0,
             lane(9223372036854775807, 9223372036854775808.0),
             lane_rev(9223372036854775808.0, 9223372036854775807))
    lane_row("-2**63:-2.0**63", -9223372036854775807 - 1, -9223372036854775808.0,
             lane(-9223372036854775807 - 1, -9223372036854775808.0),
             lane_rev(-9223372036854775808.0, -9223372036854775807 - 1))
    lane_row("-2**63+1:-2.0**63", -9223372036854775807, -9223372036854775808.0,
             lane(-9223372036854775807, -9223372036854775808.0),
             lane_rev(-9223372036854775808.0, -9223372036854775807))
    lane_row("5:nan", 5, NAN, lane(5, NAN), lane_rev(NAN, 5))
    lane_row("7:inf", 7, INF, lane(7, INF), lane_rev(INF, 7))
    lane_row("7:-inf", 7, -INF, lane(7, -INF), lane_rev(-INF, 7))
    lane_row("0:0.0", 0, 0.0, lane(0, 0.0), lane_rev(0.0, 0))
    lane_row("0:-0.0", 0, -0.0, lane(0, -0.0), lane_rev(-0.0, 0))
    lane_row("1:-0.0", 1, -0.0, lane(1, -0.0), lane_rev(-0.0, 1))
    lane_row("-1:0.0", -1, 0.0, lane(-1, 0.0), lane_rev(0.0, -1))
    lane_row("3:3.0", 3, 3.0, lane(3, 3.0), lane_rev(3.0, 3))
    lane_row("1:1.5", 1, 1.5, lane(1, 1.5), lane_rev(1.5, 1))
    lane_row("-2:-1.5", -2, -1.5, lane(-2, -1.5), lane_rev(-1.5, -2))
    # Integers beyond i64 (heap ints).
    wide_row("2**63:2.0**63", 2 ** 63, 9223372036854775808.0)
    wide_row("2**64+1:2.0**64", 2 ** 64 + 1, 18446744073709551616.0)
    wide_row("10**30:1e30", 10 ** 30, 1e30)
    wide_row("-10**30:-1e30", -(10 ** 30), -1e30)
    wide_row("-2**80-1:-2.0**80", -(2 ** 80) - 1, -1.2089258196146292e24)
    wide_row("2**70:nan", 2 ** 70, NAN)
    wide_row("2**70:inf", 2 ** 70, INF)
    wide_row("-2**70:-inf", -(2 ** 70), -INF)
    wide_row("-2**70:1.5", -(2 ** 70), 1.5)
    wide_row("10**400:max", 10 ** 400, 1.7976931348623157e308)
    wide_row("-10**400:-inf", -(10 ** 400), -INF)
    bool_row("True:1.0", True, 1.0)
    bool_row("True:0.5", True, 0.5)
    bool_row("False:-0.0", False, -0.0)
    bool_row("True:nan", True, NAN)
    bool_row("True:1+ulp", True, 1.0000000000000002)
    print(9007199254740993 == 9007199254740992.0,
          9007199254740992.0 < 9007199254740993,
          10 ** 30 == 1e30, 1e30 > 10 ** 30,
          2 ** 63 - 1 < 9223372036854775808.0, 3 == 3.0, 1 < 1.5)
    print(sorted([2 ** 53 + 1, 9007199254740992.0, 2 ** 53 - 1, 1e30, 10 ** 30]))
    print(max([9007199254740992.0, 2 ** 53 + 1]),
          min([2 ** 53 + 1, 9007199254740992.0]))
    print(2 ** 53 + 1 in [9007199254740992.0], 10 ** 30 in (1e30,),
          1e30 in [10 ** 30], 2 ** 53 in [9007199254740992.0])
    # float(int) rounds once, half to even.
    wide = [
        2 ** 64 + 2 ** 11 + 1, 2 ** 64 + 2 ** 11, 2 ** 54 + 1, 2 ** 55 + 3,
        -(2 ** 80 + 2 ** 27 + 1), 10 ** 30, 2 ** 96 + 2 ** 43 + 1,
        2 ** 117 + 2 ** 64 + 1, -(2 ** 96 + 2 ** 43 + 1),
        2 ** 1023 + 2 ** 970, 2 ** 1023 + 2 ** 970 + 1,
    ]
    print(as_floats(wide))
    print(times_one(wide))
    print(halves(wide))


main()
'''

EXPECTED = (
    "2**53+1:2.0**53 FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "2**53:2.0**53 TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "2**53-1:2.0**53 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "-2**53-1:-2.0**53 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "2**63-1:2.0**63 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "-2**63:-2.0**63 TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "-2**63+1:-2.0**63 FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "5:nan FTFFFF FTFFFF FTFFFF FTFFFF FTFFFF FTFFFF\n"
    "7:inf FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "7:-inf FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "0:0.0 TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "0:-0.0 TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "1:-0.0 FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "-1:0.0 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "3:3.0 TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "1:1.5 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "-2:-1.5 FTTTFF FTFFTT FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "2**63:2.0**63 TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "2**64+1:2.0**64 FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "10**30:1e30 FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "-10**30:-1e30 FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "-2**80-1:-2.0**80 FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "2**70:nan FTFFFF FTFFFF FTFFFF FTFFFF\n"
    "2**70:inf FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "-2**70:-inf FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "-2**70:1.5 FTTTFF FTFFTT FTTTFF FTFFTT\n"
    "10**400:max FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "-10**400:-inf FTFFTT FTTTFF FTFFTT FTTTFF\n"
    "True:1.0 TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "True:0.5 FTFFTT FTTTFF FTFFTT FTFFTT\n"
    "False:-0.0 TFFTFT TFFTFT TFFTFT TFFTFT\n"
    "True:nan FTFFFF FTFFFF FTFFFF FTFFFF\n"
    "True:1+ulp FTTTFF FTFFTT FTTTFF FTTTFF\n"
    "False True False True True True True\n"
    "[9007199254740991, 9007199254740992.0, 9007199254740993, 1000000000000000000000000000000, 1e+30]\n"
    "9007199254740993 9007199254740992.0\n"
    "False False False True\n"
    "[1.8446744073709556e+19, 1.8446744073709552e+19, 1.8014398509481984e+16, 3.602879701896397e+16, -1.2089258196146294e+24, 1e+30, 7.922816251426436e+28, 1.6615349947311452e+35, -7.922816251426436e+28, 8.98846567431158e+307, 8.988465674311582e+307]\n"
    "[1.8446744073709556e+19, 1.8446744073709552e+19, 1.8014398509481984e+16, 3.602879701896397e+16, -1.2089258196146294e+24, 1e+30, 7.922816251426436e+28, 1.6615349947311452e+35, -7.922816251426436e+28, 8.98846567431158e+307, 8.988465674311582e+307]\n"
    "[9.223372036854778e+18, 9.223372036854776e+18, 9007199254740992.0, 1.8014398509481984e+16, -6.044629098073147e+23, 5e+29, 3.961408125713218e+28, 8.307674973655726e+34, -3.961408125713218e+28, 4.49423283715579e+307, 4.494232837155791e+307]\n"
)


@pytest.mark.parametrize("typed_int_abi", ["auto", "unsafe-i64"])
def test_int_float_comparison_is_exact(
    tmp_path, monkeypatch, typed_int_abi, pcc_runtime_archive,
    python_program_compiler,
):
    monkeypatch.setenv("PCC_PYTHON_TYPED_INT_ABI", typed_int_abi)
    source = tmp_path / "int_float_exact_compare.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == EXPECTED
    binary = tmp_path / "int_float_exact_compare"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == EXPECTED, f"GC{backend}"
