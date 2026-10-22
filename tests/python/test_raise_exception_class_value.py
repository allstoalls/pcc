"""``raise C`` for an exception class held in a variable raises ``C()``.

Codegen instantiates a raised class it resolves statically; the runtime
normalized any other raised object and turned a class into TypeError
"exceptions must derive from BaseException", so raising a function-local
exception class (or any class passed around as a value) failed. The runtime
now follows CPython's do_raise: an exception class is called with no
arguments, an exception from that call is raised instead, and a call that
returns a non-exception raises TypeError.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class ModuleExc(Exception):
    pass


class NeedsArg(Exception):
    def __init__(self, value):
        super().__init__(value)


class Weird(Exception):
    def __new__(cls):
        return 42


def raise_value(value):
    raise value


def main():
    class Local(Exception):
        def __init__(self):
            super().__init__("made by Local()")
            self.flag = "initialized"

    for value in (Local, ModuleExc, ValueError, Weird, int, 3):
        try:
            raise_value(value)
        except BaseException as caught:
            print(type(caught).__name__, caught, getattr(caught, "flag", "-"))
    try:
        raise_value(NeedsArg)
    except TypeError:
        print("NeedsArg() raised TypeError")


main()
'''


@pytest.mark.integration
def test_raised_exception_class_is_instantiated_like_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "raise_class.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "raise_class"
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
