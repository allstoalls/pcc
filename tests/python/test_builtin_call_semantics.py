"""Builtin calls follow CPython's protocols, messages and argument checks.

A differential run of every builtin against CPython exposed direct-call
gaps: abs()'s typed message and complex magnitude, the not-iterable message
of all()/iter(), chr()'s range error and lone surrogates, the 3.14+
zero-division text, format()'s spec type check, and hasattr()'s name type
check and AttributeError-only suppression; hash() of tuples, None and big
ints, issubclass(bool, int), len() of an unsized object and ord()'s checks
follow CPython as well. any()/all() follow the iterator
protocol with CPython's short circuit (they walked len/getitem, answering
generators, sets and user iterables wrongly) after the argument is fully
evaluated. Each program is compared with CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


DIRECT = r'''
class Prop:
    @property
    def boom(self):
        raise ValueError("boom")

    @property
    def missing(self):
        raise AttributeError("nope")


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__ + ":", error)


attempt("abs str", lambda: abs("x"))
attempt("abs complex", lambda: abs(3 + 4j))
attempt("all int", lambda: all(5))
attempt("any none", lambda: any(None))
attempt("iter float", lambda: iter(1.5))
attempt("for int", lambda: [x for x in 5])
attempt("chr negative", lambda: chr(-1))
attempt("chr big", lambda: chr(1114112))
attempt("chr surrogate", lambda: len(chr(0xD800)))
attempt("chr surrogate ord", lambda: ord(chr(0xDC80)))
attempt("divmod zero", lambda: divmod(1, 0))
attempt("divmod float zero", lambda: divmod(1.0, 0))
attempt("floordiv float zero", lambda: 1.0 // 0.0)
attempt("complex div zero", lambda: (1 + 1j) / 0)
attempt("format spec int", lambda: format(1, 2))
attempt("format spec none", lambda: format(1, None))
attempt("hasattr int name", lambda: hasattr(Prop(), 3))
attempt("hasattr raising property", lambda: hasattr(Prop(), "boom"))
attempt("hasattr attribute error property", lambda: hasattr(Prop(), "missing"))
attempt("hasattr absent", lambda: hasattr(Prop(), "absent"))
name = "bo" + "om"
attempt("hasattr dynamic name", lambda: hasattr(Prop(), name))
attempt("hash tuple", lambda: hash((1, 2)))
attempt("hash nested", lambda: hash((1, (2, None), -1)))
attempt("hash none", lambda: hash(None))
attempt("hash big", lambda: hash(2 ** 70))
attempt("hash negative big", lambda: hash(-(2 ** 70)))
attempt("issubclass bool int", lambda: issubclass(bool, int))
attempt("bool mro", lambda: [cls.__name__ for cls in bool.__mro__])
attempt("len int", lambda: len(5))
attempt("sorted key len", lambda: sorted([[1], 5], key=len))
attempt("ord two", lambda: ord("ab"))
attempt("ord int", lambda: ord(1))
attempt("ord bytes", lambda: ord(b"z"))
attempt("ord index", lambda: ord("ab"[5]))
attempt("ord negative index", lambda: ord("abc"[-1]))
'''

ANY_ALL = r'''
def gen(values):
    for value in values:
        yield value


class Bag:
    def __iter__(self):
        return iter([0, 0, 7])


class Loud:
    def __init__(self, value):
        self.value = value

    def __bool__(self):
        print("bool", self.value)
        return bool(self.value)


def side(value):
    print("built", value)
    return value


def main():
    g = gen([0, 1])
    print(any(g), all(gen([1, 0])), all(gen([])))
    print(any({0, 1}), all({0}), any(iter([0, 3])))
    print(all(x for x in [1, 0]), any(x for x in [0, 2]))
    print(any("ab"), all(""), any(Bag()), all(Bag()))
    calls = []

    def noisy(values):
        for value in values:
            calls.append(value)
            yield value
    print(any(noisy([0, 5, 9])), calls)
    print(any([side(1), side(0)]), all((Loud(1), Loud(0), Loud(2))))
    print(any(map(lambda x: x > 1, [0, 1, 2])), all(map(lambda x: x < 3, [0, 1, 2])))
    try:
        any(5)
    except TypeError as error:
        print("TypeError", error)


main()
'''

PROGRAMS = [
    pytest.param(DIRECT, id="direct"),
    pytest.param(ANY_ALL, id="anyall"),
]


@pytest.mark.integration
@pytest.mark.parametrize("program", PROGRAMS)
def test_builtin_calls_match_cpython_on_five_collectors(
    program, tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "builtin_calls.py"
    source.write_text(program, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "builtin_calls"
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
