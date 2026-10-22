"""Expanded known-class calls use native runtime argument binding."""

import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


DATACLASS = '''from dataclasses import dataclass
@dataclass(frozen=True)
class Row:
    kind: int
    flags: int
    size: int = 8
    register: int = 0
    base_index: int = -1
    offset: int = 0
    extent: int = 8
def construct(entry):
    return Row(*entry)
def main():
    row = construct((1, 3, 8, 6, -1, -24, 8))
    assert (row.kind, row.flags, row.size, row.register, row.base_index, row.offset, row.extent) == (1, 3, 8, 6, -1, -24, 8)
    rows = [Row(*entry) for entry in [(2, 5), (4, 7)]]
    assert rows[0].kind == 2 and rows[1].flags == 7
    assert rows[0].size == 8 and rows[0].base_index == -1
    row = Row(**{"kind": 9, "flags": 11, "offset": -16})
    assert (row.kind, row.flags, row.offset, row.extent) == (9, 11, -16, 8)
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

ORDER = '''events = []
def mark(value):
    events.append(value)
    return value
def spread():
    events.append(2)
    yield 20
    events.append(3)
class Box:
    def __init__(self, first, second, third, fourth, fifth=0):
        self.values = (first, second, third, fourth, fifth)
def main():
    value = Box(mark(1), *spread(), mark(4), *(mark(5),), fifth=mark(6))
    assert events == [1, 2, 3, 4, 5, 6]
    assert value.values == (1, 20, 4, 5, 6)
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

DUPLICATES = '''class Box:
    def __init__(self, value=7, other=8):
        self.value = value
        self.other = other
def main():
    count = 0
    try:
        Box(*(1,), **{"value": 2})
    except TypeError:
        count += 1
    try:
        Box(**{"value": 1}, **{"value": 2})
    except TypeError:
        count += 1
    try:
        Box(**{1: 2})
    except TypeError:
        count += 1
    try:
        Box(*(1, 2, 3))
    except TypeError:
        count += 1
    try:
        Box(**{"unknown": 1})
    except TypeError:
        count += 1
    assert count == 5
    assert Box(*()).value == 7
    assert Box(**{"other": 13}).other == 13
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

INHERITANCE = '''class Base:
    def __init__(self, value, other=17):
        self.value = value
        self.other = other
class Child(Base):
    pass
class Foreign:
    def __new__(cls, value):
        return value
    def __init__(self, value):
        raise AssertionError("foreign __new__ result must skip __init__")
def main():
    child = Child(*(12,), **{"other": 19})
    assert isinstance(child, Child)
    assert child.value == 12 and child.other == 19
    assert Foreign(*("foreign",)) == "foreign"
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

METACLASS = '''class Meta(type):
    def __call__(cls, value, other=23):
        return (value, other)
class Box(metaclass=Meta):
    def __init__(self):
        raise AssertionError("metaclass owns construction")
def main():
    assert Box(*(11,), **{"other": 29}) == (11, 29)
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

OWNERS = '''import gc
import weakref
references = []
finalized = []
class Token:
    def __init__(self):
        references.append(weakref.ref(self))
    def __del__(self):
        finalized.append(1)
class Box:
    def __init__(self, value, extra=None):
        self.value = value
def fail():
    gc.collect()
    assert references[0]() is not None
    raise ValueError("argument failure")
def main():
    try:
        Box(*[Token()], extra=fail())
    except ValueError as error:
        assert str(error) == "argument failure"
    else:
        raise AssertionError("argument exception was swallowed")
    gc.collect()
    assert references[0]() is None
    assert finalized == [1]
    box = Box(*[Token()])
    gc.collect()
    assert references[1]() is box.value
    del box
    gc.collect()
    assert references[1]() is None
    assert finalized == [1, 1]
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

CAPTURES = '''def main():
    captured = 3
    class Box:
        def __init__(self, value):
            self.value = value
        def read(self):
            return self.value + captured
    box = Box(*(7,))
    captured = 5
    assert box.read() == 12
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

MAPPING_ORDER = '''events = []
def mark(number, value):
    events.append(number)
    return value
class Box:
    def __init__(self, first, second=0, third=0):
        self.values = (first, second, third)
def main():
    box = Box(**mark(1, {"first": 11}), second=mark(2, 22), **mark(3, {"third": 33}))
    assert events == [1, 2, 3]
    assert box.values == (11, 22, 33)
    events.clear()
    try:
        Box(**mark(1, {"first": 11}), first=mark(2, 22), **mark(3, {"third": 33}))
    except TypeError:
        pass
    else:
        raise AssertionError("duplicate keyword was accepted")
    assert events == [1, 2]
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

CLASSMETHOD = '''class Required:
    def __init__(self, first, second, third):
        self.values = (first, second, third)
    @classmethod
    def make(cls, values):
        return cls(*values)
class RequiredChild(Required):
    pass
class Defaulted:
    def __init__(self, first, second=17):
        self.values = (first, second)
    @classmethod
    def make(cls, values, options):
        return cls(*values, **options)
class DefaultedChild(Defaulted):
    pass
def main():
    first = RequiredChild.make((1, 2, 3))
    assert isinstance(first, RequiredChild)
    assert first.values == (1, 2, 3)
    second = DefaultedChild.make((5,), {})
    assert isinstance(second, DefaultedChild)
    assert second.values == (5, 17)
    third = DefaultedChild.make((5,), {"second": 19})
    assert third.values == (5, 19)
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

CLASSMETHOD_META = '''class Meta(type):
    def __call__(cls, value):
        return ("meta", value)
class Base:
    @classmethod
    def make(cls, values):
        return cls(*values)
class Child(Base, metaclass=Meta):
    pass
def main():
    assert Child.make((7,)) == ("meta", 7)
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

MAPPING_OWNERS = '''import gc
import weakref
references = []
finalized = []
class Token:
    def __del__(self):
        finalized.append(1)
class Box:
    def __init__(self, value, extra=None):
        self.value = value
def mapping():
    token = Token()
    references.append(weakref.ref(token))
    return {"value": token}
def fail():
    gc.collect()
    assert references[0]() is not None
    raise ValueError("keyword failure")
def main():
    try:
        Box(**mapping(), extra=fail())
    except ValueError as error:
        assert str(error) == "keyword failure"
    else:
        raise AssertionError("keyword exception was swallowed")
    gc.collect()
    assert references[0]() is None and finalized == [1]
    box = Box(**mapping())
    gc.collect()
    assert references[1]() is box.value
    del box
    gc.collect()
    assert references[1]() is None and finalized == [1, 1]
    try:
        Box(**mapping(), **{"value": None})
    except TypeError:
        pass
    else:
        raise AssertionError("mapping collision was accepted")
    gc.collect()
    assert references[2]() is None and finalized == [1, 1, 1]
    print("CONSTRUCTOR_UNPACK_OK")
main()
'''

PROGRAMS = {
    "dataclass": DATACLASS,
    "order": ORDER,
    "duplicates": DUPLICATES,
    "inheritance": INHERITANCE,
    "metaclass": METACLASS,
    "owners": OWNERS,
    "captures": CAPTURES,
    "mapping_order": MAPPING_ORDER,
    "classmethod": CLASSMETHOD,
    "classmethod_meta": CLASSMETHOD_META,
    "mapping_owners": MAPPING_OWNERS,
}


@pytest.mark.parametrize("name", list(PROGRAMS))
def test_constructor_unpack_reference(name):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAMS[name], {})
    assert output.getvalue() == "CONSTRUCTOR_UNPACK_OK\n"


@pytest.mark.parametrize("name", list(PROGRAMS))
def test_constructor_unpack_owned_ir(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("constructor_" + name + ".py")
    output = source.with_suffix(".ll")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on", target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    assert "ctor.unpack.call" in text
    assert "name '*' is not defined" not in text
    assert "name '**' is not defined" not in text
    assert "ctor.unpack.args" in text and "ctor.unpack.result" in text
    assert re.search(r"call i64[^\n]*@py_obj_call_slots\(", text)
    if name == "captures":
        # Box's methods are closures: the class statement publishes function
        # objects that carry the enclosing captures (ac063182a).
        assert "method.captures.new" in text
    artifact = emit_owned_object(text, "x86_64-unknown-linux-gnu")
    assert artifact[:4] == b"\x7fELF"


@pytest.mark.integration
@pytest.mark.parametrize("name", list(PROGRAMS))
def test_constructor_unpack_native(tmp_path, pcc_runtime_archive, name):
    source = tmp_path / ("constructor_" + name + ".py")
    output = source.with_suffix("")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == "CONSTRUCTOR_UNPACK_OK\n"
        assert result.stderr == ""
