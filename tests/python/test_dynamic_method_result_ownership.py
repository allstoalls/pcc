"""Results of dynamically dispatched list-method names keep one owner.

``receiver.append(item)`` on an untyped receiver lowers to a list fast path
and a generic method call joined by a PHI. Without a consumer root, that join
was treated as borrowed: a user ``append`` (or ``insert``/``copy``) result
passed to a direct call, returned, assigned or discarded kept one extra
reference and was never finalized. The generic arm also kept the bound method
from ``py_obj_getattr``, which keeps its receiver alive. Names the dict route
claims first (copy, pop, ...) still leak a user method's result; see
docs/status.md.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
import gc

drops = []


class Item:
    def __init__(self, label):
        self.label = label

    def __del__(self):
        drops.append(self.label)


class Recorder:
    def __init__(self, name):
        self.name = name

    def append(self, value):
        return Item(self.name + ":" + str(value))

    def insert(self, index, value):
        return Item(self.name + ":insert")

    def __del__(self):
        drops.append("recorder " + self.name)


def take(value):
    return value


def take_kw(*, value):
    return value


def as_argument(receiver, item):
    return take(receiver.append(item)).label


def as_keyword(receiver, item):
    return take_kw(value=receiver.append(item)).label


def as_return(receiver, item):
    return receiver.append(item)


def as_assignment(receiver, item):
    result = receiver.append(item)
    return result.label


def as_statement(receiver, item):
    receiver.append(item)
    return "statement"


def other_methods(receiver):
    first = receiver.insert(0, 1)
    return first.label


def run(label, function, name, item):
    result = function(Recorder(name), item)
    if not isinstance(result, str):
        result = result.label
    gc.collect()
    gc.collect()
    print(label, result, sorted(drops))
    drops.clear()


def main() -> None:
    run("argument", as_argument, "a", 1)
    run("keyword", as_keyword, "k", 2)
    run("return", as_return, "r", 3)
    run("assignment", as_assignment, "s", 4)
    run("statement", as_statement, "t", 5)
    run("other", lambda receiver, item: other_methods(receiver), "o", 0)
    plain = []
    as_statement(plain, 6)
    print(as_argument.__name__, take(plain.append(7)) is None, plain)


main()
'''


def test_dynamic_list_method_results_are_released_under_five_collectors(
        tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "dynamic_method_results.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "dynamic_method_results"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
