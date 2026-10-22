"""Native str subclasses preserve user identity and traced builtin payloads."""
from __future__ import annotations

import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit

PROGRAMS = {
    'subclass_protocols': (textwrap.dedent("""\
        import gc
        import copy

        events = []
        class Text(str):
            def __del__(self):
                events.append('drop:' + str(self))
        class Derived(Text):
            pass
        class Custom(str):
            def __new__(cls, value):
                events.append('new')
                gc.collect()
                return str.__new__(cls, value.upper())
            def __init__(self, value):
                self.note = value
                events.append('init')
            def __str__(self):
                gc.collect()
                return 'custom'
            def __format__(self, spec):
                gc.collect()
                return Text(spec)
        class Bad:
            def __str__(self):
                gc.collect()
                raise ValueError('conversion')
        class Broken:
            def __format__(self, spec):
                raise ValueError('format')
        class Invalid:
            def __format__(self, spec):
                return 1

        def main():
            ordinary = 'literal'
            assert str(ordinary) is ordinary
            assert type(ordinary) is str
            text = Text('payload-' + str(23))
            assert type(text) is Text
            assert isinstance(text, str)
            assert str(text) == 'payload-23'
            assert type(str(text)) is str
            text.note = ['owner']
            assert text.__dict__ == {'note': ['owner']}
            child = Derived('derived')
            assert type(child) is Derived
            assert isinstance(child, Text)
            assert isinstance(child, str)
            gc.collect()
            assert str(child) == 'derived'
            shallow = copy.copy(text)
            deep = copy.deepcopy(text)
            assert type(shallow) is Text and type(deep) is Text
            assert shallow is not text and deep is not text
            assert str(shallow) == 'payload-23' and str(deep) == 'payload-23'
            assert shallow.note is text.note
            assert deep.note is not text.note
            assert deep.note == text.note
            custom = Custom('abc')
            assert custom.note == 'abc'
            assert str(custom) == 'custom'
            assert events == ['new', 'init']
            single = '{0:q}'.format(custom)
            assert type(single) is Text
            assert str(single) == 'q'
            assert events == ['new', 'init']
            del single
            gc.collect()
            assert events == ['new', 'init', 'drop:q']
            try:
                Text(Bad())
            except ValueError as error:
                assert str(error) == 'conversion'
            else:
                raise AssertionError('conversion exception lost')
            try:
                '{0:x}'.format(Broken())
            except ValueError as error:
                assert str(error) == 'format'
            else:
                raise AssertionError('format exception lost')
            try:
                '{0:x}'.format(Invalid())
            except TypeError:
                pass
            else:
                raise AssertionError('non-string format result accepted')
            for index in range(8):
                moving = Text('dynamic-' + str(index) + ('x' * 257))
                gc.collect()
                assert str(moving) == 'dynamic-' + str(index) + ('x' * 257)
                del moving
            print('STR_SUBCLASS_PROTOCOLS_OK')
        main()
    """), 'STR_SUBCLASS_PROTOCOLS_OK\n'),
    'constructor_protocols': (textwrap.dedent("""\
        import gc

        events = []
        class Base(str):
            def __new__(cls, value):
                events.append('new:' + value)
                gc.collect()
                return str.__new__(cls, value)
            def __init__(self, value):
                events.append('init:' + value)
                self.original = value
        class Child(Base):
            pass
        sentinel = []
        class Foreign(str):
            def __new__(cls, value):
                return sentinel
            def __init__(self, value):
                raise AssertionError('foreign result reached init')
        class Plain(str):
            pass

        def main():
            normal = Base('a')
            assert type(normal) is Base and str(normal) == 'a'
            assert normal.original == 'a'
            assert events == ['new:a', 'init:a']
            direct = str.__new__(Base, 'b')
            assert type(direct) is Base and str(direct) == 'b'
            assert events == ['new:a', 'init:a']
            allocator = str.__new__
            alias = allocator(Base, 'c')
            assert type(alias) is Base and str(alias) == 'c'
            assert events == ['new:a', 'init:a']
            child = Child('d')
            assert type(child) is Child and str(child) == 'd'
            assert child.original == 'd'
            assert events == ['new:a', 'init:a', 'new:d', 'init:d']
            foreign = Foreign('ignored')
            assert foreign is sentinel
            assert events == ['new:a', 'init:a', 'new:d', 'init:d']
            assert str(Plain(object='keyword')) == 'keyword'
            assert str(Plain(b'bytes', encoding='utf-8')) == 'bytes'
            assert str(Plain(b'names', encoding=Plain('utf-8'), errors=Plain('strict'))) == 'names'
            assert str(Plain(encoding='utf-8')) == ''
            assert str(Plain(errors='strict')) == ''
            try:
                Plain('one', object='two')
            except TypeError:
                pass
            else:
                raise AssertionError('duplicate argument accepted')
            try:
                allocator(list, 'wrong class')
            except TypeError:
                pass
            else:
                raise AssertionError('non-string class accepted')
            gc.collect()
            assert str(normal) == 'a' and str(direct) == 'b' and str(alias) == 'c'
            print('STR_SUBCLASS_CONSTRUCTORS_OK')
        main()
    """), 'STR_SUBCLASS_CONSTRUCTORS_OK\n'),
    'finalizer_protocols': (textwrap.dedent("""\
        import gc
        import weakref

        saved = []
        events = []
        class Resurrected(str):
            def __del__(self):
                events.append(str(self))
                saved.append(self)
        class Slotted(str):
            __slots__ = ()
            def __del__(self):
                events.append(str(self))

        def main():
            value = Resurrected('payload-' + str(9))
            identity = id(value)
            reference = weakref.ref(value)
            del value
            gc.collect()
            assert events == ['payload-9']
            rescued = saved.pop()
            assert type(rescued) is Resurrected
            assert id(rescued) == identity
            assert reference() is rescued
            assert str(rescued) == 'payload-9'
            del rescued
            gc.collect()
            assert reference() is None
            assert events == ['payload-9']
            slot = Slotted('slot-' + str(2))
            gc.collect()
            assert type(slot) is Slotted and str(slot) == 'slot-2'
            try:
                slot.note = 'forbidden'
            except AttributeError:
                pass
            else:
                raise AssertionError('slots restriction lost')
            del slot
            gc.collect()
            assert events == ['payload-9', 'slot-2']
            print('STR_SUBCLASS_FINALIZERS_OK')
        main()
    """), 'STR_SUBCLASS_FINALIZERS_OK\n'),
    'view_protocols': (textwrap.dedent("""\
        import gc

        events = []
        drops = []
        class Plain(str):
            pass
        class Temporary(str):
            def __del__(self):
                drops.append(str(self))
        class Source:
            def __str__(self):
                gc.collect()
                return Temporary('converted')
        class Alias(str):
            def __format__(self, spec):
                events.append(spec)
                gc.collect()
                return self

        def main():
            converted = Plain(Source())
            assert str(converted) == 'converted'
            assert drops == ['converted']
            value = Alias('identity-' + str(7))
            same = '{0:x}'.format(value)
            assert same is value
            assert format(value, 'y') is value
            joined = '{0:a}|{0:b}'.format(value)
            assert type(joined) is str
            assert joined == 'identity-7|identity-7'
            assert events == ['x', 'y', 'a', 'b']
            identity = id(value)
            del value
            gc.collect()
            assert id(same) == identity
            assert str(same) == 'identity-7'
            for index in range(12):
                data = ('x' * 257) + str(index)
                item = Plain(data)
                gc.collect()
                assert str(item) == data
                assert type(str(item)) is str
                assert repr(item) == repr(data)
                padded = format(item, '>300')
                assert padded == (' ' * (300 - len(data))) + data
                assert type(padded) is str
                # Allocation and explicit collection bracket each view consumer;
                # small nursery thresholds are supplied by the diagnostic runner.
                debris = [str(number) + ('q' * 128) for number in range(20)]
                gc.collect()
                assert str(item) == data
            print('STR_SUBCLASS_VIEWS_OK')
        main()
    """), 'STR_SUBCLASS_VIEWS_OK\n'),
}

@pytest.mark.parametrize('shape', tuple(PROGRAMS))
def test_native_str_subclass_reference(shape, tmp_path):
    program, expected = PROGRAMS[shape]
    assert_reference_program(program, expected, tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('shape', tuple(PROGRAMS))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_native_str_subclass_five_gc(shape, python_program_compiler, request,
                                     explicit_owned_runtime, tmp_path, capfd):
    program, expected = PROGRAMS[shape]
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(program, expected, tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd)


def test_str_new_keeps_requested_class_in_owned_call():
    text = _emit('def make(cls, value):\n    return str.__new__(cls, value)\n')
    assert '@py_obj_getattr(' in text
    assert '@py_obj_call_slots(' in text
    assert '@py_obj_str(' not in text.split('define ', 1)[1]


def test_str_isinstance_uses_subtype_predicate():
    text = _emit('def check(value):\n    return isinstance(value, str)\n')
    assert '@py_str_check(' in text
