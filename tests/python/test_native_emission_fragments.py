"""Native packed-stackmap fragments preserve the canonical assembler layout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.native_object import NativeObject, encode_native_object
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    SAFEPOINT_CALL,
    SAFEPOINT_LOOP,
    decode_stack_map,
)
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_asm,
    emit_aarch64_darwin_indexed_transport,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_precise_stackmaps import FunctionStackMapPlan


def _managed_reload_ir(frame_padding: int, derived_offset: int) -> str:
    padding = (
        f"  %padding = alloca [{frame_padding} x i8], align 16\n"
        "  call void @opaque_call(ptr %padding)\n"
        if frame_padding else ""
    )
    return f'''
target triple = "arm64-apple-darwin23.6.0"
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_thread_safepoint()
declare void @opaque_call(ptr)

define void @refresh(ptr %obj) {{
entry:
{padding}  %root = alloca ptr, align 8
  store ptr %obj, ptr %root, align 8
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  %before = load ptr, ptr %root, align 8
  %derived = getelementptr i8, ptr %before, i64 {derived_offset}
  %selected = getelementptr i8, ptr %derived, i64 0
  call void @pcc_thread_safepoint()
  call void @opaque_call(ptr %selected)
  call void @pcc_gc_frame_leave(ptr %root)
  ret void
}}
'''.strip()


def _forbid_packed_line_adapters(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("native fragment replay reached a packed line adapter")

    for name in (
        "append_packed_entry_lines",
        "append_packed_record_lines",
        "append_packed_terminator_lines",
        "_reload_asm_lines_packed",
    ):
        monkeypatch.setattr(FunctionStackMapPlan, name, forbidden)


def _assert_transport_matches_oracle(source: str, expected, tmp_path) -> None:
    transport = emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=False,
    )
    assert transport.native_finalized
    assert transport.native_fragment_record_count > 0
    assert transport.encoded_line_records is None
    assert transport.line_chunks == []
    actual = transport.assemble_sections()
    assert actual == expected
    actual_sections, actual_undefined = actual
    expected_sections, expected_undefined = expected
    actual_pco = encode_native_object(NativeObject.from_sections(
        actual_sections, undefined=actual_undefined,
    ))
    expected_pco = encode_native_object(NativeObject.from_sections(
        expected_sections, undefined=expected_undefined,
    ))
    assert actual_pco == expected_pco

    compiler_name = os.environ.get("PCC_INDEXED_EMIT_TEST_COMPILER")
    if compiler_name:
        from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file

        compiler = Path(compiler_name).resolve(strict=True)
        manifest = json.loads((compiler.parent / "source-manifest.json").read_text())
        repo = Path(__file__).resolve().parents[2]
        for relative in (
            "pcc/backend/self_backend_aarch64_fragments.py",
            "pcc/backend/self_backend_aarch64_darwin.py",
            "pcc/backend/self_backend_aarch64_darwin_regs.py",
            "pcc/backend/self_backend_aarch64_darwin_slots.py",
            "pcc/backend/self_backend_precise_stackmaps.py",
            "pcc/backend/arm64_encode.py",
            "pcc/backend/arm64_asm_driver.py",
            "pcc/frontends/python/pipeline_exports.py",
        ):
            assert manifest["files"][relative] == hashlib.sha256(
                (repo / relative).read_bytes()
            ).hexdigest(), "compiler is stale for " + relative
        source_path = tmp_path / "native_fragment_worker.pidx"
        output = tmp_path / "native_fragment_worker.pco"
        encode_indexed_module_file(str(source_path), parse_self_backend_module(source))
        result = subprocess.run(
            [str(compiler), "--pcc-self-backend-indexed-emit-worker",
             str(source_path), str(output), "PCO"],
            capture_output=True, text=True, timeout=30,
        )
        (tmp_path / "native_worker.stdout").write_text(result.stdout)
        (tmp_path / "native_worker.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_bytes() == expected_pco


@pytest.mark.parametrize(
    ("frame_padding", "derived_offset"),
    [(0, 0), (0, 24), (65536, -24), (65536, 0x12345678), (65536, -0x12345678)],
)
def test_native_stackmap_fragments_bypass_packed_line_adapters(
    monkeypatch, tmp_path, frame_padding, derived_offset,
):
    source = _managed_reload_ir(frame_padding, derived_offset)
    observed_reloads = []
    original = FunctionStackMapPlan._reload_asm_lines_packed

    def observe_reload(plan, records, record_index):
        span = records.span(record_index)
        for index in range(span.fourth):
            reload = records.reload_scalar(record_index, index)
            observed_reloads.append((reload.first, reload.second, reload.third))
        return original(plan, records, record_index)

    with monkeypatch.context() as oracle_patch:
        oracle_patch.setattr(FunctionStackMapPlan, "_reload_asm_lines_packed", observe_reload)
        assembly = emit_aarch64_darwin_asm(source, optimize=False)
    expected = assemble_file(assembly)
    # The changed producer must actually load a rewritten root, adjust its
    # derived pointer and store the SSA spill used after the safepoint.
    assert observed_reloads
    assert any(offset == derived_offset for _, _, offset in observed_reloads)
    assert all(source_offset != destination for source_offset, destination, _ in observed_reloads)
    if frame_padding:
        assert any(abs(source_offset) > 32760 for source_offset, _, _ in observed_reloads)
        assert any(abs(destination) > 32760 for _, destination, _ in observed_reloads)
    if abs(derived_offset) > 65535:
        assert "movk x15" in assembly
    _forbid_packed_line_adapters(monkeypatch)
    _assert_transport_matches_oracle(source, expected, tmp_path)


def test_native_terminator_fragment_keeps_nop_between_distinct_safepoints(monkeypatch, tmp_path):
    source = '''
target triple = "arm64-apple-darwin23.6.0"
declare void @opaque_call()
define void @probe(i1 %take_call, i64 %limit) {
entry:
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ 0, %latch ]
  %again = icmp slt i64 %index, %limit
  br i1 %again, label %dispatch, label %done
dispatch:
  br i1 %take_call, label %call, label %latch
call:
  call void @opaque_call()
  br label %latch
latch:
  br label %loop
done:
  ret void
}
'''.strip()
    expected = assemble_file(emit_aarch64_darwin_asm(source, optimize=False))
    stackmaps = next(
        section for section in expected[0]
        if (section.segname, section.sectname) == ("__DATA", "__pcc_stackmaps")
    )
    function = decode_stack_map(stackmaps.data, expected_arch=ARCH_AARCH64).functions[0]
    call = next(record for record in function.records if record.kind == SAFEPOINT_CALL)
    loop = next(record for record in function.records if record.kind == SAFEPOINT_LOOP)
    # Unoptimized emission retains the branch to the latch as well as the
    # separator. The NOP must be immediately before the loop's stackmap PC.
    assert loop.instruction_offset - call.instruction_offset == 8
    text = next(section for section in expected[0] if section.sectname == "__text")
    assert text.data[loop.instruction_offset - 4:loop.instruction_offset] == bytes.fromhex("1f2003d5")
    _forbid_packed_line_adapters(monkeypatch)
    _assert_transport_matches_oracle(source, expected, tmp_path)
