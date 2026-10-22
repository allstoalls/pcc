"""CPython's argument-binding TypeErrors for calls bound at runtime.

A call through a function value (or with ``**`` keywords, which binds at
runtime) used to report ``missing required native function argument`` /
``native function got multiple values for argument`` -- no function name, no
parameter name.  The binder now raises CPython's messages, checked in
CPython's order: keyword problems, too many positional arguments, missing
positional, missing keyword-only.  ``f(**a, **b)`` with a repeated key and a
non-mapping ``**`` operand raise CPython's TypeErrors too.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


SOURCE = """
def named(a, b=2, **rest):
    return (a, b, sorted(rest.items()))


def pos_only(a, b, /, c=3):
    return (a, b, c)


def kw_only(a, *, k, m=1):
    return (a, k, m)


def two(x, y):
    return x + y


def target(**kwargs):
    return sorted(kwargs.items())


calls = [
    lambda f: f(**{"z": 1}),
    lambda f: f(1, **{"a": 2}),
    lambda f: f(),
    lambda f: f(1, 2, 3, 4),
    lambda f: f(1, **{"b": 2}),
    lambda f: f(1, q=5),
]
for f in (named, pos_only, kw_only, two):
    g = f
    for call in calls:
        try:
            print(call(g))
        except TypeError as e:
            print("TypeError:", e)

left = {"a": 1}
right = {"b": 2}
print(named(**left, **right))
print(named(**{"a": 7, "z": 9}))
print(target(**left, **right), target(**left, x=5))
for call in (
    lambda: target(**left, **{"a": 3}),
    lambda: target(**left, a=4),
    lambda: target(**{1: 2}),
    lambda: target(**left, **[1]),
):
    try:
        call()
    except TypeError as e:
        text = str(e)
        print("TypeError:", text.split(") ", 1)[-1] if ") got" in text else text)
"""


def test_runtime_binding_errors_match_cpython(tmp_path):
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
