"""Computed native callees own inputs and publish results before cleanup."""
import re
import textwrap

import pytest

from tests.python.test_shared_call_binding import _emit, _function
from tests.python.owned_regression_support import (
    assert_owned_program, assert_reference_program, explicit_owned_runtime,
)


@pytest.mark.parametrize('expression', (
    'factory()(value)', 'functions[0](value)', '(left if flag else right)(value)',
))
@pytest.mark.parametrize('context', ('return', 'argument', 'default', 'later-error'))
def test_computed_callable_uses_rooted_dispatch(expression, context):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if context == 'return':
        body = '    return ' + expression + '\n'
    elif context == 'default':
        body = '    def target(item=' + expression + '):\n        return item\n    return target()\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if context == 'later-error' else '') + ')\n'
    text = _function(_emit(prefix + 'def probe(factory, functions, left, right, flag, value):\n' + body))
    assert re.search(r'\bcall i64 [^\n]*@py_obj_call_slots\(', text)
    assert not re.search(r'\bcall ptr [^\n]*@py_obj_call\(', text)
    assert 'expr.obj.call.callable' in text
    assert 'expr.obj.call.invoke' in text


def test_closure_cell_callable_preserves_result_sink():
    text = _emit('''def outer(constructor, value):
    def capture():
        return constructor
    return [constructor(value), capture]
''')
    body = _function(text, 'user_binding_outer')
    assert re.search(r'\bcall ptr [^\n]*@py_list_getitem\(', body)
    assert 'cell.bound' in body
    assert 'expr.obj.call.invoke' in body
    assert not re.search(r'\bcall ptr [^\n]*@py_obj_call\(', body)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Payload:
        def __init__(self, name):
            self.name = name
        def __del__(self):
            events.append('drop:' + self.name)
            gc.collect()
    class Callable:
        def __call__(self, value):
            events.append('call')
            gc.collect()
            return value
        def __del__(self):
            events.append('drop:callable')
            gc.collect()
    def factory():
        events.append('factory')
        return Callable()
    def operand():
        events.append('operand')
        gc.collect()
        return Payload('value')
    def take(*, value, later=None, **extras):
        return value
    def fail():
        events.append('fail')
        gc.collect()
        raise ValueError('later')
    result = take(value=factory()(operand()))
    assert events == ['factory', 'operand', 'call', 'drop:callable']
    assert result.name == 'value'
    del result
    assert events[-1] == 'drop:value'
    events.clear()
    try:
        take(value=factory()(operand()), later=fail())
    except ValueError as error:
        assert str(error) == 'later'
    else:
        raise AssertionError('missing error')
    assert events == ['factory', 'operand', 'call', 'drop:callable', 'fail', 'drop:value']
    def defaults():
        def target(value=factory()(operand())):
            gc.collect()
            return value
        return target
    target = defaults()
    assert target() is target()
    del target
    gc.collect()
    def closure(constructor):
        def keep():
            return constructor
        return constructor(Payload('closure')), keep
    result, keep = closure(Callable())
    assert result.name == 'closure'
    del result
    del keep
    gc.collect()
    print('EXPRESSION_CALLABLE_OWNERS_OK')
''')


def test_expression_callable_reference_program(tmp_path):
    assert_reference_program(PROGRAM, 'EXPRESSION_CALLABLE_OWNERS_OK\n', tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_expression_callable_native_five_gc(python_program_compiler, request,
                                            explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'EXPRESSION_CALLABLE_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
