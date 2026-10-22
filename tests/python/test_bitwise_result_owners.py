"""Execute actual generic bitwise owner bodies with moving roots and failures."""
from __future__ import annotations

import ast
import operator
from pathlib import Path

import pytest

from test_percent_format_owners import (
    ABI,
    Handle,
    PercentMemory,
)


SOURCE = Path(__file__).parents[2] / 'pcc/runtime/py/py_obj_ops_dispatch.py'
OPERATORS = (operator.and_, operator.or_, operator.xor)


class BitwiseMemory(PercentMemory):
    def __init__(self, **kwargs):
        self.frames_in_order = []
        self.globals = {}
        self.locked = False
        self.update_count = 0
        self.item_count = 0
        super().__init__(SOURCE, **kwargs)
        for value, name in ((False, 'py_False'), (True, 'py_True')):
            handle = self.new(ABI['PY_TYPE_BOOL'], value, 'singleton')
            self.object(handle)['pinned'] = True
            self.globals[name] = handle

    def park(self):
        if not self.locked:
            super().park()

    def header(self, value, offset):
        if isinstance(value, Handle) and offset == ABI['PYOBJECTHEADER_FLAGS_OFFSET']:
            return 64 if self.object(value)['pinned'] else 0
        return super().header(value, offset)

    def install(self):
        tree = ast.parse(self.source.read_text())
        nodes = []
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id.startswith('_BITWISE_') for target in node.targets):
                nodes.append(node)
            elif isinstance(node, ast.FunctionDef) and (node.name.startswith('_bitwise_') or node.name in ('_type_of', '_union_operand_len', '_py_obj_bitwise_dispatch', 'py_obj_and', 'py_obj_or', 'py_obj_xor')):
                node.decorator_list = []
                nodes.append(node)
                names.add(node.name)
        exec(compile(ast.Module(nodes, []), str(self.source), 'exec'), self.env)
        for name in names:
            function = self.env[name]
            def entry(*args, _function=function):
                self.park()
                return _function(*args)
            self.env[name] = entry

    def environment(self):
        env = super().environment()
        def enter(count, base):
            self.park()
            self.frames[base] = count
            self.frames_in_order.append(base)
        def leave(base):
            self.park()
            assert self.frames_in_order.pop() == base
            del self.frames[base]
        def pin(value):
            self.object(value)['pinned'] = True
        def incref(value):
            self.park()
            self.object(value)['refs'] += 1
        def binary(a, b, op, tag=ABI['PY_TYPE_INT']):
            self.park()
            return self.new(tag, OPERATORS[op](self.object(a)['data'], self.object(b)['data']), 'result', pending=True)
        def tuple_new(count):
            self.park()
            value = self.new(ABI['PY_TYPE_TUPLE'], None, 'result', pending=True)
            if value is not None:
                self.object(value)['children'] = [None] * count
            return value
        def tuple_get(value, index):
            self.park()
            self.item_count += 1
            if self.failure == 'item-' + str(self.item_count):
                self.pending = 'item-error'
                return None
            child = self.current(self.object(value)['children'][index])
            self.object(child)['refs'] += 1
            self.pending_new = child.identity
            return child
        def tuple_set(value, index, child):
            self.park()
            if self.failure == 'store-' + str(index):
                self.pending = 'store-error'
                return
            obj = self.object(value)
            assert obj['children'][index] is None
            self.object(child)['refs'] += 1
            obj['children'][index] = child.identity
        def update(destination, source):
            self.park()
            self.update_count += 1
            if self.failure == 'update-' + str(self.update_count):
                self.pending = 'update-error'
                return -1
            self.object(self.load(destination))['data'].update(self.object(self.load(source))['data'])
            return 0
        def protocol(a, b, name, reflected, error):
            self.park()
            self.events.append((name.value.decode(), reflected.value.decode()))
            if self.failure == 'callback':
                self.pending = 'callback-error'
                return None
            chosen = a if self.alias else b
            self.object(chosen)['refs'] += 1
            self.pending_new = chosen.identity
            return chosen
        def binary_type_error(prefix, a, b):
            # CPython's binop_type_error: the message names both operand types.
            self.park()
            self.events.append(('type-error', prefix.value.decode(), a.identity, b.identity))
            self.pending = 'error-3'
            return None
        def lock():
            self.park()
            self.locked = True
        def unlock():
            self.locked = False
        env.update({name: value for name, value in ABI.items() if name.startswith('PY_TYPE_')})
        env.update(i64=int, PYOBJECTHEADER_FLAGS_OFFSET=ABI['PYOBJECTHEADER_FLAGS_OFFSET'],
            global_addr=lambda name: -2 if 'borrowed' in name else 5,
            global_load_ptr=lambda name: self.globals.get(name),
            pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
            pcc_gc_pin=pin, pcc_gc_load_ptr=lambda owner, slot: self.load(slot),
            pcc_py_gc_minor_graph_lock=lock, pcc_py_gc_minor_graph_unlock=unlock,
            py_incref=incref,
            pcc_capi_is_cext_type_tag=lambda tag: int(tag == 65536),
            pcc_capi_cext_binary_number=lambda a, b, op: protocol(a, b, type('Name', (), {'value': str(op).encode()})(), type('Name', (), {'value': b'cext'})(), None),
            py_int_and=lambda a, b: binary(a, b, 0),
            py_int_or=lambda a, b: binary(a, b, 1),
            py_int_xor=lambda a, b: binary(a, b, 2),
            py_set_intersection=lambda a, b: binary(a, b, 0, ABI['PY_TYPE_SET']),
            py_set_union=lambda a, b: binary(a, b, 1, ABI['PY_TYPE_SET']),
            py_set_symmetric_difference=lambda a, b: binary(a, b, 2, ABI['PY_TYPE_SET']),
            py_dict_new=lambda: (self.park(), self.new(ABI['PY_TYPE_DICT'], {}, 'result', pending=True))[1],
            py_dict_update_slots=update,
            py_tuple_new=tuple_new, py_tuple_get=tuple_get, py_tuple_set_item=tuple_set,
            py_user_binop_dispatch=protocol,
            py_raise_binary_type_error=binary_type_error)
        return env

    def decref(self, value):
        if value is None:
            return
        obj = self.object(value)
        children = obj['children']
        obj['children'] = [identity for identity in children if identity is not None]
        super().decref(value)
        if obj['refs'] > 0:
            obj['children'] = children
        elif obj['role'] != 'singleton':
            self.pending = 'disposal-error'

    def check(self, result, baseline):
        if result is not None:
            assert self.pending_new == result.identity
            self.pending_new = None
            self.decref(result)
        assert self.pending_new is None
        assert self.frames == {} and self.leases == {} and self.frames_in_order == []
        assert {identity: obj['refs'] for identity, obj in self.objects.items() if identity in baseline} == baseline
        assert all(obj['refs'] == 0 for identity, obj in self.objects.items() if identity not in baseline)
        assert all(obj['pinned'] == (obj['role'] == 'singleton') for obj in self.objects.values())


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('op', range(3))
@pytest.mark.parametrize('values', ((6, 3), (-(1 << 210), (1 << 190) + 9), (True, 3), (True, False)))
def test_actual_bitwise_numeric_dispatch_keeps_kind_and_owner(relocate, op, values):
    model = BitwiseMemory(relocate=relocate)
    args = [model.globals['py_True' if value else 'py_False'] if type(value) is bool else model.new(ABI['PY_TYPE_INT'], value) for value in values]
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](*args, op)
    expected = OPERATORS[op](*values)
    assert model.object(result)['data'] == expected
    assert model.object(result)['tag'] == ABI['PY_TYPE_BOOL' if type(expected) is bool else 'PY_TYPE_INT']
    assert model.pending is None
    model.check(result, baseline)


@pytest.mark.parametrize('op', range(3))
@pytest.mark.parametrize('alias', (False, True))
@pytest.mark.parametrize('kind', ('user', 'cext', 'set'))
def test_actual_dispatch_preserves_callback_and_set_routes(op, alias, kind):
    model = BitwiseMemory(alias=alias)
    tag = ABI['PY_TYPE_INSTANCE'] if kind == 'user' else 65536 if kind == 'cext' else ABI['PY_TYPE_SET']
    a = model.new(tag, {1, 2} if kind == 'set' else 'left')
    b = a if alias else model.new(tag, {2, 3} if kind == 'set' else 'right')
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, op)
    assert model.pending is None
    if kind == 'user':
        stem = ('and', 'or', 'xor')[op]
        assert model.events == [('__' + stem + '__', '__r' + stem + '__')]
    if kind == 'cext':
        assert model.events == [(str((8, 10, 9)[op]), 'cext')]
    model.check(result, baseline)


@pytest.mark.parametrize('failure', (None, 'result', 'update-1', 'update-2'))
@pytest.mark.parametrize('alias', (False, True))
def test_dict_union_output_survives_both_updates_and_pending_errors(failure, alias):
    model = BitwiseMemory(failure=failure)
    a = model.new(ABI['PY_TYPE_DICT'], {'a': 1, 'same': 'left'})
    b = a if alias else model.new(ABI['PY_TYPE_DICT'], {'b': 2, 'same': 'right'})
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 1)
    if failure is None:
        assert model.object(result)['data'] == ({'a': 1, 'same': 'left'} if alias else {'a': 1, 'b': 2, 'same': 'right'})
        assert result.identity != a.identity and result.identity != b.identity
        assert model.pending is None
    else:
        assert result is None and model.pending == ('result-error' if failure == 'result' else 'update-error')
    model.check(result, baseline)


@pytest.mark.parametrize('side', ('left', 'right', 'both'))
@pytest.mark.parametrize('failure', (None, 'result', 'item-1', 'item-2', 'store-0', 'store-1'))
def test_union_tuple_members_are_balanced_across_publication_and_disposal(side, failure):
    model = BitwiseMemory(failure=failure)
    a = model.new(ABI['PY_TYPE_CLASS'], 'a')
    b = model.new(ABI['PY_TYPE_CLASS'], 'b')
    if side == 'left':
        a = model.new(ABI['PY_TYPE_TUPLE'], None, children=[a, b])
    elif side == 'right':
        b = model.new(ABI['PY_TYPE_TUPLE'], None, children=[a, b])
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    expected = list(model.object(a)['children']) if side == 'left' else [a.identity]
    expected += list(model.object(b)['children']) if side == 'right' else [b.identity]
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 1)
    triggered = failure is not None and not (failure.startswith('item-') and side == 'both')
    if triggered:
        assert result is None and model.pending == ('result-error' if failure == 'result' else 'item-error' if failure.startswith('item-') else 'store-error')
    else:
        assert model.object(result)['children'] == expected
        assert model.pending is None
    model.check(result, baseline)


@pytest.mark.parametrize('copy_failure,lease_failure,failure', [(1, None, None), (2, None, None), (None, 'result', None), (None, 'input', None), (None, None, 'callback')])
def test_partial_owners_and_callback_exception_survive_cleanup(copy_failure, lease_failure, failure):
    model = BitwiseMemory(copy_failure=copy_failure, lease_failure=lease_failure, failure=failure)
    tag = ABI['PY_TYPE_INSTANCE'] if failure == 'callback' else ABI['PY_TYPE_INT']
    a, b = model.new(tag, 6), model.new(tag, 3)
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 0)
    assert result is None
    assert model.pending == ('copy-error' if copy_failure else 'lease-error' if lease_failure else 'callback-error')
    model.check(result, baseline)


@pytest.mark.parametrize('op', range(3))
def test_unsupported_actual_kind_preserves_type_error(op):
    model = BitwiseMemory()
    a, b = model.new(ABI['PY_TYPE_FLOAT'], 2.5), model.new(ABI['PY_TYPE_INT'], 1)
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, op)
    assert result is None and model.pending == 'error-3'
    symbol = '&|^'[op]
    assert model.events == [('type-error', 'unsupported operand type(s) for ' + symbol, a.identity, b.identity)]
    model.check(result, baseline)


@pytest.mark.parametrize('failure', (None, 'callback'))
def test_temporary_operand_disposal_preserves_result_or_callback_exception(failure):
    model = BitwiseMemory(failure=failure)
    a = model.new(ABI['PY_TYPE_INSTANCE'], 'left')
    b = model.new(ABI['PY_TYPE_INSTANCE'], 'right')
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    callback = model.env['py_user_binop_dispatch']
    def drop_callers_then_call(left, right, *names):
        model.decref(left)
        model.decref(right)
        return callback(left, right, *names)
    model.env['py_user_binop_dispatch'] = drop_callers_then_call
    baseline[a.identity] = baseline[b.identity] = 0
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 0)
    assert model.pending == (None if failure is None else 'callback-error')
    if result is not None:
        assert result.identity == b.identity
    model.check(result, baseline)


def test_result_alias_preserves_an_existing_external_pin():
    model = BitwiseMemory()
    a = model.new(ABI['PY_TYPE_INSTANCE'], 'left')
    b = model.new(ABI['PY_TYPE_INSTANCE'], 'right')
    model.object(b)['pinned'] = True
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 1)
    assert result.identity == b.identity and model.object(result)['pinned']
    model.object(result)['pinned'] = False
    model.check(result, baseline)


def test_union_member_lease_failure_releases_its_new_owner():
    model = BitwiseMemory(lease_failure='member')
    member = model.new(ABI['PY_TYPE_CLASS'], 'member', 'member')
    a = model.new(ABI['PY_TYPE_TUPLE'], None, children=[member])
    b = model.new(ABI['PY_TYPE_CLASS'], 'right')
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_obj_bitwise_dispatch'](a, b, 1)
    assert result is None and model.pending == 'lease-error'
    model.check(result, baseline)
