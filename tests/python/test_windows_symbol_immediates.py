"""Owned symbol-immediate bytes and ELF-to-COFF relocations.

These are real encoder/object-writer tests, not Windows execution or PE
large-image qualification. No compiler subprocess or runtime archive is used.
"""

from __future__ import annotations

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend.elf_x86_64 import R_X86_64_64, R_X86_64_TPOFF32
from pcc.backend.x86_64_encode import X86EncodeError, encode_instruction


# Explicit ISA byte oracles; do not derive expected REX/opcodes from production.
ABSOLUTE_REGISTERS = (
    ("rax", "48b8"),
    ("r8", "49b8"),
    ("r9", "49b9"),
    ("r10", "49ba"),
    ("r11", "49bb"),
    ("r12", "49bc"),
    ("r13", "49bd"),
    ("r14", "49be"),
    ("r15", "49bf"),
)
TLS_REGISTERS = (
    ("eax", "b8"),
    ("r8d", "41b8"),
    ("r9d", "41b9"),
    ("r10d", "41ba"),
    ("r11d", "41bb"),
    ("r12d", "41bc"),
    ("r13d", "41bd"),
    ("r14d", "41be"),
    ("r15d", "41bf"),
)


def _encode(instruction, pc):
    return encode_instruction(
        instruction, pc=pc, labels={}, section_name=".text",
    )


def _assert_immediate(encoded, *, pc, head, width, kind, symbol):
    prefix = bytes.fromhex(head)
    assert encoded.code == prefix + b"\0" * width
    assert len(encoded.relocations) == 1
    relocation = encoded.relocations[0]
    assert (
        relocation.offset, relocation.type, relocation.addend, relocation.symbol,
    ) == (pc + len(prefix), kind, 0, symbol)
    # The whole relocation field must occupy exactly the immediate, not a
    # sign-extended imm32 or the following instruction.
    assert len(encoded.code) - (relocation.offset - pc) == width


@pytest.mark.parametrize("pc", (0, 0x1234))
@pytest.mark.parametrize("register, head", ABSOLUTE_REGISTERS)
def test_movabs_symbol_exact_bytes_and_relocation(register, head, pc):
    encoded = _encode(f"movabs {register}, OFFSET FLAT:data_symbol", pc)
    _assert_immediate(
        encoded, pc=pc, head=head, width=8, kind=R_X86_64_64,
        symbol="data_symbol",
    )


@pytest.mark.parametrize("pc", (0, 0x1234))
@pytest.mark.parametrize("register, head", TLS_REGISTERS)
def test_tls_symbol_immediate_exact_bytes_and_relocation(register, head, pc):
    encoded = _encode(f"mov {register}, OFFSET FLAT:tls_symbol@TPOFF", pc)
    _assert_immediate(
        encoded, pc=pc, head=head, width=4, kind=R_X86_64_TPOFF32,
        symbol="tls_symbol",
    )


@pytest.mark.parametrize("instruction", (
    "movabs eax, OFFSET FLAT:data_symbol",
    "movabs ax, OFFSET FLAT:data_symbol",
    "movabs al, OFFSET FLAT:data_symbol",
    "movabs r8d, OFFSET FLAT:data_symbol",
    "movabs r15w, OFFSET FLAT:data_symbol",
    "movabs xmm0, OFFSET FLAT:data_symbol",
    "movabs QWORD PTR [rsp], OFFSET FLAT:data_symbol",
    "mov rax, OFFSET FLAT:data_symbol",
    "mov r8d, OFFSET FLAT:data_symbol",
    "mov rax, OFFSET FLAT:tls_symbol@TPOFF",
    "mov ax, OFFSET FLAT:tls_symbol@TPOFF",
    "mov al, OFFSET FLAT:tls_symbol@TPOFF",
    "mov r8, OFFSET FLAT:tls_symbol@TPOFF",
    "mov r15w, OFFSET FLAT:tls_symbol@TPOFF",
    "mov r15b, OFFSET FLAT:tls_symbol@TPOFF",
    "movabs rax, OFFSET FLAT:tls_symbol@TPOFF",
    "movabs rax, data_symbol",
    "movabs rax, OFFSET FLAT:",
    "mov r8d, OFFSET FLAT:@TPOFF",
    "movabs rax, OFFSET FLAT:data_symbol, r8",
))
def test_symbol_immediate_rejects_wrong_width_or_shape(instruction):
    with pytest.raises(X86EncodeError):
        _encode(instruction, 0x1234)


def _coff_source(instruction, *, tls):
    target = (
        '.section .tdata,"awT",@progbits\n'
        ".p2align 3\n"
        ".zero 24\n"
        ".globl tls_symbol\n"
        ".type tls_symbol, @tls_object\n"
        "tls_symbol:\n"
        ".quad 7\n"
        if tls else
        ".section .rodata\n"
        ".p2align 3\n"
        ".zero 16\n"
        ".globl data_symbol\n"
        ".type data_symbol, @object\n"
        "data_symbol:\n"
        ".quad 7\n"
    )
    return (
        ".intel_syntax noprefix\n"
        ".text\n"
        ".globl probe\n"
        ".type probe, @function\n"
        "probe:\n"
        ".byte 144, 144, 144\n"
        f"  {instruction}\n"
        "  ret\n"
        ".size probe, .-probe\n"
        + target
        + '.section .note.GNU-stack,"",@progbits\n'
    )


def _assert_coff_conversion(instruction, *, head, width, kind, symbol, tls):
    # Assemble actual instructions, serialize COFF and read it back. The
    # conversion must not manufacture a GOT slot or a PC-relative relocation.
    blob = coff.assemble_object(_coff_source(instruction, tls=tls))
    obj = coff.parse_object(blob)
    text = next(section for section in obj.sections if section.name == ".text")
    prefix = bytes.fromhex(head)
    assert text.data == b"\x90" * 3 + prefix + b"\0" * width + b"\xc3"
    assert len(text.relocations) == 1
    relocation = text.relocations[0]
    assert (relocation.offset, relocation.kind) == (3 + len(prefix), kind)
    assert coff._width(relocation.kind) == width
    assert text.data[relocation.offset:relocation.offset + width] == b"\0" * width
    assert text.data[relocation.offset + width:] == b"\xc3"

    target = obj.symbols[relocation.symbol]
    assert target.name == symbol
    assert target.section > 0
    target_section = obj.sections[target.section - 1]
    assert target_section.name == (".tls$A" if tls else ".rodata")
    assert target.value == (24 if tls else 16)
    assert target_section.data[target.value:target.value + 8] == (7).to_bytes(8, "little")
    assert not any(section.name == ".rdata$got" for section in obj.sections)
    assert coff.emit_object(obj) == blob


@pytest.mark.parametrize("register, head", ABSOLUTE_REGISTERS)
def test_movabs_serializes_to_coff_addr64(register, head):
    _assert_coff_conversion(
        f"movabs {register}, OFFSET FLAT:data_symbol",
        head=head, width=8, kind=coff.ADDR64, symbol="data_symbol", tls=False,
    )


@pytest.mark.parametrize("register, head", TLS_REGISTERS)
def test_tls_immediate_serializes_to_coff_secrel(register, head):
    _assert_coff_conversion(
        f"mov {register}, OFFSET FLAT:tls_symbol@TPOFF",
        head=head, width=4, kind=coff.SECREL, symbol="tls_symbol", tls=True,
    )
