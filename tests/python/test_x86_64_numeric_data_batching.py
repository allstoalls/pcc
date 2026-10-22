"""Numeric data in the single pass preserves the owned assembler contract.

Each numeric item is converted once and appended to its section's bounded
payload.  The frozen two-pass driver with its numeric batching disabled is
the original one-record-per-item parser and remains the byte oracle.
"""

import struct

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import elf_x86_64 as elf
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.precise_stackmap import (
    ARCH_X86_64,
    SAFEPOINT_ENTRY,
    FunctionStackMap,
    PreciseStackMap,
    SafepointRecord,
    encode_stack_map,
    function_id,
    render_stack_map_assembly,
    safepoint_id,
)
from pcc.backend.x86_64_encode import X86EncodeError


PREFIX = ".intel_syntax noprefix\n"


def _two_pass(batch_bytes):
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass as load

    oracle = load()
    oracle._NUMERIC_DATA_BATCH_BYTES = batch_bytes
    return oracle


def _per_item_object(monkeypatch, text, *, windows=False):
    # Grammar, conversions and final assembly remain real in the oracle.
    oracle = _two_pass(0)
    with monkeypatch.context() as context:
        if windows:
            context.setattr(assembler, "assemble_file_keeping_labels",
                            oracle.assemble_file_keeping_labels)
            return coff.assemble_object(text)
        return elf.emit_relocatable(oracle.assemble_file(text))


def _outcome(text, batch_bytes=None):
    # None runs the single pass; an integer runs the frozen two-pass driver
    # with that numeric batch bound (0 is its per-item record path).
    driver = assembler if batch_bytes is None else _two_pass(batch_bytes)
    try:
        return ("ok", elf.emit_relocatable(driver.assemble_file(text)))
    except Exception as exc:
        return (type(exc), str(exc))


def test_numeric_items_convert_once_into_bounded_payload_chunks(monkeypatch):
    assert assembler._SECTION_PAYLOAD_CHUNK_BYTES == 8192
    count = 4097
    text = PREFIX + ".data\n" + ".long 16909060\n" * count
    scalar_calls = []
    original = assembler._integer_payload

    def observed(value, width, *, owner):
        scalar_calls.append((value, width, owner))
        return original(value, width, owner=owner)

    monkeypatch.setattr(assembler, "_integer_payload", observed)
    sections, _, _, _ = assembler._encode_file(text, assembler._OperandParseCache())
    assert scalar_calls == [(16909060, 4, ".long")] * count
    payload = sections[".data"].payload
    assert [len(chunk) for chunk in payload.chunks] == [8192, 8192]
    assert payload.pending_size == 4
    assert payload.finish() == b"\x04\x03\x02\x01" * count


@pytest.mark.parametrize("limit", [0, -1, 1, 3, 8, 16, 8192])
def test_small_or_disabled_bounds_preserve_mixed_width_complete_object(monkeypatch, limit):
    text = PREFIX + """.data
.globl data
.type data, @object
data:
 .byte -128, 0, 255
 .short -32768, 65535
 .long -2147483648, 4294967295
 .quad -9223372036854775808, 18446744073709551615
 .float -0.0, 1.5
 .double -2.25, 0.0
.size data, .-data
.text
probe:
 ret
"""
    expected = _per_item_object(monkeypatch, text)
    monkeypatch.setattr(assembler, "_SECTION_PAYLOAD_CHUNK_BYTES", limit)
    actual = elf.emit_relocatable(assembler.assemble_file(text))
    assert actual == expected
    data = next(section for section in elf.parse_relocatable(actual).sections
                if section.name == ".data")
    assert data.data == (
        b"\x80\x00\xff" + struct.pack("<HHIIQQ", 32768, 65535, 2147483648,
                                      4294967295, 9223372036854775808,
                                      18446744073709551615)
        + struct.pack("<ffdd", -0.0, 1.5, -2.25, 0.0)
    )


@pytest.mark.parametrize("chunk_chars", [1, 65536])
def test_empty_lines_and_separators_preserve_same_object(monkeypatch, chunk_chars):
    lines = [".intel_syntax noprefix", ".data", ".byte 1, 2", ".short 3",
             ".long 4", ".quad 5", ".float 1.5", ".double 2.5"]
    ordinary = "\n".join(lines) + "\n"
    mixed = "\r\n\r\n".join(lines) + "\r\n\r\n"
    expected = elf.emit_relocatable(assembler.assemble_file(ordinary))
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", chunk_chars)
    assert elf.emit_relocatable(assembler.assemble_file(mixed)) == expected


def test_float_special_values_keep_original_packing(monkeypatch):
    text = PREFIX + ".data\n.float inf, -inf, nan\n.double inf, -inf, nan\n"
    expected = _per_item_object(monkeypatch, text)
    obj = assembler.assemble_file(text)
    assert elf.emit_relocatable(obj) == expected
    assert obj.sections[0].data == (
        struct.pack("<fffddd", float("inf"), -float("inf"), float("nan"),
                    float("inf"), -float("inf"), float("nan"))
    )


def test_symbol_label_alignment_zero_size_and_section_barriers(monkeypatch):
    text = PREFIX + """.data
.globl data
.type data, @object
data:
 .byte 1
 .short 770
pivot:
 .long 117835012
 .p2align 3, 170
 .quad external+8
 .byte 8
 .long pivot-data
 .byte 9
 .zero 2
 .byte 10
 .size data, .-data
.section .rodata,"a",@progbits
other:
 .byte 11, 12
.data
 .byte 13
.text
probe:
 ret
"""
    expected = _per_item_object(monkeypatch, text)
    obj = assembler.assemble_file(text)
    assert elf.emit_relocatable(obj) == expected
    assert [section.name for section in obj.sections] == [".data", ".rodata", ".text"]
    data = obj.sections[0]
    # Alignment fill, the relocated quad, the patched difference, the zero
    # fill and the bytes after the measured size all keep their places.
    assert data.data == (
        b"\x01\x02\x03\x04\x05\x06\x07\xaa" + bytes(8) + b"\x08"
        + struct.pack("<I", 3) + b"\x09" + bytes(2) + b"\x0a\x0d"
    )
    assert [(relocation.offset, relocation.addend) for relocation in data.relocations] == [(8, 8)]
    symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
    assert symbols["data"].size == 25
    assert obj.sections[1].data == b"\x0b\x0c"


@pytest.mark.parametrize("body", [
    ".data\n.byte 256,\n",
    ".data\n.byte 256, 1\n",
    ".data\n.byte -129\n",
    ".data\n.short 65536\n",
    ".data\n.long 4294967296\n",
    ".data\n.quad 18446744073709551616\n",
    ".data\n.float 1e39\n.bad\n",
    ".data\n.float not_a_float\n",
    ".data\n.double 1.0,\n",
    ".data\n.byte 1\n.long external\n",
    ".data\n.quad 1, bad symbol\n",
    ".data\n.long missing-other\n",
    '.section .tbss,"awT",@nobits\n.byte 0\n',
    '.section .tbss,"awT",@nobits\n.quad external\n',
    ".data\n.byte 1\n.p2align 17\n",
    ".data\nsame:\n.byte 1\nsame:\n.byte 2\n",
    ".data\n.byte 1\n.size absent, .-absent\n",
    ".data\n.byte 1\n.section .unknown\n",
    ".data\n.byte 1\n.space -1\n",
])
def test_first_error_type_and_message_match_per_item_records(body):
    text = PREFIX + body
    expected = _outcome(text, 0)
    assert expected[0] != "ok"
    assert _outcome(text, 8192) == expected
    assert _outcome(text) == expected


def test_whole_list_empty_item_check_still_precedes_scalar_range():
    text = PREFIX + ".data\n.byte 256,\n"
    expected = (X86EncodeError, "empty data item list '256,'")
    assert _outcome(text, 0) == expected
    assert _outcome(text) == expected


def test_nobits_validation_reports_the_first_failing_line():
    # The two-pass driver checked NOBITS contents only after the whole parse,
    # so a later directive error won; one pass reports the earlier line.
    text = PREFIX + '.section .tbss,"awT",@nobits\n.byte 0\n.bad\n'
    assert _outcome(text, 0) == (X86EncodeError, "directive not proven: '.bad'")
    assert _outcome(text) == (X86EncodeError, "NOBITS section '.tbss' has file data")
    valid_parse = PREFIX + '.section .tbss,"awT",@nobits\n.byte 0\n'
    assert _outcome(valid_parse) == (
        X86EncodeError, "NOBITS section '.tbss' has file data",
    )


def test_payloads_are_invocation_local_and_publish_immutable_bytes():
    first = assembler.assemble_file(PREFIX + ".data\n.byte 1, 2\n.short 3\n")
    before = first.sections[0].data
    with pytest.raises(X86EncodeError):
        assembler.assemble_file(PREFIX + ".data\n.byte 7\n.long invalid\n")
    second = assembler.assemble_file(PREFIX + ".data\n.byte 8, 9\n")
    assert first.sections[0].data is before
    assert before == b"\x01\x02\x03\x00"
    assert second.sections[0].data == b"\x08\x09"
    assert type(before) is bytes and type(second.sections[0].data) is bytes


def test_full_coff_bytes_keep_symbol_relocation_stackmap_and_unwind_sections(monkeypatch):
    text = PREFIX + """.text
.globl probe
.type probe, @function
.seh_proc probe
probe:
 push rbp
.seh_pushreg rbp
 mov rbp, rsp
.seh_setframe rbp, 0
.seh_endprologue
 mov rax, QWORD PTR data[rip]
 pop rbp
 ret
.seh_endproc
.size probe, .-probe
.data
data:
 .long 1, 2, 3
 .quad external+8
 .byte 4, 5
"""
    # A valid precise-map payload exercises the real owned object's schema
    # checks and symbolic function address, together with ordinary SEH output.
    # This is an object-format test; it does not execute a native safepoint.
    stack_map = PreciseStackMap(ARCH_X86_64, (FunctionStackMap(
        function_id("probe"), 0, 13, 0,
        (SafepointRecord(safepoint_id("probe", 0, SAFEPOINT_ENTRY),
                         0, SAFEPOINT_ENTRY, ()),),
    ),))
    text += render_stack_map_assembly(
        stack_map, ("probe",), target="x86_64-linux",
    ) + "\n"
    expected = _per_item_object(monkeypatch, text, windows=True)
    actual = coff.assemble_object(text)
    assert actual == expected
    obj = coff.parse_object(actual)
    assert {".text", ".data", ".pcc_stackmaps", ".pdata", ".xdata"} <= {
        section.name for section in obj.sections
    }
    assert all(section.data for section in obj.sections)
    maps = next(section for section in obj.sections if section.name == ".pcc_stackmaps")
    assert maps.data == encode_stack_map(stack_map)
    assert len(maps.relocations) == 1
    assert obj.symbols[maps.relocations[0].symbol].name == "probe"
