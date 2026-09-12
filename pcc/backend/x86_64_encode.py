"""Finite x86_64 encoder for the Linux self backend's emitted dialect.

This is deliberately an emitter encoder, not a general GNU assembler.  Every
accepted instruction shape is produced by ``self_backend_x86_64_linux``; an
unknown mnemonic, register, addressing form, or immediate fails closed before
an ELF object can be published.

The encoder uses fixed-width near branches.  That makes the two-pass file
driver deterministic and avoids relaxation changing label offsets.  External
PC-relative references are returned as ELF RELA records for the owned ELF
writer/linker.
"""

from __future__ import annotations

from dataclasses import dataclass

from .elf_x86_64 import (
    R_X86_64_GOTTPOFF,
    R_X86_64_PC32,
    R_X86_64_PLT32,
)


class X86EncodeError(Exception):
    """Instruction text is outside the self emitter's proven vocabulary."""


@dataclass(frozen=True)
class EncodedRelocation:
    offset: int
    symbol: str
    type: int
    addend: int


@dataclass(frozen=True)
class EncodedInstruction:
    code: bytes
    relocations: tuple[EncodedRelocation, ...] = ()


@dataclass(frozen=True)
class _Reg:
    name: str
    code: int
    width: int
    kind: str = "gp"

    @property
    def low(self) -> int:
        return self.code & 7

    @property
    def high(self) -> int:
        return (self.code >> 3) & 1

    @property
    def needs_byte_rex(self) -> bool:
        return self.kind == "gp" and self.width == 8 and self.name in {
            "spl", "bpl", "sil", "dil",
        }


@dataclass(frozen=True)
class _Imm:
    value: int


@dataclass(frozen=True)
class _Target:
    name: str


@dataclass(frozen=True)
class _Mem:
    width: int | None
    base: _Reg | None = None
    index: _Reg | None = None
    scale: int = 1
    disp: int = 0
    symbol: str | None = None
    relocation: int = R_X86_64_PC32
    segment: str | None = None


_GP_BASES = (
    "rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
)
_GP_NAMES: dict[str, _Reg] = {}
for _code, _base in enumerate(_GP_BASES):
    if _code < 8:
        _names = {
            64: _base,
            32: ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")[_code],
            16: ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")[_code],
            8: ("al", "cl", "dl", "bl", "spl", "bpl", "sil", "dil")[_code],
        }
    else:
        _names = {
            64: _base,
            32: _base + "d",
            16: _base + "w",
            8: _base + "b",
        }
    for _width, _name in _names.items():
        _GP_NAMES[_name] = _Reg(_name, _code, _width)

_XMM_NAMES = {
    "xmm" + str(index): _Reg("xmm" + str(index), index, 128, "xmm")
    for index in range(16)
}

_SIZE_PREFIXES = {
    "BYTE PTR ": 8,
    "WORD PTR ": 16,
    "DWORD PTR ": 32,
    "QWORD PTR ": 64,
}

_SET_CONDITIONS = {
    "o": 0x0,
    "no": 0x1,
    "b": 0x2,
    "ae": 0x3,
    "e": 0x4,
    "ne": 0x5,
    "be": 0x6,
    "a": 0x7,
    "s": 0x8,
    "ns": 0x9,
    "p": 0xA,
    "np": 0xB,
    "l": 0xC,
    "ge": 0xD,
    "le": 0xE,
    "g": 0xF,
}


def _parse_int(text: str) -> int:
    try:
        return int(text.strip(), 0)
    except ValueError as exc:
        raise X86EncodeError(f"expected integer, got {text!r}") from exc


def _split_operands(text: str) -> list[str]:
    result: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise X86EncodeError(f"unbalanced memory operand {text!r}")
        elif char == "," and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    if depth:
        raise X86EncodeError(f"unbalanced memory operand {text!r}")
    tail = text[start:].strip()
    if tail:
        result.append(tail)
    return result


def _parse_reg(text: str) -> _Reg | None:
    name = text.strip().lower()
    return _GP_NAMES.get(name) or _XMM_NAMES.get(name)


def _parse_mem(text: str) -> _Mem | None:
    raw = text.strip()
    width: int | None = None
    upper = raw.upper()
    for prefix, candidate in _SIZE_PREFIXES.items():
        if upper.startswith(prefix):
            width = candidate
            raw = raw[len(prefix):].strip()
            break
    if raw.lower() == "fs:0":
        return _Mem(width, disp=0, segment="fs")
    if raw.endswith("[rip]"):
        symbol = raw[:-5].strip()
        relocation = R_X86_64_PC32
        suffix = "@gottpoff"
        if symbol.lower().endswith(suffix):
            symbol = symbol[:-len(suffix)]
            relocation = R_X86_64_GOTTPOFF
        if not symbol:
            raise X86EncodeError(f"missing RIP-relative symbol in {text!r}")
        return _Mem(width, symbol=symbol, relocation=relocation)
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    expression = raw[1:-1].strip()
    if not expression:
        raise X86EncodeError("empty memory operand")
    # Intel syntax permits arbitrary whitespace around address-expression
    # operators.  Compact it before splitting so ``[rbp - 8]`` produces the
    # integer token ``-8`` rather than the invalid ``- 8``.
    normalized = "".join(expression.split()).replace("-", "+-")
    base: _Reg | None = None
    index: _Reg | None = None
    scale = 1
    disp = 0
    for term in normalized.split("+"):
        term = term.strip()
        if not term:
            continue
        if "*" in term:
            reg_text, scale_text = [part.strip() for part in term.split("*", 1)]
            reg = _parse_reg(reg_text)
            if reg is None or reg.kind != "gp" or reg.width != 64:
                raise X86EncodeError(f"bad memory index {term!r}")
            if index is not None:
                raise X86EncodeError(f"multiple memory indices in {text!r}")
            index = reg
            scale = _parse_int(scale_text)
            if scale not in (1, 2, 4, 8):
                raise X86EncodeError(f"bad memory scale {scale}")
            continue
        reg = _parse_reg(term)
        if reg is not None:
            if reg.kind != "gp" or reg.width != 64:
                raise X86EncodeError(f"bad memory base {term!r}")
            if base is None:
                base = reg
            elif index is None:
                index = reg
            else:
                raise X86EncodeError(f"too many memory registers in {text!r}")
            continue
        disp += _parse_int(term)
    if base is None:
        raise X86EncodeError(f"memory operand lacks a base register: {text!r}")
    if index is not None and index.low == 4:
        raise X86EncodeError("rsp/r12 cannot be an x86 SIB index")
    return _Mem(width, base=base, index=index, scale=scale, disp=disp)


def _parse_operand(text: str):
    reg = _parse_reg(text)
    if reg is not None:
        return reg
    mem = _parse_mem(text)
    if mem is not None:
        return mem
    try:
        return _Imm(int(text.strip(), 0))
    except ValueError:
        name = text.strip()
        if not name or any(char.isspace() for char in name):
            raise X86EncodeError(f"bad operand {text!r}")
        return _Target(name)


def _int_bytes(value: int, width: int) -> bytes:
    mask = (1 << (width * 8)) - 1
    return (value & mask).to_bytes(width, "little")


def _require_immediate_bits(
    value: int,
    width: int,
    *,
    owner: str,
    signed_only: bool = False,
) -> None:
    bits = width * 8
    lower = -(1 << (bits - 1))
    upper = (1 << (bits - 1)) - 1 if signed_only else (1 << bits) - 1
    if value < lower or value > upper:
        qualifier = "signed " if signed_only else ""
        raise X86EncodeError(
            f"{owner} immediate {value} does not fit {qualifier}{bits} bits"
        )


def _rex(*, w: bool, r: int, x: int, b: int, force: bool = False) -> bytes:
    value = 0x40 | (0x08 if w else 0) | (0x04 if r else 0) | (0x02 if x else 0) | (0x01 if b else 0)
    return bytes((value,)) if force or value != 0x40 else b""


def _legacy_width(width: int) -> bytes:
    return b"\x66" if width == 16 else b""


@dataclass(frozen=True)
class _RMEncoding:
    payload: bytes
    rex_x: int
    rex_b: int
    relocation_offset: int | None = None
    relocation_symbol: str | None = None
    relocation_type: int = R_X86_64_PC32


def _encode_rm(reg_field: int, operand) -> _RMEncoding:
    if isinstance(operand, _Reg):
        return _RMEncoding(
            bytes((0xC0 | ((reg_field & 7) << 3) | operand.low,)),
            0,
            operand.high,
        )
    if not isinstance(operand, _Mem):
        raise X86EncodeError("ModRM operand must be a register or memory")
    if operand.symbol is not None:
        return _RMEncoding(
            bytes((((reg_field & 7) << 3) | 5,)) + b"\0\0\0\0",
            0,
            0,
            relocation_offset=1,
            relocation_symbol=operand.symbol,
            relocation_type=operand.relocation,
        )
    if operand.segment is not None:
        if operand.segment != "fs" or operand.base is not None or operand.index is not None:
            raise X86EncodeError("only absolute fs:0 memory is proven")
        return _RMEncoding(
            bytes((((reg_field & 7) << 3) | 4, 0x25)) + _int_bytes(operand.disp, 4),
            0,
            0,
        )
    assert operand.base is not None
    base = operand.base
    disp = operand.disp
    if disp < -(1 << 31) or disp >= (1 << 31):
        raise X86EncodeError(
            f"memory displacement {disp} does not fit signed 32 bits"
        )
    if disp == 0 and base.low != 5:
        mod = 0
        disp_bytes = b""
    elif -128 <= disp <= 127:
        mod = 1
        disp_bytes = _int_bytes(disp, 1)
    else:
        mod = 2
        disp_bytes = _int_bytes(disp, 4)
    needs_sib = operand.index is not None or base.low == 4
    rm = 4 if needs_sib else base.low
    payload = bytearray((mod << 6 | ((reg_field & 7) << 3) | rm,))
    rex_x = 0
    if needs_sib:
        if operand.index is None:
            index_low = 4
        else:
            index_low = operand.index.low
            rex_x = operand.index.high
        scale_bits = {1: 0, 2: 1, 4: 2, 8: 3}[operand.scale]
        payload.append((scale_bits << 6) | (index_low << 3) | base.low)
    payload.extend(disp_bytes)
    return _RMEncoding(bytes(payload), rex_x, base.high)


def _modrm_instruction(
    *,
    pc: int,
    legacy: bytes,
    opcode: bytes,
    width: int,
    reg: _Reg,
    rm,
    force_rex: bool = False,
) -> EncodedInstruction:
    encoded_rm = _encode_rm(reg.code, rm)
    segment_prefix = (
        b"\x64"
        if isinstance(rm, _Mem) and rm.segment == "fs"
        else b""
    )
    rex = _rex(
        w=width == 64,
        r=reg.high,
        x=encoded_rm.rex_x,
        b=encoded_rm.rex_b,
        force=(
            force_rex
            or reg.needs_byte_rex
            or (isinstance(rm, _Reg) and rm.needs_byte_rex)
        ),
    )
    head = segment_prefix + legacy + rex + opcode
    code = head + encoded_rm.payload
    relocations: tuple[EncodedRelocation, ...] = ()
    if encoded_rm.relocation_symbol is not None:
        assert encoded_rm.relocation_offset is not None
        relocations = (EncodedRelocation(
            pc + len(head) + encoded_rm.relocation_offset,
            encoded_rm.relocation_symbol,
            encoded_rm.relocation_type,
            -4,
        ),)
    return EncodedInstruction(code, relocations)


def _require_gp(reg, *, owner: str) -> _Reg:
    if not isinstance(reg, _Reg) or reg.kind != "gp":
        raise X86EncodeError(f"{owner} requires a general-purpose register")
    return reg


def _require_xmm(reg, *, owner: str) -> _Reg:
    if not isinstance(reg, _Reg) or reg.kind != "xmm":
        raise X86EncodeError(f"{owner} requires an XMM register")
    return reg


def _operand_width(operand) -> int | None:
    if isinstance(operand, _Reg):
        return operand.width
    if isinstance(operand, _Mem):
        return operand.width
    return None


def _same_width(first, second, *, owner: str) -> int:
    first_width = _operand_width(first)
    second_width = _operand_width(second)
    width = first_width or second_width
    if width not in (8, 16, 32, 64):
        raise X86EncodeError(f"{owner} has no proven scalar width")
    if first_width not in (None, width) or second_width not in (None, width):
        raise X86EncodeError(f"{owner} operand widths disagree")
    return width


def _encode_mov(operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError("mov expects two operands")
    dst, src = operands
    if isinstance(dst, _Reg) and dst.kind == "gp" and isinstance(src, _Imm):
        width = dst.width
        if width == 8:
            _require_immediate_bits(src.value, 1, owner="mov")
            rex = _rex(w=False, r=0, x=0, b=dst.high, force=dst.needs_byte_rex)
            return EncodedInstruction(rex + bytes((0xB0 + dst.low,)) + _int_bytes(src.value, 1))
        if width == 64 and -(1 << 31) <= src.value < (1 << 31):
            # GNU as selects C7 /0 for a sign-extendable imm32 and B8+rd only
            # for a true 64-bit immediate.  Mirror that canonical choice so
            # the owned/system-object differential is byte exact.
            pseudo = _Reg("group", 0, 64)
            base = _modrm_instruction(
                pc=pc,
                legacy=b"",
                opcode=b"\xc7",
                width=64,
                reg=pseudo,
                rm=dst,
            )
            return EncodedInstruction(
                base.code + _int_bytes(src.value, 4),
                base.relocations,
            )
        immediate_width = {16: 2, 32: 4, 64: 8}[width]
        _require_immediate_bits(src.value, immediate_width, owner="mov")
        rex = _rex(w=width == 64, r=0, x=0, b=dst.high)
        return EncodedInstruction(
            _legacy_width(width) + rex + bytes((0xB8 + dst.low,))
            + _int_bytes(src.value, immediate_width)
        )
    if (
        isinstance(dst, _Reg)
        and dst.kind == "gp"
        and isinstance(src, _Reg)
        and src.kind == "gp"
    ):
        width = _same_width(dst, src, owner="mov")
        # GNU as canonicalizes register-to-register moves through the
        # r/m-destination opcode (89/88), even though 8b/8a would be
        # semantically equivalent.  Matching it keeps owned/system object
        # bytes directly comparable and the pcc2/pcc3 object stream stable.
        opcode = b"\x88" if width == 8 else b"\x89"
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=opcode,
            width=width, reg=src, rm=dst,
        )
    if isinstance(dst, _Reg) and dst.kind == "gp" and isinstance(src, _Mem):
        width = _same_width(dst, src, owner="mov")
        opcode = b"\x8a" if width == 8 else b"\x8b"
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=opcode,
            width=width, reg=dst, rm=src,
        )
    if isinstance(dst, _Mem) and isinstance(src, _Reg) and src.kind == "gp":
        width = _same_width(dst, src, owner="mov")
        opcode = b"\x88" if width == 8 else b"\x89"
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=opcode,
            width=width, reg=src, rm=dst,
        )
    raise X86EncodeError("mov operand shape not produced by the self emitter")


_BINARY_REG_RM = {
    "add": 0x03,
    "or": 0x0B,
    "and": 0x23,
    "sub": 0x2B,
    "xor": 0x33,
    "cmp": 0x3B,
}
_BINARY_RM_REG = {
    "add": 0x01,
    "or": 0x09,
    "and": 0x21,
    "sub": 0x29,
    "xor": 0x31,
    "cmp": 0x39,
}
_BINARY_IMM_GROUP = {
    "add": 0,
    "or": 1,
    "and": 4,
    "sub": 5,
    "xor": 6,
    "cmp": 7,
}


def _encode_binary(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError(f"{mnemonic} expects two operands")
    dst, src = operands
    if (
        isinstance(dst, _Reg)
        and dst.kind == "gp"
        and isinstance(src, _Reg)
        and src.kind == "gp"
    ):
        width = _same_width(dst, src, owner=mnemonic)
        # Like mov, GNU as selects the r/m-destination encoding for a pair of
        # registers.  Both encodings execute identically, but only this one is
        # byte-identical to the differential oracle.
        opcode = _BINARY_RM_REG[mnemonic]
        if width == 8:
            opcode -= 1
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=bytes((opcode,)),
            width=width, reg=src, rm=dst,
        )
    if isinstance(dst, _Reg) and dst.kind == "gp" and isinstance(src, _Mem):
        width = _same_width(dst, src, owner=mnemonic)
        opcode = _BINARY_REG_RM[mnemonic]
        if width == 8:
            opcode -= 1
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=bytes((opcode,)),
            width=width, reg=dst, rm=src,
        )
    if isinstance(dst, _Mem) and isinstance(src, _Reg) and src.kind == "gp":
        width = _same_width(dst, src, owner=mnemonic)
        opcode = _BINARY_RM_REG[mnemonic]
        if width == 8:
            opcode -= 1
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=bytes((opcode,)),
            width=width, reg=src, rm=dst,
        )
    if isinstance(dst, _Reg) and dst.kind == "gp" and isinstance(src, _Imm):
        width = dst.width
        group_reg = _Reg("group", _BINARY_IMM_GROUP[mnemonic], width)
        if width == 8:
            _require_immediate_bits(src.value, 1, owner=mnemonic)
            opcode = b"\x80"
            immediate = _int_bytes(src.value, 1)
        elif -128 <= src.value <= 127:
            opcode = b"\x83"
            immediate = _int_bytes(src.value, 1)
        else:
            opcode = b"\x81"
            immediate_width = 2 if width == 16 else 4
            _require_immediate_bits(
                src.value,
                immediate_width,
                owner=mnemonic,
                signed_only=width == 64,
            )
            immediate = _int_bytes(src.value, immediate_width)
        base = _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=opcode,
            width=width, reg=group_reg, rm=dst,
            force_rex=dst.needs_byte_rex,
        )
        return EncodedInstruction(base.code + immediate, base.relocations)
    raise X86EncodeError(f"{mnemonic} operand shape not proven")


def _encode_test(operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError("test expects two operands")
    left, right = operands
    if not isinstance(right, _Reg) or right.kind != "gp" or not isinstance(left, (_Reg, _Mem)):
        raise X86EncodeError("test expects r/m, register")
    width = _same_width(left, right, owner="test")
    return _modrm_instruction(
        pc=pc, legacy=_legacy_width(width),
        opcode=b"\x84" if width == 8 else b"\x85",
        width=width, reg=right, rm=left,
    )


def _encode_lea(operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError("lea expects two operands")
    dst = _require_gp(operands[0], owner="lea")
    src = operands[1]
    if dst.width != 64 or not isinstance(src, _Mem):
        raise X86EncodeError("self emitter lea requires r64, memory")
    return _modrm_instruction(
        pc=pc, legacy=b"", opcode=b"\x8d", width=64, reg=dst, rm=src,
    )


def _encode_imul(operands: list, pc: int) -> EncodedInstruction:
    if len(operands) not in (2, 3):
        raise X86EncodeError("imul expects two or three operands")
    dst = _require_gp(operands[0], owner="imul")
    src = operands[1]
    if not isinstance(src, (_Reg, _Mem)):
        raise X86EncodeError("imul source must be register/memory")
    width = _same_width(dst, src, owner="imul")
    if width == 8:
        raise X86EncodeError("8-bit imul is outside the emitter dialect")
    if len(operands) == 2:
        return _modrm_instruction(
            pc=pc, legacy=_legacy_width(width), opcode=b"\x0f\xaf",
            width=width, reg=dst, rm=src,
        )
    immediate = operands[2]
    if not isinstance(immediate, _Imm):
        raise X86EncodeError("three-operand imul requires an immediate")
    if -128 <= immediate.value <= 127:
        opcode = b"\x6b"
        tail = _int_bytes(immediate.value, 1)
    else:
        opcode = b"\x69"
        immediate_width = 2 if width == 16 else 4
        _require_immediate_bits(
            immediate.value,
            immediate_width,
            owner="imul",
            signed_only=True,
        )
        tail = _int_bytes(immediate.value, immediate_width)
    base = _modrm_instruction(
        pc=pc, legacy=_legacy_width(width), opcode=opcode,
        width=width, reg=dst, rm=src,
    )
    return EncodedInstruction(base.code + tail, base.relocations)


def _encode_group_unary(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 1 or not isinstance(operands[0], (_Reg, _Mem)):
        raise X86EncodeError(f"{mnemonic} expects one r/m operand")
    operand = operands[0]
    width = _operand_width(operand)
    if width not in (8, 16, 32, 64):
        raise X86EncodeError(f"{mnemonic} has no width")
    group = {"neg": 3, "div": 6, "idiv": 7}[mnemonic]
    pseudo = _Reg("group", group, width)
    return _modrm_instruction(
        pc=pc, legacy=_legacy_width(width),
        opcode=b"\xf6" if width == 8 else b"\xf7",
        width=width, reg=pseudo, rm=operand,
    )


def _encode_shift(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2 or not isinstance(operands[0], (_Reg, _Mem)):
        raise X86EncodeError(f"{mnemonic} expects r/m, count")
    dst, count = operands
    width = _operand_width(dst)
    if width not in (8, 16, 32, 64):
        raise X86EncodeError(f"{mnemonic} has no width")
    group = {"shl": 4, "shr": 5, "sar": 7}[mnemonic]
    pseudo = _Reg("group", group, width)
    if isinstance(count, _Imm):
        _require_immediate_bits(count.value, 1, owner=mnemonic)
        opcode = b"\xc0" if width == 8 else b"\xc1"
        tail = _int_bytes(count.value, 1)
    elif isinstance(count, _Reg) and count.name == "cl":
        opcode = b"\xd2" if width == 8 else b"\xd3"
        tail = b""
    else:
        raise X86EncodeError(f"{mnemonic} count must be imm8 or cl")
    base = _modrm_instruction(
        pc=pc, legacy=_legacy_width(width), opcode=opcode,
        width=width, reg=pseudo, rm=dst,
    )
    return EncodedInstruction(base.code + tail, base.relocations)


def _encode_setcc(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    suffix = mnemonic[3:]
    if suffix not in _SET_CONDITIONS or len(operands) != 1:
        raise X86EncodeError(f"unsupported setcc {mnemonic!r}")
    dst = _require_gp(operands[0], owner=mnemonic)
    if dst.width != 8:
        raise X86EncodeError("setcc destination must be byte register")
    pseudo = _Reg("group", 0, 8)
    return _modrm_instruction(
        pc=pc, legacy=b"", opcode=bytes((0x0F, 0x90 + _SET_CONDITIONS[suffix])),
        width=8, reg=pseudo, rm=dst, force_rex=dst.needs_byte_rex,
    )


def _encode_cmovcc(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    suffix = mnemonic[4:]
    if suffix not in _SET_CONDITIONS or len(operands) != 2:
        raise X86EncodeError(f"unsupported conditional move {mnemonic!r}")
    dst = _require_gp(operands[0], owner=mnemonic)
    src = operands[1]
    if not isinstance(src, (_Reg, _Mem)):
        raise X86EncodeError(f"{mnemonic} source must be r/m")
    width = _same_width(dst, src, owner=mnemonic)
    if width not in (16, 32, 64):
        raise X86EncodeError(f"{mnemonic} byte form does not exist")
    return _modrm_instruction(
        pc=pc,
        legacy=_legacy_width(width),
        opcode=bytes((0x0F, 0x40 + _SET_CONDITIONS[suffix])),
        width=width,
        reg=dst,
        rm=src,
    )


def _encode_extend(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError(f"{mnemonic} expects two operands")
    dst = _require_gp(operands[0], owner=mnemonic)
    src = operands[1]
    src_width = _operand_width(src)
    if not isinstance(src, (_Reg, _Mem)):
        raise X86EncodeError(f"{mnemonic} source must be r/m")
    if mnemonic == "movsxd":
        if dst.width != 64 or src_width != 32:
            raise X86EncodeError("movsxd requires r64, r/m32")
        return _modrm_instruction(
            pc=pc, legacy=b"", opcode=b"\x63", width=64, reg=dst, rm=src,
        )
    if src_width not in (8, 16) or dst.width not in (16, 32, 64) or src_width >= dst.width:
        raise X86EncodeError(f"{mnemonic} width pair not proven")
    opcode = {
        ("movzx", 8): b"\x0f\xb6",
        ("movzx", 16): b"\x0f\xb7",
        ("movsx", 8): b"\x0f\xbe",
        ("movsx", 16): b"\x0f\xbf",
    }[(mnemonic, src_width)]
    return _modrm_instruction(
        pc=pc, legacy=_legacy_width(dst.width), opcode=opcode,
        width=dst.width, reg=dst, rm=src,
    )


def _encode_xchg_like(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError(f"{mnemonic} expects two operands")
    rm, reg = operands
    reg = _require_gp(reg, owner=mnemonic)
    if not isinstance(rm, (_Reg, _Mem)):
        raise X86EncodeError(f"{mnemonic} expects r/m, register")
    width = _same_width(rm, reg, owner=mnemonic)
    if mnemonic == "xchg":
        opcode = b"\x86" if width == 8 else b"\x87"
    elif mnemonic == "xadd":
        opcode = b"\x0f\xc0" if width == 8 else b"\x0f\xc1"
    else:
        opcode = b"\x0f\xb0" if width == 8 else b"\x0f\xb1"
    return _modrm_instruction(
        pc=pc, legacy=_legacy_width(width), opcode=opcode,
        width=width, reg=reg, rm=rm,
    )


def _encode_sse(mnemonic: str, operands: list, pc: int) -> EncodedInstruction:
    if len(operands) != 2:
        raise X86EncodeError(f"{mnemonic} expects two operands")
    dst, src = operands
    scalar_ops = {
        "movss": (b"\xf3", 0x10, 0x11, 32),
        "movsd": (b"\xf2", 0x10, 0x11, 64),
        "addss": (b"\xf3", 0x58, None, 32),
        "addsd": (b"\xf2", 0x58, None, 64),
        "subss": (b"\xf3", 0x5C, None, 32),
        "subsd": (b"\xf2", 0x5C, None, 64),
        "mulss": (b"\xf3", 0x59, None, 32),
        "mulsd": (b"\xf2", 0x59, None, 64),
        "divss": (b"\xf3", 0x5E, None, 32),
        "divsd": (b"\xf2", 0x5E, None, 64),
        "sqrtss": (b"\xf3", 0x51, None, 32),
        "sqrtsd": (b"\xf2", 0x51, None, 64),
        "ucomiss": (b"", 0x2E, None, 32),
        "ucomisd": (b"\x66", 0x2E, None, 64),
        # The self emitter only uses packed xor as a register zero/sign-mask
        # operation.  Its 128-bit memory spelling is deliberately outside the
        # finite scalar memory vocabulary.
        "xorps": (b"", 0x57, None, None),
        "xorpd": (b"\x66", 0x57, None, None),
        "cvtsd2ss": (b"\xf2", 0x5A, None, 64),
        "cvtss2sd": (b"\xf3", 0x5A, None, 32),
    }
    if mnemonic in scalar_ops:
        legacy, load_opcode, store_opcode, memory_width = scalar_ops[mnemonic]
        if isinstance(dst, _Mem):
            if store_opcode is None:
                raise X86EncodeError(f"{mnemonic} has no memory-destination form")
            if memory_width is None or dst.width != memory_width:
                raise X86EncodeError(f"{mnemonic} memory width mismatch")
            src_reg = _require_xmm(src, owner=mnemonic)
            return _modrm_instruction(
                pc=pc, legacy=legacy, opcode=bytes((0x0F, store_opcode)),
                width=32, reg=src_reg, rm=dst,
            )
        dst_reg = _require_xmm(dst, owner=mnemonic)
        if not isinstance(src, (_Reg, _Mem)) or (isinstance(src, _Reg) and src.kind != "xmm"):
            raise X86EncodeError(f"{mnemonic} source must be XMM/memory")
        if isinstance(src, _Mem) and (
            memory_width is None or src.width != memory_width
        ):
            raise X86EncodeError(f"{mnemonic} memory width mismatch")
        return _modrm_instruction(
            pc=pc, legacy=legacy, opcode=bytes((0x0F, load_opcode)),
            width=32, reg=dst_reg, rm=src,
        )
    if mnemonic in ("movd", "movq"):
        width = 32 if mnemonic == "movd" else 64
        if isinstance(dst, _Reg) and dst.kind == "xmm":
            src_reg = _require_gp(src, owner=mnemonic)
            if src_reg.width != width:
                raise X86EncodeError(f"{mnemonic} source width mismatch")
            return _modrm_instruction(
                pc=pc, legacy=b"\x66", opcode=b"\x0f\x6e",
                width=width, reg=dst, rm=src_reg,
            )
        dst_reg = _require_gp(dst, owner=mnemonic)
        src_reg = _require_xmm(src, owner=mnemonic)
        if dst_reg.width != width:
            raise X86EncodeError(f"{mnemonic} destination width mismatch")
        return _modrm_instruction(
            pc=pc, legacy=b"\x66", opcode=b"\x0f\x7e",
            width=width, reg=src_reg, rm=dst_reg,
        )
    if mnemonic in ("cvtsi2ss", "cvtsi2sd"):
        dst_reg = _require_xmm(dst, owner=mnemonic)
        src_reg = _require_gp(src, owner=mnemonic)
        if src_reg.width not in (32, 64):
            raise X86EncodeError(f"{mnemonic} source must be i32/i64")
        return _modrm_instruction(
            pc=pc, legacy=b"\xf3" if mnemonic.endswith("ss") else b"\xf2",
            opcode=b"\x0f\x2a", width=src_reg.width,
            reg=dst_reg, rm=src_reg,
        )
    if mnemonic in ("cvttss2si", "cvttsd2si"):
        dst_reg = _require_gp(dst, owner=mnemonic)
        src_reg = _require_xmm(src, owner=mnemonic)
        if dst_reg.width not in (32, 64):
            raise X86EncodeError(f"{mnemonic} destination must be i32/i64")
        return _modrm_instruction(
            pc=pc, legacy=b"\xf3" if mnemonic.startswith("cvttss") else b"\xf2",
            opcode=b"\x0f\x2c", width=dst_reg.width,
            reg=dst_reg, rm=src_reg,
        )
    raise X86EncodeError(f"SSE mnemonic {mnemonic!r} not proven")


def _branch(
    mnemonic: str,
    operands: list,
    *,
    pc: int,
    labels: dict[str, tuple[str, int]],
    section_name: str,
) -> EncodedInstruction:
    if len(operands) != 1:
        raise X86EncodeError(f"{mnemonic} expects one target")
    target = operands[0]
    if isinstance(target, _Reg):
        if mnemonic not in ("call", "jmp") or target.kind != "gp" or target.width != 64:
            raise X86EncodeError(f"indirect {mnemonic} target not proven")
        pseudo = _Reg("group", 2 if mnemonic == "call" else 4, 64)
        return _modrm_instruction(
            pc=pc, legacy=b"", opcode=b"\xff", width=64,
            reg=pseudo, rm=target,
        )
    if not isinstance(target, _Target):
        raise X86EncodeError(f"{mnemonic} target must be a symbol")
    if mnemonic == "call":
        opcode = b"\xe8"
        relocation_type = R_X86_64_PLT32
    elif mnemonic == "jmp":
        opcode = b"\xe9"
        relocation_type = R_X86_64_PLT32
    elif mnemonic.startswith("j") and mnemonic[1:] in _SET_CONDITIONS:
        opcode = bytes((0x0F, 0x80 + _SET_CONDITIONS[mnemonic[1:]]))
        relocation_type = R_X86_64_PLT32
    else:
        raise X86EncodeError(f"branch mnemonic {mnemonic!r} not proven")
    width = len(opcode) + 4
    label = labels.get(target.name)
    if label is not None and label[0] == section_name:
        displacement = label[1] - (pc + width)
        if displacement < -(1 << 31) or displacement >= (1 << 31):
            raise X86EncodeError(
                f"branch target {target.name!r} is outside rel32 range"
            )
        return EncodedInstruction(opcode + _int_bytes(displacement, 4))
    return EncodedInstruction(
        opcode + b"\0\0\0\0",
        (EncodedRelocation(
            pc + len(opcode), target.name, relocation_type, -4,
        ),),
    )


def encode_instruction(
    line: str,
    *,
    pc: int,
    labels: dict[str, tuple[str, int]],
    section_name: str,
) -> EncodedInstruction:
    """Encode one normalized Intel-syntax instruction at ``pc``."""
    stripped = line.strip()
    if not stripped:
        return EncodedInstruction(b"")
    mnemonic, _, rest = stripped.partition(" ")
    mnemonic = mnemonic.lower()
    if mnemonic == "lock":
        nested_mnemonic = rest.strip().partition(" ")[0].lower()
        if nested_mnemonic not in ("xadd", "cmpxchg"):
            raise X86EncodeError(
                "lock prefix is only proven for xadd/cmpxchg"
            )
        nested = encode_instruction(
            rest,
            pc=pc + 1,
            labels=labels,
            section_name=section_name,
        )
        return EncodedInstruction(
            b"\xf0" + nested.code,
            nested.relocations,
        )
    operands = [_parse_operand(item) for item in _split_operands(rest)]
    if mnemonic == "mov":
        return _encode_mov(operands, pc)
    if mnemonic in _BINARY_REG_RM:
        return _encode_binary(mnemonic, operands, pc)
    if mnemonic == "test":
        return _encode_test(operands, pc)
    if mnemonic == "lea":
        return _encode_lea(operands, pc)
    if mnemonic == "imul":
        return _encode_imul(operands, pc)
    if mnemonic in ("neg", "div", "idiv"):
        return _encode_group_unary(mnemonic, operands, pc)
    if mnemonic in ("shl", "shr", "sar"):
        return _encode_shift(mnemonic, operands, pc)
    if mnemonic.startswith("set"):
        return _encode_setcc(mnemonic, operands, pc)
    if mnemonic.startswith("cmov"):
        return _encode_cmovcc(mnemonic, operands, pc)
    if mnemonic in ("movzx", "movsx", "movsxd"):
        return _encode_extend(mnemonic, operands, pc)
    if mnemonic in ("xchg", "xadd", "cmpxchg"):
        return _encode_xchg_like(mnemonic, operands, pc)
    if mnemonic in {
        "movss", "movsd", "movd", "movq", "xorps", "xorpd",
        "addss", "addsd", "subss", "subsd", "mulss", "mulsd",
        "divss", "divsd", "sqrtss", "sqrtsd", "ucomiss", "ucomisd",
        "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si",
        "cvtsd2ss", "cvtss2sd",
    }:
        return _encode_sse(mnemonic, operands, pc)
    if mnemonic in ("call", "jmp") or (
        mnemonic.startswith("j") and mnemonic[1:] in _SET_CONDITIONS
    ):
        return _branch(
            mnemonic, operands, pc=pc, labels=labels,
            section_name=section_name,
        )
    if mnemonic in ("push", "pop"):
        if len(operands) != 1:
            raise X86EncodeError(f"{mnemonic} expects one register")
        reg = _require_gp(operands[0], owner=mnemonic)
        if reg.width != 64:
            raise X86EncodeError(f"{mnemonic} only supports r64")
        opcode = (0x50 if mnemonic == "push" else 0x58) + reg.low
        return EncodedInstruction(_rex(w=False, r=0, x=0, b=reg.high) + bytes((opcode,)))
    fixed = {
        "nop": b"\x90",
        "cdq": b"\x99",
        "cqo": b"\x48\x99",
        "syscall": b"\x0f\x05",
        "mfence": b"\x0f\xae\xf0",
        "ret": b"\xc3",
        "ud2": b"\x0f\x0b",
    }
    if mnemonic in fixed:
        if operands:
            raise X86EncodeError(f"{mnemonic} takes no operands")
        return EncodedInstruction(fixed[mnemonic])
    raise X86EncodeError(f"x86_64 self emitter instruction not proven: {line!r}")
