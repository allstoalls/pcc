"""Deterministic host interleavings of actual production GC color writers.

The only model is raw memory/atomic intrinsics. Function bodies are loaded
unchanged from the runtime source. These tests prove a lost-header-update
regression and owned host IR, not native threading or bootstrap qualification.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest


pytestmark = pytest.mark.xdist_group("gc_mark_color_atomic")
ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"
ROOT_OPS = RUNTIME / "freestanding_gc_root_operations.py"
SCHEDULER = RUNTIME / "freestanding_gc_incremental_concurrent_scheduler.py"
MANAGED = RUNTIME / "py_gc_backend.py"
COLOR = 56
GRAY = 16
BLACK = 32
PINNED = 64
DYING = 524288


def _function(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == name)


def _load(path, name, namespace):
    node = _function(path, name)
    # An export decorator registers a symbol; it is not part of the body.
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def _i32(value):
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


class HeaderMemory:
    def __init__(self, initial, interference=()):
        self.obj = {8: 7, 12: _i32(initial)}
        self.interference = list(interference)
        self.states = [self.obj[12]]
        self.cas_calls = []
        self.loads = 0
        self.writes = []

    def update(self, operation):
        kind, mask = operation
        old = self.obj[12]
        self.obj[12] = _i32(old | mask if kind == "or" else old & mask)
        self.states.append(self.obj[12])

    def load(self, obj, offset, order):
        assert obj is self.obj and offset == 12 and order == "acquire"
        self.loads += 1
        return obj[offset]

    def cas(self, obj, offset, expected, desired, order, fail_order):
        assert obj is self.obj and offset == 12
        assert (order, fail_order) == ("acq_rel", "acquire")
        if self.interference:
            self.update(self.interference.pop(0))
        old = obj[offset]
        self.cas_calls.append((expected, desired, old))
        if old == _i32(expected):
            obj[offset] = _i32(desired)
            self.states.append(obj[offset])
            self.writes.append("cas")
        return old

    def rmw(self, operation, obj, offset, value, order):
        assert obj is self.obj and offset == 12 and order == "acq_rel"
        old = obj[offset]
        self.update((operation, value))
        self.writes.append(operation)
        return old

    def namespace(self):
        namespace = {
            "i64": int, "PYOBJECTHEADER_FLAGS_OFFSET": 12,
            "atomic_load_i32": self.load, "atomic_cas_i32": self.cas,
            "atomic_rmw_i32": self.rmw,
        }
        # An absent helper is allowed only so the same call-site regression
        # can demonstrate the original stale-store failure on the preimage.
        if "def pcc_gc_set_mark_color(" in ROOT_OPS.read_text(encoding="utf-8"):
            _load(ROOT_OPS, "pcc_gc_set_mark_color", namespace)
        return namespace


@pytest.mark.parametrize("color", [8, GRAY, BLACK])
@pytest.mark.parametrize("initial,interference", [
    (GRAY, [("or", DYING)]),
    (GRAY, [("or", PINNED)]),
    (GRAY | PINNED, [("and", ~PINNED)]),
    (GRAY, [("or", DYING), ("or", PINNED)]),
    (0xFFFFFFD7, [("and", ~PINNED), ("and", ~DYING)]),
])
def test_color_cas_preserves_every_noncolor_bit(initial, interference, color):
    memory = HeaderMemory(initial, interference)
    expected = _i32(initial)
    for operation, mask in interference:
        expected = _i32(expected | mask if operation == "or" else expected & mask)
    namespace = memory.namespace()
    namespace["pcc_gc_set_mark_color"](memory.obj, color)
    assert memory.obj[12] == _i32((expected & ~COLOR) | color)
    assert memory.loads == 1, "retry must use the word observed by failed CAS"
    assert len(memory.cas_calls) == len(interference) + 1
    for previous, current in zip(memory.cas_calls, memory.cas_calls[1:]):
        assert current[0] == previous[2]
    assert memory.writes == ["cas"]
    assert all((flags & COLOR) != 0 for flags in memory.states)


def _scheduler_namespace(memory, operation):
    namespace = memory.namespace()
    node = {0: memory.obj, 16: None, 32: 0}
    globals_ = {
        "pcc_gc_mark_active": {0: 1},
        "pcc_gc_cycle_requested": {0: 0},
        "pcc_gc_trace_extension_roots_pending": {0: 0},
        "pcc_gc_trace_cext_pending_obj": None,
        "pcc_gc_trace_cursor": node,
        "pcc_gc_object_head": node,
    }
    locks = []
    trace = []
    counts = []

    def store(obj, offset, value):
        obj[offset] = value

    def trace_referents(obj):
        assert obj is memory.obj and locks == ["lock"]
        trace.append(obj)
        # The mutator does not take the graph lock. Publish after the
        # scheduler's flags snapshot, before its black transition.
        memory.update(operation)

    namespace.update({
        "stack_alloc": lambda size: {}, "null": lambda: None,
        "ptr_is_null": lambda obj: int(obj is None), "is_tagged_int": lambda obj: 0,
        "load_i32": lambda obj, offset: obj[offset],
        "load_i64": lambda obj, offset: obj[offset],
        "load_ptr": lambda obj, offset: obj[offset],
        "store_i32": store, "store_i64": store, "store_ptr": store,
        "global_addr": lambda name: globals_[name],
        "global_load_ptr": lambda name: globals_[name],
        "global_store_ptr": lambda name, value: globals_.__setitem__(name, value),
        "pcc_py_gc_minor_graph_lock": lambda: locks.append("lock"),
        "pcc_py_gc_minor_graph_unlock": lambda: locks.append("unlock"),
        "pcc_capi_is_cext_type_tag": lambda tag: 0,
        "pcc_gc_trace_referents": trace_referents,
        "pcc_gc_gray_count_decrement_acq_rel": lambda: counts.append(-1),
        # Leave another gray object pending so this slice does not finish.
        "pcc_gc_gray_count_load_acquire": lambda: 1,
    })
    return namespace, locks, trace, counts


@pytest.mark.parametrize("initial,operation", [
    (GRAY, ("or", DYING)),
    (GRAY, ("or", PINNED)),
    (GRAY | PINNED, ("and", ~PINNED)),
])
def test_scheduler_preserves_mutator_header_update(initial, operation):
    memory = HeaderMemory(initial)
    namespace, locks, trace, counts = _scheduler_namespace(memory, operation)
    step = _load(SCHEDULER, "pcc_gc_tracing_step_cycle", namespace)
    assert step(1) == 1
    kind, mask = operation
    expected = initial | mask if kind == "or" else initial & mask
    assert memory.obj[12] == (expected & ~COLOR) | BLACK
    assert locks == ["lock", "unlock"] and trace == [memory.obj] and counts == [-1]


@pytest.mark.parametrize("initial,operation", [
    (GRAY, ("or", DYING)),
    (GRAY, ("or", PINNED)),
    (GRAY | PINNED, ("and", ~PINNED)),
])
def test_terminal_gray_clear_preserves_mutator_header_update(initial, operation):
    memory = HeaderMemory(initial)
    namespace = memory.namespace()
    node = {80: 0}
    # No free watch is open on this thread (pcc_gc_free_watch_begin).
    globals_ = {
        "pcc_gc_config_initialized": {0: 1},
        "pcc_gc_backend_selected": {0: 1},
        "pcc_gc_free_watch": None,
    }
    counts, retired, released, locks = [], [], [], []

    def decrement():
        counts.append(-1)
        memory.update(operation)

    namespace.update({
        "stack_alloc": lambda size: {}, "null": lambda: None,
        "ptr_is_null": lambda obj: int(obj is None),
        "load_i32": lambda obj, offset: obj[offset],
        "load_i64": lambda obj, offset: obj[offset],
        "store_i32": lambda obj, offset, value: obj.__setitem__(offset, value),
        "store_ptr": lambda obj, offset, value: obj.__setitem__(offset, value),
        "global_addr": lambda name: globals_[name],
        "global_load_ptr": lambda name: globals_[name],
        "global_store_ptr": lambda name, value: globals_.__setitem__(name, value),
        "_object_graph_lock": lambda: locks.append("lock"),
        "_object_graph_unlock": lambda: locks.append("unlock"),
        "pcc_gc_object_index_find": lambda obj: node,
        "pcc_gc_granule_is_object_start": lambda obj: 1,
        "_identity_remove": lambda obj: None, "_gc_tracks_objects": lambda: 1,
        "_object_node_freeing": lambda obj: 0,
        "_gc_gray_count_decrement_acq_rel": decrement,
        "_object_node_size": lambda obj: 64,
        "_live_bytes_subtract": lambda size: retired.append(size),
        "_set_object_node_freeing": lambda obj, value: released.append(value),
        # A minor-block node remains linked, exactly like production's branch.
        "_object_node_minor_block": lambda obj: node,
    })
    freeing = _load(MANAGED, "pcc_gc_note_object_freeing", namespace)
    freeing(memory.obj)
    kind, mask = operation
    expected = initial | mask if kind == "or" else initial & mask
    assert memory.obj[12] == expected & ~GRAY
    assert memory.writes == ["and"]
    assert counts == [-1] and retired == [64] and released == [1]
    assert locks == ["lock", "unlock"]


def test_actual_helper_emits_one_strong_full_word_cas(tmp_path):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    from tests.owned_ir_validation import verify_ir_text

    node = _function(ROOT_OPS, "pcc_gc_set_mark_color")
    constants = ast.parse((RUNTIME / "py_abi_constants.py").read_text(encoding="utf-8"))
    flags_offset = next(node for node in constants.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "PYOBJECTHEADER_FLAGS_OFFSET"
                                for target in node.targets))
    source = (
        "from pcc import i64\n"
        "from pcc.extern import c_abi_export\n"
        "from pcc.unsafe import atomic_load_i32, atomic_cas_i32\n"
        "__pcc_freestanding__ = True\n" + ast.unparse(flags_offset) + "\n"
        + ast.unparse(node) + "\n"
    )
    path = tmp_path / "actual_mark_color.py"
    output = tmp_path / "actual_mark_color.ll"
    path.write_text(source, encoding="utf-8")
    pipeline.compile_python(str(path), str(output), emit_llvm_only=True,
                            libpython_mode="off", python_library=True)
    text = output.read_text(encoding="utf-8")
    verify_ir_text(text)
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES["pcc_gc_set_mark_color"] == (("c_ptr", "c_int64"), "c_void")
    assert re.search(r"define (?:external )?void @pcc_gc_set_mark_color\(ptr [^,]+, i64 [^)]+\)", text)
    assert text.count("cmpxchg ") == 1
    assert "cmpxchg weak" not in text
    assert re.search(r"cmpxchg ptr [^,]+, i32 [^,]+, i32 [^\n]+ acq_rel acquire", text)
    assert re.search(r"load atomic i32, ptr [^\n]+ acquire", text)
    assert "atomicrmw" not in text
    assert "store i32 " not in text


@pytest.mark.parametrize("filename,expected_calls", [
    ("freestanding_gc_root_operations.py", 1),
    ("freestanding_gc_common_mark_cycle.py", 3),
    ("freestanding_gc_barrier_dispatcher.py", 2),
    ("freestanding_gc_incremental_concurrent_scheduler.py", 2),
])
def test_active_color_writers_share_one_atomic_owner(filename, expected_calls):
    tree = ast.parse((RUNTIME / filename).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "pcc_gc_set_mark_color"]
    assert len(calls) == expected_calls
    for call in calls:
        assert ast.literal_eval(call.args[1]) in (GRAY, BLACK)
    # Keep this lock narrow: non-color writers and STW initialization are
    # explicitly outside this change, not claimed fixed by the helper.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "store_i32":
            assert "~56" not in ast.unparse(node)
