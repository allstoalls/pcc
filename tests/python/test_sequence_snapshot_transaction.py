"""Actual staged kernel and split-copy bodies keep allocation outside the lock."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
RUNTIME = ROOT / 'pcc/runtime/py'
PROPOSAL = RUNTIME


def install(path, names, env):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.startswith('_SEQUENCE_SNAPSHOT_'):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(nodes, [])), str(path), 'exec'), env)


class StagedSnapshot:
    def __init__(self, values, *, fail=None, failure_index=1, forwarded=False, replace=False):
        self.values = values
        self.fail = fail
        self.failure_index = failure_index
        self.forwarded = forwarded
        self.replace = replace
        self.source = 1000
        self.source_root = 2000
        self.items = 10000
        self.children = 20000
        self.borrowed = self.children + len(values) * 8
        self.plan_base = 30000
        self.tokens = 50000
        self.memory = {}
        self.refs = {}
        self.leases = {}
        self.plans = {}
        self.locked = False
        self.events = []
        self.owning = {self.children + i * 8 for i in range(len(values))}
        self.borrowed_roots = {self.borrowed}
        self.store(self.source_root, 0, self.source)
        self.store(self.source, 8, 5)
        self.store(self.source, 16, len(values))
        self.store(self.source, 32, self.items)
        self.store(self.borrowed, 0, 0)
        for i, value in enumerate(values):
            self.store(self.items, i * 8, value)
            self.store(self.children, i * 8, 0)
            if value and value % 2 == 0:
                self.refs[value] = self.refs.get(value, 0) + 1
                if forwarded:
                    self.refs[value + 6000] = 1  # Existing forwarding-table owner.
        self.env = dict(c_ptr=int, i64=int, ptr_is_null=lambda p:int(p==0), ptr_eq=lambda a,b:int(a==b),
                        ptr_add=lambda p,o:p+o, is_tagged_int=lambda p:int(p%2), null=lambda:0,
                        load_ptr=self.load, load_i32=self.load, load_i64=self.load,
                        store_ptr=self.store, store_i64=self.store, _gc_backend_fast=lambda:3,
                        pcc_gc_store_root_plan_init=self.plan_init,
                        pcc_gc_store_root_plan_commit_locked=self.commit,
                        pcc_gc_load_borrowed_ptr=self.borrow,
                        pcc_gc_foreign_lease_acquire=self.acquire,
                        pcc_gc_foreign_lease_release=self.release,
                        pcc_gc_store_root_plan_finish=self.finish,
                        pcc_py_gc_minor_graph_lock=self.lock,
                        pcc_py_gc_minor_graph_unlock=self.unlock)
        abi = {'object.list.items_offset':32, 'object.list.length_offset':16,
               'object.pointer.size':8, 'object.header.type_tag_offset':8, 'object.type.list':5}
        self.env['abi_constant'] = abi.__getitem__
        install(RUNTIME / 'py_obj.py', {'pcc_gc_root_copy_lease_prepare_locked', 'pcc_gc_root_copy_lease_finish'}, self.env)
        install(PROPOSAL / 'freestanding_sequence_snapshot.py', {'pcc_list_snapshot_commit_slots'}, self.env)

    def load(self, pointer, offset=0):
        return self.memory.get(pointer + offset, 0)

    def store(self, pointer, offset, value):
        self.memory[pointer + offset] = value

    def lock(self):
        assert not self.locked
        self.locked = True
        self.events.append('lock')

    def unlock(self):
        assert self.locked and self.load(self.borrowed) == 0
        self.locked = False
        self.events.append('unlock')
        if self.replace:
            for i, value in enumerate(self.values):
                self.store(self.items, i * 8, 11)
                if value and value % 2 == 0:
                    self.refs[value] -= 1
            self.events.append('source-replaced')

    def plan_init(self, plan, backend):
        assert self.locked and backend == 3
        self.plans[plan] = {'attempted':False, 'value':0}

    def borrow(self, owner, source):
        assert self.locked and owner == 0 and source in self.borrowed_roots
        value = self.load(source)
        if self.forwarded and value and value % 2 == 0:
            value += 6000
            self.store(source, 0, value)
        self.events.append(('borrow', source, value))
        return value

    def commit(self, plan, destination, value):
        assert self.locked and destination in self.owning
        assert not self.plans[plan]['attempted']
        self.plans[plan]['attempted'] = True
        index = (destination - self.children) // 8
        if self.fail == 'copy' and index == self.failure_index and value != 0:
            return 0
        old = self.load(destination)
        if value and value % 2 == 0:
            self.refs[value] += 1
        if old and old % 2 == 0:
            self.refs[old] -= 1
        self.store(destination, 0, value)
        self.plans[plan]['value'] = value
        self.events.append(('root-barrier', destination, value))
        return 1

    def acquire(self, slot):
        assert self.locked and slot in self.owning
        index = (slot - self.children) // 8
        if self.fail == 'lease' and index == self.failure_index:
            return -1
        value = self.load(slot)
        if not value or value % 2:
            return 0
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        if token:
            assert self.leases.pop(slot) == token
        return 0

    def finish(self, plan):
        assert not self.locked
        self.events.append(('finish', plan))

    def run(self, length=None):
        status = self.env['pcc_list_snapshot_commit_slots'](
            self.source_root, self.children, len(self.values) if length is None else length,
            self.plan_base, self.tokens,
        )
        assert not self.locked and self.load(self.borrowed) == 0
        before = tuple(self.load(self.children, i * 8) for i in range(len(self.values)))
        for i in range(len(self.values)):
            self.env['pcc_gc_root_copy_lease_finish'](self.plan_base + i * 256)
        for i in range(len(self.values)):
            slot = self.children + i * 8
            self.release(slot, self.load(self.tokens, i * 8))
            value = self.load(slot)
            if value and value % 2 == 0:
                self.refs[value] -= 1
            self.store(slot, 0, 0)
        assert not self.leases and all(count >= 0 for count in self.refs.values())
        return status, before


@pytest.mark.parametrize('values', ((), (3000,), (3000,4000), (3000,3000), (11,3000,11)))
@pytest.mark.parametrize('forwarded', (False, True))
@pytest.mark.parametrize('replace', (False, True))
def test_registered_staging_preserves_aliases_and_borrowed_healing(values, forwarded, replace):
    model = StagedSnapshot(values, forwarded=forwarded, replace=replace)
    status, copied = model.run()
    assert status == 0
    assert copied == tuple(value + 6000 if forwarded and value and value % 2 == 0 else value for value in values)
    assert tuple(model.load(model.items, i * 8) for i in range(len(values))) == ((11,) * len(values) if replace else values)
    assert model.events.index('unlock') < next((i for i,event in enumerate(model.events) if isinstance(event,tuple) and event[0]=='finish'), len(model.events))


@pytest.mark.parametrize('failure', ('copy', 'lease'))
@pytest.mark.parametrize('position', (0,1,2))
@pytest.mark.parametrize('forwarded', (False, True))
def test_partial_failure_clears_borrowed_slot_and_balances_all_copied_roots(failure, position, forwarded):
    model = StagedSnapshot((3000,4000,5000), fail=failure, failure_index=position, forwarded=forwarded)
    initial = dict(model.refs)
    status, copied = model.run()
    assert status == -1
    assert copied[:position] == tuple(value + 6000 if forwarded else value for value in model.values[:position])
    assert copied[position:] == (0,) * (3-position)
    assert model.refs == initial


def test_length_mismatch_is_a_no_write_retry():
    model = StagedSnapshot((3000,4000))
    before = dict(model.refs)
    assert model.run(length=1) == (-2,(0,0))
    assert model.refs == before and not model.plans


@pytest.mark.parametrize('shape', ('source_type', 'null_source', 'null_item', 'negative_length', 'null_children'))
def test_invalid_snapshot_shape_does_not_publish_an_owner(shape):
    model = StagedSnapshot((3000,4000))
    if shape == 'source_type': model.store(model.source,8,7)
    elif shape == 'null_source': model.store(model.source_root,0,0)
    elif shape == 'null_item': model.store(model.items,0,0)
    before = dict(model.refs)
    status = model.env['pcc_list_snapshot_commit_slots'](
        model.source_root, 0 if shape == 'null_children' else model.children,
        -1 if shape == 'negative_length' else 2, model.plan_base, model.tokens,
    )
    assert status == -1 and not model.locked
    assert model.load(model.borrowed) == 0
    assert model.refs == before and not model.leases
    assert not model.plans


def test_nonempty_staging_root_is_rejected_without_changing_its_owner():
    model = StagedSnapshot((3000,))
    model.store(model.children,0,4000)
    model.refs[4000] = 1
    before = dict(model.memory)
    assert model.env['pcc_list_snapshot_commit_slots'](model.source_root,model.children,1,model.plan_base,model.tokens) == -1
    assert model.memory == before
    assert not model.leases and model.refs == {3000:1,4000:1}
