"""x86-64 branches straight to the block a chain of jumps ends at.

Phi edges and empty blocks are emitted as labels whose only instruction is a
``jmp``: ``jne .Lf_a_to_b`` / ``.Lf_a_to_b:`` / ``jmp .Lf_b``. Every branch
to such a label goes to its final target instead, ``jcc A; jmp B; A:``
becomes ``jncc B; A:``, and a ``jmp`` to the label right after it is
dropped. Labels stay: stack maps may name a block.
"""
from __future__ import annotations

import pytest

from pcc.backend.self_backend_x86_64_linux import (
    _has_label_before_next_instruction,
    _thread_x86_jumps,
)


def _old_labels_before_next_instruction(lines: list[str], index: int) -> list[str]:
    """Pre-change implementation, kept here only as an equivalence oracle."""
    labels: list[str] = []
    index += 1
    while index < len(lines):
        line = lines[index]
        if line:
            if line[0] == " " or not line.endswith(":"):
                return labels
            labels.append(line[:len(line) - 1])
        index += 1
    return labels


@pytest.mark.parametrize("lines", [
    [],
    ["  jmp .La", "", ".Lb:", ".La:", "  ret"],
    ["  jmp .La", ".Lb:", ".text", ".La:", "  ret"],
    ["  jmp .La", " .La:", ".La:"],
    ["  jmp .La", "\t.La:", "\t jmp .Lb", ".Lb:"],
    ["  jmp .La", ":", ".La: ", ".La:"],
    [".La:", "", ".Lb:"],
])
def test_label_membership_matches_previous_collection(lines):
    for index in range(-1, len(lines) + 2):
        for target in ("", ".La", ".Lb", ".Lc", ".La:"):
            assert _has_label_before_next_instruction(lines, index, target) == (
                target in _old_labels_before_next_instruction(lines, index)
            )


def test_branches_skip_jump_only_blocks_and_chains():
    lines = [
        "  cmp r10, 0",
        "  jne .Lf_a_to_b",
        "  call g",
        "  jmp .Lf_c",
        "",
        ".Lf_a_to_b:",
        "  jmp .Lf_mid",
        ".Lf_mid:",
        ".Lf_mid2:",
        "",
        "  jmp .Lf_b",
        "",
        ".Lf_c:",
        "  ret",
        ".Lf_b:",
        "  ret",
    ]
    assert _thread_x86_jumps(lines) == [
        "  cmp r10, 0",
        "  jne .Lf_b",
        "  call g",
        "  jmp .Lf_c",
        "",
        ".Lf_a_to_b:",
        "  jmp .Lf_b",
        ".Lf_mid:",
        ".Lf_mid2:",
        "",
        "  jmp .Lf_b",
        "",
        ".Lf_c:",
        "  ret",
        ".Lf_b:",
        "  ret",
    ]


def test_condition_inverts_over_a_jump_to_the_next_block():
    lines = [
        "  cmp r10, 0",
        "  setne al",
        "  jne .Lf_entry_to_body",
        "  jmp .Lf_fail",
        "",
        ".Lf_entry_to_body:",
        "  jmp .Lf_body",
        "",
        ".Lf_body:",
        "  ret",
        ".Lf_fail:",
        "  ud2",
    ]
    once = _thread_x86_jumps(lines)
    assert once == [
        "  cmp r10, 0",
        "  setne al",
        "  je .Lf_fail",
        "",
        ".Lf_entry_to_body:",
        "",
        ".Lf_body:",
        "  ret",
        ".Lf_fail:",
        "  ud2",
    ]
    assert _thread_x86_jumps(once) == once


def test_jump_to_the_following_label_is_dropped():
    assert _thread_x86_jumps(["  mov eax, 1", "  jmp .Lf_x", "", ".Lf_y:", ".Lf_x:", "  ret"]) == [
        "  mov eax, 1", "", ".Lf_y:", ".Lf_x:", "  ret",
    ]


@pytest.mark.parametrize("lines", [
    # Jumps that leave the function or are indirect are not local jumps.
    ["  jmp g", ".Lg:", "  ret"],
    ["  jmp r10", ".Lx:", "  ret"],
    # A label with an instruction before its jump is a real block.
    ["  je .Lf_a", "  ret", ".Lf_a:", "  mov eax, 1", "  jmp .Lf_b", ".Lf_b:", "  ret"][:4] + ["  ret"],
    # js has no recorded inverse, so it is not flipped over the jmp.
    ["  js .Lf_a", "  jmp .Lf_b", ".Lf_a:", "  ret", ".Lf_b:", "  ret"],
    # A jump-only block that loops to itself.
    [".Lf_spin:", "  jmp .Lf_spin"],
])
def test_shapes_that_stay(lines):
    assert _thread_x86_jumps(lines) == lines
