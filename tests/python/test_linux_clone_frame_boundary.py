"""Host-only contracts for the owned Linux clone machine boundary."""

import pytest

from pcc.backend.linux_thread_intrinsics import assembly
from pcc.backend.owned_elf_link import assemble


@pytest.mark.parametrize(
    "aarch64, label, reset, call",
    [
        (True, "L_pcc_clone_child:", "mov x29, xzr", "blr x9"),
        (False, ".Lpcc_clone_child:", "xor ebp, ebp", "call r11"),
    ],
)
def test_clone_child_terminates_frame_chain_before_first_call(
    aarch64, label, reset, call
):
    text = assembly(aarch64)
    parent, child = text.split(label, 1)
    child = child.split(".globl pcc_linux_thread_exit_free", 1)[0]
    instructions = [
        line.strip() for line in child.splitlines() if line.startswith("  ")
    ]
    call_index = instructions.index(call)
    assert instructions[call_index - 1] == reset
    assert instructions.count(reset) == 1
    assert reset not in parent
    assert not any(
        instruction.startswith(("call ", "bl ", "blr "))
        for instruction in instructions[:call_index]
    )


@pytest.mark.parametrize("aarch64", [True, False])
def test_clone_parent_return_tls_and_child_arguments_stay_intact(aarch64):
    text = assembly(aarch64)
    if aarch64:
        parent, child = text.split("L_pcc_clone_child:", 1)
        expected = [
            "sub x5, x0, #16", "str x2, [x5]", "str x3, [x5, #8]",
            "mov x6, x1", "mov x2, x4", "mov x1, x5", "mov x3, x6",
            "movz x0, #3840", "movk x0, #61, lsl #16", "mov x8, #220",
            "svc #0", "cbz x0, L_pcc_clone_child", "ret",
        ]
        assert child.lstrip().startswith("ldr x9, [sp]\n  ldr x0, [sp, #8]\n")
    else:
        parent, child = text.split(".Lpcc_clone_child:", 1)
        expected = [
            "sub rdi, 16", "mov QWORD PTR [rdi], rdx",
            "mov QWORD PTR [rdi + 8], rcx", "mov r9, rsi", "mov rsi, rdi",
            "mov rdi, 4001536", "mov rdx, r8", "mov r10, r8", "mov r8, r9",
            "mov eax, 56", "syscall", "test rax, rax", "je .Lpcc_clone_child", "ret",
        ]
        assert child.lstrip().startswith(
            "mov r11, QWORD PTR [rsp]\n  mov rdi, QWORD PTR [rsp + 8]\n"
        )
    parent = parent.split("pcc_linux_thread_spawn:\n", 1)[1]
    assert [line.strip() for line in parent.splitlines() if line.strip()] == expected


@pytest.mark.parametrize(
    "aarch64, target, reset, reset_bytes, call_bytes",
    [
        (True, "aarch64-unknown-linux-gnu", "mov x29, xzr", "fd031faa", "20013fd6"),
        # The owned encoder retains REX.W on the existing indirect call.
        (False, "x86_64-unknown-linux-gnu", "xor ebp, ebp", "31ed", "49ffd3"),
    ],
)
def test_clone_frame_reset_encodes_without_other_text_changes(
    aarch64, target, reset, reset_bytes, call_bytes
):
    text = assembly(aarch64)
    reset_line = "  " + reset + "\n"
    assert text.count(reset_line) == 1
    before = assemble(text.replace(reset_line, "", 1), target)
    after = assemble(text, target)
    old_text = next(
        section.data for section in before.sections if section.name == ".text"
    )
    new_text = next(
        section.data for section in after.sections if section.name == ".text"
    )
    encoded_reset = bytes.fromhex(reset_bytes)
    encoded_call = bytes.fromhex(call_bytes)
    assert new_text.count(encoded_reset + encoded_call) == 1
    assert new_text.replace(encoded_reset + encoded_call, encoded_call, 1) == old_text
    assert len(new_text) == len(old_text) + len(encoded_reset)


@pytest.mark.parametrize("aarch64", [True, False])
def test_clone_callback_return_still_exits_thread(aarch64):
    text = assembly(aarch64)
    if aarch64:
        child = text.split("L_pcc_clone_child:", 1)[1]
        assert (
            "  blr x9\n  mov x0, #0\n  mov x8, #93\n  svc #0\n  brk #0\n"
            in child
        )
    else:
        child = text.split(".Lpcc_clone_child:", 1)[1]
        assert (
            "  call r11\n  xor edi, edi\n  mov eax, 60\n  syscall\n  ud2\n"
            in child
        )
