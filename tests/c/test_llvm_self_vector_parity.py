from __future__ import annotations

import builtins
from pathlib import Path
import re
import subprocess
from unittest.mock import patch

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_targets import classify_self_backend_target_triple
from pcc.frontends.python.pipeline_targets import host_target_triple
from tests.c_testsuite_cases import PccCompileResult, _host_cc, subprocess_env
from tests.self_backend_c_testsuite_common import assert_result_triplet_matches

pytestmark = pytest.mark.xdist_group(name="llvm_self_vector_parity")


INT_VECTOR_IR = """
define i32 @main() {
entry:
  %ptr = alloca <4 x i32>, align 16
  store <4 x i32> <i32 1, i32 3, i32 5, i32 7>, ptr %ptr, align 16
  %lane = load <4 x i32>, ptr %ptr, align 16
  %elem = extractelement <4 x i32> %lane, i32 2
  ret i32 %elem
}
"""

PTR_VECTOR_IR = """
@a = global i32 11
@b = global i32 22
@c = global i32 33
@d = global i32 44

define i32 @main() {
entry:
  %slots = alloca <4 x ptr>, align 32
  %v0 = insertelement <4 x ptr> poison, ptr @a, i32 0
  %v1 = insertelement <4 x ptr> %v0, ptr @b, i32 1
  %v2 = insertelement <4 x ptr> %v1, ptr @c, i32 2
  %v3 = insertelement <4 x ptr> %v2, ptr @d, i32 3
  store <4 x ptr> %v3, ptr %slots, align 32
  %loaded = load <4 x ptr>, ptr %slots, align 32
  %elem_ptr = extractelement <4 x ptr> %loaded, i32 2
  %elem = load i32, ptr %elem_ptr
  ret i32 %elem
}
"""


REGALLOC_INT_IR = """
define i32 @main() {
entry:
  %a = add i32 7, 5
  %b = mul i32 %a, 3
  %c = xor i32 %b, 10
  %d = sub i32 %c, 4
  ret i32 %d
}
"""


def _host_triple() -> str:
    return host_target_triple()


def _ensure_target_triple(ir_text: str, triple: str) -> str:
    if re.search(r"^target triple = ", ir_text, re.M):
        return ir_text
    return f'target triple = "{triple}"\n' + ir_text


def _result_from_completed_process(
    process: subprocess.CompletedProcess[str],
) -> PccCompileResult:
    return PccCompileResult(
        process.returncode,
        process.stdout,
        (process.stdout if process.returncode == 0 else process.stderr),
    )


def _run_llvm_from_ir(ir_text: str, tmp_path, triple: str) -> PccCompileResult:
    # External LLVM/cc is a reference oracle only, never the self build owner.
    cc = _host_cc()
    ir = _ensure_target_triple(ir_text, triple)
    executable_path = tmp_path / "llvm_case.out"
    ir_path = tmp_path / "llvm_case.ll"
    ir_path.write_text(ir, encoding="utf-8")

    compile_process = subprocess.run(
        [cc, "-x", "ir", str(ir_path), "-o", str(executable_path)],
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compile_process.returncode == 0, (
        "external LLVM reference oracle compile/link failed before execution "
        f"(returncode={compile_process.returncode}):\n{compile_process.stderr}"
    )
    assert executable_path.is_file(), "external LLVM reference oracle emitted no executable"

    return _result_from_completed_process(
        subprocess.run(
            [str(executable_path)],
            env=subprocess_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    )


def _run_self_from_ir(
    ir_text: str, tmp_path, triple: str, runtime_archive: Path,
) -> PccCompileResult:
    """Assemble the original self assembly and link it with PCC-owned tools.

    The archive is supplied by the provenance-checking test fixture. No runtime
    is provisioned here, and build errors cannot masquerade as program exits.
    """
    executable_path = tmp_path / "self_case.out"
    asm_path = tmp_path / "self_case.s"
    object_path = tmp_path / "self_case.o"
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite."):
            raise AssertionError("owned self vector build imported " + name)
        return original_import(name, *args, **kwargs)

    def forbidden_process(*args, **kwargs):
        raise AssertionError("owned self vector build attempted an external process")

    stage = "emit assembly"
    try:
        with (
            patch("builtins.__import__", checked_import),
            patch("subprocess.Popen", forbidden_process),
        ):
            asm = emit_self_asm(_ensure_target_triple(ir_text, triple))
            asm_path.write_text(asm, encoding="utf-8")
            target = _self_target_identity(triple)
            stage = "assemble object"
            if target in (
                "self-aarch64-linux-v0", "self-x86_64-linux-v0",
                "self-x86_64-windows-v0",
            ):
                from pcc.backend.target_objects import encode_assembly_object

                object_path.write_bytes(encode_assembly_object(asm, triple))
                stage = "link executable"
                if target == "self-x86_64-windows-v0":
                    from pcc.backend.owned_pe_link import link_inputs

                    link_inputs(output=str(executable_path), objects=[str(object_path)],
                                archives=[str(runtime_archive)])
                else:
                    from pcc.backend.owned_elf_link import link_inputs

                    link_inputs(target=triple, output=str(executable_path),
                                objects=[str(object_path)], archives=[str(runtime_archive)])
            elif target == "self-aarch64-darwin-v0":
                from pcc.backend.arm64_asm_driver import assemble_file
                from pcc.backend.macho_exec import link_executable
                from pcc.backend.native_object import NativeObject

                sections, undefined = assemble_file(asm)
                obj = NativeObject.from_sections(sections, undefined=undefined)
                stage = "link executable"
                executable_path.write_bytes(link_executable([obj], entry="_main"))
                executable_path.chmod(0o755)
            else:
                raise AssertionError("unsupported owned self vector target: " + triple)
    except Exception as exc:
        raise AssertionError(f"owned self vector {stage} failed before execution: {exc}") from exc

    assert executable_path.is_file(), "owned self vector linker emitted no executable"
    # Executing the emitted program is deliberately outside the build guard.
    return _result_from_completed_process(
        subprocess.run(
            [str(executable_path)],
            env=subprocess_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    )


def _self_target_identity(triple: str) -> str:
    """The registry's platform verdict for ``triple``: its target identity."""
    verdict = classify_self_backend_target_triple(triple)
    if not verdict.supported:
        pytest.fail(verdict.skip_reason())
    assert verdict.target_identity is not None
    return verdict.target_identity


def _host_self_supported() -> str:
    triple = _host_triple()
    _self_target_identity(triple)
    return triple


def test_llvm_self_int_vector_lane_matches(tmp_path, pcc_runtime_archive):
    triple = _host_self_supported()
    llvm_result = _run_llvm_from_ir(INT_VECTOR_IR, tmp_path, triple)
    self_result = _run_self_from_ir(INT_VECTOR_IR, tmp_path, triple, pcc_runtime_archive)
    assert_result_triplet_matches(
        "llvm-self-int-vector", "llvm", llvm_result, "self", self_result
    )


def test_llvm_self_ptr_vector_lane_matches(tmp_path, pcc_runtime_archive):
    triple = _host_self_supported()
    llvm_result = _run_llvm_from_ir(PTR_VECTOR_IR, tmp_path, triple)
    self_result = _run_self_from_ir(PTR_VECTOR_IR, tmp_path, triple, pcc_runtime_archive)
    assert_result_triplet_matches(
        "llvm-self-ptr-vector", "llvm", llvm_result, "self", self_result
    )


def test_llvm_self_block_local_regalloc_result_matches(tmp_path, pcc_runtime_archive):
    triple = _host_self_supported()
    llvm_result = _run_llvm_from_ir(REGALLOC_INT_IR, tmp_path, triple)
    self_result = _run_self_from_ir(REGALLOC_INT_IR, tmp_path, triple, pcc_runtime_archive)
    assert_result_triplet_matches(
        "llvm-self-block-local-regalloc", "llvm", llvm_result, "self", self_result
    )


@pytest.mark.parametrize(
    ("ir_text", "expected"),
    [(INT_VECTOR_IR, 5), (PTR_VECTOR_IR, 33), (REGALLOC_INT_IR, 42)],
    ids=["int-vector", "ptr-vector", "block-local-regalloc"],
)
def test_owned_vector_program_result(ir_text, expected, tmp_path, pcc_runtime_archive):
    result = _run_self_from_ir(ir_text, tmp_path, _host_self_supported(), pcc_runtime_archive)
    assert (result.returncode, result.stdout, result.stderr) == (expected, "", "")


def test_llvm_oracle_compile_failure_never_becomes_execution(tmp_path, monkeypatch):
    calls = []

    def failed_compile(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "oracle compiler failed")

    monkeypatch.setitem(_run_llvm_from_ir.__globals__, "_host_cc", lambda: "oracle-cc")
    monkeypatch.setattr(subprocess, "run", failed_compile)
    with pytest.raises(AssertionError, match="reference oracle compile/link failed before execution"):
        _run_llvm_from_ir(INT_VECTOR_IR, tmp_path, _host_triple())
    assert len(calls) == 1
    assert calls[0][0] == "oracle-cc"


@pytest.mark.parametrize("failure", ["emit", "process", "llvmlite"])
def test_owned_vector_build_failure_never_becomes_execution(tmp_path, monkeypatch, failure):
    def failed_emit(_ir):
        if failure == "process":
            subprocess.Popen(["forbidden-compiler"])
        if failure == "llvmlite":
            __import__("llvmlite.binding")
        raise ValueError("emitter failed")

    def forbidden_run(*args, **kwargs):
        raise AssertionError("program execution attempted after failed build")

    monkeypatch.setitem(_run_self_from_ir.__globals__, "emit_self_asm", failed_emit)
    monkeypatch.setattr(subprocess, "run", forbidden_run)
    expected = {"emit": "emitter failed", "process": "external process", "llvmlite": "imported llvmlite"}[failure]
    with pytest.raises(AssertionError, match="emit assembly failed before execution: .*" + expected):
        _run_self_from_ir(INT_VECTOR_IR, tmp_path, _host_triple(), tmp_path / "unused.a")
