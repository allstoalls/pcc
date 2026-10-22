"""Finite, owned ELF64 object writer and static x86_64 linker.

``LINK-P3-ELF-LINUX`` starts with the static, no-interpreter boundary used by
the freestanding Linux gate.  This module deliberately does not try to be a
general GNU linker.  It owns:

* little-endian ELF64 ``ET_REL`` objects for x86_64;
* strict parsing of the same finite object model, including ``SHT_RELA``;
* GNU/BSD ``ar`` member selection by unresolved-symbol closure; and
* fixed-address ``ET_EXEC`` publication with no ``PT_INTERP``, dynamic table,
  or residual relocation section.

Every unsupported section, symbol shape, or relocation is rejected before an
image is returned.  The first executable slice supports absolute and
PC-relative references plus the static GOT/initial-exec TLS relocations emitted
by pcc's x86_64 backend.  Shared objects, PIE, copy relocations, symbol
versioning, COMDAT, dynamic TLS, and linker scripts are intentionally outside
this contract.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .precise_stackmap import (
    ARCH_X86_64,
    ARCH_AARCH64,
    PreciseStackMapError,
    _FUNCTION as _STACK_MAP_FUNCTION,
    function_address_offsets,
    function_id,
    validate_stack_map_payload,
)


class ElfError(Exception):
    """Input or requested output is outside the proven ELF boundary."""


ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
EV_CURRENT = 1
ELFOSABI_SYSV = 0

ET_REL = 1
ET_EXEC = 2
EM_X86_64 = 62
EM_AARCH64 = 183

PT_LOAD = 1
PT_TLS = 7
PF_X = 1
PF_W = 2
PF_R = 4

SHT_NULL = 0
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_RELA = 4
SHT_NOTE = 7
SHT_NOBITS = 8
# LLVM's discardable address-significance metadata.  It informs later
# dead-stripping; this static slice keeps every selected section and therefore
# can safely consume-and-drop the table after validating its symtab link.
SHT_LLVM_ADDRSIG = 0x6FFF4C03

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_TLS = 0x400

SHN_UNDEF = 0
SHN_ABS = 0xFFF1
SHN_COMMON = 0xFFF2

STB_LOCAL = 0
STB_GLOBAL = 1
STB_WEAK = 2

STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3
STT_TLS = 6

STV_DEFAULT = 0
STV_HIDDEN = 2

R_X86_64_NONE = 0
R_X86_64_64 = 1
R_X86_64_PC32 = 2
R_X86_64_PLT32 = 4
R_X86_64_GOTPCREL = 9
R_X86_64_32 = 10
R_X86_64_32S = 11
R_X86_64_GOTTPOFF = 22
R_X86_64_TPOFF32 = 23
R_X86_64_GOTPCRELX = 41
R_X86_64_REX_GOTPCRELX = 42

_ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
_SECTION_HEADER = struct.Struct("<IIQQQQIIQQ")
_PROGRAM_HEADER = struct.Struct("<IIQQQQQQ")
_SYMBOL = struct.Struct("<IBBHQQ")
_RELA = struct.Struct("<QQq")

_PAGE = 0x1000
_BASE = 0x400000
_AR_MAGIC = b"!<arch>\n"
_AR_HEADER_SIZE = 60
_MAX_SECTIONS = 1_000_000
_MAX_SYMBOLS = 4_000_000


def _align(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise ElfError(f"alignment must be a positive power of two, got {alignment}")
    return (value + alignment - 1) & ~(alignment - 1)


def _cstring(table: bytes, offset: int, *, owner: str) -> str:
    if offset < 0 or offset >= len(table):
        raise ElfError(f"{owner} string offset {offset} is outside its table")
    end = table.find(b"\0", offset)
    if end < 0:
        raise ElfError(f"{owner} string at {offset} is not NUL terminated")
    try:
        return table[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ElfError(f"{owner} contains a non-UTF-8 name") from exc


class _StringTable:
    # Chunks are joined once: growing one buffer per string is quadratic
    # wherever a bytearray append copies (the pcc runtime's does).
    def __init__(self) -> None:
        self.chunks: list[bytes] = [b"\0"]
        self.size = 1
        self.offsets: dict[str, int] = {"": 0}

    def add(self, value: str) -> int:
        if not isinstance(value, str) or "\0" in value:
            raise ElfError(f"invalid ELF string {value!r}")
        if value in self.offsets:
            return self.offsets[value]
        encoded = value.encode("utf-8") + b"\0"
        offset = self.size
        self.chunks.append(encoded)
        self.size += len(encoded)
        self.offsets[value] = offset
        return offset

    def payload(self) -> bytes:
        return b"".join(self.chunks)


@dataclass(frozen=True)
class ElfRelocation:
    offset: int
    symbol_index: int
    type: int
    addend: int = 0


@dataclass(frozen=True)
class _PackedElfRelocations:
    """Private immutable RELA storage for an explicitly compact parse.

    Iteration/indexing project independent immutable records. No consumer may
    depend on projection identity; ordinary public parses still return tuples.
    A bounded range can share one immutable input-store payload without making
    a second RELA copy. Copy mutable input so caller changes cannot alter it.
    """

    payload: bytes
    offset: int = 0
    size: int | None = None
    # Parser-owned scratch only, never a consumable classification proof.
    _classification_candidate: list[bool | None] | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", bytes(self.payload))
        if self.size is None:
            object.__setattr__(self, "size", len(self.payload) - self.offset)
        if (self.offset < 0 or self.size < 0 or self.offset > len(self.payload)
                or self.size > len(self.payload) - self.offset):
            raise ElfError("packed ELF relocation range is outside its payload")
        if self.size % _RELA.size:
            raise ElfError("packed ELF relocation table is truncated")

    def __len__(self) -> int:
        return self.size // _RELA.size

    def __iter__(self):
        for position in range(self.offset, self.offset + self.size, _RELA.size):
            offset, info, addend = _RELA.unpack_from(self.payload, position)
            yield ElfRelocation(offset, info >> 32, info & 0xFFFFFFFF, addend)

    def __getitem__(self, index):
        if not isinstance(index, int):
            raise TypeError("ELF relocation index must be an integer")
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("ELF relocation index out of range")
        offset, info, addend = _RELA.unpack_from(
            self.payload, self.offset + index * _RELA.size,
        )
        return ElfRelocation(offset, info >> 32, info & 0xFFFFFFFF, addend)


def _x86_packed_classification_rows(relocations):
    """Keep the decoder's generator owner without constructing public records."""
    for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):
        _offset, info, addend = _RELA.unpack_from(relocations.payload, position)
        yield info >> 32, info & 0xFFFFFFFF, addend


def _x86_packed_application_rows(relocations):
    """Decode application fields without projecting a public relocation record."""
    for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):
        offset, info, addend = _RELA.unpack_from(relocations.payload, position)
        yield offset, info >> 32, info & 0xFFFFFFFF, addend


@dataclass(frozen=True)
class ElfSection:
    name: str
    type: int
    flags: int
    align: int
    data: bytes = b""
    mem_size: int = 0
    relocations: tuple[ElfRelocation, ...] | _PackedElfRelocations = ()

    @property
    def size(self) -> int:
        return self.mem_size if self.type == SHT_NOBITS else len(self.data)


@dataclass(frozen=True)
class ElfSymbol:
    name: str
    section_index: int
    value: int
    size: int
    binding: int
    type: int
    visibility: int = STV_DEFAULT

    @classmethod
    def null(cls) -> "ElfSymbol":
        return cls("", SHN_UNDEF, 0, 0, STB_LOCAL, STT_NOTYPE)


@dataclass(frozen=True)
class ElfObject:
    sections: tuple[ElfSection, ...]
    symbols: tuple[ElfSymbol, ...]
    machine: int = EM_X86_64

    def __post_init__(self) -> None:
        _validate_object(self)


def _validate_object(obj: ElfObject) -> None:
    if obj.machine not in (EM_X86_64, EM_AARCH64):
        raise ElfError("unsupported ELF machine: " + str(obj.machine))
    if not obj.symbols or obj.symbols[0] != ElfSymbol.null():
        raise ElfError("ELF object symbol zero must be the canonical null symbol")
    if len(obj.sections) > _MAX_SECTIONS or len(obj.symbols) > _MAX_SYMBOLS:
        raise ElfError("ELF object exceeds the finite section/symbol limit")
    names: set[str] = set()
    saw_global = False
    relocation_widths: dict[int, int] = {}
    for index, section in enumerate(obj.sections, start=1):
        if not section.name or "\0" in section.name or section.name in names:
            raise ElfError(f"invalid or duplicate section name {section.name!r}")
        names.add(section.name)
        if section.type not in (SHT_PROGBITS, SHT_NOBITS, SHT_NOTE):
            raise ElfError(
                f"section {section.name!r} has unsupported type {section.type}"
            )
        if section.align <= 0 or section.align & (section.align - 1):
            raise ElfError(f"section {section.name!r} has invalid alignment")
        if section.type == SHT_NOBITS:
            if section.data or section.mem_size < 0:
                raise ElfError(f"NOBITS section {section.name!r} carries file data")
        elif section.mem_size not in (0, len(section.data)):
            raise ElfError(f"file-backed section {section.name!r} has size drift")
        # Only the owned immutable ARM records can reuse successful widths.
        # Decide after this section's original checks: custom earlier sections
        # may have changed the object, and keep their ordinary protocol path.
        cache_widths = (
            type(obj) is ElfObject
            and type(obj.sections) is tuple
            and type(obj.symbols) is tuple
            and type(section) is ElfSection
            and type(section.data) is bytes
            and type(section.relocations) is _PackedElfRelocations
            and type(section.relocations.payload) is bytes
            and type(section.relocations.offset) is int
            and type(section.relocations.size) is int
            and type(obj.machine) is int
            and obj.machine == EM_AARCH64
        )
        classification_candidate = None
        if (type(obj) is ElfObject and type(obj.sections) is tuple
                and type(obj.symbols) is tuple and type(section) is ElfSection
                and type(section.relocations) is _PackedElfRelocations):
            candidate = section.relocations._classification_candidate
            if (type(candidate) is list and len(candidate) == 1
                    and (candidate[0] is None or type(candidate[0]) is bool)):
                # A reused view must not retain a previous validation's answer.
                candidate[0] = None
                if cache_widths and type(_RELA) is struct.Struct and _RELA.format == "<QQq":
                    classification_candidate = candidate
                    classification_candidate[0] = True
        for relocation in section.relocations:
            if relocation.symbol_index < 0 or relocation.symbol_index >= len(obj.symbols):
                raise ElfError(
                    f"section {section.name!r} relocation has invalid symbol index"
                )
            if cache_widths:
                width = relocation_widths.get(relocation.type)
                if width is None:
                    width = _relocation_width(relocation.type, obj.machine)
                    # The current ARM helper accepts 21 types, including NONE.
                    # A future extension still validates through the helper.
                    if len(relocation_widths) < 21:
                        relocation_widths[relocation.type] = width
            else:
                width = _relocation_width(relocation.type, obj.machine)
            if relocation.offset < 0 or relocation.offset + width > len(section.data):
                raise ElfError(
                    f"section {section.name!r} relocation is outside its payload"
                )
            if classification_candidate is not None and classification_candidate[0] is True:
                # Unknown/custom records cannot certify absence. These reads
                # follow every original per-row validation and never resolve symbols.
                if type(relocation) is not ElfRelocation or type(relocation.type) is not int:
                    classification_candidate[0] = None
                elif relocation.type in (283, 311, 312, 541, 542):
                    classification_candidate[0] = False
    for index, symbol in enumerate(obj.symbols):
        if index == 0:
            continue
        if symbol.binding not in (STB_LOCAL, STB_GLOBAL, STB_WEAK):
            raise ElfError(f"symbol {symbol.name!r} has unsupported binding")
        if symbol.type not in (STT_NOTYPE, STT_OBJECT, STT_FUNC, STT_SECTION, STT_TLS):
            raise ElfError(f"symbol {symbol.name!r} has unsupported type")
        if symbol.visibility not in (STV_DEFAULT, STV_HIDDEN):
            raise ElfError(f"symbol {symbol.name!r} has unsupported visibility")
        if symbol.binding == STB_LOCAL:
            if saw_global:
                raise ElfError("local symbols must precede global symbols")
        else:
            saw_global = True
        if symbol.section_index == SHN_COMMON:
            raise ElfError(f"COMMON symbol {symbol.name!r} is not supported")
        if symbol.section_index not in (SHN_UNDEF, SHN_ABS) and not (
            1 <= symbol.section_index <= len(obj.sections)
        ):
            raise ElfError(f"symbol {symbol.name!r} has invalid section index")
        if symbol.section_index == SHN_UNDEF and symbol.value != 0:
            raise ElfError(f"undefined symbol {symbol.name!r} has a value")
        if 1 <= symbol.section_index <= len(obj.sections):
            section = obj.sections[symbol.section_index - 1]
            if symbol.value < 0 or symbol.value + symbol.size > section.size:
                raise ElfError(f"symbol {symbol.name!r} is outside its section")
            if symbol.type == STT_TLS and not (section.flags & SHF_TLS):
                raise ElfError(f"TLS symbol {symbol.name!r} is outside a TLS section")

    for section in obj.sections:
        if section.name != ".pcc_stackmaps":
            continue
        if (
            section.type != SHT_PROGBITS
            or section.flags != SHF_ALLOC
            or section.align < 8
        ):
            raise ElfError(
                ".pcc_stackmaps must be an 8-byte-aligned read-only alloc section"
            )
        try:
            address_offsets = function_address_offsets(section.data)
            validate_stack_map_payload(
                section.data,
                expected_arch=ARCH_AARCH64 if obj.machine == EM_AARCH64 else ARCH_X86_64,
                final_image=False,
            )
        except PreciseStackMapError as exc:
            raise ElfError(f"invalid .pcc_stackmaps payload: {exc}") from exc
        relocation_by_offset = {
            relocation.offset: relocation for relocation in section.relocations
        }
        if len(relocation_by_offset) != len(section.relocations):
            raise ElfError("duplicate .pcc_stackmaps relocation offset")
        if set(relocation_by_offset) != set(address_offsets):
            raise ElfError(
                "every stack-map function address needs exactly one relocation"
            )
        for address_offset in address_offsets:
            relocation = relocation_by_offset[address_offset]
            if relocation.type != (257 if obj.machine == EM_AARCH64 else R_X86_64_64) or relocation.addend != 0:
                raise ElfError(
                    "stack-map function address needs a plain R_X86_64_64 relocation"
                )
            if section.data[address_offset:address_offset + 8] != b"\0" * 8:
                raise ElfError(
                    "relocatable stack-map function address must be zero"
                )
        for address_offset in address_offsets:
            relocation = relocation_by_offset[address_offset]
            symbol = obj.symbols[relocation.symbol_index]
            if symbol.section_index in (SHN_UNDEF, SHN_ABS):
                raise ElfError("stack-map function target must be section-defined")
            # The validated function header immediately precedes its address
            # relocation. Read only that header: decoding the entire map here
            # allocates one object per safepoint and referenced location even
            # though the identity check consumes none of those objects.
            stackmap_function_id = _STACK_MAP_FUNCTION.unpack_from(
                section.data, address_offset - 8
            )[0]
            if stackmap_function_id != function_id(symbol.name):
                raise ElfError(
                    "stack-map function id does not match its relocation symbol"
                )


def _relocation_width(reloc_type: int, machine: int = EM_X86_64) -> int:
    if machine == EM_AARCH64:
        from .elf_aarch64_relocations import relocation_width
        try:
            return relocation_width(reloc_type)
        except ValueError as exc:
            raise ElfError(str(exc)) from exc
    if reloc_type in (R_X86_64_NONE,):
        return 0
    if reloc_type in (R_X86_64_64,):
        return 8
    if reloc_type in (
        R_X86_64_PC32,
        R_X86_64_PLT32,
        R_X86_64_GOTPCREL,
        R_X86_64_32,
        R_X86_64_32S,
        R_X86_64_GOTTPOFF,
        R_X86_64_TPOFF32,
        R_X86_64_GOTPCRELX,
        R_X86_64_REX_GOTPCRELX,
    ):
        return 4
    raise ElfError(f"x86_64 relocation type {reloc_type} is not supported")


def emit_relocatable(obj: ElfObject) -> bytes:
    """Serialize one validated object as a standard ELF64 ``ET_REL`` file."""
    _validate_object(obj)
    section_names = _StringTable()
    string_table = _StringTable()
    for section in obj.sections:
        section_names.add(section.name)
    for symbol in obj.symbols:
        string_table.add(symbol.name)

    relocation_targets = [
        index
        for index, section in enumerate(obj.sections, start=1)
        if section.relocations
    ]
    for target in relocation_targets:
        section_names.add(".rela" + obj.sections[target - 1].name)
    section_names.add(".symtab")
    section_names.add(".strtab")
    section_names.add(".shstrtab")

    user_count = len(obj.sections)
    rela_base = 1 + user_count
    symtab_index = rela_base + len(relocation_targets)
    strtab_index = symtab_index + 1
    shstrtab_index = strtab_index + 1

    payloads: list[bytes] = [b""]
    headers: list[list[int]] = [[0] * 10]
    for section in obj.sections:
        payloads.append(b"" if section.type == SHT_NOBITS else bytes(section.data))
        headers.append([
            section_names.add(section.name),
            section.type,
            section.flags,
            0,
            0,
            section.size,
            0,
            0,
            section.align,
            0,
        ])

    for target_index in relocation_targets:
        target = obj.sections[target_index - 1]
        entries = []
        for relocation in target.relocations:
            info = (relocation.symbol_index << 32) | relocation.type
            entries.append(_RELA.pack(relocation.offset, info, relocation.addend))
        payload = b"".join(entries)
        payloads.append(payload)
        headers.append([
            section_names.add(".rela" + target.name),
            SHT_RELA,
            0,
            0,
            0,
            len(payload),
            symtab_index,
            target_index,
            8,
            _RELA.size,
        ])

    symbol_entries = []
    first_global = len(obj.symbols)
    for index, symbol in enumerate(obj.symbols):
        if first_global == len(obj.symbols) and symbol.binding != STB_LOCAL:
            first_global = index
        info = (symbol.binding << 4) | symbol.type
        symbol_entries.append(_SYMBOL.pack(
            string_table.add(symbol.name),
            info,
            symbol.visibility,
            symbol.section_index,
            symbol.value,
            symbol.size,
        ))
    symbol_payload = b"".join(symbol_entries)
    payloads.append(symbol_payload)
    headers.append([
        section_names.add(".symtab"), SHT_SYMTAB, 0, 0, 0,
        len(symbol_payload), strtab_index, first_global, 8, _SYMBOL.size,
    ])
    payloads.append(string_table.payload())
    headers.append([
        section_names.add(".strtab"), SHT_STRTAB, 0, 0, 0,
        string_table.size, 0, 0, 1, 0,
    ])
    payloads.append(section_names.payload())
    headers.append([
        section_names.add(".shstrtab"), SHT_STRTAB, 0, 0, 0,
        section_names.size, 0, 0, 1, 0,
    ])

    offset = _ELF_HEADER.size
    image_size = offset
    chunks = []
    for index in range(1, len(headers)):
        header = headers[index]
        alignment = max(1, header[8])
        offset = _align(offset, alignment)
        header[4] = offset
        if header[1] != SHT_NOBITS:
            if image_size < offset:
                chunks.append(b"\0" * (offset - image_size))
                image_size = offset
            chunks.append(payloads[index])
            image_size += len(payloads[index])
            offset += len(payloads[index])
    section_header_offset = _align(offset, 8)
    if image_size < section_header_offset:
        chunks.append(b"\0" * (section_header_offset - image_size))
    for header in headers:
        chunks.append(_SECTION_HEADER.pack(*header))

    ident = ELF_MAGIC + bytes((ELFCLASS64, ELFDATA2LSB, EV_CURRENT, ELFOSABI_SYSV))
    ident += b"\0" * (16 - len(ident))
    elf_header = _ELF_HEADER.pack(
        ident,
        ET_REL,
        obj.machine,
        EV_CURRENT,
        0,
        0,
        section_header_offset,
        0,
        _ELF_HEADER.size,
        0,
        0,
        _SECTION_HEADER.size,
        len(headers),
        shstrtab_index,
    )
    chunks.insert(0, elf_header)
    return b"".join(chunks)


def _unpack_elf_header(data: bytes | bytearray) -> tuple:
    if len(data) < _ELF_HEADER.size:
        raise ElfError("ELF file is shorter than its header")
    values = _ELF_HEADER.unpack_from(data, 0)
    ident = values[0]
    if ident[:4] != ELF_MAGIC:
        raise ElfError("not an ELF file")
    if ident[4] != ELFCLASS64 or ident[5] != ELFDATA2LSB:
        raise ElfError("only little-endian ELF64 is supported")
    if ident[6] != EV_CURRENT or values[3] != EV_CURRENT:
        raise ElfError("unsupported ELF version")
    if values[2] not in (EM_X86_64, EM_AARCH64):
        raise ElfError(f"unsupported ELF machine {values[2]}")
    return values


def parse_relocatable(data: bytes, *, compact_relocations: bool = False) -> ElfObject:
    """Parse a finite external ELF64 x86_64 relocatable object."""
    return _parse_relocatable(data, compact_relocations=compact_relocations)


def _parse_relocatable_indexed(data: bytes, *, _classification_summaries=None):
    """Return private file ranges only after the ordinary strict parse succeeds.

    The input store binds these immutable ranges to the exact source length and
    digest. They contain no payload or expanded relocation records and cannot
    be used to construct an unchecked public ElfObject.
    """
    data = bytes(data)
    ranges = []
    obj = _parse_relocatable(
        data, compact_relocations=True, _section_ranges=ranges,
        _classification_summaries=_classification_summaries,
    )
    return obj, tuple(ranges)


def _parse_relocatable(data: bytes, *, compact_relocations: bool = False,
                       _section_ranges=None, _classification_summaries=None) -> ElfObject:
    header = _unpack_elf_header(data)
    if header[1] != ET_REL:
        raise ElfError(f"expected ET_REL, got ELF type {header[1]}")
    phoff, shoff = header[5], header[6]
    ehsize, phentsize, phnum = header[8], header[9], header[10]
    shentsize, shnum, shstrndx = header[11], header[12], header[13]
    if ehsize != _ELF_HEADER.size or phoff != 0 or phnum != 0 or phentsize not in (0, _PROGRAM_HEADER.size):
        raise ElfError("relocatable object has an unsupported header layout")
    if shentsize != _SECTION_HEADER.size or not (0 < shnum <= _MAX_SECTIONS):
        raise ElfError("relocatable object has an invalid section table")
    if shoff < _ELF_HEADER.size or shoff + shnum * shentsize > len(data):
        raise ElfError("ELF section table is outside the file")
    if not (0 < shstrndx < shnum):
        raise ElfError("ELF section-name table index is invalid")

    raw_headers = [
        _SECTION_HEADER.unpack_from(data, shoff + index * shentsize)
        for index in range(shnum)
    ]

    def section_payload(index: int) -> bytes:
        raw = raw_headers[index]
        section_type, offset, size = raw[1], raw[4], raw[5]
        if section_type == SHT_NOBITS:
            return b""
        if offset > len(data) or size > len(data) - offset:
            raise ElfError(f"ELF section {index} is outside the file")
        return data[offset:offset + size]

    shstr = section_payload(shstrndx)
    raw_names = [
        _cstring(shstr, raw[0], owner="section-name table")
        for raw in raw_headers
    ]
    if raw_headers[0] != (0,) * 10:
        raise ElfError("ELF section zero is not the canonical null header")

    symtab_indices = [
        index for index, raw in enumerate(raw_headers) if raw[1] == SHT_SYMTAB
    ]
    if len(symtab_indices) != 1:
        raise ElfError("ELF object must contain exactly one SHT_SYMTAB")
    symtab_index = symtab_indices[0]
    symtab_header = raw_headers[symtab_index]
    if symtab_header[9] != _SYMBOL.size or symtab_header[5] % _SYMBOL.size:
        raise ElfError("ELF symbol table has an invalid entry size")
    strtab_index = symtab_header[6]
    if not (0 < strtab_index < shnum) or raw_headers[strtab_index][1] != SHT_STRTAB:
        raise ElfError("ELF symbol table has an invalid linked string table")
    symbol_strings = section_payload(strtab_index)

    addrsig_indices = {
        index
        for index, raw in enumerate(raw_headers)
        if raw[1] == SHT_LLVM_ADDRSIG
    }
    for index in addrsig_indices:
        raw = raw_headers[index]
        if raw[6] not in (0, symtab_index) or raw[7] or raw[9] not in (0, 1):
            raise ElfError(
                f"LLVM address-significance section {raw_names[index]!r} is malformed"
            )
        section_payload(index)
    meta_indices = {
        0, symtab_index, strtab_index, shstrndx,
        *addrsig_indices,
        *(
            index for index, raw in enumerate(raw_headers)
            if raw[1] == SHT_RELA
        ),
    }
    source_to_model: dict[int, int] = {}
    sections: list[ElfSection] = []
    ranges = []
    for source_index, raw in enumerate(raw_headers):
        if source_index in meta_indices:
            continue
        section_type = raw[1]
        if section_type not in (SHT_PROGBITS, SHT_NOBITS, SHT_NOTE):
            raise ElfError(
                f"section {raw_names[source_index]!r} has unsupported type {section_type}"
            )
        if raw[6] or raw[7] or raw[9]:
            raise ElfError(
                f"section {raw_names[source_index]!r} has unsupported link/info/entsize"
            )
        alignment = raw[8] or 1
        if alignment & (alignment - 1):
            raise ElfError(f"section {raw_names[source_index]!r} has invalid alignment")
        source_to_model[source_index] = len(sections) + 1
        sections.append(ElfSection(
            name=raw_names[source_index],
            type=section_type,
            flags=raw[2],
            align=alignment,
            data=section_payload(source_index),
            mem_size=raw[5] if section_type == SHT_NOBITS else 0,
        ))
        if _section_ranges is not None:
            ranges.append((raw[4], 0 if section_type == SHT_NOBITS else raw[5], 0, 0))

    symbols: list[ElfSymbol] = []
    raw_symbols = section_payload(symtab_index)
    symbol_count = len(raw_symbols) // _SYMBOL.size
    if not (0 < symbol_count <= _MAX_SYMBOLS):
        raise ElfError("ELF symbol table has an invalid size")
    for index in range(symbol_count):
        name_off, info, other, shndx, value, size = _SYMBOL.unpack_from(
            raw_symbols, index * _SYMBOL.size
        )
        binding, symbol_type = info >> 4, info & 0xF
        if shndx not in (SHN_UNDEF, SHN_ABS, SHN_COMMON):
            if shndx not in source_to_model:
                raise ElfError(
                    f"symbol {index} refers to unsupported section {shndx}"
                )
            shndx = source_to_model[shndx]
        symbols.append(ElfSymbol(
            _cstring(symbol_strings, name_off, owner="symbol table"),
            shndx,
            value,
            size,
            binding,
            symbol_type,
            other & 3,
        ))

    mutable_sections = list(sections)
    for rela_index, raw in enumerate(raw_headers):
        if raw[1] != SHT_RELA:
            continue
        if raw[6] != symtab_index or raw[9] != _RELA.size or raw[5] % _RELA.size:
            raise ElfError(f"relocation section {raw_names[rela_index]!r} is malformed")
        source_target = raw[7]
        if source_target not in source_to_model:
            raise ElfError(
                f"relocation section {raw_names[rela_index]!r} targets unsupported section"
            )
        target = source_to_model[source_target] - 1
        if mutable_sections[target].relocations:
            raise ElfError(f"section {mutable_sections[target].name!r} has multiple RELA tables")
        payload = section_payload(rela_index)
        if compact_relocations:
            candidate = None
            if (header[2] == EM_AARCH64 and _section_ranges is not None
                    and type(_classification_summaries) is list):
                try:
                    candidate = [None]
                except MemoryError:
                    pass  # Optional optimization metadata may be abandoned.
            relocations = _PackedElfRelocations(
                payload, _classification_candidate=candidate,
            )
        else:
            rows: list[ElfRelocation] = []
            for offset in range(0, len(payload), _RELA.size):
                r_offset, r_info, addend = _RELA.unpack_from(payload, offset)
                symbol_index, reloc_type = r_info >> 32, r_info & 0xFFFFFFFF
                rows.append(ElfRelocation(r_offset, symbol_index, reloc_type, addend))
            relocations = tuple(rows)
        old = mutable_sections[target]
        mutable_sections[target] = ElfSection(
            old.name, old.type, old.flags, old.align, old.data, old.mem_size,
            relocations,
        )
        if _section_ranges is not None:
            data_offset, data_size, _rela_offset, _rela_size = ranges[target]
            ranges[target] = (data_offset, data_size, raw[4], raw[5])
    obj = ElfObject(tuple(mutable_sections), tuple(symbols), header[2])
    if _section_ranges is not None:
        _section_ranges.extend(ranges)
    # Only the ordinary constructor's successful return commits a result:
    # symbol and stack-map validation happen after the mandatory RELA walk.
    if (header[2] == EM_AARCH64 and _section_ranges is not None
            and type(_classification_summaries) is list
            and type(obj) is ElfObject and type(obj.sections) is tuple):
        try:
            summaries = []
            for section in obj.sections:
                bit = None
                if type(section) is ElfSection and type(section.relocations) is _PackedElfRelocations:
                    candidate = section.relocations._classification_candidate
                    if type(candidate) is list and len(candidate) == 1:
                        if candidate[0] is True or candidate[0] is False:
                            bit = candidate[0]
                summaries.append(bit)
            _classification_summaries.extend(summaries)
        except MemoryError:
            _classification_summaries.clear()
    return obj


@dataclass(frozen=True)
class ElfArchiveMember:
    name: str
    object: ElfObject
    defines: frozenset[str]
    undefined: frozenset[str]


def _object_symbol_sets(obj: ElfObject) -> tuple[frozenset[str], frozenset[str]]:
    defined: set[str] = set()
    undefined: set[str] = set()
    for symbol in obj.symbols[1:]:
        if symbol.binding not in (STB_GLOBAL, STB_WEAK) or not symbol.name:
            continue
        if symbol.section_index == SHN_UNDEF:
            if symbol.binding != STB_WEAK:
                undefined.add(symbol.name)
        else:
            defined.add(symbol.name)
    return frozenset(defined), frozenset(undefined)


def read_archive_payloads(data: bytes) -> list[tuple[str, bytes]]:
    """Read GNU/BSD/COFF ar containers without assuming an object format."""
    if not data.startswith(_AR_MAGIC):
        raise ElfError("not an ar archive")
    offset = len(_AR_MAGIC)
    gnu_names = b""
    pending: list[tuple[str, bytes]] = []
    while offset < len(data):
        if offset + _AR_HEADER_SIZE > len(data):
            raise ElfError(f"truncated ar header at {offset}")
        header = data[offset:offset + _AR_HEADER_SIZE]
        if header[58:60] != b"`\n":
            raise ElfError(f"bad ar member magic at {offset}")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except (UnicodeDecodeError, ValueError) as exc:
            raise ElfError(f"bad ar member size at {offset}") from exc
        body = offset + _AR_HEADER_SIZE
        end = body + size
        if size < 0 or end > len(data):
            raise ElfError(f"ar member at {offset} runs past end of archive")
        raw_name = header[:16].rstrip()
        payload = data[body:end]
        offset = end + (end & 1)
        if raw_name == b"//":
            gnu_names = payload
            continue
        if raw_name in (b"/", b"/SYM64/") or raw_name.startswith(b"__.SYMDEF"):
            continue
        if raw_name.startswith(b"#1/"):
            try:
                length = int(raw_name[3:])
            except ValueError as exc:
                raise ElfError("bad BSD ar extended name") from exc
            if length > len(payload):
                raise ElfError("BSD ar extended name exceeds member")
            name = payload[:length].rstrip(b"\0").decode("utf-8", "surrogateescape")
            payload = payload[length:]
        elif raw_name.startswith(b"/") and raw_name[1:].isdigit():
            if not gnu_names:
                raise ElfError("GNU ar member uses a missing string table")
            name_offset = int(raw_name[1:])
            if name_offset >= len(gnu_names):
                raise ElfError("GNU ar member name offset is out of range")
            name_end = gnu_names.find(b"/\n", name_offset)
            if name_end < 0:
                name_end = gnu_names.find(b"\0", name_offset)
            if name_end < 0:
                raise ElfError("ar long member name is unterminated")
            name = gnu_names[name_offset:name_end].decode("utf-8", "surrogateescape")
        else:
            name = raw_name.rstrip(b"/").decode("utf-8", "surrogateescape")
        # BSD extended names encode the symbol index name in the payload;
        # filtering only the header spelling treats ranlib data as an object.
        if name.startswith("__.SYMDEF"):
            continue
        pending.append((name, payload))

    return pending


def read_archive(data: bytes, *, compact_relocations: bool = False) -> tuple[ElfArchiveMember, ...]:
    members: list[ElfArchiveMember] = []
    for name, payload in read_archive_payloads(data):
        try:
            obj = parse_relocatable(payload, compact_relocations=compact_relocations)
        except ElfError as exc:
            raise ElfError(f"archive member {name!r} is not a proven ELF object: {exc}") from exc
        defined, undefined = _object_symbol_sets(obj)
        members.append(ElfArchiveMember(name, obj, defined, undefined))
    return tuple(members)


def select_archive_members(
    members: tuple[ElfArchiveMember, ...],
    undefined: set[str],
    *,
    already_defined: set[str] | frozenset[str] = frozenset(),
    selected_names: list[str] | None = None,
) -> tuple[list[ElfObject], set[str]]:
    pending = set(undefined) - set(already_defined)
    provided = set(already_defined)
    selected: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index, member in enumerate(members):
            if index in selected or not (member.defines & pending):
                continue
            selected.add(index)
            provided.update(member.defines)
            pending.difference_update(member.defines)
            pending.update(member.undefined - provided)
            changed = True
    if selected_names is not None:
        selected_names.extend(member.name for index, member in enumerate(members)
                              if index in selected)
    return [member.object for index, member in enumerate(members) if index in selected], pending


@dataclass(frozen=True)
class _Placement:
    file_offset: int
    address: int


def _global_state(objects: list[ElfObject]) -> tuple[dict[str, tuple[int, int]], set[str]]:
    definitions: dict[str, tuple[int, int]] = {}
    weak_definitions: dict[str, tuple[int, int]] = {}
    undefined: set[str] = set()
    for object_index, obj in enumerate(objects):
        for symbol_index, symbol in enumerate(obj.symbols[1:], start=1):
            if symbol.binding not in (STB_GLOBAL, STB_WEAK) or not symbol.name:
                continue
            if symbol.section_index == SHN_UNDEF:
                if symbol.binding != STB_WEAK:
                    undefined.add(symbol.name)
                continue
            target = (object_index, symbol_index)
            if symbol.binding == STB_WEAK:
                weak_definitions.setdefault(symbol.name, target)
                continue
            if symbol.name in definitions:
                raise ElfError(f"duplicate strong definition of {symbol.name!r}")
            definitions[symbol.name] = target
    for name, target in weak_definitions.items():
        definitions.setdefault(name, target)
    return definitions, undefined - set(definitions)


def _symbol_identity(
    objects: list[ElfObject],
    definitions: dict[str, tuple[int, int]],
    object_index: int,
    symbol_index: int,
) -> tuple[str, object]:
    symbol = objects[object_index].symbols[symbol_index]
    if symbol.binding == STB_LOCAL or symbol.section_index != SHN_UNDEF:
        return ("local", (object_index, symbol_index))
    if symbol.name in definitions:
        return ("global", symbol.name)
    if symbol.binding == STB_WEAK:
        return ("weak-zero", symbol.name)
    raise ElfError(f"undefined symbol {symbol.name!r}")


def _write_int(image: bytearray, offset: int, value: int, bits: int, signed: bool) -> None:
    minimum = -(1 << (bits - 1)) if signed else 0
    maximum = (1 << (bits - (1 if signed else 0))) - 1
    if value < minimum or value > maximum:
        kind = "signed" if signed else "unsigned"
        raise ElfError(f"relocation result {value} does not fit {kind} i{bits}")
    image[offset:offset + bits // 8] = int(value).to_bytes(
        bits // 8, "little", signed=signed
    )


def _aarch64_relocation_error_context(
    object_index: int, section_index: int, section_name: str,
    relocation_offset: int, relocation_type: int,
    symbol_index: int, symbol_name: str, resolved_symbol_name: str,
    symbol_address: int, addend: int, target_base: int, place: int,
    section_file_offset: int, section_address: int,
    section_size: int, image_size: int,
) -> str:
    """Format resident metadata only after a relocation has already failed."""
    # Units describe the relocation type, not a second evaluation of its check.
    # The original _signed exception supplies the actual checked value and bits.
    units = "not-signed-pcrel"
    if relocation_type in (282, 283):
        units = "4-byte-instruction-words"
    elif relocation_type in (275, 311, 541):
        units = "4096-byte-pages"
    elif relocation_type in (260, 261, 274):
        units = "bytes"
    return (
        "; rtype=" + str(relocation_type)
        + "; relocation_units=" + units
        + "; object_index=" + str(object_index)
        + "; section_index=" + str(section_index)
        + "; section=" + repr(section_name)
        + "; relocation_offset=" + str(relocation_offset)
        + "; symbol_index=" + str(symbol_index)
        + "; symbol=" + repr(symbol_name)
        + "; resolved_symbol=" + repr(resolved_symbol_name)
        + "; S=" + str(symbol_address)
        + "; A=" + str(addend)
        + "; target_base=" + str(target_base)
        + "; effective_target=" + str(target_base + addend)
        + "; P=" + str(place)
        + "; section_file_offset=" + str(section_file_offset)
        + "; section_address=" + str(section_address)
        + "; section_size=" + str(section_size)
        + "; image_size=" + str(image_size)
    )


def link_static_executable(
    objects: list[ElfObject],
    *,
    archives: list[bytes] = (),
    entry: str = "_start",
    base_address: int = _BASE,
    archive_selections: list[tuple[int, str]] | None = None,
    compact_archive_relocations: bool = False,
) -> bytes:
    """Public immutable ELF image; private publication may retain the builder."""
    return bytes(_link_static_executable_image(
        objects, archives=archives, entry=entry, base_address=base_address,
        archive_selections=archive_selections,
        compact_archive_relocations=compact_archive_relocations,
    ))


def _staged_classification_is_empty(store, objects, object_index, section_index, section, relocations):
    try:
        if (type(objects) is not list or type(object_index) is not int
                or not 0 <= object_index < len(objects)):
            return False
        from .owned_elf_inputs import _classification_is_empty
        return _classification_is_empty(
            store, objects[object_index], section_index, section, relocations,
        )
    except MemoryError:
        # Failure to acquire optional evidence takes the original scan path.
        return False


def _report_classification_skips(timing, completed_sections, total_sections,
                                 skipped_sections, skipped_relocations):
    try:
        return timing.elf_classification_skips(
            completed_sections, total_sections, skipped_sections, skipped_relocations,
        )
    except BaseException:
        # Optional bounded evidence must not replace a linker result or error.
        return False


def _elf_relocation_progress_counts(alloc_sections):
    """Resident row cardinalities only; never load or iterate relocation data."""
    try:
        from .owned_elf_inputs import _InputSection
        if type(alloc_sections) is not list:
            return None
        # Keep the weighted numerator within signed 64 bits on native hosts.
        limit = ((1 << 63) - 1) // 8
        if len(alloc_sections) > limit:
            return None
        # total rows, completed sections/rows, last quantile; then section counts.
        counts = [0, 0, 0, 0]
        for item in alloc_sections:
            if type(item) is not tuple or len(item) != 3:
                return None
            observed_section = item[2]
            if type(observed_section) is _InputSection:
                count = observed_section.relocation_count
            elif type(observed_section) is ElfSection:
                rows = observed_section.relocations
                if type(rows) is tuple:
                    count = len(rows)
                elif type(rows) is _PackedElfRelocations:
                    if (type(rows.payload) is not bytes or type(rows.offset) is not int
                            or type(rows.size) is not int or rows.offset < 0 or rows.size < 0
                            or rows.offset > len(rows.payload)
                            or rows.size > len(rows.payload) - rows.offset or rows.size % 24):
                        return None
                    count = rows.size // 24
                else:
                    return None
            else:
                return None
            if type(count) is not int or count < 0 or count > limit - counts[0]:
                return None
            counts[0] += count
            counts.append(count)
        return counts
    except BaseException:
        # Optional evidence must not introduce a new linker failure.
        return None


def _report_relocation_progress(timing, event, completed_sections, total_sections,
                                completed_relocations, total_relocations):
    try:
        return timing.elf_relocation_progress(
            event, completed_sections, total_sections,
            completed_relocations, total_relocations,
        )
    except BaseException:
        return False


def _link_static_executable_image(
    objects: list[ElfObject],
    *,
    archives: list[bytes] = (),
    entry: str = "_start",
    base_address: int = _BASE,
    archive_selections: list[tuple[int, str]] | None = None,
    compact_archive_relocations: bool = False,
    _input_store=None,
    coordinator_timing=None,
) -> bytearray:
    """Build a private mutable image, fully validated before publication."""
    image_phase_started = 0
    if coordinator_timing is not None:
        image_phase_started = coordinator_timing.begin("elf_image_select_inputs")
    if not objects:
        raise ElfError("static ELF link requires at least one explicit object")
    if base_address < 0x10000 or base_address % _PAGE:
        raise ElfError("static ELF base address must be page-aligned and >= 0x10000")
    objects = list(objects)
    machine = objects[0].machine
    boundary_names = ("__init_array_start", "__init_array_end", "__fini_array_start", "__fini_array_end")
    boundary_symbols = [ElfSymbol.null()]
    for name in boundary_names:
        boundary_symbols.append(ElfSymbol(name, SHN_ABS, 0, 0, STB_GLOBAL, STT_NOTYPE))
    objects.append(ElfObject((), tuple(boundary_symbols), machine))
    definitions, undefined = _global_state(objects)
    archive_inputs = archives if _input_store is None else _input_store.archives
    for archive_index, archive_data in enumerate(archive_inputs):
        if entry not in definitions:
            undefined.add(entry)
        selected_names = [] if archive_selections is not None else None
        selected, _remaining = select_archive_members(
            (read_archive(archive_data, compact_relocations=compact_archive_relocations)
             if _input_store is None else _input_store.read_archive(archive_index)),
            undefined,
            already_defined=set(definitions),
            selected_names=selected_names,
        )
        if archive_selections is not None:
            archive_selections.extend((archive_index, name) for name in selected_names)
        objects.extend(selected)
        definitions, undefined = _global_state(objects)
    if any(obj.machine != machine for obj in objects):
        raise ElfError("cannot mix ELF machines in a link")
    if undefined:
        raise ElfError("undefined static ELF symbols: " + ", ".join(sorted(undefined)))
    if entry not in definitions:
        raise ElfError(f"entry symbol {entry!r} is not defined")

    if coordinator_timing is not None:
        coordinator_timing.complete("elf_image_select_inputs", image_phase_started)
        image_phase_started = coordinator_timing.begin("elf_image_classify_layout")
    alloc_sections: list[tuple[int, int, ElfSection]] = []
    for object_index, obj in enumerate(objects):
        for section_index, section in enumerate(obj.sections, start=1):
            if not section.flags & SHF_ALLOC:
                if section.relocations:
                    raise ElfError(f"non-alloc section {section.name!r} carries relocations")
                if section.name not in (".note.GNU-stack", ".comment", ".llvm_addrsig"):
                    raise ElfError(f"non-alloc section {section.name!r} is not proven")
                continue
            if section.type == SHT_NOTE:
                raise ElfError(f"allocated NOTE section {section.name!r} is not proven")
            if section.type == SHT_NOBITS and not section.flags & SHF_WRITE:
                raise ElfError(f"NOBITS section {section.name!r} must be writable")
            if section.flags & SHF_TLS and not section.flags & SHF_WRITE:
                raise ElfError(f"TLS section {section.name!r} must be writable")
            alloc_sections.append((object_index, section_index, section))

    # Reserve by input identity and RELA addend, never symbol name. Do not
    # resolve branch symbols during classification: a later bad symbol must
    # not displace the first relocation failure in application order.
    branch_slots: dict[tuple[int, int], dict[tuple[int, int, int], int]] = {}
    got_identities: list[tuple[tuple[str, object], str]] = []
    got_seen: set[tuple[tuple[str, object], str]] = set()
    got_reloc_types = {
        R_X86_64_GOTPCREL,
        R_X86_64_GOTTPOFF,
        R_X86_64_GOTPCRELX,
        R_X86_64_REX_GOTPCRELX,
    }
    if machine == EM_AARCH64:
        got_reloc_types = {311, 312, 541, 542}
    # A prior generic row must still be released by the original first packed
    # unpack/split/assignment, not by a metadata-only shortcut.
    relocation = None
    summary_counts = None
    if coordinator_timing is not None:
        try:
            # completed sections, skipped sections/rows, last bucket, total sections
            summary_counts = [0, 0, 0, 0, len(alloc_sections)]
        except BaseException:
            pass
    for object_index, section_index, section in alloc_sections:
        relocations = section.relocations
        if type(machine) is int and machine == EM_AARCH64 and type(relocations) is _PackedElfRelocations:
            try:
                if (relocation is None and _input_store is not None
                        and _staged_classification_is_empty(
                            _input_store, objects, object_index, section_index, section, relocations,
                        )):
                    if summary_counts is not None:
                        try:
                            if relocations.size:
                                summary_counts[1] += 1
                                summary_counts[2] += relocations.size // _RELA.size
                        except BaseException:
                            summary_counts = None
                else:
                    # Keep the iterator's exact range and per-row payload reads. Only
                    # its transient frozen record is unnecessary during classification.
                    for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):
                        _offset, info, addend = _RELA.unpack_from(relocations.payload, position)
                        symbol_index, relocation_type = info >> 32, info & 0xFFFFFFFF
                        # The ordinary loop overwrites its previous row here. Release
                        # a preceding custom row at the same boundary before callbacks.
                        relocation = None
                        if (machine == EM_AARCH64 and relocation_type == 283
                                and section.flags & SHF_EXECINSTR
                                and not section.flags & SHF_WRITE):
                            slots = branch_slots.get((object_index, section_index))
                            if slots is None:
                                slots = {}
                                branch_slots[(object_index, section_index)] = slots
                            branch_key = (object_index, symbol_index, addend)
                            if branch_key not in slots:
                                slots[branch_key] = len(slots)
                        if relocation_type not in got_reloc_types:
                            continue
                        identity = _symbol_identity(
                            objects, definitions, object_index, symbol_index
                        )
                        kind = "tpoff" if relocation_type in ((541, 542) if machine == EM_AARCH64 else (R_X86_64_GOTTPOFF,)) else "address"
                        key = (identity, kind)
                        if key not in got_seen:
                            got_seen.add(key)
                            got_identities.append(key)
            finally:
                # An exception traceback must not retain this source's view.
                del relocations
        elif (type(machine) is int and machine == EM_X86_64
                and type(relocations) is _PackedElfRelocations
                and type(getattr(relocations, "payload", None)) is bytes
                and type(relocations.offset) is int and type(relocations.size) is int
                and type(_RELA) is struct.Struct and _RELA.format == "<QQq"):
            # Keep a generator as the decoder-error owner, including if a
            # symbol callback changes a later payload. Body errors release it.
            relocation_sources = [relocations]
            del relocations
            for symbol_index, relocation_type, addend in _x86_packed_classification_rows(relocation_sources.pop()):
                # Match assignment of the first projected row before callbacks.
                relocation = None
                if relocation_type not in got_reloc_types:
                    continue
                identity = _symbol_identity(objects, definitions, object_index, symbol_index)
                kind = "tpoff" if relocation_type == R_X86_64_GOTTPOFF else "address"
                key = (identity, kind)
                if key not in got_seen:
                    got_seen.add(key)
                    got_identities.append(key)
            del relocation_sources
        else:
            # Transfer the temporary iterable directly to for's one GET_ITER.
            # The exact list is empty before callbacks or exceptions can run.
            relocation_sources = [relocations]
            del relocations
            for relocation in relocation_sources.pop():
                if (machine == EM_AARCH64 and relocation.type == 283
                        and section.flags & SHF_EXECINSTR
                        and not section.flags & SHF_WRITE):
                    slots = branch_slots.get((object_index, section_index))
                    if slots is None:
                        slots = {}
                        branch_slots[(object_index, section_index)] = slots
                    branch_key = (object_index, relocation.symbol_index, relocation.addend)
                    if branch_key not in slots:
                        slots[branch_key] = len(slots)
                if relocation.type not in got_reloc_types:
                    continue
                identity = _symbol_identity(
                    objects, definitions, object_index, relocation.symbol_index
                )
                kind = "tpoff" if relocation.type in ((541, 542) if machine == EM_AARCH64 else (R_X86_64_GOTTPOFF,)) else "address"
                key = (identity, kind)
                if key not in got_seen:
                    got_seen.add(key)
                    got_identities.append(key)
            del relocation_sources
        if summary_counts is not None:
            try:
                # Count only after the whole section and its original cleanup.
                # These are unweighted section quantiles, never per-row observations.
                summary_counts[0] += 1
                summary_bucket = 8 * summary_counts[0] // summary_counts[4]
                if summary_bucket > summary_counts[3]:
                    summary_counts[3] = summary_bucket
                    if not _report_classification_skips(
                        coordinator_timing, summary_counts[0], summary_counts[4],
                        summary_counts[1], summary_counts[2],
                    ):
                        summary_counts = None
            except BaseException:
                summary_counts = None

    metadata_sections = [
        item for item in alloc_sections
        if item[2].name == ".pcc_stackmaps"
    ]
    rx_sections = [
        item for item in alloc_sections
        if not item[2].flags & SHF_WRITE
        and item[2].name != ".pcc_stackmaps"
    ]
    if machine == EM_AARCH64:
        rx_sections.sort(key=lambda item: not bool(item[2].flags & SHF_EXECINSTR))
    rw_file_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_WRITE
        and not item[2].flags & SHF_TLS
        and item[2].type != SHT_NOBITS
    ]
    rw_file_sections.sort(key=lambda item: (
        1 if item[2].name.startswith(".init_array") else 2 if item[2].name.startswith(".fini_array") else 0,
        item[2].name if item[2].name.startswith((".init_array", ".fini_array")) else "",
    ))
    tdata_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_TLS and item[2].type != SHT_NOBITS
    ]
    tbss_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_TLS and item[2].type == SHT_NOBITS
    ]
    bss_sections = [
        item for item in alloc_sections
        if not item[2].flags & SHF_TLS and item[2].type == SHT_NOBITS
    ]
    if any(item[2].flags & SHF_EXECINSTR for item in rw_file_sections + tdata_sections + tbss_sections + bss_sections):
        raise ElfError("writable executable ELF sections are not supported")

    has_rw = bool(rw_file_sections or tdata_sections or tbss_sections or bss_sections or got_identities)
    has_tls = bool(tdata_sections or tbss_sections)
    phnum = 1 + int(has_rw) + int(has_tls) + int(bool(metadata_sections))
    header_bytes = _ELF_HEADER.size + phnum * _PROGRAM_HEADER.size
    placements: dict[tuple[int, int], _Placement] = {}

    branch_pools: dict[tuple[int, int], tuple[int, int]] = {}
    veneer_pools: list[tuple[int, int]] = []
    veneer_padding: list[tuple[int, int]] = []
    if branch_slots:
        from .elf_aarch64_relocations import veneer_pool_end
    cursor = _align(header_bytes, _PAGE)
    for object_index, section_index, section in rx_sections:
        section_key = (object_index, section_index)
        slot_count = len(branch_slots.get(section_key, {}))
        if slot_count:
            before_pool = _align(cursor, 8)
            veneer_padding.append((cursor, before_pool))
            veneer_pools.append((before_pool, slot_count))
            cursor = veneer_pool_end(before_pool, slot_count)
        aligned = _align(cursor, section.align)
        if branch_slots and section.flags & SHF_EXECINSTR:
            veneer_padding.append((cursor, aligned))
        cursor = aligned
        placements[section_key] = _Placement(cursor, base_address + cursor)
        cursor += section.size
        if slot_count:
            after_pool = _align(cursor, 8)
            veneer_padding.append((cursor, after_pool))
            veneer_pools.append((after_pool, slot_count))
            cursor = veneer_pool_end(after_pool, slot_count)
            branch_pools[section_key] = (before_pool, after_pool)
    rx_file_end = cursor

    rw_start = _align(rx_file_end, _PAGE) if has_rw else 0
    cursor = rw_start
    for object_index, section_index, section in rw_file_sections:
        cursor = _align(cursor, section.align)
        placements[(object_index, section_index)] = _Placement(cursor, base_address + cursor)
        cursor += section.size
    got_offset = _align(cursor, 8)
    got_address = base_address + got_offset
    cursor = got_offset + len(got_identities) * 8

    tls_align = max((item[2].align for item in tdata_sections + tbss_sections), default=1)
    tls_file_start = _align(cursor, tls_align) if has_tls else 0
    if has_tls:
        cursor = tls_file_start
    for object_index, section_index, section in tdata_sections:
        cursor = _align(cursor, section.align)
        placements[(object_index, section_index)] = _Placement(cursor, base_address + cursor)
        cursor += section.size
    tls_file_end = cursor
    memory_cursor = cursor
    for object_index, section_index, section in tbss_sections:
        memory_cursor = _align(memory_cursor, section.align)
        placements[(object_index, section_index)] = _Placement(
            tls_file_end, base_address + memory_cursor
        )
        memory_cursor += section.size
    tls_memory_end = memory_cursor
    for object_index, section_index, section in bss_sections:
        memory_cursor = _align(memory_cursor, section.align)
        placements[(object_index, section_index)] = _Placement(
            cursor, base_address + memory_cursor
        )
        memory_cursor += section.size
    rw_file_end = cursor
    rw_memory_end = memory_cursor

    metadata_file_start = 0
    metadata_file_end = 0
    metadata_address_start = 0
    if metadata_sections:
        # Metadata is read-only and may be much larger than the signed PC-relative
        # window. Keep code, constants and writable globals together first.
        # File and virtual positions differ when the RW segment ends in BSS.
        metadata_align = max(_PAGE, max(item[2].align for item in metadata_sections))
        metadata_file_start = _align(max(rx_file_end, rw_file_end), metadata_align)
        metadata_address_start = _align(max(rx_file_end, rw_memory_end), metadata_align)
        cursor = metadata_file_start
        address_cursor = metadata_address_start
        for object_index, section_index, section in metadata_sections:
            cursor = _align(cursor, section.align)
            address_cursor = _align(address_cursor, section.align)
            placements[(object_index, section_index)] = _Placement(
                cursor, base_address + address_cursor,
            )
            cursor += section.size
            address_cursor += section.size
        metadata_file_end = cursor
    image_size = max(rx_file_end, rw_file_end, metadata_file_end, header_bytes)
    if coordinator_timing is not None:
        coordinator_timing.complete("elf_image_classify_layout", image_phase_started)
        image_phase_started = coordinator_timing.begin("elf_image_allocate_copy")
    if _input_store is not None:
        _input_store.clear()
    image = bytearray(image_size)
    for object_index, section_index, section in alloc_sections:
        if section.type == SHT_NOBITS:
            continue
        placement = placements[(object_index, section_index)]
        image[placement.file_offset:placement.file_offset + section.size] = section.data

    if veneer_pools:
        from .elf_aarch64_relocations import initialize_veneer_pool
        for padding_start, padding_end in veneer_padding:
            # Complete instructions only; a pre-existing partial instruction
            # at a section end is not made executable by linker padding.
            padding_start = _align(padding_start, 4)
            for padding_offset in range(padding_start, padding_end - 3, 4):
                struct.pack_into("<I", image, padding_offset, 0xD503201F)
        for pool_start, slot_count in veneer_pools:
            initialize_veneer_pool(image, pool_start, slot_count)

    if coordinator_timing is not None:
        coordinator_timing.complete("elf_image_allocate_copy", image_phase_started)
        image_phase_started = coordinator_timing.begin("elf_image_resolve_relocate")
    boundaries = {}
    for family in ("init", "fini"):
        selected = [item for item in alloc_sections if item[2].name.startswith("." + family + "_array")]
        begin = min((placements[(oi, si)].address for oi, si, sec in selected), default=0)
        end = max((placements[(oi, si)].address + sec.size for oi, si, sec in selected), default=0)
        boundaries["__" + family + "_array_start"] = begin
        boundaries["__" + family + "_array_end"] = end

    if coordinator_timing is not None:
        try:
            relocation_progress = _elf_relocation_progress_counts(alloc_sections)
            relocation_progress_active = 1
            if relocation_progress is None:
                if not _report_relocation_progress(coordinator_timing, "scale_unknown", 0, 0, 0, 0):
                    relocation_progress_active = 0
            elif not _report_relocation_progress(
                    coordinator_timing, "scale", 0, len(relocation_progress) - 4,
                    0, relocation_progress[0]):
                relocation_progress = None
                relocation_progress_active = 0
        except BaseException:
            relocation_progress = None
            relocation_progress_active = 0

    def resolve_symbol(object_index: int, symbol_index: int) -> tuple[int, ElfSymbol]:
        identity = _symbol_identity(objects, definitions, object_index, symbol_index)
        if identity[0] == "weak-zero":
            return 0, objects[object_index].symbols[symbol_index]
        target_object, target_symbol = (
            identity[1] if identity[0] == "local" else definitions[str(identity[1])]
        )
        symbol = objects[target_object].symbols[target_symbol]
        if symbol.section_index == SHN_ABS:
            return boundaries.get(symbol.name, symbol.value), symbol
        if symbol.section_index == SHN_UNDEF:
            return 0, symbol
        placement = placements.get((target_object, symbol.section_index))
        if placement is None:
            raise ElfError(f"symbol {symbol.name!r} is defined in a non-alloc section")
        return placement.address + symbol.value, symbol

    tls_tp = base_address + tls_file_start + _align(tls_memory_end - tls_file_start, tls_align) if has_tls else 0

    def tls_offset(object_index: int, symbol_index: int) -> int:
        address, symbol = resolve_symbol(object_index, symbol_index)
        if symbol.type != STT_TLS:
            raise ElfError(f"TLS relocation targets non-TLS symbol {symbol.name!r}")
        if not has_tls:
            raise ElfError("TLS relocation exists without a PT_TLS image")
        if machine == EM_AARCH64:
            return address - (base_address + tls_file_start) + _align(16, tls_align)
        return address - tls_tp

    got_offsets = {identity: index * 8 for index, identity in enumerate(got_identities)}
    for (identity, kind), relative in got_offsets.items():
        if identity[0] == "weak-zero":
            if kind == "tpoff":
                raise ElfError("GOTTPOFF cannot target an undefined weak symbol")
            value = 0
        else:
            target_object, target_symbol = (
                identity[1] if identity[0] == "local" else definitions[str(identity[1])]
            )
            symbol = objects[target_object].symbols[target_symbol]
            if kind == "tpoff":
                value = tls_offset(target_object, target_symbol)
            else:
                if symbol.type == STT_TLS:
                    raise ElfError(
                        f"ordinary GOT relocation targets TLS symbol {symbol.name!r}"
                    )
                value = resolve_symbol(target_object, target_symbol)[0]
        _write_int(image, got_offset + relative, value, 64, value < 0)

    if coordinator_timing is not None:
        try:
            if relocation_progress_active and not _report_relocation_progress(
                    coordinator_timing, "got_complete", 0, 0, 0, 0):
                relocation_progress = None
        except BaseException:
            relocation_progress = None

    for object_index, section_index, section in alloc_sections:
        if section.type == SHT_NOBITS and section.relocations:
            raise ElfError(f"NOBITS section {section.name!r} carries relocations")
        placement = placements[(object_index, section_index)]
        relocations = section.relocations
        if (type(machine) is int and machine == EM_AARCH64
                and type(relocations) is _PackedElfRelocations
                and type(relocations.payload) is bytes
                and type(relocations.offset) is int and type(relocations.size) is int):
            try:
                # Match the public iterator's range and per-row payload reads,
                # without projecting an unused temporary frozen record.
                for position in range(relocations.offset, relocations.offset + relocations.size, _RELA.size):
                    relocation_offset, info, addend = _RELA.unpack_from(relocations.payload, position)
                    symbol_index, relocation_type = info >> 32, info & 0xFFFFFFFF
                    # Preserve the previous generic row's release boundary.
                    relocation = None
                    if relocation_type == R_X86_64_NONE:
                        continue
                    patch = placement.file_offset + relocation_offset
                    place = placement.address + relocation_offset
                    symbol_address, _symbol = resolve_symbol(
                        object_index, symbol_index
                    )
                    from .elf_aarch64_relocations import (
                        apply_call26_with_veneer,
                        apply_relocation,
                        veneer_slot_offset,
                    )
                    target = symbol_address
                    if relocation_type in (311, 312, 541, 542):
                        identity = _symbol_identity(objects, definitions, object_index, symbol_index)
                        kind = "tpoff" if relocation_type in (541, 542) else "address"
                        target = got_address + got_offsets[(identity, kind)]
                    elif relocation_type in (549, 551):
                        target = tls_offset(object_index, symbol_index)
                    try:
                        if (relocation_type == 283 and _symbol.type == STT_FUNC
                                and _symbol.section_index != SHN_UNDEF
                                and not -(1 << 27) <= target + addend - place < (1 << 27)
                                and (object_index, section_index) in branch_pools):
                            slot_index = branch_slots[(object_index, section_index)][
                                (object_index, symbol_index, addend)
                            ]
                            pool_starts = branch_pools[(object_index, section_index)]
                            veneer_offsets = tuple(veneer_slot_offset(pool, slot_index)
                                                   for pool in pool_starts)
                            apply_call26_with_veneer(image, patch,
                                                    target + addend, place,
                                                    veneer_offsets)
                        else:
                            apply_relocation(image, patch, relocation_type,
                                             target + addend, place)
                    except ValueError as exc:
                        raise ElfError(str(exc) + _aarch64_relocation_error_context(
                            object_index, section_index, section.name,
                            relocation_offset, relocation_type,
                            symbol_index,
                            objects[object_index].symbols[symbol_index].name,
                            _symbol.name, symbol_address, addend, target, place,
                            placement.file_offset, placement.address,
                            section.size, image_size,
                        )) from exc
            finally:
                # Do not retain a source view in an exception traceback.
                del relocations
        elif (type(machine) is int and machine == EM_X86_64
                and type(relocations) is _PackedElfRelocations
                and type(getattr(relocations, "payload", None)) is bytes
                and type(relocations.offset) is int and type(relocations.size) is int
                and type(_RELA) is struct.Struct and _RELA.format == "<QQq"):
            # Keep decoding in its generator: a decoder exception retains its
            # source, but an application exception must release the iterator.
            relocation_sources = [relocations]
            del relocations
            for relocation_offset, symbol_index, relocation_type, addend in _x86_packed_application_rows(relocation_sources.pop()):
                # As in the generic loop, decode before releasing a prior row.
                relocation = None
                if relocation_type == R_X86_64_NONE:
                    continue
                patch = placement.file_offset + relocation_offset
                place = placement.address + relocation_offset
                symbol_address, _symbol = resolve_symbol(object_index, symbol_index)
                if relocation_type == R_X86_64_64:
                    _write_int(image, patch, symbol_address + addend, 64, False)
                elif relocation_type in (R_X86_64_PC32, R_X86_64_PLT32):
                    _write_int(image, patch, symbol_address + addend - place, 32, True)
                elif relocation_type == R_X86_64_32:
                    _write_int(image, patch, symbol_address + addend, 32, False)
                elif relocation_type == R_X86_64_32S:
                    _write_int(image, patch, symbol_address + addend, 32, True)
                elif relocation_type in (
                    R_X86_64_GOTPCREL,
                    R_X86_64_GOTPCRELX,
                    R_X86_64_REX_GOTPCRELX,
                    R_X86_64_GOTTPOFF,
                ):
                    identity = _symbol_identity(objects, definitions, object_index, symbol_index)
                    kind = "tpoff" if relocation_type == R_X86_64_GOTTPOFF else "address"
                    slot = got_address + got_offsets[(identity, kind)]
                    _write_int(image, patch, slot + addend - place, 32, True)
                elif relocation_type == R_X86_64_TPOFF32:
                    value = tls_offset(object_index, symbol_index)
                    _write_int(image, patch, value + addend, 32, True)
                else:
                    raise ElfError(f"relocation type {relocation_type} has no link rule")
            del relocation_sources
        else:
            # Transfer ownership to for's original single GET_ITER operation.
            relocation_sources = [relocations]
            del relocations
            for relocation in relocation_sources.pop():
                if relocation.type == R_X86_64_NONE:
                    continue
                patch = placement.file_offset + relocation.offset
                place = placement.address + relocation.offset
                symbol_address, _symbol = resolve_symbol(
                    object_index, relocation.symbol_index
                )
                if machine == EM_AARCH64:
                    from .elf_aarch64_relocations import (
                        apply_call26_with_veneer,
                        apply_relocation,
                        veneer_slot_offset,
                    )
                    target = symbol_address
                    if relocation.type in (311, 312, 541, 542):
                        identity = _symbol_identity(objects, definitions, object_index, relocation.symbol_index)
                        kind = "tpoff" if relocation.type in (541, 542) else "address"
                        target = got_address + got_offsets[(identity, kind)]
                    elif relocation.type in (549, 551):
                        target = tls_offset(object_index, relocation.symbol_index)
                    try:
                        if (relocation.type == 283 and _symbol.type == STT_FUNC
                                and _symbol.section_index != SHN_UNDEF
                                and not -(1 << 27) <= target + relocation.addend - place < (1 << 27)
                                and (object_index, section_index) in branch_pools):
                            slot_index = branch_slots[(object_index, section_index)][
                                (object_index, relocation.symbol_index, relocation.addend)
                            ]
                            pool_starts = branch_pools[(object_index, section_index)]
                            veneer_offsets = tuple(veneer_slot_offset(pool, slot_index)
                                                   for pool in pool_starts)
                            apply_call26_with_veneer(image, patch,
                                                    target + relocation.addend, place,
                                                    veneer_offsets)
                        else:
                            apply_relocation(image, patch, relocation.type,
                                             target + relocation.addend, place)
                    except ValueError as exc:
                        raise ElfError(str(exc) + _aarch64_relocation_error_context(
                            object_index, section_index, section.name,
                            relocation.offset, relocation.type,
                            relocation.symbol_index,
                            objects[object_index].symbols[relocation.symbol_index].name,
                            _symbol.name, symbol_address, relocation.addend, target, place,
                            placement.file_offset, placement.address,
                            section.size, image_size,
                        )) from exc
                elif relocation.type == R_X86_64_64:
                    _write_int(image, patch, symbol_address + relocation.addend, 64, False)
                elif relocation.type in (R_X86_64_PC32, R_X86_64_PLT32):
                    _write_int(image, patch, symbol_address + relocation.addend - place, 32, True)
                elif relocation.type == R_X86_64_32:
                    _write_int(image, patch, symbol_address + relocation.addend, 32, False)
                elif relocation.type == R_X86_64_32S:
                    _write_int(image, patch, symbol_address + relocation.addend, 32, True)
                elif relocation.type in (
                    R_X86_64_GOTPCREL,
                    R_X86_64_GOTPCRELX,
                    R_X86_64_REX_GOTPCRELX,
                    R_X86_64_GOTTPOFF,
                ):
                    identity = _symbol_identity(
                        objects, definitions, object_index, relocation.symbol_index
                    )
                    kind = (
                        "tpoff"
                        if relocation.type == R_X86_64_GOTTPOFF
                        else "address"
                    )
                    slot = got_address + got_offsets[(identity, kind)]
                    _write_int(image, patch, slot + relocation.addend - place, 32, True)
                elif relocation.type == R_X86_64_TPOFF32:
                    value = tls_offset(object_index, relocation.symbol_index)
                    _write_int(image, patch, value + relocation.addend, 32, True)
                else:
                    # _relocation_width made this unreachable unless a newly
                    # accepted writer relocation was not deliberately linked.
                    raise ElfError(f"relocation type {relocation.type} has no link rule")
            del relocation_sources
        if coordinator_timing is not None:
            try:
                if relocation_progress is not None:
                    progress_section = relocation_progress[1]
                    relocation_progress[2] += relocation_progress[progress_section + 4]
                    relocation_progress[1] = progress_section + 1
                    if relocation_progress[0]:
                        progress_bucket = 8 * relocation_progress[2] // relocation_progress[0]
                        if progress_bucket > relocation_progress[3]:
                            relocation_progress[3] = progress_bucket
                            if not _report_relocation_progress(
                                    coordinator_timing, "sections", relocation_progress[1],
                                    len(relocation_progress) - 4,
                                    relocation_progress[2], relocation_progress[0]):
                                relocation_progress = None
            except BaseException:
                relocation_progress = None

    if coordinator_timing is not None:
        coordinator_timing.complete("elf_image_resolve_relocate", image_phase_started)
        image_phase_started = coordinator_timing.begin("elf_image_finalize")
    if _input_store is not None:
        _input_store.clear()
    # Validate the exact bytes that will become executable metadata after all
    # function-address relocations have been applied.  The final ET_EXEC drops
    # section headers, so this is the last owned publication boundary at which
    # a malformed or unresolved PC table can still be attributed precisely.
    for object_index, section_index, section in alloc_sections:
        if section.name != ".pcc_stackmaps":
            continue
        placement = placements[(object_index, section_index)]
        payload = bytes(
            image[
                placement.file_offset:
                placement.file_offset + section.size
            ]
        )
        try:
            validate_stack_map_payload(
                payload,
                expected_arch=ARCH_AARCH64 if machine == EM_AARCH64 else ARCH_X86_64,
                final_image=True,
            )
        except PreciseStackMapError as exc:
            raise ElfError(
                f"invalid final .pcc_stackmaps payload: {exc}"
            ) from exc

    entry_object, entry_symbol = definitions[entry]
    entry_record = objects[entry_object].symbols[entry_symbol]
    if entry_record.section_index in (SHN_UNDEF, SHN_ABS):
        raise ElfError(f"entry symbol {entry!r} is not section-defined")
    entry_section = objects[entry_object].sections[entry_record.section_index - 1]
    if not entry_section.flags & SHF_EXECINSTR:
        raise ElfError(f"entry symbol {entry!r} is not in executable code")
    entry_address, _ = resolve_symbol(entry_object, entry_symbol)
    program_headers: list[bytes] = [
        _PROGRAM_HEADER.pack(
            PT_LOAD, PF_R | PF_X, 0, base_address, base_address,
            rx_file_end, rx_file_end, _PAGE,
        )
    ]
    if has_rw:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_LOAD, PF_R | PF_W, rw_start, base_address + rw_start,
            base_address + rw_start, rw_file_end - rw_start,
            rw_memory_end - rw_start, _PAGE,
        ))
    if has_tls:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_TLS, PF_R, tls_file_start, base_address + tls_file_start,
            base_address + tls_file_start, tls_file_end - tls_file_start,
            tls_memory_end - tls_file_start, tls_align,
        ))
    if metadata_sections:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_LOAD, PF_R, metadata_file_start,
            base_address + metadata_address_start,
            base_address + metadata_address_start,
            metadata_file_end - metadata_file_start,
            metadata_file_end - metadata_file_start,
            _PAGE,
        ))
    ident = ELF_MAGIC + bytes((ELFCLASS64, ELFDATA2LSB, EV_CURRENT, ELFOSABI_SYSV))
    ident += b"\0" * (16 - len(ident))
    image[:_ELF_HEADER.size] = _ELF_HEADER.pack(
        ident, ET_EXEC, machine, EV_CURRENT, entry_address,
        _ELF_HEADER.size, 0, 0, _ELF_HEADER.size,
        _PROGRAM_HEADER.size, len(program_headers), 0, 0, 0,
    )
    phoff = _ELF_HEADER.size
    for program_header in program_headers:
        image[phoff:phoff + _PROGRAM_HEADER.size] = program_header
        phoff += _PROGRAM_HEADER.size
    if coordinator_timing is not None:
        coordinator_timing.complete("elf_image_finalize", image_phase_started)
    return image


def parse_static_executable(data: bytes | bytearray) -> dict[str, int]:
    """Validate the final no-dynamic ELF shape and return useful fields."""
    header = _unpack_elf_header(data)
    if header[1] != ET_EXEC:
        raise ElfError("final image is not ET_EXEC")
    if header[6] != 0 or header[12] != 0:
        raise ElfError("final static image unexpectedly has section headers")
    phoff, phentsize, phnum = header[5], header[9], header[10]
    if phentsize != _PROGRAM_HEADER.size or phnum not in (1, 2, 3, 4):
        raise ElfError("final static image has an unsupported program-header table")
    if phoff + phentsize * phnum > len(data):
        raise ElfError("program-header table is truncated")
    load_count = 0
    tls_count = 0
    for index in range(phnum):
        values = _PROGRAM_HEADER.unpack_from(data, phoff + index * phentsize)
        p_type, _flags, offset, _vaddr, _paddr, filesz, memsz, align = values
        if p_type == PT_LOAD:
            load_count += 1
        elif p_type == PT_TLS:
            tls_count += 1
        else:
            raise ElfError(f"unexpected program header type {p_type}")
        if filesz > memsz or offset > len(data) or filesz > len(data) - offset:
            raise ElfError("program header maps bytes outside the image")
        if align <= 0 or align & (align - 1):
            raise ElfError("program header has invalid alignment")
    if load_count not in (1, 2, 3) or tls_count > 1:
        raise ElfError("final static image has an invalid LOAD/TLS topology")
    return {
        "entry": header[4],
        "program_headers": phnum,
        "load_segments": load_count,
        "tls_segments": tls_count,
    }
