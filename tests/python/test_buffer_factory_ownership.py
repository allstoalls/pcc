"""Execute buffer conversion bodies with moving bases and cleanup callbacks."""
from pathlib import Path
import pytest
from tests.python.test_foreign_address_leases import _functions
from tests.python.test_list_extend_iterator_ownership import _Object

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_obj_stubs.py'


class BufferModel:
    def __init__(self, kind='bytes', *, relocate=True, failure=None, pending=None, initial_refs=1):
        self.relocate, self.failure = relocate, failure
        self.memory, self.frames, self.leases, self.raw = {}, {}, {}, {}
        self.objects, self.pins, self.events = [], set(), []
        self.fresh, self.pending = None, None
        self.next_address, self.allocations, self.raises = 10000, 0, 0
        self.external = 1000
        self.source = self.make_source(kind)
        self.source[0].refs = initial_refs
        self.frames[self.external] = 2
        self.memory[self.external] = self.source
        self.memory[self.external + 8] = None
        if pending:
            self.pending = self.error(99, pending)
        self.ns = dict(
            _BUFFER_SOURCE=0, _BUFFER_LEAF=1, _BUFFER_ITEM=2, _BUFFER_INDEX=3,
            _BUFFER_RESULT=4, _BUFFER_ERROR=5, _BUFFER_COUNT=6, _BUFFER_BYTES=8,
            PY_TYPE_INT=1, PY_TYPE_BOOL=2, PY_TYPE_BYTES=9, PY_TYPE_BYTEARRAY=10,
            PY_TYPE_MEMORYVIEW=11, PY_TYPE_LIST=6, PY_TYPE_TUPLE=7,
            PYMEMORYVIEWOBJECT_BASE_OFFSET=16, PYBYTESOBJECT_DATA_OFFSET=24,
            PYOBJECTHEADER_FLAGS_OFFSET=12, PYOBJECTHEADER_TYPE_TAG_OFFSET=8,
            PY_FLAG_GC_PINNED=64,
            null=lambda: None, ptr_is_null=lambda value: value is None,
            is_tagged_int=lambda value: False, ptr_eq=lambda a, b: a == b,
            cstr=lambda value: value, stack_alloc=self.stack, memset=self.memset,
            memcpy=self.memcpy,
            ptr_add=self.add, load_ptr=self.load, load_i64=self.load, load_i32=self.load,
            load_i8=self.load, store_ptr=self.store, store_i64=self.store,
            store_i32=self.store, store_i8=self.store,
            global_addr=lambda name: -1 if 'borrowed' in name else 6,
            global_load_ptr=lambda name: None,
            pcc_gc_frame_enter=self.enter, pcc_gc_frame_leave=self.leave,
            pcc_gc_root_copy_lease=self.copy, pcc_gc_root_copy_borrowed_lease=self.copy,
            pcc_gc_foreign_lease_acquire=self.acquire, pcc_gc_foreign_lease_release=self.release,
            pcc_gc_root_move=self.move, pcc_gc_store_root=self.clear_root,
            pcc_gc_note_slot_write_barrier=self.barrier,
            pcc_py_gc_minor_graph_lock=self.boundary, pcc_py_gc_minor_graph_unlock=self.boundary,
            pcc_gc_pin=self.pin, pcc_gc_take_pinned_slot=self.take,
            pcc_gc_alloc=self.allocate, pcc_gc_load_ptr=self.gc_load,
            pcc_gc_store_ptr=self.store_member, pcc_gc_publish_initialized=self.publish,
            pcc_platform_abort=lambda: pytest.fail('invariant abort'),
            py_list_len=self.length, py_tuple_len=self.length,
            py_list_get=self.get, py_tuple_get=self.get,
            py_obj_index_slots=self.index, py_int_to_i64=self.integer,
            py_int_value_i64=lambda value: self.check(value)[0].value,
            py_mem_alloc=self.raw_alloc, py_mem_free=self.raw_free,
            py_err_occurred=lambda: self.pending is not None,
            py_exc_new=self.error, py_raise_owned=self.raise_owned,
            py_tls_exc_swap_slot=self.swap, py_clear_exception=self.clear_error,
            py_incref=self.incref, py_decref=self.decref,
        )
        names = {
            '_buffer_error', '_buffer_drop', '_buffer_adopt', '_buffer_copy',
            '_buffer_new_raw', '_buffer_count_fill', '_buffer_sequence_fill',
            '_buffer_payload_fill', '_buffer_fill', '_buffer_convert_owned',
            'py_bytes_from_obj', 'py_bytearray_from_obj', 'py_memoryview_new',
            '_bytes_from_int_sequence', '_bytes_from_integer_count',
            'py_bytes_new', '_bytearray_new_raw', '_type_of', '_bytes_data', 'py_bytes_len',
        }
        _functions(RUNTIME, names, self.ns)

    def new(self, name, tag, *, fresh=False):
        obj = _Object(name)
        obj.tag, obj.data, obj.base, obj.buffer, obj.length = tag, bytearray(), None, None, 0
        obj.payload, obj.value = [], 0
        obj.initialized_fields = {"base", "buffer"}
        self.objects.append(obj)
        value = obj.pointer()
        if fresh:
            assert self.fresh is None
            self.fresh = value
        return value

    def make_source(self, kind):
        if kind in ('bytes', 'bytearray'):
            value = self.new('source', 9 if kind == 'bytes' else 10)
            value[0].data, value[0].length = bytearray(b'ABC'), 3
            return value
        if kind in ('memoryview', 'nested-view'):
            base = self.make_source('bytearray')
            view = self.new('view', 11)
            view[0].base = base  # Transfer the temporary base owner to this field.
            if kind == 'nested-view':
                outer = self.new('outer-view', 11)
                outer[0].base = view
                return outer
            return view
        if kind in ('list', 'tuple', 'bad-sequence', 'overflow-sequence', 'index-sequence'):
            source = self.new('source', 7 if kind == 'tuple' else 6)
            values = [65, 66, 67]
            if kind == 'bad-sequence':
                values[1] = 256
            if kind == 'overflow-sequence':
                values[1] = 1 << 100
            for i, number in enumerate(values):
                item = self.new('element-' + str(i), 30 if kind == 'index-sequence' else 1)
                item[0].value = number
                source[0].payload.append(item)
            return source
        value = self.new('source', 1)
        value[0].value = -1 if kind == 'negative-count' else 3
        return value

    def check(self, value):
        if isinstance(value, tuple):
            obj, generation = value[:2]
            assert obj.refs > 0 and generation == obj.generation, 'stale or disposed managed/payload pointer'
        return value

    def boundary(self):
        assert self.fresh is None, 'parking call before NEW result publication'
        if not self.relocate:
            return
        for obj in self.objects:
            if obj.refs and obj.tag == 11:
                assert {'base', 'buffer'} <= obj.initialized_fields, 'uninitialized traceable memoryview shape'
        protected = self.pins | {self.load(slot, 0)[0] for slot in self.leases if self.load(slot, 0) is not None}
        moved = {obj for obj in self.objects if obj.refs and obj not in protected and not obj.immortal}
        for obj in moved:
            obj.generation += 1
        def current(value):
            return (value[0], value[0].generation, *value[2:]) if isinstance(value, tuple) and value[0] in moved else value
        for base, count in self.frames.items():
            for i in range(abs(count)):
                address = base + i * 8
                self.memory[address] = current(self.memory.get(address))
        for obj in self.objects:
            obj.base = current(obj.base)
            obj.payload = [current(value) for value in obj.payload]
        self.pending = current(self.pending)

    def stack(self, size):
        self.next_address += 1000
        return self.next_address

    def add(self, ptr, offset):
        if isinstance(ptr, tuple):
            self.check(ptr)
            return ptr[0], ptr[1], (ptr[2] if len(ptr) == 3 else 0) + offset
        return ptr + offset

    def load(self, ptr, offset):
        if isinstance(ptr, int):
            for base, data in self.raw.items():
                if base <= ptr + offset < base + len(data):
                    return data[ptr + offset - base]
            return self.memory.get(ptr + offset, 0)
        self.check(ptr)
        obj = ptr[0]
        off = offset + (ptr[2] if len(ptr) == 3 else 0)
        if off == 0:
            return obj.refs
        if off == 8:
            return obj.tag
        if off == 12:
            return 64 if obj in self.pins else 0
        if off == 16:
            return obj.base if obj.tag == 11 else obj.length
        if obj.tag == 11 and off == 24:
            return obj.buffer
        if off >= 24:
            return obj.data[off - 24]
        raise AssertionError(('unknown load', obj.name, off))

    def store(self, ptr, offset, value):
        if isinstance(ptr, int):
            for base, data in self.raw.items():
                if base <= ptr + offset < base + len(data):
                    data[ptr + offset - base] = value
                    return
            self.memory[ptr + offset] = value
            if self.fresh is not None and value == self.fresh:
                assert any(count > 0 and base <= ptr + offset < base + count * 8 for base, count in self.frames.items())
                self.fresh = None
            return
        self.check(ptr)
        obj = ptr[0]
        off = offset + (ptr[2] if len(ptr) == 3 else 0)
        if off == 0:
            obj.refs = value
        elif off == 8:
            obj.tag = value
        elif off == 16:
            if obj.tag == 11:
                obj.base = value
                obj.initialized_fields.add("base")
            else:
                obj.length = value
        elif obj.tag == 11 and off == 24:
            obj.buffer = value
            obj.initialized_fields.add("buffer")
        elif off >= 24:
            obj.data[off - 24] = value
        else:
            raise AssertionError(('unknown store', obj.name, off))

    def memcpy(self, dst, src, size):
        for i in range(size):
            self.store(dst, i, self.load(src, i))
        return dst

    def memset(self, ptr, value, size):
        if isinstance(ptr, tuple):
            self.boundary()
            self.check(ptr)
            for i in range(size):
                self.store(ptr, i, value)
        else:
            for i in range(0, size, 8):
                self.memory[ptr + i] = None

    def enter(self, count, base):
        self.frames[base] = count
        self.boundary()

    def leave(self, base):
        self.boundary()
        self.frames.pop(base)

    def acquire(self, slot):
        self.boundary()
        value = self.load(slot, 0)
        if value is None:
            return 0
        self.check(value)
        if self.failure == 'lease-result' and value[0].name == 'result':
            return -1
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        self.boundary()
        if token:
            assert self.leases.pop(slot) == token
        return 0

    def copy(self, destination, source):
        self.boundary()
        if self.failure == 'copy':
            return -1
        value = self.load(source, 0)
        if value is not None:
            self.check(value)
            value[0].refs += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def move(self, destination, source):
        self.boundary()
        assert self.load(destination, 0) is None
        self.store(destination, 0, self.load(source, 0))
        self.store(source, 0, None)
        if source in self.leases:
            self.leases[destination] = self.leases.pop(source)
        return 0

    def clear_root(self, slot, value):
        self.boundary()
        assert value is None
        prior = self.load(slot, 0)
        self.store(slot, 0, None)
        self.drop(prior)

    def drop(self, value):
        if value is None:
            return
        self.check(value)
        obj = value[0]
        obj.refs -= 1
        assert obj.refs >= 0
        if obj.refs == 0:
            base, obj.base = obj.base, None
            self.drop(base)
            for i in range(len(obj.payload)):
                child, obj.payload[i] = obj.payload[i], None
                self.drop(child)
            self.events.append(('dispose', obj.name))

    def barrier(self, owner, slot, value):
        self.boundary()
        self.check(value)
        assert value == self.load(slot, 0)

    def allocate(self, size, tag, flags):
        self.boundary()
        self.allocations += 1
        if self.failure == 'allocation':
            return None
        obj = self.new('result', tag, fresh=True)
        obj[0].data = bytearray(max(0, size - 24))
        if tag == 11:
            obj[0].initialized_fields = set()
        return obj

    def raw_alloc(self, size):
        self.boundary()
        if self.failure == 'raw-allocation':
            return None
        ptr = self.stack(size)
        self.raw[ptr] = bytearray(size)
        return ptr

    def raw_free(self, ptr):
        self.boundary()
        self.events.append(('free', ptr))
        self.raw.pop(ptr)
        self.clear_error()
        self.pending = self.error(99, 'free-callback-error')

    def gc_load(self, owner, slot):
        self.boundary()
        return self.load(slot, 0)

    def store_member(self, owner, slot, value):
        self.boundary()
        self.check(owner)
        self.check(value)
        if value is not None:
            value[0].refs += 1
        self.store(slot, 0, value)

    def publish(self, value):
        self.boundary()
        self.check(value)

    def length(self, source):
        self.boundary()
        return len(self.check(source)[0].payload)

    def get(self, source, index):
        self.boundary()
        value = self.check(source)[0].payload[index]
        value[0].refs += 1
        if not value[0].immortal:
            self.fresh = value
        return value

    def index(self, source_slot, result_slot):
        self.boundary()
        value = self.check(self.load(source_slot, 0))
        if self.failure == 'index' and value[0].name == 'element-1':
            self.pending = self.error(2, 'index-callback-error')
            return -1
        if value[0].tag not in (1, 2, 30):
            self.pending = self.error(3, 'not an integer')
            return -1
        obj = self.new('index', 1, fresh=True)
        obj[0].value = value[0].value
        self.store(result_slot, 0, obj)
        return 0

    def integer(self, obj, overflow):
        self.boundary()
        value = self.check(obj)[0].value
        self.store(overflow, 0, int(value < -(1 << 63) or value >= (1 << 63)))
        return value if -(1 << 63) <= value < (1 << 63) else 0

    def pin(self, obj):
        self.check(obj)
        self.pins.add(obj[0])

    def take(self, slot, prior):
        value = self.load(slot, 0)
        self.store(slot, 0, None)
        if value is not None:
            self.check(value)
            if not prior:
                self.pins.remove(value[0])
            self.fresh = value
        return value

    def error(self, kind, message):
        self.boundary()
        value = self.new('error', 99)
        value[0].kind, value[0].message = kind, message
        return value

    def raise_owned(self, value):
        prior, self.pending = self.pending, value
        self.drop(prior)
        self.boundary()
        self.raises += 1

    def swap(self, slot):
        self.boundary()
        prior = self.load(slot, 0)
        self.store(slot, 0, self.pending)
        self.pending = prior

    def clear_error(self):
        self.boundary()
        value, self.pending = self.pending, None
        self.drop(value)

    def incref(self, value):
        self.boundary()
        self.check(value)[0].refs += 1

    def decref(self, value):
        self.boundary()
        self.drop(value)

    def run(self, mode=0):
        name = ['py_bytes_from_obj', 'py_bytearray_from_obj', 'py_memoryview_new'][mode]
        result = self.ns[name](self.load(self.external, 0))
        self.store(self.external, 8, result)
        self.boundary()
        return self.load(self.external, 8)

    def content(self, value):
        self.check(value)
        while value[0].tag == 11:
            value = self.check(value[0].base)
        return bytes(value[0].data[:value[0].length])


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('mode', (0, 1))
@pytest.mark.parametrize('kind,expected', (
    ('bytes', b'ABC'), ('bytearray', b'ABC'), ('memoryview', b'ABC'),
    ('nested-view', b'ABC'), ('list', b'ABC'), ('tuple', b'ABC'),
    ('index-sequence', b'ABC'), ('count', b'\0\0\0'),
))
def test_buffer_copy_holds_actual_payload_owner_through_allocation(kind, expected, mode, relocate):
    model = BufferModel(kind, relocate=relocate)
    result = model.run(mode)
    assert model.content(result) == expected
    assert model.pending is None and not model.leases and not model.pins and not model.raw
    assert model.frames == {model.external: 2}
    if kind == 'bytes' and mode == 0:
        assert result[0] is model.source[0] and result[0].refs == 2
    else:
        assert result[0] is not model.source[0] and result[0].refs == 1
    model.clear_root(model.external, None)
    model.boundary()
    result = model.load(model.external, 8)
    assert model.content(result) == expected and result[0].refs == 1
    model.clear_root(model.external + 8, None)
    assert all(obj.refs == 0 for obj in model.objects)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('kind', ('bytes', 'bytearray', 'memoryview', 'nested-view'))
def test_memoryview_factory_retains_base_before_source_disposal(kind, relocate):
    model = BufferModel(kind, relocate=relocate)
    result = model.run(2)
    assert result[0].tag == 11 and model.content(result) == b'ABC'
    assert model.source[0].refs == 2
    model.clear_root(model.external, None)
    model.boundary()
    assert model.content(model.load(model.external, 8)) == b'ABC'
    model.clear_root(model.external + 8, None)
    assert all(obj.refs == 0 for obj in model.objects)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('kind,failure,message', (
    ('list', 'raw-allocation', 'byte sequence: out of memory'),
    ('list', 'allocation', 'bytes: out of memory'),
    ('list', 'index', 'index-callback-error'),
    ('list', 'lease-result', 'buffer conversion: cannot lease owner'),
    ('memoryview', 'copy', 'buffer conversion: cannot retain owner'),
    ('bad-sequence', None, 'bytes must be in range(0, 256)'),
    ('overflow-sequence', None, 'bytes must be in range(0, 256)'),
    ('negative-count', None, 'negative count'),
))
def test_buffer_failure_keeps_primary_error_through_free_and_owner_cleanup(kind, failure, message, relocate):
    model = BufferModel(kind, failure=failure, relocate=relocate)
    assert model.run() is None
    assert model.pending[0].message == message
    assert not model.raw and not model.leases and not model.pins
    assert model.frames == {model.external: 2}
    model.clear_root(model.external, None)
    model.clear_error()
    assert all(obj.refs == 0 for obj in model.objects)


@pytest.mark.parametrize('function', ('py_bytearray_extend', 'py_bytearray_set_slice'))
@pytest.mark.parametrize('failure', ('index-error', 'allocation-error', 'range-error'))
def test_sequence_conversion_consumers_preserve_the_original_error(function, failure):
    primary = {
        'index-error': RuntimeError('index callback'),
        'allocation-error': MemoryError('conversion allocation'),
        'range-error': ValueError('byte range'),
    }[failure]
    state = {'pending': None, 'raised': 0}
    def convert(value, mutable):
        assert value == 'replacement' and mutable == 1
        state['pending'] = primary
        return None
    def raise_owned(error):
        state['raised'] += 1
        state['pending'] = error
    namespace = {
        'PY_TYPE_BYTEARRAY': 10, 'PY_TYPE_LIST': 6, 'PY_TYPE_TUPLE': 7,
        'null': lambda: None, 'ptr_is_null': lambda value: value is None,
        '_type_of': lambda value: 10 if value == 'receiver' else 6,
        'py_bytes_len': lambda value: 3, 'load_i64': lambda value, offset: 3,
        '_bytes_data': lambda value: 'data' if value == 'receiver' else None,
        '_bytes_is_none_or_null': lambda value: value is None,
        '_bytes_from_int_sequence': convert,
        'py_err_occurred': lambda: state['pending'] is not None,
        'py_raise_owned': raise_owned, 'py_exc_new': lambda kind, text: (kind, text),
        'cstr': lambda value: value,
    }
    _functions(RUNTIME, {function}, namespace)
    if function == 'py_bytearray_extend':
        assert namespace[function]('receiver', 'replacement') is None
    else:
        assert namespace[function]('receiver', None, None, None, 'replacement') == -1
    assert state['pending'] is primary and state['raised'] == 0


@pytest.mark.parametrize('function,kind,expected', (
    ('_bytes_from_int_sequence', 'list', b'ABC'),
    ('_bytes_from_integer_count', 'count', b'\0\0\0'),
))
@pytest.mark.parametrize('mutable', (-1, 0, 1, 2))
def test_private_buffer_factories_keep_boolean_mutability_contract(function, kind, expected, mutable):
    model = BufferModel(kind, relocate=True)
    value = model.ns[function](model.load(model.external, 0), mutable)
    model.store(model.external, 8, value)
    model.boundary()
    result = model.load(model.external, 8)
    assert result[0].tag == (10 if mutable else 9)
    assert model.content(result) == expected
    assert not model.leases and not model.pins and not model.raw
