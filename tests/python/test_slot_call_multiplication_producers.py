"""Multiplication passes authoritative roots to the numeric/repetition ABI."""
from __future__ import annotations

import re

import pytest

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)
from pcc.frontends.python import (
    type_infer,
)
from pcc.frontends.python.py_ast import (
    BinOp,
    ComplexType,
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


def assert_multiplication_slots(text, count=1):
    checked = 0
    for name, body in function_bodies(text):
        aliases = pointer_bitcast_aliases(body)
        calls = list(re.finditer(r'^  (%[^ ]+) = call i64[^\n@]*@py_obj_mul_slots\(([^\n]*)\)', body, re.M))
        for call in calls:
            pointers = [canonical_pointer(argument.strip().split()[-1], aliases) for argument in call[2].split(',')]
            assert len(pointers) == 3 and len(set(pointers)) == 3, (name, pointers)
            for pointer in pointers:
                assert re.search(re.escape(pointer) + r' = alloca ptr', body), (name, pointer)
                registrations = list(re.finditer(r'call void[^\n@]*@pcc_gc_frame_enter(?:_lifo)?\([^\n]*ptr (%[\w.$]+)\)', body[:call.start()]))
                assert any(canonical_pointer(registration[1], aliases) == pointer for registration in registrations), (name, pointer)
            # The scalar return is checked as status; no raw result phi or
            # pointer store is allowed to pretend that status is an owner.
            assert re.search(r'icmp slt i64 ' + re.escape(call[1]) + r', 0', body[call.end():])
            checked += 1
    assert checked == count
    assert not _calls(text, 'py_int_to_i64_lane')
    assert not _calls(text, 'py_int_value_i64')
    assert not _calls(text, 'py_obj_mul')


@pytest.mark.parametrize('expression,count', [
    ('left * right', 1),
    ('left * 131', 1),
    ('(left - 1) * right', 1),
    ('left * (right * 2)', 2),
    ('"ab" * right', 1),
    ('b"\\0" * right', 1),
    ('[left] * right', 1),
    ('(left,) * right', 1),
    ('right * "ab"', 1),
    ('right * [left]', 1),
    ('[left] * len(right)', 1),
])
def test_multiplication_passes_owning_operand_and_output_slots(expression, count, tmp_path):
    text = emit_operand('def probe(left, right):\n    return slot_operand_probe(' + expression + ')\n')
    (tmp_path / 'program.ll').write_text(text)
    assert_multiplication_slots(text, count)


@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error', 'index', 'lambda'))
def test_multiplication_contexts_use_the_same_slot_producer(site):
    prefix = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    if site == 'default':
        body = '    def saved(value=left * right):\n        return value\n    return saved()\n'
    elif site == 'return':
        body = '    return left * right\n'
    elif site == 'later-error':
        body = '    return take(value=left * right, later=fail())\n'
    elif site == 'index':
        body = '    return right[(left + 1) * 2]\n'
    elif site == 'lambda':
        body = '    function = lambda value: value * 2\n    return function(left)\n'
    else:
        body = '    return take(value=left * right)\n'
    assert_multiplication_slots(emit_binding(prefix + 'def probe(left, right):\n' + body))


@pytest.mark.parametrize('annotation', ('int', 'float'))
def test_ordinary_numeric_annotation_is_not_a_literal_overflow_proof(annotation):
    text = emit_operand('def probe(left: ' + annotation + ', right: ' + annotation + '):\n    return slot_operand_probe(left * right)\n')
    assert_multiplication_slots(text)
    assert not _calls(text, 'py_int_mul')
    assert not _calls(text, 'py_float_mul')


@pytest.mark.parametrize('projection', (
    IntType(name='pcc.i64'),
    IntType(name='pcc.u64', signed=False),
    IntType(name='int', width=32),
    IntType(name='int', signed=False),
    FloatType(name='pcc.f64'),
    FloatType(name='float', width=32),
))
def test_multiplication_preserves_separate_machine_routes(projection):
    module = type_infer.infer_module(parse_and_lift('', 'multiply_projection.py', 'multiply_projection'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    left = Name(span=None, ty=projection, ident='left')
    right = Name(span=None, ty=DynType(name='dyn'), ident='right')
    expr = BinOp(span=None, ty=DynType(name='dyn'), op='*', lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


def test_complex_multiplication_uses_the_runtime_only_at_an_object_boundary():
    module = type_infer.infer_module(parse_and_lift('', 'multiply_complex.py', 'multiply_complex'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    left = Name(span=None, ty=ComplexType(name='complex'), ident='left')
    for right_ty in (DynType(name='dyn'), ComplexType(name='complex')):
        right = Name(span=None, ty=right_ty, ident='right')
        expr = BinOp(span=None, ty=ComplexType(name='complex'), op='*', lhs=left, rhs=right)
        assert codegen._slot_call_binary_runtime(expr, object_boundary=True) == 'py_obj_mul_slots'
        assert codegen._slot_call_binary_runtime(expr) is None


@pytest.mark.parametrize('lane', ('_freestanding_module', '_runtime_port_module'))
def test_manual_runtime_modules_keep_their_existing_projection(lane):
    module = type_infer.infer_module(parse_and_lift('', 'multiply_manual.py', 'multiply_manual'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    setattr(codegen, lane, True)
    left = Name(span=None, ty=DynType(name='dyn'), ident='left')
    expr = BinOp(span=None, ty=DynType(name='dyn'), op='*', lhs=left, rhs=left)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


def test_literal_integer_product_retains_its_proven_kernel():
    text = emit_operand('def probe():\n    return slot_operand_probe((1 << 100) * 3)\n')
    assert _calls(text, 'py_int_mul')
    assert not _calls(text, 'py_obj_mul_slots')


PROGRAM = '''\
import gc
import sys

events = []
marker = {'value': 31}


class Left:
    def __mul__(self, other):
        gc.collect()
        events.append('mul')
        return NotImplemented


class Right:
    def __rmul__(self, other):
        gc.collect()
        events.append('rmul')
        return marker


class Count:
    def __index__(self):
        gc.collect()
        events.append('index')
        return 3

    def __mul__(self, other):
        events.append('count-mul')
        return NotImplemented

    def __rmul__(self, other):
        events.append('count-rmul')
        return NotImplemented


class ReplacingCount:
    def __init__(self, source):
        self.source = source

    def __index__(self):
        self.source[:] = [marker]
        gc.collect()
        return 2


class RaisingCount:
    def __index__(self):
        gc.collect()
        raise KeyError('index')


class Temporary:
    def __del__(self):
        events.append('temporary-drop')


def multiply(left, right):
    return left * right


def take(*, value, later=None, **extras):
    gc.collect()
    return value


def fail():
    raise ValueError('later')


def main():
    huge = 1 << 200
    assert multiply(huge, -huge) == -(1 << 400)
    assert multiply(1.25, 4) == 5.0
    assert multiply(True, 7) == 7
    events.clear()
    assert multiply(Left(), Right()) is marker
    assert events == ['mul', 'rmul']

    values = ['abé', b'ab', bytearray(b'ab'), [marker], (marker,)]
    for value in values:
        assert take(value=multiply(value, 0)) == value[:0]
        assert multiply(value, -3) == value[:0]
        assert multiply(value, 3) == value + value + value
        assert multiply(3, value) == value + value + value
        events.clear()
        assert multiply(value, Count()) == value + value + value
        assert events == ['count-rmul', 'index']
        events.clear()
        assert multiply(Count(), value) == value + value + value
        assert events == ['count-mul', 'index']
        try:
            multiply(value, RaisingCount())
        except KeyError as error:
            assert error.args[0] == 'index'
        else:
            raise AssertionError('missing index exception')
        try:
            multiply(value, 1 << 100)
        except OverflowError:
            pass
        else:
            raise AssertionError('wide count truncated')

    immutable = (marker,)
    assert multiply(immutable, 1) is immutable
    repeated = multiply([marker], 4)
    for item in repeated:
        assert item is marker
    source = [1, 2]
    repeated = multiply(source, ReplacingCount(source))
    assert repeated == [marker, marker]
    assert repeated[0] is marker and repeated[1] is marker
    events.clear()
    try:
        take(value=multiply([Temporary()], 3), later=fail())
    except ValueError as error:
        assert error.args[0] == 'later'
    else:
        raise AssertionError('missing later exception')
    gc.collect()
    assert events == ['temporary-drop']

    def saved(value=multiply([marker], 2)):
        gc.collect()
        return value
    assert saved()[0] is marker and saved()[1] is marker
    for value in ('ab', b'ab'):
        try:
            multiply(value, sys.maxsize)
        except OverflowError:
            pass
        else:
            raise AssertionError('missing size overflow')
    print('MULTIPLICATION_OWNER_NATIVE_OK')


main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_multiplication_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, "MULTIPLICATION_OWNER_NATIVE_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""
