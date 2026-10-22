from __future__ import annotations

import re
from pathlib import Path

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_linux_start.py"
LINUX_TRIPLE = "x86_64-unknown-linux-gnu"


def test_none_return_type_survives_self_host_module_class_boundary():
    from pcc.frontends.python.codegen.user_function_decl_lowering import (
        _is_none_semantic_type,
    )

    class IndependentlyCompiledNoneType:
        name = "None"

    assert _is_none_semantic_type(IndependentlyCompiledNoneType()) is True
    assert _is_none_semantic_type(None) is True
    assert _is_none_semantic_type(object()) is False


def _compile_linux_ir(tmp_path: Path, monkeypatch) -> str:
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    output = tmp_path / "freestanding_linux_start.ll"
    pipeline.compile_python(
        str(SOURCE),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return "\n".join(
        f'target triple = "{LINUX_TRIPLE}"'
        if line.startswith("target triple = ")
        else line
        for line in output.read_text(encoding="utf-8").splitlines()
    )


def test_linux_start_is_freestanding_python_with_no_owned_c_or_asm_source():
    source = SOURCE.read_text(encoding="utf-8")
    assert "__pcc_freestanding__ = True" in source
    assert '@c_abi_export("_start")' in source
    assert "load_i64(initial_stack, 0)" in source
    assert "load_ptr(initial_stack, 8)" in source
    assert "process_exit(status)" in source
    assert not SOURCE.with_suffix(".c").exists()
    assert not SOURCE.with_suffix(".s").exists()


def test_linux_start_lowers_to_raw_syscalls_and_kernel_stack_contract(
    tmp_path, monkeypatch
):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    ir_text = _compile_linux_ir(tmp_path, monkeypatch)
    declarations = [line for line in ir_text.splitlines() if line.startswith("declare ")]
    assert all("@write(" not in line for line in declarations)
    assert all("@_exit(" not in line for line in declarations)
    assert ir_text.count("syscall") >= 2
    assert re.search(r"define void @_start\(ptr %[^)]+\)", ir_text)

    asm = emit_self_asm(ir_text, LINUX_TRIPLE)
    start_body = asm.split("_start:", 1)[1].split(".size _start", 1)[0]
    assert "  mov r11, rsp" in start_body
    assert "  and rsp, -16" in start_body
    assert "  syscall" in start_body
    assert "  ud2" in start_body
    assert "  ret" not in start_body


def test_python_cli_propagates_explicit_cross_target_to_emitted_ir(
    tmp_path, monkeypatch
):
    from pcc.driver import cli_core
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    output = tmp_path / "cli-linux-start.ll"
    result = cli_core.execute_cli(
        path=str(SOURCE),
        emit_llvm=str(output),
        target_triple=LINUX_TRIPLE,
        backend="self",
        python_library=True,
        python_libpython="off",
    )

    assert result == 0
    assert f'target triple = "{LINUX_TRIPLE}"' in output.read_text(
        encoding="utf-8"
    )


def test_python_cli_launcher_propagates_explicit_cross_target(
    tmp_path, monkeypatch
):
    from pcc.driver import cli_core
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    output = tmp_path / "launcher-linux-start.ll"
    result = cli_core.cli_main(
        [
            "--backend=self",
            "--target",
            LINUX_TRIPLE,
            "--python-library",
            "--python-libpython=off",
            "--emit-llvm=" + str(output),
            str(SOURCE),
        ]
    )

    assert result == 0
    assert f'target triple = "{LINUX_TRIPLE}"' in output.read_text(
        encoding="utf-8"
    )


def test_multi_file_python_emit_applies_cross_target_to_every_module(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    output = tmp_path / "multi-linux.ll"
    first.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    second.write_text("def value() -> int:\n    return 2\n", encoding="utf-8")

    pipeline.compile_python_multi(
        [str(first), str(second)],
        str(output),
        emit_llvm_only=True,
        entry_module="pkg.first",
        module_names=["pkg.first", "pkg.second"],
        libpython_mode="off",
        target_triple=LINUX_TRIPLE,
    )

    ir_text = output.read_text(encoding="utf-8")
    assert ir_text.count(f'target triple = "{LINUX_TRIPLE}"') == 2
    assert 'target triple = "unknown-unknown-unknown"' not in ir_text
