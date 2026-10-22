"""A method's general decorators apply when its class is created.

class_gen lowered only staticmethod, classmethod, the property family and a
few no-op decorators on a method; any other decorator -- @unittest.skip(...),
@support.cpython_only, a functools.wraps wrapper -- stopped compilation with
"Layer 1 does not handle decorator", which kept most CPython Lib/test modules
from compiling. py_lift now splits such a method into its undecorated
function (a class-body temporary) and the assignment m = dec(m), so the
decorators run in source order on the function object at class creation and
every access goes through the bound name. Compared with CPython on five
collectors; the split itself is checked on the lifted AST.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pcc.frontends.python import py_ast as pa
from pcc.frontends.python.method_decorators import (
    HIDDEN_METHOD_PREFIX,
    native_method_decorator,
    split_general_method_decorators,
)


PROGRAM = r'''
import functools
import unittest

registry = []


def register(func):
    registry.append(func.__name__)
    return func


def wrap(tag):
    def decorator(func):
        @functools.wraps(func)
        def inner(self, *args):
            return (tag, func(self, *args))
        return inner
    return decorator


def constant(func):
    return 42


class Callable:
    def __init__(self, func):
        self.func = func

    def __call__(self, *args):
        return ("called", len(args))


class Base:
    @register
    def plain(self, x):
        return x * 10

    @wrap("outer")
    @wrap("inner")
    def stacked(self, x):
        return x + 1

    @constant
    def answer(self):
        return 0

    @Callable
    def obj(self):
        return 0

    def use(self):
        return self.plain(1), self.stacked(2), self.answer, self.obj(1, 2)


class Child(Base):
    @wrap("child")
    def plain(self, x):
        return ("super", super().plain(x))


class Tests(unittest.TestCase):
    @unittest.skip("not today")
    def test_skipped(self):
        raise AssertionError("never")

    @unittest.skipIf(False, "kept")
    def test_kept(self):
        return "kept ran"


b = Base()
print(registry)
print(b.use())
print(Base.plain(b, 3), getattr(b, "stacked")(4))
print(Base.plain.__name__, hasattr(Base, "__pcc_decorated__plain"))
c = Child()
print(c.plain(5), c.use()[0])
t = Tests("test_skipped")
print(getattr(Tests.test_skipped, "__unittest_skip__", False), getattr(Tests.test_skipped, "__unittest_skip_why__", ""))
try:
    t.test_skipped()
except unittest.SkipTest as error:
    print("SkipTest", error)
print(Tests("test_kept").test_kept())
'''


def _class_body(source):
    from pcc.frontends.python.py_lift import parse_and_lift
    module = parse_and_lift(source, "split.py", "split")
    for stmt in module.body:
        if isinstance(stmt, pa.ClassDef):
            return stmt.body
    raise AssertionError("no class")


def test_general_decorators_become_a_class_body_assignment():
    body = _class_body(
        "import functools\n"
        "def deco(f):\n    return f\n"
        "class C:\n"
        "    @deco\n"
        "    @functools.wraps(len)\n"
        "    def m(self):\n        return 1\n"
        "    @staticmethod\n"
        "    def s():\n        return 2\n"
        "    @staticmethod\n"
        "    @deco\n"
        "    def mixed():\n        return 3\n"
        "    @deco\n"
        "    def __eq__(self, other):\n        return True\n"
    )
    names = [stmt.name if isinstance(stmt, pa.FuncDef) else type(stmt).__name__ for stmt in body]
    assert names == [HIDDEN_METHOD_PREFIX + "m", "Assign", "s", "mixed", "__eq__"]
    hidden, assign = body[0], body[1]
    assert hidden.decorators == ()
    assert [target.ident for target in assign.targets] == ["m"]
    outer = assign.value
    assert isinstance(outer, pa.Call) and outer.func.ident == "deco"
    inner = outer.args[0]
    assert isinstance(inner, pa.Call) and isinstance(inner.func, pa.Call)
    assert inner.args[0].ident == HIDDEN_METHOD_PREFIX + "m"
    # Native kinds, mixed lists and dunder methods keep their decorators.
    assert [len(stmt.decorators) for stmt in body[2:]] == [1, 2, 1]


@pytest.mark.parametrize("name,native", (
    ("staticmethod", True), ("classmethod", True), ("property", True),
    ("functools.cached_property", True), ("abc.abstractmethod", True),
    ("functools.lru_cache", True), ("value.setter", True), ("np.errstate", True),
    ("unittest.skip", False), ("support.cpython_only", False), ("functools.wraps", False),
    ("register", False),
))
def test_native_method_decorators_match_class_gen(name, native):
    if "." in name:
        owner, attr = name.split(".", 1)
        dec = pa.Attr(None, pa.DynType("dyn"), pa.Name(None, pa.DynType("dyn"), owner), attr)
    else:
        dec = pa.Name(None, pa.DynType("dyn"), name)
    assert native_method_decorator(dec) is native
    assert native_method_decorator(pa.Call(None, pa.DynType("dyn"), dec, ())) is native


@pytest.mark.integration
def test_decorated_methods_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "method_decorators.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "method_decorators"
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
