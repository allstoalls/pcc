"""Suffix record scratch stays local and is consumed before the next instruction."""

import ast
import inspect
from types import SimpleNamespace

import pytest

from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KIND_CALL
from pcc.backend.self_backend_value_arena import CompilerInt2, CompilerInt4


class Scalars(list):
    def get_unchecked(self, index):
        return self[index]


class Kernel:
    block_names = ["entry", "next"]
    block_layout_ids = Scalars()
    error_edge_scalars = Scalars([0])

    def block_fact(self, block_id):
        return CompilerInt4(0, 5, 0, -1) if block_id == 0 else CompilerInt4(5, 3, 0, -1)

    def inline_error_edge_span(self, block_id):
        return CompilerInt2(0, 1) if block_id == 0 else CompilerInt2(1, 0)

    def inline_error_edge_trigger(self, edge_id):
        return 3

    def instruction_metadata_by_id(self, instruction_id):
        return CompilerInt4(PARSED_INSTRUCTION_KIND_CALL, instruction_id, 0, 0)


class Plan:
    def __init__(self, on_label=lambda record: None, on_reload=lambda record: None):
        self.on_label = on_label
        self.on_reload = on_reload
        self.packed_records = SimpleNamespace(
            suffix_route_spans=Scalars([0, 3, 3, 2]),
            suffix_routes=Scalars([
                0, 1, 10, 0, 1, 11, 0, 3, 12,
                1, 0, 13, 1, 2, 14,
            ]),
            entry_route_spans=Scalars([0, 0, 0, 0]),
            terminator_route_spans=Scalars([0, 0, 0, 0]),
        )

    def append_packed_entry_lines(self, lines, block_id):
        pass

    def append_packed_terminator_lines(self, lines, block_id):
        pass

    def packed_record_label_line(self, record):
        self.on_label(record)
        return "label_" + str(record)

    def packed_record_reload_lines(self, record):
        self.on_reload(record)
        return ["reload_" + str(record)]

    def append_packed_label_span(self, owner, fragment, record):
        fragment.append(self.packed_record_label_line(record))

    def append_packed_reload_span(self, owner, fragment, record):
        fragment.extend(self.packed_record_reload_lines(record))


class Sink:
    def __init__(self):
        self.lines = []
        self.fragments = SimpleNamespace(new_fragment=list)

    def append(self, line):
        self.lines.append(line)

    def extend(self, lines):
        self.lines.extend(lines)

    def publish_fragment(self, fragment):
        self.lines.extend(fragment)


def _counted_emitter(monkeypatch):
    allocations = []

    class Scratch(list):
        def __init__(self):
            super().__init__()
            allocations.append(self)

        def clear(self):
            raise AssertionError("suffix reuse must not depend on native list.clear")

    # Instrument only this literal in a test-local copy of the actual emitter.
    # This checks dynamic allocation count without a production test hook or
    # relying on allocator address recycling. All control flow is unchanged.
    tree = ast.parse(inspect.getsource(emitter._emit_dense_indexed_function_blocks))
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "suffix_records":
            assignments.append(node)
    assert assignments
    for assignment in assignments:
        assert isinstance(assignment.value, ast.List)
        assert assignment.value.elts == []
        assignment.value = ast.Call(
            func=ast.Name(id="Scratch", ctx=ast.Load()), args=[], keywords=[],
        )
    ast.fix_missing_locations(tree)
    monkeypatch.setattr(emitter, "_direct_branch_targets_in_range", lambda kernel: True)
    monkeypatch.setattr(emitter, "_emit_dense_indexed_terminator", lambda *args: ["term"])
    monkeypatch.setattr(emitter, "_emit_dense_indexed_error_edge", lambda *args: ["error_edge"])
    monkeypatch.setattr(emitter, "_terms_emit_inline_error_stub_indexed", lambda *args: ["cold_stub"])

    def parts(func, kernel, block_id, index, instruction_id, *args):
        if instruction_id in (1, 3, 5, 7):
            return ["  bl helper", "result_" + str(instruction_id)]
        return ["body_" + str(instruction_id)]

    monkeypatch.setattr(emitter, "_emit_dense_indexed_instruction_parts", parts)
    namespace = dict(emitter.__dict__, Scratch=Scratch)
    exec(compile(tree, "<counted AArch64 suffix scratch>", "exec"), namespace)
    return namespace["_emit_dense_indexed_function_blocks"], allocations


def _expected(name):
    return [
        "L_" + name + "_entry:", "body_0", "  bl helper",
        "label_10", "label_11", "result_1", "reload_10", "reload_11",
        "body_2", "  bl helper", "label_12", "result_3", "reload_12",
        "error_edge", "body_4", "term", "", "L_" + name + "_next:",
        "  bl helper", "label_13", "result_5", "reload_13", "body_6",
        "  bl helper", "label_14", "result_7", "reload_14", "term", "cold_stub",
    ]


@pytest.mark.parametrize("native", [False, True], ids=["buffered", "native-sink"])
@pytest.mark.parametrize("shape", ["ordinary", "nested", "label-exception", "reload-exception"])
def test_suffix_scratch_is_per_invocation_and_preserves_route_order(monkeypatch, native, shape):
    emit, allocations = _counted_emitter(monkeypatch)

    def run(name, plan):
        sink = Sink() if native else None
        lines = emit(SimpleNamespace(name=name), Kernel(), plan, sink)
        return sink.lines if native else lines

    nested = []

    def on_label(record):
        if record != 11:
            return
        if shape == "nested":
            nested.append(run("nested", Plan()))
        elif shape == "label-exception":
            raise RuntimeError("intentional suffix failure")

    def on_reload(record):
        if record == 11 and shape == "reload-exception":
            raise RuntimeError("intentional suffix failure")

    if shape.endswith("exception"):
        with pytest.raises(RuntimeError, match="intentional suffix failure"):
            run("failed", Plan(on_label, on_reload))
        assert len(allocations) == 1
        assert allocations[0] == [10, 11]
    else:
        assert run("outer", Plan(on_label, on_reload)) == _expected("outer")
        # Eight instructions have only four suffix-bearing positions. There
        # is one initial list plus one replacement per nonempty batch.
        assert len(allocations) == (10 if shape == "nested" else 5)
        assert sum(not scratch for scratch in allocations) == (2 if shape == "nested" else 1)
        assert nested == ([_expected("nested")] if shape == "nested" else [])

    # A later invocation cannot inherit pending IDs, even after an exception.
    prior_allocations = len(allocations)
    assert run("again", Plan()) == _expected("again")
    assert len(allocations) == prior_allocations + 5
    assert allocations[-1] == []


def _owned_reload_case(*, nonempty=(), plan_type=None, incomplete=False, target="aarch64-darwin"):
    from pcc.backend.self_backend_aarch64_fragments import AArch64EmissionFragments
    from pcc.backend.self_backend_precise_stackmaps import (
        FunctionStackMapPlan,
        PackedPlannedSafepoints,
    )
    from pcc.backend.self_backend_value_arena import CompilerIntArena

    records = PackedPlannedSafepoints(block_names=["entry", "next"])
    for record in range(15):
        start = len(records.reload_scalars) // 3
        count = int(record in nonempty)
        if count:
            records.reload_scalars.append3(-8, -16, 0)
        records.spans.append4(0, 0, start, count)
        records.labels.append("L_reload_" + str(record))
    for values in ((0, 1, 10), (0, 1, 11), (0, 3, 12), (1, 0, 13), (1, 2, 14)):
        records.suffix_routes.append3(*values)
    records.finish_build()
    if incomplete:
        records.spans.truncate(59)
    cls = plan_type or FunctionStackMapPlan
    plan = cls("owned", 1, 32, "L_end", (), (), (), (), target, records)
    sink = object.__new__(emitter._NativeAArch64Emission)
    sink.fragments = AArch64EmissionFragments()
    sink.direct_records = CompilerIntArena()
    sink.direct_index = 0
    sink.barrier_depth = 0
    sink.structured_count = sink.direct_count = 0
    sink.move_count = sink.unscaled_count = sink.fragment_record_count = 0
    sink.events = []

    class Builder:
        def append_label(self, name):
            sink.events.append(("label", name))

        def append_encoded(self, word, fixup, symbol):
            sink.events.append(("word", word, fixup, symbol))

    sink.builder = Builder()
    return plan, sink


def _release_reload_case(plan, sink):
    plan.packed_records.close()
    sink.fragments.close()
    sink.direct_records.close()


def _record_sink_lines(monkeypatch):
    # Keep the actual fragment publisher and its counters. Only the unrelated
    # textual instruction producer/consumer is a bounded host fixture.
    monkeypatch.setattr(
        emitter._NativeAArch64Emission, "append",
        lambda sink, line: sink.events.append(("line", line)),
    )

    def extend(sink, lines):
        for line in lines:
            sink.append(line)

    monkeypatch.setattr(emitter._NativeAArch64Emission, "extend", extend)


@pytest.mark.parametrize("nonempty", [(), (11, 13), (10, 11, 12, 13, 14)])
def test_owned_empty_reload_fragments_preserve_output_and_reset(monkeypatch, nonempty):
    from pcc.backend.self_backend_precise_stackmaps import FunctionStackMapPlan

    class LegacyPlan(FunctionStackMapPlan):
        pass

    emit, _allocations = _counted_emitter(monkeypatch)
    _record_sink_lines(monkeypatch)
    outcomes = []
    for cls in (FunctionStackMapPlan, LegacyPlan):
        plan, sink = _owned_reload_case(nonempty=nonempty, plan_type=cls)
        try:
            assert emit(SimpleNamespace(name="owned"), Kernel(), plan, sink) == []
            events = list(sink.events)
            counters = (
                sink.structured_count, sink.direct_count, sink.move_count,
                sink.unscaled_count, sink.fragment_record_count,
            )
            spans = len(sink.fragments.spans.spans) // 2
            outcomes.append((events, counters, spans))
            assert sink.fragments.next_record_id() == -1
            assert len(sink.fragments.cursor) == 1
            generation = sink.fragments.spans.generation
            sink.release_captured_function()
            assert sink.fragments.spans.generation == generation + 1
            assert len(sink.fragments.spans.spans) == 0
            assert len(sink.fragments.cursor) == 0
            assert len(sink.fragments.records) == 0
            sink.events.clear()
            assert emit(SimpleNamespace(name="owned"), Kernel(), plan, sink) == []
            assert sink.events == events
            assert len(sink.fragments.spans.spans) // 2 == spans
            assert (
                sink.structured_count, sink.direct_count, sink.move_count,
                sink.unscaled_count, sink.fragment_record_count,
            ) == tuple(2 * count for count in counters)
        finally:
            _release_reload_case(plan, sink)
    assert outcomes[0][:2] == outcomes[1][:2]
    assert outcomes[0][2] == 5 + len(nonempty)
    assert outcomes[1][2] == 10
    first_result = outcomes[0][0].index(("line", "result_1"))
    assert outcomes[0][0][first_result - 3:first_result] == [
        ("line", "  bl helper"),
        ("label", "L_reload_10"),
        ("label", "L_reload_11"),
    ]
    if 10 in nonempty or 11 in nonempty:
        assert outcomes[0][0][first_result + 1][0] == "word"


@pytest.mark.parametrize("failure", ["subclass-hook", "incomplete", "wrong-target", "closed"])
def test_empty_reload_guard_retains_failure_fallback(monkeypatch, failure):
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_precise_stackmaps import FunctionStackMapPlan

    class HookedPlan(FunctionStackMapPlan):
        def append_packed_reload_span(self, owner, fragment, record):
            raise RuntimeError("reload hook preserved")

    emit, _allocations = _counted_emitter(monkeypatch)
    _record_sink_lines(monkeypatch)
    plan, sink = _owned_reload_case(
        plan_type=HookedPlan if failure == "subclass-hook" else None,
        incomplete=failure == "incomplete",
        target="x86_64-linux" if failure == "wrong-target" else "aarch64-darwin",
    )
    try:
        if failure == "closed":
            sink.fragments.close()
        error, message = {
            "subclass-hook": (RuntimeError, "reload hook preserved"),
            "incomplete": (IndexError, "list index out of range"),
            "wrong-target": (BackendUnavailable, "require AArch64"),
            "closed": (RuntimeError, "fragments are closed"),
        }[failure]
        with pytest.raises(error, match=message):
            emit(SimpleNamespace(name="failed"), Kernel(), plan, sink)
        if failure == "subclass-hook":
            # Both labels precede the original first reload callback.
            assert sink.events[-1] == ("line", "result_1")
            assert len(sink.fragments.spans.spans) // 2 == 3
        elif failure == "incomplete":
            # No speculative elision occurs when the scalar table is short.
            assert len(sink.fragments.spans.spans) // 2 == 10
        elif failure == "wrong-target":
            assert len(sink.fragments.spans.spans) // 2 == 1
    finally:
        _release_reload_case(plan, sink)


def test_empty_reload_owner_guards_are_outside_instruction_loops():
    tree = ast.parse(inspect.getsource(emitter._emit_dense_indexed_function_blocks))
    function = tree.body[0]
    guard_index = next(
        index for index, node in enumerate(function.body)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "type"
            for child in ast.walk(node.test)
        )
    )
    loop_index = next(
        index for index, node in enumerate(function.body)
        if isinstance(node, (ast.For, ast.While))
    )
    assert guard_index < loop_index
    for loop in function.body[loop_index:]:
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "type"
            for node in ast.walk(loop)
        )
