"""The real allocation bodies share one bounded, reserved-aware CAS owner."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ast
from pathlib import Path
import re
import threading

import pytest

from pcc.runtime.py import py_abi_constants as abi


ROOT = Path(__file__).resolve().parents[2]
CLASS_SOURCE = ROOT / 'pcc/runtime/py/py_class.py'
SUBSTRATE_SOURCE = ROOT / 'pcc/runtime/py/py_substrate.py'
START, LIMIT = abi.PY_TYPE_USER_CLASS_START, abi.PY_TYPE_CEXT_TAG_BASE
# Address of the class name copy py_class_new allocates in these models.
NAME = 8192
RESERVED = {name: value for name, value in vars(abi).items()
            if name.startswith('PY_TYPE_') and isinstance(value, int)
            and START <= value < LIMIT and name != 'PY_TYPE_USER_CLASS_START'}


def _load(path, names, environment):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), environment)


class Counter:
    def __init__(self, value=START, first_reads=0):
        self.value = value
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(first_reads) if first_reads else None
        self.local = threading.local()
        self.failures = 0
        self.environment = {name: value for name, value in vars(abi).items() if name.isupper()}
        self.environment.update(global_addr=lambda name: name,
                                atomic_load_i32=self.load, atomic_cas_i32=self.cas)
        _load(SUBSTRATE_SOURCE, ('py_subs_alloc_user_tag',), self.environment)
        _load(CLASS_SOURCE, ('_alloc_user_tag',), self.environment)

    def load(self, slot, offset, order):
        assert (slot, offset, order) == ('py_next_user_tag', 0, 'relaxed')
        with self.lock:
            value = self.value
        if self.barrier is not None and not getattr(self.local, 'read_once', False):
            self.local.read_once = True
            self.barrier.wait(timeout=3)
        return value

    def cas(self, slot, offset, expected, desired, success, failure):
        assert (slot, offset, success, failure) == ('py_next_user_tag', 0, 'relaxed', 'relaxed')
        with self.lock:
            previous = self.value
            if previous == expected:
                self.value = desired
            else:
                self.failures += 1
            return previous

    def allocate(self, route):
        return self.environment[route]()


@pytest.mark.parametrize('route', ['_alloc_user_tag', 'py_subs_alloc_user_tag'])
def test_reserved_inventory_is_skipped_without_renumbering(route):
    counter = Counter()
    values = [counter.allocate(route) for _ in range(100)]
    expected = [value for value in range(START, START + 100 + len(RESERVED)) if value not in RESERVED.values()][:100]
    assert values == expected
    assert not set(values) & set(RESERVED.values())
    assert values[96] == 201


@pytest.mark.parametrize('route', ['_alloc_user_tag', 'py_subs_alloc_user_tag'])
@pytest.mark.parametrize('start', [START - 1, LIMIT - 1, LIMIT, 2147483647])
def test_invalid_or_exhausted_counter_is_bounded_and_stable(route, start):
    counter = Counter(start)
    first = counter.allocate(route)
    expected = LIMIT - 1 if start == LIMIT - 1 else -1
    assert first == expected
    terminal = LIMIT if start == LIMIT - 1 else start
    assert counter.value == terminal
    assert [counter.allocate(route) for _ in range(3)] == [-1, -1, -1]
    assert counter.value == terminal


def test_two_entry_points_share_atomic_allocation_under_contention():
    workers, per_worker = 8, 32
    counter = Counter(194, first_reads=workers)
    def allocate(index):
        route = '_alloc_user_tag' if index % 2 else 'py_subs_alloc_user_tag'
        return [counter.allocate(route) for _ in range(per_worker)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(allocate, range(workers)))
    values = [value for row in rows for value in row]
    assert len(values) == len(set(values)) == workers * per_worker
    assert not set(values) & set(RESERVED.values())
    assert sorted(values) == [value for value in range(194, counter.value) if value not in RESERVED.values()]
    assert counter.failures >= workers - 1


def test_both_helpers_have_exactly_one_counter_writer():
    substrate = ast.parse(SUBSTRATE_SOURCE.read_text())
    owner = next(node for node in substrate.body if isinstance(node, ast.FunctionDef) and node.name == 'py_subs_alloc_user_tag')
    calls = [node.func.id for node in ast.walk(owner) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert calls.count('atomic_cas_i32') == 1
    assert 'store_i32' not in calls and 'atomic_store_i32' not in calls
    tree = ast.parse(CLASS_SOURCE.read_text())
    delegate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_alloc_user_tag')
    delegated_calls = [node.func.id for node in ast.walk(delegate) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert delegated_calls == ['py_subs_alloc_user_tag']


@pytest.mark.parametrize('allocation', ['class', 'tag'])
def test_exhausted_constructor_uses_actual_partial_owner_cleanup(allocation):
    # ``tag``: the class tag space is exhausted after the class object holds
    # its copied name, which py_class_dealloc frees with the dropped class.
    # ``class``: the class object itself cannot be allocated, so the name
    # copy must be freed before returning.
    memory = {}
    events = []
    next_stack = [4096]
    def stack_alloc(size):
        result = next_stack[0]
        next_stack[0] += size + 64
        return result
    def store(base, offset, value):
        memory[base + offset] = value
    def rmw(op, base, offset, value, order):
        assert op == 'and'
        prior = memory.get(base + offset, 0)
        memory[base + offset] = prior & value
        return prior
    def clear_root(slot, value):
        assert value == 0
        events.append(('drop_root', memory.get(slot, 0)))
        memory[slot] = 0
    counter = Counter(LIMIT)
    env = dict(counter.environment)
    env.update(
        null=lambda: 0, ptr_is_null=lambda value: value == 0,
        ptr_add=lambda value, offset: value + offset,
        stack_alloc=stack_alloc, memset=lambda base, value, size: None,
        pcc_gc_frame_enter=lambda frame_map, slots: None,
        pcc_gc_frame_leave=lambda slots: events.append(('leave', slots)),
        pcc_gc_alloc=lambda size, tag, flags: 1024 if allocation == 'tag' else 0,
        py_tuple_new=lambda count: 2048,
        strlen=len,
        malloc=lambda size: events.append(('malloc', size)) or NAME,
        memcpy=lambda target, source, size: events.append(('memcpy', target, source, size)),
        free=lambda address: events.append(('free', address)),
        load_i32=lambda base, offset: memory.get(base + offset, 0),
        load_ptr=lambda base, offset: memory.get(base + offset, 0),
        store_ptr=store, store_i32=store, store_i64=store,
        pcc_gc_load_ptr=lambda owner, slot: memory.get(slot, 0),
        atomic_rmw_i32=rmw, pcc_gc_store_root=clear_root,
        _class_construct_input_roots=lambda bases, count: 0,
        _class_construct_input_leave=lambda roots, count: None,
        cstr=lambda text: text,
        py_runtime_error_if_unset=lambda helper, message: events.append(('error', helper, message)),
    )
    _load(CLASS_SOURCE, ('_class_require_result', '_class_construct_finish', 'py_class_new'), env)
    assert env['py_class_new']('Example', 0, 0, 0, 0) == 0
    assert ('memcpy', NAME, 'Example', len('Example') + 1) in events
    if allocation == 'class':
        assert events.count(('free', NAME)) == 1
        return
    assert ('free', NAME) not in events
    assert memory[1024 + abi.PYCLASSOBJECT_NAME_OFFSET] == NAME
    assert ('error', 'class type tag allocation', 'user class type tag space exhausted') in events
    assert ('drop_root', 1024) in events
    assert not memory[1024 + abi.PYOBJECTHEADER_FLAGS_OFFSET] & abi.PY_FLAG_IMMORTAL
    assert 1024 + abi.PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET not in memory
    assert counter.value == LIMIT


def test_class_dealloc_frees_the_owned_name():
    memory = {1024 + abi.PYCLASSOBJECT_NAME_OFFSET: NAME}
    events = []
    env = {name: value for name, value in vars(abi).items() if name.isupper()}
    env.update(
        null=lambda: 0, ptr_is_null=lambda value: value == 0,
        ptr_add=lambda value, offset: value + offset,
        load_ptr=lambda base, offset: memory.get(base + offset, 0),
        store_ptr=lambda base, offset, value: memory.__setitem__(base + offset, value),
        pcc_gc_load_ptr=lambda owner, slot: memory.get(slot, 0),
        py_decref=lambda value: events.append(('decref', value)),
        free=lambda address: events.append(('free', address)),
        _bump_class_attr_cache_epoch=lambda: None,
        pcc_class_retire_metaclass=lambda value: None,
        pcc_gc_free_object_memory=lambda value: events.append(('object', value)),
    )
    _load(CLASS_SOURCE, ('py_class_dealloc',), env)
    env['py_class_dealloc'](1024)
    assert events.count(('free', NAME)) == 1
    assert events[-1] == ('object', 1024)
