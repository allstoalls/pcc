"""getattr owns every operand/result and preserves only-AttributeError defaults."""
from __future__ import annotations

import re
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit as _emit_unchecked, _function
from tests.owned_ir_validation import verify_ir_text
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


def _emit(source):
    text = _emit_unchecked(source)
    verify_ir_text(text)
    return text


@pytest.mark.parametrize('site', ('assignment', 'return', 'argument', 'default', 'discarded', 'later-error'))
@pytest.mark.parametrize('name', ("'value'", 'name'))
@pytest.mark.parametrize('defaulted', (False, True))
def test_getattr_result_reaches_owned_context(site, name, defaulted):
    expression = 'getattr(receiver, ' + name + (', fallback)' if defaulted else ')')
    body = {
        'assignment': '    value = ' + expression + '\n    return value\n',
        'return': '    return ' + expression + '\n',
        'argument': '    return take(value=' + expression + ')\n',
        'default': '    def selected(value=' + expression + '):\n        return value\n    return selected()\n',
        'discarded': '    ' + expression + '\n',
        'later-error': '    return take(value=' + expression + ', later=fail())\n',
    }[site]
    text = _emit('def take(*, value, later=None, **extras):\n    return value\n'
                 'def fail():\n    raise ValueError("later")\n'
                 'def probe(receiver, name, fallback):\n' + body)
    _assert_immediate_publication(text, 'py_obj_getattr')
    assert 'strict.nolib.stub' not in text
    assert '@py_obj_getattr_maybe(' not in _function(text)
    assert '@py_tls_exc_swap_slot(' in _function(text)
    assert 'getattr.invalid_result' in _function(text)
    if defaulted:
        assert '@py_exc_matches(' in _function(text)
        assert 'getattr.propagate' in _function(text)
    if name == 'name':
        assert 'getattr.name.lease' in _function(text)
        assert 'getattr.name.release' in _function(text)


def test_getattr_dynamic_name_validation_follows_all_argument_evaluation():
    body = _function(_emit('''
class Name:
    def __str__(self):
        raise AssertionError('name coerced')
def receiver():
    return object()
def name():
    return Name()
def default():
    return []
def probe():
    return getattr(receiver(), name(), default())
'''))
    calls = [re.search(r'call [^\n]*@user_binding_' + symbol + r'\(', body) for symbol in ('receiver', 'name', 'default')]
    assert all(calls)
    assert calls[0].start() < calls[1].start() < calls[2].start()
    assert calls[2].start() < body.index('@py_obj_type_tag(')
    assert body.index('@py_obj_type_tag(') < body.index('@py_str_utf8(')


@pytest.mark.parametrize('defaulted', (False, True))
def test_getattr_null_without_exception_has_a_separate_contract_error(defaulted):
    suffix = ', fallback)' if defaulted else ')'
    body = _function(_emit('def probe(receiver, fallback):\n'
                           + "    return getattr(receiver, 'value'" + suffix + '\n'))
    assert '@py_obj_getattr(' in body
    assert '@py_obj_getattr_maybe(' not in body
    present = re.search(r'^getattr\.present[^:]*:\n(.*?)(?=^[^ \n][^\n]*:|^})', body, re.M | re.S)
    invalid = re.search(r'^getattr\.invalid_result[^:]*:\n(.*?)(?=^[^ \n][^\n]*:|^})', body, re.M | re.S)
    assert present and invalid
    assert 'icmp eq ptr ' in present.group(1) and ', null' in present.group(1)
    assert 'label %getattr.invalid_result' in present.group(1)
    assert '@py_exc_new(i64 7,' in invalid.group(1)  # RuntimeError, not a miss/default.
    assert '@py_raise(' in invalid.group(1)
    assert 'label %call.slot.cleanup' in invalid.group(1)
    if defaulted:
        propagate = re.search(r'^getattr\.propagate[^:]*:\n(.*?)(?=^[^ \n][^\n]*:|^})', body, re.M | re.S)
        assert propagate and '@py_tls_exc_swap_slot(' in propagate.group(1)
        assert 'label %call.slot.cleanup' in propagate.group(1)
        assert '@py_exc_matches(' in body


PROGRAMS = {}
exception_prelude = '''import gc
class CustomMissing(AttributeError):
    pass
class Receiver:
    def __getattribute__(self, name):
        gc.collect()
        if name == 'missing':
            raise AttributeError('missing')
        if name == 'missing_subclass':
            raise CustomMissing('missing')
        if name == 'value':
            return None
        raise ValueError('lookup failed')
def dynamic_name():
    return ''.join(('er', 'ror'))
def take(*, value, later=None, **extras):
    return value
def later():
    raise AssertionError('later operand evaluated')
'''
functions = []
for arity in (2, 3):
    expression = "getattr(Receiver(), dynamic_name()" + (", object())" if arity == 3 else ')')
    sites = {
        'assignment': 'value = ' + expression,
        'return': 'return ' + expression,
        'argument': 'take(value=' + expression + ')',
        'default': 'def selected(value=' + expression + '):\n    return value',
        'discarded': expression,
        'later': 'take(value=' + expression + ', later=later())',
    }
    for site, statement in sites.items():
        name = 'probe_' + str(arity) + '_' + site
        functions.append('def ' + name + '():\n    try:\n'
                         + textwrap.indent(statement, '        ') + '\n'
                         + "    except ValueError as error:\n        assert str(error) == 'lookup failed'\n        return 'caught'\n"
                         + "    else:\n        raise AssertionError('lookup exception lost')\n")
PROGRAMS['exceptions'] = exception_prelude + '\n'.join(functions) + '''
def main():
''' + ''.join('    assert probe_' + str(arity) + '_' + site + "() == 'caught'\n" for arity in (2, 3) for site in ('assignment', 'return', 'argument', 'default', 'discarded', 'later')) + '''    fallback = object()
    assert getattr(Receiver(), 'missing', fallback) is fallback
    assert getattr(Receiver(), 'missing', None) is None
    assert getattr(Receiver(), 'missing_subclass', fallback) is fallback
    assert getattr(Receiver(), 'value', fallback) is None
    try:
        getattr(Receiver(), 'missing')
    except AttributeError:
        pass
    else:
        raise AssertionError('two-argument miss suppressed')
    print('GETATTR_EXCEPTIONS_OK')
main()
'''

PROGRAMS['ownership'] = '''import gc
retired = []
class Token:
    def __init__(self, tag):
        self.tag = tag
    def __del__(self):
        retired.append(self.tag)
        gc.collect()
        try:
            raise RuntimeError('cleanup-local error')
        except RuntimeError:
            pass
class Receiver:
    def __getattribute__(self, name):
        gc.collect()
        if name == 'value':
            return Token('value')
        if name == 'alias':
            return self
        if name == 'missing':
            raise AttributeError(name)
        raise ValueError('lookup failed')
    def __del__(self):
        retired.append('receiver')
        gc.collect()
def name():
    gc.collect()
    return ''.join(('val', 'ue'))
def take(*, value, later=None, **extras):
    gc.collect()
    return value
def fail():
    gc.collect()
    raise ValueError('later failed')
def returned():
    return getattr(Receiver(), name())
def main():
    value = getattr(Receiver(), name(), Token('unused'))
    gc.collect()
    assert retired.count('receiver') == 1
    assert retired.count('unused') == 1
    assert retired.count('value') == 0
    value = None
    gc.collect()
    assert retired.count('value') == 1
    value = returned()
    assert value.tag == 'value'
    value = None
    gc.collect()
    assert retired.count('value') == 2
    value = take(value=getattr(Receiver(), name()))
    assert value.tag == 'value'
    value = None
    gc.collect()
    assert retired.count('value') == 3
    getattr(Receiver(), name())
    gc.collect()
    assert retired.count('value') == 4
    try:
        take(value=getattr(Receiver(), name()), later=fail())
    except ValueError as error:
        assert str(error) == 'later failed'
    else:
        raise AssertionError('later exception lost')
    gc.collect()
    assert retired.count('value') == 5
    def selected(value=getattr(Receiver(), name())):
        return value
    gc.collect()
    assert retired.count('value') == 5
    held = selected()
    assert held.tag == 'value'
    held = None
    selected = None
    gc.collect()
    assert retired.count('value') == 6
    try:
        getattr(Receiver(), 'error', Token('failed-default'))
    except ValueError as error:
        assert str(error) == 'lookup failed'
    else:
        raise AssertionError('lookup exception lost during cleanup')
    gc.collect()
    assert retired.count('failed-default') == 1
    fallback = Token('fallback')
    value = getattr(Receiver(), 'missing', fallback)
    assert value is fallback
    value = None
    fallback = None
    gc.collect()
    assert retired.count('fallback') == 1
    receiver = Receiver()
    assert getattr(receiver, 'alias', receiver) is receiver
    receiver = None
    gc.collect()
    print('GETATTR_OWNERSHIP_OK')
main()
'''

PROGRAMS['validation-order'] = '''import gc
events = []
class Receiver:
    def __getattribute__(self, name):
        events.append('lookup')
        return None
class InvalidName:
    def __str__(self):
        events.append('coercion')
        return 'value'
def receiver():
    events.append('receiver')
    return Receiver()
def name():
    events.append('name')
    return InvalidName()
def default():
    events.append('default')
    gc.collect()
    return object()
def failed_default():
    events.append('default')
    raise ValueError('default failed')
def main():
    try:
        getattr(receiver(), name(), default())
    except TypeError:
        pass
    else:
        raise AssertionError('invalid name accepted')
    assert events == ['receiver', 'name', 'default']
    events.clear()
    try:
        getattr(receiver(), name(), failed_default())
    except ValueError as error:
        assert str(error) == 'default failed'
    else:
        raise AssertionError('name validation ran before default')
    assert events == ['receiver', 'name', 'default']
    events.clear()
    try:
        getattr(receiver(), name())
    except TypeError:
        pass
    else:
        raise AssertionError('invalid two-argument name accepted')
    assert events == ['receiver', 'name']
    print('GETATTR_VALIDATION_ORDER_OK')
main()
'''

EXPECTED = {
    'exceptions': 'GETATTR_EXCEPTIONS_OK\n',
    'ownership': 'GETATTR_OWNERSHIP_OK\n',
    'validation-order': 'GETATTR_VALIDATION_ORDER_OK\n',
}


@pytest.mark.parametrize('case', tuple(PROGRAMS))
def test_getattr_program_reference(case, tmp_path):
    assert_reference_program(PROGRAMS[case], EXPECTED[case], tmp_path)


@pytest.mark.parametrize('case', tuple(PROGRAMS))
def test_getattr_program_compiles_without_stub(case):
    assert 'strict.nolib.stub' not in _emit(PROGRAMS[case])


@pytest.mark.integration
@pytest.mark.parametrize('case', tuple(PROGRAMS))
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_getattr_owners_native_five_gc(
    case, python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[case], EXPECTED[case], tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
