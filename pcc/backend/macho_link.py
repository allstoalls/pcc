"""Static-link core: merge pcc's objects, resolve symbols, fix relocations.

LINK-P1-MACHO-LINK-STATIC, first slice. The row's engine is symbol
resolution + section merging + layout + relocation application; the
*executable container* (dyld surface, stubs, GOT, entry point) is
LINK-P1-MACHO-LINK-DYLD. This module implements the engine and proves it in
the shape where it can be proven on its own: relocatable linking, the job
`ld -r` does.

What it does, per `ld -r` semantics verified against the real thing:

- sections merge by (segname, sectname), each input's payload appended at its
  own alignment; segment ordering follows the same __TEXT-before-__DATA rule
  the assembler driver uses, since it decides every symbol's address
- defined symbols keep their identity and get their merged address; a symbol
  undefined in one input and defined in another is *resolved* — it leaves the
  undefined list and the relocation now targets the defined symbol
- relocations keep their type and extern bit, shift `r_address` by the
  input's offset within the merged section, and re-point at the merged symbol
  table; ARM64_RELOC_ADDEND companions are folded on read and re-emitted on
  write, so the addend travels with the relocation it modifies
- LC_DATA_IN_CODE ranges move with their owning instruction section instead
  of disappearing or retaining stale pre-merge addresses

Fail closed: a duplicate definition, an unsupported section, or a relocation
against a symbol no input defines or declares raises `LinkError`. A linker
that guesses produces a binary that crashes far from the mistake.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import macho_spec as spec
from .macho_obj import (
    DataInCodeRegion,
    MachOEmitError,
    Relocation,
    Section,
    TextSymbol,
    new_relocation,
)
from .macho_parallel import (
    ParallelLinkError,
    ShardedSymbolDefinitions,
    SymbolDefinition,
    ordered_parallel_map,
)
from .native_object import (
    NativeObject,
    NativeObjectError,
    OwnedMergedSourceView,
    NativeSection,
    NativeSymbol,
    NativeObjectView,
    PackedNativeObject,
    PackedNativeSection,
    PackedNativeSymbol,
    is_native_object_bytes,
    _decode_index,
    _packed_relocation_fields_at,
    _packed_relocation_sort_indices,
    _packed_section_target_relocations,
)
from .precise_stackmap import (
    ARCH_AARCH64,
    RUNTIME_TABLE_VERSION,
    PreciseStackMapError,
    _scan_stack_map_payload,
    function_address_offsets,
    merge_stack_map_payloads,
    precise_stackmap_runtime_format,
    transcode_runtime_table,
)


_U64_MASK = (1 << 64) - 1  # computed, NOT a literal: see M5-SELFHOST-BIG-INT-LITERAL


class LinkError(Exception):
    """The link job is outside what this linker proves."""


# A real PEP 604 union.  This was once a *string*, to dodge a self-host gap:
# pcc1 could not evaluate `|` between class objects, so the module-level alias
# made the owned linker unimportable.  Both halves of that gap are now
# implemented -- the runtime evaluates the union, and `type_infer` records a
# module-level union alias instead of leaving a phantom `ClassType(alias)`.
# The string form was actively harmful: `isinstance(value, bytes)` inside
# `_coerce_link_object` compiled against that phantom class and answered False
# for real bytes, so a self-hosted link rejected its own archive members.
LinkInput = (
    bytes | NativeObject | NativeObjectView | PackedNativeObject
    | spec.MachOObject
)


def _coerce_link_object(value: LinkInput, input_index: int):
    """Normalize one explicitly typed input exactly once.

    Raw bytes are the external Mach-O boundary.  The pcc-native codec is a
    cache/process transport, not another platform object encoding, so callers
    must decode it to ``NativeObject`` before entering this API.  Keeping that
    distinction explicit prevents a mislabeled ``--object`` input from being
    accepted merely because its bytes happen to carry pcc's private magic.
    """
    try:
        if isinstance(value, NativeObjectView):
            return value
        if isinstance(value, PackedNativeObject):
            return value
        if isinstance(value, NativeObject):
            # Keep pcc's already-validated indexed representation intact.
            # Expanding it to NativeObjectView eagerly copied every payload
            # and materialised Mach-O-shaped dicts for every symbol and raw
            # relocation before the linker converted them back to indexed
            # NativeObject records at the output boundary.
            return value
        if isinstance(value, spec.MachOObject):
            return value
        if not isinstance(value, bytes):
            raise LinkError(
                f"input {input_index} is neither Mach-O nor a pcc-native object"
            )
        if is_native_object_bytes(value):
            raise LinkError(
                f"input {input_index} contains encoded pcc-native bytes; "
                "decode it explicitly before linking"
            )
        return spec.parse_object(value)
    except (NativeObjectError, spec.MachOFormatError) as exc:
        raise LinkError(f"input {input_index} is not a valid object: {exc}") from exc


@dataclass
class _MergedSection:
    segname: str
    sectname: str
    flags: int
    align_log2: int = 0
    data: bytes | bytearray = field(default_factory=bytearray)
    data_parts: list[bytes | bytearray] = field(default_factory=list)
    data_size: int = 0
    symbols: list[TextSymbol] = field(default_factory=list)
    relocations: list[Relocation] = field(default_factory=list)
    data_in_code: list[DataInCodeRegion] = field(default_factory=list)
    zerofill_size: int = 0
    # The leading ``proven_relocations`` records came from packed inputs whose
    # decode already validated every record against the input section, and
    # the merge kept each record in range and on its instruction grid.  The
    # prefix closes at the first input that is not such a source.
    proven_relocations: int = 0
    proven_prefix_open: bool = True
    proven_includes_data: bool = False


@dataclass(frozen=True)
class _InspectedLinkInput:
    obj: object
    symbols: object
    relocation_target_indices: frozenset[int]
    section_addrs: tuple[int, ...] = ()


# Each accessor tests the concrete classes one at a time.  A tuple isinstance
# narrows to a union, and pcc1 cannot pick a field offset for a union: every
# ``symbol.section_index`` became a dynamic getattr -- 9% of a Stage2 link
# (_symbol_type alone 2%).  Packed inputs come first: they are what the owned
# link consumes.


def _symbol_is_weak_reference(symbol) -> bool:
    if isinstance(symbol, PackedNativeSymbol):
        return symbol.weak_reference
    if isinstance(symbol, NativeSymbol):
        return symbol.weak_reference
    return bool(symbol["n_desc"] & spec.N_WEAK_REF)


def _symbol_name(symbol) -> str:
    if isinstance(symbol, PackedNativeSymbol):
        return symbol.name
    if isinstance(symbol, NativeSymbol):
        return symbol.name
    return symbol["name"]


def _native_symbol_type(section_index: int, external: bool, private_external: bool) -> int:
    if section_index == 0:
        return spec.N_UNDF | spec.N_EXT
    value = spec.N_SECT
    if external:
        value |= spec.N_EXT
    if private_external:
        value |= spec.N_PEXT
    return value


def _symbol_type(symbol) -> int:
    if isinstance(symbol, PackedNativeSymbol):
        return _native_symbol_type(
            symbol.section_index, symbol.external, symbol.private_external
        )
    if isinstance(symbol, NativeSymbol):
        return _native_symbol_type(
            symbol.section_index, symbol.external, symbol.private_external
        )
    return symbol["n_type"]


def _symbol_section_index(symbol) -> int:
    if isinstance(symbol, PackedNativeSymbol):
        return symbol.section_index
    if isinstance(symbol, NativeSymbol):
        return symbol.section_index
    return symbol["n_sect"]


def _section_key(section) -> tuple[str, str]:
    if isinstance(section, PackedNativeSection):
        return section.segname, section.sectname
    if isinstance(section, NativeSection):
        return section.segname, section.sectname
    return section["segname_str"], section["sectname_str"]


def _section_flags(section) -> int:
    if isinstance(section, PackedNativeSection):
        return section.flags
    if isinstance(section, NativeSection):
        return section.flags
    return section["flags"]


def _section_align(section) -> int:
    if isinstance(section, PackedNativeSection):
        return section.align_log2
    if isinstance(section, NativeSection):
        return section.align_log2
    return section["align"]


def _section_size(section) -> int:
    if isinstance(section, PackedNativeSection):
        return section.vm_size
    if isinstance(section, NativeSection):
        return section.vm_size
    return section["size"]


def _native_section_addrs(
    native: NativeObject | PackedNativeObject,
) -> tuple[int, ...]:
    addrs: list[int] = []
    cursor = 0
    for section in native.sections:
        cursor = _align_up(cursor, section.align_log2)
        addrs.append(cursor)
        cursor += section.vm_size
    return tuple(addrs)


def _section_addr(section, section_index: int, addrs: tuple[int, ...]) -> int:
    if isinstance(section, (NativeSection, PackedNativeSection)):
        return addrs[section_index - 1]
    return section["addr"]


def _normalized_section_target_value(
    stored, target_offset, target_addr, target_size, width, target_section_index,
):
    """Shared absolute-first interpretation of a native section target."""
    if target_offset is not None:
        if stored:
            raise NativeObjectError(
                "section-target relocation with target_offset must have "
                "a zero-filled field"
            )
        target_value = target_addr + target_offset
    elif target_addr <= stored < target_addr + target_size:
        target_value = stored
    elif 0 <= stored < target_size:
        target_value = target_addr + stored
    else:
        raise NativeObjectError(
            f"section-target value {stored} is outside section "
            f"{target_section_index}"
        )
    if target_value >= 1 << (width * 8):
        raise NativeObjectError("section-target value does not fit relocation width")
    return target_value


def _native_section_payload(
    native: NativeObject | PackedNativeObject,
    section_index: int,
    addrs: tuple[int, ...],
) -> bytes | bytearray:
    """Return one payload, patching only section-target fields that need it.

    NativeObjectView used to copy *all* input payloads up front.  A section
    target needs the same object-address normalization, but ordinary sections
    can be consumed directly from their immutable bytes.
    """

    section = native.sections[section_index - 1]
    if isinstance(native, PackedNativeObject):
        section_targets = _packed_section_target_relocations(native, section_index - 1)
        source_payload = native.section_data(section_index - 1)
    else:
        section_targets = tuple(
            relocation
            for relocation in section.relocations
            if relocation.target_section_index is not None
        )
        source_payload = section.data
    if not section_targets:
        return source_payload
    payload = bytearray(source_payload)
    for relocation in section_targets:
        if isinstance(native, PackedNativeObject):
            (
                start, _symbol_index, _type, _pcrel, length, _addend,
                target_section_index, _minuend_index, target_offset,
            ) = relocation
        else:
            start = relocation.offset
            length = relocation.length
            target_section_index = relocation.target_section_index
            target_offset = relocation.target_offset
        width = 1 << length
        stored = int.from_bytes(payload[start:start + width], "little")
        assert target_section_index is not None
        target_index = target_section_index - 1
        target_addr = addrs[target_index]
        target_size = native.sections[target_index].vm_size
        target_value = _normalized_section_target_value(
            stored, target_offset, target_addr, target_size, width, target_section_index,
        )
        # pcc's Layer 1 frontend does not lower ``bytearray`` slice assignment
        # yet, and this module must compile under pcc so an installed native
        # pcc1 can link without a host Python. Store the fixed-width field one
        # byte at a time instead.
        field = target_value.to_bytes(width, "little")
        byte_index = 0
        while byte_index < width:
            payload[start + byte_index] = field[byte_index]
            byte_index += 1
    return payload


def _section_payload(
    obj,
    section,
    section_index: int,
    section_addrs: tuple[int, ...],
):
    if isinstance(obj, (NativeObject, PackedNativeObject)):
        return _native_section_payload(obj, section_index, section_addrs)
    return obj.data[section["offset"]:section["offset"] + section["size"]]


def _is_zerofill(section) -> bool:
    return (_section_flags(section) & spec.SECTION_TYPE) in (
        spec.S_ZEROFILL, _S_THREAD_LOCAL_ZEROFILL
    )


# Thread-local zerofill has its own section type; it behaves the same way
# here (vm space, no file payload).
_S_THREAD_LOCAL_ZEROFILL = 0x12

# Merged __TEXT,__text is split into sections of at most this many bytes, at
# input boundaries. One input's code is whole functions, so a boundary never
# falls inside fallthrough code. The executable linker can place a branch
# thunk island only between output sections, and AArch64 b/bl reach +-128
# MiB, so a single merged __text larger than that left call sites in its
# middle out of reach of every island. Keep this below the executable
# linker's island span.
_TEXT_KEY = ("__TEXT", "__text")


def _is_text_chunk_key(key: tuple[str, str]) -> bool:
    """__TEXT,__text and the numbered chunks it is split into."""
    return key[0] == "__TEXT" and key[1].startswith("__text")
_TEXT_CHUNK_BYTES = 32 * 1024 * 1024


def _align_up(value: int, align_log2: int) -> int:
    mask = (1 << align_log2) - 1
    return (value + mask) & ~mask


def _place_merged_section(
    merged: dict[tuple[str, str], _MergedSection],
    order: list[tuple[str, str]],
    key: tuple[str, str],
    flags: int,
    align_log2: int,
    size: int,
    text_chunk_index: int,
    text_chunk_key: tuple[str, str],
) -> tuple[tuple[str, str], _MergedSection, int, int, int, tuple[str, str]]:
    """Place one input section using metadata, without retaining its payload.

    The text-chunk decision precedes alignment, exactly as in the merge.
    Zerofill commits its VM size here. For ordinary data, the caller commits
    ``data_size`` after reading the actual payload; a failed or short read
    must not be replaced by a declared-size advance.
    """
    zerofill = (flags & spec.SECTION_TYPE) in (
        spec.S_ZEROFILL, _S_THREAD_LOCAL_ZEROFILL,
    )
    merged_key = key
    if key == _TEXT_KEY and not zerofill:
        current = merged.get(text_chunk_key)
        if (
            current is not None
            and current.data_size > 0
            and current.data_size + size > _TEXT_CHUNK_BYTES
        ):
            text_chunk_index += 1
            text_chunk_key = ("__TEXT", "__text" + str(text_chunk_index))
        merged_key = text_chunk_key
    if merged_key not in merged:
        merged[merged_key] = _MergedSection(merged_key[0], merged_key[1], flags)
        order.append(merged_key)
    target = merged[merged_key]
    if (target.flags & spec.SECTION_TYPE) != (flags & spec.SECTION_TYPE):
        raise LinkError(
            f"{key} has conflicting section TYPES across inputs "
            f"({target.flags:#x} vs {flags:#x})"
        )
    # Attributes are a union, not an identity: an input whose __text
    # happens to contain no branch targets omits SOME_INSTRUCTIONS.
    target.flags |= flags & spec.SECTION_ATTRIBUTES
    target.align_log2 = max(target.align_log2, align_log2)
    padding = 0
    if zerofill:
        base = _align_up(target.zerofill_size, align_log2)
        target.zerofill_size = base + size
    else:
        base = _align_up(target.data_size, align_log2)
        padding = base - target.data_size
        if padding and target.data_size % 4 != 0 and (
            target.flags & spec.S_ATTR_PURE_INSTRUCTIONS
        ):
            # An odd-sized inline-data tail needs data padding, not a
            # partial AArch64 instruction, before the next text atom.
            target.data_in_code.append(DataInCodeRegion(
                offset=target.data_size, length=padding, kind=spec.DICE_KIND_DATA,
            ))
    return merged_key, target, base, padding, text_chunk_index, text_chunk_key


def _relocation_symbol_name(
    symbols,
    symbol_index: int,
    local_rename: dict[str, str],
    *,
    context: str,
) -> str:
    if not 0 <= symbol_index < len(symbols):
        raise LinkError(
            f"{context} names symbol index {symbol_index}, but the input has "
            f"{len(symbols)} symbols"
        )
    symbol = symbols[symbol_index]
    name = _symbol_name(symbol)
    if (
        (_symbol_type(symbol) & spec.N_TYPE) == spec.N_SECT
        and not (_symbol_type(symbol) & spec.N_EXT)
    ):
        return local_rename.get(name, name)
    return name


def _local_symbol_renames(symbol_tables) -> list[dict[str, str]]:
    """Give colliding locals output-only names without changing externs.

    Mach-O relocations carry a symbol-table index, so a local symbol and an
    external symbol may legitimately have the same spelling.  ``emit_object``
    intentionally uses a name-keyed API; rename only the local side before
    entering that API.  Reserve every external spelling up front so the
    result is independent of input order.
    """
    external_names = {
        _symbol_name(symbol)
        for symbols in symbol_tables
        for symbol in symbols
        if _symbol_type(symbol) & spec.N_EXT
    }
    reserved_names = {
        _symbol_name(symbol)
        for symbols in symbol_tables
        for symbol in symbols
    }
    used_names = set(external_names)
    renames: list[dict[str, str]] = []

    for input_index, symbols in enumerate(symbol_tables):
        mapping: dict[str, str] = {}
        local_names: set[str] = set()
        for symbol in symbols:
            if (_symbol_type(symbol) & spec.N_TYPE) != spec.N_SECT:
                continue
            if _symbol_type(symbol) & spec.N_EXT:
                continue
            name = _symbol_name(symbol)
            if name in local_names:
                raise LinkError(
                    f"input {input_index} defines local symbol {name!r} twice"
                )
            local_names.add(name)
            if name not in used_names:
                used_names.add(name)
                continue

            suffix = f"$link{input_index}"
            candidate = name + suffix
            collision = 1
            while candidate in reserved_names or candidate in used_names:
                candidate = name + suffix + f"${collision}"
                collision += 1
            mapping[name] = candidate
            used_names.add(candidate)
            reserved_names.add(candidate)
        renames.append(mapping)
    return renames


def _read_relocations(
    obj,
    section,
    symbols,
    local_rename: dict[str, str],
    offset_bias: int = 0,
    symbol_names=None,
    object_sections=None,
) -> list[Relocation]:
    """Relocations with ADDEND/SUBTRACTOR companions folded atomically.

    A non-extern entry targets a SECTION: `r_symbolnum` is that section's
    1-based index and the value being relocated is stored in the payload as
    an address in this object's own address space. It is carried through as a
    (segname, sectname) target so the merge can rewrite that stored address
    once the section's new home is known. Apply the destination section's
    offset while constructing the record, avoiding a second immutable copy.
    """
    if isinstance(obj, PackedNativeObject):
        out: list[Relocation] = []
        section_index = obj.sections.index(section)
        count = section.relocation_count
        if symbol_names is None:
            symbol_names = [
                _relocation_symbol_name(symbols, index, local_rename, context="packed native relocation")
                for index in range(len(symbols))
            ]
        order = section.relocation_order
        sorted_indices = []
        if order == 0:
            sorted_indices = _packed_relocation_sort_indices(obj, section)
        for ordinal in range(count):
            storage_index = ordinal
            if order > 0:
                storage_index = count - 1 - ordinal
            elif order == 0:
                storage_index = sorted_indices[ordinal]
            (
                offset,
                symbol_raw,
                relocation_type,
                pcrel_raw,
                length,
                addend,
                target_section_raw,
                minuend_raw,
                target_offset_raw,
            ) = _packed_relocation_fields_at(obj, section, storage_index)
            symbol_index = _decode_index(symbol_raw)
            target_section_index = _decode_index(target_section_raw)
            minuend_index = _decode_index(minuend_raw)
            pcrel = bool(pcrel_raw)
            target_offset = None if target_offset_raw == -1 else target_offset_raw
            target_section = None
            symbol_name = ""
            if target_section_index is not None:
                target_section = _section_key(
                    obj.sections[target_section_index - 1]
                )
            else:
                assert symbol_index is not None
                if 0 <= symbol_index < len(symbol_names):
                    symbol_name = symbol_names[symbol_index]
                else:
                    symbol_name = _relocation_symbol_name(
                        symbols, symbol_index, local_rename, context="packed native relocation",
                    )
            minuend = None
            if minuend_index is not None:
                if 0 <= minuend_index < len(symbol_names):
                    minuend = symbol_names[minuend_index]
                else:
                    minuend = _relocation_symbol_name(
                        symbols, minuend_index, local_rename, context="packed native SUBTRACTOR minuend relocation",
                    )
            out.append(new_relocation(
                offset + offset_bias,
                symbol_name,
                relocation_type,
                pcrel,
                length,
                addend,
                target_section,
                minuend,
                target_offset,
            ))
        return out

    if isinstance(obj, NativeObject):
        out: list[Relocation] = []
        for entry in sorted(
            section.relocations,
            key=lambda relocation: relocation.offset,
            reverse=True,
        ):
            target_section = None
            symbol_name = ""
            if entry.target_section_index is not None:
                target_section = _section_key(
                    obj.sections[entry.target_section_index - 1]
                )
            else:
                assert entry.symbol_index is not None
                symbol_name = _relocation_symbol_name(
                    symbols,
                    entry.symbol_index,
                    local_rename,
                    context="native relocation",
                )
            minuend = None
            if entry.minuend_index is not None:
                minuend = _relocation_symbol_name(
                    symbols,
                    entry.minuend_index,
                    local_rename,
                    context="native SUBTRACTOR minuend relocation",
                )
            out.append(Relocation(
                offset=entry.offset + offset_bias,
                symbol=symbol_name,
                type=entry.type,
                pcrel=entry.pcrel,
                length=entry.length,
                addend=entry.addend,
                section=target_section,
                minuend=minuend,
                target_offset=entry.target_offset,
            ))
        return out

    if object_sections is None:
        object_sections = obj.sections()
    return _folded_relocation_rows(
        obj.relocations(section), object_sections, symbols, local_rename,
        offset_bias=offset_bias, symbol_names=symbol_names,
    )


def _iter_folded_relocation_rows(
    raw, sections, symbols, local_rename, *, offset_bias=0, symbol_names=None,
):
    """Fold the existing raw companion contract for one section's rows."""
    for relocation in _folded_relocation_rows(
        raw, sections, symbols, local_rename,
        offset_bias=offset_bias, symbol_names=symbol_names,
    ):
        yield relocation


def _folded_relocation_rows(
    raw, sections, symbols, local_rename, *, offset_bias=0, symbol_names=None,
) -> list:
    """Fold one section's raw rows and their ADDEND/SUBTRACTOR companions.

    ``symbol_names`` optionally holds every symbol's resolved relocation name
    (same ``local_rename``); in-range extern rows read it instead of resolving
    the symbol again. A list, not a generator, with relocation constants read
    once and records built through ``new_relocation``: under pcc1 the resumed
    generator, the keyword dataclass calls and the per-row ``spec`` lookups
    were most of a prior-gate link's relocation reads.
    """
    reloc_unsigned = spec.ARM64_RELOC_UNSIGNED
    reloc_addend = spec.ARM64_RELOC_ADDEND
    reloc_subtractor = spec.ARM64_RELOC_SUBTRACTOR
    out: list = []
    pending_addend = None
    pending_subtractor = None
    for entry in raw:
        if pending_subtractor is not None:
            if (
                entry["r_type"] != reloc_unsigned
                or not entry["r_extern"]
                or entry["r_address"] != pending_subtractor["r_address"]
                or entry["r_pcrel"]
                or entry["r_length"] != pending_subtractor["r_length"]
            ):
                raise LinkError(
                    "SUBTRACTOR must be followed by a same-address, "
                    "same-width extern UNSIGNED entry"
                )
            out.append(new_relocation(
                pending_subtractor["r_address"] + offset_bias,
                _relocation_symbol_name(
                    symbols,
                    pending_subtractor["r_symbolnum"],
                    local_rename,
                    context="SUBTRACTOR relocation",
                ),
                reloc_subtractor, False, pending_subtractor["r_length"], 0, None,
                _relocation_symbol_name(
                    symbols,
                    entry["r_symbolnum"],
                    local_rename,
                    context="SUBTRACTOR minuend relocation",
                ),
                None,
            ))
            pending_subtractor = None
            continue
        if entry["r_type"] == reloc_addend:
            if pending_addend is not None:
                raise LinkError("consecutive ADDEND relocation entries")
            if entry["r_extern"] or entry["r_pcrel"]:
                raise LinkError(
                    "ADDEND relocation must be non-extern and non-pcrel"
                )
            pending_addend = entry
            continue
        if pending_addend is not None and (
            not entry["r_extern"]
            or entry["r_address"] != pending_addend["r_address"]
            or entry["r_length"] != pending_addend["r_length"]
        ):
            raise LinkError(
                "ADDEND must be followed by a same-address, same-width "
                "extern relocation"
            )
        if entry["r_type"] == reloc_subtractor and entry["r_extern"]:
            if pending_addend is not None:
                raise LinkError("ADDEND before SUBTRACTOR is not supported")
            pending_subtractor = entry
            continue
        if not entry["r_extern"]:
            if entry["r_type"] != reloc_unsigned:
                raise LinkError(
                    f"non-extern relocation type {entry['r_type']} at "
                    f"{entry['r_address']} is outside the proven subset"
                )
            if entry["r_pcrel"] or entry["r_length"] != 3:
                raise LinkError(
                    "non-extern UNSIGNED relocation must be a non-pcrel "
                    "eight-byte section target"
                )
            index = entry["r_symbolnum"]
            if not 1 <= index <= len(sections):
                raise LinkError(
                    f"section-target relocation names section {index}"
                )
            target = sections[index - 1]
            out.append(new_relocation(
                entry["r_address"] + offset_bias, "",
                entry["r_type"], bool(entry["r_pcrel"]), entry["r_length"], 0,
                (target["segname_str"], target["sectname_str"]), None, None,
            ))
            pending_addend = None
            continue
        addend = (
            pending_addend["r_symbolnum"]
            if pending_addend is not None
            else 0
        )
        pending_addend = None
        symbol_index = entry["r_symbolnum"]
        if symbol_names is not None and 0 <= symbol_index < len(symbol_names):
            symbol_name = symbol_names[symbol_index]
        else:
            symbol_name = _relocation_symbol_name(
                symbols,
                symbol_index,
                local_rename,
                context="extern relocation",
            )
        out.append(new_relocation(
            entry["r_address"] + offset_bias, symbol_name, entry["r_type"],
            bool(entry["r_pcrel"]), entry["r_length"], addend, None, None, None,
        ))
    if pending_addend is not None:
        raise LinkError("trailing ADDEND entry with nothing to modify")
    if pending_subtractor is not None:
        raise LinkError("trailing SUBTRACTOR entry without UNSIGNED pair")
    return out


def _inspect_link_input(
    item: tuple[int, LinkInput, ShardedSymbolDefinitions],
) -> _InspectedLinkInput:
    """Parse and validate one input without mutating global link state."""

    index, data, definitions = item
    obj = _coerce_link_object(data, index)
    if isinstance(obj, PackedNativeObject):
        symbols = obj.symbols
        targeted = obj.relocation_target_indices
        for symbol_index, symbol in enumerate(symbols):
            if symbol.section_index and symbol.external:
                definitions.add(
                    symbol.name,
                    SymbolDefinition(index, symbol_index),
                )
        return _InspectedLinkInput(
            obj,
            symbols,
            targeted,
            _native_section_addrs(obj),
        )

    if isinstance(obj, NativeObject):
        symbols = obj.symbols
        targeted: set[int] = set()
        for section in obj.sections:
            for entry in section.relocations:
                if entry.symbol_index is not None:
                    targeted.add(entry.symbol_index)
                if entry.minuend_index is not None:
                    targeted.add(entry.minuend_index)
        for symbol_index, symbol in enumerate(symbols):
            if symbol.section_index and symbol.external:
                definitions.add(
                    symbol.name,
                    SymbolDefinition(index, symbol_index),
                )
        return _InspectedLinkInput(
            obj,
            symbols,
            frozenset(targeted),
            _native_section_addrs(obj),
        )

    if obj.header["filetype"] != spec.MH_OBJECT:
        raise LinkError(f"input {index} is not an MH_OBJECT")
    if (
        obj.header["cputype"] != spec.CPU_TYPE_ARM64
        or obj.header["cpusubtype"] != spec.CPU_SUBTYPE_ARM64_ALL
    ):
        raise LinkError(
            f"input {index} is not an arm64-all object "
            f"(cputype={obj.header['cputype']}, "
            f"cpusubtype={obj.header['cpusubtype']})"
        )

    symbols = obj.symbols()
    for symbol in symbols:
        if (symbol["n_type"] & spec.N_TYPE) != spec.N_UNDF:
            continue
        if (
            not (symbol["n_type"] & spec.N_EXT)
            or symbol["n_sect"] != spec.NO_SECT
            or symbol["n_value"] != 0
        ):
            raise LinkError(
                f"input {index} symbol {symbol['name']!r} uses an "
                "unsupported local/common undefined shape"
            )

    targeted: set[int] = set()
    symbol_count = len(symbols)
    for section in obj.sections():
        # Only extern/type/symbolnum are needed here; skip per-row dicts.
        for info in obj.relocation_infos(section):
            if (
                (info >> spec.RELOC_EXTERN_SHIFT) & 0x1
                and ((info >> spec.RELOC_TYPE_SHIFT) & 0xF) != spec.ARM64_RELOC_ADDEND
            ):
                symbol_index = info & spec.RELOC_SYMBOLNUM_MASK
                # Only the index check matters here; resolving the name for
                # every relocation was discarded work.
                if not 0 <= symbol_index < symbol_count:
                    _relocation_symbol_name(
                        symbols,
                        symbol_index,
                        {},
                        context=f"input {index} relocation",
                    )
                targeted.add(symbol_index)
    for symbol_index, symbol in enumerate(symbols):
        if (
            (symbol["n_type"] & spec.N_TYPE) == spec.N_SECT
            and symbol["n_type"] & spec.N_EXT
        ):
            definitions.add(
                symbol["name"],
                SymbolDefinition(index, symbol_index),
            )
    return _InspectedLinkInput(obj, symbols, frozenset(targeted))


def _link_input_size_hint(value: LinkInput) -> int:
    """Cheap byte-volume hint used only for bounded worker selection."""

    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, NativeObject):
        return sum(len(section.data) for section in value.sections)
    if isinstance(value, PackedNativeObject):
        return len(value.encoded)
    if isinstance(value, NativeObjectView):
        return len(value.data)
    if isinstance(value, spec.MachOObject):
        return len(value.data)
    return 0


def _coerce_indexed_link_object(item: tuple[int, LinkInput]):
    input_index, value = item
    return _coerce_link_object(value, input_index)


def _coerce_link_objects(
    values: list[LinkInput],
    *,
    start_index: int = 0,
) -> list:
    """Parse independent external objects with deterministic result order."""

    indexed = [
        (start_index + offset, value)
        for offset, value in enumerate(values)
    ]
    try:
        return ordered_parallel_map(
            indexed,
            _coerce_indexed_link_object,
            total_bytes=sum(_link_input_size_hint(value) for value in values),
        )
    except ParallelLinkError as exc:
        raise LinkError(f"parallel input parsing failed: {exc}") from exc


def _merge_input_symbols(
    index, symbols, targeted_indices, sections, section_addrs, local_rename,
    bases, input_merged_key, external_definitions, merged, defined, referenced,
    weak_references, strong_references,
) -> None:
    """Shared symbol provenance/order step after one input's sections."""
    for symbol_index, sym in enumerate(symbols):
        name = _symbol_name(sym)
        sym_type = _symbol_type(sym)
        if (sym_type & spec.N_TYPE) == spec.N_UNDF:
            referenced.add(name)
            if _symbol_is_weak_reference(sym):
                weak_references.add(name)
            else:
                strong_references.add(name)
            continue
        if (sym_type & spec.N_TYPE) != spec.N_SECT:
            raise LinkError(f"symbol {name!r} has an unsupported type")
        if (
            name[:1] in ("l", "L")
            and symbol_index not in targeted_indices
        ):
            # Mach-O assembler-local temporaries that nothing references:
            # they delimit atoms inside one input and mean nothing after
            # the merge. `ld -r` drops them, and so does this — keeping
            # them would make every multi-object link collide on `ltmp0`.
            # Referenced L-labels (pcc emits cstring labels this way) are
            # real targets and are kept.
            continue
        name = local_rename.get(name, name)
        if sym_type & spec.N_EXT:
            owner = external_definitions.owner(name)
            if owner != SymbolDefinition(index, symbol_index):
                raise LinkError(f"duplicate definition of {name!r}")
        if name in defined:
            raise LinkError(f"duplicate definition of {name!r}")
        sec_index = _symbol_section_index(sym)
        if sec_index not in bases:
            raise LinkError(f"symbol {name!r} names section {sec_index}")
        sec = sections[sec_index - 1]
        key = _section_key(sec)
        merged_key = input_merged_key.get(key, key)
        # n_value is the section-relative address in an object file;
        # subtract the section's own addr to get the payload offset.
        if isinstance(sym, (NativeSymbol, PackedNativeSymbol)):
            offset = sym.offset + bases[sec_index]
        else:
            offset = (
                sym["n_value"]
                - _section_addr(sec, sec_index, section_addrs)
                + bases[sec_index]
            )
        merged[merged_key].symbols.append(TextSymbol(
            name,
            offset,
            external=bool(sym_type & spec.N_EXT),
            private_external=bool(sym_type & spec.N_PEXT),
        ))
        defined[name] = merged_key


def link_relocatable_native(
    objects: list[LinkInput], *, _consume_inputs: bool = False,
    _source_view: bool = False,
    compact_budget=None,
    _final_executable: bool = False,
):
    """Merge inputs into pcc's indexed object without a Mach-O round trip.

    ``_final_executable`` marks a merge whose result becomes an executable
    image with no further merge, so its stack-map table takes the selected
    runtime format; a relocatable result keeps version 2.
    """
    if not _consume_inputs:
        objects = list(objects)
    if not objects:
        raise LinkError("nothing to link")

    merged: dict[tuple[str, str], _MergedSection] = {}
    order: list[tuple[str, str]] = []
    defined: dict[str, tuple[str, str]] = {}
    referenced: set[str] = set()
    weak_references: set[str] = set()
    strong_references: set[str] = set()
    # Dedicated stack-map sections are semantic tables, not byte streams:
    # concatenating two versioned headers would produce an apparently valid
    # first table followed by trailing garbage.  Decode each input and rebuild
    # one deterministic table after all native symbols have been inspected.
    stack_map_payloads: list[bytes] = []
    stack_map_snapshot_bytes = 0
    input_raw_bytes = 0
    # Merged bytes that do not share an input buffer: alignment padding and
    # patched or sliced copies. A payload view of an input is already inside
    # input_raw_bytes, so charging it again would count the inputs twice.
    merged_private_bytes = 0
    if compact_budget is not None:
        input_raw_bytes = sum(_link_input_size_hint(value) for value in objects)
    stack_map_symbol_by_function_id: dict[int, str] = {}
    # (merged section, relocation, this input's section addresses). Defer
    # section targets so rewriting never searches or removes from the large
    # symbol-relocation prefix. Appending rewrites later preserves the old
    # remove-and-append order exactly.
    rebases: list = []

    # Parse/scan inputs through stable worker partitions and retain
    # symbol-index provenance.  External definitions enter a sharded table,
    # but ownership is the minimum (input index, symbol index), never the
    # thread that happened to insert first.  The merge below remains in exact
    # command-line order.  Assembler-local temporaries are dropped only when
    # THIS input has no relocation to their table entry; comparing names
    # across objects conflates distinct local symbols.
    external_definitions = ShardedSymbolDefinitions()
    indexed_objects = [
        (index, value, external_definitions)
        for index, value in enumerate(objects)
    ]
    try:
        inspected_inputs = ordered_parallel_map(
            indexed_objects,
            _inspect_link_input,
            total_bytes=sum(_link_input_size_hint(value) for value in objects),
        )
        external_definitions.freeze()
    except ParallelLinkError as exc:
        raise LinkError(f"parallel input inspection failed: {exc}") from exc
    parsed_objects = [item.obj for item in inspected_inputs]
    symbol_tables = [item.symbols for item in inspected_inputs]
    relocation_target_indices = [
        item.relocation_target_indices for item in inspected_inputs
    ]

    local_renames = _local_symbol_renames(symbol_tables)
    text_chunk_index = 0
    text_chunk_key = _TEXT_KEY

    for index, obj in enumerate(parsed_objects):

        is_native = isinstance(obj, (NativeObject, PackedNativeObject))
        sections = obj.sections if is_native else obj.sections()
        symbols = symbol_tables[index]
        local_rename = local_renames[index]
        relocation_symbol_names = None
        # Packed and Mach-O inputs resolve each symbol once, not per relocation.
        if not isinstance(obj, NativeObject):
            relocation_symbol_names = [
                _relocation_symbol_name(symbols, symbol_index, local_rename, context="packed native relocation")
                for symbol_index in range(len(symbols))
            ]
        section_addrs = inspected_inputs[index].section_addrs
        debug_bases, debug_sizes = {}, {}
        debug_abbreviations = None
        for debug_index, debug_section in enumerate(sections, start=1):
            debug_key = _section_key(debug_section)
            if debug_key[0] != "__DWARF":
                continue
            debug_sizes[debug_key[1]] = _section_size(debug_section)
            existing = merged.get(debug_key)
            debug_bases[debug_key[1]] = _align_up(
                0 if existing is None else existing.data_size, _section_align(debug_section),
            )
            if debug_key[1] == "__debug_abbrev":
                debug_abbreviations = _section_payload(obj, debug_section, debug_index, section_addrs)

        # LC_DATA_IN_CODE uses object-address offsets, not file offsets.
        # Assign every range to exactly one input section now so its start can
        # be rebased alongside that section's payload during the merge.
        data_regions_by_section: dict[int, list[DataInCodeRegion]] = {}
        if is_native:
            for sec_index, section in enumerate(sections, start=1):
                if section.data_in_code:
                    if isinstance(section, PackedNativeSection):
                        data_regions_by_section[sec_index] = [
                            DataInCodeRegion(*region)
                            for region in section.data_in_code
                        ]
                    else:
                        data_regions_by_section[sec_index] = list(
                            section.data_in_code
                        )
        else:
            for entry in obj.data_in_code():
                start = entry["offset"]
                end = start + entry["length"]
                owners = [
                    (sec_index, sec)
                    for sec_index, sec in enumerate(sections, start=1)
                    if (
                        _section_addr(sec, sec_index, section_addrs) <= start
                        and end <= (
                            _section_addr(sec, sec_index, section_addrs)
                            + _section_size(sec)
                        )
                    )
                ]
                if len(owners) != 1:
                    raise LinkError(
                        "data-in-code range does not belong to exactly one "
                        f"section: {start}..{end}"
                    )
                sec_index, sec = owners[0]
                data_regions_by_section.setdefault(sec_index, []).append(
                    DataInCodeRegion(
                        offset=start - _section_addr(
                            sec, sec_index, section_addrs,
                        ),
                        length=entry["length"],
                        kind=entry["kind"],
                    )
                )

        # Where each of this input's sections lands in the merged output.
        bases: dict[int, int] = {}
        # This input's own section addresses and their merged bases, keyed by
        # (segname, sectname): a stored address is rebased through them.
        input_section_addr: dict[tuple[str, str], tuple[int, int]] = {}
        # This input's section keys mapped to the merged sections they joined;
        # only __TEXT,__text can land in a later chunk.
        input_merged_key: dict[tuple[str, str], tuple[str, str]] = {}
        for sec_index, sec in enumerate(sections, start=1):
            key = _section_key(sec)
            sec_flags = _section_flags(sec)
            sec_align = _section_align(sec)
            sec_size = _section_size(sec)
            sec_addr = _section_addr(sec, sec_index, section_addrs)
            if key == ("__DATA", "__pcc_stackmaps"):
                if sec_flags != spec.S_REGULAR or sec_align < 3:
                    raise LinkError("input stack-map section has invalid flags/alignment")
                try:
                    if compact_budget is not None:
                        # Keep the source, any temporary payload and the new
                        # immutable snapshot in the known-buffer estimate.
                        compact_budget.check_peak(
                            input_raw_bytes + merged_private_bytes
                            + stack_map_snapshot_bytes + 2 * sec_size,
                            entries=len(stack_map_symbol_by_function_id),
                            label="Mach-O input stack-map snapshot",
                        )
                    payload = bytes(_section_payload(
                        obj, sec, sec_index, section_addrs,
                    ))
                    if compact_budget is None:
                        _function_count, scanned, _tstart, _tcount = (
                            _scan_stack_map_payload(payload)
                        )
                    else:
                        _function_count, scanned, _tstart, _tcount = (
                            _scan_stack_map_payload(
                                payload, compact_budget=compact_budget,
                            )
                        )
                except PreciseStackMapError as exc:
                    sizes = getattr(compact_budget, "last_rejection", "")
                    raise LinkError(
                        f"input stack-map section is malformed: {exc}"
                        + ("; " + sizes if sizes else "")
                    ) from exc
                stack_relocations = _read_relocations(
                    obj, sec, symbols, local_rename,
                    symbol_names=relocation_symbol_names,
                    object_sections=sections,
                )
                relocation_by_offset = {
                    relocation.offset: relocation
                    for relocation in stack_relocations
                }
                address_offsets = [
                    fn_start + 8 for _fid, fn_start, _fn_end in scanned
                ]
                if (
                    len(relocation_by_offset) != len(stack_relocations)
                    or set(relocation_by_offset) != set(address_offsets)
                ):
                    raise LinkError(
                        "input stack-map function addresses and relocations disagree"
                    )
                if any(
                    payload[offset:offset + 8] != b"\0" * 8
                    for offset in address_offsets
                ):
                    raise LinkError(
                        "relocatable stack-map function address must be zero"
                    )
                if any(
                    (_symbol_type(sym) & spec.N_TYPE) == spec.N_SECT
                    and _symbol_section_index(sym) == sec_index
                    for sym in symbols
                ):
                    raise LinkError("stack-map section must not define symbols")
                for (function_id, _fn_start, _fn_end), address_offset in zip(
                    scanned, address_offsets,
                ):
                    relocation = relocation_by_offset[address_offset]
                    if (
                        relocation.type != spec.ARM64_RELOC_UNSIGNED
                        or relocation.pcrel
                        or relocation.length != 3
                        or relocation.section is not None
                        or relocation.minuend is not None
                        or relocation.addend
                    ):
                        raise LinkError(
                            "stack-map address relocation is not plain UNSIGNED64"
                        )
                    previous_symbol = stack_map_symbol_by_function_id.get(
                        function_id,
                    )
                    if previous_symbol is not None:
                        raise LinkError(
                            "duplicate stable function id in stack-map inputs"
                        )
                    stack_map_symbol_by_function_id[function_id] = (
                        relocation.symbol
                    )
                    referenced.add(relocation.symbol)
                stack_map_payloads.append(payload)
                if compact_budget is not None:
                    stack_map_snapshot_bytes += len(payload)
                continue
            (
                merged_key, target, base, padding,
                text_chunk_index, text_chunk_key,
            ) = _place_merged_section(
                merged, order, key, sec_flags, sec_align, sec_size,
                text_chunk_index, text_chunk_key,
            )
            input_merged_key[key] = merged_key
            input_is_code = bool(sec_flags & spec.S_ATTR_PURE_INSTRUCTIONS)
            relocations_proven = isinstance(obj, PackedNativeObject)
            if not _is_zerofill(sec):
                if padding:
                    target.data_parts.append(b"\0" * padding)
                    merged_private_bytes += padding
                payload = _section_payload(
                    obj, sec, sec_index, section_addrs,
                )
                if key == ("__DWARF", "__debug_info"):
                    from .macho_dwarf import DwarfLinkError, patch_dwarf_info
                    if debug_abbreviations is None:
                        raise LinkError("DWARF info has no input abbreviation section")
                    if compact_budget is not None:
                        compact_budget.check_peak(
                            input_raw_bytes + merged_private_bytes + stack_map_snapshot_bytes
                            + len(payload) + len(debug_abbreviations) * 1024 + 8192,
                            label="Mach-O DWARF offset repair",
                        )
                    payload = bytearray(payload)
                    try:
                        patch_dwarf_info(payload, debug_abbreviations, debug_bases, debug_sizes)
                    except DwarfLinkError as error:
                        raise LinkError("DWARF merge rejected: " + str(error)) from error
                target.data_parts.append(payload)
                # A memoryview, or a native section's own bytes, shares the
                # input buffer; a bytearray or a bytes slice is a copy.
                if not (isinstance(payload, memoryview)
                        or (is_native and not isinstance(payload, bytearray))):
                    merged_private_bytes += len(payload)
                target.data_size = base + len(payload)
                # Decode checked each record against this payload: shifted by
                # base it stays inside the merged payload, inside the signed
                # r_address range while the payload ends by 2 GiB, and on the
                # instruction grid while a code base is 4-aligned.
                relocations_proven = relocations_proven and (
                    base + len(payload) <= 0x80000000
                    and (not input_is_code or base % 4 == 0)
                )
            if not input_is_code:
                target.proven_includes_data = True
            bases[sec_index] = base
            input_section_addr[key] = (sec_addr, base)
            target.data_in_code.extend(
                DataInCodeRegion(
                    offset=base + region.offset,
                    length=region.length,
                    kind=region.kind,
                )
                for region in data_regions_by_section.get(sec_index, ())
            )

            for reloc in _read_relocations(
                obj, sec, symbols, local_rename, offset_bias=base,
                symbol_names=relocation_symbol_names,
                object_sections=sections,
            ):
                if reloc.section is None:
                    target.relocations.append(reloc)
                    referenced.add(reloc.symbol)
                    if reloc.minuend is not None:
                        referenced.add(reloc.minuend)
                else:
                    # The stored value is an address in THIS input's space;
                    # record what it has to be rebased by once the merged
                    # layout is known.
                    rebases.append((
                        target, reloc, input_section_addr, input_merged_key,
                    ))
            if relocations_proven and target.proven_prefix_open:
                target.proven_relocations = len(target.relocations)
            else:
                target.proven_prefix_open = False

        _merge_input_symbols(
            index, symbols, relocation_target_indices[index], sections,
            section_addrs, local_rename, bases, input_merged_key,
            external_definitions, merged, defined, referenced,
            weak_references, strong_references,
        )

    if _consume_inputs:
        # The merge loop was the inputs' last use; only the payload views in
        # data_parts still hold their buffers. Retire every other reference
        # now, so each input's buffer is freed by the join that copies its
        # last view instead of surviving until the merged object is built.
        # Borrowed API inputs keep their existing lifetime and contents.
        objects.clear()
        indexed_objects.clear()
        inspected_inputs.clear()
        parsed_objects.clear()
        symbol_tables.clear()
        relocation_target_indices.clear()
        obj = None
        sections = None
        symbols = None
        sec = None
        # The last ordinary section may still be a memoryview into its PCO.
        # Drop this local too: data_parts must own every remaining view so
        # the joins below really release the inputs before their budget credit.
        payload = None

    # Keep each input payload once while calculating offsets. Growing the
    # merged bytearray for every object copies an ever-larger prefix in the
    # native runtime. Keep the joined bytes immutable until an actual section
    # rebase needs a write, so unchanged text is not copied into a bytearray
    # here and back into bytes at the output boundary. Every input spreads its
    # payloads over the data sections but over at most a few consecutive text
    # chunks, so joining the data sections first lets each text chunk's join
    # free the inputs whose text ends in it: the peak is about one copy of the
    # inputs plus one chunk, not two copies.
    for key in sorted(merged, key=_is_text_chunk_key):
        section = merged[key]
        section.data = b"".join(section.data_parts)
        section.data_parts.clear()
    if _consume_inputs:
        input_raw_bytes = 0

    if stack_map_payloads:
        try:
            if compact_budget is None:
                stack_payload, stack_address_offsets = merge_stack_map_payloads(
                    tuple(stack_map_payloads)
                )
            else:
                compact_budget.check_peak(
                    input_raw_bytes
                    + sum(len(section.data) for section in merged.values())
                    + 3 * stack_map_snapshot_bytes,
                    entries=len(stack_map_symbol_by_function_id),
                    label="Mach-O merged stack-map buffers",
                )
                previous_retained_bytes = compact_budget.retained_bytes
                compact_budget.retained_bytes = (
                    previous_retained_bytes + input_raw_bytes
                    + sum(len(section.data) for section in merged.values())
                )
                try:
                    stack_payload, stack_address_offsets = merge_stack_map_payloads(
                        tuple(stack_map_payloads), compact_budget=compact_budget,
                    )
                finally:
                    compact_budget.retained_bytes = previous_retained_bytes
            if _final_executable and precise_stackmap_runtime_format() == RUNTIME_TABLE_VERSION:
                if compact_budget is not None:
                    compact_budget.check_peak(
                        input_raw_bytes
                        + sum(len(section.data) for section in merged.values())
                        + 4 * len(stack_payload),
                        entries=len(stack_address_offsets),
                        label="Mach-O runtime stack-map table",
                    )
                stack_payload, stack_address_offsets = transcode_runtime_table(
                    stack_payload, stack_address_offsets,
                )
        except PreciseStackMapError as exc:
            raise LinkError(f"merged stack-map table is invalid: {exc}") from exc
        # The merged payload owns its bytes. Input table snapshots can be
        # large in a self-host link and are no longer needed after this phase.
        stack_map_payloads = None
        stack_key = ("__DATA", "__pcc_stackmaps")
        stack_section = _MergedSection(
            stack_key[0], stack_key[1], spec.S_REGULAR,
            align_log2=3,
            data=stack_payload,
        )
        for function_id, address_offset in stack_address_offsets:
            symbol = stack_map_symbol_by_function_id[function_id]
            stack_section.relocations.append(Relocation(
                offset=address_offset,
                symbol=symbol,
                type=spec.ARM64_RELOC_UNSIGNED,
                pcrel=False,
                length=3,
            ))
        merged[stack_key] = stack_section
        order.append(stack_key)

    # An unresolved symbol is valid in an MH_OBJECT: preserve it for the
    # eventual final link rather than guessing a definition.
    unresolved = sorted(referenced - set(defined))
    weak_unresolved = tuple(sorted(set(unresolved) & (weak_references - strong_references)))

    # Segment order decides every symbol's address; within a segment,
    # zerofill sections must come last (ld rejects content after zerofill).
    seg_rank = {"__TEXT": 0, "__DATA_CONST": 1, "__DATA": 2}
    appearance = {key: i for i, key in enumerate(order)}
    order.sort(key=lambda key: (
        seg_rank.get(key[0], 3),
        1 if (merged[key].zerofill_size and not merged[key].data) else 0,
        appearance[key],
    ))

    # A section-target relocation stores an address in its input's address
    # space. The input's target section moved to `base` inside the merged
    # section, so the stored value is rebased by (base - old section addr).
    # The merged section's own final address is added by the consumer (the
    # executable linker) or left at the object's section addr (relocatable
    # output), exactly as ld -r leaves it.
    import struct as _struct
    # Where each defined symbol landed, so a rebased address can be named.
    symbol_at: dict[tuple[tuple[str, str], int], str] = {}
    for key, section in merged.items():
        for sym in section.symbols:
            symbol_at.setdefault((key, sym.offset), sym.name)

    for target, entry, input_addrs, input_keys in rebases:
        offset = entry.offset
        target_key = entry.section
        assert target_key is not None
        if target_key not in input_addrs:
            raise LinkError(
                f"section-target relocation names {target_key}, which the "
                "same input does not contain"
            )
        old_addr, new_base = input_addrs[target_key]
        merged_target_key = input_keys.get(target_key, target_key)
        if not isinstance(target.data, bytearray):
            target.data = bytearray(target.data)
        stored, = _struct.unpack_from("<Q", target.data, offset)
        rebased = (stored - old_addr + new_base) & _U64_MASK

        # `ld -r` re-points these at the SYMBOL that owns the address and
        # zeroes the field, rather than keeping a section target with the
        # address baked in — which survives any later reordering, so pcc
        # does the same whenever a defined symbol sits exactly there.
        # Debug consumers inspect MH_OBJECT addresses directly. Keep their
        # section targets instead of replacing the address with a zero-valued
        # external-symbol addend, which would overlap all function ranges.
        owner = None if target.segname == "__DWARF" else symbol_at.get((merged_target_key, rebased))
        if owner is not None:
            _struct.pack_into("<Q", target.data, offset, 0)
            target.relocations.append(Relocation(
                offset=offset, symbol=owner, type=entry.type,
                pcrel=entry.pcrel, length=entry.length, addend=0,
            ))
            referenced.add(owner)
        else:
            _struct.pack_into("<Q", target.data, offset, 0)
            target.relocations.append(Relocation(
                offset=entry.offset,
                symbol="",
                type=entry.type,
                pcrel=entry.pcrel,
                length=entry.length,
                addend=0,
                section=merged_target_key,
                target_offset=rebased,
            ))

    # These private tables have finished their last semantic use. In
    # particular, rebases still owns the source records that were replaced.
    rebases = None
    symbol_at = None
    defined = None
    referenced = None
    local_renames = None

    out_sections = []
    proven_relocations = []
    for key in order:
        m = merged[key]
        # A data input's records were not held to the instruction grid at
        # decode; once attributes make the merged section code, they are.
        if m.proven_includes_data and m.flags & spec.S_ATTR_PURE_INSTRUCTIONS:
            proven_relocations.append(0)
        else:
            proven_relocations.append(m.proven_relocations)
        zero = m.zerofill_size if not m.data else 0
        payload = bytes(m.data)
        # A rebased section was a bytearray; bytes() copied it, so drop the
        # bytearray now instead of keeping both until every section is built.
        m.data = b""
        out_sections.append(Section(
            sectname=m.sectname, segname=m.segname,
            data=payload,
            align_log2=m.align_log2, flags=m.flags,
            symbols=tuple(sorted(m.symbols, key=lambda s: s.offset)),
            # This is a fresh merge buffer, independent of every caller input.
            # Transfer it so indexing can retire source records incrementally.
            relocations=m.relocations,
            data_in_code=tuple(sorted(
                m.data_in_code, key=lambda region: region.offset,
            )),
            zerofill_size=zero,
        ))
    try:
        if _source_view:
            if compact_budget is not None:
                return OwnedMergedSourceView(
                    out_sections, undefined=unresolved, weak_undefined=weak_unresolved,
                    proven_relocations=tuple(proven_relocations),
                    compact_budget=compact_budget,
                )
            return OwnedMergedSourceView(
                out_sections, undefined=unresolved, weak_undefined=weak_unresolved,
                proven_relocations=tuple(proven_relocations),
            )
        return NativeObject.from_sections(
            out_sections, undefined=unresolved, weak_undefined=weak_unresolved,
            _consume_relocations=True,
        )
    except (MachOEmitError, NativeObjectError) as exc:
        raise LinkError(f"merged object is outside the proven subset: {exc}") from exc


def link_relocatable(objects: list[LinkInput]) -> bytes:
    """Merge objects into a standard relocatable object, as ``ld -r`` does.

    Standard Mach-O materialisation is kept at this public/external boundary;
    final pcc-to-pcc links call :func:`link_relocatable_native` directly.
    """
    try:
        return link_relocatable_native(list(objects)).to_macho()
    except (MachOEmitError, NativeObjectError) as exc:
        raise LinkError(f"merged object is outside the proven subset: {exc}") from exc
