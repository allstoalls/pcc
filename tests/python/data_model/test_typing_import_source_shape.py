from __future__ import annotations

from pcc.frontends.python import parser
from pcc.frontends.python.py_ast import ImportFrom, ClassDef, Assign


def test_typing_protocol_source_shape_for_no_libpython_import():
    mod = parser.parse(
        "from typing import Protocol, TypeVar, Generic, Optional\n"
        "T = TypeVar('T')\n"
        "class P(Protocol):\n"
        "    pass\n"
        "class Box(Generic[T]):\n"
        "    value: Optional[int]\n",
        "typing_probe.py",
    )
    assert isinstance(mod.body[0], ImportFrom)
    assert mod.body[0].module == "typing"
    assert isinstance(mod.body[1], Assign)
    assert isinstance(mod.body[2], ClassDef)
    assert isinstance(mod.body[3], ClassDef)


def test_types_module_import_source_shape():
    mod = parser.parse(
        "from types import SimpleNamespace, ModuleType\n"
        "x = SimpleNamespace(a=1)\n",
        "types_probe.py",
    )
    assert isinstance(mod.body[0], ImportFrom)
    assert mod.body[0].module == "types"
    assert isinstance(mod.body[1], Assign)
