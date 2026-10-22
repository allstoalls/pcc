"""Execute the existing floor ABI, including its raw bigint scratch lifetime.

The user-operand cases run the real named slot dispatch in
test_object_numeric_boundary (FloorDispatchMemory); the integration test runs
the whole protocol natively against CPython on five collectors.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.test_boolean_float_conversion import (
    NumericMemory,
    functions,
)


RUNTIME = Path(__file__).parents[2] / 'pcc/runtime/py'


class FloorMemory(NumericMemory):
    def __init__(self):
        super().__init__()
        self.events = []
        self.live_scratch = set()
        self.results = []
        def fmod(a, b):
            try:
                return math.fmod(a, b)
            except ValueError:
                return math.nan
        number = self.environment['load_f64']
        self.environment.update(
            # The float quotient runs the real _float_div_mod over a modeled
            # stack buffer; float objects keep their 'number' payload.
            stack_alloc=lambda size: {},
            store_f64=lambda buffer, offset, value: buffer.__setitem__(offset, value),
            load_f64=lambda value, offset: value[offset] if offset in value else number(value, offset),
            fmod_c=fmod,
            f64_div=lambda a, b: a / b,
            f64_signbit=lambda value: int(math.copysign(1.0, value) < 0),
            pcc_numeric_float_from_bits=lambda bits: -0.0 if bits == -(1 << 63) else pytest.fail('unexpected bits'),
            floor_c=lambda value: float(math.floor(value)),
            _both_tagged=lambda a, b: type(a) is int and type(b) is int,
            py_int_from_i64=self.result,
            py_bigint_from_any=lambda value: self.allocate(self.integer(value)),
            malloc=lambda size: self.allocate(None),
            free=self.free,
            load_ptr=lambda slot, offset: slot['value'],
            py_bigint_divmod=self.divmod,
            _wrap_bigint=self.wrap,
            py_float_from_f64=self.result,
            _instance_class=lambda value: type(value) if self.user(value) else None,
            py_class_lookup=lambda cls, name: getattr(cls, name, None),
            _lookup_dunder=lambda value, name: getattr(type(value), name, None),
            py_obj_issubclass=lambda cls, base: int(issubclass(cls, base)),
            _call_binary=self.callback,
            py_raise_binary_type_error=self.binary_type_error,
            py_decref=lambda value: self.events.append(('drop', value)),
            global_load_ptr=lambda name: {
                'py_True': self.true, 'py_False': self.false,
                'py_NotImplemented': NotImplemented,
            }[name],
        )
        functions(RUNTIME / 'py_int_ops.py', {'py_int_floordiv'}, self.environment)
        functions(RUNTIME / 'py_protocol_runtime.py', {'py_user_binop_dispatch', 'py_obj_floordiv', '_float_div_mod'}, self.environment)
        self.environment['_type_of'] = self.type_of

    def binary_type_error(self, prefix, left, right):
        self.pending = (3, prefix + ": '" + type(left).__name__ + "' and '" + type(right).__name__ + "'")
        return None

    def user(self, value):
        return value is not None and type(value) not in (int, float, dict, str)

    def type_of(self, value):
        if self.user(value):
            return self.abi['PY_TYPE_INSTANCE']
        if type(value) is str:
            return -1
        return self.abi['PY_TYPE_INT'] if type(value) is int else value['tag']

    def allocate(self, value):
        raw = {'value': value}
        self.live_scratch.add(id(raw))
        self.events.append(('allocate', value))
        return raw

    def free(self, raw):
        if raw is not None:
            assert id(raw) in self.live_scratch
            self.live_scratch.remove(id(raw))
            self.events.append(('free', raw['value']))

    def divmod(self, a, b, q, r):
        if b['value'] == 0:
            return -1
        quotient, remainder = divmod(a['value'], b['value'])
        q['value'] = self.allocate(quotient)
        r['value'] = self.allocate(remainder)
        return 0

    def result(self, value):
        assert not self.live_scratch, 'managed result created before scratch disposal'
        self.events.append(('new', value))
        self.results.append(value)
        return value

    def wrap(self, raw):
        value = raw['value']
        self.free(raw)
        return self.result(value)

    def callback(self, method, left, right):
        self.events.append(('callback', method.__name__))
        try:
            return method(left, right)
        except Exception as error:
            self.pending = error
            return None

    def floor(self, left, right):
        def box(value):
            if type(value) is bool:
                return self.true if value else self.false
            if type(value) is float:
                return self.float(value)
            if type(value) is int and abs(value) >= (1 << 62):
                return {'tag': self.abi['PY_TYPE_INT'], 'size': 48, 'number': value}
            return value
        return self.environment['py_obj_floordiv'](box(left), box(right))


@pytest.mark.parametrize('left,right', [
    (7, 2), (-7, 2), (7, -2), (-7, -2),
    (True, 2), (False, True), (7, True),
    ((1 << 200) + 5, 3), (-(1 << 200), 3),
    (7.0, 2), (7, 2.0), (-7.0, 2), (True, 2.0),
])
def test_floor_numeric_routes_return_new_after_raw_scratch_cleanup(left, right):
    memory = FloorMemory()
    result = memory.floor(left, right)
    assert result == left // right
    assert type(result) is type(left // right)
    assert memory.pending is None
    assert memory.events[-1] == ('new', result)
    assert not memory.live_scratch


@pytest.mark.parametrize('zero', (0, False, 0.0))
def test_floor_zero_keeps_the_existing_integer_null_and_float_tls_abis(zero):
    memory = FloorMemory()
    assert memory.floor(7, zero) is None
    assert memory.pending == ((9, 'division by zero') if type(zero) is float else None)
    assert not memory.live_scratch


def test_floor_unsupported_operands_keep_type_error():
    memory = FloorMemory()
    assert memory.floor('text', 2) is None
    assert memory.pending == (3, "unsupported operand type(s) for //: 'str' and 'int'")


PROGRAM = r'''
events = []
marker = object()


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__ + ":", error)


class Left:
    def __floordiv__(self, other):
        events.append("left")
        return NotImplemented

    def __repr__(self):
        return "Left()"


class Right:
    def __init__(self, alias):
        self.alias = alias

    def __rfloordiv__(self, other):
        events.append("right")
        return {"left": other, "right": self, "new": marker}[self.alias]

    def __repr__(self):
        return "Right()"


def aliases():
    for alias in ("left", "right", "new"):
        events.clear()
        left, right = Left(), Right(alias)
        result = left // right
        print(alias, result is {"left": left, "right": right, "new": marker}[alias], events)


aliases()


class Base:
    def __floordiv__(self, other):
        events.append("base.floordiv")
        return NotImplemented

    def __rfloordiv__(self, other):
        events.append("base.rfloordiv")
        return "base"


class Derived(Base):
    def __rfloordiv__(self, other):
        events.append("derived.rfloordiv")
        return "derived"


events.clear()
print("subclass first", Base() // Derived(), events)
events.clear()
attempt("same type", lambda: Base() // Base())
print(events)


class Raising:
    def __floordiv__(self, other):
        raise KeyError("operator")


class NeverCalled:
    def __rfloordiv__(self, other):
        events.append("must not run")
        return "wrong"


events.clear()
attempt("callback error", lambda: Raising() // NeverCalled())
print(events)
attempt("str int", lambda: "text" // 2)
attempt("int str", lambda: 2 // "text")
attempt("complex", lambda: 1j // 2)
attempt("float zero", lambda: 7 // 0.0)
attempt("int zero", lambda: 7 // 0)
attempt("float floor", lambda: 7.5 // 2)
attempt("negative float", lambda: -7.5 // 2)
attempt("big", lambda: (1 << 200) // 3)
values = [7, 0.0]
attempt("dyn zero", lambda: values[0] // values[1])
'''


@pytest.mark.integration
def test_floor_division_protocol_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "floor_protocol.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "floor_protocol"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
