"""Execute iterator source at its moving-GC root teardown boundaries.

The memory model forces individual park gates; the integration case separately
executes the same sequence/callable/cleanup paths in the real native runtime.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_runtime_getter_roots import _GetterMemory


class _IteratorMemory(_GetterMemory):
    def __init__(self, backend=4, gate=0, after_unlink=False, prior_pin=False,
                 callable_iterator=False, stop=False, call_error=False, eq_error=False,
                 fail_register=0):
        super().__init__(backend=backend, phase="unregister", prior_pin=prior_pin,
                         fail_register=fail_register)
        self.gate, self.after_unlink = gate, after_unlink
        self.unregister_calls = 0
        self.releases = []
        self.call_error, self.eq_error, self.stop = call_error, eq_error, stop
        self.iterator = self.make(abi.PY_TYPE_ITER)
        self.iterator.fields.update({16: self.lst, 24: 0})
        if callable_iterator:
            self.callable = self.make(abi.PY_TYPE_FUNC)
            self.sentinel = self.make(abi.PY_TYPE_STR)
            pair = self.make(abi.PY_TYPE_TUPLE)
            pair.fields.update({16: self.callable, 24: self.sentinel})
            self.iterator.fields.update({16: pair, 24: -1})
        self.namespace.update({
            "store_i64": self.write, "py_decref": self.decref,
            "pcc_capi_is_cext_type_tag": lambda _tag: 0,
            "getenv": lambda _name: None,
            "py_runtime_error_if_unset": self.runtime_error,
            "py_tuple_get": self.tuple_get,
            "py_tuple_new": lambda _size: self.make(abi.PY_TYPE_TUPLE),
            "global_load_ptr": lambda _name: None,
            "py_obj_call": self.call, "py_obj_eq": self.eq,
            "py_err_occurred": lambda: int(self.error is not None),
        })
        self.load("pcc/runtime/py/py_iter.py")

    def unregister(self, handle):
        self.unregister_calls += 1
        if self.unregister_calls == self.gate and not self.after_unlink:
            self.gc("unregister")
        del self.handles[handle]
        if self.unregister_calls == self.gate and self.after_unlink:
            self.gc("unregister")

    def decref(self, value):
        assert self.depth == 0, "finalizers must run outside the graph lease"
        if isinstance(value, _Object):
            assert value.alive, "stale iterator owner during release"
            self.releases.append(value.tag)
            value.references -= 1
            if value.references == 0:
                value.alive = False

    def runtime_error(self, helper, message):
        if self.error is None:
            self.error = (helper, message)

    def tuple_get(self, pair, index):
        value = self.read(pair, 16 + index * 8)
        self.incref(value)
        return value

    def call(self, callable, args, _kwargs):
        assert callable.alive and args.alive
        if self.call_error:
            self.error = "call error"
            return None
        self.incref(self.item)
        return self.item

    def eq(self, result, sentinel):
        assert result.alive and sentinel.alive
        if self.eq_error:
            self.error = "equality error"
        return int(self.stop)

    def next(self):
        return self.namespace["py_obj_next"](self.resolve(self.iterator))

    def assert_balanced(self):
        assert not self.handles
        assert self.depth == self.pins == 0


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("gate", (1, 2, 3, 4, 5))
@pytest.mark.parametrize("after_unlink", (False, True))
def test_sequence_next_reloads_length_owner_and_teardown_result(backend, gate, after_unlink):
    memory = _IteratorMemory(backend=backend, gate=gate, after_unlink=after_unlink)
    result = memory.next()
    assert result is memory.item and result.alive and result.references == 2
    assert memory.resolve(memory.iterator).fields[24] == 1
    assert memory.moves > 0
    assert memory.error is None
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("callable_iterator", (False, True))
@pytest.mark.parametrize("prior_pin", (False, True))
def test_iterator_owned_return_restores_existing_pin_and_reference(backend, callable_iterator, prior_pin):
    memory = _IteratorMemory(backend=backend, gate=5, prior_pin=prior_pin,
                             callable_iterator=callable_iterator)
    result = memory.next()
    assert result is memory.item and result.alive and result.references == 2
    assert bool(result.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
    assert memory.error is None
    memory.assert_balanced()


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("gate", (1, 2, 3, 4, 5))
@pytest.mark.parametrize("outcome", ("value", "stop", "equality error", "call error"))
@pytest.mark.parametrize("after_unlink", (False, True))
def test_callable_iterator_cleanup_keeps_live_addresses(backend, gate, outcome, after_unlink):
    memory = _IteratorMemory(backend=backend, gate=gate, after_unlink=after_unlink,
                             callable_iterator=True, stop=outcome == "stop",
                             eq_error=outcome == "equality error",
                             call_error=outcome == "call error")
    result = memory.next()
    if outcome == "value":
        assert result is memory.item and result.alive and result.references == 2
        assert memory.error is None
    else:
        assert result is None
        assert memory.item.references == 1
        assert memory.error == ((8, None) if outcome == "stop" else outcome)
        assert memory.resolve(memory.iterator).fields[24] == (-2 if outcome == "stop" else -1)
    assert memory.resolve(memory.callable).references == 1
    assert memory.resolve(memory.sentinel).references == 1
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
def test_tagged_iterator_result_needs_no_pin(backend):
    memory = _IteratorMemory(backend=backend, gate=4)
    memory.items.fields[0] = 85
    assert memory.next() == 85
    assert memory.error is None
    memory.assert_balanced()


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("failed_root", (1, 2, 3, 4, 5))
def test_callable_registration_failure_releases_only_owned_references(backend, failed_root):
    memory = _IteratorMemory(backend=backend, callable_iterator=True,
                             fail_register=failed_root)
    assert memory.next() is None
    assert memory.item.references == 1
    assert memory.resolve(memory.callable).references == 1
    assert memory.resolve(memory.sentinel).references == 1
    memory.assert_balanced()


_NATIVE_SOURCE = '''class Record:
    def __init__(self, value):
        self.value = value

class Producer:
    def __init__(self, value):
        self.value = value
        self.calls = 0
    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            return self.value
        return None

class BadEquality:
    def __eq__(self, other):
        raise ValueError("comparison")

class BadCall:
    def __call__(self):
        raise ValueError("call")

def main():
    for iteration in range(24):
        record = Record(iteration)
        sequence = [record]
        iterator = iter(sequence)
        result = next(iterator)
        assert result is record
        assert result.value == iteration
        assert next(iterator, None) is None
        tuple_iterator = iter((record,))
        assert next(tuple_iterator) is record
        assert next(tuple_iterator, None) is None
        numbered = list(enumerate(sequence, 17))
        assert numbered[0][0] == 17
        assert numbered[0][1] is record
        callable_iterator = iter(Producer(record), None)
        assert next(callable_iterator) is record
        assert next(callable_iterator, "done") == "done"
        assert next(callable_iterator, "done") == "done"
        assert list(iter("ab")) == ["a", "b"]
        assert list(iter(b"ab")) == [97, 98]
        try:
            next(iter(BadCall(), None))
            raise AssertionError("call error lost")
        except ValueError as error:
            assert str(error) == "call"
        try:
            next(iter(Producer(BadEquality()), Record(-1)))
            raise AssertionError("comparison error lost")
        except ValueError as error:
            assert str(error) == "comparison"
    print("ITERATOR_ROOT_HANDOFF_OK")
main()
'''


@pytest.mark.integration
def test_iterator_handoff_executes_all_collectors(tmp_path, monkeypatch,
        pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "iterator_root_handoff.py"
    source.write_text(_NATIVE_SOURCE)
    binary = tmp_path / "iterator_root_handoff"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self",
        libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True,
            timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                 PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert result.returncode == 0, (backend, result.returncode, result.stdout, result.stderr)
        assert result.stdout == "ITERATOR_ROOT_HANDOFF_OK\n", (backend, result.stdout)
        assert result.stderr == "", (backend, result.stderr)


class _EnumerateMemory(_IteratorMemory):
    def __init__(self, backend=4, gate=0, after_unlink=False, fail=""):
        super().__init__(backend=backend, gate=gate, after_unlink=after_unlink)
        self.fail = fail
        self.next_calls = 0
        self.out = None
        self.namespace.update({
            "py_obj_iter": self.iter,
            "py_obj_next": self.next_item,
            "py_list_new": self.new_list,
            "py_tuple_new": self.new_pair,
            "py_tuple_set_item": self.tuple_set,
            "py_int_from_i64": self.new_index,
            "py_list_append": self.append,
            "pcc_gc_unpin": self.unpin,
            "py_current_exception": lambda: self.error,
            "py_exc_builtin_class": lambda tag: tag,
            "py_exc_matches": lambda error, tag: error == (tag, None),
            "py_clear_exception": lambda: setattr(self, "error", None),
        })

    def iter(self, _sequence):
        iterator = self.resolve(self.iterator)
        self.incref(iterator)
        return iterator

    def next_item(self, iterator):
        assert iterator.alive
        self.next_calls += 1
        if self.fail == "next":
            self.error = "iteration error"
            return None
        if self.next_calls > 1:
            self.error = (8, None)
            return None
        self.incref(self.item)
        return self.item

    def new_list(self, _capacity):
        if self.fail == "list":
            return None
        self.out = self.make(abi.PY_TYPE_LIST)
        return self.out

    def new_pair(self, _size):
        return None if self.fail == "pair" else self.make(abi.PY_TYPE_TUPLE)

    def new_index(self, index):
        if self.fail == "index":
            return None
        value = self.make(abi.PY_TYPE_INT)
        value.fields[16] = index
        return value

    def tuple_set(self, pair, index, value):
        assert pair.alive and value.alive
        self.incref(value)
        self.write(pair, 16 + index * 8, value)

    def append(self, out, pair):
        assert out.alive and pair.alive
        if self.fail == "append":
            self.error = "append error"
            return
        self.incref(pair)
        self.write(out, 16, pair)

    def unpin(self, value):
        assert value.alive
        value.flags &= ~abi.PY_FLAG_GC_PINNED
        self.pins -= 1


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("gate", (1, 2, 3))
@pytest.mark.parametrize("after_unlink", (False, True))
@pytest.mark.parametrize("fail", ("", "list", "next", "pair", "index", "append"))
def test_enumerate_owner_teardown_and_errors_keep_live_addresses(backend, gate, after_unlink, fail):
    memory = _EnumerateMemory(backend, gate, after_unlink, fail)
    result = memory.namespace["py_enumerate_list"](memory.lst, 17)
    if fail:
        assert result is None and memory.error is not None
    else:
        assert result is memory.out and result.alive
        assert result.references == 1
        pair = memory.read(result, 16)
        assert memory.read(pair, 24) is memory.item
        assert memory.read(memory.read(pair, 16), 16) == 17
        assert memory.error is None
    assert memory.resolve(memory.iterator).references == 1
    memory.assert_balanced()


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("prior_pin", (False, True))
def test_owned_release_unroots_before_reentrant_finalizer(backend, prior_pin):
    memory = _IteratorMemory(backend=backend, gate=1, prior_pin=prior_pin)
    slot = _Slots(8)
    handle = memory.namespace["_iter_prepare_moving_root"](slot, memory.item, backend)
    finalized = []

    def finalize(value):
        assert value is memory.item and value.alive
        assert not memory.handles and memory.depth == memory.pins == 0
        assert bool(value.flags & abi.PY_FLAG_GC_PINNED) == prior_pin
        # The unchanged decref/finalizer tail owns the reference here, and may
        # resurrect it; handoff must not hold a lease or erase its earlier pin.
        value.references -= 1
        assert value.references == 0
        value.references += 1
        finalized.append(value)

    memory.namespace["py_decref"] = finalize
    memory.namespace["_iter_release_owned_root"](slot, handle)
    assert finalized == [memory.item]
    assert memory.item.references == 1
    memory.assert_balanced()
