"""Production declaration snapshots under independently moving child owners.

These deterministic runtime-body tests do not replace native five-GC replay.
"""
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_dict_super_slot_roots import DictMemory
from tests.python.test_set_call_slot_roots import Object


RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_func.py'
PHASES = ('register', 'copy', 'acquire', 'callback', 'release', 'drop', 'unregister', 'lock')


class DefaultsMemory(DictMemory):
    def __init__(self, phase='none', failure=None, cleanup_error=False):
        super().__init__('none')
        self.failure = None
        self.cleanup_error = False
        self.active_test = False
        self.closing = False
        self.frames = []
        self.replace_on_allocate = False
        self.ns.update(c_ptr=object, py_tuple_new=self.allocate_tuple,
                       py_tuple_set_item=self.assign_tuple,
                       pcc_gc_store_root=self.store_root,
                       pcc_gc_foreign_lease_acquire=self.acquire,
                       global_addr=lambda name: name,
                       pcc_gc_frame_enter=self.frame_enter,
                       pcc_gc_frame_leave=self.frame_leave,
                       pcc_gc_unpin=self.unpin,
                       py_tls_exc_swap_slot=self.swap_error,
                       py_obj_call=lambda *_: pytest.fail('copy invoked a factory'))
        names = {'_checked_func', '_is_tuple', '_func_runtime_error_if_unset',
                 '_signature_diagnostic_copy_signature', 'py_func_copy_default_slots',
                 'py_func_release_default_snapshot',
                 'py_func_copy_signature_defaults_slots'}
        body = []
        for node in ast.parse(RUNTIME.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id.startswith(('_FUNC_DEFAULTS_', '_FUNC_SNAPSHOT_RELEASE_'))
                    for target in node.targets):
                body.append(node)
            elif isinstance(node, ast.FunctionDef) and (
                    node.name.startswith('_func_defaults_') or node.name in names):
                node.decorator_list = []
                body.append(node)
        exec(compile(ast.Module(body, []), str(RUNTIME), 'exec'), self.ns)
        token = self.make('token', abi.PY_TYPE_USER_CLASS_START)
        factory = self.make('factory', abi.PY_TYPE_FUNC)
        defaults = self.wrap((self.none, self.none, token, factory))
        defaults.value = 'defaults'
        signature = self.wrap((self.none, self.none, self.none, self.none, defaults))
        signature.value = 'signature'
        captures = self.wrap((self.none, signature))
        captures.value = 'captures'
        function = self.make('function', abi.PY_TYPE_FUNC)
        function.fields[64] = captures
        captures.refs += 1
        self.caller = self.input_roots((function, None, None))
        self.output = self.add(self.caller, 8)
        self.value_output = self.add(self.caller, 16)
        self.baseline = self.references()
        self.phase, self.failure, self.cleanup_error = phase, failure, cleanup_error
        if failure and failure.startswith('register'):
            self.fail_register = int(failure.removeprefix('register'))
        if failure and failure.startswith('copy'):
            self.fail_copy = int(failure.removeprefix('copy'))
        self.allocations = 0
        self.active_test = True

    def references(self):
        return {obj.value: obj.refs for obj in self.objects if obj.alive}

    def allocate_tuple(self, size):
        self.collect('callback')
        if self.replace_on_allocate:
            self.replace_on_allocate = False
            function = self.read(self.caller, 0)
            captures = self.read(function, 64)
            signature = self.read(captures, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 8)
            replacement = self.wrap((self.none, self.none, 'replacement', 'replacement_factory'))
            replacement.value = 'replacement_vector'
            if self.failure == 'replace_signature':
                new_signature = self.wrap((self.none, self.none, self.none, self.none, replacement))
                new_signature.value = 'replacement_signature'
                self.store_field(captures, self.add(captures, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 8), new_signature)
            else:
                self.store_field(signature, self.add(signature, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 4 * 8), replacement)
        self.allocations += 1
        if self.failure == 'allocation':
            self.error = (15, 'snapshot allocation failure')
            return None
        result = super().new_tuple(size)
        result.value = 'snapshot'
        return result

    def assign_tuple(self, obj, index, value):
        self.collect('callback')
        assert obj.alive and obj.leases > 0
        assert not isinstance(value, Object) or value.alive and value.leases > 0
        if self.failure == 'fill' and index == 2:
            self.error = (15, 'snapshot fill failure')
            return
        super().set_tuple(obj, index, value)

    def acquire(self, slot):
        if self.active_test and self.failure == 'result_lease' and slot is self.output:
            self.collect('acquire')
            return -1
        return super().acquire(slot)

    def drop_reference(self, value):
        if not isinstance(value, Object):
            return
        assert value.alive and value.refs > 0
        value.refs -= 1
        if value.refs == 0:
            for child in tuple(value.fields.values()):
                if isinstance(child, Object):
                    self.drop_reference(child)
            value.fields.clear()
            value.alive = False

    def store_root(self, slot, value):
        self.collect('drop')
        old = self.read(slot, 0)
        self.write(slot, 0, value)
        self.drop_reference(old)
        if isinstance(value, Object):
            value.refs += 1
        if self.active_test and self.cleanup_error and self.closing:
            self.error = (7, 'cleanup replaced error')

    def swap_error(self, slot):
        super().swap_error(slot)
        self.closing = not self.closing

    def frame_enter(self, name, slots):
        assert name == 'pcc_func_snapshot_release_frame_map'
        handles = [self.register(self.add(slots, index * 8)) for index in range(2)]
        assert all(handle is not None for handle in handles)
        self.frames.append((slots, handles))

    def frame_leave(self, slots):
        expected, handles = self.frames.pop()
        assert expected is slots
        for handle in reversed(handles):
            self.unregister(handle)

    def unpin(self, value):
        assert value.alive and value.leases > 0
        value.leases -= 1
        self.collect('unpin')

    def run(self):
        return self.ns['py_func_copy_signature_defaults_slots'](self.caller, self.output)

    def declaration_source(self):
        function = self.read(self.caller, 0)
        captures = self.read(function, 64)
        signature = self.read(captures, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 8)
        return self.read(signature, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 4 * 8)

    def assert_released(self):
        assert len(self.roots) == 3 and self.depth == 0 and not self.frames
        assert all(obj.leases == (1000 if obj is self.none else 0)
                   for obj in self.objects if obj.alive)

    def discard_outputs(self):
        self.cleanup_error = False
        self.store_root(self.value_output, None)
        self.store_root(self.output, None)
        self.assert_released()
        assert self.references() == self.baseline


@pytest.mark.parametrize('phase', PHASES)
def test_snapshot_and_reads_keep_children_owned_across_movement(phase):
    memory = DefaultsMemory(phase)
    assert memory.run() == 0 and memory.error is None
    snapshot = memory.read(memory.output, 0)
    assert snapshot is not memory.declaration_source()
    assert memory.unwrap(snapshot) == (None, None, 'token', 'factory')
    memory.assert_released()
    assert memory.ns['py_func_copy_default_slots'](memory.output, 3, memory.value_output) == 0
    snapshot = memory.read(memory.output, 0)
    assert memory.read(memory.value_output, 0) is memory.read(snapshot, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 3 * 8)
    # The declaration owns a fresh tuple, independent of changes to the
    # source signature's vector, while individual default identities agree.
    source = memory.declaration_source()
    original = memory.read(source, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 2 * 8)
    assert memory.read(snapshot, abi.PYTUPLEOBJECT_ITEMS_OFFSET + 2 * 8) is original
    memory.discard_outputs()
    assert memory.collections > 0


@pytest.mark.parametrize('failure', [
    'register1', 'register2', 'register3', 'register4', 'register5',
    'copy1', 'copy2', 'copy3', 'copy4', 'copy5', 'copy6', 'copy7',
    'allocation', 'result_lease', 'fill',
])
def test_snapshot_failure_balances_every_partial_owner(failure):
    memory = DefaultsMemory('release', failure=failure)
    assert memory.run() == -1 and memory.error is not None
    memory.assert_released()
    memory.discard_outputs()


def test_snapshot_cleanup_restores_the_original_error():
    memory = DefaultsMemory('drop', failure='fill', cleanup_error=True)
    assert memory.run() == -1
    assert memory.error == (15, 'snapshot fill failure')
    memory.discard_outputs()


@pytest.mark.parametrize('index', (-1, 4, 20))
def test_declaration_read_rejects_invalid_indices_without_owners(index):
    memory = DefaultsMemory('lock')
    assert memory.run() == 0
    assert memory.ns['py_func_copy_default_slots'](memory.output, index, memory.value_output) == -1
    assert memory.read(memory.value_output, 0) is None
    memory.discard_outputs()


def test_snapshot_preserves_preexisting_error_without_allocating():
    memory = DefaultsMemory()
    memory.error = (2, 'earlier error')
    assert memory.run() == -1
    assert memory.error == (2, 'earlier error')
    assert memory.registrations == memory.allocations == 0
    memory.discard_outputs()


@pytest.mark.parametrize('kind', ('replace_signature', 'replace_vector'))
def test_snapshot_survives_source_replacement_during_allocation(kind):
    memory = DefaultsMemory('callback', failure=kind)
    memory.replace_on_allocate = True
    assert memory.run() == 0
    assert memory.unwrap(memory.read(memory.output, 0)) == (None, None, 'token', 'factory')
    assert memory.unwrap(memory.declaration_source()) == (None, None, 'replacement', 'replacement_factory')
    memory.assert_released()


def test_declaration_output_cannot_overwrite_its_source_owner():
    memory = DefaultsMemory('copy')
    assert memory.run() == 0
    snapshot = memory.read(memory.output, 0)
    assert memory.ns['py_func_copy_default_slots'](memory.output, 2, memory.output) == -1
    assert memory.read(memory.output, 0) is snapshot
    memory.discard_outputs()


@pytest.mark.parametrize('phase', ('register', 'lock', 'unpin', 'drop', 'unregister'))
@pytest.mark.parametrize('keep_alias', (False, True))
def test_prepared_snapshot_release_roots_owner_before_unpin(phase, keep_alias):
    memory = DefaultsMemory()
    assert memory.run() == 0
    snapshot = memory.read(memory.output, 0)
    snapshot.leases += 1
    memory.write(memory.output, 0, None)  # transfer the pinned NEW owner
    if keep_alias:
        snapshot.refs += 1
        memory.write(memory.value_output, 0, snapshot)
    memory.phase = phase
    memory.error = (2, 'metaclass construction failed')
    memory.cleanup_error = True
    memory.ns['py_func_release_default_snapshot'](snapshot)
    assert memory.error == (2, 'metaclass construction failed')
    memory.assert_released()
    if keep_alias:
        assert memory.unwrap(memory.read(memory.value_output, 0)) == (None, None, 'token', 'factory')
    memory.discard_outputs()
