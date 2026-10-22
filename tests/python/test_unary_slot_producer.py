"""Generic unary object results use the same strict owner sink as calls."""
import re

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.python.test_lambda_constructor_roots import (
    emit,
    immediate_store,
)


@pytest.mark.parametrize('operator,runtime', [('-', 'py_obj_neg'), ('+', 'py_obj_pos'), ('~', 'py_obj_invert')])
@pytest.mark.parametrize('context', ['argument', 'default', 'tuple', 'lambda_return'])
def test_dynamic_unary_result_is_published_before_cleanup(operator, runtime, context):
    expression = operator + 'value'
    if context == 'argument':
        tail = '    return take(value=' + expression + ')\n'
    elif context == 'default':
        tail = '    def kept(arg=' + expression + '):\n        return arg\n    return kept\n'
    elif context == 'tuple':
        tail = '    return take(value=(' + expression + ',))\n'
    else:
        tail = '    return lambda value: ' + expression + '\n'
    text = emit('def take(*, value, **extras):\n    return value\ndef probe(value):\n' + tail)
    produced = immediate_store(text, runtime)
    assert produced >= 1, (operator, context)


def test_known_bool_inversion_remains_an_explicit_unsupported_boundary():
    with pytest.raises(L1CodegenError, match='literal-derived integer tree'):
        emit('def take(*, value, **extras):\n    return value\ndef probe():\n    return take(value=~True)\n')


def test_explicit_machine_projection_keeps_its_scalar_unary_route():
    text = emit('from pcc import i64\ndef take(*, value, **extras):\n    return value\ndef probe(value: i64):\n    return take(value=-value)\n')
    calls = re.findall(r'\bcall[^\n]*@py_obj_neg\(', text)
    assert calls == []
    assert re.search(r'\bsub i64 0,', text)
