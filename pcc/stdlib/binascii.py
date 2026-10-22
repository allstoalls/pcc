"""pcc.stdlib.binascii - small binary helpers."""

from __future__ import annotations


class Error(Exception):
    pass


def crc32(data, value: int = 0) -> int:
    # CPython accepts an arbitrary Python int seed but the CRC state is a
    # 32-bit register.  Mask before the first shift so high seed bits cannot
    # bleed into the checksum over the next bytes.
    crc = (value & 0xFFFFFFFF) ^ 0xFFFFFFFF
    for b in data:
        cur = b if isinstance(b, int) else ord(b)
        crc = crc ^ (cur & 0xFF)
        i = 0
        while i < 8:
            if crc & 1:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = crc >> 1
            i += 1
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
