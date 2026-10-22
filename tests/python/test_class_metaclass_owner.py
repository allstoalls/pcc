"""Execute production relation writers and both graph walkers against counts."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
from pcc.runtime.py import py_abi_constants as abi


ROOT = Path(__file__).resolve().parents[2]
CLASS_SOURCE = ROOT / 'pcc/runtime/py/py_class.py'
SLOTS_SOURCE = ROOT / 'pcc/runtime/py/freestanding_gc_object_slots.py'


def _load_functions(path, names, environment):
    tree = ast.parse(path.read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), environment)


class RelationMemory:
    def __init__(self):
        self.owner, self.old, self.new = 1024, 2048, 3072
        self.classes = {self.owner, self.old, self.new}
        self.words = {value + abi.PYOBJECTHEADER_TYPE_TAG_OFFSET: abi.PY_TYPE_CLASS for value in self.classes}
        self.counts = {value: 1 for value in self.classes}
        self.events = []
        self.on_release = lambda value: None
        self.environment = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
        self.environment.update(
            i64=int, c_ptr=int,
            _ptr_is_class=lambda value: value in self.classes,
            ptr_is_null=lambda value: int(value == 0),
            is_tagged_int=lambda value: 0,
            null=lambda: 0,
            ptr_add=lambda value, offset: value + offset,
            pcc_gc_note_relocation_read=lambda value: value,
            pcc_gc_load_ptr=lambda owner, slot: self.words.get(slot, 0),
            load_ptr=lambda owner, offset: self.words.get(owner + offset, 0),
            load_i32=lambda owner, offset: self.words.get(owner + offset, 0),
            load_i64=lambda owner, offset: self.words.get(owner + offset, 0),
            store_ptr=lambda owner, offset, value: self.words.__setitem__(owner + offset, value),
            store_i64=lambda owner, offset, value: self.words.__setitem__(owner + offset, value),
            pcc_gc_store_ptr=self.store,
            pcc_class_retire_metaclass=lambda owner: self.store(owner, owner + abi.PYCLASSOBJECT_METACLASS_OFFSET, 0),
            py_decref=self.release,
            free=lambda value: self.events.append(('free_payload', value)),
            _bump_class_attr_cache_epoch=lambda: self.events.append(('invalidate_cache', self.owner)),
            pcc_gc_free_object_memory=lambda value: self.events.append(('free_class', value)),
            abi_constant=ABI_CONSTANTS.__getitem__,
            pcc_capi_is_cext_type_tag=lambda tag: 0,
            call_void_ptr_i64_ptr=lambda visitor, slot, role, context: visitor(slot, role, context),
        )
        _load_functions(CLASS_SOURCE, ('py_class_set_metaclass', 'py_class_dealloc'), self.environment)
        _load_functions(SLOTS_SOURCE, ('_visit_slot', '_visit_class_slots', '_has_no_pointer_slots', 'pcc_gc_visit_object_slots_slice'), self.environment)

    @property
    def slot(self):
        return self.owner + abi.PYCLASSOBJECT_METACLASS_OFFSET

    def store(self, owner, slot, value):
        assert owner in self.classes
        prior = self.words.get(slot, 0)
        if prior == value:
            return
        if value:
            self.counts[value] += 1
        self.words[slot] = value
        self.release(prior)

    def release(self, value):
        if value:
            self.counts[value] -= 1
            assert self.counts[value] >= 0
            self.events.append(('release', value))
            self.on_release(value)

    def assign(self, value, owner=None):
        self.environment['py_class_set_metaclass'](self.owner if owner is None else owner, value)


def test_metaclass_relation_replacement_owns_one_reference():
    memory = RelationMemory()
    memory.assign(memory.old)
    assert memory.counts[memory.old] == 2
    memory.on_release = lambda value: memory.events.append(('observed_relation', memory.words[memory.slot]))
    memory.assign(memory.new)
    assert memory.counts == {memory.owner: 1, memory.old: 1, memory.new: 2}
    assert ('observed_relation', memory.new) in memory.events
    memory.assign(0)
    assert memory.counts[memory.new] == 1
    assert memory.words[memory.slot] == 0


def test_metaclass_identity_replacement_is_count_neutral():
    memory = RelationMemory()
    memory.assign(memory.old)
    memory.events.clear()
    memory.assign(memory.old)
    assert memory.counts[memory.old] == 2
    assert memory.events == []


@pytest.mark.parametrize('owner,value', [(9999, 2048), (1024, 9999)])
def test_invalid_relation_does_not_change_owners(owner, value):
    memory = RelationMemory()
    before = dict(memory.counts)
    memory.assign(value, owner)
    assert memory.counts == before
    assert memory.words.get(memory.slot, 0) == 0


def test_class_retirement_detaches_metaclass_before_nested_cleanup():
    memory = RelationMemory()
    memory.assign(memory.old)
    observed = []
    memory.on_release = lambda value: observed.append((memory.words[memory.slot], ('invalidate_cache', memory.owner) in memory.events))
    memory.environment['py_class_dealloc'](memory.owner)
    assert observed == [(0, True)]
    assert memory.counts[memory.old] == 1
    assert memory.events[-1] == ('free_class', memory.owner)


@pytest.mark.parametrize('limit', [1, 2, 99])
def test_complete_and_cursor_walkers_agree_on_counted_metaclass(limit):
    memory = RelationMemory()
    memory.assign(memory.old)
    full = []
    memory.environment['_visit_class_slots'](memory.owner, lambda slot, role, ctx: full.append((slot, role)), 0)
    sliced = []
    cursor, state = 0, 8192
    while cursor >= 0:
        assert memory.environment['pcc_gc_visit_object_slots_slice'](
            memory.owner, cursor, limit, lambda slot, role, ctx: sliced.append((slot, role)), 0, state,
        ) == 1
        cursor = memory.words[state]
    assert sliced == full
    assert (memory.slot, 1) in full
    assert all(role == 1 for slot, role in full if slot == memory.slot)


def test_class_immortality_is_still_explicitly_preserved():
    tree = ast.parse(CLASS_SOURCE.read_text())
    constructor = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'py_class_new')
    assert any(isinstance(node, ast.Name) and node.id == 'PY_FLAG_IMMORTAL' for node in ast.walk(constructor))


def test_class_retirement_keeps_relation_owned_across_cache_safepoint():
    memory = RelationMemory()
    memory.assign(memory.old)
    relocated = 4096

    def invalidate_and_relocate():
        # The cache helper is a semantic runtime function and its actual IR
        # contains a safepoint. A detached raw local is not a moving owner.
        assert memory.words[memory.slot] == memory.old
        memory.events.append(('invalidate_cache', memory.owner))
        memory.counts[relocated] = memory.counts.pop(memory.old)
        memory.classes.remove(memory.old)
        memory.classes.add(relocated)
        memory.words[memory.slot] = relocated

    memory.environment['_bump_class_attr_cache_epoch'] = invalidate_and_relocate
    memory.environment['py_class_dealloc'](memory.owner)
    assert memory.words[memory.slot] == 0
    assert memory.counts[relocated] == 1
    assert ('release', relocated) in memory.events
    assert memory.events[-1] == ('free_class', memory.owner)


def assert_metaclass_retirement_slot_ir(text):
    import re

    match = re.search(r'^define[^\n]*@py_class_dealloc\([^\n]*\).*?^}', text, re.M | re.S)
    assert match
    body = match.group(0)
    assert re.search(r'@pcc_class_retire_metaclass\(ptr %o\)', body)
    # The semantic deallocator never detaches the relation into a raw SSA.
    assert not re.search(r'getelementptr i8, ptr %o, i64 112\b', body)
    return {'retirement_owner': 'pcc_class_retire_metaclass'}


def test_metaclass_self_alias_retains_and_replaces_one_owner():
    memory = RelationMemory()
    memory.assign(memory.owner)
    assert memory.words[memory.slot] == memory.owner
    assert memory.counts[memory.owner] == 2
    memory.assign(memory.owner)
    assert memory.counts[memory.owner] == 2
    memory.assign(memory.new)
    assert memory.counts[memory.owner] == 1
    assert memory.counts[memory.new] == 2
