"""Execute SysV variadic GP-register and overflow arguments with owned ELF."""

import platform
import subprocess
import sys
from unittest.mock import patch

import pytest

from pcc.backend.elf_x86_64 import link_static_executable
from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm
from pcc.backend.x86_64_asm_driver import assemble_file


_HOST = sys.platform == "linux" and platform.machine().lower() in ("x86_64", "amd64")
_native = pytest.mark.pcc_gate(unavailable=None if _HOST else "needs Linux x86_64 execution")
_START = """.intel_syntax noprefix
.text
.globl _start
_start:
  call probe
  mov edi, eax
  mov eax, 60
  syscall
"""


@_native
@pytest.mark.parametrize("count", [1, 5, 6, 12])
def test_owned_sysv_varargs_crosses_register_save_area_into_stack(tmp_path, count):
    lines = [
        'target triple = "x86_64-unknown-linux-gnu"',
        'declare void @llvm.va_start.p0(ptr)',
        'declare void @llvm.va_end.p0(ptr)',
        'define i32 @collect(i32 %tag, ...) {',
        'entry:',
        '  %ap = alloca { i32, i32, ptr, ptr }, align 8',
        '  call void @llvm.va_start.p0(ptr %ap)',
    ]
    previous = "0"
    for index in range(count):
        lines.append(f"  %value{index} = va_arg ptr %ap, i32")
        lines.append(f"  %sum{index} = add i32 {previous}, %value{index}")
        previous = f"%sum{index}"
    lines.extend(['  call void @llvm.va_end.p0(ptr %ap)',
                  f'  ret i32 {previous}', '}', 'define i32 @probe() {', 'entry:'])
    arguments = ", ".join("i32 " + str(value) for value in range(1, count + 1))
    expected = count * (count + 1) // 2
    lines.extend([
        f"  %actual = call i32 (i32, ...) @collect(i32 0, {arguments})",
        f"  %difference = sub i32 %actual, {expected}",
        "  ret i32 %difference", "}",
    ])
    ir = "\n".join(lines) + "\n"
    (tmp_path / "program.ll").write_text(ir, encoding="utf-8")
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external build process")):
        assembly = emit_x86_64_linux_asm(ir)
        (tmp_path / "program.s").write_text(assembly, encoding="utf-8")
        image = link_static_executable([assemble_file(assembly), assemble_file(_START)])
    executable = tmp_path / "program"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@_native
@pytest.mark.parametrize("fp_type, fixed_fp", [
    pytest.param("float", 10, id="fixed-float"),
    pytest.param("double", 10, id="fixed-double"),
    pytest.param("double", 0, id="variadic-double"),
    pytest.param("double", 9, id="fixed-and-variadic-stack-double"),
])
def test_owned_sysv_stack_float_arguments_preserve_bits(tmp_path, fp_type, fixed_fp):
    # Exhaust the six GP and eight SSE argument registers independently.
    # Four integer overflow slots precede FP stack arguments at rsp+32/+40.
    # Verify every register and stack argument, including signed zero, a NaN
    # payload and a subnormal, without floating arithmetic canonicalizing bits.
    width = 32 if fp_type == "float" else 64
    bits = ([0, 0x3F800000, 0xBFA00000, 0x7F800000, 0xFF800000,
             0x7FA12345, 0x00800000, 0x3F000000, 0x80000000, 0x7FC12345]
            if width == 32 else
            [0, 0x3FF0000000000000, 0xBFF4000000000000, 0x7FF0000000000000,
             0xFFF0000000000000, 0x7FF42468A0000000, 0x0010000000000000,
             0x3FE0000000000000, 0x8000000000000000, 0x0000000000000001])
    gp_values = [-(index + 1) * 17 for index in range(10)]
    parameters = [f"i32 %gp{index}" for index in range(10)]
    parameters.extend(f"{fp_type} %fp{index}" for index in range(fixed_fp))
    variadic = fixed_fp < len(bits)
    if variadic:
        parameters.append("...")
    lines = [
        'target triple = "x86_64-unknown-linux-gnu"',
        'declare void @llvm.va_start.p0(ptr)',
        'declare void @llvm.va_end.p0(ptr)',
        f'define i32 @collect_fp({", ".join(parameters)}) {{', 'entry:',
    ]
    if variadic:
        lines.extend([
            '  %ap = alloca { i32, i32, ptr, ptr }, align 8',
            '  call void @llvm.va_start.p0(ptr %ap)',
        ])
        for index in range(fixed_fp, len(bits)):
            lines.append(f'  %fp{index} = va_arg ptr %ap, {fp_type}')
        lines.append('  call void @llvm.va_end.p0(ptr %ap)')
    checks = []
    for index, expected in enumerate(gp_values):
        name = f'gp_bad{index}'
        lines.append(f'  %{name} = icmp ne i32 %gp{index}, {expected}')
        checks.append(name)
    for index, expected in enumerate(bits):
        lines.extend([
            f'  %bits{index} = bitcast {fp_type} %fp{index} to i{width}',
            f'  %fp_bad{index} = icmp ne i{width} %bits{index}, {expected}',
        ])
        checks.append(f'fp_bad{index}')
    previous = 'false'
    for index, name in enumerate(checks):
        lines.append(f'  %bad{index} = or i1 {previous}, %{name}')
        previous = f'%bad{index}'
    lines.extend([f'  %result = zext i1 {previous} to i32',
                  '  ret i32 %result', '}', 'define i32 @probe() {', 'entry:'])
    for index, value in enumerate(bits):
        lines.append(f'  %value{index} = bitcast i{width} {value} to {fp_type}')
    arguments = [f'i32 {value}' for value in gp_values]
    arguments.extend(f'{fp_type} %value{index}' for index in range(len(bits)))
    signature = ["i32"] * len(gp_values) + [fp_type] * fixed_fp
    if variadic:
        signature.append('...')
    lines.extend([
        f'  %result = call i32 ({", ".join(signature)}) @collect_fp({", ".join(arguments)})',
        '  ret i32 %result', '}',
    ])
    ir = '\n'.join(lines) + '\n'
    (tmp_path / 'program.ll').write_text(ir, encoding='utf-8')
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external build process")):
        assembly = emit_x86_64_linux_asm(ir)
        (tmp_path / 'program.s').write_text(assembly, encoding='utf-8')
        image = link_static_executable([assemble_file(assembly), assemble_file(_START)])
    executable = tmp_path / 'program'
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, b'', b'')
