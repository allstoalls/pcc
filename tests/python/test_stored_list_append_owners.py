"""Execute stored append's actual runtime bodies with relocation and failures."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_bitwise_result_owners import BitwiseMemory
from test_percent_format_owners import ABI, PercentMemory


SOURCE = Path(__file__).parents[2] / 'pcc/runtime/py/py_obj_ops_dispatch.py'


class BoundListMemory(BitwiseMemory):
    CONSTANT_PREFIX = '_BOUND_LIST_'
    FUNCTION_PREFIXES = ('_bound_list_', '_py_list_append_bound')
    OWNED_SLOTS = 6

    def __init__(self, **kwargs):
        self.frames_in_order = []
        self.globals = {}
        self.locked = False
        self.update_count = 0
        self.item_count = 0
        PercentMemory.__init__(self, SOURCE, **kwargs)
        value = self.new(ABI['PY_TYPE_NONE'], None, 'singleton')
        self.object(value)['pinned'] = True
        self.globals['py_None'] = value

    def install(self):
        nodes = []
        names = []
        for node in ast.parse(self.source.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith(self.CONSTANT_PREFIX)
                for target in node.targets
            ):
                nodes.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name.startswith(self.FUNCTION_PREFIXES):
                node.decorator_list = []
                nodes.append(node)
                names.append(node.name)
        exec(compile(ast.Module(nodes, []), str(self.source), 'exec'), self.env)
        for name in names:
            function = self.env[name]
            def entry(*args, _function=function):
                self.park()
                return _function(*args)
            self.env[name] = entry

    def environment(self):
        env = super().environment()
        def tuple_new(count):
            self.park()
            value = self.new(ABI['PY_TYPE_TUPLE'], None, 'captures', pending=True)
            if value is not None:
                self.object(value)['children'] = [None] * count
            return value
        def function(entry, captures, name, receiver):
            self.park()
            return self.new(ABI['PY_TYPE_FUNC'], entry, 'method',
                            (captures, receiver), pending=True)
        def append(receiver, item):
            self.park()
            if self.failure == 'append':
                self.pending = 'append-error'
                return
            self.object(item)['refs'] += 1
            self.object(receiver)['children'].append(item.identity)
        env.update(global_addr=lambda name: -2 if 'borrowed' in name else self.OWNED_SLOTS,
                   py_tuple_new=tuple_new, py_func_new_bound=function,
                   py_list_append=append)
        return env


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'captures', 'method', 'store-0'))
def test_stored_append_constructor_owns_receiver_and_captures(relocate, failure):
    model = BoundListMemory(relocate=relocate, failure=failure)
    receiver = model.new(ABI['PY_TYPE_LIST'], [])
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_list_append_bound'](receiver)
    if failure is None:
        function = model.object(result)
        captures = model.object(model.current(function['children'][0]))
        assert function['children'][1] == receiver.identity
        assert captures['children'] == [receiver.identity]
        assert model.pending is None
    else:
        assert result is None
        assert model.pending == ('store-error' if failure == 'store-0' else failure + '-error')
    model.check(result, baseline)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', (None, 'append', 'item-1', 'item-2'))
def test_stored_append_call_retains_item_and_cleans_temporaries(relocate, failure):
    model = BoundListMemory(relocate=relocate, failure=failure)
    receiver = model.new(ABI['PY_TYPE_LIST'], [])
    item = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    captures = model.new(ABI['PY_TYPE_TUPLE'], None, children=[receiver])
    args = model.new(ABI['PY_TYPE_TUPLE'], None, children=[item])
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_list_append_bound_entry'](captures, args)
    if failure is None:
        assert result.identity == model.globals['py_None'].identity
        assert model.objects[receiver.identity]['children'] == [item.identity]
        baseline[item.identity] += 1
        assert model.pending is None
    else:
        assert result is None
        assert model.pending == ('append-error' if failure == 'append' else 'item-error')
        assert model.objects[receiver.identity]['children'] == []
    model.check(result, baseline)


@pytest.mark.parametrize('count', (0, 2))
def test_stored_append_rejects_wrong_arity_without_mutation(count):
    model = BoundListMemory()
    receiver = model.new(ABI['PY_TYPE_LIST'], [])
    item = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    captures = model.new(ABI['PY_TYPE_TUPLE'], None, children=[receiver])
    args = model.new(ABI['PY_TYPE_TUPLE'], None, children=[item] * count)
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_list_append_bound_entry'](captures, args)
    assert result is None and model.pending == 'error-3'
    assert model.objects[receiver.identity]['children'] == []
    model.check(result, baseline)


@pytest.mark.parametrize('copy_failure,lease_failure', (
    (1, None), (2, None), (None, 'input'), (None, 'captures'), (None, 'method'),
))
def test_stored_append_constructor_partial_owners_are_balanced(copy_failure, lease_failure):
    model = BoundListMemory(copy_failure=copy_failure, lease_failure=lease_failure)
    receiver = model.new(ABI['PY_TYPE_LIST'], [])
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['_py_list_append_bound'](receiver)
    assert result is None
    assert model.pending == ('copy-error' if copy_failure else 'lease-error')
    model.check(result, baseline)
