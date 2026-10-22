"""Small owned PE relocation controls, not Windows loader execution.

Use the production assembler, COFF serialization and PE linker. The test
oracle applies only the emitted DIR64 records to a bounded byte image.
"""

from __future__ import annotations

import struct

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend.pe_x86_64 import link_executable


def _object(source):
    return coff.parse_object(coff.assemble_object(source))


def _pe(image):
    assert image[:2] == b"MZ" and len(image) < 65536
    pe = struct.unpack_from("<I", image, 60)[0]
    assert image[pe:pe + 4] == b"PE\0\0"
    machine, count, _, _, _, optional_size, flags = struct.unpack_from(
        "<HHIIIHH", image, pe + 4,
    )
    assert machine == 0x8664 and optional_size == 240
    assert not flags & 1  # IMAGE_FILE_RELOCS_STRIPPED
    optional = pe + 24
    assert struct.unpack_from("<H", image, optional)[0] == 0x20B
    assert struct.unpack_from("<I", image, optional + 56)[0] < 65536
    assert struct.unpack_from("<H", image, optional + 70)[0] & 0x40  # DYNAMIC_BASE
    sections = {}
    for index in range(count):
        name, size, rva, raw_size, raw, _, _, _, _, _ = struct.unpack_from(
            "<8sIIIIIIHHI", image, optional + optional_size + index * 40,
        )
        name = name.rstrip(b"\0").decode("ascii")
        assert name not in sections and raw + raw_size <= len(image)
        assert size <= raw_size
        sections[name] = (rva, size, raw, raw_size)
    return optional, sections


def _offset(sections, rva, width):
    matches = [
        raw + rva - address
        for address, size, raw, _ in sections.values()
        if address <= rva and rva + width <= address + size
    ]
    assert len(matches) == 1, (rva, width)
    return matches[0]


def _value(image, sections, rva, fmt):
    return struct.unpack_from(fmt, image, _offset(sections, rva, struct.calcsize(fmt)))[0]


def _relocations(image, optional, sections):
    rva, size = struct.unpack_from("<II", image, optional + 112 + 5 * 8)
    assert (rva, size) == sections[".reloc"][:2] and size
    begin = _offset(sections, rva, size)
    cursor, end = begin, begin + size
    blocks, sites = [], []
    while cursor < end:
        page, block_size = struct.unpack_from("<II", image, cursor)
        assert page % 4096 == 0 and block_size >= 8 and block_size % 4 == 0
        assert cursor + block_size <= end
        words = struct.unpack_from("<" + "H" * ((block_size - 8) // 2), image, cursor + 8)
        assert not blocks or blocks[-1][0] < page
        blocks.append((page, words))
        for index, word in enumerate(words):
            if word == 0:
                assert index == len(words) - 1  # ABSOLUTE alignment padding
            else:
                assert word >> 12 == 10  # IMAGE_REL_BASED_DIR64
                site = page + (word & 4095)
                _offset(sections, site, 8)
                sites.append(site)
        cursor += block_size
    assert cursor == end and sites == sorted(set(sites))
    return blocks, sites


def _rebase(image, sections, sites, delta):
    # Independent PE/COFF DIR64 arithmetic, not an alternate production linker.
    result = bytearray(image)
    for site in sites:
        offset = _offset(sections, site, 8)
        old = struct.unpack_from("<Q", result, offset)[0]
        struct.pack_into("<Q", result, offset, (old + delta) & ((1 << 64) - 1))
    return bytes(result)


@pytest.mark.parametrize("image_base", (0x140000000, 0x180000000))
@pytest.mark.parametrize("delta", (0x200000000, -0x100000000))
def test_addr64_instruction_and_data_have_exact_dir64_and_rebase(image_base, delta):
    consumer = _object(
        ".intel_syntax noprefix\n.text\n.globl pcc_windows_start\n"
        ".type pcc_windows_start, @function\npcc_windows_start:\n"
        "movabs r11, OFFSET FLAT:payload\nret\n"
        ".data\n.globl pointer_slot\npointer_slot:\n.quad payload+7\n"
    )
    provider = _object(
        ".intel_syntax noprefix\n.section .rodata\n.zero 13\n"
        ".globl payload\n.type payload, @object\npayload:\n.quad 42\n"
    )
    text_object = next(section for section in consumer.sections if section.name == ".text")
    data_object = next(section for section in consumer.sections if section.name == ".data")
    assert text_object.data == b"\x49\xbb" + b"\0" * 8 + b"\xc3"
    assert [(rel.offset, rel.kind, consumer.symbols[rel.symbol].name)
            for rel in text_object.relocations] == [(2, coff.ADDR64, "payload")]
    assert [(rel.offset, rel.kind, consumer.symbols[rel.symbol].name)
            for rel in data_object.relocations] == [(0, coff.ADDR64, "payload")]
    assert data_object.data == struct.pack("<Q", 7)
    assert any(symbol.name == "payload" and symbol.section == 0 and symbol.external
               for symbol in consumer.symbols)

    image = link_executable([consumer, provider], image_base=image_base)
    optional, sections = _pe(image)
    assert struct.unpack_from("<II", image, optional + 112 + 9 * 8) == (0, 0)
    assert ".tlsdir" not in sections
    assert struct.unpack_from("<Q", image, optional + 24)[0] == image_base
    text_rva, data_rva = sections[".text"][0], sections[".data"][0]
    target_rva = sections[".rdata"][0] + 13
    assert _value(image, sections, target_rva, "<Q") == 42
    blocks, sites = _relocations(image, optional, sections)
    assert blocks == [
        (text_rva, (0xA002, 0)),
        (data_rva, (0xA000, 0)),
    ]
    assert sites == [text_rva + 2, data_rva]
    assert _value(image, sections, text_rva + 2, "<Q") == image_base + target_rva
    assert _value(image, sections, data_rva, "<Q") == image_base + target_rva + 7

    rebased = _rebase(image, sections, sites, delta)
    assert _value(rebased, sections, text_rva + 2, "<Q") == image_base + delta + target_rva
    assert _value(rebased, sections, data_rva, "<Q") == image_base + delta + target_rva + 7
    assert _value(rebased, sections, target_rva, "<Q") == 42


@pytest.mark.parametrize("prefix_first, init_offset", ((True, 19), (False, 3)))
def test_tls_secrel_binds_other_objects_after_section_merge(prefix_first, init_offset):
    consumer = _object(
        ".intel_syntax noprefix\n.text\n.globl pcc_windows_start\n"
        ".type pcc_windows_start, @function\npcc_windows_start:\n"
        "movabs r10, OFFSET FLAT:_tls_index\n"
        "mov r11d, OFFSET FLAT:tls_init@TPOFF\n"
        "mov eax, OFFSET FLAT:tls_zero@TPOFF\nret\n"
    )
    prefix = _object(
        '.intel_syntax noprefix\n.section .tdata,"awT",@progbits\n'
        ".byte 9,8,7,6,5\n"
    )
    provider = _object(
        '.intel_syntax noprefix\n.section .tdata,"awT",@progbits\n'
        ".p2align 4\n.zero 3\n.globl tls_init\n.type tls_init, @tls_object\n"
        "tls_init:\n.quad 42\n"
        '.section .tbss,"awT",@nobits\n.p2align 5\n.zero 7\n'
        ".globl tls_zero\n.type tls_zero, @tls_object\ntls_zero:\n.zero 8\n"
    )
    assert {section.name: section.align for section in provider.sections} == {
        ".tls$A": 16, ".tls$B": 32,
    }
    text_object = next(section for section in consumer.sections if section.name == ".text")
    assert text_object.data == (
        b"\x49\xba" + b"\0" * 8 + b"\x41\xbb" + b"\0" * 4
        + b"\xb8" + b"\0" * 4 + b"\xc3"
    )
    assert [(rel.offset, rel.kind, consumer.symbols[rel.symbol].name)
            for rel in text_object.relocations] == [
        (2, coff.ADDR64, "_tls_index"),
        (12, coff.SECREL, "tls_init"),
        (17, coff.SECREL, "tls_zero"),
    ]
    assert {symbol.name for symbol in consumer.symbols
            if symbol.external and not symbol.section} == {
        "_tls_index", "tls_init", "tls_zero",
    }
    objects = [consumer, prefix, provider] if prefix_first else [consumer, provider, prefix]
    image_base, delta = 0x140000000, 0x200000000
    image = link_executable(objects, image_base=image_base)
    optional, sections = _pe(image)
    text_rva, tls_rva = sections[".text"][0], sections[".tls"][0]
    index_rva = sections[".data"][0]
    assert sections[".tls"][1] == 47
    assert _value(image, sections, tls_rva + init_offset, "<Q") == 42
    assert _value(image, sections, tls_rva + 39, "<Q") == 0
    assert _value(image, sections, text_rva + 12, "<I") == init_offset
    assert _value(image, sections, text_rva + 17, "<I") == 39
    assert _value(image, sections, text_rva + 2, "<Q") == image_base + index_rva

    directory, size = struct.unpack_from("<II", image, optional + 112 + 9 * 8)
    assert (directory, size) == (sections[".tlsdir"][0], 40)
    tls_fields = struct.unpack_from("<QQQQII", image, _offset(sections, directory, 40))
    assert tls_fields == (
        image_base + tls_rva, image_base + tls_rva + 47,
        image_base + index_rva, 0, 0, 0x00600000,  # IMAGE_SCN_ALIGN_32BYTES
    )
    blocks, sites = _relocations(image, optional, sections)
    assert blocks == [
        (text_rva, (0xA002, 0)),
        (directory, (0xA000, 0xA008, 0xA010, 0)),
    ]
    assert sites == [text_rva + 2, directory, directory + 8, directory + 16]
    rebased = _rebase(image, sections, sites, delta)
    assert _value(rebased, sections, text_rva + 2, "<Q") == image_base + delta + index_rva
    assert _value(rebased, sections, text_rva + 12, "<I") == init_offset
    assert _value(rebased, sections, text_rva + 17, "<I") == 39
    assert struct.unpack_from("<QQQQII", rebased, _offset(sections, directory, 40)) == (
        image_base + delta + tls_rva, image_base + delta + tls_rva + 47,
        image_base + delta + index_rva, 0, 0, 0x00600000,
    )


def _tls_alignment_object(alignment, data=b"A"):
    return coff.CoffObject((
        # A non-TLS section must not increase the TLS directory's alignment.
        coff.CoffSection(".text", b"\xc3", 0x60000020, 4096),
        coff.CoffSection(".tls$A", data, 0xC0000040, alignment),
        coff.CoffSection(".tls$B", b"B", 0xC0000040, 1),
    ), (coff.CoffSymbol("pcc_windows_start", 1, function=True),))


@pytest.mark.parametrize("alignment, characteristics", (
    (1, 0x00100000), (2, 0x00200000), (4, 0x00300000),
    (8, 0x00400000), (16, 0x00500000), (32, 0x00600000),
    (64, 0x00700000), (128, 0x00800000), (256, 0x00900000),
    (512, 0x00A00000), (1024, 0x00B00000), (2048, 0x00C00000),
    (4096, 0x00D00000),
))
@pytest.mark.parametrize("data", (b"A", b""), ids=("initialized", "empty"))
def test_tls_directory_encodes_maximum_input_alignment(alignment, characteristics, data):
    obj = coff.parse_object(coff.emit_object(_tls_alignment_object(alignment, data)))
    image = link_executable([obj])
    optional, sections = _pe(image)
    tls_rva, tls_size, raw, _ = sections[".tls"]
    assert tls_rva % alignment == 0
    assert image[raw:raw + tls_size] == data + b"B"
    directory, size = struct.unpack_from("<II", image, optional + 112 + 9 * 8)
    assert (directory, size) == (sections[".tlsdir"][0], 40)
    assert _value(image, sections, directory + 36, "<I") == characteristics
    _blocks, sites = _relocations(image, optional, sections)
    assert sites == [directory, directory + 8, directory + 16]
    rebased = _rebase(image, sections, sites, 0x200000000)
    assert _value(rebased, sections, directory + 36, "<I") == characteristics


@pytest.mark.parametrize("alignment", (0, -1, 3, 24))
def test_tls_directory_rejects_invalid_input_alignment(alignment):
    with pytest.raises(coff.CoffError, match="invalid PE TLS section alignment"):
        link_executable([_tls_alignment_object(alignment)])


@pytest.mark.parametrize("alignment", (8192, 16384))
def test_tls_directory_preserves_page_alignment_limit(alignment):
    obj = _tls_alignment_object(alignment)
    if alignment == 8192:
        # The PE encoding allows 8192, but this linker still caps sections at 4096.
        obj = coff.parse_object(coff.emit_object(obj))
    with pytest.raises(coff.CoffError, match="PE section alignment above page size is unsupported"):
        link_executable([obj])


@pytest.mark.parametrize("patch, target, addend, displacement", (
    (0, 8, (1 << 31) - 1, (1 << 31) + 3),
    (8, 0, -(1 << 31), -(1 << 31) - 12),
))
def test_rel32_overflow_still_rejected_without_large_allocation(
    patch, target, addend, displacement,
):
    payload = bytearray(b"\x90" * 15 + b"\xc3")
    struct.pack_into("<i", payload, patch, addend)
    obj = coff.CoffObject((
        coff.CoffSection(".text", bytes(payload), 0x60000020, 16,
                         (coff.CoffRelocation(patch, 1, coff.REL32),)),
    ), (
        coff.CoffSymbol("pcc_windows_start", 1, 0, True, True),
        coff.CoffSymbol("far_target", 1, target),
    ))
    obj = coff.parse_object(coff.emit_object(obj))
    with pytest.raises(coff.CoffError, match="PE rel32 out of range") as caught:
        link_executable([obj])
    assert f"addend {addend} displacement {displacement}" in str(caught.value)
