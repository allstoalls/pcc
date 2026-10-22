"""Collection consumers require fresh, rooted producers for their input Calls."""
from __future__ import annotations

import textwrap
import re

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('expression,runtime', (
    ('list(range(3))', 'py_int_from_i64'),
    ('tuple(range(3, 0, -1))', 'py_int_from_i64'),
    ('list(reversed(item))', 'py_obj_getitem'),
    ('list(item.keys())', 'py_dict_keys'),
    ('tuple(item.values())', 'py_dict_values'),
    ('list(item.items())', 'py_dict_items'),
    ('sorted(os.listdir(item))', 'py_os_listdir'),
))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'attribute', 'later-error'))
def test_collection_input_result_is_published_before_any_cleanup(expression, runtime, site):
    prefix = ('import os\ndef take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'attribute':
        body = '    class Holder:\n        value = ' + expression + '\n    return Holder\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(item):\n' + body)
    calls = list(re.finditer(
        r'^  (%call\.slot\.runtime[^ ]*) = call [^\n]*@' + runtime + r'\([^\n]*\)\n',
        text, re.M,
    ))
    assert calls, runtime
    for call in calls:
        assert text[call.end():].lstrip().startswith('store ptr ' + call.group(1) + ',')
    assert 'strict.nolib.stub' not in text


@pytest.mark.parametrize('method', ('keys', 'values', 'items'))
def test_dynamic_dictionary_view_preserves_override_dispatch(method):
    text = _function(_emit('def probe(item):\n    return list(item.' + method + '())\n'))
    assert '@py_obj_type_tag(' in text
    assert '@py_dict_' + method + '(' in text
    assert '@py_obj_getattr(' in text
    assert '@py_obj_call_slots(' in text
    assert '@py_obj_call_method(' not in text


@pytest.mark.parametrize('expression', (
    "tuple(item.resolve())", "list(item.finditer('ab'))",
    "mapping.get(item.group('callee'), set())",
))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'later-error'))
def test_generic_method_input_retains_original_call_result_sink(expression, site):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _function(_emit(prefix + 'def probe(item, mapping):\n' + body))
    assert '@py_obj_call_slots(' in text
    assert '@py_obj_getattr(' in text
    assert '@py_obj_call_method(' not in text
    assert '@py_obj_call_method_kwargs(' not in text


PROGRAM = textwrap.dedent('''\
    import gc
    import os
    import re
    events = []
    class Item:
        def __init__(self, value):
            self.value = value
        def __del__(self):
            events.append(self.value)
            gc.collect()
    class Indexed:
        def __len__(self):
            gc.collect()
            return 3
        def __getitem__(self, index):
            gc.collect()
            return Item(index)
    class Views:
        def keys(self):
            gc.collect()
            return [3, 1]
        def values(self):
            gc.collect()
            return [4, 2]
        def items(self):
            gc.collect()
            return [(3, 4), (1, 2)]
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later')
    def main():
        assert list(range(4)) == [0, 1, 2, 3]
        assert tuple(range(3, 0, -1)) == (3, 2, 1)
        result = take(value=list(reversed(Indexed())))
        assert [item.value for item in result] == [2, 1, 0]
        del result
        gc.collect()
        assert sorted(events) == [0, 1, 2]
        value = Item(7)
        data = {'a': value, 'b': value}
        assert list(data.keys()) == ['a', 'b']
        values = tuple(data.values())
        pairs = list(data.items())
        assert values[0] is value and values[1] is value
        assert pairs[0][1] is value and pairs[1][1] is value
        assert list(Views().keys()) == [3, 1]
        assert tuple(Views().values()) == (4, 2)
        assert list(Views().items()) == [(3, 4), (1, 2)]
        pattern = re.compile(r'([a-z]+)')
        matches = list(pattern.finditer('ab cd'))
        assert [match.group(0) for match in matches] == ['ab', 'cd']
        mapping = {'ab': {1}}
        assert mapping.get(matches[0].group(0), set()) == {1}
        names = sorted(os.listdir('.'))
        assert '.' not in names and '..' not in names
        try:
            take(value=list(reversed(Indexed())), later=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('later failure was lost')
        print('collection-input-owners-ok')
    main()
''')


def test_collection_input_reference_program(tmp_path):
    assert_reference_program(PROGRAM, 'collection-input-owners-ok\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_collection_input_native_five_gc(python_program_compiler, request,
                                         explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, 'collection-input-owners-ok\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
