"""Cleanup sharing keeps exact ordered ownership/SSA states separate."""
from types import SimpleNamespace
import gc
import weakref

from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin


class IdentityOnly:
    def __eq__(self, other):
        raise AssertionError("IR identity must not invoke value equality")

    def __hash__(self):
        raise AssertionError("IR values must not be dict keys")


class Recorder(CallObjectLoweringMixin):
    def __init__(self):
        self.blocks = []
        self.events = []
        self.current_function = SimpleNamespace(append_basic_block=self.new_block)
        self.builder = SimpleNamespace(
            _block=IdentityOnly(), position_at_end=self.position,
            call=lambda fn, args: self.events.append((fn, tuple(args))),
            branch=lambda target: self.events.append(("branch", target)),
            store=lambda value, flag: self.events.append(("store", flag)),
        )
        self.module = SimpleNamespace(globals={"py_tls_exc_swap_slot": "swap"})
        self.runtime = {name: name for name in (
            "pcc_gc_store_root", "py_clear_exception", "pcc_gc_foreign_lease_release",
            "py_cleanup_one_root_preserving_exception",
            "py_cleanup_two_roots_preserving_exception",
            "py_cleanup_one_lease_preserving_exception",
        )}
        self._slot_call_root_records = []

    def new_block(self, name):
        block = IdentityOnly()
        self.blocks.append(block)
        return block

    def position(self, block):
        self.builder._block = block

    def _fresh(self, label):
        return label

    def _as_gc_ptr(self, value):
        return value

    def _alloca_in_entry(self, *args, **kwargs):
        return IdentityOnly()

    def _gc_one_slot_frame_map(self):
        return None

    def _emit_current_gc_frame_enter_lifo(self, frame_map, slot):
        self.events.append(("enter", slot))

    def _emit_gc_frame_leave_lifo_for_slot(self, slot):
        self.events.append(("leave", slot))


def setup_roots():
    model = Recorder()
    first, second, target = IdentityOnly(), IdentityOnly(), IdentityOnly()
    model._slot_call_root_records = [(first, None, True), (second, None, True)]
    return model, first, second, target


def test_identical_cleanup_is_shared_without_reemission():
    model, first, second, target = setup_roots()
    block = model._slot_call_cleanup_block((first, second), target)
    events = len(model.events)
    saved = model.builder._block
    assert model._slot_call_cleanup_block((first, second), target) is block
    assert len(model.events) == events
    assert model.builder._block is saved


def test_root_order_destination_registration_and_function_stay_distinct():
    model, first, second, target = setup_roots()
    blocks = [model._slot_call_cleanup_block((first, second), target)]
    blocks.append(model._slot_call_cleanup_block((second, first), target))
    blocks.append(model._slot_call_cleanup_block((first, second), IdentityOnly()))
    model._slot_call_root_records[0] = (first, IdentityOnly(), True)
    blocks.append(model._slot_call_cleanup_block((first, second), target))
    model._slot_call_root_records[0] = (first, None, False)
    blocks.append(model._slot_call_cleanup_block((first, second), target))
    model.current_function = SimpleNamespace(append_basic_block=model.new_block)
    blocks.append(model._slot_call_cleanup_block((first, second), target))
    assert len({id(block) for block in blocks}) == len(blocks)


def test_lease_operand_identity_and_order_stay_distinct():
    model, first, second, target = setup_roots()
    one, two = IdentityOnly(), IdentityOnly()
    leases = ((first, one), (second, two))
    block = model._slot_call_cleanup_block((first, second), target, leases)
    assert model._slot_call_cleanup_block((first, second), target, leases) is block
    assert model._slot_call_cleanup_block((first, second), target, tuple(reversed(leases))) is not block
    assert model._slot_call_cleanup_block((first, second), target, ((first, two), (second, one))) is not block


def test_mutating_registration_cannot_drop_cached_identity_owner():
    model, first, second, target = setup_roots()
    flag = IdentityOnly()
    reference = weakref.ref(flag)
    mutable_record = [first, flag, True]
    model._slot_call_root_records[0] = mutable_record
    model._slot_call_cleanup_block((first,), target)
    mutable_record[1] = None
    del flag
    gc.collect()
    assert reference() is not None


def test_entry_identity_is_part_of_cleanup_context():
    model, first, second, target = setup_roots()
    model._current_entry_block = IdentityOnly()
    first_block = model._slot_call_cleanup_block((first,), target)
    model._current_entry_block = IdentityOnly()
    assert model._slot_call_cleanup_block((first,), target) is not first_block


def test_partial_cleanup_is_never_published():
    model, first, second, target = setup_roots()
    original = model.builder.call
    def fail(fn, args):
        raise RuntimeError("injected incomplete cleanup")
    model.builder.call = fail
    import pytest
    with pytest.raises(RuntimeError, match="injected incomplete cleanup"):
        model._slot_call_cleanup_block((first,), target)
    assert model._slot_call_cleanup_blocks == {}
    model.builder.call = original
    completed = model._slot_call_cleanup_block((first,), target)
    assert completed is not model.blocks[0]
    assert model._slot_call_cleanup_block((first,), target) is completed
