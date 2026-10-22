"""Dict and list methods bind on a receiver the compiler cannot type.

``py_obj_getattr`` bound only list.append/pop/sort, dict.pop and set.pop on
plain containers, so ``d.get(...)`` through an untyped attribute, a
``getattr`` result or configparser's ``current_section`` (a dict assigned
to a None-initialised local) raised "'dict' object has no attribute 'get'".
Every method binds now, calls the runtime entry the typed lowering uses and
reports CPython's argument-count TypeErrors. list.remove of a missing item
raised ArithmeticError (exception tag 12) and the typed call never checked
it, so ``except ValueError`` missed it. EXPECTED is CPython 3.15.0's
output; the native binary must print it under every collector.
"""
from __future__ import annotations

import os
import subprocess


PROGRAM = r'''
import configparser


class Box:
    def __init__(self, value):
        self.value = value


def call(obj, name, *args):
    return getattr(obj, name)(*args)


def arity(obj, cases):
    for name, args in cases:
        try:
            call(obj, name, *args)
            print("no error", name)
        except TypeError as error:
            print("TypeError", error)


def dict_methods(holder):
    holder.value = {"a": 1, "b": 2}
    d = holder.value
    print(d.get("a"), d.get("z"), d.get("z", 9))
    print(sorted(d.keys()), sorted(d.values()), sorted(d.items()))
    print(d.setdefault("c", 3), d.setdefault("a", 7), d.setdefault("n"))
    d.update({"e": 5})
    d.update([("f", 6)])
    d.update()
    print(sorted(d.keys()), d["n"], d["f"])
    copied = d.copy()
    print(copied == d, copied is d, len(copied))
    print(d.popitem(), d.popitem())
    print("a" in d, d.__contains__("zz"), d.__getitem__("a"))
    try:
        d.__getitem__("missing")
    except KeyError as error:
        print("KeyError", error)
    d.clear()
    print(d, len(copied))
    arity(d, (("get", ()), ("get", (1, 2, 3)), ("keys", (1,)), ("values", (1,)),
              ("update", ({}, {})), ("setdefault", ()), ("copy", (1,)),
              ("clear", (1,)), ("popitem", (1,)), ("__getitem__", ())))
    bound = getattr({"k": "v"}, "get")
    print(bound("k"), bound("x", "default"))


def list_methods(holder):
    holder.value = [3, 1, 2]
    items = holder.value
    items.extend([4, 5])
    items.extend((6,))
    items.insert(0, 0)
    items.insert(100, 9)
    items.insert(-1, 8)
    items.remove(9)
    print(items, items.index(4), items.index(1, 2), items.index(2, -5, -1), items.count(1))
    items.reverse()
    print(items, items.copy() == items, items.copy() is items)
    try:
        items.index(42)
    except ValueError as error:
        print("ValueError", error)
    try:
        items.remove(42)
    except ValueError as error:
        print("ValueError", error)
    items.clear()
    print(items)
    arity(items, (("extend", ()), ("insert", (1,)), ("remove", ()), ("index", ()),
                  ("index", (1, 2, 3, 4)), ("count", ()), ("reverse", (1,)),
                  ("copy", (1,)), ("clear", (1,))))


def typed_remove():
    values = [1, 2]
    try:
        values.remove(5)
        print("no error")
    except ValueError as error:
        print("typed ValueError", error)
    values.remove(1)
    print(values)


def continuation():
    parser = configparser.ConfigParser()
    parser.read_string("[s]\nk = first\n  second\n\n  third\nother = x\n")
    print(repr(parser.get("s", "k")), repr(parser.get("s", "other")))


holder = Box(None)
dict_methods(holder)
list_methods(holder)
typed_remove()
continuation()
'''

EXPECTED = """\
1 None 9
['a', 'b'] [1, 2] [('a', 1), ('b', 2)]
3 1 None
['a', 'b', 'c', 'e', 'f', 'n'] None 6
True False 6
('f', 6) ('e', 5)
True False 1
KeyError 'missing'
{} 6
TypeError get expected at least 1 argument, got 0
TypeError get expected at most 2 arguments, got 3
TypeError dict.keys() takes no arguments (1 given)
TypeError dict.values() takes no arguments (1 given)
TypeError update expected at most 1 argument, got 2
TypeError setdefault expected at least 1 argument, got 0
TypeError dict.copy() takes no arguments (1 given)
TypeError dict.clear() takes no arguments (1 given)
TypeError dict.popitem() takes no arguments (1 given)
TypeError dict.__getitem__() takes exactly one argument (0 given)
v default
[0, 3, 1, 2, 4, 5, 6, 8] 4 2 3 1
[8, 6, 5, 4, 2, 1, 3, 0] True False
ValueError list.index(x): x not in list
ValueError list.remove(x): x not in list
[]
TypeError list.extend() takes exactly one argument (0 given)
TypeError insert expected 2 arguments, got 1
TypeError list.remove() takes exactly one argument (0 given)
TypeError index expected at least 1 argument, got 0
TypeError index expected at most 3 arguments, got 4
TypeError list.count() takes exactly one argument (0 given)
TypeError list.reverse() takes no arguments (1 given)
TypeError list.copy() takes no arguments (1 given)
TypeError list.clear() takes no arguments (1 given)
typed ValueError list.remove(x): x not in list
[2]
'first\\nsecond\\n\\nthird' 'x'
"""


def test_native_dynamic_dict_and_list_methods_match_cpython(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "dynamic_methods.py"
    output = tmp_path / "dynamic_methods"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", recursive_stdlib=True,
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH="",
                     PCC_HOST_PYTHON="/nonexistent/host-python"),
        )
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == EXPECTED, f"GC{gc}: " + result.stdout
