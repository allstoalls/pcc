"""Bounded host PE suffix controls; no native compiler or GC claim."""

from dataclasses import replace
from pathlib import Path
import random
import struct
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_x86_64 as pe
from pcc.backend import precise_stackmap as wire
from pcc.backend import stackmap_host_suffix as suffix
from tests.python.test_pe_stackmap_merge import (
    _dir64_sites,
    _file_offset,
    _object,
    _pe_budget,
    _sections,
    _stack_table,
)


def _root(number, *, base=wire.NO_BASE):
    flags = wire.LOCATION_MANAGED | wire.LOCATION_OWNED
    if base != wire.NO_BASE:
        flags |= wire.LOCATION_DERIVED | wire.LOCATION_RELOAD_REQUIRED
    return wire.StackMapLocation(wire.LOCATION_STACK_INDIRECT, flags,
                                 register=6, base_index=base, offset=-8 * number)


def _payload(vectors, *, fid=1):
    records = tuple(wire.SafepointRecord(
        safepoint_id=fid * 1000 + i + 1, instruction_offset=i * 4,
        kind=wire.SAFEPOINT_CONTINUATION, locations=tuple(vector),
        flags=wire.RECORD_HAS_EXCEPTION_EDGE | wire.RECORD_SUSPENDED,
        exceptional_offset=i * 4 + 1,
        continuation_id=i + 1,
    ) for i, vector in enumerate(vectors))
    function = wire.FunctionStackMap(fid, 0, max(16, len(records) * 4), 4096, records)
    return wire.encode_stack_map(wire.PreciseStackMap(wire.ARCH_X86_64, (function,)))


def _merge(payloads, *, budget=None):
    return wire.merge_stack_map_payloads(
        tuple(payloads), compact_budget=budget or _pe_budget(), _share_suffixes=True,
    )


def _wire_records(payload):
    _count, functions, table_start, table_count = wire._scan_stack_map_payload(payload)
    result = {}
    for fid, start, end in functions:
        records = []
        for cursor in range(start + wire.FUNCTION_SIZE, end, wire.RECORD_SIZE):
            count = int.from_bytes(payload[cursor + 20:cursor + 22], "little")
            index = int.from_bytes(payload[cursor + 28:cursor + 32], "little")
            assert index + count <= table_count
            begin = table_start + index * wire.LOCATION_SIZE
            records.append((payload[cursor:cursor + 28],
                            payload[begin:begin + count * wire.LOCATION_SIZE], index))
        result[fid] = (payload[start:start + wire.FUNCTION_SIZE], records)
    return result


def _assert_same_records(before, after):
    left, right = _wire_records(before), _wire_records(after)
    assert left.keys() == right.keys()
    for fid in left:
        assert left[fid][0] == right[fid][0]
        assert [(prefix, roots) for prefix, roots, _ in left[fid][1]] == [
            (prefix, roots) for prefix, roots, _ in right[fid][1]
        ]
    wire.validate_stack_map_payload(after, expected_arch=wire.ARCH_X86_64)
    assert wire.decode_stack_map(after) == wire.decode_stack_map(before)


@pytest.mark.parametrize("short_first", (False, True))
def test_exact_suffixes_keep_every_record_byte_and_relative_derived_base(short_first):
    a, b, c, d = (_root(i) for i in range(1, 5))
    derived = _root(5, base=0)
    # (b, derived) names b, while (a, b, derived) names a. Sharing their
    # exact suffix does not reinterpret either record's relative base index.
    vectors = [(a, b, derived), (b, derived), (a, c, d), (c, d), (d,), (), (a, c, d)]
    if short_first:
        vectors.reverse()
    payload = _payload(vectors)
    expected, offsets = wire.merge_stack_map_payloads((payload,))
    actual, shared_offsets = _merge((payload,))
    assert shared_offsets == offsets
    assert len(expected) - len(actual) == 5 * wire.LOCATION_SIZE
    _assert_same_records(expected, actual)
    assert _merge((payload,)) == (actual, shared_offsets)


def test_global_index_owns_only_unique_materialized_representatives():
    a, b, c = (_root(i) for i in range(1, 4))
    payloads = (_payload(((b, c), (a, b, c)), fid=3),
                _payload(((b, c),), fid=1),
                _payload(((b, c), (a, b, c)), fid=2))
    baseline, offsets = wire.merge_stack_map_payloads(payloads)
    actual, shared_offsets = _merge(payloads)
    assert shared_offsets == offsets
    assert struct.unpack_from("<I", actual, 16)[0] == 5
    assert len(actual) <= len(baseline)
    _assert_same_records(baseline, actual)


def test_already_overlapping_ranges_are_not_reexpanded():
    a, b, c = (_root(i) for i in range(1, 4))
    payload = bytearray(_payload(((a, b, c), (b, c), (c,), ())))
    table_start = wire.HEADER_SIZE + wire.FUNCTION_SIZE + 4 * wire.RECORD_SIZE
    struct.pack_into("<I", payload, 16, 3)
    for ordinal, index in enumerate((0, 1, 2, 3)):
        struct.pack_into("<I", payload,
                         wire.HEADER_SIZE + wire.FUNCTION_SIZE + ordinal * wire.RECORD_SIZE + 28,
                         index)
    payload = bytes(payload[:table_start + 3 * wire.LOCATION_SIZE])
    wire.validate_stack_map_payload(payload)
    actual, _ = _merge((payload,))
    assert len(actual) == len(payload)
    _assert_same_records(payload, actual)


@pytest.mark.parametrize("count,index", ((3, 0xFFFFFFFF), (0, 4)))
def test_invalid_ranges_reject_before_reverse_slice(monkeypatch, count, index):
    payload = bytearray(_payload(((_root(1),),)))
    record = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    struct.pack_into("<H", payload, record + 20, count)
    struct.pack_into("<I", payload, record + 28, index)
    budget = _pe_budget()
    labels = []
    original = budget.check_peak

    def check(raw, **kwargs):
        labels.append(kwargs.get("label"))
        return original(raw, **kwargs)

    monkeypatch.setattr(budget, "check_peak", check)
    with pytest.raises(wire.PreciseStackMapError, match="outside its table"):
        _merge((bytes(payload),), budget=budget)
    assert "suffix range" not in labels


def test_overlap_and_duplicate_bytes_keep_the_aggregate_merge_work_cap(monkeypatch):
    a, b, c = (_root(i) for i in range(1, 4))
    payloads = (_payload(((a, b, c), (b, c)), fid=1),
                _payload(((a, b, c), (b, c)), fid=2))
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 9)
    with pytest.raises(wire.PreciseStackMapError, match="location merge work"):
        _merge(payloads)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 10)
    actual, _ = _merge(payloads)
    assert struct.unpack_from("<I", actual, 16)[0] == 3


def test_suffix_planning_charges_the_extra_record_pass_without_raising_limits():
    payload = _payload(((_root(1), _root(2)), (_root(2),), ()))
    budget = _pe_budget(work=6)
    _merge((payload,), budget=budget)
    assert budget.record_visits == 6 and budget.record_visit_limit == 6
    budget = _pe_budget(work=5)
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        _merge((payload,), budget=budget)
    assert budget.record_visits == 3 and budget.record_visit_limit == 5


def test_host_gate_keeps_native_unbudgeted_and_consuming_paths_unchanged(monkeypatch):
    payloads = (_payload(((_root(1), _root(2)), (_root(2),))),)
    expected = wire.merge_stack_map_payloads(payloads)
    with pytest.raises(wire.PreciseStackMapError, match="non-consuming host budget"):
        wire.merge_stack_map_payloads(payloads, _share_suffixes=True)
    owned = list(payloads)
    with pytest.raises(wire.PreciseStackMapError, match="non-consuming host budget"):
        wire.merge_stack_map_payloads(owned, compact_budget=_pe_budget(),
                                     _consume_payloads=True, _share_suffixes=True)
    assert owned == list(payloads)
    for mutable in (bytearray(payloads[0]), memoryview(payloads[0])):
        with pytest.raises(wire.PreciseStackMapError, match="non-consuming host budget"):
            _merge((mutable,))
    runtime = SimpleNamespace(**vars(sys.implementation))
    runtime.name = "pcc"
    monkeypatch.setattr(sys, "implementation", runtime)
    assert wire.merge_stack_map_payloads(payloads) == expected
    with pytest.raises(wire.PreciseStackMapError, match="non-consuming host budget"):
        _merge(payloads)


def _trace_suffix_owners(payloads, *, limit=100_000_000):
    budget = _pe_budget(limit=limit, work=10_000)
    budget.retained_bytes = 137
    checks = []
    original = budget.check_peak

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if label.startswith("suffix "):
            admission = sys._getframe(1)
            helper = admission.f_back
            assert helper.f_code is suffix.plan_suffixes.__code__
            state = helper.f_locals
            indices = state.get("source_indices", {})
            reverse_vectors = state.get("reverse_vectors", ())
            reverse_bytes = sum(len(row[0]) for row in reverse_vectors)
            assert reverse_bytes == state.get("reverse_bytes", 0)
            assert all(type(key) is int and type(value) is int
                       for key, value in indices.items())
            assert all(type(row[0]) is bytes for row in reverse_vectors)
            assert {id(key) for key in state["table"]} == {id(key) for key in state["table_index"]}
            assert sum(map(len, state["table"])) == state["table_locations"] * wire.LOCATION_SIZE
            ranges = len(indices)
            prospective_locations = state["table_locations"]
            scratch = reverse_bytes
            if label == "suffix range":
                # The newly discovered range has not been inserted or sliced.
                assert state["source_key"] not in indices
                assert len(reverse_vectors) == ranges
                ranges += 1
                scratch += 2 * state["size"]
            elif label == "suffix append":
                assert state["source_key"] not in state["destinations"]
                prospective_locations += state["count"]
                scratch += state["size"]
            assert raw == (sum(map(len, payloads))
                           + 2 * (wire.HEADER_SIZE + state["function_bytes"]
                                  + prospective_locations * wire.LOCATION_SIZE)
                           + scratch)
            assert entries == (4 * state["function_count"] + len(state["scanned"]) + 16
                               + len(state["table_index"]) + 4 * ranges)
            del state, helper, admission, indices, reverse_vectors
        elif label == "input":
            scanner = sys._getframe(1)
            merge = scanner.f_back
            state = merge.f_locals
            assert state.get("source_index_cache") is None and state.get("scanned") is None
            existing = (sum(map(len, payloads)) - raw + state["function_bytes"]
                        + sum(map(len, state["table"])))
            metadata = 4 * len(state["functions"]) + len(state["table_index"])
            assert budget.retained_bytes == 137 + existing + metadata * budget.metadata_entry_bytes
            del state, merge, scanner
        elif label == "merge join":
            merge = sys._getframe(1)
            state = merge.f_locals
            assert state["scanned"] is None and state["source_index_cache"] is None
            assert raw == (sum(map(len, payloads))
                           + 2 * (wire.HEADER_SIZE + state["function_bytes"]
                                  + sum(map(len, state["table"]))))
            del state, merge
        estimate = raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
        checks.append((label, estimate))
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = check
    result = _merge(payloads, budget=budget)
    assert budget.retained_bytes == 137
    return result, checks


def test_suffix_scratch_has_all_live_owners_and_an_exact_limit_boundary():
    roots = tuple(_root(i + 1) for i in range(128))
    payloads = (_payload(tuple(roots[i:] for i in range(128)), fid=2),
                _payload(((roots[-1],),), fid=1))
    expected, checks = _trace_suffix_owners(payloads)
    peak = max(value for _, value in checks)
    assert any(label == "suffix append" and value == peak for label, value in checks)
    assert _trace_suffix_owners(payloads, limit=peak)[0] == expected
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded: suffix append"):
        _trace_suffix_owners(payloads, limit=peak - 1)


def test_all_empty_roots_keep_records_and_require_two_join_copies():
    payloads = (_payload(((),) * 32, fid=2), _payload(((),) * 32, fid=1))
    expected = wire.merge_stack_map_payloads(payloads)
    actual, checks = _trace_suffix_owners(payloads)
    assert actual == expected
    assert not any(label in ("suffix range", "suffix append") for label, _ in checks)
    peak = max(value for _, value in checks)
    assert _trace_suffix_owners(payloads, limit=peak)[0] == expected
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        _trace_suffix_owners(payloads, limit=peak - 1)


def _suffix_object(name):
    obj = _object(name, roots=3, ordinary=b"read-only companion")
    function = wire.decode_stack_map(obj.sections[1].data).functions[0]
    roots = function.records[0].locations
    records = (replace(function.records[0], locations=roots[1:]), function.records[1])
    payload = wire.encode_stack_map(wire.PreciseStackMap(
        wire.ARCH_X86_64, (replace(function, records=records),),
    ))
    return replace(obj, sections=(obj.sections[0], replace(obj.sections[1], data=payload),
                                  *obj.sections[2:]))


def test_production_pe_shares_suffixes_and_keeps_aslr_relocations_and_inputs():
    objects = (_suffix_object("pcc_windows_start"), _suffix_object("private_worker"))
    originals = tuple(coff.emit_object(obj) for obj in objects)
    baseline = pe.link_executable(objects)
    budget = _pe_budget()
    actual = pe.link_executable(objects, compact_budget=budget)
    rva, table = _stack_table(actual)
    _, baseline_table = _stack_table(baseline)
    assert len(baseline_table) - len(table) == 2 * wire.LOCATION_SIZE
    _assert_same_records(baseline_table, table)
    assert originals == tuple(coff.emit_object(obj) for obj in objects)
    assert budget.retained_bytes == 0 and budget.record_visits == 32
    assert _dir64_sites(actual) == _dir64_sites(baseline)
    assert sorted(_dir64_sites(actual)) == [rva + offset for offset in wire.function_address_offsets(table)]
    _, sections = _sections(actual)
    rebased = bytearray(actual)
    delta = 0x200000000
    for site in _dir64_sites(actual):
        offset = _file_offset(sections, site)
        struct.pack_into("<Q", rebased, offset, struct.unpack_from("<Q", rebased, offset)[0] + delta)
    _, relocated_table = _stack_table(bytes(rebased))
    before = wire.decode_stack_map(table, final_image=True)
    after = wire.decode_stack_map(relocated_table, final_image=True)
    assert [fn.function_address for fn in after.functions] == [
        fn.function_address + delta for fn in before.functions
    ]


@pytest.mark.parametrize("seed", range(8))
def test_bounded_varied_vectors_match_the_exact_suffix_oracle(seed):
    rng = random.Random(seed)
    roots = tuple(_root(i + 1) for i in range(8))
    vectors = [()]
    for _ in range(12):
        vector = tuple(rng.choice(roots) for _ in range(rng.randrange(1, 9)))
        vectors.extend((vector, vector[rng.randrange(len(vector)):], vector))
    rng.shuffle(vectors)
    payload = _payload(vectors)
    baseline, offsets = wire.merge_stack_map_payloads((payload,))
    actual, shared_offsets = _merge((payload,))
    unique = set(vectors) - {()}
    maximal = {vector for vector in unique if not any(
        len(other) > len(vector) and other[-len(vector):] == vector for other in unique
    )}
    assert struct.unpack_from("<I", actual, 16)[0] == sum(map(len, maximal))
    assert shared_offsets == offsets and len(actual) <= len(baseline)
    _assert_same_records(baseline, actual)


def test_suffix_helper_is_excluded_from_native_dependency_discovery():
    from pcc.frontends.python import pipeline_import_scan as scan
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets

    root = Path(__file__).resolve().parents[2]
    path = root / "pcc/backend/precise_stackmap.py"
    source = path.read_text()
    native = scan._without_inactive_implementation_imports(source, implementation="pcc")
    host = scan._without_inactive_implementation_imports(source, implementation="cpython")
    assert "from .stackmap_host_suffix import plan_suffixes" not in native
    assert "from .stackmap_host_suffix import plan_suffixes" in host
    targets = _package_import_targets(str(path), "pcc.backend.precise_stackmap", root_dir=str(root))
    assert "pcc.backend.stackmap_host_suffix" not in {name for _, name in targets}


def test_runtime_consumer_visits_exact_suffix_slots_at_each_original_pc(monkeypatch):
    from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
    from pcc.runtime.py import freestanding_gc_mapped_roots as runtime

    image = pe.link_executable((_suffix_object("pcc_windows_start"),), compact_budget=_pe_budget())
    _, table = _stack_table(image)
    decoded = wire.decode_stack_map(table, final_image=True)
    visited = []
    frame = object()
    monkeypatch.setattr(runtime, "abi_constant", ABI_CONSTANTS.__getitem__)
    monkeypatch.setattr(runtime, "gc_backend_current", lambda: 3)
    monkeypatch.setattr(runtime, "null", lambda: None)
    monkeypatch.setattr(runtime, "ptr_is_null", lambda value: int(value is None))
    monkeypatch.setattr(runtime, "ptr_add", lambda pointer, offset: (pointer[0], pointer[1] + offset))
    monkeypatch.setattr(runtime, "load_i32", lambda pointer, offset: struct.unpack_from(
        "<i", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "load_i64", lambda pointer, offset: struct.unpack_from(
        "<q", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "unsigned_div_i64", lambda a, b: (
        (a & ((1 << 64) - 1)) // (b & ((1 << 64) - 1))))

    def visit(base, offset, stable, borrowed, mode, resolve):
        visited.append((base, offset, borrowed, mode, resolve))
        return 0

    monkeypatch.setattr(runtime, "pcc_gc_visit_mapped_root_slot", visit)
    function = decoded.functions[0]
    for record in function.records:
        pc = function.function_address + record.instruction_offset
        visited.clear()
        assert runtime.pcc_gc_consume_precise_stackmap(
            (table, 0), len(table), pc, frame, 2, 1,
        ) == len(record.locations)
        assert visited == [(frame, root.offset, 0, 1, 0) for root in record.locations]
        visited.clear()
        assert runtime.pcc_gc_consume_precise_stackmap(
            (table, 0), len(table), pc + 1, frame, 2, 1,
        ) == -13
        assert visited == []
