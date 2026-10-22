"""Execute lookup cleanup bodies against byte-addressed, fixture-free memory.

This covers saved-error ownership and callback ordering, not compiled runtime
execution or the outstanding threaded/moving-GC entry and lease boundaries.
"""

from pathlib import Path

import pytest

from test_foreign_address_leases import _functions
from test_runtime_entry_handoff import SpecialCallModel


RUNTIME = Path(__file__).resolve().parents[2] / "pcc/runtime/py"


class _LookupCleanupModel(SpecialCallModel):
    def __init__(self, *, tls_change="replace", prior_pin=0, alias=True, fail=True):
        super().__init__()
        self.tls_change = tls_change
        self.alias = alias
        self.fail = fail
        self.original = self.exception("lookup failure")
        self.store(self.original, self.abi.PYOBJECTHEADER_FLAGS_OFFSET, prior_pin)
        self.meta = self.make(self.abi.PY_TYPE_CLASS)
        self.receiver = self.make(self.abi.PY_TYPE_CLASS)
        self.arguments = self.make(self.abi.PY_TYPE_TUPLE)
        self.temporary = self.make(self.abi.PY_TYPE_INSTANCE)
        self.result = 0 if fail else self.make(self.abi.PY_TYPE_STR)
        self.replacement = 0
        self.finalized = []
        self.restored = []
        self.unregistered = []
        self.slots = 0
        self.ns.update(
            _metaclass=lambda _cls: self.meta,
            py_builtin_type_class_tag=lambda _cls: 0,
            py_incref=self.retain,
            py_decref=self.drop_reference,
            py_current_exception=lambda: self.error,
            py_raise=self.restore_error,
            _metaclass_call_body=self.body,
            _instance_getattr_default_body=self.instance_body,
            _instance_getattr_custom_body=self.instance_body,
        )
        _functions(
            RUNTIME / "py_class.py",
            {
                "py_class_metaclass_call",
                "_instance_getattr_default_rooted",
                "_instance_lookup_hold",
                "_instance_lookup_release",
            },
            self.ns,
        )
        _functions(
            RUNTIME / "freestanding_gc_root_operations.py",
            {"pcc_gc_pin", "pcc_gc_unpin", "pcc_gc_take_pinned_slot"},
            self.ns,
        )

    def register(self, slot):
        # These existing wrappers publish populated input slots. The newer
        # slot-binder model's empty-on-registration contract does not apply.
        handle = self.allocate(self.abi.C_POINTER_SIZE)
        self.registered[handle] = slot
        self.roots.add(slot)
        return handle

    def unregister(self, handle):
        slot = self.registered.pop(handle)
        self.unregistered.append((slot, self.load(slot)))
        self.roots.remove(slot)

    def retain(self, value):
        if value:
            assert self.references[value] > 0, "retained a dead exception or owner"
            self.references[value] += 1

    def set_error_owned(self, value):
        previous, self.error = self.error, value
        self.drop_reference(previous)

    def drop_reference(self, value):
        if not value:
            return
        super().drop_reference(value)
        if value != self.temporary or self.references[value]:
            return
        self.finalized.append(value)
        if not self.fail:
            return
        saved_error = self.slots + 7 * self.abi.C_POINTER_SIZE
        assert saved_error in self.registered.values()
        assert self.load(saved_error) == self.original
        assert (
            self.load(self.original, self.abi.PYOBJECTHEADER_FLAGS_OFFSET)
            & self.abi.PY_FLAG_GC_PINNED
        )
        # The slot-6 alias was already released. TLS and the saved slot are
        # now the only owners, so replacing TLS would destroy an unheld error.
        assert self.references[self.original] == 2
        if self.tls_change == "replace":
            self.replacement = self.exception("temporary finalizer failure")
        self.set_error_owned(self.replacement)
        assert self.references[self.original] == 1

    def restore_error(self, value):
        assert value == self.original, "cleanup restored the wrong exception"
        assert self.references[value] == 1
        assert self.slots + 7 * self.abi.C_POINTER_SIZE in self.registered.values()
        assert (
            self.load(value, self.abi.PYOBJECTHEADER_FLAGS_OFFSET)
            & self.abi.PY_FLAG_GC_PINNED
        )
        self.restored.append(value)
        self.retain(value)
        self.set_error_owned(value)

    def body(self, slots, pins):
        self.slots = slots
        self.ns["_instance_lookup_hold"](slots, pins, 5, self.temporary, 0)
        if self.fail:
            self.set_error_owned(self.original)
            if self.alias:
                self.ns["_instance_lookup_hold"](slots, pins, 6, self.original, 1)
        return self.result

    def instance_body(self, _inst, _cls, _name, slots, pins):
        return self.body(slots, pins)

    def invoke(self, entry):
        if entry == "metaclass":
            return self.ns["py_class_metaclass_call"](self.receiver, self.arguments, 0)
        return self.ns["_instance_getattr_default_rooted"](
            self.receiver, self.meta, "missing", int(entry == "custom-instance")
        )

    def assert_balanced(self):
        assert self.finalized == [self.temporary]
        assert self.references[self.temporary] == 0
        assert not self.registered and not self.roots and self.depth == 0
        assert self.load("pcc_gc_metric_pin") == 0
        assert all(
            self.load(self.slots, offset) == 0
            for offset in range(0, 8 * self.abi.C_POINTER_SIZE, self.abi.C_POINTER_SIZE)
        )
        for value in (self.receiver, self.meta, self.arguments):
            assert self.references[value] == 1
            assert self.load(value, self.abi.PYOBJECTHEADER_FLAGS_OFFSET) == 0


@pytest.mark.parametrize("entry", ["metaclass", "instance", "custom-instance"])
@pytest.mark.parametrize("tls_change", ["clear", "replace"])
@pytest.mark.parametrize("prior_pin", [0, 64])
@pytest.mark.parametrize("alias", [False, True])
def test_lookup_cleanup_restores_original_exception(
    entry, tls_change, prior_pin, alias
):
    model = _LookupCleanupModel(tls_change=tls_change, prior_pin=prior_pin, alias=alias)
    assert model.invoke(entry) == 0
    assert model.error == model.original, (
        "finalizer replaced or cleared the lookup exception"
    )
    assert model.restored == [model.original]
    assert model.references[model.original] == 1
    assert (
        model.load(model.original, model.abi.PYOBJECTHEADER_FLAGS_OFFSET) == prior_pin
    )
    if model.replacement:
        assert model.references[model.replacement] == 0
    assert all(value == 0 for _slot, value in model.unregistered)
    model.assert_balanced()


@pytest.mark.parametrize("entry", ["metaclass", "instance", "custom-instance"])
def test_lookup_cleanup_without_exception_keeps_result_owner(entry):
    model = _LookupCleanupModel(fail=False)
    assert model.invoke(entry) == model.result
    assert model.error == 0 and model.restored == []
    assert model.references[model.result] == 1
    assert model.load(model.result, model.abi.PYOBJECTHEADER_FLAGS_OFFSET) == 0
    assert all(value == 0 or slot == model.slots for slot, value in model.unregistered)
    model.assert_balanced()
