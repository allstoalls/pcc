"""Dynamic and ordinary scalar shifts publish through their boxed protocol."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python import (
    type_infer,
)
from pcc.frontends.python.py_ast import (
    BinOp,
    DynType,
    FloatType,
    IntType,
    Name,
)
from pcc.frontends.python.py_lift import (
    parse_and_lift,
)
from tests.python.test_shared_call_binding import (
    _emit as emit_binding,
)
from tests.python.test_slot_call_operand_roots import (
    SlotProbeCodegen,
    _calls,
    _emit as emit_operand,
)


def assert_shift_publication(text, operator, count=1):
    target = 'py_obj_lshift' if operator == '<<' else 'py_obj_rshift'
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@' + target + r'\([^\n]*\)\n', text, re.M))
    assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith('store ptr ' + call.group(1) + ','), following
    assert _calls(text, 'pcc_gc_foreign_lease_acquire')
    assert _calls(text, 'pcc_gc_foreign_lease_release')
    assert not _calls(text, 'py_int_to_i64_lane')


@pytest.mark.parametrize('operator', ('<<', '>>'))
@pytest.mark.parametrize('expression,count', [
    ('left OP right', 1),
    ('left OP 0', 1),
    ('left OP -1', 1),
    ('left OP (1 << 100)', 1),
    ('left OP (right OP 1)', 2),
    ('left[0] OP right', 1),
])
def test_shift_operands_keep_full_integer_count_and_publish_output(operator, expression, count):
    text = emit_operand('def probe(left, right):\n    return slot_operand_probe(' + expression.replace('OP', operator) + ')\n')
    assert_shift_publication(text, operator, count)


@pytest.mark.parametrize('operator', ('<<', '>>'))
@pytest.mark.parametrize('site', ('default', 'return', 'later-error'))
def test_shift_default_return_and_error_cleanup(operator, site):
    source = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    expression = 'left ' + operator + ' right'
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    else:
        body = '    return take(value=' + expression + ', later=fail())\n'
    assert_shift_publication(emit_binding(source + 'def probe(left, right):\n' + body), operator)


@pytest.mark.parametrize('operator', ('<<', '>>'))
def test_shift_original_lambda_shape(operator):
    text = emit_binding('operations = {"shift": lambda a, b: a ' + operator + ' b if 0 <= b < 64 else None}\n')
    assert_shift_publication(text, operator)


@pytest.mark.parametrize('operator', ('<<', '>>'))
def test_ordinary_integer_annotation_never_proves_a_fixed_width_shift(operator):
    text = emit_operand('def probe(left: int, right: int):\n    return slot_operand_probe(left ' + operator + ' right)\n')
    assert_shift_publication(text, operator)
    assert not _calls(text, 'py_int_shl' if operator == '<<' else 'py_int_shr')


@pytest.mark.parametrize('operator', ('<<', '>>'))
@pytest.mark.parametrize('numeric', [
    IntType(name='pcc.i64'),
    IntType(name='pcc.u64', signed=False),
    IntType(name='int', width=32),
    FloatType(name='pcc.f64'),
])
def test_shift_keeps_explicit_machine_projections(operator, numeric):
    module = type_infer.infer_module(parse_and_lift('', 'shift_projection.py', 'shift_projection'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    left = Name(span=None, ty=numeric, ident='left')
    right = Name(span=None, ty=DynType(name='dyn'), ident='right')
    expr = BinOp(span=None, ty=DynType(name='dyn'), op=operator, lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None
