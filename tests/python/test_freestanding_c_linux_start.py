"""Linux process entry for C programs using the pcc-Python libc."""

from __future__ import annotations

import re
from pathlib import Path

from pcc.frontends.python import pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_c_linux_start.py"
LINUX_TRIPLE = "x86_64-unknown-linux-gnu"


def _compile_linux_ir(tmp_path: Path, monkeypatch) -> str:
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    output = tmp_path / "freestanding_c_linux_start.ll"
    pipeline.compile_python(
        str(SOURCE),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        backend="self",
        target_triple=LINUX_TRIPLE,
    )
    return output.read_text(encoding="utf-8")


def test_c_linux_start_is_freestanding_python_and_calls_c_main(tmp_path, monkeypatch):
    ir_text = _compile_linux_ir(tmp_path, monkeypatch)

    assert SOURCE.with_suffix(".c").exists() is False
    assert SOURCE.with_suffix(".s").exists() is False
    assert f'target triple = "{LINUX_TRIPLE}"' in ir_text
    assert re.search(r"define (?:external )?void @_start\(ptr %[^)]+\)", ir_text)
    assert re.search(r"declare (?:external )?i32 @main\(i32, ptr, ptr\)", ir_text)
    assert re.search(
        r"call i32 \(i32, ptr, ptr\) @main\(i32 .*, ptr .*, ptr .*\)",
        ir_text,
    )
    assert "@pcc_platform_env_init(ptr" in ir_text
    assert "call i32 (ptr) @pcc_platform_env_init(" in ir_text
    assert "syscall" in ir_text


def test_c_linux_start_establishes_static_tls_before_runtime_calls(
    tmp_path, monkeypatch
):
    ir_text = _compile_linux_ir(tmp_path, monkeypatch)
    source = SOURCE.read_text(encoding="utf-8")

    assert "@pcc_linux_tls_image" in ir_text
    assert "@pcc_linux_allocate_tls" in ir_text
    assert "page_alloc(allocation_size)" in source
    assert '@c_abi_export("pcc_linux_initial_tls_setup")' in source
    assert "load_i64(auxv, aux_index * 16)" in source
    assert "load_i32(program_header, 0) == 7" in source  # PT_TLS
    assert "load_i64(program_header, 32)" in source  # p_filesz
    assert "load_i64(program_header, 40)" in source  # p_memsz
    assert "load_i64(program_header, 48)" in source  # p_align
    assert "load_i8(tls_template, copy_index)" in source
    assert "i64 158" in ir_text  # SYS_arch_prctl
    assert "i64 4098" in ir_text  # ARCH_SET_FS
    start_body = ir_text.split("define external void @_start", 1)[1]
    tls_setup = start_body.index("@pcc_linux_initial_tls_setup")
    env_init = start_body.index("@pcc_platform_env_init")
    assert tls_setup < env_init


def test_c_linux_start_preserves_kernel_stack_contract(tmp_path, monkeypatch):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    asm = emit_self_asm(_compile_linux_ir(tmp_path, monkeypatch), LINUX_TRIPLE)
    start_body = asm.split("_start:", 1)[1].split(".size _start", 1)[0]
    assert "  mov r11, rsp" in start_body
    assert "  and rsp, -16" in start_body
    assert "  call main" in start_body
    assert "  call pcc_platform_env_init" in start_body
    assert start_body.count("  syscall") >= 2
    assert "  ud2" in start_body
    assert "  ret" not in start_body
