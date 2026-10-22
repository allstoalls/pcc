"""Host COFF byte, validation-order and bounded-copy controls; no native timing."""

from dataclasses import replace
import struct

import pytest

from pcc.backend import coff_x86_64 as coff
from tests import coff_emit_reference as reference


def _fixture(span=0):
    kinds = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
    records = tuple(coff.CoffRelocation(index * 8, index % 3, kind)
                    for index, kind in enumerate(kinds))
    return coff.CoffObject((
        coff.CoffSection(".text", bytes(range(128)), 0x60000020, 16, records),
        coff.CoffSection("shared_long_name", b"payload" * span, 0x40000040, 8192),
        coff.CoffSection(".empty", b"", 0xC0000040),
        coff.CoffSection("éééé", b"abc", 0x40000040, 4),
        coff.CoffSection("ééééa", b"z", 0x40000040, 2),
        coff.CoffSection("shared_long_name", b"tail", 0x40000040),
    ), (
        coff.CoffSymbol("entry", 1, 7, True, True),
        coff.CoffSymbol("shared_long_name", 2, 0xFFFFFFFF, False),
        coff.CoffSymbol("external_long_symbol", 0),
        coff.CoffSymbol("shared_long_name", 2, 3, False, True),
        coff.CoffSymbol("éééé", 4),
        coff.CoffSymbol("ééééa", 5),
    ))


@pytest.mark.parametrize("span", (0, 1, 1170, 1171, 1172, 8193))
def test_complete_object_matches_frozen_writer_across_chunk_boundaries(span):
    obj = _fixture(span)
    expected = reference.emit_object(obj)
    actual = coff.emit_object(obj)
    assert type(actual) is bytes and actual == expected
    assert coff.parse_object(actual) == obj
    assert coff.emit_object(obj) == actual


@pytest.mark.parametrize("sections,symbols", (((), ()), ((), (coff.CoffSymbol("external", 0),))))
def test_empty_tables_match_frozen_writer(sections, symbols):
    obj = coff.CoffObject(sections, symbols)
    assert coff.emit_object(obj) == reference.emit_object(obj)
    assert coff.parse_object(coff.emit_object(obj)) == obj


def test_long_and_repeated_utf8_names_keep_exact_offsets_and_inline_padding():
    obj = _fixture()
    actual = coff.emit_object(obj)
    symoff, count = struct.unpack_from("<II", actual, 8)
    assert count == len(obj.symbols)
    stroff = symoff + 18 * count
    expected_strings = b"shared_long_name\0" + "ééééa\0".encode() + b"external_long_symbol\0"
    assert actual[stroff:] == struct.pack("<I", 4 + len(expected_strings)) + expected_strings
    shared_offset = struct.pack("<II", 0, 4)
    assert actual[symoff + 18:symoff + 26] == shared_offset
    assert actual[symoff + 54:symoff + 62] == shared_offset
    assert actual[symoff:symoff + 8] == b"entry\0\0\0"
    assert actual[20 + 3 * 40:28 + 3 * 40] == "éééé".encode()
    assert actual[20 + 4 * 40:28 + 4 * 40] == b"/21\0\0\0\0\0"


@pytest.mark.parametrize("length", (7, 8, 9, 8191, 8192, 8193))
def test_single_name_and_payload_cross_exact_chunk_boundaries(length):
    name = "n" * length
    obj = coff.CoffObject((coff.CoffSection(name, b"d" * length, 0x40000040),),
                          (coff.CoffSymbol(name, 1),))
    actual = coff.emit_object(obj)
    assert actual == reference.emit_object(obj)
    assert coff.parse_object(actual) == obj


@pytest.mark.parametrize("factory", (bytearray, memoryview, list, tuple))
def test_non_bytes_section_payloads_keep_extend_compatibility(factory):
    obj = coff.CoffObject((coff.CoffSection(".data", factory(b"abc"), 0x40000040),), ())
    assert coff.emit_object(obj) == reference.emit_object(obj)


@pytest.mark.parametrize("length", (1, 8191, 8192, 8193))
def test_mutable_section_payload_is_snapshotted_before_section_flags(length):
    def fixture():
        data = bytearray(b"a" * length)

        class MutatingFlags(int):
            def __or__(self, value):
                data[0] = ord("z")
                return int(self) | value

        return coff.CoffObject((coff.CoffSection(".data", data, MutatingFlags(0x40000040)),), ())

    expected = reference.emit_object(fixture())
    actual = coff.emit_object(fixture())
    assert actual == expected
    assert coff.parse_object(actual).sections[0].data == b"a" * length


def test_bytes_subclass_keeps_actual_payload_size_for_following_offsets():
    class ShortLength(bytes):
        def __len__(self):
            return 0

    obj = coff.CoffObject((
        coff.CoffSection(".data", ShortLength(b"abc"), 0x40000040),
        coff.CoffSection(".next", b"x", 0x40000040),
    ), ())
    assert coff.emit_object(obj) == reference.emit_object(obj)


@pytest.mark.parametrize("data", (3, [1, 256], ["a"]))
def test_invalid_payload_preserves_extend_error_before_relocation_validation(data):
    obj = coff.CoffObject((coff.CoffSection(
        ".data", data, 0x40000040, 1, (coff.CoffRelocation(-1, -1, 12),),
    ),), ())
    outcomes = []
    for encode in (reference.emit_object, coff.emit_object):
        with pytest.raises(Exception) as caught:
            encode(obj)
        outcomes.append((type(caught.value), str(caught.value)))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("count", (0, 1, 65534, 65535, 65536))
def test_extended_relocation_count_boundary_matches_every_frozen_byte(count):
    record = coff.CoffRelocation(0, 0, coff.ADDR64)
    obj = coff.CoffObject((coff.CoffSection(
        ".text", b"\0" * 8, 0x60000020, 16, (record,) * count,
    ),), (coff.CoffSymbol("entry", 1),))
    actual = coff.emit_object(obj)
    assert actual == reference.emit_object(obj)
    assert coff.parse_object(actual) == obj
    relptr = struct.unpack_from("<I", actual, 44)[0]
    nrel, _line, flags = struct.unpack_from("<HHI", actual, 52)
    assert nrel == min(count, 65535)
    assert bool(flags & 0x01000000) == (count >= 65535)
    if count >= 65535:
        assert struct.unpack_from("<IIH", actual, relptr) == (count + 1, 0, 0)
    elif not count:
        assert relptr == 0


@pytest.mark.parametrize("alignment", (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192))
def test_all_supported_alignments_keep_raw_offsets_without_added_padding(alignment):
    obj = coff.CoffObject((
        coff.CoffSection(".a", b"abc", 0x40000040, alignment),
        coff.CoffSection(".b", b"z", 0xC0000040, alignment),
    ), ())
    actual = coff.emit_object(obj)
    assert actual == reference.emit_object(obj)
    assert struct.unpack_from("<I", actual, 40)[0] == 100
    assert struct.unpack_from("<I", actual, 80)[0] == 103
    assert coff.parse_object(actual) == obj


def _invalid(kind):
    obj = _fixture()
    section = obj.sections[0]
    symbol = obj.symbols[0]
    if kind == "nul-before-alignment":
        return replace(obj, sections=(replace(section, align=3),),
                       symbols=(replace(symbol, name="bad\0name"),))
    if kind == "utf8-before-alignment":
        return replace(obj, sections=(replace(section, align=3),),
                       symbols=(replace(symbol, name="\ud800"),))
    if kind.startswith("alignment-"):
        return replace(obj, sections=(replace(section, align=int(kind.split("-")[1]),
                                             relocations=(coff.CoffRelocation(-1, -1, 12),)),))
    records = {
        "kind-before-offset": (coff.CoffRelocation(-1, -1, 12),),
        "negative-offset-before-symbol": (coff.CoffRelocation(-1, -1, 1),),
        "end-offset-before-symbol": (coff.CoffRelocation(len(section.data) - 7, -1, 1),),
        "negative-symbol": (coff.CoffRelocation(0, -1, 1),),
        "end-symbol": (coff.CoffRelocation(0, len(obj.symbols), 1),),
        "record-order": (coff.CoffRelocation(0, -1, 1), coff.CoffRelocation(-1, -1, 12)),
    }
    if kind in records:
        return replace(obj, sections=(replace(section, relocations=records[kind]),))
    if kind == "section-header-before-next-section":
        return replace(obj, sections=(replace(section, flags=1 << 32), replace(section, align=3)))
    if kind == "section-before-symbol":
        return replace(obj, sections=(replace(section, align=3),),
                       symbols=(replace(symbol, value=1 << 32),))
    if kind == "symbol-order":
        return replace(obj, sections=(replace(section, relocations=()),),
                       symbols=(replace(symbol, value=1 << 32),
                                replace(symbol, section=32768)))
    raise AssertionError(kind)


@pytest.mark.parametrize("kind,message", (
    ("nul-before-alignment", "NUL in COFF name"),
    ("utf8-before-alignment", "surrogates not allowed"),
    ("alignment-0", "invalid COFF section alignment"),
    ("alignment-3", "invalid COFF section alignment"),
    ("alignment-16384", "invalid COFF section alignment"),
    ("kind-before-offset", "unsupported AMD64 COFF relocation"),
    ("negative-offset-before-symbol", "COFF relocation exceeds section"),
    ("end-offset-before-symbol", "COFF relocation exceeds section"),
    ("negative-symbol", "COFF relocation symbol out of range"),
    ("end-symbol", "COFF relocation symbol out of range"),
    ("record-order", "COFF relocation symbol out of range"),
    ("section-header-before-next-section", "format requires"),
    ("section-before-symbol", "invalid COFF section alignment"),
    ("symbol-order", "format requires"),
))
def test_validation_first_error_matches_frozen_writer(kind, message):
    obj = _invalid(kind)
    outcomes = []
    for encode in (reference.emit_object, coff.emit_object):
        with pytest.raises(Exception) as caught:
            encode(obj)
        outcomes.append((type(caught.value), str(caught.value)))
    assert outcomes[0] == outcomes[1]
    assert message in outcomes[0][1]


@pytest.mark.parametrize("record_count", (256, 512, 1024))
def test_output_growth_never_copies_prior_record_prefixes(monkeypatch, record_count):
    class CountingBytearray(bytearray):
        prefix_bytes = 0

        def extend(self, value):
            type(self).prefix_bytes += len(self)
            return super().extend(value)

    obj = coff.CoffObject((coff.CoffSection(
        ".text", bytes(8), 0x60000020, 1,
        (coff.CoffRelocation(0, 0, coff.ADDR64),) * record_count,
    ),), (coff.CoffSymbol("entry", 1),))
    monkeypatch.setattr(reference, "bytearray", CountingBytearray, raising=False)
    expected = reference.emit_object(obj)
    # This records bytes the native extend implementation copies, not CPython
    # allocator work or a timing result. Each relocation adds another prefix.
    assert CountingBytearray.prefix_bytes == (
        60 + record_count * 68 + 5 * record_count * (record_count - 1)
        + (68 + 10 * record_count) + (86 + 10 * record_count)
    )
    CountingBytearray.prefix_bytes = 0
    monkeypatch.setattr(coff, "bytearray", CountingBytearray, raising=False)
    assert coff.emit_object(obj) == expected
    assert CountingBytearray.prefix_bytes == 0


def test_small_record_owners_are_bounded_and_large_payloads_are_reused(monkeypatch):
    original = coff._append_coff_chunk
    seen = []

    def checked(chunks, pending, pending_size, raw):
        result = original(chunks, pending, pending_size, raw)
        assert result == sum(map(len, pending)) < coff._COFF_CHUNK_BYTES
        if len(raw) >= coff._COFF_CHUNK_BYTES:
            assert chunks[-1] is raw and not pending
        seen.append((len(chunks), len(pending), len(raw)))
        return result

    monkeypatch.setattr(coff, "_append_coff_chunk", checked)
    obj = _fixture(2000)
    symbols = tuple(coff.CoffSymbol("long_shared_symbol_" + str(index), 0)
                    for index in range(4096))
    obj = replace(obj, symbols=obj.symbols + symbols)
    assert coff.emit_object(obj) == reference.emit_object(obj)
    assert any(size >= coff._COFF_CHUNK_BYTES for _, _, size in seen)
    assert max(pending for _, pending, _ in seen) <= coff._COFF_CHUNK_BYTES // 10
    assert max(chunks for chunks, _, _ in seen) < 64
