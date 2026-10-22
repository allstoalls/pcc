"""Builtin calls with keywords or unpacking parse arguments as CPython does.

Direct builtin lowerings parse fixed shapes. pow(2, 3, mod=5) and
pow(*args) raised NameError, len(obj=x) and isinstance(*args)
failed to compile, iter(object=x) and print(x, flush=flag) needed
libpython, and min/max with key= inside a lambda failed to
compile. A call shape the direct lowering does not parse now runs the
builtin's Python wrapper (CPython's own argument parsing and messages);
min/max with keywords iterate once and call the key before each
comparison, interleaved like CPython. The program is compared with CPython
on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import sys


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__ + ":", error)


class Loud:
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        print("  lt", self.value, other.value)
        return self.value < other.value

    def __gt__(self, other):
        print("  gt", self.value, other.value)
        return self.value > other.value

    def __repr__(self):
        return "Loud(" + str(self.value) + ")"


def noisy_key(item):
    print("  key", item)
    return -item


def produce():
    for value in (3, 1, 2):
        print("  yield", value)
        yield value


xs = [3, 1, 2]
pair = (2, 10)
attempt("pow mod kw", lambda: pow(2, 3, mod=5))
attempt("pow all kw", lambda: pow(base=2, exp=3, mod=5))
attempt("pow star", lambda: pow(*pair))
attempt("pow star kw", lambda: pow(*pair, **{"mod": 7}))
attempt("pow bad kw", lambda: pow(2, 3, modulus=5))
attempt("sum start", lambda: sum([1, 2], start=10))
attempt("sum star", lambda: sum(*([1, 2],)))
attempt("round ndigits", lambda: round(2.567, ndigits=2))
attempt("round number", lambda: round(number=2.5))
attempt("sorted reverse", lambda: sorted(xs, reverse=True))
attempt("sorted key reverse", lambda: sorted(xs, key=lambda v: -v, reverse=True))
attempt("sorted star", lambda: sorted(*(xs,)))
attempt("sorted bad kw", lambda: sorted(xs, cmp=None))
attempt("min key", lambda: min(xs, key=lambda v: -v))
attempt("max default", lambda: max([], default=5))
attempt("max str default", lambda: max(["a", "c"], default="z"))
attempt("max key default", lambda: max([], key=lambda v: v, default=7))
attempt("min star", lambda: min(*xs))
attempt("max empty", lambda: max([]))
attempt("min empty iter", lambda: min(iter([])))
attempt("max multi default", lambda: max(1, 2, default=0))
attempt("max bad kw", lambda: max(xs, reverse=True))
attempt("max interleaved", lambda: max(produce(), key=noisy_key))
attempt("min loud", lambda: min([Loud(2), Loud(1), Loud(3)], key=lambda item: item))
attempt("len kw", lambda: len(obj=xs))
attempt("len star", lambda: len(*(xs,)))
attempt("abs star", lambda: abs(*(-3,)))
attempt("abs kw", lambda: abs(x=-3))
attempt("divmod star", lambda: divmod(*(7, 2)))
attempt("isinstance star", lambda: isinstance(*(1, int)))
attempt("isinstance kw", lambda: isinstance(1, classinfo=int))
attempt("getattr star", lambda: getattr(*(xs, "append")) is not None)
attempt("hash kw", lambda: hash(obj=1))
attempt("next star", lambda: next(*(iter(xs),)))
attempt("next default star", lambda: next(*(iter([]), "done")))
attempt("iter kw", lambda: iter(object=xs))
attempt("format star", lambda: format(*(3.14159, ".2f")))
attempt("ord star", lambda: ord(*("a",)))
attempt("repr kw", lambda: repr(obj=1))
attempt("chr star", lambda: chr(*(65,)))
flag = True
print(1, 2, sep="-", end="!\n", flush=True)
print(*xs, sep=",")
print("err", file=sys.stdout)
print("flag", flush=flag)
print(*xs, **{"sep": "+"})
attempt("print bad kw", lambda: print("x", color=1))


def nested(values):
    return max(values, key=lambda v: v % 3), min(values, default=None)


print(nested([4, 5, 6]))
'''


@pytest.mark.integration
def test_builtin_call_shapes_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "builtin_shapes.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "builtin_shapes"
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
