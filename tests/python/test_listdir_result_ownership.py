"""Actual listdir body models retain UTF-8, partial lists and primary errors."""
from pathlib import Path
import pytest

from tests.python.test_buffer_factory_ownership import BufferModel
from tests.python.test_foreign_address_leases import _functions

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_process_substrate.py'


class ListdirModel(BufferModel):
    def __init__(self, *, relocate=True, failure=None, alias=False):
        super().__init__('bytes', relocate=relocate, failure=failure)
        self.source[0].data = bytearray(b'.\0')
        self.source[0].length = 1
        self.alias = alias
        self.closed, self.opened, self.next_entry, self.new_items = 0, 0, 0, 0
        self.entries = ('.', '..', 'alpha', 'beta')
        self.ns.update(
            _LISTDIR_PATH=0, _LISTDIR_TEXT=1, _LISTDIR_RESULT=2,
            _LISTDIR_ITEM=3, _LISTDIR_ERROR=4, _LISTDIR_COUNT=5, _LISTDIR_BYTES=8,
            global_addr=lambda name: -1 if 'borrowed' in name else 5,
            py_obj_str=self.stringify, py_str_utf8=self.utf8,
            directory_open=self.open, directory_next=self.next,
            directory_error=self.directory_error, directory_close=self.close,
            py_list_new=self.list_new, py_str_new=self.string_new,
            py_list_append=self.append, strlen=self.strlen,
            pcc_gc_foreign_lease_acquire=self.acquire,
        )
        _functions(RUNTIME, {
            '_listdir_error', '_listdir_drop', '_listdir_adopt',
            '_listdir_read', '_listdir_owned', 'py_os_listdir',
        }, self.ns)

    def acquire(self, slot):
        self.boundary()
        value = self.load(slot, 0)
        if value is not None:
            name = self.check(value)[0].name
            if self.failure == 'lease-text' and name == 'text':
                return -1
            if self.failure == 'lease-item' and name == 'beta':
                return -1
        return super().acquire(slot)

    def stringify(self, source):
        self.boundary()
        self.check(source)
        if self.failure == 'stringify':
            self.pending = self.error(71, 'path conversion failed')
            return None
        if self.alias:
            source[0].refs += 1
            self.fresh = source
            return source
        value = self.new('text', 3, fresh=True)
        value[0].data, value[0].length = bytearray(b'.\0'), 1
        return value

    def utf8(self, value):
        self.boundary()
        self.check(value)
        if self.failure == 'utf8':
            self.pending = self.error(72, 'UTF-8 conversion failed')
            return None
        return self.add(value, 24)

    def open(self, path):
        self.boundary()
        self.check(path)
        assert self.load(path, 0) == ord('.')
        if self.failure == 'open':
            return None
        self.opened += 1
        return 42

    def next(self, stream):
        self.boundary()
        assert stream == 42 and self.closed == 0
        if self.next_entry == len(self.entries):
            return None
        data = self.entries[self.next_entry].encode() + b'\0'
        self.next_entry += 1
        address = self.stack(len(data))
        self.raw[address] = bytearray(data)
        return address

    def directory_error(self, stream):
        self.boundary()
        assert stream == 42
        return -1 if self.failure == 'read' else 0

    def close(self, stream):
        self.boundary()
        assert stream == 42 and self.closed == 0
        self.closed += 1
        self.raw.clear()
        self.clear_error()
        self.pending = self.error(90, 'close callback')

    def list_new(self, count):
        self.boundary()
        assert count == 0
        if self.failure == 'list-allocation':
            return None
        return self.new('result', 6, fresh=True)

    def strlen(self, data):
        self.boundary()
        length = 0
        while self.load(data, length):
            length += 1
        return length

    def string_new(self, data, length):
        self.boundary()
        self.new_items += 1
        if self.failure == 'item-allocation' and self.new_items == 2:
            return None
        name = bytes(self.load(data, i) for i in range(length)).decode()
        value = self.new(name, 3, fresh=True)
        value[0].data, value[0].length = bytearray(name.encode()+b'\0'), length
        return value

    def append(self, output, item):
        self.boundary()
        out, value = self.check(output)[0], self.check(item)[0]
        value.refs += 1
        out.payload.append(item)
        if self.failure == 'append' and value.name == 'beta':
            self.pending = self.error(73, 'append callback failed')

    def drop(self, value):
        obj = value[0] if value is not None else None
        super().drop(value)
        if obj is not None and obj.refs == 0 and obj.name in ('text', 'alpha', 'beta'):
            self.clear_error()
            self.pending = self.error(91, 'destructor callback')

    def run(self):
        result = self.ns['py_os_listdir'](self.load(self.external, 0))
        self.store(self.external, 8, result)
        self.boundary()
        assert not self.leases
        assert self.frames == {self.external: 2}
        assert not self.pins
        assert self.load(self.external, 0)[0].refs == 1
        return self.load(self.external, 8)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('alias', (False, True))
def test_listdir_owns_path_utf8_and_result_through_all_cleanup(relocate, alias):
    model = ListdirModel(relocate=relocate, alias=alias)
    result = model.run()
    assert [model.check(value)[0].name for value in result[0].payload] == ['alpha', 'beta']
    assert model.pending is None
    assert model.opened == model.closed == 1
    assert result[0].refs == 1
    assert all(value[0].refs == 1 for value in result[0].payload)
    assert all(obj.refs == 0 for obj in model.objects if obj.name in ('text', 'error'))


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure,kind,message', (
    ('copy', 19, 'cannot retain path'),
    ('stringify', 71, 'path conversion failed'),
    ('utf8', 72, 'UTF-8 conversion failed'),
    ('lease-text', 19, 'cannot lease owner'),
    ('open', 14, 'could not open directory'),
    ('list-allocation', 19, 'out of memory'),
    ('lease-result', 19, 'cannot lease owner'),
    ('item-allocation', 19, 'out of memory'),
    ('lease-item', 19, 'cannot lease owner'),
    ('append', 73, 'append callback failed'),
    ('read', 14, 'could not read directory'),
))
def test_listdir_failure_disposes_partial_owners_preserving_primary_error(relocate, failure, kind, message):
    model = ListdirModel(relocate=relocate, failure=failure)
    assert model.run() is None
    assert model.pending[0].kind == kind
    assert message in model.pending[0].message
    assert model.closed == model.opened
    live = [obj for obj in model.objects if obj.refs]
    assert set(live) == {model.load(model.external, 0)[0], model.pending[0]}


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('names', ((), ('.', '..'), ('.', '..', '.hidden', 'snow☃')))
def test_listdir_empty_dot_and_utf8_entries_preserve_order(relocate, names):
    model = ListdirModel(relocate=relocate)
    model.entries = names
    result = model.run()
    assert [model.check(value)[0].name for value in result[0].payload] == [
        name for name in names if name not in ('.', '..')
    ]
    assert model.closed == 1 and model.pending is None
