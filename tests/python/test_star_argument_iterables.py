"""A * argument of a user iterable reaches its __iter__ / __getitem__.

py_obj_special_present read the instance's class slot one byte off (it
passed 1, a byte offset, to pcc_gc_resolve_root_slot_unlocked), so
f(*obj) for a class instance crashed in _special_lookup_locked on every
collector (test_binder_contract_native_five_gc's star-iteration cells on
master). Compared with CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
def target(a, b=2, *items, c=3):
    return (a, b, items, c)


class CallIterable:
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        return iter(self.values)


class Indexed:
    def __getitem__(self, index):
        if index < 3:
            return index * 10
        raise IndexError(index)


def gen():
    yield 7
    yield 8


def probe():
    print(target(*CallIterable((1, 2, 4))))
    print(target(*CallIterable((1, 2, 4)), c=9))
    print(target(0, *CallIterable((5,))))
    print(target(*Indexed()))
    print(target(*gen()))
    print(target(*"ab"))
    print(target(*{1: "x", 2: "y"}))
    try:
        target(*5)
    except TypeError as error:
        print("TypeError", error)


probe()
'''


@pytest.mark.integration
def test_star_arguments_of_user_iterables_match_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "star_arguments.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "star_arguments"
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
