"""``__name__`` / ``__qualname__`` of nested defs, lambdas and methods.

Hoisting emits a nested def as ``__nested_<name>``; the function object
reported that internal name as both ``__name__`` and ``__qualname__``, and
methods reported a bare ``__qualname__``.  CPython's names are what reprs and
argument errors print (``outer.<locals>.inner() missing ...``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


SOURCE = """
def outer():
    def inner(x):
        return x
    return inner


def make_lambda():
    f = lambda y: y
    return f


class K:
    def method(self):
        def helper():
            return 1
        return helper


f = outer()
print(f.__name__, f.__qualname__)
h = K().method()
print(h.__name__, h.__qualname__)
print(outer.__name__, outer.__qualname__, K.method.__qualname__)
lam = make_lambda()
print(lam.__name__, lam.__qualname__)
print(repr(f).split(" at ")[0])
try:
    f()
except TypeError as e:
    print(e)
"""


def test_function_names_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(SOURCE).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert native.returncode == 0, native.stderr
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60
    )
    assert expected.returncode == 0, expected.stderr
    assert native.stdout == expected.stdout
