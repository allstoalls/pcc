"""Host models of the real page selector's registered-cursor protocol.

These execute source functions against raw-memory and external-provider doubles.
They do not compile a runtime or establish native relocation/concurrency behavior.
"""

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SELECTOR = ROOT / "pcc/runtime/py/freestanding_gc_relocation_selector.py"
LIFECYCLE = ROOT / "pcc/runtime/py/freestanding_gc_zpage_lifecycle.py"


def _load_functions(path, namespace, names=None):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and (names is None or node.name in names)
    ]
    if names is not None:
        assert {node.name for node in functions} == names
    for node in functions:
        node.decorator_list = []
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"),
        namespace,
    )


class _SelectorMemory:
    def __init__(self, threads_enabled=1):
        self.cells = {}
        self.allocation_sizes = {}
        self.freed = set()
        self.next_pointer = 1024
        self.nodes = set()
        self.locked = False
        self.members = []
        self.refused = set()
        self.heap_visits = 0
        self.heap_visits_by_lock = {False: 0, True: 0}
        self.scan_starts = []
        self.scan_member_counts = []
        self.commit_chunks = 0
        self.malloc_calls = 0
        self.malloc_allocations = set()
        self.malloc_poll_counts = []
        self.fail_malloc_at = None
        self.after_malloc = None
        self.on_poll = None
        self.polls = 0
        # Preserve the threaded mutation model by default. Raw malloc cannot
        # run a managed mutation callback in the threads-off no-poll interval.
        self.threads_enabled = threads_enabled
        self.runtime_guard_polls = []
        self.runtime_guard_kinds = []
        self.rank_entry_pending = False
        self.rank_building = False
        self.rank_score_count = 0
        self.rank_score_batches = []
        self.ranked_page_batches = []
        self.page_best_batches = []
        self.namespace = {
            "i64": int,
            "null": lambda: 0,
            "ptr_eq": lambda left, right: int(left == right),
            "ptr_is_null": lambda value: int(value == 0),
            "ptr_add": self.ptr_add,
            "is_tagged_int": lambda value: 0,
            "global_addr": lambda name: name,
            "global_load_ptr": self.global_get,
            "global_store_ptr": self.global_set,
            "load_ptr": self.read,
            "load_i64": self.read,
            "load_i32": self.read,
            "store_ptr": self.write,
            "store_i64": self.write,
            "store_i32": self.write,
            "stack_alloc": self.new,
            "malloc": self.malloc,
            "free": self.free,
            "atomic_rmw_i32": self.atomic,
            "abi_constant": {
                # Model objects are tag 5; scheduler-bearing tags are outside
                # this model's scope and must retain their native coverage.
                "object.type.thread": 10001,
                "object.type.virtual_thread": 10002,
                "object.type.vthread_channel": 10003,
            }.__getitem__,
            "pcc_gc_config_ensure": lambda: 4,
            "pcc_current_thread_id": lambda: 1,
            "pcc_threads_enabled": self.runtime_threads_enabled,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "pcc_thread_safepoint": self.poll,
            "pcc_gc_object_is_address_pinned": (
                lambda obj: int(bool(self.read(obj, 12) & 64))
            ),
            "pcc_gc_backend4_relocate_copy_supported_tag": lambda tag: 1,
            "pcc_gc_forwarding_find": lambda obj: int(obj in self.refused),
            "pcc_gc_forwarding_target_exists": lambda obj: 0,
            "pcc_gc_backend4_zpage_clear_active_page": lambda page: None,
            "pcc_gc_relocation_member_note": self.members.append,
            "pcc_gc_zpage_owner_index_remove": lambda obj: 0,
        }
        _load_functions(SELECTOR, self.namespace)
        _load_functions(
            LIFECYCLE,
            self.namespace,
            {
                "pcc_gc_backend4_zpage_unlink_node",
                "pcc_gc_backend4_zpage_set_page_head",
                "pcc_gc_backend4_zpage_page_head",
            },
        )
        scan = self.namespace["_best_relocation_page_batch"]
        commit = self.namespace["_select_page_objects_batch"]
        rank_begin = self.namespace["_single_thread_page_rank_begin"]
        ranked_page = self.namespace["_single_thread_ranked_page"]
        page_best = self.namespace["_single_thread_page_best_batch"]
        candidate_score = self.namespace["_backend4_zpage_candidate_score"]

        def bounded_scan(owner, page, unselected, large, result):
            assert self.locked
            if self.global_get("pcc_gc_backend4_selector_scan_owner") == 0:
                self.scan_starts.append((page, unselected))
                self.scan_member_counts.append(len(self.members))
            outcome = scan(owner, page, unselected, large, result)
            assert 0 <= self.read(result, 8) <= 16
            return outcome

        def bounded_commit(owner, seed, budget, large, plan, result):
            assert self.locked
            outcome = commit(owner, seed, budget, large, plan, result)
            assert 0 <= self.read(result, 0) <= 16
            self.commit_chunks += 1
            return outcome

        def begin_rank(state):
            assert not self.locked and self.threads_enabled == 0
            self.rank_building = True
            try:
                return rank_begin(state)
            finally:
                self.rank_building = False

        def locked_score(node, large):
            assert self.locked
            if self.rank_building:
                self.rank_score_count += 1
            return candidate_score(node, large)

        def bounded_ranked_page(state, result):
            assert self.locked and self.threads_enabled == 0
            outcome = ranked_page(state, result)
            examined = self.read(result, 8)
            assert 0 <= examined <= 16
            self.ranked_page_batches.append(examined)
            return outcome

        def bounded_page_best(page, state, result):
            assert self.locked and self.threads_enabled == 0
            outcome = page_best(page, state, result)
            examined = self.read(result, 8)
            assert 0 <= examined <= 16
            self.page_best_batches.append(examined)
            return outcome

        self.namespace["_best_relocation_page_batch"] = bounded_scan
        self.namespace["_select_page_objects_batch"] = bounded_commit
        self.namespace["_single_thread_page_rank_begin"] = begin_rank
        self.namespace["_backend4_zpage_candidate_score"] = locked_score
        self.namespace["_single_thread_ranked_page"] = bounded_ranked_page
        self.namespace["_single_thread_page_best_batch"] = bounded_page_best

    def new(self, size):
        self.next_pointer += max(16, size)
        self.cells[self.next_pointer] = {}
        self.allocation_sizes[self.next_pointer] = size
        return self.next_pointer

    def address(self, pointer, offset, width=8):
        # Allocation IDs are opaque; interior arena pointers retain both the
        # allocation identity and byte displacement, including aliasing at +0.
        if isinstance(pointer, tuple):
            pointer, displacement = pointer
            offset += displacement
        assert pointer in self.allocation_sizes
        assert pointer not in self.freed, "selector dereferenced freed metadata"
        assert 0 <= offset and offset + width <= self.allocation_sizes[pointer]
        return pointer, offset

    def ptr_add(self, pointer, offset):
        base, displacement = self.address(pointer, offset, width=0)
        return (base, displacement) if displacement else base

    def malloc(self, size):
        assert not self.locked
        self.malloc_calls += 1
        self.malloc_poll_counts.append(self.polls)
        if self.fail_malloc_at is not None and self.malloc_calls >= self.fail_malloc_at:
            return 0
        result = self.new(size)
        self.malloc_allocations.add(result)
        if self.after_malloc is not None:
            assert self.threads_enabled != 0
            self.after_malloc(self)
        return result

    def runtime_threads_enabled(self):
        # The immutable provider is queried once outside the lock to choose
        # multi-page ranking. Saved-preflight checks still require the lock.
        if self.locked:
            self.runtime_guard_kinds.append("preflight")
        else:
            assert self.rank_entry_pending
            self.rank_entry_pending = False
            self.runtime_guard_kinds.append("rank-entry")
        self.runtime_guard_polls.append(self.polls)
        return self.threads_enabled

    def free(self, pointer):
        assert pointer not in self.freed
        assert pointer in self.cells
        self.freed.add(pointer)

    def read(self, pointer, offset):
        if isinstance(pointer, str):
            return self.cells.get(pointer, {}).get(offset, 0)
        pointer, offset = self.address(pointer, offset)
        if pointer in self.nodes and offset == 16:
            self.heap_visits += 1
            self.heap_visits_by_lock[self.locked] += 1
        return self.cells[pointer].get(offset, 0)

    def write(self, pointer, offset, value):
        if not isinstance(pointer, str):
            pointer, offset = self.address(pointer, offset)
        self.cells.setdefault(pointer, {})[offset] = value

    def global_get(self, name):
        return self.read(name, 0)

    def global_set(self, name, value):
        self.write(name, 0, value)

    def atomic(self, operation, pointer, offset, value, ordering):
        assert self.locked and operation == "or" and ordering == "acq_rel"
        old = self.read(pointer, offset)
        self.write(pointer, offset, old | value)
        return old

    def lock(self):
        assert not self.locked
        self.locked = True

    def unlock(self):
        assert self.locked
        if self.rank_building:
            assert 0 <= self.rank_score_count <= 16
            self.rank_score_batches.append(self.rank_score_count)
            self.rank_score_count = 0
        self.locked = False

    def poll(self):
        assert not self.locked, "safepoint while graph-locked"
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self)

    def add_owner(self, page, flags=0, pressure=0):
        obj, node = self.new(64), self.new(80)
        self.nodes.add(node)
        self.write(obj, 8, 5)
        self.write(obj, 12, flags)
        self.write(node, 0, obj)
        self.write(node, 8, page)
        self.write(node, 32, 64)
        self.write(node, 72, pressure)
        head = self.global_get("pcc_gc_backend4_zpage_head")
        self.write(node, 16, head)
        if head:
            self.write(head, 40, node)
        self.global_set("pcc_gc_backend4_zpage_head", node)
        page_head = self.read(page, 112)
        self.write(node, 48, page_head)
        if page_head:
            self.write(page_head, 56, node)
        self.write(page, 112, node)
        self.write(page, 0, obj)
        self.write(page, 32, self.read(page, 32) + 1)
        self.write(page, 8, self.read(page, 8) + 64)
        return node

    def page(self, owners, pressure=0):
        page = self.new(376)
        # Keep a positive, equal fragmentation score across model pages.
        self.write(page, 16, 4096 + owners * 64)
        self.write(page, 40, pressure)
        nodes = [self.add_owner(page) for _ in range(owners)]
        return page, nodes

    def remove(self, node):
        assert self.locked
        page, obj = self.read(node, 8), self.read(node, 0)
        self.namespace["pcc_gc_backend4_zpage_unlink_node"](node)
        self.write(page, 32, self.read(page, 32) - 1)
        self.write(page, 8, self.read(page, 8) - 64)
        self.free(node)
        self.free(obj)

    def select(self, budget, foreign_page_owner=0, foreign_scan_owner=0):
        self.heap_visits = 0
        self.heap_visits_by_lock = {False: 0, True: 0}
        self.rank_entry_pending = budget > 1
        selected = self.namespace["pcc_gc_backend4_select_relocation_pages"](budget)
        assert not self.locked and not self.rank_entry_pending
        assert self.global_get("pcc_gc_backend4_selector_scan_owner") == foreign_scan_owner
        assert self.global_get("pcc_gc_backend4_selector_page_owner") == foreign_page_owner
        return selected


@pytest.mark.parametrize("owners", [17, 48, 64, 128])
def test_page_cursor_continues_without_a_heap_scan_per_object_batch(owners):
    memory = _SelectorMemory()
    _, nodes = memory.page(owners)
    objects = {memory.read(node, 0) for node in nodes}

    assert memory.select(1) == owners
    assert set(memory.members) == objects
    assert len(memory.members) == len(set(memory.members))
    assert memory.heap_visits == owners * 2
    assert len(memory.scan_starts) == 2
    assert memory.commit_chunks >= (owners + 15) // 16


@pytest.mark.parametrize("owners", [1, 16, 17, 48, 64, 128])
def test_threads_off_reuses_static_preflight_and_saves_one_heap_walk(owners):
    outcomes = []
    for threads_enabled in (1, 0):
        memory = _SelectorMemory(threads_enabled=threads_enabled)
        _, nodes = memory.page(owners)
        # Equal highest scores choose the first node in global-list order.
        memory.write(nodes[0], 72, 100)
        memory.write(nodes[owners // 2], 72, 100)
        seed = nodes[owners // 2]
        expected = [memory.read(seed, 0)] + [
            memory.read(node, 0) for node in reversed(nodes) if node != seed
        ]

        assert memory.select(1) == owners
        assert memory.members == expected
        assert memory.heap_visits == owners * (1 + threads_enabled)
        assert len(memory.scan_starts) == 1 + threads_enabled
        outcomes.append(memory.members)
    assert outcomes[0] == outcomes[1]


def test_threads_off_preserves_mixed_page_score_and_seed_tie_order():
    outcomes = []
    for threads_enabled in (1, 0):
        memory = _SelectorMemory(threads_enabled=threads_enabled)
        low, low_nodes = memory.page(19, pressure=1)
        high, high_nodes = memory.page(48, pressure=100)
        tied_high, tied_nodes = memory.page(48, pressure=100)
        middle, middle_nodes = memory.page(33, pressure=10)
        memory.write(high_nodes[1], 72, 50)
        memory.write(high_nodes[5], 72, 50)
        seed = high_nodes[5]
        expected_nodes = (
            [seed]
            + [node for node in reversed(high_nodes) if node != seed]
            + list(reversed(tied_nodes))
            + list(reversed(middle_nodes))
        )

        assert memory.select(3) == len(expected_nodes)
        assert memory.members == [memory.read(node, 0) for node in expected_nodes]
        assert all(memory.read(page, 108) == 1 for page in (high, tied_high, middle))
        assert memory.read(low, 108) == 0
        assert not ({memory.read(node, 0) for node in low_nodes} & set(memory.members))
        assert memory.runtime_guard_kinds == ["rank-entry"] + ["preflight"] * 3
        if threads_enabled:
            assert memory.heap_visits == 148 * 3 * 2
            assert memory.heap_visits_by_lock == {False: 0, True: 888}
            assert len(memory.scan_starts) == 6
            assert memory.rank_score_batches == memory.ranked_page_batches == []
        else:
            # Ranking counts the static owner list once outside the lock,
            # then scores it once in bounded locked batches. Selected pages
            # are skipped in the heap rather than rescanning the owner list.
            assert memory.heap_visits == 148 * 2
            assert memory.heap_visits_by_lock == {False: 148, True: 148}
            assert memory.scan_starts == []
            assert memory.rank_score_batches == [16] * 9 + [4]
            assert memory.ranked_page_batches == [1, 2] + [16] * 5 + [14]
            assert memory.page_best_batches == []
            assert memory.commit_chunks == 4 + 4 + 3
        outcomes.append(memory.members)
    assert outcomes[0] == outcomes[1]


def test_threads_off_reuse_reduces_duplicate_fresh_skip_encounters():
    outcomes = []
    for threads_enabled in (1, 0):
        memory = _SelectorMemory(threads_enabled=threads_enabled)
        page, nodes = memory.page(48)
        fresh = memory.read(nodes[0], 0)
        memory.write(fresh, 12, 16384)

        assert memory.select(1) == 47
        assert memory.members == [memory.read(node, 0) for node in reversed(nodes[1:])]
        # The ineligible owner is still encountered in preflight, the page
        # cursor, and the residual global scan. Only the duplicate initial
        # revalidation encounter disappears in the threads-off path.
        assert memory.global_get("pcc_gc_backend4_candidate_fresh_skips_g") == (
            3 + threads_enabled
        )
        assert memory.global_get("pcc_gc_backend4_relocation_add_refusals_g") == 0
        assert memory.scan_starts == [(0, 1)] + [(page, 0)] * (1 + threads_enabled)
        assert memory.heap_visits == 48 * (2 + threads_enabled)
        outcomes.append(memory.members)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("runtime_value", [-1, 0, 1, 2])
def test_preflight_reuse_requires_runtime_provider_zero(monkeypatch, runtime_value):
    # A contradictory build-option string cannot authorize or disable reuse.
    monkeypatch.setenv("PCC_WITH_THREADS", "1" if runtime_value == 0 else "0")
    memory = _SelectorMemory(threads_enabled=runtime_value)
    memory.page(48)

    assert memory.select(1) == 48
    assert len(memory.runtime_guard_polls) == 1
    walks = 1 if runtime_value == 0 else 2
    assert memory.heap_visits == 48 * walks
    assert len(memory.scan_starts) == walks


def test_threads_off_registers_saved_seed_before_first_post_preflight_poll():
    memory = _SelectorMemory(threads_enabled=0)
    page, nodes = memory.page(48)
    memory.write(nodes[0], 72, 100)
    registered = []

    def check_first_plan_handoff(model):
        if not model.malloc_calls or registered:
            return
        assert model.commit_chunks == 0 and not model.members
        assert model.global_get("pcc_gc_backend4_selector_page_owner") == 1
        assert model.global_get("pcc_gc_backend4_selector_page") == page
        assert model.global_get("pcc_gc_backend4_selector_page_seed") == nodes[0]
        assert model.global_get("pcc_gc_backend4_selector_page_seed_pending") == 1
        assert model.global_get("pcc_gc_backend4_selector_page_cursor") == nodes[-1]
        assert model.malloc_poll_counts == [model.runtime_guard_polls[0]] * 49
        assert model.polls == model.runtime_guard_polls[0] + 1
        registered.append(True)

    memory.on_poll = check_first_plan_handoff
    assert memory.select(1) == 48
    assert registered == [True]


def test_page_budget_and_score_order_preserve_complete_selected_pages():
    memory = _SelectorMemory()
    low, low_nodes = memory.page(19, pressure=1)
    high, high_nodes = memory.page(48, pressure=100)
    middle, middle_nodes = memory.page(33, pressure=10)
    expected = {
        memory.read(node, 0) for node in high_nodes + middle_nodes
    }

    assert memory.select(2) == 81
    assert set(memory.members) == expected
    assert memory.members[0] == memory.read(high_nodes[-1], 0)
    assert memory.read(high, 108) == memory.read(middle, 108) == 1
    assert memory.read(low, 108) == 0
    assert not ({memory.read(node, 0) for node in low_nodes} & set(memory.members))
    assert memory.heap_visits == 100 * 2 * 2
    assert len(memory.scan_starts) == 4
    assert memory.runtime_guard_kinds == ["rank-entry", "preflight", "preflight"]


@pytest.mark.parametrize("flags", [64, 16384])
@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_residual_pass_finds_an_owner_made_eligible_after_cursor_passage(
    flags, threads_enabled,
):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    _, nodes = memory.page(48)
    initially_ineligible = memory.read(nodes[-1], 0)
    memory.write(initially_ineligible, 12, flags)
    published = []

    def publish_after_first_chunk(model):
        if not published and model.commit_chunks == 1:
            model.lock()
            model.write(initially_ineligible, 12, 0)
            model.unlock()
            published.append(True)

    memory.on_poll = publish_after_first_chunk
    assert memory.select(1) == 48
    assert published == [True]
    assert initially_ineligible in memory.members
    # Only initial revalidation may reuse the seed; the residual pass still
    # walks all owners after a poll makes a passed node eligible again.
    assert len(memory.scan_starts) == 2 + threads_enabled
    assert memory.heap_visits == 48 * (2 + threads_enabled)


@pytest.mark.parametrize(
    ("remove_seed", "after_first_chunk"),
    [(False, False), (True, False), (False, True)],
)
@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_page_cursor_and_pending_seed_are_repaired_before_node_free(
    remove_seed, after_first_chunk, threads_enabled,
):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    _, nodes = memory.page(48)
    memory.write(nodes[0], 72, 100)  # Best seed differs from page-list head.
    removed = []

    def remove_at_handoff(model):
        if removed or not model.global_get("pcc_gc_backend4_selector_page_owner"):
            return
        if after_first_chunk and model.commit_chunks == 0:
            return
        name = "seed" if remove_seed else "cursor"
        node = model.global_get("pcc_gc_backend4_selector_page_" + name)
        removed.append(model.read(node, 0))
        model.lock()
        model.remove(node)
        model.unlock()

    memory.on_poll = remove_at_handoff
    assert memory.select(1) == 47
    assert len(removed) == 1 and removed[0] not in memory.members
    assert len(set(memory.members)) == 47


@pytest.mark.parametrize("after_first_chunk", [False, True])
@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_removing_all_page_owners_does_not_dereference_recycled_page(
    after_first_chunk, threads_enabled,
):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    page, nodes = memory.page(48)
    removed = []

    def remove_page_at_handoff(model):
        if removed or not model.global_get("pcc_gc_backend4_selector_page_owner"):
            return
        if after_first_chunk and model.commit_chunks == 0:
            return
        model.lock()
        for node in nodes:
            model.remove(node)
        # Unlink has repaired both registered node pointers. Poison the page
        # itself to reject a raw page-token continuation after recycling.
        model.free(page)
        model.unlock()
        removed.append(True)

    memory.on_poll = remove_page_at_handoff
    selected = memory.select(1)
    # Return values count successful admissions, including any earlier than
    # the modeled deletion. The model does not execute object-free callbacks.
    assert selected == len(memory.members)
    if after_first_chunk:
        assert 0 < selected < 48
    else:
        assert selected == 0
    assert removed == [True]


@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_competing_page_scan_owner_keeps_its_registered_state(threads_enabled):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    page, nodes = memory.page(48)
    memory.global_set("pcc_gc_backend4_selector_page_owner", 99)
    memory.global_set("pcc_gc_backend4_selector_page", page)
    memory.global_set("pcc_gc_backend4_selector_page_cursor", nodes[-1])
    memory.global_set("pcc_gc_backend4_selector_page_seed", nodes[0])

    assert memory.select(1, foreign_page_owner=99) == 0
    assert memory.global_get("pcc_gc_backend4_selector_page_cursor") == nodes[-1]
    assert memory.global_get("pcc_gc_backend4_selector_page_seed") == nodes[0]
    assert not memory.members
    assert memory.malloc_allocations <= memory.freed


@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_competing_discovery_owner_keeps_its_registered_state(threads_enabled):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    page, nodes = memory.page(48)
    foreign_state = {
        "owner": 99,
        "page": page,
        "cursor": nodes[-1],
        "best": nodes[0],
        "best_score": 4096,
        "allow_large": 1,
        "require_unselected": 1,
        "restart": 1,
    }
    for suffix, value in foreign_state.items():
        memory.global_set("pcc_gc_backend4_selector_scan_" + suffix, value)

    assert memory.select(1, foreign_scan_owner=99) == 0
    assert all(
        memory.global_get("pcc_gc_backend4_selector_scan_" + suffix) == value
        for suffix, value in foreign_state.items()
    )
    assert not memory.members
    assert memory.malloc_calls == 0
    assert memory.runtime_guard_polls == []


def test_page_growth_after_preflight_retries_before_any_admission():
    memory = _SelectorMemory()
    page, _ = memory.page(48)
    grew = []

    def grow_during_plan_allocation(model):
        if not grew:
            assert not model.members
            model.lock()
            model.add_owner(page)
            model.unlock()
            grew.append(True)

    memory.after_malloc = grow_during_plan_allocation
    assert memory.select(1) == 49
    assert grew == [True]
    assert memory.scan_starts.count((0, 1)) == 2
    assert [
        count for (page, unselected), count in zip(
            memory.scan_starts, memory.scan_member_counts
        ) if unselected
    ] == [0, 0]
    assert len(set(memory.members)) == 49
    assert len(memory.malloc_allocations & memory.freed) == 49
    assert len(memory.malloc_allocations - memory.freed) == 50


@pytest.mark.parametrize("failure", [1, 2, 17, 49])
@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_incomplete_page_plan_adds_nothing(failure, threads_enabled):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    page, _ = memory.page(48)
    memory.fail_malloc_at = failure

    assert memory.select(1) == 0
    assert memory.read(page, 108) == 0
    assert not memory.members
    assert memory.malloc_allocations <= memory.freed


@pytest.mark.parametrize("threads_enabled", [1, 0])
def test_refused_residual_candidate_ends_on_no_progress(threads_enabled):
    memory = _SelectorMemory(threads_enabled=threads_enabled)
    _, nodes = memory.page(48)
    refused = memory.read(nodes[0], 0)
    memory.refused.add(refused)

    assert memory.select(1) == 47
    assert refused not in memory.members
    assert len(memory.scan_starts) == 2 + threads_enabled
    assert memory.heap_visits == 48 * (2 + threads_enabled)


@pytest.mark.parametrize("budget", [0, 1, 16, 17])
def test_object_selector_keeps_its_object_budget(budget):
    memory = _SelectorMemory()
    memory.page(48)

    assert memory.namespace["pcc_gc_select_relocation_set"](budget) == budget
    assert len(memory.members) == budget
    assert not memory.locked
    assert memory.global_get("pcc_gc_backend4_selector_page_owner") == 0
