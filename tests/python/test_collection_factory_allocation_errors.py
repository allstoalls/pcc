"""Production allocation failures preserve TLS and dispose partial owners once."""
from pathlib import Path
import pytest
from tests.python.test_foreign_address_leases import _functions

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py'


class ListFactoryModel:
    def __init__(self, failure, pending, relocate):
        self.failure, self.pending, self.relocate = failure, pending, relocate
        self.memory, self.frames, self.leases, self.refs = {}, {}, {}, {}
        self.pins, self.objects = set(), set()
        self.fresh = None
        self.next_address = 10000
        self.disposals = 0
        self.ns = {
            'PYLISTOBJECT_SIZE': 40, 'PY_TYPE_LIST': 6,
            'PYLISTOBJECT_LENGTH_OFFSET': 16, 'PYLISTOBJECT_CAPACITY_OFFSET': 24,
            'PYLISTOBJECT_ITEMS_OFFSET': 32, 'PYOBJECTHEADER_FLAGS_OFFSET': 12,
            'PY_FLAG_GC_PINNED': 64,
            'null': lambda: None, 'ptr_is_null': lambda value: value is None,
            'stack_alloc': self.stack, 'memset': self.zero,
            'global_addr': lambda name: 2, 'ptr_add': lambda base, offset: base + offset,
            'store_ptr': self.store, 'store_i64': self.store,
            'load_ptr': self.load, 'load_i64': self.load, 'load_i32': self.load,
            'pcc_gc_frame_enter': self.enter, 'pcc_gc_frame_leave': self.leave,
            'pcc_gc_alloc': self.allocate, 'malloc': self.malloc,
            'pcc_gc_foreign_lease_acquire': self.acquire,
            'pcc_gc_foreign_lease_release': self.release,
            'pcc_gc_note_slot_write_barrier': self.barrier,
            'pcc_gc_backend4_zpage_register_owner_payload_span': self.span,
            'py_gc_track': self.track, 'pcc_gc_publish_initialized': self.track,
            'pcc_py_gc_minor_graph_lock': self.boundary,
            'pcc_py_gc_minor_graph_unlock': self.boundary,
            'pcc_gc_load_ptr': lambda owner, slot: self.load(slot, 0),
            'pcc_gc_pin': self.pin, 'pcc_gc_take_pinned_slot': self.take,
            'py_err_occurred': lambda: self.pending is not None,
            'cstr': lambda value: value,
            'py_exc_new': self.exception, 'py_raise_owned': self.raise_owned,
            'py_tls_exc_swap_slot': self.swap, 'py_clear_exception': self.clear_error,
            'pcc_gc_store_root': self.clear_root,
            '_debug_bad_container': lambda obj, code: pytest.fail('unexpected oversized input'),
            'pcc_platform_abort': lambda: pytest.fail('invariant abort'),
        }
        _functions(RUNTIME / 'py_list.py', {'py_list_new', '_list_factory_finish'}, self.ns)

    def stack(self, size):
        self.next_address += 1000
        return self.next_address

    def zero(self, ptr, value, size):
        for offset in range(0, size, 8):
            self.memory[ptr + offset] = None

    def boundary(self):
        assert self.fresh is None, 'NEW header crossed runtime boundary before root publication'
        for obj in self.objects:
            assert self.memory.get(obj + 16) == 0
            assert obj + 32 in self.memory, 'collector-visible shape is uninitialized'
        if not self.relocate:
            return
        protected = self.pins | {self.load(slot, 0) for slot in self.leases}
        for obj in list(self.objects - protected):
            moved = obj + 100000
            for offset in (12, 16, 24, 32):
                self.memory[moved + offset] = self.memory.pop(obj + offset)
            self.objects.remove(obj)
            self.objects.add(moved)
            self.refs[moved] = self.refs.pop(obj)
            for base, count in self.frames.items():
                for offset in range(count):
                    address = base + offset * 8
                    if self.memory.get(address) == obj:
                        self.memory[address] = moved

    def load(self, ptr, offset):
        return self.memory.get(ptr + offset)

    def store(self, ptr, offset, value):
        self.memory[ptr + offset] = value
        if value is not None and value == self.fresh:
            assert any(base <= ptr + offset < base + count * 8 for base, count in self.frames.items())
            self.fresh = None

    def enter(self, count, base):
        self.frames[base] = count
        self.boundary()

    def leave(self, base):
        self.boundary()
        self.frames.pop(base)

    def allocate(self, size, tag, flags):
        self.boundary()
        if self.failure == 'header':
            return None
        obj = self.stack(size)
        self.refs[obj] = 1
        self.objects.add(obj)
        self.memory[obj + 12] = 0
        self.fresh = obj
        return obj

    def malloc(self, size):
        self.boundary()
        return None if self.failure == 'items' else self.stack(size)

    def acquire(self, slot):
        self.boundary()
        if self.failure == 'lease':
            return -1
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        self.boundary()
        assert self.leases.pop(slot) == token
        return 0

    def barrier(self, owner, slot, value):
        self.boundary()
        assert self.load(slot, 0) == value and value in self.objects

    def span(self, obj, items, size):
        self.boundary()
        assert obj in self.objects and self.load(obj, 32) == items

    def track(self, obj):
        self.boundary()
        assert obj in self.objects

    def exception(self, kind, message):
        self.boundary()
        assert kind == 19
        return message

    def raise_owned(self, error):
        self.boundary()
        self.pending = error

    def swap(self, slot):
        self.boundary()
        prior = self.load(slot, 0)
        self.store(slot, 0, self.pending)
        self.pending = prior

    def clear_error(self):
        self.boundary()
        self.pending = None

    def clear_root(self, slot, value):
        self.boundary()
        assert value is None
        old = self.load(slot, 0)
        self.store(slot, 0, None)
        if old is not None:
            assert old in self.objects and self.refs[old] == 1
            self.refs[old] = 0
            self.objects.remove(old)
            self.disposals += 1
            self.pending = 'partial-disposal-error'
            self.boundary()

    def pin(self, obj):
        assert obj in self.objects
        self.pins.add(obj)

    def take(self, slot, prior):
        obj = self.load(slot, 0)
        assert obj in self.pins and prior == 0
        self.store(slot, 0, None)
        self.pins.remove(obj)
        return obj


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('pending', (None, 'original-error'))
@pytest.mark.parametrize('failure', ('header', 'items', 'lease'))
def test_list_factory_failure_preserves_error_and_disposes_partial_header(failure, pending, relocate):
    model = ListFactoryModel(failure, pending, relocate)
    assert model.ns['py_list_new'](4) is None
    assert model.pending == (pending or 'list: out of memory')
    assert model.disposals == (0 if failure == 'header' else 1)
    assert not model.objects and not model.frames and not model.leases and not model.pins


@pytest.mark.parametrize('relocate', (False, True))
def test_list_factory_success_transfers_current_owner_after_frame_leave(relocate):
    model = ListFactoryModel(None, None, relocate)
    result = model.ns['py_list_new'](4)
    assert result in model.objects and model.refs[result] == 1
    assert not model.frames and not model.leases and not model.pins
    assert model.pending is None and model.disposals == 0


@pytest.mark.parametrize('pending', (None, 'original-error'))
@pytest.mark.parametrize('module,function,args,message', (
    ('py_tuple.py', 'py_tuple_new', (3,), 'tuple: out of memory'),
    ('py_obj_stubs.py', 'py_bytes_new', (None, 3), 'bytes: out of memory'),
    ('py_obj_stubs.py', '_bytearray_new_raw', (None, 3), 'bytearray: out of memory'),
    ('py_obj_stubs.py', 'py_memoryview_new', ('source',), 'memoryview: out of memory'),
))
def test_single_allocation_factories_raise_only_when_no_error_pending(module, function, args, message, pending):
    if function == 'py_memoryview_new':
        from tests.python.test_buffer_factory_ownership import BufferModel

        model = BufferModel('bytes', failure='allocation', pending=pending)
        assert model.run(2) is None
        assert model.pending[0].message == (pending or message)
        assert model.allocations == 1 and model.raises == int(pending is None)
        assert not model.leases and not model.pins
        model.clear_root(model.external, None)
        model.clear_error()
        assert all(value.refs == 0 for value in model.objects)
        return
    state = {'pending': pending, 'allocations': 0, 'raises': 0}
    def allocate(size, tag, flags):
        state['allocations'] += 1
        return None
    def raise_owned(error):
        state['raises'] += 1
        state['pending'] = error
    ns = dict(PYTUPLEOBJECT_SIZE=24, PY_TYPE_TUPLE=7, PY_TYPE_BYTES=9,
              PY_TYPE_BYTEARRAY=10, PY_TYPE_MEMORYVIEW=11,
              null=lambda: None, ptr_is_null=lambda value: value is None,
              pcc_gc_alloc=allocate, py_err_occurred=lambda: state['pending'] is not None,
              py_exc_new=lambda kind, text: text if kind == 19 else pytest.fail('wrong exception'),
              cstr=lambda text: text, py_raise_owned=raise_owned)
    _functions(RUNTIME / module, {function}, ns)
    assert ns[function](*args) is None
    assert state == {'pending': pending or message, 'allocations': 1, 'raises': int(pending is None)}
