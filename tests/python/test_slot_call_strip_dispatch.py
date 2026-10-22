"""Dynamic strip calls preserve bytes, ordinary dispatch and owning slots."""
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


PROGRAM = textwrap.dedent(r'''
    import gc
    events = []
    saved = [b'  \xffname\xfe/  ']
    marker = object()

    def dynamic(value):
        return value.strip(), value.lstrip(), value.rstrip(), value.rstrip(None)

    def explicit(value, chars):
        return value.strip(chars), value.lstrip(chars), value.rstrip(chars)

    def typed_bytes(value: bytes):
        return value.strip(), value.lstrip(), value.rstrip(), value.rstrip(None)

    def typed_bytearray(value: bytearray):
        return value.strip(), value.lstrip(), value.rstrip(), value.rstrip(None)

    def chars():
        events.append('chars')
        gc.collect()
        return marker

    class Other:
        @property
        def rstrip(self):
            events.append('lookup')
            gc.collect()
            return self.apply
        def apply(self, value):
            events.append('call')
            assert value is marker
            return marker

    class Broken:
        @property
        def rstrip(self):
            events.append('broken-lookup')
            raise ValueError('lookup-error')

    def invoke(value):
        return value.rstrip(chars())

    def receiver():
        events.append('receiver')
        gc.collect()
        return Other()

    def replace_source():
        events.append('replace')
        saved[0] = b'replacement'
        gc.collect()
        return b' /'

    def preserve_receiver():
        return saved[0].strip(replace_source())

    def main():
        raw = b'  \xffname\xfe/  '
        expected = (b'\xffname\xfe/', b'\xffname\xfe/  ', b'  \xffname\xfe/', b'  \xffname\xfe/')
        for result in (dynamic(raw), typed_bytes(raw)):
            assert result == expected
            for value in result:
                assert type(value).__name__ == 'bytes'
        for result in (dynamic(bytearray(raw)), typed_bytearray(bytearray(raw))):
            assert tuple(bytes(value) for value in result) == expected
            for value in result:
                assert type(value).__name__ == 'bytearray'
        expected_chars = (b'\xffname\xfe', b'\xffname\xfe/  ', b'  \xffname\xfe')
        assert explicit(raw, b' /') == expected_chars
        assert explicit(raw, bytearray(b' /')) == expected_chars
        assert dynamic('  name/  ') == ('name/', 'name/  ', '  name/', '  name/')
        assert explicit('  name/  ', ' /') == ('name', 'name/  ', '  name')
        for value, invalid in ((raw, ' /'), (raw, 12), ('name', b'x'), ('name', 12)):
            try:
                explicit(value, invalid)
            except TypeError:
                pass
            else:
                raise AssertionError('invalid strip chars accepted')
        assert invoke(receiver()) is marker
        assert events == ['receiver', 'lookup', 'chars', 'call']
        try:
            invoke(Broken())
        except ValueError as error:
            assert str(error) == 'lookup-error'
        else:
            raise AssertionError('lookup failure swallowed')
        assert events == ['receiver', 'lookup', 'chars', 'call', 'broken-lookup']
        assert preserve_receiver() == b'\xffname\xfe'
        assert saved[0] == b'replacement'
        assert events[-1] == 'replace'
        header = b'name.o/         0           0     0     100644  0         `\n'
        assert header[:16].rstrip().rstrip(b'/').decode('utf-8', 'surrogateescape') == 'name.o'
        assert b'\xffname/ '.rstrip().rstrip(b'/').decode('utf-8', 'surrogateescape') == '\udcffname'
        print('STRIP_DISPATCH_OK')
    main()
''')


@pytest.mark.parametrize('method', ('strip', 'lstrip', 'rstrip'))
def test_strip_dynamic_routes_are_owned(method):
    text = _emit('def probe(value, chars):\n    return value.' + method + '(chars)\n')
    for family in ('str', 'bytes'):
        _assert_immediate_publication(text, 'py_' + family + '_' + method)
        _assert_immediate_publication(text, 'py_' + family + '_' + method + '_chars')
    assert 'py_obj_type_tag' in text and 'py_obj_getattr' in text
    assert 'py_obj_call_slots' in text


def test_strip_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'STRIP_DISPATCH_OK\n', tmp_path)


def test_strip_helper_native_export_and_caller_signature():
    from tests.python.test_native_file_re_result_handoff import (
        test_regex_helper_native_exports_and_callers,
    )
    test_regex_helper_native_exports_and_callers(
        '_emit_owned_strip_method', ('self', 'expr'), (False, False),
    )


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_strip_program_native(python_program_compiler, request,
                              explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'STRIP_DISPATCH_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
