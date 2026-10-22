"""Raw AArch64 fixups are validated before relaxation consumes their words.

These are host encoder checks over a few arena entries and one data chunk.
They do not build pcc1, link an object, or execute the generated instructions.
"""

from __future__ import annotations

import struct

import pytest

from pcc.backend.arm64_encode import (
    EncodeError,
    PackedAArch64TextBuilder,
    STRUCTURED_FIXUP_BRANCH19,
    assemble_native_text_entries,
    assemble_text_lines,
)
from pcc.backend.macho_obj import DataInCodeRegion, Relocation
from pcc.backend.macho_spec import ARM64_RELOC_BRANCH26
from pcc.backend.self_backend_value_arena import CompilerIntArena


_FAR = 1 << 20
_RET = 0xD65F03C0


def _raw(word, padding, *, backward=False, kind=2, records=None, data_index=0):
    entries = CompilerIntArena()
    relocations = CompilerIntArena()
    branch_entry = 2 if backward else 0
    if records is None:
        records = [(branch_entry, STRUCTURED_FIXUP_BRANCH19, 0)]
    if backward:
        entries.append2(2, _RET)
        entries.append2(1, data_index)
        entries.append2(kind, word)
        labels = {"Ltarget": 0, "Lbranch": padding + 4}
    else:
        entries.append2(kind, word)
        entries.append2(1, data_index)
        labels = {"Lbranch": 0, "Ltarget": padding + 4}
    entries.append2(2, _RET)
    for entry, relocation_kind, symbol in records:
        relocations.append3(entry, relocation_kind, symbol)
    try:
        return assemble_native_text_entries(
            entries, [b"\0" * padding], relocations, ("Ltarget",), labels,
            [DataInCodeRegion(4, padding, 1)],
        )
    finally:
        # The production adapter owns both input arenas, including errors.
        assert entries._closed and relocations._closed
        assert len(entries) == len(relocations) == 0


def _expect_error(message, word, padding, **kwargs):
    try:
        assembled = _raw(word, padding, **kwargs)
    except EncodeError as exc:
        assert message in str(exc), str(exc)
        return
    at = assembled.labels["Lbranch"]
    words = struct.unpack_from("<II", assembled.code, at)
    raise AssertionError(
        "malformed raw input was accepted: branch words="
        + ",".join(hex(word) for word in words)
    )


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("word,kind,message", [
    (0xD503201F, 2, "structured fixup requires an unpatched branch word"),
    (0x54000020, 2, "structured fixup requires an unpatched branch word"),
    (0x34000020, 2, "structured fixup requires an unpatched branch word"),
    (-1, 2, "text word is outside u32 range"),
    ((1 << 32) | 0x54000000, 2, "text word is outside u32 range"),
    (0x34000000, 3, "text instruction index is outside its owner"),
    (0x34000000, 0, "text instruction index is outside its owner"),
], ids=["nop", "patched-condition", "patched-cbz", "negative-word",
        "wide-word", "unknown-kind", "text-kind"])
def test_raw_branch19_rejects_invalid_source_entries(word, kind, message, padding):
    _expect_error(message, word, padding, kind=kind)


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("shape", [
    "duplicate", "unordered", "trailing", "negative-entry", "data-entry",
    "negative-symbol", "missing-symbol", "unknown-fixup",
])
def test_raw_branch19_preserves_relocation_validation(shape, padding):
    branch = STRUCTURED_FIXUP_BRANCH19
    backward = shape == "unordered"
    records = [(0, branch, 0)]
    message = "structured relocation records are not ordered"
    if shape == "duplicate":
        records.append((0, branch, 0))
    elif shape == "unordered":
        records = [(2, branch, 0), (0, branch, 0)]
    elif shape == "trailing":
        records.append((99, branch, 0))
        message = "structured relocation exceeds text entries"
    elif shape == "negative-entry":
        records.insert(0, (-1, branch, 0))
    elif shape == "data-entry":
        records.append((1, branch, 0))
    elif shape == "negative-symbol":
        records = [(0, branch, -1)]
        message = "text relocation symbol is outside its owner"
    elif shape == "missing-symbol":
        records = [(0, branch, 1)]
        message = "text relocation symbol is outside its owner"
    elif shape == "unknown-fixup":
        records = [(0, -123, 0)]
        message = "structured fixup kind is not proven"
    _expect_error(message, 0x34000009, padding, backward=backward, records=records)


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("backward", [False, True], ids=["forward", "backward"])
@pytest.mark.parametrize("word,line,inverse", [
    (0x54000000, "b.eq Ltarget", 0x54000041),
    (0xB4000003, "cbz x3, Ltarget", 0xB5000043),
    (0x35000009, "cbnz w9, Ltarget", 0x34000049),
], ids=["condition", "cbz", "cbnz"])
def test_valid_raw_branch19_still_matches_text_relaxation(word, line, inverse, backward, padding):
    actual = _raw(word, padding, backward=backward)
    gap = [".data_region", ".space " + str(padding), ".end_data_region"]
    if backward:
        lines = ["Ltarget:", "ret"] + gap + ["Lbranch:", line, "ret"]
    else:
        lines = ["Lbranch:", line] + gap + ["Ltarget:", "ret"]
    expected = assemble_text_lines(lines)
    assert actual == expected
    at = actual.labels["Lbranch"]
    first = struct.unpack_from("<I", actual.code, at)[0]
    if padding == _FAR:
        assert first == inverse
        jump = struct.unpack_from("<I", actual.code, at + 4)[0]
        assert jump & 0xFC000000 == 0x14000000
        displacement = jump & 0x3FFFFFF
        if displacement & 0x2000000:
            displacement -= 1 << 26
        assert at + 4 + displacement * 4 == actual.labels["Ltarget"]
    else:
        assert first & 0xFF00001F == word
        assert len(actual.code) == padding + (12 if backward else 8)


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
def test_raw_none_relocation_keeps_existing_adapter_contract(padding):
    # The raw encoder accepts a NONE record with an in-range symbol; typed
    # append_encoded has a stricter symbol=-1 contract. Do not conflate them.
    actual = _raw(_RET, padding, records=[(0, 0, 0)])
    assert struct.unpack_from("<I", actual.code)[0] == _RET
    assert len(actual.code) == padding + 8
    assert actual.relocations == []


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("condition", [14, 15], ids=["al", "nv"])
def test_raw_unconditional_condition_codes_keep_original_range_contract(condition, padding):
    word = 0x54000000 | condition
    if padding == _FAR:
        _expect_error("out of range", word, padding)
    else:
        actual = _raw(word, padding)
        assert struct.unpack_from("<I", actual.code)[0] == word | (((padding + 4) // 4) << 5)


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("data_index", [-1, 1], ids=["negative", "missing"])
def test_raw_entries_reject_invalid_data_indices(data_index, padding):
    _expect_error("text data index is outside its owner", 0x34000000,
                  padding, data_index=data_index)


@pytest.mark.parametrize("padding", [1, _FAR + 1], ids=["near", "far"])
def test_raw_entries_reject_unaligned_instructions(padding):
    # No branch record is needed to exercise original-PC alignment checking.
    _expect_error("instruction at unaligned __text offset after data", _RET,
                  padding, records=[])


def _alignment_lines(line, padding, backward, residue):
    if backward:
        prefix = ["_target:"] if residue == 0 else [
            ".data_region", ".space " + str(residue), "_target:",
            ".space " + str(4 - residue), ".end_data_region",
        ]
        return prefix + [
            "ret", ".data_region", ".space " + str(padding),
            ".end_data_region", "_caller:", line, "ret",
        ]
    gap = [".data_region", ".space " + str(padding + residue)]
    if residue:
        gap += ["_target:", ".space " + str(4 - residue), ".end_data_region"]
    else:
        gap += [".end_data_region", "_target:"]
    return ["_caller:", line] + gap + ["ret"]


def _alignment_builder(lines, branch_line, word, encoded):
    builder = PackedAArch64TextBuilder(("_target",))
    try:
        for line in lines:
            if encoded and line == branch_line:
                builder.append_branch(word, 19, 0)
            elif encoded and line == "ret":
                builder.append_encoded(_RET, 0, -1)
            else:
                builder.append_line(line)
        return builder
    except BaseException:
        builder.close()
        raise


def _assemble_alignment(route, line, word, padding, backward, residue):
    lines = _alignment_lines(line, padding, backward, residue)
    if route == "text":
        return assemble_text_lines(lines)
    builder = _alignment_builder(lines, line, word, True)
    try:
        return assemble_native_text_entries(
            builder.entries, builder.data_chunks, builder.structured_relocations,
            builder.structured_symbol_names, builder.labels, builder.data_in_code,
        )
    finally:
        # The raw adapter owns these arenas, even when alignment is rejected.
        assert builder.entries._closed and builder.structured_relocations._closed
        builder.close()


@pytest.mark.parametrize("route", ["raw", "text"])
@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("backward", [False, True], ids=["forward", "backward"])
@pytest.mark.parametrize("residue", [1, 2, 3], ids=["mod1", "mod2", "mod3"])
@pytest.mark.parametrize("word,line", [
    (0x54000000, "b.eq _target"),
    (0xB4000003, "cbz x3, _target"),
    (0x35000009, "cbnz w9, _target"),
], ids=["condition", "cbz", "cbnz"])
def test_conditional_cross_atom_target_alignment(word, line, residue, backward, padding, route):
    # Data keeps each actual instruction aligned while its target label is not.
    try:
        actual = _assemble_alignment(route, line, word, padding, backward, residue)
    except EncodeError as exc:
        assert str(exc) == "misaligned branch target"
        return
    at = actual.labels["_caller"]
    words = struct.unpack_from("<II", actual.code, at)
    raise AssertionError(
        f"misaligned target {actual.labels['_target']} was accepted: "
        f"branch words={tuple(hex(word) for word in words)!r}, relocations={actual.relocations!r}"
    )


@pytest.mark.parametrize("padding", [16, _FAR], ids=["near", "far"])
@pytest.mark.parametrize("backward", [False, True], ids=["forward", "backward"])
@pytest.mark.parametrize("word,line,inverse", [
    (0x54000000, "b.eq _target", 0x54000041),
    (0xB4000003, "cbz x3, _target", 0xB5000043),
    (0x35000009, "cbnz w9, _target", 0x34000049),
], ids=["condition", "cbz", "cbnz"])
def test_aligned_cross_atom_conditional_relocations(word, line, inverse, backward, padding):
    actual = _assemble_alignment("raw", line, word, padding, backward, 0)
    assert actual == _assemble_alignment("text", line, word, padding, backward, 0)
    at = actual.labels["_caller"]
    assert at == (padding + 4 if backward else 0)
    first = struct.unpack_from("<I", actual.code, at)[0]
    if padding == _FAR:
        assert first == inverse
        assert struct.unpack_from("<I", actual.code, at + 4)[0] == 0x14000000
        assert actual.relocations == [
            Relocation(at + 4, "_target", ARM64_RELOC_BRANCH26, pcrel=True),
        ]
        assert actual.labels["_target"] == (0 if backward else padding + 8)
        assert actual.data_in_code == [DataInCodeRegion(4 if backward else 8, padding, 1)]
        assert len(actual.code) == padding + (16 if backward else 12)
    else:
        assert actual.relocations == []
        displacement = (first >> 5) & 0x7FFFF
        if displacement & 0x40000:
            displacement -= 1 << 19
        assert at + displacement * 4 == actual.labels["_target"]
        assert len(actual.code) == padding + (12 if backward else 8)
    assert actual.undefined == []


@pytest.mark.parametrize("encoded", [False, True], ids=["text", "encoded"])
def test_misaligned_layout_rejection_preserves_builder_state(encoded):
    line = "cbz x3, _target"
    lines = _alignment_lines(line, _FAR, False, 1)
    builder = _alignment_builder(lines, line, 0xB4000003, encoded)
    entries = builder.entries
    relocations = builder.structured_relocations
    original = (
        [entries.get_unchecked(i) for i in range(len(entries))],
        [relocations.get_unchecked(i) for i in range(len(relocations))],
        list(builder.instruction_lines), dict(builder.labels),
        list(builder.data_in_code), builder.pc,
    )
    try:
        with pytest.raises(EncodeError, match="^misaligned branch target$"):
            builder.finalize_layout()
        assert not builder.layout_finalized and not builder.closed
        assert builder.entries is entries and builder.structured_relocations is relocations
        assert not entries._closed and not relocations._closed
        assert original == (
            [entries.get_unchecked(i) for i in range(len(entries))],
            [relocations.get_unchecked(i) for i in range(len(relocations))],
            list(builder.instruction_lines), dict(builder.labels),
            list(builder.data_in_code), builder.pc,
        )
    finally:
        builder.close()
    assert entries._closed and relocations._closed
