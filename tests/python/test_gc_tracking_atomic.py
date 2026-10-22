"""Actual tracking functions must preserve concurrent non-TRACKED updates.

The table lock owns list/index membership, not the graph-lock color changes
or atomic pin/lifetime flags. These are bounded host interleavings, not native
collector or object-lifetime qualification.
"""

from __future__ import annotations

import ast

import pytest

from tests.python.test_gc_mark_color_atomic import (
    BLACK, DYING, GRAY, PINNED, RUNTIME, HeaderMemory, _i32, _load,
)


pytestmark = pytest.mark.xdist_group("gc_tracking_atomic")
TRACKING = RUNTIME / "freestanding_gc_tracking.py"
TRACKED = 2
UPDATES = ("black", "pin", "unpin", "dying", "high_bit")


class TrackingState:
    def __init__(self, initial, update, *, inserted=1, owner=False, present=True,
                 allocation=True, backend=1):
        self.memory = HeaderMemory(initial)
        self.namespace = self.memory.namespace()
        self.update = update
        self.owner = owner
        self.inserted = inserted
        self.present = present
        self.allocation = allocation
        self.node = {0: self.memory.obj, 24: None, 32: None}
        self.globals = {
            "pcc_gc_table_lock_owner_token": {0: 99 if owner else 0},
            "pcc_gc_tracked_node_pool": None,
            "pcc_gc_tracked_node_pool_count": {0: 0},
            "py_gc_head": self.node if initial & TRACKED else None,
            "py_gc_tracked_count": {0: 1 if initial & TRACKED else 0},
            "pcc_gc_deferred_node_free_head": None,
        }
        self.events = []
        self.expected_nontracked = initial & ~TRACKED
        self.namespace.update({
            "ptr_is_null": lambda obj: int(obj is None),
            "is_tagged_int": lambda obj: 0,
            "null": lambda: None,
            "pcc_threads_enabled": lambda: 1,
            "gc_backend_current": lambda: backend,
            "atomic_load_i64": self.load_owner,
            "pcc_current_native_thread_token": lambda: 99,
            "ptr_diff": lambda ptr, base: ptr,
            "pcc_gc_default_table_lock": lambda: self.events.append("lock"),
            "pcc_gc_default_table_unlock": lambda: self.events.append("unlock"),
            "global_addr": lambda name: self.globals[name],
            "global_load_ptr": lambda name: self.globals[name],
            "global_store_ptr": lambda name, value: self.globals.__setitem__(name, value),
            "load_i32": lambda obj, offset: obj[offset],
            "load_ptr": lambda obj, offset: obj[offset],
            "store_i32": lambda obj, offset, value: obj.__setitem__(offset, _i32(value)),
            "store_i64": lambda obj, offset, value: obj.__setitem__(offset, value),
            "store_ptr": lambda obj, offset, value: obj.__setitem__(offset, value),
            "malloc": self.allocate,
            "free": lambda node: self.events.append("free"),
            "py_gc_index_insert": self.insert,
            "py_gc_index_remove": self.remove,
        })
        for name in ("_tracked_node_recycle", "pcc_gc_default_unlink_tracked_node",
                     "py_gc_track", "py_gc_untrack"):
            _load(TRACKING, name, self.namespace)

    def load_owner(self, slot, offset, order):
        assert slot is self.globals["pcc_gc_table_lock_owner_token"]
        assert (offset, order) == (0, "acquire")
        return slot[0]

    def allocate(self, size):
        assert size == 40
        self.events.append("allocate")
        return self.node if self.allocation else None

    def interfere(self):
        # Both index callbacks occur after the tracking function's flags
        # snapshot and before its membership publication. A different lock
        # protects the collector's color CAS; neither waits for this table lock.
        assert self.owner or self.events[0] == "lock"
        obj = self.memory.obj
        if self.update == "black":
            self.namespace["pcc_gc_set_mark_color"](obj, BLACK)
        elif self.update == "pin":
            self.memory.update(("or", PINNED))
        elif self.update == "unpin":
            self.memory.update(("and", ~PINNED))
        elif self.update == "dying":
            self.memory.update(("or", DYING))
        elif self.update == "high_bit":
            self.memory.update(("or", 0x80000000))
        else:
            raise AssertionError(self.update)
        self.expected_nontracked = obj[12] & ~TRACKED
        self.events.append("interference")

    def insert(self, obj, node):
        assert obj is self.memory.obj and node is self.node
        self.events.append("insert")
        self.interfere()
        return self.inserted

    def remove(self, obj):
        assert obj is self.memory.obj
        self.events.append("remove")
        self.interfere()
        return self.node if self.present else None

    def check_locks(self):
        assert self.events.count("lock") == (0 if self.owner else 1)
        assert self.events.count("unlock") == (0 if self.owner else 1)
        if not self.owner:
            assert self.events[0] == "lock" and self.events[-1] == "unlock"


@pytest.mark.parametrize("update", UPDATES)
@pytest.mark.parametrize("inserted", [1, 0])
@pytest.mark.parametrize("owner", [False, True])
def test_track_only_sets_membership_bit(update, inserted, owner):
    initial = GRAY if update == "pin" else GRAY | PINNED
    state = TrackingState(initial, update, inserted=inserted, owner=owner)
    state.namespace["py_gc_track"](state.memory.obj)
    assert state.memory.obj[12] == _i32(state.expected_nontracked | TRACKED)
    assert state.memory.writes[-1] == "or"
    assert state.globals["py_gc_tracked_count"][0] == (1 if inserted > 0 else 0)
    if inserted > 0:
        assert state.globals["py_gc_head"] is state.node
        assert state.node[0] is state.memory.obj
    else:
        assert state.globals["pcc_gc_tracked_node_pool"] is state.node
        assert state.globals["pcc_gc_tracked_node_pool_count"][0] == 1
    state.check_locks()


@pytest.mark.parametrize("update", UPDATES)
@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("owner", [False, True])
def test_untrack_only_clears_membership_bit(update, present, owner):
    initial = (GRAY if update == "pin" else GRAY | PINNED) | TRACKED
    state = TrackingState(initial, update,
                          present=present, owner=owner)
    state.namespace["py_gc_untrack"](state.memory.obj)
    assert state.memory.obj[12] == _i32(state.expected_nontracked)
    assert state.memory.writes[-1] == "and"
    assert state.globals["py_gc_tracked_count"][0] == (0 if present else 1)
    if present:
        assert state.globals["py_gc_head"] is None
        if owner:
            assert state.globals["pcc_gc_deferred_node_free_head"] is state.node
            assert state.globals["pcc_gc_tracked_node_pool_count"][0] == 0
        else:
            assert state.globals["pcc_gc_tracked_node_pool"] is state.node
            assert state.globals["pcc_gc_tracked_node_pool_count"][0] == 1
    state.check_locks()


@pytest.mark.parametrize("name,initial,backend", [
    ("py_gc_track", GRAY | TRACKED, 1),
    ("py_gc_untrack", GRAY, 1),
    ("py_gc_track", GRAY, 4),
    ("py_gc_untrack", GRAY | TRACKED, 4),
])
def test_tracking_early_returns_do_not_mutate_header(name, initial, backend):
    state = TrackingState(initial, "black", backend=backend)
    state.namespace[name](state.memory.obj)
    assert state.memory.obj[12] == initial
    assert not state.memory.writes
    assert "insert" not in state.events and "remove" not in state.events
    assert state.events == ([] if backend == 4 else ["lock", "unlock"])


@pytest.mark.parametrize("allocation", [False, True])
def test_failed_tracking_does_not_publish_membership(allocation):
    state = TrackingState(GRAY | PINNED, "black", inserted=-1, allocation=allocation)
    state.namespace["py_gc_track"](state.memory.obj)
    assert state.memory.obj[12] == _i32(state.expected_nontracked)
    assert "or" not in state.memory.writes and "and" not in state.memory.writes
    assert state.globals["py_gc_tracked_count"][0] == 0
    assert state.globals["py_gc_head"] is None
    state.check_locks()


def test_tracking_header_writes_are_exact_masked_atomic_operations():
    tree = ast.parse(TRACKING.read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "atomic_rmw_i32":
            assert ast.unparse(node.args[1]) == "obj"
            assert ast.literal_eval(node.args[2]) == 12
            assert ast.literal_eval(node.args[4]) == "acq_rel"
            writes.append((ast.literal_eval(node.args[0]), ast.unparse(node.args[3])))
        elif node.func.id == "store_i32":
            assert ast.unparse(node.args[0]) != "obj"
    assert sorted(writes) == [("and", "~2"), ("or", "2"), ("or", "2")]
