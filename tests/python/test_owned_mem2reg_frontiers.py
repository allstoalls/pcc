"""Bounded host graph controls for completed dominance-frontier suffixes."""

import copy
import inspect
import random
import sys

from pcc.ir.optimization import mem2reg as owned


def _frontiers_reference(order, predecessors, idom, reachable):
    """Frozen original frontier walk, retaining ordering and failure guards."""
    frontiers = {}
    members = {}
    for block in order:
        frontiers[block] = []
        members[block] = set()
    for block in order:
        joining = []
        for predecessor in predecessors.get(block, []):
            if predecessor in reachable:
                joining.append(predecessor)
        if len(joining) < 2:
            continue
        stop = idom[block]
        for predecessor in joining:
            runner = predecessor
            steps = 0
            while runner != stop:
                steps = steps + 1
                if steps > owned._IDOM_WALK_LIMIT:
                    return None
                if block not in members[runner]:
                    members[runner].add(block)
                    frontiers[runner].append(block)
                next_runner = idom.get(runner, "")
                if next_runner == "" or next_runner == runner:
                    runner = stop
                    continue
                runner = next_runner
    return frontiers


def _outcome(function, graph):
    try:
        result = function(*graph)
    except Exception as exc:
        return "raise", type(exc), exc.args
    # Compare dictionary insertion order as well as each frontier's order.
    return "return", None if result is None else list(result.items())


def _shared_exit_graph(depth, leaves):
    chain = ["chain" + str(index) for index in range(depth)]
    tips = ["tip" + str(index) for index in range(leaves)]
    order = ["entry", *chain, *tips, "join"]
    predecessors = {name: [] for name in order}
    idom = {"entry": "entry", "join": "entry"}
    parent = "entry"
    for name in chain:
        predecessors[name] = [parent]
        idom[name] = parent
        parent = name
    for name in tips:
        predecessors[name] = [parent]
        idom[name] = parent
    predecessors["join"] = ["entry", *tips]
    return order, predecessors, idom, set(order)


def _count_parent_lookups(function, graph):
    # A dict subclass would select the compatibility path. Count the actual
    # parent-lookup line while keeping all graph inputs exact builtins.
    source, start = inspect.getsourcelines(function)
    offsets = [index for index, line in enumerate(source)
               if line.strip() == 'next_runner = idom.get(runner, "")']
    assert len(offsets) == 1
    location = start + offsets[0]
    count = 0

    def trace(frame, event, _arg):
        nonlocal count
        if frame.f_code is function.__code__:
            if event == "line" and frame.f_lineno == location:
                count += 1
            return trace
        return None

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        result = _outcome(function, graph)
    finally:
        sys.settrace(previous)
    return result, count


def test_frontier_suffixes_match_generated_cfgs(monkeypatch):
    rng = random.Random(1709)
    for _sample in range(96):
        names = ["b" + str(index) for index in range(rng.randrange(2, 11))]
        successors = {name: [] for name in names}
        # A chain makes the main graph reachable; extra edges include loops,
        # duplicate branches and joins. The orphan remains unreachable.
        for left, right in zip(names, names[1:]):
            successors[left].append(right)
        for _edge in range(rng.randrange(2 * len(names))):
            successors[rng.choice(names)].append(rng.choice(names))
        successors["orphan"] = [rng.choice(names)]
        predecessors = {name: [] for name in successors}
        for source, targets in successors.items():
            for target in targets:
                predecessors[target].append(source)
        order = owned._reverse_postorder(names[0], successors)
        reachable = set(order)
        monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 1 << 20)
        idom = owned._immediate_dominators(order, predecessors, reachable)
        assert idom is not None
        graph = order, predecessors, idom, reachable
        before = copy.deepcopy(graph)
        for limit in (0, 1, len(names) // 2, len(names), 1 << 20):
            monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", limit)
            assert _outcome(owned._dominance_frontiers, graph) == _outcome(_frontiers_reference, graph)
            assert graph == before


def test_frontier_suffixes_preserve_errors_and_terminal_parents(monkeypatch):
    def graph(parents, incoming=None, order=None):
        blocks = ["entry", "a", "b", "join"] if order is None else order
        return blocks, {"join": ["a", "a"] if incoming is None else incoming}, parents, set(blocks)

    cases = [
        ("missing-join", graph({"a": "entry"}), ("raise", KeyError, ("join",))),
        ("single-predecessor", graph({}, ["a"]), "success"),
        ("missing-parent", graph({"join": "entry"}), "success"),
        ("self-parent", graph({"join": "entry", "a": "a"}), "success"),
        ("unknown-runner", graph({"join": "entry", "a": "unknown"}),
         ("raise", KeyError, ("unknown",))),
        ("cycle", graph({"join": "entry", "a": "b", "b": "a"}), ("return", None)),
        ("stop-outside-order", graph({"join": "outside", "a": "outside"}), "success"),
        ("stop-is-predecessor", graph({"join": "a"}), "success"),
        ("unreachable-predecessor", graph({"join": "entry"}, ["ghost", "a"]), "success"),
        ("duplicate-order", graph({"join": "entry", "a": "entry"},
                                  order=["entry", "a", "join", "join"]), "success"),
    ]
    for name, value, expected in cases:
        monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 4)
        actual = _outcome(owned._dominance_frontiers, value)
        assert actual == _outcome(_frontiers_reference, value), name
        if expected == "success":
            assert actual[0] == "return" and actual[1] is not None, name
        else:
            assert actual == expected, name
        # Original guard wins over an unknown runner at limit zero.
        monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 0)
        assert _outcome(owned._dominance_frontiers, value) == _outcome(_frontiers_reference, value), name


def test_frontier_suffixes_charge_full_cached_length_at_boundary(monkeypatch):
    order = ["entry", "a", "b", "long", "join"]
    predecessors = {"join": ["b", "long"]}
    idom = {"entry": "entry", "a": "entry", "b": "a", "long": "b", "join": "entry"}
    graph = order, predecessors, idom, set(order)
    for limit in (-1, 0, 1, 2, 3, 4):
        monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", limit)
        actual = _outcome(owned._dominance_frontiers, graph)
        assert actual == _outcome(_frontiers_reference, graph)
        assert (actual == ("return", None)) is (limit < 3)
    monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 2)
    actual, count = _count_parent_lookups(owned._dominance_frontiers, graph)
    expected, old_count = _count_parent_lookups(_frontiers_reference, graph)
    assert actual == expected == ("return", None)
    assert count == 3 and old_count == 4
    # A zero-length predecessor walk never checks the limit, even if negative.
    graph = ["entry", "join"], {"join": ["entry", "entry"]}, {"join": "entry"}, {"entry", "join"}
    monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", -1)
    assert _outcome(owned._dominance_frontiers, graph) == ("return", [("entry", []), ("join", [])])


def test_frontier_suffixes_keep_legacy_callbacks_and_error_priority(monkeypatch):
    def outcome(function, case):
        events = []

        class Parents(dict):
            def get(self, key, default=None):
                events.append(("parent-get", key))
                if case == "repeated-parent-error" and events.count(("parent-get", key)) == 2:
                    raise ValueError("original repeated parent lookup")
                value = super().get(key, default)
                if case == "parent-mutation" and key == "chain1":
                    self[key] = "entry"
                return value

        class Blocks(list):
            def __iter__(self):
                events.append("blocks-iter")
                return super().__iter__()

        class Incoming(list):
            def __iter__(self):
                events.append("incoming-iter")
                return super().__iter__()

        class Preds(dict):
            def get(self, key, default=None):
                events.append(("preds-get", key))
                return super().get(key, default)

        class Reachable(set):
            def __contains__(self, key):
                events.append(("reachable", key))
                return super().__contains__(key)

        class Label(str):
            def __hash__(self):
                events.append(("label-hash", str(self)))
                return super().__hash__()

        class Limit(int):
            def __lt__(self, value):
                events.append(("limit", value))
                return super().__lt__(value)

        order, predecessors, idom, reachable = _shared_exit_graph(2, 2)
        limit = 10
        if case in ("parent-callback", "repeated-parent-error", "parent-mutation"):
            idom = Parents(idom)
        elif case == "order-subclass":
            order = Blocks(order)
        elif case == "incoming-subclass":
            predecessors["join"] = Incoming(predecessors["join"])
        elif case == "predecessor-subclass":
            predecessors = Preds(predecessors)
        elif case == "reachable-subclass":
            reachable = Reachable(reachable)
        elif case == "label-subclass":
            order[1] = Label(order[1])
        elif case == "limit-subclass":
            limit = Limit(limit)
        elif case == "tuple-order":
            order = tuple(order)
        elif case == "iterator-order":
            order = iter(order)
        elif case == "tuple-incoming":
            predecessors["join"] = tuple(predecessors["join"])
        elif case in ("bad-parent-limit", "bad-parent-error"):
            idom["tip0"] = []
            limit = 1 if case == "bad-parent-limit" else 2
        elif case == "noniterable-order":
            order = None
        else:
            raise AssertionError(case)
        monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", limit)
        events.clear()
        result = _outcome(function, (order, predecessors, idom, reachable))
        return result, events

    cases = ("parent-callback", "repeated-parent-error", "parent-mutation", "order-subclass",
             "incoming-subclass", "predecessor-subclass", "reachable-subclass",
             "label-subclass", "limit-subclass", "tuple-order", "iterator-order",
             "tuple-incoming", "bad-parent-limit", "bad-parent-error", "noniterable-order")
    for case in cases:
        assert outcome(owned._dominance_frontiers, case) == outcome(_frontiers_reference, case), case
    assert outcome(owned._dominance_frontiers, "bad-parent-limit")[0] == ("return", None)
    assert outcome(owned._dominance_frontiers, "bad-parent-error")[0][0:2] == ("raise", TypeError)


def test_frontier_suffixes_reduce_shared_exit_walks_without_reordering(monkeypatch):
    monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 1 << 20)
    for depth, leaves in ((8, 8), (16, 16), (32, 32)):
        graph = _shared_exit_graph(depth, leaves)
        before = copy.deepcopy(graph)
        actual, count = _count_parent_lookups(owned._dominance_frontiers, graph)
        expected, old_count = _count_parent_lookups(_frontiers_reference, graph)
        assert actual == expected
        assert old_count == leaves * (depth + 1)
        assert count == depth + leaves
        assert graph == before
    # Repeated edges still exist for PHI construction; only the repeated walk
    # is reused after its first successful completion.
    graph = _shared_exit_graph(5, 1)
    graph[1]["join"] = ["entry", "tip0", "tip0", "tip0"]
    actual, count = _count_parent_lookups(owned._dominance_frontiers, graph)
    expected, old_count = _count_parent_lookups(_frontiers_reference, graph)
    assert actual == expected and count == 6 and old_count == 18
    assert graph[1]["join"] == ["entry", "tip0", "tip0", "tip0"]


def test_frontier_suffix_cache_is_per_join_and_preserves_list_order(monkeypatch):
    monkeypatch.setattr(owned, "_IDOM_WALK_LIMIT", 20)
    graph = _shared_exit_graph(2, 2)
    graph[0].append("second")
    graph[1]["second"] = ["tip1", "tip0"]
    graph[2]["second"] = "entry"
    graph[3].add("second")
    result = owned._dominance_frontiers(*graph)
    assert result == _frontiers_reference(*graph)
    for block in ("chain0", "chain1", "tip0", "tip1"):
        assert result[block] == ["join", "second"]


def test_frontier_suffixes_preserve_public_mem2reg_bytes(monkeypatch):
    source = """declare void @escape(ptr)
define i64 @pick(i1 %c, i1 %again) {
entry:
  %slot = alloca i64
  br i1 %c, label %left, label %right
left:
  store i64 1, ptr %slot
  br i1 %again, label %join, label %join
right:
  store i64 2, ptr %slot
  br label %join
join:
  %value = load i64, ptr %slot
  br i1 %again, label %left, label %done
done:
  ret i64 %value
}
"""
    fixtures = (source, source.replace("br i1 %again, label %left, label %done", "br label %done"),
                source.replace("store i64 1, ptr %slot", "call void @escape(ptr %slot)"),
                source.replace("br label %join", "br label %missing"))
    current = owned._dominance_frontiers
    for text in fixtures:
        monkeypatch.setattr(owned, "_dominance_frontiers", current)
        actual, changed = owned.mem2reg_text(text)
        monkeypatch.setattr(owned, "_dominance_frontiers", _frontiers_reference)
        expected, old_changed = owned.mem2reg_text(text)
        assert changed is old_changed
        assert actual.encode("utf-8") == expected.encode("utf-8")
        if text == source:
            assert changed and " = phi i64 " in actual
            assert actual.count("%left ]") == 2
