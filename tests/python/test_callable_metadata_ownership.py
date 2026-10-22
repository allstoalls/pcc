"""Execute reconstructed production metadata initialization and lease bodies.

These deterministic models prove owners, root ordering, failure cleanup and
TLS preservation. They do not replace matched five-GC native qualification.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_foreign_address_leases import _functions
from test_runtime_entry_handoff import SpecialCallModel


RUNTIME = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_func.py"


class MetadataModel(SpecialCallModel):
    def __init__(self, failure=None, prior_pin=False, cleanup_effect=None):
        super().__init__()
        self.failure = failure
        self.cleanup_effect = cleanup_effect
        self.function_object = self.make(self.abi.PY_TYPE_FUNC)
        self.source = self.own(self.function_object)
        self.store(self.function_object, self.abi.PYOBJECTHEADER_FLAGS_OFFSET, 64 if prior_pin else 0)
        self.prior_flags = 64 if prior_pin else 0
        self.text_values = {}
        self.attributes = {}
        self.setter_calls = []
        self.allocations = 0
        self.value_leases = 0
        self.original_failure = 0
        self.cleanup_errors = []
        self.ns.update(
            py_str_new=self.new_text,
            py_obj_setattr=self.set_attribute,
            py_clear_exception=self.clear_exception,
            pcc_gc_store_root=self.clear_or_store,
        )
        acquire = self.ns["pcc_gc_foreign_lease_acquire"]

        def acquire_value(slot):
            if self.load(slot) in self.text_values:
                self.value_leases += 1
                if self.failure == "lease" + str(self.value_leases):
                    return -1
            return acquire(slot)

        self.ns["pcc_gc_foreign_lease_acquire"] = acquire_value
        if failure and failure.startswith("register"):
            self.fail_registration = int(failure[-1])
        self.fail_commit = failure == "copy"
        tree = ast.parse(RUNTIME.read_text())
        constants = [node for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id.startswith("_FUNC_METADATA_")
                             for target in node.targets)]
        exec(compile(ast.Module(body=constants, type_ignores=[]), str(RUNTIME), "exec"), self.ns)
        _functions(RUNTIME, {
            "_func_metadata_close", "_func_metadata_set_text", "py_func_init_metadata_slots",
        }, self.ns)

    def clear_exception(self):
        if self.error:
            self.drop_reference(self.error)
        self.error = 0

    def fail(self, message):
        self.error = self.exception(message)
        self.original_failure = self.error
        return self.error

    def new_text(self, text, size):
        assert size == len(text)
        self.allocations += 1
        assert self.count(self.function_object) == 1
        if self.failure == "allocate" + str(self.allocations):
            self.fail("metadata allocation failed")
            return 0
        value = self.make(self.abi.PY_TYPE_STR)
        self.text_values[value] = text
        return value

    def set_attribute(self, function, attribute, value):
        assert function == self.load(self.source)
        assert self.count(function) == self.count(value) == 1
        assert self.depth == 0
        assert self.references[function] == 2
        self.setter_calls.append(attribute)
        if self.failure == "setter" + str(len(self.setter_calls)):
            self.fail("metadata setter failed")
            return -1
        self.references[value] += 1
        self.attributes[attribute] = value
        return 0

    def clear_or_store(self, slot, value):
        previous = self.load(slot)
        self.root_store(slot, value)
        if previous == self.function_object and not value and self.cleanup_effect:
            if self.cleanup_effect == "replace":
                self.clear_exception()
                error = self.exception("cleanup replaced exception")
                self.cleanup_errors.append(error)
                self.error = error
            elif self.cleanup_effect == "clear":
                self.clear_exception()

    def invoke(self, qualname="Outer.inner"):
        return self.ns["py_func_init_metadata_slots"](self.source, "provider", qualname)

    def assert_balanced(self):
        assert not self.registered
        assert self.active() == 0 and self.depth == 0
        assert self.references[self.function_object] == 1
        assert self.load(self.function_object, self.abi.PYOBJECTHEADER_FLAGS_OFFSET) == self.prior_flags
        for value in self.text_values:
            expected = sum(item == value for item in self.attributes.values())
            assert self.references[value] == expected
            assert self.count(value) == 0
        assert all(self.references[value] == 0 for value in self.cleanup_errors)


@pytest.mark.parametrize("qualname", ["Outer.inner", 0])
@pytest.mark.parametrize("prior_pin", [False, True])
def test_metadata_slots_keep_source_and_value_leased(qualname, prior_pin):
    model = MetadataModel(prior_pin=prior_pin)
    assert model.invoke(qualname) == 0
    expected = {"__module__": "provider"}
    if qualname:
        expected["__qualname__"] = qualname
    assert {name: model.text_values[value] for name, value in model.attributes.items()} == expected
    assert not model.error
    model.assert_balanced()


@pytest.mark.parametrize("failure", [
    "register1", "register2", "register3", "copy",
    "allocate1", "allocate2", "setter1", "setter2", "lease1", "lease2",
])
def test_metadata_slots_failure_balances_every_partial_owner(failure):
    model = MetadataModel(failure=failure)
    assert model.invoke() == -1
    assert model.error
    if model.original_failure:
        assert model.error == model.original_failure
    model.assert_balanced()


@pytest.mark.parametrize("effect", ["clear", "replace"])
def test_metadata_slots_cleanup_preserves_original_tls_error(effect):
    model = MetadataModel(failure="setter2", cleanup_effect=effect)
    assert model.invoke() == -1
    assert model.error == model.original_failure
    model.assert_balanced()


def test_metadata_slots_leave_preexisting_exception_and_source_untouched():
    model = MetadataModel()
    previous = model.fail("earlier exception")
    assert model.invoke() == -1
    assert model.error == previous and not model.allocations and not model.setter_calls
    assert model.registration_count == 0
    model.assert_balanced()


def test_metadata_slots_copy_source_after_registration_can_move_it():
    model = MetadataModel()
    replacement = model.make(model.abi.PY_TYPE_FUNC)
    model.after_registration = lambda: model.store(model.source, 0, replacement)
    model.function_object = replacement
    assert model.invoke() == 0
    model.assert_balanced()
