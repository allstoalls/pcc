"""A ``**`` call reaches the generic call wherever a lowering takes fixed operands.

py_lift, the production parser, keeps ``f(**mapping)`` as a ``**`` marker
call among the arguments (the CPython-ast parser put it under a ``**``
keyword). The guards that admit a fixed-operand lowering checked for no
keywords and no ``*`` argument, so ``int("11", **kw)`` as a call operand took
the int constructor with the marker as its base and raised "NameError: name
'**' is not defined" at run time; ``set``, ``pow``, ``dict.update``,
``dict``, a method on the direct virtual dispatch and the list, tuple, set
and dict method fast paths had the same hole; a typed container call with
unpacked arguments now takes the generic method call, as a dynamic receiver
does. Each of these calls now binds its unpacked arguments.
(The str/bytes method
helpers, ``int.to_bytes``/``from_bytes`` and ``bytes.decode`` decline such a
call too, but binding it natively needs str and bytes methods as
attributes, and ``d.update(**m)`` a bound method that takes keywords; both
are separate work.)
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class Box:
    def __init__(self, value):
        self.value = value

    def scaled(self, factor, offset=0):
        return self.value * factor + offset


def show(label, value):
    print(label, repr(value))


def operands():
    base = {"base": 2}
    show("int operand", int("11", **base))
    show("int nested", [int("101", **base)])
    show("set star", sorted(set(*[[3, 1, 3]])))
    show("set empty", set(**{}))
    show("pow", pow(2, **{"exp": 10}))
    box = Box(4)
    show("method", box.scaled(**{"factor": 3, "offset": 1}))
    show("method mixed", box.scaled(2, **{"offset": 5}))
    return int("111", **base)


def updates():
    table = {"a": 1}
    table.update(*[{"b": 2}])
    show("update", sorted(table.items()))
    show("dict star", sorted(dict(**{"c": 3, "d": 4}).items()))
    return dict(**table)


def containers(dynamic):
    items = [1, 3]
    items.append(*[4])
    items.insert(*[1, 2])
    items.sort(**{"reverse": True})
    show("list", items)
    table = {"k": 5}
    show("dict get", (table.get(*["k", 0]), table.get(*["z", 0])))
    show("dict setdefault", (table.setdefault(*["n", 3]), sorted(table.items())))
    show("tuple count", (1, 2, 1).count(*[1]))
    dynamic.append(*[7])
    show("dynamic list", dynamic)


def errors():
    try:
        set(**{"x": 1})
    except TypeError as caught:
        print("set kw", type(caught).__name__, caught)


show("returned", operands())
show("returned dict", sorted(updates().items()))
containers([6])
errors()
'''


@pytest.mark.integration
def test_unpacked_call_operands_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "unpacked_operands.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "unpacked_operands"
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
