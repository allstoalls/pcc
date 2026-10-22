"""x86-64 reads a copied register's original and drops moves nothing reads.

``mov r10, rbx`` / ``cmp r10, 0`` compares rbx directly, after which the
move is dead and dropped. Instructions outside the modeled set are barriers,
labels end forwarding, and rsp/rbp are never forwarded or dropped.
"""
from __future__ import annotations

import pytest

from pcc.backend.x86_64_register_copies import forward_register_copies


def test_operands_read_the_original_and_dead_copies_go():
    assert forward_register_copies([
        "  mov r10, rbx",
        "  cmp r10, 0",
        "  mov r11, r10",
        "  mov rdi, r11",
        "  mov rsi, QWORD PTR [r10 + 8]",
        "  call g",
        "  mov r10, rax",
        "  mov r12, r10",
        "  ret",
    ]) == [
        "  cmp rbx, 0",
        "  mov rdi, rbx",
        "  mov rsi, QWORD PTR [rbx + 8]",
        "  call g",
        "  mov r12, rax",
        "  ret",
    ]


def test_narrow_copies_forward_only_at_their_width():
    assert forward_register_copies([
        "  mov r10b, bl",
        "  test r10b, r10b",
        "  mov r11d, r12d",
        "  mov rdi, r11",
        "  mov ecx, r11d",
        "  call g",
        "  ret",
    ]) == [
        "  test bl, bl",
        "  mov r11d, r12d",
        "  mov rdi, r11",
        "  mov ecx, r12d",
        "  call g",
        "  ret",
    ]


def test_flags_results_nothing_reads_go():
    assert forward_register_copies([
        "  cmp rax, 0", "  setne r11b", "  je .Lf_x", "  mov rax, 1", ".Lf_x:", "  ret",
    ]) == ["  cmp rax, 0", "  je .Lf_x", "  mov rax, 1", ".Lf_x:", "  ret"]
    # al is part of the returned rax.
    lines = ["  cmp rdi, 0", "  setne al", "  je .Lf_x", "  mov rax, 1", ".Lf_x:", "  ret"]
    assert forward_register_copies(lines) == lines


def test_moves_that_only_feed_themselves_around_a_loop_go():
    # Each byte write merges into rax, which only the next iteration's byte
    # write reads; nothing reads al or rax before the exit writes eax. (A
    # call would read al: it may be variadic.)
    assert forward_register_copies([
        ".Lf_loop:",
        "  mov al, r13b",
        "  test r13b, r13b",
        "  je .Lf_done",
        "  add r12d, 1",
        "  jmp .Lf_loop",
        ".Lf_done:",
        "  mov eax, r12d",
        "  ret",
    ]) == [
        ".Lf_loop:",
        "  test r13b, r13b",
        "  je .Lf_done",
        "  add r12d, 1",
        "  jmp .Lf_loop",
        ".Lf_done:",
        "  mov eax, r12d",
        "  ret",
    ]


def test_self_moves_go_except_the_zero_extending_one():
    assert forward_register_copies([
        "  mov r10, rbx", "  mov rbx, r10", "  mov r13d, r13d", "  mov rdi, r13", "  call g", "  ret",
    ]) == ["  mov r13d, r13d", "  mov rdi, r13", "  call g", "  ret"]


def test_a_call_reads_rax_only_as_a_variadic_count():
    # A byte result in al that no call or later read uses is dropped; the
    # variadic vector count stays even with nothing else reading al.
    assert forward_register_copies([
        "  cmp rdi, 0", "  setne al", "  call g", "  mov rdi, rax", "  mov al, 1",
        "  mov r11, rbx", "  call r11", "  call h", "  ret",
    ]) == [
        "  cmp rdi, 0", "  call g", "  mov rdi, rax", "  mov al, 1", "  mov r11, rbx", "  call r11",
        "  call h", "  ret",
    ]


def test_the_win64_stack_probe_reads_its_size_from_rax():
    lines = ["  push rbp", "  mov rbp, rsp", "  mov eax, 8192", "  call __pcc_chkstk",
             "  sub rsp, rax", "  .seh_stackalloc 8192", "  mov rax, rbx", "  ret"]
    assert forward_register_copies(lines) == lines


def test_scratch_registers_are_dead_at_a_return():
    assert forward_register_copies([
        "  mov r10, rax", "  mov rcx, 1", "  mov rdx, r13", "  ret",
    ]) == ["  mov rdx, r13", "  ret"]


def test_writes_end_a_copy():
    lines = [
        "  mov r10, rbx",
        "  add rbx, 1",
        "  mov rdi, r10",
        "  mov r11, r13",
        "  add r11, 2",
        "  mov rsi, r11",
        "  call g",
        "  ret",
    ]
    assert forward_register_copies(lines) == lines


def test_calls_labels_and_barriers_end_copies():
    lines = [
        # The call clobbers r10, the copy's source.
        "  mov r13, r10",
        "  call g",
        "  mov rdi, r13",
        "  mov r14, rbx",
        # Another path may enter at a label.
        ".Lf_x:",
        "  mov rsi, r14",
        "  mov r10, r12",
        "  lock xadd QWORD PTR [r13], r10",
        "  mov rdx, r10",
        "  call h",
        "  ret",
    ]
    assert forward_register_copies(lines) == lines


def test_copies_of_callee_saved_registers_survive_calls():
    assert forward_register_copies([
        "  mov r13, rbx", "  call g", "  mov rdi, r13", "  call h", "  ret",
    ]) == ["  mov r13, rbx", "  call g", "  mov rdi, rbx", "  call h", "  ret"]


def test_live_registers_across_blocks_keep_their_moves():
    lines = [
        "  mov r10, rbx",
        "  cmp r12, 0",
        "  je .Lf_b",
        "  mov rax, 1",
        "  ret",
        ".Lf_b:",
        "  mov rdi, r10",
        "  call g",
        "  ret",
    ]
    assert forward_register_copies(lines) == lines


@pytest.mark.parametrize("lines", [
    # A load may fault.
    ["  mov r10, QWORD PTR [rbx]", "  ud2"],
    # rsp and rbp are never forwarded or dropped.
    ["  mov r11, rsp", "  mov rdi, QWORD PTR [r11 + 8]", "  mov rbp, rsp", "  ud2"],
    # r12 cannot be an SIB index.
    ["  mov r10, r12", "  mov rax, QWORD PTR [rbx + r10*8]", "  ret"],
    # The divisor keeps rax/rdx live.
    ["  mov rax, r12", "  cqo", "  idiv rbx", "  ret"],
    # A jump out of the function text stops the dead-move pass.
    ["  mov r10, 5", "  jmp g"],
])
def test_shapes_that_stay(lines):
    assert forward_register_copies(lines) == lines


def test_constants_become_immediates():
    assert forward_register_copies([
        "  mov r10b, 1",
        "  mov BYTE PTR [rbp - 64], r10b",
        "  xor r10, r10",
        "  mov QWORD PTR [rbp - 56], r10",
        "  mov r11, 5",
        "  mov rsi, r11",
        "  cmp rbx, r11",
        "  mov r10, 4294967296",
        "  mov QWORD PTR [rbp - 72], r10",
        "  call g",
        "  ret",
    ]) == [
        "  mov BYTE PTR [rbp - 64], 1",
        "  mov QWORD PTR [rbp - 56], 0",
        "  mov rsi, 5",
        "  cmp rbx, 5",
        # A 64-bit store takes only a sign-extended imm32.
        "  mov r10, 4294967296",
        "  mov QWORD PTR [rbp - 72], r10",
        "  call g",
        "  ret",
    ]


def test_constants_stay_registers_where_the_encoder_takes_no_immediate():
    lines = [
        "  mov r10, 5", "  test rbx, r10", "  cmp QWORD PTR [rbx], r10", "  setne al",
        "  movzx eax, al", "  ret",
    ]
    assert forward_register_copies(lines) == lines


def test_zero_idiom_stays_while_its_flags_are_read():
    lines = ["  xor r10, r10", "  mov QWORD PTR [rbp - 8], r10", "  jne .Lf_x", "  ud2", ".Lf_x:", "  ret"]
    assert forward_register_copies(lines) == [
        "  xor r10, r10", "  mov QWORD PTR [rbp - 8], 0", "  jne .Lf_x", "  ud2", ".Lf_x:", "  ret",
    ]
    # A variadic call's vector count stays though the model has calls read
    # no rax.
    lines = ["  xor eax, eax", "  call printf", "  ret"]
    assert forward_register_copies(lines) == lines
