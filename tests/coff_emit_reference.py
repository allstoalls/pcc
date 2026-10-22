"""Frozen pre-chunk COFF encoder, production blob 9ddce504aeee90871219d67cc2ec57690483a5ee.

This independent writer is the whole-file byte/error-order oracle for the
bounded chunk rewrite. It deliberately retains the old growing bytearrays.
"""

import struct

from pcc.backend.coff_x86_64 import (
    ADDR64, ADDR32, ADDR32NB, SECTION, SECREL, CoffError, CoffObject,
)


def _width(kind):
    if kind == ADDR64:
        return 8
    if kind == SECTION:
        return 2
    if kind in (ADDR32, ADDR32NB, SECREL) or 4 <= kind <= 9:
        return 4
    raise CoffError("unsupported AMD64 COFF relocation: " + str(kind))


def emit_object(obj: CoffObject) -> bytes:
    strings = bytearray(b"\0\0\0\0")
    names = {}
    for name in [sec.name for sec in obj.sections] + [sym.name for sym in obj.symbols]:
        if "\0" in name:
            raise CoffError("NUL in COFF name")
        if len(name.encode("utf-8")) > 8 and name not in names:
            names[name] = len(strings)
            strings.extend(name.encode("utf-8") + b"\0")
    struct.pack_into("<I", strings, 0, len(strings))
    data = bytearray(b"\0" * (20 + len(obj.sections) * 40))
    for index, sec in enumerate(obj.sections):
        if sec.align <= 0 or sec.align & (sec.align - 1) or sec.align > 8192:
            raise CoffError("invalid COFF section alignment")
        raw = len(data)
        data.extend(sec.data)
        reloc = len(data)
        count = len(sec.relocations)
        flags = sec.flags | ((sec.align.bit_length()) << 20)
        if count >= 65535:
            flags |= 0x01000000
            data.extend(struct.pack("<IIH", count + 1, 0, 0))
        for record in sec.relocations:
            width = _width(record.kind)
            if record.offset < 0 or record.offset + width > len(sec.data):
                raise CoffError("COFF relocation exceeds section")
            if not 0 <= record.symbol < len(obj.symbols):
                raise CoffError("COFF relocation symbol out of range")
            data.extend(struct.pack("<IIH", record.offset, record.symbol, record.kind))
        encoded = ("/" + str(names[sec.name])).encode() if sec.name in names else sec.name.encode()
        struct.pack_into("<8sIIIIIIHHI", data, 20 + index * 40, encoded,
                         0, 0, len(sec.data), raw, reloc if count else 0, 0,
                         min(count, 65535), 0, flags)
    symoff = len(data)
    for symbol in obj.symbols:
        encoded = (struct.pack("<II", 0, names[symbol.name]) if symbol.name in names
                   else symbol.name.encode().ljust(8, b"\0"))
        data.extend(struct.pack("<8sIhHBB", encoded, symbol.value, symbol.section,
                                0x20 if symbol.function else 0,
                                2 if symbol.external else 3, 0))
    data.extend(strings)
    struct.pack_into("<HHIIIHH", data, 0, 0x8664, len(obj.sections), 0,
                     symoff, len(obj.symbols), 0, 0)
    return bytes(data)
