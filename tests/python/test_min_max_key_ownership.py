"""min()/max() with a key: CPython semantics and bounded ownership.

A key that is not one of the structural shapes (attribute, index, ...) used
to compile to a call of the ``max`` builtin, which does not exist without
libpython: the program built and then raised NameError.  pcc's own
``ir_passes/gvn.py`` has such a call.  Every key now takes the native fold:
the key runs once per element (the old fold evaluated it twice per
comparison), the first extreme element wins, an empty input returns
``default=`` or raises ValueError, and elements, keys and the result are
owned and released on every path, including a key that collects or raises.
"""

import os
import subprocess
import sys
import textwrap

import pytest


SEMANTICS = '''import gc


class Item:
    def __init__(self, index):
        self.index = index


class Key:
    def __init__(self, bias):
        self.bias = bias

    def __call__(self, value):
        return abs(value - self.bias)


calls = []


def counted(value):
    calls.append(value)
    return value % 3


def collecting(value):
    gc.collect()
    return -value


def rank(c, t):
    return abs(c - t)


def main():
    members = [1, 5, 9]
    target = 4
    print(max(members, key=lambda c: abs(c - target)), min(members, key=lambda c: abs(c - target)))
    print(max(members, key=lambda c: rank(c, target)), min(members, key=lambda c: rank(c, target)))
    print(max([1, 3, 3, 2], key=lambda v: 0), min([5, 4, 6], key=lambda v: 0))
    print(max([4, 7, 5, 8], key=counted), calls)
    print(max([], key=lambda v: v, default="none"), min([], key=lambda v: abs(v), default=None))
    try:
        max([], key=lambda v: v)
    except ValueError as exc:
        print("ValueError", exc)
    try:
        min([], key=lambda v: v)
    except ValueError as exc:
        print("ValueError", exc)
    print(max((v * 2 for v in range(5)), key=lambda v: -abs(v - 5)))
    items = [Item(i) for i in range(4)]
    print(max(items, key=lambda it: it.index).index, min(items, key=lambda it: it.index).index)
    print(max([3, 10, 6], key=Key(7)), min([3, 10, 6], key=Key(7)))
    try:
        max([1, 2], key=lambda v: 1 // (v - 2))
    except ZeroDivisionError:
        print("ZeroDivisionError")
    print(max(range(50), key=collecting), min(list(range(50)), key=collecting))
    words = ["bb", "a", "ccc", "dd"]
    print(max(words, key=len), min(words, key=len), max(words, key=lambda w: (len(w), w)))


main()
'''

EXPECTED = """9 5
9 5
1 5
5 [4, 7, 5, 8]
none None
ValueError max() iterable argument is empty
ValueError min() iterable argument is empty
4
3 0
3 6
ZeroDivisionError
0 49
ccc a ccc
"""


def test_min_max_key_matches_cpython_under_every_collector(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "min_max_key.py"
    source.write_text(SEMANTICS, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == EXPECTED
    binary = tmp_path / "min_max_key"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == EXPECTED, f"GC{backend}"


_OWNER_PROGRAM = '''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
created = 0
destroyed = 0
class Item:
    def __init__(self, index):
        global created
        self.index = index
        self.payload = b"x" * 1024
        created += 1
    def __del__(self):
        global destroyed
        destroyed += 1
class Key:
    def __init__(self, payload):
        self.payload = payload
    def __call__(self, index):
        return self.payload[index].index
def factory(payload):
    return lambda index: payload[index].index
def fail(payload, index):
    assert payload[index].index == index
    raise ValueError("key failed")
def collected(payload, index):
    gc.collect()
    return payload[index].index
def exercise(order: list):
    payload = [Item(i) for i in range(300)]
    BODY
def run():
    exercise([2, 0, 1])
def main():
    run()
    gc.collect()
    gc.collect()
    assert created == destroyed
    before = heap()
    run()
    gc.collect()
    gc.collect()
    assert created == destroyed
    print(created, destroyed, heap() - before)
main()
'''

_OWNER_BODIES = {
    "lambda": (
        "assert max(order, key=lambda index: payload[index].index) == 2\n"
        "assert min(order, key=lambda index: payload[index].index) == 0"
    ),
    "callable": (
        "assert max(order, key=Key(payload)) == 2\n"
        "assert min(order, key=Key(payload)) == 0"
    ),
    "factory": "assert max(order, key=factory(payload)) == 2",
    "collect": "assert max(order, key=lambda index: collected(payload, index)) == 2",
    "raising": (
        "try:\n"
        "    max(order, key=lambda index: fail(payload, index))\n"
        "except ValueError as exc:\n"
        "    assert str(exc) == 'key failed'\n"
        "else:\n"
        "    assert False"
    ),
    "empty_default": (
        "result = max([], key=lambda index: payload[index].index, default=payload[0])\n"
        "assert result is payload[0]"
    ),
    "empty_raise": (
        "try:\n"
        "    min([], key=lambda index: payload[index].index)\n"
        "except ValueError:\n"
        "    pass\n"
        "else:\n"
        "    assert False"
    ),
    "items": (
        "result = max(payload, key=lambda item: item.index)\n"
        "assert result is payload[-1]\n"
        "del payload\n"
        "gc.collect()\n"
        "assert result.index == 299"
    ),
    "generator": (
        "assert max((index for index in order), key=lambda index: payload[index].index) == 2"
    ),
}


@pytest.mark.parametrize("shape", sorted(_OWNER_BODIES))
def test_min_max_key_owners_are_released(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive, shape,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "min_max_owners.py"
    source.write_text(_OWNER_PROGRAM.replace(
        "    BODY\n", textwrap.indent(_OWNER_BODIES[shape], "    ") + "\n"
    ))
    binary = tmp_path / "min_max_owners"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        created, destroyed, retained = map(int, result.stdout.split())
        assert created == destroyed == 600
        if backend == 0:
            assert retained < 16384, retained
