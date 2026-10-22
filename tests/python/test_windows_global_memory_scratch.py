"""Scratch ownership of the Win64 global-memory helper.

Exercise real lowering helpers and the owned encoder/COFF writer. These
checks do not execute native instructions or qualify the Windows ABI.
"""

from __future__ import annotations

import re

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import self_backend_win64_abi as win64
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.x86_64_frame_registers import saved_area_offset


# Expected scratch is explicit, independently of the helper's alias test.
CASES = (
    ("r10", "int", 64, "QWORD PTR", "mov", "r11", 64),
    ("r10d", "int", 32, "DWORD PTR", "mov", "r11", 64),
    ("r10w", "int", 16, "WORD PTR", "mov", "r11", 64),
    ("r10b", "int", 8, "BYTE PTR", "mov", "r11", 64),
    ("r11", "int", 64, "QWORD PTR", "mov", "r10", 24),
    ("r11d", "int", 32, "DWORD PTR", "mov", "r10", 24),
    ("r11w", "int", 16, "WORD PTR", "mov", "r10", 24),
    ("r11b", "int", 8, "BYTE PTR", "mov", "r10", 24),
    ("xmm0", "fp", 32, "DWORD PTR", "movss", "r11", 64),
    ("xmm0", "fp", 64, "QWORD PTR", "movsd", "r11", 64),
    ("xmm1", "fp", 32, "DWORD PTR", "movss", "r11", 64),
    ("xmm1", "fp", 64, "QWORD PTR", "movsd", "r11", 64),
    ("xmm10", "fp", 32, "DWORD PTR", "movss", "r11", 64),
    ("xmm10", "fp", 64, "QWORD PTR", "movsd", "r11", 64),
    ("xmm11", "fp", 32, "DWORD PTR", "movss", "r11", 64),
    ("xmm11", "fp", 64, "QWORD PTR", "movsd", "r11", 64),
)


@pytest.fixture(autouse=True)
def _win64_context(monkeypatch):
    # Select the actual target/helper state; do not replace any implementation.
    monkeypatch.setattr(emitter, "_WINDOWS_ABI", True)
    monkeypatch.setattr(emitter, "_TLS_GLOBALS", {})
    monkeypatch.setattr(emitter, "_MODULE_SYMBOLS", type(emitter._MODULE_SYMBOLS)(
        internal_prefix="", defined_symbols=frozenset(),
        internal_symbols=frozenset(), thread_local_symbols=frozenset(),
    ))


def _frame(local_bytes, arguments):
    declaration = ""
    call = ""
    if arguments:
        types = ", ".join(["i64"] * arguments)
        values = ", ".join(f"i64 {index + 1}" for index in range(arguments))
        declaration = f"declare void @callee({types})\n"
        call = f"  call void @callee({values})\n"
    module = parse_self_backend_module(
        'target triple = "x86_64-pc-windows-msvc"\n'
        + declaration
        + "define void @probe() {\nentry:\n"
        + call + "  ret void\n}\n"
    )
    function = module.functions[0]
    # This is prepare_frame's existing target-neutral stack-size input.
    function.frame_size = local_bytes
    win64.prepare_frame(function)
    return function


@pytest.mark.parametrize("store", (False, True), ids=("load", "store"))
@pytest.mark.parametrize(
    "register, kind, width, memory, opcode, scratch, relative_slot", CASES,
)
def test_global_access_keeps_value_and_scratch_owners(
    monkeypatch, register, kind, width, memory, opcode, scratch, relative_slot, store,
):
    function = _frame(160, 9)
    base = win64.outgoing_size(function)
    monkeypatch.setattr(emitter, "_WINDOWS_SCRATCH_BASE", base)
    type_desc = TypeDesc(kind, width)
    entry = emitter._store_reg_to_global if store else emitter._load_global_to_reg
    lines = entry("@global_value", register, type_desc)
    slot = base + relative_slot
    access = (
        f"  {opcode} {memory} [{scratch}], {register}"
        if store else f"  {opcode} {register}, {memory} [{scratch}]"
    )
    assert lines == [
        f"  mov QWORD PTR [rsp + {slot}], {scratch}",
        f"  movabs {scratch}, OFFSET FLAT:global_value",
        access,
        f"  mov {scratch}, QWORD PTR [rsp + {slot}]",
    ]
    assert lines == win64.global_memory_access(
        "@global_value", register, type_desc, base, store,
    )

    # Full physical-register aliases must differ: restoring r11 after an
    # r11d/r11w/r11b load would also overwrite the newly loaded value.
    value_parent = (
        "r10" if register in ("r10", "r10d", "r10w", "r10b")
        else "r11" if register in ("r11", "r11d", "r11w", "r11b")
        else register
    )
    assert scratch != value_parent
    assert all(not line.strip().startswith(("call ", "push ", "pop ", "sub rsp", "add rsp"))
               for line in lines)

    # Encode the complete real sequence, including each integer/XMM width.
    source = (
        ".intel_syntax noprefix\n.text\n.globl probe\n"
        ".type probe, @function\nprobe:\n"
        + "\n".join(lines)
        + "\nret\n.size probe, .-probe\n"
        ".data\n.p2align 3\n.globl global_value\n"
        ".type global_value, @object\nglobal_value:\n.quad 7\n"
    )
    obj = coff.parse_object(coff.assemble_object(source))
    text = next(section for section in obj.sections if section.name == ".text")
    assert text.data[-1:] == b"\xc3"
    assert len(text.relocations) == 1
    relocation = text.relocations[0]
    assert relocation.kind == coff.ADDR64
    assert obj.symbols[relocation.symbol].name == "global_value"
    assert text.data[relocation.offset:relocation.offset + 8] == b"\0" * 8


@pytest.mark.parametrize("local_bytes, arguments, expected_outgoing", (
    (0, 0, 32),
    (0, 9, 80),
    (160, 9, 160),
))
def test_global_scratch_is_outside_arguments_saved_registers_and_locals(
    local_bytes, arguments, expected_outgoing,
):
    function = _frame(local_bytes, arguments)
    base = win64.outgoing_size(function)
    total = win64.frame_size(function)
    area = win64.PLATFORM_AREA_SIZE
    assert base == expected_outgoing
    assert function.platform_frame_extra == base + area
    assert total == local_bytes + base + area

    prologue = win64.prologue(function)
    epilogue = win64.epilogue(function)
    saved = []
    for line in prologue:
        match = re.fullmatch(
            r"  (mov|movdqu) (QWORD|XMMWORD) PTR \[rsp \+ ([0-9]+)\], (rbx|rsi|rdi|xmm10|xmm11)",
            line,
        )
        if match:
            saved.append((match[4], int(match[3]), 16 if match[2] == "XMMWORD" else 8))
    assert saved == [
        ("rbx", base, 8), ("rsi", base + 8, 8), ("rdi", base + 16, 8),
        ("xmm10", base + 32, 16), ("xmm11", base + 48, 16),
    ]
    for register, offset, width in saved:
        opcode, size = ("movdqu", "XMMWORD") if width == 16 else ("mov", "QWORD")
        assert f"  {opcode} {register}, {size} PTR [rsp + {offset}]" in epilogue

    first = set(range(base + 24, base + 32))
    second = set(range(base + 64, base + 72))
    assert first.isdisjoint(second)
    occupied = set(range(base))  # outgoing arguments and parallel-phi scratch
    occupied.update(range(total - local_bytes, total))
    for _register, offset, width in saved:
        occupied.update(range(offset, offset + width))
    # The nearbyint MXCSR pair and the r12-r15 save area.
    occupied.update(range(base + win64.MXCSR_OFFSET, base + win64.MXCSR_OFFSET + 8))
    occupied.update(range(saved_area_offset(function.platform_frame_extra), base + area))
    assert first.isdisjoint(occupied)
    assert second.isdisjoint(occupied)
    assert max(first | second) < base + area
