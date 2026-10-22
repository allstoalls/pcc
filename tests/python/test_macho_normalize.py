from __future__ import annotations

import struct

from pcc.diagnostics.macho_normalize import normalize_macho_metadata


def test_normalize_macho_metadata_zeros_lc_uuid_payload(tmp_path):
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        0x0100000C,
        0,
        2,
        1,
        24,
        0,
        0,
    )
    uuid = bytes(range(16))
    load_command = struct.pack("<II", 0x1B, 24) + uuid
    path = tmp_path / "thin.macho"
    path.write_bytes(header + load_command + b"payload")

    assert normalize_macho_metadata(path) == 1

    data = path.read_bytes()
    assert data[:32] == header
    assert data[32:40] == struct.pack("<II", 0x1B, 24)
    assert data[40:56] == b"\x00" * 16
    assert data[56:] == b"payload"
