"""Version 3 runtime stack-map tables (stack-map step 1).

A merged version 2 table is transcoded into the compact runtime form for a
final executable. These tests check that the form preserves every frame's root
set, pc and exception/continuation fields, that the validators and scanners
accept it, and that malformed tables are refused.
"""
from __future__ import annotations

import struct

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    ARCH_X86_64,
    LOCATION_MANAGED,
    LOCATION_OWNED,
    LOCATION_STACK_INDIRECT,
    NO_OFFSET,
    RECORD_HAS_EXCEPTION_EDGE,
    RECORD_SUSPENDED,
    SAFEPOINT_CALL,
    SAFEPOINT_CONTINUATION,
    SAFEPOINT_ENTRY,
    SAFEPOINT_EXCEPTION,
    SAFEPOINT_LOOP,
    FunctionStackMap,
    PreciseStackMap,
    PreciseStackMapError,
    SafepointRecord,
    StackMapLocation,
    decode_runtime_table,
    decode_stack_map,
    encode_stack_map,
    function_address_offsets,
    transcode_runtime_table,
    validate_runtime_table,
    validate_stack_map_payload,
)


def _slot(offset, owned=False, register=29):
    flags = LOCATION_MANAGED | (LOCATION_OWNED if owned else 0)
    return StackMapLocation(LOCATION_STACK_INDIRECT, flags, register=register, offset=offset)


def _map(arch=ARCH_AARCH64, register=29):
    shared = (_slot(-16, register=register), _slot(-24, owned=True, register=register))
    first = FunctionStackMap(
        function_id=11, function_address=0, code_size=200, frame_size=48,
        records=(
            SafepointRecord(1, 0, SAFEPOINT_ENTRY, ()),
            SafepointRecord(2, 20, SAFEPOINT_CALL, shared),
            SafepointRecord(3, 40, SAFEPOINT_LOOP, shared),
            SafepointRecord(
                4, 60, SAFEPOINT_EXCEPTION, (_slot(-8, register=register),),
                flags=RECORD_HAS_EXCEPTION_EDGE, exceptional_offset=120,
            ),
        ),
    )
    second = FunctionStackMap(
        function_id=29, function_address=0, code_size=96, frame_size=32,
        records=(
            SafepointRecord(5, 12, SAFEPOINT_CALL, shared),
            SafepointRecord(
                6, 44, SAFEPOINT_CONTINUATION, (_slot(-32, owned=True, register=register),),
                flags=RECORD_SUSPENDED, continuation_id=7,
            ),
        ),
    )
    return PreciseStackMap(arch=arch, functions=(first, second))


def _address_offsets(payload):
    decoded = decode_stack_map(payload, expected_arch=None, final_image=False)
    return tuple(zip((function.function_id for function in decoded.functions),
                     function_address_offsets(payload)))


@pytest.mark.parametrize("arch, register", [(ARCH_AARCH64, 29), (ARCH_X86_64, 6)])
def test_transcoding_preserves_every_frames_roots_and_fields(arch, register):
    value = _map(arch, register)
    payload = encode_stack_map(value)
    runtime, remapped = transcode_runtime_table(payload, _address_offsets(payload))
    functions = decode_runtime_table(runtime, expected_arch=arch)
    assert [function[0] for function in functions] == [11, 29]
    for original, (function_id, address, code_size, frame_size, records) in zip(value.functions, functions):
        assert (function_id, address, code_size, frame_size) == (
            original.function_id, 0, original.code_size, original.frame_size,
        )
        assert len(records) == len(original.records)
        for record, (pc, roots, kind, flags, exceptional, continuation) in zip(original.records, records):
            assert pc == record.instruction_offset
            assert roots == tuple((loc.offset, bool(loc.flags & LOCATION_OWNED)) for loc in record.locations)
            plain = record.kind in (SAFEPOINT_ENTRY, SAFEPOINT_LOOP, SAFEPOINT_CALL) and not record.flags
            assert kind == (0 if plain else record.kind)
            assert (flags, exceptional, continuation) == (
                record.flags, record.exceptional_offset, record.continuation_id,
            )
    # The address relocation still sits at function entry start + 8.
    assert remapped == tuple(zip((11, 29), function_address_offsets(runtime)))
    assert function_address_offsets(runtime) == (40 + 8, 40 + 32 + 8)


def test_runtime_table_is_smaller_and_deduplicates_live_sets():
    payload = encode_stack_map(_map())
    runtime, _remapped = transcode_runtime_table(payload, _address_offsets(payload))
    assert len(runtime) < len(payload)
    header = struct.unpack_from("<8sHBBIIIIIII", runtime, 0)
    function_count, record_count, live_sets, locations, rare = header[4:9]
    assert (function_count, record_count) == (2, 6)
    # (), shared, (-8,) and (-32 owned,): the shared pair is stored once.
    assert (live_sets, locations, rare) == (4, 4, 2)


def test_publication_validators_accept_a_final_runtime_table():
    payload = encode_stack_map(_map())
    runtime, remapped = transcode_runtime_table(payload, _address_offsets(payload))
    final = bytearray(runtime)
    for _function_id, offset in remapped:
        struct.pack_into("<Q", final, offset, 0x100004000 + offset)
    validate_stack_map_payload(bytes(final), expected_arch=ARCH_AARCH64, final_image=True)
    with pytest.raises(PreciseStackMapError, match="unresolved function address"):
        validate_stack_map_payload(runtime, expected_arch=ARCH_AARCH64, final_image=True)
    count, scanned, _start, _table = wire._scan_stack_map_payload(runtime)
    assert count == 2 and [fid for fid, _s, _e in scanned] == [11, 29]


def test_transcoding_refuses_a_location_the_runtime_form_cannot_carry():
    # Valid version 2 (stack-pointer relative), but not the frame-pointer
    # slot every producer emits.
    sp_root = _slot(-16, register=31)
    value = PreciseStackMap(arch=ARCH_AARCH64, functions=(FunctionStackMap(
        function_id=3, function_address=0, code_size=64, frame_size=16,
        records=(SafepointRecord(1, 8, SAFEPOINT_CALL, (sp_root,)),),
    ),))
    payload = encode_stack_map(value)
    with pytest.raises(PreciseStackMapError, match="managed frame-relative slots"):
        transcode_runtime_table(payload, _address_offsets(payload))


def _runtime():
    payload = encode_stack_map(_map())
    runtime, _remapped = transcode_runtime_table(payload, _address_offsets(payload))
    return bytearray(runtime)


def test_runtime_validator_refuses_malformed_tables():
    records_base = 40 + 2 * 32
    unordered = _runtime()
    struct.pack_into("<I", unordered, records_base + 8 * 2, 10)  # pc 40 -> 10 after 20
    with pytest.raises(PreciseStackMapError, match="invalid runtime safepoint record"):
        validate_runtime_table(bytes(unordered))
    bad_live_set = _runtime()
    struct.pack_into("<I", bad_live_set, records_base + 4, 99)
    with pytest.raises(PreciseStackMapError, match="invalid runtime safepoint record"):
        validate_runtime_table(bytes(bad_live_set))
    with pytest.raises(PreciseStackMapError, match="size disagrees"):
        validate_runtime_table(bytes(_runtime() + b"\0\0\0\0"))
    wrong_first = _runtime()
    struct.pack_into("<I", wrong_first, 40 + 32 + 24, 3)  # second function's first record
    with pytest.raises(PreciseStackMapError, match="invalid runtime function entry"):
        validate_runtime_table(bytes(wrong_first))


def test_format_policy_accepts_only_two_and_three(monkeypatch):
    monkeypatch.delenv(wire.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    assert wire.precise_stackmap_runtime_format() == 3
    monkeypatch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, "2")
    assert wire.precise_stackmap_runtime_format() == 2
    monkeypatch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, "3")
    assert wire.precise_stackmap_runtime_format() == 3
    monkeypatch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, "4")
    with pytest.raises(PreciseStackMapError):
        wire.precise_stackmap_runtime_format()
