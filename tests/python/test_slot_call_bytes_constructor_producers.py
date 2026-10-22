"""Bytes-family constructor owners publish before operand cleanup."""
from __future__ import annotations

import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize('expression,runtime', (
    ('bytes()', 'py_bytes_new'), ('bytearray()', 'py_bytearray_from_obj'),
    ('bytes(item)', 'py_bytes_from_obj'), ('bytearray(item)', 'py_bytearray_from_obj'),
    ('memoryview(item)', 'py_memoryview_new'), ('bytes(3)', 'py_bytes_from_obj'),
    ("bytes('text', 'utf-8')", 'py_str_utf8_encode'),
    ("bytearray('text', 'latin-1')", 'py_str_latin1_encode'),
))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'attribute', 'later-error'))
def test_buffer_constructor_immediate_publication(expression, runtime, site):
    prefix = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'attribute':
        body = '    class Holder:\n        value = ' + expression + '\n    return Holder\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    _assert_immediate_publication(text, runtime)
    if expression.startswith('bytearray(') and "'text'" in expression:
        _assert_immediate_publication(text, 'py_bytearray_from_obj')


def test_bytes_alias_runtime_retains_separate_result_owner():
    from tests.python.test_buffer_factory_ownership import BufferModel

    model = BufferModel('bytes', relocate=True, initial_refs=2)
    # Original and caller-source slots hold independent aliases before the
    # runtime acquires its own source owner and publishes a third result owner.
    model.frames[model.external] = 3
    model.store(model.external, 16, model.source)
    result = model.run()
    assert result[0] is model.source[0] and result[0].refs == 3
    model.clear_root(model.external, None)
    model.clear_root(model.external + 16, None)
    result = model.load(model.external, 8)
    assert result[0].refs == 1
    model.clear_root(model.external + 8, None)
    assert all(value.refs == 0 for value in model.objects)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Indexed:
        def __init__(self, value):
            self.value = value
        def __index__(self):
            gc.collect()
            return self.value
    class BadIndex:
        def __index__(self):
            gc.collect()
            raise ValueError('index-error')
    def source():
        events.append('source')
        return bytearray([65, 66])
    def later():
        gc.collect()
        events.append('later')
        return None
    def take(*, value, other=None, **extras):
        gc.collect()
        return value
    def returned(value):
        return bytes(value)
    def main():
        original = bytes([65, 66])
        alias = take(value=bytes(original), other=later())
        assert alias is original
        del original
        gc.collect()
        assert alias == b'AB'
        copied = take(value=bytearray(alias))
        assert bytes(copied) == alias and copied is not alias
        viewed = take(value=memoryview(source()), other=later())
        gc.collect()
        assert bytes(viewed) == b'AB'
        assert bytes(memoryview(memoryview(bytearray([65, 66])))) == b'AB'
        assert bytes([Indexed(65), Indexed(66)]) == b'AB'
        try:
            bytes([Indexed(65), BadIndex()])
        except ValueError as error:
            assert str(error) == 'index-error'
        else:
            raise AssertionError('index callback error was lost')
        try:
            bytearray([1 << 100])
        except ValueError:
            pass
        else:
            raise AssertionError('arbitrary-precision byte overflow was lost')
        assert events == ['later', 'source', 'later']
        def default(value=bytes(copied)):
            gc.collect()
            return value
        class Holder:
            value = bytearray('text', 'utf-8')
        gc.collect()
        assert default() == b'AB'
        assert bytes(Holder.value) == b'text'
        assert returned(copied) == b'AB'
        assert bytes() == b'' and bytearray() == bytearray(b'')
        assert bytes(3) == b'\\x00\\x00\\x00'
        assert bytes('text', 'utf-8') == b'text'
        try:
            take(value=bytes(-1), other=later())
        except ValueError:
            pass
        else:
            raise AssertionError('negative byte count accepted')
        assert events == ['later', 'source', 'later']
        print('BUFFER_OWNERSHIP_OK')
    main()
''')


def test_buffer_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'BUFFER_OWNERSHIP_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_buffer_native_five_gc(python_program_compiler, request,
                               explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'BUFFER_OWNERSHIP_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
