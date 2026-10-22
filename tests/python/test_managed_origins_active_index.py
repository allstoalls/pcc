"""Finite source-extracted host controls; no project imports or native proof.

The actual class, private convergence method and complete builder are extracted
from production. Only input tables, GEP-offset lookup and root/alias providers
are finite fakes. Arena methods are extracted from production and raw memory
operations are forbidden. Frozen reference identity is Python-version and CRLF-checkout stable.
"""

import ast
import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/backend/self_backend_precise_stackmaps.py"
ARENA = ROOT / "pcc/backend/self_backend_value_arena.py"
KERNEL = ROOT / "pcc/backend/self_backend_kernel.py"
REFERENCE = ROOT / "tests/managed_origins_active_index_reference.json"
REFERENCE_SHA256 = "06e17d3f29e528ddc90be5b20309f8d59fd0833e63a243a092c6274e67471a63"
ORIGIN_NAMES = ("states", "transfers", "transfer_ids", "proposal")


def _reference(raw=None):
    raw = REFERENCE.read_bytes() if raw is None else raw
    # Git may materialize text files with CRLF. Canonicalize that exact pair
    # only; source bytes, other whitespace and lone CR remain identity-bearing.
    canonical = raw.replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == REFERENCE_SHA256
    return json.loads(canonical)["source"]


def _node(source, name):
    return next(node for node in ast.parse(source).body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name)


def _raw_method(source, name):
    cls = _node(source, "PackedManagedOrigins")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    return "".join(source.splitlines(keepends=True)[method.lineno - 1:method.end_lineno])


def _assert_public_unchanged(source):
    reference = _reference()["PackedManagedOrigins"]
    assert _raw_method(source, "converge") == _raw_method(reference, "converge")
    before = _node(reference, "PackedManagedOrigins")
    after = _node(source, "PackedManagedOrigins")
    # Compare version-local structures, never a version-dependent AST hash.
    before.body = [node for node in before.body if not isinstance(node, ast.Expr)]
    after.body = [node for node in after.body if not isinstance(node, ast.Expr)
                  and not (isinstance(node, ast.FunctionDef) and node.name == "_converge_fresh")]
    assert ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)


def _exec(nodes, namespace):
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                       names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<finite-managed-origins>", "exec"), namespace)


def _forbidden(*args, **kwargs):
    raise AssertionError("native operation is forbidden in finite host controls")


def _is_index_build(node):
    expected = ast.parse("changed and not indexed", mode="eval").body
    return (isinstance(node, ast.If)
            and ast.dump(node.test, include_attributes=False)
            == ast.dump(expected, include_attributes=False))


def _runtime(original=False, fault=None):
    events = SimpleNamespace(phase="seed", ops=[], calls=[], labels={}, arenas=[],
                             origins=[], sweeps=[], external=[], fault=fault)

    def phase(name):
        events.phase = name

    def enter(origins, name):
        events.calls.append((name, events.phase))

    def sweep(indexed):
        events.phase = "indexed" if indexed else "full"
        events.sweeps.append(events.phase)

    def capture(origins):
        events.origins.append(origins)
        for name in ORIGIN_NAMES:
            events.labels[id(getattr(origins, name))] = name

    def arena_event(arena, operation, index=-1, value=None, stage="before"):
        label = events.labels.get(id(arena), "other")
        events.ops.append((events.phase, label, operation, index, value, stage))
        if events.fault is not None:
            events.fault(events, arena, operation, index, value, stage)

    def allocate(arena, capacity):
        events.arenas.append((arena, capacity))

    def malloc(*args):
        raise NotImplementedError("finite host arena")

    namespace = dict(valueclass=lambda cls: dataclass(frozen=True)(cls),
                     NO_OFFSET=-(1 << 62), TYPE_KIND_PTR=3,
                     PARSED_INSTRUCTION_KIND_LOAD=1, PARSED_INSTRUCTION_KIND_CAST=2,
                     PARSED_INSTRUCTION_KIND_GEP=3, PARSED_INSTRUCTION_KIND_SELECT=4,
                     IndexedFunctionKernel=type("FiniteExactKernel", (), {}),
                     _phase=phase, _enter=enter, _sweep=sweep, _capture=capture,
                     _arena_event=arena_event, _allocate=allocate)
    arena_tree = ast.parse(ARENA.read_text())
    namespace.update({alias.asname or alias.name: _forbidden
                      for item in arena_tree.body if isinstance(item, ast.ImportFrom)
                      for alias in item.names})
    namespace.update(malloc=malloc, _py_bytes_new=_forbidden)
    nodes = [copy.deepcopy(node) for node in arena_tree.body if isinstance(node, ast.ClassDef)
             and node.name in ("CompilerInt2", "CompilerInt3", "CompilerInt4", "CompilerIntArena")]
    arena = next(node for node in nodes if node.name == "CompilerIntArena")
    for method in arena.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        if method.name == "__init__":
            method.body.insert(0, ast.parse("_allocate(self, initial_capacity)").body[0])
        if method.name in ("get_unchecked", "set_unchecked", "set3_unchecked", "append", "append_zeros"):
            index = "index" if method.name in ("get_unchecked", "set_unchecked", "set3_unchecked") else "-1"
            value = "value" if method.name in ("set_unchecked", "append") else "None"
            call = f"_arena_event(self, {method.name!r}, {index}, {value})"
            method.body.insert(0, ast.parse(call).body[0])
            if method.name == "set_unchecked":
                method.body.append(ast.parse("_arena_event(self, 'set_unchecked', index, value, 'after')").body[0])
    _exec(nodes, namespace)
    stackmap_source = SOURCE.read_text()
    origin_source = _reference()["PackedManagedOrigins"] if original else stackmap_source
    cls = copy.deepcopy(_node(origin_source, "PackedManagedOrigins"))
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        if method.name == "__init__":
            method.body.append(ast.parse("_capture(self)").body[0])
        if method.name in ("converge", "_converge_fresh", "write_proposed"):
            method.body.insert(0, ast.parse(f"_enter(self, {method.name!r})").body[0])
        if method.name == "converge":
            fixedpoint = next(node for node in method.body if isinstance(node, ast.While))
            fixedpoint.body.insert(0, ast.parse("_sweep(False)").body[0])
            index = method.body.index(fixedpoint)
            method.body.insert(index + 1, ast.parse("_phase('terminal')").body[0])
        if method.name == "_converge_fresh":
            main = next(node for node in method.body if isinstance(node, ast.Try))
            fixedpoint = next(node for node in main.body if isinstance(node, ast.While))
            fixedpoint.body.insert(0, ast.parse("_sweep(indexed)").body[0])
            build = next(node for node in fixedpoint.body if _is_index_build(node))
            build.body.insert(0, ast.parse("_phase('index')").body[0])
            index = main.body.index(fixedpoint)
            main.body.insert(index + 1, ast.parse("_phase('terminal')").body[0])
            main.finalbody.insert(0, ast.parse("_phase('cleanup')").body[0])
    constants = [copy.deepcopy(node) for node in ast.parse(stackmap_source).body
                 if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                 and target.id.startswith(("_ORIGIN_", "_TRANSFER_")) for target in node.targets)]
    builder_source = _reference()["_native_managed_origins"] if original else stackmap_source
    builder = copy.deepcopy(_node(builder_source, "_native_managed_origins"))
    scalar_window = copy.deepcopy(_node(KERNEL.read_text(), "_scalar_window_is_complete"))
    shape = copy.deepcopy(_node(stackmap_source, "_managed_origin_arena_has_shape"))
    identity = copy.deepcopy(_node(stackmap_source, "_managed_origin_types_identical"))
    _exec([*constants, identity, shape, cls, scalar_window, builder], namespace)
    return SimpleNamespace(ns=namespace, events=events)


def _arena(runtime, values):
    arena = runtime.ns["CompilerIntArena"]()
    for value in values:
        arena.append(value)
    return arena


def _inputs(runtime, specification):
    """Ordinary finite input records; no parser/real-kernel coverage claim."""
    ns, events = runtime.ns, runtime.events
    r2, r4 = ns["CompilerInt2"], ns["CompilerInt4"]
    kernel_type = ns["IndexedFunctionKernel"]
    if specification.get("custom_kernel"):
        kernel_type = type("CustomKernel", (kernel_type,), {})
    kernel = kernel_type()
    count = specification.get("count", 32)
    instructions = specification.get("instructions", [])
    phis = specification.get("phis", [])
    args = specification.get("args", [])
    kernel.value_names = [f"%{index}" for index in range(count)]
    kernel.block_names = ["entry"] if instructions or phis else []
    kernel.type_scalars = _arena(runtime, [3] + [0] * 23)
    kernel.instruction_facts = _arena(runtime, [value for instruction in instructions
                                               for value in (instruction[1], 0, 0, 0)])
    incoming_records = []
    phi_records = []
    for destination, sources in phis:
        phi_records.append(r4(destination, 0, len(incoming_records), len(sources)))
        incoming_records.extend(r2(source, 0) for source in sources)
    records = []
    for instruction in instructions:
        kind, destination, first, second = instruction
        records.append(r4(0, second if kind == "cast" else 0, first,
                          second if kind == "select" else 0))
    kinds = {"load": 1, "cast": 2, "gep": 3, "select": 4, "raw": 99}
    kernel.value_id = lambda name: dict(args)[name]
    kernel.block_fact = lambda block_id: r4(0, len(instructions), 0, 0)
    kernel.block_phi_fact = lambda block_id: r2(0, len(phis))
    kernel.phi_record = lambda index: phi_records[index]
    kernel.phi_incoming = lambda index: incoming_records[index]
    kernel.instruction_fact_by_id = lambda index: r4(instructions[index][1], 0, 0, 0)
    kernel.instruction_metadata_by_id = lambda index: r4(kinds[instructions[index][0]], index, 0, 0)
    kernel.instruction_record = lambda index: records[index]
    kernel.gep_header = lambda index: records[index]
    kernel.value_type_id = lambda value_id: specification.get("type_ids", {}).get(value_id, 0)
    kernel.type_header = lambda type_id: r4(3 if type_id == 0 else 0, 0, 0, 0)
    ns["_constant_gep_offset_indexed"] = lambda owner, index: instructions[index][3]
    aliases = SimpleNamespace(result=_arena(runtime, [0, 0]))

    def resolve(value_id):
        aliases.result.set_unchecked(0, value_id)

    aliases.resolve = resolve
    root_offsets = specification.get("roots", {})
    roots = SimpleNamespace(registered_root_offset=lambda base, offset: root_offsets.get(base, ns["NO_OFFSET"]))
    func = SimpleNamespace(args=[SimpleNamespace(name=name, type=SimpleNamespace(is_ptr=True))
                                 for name, value in args])
    return func, aliases, roots, kernel


def _snapshot(origins):
    return tuple((name, tuple(getattr(origins, name)._values), getattr(origins, name)._length,
                  getattr(origins, name)._capacity, getattr(origins, name)._closed)
                 for name in ORIGIN_NAMES)


def _run(specification, *, original=False, fault=None, customize=None):
    runtime = _runtime(original, fault)
    spec = specification(runtime) if callable(specification) else copy.deepcopy(specification)
    inputs = _inputs(runtime, spec)
    if customize is not None:
        customize(runtime, inputs)
    try:
        result = runtime.ns["_native_managed_origins"](*inputs)
        error = None
    except BaseException as exc:
        result = runtime.events.origins[-1] if runtime.events.origins else None
        error = (type(exc), exc.args)
    runtime.result, runtime.error = result, error
    return runtime


def _equivalent(specification, customize=None, expected_error=None):
    old = _run(specification, original=True, customize=customize)
    new = _run(specification, customize=customize)
    # Equality alone can hide the same broken fixture/native trap on both
    # sides. Happy controls must finish; intentional errors are exact contracts.
    assert new.error == old.error == expected_error
    assert (None if new.result is None else _snapshot(new.result)) == (
        None if old.result is None else _snapshot(old.result))
    assert new.events.external == old.events.external
    # The scheduler may omit only outer transfer-kind reads. Actual state,
    # proposal and JOIN-source operations retain their order and multiplicity.
    def semantic_operations(runtime):
        return [event[1:] for event in runtime.events.ops
                if event[0] != "seed" and event[1] in ("states", "proposal", "transfer_ids")]
    assert semantic_operations(new) == semantic_operations(old)
    return old, new


def _method_calls(runtime, name):
    return sum(call == name for call, phase in runtime.events.calls)


def _operations(runtime, phase, label, operation, *, lane=None, stage="before"):
    return [event for event in runtime.events.ops
            if event[0:3] == (phase, label, operation) and event[5] == stage
            and (lane is None or event[3] % 4 == lane)]


def test_public_source_and_reference_have_stable_identity():
    source = SOURCE.read_text()
    _assert_public_unchanged(source)
    with pytest.raises(AssertionError):
        _assert_public_unchanged(source.replace("    def converge(self) -> None:\n",
                                               "    def converge(self) -> None:\n        pass\n", 1))
    raw = REFERENCE.read_bytes()
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    assert _reference(lf) == _reference(crlf) == _reference(raw)
    changed_body = lf.replace(b"Dense managed-pointer provenance", b"Changed managed-pointer provenance", 1)
    assert changed_body != lf
    with pytest.raises(AssertionError):
        _reference(changed_body)
    with pytest.raises(AssertionError):
        _reference(lf + b" ")
    with pytest.raises(AssertionError):
        _reference(lf.replace(b"\n", b"\r"))


def test_matching_unexpected_builder_failures_cannot_pass_equivalence():
    def customize(runtime, inputs):
        inputs[3].instruction_record = lambda index: _forbidden()

    with pytest.raises(AssertionError):
        _equivalent({"count": 32, "instructions": [("cast", 1, 0, 0)]}, customize)


def test_index_build_selector_uses_structure_and_rejects_changed_predicate():
    assert _is_index_build(ast.parse("if changed and not indexed:\n    pass\n").body[0])
    assert not _is_index_build(ast.parse("if changed or not indexed:\n    pass\n").body[0])
    assert not _is_index_build(ast.parse("if changed and indexed:\n    pass\n").body[0])


CASES = [
    {"count": 0},
    {"count": 32, "args": [("raw", 0)]},
    {"count": 32, "instructions": [("load", 31, 5, 0)], "roots": {5: 24}},
    {"count": 32, "args": [("raw", 0)], "instructions": [("cast", 1, 0, 0)]},
    {"count": 32, "instructions": [("load", 31, 5, 0), ("gep", 1, 31, 8)], "roots": {5: 24}},
    {"count": 32, "instructions": [("load", 31, 5, 0), ("gep", 1, 31, None)], "roots": {5: 24}},
    {"count": 32, "instructions": [("cast", 1, 2, 0), ("cast", 2, 31, 0), ("load", 31, 5, 0)], "roots": {5: 24}},
    {"count": 32, "instructions": [("cast", 1, 2, 0), ("cast", 2, 1, 0)]},
    {"count": 32, "phis": [(1, [1])]},
    {"count": 32, "phis": [(1, [])]},
    {"count": 32, "phis": [(1, [31, 30])], "instructions": [("load", 31, 5, 0), ("load", 30, 5, 0)], "roots": {5: 24}},
    {"count": 32, "phis": [(1, [31, 30])], "instructions": [("load", 31, 5, 0), ("load", 30, 6, 0)], "roots": {5: 24, 6: 32}},
    {"count": 32, "args": [("raw", 30)], "phis": [(1, [31, 30])], "instructions": [("load", 31, 5, 0)], "roots": {5: 24}},
    {"count": 32, "instructions": [("select", 1, 31, 30), ("load", 31, 5, 0)], "roots": {5: 24}},
    {"count": 32, "instructions": [("cast", 1, -1, 0), ("gep", 2, 100, 3)]},
    {"count": 32, "phis": [(1, [-1, 100])]},
    {"count": 32, "instructions": [("cast", 1, 31, 1), ("raw", 2, 0, 0)]},
    {"count": 8, "instructions": [("cast", value, -1, 0) for value in range(8)]},
    {"count": 8, "phis": [(1, [0]), (1, [0])]},
    {"count": 9, "args": [("raw", 0)], "phis": [(1, [0]), (1, [0])]},
    {"count": 32, "phis": [(-1, [0])]},
    {"count": 32, "phis": [(32, [0])], "expected_error": (IndexError, ("list assignment index out of range",))},
    {"count": 32, "args": [("bad", 32)], "expected_error": (IndexError, ("list assignment index out of range",))},
    {"count": 32, "instructions": [("cast", -1, 0, 0)]},
    {"count": 32, "instructions": [("cast", 32, 0, 0)], "expected_error": (IndexError, ("list assignment index out of range",))},
    {"count": 32, "instructions": [("load", 0, 5, 0), ("gep", 1, 0, 1 << 90)], "roots": {5: -(1 << 88)}},
]


@pytest.mark.parametrize("specification", CASES)
def test_builder_full_arena_equivalence(specification):
    _equivalent(specification, expected_error=specification.get("expected_error"))


def test_zero_dense_r1_and_multi_sweep_counts():
    _, zero = _equivalent({"count": 32, "args": [("raw", 0)]})
    assert zero.events.sweeps == []
    assert not [event for event in zero.events.ops if event[0] != "seed"]
    _, dense = _equivalent({"count": 8, "phis": [(1, [0]), (1, [0])]})
    assert _method_calls(dense, "converge") == 1
    assert not _operations(dense, "index", "transfers", "set_unchecked")
    old, r1 = _equivalent({"count": 32, "phis": [(1, [1])]})
    assert r1.events.sweeps == ["full"]
    assert not _operations(r1, "index", "transfers", "set_unchecked")
    assert len(_operations(r1, "full", "transfers", "get_unchecked", lane=0)) == 33
    assert len(_operations(r1, "terminal", "transfers", "get_unchecked", lane=0)) == 32
    old, multi = _equivalent({"count": 32, "instructions": [
        ("cast", 1, 2, 0), ("cast", 2, 31, 0), ("load", 31, 5, 0)], "roots": {5: 24}})
    assert multi.events.sweeps == ["full", "indexed", "indexed"]
    assert old.events.sweeps == ["full"] * 3
    assert [event[4] for event in _operations(multi, "index", "transfers", "set_unchecked")] == [1, 2]
    assert [event[3] for event in _operations(multi, "index", "transfers", "set_unchecked")] == [3, 7]
    assert len(_operations(multi, "index", "transfers", "get_unchecked", lane=0)) == 32
    assert len(_operations(multi, "indexed", "transfers", "get_unchecked", lane=3)) == 4
    assert len(_operations(multi, "terminal", "transfers", "get_unchecked", lane=3)) == 2
    assert not _operations(multi, "terminal", "transfers", "get_unchecked", lane=0)
    assert [event[4] for event in _operations(multi, "cleanup", "transfers", "set_unchecked")] == [0, 0]
    assert [capacity for arena, capacity in multi.events.arenas] == [
        capacity for arena, capacity in old.events.arenas]
    # No scheduler arena, growth, append or changed capacity during convergence.
    assert not [event for event in multi.events.ops if event[0] != "seed"
                and event[2] in ("append", "append_zeros")]
    assert all(value == 0 for value in multi.result.transfers._values[3::4])


def test_duplicate_bound_never_undercounts_active_ids():
    _, runtime = _equivalent({"count": 9, "args": [("raw", 0)], "phis": [(1, [0]), (1, [0])]})
    assert [event[4] for event in _operations(runtime, "index", "transfers", "set_unchecked")] == [1]


def test_proof_reads_are_separate_from_convergence_operation_bound():
    old, new = _equivalent({"count": 32, "args": [("raw", 0)],
                            "phis": [(1, [0, 0, -1])],
                            "instructions": [("cast", 2, 0, 0), ("gep", 3, 0, 8),
                                             ("select", 4, 0, -1)]})
    seed_reads = lambda runtime: sum(event[0] == "seed" and event[2] == "get_unchecked"
                                     for event in runtime.events.ops)
    # Three PHI inputs, two SELECT inputs, one COPY and one GEP source.
    assert seed_reads(new) - seed_reads(old) == 7
    assert _method_calls(new, "converge") == 0


def test_failed_payload_proof_stops_subsequent_proof_reads():
    def spec(runtime):
        class CustomInt(int):
            pass
        return {"count": 32, "phis": [(1, [CustomInt(0), 0, 0])],
                "instructions": [("cast", 2, 0, 0), ("gep", 3, 0, 8)]}

    old, new = _equivalent(spec)
    seed_reads = lambda runtime: sum(event[0] == "seed" and event[2] == "get_unchecked"
                                     for event in runtime.events.ops)
    assert seed_reads(new) - seed_reads(old) == 1
    assert _method_calls(new, "_converge_fresh") == 0
    assert _method_calls(new, "converge") == 1


def test_shape_proof_never_scans_payload_or_calls_custom_storage():
    runtime = _runtime()
    shape = runtime.ns["_managed_origin_arena_has_shape"]
    arena = _arena(runtime, [0, 0, 0])
    assert shape(arena, 3)
    arena._values[0] = object()
    assert shape(arena, 3)  # Payload proof belongs to the actual copy sites.

    class PoisonList(list):
        def __len__(self):
            raise AssertionError("custom length must not be called")

        def __getitem__(self, index):
            raise AssertionError("custom payload must not be read")

    arena._values = PoisonList([0, 0, 0])
    assert not shape(arena, 3)
    arena._values = [0, 0, 0]
    arena._address, arena._capacity = 4096, 3
    assert shape(arena, 3)  # Metadata-only native branch, no raw access.
    arena._capacity = 2
    assert not shape(arena, 3)
    arena._address = 0


def test_no_production_scheduler_allocation_or_public_constructor_change():
    tree = ast.parse(SOURCE.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "PackedManagedOrigins")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_converge_fresh")
    assert not any(isinstance(node, (ast.List, ast.ListComp, ast.Dict, ast.DictComp,
                                     ast.Set, ast.SetComp, ast.GeneratorExp))
                   for node in ast.walk(method))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "CompilerIntArena" for node in ast.walk(method))
    _assert_public_unchanged(SOURCE.read_text())


def test_repeated_public_convergence_after_private_cleanup():
    old, new = _equivalent({"count": 32, "args": [("raw", 0)], "instructions": [("cast", 1, 0, 0)]})
    for runtime in (old, new):
        runtime.result.converge()
        runtime.result.set_transfer(3, runtime.ns["_TRANSFER_COPY"], 1)
        runtime.result.converge()
    assert _snapshot(old.result) == _snapshot(new.result)
    assert _method_calls(new, "converge") == 2


@pytest.mark.parametrize("site", ["arg", "phi_dest", "incoming", "dest", "root", "cast", "gep_source", "gep_offset", "select_first", "select_second"])
def test_custom_int_copy_sites_fall_back_without_extra_callbacks(site):
    def specification(runtime):
        class Scalar(int):
            def __lt__(self, other):
                runtime.events.external.append(("lt", int(self), other))
                return super().__lt__(other)

            def __ge__(self, other):
                runtime.events.external.append(("ge", int(self), other))
                return super().__ge__(other)

            def __mul__(self, other):
                runtime.events.external.append(("mul", int(self), other))
                return super().__mul__(other)

            def __add__(self, other):
                runtime.events.external.append(("add", int(self), other))
                return super().__add__(other)

        cases = {
            "arg": {"args": [("raw", Scalar(0))]},
            "phi_dest": {"phis": [(Scalar(1), [0])]},
            "incoming": {"phis": [(1, [Scalar(0)])]},
            "dest": {"instructions": [("cast", Scalar(1), 0, 0)]},
            "root": {"instructions": [("load", 0, 5, 0)], "roots": {5: Scalar(24)}},
            "cast": {"instructions": [("cast", 1, Scalar(0), 0)]},
            "gep_source": {"instructions": [("gep", 1, Scalar(0), 0)]},
            "gep_offset": {"instructions": [("gep", 1, 0, Scalar(8))]},
            "select_first": {"instructions": [("select", 1, Scalar(0), 0)]},
            "select_second": {"instructions": [("select", 1, 0, Scalar(0))]},
        }
        return dict(count=32, **cases[site])

    old, new = _equivalent(specification)
    assert _method_calls(new, "_converge_fresh") == 0
    assert _method_calls(new, "converge") == 1


def test_custom_kernel_preserves_public_route():
    _, new = _equivalent({"count": 32, "custom_kernel": True,
                          "args": [("raw", 0)], "instructions": [("cast", 1, 0, 0)]})
    assert _method_calls(new, "_converge_fresh") == 0
    assert _method_calls(new, "converge") == 1


def test_nonexact_phi_record_is_not_read_again_for_destination_proof():
    def customize(runtime, inputs):
        kernel = inputs[3]
        original = kernel.phi_record

        class RecordMeta(type):
            def __eq__(self, other):
                raise AssertionError("record type proof must use identity, not equality")

        class CustomRecord(metaclass=RecordMeta):
            def __init__(self, record):
                self.record = record

            def __getattr__(self, name):
                runtime.events.external.append(name)
                return getattr(self.record, name)

        kernel.phi_record = lambda index: CustomRecord(original(index))

    _, new = _equivalent({"count": 32, "phis": [(1, [0])]}, customize)
    assert new.events.external.count("first") == 1
    assert _method_calls(new, "_converge_fresh") == 0
    assert _method_calls(new, "converge") == 1


@pytest.mark.parametrize("kind", ["arena_subclass", "list_subclass", "closed", "length", "capacity", "address"])
def test_nonordinary_fresh_arena_shapes_keep_public_loop(kind):
    def customize(runtime, inputs):
        original = runtime.ns["PackedManagedOrigins"].__init__

        def initialize(self, count):
            original(self, count)
            if kind == "arena_subclass":
                class CustomArena(runtime.ns["CompilerIntArena"]):
                    pass
                replacement = CustomArena()
                replacement.append_zeros(count * 4)
                self.transfers = replacement
                runtime.events.labels[id(replacement)] = "transfers"
            elif kind == "list_subclass":
                self.transfers._values = type("CustomList", (list,), {})(self.transfers._values)
            elif kind == "closed":
                self.transfers._closed = True
            elif kind == "length":
                self.transfers._length += 1
            elif kind == "capacity":
                self.transfers._capacity = False
            else:
                self.transfers._address = False

        runtime.ns["PackedManagedOrigins"].__init__ = initialize

    _, new = _equivalent({"count": 32, "args": [("raw", 0)], "instructions": [("cast", 1, 0, 0)]}, customize)
    assert _method_calls(new, "converge") == 1
    assert not _operations(new, "index", "transfers", "set_unchecked")


@pytest.mark.parametrize("phase,label,operation,stage,index", [
    ("index", "transfers", "set_unchecked", "before", 3),
    ("index", "transfers", "set_unchecked", "after", 3),
    ("index", "transfers", "set_unchecked", "before", 7),
    ("index", "transfers", "set_unchecked", "after", 7),
    ("index", "transfers", "get_unchecked", "before", 12),
    ("indexed", "transfers", "get_unchecked", "before", 3),
    ("indexed", "states", "get_unchecked", "before", 3),
    ("indexed", "proposal", "get_unchecked", "before", 0),
    ("terminal", "transfers", "get_unchecked", "before", 3),
])
def test_index_and_convergence_failures_restore_every_covered_lane(phase, label, operation, stage, index):
    fired = []

    def fault(events, arena, current_operation, current_index, value, current_stage):
        if (not fired and events.phase == phase and events.labels.get(id(arena)) == label
                and current_operation == operation and current_stage == stage and current_index == index):
            fired.append(True)
            raise RuntimeError("primary")

    result = _run({"count": 32, "args": [("raw", 0)], "instructions": [
        ("cast", 1, 0, 0), ("cast", 2, 0, 0)]}, fault=fault)
    assert fired
    assert result.error == (RuntimeError, ("primary",))
    writes = _operations(result, "index", "transfers", "set_unchecked")
    clears = _operations(result, "cleanup", "transfers", "set_unchecked")
    assert [event[3] for event in clears] == [event[3] for event in writes]
    assert all(value == 0 for value in result.result.transfers._values[3::4])


@pytest.mark.parametrize("primary", [False, True])
def test_cleanup_continues_and_preserves_primary_exception(primary):
    raised = []

    def fault(events, arena, operation, index, value, stage):
        if events.labels.get(id(arena)) != "transfers":
            return
        if primary and not raised and events.phase == "indexed" and operation == "get_unchecked":
            raised.append("primary")
            raise RuntimeError("primary")
        if events.phase == "cleanup" and operation == "set_unchecked" and stage == "after":
            raise ValueError("cleanup", index)

    result = _run({"count": 32, "args": [("raw", 0)], "instructions": [
        ("cast", 1, 0, 0), ("cast", 2, 0, 0)]}, fault=fault)
    assert result.error == ((RuntimeError, ("primary",)) if primary else (ValueError, ("cleanup", 3)))
    assert [event[3] for event in _operations(result, "cleanup", "transfers", "set_unchecked")] == [3, 7]
    assert all(value == 0 for value in result.result.transfers._values[3::4])


def test_malformed_public_join_keeps_reference_first_error_and_storage():
    old, new = _runtime(True), _runtime()
    for runtime in (old, new):
        origin = runtime.ns["PackedManagedOrigins"](32)
        origin.set_transfer(1, runtime.ns["_TRANSFER_JOIN"], 0, 1)
        origin.transfers.set_unchecked(3, 123)
        try:
            origin.converge()
        except BaseException as exc:
            runtime.error = (type(exc), exc.args)
        runtime.result = origin
    assert old.error == new.error == (IndexError, ("list index out of range",))
    assert _snapshot(old.result) == _snapshot(new.result)


@pytest.mark.parametrize("label,index", [("transfers", 4), ("states", 3), ("proposal", 0)])
def test_first_sweep_error_preserves_reference_state_without_index_writes(label, index):
    def fault(events, arena, operation, current_index, value, stage):
        if (events.phase == "full" and events.labels.get(id(arena)) == label
                and operation == "get_unchecked" and current_index == index):
            raise LookupError("first sweep", label)

    spec = {"count": 32, "args": [("raw", 0)], "instructions": [("cast", 1, 0, 0)]}
    old, new = _run(spec, original=True, fault=fault), _run(spec, fault=fault)
    assert old.error == new.error == (LookupError, ("first sweep", label))
    assert _snapshot(old.result) == _snapshot(new.result)
    assert not _operations(new, "index", "transfers", "set_unchecked")


@pytest.mark.parametrize("site", ["incoming", "phi", "cast", "gep", "select_first", "select_second"])
@pytest.mark.parametrize("failure", ["lookup", "property", None])
def test_generic_method_lookup_precedes_record_property(site, failure):
    def customize(runtime, inputs):
        ns, events = runtime.ns, runtime.events
        func, aliases, roots, kernel = inputs
        packed_base = ns["PackedManagedOrigins"]
        arena_base = ns["CompilerIntArena"]
        watched = "set_join" if site == "phi" else "set_transfer"
        getter = "first" if site in ("incoming", "phi") else "fourth" if site == "select_second" else "third"
        armed = []

        def observed_lookup(name):
            if armed:
                events.external.append(("lookup", name))
                if failure == "lookup":
                    raise LookupError("lookup")

        class CustomOrigins(packed_base):
            def __getattribute__(self, name):
                if name == watched:
                    observed_lookup(name)
                return super().__getattribute__(name)

        class CustomArena(arena_base):
            def __getattribute__(self, name):
                if name == "append" and site in ("incoming", "select_first", "select_second"):
                    observed_lookup(name)
                return super().__getattribute__(name)

        class Record:
            def __init__(self, value):
                self.value = value

            def __getattribute__(self, name):
                if name == getter:
                    events.external.append(("property", name))
                    if failure == "property":
                        raise ValueError("property")
                if name in ("first", "second", "third", "fourth"):
                    return getattr(object.__getattribute__(self, "value"), name)
                return object.__getattribute__(self, name)

        ns["PackedManagedOrigins"] = CustomOrigins
        ns["CompilerIntArena"] = CustomArena

        def wrap(method):
            def call(index):
                result = method(index)
                armed.append(True)
                return Record(result)
            return call

        if site == "incoming":
            kernel.phi_incoming = wrap(kernel.phi_incoming)
        elif site == "phi":
            kernel.phi_record = wrap(kernel.phi_record)
        elif site == "gep":
            kernel.gep_header = wrap(kernel.gep_header)
        else:
            kernel.instruction_record = wrap(kernel.instruction_record)

    if site in ("incoming", "phi"):
        spec = {"count": 32, "custom_kernel": True, "phis": [(1, [0])]}
    else:
        kind = "select" if site.startswith("select") else site
        spec = {"count": 32, "custom_kernel": True, "instructions": [(kind, 1, 0, 0)]}
    expected_error = ((LookupError, ("lookup",)) if failure == "lookup" else
                      (ValueError, ("property",)) if failure == "property" else None)
    old, new = _equivalent(spec, customize, expected_error=expected_error)
    assert new.events.external
    if failure is not None:
        expected = LookupError if failure == "lookup" else ValueError
        assert new.error[0] is expected
    else:
        target_property = next(index for index, event in enumerate(new.events.external)
                               if event[0] == "property")
        assert new.events.external[target_property - 1][0] == "lookup"


def test_generic_one_shot_callable_finalizer_is_not_delayed():
    def customize(runtime, inputs):
        base = runtime.ns["PackedManagedOrigins"]
        events = runtime.events

        class OneShot:
            def __init__(self, method):
                self.method = method

            def __call__(self, *args):
                events.external.append("call")
                return self.method(*args)

            def __del__(self):
                events.external.append("release")

        class CustomOrigins(base):
            def __getattribute__(self, name):
                method = super().__getattribute__(name)
                if name == "set_transfer":
                    events.external.append("lookup")
                    return OneShot(method)
                return method

        runtime.ns["PackedManagedOrigins"] = CustomOrigins
        kernel = inputs[3]
        metadata = kernel.instruction_metadata_by_id

        def next_instruction(index):
            events.external.append(("next", index))
            return metadata(index)

        kernel.instruction_metadata_by_id = next_instruction

    spec = {"count": 32, "custom_kernel": True,
            "instructions": [("cast", 1, 0, 0), ("cast", 2, 0, 0)]}
    _, new = _equivalent(spec, customize)
    assert new.events.external == [
        ("next", 0), "lookup", "call", "release",
        ("next", 1), "lookup", "call", "release",
    ]
