"""Generic bitwise results publish before error checks and operand disposal."""
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
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import (
    _emit as emit_binding,
)
from tests.python.test_slot_call_operand_roots import (
    SlotProbeCodegen,
    _calls,
    _emit as emit_operand,
)


def assert_bitwise_publication(text, operator, count=1):
    symbol = {'&': 'py_obj_and', '|': 'py_obj_or', '^': 'py_obj_xor'}[operator]
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@' + symbol + r'\([^\n]*\)\n', text, re.M))
    assert len(calls) == count
    for call in calls:
        next_line = text[call.end():].splitlines()[0]
        assert next_line.lstrip().startswith('store ptr ' + call.group(1) + ','), next_line
    assert _calls(text, 'pcc_gc_foreign_lease_acquire')
    assert _calls(text, 'pcc_gc_foreign_lease_release')
    assert not _calls(text, 'py_int_to_i64_lane')


@pytest.mark.parametrize('operator', ('&', '|', '^'))
@pytest.mark.parametrize('annotation', ('', ': int'))
@pytest.mark.parametrize('shape,count', [('left OP right', 1), ('(left OP right) OP right', 2), ('left OP (right OP left)', 2), ('left OP 15', 1)])
def test_generic_bitwise_has_immediate_output_owner(operator, annotation, shape, count):
    text = emit_operand('def probe(left' + annotation + ', right' + annotation + '):\n    return slot_operand_probe(' + shape.replace('OP', operator) + ')\n')
    assert_bitwise_publication(text, operator, count)


@pytest.mark.parametrize('operator', ('&', '|', '^'))
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
def test_bitwise_owner_contexts(operator, site):
    expression = 'left ' + operator + ' right'
    source = "def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError('later')\n"
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + expression + ', later=fail())\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    assert_bitwise_publication(emit_binding(source + 'def probe(left, right):\n' + body), operator)


@pytest.mark.parametrize('operator', ('&', '|', '^'))
@pytest.mark.parametrize('machine', [IntType(name='pcc.i64'), IntType(name='pcc.u64', signed=False), IntType(name='int', width=32), FloatType(name='float', width=32), FloatType(name='pcc.f64')])
def test_bitwise_does_not_capture_explicit_machine_operands(operator, machine):
    module = type_infer.infer_module(parse_and_lift('', 'bitwise_projection.py', 'bitwise_projection'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    expression = BinOp(span=None, ty=DynType(name='dyn'), op=operator, lhs=Name(span=None, ty=machine, ident='left'), rhs=Name(span=None, ty=DynType(name='dyn'), ident='right'))
    assert codegen._slot_call_binary_runtime(expression, object_boundary=True) is None


def test_original_hex_index_bitmask_shape_has_runtime_output_owner():
    source = '''def _hex64(value) -> str:
    digits = "0123456789abcdef"
    out = ""
    shift = 60
    while shift >= 0:
        out += digits[(value >> shift) & 15]
        shift -= 4
    return "0x" + out
'''
    assert_bitwise_publication(emit_binding(source), '&')


def test_original_nested_section_flags_shape_has_runtime_output_owners():
    source = '''def section(spec):
    return consume(spec.S_REGULAR | spec.S_ATTR_PURE_INSTRUCTIONS | spec.S_ATTR_SOME_INSTRUCTIONS)
def consume(flags):
    return flags
'''
    assert_bitwise_publication(emit_binding(source), '|', 2)


def test_ast_integer_field_bitwise_value_uses_the_object_protocol():
    source = 'class IntLit:\n    def __init__(self, value: int, **extras):\n        self.value = value\ndef combine(lhs: IntLit, rhs: IntLit):\n    return IntLit(value=lhs.value | rhs.value)\n'
    assert_bitwise_publication(emit_binding(source), '|')


PROGRAM = '''\
import gc

events = []
marker = {'answer': 42}

class Left:
    def __and__(self, other):
        gc.collect()
        events.append('left')
        return NotImplemented
    def __del__(self):
        events.append('left-drop')

class Right:
    def __rand__(self, other):
        gc.collect()
        events.append('right')
        return marker
    def __del__(self):
        events.append('right-drop')

class Alias:
    def __or__(self, other):
        gc.collect()
        return self

class Raising:
    def __xor__(self, other):
        gc.collect()
        raise KeyError('operator')

def bit_and(left, right):
    return left & right

def bit_or(left, right):
    return left | right

def bit_xor(left, right):
    return left ^ right

def take(*, value, later=None, **extras):
    gc.collect()
    events.append('take')
    return value

def fail():
    events.append('later')
    raise ValueError('later')

def main():
    large = 1 << 200
    assert bit_and(-large, large + 17) == large
    assert bit_or(-large, 17) == -large + 17
    assert bit_xor(large, large + 17) == 17
    assert bit_and(True, False) is False
    assert bit_or(False, True) is True
    assert bit_xor(True, True) is False
    assert type(bit_or(True, 2)) is int
    assert bit_and({1, 2}, {2, 3}) == {2}
    assert bit_or({1, 2}, {2, 3}) == {1, 2, 3}
    assert bit_xor({1, 2}, {2, 3}) == {1, 3}
    left = {'x': 1, 'same': 'left'}
    right = {'y': 2, 'same': 'right'}
    merged = take(value=bit_or(left, right))
    assert merged == {'x': 1, 'y': 2, 'same': 'right'}
    assert merged is not left and merged is not right
    alias = Alias()
    assert take(value=alias | alias) is alias
    events.clear()
    assert take(value=Left() & Right()) is marker
    assert events[:2] == ['left', 'right']
    assert events.count('left-drop') == 1 and events.count('right-drop') == 1
    assert events[-1] == 'take'
    events.clear()
    try:
        take(value=Left() & Right(), later=fail())
    except ValueError:
        pass
    else:
        raise AssertionError('missing later exception')
    assert events[:2] == ['left', 'right'] and 'take' not in events
    assert events.count('left-drop') == 1 and events.count('right-drop') == 1
    try:
        take(value=Raising() ^ 3, later=fail())
    except KeyError:
        pass
    else:
        raise AssertionError('missing operator exception')
    def default(value=bit_or(left, right)):
        gc.collect()
        return value
    assert default() == merged
    try:
        bit_and(1.5, 2)
    except TypeError:
        pass
    else:
        raise AssertionError('float accepted as integer')
    print('BITWISE_OWNER_NATIVE_OK')

main()
'''


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_bitwise_native_ownership(python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'BITWISE_OWNER_NATIVE_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
