"""Class namespace notifications share the actual dictionary commit boundary."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def _load_helpers(path, names, environment):
    tree = ast.parse(path.read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id.startswith('_CLASS_NAMESPACE_CONTEXT_')
                   for target in targets):
                selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            selected.append(node)
    assert {node.name for node in selected if isinstance(node, ast.FunctionDef)} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(selected, [])), str(path), 'exec'), environment)


class NamespaceCommitModel:
    """Execute production commit kernels with observable deferred disposal."""

    def __init__(self, operation, invalid_namespace=False, stale=False, failed_plan=False):
        self.operation = operation
        self.invalid_namespace = invalid_namespace
        self.stale = stale
        self.failed_plan = failed_plan
        self.memory = {}
        self.locked = False
        self.events = []
        self.plans = {}
        self.next_address = 20000
        self.epoch = 0
        self.notified = True
        self.dict = 1000
        self.cls = 2000
        self.entries = 3000
        self.indices = 4000
        self.methods = 5000
        self.class_slot = 6000
        self.dict_slot = 6100
        self.key_slot = 6200
        self.value_slot = 6300
        self.context = 7000
        self.name = 8000
        self.key = 9000
        self.old = 10000
        self.new = 11000
        self.raw = 12000
        self.env = self.environment()
        _load_helpers(ROOT / 'pcc/runtime/py/freestanding_class_namespace.py', (
            '_class_namespace_same_name', 'py_class_namespace_validate_locked',
            'py_class_namespace_commit_locked',
        ), self.env)
        _load_helpers(ROOT / 'pcc/runtime/py/py_dict.py', (
            '_dict_insert_rooted_slot', '_dict_replace_value_rooted_slot',
            '_dict_del_rooted_slot',
        ), self.env)
        self.initialize()

    def load(self, base, offset=0):
        return self.memory.get(base + offset, 0)

    def store(self, base, offset, value):
        self.memory[base + offset] = value

    def alloc(self, size):
        self.next_address += size + 100
        return self.next_address

    def initialize(self):
        c = self.env
        self.store(self.class_slot, 0, self.cls)
        self.store(self.context, c['_CLASS_NAMESPACE_CONTEXT_OWNER_SLOT'] * c['C_POINTER_SIZE'], self.class_slot)
        self.store(self.context, c['_CLASS_NAMESPACE_CONTEXT_NAME'] * c['C_POINTER_SIZE'], self.name)
        self.store(self.cls, c['PYOBJECTHEADER_TYPE_TAG_OFFSET'], c['PY_TYPE_CLASS'])
        self.store(self.cls, c['PYCLASSOBJECT_ATTRS_OFFSET'], self.dict + int(self.invalid_namespace))
        self.store(self.cls, c['PYCLASSOBJECT_METHODS_OFFSET'], self.methods)
        self.store(self.cls, c['PYCLASSOBJECT_N_METHODS_OFFSET'], 1)
        self.store(self.cls, c['PYCLASSOBJECT_DEL_METHOD_OFFSET'], self.raw)
        self.store(self.methods, c['PYCLASSMETHOD_NAME_OFFSET'], self.name)
        self.store(self.methods, c['PYCLASSMETHOD_FUNC_OFFSET'], self.raw)
        for index, byte in enumerate(b'__del__\0'):
            self.store(self.name, index, byte)
        self.store(self.dict_slot, 0, self.dict)
        self.store(self.key_slot, 0, self.key)
        self.store(self.value_slot, 0, self.new)
        self.store(self.dict, c['PYDICTOBJECT_ENTRIES_OFFSET'], self.entries)
        self.store(self.dict, c['PYDICTOBJECT_INDICES_OFFSET'], self.indices)
        self.store(self.dict, c['PYDICTOBJECT_CAPACITY_OFFSET'], 8)
        self.store(self.dict, c['PYDICTOBJECT_ENTRIES_USED_OFFSET'], 0 if self.operation == 'insert' else 1)
        self.store(self.dict, c['PYDICTOBJECT_ITEM_COUNT_OFFSET'], 0 if self.operation == 'insert' else 1)
        self.store(self.indices, 0, -1 if self.operation == 'insert' else 0)
        self.store(self.entries, c['DICTENTRY_HASH_OFFSET'], 99)
        self.store(self.entries, c['DICTENTRY_KEY_OFFSET'], 0 if self.operation == 'insert' else self.key)
        self.store(self.entries, c['DICTENTRY_VALUE_OFFSET'], 0 if self.operation == 'insert' else self.old)

    def environment(self):
        constants = {}
        tree = ast.parse((ROOT / 'pcc/runtime/py/py_abi_constants.py').read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        def lock():
            assert not self.locked
            self.locked = True
            self.events.append('lock')
        def unlock():
            assert self.locked
            self.events.append('unlock')
            self.locked = False
        def init(plan, owner, backend):
            assert not self.locked
            self.plans[plan] = []
        def commit(plan, owner, slot, value):
            assert self.locked
            self.events.append('commit')
            if self.failed_plan:
                return 0
            old = self.load(slot)
            self.store(slot, 0, value)
            self.plans[plan].append(old)
            return 1
        def finish(plan):
            assert not self.locked
            self.events.append('finish')
            for old in self.plans.pop(plan):
                if old:
                    assert self.epoch == int(self.notified)
                    expected_alias = 0 if self.notified else self.raw
                    assert self.load(self.methods, constants['PYCLASSMETHOD_FUNC_OFFSET']) == expected_alias
                    assert self.load(self.cls, constants['PYCLASSOBJECT_DEL_METHOD_OFFSET']) == expected_alias
                    expected = 0 if self.operation == 'delete' else self.new
                    assert self.load(self.entries, constants['DICTENTRY_VALUE_OFFSET']) == expected
                    self.events.append('dispose')
        def atomic(op, pointer, offset, value, ordering):
            assert self.locked and op == 'add' and ordering == 'release'
            self.epoch += value
            self.events.append('notify')
        def cstr(value):
            assert value == '__del__'
            return self.name
        def grow(dictionary):
            assert not self.locked
            self.events.append('grow')
            return 0
        constants.update(dict(
            i64=int, c_ptr=int, null=lambda: 0, ptr_is_null=lambda x: int(x == 0),
            ptr_eq=lambda a,b: int(a == b), ptr_add=lambda a,b: a+b,
            int_to_ptr=lambda value: value, load_ptr=self.load, store_ptr=self.store,
            load_i8=self.load, load_i32=self.load, load_i64=self.load,
            store_i64=self.store, stack_alloc=self.alloc, cstr=cstr,
            global_addr=lambda name: 14000, atomic_rmw_i32=atomic,
            _dict_read_reload_root=lambda slot,handle: self.load(slot),
            pcc_gc_backend=lambda: 4, pcc_py_gc_minor_graph_lock=lock,
            pcc_py_gc_minor_graph_unlock=unlock, pcc_gc_store_ptr_plan_init=init,
            pcc_gc_store_ptr_plan_commit_locked=commit, pcc_gc_store_ptr_plan_finish=finish,
            _ptr_is_dict=lambda value: value == self.dict,
            _entry_key=lambda owner,entries,offset: self.load(entries, offset+constants['DICTENTRY_KEY_OFFSET']),
            _maybe_grow=grow,
        ))
        return constants

    def run(self, notified=True):
        self.notified = notified
        context = self.context if notified else 0
        capacity = 16 if self.stale else 8
        if self.operation == 'insert':
            return self.env['_dict_insert_rooted_slot'](
                self.dict_slot, 0, self.key_slot, 0, self.value_slot, 0,
                self.indices, self.entries, capacity, 0, 0, 99, context,
            )
        if self.operation == 'replace':
            return self.env['_dict_replace_value_rooted_slot'](
                self.dict_slot, 0, self.value_slot, 0, self.indices,
                self.entries, capacity, 0, 0, 99, context,
            )
        return self.env['_dict_del_rooted_slot'](
            self.dict_slot, 0, self.indices, self.entries, capacity, 0, 0, context,
        )


@pytest.mark.parametrize('operation', ('insert', 'replace', 'delete'))
def test_namespace_notification_precedes_displaced_owner_disposal(operation):
    model = NamespaceCommitModel(operation)
    assert model.run() == 1
    assert model.epoch == 1 and not model.locked
    assert model.events.index('notify') < model.events.index('unlock') < model.events.index('finish')
    assert model.load(model.methods, model.env['PYCLASSMETHOD_FUNC_OFFSET']) == 0


@pytest.mark.parametrize('operation', ('insert', 'replace', 'delete'))
@pytest.mark.parametrize('failure', ('invalid_namespace', 'stale', 'failed_plan'))
def test_failed_namespace_commit_does_not_notify(operation, failure):
    model = NamespaceCommitModel(operation, **{failure: True})
    before = dict(model.memory)
    assert model.run() == (-2 if failure == 'invalid_namespace' else 0)
    assert model.epoch == 0 and not model.locked
    assert model.load(model.methods, model.env['PYCLASSMETHOD_FUNC_OFFSET']) == model.raw
    assert 'dispose' not in model.events
    if failure == 'invalid_namespace':
        assert model.memory == before
        assert 'commit' not in model.events


def test_namespace_notification_has_no_managed_alias_or_parking_call():
    tree = ast.parse((ROOT / 'pcc/runtime/py/freestanding_class_namespace.py').read_text())
    selected = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    allowed = {'load_ptr','load_i32','load_i8','ptr_is_null','ptr_eq','store_ptr',
               '_class_namespace_same_name','null','cstr','atomic_rmw_i32','global_addr'}
    for name in ('_class_namespace_same_name','py_class_namespace_validate_locked','py_class_namespace_commit_locked'):
        node = selected[name]
        for arg in node.args.args:
            assert isinstance(arg.annotation, ast.Name) and arg.annotation.id == 'c_ptr'
        calls = {part.func.id for part in ast.walk(ast.Module(body=node.body, type_ignores=[]))
                 if isinstance(part, ast.Call) and isinstance(part.func, ast.Name)}
        assert calls <= allowed


@pytest.mark.parametrize('operation', ('insert', 'replace', 'delete'))
def test_ordinary_dictionary_commit_does_not_notify_class(operation):
    model = NamespaceCommitModel(operation)
    assert model.run(notified=False) == 1
    assert model.epoch == 0 and not model.locked
    assert model.load(model.methods, model.env['PYCLASSMETHOD_FUNC_OFFSET']) == model.raw


@pytest.mark.parametrize('operation', ('insert', 'replace', 'delete'))
def test_namespace_context_reloads_relocated_class_owner(operation):
    model = NamespaceCommitModel(operation)
    old_class = model.cls
    model.cls = 15000
    for address, value in tuple(model.memory.items()):
        if old_class <= address < old_class + model.env['PYCLASSOBJECT_SIZE']:
            model.memory[model.cls + address - old_class] = value
            del model.memory[address]
    model.store(model.class_slot, 0, model.cls)
    assert model.run() == 1
    assert model.epoch == 1


@pytest.mark.parametrize('operation', ('insert', 'replace'))
def test_namespace_value_alias_does_not_become_borrowed_method_metadata(operation):
    model = NamespaceCommitModel(operation)
    model.new = model.cls
    model.store(model.value_slot, 0, model.new)
    assert model.run() == 1
    assert model.load(model.methods, model.env['PYCLASSMETHOD_FUNC_OFFSET']) == 0
    assert model.load(model.entries, model.env['DICTENTRY_VALUE_OFFSET']) == model.cls


def test_non_string_namespace_key_invalidates_epoch_without_reading_c_name():
    model = NamespaceCommitModel('insert')
    model.store(model.context, model.env['_CLASS_NAMESPACE_CONTEXT_NAME'] * model.env['C_POINTER_SIZE'], 0)
    assert model.run() == 1
    assert model.epoch == 1 and not model.locked
    assert model.load(model.methods, model.env['PYCLASSMETHOD_FUNC_OFFSET']) == model.raw
    assert model.load(model.cls, model.env['PYCLASSOBJECT_DEL_METHOD_OFFSET']) == model.raw
