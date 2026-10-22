"""PE32+ documented range checks, not Windows loader boundary qualification."""

import struct

import pytest

from pcc.backend import pe_x86_64 as pe
from pcc.backend.coff_x86_64 import CoffError, CoffObject, CoffSection, CoffSymbol


@pytest.mark.parametrize("size", (0x7FFFF000, 0x80000000))
def test_pe_image_range_does_not_reject_the_documented_maximum(size):
    # The documentation does not establish actual loader behavior at
    # equality; do not invent a stricter rejection without that evidence.
    pe._check_image_size(size)


@pytest.mark.parametrize("size", (0x80000001, 0x80001000, 0x945F5000, 0x100000000))
def test_pe_image_range_rejects_only_sizes_above_documented_maximum(size):
    with pytest.raises(CoffError, match="exceeds documented 2 GiB limit") as caught:
        pe._check_image_size(size)
    assert hex(size) in str(caught.value)


def test_pe_image_error_reports_existing_layout_counts_without_payload_contents():
    sections = [(".text", 0x1000, b"\xc3", 0x60000020),
                (".rdata", 0x2000, b"private", 0x40000040)]
    with pytest.raises(CoffError) as caught:
        pe._check_image_size(0x80001000, sections, stack_map_bytes=3)
    message = str(caught.value)
    assert "2147487744 bytes (0x80001000)" in message
    assert ".text@0x1000 bytes=1" in message
    assert ".rdata@0x2000 bytes=7" in message
    assert "merged_stack_map_bytes=3" in message
    assert "private" not in message


def test_pe_link_checks_the_actual_size_written_to_the_optional_header(monkeypatch):
    checked = []
    original = pe._check_image_size

    def check(size, sections=(), *, stack_map_bytes=0, stack_map_counts=()):
        checked.append(size)
        original(size, sections, stack_map_bytes=stack_map_bytes,
                 stack_map_counts=stack_map_counts)

    monkeypatch.setattr(pe, "_check_image_size", check)
    obj = CoffObject((CoffSection(".text", b"\xc3", 0x60000020, 16),),
                     (CoffSymbol("pcc_windows_start", 1, 0, True, True),))
    image = pe.link_executable([obj])
    optional = struct.unpack_from("<I", image, 60)[0] + 24
    assert checked == [struct.unpack_from("<I", image, optional + 56)[0]]
