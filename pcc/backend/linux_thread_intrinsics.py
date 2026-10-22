"""Owned clone/exit machine-boundary routines for the Linux thread runtime.

Clone replaces the child stack pointer but inherits the creator's frame pointer.
Terminate that chain before the first compiled child call: thread registration
walks saved frame pointers and must never follow the creator's mutable stack.
The parent path must retain its frame pointer so it can return normally.
"""


def assembly(aarch64: bool) -> str:
    if aarch64:
        return """.section __TEXT,__text,regular,pure_instructions
.globl pcc_linux_rt_sigreturn
pcc_linux_rt_sigreturn:
  mov x8, #139
  svc #0
  brk #0
.globl pcc_linux_thread_spawn
pcc_linux_thread_spawn:
  sub x5, x0, #16
  str x2, [x5]
  str x3, [x5, #8]
  mov x6, x1
  mov x2, x4
  mov x1, x5
  mov x3, x6
  movz x0, #3840
  movk x0, #61, lsl #16
  mov x8, #220
  svc #0
  cbz x0, L_pcc_clone_child
  ret
L_pcc_clone_child:
  ldr x9, [sp]
  ldr x0, [sp, #8]
  mov x29, xzr
  blr x9
  mov x0, #0
  mov x8, #93
  svc #0
  brk #0
.globl pcc_linux_thread_exit_free
pcc_linux_thread_exit_free:
  mov x19, x0
  mov x20, x1
  mov x0, x2
  mov x1, x3
  mov x8, #215
  svc #0
  mov x0, x19
  mov x1, x20
  mov x8, #215
  svc #0
  mov x0, #0
  mov x8, #93
  svc #0
  brk #0
"""
    return """.intel_syntax noprefix
.text
.globl pcc_linux_rt_sigreturn
.type pcc_linux_rt_sigreturn, @function
pcc_linux_rt_sigreturn:
  mov eax, 15
  syscall
  ud2
.size pcc_linux_rt_sigreturn, .-pcc_linux_rt_sigreturn
.globl pcc_linux_thread_spawn
.type pcc_linux_thread_spawn, @function
pcc_linux_thread_spawn:
  sub rdi, 16
  mov QWORD PTR [rdi], rdx
  mov QWORD PTR [rdi + 8], rcx
  mov r9, rsi
  mov rsi, rdi
  mov rdi, 4001536
  mov rdx, r8
  mov r10, r8
  mov r8, r9
  mov eax, 56
  syscall
  test rax, rax
  je .Lpcc_clone_child
  ret
.Lpcc_clone_child:
  mov r11, QWORD PTR [rsp]
  mov rdi, QWORD PTR [rsp + 8]
  xor ebp, ebp
  call r11
  xor edi, edi
  mov eax, 60
  syscall
  ud2
.size pcc_linux_thread_spawn, .-pcc_linux_thread_spawn
.globl pcc_linux_thread_exit_free
.type pcc_linux_thread_exit_free, @function
pcc_linux_thread_exit_free:
  mov r8, rdi
  mov r9, rsi
  mov rdi, rdx
  mov rsi, rcx
  mov eax, 11
  syscall
  mov rdi, r8
  mov rsi, r9
  mov eax, 11
  syscall
  xor edi, edi
  mov eax, 60
  syscall
  ud2
.size pcc_linux_thread_exit_free, .-pcc_linux_thread_exit_free
"""
