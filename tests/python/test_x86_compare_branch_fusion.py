"""x86-64 compares branch on their own flags and take small immediates.

Every fallible call is followed by an i1 compare that the error edge tests:
``mov r11, 0; cmp r10, r11; setne al; mov [slot], al; test al, al; jne L``.
``setcc`` and ``mov`` leave the flags alone, so the edge can branch on the
compare directly, and a compare against a constant that fits imm32 needs no
scratch register. About 10,000 of 220,355 instructions in the x86-64 object
of ``worker_resource_plan`` were these two shapes.
"""
from __future__ import annotations

import pytest

from pcc.backend.self_backend_x86_64_linux import _fuse_x86_compare_branches


def test_edge_branches_on_the_compare_flags():
    assert _fuse_x86_compare_branches([
        "  mov r10, QWORD PTR [rbp - 24]",
        "  mov r11, 0",
        "  cmp r10, r11",
        "  setne al",
        "  mov BYTE PTR [rbp - 20], al",
        "  test al, al",
        "  jne .Lf_err",
    ]) == [
        "  mov r10, QWORD PTR [rbp - 24]",
        "  cmp r10, 0",
        "  setne al",
        "  mov BYTE PTR [rbp - 20], al",
        "  jne .Lf_err",
    ]


@pytest.mark.parametrize("setcc, branch, fused", [
    ("setl", "jne", "jl"),
    ("setl", "je", "jge"),
    ("setge", "je", "jl"),
    ("sete", "je", "jne"),
    ("setb", "jne", "jb"),
    ("seta", "je", "jbe"),
])
def test_condition_mapping(setcc, branch, fused):
    lines = ["  cmp r10, r11", f"  {setcc} al", "  mov BYTE PTR [rbp - 9], al",
             "  test al, al", f"  {branch} .Lx"]
    assert _fuse_x86_compare_branches(lines) == lines[:3] + [f"  {fused} .Lx"]


def test_32_bit_and_zeroed_scratch_compares():
    assert _fuse_x86_compare_branches([
        "  mov r11d, -7", "  cmp r10d, r11d", "  jl .Ly",
    ]) == ["  cmp r10d, -7", "  jl .Ly"]
    assert _fuse_x86_compare_branches([
        "  xor r11, r11", "  cmp r10, r11", "  sete al",
    ]) == ["  cmp r10, 0", "  sete al"]


@pytest.mark.parametrize("lines", [
    # r11 is read after the compare (min/max via cmov).
    ["  mov r11, 5", "  cmp r10, r11", "  cmovg r10, r11"],
    # The immediate does not fit a sign-extended imm32.
    ["  mov r11, 4294967296", "  cmp r10, r11", "  setne al"],
    # Something other than setcc/mov sits between the compare and the test.
    ["  cmp r10, r11", "  setp al", "  and al, cl", "  mov BYTE PTR [rbp - 9], al",
     "  test al, al", "  jne .Lz"],
    # The tested byte was reloaded, not produced by this setcc.
    ["  mov al, BYTE PTR [rbp - 9]", "  test al, al", "  jne .Lz"],
])
def test_shapes_that_must_stay(lines):
    assert _fuse_x86_compare_branches(lines) == lines
