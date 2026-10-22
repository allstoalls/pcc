"""Host control-flow models of the actual GC pointer-load entry points.

The root-membership provider is opaque: this does not model or change root
spans, locks, native concurrency, relocation, or collector qualification.
"""

import ast
from collections import Counter
from itertools import product
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LOAD_SOURCE = ROOT / "pcc/runtime/py/py_obj.py"
IDENTITY_SOURCE = ROOT / "pcc/runtime/py/freestanding_gc_forwarding_identity.py"
ENTRY_POINTS = ("pcc_gc_load_ptr", "pcc_gc_load_borrowed_ptr")


def _load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"),
        namespace,
    )


class _LoadModel:
    SLOT = 4096
    OBJECT = 8192
    REPLACEMENT = 12288

    def __init__(
        self, *, source=LOAD_SOURCE, backend=4, enabled=1, initialized=1,
        population=0, pending=1, root=False, value=3, known=True,
        candidate=False, forwarded=False,
    ):
        self.cells = {self.SLOT: value}
        self.events = []
        self.counts = Counter()
        self.backend = backend
        self.root = root
        self.known = known
        self.flags = 2048 if candidate else 0
        self.forwarding = {self.OBJECT: self.REPLACEMENT} if forwarded else {}
        self.globals = {
            "pcc_gc_read_barrier_enabled": enabled,
            "pcc_gc_config_initialized": initialized,
            "pcc_gc_backend_selected": backend,
            "pcc_gc_forwarding_population": population,
            "pcc_gc_relocation_set_head": 16384 if pending else 0,
        }
        self.after_slot_read = None
        self.namespace = {
            "i64": int,
            "c_ptr": int,
            "null": lambda: 0,
            "ptr_eq": lambda left, right: int(left == right),
            "ptr_is_null": lambda value: int(value == 0),
            "is_tagged_int": lambda value: value & 1,
            "global_addr": lambda name: name,
            "global_load_ptr": self.global_get,
            "load_ptr": self.load_slot,
            "load_i32": self.load_i32,
            "store_ptr": self.store_slot,
            "pcc_gc_backend": self.configure,
            "pcc_gc_slot_is_runtime_root": self.is_root,
            "pcc_gc_note_load": self.note_load,
            "pcc_gc_note_relocation_read": self.resolve,
            "pcc_gc_forwarding_find": self.find_forwarding,
            "pcc_gc_object_is_known_no_lock": self.is_known,
            "_gc_relocation_candidate": self.gc3_candidate,
        }
        _load_functions(
            IDENTITY_SOURCE, {"pcc_gc_backend4_slot_needs_resolve"},
            self.namespace,
        )
        needs_resolve = self.namespace["pcc_gc_backend4_slot_needs_resolve"]

        def counted_resolver(value):
            self.note("needs_resolve", value)
            return needs_resolve(value)

        self.namespace["pcc_gc_backend4_slot_needs_resolve"] = counted_resolver
        _load_functions(
            source,
            set(ENTRY_POINTS) | {
                "_gc_backend_fast", "_gc_forwarding_population",
                "_gc_backend4_should_check_slot",
            },
            self.namespace,
        )

    def note(self, name, *args):
        self.counts[name] += 1
        self.events.append((name, *args))

    def global_get(self, name):
        self.note("global_read", name)
        return self.globals[name]

    def load_i32(self, pointer, offset):
        if isinstance(pointer, str):
            assert offset == 0
            return self.global_get(pointer)
        # A null/tagged/unknown value must never be dereferenced.
        assert pointer == self.OBJECT and self.known and offset == 12
        self.note("header_read", pointer)
        return self.flags

    def load_slot(self, slot, offset):
        assert slot == self.SLOT and offset == 0
        self.note("slot_read", slot)
        value = self.cells[slot]
        if self.after_slot_read is not None:
            self.after_slot_read(self)
        return value

    def store_slot(self, slot, offset, value):
        assert slot == self.SLOT and offset == 0
        self.note("slot_write", slot, value)
        self.cells[slot] = value

    def configure(self):
        self.note("configure")
        self.globals["pcc_gc_config_initialized"] = 1
        return self.backend

    def is_root(self, slot):
        assert slot == self.SLOT
        self.note("root_membership", slot)
        return int(self.root)

    def note_load(self):
        self.note("read_barrier_counter")

    def find_forwarding(self, value):
        assert value != 0 and value & 1 == 0
        self.note("forwarding_lookup", value)
        return self.forwarding.get(value, 0)

    def is_known(self, value):
        self.note("known_object_lookup", value)
        return int(value == self.OBJECT and self.known)

    def gc3_candidate(self, value):
        self.note("gc3_candidate", value)
        return int(value == self.OBJECT and self.known and self.flags != 0)

    def resolve(self, value):
        assert value == self.OBJECT
        self.note("relocation_read", value)
        return self.forwarding.get(value, value)

    def run(self, entry, *, null_slot=False):
        return self.namespace[entry](0, 0 if null_slot else self.SLOT)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("value", [0, 1, 3, (1 << 64) - 1])
def test_gc4_scalar_load_skips_membership_and_resolution(entry, value):
    for population, pending, root in product((0, 1), repeat=3):
        model = _LoadModel(value=value, population=population, pending=pending, root=root)
        assert model.run(entry) == value
        assert model.cells[model.SLOT] == value
        for name in (
            "root_membership", "read_barrier_counter", "needs_resolve",
            "forwarding_lookup", "known_object_lookup", "header_read",
            "relocation_read", "slot_write",
        ):
            assert model.counts[name] == 0, (name, population, pending, root)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_gc4_pointer_load_retains_existing_barrier_and_healing(entry):
    for population, pending, root, known, candidate, forwarded in product((0, 1), repeat=6):
        model = _LoadModel(
            value=_LoadModel.OBJECT, population=population, pending=pending,
            root=root, known=known, candidate=candidate, forwarded=forwarded,
        )
        checked = bool(population or (pending and not root))
        resolved = checked and (forwarded or (known and candidate))
        healed = resolved and forwarded
        expected = model.REPLACEMENT if healed else model.OBJECT
        assert model.run(entry) == expected
        assert model.cells[model.SLOT] == expected
        assert model.counts["root_membership"] == int(not population and pending)
        assert model.counts["read_barrier_counter"] == int(checked)
        assert model.counts["needs_resolve"] == int(checked)
        assert model.counts["relocation_read"] == int(resolved)
        assert model.counts["slot_write"] == int(healed)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_load_config_disabled_backend_and_gc3_controls(entry):
    for backend, enabled, initialized, value, population in product(
        range(5), (0, 1), (0, 1), (0, 3, _LoadModel.OBJECT), (0, 1),
    ):
        model = _LoadModel(
            backend=backend, enabled=enabled, initialized=initialized,
            value=value, population=population, pending=0,
        )
        assert model.run(entry) == value
        assert model.counts["configure"] == int(enabled and not initialized)
        if not enabled or backend < 3:
            assert model.counts["read_barrier_counter"] == 0
            assert model.counts["needs_resolve"] == 0
        if backend == 3:
            assert model.counts["read_barrier_counter"] == int(enabled and population)
            assert model.counts["gc3_candidate"] == int(enabled and population)
            assert model.counts["root_membership"] == 0
            assert model.counts["needs_resolve"] == 0


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_null_slot_returns_without_configuration_or_read(entry):
    model = _LoadModel(initialized=0)
    assert model.run(entry, null_slot=True) == 0
    assert not model.events


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_scalar_return_uses_existing_single_load_snapshot(entry):
    model = _LoadModel(value=3)
    model.after_slot_read = lambda state: state.cells.__setitem__(state.SLOT, state.OBJECT)
    assert model.run(entry) == 3
    assert model.cells[model.SLOT] == model.OBJECT
    assert model.counts["slot_read"] == 1
    assert model.counts["slot_write"] == 0
    # This sequential mutation double is a value-flow test, not a proof of
    # native memory ordering or concurrent root-registry behavior.


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_scalar_guard_precedes_membership_in_source(entry):
    tree = ast.parse(LOAD_SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry)
    body = ast.get_source_segment(LOAD_SOURCE.read_text(encoding="utf-8"), function)
    assert body.index("is_tagged_int(v)") < body.index("_gc_backend4_should_check_slot(slot)")
    assert body.index("_gc_backend4_should_check_slot(slot)") < body.index("pcc_gc_note_load()")
