"""Host-only resource-contract tests; no source compilation or native runtime."""

from dataclasses import replace
import hashlib
import struct

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import precise_stackmap as wire
from pcc.backend import self_backend_precise_stackmaps as plans
from pcc.backend import relocatable_merge


def payload_for_ranges(ranges, *, table_count=3, frames=None, arch=wire.ARCH_X86_64):
    """Write valid v2 records directly, without expanding their shared roots."""
    if frames is None:
        frames = [16 * ((table_count * 8 + 15) // 16)]
    chunks = [wire._HEADER.pack(wire.MAGIC, wire.VERSION, arch, 8, len(frames), table_count, 0)]
    record_id = 1
    for function_index, frame in enumerate(frames):
        chunks.append(wire._FUNCTION.pack(wire.function_id("_start") + function_index, 0, len(ranges) + 1, frame, len(ranges), 0))
        for pc, (index, count) in enumerate(ranges, 1):
            chunks.append(wire._RECORD.pack(record_id, pc, wire.NO_OFFSET, 0, count, 0, wire.SAFEPOINT_CALL, 0, 0, index))
            record_id += 1
    for index in range(table_count):
        chunks.append(wire._LOCATION.pack(wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
                                          8, 6 if arch == wire.ARCH_X86_64 else 29, wire.NO_BASE, -8 * (index + 1), 8))
    return b"".join(chunks)


def elf_object(payload, arch=wire.ARCH_X86_64):
    code_size = wire._FUNCTION.unpack_from(payload, wire.HEADER_SIZE)[2]
    return elf.ElfObject(
        sections=(elf.ElfSection(".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR, 16, b"\x90" * (code_size - 1) + b"\xc3"),
                  elf.ElfSection(".pcc_stackmaps", elf.SHT_PROGBITS, elf.SHF_ALLOC, 8, payload,
                                 relocations=(elf.ElfRelocation(wire.HEADER_SIZE + 8, 1,
                                                              257 if arch == wire.ARCH_AARCH64 else elf.R_X86_64_64),))),
        symbols=(elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, code_size, elf.STB_GLOBAL, elf.STT_FUNC)),
        machine=elf.EM_AARCH64 if arch == wire.ARCH_AARCH64 else elf.EM_X86_64,
    )


@pytest.mark.parametrize("arch", [wire.ARCH_AARCH64, wire.ARCH_X86_64])
def test_repeated_roots_publish_and_merge_without_decode_expansion(arch, monkeypatch):
    payload = payload_for_ranges([(0, 3)] * 4, arch=arch)
    decoded = wire.decode_stack_map(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map locations"):
        wire.decode_stack_map(payload)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map locations"):
        wire.validate_stack_map(decoded)
    assert wire.function_address_offsets(payload) == (wire.HEADER_SIZE + 8,)
    assert wire.validate_stack_map_payload(payload, expected_arch=arch) is None
    merged, offsets = wire.merge_stack_map_payloads((payload,))
    assert merged == payload
    assert offsets == ((wire.function_id("_start"), wire.HEADER_SIZE + 8),)
    obj = elf_object(payload, arch)
    emitted = elf.emit_relocatable(obj)
    reparsed = elf.parse_relocatable(emitted)
    assert next(section.data for section in reparsed.sections if section.name == ".pcc_stackmaps") == payload
    assert hashlib.sha256(payload).hexdigest() == expected_hash
    merged_obj = relocatable_merge.merge_elf_objects([obj])
    assert next(section.data for section in merged_obj.sections if section.name == ".pcc_stackmaps") == payload


def test_default_limit_has_real_compact_counterexample():
    records, roots = 2048, 65535
    payload = payload_for_ranges([(0, roots)] * records, table_count=roots)
    assert records * roots == 134215680 > wire.MAX_LOCATIONS
    assert len(payload) == 1114152
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map locations"):
        wire.decode_stack_map(payload)
    wire.validate_stack_map_payload(payload)
    assert wire.function_address_offsets(payload) == (wire.HEADER_SIZE + 8,)
    merged, _ = wire.merge_stack_map_payloads((payload,))
    assert merged == payload
    assert elf.emit_relocatable(elf_object(payload))


def test_existing_x86_producer_preserves_interned_roots_and_wire_bytes(monkeypatch):
    locations = tuple(plans.PlannedRootLocation(-8 * (index + 1), True) for index in range(3))
    records = tuple(plans.PlannedSafepoint(index + 1, "pc" + str(index + 1), wire.SAFEPOINT_CALL, locations)
                    for index in range(4))
    plan = plans.FunctionStackMapPlan("_start", wire.function_id("_start"), 32, "end", records,
                                     (), (), (), "x86_64-unknown-linux-gnu")
    offsets = {"_start": 0, "end": 5, **{"pc" + str(index): index for index in range(1, 5)}}
    payload, relocations = plans.build_x86_64_stack_map_payload(
        (plan,), offsets, function_symbol=lambda name: name,
        block_label=lambda name, block: name + block,
    )
    assert payload == payload_for_ranges([(0, 3)] * 4)
    assert relocations == ((wire.HEADER_SIZE + 8, "_start"),)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    assert elf.emit_relocatable(elf_object(payload))


def test_compact_relocatable_scope_preserves_unsigned_ids_and_roots(monkeypatch):
    source = bytearray(payload_for_ranges([(0, 3)] * 4))
    first_record = wire.HEADER_SIZE + wire.FUNCTION_SIZE
    struct.pack_into("<Q", source, first_record, (1 << 64) - 1)
    source = bytes(source)
    original = wire.decode_stack_map(source)
    with monkeypatch.context() as context:
        context.setattr(wire, "MAX_LOCATIONS", 4)
        merged, relocations = relocatable_merge._stack_table(
            [(source, [(wire.HEADER_SIZE + 8, 7, "_start", "_scoped")])],
            wire.ARCH_X86_64, elf.ElfError,
        )
    actual = wire.decode_stack_map(merged)
    function = original.functions[0]
    expected = replace(original, functions=(replace(function,
        function_id=wire.function_id("_scoped"),
        records=tuple(replace(record, safepoint_id=wire.scoped_stable_id(
            "relocatable-safepoint", "_scoped", str(record.safepoint_id)))
            for record in function.records)),))
    assert actual == expected
    assert relocations == [(wire.HEADER_SIZE + 8, 7)]


@pytest.mark.parametrize("relative, format_, value, message", [
    (0, "Q", 0, "safepoint id"),
    (8, "I", 5, "ordered in-function"),
    (12, "I", 5, "exceptional successor"),
    (16, "I", 1, "continuation record"),
    (22, "H", 1, "reserved"),
    (24, "B", 0, "unknown safepoint"),
    (25, "B", 4, "record flags"),
    (26, "H", 1, "reserved"),
    (28, "I", 1, "outside its table"),
])
def test_record_checks_survive_shared_reference_expansion(relative, format_, value, message, monkeypatch):
    malformed = bytearray(payload_for_ranges([(0, 3)] * 4))
    struct.pack_into("<" + format_, malformed, wire.HEADER_SIZE + wire.FUNCTION_SIZE + relative, value)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match=message):
        wire.validate_stack_map_payload(bytes(malformed))


@pytest.mark.parametrize("relative, format_, value, message", [
    (0, "B", 0, "unknown location"),
    (1, "B", 0, "raw pointer"),
    (1, "B", 128, "location flags"),
    (2, "H", 4, "one pointer wide"),
    (4, "H", 1, "frame/stack register"),
    (6, "h", 0, "non-derived"),
    (8, "i", -40, "exceeds frame"),
    (8, "i", -7, "not aligned"),
    (12, "I", 16, "one pointer wide"),
])
def test_root_checks_survive_shared_reference_expansion(relative, format_, value, message, monkeypatch):
    malformed = bytearray(payload_for_ranges([(0, 3)] * 4))
    table_start = wire.HEADER_SIZE + wire.FUNCTION_SIZE + 4 * wire.RECORD_SIZE
    struct.pack_into("<" + format_, malformed, table_start + relative, value)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match=message):
        wire.validate_stack_map_payload(bytes(malformed))


@pytest.mark.parametrize("consumer", [wire.function_address_offsets, wire._scan_stack_map_payload, wire.validate_stack_map_payload, wire.decode_stack_map])
def test_physical_table_budget_remains(consumer, monkeypatch):
    payload = payload_for_ranges([(0, 0)], table_count=5)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map locations"):
        consumer(payload)


def test_overlapping_slices_keep_a_validation_work_budget(monkeypatch):
    payload = payload_for_ranges([(0, 4), (1, 4)], table_count=5)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 5)
    with pytest.raises(wire.PreciseStackMapError, match="location validation work"):
        wire.validate_stack_map_payload(payload)
    with pytest.raises(wire.PreciseStackMapError, match="location merge work"):
        wire.merge_stack_map_payloads((payload,))


def test_same_range_in_different_frames_is_charged_and_validated(monkeypatch):
    payload = payload_for_ranges([(0, 3)], frames=[32, 48])
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match="location validation work"):
        wire.validate_stack_map_payload(payload)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 6)
    wire.validate_stack_map_payload(payload)
    malformed = bytearray(payload)
    second = wire.HEADER_SIZE + wire.FUNCTION_SIZE + wire.RECORD_SIZE
    struct.pack_into("<I", malformed, second + 20, 16)
    with pytest.raises(wire.PreciseStackMapError, match="exceeds frame size 16"):
        wire.validate_stack_map_payload(bytes(malformed))


def test_merge_rejects_out_of_table_ranges_before_copying():
    payload = payload_for_ranges([(2, 3)])
    with pytest.raises(wire.PreciseStackMapError, match="outside its table"):
        wire.merge_stack_map_payloads((payload,))


@pytest.mark.parametrize("field, value, message", [("MAX_FUNCTIONS", 0, "functions"), ("MAX_RECORDS", 0, "records")])
@pytest.mark.parametrize("consumer", [wire.function_address_offsets, wire._scan_stack_map_payload, wire.validate_stack_map_payload])
def test_compact_function_and_record_budgets(consumer, field, value, message, monkeypatch):
    payload = payload_for_ranges([(0, 3)])
    monkeypatch.setattr(wire, field, value)
    with pytest.raises(wire.PreciseStackMapError, match="too many stack-map " + message):
        consumer(payload)


def test_merge_aggregate_function_and_record_budgets(monkeypatch):
    first = payload_for_ranges([(0, 3)])
    second = bytearray(first)
    struct.pack_into("<Q", second, wire.HEADER_SIZE, 2)
    struct.pack_into("<Q", second, wire.HEADER_SIZE + wire.FUNCTION_SIZE, 2)
    for limit, message in [("MAX_FUNCTIONS", "functions"), ("MAX_RECORDS", "records")]:
        with monkeypatch.context() as context:
            context.setattr(wire, limit, 1)
            with pytest.raises(wire.PreciseStackMapError, match="too many stack-map " + message):
                wire.merge_stack_map_payloads((first, bytes(second)))


def test_merge_aggregate_work_budget_is_bounded(monkeypatch):
    first = payload_for_ranges([(0, 3)])
    second = bytearray(first)
    struct.pack_into("<Q", second, wire.HEADER_SIZE, 2)
    struct.pack_into("<Q", second, wire.HEADER_SIZE + wire.FUNCTION_SIZE, 2)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 4)
    with pytest.raises(wire.PreciseStackMapError, match="location merge work"):
        wire.merge_stack_map_payloads((first, bytes(second)))
