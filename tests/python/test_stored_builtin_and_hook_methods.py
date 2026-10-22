"""Stored builtin methods and intercepted instance calls keep Python lookup."""
from __future__ import annotations

import re
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize("shape", ("direct", "inherited", "subclass", "multiple"))
def test_attribute_hook_precedes_operand_evaluation(shape):
    method = "    def target(self, value):\n        return value\n"
    hook = ("    def __getattribute__(self, name):\n"
            "        return object.__getattribute__(self, name)\n")
    if shape == "direct":
        source = "class Receiver:\n" + method + hook
        parameter = ""
        setup = "    receiver = Receiver()\n"
    elif shape == "inherited":
        source = "class Base:\n" + method + hook + "class Receiver(Base):\n    pass\n"
        parameter = ""
        setup = "    receiver = Receiver()\n"
    else:
        if shape == "multiple":
            source = ("class Receiver:\n" + method + "class Hook:\n" + hook
                      + "class Child(Receiver, Hook):\n    pass\n")
        else:
            source = "class Receiver:\n" + method + "class Child(Receiver):\n" + hook
        parameter = "receiver: Receiver"
        setup = ""
    source += ("def operand():\n    return 3\ndef probe(" + parameter + "):\n"
               + setup + "    return receiver.target(operand())\n")
    body = _function(_emit(source))
    lookup = re.search(r"call [^\n]*@py_obj_getattr\(", body)
    operand = re.search(r"call [^\n]*@user_binding_operand\(", body)
    assert lookup is not None and operand is not None
    assert lookup.start() < operand.start()
    assert not re.search(r"call [^\n]*@user_binding_(?:Receiver|Base)_target\(", body)
    assert "@py_obj_call_slots(" in body


PROGRAMS = {
    "append": textwrap.dedent('''\
        import gc
        events = []
        class Item:
            def __init__(self, tag):
                self.tag = tag
            def __del__(self):
                events.append(self.tag)
        def make():
            gc.collect()
            return Item('retired')
        def main():
            values = []
            push = values.append
            assert push.__self__ is values
            assert push(make()) is None
            assert len(values) == 1
            alias = getattr(values, 'append')
            for i in range(48):
                gc.collect()
                assert alias(i) is None
            assert len(values) == 49
            assert values[48] == 47
            for wrong in ((), (1, 2)):
                try:
                    push(*wrong)
                except TypeError:
                    pass
                else:
                    raise AssertionError('append arity error missing')
            try:
                push(value=5)
            except TypeError:
                pass
            else:
                raise AssertionError('append keyword error missing')
            assert len(values) == 49
            del values
            del alias
            gc.collect()
            assert events == []
            assert len(push.__self__) == 49
            del push
            gc.collect()
            assert events == ['retired']
            print('METHOD_PROTOCOL_OK')
        main()
    '''),
    "hook": textwrap.dedent('''\
        import gc
        events = []
        def operand(label):
            events.append(label)
            gc.collect()
            return label
        class Base:
            def target(self, value):
                events.append('call')
                gc.collect()
                return value
            def outer(self):
                return self.target(operand('self-argument'))
        class Receiver(Base):
            def __getattribute__(self, name):
                if name == 'target':
                    events.append('lookup')
                    gc.collect()
                if name == 'broken':
                    events.append('broken-lookup')
                    raise ValueError('lookup failed')
                return object.__getattribute__(self, name)
            def broken(self, value):
                raise AssertionError('hook was bypassed')
        class Inherited(Receiver):
            pass
        class Hook:
            def __getattribute__(self, name):
                if name == 'target':
                    events.append('lookup')
                    gc.collect()
                return object.__getattribute__(self, name)
        class Mixed(Base, Hook):
            pass
        def through_base(receiver: Base):
            return receiver.target(operand('base-argument'))
        def main():
            receiver = Receiver()
            assert receiver.target(operand('argument')) == 'argument'
            assert events == ['lookup', 'argument', 'call']
            events.clear()
            child = Inherited()
            assert child.target(operand('inherited-argument')) == 'inherited-argument'
            assert events == ['lookup', 'inherited-argument', 'call']
            events.clear()
            assert through_base(receiver) == 'base-argument'
            assert events == ['lookup', 'base-argument', 'call']
            events.clear()
            assert through_base(Mixed()) == 'base-argument'
            assert events == ['lookup', 'base-argument', 'call']
            events.clear()
            assert receiver.outer() == 'self-argument'
            assert events == ['lookup', 'self-argument', 'call']
            events.clear()
            try:
                receiver.broken(operand('must not run'))
            except ValueError as error:
                assert str(error) == 'lookup failed'
            else:
                raise AssertionError('lookup error missing')
            assert events == ['broken-lookup']
            print('METHOD_PROTOCOL_OK')
        main()
    '''),
}


PROGRAMS["redirect"] = textwrap.dedent('''\
    import gc
    events = []
    def replacement(value):
        gc.collect()
        return 'redirected'
    class Receiver:
        def __getattribute__(self, name):
            if name in ('real', 'count', 'flag'):
                events.append(name)
                gc.collect()
                return replacement
            return object.__getattribute__(self, name)
        def real(self, value) -> float:
            return 0.0
        def count(self, value) -> int:
            return 0
        def flag(self, value) -> bool:
            return False
    def main():
        receiver = Receiver()
        real = receiver.real(1)
        count = receiver.count(1)
        flag = receiver.flag(1)
        assert real == count == flag == 'redirected'
        assert events == ['real', 'count', 'flag']
        assert receiver.real(1)
        assert receiver.count(1)
        assert receiver.flag(1)
        print('METHOD_PROTOCOL_OK')
    main()
''')


def test_intercepted_declared_scalar_result_stays_an_object():
    body = _function(_emit(PROGRAMS["redirect"]), "user_binding_main")
    assert "@py_obj_call_slots(" in body
    assert "@py_float_to_f64(" not in body
    assert "@py_int_to_i64_lane(" not in body
    assert "@py_obj_truthy(" in body
    assert "fcmp" not in body


@pytest.mark.parametrize("case", PROGRAMS)
def test_method_protocol_reference(case, tmp_path):
    assert_reference_program(PROGRAMS[case], 'METHOD_PROTOCOL_OK\n', tmp_path)


@pytest.mark.parametrize("case", PROGRAMS)
def test_method_protocol_owned_ir(case):
    assert "strict.nolib.stub" not in _emit(PROGRAMS[case])


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
@pytest.mark.parametrize("case", PROGRAMS)
def test_method_protocol_native_five_gc(case, python_program_compiler, request,
                                        explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[case], 'METHOD_PROTOCOL_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
