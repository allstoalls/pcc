"""Execute the owned cleanup helper bodies with hostile callbacks and moving roots.

These are deterministic production-body tests, not native GC qualification.
"""
from __future__ import annotations

from collections import Counter

import pytest

from pcc.runtime.py.py_abi_constants import C_POINTER_SIZE
from test_foreign_address_leases import RUNTIME, _functions


HELPERS = {
    "root": "py_cleanup_one_root_preserving_exception",
    "lease": "py_cleanup_one_lease_preserving_exception",
}
TWO_ROOT_HELPER = "py_cleanup_two_roots_preserving_exception"


class UnusedStatus:
    """A release status must neither escape nor influence cleanup."""

    def __bool__(self):
        pytest.fail("cleanup inspected the lease release status")

    def __eq__(self, other):
        pytest.fail("cleanup compared the lease release status")

    def __lt__(self, other):
        pytest.fail("cleanup compared the lease release status")

    def __gt__(self, other):
        pytest.fail("cleanup compared the lease release status")


class CleanupModel:
    def __init__(self, *, pending=True, moving=True):
        self.memory = {}
        self.addresses = {}
        self.objects = {}
        self.references = Counter()
        self.retired = Counter()
        self.leases = Counter()
        self.releases = []
        self.root_stores = []
        self.events = []
        self.callbacks = {}
        self.caller_slots = set()
        self.registered = set()
        self.caller_frame = object()
        self.frames = [self.caller_frame]
        self.allocations = []
        self.entered = []
        self.left = []
        self.next_address = 1000
        self.pending = 0
        self.moving = moving
        self.moves = 0
        self.release_status = 0
        if pending:
            self.set_exception("original")
        self.ns = {
            "C_POINTER_SIZE": C_POINTER_SIZE,
            "null": lambda: 0,
            "stack_alloc": self.stack_alloc,
            "store_ptr": self.store_ptr,
            "global_addr": lambda name: name,
            "pcc_gc_frame_enter_lifo": self.enter,
            "pcc_gc_frame_leave_lifo": self.leave,
            "py_tls_exc_swap_slot": self.swap,
            "pcc_gc_store_root": self.store_root,
            "pcc_gc_foreign_lease_release": self.release,
            "py_clear_exception": self.clear_exception,
        }
        _functions(
            RUNTIME / "py_cleanup_runtime.py",
            set(HELPERS.values()) | {TWO_ROOT_HELPER}, self.ns,
        )

    def allocate(self):
        self.next_address += 64
        return self.next_address

    def object(self, name):
        assert name not in self.addresses
        address = self.allocate()
        self.addresses[name] = address
        self.objects[address] = name
        return address

    def name(self, address):
        if address == 0:
            return None
        assert address in self.objects, "stale raw object survived a relocation"
        return self.objects[address]

    def own(self, address):
        if address:
            self.references[self.name(address)] += 1

    def drop(self, address):
        if address:
            name = self.name(address)
            assert self.references[name] > 0, "an owner was released twice"
            self.references[name] -= 1
            if not self.references[name]:
                self.retired[name] += 1

    def root(self, name=None, *, token=None):
        slot = self.allocate()
        self.caller_slots.add(slot)
        self.registered.add(slot)
        self.memory[slot] = self.object(name) if name else 0
        self.own(self.memory[slot])
        if token is not None:
            self.leases[slot, token] += 1
        return slot

    def set_exception(self, name):
        self.drop(self.pending)
        self.pending = self.object(name) if name else 0
        self.own(self.pending)

    def collect(self):
        if not self.moving:
            return
        # Only authoritative registered slots and TLS are healed. A helper
        # caching either object address across a call will see a stale value.
        leased = {
            self.memory[slot] for (slot, _token), count in self.leases.items() if count
        }
        for name, old in list(self.addresses.items()):
            if not self.references[name] or old in leased:
                continue
            new = self.allocate()
            self.addresses[name] = new
            self.objects[new] = self.objects.pop(old)
            for slot in self.registered:
                if self.memory[slot] == old:
                    self.memory[slot] = new
            if self.pending == old:
                self.pending = new
            self.moves += 1

    def stack_alloc(self, size):
        assert size == C_POINTER_SIZE
        slot = self.allocate()
        self.memory[slot] = object()  # Stack memory must be initialized.
        self.allocations.append(slot)
        self.events.append("allocate")
        return slot

    def store_ptr(self, slot, offset, value):
        assert slot in self.allocations and slot not in self.registered
        assert offset == 0 and value == 0
        self.memory[slot] = value
        self.events.append("initialize")

    def enter(self, frame_map, slot):
        assert frame_map == "pcc_cleanup_exception_map"
        assert slot in self.allocations and self.memory[slot] == 0
        assert slot not in self.registered
        self.frames.append(slot)
        self.registered.add(slot)
        self.entered.append(slot)
        self.events.append("enter")
        self.collect()

    def leave(self, slot):
        assert slot != self.caller_frame, "cleanup left the caller's frame"
        assert slot == self.frames[-1], "cleanup frames must leave in LIFO order"
        assert self.memory[slot] == 0, "saved exception ownership leaked"
        self.frames.pop()
        self.registered.remove(slot)
        self.left.append(slot)
        self.events.append("leave")
        self.collect()

    def swap(self, slot):
        assert slot in self.registered and slot in self.allocations
        self.collect()
        self.name(self.memory[slot])
        self.name(self.pending)
        self.pending, self.memory[slot] = self.memory[slot], self.pending
        self.events.append("swap")

    def store_root(self, slot, value):
        assert slot in self.caller_slots and slot in self.registered
        assert value == 0
        self.collect()
        old = self.memory[slot]
        self.root_stores.append((slot, self.name(old)))
        self.memory[slot] = 0
        self.drop(old)
        self.events.append("store-root")
        if old and slot in self.callbacks:
            self.callbacks[slot]()

    def release(self, slot, token):
        assert slot in self.caller_slots and slot in self.registered
        self.collect()
        self.releases.append((slot, token, self.name(self.memory[slot])))
        if self.leases[slot, token]:
            self.leases[slot, token] -= 1
        self.events.append("release")
        if slot in self.callbacks:
            self.callbacks[slot]()
        return self.release_status

    def clear_exception(self):
        self.collect()
        self.drop(self.pending)
        self.pending = 0
        self.events.append("clear")

    def cleanup(self, kind, slot, token=1):
        helper = self.ns[HELPERS[kind]]
        return helper(slot) if kind == "root" else helper(slot, token)

    def assert_balanced(self, *, pending):
        assert self.frames == [self.caller_frame]
        assert self.registered == self.caller_slots
        assert sorted(self.entered) == sorted(self.allocations)
        assert sorted(self.left) == sorted(self.allocations)
        assert all(self.memory[slot] == 0 for slot in self.allocations)
        assert self.name(self.pending) == ("original" if pending else None)
        assert self.references["original"] == int(pending)
        assert self.retired["original"] == 0


@pytest.mark.parametrize("kind", HELPERS)
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("callback", ["clear", "replace"])
def test_cleanup_preserves_moving_original_exception_and_caller_frame(kind, pending, callback):
    model = CleanupModel(pending=pending)
    slot = model.root("owner", token=1 if kind == "lease" else None)

    def hostile_callback():
        assert model.pending == 0
        assert model.frames[-1] in model.allocations
        assert model.name(model.memory[model.frames[-1]]) == ("original" if pending else None)
        model.set_exception("discarded-callback-error")
        model.set_exception(None if callback == "clear" else "callback-error")
        model.collect()

    model.callbacks[slot] = hostile_callback
    assert model.cleanup(kind, slot) is None
    model.assert_balanced(pending=pending)
    assert model.moves > 0
    assert model.retired["discarded-callback-error"] == 1
    assert model.references["callback-error"] == 0
    assert model.retired["callback-error"] == int(callback == "replace")
    assert model.references["owner"] == int(kind == "lease")
    assert model.retired["owner"] == int(kind == "root")
    assert model.events == [
        "allocate", "initialize", "enter", "swap",
        "store-root" if kind == "root" else "release", "clear", "swap", "leave",
    ]
    if kind == "root":
        assert model.memory[slot] == 0
        assert model.root_stores == [(slot, "owner")]
        assert model.releases == []
    else:
        assert model.name(model.memory[slot]) == "owner"
        assert model.root_stores == []
        assert model.releases == [(slot, 1, "owner")]
        assert model.leases[slot, 1] == 0


@pytest.mark.parametrize("pending", [False, True])
def test_empty_and_already_cleared_root_never_release_twice(pending):
    model = CleanupModel(pending=pending)
    empty = model.root()
    owner = model.root("owner")
    calls = []
    model.callbacks[empty] = lambda: pytest.fail("empty root invoked finalizer")
    model.callbacks[owner] = lambda: calls.append("finalizer")
    for slot in (empty, owner, owner):
        assert model.cleanup("root", slot) is None
        model.assert_balanced(pending=pending)
    assert model.root_stores == [(empty, None), (owner, "owner"), (owner, None)]
    assert calls == ["finalizer"]
    assert model.references["owner"] == 0 and model.retired["owner"] == 1
    assert model.releases == []


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("empty", [(), ("first",), ("second",), ("first", "second")])
def test_two_roots_share_one_exception_scope_and_reread_moving_owners(pending, empty):
    model = CleanupModel(pending=pending)
    first = model.root(None if "first" in empty else "first")
    second = model.root(None if "second" in empty else "second")
    callbacks = []
    moved_first = []

    def second_callback():
        assert model.pending == 0
        assert model.memory[second] == 0
        assert first in model.registered and second in model.registered
        callbacks.append(("second", model.name(model.pending)))
        model.set_exception("second-error")
        before = model.memory[first]
        model.collect()
        if before:
            assert model.memory[first] != before
            moved_first.append(model.memory[first])

    def first_callback():
        # A chain of one-root helpers would clear second-error too early.
        visible = None if "second" in empty else "second-error"
        assert model.name(model.pending) == visible
        assert model.memory[first] == model.memory[second] == 0
        callbacks.append(("first", model.name(model.pending)))
        model.set_exception("first-error")
        model.collect()

    model.callbacks[first] = first_callback
    model.callbacks[second] = second_callback
    assert model.ns[TWO_ROOT_HELPER](first, second) is None
    model.assert_balanced(pending=pending)
    assert len(model.allocations) == 1
    assert model.root_stores == [
        (second, None if "second" in empty else "second"),
        (first, None if "first" in empty else "first"),
    ]
    assert callbacks == [
        entry for name, entry in (
            ("second", ("second", None)),
            ("first", ("first", None if "second" in empty else "second-error")),
        ) if name not in empty
    ]
    assert bool(moved_first) == (not empty)
    for name in ("first", "second"):
        assert model.references[name] == model.references[name + "-error"] == 0
        assert model.retired[name] == model.retired[name + "-error"] == int(name not in empty)
    assert model.events == [
        "allocate", "initialize", "enter", "swap", "store-root", "store-root",
        "clear", "swap", "leave",
    ]
    assert model.releases == []


@pytest.mark.parametrize("pending", [False, True])
def test_two_root_callback_can_reenter_cleanup_before_later_owner(pending):
    model = CleanupModel(pending=pending)
    first, second = model.root("first"), model.root("second")
    inner = model.root("inner")
    seen = []

    def inner_callback():
        assert model.pending == 0
        assert len(model.frames) == 3
        assert model.name(model.memory[model.frames[-1]]) == "second-error"
        model.set_exception("inner-error")
        model.collect()

    def second_callback():
        model.set_exception("second-error")
        model.cleanup("root", inner)
        assert model.name(model.pending) == "second-error"
        assert len(model.frames) == 2

    def first_callback():
        seen.append(model.name(model.pending))
        model.set_exception("first-error")

    model.callbacks.update({first: first_callback, second: second_callback, inner: inner_callback})
    model.ns[TWO_ROOT_HELPER](first, second)
    model.assert_balanced(pending=pending)
    assert seen == ["second-error"]
    assert model.root_stores == [(second, "second"), (inner, "inner"), (first, "first")]
    assert model.left == list(reversed(model.entered))
    assert all(model.retired[name] == 1 for name in (
        "first", "second", "inner", "first-error", "second-error", "inner-error",
    ))


@pytest.mark.parametrize("token", [-1, 0, 1, 2, 37])
@pytest.mark.parametrize("status", [0, -3, pytest.param(UnusedStatus(), id="uninspectable")])
def test_lease_forwards_exact_token_ignores_status_and_keeps_root(token, status):
    model = CleanupModel()
    slot = model.root("owner", token=token)
    model.release_status = status
    model.callbacks[slot] = lambda: model.set_exception("release-error")
    assert model.cleanup("lease", slot, token) is None
    model.assert_balanced(pending=True)
    assert model.releases == [(slot, token, "owner")]
    assert model.leases[slot, token] == 0
    assert model.name(model.memory[slot]) == "owner"
    assert model.references["owner"] == 1 and model.retired["owner"] == 0
    assert model.retired["release-error"] == 1
    assert model.root_stores == []


@pytest.mark.parametrize("outer_kind", HELPERS)
@pytest.mark.parametrize("inner_kind", HELPERS)
@pytest.mark.parametrize("pending", [False, True])
def test_nested_cleanup_uses_independent_saved_exception_slots(outer_kind, inner_kind, pending):
    model = CleanupModel(pending=pending)
    outer = model.root("outer-owner", token=2 if outer_kind == "lease" else None)
    inner = model.root("inner-owner", token=1 if inner_kind == "lease" else None)
    called = Counter()

    def inner_callback():
        called["inner"] += 1
        assert model.pending == 0
        assert len(model.frames) == 3
        assert model.frames[-2] != model.frames[-1]
        assert model.name(model.memory[model.frames[-2]]) == ("original" if pending else None)
        assert model.name(model.memory[model.frames[-1]]) == "outer-error"
        model.set_exception("inner-error")
        model.collect()

    def outer_callback():
        called["outer"] += 1
        assert model.pending == 0
        saved = model.frames[-1]
        model.set_exception("outer-error")
        assert model.cleanup(inner_kind, inner, 1) is None
        assert model.frames == [model.caller_frame, saved]
        assert model.name(model.pending) == "outer-error"
        assert model.retired["inner-error"] == 1
        model.collect()

    model.callbacks[outer] = outer_callback
    model.callbacks[inner] = inner_callback
    assert model.cleanup(outer_kind, outer, 2) is None
    model.assert_balanced(pending=pending)
    assert called == Counter(outer=1, inner=1)
    assert len(set(model.allocations)) == 2
    assert model.left == list(reversed(model.entered))
    assert model.retired["outer-error"] == model.retired["inner-error"] == 1
    assert model.references["outer-error"] == model.references["inner-error"] == 0
    for slot, name, kind, token in (
        (outer, "outer-owner", outer_kind, 2), (inner, "inner-owner", inner_kind, 1),
    ):
        assert model.references[name] == int(kind == "lease")
        assert model.retired[name] == int(kind == "root")
        assert model.leases[slot, token] == 0
        if kind == "lease":
            assert model.releases.count((slot, token, name)) == 1
            assert model.name(model.memory[slot]) == name
        else:
            assert model.root_stores.count((slot, name)) == 1
            assert model.memory[slot] == 0
