"""Known class attributes consume the live lexical class receiver."""

import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from tests.python.test_hoist_source_callable_bindings import CLASS_METHOD_VALUES, CLASS_VALUES


CLASSMETHOD = '''def outer():
    class C:
        @classmethod
        def identity(cls):
            return cls
    method = C.identity
    assert method() is C
    return C, method
def main():
    first, first_method = outer()
    second, second_method = outer()
    assert first is not second
    assert first_method() is first
    assert second_method() is second
    print("LOCAL_CLASSMETHOD_RECEIVER_OK")
main()
'''

METACLASS_PROPERTY = '''def outer():
    class Meta(type):
        @property
        def receiver(cls):
            return cls
    class C(metaclass=Meta):
        pass
    assert C.receiver is C
    return C
def main():
    first = outer()
    second = outer()
    assert first is not second
    assert first.receiver is first
    assert second.receiver is second
    print("LOCAL_METACLASS_PROPERTY_RECEIVER_OK")
main()
'''

METACLASS_MUTATION = '''events = []
def outer():
    events.clear()
    class Meta(type):
        @property
        def receiver(cls):
            return cls
        @receiver.setter
        def receiver(cls, value):
            events.append((cls, value))
        @receiver.deleter
        def receiver(cls):
            events.append((cls, None))
    class C(metaclass=Meta):
        pass
    C.receiver = 42
    del C.receiver
    assert events[0][0] is C and events[0][1] == 42
    assert events[1][0] is C and events[1][1] is None
    return C
def main():
    first = outer()
    second = outer()
    assert first is not second
    print("LOCAL_METACLASS_MUTATION_RECEIVER_OK")
main()
'''

METACLASS_DESCRIPTOR = '''class Descriptor:
    def __get__(self, receiver, owner):
        return receiver, owner
    def __set__(self, receiver, value):
        pass
def outer():
    class Meta(type):
        receiver = Descriptor()
    class C(metaclass=Meta):
        pass
    receiver, owner = C.receiver
    assert receiver is C and owner is Meta
    return C
def main():
    first = outer()
    second = outer()
    assert first is not second
    print("LOCAL_METACLASS_DESCRIPTOR_OWNER_OK")
main()
'''

CONDITIONAL = '''def outer(enabled):
    if enabled:
        class C:
            value = 42
    try:
        value = C.value
    except UnboundLocalError:
        assert not enabled
        return 0
    assert enabled
    del C
    try:
        C.value
    except UnboundLocalError:
        return value
    raise AssertionError("deleted local class remained readable")
def main():
    assert outer(False) == 0
    assert outer(True) == 42
    print("LOCAL_CLASS_RECEIVER_BOUNDNESS_OK")
main()
'''

PROGRAMS = {
    "class_values": (CLASS_VALUES, "HOIST_CLASS_FUNCTION_IDENTITY_OK\n"),
    "class_method_values": (CLASS_METHOD_VALUES, "HOIST_CLASS_METHOD_FUNCTION_IDENTITY_OK\n"),
    "classmethod": (CLASSMETHOD, "LOCAL_CLASSMETHOD_RECEIVER_OK\n"),
    "metaclass_property": (METACLASS_PROPERTY, "LOCAL_METACLASS_PROPERTY_RECEIVER_OK\n"),
    "metaclass_mutation": (METACLASS_MUTATION, "LOCAL_METACLASS_MUTATION_RECEIVER_OK\n"),
    "metaclass_descriptor": (METACLASS_DESCRIPTOR, "LOCAL_METACLASS_DESCRIPTOR_OWNER_OK\n"),
    "conditional": (CONDITIONAL, "LOCAL_CLASS_RECEIVER_BOUNDNESS_OK\n"),
}


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<local-class-receiver>", "local_class_receiver"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    return codegen, text


def _outer_text(text):
    return next(body for body in re.findall(r"define [^\n]+\n.*?^\}", text, re.M | re.S)
                if "@user_local_class_receiver_outer(" in body.splitlines()[0])


@pytest.mark.parametrize("name", PROGRAMS)
def test_local_class_receiver_reads_lexical_slot_instead_of_unpublished_global(name):
    _codegen, text = _generate(PROGRAMS[name][0])
    outer = _outer_text(text)
    global_reads = re.findall(r"load ptr, ptr @\.class\.local_class_receiver\.C(?:\s|$)", outer)
    assert not global_reads
    assert "C.class.addr" in outer
    if name == "conditional":
        # A receiver that may be unbound is guarded; a class statement that
        # binds on every path before the read needs no guard (439d8274e).
        assert "C.bound.error" in outer
    if name == "metaclass_descriptor":
        meta_global_reads = re.findall(r"load ptr, ptr @\.class\.local_class_receiver\.Meta(?:\s|$)", outer)
        assert not meta_global_reads


@pytest.mark.parametrize("name", PROGRAMS)
def test_local_class_receiver_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_local_class_receiver_shapes_reach_owned_objects(name, target):
    _codegen, text = _generate(PROGRAMS[name][0])
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_local_class_receivers_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
    source, expected = PROGRAMS[name]
    path = tmp_path / (name + ".py")
    path.write_text(source)
    binary = tmp_path / name
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == expected and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
