"""Production AArch64 metadata must describe the relaxed instruction layout.

The only synthetic code is a conditional branch over one data-in-code gap,
inserted after a real function prologue.  Tiny ordinary IR still goes through
verification, stack-map planning, function emission, peepholes, metadata
encoding and section assembly.  This avoids a quarter-million-instruction IR
fixture and never links or executes AArch64 code.

The oracle is the actual assembler result, not a second offset calculator.
Checking the inverted branch and added unconditional branch proves that the
fixture reached the changed shape before comparing metadata to final labels.
"""

from __future__ import annotations

import struct

import pytest

from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend.arm64_asm_driver import AArch64ModuleBuilder, assemble_file
from pcc.backend.arm64_elf_driver import from_sections as elf_from_sections
from pcc.backend.arm64_encode import EncodeError, PackedAArch64TextBuilder
from pcc.backend.elf_x86_64 import EM_AARCH64
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    NO_OFFSET,
    RECORD_HAS_EXCEPTION_EDGE,
    SAFEPOINT_CALL,
    SAFEPOINT_ENTRY,
    SAFEPOINT_EXCEPTION,
    decode_stack_map,
)
from pcc.backend.self_backend_parse import parse_self_backend_module


_PADDING = 1 << 20
_BRANCH = "L_layout_metadata_branch"
_RESUME = "L_layout_metadata_resume"
_IR = """
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @opaque_call(ptr)
declare i32 @py_err_occurred()

define i64 @layout_probe(ptr %obj) {
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  call void @opaque_call(ptr %obj)
  call void @pcc_gc_frame_leave(ptr %root)
  %error = call i32 @py_err_occurred()
  %failed = icmp ne i32 %error, 0
  br i1 %failed, label %error_exit, label %ok
error_exit:
  ret i64 -1
ok:
  ret i64 42
}

define i64 @after_probe() {
entry:
  ret i64 7
}
"""


_DEBUG_IR = (
    _IR.replace(
        "define i64 @layout_probe(ptr %obj) {",
        "define i64 @layout_probe(ptr %obj) !dbg !2 {",
    ).replace(
        "define i64 @after_probe() {",
        "define i64 @after_probe() !dbg !4 {",
    ).replace(
        "call void @opaque_call(ptr %obj)",
        "call void @opaque_call(ptr %obj), !dbg !3",
    ).replace("ret i64 7", "ret i64 7, !dbg !5")
    + """
!llvm.dbg.cu = !{!1}
!0 = !DIFile(filename: "layout.py", directory: "/fixture")
!1 = distinct !DICompileUnit(language: DW_LANG_Python, file: !0, producer: "pcc", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug)
!2 = distinct !DISubprogram(name: "layout_probe", file: !0, line: 1, unit: !1, scope: !0)
!3 = !DILocation(line: 4, column: 3, scope: !2)
!4 = distinct !DISubprogram(name: "after_probe", file: !0, line: 20, unit: !1, scope: !0)
!5 = !DILocation(line: 21, column: 3, scope: !4)
"""
)


@pytest.mark.parametrize("triple, source_debug", [
    pytest.param("arm64-apple-darwin", False, id="arm64-apple-darwin"),
    pytest.param("aarch64-unknown-linux-gnu", False, id="aarch64-unknown-linux-gnu"),
    pytest.param("arm64-apple-darwin", True, id="arm64-apple-darwin-debug"),
])
@pytest.mark.parametrize("route", [
    "native", "optimized-structured", "optimized-rendered",
])
@pytest.mark.parametrize("padding", [16, _PADDING], ids=["near", "far"])
def test_emitted_metadata_uses_final_relaxed_layout(
    monkeypatch, triple, source_debug, route, padding,
):
    monkeypatch.setenv("PCC_PRECISE_STACKMAP_TABLES", "1")
    monkeypatch.delenv("PCC_SELF_TARGET_PASS_TRANSPORT", raising=False)
    real_prologue = emitter._prologue_emit_function_prologue
    real_function = emitter._emit_function
    real_finish = PackedAArch64TextBuilder.finish
    plans = {}
    debug_plans = {}
    assembled_results = []

    def padded_prologue(func, module_symbols):
        lines = real_prologue(func, module_symbols)
        if func.name == "layout_probe":
            lines.extend([
                _BRANCH + ":",
                "  cbz x15, " + _RESUME,
                ".data_region",
                ".space " + str(padding),
                ".end_data_region",
                _RESUME + ":",
                "  nop",
            ])
        return lines

    def record_plan(
        func, plan, native_sink=None, *, debug_plan=None, phase_timing=None,
    ):
        assert (debug_plan is not None) == source_debug
        if debug_plan is not None:
            assert debug_plan.function is emitter.function_debug(func)
            assert debug_plan.begin == emitter._function_symbol(func.name)
            assert debug_plan.end == plan.end_label
            debug_plans[func.name] = debug_plan
        # Copy diagnostics before the production metadata writer consumes the
        # packed plan; do not replace or change any planned records.
        plans[plan.function_id] = (
            emitter._function_symbol(func.name),
            plan.end_label,
            tuple(
                (
                    record.safepoint_id,
                    record.label,
                    record.kind,
                    record.flags,
                    emitter._block_label(func.name, record.exceptional_block)
                    if record.exceptional_block else None,
                )
                for record in plan.diagnostic_records()
            ),
        )
        result = real_function(
            func, plan, native_sink=native_sink,
            debug_plan=debug_plan, phase_timing=phase_timing,
        )
        if debug_plan is not None:
            # Accepting the keyword is not enough: the real emitter must use
            # this same plan to record the fixture's source locations.
            assert any(location is not None for _label, location in debug_plan.rows)
        return result

    def record_assembled_text(builder):
        result = real_finish(builder)
        assembled_results.append(result)
        return result

    monkeypatch.setattr(emitter, "_prologue_emit_function_prologue", padded_prologue)
    monkeypatch.setattr(emitter, "_emit_function", record_plan)
    monkeypatch.setattr(PackedAArch64TextBuilder, "finish", record_assembled_text)
    module = parse_self_backend_module(
        'target triple = "' + triple + '"\n' + (_DEBUG_IR if source_debug else _IR),
    )
    if route == "optimized-rendered":
        assembly = emitter.emit_aarch64_darwin_indexed_module(module, optimize=True)
        sections, undefined = assemble_file(assembly)
    else:
        transport = emitter.emit_aarch64_darwin_indexed_transport(
            module, optimize=route != "native", structured_instructions=True,
        )
        try:
            # Source debug deliberately retains the unoptimized native sink.
            assert transport.native_finalized == (route == "native" or source_debug)
            if route == "native" or source_debug:
                assert transport.fallback_instruction_count == 0
                assert transport.fallback_instruction_lines == ()
            sections, undefined = transport.assemble_sections()
        finally:
            if transport.encoded_line_records is not None:
                transport.encoded_line_records.close()

    text = next(section for section in sections if section.sectname == "__text")
    assembled = assembled_results[-1]
    assert assembled.code == text.data
    labels = assembled.labels
    if source_debug:
        assert set(debug_plans) == {"layout_probe", "after_probe"}
        assert {"__debug_info", "__debug_line", "__debug_abbrev"} <= {
            section.sectname for section in sections
        }
        for plan in debug_plans.values():
            assert all(
                labels[plan.begin] <= labels[label] <= labels[plan.end]
                for label, _location in plan.rows
            )
    else:
        assert not debug_plans
    branch = labels[_BRANCH]
    resume = labels[_RESUME]
    # The far cbz grows into cbnz x15, +8 ; b resume.  The near control
    # remains one instruction.  The gap is always one host data chunk.
    skip = struct.unpack_from("<I", text.data, branch)[0]
    if padding == _PADDING:
        assert skip == 0xB500004F
        jump = struct.unpack_from("<I", text.data, branch + 4)[0]
        assert jump >> 26 == 0b000101
        displacement = jump & 0x03FFFFFF
        if displacement & 0x02000000:
            displacement -= 1 << 26
        assert branch + 4 + displacement * 4 == resume
        branch_size = 8
    else:
        assert skip == 0xB400000F | (((padding + 4) // 4) << 5)
        branch_size = 4
    assert resume - branch == padding + branch_size
    assert any(
        region.offset == branch + branch_size and region.length == padding
        for region in text.data_in_code
    )
    assert len(plans) == 2

    stackmaps = next(
        section for section in sections if section.sectname == "__pcc_stackmaps"
    )
    payload = stackmaps.data
    if "linux" in triple:
        # Use the production ELF adapter, including function-address RELA
        # conversion.  No Mach-O serialization or native execution is involved.
        assert not any(section.sectname == "__compact_unwind" for section in sections)
        elf = elf_from_sections(sections, undefined)
        assert elf.machine == EM_AARCH64
        elf_text = next(section for section in elf.sections if section.name == ".text")
        elf_maps = next(
            section for section in elf.sections if section.name == ".pcc_stackmaps"
        )
        assert elf_text.data == text.data
        assert elf_maps.data == payload
        assert len(elf_maps.relocations) == len(plans)
        assert all(relocation.type == 257 for relocation in elf_maps.relocations)
        assert {
            elf.symbols[relocation.symbol_index].name
            for relocation in elf_maps.relocations
        } == {plan[0] for plan in plans.values()}
        payload = elf_maps.data

    decoded = decode_stack_map(payload, expected_arch=ARCH_AARCH64)
    actual = {}
    expected = {}
    saw_exception = False
    for function in decoded.functions:
        symbol, end_label, records = plans[function.function_id]
        start = labels[symbol]
        assert {record[2] for record in records} <= {
            SAFEPOINT_ENTRY, SAFEPOINT_CALL, SAFEPOINT_EXCEPTION,
        }
        actual[symbol] = (
            function.code_size,
            {
                record.safepoint_id: (
                    record.instruction_offset, record.exceptional_offset,
                    record.kind, record.flags,
                )
                for record in function.records
            },
        )
        expected[symbol] = (
            labels[end_label] - start,
            {
                record_id: (
                    labels[label] - start,
                    labels[exceptional] - start if exceptional else NO_OFFSET,
                    kind, flags,
                )
                for record_id, label, kind, flags, exceptional in records
            },
        )
        for _record_id, _label, _kind, flags, exceptional in records:
            if exceptional:
                assert flags & RECORD_HAS_EXCEPTION_EDGE
                saw_exception = True
    assert saw_exception
    assert len(decoded.functions) == len(plans)
    rooted_calls = [
        record
        for function in decoded.functions
        for record in function.records
        if record.kind == SAFEPOINT_CALL
    ]
    assert len(rooted_calls) == 1
    assert rooted_calls[0].locations

    if "darwin" in triple:
        unwind = next(
            section for section in sections if section.sectname == "__compact_unwind"
        )
        assert len(unwind.data) == 32 * len(plans)
        starts = sorted((labels[plan[0]], plan[0]) for plan in plans.values())
        assert len(unwind.relocations) == len(starts)
        for index, (start, symbol) in enumerate(starts):
            relocation = unwind.relocations[index]
            assert relocation.offset == index * 32
            assert relocation.symbol == ""
            assert relocation.section == ("__TEXT", "__text")
            assert relocation.target_offset == start
            end = starts[index + 1][0] if index + 1 < len(starts) else len(text.data)
            actual["unwind:" + symbol] = struct.unpack_from("<I", unwind.data, index * 32 + 8)[0]
            expected["unwind:" + symbol] = end - start

    # Compare together so a failing baseline reports stale code_size,
    # safepoint/exception PCs and unwind lengths, rather than stopping at the
    # first field.  The unchanged test must fail before the final-layout fix.
    assert actual == expected, (actual, expected)


@pytest.mark.parametrize("padding", [16, _PADDING], ids=["near", "far"])
@pytest.mark.parametrize("structured", [False, True], ids=["text", "encoded"])
def test_finalized_text_layout_is_idempotent_and_immutable(padding, structured):
    builder = PackedAArch64TextBuilder(("Ldone",))
    try:
        builder.append_label("_probe")
        if structured:
            builder.append_branch(0xB400000F, 19, 0)
        else:
            builder.append_line("cbz x15, Ldone")
        builder.append_line(".data_region")
        builder.append_line(".space " + str(padding))
        builder.append_line(".end_data_region")
        builder.append_label("Ldone")
        builder.append_line("ret")
        builder.finalize_layout()
        branch_size = 8 if padding == _PADDING else 4
        assert builder.pc == padding + branch_size + 4
        assert builder.labels == {"_probe": 0, "Ldone": padding + branch_size}
        entries = builder.entries
        relocations = builder.structured_relocations
        labels = builder.labels
        regions = builder.data_in_code
        builder.finalize_layout()
        assert builder.entries is entries
        assert builder.structured_relocations is relocations
        assert builder.labels is labels
        assert builder.data_in_code is regions
        assert not builder.closed
        for append in (
            lambda: builder.append_label("Llate"),
            lambda: builder.append_line("nop"),
            lambda: builder.append_line(".space 4"),
            lambda: builder.append_encoded(0xD503201F, 0, -1),
            lambda: builder.append_branch(0x14000000, 26, 0),
            lambda: builder.append_call(0),
        ):
            with pytest.raises(EncodeError, match="finalized"):
                append()
        final_size = builder.pc
        result = builder.finish()
        assert len(result.code) == final_size
        assert result.labels == labels
        assert result.data_in_code == regions
    finally:
        builder.close()


def test_module_layout_freeze_still_allows_nontext_metadata():
    builder = AArch64ModuleBuilder()
    try:
        builder.append_chunk(".section __TEXT,__text,regular,pure_instructions")
        builder.append_label("_probe")
        builder.append_chunk("ret")
        builder.finalize_text_layout()
        builder.finalize_text_layout()
        assert builder.text_label_offsets() == {"_probe": 0}
        assert builder.text_size() == 4
        builder.append_chunk(".section __LD,__compact_unwind,regular,debug")
        builder.append_chunk(".p2align 3")
        for line in (".quad _probe", ".long 4", ".long 0x04000000", ".quad 0", ".quad 0"):
            builder.append_chunk(line)
        sections, undefined = builder.finish()
        assert undefined == []
        text, unwind = sections
        assert text.data == struct.pack("<I", 0xD65F03C0)
        assert struct.unpack_from("<I", unwind.data, 8)[0] == len(text.data)
        relocation = unwind.relocations[0]
        assert relocation.symbol == ""
        assert relocation.section == ("__TEXT", "__text")
        assert relocation.target_offset == 0
    finally:
        builder.close()
