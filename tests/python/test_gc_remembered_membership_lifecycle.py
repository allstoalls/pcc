"""Execute production remembered-list and index functions with raw-memory doubles.

The host cases prove list/filter mutation ordering, including allocator callback
reentry; they are not native GC or performance evidence. A separate bounded
native index-object test exercises the owned self emitter and a host C harness.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "pcc/runtime/py/py_gc_backend.py"
INDEX = ROOT / "pcc/runtime/py/freestanding_gc_index_table.py"
ABI = ROOT / "pcc/frontends/python/codegen/runtime_abi.py"
HEAD = "pcc_gc_backend4_remembered_slots_head"
PREFIX = "pcc_py_gc_remembered_member_"


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
        self.head_reads = 0
        self.list_reads = 0
        self.list_nodes = set()
        self.fail_calloc = False
        self.fail_malloc = False
        self.callbacks = {}
        self.store_buffer_removes = []
        self.namespace = {
            "i64": int, "c_ptr": int,
            "null": lambda: 0,
            "ptr_is_null": lambda value: int(value == 0),
            "ptr_eq": lambda left, right: int(left == right),
            "ptr_add": lambda base, offset: base + offset,
            "ptr_diff": lambda left, right: left - right,
            "ptr_to_int": int,
            "is_tagged_int": lambda value: int(bool(value & 1)),
            "logical_shift_right_i64": lambda value, shift: (value & ((1 << 64) - 1)) >> shift,
            "wrapping_mul_i64": lambda left, right: (left * right) & ((1 << 64) - 1),
            "global_addr": lambda name: name,
            "global_load_ptr": lambda name: self.read(name, 0),
            "global_store_ptr": lambda name, value: self.write(name, 0, value),
            "load_ptr": self.read, "load_i8": self.read,
            "load_i32": self.read, "load_i64": self.read,
            "store_ptr": self.write, "store_i8": self.write,
            "store_i32": self.write, "store_i64": self.write,
            "malloc": self.malloc, "calloc": self.calloc, "free": self.free,
            "stack_alloc": self.allocate,
            "_backend4_remembered_entry_attach": lambda owner, slot: None,
            "_backend4_remembered_entry_detach": lambda owner, slot: None,
            "_backend4_zpage_note_remembered_slot": lambda owner, delta: None,
            "_backend4_zpage_note_remembered_card": lambda owner, slot, delta: None,
            "_backend4_remembered_page_add": lambda slot: None,
            "_backend4_remembered_page_remove_slot": lambda slot: None,
            "_backend4_remembered_pages_clear": lambda: None,
            "_init_config": lambda: 4,
            "_object_graph_lock": lambda: None, "_object_graph_unlock": lambda: None,
            "_backend4_zpage_find": lambda owner: self.owner_node,
            "_backend4_zpage_card_for_node_slot": lambda node, slot: slot // 512,
            "pcc_gc_object_index_find": lambda owner: 0,
            "pcc_gc_granule_is_object_start": lambda owner: 1,
            "_forwarding_detach_into_finish": lambda owner, finish: None,
            "_forwarding_remove_target": lambda owner, finish: None,
            "_identity_remove": lambda owner: None,
            "_relocation_set_remove": lambda owner: None,
            "_backend4_store_buffer_remove": self.store_buffer_removes.append,
            "_gc_tracks_objects": lambda: 0,
            "_backend4_finish_remap_retirement": lambda finish: None,
        }
        index_names = {
            "pcc_gc_index_py_hash_ptr", "pcc_gc_index_py_next_pow2",
            "pcc_gc_index_py_rehash_capacity", "pcc_gc_index_py_find_slot",
            "pcc_gc_index_py_rehash_slots", "pcc_gc_index_py_insert",
            "pcc_gc_index_py_remove",
            *("pcc_gc_remembered_member_" + suffix
              for suffix in ("note", "maybe", "require", "forget", "clear")),
        }
        _load_functions(INDEX, self.namespace, index_names)
        _load_functions(BACKEND, self.namespace, {
            "_remembered_set_head", "_set_remembered_set_head",
            "_backend4_remembered_set_contains", "_backend4_owner_remembered_slots",
            "_backend4_remembered_set_add", "_backend4_remembered_set_remove",
            "_backend4_remembered_set_remove_slot", "_backend4_remembered_set_clear",
            "_backend4_remembered_set_retarget_slot",
            "pcc_gc_backend4_zpage_clear_remembered_card",
            "_store_buffer_head", "_store_buffer_medium_head",
            "_backend4_map_mutator_payload_slot",
            "pcc_gc_backend4_retarget_mutator_payload_locked",
            "pcc_gc_note_object_freeing",
        })
        self.owner_node = self.allocate(80)
        self.page = self.allocate(256)
        self.write(self.owner_node, 8, self.page)
        self.write("pcc_gc_config_initialized", 0, 1)
        self.write("pcc_gc_backend_selected", 0, 4)

    def allocate(self, size):
        base = self.next_pointer
        self.next_pointer += ((size + 15) // 16) * 16 + 16
        self.blocks[base] = size
        return base

    def _callback(self, kind, pointer):
        callback = self.callbacks.pop(kind, None)
        if callback is not None:
            callback(pointer)

    def malloc(self, size):
        if self.fail_malloc:
            return 0
        pointer = self.allocate(size)
        if size == 24:
            self.list_nodes.add(pointer)
        self._callback("malloc", pointer)
        return pointer

    def calloc(self, count, size):
        self._callback("calloc", 0)
        return 0 if self.fail_calloc else self.allocate(count * size)

    def free(self, pointer):
        if pointer:
            assert pointer in self.blocks, "double free or unknown allocation"
            del self.blocks[pointer]
            for key in [key for key in self.cells if isinstance(key, int)
                        and pointer <= key < pointer + 8]:
                self.cells.pop(key)
        self._callback("free", pointer)

    def _address(self, base, offset):
        if isinstance(base, str):
            return (base, offset)
        assert any(start <= base + offset < start + size
                   for start, size in self.blocks.items()), "read/write freed memory"
        return base + offset

    def read(self, base, offset):
        if base == HEAD:
            self.head_reads += 1
        if base in self.list_nodes:
            self.list_reads += 1
        return self.cells.get(self._address(base, offset), 0)

    def write(self, base, offset, value):
        self.cells[self._address(base, offset)] = value

    def owner(self, flags=0):
        owner = self.allocate(64)
        self.write(owner, 12, flags)
        return owner

    def call(self, name, *args):
        return self.namespace[name](*args)

    def add(self, owner, slot=None):
        return self.call("_backend4_remembered_set_add", owner, slot or owner + 24)

    def maybe(self, owner):
        return self.call("pcc_gc_remembered_member_maybe", owner)

    def members(self):
        out = []
        node = self.read(HEAD, 0)
        while node:
            out.append((self.read(node, 0), self.read(node, 8)))
            node = self.read(node, 16)
        return out

    def assert_conservative(self):
        for owner, _ in self.members():
            assert self.maybe(owner) == 1


def test_nonmember_free_does_not_read_remembered_nodes():
    memory = _Memory()
    owners = [memory.owner() for _ in range(64)]
    for owner in owners:
        assert memory.add(owner) == 1
    absent = memory.owner()
    memory.head_reads = memory.list_reads = 0
    for _ in range(20):
        memory.call("pcc_gc_note_object_freeing", absent)
    assert memory.list_reads == 0
    assert memory.head_reads == 0
    assert memory.store_buffer_removes == [absent] * 20
    assert len(memory.members()) == 64


def test_nonmember_payload_retarget_does_not_read_remembered_nodes():
    # A pcc1 GC4 compile walked 18,837 remembered entries on every list/set
    # payload growth although most owners held none.
    memory = _Memory()
    for _ in range(64):
        assert memory.add(memory.owner()) == 1
    absent = memory.owner()
    old_payload, new_payload = memory.allocate(32), memory.allocate(64)
    memory.head_reads = memory.list_reads = 0
    assert memory.call("pcc_gc_backend4_retarget_mutator_payload_locked",
                       absent, old_payload, 32, new_payload, 64, 0, 0) == 2
    assert memory.list_reads == 0
    assert memory.head_reads == 0
    assert len(memory.members()) == 64


def test_duplicate_multiple_slots_and_cleared_header_still_remove_real_member():
    memory = _Memory()
    owner = memory.owner(flags=512)
    other = memory.owner()
    assert memory.add(owner) == 1
    assert memory.add(owner) == 0
    assert memory.add(owner, owner + 32) == 1
    assert memory.add(other) == 1
    memory.write(owner, 12, 0)  # Drain clears 512 but remembered slots survive.
    memory.call("pcc_gc_note_object_freeing", owner)
    assert memory.members() == [(other, other + 24)]
    assert memory.maybe(owner) == 0
    assert memory.maybe(other) == 1


@pytest.mark.parametrize("method", ["slot", "card"])
def test_partial_deletion_keeps_other_slots_and_stale_keys_are_safe(method):
    memory = _Memory()
    owner = memory.owner()
    slots = memory.allocate(1024)
    for slot in (slots, slots + 8, slots + 600):
        memory.add(owner, slot)
    if method == "slot":
        assert memory.call("_backend4_remembered_set_remove_slot", slots) == 1
        assert len(memory.members()) == 2
    else:
        assert memory.call("pcc_gc_backend4_zpage_clear_remembered_card", owner, slots) == 2
        assert memory.members() == [(owner, slots + 600)]
    assert memory.maybe(owner) == 1
    memory.call("_backend4_remembered_set_remove", owner)
    assert memory.members() == [] and memory.maybe(owner) == 0
    memory.free(owner)
    memory.blocks[owner] = 64  # Recycle the raw address for a new lifetime.
    for address in range(owner, owner + 64):
        memory.cells.pop(address, None)
    memory.write(owner, 12, 0)
    assert memory.add(owner) == 1
    assert memory.maybe(owner) == 1
    memory.call("_backend4_remembered_set_remove", owner)
    assert memory.members() == []


def test_retarget_multiple_slots_no_match_and_payload_growth_preserve_owner_membership():
    memory = _Memory()
    old, new = memory.owner(), memory.owner()
    memory.add(old, old + 24)
    memory.add(old, old + 32)
    memory.call("_backend4_remembered_set_retarget_slot", old, new, old + 24, new + 24)
    assert Counter(owner for owner, _ in memory.members()) == Counter({old: 1, new: 1})
    memory.assert_conservative()
    memory.call("_backend4_remembered_set_retarget_slot", old, new, old + 40, new + 40)
    memory.call("_backend4_remembered_set_retarget_slot", old, old, old + 32, old + 40)
    memory.assert_conservative()
    old_payload, new_payload = memory.allocate(32), memory.allocate(64)
    memory.add(old, old_payload + 8)
    assert memory.call("pcc_gc_backend4_retarget_mutator_payload_locked",
                       old, old_payload, 32, new_payload, 64, 0, 0) == 2
    assert (old, new_payload + 8) in memory.members()
    assert (old, old_payload + 8) not in memory.members()
    memory.assert_conservative()
    memory.call("_backend4_remembered_set_remove", old)
    assert memory.members() == [(new, new + 24)]
    memory.call("_backend4_remembered_set_remove", new)
    assert memory.members() == []


@pytest.mark.parametrize("growth", [False, True], ids=["first-table", "rehash"])
def test_index_allocation_failure_stays_unknown_until_authoritative_clear(growth):
    memory = _Memory()
    if growth:
        for _ in range(128):
            memory.add(memory.owner())
    memory.fail_calloc = True
    owner = memory.owner()
    assert memory.add(owner) == 1  # Allocation of list node still succeeded.
    assert memory.read(PREFIX + "incomplete", 0) == 1
    memory.fail_calloc = False
    memory.add(memory.owner())
    assert memory.read(PREFIX + "incomplete", 0) == 1
    absent = memory.owner()
    memory.list_reads = 0
    memory.call("_backend4_remembered_set_remove", absent)
    assert memory.list_reads > 0
    memory.call("_backend4_remembered_set_remove", owner)
    assert all(found != owner for found, _ in memory.members())
    memory.call("_backend4_remembered_set_clear")
    assert memory.read(PREFIX + "incomplete", 0) == 0
    assert memory.maybe(owner) == 0
    memory.add(owner)
    assert memory.maybe(owner) == 1


def test_list_allocation_failure_never_publishes_or_indexes_owner():
    memory = _Memory()
    owner = memory.owner()
    memory.fail_malloc = True
    assert memory.add(owner) == 0
    assert memory.members() == [] and memory.maybe(owner) == 0


@pytest.mark.parametrize("operation", ["note", "forget", "clear", "require"])
@pytest.mark.parametrize("callback_site", ["calloc", "free"])
def test_index_allocator_reentry_is_conservative_and_taints_epoch(operation, callback_site):
    memory = _Memory()
    if callback_site == "free":
        for _ in range(128):
            memory.add(memory.owner())
    nested, outer = memory.owner(), memory.owner()
    def callback(_):
        assert memory.maybe(nested) == 1
        if operation == "note":
            memory.add(nested)
        elif operation == "clear":
            memory.call("_backend4_remembered_set_clear")
        elif operation == "require":
            memory.call("pcc_gc_remembered_member_require", nested)
        else:
            memory.call("pcc_gc_remembered_member_forget", nested)
    memory.callbacks[callback_site] = callback
    assert memory.add(outer) == 1
    assert memory.read(PREFIX + "busy", 0) == 0
    assert memory.read(PREFIX + "incomplete", 0) == 1
    memory.assert_conservative()
    assert memory.maybe(memory.owner()) == 1


def test_clear_publishes_empty_index_before_free_callback_builds_new_epoch():
    memory = _Memory()
    old, new = memory.owner(), memory.owner()
    memory.add(old)
    memory.callbacks["free"] = lambda _: memory.add(new)
    memory.call("_backend4_remembered_set_clear")
    assert memory.members() == [(new, new + 24)]
    assert memory.maybe(new) == 1 and memory.maybe(old) == 0
    assert memory.read(PREFIX + "incomplete", 0) == 0


def test_owner_remove_does_not_forget_same_owner_reinserted_by_free_callback():
    memory = _Memory()
    owner = memory.owner()
    memory.add(owner)
    memory.callbacks["free"] = lambda _: memory.add(owner, owner + 32)
    memory.call("_backend4_remembered_set_remove", owner)
    assert memory.members() == [(owner, owner + 32)]
    assert memory.maybe(owner) == 1
    memory.call("_backend4_remembered_set_remove", owner)
    assert memory.members() == [] and memory.maybe(owner) == 0


def test_retarget_prepares_index_before_holding_a_list_cursor():
    memory = _Memory()
    old = memory.owner()
    memory.add(old)
    for _ in range(127):
        memory.call("pcc_gc_remembered_member_note", memory.owner())
    new = memory.owner()
    memory.callbacks["calloc"] = lambda _: memory.call("_backend4_remembered_set_clear")
    memory.call("_backend4_remembered_set_retarget_slot", old, new, old + 24, new + 24)
    assert memory.members() == []  # No use of the cursor freed by callback.
    assert memory.read(PREFIX + "incomplete", 0) == 1


def test_malformed_index_metadata_falls_back_without_reading_table():
    memory = _Memory()
    owner = memory.owner()
    memory.write(PREFIX + "cap", 0, 16)
    assert memory.maybe(owner) == 1
    assert memory.read(PREFIX + "incomplete", 0) == 1


def test_remembered_membership_exports_have_exact_raw_abi_signatures():
    expected = {
        "pcc_gc_remembered_member_note": (("c_ptr",), "c_void"),
        "pcc_gc_remembered_member_maybe": (("c_ptr",), "c_int64"),
        "pcc_gc_remembered_member_require": (("c_ptr",), "c_void"),
        "pcc_gc_remembered_member_forget": (("c_ptr",), "c_void"),
        "pcc_gc_remembered_member_clear": ((), "c_void"),
    }
    signatures = {}
    for node in ast.parse(ABI.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_cross_object_signatures_part_"):
            signatures.update(ast.literal_eval(node.body[0].value))
    declarations = {}
    for node in ast.parse(BACKEND.read_text()).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "extern"):
            call = node.value
            declarations[call.args[0].value] = (tuple(arg.id for arg in call.args[1].elts), call.args[2].id)
    assert {name: signatures[name] for name in expected} == expected
    assert {name: declarations[name] for name in expected} == expected


def test_retarget_revalidates_target_after_real_page_free_callback():
    memory = _Memory()
    _load_functions(BACKEND, memory.namespace, {
        "_backend4_remembered_page_find", "_backend4_remembered_page_add",
        "_backend4_remembered_page_remove_slot",
        "_backend4_remembered_entry_attach", "_backend4_remembered_entry_detach",
    })
    old, new = memory.owner(), memory.owner()
    memory.add(old)
    def remove_destination(_):
        # The pre-noted target has no linked slot yet. Page retirement invokes
        # the ordinary owner removal, legitimately forgetting that stale key.
        memory.call("_backend4_remembered_set_remove", new)
        assert memory.maybe(new) == 0
    memory.callbacks["free"] = remove_destination
    memory.call("_backend4_remembered_set_retarget_slot", old, new, old + 24, new + 24)
    assert memory.members() == [(new, new + 24)]
    assert memory.maybe(new) == 1
    memory.call("_backend4_remembered_set_remove", new)
    assert memory.members() == []


def test_clear_preserves_incomplete_new_epoch_from_free_callback():
    memory = _Memory()
    old, new = memory.owner(), memory.owner()
    memory.add(old)
    def insert_with_failed_index(_):
        memory.fail_calloc = True
        memory.add(new)
        memory.fail_calloc = False
    memory.callbacks["free"] = insert_with_failed_index
    memory.call("_backend4_remembered_set_clear")
    assert memory.members() == [(new, new + 24)]
    assert memory.read(PREFIX + "incomplete", 0) == 1
    assert memory.maybe(new) == 1 and memory.maybe(old) == 1


@pytest.mark.parametrize("method", ["slot", "card"])
def test_last_slot_deletion_keeps_only_a_safe_stale_owner_key(method):
    memory = _Memory()
    owner = memory.owner()
    memory.add(owner)
    if method == "slot":
        memory.call("_backend4_remembered_set_remove_slot", owner + 24)
    else:
        memory.call("pcc_gc_backend4_zpage_clear_remembered_card", owner, owner + 24)
    assert memory.members() == [] and memory.maybe(owner) == 1
    memory.call("pcc_gc_note_object_freeing", owner)
    assert memory.maybe(owner) == 0
