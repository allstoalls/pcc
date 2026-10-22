"""Actual scalar boxing and integer object conversion publish owning operands."""
from __future__ import annotations
import re
import textwrap
import pytest
from tests.python.test_slot_call_operand_roots import _emit
from tests.python.test_shared_call_binding import _emit as _emit_binding
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


def _body(text, name):
    body = re.search(r'^define [^\n]*@user_[^\n(]*_' + name + r'\([^\n]*\).*?^}', text, re.M | re.S)
    assert body is not None
    return body.group(0)


def _published(text, helper):
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@' + helper + r'\([^\n]*\)\n', text, re.M))
    assert calls, helper
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith('store ptr ' + call.group(1) + ','), following


@pytest.mark.parametrize('expression,helper', (
    ('len(values)', 'py_int_from_i64'), ('bool(values)', 'py_bool_from_bit'),
    ('int(text)', 'py_obj_as_int_object_args'), ('int(text, base)', 'py_obj_as_int_object_args'),
    ('len(values) == 1', 'py_bool_from_bit'),
    ('(len(values) == 1) and (len(values) < 2)', 'py_bool_from_bit'),
))
def test_builtin_call_operand_publishes_actual_object_producer(expression, helper):
    text = _emit('def probe(values, text, base):\n    return slot_operand_probe(' + expression + ')\n')
    body = _body(text, 'probe')
    _published(body, helper)
    if expression.startswith('int('):
        assert '@py_int_to_i64(' not in body
        assert '@py_cpy_' not in body


@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
@pytest.mark.parametrize('producer', ('len(values)', 'bool(values)', 'int(text)'))
def test_boxed_call_producer_context_matrix(site, producer):
    prelude = ('def take(*, value, later=None, **extras):\n    return value\n'
               'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def target(value=' + producer + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + producer + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + producer + ', later=fail())\n'
    else:
        body = '    return take(value=' + producer + ')\n'
    text = _emit_binding(prelude + 'def probe(values, text):\n' + body)
    assert 'define' in text
    if producer == 'int(text)' and site != 'return':
        _published(_body(text, 'probe'), 'py_obj_as_int_object_args')


@pytest.mark.parametrize('scope', ('parameter', 'function', 'global'))
def test_shadowed_int_keeps_callable_binding(scope):
    prefix = 'def convert(value):\n    return {"value": value}\n'
    parameter = ''
    if scope == 'parameter':
        parameter = 'int'
    elif scope == 'function':
        prefix += 'def int(value):\n    return convert(value)\n'
    else:
        prefix += 'int = convert\n'
    text = _emit(prefix + 'def probe(' + parameter + '):\n    return slot_operand_probe(int("input"))\n')
    assert '@py_obj_as_int_object_args(' not in _body(text, 'probe')


PROGRAM = textwrap.dedent('''\
    import gc
    from builtins import int as convert
    events = []
    class BadLength:
        def __len__(self):
            gc.collect()
            raise ValueError('length')
    def mark():
        events.append('later')
        return None
    def failing_length(value):
        return take(value=len(value), later=mark())
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def later():
        gc.collect()
        raise ValueError('later')
    def local(int):
        return take(value=int('shadow'))
    def shadow(value):
        return {'value': value}
    def main():
        text = '1234567890123456789012345678901234567890'
        large = 1234567890123456789012345678901234567890
        assert take(value=convert(text)) == large
        assert take(value=int(text)) == large
        assert take(value=int(large)) is large
        assert take(value=int('abcdef0123456789abcdef', 16)) == 207698809136909011942886895
        assert take(value=int(1e20)) == 100000000000000000000
        assert take(value=int(True)) == 1
        values = [1, 2, 3]
        assert take(value=len(values)) == 3
        assert take(value=bool(values)) is True
        assert take(value=bool([])) is False
        assert local(shadow) == {'value': 'shadow'}
        def default(value=int(text)):
            gc.collect()
            return value
        assert default() == large
        try:
            take(value=int(text), later=later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing later exception')
        try:
            take(value=int('bad'))
        except ValueError:
            pass
        else:
            raise AssertionError('missing conversion exception')
        try:
            take(value=int('1', None))
        except TypeError:
            pass
        else:
            raise AssertionError('explicit None base treated as omitted')
        try:
            failing_length(BadLength())
        except ValueError:
            pass
        else:
            raise AssertionError('missing length exception')
        assert events == []
        print('BOXED_CALL_PRODUCERS_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_boxed_call_producers_native_five_gc(python_program_compiler, request,
                                            explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'BOXED_CALL_PRODUCERS_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''
