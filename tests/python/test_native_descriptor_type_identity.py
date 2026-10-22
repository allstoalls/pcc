"""Native descriptor classes retain canonical identity and factory ownership."""
import re

import pytest

from pcc.frontends.python.pipeline import compile_python
from ir_pointer_aliases import function_bodies
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


PROGRAM = r'''
import gc
from builtins import staticmethod as Static, classmethod as Class, property as Property

def access(value):
    gc.collect()
    return value

def assigned_static(value):
    constructor = staticmethod
    return constructor(value)

def assigned_class(value):
    constructor = classmethod
    return constructor(value)

def assigned_property(value):
    constructor = property
    return constructor(value)

def check():
    assert type(assigned_static(access)) is staticmethod
    assert type(assigned_class(access)) is classmethod
    assert type(assigned_property(access)) is property
    assert Static is staticmethod
    assert Class is classmethod
    assert Property is property
    a = staticmethod(access)
    b = classmethod(access)
    c = property(access)
    assert type(a) is staticmethod
    assert type(b) is classmethod
    assert type(c) is property
    assert type(a) is not type(b)
    assert type(b) is not type(c)
    gc.collect()
    for descriptor, kind in [(a, Static), (b, Class), (c, Property)]:
        assert isinstance(descriptor, kind)
        assert isinstance(descriptor, (str, kind))
        assert not isinstance(42, kind)
        made = type(descriptor)(access)
        assert type(made) is kind
        assert made is not descriptor
        gc.collect()
        class Holder:
            value = made
        obj = Holder()
        if kind is Static:
            assert obj.value('x') == 'x'
            assert Holder.value('y') == 'y'
            assert made('z') == 'z'
        elif kind is Class:
            assert obj.value() is Holder
            assert Holder.value() is Holder
        else:
            assert obj.value is obj
            assert Holder.value is made
    for kind in [Static, Class]:
        try:
            kind()
        except TypeError:
            pass
        else:
            raise AssertionError('missing descriptor argument')
        try:
            kind(access, access)
        except TypeError:
            pass
        else:
            raise AssertionError('extra descriptor argument')
        try:
            kind(function=access)
        except TypeError:
            pass
        else:
            raise AssertionError('descriptor keyword argument')
    empty = Property()
    assert type(empty) is property
    def read_value(receiver):
        gc.collect()
        return receiver.saved
    def write_value(receiver, value):
        gc.collect()
        receiver.saved = value
    def delete_value(receiver):
        gc.collect()
        receiver.saved = 'deleted'
    full = Property(read_value, write_value, delete_value)
    class Managed:
        value = full
    managed = Managed()
    gc.collect()
    managed.value = 'set'
    assert managed.value == 'set'
    del managed.value
    assert managed.saved == 'deleted'
    keyword = Property(fget=access)
    class Keyword:
        value = keyword
    obj = Keyword()
    assert obj.value is obj
    try:
        Property(access, fget=access)
    except TypeError:
        pass
    else:
        raise AssertionError('duplicate property accessor')
    try:
        Property(unknown=access)
    except TypeError:
        pass
    else:
        raise AssertionError('unknown property accessor')
    gc.collect()
    print('DESCRIPTOR_IDENTITY_OK')

def lifetime():
    def make(kind, expected):
        captured = [expected]
        def getter(value):
            gc.collect()
            assert captured[0] == expected
            return value
        return kind(getter)
    for kind, expected in [(Static, 'static'), (Class, 'class'), (Property, 'property')]:
        descriptor = make(kind, expected)
        gc.collect()
        assert type(descriptor) is kind
        class Receiver:
            value = descriptor
        instance = Receiver()
        del descriptor
        gc.collect()
        if kind is Static:
            assert Receiver.value(expected) == expected
            assert instance.value(expected) == expected
        elif kind is Class:
            assert Receiver.value() is Receiver
            assert instance.value() is Receiver
        else:
            assert instance.value is instance
    def failing(value):
        gc.collect()
        raise ValueError('wrapped failure')
    for kind in [Static, Class, Property]:
        descriptor = kind(failing)
        class Failed:
            value = descriptor
        instance = Failed()
        try:
            if kind is Static:
                instance.value(None)
            elif kind is Class:
                instance.value()
            else:
                instance.value
        except ValueError as error:
            assert str(error) == 'wrapped failure'
        else:
            raise AssertionError('wrapped error was lost')
    def shadow(staticmethod):
        return staticmethod
    assert shadow(123) == 123
    print('DESCRIPTOR_LIFETIME_OK')

check()
lifetime()
'''

CONSTRUCTOR_RESULTS = r'''import gc

def accessor(value):
    return value

def take(*, value, **extras):
    gc.collect()
    return value

def static_case():
    class Holder:
        value = staticmethod(accessor)
    def make():
        return staticmethod(accessor)
    def defaulted(value=staticmethod(accessor)):
        return value
    values = (take(value=staticmethod(accessor)), [staticmethod(accessor)][0], make(), defaulted())
    assert Holder.value('holder') == 'holder'
    for descriptor in values:
        gc.collect()
        assert type(descriptor).__name__ == 'staticmethod'
        class Receiver:
            value = descriptor
        instance = Receiver()
        assert instance.value('instance') == 'instance'
        assert Receiver.value('class') == 'class'

def class_case():
    class Holder:
        value = classmethod(accessor)
    def make():
        return classmethod(accessor)
    def defaulted(value=classmethod(accessor)):
        return value
    values = (take(value=classmethod(accessor)), [classmethod(accessor)][0], make(), defaulted())
    assert Holder.value() is Holder
    for descriptor in values:
        gc.collect()
        assert type(descriptor).__name__ == 'classmethod'
        class Receiver:
            value = descriptor
        instance = Receiver()
        assert instance.value() is Receiver
        assert Receiver.value() is Receiver

def property_case():
    class Holder:
        value = property(accessor)
    def make():
        return property(accessor)
    def defaulted(value=property(accessor)):
        return value
    values = (take(value=property(accessor)), [property(accessor)][0], make(), defaulted())
    holder = Holder()
    assert holder.value is holder
    for descriptor in values:
        gc.collect()
        assert type(descriptor).__name__ == 'property'
        class Receiver:
            value = descriptor
        instance = Receiver()
        assert instance.value is instance
        assert Receiver.value is descriptor

static_case()
class_case()
property_case()
gc.collect()
print('DESCRIPTOR_CONSTRUCTOR_RESULTS_OK')
'''


def test_descriptor_type_values_emit_native_factory_calls(tmp_path):
    source = tmp_path / "descriptor_identity.py"
    output = tmp_path / "descriptor_identity.ll"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    text = output.read_text()
    assert not re.search(r"\bcall[^\n]*@py_cpy_", text)
    assert "strict.nolib.stub" not in text
    for tag in (101, 102, 103):
        assert re.search(r"\bcall[^\n]*@py_builtin_type_for_tag\(i64 " + str(tag) + r"\)", text)


@pytest.mark.parametrize("name", ("staticmethod", "classmethod", "property"))
def test_descriptor_global_shadowing_preserves_actual_module_value(tmp_path, name):
    source = tmp_path / "descriptor_shadow.py"
    output = tmp_path / "descriptor_shadow.ll"
    source.write_text(name + " = 123\ndef descriptor_shadow():\n    return " + name + "\n")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    body = next(body for symbol, body in function_bodies(output.read_text())
                if symbol.endswith("_descriptor_shadow"))
    assert "@py_builtin_type_for_tag(" not in body
    assert ".modvar.descriptor_shadow." + name in body


@pytest.mark.parametrize("name,tag", (("staticmethod", 103), ("classmethod", 102), ("property", 101)))
def test_descriptor_assignment_alias_materializes_canonical_type(tmp_path, name, tag):
    source = tmp_path / "descriptor_alias.py"
    output = tmp_path / "descriptor_alias.ll"
    source.write_text("def assigned_descriptor():\n    constructor = " + name + "\n    return constructor\n")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", emit_llvm_only=True)
    body = next(body for symbol, body in function_bodies(output.read_text())
                if symbol.endswith("_assigned_descriptor"))
    assert "@py_module_attr_get(" not in body
    assert "@py_builtin_type_for_tag(i64 " + str(tag) + ")" in body


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_descriptor_identity_and_lifetime_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "DESCRIPTOR_IDENTITY_OK\nDESCRIPTOR_LIFETIME_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_descriptor_constructor_results_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        CONSTRUCTOR_RESULTS, "DESCRIPTOR_CONSTRUCTOR_RESULTS_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )


SHADOWING_PROGRAM = r'''
import gc
staticmethod = 'static shadow'
classmethod = 'class shadow'
property = 'property shadow'

def read_shadow():
    return staticmethod, classmethod, property

def main():
    gc.collect()
    assert read_shadow() == ('static shadow', 'class shadow', 'property shadow')
    print('DESCRIPTOR_GLOBAL_SHADOW_OK')
main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_descriptor_global_values_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        SHADOWING_PROGRAM, "DESCRIPTOR_GLOBAL_SHADOW_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
