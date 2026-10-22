"""Existing generic modulo calls publish before their legacy NULL/error check."""
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


def assert_modulo_publication(text, count=1):
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@py_obj_mod\([^\n]*\)\n', text, re.M))
    assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith('store ptr ' + call.group(1) + ','), following
    assert _calls(text, 'pcc_gc_foreign_lease_acquire')
    assert _calls(text, 'pcc_gc_foreign_lease_release')
    assert 'divres_null' in text and 'div.zero' in text
    assert not _calls(text, 'py_int_to_i64_lane')


@pytest.mark.parametrize('expression,count', [
    ('left % right', 1),
    ('left % 0', 1),
    ('left % (1 << 100)', 1),
    ('left % (right % left)', 2),
    ("'name=%s' % right", 1),
    ("b'name=%s' % right", 1),
    ("bytearray(b'name=%s') % right", 1),
    ("'name=%(name)s' % {'name': right}", 1),
])
def test_generic_modulo_and_percent_formats_publish_before_cleanup(expression, count):
    text = emit_operand('def probe(left, right):\n    return slot_operand_probe(' + expression + ')\n')
    assert_modulo_publication(text, count)


@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
def test_generic_modulo_contexts(site):
    source = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    if site == 'default':
        body = '    def target(value=left % right):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return left % right\n'
    elif site == 'later-error':
        body = '    return take(value=left % right, later=fail())\n'
    else:
        body = '    return take(value=left % right)\n'
    assert_modulo_publication(emit_binding(source + 'def probe(left, right):\n' + body))


def test_generic_modulo_original_lambda_shape():
    text = emit_binding('operations = {"%": lambda a, b: a % b if b != 0 else None}\n')
    assert_modulo_publication(text)


@pytest.mark.parametrize('numeric', [
    IntType(name='pcc.i64'),
    IntType(name='pcc.u64', signed=False),
    IntType(name='int', width=32),
    FloatType(name='pcc.f64'),
])
def test_modulo_preserves_explicit_machine_operand_exclusion(numeric):
    module = type_infer.infer_module(parse_and_lift('', 'mod_projection.py', 'mod_projection'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    left = Name(span=None, ty=numeric, ident='left')
    right = Name(span=None, ty=DynType(name='dyn'), ident='right')
    expr = BinOp(span=None, ty=DynType(name='dyn'), op='%', lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


@pytest.mark.parametrize('numeric', (IntType(name='int'), FloatType(name='float')))
def test_ordinary_numeric_modulo_object_boundary_uses_runtime(numeric):
    module = type_infer.infer_module(parse_and_lift('', 'typed_mod.py', 'typed_mod'))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode='on')
    left = Name(span=None, ty=numeric, ident='left')
    right = Name(span=None, ty=numeric, ident='right')
    expr = BinOp(span=None, ty=numeric, op='%', lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) == "py_obj_mod"
    assert codegen._slot_call_binary_runtime(expr) is None


PROGRAM = '''\
"""Native qualification control; run against an explicitly matched runtime."""
import gc


events = []
mapping = {}


class Value:
    def __str__(self):
        gc.collect()
        events.append('str')
        return 'VALUE'

    def __repr__(self):
        gc.collect()
        events.append('repr')
        return 'VALUE'

    def __bytes__(self):
        gc.collect()
        events.append('bytes')
        return b'VALUE'

    def __del__(self):
        events.append('drop')
        gc.collect()


class Mapped:
    def __str__(self):
        mapping.clear()
        gc.collect()
        events.append('mapped')
        return 'MAPPED'

    def __del__(self):
        events.append('mapped-drop')
        gc.collect()


class Raising:
    def __str__(self):
        gc.collect()
        raise KeyError('conversion')

    def __bytes__(self):
        gc.collect()
        raise KeyError('bytes-conversion')


def take(*, value, later=None, **extras):
    gc.collect()
    events.append('take')
    return value


def left():
    events.append('left')
    return '[%s]'


def right():
    events.append('right')
    return Value()


def later():
    events.append('later')
    gc.collect()
    raise ValueError('later')


def returned(pattern, value):
    return pattern % value


def main():
    assert take(value=left() % right()) == '[VALUE]'
    assert events == ['left', 'right', 'str', 'drop', 'take']
    events.clear()
    assert take(value=b'[%b]' % Value()) == b'[VALUE]'
    assert events == ['bytes', 'drop', 'take']
    events.clear()
    assert take(value=bytearray(b'[%b]') % Value()) == bytearray(b'[VALUE]')
    assert events == ['bytes', 'drop', 'take']
    events.clear()
    assert take(value='%r/%a' % (Value(), Value())) == 'VALUE/VALUE'
    assert events.count('repr') == 2 and events.count('drop') == 2
    assert events[-1] == 'take'
    events.clear()
    mapping['item'] = Mapped()
    assert take(value='%(item)s' % mapping) == 'MAPPED'
    assert mapping == {}
    assert events == ['mapped', 'mapped-drop', 'take']
    events.clear()
    value = Value()
    def target(default='[%s]' % value):
        gc.collect()
        return default
    assert target() == '[VALUE]'
    assert returned('[%s]', value) == '[VALUE]'
    assert take(value='%*.*s' % (8, 3, 'abcdef')) == '     abc'
    assert take(value='%d/%c' % (12.5, 65)) == '12/A'
    assert take(value='%x' % (1 << 100)) == '10000000000000000000000000'
    try:
        take(value='%s' % Value(), later=later())
    except ValueError as error:
        assert str(error) == 'later'
    else:
        raise AssertionError('missing later error')
    assert events[-1] == 'later'
    for pattern in ('%s',):
        try:
            take(value=pattern % Raising())
        except KeyError as error:
            assert error.args[0] == 'conversion'
        else:
            raise AssertionError('missing conversion error')
    try:
        take(value=b'%b' % Raising())
    except KeyError as error:
        assert error.args[0] == 'bytes-conversion'
    else:
        raise AssertionError('missing bytes conversion error')
    try:
        take(value='%(missing)s' % {})
    except KeyError as error:
        assert error.args[0] == 'missing'
    else:
        raise AssertionError('missing mapping error')
    print('PERCENT_OWNER_NATIVE_OK')


main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_modulo_producer_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, 'PERCENT_OWNER_NATIVE_OK\n', tmp_path, python_program_compiler, mode,
        explicit_owned_runtime, capfd, provenance_probe="2",
    )
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""
