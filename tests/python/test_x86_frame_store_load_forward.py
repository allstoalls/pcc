"""An x86 frame-slot reload right after its store becomes a register move.

Every SSA value lives in a stack slot, so a definition's store is usually
followed by a reload of the same slot. The store must stay (the slot is the
value's home for stack maps and later uses); only an adjacent reload of the
same slot and width is forwarded.
"""
from __future__ import annotations

from pcc.backend.self_backend_x86_64_linux import (
    _forward_adjacent_frame_store_load,
    _parse_frame_transfer,
)


def test_parse_frame_transfer_reads_store_and_load():
    assert _parse_frame_transfer("  mov QWORD PTR [rbp - 24], rax") == (True, 8, 24, "rax")
    assert _parse_frame_transfer("  mov ecx, DWORD PTR [rbp - 8]") == (False, 4, 8, "ecx")
    assert _parse_frame_transfer("  mov r9b, BYTE PTR [rbp - 1]") == (False, 1, 1, "r9b")
    # Width and register size must agree; other bases, immediates and SSE
    # moves are not frame transfers.
    assert _parse_frame_transfer("  mov QWORD PTR [rbp - 8], eax") is None
    assert _parse_frame_transfer("  mov QWORD PTR [rsp + 8], rax") is None
    assert _parse_frame_transfer("  mov QWORD PTR [rbp - 8], 0") is None
    assert _parse_frame_transfer("  movsd QWORD PTR [rbp - 8], xmm0") is None
    assert _parse_frame_transfer("  mov rbx, QWORD PTR [rbp - 8]") is None


def test_adjacent_reload_becomes_register_move_or_disappears():
    assert _forward_adjacent_frame_store_load([
        "  mov QWORD PTR [rbp - 16], rax",
        "  mov rcx, QWORD PTR [rbp - 16]",
        "  mov DWORD PTR [rbp - 20], edx",
        "  mov edx, DWORD PTR [rbp - 20]",
    ]) == [
        "  mov QWORD PTR [rbp - 16], rax",
        "  mov rcx, rax",
        "  mov DWORD PTR [rbp - 20], edx",
    ]


def test_reload_is_kept_unless_adjacent_same_slot_and_width():
    lines = [
        "  mov QWORD PTR [rbp - 16], rax",
        "  mov rcx, QWORD PTR [rbp - 24]",
        "  mov QWORD PTR [rbp - 32], rax",
        "  mov ecx, DWORD PTR [rbp - 32]",
        "  mov QWORD PTR [rbp - 40], rax",
        ".Lblock_7:",
        "  mov rcx, QWORD PTR [rbp - 40]",
        "  mov QWORD PTR [rbp - 48], rax",
        "  call pcc_helper",
        "  mov rcx, QWORD PTR [rbp - 48]",
    ]
    assert _forward_adjacent_frame_store_load(lines) == lines
