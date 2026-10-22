"""Small host-only contracts for explicit compact stack-map budgets.

All fixtures contain at most six records.  The allocation profile is deliberately
synthetic: these tests check accounting and boundaries, not measured host RSS.
No compiler, subprocess, native executable, or sibling-test imports are needed.
"""

from __future__ import annotations

import struct

import pytest

from pcc.backend import macho_exec, macho_obj, macho_spec
from pcc.backend import precise_stackmap as wire
from pcc.backend.macho_exec import prepare_executable_object
from pcc.backend.macho_link import LinkError
from pcc.backend.native_object import NativeObject, OwnedMergedSourceView
from pcc.backend.relocatable_merge import _stack_table


_PROFILE = {
    "set_header_bytes": 10,
    "set_entry_bytes": 2,
    "integer_bytes": 3,
    "metadata_entry_bytes": 7,
    "allocation_quantum": 1,
}
_CONSUMERS = (
    "function_address_offsets",
    "_scan_stack_map_payload",
    "validate_stack_map_payload",
    "merge_stack_map_payloads",
)


def _budget(*, limit_bytes=1_000_000, record_visit_limit=100, **profile):
    return wire.CompactStackMapBudget(
        limit_bytes,
        record_visit_limit,
        **(_PROFILE | profile),
    )


def _root(offset=-8):
    return wire.StackMapLocation(
        kind=wire.LOCATION_STACK_INDIRECT,
        flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
        register=31,
        offset=offset,
    )


def _function(function_id=1, ids=(1, 2, 3), *, roots=None, frame_size=32):
    if roots is None:
        roots = ((),) * len(ids)
    assert len(roots) == len(ids)
    return wire.FunctionStackMap(
        function_id=function_id,
        function_address=0,
        code_size=64,
        frame_size=frame_size,
        records=tuple(
            wire.SafepointRecord(
                safepoint_id=record_id,
                instruction_offset=4 * (index + 1),
                kind=wire.SAFEPOINT_ENTRY,
                locations=roots[index],
            )
            for index, record_id in enumerate(ids)
        ),
    )


def _payload(*functions):
    return wire.encode_stack_map(wire.PreciseStackMap(
        arch=wire.ARCH_AARCH64,
        functions=functions or (_function(),),
    ))


def _consume(consumer, payload, **kwargs):
    if consumer == "merge_stack_map_payloads":
        return wire.merge_stack_map_payloads((payload,), **kwargs)
    return getattr(wire, consumer)(payload, **kwargs)


def _changed(payload, format_string, offset, value):
    result = bytearray(payload)
    struct.pack_into(format_string, result, offset, value)
    return bytes(result)


def _native_stackmap_object(payload):
    address_offsets = wire.function_address_offsets(payload)
    assert len(address_offsets) == 1
    return NativeObject.from_sections([
        macho_obj.Section(
            sectname="__text",
            segname="__TEXT",
            data=b"\xc0\x03\x5f\xd6" * 16,
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(macho_obj.TextSymbol("_main", 0),),
        ),
        macho_obj.Section(
            sectname="__pcc_stackmaps",
            segname="__DATA",
            data=payload,
            align_log2=3,
            flags=macho_obj.PCC_STACKMAP_SECTION_FLAGS,
            relocations=(macho_obj.Relocation(
                offset=address_offsets[0],
                symbol="_main",
                type=macho_spec.ARM64_RELOC_UNSIGNED,
                pcrel=False,
                length=3,
            ),),
        ),
    ])


def _owned_stackmap_source():
    payload = _payload(_function(wire.function_id("_main")))
    inputs = [_native_stackmap_object(payload)]
    view = prepare_executable_object(
        inputs, _consume_inputs=True, _source_view=True,
    )
    assert inputs == []
    assert isinstance(view, OwnedMergedSourceView)
    return view


@pytest.mark.parametrize("field", (
    "limit_bytes", "record_visit_limit", "set_header_bytes",
    "set_entry_bytes", "integer_bytes", "metadata_entry_bytes",
    "allocation_quantum",
))
@pytest.mark.parametrize("value", (0, -1, False, True, 1 << 63, 1.5))
def test_budget_rejects_invalid_profile_fields(field, value):
    arguments = dict(limit_bytes=1_000, record_visit_limit=10, **_PROFILE)
    arguments[field] = value
    with pytest.raises(wire.PreciseStackMapError, match="invalid compact stack-map budget"):
        wire.CompactStackMapBudget(**arguments)


@pytest.mark.parametrize("field", ("raw_bytes", "records", "entries"))
@pytest.mark.parametrize("value", (-1, True, 1 << 63, 1.5))
def test_budget_rejects_invalid_peak_sizes(field, value):
    arguments = dict(raw_bytes=0, records=0, entries=0)
    arguments[field] = value
    with pytest.raises(wire.PreciseStackMapError, match="invalid compact stack-map size"):
        _budget().check_peak(**arguments)


@pytest.mark.parametrize("value", (-1, True, 1 << 63, 1.5))
def test_budget_rejects_invalid_work_charges(value):
    budget = _budget(record_visit_limit=3)
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        budget.charge_records(value)
    assert budget.record_visits == 0


@pytest.mark.parametrize("records,peak", (
    (1, 29), (4, 38), (5, 105), (6, 105),
    (18, 128), (19, 387), (20, 387),
    (78_642, 498_080), (78_643, 1_022_371), (78_644, 1_022_371),
))
def test_set_capacity_staircase_includes_resize_overlap(records, peak):
    # At five entries: 10 + 5*3 + 8*2 + 32*2 = 105.
    # At nineteen: 10 + 19*3 + 32*2 + 128*2 = 387.
    # At 78,643 the >50,000 switch uses 2x growth, not 4x. These
    # cases do only O(log R) integer arithmetic, not an R-element allocation.
    # Immediately after a resize the old-table overlap remains the peak.
    _budget(limit_bytes=peak).check_peak(0, records=records)
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        _budget(limit_bytes=peak - 1).check_peak(0, records=records)


def test_byte_boundary_counts_raw_storage_metadata_and_allocation_rounding():
    # Five records: 10 + 15 + round16(8*3) + round16(32*3) = 153.
    # Add ten raw bytes and two seven-byte metadata entries: 177.
    budget = _budget(limit_bytes=177, set_entry_bytes=3, allocation_quantum=16)
    budget.check_peak(10, records=5, entries=2)
    budget.check_peak(10, records=5, entries=2)  # Cached estimates stay exact.
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        _budget(
            limit_bytes=176, set_entry_bytes=3, allocation_quantum=16,
        ).check_peak(10, records=5, entries=2)
    _budget(limit_bytes=24).check_peak(10, entries=2)
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        _budget(limit_bytes=23).check_peak(10, entries=2)


def test_metadata_count_is_rejected_before_its_large_product():
    with pytest.raises(wire.PreciseStackMapError, match="metadata budget exceeded"):
        _budget(limit_bytes=10).check_peak(0, entries=(1 << 63) - 1)


@pytest.mark.parametrize("value", (-1, True, 1 << 63, 1.5))
def test_budget_rejects_invalid_retained_storage(value):
    budget = _budget()
    budget.retained_bytes = value
    with pytest.raises(wire.PreciseStackMapError, match="invalid compact stack-map size"):
        budget.check_peak(0)


def test_retained_buffers_and_id_set_share_one_exact_byte_allowance():
    budget = _budget(limit_bytes=156)
    assert budget.retained_bytes == 0
    budget.retained_bytes = 27
    # 27 retained + 10 raw + 105 ID-set peak + 2*7 metadata = 156.
    budget.check_peak(10, records=5, entries=2)
    budget.retained_bytes = 28
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        budget.check_peak(10, records=5, entries=2)
    assert budget.record_visits == 0


def test_record_work_boundary_is_cumulative_and_failed_charge_is_atomic():
    budget = _budget(record_visit_limit=3)
    budget.charge_records(0)
    budget.charge_records(2)
    budget.charge_records(1)
    assert budget.record_visits == 3
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        budget.charge_records(1)
    assert budget.record_visits == 3


@pytest.mark.parametrize("consumer", _CONSUMERS)
def test_explicit_budget_admits_three_records_above_tiny_legacy_cap(monkeypatch, consumer):
    payload = _payload()
    expected = _consume(consumer, payload)
    monkeypatch.setattr(wire, "MAX_RECORDS", 2)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map records"):
        _consume(consumer, payload)
    assert _consume(consumer, payload, compact_budget=_budget()) == expected


def test_materialized_decoder_retains_legacy_record_cap(monkeypatch):
    payload = _payload()
    monkeypatch.setattr(wire, "MAX_RECORDS", 2)
    wire.validate_stack_map_payload(payload, compact_budget=_budget())
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map records"):
        wire.decode_stack_map(payload)


def test_shared_elf_coff_stack_table_retains_legacy_record_cap(monkeypatch):
    payload = _payload(_function(wire.function_id("probe")))
    records = ((wire.HEADER_SIZE + 8, 0, "probe", "probe"),)
    inputs = ((payload, records),)
    _stack_table(inputs, wire.ARCH_AARCH64, ValueError)
    monkeypatch.setattr(wire, "MAX_RECORDS", 2)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map records"):
        _stack_table(inputs, wire.ARCH_AARCH64, ValueError)


def test_merge_aggregate_cap_requires_budget_when_individual_inputs_fit(monkeypatch):
    inputs = (_payload(_function(1, (1, 2))), _payload(_function(2, (3,))))
    expected = wire.merge_stack_map_payloads(inputs)
    monkeypatch.setattr(wire, "MAX_RECORDS", 2)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map records"):
        wire.merge_stack_map_payloads(inputs)
    assert wire.merge_stack_map_payloads(inputs, compact_budget=_budget()) == expected


def test_shared_budget_charges_actual_merge_and_two_validation_passes():
    payload = _payload()
    budget = _budget(record_visit_limit=9)
    wire.function_address_offsets(payload, compact_budget=budget)
    wire._scan_stack_map_payload(payload, compact_budget=budget)
    assert budget.record_visits == 0  # Fixed-width skips do not visit records.
    merged, _ = wire.merge_stack_map_payloads((payload,), compact_budget=budget)
    assert budget.record_visits == 3
    wire.validate_stack_map_payload(merged, compact_budget=budget)
    assert budget.record_visits == 9
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        wire.validate_stack_map_payload(merged, compact_budget=budget)
    assert budget.record_visits == 9


def test_validator_record_work_has_an_exact_two_pass_boundary():
    payload = _payload()
    budget = _budget(record_visit_limit=6)
    wire.validate_stack_map_payload(payload, compact_budget=budget)
    assert budget.record_visits == 6
    budget = _budget(record_visit_limit=5)
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        wire.validate_stack_map_payload(payload, compact_budget=budget)
    assert budget.record_visits == 3


@pytest.mark.parametrize("record_id", (1, 1 << 63, (1 << 64) - 1))
def test_unsigned_safepoint_ids_survive_budgeted_validation(record_id):
    payload = _payload(_function(ids=(record_id,)))
    wire.validate_stack_map_payload(payload, compact_budget=_budget())
    assert wire.decode_stack_map(payload).functions[0].records[0].safepoint_id == record_id


def test_zero_safepoint_id_is_rejected_even_for_an_empty_root_slice():
    payload = _changed(_payload(), "<Q", wire.HEADER_SIZE + wire.FUNCTION_SIZE, 0)
    with pytest.raises(wire.PreciseStackMapError, match="safepoint id is outside uint64"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


def test_safepoint_ids_are_unique_across_functions_not_just_within_them():
    payload = _payload(_function(1, (1,)), _function(2, (2,)))
    second_record = wire.HEADER_SIZE + 2 * wire.FUNCTION_SIZE + wire.RECORD_SIZE
    payload = _changed(payload, "<Q", second_record, 1)
    with pytest.raises(wire.PreciseStackMapError, match="duplicate safepoint id"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


@pytest.mark.parametrize("field_offset,format_string,value,error", (
    (8, "<I", 64, "ordered in-function instruction offsets"),
    (16, "<I", 1, "continuation record needs"),
    (24, "<B", 255, "unknown safepoint kind"),
    (25, "<B", 128, "unknown stack-map record flags"),
    (28, "<I", 1, "outside its table"),
))
def test_empty_slice_fast_path_keeps_record_checks(field_offset, format_string, value, error):
    start = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    payload = _changed(_payload(), format_string, start + field_offset, value)
    with pytest.raises(wire.PreciseStackMapError, match=error):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


@pytest.mark.parametrize("damage,error", (
    ("reserved", "non-zero reserved stack-map field"),
    ("range", "outside its table"),
    ("truncated", "truncated safepoint record"),
    ("trailing", "trailing bytes"),
))
def test_structural_pass_wins_over_earlier_semantic_fault(damage, error):
    first_record = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    payload = _changed(_payload(), "<Q", first_record, 0)
    final_record = first_record + 2 * wire.RECORD_SIZE
    if damage == "reserved":
        payload = _changed(payload, "<H", final_record + 22, 1)
    elif damage == "range":
        payload = _changed(payload, "<I", final_record + 28, 1)
    elif damage == "truncated":
        payload = payload[:-1]
    else:
        payload += b"x"
    with pytest.raises(wire.PreciseStackMapError, match=error):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("retained_record_bytes", (0, 1, 31, 95))
def test_budget_does_not_permit_a_truncated_record_span(consumer, retained_record_bytes):
    start = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    payload = _payload()[:start + retained_record_bytes]
    with pytest.raises(wire.PreciseStackMapError, match="truncated safepoint record"):
        _consume(consumer, payload, compact_budget=_budget())


@pytest.mark.parametrize("offset,format_string,value,error", (
    (8, "<H", 999, "unsupported pcc stack-map version"),
    (10, "<B", 255, "unknown stack-map architecture"),
    (11, "<B", 4, "unsupported stack-map pointer size"),
    (12, "<I", wire.MAX_FUNCTIONS + 1, "too many stack-map functions"),
    (16, "<I", wire.MAX_LOCATIONS + 1, "too many stack-map locations"),
))
def test_explicit_budget_keeps_wire_header_bounds(offset, format_string, value, error):
    payload = _changed(_payload(), format_string, offset, value)
    with pytest.raises(wire.PreciseStackMapError, match=error):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


def test_budget_does_not_permit_a_truncated_location_table():
    payload = _payload(_function(ids=(1,), roots=((_root(),),)))
    with pytest.raises(wire.PreciseStackMapError, match="truncated stack-map location table"):
        wire.validate_stack_map_payload(payload[:-1], compact_budget=_budget())


def test_validator_cache_miss_has_exact_byte_boundary_and_repeated_slice_is_free():
    roots = (_root(),)
    payload = _payload(_function(roots=(roots, roots, roots)))
    # Three IDs need 10 + 3*3 + 8*2 = 35 bytes. One function and one
    # cached slice need 2*7; temporary location flags need one integer.
    peak = len(payload) + 35 + 2 * 7 + 3
    wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak))
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded: validation cache"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak - 1))


def test_validator_empty_slices_do_not_allocate_cache_entries():
    payload = _payload()
    peak = len(payload) + 35 + 7
    wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak))
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded: validation$"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak - 1))


def test_same_location_range_in_distinct_frames_charges_distinct_cache_entries():
    roots = ((_root(),),)
    payload = _payload(
        _function(1, (1,), roots=roots, frame_size=32),
        _function(2, (2,), roots=roots, frame_size=64),
    )
    # Two IDs: 10 + 2*3 + 8*2 = 32. Two function entries, two slice
    # entries, and one temporary flags integer are live at the second miss.
    peak = len(payload) + 32 + 4 * 7 + 3
    wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak))
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded: validation cache"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget(limit_bytes=peak - 1))


def test_cache_reuse_does_not_skip_frame_dependent_location_validation():
    roots = ((_root(),),)
    payload = _payload(
        _function(1, (1,), roots=roots, frame_size=64),
        _function(2, (2,), roots=roots, frame_size=32),
    )
    table_start = wire.HEADER_SIZE + 2 * (wire.FUNCTION_SIZE + wire.RECORD_SIZE)
    payload = _changed(payload, "<i", table_start + 8, -48)
    with pytest.raises(wire.PreciseStackMapError, match="exceeds frame size 32"):
        wire.validate_stack_map_payload(payload, compact_budget=_budget())


@pytest.mark.parametrize("record_count", (1, 3))
def test_merger_cache_miss_has_exact_byte_boundary(record_count):
    payload = _payload(_function(
        ids=tuple(range(1, record_count + 1)),
        roots=((_root(),),) * record_count,
    ))
    function_bytes = wire.FUNCTION_SIZE + record_count * wire.RECORD_SIZE
    peak = (
        len(payload)
        + 2 * (wire.HEADER_SIZE + function_bytes + wire.LOCATION_SIZE)
        + 2 * function_bytes + wire.LOCATION_SIZE + 6 * 7
    )
    expected = wire.merge_stack_map_payloads((payload,))
    assert wire.merge_stack_map_payloads(
        (payload,), compact_budget=_budget(limit_bytes=peak),
    ) == expected
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded: merge cache"):
        wire.merge_stack_map_payloads((payload,), compact_budget=_budget(limit_bytes=peak - 1))


def test_merger_empty_slice_needs_no_source_or_table_cache_entries():
    payload = _payload(_function(ids=(1,)))
    function_bytes = wire.FUNCTION_SIZE + wire.RECORD_SIZE
    peak = len(payload) + 2 * (wire.HEADER_SIZE + function_bytes) + 2 * function_bytes + 4 * 7
    assert wire.merge_stack_map_payloads(
        (payload,), compact_budget=_budget(limit_bytes=peak),
    ) == wire.merge_stack_map_payloads((payload,))


def test_merger_distinct_source_indices_share_root_bytes_without_stale_join_charge(monkeypatch):
    root = (_root(),)
    canonical = _payload(_function(roots=(root, root, root)))
    table_start = wire.HEADER_SIZE + wire.FUNCTION_SIZE + 3 * wire.RECORD_SIZE
    root_bytes = canonical[table_start:]
    assert len(root_bytes) == wire.LOCATION_SIZE
    payload = _changed(canonical + root_bytes * 2, "<I", 16, 3)
    for ordinal in range(3):
        record_start = wire.HEADER_SIZE + wire.FUNCTION_SIZE + ordinal * wire.RECORD_SIZE
        payload = _changed(payload, "<I", record_start + 28, ordinal)
    wire.validate_stack_map_payload(payload, compact_budget=_budget())
    expected = wire.merge_stack_map_payloads((payload,))
    budget = _budget()
    original_check = budget.check_peak
    cache_entries = []
    join_entries = []

    def checked_peak(raw_bytes, *, records=0, entries=0, label="stack-map"):
        if label == "merge cache":
            cache_entries.append(entries)
        elif label == "merge join":
            # Only five per-function entries and one interned root shape
            # remain. The three per-source-index entries are retired.
            join_entries.append(entries)
            assert entries == 6
        return original_check(raw_bytes, records=records, entries=entries, label=label)

    monkeypatch.setattr(budget, "check_peak", checked_peak)
    actual = wire.merge_stack_map_payloads((payload,), compact_budget=budget)
    assert actual == expected
    assert actual[0] == canonical
    assert cache_entries == [6, 8, 9]
    assert join_entries == [6]
    assert budget.record_visits == 3


@pytest.mark.parametrize("empty_first", (False, True))
def test_merger_preserves_first_global_empty_index_and_legacy_bytes(empty_first):
    first_roots = ((), (_root(-8),)) if empty_first else ((_root(-8),), ())
    second_roots = ((_root(-16),), ())
    inputs = (
        _payload(_function(1, (1, 2), roots=first_roots)),
        _payload(_function(2, (3, 4), roots=second_roots)),
    )
    expected = wire.merge_stack_map_payloads(inputs)
    actual = wire.merge_stack_map_payloads(inputs, compact_budget=_budget())
    assert actual == expected
    merged, _ = actual
    expected_index = 0 if empty_first else 1
    first_record = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    first_empty = first_record + (0 if empty_first else wire.RECORD_SIZE)
    second_empty = first_record + wire.FUNCTION_SIZE + 3 * wire.RECORD_SIZE
    assert struct.unpack_from("<I", merged, first_empty + 28)[0] == expected_index
    assert struct.unpack_from("<I", merged, second_empty + 28)[0] == expected_index
    wire.validate_stack_map_payload(merged, compact_budget=_budget())


def test_owned_macho_source_view_threads_budget_through_hidden_section_scan(monkeypatch):
    payload = _payload(_function(wire.function_id("_main")))
    canonical, offsets = wire.merge_stack_map_payloads((payload,))
    assert canonical == payload
    runtime, runtime_offsets = wire.transcode_runtime_table(canonical, offsets)
    # A final source view honors the runtime format policy. Keep the same
    # hidden-scan budget assertions for both formats and the production default.
    for table_format in (None, "2", "3"):
        with monkeypatch.context() as patch:
            if table_format is None:
                patch.delenv(wire.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
            else:
                patch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, table_format)
            expected, expected_offsets = (
                (canonical, offsets) if table_format == "2" else (runtime, runtime_offsets)
            )
            source = _native_stackmap_object(payload)
            patch.setattr(wire, "MAX_RECORDS", 2)
            with pytest.raises(LinkError, match="too many stack-map records"):
                prepare_executable_object([source], _consume_inputs=True, _source_view=True)

            original_scan = macho_obj._scan_stack_map_section
            scan_budgets = []

            def checked_scan(data, *, compact_budget=None):
                scan_budgets.append(compact_budget)
                return original_scan(data, compact_budget=compact_budget)

            patch.setattr(macho_obj, "_scan_stack_map_section", checked_scan)
            budget = _budget()
            inputs = [source]
            view = prepare_executable_object(
                inputs,
                _consume_inputs=True,
                _source_view=True,
                compact_budget=budget,
            )
            assert inputs == []
            assert isinstance(view, OwnedMergedSourceView)
            assert scan_budgets and all(value is budget for value in scan_budgets)
            section = next(
                section for section in view.sections()
                if (section["segname_str"], section["sectname_str"]) == ("__DATA", "__pcc_stackmaps")
            )
            assert section["nreloc"] == 1
            assert bytes(view.section_data(section)) == expected
            assert tuple(row["r_address"] for row in view.iter_relocations(section)) == tuple(
                offset for _function_id, offset in expected_offsets
            )
            wire.validate_stack_map_payload(bytes(view.section_data(section)), compact_budget=budget)


@pytest.mark.parametrize("inject_validation_error", (False, True))
def test_final_publication_forwards_budget_and_restores_retained_storage(
    monkeypatch, inject_validation_error,
):
    # This test covers v2's two-pass, six-visit compact-budget accounting and
    # address layout, not v3 accounting. Select that format explicitly.
    monkeypatch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, "2")
    # Construct the real three-record owned source view before lowering the
    # legacy cap. Address relocation and final-image preparation remain real.
    view = _owned_stackmap_source()
    source_bytes = macho_exec._compact_source_raw_bytes(view)
    # Preparation copies each payload into the image and retires it from the
    # single-use view; its relocation rows stay until the image is finished.
    row_bytes = sum(len(rows) for rows in view._raw_relocations)
    assert 0 < row_bytes < source_bytes
    monkeypatch.setattr(wire, "MAX_RECORDS", 2)
    budget = _budget()
    budget.retained_bytes = 17
    validation_calls = []
    finish_calls = []
    original_validate = macho_exec.validate_stack_map_payload

    def checked_validate(
        payload, *, expected_arch=None, final_image=False, compact_budget=None,
    ):
        assert compact_budget is budget
        assert final_image is True
        assert expected_arch == wire.ARCH_AARCH64
        assert compact_budget.retained_bytes > 17 + row_bytes
        assert struct.unpack_from("<Q", payload, wire.HEADER_SIZE + 8)[0] != 0
        validation_calls.append(compact_budget.retained_bytes)
        if inject_validation_error:
            raise wire.PreciseStackMapError("injected final validation failure")
        return original_validate(
            payload,
            expected_arch=expected_arch,
            final_image=final_image,
            compact_budget=compact_budget,
        )

    def checked_finish(
        plan, identifier, phase_callback, *, compact_budget=None,
        retained_source_bytes=0,
    ):
        assert compact_budget is budget
        assert compact_budget.retained_bytes == 17
        assert retained_source_bytes == row_bytes > 0
        assert identifier == b"compact-budget-test"
        assert phase_callback is None
        assert plan[1]  # Real output regions were prepared without an image.
        finish_calls.append(retained_source_bytes)
        return b"prepared-budget-sentinel"

    monkeypatch.setattr(macho_exec, "validate_stack_map_payload", checked_validate)
    monkeypatch.setattr(macho_exec, "_finish_executable_image", checked_finish)
    if inject_validation_error:
        with pytest.raises(LinkError, match="final stack-map table is invalid: injected final validation failure"):
            macho_exec.link_prepared_executable(
                view, identifier=b"compact-budget-test", compact_budget=budget,
            )
        assert finish_calls == []
    else:
        assert macho_exec.link_prepared_executable(
            view, identifier=b"compact-budget-test", compact_budget=budget,
        ) == b"prepared-budget-sentinel"
        assert finish_calls == [row_bytes]
        assert all(len(payload) == 0 for payload in view._payloads)
        assert budget.record_visits == 6
    assert len(validation_calls) == 1
    assert budget.retained_bytes == 17


def test_mutable_section_budget_guard_runs_before_reading_source_payload(monkeypatch):
    view = _owned_stackmap_source()

    def unexpected_payload_read(self, section):
        raise AssertionError("source payload read before the allocation guard")

    monkeypatch.setattr(OwnedMergedSourceView, "section_data", unexpected_payload_read)
    budget = _budget(limit_bytes=1)
    with pytest.raises(LinkError, match="byte budget exceeded: Mach-O mutable section output"):
        macho_exec._prepare_executable_image(view, compact_budget=budget)
    assert budget.record_visits == 0
    assert budget.retained_bytes == 0


def test_image_budget_guard_runs_before_materialization_or_signing(monkeypatch):
    regions = [macho_exec.OutputRegion(0, b"x", "tiny test region")]
    plan = (8, regions, 16, 8)
    phase_calls = []

    def unexpected_materialization(*args, **kwargs):
        raise AssertionError("image materialization before the allocation guard")

    def unexpected_signature(*args, **kwargs):
        raise AssertionError("signature work before the allocation guard")

    monkeypatch.setattr(macho_exec, "materialize_output_buffer", unexpected_materialization)
    monkeypatch.setattr(macho_exec, "build_signature", unexpected_signature)
    with pytest.raises(LinkError, match="byte budget exceeded: Mach-O image materialization"):
        macho_exec._finish_executable_image(
            plan,
            b"compact-budget-test",
            phase_calls.append,
            compact_budget=_budget(limit_bytes=1),
            retained_source_bytes=7,
        )
    assert len(regions) == 1
    assert phase_calls == []
