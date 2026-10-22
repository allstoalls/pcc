"""A pcc-linked executable that dyld loads and runs.

The end of the toolchain: `pcc.backend.macho_exec.link_executable` turns
pcc-emitted objects into an `MH_EXECUTE` — addresses assigned, relocations
applied, libSystem imports bound through `__stubs` + `__got` with chained
fixups, ad-hoc signed by pcc — with no `ld`, no `as`, and no `codesign`
anywhere in the path.

The bar is behavioral, not structural: the kernel must load it, dyld must
bind it, `codesign --verify` must accept it, and the program must produce the
right output and exit status. A structural check alone would have passed
several intermediate versions that the kernel killed on sight.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from pcc.backend import macho_obj, macho_spec as spec
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.macho_exec import link_executable
from pcc.backend.macho_link import LinkError

_CC = shutil.which(os.environ.get("CC", "cc"))
_IS_ARM64_DARWIN = os.uname().sysname == "Darwin" and os.uname().machine == "arm64"
_GATE = None if (_CC and _IS_ARM64_DARWIN) else "needs cc and Darwin arm64"

pytestmark = pytest.mark.pcc_gate(unavailable=_GATE)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


# Calls libSystem's puts (an import bound through stub+GOT), reads its own
# __DATA, and returns a computed status.
WITH_IMPORTS = """\
.section __DATA,__data
.p2align 3
.globl _counter
_counter:
  .quad 41
.section __TEXT,__cstring,cstring_literals
Lmsg:
  .byte 104, 105, 0
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _main
_main:
  paciasp
  stp x29, x30, [sp, #-16]!
  mov x29, sp
  adrp x0, Lmsg@PAGE
  add x0, x0, Lmsg@PAGEOFF
  bl _puts
  adrp x9, _counter@PAGE
  add x9, x9, _counter@PAGEOFF
  ldur x0, [x9]
  add w0, w0, #1
  ldp x29, x30, [sp], #16
  autiasp
  ret
.subsections_via_symbols
"""

NO_IMPORTS = """\
.section __DATA,__data
.p2align 3
.globl _counter
_counter:
  .quad 41
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _main
_main:
  adrp x9, _counter@PAGE
  add x9, x9, _counter@PAGEOFF
  ldur x0, [x9]
  add w0, w0, #1
  ret
.subsections_via_symbols
"""

# Two objects: main calls a helper defined in the other one, which calls puts.
UNIT_MAIN = """\
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _main
_main:
  paciasp
  stp x29, x30, [sp, #-16]!
  mov x29, sp
  bl _greet
  ldp x29, x30, [sp], #16
  autiasp
  ret
.subsections_via_symbols
"""

UNIT_HELPER = """\
.section __TEXT,__cstring,cstring_literals
Lg:
  .byte 121, 111, 0
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _greet
_greet:
  paciasp
  stp x29, x30, [sp, #-16]!
  mov x29, sp
  adrp x0, Lg@PAGE
  add x0, x0, Lg@PAGEOFF
  bl _puts
  movz w0, #7
  ldp x29, x30, [sp], #16
  autiasp
  ret
.subsections_via_symbols
"""


def _object(unit: str) -> bytes:
    sections, undefined = assemble_file(unit)
    return macho_obj.emit_object(sections, undefined=undefined)


def _link(tmp_path: Path, name: str, units: list[str]) -> Path:
    exe = tmp_path / name
    exe.write_bytes(link_executable([_object(u) for u in units]))
    exe.chmod(0o755)
    return exe


def _segments(obj):
    return [
        command for command in obj.commands
        if command.cmd == spec.LC_SEGMENT_64
    ]


def _command_string(command, field_offset: int) -> bytes:
    string_offset, = struct.unpack_from("<I", command.raw, field_offset)
    assert 0 < string_offset < command.cmdsize
    return command.raw[string_offset:].split(b"\0", 1)[0]


def test_pcc_linked_binary_calls_libsystem_and_runs(tmp_path):
    exe = _link(tmp_path, "with_imports", [WITH_IMPORTS])
    run = _run([str(exe)])
    assert run.stdout == "hi\n", (run.stdout, run.stderr)
    assert run.returncode == 42, run.returncode


def test_pcc_linked_binary_without_imports_runs(tmp_path):
    exe = _link(tmp_path, "no_imports", [NO_IMPORTS])
    assert _run([str(exe)]).returncode == 42


def test_minimal_executable_layout_is_page_aligned_and_nonoverlapping():
    image = link_executable([_object(NO_IMPORTS)])
    obj = spec.parse_object(image)
    segments = _segments(obj)
    assert [segment.body["segname_str"] for segment in segments] == [
        "__PAGEZERO", "__TEXT", "__DATA_CONST", "__DATA", "__LINKEDIT",
    ]
    pagezero, text, data_const, data, linkedit = [
        segment.body for segment in segments
    ]

    assert (pagezero["vmaddr"], pagezero["fileoff"], pagezero["filesize"]) == (
        0, 0, 0,
    )
    assert pagezero["vmsize"] == text["vmaddr"]
    assert text["fileoff"] == 0
    assert text["vmaddr"] + text["vmsize"] <= data_const["vmaddr"]
    assert data_const["vmaddr"] + data_const["vmsize"] <= data["vmaddr"]
    assert data["vmaddr"] + data["vmsize"] <= linkedit["vmaddr"]
    assert text["filesize"] <= data_const["fileoff"]
    assert data_const["fileoff"] + data_const["filesize"] <= data["fileoff"]
    assert data["fileoff"] + data["filesize"] <= linkedit["fileoff"]
    for segment in (text, data_const, data, linkedit):
        assert segment["vmaddr"] % 0x4000 == 0
        assert segment["fileoff"] % 0x4000 == 0

    main_command = obj.command(spec.LC_MAIN)
    assert main_command is not None
    _cmd, _cmdsize, entryoff, stacksize = struct.unpack(
        "<IIQQ", main_command.raw,
    )
    assert 0 <= entryoff < text["filesize"]
    assert stacksize == 0
    section_names = {section["sectname_str"] for section in obj.sections()}
    assert "__stubs" not in section_names
    assert "__got" not in section_names


def test_multiple_objects_link_into_one_executable(tmp_path):
    exe = _link(tmp_path, "two_units", [UNIT_MAIN, UNIT_HELPER])
    run = _run([str(exe)])
    assert run.stdout == "yo\n", (run.stdout, run.stderr)
    assert run.returncode == 7


def test_the_binary_is_validly_signed(tmp_path):
    exe = _link(tmp_path, "signed", [WITH_IMPORTS])
    verify = _run(["codesign", "--verify", "--strict", str(exe)])
    assert verify.returncode == 0, verify.stderr


def test_the_dyld_surface_is_what_was_chosen():
    image = link_executable([_object(WITH_IMPORTS)])
    obj = spec.parse_object(image)
    assert obj.header["filetype"] == spec.MH_EXECUTE
    present = {lc.cmd for lc in obj.commands}
    for cmd in (spec.LC_DYLD_CHAINED_FIXUPS, spec.LC_MAIN, spec.LC_LOAD_DYLIB,
                spec.LC_LOAD_DYLINKER, spec.LC_BUILD_VERSION, spec.LC_UUID,
                spec.LC_SYMTAB, spec.LC_DYSYMTAB, spec.LC_CODE_SIGNATURE):
        assert cmd in present, hex(cmd)
    # Classic dyld info was deliberately not implemented.
    assert spec.LC_DYLD_INFO_ONLY not in present
    names = {(s["segname_str"], s["sectname_str"]) for s in obj.sections()}
    assert ("__TEXT", "__stubs") in names
    assert ("__DATA_CONST", "__got") in names

    dylibs = [command for command in obj.commands if command.cmd == spec.LC_LOAD_DYLIB]
    assert len(dylibs) == 1
    assert _command_string(dylibs[0], 8) == b"/usr/lib/libSystem.B.dylib"
    dyld = obj.command(spec.LC_LOAD_DYLINKER)
    assert dyld is not None
    assert _command_string(dyld, 8) == b"/usr/lib/dyld"

    build = obj.command(spec.LC_BUILD_VERSION)
    assert build is not None
    assert build.body["platform"] == spec.PLATFORM_MACOS
    assert build.body["minos"] == 0x000C0000
    assert build.body["ntools"] == 0

    sections = obj.sections()
    stubs = next(section for section in sections if section["sectname_str"] == "__stubs")
    got = next(section for section in sections if section["sectname_str"] == "__got")
    assert stubs["flags"] & spec.SECTION_TYPE == spec.S_REGULAR
    assert got["flags"] & spec.SECTION_TYPE == spec.S_REGULAR
    assert (stubs["reserved1"], got["reserved1"]) == (0, 0)

    fixup_command = obj.command(spec.LC_DYLD_CHAINED_FIXUPS)
    assert fixup_command is not None
    _cmd, _cmdsize, dataoff, datasize = struct.unpack(
        "<IIII", fixup_command.raw,
    )
    fixups = image[dataoff:dataoff + datasize]
    (
        version, starts_off, imports_off, symbols_off,
        imports_count, imports_format, symbols_format,
    ) = struct.unpack_from("<7I", fixups)
    assert (version, imports_count, imports_format, symbols_format) == (0, 1, 1, 0)

    segment_count, = struct.unpack_from("<I", fixups, starts_off)
    starts = struct.unpack_from(
        f"<{segment_count}I", fixups, starts_off + 4,
    )
    assert segment_count == len(_segments(obj))
    assert [index for index, offset in enumerate(starts) if offset] == [2]
    info_at = starts_off + starts[2]
    (
        _size, page_size, pointer_format, segment_offset,
        max_valid_pointer, page_count,
    ) = struct.unpack_from("<IHHQIH", fixups, info_at)
    assert (page_size, pointer_format, max_valid_pointer) == (0x4000, 6, 0)
    data_const = _segments(obj)[2].body
    assert segment_offset == data_const["fileoff"]
    assert page_count == 1
    page_start, = struct.unpack_from("<H", fixups, info_at + 22)
    assert page_start == got["addr"] - data_const["vmaddr"]

    import_word, = struct.unpack_from("<I", fixups, imports_off)
    assert import_word & 0xFF == 1  # libSystem is dylib ordinal 1
    assert (import_word >> 8) & 1 == 0  # non-weak import
    name_offset = import_word >> 9
    imported_name = fixups[symbols_off + name_offset:].split(b"\0", 1)[0]
    assert imported_name == b"_puts"

    bind_word, = struct.unpack_from("<Q", image, got["offset"])
    assert bind_word >> 63 == 1
    assert bind_word & 0xFFFFFF == 0  # index of _puts in the import table
    assert (bind_word >> 51) & 0xFFF == 0  # one-site chain terminator

    puts = next(symbol for symbol in obj.symbols() if symbol["name"] == "_puts")
    assert puts["n_type"] == spec.N_UNDF | spec.N_EXT
    assert (puts["n_desc"] >> 8) & 0xFF == 1


def test_dynamic_symbol_table_partitions_match_the_nlist_order():
    image = link_executable([_object(WITH_IMPORTS)])
    obj = spec.parse_object(image)
    command = obj.command(spec.LC_DYSYMTAB)
    assert command is not None
    body = command.body
    symbols = obj.symbols()

    local_begin = body["ilocalsym"]
    local_end = local_begin + body["nlocalsym"]
    ext_begin = body["iextdefsym"]
    ext_end = ext_begin + body["nextdefsym"]
    undef_begin = body["iundefsym"]
    undef_end = undef_begin + body["nundefsym"]
    assert (local_begin, ext_begin, undef_begin, undef_end) == (
        0, local_end, ext_end, len(symbols),
    )

    locals_ = symbols[local_begin:local_end]
    extdefs = symbols[ext_begin:ext_end]
    undefs = symbols[undef_begin:undef_end]
    assert locals_, "WITH_IMPORTS must retain its referenced local cstring label"
    assert all(not (symbol["n_type"] & spec.N_EXT) for symbol in locals_)
    assert all(
        symbol["n_type"] & spec.N_EXT
        and (symbol["n_type"] & spec.N_TYPE) == spec.N_SECT
        for symbol in extdefs
    )
    assert all(
        symbol["n_type"] == spec.N_UNDF | spec.N_EXT for symbol in undefs
    )


def test_fails_closed_on_a_missing_entry_point(tmp_path):
    with pytest.raises(LinkError):
        link_executable([_object(UNIT_HELPER)])  # defines _greet, not _main


def test_fails_closed_on_unreferenced_undefined_and_duplicate_definitions():
    text = macho_obj.Section(
        sectname="__text",
        segname="__TEXT",
        data=struct.pack("<I", 0xD65F03C0),
        align_log2=2,
        flags=macho_obj.TEXT_SECTION_FLAGS,
        symbols=(macho_obj.TextSymbol("_main", 0),),
    )
    dangling = macho_obj.emit_object([text], undefined=("_not_imported",))
    with pytest.raises(LinkError, match="undefined symbol.*no relocation"):
        link_executable([dangling])

    leaf = macho_obj.emit_object([text])
    with pytest.raises(LinkError, match="duplicate definition"):
        link_executable([leaf, leaf])


def test_fails_closed_below_chained_fixup_os_floor():
    with pytest.raises(LinkError, match=r"requires minos >= \(12, 0\)"):
        link_executable([_object(NO_IMPORTS)], minos=(11, 6))


def test_fails_closed_on_an_unproven_input_load_command():
    data = bytearray(_object(NO_IMPORTS))
    parsed = spec.parse_object(bytes(data))
    build = parsed.command(spec.LC_BUILD_VERSION)
    assert build is not None
    struct.pack_into("<I", data, build.offset, spec.LC_LINKER_OPTION)
    with pytest.raises(LinkError, match=r"unsupported load command.*0x2d"):
        link_executable([bytes(data)])


def test_accepts_bounded_optional_linker_optimization_hints():
    text = macho_obj.Section(
        sectname="__text",
        segname="__TEXT",
        data=struct.pack("<I", 0xD65F03C0) + b"hintdata",
        align_log2=2,
        flags=macho_obj.TEXT_SECTION_FLAGS,
        symbols=(macho_obj.TextSymbol("_main", 0),),
        data_in_code=(macho_obj.DataInCodeRegion(4, 8),),
    )
    data = bytearray(macho_obj.emit_object([text]))
    parsed = spec.parse_object(bytes(data))
    command = parsed.command(spec.LC_DATA_IN_CODE)
    assert command is not None
    struct.pack_into(
        "<I",
        data,
        command.offset,
        spec.LC_LINKER_OPTIMIZATION_HINT,
    )
    image = link_executable([bytes(data)])
    assert spec.parse_object(image).header["filetype"] == spec.MH_EXECUTE

    malformed = bytearray(data)
    struct.pack_into("<I", malformed, command.offset + 8, len(malformed) + 1)
    with pytest.raises(LinkError, match="invalid linker hints.*outside file"):
        link_executable([bytes(malformed)])


def test_fails_closed_when_entry_is_data_or_import_shape_is_unsupported():
    data_entry = macho_obj.emit_object([
        macho_obj.Section(
            sectname="__data",
            segname="__DATA",
            data=b"\0" * 8,
            symbols=(macho_obj.TextSymbol("_main", 0),),
        ),
    ])
    with pytest.raises(LinkError, match="executable __TEXT"):
        link_executable([data_entry])

    text = macho_obj.Section(
        sectname="__text",
        segname="__TEXT",
        data=struct.pack("<I", 0xD65F03C0),
        align_log2=2,
        flags=macho_obj.TEXT_SECTION_FLAGS,
        symbols=(macho_obj.TextSymbol("_main", 0),),
    )
    unsupported_import = macho_obj.emit_object([
        text,
        macho_obj.Section(
            sectname="__data",
            segname="__DATA",
            data=b"\0" * 8,
            relocations=(macho_obj.Relocation(
                0, "_puts", spec.ARM64_RELOC_POINTER_TO_GOT,
                pcrel=False, length=3,
            ),),
        ),
    ], undefined=("_puts",))
    with pytest.raises(LinkError, match="relocation type .* not applied"):
        link_executable([unsupported_import])


def _import_data_pointer_object(segname, sectname, addend):
    return macho_obj.emit_object([
        macho_obj.Section(
            sectname="__text",
            segname="__TEXT",
            data=struct.pack("<I", 0xD65F03C0),
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(macho_obj.TextSymbol("_main", 0),),
        ),
        macho_obj.Section(
            sectname=sectname,
            segname=segname,
            data=struct.pack("<q", addend),
            relocations=(macho_obj.Relocation(
                0, "_puts", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
    ], undefined=("_puts",))


def test_binds_an_import_in_a_data_pointer_and_fails_closed_otherwise():
    """50889ccd binds an import in a __DATA/__DATA_CONST pointer with a chained
    bind whose addend field holds 8 bits; anything else still refuses."""
    image = link_executable([_import_data_pointer_object("__DATA", "__data", 7)])
    assert b"_puts" in image
    with pytest.raises(LinkError, match=r"import '_puts' data pointer addend 256 does not fit a chained bind"):
        link_executable([_import_data_pointer_object("__DATA", "__data", 256)])
    with pytest.raises(LinkError, match=r"import '_puts' in a data pointer"):
        link_executable([_import_data_pointer_object("__TEXT", "__const", 0)])


def test_links_against_an_archive_pulling_only_what_is_needed(tmp_path):
    """The runtime is an archive; a binary must not carry all of it."""
    helper = _object(UNIT_HELPER)
    unused = _object(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".p2align 2\n.globl _never_called\n_never_called:\n"
        "  movz w0, #99\n  ret\n.subsections_via_symbols\n"
    )
    (tmp_path / "helper.o").write_bytes(helper)
    (tmp_path / "unused.o").write_bytes(unused)
    lib = tmp_path / "lib.a"
    ar = shutil.which("ar")
    assert ar is not None
    assert subprocess.run(
        [ar, "rcs", str(lib), str(tmp_path / "helper.o"),
         str(tmp_path / "unused.o")],
        capture_output=True, timeout=120,
    ).returncode == 0

    exe = tmp_path / "from_archive"
    exe.write_bytes(link_executable(
        [_object(UNIT_MAIN)], archives=[lib.read_bytes()]
    ))
    exe.chmod(0o755)
    run = _run([str(exe)])
    assert run.stdout == "yo\n", (run.stdout, run.stderr)
    assert run.returncode == 7

    obj = spec.parse_object(exe.read_bytes())
    names = {s["name"] for s in obj.symbols()}
    assert "_greet" in names
    assert "_never_called" not in names, (
        "an unreferenced archive member was linked in"
    )


def test_archive_selection_keeps_explicit_definitions_satisfied(tmp_path):
    """A pulled member may refer back to an explicit object's definition."""
    main = _object("""\
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _main
_main:
  paciasp
  stp x29, x30, [sp, #-16]!
  mov x29, sp
  bl _helper
  ldp x29, x30, [sp], #16
  autiasp
  ret
.globl _already
_already:
  movz w0, #42
  ret
.subsections_via_symbols
""")
    helper = _object("""\
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _helper
_helper:
  paciasp
  stp x29, x30, [sp, #-16]!
  mov x29, sp
  bl _already
  ldp x29, x30, [sp], #16
  autiasp
  ret
.subsections_via_symbols
""")
    duplicate = _object("""\
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _already
_already:
  movz w0, #99
  ret
.globl _decoy_marker
_decoy_marker:
  ret
.subsections_via_symbols
""")
    helper_path = tmp_path / "helper.o"
    duplicate_path = tmp_path / "duplicate.o"
    helper_path.write_bytes(helper)
    duplicate_path.write_bytes(duplicate)
    archive_path = tmp_path / "libhelpers.a"
    ar = shutil.which("ar")
    assert ar is not None
    made = _run([
        ar, "rcs", str(archive_path), str(helper_path), str(duplicate_path),
    ])
    assert made.returncode == 0, made.stderr

    exe = tmp_path / "archive_known_definition"
    exe.write_bytes(link_executable(
        [main], archives=[archive_path.read_bytes()],
    ))
    exe.chmod(0o755)
    run = _run([str(exe)])
    assert run.returncode == 42, (run.returncode, run.stderr)
    names = {symbol["name"] for symbol in spec.parse_object(exe.read_bytes()).symbols()}
    assert "_helper" in names
    assert "_decoy_marker" not in names


def test_unwind_metadata_is_dropped_not_refused(tmp_path):
    """A merged object with __LD,__compact_unwind must link, not raise.

    ld drops compact-unwind into a synthesized __unwind_info at executable
    link; pcc drops it (no synthesis). The only cost is unwinding through
    those frames, which is out of scope for this linker. But a live reference
    into a dropped section must still fail loudly.
    """
    from pcc.backend import macho_link
    from pcc.backend.macho_obj import (
        COMPACT_UNWIND_SECTION_FLAGS,
        Relocation,
        Section,
        TextSymbol,
    )

    # A leaf with a compact-unwind section that nothing live references.
    text = struct.pack("<II", 0x52800540, 0xD65F03C0)  # mov w0,#42 ; ret
    cu = b"\x00" * 32
    merged = macho_link.link_relocatable([
        macho_obj.emit_object([
            Section(sectname="__text", segname="__TEXT", data=text,
                    align_log2=2, flags=macho_obj.TEXT_SECTION_FLAGS,
                    symbols=(TextSymbol("_main", 0),)),
            Section(sectname="__compact_unwind", segname="__LD", data=cu,
                    align_log2=3, flags=COMPACT_UNWIND_SECTION_FLAGS,
                    relocations=(Relocation(
                        0, "_main", spec.ARM64_RELOC_UNSIGNED,
                        pcrel=False, length=3,
                    ),)),
        ]),
    ])
    exe = tmp_path / "with_unwind"
    exe.write_bytes(link_executable([merged]))
    exe.chmod(0o755)
    assert _run([str(exe)]).returncode == 42

    obj = spec.parse_object(exe.read_bytes())
    names = {s["sectname_str"] for s in obj.sections()}
    assert "__compact_unwind" not in names, "unwind section was not dropped"

    # Dropping metadata is safe only while no retained section points into it.
    # Debug sections deliberately contain no symbols, so model the live
    # reference with Mach-O's non-external section-target relocation to byte 8
    # of the compact-unwind row.  The executable linker must reject the link.
    referenced_unwind = macho_obj.emit_object([
        Section(
            sectname="__text",
            segname="__TEXT",
            data=struct.pack("<I", 0xD65F03C0),
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
        ),
        Section(
            sectname="__compact_unwind",
            segname="__LD",
            data=b"\0" * 32,
            align_log2=3,
            flags=COMPACT_UNWIND_SECTION_FLAGS,
            relocations=(Relocation(
                0, "_main", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
        Section(
            sectname="__data",
            segname="__DATA",
            data=b"\0" * 8,
            align_log2=3,
            flags=macho_obj.DATA_SECTION_FLAGS,
            relocations=(Relocation(
                0, "", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
                section=("__LD", "__compact_unwind"), target_offset=8,
            ),),
        ),
    ])
    with pytest.raises(LinkError, match="targets dropped unwind metadata"):
        link_executable([referenced_unwind])


def test_non_unwind_unknown_segment_still_fails_closed(tmp_path):
    """Dropping is only for unwind metadata; anything else still refuses."""
    from pcc.backend.macho_obj import Section, TextSymbol

    text = struct.pack("<II", 0x52800540, 0xD65F03C0)
    obj = macho_obj.emit_object([
        Section(sectname="__text", segname="__TEXT", data=text, align_log2=2,
                flags=macho_obj.TEXT_SECTION_FLAGS,
                symbols=(TextSymbol("_main", 0),)),
        Section(sectname="__weird", segname="__WEIRD", data=b"\x01\x02\x03\x04",
                align_log2=2, symbols=(TextSymbol("_w", 0),)),
    ])
    with pytest.raises(LinkError):
        link_executable([obj])


def test_absolute_pointer_in_text_fails_closed_instead_of_breaking_pie():
    from pcc.backend.macho_obj import Relocation, Section, TextSymbol

    obj = macho_obj.emit_object([
        Section(
            sectname="__text",
            segname="__TEXT",
            data=struct.pack("<I", 0xD65F03C0),
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
        ),
        Section(
            sectname="__const",
            segname="__TEXT",
            data=b"\0" * 8,
            align_log2=3,
            flags=spec.S_REGULAR,
            relocations=(Relocation(
                0, "_main", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
    ])
    with pytest.raises(LinkError, match="absolute pointer relocation in __TEXT"):
        link_executable([obj])


def test_chained_fixup_pointer_must_not_cross_a_page_boundary():
    from pcc.backend.macho_obj import Relocation, Section, TextSymbol

    obj = macho_obj.emit_object([
        Section(
            sectname="__text",
            segname="__TEXT",
            data=struct.pack("<I", 0xD65F03C0),
            align_log2=2,
            flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
        ),
        Section(
            sectname="__data",
            segname="__DATA",
            data=b"\0" * (0x4000 + 4),
            align_log2=2,
            flags=macho_obj.DATA_SECTION_FLAGS,
            symbols=(TextSymbol("_target", 0),),
            relocations=(Relocation(
                0x4000 - 4, "_target", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
    ])
    with pytest.raises(LinkError, match="crosses a 0x4000-byte page boundary"):
        link_executable([obj])


def test_got_load_of_a_defined_symbol_is_relaxed_and_runs(tmp_path):
    """A GOT load of an in-image symbol must relax to direct addressing.

    The runtime archive references thousands of defined symbols through the
    GOT (measured: 4703 of 4727). ld relaxes those `ldr [GOT]` sequences into
    `adrp`+`add` instead of keeping a GOT slot each; pcc must do the same, or
    every one needs a rebase chained-fixup. This exercises the relaxation on
    a symbol pcc's own emitter puts behind the GOT.
    """
    # _reader loads _shared through the GOT (adrp @GOTPAGE / ldr @GOTPAGEOFF),
    # then reads it. _shared is defined in the same link, so the GOT load
    # must relax to adrp/add and read the value directly.
    unit = """\
.section __DATA,__data
.p2align 3
.globl _shared
_shared:
  .quad 777
.section __TEXT,__text,regular,pure_instructions
.p2align 2
.globl _main
_main:
  adrp x9, _shared@GOTPAGE
  ldr x9, [x9, _shared@GOTPAGEOFF]
  ldr x0, [x9]
  ret
.subsections_via_symbols
"""
    exe = tmp_path / "relaxed"
    exe.write_bytes(link_executable([_object(unit)]))
    exe.chmod(0o755)
    run = _run([str(exe)])
    assert run.returncode == (777 & 0xFF), (run.returncode, run.stderr)

    # No GOT slot was created for the defined symbol: with no imports either,
    # there should be no __got section at all.
    obj = spec.parse_object(exe.read_bytes())
    got = next(
        (s for s in obj.sections() if s["sectname_str"] == "__got"), None
    )
    assert got is None or got["size"] == 0, (
        "a GOT slot was emitted for a defined symbol that should have relaxed"
    )


def test_thread_local_variable_links_and_runs(tmp_path):
    """A _Thread_local, linked by pcc, resolved through the TLV descriptor.

    Built from a cc-compiled object (pcc's emitter path for TLS goes through
    the same relocations). The descriptor thunk binds __tlv_bootstrap via a
    chained fixup in __DATA, the TLVP relocations relax to the descriptor
    address, MH_HAS_TLV_DESCRIPTORS is set, and dyld resolves the access.
    """
    from pcc.backend.macho_link import link_relocatable

    cc = shutil.which(os.environ.get("CC", "cc"))
    if cc is None:
        raise AssertionError("cc required")
    src = tmp_path / "tls.c"
    src.write_text(
        "_Thread_local int counter = 5;\n"
        "int get_counter(void){ counter += 37; return counter; }\n",
        encoding="utf-8",
    )
    obj1 = tmp_path / "tls.o"
    assert subprocess.run(
        [cc, "-c", "-O1", str(src), "-o", str(obj1)],
        capture_output=True, timeout=120,
    ).returncode == 0
    main_c = tmp_path / "m.c"
    main_c.write_text(
        "extern int get_counter(void);\nint main(void){ return get_counter(); }\n",
        encoding="utf-8",
    )
    obj2 = tmp_path / "m.o"
    assert subprocess.run(
        [cc, "-c", "-O1", str(main_c), "-o", str(obj2)],
        capture_output=True, timeout=120,
    ).returncode == 0

    merged = link_relocatable([obj1.read_bytes(), obj2.read_bytes()])
    exe = tmp_path / "tls_exe"
    exe.write_bytes(link_executable([merged]))
    exe.chmod(0o755)

    # counter starts 5, += 37 -> 42
    assert _run([str(exe)]).returncode == 42

    obj = spec.parse_object(exe.read_bytes())
    assert obj.header["flags"] & 0x00800000, "MH_HAS_TLV_DESCRIPTORS not set"
    assert any(s["sectname_str"] == "__thread_vars" for s in obj.sections())


def test_thread_local_zerofill_precedes_large_ordinary_bss(tmp_path):
    """The compact TLS template must not be separated by ordinary BSS.

    TLV descriptor offsets are relative to one compact block containing
    ``__thread_data`` followed by ``__thread_bss``.  If executable layout
    leaves ordinary ``__bss``/``__common`` between those sections, dyld uses
    unrelated file or LINKEDIT bytes as the zero-initialized TLS values.
    Exercise two independent TLS slots around a large ordinary BSS tail so
    exception/root-handle-style adjacent slots are both proven zero.
    """
    from pcc.backend.macho_link import link_relocatable

    cc = shutil.which(os.environ.get("CC", "cc"))
    assert cc is not None
    src = tmp_path / "tls_bss.c"
    src.write_text(
        "volatile _Thread_local int tls_init = 7;\n"
        "volatile _Thread_local void *tls_zero_a;\n"
        "static volatile unsigned char ordinary_bss[0x20000];\n"
        "volatile _Thread_local void *tls_zero_b;\n"
        "int main(void) {\n"
        "  return tls_init != 7 || tls_zero_a != 0 || tls_zero_b != 0\n"
        "      || ordinary_bss[0x4000] != 0;\n"
        "}\n",
        encoding="utf-8",
    )
    compiled = tmp_path / "tls_bss.o"
    result = subprocess.run(
        [cc, "-c", "-O1", str(src), "-o", str(compiled)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr

    merged = link_relocatable([compiled.read_bytes()])
    exe = tmp_path / "tls_bss_exe"
    exe.write_bytes(link_executable([merged]))
    exe.chmod(0o755)
    run = _run([str(exe)])
    assert run.returncode == 0, (run.returncode, run.stderr)

    parsed = spec.parse_object(exe.read_bytes())
    names = [section["sectname_str"] for section in parsed.sections()]
    assert names.index("__thread_data") < names.index("__thread_bss")
    assert names.index("__thread_bss") < names.index("__bss")


def test_bss_global_reads_zero_not_file_garbage(tmp_path):
    """A __bss (zerofill) global must be zero at runtime, not spliced file bytes.

    macho_exec once read a zerofill section's "data" from file offset 0 —
    which is the mach header + __text — and wrote it into __bss, so a global
    that should be 0 held instruction bytes. A real program that branches on
    such a global (a lazily-initialized list head) then jumped through
    garbage. This links a program whose behavior depends on a __bss int being
    zero.
    """
    from pcc.backend.macho_obj import (
        Relocation,
        Section,
        TextSymbol,
        TEXT_SECTION_FLAGS,
        ZEROFILL_SECTION_FLAGS,
    )

    text = struct.pack(
        "<4I", 0x90000009, 0x91000129, 0xF9400120, 0xD65F03C0
    )  # adrp x9,_flag@PAGE ; add x9,x9,#off ; ldr x0,[x9] ; ret
    obj = macho_obj.emit_object([
        Section(sectname="__text", segname="__TEXT", data=text, align_log2=2,
                flags=TEXT_SECTION_FLAGS,
                symbols=(TextSymbol("_main", 0),),
                relocations=(
                    Relocation(0, "_flag", spec.ARM64_RELOC_PAGE21, pcrel=True),
                    Relocation(4, "_flag", spec.ARM64_RELOC_PAGEOFF12, pcrel=False),
                )),
        Section(sectname="__bss", segname="__DATA",
                flags=ZEROFILL_SECTION_FLAGS, align_log2=3,
                symbols=(TextSymbol("_flag", 0),), zerofill_size=8),
    ])
    exe = tmp_path / "bss_exe"
    exe.write_bytes(link_executable([obj]))
    exe.chmod(0o755)
    # _flag is zero -> main returns 0. If __bss held garbage, non-zero/crash.
    assert _run([str(exe)]).returncode == 0

    parsed = spec.parse_object(exe.read_bytes())
    bss = next(s for s in parsed.sections() if s["sectname_str"] == "__bss")
    assert bss["offset"] == 0, "zerofill __bss must have file offset 0"


def test_large_mixed_data_and_bss_do_not_alias_linkedit_pages(tmp_path):
    """LINKEDIT must follow BSS in VM space without storing BSS in the file.

    A large production image exposed a layout bug: __DATA had both file-backed
    content and more than one page of zerofill, and LINKEDIT's *VM address* was
    once derived from its compact file offset.  That overlapped __DATA's BSS.
    The file offset itself should remain compact, as ld does.  Keep the
    regression small but force the same multi-page shape, include a data
    pointer rebase (so __DATA has a chained-fixup starts table), and execute
    reads from both the start and the end of zerofill.  Reading only the tail
    misses the bug: compact LINKEDIT aliases the first virtual-only pages.
    """
    from pcc.backend.macho_obj import (
        DATA_SECTION_FLAGS,
        Relocation,
        Section,
        TextSymbol,
        TEXT_SECTION_FLAGS,
        ZEROFILL_SECTION_FLAGS,
    )

    text = struct.pack(
        "<8I",
        0x90000009, 0x91000129, 0xB9400120,  # read _probe
        0x9000000A, 0x9100014A, 0xB940014A,  # read _tail
        0x0B0A0000,                          # add w0, w0, w10
        0xD65F03C0,
    )
    obj = macho_obj.emit_object([
        Section(
            sectname="__text", segname="__TEXT", data=text, align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
            relocations=(
                Relocation(
                    0, "_probe", spec.ARM64_RELOC_PAGE21, pcrel=True,
                ),
                Relocation(
                    4, "_probe", spec.ARM64_RELOC_PAGEOFF12, pcrel=False,
                ),
                Relocation(
                    12, "_tail", spec.ARM64_RELOC_PAGE21, pcrel=True,
                ),
                Relocation(
                    16, "_tail", spec.ARM64_RELOC_PAGEOFF12, pcrel=False,
                ),
            ),
        ),
        Section(
            sectname="__data", segname="__DATA", data=b"\0" * 8,
            flags=DATA_SECTION_FLAGS,
            relocations=(Relocation(
                0, "_probe", spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
        Section(
            sectname="__bss", segname="__DATA",
            flags=ZEROFILL_SECTION_FLAGS, align_log2=3,
            symbols=(TextSymbol("_probe", 4), TextSymbol("_tail", 0x8FFC)),
            zerofill_size=0x9000,
        ),
    ])
    exe = tmp_path / "large_mixed_bss"
    exe.write_bytes(link_executable([obj]))
    exe.chmod(0o755)
    assert _run([str(exe)]).returncode == 0

    parsed = spec.parse_object(exe.read_bytes())
    data_segment = next(
        segment.body for segment in _segments(parsed)
        if segment.body["segname_str"] == "__DATA"
    )
    linkedit = next(
        segment.body for segment in _segments(parsed)
        if segment.body["segname_str"] == "__LINKEDIT"
    )
    # The file payload ends at the last file-backed byte (the bss is VM-only);
    # LINKEDIT's file offset is the page-aligned end right after it, and its
    # VM address follows the bss.  The __DATA filesize must not extend into
    # the bss: backing those addresses with file bytes made the globals read
    # garbage at startup instead of the dyld zero-fill.
    data_content_end = data_segment["fileoff"] + data_segment["filesize"]
    data_vm_end = data_segment["vmaddr"] + data_segment["vmsize"]
    assert data_content_end <= linkedit["fileoff"]
    assert linkedit["vmaddr"] == data_vm_end
    assert linkedit["fileoff"] < data_segment["fileoff"] + data_segment["vmsize"]
    assert linkedit["fileoff"] % 0x4000 == linkedit["vmaddr"] % 0x4000
    assert len(exe.read_bytes()) < data_segment["fileoff"] + data_segment["vmsize"]

    fixup_command = parsed.command(spec.LC_DYLD_CHAINED_FIXUPS)
    assert fixup_command is not None
    _cmd, _cmdsize, fixup_off, fixup_size = struct.unpack(
        "<IIII", fixup_command.raw,
    )
    fixups = exe.read_bytes()[fixup_off:fixup_off + fixup_size]
    _version, starts_off = struct.unpack_from("<2I", fixups)
    segments = _segments(parsed)
    segment_count, = struct.unpack_from("<I", fixups, starts_off)
    starts = struct.unpack_from(
        f"<{segment_count}I", fixups, starts_off + 4,
    )
    data_index = next(
        index for index, segment in enumerate(segments)
        if segment.body["segname_str"] == "__DATA"
    )
    info_at = starts_off + starts[data_index]
    assert starts[data_index] != 0
    _size, _page_size, _pointer_format, _segment_offset, _max_valid, page_count = (
        struct.unpack_from("<IHHQIH", fixups, info_at)
    )
    # The fixup starts table covers the page-aligned file span (the segment
    # payload may be shorter than a page); the __DATA filesize is the actual
    # content length, so round up.
    assert page_count == (data_segment["filesize"] + 0x3FFF) // 0x4000

    # The platform linker uses the same split invariant: LINKEDIT starts after
    # file-backed DATA in the file and after all of DATA (including BSS) in VM
    # space.  It does not materialize the BSS merely to make the two offsets
    # numerically equal.
    oracle_c = tmp_path / "oracle_bss.c"
    oracle_c.write_text(
        "volatile int live_data = 1;\n"
        "volatile unsigned char virtual_tail[0x9000];\n"
        "int main(void) { return virtual_tail[0x8fff] + live_data - 1; }\n",
        encoding="utf-8",
    )
    oracle_exe = tmp_path / "oracle_bss"
    linked = _run([_CC, str(oracle_c), "-o", str(oracle_exe)])
    assert linked.returncode == 0, linked.stderr
    oracle = spec.parse_object(oracle_exe.read_bytes())
    oracle_data = next(
        segment.body for segment in _segments(oracle)
        if segment.body["segname_str"] == "__DATA"
    )
    oracle_linkedit = next(
        segment.body for segment in _segments(oracle)
        if segment.body["segname_str"] == "__LINKEDIT"
    )
    assert oracle_linkedit["fileoff"] == (
        oracle_data["fileoff"] + oracle_data["filesize"]
    )
    assert oracle_linkedit["vmaddr"] == (
        oracle_data["vmaddr"] + oracle_data["vmsize"]
    )
    assert oracle_linkedit["fileoff"] % 0x4000 == (
        oracle_linkedit["vmaddr"] % 0x4000
    )


def test_pageoff12_ldr_scales_the_immediate(tmp_path):
    """A global read via `ldr [x, off@PAGEOFF12]` must resolve to the same
    address it is written to via `add x, off@PAGEOFF12`.

    ARM64_RELOC_PAGEOFF12 encodes differently per instruction: `add` takes the
    byte offset, but a load/store takes the byte offset divided by the access
    size. Writing the raw byte offset into an `ldr` put the target at 8x its
    address — the runtime read its GC-index base from the wrong global and got
    zero, crashing far from the mistake. This roundtrips a value through a
    global using both instruction forms.
    """
    from pcc.backend.macho_obj import (
        DATA_SECTION_FLAGS,
        Relocation,
        Section,
        TextSymbol,
        TEXT_SECTION_FLAGS,
    )

    # _main:
    #   adrp x9, _slot@PAGE ; add x9, x9, _slot@PAGEOFF   (compute &_slot)
    #   movz w10, #7 ; str x10, [x9]                       (write 7 via the add-addr)
    #   adrp x11, _slot@PAGE ; ldr x0, [x11, _slot@PAGEOFF] (read via ldr PAGEOFF12)
    #   ret
    text = struct.pack(
        "<9I",
        0x90000009,  # adrp x9, _slot@PAGE
        0x91000129,  # add  x9, x9, #off      (PAGEOFF12 add)
        0x528000EA,  # movz w10, #7
        0xF900012A,  # str  x10, [x9]
        0x9000000B,  # adrp x11, _slot@PAGE
        0xF9400160,  # ldr  x0, [x11, #off]   (PAGEOFF12 ldr — must scale)
        0xD65F03C0,  # ret
        0xD503201F,  # nop (pad)
        0xD503201F,
    )
    obj = macho_obj.emit_object([
        Section(sectname="__text", segname="__TEXT", data=text, align_log2=2,
                flags=TEXT_SECTION_FLAGS,
                symbols=(TextSymbol("_main", 0),),
                relocations=(
                    Relocation(0, "_slot", spec.ARM64_RELOC_PAGE21, pcrel=True),
                    Relocation(4, "_slot", spec.ARM64_RELOC_PAGEOFF12, pcrel=False),
                    Relocation(16, "_slot", spec.ARM64_RELOC_PAGE21, pcrel=True),
                    Relocation(20, "_slot", spec.ARM64_RELOC_PAGEOFF12, pcrel=False),
                )),
        Section(sectname="__data", segname="__DATA",
                data=b"\x00" * 16, align_log2=3, flags=DATA_SECTION_FLAGS,
                symbols=(TextSymbol("_slot", 0),)),
    ])
    exe = tmp_path / "pageoff_exe"
    exe.write_bytes(link_executable([obj]))
    exe.chmod(0o755)
    # store 7 through the add-address, read back through the ldr-address:
    # if the ldr immediate is unscaled, it reads a different global -> not 7.
    assert _run([str(exe)]).returncode == 7


def test_pageoff12_ldr_q_uses_sixteen_byte_scale(tmp_path):
    """A Q-register literal load uses a 16-byte scaled imm12.

    AArch64 encodes this width as size=0 plus the SIMD/FP opc high bit.  Reading
    only size made the final linker patch the byte offset as if this were an
    8-bit load; the processor then multiplied that immediate by 16 and loaded
    unrelated globals instead of the literal pool.
    """
    from pcc.backend.macho_obj import (
        DATA_SECTION_FLAGS,
        Relocation,
        Section,
        TextSymbol,
        TEXT_SECTION_FLAGS,
    )

    # _main:
    #   adrp x8, _vec@PAGE
    #   ldr  q0, [x8, _vec@PAGEOFF]
    #   fmov x0, d0
    #   ret
    # The lower 64-bit lane is returned as the process status.
    text = struct.pack(
        "<4I",
        0x90000008,
        0x3DC00100,
        0x9E660000,
        0xD65F03C0,
    )
    obj = macho_obj.emit_object([
        Section(
            sectname="__text",
            segname="__TEXT",
            data=text,
            align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
            relocations=(
                Relocation(
                    0, "_vec", spec.ARM64_RELOC_PAGE21, pcrel=True,
                ),
                Relocation(
                    4, "_vec", spec.ARM64_RELOC_PAGEOFF12, pcrel=False,
                ),
            ),
        ),
        Section(
            sectname="__literal16",
            segname="__TEXT",
            data=struct.pack("<QQ", 7, 11),
            align_log2=4,
            flags=DATA_SECTION_FLAGS,
            symbols=(TextSymbol("_vec", 0),),
        ),
    ])
    exe = tmp_path / "pageoff_q_exe"
    exe.write_bytes(link_executable([obj]))
    exe.chmod(0o755)
    assert _run([str(exe)]).returncode == 7
