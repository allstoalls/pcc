"""Execute the real set binder bodies against a moving, leased slot model.

This is a deterministic runtime-boundary oracle, not native-GC qualification.
Movement is injected before root registration/copy, during hash/equality, and
at lease release; input and output slots must remain authoritative throughout.
"""
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi


PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_set.py"


class Block:
    def __init__(self):
        self.fields = {}


class Ptr:
    def __init__(self, block, offset=0):
        self.block, self.offset = block, offset


class Object(Block):
    def __init__(self, value, tag):
        super().__init__()
        self.value, self.tag = value, tag
        self.alive, self.leases, self.refs = True, 0, 1
        self.fields[abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] = tag


class Memory:
    def __init__(self, phase="register", fail_copy=0, fail_release=False,
                 fail_register=0, fail_allocation=0):
        self.phase, self.fail_copy, self.fail_release = phase, fail_copy, fail_release
        self.depth, self.moves, self.copies, self.collections = 0, 0, 0, 0
        self.roots, self.objects, self.error = {}, [], None
        self.on_equal = None
        self.fail_register, self.fail_allocation = fail_register, fail_allocation
        self.registrations, self.allocations = 0, 0
        self.dummy = object()
        self.none = self.make(None, 0)
        self.none.leases = 1000  # immortal static singleton
        tree = ast.parse(PORT.read_text())
        names = {"_ptr_is_set", "_perturb_shift5", "_rehash_find_empty_slot",
                 "_entry_key", "_set_read_reload_root", "_set_remove_rooted_slot",
                 "_set_add_rooted_slot"}
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and (node.name.startswith("_set_call_") or node.name in names
                     or node.name == "py_set_call_method_slots")]
        for node in body:
            node.decorator_list = []
        ns = {name: getattr(abi, name) for name in (
            "PYOBJECTHEADER_TYPE_TAG_OFFSET", "PYTUPLEOBJECT_ITEMS_OFFSET",
            "PYTUPLEOBJECT_LEN_OFFSET", "PY_TYPE_SET")}
        ns.update({
            "stack_alloc": lambda _n: Block(), "malloc": lambda _n: Block(),
            "memset": self.memset, "free": lambda _: None,
            "ptr_add": self.add, "load_ptr": self.read, "load_i64": self.read,
            "load_i32": self.read, "store_ptr": self.write,
            "store_i64": self.write, "store_i32": self.write,
            "null": lambda: None, "ptr_is_null": lambda p: int(p is None),
            "ptr_eq": lambda a, b: int(a == b if isinstance(a, int) and isinstance(b, int) else a is b),
            "is_tagged_int": lambda p: int(isinstance(p, int)),
            "cstr": lambda s: s,
            "global_load_ptr": lambda name: self.none if name == "py_None" else self.dummy,
            "pcc_gc_load_ptr": lambda _owner, p: self.read(p, 0),
            "pcc_gc_resolve_root_slot_unlocked": lambda p, offset: self.read(p, offset),
            "pcc_gc_scheduler_root_register_handle": self.register,
            "pcc_gc_scheduler_root_unregister_handle": self.unregister,
            "pcc_gc_root_copy_lease": self.copy, "pcc_gc_root_move": self.move_root,
            "pcc_gc_root_copy_lease_prepare_locked": self.copy_prepare,
            "pcc_gc_root_copy_lease_finish": self.copy_finish,
            "pcc_gc_foreign_lease_acquire": self.acquire,
            "pcc_gc_foreign_lease_release": self.release,
            "pcc_gc_store_root": self.store_root,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "py_err_occurred": lambda: int(self.error is not None),
            "py_exc_new": lambda kind, message: (kind, message),
            "py_raise_owned": self.raise_error,
            "py_current_exception": lambda: self.error,
            "py_exc_builtin_class": lambda kind: kind,
            "py_exc_matches": lambda error, kind: int(error[0] == kind),
            "py_clear_exception": self.clear_error,
            "py_dict_len": lambda obj: len(obj.value),
            "py_set_new": self.new_set, "py_obj_iter": self.iterate,
            "py_obj_next": self.next, "py_obj_hash": self.hash,
            "py_obj_eq": self.equal, "pcc_gc_backend": lambda: 4,
            "_alloc_entries": lambda _capacity: Block(),
            "_maybe_grow": lambda owner: None,
            "_rehash": self.rehash,
            "pcc_gc_store_ptr_plan_init": self.plan_init,
            "pcc_gc_store_ptr_plan_commit_sentinel_aware_locked": self.plan_commit,
            "pcc_gc_store_ptr_plan_finish": self.plan_finish,
        })
        exec(compile(ast.Module(body=body, type_ignores=[]), str(PORT), "exec"), ns)
        self.ns = ns

    def make(self, value, tag):
        obj = Object(value, tag)
        self.objects.append(obj)
        return obj

    def wrap(self, value):
        if isinstance(value, int):
            return value
        if isinstance(value, set):
            out = self.new_set()
            for item in value:
                key = self.wrap(item)
                h = hash(item)
                slot = self.ns["_rehash_find_empty_slot"](out.fields[40], out.fields[24], h)
                self.write(out.fields[40], slot * 16, h)
                self.write(out.fields[40], slot * 16 + 8, key)
                out.fields[16] += 1
                out.fields[32] += 1
            return out
        tag = abi.PY_TYPE_TUPLE if isinstance(value, tuple) else abi.PY_TYPE_DICT if isinstance(value, dict) else abi.PY_TYPE_LIST
        obj = self.make(value, tag)
        if isinstance(value, tuple):
            obj.fields[abi.PYTUPLEOBJECT_LEN_OFFSET] = len(value)
            for index, child in enumerate(value):
                obj.fields[abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8] = self.wrap(child)
        return obj

    @staticmethod
    def add(ptr, offset):
        return Ptr(ptr.block, ptr.offset + offset) if isinstance(ptr, Ptr) else Ptr(ptr, offset)

    @staticmethod
    def location(ptr, offset):
        return (ptr.block, ptr.offset + offset) if isinstance(ptr, Ptr) else (ptr, offset)

    def read(self, ptr, offset):
        block, offset = self.location(ptr, offset)
        assert not isinstance(block, Object) or block.alive, "read from stale object"
        return block.fields.get(offset, None)

    def write(self, ptr, offset, value):
        block, offset = self.location(ptr, offset)
        assert not isinstance(block, Object) or block.alive, "write to stale object"
        block.fields[offset] = value

    def memset(self, block, _byte, size):
        for offset in range(0, size, 8):
            self.write(block, offset, None)

    def collect(self, phase):
        if self.depth or phase != self.phase:
            return
        self.collections += 1
        mapping = {}
        for old in list(self.objects):
            if old.alive and not old.leases:
                new = self.make(old.value, old.tag)
                new.fields, new.refs = old.fields.copy(), old.refs
                old.alive, old.refs = False, 0
                mapping[old] = new
                self.moves += 1
        for slot in self.roots.values():
            value = self.read(slot, 0)
            if value in mapping if isinstance(value, Object) else False:
                self.write(slot, 0, mapping[value])
        for obj in self.objects:
            if obj.alive:
                for offset, value in list(obj.fields.items()):
                    if isinstance(value, Object) and value in mapping:
                        obj.fields[offset] = mapping[value]
                    elif isinstance(value, Block) and not isinstance(value, Object):
                        for child_offset, child in list(value.fields.items()):
                            if isinstance(child, Object) and child in mapping:
                                value.fields[child_offset] = mapping[child]

    def register(self, slot):
        self.collect("register")
        assert self.read(slot, 0) is None, "new roots must be empty before registration"
        self.registrations += 1
        if self.registrations == self.fail_register:
            return None
        handle = object()
        self.roots[handle] = slot
        return handle

    def unregister(self, handle):
        self.collect("unregister")
        assert self.read(self.roots[handle], 0) is None
        del self.roots[handle]

    def lock(self):
        self.collect("lock")
        self.depth += 1

    def unlock(self):
        self.depth -= 1
        assert self.depth >= 0

    def copy(self, destination, source):
        assert self.depth == 0, "copy finish must not run in an outer graph transaction"
        return self.copy_impl(destination, source)

    def copy_prepare(self, destination, source, borrowed, plan):
        assert self.depth > 0 and borrowed == 0
        return self.copy_impl(destination, source)

    def copy_finish(self, plan):
        assert self.depth == 0, "prepared copy finish must run after outermost unlock"

    def copy_impl(self, destination, source):
        self.collect("copy")
        self.copies += 1
        if self.copies == self.fail_copy:
            return -2
        assert self.read(destination, 0) is None
        value = self.read(source, 0)
        if isinstance(value, Object):
            assert value.alive
            value.refs += 1
        self.write(destination, 0, value)
        return self.acquire(destination)

    def acquire(self, slot):
        self.collect("acquire")
        value = self.read(slot, 0)
        if not isinstance(value, Object):
            return 0
        assert value.alive and value.refs > 0
        value.leases += 1
        return 1

    def release(self, slot, token):
        value = self.read(slot, 0)
        if token:
            assert value.alive and value.leases > 0
            value.leases -= 1
        self.collect("release")
        return -3 if self.fail_release else 0

    def move_root(self, destination, source):
        self.collect("move")
        assert self.read(destination, 0) is None
        self.write(destination, 0, self.read(source, 0))
        self.write(source, 0, None)
        return 0

    def store_root(self, slot, value):
        self.collect("drop")
        old = self.read(slot, 0)
        self.write(slot, 0, value)
        if isinstance(old, Object):
            assert old.refs > 0
            old.refs -= 1
        if isinstance(value, Object):
            value.refs += 1

    def raise_error(self, error):
        self.error = error

    def clear_error(self):
        self.error = None

    def new_set(self):
        self.allocations += 1
        if self.allocations == self.fail_allocation:
            return None
        obj = self.make(None, abi.PY_TYPE_SET)
        obj.fields.update({16: 0, 24: 32, 32: 0, 40: Block()})
        return obj

    def iterate(self, obj):
        assert obj.alive and obj.leases > 0
        try:
            return self.make(iter(obj.value), abi.PY_TYPE_ITER)
        except Exception as error:
            self.error = (3, str(error))
            return None

    def next(self, obj):
        assert obj.alive and obj.leases > 0
        self.collect("callback")
        try:
            return self.wrap(next(obj.value))
        except StopIteration:
            self.error = (8, "StopIteration")
        except Exception as error:
            self.error = (7, str(error))
        return None

    def hash(self, obj):
        self.collect("callback")
        if isinstance(obj, Object):
            assert obj.alive and obj.leases > 0
            obj = obj.value
        try:
            return hash(obj)
        except Exception as error:
            self.error = (3, str(error))
            return -1

    def equal(self, lhs, rhs):
        self.collect("callback")
        for obj in (lhs, rhs):
            assert not isinstance(obj, Object) or obj.alive and obj.leases > 0
        result = int((lhs.value if isinstance(lhs, Object) else lhs) == (rhs.value if isinstance(rhs, Object) else rhs))
        if self.on_equal is not None:
            callback, self.on_equal = self.on_equal, None
            callback()
        return result

    def add_key(self, set_slot, _sh, item_slot, _ih, entries, capacity, index, hash_value):
        target, item = self.read(set_slot, 0), self.read(item_slot, 0)
        assert target.alive and target.leases > 0
        assert target.fields[40] is entries and target.fields[24] == capacity
        old = self.read(entries, index * 16 + 8)
        assert old is None or old is self.dummy
        self.write(entries, index * 16, hash_value)
        self.write(entries, index * 16 + 8, item)
        if isinstance(item, Object):
            item.refs += 1
        target.fields[16] += 1
        target.fields[32] += int(old is None)
        return 1

    def remove_key(self, set_slot, _sh, entries, capacity, index):
        target = self.read(set_slot, 0)
        assert target.alive and target.leases > 0
        assert target.fields[40] is entries and target.fields[24] == capacity
        old = self.read(entries, index * 16 + 8)
        if old is None or old is self.dummy:
            return 0
        self.write(entries, index * 16 + 8, self.dummy)
        if isinstance(old, Object):
            old.refs -= 1
        target.fields[16] -= 1
        return 1

    def install_owning_forwarding(self, old, new, backend=3):
        """Run the production resolver/copy-prepare with explicit old owners.

        A borrowed load intentionally rewrites without a reference transfer.
        Calling it first therefore makes the old binder fail the count oracle.
        """
        forwarding, selected = Block(), Block()
        forwarding.fields[8], selected.fields[0] = new, backend
        old.fields[12], new.fields[12] = 2048, 0
        old.refs, new.refs = (1, 0) if backend == 3 else (0, 1)
        transfers = []

        def borrowed_load(_owner, slot):
            value = self.read(slot, 0)
            if value is old:
                self.write(slot, 0, new)
                return new
            return value

        def change_ref(value, delta):
            transfers.append((value, delta))
            value.refs += delta
            assert value.refs >= 0

        def acquire_resolving(slot):
            self.ns["pcc_gc_resolve_root_slot_unlocked"](slot, 0)
            return self.acquire(slot)

        self.ns.update({
            "i64": int,
            "pcc_gc_object_is_known_no_lock": lambda value: 0,
            "pcc_gc_forwarding_index_find": lambda value: forwarding if value is old else None,
            "global_addr": lambda name: selected,
            "py_incref": lambda value: change_ref(value, 1),
            "py_decref": lambda value: change_ref(value, -1),
            "pcc_gc_load_ptr": borrowed_load,
            "_gc_backend_fast": lambda: backend,
            "pcc_gc_store_root_plan_init": lambda plan, value: self.plan_init(plan, None, value),
            "pcc_gc_store_root_plan_commit_locked": lambda plan, slot, value: self.plan_commit(plan, None, slot, value, None),
            "pcc_gc_store_root_plan_finish": self.plan_finish,
            "pcc_gc_foreign_lease_acquire": acquire_resolving,
        })
        for name, functions in (
            ("freestanding_gc_root_operations.py", {"pcc_gc_resolve_root_slot_unlocked"}),
            ("py_obj.py", {"pcc_gc_root_copy_lease_prepare_locked", "pcc_gc_root_copy_lease_finish"}),
        ):
            path = PORT.with_name(name)
            tree = ast.parse(path.read_text())
            body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
            for node in body:
                node.decorator_list = []
            exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), self.ns)
        return transfers

    def rehash(self, obj, needed):
        raise AssertionError("fixture should not require capacity growth")

    def plan_init(self, plan, owner, _backend):
        self.write(plan, 0, None)

    def plan_commit(self, plan, owner, slot, value, dummy):
        assert self.depth > 0
        old = self.read(slot, 0)
        self.write(plan, 0, None if old is dummy else old)
        self.write(slot, 0, value)
        if isinstance(value, Object):
            value.refs += 1
        return 1

    def plan_finish(self, plan):
        assert self.depth == 0, "removal finalizers must run after outermost unlock"
        old = self.read(plan, 0)
        if isinstance(old, Object):
            assert old.alive
            old.refs -= 1
        self.write(plan, 0, None)

    def values(self, value):
        assert value.alive
        return {key.value if isinstance(key, Object) else key
                for offset, key in value.fields[40].fields.items()
                if offset % 16 == 8 and key is not None and key is not self.dummy}

    def call(self, method, values, arguments, kwargs=None):
        caller = Block()
        self.caller = caller
        inputs = (self.wrap(values), self.wrap(tuple(arguments)), self.none if kwargs is None else self.wrap(kwargs), None)
        for index, value in enumerate(inputs):
            slot = self.add(caller, index * 8)
            handle = object()
            self.roots[handle] = slot
            self.write(slot, 0, value)
        status = self.ns["py_set_call_method_slots"](caller, method, self.add(caller, 8), self.add(caller, 16), self.add(caller, 24))
        assert len(self.roots) == 4 and self.depth == 0
        assert all(obj.leases == (1000 if obj is self.none else 0) for obj in self.objects if obj.alive)
        output = self.read(caller, 24)
        return status, self.values(self.read(caller, 0)), (None if output is None or output is self.none else self.values(output))


@pytest.mark.parametrize("method,args,expected", [
    (0, ([2, 3], [4]), {1, 2, 3, 4}),
    (1, ([2, 3], [2, 4]), {2}),
    (2, ([1], [8]), {2}),
    (3, ([2, 3], [4]), {1, 2, 3, 4}),
    (4, ([2, 3], [2, 4]), {2}),
    (5, ([1], [8]), {2}),
    (6, ([2, 2, 3, 3],), {1, 3}),
    (7, ([2, 2, 3, 3],), {1, 3}),
])
@pytest.mark.parametrize("phase", ("register", "copy", "acquire", "callback", "release", "move", "drop", "unregister"))
def test_set_call_slot_movement(method, args, expected, phase):
    memory = Memory(phase)
    status, receiver, result = memory.call(method, {1, 2}, args)
    assert status == 0 and memory.error is None
    mutating = method in (3, 4, 5, 7)
    assert receiver == (expected if mutating else {1, 2})
    assert result == (None if mutating else expected)
    if phase == "move" and method in (3, 5, 7):
        # These in-place methods publish immortal None directly, with no
        # owner-transfer operation at which a relocation can be injected.
        assert memory.collections == 0
        return
    assert memory.collections > 0
    # A callback can find every live object leased (symmetric difference
    # during its deduplication pass); attempting GC then correctly moves none.
    if phase != "callback":
        assert memory.moves > 0


@pytest.mark.parametrize("failure", (1, 2, 3, 4, 5))
def test_set_call_partial_acquisition_cleanup(failure):
    memory = Memory("copy", fail_copy=failure)
    status, receiver, result = memory.call(0, {1, 2}, ([3], [4]))
    assert status == -1 and memory.error[0] == 15
    assert receiver == {1, 2} and result is None


@pytest.mark.parametrize("method", range(8))
def test_set_call_keyword_rejection_before_mutation(method):
    memory = Memory("register")
    status, receiver, result = memory.call(method, {1, 2}, ([3],), {"bad": 1})
    assert status == -1 and memory.error[0] == 3
    assert receiver == {1, 2} and result is None


def test_set_call_partial_iterator_failure():
    def values():
        yield 3
        raise ValueError("iterator failed")
    for method, expected in ((3, {1, 2, 3}), (4, {1, 2}), (5, {1, 2})):
        memory = Memory("release")
        status, receiver, result = memory.call(method, {1, 2}, (values(),))
        assert status == -1 and memory.error == (7, "iterator failed")
        assert receiver == expected and result is None


def test_set_call_cleanup_preserves_original_exception():
    memory = Memory("release", fail_release=True)
    status, receiver, result = memory.call(0, {1, 2}, (), {"bad": 1})
    assert status == -1 and memory.error[0] == 3
    assert receiver == {1, 2} and result is None


@pytest.mark.parametrize("registration", (1, 5, 10))
def test_set_call_partial_root_registration_failure(registration):
    memory = Memory("register", fail_register=registration)
    status, receiver, result = memory.call(0, {1, 2}, ([3],))
    assert status == -1 and memory.error[0] == 19
    assert receiver == {1, 2} and result is None


def test_set_call_result_allocation_failure_preserves_receiver():
    # First allocation constructs the fixture receiver; second is the real
    # binder's new result, after its authoritative input copies are acquired.
    memory = Memory("release", fail_allocation=2)
    status, receiver, result = memory.call(0, {1, 2}, ([3],))
    assert status == -1 and memory.error[0] == 19
    assert receiver == {1, 2} and result is None


class Collision:
    def __init__(self, value):
        self.value = value

    def __hash__(self):
        return 1

    def __eq__(self, other):
        return isinstance(other, Collision) and self.value == other.value


@pytest.mark.parametrize("phase", ("register", "copy", "callback", "release", "drop"))
def test_set_call_equality_candidate_is_leased_across_rehash(phase):
    memory = Memory(phase)
    def rehash_during_equality():
        receiver = memory.read(memory.caller, 0)
        entries = Block()
        entries.fields = receiver.fields[40].fields.copy()
        receiver.fields[40] = entries
    memory.on_equal = rehash_during_equality
    status, receiver, result = memory.call(3, {Collision(1)}, ([Collision(2)],))
    assert status == 0 and result is None
    assert {key.value for key in receiver} == {1, 2}
    assert memory.on_equal is None


@pytest.mark.parametrize("kind", ("identity", "equal"))
def test_set_call_removal_revalidates_candidate_at_commit(kind):
    memory = Memory("none")
    remove = memory.ns["_set_remove_rooted_slot"]
    interleaved = []
    original = 1 if kind == "identity" else Collision(1)
    replacement = 33 if kind == "identity" else Collision(2)

    def remove_after_competing_mutation(*args):
        if not interleaved:
            assert memory.depth == 0
            owner = memory.read(args[0], 0)
            entries, position = args[2], args[4]
            old = memory.read(entries, position * 16 + 8)
            new = memory.wrap(replacement)
            # Another mutator removes the matched entry and reuses its
            # tombstone without changing the table address or capacity.
            memory.write(entries, position * 16, hash(replacement))
            memory.write(entries, position * 16 + 8, new)
            if isinstance(old, Object):
                old.refs -= 1
            assert owner.fields[16] == 1
            interleaved.append(new)
        return remove(*args)

    memory.ns["_set_remove_rooted_slot"] = remove_after_competing_mutation
    status, receiver, result = memory.call(5, {original}, ([original],))
    assert interleaved and status == 0 and result is None
    assert receiver == {replacement}


@pytest.mark.parametrize("path", ("source", "candidate", "replacement"))
@pytest.mark.parametrize("backend", (3, 4))
def test_set_call_forwarded_key_preserves_owning_references(path, backend):
    memory = Memory("none")
    key = Collision(1)
    old = memory.wrap(key)
    new = memory.wrap(key)
    transfers = memory.install_owning_forwarding(old, new, backend)
    slots, tokens = Block(), Block()
    for index in range(10):
        memory.write(slots, index * 8, None)
        memory.write(tokens, index * 8, -1)
        memory.roots[object()] = memory.add(slots, index * 8)
    target, source = memory.new_set(), memory.new_set()
    memory.write(slots, 0, target)
    memory.write(slots, 8, source)
    target.leases, source.leases = 1, 1
    owner = target if path == "candidate" else source
    memory.write(owner.fields[40], 16, 1)
    memory.write(owner.fields[40], 24, old)
    owner.fields[16], owner.fields[32] = 1, 1

    if path == "candidate":
        item = memory.wrap(Collision(1))
        item.leases = 1
        memory.write(slots, 56, item)
        assert memory.ns["_set_call_lookup"](slots, tokens, 0, 1, 0) == 1
        expected_refs = 1
    elif path == "source":
        assert memory.ns["_set_call_apply"](slots, tokens, 0, 1, 0) == 1
        expected_refs = 2  # Original source owner and the newly inserted key.
    else:
        assert memory.ns["_set_call_replace"](slots, 0, 1) == 1
        expected_refs = 2
    assert memory.depth == 0 and memory.error is None
    assert memory.read(owner.fields[40], 24) is new
    assert old.refs == 0 and new.refs == expected_refs
    assert transfers == ([(new, 1), (old, -1)] if backend == 3 else [])


def test_set_call_insert_commits_before_another_mutator_reuses_earlier_tombstone():
    memory = Memory("none")
    slots, tokens = Block(), Block()
    for index in range(10):
        memory.write(slots, index * 8, None)
        memory.write(tokens, index * 8, -1)
        memory.roots[object()] = memory.add(slots, index * 8)
    target = memory.wrap({Collision(1)})
    item = memory.wrap(Collision(2))
    target.leases, item.leases = 1, 1
    memory.write(slots, 0, target)
    memory.write(slots, 56, item)
    unlocks, interleaved = [], []

    def competing_mutator_after_unlock():
        memory.unlock()
        if memory.depth != 0:
            return
        unlocks.append(True)
        # Candidate retention and post-equality validation used the first
        # two unlocks. The third follows the empty insertion-slot selection.
        if len(unlocks) == 3:
            entries = target.fields[40]
            old = memory.read(entries, 24)
            assert old.value.value == 1
            memory.write(entries, 24, memory.dummy)
            old.refs -= 1
            target.fields[16] -= 1
            # Concurrent remove(1); add(2) chooses the earlier tombstone if
            # the first mutator has not committed its own insertion yet.
            if not any(key == Collision(2) for key in memory.values(target)):
                memory.write(entries, 24, item)
                item.refs += 1
                target.fields[16] += 1
            interleaved.append(True)

    memory.ns["pcc_py_gc_minor_graph_unlock"] = competing_mutator_after_unlock
    memory.ns["_set_call_lookup"](slots, tokens, 0, 1, 2)
    assert interleaved and memory.error is None and memory.depth == 0
    assert target.fields[16] == 1
    assert {key.value for key in memory.values(target)} == {2}


def test_set_call_insert_commit_failure_preserves_receiver():
    memory = Memory("none")
    memory.ns["pcc_gc_store_ptr_plan_commit_sentinel_aware_locked"] = lambda *args: 0
    status, receiver, result = memory.call(3, {1}, ([2],))
    assert status == -1 and memory.error == (7, "cannot commit set call insertion")
    assert receiver == {1} and result is None
