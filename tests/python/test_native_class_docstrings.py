"""Class documentation belongs to each actual namespace, including no-doc None."""
from __future__ import annotations

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


PROGRAM = '''import gc
events = []
class Documented:
    """source documentation"""
    captured = __doc__
class Plain:
    pass
class Child(Documented):
    pass
class OwnDoc(Documented):
    """child documentation"""
class Empty:
    """"""
class Late:
    value = 1
    """not a class docstring"""
class Explicit:
    __doc__ = 23
class Replaced:
    """initial documentation"""
    __doc__ = "replacement"
class Meta(type):
    @classmethod
    def __prepare__(mcls, name, bases):
        return {}
    def __new__(mcls, name, bases, namespace):
        events.append((name, "__doc__" in namespace, namespace.get("__doc__")))
        if name == "PreparedChanged":
            namespace["__doc__"] = "changed by metaclass"
        if name == "PreparedRemoved":
            del namespace["__doc__"]
        return type.__new__(mcls, name, bases, namespace)
class Prepared(metaclass=Meta):
    """prepared documentation"""
    captured = __doc__
class PreparedChild(Prepared):
    pass
class PreparedChanged(metaclass=Meta):
    """source before replacement"""
    captured = __doc__
class PreparedRemoved(metaclass=Meta):
    """source before deletion"""
class PlainMeta(type):
    def __new__(mcls, name, bases, namespace):
        namespace["__doc__"] = "changed without prepare"
        return type.__new__(mcls, name, bases, namespace)
class PlainChanged(metaclass=PlainMeta):
    """original plain meta documentation"""
class NoPrepareMeta(type):
    def __new__(mcls, name, bases, namespace):
        return type.__new__(mcls, name, bases, namespace)
class MethodDocumentation(metaclass=NoPrepareMeta):
    def __doc__(self):
        return "callable documentation"
def local_class():
    class Local:
        """local documentation"""
    return Local
def main():
    assert Documented.__doc__ == "source documentation"
    assert Documented.captured == Documented.__doc__
    assert Documented().__doc__ == "source documentation"
    assert Plain.__doc__ is None
    assert Plain.__dict__["__doc__"] is None
    assert Child.__doc__ is None
    assert Child().__doc__ is None
    assert Child.__dict__["__doc__"] is None
    assert OwnDoc.__doc__ == "child documentation"
    assert Empty.__doc__ == ""
    assert Late.__doc__ is None
    assert Explicit.__doc__ == 23
    assert Replaced.__doc__ == "replacement"
    assert local_class().__doc__ == "local documentation"
    assert Prepared.__doc__ == "prepared documentation"
    assert Prepared.captured == "prepared documentation"
    assert PreparedChild.__doc__ is None
    assert PreparedChanged.__doc__ == "changed by metaclass"
    assert PreparedChanged.__dict__["__doc__"] == "changed by metaclass"
    assert PreparedChanged.captured == "source before replacement"
    assert PreparedRemoved.__doc__ is None
    assert PreparedRemoved.__dict__["__doc__"] is None
    assert PlainChanged.__doc__ == "changed without prepare"
    assert callable(MethodDocumentation.__dict__["__doc__"])
    assert MethodDocumentation().__doc__() == "callable documentation"
    assert events == [("Prepared", True, "prepared documentation"), ("PreparedChild", False, None), ("PreparedChanged", True, "source before replacement"), ("PreparedRemoved", True, "source before deletion")]
    PreparedChanged.__doc__ = "updated prepared documentation"
    assert PreparedChanged.__doc__ == "updated prepared documentation"
    dynamic = type("Dynamic", (Documented,), {})
    assert dynamic.__doc__ is None
    assert dynamic.__dict__["__doc__"] is None
    dynamic_doc = type("DynamicDoc", (), {"__doc__": "dynamic documentation"})
    assert dynamic_doc.__doc__ == "dynamic documentation"
    Documented.__doc__ = "updated documentation"
    gc.collect()
    assert Documented.__doc__ == "updated documentation"
    assert Documented().__doc__ == "updated documentation"
    assert Child.__doc__ is None
    print("CLASS_DOCSTRINGS_OK")
main()
'''


def test_class_docstrings_reference(tmp_path):
    assert_reference_program(PROGRAM, "CLASS_DOCSTRINGS_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_class_docstrings_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "CLASS_DOCSTRINGS_OK\n", tmp_path, python_program_compiler,
        mode, explicit_owned_runtime, capfd,
    )
