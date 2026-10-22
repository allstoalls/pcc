"""Raw Linux syscalls retain structured capture, ABI and emitted execution."""

import platform
import subprocess
import sys

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.ir import ir


TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
MESSAGE = b"direct syscall output\n"


def _module(monkeypatch, target, *, direct=True, no_text=True, fuse_uses=True):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if no_text else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1" if fuse_uses else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "1")
    module = ir.Module(name="direct-syscall")
    module.triple = target
    i64 = ir.IntType(64)
    i32 = ir.IntType(32)
    array = ir.ArrayType(ir.IntType(8), len(MESSAGE))
    message = ir.GlobalVariable(module, array, name="message")
    message.global_constant = True
    message.initializer = ir.Constant(array, bytearray(MESSAGE))
    function = ir.Function(module, ir.FunctionType(i32, []), name="main")
    entry = function.append_basic_block("activation.entry")
    body = function.append_basic_block("write")
    builder = ir.IRBuilder(entry)
    builder.branch(body)
    builder.position_at_end(body)
    pointer = builder.ptrtoint(message, i64, name="address")
    zero = ir.Constant(i64, 0)
    result = builder.syscall6(
        ir.Constant(i64, 64 if target.startswith("aarch64") else 1),
        ir.Constant(i64, 1), pointer, ir.Constant(i64, len(MESSAGE)),
        zero, zero, zero, name="written",
    )
    builder.ret(builder.trunc(result, i32, name="status"))
    return module


def _assembly(module):
    from pcc.backend.target_objects import emit_indexed_assembly
    return emit_indexed_assembly(module, optimize=False)


def _object(assembly, target):
    from pcc.backend.target_objects import encode_assembly_object
    return encode_assembly_object(assembly, target)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("fuse_uses", [False, True])
def test_syscall_no_text_capture_never_parses_function(monkeypatch, target, fuse_uses):
    import pcc.ir.direct_indexed_kernel as capture

    def forbidden(*args, **kwargs):
        raise AssertionError("syscall direct capture attempted function text fallback")

    source = _module(monkeypatch, target, fuse_uses=fuse_uses)
    records = [record for block in source.functions[0].blocks for record in block._instrs]
    assert all(record._direct_record_id >= 0 for record in records)
    syscall = next(record for record in records if record.opname == "syscall6")
    assert syscall.text == ""
    monkeypatch.setattr(capture, "build_indexed_function_seed_from_block_lines", forbidden)
    direct = source.direct_indexed_module()
    assert source._direct_indexed_fallback_records == 0
    verify_parsed_module(direct)
    assembly = _assembly(direct)
    assert "  " + ("svc #0" if target.startswith("aarch64") else "syscall") in assembly


@pytest.mark.parametrize("target", TARGETS)
def test_syscall_capture_text_codec_and_object_parity(monkeypatch, tmp_path, target):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    text = str(_module(monkeypatch, target, direct=False, no_text=False))
    source = _module(monkeypatch, target)
    direct = source.direct_indexed_module()
    parsed = parse_self_backend_module(text)
    for module in (direct, parsed):
        verify_parsed_module(module)
    direct_kernel = get_indexed_function_kernel(direct.functions[0])
    parsed_kernel = get_indexed_function_kernel(parsed.functions[0])
    assert direct_kernel.diagnostic_instruction(1, 1).data == parsed_kernel.diagnostic_instruction(1, 1).data
    path = tmp_path / "syscall.pidx"
    encode_indexed_module_file(str(path), direct)
    decoded = decode_indexed_module_file(str(path))
    verify_parsed_module(decoded)
    expected = _object(emit_self_asm(text), target)
    assert _object(_assembly(direct), target) == expected
    assert _object(_assembly(decoded), target) == expected


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_direct_syscall_owned_linux_native_output(monkeypatch, tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable
    from pcc.backend.owned_elf_link import assemble

    target = TARGETS[0]
    source = _module(monkeypatch, target)
    assembly = _assembly(source.direct_indexed_module())
    startup = '''.intel_syntax noprefix
.text
.globl _start
_start:
  call main
  mov rdi, rax
  mov rax, 60
  syscall
'''
    image = link_static_executable([assemble(assembly, target), assemble(startup, target)])
    executable = tmp_path / "syscall"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.stdout == MESSAGE
    assert result.stderr == b""
    assert result.returncode == len(MESSAGE)


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("width", [8, 32])
def test_syscall_builder_rejects_non_i64_operands(monkeypatch, direct, width):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    module = ir.Module(name="bad-syscall")
    module.triple = TARGETS[0]
    function = ir.Function(module, ir.FunctionType(ir.IntType(64), []), name="bad")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    zero = ir.Constant(ir.IntType(64), 0)
    with pytest.raises(ValueError, match="syscall6 argument must be i64"):
        builder.syscall6(zero, zero, zero, ir.Constant(ir.IntType(width), 1), zero, zero, zero)
    assert not function.blocks[0]._instrs


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("change", ["opcode", "constraints", "sideeffect", "count", "type"])
def test_malformed_syscall_asm_fails_closed(monkeypatch, target, change):
    text = str(_module(monkeypatch, target, direct=False, no_text=False))
    if change == "opcode":
        text = text.replace('"svc #0"', '"svc #1"').replace('"syscall"', '"sysenter"')
    elif change == "constraints":
        text = text.replace('~{memory}', '~{bogus}')
    elif change == "sideeffect":
        text = text.replace('asm sideeffect', 'asm')
    elif change == "count":
        text = text.replace(', i64 0, i64 0, i64 0)', ', i64 0, i64 0)')
    else:
        text = text.replace(', i64 0, i64 0, i64 0)', ', i32 0, i64 0, i64 0)')
    with pytest.raises(BackendUnavailable, match="inline asm shape|syscall6"):
        parse_self_backend_module(text)


def _frontend_write_object(tmp_path, monkeypatch, target, *, freestanding):
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    source = tmp_path / "raw_write.py"
    if freestanding:
        source.write_text(
            "from pcc.extern import c_abi_export\n"
            "from pcc.unsafe import cstr, write\n"
            "__pcc_freestanding__ = True\n"
            "@c_abi_export('main')\n"
            "def main() -> i64:\n"
            "    return write(1, cstr('ordinary unsafe write\\n'), 22)\n"
        )
    else:
        source.write_text(
            "from pcc.unsafe import cstr, write\n"
            "def main() -> None:\n"
            "    written = write(1, cstr('ordinary unsafe write\\n'), 22)\n"
            "    assert written == 22\n"
            "main()\n"
        )
    original_generate = L1CodeGen.generate

    def generate_for_target(self, module=None):
        assert module is self.ast_module
        self._target_triple = target
        return original_generate(self)

    monkeypatch.setattr(L1CodeGen, "generate", generate_for_target)
    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
                "PCC_DIRECT_INDEXED_NATIVE_OBJECT"):
        monkeypatch.setenv(key, "1")
    for key in ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "PCC_TEXT_INDEXED_KERNEL_EMIT",
                "PCC_DIRECT_INDEXED_SIDECAR"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "off")
    original_capture = ir.Module.direct_indexed_module
    captured = []

    def capture(module):
        result = original_capture(module)
        assert module._direct_indexed_fallback_records == 0
        records = [record for function in module.functions for block in function.blocks
                   for record in block._instrs if record.opname == "syscall6"]
        assert records and all(record._direct_record_id >= 0 and not record.text for record in records)
        captured.append(len(records))
        return result

    monkeypatch.setattr(ir.Module, "direct_indexed_module", capture)
    manifest = tmp_path / "worker.manifest"
    result = tmp_path / "result.tsv"
    pipeline._write_python_frontend_worker_manifest(
        str(manifest), str(result), str(tmp_path), "", "", [str(source)],
        ["raw_write"], [0], entry_module="raw_write", sibling_inits=(),
        libpython_mode="off", ir_scaffold_mode="on", verbose=False,
    )
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    assert captured
    assert (tmp_path / "module_0.ll").read_text() == ""
    object_path = tmp_path / "module_0.direct.pco"
    assert object_path.stat().st_size > 0
    return object_path


@pytest.mark.parametrize("target", TARGETS)
def test_ordinary_unsafe_write_worker_has_zero_capture_fallback(monkeypatch, tmp_path, target):
    from pcc.backend.elf_x86_64 import parse_relocatable

    obj = _frontend_write_object(tmp_path, monkeypatch, target, freestanding=False)
    assert parse_relocatable(obj.read_bytes()).machine == (183 if target.startswith("aarch64") else 62)


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_freestanding_unsafe_write_owned_linux_native_output(monkeypatch, tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.owned_elf_link import assemble

    target = TARGETS[0]
    obj = _frontend_write_object(tmp_path, monkeypatch, target, freestanding=True)
    startup = '''.intel_syntax noprefix
.text
.globl _start
_start:
  call main
  mov rdi, rax
  mov rax, 60
  syscall
'''
    image = link_static_executable([parse_relocatable(obj.read_bytes()), assemble(startup, target)])
    executable = tmp_path / "unsafe-write"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.stdout == b"ordinary unsafe write\n"
    assert result.stderr == b""
    assert result.returncode == 22


@pytest.mark.parametrize("optimize", [False, True])
def test_aarch64_syscall_transport_has_no_instruction_fallback(monkeypatch, tmp_path, optimize):
    from pcc.backend.arm64_asm_driver import assemble_lines
    from pcc.backend.arm64_elf_driver import from_sections
    from pcc.backend.elf_x86_64 import emit_relocatable
    from pcc.backend.self_backend_aarch64_darwin import (
        emit_aarch64_darwin_asm, emit_aarch64_darwin_indexed_transport,
    )

    target = TARGETS[1]
    text = str(_module(monkeypatch, target, direct=False, no_text=False))
    direct = _module(monkeypatch, target).direct_indexed_module()
    path = tmp_path / "syscall.pidx"
    encode_indexed_module_file(str(path), direct)
    for module in (direct, decode_indexed_module_file(str(path))):
        transport = emit_aarch64_darwin_indexed_transport(module, optimize=optimize)
        assert transport.fallback_instruction_count == 0
        assert transport.fallback_instruction_lines == ()
        if not optimize:
            assert transport.native_finalized
            assert transport.line_chunks == []
        sections, undefined = assemble_lines(
            transport.line_chunks, transport.structured_sections,
            transport.encoded_line_records, transport.structured_symbol_names,
        )
        encoded = emit_relocatable(from_sections(sections, undefined))
        assert encoded == _object(emit_aarch64_darwin_asm(text, optimize=optimize), target)
        assert b"\x01\x00\x00\xd4" in encoded


@pytest.mark.parametrize("fuse_uses", [False, True])
def test_syscall_records_all_seven_dynamic_operand_uses(monkeypatch, fuse_uses):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1" if fuse_uses else "0")
    module = ir.Module(name="syscall-uses")
    module.triple = TARGETS[0]
    i64 = ir.IntType(64)
    function = ir.Function(module, ir.FunctionType(i64, [i64] * 7), name="raw")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    result = builder.syscall6(*function.args)
    builder.ret(result)
    direct = module.direct_indexed_module()
    verify_parsed_module(direct)
    kernel = get_indexed_function_kernel(direct.functions[0])
    assert kernel.instruction_use_count(0, 0) == 7
    assert [kernel.instruction_use_id(0, 0, i) for i in range(7)] == list(range(7))
    assert kernel.terminator_use_id(0, 0) == kernel.defined_value_id(0, 0)


@pytest.mark.parametrize("count", [0, 6, 8])
def test_direct_syscall_publisher_rejects_wrong_arity(monkeypatch, count):
    from pcc.ir.direct_indexed_kernel import DirectIndexedFunctionBuilder

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    module = ir.Module(name="syscall-arity")
    i64 = ir.IntType(64)
    function = ir.Function(module, ir.FunctionType(i64, []), name="raw")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    direct_builder = builder._direct_builder_plane()
    dest = builder._next("result", i64)
    with pytest.raises(BackendUnavailable, match="syscall6 expects 7 arguments"):
        DirectIndexedFunctionBuilder.publish_syscall6(
            direct_builder, dest, [ir.Constant(i64, 0)] * count,
        )
    assert direct_builder.supported_records == 0
