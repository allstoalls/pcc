"""A64 instruction encoder for the self backend's own asm vocabulary.

LINK-P1-MACHO-OBJ-SWITCH slice 1: the self path today emits textual asm and
shells out to as(1); direct object emission needs these bytes encoded by pcc.
The vocabulary is the *emitter's*, not the ISA's — the sized inventory
(docs/goal/evidence/2026-08-01-obj-switch-encoder-sized.md) is ~78 mnemonics,
and this module implements the measured core, one operand shape at a time,
exactly as the self backend writes them.

Fail closed everywhere: an unknown mnemonic, register spelling, immediate
range, or operand shape raises `EncodeError`. A silent mis-encoding produces
a plausible instruction stream that crashes far from the bug; a refusal names
the missing shape and points at the differential suite to extend.

The oracle is as(1): `tests/python/test_arm64_encode.py` assembles every
supported shape with the system assembler and byte-compares.

`assemble_text()` is two-pass (labels first, then encoding) and returns the
code bytes plus the extern relocations in `macho_obj` form, so its output
plugs straight into `emit_object`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import macho_spec as spec
from .aarch64_fp_immediates import direct_fp_immediate_encoding
from .macho_obj import DataInCodeRegion, Relocation
from .self_backend_value_arena import CompilerIntArena


class EncodeError(Exception):
    """The instruction is outside the differentially proven subset."""


STRUCTURED_RELOCATION_NONE = 0
STRUCTURED_RELOCATION_BRANCH26 = 1
STRUCTURED_RELOCATION_PAGE21 = 2
STRUCTURED_RELOCATION_PAGEOFF12 = 3
STRUCTURED_RELOCATION_GOT_LOAD_PAGE21 = 4
STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12 = 5
# Compiler-private fixups, resolved using the final text layout before object
# publication. Positive kinds above remain native-object relocations.
STRUCTURED_FIXUP_CALL = -1
STRUCTURED_FIXUP_BRANCH26 = -26
STRUCTURED_FIXUP_BRANCH19 = -19

EMITTED_INSTRUCTION_FALLBACK = 0
EMITTED_INSTRUCTION_UNSCALED = 1
EMITTED_INSTRUCTION_MOVE = 2
EMITTED_INSTRUCTION_CALL = 3
EMITTED_INSTRUCTION_SCALAR = 4


def encode_emitted_nop_parts() -> int:
    """Canonical fixed word shared by text and native fragment producers."""
    return 0xD503201F


def validate_emitted_label_name(name: str) -> None:
    """Accept one emitted symbol, never an assembly line or directive."""
    if name.isascii() and name.isidentifier():
        return
    if not _is_symbol(name) or not name.isascii():
        raise EncodeError(f"invalid emitted label {name!r}")
    identifier = name
    if "." in identifier:
        identifier = identifier.replace(".", "_")
    if "$" in identifier:
        identifier = identifier.replace("$", "_")
    if not identifier.isidentifier():
        raise EncodeError(f"invalid emitted label {name!r}")


# --- operand parsing --------------------------------------------------------

_XREG_ALIASES = {"fp": 29, "lr": 30}


def _reg(tok: str) -> tuple[int, bool]:
    """Parse a register -> (number, is64). sp/zr map to 31 in their domains."""
    t = tok.strip().lower().rstrip(",")
    if t == "sp":
        return 31, True
    if t == "wsp":
        return 31, False
    if t == "xzr":
        return 31, True
    if t == "wzr":
        return 31, False
    if t in _XREG_ALIASES:
        return _XREG_ALIASES[t], True
    if t and t[0] in "xw" and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n <= 30:
            return n, t[0] == "x"
    raise EncodeError(f"bad register {tok!r}")


def _imm(tok: str) -> int:
    t = tok.strip().rstrip(",")
    if not t.startswith("#"):
        raise EncodeError(f"expected immediate, got {tok!r}")
    return int(t[1:], 0)


def _fp_imm8(tok: str) -> int:
    """Encode the direct FP literals emitted by the self backend.

    AArch64's scalar ``fmov`` immediate is an encoded 8-bit floating-point
    value rather than an IEEE bit pattern.  The materializer currently emits
    only these two direct literals; all other constants travel through a GPR.
    Keep this inventory exact and fail closed when the emitter grows.
    """
    t = tok.strip().rstrip(",")
    if not t.startswith("#"):
        raise EncodeError(f"expected floating immediate, got {tok!r}")
    literal = t[1:]
    encoded = direct_fp_immediate_encoding(literal)
    if encoded is None:
        raise EncodeError(f"floating immediate {tok!r} is not proven")
    return encoded


def _vreg8b(tok: str) -> int:
    """Parse 'vN.8b' -> N (the only vector shape the emitter uses: popcount)."""
    t = tok.strip().rstrip(",").lower()
    if t.endswith(".8b") and t.startswith("v") and t[1:-3].isdigit():
        n = int(t[1:-3])
        if 0 <= n <= 31:
            return n
    raise EncodeError(f"bad vector register {tok!r} (only vN.8b is proven)")


def _freg(tok: str) -> tuple[int, str]:
    """Parse a float register -> (number, 'd'|'s')."""
    t = tok.strip().lower().rstrip(",")
    if t and t[0] in "ds" and t[1:].isdigit():
        n = int(t[1:])
        if 0 <= n <= 31:
            return n, t[0]
    raise EncodeError(f"bad float register {tok!r}")


def _reg_kind(tok: str) -> str:
    """'x'/'w'/'d'/'s' for dispatching mixed-domain instructions."""
    t = tok.strip().lower().rstrip(",")
    if t in ("sp", "xzr", "lr", "fp") or (t and t[0] == "x" and t[1:].isdigit()):
        return "x"
    if t in ("wsp", "wzr") or (t and t[0] == "w" and t[1:].isdigit()):
        return "w"
    if t and t[0] == "d" and t[1:].isdigit():
        return "d"
    if t and t[0] == "s" and t[1:].isdigit():
        return "s"
    raise EncodeError(f"bad register {tok!r}")


_COND = {
    "eq": 0, "ne": 1, "cs": 2, "hs": 2, "cc": 3, "lo": 3, "mi": 4, "pl": 5,
    "vs": 6, "vc": 7, "hi": 8, "ls": 9, "ge": 10, "lt": 11, "gt": 12, "le": 13,
}


def _cond(tok: str) -> int:
    t = tok.strip().rstrip(",").lower()
    if t not in _COND:
        raise EncodeError(f"bad condition code {tok!r}")
    return _COND[t]


def _split_operands(rest: str) -> list[str]:
    """Split on commas not inside brackets: 'x0, [x1, #8]' -> ['x0', '[x1, #8]']."""
    out, depth, cur = [], 0, ""
    for ch in rest:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _mem(tok: str) -> tuple[int, int, str]:
    """Parse '[xN]' / '[xN, #imm]' -> (base, imm, mode). mode: '', 'pre', 'post'
    is handled by the caller for ldp/stp."""
    t = tok.strip()
    if not (t.startswith("[") and (t.endswith("]") or t.endswith("]!"))):
        raise EncodeError(f"bad memory operand {tok!r}")
    pre = t.endswith("]!")
    inner = t[1:-2] if pre else t[1:-1]
    parts = [p.strip() for p in inner.split(",")]
    base, base64 = _reg(parts[0])
    if not base64:
        raise EncodeError(f"memory base must be 64-bit: {tok!r}")
    imm = _imm(parts[1]) if len(parts) > 1 else 0
    if len(parts) > 2:
        raise EncodeError(f"unsupported memory operand {tok!r}")
    return base, imm, "pre" if pre else ""


# --- bitmask (logical) immediates -------------------------------------------


def _logical_imm(value: int, is64: bool) -> tuple[int, int, int]:
    """Encode a bitmask immediate -> (N, immr, imms); EncodeError if impossible."""
    size = 64 if is64 else 32
    mask = (1 << size) - 1
    value &= mask
    if value in (0, mask):
        raise EncodeError(f"immediate {value:#x} is not a valid bitmask immediate")
    # Find the smallest element size whose repetition produces the value.
    for esize in (2, 4, 8, 16, 32, 64):
        if esize > size:
            break
        emask = (1 << esize) - 1
        elem = value & emask
        repeated = 0
        for i in range(0, size, esize):
            repeated |= elem << i
        if repeated != value:
            continue
        # elem must be a rotated run of ones: rotate until it is 0..01..1
        for rot in range(esize):
            rotated = ((elem >> rot) | (elem << (esize - rot))) & emask
            ones = bin(rotated).count("1")
            if rotated == (1 << ones) - 1 and 0 < ones < esize:
                immr = (esize - rot) % esize
                imms = ({2: 0x3C, 4: 0x38, 8: 0x30, 16: 0x20, 32: 0x00, 64: 0x00}[esize]
                        | (ones - 1))
                n_bit = 1 if esize == 64 else 0
                return n_bit, immr, imms
    raise EncodeError(f"immediate {value:#x} is not a valid bitmask immediate")


# --- per-family encoders -----------------------------------------------------


def _sf(is64: bool) -> int:
    return 1 << 31 if is64 else 0


_REGISTER_SHIFT_KINDS = {"lsl": 0, "lsr": 1, "asr": 2, "ror": 3}


def _shifted_register_operand(ops, index: int, line: str, allowed):
    """Parse an optional ``<kind> #<amount>`` register-shift operand.

    Returns ``(shift, shift_kind)``; ``(0, 0)`` when the operand is absent.
    Anything outside the proven subset raises instead of being ignored: a
    dropped shift silently encoded the unshifted word, which turned the self
    backend's scaled GEP adds (``add x11, x9, x10, lsl #3``) into unscaled
    ones on every dynamically indexed array.
    """
    if len(ops) <= index:
        return 0, 0
    if len(ops) > index + 1:
        raise EncodeError(f"too many operands: {line!r}")
    text = ops[index].strip().lower()
    head, sep, amount = text.partition("#")
    head = head.strip()
    if not sep or head not in allowed or head not in _REGISTER_SHIFT_KINDS:
        raise EncodeError(f"shift {ops[index]!r} not in the proven subset")
    return int(amount, 0), _REGISTER_SHIFT_KINDS[head]


def _enc_addsub_reg(
    op_sub: int,
    rd,
    rn,
    rm,
    is64: bool,
    set_flags=0,
    shift: int = 0,
    shift_kind: int = 0,
) -> int:
    # shift_kind: 0 lsl, 1 lsr, 2 asr (ror is reserved for add/sub). The shift
    # type packs into bits 23-22 and imm6 into bits 15-10 of the shifted-
    # register form; callers that pass no shift keep the unshifted encoding.
    limit = 63 if is64 else 31
    if not 0 <= shift <= limit:
        raise EncodeError(f"add/sub register shift {shift} out of range")
    if not 0 <= shift_kind <= 2:
        raise EncodeError(f"add/sub register shift kind {shift_kind} invalid")
    return (
        _sf(is64) | (op_sub << 30) | (set_flags << 29) | 0x0B000000
        | (shift_kind << 22) | (shift << 10)
        | (rm << 16) | (rn << 5) | rd
    )


def _enc_addsub_ext(op_sub: int, rd, rn, rm, is64: bool, set_flags=0) -> int:
    """ADD/SUB (extended register) — the only register form that means SP.

    In the shifted-register form above, register number 31 decodes as XZR,
    so `sub sp, sp, x15` assembled that way becomes `sub xzr, xzr, x15`: a
    silent no-op that leaves the frame unallocated. Only the extended form
    reads 31 as SP. option=UXTX (64-bit) / UXTW (32-bit), imm3=0.
    """
    option = 3 if is64 else 2
    return (
        _sf(is64) | (op_sub << 30) | (set_flags << 29) | 0x0B200000
        | (rm << 16) | (option << 13) | (rn << 5) | rd
    )


def _is_sp_token(tok: str) -> bool:
    return tok.strip().lower().rstrip(",") in ("sp", "wsp")


def _enc_addsub_imm(op_sub: int, rd, rn, imm: int, is64: bool, set_flags=0) -> int:
    if not 0 <= imm <= 0xFFF:
        raise EncodeError(f"add/sub immediate {imm} out of unsigned 12-bit range")
    return (
        _sf(is64) | (op_sub << 30) | (set_flags << 29) | 0x11000000
        | (imm << 10) | (rn << 5) | rd
    )


def _enc_logical_reg(
    opc: int, rd, rn, rm, is64: bool, shift: int = 0, shift_kind: int = 0
) -> int:
    # opc: 0 and, 1 orr, 2 eor, 3 ands
    # shift_kind: 0 lsl, 1 lsr, 2 asr, 3 ror.  The shift type packs into bits
    # 23-22 and the amount into imm6 at bits 15-10 of the shifted-register
    # form.  Callers that pass no shift keep the unshifted encoding.
    limit = 63 if is64 else 31
    if not 0 <= shift <= limit:
        raise EncodeError(f"logical register shift {shift} out of range")
    if not 0 <= shift_kind <= 3:
        raise EncodeError(f"logical register shift kind {shift_kind} invalid")
    return (
        _sf(is64)
        | (opc << 29)
        | 0x0A000000
        | (shift_kind << 22)
        | (shift << 10)
        | (rm << 16)
        | (rn << 5)
        | rd
    )


def _enc_movewide(opc: int, rd, imm16: int, shift: int, is64: bool) -> int:
    # opc: 2 movz, 3 movk
    if not 0 <= imm16 <= 0xFFFF:
        raise EncodeError(f"move-wide immediate {imm16} out of 16-bit range")
    if shift % 16 != 0 or shift // 16 > (3 if is64 else 1):
        raise EncodeError(f"move-wide shift {shift} invalid")
    return (
        _sf(is64) | (opc << 29) | 0x12800000
        | ((shift // 16) << 21) | (imm16 << 5) | rd
    )


def _enc_ldst_unscaled(size: int, opc: int, rt, rn, imm9: int) -> int:
    if not -256 <= imm9 <= 255:
        raise EncodeError(f"unscaled offset {imm9} out of 9-bit signed range")
    return (
        (size << 30) | 0x38000000 | (opc << 22)
        | ((imm9 & 0x1FF) << 12) | (rn << 5) | rt
    )


_EMITTED_REG_X = 0
_EMITTED_REG_W = 1
_EMITTED_REG_D = 2
_EMITTED_REG_S = 3


def _emitted_register_code(token: str) -> int:
    """Return number|kind<<6 for exact emitter register spellings."""

    if token == "sp" or token == "xzr":
        return 31 | (_EMITTED_REG_X << 6)
    if token == "wsp" or token == "wzr":
        return 31 | (_EMITTED_REG_W << 6)
    if token == "fp":
        return 29 | (_EMITTED_REG_X << 6)
    if token == "lr":
        return 30 | (_EMITTED_REG_X << 6)
    if len(token) < 2 or token[0] not in "xwds":
        return -1
    digits = token[1:]
    if not digits.isdigit():
        return -1
    number = int(digits)
    if number < 0 or number > 30:
        return -1
    first = token[0]
    if first == "x":
        kind = _EMITTED_REG_X
    elif first == "w":
        kind = _EMITTED_REG_W
    elif first == "d":
        kind = _EMITTED_REG_D
    else:
        kind = _EMITTED_REG_S
    return number | (kind << 6)


def encode_emitted_unscaled_load_store(line: str) -> int | None:
    """Encode one exact emitter ldur/stur family line without operand objects.

    Unrecognised or malformed text returns ``None`` so the ordinary assembler
    remains the diagnostic oracle. The structured transport calls this only
    after target-final labels and peepholes are frozen.
    """

    if not line.startswith("  "):
        return None
    mnemonic_end = line.find(" ", 2)
    if mnemonic_end < 0:
        return None
    mnemonic = line[2:mnemonic_end]
    if mnemonic not in ("ldur", "stur", "ldurb", "sturb"):
        return None
    register_start = mnemonic_end + 1
    register_end = line.find(", ", register_start)
    if register_end < 0:
        return None
    register_code = _emitted_register_code(
        line[register_start:register_end]
    )
    if register_code < 0:
        return None
    memory = line[register_end + 2:]
    if len(memory) < 3 or memory[0] != "[" or memory[-1] != "]":
        return None
    inner = memory[1:-1]
    offset_start = inner.find(", #")
    if offset_start < 0:
        base_token = inner
        immediate = 0
    else:
        base_token = inner[:offset_start]
        immediate_token = inner[offset_start + 3:]
        try:
            immediate = int(immediate_token, 0)
        except ValueError:
            return None
    if immediate < -256 or immediate > 255:
        return None
    base_code = _emitted_register_code(base_token)
    if base_code < 0 or base_code >> 6 != _EMITTED_REG_X:
        return None
    register_kind = register_code >> 6
    register_number = register_code & 63
    base_number = base_code & 63
    is_load = mnemonic == "ldur" or mnemonic == "ldurb"
    if mnemonic == "ldurb" or mnemonic == "sturb":
        if register_kind != _EMITTED_REG_W:
            return None
        return _enc_ldst_unscaled(
            0,
            1 if is_load else 0,
            register_number,
            base_number,
            immediate,
        )
    if register_kind == _EMITTED_REG_X:
        size = 3
        vector = 0
    elif register_kind == _EMITTED_REG_W:
        size = 2
        vector = 0
    elif register_kind == _EMITTED_REG_D:
        size = 3
        vector = 1 << 26
    elif register_kind == _EMITTED_REG_S:
        size = 2
        vector = 1 << 26
    else:
        return None
    return _enc_ldst_unscaled(
        size,
        1 if is_load else 0,
        register_number,
        base_number,
        immediate,
    ) | vector


def encode_emitted_move(line: str) -> int | None:
    """Encode exact emitter mov/movz/movk spellings without operand lists."""

    if not line.startswith("  "):
        return None
    mnemonic_end = line.find(" ", 2)
    if mnemonic_end < 0:
        return None
    mnemonic = line[2:mnemonic_end]
    if mnemonic not in ("mov", "movz", "movk"):
        return None
    destination_start = mnemonic_end + 1
    destination_end = line.find(", ", destination_start)
    if destination_end < 0:
        return None
    destination_token = line[destination_start:destination_end]
    destination_code = _emitted_register_code(destination_token)
    if destination_code < 0:
        return None
    destination_kind = destination_code >> 6
    if destination_kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
        return None
    destination_number = destination_code & 63
    rest = line[destination_end + 2:]
    destination_is_64 = destination_kind == _EMITTED_REG_X

    if mnemonic == "mov":
        if "," in rest or " " in rest:
            return None
        source_code = _emitted_register_code(rest)
        if source_code < 0 or source_code >> 6 != destination_kind:
            return None
        source_number = source_code & 63
        if destination_token in ("sp", "wsp") or rest in ("sp", "wsp"):
            return _enc_addsub_imm(
                0,
                destination_number,
                source_number,
                0,
                destination_is_64,
            )
        return _enc_logical_reg(
            1,
            destination_number,
            31,
            source_number,
            destination_is_64,
        )

    shift_marker = ", lsl #"
    shift_start = rest.find(shift_marker)
    if shift_start < 0:
        immediate_token = rest
        shift = 0
    else:
        immediate_token = rest[:shift_start]
        shift_token = rest[shift_start + len(shift_marker):]
        try:
            shift = int(shift_token, 0)
        except ValueError:
            return None
    if not immediate_token.startswith("#"):
        return None
    try:
        immediate = int(immediate_token[1:], 0)
    except ValueError:
        return None
    if immediate < 0 or immediate > 0xFFFF:
        return None
    max_shift = 48 if destination_is_64 else 16
    if shift < 0 or shift > max_shift or shift % 16:
        return None
    return _enc_movewide(
        2 if mnemonic == "movz" else 3,
        destination_number,
        immediate,
        shift,
        destination_is_64,
    )


def encode_emitted_move_register_parts(
    destination: str,
    source: str,
) -> int:
    destination_code = _emitted_register_code(destination)
    source_code = _emitted_register_code(source)
    if destination_code < 0 or source_code < 0:
        raise EncodeError("bad emitted move register")
    destination_kind = destination_code >> 6
    if (
        destination_kind not in (_EMITTED_REG_X, _EMITTED_REG_W)
        or source_code >> 6 != destination_kind
    ):
        raise EncodeError("emitted move register widths differ")
    destination_number = destination_code & 63
    source_number = source_code & 63
    is_64 = destination_kind == _EMITTED_REG_X
    if destination in ("sp", "wsp") or source in ("sp", "wsp"):
        return _enc_addsub_imm(
            0,
            destination_number,
            source_number,
            0,
            is_64,
        )
    return _enc_logical_reg(
        1,
        destination_number,
        31,
        source_number,
        is_64,
    )


def encode_emitted_movewide_parts(
    mnemonic: str,
    destination: str,
    immediate: int,
    shift: int = 0,
) -> int:
    if mnemonic not in ("movz", "movk"):
        raise EncodeError("unsupported emitted move-wide mnemonic " + mnemonic)
    if shift < 0:
        raise EncodeError("negative emitted move-wide shift")
    destination_code = _emitted_register_code(destination)
    if destination_code < 0:
        raise EncodeError("bad emitted move-wide register " + destination)
    destination_kind = destination_code >> 6
    if destination_kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
        raise EncodeError("emitted move-wide requires an integer register")
    return _enc_movewide(
        2 if mnemonic == "movz" else 3,
        destination_code & 63,
        immediate,
        shift,
        destination_kind == _EMITTED_REG_X,
    )


def encode_emitted_addsub_register_parts(
    mnemonic: str,
    destination: str,
    left: str,
    right: str,
) -> int:
    if mnemonic not in ("add", "sub"):
        raise EncodeError("unsupported emitted add/sub mnemonic " + mnemonic)
    destination_code = _emitted_register_code(destination)
    left_code = _emitted_register_code(left)
    right_code = _emitted_register_code(right)
    if destination_code < 0 or left_code < 0 or right_code < 0:
        raise EncodeError("bad emitted arithmetic register")
    kind = destination_code >> 6
    if (
        kind not in (_EMITTED_REG_X, _EMITTED_REG_W)
        or left_code >> 6 != kind
        or right_code >> 6 != kind
    ):
        raise EncodeError("emitted arithmetic register widths differ")
    rd = destination_code & 63
    rn = left_code & 63
    rm = right_code & 63
    is_64 = kind == _EMITTED_REG_X
    if destination in ("sp", "wsp") or left in ("sp", "wsp"):
        return _enc_addsub_ext(1 if mnemonic == "sub" else 0, rd, rn, rm, is_64)
    return _enc_addsub_reg(1 if mnemonic == "sub" else 0, rd, rn, rm, is_64)


def encode_emitted_addsub_immediate_parts(
    mnemonic: str,
    destination: str,
    left: str,
    immediate: int,
) -> int:
    if mnemonic not in ("add", "sub"):
        raise EncodeError("unsupported emitted add/sub mnemonic " + mnemonic)
    destination_code = _emitted_register_code(destination)
    left_code = _emitted_register_code(left)
    if destination_code < 0 or left_code < 0:
        raise EncodeError("bad emitted arithmetic register")
    kind = destination_code >> 6
    if (
        kind not in (_EMITTED_REG_X, _EMITTED_REG_W)
        or left_code >> 6 != kind
    ):
        raise EncodeError("emitted arithmetic register widths differ")
    return _enc_addsub_imm(
        1 if mnemonic == "sub" else 0,
        destination_code & 63,
        left_code & 63,
        immediate,
        kind == _EMITTED_REG_X,
    )


def encode_emitted_compare_register_parts(left: str, right: str) -> int:
    left_code = _emitted_register_code(left)
    right_code = _emitted_register_code(right)
    if left_code < 0 or right_code < 0:
        raise EncodeError("bad emitted compare register")
    kind = left_code >> 6
    if (
        kind not in (_EMITTED_REG_X, _EMITTED_REG_W)
        or right_code >> 6 != kind
    ):
        raise EncodeError("emitted compare register widths differ")
    if left in ("sp", "wsp"):
        return _enc_addsub_ext(
            1,
            31,
            left_code & 63,
            right_code & 63,
            kind == _EMITTED_REG_X,
            set_flags=1,
        )
    return _enc_addsub_reg(
        1,
        31,
        left_code & 63,
        right_code & 63,
        kind == _EMITTED_REG_X,
        set_flags=1,
    )


def encode_emitted_compare_immediate_parts(left: str, immediate: int) -> int:
    left_code = _emitted_register_code(left)
    if left_code < 0 or left_code >> 6 not in (
        _EMITTED_REG_X,
        _EMITTED_REG_W,
    ):
        raise EncodeError("bad emitted compare register")
    return _enc_addsub_imm(
        1,
        31,
        left_code & 63,
        immediate,
        left_code >> 6 == _EMITTED_REG_X,
        set_flags=1,
    )


def encode_emitted_cset_parts(destination: str, condition: str) -> int:
    destination_code = _emitted_register_code(destination)
    if destination_code < 0 or destination_code >> 6 not in (
        _EMITTED_REG_X,
        _EMITTED_REG_W,
    ):
        raise EncodeError("bad emitted cset register")
    return (
        _sf(destination_code >> 6 == _EMITTED_REG_X)
        | 0x1A800400
        | (31 << 16)
        | ((_cond(condition) ^ 1) << 12)
        | (31 << 5)
        | (destination_code & 63)
    )


def encode_emitted_adrp_parts(destination: str) -> int:
    register = _emitted_register_code(destination)
    if register < 0 or register >> 6 != _EMITTED_REG_X:
        raise EncodeError("emitted adrp requires an x register")
    return 0x90000000 | (register & 63)


def encode_emitted_add_pageoff_parts(destination: str, base: str) -> int:
    destination_code = _emitted_register_code(destination)
    base_code = _emitted_register_code(base)
    if (
        destination_code < 0
        or base_code < 0
        or destination_code >> 6 != _EMITTED_REG_X
        or base_code >> 6 != _EMITTED_REG_X
    ):
        raise EncodeError("emitted PAGEOFF add requires x registers")
    return _enc_addsub_imm(
        0,
        destination_code & 63,
        base_code & 63,
        0,
        True,
    )


def encode_emitted_ldr_got_pageoff_parts(
    destination: str,
    base: str,
) -> int:
    destination_code = _emitted_register_code(destination)
    base_code = _emitted_register_code(base)
    if (
        destination_code < 0
        or base_code < 0
        or destination_code >> 6 != _EMITTED_REG_X
        or base_code >> 6 != _EMITTED_REG_X
    ):
        raise EncodeError("emitted GOTPAGEOFF load requires x registers")
    return _enc_ldst_unsigned(
        3,
        1,
        destination_code & 63,
        base_code & 63,
        0,
    )


def encode_emitted_branch_base_parts(
    mnemonic: str,
    register: str = "",
) -> int:
    if mnemonic == "b":
        return 0x14000000
    if mnemonic.startswith("b."):
        return 0x54000000 | _cond(mnemonic[2:])
    if mnemonic == "cbz" or mnemonic == "cbnz":
        register_code = _emitted_register_code(register)
        if register_code < 0 or register_code >> 6 not in (
            _EMITTED_REG_X,
            _EMITTED_REG_W,
        ):
            raise EncodeError("bad emitted zero-branch register")
        word = (
            0x35000000 if mnemonic == "cbnz" else 0x34000000
        ) | (register_code & 63)
        if register_code >> 6 == _EMITTED_REG_X:
            word |= 0x80000000
        return word
    raise EncodeError("unsupported emitted branch mnemonic " + mnemonic)


def emitted_direct_call_target(line: str) -> str | None:
    """Return the exact target of an emitter-owned direct ``bl`` line."""

    prefix = "  bl "
    if not line.startswith(prefix):
        return None
    target = line[len(prefix):]
    if (
        not target
        or " " in target
        or "\t" in target
        or "," in target
        or "@" in target
    ):
        return None
    if target[0] != "_" and not target[0].isalpha():
        return None
    return target


def _enc_ldst_unsigned(size: int, opc: int, rt, rn, imm: int) -> int:
    scale = size
    if imm % (1 << scale) != 0:
        raise EncodeError(f"offset {imm} not aligned for the access size")
    imm12 = imm >> scale
    if not 0 <= imm12 <= 0xFFF:
        raise EncodeError(f"offset {imm} out of unsigned-offset range")
    return (
        (size << 30) | 0x39000000 | (opc << 22)
        | (imm12 << 10) | (rn << 5) | rt
    )


def encode_emitted_load_store_parts(
    mnemonic: str,
    register: str,
    base: str,
    immediate: int,
) -> int:
    """Encode one emitter-owned scalar memory instruction from typed parts."""

    unscaled = mnemonic in (
        "ldur",
        "stur",
        "ldurb",
        "sturb",
        "ldurh",
        "sturh",
    )
    scaled = mnemonic in (
        "ldr",
        "str",
        "ldrb",
        "strb",
        "ldrh",
        "strh",
    )
    if not unscaled and not scaled:
        raise EncodeError("unsupported emitted load/store mnemonic " + mnemonic)
    register_code = _emitted_register_code(register)
    base_code = _emitted_register_code(base)
    if register_code < 0:
        raise EncodeError("bad emitted load/store register " + register)
    if base_code < 0 or base_code >> 6 != _EMITTED_REG_X:
        raise EncodeError("bad emitted load/store base " + base)

    register_kind = register_code >> 6
    byte_access = mnemonic.endswith("b")
    half_access = mnemonic.endswith("h")
    if byte_access or half_access:
        if register_kind != _EMITTED_REG_W:
            raise EncodeError("narrow emitted load/store requires a w register")
        size = 0 if byte_access else 1
        vector = 0
    elif register_kind == _EMITTED_REG_X:
        size = 3
        vector = 0
    elif register_kind == _EMITTED_REG_W:
        size = 2
        vector = 0
    elif register_kind == _EMITTED_REG_D:
        size = 3
        vector = 1 << 26
    elif register_kind == _EMITTED_REG_S:
        size = 2
        vector = 1 << 26
    else:
        raise EncodeError("unsupported emitted load/store register class")

    is_load = mnemonic.startswith("ld")
    if unscaled:
        return _enc_ldst_unscaled(
            size,
            1 if is_load else 0,
            register_code & 63,
            base_code & 63,
            immediate,
        ) | vector
    return _enc_ldst_unsigned(
        size,
        1 if is_load else 0,
        register_code & 63,
        base_code & 63,
        immediate,
    ) | vector


def _enc_ldstp(load: int, mode: str, rt, rt2, rn, imm: int, is64: bool) -> int:
    scale = 3 if is64 else 2
    if imm % (1 << scale) != 0:
        raise EncodeError(f"pair offset {imm} not aligned")
    imm7 = imm >> scale
    if not -64 <= imm7 <= 63:
        raise EncodeError(f"pair offset {imm} out of 7-bit range")
    base = {"pre": 0x29800000, "post": 0x28C00000, "signed": 0x29000000}[mode]
    if mode == "signed" and load:
        base |= 1 << 22
    elif load:
        base |= 1 << 22
    word = base | ((imm7 & 0x7F) << 15) | (rt2 << 10) | (rn << 5) | rt
    if is64:
        word |= 0x80000000
    return word


def encode_emitted_frame_pair_parts(load: bool) -> int:
    if load:
        return _enc_ldstp(1, "post", 29, 30, 31, 16, True)
    return _enc_ldstp(0, "pre", 29, 30, 31, -16, True)


def _emitted_register_code_span(text: str, start: int, end: int) -> int:
    """Parse one exact emitter register without allocating a token string."""

    length = end - start
    if length == 2:
        first = text[start]
        second = text[start + 1]
        if first == "s" and second == "p":
            return 31 | (_EMITTED_REG_X << 6)
        if first == "f" and second == "p":
            return 29 | (_EMITTED_REG_X << 6)
        if first == "l" and second == "r":
            return 30 | (_EMITTED_REG_X << 6)
    if length == 3:
        first = text[start]
        if first == "w" and text[start + 1] == "s" and text[start + 2] == "p":
            return 31 | (_EMITTED_REG_W << 6)
        if first == "x" and text[start + 1] == "z" and text[start + 2] == "r":
            return 31 | (_EMITTED_REG_X << 6)
        if first == "w" and text[start + 1] == "z" and text[start + 2] == "r":
            return 31 | (_EMITTED_REG_W << 6)
    if length < 2:
        return -1
    first = text[start]
    if first not in "xwds":
        return -1
    number = 0
    index = start + 1
    while index < end:
        code = ord(text[index]) - 48
        if code < 0 or code > 9:
            return -1
        number = number * 10 + code
        index += 1
    if number > 30:
        return -1
    if first == "x":
        kind = _EMITTED_REG_X
    elif first == "w":
        kind = _EMITTED_REG_W
    elif first == "d":
        kind = _EMITTED_REG_D
    else:
        kind = _EMITTED_REG_S
    return number | (kind << 6)


def _emitted_immediate_span(text: str, start: int, end: int) -> int | None:
    if start >= end or text[start] != "#":
        return None
    index = start + 1
    negative = False
    if index < end and text[index] == "-":
        negative = True
        index += 1
    base = 10
    if index + 1 < end and text[index] == "0" and text[index + 1] in "xX":
        base = 16
        index += 2
    if index >= end:
        return None
    value = 0
    while index < end:
        code = ord(text[index])
        if 48 <= code <= 57:
            digit = code - 48
        elif base == 16 and 65 <= code <= 70:
            digit = code - 55
        elif base == 16 and 97 <= code <= 102:
            digit = code - 87
        else:
            return None
        if digit >= base:
            return None
        value = value * base + digit
        index += 1
    return -value if negative else value


def _emitted_condition_span(text: str, start: int, end: int) -> int:
    if end - start != 2:
        return -1
    first = text[start]
    second = text[start + 1]
    if first == "e" and second == "q":
        return 0
    if first == "n" and second == "e":
        return 1
    if (first == "c" and second == "s") or (first == "h" and second == "s"):
        return 2
    if (first == "c" and second == "c") or (first == "l" and second == "o"):
        return 3
    if first == "m" and second == "i":
        return 4
    if first == "p" and second == "l":
        return 5
    if first == "v" and second == "s":
        return 6
    if first == "v" and second == "c":
        return 7
    if first == "h" and second == "i":
        return 8
    if first == "l" and second == "s":
        return 9
    if first == "g" and second == "e":
        return 10
    if first == "l" and second == "t":
        return 11
    if first == "g" and second == "t":
        return 12
    if first == "l" and second == "e":
        return 13
    return -1


def _emitted_branch_bits(
    target: str,
    width_bits: int,
    text_offset: int,
    label_offsets: dict[str, int],
) -> int:
    if target not in label_offsets:
        return -1
    delta = label_offsets[target] - text_offset
    if delta % 4:
        return -1
    words = delta >> 2
    limit = 1 << (width_bits - 1)
    if words < -limit or words >= limit:
        return -1
    return words & ((1 << width_bits) - 1)


def _emitted_symbol_id(
    symbol: str,
    symbol_ids: dict[str, int],
    symbol_names: list[str],
) -> int:
    if symbol in symbol_ids:
        return symbol_ids[symbol]
    symbol_id = len(symbol_names)
    symbol_ids[symbol] = symbol_id
    symbol_names.append(symbol)
    return symbol_id


def intern_emitted_symbol(
    symbol: str,
    symbol_ids: dict[str, int],
    symbol_names: list[str],
) -> int:
    return _emitted_symbol_id(symbol, symbol_ids, symbol_names)


def append_emitted_local_branch_record(
    base_word: int,
    width_bits: int,
    target: str,
    line_index: int,
    text_offset: int,
    label_offsets: dict[str, int],
    records: CompilerIntArena,
) -> None:
    bits = _emitted_branch_bits(
        target,
        width_bits,
        text_offset,
        label_offsets,
    )
    if bits < 0:
        raise EncodeError("direct emitted branch target is out of range")
    word = base_word | (bits if width_bits == 26 else bits << 5)
    records.append4(
        line_index,
        word,
        STRUCTURED_RELOCATION_NONE,
        -1,
    )


def append_emitted_direct_call_record(
    target: str,
    line_index: int,
    text_offset: int,
    current_atom_offset: int,
    label_offsets: dict[str, int] | None,
    records: CompilerIntArena,
    symbol_ids: dict[str, int],
    symbol_names: list[str],
    link: bool = True,
) -> None:
    """Publish a direct B/BL using the same Mach-O atom/relocation rule."""

    base_word = 0x94000000 if link else 0x14000000

    if label_offsets is None:
        symbol_id = _emitted_symbol_id(target, symbol_ids, symbol_names)
        records.append4(
            line_index, base_word,
            STRUCTURED_FIXUP_CALL if link else STRUCTURED_FIXUP_BRANCH26, symbol_id,
        )
        return
    target_offset = label_offsets.get(target)
    inline_call = target_offset is not None and (
        target.startswith("L") or target_offset == current_atom_offset
    )
    if inline_call:
        bits = _emitted_branch_bits(
            target,
            26,
            text_offset,
            label_offsets,
        )
        if bits < 0:
            raise EncodeError("direct emitted call target is out of range")
        records.append4(
            line_index,
            base_word | bits,
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return
    symbol_id = _emitted_symbol_id(
        target,
        symbol_ids,
        symbol_names,
    )
    records.append4(
        line_index,
        base_word,
        STRUCTURED_RELOCATION_BRANCH26,
        symbol_id,
    )


def append_emitted_instruction_record(
    line: str,
    line_index: int,
    text_offset: int,
    current_atom_offset: int,
    label_offsets: dict[str, int] | None,
    records: CompilerIntArena,
    symbol_ids: dict[str, int],
    symbol_names: list[str],
) -> int:
    """Publish one exact instruction; None layout defers branches to the builder."""

    word = encode_emitted_unscaled_load_store(line)
    if word is not None:
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_UNSCALED
    word = encode_emitted_move(line)
    if word is not None:
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_MOVE
    call_target = emitted_direct_call_target(line)
    if call_target is not None:
        try:
            append_emitted_direct_call_record(
                call_target,
                line_index,
                text_offset,
                current_atom_offset,
                label_offsets,
                records,
                symbol_ids,
                symbol_names,
            )
        except EncodeError:
            return EMITTED_INSTRUCTION_FALLBACK
        return EMITTED_INSTRUCTION_CALL

    fixed_word = -1
    if line == "  ret":
        fixed_word = 0xD65F03C0
    elif line == "  nop":
        fixed_word = encode_emitted_nop_parts()
    elif line == "  paciasp":
        fixed_word = 0xD503233F
    elif line == "  autiasp":
        fixed_word = 0xD50323BF
    if fixed_word >= 0:
        records.append4(
            line_index, fixed_word, STRUCTURED_RELOCATION_NONE, -1
        )
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  adrp "):
        destination_start = 7
        destination_end = line.find(", ", destination_start)
        if destination_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        destination = _emitted_register_code_span(
            line, destination_start, destination_end
        )
        if destination < 0 or destination >> 6 != _EMITTED_REG_X:
            return EMITTED_INSTRUCTION_FALLBACK
        target_start = destination_end + 2
        relocation_kind = STRUCTURED_RELOCATION_NONE
        suffix_length = 0
        if line.endswith("@GOTPAGE"):
            relocation_kind = STRUCTURED_RELOCATION_GOT_LOAD_PAGE21
            suffix_length = len("@GOTPAGE")
        elif line.endswith("@PAGE"):
            relocation_kind = STRUCTURED_RELOCATION_PAGE21
            suffix_length = len("@PAGE")
        if relocation_kind == STRUCTURED_RELOCATION_NONE:
            return EMITTED_INSTRUCTION_FALLBACK
        symbol = line[target_start : len(line) - suffix_length]
        if not symbol:
            return EMITTED_INSTRUCTION_FALLBACK
        symbol_id = _emitted_symbol_id(symbol, symbol_ids, symbol_names)
        records.append4(
            line_index,
            0x90000000 | (destination & 63),
            relocation_kind,
            symbol_id,
        )
        return EMITTED_INSTRUCTION_SCALAR

    arithmetic_prefix = ""
    op_sub = 0
    if line.startswith("  add "):
        arithmetic_prefix = "  add "
    elif line.startswith("  sub "):
        arithmetic_prefix = "  sub "
        op_sub = 1
    if arithmetic_prefix:
        first_start = len(arithmetic_prefix)
        first_end = line.find(", ", first_start)
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        third_start = second_end + 2
        if first_end < 0 or second_end < 0 or third_start >= len(line):
            return EMITTED_INSTRUCTION_FALLBACK
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        if first < 0 or second < 0 or first >> 6 != second >> 6:
            return EMITTED_INSTRUCTION_FALLBACK
        kind = first >> 6
        if kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        is_64 = kind == _EMITTED_REG_X
        if op_sub == 0 and line.endswith("@PAGEOFF"):
            symbol = line[third_start : -len("@PAGEOFF")]
            if not symbol:
                return EMITTED_INSTRUCTION_FALLBACK
            symbol_id = _emitted_symbol_id(symbol, symbol_ids, symbol_names)
            word = _enc_addsub_imm(
                0,
                first & 63,
                second & 63,
                0,
                is_64,
            )
            records.append4(
                line_index, word, STRUCTURED_RELOCATION_PAGEOFF12, symbol_id
            )
            return EMITTED_INSTRUCTION_SCALAR
        immediate = _emitted_immediate_span(line, third_start, len(line))
        if immediate is not None:
            if immediate < 0 or immediate > 0xFFF:
                return EMITTED_INSTRUCTION_FALLBACK
            word = _enc_addsub_imm(
                op_sub,
                first & 63,
                second & 63,
                immediate,
                is_64,
            )
        else:
            third = _emitted_register_code_span(line, third_start, len(line))
            if third < 0 or third >> 6 != kind:
                return EMITTED_INSTRUCTION_FALLBACK
            first_is_sp = line[first_start:first_end] in ("sp", "wsp")
            second_is_sp = line[second_start:second_end] in ("sp", "wsp")
            if first_is_sp or second_is_sp:
                word = _enc_addsub_ext(
                    op_sub,
                    first & 63,
                    second & 63,
                    third & 63,
                    is_64,
                )
            else:
                word = _enc_addsub_reg(
                    op_sub,
                    first & 63,
                    second & 63,
                    third & 63,
                    is_64,
                )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  cmp "):
        first_start = 6
        first_end = line.find(", ", first_start)
        second_start = first_end + 2
        if first_end < 0 or second_start >= len(line):
            return EMITTED_INSTRUCTION_FALLBACK
        first = _emitted_register_code_span(line, first_start, first_end)
        if first < 0 or first >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        is_64 = first >> 6 == _EMITTED_REG_X
        immediate = _emitted_immediate_span(line, second_start, len(line))
        if immediate is not None:
            if immediate < 0 or immediate > 0xFFF:
                return EMITTED_INSTRUCTION_FALLBACK
            word = _enc_addsub_imm(
                1, 31, first & 63, immediate, is_64, set_flags=1
            )
        else:
            second = _emitted_register_code_span(line, second_start, len(line))
            if second < 0 or second >> 6 != first >> 6:
                return EMITTED_INSTRUCTION_FALLBACK
            if line[first_start:first_end] in ("sp", "wsp"):
                word = _enc_addsub_ext(
                    1, 31, first & 63, second & 63, is_64, set_flags=1
                )
            else:
                word = _enc_addsub_reg(
                    1, 31, first & 63, second & 63, is_64, set_flags=1
                )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  cset "):
        destination_start = 7
        destination_end = line.find(", ", destination_start)
        condition_start = destination_end + 2
        if destination_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        destination = _emitted_register_code_span(
            line, destination_start, destination_end
        )
        condition = _emitted_condition_span(line, condition_start, len(line))
        if destination < 0 or condition < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        kind = destination >> 6
        if kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        word = (
            _sf(kind == _EMITTED_REG_X)
            | 0x1A800400
            | (31 << 16)
            | ((condition ^ 1) << 12)
            | (31 << 5)
            | (destination & 63)
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    branch_prefix = ""
    branch_base = 0
    branch_width = 0
    if line.startswith("  b "):
        branch_prefix = "  b "
        branch_base = 0x14000000
        branch_width = 26
    elif line.startswith("  b."):
        condition_end = line.find(" ", 4)
        condition = _emitted_condition_span(line, 4, condition_end)
        if condition_end < 0 or condition < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        target = line[condition_end + 1 :]
        if label_offsets is None:
            records.append4(
                line_index, 0x54000000 | condition, STRUCTURED_FIXUP_BRANCH19,
                _emitted_symbol_id(target, symbol_ids, symbol_names),
            )
            return EMITTED_INSTRUCTION_SCALAR
        bits = _emitted_branch_bits(target, 19, text_offset, label_offsets)
        if bits < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        records.append4(
            line_index,
            0x54000000 | (bits << 5) | condition,
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return EMITTED_INSTRUCTION_SCALAR
    if branch_prefix:
        target = line[len(branch_prefix) :]
        if not target.startswith("L"):
            try:
                append_emitted_direct_call_record(
                    target, line_index, text_offset, current_atom_offset,
                    label_offsets, records, symbol_ids, symbol_names, False,
                )
            except EncodeError:
                return EMITTED_INSTRUCTION_FALLBACK
            return EMITTED_INSTRUCTION_SCALAR
        if label_offsets is None:
            records.append4(
                line_index, branch_base, STRUCTURED_FIXUP_BRANCH26,
                _emitted_symbol_id(target, symbol_ids, symbol_names),
            )
            return EMITTED_INSTRUCTION_SCALAR
        bits = _emitted_branch_bits(
            target, branch_width, text_offset, label_offsets
        )
        if bits < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        records.append4(
            line_index,
            branch_base | bits,
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return EMITTED_INSTRUCTION_SCALAR

    zero_branch_prefix = ""
    zero_branch_base = 0
    if line.startswith("  cbz "):
        zero_branch_prefix = "  cbz "
        zero_branch_base = 0x34000000
    elif line.startswith("  cbnz "):
        zero_branch_prefix = "  cbnz "
        zero_branch_base = 0x35000000
    if zero_branch_prefix:
        register_start = len(zero_branch_prefix)
        register_end = line.find(", ", register_start)
        target_start = register_end + 2
        if register_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        register = _emitted_register_code_span(line, register_start, register_end)
        if register < 0 or register >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        target = line[target_start:]
        if label_offsets is None:
            word = zero_branch_base | (register & 63)
            if register >> 6 == _EMITTED_REG_X:
                word |= 0x80000000
            records.append4(
                line_index, word, STRUCTURED_FIXUP_BRANCH19,
                _emitted_symbol_id(target, symbol_ids, symbol_names),
            )
            return EMITTED_INSTRUCTION_SCALAR
        bits = _emitted_branch_bits(target, 19, text_offset, label_offsets)
        if bits < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        word = zero_branch_base | (bits << 5) | (register & 63)
        if register >> 6 == _EMITTED_REG_X:
            word |= 0x80000000
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    load_store_prefix = ""
    load_store_size = -1
    load_store_opc = 0
    if line.startswith("  ldr "):
        load_store_prefix = "  ldr "
        load_store_opc = 1
    elif line.startswith("  str "):
        load_store_prefix = "  str "
    elif line.startswith("  ldrb "):
        load_store_prefix = "  ldrb "
        load_store_size = 0
        load_store_opc = 1
    elif line.startswith("  strb "):
        load_store_prefix = "  strb "
        load_store_size = 0
    elif line.startswith("  ldrh "):
        load_store_prefix = "  ldrh "
        load_store_size = 1
        load_store_opc = 1
    elif line.startswith("  strh "):
        load_store_prefix = "  strh "
        load_store_size = 1
    if load_store_prefix:
        register_start = len(load_store_prefix)
        register_end = line.find(", ", register_start)
        memory_start = register_end + 2
        if register_end < 0 or memory_start >= len(line):
            return EMITTED_INSTRUCTION_FALLBACK
        register = _emitted_register_code_span(line, register_start, register_end)
        if register < 0 or line[memory_start] != "[" or line[-1] != "]":
            return EMITTED_INSTRUCTION_FALLBACK
        inner_start = memory_start + 1
        inner_end = len(line) - 1
        comma = line.find(", ", inner_start, inner_end)
        base_end = inner_end if comma < 0 else comma
        base = _emitted_register_code_span(line, inner_start, base_end)
        if base < 0 or base >> 6 != _EMITTED_REG_X:
            return EMITTED_INSTRUCTION_FALLBACK
        if comma >= 0 and line.endswith("@GOTPAGEOFF]"):
            if load_store_prefix != "  ldr " or register >> 6 != _EMITTED_REG_X:
                return EMITTED_INSTRUCTION_FALLBACK
            symbol_start = comma + 2
            symbol_end = inner_end - len("@GOTPAGEOFF")
            symbol = line[symbol_start:symbol_end]
            if not symbol:
                return EMITTED_INSTRUCTION_FALLBACK
            symbol_id = _emitted_symbol_id(symbol, symbol_ids, symbol_names)
            word = _enc_ldst_unsigned(3, 1, register & 63, base & 63, 0)
            records.append4(
                line_index,
                word,
                STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12,
                symbol_id,
            )
            return EMITTED_INSTRUCTION_SCALAR
        immediate = 0
        if comma >= 0:
            parsed = _emitted_immediate_span(line, comma + 2, inner_end)
            if parsed is None or parsed < 0:
                return EMITTED_INSTRUCTION_FALLBACK
            immediate = parsed
        kind = register >> 6
        vector = 0
        if load_store_size < 0:
            if kind == _EMITTED_REG_X:
                load_store_size = 3
            elif kind == _EMITTED_REG_W:
                load_store_size = 2
            elif kind == _EMITTED_REG_D:
                load_store_size = 3
                vector = 1 << 26
            elif kind == _EMITTED_REG_S:
                load_store_size = 2
                vector = 1 << 26
            else:
                return EMITTED_INSTRUCTION_FALLBACK
        elif kind != _EMITTED_REG_W:
            return EMITTED_INSTRUCTION_FALLBACK
        scale = 1 << load_store_size
        if immediate % scale or immediate // scale > 0xFFF:
            return EMITTED_INSTRUCTION_FALLBACK
        word = _enc_ldst_unsigned(
            load_store_size,
            load_store_opc,
            register & 63,
            base & 63,
            immediate,
        ) | vector
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    pair_prefix = ""
    pair_load = 0
    if line.startswith("  ldp "):
        pair_prefix = "  ldp "
        pair_load = 1
    elif line.startswith("  stp "):
        pair_prefix = "  stp "
    if pair_prefix:
        first_start = len(pair_prefix)
        first_end = line.find(", ", first_start)
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        memory_start = second_end + 2
        if first_end < 0 or second_end < 0 or memory_start >= len(line):
            return EMITTED_INSTRUCTION_FALLBACK
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        if first < 0 or second < 0 or first >> 6 != second >> 6:
            return EMITTED_INSTRUCTION_FALLBACK
        kind = first >> 6
        if kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        mode = "signed"
        immediate = 0
        memory_end = len(line)
        post_marker = line.find("], #", memory_start)
        if post_marker >= 0:
            mode = "post"
            base_end = post_marker
            immediate_value = _emitted_immediate_span(
                line, post_marker + 3, len(line)
            )
            if immediate_value is None:
                return EMITTED_INSTRUCTION_FALLBACK
            immediate = immediate_value
            if line[memory_start] != "[" or line[base_end] != "]":
                return EMITTED_INSTRUCTION_FALLBACK
            base = _emitted_register_code_span(
                line, memory_start + 1, base_end
            )
        elif line.endswith("]!"):
            mode = "pre"
            memory_end = len(line) - 2
            comma = line.find(", ", memory_start + 1, memory_end)
            if comma < 0:
                return EMITTED_INSTRUCTION_FALLBACK
            base = _emitted_register_code_span(
                line, memory_start + 1, comma
            )
            immediate_value = _emitted_immediate_span(
                line, comma + 2, memory_end
            )
            if immediate_value is None:
                return EMITTED_INSTRUCTION_FALLBACK
            immediate = immediate_value
        elif line.endswith("]"):
            memory_end = len(line) - 1
            comma = line.find(", ", memory_start + 1, memory_end)
            if comma < 0:
                base = _emitted_register_code_span(
                    line, memory_start + 1, memory_end
                )
            else:
                base = _emitted_register_code_span(
                    line, memory_start + 1, comma
                )
                immediate_value = _emitted_immediate_span(
                    line, comma + 2, memory_end
                )
                if immediate_value is None:
                    return EMITTED_INSTRUCTION_FALLBACK
                immediate = immediate_value
        else:
            return EMITTED_INSTRUCTION_FALLBACK
        if base < 0 or base >> 6 != _EMITTED_REG_X:
            return EMITTED_INSTRUCTION_FALLBACK
        scale = 8 if kind == _EMITTED_REG_X else 4
        if immediate % scale or immediate // scale < -64 or immediate // scale > 63:
            return EMITTED_INSTRUCTION_FALLBACK
        word = _enc_ldstp(
            pair_load,
            mode,
            first & 63,
            second & 63,
            base & 63,
            immediate,
            kind == _EMITTED_REG_X,
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    logical_prefix = ""
    logical_opc = -1
    if line.startswith("  and "):
        logical_prefix = "  and "
        logical_opc = 0
    elif line.startswith("  orr "):
        logical_prefix = "  orr "
        logical_opc = 1
    elif line.startswith("  eor "):
        logical_prefix = "  eor "
        logical_opc = 2
    if logical_prefix:
        first_start = len(logical_prefix)
        first_end = line.find(", ", first_start)
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        third_start = second_end + 2
        if first_end < 0 or second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        if first < 0 or second < 0 or first >> 6 != second >> 6:
            return EMITTED_INSTRUCTION_FALLBACK
        kind = first >> 6
        if kind not in (_EMITTED_REG_X, _EMITTED_REG_W):
            return EMITTED_INSTRUCTION_FALLBACK
        immediate = _emitted_immediate_span(line, third_start, len(line))
        if immediate is not None:
            if logical_opc != 0:
                return EMITTED_INSTRUCTION_FALLBACK
            n_bit, immr, imms = _logical_imm(
                immediate, kind == _EMITTED_REG_X
            )
            word = (
                _sf(kind == _EMITTED_REG_X)
                | 0x12000000
                | (n_bit << 22)
                | (immr << 16)
                | (imms << 10)
                | ((second & 63) << 5)
                | (first & 63)
            )
        else:
            third = _emitted_register_code_span(line, third_start, len(line))
            if third < 0 or third >> 6 != kind:
                return EMITTED_INSTRUCTION_FALLBACK
            word = _enc_logical_reg(
                logical_opc,
                first & 63,
                second & 63,
                third & 63,
                kind == _EMITTED_REG_X,
            )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    variable_shift_op2 = -1
    variable_shift_prefix = ""
    if line.startswith("  lslv "):
        variable_shift_prefix = "  lslv "
        variable_shift_op2 = 0b1000
    elif line.startswith("  lsrv "):
        variable_shift_prefix = "  lsrv "
        variable_shift_op2 = 0b1001
    elif line.startswith("  asrv "):
        variable_shift_prefix = "  asrv "
        variable_shift_op2 = 0b1010
    if variable_shift_prefix:
        first_start = len(variable_shift_prefix)
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        third = _emitted_register_code_span(line, third_start, len(line))
        if (
            first < 0
            or second < 0
            or third < 0
            or first >> 6 != second >> 6
            or first >> 6 != third >> 6
            or first >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W)
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        word = (
            _sf(first >> 6 == _EMITTED_REG_X)
            | 0x1AC00000
            | ((third & 63) << 16)
            | (variable_shift_op2 << 10)
            | ((second & 63) << 5)
            | (first & 63)
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  asr "):
        first_start = 6
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        shift = _emitted_immediate_span(line, third_start, len(line))
        if (
            first < 0
            or second < 0
            or shift is None
            or first >> 6 != second >> 6
            or first >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W)
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        is_64 = first >> 6 == _EMITTED_REG_X
        width = 64 if is_64 else 32
        if shift < 0 or shift >= width:
            return EMITTED_INSTRUCTION_FALLBACK
        word = (
            _sf(is_64)
            | 0x13000000
            | ((1 << 22) if is_64 else 0)
            | (shift << 16)
            | ((width - 1) << 10)
            | ((second & 63) << 5)
            | (first & 63)
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    integer_three_prefix = ""
    integer_three_kind = 0
    if line.startswith("  mul "):
        integer_three_prefix = "  mul "
        integer_three_kind = 1
    elif line.startswith("  sdiv "):
        integer_three_prefix = "  sdiv "
        integer_three_kind = 2
    elif line.startswith("  smulh "):
        integer_three_prefix = "  smulh "
        integer_three_kind = 3
    if integer_three_prefix:
        first_start = len(integer_three_prefix)
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        third = _emitted_register_code_span(line, third_start, len(line))
        if (
            first < 0
            or second < 0
            or third < 0
            or first >> 6 != second >> 6
            or first >> 6 != third >> 6
            or first >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W)
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        is_64 = first >> 6 == _EMITTED_REG_X
        if integer_three_kind == 1:
            word = (
                _sf(is_64)
                | 0x1B007C00
                | ((third & 63) << 16)
                | ((second & 63) << 5)
                | (first & 63)
            )
        elif integer_three_kind == 2:
            word = (
                _sf(is_64)
                | 0x1AC00800
                | (1 << 10)
                | ((third & 63) << 16)
                | ((second & 63) << 5)
                | (first & 63)
            )
        else:
            if not is_64:
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                0x9B407C00
                | ((third & 63) << 16)
                | ((second & 63) << 5)
                | (first & 63)
            )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  msub ") or line.startswith("  csel "):
        is_msub = line.startswith("  msub ")
        prefix = "  msub " if is_msub else "  csel "
        first_start = len(prefix)
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        third_end = line.find(", ", third_start)
        if third_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        fourth_start = third_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        third = _emitted_register_code_span(line, third_start, third_end)
        if (
            first < 0
            or second < 0
            or third < 0
            or first >> 6 != second >> 6
            or first >> 6 != third >> 6
            or first >> 6 not in (_EMITTED_REG_X, _EMITTED_REG_W)
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        is_64 = first >> 6 == _EMITTED_REG_X
        if is_msub:
            fourth = _emitted_register_code_span(
                line, fourth_start, len(line)
            )
            if fourth < 0 or fourth >> 6 != first >> 6:
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                _sf(is_64)
                | 0x1B000000
                | (1 << 15)
                | ((third & 63) << 16)
                | ((fourth & 63) << 10)
                | ((second & 63) << 5)
                | (first & 63)
            )
        else:
            condition = _emitted_condition_span(
                line, fourth_start, len(line)
            )
            if condition < 0:
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                _sf(is_64)
                | 0x1A800000
                | ((third & 63) << 16)
                | (condition << 12)
                | ((second & 63) << 5)
                | (first & 63)
            )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  blr "):
        register = _emitted_register_code_span(line, 6, len(line))
        if register < 0 or register >> 6 != _EMITTED_REG_X:
            return EMITTED_INSTRUCTION_FALLBACK
        records.append4(
            line_index,
            0xD63F0000 | ((register & 63) << 5),
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  brk "):
        immediate = _emitted_immediate_span(line, 6, len(line))
        if immediate is None or immediate < 0 or immediate > 0xFFFF:
            return EMITTED_INSTRUCTION_FALLBACK
        records.append4(
            line_index,
            0xD4200000 | (immediate << 5),
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return EMITTED_INSTRUCTION_SCALAR

    float_binary_prefix = ""
    float_binary_opcode = 0
    if line.startswith("  fmul "):
        float_binary_prefix = "  fmul "
        float_binary_opcode = 0x0800
    elif line.startswith("  fdiv "):
        float_binary_prefix = "  fdiv "
        float_binary_opcode = 0x1800
    elif line.startswith("  fadd "):
        float_binary_prefix = "  fadd "
        float_binary_opcode = 0x2800
    elif line.startswith("  fsub "):
        float_binary_prefix = "  fsub "
        float_binary_opcode = 0x3800
    if float_binary_prefix:
        first_start = len(float_binary_prefix)
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        third = _emitted_register_code_span(line, third_start, len(line))
        if (
            first < 0
            or second < 0
            or third < 0
            or first >> 6 != _EMITTED_REG_D
            or second >> 6 != _EMITTED_REG_D
            or third >> 6 != _EMITTED_REG_D
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        word = (
            0x1E600000
            | float_binary_opcode
            | ((third & 63) << 16)
            | ((second & 63) << 5)
            | (first & 63)
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  fneg "):
        first_start = 7
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, len(line))
        if (
            first < 0
            or second < 0
            or first >> 6 != _EMITTED_REG_D
            or second >> 6 != _EMITTED_REG_D
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        records.append4(
            line_index,
            0x1E614000 | ((second & 63) << 5) | (first & 63),
            STRUCTURED_RELOCATION_NONE,
            -1,
        )
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  fmov "):
        first_start = 7
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        if first < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        if line[second_start] == "#":
            literal = line[second_start + 1 :]
            immediate = direct_fp_immediate_encoding(literal)
            if immediate is None or first >> 6 not in (
                _EMITTED_REG_D,
                _EMITTED_REG_S,
            ):
                return EMITTED_INSTRUCTION_FALLBACK
            base = 0x1E601000 if first >> 6 == _EMITTED_REG_D else 0x1E201000
            word = base | (immediate << 13) | (first & 63)
        else:
            second = _emitted_register_code_span(
                line, second_start, len(line)
            )
            if second < 0:
                return EMITTED_INSTRUCTION_FALLBACK
            first_kind = first >> 6
            second_kind = second >> 6
            if first_kind == _EMITTED_REG_D and second_kind == _EMITTED_REG_D:
                word = 0x1E604000 | ((second & 63) << 5) | (first & 63)
            elif first_kind == _EMITTED_REG_D and second_kind == _EMITTED_REG_X:
                word = 0x9E670000 | ((second & 63) << 5) | (first & 63)
            elif first_kind == _EMITTED_REG_X and second_kind == _EMITTED_REG_D:
                word = 0x9E660000 | ((second & 63) << 5) | (first & 63)
            else:
                return EMITTED_INSTRUCTION_FALLBACK
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  scvtf ") or line.startswith("  fcvtzs "):
        is_scvtf = line.startswith("  scvtf ")
        prefix = "  scvtf " if is_scvtf else "  fcvtzs "
        first_start = len(prefix)
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, len(line))
        if first < 0 or second < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        if is_scvtf:
            if first >> 6 != _EMITTED_REG_D or second >> 6 not in (
                _EMITTED_REG_X,
                _EMITTED_REG_W,
            ):
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                0x9E620000
                if second >> 6 == _EMITTED_REG_X
                else 0x1E620000
            ) | ((second & 63) << 5) | (first & 63)
        else:
            if first >> 6 not in (
                _EMITTED_REG_X,
                _EMITTED_REG_W,
            ) or second >> 6 != _EMITTED_REG_D:
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                0x9E780000
                if first >> 6 == _EMITTED_REG_X
                else 0x1E780000
            ) | ((second & 63) << 5) | (first & 63)
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  fcmp "):
        first_start = 7
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        if first < 0 or first >> 6 != _EMITTED_REG_D:
            return EMITTED_INSTRUCTION_FALLBACK
        if line[second_start:] in ("#0", "#0.0"):
            word = 0x1E602008 | ((first & 63) << 5)
        else:
            second = _emitted_register_code_span(
                line, second_start, len(line)
            )
            if second < 0 or second >> 6 != _EMITTED_REG_D:
                return EMITTED_INSTRUCTION_FALLBACK
            word = (
                0x1E602000
                | ((second & 63) << 16)
                | ((first & 63) << 5)
            )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    if line.startswith("  fcsel "):
        first_start = 8
        first_end = line.find(", ", first_start)
        if first_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        second_start = first_end + 2
        second_end = line.find(", ", second_start)
        if second_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        third_start = second_end + 2
        third_end = line.find(", ", third_start)
        if third_end < 0:
            return EMITTED_INSTRUCTION_FALLBACK
        fourth_start = third_end + 2
        first = _emitted_register_code_span(line, first_start, first_end)
        second = _emitted_register_code_span(line, second_start, second_end)
        third = _emitted_register_code_span(line, third_start, third_end)
        condition = _emitted_condition_span(line, fourth_start, len(line))
        if (
            first < 0
            or second < 0
            or third < 0
            or condition < 0
            or first >> 6 != _EMITTED_REG_D
            or second >> 6 != _EMITTED_REG_D
            or third >> 6 != _EMITTED_REG_D
        ):
            return EMITTED_INSTRUCTION_FALLBACK
        word = (
            0x1E600C00
            | ((third & 63) << 16)
            | (condition << 12)
            | ((second & 63) << 5)
            | (first & 63)
        )
        records.append4(line_index, word, STRUCTURED_RELOCATION_NONE, -1)
        return EMITTED_INSTRUCTION_SCALAR

    return EMITTED_INSTRUCTION_FALLBACK


# --- the assembler -----------------------------------------------------------


@dataclass
class AssembledText:
    code: bytes
    relocations: list[Relocation]
    undefined: list[str]
    labels: dict[str, int]
    data_in_code: list[DataInCodeRegion]


def _is_symbol(tok: str) -> bool:
    t = tok.strip()
    return bool(t) and (t[0] == "_" or t[0].isalpha()) and "@" not in t


def _encode_inline_text_data(line: str) -> bytes:
    directive, _, rest = line.partition(" ")
    if directive == ".space":
        try:
            count = int(rest.strip(), 0)
        except ValueError as exc:
            raise EncodeError(f"bad inline {line!r}") from exc
        if count < 0:
            raise EncodeError(f"bad inline {line!r}")
        return b"\0" * count
    widths = {".byte": 1, ".short": 2, ".long": 4, ".quad": 8}
    width = widths[directive]
    parts: list[bytes] = []
    for item in (piece.strip() for piece in rest.split(",")):
        if not item:
            continue
        try:
            value = int(item, 0)
        except ValueError as exc:
            raise EncodeError(
                f"symbol-valued inline data not proven: {line!r}"
            ) from exc
        parts.append((value & ((1 << (width * 8)) - 1)).to_bytes(width, "little"))
    return b"".join(parts)


def _validate_text_fixup_word(word: int, kind: int) -> None:
    if kind == STRUCTURED_FIXUP_CALL:
        valid = word == 0x94000000
    elif kind == STRUCTURED_FIXUP_BRANCH26:
        valid = word == 0x14000000
    elif kind == STRUCTURED_FIXUP_BRANCH19:
        valid = (word & 0xFFFFFFF0) == 0x54000000 or (
            (word & 0x7FFFFFE0) in (0x34000000, 0x35000000)
        )
    else:
        raise EncodeError("structured fixup kind is not proven")
    if not valid:
        raise EncodeError("structured fixup requires an unpatched branch word")


class PackedAArch64TextBuilder:
    """One native entry/layout owner; textual instructions are an explicit adapter.

    The ordinary text API and structured directive driver share this parser.
    Encoded instructions occupy only arena scalars, never blank string slots.
    Label names and data blobs are module/section side tables; their remaining
    projection is distinct from the instruction-buffer closure.
    """

    def __init__(
        self,
        structured_symbol_names: list[str] | tuple[str, ...] = (),
        initial_capacity: int = 8,
    ) -> None:
        self.entries = CompilerIntArena(max(1, initial_capacity))
        self.structured_relocations = CompilerIntArena()
        self.instruction_lines: list[str] = []
        self.data_chunks: list[bytes] = []
        self.labels: dict[str, int] = {}
        self.data_in_code: list[DataInCodeRegion] = []
        self.structured_symbol_names = structured_symbol_names
        self.pc: int = 0
        self.active_data_start: int = -1
        self.active_data_kind: int = -1
        self.closed: bool = False

    def append_encoded(self, word: int, relocation_kind: int, symbol_id: int) -> None:
        if self.closed:
            raise EncodeError("text builder is closed")
        if word < 0 or word > 0xFFFFFFFF:
            raise EncodeError("structured instruction word is outside uint32")
        if relocation_kind == STRUCTURED_RELOCATION_NONE:
            if symbol_id != -1:
                raise EncodeError("non-relocating structured instruction has a symbol")
        elif relocation_kind in (
            STRUCTURED_RELOCATION_BRANCH26,
            STRUCTURED_RELOCATION_PAGE21,
            STRUCTURED_RELOCATION_PAGEOFF12,
            STRUCTURED_RELOCATION_GOT_LOAD_PAGE21,
            STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12,
            STRUCTURED_FIXUP_CALL,
            STRUCTURED_FIXUP_BRANCH26,
            STRUCTURED_FIXUP_BRANCH19,
        ):
            if symbol_id < 0 or symbol_id >= len(self.structured_symbol_names):
                raise EncodeError("structured instruction symbol id is out of range")
        else:
            raise EncodeError("structured instruction relocation kind is not proven")
        if relocation_kind < 0:
            _validate_text_fixup_word(word, relocation_kind)
        if self.active_data_start >= 0:
            raise EncodeError("structured instruction inside .data_region is not proven")
        if self.pc % 4:
            raise EncodeError("structured instruction at unaligned __text offset")
        if relocation_kind != STRUCTURED_RELOCATION_NONE:
            self.structured_relocations.append3(
                len(self.entries) // 2, relocation_kind, symbol_id,
            )
        self.entries.append2(2, word)
        self.pc += 4

    def append_branch(self, base_word: int, width_bits: int, symbol_id: int) -> None:
        if width_bits not in (19, 26):
            raise EncodeError("structured branch width is not proven")
        self.append_encoded(base_word, -width_bits, symbol_id)

    def append_call(self, symbol_id: int) -> None:
        self.append_encoded(0x94000000, STRUCTURED_FIXUP_CALL, symbol_id)

    def append_label(self, name: str) -> None:
        if self.closed:
            raise EncodeError("text builder is closed")
        if name in self.labels:
            raise EncodeError(f"duplicate label {name!r}")
        self.labels[name] = self.pc

    def append_line(self, raw: str) -> None:
        if self.closed:
            raise EncodeError("text builder is closed")
        line = raw.split(";")[0].split("//")[0].strip()
        if not line:
            return
        directive = line.split(None, 1)[0]
        if directive == ".p2align":
            parts = line.split()
            try:
                align_log2 = int(parts[1], 0) if len(parts) == 2 else -1
            except ValueError as exc:
                raise EncodeError(f"bad text alignment {line!r}") from exc
            if not 0 <= align_log2 <= 2:
                raise EncodeError(f"bad text alignment {line!r}")
            padding = (-self.pc) & ((1 << align_log2) - 1)
            if padding:
                if self.active_data_start < 0:
                    raise EncodeError("non-data text alignment padding is not proven")
                self.entries.append2(1, len(self.data_chunks))
                self.data_chunks.append(b"\0" * padding)
                self.pc += padding
            return
        if directive == ".data_region":
            if self.active_data_start >= 0:
                raise EncodeError("nested .data_region is not proven")
            parts = line.split()
            spelling = parts[1] if len(parts) == 2 else ""
            region_kinds = {
                "": spec.DICE_KIND_DATA,
                "jt8": spec.DICE_KIND_JUMP_TABLE8,
                "jt16": spec.DICE_KIND_JUMP_TABLE16,
                "jt32": spec.DICE_KIND_JUMP_TABLE32,
            }
            if len(parts) > 2 or spelling not in region_kinds:
                raise EncodeError(f"bad data-region kind in {line!r}")
            self.active_data_start = self.pc
            self.active_data_kind = region_kinds[spelling]
            return
        if directive == ".end_data_region":
            if line != ".end_data_region":
                raise EncodeError(f"bad data-region terminator {line!r}")
            if self.active_data_start < 0:
                raise EncodeError(".end_data_region without .data_region")
            if self.pc == self.active_data_start:
                raise EncodeError("empty data-in-code region is not proven")
            self.data_in_code.append(DataInCodeRegion(
                self.active_data_start, self.pc - self.active_data_start,
                self.active_data_kind,
            ))
            self.active_data_start = -1
            self.active_data_kind = -1
            return
        if directive in (".byte", ".short", ".long", ".quad", ".space"):
            if self.active_data_start < 0:
                raise EncodeError(f"inline data outside .data_region: {line!r}")
            raw_data = _encode_inline_text_data(line)
            self.entries.append2(1, len(self.data_chunks))
            self.data_chunks.append(raw_data)
            self.pc += len(raw_data)
            return
        if line.startswith("."):
            raise EncodeError(
                f"directive {line.split()[0]!r} reached the instruction "
                "assembler; sections are the caller's job"
            )
        if line.endswith(":"):
            name = line[:-1].strip()
            self.append_label(name)
            return
        if self.active_data_start >= 0:
            raise EncodeError("instruction inside .data_region is not proven")
        if self.pc % 4:
            raise EncodeError(
                f"instruction at unaligned __text offset {self.pc} after data region"
            )
        self.entries.append2(0, len(self.instruction_lines))
        self.instruction_lines.append(line)
        self.pc += 4

    def finish(self) -> AssembledText:
        try:
            if self.closed:
                raise EncodeError("text builder is closed")
            if self.active_data_start >= 0:
                raise EncodeError("unterminated .data_region")
            return _finalize_text_entries(
                self.entries, self.instruction_lines, self.data_chunks,
                self.structured_relocations, self.structured_symbol_names,
                self.labels, self.data_in_code,
            )
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.entries.close()
        self.structured_relocations.close()
        self.instruction_lines.clear()
        self.data_chunks.clear()
        self.closed = True


def assemble_text_lines(
    asm_lines: list[str],
    encoded_line_records: CompilerIntArena | None = None,
    structured_symbol_names: list[str] | tuple[str, ...] = (),
) -> AssembledText:
    """Compatibility line input over the canonical packed text/layout builder."""
    if encoded_line_records is not None and len(encoded_line_records) % 4:
        raise EncodeError(
            "structured instruction records need line/word/relocation/symbol"
        )
    encoded_record_count = (
        0 if encoded_line_records is None else len(encoded_line_records) // 4
    )
    builder = PackedAArch64TextBuilder(structured_symbol_names, max(1, len(asm_lines) * 2))
    try:
        encoded_record_index = 0
        line_index = 0
        while line_index < len(asm_lines):
            current_line_index = line_index
            raw = asm_lines[line_index]
            line_index += 1
            if encoded_record_index < encoded_record_count:
                encoded_offset = encoded_record_index * 4
                encoded_index = encoded_line_records.get_unchecked(encoded_offset)
                if encoded_index < current_line_index:
                    raise EncodeError("structured instruction indices are not ordered")
                if encoded_index == current_line_index:
                    builder.append_encoded(
                        encoded_line_records.get_unchecked(encoded_offset + 1),
                        encoded_line_records.get_unchecked(encoded_offset + 2),
                        encoded_line_records.get_unchecked(encoded_offset + 3),
                    )
                    encoded_record_index += 1
                    continue
            builder.append_line(raw)
        if encoded_record_index != encoded_record_count:
            raise EncodeError("structured instruction index exceeds text input")
        return builder.finish()
    finally:
        builder.close()


def assemble_native_text_entries(
    entries: CompilerIntArena,
    data_chunks: list[bytes],
    structured_relocations: CompilerIntArena,
    structured_symbol_names: list[str] | tuple[str, ...],
    labels: dict[str, int],
    data_in_code: list[DataInCodeRegion],
) -> AssembledText:
    """Consume packed word/data entries through the canonical finalizer.

    Ownership of the entry and relocation arenas transfers to this call.
    Kind 1 indexes a data blob; kind 2 carries a final instruction word.
    Symbol/label and inline-data metadata are module-owned, not per-instruction
    string slots. The producer/driver integration is a separate migration gate.
    """
    if len(entries) % 2 or len(structured_relocations) % 3:
        entries.close()
        structured_relocations.close()
        raise EncodeError("native text entry or relocation arena has invalid width")
    return _finalize_text_entries(
        entries, [], data_chunks, structured_relocations,
        structured_symbol_names, labels, data_in_code,
    )


def _finalize_text_entries(
    entries: CompilerIntArena,
    instruction_lines: list[str],
    data_chunks: list[bytes],
    structured_relocations: CompilerIntArena,
    structured_symbol_names: list[str] | tuple[str, ...],
    labels: dict[str, int],
    data_in_code: list[DataInCodeRegion],
) -> AssembledText:
    word_run = CompilerIntArena()
    try:
        return _encode_text_entries_active(
            entries, instruction_lines, data_chunks, structured_relocations,
            structured_symbol_names, labels, data_in_code, word_run,
        )
    finally:
        word_run.close()
        entries.close()
        structured_relocations.close()


def _encode_text_entries_active(
    entries: CompilerIntArena,
    instruction_lines: list[str],
    data_chunks: list[bytes],
    structured_relocations: CompilerIntArena,
    structured_symbol_names: list[str] | tuple[str, ...],
    labels: dict[str, int],
    data_in_code: list[DataInCodeRegion],
    word_run: CompilerIntArena,
) -> AssembledText:
    entry_instruction = 0
    entry_data = 1
    entry_word = 2
    # Chunks plus one join, never ``bytearray +=``: pcc's bytearray append/
    # extend allocate a replacement buffer (PY-P0-BYTEARRAY-INPLACE-IDENTITY-
    # MUTATION), so the per-instruction append was O(n^2) under pcc1 -- a
    # 472 KB __text cost 13.6 GiB and 66.8% of samples in _py_bytes_concat.
    chunks: list[bytes] = []
    code_len = 0
    relocations: list[Relocation] = []
    undefined: set[str] = set()

    def resolve_branch(target: str, width_bits: int, at: int) -> int:
        if target not in labels:
            raise EncodeError(f"branch to unknown label {target!r}")
        delta = labels[target] - at
        if delta % 4 != 0:
            raise EncodeError("misaligned branch target")
        words = delta >> 2
        limit = 1 << (width_bits - 1)
        if not -limit <= words < limit:
            raise EncodeError(f"branch to {target!r} out of range")
        return words & ((1 << width_bits) - 1)

    # Atom model (subsections-via-symbols): every non-L label starts an atom.
    # A branch whose target lives in the SAME atom as the branch site has a
    # fixed offset no matter how the linker reorders subsections, so as(1)
    # resolves it inline (the recursive-call case); any cross-atom call gets
    # a BRANCH26 relocation instead. Verified against as(1) on both shapes.
    atom_starts = sorted(
        off for name, off in labels.items() if not name.startswith("L")
    )

    def same_atom(target: str, at: int) -> bool:
        if target not in labels or target.startswith("L"):
            return False
        # A native call now reaches this canonical rule for every call site.
        # Locate its atom without a per-call scan of all preceding functions.
        low = 0
        high = len(atom_starts)
        while low < high:
            middle = (low + high) // 2
            if atom_starts[middle] <= at:
                low = middle + 1
            else:
                high = middle
        enclosing = atom_starts[low - 1] if low else 0
        return labels[target] == enclosing

    entry_index = 0
    structured_relocation_index = 0
    structured_relocation_count = len(structured_relocations) // 3
    while entry_index < len(entries) // 2:
        entry_offset = entry_index * 2
        kind = entries.get_unchecked(entry_offset)
        payload_index = entries.get_unchecked(entry_offset + 1)
        if kind == entry_data:
            if payload_index < 0 or payload_index >= len(data_chunks):
                raise EncodeError("text data index is outside its owner")
            if len(word_run):
                chunks.append(word_run.pack_u32_bytes())
                word_run.clear()
            payload = data_chunks[payload_index]
            chunks.append(payload)
            code_len += len(payload)
            entry_index += 1
            continue
        if kind == entry_word:
            if code_len % 4:
                raise EncodeError("instruction at unaligned __text offset after data")
            if payload_index < 0 or payload_index > 0xFFFFFFFF:
                raise EncodeError("text word is outside u32 range")
            if structured_relocation_index < structured_relocation_count:
                relocation_offset = structured_relocation_index * 3
                relocation_entry = structured_relocations.get_unchecked(
                    relocation_offset
                )
                if relocation_entry < entry_index:
                    raise EncodeError(
                        "structured relocation records are not ordered"
                    )
                if relocation_entry == entry_index:
                    relocation_kind = structured_relocations.get_unchecked(
                        relocation_offset + 1
                    )
                    symbol_id = structured_relocations.get_unchecked(
                        relocation_offset + 2
                    )
                    if symbol_id < 0 or symbol_id >= len(structured_symbol_names):
                        raise EncodeError("text relocation symbol is outside its owner")
                    symbol = structured_symbol_names[symbol_id]
                    if relocation_kind < 0:
                        _validate_text_fixup_word(payload_index, relocation_kind)
                    if relocation_kind in (
                        STRUCTURED_FIXUP_BRANCH26, STRUCTURED_FIXUP_BRANCH19,
                    ):
                        width = -relocation_kind
                        if width == 26 and not symbol.startswith("L") and not same_atom(symbol, code_len):
                            relocation_kind = STRUCTURED_RELOCATION_BRANCH26
                        else:
                            bits = resolve_branch(symbol, width, code_len)
                            payload_index |= bits if width == 26 else bits << 5
                            relocation_kind = STRUCTURED_RELOCATION_NONE
                    elif relocation_kind == STRUCTURED_FIXUP_CALL:
                        if (
                            symbol.startswith("L") and symbol in labels
                        ) or same_atom(symbol, code_len):
                            payload_index |= resolve_branch(symbol, 26, code_len)
                            relocation_kind = STRUCTURED_RELOCATION_NONE
                        else:
                            relocation_kind = STRUCTURED_RELOCATION_BRANCH26
                    if relocation_kind == STRUCTURED_RELOCATION_NONE:
                        native_relocation_kind = -1
                        relocation_pcrel = False
                    elif relocation_kind == STRUCTURED_RELOCATION_BRANCH26:
                        native_relocation_kind = spec.ARM64_RELOC_BRANCH26
                        relocation_pcrel = True
                    elif relocation_kind == STRUCTURED_RELOCATION_PAGE21:
                        native_relocation_kind = spec.ARM64_RELOC_PAGE21
                        relocation_pcrel = True
                    elif relocation_kind == STRUCTURED_RELOCATION_PAGEOFF12:
                        native_relocation_kind = spec.ARM64_RELOC_PAGEOFF12
                        relocation_pcrel = False
                    elif relocation_kind == STRUCTURED_RELOCATION_GOT_LOAD_PAGE21:
                        native_relocation_kind = spec.ARM64_RELOC_GOT_LOAD_PAGE21
                        relocation_pcrel = True
                    elif (
                        relocation_kind
                        == STRUCTURED_RELOCATION_GOT_LOAD_PAGEOFF12
                    ):
                        native_relocation_kind = (
                            spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12
                        )
                        relocation_pcrel = False
                    else:
                        raise EncodeError(
                            "structured relocation kind is not proven"
                        )
                    if native_relocation_kind >= 0:
                        undefined_or_local(symbol, labels, undefined)
                        relocations.append(Relocation(
                            code_len,
                            symbol,
                            native_relocation_kind,
                            pcrel=relocation_pcrel,
                        ))
                    structured_relocation_index += 1
            word_run.append(payload_index)
            code_len += 4
            entry_index += 1
            continue
        if (
            kind != entry_instruction
            or payload_index < 0
            or payload_index >= len(instruction_lines)
        ):
            raise EncodeError("text instruction index is outside its owner")
        if code_len % 4:
            raise EncodeError("instruction at unaligned __text offset after data")
        at = code_len
        word = _encode_one(
            instruction_lines[payload_index],
            at,
            labels,
            resolve_branch,
            relocations,
            undefined,
            same_atom,
        )
        word_run.append(word)
        code_len += 4
        entry_index += 1

    if structured_relocation_index != structured_relocation_count:
        raise EncodeError("structured relocation exceeds text entries")
    if len(word_run):
        chunks.append(word_run.pack_u32_bytes())
    return AssembledText(
        code=b"".join(chunks),
        relocations=relocations,
        undefined=sorted(undefined),
        labels=labels,
        data_in_code=data_in_code,
    )


def assemble_text(asm_text: str) -> AssembledText:
    """Compatibility string projection for the proven text dialect."""

    return assemble_text_lines(asm_text.splitlines())


def _encode_one(line, at, labels, resolve_branch, relocations, undefined,
                same_atom) -> int:
    parts = line.split(None, 1)
    mn = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    ops = _split_operands(rest)

    if mn == "ret":
        return 0xD65F03C0
    if mn == "nop":
        if ops:
            raise EncodeError(f"nop takes no operands: {line!r}")
        return encode_emitted_nop_parts()
    if mn == "paciasp":
        return 0xD503233F
    if mn == "autiasp":
        return 0xD50323BF

    if mn in ("add", "sub", "adds", "subs"):
        op_sub = 1 if mn.startswith("sub") else 0
        set_flags = 1 if mn in ("adds", "subs") else 0
        rd, d64 = _reg(ops[0])
        rn, n64 = _reg(ops[1])
        third = ops[2]
        if "@PAGEOFF" in third:
            if set_flags:
                raise EncodeError(f"{mn} with @PAGEOFF not proven")
            symbol = third.split("@")[0]
            undefined_or_local(symbol, labels, undefined)
            relocations.append(Relocation(
                at, symbol, spec.ARM64_RELOC_PAGEOFF12, pcrel=False))
            return _enc_addsub_imm(op_sub, rd, rn, 0, d64)
        if third.startswith("#"):
            return _enc_addsub_imm(op_sub, rd, rn, _imm(third), d64, set_flags)
        rm, _ = _reg(third)
        if _is_sp_token(ops[0]) or _is_sp_token(ops[1]):
            if len(ops) > 3:
                raise EncodeError(f"{mn} with SP does not take a shift: {line!r}")
            return _enc_addsub_ext(op_sub, rd, rn, rm, d64, set_flags)
        shift, shift_kind = _shifted_register_operand(
            ops, 3, line, allowed=("lsl", "lsr", "asr")
        )
        return _enc_addsub_reg(
            op_sub, rd, rn, rm, d64, set_flags, shift, shift_kind
        )

    if mn == "cmp":
        rn, n64 = _reg(ops[0])
        if ops[1].startswith("#"):
            return _enc_addsub_imm(1, 31, rn, _imm(ops[1]), n64, set_flags=1)
        rm, _ = _reg(ops[1])
        if _is_sp_token(ops[0]):
            return _enc_addsub_ext(1, 31, rn, rm, n64, set_flags=1)
        return _enc_addsub_reg(1, 31, rn, rm, n64, set_flags=1)

    if mn in ("and", "orr", "eor"):
        opc = {"and": 0, "orr": 1, "eor": 2}[mn]
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        if ops[2].startswith("#"):
            if mn != "and":
                raise EncodeError(f"{mn} immediate not in the proven subset")
            n_bit, immr, imms = _logical_imm(_imm(ops[2]), d64)
            return (
                _sf(d64) | 0x12000000 | (n_bit << 22)
                | (immr << 16) | (imms << 10) | (rn << 5) | rd
            )
        rm, _ = _reg(ops[2])
        shift, shift_kind = _shifted_register_operand(
            ops, 3, line, allowed=("lsl", "lsr", "asr", "ror")
        )
        return _enc_logical_reg(opc, rd, rn, rm, d64, shift, shift_kind)

    if mn == "mov":
        rd_tok = ops[0].strip().lower()
        rd, d64 = _reg(ops[0])
        rm_tok = ops[1].strip().lower()
        rm, m64 = _reg(ops[1])
        if d64 != m64:
            raise EncodeError(f"mov register widths differ: {line!r}")
        if rd_tok in ("sp", "wsp") or rm_tok in ("sp", "wsp"):
            # involves sp: alias of add #0
            return _enc_addsub_imm(0, rd, rm, 0, d64)
        return _enc_logical_reg(1, rd, 31, rm, d64)

    if mn in ("movz", "movk"):
        opc = 2 if mn == "movz" else 3
        rd, d64 = _reg(ops[0])
        imm16 = _imm(ops[1])
        shift = 0
        if len(ops) > 2:
            lsl = ops[2].split()
            if lsl[0].lower() != "lsl":
                raise EncodeError(f"bad move-wide shift {ops[2]!r}")
            shift = _imm(lsl[1])
        return _enc_movewide(opc, rd, imm16, shift, d64)

    if mn in ("asrv", "lslv", "lsrv"):
        op2 = {"lslv": 0b1000, "lsrv": 0b1001, "asrv": 0b1010}[mn]
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        rm, _ = _reg(ops[2])
        return (
            _sf(d64) | 0x1AC00000 | (rm << 16) | (op2 << 10) | (rn << 5) | rd
        )

    if mn == "adrp":
        rd, _ = _reg(ops[0])
        target = ops[1]
        if "@GOTPAGE" in target:
            symbol = target.split("@")[0]
            reloc_type = spec.ARM64_RELOC_GOT_LOAD_PAGE21
        elif "@PAGE" in target:
            symbol = target.split("@")[0]
            reloc_type = spec.ARM64_RELOC_PAGE21
        else:
            raise EncodeError(f"adrp needs @PAGE/@GOTPAGE: {line!r}")
        undefined_or_local(symbol, labels, undefined)
        relocations.append(Relocation(at, symbol, reloc_type, pcrel=True))
        return 0x90000000 | rd

    if (
        mn in ("ldur", "stur")
        and _reg_kind(ops[0]) in ("d", "s")
    ):
        rt, kind = _freg(ops[0])
        base, imm, mode = _mem(ops[1])
        if mode:
            raise EncodeError(f"{mn} with writeback not in the proven subset")
        size = 3 if kind == "d" else 2
        load = 1 if mn == "ldur" else 0
        # SIMD/FP unscaled load/store is the integer encoding with V=1.
        return _enc_ldst_unscaled(size, load, rt, base, imm) | (1 << 26)

    if mn in ("ldur", "stur", "ldurb", "sturb", "ldurh", "sturh"):
        rt, t64 = _reg(ops[0])
        base, imm, mode = _mem(ops[1])
        if mode:
            raise EncodeError(f"{mn} with writeback not in the proven subset")
        if mn.endswith("b"):
            size, opc = 0, (1 if mn == "ldurb" else 0)
        elif mn.endswith("h"):
            if t64:
                raise EncodeError("halfword memory instructions require a w register")
            size, opc = 1, (1 if mn == "ldurh" else 0)
        else:
            size, opc = (3 if t64 else 2), (1 if mn == "ldur" else 0)
        return _enc_ldst_unscaled(size, opc, rt, base, imm)

    if mn in ("ldar", "ldaxr", "stlr", "ldaxrb", "stlrb"):
        rt, t64 = _reg(ops[0])
        base, imm, mode = _mem(ops[1])
        if imm or mode:
            raise EncodeError(f"{mn} takes a bare [Xn] address: {line!r}")
        if mn.endswith("b"):
            # byte variants: size bits 00 instead of 10/11
            if t64:
                raise EncodeError(f"{mn} takes a W register: {line!r}")
            base_op = {"ldaxrb": 0x085FFC00, "stlrb": 0x089FFC00}[mn]
            return base_op | (base << 5) | rt
        sf_bit = 0x40000000 if t64 else 0
        if mn == "ldar":
            return 0x88DFFC00 | sf_bit | (base << 5) | rt
        if mn == "ldaxr":
            return 0x885FFC00 | sf_bit | (base << 5) | rt
        return 0x889FFC00 | sf_bit | (base << 5) | rt

    if mn in ("stlxr", "stlxrb"):
        rs, s64 = _reg(ops[0])
        if s64:
            raise EncodeError(f"{mn} status register must be a W register: {line!r}")
        rt, t64 = _reg(ops[1])
        base, imm, mode = _mem(ops[2])
        if imm or mode:
            raise EncodeError(f"{mn} takes a bare [Xn] address: {line!r}")
        if mn == "stlxrb":
            if t64:
                raise EncodeError(f"stlxrb takes a W data register: {line!r}")
            return 0x0800FC00 | (rs << 16) | (base << 5) | rt
        sf_bit = 0x40000000 if t64 else 0
        return 0x8800FC00 | sf_bit | (rs << 16) | (base << 5) | rt

    if mn == "clrex":
        if ops:
            raise EncodeError(f"clrex operands not proven: {line!r}")
        return 0xD5033F5F

    if mn == "dmb":
        if len(ops) != 1 or ops[0].strip().lower() != "ish":
            raise EncodeError(f"dmb domain not in the proven subset: {line!r}")
        return 0xD5033BBF

    if mn in ("ldr", "str") and _reg_kind(ops[0]) in ("d", "s"):
        rt, kind = _freg(ops[0])
        base, imm, mode = _mem(ops[1])
        if mode:
            raise EncodeError(f"{mn} with writeback not in the proven subset")
        size = 3 if kind == "d" else 2
        load = 1 if mn == "ldr" else 0
        # SIMD/FP unsigned-offset load/store is the integer encoding with V=1.
        return _enc_ldst_unsigned(size, load, rt, base, imm) | (1 << 26)

    if mn in ("ldr", "str", "ldrb", "strb", "ldrh", "strh"):
        rt, t64 = _reg(ops[0])
        memop = ops[1]
        if "@GOTPAGEOFF" in memop:
            inner = memop.strip()[1:-1]
            base_tok, sym_tok = [p.strip() for p in inner.split(",")]
            base, _ = _reg(base_tok)
            symbol = sym_tok.split("@")[0]
            undefined_or_local(symbol, labels, undefined)
            relocations.append(Relocation(
                at, symbol, spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12, pcrel=False))
            if mn != "ldr" or not t64:
                raise EncodeError("@GOTPAGEOFF only proven for 64-bit ldr")
            return _enc_ldst_unsigned(3, 1, rt, base, 0)
        base, imm, mode = _mem(memop)
        if mode:
            raise EncodeError(f"{mn} with writeback not in the proven subset")
        if mn.endswith("b"):
            size, opc = 0, (1 if mn == "ldrb" else 0)
        elif mn.endswith("h"):
            if t64:
                raise EncodeError("halfword memory instructions require a w register")
            size, opc = 1, (1 if mn == "ldrh" else 0)
        else:
            size, opc = (3 if t64 else 2), (1 if mn == "ldr" else 0)
        return _enc_ldst_unsigned(size, opc, rt, base, imm)

    if mn in ("ldp", "stp"):
        load = 1 if mn == "ldp" else 0
        rt, t64 = _reg(ops[0])
        rt2, _ = _reg(ops[1])
        memop = ops[2]
        post_imm = None
        if len(ops) > 3:  # post-index: [sp], #16
            post_imm = _imm(ops[3])
        base, imm, mode = _mem(memop)
        if post_imm is not None:
            if mode:
                raise EncodeError("both pre and post index")
            return _enc_ldstp(load, "post", rt, rt2, base, post_imm, t64)
        if mode == "pre":
            return _enc_ldstp(load, "pre", rt, rt2, base, imm, t64)
        return _enc_ldstp(load, "signed", rt, rt2, base, imm, t64)

    if mn == "b" or mn == "bl":
        target = ops[0]
        base_word = 0x14000000 if mn == "b" else 0x94000000
        # A transfer to another atom needs a relocation even when defined in
        # this input; the linker can reorder symbol subsections. Local labels
        # and self-recursion remain relative within their current atom.
        if (target.startswith("L") and target in labels) or same_atom(target, at):
            return base_word | resolve_branch(target, 26, at)
        if mn == "b" and target.startswith("L"):
            return base_word | resolve_branch(target, 26, at)
        if target not in labels:
            undefined.add(target)
        relocations.append(Relocation(
            at, target, spec.ARM64_RELOC_BRANCH26, pcrel=True))
        return base_word

    if mn.startswith("b.") and len(mn) == 4:
        return 0x54000000 | (resolve_branch(ops[0], 19, at) << 5) | _cond(mn[2:])

    if mn in ("cbz", "cbnz"):
        rt, t64 = _reg(ops[0])
        base = 0x35000000 if mn == "cbnz" else 0x34000000
        word = base | (resolve_branch(ops[1], 19, at) << 5) | rt
        if t64:
            word |= 0x80000000
        return word

    if mn == "csel":
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        rm, _ = _reg(ops[2])
        return (
            _sf(d64) | 0x1A800000 | (rm << 16)
            | (_cond(ops[3]) << 12) | (rn << 5) | rd
        )

    if mn == "cset":
        # alias of csinc rd, zr, zr, invert(cond)
        rd, d64 = _reg(ops[0])
        inv = _cond(ops[1]) ^ 1
        return (
            _sf(d64) | 0x1A800400 | (31 << 16) | (inv << 12) | (31 << 5) | rd
        )

    if mn in ("mul", "sdiv", "udiv", "smulh", "umulh"):
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        rm, _ = _reg(ops[2])
        if mn == "mul":  # alias of madd rd, rn, rm, zr
            return _sf(d64) | 0x1B007C00 | (rm << 16) | (rn << 5) | rd
        if mn in ("smulh", "umulh"):
            if not d64:
                raise EncodeError(f"{mn} is 64-bit only")
            u = 1 << 23 if mn == "umulh" else 0
            return 0x9B407C00 | u | (rm << 16) | (rn << 5) | rd
        return _sf(d64) | 0x1AC00800 | (rm << 16) | ((mn == "sdiv") << 10) | (rn << 5) | rd

    if mn in ("smull", "umull"):
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        rm, _ = _reg(ops[2])
        if not d64:
            raise EncodeError(f"{mn} destination must be 64-bit")
        u = 1 << 23 if mn == "umull" else 0
        return 0x9B207C00 | u | (rm << 16) | (rn << 5) | rd

    if mn in ("madd", "msub"):
        if len(ops) != 4:
            raise EncodeError(f"{mn} expects four same-width GPR operands")
        for operand in ops:
            if operand.strip().lower() == "sp":
                raise EncodeError(f"{mn} does not accept sp")
        rd, d64 = _reg(ops[0])
        rn, n64 = _reg(ops[1])
        rm, m64 = _reg(ops[2])
        ra, a64 = _reg(ops[3])
        if not (d64 == n64 == m64 == a64):
            raise EncodeError(f"{mn} expects four same-width GPR operands")
        if mn == "madd" and not d64:
            raise EncodeError("madd is proven only for the AArch64 i64 slice")
        subtract = 1 << 15 if mn == "msub" else 0
        return (
            _sf(d64)
            | 0x1B000000
            | subtract
            | (rm << 16)
            | (ra << 10)
            | (rn << 5)
            | rd
        )

    if mn == "neg":  # alias of sub rd, zr, rm
        rd, d64 = _reg(ops[0])
        rm, _ = _reg(ops[1])
        return _enc_addsub_reg(1, rd, 31, rm, d64)

    if mn in ("asr", "lsr", "lsl") and ops[2].startswith("#"):
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        sh = _imm(ops[2])
        width = 64 if d64 else 32
        if not 0 <= sh < width:
            raise EncodeError(f"{mn} shift {sh} out of range")
        n_bit = (1 << 22) if d64 else 0
        if mn == "asr":   # sbfm rd, rn, #sh, #width-1
            return _sf(d64) | 0x13000000 | n_bit | (sh << 16) | ((width - 1) << 10) | (rn << 5) | rd
        if mn == "lsr":   # ubfm rd, rn, #sh, #width-1
            return _sf(d64) | 0x53000000 | n_bit | (sh << 16) | ((width - 1) << 10) | (rn << 5) | rd
        # lsl: ubfm rd, rn, #(width-sh)%width, #(width-1-sh)
        immr = (width - sh) % width
        imms = width - 1 - sh
        return _sf(d64) | 0x53000000 | n_bit | (immr << 16) | (imms << 10) | (rn << 5) | rd

    if mn == "tst":  # alias of ands zr, rn, rm
        rn, n64 = _reg(ops[0])
        rm, _ = _reg(ops[1])
        return _enc_logical_reg(3, 31, rn, rm, n64)

    if mn in ("sxtb", "sxth", "sxtw"):
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        imms = {"sxtb": 7, "sxth": 15, "sxtw": 31}[mn]
        n_bit = (1 << 22) if d64 else 0
        return _sf(d64) | 0x13000000 | n_bit | (imms << 10) | (rn << 5) | rd

    if mn in ("clz", "rbit", "rev", "rev16"):
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        op2 = {"rbit": 0, "rev16": 1, "rev": 3 if d64 else 2, "clz": 4}[mn]
        return _sf(d64) | 0x5AC00000 | (op2 << 10) | (rn << 5) | rd

    if mn == "cneg":  # alias of csneg rd, rn, rn, invert(cond)
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        inv = _cond(ops[2]) ^ 1
        return _sf(d64) | 0x5A800400 | (rn << 16) | (inv << 12) | (rn << 5) | rd

    if mn == "csinv":
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        rm, _ = _reg(ops[2])
        return _sf(d64) | 0x5A800000 | (rm << 16) | (_cond(ops[3]) << 12) | (rn << 5) | rd

    if mn == "blr":
        rn, n64 = _reg(ops[0])
        if not n64:
            raise EncodeError("blr needs a 64-bit register")
        return 0xD63F0000 | (rn << 5)

    if mn == "brk":
        imm = _imm(ops[0])
        if not 0 <= imm <= 0xFFFF:
            raise EncodeError(f"brk immediate {imm} out of range")
        return 0xD4200000 | (imm << 5)

    # --- floating point (double, with fcvt/scvtf/fcvtzs domain crossings) ---

    # Scalar FP was encoded for double precision only, a deliberate scope
    # boundary that the pcc-C runtime build walked straight into (py_json.c's
    # float paths). Single precision is the same encoding with ftype 01 -> 00,
    # i.e. base 0x1E600000 -> 0x1E200000 and nothing else moved; verified
    # against the system assembler for fmul/fdiv/fadd/fsub, fneg/fabs/fsqrt,
    # frintn/p/m/z, fcmp and fcsel with Rd=9 Rn=10 Rm=11.
    if mn in ("fadd", "fsub", "fmul", "fdiv"):
        rd, kd = _freg(ops[0])
        rn, _ = _freg(ops[1])
        rm, _ = _freg(ops[2])
        if kd not in ("d", "s"):
            raise EncodeError(f"{mn} only proven for single/double precision")
        base = 0x1E600000 if kd == "d" else 0x1E200000
        opc = {"fmul": 0x0800, "fdiv": 0x1800, "fadd": 0x2800, "fsub": 0x3800}[mn]
        return base | opc | (rm << 16) | (rn << 5) | rd

    if mn in (
        "fneg", "fabs", "fsqrt",
        "frintn", "frintp", "frintm", "frintz",
    ):
        rd, kd = _freg(ops[0])
        rn, _ = _freg(ops[1])
        if kd not in ("d", "s"):
            raise EncodeError(f"{mn} only proven for single/double precision")
        # FP data-processing (1 source): opc packs opcode<<15 with the fixed
        # 10000 field at bits 14-10 (0x4000).  The frint* opcodes are 001000
        # (N, nearest-even), 001001 (P, +inf), 001010 (M, -inf) and 001011
        # (Z, toward zero) -- the four the aarch64 call lowering emits for
        # round/ceil/floor/trunc.
        opc = {
            "fabs": 0x0C000,
            "fneg": 0x14000,
            "fsqrt": 0x1C000,
            "frintn": 0x44000,
            "frintp": 0x4C000,
            "frintm": 0x54000,
            "frintz": 0x5C000,
        }[mn]
        return (0x1E600000 if kd == "d" else 0x1E200000) | opc | (rn << 5) | rd

    if mn == "fmov":
        if ops[1].strip().startswith("#"):
            rd, kd = _freg(ops[0])
            imm8 = _fp_imm8(ops[1])
            base = 0x1E601000 if kd == "d" else 0x1E201000
            return base | (imm8 << 13) | rd
        kd = _reg_kind(ops[0])
        ks = _reg_kind(ops[1])
        if kd == "d" and ks == "d":
            rd, _ = _freg(ops[0]); rn, _ = _freg(ops[1])
            return 0x1E604000 | (rn << 5) | rd
        # The single-precision mirrors of the three shapes below. Only the
        # double forms were encoded, so `fmov s9, s10` -- which the pcc-C
        # runtime build emits for py_capi_shim_oracle.c -- aborted with
        # "shape not proven". Bases verified against the system assembler:
        # fmov s9,s10 = 1e204149, fmov s9,w10 = 1e270149,
        # fmov w9,s10 = 1e260149, with (Rn<<5)|Rd = 0x149.
        if kd == "s" and ks == "s":
            rd, _ = _freg(ops[0]); rn, _ = _freg(ops[1])
            return 0x1E204000 | (rn << 5) | rd
        if kd == "s" and ks == "w":
            rd, _ = _freg(ops[0]); rn, _ = _reg(ops[1])
            return 0x1E270000 | (rn << 5) | rd
        if kd == "w" and ks == "s":
            rd, _ = _reg(ops[0]); rn, _ = _freg(ops[1])
            return 0x1E260000 | (rn << 5) | rd
        if kd == "d" and ks == "x":
            rd, _ = _freg(ops[0]); rn, _ = _reg(ops[1])
            return 0x9E670000 | (rn << 5) | rd
        if kd == "x" and ks == "d":
            rd, _ = _reg(ops[0]); rn, _ = _freg(ops[1])
            return 0x9E660000 | (rn << 5) | rd
        raise EncodeError(f"fmov shape {line!r} not proven")

    if mn == "fcvt":
        rd, kd = _freg(ops[0])
        rn, ks = _freg(ops[1])
        if kd == "d" and ks == "s":
            return 0x1E22C000 | (rn << 5) | rd
        if kd == "s" and ks == "d":
            return 0x1E624000 | (rn << 5) | rd
        raise EncodeError(f"fcvt shape {line!r} not proven")

    # Single-precision bases verified against the system assembler with
    # Rd=9 Rn=10 (low bits 0x149): scvtf s9,w10 = 1e220149,
    # scvtf s9,x10 = 9e220149, fcvtzs w9,s10 = 1e380149,
    # fcvtzs x9,s10 = 9e380149; the unsigned forms add 0x10000, and the
    # double bases below are the ones the assembler confirms unchanged.
    if mn in ("scvtf", "ucvtf"):
        rd, kd = _freg(ops[0])
        rn, n64 = _reg(ops[1])
        if kd not in ("d", "s"):
            raise EncodeError(f"{mn} only proven for single/double destinations")
        u = 0x10000 if mn == "ucvtf" else 0
        if kd == "d":
            base = 0x9E620000 if n64 else 0x1E620000
        else:
            base = 0x9E220000 if n64 else 0x1E220000
        return base | u | (rn << 5) | rd

    if mn in ("fcvtzs", "fcvtzu"):
        rd, d64 = _reg(ops[0])
        rn, ks = _freg(ops[1])
        if ks not in ("d", "s"):
            raise EncodeError(f"{mn} only proven from single/double sources")
        u = 0x10000 if mn == "fcvtzu" else 0
        if ks == "d":
            base = 0x9E780000 if d64 else 0x1E780000
        else:
            base = 0x9E380000 if d64 else 0x1E380000
        return base | u | (rn << 5) | rd

    if mn == "uxtw":  # zero-extend w -> x
        rd, d64 = _reg(ops[0])
        rn, _ = _reg(ops[1])
        if not d64:
            raise EncodeError("uxtw destination must be 64-bit")
        # ubfm xd, xn, #0, #31
        return 0xD3407C00 | (rn << 5) | rd

    if mn == "cnt":  # popcount lane: cnt vN.8b, vM.8b
        rd = _vreg8b(ops[0])
        rn = _vreg8b(ops[1])
        return 0x0E205800 | (rn << 5) | rd

    if mn == "addv":  # addv bN, vM.8b
        bd = ops[0].strip().rstrip(",").lower()
        if not (bd.startswith("b") and bd[1:].isdigit()):
            raise EncodeError(f"addv destination {ops[0]!r} not proven")
        rd = int(bd[1:])
        rn = _vreg8b(ops[1])
        return 0x0E31B800 | (rn << 5) | rd

    if mn == "umov":  # umov wN, vM.b[0]
        rd, d64 = _reg(ops[0])
        if d64:
            raise EncodeError("umov only proven for w destinations")
        src = ops[1].strip().lower()
        if not src.endswith(".b[0]"):
            raise EncodeError(f"umov source {ops[1]!r} not proven")
        rn = _vreg8b(src.split(".")[0] + ".8b")
        return 0x0E013C00 | (rn << 5) | rd

    if mn == "fcmp":
        rn, kn = _freg(ops[0])
        if kn not in ("d", "s"):
            raise EncodeError("fcmp only proven for single/double precision")
        base = 0x1E602000 if kn == "d" else 0x1E202000
        if ops[1].startswith("#"):
            if float(ops[1][1:]) != 0.0:
                raise EncodeError("fcmp immediate must be #0.0")
            return base | 0x8 | (rn << 5)
        rm, _ = _freg(ops[1])
        return base | (rm << 16) | (rn << 5)

    if mn == "fcsel":
        rd, kd = _freg(ops[0])
        rn, _ = _freg(ops[1])
        rm, _ = _freg(ops[2])
        if kd not in ("d", "s"):
            raise EncodeError("fcsel only proven for single/double precision")
        base = 0x1E600C00 if kd == "d" else 0x1E200C00
        return base | (rm << 16) | (_cond(ops[3]) << 12) | (rn << 5) | rd

    raise EncodeError(f"mnemonic {mn!r} not in the proven subset: {line!r}")


def undefined_or_local(symbol: str, labels: dict, undefined: set) -> None:
    if symbol not in labels:
        undefined.add(symbol)
