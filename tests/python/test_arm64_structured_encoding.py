"""Host/pcc-owned oracle gates for structured AArch64 instruction records."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_aarch64_darwin import (
    _emit_prepared_aarch64_darwin_lines,
    emit_aarch64_darwin_asm,
)

from pcc.backend.arm64_encode import (
    EncodeError,
    PackedAArch64TextBuilder,
    STRUCTURED_RELOCATION_BRANCH26,
    append_emitted_instruction_record,
    assemble_text,
    assemble_text_lines,
    assemble_native_text_entries,
    emitted_direct_call_target,
    encode_emitted_addsub_immediate_parts,
    encode_emitted_addsub_register_parts,
    encode_emitted_compare_immediate_parts,
    encode_emitted_compare_register_parts,
    encode_emitted_cset_parts,
    encode_emitted_load_store_parts,
    encode_emitted_move,
    encode_emitted_move_register_parts,
    encode_emitted_movewide_parts,
    encode_emitted_unscaled_load_store,
)
from pcc.backend.arm64_asm_driver import assemble_lines
from pcc.backend.self_backend_value_arena import CompilerIntArena
from pcc.backend.self_backend_aarch64_darwin_mem import (
    DIRECT_CALL_INSTRUCTION_PLACEHOLDER,
    DIRECT_INSTRUCTION_PLACEHOLDER,
    begin_direct_instruction_capture,
    borrow_direct_instruction_records,
    borrow_direct_instruction_symbol_names,
    emitted_direct_call_line,
    emitted_memory_instruction_line,
    emitted_move_register_line,
    emitted_movewide_instruction_line,
    end_direct_instruction_capture,
)


@pytest.mark.parametrize(
    "line",
    [
        "  stur x9, [x29, #-24]",
        "  stur w8, [x29, #-28]",
        "  stur xzr, [x29, #-16]",
        "  stur x9, [x10]",
        "  ldur x9, [x29, #-24]",
        "  ldur w8, [x29, #-28]",
        "  ldur x9, [x10]",
        "  sturb w8, [x29, #-1]",
        "  sturb wzr, [x29, #-2]",
        "  sturb w8, [x10]",
        "  ldurb w8, [x29, #-1]",
        "  ldurb w8, [x10]",
        "  stur d9, [x29, #-24]",
        "  ldur d9, [x29, #-24]",
        "  stur s8, [x29, #-20]",
        "  ldur s8, [x29, #-20]",
    ],
)
def test_emitted_unscaled_load_store_word_matches_text_oracle(line: str) -> None:
    word = encode_emitted_unscaled_load_store(line)
    assert word is not None
    assert word.to_bytes(4, "little") == assemble_text(line).code


@pytest.mark.parametrize(
    ("mnemonic", "register", "base", "offset"),
    [
        ("stur", "x9", "x29", -24),
        ("ldur", "w8", "x29", -28),
        ("sturb", "w8", "x29", -1),
        ("ldurb", "w8", "x10", 0),
        ("stur", "d9", "x29", -24),
        ("ldur", "s8", "x29", -20),
        ("str", "x9", "x10", 16),
        ("ldr", "w8", "x10", 12),
        ("strb", "w8", "x10", 3),
    ],
)
def test_emitted_load_store_parts_match_text_oracle(
    mnemonic: str,
    register: str,
    base: str,
    offset: int,
) -> None:
    suffix = "" if offset == 0 else f", #{offset}"
    line = f"  {mnemonic} {register}, [{base}{suffix}]"
    word = encode_emitted_load_store_parts(
        mnemonic,
        register,
        base,
        offset,
    )
    assert word.to_bytes(4, "little") == assemble_text(line).code


def test_native_text_entries_share_canonical_call_relocations() -> None:
    entries = CompilerIntArena()
    relocations = CompilerIntArena()
    entries.append2(2, encode_emitted_movewide_parts("movz", "x0", 42))
    entries.append2(2, 0x94000000)
    entries.append2(2, 0xD65F03C0)
    relocations.append3(1, STRUCTURED_RELOCATION_BRANCH26, 0)

    actual = assemble_native_text_entries(
        entries, [], relocations, ["_callee"], {"_probe": 0}, [],
    )
    expected = assemble_text("_probe:\n  movz x0, #42\n  bl _callee\n  ret\n")

    assert actual == expected


def test_native_text_builder_resolves_forward_fixups_from_final_layout() -> None:
    builder = PackedAArch64TextBuilder(
        ["L_after", "_first", "_second", "_external"],
    )
    builder.append_line("_first:")
    builder.append_branch(0x14000000, 26, 0)
    builder.append_line(".data_region jt8")
    builder.append_line(".byte 7")
    builder.append_line(".p2align 2")
    builder.append_line(".end_data_region")
    builder.append_line("L_after:")
    builder.append_call(1)  # recursive: resolve within the current atom
    builder.append_call(2)  # defined later, different atom: retain relocation
    builder.append_call(3)  # external: retain relocation and undefined name
    builder.append_encoded(0xD65F03C0, 0, -1)
    builder.append_line("_second:")
    builder.append_branch(0xB4000000, 19, 0)
    builder.append_encoded(0xD65F03C0, 0, -1)
    assert builder.instruction_lines == []
    assert builder.labels == {"_first": 0, "L_after": 8, "_second": 24}
    actual = builder.finish()
    expected = assemble_text(
        "_first:\n b L_after\n .data_region jt8\n .byte 7\n"
        " .p2align 2\n .end_data_region\n L_after:\n"
        " bl _first\n bl _second\n bl _external\n ret\n"
        "_second:\n cbz x0, L_after\n ret\n"
    )
    assert actual == expected
    assert builder.closed


@pytest.mark.parametrize("width", [19, 26])
@pytest.mark.parametrize("target", [None, 1, 1 << 28])
def test_native_text_builder_fixup_failure_closes_arenas(width, target) -> None:
    builder = PackedAArch64TextBuilder(["L_bad"])
    builder.append_branch(0x14000000 if width == 26 else 0xB4000000, width, 0)
    if target is not None:
        builder.labels["L_bad"] = target
    with pytest.raises(EncodeError, match="unknown label|misaligned|out of range"):
        builder.finish()
    assert builder.closed
    for arena in (builder.entries, builder.structured_relocations):
        with pytest.raises(RuntimeError, match="closed"):
            arena.diagnostic_values()


def test_structured_driver_resolves_symbol_fixups_after_text_section_reentry() -> None:
    source = [
        ".section __TEXT,__text,regular,pure_instructions", "_first:",
        "  b L_after", "  nop", "L_after:", "  bl _first",
        "  bl _second", "  bl _external", "  ret",
        ".section __DATA,__data", "_data:", "  .quad 7",
        ".section __TEXT,__text,regular,pure_instructions", "_second:",
        "  cbz x0, L_after", "  b.ne L_after", "  ret",
    ]
    lines = list(source)
    records = CompilerIntArena()
    symbol_ids, symbol_names = {}, []
    try:
        for index in (2, 3, 5, 6, 7, 8, 14, 15, 16):
            assert append_emitted_instruction_record(
                lines[index], index, 0, -1, None, records, symbol_ids, symbol_names,
            ) != 0
            lines[index] = ""
        assert assemble_lines(
            lines, encoded_line_records=records,
            structured_symbol_names=symbol_names,
        ) == assemble_lines(source)
    finally:
        records.close()


@pytest.mark.parametrize(
    "word,kind",
    [(0x94000001, -1), (0x14000001, -26), (0xB4000020, -19), (0xD503201F, -19)],
)
def test_native_text_fixups_reject_patched_or_non_branch_words(word, kind):
    builder = PackedAArch64TextBuilder(["L_target"])
    try:
        with pytest.raises(EncodeError, match="unpatched branch"):
            builder.append_encoded(word, kind, 0)
        assert builder.pc == 0
        assert len(builder.entries) == 0
    finally:
        builder.close()
    # Bypassing the builder must not bypass publication validation.
    entries, relocations = CompilerIntArena(), CompilerIntArena()
    entries.append2(2, word)
    relocations.append3(0, kind, 0)
    with pytest.raises(EncodeError, match="unpatched branch"):
        assemble_native_text_entries(
            entries, [], relocations, ["L_target"], {"L_target": 0}, [],
        )


@pytest.mark.parametrize("invalid", ["width", "word", "kind", "data", "symbol", "alignment"])
def test_native_text_entries_fail_closed_and_release_owned_arenas(invalid) -> None:
    entries = CompilerIntArena()
    relocations = CompilerIntArena()
    data = []
    if invalid == "width":
        entries.append(2)
    elif invalid == "word":
        entries.append2(2, -1)
    elif invalid == "kind":
        entries.append2(0, 0)
    elif invalid == "data":
        entries.append2(1, 0)
    elif invalid == "alignment":
        entries.append2(1, 0)
        entries.append2(2, 0xD503201F)
        data = [b"\0"]
    else:
        entries.append2(2, 0x94000000)
        relocations.append3(0, STRUCTURED_RELOCATION_BRANCH26, -1)

    with pytest.raises(EncodeError):
        assemble_native_text_entries(entries, data, relocations, [], {}, [])
    for arena in (entries, relocations):
        with pytest.raises(RuntimeError, match="closed"):
            arena.diagnostic_values()


def test_direct_memory_capture_keeps_owner_until_explicit_end() -> None:
    begin_direct_instruction_capture()
    first = emitted_memory_instruction_line("stur", "x9", "x29", -8)
    records = borrow_direct_instruction_records()
    second = emitted_memory_instruction_line("ldur", "x10", "x29", -8)
    third = emitted_move_register_line("x11", "x10")
    fourth = emitted_movewide_instruction_line("movz", "x12", 7)
    fifth = emitted_direct_call_line("_callee")
    symbols = borrow_direct_instruction_symbol_names()
    try:
        assert first == DIRECT_INSTRUCTION_PLACEHOLDER
        assert second == DIRECT_INSTRUCTION_PLACEHOLDER
        assert third == DIRECT_INSTRUCTION_PLACEHOLDER
        assert fourth == DIRECT_INSTRUCTION_PLACEHOLDER
        assert fifth == DIRECT_CALL_INSTRUCTION_PLACEHOLDER
        assert len(records) == 20
        assert symbols == ["_callee"]
    finally:
        records.close()
        end_direct_instruction_capture()


def test_direct_capture_exception_does_not_poison_later_asm() -> None:
    records = CompilerIntArena()
    try:
        with pytest.raises(BackendUnavailable, match="only supports AArch64 Darwin"):
            _emit_prepared_aarch64_darwin_lines(
                SimpleNamespace(triple="x86_64-linux-gnu"),
                optimize=False,
                encoded_line_records=records,
            )
        asm = emit_aarch64_darwin_asm(
            'target triple = "arm64-apple-darwin23.6.0"\n'
            'define i64 @probe() {\nentry:\n ret i64 7\n}\n',
            optimize=False,
        )
        assert DIRECT_INSTRUCTION_PLACEHOLDER not in asm
        assert DIRECT_CALL_INSTRUCTION_PLACEHOLDER not in asm
    finally:
        records.close()
        end_direct_instruction_capture()


def test_nested_capture_and_asm_reject_without_destroying_outer_owner() -> None:
    begin_direct_instruction_capture()
    try:
        outer = borrow_direct_instruction_records()
        emitted_move_register_line("x0", "x1")
        with pytest.raises(BackendUnavailable, match="already active"):
            begin_direct_instruction_capture()
        with pytest.raises(BackendUnavailable, match="already active"):
            _emit_prepared_aarch64_darwin_lines(SimpleNamespace(), optimize=False)
        assert borrow_direct_instruction_records() is outer
        assert len(outer) == 4
        emitted_move_register_line("x2", "x3")
        assert len(outer) == 8
    finally:
        end_direct_instruction_capture()


@pytest.mark.parametrize("outer_structured", [False, True])
def test_all_emission_modes_reserve_capture_scope(monkeypatch, outer_structured):
    from pcc.backend import self_backend_aarch64_darwin as emitter
    from pcc.backend.self_backend_parse import parse_self_backend_module

    source = (
        'target triple = "arm64-apple-darwin23.6.0"\n'
        'define i64 @probe() {\nentry:\n ret i64 7\n}\n'
    )
    original = emitter._emit_function
    nested_rejected = []

    def emit_with_nested_attempt(func, plan, **kwargs):
        with pytest.raises(BackendUnavailable, match="emission is already active"):
            emitter.emit_aarch64_darwin_indexed_transport(
                parse_self_backend_module(source), optimize=False,
            )
        nested_rejected.append(True)
        return original(func, plan, **kwargs)

    monkeypatch.setattr(emitter, "_emit_function", emit_with_nested_attempt)
    if outer_structured:
        result = emitter.emit_aarch64_darwin_indexed_transport(
            parse_self_backend_module(source), optimize=False,
        )
        assert result.native_finalized
        assert result.encoded_line_records is None
    else:
        result = emitter.emit_aarch64_darwin_asm(source, optimize=False)
        assert DIRECT_INSTRUCTION_PLACEHOLDER not in result
        assert DIRECT_CALL_INSTRUCTION_PLACEHOLDER not in result
    assert nested_rejected == [True]


@pytest.mark.parametrize("markers", [(False,), (True,), (True, True, False)])
def test_native_scope_replays_canonical_unbalanced_barrier_diagnostics(markers):
    from pcc.backend import self_backend_aarch64_darwin as emitter
    from pcc.backend.self_backend_target_passes import (
        AARCH64_MEMORY_PAIR_BARRIER_BEGIN as begin,
        AARCH64_MEMORY_PAIR_BARRIER_END as end,
        pair_adjacent_aarch64_64bit_memory_ops,
    )

    lines = [begin if marker else end for marker in markers]
    with pytest.raises(BackendUnavailable) as expected:
        pair_adjacent_aarch64_64bit_memory_ops(lines, enabled=False)
    begin_direct_instruction_capture()
    sink = emitter._NativeAArch64Emission()
    try:
        with pytest.raises(BackendUnavailable) as actual:
            sink.extend(lines)
            sink.release_captured_function()
        assert str(actual.value) == str(expected.value)
    finally:
        sink.close()
        end_direct_instruction_capture()


@pytest.mark.parametrize(
    "line",
    [
        "  ldur x0, [x1, #256]",
        "  ldur x0, [w1]",
        "  ldurb x0, [x1]",
        "  ldr x0, [x1]",
        "ldur x0, [x1]",
    ],
)
def test_unscaled_structured_encoder_defers_unowned_shapes_to_oracle(
    line: str,
) -> None:
    assert encode_emitted_unscaled_load_store(line) is None


@pytest.mark.parametrize(
    "line",
    [
        "  mov x3, x4",
        "  mov w5, w6",
        "  mov xzr, x0",
        "  mov wzr, w0",
        "  mov sp, x9",
        "  mov x9, sp",
        "  movz x9, #4660",
        "  movz x9, #4660, lsl #16",
        "  movz w8, #2, lsl #16",
        "  movk x9, #43981, lsl #32",
    ],
)
def test_emitted_move_word_matches_text_oracle(line: str) -> None:
    word = encode_emitted_move(line)
    assert word is not None
    assert word.to_bytes(4, "little") == assemble_text(line).code


def test_emitted_move_wide_rejects_negative_shift() -> None:
    with pytest.raises(EncodeError, match="negative.*shift"):
        encode_emitted_movewide_parts("movz", "x0", 1, -16)


@pytest.mark.parametrize(
    ("destination", "source"),
    [
        ("x3", "x4"),
        ("w5", "w6"),
        ("xzr", "x0"),
        ("wzr", "w0"),
        ("sp", "x9"),
        ("x9", "sp"),
    ],
)
def test_emitted_move_register_parts_match_text_oracle(
    destination: str,
    source: str,
) -> None:
    word = encode_emitted_move_register_parts(destination, source)
    assert word.to_bytes(4, "little") == assemble_text(
        f"  mov {destination}, {source}"
    ).code


@pytest.mark.parametrize(
    ("mnemonic", "destination", "immediate", "shift"),
    [
        ("movz", "x9", 0, 0),
        ("movz", "x9", 4660, 16),
        ("movz", "w8", 2, 16),
        ("movk", "x9", 43981, 32),
    ],
)
def test_emitted_movewide_parts_match_text_oracle(
    mnemonic: str,
    destination: str,
    immediate: int,
    shift: int,
) -> None:
    suffix = "" if shift == 0 else f", lsl #{shift}"
    word = encode_emitted_movewide_parts(
        mnemonic,
        destination,
        immediate,
        shift,
    )
    assert word.to_bytes(4, "little") == assemble_text(
        f"  {mnemonic} {destination}, #{immediate}{suffix}"
    ).code


@pytest.mark.parametrize(
    ("mnemonic", "destination", "left", "right"),
    [
        ("add", "x11", "x9", "x10"),
        ("sub", "w11", "w9", "w10"),
        ("add", "sp", "sp", "x15"),
    ],
)
def test_emitted_addsub_register_parts_match_text_oracle(
    mnemonic: str,
    destination: str,
    left: str,
    right: str,
) -> None:
    word = encode_emitted_addsub_register_parts(
        mnemonic,
        destination,
        left,
        right,
    )
    assert word.to_bytes(4, "little") == assemble_text(
        f"  {mnemonic} {destination}, {left}, {right}"
    ).code


@pytest.mark.parametrize(
    ("mnemonic", "destination", "left", "immediate"),
    [
        ("add", "x10", "x10", 8),
        ("sub", "sp", "sp", 64),
        ("sub", "x15", "x29", 256),
    ],
)
def test_emitted_addsub_immediate_parts_match_text_oracle(
    mnemonic: str,
    destination: str,
    left: str,
    immediate: int,
) -> None:
    word = encode_emitted_addsub_immediate_parts(
        mnemonic,
        destination,
        left,
        immediate,
    )
    assert word.to_bytes(4, "little") == assemble_text(
        f"  {mnemonic} {destination}, {left}, #{immediate}"
    ).code


@pytest.mark.parametrize(
    ("left", "right"),
    [("x9", "x10"), ("w11", "w12"), ("sp", "x15")],
)
def test_emitted_compare_register_parts_match_text_oracle(
    left: str,
    right: str,
) -> None:
    word = encode_emitted_compare_register_parts(left, right)
    assert word.to_bytes(4, "little") == assemble_text(
        f"  cmp {left}, {right}"
    ).code


@pytest.mark.parametrize(
    ("left", "immediate"),
    [("x12", 0), ("w9", 7), ("sp", 16)],
)
def test_emitted_compare_immediate_parts_match_text_oracle(
    left: str,
    immediate: int,
) -> None:
    word = encode_emitted_compare_immediate_parts(left, immediate)
    assert word.to_bytes(4, "little") == assemble_text(
        f"  cmp {left}, #{immediate}"
    ).code


@pytest.mark.parametrize(
    ("destination", "condition"),
    [("w11", "eq"), ("w12", "ne"), ("x9", "hi")],
)
def test_emitted_cset_parts_match_text_oracle(
    destination: str,
    condition: str,
) -> None:
    word = encode_emitted_cset_parts(destination, condition)
    assert word.to_bytes(4, "little") == assemble_text(
        f"  cset {destination}, {condition}"
    ).code


@pytest.mark.parametrize(
    "line",
    [
        "  mov x0, w1",
        "  mov d0, d1",
        "  movz x0, #-1",
        "  movz w0, #1, lsl #32",
        "  movk x0, #65536",
        "  add x0, x1, x2",
    ],
)
def test_move_structured_encoder_defers_unowned_shapes_to_oracle(
    line: str,
) -> None:
    assert encode_emitted_move(line) is None


def test_direct_call_target_parser_owns_only_exact_emitter_spelling() -> None:
    assert emitted_direct_call_target("  bl _external") == "_external"
    assert emitted_direct_call_target("  bl L_local") == "L_local"
    assert emitted_direct_call_target("bl _external") is None
    assert emitted_direct_call_target("  bl _external extra") is None
    assert emitted_direct_call_target("  bl _external@PAGE") is None


def test_compiler_word_arena_packs_one_little_endian_bytes_payload() -> None:
    words = CompilerIntArena()
    for value in (0, 0x12345678, 0xFFFFFFFF):
        words.append(value)

    assert words.pack_u32_bytes() == (
        b"\0\0\0\0\x78\x56\x34\x12\xff\xff\xff\xff"
    )
    words.close()


def test_preencoded_external_call_publishes_exact_branch_relocation() -> None:
    source = ["_entry:", "  bl _external", "  ret"]
    records = CompilerIntArena()
    records.append4(1, 0x94000000, STRUCTURED_RELOCATION_BRANCH26, 0)
    candidate = list(source)
    candidate[1] = ""

    assert assemble_text_lines(
        candidate,
        records,
        ("_external",),
    ) == assemble_text("\n".join(source))
    records.close()


def test_preencoded_text_word_keeps_label_offsets_and_bytes_exact() -> None:
    source = [
        "_entry:",
        "  ldur x9, [x29, #-24]",
        "  cbz x9, L_done",
        "L_done:",
        "  ret",
    ]
    word = encode_emitted_unscaled_load_store(source[1])
    assert word is not None
    records = CompilerIntArena()
    records.append4(1, word, 0, -1)
    candidate = list(source)
    candidate[1] = ""

    assert assemble_text_lines(candidate, records) == assemble_text(
        "\n".join(source)
    )
    records.close()


def test_module_driver_places_preencoded_word_in_text_section() -> None:
    source = [
        ".section __TEXT,__text,regular,pure_instructions",
        ".globl _entry",
        "_entry:",
        "  ldur x9, [x29, #-24]",
        "  ret",
    ]
    word = encode_emitted_unscaled_load_store(source[3])
    assert word is not None
    records = CompilerIntArena()
    records.append4(3, word, 0, -1)
    candidate = list(source)
    candidate[3] = ""

    candidate_sections, candidate_undefined = assemble_lines(
        candidate,
        encoded_line_records=records,
    )
    oracle_sections, oracle_undefined = assemble_lines(source)
    assert candidate_sections == oracle_sections
    assert candidate_undefined == oracle_undefined
    # Native text consumes original chunk coordinates without constructing or
    # remapping physical/text line-slot arrays. The caller's arena stays intact.
    assert records.get_unchecked(0) == 3
    records.close()


@pytest.mark.parametrize(
    "text_error",
    ["_probe:\n_probe:", ".data_region jt32\nret"],
)
def test_streaming_driver_preserves_driver_before_text_diagnostic_priority(text_error):
    lines = [
        ".section __TEXT,__text,regular,pure_instructions",
        text_error,
        ".bad",
    ]
    with pytest.raises(EncodeError, match="directive '.bad' not proven"):
        assemble_lines(lines)


def test_structured_driver_keeps_words_out_of_generic_line_slots(monkeypatch):
    observed = []
    original = PackedAArch64TextBuilder.finish

    def observe(builder):
        observed.append((len(builder.instruction_lines), len(builder.entries)))
        return original(builder)

    monkeypatch.setattr(PackedAArch64TextBuilder, "finish", observe)
    records = CompilerIntArena()
    records.append4(2, 0xD503201F, 0, -1)
    records.append4(3, 0xD65F03C0, 0, -1)
    try:
        sections, _ = assemble_lines([
            ".section __TEXT,__text,regular,pure_instructions", "_probe:", "", "",
        ], encoded_line_records=records)
        assert sections[0].data == bytes.fromhex("1f2003d5c0035fd6")
        assert observed == [(0, 4)]
        assert records.get_unchecked(0) == 2
        assert records.get_unchecked(4) == 3
    finally:
        records.close()


def test_common_emitter_vocabulary_publishes_only_scalar_records() -> None:
    source = [
        ".section __TEXT,__text,regular,pure_instructions",
        ".globl _entry",
        "_entry:",
        "  paciasp",
        "  stp x29, x30, [sp, #-16]!",
        "  sub sp, sp, #32",
        "  adrp x9, _external@GOTPAGE",
        "  ldr x9, [x9, _external@GOTPAGEOFF]",
        "  adrp x10, _local@PAGE",
        "  add x10, x10, _local@PAGEOFF",
        "  ldr x11, [x10]",
        "  str x11, [x10, #8]",
        "  ldrb w12, [x10]",
        "  strb w12, [sp]",
        "  add x11, x11, #1",
        "  sub x11, x11, x12",
        "  cmp x11, x12",
        "  cset w9, ne",
        "  cbz w9, L_done",
        "  and w9, w9, #0xff",
        "  asrv x11, x11, x12",
        "  b L_done",
        "  nop",
        "L_done:",
        "  add sp, sp, #32",
        "  ldp x29, x30, [sp], #16",
        "  autiasp",
        "  ret",
        ".section __DATA,__data",
        "_local:",
        "  .quad 0",
    ]
    labels = {"_entry": 0, "L_done": 80}
    records = CompilerIntArena()
    symbol_ids = {}
    symbol_names = []
    candidate = list(source)
    text_offset = 0
    current_atom_offset = -1
    in_text = False
    for line_index, line in enumerate(source):
        stripped = line.strip()
        if stripped.startswith(".section "):
            in_text = stripped[len(".section ") :].startswith("__TEXT,__text,")
            continue
        if not in_text or not stripped or stripped.startswith("."):
            continue
        if stripped.endswith(":"):
            if not stripped.startswith("L"):
                current_atom_offset = text_offset
            continue
        family = append_emitted_instruction_record(
            line,
            line_index,
            text_offset,
            current_atom_offset,
            labels,
            records,
            symbol_ids,
            symbol_names,
        )
        assert family != 0, line
        candidate[line_index] = ""
        text_offset += 4

    candidate_sections, candidate_undefined = assemble_lines(
        candidate,
        encoded_line_records=records,
        structured_symbol_names=symbol_names,
    )
    oracle_sections, oracle_undefined = assemble_lines(source)
    assert candidate_sections == oracle_sections
    assert candidate_undefined == oracle_undefined
    records.close()


@pytest.mark.parametrize(
    "line",
    [
        "  asr x13, x11, #63",
        "  blr x12",
        "  brk #0",
        "  csel x12, x10, x11, ne",
        "  mul x11, x9, x10",
        "  sdiv x11, x9, x10",
        "  smulh x12, x9, x10",
        "  msub x11, x11, x10, x9",
        "  fadd d11, d9, d10",
        "  fsub d11, d9, d10",
        "  fmul d11, d9, d10",
        "  fdiv d11, d9, d10",
        "  fneg d11, d9",
        "  fmov d10, x12",
        "  fmov x10, d9",
        "  fmov d10, #1.0",
        "  fmov d10, #2.0",
        "  scvtf d10, x9",
        "  fcvtzs x10, d9",
        "  fcmp d9, d10",
        "  fcmp d9, #0.0",
        "  fcsel d12, d10, d11, ne",
    ],
)
def test_rare_emitter_vocabulary_publishes_scalar_words(line: str) -> None:
    source = [
        ".section __TEXT,__text,regular,pure_instructions",
        ".globl _entry",
        "_entry:",
        line,
        "  ret",
    ]
    records = CompilerIntArena()
    symbols = []
    family = append_emitted_instruction_record(
        line,
        3,
        0,
        0,
        {"_entry": 0},
        records,
        {},
        symbols,
    )
    assert family != 0
    candidate = list(source)
    candidate[3] = ""
    candidate_sections, candidate_undefined = assemble_lines(
        candidate,
        encoded_line_records=records,
        structured_symbol_names=symbols,
    )
    oracle_sections, oracle_undefined = assemble_lines(source)
    assert candidate_sections == oracle_sections
    assert candidate_undefined == oracle_undefined
    records.close()


@pytest.mark.parametrize("count", [0, 1, 1023, 1024, 1025, 2048, 2057])
def test_compiler_word_pack_preserves_chunk_boundaries_and_input(count) -> None:
    values = [0, 1, 0x12345678, 0x80000000, 0xFFFFFFFF]
    words = CompilerIntArena()
    expected = bytearray()
    for index in range(count):
        value = values[index % len(values)]
        words.append(value)
        expected.extend(value.to_bytes(4, "little"))
    before = words.diagnostic_values()
    assert words.pack_u32_bytes() == bytes(expected)
    assert words.pack_u32_bytes() == bytes(expected)
    assert words.diagnostic_values() == before
    words.close()


@pytest.mark.parametrize("bad", [-1, 0x100000000])
def test_compiler_word_pack_rejects_invalid_tail_after_full_chunk(bad) -> None:
    words = CompilerIntArena()
    for _ in range(1024):
        words.append(0xFFFFFFFF)
    words.append(bad)
    with pytest.raises(ValueError, match="outside uint32"):
        words.pack_u32_bytes()
    assert len(words) == 1025 and words.get_unchecked(1024) == bad
    words.close()
    with pytest.raises(RuntimeError, match="closed"):
        words.pack_u32_bytes()


def test_compiler_word_pack_bounds_temporary_host_objects() -> None:
    import tracemalloc

    words = CompilerIntArena()
    count = 65536
    for _ in range(count):
        words.append(0xD503201F)
    tracemalloc.start()
    try:
        packed = words.pack_u32_bytes()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        words.close()
    assert packed == b"\x1f\x20\x03\xd5" * count
    # Input allocation precedes tracing. Packing holds compact chunks plus
    # final output and bounded 1024-word scratch, not one object per word
    # across the whole module. Leave ample fixed interpreter overhead.
    assert peak <= 3 * len(packed) + 512 * 1024
