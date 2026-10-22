"""Exact machine-IR bitvectors through owned x86 emission and ELF linking.

These tests do not provision pcc1 or a runtime archive. The Python integers
below model explicit IR bit widths; ordinary Python integer semantics are not
changed by the backend casts.
"""

import platform
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend.elf_x86_64 import link_static_executable
from pcc.backend.owned_elf_link import assemble
from pcc.backend.self_backend_ir import (
    ParsedFunction,
    TypeDesc,
)
from pcc.backend.self_backend_parse import (
    _decode_parenthesized_constant_cast,
    decode_value_token,
)
from pcc.backend.self_backend_x86_64_linux import (
    _materialize_value,
    _sign_extend_reg_to_r10,
    _truncate_reg_to_r10,
    _zero_extend_reg_to_r10,
    emit_x86_64_linux_asm,
)


_TARGET = "x86_64-unknown-linux-gnu"
_U64_MASK = (1 << 64) - 1
_POINTER_CASES = (
    (1, 0, 0),
    (1, -1, 1),
    (3, -3, 5),
    (8, 127, 127),
    (8, -1, 255),
    (12, -1, 4095),
    (16, -1, 65535),
    (32, -1, 4294967295),
    (53, -1, (1 << 53) - 1),
    (64, 0, 0),
    (64, -1, -1),
    (64, 1 << 63, -(1 << 63)),
    (128, (1 << 100) + 37, 37),
    (128, -(1 << 80) + 19, 19),
    (128, -1, -1),
)


def _model_r10(lines, initial):
    """Independent x86 register semantics for the small cast-helper slice."""
    value = initial & _U64_MASK
    register_widths = {"r10b": 8, "r10w": 16, "r10d": 32, "r10": 64}
    for line in lines:
        operation, operands = line.strip().split(" ", 1)
        destination, source = operands.split(", ")
        width = register_widths[destination]
        mask = (1 << width) - 1
        old = value & mask
        if source in register_widths:
            source_width = register_widths[source]
            source_bits = value & ((1 << source_width) - 1)
        else:
            source_bits = int(source)
            source_width = width
        if operation == "and":
            result = old & source_bits
        elif operation == "shl":
            result = old << source_bits
        elif operation == "shr":
            result = old >> source_bits
        elif operation == "sar":
            signed = old - (1 << width) if old >> (width - 1) else old
            result = signed >> source_bits
        elif operation in ("movsx", "movsxd"):
            result = source_bits
            if source_bits >> (source_width - 1):
                result -= 1 << source_width
        elif operation in ("mov", "movzx"):
            result = source_bits
        else:
            raise AssertionError(line)
        if width >= 32:
            value = result & mask
        else:
            value = (value & ~mask) | (result & mask)
    return value


@pytest.mark.parametrize("width", range(1, 65))
def test_integer_cast_helpers_preserve_exact_declared_bits(width):
    integer_type = TypeDesc("int", width)
    mask = (1 << width) - 1
    sign = 1 << (width - 1)
    patterns = (0, 1, sign - 1, sign, mask, _U64_MASK, 0xA5D37BC19E2468F0)
    for pattern in patterns:
        expected_bits = pattern & mask
        signed = expected_bits - (1 << width) if expected_bits & sign else expected_bits
        assert _model_r10(_truncate_reg_to_r10(integer_type), pattern) & mask == expected_bits
        # Also check padding bits in an irregular storage lane, independently
        # of zext/sext, which could otherwise hide a broken truncation.
        lane_width = next(lane for lane in (8, 16, 32, 64) if width <= lane)
        truncated = _model_r10(_truncate_reg_to_r10(integer_type), pattern)
        assert truncated & ((1 << lane_width) - 1) == expected_bits
        assert _model_r10(_zero_extend_reg_to_r10(integer_type), pattern) == expected_bits
        assert _model_r10(_sign_extend_reg_to_r10(integer_type), pattern) == signed & _U64_MASK


@pytest.mark.parametrize("helper", (
    _truncate_reg_to_r10,
    _zero_extend_reg_to_r10,
    _sign_extend_reg_to_r10,
))
@pytest.mark.parametrize("invalid_type", (
    TypeDesc("int", 0),
    TypeDesc("int", 65),
    TypeDesc("int", 128),
    TypeDesc("fp", 32),
    TypeDesc("ptr", pointee=TypeDesc("void")),
))
def test_integer_cast_helpers_reject_non_machine_integer_types(helper, invalid_type):
    with pytest.raises(BackendUnavailable, match="requires i1..i64"):
        helper(invalid_type)


@pytest.mark.parametrize("width,value,expected", _POINTER_CASES)
def test_inttoptr_constant_decoder_keeps_source_bitvector_width(width, value, expected):
    expression = f"inttoptr (i{width} {value} to ptr)"
    assert decode_value_token(expression) == f"inttoptrconst:{expected}"


@pytest.mark.parametrize("expression", (
    "inttoptr (i32 1 to i64)",
    "inttoptr (double 1.0 to ptr)",
    "inttoptr (ptr null to ptr)",
    "inttoptr (i0 1 to ptr)",
    "inttoptr (i64 @symbol to ptr)",
))
def test_inttoptr_constant_decoder_rejects_invalid_types_and_values(expression):
    assert _decode_parenthesized_constant_cast(expression) is None


@pytest.mark.parametrize("expected_type,value,message", (
    (TypeDesc("int", 64), "inttoptrconst:1", "as non-pointer"),
    (TypeDesc("fp", 64), "inttoptrconst:1", "as non-pointer"),
    (TypeDesc("ptr", pointee=TypeDesc("void")), "inttoptrconst:@symbol", "expected integer constant"),
))
def test_x86_inttoptr_materializer_validates_target_and_payload(expected_type, value, message):
    function = ParsedFunction("probe", TypeDesc("void"), [], True, False, [])
    with pytest.raises(BackendUnavailable, match=message):
        _materialize_value(function, value, expected_type, "r10")


def _execute_owned_ir(ir_text):
    startup = """.intel_syntax noprefix
.text
.globl _start
_start:
  call probe
  mov rdi, rax
  mov rax, 60
  syscall
"""
    # Building both objects and linking may not invoke an external owner.
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external compiler invoked")):
        assembly = emit_x86_64_linux_asm(ir_text)
        image = link_static_executable(
            [assemble(assembly, _TARGET), assemble(startup, _TARGET)]
        )
    with tempfile.TemporaryDirectory(prefix="pcc-integer-casts-") as directory:
        executable = Path(directory) / "probe"
        executable.write_bytes(image)
        executable.chmod(0o755)
        result = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=10
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def _finish_checks(lines, names):
    previous = "true"
    for index, name in enumerate(names):
        current = f"combined_{index}"
        lines.append(f"  %{current} = and i1 {previous}, %{name}")
        previous = f"%{current}"
    lines.extend((
        f"  %failed = xor i1 {previous}, true",
        "  %result = zext i1 %failed to i32",
        "  ret i32 %result",
        "}",
    ))


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64")
)
@pytest.mark.parametrize("width", range(1, 65))
def test_owned_x86_integer_width_casts_and_downstream_operations_execute(width):
    mask = (1 << width) - 1
    sign = 1 << (width - 1)
    inputs = (0, sign - 1, sign, mask, -1, 0xA5D37BC19E2468F0)
    module = [f'target triple = "{_TARGET}"']
    calls = ["define i32 @probe() {", "entry:"]
    checks = []
    for case, value in enumerate(inputs):
        bits = value & mask
        signed = bits - (1 << width) if bits & sign else bits
        quotient = -(abs(signed) // 3) if signed < 0 else signed // 3
        name = f"check_{case}"
        lines = [f"define i32 @{name}(i64 %input) {{", "entry:"]
        if width < 64:
            lines.extend((
                f"  %narrow = trunc i64 %input to i{width}",
                f"  %signed = sext i{width} %narrow to i64",
                f"  %unsigned = zext i{width} %narrow to i64",
                f"  %constant_signed = sext i{width} -1 to i64",
                f"  %constant_unsigned = zext i{width} -1 to i64",
            ))
        else:
            lines.extend((
                "  %narrow = bitcast i64 %input to i64",
                "  %signed = bitcast i64 %input to i64",
                "  %unsigned = bitcast i64 %input to i64",
                "  %constant_signed = bitcast i64 -1 to i64",
                "  %constant_unsigned = bitcast i64 -1 to i64",
            ))
        lines.extend((
            f"  %pointer = inttoptr i{width} %narrow to ptr",
            "  %pointer_bits = ptrtoint ptr %pointer to i64",
            f"  %raw_ok = icmp eq i{width} %narrow, {bits}",
            f"  %signed_ok = icmp eq i64 %signed, {signed}",
            f"  %unsigned_ok = icmp eq i64 %unsigned, {bits}",
            f"  %pointer_ok = icmp eq i64 %pointer_bits, {bits}",
            "  %constant_signed_ok = icmp eq i64 %constant_signed, -1",
            f"  %constant_unsigned_ok = icmp eq i64 %constant_unsigned, {mask}",
            "  %signed_division = sdiv i64 %signed, 3",
            "  %unsigned_division = udiv i64 %unsigned, 3",
            "  %signed_shift = ashr i64 %signed, 1",
            "  %unsigned_shift = lshr i64 %unsigned, 1",
            "  %signed_comparison = icmp slt i64 %signed, 0",
            f"  %unsigned_comparison = icmp ugt i64 %unsigned, {(1 << 63) - 1}",
            f"  %signed_division_ok = icmp eq i64 %signed_division, {quotient}",
            f"  %unsigned_division_ok = icmp eq i64 %unsigned_division, {bits // 3}",
            f"  %signed_shift_ok = icmp eq i64 %signed_shift, {signed >> 1}",
            f"  %unsigned_shift_ok = icmp eq i64 %unsigned_shift, {bits >> 1}",
            f"  %signed_comparison_ok = icmp eq i1 %signed_comparison, {int(signed < 0)}",
            f"  %unsigned_comparison_ok = icmp eq i1 %unsigned_comparison, {int(bits > (1 << 63) - 1)}",
        ))
        _finish_checks(lines, (
            "raw_ok", "signed_ok", "unsigned_ok", "pointer_ok",
            "constant_signed_ok", "constant_unsigned_ok",
            "signed_division_ok", "unsigned_division_ok",
            "signed_shift_ok", "unsigned_shift_ok",
            "signed_comparison_ok", "unsigned_comparison_ok",
        ))
        module.extend(lines)
        calls.append(f"  %call_{case} = call i32 @{name}(i64 {value})")
        calls.append(f"  %ok_{case} = icmp eq i32 %call_{case}, 0")
        checks.append(f"ok_{case}")
    _finish_checks(calls, checks)
    module.extend(calls)
    _execute_owned_ir("\n".join(module))


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64")
)
@pytest.mark.parametrize("width,value,expected", _POINTER_CASES)
def test_owned_x86_inttoptr_constants_execute(width, value, expected):
    expression = f"inttoptr (i{width} {value} to ptr)"
    ir_text = f'''
target triple = "{_TARGET}"
@pointer_constant = global ptr {expression}

define ptr @identity(ptr %value) {{
entry:
  ret ptr %value
}}

define i32 @probe() {{
entry:
  %constant = select i1 true, ptr {expression}, ptr null
  %called = call ptr @identity(ptr {expression})
  %global = load ptr, ptr @pointer_constant
  %constant_bits = ptrtoint ptr %constant to i64
  %called_bits = ptrtoint ptr %called to i64
  %global_bits = ptrtoint ptr %global to i64
  %constant_ok = icmp eq i64 %constant_bits, {expected}
  %called_ok = icmp eq i64 %called_bits, {expected}
  %global_ok = icmp eq i64 %global_bits, {expected}
  %first_pair = and i1 %constant_ok, %called_ok
  %all_ok = and i1 %first_pair, %global_ok
  %failed = xor i1 %all_ok, true
  %result = zext i1 %failed to i32
  ret i32 %result
}}
'''
    _execute_owned_ir(ir_text)


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64")
)
def test_owned_x86_chained_irregular_width_sign_extension_executes():
    ir_text = f'''
target triple = "{_TARGET}"
define i32 @probe() {{
entry:
  %first = trunc i64 -3 to i3
  %second = sext i3 %first to i12
  %third = sext i12 %second to i20
  %fourth = sext i20 %third to i29
  %fifth = sext i29 %fourth to i53
  %sixth = sext i53 %fifth to i64
  %second_bits = zext i12 %second to i64
  %fifth_bits = zext i53 %fifth to i64
  %second_ok = icmp eq i64 %second_bits, 4093
  %fifth_ok = icmp eq i64 %fifth_bits, {(1 << 53) - 3}
  %sixth_ok = icmp eq i64 %sixth, -3
  %first_pair = and i1 %second_ok, %fifth_ok
  %all_ok = and i1 %first_pair, %sixth_ok
  %failed = xor i1 %all_ok, true
  %result = zext i1 %failed to i32
  ret i32 %result
}}
'''
    _execute_owned_ir(ir_text)
