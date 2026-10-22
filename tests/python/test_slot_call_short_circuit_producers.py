"""Selected boolean operands retain identity through authoritative root moves."""
from __future__ import annotations

import textwrap

import pytest

from tests.python.test_slot_call_operand_roots import _calls, _emit, _probe_function
from tests.python.test_shared_call_binding import _emit as _emit_binding
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


@pytest.mark.parametrize('expression, tests, moves', (
    ('left and right', 1, 2),
    ('left or right', 1, 2),
    ('left and middle and right', 2, 4),
    ('left or middle or right', 2, 4),
    ('(left and middle) or right', 2, 4),
    ('(left or middle) and right', 2, 4),
    ('left and (middle or right)', 2, 4),
    ('left or (middle and right)', 2, 4),
))
def test_short_circuit_moves_owner_without_retesting_selected_operand(expression, tests, moves):
    text = _probe_function(_emit('def probe(left, middle, right):\n'
                                '    return slot_operand_probe(' + expression + ')\n'))
    assert len(_calls(text, 'py_obj_truthy')) == tests
    assert len(_calls(text, 'pcc_gc_root_move')) == moves
    assert _calls(text, 'pcc_gc_root_copy_borrowed_lease')
    assert _calls(text, 'pcc_gc_foreign_lease_acquire')
    assert 'call.slot.bool.rhs' in text and 'call.slot.bool.short' in text
    assert 'phi ptr' not in text


@pytest.mark.parametrize('operation', ('and', 'or'))
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
def test_short_circuit_producer_contexts(operation, site):
    expression = 'left ' + operation + ' right'
    prefix = ('from pcc.unsafe import null\n'
              'def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return str(' + expression + ')\n'
    elif site == 'later-error':
        body = '    return take(value=' + expression + ', later=fail())\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    text = _emit_binding(prefix + 'def probe(left, right):\n' + body)
    assert _calls(text, 'pcc_gc_root_move')
    assert 'call.slot.bool.rhs' in text


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Value:
        def __init__(self, name, truth):
            self.name = name
            self.truth = truth
        def __bool__(self):
            events.append(self.name)
            gc.collect()
            return self.truth
        def __del__(self):
            events.append('dispose:' + self.name)
            gc.collect()
    class Bad:
        def __bool__(self):
            gc.collect()
            raise ValueError('truth-error')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def rhs(value):
        events.append('rhs')
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later-error')
    def main():
        yes = Value('yes', True)
        no = Value('no', False)
        selected = {'identity': 42}
        assert take(value=yes or rhs(selected)) is yes
        assert events == ['yes']
        events.clear()
        assert take(value=no and rhs(selected)) is no
        assert events == ['no']
        events.clear()
        assert take(value=no or rhs(selected)) is selected
        assert events == ['no', 'rhs']
        events.clear()
        assert take(value=yes and rhs(selected)) is selected
        assert events == ['yes', 'rhs']
        events.clear()
        assert take(value=(no and yes) or rhs(selected)) is selected
        assert events == ['no', 'rhs']
        events.clear()
        assert take(value=(yes or no) and rhs(selected)) is selected
        assert events == ['yes', 'rhs']
        events.clear()
        assert take(value=yes and no and rhs(selected)) is no
        assert events == ['yes', 'no']
        events.clear()
        assert take(value=no or yes or rhs(selected)) is yes
        assert events == ['no', 'yes']
        events.clear()
        def target(value=no or selected):
            gc.collect()
            return value
        assert target() is selected
        assert events == ['no']
        events.clear()
        assert take(value=Value('temporary', False) or rhs(selected)) is selected
        gc.collect()
        assert events == ['temporary', 'dispose:temporary', 'rhs']
        events.clear()
        try:
            take(value=Bad() or rhs(selected))
        except ValueError as error:
            assert str(error) == 'truth-error'
        else:
            raise AssertionError('missing truth error')
        assert events == []
        try:
            take(value=yes or selected, later=fail())
        except ValueError as error:
            assert str(error) == 'later-error'
        else:
            raise AssertionError('missing later error')
        assert events == ['yes']
        assert take(value=yes or selected) is yes
        print('SHORT_CIRCUIT_OWNERS_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_short_circuit_native_five_gc(python_program_compiler, request,
                                     explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SHORT_CIRCUIT_OWNERS_OK\n', tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
