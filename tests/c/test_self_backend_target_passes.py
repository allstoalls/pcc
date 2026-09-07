from __future__ import annotations

import re

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_target_passes import (
    AARCH64_MEMORY_PAIR_BARRIER_BEGIN,
    AARCH64_MEMORY_PAIR_BARRIER_END,
    pair_adjacent_aarch64_64bit_memory_ops,
    resolve_self_target_pass_names,
    resolve_self_target_pass_transport,
    run_self_target_pass_pipeline,
)


def test_self_target_passes_default_off():
    assert resolve_self_target_pass_names("") == ()
    assert resolve_self_target_pass_names("off") == ()
    assert resolve_self_target_pass_names("default") == ()


def test_self_target_pass_transport_text_default():
    assert resolve_self_target_pass_transport("") == "text"
    assert resolve_self_target_pass_transport("text") == "text"
    assert resolve_self_target_pass_transport("memory") == "memory"


def test_self_target_pass_strips_trailing_whitespace():
    asm = "one   \n  two\t\nthree\n"

    out = run_self_target_pass_pipeline(
        asm,
        "self-aarch64-darwin-v0",
        raw_passes="strip-trailing-whitespace",
        raw_transport="text",
    )

    assert out == "one\n  two\nthree\n"


def test_aarch64_target_pass_pairs_adjacent_64bit_loads_and_stores():
    lines = [
        "L_load:",
        "  ldr x10, [x9]",
        "  ldr x11, [x9, #8]",
        "  str x12, [sp, #16]",
        "  str x13, [sp, #24]",
    ]
    assert pair_adjacent_aarch64_64bit_memory_ops(lines) == [
        "L_load:",
        "  ldp x10, x11, [x9]",
        "  stp x12, x13, [sp, #16]",
    ]
    assert pair_adjacent_aarch64_64bit_memory_ops(lines, enabled=False) == lines


def test_aarch64_target_pass_rejects_aliasing_and_unencodable_pairs():
    controls = (
        # Different textual bases may alias at runtime; no speculation.
        ["  ldr x10, [x9]", "  ldr x11, [x12, #8]"],
        # The first scalar load would replace the base used by the second.
        ["  ldr x9, [x9]", "  ldr x10, [x9, #8]"],
        # Preserve source access order instead of reversing register operands.
        ["  str x10, [x9, #8]", "  str x11, [x9]"],
        # The signed scaled pair immediate cannot encode this first offset.
        ["  ldr x10, [x9, #512]", "  ldr x11, [x9, #520]"],
        # Only 64-bit integer/pointer registers and aligned offsets qualify.
        ["  ldr w10, [x9]", "  ldr w11, [x9, #4]"],
        ["  ldr q0, [x9]", "  ldr q1, [x9, #16]"],
    )
    for lines in controls:
        assert pair_adjacent_aarch64_64bit_memory_ops(lines) == lines


def test_aarch64_target_pass_treats_atomic_markers_and_exclusive_region_as_barriers():
    relaxed_atomic_lines = [
        AARCH64_MEMORY_PAIR_BARRIER_BEGIN,
        "  ldr x10, [x9]",
        "  ldr x11, [x9, #8]",
        AARCH64_MEMORY_PAIR_BARRIER_END,
    ]
    assert pair_adjacent_aarch64_64bit_memory_ops(relaxed_atomic_lines) == [
        "  ldr x10, [x9]",
        "  ldr x11, [x9, #8]",
    ]

    exclusive_lines = [
        "Lat_retry:",
        "  ldaxr x12, [x9]",
        "  ldr x10, [x14]",
        "  ldr x11, [x14, #8]",
        "  stlxr w13, x12, [x9]",
        "  cbnz w13, Lat_retry",
    ]
    assert pair_adjacent_aarch64_64bit_memory_ops(exclusive_lines) == exclusive_lines

    fenced_lines = [
        "  ldr x10, [x9]",
        "  dmb ish",
        "  ldr x11, [x9, #8]",
    ]
    assert pair_adjacent_aarch64_64bit_memory_ops(fenced_lines) == fenced_lines


def test_emit_self_asm_pairs_normal_aggregate_memory_but_not_volatile_memory():
    ir_text = """
target triple = "arm64-apple-darwin23.6.0"

%Pair = type { i64, ptr }

define %Pair @load_pair(ptr %source) {
entry:
  %value = load %Pair, ptr %source, align 8
  ret %Pair %value
}

define void @store_pair(ptr %dest, %Pair %value) {
entry:
  store %Pair %value, ptr %dest, align 8
  ret void
}

define %Pair @load_pair_volatile(ptr %source) {
entry:
  %value = load volatile %Pair, ptr %source, align 8
  ret %Pair %value
}

define void @store_pair_volatile(ptr %dest, %Pair %value) {
entry:
  store volatile %Pair %value, ptr %dest, align 8
  ret void
}
""".strip()

    asm_text = emit_self_asm(ir_text)

    assert "  ldp x10, x11, [x9]" in asm_text
    assert "  stp x10, x11, [x9]" in asm_text
    volatile_load = asm_text.split("_load_pair_volatile:", 1)[1].split(
        "_store_pair_volatile:", 1
    )[0]
    volatile_store = asm_text.split("_store_pair_volatile:", 1)[1]
    assert "  ldr x10, [x9]\n  ldr x11, [x9, #8]" in volatile_load
    assert "  ldp x10, x11, [x9]" not in volatile_load
    assert "  str x10, [x9]\n  str x11, [x9, #8]" in volatile_store
    assert "  stp x10, x11, [x9]" not in volatile_store
    assert ".pcc_memory_pair_barrier" not in asm_text


def test_emit_self_asm_fuses_only_proven_i64_madd_msub_shapes():
    ir_text = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @fuse_madd(i64 %a, i64 %b, i64 %c) {
entry:
  %factor = add i64 %a, 1
  %product = mul i64 %factor, %b
  %accumulator = xor i64 %c, 7
  %result = add i64 %product, %accumulator
  ret i64 %result
}

define i64 @fuse_msub(i64 %a, i64 %b, i64 %c) {
entry:
  %product = mul i64 %a, %b
  %result = sub i64 %c, %product
  ret i64 %result
}

define i64 @keep_multi_use(i64 %a, i64 %b, i64 %c) {
entry:
  %product = mul i64 %a, %b
  %sum = add i64 %product, %c
  %result = xor i64 %sum, %product
  ret i64 %result
}

define i64 @keep_live_flags(i64 %a, i64 %b, i64 %c) {
entry:
  %product = mul nsw i64 %a, %b
  %result = add nsw i64 %product, %c
  ret i64 %result
}

define i64 @keep_atomic_barrier(i64 %a, i64 %b, i64 %c) {
entry:
  %product = mul i64 %a, %b
  fence seq_cst
  %result = add i64 %product, %c
  ret i64 %result
}
""".strip()

    asm_text = emit_self_asm(ir_text)
    madd_body = asm_text.split("_fuse_madd:", 1)[1].split("_fuse_msub:", 1)[0]
    msub_body = asm_text.split("_fuse_msub:", 1)[1].split(
        "_keep_multi_use:", 1
    )[0]
    multi_use_body = asm_text.split("_keep_multi_use:", 1)[1].split(
        "_keep_live_flags:", 1
    )[0]
    live_flags_body = asm_text.split("_keep_live_flags:", 1)[1].split(
        "_keep_atomic_barrier:", 1
    )[0]
    atomic_body = asm_text.split("_keep_atomic_barrier:", 1)[1]

    assert "  madd x11, x9, x10, x12" in madd_body
    assert "  mul x11, x9, x10" not in madd_body
    assert "  msub x11, x9, x10, x12" in msub_body
    assert "  mul x11, x9, x10" not in msub_body

    assert "  madd " not in multi_use_body
    assert "  msub " not in multi_use_body
    assert re.search(r"  mul x\d+, x\d+, x\d+", multi_use_body)
    assert re.search(r"  add x\d+, x\d+, x\d+", multi_use_body)

    assert "  madd " not in live_flags_body
    assert "  msub " not in live_flags_body
    assert re.search(r"  mul x\d+, x\d+, x\d+", live_flags_body)
    assert re.search(r"  add x\d+, x\d+, x\d+", live_flags_body)

    assert "  madd " not in atomic_body
    assert "  msub " not in atomic_body
    assert re.search(r"  mul x\d+, x\d+, x\d+", atomic_body)
    assert "  dmb ish" in atomic_body


def test_self_target_pass_memory_transport_runs_before_asm_text():
    assert (
        run_self_target_pass_pipeline(
            "ret   \n",
            "self-aarch64-darwin-v0",
            raw_passes="all",
            raw_transport="memory",
        )
        == "ret   \n"
    )
    assert resolve_self_target_pass_names("all", transport="memory") == (
        "verify-prepared-module",
    )


@pytest.mark.parametrize("size", (32, 64, 128))
def test_aarch64_aligned_fixed_block_copy_uses_q_registers(
    monkeypatch, size: int
) -> None:
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES", raising=False)
    ir_text = f'''
target triple = "arm64-apple-darwin23.6.0"

declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i1)

define void @copy(ptr %dst, ptr %src) {{
entry:
  call void @llvm.memcpy.p0.p0.i64(ptr align 16 %dst, ptr align 16 %src, i64 {size}, i1 false)
  ret void
}}
'''.strip()

    asm_text = emit_self_asm(ir_text)

    assert "  bl _memcpy" not in asm_text
    assert asm_text.count("  ldr q0, [x10") == size // 16
    assert asm_text.count("  str q0, [x9") == size // 16


@pytest.mark.parametrize("size", (32, 64, 128))
def test_aarch64_aligned_fixed_block_zero_uses_q_registers(
    monkeypatch, size: int
) -> None:
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES", raising=False)
    ir_text = f'''
target triple = "arm64-apple-darwin23.6.0"

declare void @llvm.memset.p0.i64(ptr, i8, i64, i1)

define void @zero(ptr %dst) {{
entry:
  call void @llvm.memset.p0.i64(ptr align 16 %dst, i8 0, i64 {size}, i1 false)
  ret void
}}
'''.strip()

    asm_text = emit_self_asm(ir_text)

    assert "  bl _memset" not in asm_text
    assert asm_text.count("  movi v0.16b, #0") == 1
    assert asm_text.count("  str q0, [x9") == size // 16


@pytest.mark.parametrize(
    ("dst_alignment", "src_alignment", "size", "is_volatile"),
    (
        (8, 16, 32, "false"),
        (16, 8, 32, "false"),
        (16, 16, 48, "false"),
        (16, 16, 32, "true"),
    ),
)
def test_aarch64_block_copy_controls_stay_on_existing_fallback(
    monkeypatch,
    dst_alignment: int,
    src_alignment: int,
    size: int,
    is_volatile: str,
) -> None:
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES", raising=False)
    ir_text = f'''
target triple = "arm64-apple-darwin23.6.0"

declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i1)

define void @copy(ptr %dst, ptr %src) {{
entry:
  call void @llvm.memcpy.p0.p0.i64(ptr align {dst_alignment} %dst, ptr align {src_alignment} %src, i64 {size}, i1 {is_volatile})
  ret void
}}
'''.strip()

    asm_text = emit_self_asm(ir_text)

    assert "  bl _memcpy" in asm_text
    assert "  ldr q0" not in asm_text
    assert "  str q0" not in asm_text


@pytest.mark.parametrize(
    ("dst_alignment", "fill", "size", "is_volatile"),
    (
        (8, 0, 32, "false"),
        (16, 1, 32, "false"),
        (16, 0, 48, "false"),
        (16, 0, 32, "true"),
    ),
)
def test_aarch64_block_zero_controls_choose_scalar_or_call_fallback(
    monkeypatch,
    dst_alignment: int,
    fill: int,
    size: int,
    is_volatile: str,
) -> None:
    monkeypatch.delenv("PCC_SELF_TARGET_PASSES", raising=False)
    ir_text = f'''
target triple = "arm64-apple-darwin23.6.0"

declare void @llvm.memset.p0.i64(ptr, i8, i64, i1)

define void @zero(ptr %dst) {{
entry:
  call void @llvm.memset.p0.i64(ptr align {dst_alignment} %dst, i8 {fill}, i64 {size}, i1 {is_volatile})
  ret void
}}
'''.strip()

    asm_text = emit_self_asm(ir_text)

    if fill == 0 and is_volatile == "false" and size <= 128:
        assert "  bl _memset" not in asm_text
        assert "xzr" in asm_text
    else:
        assert "  bl _memset" in asm_text
    assert "  movi v0.16b, #0" not in asm_text
    assert "  str q0" not in asm_text


def test_self_target_pass_unknown_name_fails():
    with pytest.raises(BackendUnavailable, match="unknown self target pass"):
        resolve_self_target_pass_names("not-a-pass")


def test_self_target_memory_pass_rejects_text_only_pass():
    with pytest.raises(BackendUnavailable, match="unknown self target pass"):
        resolve_self_target_pass_names(
            "strip-trailing-whitespace",
            transport="memory",
        )


def test_emit_self_asm_runs_explicit_target_text_pass(monkeypatch):
    ir_text = """
target triple = "arm64-apple-darwin23.6.0"

define i32 @main() {
entry:
  ret i32 42
}
""".strip()

    monkeypatch.setenv(
        "PCC_SELF_TARGET_PASSES",
        "strip-trailing-whitespace",
    )
    asm_text = emit_self_asm(ir_text)

    assert "_main:" in asm_text
    assert "movz w0, #42" in asm_text
    assert all(not line.endswith((" ", "\t")) for line in asm_text.splitlines())


def test_emit_self_asm_runs_explicit_target_memory_pass(monkeypatch):
    ir_text = """
target triple = "arm64-apple-darwin23.6.0"

define i32 @main() {
entry:
  ret i32 42
}
""".strip()

    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "all")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "memory")
    asm_text = emit_self_asm(ir_text)

    assert "_main:" in asm_text
    assert "movz w0, #42" in asm_text
