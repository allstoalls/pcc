"""Execute namespace writer bodies with moving owners and deferred cleanup.

These models cover the writer API and its locked transaction helpers. They do
not qualify legacy class lookup, raw method tables, or descriptor dispatch.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
LOCKED_NAMES = (
    '_class_namespace_source_locked', '_class_namespace_copy_locked',
    'pcc_class_namespace_acquire_slots', 'pcc_class_namespace_install_slots',
)
WRITER_NAMES = (
    '_class_write_acquire_namespace', '_class_write_namespace_body',
    '_class_write_namespace_commit',
    'py_class_write_namespace_slots',
)


def load_functions(path, names, environment):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id.startswith('_CLASS_WRITE_')
                   for t in node.targets):
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(nodes, [])), str(path), 'exec'), environment)


class NamespaceWriterModel:
    def __init__(self, *, existing=True, remove=False, failure=None,
                 creation_race=False, retarget=False, relocate=False, alias=False):
        self.memory = {}
        self.next_address = 10000
        self.locked = False
        self.registered = set()
        self.leased = set()
        self.events = []
        self.plans = {}
        self.pending = 'incoming-error'
        self.saved = None
        self.failure = failure
        self.remove = remove
        self.creation_race = creation_race
        self.retarget = retarget
        self.relocate = relocate
        self.alias = alias
        self.cls = 1000
        self.dictionary = 2000 if existing else 0
        self.value = self.cls if alias else 3000
        self.key = 4000
        self.created = 5000
        self.winner = 6000
        self.name = 7000
        self.input_class = 8000
        self.input_value = 8016
        self.namespace_values = {2000: 3333, 6000: 4444}
        self.mutations = []
        self.count = 0
        self.env = self.environment()
        self.store(self.input_class, 0, self.cls)
        self.store(self.input_value, 0, self.value)
        self.store(self.cls, self.env['PYOBJECTHEADER_TYPE_TAG_OFFSET'], self.env['PY_TYPE_CLASS'])
        self.store(self.cls, self.env['PYCLASSOBJECT_ATTRS_OFFSET'], self.dictionary)
        for value in (2000, self.created, self.winner):
            self.store(value, self.env['PYOBJECTHEADER_TYPE_TAG_OFFSET'], self.env['PY_TYPE_DICT'])
        self.registered.update((self.input_class, self.input_value))
        load_functions(ROOT / 'pcc/runtime/py/freestanding_class_namespace.py', LOCKED_NAMES, self.env)
        load_functions(ROOT / 'pcc/runtime/py/py_class.py', WRITER_NAMES, self.env)

    def alloc(self, size):
        self.next_address += size + 64
        return self.next_address

    def load(self, base, offset=0):
        return self.memory.get(base + offset, 0)

    def store(self, base, offset, value):
        self.memory[base + offset] = value

    def interrupt(self, reason):
        assert not self.locked
        self.events.append(reason)
        if self.relocate and self.cls == 1000 and reason == 'register':
            old = self.cls
            self.cls = 1100
            for offset in (self.env['PYOBJECTHEADER_TYPE_TAG_OFFSET'], self.env['PYCLASSOBJECT_ATTRS_OFFSET']):
                self.store(self.cls, offset, self.load(old, offset))
            for slot in self.registered:
                if self.load(slot) == old:
                    self.store(slot, 0, self.cls)
            self.memory.pop(old + self.env['PYOBJECTHEADER_TYPE_TAG_OFFSET'])
            self.events.append('class-moved')

    def environment(self):
        constants = {}
        for node in ast.parse((ROOT / 'pcc/runtime/py/py_abi_constants.py').read_text()).body:
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
            self.locked = False
            self.events.append('unlock')

        def opened(slots, tokens, handles):
            for i in range(14):
                self.store(slots, i * 8, 0)
                self.store(tokens, i * 8, 0)
            for i in range(14):
                self.interrupt('register')
                if self.failure == 'registration' and i == 3:
                    self.count = i
                    return i
                self.registered.add(slots + i * 8)
            self.count = 14
            return 14

        def copy(slots, tokens, index, source, borrowed):
            assert borrowed == 0 and source in self.registered
            self.interrupt('copy')
            if self.failure == 'input-copy' and index == 2:
                return -1
            destination = slots + index * 8
            assert destination in self.registered and self.load(destination) == 0
            self.store(destination, 0, self.load(source))
            self.leased.add(destination)
            self.store(tokens, index * 8, 1)
            return 0

        def adopt(slots, tokens, index):
            slot = slots + index * 8
            assert slot in self.registered
            expected = self.key if index == 4 else self.created
            # Even the interrupt at lease acquisition sees the produced owner.
            assert self.load(slot) in (0, expected)
            self.interrupt('adopt')
            if self.failure == 'adopt' and index == 4:
                return -1
            self.leased.add(slot)
            self.store(tokens, index * 8, 1)
            return 0

        def drop(slots, tokens, index):
            assert not self.locked
            slot = slots + index * 8
            self.events.append('drop-' + str(index))
            self.leased.discard(slot)
            self.store(tokens, index * 8, 0)
            self.store(slot, 0, 0)

        def close(slots, tokens, handles, count, suspended):
            assert not self.locked
            assert count == self.count
            outgoing = self.pending
            for i in range(1, 13):
                drop(slots, tokens, i)
                if self.failure == 'cleanup-tls':
                    self.pending = 'disposal-error'
            if suspended:
                self.pending = outgoing if outgoing else self.load(slots)
            for i in range(count):
                self.registered.remove(slots + i * 8)
            self.events.append('closed')

        def swap(slot):
            self.pending, self.memory[slot] = self.load(slot), self.pending

        def error(message):
            assert not self.locked
            if not self.pending:
                self.pending = message
            return -1

        def new_dictionary():
            self.interrupt('allocate-dict')
            if self.creation_race:
                self.store(self.cls, constants['PYCLASSOBJECT_ATTRS_OFFSET'], self.winner)
            if self.failure == 'allocation':
                self.pending = 'allocation-error'
                return 0
            return self.created

        def new_key(name, length):
            self.interrupt('allocate-key')
            return self.key

        def init(plan, owner, backend):
            assert not self.locked
            assert any(self.load(slot) == owner for slot in self.leased)
            self.plans[plan] = 'write'
            self.events.append('init-write')

        def commit(plan, owner, slot, value):
            assert self.locked and self.plans[plan] == 'write'
            assert self.load(slot) == 0
            self.events.append('install')
            if self.failure == 'install':
                return 0
            self.store(slot, 0, value)
            return 1

        def prepare(output, source, borrowed, plan):
            assert self.locked and borrowed == 0
            assert output in self.registered and self.load(output) == 0
            self.plans[plan] = 'copy'
            self.events.append('prepare-copy')
            if self.failure == 'namespace-copy':
                return -1
            self.store(output, 0, self.load(source))
            self.leased.add(output)
            return 1

        def finish(plan):
            assert not self.locked
            self.events.append('finish-' + self.plans.pop(plan))

        def mutate(dict_slot, key_slot, value_slot, context):
            assert not self.locked
            class_slot = self.load(context)
            assert all(slot in self.leased for slot in (dict_slot, key_slot, class_slot))
            assert self.load(class_slot) == self.cls
            dictionary = self.load(dict_slot)
            self.events.append('dict-operation')
            if self.retarget:
                self.retarget = False
                self.store(self.cls, constants['PYCLASSOBJECT_ATTRS_OFFSET'], self.winner)
                return -2
            assert dictionary == self.load(self.cls, constants['PYCLASSOBJECT_ATTRS_OFFSET'])
            if self.failure in ('mutation', 'cleanup-tls'):
                self.pending = 'mutation-error'
                return -1
            if self.remove:
                if dictionary not in self.namespace_values:
                    return 1
                del self.namespace_values[dictionary]
            else:
                assert value_slot in self.leased
                self.namespace_values[dictionary] = self.load(value_slot)
            self.mutations.append(dictionary)
            return 0

        constants.update(dict(
            c_ptr=int, i64=int, null=lambda: 0, ptr_is_null=lambda v: int(v == 0),
            is_tagged_int=lambda v: int(v == 1), ptr_add=lambda p, o: p + o,
            load_ptr=self.load, load_i32=self.load, load_i64=self.load,
            store_ptr=self.store, store_i64=self.store, stack_alloc=self.alloc,
            cstr=lambda s: s, strlen=lambda _: 7, _strs_eq=lambda a,b: int(a == self.name),
            _note_class_defines_del=lambda: self.events.append('del-hint'),
            _special_open=opened, _special_copy=copy, _special_adopt=adopt,
            _special_drop=drop, _special_close=close, _special_error=error,
            py_tls_exc_swap_slot=swap, py_dict_new=new_dictionary, py_str_new=new_key,
            pcc_py_gc_minor_graph_lock=lock, pcc_py_gc_minor_graph_unlock=unlock,
            pcc_gc_backend=lambda: 4, pcc_gc_store_ptr_plan_init=init,
            pcc_gc_store_ptr_plan_commit_locked=commit, pcc_gc_store_ptr_plan_finish=finish,
            pcc_gc_root_copy_lease_prepare_locked=prepare, pcc_gc_root_copy_lease_finish=finish,
            py_dict_namespace_set_slots=mutate,
            py_dict_namespace_del_slots=lambda d,k,c: mutate(d,k,0,c),
        ))
        return constants

    def run(self):
        result = self.env['py_class_write_namespace_slots'](
            self.input_class, self.name, self.input_value, int(self.remove),
        )
        assert not self.locked and not self.leased and not self.plans
        assert self.registered == {self.input_class, self.input_value}
        assert self.events[-1] == 'closed'
        return result


@pytest.mark.parametrize('existing', (False, True))
@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('alias', (False, True))
def test_namespace_writer_publishes_owned_value(existing, relocate, alias):
    model = NamespaceWriterModel(existing=existing, relocate=relocate, alias=alias)
    assert model.run() == 0
    dictionary = model.load(model.cls, model.env['PYCLASSOBJECT_ATTRS_OFFSET'])
    assert model.namespace_values[dictionary] == model.load(model.input_value)
    assert model.pending == 'incoming-error'
    assert model.events.index('del-hint') < model.events.index('dict-operation')
    if relocate:
        assert 'class-moved' in model.events


@pytest.mark.parametrize('existing', (False, True))
def test_namespace_delete_does_not_create_dictionary(existing):
    model = NamespaceWriterModel(existing=existing, remove=True)
    assert model.run() == (0 if existing else 1)
    assert 'allocate-dict' not in model.events
    assert 'del-hint' not in model.events


def test_losing_namespace_creation_selects_winner_without_overwrite():
    model = NamespaceWriterModel(existing=False, creation_race=True)
    assert model.run() == 0
    assert 'install' not in model.events
    assert model.mutations == [model.winner]
    assert model.load(model.cls, model.env['PYCLASSOBJECT_ATTRS_OFFSET']) == model.winner


@pytest.mark.parametrize('remove', (False, True))
def test_namespace_identity_retry_drops_old_owner_and_reacquires(remove):
    model = NamespaceWriterModel(retarget=True, remove=remove)
    assert model.run() == 0
    assert model.mutations == [model.winner]
    operations = [i for i, event in enumerate(model.events) if event == 'dict-operation']
    assert len(operations) == 2
    assert 'drop-3' in model.events[operations[0] + 1:operations[1]]


@pytest.mark.parametrize('failure', (
    'registration', 'input-copy', 'adopt', 'allocation', 'install',
    'namespace-copy', 'mutation', 'cleanup-tls',
))
def test_namespace_write_failure_closes_registered_owners_and_finishes_plans(failure):
    model = NamespaceWriterModel(existing=False, failure=failure)
    assert model.run() == -1
    assert not model.mutations
    if failure in ('mutation', 'cleanup-tls'):
        assert model.pending == 'mutation-error'
    elif failure == 'allocation':
        assert model.pending == 'allocation-error'


@pytest.mark.parametrize('receiver', (0, 1, 9999))
def test_invalid_receiver_is_failure_without_namespace_retry(receiver):
    model = NamespaceWriterModel()
    model.store(model.input_class, 0, receiver)
    assert model.run() == -1
    assert model.events.count('lock') == 1
    assert 'dict-operation' not in model.events


def test_graph_transactions_are_contained_in_freestanding_entries():
    path = ROOT / 'pcc/runtime/py/py_class.py'
    tree = ast.parse(path.read_text())
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in WRITER_NAMES:
            calls = {node.func.id for node in ast.walk(ast.Module(function.body, []))
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            assert 'pcc_py_gc_minor_graph_lock' not in calls
            assert 'pcc_py_gc_minor_graph_unlock' not in calls


class SharedHelperNamespaceWriterModel(NamespaceWriterModel):
    """Also execute the production registration, copy, lease and TLS helpers."""

    def environment(self):
        env = super().environment()
        self.active_slots = 0
        self.registration_count = 0
        self.handle_slots = {}
        self.closed = False

        def memset(base, value, size):
            assert value == 0
            for offset in range(0, size, 8):
                self.store(base, offset, 0)

        def register(slot):
            if not self.active_slots:
                self.active_slots = slot
            self.interrupt('register')
            if self.failure == 'registration' and self.registration_count == 3:
                self.count = self.registration_count
                return 0
            self.registered.add(slot)
            self.registration_count += 1
            self.count = self.registration_count
            handle = self.alloc(8)
            self.handle_slots[handle] = slot
            return handle

        def unregister(handle):
            self.interrupt('unregister')
            self.registered.remove(self.handle_slots.pop(handle))
            if not self.handle_slots:
                self.events.append('closed')

        def acquire(slot):
            assert slot in self.registered
            value = self.load(slot)
            self.interrupt('lease')
            if self.failure == 'adopt' and slot == self.active_slots + 4 * 8:
                return -1
            if value:
                self.leased.add(slot)
                return 1
            return 0

        def release(slot, token):
            self.interrupt('release')
            if token:
                assert slot in self.leased
                self.leased.remove(slot)
            return 0

        def copy(destination, source):
            assert source in self.registered and destination in self.registered
            assert not self.load(destination)
            self.interrupt('copy')
            if self.failure == 'input-copy' and destination == self.active_slots + 2 * 8:
                return -1
            self.store(destination, 0, self.load(source))
            return acquire(destination)

        def store_root(slot, value):
            assert value == 0
            old = self.load(slot)
            assert slot not in self.leased
            assert slot in self.registered or not old
            self.store(slot, 0, 0)
            index = (slot - self.active_slots) // 8
            self.events.append('drop-' + str(index))
            if old and self.failure == 'cleanup-tls':
                self.pending = 'disposal-error'

        def clear():
            self.pending = 0

        def runtime_error(label, message):
            assert not self.locked
            if not self.pending:
                self.pending = message

        def barrier(owner, slot, value):
            assert self.locked and owner == 0
            assert slot in self.registered and self.load(slot) == value

        def abort():
            raise AssertionError('lease invariant aborted')

        env.update({
            'memset': memset, 'pcc_gc_scheduler_root_register_handle': register,
            'pcc_gc_scheduler_root_unregister_handle': unregister,
            'pcc_gc_root_copy_lease': copy,
            'pcc_gc_root_copy_borrowed_lease': lambda *args: pytest.fail('unexpected borrowed input'),
            'pcc_gc_foreign_lease_acquire': acquire,
            'pcc_gc_foreign_lease_release': release,
            'pcc_gc_store_root': store_root, 'pcc_gc_note_slot_write_barrier': barrier,
            'py_clear_exception': clear, 'py_runtime_error_if_unset': runtime_error,
            'pcc_platform_abort': abort,
        })
        load_functions(ROOT / 'pcc/runtime/py/py_class.py', (
            '_special_open', '_special_error', '_special_copy', '_special_adopt',
            '_special_drop', '_special_close',
        ), env)
        return env


@pytest.mark.parametrize('failure', (
    None, 'registration', 'input-copy', 'adopt', 'allocation', 'install',
    'namespace-copy', 'mutation', 'cleanup-tls',
))
def test_namespace_writer_uses_actual_shared_owner_cleanup(failure):
    model = SharedHelperNamespaceWriterModel(existing=False, failure=failure, relocate=True, alias=True)
    assert model.run() == (0 if failure is None else -1)
    assert not model.handle_slots
    if failure is None:
        assert model.pending == 'incoming-error'
    elif failure in ('mutation', 'cleanup-tls'):
        assert model.pending == 'mutation-error'


@pytest.mark.parametrize('remove', (False, True))
def test_namespace_retry_actual_helpers_release_old_owner_before_reacquire(remove):
    model = SharedHelperNamespaceWriterModel(remove=remove, retarget=True)
    assert model.run() == 0
    assert model.mutations == [model.winner]
    assert model.pending == 'incoming-error'


@pytest.mark.parametrize('existing,relocate,retarget', ((False, True, False), (True, True, False), (True, False, True)))
def test_dictionary_key_writer_owns_key_and_reuses_transaction(existing, relocate, retarget):
    model = NamespaceWriterModel(existing=existing, relocate=relocate, retarget=retarget)
    key_input = 8024
    model.store(key_input, 0, model.key)
    model.registered.add(key_input)
    model.env['_ptr_can_have_header'] = lambda _: False
    load_functions(ROOT / 'pcc/runtime/py/py_class.py', ('py_class_write_namespace_key_slots',), model.env)
    result = model.env['py_class_write_namespace_key_slots'](model.input_class, key_input, model.input_value)
    assert result == 0
    assert not model.locked and not model.leased and not model.plans
    assert model.registered == {model.input_class, key_input, model.input_value}
    assert model.namespace_values[model.load(model.cls, model.env['PYCLASSOBJECT_ATTRS_OFFSET'])] == model.value
    assert model.pending == 'incoming-error'
    assert 'del-hint' not in model.events
    assert 'allocate-key' not in model.events
