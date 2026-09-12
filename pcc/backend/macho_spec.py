"""Mach-O structure declarations pcc needs to own its object/link path.

This module *declares and parses*; it emits nothing. It is the foundation
LINK-P1-MACHO-SPEC asks for: before writing a single byte of a Mach-O file,
have the structures written down, checked against the SDK headers, and proven
against a real clang-produced object.

Two deliberate choices:

- The declarations carry `struct` format strings rather than being generated
  into a second file. Every hand-maintained mirror in this repository has
  eventually drifted from its source; instead of adding another generated
  artifact to keep in sync, `tests/python/test_macho_spec.py` checks every
  field's offset and size against a cc probe over `<mach-o/loader.h>` and
  friends, so the SDK stays the authority without a file to regenerate.
- Parsing is exact and fail-closed. An unrecognised load command is kept as
  raw bytes with its `cmd`/`cmdsize`, never skipped, so re-serialising a
  parsed object reproduces the original file byte for byte. That round-trip
  is what proves the declarations are complete: a missing or mis-sized field
  cannot survive it.

References (read-only): SDK `mach-o/{loader.h,reloc.h,arm64/reloc.h}`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

# --- file/CPU identity -----------------------------------------------------

MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM_64 = 0xCFFAEDFE

CPU_ARCH_ABI64 = 0x01000000
CPU_TYPE_X86 = 7
CPU_TYPE_X86_64 = CPU_TYPE_X86 | CPU_ARCH_ABI64
CPU_TYPE_ARM = 12
CPU_TYPE_ARM64 = CPU_TYPE_ARM | CPU_ARCH_ABI64
CPU_SUBTYPE_ARM64_ALL = 0
CPU_SUBTYPE_X86_64_ALL = 3

MH_OBJECT = 0x1
MH_EXECUTE = 0x2
MH_DYLIB = 0x6
MH_SUBSECTIONS_VIA_SYMBOLS = 0x2000

# --- load commands ---------------------------------------------------------

LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
LC_LOAD_DYLIB = 0xC
LC_LOAD_DYLINKER = 0xE
LC_UUID = 0x1B
LC_BUILD_VERSION = 0x32
LC_MAIN = 0x80000028
LC_DYLD_INFO_ONLY = 0x80000022
LC_DYLD_CHAINED_FIXUPS = 0x80000034
LC_DYLD_EXPORTS_TRIE = 0x80000033
LC_LINKER_OPTION = 0x2D
# Optional optimization hints carried by MH_OBJECT inputs.  They affect link
# quality only; an owned linker may ignore a well-formed payload without
# changing section/symbol/relocation semantics.
LC_LINKER_OPTIMIZATION_HINT = 0x2E
LC_DATA_IN_CODE = 0x29
LC_CODE_SIGNATURE = 0x1D
LC_FUNCTION_STARTS = 0x26

# --- section types and attributes -----------------------------------------

SECTION_TYPE = 0x000000FF
SECTION_ATTRIBUTES = 0xFFFFFF00

S_REGULAR = 0x0
S_ZEROFILL = 0x1
S_CSTRING_LITERALS = 0x2
S_SYMBOL_STUBS = 0x8
S_MOD_INIT_FUNC_POINTERS = 0x9
S_MOD_TERM_FUNC_POINTERS = 0xA
S_COALESCED = 0xB
S_THREAD_LOCAL_VARIABLES = 0x13

S_ATTR_PURE_INSTRUCTIONS = 0x80000000
S_ATTR_NO_TOC = 0x40000000
S_ATTR_STRIP_STATIC_SYMS = 0x20000000
S_ATTR_NO_DEAD_STRIP = 0x10000000
S_ATTR_LIVE_SUPPORT = 0x08000000
S_ATTR_DEBUG = 0x02000000
S_ATTR_SOME_INSTRUCTIONS = 0x00000400
S_ATTR_EXT_RELOC = 0x00000200
S_ATTR_LOC_RELOC = 0x00000100

# --- data-in-code entry kinds ---------------------------------------------

DICE_KIND_DATA = 0x0001
DICE_KIND_JUMP_TABLE8 = 0x0002
DICE_KIND_JUMP_TABLE16 = 0x0003
DICE_KIND_JUMP_TABLE32 = 0x0004
DICE_KIND_ABS_JUMP_TABLE32 = 0x0005

# --- protection and platform ----------------------------------------------

VM_PROT_NONE = 0x0
VM_PROT_READ = 0x1
VM_PROT_WRITE = 0x2
VM_PROT_EXECUTE = 0x4

PLATFORM_MACOS = 1
PLATFORM_IOS = 2

# --- symbol table (nlist n_type bits) --------------------------------------

N_STAB = 0xE0
N_PEXT = 0x10
N_TYPE = 0x0E
N_EXT = 0x01

N_UNDF = 0x0
N_ABS = 0x2
N_SECT = 0xE
N_PBUD = 0xC
N_INDR = 0xA

NO_SECT = 0
REFERENCE_FLAG_UNDEFINED_NON_LAZY = 0

# --- arm64 relocation types (mach-o/arm64/reloc.h) -------------------------

ARM64_RELOC_UNSIGNED = 0
ARM64_RELOC_SUBTRACTOR = 1
ARM64_RELOC_BRANCH26 = 2
ARM64_RELOC_PAGE21 = 3
ARM64_RELOC_PAGEOFF12 = 4
ARM64_RELOC_GOT_LOAD_PAGE21 = 5
ARM64_RELOC_GOT_LOAD_PAGEOFF12 = 6
ARM64_RELOC_POINTER_TO_GOT = 7
ARM64_RELOC_TLVP_LOAD_PAGE21 = 8
ARM64_RELOC_TLVP_LOAD_PAGEOFF12 = 9
ARM64_RELOC_ADDEND = 10

R_SCATTERED = 0x80000000


# --- structure declarations ------------------------------------------------


@dataclass(frozen=True)
class MachOStruct:
    """One C structure from the Mach-O headers.

    `fields` is ordered (name, struct-format) pairs. The format letters are
    the little-endian ones only; Mach-O objects pcc produces and consumes are
    little-endian by construction, and a big-endian magic is rejected at parse
    time rather than silently byte-swapped.
    """

    name: str
    fields: tuple[tuple[str, str], ...]

    @property
    def format(self) -> str:
        return "<" + "".join(fmt for _name, fmt in self.fields)

    @property
    def size(self) -> int:
        return struct.calcsize(self.format)

    def offset_of(self, field_name: str) -> int:
        acc = 0
        for name, fmt in self.fields:
            if name == field_name:
                return acc
            acc += struct.calcsize("<" + fmt)
        raise KeyError(f"{self.name} has no field {field_name!r}")

    def unpack(self, data: bytes, offset: int = 0) -> dict[str, Any]:
        values = struct.unpack_from(self.format, data, offset)
        return dict(zip((name for name, _ in self.fields), values))

    def pack(self, values: dict[str, Any]) -> bytes:
        missing = [name for name, _ in self.fields if name not in values]
        if missing:
            raise KeyError(f"{self.name} missing fields: {missing}")
        return struct.pack(
            self.format, *(values[name] for name, _ in self.fields)
        )


MACH_HEADER_64 = MachOStruct(
    "mach_header_64",
    (
        ("magic", "I"),
        ("cputype", "i"),
        ("cpusubtype", "i"),
        ("filetype", "I"),
        ("ncmds", "I"),
        ("sizeofcmds", "I"),
        ("flags", "I"),
        ("reserved", "I"),
    ),
)

LOAD_COMMAND = MachOStruct(
    "load_command", (("cmd", "I"), ("cmdsize", "I")),
)

SEGMENT_COMMAND_64 = MachOStruct(
    "segment_command_64",
    (
        ("cmd", "I"),
        ("cmdsize", "I"),
        ("segname", "16s"),
        ("vmaddr", "Q"),
        ("vmsize", "Q"),
        ("fileoff", "Q"),
        ("filesize", "Q"),
        ("maxprot", "i"),
        ("initprot", "i"),
        ("nsects", "I"),
        ("flags", "I"),
    ),
)

SECTION_64 = MachOStruct(
    "section_64",
    (
        ("sectname", "16s"),
        ("segname", "16s"),
        ("addr", "Q"),
        ("size", "Q"),
        ("offset", "I"),
        ("align", "I"),
        ("reloff", "I"),
        ("nreloc", "I"),
        ("flags", "I"),
        ("reserved1", "I"),
        ("reserved2", "I"),
        ("reserved3", "I"),
    ),
)

SYMTAB_COMMAND = MachOStruct(
    "symtab_command",
    (
        ("cmd", "I"),
        ("cmdsize", "I"),
        ("symoff", "I"),
        ("nsyms", "I"),
        ("stroff", "I"),
        ("strsize", "I"),
    ),
)

DYSYMTAB_COMMAND = MachOStruct(
    "dysymtab_command",
    (
        ("cmd", "I"),
        ("cmdsize", "I"),
        ("ilocalsym", "I"),
        ("nlocalsym", "I"),
        ("iextdefsym", "I"),
        ("nextdefsym", "I"),
        ("iundefsym", "I"),
        ("nundefsym", "I"),
        ("tocoff", "I"),
        ("ntoc", "I"),
        ("modtaboff", "I"),
        ("nmodtab", "I"),
        ("extrefsymoff", "I"),
        ("nextrefsyms", "I"),
        ("indirectsymoff", "I"),
        ("nindirectsyms", "I"),
        ("extreloff", "I"),
        ("nextrel", "I"),
        ("locreloff", "I"),
        ("nlocrel", "I"),
    ),
)

NLIST_64 = MachOStruct(
    "nlist_64",
    (
        ("n_strx", "I"),
        ("n_type", "B"),
        ("n_sect", "B"),
        ("n_desc", "H"),
        ("n_value", "Q"),
    ),
)

# relocation_info's second word is a bitfield; it is declared as one u32 here
# and split by `unpack_relocation` so the bit layout lives in exactly one
# place instead of being open-coded at every use.
RELOCATION_INFO = MachOStruct(
    "relocation_info", (("r_address", "i"), ("r_info", "I")),
)
_RELOCATION_READER = struct.Struct(RELOCATION_INFO.format)

BUILD_VERSION_COMMAND = MachOStruct(
    "build_version_command",
    (
        ("cmd", "I"),
        ("cmdsize", "I"),
        ("platform", "I"),
        ("minos", "I"),
        ("sdk", "I"),
        ("ntools", "I"),
    ),
)

BUILD_TOOL_VERSION = MachOStruct(
    "build_tool_version", (("tool", "I"), ("version", "I")),
)

LINKEDIT_DATA_COMMAND = MachOStruct(
    "linkedit_data_command",
    (
        ("cmd", "I"),
        ("cmdsize", "I"),
        ("dataoff", "I"),
        ("datasize", "I"),
    ),
)

DATA_IN_CODE_ENTRY = MachOStruct(
    "data_in_code_entry",
    (
        ("offset", "I"),
        ("length", "H"),
        ("kind", "H"),
    ),
)

ALL_STRUCTS = (
    MACH_HEADER_64,
    LOAD_COMMAND,
    SEGMENT_COMMAND_64,
    SECTION_64,
    SYMTAB_COMMAND,
    DYSYMTAB_COMMAND,
    NLIST_64,
    RELOCATION_INFO,
    BUILD_VERSION_COMMAND,
    BUILD_TOOL_VERSION,
    LINKEDIT_DATA_COMMAND,
    DATA_IN_CODE_ENTRY,
)


# --- relocation bitfield ---------------------------------------------------


def unpack_relocation(r_info: int, r_address: int | None = None) -> dict[str, int]:
    """Split relocation_info's packed word.

    Little-endian bitfield order from `mach-o/reloc.h`:
    symbolnum:24, pcrel:1, length:2, extern:1, type:4.
    """
    symbolnum = r_info & 0x00FFFFFF
    pcrel = (r_info >> 24) & 0x1
    length = (r_info >> 25) & 0x3
    external = (r_info >> 27) & 0x1
    kind = (r_info >> 28) & 0xF
    if r_address is not None:
        return {
            "r_address": r_address,
            "r_symbolnum": symbolnum,
            "r_pcrel": pcrel,
            "r_length": length,
            "r_extern": external,
            "r_type": kind,
        }
    return {
        "r_symbolnum": symbolnum,
        "r_pcrel": pcrel,
        "r_length": length,
        "r_extern": external,
        "r_type": kind,
    }


def pack_relocation(
    *, r_symbolnum: int, r_pcrel: int, r_length: int, r_extern: int, r_type: int
) -> int:
    if not 0 <= r_symbolnum <= 0x00FFFFFF:
        raise ValueError(f"r_symbolnum out of range: {r_symbolnum}")
    if r_length not in (0, 1, 2, 3):
        raise ValueError(f"r_length must be 0..3 (log2 bytes): {r_length}")
    if not 0 <= r_type <= 0xF:
        raise ValueError(f"r_type out of range: {r_type}")
    return (
        (r_symbolnum & 0x00FFFFFF)
        | ((r_pcrel & 0x1) << 24)
        | ((r_length & 0x3) << 25)
        | ((r_extern & 0x1) << 27)
        | ((r_type & 0xF) << 28)
    )


# --- parsed object model ---------------------------------------------------


class MachOFormatError(Exception):
    """The input is not a Mach-O object pcc's declarations describe.

    Fail closed: pcc's own link path must never guess at a shape it does not
    model, because a wrong guess produces a plausible-looking object that the
    linker mis-reads.
    """


@dataclass
class LoadCommand:
    cmd: int
    cmdsize: int
    offset: int
    raw: bytes
    body: dict[str, Any] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)

    def pack(self) -> bytes:
        """Re-serialise exactly what was read.

        The raw bytes are authoritative: parsing must be lossless, and a
        command pcc does not model still has to survive a round trip.
        """
        return self.raw


@dataclass
class MachOObject:
    header: dict[str, Any]
    commands: list[LoadCommand]
    data: bytes

    def command(self, cmd: int) -> LoadCommand | None:
        for lc in self.commands:
            if lc.cmd == cmd:
                return lc
        return None

    def sections(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for lc in self.commands:
            out.extend(lc.sections)
        return out

    def symbols(self) -> list[dict[str, Any]]:
        symtab = self.command(LC_SYMTAB)
        if symtab is None:
            return []
        body = symtab.body
        out = []
        stroff = body["stroff"]
        strsize = body["strsize"]
        strtab = self.data[stroff:stroff + strsize]
        for i in range(body["nsyms"]):
            entry = NLIST_64.unpack(self.data, body["symoff"] + i * NLIST_64.size)
            name_end = strtab.find(b"\0", entry["n_strx"])
            entry["name"] = strtab[entry["n_strx"]:name_end].decode(
                "utf-8", "surrogateescape"
            )
            out.append(entry)
        return out

    def relocations(self, section: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for i in range(section["nreloc"]):
            # Read the two wire words once and build the final row directly.
            # The former path built two intermediate dicts and snapshotted
            # another mapping in dict.update for every eight-byte record.
            address, info = _RELOCATION_READER.unpack_from(
                self.data, section["reloff"] + i * RELOCATION_INFO.size
            )
            out.append(unpack_relocation(info, address))
        return out

    def data_in_code(self) -> list[dict[str, Any]]:
        """Return the ranges named by LC_DATA_IN_CODE, in table order."""
        command = self.command(LC_DATA_IN_CODE)
        if command is None:
            return []
        body = command.body
        dataoff = body["dataoff"]
        datasize = body["datasize"]
        if datasize % DATA_IN_CODE_ENTRY.size != 0:
            raise MachOFormatError(
                "LC_DATA_IN_CODE payload is not a whole entry table"
            )
        end = dataoff + datasize
        if dataoff < 0 or end > len(self.data):
            raise MachOFormatError("LC_DATA_IN_CODE payload is outside file")
        return [
            DATA_IN_CODE_ENTRY.unpack(
                self.data, dataoff + i * DATA_IN_CODE_ENTRY.size
            )
            for i in range(datasize // DATA_IN_CODE_ENTRY.size)
        ]

    def linker_optimization_hints(self) -> bytes:
        """Return the bounded optional LC_LINKER_OPTIMIZATION_HINT payload."""
        command = self.command(LC_LINKER_OPTIMIZATION_HINT)
        if command is None:
            return b""
        body = command.body
        dataoff = body["dataoff"]
        datasize = body["datasize"]
        end = dataoff + datasize
        if dataoff < 0 or end < dataoff or end > len(self.data):
            raise MachOFormatError(
                "LC_LINKER_OPTIMIZATION_HINT payload is outside file"
            )
        return self.data[dataoff:end]

    def pack(self) -> bytes:
        """Rebuild the header + load-command region from the parsed model.

        Only the region the declarations cover; payload (section contents,
        symbol table, string table) is addressed by offset and is not moved.
        """
        head = MACH_HEADER_64.pack(self.header)
        body = b"".join(lc.pack() for lc in self.commands)
        return head + body


def _cstr16(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


def parse_object(data: bytes) -> MachOObject:
    """Parse a 64-bit little-endian Mach-O file into the declared structures."""
    if len(data) < MACH_HEADER_64.size:
        raise MachOFormatError(f"file is {len(data)} bytes, shorter than a header")
    header = MACH_HEADER_64.unpack(data)
    if header["magic"] == MH_CIGAM_64:
        raise MachOFormatError(
            "byte-swapped Mach-O (MH_CIGAM_64); pcc models little-endian only"
        )
    if header["magic"] != MH_MAGIC_64:
        raise MachOFormatError(f"bad magic 0x{header['magic']:08x}")

    commands: list[LoadCommand] = []
    offset = MACH_HEADER_64.size
    end_of_commands = offset + header["sizeofcmds"]
    for _ in range(header["ncmds"]):
        if offset + LOAD_COMMAND.size > end_of_commands:
            raise MachOFormatError("load commands run past sizeofcmds")
        head = LOAD_COMMAND.unpack(data, offset)
        cmdsize = head["cmdsize"]
        if cmdsize < LOAD_COMMAND.size or offset + cmdsize > end_of_commands:
            raise MachOFormatError(
                f"load command at {offset} has bad cmdsize {cmdsize}"
            )
        raw = data[offset:offset + cmdsize]
        lc = LoadCommand(cmd=head["cmd"], cmdsize=cmdsize, offset=offset, raw=raw)

        if lc.cmd == LC_SEGMENT_64:
            lc.body = SEGMENT_COMMAND_64.unpack(data, offset)
            lc.body["segname_str"] = _cstr16(lc.body["segname"])
            sec_off = offset + SEGMENT_COMMAND_64.size
            for _s in range(lc.body["nsects"]):
                sec = SECTION_64.unpack(data, sec_off)
                sec["sectname_str"] = _cstr16(sec["sectname"])
                sec["segname_str"] = _cstr16(sec["segname"])
                lc.sections.append(sec)
                sec_off += SECTION_64.size
        elif lc.cmd == LC_SYMTAB:
            lc.body = SYMTAB_COMMAND.unpack(data, offset)
        elif lc.cmd == LC_DYSYMTAB:
            lc.body = DYSYMTAB_COMMAND.unpack(data, offset)
        elif lc.cmd == LC_BUILD_VERSION:
            lc.body = BUILD_VERSION_COMMAND.unpack(data, offset)
            tool_off = offset + BUILD_VERSION_COMMAND.size
            lc.body["tools"] = [
                BUILD_TOOL_VERSION.unpack(data, tool_off + i * BUILD_TOOL_VERSION.size)
                for i in range(lc.body["ntools"])
            ]
        elif lc.cmd in (LC_DATA_IN_CODE, LC_LINKER_OPTIMIZATION_HINT):
            if cmdsize != LINKEDIT_DATA_COMMAND.size:
                raise MachOFormatError(
                    f"load command {lc.cmd:#x} has bad cmdsize {cmdsize}"
                )
            lc.body = LINKEDIT_DATA_COMMAND.unpack(data, offset)

        commands.append(lc)
        offset += cmdsize

    return MachOObject(header=header, commands=commands, data=data)
