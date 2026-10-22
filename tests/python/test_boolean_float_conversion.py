"""Header-only bool singletons participate in floating arithmetic by identity."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
RUNTIME = ROOT / 'pcc/runtime/py'


def functions(path, names, environment):
    body = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            body.append(node)
    assert {node.name for node in body} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(body, [])), str(path), 'exec'), environment)


class NumericMemory:
    def __init__(self):
        self.abi = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in ast.parse((RUNTIME / 'py_abi_constants.py').read_text()).body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)
        }
        self.true = {'tag': self.abi['PY_TYPE_BOOL'], 'size': 16}
        self.false = {'tag': self.abi['PY_TYPE_BOOL'], 'size': 16}
        self.pending = None
        self.reads = []
        self.environment = dict(self.abi,
            ptr_is_null=lambda value: int(value is None),
            is_tagged_int=lambda value: int(type(value) is int),
            untag_int=lambda value: value,
            load_i32=self.load_i32,
            load_f64=lambda value, offset: value['number'],
            py_bigint_to_double=lambda value: float(value['number']),
            ptr_eq=lambda left, right: int(left is right),
            global_load_ptr=lambda name: {'py_True': self.true, 'py_False': self.false}[name],
            py_float_from_f64=lambda number: number,
            py_exc_new=lambda kind, message: (kind, message),
            py_raise_owned=lambda error: setattr(self, 'pending', error),
            cstr=lambda value: value,
            null=lambda: None,
            pcc_capi_is_cext_type_tag=lambda tag: 0,
            pcc_capi_cext_binary_number=lambda *args: pytest.fail('unexpected C-extension route'),
            py_user_binop_dispatch=lambda *args: pytest.fail('unexpected dunder route'),
            py_complex_new=lambda real, imag: complex(real, imag),
            _float_new=lambda value: value,
            pow_c=pow,
            py_bigint_from_any=self.integer,
            py_bigint_pow=pow,
            _wrap_bigint=lambda value: value,
            free=lambda value: None,
            # py_int_pow hands a negative exponent to the real float_pow port;
            # only its overflow and complex-result branches stay stubbed.
            py_err_occurred=lambda: int(self.pending is not None),
            float_to_i64=int,
            py_complex_pow=lambda *args: pytest.fail('unexpected complex power'),
            _float_pow_overflow=lambda: pytest.fail('unexpected float overflow'),
        )
        functions(RUNTIME / 'py_obj_stubs.py', {
            'py_float_to_f64', '_complex_real_part', '_complex_imag_part',
            'py_float_add', 'py_float_sub', 'py_float_mul', 'py_complex_add',
            'py_float_pow', '_f64_is_nan', '_f64_is_inf', '_f64_abs', '_f64_integer_kind',
        }, self.environment)
        functions(RUNTIME / 'py_obj_ops_dispatch.py', {'_type_of', 'py_obj_truediv'}, self.environment)
        functions(RUNTIME / 'py_int_ops.py', {
            '_int_to_double', '_int_shift_sign', 'py_int_truediv', 'py_int_pow',
        }, self.environment)

    def integer(self, value):
        if value is self.true:
            return 1
        if value is self.false:
            return 0
        return value if type(value) is int else value['number']

    def load_i32(self, value, offset):
        self.reads.append((value, offset))
        assert 0 <= offset and offset + 4 <= value['size'], 'read beyond singleton header'
        assert offset == self.abi['PYOBJECTHEADER_TYPE_TAG_OFFSET']
        return value['tag']

    def float(self, value):
        return {'tag': self.abi['PY_TYPE_FLOAT'], 'size': 24, 'number': value}


@pytest.mark.parametrize('value,expected', ((False, 0.0), (True, 1.0)))
def test_boolean_float_conversion_never_reads_a_nonexistent_payload(value, expected):
    memory = NumericMemory()
    singleton = memory.true if value else memory.false
    assert memory.environment['py_float_to_f64'](singleton) == expected
    assert all(offset < 16 for _, offset in memory.reads)


@pytest.mark.parametrize('kind', ('integer', 'float', 'bool'))
def test_dynamic_division_rejects_each_actual_zero_kind(kind):
    memory = NumericMemory()
    divisor = 0 if kind == 'integer' else memory.float(0.0) if kind == 'float' else memory.false
    assert memory.environment['py_obj_truediv'](1, divisor) is None
    assert memory.pending == (9, 'division by zero')


@pytest.mark.parametrize('left,right,expected', ((False, True, 0.0), (True, True, 1.0), (True, 2, 0.5)))
def test_boolean_numeric_operands_keep_value_semantics(left, right, expected):
    memory = NumericMemory()
    first = memory.true if left else memory.false
    second = (memory.true if right else memory.false) if isinstance(right, bool) else right
    assert memory.environment['py_obj_truediv'](first, second) == expected
    assert memory.pending is None


def test_model_layout_matches_the_real_bool_singleton_declarations():
    tree = ast.parse((RUNTIME / 'py_substrate.py').read_text())
    declarations = {
        node.value.args[0].value: node.value.func.id
        for node in tree.body if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        and node.value.func.id.startswith('define_global_') and node.value.args
        and isinstance(node.value.args[0], ast.Constant)
    }
    assert declarations['py_true_storage'] == declarations['py_false_storage'] == 'define_global_header'


@pytest.mark.parametrize('helper', ('_complex_real_part', '_int_to_double'))
@pytest.mark.parametrize('value,expected', ((False, 0.0), (True, 1.0)))
def test_sibling_numeric_conversions_use_the_same_bool_layout(helper, value, expected):
    memory = NumericMemory()
    singleton = memory.true if value else memory.false
    assert memory.environment[helper](singleton) == expected
    assert all(offset < 16 for _, offset in memory.reads)


@pytest.mark.parametrize('helper,operation', (
    ('py_float_add', lambda a, b: a + b),
    ('py_float_sub', lambda a, b: a - b),
    ('py_float_mul', lambda a, b: a * b),
    ('py_complex_add', lambda a, b: complex(a + b)),
))
@pytest.mark.parametrize('value', (False, True))
def test_float_and_complex_consumers_observe_bool_value(helper, operation, value):
    memory = NumericMemory()
    singleton = memory.true if value else memory.false
    assert memory.environment[helper](memory.float(2.5), singleton) == operation(2.5, value)
    assert memory.environment[helper](singleton, memory.float(2.5)) == operation(value, 2.5)


@pytest.mark.parametrize('numerator', (False, True))
@pytest.mark.parametrize('denominator', (False, True))
def test_integer_division_bool_guard_does_not_read_bigint_sign(numerator, denominator):
    memory = NumericMemory()
    left = memory.true if numerator else memory.false
    right = memory.true if denominator else memory.false
    result = memory.environment['py_int_truediv'](left, right)
    assert result == (float(numerator) if denominator else None)
    # Preserve the existing py_int_truediv NULL-on-zero caller contract.
    assert memory.pending is None


@pytest.mark.parametrize('base,exponent,expected', ((False, True, 0), (True, False, 1), (2, False, 1), (True, -1, 1.0)))
def test_integer_power_bool_inputs_never_use_bigint_payload(base, exponent, expected):
    memory = NumericMemory()
    left = (memory.true if base else memory.false) if isinstance(base, bool) else base
    right = (memory.true if exponent else memory.false) if isinstance(exponent, bool) else exponent
    assert memory.environment['py_int_pow'](left, right) == expected
