"""pcc.unsafe.syscall6 (LIBC-P1-PRIMITIVES, syscall half).

The intrinsic is the musl ``arch/x86_64/syscall_arch.h`` ``__syscall6``
ABI as one fixed inline-asm shape: ``rax``=nr, args in
``rdi/rsi/rdx/r10/r8/r9``, ``rcx/r11/memory`` clobbered, raw return in
``rax``.  Linux x86_64 only; Darwin raw syscalls are unsupported by
policy (macOS code keeps named libSystem externs).

Covers on the host:
- the exact inline-asm IR shape the llvm_capi builder emits;
- the x86_64-linux self-backend lowering (register loads + ``syscall``);
- the aarch64 self backend failing closed on the parsed kind;
- the parser failing closed on any other inline-asm shape;
- the frontend platform policy for ``pcc.unsafe.syscall6`` calls.

Real-machine execution is proven by the docker differential in
``tests/integration/test_self_backend_x86_64_linux.py``.
"""
from __future__ import annotations

import sys
import textwrap

import pytest


X86_64_LINUX_TRIPLE = "x86_64-unknown-linux-gnu"
MSG = b"pcc syscall6 ok\n"


def _build_syscall6_module(triple: str = X86_64_LINUX_TRIPLE) -> str:
    from pcc.ir import ir

    mod = ir.Module(name="syscall6_smoke")
    mod.triple = triple
    i8 = ir.IntType(8)
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    arr_ty = ir.ArrayType(i8, len(MSG))
    gv = ir.GlobalVariable(mod, arr_ty, name="pcc_syscall6_msg")
    gv.initializer = ir.Constant(arr_ty, bytearray(MSG))
    gv.global_constant = True
    fn = ir.Function(mod, ir.FunctionType(i32, []), name="main")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    ptr = builder.ptrtoint(gv, i64, name="msgaddr")
    zero = ir.Constant(i64, 0)
    ret = builder.syscall6(
        ir.Constant(i64, 1),  # SYS_write
        ir.Constant(i64, 1),  # stdout
        ptr,
        ir.Constant(i64, len(MSG)),
        zero,
        zero,
        zero,
        name="written",
    )
    builder.ret(builder.trunc(ret, i32, name="code"))
    return str(mod)


def test_syscall6_builder_emits_the_pinned_inline_asm_shape():
    ir_text = _build_syscall6_module()
    assert (
        'call i64 asm sideeffect "syscall", '
        '"={rax},{rax},{rdi},{rsi},{rdx},{r10},{r8},{r9},'
        '~{rcx},~{r11},~{memory}"'
    ) in ir_text
    assert "(i64 1, i64 1, i64 %msgaddr" in ir_text


def test_syscall6_lowers_to_x86_64_linux_syscall_sequence():
    from pcc.backend.self_backend_dispatch import emit_self_asm

    asm = emit_self_asm(_build_syscall6_module())
    assert "  syscall" in asm
    body = asm[: asm.index("  syscall")]
    # musl ABI register loads, each present before the syscall itself.
    for needle in (
        "mov rax, 1",
        "mov rdi, 1",
        "mov rsi, QWORD PTR",
        "mov rdx, 16",
        "mov r10, 0",
        "mov r8, 0",
        "mov r9, 0",
    ):
        assert needle in body, f"missing {needle!r} before syscall:\n{asm}"
    # the raw return value must come back out of rax into the dest slot.
    after = asm[asm.index("  syscall") :]
    assert "], rax" in after, f"missing rax spill after syscall:\n{asm}"


def test_syscall6_fails_closed_on_the_aarch64_self_backend():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_dispatch import emit_self_asm

    with pytest.raises(BackendUnavailable, match="syscall6"):
        emit_self_asm(_build_syscall6_module(triple="arm64-apple-darwin"))


def test_other_inline_asm_shapes_fail_closed_in_the_parser():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_dispatch import emit_self_asm

    ir_text = _build_syscall6_module().replace(
        '"syscall", "={rax},{rax},{rdi},{rsi},{rdx},{r10},{r8},{r9},'
        '~{rcx},~{r11},~{memory}"',
        '"rdtsc", "={rax}"',
    ).replace("(i64 1, i64 1, i64 %msgaddr.1, i64 16, i64 0, i64 0, i64 0)", "()")
    with pytest.raises(BackendUnavailable, match="inline asm shape"):
        emit_self_asm(ir_text)


def test_syscall6_frontend_policy_matches_the_target(tmp_path):
    """Darwin (and any non-linux-x86_64 host) must reject the intrinsic."""
    import platform

    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "syscall6_policy.py"
    out = tmp_path / "syscall6_policy.ll"
    src.write_text(
        textwrap.dedent(
            """
            from pcc.unsafe import malloc, syscall6

            def main() -> None:
                p = malloc(8)
                print(syscall6(1, 1, p, 0, 0, 0, 0))

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    host_is_linux_x86_64 = sys.platform.startswith("linux") and platform.machine() in (
        "x86_64",
        "amd64",
    )
    if host_is_linux_x86_64:
        compile_python(
            str(src), str(out), emit_llvm_only=True, libpython_mode="off",
        )
        assert 'asm sideeffect "syscall"' in out.read_text(encoding="utf-8")
    else:
        with pytest.raises(NotImplementedError, match="Linux x86_64 only"):
            compile_python(
                str(src), str(out), emit_llvm_only=True, libpython_mode="off",
            )
