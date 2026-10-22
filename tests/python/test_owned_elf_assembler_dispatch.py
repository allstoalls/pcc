"""Host contracts for the real ELF assembler dispatch.

The normal pcc1 canary remains the native import/binding acceptance gate;
these checks run the production assemblers in the host interpreter.
"""

import struct

import pytest

from pcc.backend import arm64_elf_driver, owned_elf_link, x86_64_asm_driver
from pcc.backend.arm64_encode import EncodeError
from pcc.backend.elf_x86_64 import (
    EM_AARCH64,
    EM_X86_64,
    ET_REL,
    ElfError,
    emit_relocatable,
    parse_relocatable,
)
from pcc.backend.indexed_phase_timing import ModulePhaseTiming
from pcc.backend.x86_64_encode import X86EncodeError


_ARM_TARGET = "aarch64-unknown-linux-gnu"
_X86_TARGET = "x86_64-unknown-linux-gnu"
_ARM_ASM = """.section __TEXT,__text,regular,pure_instructions
.globl dispatch_probe
dispatch_probe:
  movz x0, #42
  ret
"""
_X86_ASM = """.intel_syntax noprefix
.text
.globl dispatch_probe
.type dispatch_probe, @function
dispatch_probe:
  mov eax, 42
  ret
.size dispatch_probe, .-dispatch_probe
"""
_ARM_CODE = bytes.fromhex("40 05 80 d2 c0 03 5f d6")
_X86_CODE = bytes.fromhex("b8 2a 00 00 00 c3")


def _assert_object(obj, machine, code):
    assert obj.machine == machine
    text_sections = [section for section in obj.sections if section.name == ".text"]
    assert len(text_sections) == 1
    assert text_sections[0].data == code
    assert not text_sections[0].relocations
    symbols = [symbol for symbol in obj.symbols if symbol.name == "dispatch_probe"]
    assert len(symbols) == 1
    assert symbols[0].value == 0 and symbols[0].section_index > 0
    encoded = emit_relocatable(obj)
    assert encoded[:6] == b"\x7fELF\x02\x01"
    assert struct.unpack_from("<HH", encoded, 16) == (ET_REL, machine)
    restored = parse_relocatable(encoded)
    assert restored.machine == machine
    assert emit_relocatable(restored) == encoded
    return encoded


@pytest.mark.parametrize("arm_target", (
    _ARM_TARGET, "arm64-unknown-linux-musl",
))
def test_real_assembler_dispatch_arm_x86_arm(arm_target):
    # Do not replace either provider: check their actual instruction words and
    # ELF transport, including a return to ARM after selecting the x86 path.
    first = owned_elf_link.assemble(_ARM_ASM, arm_target)
    middle = owned_elf_link.assemble(_X86_ASM, _X86_TARGET)
    last = owned_elf_link.assemble(_ARM_ASM, arm_target)
    arm_bytes = _assert_object(first, EM_AARCH64, _ARM_CODE)
    x86_bytes = _assert_object(middle, EM_X86_64, _X86_CODE)
    assert _assert_object(last, EM_AARCH64, _ARM_CODE) == arm_bytes
    assert arm_bytes == emit_relocatable(arm64_elf_driver.assemble_file(_ARM_ASM))
    assert x86_bytes == emit_relocatable(x86_64_asm_driver.assemble_file(_X86_ASM))


def test_x86_timing_is_forwarded_without_changing_object(capsys):
    timing = ModulePhaseTiming("owned_elf_dispatch")
    timed = owned_elf_link.assemble(_X86_ASM, _X86_TARGET, phase_timing=timing)
    plain = owned_elf_link.assemble(_X86_ASM, _X86_TARGET)
    assert _assert_object(timed, EM_X86_64, _X86_CODE) == emit_relocatable(plain)
    events = [
        dict(token.split("=", 1) for token in line.split() if "=" in token)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("pcc coarse phase scope=assembly ")
    ]
    assert [(event["phase"], event["event"]) for event in events] == [
        (phase, event)
        for phase in ("assembly_parse", "assembly_measure", "assembly_emit_finalize")
        for event in ("begin", "complete")
    ]
    assert not timing.failed
    assert all(event["clock_ok"] == "1" for event in events)


def test_arm_preserves_existing_no_timing_forwarding(capsys):
    timing = ModulePhaseTiming("owned_elf_dispatch")
    timed = owned_elf_link.assemble(_ARM_ASM, _ARM_TARGET, phase_timing=timing)
    assert _assert_object(timed, EM_AARCH64, _ARM_CODE) == emit_relocatable(
        arm64_elf_driver.assemble_file(_ARM_ASM),
    )
    assert timing.totals_ns == [0] * 16
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("target,source,provider,error_type", (
    (_ARM_TARGET, _ARM_ASM, arm64_elf_driver.assemble_file, EncodeError),
    (_X86_TARGET, _X86_ASM, x86_64_asm_driver.assemble_file, X86EncodeError),
))
def test_real_assembler_errors_are_not_replaced(target, source, provider, error_type):
    invalid = source + ".pcc_invalid_directive\n"
    with pytest.raises(error_type) as direct:
        provider(invalid)
    with pytest.raises(error_type) as routed:
        owned_elf_link.assemble(invalid, target)
    assert str(routed.value) == str(direct.value)


@pytest.mark.parametrize("target", (
    "aarch64-apple-darwin", "x86_64-pc-windows-msvc", "not-a-target",
))
def test_unsupported_target_keeps_exact_error(target):
    with pytest.raises(ElfError) as error:
        owned_elf_link.assemble(_ARM_ASM, target)
    assert str(error.value) == "unsupported owned ELF target: " + target
