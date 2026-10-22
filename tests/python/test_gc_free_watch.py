"""Host execution of production free-watch and caller bodies, not native GC proof.

Raw memory/TLS and deallocation are deterministic shims. The note function's
non-moving, untracked branch isolates its watch bookkeeping from reclamation.
Address reuse is injected at the deallocator boundary, not a GC4 execution.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"
pytestmark = pytest.mark.xdist_group("gc_free_watch")


def _load(filename, names, namespace):
    path = RUNTIME / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and (t.id.startswith("_FREE_WATCH_") or t.id == "GC_FREE_WATCH_BYTES")
            for t in node.targets
        ):
            nodes.append(node)
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


class WatchRuntime:
    def __init__(self):
        self.memory = {}
        self.next_address = 1000
        self.tls = {"pcc_gc_free_watch": 0}
        self.globals = {
            "pcc_gc_config_initialized": {0: 1},
            "pcc_gc_backend_selected": {0: 0},
            "pcc_diagnostics_runtime_log_fast_state": {0: 0},
        }
        self.notes = []
        self.probes = []
        self.managed = 1
        self.reused = set()
        self.locks = 0
        self.ns = {
            "i64": int, "null": lambda: 0,
            "ptr_is_null": lambda p: int(p == 0), "ptr_eq": lambda a, b: int(a == b),
            "global_load_ptr": lambda n: self.tls.get(n, 0),
            "global_store_ptr": lambda n, v: self.tls.__setitem__(n, v),
            "global_addr": lambda n: self.globals[n],
            "load_ptr": self.load, "load_i64": self.load, "load_i32": self.load,
            "store_ptr": self.store, "store_i64": self.store, "store_i32": self.store,
            "stack_alloc": self.allocate,
            "_object_graph_lock": lambda: self.lock(1),
            "_object_graph_unlock": lambda: self.lock(-1),
            "pcc_gc_object_index_find": lambda o: 0,
            "pcc_gc_granule_is_object_start": lambda o: 1,
            "_identity_remove": self.notes.append, "_gc_tracks_objects": lambda: 0,
        }
        _load("py_gc_backend.py", ["pcc_gc_free_watch_begin", "pcc_gc_free_watch_end",
                                  "pcc_gc_note_object_freeing"], self.ns)

    def allocate(self, size=24):
        address = self.next_address
        self.next_address += max(size, 64)
        self.memory[address] = {}
        return address

    def load(self, p, offset):
        assert not isinstance(p, int) or p not in self.reused, "stale address read"
        return (p if isinstance(p, dict) else self.memory[p])[offset]

    def store(self, p, offset, value):
        assert not isinstance(p, int) or p not in self.reused, "stale address write"
        (p if isinstance(p, dict) else self.memory[p])[offset] = value

    def lock(self, delta):
        self.locks += delta
        assert self.locks in (0, 1)

    def begin(self, obj):
        record = self.allocate()
        self.ns["pcc_gc_free_watch_begin"](record, obj)
        return record

    def end(self, record):
        return self.ns["pcc_gc_free_watch_end"](record)

    def note(self, obj):
        self.ns["pcc_gc_note_object_freeing"](obj)
        assert self.locks == 0


@pytest.mark.parametrize("noted", [False, True])
def test_record_initialization_and_normal_pop(noted):
    rt = WatchRuntime()
    obj = rt.allocate()
    record = rt.begin(obj)
    assert rt.memory[record] == {0: 0, 8: obj, 16: 0}
    if noted:
        rt.note(obj)
    assert rt.end(record) == int(noted)
    assert rt.tls["pcc_gc_free_watch"] == 0


def test_same_object_propagates_through_contiguous_nested_watches():
    rt = WatchRuntime()
    obj = rt.allocate()
    records = [rt.begin(obj) for _ in range(3)]
    rt.note(obj)
    assert [rt.memory[r][16] for r in records] == [0, 0, 1]
    for record in reversed(records):
        assert rt.end(record) == 1
    assert rt.tls["pcc_gc_free_watch"] == 0


def test_different_object_nesting_does_not_propagate():
    rt = WatchRuntime()
    a, b = rt.allocate(), rt.allocate()
    outer, inner = rt.begin(a), rt.begin(b)
    rt.note(b)
    assert rt.end(inner) == 1
    assert rt.end(outer) == 0


def test_watch_contract_is_innermost_not_arbitrary_ancestor():
    rt = WatchRuntime()
    a, b = rt.allocate(), rt.allocate()
    outer, middle, inner = rt.begin(a), rt.begin(b), rt.begin(a)
    rt.note(a)
    assert rt.end(inner) == 1
    assert rt.end(middle) == 0
    assert rt.end(outer) == 0  # No propagation across the intervening B watch.


def test_note_for_outer_object_does_not_search_past_inner_watch():
    rt = WatchRuntime()
    a, b = rt.allocate(), rt.allocate()
    outer, inner = rt.begin(a), rt.begin(b)
    rt.note(a)
    assert rt.end(inner) == 0
    assert rt.end(outer) == 0


def test_null_note_does_not_mark_watch():
    rt = WatchRuntime()
    record = rt.begin(0)
    rt.note(0)
    assert rt.end(record) == 0
    assert rt.notes == []


def _caller(rt, caller, deallocate, delayed):
    ns = rt.ns
    obj = rt.allocate()
    rt.memory[obj] = {0: 0, 8: 1, 12: 65536 if delayed else 0}

    def probe(o):
        assert o not in rt.reused, "reused object must not be probed"
        rt.probes.append(o)
        return rt.managed

    ns.update({
        "GC_FREE_WATCH_BYTES": 24, "PYOBJECTHEADER_REFCOUNT_OFFSET": 0,
        "PYOBJECTHEADER_FLAGS_OFFSET": 12, "PY_TYPE_GEN": 10,
        "PY_TYPE_INSTANCE": 20, "PY_TYPE_USER_CLASS_START": 100,
        "pcc_refcount_forget": lambda o: None, "py_gc_untrack": lambda o: None,
        "py_weakref_invalidate": lambda o: None, "py_user_del_dispatch": lambda o: None,
        "_dealloc_ptr_is_instance": lambda o: False,
        "_gc_backend_selected_fast": lambda: 4,
        "pcc_gc_pointer_is_managed": probe,
        "pcc_dealloc_with_trash": lambda o, tag: deallocate(o),
        "pcc_gc_free_object_memory": deallocate,
        "is_tagged_int": lambda o: 0, "pcc_gc_object_is_address_pinned": lambda o: 0,
        "abi_constant": lambda name: 1 if name == "object.type.int" else 99,
        "py_dealloc_int": deallocate,
    })
    if caller == "decref":
        _load("py_obj.py", ["_py_decref_finish"], ns)
        prepared = {0: obj, 8: 1, 16: rt.memory[obj][12], 24: 4, 32: 0, 40: 1, 48: 0}
        return obj, lambda: ns["_py_decref_finish"](prepared)
    if caller == "instance":
        _load("py_class.py", ["py_instance_dealloc"], ns)
        return obj, lambda: ns["py_instance_dealloc"](obj)
    _load("freestanding_gc_tracing_sweep_collector.py", ["pcc_gc_tracing_finalize_unreachable"], ns)
    # Caller selects GC4; the actual note function selects its non-moving
    # bookkeeping branch. This deliberately does not model zpage retirement.
    original_addr = ns["global_addr"]
    def caller_addr(name):
        if name == "pcc_gc_backend_selected":
            return {0: 4}
        return original_addr(name)
    def run():
        ns["global_addr"] = caller_addr
        original_note = ns["pcc_gc_note_object_freeing"]
        def note(o):
            ns["global_addr"] = original_addr
            try:
                original_note(o)
            finally:
                ns["global_addr"] = caller_addr
        ns["pcc_gc_note_object_freeing"] = note
        ns["pcc_gc_tracing_finalize_unreachable"](obj)
    return obj, run


@pytest.mark.parametrize("caller", ["decref", "instance", "collector"])
@pytest.mark.parametrize("outcome", ["noted_reused", "skipped_note", "skipped_unmanaged", "nested_same_object"])
def test_delayed_callers_balance_and_do_not_touch_reused_address(caller, outcome):
    rt = WatchRuntime()
    def deallocate(obj):
        if outcome in ("skipped_note", "skipped_unmanaged"):
            return
        inner = rt.begin(obj) if outcome == "nested_same_object" else None
        rt.note(obj)
        if inner is not None:
            assert rt.end(inner) == 1
        rt.reused.add(obj)
    rt.managed = int(outcome != "skipped_unmanaged")
    obj, run = _caller(rt, caller, deallocate, delayed=True)
    run()
    expected_notes = [] if outcome == "skipped_unmanaged" and caller != "collector" else [obj]
    assert rt.notes == expected_notes
    assert rt.probes == ([obj] if outcome.startswith("skipped_") and caller != "collector" else [])
    assert rt.tls["pcc_gc_free_watch"] == 0
    assert rt.locks == 0


@pytest.mark.parametrize("caller", ["decref", "instance", "collector"])
def test_nondelayed_callers_leave_enclosing_watch_balanced(caller):
    rt = WatchRuntime()
    enclosing = rt.begin(rt.allocate())
    obj, run = _caller(rt, caller, lambda o: None, delayed=False)
    run()
    assert rt.notes == [obj]
    assert rt.tls["pcc_gc_free_watch"] == enclosing
    assert rt.end(enclosing) == 0
