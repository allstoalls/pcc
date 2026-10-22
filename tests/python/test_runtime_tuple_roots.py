"""List-to-tuple conversion keeps current addresses across moving-GC gates."""
from __future__ import annotations

import os
import subprocess

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_runtime_getter_roots import _GetterMemory


class _TupleMemory(_GetterMemory):
    def __init__(self, backend=4, phase="unregister", after_unlink=False,
                 prior_pin=False, fail="", fail_register=0, count=2):
        super().__init__(backend=backend, phase=phase, prior_pin=prior_pin,
                         fail_register=fail_register)
        self.after_unlink, self.fail = after_unlink, fail
        self.out = None
        self.release_depth = 0
        self.operation_error = None
        self.cleanup_error = False
        self.lst.fields[abi.PYLISTOBJECT_LENGTH_OFFSET] = count
        self.lst.fields[abi.PYLISTOBJECT_CAPACITY_OFFSET] = count
        for index in range(count):
            self.items.fields[index * 8] = self.item
        self.item.references = count
        self.lst.flags |= abi.PY_FLAG_GC_PINNED if prior_pin else 0
        self.caller_slot = _Slots(8)
        self.caller_slot.fields[0] = self.lst
        self.caller_handle = object()
        self.handles[self.caller_handle] = self.caller_slot
        self.namespace.update({
            "memset": self.zero, "store_i32": self.write, "store_i64": self.write,
            "py_decref": self.decref,
            "py_err_occurred": lambda: int(self.error is not None),
            "py_current_exception": lambda: self.error,
            "py_clear_exception": self.clear_error,
            "py_raise": self.raise_error,
            "py_raise_owned": self.raise_owned,
            "py_exc_new": self.new_exception,
        })
        self.load("pcc/runtime/py/py_tuple.py")
        self.namespace.update({"py_tuple_new": self.new_tuple,
                               "py_tuple_set_item": self.set_item})
        original_get = self.namespace["py_list_get"]

        def get(lst, index):
            assert lst.alive, "stale input at py_list_get"
            if self.fail == "get" and index == 1:
                self.operation_error = self.error = self.make(abi.PY_TYPE_EXC)
                return None
            return original_get(lst, index)

        self.namespace["py_list_get"] = get

    def zero(self, slot, _byte, size):
        for offset in range(0, size, 8):
            self.write(slot, offset, None)

    def gc(self, phase):
        super().gc(phase)
        self.error = self.resolve(self.error)
        self.operation_error = self.resolve(self.operation_error)

    def register(self, slot):
        # Target conversion setup, not the nested getter's own registration
        # protocol (the real read barrier can heal its pre-link addresses).
        conversion_root = self.registrations < 4
        if conversion_root:
            self.gc("register_before")
        handle = super().register(slot)
        if conversion_root:
            self.gc("register_after")
        return handle

    def unregister(self, handle):
        if not self.after_unlink:
            self.gc("unregister")
        del self.handles[handle]
        if self.after_unlink:
            self.gc("unregister")

    def new_tuple(self, size):
        self.gc("allocation")
        if self.fail == "allocation":
            # The real py_tuple_new returns NULL with no pending exception.
            return None
        self.out = self.make(abi.PY_TYPE_TUPLE)
        self.out.fields[abi.PYTUPLEOBJECT_LEN_OFFSET] = size
        return self.out

    def new_exception(self, _tag, _message):
        self.operation_error = self.make(abi.PY_TYPE_EXC)
        return self.operation_error

    def set_item(self, out, index, item):
        self.gc("store")
        assert out.alive, "stale output at py_tuple_set_item"
        if isinstance(item, _Object):
            assert item.alive, "stale item at py_tuple_set_item"
        if self.fail == "set" and index == 1:
            self.operation_error = self.error = self.make(abi.PY_TYPE_EXC)
            return
        self.incref(item)
        self.write(out, abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8, item)
        self.gc("publish")

    def decref(self, value):
        assert self.depth == 0, "finalizers must run outside the graph lease"
        self.release_depth += 1
        if isinstance(value, _Object):
            assert value.alive, "stale owner during release"
            value.references -= 1
            if value.references == 0:
                if value.tag == abi.PY_TYPE_TUPLE:
                    if self.cleanup_error:
                        self.error = self.make(abi.PY_TYPE_EXC)
                    for offset, item in list(value.fields.items()):
                        if offset >= abi.PYTUPLEOBJECT_ITEMS_OFFSET:
                            self.decref(item)
                value.alive = False
        self.release_depth -= 1
        if self.release_depth == 0:
            self.gc("decref")

    def clear_error(self):
        error, self.error = self.error, None
        self.decref(error)

    def raise_error(self, error):
        self.incref(error)
        old, self.error = self.error, error
        self.decref(old)

    def raise_owned(self, error):
        self.raise_error(error)
        self.decref(self.resolve(error))

    def convert(self):
        return self.namespace["py_tuple_from_list"](self.lst)

    def assert_balanced(self):
        assert self.handles == {self.caller_handle: self.caller_slot}
        assert self.depth == self.pins == 0
        assert self.resolve(self.lst).references == 1


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("phase", ("unregister", "graph_lock", "graph_unlock", "retain_finish",
                                    "allocation", "store", "publish", "decref",
                                    "register_before", "register_after"))
@pytest.mark.parametrize("after_unlink", (False, True))
def test_tuple_from_list_reloads_owners_at_each_moving_gate(backend, phase, after_unlink):
    memory = _TupleMemory(backend, phase, after_unlink)
    result = memory.convert()
    assert result is memory.resolve(memory.out) and result.alive
    assert result.references == 1
    assert memory.read(result, abi.PYTUPLEOBJECT_LEN_OFFSET) == 2
    assert memory.read(result, abi.PYTUPLEOBJECT_ITEMS_OFFSET) is memory.item
    assert memory.read(result, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 8) is memory.item
    assert memory.item.references == 4
    assert memory.error is None
    assert memory.moves > 0
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("prior_pin", (False, True))
@pytest.mark.parametrize("count", (0, 1, 3))
def test_tuple_conversion_keeps_ownership_and_existing_pins(backend, prior_pin, count):
    memory = _TupleMemory(backend=backend, prior_pin=prior_pin, count=count)
    result = memory.convert()
    assert result is memory.resolve(memory.out) and result.references == 1
    assert memory.read(result, abi.PYTUPLEOBJECT_LEN_OFFSET) == count
    assert memory.item.references == count * 2
    assert bool(memory.item.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
    assert bool(memory.lst.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
    assert not result.flags & abi.PY_FLAG_GC_PINNED
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
def test_tuple_conversion_accepts_tagged_elements_and_null_input(backend):
    memory = _TupleMemory(backend=backend)
    memory.items.fields.update({0: 85, 8: 87})
    result = memory.convert()
    assert memory.read(result, abi.PYTUPLEOBJECT_ITEMS_OFFSET) == 85
    assert memory.read(result, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 8) == 87
    assert memory.namespace["py_tuple_from_list"](None) is None
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("fail", ("allocation", "get", "set"))
@pytest.mark.parametrize("phase", ("unregister", "graph_unlock", "decref"))
def test_tuple_conversion_releases_partial_output_and_preserves_error(backend, fail, phase):
    memory = _TupleMemory(backend=backend, phase=phase, fail=fail)
    assert memory.convert() is None
    assert memory.error is not None and memory.error.alive
    assert memory.error.tag == abi.PY_TYPE_EXC and memory.error.references == 1
    assert memory.error is memory.operation_error
    assert memory.item.references == 2
    if memory.out is not None:
        assert not memory.resolve(memory.out).alive
    memory.assert_balanced()


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("failed_root", (1, 2, 3, 4))
@pytest.mark.parametrize("prior_pin", (False, True))
def test_tuple_conversion_registration_failure_balances_borrow_and_pins(backend, failed_root, prior_pin):
    memory = _TupleMemory(backend=backend, fail_register=failed_root, prior_pin=prior_pin)
    assert memory.convert() is None
    assert memory.out is None and memory.item.references == 2
    assert memory.error is not None and memory.error.references == 1
    assert bool(memory.lst.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("prior_pin", (False, True))
@pytest.mark.parametrize("resurrect", (False, True))
def test_tuple_conversion_release_transfers_owner_before_finalizer(backend, prior_pin, resurrect):
    memory = _TupleMemory(backend=backend, prior_pin=prior_pin, count=1)
    slot = _Slots(8)
    slot.fields[0] = memory.item
    handle = memory.register(slot)
    events = []

    def finalize(value):
        assert value is memory.item and value.alive
        assert memory.depth == memory.pins == 0
        assert slot.fields[0] is None
        assert bool(value.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
        value.references -= 1
        assert value.references == 0
        events.append("finalizer")
        if resurrect:
            value.references += 1
        else:
            events.append("weakref")
            value.alive = False

    memory.namespace["py_decref"] = finalize
    memory.namespace["_tuple_conversion_release_slot"](slot)
    memory.unregister(handle)
    assert events == (["finalizer"] if resurrect else ["finalizer", "weakref"])
    assert memory.item.references == int(resurrect)
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
def test_tuple_conversion_error_survives_reentrant_cleanup(backend):
    memory = _TupleMemory(backend=backend, phase="decref", fail="get")
    memory.cleanup_error = True
    assert memory.convert() is None
    assert memory.error is memory.operation_error
    assert memory.error.alive and memory.error.references == 1
    assert memory.item.references == 2
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
def test_tuple_conversion_owned_return_restores_preexisting_output_pin(backend):
    memory = _TupleMemory(backend=backend)
    original_new = memory.namespace["py_tuple_new"]

    def pinned_new(size):
        result = original_new(size)
        result.flags |= abi.PY_FLAG_GC_PINNED
        return result

    memory.namespace["py_tuple_new"] = pinned_new
    result = memory.convert()
    assert result.flags & abi.PY_FLAG_GC_PINNED
    assert result.references == 1
    memory.assert_balanced()


_NATIVE_SOURCE = '''import gc
class Record:
    def __init__(self, value):
        self.value = value

def consume(first, middle, last):
    assert first is middle
    assert first is last
    return first

def suspended(record):
    result = (record, (yield record), record)
    yield result

def main():
    for iteration in range(24):
        record = Record(iteration)
        source = [record, record]
        # Mixed tuple splats and dynamic call argument normalization both
        # execute py_tuple_from_list, including source/result/item owners.
        converted = (record, *source, record)
        assert len(converted) == 4
        for item in converted:
            assert item is record
            assert item.value == iteration
        callback = consume
        assert callback(record, *[record], record) is record
        generator = suspended(record)
        assert next(generator) is record
        gc.collect()
        resumed = generator.send(record)
        assert len(resumed) == 3
        assert resumed[0] is record
        assert resumed[1] is record
        assert resumed[2] is record
        assert next(generator, None) is None
        gc.collect()
        assert converted[0] is record
        assert converted[3].value == iteration
    print("TUPLE_CONVERSION_ROOTS_OK")
main()
'''


@pytest.mark.integration
def test_tuple_conversion_executes_all_collectors(tmp_path, monkeypatch,
        pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "tuple_conversion_roots.py"
    source.write_text(_NATIVE_SOURCE)
    binary = tmp_path / "tuple_conversion_roots"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self",
        libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
            timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                 PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert result.returncode == 0, (backend, result.returncode, result.stdout, result.stderr)
        assert result.stdout == "TUPLE_CONVERSION_ROOTS_OK\n", (backend, result.stdout)
        assert result.stderr == "", (backend, result.stderr)
