"""Production staged ELF input and metadata-layout contracts."""

from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
import hashlib
import platform
import struct
import subprocess
import sys
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import owned_elf_inputs, owned_elf_link
from pcc.backend.precise_stackmap import function_id
from tests.python.test_elf_x86_64 import (
    _ar_member, _archive_selection_fixture, _exit_42_object,
    _interleave_elf_section_headers,
)

TARGET = "x86_64-unknown-linux-gnu"
ASM = ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n mov edi, 42\n mov eax, 60\n syscall\n"


def _extra_inputs():
    from pcc.backend.linux_thread_intrinsics import assembly
    return [owned_elf_link._thread_pointer_object(TARGET),
            owned_elf_link.assemble(assembly(False), TARGET)]


def _raw_member(name, payload):
    return (name.encode("ascii").ljust(16, b" ")
            + b"0".ljust(12, b" ") + b"0".ljust(6, b" ")
            + b"0".ljust(6, b" ") + b"100644".ljust(8, b" ")
            + str(len(payload)).encode("ascii").ljust(10, b" ") + b"`\n"
            + payload + (b"\n" if len(payload) & 1 else b""))


def _archive(dialect):
    start, original = _archive_selection_fixture()
    members = elf.read_archive_payloads(original)
    if dialect == "ordinary":
        return start, original
    if dialect == "gnu":
        names = b"answer_with_a_long_filename.o/\nunused_with_a_long_filename.o/\n"
        data = b"!<arch>\n" + _raw_member("/", b"index") + _raw_member("//", names)
        data += _raw_member("/0", members[0][1])
        data += _raw_member("/" + str(names.index(b"unused")), members[1][1])
    elif dialect == "bsd":
        data = b"!<arch>\n" + _raw_member("#1/9", b"__.SYMDEF" + b"index")
        for name, payload in members:
            name = ("long_" + name).encode("ascii")
            data += _raw_member("#1/" + str(len(name)), name + payload)
    else:
        data = b"!<arch>\n" + _raw_member("/", b"index") + original[8:]
    return start, data


@pytest.mark.parametrize("dialect", ["ordinary", "gnu", "bsd", "coff"])
def test_staged_archives_match_owned_selection_bytes_and_map(tmp_path, dialect):
    start, archive = _archive(dialect)
    object_path = tmp_path / "start.o"
    object_path.write_bytes(elf.emit_relocatable(start))
    archive_path = tmp_path / "answer.a"
    archive_path.write_bytes(archive)
    output = tmp_path / "program"
    map_path = tmp_path / "program.map"
    selected = []
    expected = elf.link_static_executable([start] + _extra_inputs(), archives=[archive],
                                          archive_selections=selected)
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(object_path)],
                              archives=[str(archive_path)], map_path=str(map_path))
    assert output.read_bytes() == expected
    members = dict(elf.read_archive_payloads(archive))
    for index, name in selected:
        assert index == 0
        assert (str(archive_path) + "(" + name + ") archive_sha256="
                + hashlib.sha256(archive).hexdigest() + " member_sha256="
                + hashlib.sha256(members[name]).hexdigest()) in map_path.read_text()
    assert "unused" not in map_path.read_text()


@pytest.mark.parametrize("kind", ["PCO", "ASM", "manifest"])
def test_staged_direct_inputs_match_retained_bytes(tmp_path, kind):
    output = tmp_path / "program"
    options = dict(target=TARGET, output=str(output))
    if kind == "PCO":
        obj = _exit_42_object()
        path = tmp_path / "input.o"
        path.write_bytes(elf.emit_relocatable(obj))
        options["objects"] = [str(path)]
    else:
        obj = owned_elf_link.assemble(ASM, TARGET)
        path = tmp_path / "input.s"
        path.write_bytes(ASM.replace("\n", "\r\n").encode())
        if kind == "ASM":
            options["assembly"] = [str(path)]
        else:
            manifest = tmp_path / "manifest"
            manifest.write_text("pcc.macho-internal-inputs.v1\n1\nASM\t" + str(path) + "\n")
            options["manifest"] = str(manifest)
    expected = elf.link_static_executable([obj] + _extra_inputs())
    owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == expected


def test_staged_aarch64_ranges_match_retained_relocations_and_image(tmp_path):
    from pcc.backend.linux_thread_intrinsics import assembly

    target = "aarch64-unknown-linux-gnu"
    # _start calls answer, which returns 42 in x0, then performs Linux exit.
    text = bytes.fromhex("00000094 a80b80d2 010000d4 400580d2 c0035fd6")
    obj = elf.ElfObject(
        (elf.ElfSection(".text", 1, 6, 4, text,
                        relocations=(elf.ElfRelocation(0, 2, 283),)),
         elf.ElfSection(".data", 1, 3, 8, b"\0" * 8,
                        relocations=(elf.ElfRelocation(0, 2, 257),)),
         elf.ElfSection(".bss", 8, 3, 8, mem_size=8),
         elf.ElfSection(".empty", 1, 2, 1)),
        (elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 12, 1, 2),
         elf.ElfSymbol("answer", 1, 12, 8, 1, 2)), elf.EM_AARCH64,
    )
    encoded = _interleave_elf_section_headers(elf.emit_relocatable(obj))
    indexed, ranges = elf._parse_relocatable_indexed(encoded)
    assert indexed.machine == elf.EM_AARCH64 and indexed.symbols == obj.symbols
    for section, (offset, size, rela_offset, rela_size) in zip(obj.sections, ranges):
        assert encoded[offset:offset + size] == section.data
        assert tuple(elf._PackedElfRelocations(encoded, rela_offset, rela_size)) == (
            section.relocations)
    path = tmp_path / "arm.o"
    path.write_bytes(encoded)
    output = tmp_path / "arm-program"
    extra = [owned_elf_link._thread_pointer_object(target),
             owned_elf_link.assemble(assembly(True), target)]
    expected = elf.link_static_executable([obj] + extra)
    owned_elf_link.link_inputs(target=target, output=str(output), objects=[str(path)])
    assert output.read_bytes() == expected
    assert elf._ELF_HEADER.unpack_from(output.read_bytes())[2] == elf.EM_AARCH64


def test_staging_releases_parsed_payload_and_uses_one_reload_cache(tmp_path, monkeypatch):
    paths = []
    for index in range(4):
        path = tmp_path / (str(index) + ".o")
        obj = _exit_42_object()
        obj = replace(obj, symbols=(obj.symbols[0],
            replace(obj.symbols[1], name="_start" if index == 0 else "entry" + str(index)),
            replace(obj.symbols[2], name="answer" + str(index))))
        path.write_bytes(elf.emit_relocatable(obj))
        paths.append(str(path))
    refs = []
    reads = []
    stores = []
    parse = elf._parse_relocatable
    read = owned_elf_inputs.ElfInputStore._read

    def observed_parse(*args, **kwargs):
        assert not any(ref() is not None for ref in refs)
        obj = parse(*args, **kwargs)
        refs.append(weakref.ref(obj))
        return obj

    def observed_read(store, source):
        assert store.current is None and store.current_index == -1
        data, digest = read(store, source)
        assert type(data) is bytes and len(data) == source.size
        assert digest == hashlib.sha256(data).hexdigest()
        if source.sha256:
            assert source.sha256 == digest
        reads.append((source.path, source.sha256, digest))
        if not stores:
            stores.append(store)
        return data, digest

    monkeypatch.setattr(elf, "_parse_relocatable", observed_parse)
    monkeypatch.setattr(owned_elf_inputs.ElfInputStore, "_read", observed_read)
    output = tmp_path / "program"
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=paths)
    # Exactly one full validation per source, with identity-checked rereads for
    # the actual GOT/copy/relocation passes rather than another full parse.
    assert len(refs) == len(paths)
    assert [path for path, expected, _digest in reads if not expected] == paths
    assert len([row for row in reads if row[1]]) > len(paths)
    assert {path for path, expected, _digest in reads if expected} == set(paths)
    assert not any(ref() is not None for ref in refs)
    assert stores[0].current is None and stores[0].current_index == -1
    assert _retained_payloads(stores[0]) == []


def _retained_payloads(root):
    """Inspect only the staged input graph, including all private descriptors."""
    seen = set()
    payloads = []

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (bytes, bytearray, memoryview)):
            payloads.append(value)
        elif is_dataclass(value):
            for field in fields(value):
                visit(getattr(value, field.name))
        elif isinstance(value, owned_elf_inputs.ElfInputStore):
            visit(vars(value))
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                visit(item)

    visit(root)
    return payloads


@pytest.mark.parametrize("interspersed", [False, True])
def test_verified_ranges_retain_one_payload_and_bind_each_source(tmp_path, interspersed):
    store = owned_elf_inputs.ElfInputStore(TARGET)
    objects = []
    encoded = []
    for index in range(3):
        obj = _mapped_object(tls=True)
        section = obj.sections[1]
        obj = replace(obj, sections=(obj.sections[0],
            replace(section, data=(40 + index).to_bytes(4, "little")), *obj.sections[2:]))
        data = elf.emit_relocatable(obj)
        if interspersed:
            data = _interleave_elf_section_headers(data)
        path = tmp_path / (str(index) + ".o")
        path.write_bytes(data)
        staged = store.stage(str(path))
        objects.append(staged)
        encoded.append(data)
        source = store.sources[index]
        assert source.size == len(data)
        assert source.sha256 == hashlib.sha256(data).hexdigest()
        assert len(source.section_ranges) == len(obj.sections)
        assert all(type(row) is tuple and len(row) == 4
                   and all(type(value) is int for value in row)
                   for row in source.section_ranges)
        with pytest.raises(FrozenInstanceError):
            source.sha256 = "changed"
        with pytest.raises(TypeError):
            source.section_ranges[0][0] = 0
        assert _retained_payloads((store, objects)) == []
    for index in (2, 0, 1, 2, 0):
        obj = objects[index]
        assert obj.sections[1].data == (40 + index).to_bytes(4, "little")
        payload = store.current
        assert type(payload) is bytes and payload == encoded[index]
        assert store.current_index == index
        assert _retained_payloads((store, objects)) == [payload]
        relocations = obj.sections[0].relocations
        assert relocations.payload is payload
        assert relocations[0] == elf.ElfRelocation(2, 2, 2, -4)
        assert store.load(index) is payload
        with pytest.raises(TypeError):
            payload[0] = 0
        view = weakref.ref(relocations)
        del relocations, payload
        assert view() is None
    store.clear()
    assert store.current_index == -1
    assert _retained_payloads((store, objects)) == []


def test_assembly_staging_still_reassembles_after_cache_release(tmp_path, monkeypatch):
    path = tmp_path / "input.s"
    path.write_bytes(ASM.replace("\n", "\r\n").encode())
    assemble = owned_elf_link.assemble
    calls = []

    def observed_assemble(text, target):
        calls.append(text)
        return assemble(text, target)

    monkeypatch.setattr(owned_elf_link, "assemble", observed_assemble)
    store = owned_elf_inputs.ElfInputStore(TARGET)
    obj = store.stage(str(path), kind="ASM")
    assert store.sources[0].section_ranges is None
    assert store.current is None and len(calls) == 1
    first = obj.sections[0].data
    assert isinstance(store.current, elf.ElfObject) and len(calls) == 2
    assert obj.sections[0].data == first and len(calls) == 2
    store.clear()
    assert obj.sections[0].data == first and len(calls) == 3
    assert all(text == ASM for text in calls)
    store.clear()


@pytest.mark.parametrize("field,value,diagnostic", [
    (0, 1000, "relocation is outside its payload"),
    (1, (3 << 32) | elf.R_X86_64_PLT32, "relocation has invalid symbol index"),
    (1, (2 << 32) | 0xFFFFFFFF, "relocation type 4294967295 is not supported"),
])
@pytest.mark.parametrize("in_archive", [False, True])
def test_staged_malformed_relocations_fail_even_in_unused_archive_member(
    tmp_path, field, value, diagnostic, in_archive,
):
    payload = bytearray(elf.emit_relocatable(_exit_42_object()))
    header = elf._ELF_HEADER.unpack_from(payload)
    sections = [elf._SECTION_HEADER.unpack_from(payload, header[6] + index * 64)
                for index in range(header[12])]
    rela = next(row for row in sections if row[1] == 4)
    record = list(struct.unpack_from("<QQq", payload, rela[4]))
    record[field] = value
    struct.pack_into("<QQq", payload, rela[4], *record)
    path = tmp_path / "input.o"
    output = tmp_path / "program"
    options = dict(target=TARGET, output=str(output), objects=[str(path)])
    if in_archive:
        path.write_bytes(elf.emit_relocatable(_exit_42_object()))
        archive = tmp_path / "unused.a"
        archive.write_bytes(b"!<arch>\n" + _ar_member("bad.o", payload))
        options["archives"] = [str(archive)]
    else:
        path.write_bytes(payload)
    with pytest.raises(elf.ElfError, match=diagnostic):
        owned_elf_link.link_inputs(**options)
    assert not output.exists()


@pytest.mark.parametrize("in_archive", [False, True])
def test_invalid_stackmap_cannot_publish_a_reusable_index(tmp_path, in_archive):
    data = bytearray(elf.emit_relocatable(_mapped_object()))
    data[data.index(b"PCCSMAP1")] = 0
    unpublished = []
    with pytest.raises(elf.ElfError, match="invalid .pcc_stackmaps") as ordinary:
        elf.parse_relocatable(data)
    with pytest.raises(elf.ElfError) as indexed:
        elf._parse_relocatable(data, compact_relocations=True,
                               _section_ranges=unpublished)
    assert str(indexed.value) == str(ordinary.value)
    assert unpublished == []
    path = tmp_path / "input.o"
    output = tmp_path / "program"
    output.write_bytes(b"previous executable")
    options = dict(target=TARGET, output=str(output), objects=[str(path)])
    if in_archive:
        path.write_bytes(elf.emit_relocatable(_exit_42_object()))
        archive = tmp_path / "unused.a"
        archive.write_bytes(b"!<arch>\n" + _ar_member("bad.o", data))
        options["archives"] = [str(archive)]
    else:
        path.write_bytes(data)
    with pytest.raises(elf.ElfError, match="invalid .pcc_stackmaps"):
        owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == b"previous executable"
    assert not output.with_name("program.pcc-link.tmp").exists()


@pytest.mark.parametrize("source_kind", ["object", "assembly", "archive", "manifest"])
@pytest.mark.parametrize("mutation", ["rewrite", "append", "truncate"])
def test_source_drift_preserves_previous_executable_and_map(
    tmp_path, monkeypatch, source_kind, mutation,
):
    path = tmp_path / "input"
    output = tmp_path / "program"
    map_path = tmp_path / "program.map"
    output.write_bytes(b"previous image")
    map_path.write_text("previous map")
    options = dict(target=TARGET, output=str(output), map_path=str(map_path))
    if source_kind == "assembly":
        path.write_text(ASM)
        options["assembly"] = [str(path)]
    elif source_kind == "archive":
        start, archive = _archive_selection_fixture()
        object_path = tmp_path / "start.o"
        object_path.write_bytes(elf.emit_relocatable(start))
        path.write_bytes(archive)
        options.update(objects=[str(object_path)], archives=[str(path)])
    else:
        object_path = path if source_kind == "object" else tmp_path / "start.o"
        object_path.write_bytes(elf.emit_relocatable(_exit_42_object()))
        if source_kind == "object":
            options["objects"] = [str(path)]
        else:
            path.write_text("pcc.macho-internal-inputs.v1\n1\nPCO\t" + str(object_path) + "\n")
            options["manifest"] = str(path)
    link = owned_elf_link._link_static_executable_image

    def mutate_after_link(*args, **kwargs):
        image = link(*args, **kwargs)
        data = path.read_bytes()
        if mutation == "rewrite":
            data = data[:-1] + bytes([data[-1] ^ 1])
        elif mutation == "append":
            data += b"extra"
        else:
            data = data[:-1]
        path.write_bytes(data)
        return image

    monkeypatch.setattr(owned_elf_link, "_link_static_executable_image", mutate_after_link)
    with pytest.raises(elf.ElfError, match="changed|truncated"):
        owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == b"previous image"
    assert map_path.read_text() == "previous map"
    assert not output.with_name("program.pcc-link.tmp").exists()


@pytest.mark.parametrize("access", ["data", "relocations"])
@pytest.mark.parametrize("mutation", ["rewrite", "symbols", "rela", "append", "truncate"])
@pytest.mark.parametrize("in_archive", [False, True])
def test_reload_rejects_drift_before_using_payload(tmp_path, access, mutation, in_archive):
    path = tmp_path / "input.o"
    encoded = elf.emit_relocatable(_exit_42_object())
    path.write_bytes(encoded)
    store = owned_elf_inputs.ElfInputStore(TARGET)
    if in_archive:
        path = tmp_path / "input.a"
        path.write_bytes(b"!<arch>\n" + _ar_member("start.o", encoded))
        store = owned_elf_inputs.ElfInputStore(TARGET, [str(path)])
        obj = store.read_archive(0)[0].object
    else:
        obj = store.stage(str(path))
    # A successful first load must not exempt a later load from identity checks.
    assert obj.sections[0].data
    store.clear()
    payload = path.read_bytes()
    if mutation in ("rewrite", "symbols", "rela"):
        # Change header/symbol/RELA bytes without changing the complete length.
        offset = store.sources[0].offset
        if mutation != "rewrite":
            header = elf._ELF_HEADER.unpack_from(payload, offset)
            rows = [elf._SECTION_HEADER.unpack_from(payload, offset + header[6] + index * 64)
                    for index in range(header[12])]
            table_type = elf.SHT_SYMTAB if mutation == "symbols" else elf.SHT_RELA
            offset += next(row[4] for row in rows if row[1] == table_type)
        payload = payload[:offset] + bytes([payload[offset] ^ 1]) + payload[offset + 1:]
    elif mutation == "append":
        payload += b"extra"
    else:
        source = store.sources[0]
        payload = payload[:source.offset + source.size - 1]
    path.write_bytes(payload)
    if in_archive and mutation == "append":
        # Member rereads remain bounded; complete-archive verification catches
        # an append outside the validated member range before publication.
        assert getattr(obj.sections[0], access)
        with pytest.raises(elf.ElfError, match="archive identity changed"):
            store.verify()
    else:
        with pytest.raises(elf.ElfError, match="changed|truncated"):
            getattr(obj.sections[0], access)
    assert store.current is None and store.current_index == -1
    assert _retained_payloads((store, obj)) == []


def test_failed_stage_or_reload_releases_previous_cache(tmp_path):
    path = tmp_path / "good.o"
    original = elf.emit_relocatable(_exit_42_object())
    path.write_bytes(original)
    store = owned_elf_inputs.ElfInputStore(TARGET)
    obj = store.stage(str(path))
    assert obj.sections[0].data
    bad = tmp_path / "bad.o"
    bad.write_bytes(b"invalid")
    with pytest.raises(elf.ElfError):
        store.stage(str(bad))
    assert len(store.sources) == 1
    assert store.current is None and store.current_index == -1
    assert _retained_payloads((store, obj)) == []
    other = tmp_path / "other.o"
    other.write_bytes(original)
    second = store.stage(str(other))
    assert obj.sections[0].data
    other.write_bytes(original[:-1])
    with pytest.raises(elf.ElfError, match="truncated"):
        _ = second.sections[0].data
    assert store.current is None and store.current_index == -1
    assert _retained_payloads((store, obj, second)) == []


def test_staged_index_size_tracks_sections_not_relocation_count(tmp_path):
    count = 1024
    obj = elf.ElfObject(
        (elf.ElfSection(".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR,
                        8, b"\0" * (8 * count), relocations=tuple(
                            elf.ElfRelocation(8 * index, 1, elf.R_X86_64_64)
                            for index in range(count))),),
        (elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 8 * count, 1, 2)),
    )
    path = tmp_path / "many.o"
    data = elf.emit_relocatable(obj)
    path.write_bytes(data)
    store = owned_elf_inputs.ElfInputStore(TARGET)
    staged = store.stage(str(path))
    assert len(store.sources[0].section_ranges) == 1
    assert len(store.sources[0].section_ranges[0]) == 4
    assert staged.sections[0].relocation_count == count
    assert _retained_payloads((store, staged)) == []
    relocations = staged.sections[0].relocations
    assert len(relocations) == count
    assert relocations.payload is store.current
    assert relocations.size == count * elf._RELA.size
    assert tuple(relocations) == obj.sections[0].relocations
    del relocations
    store.clear()
    assert _retained_payloads((store, staged)) == []


def test_undefined_and_duplicate_strong_symbols_remain_strict(tmp_path):
    start, _archive = _archive_selection_fixture()
    path = tmp_path / "start.o"
    path.write_bytes(elf.emit_relocatable(start))
    with pytest.raises(elf.ElfError, match="undefined static ELF symbols: answer"):
        owned_elf_link.link_inputs(target=TARGET, output=str(tmp_path / "missing"), objects=[str(path)])
    path.write_bytes(elf.emit_relocatable(_exit_42_object()))
    with pytest.raises(elf.ElfError, match="duplicate strong definition"):
        owned_elf_link.link_inputs(target=TARGET, output=str(tmp_path / "duplicate"),
                                  objects=[str(path), str(path)])


def test_archive_duplicate_member_names_preserve_no_map_semantics(tmp_path):
    start, archive = _archive_selection_fixture()
    members = elf.read_archive_payloads(archive)
    archive = b"!<arch>\n" + b"".join(_ar_member("same.o", data) for _name, data in members)
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(start))
    archive_path = tmp_path / "input.a"
    archive_path.write_bytes(archive)
    output = tmp_path / "program"
    options = dict(target=TARGET, output=str(output), objects=[str(path)], archives=[str(archive_path)])
    expected = elf.link_static_executable([start] + _extra_inputs(), archives=[archive])
    owned_elf_link.link_inputs(**options)
    assert output.read_bytes() == expected
    with pytest.raises(elf.ElfError, match="duplicate archive member names"):
        owned_elf_link.link_inputs(**options, map_path=str(tmp_path / "map"))
    assert output.read_bytes() == expected


def _mapped_object(*, bss=8192, incoming_map=False, tls=False):
    text = bytes.fromhex("8b3d00000000033d00000000b83c0000000f05")
    relocations = [elf.ElfRelocation(2, 2, 2, -4), elf.ElfRelocation(8, 3, 2, -4)]
    if incoming_map:
        text += b"\0" * 4
        relocations.append(elf.ElfRelocation(len(text) - 4, 4, 2, 0))
    payload = (struct.pack("<8sHBBIII", b"PCCSMAP1", 2, 2, 8, 1, 0, 0)
               + struct.pack("<QQIIII", function_id("_start"), 0, len(text), 0, 0, 0))
    sections = [
        elf.ElfSection(".text", 1, 6, 16, text, relocations=tuple(relocations)),
        elf.ElfSection(".data", 1, 3, 16, (42).to_bytes(4, "little")),
        elf.ElfSection(".bss", 8, 3, 4096, mem_size=bss),
        elf.ElfSection(".pcc_stackmaps", 1, 2, 8, payload,
                       relocations=(elf.ElfRelocation(32, 1, 1),)),
    ]
    symbols = [elf.ElfSymbol.null(),
               elf.ElfSymbol("_start", 1, 0, len(text), 1, 2),
               elf.ElfSymbol("answer", 2, 0, 4, 1, 1),
               elf.ElfSymbol("zero", 3, 0, 4, 1, 1),
               elf.ElfSymbol("map_begin", 4, 0, len(payload), 1, 1)]
    if tls:
        sections.extend([
            elf.ElfSection(".tdata", 1, 3 | elf.SHF_TLS, 8, b"\0" * 8),
            elf.ElfSection(".tbss", 8, 3 | elf.SHF_TLS, 16, mem_size=32),
        ])
        symbols.append(elf.ElfSymbol("tls_var", 5, 0, 8, 1, elf.STT_TLS))
    return elf.ElfObject(tuple(sections), tuple(symbols))


@pytest.mark.parametrize("tls", [False, True])
def test_metadata_load_is_readonly_after_rw_bss_and_tls(tmp_path, tls):
    obj = _mapped_object(tls=tls)
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(obj))
    output = tmp_path / "program"
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(path)])
    image = output.read_bytes()
    assert image == elf.link_static_executable([obj] + _extra_inputs())
    shape = elf.parse_static_executable(image)
    assert shape["load_segments"] == 3 and shape["tls_segments"] == int(tls)
    header = elf._ELF_HEADER.unpack_from(image)
    headers = [elf._PROGRAM_HEADER.unpack_from(image, header[5] + index * 56)
               for index in range(header[10])]
    loads = [row for row in headers if row[0] == elf.PT_LOAD]
    assert [row[1] for row in loads] == [5, 6, 4]
    assert all(row[2] % 4096 == row[3] % 4096 for row in loads)
    assert all(loads[index][3] + loads[index][6] <= loads[index + 1][3] for index in range(2))
    metadata = loads[-1]
    assert metadata[3] - elf._BASE > metadata[2]
    assert int.from_bytes(image[metadata[2] + 32:metadata[2] + 40], "little") == header[4]
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(path)])
    assert output.read_bytes() == image


@pytest.mark.pcc_gate(unavailable=(
    None if sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64")
    else "emitted ELF execution requires Linux x86_64"
))
@pytest.mark.parametrize("tls", [False, True])
def test_staged_metadata_image_executes_data_and_bss_relocations(tmp_path, tls):
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(_mapped_object(tls=tls)))
    output = tmp_path / "program"
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(path)])
    result = subprocess.run([str(output)], capture_output=True, timeout=5)
    assert result.returncode == 42, result.stderr


def test_sparse_metadata_pc32_overflow_remains_strict(tmp_path):
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(_mapped_object(bss=3 * 1024**3, incoming_map=True)))
    # NOBITS changes virtual extent without increasing file payload. The
    # section's 4096-byte alignment accounts for the small object's padding.
    assert path.stat().st_size == len(elf.emit_relocatable(
        _mapped_object(bss=8192, incoming_map=True)))
    output = tmp_path / "program"
    with pytest.raises(elf.ElfError, match="does not fit signed i32"):
        owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(path)])
    assert not output.exists()


def test_staged_input_records_are_visible_to_inventory(tmp_path):
    from tests.python.test_pcc_record_inventory_tool import _load_tool
    tool = _load_tool()
    assert tool.data_plane_class_contract_report()["unclassified"] == []
    assert tool.data_plane_class_contract_report()["stale_classifications"] == []
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(_exit_42_object()))
    store = owned_elf_inputs.ElfInputStore(TARGET)
    obj = store.stage(str(path))
    report = tool._graph_inventory((store, obj))
    for name in ("_InputSource", "_InputSection", "_InputObject", "ElfInputStore"):
        assert "owned_elf_inputs.py:" + name in tool.DATA_PLANE_CLASS_CONTRACT
        assert report["families"][name]["unique_objects"] >= 1
    assert "ElfObject" not in report["families"]


def test_staged_got_tls_image_matches_retained_layout(tmp_path):
    obj = elf.ElfObject(
        (elf.ElfSection(".text", 1, 6, 16, b"\0" * 8, relocations=(
            elf.ElfRelocation(0, 2, elf.R_X86_64_GOTTPOFF, -4),
            elf.ElfRelocation(4, 3, elf.R_X86_64_GOTPCREL, -4))),
         elf.ElfSection(".tdata", 1, 3 | elf.SHF_TLS, 8, (37).to_bytes(8, "little")),
         elf.ElfSection(".data", 1, 3, 8, (42).to_bytes(8, "little"))),
        (elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 8, 1, 2),
         elf.ElfSymbol("tls_var", 2, 0, 8, 1, elf.STT_TLS),
         elf.ElfSymbol("answer", 3, 0, 8, 1, 1)))
    path = tmp_path / "input.o"
    path.write_bytes(elf.emit_relocatable(obj))
    output = tmp_path / "program"
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=[str(path)])
    assert output.read_bytes() == elf.link_static_executable([obj] + _extra_inputs())
    assert elf.parse_static_executable(output.read_bytes())["tls_segments"] == 1


def test_staged_archive_rescan_closure_and_archive_order_match_retained(tmp_path):
    start, original = _archive_selection_fixture()
    answer = elf.read_archive(original)[0].object
    # answer needs helper. Within one archive helper precedes answer, so
    # selecting answer requires the existing member-rescan fixed point.
    helper = elf.ElfObject((), (elf.ElfSymbol.null(),
        elf.ElfSymbol("helper", elf.SHN_ABS, 42, 0, 1, 0)))
    answer = replace(answer, symbols=answer.symbols + (
        elf.ElfSymbol("helper", elf.SHN_UNDEF, 0, 0, 1, 0),))
    path = tmp_path / "start.o"
    path.write_bytes(elf.emit_relocatable(start))
    helper_data = _ar_member("helper.o", elf.emit_relocatable(helper))
    answer_data = _ar_member("answer.o", elf.emit_relocatable(answer))
    archive = tmp_path / "all.a"
    archive.write_bytes(b"!<arch>\n" + helper_data + answer_data)
    output = tmp_path / "program"
    options = dict(target=TARGET, output=str(output), objects=[str(path)])
    owned_elf_link.link_inputs(**options, archives=[str(archive)])
    assert output.read_bytes() == elf.link_static_executable(
        [start] + _extra_inputs(), archives=[archive.read_bytes()])
    first = tmp_path / "first.a"
    second = tmp_path / "second.a"
    first.write_bytes(b"!<arch>\n" + helper_data)
    second.write_bytes(b"!<arch>\n" + answer_data)
    with pytest.raises(elf.ElfError, match="undefined static ELF symbols: helper"):
        owned_elf_link.link_inputs(**options, archives=[str(first), str(second)])
    owned_elf_link.link_inputs(**options, archives=[str(second), str(first)])
    assert output.read_bytes() == elf.link_static_executable(
        [start] + _extra_inputs(), archives=[second.read_bytes(), first.read_bytes()])


def test_staged_weak_undefined_and_strong_over_weak_match_retained(tmp_path):
    obj = _exit_42_object()
    obj = replace(obj, symbols=obj.symbols + (
        elf.ElfSymbol("optional", elf.SHN_UNDEF, 0, 0, elf.STB_WEAK, 0),
        elf.ElfSymbol("override", elf.SHN_ABS, 7, 0, elf.STB_WEAK, 0)))
    other = elf.ElfObject((), (elf.ElfSymbol.null(),
        elf.ElfSymbol("override", elf.SHN_ABS, 42, 0, 1, 0)))
    paths = []
    for index, value in enumerate((obj, other)):
        path = tmp_path / (str(index) + ".o")
        path.write_bytes(elf.emit_relocatable(value))
        paths.append(str(path))
    output = tmp_path / "program"
    owned_elf_link.link_inputs(target=TARGET, output=str(output), objects=paths)
    assert output.read_bytes() == elf.link_static_executable([obj, other] + _extra_inputs())


@pytest.mark.parametrize("destination", ["input", "archive", "input-temporary", "symlink"])
def test_staged_publication_rejects_source_aliases(tmp_path, destination):
    obj = tmp_path / "input.o"
    original = elf.emit_relocatable(_exit_42_object())
    obj.write_bytes(original)
    archive = tmp_path / "input.a"
    archive_bytes = b"!<arch>\n"
    archive.write_bytes(archive_bytes)
    if destination == "archive":
        output = archive
    elif destination == "input":
        output = obj
    elif destination == "input-temporary":
        obj = tmp_path / "program.pcc-link.tmp"
        obj.write_bytes(original)
        output = tmp_path / "program"
    else:
        output = tmp_path / "alias"
        output.symlink_to(obj)
    with pytest.raises(elf.ElfError, match="link output aliases an input"):
        owned_elf_link.link_inputs(target=TARGET, output=str(output),
                                  objects=[str(obj)], archives=[str(archive)])
    assert obj.read_bytes() == original
    assert archive.read_bytes() == archive_bytes
