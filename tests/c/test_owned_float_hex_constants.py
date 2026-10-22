"""LLVM float tokens are widened binary64 spellings, never raw float bits."""

import platform
import subprocess
import sys

import pytest

from pcc.backend.elf_x86_64 import link_static_executable
from pcc.backend.owned_elf_link import assemble
from pcc.backend.self_backend_ir import TypeDesc
from pcc.backend.self_backend_parse import aggregate_literal_to_bytes
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.backend.wide_float import encode_float_bits


_FLOAT_CONSTANTS = [
    pytest.param("0x0000000000000000", 0x00000000, id="positive-zero"),
    pytest.param("0x8000000000000000", 0x80000000, id="negative-zero"),
    pytest.param("0x3FF0000000000000", 0x3F800000, id="one"),
    pytest.param("0x3FF8000000000000", 0x3FC00000, id="one-and-a-half"),
    pytest.param("0x7FF0000000000000", 0x7F800000, id="positive-infinity"),
    pytest.param("0xFFF0000000000000", 0xFF800000, id="negative-infinity"),
    pytest.param("0x7FF8000000000000", 0x7FC00000, id="quiet-nan"),
    pytest.param("0xFFF8000000000000", 0xFFC00000, id="negative-quiet-nan"),
    pytest.param("0x7FF82468A0000000", 0x7FC12345, id="quiet-nan-payload"),
    pytest.param("0xFFF82468A0000000", 0xFFC12345, id="negative-nan-payload"),
    pytest.param("0x7FF42468A0000000", 0x7FA12345, id="signaling-nan-payload"),
    pytest.param("0xFFF42468A0000000", 0xFFA12345, id="negative-signaling-nan"),
    pytest.param("0x36A0000000000000", 0x00000001, id="smallest-subnormal"),
]


@pytest.mark.parametrize("token,bits", _FLOAT_CONSTANTS)
def test_float_hex_aggregate_literals_use_widened_binary64_tokens(token, bits):
    float_type = TypeDesc("fp", 32)
    vector_type = TypeDesc("array", count=2, elem=float_type)
    expected = bits.to_bytes(4, "little")
    assert encode_float_bits(token, 32) == bits
    assert aggregate_literal_to_bytes(float_type, token) == expected
    assert aggregate_literal_to_bytes(
        vector_type, f"<float {token}, float {token}>"
    ) == expected + expected


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64")
)
@pytest.mark.parametrize("token,bits", _FLOAT_CONSTANTS)
def test_owned_x86_float_hex_scalar_aggregate_and_vector_lane_execute(
    tmp_path, monkeypatch, token, bits
):
    target = "x86_64-unknown-linux-gnu"
    ir_text = f'''
target triple = "{target}"

@global_float = global float {token}
@global_vector = global <2 x float> <float {token}, float {token}>

define i32 @probe() {{
entry:
  %scalar = alloca float
  %aggregate = alloca <2 x float>
  %lane_slot = alloca <2 x float>
  store float {token}, ptr %scalar
  %loaded_aggregate = load <2 x float>, ptr @global_vector
  store <2 x float> %loaded_aggregate, ptr %aggregate
  %shuffled = shufflevector <2 x float> <float {token}, float {token}>, <2 x float> zeroinitializer, <2 x i32> zeroinitializer
  store <2 x float> %shuffled, ptr %lane_slot
  %lane_address = getelementptr i8, ptr %lane_slot, i64 4
  %scalar_bits = load i32, ptr %scalar
  %aggregate_bits = load i32, ptr %aggregate
  %lane_bits = load i32, ptr %lane_address
  %global_bits = load i32, ptr @global_float
  %scalar_ok = icmp eq i32 %scalar_bits, {bits}
  %aggregate_ok = icmp eq i32 %aggregate_bits, {bits}
  %lane_ok = icmp eq i32 %lane_bits, {bits}
  %global_ok = icmp eq i32 %global_bits, {bits}
  %first_pair = and i1 %scalar_ok, %aggregate_ok
  %second_pair = and i1 %lane_ok, %global_ok
  %all_ok = and i1 %first_pair, %second_pair
  %failed = xor i1 %all_ok, true
  %result = zext i1 %failed to i32
  ret i32 %result
}}
'''
    startup = '''.intel_syntax noprefix
.text
.globl _start
_start:
  call probe
  mov rdi, rax
  mov rax, 60
  syscall
'''

    def forbid_compiler_process(*args, **kwargs):
        raise AssertionError("float regression invoked an external compiler")

    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", forbid_compiler_process)
        assembly = emit_x86_64_linux_asm(ir_text)
        image = link_static_executable(
            [assemble(assembly, target), assemble(startup, target)]
        )
    executable = tmp_path / "float_constants"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


_FP80_CONSTANTS = [
    0x00000000000000000000,
    0x80000000000000000000,
    0x4003F8CCCCCCCCCCCCCD,
    0x7FFF8000000000000000,
    0xFFFFC000000000000123,
    0x7FFFA000000000000123,
    0x00000000000000000001,
]


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64")
)
@pytest.mark.parametrize("bits", _FP80_CONSTANTS)
def test_owned_fp80_global_storage_payload_padding_offsets_and_relocations_execute(
    tmp_path, monkeypatch, bits
):
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_indexed_codec import (
        decode_indexed_module_file, encode_indexed_module_file,
    )
    from pcc.backend.self_backend_x86_64_linux import _emit_x86_64_module

    target = "x86_64-unknown-linux-gnu"
    token = "0xK" + format(bits, "020X")
    text = f'''target triple = "{target}"
%Envelope = type {{ i8, [2 x x86_fp80], ptr }}
@scalar = global x86_fp80 {token}
@record = global %Envelope {{ i8 7, [2 x x86_fp80] [x86_fp80 {token}, x86_fp80 {token}], ptr @scalar }}
@selected = global ptr getelementptr (%Envelope, ptr @record, i32 0, i32 1, i32 1)
define ptr @storage_pointer(%Envelope* %address) {{
entry:
  %element = getelementptr %Envelope, %Envelope* %address, i32 0, i32 1, i32 1
  ret ptr %element
}}
define i32 @probe() {{
entry:
  %storage = alloca %Envelope
  %address = getelementptr %Envelope, ptr %storage, i32 0, i32 1, i32 1
  %global_address = call ptr @storage_pointer(%Envelope* @record)
  %relative_address = getelementptr i8, ptr @record, i32 32
  %selected_address = load ptr, ptr @selected
  %relocation_ok = icmp eq ptr %selected_address, %global_address
  %offset_ok = icmp eq ptr %selected_address, %relative_address
  %scalar_low = load i64, ptr @scalar
  %scalar_high_ptr = getelementptr i8, ptr @scalar, i32 8
  %scalar_high = load i64, ptr %scalar_high_ptr
  %element_low = load i64, ptr %selected_address
  %element_high_ptr = getelementptr i8, ptr %selected_address, i32 8
  %element_high = load i64, ptr %element_high_ptr
  %tail_ptr = getelementptr %Envelope, ptr @record, i32 0, i32 2
  %tail = load ptr, ptr %tail_ptr
  %tail_ok = icmp eq ptr %tail, @scalar
  %scalar_low_ok = icmp eq i64 %scalar_low, {bits & ((1 << 64) - 1)}
  %scalar_high_ok = icmp eq i64 %scalar_high, {bits >> 64}
  %element_low_ok = icmp eq i64 %element_low, {bits & ((1 << 64) - 1)}
  %element_high_ok = icmp eq i64 %element_high, {bits >> 64}
  %a = and i1 %relocation_ok, %offset_ok
  %b = and i1 %scalar_low_ok, %scalar_high_ok
  %c = and i1 %element_low_ok, %element_high_ok
  %d = and i1 %a, %b
  %e = and i1 %c, %tail_ok
  %ok = and i1 %d, %e
  %bad = xor i1 %ok, true
  %result = zext i1 %bad to i32
  ret i32 %result
}}
'''
    startup = '''.intel_syntax noprefix
.text
.globl _start
_start:
  call probe
  mov rdi, rax
  mov rax, 60
  syscall
'''
    module = parse_self_backend_module(text)
    scalar = module.globals_[0].type
    record = module.globals_[1].type
    assert scalar.describe() == "x86_fp80"
    assert scalar.slot_size == scalar.align == 16
    assert record.slot_size == 64
    assert [record.field_offset(i) for i in range(3)] == [0, 16, 48]
    expected_scalar = bits.to_bytes(10, "little") + bytes(6)
    assert aggregate_literal_to_bytes(scalar, token) == expected_scalar
    assert aggregate_literal_to_bytes(
        record,
        f"{{ i8 7, [2 x x86_fp80] [x86_fp80 {token}, x86_fp80 {token}], ptr null }}",
        type_context=module.type_context,
    ) == bytes([7]) + bytes(15) + expected_scalar * 2 + bytes(16)
    sidecar = tmp_path / "globals.pidx"
    encode_indexed_module_file(str(sidecar), module)
    restored = decode_indexed_module_file(str(sidecar))
    assert restored.type_context.target_triple == target

    def forbid_compiler_process(*args, **kwargs):
        raise AssertionError("fp80 storage invoked an external compiler")

    for name, parsed in (("text", module), ("indexed", restored)):
        with monkeypatch.context() as guard:
            guard.setattr(subprocess, "Popen", forbid_compiler_process)
            assembly = _emit_x86_64_module("", module=parsed)
            image = link_static_executable(
                [assemble(assembly, target), assemble(startup, target)]
            )
        executable = tmp_path / name
        executable.write_bytes(image)
        executable.chmod(0o755)
        result = subprocess.run([str(executable)], capture_output=True, timeout=10)
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("target", [
    "aarch64-unknown-linux-gnu", "arm64-apple-darwin", "i386-unknown-linux-gnu",
    "x86_64-pc-windows-msvc", "x86_64-w64-windows-gnu",
])
def test_fp80_storage_rejects_unimplemented_target_layout(target):
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_parse import parse_self_backend_module

    with pytest.raises(BackendUnavailable) as error:
        parse_self_backend_module(
            f'target triple = "{target}"\n@value = global x86_fp80 zeroinitializer\n'
        )
    assert "x86_64 Linux target context" in str(error.value.__cause__)


def test_fp80_storage_without_target_context_is_rejected():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_parse import parse_ir_type

    with pytest.raises(BackendUnavailable, match="x86_64 Linux target context"):
        parse_ir_type("x86_fp80")


@pytest.mark.parametrize("body", [
    "define x86_fp80 @probe() {\nentry:\n  ret x86_fp80 zeroinitializer\n}\n",
    "define void @probe(x86_fp80 %value) {\nentry:\n  ret void\n}\n",
    "define void @probe(%Wide %value) {\nentry:\n  ret void\n}\n",
    "define void @probe(ptr %address) {\nentry:\n  %value = load x86_fp80, ptr %address\n  ret void\n}\n",
    "define void @probe(ptr %address) {\nentry:\n  store x86_fp80 zeroinitializer, ptr %address\n  ret void\n}\n",
    "define void @probe(ptr %ap) {\nentry:\n  %value = va_arg ptr %ap, x86_fp80\n  ret void\n}\n",
    "declare void @consume(i32, ...)\ndefine void @probe() {\nentry:\n  call void @consume(i32 0, x86_fp80 1.0)\n  ret void\n}\n",
    "declare x86_fp80 @produce()\ndefine void @probe() {\nentry:\n  %unused = call x86_fp80 @produce()\n  ret void\n}\n",
])
def test_fp80_executable_values_fail_before_scalar_or_aggregate_abi_lowering(body):
    from pcc.backend import BackendUnavailable

    text = 'target triple = "x86_64-unknown-linux-gnu"\n%Wide = type { x86_fp80 }\n' + body
    with pytest.raises(BackendUnavailable, match="x86_fp80 value/ABI lowering"):
        emit_x86_64_linux_asm(text)


def test_fp80_arithmetic_remains_an_explicit_unsupported_instruction():
    from pcc.backend import BackendUnavailable

    text = (
        'target triple = "x86_64-unknown-linux-gnu"\n'
        'define void @probe() {\nentry:\n'
        '  %value = fadd x86_fp80 1.0, 2.0\n  ret void\n}\n'
    )
    with pytest.raises(BackendUnavailable, match="does not support instruction.*fadd x86_fp80"):
        emit_x86_64_linux_asm(text)


def test_fp80_preparsed_module_cannot_bypass_target_layout_validation():
    from dataclasses import replace
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_verify import verify_parsed_module

    module = parse_self_backend_module(
        'target triple = "x86_64-unknown-linux-gnu"\n'
        '@value = global x86_fp80 zeroinitializer\n'
    )
    for target in ("", "i386-unknown-linux-gnu", "aarch64-unknown-linux-gnu"):
        with pytest.raises(BackendUnavailable, match="x86_fp80 storage requires an x86_64 Linux target"):
            verify_parsed_module(replace(module, triple=target))


def test_fp80_vector_does_not_inherit_array_storage_layout():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_parse import parse_self_backend_module

    with pytest.raises(BackendUnavailable) as error:
        parse_self_backend_module(
            'target triple = "x86_64-unknown-linux-gnu"\n'
            '@value = global <2 x x86_fp80> zeroinitializer\n'
        )
    assert "x86_fp80 vector storage layout is not implemented" in str(error.value.__cause__)
