"""Relocatable DWARF4 section-offset repair shared by owned merge paths.

Machine addresses stay in the normal relocation tables. DWARF section offsets
are a different domain: they are rebased within their corresponding merged
section, never to a virtual address. All reads are bounded by the source unit.
"""
from __future__ import annotations

import struct


class DwarfLinkError(ValueError):
    pass


def _uint(data, position: int, width: int, limit: int) -> tuple[int, int]:
    if position < 0 or width < 0 or position + width > limit:
        raise DwarfLinkError("truncated DWARF field")
    return int.from_bytes(data[position:position + width], "little"), position + width


def _leb(data, position: int, limit: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        byte, position = _uint(data, position, 1, limit)
        if shift == 63 and byte > 1:
            raise DwarfLinkError("DWARF integer exceeds 64 bits")
        result |= (byte & 127) << shift
        if not byte & 128:
            return result, position
    raise DwarfLinkError("unterminated DWARF integer")


def _string_end(data, position: int, limit: int) -> int:
    while position < limit:
        if data[position] == 0:
            return position + 1
        position += 1
    raise DwarfLinkError("unterminated DWARF string")


def _abbreviations(data, start: int):
    position = start
    result = {}
    while position < len(data):
        code, position = _leb(data, position, len(data))
        if code == 0:
            return result
        _tag, position = _leb(data, position, len(data))
        children, position = _uint(data, position, 1, len(data))
        if code in result or children not in (0, 1):
            raise DwarfLinkError("invalid DWARF abbreviation")
        attributes = []
        while True:
            name, position = _leb(data, position, len(data))
            form, position = _leb(data, position, len(data))
            if name == 0 and form == 0:
                break
            if name == 0 or form == 0:
                raise DwarfLinkError("incomplete DWARF abbreviation attribute")
            attributes.append((name, form))
        result[code] = attributes
    raise DwarfLinkError("unterminated DWARF abbreviation table")


# DW_FORM_sec_offset attribute classes (DWARF4 section 7.5.4).
_SECTION_ATTRIBUTES = {
    0x10: "__debug_line", 0x43: "__debug_macinfo", 0x55: "__debug_ranges",
    0x02: "__debug_loc", 0x19: "__debug_loc", 0x2A: "__debug_loc",
    0x38: "__debug_loc", 0x40: "__debug_loc", 0x46: "__debug_loc",
    0x48: "__debug_loc", 0x4A: "__debug_loc", 0x4D: "__debug_loc",
}
_FIXED_FORMS = {0x05: 2, 0x06: 4, 0x07: 8, 0x0B: 1, 0x0C: 1,
                0x11: 1, 0x12: 2, 0x13: 4, 0x14: 8, 0x19: 0, 0x20: 8}


def _patch_offset(payload, position: int, limit: int, section: str, bases, sizes):
    value, end = _uint(payload, position, 4, limit)
    if section not in sizes or not 0 <= value < sizes[section]:
        raise DwarfLinkError("DWARF offset is outside input " + section)
    relocated = value + bases[section]
    if not 0 <= relocated < 0xFFFFFFF0:
        raise DwarfLinkError("merged DWARF offset exceeds DWARF32")
    struct.pack_into("<I", payload, position, relocated)
    return end


def patch_dwarf_info(payload: bytearray, abbreviations, bases: dict[str, int], sizes: dict[str, int]) -> None:
    """Patch a caller-owned mutable copy after source section placement.

    `bases` and `sizes` use names within this input's __DWARF segment. Repeated
    partial links work because each source CU retains its current abbreviation
    offset and every referenced section contribution is adjusted exactly once.
    """
    unit = 0
    table_offset = -1
    table = None
    while unit < len(payload):
        length, position = _uint(payload, unit, 4, len(payload))
        if length >= 0xFFFFFFF0 or length < 7 or unit + 4 + length > len(payload):
            raise DwarfLinkError("invalid or unsupported DWARF32 unit length")
        end = unit + 4 + length
        version, position = _uint(payload, position, 2, end)
        if version != 4:
            raise DwarfLinkError("owned debug merge requires DWARF4 units")
        abbrev_offset, _unused = _uint(payload, position, 4, end)
        position = _patch_offset(payload, position, end, "__debug_abbrev", bases, sizes)
        address_size, position = _uint(payload, position, 1, end)
        if address_size != 8:
            raise DwarfLinkError("owned debug merge requires 64-bit addresses")
        if table is None or table_offset != abbrev_offset:
            table = _abbreviations(abbreviations, abbrev_offset)
            table_offset = abbrev_offset
        while position < end:
            code, position = _leb(payload, position, end)
            if code == 0:
                continue
            attributes = table.get(code)
            if attributes is None:
                raise DwarfLinkError("DWARF entry references an unknown abbreviation")
            for name, form in attributes:
                # An indirect form is encoded in the DIE itself. Keep the
                # finite decode bounded even for malformed nested indirects.
                for _depth in range(8):
                    if form != 0x16:
                        break
                    form, position = _leb(payload, position, end)
                if form == 0x16:
                    raise DwarfLinkError("nested DWARF indirect form exceeds limit")
                if form == 0x17:
                    section = _SECTION_ATTRIBUTES.get(name)
                    if section is None:
                        raise DwarfLinkError("unknown DWARF section-offset attribute " + str(name))
                    position = _patch_offset(payload, position, end, section, bases, sizes)
                elif form in (0x0E, 0x10):
                    section = "__debug_str" if form == 0x0E else "__debug_info"
                    position = _patch_offset(payload, position, end, section, bases, sizes)
                elif form == 0x08:
                    position = _string_end(payload, position, end)
                elif form in (0x0F, 0x15):
                    _value, position = _leb(payload, position, end)
                elif form == 0x0D:
                    # Signed LEB's final byte can carry sign bits; we only
                    # need its encoded extent, not an unsigned interpretation.
                    for _count in range(10):
                        byte, position = _uint(payload, position, 1, end)
                        if not byte & 128:
                            break
                    else:
                        raise DwarfLinkError("unterminated DWARF signed integer")
                elif form in (0x03, 0x04, 0x09, 0x0A, 0x18):
                    if form in (0x09, 0x18):
                        count, position = _leb(payload, position, end)
                    else:
                        width = {0x03: 2, 0x04: 4, 0x0A: 1}[form]
                        count, position = _uint(payload, position, width, end)
                    if position + count > end:
                        raise DwarfLinkError("truncated DWARF block")
                    position += count
                elif form == 0x01 or form in _FIXED_FORMS:
                    width = address_size if form == 0x01 else _FIXED_FORMS[form]
                    _value, position = _uint(payload, position, width, end)
                else:
                    raise DwarfLinkError("unsupported DWARF form " + str(form))
        unit = end
