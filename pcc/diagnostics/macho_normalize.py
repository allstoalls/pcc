"""Mach-O metadata normalization used by bootstrap byte-identity checks."""

from __future__ import annotations

import argparse
from pathlib import Path
import struct


_LC_UUID = 0x1B
_MACHO_32_LE = b"\xce\xfa\xed\xfe"
_MACHO_64_LE = b"\xcf\xfa\xed\xfe"
_MACHO_32_BE = b"\xfe\xed\xfa\xce"
_MACHO_64_BE = b"\xfe\xed\xfa\xcf"
_FAT_MAGIC = b"\xca\xfe\xba\xbe"
_FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"


def _zero_thin_macho_uuid(data: bytearray, base: int = 0) -> int:
    magic = bytes(data[base : base + 4])
    if magic in {_MACHO_32_LE, _MACHO_64_LE}:
        endian = "<"
    elif magic in {_MACHO_32_BE, _MACHO_64_BE}:
        endian = ">"
    else:
        return 0

    header_size = 32 if magic in {_MACHO_64_LE, _MACHO_64_BE} else 28
    if base + header_size > len(data):
        return 0
    ncmds = struct.unpack_from(endian + "I", data, base + 16)[0]
    cursor = base + header_size
    changed = 0
    for _ in range(ncmds):
        if cursor + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack_from(endian + "II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > len(data):
            break
        if cmd == _LC_UUID and cmdsize >= 24:
            data[cursor + 8 : cursor + 24] = b"\x00" * 16
            changed += 1
        cursor += cmdsize
    return changed


def _zero_macho_uuid_bytes(data: bytearray) -> int:
    magic = bytes(data[:4])
    if magic not in {_FAT_MAGIC, _FAT_MAGIC_64}:
        return _zero_thin_macho_uuid(data, 0)

    if len(data) < 8:
        return 0
    nfat = struct.unpack_from(">I", data, 4)[0]
    entry_size = 32 if magic == _FAT_MAGIC_64 else 20
    changed = 0
    for i in range(nfat):
        entry = 8 + i * entry_size
        if entry + entry_size > len(data):
            break
        if magic == _FAT_MAGIC_64:
            arch_offset = struct.unpack_from(">Q", data, entry + 8)[0]
        else:
            arch_offset = struct.unpack_from(">I", data, entry + 8)[0]
        if arch_offset < len(data):
            changed += _zero_thin_macho_uuid(data, int(arch_offset))
    return changed


def normalize_macho_metadata(path: str | Path) -> int:
    """Normalize nondeterministic Mach-O metadata in-place.

    The helper currently zeros LC_UUID payload bytes. It intentionally does not
    remove code signatures; callers that need signature normalization should run
    ``codesign --remove-signature`` before calling this function.
    """

    path = Path(path)
    data = bytearray(path.read_bytes())
    changed = _zero_macho_uuid_bytes(data)
    if changed:
        path.write_bytes(data)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    for path in args.paths:
        normalize_macho_metadata(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
