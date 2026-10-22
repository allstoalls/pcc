"""Exercise production store-buffer enqueue/index/drain bodies with host doubles.

These cases check queue/index mutation ordering and telemetry, including unlocked
cleanup reentry. They do not qualify emitted runtime behavior or native GC.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "pcc/runtime/py/py_gc_backend.py"
INDEX = ROOT / "pcc/runtime/py/freestanding_gc_index_table.py"
SCHEDULER = ROOT / "pcc/runtime/py/freestanding_gc_generational_scheduler.py"
SCHEDULER_TEST = ROOT / "tests/python/test_freestanding_gc_generational_scheduler.py"
QUEUE = "pcc_gc_backend4_store_buffer_"
MEMBERS = "pcc_py_gc_store_buffer_member_"


def _load_functions(path, namespace, names):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


class _Memory:
    def __init__(self):
        self.cells = {}
        self.blocks = {}
        self.next_pointer = 0x10000
        self.known = set()
        self.unreadable_headers = set()
        self.references = Counter()
        self.promotions = []
        self.cleanup = []
        self.cleanup_callback = None
        self.lock_depth = 0
        self.draining = False
        self.fail_calloc = False
        self.namespace = {
            "i64": int, "c_ptr": int,
            "null": lambda: 0,
            "ptr_is_null": lambda value: int(value == 0),
            "ptr_eq": lambda left, right: int(left == right),
            "ptr_diff": lambda left, right: left - right,
            "is_tagged_int": lambda value: int(bool(value & 1)),
            "logical_shift_right_i64": lambda value, shift: (value & ((1 << 64) - 1)) >> shift,
            "wrapping_mul_i64": lambda left, right: (left * right) & ((1 << 64) - 1),
            "global_addr": lambda name: name,
            "global_load_ptr": lambda name: self.read(name, 0),
            "global_store_ptr": lambda name, value: self.write(name, 0, value),
            "load_ptr": self.read, "load_i32": self.read, "load_i64": self.read,
            "store_ptr": self.write, "store_i32": self.write, "store_i64": self.write,
            "atomic_rmw_i32": self.atomic_rmw,
            "malloc": self.malloc, "calloc": self.calloc, "free": self.free,
            "py_incref": self.incref, "py_decref": self.decref,
            "_object_graph_lock": self.lock, "_object_graph_unlock": self.unlock,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "pcc_current_thread_id": lambda: 7,
            "pcc_gc_object_is_known_no_lock": lambda owner: int(owner in self.known),
            "pcc_gc_generational_promote_young_if_known": lambda value: self.promote("value", value),
            "pcc_gc_generational_promote_owned_slot_mode": lambda slot, mode, old: self.promote("slot", slot, mode, old),
            "pcc_gc_trace_referents_for_promotion": lambda owner: self.promote("owner", owner),
            "pcc_gc_backend3_drain_promotion_worklist": lambda budget: 0,
            "pcc_thread_safepoint": self.safepoint,
            "_backend4_remembered_set_add": lambda owner, slot: None,
        }
        _load_functions(INDEX, self.namespace, {
            "pcc_gc_index_py_hash_ptr", "pcc_gc_index_py_next_pow2",
            "pcc_gc_index_py_rehash_capacity", "pcc_gc_index_py_find_slot",
            "pcc_gc_index_py_rehash_slots", "pcc_gc_index_py_insert",
            "pcc_gc_index_py_remove",
            *("pcc_gc_store_buffer_member_" + suffix for suffix in
              ("note", "taint", "maybe", "forget", "owner_count", "clear")),
        })
        _load_functions(BACKEND, self.namespace, {
            "_store_buffer_head", "_set_store_buffer_head",
            "_store_buffer_medium_head", "_set_store_buffer_medium_head",
            "_backend4_store_buffer_contains", "_backend4_store_buffer_dec",
            "_backend4_store_buffer_medium_capacity", "_backend4_store_buffer_medium_count",
            "_backend4_store_buffer_medium_set_count",
            "_backend4_store_buffer_append_global_owned",
            "_backend4_note_cross_thread_medium_flush",
            "_backend4_store_buffer_flush_medium_locked", "_backend4_store_buffer_enqueue",
            "_backend4_store_buffer_owner_fanout", "_backend4_store_buffer_distinct_owners",
            "_backend4_store_buffer_owner_count", "_backend4_store_buffer_entry_count",
            "_backend4_store_buffer_max_owner_fanout", "_backend4_reset_store_buffer_epoch_state",
        })
        _load_functions(SCHEDULER, self.namespace, {
            "pcc_gc_backend4_step_remembered_roots",
        })

    def allocate(self, size):
        pointer = self.next_pointer
        self.next_pointer += ((size + 15) // 16) * 16 + 16
        self.blocks[pointer] = size
        return pointer

    def _assert_unlocked_drain_allocation(self):
        assert not (self.draining and self.lock_depth), "allocation/free in locked drain"

    def malloc(self, size):
        self._assert_unlocked_drain_allocation()
        return self.allocate(size)

    def calloc(self, count, size):
        self._assert_unlocked_drain_allocation()
        return 0 if self.fail_calloc else self.allocate(count * size)

    def free(self, pointer):
        self._assert_unlocked_drain_allocation()
        if pointer:
            size = self.blocks.pop(pointer)
            for address in [key for key in self.cells if isinstance(key, int)
                            and pointer <= key < pointer + size]:
                del self.cells[address]
        if self.draining:
            self.cleanup.append(("free", pointer, self.lock_depth))

    def _address(self, base, offset):
        if isinstance(base, str):
            return (base, offset)
        assert any(start <= base + offset < start + size
                   for start, size in self.blocks.items()), "read/write freed memory"
        return base + offset

    def read(self, base, offset):
        assert not (base in self.unreadable_headers and offset == 12), "unknown owner header read"
        return self.cells.get(self._address(base, offset), 0)

    def write(self, base, offset, value):
        self.cells[self._address(base, offset)] = value

    def atomic_rmw(self, operation, base, offset, value, ordering):
        assert operation == "and" and ordering == "acq_rel"
        old = self.read(base, offset)
        self.write(base, offset, old & value)
        return old

    def incref(self, value):
        self.references[value] += 1

    def decref(self, value):
        self.references[value] -= 1
        assert self.references[value] >= 0
        if self.draining:
            assert self.lock_depth == 0
            self.cleanup.append(("decref", value, self.lock_depth))
        callback, self.cleanup_callback = self.cleanup_callback, None
        if callback is not None:
            # Nested enqueue is a separate operation after drain unlocked.
            draining, self.draining = self.draining, False
            try:
                callback()
            finally:
                self.draining = draining

    def lock(self):
        assert self.lock_depth == 0
        self.lock_depth = 1

    def unlock(self):
        assert self.lock_depth == 1
        self.lock_depth = 0

    def promote(self, *record):
        assert self.lock_depth == 1
        self.promotions.append(record)

    def safepoint(self):
        assert self.lock_depth == 0

    def call(self, name, *args):
        return self.namespace[name](*args)

    def owner(self):
        owner = self.allocate(64)
        self.known.add(owner)
        self.write(owner, 12, 256)
        return owner

    def enqueue(self, owner, *, whole_owner=False):
        value = self.allocate(64)
        slot = 0 if whole_owner else self.allocate(8)
        if slot:
            self.write(slot, 0, value)
        self.lock()
        try:
            assert self.call("_backend4_store_buffer_enqueue", owner, slot, value) == 1
        finally:
            self.unlock()
        return slot, value

    def flush(self):
        self.lock()
        try:
            self.call("_backend4_store_buffer_flush_medium_locked")
        finally:
            self.unlock()

    def drain(self, budget=8):
        self.draining = True
        try:
            return self.call("pcc_gc_backend4_step_remembered_roots", budget)
        finally:
            self.draining = False

    def owners(self):
        owners = Counter()
        for head in ("medium_head", "head"):
            node = self.read(QUEUE + head, 0)
            while node:
                owners[self.read(node, 0)] += 1
                node = self.read(node, 24)
        return owners

    def count(self):
        return self.call("pcc_gc_store_buffer_member_owner_count")

    def maybe(self, owner):
        return self.call("pcc_gc_store_buffer_member_maybe", owner)

    def high_water(self):
        return self.read(QUEUE + "owner_count_high_water_count", 0)


def test_drained_live_owner_does_not_inflate_next_owner_or_stats_reset():
    memory = _Memory()
    first, second = memory.owner(), memory.owner()
    memory.enqueue(first)
    assert memory.count() == memory.high_water() == 1
    assert memory.drain() == 1
    assert memory.owners() == {}
    assert first in memory.known
    assert memory.count() == memory.maybe(first) == 0
    assert memory.high_water() == 1  # Historical high-water is not a live count.
    memory.call("_backend4_reset_store_buffer_epoch_state")
    assert memory.high_water() == 0
    memory.enqueue(second)
    assert memory.count() == memory.high_water() == 1
    assert memory.maybe(first) == 0 and memory.maybe(second) == 1


@pytest.mark.parametrize("queues", ["medium", "global", "both"])
def test_partial_batch_retains_owner_until_last_entry_without_index_allocation(queues):
    memory = _Memory()
    owner = memory.owner()
    for index in range(10):
        memory.enqueue(owner)
        if queues == "global" or (queues == "both" and index == 4):
            memory.flush()
    slots = memory.read(MEMBERS + "slots", 0)
    assert memory.drain(20) == 8
    assert memory.owners() == {owner: 2}
    assert memory.count() == memory.maybe(owner) == 1
    assert memory.read(owner, 12) & 512
    assert memory.drain() == 2
    assert memory.owners() == {}
    assert memory.count() == memory.maybe(owner) == 0
    assert memory.read(owner, 12) & 512 == 0
    assert memory.read(MEMBERS + "slots", 0) == slots
    assert len(memory.cleanup) == 20
    assert all(depth == 0 for _, _, depth in memory.cleanup)
    assert all(count == 0 for count in memory.references.values())


@pytest.mark.parametrize("state", ["unknown", "flag-clear"])
@pytest.mark.parametrize("global_queue", [False, True])
@pytest.mark.parametrize("entries", [1, 3])
def test_nonpromotable_entries_retire_membership_without_reading_unknown_header(
    state, global_queue, entries
):
    memory = _Memory()
    owner = memory.owner()
    for _ in range(entries):
        memory.enqueue(owner)
    if global_queue:
        memory.flush()
    if state == "unknown":
        memory.known.remove(owner)
        memory.unreadable_headers.add(owner)
    else:
        memory.write(owner, 12, 256)
    assert memory.drain(1) == 1
    if entries > 1:
        assert memory.count() == memory.maybe(owner) == 1
        assert memory.drain() == entries - 1
    assert memory.owners() == {}
    assert memory.count() == memory.maybe(owner) == 0
    assert memory.promotions == []


def test_cleanup_reenqueue_preserves_new_membership_after_drained_epoch():
    memory = _Memory()
    owner = memory.owner()
    memory.enqueue(owner)

    def reenqueue():
        assert memory.lock_depth == 0
        assert memory.owners() == {}
        assert memory.count() == memory.maybe(owner) == 0
        memory.enqueue(owner)

    memory.cleanup_callback = reenqueue
    assert memory.drain() == 1
    assert memory.owners() == {owner: 1}
    assert memory.count() == memory.maybe(owner) == 1
    assert memory.read(owner, 12) & 512
    assert memory.drain() == 1
    assert memory.count() == memory.maybe(owner) == 0


def test_retiring_one_owner_keeps_the_other_queue_owner_indexed():
    memory = _Memory()
    first, second = memory.owner(), memory.owner()
    memory.enqueue(first)
    memory.flush()
    memory.enqueue(second)
    assert memory.count() == 2
    assert memory.drain(1) == 1
    assert memory.owners() == {first: 1}
    assert memory.count() == memory.maybe(first) == 1
    assert memory.maybe(second) == 0
    assert memory.read(first, 12) & 512
    assert memory.read(second, 12) & 512 == 0
    memory.call("_backend4_reset_store_buffer_epoch_state")
    assert memory.high_water() == 1
    assert memory.drain() == 1
    assert memory.count() == 0


@pytest.mark.parametrize("failure", ["allocation", "taint"])
def test_incomplete_index_keeps_fallback_through_partial_and_full_drain(failure):
    memory = _Memory()
    owner = memory.owner()
    memory.fail_calloc = failure == "allocation"
    memory.enqueue(owner)
    memory.fail_calloc = False
    memory.enqueue(owner)
    if failure == "taint":
        memory.call("pcc_gc_store_buffer_member_taint")
    assert memory.count() == -1
    assert memory.drain(1) == 1
    assert memory.count() == -1 and memory.maybe(owner) == 1
    assert memory.call("_backend4_store_buffer_distinct_owners") == 1
    assert memory.drain() == 1
    assert memory.owners() == {}
    assert memory.count() == -1 and memory.maybe(owner) == 1
    assert memory.call("_backend4_store_buffer_distinct_owners") == 0
    memory.call("_backend4_reset_store_buffer_epoch_state")
    assert memory.high_water() == 0
    memory.call("pcc_gc_store_buffer_member_clear")
    assert memory.count() == memory.maybe(owner) == 0


def test_null_slot_owner_promotion_and_snapshot_promotion_are_preserved():
    memory = _Memory()
    owner = memory.owner()
    _, value = memory.enqueue(owner, whole_owner=True)
    assert memory.drain() == 1
    assert memory.promotions == [("value", value), ("owner", owner)]
    assert memory.count() == 0


def test_scheduler_store_buffer_forget_has_exact_admitted_raw_abi():
    from pcc.frontends.python.codegen.runtime_abi import (
        FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
        RUNTIME_SIGNATURES,
        is_freestanding_gc_cross_object_runtime_import,
    )
    from pcc.frontends.python.pipeline_freestanding import (
        freestanding_gc_cross_object_runtime_imports,
        freestanding_module_scope_extern_bindings,
        validate_freestanding_ir,
    )
    from pcc.frontends.python.pipeline_modes import PyPipelineError

    symbol = "pcc_gc_store_buffer_member_forget"
    source = SCHEDULER.read_text(encoding="utf-8")
    bindings = {name: (parameters, result) for name, parameters, result
                in freestanding_module_scope_extern_bindings(source)}
    assert bindings[symbol] == ("(c_ptr,)", "c_void")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == (("c_ptr",), "c_void")
    assert symbol not in RUNTIME_SIGNATURES  # Raw seam, not a managed ABI escape.
    allowed = freestanding_gc_cross_object_runtime_imports(source)
    assert symbol in allowed
    for parameters, result in [("()", "c_void"), ("(c_int64,)", "c_void"),
                               ("(c_ptr,c_ptr)", "c_void"), ("(c_ptr,)", "c_int64")]:
        assert not is_freestanding_gc_cross_object_runtime_import(
            symbol, parameters, result
        )

    # Exercise the real fail-closed verifier, not an emitted/native runtime.
    ir = (
        "define void @drain_abi_probe(ptr %owner) {\n"
        "  call void @" + symbol + "(ptr %owner)\n"
        "  ret void\n}\n"
    )
    validate_freestanding_ir(ir, allowed)
    with pytest.raises(PyPipelineError, match="managed-runtime reference"):
        validate_freestanding_ir(ir, allowed - {symbol})

    exported = [node for node in ast.parse(INDEX.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == symbol]
    assert len(exported) == 1
    function = exported[0]
    assert [arg.annotation.id for arg in function.args.args] == ["c_ptr"]
    assert isinstance(function.returns, ast.Constant) and function.returns.value is None
    assert any(isinstance(decorator, ast.Call)
               and isinstance(decorator.func, ast.Name)
               and decorator.func.id == "c_abi_export"
               and ast.literal_eval(decorator.args[0]) == symbol
               for decorator in function.decorator_list)
    closure = [node for node in ast.parse(SCHEDULER_TEST.read_text()).body
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "RAW_FUNCTION_IMPORTS"
                       for target in node.targets)]
    assert len(closure) == 1 and symbol in ast.literal_eval(closure[0].value)
