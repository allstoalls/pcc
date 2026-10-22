"""len/subscript/membership/truth on a class use its metaclass's methods.

CPython resolves ``len(cls)``, ``cls[key]``, ``key in cls`` and ``bool(cls)``
on ``type(cls)``, which is how enum classes support ``len(Color)``,
``Color["RED"]`` and ``Color.RED in Color``. Natively ``len(cls)`` returned 0,
``cls[key]`` raised "'type' object is not subscriptable" and ``key in cls``
answered False without consulting the metaclass.
"""
from __future__ import annotations

import os
import subprocess
import sys


PROGRAM = """
class Registry(type):
    def __len__(cls):
        return len(cls.entries)

    def __getitem__(cls, key):
        return cls.entries[key]

    def __contains__(cls, key):
        return key in cls.entries

    def __iter__(cls):
        return iter(sorted(cls.entries))


class Empty(type):
    def __bool__(cls):
        return False


class Codes(metaclass=Registry):
    entries = {"a": 1, "b": 2, "c": 3}


class Nothing(metaclass=Empty):
    pass


class Plain:
    pass


def attempt(label, fn):
    try:
        print(label, fn())
    except Exception as exc:
        print(label, type(exc).__name__)


def main():
    attempt("len", lambda: len(Codes))
    attempt("item", lambda: Codes["b"])
    attempt("missing", lambda: Codes["z"])
    attempt("in", lambda: "c" in Codes)
    attempt("not in", lambda: "z" in Codes)
    attempt("iter", lambda: list(Codes))
    attempt("bool", lambda: bool(Codes))
    attempt("falsy", lambda: bool(Nothing))
    attempt("plain bool", lambda: bool(Plain))
    attempt("plain item", lambda: Plain[0])


main()
"""

EXPECTED = (
    "len 3\n"
    "item 2\n"
    "missing KeyError\n"
    "in True\n"
    "not in False\n"
    "iter ['a', 'b', 'c']\n"
    "bool True\n"
    "falsy False\n"
    "plain bool True\n"
    "plain item TypeError\n"
)


def test_class_protocols_dispatch_to_the_metaclass(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "metaclass_protocols.py"
    exe = tmp_path / "metaclass_protocols.out"
    src.write_text(PROGRAM)
    ref = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    )
    assert ref.returncode == 0, ref.stderr
    assert ref.stdout == EXPECTED
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    for backend in range(5):
        native = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert native.returncode == 0, f"GC{backend}: {native.stderr}"
        assert native.stdout == EXPECTED, f"GC{backend}"
