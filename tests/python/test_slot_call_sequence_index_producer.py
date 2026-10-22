"""Dynamic index(x) preserves sequence ABI, arbitrary result ownership and errors."""
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


@pytest.mark.parametrize('body', (
    '    return take(value=sequence.index(item))\n',
    '    return (item, sequence.index(item))\n',
    '    return take(value=sequence.index(item), later=fail())\n',
    '    def target(value=sequence.index(item)):\n        return value\n    return target()\n',
))
def test_sequence_index_publishes_native_and_user_method_results(body):
    text = _emit(
        'def take(*, value, later=None, **extras):\n    return value\n'
        "def fail():\n    raise ValueError('later')\n"
        'def probe(sequence, item):\n' + body
    )
    function = _function(text)
    assert '@py_list_index_range(' in function
    assert '@py_tuple_index(' in function
    assert '@py_list_index(' not in function
    assert '@py_obj_call_slots(' in function
    call = re.search(r'(%dyn\.index\.boxed[^ ]*) = call [^\n]*@py_int_from_i64\([^\n]*\)\n', function)
    assert call is not None
    assert function[call.end():].lstrip().startswith('store ptr ' + call.group(1) + ',')
    assert 'phi ptr' not in function
    assert 'strict.nolib.stub' not in text
    assert '@py_cpy_' not in function


def test_sequence_index_in_original_lambda_tuple_shape():
    text = _emit(
        "ACTION_STAGES = ('source', 'parse', 'emit')\n"
        'def probe(actions):\n'
        '    return sorted(actions, key=lambda action: (action.module, ACTION_STAGES.index(action.stage)))\n'
    )
    assert '@py_tuple_index(' in text
    assert '@py_list_index_range(' in text
    assert 'strict.nolib.stub' not in text


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    ACTION_STAGES = ('source', 'parse', 'emit')
    class Action:
        def __init__(self, module, stage):
            self.module = module
            self.stage = stage
    class Result:
        def __init__(self, value):
            self.value = value
        def __del__(self):
            events.append('released')
    class Custom:
        def index(self, item):
            events.append('custom')
            gc.collect()
            return Result(item)
    class Comparable:
        def __eq__(self, other):
            events.append('compare')
            gc.collect()
            if other == 'explode':
                raise RuntimeError('comparison failed')
            return other == 'match'
    def index(sequence, item):
        return sequence.index(item)
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later')
    def main():
        assert take(value=index(['first', 'second'], 'second')) == 1
        assert take(value=index(('first', 'second'), 'first')) == 0
        for sequence in (['first'], ('first',)):
            try:
                index(sequence, 'absent')
            except ValueError:
                pass
            else:
                raise AssertionError('missing index must raise')
        for sequence in ([Comparable()], (Comparable(),)):
            assert index(sequence, 'match') == 0
            try:
                index(sequence, 'explode')
            except RuntimeError as error:
                assert str(error) == 'comparison failed'
            else:
                raise AssertionError('comparison error lost')
        result = take(value=index(Custom(), 'payload'))
        assert result.value == 'payload'
        del result
        gc.collect()
        assert events[-1] == 'released'
        try:
            take(value=index(Custom(), 'error'), later=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('later error lost')
        gc.collect()
        assert events[-1] == 'released'
        actions = [Action('beta', 'emit'), Action('alpha', 'emit'), Action('alpha', 'source')]
        ordered = sorted(actions, key=lambda action: (action.module, ACTION_STAGES.index(action.stage)))
        assert [(action.module, action.stage) for action in ordered] == [
            ('alpha', 'source'), ('alpha', 'emit'), ('beta', 'emit')]
        print('SEQUENCE_INDEX_OWNERSHIP_OK')
    main()
''')


def test_sequence_index_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'SEQUENCE_INDEX_OWNERSHIP_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_sequence_index_native_five_gc(python_program_compiler, request,
                                      explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SEQUENCE_INDEX_OWNERSHIP_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
