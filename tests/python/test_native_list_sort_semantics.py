"""``list.sort`` follows CPython's listsort through every entry.

The in-place lowering reversed a completed ascending sort for
``reverse=True``, so items with equal keys came out in reverse order
(``[1, 1.0, True].sort(reverse=True)`` gave ``[True, 1.0, 1]``); listsort
reverses before and after a stable ascending sort, keeping them in their
original order. ``sort`` reached as a method object (``f = xs.sort``, or a
call the generic method call binds) was an insertion sort that read ``key=``
and silently ignored ``reverse=`` and every other keyword; it now binds
CPython's keyword-only signature, sorts with the stable merge sort and
raises "list modified during sort" when a key changes the list. A key
callable and an ordinary class's ``__lt__`` run arbitrary code -- a
collection under the moving collector (GC4) left the in-place transform
reading a stale ``payload`` -- so those sorts go through the same leased
runtime sort as sorted(); a class without ``__lt__`` raises TypeError
instead of being left unsorted.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import gc


def show(label, value):
    print(label, repr(value))


def direct():
    records = [(1, "a"), (0, "z"), (1, "b")]
    records.sort(key=lambda record: record[0], reverse=True)
    show("key reverse", records)
    mixed = [1, 1.0, True]
    mixed.sort(reverse=True)
    show("reverse", mixed)
    mixed.sort()
    show("ascending", mixed)


def dynamic(values):
    values.sort(reverse=True)
    return values


def method_object():
    numbers = [3, 1, 2]
    method = numbers.sort
    method(key=lambda value: -value, reverse=True)
    show("method", numbers)
    pairs = [(1, "x"), (0, "y"), (1, "w")]
    sort_pairs = pairs.sort
    sort_pairs(key=lambda pair: pair[0], reverse=True)
    show("method stable", pairs)
    method(reverse=0)
    show("method falsy", numbers)
    for label, arguments, keywords in (("positional", (1,), {}), ("keyword", (), {"x": 1})):
        try:
            method(*arguments, **keywords)
        except TypeError as caught:
            print(label, type(caught).__name__, caught)
    grow = [3, 1, 2]

    def key(value):
        grow.append(value)
        return value

    try:
        grow.sort(key=key)
    except ValueError as caught:
        print("modified", type(caught).__name__, caught, grow)
    broken = [3, "a", 1]
    try:
        broken.sort()
    except TypeError as caught:
        print("unorderable", type(caught).__name__, len(broken), 3 in broken and "a" in broken and 1 in broken)


class Item:
    def __init__(self, index):
        self.index = index
        self.payload = b"x" * 256


class Ranked:
    def __init__(self, rank):
        self.rank = rank

    def __lt__(self, other):
        gc.collect()
        return self.rank < other.rank


class Plain:
    pass


def collected(payload, index):
    gc.collect()
    return payload[index].index


def collecting():
    # Keys and comparisons that collect: every operand must survive a moving
    # collection inside the callback.
    payload = [Item(i) for i in range(200)]
    order = [5, 0, 3, 1]
    order.sort(key=lambda index: collected(payload, index), reverse=True)
    show("collecting key", order)
    ranked = [Ranked(3), Ranked(1), Ranked(2)]
    ranked.sort()
    show("collecting lt", [item.rank for item in ranked])
    plain = [Plain(), Plain()]
    try:
        plain.sort()
    except TypeError as caught:
        print("unordered", type(caught).__name__)


direct()
show("dynamic", dynamic([1, 1.0, True]))
method_object()
collecting()
show("sorted", sorted([1, 1.0, True], reverse=True))
'''


@pytest.mark.integration
def test_list_sort_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "list_sort.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "list_sort"
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
