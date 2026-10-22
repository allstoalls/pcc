"""Builtin value types called through their type objects parse like CPython.

``kind(*args, **kwargs)`` with ``kind`` bound to ``int``, ``str``,
``bytes``, ``bytearray``, ``complex`` or ``dict`` reached a native type call
that accepted at most one positional argument: ``int("7f", 16)`` raised
"native builtin constructor accepts at most one positional argument",
``bytes``/``bytearray``/``complex`` were instantiated as if they were user
classes, and ``dict(pairs)`` required a dict. They now unpack their
arguments as the argument clinic does (positions, keywords, then keywords
given twice or outside the signature) into a frame whose parameter slots
each hold a counted lease, so a callback such as ``__index__`` or
``__complex__`` that allocates cannot move an operand under the
constructor. A direct ``complex(...)`` call uses the same constructor, so
strings, ``__complex__`` and complex parts follow ``complex_new``.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
def churn():
    # Allocation inside a callback, so a collection can run mid-constructor.
    return len([str(i) * 3 for i in range(400)])


class Indexish:
    def __index__(self):
        churn()
        return 7


class Floatish:
    def __float__(self):
        churn()
        return 1.5


class Complexish:
    def __complex__(self):
        churn()
        return 2 + 3j


class BadComplex:
    def __complex__(self):
        return 5


class Texty:
    def __str__(self):
        churn()
        return "texty"


class Pairs:
    def __iter__(self):
        return iter([("p", 1), ("q", 2)])


def build(kind, *args, **kwargs):
    return kind(*args, **kwargs)


def show(label, make):
    try:
        print(label, repr(make()))
    except Exception as caught:
        print(label, type(caught).__name__, caught)


def ints():
    show("int hex", lambda: build(int, "7f", 16))
    show("int base kw", lambda: build(int, "11", base=2))
    show("int none", lambda: build(int))
    show("int float", lambda: build(int, 5.7))
    show("int index", lambda: build(int, Indexish()))
    show("int total", lambda: build(int, "5", 10, base=2))
    show("int many", lambda: build(int, 1, 2, 3))
    show("int kw", lambda: build(int, x=1))
    show("int base only", lambda: build(int, base=2))
    show("int nonstr base", lambda: build(int, 5, 16))


def strs():
    show("str decode", lambda: build(str, b"ab", "ascii"))
    show("str decode kw", lambda: build(str, b"ab", encoding="ascii"))
    show("str object kw", lambda: build(str, object=5))
    show("str none", lambda: build(str))
    show("str user", lambda: build(str, Texty()))
    show("str twice", lambda: build(str, b"x", "ascii", encoding="ascii"))
    show("str many", lambda: build(str, 1, 2, 3, 4))
    show("str encoding type", lambda: build(str, b"x", 5))
    show("str bad kw", lambda: build(str, b"x", codec="ascii"))


def byte_types():
    show("bytes encode", lambda: build(bytes, "ab", "ascii"))
    show("bytes kw", lambda: build(bytes, source="ab", encoding="ascii"))
    show("bytes ints", lambda: build(bytes, [104, 105]))
    show("bytes count", lambda: build(bytes, 3))
    show("bytes none", lambda: build(bytes))
    show("bytearray encode", lambda: build(bytearray, "ab", "ascii"))
    show("bytearray copy", lambda: build(bytearray, b"xy"))
    show("bytearray none", lambda: build(bytearray))
    show("bytes many", lambda: build(bytes, 1, 2, 3, 4))
    show("bytes kw many", lambda: build(bytes, source=1, encoding="a", errors="b", x=4))
    show("bytes str alone", lambda: build(bytes, "ab"))
    show("bytes encoding int", lambda: build(bytes, 5, "ascii"))
    show("bytes errors int", lambda: build(bytes, 5, errors="strict"))
    show("bytes encoding alone", lambda: build(bytes, encoding="ascii"))
    show("bytes encoding type", lambda: build(bytes, "a", 5))
    show("bytearray bad kw", lambda: build(bytearray, x=1))
    show("bytearray str alone", lambda: build(bytearray, "ab", errors="strict"))


def complexes():
    show("complex pair", lambda: build(complex, 1, 2))
    show("complex kw", lambda: build(complex, real=1, imag=2))
    show("complex imag only", lambda: build(complex, imag=2))
    show("complex text", lambda: build(complex, "1+2j"))
    show("complex spaced", lambda: build(complex, " ( -j ) "))
    show("complex exponent", lambda: build(complex, "\t1-2.5e3J\n"))
    show("complex digits", lambda: build(complex, "1_0+2_0j"))
    show("complex parts", lambda: build(complex, 1j, 1j))
    show("complex special", lambda: build(complex, Complexish()))
    show("complex special pair", lambda: build(complex, Complexish(), 1))
    show("complex float", lambda: build(complex, Floatish()))
    show("complex index", lambda: build(complex, Indexish()))
    show("complex both", lambda: build(complex, Floatish(), Indexish()))
    show("complex none", lambda: build(complex))
    show("complex many", lambda: build(complex, 1, 2, 3))
    show("complex text pair", lambda: build(complex, "1", 2))
    show("complex text imag", lambda: build(complex, 1, "2"))
    show("complex object", lambda: build(complex, object()))
    show("complex imag object", lambda: build(complex, 1, Complexish()))
    show("complex hex", lambda: build(complex, "0x1p3"))
    show("complex payload", lambda: build(complex, "nan(1)"))
    show("complex underscores", lambda: build(complex, "1__0j"))
    show("complex open", lambda: build(complex, "(1+2j"))
    show("complex trailing", lambda: build(complex, "1j+2"))
    show("complex empty", lambda: build(complex, ""))
    show("complex bad special", lambda: build(complex, BadComplex()))
    show("complex twice", lambda: build(complex, 1, real=2))
    show("complex bool", lambda: build(complex, True, False))


def dicts():
    show("dict pairs kw", lambda: build(dict, [("a", 1)], b=2))
    show("dict override", lambda: build(dict, {"a": 1}, a=2))
    show("dict kw only", lambda: build(dict, a=1))
    show("dict iterable", lambda: build(dict, Pairs()))
    show("dict items", lambda: build(dict, {"a": 1}.items(), b=2))
    show("dict many", lambda: build(dict, 1, 2))
    show("dict int", lambda: build(dict, 5))
    show("dict short pair", lambda: build(dict, ["ab", "c"]))


def single_argument_types():
    show("float text", lambda: build(float, "2.5"))
    show("float index", lambda: build(float, Indexish()))
    show("float kw", lambda: build(float, x=1))
    show("float many", lambda: build(float, 1, 2))
    show("list kw", lambda: build(list, x=1))
    show("tuple many", lambda: build(tuple, 1, 2))
    show("set kw", lambda: build(set, x=1))
    show("bool many", lambda: build(bool, 1, 2))
    show("bool kw", lambda: build(bool, x=1))


def direct_complex():
    show("direct text", lambda: complex("1+2j"))
    show("direct parts", lambda: complex(1j, 1j))
    show("direct special", lambda: complex(Complexish()))
    show("direct pair", lambda: complex(Floatish(), 2))
    show("direct signed zero", lambda: complex(1, -0.0))
    show("direct malformed", lambda: complex("1+"))
    show("direct object", lambda: complex(None))
    show("direct none", lambda: complex())
    value = complex(Indexish(), Floatish())
    print("direct rooted", value.real, value.imag)


ints()
strs()
byte_types()
complexes()
dicts()
single_argument_types()
direct_complex()
'''


@pytest.mark.integration
def test_builtin_constructor_type_calls_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "constructor_type_calls.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, "-W", "ignore", str(source)],
        capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "constructor_type_calls"
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
