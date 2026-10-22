"""Execute instance-owner production bodies in a bounded memory/RC model.

This covers physical storage, teardown and replacement ownership. Forwarding
is injected only for the reserved payload while instance/class addresses stay
stable; it does not qualify the legacy raw-entry ABI or native moving GC.
"""
from __future__ import annotations

import ast
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest


RUNTIME = Path(__file__).resolve().parents[2] / "pcc/runtime/py"


@lru_cache(maxsize=1)
def _constants():
    values = {}
    for node in ast.parse((RUNTIME / "py_abi_constants.py").read_text()).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (TypeError, ValueError):
                pass
    return values


@lru_cache(maxsize=1)
def _production_code():
    path = RUNTIME / "py_class.py"
    names = {
        "_instance_reserved_owner_slot", "_dynamic_attr_slot", "_instance_dict_of",
        "_copy_instance_reserved_owner", "_instance_storage_slot_count",
        "_instance_builtin_payload_slot", "py_instance_copy_builtin_payload",
        "py_instance_new", "py_instance_dealloc",
        "py_dataclass_replace", "py_dataclass_replace_from_dict",
    }
    body = []
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            body.append(node)
    assert {node.name for node in body} == names
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future] + body, type_ignores=[])
    return compile(ast.fix_missing_locations(module), str(path), "exec")


class _Block:
    def __init__(self, size, kind):
        self.size, self.kind = size, kind
        self.data, self.alive = {}, True


class _Memory:
    def __init__(self, backend=4):
        self.abi, self.backend = _constants(), backend
        self.depth, self.on_lock, self.on_user_del = 0, None, None
        self.forward, self.finalizers = {}, {}
        self.retains, self.releases = Counter(), Counter()
        self.allocations, self.freed, self.tracked = [], [], []
        self.store_calls, self.resolved_slots, self.barriers = [], [], []
        ns = dict(self.abi)
        ns.update({
            "null": lambda: None, "ptr_is_null": lambda p: int(p is None),
            "ptr_add": self.add, "load_ptr": self.read_ptr,
            "load_i32": lambda p, o: self.read(p, o, 4, 0),
            "load_i64": lambda p, o: self.read(p, o, 8, 0),
            "store_ptr": self.write, "store_i64": self.write,
            "store_i32": lambda p, o, v: self.write(p, o, v, 4),
            "memset": self.memset,
            "_ptr_is_instance": lambda p: self.is_kind(p, "instance"),
            "_dealloc_ptr_is_instance": lambda p: self.is_kind(p, "instance"),
            "_ptr_is_class": lambda p: self.is_kind(p, "class"),
            "_ptr_can_have_header": lambda p: p is not None,
            "pcc_gc_note_relocation_read": lambda p: p,
            "pcc_gc_load_ptr": lambda _owner, slot: self.read_ptr(slot, 0),
            "pcc_gc_resolve_root_slot_unlocked": self.resolve_slot,
            "pcc_gc_store_ptr": self.store_owner,
            "pcc_gc_note_slot_write_barrier": self.barrier,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "py_incref": self.retain, "py_decref": self.release,
            "pcc_gc_alloc": self.allocate,
            "py_gc_track": lambda p: self.tracked.append(self.location(p)[0]),
            "py_gc_untrack": lambda _p: None,
            "py_weakref_invalidate": lambda _p: None,
            "py_user_del_dispatch": self.user_del,
            "pcc_gc_note_object_freeing": lambda _p: None,
            "pcc_gc_free_object_memory": self.free,
            "pcc_gc_pointer_is_managed": lambda p: int(self.location(p)[0].alive),
            "pcc_gc_object_index_find": lambda p: p,
            "_gc_backend_selected_fast": lambda: self.backend,
            "_lookup_field_index": lambda _cls, _name: -1,
        })
        exec(_production_code(), ns)
        self.ns = ns

    @staticmethod
    def location(pointer, offset=0):
        if isinstance(pointer, tuple):
            return pointer[0], pointer[1] + offset
        return pointer, offset

    def add(self, pointer, offset):
        return self.location(pointer, offset)

    def read(self, pointer, offset, width=8, default=None):
        block, offset = self.location(pointer, offset)
        assert block.alive, "read through stale owner"
        assert 0 <= offset and offset + width <= block.size, "read outside allocated storage"
        return block.data.get(offset, default)

    def read_ptr(self, pointer, offset=0):
        return self.read(pointer, offset)

    def write(self, pointer, offset, value, width=8):
        block, offset = self.location(pointer, offset)
        assert block.alive, "write through stale owner"
        assert 0 <= offset and offset + width <= block.size, "write outside allocated storage"
        block.data[offset] = value

    def memset(self, pointer, byte, size):
        block, offset = self.location(pointer)
        assert byte == 0 and 0 <= offset and offset + size <= block.size
        for relative in range(0, size, self.abi["C_POINTER_SIZE"]):
            self.write(block, offset + relative, None)

    def is_kind(self, pointer, kind):
        return pointer is not None and self.location(pointer)[0].kind == kind

    def payload(self):
        value = _Block(32, "payload")
        value.data[0] = 1
        return value

    def class_object(self, n_fields, flags):
        cls = _Block(self.abi["PYCLASSOBJECT_SIZE"], "class")
        cls.data.update({0: 1, self.abi["PYCLASSOBJECT_N_FIELDS_OFFSET"]: n_fields,
                         self.abi["PYOBJECTHEADER_FLAGS_OFFSET"]: flags,
                         self.abi["PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET"]: self.abi["PY_TYPE_INSTANCE"]})
        return cls

    def allocate(self, size, tag, flags):
        inst = _Block(size, "instance")
        inst.data.update({0: 1, self.abi["PYOBJECTHEADER_TYPE_TAG_OFFSET"]: tag,
                          self.abi["PYOBJECTHEADER_FLAGS_OFFSET"]: flags})
        self.allocations.append(inst)
        return inst

    def instance(self, n_fields=2, flags=2):
        cls = self.class_object(n_fields, flags)
        inst = self.ns["py_instance_new"](cls)
        return inst, cls

    def reserved(self, inst, n_fields):
        return self.add(inst, self.abi["PYINSTANCEOBJECT_FIELDS_OFFSET"] + max(n_fields, 0) * self.abi["C_POINTER_SIZE"])

    def populate(self, inst, n_fields):
        fields = []
        for index in range(max(n_fields, 0)):
            value = self.payload()
            self.write(inst, self.abi["PYINSTANCEOBJECT_FIELDS_OFFSET"] + index * self.abi["C_POINTER_SIZE"], value)
            fields.append(value)
        owner = self.payload()
        self.write(self.reserved(inst, n_fields), 0, owner)
        return fields, owner

    def retain(self, value):
        if value is not None:
            assert value.alive, "retained stale reserved payload"
            value.data[0] += 1
            self.retains[value] += 1

    def release(self, value):
        if value is not None:
            assert value.alive and value.data[0] > 0, "released stale or unowned payload"
            value.data[0] -= 1
            self.releases[value] += 1
            if value.data[0] == 0 and value in self.finalizers:
                self.finalizers[value]()

    def lock(self):
        if self.depth == 0 and self.on_lock is not None:
            hook, self.on_lock = self.on_lock, None
            hook()
        self.depth += 1

    def unlock(self):
        self.depth -= 1
        assert self.depth >= 0

    def resolve_slot(self, slot, offset):
        assert self.depth > 0, "owning resolution must be protected"
        location = self.location(slot, offset)
        value = self.read_ptr(location)
        if value in self.forward:
            value = self.forward[value]
            self.write(location, 0, value)
        self.resolved_slots.append(location)
        return value

    def barrier(self, owner, slot, value):
        assert self.depth > 0
        assert self.location(owner)[0] is self.location(slot)[0]
        assert self.read_ptr(slot) is value
        self.barriers.append((self.location(slot), value))

    def store_owner(self, owner, slot, value):
        self.store_calls.append((self.location(owner)[0], self.location(slot), value))
        old = self.read_ptr(slot)
        old = self.forward.get(old, old)
        if old is value:
            return
        self.retain(value)
        self.write(slot, 0, value)
        assert self.depth == 0, "terminal decref/finalizer remains outside caller graph lock"
        self.release(old)

    def user_del(self, inst):
        if self.on_user_del is not None:
            self.on_user_del(inst)

    def free(self, inst):
        block, _ = self.location(inst)
        assert block.alive, "double instance free"
        block.alive = False
        self.freed.append(block)


@pytest.mark.parametrize("n_fields", [-1, 0, 2])
@pytest.mark.parametrize("flags", [0, 2])
def test_reserved_storage_is_independent_of_public_dict_visibility(n_fields, flags):
    model = _Memory()
    inst, cls = model.instance(n_fields, flags)
    _fields, owner = model.populate(inst, n_fields)
    slot = model.ns["_instance_reserved_owner_slot"](inst, cls)
    assert slot == model.reserved(inst, n_fields)
    assert model.read_ptr(slot) is owner
    visible = model.ns["_dynamic_attr_slot"](inst)
    assert visible == (None if flags & 2 else slot)
    public_dict = model.ns["_instance_dict_of"](inst, cls)
    assert public_dict is (None if flags & 2 else owner)


@pytest.mark.parametrize("n_fields", [-1, 0, 2])
@pytest.mark.parametrize("flags", [0, 2])
def test_instance_allocation_reserves_slot_even_for_malformed_count(n_fields, flags):
    model = _Memory()
    inst, cls = model.instance(n_fields, flags)
    block, _ = model.location(inst)
    assert block.size == model.abi["PYINSTANCEOBJECT_SIZE"] + (max(n_fields, 0) + 1) * model.abi["C_POINTER_SIZE"]
    assert model.read_ptr(model.reserved(inst, n_fields)) is None
    assert model.read_ptr(inst, model.abi["PYINSTANCEOBJECT_CLS_OFFSET"]) is cls


@pytest.mark.parametrize("n_fields", [-1, 0, 2])
@pytest.mark.parametrize("flags", [0, 2])
def test_deallocation_clears_reserved_owner_before_reentrant_finalizer(n_fields, flags):
    model = _Memory()
    inst, _cls = model.instance(n_fields, flags)
    fields, owner = model.populate(inst, n_fields)
    slot = model.reserved(inst, n_fields)
    observed = []

    def finalize():
        observed.append(model.read_ptr(slot))
        # A reentrant cleanup reads the already-cleared physical slot and
        # cannot consume the same owner a second time.
        model.store_owner(inst, slot, None)

    model.finalizers[owner] = finalize
    model.write(inst, 0, 0)
    model.ns["py_instance_dealloc"](inst)
    assert observed == [None]
    assert owner.data[0] == 0 and model.releases[owner] == 1
    assert all(value.data[0] == 0 and model.releases[value] == 1 for value in fields)
    assert (model.location(inst)[0], slot, None) in model.store_calls
    assert model.freed == [model.location(inst)[0]]


@pytest.mark.parametrize("backend", [0, 3, 4])
@pytest.mark.parametrize("flags", [0, 2])
def test_resurrection_preserves_static_and_reserved_owners(backend, flags):
    model = _Memory(backend)
    inst, _cls = model.instance(2, flags)
    fields, owner = model.populate(inst, 2)
    model.write(inst, 0, 0)
    model.write(inst, model.abi["PYOBJECTHEADER_FLAGS_OFFSET"], 524288, 4)
    model.on_user_del = lambda value: model.write(value, 0, 1)
    model.ns["py_instance_dealloc"](inst)
    assert not model.releases and not model.freed and not model.store_calls
    assert model.read_ptr(model.reserved(inst, 2)) is owner
    assert all(value.data[0] == 1 for value in fields + [owner])
    for index, value in enumerate(fields):
        assert model.read_ptr(inst, model.abi["PYINSTANCEOBJECT_FIELDS_OFFSET"] + index * model.abi["C_POINTER_SIZE"]) is value
    assert model.read(inst, model.abi["PYOBJECTHEADER_FLAGS_OFFSET"], 4, 0) & 524288 == 0


@pytest.mark.parametrize("relocate_payload", [False, True])
@pytest.mark.parametrize("flags", [0, 2])
def test_reserved_owner_copy_retains_current_payload_once(flags, relocate_payload):
    model = _Memory()
    source, cls = model.instance(2, flags)
    destination = model.ns["py_instance_new"](cls)
    _fields, old = model.populate(source, 2)
    current = old
    if relocate_payload:
        current = model.payload()

        def forward():
            old.alive = False
            old.data[0] = 0
            model.forward[old] = current

        model.on_lock = forward
    model.ns["_copy_instance_reserved_owner"](source, destination, cls)
    assert model.depth == 0
    assert model.read_ptr(model.reserved(source, 2)) is current
    assert model.read_ptr(model.reserved(destination, 2)) is current
    assert current.data[0] == 2 and model.retains[current] == 1
    assert not model.releases
    assert model.reserved(source, 2) in model.resolved_slots
    assert model.barriers == [(model.reserved(destination, 2), current)]


@pytest.mark.parametrize("flags", [0, 2])
def test_empty_reserved_owner_copy_is_balanced(flags):
    model = _Memory()
    source, cls = model.instance(0, flags)
    destination = model.ns["py_instance_new"](cls)
    model.ns["_copy_instance_reserved_owner"](source, destination, cls)
    assert model.depth == 0
    assert model.read_ptr(model.reserved(destination, 0)) is None
    assert not model.retains and not model.releases and not model.barriers


@pytest.mark.parametrize("api", ["py_dataclass_replace", "py_dataclass_replace_from_dict"])
@pytest.mark.parametrize("flags", [0, 2])
@pytest.mark.parametrize("n_fields", [-1, 2])
def test_both_replacement_variants_preserve_and_balance_reserved_owner(api, flags, n_fields):
    model = _Memory()
    source, _cls = model.instance(n_fields, flags)
    fields, owner = model.populate(source, n_fields)
    if api == "py_dataclass_replace":
        destination = model.ns[api](source, 0, None, None)
    else:
        overrides = _Block(model.abi["PYDICTOBJECT_SIZE"], "dict")
        overrides.data[model.abi["PYOBJECTHEADER_TYPE_TAG_OFFSET"]] = model.abi["PY_TYPE_DICT"]
        overrides.data[model.abi["PYDICTOBJECT_ENTRIES_USED_OFFSET"]] = 0
        destination = model.ns[api](source, overrides)
    assert model.read_ptr(model.reserved(destination, n_fields)) is owner
    assert all(value.data[0] == 2 and model.retains[value] == 1 for value in fields + [owner])
    assert model.depth == 0
    model.write(destination, 0, 0)
    model.ns["py_instance_dealloc"](destination)
    assert all(value.data[0] == 1 and model.releases[value] == 1 for value in fields + [owner])
    assert model.read_ptr(model.reserved(source, n_fields)) is owner
    model.write(source, 0, 0)
    model.ns["py_instance_dealloc"](source)
    assert all(value.data[0] == 0 and model.releases[value] == 2 for value in fields + [owner])
