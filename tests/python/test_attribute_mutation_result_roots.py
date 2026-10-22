"""Side-effect builtin expressions still return an independently owned None."""
import re

import pytest

from tests.python.test_lambda_constructor_roots import emit
from tests.python.test_lambda_adapter_scope_lifetime import check_frames


@pytest.mark.parametrize('operation,runtime', [('setattr', 'py_obj_setattr'), ('delattr', 'py_obj_delattr')])
@pytest.mark.parametrize('context', ['lambda', 'argument', 'default', 'tuple'])
def test_literal_attribute_mutation_publishes_none_before_operand_cleanup(operation, runtime, context):
    expression = operation + '(item, "init"' + (', new_expr)' if operation == 'setattr' else ')')
    if context == 'lambda':
        tail = '    return lambda new_expr: ' + expression + '\n'
    elif context == 'argument':
        tail = '    return take(value=' + expression + ')\n'
    elif context == 'default':
        tail = '    def kept(value=' + expression + '):\n        return value\n    return kept\n'
    else:
        tail = '    return (' + expression + ',)\n'
    text = emit('def take(*, value, **extras):\n    return value\ndef probe(item, new_expr):\n' + tail)
    callee = re.search(r'^define[^\n]*@(user_lambda_owned__native_lambda_\d+|user_lambda_owned_probe)\([^\n]*\).*?^}', text, re.M | re.S)
    bodies = re.findall(r'^define[^\n]*\([^\n]*\).*?^}', text, re.M | re.S)
    body = next(body for body in bodies if re.search(r'\bcall[^\n]*@'+runtime+r'\(', body))
    mutation = re.search(r'\bcall[^\n]*@'+runtime+r'\(', body)
    assert mutation is not None
    retain = re.search(r'(%[-\w.]+) = call ptr[^\n]*@pcc_gc_retain\(ptr %none[^\n]*\n([^\n]+)', body)
    assert retain is not None
    assert 'store ptr ' + retain.group(1) in retain.group(2)
    assert mutation.start() < retain.start()
    after = body[retain.end():]
    assert re.search(r'\bcall[^\n]*@pcc_gc_store_root\(ptr [^,]+, ptr null\)', after)
    assert re.search(r'\bcall[^\n]*@pcc_gc_foreign_lease_acquire\(', body[:mutation.start()])
    assert re.search(r'\bcall[^\n]*@pcc_gc_foreign_lease_release\(', body[mutation.end():retain.start()])
    if context == 'lambda':
        check_frames(body)


def test_original_setter_lambda_tuple_shape_has_owned_none_return():
    text = emit('''def extract(item):
    return item.name, item.init, lambda new_expr: setattr(item, "init", new_expr)
''')
    body = re.search(r'^define[^\n]*@user_lambda_owned__native_lambda_\d+\([^\n]*\).*?^}', text, re.M | re.S)
    assert body is not None
    assert re.search(r'\bcall[^\n]*@py_obj_setattr\(', body.group(0))
    assert re.search(r'\bcall[^\n]*@pcc_gc_retain\(ptr %none', body.group(0))
    check_frames(body.group(0))
