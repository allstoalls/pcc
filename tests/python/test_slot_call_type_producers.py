"""One-argument native type consumes a leased value and returns a NEW owner."""
from __future__ import annotations

import re
import textwrap

import pytest

from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error', 'attribute'))
def test_native_type_result_publishes_before_operand_cleanup(site):
    prefix = ('from pcc.unsafe import null\n'
              'def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def target(value=type(item)):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return type(item)\n'
    elif site == 'attribute':
        body = '    return take(value=type(item).__name__)\n'
    elif site == 'later-error':
        body = '    return take(value=type(item), later=fail())\n'
    else:
        body = '    return take(value=type(item))\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    _assert_immediate_publication(text, 'py_type_builtin')
    assert 'call.slot.argument.lease' in text
    if site == 'default':
        publication = re.search(r'(%[^ ]+) = call [^\n]*@py_type_builtin\([^\n]*\)\n'
                                r'  store ptr \1, ptr (%[^ ,\n]+)', text)
        assert publication is not None
        tail = text[publication.end():]
        assert 'func.sig.kind' in tail
        assert '@pcc_gc_take_pinned_slot(' not in tail[:tail.index('func.sig.kind')]


PROGRAM = textwrap.dedent('''\
    import gc
    disposed = []
    class Tracked:
        def __del__(self):
            disposed.append('tracked')
            gc.collect()
    class Spoof:
        @property
        def __class__(self):
            raise AssertionError('type used a __class__ property')
        def __getattribute__(self, name):
            raise AssertionError('type used an attribute override')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def returned(value):
        return type(value)
    def later():
        gc.collect()
        raise ValueError('later-error')
    def main():
        assert take(value=type(Tracked())) is Tracked
        gc.collect()
        assert disposed == ['tracked']
        item = Tracked()
        assert returned(item) is Tracked
        assert type(item).__name__ == 'Tracked'
        assert take(value=type(Spoof())) is Spoof
        def target(value=type(Tracked())):
            gc.collect()
            return value
        assert target() is Tracked
        gc.collect()
        assert disposed == ['tracked', 'tracked']
        try:
            take(value=type(Tracked()), later=later())
        except ValueError as error:
            assert str(error) == 'later-error'
        else:
            raise AssertionError('missing later error')
        gc.collect()
        assert disposed == ['tracked', 'tracked', 'tracked']
        assert take(value=type(1)) is int
        assert take(value=type(True)) is bool
        assert take(value=type('x')) is str
        large = 1234567890123456789012345678901234567890
        assert take(value=type(large)) is int
        print('TYPE_PRODUCER_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_native_type_five_gc(python_program_compiler, request,
                            explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'TYPE_PRODUCER_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
