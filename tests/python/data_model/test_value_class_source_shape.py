from __future__ import annotations

import pytest

from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python import type_infer
from pcc.frontends.python.types import PyFrontendError


def _infer(source: str):
    mod = parse_and_lift(source, "<valueclass-source-shape>", "value_mod")
    return type_infer.infer_module(mod)


def test_valueclass_accepts_typed_fields_and_typed_initializer():
    typed = _infer(
        "import pcc\n"
        "@pcc.valueclass\n"
        "class Point:\n"
        "    x: int\n"
        "    y: int\n"
        "    def __init__(self, x: int, y: int):\n"
        "        self.x = x\n"
        "        self.y = y\n"
        "p = Point(1, 2)\n"
    )
    assign = typed.body[-1]
    assert assign.value.ty.valueclass is True
    assert tuple(name for name, _ in assign.value.ty.fields) == ("x", "y")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "import pcc\n" "@pcc.valueclass\n" "class Bad:\n" "    x = 1\n",
            "needs an explicit type annotation",
        ),
        (
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Bad:\n"
            "    x: int\n"
            "    def __init__(self, x):\n"
            "        self.x = x\n",
            "needs a typed initializer",
        ),
        (
            "import pcc\n"
            "class Base:\n"
            "    pass\n"
            "@pcc.valueclass\n"
            "class Bad(Base):\n"
            "    x: int\n",
            "cannot subclass",
        ),
        (
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Bad:\n"
            "    __slots__ = ('__weakref__',)\n"
            "    x: int\n",
            "cannot include __weakref__",
        ),
        (
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Bad:\n"
            "    x: int\n"
            "    def __del__(self):\n"
            "        pass\n",
            "cannot define __del__",
        ),
        (
            "import pcc\n" "@pcc.valueclass\n" "class Bad:\n" "    child: 'Bad'\n",
            "recursive valueclass",
        ),
        (
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Left:\n"
            "    right: 'Right'\n"
            "@pcc.valueclass\n"
            "class Right:\n"
            "    left: Left\n",
            "recursive valueclass",
        ),
        (
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Bad:\n"
            "    children: list['Bad']\n",
            "recursive valueclass",
        ),
    ],
)
def test_valueclass_rejects_unsupported_source_shapes(source: str, message: str):
    with pytest.raises(PyFrontendError, match=message):
        _infer(source)


def test_valueclass_rejects_identity_escape_operations():
    with pytest.raises(PyFrontendError, match="identity comparison"):
        _infer(
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Point:\n"
            "    x: int\n"
            "p = Point(1)\n"
            "q = Point(1)\n"
            "same = p is q\n"
        )
    with pytest.raises(PyFrontendError, match=r"id\(\) is not supported"):
        _infer(
            "import pcc\n"
            "@pcc.valueclass\n"
            "class Point:\n"
            "    x: int\n"
            "p = Point(1)\n"
            "ident = id(p)\n"
        )


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("leak = p.__dict__", "instance dictionary"),
        ("leak = p.__weakref__", "weak reference slot"),
        ("leak = vars(p)", r"vars\(\)"),
        ("leak = getattr(p, '__dict__')", "instance dictionary"),
        ("leak = hasattr(p, '__weakref__')", "weak reference slot"),
        ("setattr(p, 'extra', 2)", "attribute mutation"),
        ("delattr(p, 'x')", "attribute mutation"),
    ],
)
def test_valueclass_rejects_instance_identity_surfaces(operation: str, message: str):
    source = (
        "import pcc\n"
        "@pcc.valueclass\n"
        "class Point:\n"
        "    x: int\n"
        "p = Point(1)\n" + operation + "\n"
    )

    with pytest.raises(PyFrontendError, match=message):
        _infer(source)


def test_valueclass_identity_diagnostics_do_not_capture_shadowed_builtin_names():
    _infer(
        "import pcc\n"
        "@pcc.valueclass\n"
        "class Point:\n"
        "    x: int\n"
        "def id(value):\n"
        "    return value\n"
        "def vars(value):\n"
        "    return value\n"
        "def getattr(value, name):\n"
        "    return value\n"
        "def hasattr(value, name):\n"
        "    return True\n"
        "def setattr(value, name, replacement):\n"
        "    return None\n"
        "def delattr(value, name):\n"
        "    return None\n"
        "p = Point(1)\n"
        "same = id(p)\n"
        "mapping = vars(p)\n"
        "field = getattr(p, '__dict__')\n"
        "present = hasattr(p, '__weakref__')\n"
        "setattr(p, 'extra', 2)\n"
        "delattr(p, 'x')\n"
    )
