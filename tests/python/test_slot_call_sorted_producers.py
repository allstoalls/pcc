"""Sorted publishes through a stable result slot across callbacks and teardown."""
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


@pytest.mark.parametrize('expression', (
    'sorted(item)', 'sorted(item, reverse=True)', 'sorted(item, key=None)',
    'sorted(item, key=rank)', 'sorted(item, key=rank, reverse=True)',
))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'attribute', 'later-error'))
def test_sorted_publishes_to_authoritative_caller_slot(expression, site):
    prefix = (
        'def rank(item):\n    return item\n'
        'def take(*, value, later=None, **extras):\n    return value\n'
        'def fail():\n    raise ValueError("later")\n'
    )
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'attribute':
        body = '    class Holder:\n        value = ' + expression + '\n    return Holder\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    function = _function(text)
    assert '@py_obj_sorted_slots(' in function
    assert '@py_obj_sorted(' not in function
    assert '@py_list_reverse(' not in function
    assert 'strict.nolib.stub' not in text
    call = re.search(r'@py_obj_sorted_slots\([^\n]*ptr (%[^,)]+)\)', function)
    assert call is not None
    if site != 'return':
        # The output root survives later metadata/argument materialization.
        assert 'sorted.current' in function or site == 'attribute'


def test_sorted_evaluates_iterable_before_key_factory():
    text = _function(_emit(
        'def rank(item):\n    return item\n'
        'def source():\n    return [3, 1]\n'
        'def key_factory():\n    return rank\n'
        'def probe():\n    return sorted(source(), key=key_factory())\n'
    ))
    source = re.search(r'(%[^ ]+) = call [^\n]*@user_binding_source\([^\n]*\)\n', text)
    key = re.search(r'(%[^ ]+) = call [^\n]*@user_binding_key_factory\([^\n]*\)\n', text)
    assert source and key and source.start() < key.start() < text.index('@py_obj_sorted_slots(')
    assert text[source.end():].lstrip().startswith('store ptr ' + source.group(1) + ',')
    assert text[key.end():].lstrip().startswith('store ptr ' + key.group(1) + ',')


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Item:
        def __init__(self, value, label):
            self.value = value
            self.label = label
        def __lt__(self, other):
            gc.collect()
            return self.value < other.value
    class Key:
        def __init__(self, value):
            self.value = value
        def __lt__(self, other):
            gc.collect()
            return self.value < other.value
        def __del__(self):
            events.append('drop-key')
            gc.collect()
    def source():
        events.append('source')
        return [Item(2, 'a'), Item(1, 'b'), Item(2, 'c'), Item(1, 'd')]
    def rank(item):
        events.append(item.label)
        gc.collect()
        return Key(item.value)
    def key_factory():
        events.append('key-factory')
        return rank
    def identity(item):
        gc.collect()
        return item
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later')
    def bad_key(item):
        if item.label == 'b':
            raise ValueError('key-error')
        return Key(item.value)
    def main():
        result = take(value=sorted(source(), key=key_factory(), reverse=True))
        assert events[:6] == ['source', 'key-factory', 'a', 'b', 'c', 'd']
        assert [item.label for item in result] == ['a', 'c', 'b', 'd']
        gc.collect()
        assert events.count('drop-key') == 4
        original = source()
        alias_keys = sorted(original, key=identity)
        assert [item.label for item in alias_keys] == ['b', 'd', 'a', 'c']
        assert alias_keys[0] is original[1]
        assert [item.label for item in original] == ['a', 'b', 'c', 'd']
        direct = sorted(original, reverse=True)
        assert [item.label for item in direct] == ['a', 'c', 'b', 'd']
        try:
            sorted(original, key=bad_key)
        except ValueError as error:
            assert str(error) == 'key-error'
        else:
            raise AssertionError('key failure lost')
        gc.collect()
        assert events.count('drop-key') == 5
        def saved(value=sorted([3, 1, 2])):
            gc.collect()
            return value
        class Holder:
            value = sorted([3, 1, 2], reverse=True)
        assert saved() == [1, 2, 3] and Holder.value == [3, 2, 1]
        try:
            take(value=sorted(original), later=fail())
        except ValueError as error:
            assert str(error) == 'later'
        assert sorted([], key=rank) == []
        assert sorted([1], key=None) == [1]
        print('SORTED_OWNERSHIP_OK')
    main()
''')


def test_sorted_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'SORTED_OWNERSHIP_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_sorted_native_five_gc(python_program_compiler, request,
                               explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SORTED_OWNERSHIP_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)


@pytest.mark.parametrize('keyword', ('', ', key=None'))
def test_sorted_keeps_explicit_valueclass_comparator_adapter(keyword):
    text = _emit(
        'def valueclass(cls):\n    return cls\n'
        '@valueclass\n'
        'class Token:\n'
        '    value: int\n'
        '    def __lt__(self, other: Token) -> bool:\n'
        '        return self.value < other.value\n'
        'def probe():\n'
        '    values = [Token(2), Token(1)]\n'
        '    return sorted(values' + keyword + ')\n'
    )
    body = _function(text)
    assert 'sorted.compare.class' in body
    assert '@py_obj_getattr(' in body
    call = re.search(r'@py_obj_sorted_slots\(ptr [^,]+, ptr [^,]+, ptr (%[^,]+), i64', body)
    assert call is not None
    comparator = call.group(1)
    alias = re.search(re.escape(comparator) + r' = bitcast ptr (%sorted\.compare\.operand[^ ]+) to ptr', body)
    assert comparator.startswith('%sorted.compare.operand') or alias is not None
    assert 'strict.nolib.stub' not in text


@pytest.mark.parametrize('key', (
    'lambda value: value',
    'lambda value: value[0]',
    'lambda value: -value',
    'lambda value: (lookup[value], value)',
))
def test_sorted_inline_keys_use_owned_callable_producers(key):
    text = _emit('def probe(item, lookup):\n    return sorted(item, key=' + key + ')\n')
    body = _function(text)
    assert '@py_obj_sorted_slots(' in body
    assert '@py_obj_sorted(' not in body
    assert 'strict.nolib.stub' not in text
    call = re.search(r'@py_obj_sorted_slots\(ptr [^,]+, ptr (%[^,]+), ptr ', body)
    assert call is not None
