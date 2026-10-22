"""Deterministic execution of production authoritative-slot handoff bodies.

These tests prove transaction ordering and owner/count invariants. They do
not replace the matched threaded GC3/GC4 native movement gate.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_foreign_address_leases import Model, _functions


ROOT = Path(__file__).resolve().parents[2]


class HandoffModel(Model):
    def __init__(self, backend=3):
        super().__init__(backend)
        self.roots = set()
        self.references = {}
        self.plans = {}
        self.plan_backends = {}
        self.pending_exception = object()
        self.fail_commit = False
        self.plan_id = 100000
        self.ns.update(
            stack_alloc=self.allocate,
            _gc_backend_fast=lambda: self.load("pcc_gc_backend_selected", 0),
            pcc_gc_store_root_plan_init=self.plan_init,
            pcc_gc_store_root_plan_commit_locked=self.plan_commit,
            pcc_gc_store_root_plan_finish=self.plan_finish,
            pcc_gc_note_slot_write_barrier=self.barrier,
            pcc_gc_load_borrowed_ptr=lambda owner, slot: self.borrowed_load(slot),
            ptr_add=lambda pointer, offset: pointer + offset,
        )
        _functions(ROOT / "pcc/runtime/py/py_obj.py", {
            "pcc_gc_root_copy_lease", "pcc_gc_root_move", "pcc_gc_root_copy_borrowed_lease",
            "pcc_gc_root_copy_lease_prepare_locked", "pcc_gc_root_copy_lease_finish",
        }, self.ns)

    def object(self, obj=1000, **kwargs):
        slot = super().object(obj, **kwargs)
        self.references[obj] = self.references.get(obj, 0) + 1
        self.roots.add(slot)
        return slot

    def empty(self, slot=90000):
        self.store(slot, 0, 0)
        self.roots.add(slot)
        return slot

    def allocate(self, size):
        self.plan_id += size
        return self.plan_id

    def plan_init(self, plan, backend):
        self.plans[plan] = False
        self.plan_backends[plan] = backend

    def plan_commit(self, plan, destination, value):
        assert self.depth > 0
        assert destination in self.roots
        previous = self.load(destination, 0)
        assert previous == 0 or value == 0
        self.events.append(("retain_publish", destination, value))
        if self.fail_commit:
            return 0
        if value and not (isinstance(value, int) and value & 1):
            self.references[value] += 1
        if previous and not (isinstance(previous, int) and previous & 1):
            self.references[previous] -= 1
        self.store(destination, 0, value)
        self.plans[plan] = True
        return 1

    def plan_finish(self, plan):
        assert self.depth == 0
        self.events.append(("finish", plan))

    def barrier(self, owner, slot, value):
        assert self.depth > 0
        assert owner == 0 and slot in self.roots
        self.events.append(("barrier", slot, value))

    def copy(self, destination, source):
        return self.ns["pcc_gc_root_copy_lease"](destination, source)

    def borrowed_load(self, source):
        assert self.depth > 0
        self.events.append(("borrowed_reload", source))
        return self.load(source, 0)

    def copy_borrowed(self, destination, source):
        return self.ns["pcc_gc_root_copy_borrowed_lease"](destination, source)

    def move(self, destination, source):
        return self.ns["pcc_gc_root_move"](destination, source)


def test_copy_reloads_authoritative_source_after_registration_and_lock_wait():
    model = HandoffModel()
    source = model.object()
    model.object(2000)
    destination = model.empty()
    # A destination may be registered empty while the caller's source moves.
    # The production helper must not carry a pre-lock raw copy into the plan.
    model.on_lock = lambda: model.store(source, 0, 2000)
    assert model.copy(destination, source) == 1
    assert model.load(destination, 0) == 2000
    assert model.count(1000) == 0 and model.count(2000) == 1
    assert model.references[1000] == 1 and model.references[2000] == 2
    assert ("retain_publish", destination, 1000) not in model.events
    assert model.release(destination, 1) == 0


def test_copy_publishes_owner_and_count_before_unlock_can_park():
    model = HandoffModel()
    source, destination = model.object(), model.empty()
    observed = []
    model.on_unlock = lambda: observed.append((
        model.load(destination, 0), model.count(), model.references[1000],
    ))
    assert model.copy(destination, source) == 1
    assert observed == [(1000, 1, 2)]
    assert model.release(destination, 1) == 0


@pytest.mark.parametrize("failure", ["overflow", "invalid", "store"])
def test_copy_failure_preserves_source_owner_and_rolls_back_count(failure):
    model = HandoffModel()
    source = model.object(node=failure != "invalid", proven=failure != "invalid")
    destination = model.empty()
    if failure == "overflow":
        model.store(model.nodes[1000], 80, 2**63 - 1)
    model.fail_commit = failure == "store"
    old_count = model.count() if failure != "invalid" else 0
    old_exception = model.pending_exception
    expected = {"overflow": -2, "invalid": -1, "store": -4}[failure]
    assert model.copy(destination, source) == expected
    assert model.load(source, 0) == 1000
    assert model.load(destination, 0) == 0
    assert model.references[1000] == 1
    assert model.active() == 0
    if failure != "invalid":
        assert model.count() == old_count
    assert model.pending_exception is old_exception


@pytest.mark.parametrize("method", ["copy", "move"])
def test_nonempty_or_identical_destination_never_drops_an_owner(method):
    model = HandoffModel()
    source, destination = model.object(), model.object(2000)
    invoke = getattr(model, method)
    before = dict(model.references)
    assert invoke(destination, source) == -4
    assert invoke(source, source) == -1
    assert invoke(0, source) == -1
    assert invoke(destination, 0) == -1
    assert model.references == before
    assert model.load(source, 0) == 1000 and model.load(destination, 0) == 2000
    assert model.active() == 0


def test_alias_copies_have_independent_counts_despite_callback_unpin():
    model = HandoffModel()
    source = model.object()
    first, second = model.empty(90000), model.empty(90008)
    assert model.copy(first, source) == model.copy(second, source) == 1
    model.store(1000, 12, 64)
    model.store(1000, 12, 0)
    assert model.count() == 2 and model.references[1000] == 3
    assert model.release(first, 1) == 0 and model.pinned() == 1
    assert model.release(second, 1) == 0 and model.pinned() == 0


def test_move_publishes_caller_output_before_unlock_without_new_owner():
    model = HandoffModel()
    source, destination = model.object(), model.empty()
    token = model.acquire(source)
    before = dict(model.references)
    observed = []
    model.on_unlock = lambda: observed.append((
        model.load(source, 0), model.load(destination, 0), model.count(),
    ))
    assert model.move(destination, source) == 0
    assert observed == [(0, 1000, 1)]
    assert model.references == before
    assert model.release(destination, token) == 0
    assert model.active() == 0


def test_move_reloads_after_contended_lock_before_publishing():
    model = HandoffModel()
    source = model.object()
    model.object(2000)
    destination = model.empty()
    model.on_lock = lambda: model.store(source, 0, 2000)
    assert model.move(destination, source) == 0
    assert model.load(source, 0) == 0 and model.load(destination, 0) == 2000


@pytest.mark.parametrize("value", [0, 7])
def test_empty_and_tagged_source_need_no_counted_lease(value):
    model = HandoffModel()
    source, destination = model.empty(90008), model.empty()
    model.store(source, 0, value)
    assert model.copy(destination, source) == 0
    assert model.load(destination, 0) == value and model.active() == 0


def test_handoff_abi_returns_only_scalar_status_and_tokens():
    from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES

    for name in ("pcc_gc_root_copy_lease", "pcc_gc_root_move"):
        result, args, variadic = RUNTIME_SIGNATURES[name]
        assert str(result) == "i64" and len(args) == 2 and not variadic
    tree = ast.parse((ROOT / "pcc/runtime/py/py_obj.py").read_text())
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in {
            "pcc_gc_root_copy_lease", "pcc_gc_root_move",
        }:
            calls = [node.func.id for node in ast.walk(function)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
            assert "pcc_gc_pin" not in calls
            assert "pcc_gc_take_pinned_slot" not in calls


def test_borrowed_copy_never_resolves_source_as_an_owning_root():
    model = HandoffModel()
    source, destination = model.object(), model.empty()
    assert model.copy_borrowed(destination, source) == 1
    assert ("borrowed_reload", source) in model.events
    assert ("reload", source) not in model.events
    assert model.references[1000] == 2 and model.count() == 1
    assert model.release(destination, 1) == 0


def test_borrowed_copy_failure_rolls_back_destination_reference():
    model = HandoffModel()
    source, destination = model.object(), model.empty()
    model.store(model.nodes[1000], 80, 2**63 - 1)
    assert model.copy_borrowed(destination, source) == -2
    assert model.load(source, 0) == 1000 and model.load(destination, 0) == 0
    assert model.references[1000] == 1 and model.active() == 0


class SpecialCallModel(HandoffModel):
    """Real slot binder bodies; deterministic allocator/callback surface."""
    def __init__(self):
        from pcc.runtime.py import py_abi_constants as abi

        super().__init__()
        self.abi = abi
        self.next_object = 1000000
        self.callbacks = {}
        self.calls = []
        self.error = 0
        self.registered = {}
        self.registration_count = 0
        self.fail_registration = 0
        self.after_registration = None
        self.none = self.make(abi.PY_TYPE_NONE)
        self.store("pcc_native_callable_sync_context", 0, 1)
        self.ns.update({name: getattr(abi, name) for name in dir(abi) if name.isupper()})
        self.ns.update(
            cstr=lambda value: value,
            strlen=len,
            memset=self.zero,
            ptr_add=lambda pointer, offset: pointer + offset,
            load_i8=self.byte,
            pcc_gc_scheduler_root_register_handle=self.register,
            pcc_gc_scheduler_root_unregister_handle=self.unregister,
            pcc_gc_store_root=self.root_store,
            pcc_gc_load_ptr=lambda owner, slot: self.load(slot),
            py_tls_exc_swap_slot=self.swap_error,
            py_clear_exception=lambda: setattr(self, "error", 0),
            py_err_occurred=lambda: int(self.error != 0),
            py_runtime_error_if_unset=self.runtime_error,
            py_raise_owned=lambda value: setattr(self, "error", value),
            py_exc_new=lambda tag, message: self.exception(message),
            py_tuple_new=self.tuple_new,
            py_tuple_len=lambda value: self.load(value, abi.PYTUPLEOBJECT_LEN_OFFSET),
            py_tuple_set_item=self.tuple_set,
            py_dict_len=lambda value: self.load(value, abi.PYDICTOBJECT_ITEM_COUNT_OFFSET),
            py_obj_call=self.raw_call,
            py_obj_call_default=self.raw_call,
            py_obj_call_context_is_deferred=lambda: int((self.load("pcc_native_callable_sync_context") & 1) == 0),
            _ptr_is_class=lambda value: bool(value in self.nodes and self.load(value, 8) == abi.PY_TYPE_CLASS),
            _ptr_is_instance=lambda value: bool(value in self.nodes and (self.load(value, 8) == abi.PY_TYPE_INSTANCE or self.load(value, 8) >= abi.PY_TYPE_USER_CLASS_START)),
            _ptr_can_have_header=lambda value: value in self.nodes,
            pcc_capi_is_cext_type_tag=lambda tag: int(tag >= abi.PY_TYPE_CEXT_TAG_BASE),
            _strs_eq=lambda first, second: int(first == second),
            global_load_ptr=lambda name: self.none if name == "py_None" else self.load(name),
            _is_none=lambda value: value == 0 or value == self.none,
            _type_of=lambda value: self.load(value, 8),
            pcc_platform_abort=lambda: pytest.fail("unexpected lease invariant abort"),
            call_ptr1=self.native_call,
            call_ptr2=self.native_call,
            call_ptr3=self.native_call,
            call_ptr4=self.native_call,
        )
        path = ROOT / "pcc/runtime/py/py_class.py"
        names = {node.name for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.FunctionDef)
                 and (node.name.startswith("_special_") or node.name in {
                     "py_obj_special_call_slots", "py_obj_call_slots",
                 })}
        _functions(path, names, self.ns)
        _functions(ROOT / "pcc/runtime/py/py_call_splat_runtime.py",
                   {"py_call_validate_kwargs"}, self.ns)
        _functions(ROOT / "pcc/runtime/py/py_func.py", {"py_obj_call_slots_sync"}, self.ns)

    def load(self, base, offset=0):
        return self.memory.get((base + offset, 0) if isinstance(base, int) else (base, offset), 0)

    def allocate(self, size):
        address = self.plan_id
        self.plan_id += ((size + 7) // 8) * 8 + 16
        return address

    def store(self, base, offset, value):
        key = (base + offset, 0) if isinstance(base, int) else (base, offset)
        self.memory[key] = value

    def byte(self, base, offset):
        if isinstance(base, str):
            return ord(base[offset])
        return self.load(base, offset)

    def zero(self, base, value, size):
        for offset in range(0, size, 8):
            self.store(base, offset, value)
        return base

    def make(self, tag):
        value = self.next_object
        self.next_object += 32768
        self.store(value, 8, tag)
        self.store(value, 12, 0)
        self.nodes[value] = value + 10000
        self.proven.add(value)
        self.references[value] = 1
        return value

    def own(self, value):
        slot = self.allocate(8)
        self.empty(slot)
        self.store(slot, 0, value)
        return slot

    def root_store(self, slot, value):
        previous = self.load(slot)
        if previous == value:
            return
        if value in self.references:
            self.references[value] += 1
        self.store(slot, 0, value)
        if previous in self.references:
            self.drop_reference(previous)

    def drop_reference(self, value):
        self.references[value] -= 1
        assert self.references[value] >= 0
        if self.references[value] == 0 and self.load(value, 8) == self.abi.PY_TYPE_TUPLE:
            for item in self.tuple_values(value):
                if item in self.references:
                    self.drop_reference(item)

    def register(self, slot):
        assert self.load(slot) == 0, "new boundary must register EMPTY roots"
        self.registration_count += 1
        if self.registration_count == self.fail_registration:
            return 0
        handle = self.allocate(8)
        self.registered[handle] = slot
        self.roots.add(slot)
        if self.after_registration:
            callback, self.after_registration = self.after_registration, None
            callback()
        return handle

    def unregister(self, handle):
        slot = self.registered.pop(handle)
        assert self.load(slot) == 0, "boundary leaked a managed root owner"
        self.roots.remove(slot)

    def swap_error(self, slot):
        self.error, previous = self.load(slot), self.error
        self.store(slot, 0, previous)

    def exception(self, message):
        value = self.make(self.abi.PY_TYPE_EXC)
        self.store(value, 24, message)
        return value

    def runtime_error(self, helper, message):
        assert self.depth == 0, "no diagnostic allocation inside graph transaction"
        if not self.error:
            self.error = self.exception(message)
        return 0

    def tuple_new(self, length):
        value = self.make(self.abi.PY_TYPE_TUPLE)
        self.store(value, self.abi.PYTUPLEOBJECT_LEN_OFFSET, length)
        return value

    def tuple_set(self, value, index, item):
        self.root_store(value + self.abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8, item)

    def tuple(self, values):
        result = self.tuple_new(len(values))
        for index, value in enumerate(values):
            self.tuple_set(result, index, value)
        return result

    def tuple_values(self, value):
        if not value:
            return ()
        count = self.load(value, self.abi.PYTUPLEOBJECT_LEN_OFFSET)
        return tuple(self.load(value, self.abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8)
                     for index in range(count))

    def function(self, callback):
        value = self.make(self.abi.PY_TYPE_FUNC)
        self.callbacks[value] = callback
        return value

    def raw_call(self, function, args, kwargs):
        assert self.depth == 0
        positional = self.tuple_values(args)
        self.calls.append((function, positional, kwargs))
        if function in self.callbacks:
            result = self.callbacks[function](*positional)
            result = 0 if result is None else result
            if result in self.references:
                self.references[result] += 1
            return result
        self.error = self.exception("object is not callable")
        return 0

    def native_call(self, function, *args):
        assert self.depth == 0
        assert all(self.load(slot) != function for slot in self.roots)
        self.calls.append((function, args, 0))
        result = self.callbacks[function](*args)
        if result in self.references:
            self.references[result] += 1
        return result

    def string(self, text):
        value = self.make(self.abi.PY_TYPE_STR)
        self.store(value, self.abi.PYSTROBJECT_BYTE_LEN_OFFSET, len(text))
        for index, char in enumerate(text):
            self.store(value, self.abi.PYSTROBJECT_DATA_OFFSET + index, ord(char))
        return value

    def cls(self, *, meta=0, bases=(), namespace=None, methods=None):
        abi = self.abi
        value = self.make(abi.PY_TYPE_CLASS)
        self.store(value, abi.PYCLASSOBJECT_METACLASS_OFFSET, meta)
        mro = [value]
        for base in bases:
            base_mro = self.load(base, abi.PYCLASSOBJECT_MRO_OFFSET)
            for index in range(self.load(base, abi.PYCLASSOBJECT_N_MRO_OFFSET)):
                owner = self.load(base_mro, index * 8)
                if owner not in mro:
                    mro.append(owner)
        mro_array = self.allocate(len(mro) * 8)
        for index, owner in enumerate(mro):
            self.store(mro_array, index * 8, owner)
        self.store(value, abi.PYCLASSOBJECT_MRO_OFFSET, mro_array)
        self.store(value, abi.PYCLASSOBJECT_N_MRO_OFFSET, len(mro))
        if namespace:
            attrs = self.make(abi.PY_TYPE_DICT)
            entries = self.allocate(len(namespace) * abi.DICTENTRY_SIZE)
            for index, (name, item) in enumerate(namespace.items()):
                self.store(entries, index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET, self.string(name))
                self.store(entries, index * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET, item)
                if item in self.references:
                    self.references[item] += 1
            self.store(attrs, abi.PYDICTOBJECT_ENTRIES_USED_OFFSET, len(namespace))
            self.store(attrs, abi.PYDICTOBJECT_ENTRIES_OFFSET, entries)
            self.store(value, abi.PYCLASSOBJECT_ATTRS_OFFSET, attrs)
        if methods:
            entries = self.allocate(len(methods) * abi.PYCLASSMETHOD_SIZE)
            for index, (name, method) in enumerate(methods.items()):
                self.store(entries, index * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_NAME_OFFSET, name)
                self.store(entries, index * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_FUNC_OFFSET, method)
            self.store(value, abi.PYCLASSOBJECT_N_METHODS_OFFSET, len(methods))
            self.store(value, abi.PYCLASSOBJECT_METHODS_OFFSET, entries)
        return value

    def instance(self, cls):
        value = self.make(self.abi.PY_TYPE_INSTANCE)
        self.store(value, self.abi.PYINSTANCEOBJECT_CLS_OFFSET, cls)
        return value

    def descriptor(self, tag, function, offset):
        value = self.make(tag)
        self.store(value, offset, function)
        self.references[function] += 1
        return value

    def invoke(self, receiver, args=(), name="__call__"):
        source = self.own(receiver)
        arguments = self.own(self.tuple(args))
        output = self.own(0)
        handled = self.allocate(8)
        status = self.ns["py_obj_special_call_slots"](source, name, arguments, 0, output, handled)
        assert not self.registered
        assert self.active() == 0
        return status, self.load(handled), self.load(output)


@pytest.mark.parametrize("binding", ["normal", "staticmethod", "classmethod"])
def test_metaclass_special_binding_uses_actual_owner_once(binding):
    model = SpecialCallModel()
    seen = []
    function = model.function(lambda *args: seen.append(args) or 43)
    descriptor = function
    if binding == "staticmethod":
        descriptor = model.descriptor(model.abi.PY_TYPE_STATICMETHOD, function, model.abi.PYSTATICMETHODOBJECT_FUNC_OFFSET)
    if binding == "classmethod":
        descriptor = model.descriptor(model.abi.PY_TYPE_CLASSMETHOD, function, model.abi.PYCLASSMETHODOBJECT_FUNC_OFFSET)
    meta = model.cls(namespace={"__call__": descriptor})
    receiver = model.cls(meta=meta)
    assert model.invoke(receiver, (7, 9)) == (0, 1, 43)
    expected = (7, 9) if binding == "staticmethod" else ((meta if binding == "classmethod" else receiver), 7, 9)
    assert seen == [expected]


def test_inherited_method_table_beats_later_base_namespace():
    model = SpecialCallModel()
    earlier = model.function(lambda receiver: 17)
    later = model.function(lambda receiver: 19)
    base = model.cls(namespace={"__call__": later})
    meta = model.cls(bases=(base,), methods={"__call__": earlier})
    assert model.invoke(model.cls(meta=meta)) == (0, 1, 17)
    assert len(model.calls) == 1


def test_subclass_metaclass_and_custom_descriptor_binding():
    model = SpecialCallModel()
    seen = []
    returned = model.function(lambda value: seen.append(("call", value)) or 29)
    getter = model.function(lambda descriptor, receiver, owner: seen.append(("get", receiver, owner)) or returned)
    descriptor_type = model.cls(namespace={"__get__": getter})
    descriptor = model.instance(descriptor_type)
    base_meta = model.cls(namespace={"__call__": descriptor})
    subclass_meta = model.cls(bases=(base_meta,))
    receiver = model.cls(meta=subclass_meta)
    assert model.invoke(receiver, (11,)) == (0, 1, 29)
    assert seen == [("get", receiver, subclass_meta), ("call", 11)]


def test_absence_preserves_old_tls_and_is_not_a_failed_selection():
    model = SpecialCallModel()
    old = model.exception("old")
    model.error = old
    assert model.invoke(model.cls(meta=model.cls())) == (0, 0, 0)
    assert model.error == old and model.calls == []


@pytest.mark.parametrize("failure", ["silent_null", "new_exception"])
def test_selected_callback_failure_never_falls_back_with_old_tls(failure):
    model = SpecialCallModel()
    old = model.exception("old")
    new = model.exception("callback")
    def callback(receiver):
        assert model.error == 0
        if failure == "new_exception":
            model.error = new
        return None
    function = model.function(callback)
    model.error = old
    assert model.invoke(model.cls(meta=model.cls(namespace={"__call__": function}))) == (-1, 1, 0)
    assert model.error != old and model.error != 0
    if failure == "new_exception":
        assert model.error == new
    assert len(model.calls) == 1


def test_raw_native_method_is_never_a_managed_root():
    model = SpecialCallModel()
    native = 0x70000000
    model.callbacks[native] = lambda receiver, argument: argument
    meta = model.cls(methods={"__call__": native})
    assert model.invoke(model.cls(meta=meta), (33,)) == (0, 1, 33)


def test_partial_registration_failure_unlinks_only_published_empty_roots():
    model = SpecialCallModel()
    model.fail_registration = 5
    assert model.invoke(model.cls(meta=model.cls())) == (-1, 1, 0)
    assert model.error and not model.registered


def test_successful_alias_return_survives_legacy_callback_unpin():
    model = SpecialCallModel()
    def callback(receiver):
        assert model.count(receiver) > 0
        model.store(receiver, 12, 64)
        model.store(receiver, 12, 0)
        assert model.pinned(receiver) == 1
        return receiver
    function = model.function(callback)
    receiver = model.cls(meta=model.cls(namespace={"__call__": function}))
    assert model.invoke(receiver) == (0, 1, receiver)
    assert model.references[receiver] == 2  # caller source and output, once each


def test_property_descriptor_returns_the_callable_used_by_special_call():
    model = SpecialCallModel()
    seen = []
    callable_value = model.function(lambda arg: seen.append(("call", arg)) or 39)
    getter = model.function(lambda receiver: seen.append(("get", receiver)) or callable_value)
    descriptor = model.descriptor(model.abi.PY_TYPE_PROPERTY, getter, model.abi.PYPROPERTYOBJECT_FGET_OFFSET)
    receiver = model.cls(meta=model.cls(namespace={"__call__": descriptor}))
    assert model.invoke(receiver, (37,)) == (0, 1, 39)
    assert seen == [("get", receiver), ("call", 37)]


def test_special_call_reloads_caller_root_after_empty_registration():
    model = SpecialCallModel()
    old_function = model.function(lambda receiver: 41)
    new_function = model.function(lambda receiver: 47)
    old = model.cls(meta=model.cls(namespace={"__call__": old_function}))
    new = model.cls(meta=model.cls(namespace={"__call__": new_function}))
    source = model.own(old)
    output = model.own(0)
    handled = model.allocate(8)
    model.after_registration = lambda: model.store(source, 0, new)
    status = model.ns["py_obj_special_call_slots"](source, "__call__", 0, 0, output, handled)
    assert (status, model.load(handled), model.load(output)) == (0, 1, 47)
    assert model.calls[0][0] == new_function
    assert model.calls[0][1] == (new,)
    assert not model.registered and model.active() == 0


@pytest.mark.parametrize("borrowed", [0, 1])
def test_split_handoff_defers_finish_until_outermost_unlock(borrowed):
    model = HandoffModel()
    source, destination = model.object(), model.empty()
    plan = model.allocate(256)
    model.lock()
    token = model.ns["pcc_gc_root_copy_lease_prepare_locked"](destination, source, borrowed, plan)
    assert token == 1 and model.load(destination) == 1000 and model.count() == 1
    assert not any(event[0] == "finish" for event in model.events)
    model.unlock()
    model.ns["pcc_gc_root_copy_lease_finish"](plan)
    assert model.release(destination, token) == 0


@pytest.mark.parametrize("invalid", ["args", "kwargs"])
def test_slot_call_rejects_invalid_argument_containers_before_callback(invalid):
    model = SpecialCallModel()
    function = model.function(lambda receiver: 13)
    receiver = model.cls(meta=model.cls(namespace={"__call__": function}))
    source, output, bad = model.own(receiver), model.own(0), model.own(7)
    handled = model.allocate(8)
    status = model.ns["py_obj_special_call_slots"](
        source, "__call__", bad if invalid == "args" else 0,
        bad if invalid == "kwargs" else 0, output, handled,
    )
    assert status == -1 and model.load(handled) == 1 and model.load(output) == 0
    assert model.error and model.calls == []
    assert not model.registered and model.active() == 0


@pytest.mark.parametrize("previous", [0, 2])
@pytest.mark.parametrize("failure", [False, True])
def test_default_only_sync_recursion_restores_context_without_metaclass_lookup(previous, failure):
    from pcc.runtime.py import py_abi_constants as abi

    context = {"pcc_native_callable_sync_context": previous}
    calls = []
    pending = []
    def construct(callable_obj, args, count):
        assert context["pcc_native_callable_sync_context"] == 1
        calls.append((callable_obj, args, count))
        if failure:
            pending.append("construction error")
            return 0
        return 17
    ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
    ns.update(
        null=lambda: 0,
        cstr=lambda text: text,
        ptr_is_null=lambda value: int(value == 0),
        ptr_eq=lambda left, right: int(left == right),
        is_tagged_int=lambda value: int(value & 1),
        global_addr=lambda name: name,
        global_load_ptr=lambda name: 0,
        load_i32=lambda base, offset: context[base] if isinstance(base, str) else abi.PY_TYPE_CLASS,
        store_i32=lambda base, offset, value: context.__setitem__(base, value),
        pcc_diagnostics_runtime_log_event_code=lambda *args: None,
        pcc_capi_type_object_is_callable=lambda value: 0,
        py_obj_call_context_is_deferred=lambda: int((context["pcc_native_callable_sync_context"] & 1) == 0),
        py_class_metaclass_call=lambda *args: pytest.fail("default-only dispatch repeated metaclass lookup"),
        _builtin_exception_class_tag=lambda value: 0,
        py_builtin_type_class_tag=lambda value: -1,
        _builtin_exception_call=construct,
    )
    _functions(ROOT / "pcc/runtime/py/py_obj_ops_dispatch.py", {
        "py_obj_call", "py_obj_call_default", "_py_obj_call_body",
    }, ns)
    _functions(ROOT / "pcc/runtime/py/py_func.py", {"py_obj_call_default_sync"}, ns)
    assert ns["py_obj_call_default"](1000, 0, 0) == (0 if failure else 17)
    assert calls == [(1000, 0, 0)]
    assert context["pcc_native_callable_sync_context"] == previous
    assert pending == (["construction error"] if failure else [])


def test_public_raw_call_retains_existing_metaclass_semantics():
    from pcc.runtime.py import py_abi_constants as abi

    seen = []
    ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
    ns.update(
        null=lambda: 0,
        cstr=lambda text: text,
        ptr_is_null=lambda value: int(value == 0),
        is_tagged_int=lambda value: int(value & 1),
        load_i32=lambda base, offset: abi.PY_TYPE_CLASS,
        pcc_diagnostics_runtime_log_event_code=lambda *args: None,
        pcc_capi_type_object_is_callable=lambda value: 0,
        py_obj_call_context_is_deferred=lambda: 0,
        py_class_metaclass_call=lambda *args: seen.append(args) or 23,
    )
    _functions(ROOT / "pcc/runtime/py/py_obj_ops_dispatch.py", {
        "py_obj_call", "_py_obj_call_body",
    }, ns)
    assert ns["py_obj_call"](1000, 2000, 3000) == 23
    assert seen == [(1000, 2000, 3000)]


@pytest.mark.parametrize("method", ["copy", "copy_borrowed"])
def test_copy_store_plan_uses_backend_after_contended_lock_transition(method):
    model = HandoffModel(backend=3)
    source, destination = model.object(), model.empty()
    model.on_lock = lambda: model.store("pcc_gc_backend_selected", 0, 4)
    assert getattr(model, method)(destination, source) == 1
    assert set(model.plan_backends.values()) == {4}
    assert model.release(destination, 1) == 0


def test_namespace_lookup_heals_key_as_an_owning_slot_before_matching():
    model = SpecialCallModel()
    function = model.function(lambda receiver: 53)
    meta = model.cls(namespace={"__call__": function})
    attrs = model.load(meta, model.abi.PYCLASSOBJECT_ATTRS_OFFSET)
    entries = model.load(attrs, model.abi.PYDICTOBJECT_ENTRIES_OFFSET)
    key_slot = entries + model.abi.DICTENTRY_KEY_OFFSET
    old_key = model.load(key_slot)
    new_key = model.string("__call__")
    model.references[new_key] = 0
    resolved = []
    def owning_resolve(slot, offset):
        assert model.depth > 0
        value = model.load(slot, offset)
        if slot + offset == key_slot and value == old_key:
            model.references[new_key] += 1
            model.references[old_key] -= 1
            model.store(slot, offset, new_key)
            resolved.append(slot)
            return new_key
        return value
    model.ns["pcc_gc_resolve_root_slot_unlocked"] = owning_resolve
    assert model.invoke(model.cls(meta=meta)) == (0, 1, 53)
    assert resolved == [key_slot]
    assert model.references[old_key] == 0 and model.references[new_key] == 1


def test_namespace_lookup_heals_attrs_as_an_owning_slot_before_key_scan():
    model = SpecialCallModel()
    function = model.function(lambda receiver: 59)
    meta = model.cls(namespace={"__call__": function})
    attrs_slot = meta + model.abi.PYCLASSOBJECT_ATTRS_OFFSET
    old_attrs = model.load(attrs_slot)
    new_attrs = model.make(model.abi.PY_TYPE_DICT)
    for offset in (model.abi.PYDICTOBJECT_ENTRIES_USED_OFFSET,
                   model.abi.PYDICTOBJECT_ENTRIES_OFFSET):
        model.store(new_attrs, offset, model.load(old_attrs, offset))
    model.references[new_attrs] = 0
    resolved = []
    def owning_resolve(slot, offset):
        assert model.depth > 0
        value = model.load(slot, offset)
        if slot + offset == attrs_slot and value == old_attrs:
            model.references[new_attrs] += 1
            model.references[old_attrs] -= 1
            model.store(slot, offset, new_attrs)
            resolved.append(slot)
            return new_attrs
        return value
    model.ns["pcc_gc_resolve_root_slot_unlocked"] = owning_resolve
    assert model.invoke(model.cls(meta=meta)) == (0, 1, 59)
    assert resolved == [attrs_slot]
    assert model.references[old_attrs] == 0 and model.references[new_attrs] == 1


def test_owned_callback_result_publication_has_barrier_while_lease_is_live():
    model = SpecialCallModel()
    value = model.make(model.abi.PY_TYPE_LIST)
    slots = model.allocate(14 * 8)
    tokens = model.allocate(14 * 8)
    model.zero(slots, 0, 14 * 8)
    model.zero(tokens, 0, 14 * 8)
    result_slot = slots + 12 * 8
    model.empty(result_slot)
    model.store(result_slot, 0, value)
    barriers = []
    def barrier(owner, slot, current):
        assert model.depth > 0 and model.count(value) == 1
        barriers.append((owner, slot, current))
    model.ns["pcc_gc_note_slot_write_barrier"] = barrier
    assert model.ns["_special_adopt"](slots, tokens, 12) == 0
    assert barriers == [(0, result_slot, value)]
    assert model.release(result_slot, model.load(tokens, 12 * 8)) == 0


def test_extension_callable_never_uses_native_instance_class_layout():
    model = SpecialCallModel()
    extension = model.make(model.abi.PY_TYPE_CEXT_TAG_BASE)
    model.callbacks[extension] = lambda value: value
    original = model.ns["_ptr_is_instance"]
    def native_layout(value):
        assert value != extension, "extension has no native instance cls field"
        return original(value)
    model.ns["_ptr_is_instance"] = native_layout
    source, arguments, output = model.own(extension), model.own(model.tuple((61,))), model.own(0)
    assert model.ns["py_obj_call_slots"](source, arguments, 0, output) == 0
    assert model.load(output) == 61
    assert model.calls == [(extension, (61,), 0)]
    assert not model.registered and model.active() == 0


@pytest.mark.parametrize("failure", [False, True])
def test_selected_metaclass_call_consumes_and_restores_deferred_context(failure):
    model = SpecialCallModel()
    model.store("pcc_native_callable_sync_context", 0, 0)
    def callback(receiver):
        assert model.load("pcc_native_callable_sync_context") == 1
        if failure:
            model.error = model.exception("metaclass failure")
            return None
        return 63
    function = model.function(callback)
    cls = model.cls(meta=model.cls(namespace={"__call__": function}))
    source, output = model.own(cls), model.own(0)
    status = model.ns["py_obj_call_slots"](source, 0, 0, output)
    assert status == (-1 if failure else 0)
    assert model.load(output) == (0 if failure else 63)
    assert model.load("pcc_native_callable_sync_context") == 0
    assert not model.registered and model.active() == 0
