"""AArch64 conditional branches beyond +/-1 MiB are relaxed, not refused.

b.cond/cbz/cbnz carry a 19-bit word displacement.  Producers used to keep
targets near by hand (an 8192-IR-instruction cap on direct branch targets,
local edge labels, a line limit in a peephole); inline error edges reach
cold stubs after the function's blocks, so Stage1 failed in
``self_backend_parse._parse_instruction`` with "branch to '...edgedot1' out
of range".  The text finalizer now rewrites such a branch as the opposite
condition over one ``b`` (+/-128 MiB) and shifts every later label.
"""
from __future__ import annotations

import struct

from pcc.backend.arm64_encode import (
    PackedAArch64TextBuilder,
    assemble_text_lines,
)

_FAR = 300_000  # instructions: 1.2 MB, beyond a conditional branch's reach


def _words(code: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(code) // 4}I", code))


def _imm19_target(word: int, at: int) -> int:
    imm = (word >> 5) & 0x7FFFF
    if imm & 0x40000:
        imm -= 1 << 19
    return at + imm * 4


def _imm26_target(word: int, at: int) -> int:
    imm = word & 0x3FFFFFF
    if imm & 0x2000000:
        imm -= 1 << 26
    return at + imm * 4


def test_far_text_branches_become_inverted_skip_plus_b():
    lines = ["Lback:", "cbz x3, Lfar", "b.ne Lfar", "cbnz w4, Lnear", "Lnear:"]
    lines += ["nop"] * _FAR
    lines += ["Lfar:", "b.eq Lback", "ret"]
    assembled = assemble_text_lines(lines)
    words = _words(assembled.code)
    far = assembled.labels["Lfar"]
    # cbz x3 -> cbnz x3, +8 ; b Lfar
    assert words[0] == 0xB5000000 | (2 << 5) | 3
    assert _imm26_target(words[1], 4) == far and words[1] >> 26 == 0b000101
    # b.ne -> b.eq, +8 ; b Lfar
    assert words[2] == 0x54000000 | (2 << 5) | 0
    assert _imm26_target(words[3], 12) == far
    # In range: unchanged single cbnz to the next instruction.
    assert words[4] == 0x35000000 | (1 << 5) | 4
    assert assembled.labels["Lnear"] == 20
    assert far == 20 + 4 * _FAR
    # Backward far b.eq -> b.ne, +8 ; b Lback
    assert words[far // 4] == 0x54000000 | (2 << 5) | 1
    assert _imm26_target(words[far // 4 + 1], far + 4) == 0
    assert words[far // 4 + 2] == 0xD65F03C0
    assert len(words) == far // 4 + 3


def test_in_range_text_is_unchanged():
    lines = ["cbz x3, Lend"] + ["nop"] * 1000 + ["Lend:", "ret"]
    assembled = assemble_text_lines(lines)
    words = _words(assembled.code)
    assert len(words) == 1002
    assert _imm19_target(words[0], 0) == assembled.labels["Lend"] == 4004


def test_structured_branch19_fixups_relax_and_shift_data_regions():
    builder = PackedAArch64TextBuilder(("Lfar",), 16)
    builder.append_branch(0x54000000 | 0xB, 19, 0)  # b.lt Lfar
    builder.append_branch(0xB4000000 | 7, 19, 0)  # cbz x7, Lfar
    builder.append_line(".data_region")
    builder.append_line(".long 0x11223344")
    builder.append_line(".end_data_region")
    for _ in range(_FAR):
        builder.append_line("nop")
    builder.append_label("Lfar")
    builder.append_line("ret")
    assembled = builder.finish()
    words = _words(assembled.code)
    far = assembled.labels["Lfar"]
    assert words[0] == 0x54000000 | (2 << 5) | 0xA  # b.ge over the b
    assert _imm26_target(words[1], 4) == far
    assert words[2] == 0xB5000000 | (2 << 5) | 7  # cbnz x7 over the b
    assert _imm26_target(words[3], 12) == far
    assert words[4] == 0x11223344
    (region,) = assembled.data_in_code
    assert region.offset == 16 and region.length == 4
    assert far == 20 + 4 * _FAR
    assert not assembled.relocations
