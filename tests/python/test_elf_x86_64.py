"""Focused contracts for pcc's finite static x86_64 ELF toolchain."""

from __future__ import annotations

import builtins
import hashlib
import stat
import struct

import pytest

from pcc.backend import elf_x86_64 as elf, owned_elf_link
from pcc.backend.elf_x86_64 import (
    ET_EXEC,
    R_X86_64_64,
    R_X86_64_GOTTPOFF,
    R_X86_64_PLT32,
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHF_TLS,
    SHF_WRITE,
    SHT_NOBITS,
    SHT_PROGBITS,
    STB_GLOBAL,
    STT_FUNC,
    STT_OBJECT,
    STT_TLS,
    ElfError,
    ElfObject,
    ElfRelocation,
    ElfSection,
    ElfSymbol,
    emit_relocatable,
    link_static_executable,
    parse_relocatable,
    parse_static_executable,
)
from pcc.backend.x86_64_asm_driver import assemble_file
from pcc.backend.x86_64_encode import X86EncodeError, encode_instruction


def _exit_42_object() -> ElfObject:
    # _start: call answer; mov edi,eax; mov eax,60; syscall
    # answer: mov eax,42; ret
    text = bytes.fromhex(
        "e8 00 00 00 00 89 c7 b8 3c 00 00 00 0f 05 "
        "b8 2a 00 00 00 c3"
    )
    return ElfObject(
        sections=(ElfSection(
            ".text",
            SHT_PROGBITS,
            SHF_ALLOC | SHF_EXECINSTR,
            16,
            text,
            relocations=(ElfRelocation(1, 2, R_X86_64_PLT32, -4),),
        ),),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("_start", 1, 0, 14, STB_GLOBAL, STT_FUNC),
            ElfSymbol("answer", 1, 14, 6, STB_GLOBAL, STT_FUNC),
        ),
    )


def _ar_member(name: str, payload: bytes) -> bytes:
    encoded_name = (name + "/").encode("ascii")
    assert len(encoded_name) <= 16
    header = (
        encoded_name.ljust(16, b" ")
        + b"0".ljust(12, b" ")
        + b"0".ljust(6, b" ")
        + b"0".ljust(6, b" ")
        + b"100644".ljust(8, b" ")
        + str(len(payload)).encode("ascii").ljust(10, b" ")
        + b"`\n"
    )
    assert len(header) == 60
    return header + payload + (b"\n" if len(payload) & 1 else b"")


@pytest.mark.parametrize("compact", [False, True])
def test_owned_relocatable_writer_roundtrips_symbols_and_relocations(compact):
    source = _exit_42_object()
    sections = source.sections
    relocations = sections[0].relocations
    relocation = relocations[0]
    encoded = emit_relocatable(source)
    assert encoded.startswith(b"\x7fELF")
    assert parse_relocatable(encoded) == source
    parsed = parse_relocatable(encoded, compact_relocations=compact)
    assert tuple(parsed.sections[0].relocations) == relocations
    assert parsed.sections[0].relocations[-1] == relocation
    assert emit_relocatable(parsed) == encoded
    inputs = [source]
    expected = link_static_executable(inputs)
    assert type(expected) is bytes
    assert link_static_executable([parsed]) == expected
    assert inputs == [source] and inputs[0] is source
    assert source.sections is sections
    assert sections[0].relocations is relocations
    assert relocations[0] is relocation
    if not compact:
        assert isinstance(parsed.sections[0].relocations, tuple)
        assert parsed.sections[0].relocations[0] is parsed.sections[0].relocations[0]
    else:
        assert not isinstance(parsed.sections[0].relocations, tuple)


@pytest.mark.parametrize("field,value,diagnostic", [
    (0, 1000, "relocation is outside its payload"),
    (1, (3 << 32) | R_X86_64_PLT32, "relocation has invalid symbol index"),
    (1, (2 << 32) | 0xFFFFFFFF, "relocation type 4294967295 is not supported"),
])
def test_compact_relocation_parser_preserves_strict_errors(field, value, diagnostic):
    encoded = bytearray(emit_relocatable(_exit_42_object()))
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", encoded)
    headers = [struct.unpack_from("<IIQQQQIIQQ", encoded, header[6] + index * 64)
               for index in range(header[12])]
    rela = next(row for row in headers if row[1] == 4)
    record = list(struct.unpack_from("<QQq", encoded, rela[4]))
    record[field] = value
    struct.pack_into("<QQq", encoded, rela[4], *record)
    with pytest.raises(ElfError) as ordinary:
        parse_relocatable(bytes(encoded))
    with pytest.raises(ElfError) as compact:
        parse_relocatable(bytes(encoded), compact_relocations=True)
    with pytest.raises(ElfError) as indexed:
        elf._parse_relocatable_indexed(bytes(encoded))
    unpublished = []
    with pytest.raises(ElfError):
        elf._parse_relocatable(encoded, compact_relocations=True,
                               _section_ranges=unpublished)
    assert unpublished == []  # No reusable range escapes failed full validation.
    assert diagnostic in str(ordinary.value)
    assert str(compact.value) == str(ordinary.value)
    assert str(indexed.value) == str(ordinary.value)


def _interleave_elf_section_headers(encoded):
    """Move metadata among payload headers while preserving model section order."""
    data = bytearray(encoded)
    header = list(elf._ELF_HEADER.unpack_from(data))
    rows = [list(elf._SECTION_HEADER.unpack_from(data, header[6] + index * 64))
            for index in range(header[12])]
    model = [index for index, row in enumerate(rows) if row[1] in (1, 7, 8)]
    metadata = [index for index in range(1, len(rows)) if index not in model]
    order = [0]
    for index in range(max(len(model), len(metadata))):
        if index < len(metadata):
            order.append(metadata[-index - 1])
        if index < len(model):
            order.append(model[index])
    mapping = {old: new for new, old in enumerate(order)}
    for row in rows:
        if row[1] == elf.SHT_SYMTAB:
            for offset in range(row[4], row[4] + row[5], elf._SYMBOL.size):
                symbol = list(elf._SYMBOL.unpack_from(data, offset))
                if symbol[3] not in (elf.SHN_UNDEF, elf.SHN_ABS, elf.SHN_COMMON):
                    symbol[3] = mapping[symbol[3]]
                elf._SYMBOL.pack_into(data, offset, *symbol)
        if row[6]:
            row[6] = mapping[row[6]]
        if row[1] == elf.SHT_RELA:
            row[7] = mapping[row[7]]
    for new, old in enumerate(order):
        elf._SECTION_HEADER.pack_into(data, header[6] + new * 64, *rows[old])
    header[13] = mapping[header[13]]
    elf._ELF_HEADER.pack_into(data, 0, *header)
    return bytes(data)


@pytest.mark.parametrize("interspersed", [False, True])
def test_private_index_projects_exact_validated_sections_and_relocations(interspersed):
    source = _exit_42_object()
    source = ElfObject(source.sections + (
        ElfSection(".bss", SHT_NOBITS, SHF_ALLOC | SHF_WRITE, 16, mem_size=32),
        ElfSection(".data", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 8, b"abc\0"),
        ElfSection(".empty", SHT_PROGBITS, SHF_ALLOC, 1, b""),
    ), source.symbols)
    encoded = emit_relocatable(source)
    if interspersed:
        encoded = _interleave_elf_section_headers(encoded)
    assert parse_relocatable(encoded) == source
    parsed, ranges = elf._parse_relocatable_indexed(encoded)
    assert emit_relocatable(parsed) == emit_relocatable(source)
    assert parsed.symbols == source.symbols
    assert len(ranges) == len(source.sections)
    for section, row in zip(source.sections, ranges):
        offset, size, rela_offset, rela_size = row
        assert encoded[offset:offset + size] == section.data
        projected = elf._PackedElfRelocations(encoded, rela_offset, rela_size)
        assert projected.payload is encoded
        assert tuple(projected) == section.relocations
        assert len(projected) == len(section.relocations)
    empty = ElfObject((), (ElfSymbol.null(),))
    empty_parsed, empty_ranges = elf._parse_relocatable_indexed(emit_relocatable(empty))
    assert empty_parsed == empty and empty_ranges == ()


@pytest.mark.parametrize("shape", ["empty-empty", "empty-nonempty", "nonempty-empty"])
def test_private_index_preserves_existing_duplicate_empty_rela_behavior(shape):
    data = bytearray(emit_relocatable(_exit_42_object()))
    header = list(elf._ELF_HEADER.unpack_from(data))
    rows = [list(elf._SECTION_HEADER.unpack_from(data, header[6] + index * 64))
            for index in range(header[12])]
    rela_index = next(index for index, row in enumerate(rows) if row[1] == elf.SHT_RELA)
    appended = list(rows[rela_index])
    if shape.startswith("empty-"):
        rows[rela_index][5] = 0
        elf._SECTION_HEADER.pack_into(data, header[6] + rela_index * 64, *rows[rela_index])
    if shape.endswith("-empty"):
        appended[5] = 0
    data.extend(elf._SECTION_HEADER.pack(*appended))
    header[12] += 1
    elf._ELF_HEADER.pack_into(data, 0, *header)
    if shape == "nonempty-empty":
        with pytest.raises(ElfError, match="multiple RELA tables") as ordinary:
            parse_relocatable(data)
        with pytest.raises(ElfError) as indexed:
            elf._parse_relocatable_indexed(data)
        assert str(indexed.value) == str(ordinary.value)
    else:
        ordinary = parse_relocatable(bytes(data))
        indexed, ranges = elf._parse_relocatable_indexed(data)
        assert emit_relocatable(indexed) == emit_relocatable(ordinary)
        _offset, _size, rela_offset, rela_size = ranges[0]
        assert tuple(elf._PackedElfRelocations(data, rela_offset, rela_size)) == (
            ordinary.sections[0].relocations)


def test_packed_relocation_range_is_immutable_and_reiterable():
    expected = (ElfRelocation(3, 2, R_X86_64_64, -7),
                ElfRelocation(11, 1, R_X86_64_PLT32, 5))
    records = b"".join(elf._RELA.pack(row.offset, (row.symbol_index << 32) | row.type,
                                     row.addend) for row in expected)
    mutable = bytearray(b"prefix!" + records + b"suffix")
    projected = elf._PackedElfRelocations(mutable, 7, len(records))
    mutable[:] = b"\0" * len(mutable)
    assert type(projected.payload) is bytes
    assert tuple(projected) == expected
    assert tuple(projected) == expected
    assert projected[0] == expected[0] and projected[-1] == expected[-1]
    assert projected[0] is not projected[0]
    with pytest.raises(IndexError):
        projected[2]
    with pytest.raises(IndexError):
        projected[-3]
    with pytest.raises(TypeError):
        projected[:1]
    assert tuple(elf._PackedElfRelocations(projected.payload, 7, 0)) == ()


@pytest.mark.parametrize("offset,size,diagnostic", [
    (-1, 24, "outside"), (0, -1, "outside"), (49, 0, "outside"),
    (24, 25, "outside"), (0, 23, "truncated"),
])
def test_packed_relocation_range_rejects_invalid_bounds(offset, size, diagnostic):
    with pytest.raises(ElfError, match=diagnostic):
        elf._PackedElfRelocations(b"\0" * 48, offset, size)


def test_static_link_applies_plt32_and_has_no_dynamic_or_section_surface():
    image = link_static_executable([_exit_42_object()])
    shape = parse_static_executable(image)
    assert shape == {
        "entry": 0x401000,
        "program_headers": 1,
        "load_segments": 1,
        "tls_segments": 0,
    }
    assert int.from_bytes(image[0x1001:0x1005], "little", signed=True) == 9
    assert int.from_bytes(image[16:18], "little") == ET_EXEC
    # No section table means no residual SHT_RELA/SHT_DYNSYM can be consumed
    # accidentally by a runtime loader.
    assert image[40:48] == b"\0" * 8


def _archive_selection_fixture():
    start = ElfObject(
        sections=(ElfSection(
            ".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 16,
            bytes.fromhex("e8 00 00 00 00 89 c7 b8 3c 00 00 00 0f 05"),
            relocations=(ElfRelocation(1, 2, R_X86_64_PLT32, -4),),
        ),),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("_start", 1, 0, 14, STB_GLOBAL, STT_FUNC),
            ElfSymbol("answer", 0, 0, 0, STB_GLOBAL, STT_FUNC),
        ),
    )
    answer = ElfObject(
        sections=(
            ElfSection(
                ".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 16,
                bytes.fromhex("b8 2a 00 00 00 c3"),
            ),
            ElfSection(
                ".data", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 8, b"\0" * 8,
                relocations=(ElfRelocation(0, 1, R_X86_64_64),),
            ),
        ),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("answer", 1, 0, 6, STB_GLOBAL, STT_FUNC),
        ),
    )
    unused = ElfObject(
        sections=(ElfSection(
            ".rodata", SHT_PROGBITS, SHF_ALLOC, 1, b"unused" * 100,
        ),),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("unused_payload", 1, 0, 600, STB_GLOBAL, STT_OBJECT),
        ),
    )
    archive = (
        b"!<arch>\n"
        + _ar_member("answer.o", emit_relocatable(answer))
        + _ar_member("unused.o", emit_relocatable(unused))
    )
    return start, archive


@pytest.mark.parametrize("compact", [False, True])
def test_archive_member_is_selected_by_unresolved_closure_only(compact):
    start, archive = _archive_selection_fixture()
    image = link_static_executable([start], archives=[archive],
                                   compact_archive_relocations=compact)
    parse_static_executable(image)
    assert b"unusedunusedunused" not in image
    assert image == link_static_executable([start], archives=[archive])


@pytest.fixture
def owned_elf_publication(tmp_path):
    from pcc.backend.linux_thread_intrinsics import assembly

    start, archive = _archive_selection_fixture()
    object_path = tmp_path / "start.o"
    object_path.write_bytes(emit_relocatable(start))
    archive_path = tmp_path / "answer.a"
    archive_path.write_bytes(archive)
    target = "x86_64-unknown-linux-gnu"
    expected = link_static_executable([
        start, owned_elf_link._thread_pointer_object(target),
        owned_elf_link.assemble(assembly(False), target),
    ], archives=[archive])
    output = tmp_path / "program"
    options = dict(target=target, output=str(output), objects=[str(object_path)],
                   archives=[str(archive_path)])
    return options, expected, output, archive_path


@pytest.mark.parametrize("with_map", [False, True])
def test_owned_elf_chunked_short_writes_publish_exact_image_and_map(
    owned_elf_publication, monkeypatch, with_map,
):
    options, expected, output, archive_path = owned_elf_publication
    map_path = output.with_suffix(".map")
    if with_map:
        options["map_path"] = str(map_path)
    monkeypatch.setattr(owned_elf_link, "_IMAGE_CHUNK_BYTES", 31)
    write_sizes = []

    class ShortWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            assert type(data) is bytes
            write_sizes.append(len(data))
            return self.stream.write(data[:7])

    def open_file(path, mode="r", **kwargs):
        stream = builtins.open(path, mode, **kwargs)
        return ShortWriter(stream) if mode == "wb" else stream

    monkeypatch.setattr(owned_elf_link, "open", open_file, raising=False)
    owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o755
    assert len(write_sizes) > 1 and max(write_sizes) <= 31
    assert not output.with_name(output.name + ".pcc-link.tmp").exists()
    if with_map:
        lines = map_path.read_text().splitlines()
        assert "# image sha256=" + hashlib.sha256(expected).hexdigest() in lines
        assert any(line.startswith(str(archive_path) + "(answer.o)") for line in lines)
        assert not any("(unused.o)" in line for line in lines)
        assert not map_path.with_name(map_path.name + ".pcc-link.tmp").exists()


@pytest.mark.parametrize("failure", ["write", "zero-write", "replace", "validation"])
def test_owned_elf_chunked_failure_preserves_output_and_cleans_temporary(
    owned_elf_publication, monkeypatch, failure,
):
    options, _expected, output, _archive = owned_elf_publication
    output.write_bytes(b"previous executable")
    map_path = output.with_suffix(".map")
    map_path.write_text("previous map")
    options["map_path"] = str(map_path)
    opened = []

    class FailedWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if failure == "zero-write":
                return 0
            self.stream.write(data[:3])
            raise OSError("synthetic write failure")

    def open_file(path, mode="r", **kwargs):
        stream = builtins.open(path, mode, **kwargs)
        if mode == "wb":
            opened.append(path)
            if failure in ("write", "zero-write"):
                return FailedWriter(stream)
        return stream

    def reject_replace(*_args):
        raise OSError("synthetic replace failure")

    def reject_validation(_image):
        raise ElfError("synthetic executable validation failure")

    monkeypatch.setattr(owned_elf_link, "open", open_file, raising=False)
    if failure == "replace":
        monkeypatch.setattr(owned_elf_link.os, "replace", reject_replace)
    if failure == "validation":
        monkeypatch.setattr(owned_elf_link, "parse_static_executable", reject_validation)
    with pytest.raises(ElfError if failure == "validation" else OSError):
        owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == b"previous executable"
    assert map_path.read_text() == "previous map"
    assert not output.with_name(output.name + ".pcc-link.tmp").exists()
    assert not map_path.with_name(map_path.name + ".pcc-link.tmp").exists()
    assert bool(opened) is (failure != "validation")


def test_initial_exec_tls_relocation_builds_got_and_pt_tls():
    obj = ElfObject(
        sections=(
            ElfSection(
                ".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 16,
                b"\0" * 4,
                relocations=(ElfRelocation(0, 2, R_X86_64_GOTTPOFF, -4),),
            ),
            ElfSection(
                ".tdata", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE | SHF_TLS,
                4, (37).to_bytes(4, "little"),
            ),
            ElfSection(
                ".tbss", SHT_NOBITS, SHF_ALLOC | SHF_WRITE | SHF_TLS,
                8, mem_size=8,
            ),
        ),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("_start", 1, 0, 4, STB_GLOBAL, STT_FUNC),
            ElfSymbol("tls_init", 2, 0, 4, STB_GLOBAL, STT_TLS),
            ElfSymbol("tls_zero", 3, 0, 8, STB_GLOBAL, STT_TLS),
        ),
    )
    shape = parse_static_executable(link_static_executable([obj]))
    assert shape["load_segments"] == 2
    assert shape["tls_segments"] == 1


def test_static_link_fails_closed_on_undefined_and_duplicate_symbols():
    source = _exit_42_object()
    duplicate = ElfObject(
        sections=(ElfSection(
            ".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 1, b"\xc3",
        ),),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("answer", 1, 0, 1, STB_GLOBAL, STT_FUNC),
        ),
    )
    try:
        link_static_executable([source, duplicate])
    except ElfError as exc:
        assert "duplicate strong definition" in str(exc)
    else:
        raise AssertionError("duplicate strong definition was accepted")

    undefined = ElfObject(
        sections=(ElfSection(
            ".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 1, b"\0" * 4,
            relocations=(ElfRelocation(0, 2, R_X86_64_PLT32, -4),),
        ),),
        symbols=(
            ElfSymbol.null(),
            ElfSymbol("_start", 1, 0, 4, STB_GLOBAL, STT_FUNC),
            ElfSymbol("missing", 0, 0, 0, STB_GLOBAL, STT_FUNC),
        ),
    )
    try:
        link_static_executable([undefined])
    except ElfError as exc:
        assert "undefined static ELF symbols: missing" in str(exc)
    else:
        raise AssertionError("undefined static symbol was accepted")


def test_owned_x86_encoder_covers_core_integer_memory_branch_and_atomic_forms():
    labels = {".Ldone": (".text", 256)}
    lines = (
        "push rbp",
        "mov rbp, rsp",
        "sub rsp, 32",
        "mov r10, 42",
        "mov QWORD PTR [rbp - 8], r10",
        "mov r11, QWORD PTR [rbp - 8]",
        "lea r10, [r11 + r10*8]",
        "imul r10, r11, 9",
        "cmp r10, r11",
        "setne al",
        "movzx r10, al",
        "lock xadd QWORD PTR [r11], r10",
        "lock cmpxchg QWORD PTR [r11], r10",
        "jne .Ldone",
        "add rsp, 32",
        "pop rbp",
        "ret",
    )
    offset = 0
    payload = bytearray()
    for line in lines:
        encoded = encode_instruction(
            line, pc=offset, labels=labels, section_name=".text"
        )
        assert encoded.code
        assert not encoded.relocations
        payload.extend(encoded.code)
        offset += len(encoded.code)
    assert payload.startswith(bytes.fromhex("55 48 89 e5"))
    assert payload.endswith(bytes.fromhex("48 83 c4 20 5d c3"))


def test_owned_asm_driver_emits_external_call_data_and_tls_relocations():
    source = """
.intel_syntax noprefix
.section .tdata,"awT",@progbits
.p2align 3
.globl tls_value
.type tls_value, @object
.size tls_value, 8
tls_value:
  .quad 7
.data
.p2align 3
.globl callback_slot
.type callback_slot, @object
.size callback_slot, 8
callback_slot:
  .quad external_callback+4
.text
.p2align 4, 0x90
.globl _start
.type _start, @function
_start:
  mov r10, QWORD PTR tls_value@gottpoff[rip]
  add r10, QWORD PTR fs:0
  call external_callback
  ret
.size _start, .-_start
.section .note.GNU-stack,"",@progbits
"""
    obj = assemble_file(source)
    encoded = emit_relocatable(obj)
    parsed = parse_relocatable(encoded)
    relocation_types = {
        relocation.type
        for section in parsed.sections
        for relocation in section.relocations
    }
    assert R_X86_64_GOTTPOFF in relocation_types
    assert R_X86_64_PLT32 in relocation_types
    assert R_X86_64_64 in relocation_types
    tls_symbol = next(symbol for symbol in parsed.symbols if symbol.name == "tls_value")
    assert tls_symbol.type == STT_TLS


def test_owned_asm_driver_resolves_temporary_labels_without_publishing_them():
    source = """
.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  xor eax, eax
.Lagain:
  add eax, 1
  cmp eax, 2
  jne .Lagain
  ret
.size probe, .-probe
.section .note.GNU-stack,"",@progbits
"""
    obj = assemble_file(source)
    text = next(section for section in obj.sections if section.name == ".text")
    assert not text.relocations
    assert "probe" in {symbol.name for symbol in obj.symbols}
    assert ".Lagain" not in {symbol.name for symbol in obj.symbols}


def test_owned_x86_assembler_fails_closed_on_dialect_growth():
    rejected = (
        (
            ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n"
            "  vzeroupper\n"
        ),
        ".text\n.globl _start\n_start:\n  ret\n",
        (
            ".intel_syntax noprefix\n.text\n"
            ".intel_syntax noprefix\n_start:\n  ret\n"
        ),
    )
    for source in rejected:
        try:
            assemble_file(source)
        except X86EncodeError as exc:
            assert "not proven" in str(exc) or ".intel_syntax" in str(exc)
        else:
            raise AssertionError(
                "unknown x86 assembly shape was silently accepted"
            )


# Host-only validation controls: raw RELA bytes and data classes, never an ELF
# emitter/parser, linker, compiler, runtime, FFI or native executable.
def _raw_validation_object(rows, *, second_rows=(), machine=elf.EM_AARCH64):
    def section(name, records):
        payload = b"".join(elf._RELA.pack(offset, (symbol << 32) | kind, 0)
                           for offset, symbol, kind in records)
        return ElfSection(name, SHT_PROGBITS, SHF_ALLOC, 8, b"\0" * 16,
                          relocations=elf._PackedElfRelocations(payload))

    obj = object.__new__(ElfObject)
    object.__setattr__(obj, "sections", (section(".data", rows),
                                         section(".more", second_rows)))
    object.__setattr__(obj, "symbols", (ElfSymbol.null(),
                                         ElfSymbol("target", elf.SHN_UNDEF, 0, 0,
                                                   STB_GLOBAL, STT_OBJECT)))
    object.__setattr__(obj, "machine", machine)
    return obj


def _validation_outcome(validate, obj):
    try:
        result = validate(obj)
    except Exception as exc:
        cause = exc.__cause__
        return (type(exc), str(exc), None if cause is None else (type(cause), str(cause)))
    return result


@pytest.mark.parametrize("rows,second_rows,diagnostic", [
    ([(0, 1, 257), (8, 1, 257), (16, 1, 0), (16, 1, 0)], [], None),
    ([], [], None),
    ([(100, 2, 0xFFFFFFFF)], [], "invalid symbol index"),
    ([(100, 1, 0xFFFFFFFF)], [], "unsupported AArch64 ELF relocation: 4294967295"),
    ([(15, 1, 257), (0, 1, 0xFFFFFFFF)], [], "outside its payload"),
    ([(0, 1, 257), (100, 2, 257)], [], "invalid symbol index"),
    ([(0, 1, 257), (15, 1, 257)], [], "outside its payload"),
    ([(16, 1, 0), (17, 1, 0)], [], "outside its payload"),
    ([(16, 1, 0), (17, 2, 0)], [], "invalid symbol index"),
    ([(0, 1, 257)], [(15, 1, 257)], "outside its payload"),
    ([(0, 1, 257)], [(100, 2, 0xFFFFFFFF)], "invalid symbol index"),
    ([(0, 1, 257)], [(100, 1, 0xFFFFFFFF)], "unsupported AArch64 ELF relocation: 4294967295"),
])
def test_arm_packed_width_cache_preserves_first_error(rows, second_rows, diagnostic):
    from tests.elf_validate_object_preimage import original_validator

    obj = _raw_validation_object(rows, second_rows=second_rows)
    sections, symbols = obj.sections, obj.symbols
    frozen = tuple((section.data, section.relocations, section.relocations.payload)
                   for section in sections)
    before = _validation_outcome(original_validator(elf), obj)
    after = _validation_outcome(elf._validate_object, obj)
    assert after == before
    assert obj.sections is sections and obj.symbols is symbols and obj.machine == elf.EM_AARCH64
    for section, (data, relocations, payload) in zip(obj.sections, frozen):
        assert section.data is data and section.relocations is relocations
        assert relocations.payload is payload
    if diagnostic is None:
        assert after is None
    else:
        assert after[0] is ElfError and diagnostic in after[1]


def test_arm_packed_width_cache_reuses_all_legal_types_per_object(monkeypatch):
    from tests.elf_validate_object_preimage import original_validator

    # Independent finite ARM corpus, covering every 0/4/8-byte width.
    kinds = (0, 257, 258, 260, 261, 274, 275, 277, 278, 282, 283,
             284, 285, 286, 299, 311, 312, 541, 542, 549, 551)
    rows = [(16 if kind == 0 else 0, 1, kind) for kind in kinds * 2]
    obj = _raw_validation_object(rows, second_rows=rows)
    original = original_validator(elf)
    helper = elf._relocation_width
    calls = []

    def width(kind, machine):
        calls.append((kind, machine))
        return helper(kind, machine)

    original.__globals__["_relocation_width"] = width
    monkeypatch.setattr(elf, "_relocation_width", width)
    assert original(obj) is None and len(calls) == 84
    calls.clear()
    assert elf._validate_object(obj) is None
    assert calls == [(kind, elf.EM_AARCH64) for kind in kinds]
    # No global cache survives an object validation, even for the same object.
    assert elf._validate_object(obj) is None
    assert calls == [(kind, elf.EM_AARCH64) for kind in kinds] * 2


def test_arm_packed_width_cache_does_not_cache_failure(monkeypatch):
    obj = _raw_validation_object([(16, 1, 0), (0, 1, 0xFFFFFFFF)])
    helper = elf._relocation_width
    calls = []

    def width(kind, machine):
        calls.append(kind)
        return helper(kind, machine)

    monkeypatch.setattr(elf, "_relocation_width", width)
    for _ in range(2):
        with pytest.raises(ElfError, match="unsupported AArch64 ELF relocation") as caught:
            elf._validate_object(obj)
        assert type(caught.value.__cause__) is ValueError
    assert calls == [0, 0xFFFFFFFF, 0, 0xFFFFFFFF]


def test_arm_packed_width_cache_remains_bounded_on_helper_extension(monkeypatch):
    # Simulate a future successful helper extension without extending today's
    # production relocation table. Entries beyond 21 still use the helper.
    calls = []

    def width(kind, machine):
        calls.append(kind)
        assert machine == elf.EM_AARCH64
        return 0

    monkeypatch.setattr(elf, "_relocation_width", width)
    rows = [(0, 1, kind) for kind in tuple(range(24)) * 2]
    assert elf._validate_object(_raw_validation_object(rows)) is None
    assert calls == list(range(24)) + [21, 22, 23]


@pytest.mark.parametrize("shape", [
    "object-subclass", "custom-object", "sections-list", "symbols-list",
    "section-subclass", "data-subclass", "packed-subclass", "packed-payload-subclass",
    "packed-offset-subclass", "packed-size-subclass", "machine-subclass",
    "relocations-list", "relocations-tuple", "custom-relocations", "x86",
])
def test_arm_packed_width_cache_keeps_fallback_helper_calls(monkeypatch, shape):
    from types import SimpleNamespace
    from tests.elf_validate_object_preimage import original_validator

    class ObjectSubclass(ElfObject):
        pass

    class SectionSubclass(ElfSection):
        pass

    class PackedSubclass(elf._PackedElfRelocations):
        pass

    class IntSubclass(int):
        pass

    class BytesSubclass(bytes):
        pass

    class CustomRelocations:
        def __iter__(self):
            return iter((ElfRelocation(0, 1, 0), ElfRelocation(0, 1, 0)))

    obj = _raw_validation_object([(0, 1, 0), (0, 1, 0)])
    section = obj.sections[0]
    packed = section.relocations
    if shape in ("object-subclass", "custom-object"):
        replacement = (object.__new__(ObjectSubclass) if shape == "object-subclass"
                       else SimpleNamespace())
        for name in ("sections", "symbols", "machine"):
            object.__setattr__(replacement, name, getattr(obj, name))
        obj = replacement
    elif shape in ("sections-list", "symbols-list"):
        name = shape.split("-")[0]
        object.__setattr__(obj, name, list(getattr(obj, name)))
    elif shape == "section-subclass":
        replacement = SectionSubclass(section.name, section.type, section.flags,
                                      section.align, section.data, section.mem_size, packed)
        object.__setattr__(obj, "sections", (replacement,))
    elif shape == "data-subclass":
        object.__setattr__(section, "data", BytesSubclass(section.data))
    elif shape == "packed-subclass":
        object.__setattr__(section, "relocations", PackedSubclass(packed.payload))
    elif shape == "packed-payload-subclass":
        object.__setattr__(packed, "payload", BytesSubclass(packed.payload))
    elif shape in ("packed-offset-subclass", "packed-size-subclass"):
        name = shape.split("-")[1]
        object.__setattr__(packed, name, IntSubclass(getattr(packed, name)))
    elif shape == "machine-subclass":
        object.__setattr__(obj, "machine", IntSubclass(elf.EM_AARCH64))
    elif shape == "x86":
        object.__setattr__(obj, "machine", elf.EM_X86_64)
    else:
        rows = tuple(packed)
        value = (list(rows) if shape == "relocations-list" else rows
                 if shape == "relocations-tuple" else CustomRelocations())
        object.__setattr__(section, "relocations", value)
    helper = elf._relocation_width
    calls = []

    def width(kind, machine):
        calls.append((kind, machine))
        return helper(kind, machine)

    original = original_validator(elf)
    original.__globals__["_relocation_width"] = width
    monkeypatch.setattr(elf, "_relocation_width", width)
    assert original(obj) is None
    before = calls[:]
    calls.clear()
    assert elf._validate_object(obj) is None
    assert calls == before and len(calls) == 2


def test_arm_packed_width_cache_preserves_custom_machine_reads(monkeypatch):
    from tests.elf_validate_object_preimage import original_validator

    events = []

    class CustomObject(ElfObject):
        def __getattribute__(self, name):
            if name == "machine":
                events.append(name)
            return super().__getattribute__(name)

    obj = _raw_validation_object([(0, 1, 0), (0, 1, 0)])
    custom = object.__new__(CustomObject)
    for name in ("sections", "symbols", "machine"):
        object.__setattr__(custom, name, getattr(obj, name))
    assert original_validator(elf)(custom) is None
    before = events[:]
    events.clear()
    assert elf._validate_object(custom) is None
    assert events == before == ["machine"] * 3
    events.clear()

    class CustomSection(ElfSection):
        def __getattribute__(self, name):
            if name == "relocations":
                events.append(name)
            return super().__getattribute__(name)

    section = obj.sections[0]
    custom_section = CustomSection(section.name, section.type, section.flags,
                                   section.align, section.data, section.mem_size,
                                   section.relocations)
    object.__setattr__(obj, "sections", (custom_section,))
    assert original_validator(elf)(obj) is None
    before = events[:]
    events.clear()
    assert elf._validate_object(obj) is None
    assert events == before == ["relocations"]

    # A property-returned temporary must not be retained in a new local: its
    # finalizer's original placement relative to row checks is observable.
    class TemporaryRows:
        def __iter__(self):
            events.append("iter")
            return iter((ElfRelocation(0, 1, 0), ElfRelocation(0, 1, 0)))

        def __del__(self):
            events.append("del")

    class TemporarySection(ElfSection):
        def __getattribute__(self, name):
            if name == "relocations":
                return TemporaryRows()
            return super().__getattribute__(name)

    temporary = TemporarySection(section.name, section.type, section.flags,
                                 section.align, section.data)
    object.__setattr__(obj, "sections", (temporary,))
    helper = elf._relocation_width

    def width(kind, machine):
        events.append("width")
        return helper(kind, machine)

    original = original_validator(elf)
    original.__globals__["_relocation_width"] = width
    monkeypatch.setattr(elf, "_relocation_width", width)
    events.clear()
    assert original(obj) is None
    before = events[:]
    events.clear()
    assert elf._validate_object(obj) is None
    assert events == before == ["iter", "del", "width", "width"]


def test_arm_packed_width_cache_rechecks_machine_after_custom_section():
    from tests.elf_validate_object_preimage import original_validator

    class MutatingRelocations:
        def __init__(self, obj):
            self.obj = obj

        def __iter__(self):
            object.__setattr__(self.obj, "machine", elf.EM_X86_64)
            return iter(())

    def make_object():
        obj = _raw_validation_object([(0, 1, 257)], second_rows=[(0, 1, 257)])
        middle = ElfSection(".custom", SHT_PROGBITS, SHF_ALLOC, 8, b"",
                            relocations=MutatingRelocations(obj))
        object.__setattr__(obj, "sections", (obj.sections[0], middle, obj.sections[1]))
        return obj

    before = _validation_outcome(original_validator(elf), make_object())
    after = _validation_outcome(elf._validate_object, make_object())
    assert after == before
    assert after[:2] == (ElfError, "x86_64 relocation type 257 is not supported")


def test_arm_packed_width_cache_same_input_alternating_receipt(tmp_path):
    # CI-only bounded observation in the existing Linux ELF host invocation.
    # It neither asserts speedup nor invokes a profiler or any native boundary.
    import json
    import platform
    import sys
    import time
    from pathlib import Path
    from tests.elf_validate_object_preimage import (
        ORIGINAL_VALIDATE_OBJECT_SOURCE,
        original_validator,
    )

    kinds = (0, 257, 258, 260, 261, 274, 275, 277, 278, 282, 283,
             284, 285, 286, 299, 311, 312, 541, 542, 549, 551)
    rows = [(0, 1, kind) for kind in kinds * 128]
    obj = _raw_validation_object(rows, second_rows=rows)
    frozen = tuple((section.data, section.relocations.payload) for section in obj.sections)
    validators = {"original": original_validator(elf), "candidate": elf._validate_object}
    rounds = []
    for round_index, order in enumerate(("original candidate", "candidate original",
                                         "original candidate", "candidate original")):
        for name in order.split():
            started = time.perf_counter_ns()
            assert validators[name](obj) is None
            elapsed = time.perf_counter_ns() - started
            rounds.append({"round": round_index, "validator": name, "elapsed_ns": elapsed})
    assert frozen == tuple((section.data, section.relocations.payload) for section in obj.sections)
    assert len(rounds) == 8 and all(row["elapsed_ns"] >= 0 for row in rounds)
    root = Path(__file__).resolve().parents[2]
    receipt = {
        "schema": "pcc.arm-packed-width-cache-ab.v1",
        "scope": "same frozen synthetic ARM RELA object; host validation only",
        "input": {"sha256": hashlib.sha256(b"".join(data + rela for data, rela in frozen)).hexdigest(),
                  "relocations": len(rows) * 2, "sections": len(obj.sections), "legal_types": len(kinds)},
        "source": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
            "pcc/backend/elf_x86_64.py", "pcc/backend/elf_aarch64_relocations.py",
            "tests/elf_validate_object_preimage.py", "tests/python/test_elf_x86_64.py")},
        "original_function_sha256": hashlib.sha256(ORIGINAL_VALIDATE_OBJECT_SOURCE.encode()).hexdigest(),
        "runtime": {"implementation": platform.python_implementation(), "version": sys.version,
                    "executable": sys.executable,
                    "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                    "platform": sys.platform, "machine": platform.machine()},
        "rounds": rounds,
        "timing_threshold": None,
    }
    output = tmp_path / "arm-packed-width-cache-ab.json"
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    assert output.stat().st_size <= 4096
    assert json.loads(output.read_text()) == receipt


@pytest.mark.parametrize("shape", ["negative-offset", "negative-type", "unhashable-type"])
def test_arm_packed_width_cache_preserves_generic_row_errors(shape):
    from tests.elf_validate_object_preimage import original_validator

    class UnhashableInt(int):
        __hash__ = None

    offset = -1 if shape == "negative-offset" else 0
    kind = -1 if shape == "negative-type" else UnhashableInt(0)
    obj = _raw_validation_object([])
    object.__setattr__(obj.sections[0], "relocations", (ElfRelocation(offset, 1, kind),))
    before = _validation_outcome(original_validator(elf), obj)
    assert _validation_outcome(elf._validate_object, obj) == before
    if shape == "unhashable-type":
        assert before is None
    else:
        assert before[0] is ElfError
