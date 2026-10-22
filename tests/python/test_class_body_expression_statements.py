"""A class body's expression statements run, in order with its bindings.

Class creation emitted events only for the body's attribute bindings,
annotations, methods and property accessors; an expression statement such
as ``events.append("body")`` was silently dropped, so a class body that
recorded or registered something did nothing natively (CPython prints
``['body of Point'] 1`` where pcc printed ``[] 0``). Expression statements
are now class-creation events in source order; a string literal still does
nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
events = []


def note(label):
    events.append(label)
    return label


class Point:
    "Docstring stays documentation."
    note("first statement")
    x = len(events)
    events.append("second statement")
    "a later string literal does nothing"

    def method(self):
        return "method"

    note("after method " + str(x))
    y = len(events)


def make(tag):
    class Local:
        note("local " + tag)
        value = tag * 2
    return Local


def main():
    print(events, Point.x, Point.y, Point.__doc__, Point().method())
    print(make("a").value, make("b").value)
    print(events)


main()
'''


@pytest.mark.integration
def test_class_body_expression_statements_run_like_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "class_body_statements.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "class_body_statements"
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
