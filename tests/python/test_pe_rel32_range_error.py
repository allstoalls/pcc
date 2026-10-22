"""An unreachable PE REL32 names the reference and the image layout.

Windows Stage1 at 263d12af compiled all 460 modules and then failed in the PE
link with only "PE rel32 out of range" (run 38436916173), which gave no symbol,
section or distance to work from.
"""

from __future__ import annotations

import pytest


def test_rel32_overflow_reports_symbol_sections_and_displacement():
    from pcc.backend.coff_x86_64 import CoffError, CoffObject, CoffRelocation, CoffSection, CoffSymbol
    from pcc.backend.pe_x86_64 import link_executable

    # REL32 (kind 4) at offset 0 with an addend of 0x7fffffff to a symbol
    # eight bytes further on: the displacement exceeds 2**31 - 1.
    text = CoffSection(
        ".text", b"\xff\xff\xff\x7f" + b"\xc3" * 12, 0x60000020, 16,
        (CoffRelocation(0, 1, 4),),
    )
    obj = CoffObject((text,), (
        CoffSymbol("pcc_windows_start", 1, 0, True, True),
        CoffSymbol("far_target", 1, 8, True, False),
    ))
    with pytest.raises(CoffError) as caught:
        link_executable([obj])
    message = str(caught.value)
    assert message.startswith("PE rel32 out of range: far_target from .text at rva 0x1000 to rva 0x1008 (.text)")
    assert "addend 2147483647 displacement 2147483651" in message
    assert "; sections .text@0x1000+" in message
