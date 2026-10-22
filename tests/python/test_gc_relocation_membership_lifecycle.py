"""Host models of production relocation-list membership mutation ordering.

The actual removal/reset functions are AST-extracted and run against raw-memory
and provider doubles. These tests do not compile a runtime or establish native
GC, allocator, hash-table, or concurrency behavior. Native index fixtures cover
the real table's retained capacity and incomplete-state recovery separately.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "pcc/runtime/py/py_gc_backend.py"
INDEX = ROOT / "pcc/runtime/py/freestanding_gc_index_table.py"
COPY = ROOT / "pcc/runtime/py/freestanding_gc_relocation_copy.py"
ABI = ROOT / "pcc/frontends/python/codegen/runtime_abi.py"

RELOCATION_HEAD = "pcc_gc_relocation_set_head"
RELOCATION_CURSOR = "pcc_gc_backend4_reseed_relocation_cursor"
RELOCATION_REVISION = "pcc_gc_backend4_reseed_relocation_revision"
RESET_OWNER = "pcc_gc_backend4_relocation_reset_owner"


def _load_functions(path, namespace, names):
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


class _MembershipMemory:
    """Audit every list unlink and the provider event that accounts for it.

    ``counts`` is the logical oracle even during an incomplete index epoch.
    It deliberately does not represent the unknown physical hash-table values.
    ``maybe`` is conservative in that epoch, while the strict forget provider
    verifies that callers still report each committed unlink exactly once.
    """

    def __init__(self, *, incomplete=False):
        self.cells = {}
        self.next_pointer = 1024
        self.relocation_nodes = set()
        self.freed = set()
        self.counts = Counter()
        self.incomplete = incomplete
        self.locked = False
        self.pending_unlink = None
        self.forwarded = set()
        self.forgets = []
        self.clears = []
        self.unlocks = []
        self.finish_batches = []
        self.polls = 0
        self.namespace = {
            "null": lambda: 0,
            "ptr_is_null": lambda value: int(value == 0),
            "ptr_eq": lambda left, right: int(left == right),
            "is_tagged_int": lambda value: int(bool(value & 1)),
            "global_addr": lambda name: name,
            "global_load_ptr": self.global_get,
            "global_store_ptr": self.global_set,
            "load_ptr": self.read,
            "load_i32": self.read,
            "load_i64": self.read,
            "store_ptr": self.write,
            "store_i32": self.write,
            "store_i64": self.write,
            "free": self.free,
            "_init_config": lambda: 4,
            "_object_head": lambda: 0,
            "_forwarding_find": self.forwarding_find,
            "pcc_current_thread_id": lambda: 7,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "pcc_thread_safepoint": self.poll,
            "pcc_gc_relocation_member_maybe": self.member_maybe,
            "pcc_gc_relocation_member_forget": self.member_forget,
            "pcc_gc_relocation_member_clear": self.member_clear,
        }
        _load_functions(
            BACKEND,
            self.namespace,
            {
                "_object_graph_lock",
                "_object_graph_unlock",
                "_relocation_set_head",
                "_set_relocation_set_head",
                "_evacuation_page_head",
                "_set_evacuation_page_head",
                "_relocation_set_remove",
                "pcc_gc_reset_relocation_set",
                "_relocation_reset_finish",
                "_backend4_evacuation_page_finish_detached",
            },
        )
        reset_finish = self.namespace["_relocation_reset_finish"]

        def finish_batch(head, pages):
            assert not self.locked
            assert self.pending_unlink is None
            self.finish_batches.append(tuple(self.chain(head)))
            reset_finish(head, pages)

        self.namespace["_relocation_reset_finish"] = finish_batch

    def new_object(self, flags=2048 | 128):
        self.next_pointer += 32
        obj = self.next_pointer
        self.cells[obj] = {12: flags}
        return obj

    def seed(self, objects):
        assert not self.locked and not self.linked_nodes()
        nodes = []
        for obj in objects:
            self.next_pointer += 32
            node = self.next_pointer
            self.cells[node] = {0: obj, 8: 0}
            self.relocation_nodes.add(node)
            nodes.append(node)
        for node, nxt in zip(nodes, nodes[1:]):
            self.cells[node][8] = nxt
        self.cells[RELOCATION_HEAD] = {0: nodes[0] if nodes else 0}
        self.counts = Counter(objects)
        self.assert_accounted()
        return nodes

    def chain(self, head):
        nodes = []
        while head:
            assert head not in nodes, "cycle in modeled linked list"
            assert head in self.cells and head not in self.freed
            nodes.append(head)
            head = self.cells[head].get(8, 0)
        return nodes

    def linked_nodes(self):
        return self.chain(self.cells.get(RELOCATION_HEAD, {}).get(0, 0))

    def linked_counts(self):
        return Counter(self.cells[node][0] for node in self.linked_nodes())

    def assert_accounted(self):
        assert self.pending_unlink is None, "unlink lacks membership decrement"
        assert self.counts == self.linked_counts()

    def read(self, base, offset):
        if isinstance(base, str):
            return self.cells.get(base, {}).get(offset, 0)
        assert base in self.cells and base not in self.freed
        return self.cells[base].get(offset, 0)

    def write(self, base, offset, value):
        changes_link = (
            (base == RELOCATION_HEAD and offset == 0)
            or (base in self.relocation_nodes and offset == 8)
        )
        before = self.linked_nodes() if changes_link else None
        if not isinstance(base, str):
            assert base in self.cells and base not in self.freed
        self.cells.setdefault(base, {})[offset] = value
        if changes_link:
            after = self.linked_nodes()
            if before != after:
                assert self.locked, "visible unlink escaped the graph lock"
                assert self.pending_unlink is None, "previous unlink unaccounted"
                removed = [node for node in before if node not in after]
                assert len(removed) == 1
                assert after == [node for node in before if node != removed[0]]
                self.pending_unlink = removed[0]

    def global_get(self, name):
        return self.read(name, 0)

    def global_set(self, name, value):
        self.write(name, 0, value)

    def lock(self):
        assert not self.locked
        self.assert_accounted()
        self.locked = True

    def unlock(self):
        assert self.locked
        self.assert_accounted()
        self.unlocks.append((len(self.linked_nodes()), self.incomplete))
        self.locked = False

    def poll(self):
        assert not self.locked
        self.assert_accounted()
        self.polls += 1

    def forwarding_find(self, obj):
        assert self.locked
        return 1 if obj in self.forwarded else 0

    def member_maybe(self, obj):
        assert self.locked
        return int(self.incomplete or self.counts[obj] > 0)

    def member_forget(self, obj):
        assert self.locked
        assert self.pending_unlink is not None, "forget without committed unlink"
        node = self.pending_unlink
        assert self.cells[node][0] == obj
        expected_before = self.linked_counts()
        expected_before[obj] += 1
        assert self.counts == expected_before, "forgot before unlink or twice"
        assert self.counts[obj] > 0
        self.counts[obj] -= 1
        if self.counts[obj] == 0:
            del self.counts[obj]
        self.pending_unlink = None
        self.assert_accounted()
        self.forgets.append((node, obj, len(self.linked_nodes()), self.incomplete))

    def member_clear(self):
        assert self.locked
        self.assert_accounted()
        assert self.global_get(RELOCATION_HEAD) == 0, "clear on nonempty list"
        assert not self.counts
        self.clears.append((len(self.forgets), self.incomplete))
        self.counts.clear()
        self.incomplete = False

    def free(self, node):
        self.assert_accounted()
        assert node in self.cells and node not in self.freed
        assert node not in self.linked_nodes(), "freed a linked relocation node"
        self.freed.add(node)

    def remove(self, obj):
        self.lock()
        self.namespace["_relocation_set_remove"](obj)
        self.unlock()


def test_remove_keeps_duplicate_count_after_candidate_hint_is_cleared():
    memory = _MembershipMemory()
    obj = memory.new_object(flags=128)
    nodes = memory.seed([obj, obj])

    memory.remove(obj)
    assert memory.counts == Counter({obj: 1})
    assert memory.linked_nodes() == nodes[1:]
    assert memory.freed == {nodes[0]}
    assert memory.read(obj, 12) == 128
    assert len(memory.forgets) == 1 and memory.clears == []

    memory.remove(obj)
    assert not memory.counts and not memory.linked_nodes()
    assert memory.freed == set(nodes)
    assert len(memory.forgets) == 2 and memory.clears == []


@pytest.mark.parametrize("position", [0, 1, 2], ids=["head", "middle", "tail"])
def test_remove_repairs_matching_cursor_and_revision_for_each_list_position(position):
    memory = _MembershipMemory()
    objects = [memory.new_object() for _ in range(3)]
    nodes = memory.seed(objects)
    memory.global_set(RELOCATION_CURSOR, nodes[position])
    memory.global_set(RELOCATION_REVISION, 41)
    expected_cursor = nodes[position + 1] if position < 2 else 0

    memory.remove(objects[position])

    assert memory.linked_nodes() == nodes[:position] + nodes[position + 1:]
    assert memory.counts == Counter(objects[:position] + objects[position + 1:])
    assert memory.global_get(RELOCATION_CURSOR) == expected_cursor
    assert memory.global_get(RELOCATION_REVISION) == 42
    assert memory.freed == {nodes[position]}
    assert memory.forgets == [(nodes[position], objects[position], 2, False)]
    assert memory.read(objects[position], 12) == 128
    assert memory.clears == []


@pytest.mark.parametrize("incomplete", [False, True], ids=["exact", "incomplete"])
def test_absent_remove_never_decrements_or_mutates_the_list(incomplete):
    memory = _MembershipMemory(incomplete=incomplete)
    obj = memory.new_object()
    absent = memory.new_object()
    nodes = memory.seed([obj, obj])
    memory.global_set(RELOCATION_CURSOR, nodes[1])
    memory.global_set(RELOCATION_REVISION, 19)

    for candidate in (absent, 0, 1):
        memory.remove(candidate)

    assert memory.linked_nodes() == nodes
    assert memory.counts == Counter({obj: 2})
    assert memory.global_get(RELOCATION_CURSOR) == nodes[1]
    assert memory.global_get(RELOCATION_REVISION) == 19
    assert memory.forgets == [] and memory.clears == [] and not memory.freed
    assert memory.incomplete is incomplete


def test_empty_remove_and_empty_reset_can_recover_incomplete_membership():
    memory = _MembershipMemory(incomplete=True)
    absent = memory.new_object()
    memory.remove(absent)
    assert memory.clears == [(0, True)]
    assert not memory.incomplete and memory.forgets == []

    memory.incomplete = True
    memory.namespace["pcc_gc_reset_relocation_set"]()
    assert memory.clears == [(0, True), (0, True)]
    assert not memory.incomplete and memory.forgets == []
    assert memory.global_get(RESET_OWNER) == 0
    assert not memory.locked


@pytest.mark.parametrize("incomplete", [False, True], ids=["exact", "incomplete"])
def test_reset_accounts_for_every_duplicate_pop_before_batched_unlock(incomplete):
    memory = _MembershipMemory(incomplete=incomplete)
    objects = [memory.new_object() for _ in range(3)]
    selected = objects * 11 + objects[:2]
    nodes = memory.seed(selected)
    memory.forwarded.add(objects[1])
    memory.global_set(RELOCATION_CURSOR, nodes[18])
    memory.global_set(RELOCATION_REVISION, 100)

    memory.namespace["pcc_gc_reset_relocation_set"]()

    assert [event[0] for event in memory.forgets] == nodes
    assert [event[1] for event in memory.forgets] == selected
    assert [event[2] for event in memory.forgets] == list(range(34, -1, -1))
    assert all(event[3] is incomplete for event in memory.forgets)
    assert [len(batch) for batch in memory.finish_batches] == [16, 16, 3]
    assert memory.finish_batches == [
        tuple(reversed(nodes[:16])),
        tuple(reversed(nodes[16:32])),
        tuple(reversed(nodes[32:])),
    ]
    assert [(left, bad) for left, bad in memory.unlocks if left] == [
        (19, incomplete), (3, incomplete),
    ]
    assert memory.clears == [(35, incomplete)]
    assert memory.polls == 2
    assert memory.global_get(RELOCATION_CURSOR) == 0
    assert memory.global_get(RELOCATION_REVISION) == 135
    assert memory.global_get(RESET_OWNER) == 0
    assert not memory.incomplete and not memory.locked
    assert not memory.counts and not memory.linked_nodes()
    assert memory.freed == set(nodes)
    assert memory.read(objects[0], 12) == 128
    assert memory.read(objects[1], 12) == (2048 | 128)
    assert memory.read(objects[2], 12) == 128


def test_membership_count_and_forget_have_matching_raw_abi_declarations():
    expected = {
        "pcc_gc_relocation_member_count": (("c_ptr",), "c_int64"),
        "pcc_gc_relocation_member_forget": (("c_ptr",), "c_void"),
    }
    abi_tree = ast.parse(ABI.read_text(encoding="utf-8"))
    signatures = {}
    for node in abi_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith(
            "_cross_object_signatures_part_"
        ):
            signatures.update(ast.literal_eval(node.body[0].value))
    copy_tree = ast.parse(COPY.read_text(encoding="utf-8"))
    index_tree = ast.parse(INDEX.read_text(encoding="utf-8"))
    exports = {
        decorator.args[0].value: node
        for node in index_tree.body if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "c_abi_export"
    }
    for symbol, signature in expected.items():
        assert signatures[symbol] == signature
        declarations = [
            node.value for node in copy_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "extern"
            and isinstance(node.value.args[0], ast.Constant)
            and node.value.args[0].value == symbol
        ]
        assert len(declarations) == 1
        declaration = declarations[0]
        assert tuple(arg.id for arg in declaration.args[1].elts) == signature[0]
        assert declaration.args[2].id == signature[1]
        function = exports[symbol]
        assert len(function.args.args) == 1
        assert function.args.args[0].annotation.id == "c_ptr"
        if signature[1] == "c_int64":
            assert function.returns.id == "i64"
        else:
            assert isinstance(function.returns, ast.Constant)
            assert function.returns.value is None
