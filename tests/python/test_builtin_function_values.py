"""Builtin functions are first-class values with CPython's behaviour.

``f = len`` raised NameError natively (only a handful of builtins had value
adapters), ``builtins.len`` needed libpython, and the existing values printed
as ``<function abs at ...>`` with ``type(abs).__name__ == 'object'``. Every
function object reported ``object`` as its type, so ``types.FunctionType``
(``type(lambda: None)``) accepted any object.

The values program calls each builtin through a first-class reference with
ordinary, keyword and wrong-arity arguments; the objects program checks
identity across spellings, repr, type objects, metadata, ``types``
membership, hashing, and builtins passed as sorted/max keys and through
``*args``. Both compare with CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


CASES = r'''
class Obj:
    def __init__(self):
        self.a = 1

    def __repr__(self):
        return "Obj()"


class Rounded:
    def __repr__(self):
        return "Rounded()"

    def __round__(self, ndigits=None):
        return ("rounded", ndigits)


def counter_factory():
    state = [0]

    def tick():
        state[0] += 1
        return state[0]
    return tick


def run(label, target, args, kwargs):
    try:
        result = target(*args, **kwargs)
        print(label, "->", repr(result))
    except Exception as error:
        print(label, "->", type(error).__name__ + ":", str(error))


def cases(targets):
    obj = Obj()
    rows = [
        ("abs", (-3,), {}), ("abs", (2.5,), {}), ("abs", ("x",), {}), ("abs", (), {}), ("abs", (1, 2), {}), ("abs", (1,), {"zz": 1}),
        ("all", ([1, 0],), {}), ("all", ([],), {}), ("all", (5,), {}),
        ("any", ([0, 0, 1],), {}), ("any", ([],), {}), ("any", (), {}),
        ("ascii", ("é",), {}), ("ascii", ([1, "ü"],), {}),
        ("bin", (10,), {}), ("bin", (-3,), {}), ("bin", (1.5,), {}),
        ("callable", (5,), {}), ("callable", (Obj,), {}), ("callable", (run,), {}),
        ("chr", (65,), {}), ("chr", (128512,), {}), ("chr", (-1,), {}),
        ("divmod", (7, 2), {}), ("divmod", (-7, 2), {}), ("divmod", (7.5, 2), {}), ("divmod", (1, 0), {}), ("divmod", (1,), {}),
        ("format", (3.14159, ".2f"), {}), ("format", (42,), {}), ("format", ("x", ">3"), {}), ("format", (1, 2), {}), ("format", (), {}), ("format", (1, "", 3), {}),
        ("getattr", (obj, "a"), {}), ("getattr", (obj, "missing", 7), {}), ("getattr", (obj, "missing"), {}), ("getattr", (obj,), {}), ("getattr", (obj, "a"), {"x": 1}), ("getattr", (1, 2, 3, 4), {}),
        ("hasattr", (obj, "a"), {}), ("hasattr", (obj, "nope"), {}), ("hasattr", (obj, 3), {}),
        ("hash", (1,), {}), ("hash", ((1, 2),), {}), ("hash", ([],), {}),
        ("hex", (255,), {}), ("hex", (-1,), {}),
        ("isinstance", (1, int), {}), ("isinstance", ("a", (int, str)), {}), ("isinstance", (1, 5), {}), ("isinstance", (1,), {}),
        ("issubclass", (bool, int), {}), ("issubclass", (int, (str, bytes)), {}), ("issubclass", (1, int), {}),
        ("len", ([1, 2, 3],), {}), ("len", ("abc",), {}), ("len", (5,), {}), ("len", (), {}), ("len", ([1],), {"obj": 1}),
        ("max", ([3, 1, 2],), {}), ("max", (3, 1, 2), {}), ("max", ([],), {}), ("max", ([],), {"default": 0}),
        ("max", (["bb", "a", "ccc"],), {"key": lambda s: len(s)}), ("max", (1, 2), {"default": 0}), ("max", (1, 2), {"zz": 1}), ("max", (), {}),
        ("max", (1, 2), {"key": lambda v: -v}), ("max", ([1, 3, 3],), {"key": None}),
        ("min", ([3, 1, 2],), {}), ("min", (3, 1, 2), {}), ("min", ([],), {}), ("min", ([],), {"default": "d"}), ("min", ([3, 1],), {"key": None}),
        ("min", ([(1, "b"), (1, "a")],), {"key": lambda t: t[0]}),
        ("oct", (8,), {}), ("ord", ("a",), {}), ("ord", ("ab",), {}), ("ord", (1,), {}),
        ("pow", (2, 10), {}), ("pow", (2, -1), {}), ("pow", (2, 10, 1000), {}), ("pow", (), {"base": 2, "exp": 3}),
        ("pow", (2, 3), {"mod": 5}), ("pow", (2,), {"exp": 3}), ("pow", (), {}), ("pow", (1,), {"zz": 1}), ("pow", (2, 3, 4, 5), {}), ("pow", (2, 3), {"base": 1}),
        ("pow", (3, -1, 7), {}), ("pow", (2.0, 3, 5), {}), ("pow", (2, 3, None), {}),
        ("repr", ("a",), {}), ("repr", ([1],), {}),
        ("round", (2.675, 2), {}), ("round", (5,), {}), ("round", (2.5,), {}), ("round", (), {"number": 1.5}), ("round", (2.567,), {"ndigits": 2}),
        ("round", (1, 2, 3), {}), ("round", (1.5, None), {}), ("round", (1.5,), {"zz": 1}),
        ("sorted", ([3, 1, 2],), {}), ("sorted", (["bb", "a"],), {"key": lambda s: len(s)}),
        ("sorted", ([1],), {"key": None}), ("sorted", ([1], None), {}), ("sorted", (), {}), ("sorted", (1,), {"zz": 1}), ("sorted", ([1],), {"zz": 1}),
        ("sorted", ("cba",), {}), ("sorted", ({3: "x", 1: "y"},), {}),
        ("sum", ([1, 2, 3],), {}), ("sum", ([1, 2], 10), {}),
        ("sum", ([1],), {"start": 5}), ("sum", (), {"start": 5}), ("sum", ([1], 2, 3), {}),
        ("sum", ([1],), {"iterable": 2}),
    ]
    for name, args, kwargs in rows:
        label = name + repr(args) + (repr(kwargs) if kwargs else "")
        if "lambda" in label or "function" in label:
            label = name + "(<callable args>)"
        run(label, targets[name], args, kwargs)
    holder = Obj()
    run("setattr", targets["setattr"], (holder, "b", 5), {})
    print("after setattr", holder.b)
    run("setattr arity", targets["setattr"], (holder, "b"), {})
    run("delattr", targets["delattr"], (holder, "b"), {})
    print("after delattr", hasattr(holder, "b"))
    numbers = targets["iter"]([1, 2])
    run("next", targets["next"], (numbers,), {})
    run("next", targets["next"], (numbers,), {})
    run("next default", targets["next"], (numbers, "done"), {})
    run("next stop", targets["next"], (numbers,), {})
    run("next arity", targets["next"], (), {})
    tick = counter_factory()
    print("iter sentinel", list(targets["iter"](tick, 3)))
    run("iter not callable", targets["iter"], (1, 2), {})
    run("iter not iterable", targets["iter"], (1,), {})
    run("iter arity", targets["iter"], (1, 2, 3), {})
    run("id", lambda value: targets["id"](value) == id(value), (holder,), {})
    targets["print"]("p1", "p2", sep="-", end="!\n")
    targets["print"]("p3", sep=None, end=None)
    targets["print"]()
    run("print bad sep", targets["print"], ("x",), {"sep": 5})
    run("print bad kw", targets["print"], ("x",), {"zz": 5})
'''

VALUES = CASES + r'''
targets = {
    "abs": abs, "all": all, "any": any, "ascii": ascii, "bin": bin, "callable": callable,
    "chr": chr, "delattr": delattr, "divmod": divmod, "format": format, "getattr": getattr,
    "hasattr": hasattr, "hash": hash, "hex": hex, "id": id, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "max": max, "min": min, "next": next,
    "oct": oct, "ord": ord, "pow": pow, "print": print, "repr": repr, "round": round,
    "setattr": setattr, "sorted": sorted, "sum": sum,
}
cases(targets)
'''

OBJECTS = r'''
import builtins
import types
from builtins import len as imported_len


def user():
    pass


class Box:
    def method(self):
        return 1


alias = len
print(alias is len, imported_len is len, builtins.len is len, builtins.sorted is sorted)
print(repr(len), repr(print), repr(sorted))
print(type(len).__name__, type(user).__name__, type(lambda: 0).__name__, type(Box().method).__name__)
print(len.__name__, len.__qualname__, len.__module__, sorted.__name__)
print(isinstance(len, types.BuiltinFunctionType), isinstance(user, types.FunctionType),
      isinstance(5, types.FunctionType), isinstance(len, types.FunctionType),
      isinstance(Box().method, types.MethodType), isinstance(user, types.MethodType))
print(type(len) is types.BuiltinFunctionType, type(user) is types.FunctionType,
      type(Box().method) is types.MethodType)
print(callable(len), hash(len) == hash(len), len == len, len != repr)
table = {len: "len", repr: "repr"}
print(table[len], table[repr])
print(sorted(["ccc", "a", "bb"], key=len), max(["a", "bbb"], key=len))
try:
    type(len)()
except TypeError as error:
    print("TypeError", error)
try:
    user.missing
except AttributeError as error:
    print("AttributeError", error)


def apply(function, *args):
    return function(*args)


print(apply(len, "abc"), apply(divmod, 7, 3), apply(getattr, Box(), "method")())
'''

PROGRAMS = [
    pytest.param(VALUES, id="values"),
    pytest.param(OBJECTS, id="objects"),
]


@pytest.mark.integration
@pytest.mark.parametrize("program", PROGRAMS)
def test_builtin_functions_match_cpython_on_five_collectors(
    program, tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "builtin_values.py"
    source.write_text(program, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "builtin_values"
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
