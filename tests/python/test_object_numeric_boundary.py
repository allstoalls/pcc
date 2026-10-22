"""Ordinary numeric annotations use actual object protocols at owning sinks."""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from tests.python.test_boolean_float_conversion import (
    functions,
)
from tests.python.test_floor_division_runtime_contract import (
    FloorMemory,
    RUNTIME,
)
from tests.python.test_shared_call_binding import (
    _emit as emit_binding,
)
from tests.python.test_slot_call_division_producers import (
    assert_division_publication,
)
from tests.python.test_slot_call_floor_division_producers import (
    assert_floor_division_publication,
)
from tests.python.test_slot_call_modulo_producers import (
    assert_modulo_publication,
)
from tests.python.test_slot_call_operand_roots import (
    _emit as emit_operand,
)


from tests.python.test_tuple_subclass_slot_models import SelectionMemory


SOURCE = Path(__file__).parents[2] / 'pcc/runtime/py/py_obj_ops_mod.py'
CHECKS = {'%': assert_modulo_publication, '/': assert_division_publication, '//': assert_floor_division_publication}


@pytest.mark.parametrize('operator', ('%', '/', '//'))
@pytest.mark.parametrize('annotation', ('int', 'float', 'bool'))
@pytest.mark.parametrize('shape,count', [('left OP right', 1), ('left OP (right OP left)', 2)])
def test_ordinary_numeric_operand_projection_uses_runtime_kind(operator, annotation, shape, count):
    source = 'def probe(left: ' + annotation + ', right: ' + annotation + '):\n    return slot_operand_probe(' + shape.replace('OP', operator) + ')\n'
    CHECKS[operator](emit_operand(source), count)


@pytest.mark.parametrize('operator', ('%', '/', '//'))
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
def test_ordinary_numeric_consumer_owner_and_error_paths(operator, site):
    prefix = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    expression = 'left ' + operator + ' right'
    if site == 'default':
        body = '    def target(value=' + expression + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + expression + ', later=fail())\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    CHECKS[operator](emit_binding(prefix + 'def probe(left: int, right: int) -> object:\n' + body))


def test_original_alignment_modulo_shape_keeps_full_integer_value():
    text = emit_binding('def padding(size: int):\n    chunks = []\n    chunks.append(b"\\0" * ((-size) % 8))\n    return chunks\n')
    assert_modulo_publication(text)


def test_original_arm64_shift_count_division_shape_has_an_owner():
    text = emit_binding('def encode(shift: int, imm16: int, rd: int):\n    return ((shift // 16) << 21) | (imm16 << 5) | rd\n')
    assert_floor_division_publication(text)


class ModuloProtocolMemory(SelectionMemory):
    """Real named wrapper and binary-selection bodies over stable host values.

    Reuse the slot model's relocation/lease checks; only the final Python
    callback invocation is modeled. This does not claim native execution.
    """
    def __init__(self, numeric, names=('__mod__', '__rmod__')):
        super().__init__('callback')
        self.numeric = numeric
        self.names = names
        self.classes = {}
        self.maps.update(pcc_named_protocol_borrowed_map=-3,
                         pcc_named_protocol_owned_map=7)
        tree = ast.parse((RUNTIME / 'py_protocol_runtime.py').read_text())
        body = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and (
                node.name.startswith('_named_') or node.name in {
                    '_write_handled', '_protocol_unary_pin_result',
                    'py_user_binop_dispatch',
                }
            ):
                node.decorator_list = []
                body.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith('_NAMED_')
                for target in node.targets
            ):
                body.append(node)
        exec(compile(ast.Module(body=body, type_ignores=[]),
                     str(RUNTIME / 'py_protocol_runtime.py'), 'exec'), self.ns)
        self.ns['py_raise_binary_type_error'] = self.binary_type_error

    def binary_type_error(self, prefix, left, right):
        # The runtime names both operand types, as CPython's binop_type_error.
        self.error = (3, prefix + ": '" + type(left.value).__name__
                      + "' and '" + type(right.value).__name__ + "'")
        return None

    def class_for(self, cls):
        if cls in self.classes:
            return self.classes[cls]
        parents = [self.class_for(parent) for parent in cls.__mro__[1:]
                   if parent is not object]
        result = self.klass(cls.__name__, parents)
        self.classes[cls] = result
        for name in self.names:
            method = cls.__dict__.get(name)
            if method is not None:
                self.install(result, name, self.method(name, method))
        return result

    def operand(self, value):
        result = self.make_instance(self.class_for(type(value)))
        result.value = value
        return result

    def call_slots(self, callable_slot, args_slot, kwargs_slot, result_slot):
        function = self.read(callable_slot, 0)
        arguments = self.read(args_slot, 0)
        values = [self.read(arguments, 24 + index * 8)
                  for index in range(self.read(arguments, 16))]
        self.collect('callback')
        assert function.alive and function.leases > 0
        assert all(value.alive and value.leases > 0 for value in values)
        result = self.numeric.callback(function.value, *(value.value for value in values))
        if self.numeric.pending is not None:
            self.error = self.numeric.pending
            return -1
        if result is NotImplemented:
            self.retain(self.not_implemented)
            result = self.not_implemented
        else:
            # Alias returns are NEW owners of the same model object, not a
            # fresh wrapper that could conceal independent owner cleanup.
            alias = next((value for value in values if value.value is result), None)
            if alias is not None:
                self.retain(alias)
                result = alias
            else:
                result = self.make(result, self.ns['PY_TYPE_INSTANCE'])
                result.fields[12] = 0
        self.write(result_slot, 0, result)
        return 0

    def dispatch(self, left, right, name, reflected, message):
        first, second = self.operand(left), self.operand(right)
        caller = self.roots_for(first, second)
        result = self.ns['py_user_binop_dispatch'](
            self.read(caller, 0), self.read(caller, 8), name, reflected, message,
        )
        self.numeric.pending = self.error
        assert not self.frame_handles and self.depth == 0
        assert all(obj.leases == 0 for obj in self.objects
                   if obj.alive and obj not in (self.none, self.not_implemented))
        return None if result is None else result.value


class ModuloMemory(FloorMemory):
    def __init__(self):
        super().__init__()
        def fmod(a, b):
            try:
                return math.fmod(a, b)
            except ValueError:
                return math.nan
        self.environment.update(
            fmod_c=fmod,
            f64_signbit=lambda value: int(math.copysign(1.0, value) < 0),
            pcc_numeric_float_from_bits=lambda bits: -0.0 if bits == -(1 << 63) else pytest.fail('unexpected bits'),
            py_err_occurred=lambda: int(self.pending is not None),
        )
        functions(RUNTIME / 'py_int_ops.py', {'py_int_mod'}, self.environment)
        functions(SOURCE, {'py_obj_mod'}, self.environment)
        # FloorMemory's old raw-method environment does not supply today's
        # named slot-dispatch owner closure. Execute that real closure in the
        # established slot model and retain the original callback assertions.
        self.environment['py_user_binop_dispatch'] = self.user_binop

    def user_binop(self, left, right, name, reflected, message):
        return ModuloProtocolMemory(self).dispatch(left, right, name, reflected, message)

    def modulo(self, left, right):
        def box(value):
            if type(value) is bool:
                return self.true if value else self.false
            if type(value) is float:
                return self.float(value)
            if type(value) is int and abs(value) >= (1 << 62):
                return {'tag': self.abi['PY_TYPE_INT'], 'size': 48, 'number': value}
            return value
        return self.environment['py_obj_mod'](box(left), box(right))


@pytest.mark.parametrize('left,right', [
    (7, 3), (-7, 3), (7, -3), (True, 3), (False, True),
    ((1 << 210) + 9, 7), (-(1 << 210), 7),
    (7.0, 3), (-7.0, 3), (7.0, -3), (-7.0, -3),
    (True, 2.5), (2.5, True), (False, -2.5),
    (0.0, -2.0), (-0.0, 2.0), (6.0, -3.0), (-6.0, 3.0),
    (1.0, 0.1), (math.inf, 3.0), (-math.inf, 3.0),
    (1.0, math.inf), (-1.0, math.inf), (math.nan, 2.0), (2.0, math.nan),
])
def test_actual_modulo_body_matches_integer_and_float_kind_sign_and_zero(left, right):
    memory = ModuloMemory()
    result = memory.modulo(left, right)
    expected = left % right
    assert type(result) is type(expected)
    if type(expected) is float and math.isnan(expected):
        assert math.isnan(result)
    else:
        assert result == expected
        if type(expected) is float and expected == 0.0:
            assert math.copysign(1.0, result) == math.copysign(1.0, expected)
    assert memory.pending is None and not memory.live_scratch
    assert memory.events[-1][0] == 'new'


@pytest.mark.parametrize('zero', (0, False, 0.0, -0.0))
def test_float_modulo_zero_sets_the_actual_runtime_exception(zero):
    memory = ModuloMemory()
    assert memory.modulo(1.0, zero) is None
    # CPython 3.14+: every zero divisor reads "division by zero".
    assert memory.pending == (9, 'division by zero')
    assert not memory.results


@pytest.mark.parametrize('pending', (None, 'existing-exception'))
def test_float_result_allocation_failure_preserves_pending_error(pending):
    memory = ModuloMemory()
    def fail(value):
        memory.pending = pending
        return None
    memory.environment['py_float_from_f64'] = fail
    assert memory.modulo(7.0, 3.0) is None
    assert memory.pending == (pending if pending is not None else (19, 'float modulo result allocation failed'))


def test_modulo_reflection_and_callback_failure_use_the_existing_protocol():
    marker = object()
    class Left:
        def __mod__(self, other):
            return NotImplemented
    class Right:
        def __rmod__(self, other):
            return marker
    memory = ModuloMemory()
    assert memory.modulo(Left(), Right()) is marker
    assert [event for event in memory.events if event[0] == 'callback'] == [('callback', '__mod__'), ('callback', '__rmod__')]
    error = KeyError('operator')
    class Raising:
        def __mod__(self, other):
            raise error
    memory = ModuloMemory()
    assert memory.modulo(Raising(), Right()) is None
    assert memory.pending is error


@pytest.mark.parametrize('alias', ('left', 'right', 'new'))
def test_modulo_named_slot_dispatch_preserves_callback_result_identity(alias):
    marker = object()
    class Left:
        def __mod__(self, other):
            return NotImplemented
    class Right:
        def __rmod__(self, other):
            return {'left': other, 'right': self, 'new': marker}[alias]
    left, right = Left(), Right()
    memory = ModuloMemory()
    assert memory.modulo(left, right) is {'left': left, 'right': right, 'new': marker}[alias]
    assert memory.pending is None
    assert [event for event in memory.events if event[0] == 'callback'] == [
        ('callback', '__mod__'), ('callback', '__rmod__'),
    ]


def test_modulo_named_slot_dispatch_reflected_subclass_precedes_forward():
    class Left:
        def __mod__(self, other):
            pytest.fail('subclass reflection must be tried first')
    marker = object()
    class Right(Left):
        def __rmod__(self, other):
            return marker
    memory = ModuloMemory()
    assert memory.modulo(Left(), Right()) is marker
    assert memory.events == [('callback', '__rmod__')]
    assert memory.pending is None


def test_modulo_named_slot_dispatch_skips_same_type_reflection():
    class Operand:
        def __mod__(self, other):
            return NotImplemented
        def __rmod__(self, other):
            pytest.fail('same-type reflected method must not be tried')
    memory = ModuloMemory()
    assert memory.modulo(Operand(), Operand()) is None
    assert memory.pending == (3, "unsupported operand type(s) for %: 'Operand' and 'Operand'")
    assert [event for event in memory.events if event[0] == 'callback'] == [('callback', '__mod__')]


class FloorDispatchMemory(FloorMemory):
    """FloorMemory's numeric environment with today's named slot dispatch.

    The floor contract's user-operand cases moved here from
    test_floor_division_runtime_contract: its environment modeled the
    replaced raw-method dispatch, while the real named closure needs the
    slot model below.
    """
    def __init__(self):
        super().__init__()
        self.environment['py_user_binop_dispatch'] = self.user_binop

    def user_binop(self, left, right, name, reflected, message):
        protocol = ModuloProtocolMemory(self, ('__floordiv__', '__rfloordiv__'))
        return protocol.dispatch(left, right, name, reflected, message)


@pytest.mark.parametrize('alias', ('left', 'right', 'new'))
def test_floor_named_slot_dispatch_preserves_callback_result_identity(alias):
    marker = object()
    class Left:
        def __floordiv__(self, other):
            return NotImplemented
    class Right:
        def __rfloordiv__(self, other):
            return {'left': other, 'right': self, 'new': marker}[alias]
    left, right = Left(), Right()
    memory = FloorDispatchMemory()
    assert memory.floor(left, right) is {'left': left, 'right': right, 'new': marker}[alias]
    assert memory.pending is None
    assert [event for event in memory.events if event[0] == 'callback'] == [
        ('callback', '__floordiv__'), ('callback', '__rfloordiv__'),
    ]


def test_floor_named_slot_dispatch_reflected_subclass_and_same_type_skip():
    class Left:
        def __floordiv__(self, other):
            return NotImplemented
        def __rfloordiv__(self, other):
            return 'base'
    class Right(Left):
        def __rfloordiv__(self, other):
            return 'derived'
    memory = FloorDispatchMemory()
    assert memory.floor(Left(), Right()) == 'derived'
    assert [event for event in memory.events if event[0] == 'callback'] == [('callback', '__rfloordiv__')]
    memory = FloorDispatchMemory()
    assert memory.floor(Left(), Left()) is None
    assert memory.pending == (3, "unsupported operand type(s) for //: 'Left' and 'Left'")
    assert [event for event in memory.events if event[0] == 'callback'] == [('callback', '__floordiv__')]


def test_floor_named_slot_dispatch_callback_error_stops_reflection():
    class Left:
        def __floordiv__(self, other):
            raise KeyError('operator')
    class Right:
        def __rfloordiv__(self, other):
            pytest.fail('callback error must stop dispatch')
    memory = FloorDispatchMemory()
    assert memory.floor(Left(), Right()) is None
    assert isinstance(memory.pending, KeyError)
    assert memory.pending.args == ('operator',)
