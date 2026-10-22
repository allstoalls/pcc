"""AArch64 ELF relocation arithmetic (AAELF64, static small code model).

The reference implementation is LLVM's lld/ELF/Arch/AArch64.cpp.  No LLVM
code or tool is used at runtime.  Instruction addends are explicit RELA
addends; preserving unrelated instruction bits is essential for register and
load-width encodings.
"""

import struct

R_AARCH64_ABS64 = 257
R_AARCH64_ABS32 = 258
R_AARCH64_PREL64 = 260
R_AARCH64_PREL32 = 261
R_AARCH64_ADR_PREL_LO21 = 274
R_AARCH64_ADR_PREL_PG_HI21 = 275
R_AARCH64_ADD_ABS_LO12_NC = 277
R_AARCH64_LDST8_ABS_LO12_NC = 278
R_AARCH64_JUMP26 = 282
R_AARCH64_CALL26 = 283
R_AARCH64_LDST16_ABS_LO12_NC = 284
R_AARCH64_LDST32_ABS_LO12_NC = 285
R_AARCH64_LDST64_ABS_LO12_NC = 286
R_AARCH64_LDST128_ABS_LO12_NC = 299
R_AARCH64_ADR_GOT_PAGE = 311
R_AARCH64_LD64_GOT_LO12_NC = 312
R_AARCH64_TLSIE_ADR_GOTTPREL_PAGE21 = 541
R_AARCH64_TLSIE_LD64_GOTTPREL_LO12_NC = 542
R_AARCH64_TLSLE_ADD_TPREL_HI12 = 549
R_AARCH64_TLSLE_ADD_TPREL_LO12_NC = 551

GOT_ADDRESS_RELOCATIONS = (311, 312)
GOT_TLS_RELOCATIONS = (541, 542)


def relocation_width(kind: int) -> int:
    if kind == 0:
        return 0
    if kind in (257, 260):
        return 8
    if kind in (258, 261, 274, 275, 277, 278, 282, 283, 284, 285,
                286, 299, 311, 312, 541, 542, 549, 551):
        return 4
    raise ValueError("unsupported AArch64 ELF relocation: " + str(kind))


def _signed(value: int, bits: int) -> int:
    if value < -(1 << (bits - 1)) or value >= (1 << (bits - 1)):
        raise ValueError(
            "AArch64 relocation out of range: " + str(value)
            + "; signed_bits=" + str(bits)
        )
    return value & ((1 << bits) - 1)


def _write_instruction_word(image: bytearray, offset: int, word: int) -> None:
    # The owned bytearray path needs one fixed-size copy, not generic format
    # packing and four element stores. Keep subclass/protocol handling and
    # invalid-value diagnostics in struct. Recheck the current bounds: earlier
    # arithmetic/read callbacks may have resized an otherwise exact bytearray.
    # Calling struct itself is not a hook contract for these exact primitives.
    if (type(image) is bytearray and type(offset) is int and type(word) is int
            and 0 <= offset <= len(image) - 4 and 0 <= word <= 0xFFFFFFFF):
        image[offset:offset + 4] = word.to_bytes(4, "little")
    else:
        struct.pack_into("<I", image, offset, word)


# Each guarded chunk is less than 1 MiB, so fallthrough can skip it with B.
# Chunking also keeps very large numeric layout plans within branch26 reach.
_VENEERS_PER_CHUNK = 65535
_VENEER_SIZE = 16
_VENEER_GUARD_SIZE = 8


def branch26_fits(target: int, place: int) -> bool:
    delta = target - place
    return not (delta & 3) and -(1 << 27) <= delta < (1 << 27)


def veneer_pool_end(start: int, count: int) -> int:
    """Plan reserved RX bytes without allocating a section-sized buffer."""
    if start < 0 or start & 7 or count < 0:
        raise ValueError("invalid AArch64 veneer pool layout")
    chunks = (count + _VENEERS_PER_CHUNK - 1) // _VENEERS_PER_CHUNK
    return start + count * _VENEER_SIZE + chunks * _VENEER_GUARD_SIZE


def veneer_slot_offset(start: int, index: int) -> int:
    """Return the exact slot, including every preceding chunk's guard."""
    if start < 0 or start & 7 or index < 0:
        raise ValueError("invalid AArch64 veneer slot layout")
    guards = 1 + index // _VENEERS_PER_CHUNK
    return start + index * _VENEER_SIZE + guards * _VENEER_GUARD_SIZE


def initialize_veneer_pool(image: bytearray, start: int, count: int) -> None:
    """Guard reserved slots, including unused slots, against fallthrough."""
    end = veneer_pool_end(start, count)
    if end > len(image):
        raise ValueError("AArch64 veneer pool outside image")
    cursor = start
    remaining = count
    while remaining:
        chunk_count = min(remaining, _VENEERS_PER_CHUNK)
        chunk_end = cursor + _VENEER_GUARD_SIZE + chunk_count * _VENEER_SIZE
        # B skips all literals and lands on the next guard or after the pool.
        immediate = _signed((chunk_end - cursor) >> 2, 26)
        struct.pack_into("<II", image, cursor, 0x14000000 | immediate, 0xD503201F)
        cursor = chunk_end
        remaining -= chunk_count


def apply_call26_with_veneer(image: bytearray, offset: int,
                             target: int, place: int,
                             veneer_offsets: tuple[int, ...]) -> None:
    """Relax only a proven STT_FUNC CALL26 into a pre-reserved boundary slot.

    The linker supplies the resolved symbol-type proof. AAELF64 5.7.7 and
    AAPCS64 6.5.1 allow IP0/x16 clobber here. BR, rather than BLR, preserves
    the original call's LR=P+4. No layout or allocation changes happen here.
    """
    delta = target - place
    if -(1 << 27) <= delta < (1 << 27):
        apply_relocation(image, offset, R_AARCH64_CALL26, target, place)
        return
    # Do not convert malformed original instructions or targets into success.
    if offset < 0 or offset + 4 > len(image):
        raise ValueError("AArch64 relocation outside image")
    if offset & 3:
        raise ValueError("unaligned AArch64 instruction relocation")
    if delta & 3 or target & 3 or place & 3:
        raise ValueError("unaligned AArch64 branch target")
    if target < 0 or target >= 1 << 64 or place < 0 or place >= 1 << 64:
        raise ValueError("AArch64 branch address outside unsigned 64-bit range")
    word = struct.unpack_from("<I", image, offset)[0]
    if word & 0xFC000000 != 0x94000000:
        raise ValueError("AArch64 CALL26 relocation requires a BL instruction")
    for veneer_offset in veneer_offsets:
        veneer_address = place - offset + veneer_offset
        # Test the actual slot address, not its pool's beginning.
        if not branch26_fits(veneer_address, place):
            continue
        if (veneer_offset < 0 or veneer_offset + _VENEER_SIZE > len(image)
                or veneer_offset & 7 or veneer_address & 7
                or veneer_address < 0 or veneer_address >= 1 << 64):
            raise ValueError("invalid AArch64 reserved veneer slot")
        apply_relocation(image, offset, R_AARCH64_CALL26, veneer_address, place)
        # LDR x16, [PC, #8]; BR x16; full-width absolute S+A literal.
        struct.pack_into("<IIQ", image, veneer_offset, 0x58000050, 0xD61F0200, target)
        return
    # Giant input sections are kept continuous. Fail only when this actual
    # overflowing call cannot reach either of its exact reserved slots.
    apply_relocation(image, offset, R_AARCH64_CALL26, target, place)


def apply_relocation(image: bytearray, offset: int, kind: int,
                     target: int, place: int) -> None:
    """Apply S+A (or GOT/TP offset+A) to a validated instruction/data site."""
    width = relocation_width(kind)
    if not width:
        return
    if offset < 0 or offset + width > len(image):
        raise ValueError("AArch64 relocation outside image")
    if kind in (257, 258, 260, 261):
        value = target - place if kind in (260, 261) else target
        if kind in (260, 261):
            value = _signed(value, width * 8)
        elif value < 0 or value >= 1 << (width * 8):
            raise ValueError("AArch64 absolute relocation overflow")
        image[offset:offset + width] = value.to_bytes(width, "little")
        return
    if offset & 3:
        raise ValueError("unaligned AArch64 instruction relocation")
    word = struct.unpack_from("<I", image, offset)[0]
    if kind in (282, 283):
        delta = target - place
        if delta & 3:
            raise ValueError("unaligned AArch64 branch target")
        immediate = _signed(delta >> 2, 26)
        word = (word & 0xFC000000) | immediate
    elif kind in (274, 275, 311, 541):
        delta = (target - place) if kind == 274 else (
            (target >> 12) - (place >> 12)
        )
        immediate = _signed(delta, 21)
        word = (word & ~0x60FFFFE0) | ((immediate & 3) << 29)
        word |= ((immediate >> 2) & 0x7FFFF) << 5
    elif kind in (277, 549, 551):
        if kind in (549, 551) and (target < 0 or target >= 1 << 24):
            raise ValueError("AArch64 local-exec TLS offset exceeds 24 bits")
        immediate = ((target >> 12) if kind == 549 else target) & 0xFFF
        word = (word & ~0x003FFC00) | (immediate << 10)
    else:
        if type(kind) is int:
            # relocation_width and the branches above leave only the seven
            # load/store kinds here. Avoid constructing a temporary dict for
            # each instruction; 286, 312 and 542 encode eight-byte accesses.
            if kind == 278:
                scale = 0
            elif kind == 284:
                scale = 1
            elif kind == 285:
                scale = 2
            elif kind == 299:
                scale = 4
            elif kind == 286 or kind == 312 or kind == 542:
                scale = 3
            else:
                raise KeyError(kind)
        else:
            # Preserve the original hash/equality behavior for custom kinds.
            scale = {278: 0, 284: 1, 285: 2, 286: 3, 299: 4,
                     312: 3, 542: 3}[kind]
        low = target & 0xFFF
        if low & ((1 << scale) - 1):
            raise ValueError("misaligned AArch64 load/store relocation")
        word = (word & ~0x003FFC00) | ((low >> scale) << 10)
    _write_instruction_word(image, offset, word)
