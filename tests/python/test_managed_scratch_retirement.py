"""Finite list-arena lifetime controls; no PCC imports, parser, emitter or FFI."""

import ast
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/backend/self_backend_precise_stackmaps.py"
ARENA = ROOT / "pcc/backend/self_backend_value_arena.py"
REFERENCE = ROOT / "tests/managed_scratch_retirement_reference.json"
REFERENCE_SHA256 = "0da655293c4885418a56b993da5e4107c4a7614800fb60c9deb9d1a267634647"
MATRICES = ("uses", "definitions", "live_out")
RETAINED = ("tracked_values", "tracked_index", "live_in", "scratch")


def _function(name, original=False):
    if original:
        return ast.parse(json.loads(REFERENCE.read_text())["functions"][name]).body[0]
    return copy.deepcopy(next(node for node in ast.parse(SOURCE.read_text()).body
                              if isinstance(node, ast.FunctionDef) and node.name == name))


def _exec(nodes, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<finite-lifetime-source>", "exec"), namespace)


def _forbidden(*args, **kwargs):
    raise AssertionError("native operation is forbidden in finite host controls")


def _fail(func, message):
    raise ValueError(message)


def _namespace(*, original=False, checkpoint=False):
    namespace = dict(valueclass=lambda cls: dataclass(frozen=True)(cls), _fail=_fail,
                     BackendUnavailable=ValueError, IndexedFunctionKernel=type("ExactKernel", (), {}),
                     PARSED_INSTRUCTION_KIND_CALL=1, PARSED_INSTRUCTION_KIND_BR=2,
                     PARSED_INSTRUCTION_KIND_BR_COND=3, PARSED_INSTRUCTION_KIND_SWITCH=4,
                     CALL_FLAG_FRAME_PROTOCOL=8, TYPE_KIND_PTR=3, POINTER_SIZE=8,
                     NO_OFFSET=-(1 << 62), get_indexed_function_kernel=lambda func: func.kernel)
    arena_tree = ast.parse(ARENA.read_text())
    classes = [copy.deepcopy(node) for node in arena_tree.body if isinstance(node, ast.ClassDef)
               and node.name in ("CompilerInt2", "CompilerInt3", "CompilerInt4", "CompilerIntArena")]
    namespace.update({(node.asname or node.name): _forbidden for item in arena_tree.body if isinstance(item, ast.ImportFrom)
                      for node in item.names})
    namespace["_py_bytes_new"] = _forbidden
    _exec(classes, namespace)
    base = namespace["CompilerIntArena"]

    class PoisonList(list):
        def __init__(self, owner):
            self.owner = owner

        def check(self):
            if self.owner._closed:
                raise AssertionError("read/write after close: " + self.owner.label)

        def __getitem__(self, index):
            self.check()
            return super().__getitem__(index)

        def __setitem__(self, index, value):
            self.check()
            return super().__setitem__(index, value)

        def append(self, value):
            self.check()
            return super().append(value)

    class TrackingArena(base):
        instances = []
        events = []

        def __init__(self, initial_capacity=8):
            assert initial_capacity >= 0
            self._address = 0
            self._length = 0
            self._capacity = 0
            self._closed = False
            self._values = PoisonList(self)
            self.label = "arena" + str(len(self.instances))
            self.close_calls = 0
            self.releases = 0
            self.released_slots = 0
            self.instances.append(self)

        def __len__(self):
            self._values.check()
            return base.__len__(self)

        def close(self):
            self.close_calls += 1
            if not self._closed:
                self.releases += 1
                self.released_slots = self._length
                self.events.append(("close", self.label, self._length))
            base.close(self)

    namespace["CompilerIntArena"] = TrackingArena
    stackmap_tree = ast.parse(SOURCE.read_text())
    constants = [copy.deepcopy(node) for node in stackmap_tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id.startswith(("_ORIGIN_", "_TRANSFER_"))
                         for target in node.targets)]
    classes = [copy.deepcopy(node) for node in stackmap_tree.body if isinstance(node, ast.ClassDef)
               and node.name in ("PackedManagedLiveness", "PackedPointerAliases",
                                 "PackedManagedOrigins", "PackedReloadScratch")]
    _exec([*constants, *classes], namespace)
    packed = namespace["PackedManagedLiveness"]
    snapshots = []

    class CheckedPacked(packed):
        def __init__(self):
            labelled = {arena.label: arena for arena in TrackingArena.instances}
            if checkpoint and "uses" in labelled:
                snapshot = {name: (labelled[name]._closed, labelled[name]._length)
                            for name in (*MATRICES, *RETAINED)}
                snapshots.append(snapshot)
                assert all(snapshot[name][0] == (not original) for name in MATRICES)
                assert all(not snapshot[name][0] for name in RETAINED)
            super().__init__()

    namespace["PackedManagedLiveness"] = CheckedPacked
    native = _function("_native_managed_liveness", original)

    class Label(ast.NodeTransformer):
        def visit_Assign(self, node):
            if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "CompilerIntArena"):
                name = node.targets[0].id
                return [node, ast.parse(name + ".label = " + repr(name)).body[0]]
            return node

    native = Label().visit(native)
    _exec([native, _function("_managed_live_after", True),
           _function("_build_function_stack_map_plan_native", original),
           _function("_native_plan_reloads")], namespace)
    namespace.update(TrackingArena=TrackingArena, checkpoints=snapshots)
    return namespace


class Kernel:
    def __init__(self, namespace, blocks, value_count):
        self.ns = namespace
        self.value_names = ["v" + str(index) for index in range(value_count)]
        self.block_names = ["b" + str(index) for index in range(len(blocks))]
        self.blocks = blocks
        self.instructions, self.facts, self.metadata = [], [], []
        self.block_records, self.phi_spans, self.phis, self.incoming = [], [], [], []
        self.term_headers, self.term_spans, self.cases = [], [], []
        self.edge_spans, self.errors, self.call_states, self.flags = [], [], {}, {}
        self.instruction_overflow_use_ids = namespace["CompilerIntArena"]()
        R2, R4 = namespace["CompilerInt2"], namespace["CompilerInt4"]
        for block in blocks:
            instructions = block.get("instructions", [])
            term = block.get("term", -1)
            self.block_records.append(R4(len(self.facts), len(instructions), int(term >= 0), term))
            self.phi_spans.append(R2(len(self.phis), len(block.get("phis", []))))
            for destination, incoming in block.get("phis", []):
                self.phis.append(R4(destination, 0, len(self.incoming), len(incoming)))
                self.incoming.extend(R2(value, predecessor) for value, predecessor in incoming)
            for destination, used, call, flags in instructions:
                first = used[0] if used else -1
                second = -1
                if len(used) == 2:
                    second = used[1]
                elif len(used) > 2:
                    second = -len(self.instruction_overflow_use_ids) - 2
                    for value in used[1:]:
                        self.instruction_overflow_use_ids.append(value)
                self.facts.append(R4(destination, len(used), first, second))
                call_id = len(self.call_states) if call else -1
                if call:
                    self.call_states[call_id] = 0
                    self.flags[call_id] = flags
                self.metadata.append(R4(1 if call else 0, call_id, 0, 0))
            edges = block.get("edges", [])
            kind = 0 if not edges else 2 if len(edges) == 1 else 3 if len(edges) == 2 else 4
            self.term_headers.append(R4(kind, 0, 0, edges[0] if edges else -1))
            self.term_spans.append(R4(edges[1] if len(edges) > 1 else -1, len(self.cases), max(0, len(edges) - 1), 0))
            self.cases.extend(R2(0, target) for target in edges[1:])
            errors = block.get("errors", [])
            self.edge_spans.append(R2(len(self.errors), len(errors)))
            self.errors.extend(errors)
        self.phi_reads = 0

    def block_fact(self, block): return self.block_records[block]
    def block_phi_fact(self, block): return self.phi_spans[block]
    def phi_record(self, index): return self.phis[index]
    def instruction_fact_by_id(self, index): return self.facts[index]
    def instruction_metadata_by_id(self, index): return self.metadata[index]
    def terminator_header(self, block): return self.term_headers[block]
    def terminator_span(self, block): return self.term_spans[block]
    def terminator_case(self, index): return self.cases[index]
    def terminator_successor_count(self, block): return len(self.blocks[block].get("edges", []))
    def terminator_successor_id(self, block, index): return self.blocks[block].get("edges", [])[index]
    def cfg_successor_count(self, block): return self.terminator_successor_count(block) + len(self.blocks[block].get("errors", []))
    def cfg_successor_id(self, block, index):
        edges = self.blocks[block].get("edges", [])
        return edges[index] if index < len(edges) else self.blocks[block].get("errors", [])[index - len(edges)][1]
    def inline_error_edge_span(self, block): return self.edge_spans[block]
    def inline_error_edge_trigger(self, edge): return self.errors[edge][0]
    def inline_error_edge_target(self, edge): return self.errors[edge][1]
    def call_flags(self, call): return self.flags[call]
    def call_span(self, call): return self.ns["CompilerInt4"](0, 0, 0, self.call_states[call])
    def publish_call_liveness_state_id(self, call, state): self.call_states[call] = state
    def value_name(self, value): return self.value_names[value]
    def value_slot_id(self, value): return value
    def slot_offset(self, slot): return 8 * (slot + 1)
    def slot_type_id(self, slot): return 0
    def type_header(self, type_id): return self.ns["CompilerInt4"](3, 0, 0, -1)
    def phi_incoming(self, index):
        self.phi_reads += 1
        return self.incoming[index]


def _phi_blocks(fanin, loop):
    join = fanin + 1
    repeat = join + 1
    exit_block = join + 2 if loop else join + 1
    blocks = [dict(edges=list(range(1, fanin + 1)))]
    for index in range(fanin):
        blocks.append(dict(instructions=[(index, [], True, 0), (-1, [], True, 0)], edges=[join]))
    incoming = [(index, index + 1) for index in range(fanin)]
    if loop:
        incoming.append((fanin, repeat))
    blocks.append(dict(instructions=[(-1, [], True, 0)], phis=[(fanin, incoming)],
                       edges=[repeat, exit_block] if loop else [exit_block]))
    if loop:
        blocks.append(dict(instructions=[(-1, [], True, 0)], edges=[join]))
    blocks.append(dict(instructions=[(-1, [], True, 0)], term=fanin))
    return blocks


def _decode(kernel, packed):
    decoded = []
    for call, state in kernel.call_states.items():
        row = packed.record(state)
        values = [] if row.first == 0 else [row.second]
        if row.first == 2:
            values.append(row.third)
        elif row.first > 2:
            start = -row.third - 2
            values.extend(packed.overflow_ids.get_unchecked(start + index) for index in range(row.first - 1))
        decoded.append((call, tuple(values)))
    return decoded


def _run_liveness(blocks, value_count, tracked, *, original=False, set_oracle=False):
    namespace = _namespace(original=original, checkpoint=not set_oracle)
    kernel = Kernel(namespace, blocks, value_count)
    func = SimpleNamespace(kernel=kernel, name="finite", frame_size=1024)
    if set_oracle:
        result = namespace["_managed_live_after"](func, frozenset(tracked))
    else:
        origins = namespace["PackedManagedOrigins"](value_count)
        for value in tracked:
            origins.set_state(value, namespace["_ORIGIN_MANAGED"], -8, 0)
        result = namespace["_native_managed_liveness"](func, origins, kernel)
    decoded = _decode(kernel, result)
    assert not result.states._closed and not result.overflow_ids._closed
    labelled = {arena.label: arena for arena in namespace["TrackingArena"].instances}
    if not set_oracle:
        for name in (*MATRICES, *RETAINED, "current_live_state"):
            assert labelled[name]._closed and labelled[name].releases == 1
        assert all(labelled[name].close_calls == (1 if original else 2) for name in MATRICES)
    return decoded, namespace, kernel, result


def _require_reference_identity(data):
    # The maintained JSON oracle may be checked out with Windows CRLF. Only
    # exact CRLF byte pairs canonicalize; execution/source receipts stay raw.
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == REFERENCE_SHA256


def test_frozen_lifetime_oracle_and_only_seven_close_insertions():
    raw = REFERENCE.read_bytes()
    canonical = raw.replace(b"\r\n", b"\n")
    _require_reference_identity(raw)
    _require_reference_identity(canonical)
    _require_reference_identity(canonical.replace(b"\n", b"\r\n"))
    changed_body = canonical.replace(b"block_id += 1", b"block_id += 2", 1)
    lone_cr = canonical.replace(b"\n", b"\r", 1)
    assert changed_body != canonical and lone_cr != canonical
    for changed in (changed_body, lone_cr, canonical + b" "):
        with pytest.raises(AssertionError):
            _require_reference_identity(changed)
    counts = {"_native_managed_liveness": 3, "_build_function_stack_map_plan_native": 4}
    for name, added in counts.items():
        old, new = _function(name, True), _function(name)
        if name == "_native_managed_liveness":
            from tests.managed_liveness_dirty_rows_reference import without_dirty_row_schedule
            from tests.managed_liveness_dirty_rows_reference import without_plane_fold
            new = without_dirty_row_schedule(without_plane_fold(new))
        else:
            from tests.stackmap_global_index_reference import without_shared_globals_index
            new = without_shared_globals_index(new)
        old_calls = [ast.unparse(node) for node in ast.walk(old) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "close"]
        new_calls = [ast.unparse(node) for node in ast.walk(new) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "close"]
        assert len(new_calls) == len(old_calls) + added
        early = {"uses.close()", "definitions.close()", "live_out.close()"} if added == 3 else {
            "aliases.close()", "origins.transfers.close()", "origins.transfer_ids.close()", "origins.proposal.close()"}
        if name == "_build_function_stack_map_plan_native":
            early.add("roots.retire_construction_state()")
        removed = set()
        body = []
        for node in new.body:
            expression = ast.unparse(node)
            if expression in early and expression not in removed:
                removed.add(expression)
            else:
                body.append(node)
        new.body = body
        assert ast.dump(new, include_attributes=False) == ast.dump(old, include_attributes=False)


@pytest.mark.parametrize("fanin", (2, 9, 65))
@pytest.mark.parametrize("loop", (False, True))
@pytest.mark.parametrize("selection", ("all", "alternating", "none"))
def test_finite_phi_liveness_matches_set_and_frozen_oracles(fanin, loop, selection):
    tracked = list(range(fanin + 1))
    if selection == "alternating": tracked = tracked[::2]
    if selection == "none": tracked = []
    blocks = _phi_blocks(fanin, loop)
    old = _run_liveness(blocks, fanin + 1, tracked, original=True)
    new = _run_liveness(blocks, fanin + 1, tracked)
    oracle = _run_liveness(blocks, fanin + 1, tracked, set_oracle=True)
    assert new[0] == old[0] == oracle[0]
    assert new[2].phi_reads == old[2].phi_reads
    assert new[2].phi_reads <= (fanin + int(loop)) * (fanin + int(loop) + 1)
    before, after = old[1]["checkpoints"][0], new[1]["checkpoints"][0]
    assert sum(before[name][1] - after[name][1] for name in MATRICES) == 3 * len(blocks) * ((len(tracked) + 29) // 30)


@pytest.mark.parametrize("shape", ("empty", "overflow", "word-boundary", "inline-error", "frame-call"))
def test_retained_live_in_and_overflow_states_remain_readable(shape):
    count = 65 if shape == "word-boundary" else 4
    if shape == "empty":
        blocks, tracked = [], []
    elif shape == "inline-error":
        blocks = [dict(instructions=[(-1, [], True, 0), (-1, [], True, 0)], errors=[(0, 1)]),
                  dict(instructions=[(-1, [], True, 0), (-1, list(range(count)), False, 0)])]
        tracked = list(range(count))
    else:
        blocks = [dict(instructions=[(-1, [], True, 8 if shape == "frame-call" else 0),
                                      (-1, list(range(count)), False, 0)])]
        tracked = list(range(count))
    old = _run_liveness(blocks, count, tracked, original=True)
    new = _run_liveness(blocks, count, tracked)
    assert new[0] == old[0]
    if shape in ("overflow", "word-boundary"):
        assert new[0] == [(0, tuple(range(count)))]
        assert len(new[3].overflow_ids) == count - 1
    if shape == "frame-call": assert new[0] == [(0, ())]


def test_fixed_point_failure_keeps_original_error_and_retirement_boundary():
    outcomes = []
    for original in (True, False):
        namespace = _namespace(original=original, checkpoint=True)
        kernel = Kernel(namespace, [dict(edges=[999])], 1)
        origins = namespace["PackedManagedOrigins"](1)
        origins.set_state(0, namespace["_ORIGIN_MANAGED"])
        with pytest.raises(ValueError, match="unknown CFG successor id 999") as error:
            namespace["_native_managed_liveness"](SimpleNamespace(name="bad"), origins, kernel)
        labelled = {arena.label: arena for arena in namespace["TrackingArena"].instances}
        assert all(not labelled[name]._closed for name in MATRICES)
        assert namespace["checkpoints"] == []
        outcomes.append((type(error.value), error.value.args))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("diagnostic", ("managed", "ambiguous", "stale"))
def test_retired_origin_scratch_preserves_real_reload_reader_and_diagnostics(diagnostic):
    namespace = _namespace()
    origins = namespace["PackedManagedOrigins"](4)
    origins.set_state(0, namespace["_ORIGIN_MANAGED"], -64, 0)
    origins.set_transfer(1, namespace["_TRANSFER_GEP"], 0, 16)
    origins.set_transfer(2, namespace["_TRANSFER_COPY"], 1)
    joined = namespace["CompilerIntArena"]()
    joined.append(1)
    joined.append(2)
    origins.set_join(3, joined)
    joined.close()
    origins.converge()
    assert len(origins.transfer_ids) == 2
    expected = tuple(origins.states._values)
    if diagnostic == "ambiguous":
        origins.set_state(2, namespace["_ORIGIN_AMBIGUOUS"])
    for arena in (origins.transfers, origins.transfer_ids, origins.proposal): arena.close()
    assert not origins.states._closed and origins.value_count == 4
    if diagnostic != "ambiguous": assert tuple(origins.states._values) == expected
    kernel = Kernel(namespace, [], 4)
    live = namespace["PackedManagedLiveness"]()
    state_id = live.append_state({0, 1, 2, 3})
    reloads = namespace["PackedReloadScratch"]()
    roots = SimpleNamespace(has_location_offset=lambda state, offset: diagnostic != "stale")
    function = namespace["_native_plan_reloads"]
    if diagnostic == "managed":
        function(SimpleNamespace(frame_size=1024), kernel, roots, 0, live, state_id, origins, reloads)
        assert len(reloads.records) == 12
        assert sorted(reloads.records.get3_unchecked(index).third for index in range(4)) == [0, 16, 16, 16]
    else:
        message = "ambiguous root provenance" if diagnostic == "ambiguous" else "outlives root_offset=-64"
        with pytest.raises(ValueError, match=message):
            function(SimpleNamespace(frame_size=1024), kernel, roots, 0, live, state_id, origins, reloads)
    origins.close()
    origins.close()
    assert all(arena.releases == 1 for arena in (origins.states, origins.transfers, origins.transfer_ids, origins.proposal))


@pytest.mark.parametrize("failure", (None, "roots", "origins", "liveness", "finish"))
def test_planner_retires_only_after_successful_consumers_and_keeps_final_close(failure):
    outcomes = []
    for original in (True, False):
        namespace = _namespace(original=original)
        arena = namespace["CompilerIntArena"]
        kernel = Kernel(namespace, [], 3)
        func = SimpleNamespace(kernel=kernel, name="plan", frame_size=0)
        retained = {}
        checks = []

        def step(name):
            if failure == name: raise ValueError("original " + name)

        def aliases_factory(kernel):
            aliases = namespace["PackedPointerAliases"](3)
            aliases.set_alias(1, 0, 8)
            retained["aliases"] = aliases
            return aliases

        def roots_factory(func, globals_, aliases, kernel):
            aliases.resolve(1)
            assert aliases.result.get_unchecked(1) == 8
            step("roots")
            return SimpleNamespace(
                close=lambda: checks.append("roots-close"),
                retire_construction_state=lambda: checks.append("roots-retire"),
            )

        def origins_factory(func, aliases, roots, kernel):
            aliases.resolve(1)
            step("origins")
            origins = namespace["PackedManagedOrigins"](3)
            origins.set_state(0, namespace["_ORIGIN_MANAGED"], -8, 0)
            origins.set_transfer(1, namespace["_TRANSFER_GEP"], 0, 8)
            origins.set_transfer(2, namespace["_TRANSFER_COPY"], 1)
            origins.converge()
            retained["origins"] = origins
            retained["states"] = tuple(origins.states._values)
            return origins

        def liveness_factory(func, origins, kernel):
            assert ("roots-retire" in checks) == (not original)
            aliases = retained["aliases"]
            assert aliases.records._closed == (not original)
            assert aliases.result._closed == (not original)
            for scratch in (origins.transfers, origins.transfer_ids, origins.proposal):
                assert scratch._closed == (not original)
            assert not origins.states._closed and origins.value_count == 3
            assert tuple(origins.states._values) == retained["states"]
            assert all(origins.is_tracked(value) for value in range(3))
            step("liveness")
            packed = namespace["PackedManagedLiveness"]()
            packed.append_state({0, 1, 2})
            return packed

        class Planned:
            def __init__(self, **kwargs): pass
            def adopt_root_state_locations(self, roots): pass
            def finish_build(self): step("finish")

        namespace.update(_native_pointer_aliases=aliases_factory, _native_root_states=roots_factory,
                         _native_managed_origins=origins_factory, _native_managed_liveness=liveness_factory,
                         function_id=lambda identity: 1, stable_id_prefix_limb=lambda *args: 0,
                         PackedPlannedSafepoints=Planned, FunctionStackMapPlan=SimpleNamespace)
        try:
            plan = namespace["_build_function_stack_map_plan_native"](func, [], "")
            outcome = ("value", plan.function_name, plan.frame_size)
        except ValueError as error:
            outcome = ("error", error.args)
        aliases = retained["aliases"]
        if failure in ("roots", "origins"):
            assert not aliases.records._closed
        elif failure is None:
            assert aliases.records._closed and aliases.records.releases == 1
            assert aliases.records.close_calls == (1 if original else 2)
            origins = retained["origins"]
            assert origins.states.close_calls == 1
            assert all(scratch.close_calls == (1 if original else 2) for scratch in
                       (origins.transfers, origins.transfer_ids, origins.proposal))
        else:
            assert aliases.records._closed == (not original)
        outcomes.append(outcome)
    assert outcomes[0] == outcomes[1]


def test_origin_converge_remains_reusable_outside_planner():
    namespace = _namespace()
    origins = namespace["PackedManagedOrigins"](3)
    origins.set_state(0, namespace["_ORIGIN_MANAGED"], -8, 0)
    origins.set_transfer(1, namespace["_TRANSFER_COPY"], 0)
    origins.converge()
    origins.set_transfer(2, namespace["_TRANSFER_GEP"], 1, 16)
    origins.converge()
    assert origins.state(2).third == 16
    assert all(not arena._closed for arena in (origins.states, origins.transfers, origins.transfer_ids, origins.proposal))
