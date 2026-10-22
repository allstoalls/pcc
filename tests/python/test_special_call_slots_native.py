"""End-to-end special-call binding; requires a centrally matched runtime."""
import contextlib
import io
import os
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


PROGRAM = '''import gc
events = []
class NormalMeta(type):
    def __call__(cls, value, *, extra=0):
        gc.collect()
        events.append(("normal", cls.__name__, value, extra))
        return value + extra
class Normal(metaclass=NormalMeta):
    pass
class StaticMeta(type):
    @staticmethod
    def __call__(value):
        events.append(("static", value))
        return value + 1
class Static(metaclass=StaticMeta):
    pass
class ClassMeta(type):
    @classmethod
    def __call__(meta, value):
        events.append(("class", meta.__name__, value))
        return value + 2
class ClassBound(metaclass=ClassMeta):
    pass
class ChildMeta(ClassMeta):
    pass
class ChildBound(ClassBound, metaclass=ChildMeta):
    pass
class BoundCall:
    def __init__(self, cls, owner):
        self.cls = cls
        self.owner = owner
    def __call__(self, value):
        events.append(("descriptor", self.cls.__name__, self.owner.__name__, value))
        return value + 3
class CallDescriptor:
    def __get__(self, cls, owner):
        gc.collect()
        return BoundCall(cls, owner)
class DescriptorMeta(type):
    __call__ = CallDescriptor()
class DescriptorBound(metaclass=DescriptorMeta):
    pass
class InheritedDescriptorMeta(DescriptorMeta):
    pass
class InheritedBound(metaclass=InheritedDescriptorMeta):
    pass
class RaisingMeta(type):
    def __call__(cls, value):
        raise ValueError("selected failure")
class Raising(metaclass=RaisingMeta):
    pass
class Plain:
    def __init__(self, value):
        self.value = value
def main():
    assert Normal(*(10,), **{"extra": 5}) == 15
    assert Static(*(20,)) == 21
    assert ClassBound(*(30,)) == 32
    assert ChildBound(*(40,)) == 42
    assert DescriptorBound(*(50,)) == 53
    assert InheritedBound(*(60,)) == 63
    assert Plain(*(70,)).value == 70
    assert Normal(100, extra=5) == 105
    assert Static(110) == 111
    assert ClassBound(120) == 122
    assert ChildBound(130) == 132
    assert DescriptorBound(140) == 143
    assert InheritedBound(150) == 153
    caught = False
    try:
        Raising(*(80,))
    except ValueError as error:
        caught = str(error) == "selected failure"
    assert caught
    assert events == [
        ("normal", "Normal", 10, 5), ("static", 20),
        ("class", "ClassMeta", 30), ("class", "ChildMeta", 40),
        ("descriptor", "DescriptorBound", "DescriptorMeta", 50),
        ("descriptor", "InheritedBound", "InheritedDescriptorMeta", 60),
        ("normal", "Normal", 100, 5), ("static", 110),
        ("class", "ClassMeta", 120), ("class", "ChildMeta", 130),
        ("descriptor", "DescriptorBound", "DescriptorMeta", 140),
        ("descriptor", "InheritedBound", "InheritedDescriptorMeta", 150),
    ]
    print("SPECIAL_CALL_SLOTS_OK")
main()
'''


def test_special_call_slots_reference():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == "SPECIAL_CALL_SLOTS_OK\n"


@pytest.mark.integration
def test_special_call_slots_native_all_collectors(tmp_path, pcc_runtime_archive):
    source = tmp_path / "special_call_slots.py"
    output = source.with_suffix("")
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(gc),
                           PCC_GC_MINOR_ALLOC_MAX="4096")
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == "SPECIAL_CALL_SLOTS_OK\n"
        assert result.stderr == ""
