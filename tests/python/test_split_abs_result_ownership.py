"""Split and abs publish owned results before fallible cleanup or consumers."""
import ast
from pathlib import Path
import re

import pytest

from pcc.frontends.python.codegen import numeric_builtin_lowering
from tests.python.test_bytes_decode_result_roots import physical_body
from tests.python.test_lambda_adapter_scope_lifetime import check_frames
from tests.python.test_lambda_constructor_roots import emit, immediate_store


@pytest.mark.parametrize('parameter, expression, runtime', [
    ('raw: bytes', 'raw.split(b"," )', 'py_bytes_split'),
    ('raw: bytearray', 'raw.split(b",", 1)', 'py_bytes_split_max'),
    ('raw: bytes', 'raw.split(make(b","), make(1))', 'py_bytes_split_max'),
    ('raw: bytes', 'make_bytes(raw).split(b",", fail())', 'py_bytes_split_max'),
    ('raw: bytes', 'raw.split(b"\\0", 1)[0].decode("utf-8", "surrogateescape")', 'py_bytes_split_max'),
    ('raw', 'abs(raw)', 'py_obj_abs'),
    ('raw: int', 'abs(raw)', 'py_obj_abs'),
    ('raw', 'abs(make(raw))', 'py_obj_abs'),
    ('raw', 'abs(raw) // abs(make(raw))', 'py_obj_abs'),
])
@pytest.mark.parametrize('context', ['argument', 'default', 'tuple', 'return'])
def test_split_abs_result_is_published_and_inputs_are_leased(parameter, expression, runtime, context):
    if context == 'argument':
        tail = '    return take(value=' + expression + ')\n'
    elif context == 'default':
        tail = '    def kept(value=' + expression + '):\n        return value\n    return kept\n'
    elif context == 'tuple':
        tail = '    return take(value=(' + expression + ',))\n'
    else:
        tail = '    return ' + expression + '\n'
    text = emit('def take(*, value, **extras):\n    return value\ndef make(value):\n    return value\ndef make_bytes(value: bytes) -> bytes:\n    return value\ndef fail():\n    raise ValueError("later argument")\ndef probe(' + parameter + '):\n' + tail)
    assert immediate_store(text, runtime) >= 1
    body = physical_body(next(body for body in re.findall(r'^define[^\n]*\{\n.*?^}', text, re.M | re.S)
                              if '@user_lambda_owned_probe(' in body.splitlines()[0]))
    calls = list(re.finditer(r'call ptr[^\n]*@' + runtime + r'\(([^\n]*)\)', body))
    assert calls
    for call in calls:
        arguments = re.findall(r'ptr (%[-\w.]+)', call.group(1))
        assert len(arguments) == (3 if runtime.endswith('_max') else 2 if runtime == 'py_bytes_split' else 1)
        for argument in arguments:
            loaded = re.search(re.escape(argument) + r' = load ptr, ptr (%[-\w.]+)', body[:call.start()])
            assert loaded is not None
            slot = loaded.group(1)
            assert re.search(r'@pcc_gc_foreign_lease_acquire\(ptr ' + re.escape(slot) + r'\)', body[:call.start()])
            assert re.search(r'@pcc_gc_store_root\(ptr ' + re.escape(slot) + r', ptr null\)', body[call.end():])


@pytest.mark.parametrize('expression', ['abs(value)', 'abs(make(value)) // abs(value)'])
def test_abs_lambda_lifo_frames_balance_on_success_and_error(expression):
    text = emit('def make(value):\n    return value\ndef probe():\n    return lambda value: ' + expression + '\n')
    body = next(body for body in re.findall(r'^define[^\n]*\{\n.*?^}', text, re.M | re.S)
                if re.search(r'@user_lambda_owned__native_lambda_\d+\(', body.splitlines()[0]))
    check_frames(body)


def runtime_body(filename, name, namespace):
    path = Path(numeric_builtin_lowering.__file__).parents[3] / 'runtime/py' / filename
    function = next(node for node in ast.parse(path.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('kind, value, expected', [(1, 10**40, 10**40), (1, -(10**40), 10**40), (2, True, 1), (3, -0.0, 0.0), (3, -3.5, 3.5), (4, -5, 5)])
def test_actual_abs_body_retains_heap_alias_or_returns_new_number(kind, value, expected):
    class Value:
        def __init__(self, kind, value):
            self.kind, self.value, self.refs = kind, value, 1
    argument = Value(kind, value)
    def retain(obj):
        obj.refs += 1
    namespace = {
        'PY_TYPE_INT': 1, 'PY_TYPE_BOOL': 2, 'PY_TYPE_FLOAT': 3, 'PY_TYPE_COMPLEX': 16,
        'PY_TYPE_INSTANCE': 5, 'PY_TYPE_USER_CLASS_START': 100,
        'PYINTOBJECT_SIGN_OFFSET': 24,
        'ptr_is_null': lambda obj: obj is None, 'null': lambda: None,
        'is_tagged_int': lambda obj: obj.kind == 4, '_type_of': lambda obj: obj.kind,
        'py_int_value_i64': lambda obj: obj.value, 'load_i32': lambda obj, offset: -1 if obj.value < 0 else 1,
        'py_int_from_i64': lambda value: Value(1, value), 'py_int_neg': lambda obj: Value(1, -obj.value),
        '_bool_as_i64': lambda obj: int(obj.value), 'py_float_to_f64': lambda obj: obj.value,
        'py_float_from_f64': lambda value: Value(3, value), 'py_incref': retain,
    }
    result = runtime_body('py_obj_ops_compare.py', 'py_obj_abs', namespace)(argument)
    assert result.value == expected
    assert result.refs == (2 if result is argument else 1)
    argument.refs -= 1
    assert result.refs == 1
    if kind == 3 and value == 0:
        import math
        assert math.copysign(1, result.value) == 1


@pytest.mark.parametrize('callback_result', ['alias', 'different', 'error'])
def test_actual_abs_body_preserves_custom_new_reference_contract(callback_result):
    argument, other = object(), object()
    refs = {argument: 1, other: 1}
    errors = []
    def dispatch(value):
        assert value is argument
        if callback_result == 'error':
            errors.append('custom exception')
            return None
        result = argument if callback_result == 'alias' else other
        refs[result] += 1
        return result
    namespace = {
        'PY_TYPE_INT': 1, 'PY_TYPE_BOOL': 2, 'PY_TYPE_FLOAT': 3, 'PY_TYPE_COMPLEX': 16,
        'PY_TYPE_INSTANCE': 5, 'PY_TYPE_USER_CLASS_START': 100,
        'ptr_is_null': lambda obj: obj is None, 'null': lambda: None,
        'is_tagged_int': lambda obj: False, '_type_of': lambda obj: 5,
        'pcc_capi_is_cext_type_tag': lambda kind: False,
        'py_user_abs_dispatch': dispatch, 'py_err_occurred': lambda: bool(errors),
    }
    result = runtime_body('py_obj_ops_compare.py', 'py_obj_abs', namespace)(argument)
    refs[argument] -= 1
    if callback_result == 'error':
        assert result is None and errors == ['custom exception']
    else:
        assert result is (argument if callback_result == 'alias' else other)
        assert refs[result] == (1 if result is argument else 2)


@pytest.mark.parametrize('family', [bytes, bytearray])
@pytest.mark.parametrize('data, separator, limit', [(b'a,,b,', b',', None), (b'a,b,c', b',', 0), (b'a,b,c', b',', 1), (b'a,b,c', b',', -1), (b'', b',', None), (b'abc', b'', 1)])
def test_actual_bytes_split_body_returns_owned_list_and_owned_same_family_parts(family, data, separator, limit):
    class Value:
        def __init__(self, value):
            self.value, self.refs = value, 1
    receiver, sep = Value(family(data)), Value(separator)
    allocated, errors = [], []
    def new(value):
        result = Value(value)
        allocated.append(result)
        return result
    def append(sequence, item):
        item.refs += 1
        sequence.value.append(item)
    def decref(obj):
        obj.refs -= 1
        assert obj.refs >= 0
    namespace = {
        '_bytes_data': lambda obj: (obj.value, 0), 'py_bytes_len': lambda obj: len(obj.value),
        'ptr_is_null': lambda obj: obj is None, 'null': lambda: None,
        'load_i8': lambda ptr, offset: ptr[0][ptr[1] + offset],
        'ptr_add': lambda ptr, offset: (ptr[0], ptr[1] + offset),
        'py_list_new': lambda capacity: new([]), 'py_list_append': append, 'py_decref': decref,
        '_bytes_new_same_family': lambda src, ptr, size: new(type(src.value)(ptr[0][ptr[1]:ptr[1] + size])),
        'py_int_value_i64': lambda value: value,
        'cstr': lambda text: text, 'py_exc_new': lambda tag, message: (tag, message),
        'py_raise_owned': lambda exc: errors.append(exc),
    }
    name = 'py_bytes_split' if limit is None else 'py_bytes_split_max'
    arguments = (receiver, sep) if limit is None else (receiver, sep, limit)
    result = runtime_body('py_obj_stubs.py', name, namespace)(*arguments)
    assert receiver.refs == sep.refs == 1
    if not separator:
        assert result is None and errors == [(2, 'empty separator')] and allocated == []
        return
    expected = family(data).split(separator) if limit is None else family(data).split(separator, limit)
    assert [part.value for part in result.value] == expected
    assert all(type(part.value) is family for part in result.value)
    assert result.refs == 1 and all(part.refs == 1 for part in result.value)
    for part in result.value:
        decref(part)
    decref(result)
    assert all(obj.refs == 0 for obj in allocated)


@pytest.mark.parametrize('sink', [False, True])
@pytest.mark.parametrize('alias', [False, True])
def test_real_abs_lowerer_reloads_result_after_moving_operand_disposal(sink, alias):
    from pcc.frontends.python.codegen.numeric_builtin_lowering import NumericBuiltinLoweringMixin
    from pcc.frontends.python.py_ast import Call, DynType, Name
    from tests.python.test_dict_constructor_slot_roots import _MovingBoundaryModel

    class Value:
        def __abs__(self):
            return self if alias else 10**40

    class Model(_MovingBoundaryModel, NumericBuiltinLoweringMixin):
        def _slot_call_runtime_call(self, runtime, roots, result_slot=None, **kwargs):
            assert runtime == 'py_obj_abs'
            assert all(root in self.roots for root in roots)
            self.boundary(runtime)
            self._publish_slot_call_owned(result_slot, abs(roots[0].current))

        def _guard_cpy_value_not_null(self, current):
            assert current[0] is not None and current[1] == self.generation

    value = Value()
    model = Model({'value': value}, sink)
    name = lambda ident: Name(span=None, ty=DynType(name='dyn'), ident=ident)
    expression = Call(span=None, ty=DynType(name='dyn'), func=name('abs'), args=(name('value'),), kwargs=())
    result, generation = model._emit_abs_builtin(expression)
    assert generation == model.generation
    assert model.roots == ([model.sink] if sink else [])
    assert result is value if alias else result == 10**40
    assert model.events.index('py_obj_abs') < model.events.index('dispose:abs.operand')
    assert model._try_err_block is model._cpy_operand_cleanup_block is None


@pytest.mark.parametrize('annotation, expression, callee', [
    ('float', 'abs(value)', 'llvm.fabs.f64'),
    ('bool', 'abs(value)', None),
    ('int', 'float(abs(value))', 'py_obj_abs'),
    ('', 'float(abs(value))', 'py_obj_abs'),
])
def test_abs_scalar_consumers_preserve_the_selected_numeric_lane(annotation, expression, callee):
    parameter = 'value' + (': ' + annotation if annotation else '')
    text = emit('def probe(' + parameter + '):\n    return ' + expression + '\n')
    if callee == 'py_obj_abs':
        assert immediate_store(text, callee) == 1
    elif callee:
        assert re.search(r'call double[^\n]*@llvm.fabs.f64\(', text)
    else:
        assert not re.search(r'call ptr[^\n]*@py_obj_abs\(', text)
