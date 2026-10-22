"""Execute the generic struct-sequence owner in the moving slot model.

These are component proofs, not native collector qualification.
"""
from __future__ import annotations
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_set_call_slot_roots import Object, Block
from test_tuple_subclass_slot_models import TupleMemory

PORT = Path(__file__).resolve().parents[2] / 'pcc/runtime/py/py_class.py'


class StructSeqMemory(TupleMemory):
    def __init__(self, phase='', fail_kind=None):
        super().__init__(phase, fail_kind)
        self.maps.update(pcc_structseq_borrowed_map=-3, pcc_structseq_result_map=2)
        self.ns['atomic_rmw_i32'] = self.atomic_or
        self.ns['py_exc_new'] = self.exception
        names = {'_class_is_structseq',
                 '_instance_reserved_owner_slot', '_special_native_instance',
                 '_structseq_exact_tuple', '_structseq_body', '_structseq_c_object',
                 'py_structseq_new', 'py_structseq_hidden', 'py_class_mark_structseq'}
        body = []
        for node in ast.parse(PORT.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith('_STRUCTSEQ_')
                for target in node.targets
            ):
                body.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in names:
                node.decorator_list = []
                body.append(node)
        exec(compile(ast.Module(body=body, type_ignores=[]), str(PORT), 'exec'), self.ns)
        self.cls.fields[12] = 2
        self.ns['py_class_mark_structseq'](self.cls)
        assert self.error is None

    def atomic_or(self, operation, value, offset, flag, ordering):
        assert operation == 'or'
        prior = self.read(value, offset)
        self.write(value, offset, prior | flag)
        return prior

    def exception(self, kind, message):
        result = self.make((kind, message), abi.PY_TYPE_EXC)
        result.fields[12] = 0
        return result

    def runtime_error(self, callee, message):
        if self.error is None:
            self.error = self.exception(7, message)

    def allocate(self, value, tag, kind):
        result = super().allocate(value, tag, kind)
        if result is None and isinstance(self.error, tuple):
            self.error = self.exception(*self.error)
        return result

    def invoke_structseq(self, visible, hidden):
        caller = Block()
        for index, value in enumerate((self.cls, self.wrap(tuple(visible)), self.wrap(tuple(hidden)))):
            caller.fields[index * 8] = value
            self.roots[('structseq-caller', index)] = self.add(caller, index * 8)
        result = self.ns['py_structseq_new'](*[caller.fields[index * 8] for index in range(3)])
        caller.fields[24] = result
        self.roots[('structseq-caller', 3)] = self.add(caller, 24)
        self.assert_balanced()
        return caller

    def assert_balanced(self):
        assert not self.frame_handles
        assert all(isinstance(key, tuple) for key in self.roots)
        assert all(value.leases == 0 for value in self.objects if value.alive and value is not self.none)


@pytest.mark.parametrize('phase', ('register', 'copy', 'acquire', 'callback', 'release', 'drop', 'allocation', 'frame_enter', 'frame_leave'))
def test_structseq_retains_distinct_visible_and_hidden_owners(phase):
    memory = StructSeqMemory(phase)
    caller = memory.invoke_structseq(range(9), ('timezone name', 19800))
    result = memory.read(caller, 24)
    assert result.tag == abi.PY_TYPE_INSTANCE
    assert memory.values(result) == tuple(range(9))
    extras = memory.ns['py_structseq_hidden'](result)
    assert memory.values(extras) == ('timezone name', 19800)
    memory.assert_balanced()
    assert memory.moves > 0


def test_structseq_instance_allocation_failure_returns_original_exception():
    memory = StructSeqMemory('', fail_kind='instance')
    caller = memory.invoke_structseq(range(9), (None, None))
    result = memory.read(caller, 24)
    assert result.tag == abi.PY_TYPE_EXC
    assert result.value == (19, 'instance allocation failed')
    assert memory.error is None
    memory.assert_balanced()


def test_structseq_refuses_unsealed_or_non_tuple_field_owners():
    memory = StructSeqMemory()
    memory.cls.fields[12] = 2
    caller = memory.invoke_structseq(range(9), (None, None))
    result = memory.read(caller, 24)
    assert result.tag == abi.PY_TYPE_EXC
    assert result.value[0] == 3
    assert memory.error is None


@pytest.mark.parametrize('failed_root', (1,7,14))
def test_structseq_registration_failure_keeps_entry_exception_separate(failed_root):
    memory = StructSeqMemory()
    prior = memory.exception(2, 'entry exception')
    memory.error = prior
    memory.fail_register = failed_root
    caller = memory.invoke_structseq(range(9), (None, None))
    result = memory.read(caller, 24)
    assert result.tag == abi.PY_TYPE_EXC
    assert result.value[0] == 7
    assert result is not prior
    assert memory.error is prior
    memory.assert_balanced()


@pytest.mark.parametrize('phase', ('register','copy','acquire','callback','release','drop','frame_enter','frame_leave'))
@pytest.mark.parametrize('subclass', (False, True))
def test_structseq_dict_storage_adapter_keeps_real_storage_owned(phase, subclass):
    memory = StructSeqMemory(phase)
    caller = Block()
    storage = memory.wrap({'tm_zone': 'X'})
    receiver = storage
    if subclass:
        receiver = memory.make(None, abi.PY_TYPE_INSTANCE)
        receiver.fields[12] = 0
        receiver.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = storage
    caller.fields[0] = receiver
    memory.roots[('dictionary-caller', 0)] = caller

    def storage_slots(source_slot, result_slot):
        # The reused dictionary-storage owner is separately tested. This seam
        # models its owning output contract and forces the adapter to keep it
        # authoritative through publication and subsequent cleanup.
        memory.collect('callback')
        receiver = memory.read(source_slot, 0)
        assert receiver.alive and receiver.leases > 0
        value = receiver if receiver.tag == abi.PY_TYPE_DICT else memory.read(receiver, abi.PYINSTANCEOBJECT_FIELDS_OFFSET)
        memory.retain(value)
        memory.write(result_slot, 0, value)
        return 0

    memory.ns['py_dict_storage_slots'] = storage_slots
    result = memory.ns['_structseq_c_object'](caller.fields[0], None, None, 2)
    assert result.alive and result.tag == abi.PY_TYPE_DICT
    assert result.value == {'tm_zone': 'X'}
    memory.assert_balanced()
