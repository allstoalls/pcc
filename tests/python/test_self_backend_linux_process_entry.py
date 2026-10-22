from __future__ import annotations

import re

import pytest


LINUX_TRIPLE = "x86_64-unknown-linux-gnu"


def _emit(ir_text: str) -> str:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    return emit_self_asm(ir_text, LINUX_TRIPLE)


def _module(function_ir: str) -> str:
    return f'target triple = "{LINUX_TRIPLE}"\n\n{function_ir}\n'


def test_linux_process_entry_receives_kernel_initial_stack_and_traps_on_return():
    asm = _emit(
        _module(
            """
define void @_start(ptr %initial_stack) {
entry:
  %argc = load i64, ptr %initial_stack
  %used = icmp sge i64 %argc, 0
  br i1 %used, label %done, label %done
done:
  ret void
}
""".strip()
        )
    )

    start_body = asm.split("_start:", 1)[1].split(".size _start", 1)[0]
    prologue = [
        "  mov r11, rsp",
        "  and rsp, -16",
        "  sub rsp, 8",
        "  push rbp",
        "  mov rbp, rsp",
    ]
    positions = [start_body.index(line) for line in prologue]
    assert positions == sorted(positions)
    # The initial stack pointer becomes the argument's home: its frame slot or
    # the callee-saved register x86_64_frame_registers keeps it in.
    assert re.search(r"mov (QWORD PTR \[rbp - \d+\]|rbx|r1[2-5]), r11", start_body)
    assert "rdi" not in start_body[: start_body.index(".L_start_entry:")]
    assert "ud2" in start_body
    assert "  ret" not in start_body


@pytest.mark.parametrize(
    "signature",
    [
        "define void @_start() {\nentry:\n  ret void\n}",
        "define i64 @_start(ptr %stack) {\nentry:\n  ret i64 0\n}",
        "define void @_start(i64 %stack) {\nentry:\n  ret void\n}",
        "define internal void @_start(ptr %stack) {\nentry:\n  ret void\n}",
        "define void @_start(ptr %stack, ...) {\nentry:\n  ret void\n}",
    ],
)
def test_linux_process_entry_rejects_ambiguous_kernel_abi_signatures(signature):
    from pcc.backend import BackendUnavailable

    with pytest.raises(
        BackendUnavailable,
        match=r"Linux process entry '_start' must be global void \(ptr initial_stack\)",
    ):
        _emit(_module(signature))


def test_ordinary_linux_function_keeps_sysv_argument_and_return_contract():
    asm = _emit(
        _module(
            """
define i64 @identity(ptr %value) {
entry:
  %raw = ptrtoint ptr %value to i64
  ret i64 %raw
}
""".strip()
        )
    )

    body = asm.split("identity:", 1)[1].split(".size identity", 1)[0]
    assert re.search(r"mov (QWORD PTR \[rbp - \d+\]|rbx|r1[2-5]), rdi", body)
    assert "and rsp, -16" not in body
    assert "  ret" in body
