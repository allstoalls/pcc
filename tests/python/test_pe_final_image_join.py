"""Bounded host PE bytes/allocation controls; no Windows execution claim."""

import ctypes
import hashlib
import inspect
import struct
import sys

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_host_budget as host
from pcc.backend import pe_x86_64 as pe
from pcc.backend import precise_stackmap as wire


# Frozen with the original c09 linker (pe_x86_64.py SHA-256
# 7834a72a60b37be727738616bbad5d0c611bf487f29c773ab62ff5494c3b448d).
# These are whole-image comparisons, not new-budgeted/new-unbudgeted parity.
BASELINE_SHA256 = {
    (False, 0): '4ed39ee6c2a54a83e927a0c8bd9d8b7b2300d450d858f16c33b7ca71442e5a07',
    (False, 1): '4abb46722c55d6f4521f5bd1eba7a3398562704429dc7c89302b972669428678',
    (False, 511): 'c7d622441c73b6568412ff46b2c942de7919cea5e8fe0d02b9a54aeb604e2da1',
    (False, 512): '08a0b94fb32022db31ba90e963a828632ee4d9c0b56af6edeebbae5e1ff41ba2',
    (False, 513): '5063127b7ff661bff0484b1292b11ace3c242164f1faa19ff885c93df05343e8',
    (False, 1024): '2375bb7e43a7d3d86f72356647a865977ef204bf1347d67789d9224b4171182c',
    (True, 0): '8c00899b125b6b9a472e2e06443bc743c560ba4af35ce747be5455ec3aaded76',
    (True, 513): '6f88b3086417e033b9cd06910cbec11d4d2ca14065946abb48c5adedeb284546',
    (True, 32769): '8881cd9fa3d669de5138b9d73ab2adb6abc2829fa096c013b694fb65ccb2814b',
}


def _budget():
    profile = host._host_profile()
    # Callers carry the pcc_gate on a supported CPython allocation profile.
    assert profile is not None, "gate the test on host._host_profile()"
    return host.PEHostBudget(10_000_000, 1000, **profile)


def _fixture(span, rich):
    text = b"\xe8\0\0\0\0\xc3" + b"\x90" * 26 if rich else b"\xc3"
    sections = [
        coff.CoffSection(".text", text, 0x60000020, 16,
                         (coff.CoffRelocation(1, 2, coff.REL32),) if rich else ()),
        coff.CoffSection(".rdata", b"r" * span, 0x40000040, 8),
    ]
    symbols = [coff.CoffSymbol("pcc_windows_start", 1, function=True)]
    if rich:
        symbols.extend((coff.CoffSymbol("worker", 1, 16, False, True),
                        coff.CoffSymbol("ExitProcess", 0),
                        coff.CoffSymbol("unwind", 5, external=False)))
        pdata_relocations = tuple(coff.CoffRelocation(offset, symbol, coff.ADDR32NB)
                                  for offset, symbol in ((0, 1), (4, 1), (8, 3),
                                                         (12, 0), (16, 0), (20, 3)))
        sections.extend((
            coff.CoffSection(".data", struct.pack("<Q", 7), 0xC0000040, 8,
                             (coff.CoffRelocation(0, 0, coff.ADDR64),)),
            coff.CoffSection(".pdata", struct.pack("<6I", 0, 16, 0, 0, 16, 0),
                             0x40000040, 4, pdata_relocations),
            coff.CoffSection(".xdata", b"\x01\0\0\0", 0x40000040, 4),
            coff.CoffSection(".tls", b"tls-data", 0xC0000040, 8),
            coff.CoffSection(".pccinit", b"\0" * 8, 0x40000040, 8,
                             (coff.CoffRelocation(0, 0, coff.ADDR64),)),
            coff.CoffSection(".pccfini", b"\0" * 8, 0x40000040, 8,
                             (coff.CoffRelocation(0, 1, coff.ADDR64),)),
            coff.CoffSection(".zzempty", b"", 0x40000040),
            coff.CoffSection(".zzextra", b"extra", 0x40000040),
        ))
        locations = (wire.StackMapLocation(
            kind=wire.LOCATION_STACK_INDIRECT,
            flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
            register=6, base_index=wire.NO_BASE, offset=-8,
        ),)
        records = tuple(wire.SafepointRecord(
            safepoint_id=wire.safepoint_id("pcc_windows_start", index, kind),
            instruction_offset=offset, kind=kind, locations=locations,
        ) for index, (offset, kind) in enumerate(((0, wire.SAFEPOINT_ENTRY),
                                                 (5, wire.SAFEPOINT_CALL))))
        table = wire.encode_stack_map(wire.PreciseStackMap(
            arch=wire.ARCH_X86_64, functions=(wire.FunctionStackMap(
                function_id=wire.function_id("pcc_windows_start"), function_address=0,
                code_size=16, frame_size=16, records=records,
            ),),
        ))
        sections.append(coff.CoffSection(".pcc_stackmaps", table, 0x40000040, 8,
                        (coff.CoffRelocation(wire.HEADER_SIZE + 8, 0, coff.ADDR64),)))
    return coff.CoffObject(tuple(sections), tuple(symbols))


def _section_rows(image):
    optional = struct.unpack_from("<I", image, 60)[0] + 24
    count = struct.unpack_from("<H", image, optional - 18)[0]
    rows = []
    for index in range(count):
        name, length, rva, size, raw = struct.unpack_from(
            "<8sIIII", image, optional + 240 + 40 * index,
        )
        rows.append((name.rstrip(b"\0"), length, rva, size, raw))
    return optional, rows


def _assert_frozen_original_bytes_except_tls_alignment(image, rich, span):
    baseline_image = image
    if rich:
        _optional, original_rows = _section_rows(image)
        tlsdir_raw = next(raw for name, _length, _rva, _size, raw in original_rows
                          if name == b".tlsdir")
        assert struct.unpack_from("<I", image, tlsdir_raw + 36)[0] == 0x00400000
        # The old linker omitted TLS alignment. Preserve its whole-image oracle
        # for every other byte rather than replacing the frozen baseline hashes.
        baseline_image = (image[:tlsdir_raw + 36] + b"\0" * 4
                          + image[tlsdir_raw + 40:])
    assert hashlib.sha256(baseline_image).hexdigest() == BASELINE_SHA256[rich, span]


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("rich,span", (
    (False, 0), (False, 1), (False, 511), (False, 512), (False, 513), (False, 1024),
    (True, 0), (True, 513), (True, 32769),
))
def test_final_image_matches_frozen_original_except_tls_alignment(rich, span):
    obj = _fixture(span, rich)
    original = coff.emit_object(obj)
    value = _budget()
    image = pe.link_executable((obj,), compact_budget=value)
    assert type(image) is bytes
    _assert_frozen_original_bytes_except_tls_alignment(image, rich, span)
    assert coff.emit_object(obj) == original
    assert value.retained_bytes == 0 and not value._buffers
    # The stack-map payloads are merged and the merged table validated
    # (fa5628cc5): two record passes beyond the per-object validation.
    assert value.record_visits == (16 if rich else 0)
    optional, rows = _section_rows(image)
    assert (1 + 2 * len(rows) > 10) == rich
    cursor = struct.unpack_from("<I", image, optional + 60)[0]
    for _name, length, _rva, size, raw in rows:
        assert raw == cursor and size == (length + 511) & -512
        assert image[raw + length:raw + size] == b"\0" * (size - length)
        cursor += size
    assert cursor == len(image)
    if rich:
        sections = {name: (length, rva, raw) for name, length, rva, _size, raw in rows}
        for directory in (1, 3, 5, 9, 12):
            assert all(struct.unpack_from("<II", image, optional + 112 + directory * 8))
        _length, text_rva, _raw = sections[b".text"]
        image_base = struct.unpack_from("<Q", image, optional + 24)[0]
        assert struct.unpack_from("<Q", image, sections[b".data"][2])[0] == image_base + text_rva + 7
        pdata_raw = sections[b".pdata"][2]
        assert struct.unpack_from("<I", image, pdata_raw)[0] == text_rva
        assert struct.unpack_from("<I", image, pdata_raw + 12)[0] == text_rva + 16
        table_size = len(obj.sections[-1].data)
        map_raw = sections[b".rdata"][2]
        decoded = wire.decode_stack_map(image[map_raw:map_raw + table_size],
                                       expected_arch=wire.ARCH_X86_64, final_image=True)
        assert decoded.functions[0].function_address == image_base + text_rva
        assert [record.locations[0].offset for record in decoded.functions[0].records] == [-8, -8]


def _join_bound(value, size, section_count, padding_bytes):
    # Independent stable Py_buffer declaration rather than the production
    # struct-format spelling. This includes the fixed stack scratch too.
    class Buffer(ctypes.Structure):
        _fields_ = [(name, ctypes.c_void_p) for name in ("buf", "obj")] + [
            ("length", ctypes.c_ssize_t), ("itemsize", ctypes.c_ssize_t),
            ("readonly", ctypes.c_int), ("ndim", ctypes.c_int),
        ] + [(name, ctypes.c_void_p) for name in ("format", "shape", "strides", "suboffsets", "internal")]
    assert ctypes.sizeof(Buffer) == 80
    chunks = 1 + 2 * section_count
    return (value._rounded(size + sys.getsizeof(b""))
            + padding_bytes + section_count * (sys.getsizeof(b"") + value.allocation_quantum)
            + value._list_bytes(chunks) + value._rounded((10 + chunks) * ctypes.sizeof(Buffer))
            + 8 * value.integer_bytes)


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("section_count", (0, 1, 4, 5, 128))
def test_image_join_admission_exact_limit_and_all_real_owners(section_count):
    value = _budget()
    header = bytearray(b"h" * 1024)
    payloads = [bytearray(b"abc") for _ in range(section_count)]
    for owner in [header] + payloads:
        value.retain(owner)
    retained = value.retained_bytes
    size, padding = 1024 + 512 * section_count, 509 * section_count
    extra = _join_bound(value, size, section_count, padding)
    value.limit_bytes = retained + extra
    value.check_image_join(size, section_count, padding)
    chunks = [header]
    for payload in payloads:
        chunks.extend((payload, b"\0" * 509))
    image = b"".join(chunks)
    actual = sys.getsizeof(image) + sys.getsizeof(chunks)
    actual += sum(sys.getsizeof(chunks[index]) for index in range(2, len(chunks), 2))
    actual += (10 + (len(chunks) if len(chunks) > 10 else 0)) * 80
    assert actual <= extra
    assert value.retained_bytes == retained
    value.limit_bytes -= 1
    with pytest.raises(wire.PreciseStackMapError, match="PE final image join"):
        value.check_image_join(size, section_count, padding)
    assert value.retained_bytes == retained


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("under_limit", (False, True))
def test_link_checks_join_before_chunks_with_all_section_and_map_owners(monkeypatch, under_limit):
    value = _budget()
    value.retained_bytes = 17
    original = value.check_image_join
    calls = []

    def checked(size, section_count, padding_bytes):
        caller = inspect.currentframe().f_back
        try:
            local = caller.f_locals
            assert "chunks" not in local
            assert len(local["image"]) == local["headers"]
            for row in local["sections"]:
                assert value._buffers[id(row[2])][0] is row[2]
            assert value._buffers[id(local["stack_payload"])][0] is local["stack_payload"]
            assert value._buffers[id(local["image"])][0] is local["image"]
            calls.append((size, section_count, padding_bytes))
            value.limit_bytes = value.retained_bytes + _join_bound(value, size, section_count, padding_bytes)
            value.limit_bytes -= int(under_limit)
            original(size, section_count, padding_bytes)
        finally:
            del caller

    monkeypatch.setattr(value, "check_image_join", checked)
    if under_limit:
        with pytest.raises(coff.CoffError, match="PE final image join"):
            pe.link_executable((_fixture(513, True),), compact_budget=value)
    else:
        image = pe.link_executable((_fixture(513, True),), compact_budget=value)
        _assert_frozen_original_bytes_except_tls_alignment(image, True, 513)
    assert len(calls) == 1 and calls[0][1] * 2 + 1 > 10
    assert value.retained_bytes == 17 and not value._buffers and value.record_visits == 16


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("sizes", ((-1, 0, 0), (512, -1, 0), (512, 1, -1),
                                   (512, 1, 512), (0, 1, 1), (True, 0, 0)))
def test_image_join_rejects_invalid_sizes_without_retiring_owners(sizes):
    value = _budget()
    owner = bytearray(b"retained")
    value.retain(owner)
    before = value.retained_bytes
    with pytest.raises(wire.PreciseStackMapError, match="invalid PE image join sizes"):
        value.check_image_join(*sizes)
    assert value.retained_bytes == before and value._buffers[id(owner)][0] is owner
