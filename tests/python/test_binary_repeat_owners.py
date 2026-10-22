"""Execute multiplication bodies with moving roots, aliases and disposal."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_percent_format_owners import (
    ABI,
    Field,
    Handle,
    PercentMemory,
)


ROOT = Path(__file__).parents[2]
SOURCE = ROOT / 'pcc/runtime/py/py_binary_repeat_runtime.py'


class RepeatMemory(PercentMemory):
    def __init__(self, *, relocate=True, failure=None, copy_failure=None,
                 lease_failure=None, method=None, index=None, mutation=None,
                 snapshot_failure=1, allocation_failure=None):
        self.method = method
        self.index_callback = index
        self.mutation = mutation
        self.locked = False
        self.snapshot_attempts = 0
        self.snapshot_failure = snapshot_failure
        self.allocation_failure = allocation_failure
        self.allocations = 0
        self.plans = {}
        self.frame_stack = []
        self.barriers = []
        super().__init__(SOURCE, relocate=relocate, failure=failure,
                         copy_failure=copy_failure, lease_failure=lease_failure)
        self.caller = self.alloc(24)
        self.frames[self.caller] = 3

    def install(self):
        nodes = []
        for node in ast.parse(self.source.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith('_MUL_') for t in node.targets):
                nodes.append(node)
            elif isinstance(node, ast.FunctionDef):
                node.decorator_list = []
                nodes.append(node)
        exec(compile(ast.fix_missing_locations(ast.Module(nodes, [])), str(self.source), 'exec'), self.env)
        for node in nodes:
            if isinstance(node, ast.FunctionDef):
                function = self.env[node.name]
                def entry(*args, _function=function):
                    self.park()
                    return _function(*args)
                self.env[node.name] = entry

    def park(self):
        if not self.locked:
            super().park()

    def load(self, base, offset=0):
        if isinstance(base, Field):
            children = self.object(base.owner)['children']
            identity = children[(base.offset + offset - ABI['PYTUPLEOBJECT_ITEMS_OFFSET']) // 8]
            return self.current(identity) if identity is not None else None
        return super().load(base, offset)

    def header(self, value, offset):
        if isinstance(value, Handle):
            obj = self.object(value)
            if offset == ABI['PYOBJECTHEADER_TYPE_TAG_OFFSET']:
                return obj['tag']
            if offset == 16:
                return len(obj['children']) if obj['tag'] in (ABI['PY_TYPE_LIST'], ABI['PY_TYPE_TUPLE']) else len(obj['data']) if isinstance(obj['data'], (bytes, bytearray)) else obj['data']
            if offset == ABI['PYSTROBJECT_CP_LEN_OFFSET'] and obj['tag'] == ABI['PY_TYPE_STR']:
                return obj.get('codepoints', -1)
            raise AssertionError((obj['tag'], offset))
        return self.load(value, offset) or 0

    def store_scalar(self, base, offset, value):
        if isinstance(base, Handle):
            obj = self.object(base)
            assert offset == ABI['PYSTROBJECT_CP_LEN_OFFSET']
            obj['codepoints'] = value
        else:
            self.store(base, offset, value)

    def byte(self, base, offset):
        obj = self.object(base)
        start = ABI['PYSTROBJECT_DATA_OFFSET'] if obj['tag'] == ABI['PY_TYPE_STR'] else ABI['PYBYTESOBJECT_DATA_OFFSET']
        return obj['data'][offset - start]

    def store_byte(self, base, offset, value):
        obj = self.object(base)
        start = ABI['PYSTROBJECT_DATA_OFFSET'] if obj['tag'] == ABI['PY_TYPE_STR'] else ABI['PYBYTESOBJECT_DATA_OFFSET']
        obj['data'][offset - start] = value

    def ptr(self, base, offset):
        if isinstance(base, Handle):
            return Field(base, offset)
        return super().ptr(base, offset)

    def copy(self, destination, source):
        self.park()
        self.copy_count += 1
        if self.copy_count == self.copy_failure:
            self.pending = 'copy-error'
            return -1
        value = self.load(source)
        assert self.load(destination) is None
        if value is None:
            return 0
        self.object(value)['refs'] += 1
        self.store(destination, 0, value)
        assert destination not in self.leases
        self.leases[destination] = 1
        return 1

    def decref(self, value):
        if value is None:
            return
        obj = self.object(value)
        obj['refs'] -= 1
        if obj['refs'] == 0:
            assert not any(isinstance(self.load(slot), Handle) and self.load(slot).identity == value.identity for slot in self.leases)
            self.disposed.append(obj['role'])
            if obj['role'] in ('args', 'item', 'snapshot', 'result'):
                self.pending = 'disposal-error'
            for identity in obj['children']:
                if identity is not None:
                    self.decref(self.current(identity))

    def make(self, value, role='input'):
        if isinstance(value, (list, tuple)):
            children = [child if isinstance(child, Handle) else self.make(child, 'item') for child in value]
            result = self.new(ABI['PY_TYPE_LIST'] if isinstance(value, list) else ABI['PY_TYPE_TUPLE'], None, role, children)
            for original, child in zip(value, children):
                if not isinstance(original, Handle):
                    self.decref(child)
            return result
        if isinstance(value, str):
            return self.new(ABI['PY_TYPE_STR'], bytearray(value.encode()), role)
        if isinstance(value, bytearray):
            return self.new(ABI['PY_TYPE_BYTEARRAY'], bytearray(value), role)
        if isinstance(value, bytes):
            return self.new(ABI['PY_TYPE_BYTES'], bytearray(value), role)
        if isinstance(value, bool):
            return self.new(ABI['PY_TYPE_BOOL'], value, role)
        if isinstance(value, int):
            return self.new(ABI['PY_TYPE_INT'], value, role)
        if isinstance(value, float):
            return self.new(ABI['PY_TYPE_FLOAT'], value, role)
        return self.new(ABI['PY_TYPE_INSTANCE'], value, role)

    def environment(self):
        env = super().environment()
        env.update(ABI)

        def move(destination, source):
            self.park()
            assert self.load(destination) is None
            self.store(destination, 0, self.load(source))
            self.store(source, 0, None)
            if source in self.leases:
                self.leases[destination] = self.leases.pop(source)
            return 0

        def tuple_new(count):
            self.park()
            result = self.new(ABI['PY_TYPE_TUPLE'], None, 'args' if count == 1 and self.method else 'snapshot', pending=True)
            if result is not None:
                self.object(result)['children'] = [None] * count
            return result

        def tuple_set(value, index, child):
            self.park()
            obj = self.object(value)
            assert obj['children'][index] is None
            self.object(child)['refs'] += 1
            obj['children'][index] = child.identity
            if self.failure == 'snapshot-store' and obj['role'] == 'snapshot' and index == 1:
                self.pending = 'snapshot-store-error'

        def list_new(count):
            self.park()
            return self.new(ABI['PY_TYPE_LIST'], None, 'result', pending=True)

        def list_append(value, child):
            self.park()
            obj = self.object(value)
            self.object(child)['refs'] += 1
            obj['children'].append(child.identity)
            if self.failure == 'append':
                self.pending = 'append-error'

        def bytes_new(pointer, count, tag):
            self.park()
            assert pointer is None
            return self.new(tag, bytearray(count), 'result', pending=True)

        def index_checked(slot):
            self.park()
            self.events.append('index')
            obj = self.object(self.load(slot))
            result = self.index_callback(self) if self.index_callback else obj['data']
            if type(result) not in (int, bool):
                self.pending = 'index-type-error'
                return 0
            if not -(1 << 63) <= result < 1 << 63:
                self.pending = 'index-overflow'
                return 0
            return int(result)

        def has_index(value):
            # CPython's _PyIndex_Check: an int, a bool, or a modeled
            # __index__ owner (index_callback).
            data = self.object(value)['data']
            return int(type(data) in (int, bool) or self.index_callback is not None)

        def type_named_error(prefix, value, suffix):
            self.park()
            self.pending = ('type-error', prefix + type(self.object(value)['data']).__name__ + suffix)
            return None

        def generic(left, right):
            self.park()
            self.events.append('generic')
            result = self.object(left)['data'] * self.object(right)['data']
            tag = ABI['PY_TYPE_FLOAT'] if isinstance(result, float) else ABI['PY_TYPE_INT']
            return self.new(tag, result, 'result', pending=True)

        def special(receiver, name, arguments, kwargs, output, handled):
            self.park()
            self.events.append(name)
            self.store(handled, 0, int(self.method is not None))
            if self.method is None:
                return 0
            result = self.method(self)
            if result == 'error':
                self.pending = 'method-error'
                return -1
            if result is NotImplemented:
                result = self.not_implemented
            if isinstance(result, Handle):
                result = self.current(result.identity)
                self.object(result)['refs'] += 1
            else:
                result = self.make(result, 'result')
            self.store(output, 0, result)
            return 0

        def snapshot(source, output, length, plans, tokens):
            self.park()
            self.snapshot_attempts += 1
            before = self.object(self.load(source))
            assert self.frames.get(output) == (length if length else -1)
            borrowed = output + length * 8
            assert self.frames.get(borrowed) == -1
            if self.mutation == 'retry' and self.snapshot_attempts == 1:
                child = self.make(99, 'item')
                before['children'].append(child.identity)
            if len(before['children']) != length:
                return -2
            self.locked = True
            status = 0
            for index, identity in enumerate(before['children']):
                if self.failure == 'snapshot-copy' and index == self.snapshot_failure:
                    status = -1
                    break
                self.objects[identity]['refs'] += 1
                self.store(borrowed, 0, self.current(identity))
                self.store(output, index * 8, self.load(borrowed))
                self.store(borrowed, 0, None)
                self.leases[output + index * 8] = 1
                self.store(tokens, index * 8, 1)
                self.plans[plans + index * 256] = identity
            assert self.load(borrowed) is None
            self.locked = False
            if self.mutation == 'replace':
                caller_error = self.pending
                children = before['children']
                before['children'] = []
                for identity in children:
                    self.decref(self.current(identity))
                # This writer owns another thread's exception state. Its
                # finalizers cannot replace the snapshot caller's TLS.
                self.pending = caller_error
                self.events.append('source-replaced')
            return status

        def finish(plan):
            self.park()
            identity = self.plans.get(plan)
            if identity is not None:
                assert self.objects[identity]['refs'] > 0
                assert any(isinstance(self.load(slot), Handle) and self.load(slot).identity == identity for slot in self.leases)

        def memory_free(value):
            self.park()
            self.events.append('free')

        def memory_alloc(size):
            self.park()
            self.allocations += 1
            if self.allocations == self.allocation_failure:
                return None
            return self.alloc(size)

        def mutable_copy(value):
            self.park()
            return self.new(ABI['PY_TYPE_BYTEARRAY'], bytearray(self.object(value)['data']), 'mutable-result', pending=True)

        def error(kind, message):
            self.park()
            return (kind, message)

        def raise_owned(value):
            self.park()
            self.pending = value

        def barrier(owner, slot, value):
            assert value is self.load(slot)
            if value is not None:
                assert slot in self.leases
            self.barriers.append(slot)

        def enter(frame_map, base):
            self.park()
            count = 8 if frame_map == 8 else self.load(frame_map)
            assert isinstance(count, int)
            self.frames[base] = count
            self.frame_stack.append(base)

        def leave(base):
            self.park()
            assert self.frame_stack.pop() == base
            assert all(self.load(base + offset) is None for offset in range(0, abs(self.frames[base]) * 8, 8))
            del self.frames[base]

        env.update(
            c_ptr=object, null=lambda: None, ptr_add=self.ptr,
            ptr_eq=lambda a, b: int(a == b), is_tagged_int=lambda value: 0,
            load_i32=self.header, load_i64=self.header, load_i8=self.byte,
            store_i32=self.store_scalar, store_i64=self.store_scalar, store_i8=self.store_byte,
            global_addr=lambda name: 8, cstr=lambda value: value,
            global_load_ptr=lambda name: self.current(self.not_implemented.identity),
            pcc_gc_root_copy_lease=self.copy, pcc_gc_root_move=move,
            pcc_gc_foreign_lease_acquire=self.acquire,
            pcc_gc_foreign_lease_release=self.release,
            pcc_gc_store_root=self.drop,
            pcc_gc_note_slot_write_barrier=barrier,
            py_tuple_new=tuple_new, py_tuple_set_item=tuple_set,
            py_list_new=list_new, py_list_append=list_append,
            py_str_new=lambda pointer, count: bytes_new(pointer, count, ABI['PY_TYPE_STR']),
            py_bytes_new=lambda pointer, count: bytes_new(pointer, count, ABI['PY_TYPE_BYTES']),
            py_bytes_from_obj=self.bytes_copy,
            py_bytearray_from_obj=mutable_copy,
            py_index_i64_checked_slots=index_checked,
            py_obj_has_index=has_index, py_raise_type_named_error=type_named_error,
            py_obj_mul=generic, py_obj_special_call_slots=special,
            pcc_capi_is_cext_type_tag=lambda tag: 0,
            pcc_list_snapshot_commit_slots=snapshot,
            pcc_gc_backend=lambda: 4,
            pcc_gc_root_copy_lease_finish=finish,
            pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
            pcc_gc_publish_initialized=lambda value: self.park(),
            malloc=memory_alloc, free=memory_free,
            py_exc_new=error, py_raise_owned=raise_owned,
            pcc_platform_abort=lambda: pytest.fail('unexpected abort'),
        )
        self.not_implemented = self.new(ABI['PY_TYPE_NONE'], None, 'NotImplemented')
        return env

    def run(self, left, right, *, alias=False):
        a = self.make(left, 'left')
        b = a if alias else self.make(right, 'right')
        if alias:
            self.object(a)['refs'] += 1
        self.store(self.caller, 0, a)
        self.store(self.caller, 8, b)
        status = self.env['py_obj_mul_slots'](self.caller, self.caller + 8, self.caller + 16)
        assert not self.leases
        assert self.frames == {self.caller: 3}
        assert self.pending_new is None
        result = self.load(self.caller + 16)
        if status == 0:
            assert result is not None and self.pending is None
        else:
            assert result is None and self.pending is not None
        return status, result

    def value(self, handle):
        obj = self.object(handle)
        if obj['tag'] in (ABI['PY_TYPE_LIST'], ABI['PY_TYPE_TUPLE']):
            data = [self.value(self.current(identity)) for identity in obj['children']]
            return data if obj['tag'] == ABI['PY_TYPE_LIST'] else tuple(data)
        if obj['tag'] == ABI['PY_TYPE_STR']:
            return bytes(obj['data']).decode()
        if obj['tag'] == ABI['PY_TYPE_BYTES']:
            return bytes(obj['data'])
        return obj['data']

    def close(self):
        for offset in (16, 8, 0):
            self.drop(self.caller + offset, None)
        live = {identity: obj['refs'] for identity, obj in self.objects.items() if obj['refs']}
        assert live == {self.not_implemented.identity: 1}, live


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('value', ('abé', b'ab', bytearray(b'ab'), [1, 2], (1, 2)))
@pytest.mark.parametrize('count', (-3, 0, 1, 3))
@pytest.mark.parametrize('reflected', (False, True))
def test_repetition_owns_sources_elements_and_output(value, count, reflected, relocate):
    model = RepeatMemory(relocate=relocate)
    status, result = model.run(count, value) if reflected else model.run(value, count)
    assert status == 0 and model.value(result) == value * count
    if type(value) is tuple and count == 1:
        source = model.load(model.caller + (8 if reflected else 0))
        assert source.identity == result.identity
    model.close()


@pytest.mark.parametrize('left,right', ((2**200, -(2**100)), (1.5, 4), (True, 5), (-3, -7)))
def test_numeric_route_keeps_full_values(left, right):
    model = RepeatMemory()
    status, result = model.run(left, right)
    assert status == 0 and model.value(result) == left * right
    model.close()


@pytest.mark.parametrize('mutation', ('replace', 'retry'))
def test_list_snapshot_has_one_consistent_version(mutation):
    model = RepeatMemory(mutation=mutation)
    status, result = model.run([1, 2], 3)
    expected = [1, 2, 99] if mutation == 'retry' else [1, 2]
    assert status == 0 and model.value(result) == expected * 3
    assert model.snapshot_attempts == (2 if mutation == 'retry' else 1)
    model.close()


@pytest.mark.parametrize('reflected', (False, True))
@pytest.mark.parametrize('answer', (NotImplemented, 17, 'error'))
def test_user_operator_precedes_checked_index_and_preserves_error(answer, reflected):
    model = RepeatMemory(method=lambda _: answer, index=lambda _: 2)
    status, result = model.run(object(), 'ab') if reflected else model.run('ab', object())
    assert ('__mul__' if reflected else '__rmul__') in model.events
    if answer == 'error':
        assert status == -1 and model.pending == 'method-error'
        assert 'index' not in model.events
    else:
        assert status == 0 and model.value(result) == ('abab' if answer is NotImplemented else answer)
        assert ('index' in model.events) == (answer is NotImplemented)
    model.close()


@pytest.mark.parametrize('count', (-(1 << 100), 1 << 100, 1.5))
def test_count_rejection_keeps_input_owners(count):
    model = RepeatMemory()
    status, _ = model.run('ab', count)
    assert status == -1
    # sequence_repeat checks the index protocol before converting the count.
    assert model.pending == (
        ('type-error', "can't multiply sequence by non-int of type 'float'")
        if isinstance(count, float) else 'index-overflow'
    )
    model.close()


@pytest.mark.parametrize('failure', ('snapshot-copy', 'snapshot-store', 'append', 'result'))
def test_partial_failure_cleanup_preserves_original_error(failure):
    model = RepeatMemory(failure=failure)
    status, _ = model.run([1, 2], 3)
    assert status == -1 and model.pending != 'disposal-error'
    model.close()


@pytest.mark.parametrize('failure', (1, 2))
def test_partial_input_copy_cleanup(failure):
    model = RepeatMemory(copy_failure=failure)
    status, _ = model.run('ab', 2)
    assert status == -1 and model.pending == 'copy-error'
    model.close()


@pytest.mark.parametrize('role', ('snapshot', 'result'))
def test_result_publication_precedes_failing_lease(role):
    model = RepeatMemory(lease_failure=role)
    status, _ = model.run([1, 2], 3)
    assert status == -1 and model.pending == 'lease-error'
    model.close()


def test_aliasing_operands_have_independent_owners():
    model = RepeatMemory()
    status, result = model.run(2**80, None, alias=True)
    assert status == 0 and model.value(result) == 2**160
    model.close()


def test_fresh_results_run_the_root_barrier_while_address_leased():
    model = RepeatMemory()
    status, result = model.run([1, 2], 3)
    assert status == 0 and model.value(result) == [1, 2, 1, 2, 1, 2]
    assert len(model.barriers) == 2  # snapshot and output allocations
    model.close()


def test_owned_map_matches_semantic_slot_layout():
    module = ast.parse(SOURCE.read_text())
    count = next(node.value.value for node in module.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_MUL_SLOT_COUNT' for t in node.targets))
    calls = [node for node in module.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == 'define_global_i32']
    assert count == 8 and len(calls) == 1
    assert calls[0].value.args[1].id == '_MUL_SLOT_COUNT'


@pytest.mark.parametrize('position', (0, 1, 2))
@pytest.mark.parametrize('mutate', (None, 'replace'))
def test_snapshot_failure_at_each_position_finishes_all_plans(position, mutate):
    model = RepeatMemory(failure='snapshot-copy', snapshot_failure=position, mutation=mutate)
    status, _ = model.run([1, 2, 3], 2)
    assert status == -1 and model.pending != 'disposal-error'
    assert model.snapshot_attempts == 1
    assert len(model.plans) == position
    model.close()


@pytest.mark.parametrize('allocation', (1, 2, 3))
def test_snapshot_raw_allocation_failure_cleans_unregistered_scratch(allocation):
    model = RepeatMemory(allocation_failure=allocation)
    status, _ = model.run([1, 2], 2)
    assert status == -1 and model.pending[0] == 19
    model.close()


@pytest.mark.parametrize('source_index', (0, 1))
def test_custom_multiply_may_return_either_input_owner(source_index):
    model = RepeatMemory(method=lambda m: m.load(m.caller + source_index * 8))
    status, result = model.run('ab', object())
    assert status == 0
    assert result.identity == model.load(model.caller + source_index * 8).identity
    assert 'index' not in model.events
    model.close()


def test_index_callback_replaces_and_releases_original_source_binding():
    def index(model):
        source = model.object(model.load(model.caller))
        old = source['children']
        child = model.make(41, 'item')
        source['children'] = [child.identity]
        for identity in old:
            model.decref(model.current(identity))
        model.drop(model.caller, None)
        # A successful Python callback has no pending exception; finalizer
        # exceptions from its replaced values have already been unraisable.
        model.pending = None
        return 2
    model = RepeatMemory(index=index)
    status, result = model.run([1, 2], object())
    assert status == 0 and model.value(result) == [41, 41]
    model.close()


def test_index_callback_exception_is_not_replaced_by_cleanup():
    def index(model):
        model.pending = 'callback-index-error'
        return 2
    model = RepeatMemory(index=index)
    status, _ = model.run([1, 2], object())
    assert status == -1 and model.pending == 'callback-index-error'
    model.close()


@pytest.mark.parametrize('value', ('', b'', (), [], bytearray()))
@pytest.mark.parametrize('count', (-3, 0, 3))
def test_empty_repetition_preserves_immutable_identity_and_mutable_freshness(value, count):
    model = RepeatMemory()
    status, result = model.run(value, count)
    source = model.load(model.caller)
    assert status == 0 and model.value(result) == value
    assert (result.identity == source.identity) == (type(value) in (str, bytes, tuple))
    model.close()
