"""Bounded PE image-limit diagnostics over real small COFF/PE fixtures."""

import struct

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_x86_64 as pe
from pcc.backend import precise_stackmap as wire
from pcc.backend.pe_host_budget import PEHostBudget


def _object(name, *, maps=True, ordinary=b""):
    sections = [coff.CoffSection(".text", b"\x90" * 15 + b"\xc3", 0x60000020, 16)]
    if maps:
        locations = tuple(wire.StackMapLocation(
            kind=wire.LOCATION_STACK_INDIRECT,
            flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
            register=6, offset=-8 * (index + 1),
        ) for index in range(3))
        records = tuple(wire.SafepointRecord(
            safepoint_id=wire.safepoint_id(name, index, kind),
            instruction_offset=offset, kind=kind, locations=locations,
        ) for index, (offset, kind) in enumerate(((1, wire.SAFEPOINT_ENTRY),
                                                 (4, wire.SAFEPOINT_CALL))))
        payload = wire.encode_stack_map(wire.PreciseStackMap(
            arch=wire.ARCH_X86_64,
            functions=(wire.FunctionStackMap(
                function_id=wire.function_id(name), function_address=0,
                code_size=16, frame_size=32, records=records,
            ),),
        ))
        sections.append(coff.CoffSection(
            ".pcc_stackmaps", payload, 0x40000040, 8,
            (coff.CoffRelocation(wire.function_address_offsets(payload)[0],
                                 0, coff.ADDR64),),
        ))
    if ordinary:
        sections.append(coff.CoffSection(".rodata", ordinary, 0x40000040, 1))
    obj = coff.CoffObject(tuple(sections), (coff.CoffSymbol(name, 1, 0, True, True),))
    return coff.parse_object(coff.emit_object(obj))


def _budget():
    return PEHostBudget(
        10_000_000, 1000, set_header_bytes=10, set_entry_bytes=2,
        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
    )


def _oversized_link(monkeypatch, objects, *, compact_budget=None):
    """Exercise real merge, relocation and validation, then inject only size."""
    original = pe._check_image_size

    def reject(size, sections=(), *, stack_map_bytes=0, stack_map_counts=()):
        assert size < 0x80000000
        # All diagnostic metadata must already be captured. The guard must
        # not decode, scan, copy or reread the map on this failure path.
        def unexpected(*args, **kwargs):
            pytest.fail("image-size failure reread the stack map")

        monkeypatch.setattr(pe, "_stack_map_size_counts", unexpected)
        monkeypatch.setattr(wire, "_HEADER", None)
        monkeypatch.setattr(wire, "_scan_stack_map_payload", unexpected)
        monkeypatch.setattr(wire, "decode_stack_map", unexpected)
        monkeypatch.setattr(wire, "validate_stack_map_payload", unexpected)
        original(0x80001000, sections, stack_map_bytes=stack_map_bytes,
                 stack_map_counts=stack_map_counts)

    monkeypatch.setattr(pe, "_check_image_size", reject)
    with pytest.raises(coff.CoffError, match="exceeds documented 2 GiB limit") as caught:
        pe.link_executable(objects, compact_budget=compact_budget)
    return str(caught.value)


@pytest.mark.parametrize("budgeted", (False, True))
def test_real_pe_failure_reports_exact_physical_partition(monkeypatch, budgeted):
    ordinary = b"private ordinary data\0"
    objects = [_object("pcc_windows_start", ordinary=ordinary), _object("worker")]
    originals = tuple(coff.emit_object(obj) for obj in objects)
    # Two input maps share three physical locations despite twelve logical
    # root references. The diagnostic must describe the merged table only.
    if budgeted:
        monkeypatch.setattr(wire, "MAX_RECORDS", 3)
    message = _oversized_link(
        monkeypatch, objects, compact_budget=_budget() if budgeted else None,
    )
    assert "2147487744 bytes (0x80001000)" in message
    assert "merged_stack_map_bytes=264;" in message
    assert ("merged_stack_map_partition=functions=2 records=4 location_entries=3 "
            "header_bytes=24 function_bytes=64 record_bytes=128 "
            "location_table_bytes=48;") in message
    assert ".rdata@" in message and "bytes=" + str(264 + len(ordinary)) in message
    assert "private ordinary data" not in message
    assert "worker" not in message
    assert originals == tuple(coff.emit_object(obj) for obj in objects)


def test_real_pe_without_maps_reports_absent(monkeypatch):
    message = _oversized_link(monkeypatch, [_object("pcc_windows_start", maps=False)])
    assert "merged_stack_map_bytes=0; merged_stack_map_partition=absent;" in message


def test_partition_capture_reads_only_header_without_payload_copy(monkeypatch):
    payload, _targets = pe._merge_stack_maps([_object("pcc_windows_start")])

    class UnsliceableBytes(bytes):
        def __getitem__(self, key):
            pytest.fail("diagnostic sliced or indexed the payload")

        def __bytes__(self):
            pytest.fail("diagnostic copied the payload")

        def __iter__(self):
            pytest.fail("diagnostic iterated the payload")

    guarded = UnsliceableBytes(payload)
    header = wire._HEADER
    calls = []

    class HeaderReader:
        def unpack_from(self, data, offset):
            assert data is guarded and offset == 0 and header.size == 24
            calls.append(offset)
            return header.unpack_from(data, offset)

    monkeypatch.setattr(wire, "_HEADER", HeaderReader())
    assert pe._stack_map_size_counts(guarded) == (1, 2, 3)
    assert calls == [0]


@pytest.mark.parametrize("change", (
    "absent", "short", "magic", "version", "architecture", "pointer-size",
    "reserved", "functions", "locations", "negative-record-bytes",
    "partial-record", "records-without-functions",
))
def test_bad_partition_header_never_masks_the_image_size_guard(change):
    payload, _targets = pe._merge_stack_maps([_object("pcc_windows_start")])
    malformed = bytearray(payload)
    if change == "absent":
        malformed.clear()
    elif change == "short":
        del malformed[wire.HEADER_SIZE - 1:]
    elif change == "magic":
        malformed[0] = 0
    elif change == "version":
        struct.pack_into("<H", malformed, 8, 1)
    elif change == "architecture":
        malformed[10] = wire.ARCH_AARCH64
    elif change == "pointer-size":
        malformed[11] = 4
    elif change == "reserved":
        struct.pack_into("<I", malformed, 20, 1)
    elif change == "functions":
        struct.pack_into("<I", malformed, 12, wire.MAX_FUNCTIONS + 1)
    elif change == "locations":
        struct.pack_into("<I", malformed, 16, wire.MAX_LOCATIONS + 1)
    elif change == "negative-record-bytes":
        struct.pack_into("<I", malformed, 16, 20)
    elif change == "partial-record":
        malformed.append(0)
    elif change == "records-without-functions":
        struct.pack_into("<I", malformed, 12, 0)
    counts = pe._stack_map_size_counts(bytes(malformed))
    assert counts == ()
    with pytest.raises(coff.CoffError, match="exceeds documented 2 GiB limit") as caught:
        pe._check_image_size(0x80001000, stack_map_bytes=len(malformed),
                             stack_map_counts=counts)
    expected = "absent" if not malformed else "unavailable"
    assert "merged_stack_map_partition=" + expected + ";" in str(caught.value)


@pytest.mark.parametrize("counts", (
    (), None, (1, 2), (1, 2, 3, 4), (1, -2, 3), (1, "2", 3),
    (True, 2, 3), (2, 2, 3), (0, 3, 3),
))
def test_unavailable_or_inconsistent_counts_preserve_original_guard(counts):
    with pytest.raises(coff.CoffError, match="exceeds documented 2 GiB limit") as caught:
        pe._check_image_size(0x80001000, stack_map_bytes=168, stack_map_counts=counts)
    assert "merged_stack_map_bytes=168; merged_stack_map_partition=unavailable;" in str(caught.value)
