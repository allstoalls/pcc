"""Deterministic indexed archives for pcc's owned object formats."""

from .ar import ArchiveFormatError


def _member(name: str, payload: bytes, offset: int) -> bytes:
    encoded = name.encode("utf-8", "surrogateescape")
    if not encoded or b"\0" in encoded or b"\n" in encoded:
        raise ArchiveFormatError("invalid archive member name")
    # BSD extended filenames also align the enclosed object to eight bytes.
    name_size = len(encoded) + (-(offset + 60 + len(encoded))) % 8
    contents = encoded + b"\0" * (name_size - len(encoded)) + payload
    fields = ["#1/" + str(name_size), "0", "0", "0", "100644", str(len(contents))]
    widths = [16, 12, 6, 6, 8, 10]
    header = b""
    for field, width in zip(fields, widths):
        value = field.encode("ascii")
        if len(value) > width:
            raise ArchiveFormatError("archive member exceeds format limits")
        header += value.ljust(width)
    return header + b"`\n" + contents + (b"\n" if len(contents) % 2 else b"")


def _defined_symbols(payload: bytes) -> tuple[str, list[str]]:
    if payload[:4] == b"\xcf\xfa\xed\xfe":
        from . import macho_spec as spec

        obj = spec.parse_object(payload)
        names = []
        for symbol in obj.symbols():
            flags = symbol["n_type"]
            kind = flags & spec.N_TYPE
            if flags & spec.N_EXT and not flags & spec.N_STAB and kind in (spec.N_SECT, spec.N_ABS):
                names.append(symbol["name"])
        return "macho", names
    if payload[:4] == b"\x7fELF":
        from .elf_x86_64 import parse_relocatable, STB_GLOBAL, STB_WEAK, SHN_UNDEF

        obj = parse_relocatable(payload)
        return "elf", [symbol.name for symbol in obj.symbols
                       if symbol.name and symbol.binding in (STB_GLOBAL, STB_WEAK)
                       and symbol.section_index != SHN_UNDEF]
    raise ArchiveFormatError("archive input is not an owned relocatable object")


def write_archive(members: list[tuple[str, bytes]]) -> bytes:
    """Write BSD ar framing and its sorted 32-bit ranlib symbol index."""
    symbols = []
    names = set()
    formats = set()
    for index, (name, payload) in enumerate(members):
        if name in names or name.startswith("__.SYMDEF"):
            raise ArchiveFormatError("duplicate or reserved archive member name")
        names.add(name)
        kind, definitions = _defined_symbols(payload)
        formats.add(kind)
        symbols.extend((symbol.encode("utf-8"), index) for symbol in definitions)
    if len(formats) > 1:
        raise ArchiveFormatError("archive cannot mix object formats")
    symbols.sort()
    strings = b"".join(symbol + b"\0" for symbol, _index in symbols)
    index_size = 4 + len(symbols) * 8 + 4 + len(strings)
    index_placeholder = _member("__.SYMDEF SORTED", b"\0" * index_size, 8)
    offset = 8 + len(index_placeholder)
    member_offsets = []
    encoded_members = []
    for name, payload in members:
        if offset > 4294967295:
            raise ArchiveFormatError("archive exceeds the 32-bit symbol-index limit")
        member_offsets.append(offset)
        encoded = _member(name, payload, offset)
        encoded_members.append(encoded)
        offset += len(encoded)
    index_parts = [(len(symbols) * 8).to_bytes(4, "little")]
    string_offset = 0
    for symbol, member_index in symbols:
        index_parts.append(string_offset.to_bytes(4, "little"))
        index_parts.append(member_offsets[member_index].to_bytes(4, "little"))
        string_offset += len(symbol) + 1
    index_parts.append(len(strings).to_bytes(4, "little"))
    index_parts.append(strings)
    index_member = _member("__.SYMDEF SORTED", b"".join(index_parts), 8)
    if len(index_member) != len(index_placeholder):
        raise ArchiveFormatError("archive index layout changed during emission")
    return b"!<arch>\n" + index_member + b"".join(encoded_members)
