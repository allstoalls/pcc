"""Host-only ownership controls for the private consuming stack-map merge.

This standalone regression file is not registered in fixed-count CI gates.
"""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import (
    macho_exec,
    macho_host_prepare as host,
    macho_link,
    native_object as native,
    precise_stackmap as maps,
)
from tests.python.test_macho_stack_symbol_index import _write_sparse_stack_inputs
from tests.python.test_macho_windowed_prepare import _budget


def _payload(ids, *, arch=maps.ARCH_AARCH64, records=4, empty_roots=False):
    roots = () if empty_roots else (maps.StackMapLocation(
        maps.LOCATION_STACK_INDIRECT, maps.LOCATION_MANAGED,
        register=29 if arch == maps.ARCH_AARCH64 else 6, offset=-8,
    ),)
    functions = tuple(maps.FunctionStackMap(
        function_id=number, function_address=number * 4096,
        code_size=max(4, records * 4), frame_size=16,
        records=tuple(maps.SafepointRecord(
            number * 10000 + index + 1, index * 4, maps.SAFEPOINT_CALL,
            roots if index % 3 else (),
        ) for index in range(records)),
    ) for number in ids)
    return maps.encode_stack_map(maps.PreciseStackMap(arch, functions))


def _trace(payloads, *, consume=False, retained=123):
    budget = _budget(100_000_000)
    budget.retained_bytes = retained
    checks = []
    original = budget.check_peak

    def check(raw, *, records=0, entries=0, label="stack-map"):
        estimate = raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
        if records:
            estimate += budget._set_bytes(records)
        checks.append((label, raw, records, entries, estimate))
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = check
    owned = [bytes(memoryview(payload)) for payload in payloads] if consume else tuple(payloads)
    result = maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=consume)
    return result, checks, budget, owned


@pytest.mark.parametrize("arch", [maps.ARCH_AARCH64, maps.ARCH_X86_64])
@pytest.mark.parametrize("empty_roots", [False, True])
@pytest.mark.parametrize("records", [0, 4, 64])
def test_consuming_merge_preserves_bytes_offsets_roots_and_record_work(arch, empty_roots, records):
    payloads = (_payload((20, 40), arch=arch, records=records, empty_roots=empty_roots),
                _payload((10, 30), arch=arch, records=records, empty_roots=empty_roots))
    expected, _, old_budget, ordinary = _trace(payloads)
    actual, _, new_budget, owned = _trace(payloads, consume=True)
    assert actual == expected
    assert owned == [None, None] and ordinary == payloads
    assert new_budget.retained_bytes == old_budget.retained_bytes == 123
    assert new_budget.record_visits == old_budget.record_visits == records * 4
    decoded = maps.decode_stack_map(actual[0])
    assert [function.function_id for function in decoded.functions] == [10, 20, 30, 40]
    assert all(function.function_address == 0 for function in decoded.functions)
    assert tuple(number for number, _offset in actual[1]) == (10, 20, 30, 40)


def test_empty_function_payload_is_retired_and_preserves_merge():
    payloads = (_payload(()), _payload((1,)), _payload(()))
    expected, _, _, _ = _trace(payloads)
    actual, _, _, owned = _trace(payloads, consume=True)
    assert actual == expected and owned == [None, None, None]


@pytest.mark.parametrize("invalid", ["tuple", "list-subclass", "bytearray", "bytes-subclass", "memoryview", "no-budget"])
def test_consuming_requires_exact_owned_host_list_and_explicit_budget(invalid):
    class ListSubclass(list):
        pass

    class BytesSubclass(bytes):
        pass

    payload = _payload((1,))
    owned = [payload]
    budget = _budget()
    if invalid == "tuple":
        owned = tuple(owned)
    elif invalid == "list-subclass":
        owned = ListSubclass(owned)
    elif invalid == "bytearray":
        owned = [bytearray(payload)]
    elif invalid == "bytes-subclass":
        owned = [BytesSubclass(payload)]
    elif invalid == "memoryview":
        owned = [memoryview(payload)]
    elif invalid == "no-budget":
        budget = None
    before = list(owned)
    with pytest.raises(maps.PreciseStackMapError, match="requires owned host bytes"):
        maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=True)
    assert list(owned) == before
    if budget is not None:
        assert budget.record_visits == 0


def test_empty_collection_keeps_original_error_and_no_mutation():
    owned = []
    with pytest.raises(maps.PreciseStackMapError, match="cannot merge an empty"):
        maps.merge_stack_map_payloads(owned, compact_budget=_budget(), _consume_payloads=True)
    assert owned == []


def test_non_cpython_consumption_is_rejected_without_changing_tuple_path(monkeypatch):
    payloads = (_payload((1,)), _payload((2,)))
    expected = maps.merge_stack_map_payloads(payloads)
    runtime = SimpleNamespace(**vars(sys.implementation))
    runtime.name = "pcc"
    monkeypatch.setattr(sys, "implementation", runtime)
    owned = list(payloads)
    with pytest.raises(maps.PreciseStackMapError, match="requires owned host bytes"):
        maps.merge_stack_map_payloads(owned, compact_budget=_budget(), _consume_payloads=True)
    assert owned == list(payloads)
    assert maps.merge_stack_map_payloads(payloads) == expected


@pytest.mark.parametrize("failure", ["truncated", "mixed-arch", "duplicate", "bad-index", "record-work"])
def test_partial_failure_retires_only_completed_payload_and_preserves_error(failure):
    first, second = _payload((1,)), _payload((2,))
    if failure == "truncated":
        second = second[:12]
    elif failure == "mixed-arch":
        second = _payload((2,), arch=maps.ARCH_X86_64)
    elif failure == "duplicate":
        second = _payload((1,))
    elif failure == "bad-index":
        broken = bytearray(second)
        start = maps._HEADER.size + maps._FUNCTION.size + maps._RECORD.size + 28
        broken[start:start + 4] = (0xFFFFFFFF).to_bytes(4, "little")
        second = bytes(broken)
    errors = []
    for consume in (False, True):
        budget = _budget()
        budget.retained_bytes = 137
        if failure == "record-work":
            budget.record_visit_limit = 4
        owned = [bytes(memoryview(first)), bytes(memoryview(second))] if consume else [first, second]
        with pytest.raises(maps.PreciseStackMapError) as caught:
            maps.merge_stack_map_payloads(owned if consume else tuple(owned),
                                         compact_budget=budget, _consume_payloads=consume)
        errors.append((type(caught.value), str(caught.value)))
        assert owned == [None if consume else first, second]
        assert budget.retained_bytes == 137
    assert errors[0] == errors[1]


def test_each_completed_raw_owner_is_removed_after_its_last_read_only():
    payloads = (_payload((1, 4)), _payload((2, 5)), _payload((3, 6)))
    _, ordinary, _, _ = _trace(payloads)
    _, consumed, _, _ = _trace(payloads, consume=True)
    assert len(ordinary) == len(consumed)
    completed = 0
    input_index = -1
    function_bytes = 0
    table_locations = 0
    for old, new in zip(ordinary, consumed):
        label, raw, records, entries, estimate = old
        assert (new[0], new[2]) == (label, records)
        if label == "input":
            input_index += 1
            completed = sum(map(len, payloads[:input_index]))
        if label == "merge join":
            completed = sum(map(len, payloads))
        reduction = completed if label in ("merge function", "merge cache", "merge join") else 0
        # Construction has no final joined allocation. These fixture
        # functions all contain four records and share one nonempty root.
        if label == "merge function":
            function_bytes += maps.FUNCTION_SIZE + 4 * maps.RECORD_SIZE
            reduction += maps.HEADER_SIZE + function_bytes + table_locations * maps.LOCATION_SIZE
        elif label == "merge cache":
            reduction += maps.HEADER_SIZE + function_bytes + (table_locations + 1) * maps.LOCATION_SIZE
            table_locations = 1
        assert raw - new[1] == reduction
        if label in ("merge function", "merge cache"):
            # The complete current scan, including not-yet-merged functions,
            # now has an explicit consuming-only metadata reservation.
            assert new[3] == entries + 2
            assert estimate - new[4] == reduction - 2 * _budget().metadata_entry_bytes
        elif label != "input":
            assert new[3] == entries
            assert estimate - new[4] == reduction


def test_actual_slot_and_loop_references_retire_before_the_next_scan(monkeypatch):
    kept = _payload((1,))
    owned = [kept, _payload((2,))]
    baseline_refs = sys.getrefcount(kept)
    budget = _budget()
    # This intentional external owner is not transferred and stays charged.
    budget.retained_bytes = sys.getsizeof(kept)
    original = maps._scan_stack_map_payload
    scans = 0

    def scan(payload, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            assert owned[0] is None
            assert sys.getrefcount(kept) == baseline_refs - 1
            assert budget.retained_bytes == (
                sys.getsizeof(kept) + maps.FUNCTION_SIZE + 4 * maps.RECORD_SIZE
                + maps.LOCATION_SIZE + 5 * budget.metadata_entry_bytes
            )
        return original(payload, **kwargs)

    monkeypatch.setattr(maps, "_scan_stack_map_payload", scan)
    maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=True)
    assert scans == 2 and owned == [None, None]
    assert sys.getrefcount(kept) == baseline_refs - 1
    assert budget.retained_bytes == sys.getsizeof(kept)


def test_repeated_payload_alias_stays_live_and_charged_until_its_own_slot(monkeypatch):
    payload = _payload((1,))
    owned = [payload, payload]
    budget = _budget()
    budget.retained_bytes = sys.getsizeof(payload)
    original = maps._scan_stack_map_payload
    scans = 0

    def scan(value, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            assert owned == [None, payload]
            assert value is payload
            assert budget.retained_bytes == (
                sys.getsizeof(payload) + maps.FUNCTION_SIZE + 4 * maps.RECORD_SIZE
                + maps.LOCATION_SIZE + 5 * budget.metadata_entry_bytes
            )
        return original(value, **kwargs)

    monkeypatch.setattr(maps, "_scan_stack_map_payload", scan)
    with pytest.raises(maps.PreciseStackMapError, match="duplicate stack-map function"):
        maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=True)
    assert scans == 2 and owned == [None, payload]
    assert budget.retained_bytes == sys.getsizeof(payload)


def test_real_owner_retirement_admits_at_unchanged_tight_limit():
    payloads = tuple(_payload((number,), records=64) for number in range(1, 5))
    expected, ordinary, _, _ = _trace(payloads)
    actual, consumed, _, _ = _trace(payloads, consume=True)
    assert actual == expected
    limit = max(item[4] for item in consumed)
    assert limit < max(item[4] for item in ordinary)
    budget = _budget(limit)
    budget.retained_bytes = 123
    owned = [bytes(memoryview(payload)) for payload in payloads]
    assert maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=True) == expected
    assert owned == [None] * len(payloads)
    budget = _budget(limit)
    budget.retained_bytes = 123
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded"):
        maps.merge_stack_map_payloads(payloads, compact_budget=budget)
    budget = _budget(limit - 1)
    budget.retained_bytes = 123
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded"):
        maps.merge_stack_map_payloads([bytes(memoryview(payload)) for payload in payloads],
                                     compact_budget=budget, _consume_payloads=True)


@pytest.mark.parametrize("single_buffer", [False, True])
def test_host_transfers_staged_inputs_and_preserves_full_image(tmp_path, monkeypatch, single_buffer):
    paths = _write_sparse_stack_inputs(tmp_path)
    original = host.merge_staged_stack_map_payloads
    transfers = []

    def merge(payloads, **kwargs):
        assert type(payloads) is host._StagedStackMapInputs and payloads.sealed
        assert all(type(row) is tuple and len(row) == 2 for row in payloads.rows)
        assert kwargs["single_buffer"] is single_buffer
        transferred = len(payloads)
        result = original(payloads, **kwargs)
        assert payloads.rows == [] and payloads.stream is None
        assert payloads.consumed == transferred and payloads.read == payloads.written == payloads.total
        transfers.append(transferred)
        return result

    monkeypatch.setattr(host, "merge_staged_stack_map_payloads", merge)
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: single_buffer)
    budget = _budget(20_000_000)
    budget.retained_bytes = 1024
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    eager = macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        _consume_inputs=True, _source_view=True, compact_budget=_budget(20_000_000),
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=_budget(20_000_000))
    assert actual == expected and transfers == [3]
    assert budget.retained_bytes == 1024


def test_host_clears_partial_transfer_and_restores_budget_on_failure(tmp_path, monkeypatch):
    paths = _write_sparse_stack_inputs(tmp_path)
    original_merge = host.merge_staged_stack_map_payloads
    original_scan = maps._scan_stack_map_payload
    transferred = []
    scans = 0

    def merge(payloads, **kwargs):
        transferred.append(payloads)
        return original_merge(payloads, **kwargs)

    def scan(payload, **kwargs):
        nonlocal scans
        if not transferred:
            return original_scan(payload, **kwargs)
        scans += 1
        if scans == 2:
            assert transferred[0].rows[0] is None
            raise maps.PreciseStackMapError("injected second payload refusal")
        return original_scan(payload, **kwargs)

    monkeypatch.setattr(host, "merge_staged_stack_map_payloads", merge)
    monkeypatch.setattr(maps, "_scan_stack_map_payload", scan)
    budget = _budget(20_000_000)
    budget.retained_bytes = 1024
    with pytest.raises(macho_link.LinkError, match="injected second payload refusal"):
        host.prepare_executable_from_paths(paths, compact_budget=budget)
    assert scans == 2 and len(transferred) == 1
    assert transferred[0].rows == [] and transferred[0].stream is None
    assert budget.retained_bytes == 1024
