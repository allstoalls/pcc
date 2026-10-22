"""Run the actual retirement and store-plan bodies against movable owners."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi


ROOT = Path(__file__).resolve().parents[2]


def load_functions(path, names, environment):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == '_CLASS_METACLASS_STORE_PLAN_BYTES':
                nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {node.name for node in nodes if isinstance(node, ast.FunctionDef)} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), environment)


class Retirement:
    def __init__(self, backend, references):
        self.backend = backend
        self.cls, self.meta, self.moved = 1024, 2048, 3072
        self.slot = self.cls + abi.PYCLASSOBJECT_METACLASS_OFFSET
        self.words = {self.slot: self.meta if references else 0,
                      self.meta + abi.PYOBJECTHEADER_TYPE_TAG_OFFSET: abi.PY_TYPE_CLASS,
                      self.meta + abi.PYOBJECTHEADER_FLAGS_OFFSET: abi.PY_FLAG_GC_TRACKED,
                      self.meta + abi.PYOBJECTHEADER_REFCOUNT_OFFSET: references}
        self.references = references
        self.locked = False
        self.retired_address = None
        self.events = []
        self.globals = {'pcc_gc_backend_selected': 8192,
                        'pcc_gc_config_initialized': 8200,
                        'pcc_diagnostics_runtime_log_fast_state': 8208}
        self.words.update({8192: backend, 8200: 1, 8208: 0})
        env = {name: value for name, value in vars(abi).items() if name.isupper()}
        env.update(
            i64=int, c_ptr=int, null=lambda: 0, ptr_is_null=lambda p: int(p == 0),
            is_tagged_int=lambda p: 0, ptr_eq=lambda a,b: int(a == b),
            ptr_add=lambda p,n: p+n, global_addr=self.globals.__getitem__,
            load_ptr=self.load, load_i32=self.load, load_i64=self.load,
            store_ptr=self.store, store_i32=self.store, store_i64=self.store,
            stack_alloc=lambda n: 10000 if n == 128 else pytest.fail('unexpected layout'),
            memset=self.zero, pcc_py_gc_minor_graph_lock=self.lock,
            pcc_py_gc_minor_graph_unlock=self.unlock,
            pcc_gc_note_store=self.note_store,
            _gc_forwarding_population=lambda: 0,
            _gc4_old_slot_is_releasable=lambda value: 1,
            pcc_gc_note_slot_write_barrier=self.barrier,
            _refcount_provenance_probe_enabled=lambda: 0,
            pcc_capi_is_cext_type_tag=lambda tag: 0,
            pcc_gc_object_is_known=lambda value: 1,
            pcc_refcount_decref=self.decrement,
            py_weakref_invalidate=lambda value: self.events.append(('weakref', value)),
            pcc_gc_note_object_freeing=lambda value: self.events.append(('freeing', value)),
            py_gc_untrack=lambda value: self.events.append(('untrack', value)),
            pcc_refcount_forget=lambda value: self.events.append(('forget', value)),
            pcc_dealloc_with_trash=self.deallocate,
        )
        self.env = env
        load_functions(ROOT / 'pcc/runtime/py/py_obj.py', (
            '_py_refcount_prepared_reset', '_py_incref_prepare', '_py_incref_finish',
            '_py_decref_prepare', '_py_decref_finish',
            'pcc_gc_store_root_plan_init', '_pcc_gc_store_plan_commit_locked',
            'pcc_gc_store_ptr_plan_commit_locked', '_pcc_gc_store_plan_finish',
            'pcc_gc_store_ptr_plan_finish',
        ), env)
        load_functions(ROOT / 'pcc/runtime/py/freestanding_class_namespace.py',
                       ('pcc_class_retire_metaclass',), env)

    def load(self, base, offset):
        assert not (self.retired_address is not None
                    and self.retired_address <= base + offset < self.retired_address + 128), 'stale metaclass dereference after unlock'
        return self.words.get(base + offset, 0)

    def store(self, base, offset, value):
        self.words[base + offset] = value

    def zero(self, base, value, size):
        assert self.locked and value == 0 and size == 128
        for offset in range(0, size, 4):
            self.words[base + offset] = 0

    def lock(self):
        assert not self.locked
        assert self.words[self.slot] == (self.meta if self.references else 0)
        self.locked = True
        self.events.append(('lock', self.backend))

    def unlock(self):
        assert self.locked and self.words[self.slot] == 0
        self.locked = False
        self.events.append(('unlock', self.backend))
        if self.references == 2:
            # A surviving metaclass may move once the graph transaction ends.
            # Deferred nonterminal finish must not dereference the raw old SSA.
            for offset in (0, 8, 12):
                self.words[self.moved + offset] = self.words[self.meta + offset]
            self.retired_address = self.meta
        elif self.references == 1:
            assert self.words[self.meta + abi.PYOBJECTHEADER_FLAGS_OFFSET] & 524288

    def note_store(self):
        assert self.locked

    def barrier(self, owner, slot, value):
        assert self.locked and (owner, slot, value) == (self.cls, self.slot, 0)
        self.events.append(('barrier', self.backend))

    def decrement(self, value):
        assert self.locked and self.words[self.slot] == 0
        assert value == self.meta
        self.words[value] -= 1
        self.events.append(('consume', value))
        return self.words[value]

    def deallocate(self, value, tag):
        assert not self.locked and self.words[self.slot] == 0
        assert (value, tag) == (self.meta, abi.PY_TYPE_CLASS)
        assert self.words[value] == 0
        assert self.words[value + abi.PYOBJECTHEADER_FLAGS_OFFSET] & 524288
        self.events.append(('deallocate', value))


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('references', [0, 1, 2], ids=['empty', 'terminal', 'survives-and-moves'])
def test_metaclass_retirement_uses_actual_transaction_and_deferred_finish(backend, references):
    model = Retirement(backend, references)
    model.env['pcc_class_retire_metaclass'](model.cls)
    assert not model.locked and model.words[model.slot] == 0
    assert ('barrier', backend) in model.events if backend else ('barrier', backend) not in model.events
    if references:
        assert model.events.index(('consume', model.meta)) < model.events.index(('unlock', backend))
    assert (('deallocate', model.meta) in model.events) == (references == 1)
    if references == 2:
        assert model.words[model.moved] == 1
