"""sum() runs CPython 3.15's builtin_sum_impl natively.

Type inference typed every sum() call as int and the lowering indexed the
source with boxed indices, adding floats without compensation: inside a
function print(sum([0.5, 0.25])) printed nothing at all, return sum(xs) of
floats printed <null>, and sum([0.1] * 10) was 0.9999999999999999.
py_builtin_sum iterates once and keeps CPython's order: the iterator, then
the str/bytes/bytearray start refusal, then the exact-int, exact-float
(Neumaier compensated, int items through PyLong_AsDouble) and exact-complex
runs, each entered at most once, and the generic + loop for anything else.
The program is compared with CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class Money:
    def __init__(self, cents):
        self.cents = cents

    def __add__(self, other):
        if isinstance(other, Money):
            return Money(self.cents + other.cents)
        if isinstance(other, int):
            return Money(self.cents + other)
        return NotImplemented

    __radd__ = __add__

    def __repr__(self):
        return "Money(" + str(self.cents) + ")"


class Counting:
    def __init__(self, limit):
        self.limit = limit
        self.current = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.current >= self.limit:
            raise StopIteration
        self.current += 1
        return self.current


class Failing:
    def __iter__(self):
        return self

    def __next__(self):
        raise ValueError("broken iterator")


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__, error)


def floats(values: list[float]):
    return sum(values)


def ints(values: list[int]) -> int:
    return sum(values)


def main():
    xs = [0.5, 0.25]
    print(sum(xs))
    total = sum([1.5, 2])
    print(total + 1)
    print(floats([0.1] * 10))
    print(ints([1, 2, 3]) + 1)
    attempt("empty", lambda: sum([]))
    attempt("empty float list", lambda: floats([]))
    attempt("ints", lambda: sum([1, 2, 3]))
    attempt("big ints", lambda: sum([2 ** 62, 2 ** 62, 2 ** 62]))
    attempt("bools", lambda: sum([True, True, False]))
    attempt("bool start", lambda: sum([1, 2], True))
    attempt("compensated", lambda: sum([0.1] * 10))
    attempt("compensated mixed", lambda: sum([1e100, 1.0, -1e100, 1.0]))
    attempt("negative zero", lambda: sum([-0.0, -0.0], -0.0))
    attempt("negative zero default", lambda: sum([-0.0]))
    attempt("int then float", lambda: sum([1, 2, 0.5, 3, 0.25]))
    attempt("float start", lambda: sum([1, 2, 3], 0.5))
    attempt("inf", lambda: sum([float("inf"), 1.0]))
    attempt("inf minus inf", lambda: sum([float("inf"), float("-inf")]))
    attempt("nan", lambda: sum([float("nan"), 1.0]))
    attempt("overflow", lambda: sum([1e308, 1e308]))
    attempt("huge int in float sum", lambda: sum([0.5, 10 ** 400]))
    attempt("complex", lambda: sum([1j, 2, 0.5, 1 + 1j]))
    attempt("complex start", lambda: sum([1, 2.5], 0j))
    attempt("complex negative zero", lambda: sum([1], -0j))
    attempt("complex compensated", lambda: sum([0.1j] * 10))
    attempt("lists", lambda: sum([[1], [2, 3]], []))
    attempt("tuples", lambda: sum([(1,), (2,)], ()))
    attempt("user", lambda: sum([Money(5), Money(7)], Money(0)))
    attempt("user radd", lambda: sum([Money(5), Money(7)]))
    attempt("user mixed", lambda: sum([1, Money(5), 2.5]))
    attempt("str start", lambda: sum(["a"], ""))
    attempt("bytes start", lambda: sum([b"a"], b""))
    attempt("bytearray start", lambda: sum([], bytearray()))
    attempt("non iterable", lambda: sum(5))
    attempt("non iterable str start", lambda: sum(5, ""))
    attempt("str items", lambda: sum(["a", "b"]))
    attempt("none item", lambda: sum([1, None]))
    attempt("generator", lambda: sum(x * 0.5 for x in range(4)))
    attempt("range", lambda: sum(range(101)))
    attempt("custom iterator", lambda: sum(Counting(4)))
    attempt("failing iterator", lambda: sum(Failing()))
    attempt("dict keys", lambda: sum({1: "a", 2: "b"}))
    attempt("set", lambda: sum({1.5, 2.5}))
    attempt("keyword start", lambda: sum([1, 2], start=10))
    attempt("keyword iterable", lambda: sum(iterable=[1]))
    attempt("no arguments", lambda: sum())
    values = [1, 2.5, 3]
    attempt("dyn items", lambda: sum(values))
    function = sum
    attempt("value", lambda: function([1, 2], 3))
    attempt("value in comprehension", lambda: [function(row) for row in [[1, 2], [0.5, 0.25]]])
    running = 0
    for step in range(200):
        running = running + sum([step, 0.5])
    print("loop", running)


main()
'''


@pytest.mark.integration
def test_sum_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "sum_protocol.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "sum_protocol"
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
