"""Source-extracted root-audit lifetime controls; native operations are forbidden."""

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/backend/self_backend_precise_stackmaps.py"
ARENA = ROOT / "pcc/backend/self_backend_value_arena.py"


class InjectedFailure(BaseException):
    pass


def _forbidden(*args, **kwargs):
    raise AssertionError("native operation is forbidden in host lifetime controls")


def _fail(func, message):
    raise ValueError(message)


def _exec(nodes, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<root-audit-lifetime>", "exec"), namespace)


def _namespace(*, fault=None, close_faults=(), close_after_release=False, source=SOURCE):
    namespace = dict(valueclass=lambda cls: dataclass(frozen=True)(cls), _fail=_fail,
                     BackendUnavailable=ValueError, PARSED_INSTRUCTION_KIND_CALL=1,
                     CALL_FLAG_FRAME_PROTOCOL=8)
    arena_tree = ast.parse(ARENA.read_text())
    namespace.update({(node.asname or node.name): _forbidden for item in arena_tree.body
                      if isinstance(item, ast.ImportFrom) for node in item.names})
    namespace["_py_bytes_new"] = _forbidden
    _exec([node for node in arena_tree.body if isinstance(node, ast.ClassDef)
           and node.name in ("CompilerInt2", "CompilerInt3", "CompilerInt4", "CompilerIntArena")], namespace)
    base = namespace["CompilerIntArena"]
    error = InjectedFailure("original operation")
    close_errors = {label: InjectedFailure("close " + label) for label in close_faults}
    counts, events, roots = {}, [], []

    def step(label, operation):
        # Root-plane accesses before the audit are outside this fault boundary.
        if label.startswith("roots.") and TrackingArena.audit_index == 0:
            return
        key = (label, operation)
        counts[key] = counts.get(key, 0) + 1
        if fault == (*key, counts[key]):
            raise error

    class PoisonList(list):
        def __init__(self, owner):
            self.owner = owner

        def check(self):
            assert not self.owner._closed, "access after close: " + self.owner.label

        def __getitem__(self, index):
            self.check()
            return super().__getitem__(index)

        def __setitem__(self, index, value):
            self.check()
            return super().__setitem__(index, value)

        def append(self, value):
            self.check()
            return super().append(value)

        def clear(self):
            self.check()
            return super().clear()

    class TrackingArena(base):
        instances = []
        audit_index = 0
        constructing_roots = True

        def __init__(self, initial_capacity=8):
            assert initial_capacity >= 0
            self.label = "roots"
            if not self.constructing_roots:
                self.label = ("reachable", "pending")[TrackingArena.audit_index]
                TrackingArena.audit_index += 1
                step(self.label, "construct")
            self._address = self._length = self._capacity = 0
            self._closed = False
            self._values = PoisonList(self)
            self.close_calls = self.releases = 0
            self.released_values = None
            self.instances.append(self)

        def __len__(self):
            self._values.check()
            step(self.label, "length")
            return base.__len__(self)

        def append_zeros(self, count):
            step(self.label, "append_zeros")
            return base.append_zeros(self, count)

        def append(self, value):
            step(self.label, "append")
            return base.append(self, value)

        def get_unchecked(self, index):
            step(self.label, "get_unchecked")
            return base.get_unchecked(self, index)

        def set_unchecked(self, index, value):
            step(self.label, "set_unchecked")
            return base.set_unchecked(self, index, value)

        def close(self):
            self.close_calls += 1
            events.append(self.label)
            if self.label in close_errors and not close_after_release:
                raise close_errors[self.label]
            if not self._closed:
                self.released_values = tuple(self._values)
                self.releases += 1
            base.close(self)
            if self.label in close_errors:
                raise close_errors[self.label]

    namespace["CompilerIntArena"] = TrackingArena
    stackmap_tree = ast.parse(source.read_text())
    _exec([node for node in stackmap_tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
           and node.name in ("PackedRootStatePlane", "_native_index_capacity", "_native_root_states")], namespace)
    packed = namespace["PackedRootStatePlane"]

    class TrackingRoots(packed):
        def __init__(self, block_count, protocol_hint):
            super().__init__(block_count, protocol_hint)
            for name in packed.__slots__:
                arena = getattr(self, name)
                if isinstance(arena, TrackingArena):
                    arena.label = "roots." + name
            roots.append(self)
            TrackingArena.constructing_roots = False

    namespace.update(PackedRootStatePlane=TrackingRoots, TrackingArena=TrackingArena,
                     error=error, close_errors=close_errors, events=events, roots=roots, step=step)
    return namespace


class Kernel:
    def __init__(self, namespace, blocks):
        self.ns, self.blocks = namespace, blocks
        self.block_names = ["b" + str(index) for index in range(len(blocks))]
        self.block_records, self.metadata, self.edge_spans, self.errors = [], [], [], []
        self.audit_visits = []
        R2, R4 = namespace["CompilerInt2"], namespace["CompilerInt4"]
        for block in blocks:
            count = block.get("instructions", 0)
            self.block_records.append(R4(len(self.metadata), count, 0, -1))
            self.metadata.extend(R4(0, -1, 0, 0) for _ in range(count))
            errors = block.get("errors", [])
            self.edge_spans.append(R2(len(self.errors), len(errors)))
            self.errors.extend(errors)

    def block_fact(self, block): return self.block_records[block]
    def instruction_metadata_by_id(self, index): return self.metadata[index]
    def inline_error_edge_span(self, block): return self.edge_spans[block]
    def inline_error_edge_trigger(self, edge): return self.errors[edge][0]
    def inline_error_edge_target(self, edge): return self.errors[edge][1]
    def terminator_successor_count(self, block): return len(self.blocks[block].get("edges", []))
    def terminator_successor_id(self, block, index): return self.blocks[block].get("edges", [])[index]

    def successors(self, block):
        data = self.blocks[block]
        return data.get("audit_edges", data.get("edges", []) + [target for _, target in data.get("errors", [])])

    def cfg_successor_count(self, block):
        self.ns["step"]("kernel", "cfg_successor_count")
        self.audit_visits.append(block)
        return len(self.successors(block))

    def cfg_successor_id(self, block, index):
        self.ns["step"]("kernel", "cfg_successor_id")
        return self.successors(block)[index]


def _run(namespace, blocks):
    kernel = Kernel(namespace, blocks)
    return namespace["_native_root_states"](SimpleNamespace(name="root_audit"), {}, None, kernel), kernel


def _scratch(namespace):
    return [arena for arena in namespace["TrackingArena"].instances
            if arena.label in ("reachable", "pending")]


def _assert_roots_open(namespace):
    for arena in namespace["TrackingArena"].instances:
        if arena.label.startswith("roots."):
            assert not arena._closed and arena.close_calls == 0
            arena.diagnostic_values()


@pytest.mark.parametrize("blocks", [
    [],
    [{}],
    [dict(edges=[1, 1]), dict(edges=[1, 0]), {}],
    [dict(instructions=2, edges=[1, 1], errors=[(0, 2), (1, 2)]),
     dict(edges=[3]), dict(edges=[3, 0]), dict(edges=[3]), {}],
])
def test_root_audit_retires_scratch_and_preserves_returned_roots(blocks):
    namespace = _namespace()
    result, kernel = _run(namespace, blocks)
    expected, queue = set(), [0] if blocks else []
    while queue:
        block = queue.pop(0)
        if block not in expected:
            expected.add(block)
            queue.extend(kernel.successors(block))
    assert result is namespace["roots"][0]
    assert result.entry_state_ids.diagnostic_values() == [0 if block in expected else -1
                                                         for block in range(len(blocks))]
    assert set(result.queue.diagnostic_values()) == expected
    assert len(result.queue) == len(expected)
    assert set(kernel.audit_visits) == expected and len(kernel.audit_visits) == len(expected)
    scratch = _scratch(namespace)
    assert [arena.label for arena in scratch] == (["reachable", "pending"] if blocks else [])
    assert namespace["events"] == (["pending", "reachable"] if blocks else [])
    for arena in scratch:
        assert arena._closed and arena.close_calls == arena.releases == 1
        with pytest.raises(AssertionError, match="access after close"):
            len(arena)
    if scratch:
        assert scratch[0].released_values == tuple(int(block in expected) for block in range(len(blocks)))
        assert set(scratch[1].released_values) == expected
        assert len(scratch[1].released_values) == len(expected)
    _assert_roots_open(namespace)
    result.close()
    assert all(arena._closed and arena.close_calls == 1 for arena in namespace["TrackingArena"].instances)


FAULTS = [
    ("reachable", "construct", 1),
    ("reachable", "append_zeros", 1),
    ("reachable", "set_unchecked", 1),
    ("pending", "construct", 1),
    ("pending", "append", 1),
    ("pending", "length", 1),
    ("pending", "get_unchecked", 1),
    ("roots.entry_state_ids", "get_unchecked", 1),
    ("kernel", "cfg_successor_count", 1),
    ("kernel", "cfg_successor_id", 1),
    ("reachable", "get_unchecked", 1),
    ("reachable", "set_unchecked", 2),
    ("pending", "append", 2),
]


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("close_faults", [(), ("reachable", "pending")])
def test_root_audit_fault_preserves_exception_and_closes_each_constructed_arena(fault, close_faults):
    namespace = _namespace(fault=fault, close_faults=close_faults)
    with pytest.raises(InjectedFailure) as caught:
        _run(namespace, [dict(edges=[1]), {}])
    assert caught.value is namespace["error"]
    scratch = _scratch(namespace)
    expected = [] if fault == FAULTS[0] else ["reachable"] if fault in FAULTS[1:4] else ["reachable", "pending"]
    assert [arena.label for arena in scratch] == expected
    assert namespace["events"] == list(reversed(expected))
    for arena in scratch:
        assert arena.close_calls == 1
        assert arena.releases == int(arena.label not in close_faults)
        assert arena._closed == (arena.label not in close_faults)
    _assert_roots_open(namespace)


@pytest.mark.parametrize("close_faults", [("pending",), ("reachable",), ("pending", "reachable")])
@pytest.mark.parametrize("after_release", [False, True])
def test_root_audit_close_failure_still_attempts_other_release(close_faults, after_release):
    namespace = _namespace(close_faults=close_faults, close_after_release=after_release)
    with pytest.raises(InjectedFailure) as caught:
        _run(namespace, [dict(edges=[1]), {}])
    first = "pending" if "pending" in close_faults else "reachable"
    assert caught.value is namespace["close_errors"][first]
    assert namespace["events"] == ["pending", "reachable"]
    for arena in _scratch(namespace):
        assert arena.close_calls == 1
        assert arena.releases == int(after_release or arena.label not in close_faults)
    _assert_roots_open(namespace)


@pytest.mark.parametrize("close_faults", [(), ("reachable", "pending")])
def test_missing_reachable_root_state_keeps_audit_diagnostic(close_faults):
    namespace = _namespace(close_faults=close_faults)
    with pytest.raises(ValueError, match="^reachable block 'b1' has no entry root state$"):
        _run(namespace, [dict(audit_edges=[1]), {}])
    assert namespace["events"] == ["pending", "reachable"]
    assert all(arena.close_calls == 1 for arena in _scratch(namespace))
    _assert_roots_open(namespace)


@pytest.mark.parametrize("blocks, message", [
    ([dict(edges=[-1])], "unknown CFG successor id -1"),
    ([dict(instructions=1, errors=[(1, 1)]), {}], "inline error edge trigger exceeds source block"),
    ([dict(instructions=2, errors=[(1, 1), (0, 1)]), {}], "inline error edges are not trigger-ordered"),
])
def test_pre_audit_diagnostics_do_not_allocate_audit_scratch(blocks, message):
    namespace = _namespace()
    with pytest.raises(ValueError, match=message):
        _run(namespace, blocks)
    assert _scratch(namespace) == [] and namespace["events"] == []
    _assert_roots_open(namespace)
