"""Mach-O object writer for the shapes pcc has differentially proven.

LINK-P1-MACHO-OBJ-MINIMAL / LINK-P1-MACHO-OBJ-RELOC / the section subset of
LINK-P1-MACHO-OBJ-FULL: pcc emits these bytes itself instead of going through
as(1). Scope grows one differentially verified slice at a time; everything
outside the proven subset fails closed.

The layout mirrors what as(1) produces for the same input so the two stay
field-level comparable:

    mach_header_64
    LC_SEGMENT_64  (one unnamed segment, N sections)
    LC_BUILD_VERSION (platform macos)
    LC_DATA_IN_CODE (only when an instruction section has inline data)
    LC_SYMTAB
    LC_DYSYMTAB
    section payloads (aligned)
    relocation entries (per section)
    data-in-code entries
    nlist_64 entries
    string table

Structures come from `pcc.backend.macho_spec`, which is pinned against the
SDK headers and round-tripped against clang output by
tests/python/test_macho_spec.py — this writer contains no byte-layout
knowledge of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import macho_spec as spec
from .precise_stackmap import (
    ARCH_AARCH64,
    PreciseStackMapError,
    decode_stack_map,
    function_address_offsets,
)


class MachOEmitError(Exception):
    """The requested shape is outside what this writer proves.

    Fail closed: an object writer that silently approximates produces a
    plausible-looking .o whose corruption surfaces later, in the linker or at
    runtime, far from the bug.
    """


@dataclass(frozen=True, slots=True)
class TextSymbol:
    """A defined symbol at `offset` into its section's payload.

    `external=False` puts it in the symtab's locals partition (a cstring
    label, a static) — still a valid relocation target: r_extern=1 in Mach-O
    means "symbolnum is a symtab index", not "the symbol is exported".
    """

    name: str
    offset: int
    external: bool = True
    private_external: bool = False


@dataclass(frozen=True, slots=True)
class Relocation:
    """One relocation at `offset` within its section's payload.

    `symbol` may name a defined or an undefined symbol; extern relocations
    reference the symbol-table index either way. `length` is log2 bytes —
    2 for arm64 instruction fixups, 3 for pointer-sized data. A non-zero
    `addend` emits an ARM64_RELOC_ADDEND companion entry immediately before
    this one, the way as(1) encodes `_sym+8@PAGE`; UNSIGNED data relocations
    instead embed their addend in the pointer bytes themselves and reject the
    field.

    SUBTRACTOR is represented atomically: `symbol` is the subtrahend and
    `minuend` names the target of the required following UNSIGNED entry.  The
    writer emits and validates the pair together so a malformed half-pair can
    never escape.

    A non-extern section relocation sets `section`, leaves `symbol` empty, and
    may use `target_offset` for the byte offset within that target section.
    With an explicit offset the relocated field must be zero-filled and the
    writer materialises the exact object-relative address after layout.  A
    `None` offset accepts a prefilled, range-checked section/object-relative
    value for relocatable-link consumers.
    """

    offset: int
    symbol: str
    type: int
    pcrel: bool
    length: int = 2
    addend: int = 0
    # When set, this is a SECTION-target relocation (r_extern=0): the target
    # is a (segname, sectname) pair, r_symbolnum is that section's 1-based
    # index, and the value being relocated lives in the payload itself.
    section: tuple[str, str] | None = None
    minuend: str | None = None
    target_offset: int | None = None


@dataclass(frozen=True)
class DataInCodeRegion:
    """One non-instruction byte range inside an instruction section.

    ``offset`` is section-relative.  ``emit_object`` translates it to the
    object-relative address stored in ``data_in_code_entry`` only after the
    final multi-section layout is known.
    """

    offset: int
    length: int
    kind: int = spec.DICE_KIND_DATA


TEXT_SECTION_FLAGS = (
    spec.S_REGULAR
    | spec.S_ATTR_PURE_INSTRUCTIONS
    | spec.S_ATTR_SOME_INSTRUCTIONS
)
DATA_SECTION_FLAGS = spec.S_REGULAR
CSTRING_SECTION_FLAGS = spec.S_CSTRING_LITERALS
ZEROFILL_SECTION_FLAGS = spec.S_ZEROFILL
MOD_INIT_SECTION_FLAGS = spec.S_MOD_INIT_FUNC_POINTERS
COMPACT_UNWIND_SECTION_FLAGS = spec.S_REGULAR | spec.S_ATTR_DEBUG
EH_FRAME_SECTION_FLAGS = (
    spec.S_COALESCED
    | spec.S_ATTR_NO_TOC
    | spec.S_ATTR_STRIP_STATIC_SYMS
    | spec.S_ATTR_LIVE_SUPPORT
)
PCC_STACKMAP_SECTION_FLAGS = spec.S_REGULAR

_DATA_IN_CODE_KINDS = {
    spec.DICE_KIND_DATA,
    spec.DICE_KIND_JUMP_TABLE8,
    spec.DICE_KIND_JUMP_TABLE16,
    spec.DICE_KIND_JUMP_TABLE32,
    spec.DICE_KIND_ABS_JUMP_TABLE32,
}
_DATA_IN_CODE_UNITS = {
    spec.DICE_KIND_DATA: 1,
    spec.DICE_KIND_JUMP_TABLE8: 1,
    spec.DICE_KIND_JUMP_TABLE16: 2,
    spec.DICE_KIND_JUMP_TABLE32: 4,
    spec.DICE_KIND_ABS_JUMP_TABLE32: 4,
}


@dataclass(frozen=True, slots=True)
class Section:
    """One section of the object: payload plus its symbols and relocations.

    A zerofill section (S_ZEROFILL) has no file payload: `data` stays empty
    and `zerofill_size` gives its vm size. It occupies address space only,
    with file offset 0 — the loader materializes the zeros.
    """

    sectname: str
    segname: str
    data: bytes = b""
    align_log2: int = 3
    flags: int = DATA_SECTION_FLAGS
    symbols: tuple[TextSymbol, ...] = ()
    relocations: tuple[Relocation, ...] = ()
    zerofill_size: int = 0
    data_in_code: tuple[DataInCodeRegion, ...] = ()

    @property
    def is_code(self) -> bool:
        return bool(self.flags & spec.S_ATTR_PURE_INSTRUCTIONS)

    @property
    def is_zerofill(self) -> bool:
        # S_THREAD_LOCAL_ZEROFILL (0x12) behaves identically here: vm space,
        # no file payload.
        return (self.flags & spec.SECTION_TYPE) in (spec.S_ZEROFILL, 0x12)

    @property
    def vm_size(self) -> int:
        return self.zerofill_size if self.is_zerofill else len(self.data)


# The relocation types this writer has differentially proven against as(1),
# with the pcrel/length shape each one requires and whether an ADDEND
# companion is proven for it (LINK-P1-MACHO-OBJ-RELOC: one type at a time;
# anything else fails closed). UNSIGNED embeds its addend in the data bytes,
# so the addend field is rejected for it.
_PROVEN_RELOCATION_SHAPES = {
    spec.ARM64_RELOC_BRANCH26: {"forms": ((True, 2),), "addend": False},
    spec.ARM64_RELOC_PAGE21: {"forms": ((True, 2),), "addend": True},
    spec.ARM64_RELOC_PAGEOFF12: {"forms": ((False, 2),), "addend": True},
    spec.ARM64_RELOC_GOT_LOAD_PAGE21: {
        "forms": ((True, 2),), "addend": False,
    },
    spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12: {
        "forms": ((False, 2),), "addend": False,
    },
    spec.ARM64_RELOC_UNSIGNED: {"forms": ((False, 3),), "addend": False},
    # `_sym@GOT - .` is the four-byte PC-relative form; `_sym@GOT` is the
    # eight-byte absolute form.  as(1) emits both and ld -r preserves both,
    # but Darwin's final ld64 currently rejects the absolute form.  Keeping
    # it here owns the finite relocatable-object encoding; final-link support
    # is a separate, explicitly tested boundary.
    spec.ARM64_RELOC_POINTER_TO_GOT: {
        "forms": ((True, 2), (False, 3)), "addend": False,
    },
    # Thread-local variable address pair, same instruction shapes as the GOT
    # pair (measured on cc output for `_Thread_local int`): adrp is pcrel,
    # the ldr/add half is not. This object writer owns the entries; support in
    # pcc's in-repo executable linker remains a separate TLV/dyld family.
    spec.ARM64_RELOC_TLVP_LOAD_PAGE21: {
        "forms": ((True, 2),), "addend": False,
    },
    spec.ARM64_RELOC_TLVP_LOAD_PAGEOFF12: {
        "forms": ((False, 2),), "addend": False,
    },
    # Label difference, always the first half of a pair at the same address
    # (SUBTRACTOR names the base, the following UNSIGNED names the target,
    # so the stored value becomes target - base). Measured on cc's
    # __eh_frame and label-difference output: extern, length 2 or 3,
    # non-pcrel.  It is emitted only through the atomic paired path below.
    spec.ARM64_RELOC_SUBTRACTOR: {
        "forms": ((False, 2), (False, 3)), "addend": False,
    },
}

# ADDEND stores its value in relocation_info's 24-bit symbolnum field.
_ADDEND_MAX = 0x7FFFFF


def _scan_stack_map_section(data: bytes) -> tuple[int, list[int]]:
    """Structurally scan a stack-map payload; return (count, address offsets).

    Mirrors the link's scan: validates magic/version/arch/counts/sizes and
    returns each function's address-field offset without materializing the
    per-location records.
    """
    from .precise_stackmap import (
        _scan_stack_map_payload,
    )

    count, scanned, _table_start, _table_count = _scan_stack_map_payload(
        bytes(data)
    )
    return count, [fn_start + 8 for _fid, fn_start, _fn_end in scanned]


def _pad16(name: str) -> bytes:
    if not name.isascii():
        raise MachOEmitError(f"segment/section name is not ASCII: {name!r}")
    raw = name.encode()
    if len(raw) > 16:
        raise MachOEmitError(f"segment/section name too long: {name!r}")
    return raw + b"\0" * (16 - len(raw))


def _align_up(value: int, align_log2: int) -> int:
    mask = (1 << align_log2) - 1
    return (value + mask) & ~mask


def _validate_section(sec: Section, *, relocations=None, relocation_count=None) -> None:
    # Internal indexed-object validation supplies a fresh row iterator and its
    # exact count. Ordinary object emission continues to use the stored tuple.
    if relocations is None:
        relocations = sec.relocations
    if relocation_count is None:
        relocation_count = len(sec.relocations)
    if (
        not isinstance(sec.align_log2, int)
        or isinstance(sec.align_log2, bool)
        or not 0 <= sec.align_log2 <= 31
    ):
        raise MachOEmitError(
            f"{sec.segname},{sec.sectname}: align_log2 must be 0..31"
        )
    _pad16(sec.segname)
    _pad16(sec.sectname)
    if sec.is_zerofill:
        if sec.data:
            raise MachOEmitError(
                f"zerofill section {sec.sectname} must not carry file payload"
            )
        if sec.zerofill_size <= 0:
            raise MachOEmitError(
                f"zerofill section {sec.sectname} needs a positive size"
            )
        if relocation_count:
            raise MachOEmitError(
                f"zerofill section {sec.sectname} cannot have relocations"
            )
        if sec.data_in_code:
            raise MachOEmitError(
                f"zerofill section {sec.sectname} cannot contain data-in-code"
            )
    else:
        if not sec.data:
            raise MachOEmitError(f"empty section {sec.segname},{sec.sectname}")
        if sec.zerofill_size:
            raise MachOEmitError(
                f"{sec.sectname}: zerofill_size on a non-zerofill section"
            )
    section_key = (sec.segname, sec.sectname)
    section_type = sec.flags & spec.SECTION_TYPE
    if sec.flags & spec.S_ATTR_DEBUG:
        if section_type != spec.S_REGULAR or sec.symbols:
            raise MachOEmitError(
                "S_ATTR_DEBUG sections must be regular and contain no symbols"
            )
    if section_type == spec.S_MOD_INIT_FUNC_POINTERS:
        if section_key != ("__DATA", "__mod_init_func"):
            raise MachOEmitError(
                "mod-init pointer section must be __DATA,__mod_init_func"
            )
        if sec.flags != MOD_INIT_SECTION_FLAGS:
            raise MachOEmitError(
                "__mod_init_func must use S_MOD_INIT_FUNC_POINTERS only"
            )
        if sec.align_log2 < 3 or len(sec.data) % 8 != 0:
            raise MachOEmitError(
                "__mod_init_func must be pointer-aligned whole 64-bit slots"
            )
        pointer_relocs = {
            r.offset for r in relocations
            if r.type == spec.ARM64_RELOC_UNSIGNED
            and not r.pcrel and r.length == 3
        }
        expected = set(range(0, len(sec.data), 8))
        if pointer_relocs != expected or relocation_count != len(expected):
            raise MachOEmitError(
                "every __mod_init_func slot needs exactly one 64-bit "
                "UNSIGNED relocation"
            )
    elif section_key == ("__DATA", "__mod_init_func"):
        raise MachOEmitError(
            "__DATA,__mod_init_func needs S_MOD_INIT_FUNC_POINTERS"
        )

    if section_key == ("__LD", "__compact_unwind"):
        if sec.flags != COMPACT_UNWIND_SECTION_FLAGS:
            raise MachOEmitError(
                "__compact_unwind must be S_REGULAR|S_ATTR_DEBUG"
            )
        if sec.align_log2 < 3 or len(sec.data) % 32 != 0:
            raise MachOEmitError(
                "arm64 __compact_unwind must contain aligned 32-byte rows"
            )
        function_relocs = {
            r.offset for r in relocations
            if r.type == spec.ARM64_RELOC_UNSIGNED
            and not r.pcrel and r.length == 3
        }
        expected = set(range(0, len(sec.data), 32))
        if not expected.issubset(function_relocs):
            raise MachOEmitError(
                "every __compact_unwind row needs a function-address "
                "UNSIGNED relocation"
            )
    elif section_key == ("__TEXT", "__eh_frame"):
        if sec.flags != EH_FRAME_SECTION_FLAGS:
            raise MachOEmitError(
                "__eh_frame has the wrong coalesced/live-support flags"
            )
        if sec.align_log2 < 3:
            raise MachOEmitError("__eh_frame must be at least 8-byte aligned")

    if section_key == ("__DATA", "__pcc_stackmaps"):
        if sec.flags != PCC_STACKMAP_SECTION_FLAGS:
            raise MachOEmitError("__pcc_stackmaps must be a regular section")
        if sec.align_log2 < 3:
            raise MachOEmitError("__pcc_stackmaps must be 8-byte aligned")
        if sec.symbols:
            raise MachOEmitError("__pcc_stackmaps cannot define data symbols")
        try:
            # Structural scan (magic/counts/bounds/trailing) instead of a full
            # decode: a cold self-host link carries tens of millions of managed
            # locations, and this validator runs once per object construction
            # (assemble worker + merge), so materializing them here dominated
            # link time.  The complete semantic decode+validation still guards
            # the merged table at the final executable boundary.
            _scanned, address_offsets = _scan_stack_map_section(sec.data)
        except PreciseStackMapError as exc:
            raise MachOEmitError(f"invalid __pcc_stackmaps payload: {exc}") from exc
        relocation_by_offset = {relocation.offset: relocation for relocation in relocations}
        if len(relocation_by_offset) != relocation_count:
            raise MachOEmitError("duplicate __pcc_stackmaps relocation offset")
        if set(relocation_by_offset) != set(address_offsets):
            raise MachOEmitError(
                "every stack-map function address needs exactly one relocation"
            )
        for address_offset in address_offsets:
            relocation = relocation_by_offset[address_offset]
            if (
                relocation.type != spec.ARM64_RELOC_UNSIGNED
                or relocation.pcrel
                or relocation.length != 3
                or relocation.section is not None
                or relocation.minuend is not None
                or relocation.addend
            ):
                raise MachOEmitError(
                    "stack-map function address needs a plain 64-bit "
                    "UNSIGNED symbol relocation"
                )
            if sec.data[address_offset:address_offset + 8] != b"\0" * 8:
                raise MachOEmitError(
                    "relocatable stack-map function address must be zero"
                )
    elif sec.sectname == "__pcc_stackmaps":
        raise MachOEmitError("__pcc_stackmaps must live in __DATA")

    last_region_end = 0
    for region in sec.data_in_code:
        for field_name, value in (
            ("offset", region.offset),
            ("length", region.length),
            ("kind", region.kind),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise MachOEmitError(
                    f"data-in-code {field_name} must be an integer"
                )
        if not sec.is_code:
            raise MachOEmitError(
                "data-in-code regions are only valid in instruction sections"
            )
        if region.kind not in _DATA_IN_CODE_KINDS:
            raise MachOEmitError(
                f"unrecognised data-in-code kind {region.kind}"
            )
        if not 0 < region.length <= 0xFFFF:
            raise MachOEmitError(
                f"data-in-code length {region.length} outside uint16 range"
            )
        unit = _DATA_IN_CODE_UNITS[region.kind]
        if region.offset % unit != 0 or region.length % unit != 0:
            raise MachOEmitError(
                f"data-in-code kind {region.kind} needs {unit}-byte units"
            )
        if region.offset < last_region_end:
            raise MachOEmitError(
                "data-in-code regions must be ordered and non-overlapping"
            )
        instruction_gap = region.offset - last_region_end
        if instruction_gap and (
            last_region_end % 4 != 0 or instruction_gap % 4 != 0
        ):
            raise MachOEmitError(
                "bytes outside data-in-code regions must be whole aligned "
                "arm64 instructions"
            )
        if not 0 <= region.offset <= len(sec.data) - region.length:
            raise MachOEmitError(
                f"data-in-code range at {region.offset} outside {sec.sectname}"
            )
        last_region_end = region.offset + region.length
    if sec.is_code:
        trailing_instructions = len(sec.data) - last_region_end
        if trailing_instructions and (
            last_region_end % 4 != 0 or trailing_instructions % 4 != 0
        ):
            raise MachOEmitError(
                "bytes outside data-in-code regions must be whole aligned "
                "arm64 instructions"
            )
    for sym in sec.symbols:
        if not sym.name or not sym.name.isascii():
            raise MachOEmitError(f"bad symbol name {sym.name!r}")
        if sym.private_external and not sym.external:
            raise MachOEmitError(
                f"private-extern symbol {sym.name!r} must also be external"
            )
        if not 0 <= sym.offset < sec.vm_size:
            raise MachOEmitError(
                f"symbol {sym.name!r} offset {sym.offset} outside "
                f"{sec.sectname}"
            )
        symbol_is_inline_data = any(
            region.offset <= sym.offset < region.offset + region.length
            for region in sec.data_in_code
        )
        if sec.is_code and sym.offset % 4 != 0 and not symbol_is_inline_data:
            raise MachOEmitError(
                f"symbol {sym.name!r} offset {sym.offset} is not "
                f"instruction-aligned"
            )


def _validate_relocation(
    sec: Section,
    r: Relocation,
    known: dict[str, int],
    section_by_name: dict[tuple[str, str], Section],
) -> None:
    for field_name, value in (
        ("offset", r.offset),
        ("type", r.type),
        ("length", r.length),
        ("addend", r.addend),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise MachOEmitError(
                f"relocation {field_name} must be an integer, got {value!r}"
            )
    if (
        r.target_offset is not None
        and (
            not isinstance(r.target_offset, int)
            or isinstance(r.target_offset, bool)
        )
    ):
        raise MachOEmitError(
            "relocation target_offset must be an integer or None, got "
            f"{r.target_offset!r}"
        )
    if not isinstance(r.pcrel, bool):
        raise MachOEmitError(
            f"relocation pcrel must be bool, got {r.pcrel!r}"
        )
    if not isinstance(r.symbol, str):
        raise MachOEmitError(
            f"relocation symbol must be a string, got {r.symbol!r}"
        )
    if r.minuend is not None and not isinstance(r.minuend, str):
        raise MachOEmitError(
            f"SUBTRACTOR minuend must be a string, got {r.minuend!r}"
        )
    if r.section is not None:
        if (
            not isinstance(r.section, tuple)
            or len(r.section) != 2
            or not all(
                isinstance(name, str) and bool(name) and name.isascii()
                for name in r.section
            )
        ):
            raise MachOEmitError(
                f"bad section-target name {r.section!r}"
            )

    shape = _PROVEN_RELOCATION_SHAPES.get(r.type)
    if shape is None:
        raise MachOEmitError(
            f"relocation type {r.type} not differentially proven yet"
        )
    if (r.pcrel, r.length) not in shape["forms"]:
        required = ", ".join(
            f"pcrel={pcrel} length={length}"
            for pcrel, length in shape["forms"]
        )
        raise MachOEmitError(
            f"relocation at {r.offset}: type {r.type} requires "
            f"one of ({required}), "
            f"got pcrel={r.pcrel} length={r.length}"
        )
    if r.addend:
        if not shape["addend"]:
            raise MachOEmitError(
                f"relocation at {r.offset}: addend not proven for type {r.type}"
            )
        if not 0 < r.addend <= _ADDEND_MAX:
            raise MachOEmitError(
                f"relocation at {r.offset}: addend {r.addend} outside the "
                f"proven 24-bit positive range"
            )
    if r.type == spec.ARM64_RELOC_SUBTRACTOR:
        if r.section is not None:
            raise MachOEmitError("SUBTRACTOR must use extern symbol targets")
        if r.minuend is None:
            raise MachOEmitError(
                "SUBTRACTOR requires a following UNSIGNED minuend target"
            )
        if r.symbol == r.minuend:
            raise MachOEmitError(
                "SUBTRACTOR minuend and subtrahend must be distinct"
            )
        if r.symbol not in known:
            raise MachOEmitError(
                f"SUBTRACTOR targets unknown subtrahend {r.symbol!r}"
            )
        if r.minuend not in known:
            raise MachOEmitError(
                f"SUBTRACTOR targets unknown minuend {r.minuend!r}"
            )
    elif r.minuend is not None:
        raise MachOEmitError(
            "minuend is only valid on an ARM64_RELOC_SUBTRACTOR pair"
        )

    if r.section is not None:
        if r.type != spec.ARM64_RELOC_UNSIGNED:
            raise MachOEmitError(
                f"section-target relocation type {r.type} not proven "
                "(only UNSIGNED is)"
            )
        if r.symbol:
            raise MachOEmitError(
                "section-target relocation must leave symbol empty"
            )
        if r.section not in section_by_name:
            raise MachOEmitError(
                f"relocation targets section {r.section} which the output "
                "does not contain"
            )
        target_sec = section_by_name[r.section]
        if (
            r.target_offset is not None
            and not 0 <= r.target_offset < target_sec.vm_size
        ):
            raise MachOEmitError(
                f"section-target offset {r.target_offset} outside "
                f"{target_sec.segname},{target_sec.sectname}"
            )
        target_type = target_sec.flags & spec.SECTION_TYPE
        if target_type == spec.S_CSTRING_LITERALS or (
            target_sec.segname == "__DATA"
            and target_sec.sectname in ("__cfstring", "__objc_classrefs")
        ):
            raise MachOEmitError(
                "section-target relocation to linker-special section "
                f"{target_sec.segname},{target_sec.sectname} is not proven"
            )
    else:
        if r.target_offset is not None:
            raise MachOEmitError(
                "target_offset is only valid on a section-target relocation"
            )
        if r.symbol not in known:
            raise MachOEmitError(
                f"relocation targets unknown symbol {r.symbol!r}"
            )

    width = 1 << r.length
    if not 0 <= r.offset <= len(sec.data) - width:
        raise MachOEmitError(
            f"relocation offset {r.offset} outside {sec.sectname}"
        )
    if r.offset > 0x7FFFFFFF:
        raise MachOEmitError(
            f"relocation offset {r.offset} exceeds signed r_address range"
        )
    # Instruction fixups must land on an instruction; data relocations need
    # not be naturally aligned (DWARF in __eh_frame relocates at unaligned
    # offsets, and ld accepts it).
    relocation_is_inline_data = any(
        region.offset <= r.offset
        and r.offset + width <= region.offset + region.length
        for region in sec.data_in_code
    )
    if sec.is_code and r.offset % 4 != 0 and not relocation_is_inline_data:
        raise MachOEmitError(
            f"relocation offset {r.offset} is not instruction-aligned in "
            f"{sec.sectname}"
        )


def emit_object(
    sections: list[Section],
    *,
    undefined: list[str] = (),
    minos: tuple[int, int] = (12, 0),
) -> bytes:
    """Emit an arm64 MH_OBJECT with the given sections."""
    if not sections:
        raise MachOEmitError("at least one section is required")
    if len({(s.segname, s.sectname) for s in sections}) != len(sections):
        raise MachOEmitError("duplicate section names")

    seen: set[str] = set()
    defined: list[tuple[TextSymbol, int]] = []  # (symbol, 1-based section idx)
    # Zerofill must be last within ITS OWN segment; a later segment's
    # content sections are unaffected (the object here has __DATA,__bss
    # before __LD,__compact_unwind, which ld accepts).
    seen_zerofill_segments: set[str] = set()
    for index, sec in enumerate(sections, start=1):
        _validate_section(sec)
        if sec.is_zerofill:
            seen_zerofill_segments.add(sec.segname)
        elif sec.segname in seen_zerofill_segments:
            # ld requires zerofill at the end of the segment; refusing the
            # order here beats emitting an object the linker rejects later.
            raise MachOEmitError(
                f"content section {sec.sectname} after a zerofill section "
                "(sort zerofill last within its segment before emitting)"
            )
        for sym in sec.symbols:
            if sym.name in seen:
                raise MachOEmitError(f"duplicate symbol {sym.name!r}")
            seen.add(sym.name)
            defined.append((sym, index))
    if not defined:
        raise MachOEmitError("at least one defined symbol is required")
    for name in undefined:
        if not name or not name.isascii():
            raise MachOEmitError(f"bad undefined symbol name {name!r}")
        if name in seen:
            raise MachOEmitError(f"symbol {name!r} both defined and undefined")
        seen.add(name)

    # Section addresses: one unnamed segment, vmaddr accumulates across
    # sections with each section's alignment (matches as(1)); zerofill
    # sections occupy address space without file payload.
    addrs: list[int] = []
    vm = 0
    for sec in sections:
        vm = _align_up(vm, sec.align_log2)
        addrs.append(vm)
        vm += sec.vm_size
    vmsize = vm

    data_in_code_entries: list[tuple[int, DataInCodeRegion]] = []
    for section_index, sec in enumerate(sections):
        for region in sec.data_in_code:
            object_offset = addrs[section_index] + region.offset
            if object_offset > 0xFFFFFFFF:
                raise MachOEmitError(
                    f"data-in-code offset {object_offset} exceeds uint32"
                )
            data_in_code_entries.append((object_offset, region))

    # Symbol-table order per Mach-O: locals, externally defined, undefined —
    # locals/extdef in (section, offset) order, undefined sorted by name to
    # match as(1).
    defined.sort(key=lambda pair: (pair[1], pair[0].offset))
    local_defined = [pair for pair in defined if not pair[0].external]
    extern_defined = [pair for pair in defined if pair[0].external]
    defined = local_defined + extern_defined
    undef_ordered = sorted(undefined)
    sym_index = {pair[0].name: i for i, pair in enumerate(defined)}
    sym_index.update(
        (name, len(defined) + i) for i, name in enumerate(undef_ordered)
    )

    section_by_name = {
        (sec.segname, sec.sectname): sec for sec in sections
    }
    for sec in sections:
        relocation_addresses: set[int] = set()
        for r in sec.relocations:
            _validate_relocation(sec, r, sym_index, section_by_name)
            if r.offset in relocation_addresses:
                raise MachOEmitError(
                    f"multiple relocation requests at offset {r.offset} in "
                    f"{sec.segname},{sec.sectname}; use an atomic companion "
                    "shape instead"
                )
            relocation_addresses.add(r.offset)

    section_number = {
        (sec.segname, sec.sectname): i for i, sec in enumerate(sections, start=1)
    }

    # A local r_extern=0 UNSIGNED relocation stores the current
    # object-relative target address in the field and names only the target
    # section in r_symbolnum.  Own that materialisation here, after layout is
    # known, rather than requiring callers to duplicate section-address math.
    payloads = [bytearray(sec.data) for sec in sections]
    for source_index, sec in enumerate(sections):
        for r in sec.relocations:
            if r.section is None:
                continue
            target_index = section_number[r.section] - 1
            width = 1 << r.length
            field = payloads[source_index][r.offset:r.offset + width]
            stored_value = int.from_bytes(field, "little")
            target_addr = addrs[target_index]
            target_size = sections[target_index].vm_size
            if r.target_offset is not None:
                if stored_value:
                    raise MachOEmitError(
                        f"section-target relocation field at {r.offset} in "
                        f"{sec.segname},{sec.sectname} must be zero-filled"
                    )
                target_value = target_addr + r.target_offset
            elif target_addr <= stored_value < target_addr + target_size:
                # Already an object-relative target address.
                target_value = stored_value
            elif 0 <= stored_value < target_size:
                # Relocatable-link consumers naturally carry a target-section
                # offset before this writer owns the final section layout.
                target_value = target_addr + stored_value
            else:
                raise MachOEmitError(
                    f"prefilled section-target value {stored_value} outside "
                    f"{sections[target_index].segname},"
                    f"{sections[target_index].sectname}"
                )
            if target_value >= 1 << (width * 8):
                raise MachOEmitError(
                    f"section-target value {target_value} does not fit "
                    f"in {width} bytes"
                )
            encoded_target = target_value.to_bytes(width, "little")
            encoded_index = 0
            while encoded_index < width:
                payloads[source_index][r.offset + encoded_index] = encoded_target[
                    encoded_index
                ]
                encoded_index += 1

    seg_size = spec.SEGMENT_COMMAND_64.size + len(sections) * spec.SECTION_64.size
    sizeofcmds = (
        seg_size
        + spec.BUILD_VERSION_COMMAND.size
        + (spec.LINKEDIT_DATA_COMMAND.size if data_in_code_entries else 0)
        + spec.SYMTAB_COMMAND.size
        + spec.DYSYMTAB_COMMAND.size
    )

    # File layout: payloads (aligned), relocation entries, symbols, strings.
    # Zerofill sections take no file space and report offset 0.
    payload_offsets: list[int] = []
    cursor = spec.MACH_HEADER_64.size + sizeofcmds
    for sec in sections:
        if sec.is_zerofill:
            payload_offsets.append(0)
            continue
        cursor = _align_up(cursor, sec.align_log2)
        payload_offsets.append(cursor)
        cursor += len(sec.data)

    # LLVM's Mach object writer pads the complete section-data area to pointer
    # alignment before relocation/linkedit tables.  This matters when an
    # inline data region leaves __text at a non-eight-byte length.
    cursor = _align_up(cursor, 3)
    section_data_end = cursor

    reloc_offsets: list[int] = []
    reloc_counts: list[int] = []
    for sec in sections:
        entries = len(sec.relocations) + sum(
            1 for r in sec.relocations if r.addend
        ) + sum(
            1 for r in sec.relocations
            if r.type == spec.ARM64_RELOC_SUBTRACTOR
        )
        reloc_offsets.append(cursor if entries else 0)
        reloc_counts.append(entries)
        cursor += entries * spec.RELOCATION_INFO.size

    data_in_code_off = cursor if data_in_code_entries else 0
    cursor += len(data_in_code_entries) * spec.DATA_IN_CODE_ENTRY.size

    nsyms = len(defined) + len(undef_ordered)
    sym_off = cursor
    str_off = sym_off + nsyms * spec.NLIST_64.size

    # String table: index 0 is traditionally a NUL so no real name gets n_strx 0.
    strtab = bytearray(b"\0")
    strx: dict[str, int] = {}
    for name in [pair[0].name for pair in defined] + undef_ordered:
        strx[name] = len(strtab)
        strtab += name.encode() + b"\0"
    while len(strtab) % 8 != 0:
        strtab += b"\0"

    header = spec.MACH_HEADER_64.pack({
        "magic": spec.MH_MAGIC_64,
        "cputype": spec.CPU_TYPE_ARM64,
        "cpusubtype": spec.CPU_SUBTYPE_ARM64_ALL,
        "filetype": spec.MH_OBJECT,
        "ncmds": 4 + (1 if data_in_code_entries else 0),
        "sizeofcmds": sizeofcmds,
        "flags": spec.MH_SUBSECTIONS_VIA_SYMBOLS,
        "reserved": 0,
    })

    content_spans = [
        (payload_offsets[i], payload_offsets[i] + len(sec.data))
        for i, sec in enumerate(sections)
        if not sec.is_zerofill
    ]
    segment = spec.SEGMENT_COMMAND_64.pack({
        "cmd": spec.LC_SEGMENT_64,
        "cmdsize": seg_size,
        "segname": _pad16(""),  # objects use one unnamed segment
        "vmaddr": 0,
        "vmsize": vmsize,
        "fileoff": content_spans[0][0],
        "filesize": content_spans[-1][1] - content_spans[0][0],
        "maxprot": spec.VM_PROT_READ | spec.VM_PROT_WRITE | spec.VM_PROT_EXECUTE,
        "initprot": spec.VM_PROT_READ | spec.VM_PROT_WRITE | spec.VM_PROT_EXECUTE,
        "nsects": len(sections),
        "flags": 0,
    })

    section_blobs = b"".join(
        spec.SECTION_64.pack({
            "sectname": _pad16(sec.sectname),
            "segname": _pad16(sec.segname),
            "addr": addrs[i],
            "size": sec.vm_size,
            "offset": payload_offsets[i],
            "align": sec.align_log2,
            "reloff": reloc_offsets[i],
            "nreloc": reloc_counts[i],
            "flags": sec.flags,
            "reserved1": 0,
            "reserved2": 0,
            "reserved3": 0,
        })
        for i, sec in enumerate(sections)
    )

    build_version = spec.BUILD_VERSION_COMMAND.pack({
        "cmd": spec.LC_BUILD_VERSION,
        "cmdsize": spec.BUILD_VERSION_COMMAND.size,
        "platform": spec.PLATFORM_MACOS,
        "minos": (minos[0] << 16) | (minos[1] << 8),
        "sdk": 0,
        "ntools": 0,
    })

    data_in_code_command = b""
    if data_in_code_entries:
        data_in_code_command = spec.LINKEDIT_DATA_COMMAND.pack({
            "cmd": spec.LC_DATA_IN_CODE,
            "cmdsize": spec.LINKEDIT_DATA_COMMAND.size,
            "dataoff": data_in_code_off,
            "datasize": len(data_in_code_entries)
            * spec.DATA_IN_CODE_ENTRY.size,
        })

    symtab = spec.SYMTAB_COMMAND.pack({
        "cmd": spec.LC_SYMTAB,
        "cmdsize": spec.SYMTAB_COMMAND.size,
        "symoff": sym_off,
        "nsyms": nsyms,
        "stroff": str_off,
        "strsize": len(strtab),
    })

    dysymtab = spec.DYSYMTAB_COMMAND.pack({
        "cmd": spec.LC_DYSYMTAB,
        "cmdsize": spec.DYSYMTAB_COMMAND.size,
        "ilocalsym": 0,
        "nlocalsym": len(local_defined),
        "iextdefsym": len(local_defined),
        "nextdefsym": len(extern_defined),
        "iundefsym": len(defined),
        "nundefsym": len(undef_ordered),
        "tocoff": 0, "ntoc": 0,
        "modtaboff": 0, "nmodtab": 0,
        "extrefsymoff": 0, "nextrefsyms": 0,
        "indirectsymoff": 0, "nindirectsyms": 0,
        "extreloff": 0, "nextrel": 0,
        "locreloff": 0, "nlocrel": 0,
    })

    # as(1) writes relocation entries in descending r_address order, with an
    # ADDEND companion immediately before the entry it modifies; match both
    # so the differential diff can compare the tables entry by entry.
    def _reloc_blob(sec: Section) -> bytes:
        parts: list[bytes] = []
        for r in sorted(sec.relocations, key=lambda r: r.offset, reverse=True):
            if r.addend:
                parts.append(spec.RELOCATION_INFO.pack({
                    "r_address": r.offset,
                    "r_info": spec.pack_relocation(
                        r_symbolnum=r.addend,
                        r_pcrel=0,
                        r_length=r.length,
                        r_extern=0,
                        r_type=spec.ARM64_RELOC_ADDEND,
                    ),
                }))
            if r.type == spec.ARM64_RELOC_SUBTRACTOR:
                # Mach-O requires this exact adjacent order at one address:
                # SUBTRACTOR(subtrahend), then UNSIGNED(minuend).  Both are
                # extern and carry the same width/non-pcrel shape.
                parts.append(spec.RELOCATION_INFO.pack({
                    "r_address": r.offset,
                    "r_info": spec.pack_relocation(
                        r_symbolnum=sym_index[r.symbol],
                        r_pcrel=0,
                        r_length=r.length,
                        r_extern=1,
                        r_type=spec.ARM64_RELOC_SUBTRACTOR,
                    ),
                }))
                parts.append(spec.RELOCATION_INFO.pack({
                    "r_address": r.offset,
                    "r_info": spec.pack_relocation(
                        r_symbolnum=sym_index[r.minuend],
                        r_pcrel=0,
                        r_length=r.length,
                        r_extern=1,
                        r_type=spec.ARM64_RELOC_UNSIGNED,
                    ),
                }))
                continue
            if r.section is not None:
                parts.append(spec.RELOCATION_INFO.pack({
                    "r_address": r.offset,
                    "r_info": spec.pack_relocation(
                        r_symbolnum=section_number[r.section],
                        r_pcrel=1 if r.pcrel else 0,
                        r_length=r.length,
                        r_extern=0,
                        r_type=r.type,
                    ),
                }))
                continue
            parts.append(spec.RELOCATION_INFO.pack({
                "r_address": r.offset,
                "r_info": spec.pack_relocation(
                    r_symbolnum=sym_index[r.symbol],
                    r_pcrel=1 if r.pcrel else 0,
                    r_length=r.length,
                    r_extern=1,
                    r_type=r.type,
                ),
            }))
        return b"".join(parts)

    nlists = b"".join(
        spec.NLIST_64.pack({
            "n_strx": strx[sym.name],
            "n_type": (
                spec.N_SECT
                | (spec.N_EXT if sym.external else 0)
                | (spec.N_PEXT if sym.private_external else 0)
            ),
            "n_sect": section_index,
            "n_desc": 0,
            "n_value": addrs[section_index - 1] + sym.offset,
        })
        for sym, section_index in defined
    ) + b"".join(
        spec.NLIST_64.pack({
            "n_strx": strx[name],
            "n_type": spec.N_UNDF | spec.N_EXT,
            "n_sect": spec.NO_SECT,
            "n_desc": 0,
            "n_value": 0,
        })
        for name in undef_ordered
    )

    data_in_code_blob = b"".join(
        spec.DATA_IN_CODE_ENTRY.pack({
            "offset": offset,
            "length": region.length,
            "kind": region.kind,
        })
        for offset, region in data_in_code_entries
    )

    blob = bytearray()
    blob += (
        header + segment + section_blobs + build_version
        + data_in_code_command + symtab + dysymtab
    )
    for i, sec in enumerate(sections):
        while len(blob) < payload_offsets[i]:
            blob += b"\0"
        blob += payloads[i]
    while len(blob) < section_data_end:
        blob += b"\0"
    for sec in sections:
        blob += _reloc_blob(sec)
    blob += data_in_code_blob
    assert len(blob) == sym_off
    blob += nlists + bytes(strtab)
    return bytes(blob)


def emit_text_object(
    text: bytes,
    symbols: list[TextSymbol],
    *,
    undefined: list[str] = (),
    relocations: list[Relocation] = (),
    minos: tuple[int, int] = (12, 0),
    align_log2: int = 2,
) -> bytes:
    """Emit an arm64 MH_OBJECT with one __text section (the original subset)."""
    return emit_object(
        [Section(
            sectname="__text",
            segname="__TEXT",
            data=text,
            align_log2=align_log2,
            flags=TEXT_SECTION_FLAGS,
            symbols=tuple(symbols),
            relocations=tuple(relocations),
        )],
        undefined=undefined,
        minos=minos,
    )
