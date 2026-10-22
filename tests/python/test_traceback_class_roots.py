"""Hostile production-body models for traceback cache and raw ABI boundaries.

Native execution of the original pooled traceback fixture remains a separate
qualification gate; these models make relocation/reentry schedules repeatable.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from test_foreign_address_leases import _functions

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/runtime/py/py_exc_traceback.py"


class Model:
    def __init__(self):
        self.memory = {}
        self.objects = {}
        self.roots = set()
        self.registrations = Counter()
        self.leases = Counter()
        self.next_address = 1000
        self.depth = 0
        self.mutex = False
        self.constructed = []
        self.destroyed = []
        self.error = None
        self.construct_hook = None
        self.fail_registration = False
        self.fail_allocation = False
        self.fail_lock = False
        self.moves = 0
        self.ns = dict(
            C_POINTER_SIZE=8, PY_FLAG_IMMORTAL=1, _TB_VALUE=8,
            null=lambda: 0, ptr_is_null=lambda x: int(x == 0),
            ptr_eq=lambda x, y: int(x == y), ptr_add=lambda p, n: p + n,
            cstr=lambda s: s, global_addr=lambda s: s,
            load_i32=self.load, load_ptr=self.load, store_i32=self.store,
            store_ptr=self.store, pcc_gc_load_ptr=lambda owner, slot: self.load(slot),
            pcc_gc_store_root=self.root_store,
            pcc_py_gc_minor_graph_lock=self.graph_lock,
            pcc_py_gc_minor_graph_unlock=self.graph_unlock,
            pcc_gc_scheduler_root_register_handle=self.register,
            pcc_mutex_lock=self.lock, pcc_mutex_unlock=self.unlock,
            _tb_cache_mutex=lambda: 99,
            py_runtime_error_if_unset_abi=self.set_error,
            py_class_new=self.construct, atomic_rmw_i32=self.atomic,
            pcc_gc_note_slot_write_barrier=lambda owner, slot, value: self.check(value),
            py_cleanup_one_root_preserving_exception=self.clear,
            pcc_gc_foreign_lease_acquire=self.acquire,
            py_cleanup_one_lease_preserving_exception=self.release,
            py_instance_new=self.instance, py_instance_setattr=self.setattr,
        )
        _functions(SOURCE, {
            '_tb_error', '_tb_adopt', '_tb_cache_class', '_tb_discard_class',
            '_tb_new_instance', '_tb_set_slot', '_tb_set_value',
        }, self.ns)

    def alloc(self):
        self.next_address += 100
        return self.next_address

    def root(self, value=0):
        slot = self.alloc()
        self.roots.add(slot)
        self.memory[slot] = value
        return slot

    def object(self, kind='class', immortal=True):
        value = self.alloc()
        self.objects[value] = (kind, len(self.constructed))
        self.memory[value + 12] = int(immortal)
        return value

    def check(self, value):
        if value:
            assert value in self.objects, 'stale pointer crossed relocation'

    def load(self, base, offset=0):
        return self.memory.get(base + offset if isinstance(base, int) else base, 0)

    def store(self, base, offset, value):
        self.memory[base + offset] = value

    def move(self):
        if self.depth:
            return
        pinned = {self.load(slot) for slot, count in self.leases.items() if count}
        for old in list(self.objects):
            if old in pinned:
                continue
            new = self.alloc()
            self.objects[new] = self.objects.pop(old)
            self.memory[new + 12] = self.memory.pop(old + 12)
            for slot in self.roots:
                if self.load(slot) == old:
                    self.memory[slot] = new
            self.moves += 1

    def graph_lock(self):
        self.move()
        self.depth += 1

    def graph_unlock(self):
        assert self.depth > 0
        self.depth -= 1

    def lock(self, mutex):
        self.move()
        assert not self.mutex, 'constructor reentered while cache lock held'
        if self.fail_lock:
            self.fail_lock = False
            return -1
        self.mutex = True
        return 0

    def unlock(self, mutex):
        assert self.mutex
        self.mutex = False
        return 0

    def register(self, slot):
        assert self.mutex
        assert self.load(slot) == 0, 'cache must register empty'
        if self.fail_registration:
            self.fail_registration = False
            return 0
        self.registrations[slot] += 1
        self.roots.add(slot)
        return 1

    def root_store(self, slot, value):
        self.check(value)
        assert slot in self.roots
        self.memory[slot] = value

    def construct(self, name, bases, n_bases, fields, n_fields):
        assert not self.mutex and not self.depth
        assert bases == n_bases == fields == n_fields == 0
        self.move()
        if self.construct_hook:
            hook, self.construct_hook = self.construct_hook, None
            hook()
        if self.fail_allocation:
            self.fail_allocation = False
            return 0
        value = self.object()
        self.constructed.append(self.objects[value])
        return value

    def atomic(self, operation, value, offset, mask, order):
        assert operation == 'and' and self.depth > 0
        self.check(value)
        assert mask == ~1
        self.memory[value + offset] &= mask

    def clear(self, slot):
        self.move()
        old = self.load(slot)
        self.check(old)
        self.memory[slot] = 0
        if old and not (self.load(old, 12) & 1):
            assert all(self.load(root) != old for root in self.roots)
            self.destroyed.append(self.objects.pop(old))

    def set_error(self, helper, message):
        if self.error is None:
            self.error = message

    def acquire(self, slot):
        self.move()
        self.check(self.load(slot))
        self.leases[slot] += 1
        return 1

    def release(self, slot, token):
        assert token == 1 and self.leases[slot] > 0
        self.leases[slot] -= 1
        self.move()

    def instance(self, cls):
        self.move()
        self.check(cls)
        assert cls in {self.load(s) for s, count in self.leases.items() if count}
        return self.object('instance', immortal=False)

    def setattr(self, obj, name, value):
        self.move()
        self.check(obj)
        self.check(value)
        pinned = {self.load(s) for s, count in self.leases.items() if count}
        assert obj in pinned and value in pinned
        return 0

    def cache(self, cache, registered, destination, candidate):
        return self.ns['_tb_cache_class'](cache, registered, 'traceback', destination, candidate)


def test_cache_survives_relocation_and_registers_once():
    m = Model()
    destination, candidate = m.root(), m.root()
    assert m.cache(700, 800, destination, candidate) == 0
    winner = m.objects[m.load(destination)]
    m.clear(destination)
    m.move()
    assert m.cache(700, 800, destination, candidate) == 0
    assert m.objects[m.load(destination)] == winner
    assert m.load(destination) == m.load(700)
    assert m.registrations == {700: 1}
    assert len(m.constructed) == 1 and not m.destroyed
    assert not m.mutex and m.depth == 0 and m.moves > 0


def test_reentrant_constructor_publishes_once_and_retires_loser():
    m = Model()
    outer, candidate = m.root(), m.root()
    nested, nested_candidate = m.root(), m.root()
    m.construct_hook = lambda: m.cache(700, 800, nested, nested_candidate)
    assert m.cache(700, 800, outer, candidate) == 0
    assert m.load(outer) == m.load(nested) == m.load(700)
    assert m.registrations == {700: 1}
    assert len(m.constructed) == 2 and len(m.destroyed) == 1
    assert not m.load(candidate) and not m.load(nested_candidate)
    assert not m.mutex and m.depth == 0


@pytest.mark.parametrize('failure', ['fail_registration', 'fail_allocation', 'fail_lock'])
def test_cache_failure_allows_retry(failure):
    m = Model()
    destination, candidate = m.root(), m.root()
    setattr(m, failure, True)
    assert m.cache(700, 800, destination, candidate) == -1
    assert not m.mutex and m.depth == 0 and m.error
    m.error = None
    assert m.cache(700, 800, destination, candidate) == 0
    assert m.load(destination) == m.load(700)
    assert m.registrations == {700: 1}


def test_instance_constructor_leases_class_and_roots_new_result():
    m = Model()
    slots = 20000
    m.roots.update({slots, slots + 8})
    m.memory[slots] = m.object()
    assert m.ns['_tb_new_instance'](slots, 0, 1) == 0
    m.check(m.load(slots + 8))
    assert not any(m.leases.values())


def test_attribute_store_leases_both_reloaded_operands():
    m = Model()
    slots = 20000
    m.roots.update({slots, slots + 8})
    m.memory[slots] = m.object('instance', immortal=False)
    m.memory[slots + 8] = m.object('value', immortal=False)
    assert m.ns['_tb_set_slot'](slots, 0, 'field', 1) == 0
    assert not any(m.leases.values())


def test_no_implicit_poll_build_parity_and_early_borrowed_publication():
    build = (ROOT / 'pcc/frontends/python/owned_runtime_build.py').read_text()
    make = (ROOT / 'pcc/runtime/Makefile').read_text()
    assert '"py_cleanup_runtime", "py_exc_traceback"' in build
    rule = make.split('$(OBJDIR_PY)/py_exc_traceback.o:', 1)[1].split('\n\n', 1)[0]
    assert 'PCC_IMPLICIT_THREAD_SAFEPOINTS=0' in rule
    tree = ast.parse(SOURCE.read_text())
    body = next(n.body for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'py_exc_traceback_object')
    calls = [n.value for n in body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert calls[0].func.id == 'store_ptr'
    assert calls[1].func.id == 'pcc_gc_frame_enter'


def test_publication_lock_failure_retires_unpublished_candidate():
    m = Model()
    destination, candidate = m.root(), m.root()
    m.construct_hook = lambda: setattr(m, 'fail_lock', True)
    assert m.cache(700, 800, destination, candidate) == -1
    assert not m.load(700) and not m.load(candidate)
    assert len(m.destroyed) == 1
    assert not m.mutex and m.depth == 0
    assert m.cache(700, 800, destination, candidate) == 0


def test_emitted_ir_preserves_slot_and_no_poll_boundaries(tmp_path, monkeypatch):
    import re
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module

    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    monkeypatch.setenv('PCC_PY_FRONTEND_IR_CACHE', '0')
    output = tmp_path / 'traceback.ll'
    _compile_runtime_module('py_exc_traceback', str(SOURCE), str(output), 'x86_64-unknown-linux-gnu')
    ir = output.read_text()

    def body(symbol):
        match = re.search(r'^define[^\n]*@' + symbol + r'\([^\n]*\)[^\n]*\{\n(.*?)^}', ir, re.M | re.S)
        assert match, symbol
        return match.group(1)

    entry = body('py_exc_traceback_object')
    assert entry.index('store ptr %exc') < entry.index('call ')
    assert '@pcc_gc_root_move(' in entry
    assert '@pcc_thread_safepoint(' not in ir.replace('declare external void @pcc_thread_safepoint()', '')
    cache = body('user_py_exc_traceback__tb_cache_class')
    assert '@pcc_gc_scheduler_root_register_handle(' in cache
    allocation = re.search(r'(%[\w.]+) = call ptr[^\n]*@py_class_new\([^\n]*\)\n([^\n]+)', cache)
    assert allocation and 'store ptr ' + allocation.group(1) in allocation.group(2)
    constructor = body('user_py_exc_traceback__tb_new_instance')
    assert constructor.index('@pcc_gc_foreign_lease_acquire(') < constructor.index('@py_instance_new(')
    assert constructor.index('@py_instance_new(') < constructor.index('@py_cleanup_one_lease_preserving_exception(')
    disposal = body('user_py_exc_traceback__tb_discard_class')
    assert disposal.index('@pcc_py_gc_minor_graph_lock(') < disposal.index('@pcc_gc_load_ptr(')
    assert disposal.index('atomicrmw and') < disposal.index('@pcc_py_gc_minor_graph_unlock(')
    assert disposal.index('@pcc_py_gc_minor_graph_unlock(') < disposal.index('@py_cleanup_one_root_preserving_exception(')
