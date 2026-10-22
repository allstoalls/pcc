"""A class body's compound statements run when the class is created.

class_gen ran a class body as events for its attribute bindings,
annotations, methods and expression statements; an ``if``, ``for``,
``while``, ``try``, ``with``, ``import``, augmented assignment or ``del`` in a
class body was silently dropped, so ``class C: if True: flag = 1`` left
``C.flag`` unbound, and so was an assignment to a subscript or attribute
(``table[key] = value``). class_body_statements now makes each such statement a
hidden static class-body function that stores its bindings with ``setattr``
and reads class names from ``cls.__dict__`` first (STORE_NAME / LOAD_NAME),
and class_gen calls it with the class in source order. A function defined in
the statement is hoisted like a split method and bound by it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pcc.frontends.python import py_ast as pa
from pcc.frontends.python.class_body_statements import RUN_FUNCTION, run_call_parts
from pcc.frontends.python.method_decorators import public_method_name


PROGRAM = r'''
import sys

events = []


class Box:
    def __enter__(self):
        events.append("enter")
        return "context value"

    def __exit__(self, *exc):
        events.append("exit")
        return False


class C:
    base = 1
    if sys.maxsize > 2:
        flag = "big"

        def method(self):
            return "first method"
    else:
        flag = "small"

        def method(self):
            return "second method"
    if False:
        never = 1
    total = 0
    for item in range(4):
        total += item
    base = base + 1
    if base == 2:
        base += 10
    try:
        raise KeyError("k")
    except KeyError as caught:
        handled = str(caught)
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise ValueError("outer")
    except ValueError as outer:
        escaped = str(outer)
    with Box() as context:
        events.append("inside with")
    import math as math_module
    from math import sqrt
    countdown = 3
    while countdown > 0:
        countdown -= 1
    doomed = "x"
    if True:
        del doomed
    squares = [item * item for item in range(base)]
    events.append("body done")


class Lexer:
    keywords = ("INT", "CHAR", "_BOOL")
    keyword_map = {}
    for keyword in keywords:
        if keyword == "_BOOL":
            keyword_map["_Bool"] = keyword
        else:
            keyword_map[keyword.lower()] = keyword
    keyword_map["typeof"] = "TYPEOF"
    keyword_map.pop("char", None)
    first, second = keywords[0], keywords[1]


def make(label):
    class Local:
        if label:
            tag = "tag " + label
        else:
            tag = "empty"
        for letter in label:
            last = letter
    return Local


def main():
    print(C.flag, C().method(), hasattr(C, "never"), C.total, C.item, C.base)
    print(C.handled, hasattr(C, "caught"), C.context, C.math_module.floor(2.5), C.sqrt(16.0))
    print(C.escaped, hasattr(C, "inner"), hasattr(C, "outer"))
    print(C.countdown, hasattr(C, "doomed"), len(C.squares), C.squares[-1])
    first = make("ab")
    second = make("")
    print(first.tag, first.last, second.tag, hasattr(second, "last"))
    print(sorted(Lexer.keyword_map.items()), Lexer.keyword, Lexer.first, Lexer.second)
    print(events)


main()
'''


def _class_body(source):
    from pcc.frontends.python.py_lift import parse_and_lift
    module = parse_and_lift(source, "blocks.py", "blocks")
    for stmt in module.body:
        if isinstance(stmt, pa.ClassDef):
            return stmt.body
    raise AssertionError("no class")


def test_compound_statement_becomes_a_hidden_function_and_its_call():
    body = _class_body(
        "class C:\n"
        "    x = 1\n"
        "    if x:\n"
        "        y = x + 1\n"
        "        def m(self):\n            return y\n"
        "    z = 3\n"
    )
    kinds = [type(stmt).__name__ for stmt in body]
    assert kinds == ["Assign", "FuncDef", "FuncDef", "ExprStmt", "Assign"]
    hidden, function, run = body[1], body[2], body[3]
    # The defined function is hoisted unpublished, under its source name.
    assert public_method_name(hidden.name) == "m" and hidden.name != "m"
    assert public_method_name(function.name) == "run"
    assert [dec.ident for dec in function.decorators] == ["staticmethod"]
    parts = run_call_parts(run)
    assert parts == (function.name, ("y", "m"), (hidden.name,))
    assert run.expr.func.ident == RUN_FUNCTION


@pytest.mark.parametrize("statement", (
    "    if True:\n        class Inner:\n            pass\n",
    "    if (n := 1):\n        pass\n",
    "    if True:\n        global g\n",
))
def test_unmodelled_shapes_fail_closed(statement):
    from pcc.frontends.python.py_lift import LiftError

    # Lifting reports the transform's NotImplementedError with its location.
    with pytest.raises(LiftError, match="NotImplementedError: class-body statement"):
        _class_body("class C:\n" + statement)


@pytest.mark.integration
def test_class_body_compound_statements_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "class_blocks.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "class_blocks"
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
