"""Explicit inherited object allocation uses the current class MRO."""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
from pcc.frontends.python.pipeline import compile_python
from pcc.ir.optimization.driver import optimize_ir


INHERITED = '''events = []
class Base:
    pass
class Other:
    pass
class Child(Base):
    sentinel = object()
    original = sentinel
    def __init__(self):
        events.append("init")
    @classmethod
    def make(cls, value=sentinel):
        instance = Base.__new__(cls)
        instance.value = value
        return instance
    sentinel = object()
def main():
    first = Child.make()
    second = Child.__new__(Child)
    allocator = Base.__new__
    third = allocator(Child)
    assert isinstance(first, Child)
    assert isinstance(second, Child)
    assert isinstance(third, Child)
    assert first is not second
    assert first.value is Child.original
    assert events == []
    assert Base.__new__ is Base.__new__
    assert Base.__new__ is Child.__new__
    assert Base.__new__ is Other.__new__
    try:
        allocator(1)
    except TypeError:
        pass
    else:
        raise AssertionError("non-class allocator argument accepted")
    print("INHERITED_NEW_OK")
main()
'''

OVERRIDES = '''events = []
marker = object()
class Custom:
    def __new__(cls, token):
        events.append("custom")
        return token
class Inherited(Custom):
    pass
class Replaced:
    pass
class Descendant(Replaced):
    pass
def replacement(cls):
    events.append("replacement")
    return marker
def main():
    original = Replaced.__new__
    constructor = getattr(Inherited, "__new__")
    assert constructor(Inherited, marker) is marker
    assert getattr(Custom, "__new__") is constructor
    Replaced.__new__ = staticmethod(replacement)
    assert Descendant.__new__(Descendant) is marker
    assert getattr(Descendant, "__new__") is replacement
    Replaced.__new__ = None
    try:
        Descendant.__new__(Descendant)
    except TypeError:
        pass
    else:
        raise AssertionError("None shadow ignored")
    del Replaced.__new__
    assert Descendant.__new__ is original
    assert events == ["custom", "replacement"]
    print("NEW_OVERRIDE_OK")
main()
'''

ARGUMENTS = '''class Base:
    pass
class WithInit(Base):
    def __init__(self, value):
        raise AssertionError("explicit allocation must not call init")
class WithNew(Base):
    def __new__(cls, value):
        return value
def main():
    allocate = Base.__new__
    assert isinstance(allocate(WithInit, 7), WithInit)
    assert isinstance(allocate(WithInit, value=7), WithInit)
    for mode in range(4):
        try:
            if mode == 0:
                allocate()
            elif mode == 1:
                allocate(cls=Base)
            elif mode == 2:
                allocate(Base, 7)
            else:
                allocate(WithNew, 7)
        except TypeError:
            pass
        else:
            raise AssertionError("invalid explicit allocation accepted")
    print("NEW_ARGUMENTS_OK")
main()
'''

METACLASS = '''marker = object()
def replacement(cls):
    return marker
class Meta(type):
    pass
class Sample(metaclass=Meta):
    pass
class NonData:
    def __get__(self, instance, owner):
        return replacement
class Data:
    def __get__(self, instance, owner):
        return replacement
    def __set__(self, instance, value):
        raise AssertionError("unexpected descriptor store")
def main():
    original = Sample.__new__
    Meta.__new__ = NonData()
    assert Sample.__new__ is original
    Meta.__new__ = Data()
    assert Sample.__new__(Sample) is marker
    print("NEW_METACLASS_OK")
main()
'''

THREADS = '''import threading
class Base:
    pass
class Other:
    pass
start = threading.Event()
results = []
def worker():
    start.wait()
    results.append(Base.__new__)
    results.append(Other.__new__)
def main():
    threads = []
    for index in range(4):
        thread = threading.Thread(target=worker)
        threads.append(thread)
        thread.start()
    start.set()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    canonical = results[0]
    for value in results:
        assert value is canonical
    assert canonical is Base.__new__
    print("NEW_THREAD_IDENTITY_OK")
main()
'''

PROGRAMS = ((INHERITED, "INHERITED_NEW_OK\n"),
            (OVERRIDES, "NEW_OVERRIDE_OK\n"),
            (ARGUMENTS, "NEW_ARGUMENTS_OK\n"),
            (METACLASS, "NEW_METACLASS_OK\n"),
            (THREADS, "NEW_THREAD_IDENTITY_OK\n"))
PROGRAM_IDS = ("classmethod-default", "overrides", "argument-validation",
               "metaclass-descriptors", "thread-identity")


@pytest.mark.parametrize("source_text,expected", PROGRAMS,
                         ids=PROGRAM_IDS)
def test_inherited_new_reference(source_text, expected):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(source_text, {})
    assert output.getvalue() == expected


@pytest.mark.parametrize("source_text,expected", PROGRAMS,
                         ids=PROGRAM_IDS)
def test_inherited_new_program_reaches_owned_emitter(tmp_path, monkeypatch,
                                                   source_text, expected):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "inherited_new.py"
    ir_path = tmp_path / "inherited_new.ll"
    source.write_text(source_text)
    compile_python(str(source), str(ir_path), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = ir_path.read_text()
    assert "@user_inherited_new_main(" in text
    assert "@py_obj_load_method" in text
    object_bytes = emit_owned_object(text, "arm64-apple-darwin23.6.0")
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"


def test_object_allocator_runtime_reaches_owned_emitter(tmp_path):
    runtime = Path(__file__).absolute().parents[2] / "pcc" / "runtime"
    output = tmp_path / "py_class.ll"
    target = "arm64-apple-darwin23.6.0"
    _compile_runtime_module("py_class", str(runtime / "py" / "py_class.py"),
                            str(output), target)
    text = output.read_text()
    assert '@pcc_object_new_cache_mutex_bits = global i64 0' in text
    definition = re.search(r'^define[^\n]*@user_py_class__object_new_entry\([^\n]*', text, re.M)
    assert definition is not None
    assert re.search(r'ptr @user_py_class__object_new_entry\(ptr [^,]+, ptr [^)]*\)',
                     definition[0]) is not None, definition[0]
    callback_calls = [line for line in text.splitlines()
                      if '@py_func_new_bound(' in line and '_object_new_entry' in line]
    assert callback_calls, 'object allocator must publish its native two-pointer entry'
    callback_values = [line for line in text.splitlines()
                       if 'extern._object_new_entry.fnptr' in line]
    assert any('@user_py_class__object_new_entry ' in line
               or '@user_py_class__object_new_entry)' in line
               for line in callback_values), callback_values
    object_bytes = emit_owned_object(optimize_ir(text, runtime_ir_passes(str(runtime))),
                                     target)
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
@pytest.mark.parametrize("source_text,expected", PROGRAMS,
                         ids=PROGRAM_IDS)
def test_native_inherited_new(tmp_path, pcc_runtime_archive,
                              python_program_compiler, source_text, expected):
    source = tmp_path / "inherited_new.py"
    binary = tmp_path / "inherited_new"
    source.write_text(source_text)
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, (backend, result.stdout, result.stderr)
