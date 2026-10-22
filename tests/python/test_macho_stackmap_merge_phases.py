"""Consuming merge admissions describe mutually exclusive byte-owner phases."""

import sys

import pytest

from pcc.backend import precise_stackmap as maps
from tests.python.test_macho_stackmap_consumption import _payload
from tests.python.test_macho_windowed_prepare import _budget


def _merge_with_owner_checks(payloads, *, consume=True, limit=100_000_000):
    budget = _budget(limit)
    budget.retained_bytes = 137
    checks = []
    original = budget.check_peak
    # The oracle retains its own bytes. Transfer detached exact bytes only.
    owned = [bytes(memoryview(payload)) for payload in payloads]
    inputs = owned if consume else tuple(owned)

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if consume and label == "input":
            scan_frame = sys._getframe(1)
            merge_frame = scan_frame.f_back
            assert scan_frame.f_code is maps._scan_stack_map_payload.__code__
            assert merge_frame.f_code is maps.merge_stack_map_payloads.__code__
            state = merge_frame.f_locals
            assert state.get("scanned") is None
            assert state.get("source_index_cache") is None
            assert "functions" not in scan_frame.f_locals
            assert entries == scan_frame.f_locals["function_count"]
            retained_raw = (sum(len(value) for value in inputs if value is not None) - raw
                            + sum(len(record) for _, record in state["functions"])
                            + sum(map(len, state["table"])))
            retained_entries = 4 * len(state["functions"]) + len(state["table_index"])
            assert budget.retained_bytes == 137 + retained_raw + retained_entries * budget.metadata_entry_bytes
            del state, scan_frame, merge_frame
        if label in ("merge function", "merge cache", "merge join"):
            frame = sys._getframe(1)
            assert frame.f_code is maps.merge_stack_map_payloads.__code__
            state = frame.f_locals
            remaining = sum(len(value) for value in inputs if value is not None)
            assert state["input_bytes"] == remaining
            functions = state["functions"]
            table = state["table"]
            table_index = state["table_index"]
            assert all(type(record) is bytes for _, record in functions)
            assert all(type(key) is bytes for key in table)
            assert {id(key) for key in table_index} == {id(key) for key in table}
            assert sum(map(len, table)) == state["table_locations"] * maps.LOCATION_SIZE
            # No output buffer or join-pointer list exists at any admission.
            assert all(name not in state for name in ("header", "chunks", "address_offsets"))
            assert state["source_table"] is None
            if label == "merge join":
                assert state["source_index_cache"] is None
                assert state["scanned"] is None
                assert state["key"] is None
                assert state["seen_ids"] is None
                assert "blob" not in state
                if consume:
                    assert inputs == [None] * len(payloads)
                    assert state["payload"] is None
                output_size = maps.HEADER_SIZE + state["function_bytes"] + sum(map(len, table))
                assert state["function_bytes"] == sum(len(record) for _, record in functions)
                assert raw == remaining + 2 * output_size
            else:
                current_size = state["fn_end"] - state["fn_start"]
                assert state["function_bytes"] == sum(len(record) for _, record in functions) + current_size
                assert all(record is not state["payload"] for _, record in functions)
                assert all(key is not state["payload"] for key in table)
                future_root = state["count"] * maps.LOCATION_SIZE if label == "merge cache" else 0
                output_size = (maps.HEADER_SIZE + state["function_bytes"]
                               + sum(map(len, table)) + future_root)
                copies = 1 if consume else 2
                # Current function slice/freeze and the prospective key keep
                # all their old allowances. Only a nonexistent joined copy
                # differs from the ordinary tuple reservation.
                assert raw == remaining + copies * output_size + 2 * current_size + future_root
                scan_entries = len(state["scanned"]) if consume else 0
                assert entries == (4 * (len(functions) + 1) + len(state["source_index_cache"])
                                   + len(table_index) + scan_entries
                                   + (2 if label == "merge cache" else 0))
                if label == "merge cache":
                    assert type(state["blob"]) is bytearray
                    assert len(state["blob"]) == current_size
                    assert all(type(key) is int and type(value) is int
                               for key, value in state["source_index_cache"].items())
            # Save numbers only; no test-held frame or bytes survive a check.
            del state, frame, functions, table, table_index
        estimate = raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
        checks.append((label, raw, records, entries, estimate))
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = check
    result = maps.merge_stack_map_payloads(inputs, compact_budget=budget, _consume_payloads=consume)
    assert budget.retained_bytes == 137
    return result, checks, budget, inputs


@pytest.mark.parametrize("arch", (maps.ARCH_AARCH64, maps.ARCH_X86_64))
@pytest.mark.parametrize("records", (0, 4, 64))
@pytest.mark.parametrize("empty_roots", (False, True))
def test_live_owners_have_one_construction_copy_and_two_join_copies(arch, records, empty_roots):
    inputs = tuple(_payload(ids, arch=arch, records=records, empty_roots=empty_roots)
                   for ids in ((2, 5), (), (1, 3, 4)))
    expected = maps.merge_stack_map_payloads(inputs)
    result, checks, budget, owned = _merge_with_owner_checks(inputs)
    ordinary, old_checks, old_budget, old_inputs = _merge_with_owner_checks(inputs, consume=False)
    assert result == ordinary == expected
    assert owned == [None] * len(inputs) and old_inputs == inputs
    assert budget.record_visits == old_budget.record_visits == 5 * records
    assert [(label, records) for label, _, records, _, _ in checks] == [
        (label, records) for label, _, records, _, _ in old_checks
    ]


@pytest.mark.parametrize("records", (64, 256))
def test_exact_consuming_cache_limit_preserves_scratch_and_rejects_one_byte_less(records):
    inputs = (_payload((1,), records=records),)
    expected, checks, _, _ = _merge_with_owner_checks(inputs)
    cache = [check for check in checks if check[0] == "merge cache"]
    assert len(cache) == 1
    limit = cache[0][4]
    assert limit == max(check[4] for check in checks)
    result, _, _, _ = _merge_with_owner_checks(inputs, limit=limit)
    assert result == expected
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded: merge cache"):
        _merge_with_owner_checks(inputs, limit=limit - 1)
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded"):
        _merge_with_owner_checks(inputs, consume=False, limit=limit)


def test_final_join_still_requires_both_complete_output_copies():
    # Empty roots and small individual functions make the final joined
    # allocation, rather than any earlier scratch allocation, the peak.
    inputs = tuple(_payload((number,), records=32, empty_roots=True)
                   for number in range(1, 17))
    expected, checks, _, _ = _merge_with_owner_checks(inputs)
    join, = [check for check in checks if check[0] == "merge join"]
    limit = join[4]
    assert limit == max(check[4] for check in checks)
    assert join[1] == 2 * len(expected[0])
    result, _, _, _ = _merge_with_owner_checks(inputs, limit=limit)
    assert result == expected
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded: merge join"):
        _merge_with_owner_checks(inputs, limit=limit - 1)


def test_many_function_scan_refuses_before_allocating_descriptors(monkeypatch):
    first = _payload(tuple(range(1, 257)), records=0)
    second = _payload((1000,), records=4096)
    owned = [bytes(memoryview(first)), bytes(memoryview(second))]
    budget = _budget()
    caller_retained = 137
    budget.retained_bytes = caller_retained
    budget.limit_bytes = caller_retained + len(first) + len(second) + 256 * budget.metadata_entry_bytes - 1
    original = budget.check_peak
    admitted = []

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if label == "input":
            frame = sys._getframe(1)
            assert frame.f_code is maps._scan_stack_map_payload.__code__
            assert "functions" not in frame.f_locals
            assert raw == len(first) and entries == 256
            assert budget.retained_bytes == caller_retained + len(second)
            del frame
        admitted.append(label)
        return original(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(budget, "check_peak", check)
    with pytest.raises(maps.PreciseStackMapError, match="byte budget exceeded: input"):
        maps.merge_stack_map_payloads(owned, compact_budget=budget, _consume_payloads=True)
    assert admitted == ["merge inputs", "input"]
    assert owned == [first, second]
    assert budget.retained_bytes == caller_retained
    assert budget.record_visits == 0


def test_duplicate_root_bytes_at_distinct_source_indices_keep_one_owner():
    canonical = _payload((1,), records=4)
    table_start = maps.HEADER_SIZE + maps.FUNCTION_SIZE + 4 * maps.RECORD_SIZE
    root = canonical[table_start:]
    assert len(root) == maps.LOCATION_SIZE
    payload = bytearray(canonical + root * 2)
    payload[16:20] = (3).to_bytes(4, "little")
    for record, index in ((1, 0), (2, 1)):
        start = maps.HEADER_SIZE + maps.FUNCTION_SIZE + record * maps.RECORD_SIZE
        payload[start + 28:start + 32] = index.to_bytes(4, "little")
    # Give the final record the same nonempty roots through a third source
    # index. All three detached slice copies intern into one retained key.
    last = maps.HEADER_SIZE + maps.FUNCTION_SIZE + 3 * maps.RECORD_SIZE
    payload[last + 20:last + 22] = (1).to_bytes(2, "little")
    payload[last + 28:last + 32] = (2).to_bytes(4, "little")
    inputs = (bytes(payload),)
    expected = maps.merge_stack_map_payloads(inputs)
    result, checks, budget, _ = _merge_with_owner_checks(inputs)
    assert result == expected
    assert int.from_bytes(result[0][16:20], "little") == 1
    assert len([check for check in checks if check[0] == "merge cache"]) == 3
    assert budget.record_visits == 4
