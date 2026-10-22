from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import Import, ImportFrom, ClassDef


def test_abc_enum_inspect_weakref_import_shapes():
    mod = parser.parse(
        "import abc\n"
        "import enum\n"
        "import inspect\n"
        "import weakref\n"
        "from abc import ABC, abstractmethod\n"
        "from enum import Enum, auto\n",
        "stdlib_probe.py",
    )
    assert isinstance(mod.body[0], Import)
    assert isinstance(mod.body[1], Import)
    assert isinstance(mod.body[2], Import)
    assert isinstance(mod.body[3], Import)
    assert isinstance(mod.body[4], ImportFrom)
    assert mod.body[4].module == "abc"
    assert isinstance(mod.body[5], ImportFrom)
    assert mod.body[5].module == "enum"


def test_enum_class_shape_with_auto():
    mod = parser.parse(
        "from enum import Enum, auto\n"
        "class Color(Enum):\n"
        "    RED = auto()\n",
        "enum_probe.py",
    )
    assert isinstance(mod.body[1], ClassDef)
    assert mod.body[1].name == "Color"
