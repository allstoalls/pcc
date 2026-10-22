"""Finite source-extracted CFG/arena controls, never a compiler or native run.

The original full function is frozen separately. Only exact source-extracted
list arenas and scalar CFG methods execute. Loop-body spies count executed
word iterations, not collected pytest cases or predicted work.
"""

import ast
import copy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.managed_liveness_dirty_rows_reference import (
    reference_function,
    without_dirty_row_schedule,
)
from tests.python import test_managed_scratch_retirement as fixtures

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/backend/self_backend_precise_stackmaps.py"
ARENA = ROOT / "pcc/backend/self_backend_value_arena.py"
KERNEL = ROOT / "pcc/backend/self_backend_kernel.py"
CFG_METHODS = (
    "block_fact", "block_phi_fact", "phi_record", "phi_incoming",
    "instruction_fact_by_id", "instruction_metadata_by_id",
    "cfg_successor_count", "cfg_successor_id", "terminator_successor_count",
    "terminator_successor_id", "terminator_header", "terminator_span",
    "terminator_case", "inline_error_edge_span", "inline_error_edge_trigger",
    "inline_error_edge_target", "call_flags", "call_span",
    "publish_call_liveness_state_id",
)


def _function(original=False):
    if original:
        return reference_function()
    return copy.deepcopy(next(node for node in ast.parse(SOURCE.read_text()).body
                              if isinstance(node, ast.FunctionDef)
                              and node.name == "_native_managed_liveness"))


def _runtime(original=False, *, fault=None, instrument=True):
    events = SimpleNamespace(phase="setup", words=Counter(), rows=[], cfg=[],
                             labels={}, closes=[], closed_values={}, published=[], checkpoints=[],
                             callback_reads=[])

    def phase(value):
        events.phase = value
        if value == "backward":
            events.checkpoints.append({name: (arena._closed, arena._length)
                                       for name, arena in events.labels.items()})

    def label(arena, name):
        events.labels[name] = arena

    def tick(operation):
        events.words[events.phase, operation] += 1

    def row(row_start, count, changed, live_out, live_in):
        if events.phase == "fixedpoint":
            events.rows.append((row_start, count, changed,
                                tuple(live_out._values[row_start:row_start + count]),
                                tuple(live_in._values[row_start:row_start + count])))

    def cfg_result(operation, block, index, value):
        if events.phase == "fixedpoint":
            events.cfg.append((operation, block, index, value))
        return value

    def close(arena):
        name = next((name for name, owned in events.labels.items() if owned is arena), "other")
        events.closes.append((name, arena._closed, arena._length))
        if not arena._closed:
            events.closed_values[name] = tuple(arena._values)

    def alive(arena):
        assert not arena._closed, "arena read/write after close"

    def operation(arena, name):
        if fault is not None:
            label = next((key for key, owned in events.labels.items() if owned is arena), "other")
            fault("method:" + label + ":" + name, arena)

    def construct(name, factory, capacity):
        if fault is not None:
            fault("construct:" + name, None)
        return factory(capacity)

    def work_fault(name):
        if fault is not None and events.phase == "fixedpoint":
            fault("work:" + name, None)

    def host_malloc(*args):
        raise NotImplementedError("finite host arena")

    namespace = dict(valueclass=lambda cls: dataclass(frozen=True)(cls),
                     _fail=fixtures._fail, BackendUnavailable=ValueError,
                     PARSED_INSTRUCTION_KIND_CALL=1, PARSED_INSTRUCTION_KIND_BR=2,
                     PARSED_INSTRUCTION_KIND_BR_COND=3, PARSED_INSTRUCTION_KIND_SWITCH=4,
                     CALL_FLAG_FRAME_PROTOCOL=8, INLINE_ERROR_EDGE_WIDTH=8,
                     _phase=phase, _label=label, _tick=tick, _row=row,
                     _cfg_result=cfg_result, _close=close, _alive=alive,
                     _operation=operation, _construct=construct, _work_fault=work_fault)
    arena_tree = ast.parse(ARENA.read_text())
    namespace.update({alias.asname or alias.name: fixtures._forbidden
                      for node in arena_tree.body if isinstance(node, ast.ImportFrom)
                      for alias in node.names})
    namespace.update(malloc=host_malloc, _py_bytes_new=fixtures._forbidden)
    nodes = [copy.deepcopy(node) for node in arena_tree.body if isinstance(node, ast.ClassDef)
             and node.name in ("CompilerInt2", "CompilerInt3", "CompilerInt4", "CompilerIntArena")]
    arena = next(node for node in nodes if node.name == "CompilerIntArena")
    measured = {"copy_prefix_from_unchecked": "copy", "or_prefix_from_unchecked": "or",
                "converge_liveness_row_unchecked": "converge"}
    for method in arena.body:
        if not isinstance(method, ast.FunctionDef) or not instrument:
            continue
        if method.name in measured:
            # Insert INSIDE both actual word loops. Native bodies remain
            # forbidden; only host-list loop iterations reach these spies.
            for loop in ast.walk(method):
                if isinstance(loop, ast.While) and ast.unparse(loop.test) == "index < count":
                    loop.body.insert(0, ast.parse("_tick(" + repr(measured[method.name]) + ")").body[0])
        if method.name == "converge_liveness_row_unchecked":
            class ObserveReturn(ast.NodeTransformer):
                def visit_Return(self, node):
                    return [ast.parse("_row(row_start, count, changed, live_out, live_in)").body[0], node]
            ObserveReturn().visit(method)
        if method.name == "close":
            method.body.insert(0, ast.parse('_operation(self, "close")').body[0])
            method.body.insert(0, ast.parse("_close(self)").body[0])
        elif method.name not in ("__init__", "__del__"):
            method.body.insert(0, ast.parse("_operation(self, " + repr(method.name) + ")").body[0])
            method.body.insert(0, ast.parse("_alive(self)").body[0])
    fixtures._exec(nodes, namespace)
    kernel_tree = ast.parse(KERNEL.read_text())
    kernel_source = next(node for node in kernel_tree.body
                         if isinstance(node, ast.ClassDef) and node.name == "IndexedFunctionKernel")
    methods = [copy.deepcopy(node) for node in kernel_source.body
               if isinstance(node, ast.FunctionDef) and node.name in CFG_METHODS]
    for method in methods:
        if method.name not in ("cfg_successor_count", "cfg_successor_id") or not instrument:
            continue
        method.body.insert(0, ast.parse("_work_fault(" + repr(method.name) + ")").body[0])
        class ObserveCFG(ast.NodeTransformer):
            def visit_Return(self, node):
                index = ast.Name(id="successor_index", ctx=ast.Load()) if method.name.endswith("_id") else ast.Constant(-1)
                node.value = ast.Call(func=ast.Name(id="_cfg_result", ctx=ast.Load()),
                                      args=[ast.Constant(method.name), ast.Name(id="block_id", ctx=ast.Load()),
                                            index, node.value], keywords=[])
                return node
        ObserveCFG().visit(method)
    kernel = copy.deepcopy(kernel_source)
    kernel.body = [node for node in kernel.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__slots__" for target in node.targets)] + methods
    plain = copy.deepcopy(next(node for node in kernel_tree.body if isinstance(node, ast.FunctionDef)
                               and node.name == "_scalar_window_is_complete"))
    packed = copy.deepcopy(next(node for node in ast.parse(SOURCE.read_text()).body
                                if isinstance(node, ast.ClassDef) and node.name == "PackedManagedLiveness"))
    native = _function(original)

    class ObserveFunction(ast.NodeTransformer):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "CompilerIntArena":
                    if name in ("row_state", "reverse_edges"):
                        node.value = ast.Call(func=ast.Name(id="_construct", ctx=ast.Load()),
                                              args=[ast.Constant(name), node.value.func, *node.value.args], keywords=[])
                    return [node, ast.parse("_label(" + name + ", " + repr(name) + ")").body[0]]
                if name == "changed" and isinstance(node.value, ast.Constant) and node.value.value is True:
                    # Top-level initialization only; changed=True in transfer
                    # bodies is visited too but has identical phase already.
                    return [ast.parse('_phase("fixedpoint")').body[0], node]
            return node

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name) and node.target.id == "result":
                return [ast.parse('_phase("backward")').body[0], node]
            return node

    if instrument:
        ObserveFunction().visit(native)
    fixtures._exec([kernel, plain, packed, native], namespace)
    return namespace, events


def _make_kernel(namespace, blocks, value_count):
    # Reuse the existing finite source fixture's shape builder, then populate
    # the actual scalar tables consumed by the extracted production methods.
    built = fixtures.Kernel(namespace, blocks, value_count)
    kernel = namespace["IndexedFunctionKernel"]()
    kernel.value_names, kernel.block_names = built.value_names, built.block_names

    def arena(values):
        result = namespace["CompilerIntArena"]()
        for value in values:
            result.append(value)
        return result

    def cells(records, names):
        return [getattr(record, name) for record in records for name in names]

    four = ("first", "second", "third", "fourth")
    two = ("first", "second")
    kernel.block_facts = arena(cells(built.block_records, four))
    kernel.block_phi_facts = arena(cells(built.phi_spans, two))
    kernel.phi_scalars = arena(cells(built.phis, four))
    kernel.phi_incoming_scalars = arena(cells(built.incoming, two))
    kernel.instruction_facts = arena(cells(built.facts, four))
    kernel.instruction_metadata = arena(cells(built.metadata, four))
    kernel.instruction_overflow_use_ids = built.instruction_overflow_use_ids
    kernel.terminator_scalars = arena([value for pair in zip(built.term_headers, built.term_spans)
                                      for record in pair for value in cells([record], four)])
    kernel.terminator_case_scalars = arena(cells(built.cases, two))
    kernel.error_edge_spans = arena(cells(built.edge_spans, two))
    kernel.error_edge_scalars = arena([value for trigger, target in built.errors
                                      for value in (0, trigger, -1, target, 0, 0, -1, 0)])
    kernel.call_scalars = arena([value for call in built.call_states
                                for value in (0, 0, built.flags[call], 0, 0, 0, -1, 0)])
    return kernel, len(built.call_states)


def _run(blocks, value_count, *, original=False, variant=None, tracked_count=None, fault=None):
    namespace, events = _runtime(original, fault=fault)
    kernel, call_count = _make_kernel(namespace, blocks, value_count)
    if variant is not None:
        kernel = variant(namespace, events, kernel)
    if tracked_count is None:
        tracked_count = value_count
    origins = SimpleNamespace(is_tracked=lambda value: value < tracked_count)
    try:
        result = namespace["_native_managed_liveness"](SimpleNamespace(name="finite"), origins, kernel)
    except BaseException as error:
        return SimpleNamespace(outcome=(type(error), error.args), events=events, kernel=kernel, result=None, error=error)
    states = tuple(result.states._values)
    overflow = tuple(result.overflow_ids._values)
    call_states = tuple(kernel.call_span(call).fourth for call in range(call_count))
    return SimpleNamespace(outcome=(states, overflow, call_states), events=events, kernel=kernel, result=result)


def _shape(name, count):
    use = list(range(count))
    call = (-1, [], True, 0)
    consume = (-1, use, False, 0)
    if name == "empty":
        return []
    if name == "one-pass":
        return [dict(instructions=[call]) for _ in range(6)]
    if name == "isolated":
        return [dict(instructions=[call, consume]) for _ in range(6)]
    if name == "forward":
        return [dict(instructions=[call], edges=[index + 1]) for index in range(7)] + [dict(instructions=[call, consume])]
    if name == "reverse":
        return [dict(instructions=[call, consume])] + [dict(instructions=[call], edges=[index - 1]) for index in range(1, 8)]
    if name == "cycle":
        return [dict(instructions=[call, consume], edges=[3]), dict(instructions=[call], edges=[0]),
                dict(instructions=[call], edges=[1]), dict(instructions=[call], edges=[2])]
    if name == "self-loop":
        return [dict(instructions=[call, consume], edges=[0])]
    if name == "duplicate":
        return [dict(instructions=[call], edges=[1, 1, 1]), dict(instructions=[call, consume])]
    if name == "diamond":
        return [dict(instructions=[call], edges=[1, 2]), dict(instructions=[call], edges=[3]),
                dict(instructions=[call], edges=[3]), dict(instructions=[call, consume])]
    if name == "inline-error":
        return [dict(instructions=[call, call], errors=[(0, 2)]), dict(instructions=[call], edges=[0]),
                dict(instructions=[call, consume])]
    if name == "phi":
        return fixtures._phi_blocks(max(2, count - 1), False)
    if name == "phi-cycle":
        return fixtures._phi_blocks(max(2, count - 1), True)
    raise AssertionError(name)


SHAPES = ("empty", "one-pass", "isolated", "forward", "reverse", "cycle",
          "self-loop", "duplicate", "diamond", "inline-error", "phi", "phi-cycle")


def test_dirty_rows_source_keeps_frozen_fallback_and_unchanged_boundaries():
    from tests.managed_liveness_dirty_rows_reference import without_plane_fold
    old, new = reference_function(), without_plane_fold(_function())
    assert ast.dump(without_dirty_row_schedule(new)) == ast.dump(old)
    branch = next(node for node in new.body if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "dirty_rows_enabled")
    row_owner = next(node for node in branch.body if isinstance(node, ast.Try))
    edge_owner = next(node for node in row_owner.body if isinstance(node, ast.Try))
    fast = copy.deepcopy(next(node for node in edge_owner.body if isinstance(node, ast.While)))
    # Remove only the scheduler's explicit insertions from the fast transfer;
    # the remaining first sweep must be the exact original AST.
    fast.body.pop()  # first-sweep seeding after all original blocks
    block_loop = fast.body[2]
    assert isinstance(block_loop, ast.While)
    assert ast.unparse(block_loop.body[0]) == "row_offset = block_id * row_width"
    assert ast.unparse(block_loop.body[1].test) == "not first_sweep"
    del block_loop.body[:2]
    successor_loop = next(node for node in block_loop.body if isinstance(node, ast.While))
    assert ast.unparse(successor_loop.body[-2].test) == "first_sweep"
    del successor_loop.body[-2]
    converge = next(node for node in block_loop.body if isinstance(node, ast.If))
    assert ast.unparse(converge.body[-1].test) == "first_sweep"
    del converge.body[-1]
    assert ast.dump(fast) == ast.dump(branch.orelse[0])
    close_names = [ast.unparse(node.value.func.value) for node in ast.walk(branch)
                   if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "close"]
    assert Counter(close_names) == {"row_state": 2, "reverse_edges": 2}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("count", (0, 1, 65))
def test_dirty_rows_complete_results_and_first_sweep_match_frozen(shape, count):
    # PHI fixtures own destinations in addition to the chosen tracked width.
    value_count = max(3, count) if shape.startswith("phi") else count
    blocks = _shape(shape, value_count)
    old = _run(blocks, value_count, original=True, tracked_count=count)
    new = _run(blocks, value_count, tracked_count=count)
    assert old.result is not None and new.result is not None
    assert new.outcome == old.outcome
    for matrix in ("definitions", "live_out"):
        assert new.events.closed_values[matrix] == old.events.closed_values[matrix]
    incoming = "uses" if count else "live_in"
    assert new.events.closed_values[incoming] == old.events.closed_values["live_in"]
    if not count:
        assert new.events.rows[:len(blocks)] == old.events.rows[:len(blocks)]
    else:
        # Seeding all live-in rows earlier may propagate more bits in the
        # first sweep; it must remain bounded by the exact final fixed point.
        final_in = old.events.closed_values["live_in"]
        final_out = old.events.closed_values["live_out"]
        for offset, width, changed, outgoing, incoming_row in new.events.rows:
            assert all(word & ~final_in[offset + i] == 0 for i, word in enumerate(incoming_row))
            assert all(word & ~final_out[offset + i] == 0 for i, word in enumerate(outgoing))
    first_cfg = sum(1 + len(block.get("edges", [])) + len(block.get("errors", [])) for block in blocks)
    assert new.events.cfg[:first_cfg] == old.events.cfg[:first_cfg]
    for operation in ("copy", "or", "converge"):
        assert new.events.words["fixedpoint", operation] <= old.events.words["fixedpoint", operation]
    assert new.events.words["backward", "or"] == old.events.words["backward", "or"]
    if count:
        assert "row_state" in new.events.labels and "reverse_edges" in new.events.labels
    else:
        assert "row_state" not in new.events.labels


@pytest.mark.parametrize("shape", ("one-pass", "isolated", "forward", "reverse", "cycle", "inline-error"))
def test_dirty_rows_counts_executed_words_and_conservative_overhead(shape):
    blocks = _shape(shape, 65)
    old, new = _run(blocks, 65, original=True), _run(blocks, 65)
    assert new.outcome == old.outcome
    old_words = sum(old.events.words["fixedpoint", operation] for operation in ("copy", "or", "converge"))
    new_words = sum(new.events.words["fixedpoint", operation] for operation in ("copy", "or", "converge"))
    if shape == "one-pass":
        assert old_words == new_words == 2 * len(blocks) * 3
    else:
        assert new_words < old_words
    assert new.events.labels["row_state"]._closed
    assert new.events.labels["reverse_edges"]._closed
    edge_count = sum(len(block.get("edges", [])) + len(block.get("errors", [])) for block in blocks)
    released = {name: length for name, already_closed, length in new.events.closes
                if not already_closed}
    assert released["row_state"] == 2 * len(blocks)
    assert released["reverse_edges"] == 2 * edge_count


def _fallback(name):
    def change(namespace, events, kernel):
        base = namespace["CompilerIntArena"]
        if name == "kernel-subclass":
            derived = type("KernelSubclass", (type(kernel),), {})
            other = derived()
            for slot in type(kernel).__slots__:
                if hasattr(kernel, slot):
                    setattr(other, slot, getattr(kernel, slot))
            return other
        arena = kernel.terminator_scalars
        if name == "arena-subclass":
            derived = type("ArenaSubclass", (base,), {})
            other = derived()
            for slot in base.__slots__:
                setattr(other, slot, getattr(arena, slot))
            kernel.terminator_scalars = other
        elif name in ("list-subclass", "mutable-callback"):
            class Values(list):
                def __getitem__(self, index):
                    events.callback_reads.append(index)
                    if name == "mutable-callback" and index == 0:
                        # A callback changes an unrelated edge while reads
                        # are in flight. Fallback must retain the same order.
                        self[19] = 0
                    return super().__getitem__(index)
            arena._values = Values(arena._values)
        elif name == "int-subclass":
            class Kind(int):
                def __eq__(self, other):
                    events.callback_reads.append((int(self), other))
                    return super().__eq__(other)
            arena._values[0] = Kind(arena._values[0])
        elif name == "bool-payload":
            arena._values[0] = True
        elif name == "length-subclass":
            arena._length = type("Length", (int,), {})(arena._length)
        elif name == "address-subclass":
            arena._address = type("Address", (int,), {})(0)
        elif name == "capacity-subclass":
            arena._capacity = type("Capacity", (int,), {})(0)
        elif name == "missing-unused-arena":
            del kernel.terminator_case_scalars
        elif name == "inconsistent-length":
            arena._length += 1
        elif name == "closed-unused-arena":
            kernel.terminator_case_scalars.close()
        else:
            raise AssertionError(name)
        return kernel
    return change


@pytest.mark.parametrize("variant", ("kernel-subclass", "arena-subclass", "list-subclass",
                                     "mutable-callback", "int-subclass", "bool-payload",
                                     "length-subclass", "address-subclass", "capacity-subclass",
                                     "missing-unused-arena", "inconsistent-length", "closed-unused-arena"))
def test_dirty_rows_noncanonical_cfg_uses_identical_original_fallback(variant):
    blocks = _shape("reverse", 1)
    old = _run(blocks, 1, original=True, variant=_fallback(variant))
    new = _run(blocks, 1, variant=_fallback(variant))
    assert old.result is not None and new.result is not None
    assert new.outcome == old.outcome
    assert new.events.rows == old.events.rows
    assert new.events.cfg == old.events.cfg
    assert new.events.callback_reads == old.events.callback_reads
    assert new.events.words == old.events.words
    assert "row_state" not in new.events.labels


@pytest.mark.parametrize("kind", ("negative", "oversized", "descending-first", "inline-error", "short-table"))
@pytest.mark.parametrize("count", (0, 1))
def test_dirty_rows_malformed_first_errors_and_zero_width_validation(kind, count):
    blocks = [dict(edges=[-1])] if kind == "negative" else [dict(edges=[9])]
    variant = None
    if kind == "descending-first":
        blocks = [dict(edges=[-1]), dict(edges=[9])]
    if kind == "inline-error":
        blocks = [dict(instructions=[(-1, [], True, 0)], errors=[(0, 9)])]
    if kind == "short-table":
        blocks = [dict(), dict()]
        def variant(namespace, events, kernel):
            del kernel.terminator_scalars._values[-5:]
            kernel.terminator_scalars._length -= 5
            return kernel
    old = _run(blocks, count, original=True, variant=variant)
    new = _run(blocks, count, variant=variant)
    assert old.result is None and new.result is None
    assert new.outcome == old.outcome
    assert new.events.rows == old.events.rows
    assert new.events.cfg == old.events.cfg
    assert new.events.checkpoints == old.events.checkpoints == []
    # New scratch has its own all-path cleanup. The old matrices retain
    # their original exceptional-path lifetime and retirement boundary.
    assert not new.events.labels["uses"]._closed
    if count:
        assert new.events.labels["row_state"]._closed
        assert new.events.labels["reverse_edges"]._closed
    else:
        assert "row_state" not in new.events.labels


def test_dirty_rows_scratch_retires_before_backward_scan_and_result_is_independent():
    result = _run(_shape("reverse", 65), 65)
    assert result.result is not None
    checkpoint = result.events.checkpoints[0]
    for name in ("row_state", "reverse_edges", "definitions", "live_out"):
        assert checkpoint[name] == (True, 0)
    assert checkpoint["uses"] == (False, 24)  # Shared live-in retained for backward scan.
    for name in ("tracked_values", "tracked_index", "uses", "scratch"):
        assert checkpoint[name][0] is False
    # Source-extracted arena methods reject every read/write after close.
    # Results own separate tables and remain available after all scratch dies.
    assert all(arena._closed for arena in result.events.labels.values())
    assert tuple(result.result.states._values) == result.outcome[0]
    assert tuple(result.result.overflow_ids._values) == result.outcome[1]
    assert not result.result.states._closed and not result.result.overflow_ids._closed


def test_dirty_rows_reverse_links_cover_every_duplicate_and_error_edge():
    blocks = [dict(instructions=[(-1, [], True, 0)], edges=[1, 1, 2], errors=[(0, 2)]),
              dict(instructions=[(-1, [0], False, 0)], edges=[2]),
              dict(instructions=[(-1, [], True, 0)], edges=[2, 0])]
    result = _run(blocks, 1)
    assert result.result is not None
    rows = result.events.closed_values["row_state"]
    links = result.events.closed_values["reverse_edges"]
    actual = []
    for successor in range(len(blocks)):
        head = rows[successor * 2]
        seen = set()
        while head:
            assert head not in seen
            seen.add(head)
            assert head % 2 == 1 and 0 <= head - 1 < len(links) - 1
            actual.append((links[head - 1], successor))
            head = links[head]
    expected = [(predecessor, successor) for predecessor, block in enumerate(blocks)
                for successor in block.get("edges", []) + [target for _, target in block.get("errors", [])]]
    assert sorted(actual) == sorted(expected)


def test_dirty_rows_callback_failure_is_not_probed_before_original_read():
    def change(namespace, events, kernel):
        class Values(list):
            def __getitem__(self, index):
                events.callback_reads.append(index)
                if index == 8:
                    raise RuntimeError("original CFG read failed")
                return super().__getitem__(index)
        kernel.terminator_scalars._values = Values(kernel.terminator_scalars._values)
        return kernel
    blocks = [dict(edges=[-1]), dict(edges=[0])]
    old = _run(blocks, 1, original=True, variant=change)
    new = _run(blocks, 1, variant=change)
    assert new.result is old.result is None
    assert new.outcome == old.outcome == (RuntimeError, ("original CFG read failed",))
    assert new.events.callback_reads == old.events.callback_reads
    assert new.events.cfg == old.events.cfg
    assert "row_state" not in new.events.labels


@pytest.mark.parametrize("site", ("construct:row_state", "method:row_state:append_zeros",
                                  "construct:reverse_edges", "method:reverse_edges:append2",
                                  "work:cfg_successor_count", "method:scratch:converge_liveness_row_unchecked"))
@pytest.mark.parametrize("exception_type", (RuntimeError, BaseException))
@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_dirty_rows_exception_cleanup_attempts_every_constructed_arena(site, exception_type, cleanup_fails):
    business = exception_type("original business failure")
    cleanup = BaseException("cleanup failure")
    fired = []

    def fault(operation, arena):
        if operation == site and not fired:
            fired.append(operation)
            raise business
        if cleanup_fails and fired and operation in ("method:row_state:close", "method:reverse_edges:close"):
            raise cleanup

    result = _run(_shape("reverse", 1), 1, fault=fault)
    assert fired == [site]
    assert result.result is None and result.error is business
    expected = [] if site == "construct:row_state" else ["row_state"]
    if site not in ("construct:row_state", "method:row_state:append_zeros", "construct:reverse_edges"):
        expected = ["reverse_edges", "row_state"]
    attempted = [name for name, _, _ in result.events.closes if name in ("row_state", "reverse_edges")]
    assert attempted == expected
    for name in expected:
        assert result.events.labels[name]._closed == (not cleanup_fails)
    # No original arena is retired merely because the new scheduler failed.
    assert not result.events.labels["uses"]._closed
    assert not result.events.labels["definitions"]._closed
    assert not result.events.labels["live_out"]._closed


@pytest.mark.parametrize("which", ("row_state", "reverse_edges", "both"))
def test_dirty_rows_success_cleanup_failure_still_attempts_other_owned_arena(which):
    row_error, edge_error = BaseException("row close"), BaseException("edge close")

    def fault(operation, arena):
        if which in ("row_state", "both") and operation == "method:row_state:close":
            raise row_error
        if which in ("reverse_edges", "both") and operation == "method:reverse_edges:close":
            raise edge_error

    result = _run(_shape("one-pass", 1), 1, fault=fault)
    assert result.result is None
    assert result.error is (row_error if which == "row_state" else edge_error)
    assert [name for name, _, _ in result.events.closes if name in ("row_state", "reverse_edges")] == ["reverse_edges", "row_state"]
    assert not result.events.labels["uses"]._closed
