"""Execute the dict/list bound-method runtime bodies with relocation and failures."""
from __future__ import annotations

import pytest

from test_percent_format_owners import ABI, Handle
from test_stored_list_append_owners import BoundListMemory


KEYS = 2
GET = 1
REVERSE = 26


class BoundMethodMemory(BoundListMemory):
    CONSTANT_PREFIX = '_BOUND_METHOD_'
    FUNCTION_PREFIXES = ('_bound_method_', '_py_bound_method')
    OWNED_SLOTS = 8

    def load(self, base, offset=0):
        # The entry reads its operation code straight from the rooted tuple.
        if isinstance(base, Handle):
            return self.current(self.object(base)['children'][(offset - 24) // 8])
        return super().load(base, offset)

    def environment(self):
        env = super().environment()
        def reverse(receiver):
            self.park()
            if self.failure == 'reverse':
                self.pending = 'reverse-error'
                return
            self.object(receiver)['children'].reverse()
        def keys(receiver):
            self.park()
            if self.failure == 'keys':
                self.pending = 'keys-error'
                return None
            return self.new(ABI['PY_TYPE_LIST'], sorted(self.object(receiver)['data']),
                            'keys', pending=True)
        env.update(py_int_value_i64=lambda value: self.object(value)['data'],
                   unsigned_div_i64=lambda left, right: left // right,
                   _cstr_append=lambda buffer, at, text: at,
                   py_list_reverse=reverse, py_dict_keys=keys)
        return env


def _call(model, receiver, operation, *arguments):
    code = model.new(ABI['PY_TYPE_INT'], operation, 'code')
    captures = model.new(ABI['PY_TYPE_TUPLE'], None, children=[receiver, code])
    args = model.new(ABI['PY_TYPE_TUPLE'], None, children=list(arguments))
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    return model.env['_py_bound_method_entry'](captures, args), baseline


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'reverse'))
def test_void_method_returns_none_and_releases_every_owner(relocate, failure):
    model = BoundMethodMemory(relocate=relocate, failure=failure)
    first = model.new(ABI['PY_TYPE_INSTANCE'], 'first')
    second = model.new(ABI['PY_TYPE_INSTANCE'], 'second')
    receiver = model.new(ABI['PY_TYPE_LIST'], None, children=[first, second])
    result, baseline = _call(model, receiver, REVERSE)
    if failure is None:
        assert result.identity == model.globals['py_None'].identity
        assert model.objects[receiver.identity]['children'] == [second.identity, first.identity]
        assert model.pending is None
    else:
        assert result is None and model.pending == 'reverse-error'
    model.check(result, baseline)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'keys'))
def test_new_result_is_published_through_the_result_slot(relocate, failure):
    model = BoundMethodMemory(relocate=relocate, failure=failure)
    receiver = model.new(ABI['PY_TYPE_DICT'], {'b': None, 'a': None})
    result, baseline = _call(model, receiver, KEYS)
    if failure is None:
        assert model.object(result)['data'] == ['a', 'b']
        assert model.pending is None
    else:
        assert result is None and model.pending == 'keys-error'
    model.check(result, baseline)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('present', (False, True))
@pytest.mark.parametrize('explicit_default', (False, True))
def test_get_roots_key_and_default_for_the_slot_entry(relocate, present, explicit_default):
    model = BoundMethodMemory(relocate=relocate)
    value = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    receiver = model.new(ABI['PY_TYPE_DICT'], {'k': value.identity} if present else {})
    key = model.new(ABI['PY_TYPE_STR'], 'k')
    default = model.new(ABI['PY_TYPE_INSTANCE'], 'default')
    arguments = (key, default) if explicit_default else (key,)
    result, baseline = _call(model, receiver, GET, *arguments)
    if present:
        expected = value
    elif explicit_default:
        expected = default
    else:
        expected = model.globals['py_None']
    assert result.identity == expected.identity and model.pending is None
    model.check(result, baseline)


@pytest.mark.parametrize('count', (1, 2))
def test_wrong_arity_raises_without_touching_the_receiver(count):
    model = BoundMethodMemory()
    receiver = model.new(ABI['PY_TYPE_DICT'], {'a': None})
    extra = model.new(ABI['PY_TYPE_INSTANCE'], 'extra')
    result, baseline = _call(model, receiver, KEYS, *([extra] * count))
    assert result is None and model.pending == 'error-3'
    model.check(result, baseline)


@pytest.mark.parametrize('failure', ('item-1', 'item-2'))
def test_argument_copy_failure_releases_acquired_owners(failure):
    model = BoundMethodMemory(failure=failure)
    receiver = model.new(ABI['PY_TYPE_DICT'], {})
    key = model.new(ABI['PY_TYPE_STR'], 'k')
    default = model.new(ABI['PY_TYPE_INSTANCE'], 'default')
    result, baseline = _call(model, receiver, GET, key, default)
    assert result is None and model.pending == 'item-error'
    model.check(result, baseline)


@pytest.mark.parametrize('copy_failure', (1, 2))
def test_input_copy_failure_releases_only_acquired_owners(copy_failure):
    model = BoundMethodMemory(copy_failure=copy_failure)
    receiver = model.new(ABI['PY_TYPE_LIST'], None, children=[])
    result, baseline = _call(model, receiver, REVERSE)
    assert result is None and model.pending == 'copy-error'
    model.check(result, baseline)
