"""Captured calls retain the exact return-PC boundary before result/reloads."""

import struct

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import arm64_asm_driver, arm64_elf_driver, target_objects
from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend import self_backend_aarch64_darwin_calls as calls
from pcc.backend import self_backend_aarch64_darwin_mem as capture
from pcc.backend.arm64_encode import (
    EMITTED_INSTRUCTION_CALL,
    EMITTED_INSTRUCTION_MOVE,
    EMITTED_INSTRUCTION_SCALAR,
)
from pcc.backend.elf_x86_64 import emit_relocatable, parse_relocatable
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    SAFEPOINT_CALL,
    SAFEPOINT_EXCEPTION,
    decode_stack_map,
    function_id,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_value_arena import CompilerIntArena


pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")
TARGET = "aarch64-unknown-linux-gnu"
CALL = capture.DIRECT_CALL_INSTRUCTION_PLACEHOLDER
WORD = capture.DIRECT_INSTRUCTION_PLACEHOLDER


@pytest.mark.parametrize("parts, expected", [
    ([], 0),
    ([WORD, "  ret"], 2),
    (["  bl callee", "  mov x1, x0"], 1),
    (["  blr x12", "  stur x0, [x29, #-8]"], 1),
    ([CALL, WORD], 1),
    ([CALL, "  add sp, sp, #16", WORD], 1),
    ([CALL, WORD, CALL, WORD], 3),
    ([CALL, "  blr x12", WORD], 2),
    (["  bl callee", WORD, CALL, WORD], 3),
])
def test_call_part_split_keeps_the_last_real_boundary(parts, expected):
    assert emitter._after_last_call_part(parts) == expected


def test_call_marker_preserves_records_and_capture_lifetime():
    assert not capture.direct_instruction_capture_active()
    assert capture.emitted_direct_call_line("callee") == "  bl callee"
    for _ in range(2):
        capture.begin_direct_instruction_capture()
        sink = None
        try:
            parts = [
                capture.emitted_move_register_line("x0", "x1"),
                capture.emitted_direct_call_line("callee"),
                capture.emitted_move_register_line("x2", "x0"),
            ]
            assert parts == [WORD, CALL, WORD]
            records = capture.borrow_direct_instruction_records()
            assert tuple(records.diagnostic_values()) == (
                0xAA0103E0, EMITTED_INSTRUCTION_MOVE, 0, -1,
                0, EMITTED_INSTRUCTION_CALL, 0, 0,
                0xAA0003E2, EMITTED_INSTRUCTION_MOVE, 0, -1,
            )
            assert capture.borrow_direct_instruction_symbol_names() == ["callee"]
            sink = emitter._NativeAArch64Emission()
            sink.append(".section __TEXT,__text,regular,pure_instructions")
            split = emitter._after_last_call_part(parts)
            sink.extend(parts[:split])
            sink.append("L_return_pc:")
            sink.extend(parts[split:])
            assert sink.builder.text_label_offsets()["L_return_pc"] == 8
            assert sink.direct_index == 3
            sink.release_captured_function()
            assert len(records) == 0 and sink.direct_index == 0
        finally:
            if sink is not None:
                sink.close()
            capture.end_direct_instruction_capture()
        assert not capture.direct_instruction_capture_active()
        assert capture.emitted_direct_call_line("callee") == "  bl callee"


def _source(indirect=False, stack_args=False):
    arg_types = "ptr" + (", i64" * 8 if stack_args else "")
    arguments = "ptr %derived" + (
        ", " + ", ".join("i64 " + str(i) for i in range(1, 9))
        if stack_args else ""
    )
    callee = "%callee" if indirect else "@opaque_result"
    declarations = f'''target triple = "{TARGET}"
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare i64 @opaque_result({arg_types})
declare void @consume(ptr, i64)
'''
    body = f'''
define i64 @NAME(ptr %obj, ptr %callee) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  %before = load ptr, ptr %root, align 8
  %derived = getelementptr i8, ptr %before, i64 24
  %result = call i64 {callee}({arguments})
  %sum = add i64 %result, 3
  call void @consume(ptr %derived, i64 %sum)
  call void @pcc_gc_frame_leave(ptr %root)
  ret i64 %sum
}}
'''
    # Consecutive functions also exercise the native per-function arena reset.
    return declarations + body.replace("NAME", "probe") + body.replace("NAME", "again")


def _encode_native(source):
    transport = emitter.emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=False,
        structured_instructions=True,
    )
    assert transport.native_finalized
    assert transport.line_chunks == [] and transport.encoded_line_records is None
    assert transport.fallback_instruction_count == 0
    sections, undefined = transport.assemble_sections()
    return emit_relocatable(arm64_elf_driver.from_sections(sections, undefined))


def _encode_buffered(source):
    # Exercise the second packed-record consumer, without the native sink.
    prepared = prepare_module_for_target(
        source,
        aggregate_returned_indirect=emitter._aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=emitter._aggregate_returned_indirect_indexed,
        materialize_legacy_slots=False,
    )
    records = CompilerIntArena()
    sections, names, counts = [], [], []
    try:
        lines = emitter._emit_prepared_aarch64_darwin_lines(
            prepared, optimize=False, structured_sections=sections,
            encoded_line_records=records, structured_symbol_names=names,
            structured_counts=counts,
        )
        assert counts[1] == 0
        sections, undefined = arm64_asm_driver.assemble_lines(
            lines, sections, records, names,
        )
        return emit_relocatable(arm64_elf_driver.from_sections(sections, undefined))
    finally:
        records.close()


@pytest.mark.parametrize("indirect", [False, True], ids=["bl", "blr"])
@pytest.mark.parametrize("stack_args", [False, True], ids=["register-args", "stack-args"])
@pytest.mark.parametrize("result_registers", [False, True], ids=["stur-result", "mov-result"])
def test_real_call_pc_precedes_result_commit_and_root_reload(
    monkeypatch, indirect, stack_args, result_registers,
):
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "0")
    monkeypatch.setenv("PCC_SELF_CALL_RESULT_REGISTERS", "1" if result_registers else "0")
    source = _source(indirect, stack_args)
    expected = target_objects.encode_assembly_object(
        target_objects.emit_indexed_assembly(
            parse_self_backend_module(source), optimize=False,
        ),
        TARGET,
    )
    actual = _encode_native(source)
    assert actual == expected
    assert _encode_buffered(source) == expected

    obj = parse_relocatable(actual)
    text = next(section for section in obj.sections if section.name == ".text")
    stackmaps = decode_stack_map(
        next(section.data for section in obj.sections if section.name == ".pcc_stackmaps"),
        expected_arch=ARCH_AARCH64,
    )
    symbols = {symbol.name: symbol for symbol in obj.symbols}
    functions = {function.function_id: function for function in stackmaps.functions}
    if indirect:
        call_pcs = {
            offset + 4 for offset in range(0, len(text.data), 4)
            if struct.unpack_from("<I", text.data, offset)[0] == 0xD63F0180
        }
    else:
        call_pcs = {
            relocation.offset + 4 for relocation in text.relocations
            if relocation.type == 283
            and obj.symbols[relocation.symbol_index].name == "opaque_result"
        }
    assert len(call_pcs) == 2
    seen = set()
    for name in ("probe", "again"):
        function = functions[function_id(name)]
        start = symbols[name].value
        for record in function.records:
            if record.kind not in (SAFEPOINT_CALL, SAFEPOINT_EXCEPTION):
                continue
            pc = start + record.instruction_offset
            word = struct.unpack_from("<I", text.data, pc - 4)[0]
            assert word & 0xFC000000 == 0x94000000 or word & 0xFFFFFC1F == 0xD63F0000
            if pc not in call_pcs:
                continue
            assert record.locations
            seen.add(pc)
            commit = pc
            if stack_args:
                # ADD sp, sp, #16 restores the outgoing ninth argument slot.
                assert struct.unpack_from("<I", text.data, commit)[0] == 0x910043FF
                commit += 4
            result_word = struct.unpack_from("<I", text.data, commit)[0]
            if result_registers:
                assert result_word & 0xFFFFFFE0 == 0xAA0003E0  # MOV Xd, X0
            else:
                assert result_word & 0xFFE00FFF == 0xF80003A0  # STUR X0, [FP, #slot]
            # The live derived pointer is refreshed only after X0 is committed.
            reload_word = struct.unpack_from("<I", text.data, commit + 4)[0]
            assert reload_word & 0xFFE00FFF == 0xF84003B0  # LDUR X16, [FP, #root]
    assert seen == call_pcs


@pytest.mark.parametrize("encode", [_encode_native, _encode_buffered], ids=["native", "buffered"])
@pytest.mark.parametrize("wrong_call_marker", [False, True], ids=["call-as-word", "word-as-call"])
def test_both_consumers_reject_marker_family_mismatch(
    monkeypatch, encode, wrong_call_marker,
):
    def wrong_call(_target):
        records = capture.borrow_direct_instruction_records()
        if wrong_call_marker:
            records.append4(0xD503201F, EMITTED_INSTRUCTION_SCALAR, 0, -1)
            return CALL
        marker = capture.emitted_direct_call_line(_target)
        assert marker == CALL
        return WORD

    monkeypatch.setattr(calls, "emitted_direct_call_line", wrong_call)
    with pytest.raises(BackendUnavailable, match="direct call marker does not match record family"):
        encode(_source())
    assert not capture.direct_instruction_capture_active()
    assert capture.emitted_direct_call_line("after_failure") == "  bl after_failure"


@pytest.mark.parametrize("trailing", [False, True], ids=["truncated", "trailing"])
def test_native_marker_keeps_record_count_checks(trailing):
    capture.begin_direct_instruction_capture()
    sink = None
    try:
        sink = emitter._NativeAArch64Emission()
        sink.append(".section __TEXT,__text,regular,pure_instructions")
        if trailing:
            capture.emitted_direct_call_line("callee")
            with pytest.raises(BackendUnavailable, match="trailing records"):
                sink.release_captured_function()
        else:
            with pytest.raises(BackendUnavailable, match="transport is truncated"):
                sink.append(CALL)
    finally:
        if sink is not None:
            sink.close()
        capture.end_direct_instruction_capture()


@pytest.mark.parametrize("trailing", [False, True], ids=["truncated", "trailing"])
def test_buffered_marker_keeps_record_count_checks(monkeypatch, trailing):
    def emitted_globals(_globals, _symbols):
        if trailing:
            capture.emitted_direct_call_line("callee")
            return [".section __TEXT,__text,regular,pure_instructions"]
        return [".section __TEXT,__text,regular,pure_instructions", CALL]

    monkeypatch.setattr(emitter, "_emit_globals", emitted_globals)
    message = "trailing records" if trailing else "transport is truncated"
    with pytest.raises(BackendUnavailable, match=message):
        _encode_buffered(f'target triple = "{TARGET}"\n')
    assert not capture.direct_instruction_capture_active()
