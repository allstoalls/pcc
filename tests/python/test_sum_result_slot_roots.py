"""sum() publishes its runtime result before cleanup, in CPython's order.

The call is one owned runtime call (py_builtin_sum, py_builtin_sum1 without
a start) over rooted operands; the native program runs iterators that
collect garbage, fail and finalize on all five collectors. Host IR/CFG and
reference cases are independent of the native five-GC gate.
"""
from __future__ import annotations

import re
import textwrap

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


EXPRESSIONS = (
    'sum(source)', 'sum(source, start)', 'sum([1, 2, 3])',
    'sum((), start)', 'sum((value for value in source))',
    'sum([1.5, 2, True], 0.5)', 'sum((), 1.5)',
)
# The runtime entry each call uses: py_builtin_sum takes an explicit start.
WITH_START = {
    'sum(source)': 'py_builtin_sum1',
    'sum(source, start)': 'py_builtin_sum',
    'sum([1, 2, 3])': 'py_builtin_sum1',
    'sum((), start)': 'py_builtin_sum',
    'sum((value for value in source))': 'py_builtin_sum1',
    'sum([1.5, 2, True], 0.5)': 'py_builtin_sum',
    'sum((), 1.5)': 'py_builtin_sum',
}


def _source(expression, site='argument', annotation='list'):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def saved(item=' + expression + '):\n        return item\n    return saved()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'discard':
        body = '    ' + expression + '\n    return None\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    return prefix + 'def probe(source: ' + annotation + ', start: int):\n' + body


@pytest.mark.parametrize('expression', EXPRESSIONS)
@pytest.mark.parametrize('site', ('argument', 'return', 'default', 'discard', 'later-error'))
def test_sum_publication_and_caller_result_sink(expression, site):
    function = _function(_emit(_source(expression, site)))
    calls = list(re.finditer(
        r'^  (%call\.slot\.runtime[^ ]+) = call [^\n]*@(py_builtin_sum1?)\([^\n]*\)\n',
        function, re.M,
    ))
    assert len(calls) == 1
    assert calls[0].group(2) == WITH_START[expression]
    for call in calls:
        assert function[call.end():].lstrip().startswith('store ptr ' + call.group(1) + ',')
    if site in ('argument', 'default', 'later-error'):
        assert 'sum.result.operand' not in function
    else:
        assert '@pcc_gc_take_pinned_slot(' in function
    assert '@py_cpy_' not in function


@pytest.mark.parametrize('expression,annotation', (
    ('sum(source)', 'list'), ('sum(source, start)', 'tuple'),
    ('sum(source)', 'object'), ('sum((value for value in source))', 'list'),
    ('sum([1.5, 2, True], 0.5)', 'list'),
))
def test_sum_success_and_error_edges_balance_precise_root_stacks(expression, annotation):
    from tests.python.test_slot_call_lexical_roots import _emit as emit_roots
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    source = _source(expression, 'later-error', annotation)
    codegen, text = emit_roots(source)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)
    assert all(flag is None and lifo for _slot, flag, lifo in codegen._slot_call_root_records)


def test_sum_runtime_call_uses_registered_input_and_result_owners(monkeypatch):
    original = L1CodeGen._slot_call_runtime_call
    observed = []

    def observe(self, name, roots, **kwargs):
        if name in ('py_builtin_sum', 'py_builtin_sum1'):
            for root in roots:
                self._slot_call_root_record(root)
            self._slot_call_root_record(kwargs['result_slot'])
            observed.append((name, len(roots)))
        return original(self, name, roots, **kwargs)

    monkeypatch.setattr(L1CodeGen, '_slot_call_runtime_call', observe)
    for annotation in ('object', 'list', 'tuple'):
        _emit(_source('sum(source)', annotation=annotation))
        _emit(_source('sum(source, start)', annotation=annotation))
    assert observed == [('py_builtin_sum1', 1), ('py_builtin_sum', 2)] * 3


def test_sum_source_literal_finishes_evaluation_before_start_and_arithmetic():
    body = _function(_emit(
        'def first() -> int:\n    return 1\ndef second() -> int:\n    return 2\n'
        'def initial() -> int:\n    return 3\ndef probe():\n'
        '    return sum([first(), second()], initial())\n'
    ))
    positions = [body.index('@user_binding_' + name + '(') for name in ('first', 'second', 'initial')]
    assert positions == sorted(positions)
    assert positions[-1] < body.index('@py_builtin_sum(')


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Counter:
        def __init__(self, count):
            self.count = count
            self.index = 0
        def __iter__(self):
            gc.collect()
            return self
        def __next__(self):
            gc.collect()
            if self.index >= self.count:
                raise StopIteration()
            value = self.index
            self.index += 1
            return value
        def __del__(self):
            events.append('counter-disposed')
            gc.collect()
    class Failing(Counter):
        def __next__(self):
            gc.collect()
            raise ValueError('iterator-failed')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def mark(label, value: int) -> int:
        events.append(label)
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later')
    def main():
        assert take(value=sum([2**70, 3, 4])) == 2**70 + 7
        assert sum((), 2**75) == 2**75
        empty_start = 1.5
        assert sum((), empty_start) == empty_start
        assert sum([mark('first', 2), mark('second', 3)], mark('start', 7)) == 12
        assert events == ['first', 'second', 'start']
        events.clear()
        assert sum(Counter(5), 100) == 110
        gc.collect()
        assert events == ['counter-disposed']
        events.clear()
        assert sum(Counter(0)) == 0
        gc.collect()
        assert events == ['counter-disposed']
        events.clear()
        try:
            sum(Failing(1))
        except ValueError as error:
            assert str(error) == 'iterator-failed'
        else:
            raise AssertionError('iterator error lost')
        gc.collect()
        assert events == ['counter-disposed']
        def saved(value=sum([2**70, 9])):
            gc.collect()
            return value
        assert saved() == 2**70 + 9
        assert saved() is saved()
        try:
            take(value=sum([2**70, 4]), later=fail())
        except ValueError as error:
            assert str(error) == 'later'
        else:
            raise AssertionError('later error lost')
        print('SUM_RESULT_OWNERS_OK')
    main()
''')


def test_sum_program_reference(tmp_path):
    assert_reference_program(PROGRAM, 'SUM_RESULT_OWNERS_OK\n', tmp_path)


def test_sum_full_native_fixture_uses_owned_sum_paths():
    text = _emit(PROGRAM)
    assert re.search(r'call [^\n]*@py_builtin_sum\(', text)
    assert re.search(r'call [^\n]*@py_builtin_sum1\(', text)
    assert not re.search(r'call [^\n]*@py_cpy_', text)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_sum_native_five_gc(python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SUM_RESULT_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
