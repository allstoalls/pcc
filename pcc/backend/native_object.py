"""Indexed internal object representation for pcc-to-pcc links.

LINK-P2-NATIVE-OBJ-FASTPATH: keep standard Mach-O at external boundaries while
the owned assembler/linker path carries already-resolved symbol indices.

``macho_obj.Section`` is the authoring model and Mach-O remains the external
object-file ABI.  Between pcc's assembler and linker, however, serialising an
``MH_OBJECT`` only to parse its load commands, nlist/string table, and
relocation table again is pure overhead.  ``NativeObject`` is that internal
seam: symbols occur once, relocations carry validated integer indices, and a
small versioned codec permits cache/process transport without becoming a
platform object format.

The codec is deliberately not accepted as Mach-O.  Conversion to a standard
object is explicit through :meth:`NativeObject.to_macho`; final executable
images remain ordinary Mach-O and external ``.o``/``.a`` inputs keep using the
normal parser and validation boundary.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from pcc.extern import c_int64, c_ptr, extern, c_obj
from pcc.unsafe import (
    free,
    malloc,
    memset,
    ptr_is_null,
    store_i8,
    store_i32,
    store_i64,
)

from . import macho_spec as spec
from .macho_obj import (
    COMPACT_UNWIND_SECTION_FLAGS,
    DataInCodeRegion,
    EH_FRAME_SECTION_FLAGS,
    MachOEmitError,
    MOD_INIT_SECTION_FLAGS,
    PCC_STACKMAP_SECTION_FLAGS,
    Relocation,
    Section,
    TextSymbol,
    _ADDEND_MAX,
    _DATA_IN_CODE_KINDS,
    _DATA_IN_CODE_UNITS,
    _PROVEN_RELOCATION_SHAPES,
    _validate_relocation,
    _validate_section,
    emit_object,
)
from .self_backend_value_arena import CompilerIntArena


class NativeObjectError(Exception):
    """The internal object is malformed or outside the proven object model."""


MAGIC = b"PCCNOBJ\x01"
_NONE_INDEX = 0xFFFFFFFF
_MAX_COUNT = 8_000_000  # merged pcc compiler closure exceeds 1M relocations
_MAX_NAME_BYTES = 1_048_576

_HEADER = struct.Struct("<8sII")
_U32 = struct.Struct("<I")
_SYMBOL = struct.Struct("<IQI")
_SECTION = struct.Struct("<IIQQII")
_RELOCATION = struct.Struct("<QIIBB2xqIIq")
_DATA_IN_CODE = struct.Struct("<QII")

_SYMBOL_EXTERNAL = 1
_SYMBOL_PRIVATE_EXTERNAL = 2
_UINT64_MAX = (1 << 64) - 1
_RELOCATION_SCALAR_COUNT = 9

_py_bytes_new: "extern" = extern("py_bytes_new", (c_ptr, c_int64), c_obj)


@dataclass(frozen=True)
class NativeSymbol:
    name: str
    # Zero denotes undefined; defined sections use Mach-O's one-based index.
    section_index: int
    offset: int
    external: bool
    private_external: bool = False


@dataclass(frozen=True, slots=True)
class NativeRelocation:
    offset: int
    # Indices below are zero-based indices into ``NativeObject.symbols``.
    symbol_index: int | None
    type: int
    pcrel: bool
    length: int = 2
    addend: int = 0
    # Section targets retain Mach-O's one-based section numbering.
    target_section_index: int | None = None
    minuend_index: int | None = None
    target_offset: int | None = None


@dataclass(frozen=True)
class NativeSection:
    segname: str
    sectname: str
    flags: int
    align_log2: int
    data: bytes
    relocations: tuple[NativeRelocation, ...] = ()
    zerofill_size: int = 0
    data_in_code: tuple[DataInCodeRegion, ...] = ()

    @property
    def vm_size(self) -> int:
        section_type = self.flags & spec.SECTION_TYPE
        if section_type in (spec.S_ZEROFILL, 0x12):
            return self.zerofill_size
        return len(self.data)


@dataclass(frozen=True)
class NativeObject:
    sections: tuple[NativeSection, ...]
    symbols: tuple[NativeSymbol, ...]

    def __post_init__(self) -> None:
        _validate_native_object(self)

    @classmethod
    def from_sections(
        cls,
        sections: list[Section] | tuple[Section, ...],
        *,
        undefined: list[str] | tuple[str, ...] = (),
    ) -> "NativeObject":
        source_sections = tuple(sections)
        undefined_names = tuple(undefined)
        _validate_source_sections(source_sections, undefined_names)

        defined: list[tuple[TextSymbol, int]] = []
        for section_index, section in enumerate(source_sections, start=1):
            for symbol in section.symbols:
                defined.append((symbol, section_index))
        defined.sort(key=lambda pair: (pair[1], pair[0].offset))
        local_defined = [pair for pair in defined if not pair[0].external]
        external_defined = [pair for pair in defined if pair[0].external]

        symbols: list[NativeSymbol] = []
        for symbol, section_index in local_defined + external_defined:
            symbols.append(NativeSymbol(
                name=symbol.name,
                section_index=section_index,
                offset=symbol.offset,
                external=symbol.external,
                private_external=symbol.private_external,
            ))
        for name in sorted(undefined_names):
            symbols.append(NativeSymbol(
                name=name,
                section_index=0,
                offset=0,
                external=True,
            ))

        symbol_index = {symbol.name: index for index, symbol in enumerate(symbols)}
        section_index = {
            (section.segname, section.sectname): index
            for index, section in enumerate(source_sections, start=1)
        }
        native_sections: list[NativeSection] = []
        for section in source_sections:
            relocations: list[NativeRelocation] = []
            for relocation in section.relocations:
                is_section_target = relocation.section is not None
                relocations.append(NativeRelocation(
                    offset=relocation.offset,
                    symbol_index=(
                        None if is_section_target
                        else symbol_index[relocation.symbol]
                    ),
                    type=relocation.type,
                    pcrel=relocation.pcrel,
                    length=relocation.length,
                    addend=relocation.addend,
                    target_section_index=(
                        section_index[relocation.section]
                        if is_section_target else None
                    ),
                    minuend_index=(
                        symbol_index[relocation.minuend]
                        if relocation.minuend is not None else None
                    ),
                    target_offset=relocation.target_offset,
                ))
            native_sections.append(NativeSection(
                segname=section.segname,
                sectname=section.sectname,
                flags=section.flags,
                align_log2=section.align_log2,
                data=bytes(section.data),
                relocations=tuple(relocations),
                zerofill_size=section.zerofill_size,
                data_in_code=tuple(section.data_in_code),
            ))
        return cls(tuple(native_sections), tuple(symbols))

    def to_sections(self) -> tuple[list[Section], list[str]]:
        return self._source_sections(True)

    def _source_relocations(self, section_index: int):
        for native_relocation in self.sections[section_index].relocations:
            target_section = None
            symbol_name = ""
            if native_relocation.target_section_index is not None:
                target = self.sections[
                    native_relocation.target_section_index - 1
                ]
                target_section = (target.segname, target.sectname)
            else:
                assert native_relocation.symbol_index is not None
                symbol_name = self.symbols[
                    native_relocation.symbol_index
                ].name
            minuend = None
            if native_relocation.minuend_index is not None:
                minuend = self.symbols[
                    native_relocation.minuend_index
                ].name
            yield Relocation(
                offset=native_relocation.offset,
                symbol=symbol_name,
                type=native_relocation.type,
                pcrel=native_relocation.pcrel,
                length=native_relocation.length,
                addend=native_relocation.addend,
                section=target_section,
                minuend=minuend,
                target_offset=native_relocation.target_offset,
            )

    def _source_sections(self, include_relocations: bool):
        symbols_by_section: list[list[TextSymbol]] = [
            [] for _section in self.sections
        ]
        undefined: list[str] = []
        for symbol in self.symbols:
            if symbol.section_index == 0:
                undefined.append(symbol.name)
                continue
            symbols_by_section[symbol.section_index - 1].append(TextSymbol(
                name=symbol.name,
                offset=symbol.offset,
                external=symbol.external,
                private_external=symbol.private_external,
            ))

        sections: list[Section] = []
        for source_index, native_section in enumerate(self.sections):
            relocations = (
                tuple(self._source_relocations(source_index))
                if include_relocations else ()
            )
            sections.append(Section(
                sectname=native_section.sectname,
                segname=native_section.segname,
                data=native_section.data,
                align_log2=native_section.align_log2,
                flags=native_section.flags,
                symbols=tuple(symbols_by_section[source_index]),
                relocations=tuple(relocations),
                zerofill_size=native_section.zerofill_size,
                data_in_code=native_section.data_in_code,
            ))
        return sections, undefined

    def to_macho(self) -> bytes:
        """Materialise a standard MH_OBJECT at an explicit external boundary."""
        sections, undefined = self.to_sections()
        return emit_object(sections, undefined=undefined)

    def link_view(self) -> "NativeObjectView":
        """Return the cached-table interface consumed by the Mach-O linker."""
        return NativeObjectView(self)


@dataclass(frozen=True, slots=True)
class PackedNativeSymbol:
    """One symbol descriptor retained by the read-only codec view."""

    name: str
    section_index: int
    offset: int
    flags: int

    @property
    def external(self) -> bool:
        return bool(self.flags & _SYMBOL_EXTERNAL)

    @property
    def private_external(self) -> bool:
        return bool(self.flags & _SYMBOL_PRIVATE_EXTERNAL)


@dataclass(frozen=True, slots=True)
class PackedNativeSection:
    """Section metadata plus spans into one immutable encoded object."""

    segname: str
    sectname: str
    flags: int
    align_log2: int
    zerofill_size: int
    data_offset: int
    data_size: int
    relocation_offset: int
    relocation_count: int
    data_in_code: tuple[tuple[int, int, int], ...]
    relocation_order: int

    @property
    def vm_size(self) -> int:
        section_type = self.flags & spec.SECTION_TYPE
        if section_type in (spec.S_ZEROFILL, 0x12):
            return self.zerofill_size
        return self.data_size


class PackedNativeObject:
    """Fully validated read-only view of exact encoded ``.pco`` bytes.

    Symbols and sections are small descriptors.  Relocations remain fixed-size
    records in ``encoded`` and are unpacked only while the linker consumes
    them; no input ``NativeRelocation`` graph or duplicate payload buffer is
    built.
    """

    is_pcc_packed_object = True

    def __init__(
        self,
        encoded: bytes,
        sections: tuple[PackedNativeSection, ...],
        symbols: tuple[PackedNativeSymbol, ...],
    ) -> None:
        self.encoded = encoded
        self.sections = sections
        self.symbols = symbols
        self.relocation_target_indices: frozenset[int] = frozenset()

    def section_data(self, section_index: int) -> object:
        # Host execution returns a zero-copy memoryview.  The pcc frontend does
        # not yet model memoryview slicing faithfully, so keep the public
        # static boundary generic instead of claiming the inferred bytes type.
        section = self.sections[section_index]
        start = section.data_offset
        return memoryview(self.encoded)[start:start + section.data_size]

    def relocation_fields(self, section_index: int):
        section = self.sections[section_index]
        count = section.relocation_count
        if section.relocation_order < 0:
            indices = range(count)
        elif section.relocation_order > 0:
            indices = range(count - 1, -1, -1)
        else:
            indices = sorted(
                range(count),
                key=lambda index: _RELOCATION.unpack_from(
                    self.encoded,
                    section.relocation_offset + index * _RELOCATION.size,
                )[0],
                reverse=True,
            )
        for index in indices:
            yield _RELOCATION.unpack_from(
                self.encoded,
                section.relocation_offset + index * _RELOCATION.size,
            )

    def decoded_relocations(self, section_index: int):
        for fields in self.relocation_fields(section_index):
            yield (
                fields[0],
                _decode_index(fields[1]),
                fields[2],
                bool(fields[3]),
                fields[4],
                fields[5],
                _decode_index(fields[6]),
                _decode_index(fields[7]),
                None if fields[8] == -1 else fields[8],
            )


def _validate_source_sections(
    sections: tuple[Section, ...],
    undefined: tuple[str, ...],
    *,
    relocation_source=None,
    relocation_counts=None,
) -> None:
    try:
        if not sections:
            raise MachOEmitError("at least one section is required")
        if len({(s.segname, s.sectname) for s in sections}) != len(sections):
            raise MachOEmitError("duplicate section names")
        seen: set[str] = set()
        seen_zerofill_segments: set[str] = set()
        for section_index, section in enumerate(sections):
            if relocation_source is None:
                _validate_section(section)
            else:
                _validate_section(
                    section,
                    relocations=relocation_source(section_index),
                    relocation_count=relocation_counts[section_index],
                )
            if section.is_zerofill:
                seen_zerofill_segments.add(section.segname)
            elif section.segname in seen_zerofill_segments:
                raise MachOEmitError(
                    f"content section {section.sectname} after a zerofill "
                    "section"
                )
            for symbol in section.symbols:
                if "\0" in symbol.name:
                    raise MachOEmitError(
                        f"symbol name {symbol.name!r} contains NUL"
                    )
                if symbol.name in seen:
                    raise MachOEmitError(
                        f"duplicate symbol {symbol.name!r}"
                    )
                seen.add(symbol.name)
        if not seen:
            raise MachOEmitError("at least one defined symbol is required")
        for name in undefined:
            if not isinstance(name, str) or not name or not name.isascii():
                raise MachOEmitError(f"bad undefined symbol name {name!r}")
            if "\0" in name:
                raise MachOEmitError(f"symbol name {name!r} contains NUL")
            if name in seen:
                raise MachOEmitError(
                    f"symbol {name!r} both defined and undefined"
                )
            seen.add(name)
        section_by_name = {
            (section.segname, section.sectname): section
            for section in sections
        }
        known = {name: index for index, name in enumerate(seen)}
        for section_index, section in enumerate(sections):
            relocation_offsets: set[int] = set()
            relocations = (
                section.relocations if relocation_source is None
                else relocation_source(section_index)
            )
            for relocation in relocations:
                _validate_relocation(
                    section, relocation, known, section_by_name,
                )
                if relocation.offset in relocation_offsets:
                    raise MachOEmitError(
                        "multiple relocation requests at offset "
                        f"{relocation.offset} in {section.segname},"
                        f"{section.sectname}"
                    )
                relocation_offsets.add(relocation.offset)
    except MachOEmitError as exc:
        raise NativeObjectError(str(exc)) from exc


def _validate_native_object(obj: NativeObject) -> None:
    if not isinstance(obj, NativeObject):
        raise NativeObjectError("expected a NativeObject instance")
    if not isinstance(obj.sections, tuple):
        raise NativeObjectError("native object sections must be a tuple")
    if not obj.sections or len(obj.sections) > _MAX_COUNT:
        raise NativeObjectError("native object has an invalid section count")
    if not isinstance(obj.symbols, tuple):
        raise NativeObjectError("native object symbols must be a tuple")
    if not obj.symbols or len(obj.symbols) > _MAX_COUNT:
        raise NativeObjectError("native object has an invalid symbol count")

    seen_names: set[str] = set()
    saw_external_definition = False
    saw_undefined = False
    last_defined_key: dict[bool, tuple[int, int] | None] = {
        False: None,
        True: None,
    }
    last_undefined_name: str | None = None
    for symbol in obj.symbols:
        if not isinstance(symbol, NativeSymbol):
            raise NativeObjectError("native object has a non-symbol entry")
        _validate_name(symbol.name, "symbol")
        if symbol.name in seen_names:
            raise NativeObjectError(
                f"native object duplicates symbol {symbol.name!r}"
            )
        seen_names.add(symbol.name)
        if not isinstance(symbol.section_index, int) or isinstance(
            symbol.section_index, bool
        ):
            raise NativeObjectError("symbol section index must be an integer")
        if not isinstance(symbol.offset, int) or isinstance(symbol.offset, bool):
            raise NativeObjectError("symbol offset must be an integer")
        if not 0 <= symbol.offset <= _UINT64_MAX:
            raise NativeObjectError("symbol offset is outside uint64 range")
        if not isinstance(symbol.external, bool) or not isinstance(
            symbol.private_external, bool
        ):
            raise NativeObjectError("symbol visibility flags must be boolean")
        if symbol.section_index == 0:
            saw_undefined = True
            if not symbol.external or symbol.private_external or symbol.offset != 0:
                raise NativeObjectError(
                    f"undefined symbol {symbol.name!r} has invalid attributes"
                )
            if (
                last_undefined_name is not None
                and symbol.name < last_undefined_name
            ):
                raise NativeObjectError(
                    "undefined symbols are not in canonical name order"
                )
            last_undefined_name = symbol.name
        else:
            if saw_undefined:
                raise NativeObjectError(
                    "defined symbol appears after the undefined partition"
                )
            if not 1 <= symbol.section_index <= len(obj.sections):
                raise NativeObjectError(
                    f"symbol {symbol.name!r} names section "
                    f"{symbol.section_index}"
                )
            if symbol.private_external and not symbol.external:
                raise NativeObjectError(
                    f"private-extern symbol {symbol.name!r} is not external"
                )
            if symbol.external:
                saw_external_definition = True
            elif saw_external_definition:
                raise NativeObjectError(
                    "local symbol appears after the external partition"
                )
            defined_key = (symbol.section_index, symbol.offset)
            previous_key = last_defined_key[symbol.external]
            if previous_key is not None and defined_key < previous_key:
                visibility = "external" if symbol.external else "local"
                raise NativeObjectError(
                    f"{visibility} symbols are not in canonical section order"
                )
            last_defined_key[symbol.external] = defined_key

    section_names: set[tuple[str, str]] = set()
    vm_cursor = 0
    for section in obj.sections:
        if not isinstance(section, NativeSection):
            raise NativeObjectError("native object has a non-section entry")
        _validate_name(section.segname, "segment")
        _validate_name(section.sectname, "section")
        if len(section.segname) > 16 or len(section.sectname) > 16:
            raise NativeObjectError("segment/section name exceeds 16 bytes")
        key = (section.segname, section.sectname)
        if key in section_names:
            raise NativeObjectError(f"native object duplicates section {key!r}")
        section_names.add(key)
        _validate_uint(section.flags, 32, "section flags")
        _validate_uint(section.align_log2, 32, "section alignment")
        _validate_uint(section.zerofill_size, 64, "section zerofill size")
        if not isinstance(section.data, bytes):
            raise NativeObjectError("native section payload must be bytes")
        if not isinstance(section.relocations, tuple):
            raise NativeObjectError("native section relocations must be a tuple")
        if len(section.relocations) > _MAX_COUNT:
            raise NativeObjectError("native section has too many relocations")
        if not isinstance(section.data_in_code, tuple):
            raise NativeObjectError("native data-in-code entries must be a tuple")
        if len(section.data_in_code) > _MAX_COUNT:
            raise NativeObjectError("native section has too many data-in-code entries")
        for region in section.data_in_code:
            if not isinstance(region, DataInCodeRegion):
                raise NativeObjectError("native object has a bad data-in-code entry")

        # Mach-O's section addresses and sizes are uint64. Validate the whole
        # internal layout here so a malformed cache artifact cannot escape as
        # a late struct.pack OverflowError at the external boundary.
        if section.align_log2 > 31:
            raise NativeObjectError("native section alignment exceeds 31")
        vm_cursor = _align_up(vm_cursor, section.align_log2)
        if vm_cursor > _UINT64_MAX - section.vm_size:
            raise NativeObjectError("native object virtual layout exceeds uint64")
        vm_cursor += section.vm_size

        for relocation in section.relocations:
            if not isinstance(relocation, NativeRelocation):
                raise NativeObjectError("native object has a bad relocation entry")
            _validate_uint(relocation.offset, 64, "relocation offset")
            _validate_uint(relocation.type, 32, "relocation type")
            _validate_uint(relocation.length, 8, "relocation length")
            _validate_sint(relocation.addend, 64, "relocation addend")
            if not isinstance(relocation.pcrel, bool):
                raise NativeObjectError("relocation pcrel must be boolean")
            if relocation.target_offset is not None:
                _validate_sint(
                    relocation.target_offset,
                    64,
                    "relocation target offset",
                )
            has_symbol = relocation.symbol_index is not None
            has_section = relocation.target_section_index is not None
            if has_symbol == has_section:
                raise NativeObjectError(
                    "relocation must name exactly one symbol or section"
                )
            if has_symbol:
                _validate_index(
                    relocation.symbol_index,
                    len(obj.symbols),
                    "relocation symbol",
                )
            if has_section:
                _validate_index(
                    relocation.target_section_index,
                    len(obj.sections),
                    "relocation target section",
                    one_based=True,
                )
            if relocation.minuend_index is not None:
                _validate_index(
                    relocation.minuend_index,
                    len(obj.symbols),
                    "relocation minuend symbol",
                )

    # The source-format validators remain authoritative, but their rows are
    # generated on demand instead of retaining a second full relocation graph.
    sections, undefined = obj._source_sections(False)
    _validate_source_sections(
        tuple(sections), tuple(undefined),
        relocation_source=obj._source_relocations,
        relocation_counts=tuple(len(section.relocations) for section in obj.sections),
    )


def _validate_index(
    value: object,
    count: int,
    context: str,
    *,
    one_based: bool = False,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeObjectError(f"{context} index must be an integer")
    lower = 1 if one_based else 0
    upper = count + 1 if one_based else count
    if not lower <= value < upper:
        raise NativeObjectError(f"{context} index {value} is out of range")


def _validate_uint(value: object, bits: int, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeObjectError(f"{context} must be an integer")
    limit = (1 << bits) - 1
    if not 0 <= value <= limit:
        raise NativeObjectError(f"{context} is outside uint{bits} range")


def _validate_sint(value: object, bits: int, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeObjectError(f"{context} must be an integer")
    lower = -(1 << (bits - 1))
    upper = (1 << (bits - 1)) - 1
    if not lower <= value <= upper:
        raise NativeObjectError(f"{context} is outside int{bits} range")


def _validate_name(value: str, context: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or "\0" in value
    ):
        raise NativeObjectError(f"bad {context} name {value!r}")
    if len(value) > _MAX_NAME_BYTES:
        raise NativeObjectError(f"{context} name is unreasonably large")


def _align_up(value: int, align_log2: int) -> int:
    mask = (1 << align_log2) - 1
    return (value + mask) & ~mask


class NativeObjectView:
    """MachOObject-shaped view backed by indexed, already-decoded tables."""

    is_pcc_native_object = True

    def __init__(self, native: NativeObject):
        self.native = native
        addrs: list[int] = []
        vm_cursor = 0
        for section in native.sections:
            vm_cursor = _align_up(vm_cursor, section.align_log2)
            addrs.append(vm_cursor)
            vm_cursor += section.vm_size

        payloads = [bytearray(section.data) for section in native.sections]
        for section_index, section in enumerate(native.sections):
            for relocation in section.relocations:
                if relocation.target_section_index is None:
                    continue
                width = 1 << relocation.length
                start = relocation.offset
                stored = int.from_bytes(
                    payloads[section_index][start:start + width], "little"
                )
                target_index = relocation.target_section_index - 1
                target_addr = addrs[target_index]
                target_size = native.sections[target_index].vm_size
                if relocation.target_offset is not None:
                    if stored:
                        raise NativeObjectError(
                            "section-target relocation with target_offset "
                            "must have a zero-filled field"
                        )
                    target_value = target_addr + relocation.target_offset
                elif target_addr <= stored < target_addr + target_size:
                    target_value = stored
                elif 0 <= stored < target_size:
                    target_value = target_addr + stored
                else:
                    raise NativeObjectError(
                        f"section-target value {stored} is outside section "
                        f"{relocation.target_section_index}"
                    )
                if target_value >= 1 << (width * 8):
                    raise NativeObjectError(
                        "section-target value does not fit relocation width"
                    )
                encoded_target = target_value.to_bytes(width, "little")
                encoded_index = 0
                while encoded_index < width:
                    payloads[section_index][start + encoded_index] = encoded_target[
                        encoded_index
                    ]
                    encoded_index += 1

        data = bytearray()
        self._sections: list[dict] = []
        for index, section in enumerate(native.sections):
            if section.vm_size == section.zerofill_size and section.zerofill_size:
                file_offset = 0
            else:
                file_offset = _align_up(len(data), section.align_log2)
                data.extend(b"\0" * (file_offset - len(data)))
                data.extend(payloads[index])
            self._sections.append({
                "segname_str": section.segname,
                "sectname_str": section.sectname,
                "flags": section.flags,
                "align": section.align_log2,
                "addr": addrs[index],
                "offset": file_offset,
                "size": section.vm_size,
                "nreloc": _raw_relocation_count(section),
                "_pcc_native_index": index,
            })
        self.data = bytes(data)

        self._symbols: list[dict] = []
        for symbol in native.symbols:
            if symbol.section_index:
                n_type = spec.N_SECT
                if symbol.external:
                    n_type |= spec.N_EXT
                if symbol.private_external:
                    n_type |= spec.N_PEXT
                n_value = addrs[symbol.section_index - 1] + symbol.offset
            else:
                n_type = spec.N_UNDF | spec.N_EXT
                n_value = 0
            self._symbols.append({
                "name": symbol.name,
                "n_type": n_type,
                "n_sect": symbol.section_index,
                "n_desc": 0,
                "n_value": n_value,
            })

        # The public Mach-O-shaped API can still materialize a section table,
        # but the executable linker only walks rows. Keep its two passes from
        # retaining millions of dictionary projections beside native records.
        self._relocations: dict[int, list[dict]] = {}
        self._data_in_code: list[dict] = []
        for index, section in enumerate(native.sections):
            for region in section.data_in_code:
                self._data_in_code.append({
                    "offset": addrs[index] + region.offset,
                    "length": region.length,
                    "kind": region.kind,
                })
        self.header = {
            "filetype": spec.MH_OBJECT,
            "cputype": spec.CPU_TYPE_ARM64,
            "cpusubtype": spec.CPU_SUBTYPE_ARM64_ALL,
        }
        self.commands: tuple = ()

    def sections(self) -> list[dict]:
        return self._sections

    def symbols(self) -> list[dict]:
        return self._symbols

    def relocations(self, section: dict) -> list[dict]:
        index = self._relocation_section_index(section)
        if index not in self._relocations:
            self._relocations[index] = _raw_relocations(self.native.sections[index])
        return self._relocations[index]

    def iter_relocations(self, section: dict):
        index = self._relocation_section_index(section)
        if index in self._relocations:
            return iter(self._relocations[index])
        return _iter_raw_relocations(self.native.sections[index])

    def _relocation_section_index(self, section: dict) -> int:
        index = section.get("_pcc_native_index")
        if not isinstance(index, int) or not 0 <= index < len(self.native.sections):
            raise NativeObjectError("section does not belong to this native object")
        return index

    def data_in_code(self) -> list[dict]:
        return self._data_in_code


def _raw_relocation_count(section: NativeSection) -> int:
    return len(section.relocations) + sum(
        1 for relocation in section.relocations if relocation.addend
    ) + sum(
        1 for relocation in section.relocations
        if relocation.type == spec.ARM64_RELOC_SUBTRACTOR
    )


def _raw_relocations(section: NativeSection) -> list[dict]:
    return list(_iter_raw_relocations(section))


def _iter_raw_relocations(section: NativeSection):
    for relocation in sorted(
        section.relocations, key=lambda item: item.offset, reverse=True
    ):
        if relocation.addend:
            yield {
                "r_address": relocation.offset,
                "r_symbolnum": relocation.addend,
                "r_pcrel": 0,
                "r_length": relocation.length,
                "r_extern": 0,
                "r_type": spec.ARM64_RELOC_ADDEND,
            }
        if relocation.type == spec.ARM64_RELOC_SUBTRACTOR:
            assert relocation.symbol_index is not None
            assert relocation.minuend_index is not None
            yield {
                "r_address": relocation.offset,
                "r_symbolnum": relocation.symbol_index,
                "r_pcrel": 0,
                "r_length": relocation.length,
                "r_extern": 1,
                "r_type": spec.ARM64_RELOC_SUBTRACTOR,
            }
            yield {
                "r_address": relocation.offset,
                "r_symbolnum": relocation.minuend_index,
                "r_pcrel": 0,
                "r_length": relocation.length,
                "r_extern": 1,
                "r_type": spec.ARM64_RELOC_UNSIGNED,
            }
            continue
        if relocation.target_section_index is not None:
            yield {
                "r_address": relocation.offset,
                "r_symbolnum": relocation.target_section_index,
                "r_pcrel": 1 if relocation.pcrel else 0,
                "r_length": relocation.length,
                "r_extern": 0,
                "r_type": relocation.type,
            }
            continue
        assert relocation.symbol_index is not None
        yield {
            "r_address": relocation.offset,
            "r_symbolnum": relocation.symbol_index,
            "r_pcrel": 1 if relocation.pcrel else 0,
            "r_length": relocation.length,
            "r_extern": 1,
            "r_type": relocation.type,
        }


def _pack_native_relocation_records(records: CompilerIntArena) -> bytes:
    """Pack one scalar relocation arena without per-record bytes objects.

    The pcc-compiled path owns native arena storage and writes the exact
    ``_RELOCATION`` layout into one raw allocation.  CPython remains the
    semantic/format oracle and uses ``struct.Struct.pack`` because its arena
    deliberately has no raw storage.
    """

    if len(records) % _RELOCATION_SCALAR_COUNT:
        raise NativeObjectError("packed relocation scalar count is malformed")
    record_count = len(records) // _RELOCATION_SCALAR_COUNT
    if not records.uses_native_storage:
        chunks: list[bytes] = []
        record_index = 0
        while record_index < record_count:
            offset = record_index * _RELOCATION_SCALAR_COUNT
            chunks.append(_RELOCATION.pack(
                records.get_unchecked(offset),
                records.get_unchecked(offset + 1),
                records.get_unchecked(offset + 2),
                records.get_unchecked(offset + 3),
                records.get_unchecked(offset + 4),
                records.get_unchecked(offset + 5),
                records.get_unchecked(offset + 6),
                records.get_unchecked(offset + 7),
                records.get_unchecked(offset + 8),
            ))
            record_index += 1
        return b"".join(chunks)

    total = record_count * _RELOCATION.size
    if total == 0:
        return b""
    allocation = malloc(total)
    if ptr_is_null(allocation):
        raise MemoryError("packed relocation allocation failed")
    # The codec has two explicit pad bytes after its byte-width fields.
    memset(allocation, 0, total)
    record_index = 0
    while record_index < record_count:
        source = record_index * _RELOCATION_SCALAR_COUNT
        destination = record_index * _RELOCATION.size
        store_i64(allocation, destination, records.get_unchecked(source))
        store_i32(allocation, destination + 8, records.get_unchecked(source + 1))
        store_i32(allocation, destination + 12, records.get_unchecked(source + 2))
        store_i8(allocation, destination + 16, records.get_unchecked(source + 3))
        store_i8(allocation, destination + 17, records.get_unchecked(source + 4))
        store_i64(allocation, destination + 20, records.get_unchecked(source + 5))
        store_i32(allocation, destination + 28, records.get_unchecked(source + 6))
        store_i32(allocation, destination + 32, records.get_unchecked(source + 7))
        store_i64(allocation, destination + 36, records.get_unchecked(source + 8))
        record_index += 1
    result = _py_bytes_new(allocation, total)
    free(allocation)
    if ptr_is_null(result):
        raise MemoryError("packed relocation bytes allocation failed")
    return result


def encode_native_object(obj: NativeObject) -> bytes:
    """Encode a validated object for cache/subprocess transport."""
    _validate_native_object(obj)
    # Chunks plus one join: pcc's bytearray extend allocates a replacement
    # buffer (PY-P0-BYTEARRAY-INPLACE-IDENTITY-MUTATION), so a per-relocation
    # extend was O(n^2) in a pcc1 worker (24k relocations, multi-GiB churn).
    out: list[bytes] = [_HEADER.pack(MAGIC, len(obj.sections), len(obj.symbols))]
    for symbol in obj.symbols:
        _append_name(out, symbol.name)
        flags = (
            (_SYMBOL_EXTERNAL if symbol.external else 0)
            | (
                _SYMBOL_PRIVATE_EXTERNAL
                if symbol.private_external else 0
            )
        )
        out.append(_SYMBOL.pack(symbol.section_index, symbol.offset, flags))
    for section in obj.sections:
        _append_name(out, section.segname)
        _append_name(out, section.sectname)
        out.append(_SECTION.pack(
            section.flags,
            section.align_log2,
            section.zerofill_size,
            len(section.data),
            len(section.relocations),
            len(section.data_in_code),
        ))
        out.append(section.data)
        for relocation in section.relocations:
            out.append(_RELOCATION.pack(
                relocation.offset,
                _encode_index(relocation.symbol_index),
                relocation.type,
                1 if relocation.pcrel else 0,
                relocation.length,
                relocation.addend,
                _encode_index(relocation.target_section_index),
                _encode_index(relocation.minuend_index),
                (
                    -1 if relocation.target_offset is None
                    else relocation.target_offset
                ),
            ))
        for region in section.data_in_code:
            out.append(_DATA_IN_CODE.pack(
                region.offset, region.length, region.kind,
            ))
    return b"".join(out)


def _source_symbols_in_offset_order(section: Section):
    """Reuse canonical authoring order; sort only an uncommon cold input."""

    previous_offset = -1
    for symbol in section.symbols:
        if symbol.offset < previous_offset:
            return sorted(section.symbols, key=lambda item: item.offset)
        previous_offset = symbol.offset
    return section.symbols


def encode_native_object_from_sections(
    sections: list[Section] | tuple[Section, ...],
    *,
    undefined: list[str] | tuple[str, ...] = (),
) -> bytes:
    """Validate and encode authoring sections without a native-record graph.

    This is not a trusted-worker validation bypass. The existing source model
    is validated first, then the final immutable codec bytes are independently
    accepted by the complete packed-object validator. What is removed is only
    the intermediate ``NativeSymbol``/``NativeSection``/``NativeRelocation``
    materialization and its two conversions back to the same source model.
    """

    source_sections = tuple(sections)
    undefined_names = tuple(undefined)
    _validate_source_sections(source_sections, undefined_names)
    _validate_count(len(source_sections), "section")
    symbol_count = len(undefined_names)
    for section in source_sections:
        symbol_count += len(section.symbols)
        _validate_count(
            len(section.relocations),
            "relocation",
            allow_zero=True,
        )
        _validate_count(
            len(section.data_in_code),
            "data-in-code",
            allow_zero=True,
        )
    _validate_count(symbol_count, "symbol")

    out: list[bytes] = [
        _HEADER.pack(MAGIC, len(source_sections), symbol_count)
    ]
    symbol_index: dict[str, int] = {}
    next_symbol_index = 0
    external = False
    while True:
        section_index = 1
        for section in source_sections:
            for symbol in _source_symbols_in_offset_order(section):
                if symbol.external != external:
                    continue
                if not isinstance(symbol.external, bool) or not isinstance(
                    symbol.private_external,
                    bool,
                ):
                    raise NativeObjectError(
                        "symbol visibility flags must be boolean"
                    )
                _append_name(out, symbol.name)
                flags = (
                    (_SYMBOL_EXTERNAL if symbol.external else 0)
                    | (
                        _SYMBOL_PRIVATE_EXTERNAL
                        if symbol.private_external else 0
                    )
                )
                out.append(_SYMBOL.pack(section_index, symbol.offset, flags))
                symbol_index[symbol.name] = next_symbol_index
                next_symbol_index += 1
            section_index += 1
        if external:
            break
        external = True
    for name in sorted(undefined_names):
        _append_name(out, name)
        out.append(_SYMBOL.pack(0, 0, _SYMBOL_EXTERNAL))
        symbol_index[name] = next_symbol_index
        next_symbol_index += 1
    if next_symbol_index != symbol_count:
        raise NativeObjectError("native symbol count changed during encoding")

    section_index_by_name = {
        (section.segname, section.sectname): index
        for index, section in enumerate(source_sections, start=1)
    }
    for section in source_sections:
        section_data = bytes(section.data)
        _append_name(out, section.segname)
        _append_name(out, section.sectname)
        out.append(_SECTION.pack(
            section.flags,
            section.align_log2,
            section.zerofill_size,
            len(section_data),
            len(section.relocations),
            len(section.data_in_code),
        ))
        out.append(section_data)
        relocation_records = CompilerIntArena(
            len(section.relocations) * _RELOCATION_SCALAR_COUNT
        )
        packed_relocation_path = relocation_records.uses_native_storage
        for relocation in section.relocations:
            is_section_target = relocation.section is not None
            encoded_symbol_index = _encode_index(
                None
                if is_section_target
                else symbol_index[relocation.symbol]
            )
            encoded_target_section_index = _encode_index(
                section_index_by_name[relocation.section]
                if is_section_target else None
            )
            encoded_minuend_index = _encode_index(
                symbol_index[relocation.minuend]
                if relocation.minuend is not None else None
            )
            encoded_target_offset = (
                -1
                if relocation.target_offset is None
                else relocation.target_offset
            )
            if packed_relocation_path:
                relocation_records.append4(
                    relocation.offset,
                    encoded_symbol_index,
                    relocation.type,
                    1 if relocation.pcrel else 0,
                )
                relocation_records.append4(
                    relocation.length,
                    relocation.addend,
                    encoded_target_section_index,
                    encoded_minuend_index,
                )
                relocation_records.append(encoded_target_offset)
            else:
                out.append(_RELOCATION.pack(
                    relocation.offset,
                    encoded_symbol_index,
                    relocation.type,
                    1 if relocation.pcrel else 0,
                    relocation.length,
                    relocation.addend,
                    encoded_target_section_index,
                    encoded_minuend_index,
                    encoded_target_offset,
                ))
        if packed_relocation_path and len(relocation_records):
            out.append(_pack_native_relocation_records(relocation_records))
        relocation_records.close()
        for region in section.data_in_code:
            out.append(_DATA_IN_CODE.pack(
                region.offset,
                region.length,
                region.kind,
            ))
    encoded = b"".join(out)
    # Validate the exact bytes that will cross the cache/process boundary.
    # The packed validator performs the full semantic scan without allocating
    # one NativeRelocation per record.
    decode_packed_native_object(encoded)
    return encoded


def decode_native_object(data: bytes) -> NativeObject:
    """Decode one exact, versioned pcc-native object and reject trailing data."""
    if not isinstance(data, bytes):
        raise NativeObjectError("native object payload must be bytes")
    reader = _Reader(data)
    magic, section_count, symbol_count = reader.unpack(_HEADER)
    if magic != MAGIC:
        raise NativeObjectError("bad pcc-native object magic/version")
    _validate_count(section_count, "section")
    _validate_count(symbol_count, "symbol")

    symbols: list[NativeSymbol] = []
    for _index in range(symbol_count):
        name = reader.name("symbol")
        section_index, offset, flags = reader.unpack(_SYMBOL)
        if flags & ~(_SYMBOL_EXTERNAL | _SYMBOL_PRIVATE_EXTERNAL):
            raise NativeObjectError("native symbol has unknown flag bits")
        symbols.append(NativeSymbol(
            name=name,
            section_index=section_index,
            offset=offset,
            external=bool(flags & _SYMBOL_EXTERNAL),
            private_external=bool(flags & _SYMBOL_PRIVATE_EXTERNAL),
        ))

    sections: list[NativeSection] = []
    for _index in range(section_count):
        segname = reader.name("segment")
        sectname = reader.name("section")
        (
            flags,
            align_log2,
            zerofill_size,
            data_size,
            relocation_count,
            data_in_code_count,
        ) = reader.unpack(_SECTION)
        _validate_count(relocation_count, "relocation", allow_zero=True)
        _validate_count(data_in_code_count, "data-in-code", allow_zero=True)
        section_data = reader.take(data_size)
        relocations: list[NativeRelocation] = []
        for _relocation_index in range(relocation_count):
            (
                offset,
                symbol_index,
                relocation_type,
                pcrel,
                length,
                addend,
                target_section_index,
                minuend_index,
                target_offset,
            ) = reader.unpack(_RELOCATION)
            if pcrel not in (0, 1):
                raise NativeObjectError("relocation pcrel byte is not boolean")
            relocations.append(NativeRelocation(
                offset=offset,
                symbol_index=_decode_index(symbol_index),
                type=relocation_type,
                pcrel=bool(pcrel),
                length=length,
                addend=addend,
                target_section_index=_decode_index(target_section_index),
                minuend_index=_decode_index(minuend_index),
                target_offset=None if target_offset == -1 else target_offset,
            ))
        regions: list[DataInCodeRegion] = []
        for _region_index in range(data_in_code_count):
            offset, length, kind = reader.unpack(_DATA_IN_CODE)
            regions.append(DataInCodeRegion(offset, length, kind))
        sections.append(NativeSection(
            segname=segname,
            sectname=sectname,
            flags=flags,
            align_log2=align_log2,
            data=section_data,
            relocations=tuple(relocations),
            zerofill_size=zerofill_size,
            data_in_code=tuple(regions),
        ))
    if not reader.at_end:
        raise NativeObjectError("trailing bytes after pcc-native object")
    return NativeObject(tuple(sections), tuple(symbols))


def decode_packed_native_object(data: bytes) -> PackedNativeObject:
    """Validate encoded bytes without materialising input relocations."""

    if not isinstance(data, bytes):
        raise NativeObjectError("native object payload must be bytes")
    reader = _SpanReader(data)
    magic, section_count, symbol_count = reader.unpack(_HEADER)
    if magic != MAGIC:
        raise NativeObjectError("bad pcc-native object magic/version")
    _validate_count(section_count, "section")
    _validate_count(symbol_count, "symbol")

    symbols: list[PackedNativeSymbol] = []
    for _index in range(symbol_count):
        name = reader.name("symbol")
        section_index, offset, flags = reader.unpack(_SYMBOL)
        if flags & ~(_SYMBOL_EXTERNAL | _SYMBOL_PRIVATE_EXTERNAL):
            raise NativeObjectError("native symbol has unknown flag bits")
        symbols.append(PackedNativeSymbol(name, section_index, offset, flags))

    sections: list[PackedNativeSection] = []
    for _index in range(section_count):
        segname = reader.name("segment")
        sectname = reader.name("section")
        (
            flags,
            align_log2,
            zerofill_size,
            data_size,
            relocation_count,
            data_in_code_count,
        ) = reader.unpack(_SECTION)
        _validate_count(relocation_count, "relocation", allow_zero=True)
        _validate_count(data_in_code_count, "data-in-code", allow_zero=True)
        data_offset = reader.skip(data_size)
        relocation_offset = reader.offset
        previous_offset: int | None = None
        ascending = True
        descending = True
        for _relocation_index in range(relocation_count):
            fields = reader.unpack(_RELOCATION)
            relocation_offset_value = fields[0]
            if previous_offset is not None:
                ascending = ascending and previous_offset <= relocation_offset_value
                descending = descending and previous_offset >= relocation_offset_value
            previous_offset = relocation_offset_value
        if descending:
            relocation_order = -1
        elif ascending:
            relocation_order = 1
        else:
            relocation_order = 0
        regions: list[tuple[int, int, int]] = []
        for _region_index in range(data_in_code_count):
            regions.append(reader.unpack(_DATA_IN_CODE))
        sections.append(PackedNativeSection(
            segname=segname,
            sectname=sectname,
            flags=flags,
            align_log2=align_log2,
            zerofill_size=zerofill_size,
            data_offset=data_offset,
            data_size=data_size,
            relocation_offset=relocation_offset,
            relocation_count=relocation_count,
            data_in_code=tuple(regions),
            relocation_order=relocation_order,
        ))
    if not reader.at_end:
        raise NativeObjectError("trailing bytes after pcc-native object")
    packed = PackedNativeObject(data, tuple(sections), tuple(symbols))
    packed.relocation_target_indices = _validate_packed_native_object(packed)
    return packed


def _packed_relocation_indices(fields):
    return (
        _decode_index(fields[1]),
        _decode_index(fields[6]),
        _decode_index(fields[7]),
    )


def _packed_relocations_in_storage_order(
    packed: PackedNativeObject,
    section: PackedNativeSection,
):
    for index in range(section.relocation_count):
        yield _packed_relocation_fields_at(packed, section, index)


def _packed_relocation_fields_at(
    packed: PackedNativeObject,
    section: PackedNativeSection,
    index: int,
):
    return _RELOCATION.unpack_from(
        packed.encoded,
        section.relocation_offset + index * _RELOCATION.size,
    )


def _packed_is_zerofill(section: PackedNativeSection) -> bool:
    return (section.flags & spec.SECTION_TYPE) in (spec.S_ZEROFILL, 0x12)


def _packed_contains_inline_data(
    section: PackedNativeSection,
    offset: int,
    width: int,
) -> bool:
    return any(
        region_offset <= offset
        and offset + width <= region_offset + region_length
        for region_offset, region_length, _kind in section.data_in_code
    )


def _validate_packed_native_object(
    packed: PackedNativeObject,
) -> frozenset[int]:
    sections = packed.sections
    symbols = packed.symbols
    seen_names: set[str] = set()
    saw_external_definition = False
    saw_undefined = False
    last_defined_key: dict[bool, tuple[int, int] | None] = {
        False: None,
        True: None,
    }
    last_undefined_name: str | None = None
    defined_count = 0
    symbols_by_section: list[list[PackedNativeSymbol]] = [
        [] for _section in sections
    ]
    for symbol in symbols:
        _validate_name(symbol.name, "symbol")
        if symbol.name in seen_names:
            raise NativeObjectError(
                f"native object duplicates symbol {symbol.name!r}"
            )
        seen_names.add(symbol.name)
        if not 0 <= symbol.offset <= _UINT64_MAX:
            raise NativeObjectError("symbol offset is outside uint64 range")
        if symbol.section_index == 0:
            saw_undefined = True
            if (
                not symbol.external
                or symbol.private_external
                or symbol.offset != 0
            ):
                raise NativeObjectError(
                    f"undefined symbol {symbol.name!r} has invalid attributes"
                )
            if (
                last_undefined_name is not None
                and symbol.name < last_undefined_name
            ):
                raise NativeObjectError(
                    "undefined symbols are not in canonical name order"
                )
            last_undefined_name = symbol.name
            continue
        defined_count += 1
        if saw_undefined:
            raise NativeObjectError(
                "defined symbol appears after the undefined partition"
            )
        if not 1 <= symbol.section_index <= len(sections):
            raise NativeObjectError(
                f"symbol {symbol.name!r} names section {symbol.section_index}"
            )
        if symbol.private_external and not symbol.external:
            raise NativeObjectError(
                f"private-extern symbol {symbol.name!r} is not external"
            )
        if symbol.external:
            saw_external_definition = True
        elif saw_external_definition:
            raise NativeObjectError(
                "local symbol appears after the external partition"
            )
        defined_key = (symbol.section_index, symbol.offset)
        previous_key = last_defined_key[symbol.external]
        if previous_key is not None and defined_key < previous_key:
            visibility = "external" if symbol.external else "local"
            raise NativeObjectError(
                f"{visibility} symbols are not in canonical section order"
            )
        last_defined_key[symbol.external] = defined_key
        symbols_by_section[symbol.section_index - 1].append(symbol)
    if not defined_count:
        raise NativeObjectError("at least one defined symbol is required")

    section_names: set[tuple[str, str]] = set()
    seen_zerofill_segments: set[str] = set()
    vm_cursor = 0
    targeted_symbols: set[int] = set()
    for section_index, section in enumerate(sections):
        _validate_name(section.segname, "segment")
        _validate_name(section.sectname, "section")
        if len(section.segname) > 16 or len(section.sectname) > 16:
            raise NativeObjectError("segment/section name exceeds 16 bytes")
        key = (section.segname, section.sectname)
        if key in section_names:
            raise NativeObjectError(f"native object duplicates section {key!r}")
        section_names.add(key)
        if section.align_log2 > 31:
            raise NativeObjectError("native section alignment exceeds 31")
        vm_cursor = _align_up(vm_cursor, section.align_log2)
        if vm_cursor > _UINT64_MAX - section.vm_size:
            raise NativeObjectError("native object virtual layout exceeds uint64")
        vm_cursor += section.vm_size

        is_zerofill = _packed_is_zerofill(section)
        if is_zerofill:
            seen_zerofill_segments.add(section.segname)
            if section.data_size:
                raise NativeObjectError(
                    f"zerofill section {section.sectname} must not carry file payload"
                )
            if section.zerofill_size <= 0:
                raise NativeObjectError(
                    f"zerofill section {section.sectname} needs a positive size"
                )
            if section.relocation_count:
                raise NativeObjectError(
                    f"zerofill section {section.sectname} cannot have relocations"
                )
            if section.data_in_code:
                raise NativeObjectError(
                    f"zerofill section {section.sectname} cannot contain data-in-code"
                )
        else:
            if section.segname in seen_zerofill_segments:
                raise NativeObjectError(
                    f"content section {section.sectname} after a zerofill section"
                )
            if section.data_size == 0:
                raise NativeObjectError(
                    f"empty section {section.segname},{section.sectname}"
                )
            if section.zerofill_size:
                raise NativeObjectError(
                    f"{section.sectname}: zerofill_size on a non-zerofill section"
                )

        section_type = section.flags & spec.SECTION_TYPE
        section_symbols = symbols_by_section[section_index]
        if section.flags & spec.S_ATTR_DEBUG:
            if section_type != spec.S_REGULAR or section_symbols:
                raise NativeObjectError(
                    "S_ATTR_DEBUG sections must be regular and contain no symbols"
                )
        _validate_packed_data_in_code(section)
        for symbol in section_symbols:
            if not 0 <= symbol.offset < section.vm_size:
                raise NativeObjectError(
                    f"symbol {symbol.name!r} offset {symbol.offset} outside "
                    f"{section.sectname}"
                )
            if (
                section.flags & spec.S_ATTR_PURE_INSTRUCTIONS
                and symbol.offset % 4 != 0
                and not _packed_contains_inline_data(section, symbol.offset, 1)
            ):
                raise NativeObjectError(
                    f"symbol {symbol.name!r} offset {symbol.offset} is not "
                    "instruction-aligned"
                )
        targeted_symbols.update(
            _validate_packed_relocations(packed, section_index)
        )
        _validate_packed_special_section(packed, section_index)
    return frozenset(targeted_symbols)


def _validate_packed_data_in_code(section: PackedNativeSection) -> None:
    last_region_end = 0
    is_code = bool(section.flags & spec.S_ATTR_PURE_INSTRUCTIONS)
    for offset, length, kind in section.data_in_code:
        if not is_code:
            raise NativeObjectError(
                "data-in-code regions are only valid in instruction sections"
            )
        if kind not in _DATA_IN_CODE_KINDS:
            raise NativeObjectError(f"unrecognised data-in-code kind {kind}")
        if not 0 < length <= 0xFFFF:
            raise NativeObjectError(
                f"data-in-code length {length} outside uint16 range"
            )
        unit = _DATA_IN_CODE_UNITS[kind]
        if offset % unit != 0 or length % unit != 0:
            raise NativeObjectError(
                f"data-in-code kind {kind} needs {unit}-byte units"
            )
        if offset < last_region_end:
            raise NativeObjectError(
                "data-in-code regions must be ordered and non-overlapping"
            )
        instruction_gap = offset - last_region_end
        if instruction_gap and (
            last_region_end % 4 != 0 or instruction_gap % 4 != 0
        ):
            raise NativeObjectError(
                "bytes outside data-in-code regions must be whole aligned "
                "arm64 instructions"
            )
        if not 0 <= offset <= section.data_size - length:
            raise NativeObjectError(
                f"data-in-code range at {offset} outside {section.sectname}"
            )
        last_region_end = offset + length
    if is_code:
        trailing = section.data_size - last_region_end
        if trailing and (last_region_end % 4 != 0 or trailing % 4 != 0):
            raise NativeObjectError(
                "bytes outside data-in-code regions must be whole aligned "
                "arm64 instructions"
            )


def _validate_packed_relocations(
    packed: PackedNativeObject,
    section_index: int,
) -> set[int]:
    section = packed.sections[section_index]
    seen_offsets: set[int] = set()
    targeted_symbols: set[int] = set()
    for fields in _packed_relocations_in_storage_order(packed, section):
        (
            offset,
            _symbol_raw,
            relocation_type,
            pcrel,
            length,
            addend,
            _target_section_raw,
            _minuend_raw,
            target_offset_raw,
        ) = fields
        symbol_index, target_section_index, minuend_index = (
            _packed_relocation_indices(fields)
        )
        if pcrel not in (0, 1):
            raise NativeObjectError("relocation pcrel byte is not boolean")
        has_symbol = symbol_index is not None
        has_section = target_section_index is not None
        if has_symbol == has_section:
            raise NativeObjectError(
                "relocation must name exactly one symbol or section"
            )
        if symbol_index is not None and not 0 <= symbol_index < len(packed.symbols):
            raise NativeObjectError(
                f"relocation symbol index {symbol_index} is out of range"
            )
        if symbol_index is not None:
            targeted_symbols.add(symbol_index)
        if target_section_index is not None and not (
            1 <= target_section_index <= len(packed.sections)
        ):
            raise NativeObjectError(
                f"relocation target section index {target_section_index} is out of range"
            )
        if minuend_index is not None and not 0 <= minuend_index < len(packed.symbols):
            raise NativeObjectError(
                f"relocation minuend symbol index {minuend_index} is out of range"
            )
        if minuend_index is not None:
            targeted_symbols.add(minuend_index)
        shape = _PROVEN_RELOCATION_SHAPES.get(relocation_type)
        if shape is None:
            raise NativeObjectError(
                f"relocation type {relocation_type} not differentially proven yet"
            )
        if (bool(pcrel), length) not in shape["forms"]:
            raise NativeObjectError(
                f"relocation at {offset}: type {relocation_type} has an "
                "invalid pcrel/length shape"
            )
        if addend:
            if not shape["addend"] or not 0 < addend <= _ADDEND_MAX:
                raise NativeObjectError(
                    f"relocation at {offset}: invalid addend {addend}"
                )
        if relocation_type == spec.ARM64_RELOC_SUBTRACTOR:
            if target_section_index is not None or minuend_index is None:
                raise NativeObjectError(
                    "SUBTRACTOR requires extern subtrahend and minuend symbols"
                )
            assert symbol_index is not None
            if packed.symbols[symbol_index].name == packed.symbols[minuend_index].name:
                raise NativeObjectError(
                    "SUBTRACTOR minuend and subtrahend must be distinct"
                )
        elif minuend_index is not None:
            raise NativeObjectError(
                "minuend is only valid on an ARM64_RELOC_SUBTRACTOR pair"
            )
        target_offset = None if target_offset_raw == -1 else target_offset_raw
        if target_section_index is not None:
            if relocation_type != spec.ARM64_RELOC_UNSIGNED:
                raise NativeObjectError(
                    "only UNSIGNED section-target relocations are proven"
                )
            target = packed.sections[target_section_index - 1]
            if target_offset is not None and not 0 <= target_offset < target.vm_size:
                raise NativeObjectError(
                    f"section-target offset {target_offset} outside "
                    f"{target.segname},{target.sectname}"
                )
            target_type = target.flags & spec.SECTION_TYPE
            if target_type == spec.S_CSTRING_LITERALS or (
                target.segname == "__DATA"
                and target.sectname in ("__cfstring", "__objc_classrefs")
            ):
                raise NativeObjectError(
                    "section-target relocation to linker-special section "
                    f"{target.segname},{target.sectname} is not proven"
                )
        elif target_offset is not None:
            raise NativeObjectError(
                "target_offset is only valid on a section-target relocation"
            )
        width = 1 << length
        if not 0 <= offset <= section.data_size - width:
            raise NativeObjectError(
                f"relocation offset {offset} outside {section.sectname}"
            )
        if offset > 0x7FFFFFFF:
            raise NativeObjectError(
                f"relocation offset {offset} exceeds signed r_address range"
            )
        if (
            section.flags & spec.S_ATTR_PURE_INSTRUCTIONS
            and offset % 4 != 0
            and not _packed_contains_inline_data(section, offset, width)
        ):
            raise NativeObjectError(
                f"relocation offset {offset} is not instruction-aligned in "
                f"{section.sectname}"
            )
        if offset in seen_offsets:
            raise NativeObjectError(
                f"multiple relocation requests at offset {offset} in "
                f"{section.segname},{section.sectname}"
            )
        seen_offsets.add(offset)
    return targeted_symbols


def _validate_packed_special_section(
    packed: PackedNativeObject,
    section_index: int,
) -> None:
    from .precise_stackmap import PreciseStackMapError, _scan_stack_map_payload

    section = packed.sections[section_index]
    key = (section.segname, section.sectname)
    section_type = section.flags & spec.SECTION_TYPE
    special = (
        section_type == spec.S_MOD_INIT_FUNC_POINTERS
        or key == ("__DATA", "__mod_init_func")
        or key == ("__LD", "__compact_unwind")
        or key == ("__TEXT", "__eh_frame")
        or section.sectname == "__pcc_stackmaps"
    )
    if not special:
        return
    if section_type == spec.S_MOD_INIT_FUNC_POINTERS:
        if key != ("__DATA", "__mod_init_func"):
            raise NativeObjectError(
                "mod-init pointer section must be __DATA,__mod_init_func"
            )
        if section.flags != MOD_INIT_SECTION_FLAGS:
            raise NativeObjectError(
                "__mod_init_func must use S_MOD_INIT_FUNC_POINTERS only"
            )
        if section.align_log2 < 3 or section.data_size % 8 != 0:
            raise NativeObjectError(
                "__mod_init_func must be pointer-aligned whole 64-bit slots"
            )
        pointer_offsets: set[int] = set()
        relocation_index = 0
        while relocation_index < section.relocation_count:
            entry = _packed_relocation_fields_at(
                packed,
                section,
                relocation_index,
            )
            if (
                entry[2] == spec.ARM64_RELOC_UNSIGNED
                and not entry[3]
                and entry[4] == 3
            ):
                pointer_offsets.add(entry[0])
            relocation_index += 1
        expected = set(range(0, section.data_size, 8))
        if (
            pointer_offsets != expected
            or section.relocation_count != len(expected)
        ):
            raise NativeObjectError(
                "every __mod_init_func slot needs exactly one 64-bit "
                "UNSIGNED relocation"
            )
    elif key == ("__DATA", "__mod_init_func"):
        raise NativeObjectError(
            "__DATA,__mod_init_func needs S_MOD_INIT_FUNC_POINTERS"
        )

    if key == ("__LD", "__compact_unwind"):
        if section.flags != COMPACT_UNWIND_SECTION_FLAGS:
            raise NativeObjectError(
                "__compact_unwind must be S_REGULAR|S_ATTR_DEBUG"
            )
        if section.align_log2 < 3 or section.data_size % 32 != 0:
            raise NativeObjectError(
                "arm64 __compact_unwind must contain aligned 32-byte rows"
            )
        function_offsets: set[int] = set()
        relocation_index = 0
        while relocation_index < section.relocation_count:
            entry = _packed_relocation_fields_at(
                packed,
                section,
                relocation_index,
            )
            if (
                entry[2] == spec.ARM64_RELOC_UNSIGNED
                and not entry[3]
                and entry[4] == 3
            ):
                function_offsets.add(entry[0])
            relocation_index += 1
        if not set(range(0, section.data_size, 32)).issubset(function_offsets):
            raise NativeObjectError(
                "every __compact_unwind row needs a function-address "
                "UNSIGNED relocation"
            )
    elif key == ("__TEXT", "__eh_frame"):
        if section.flags != EH_FRAME_SECTION_FLAGS:
            raise NativeObjectError(
                "__eh_frame has the wrong coalesced/live-support flags"
            )
        if section.align_log2 < 3:
            raise NativeObjectError("__eh_frame must be at least 8-byte aligned")

    if key == ("__DATA", "__pcc_stackmaps"):
        if section.flags != PCC_STACKMAP_SECTION_FLAGS:
            raise NativeObjectError("__pcc_stackmaps must be a regular section")
        if section.align_log2 < 3:
            raise NativeObjectError("__pcc_stackmaps must be 8-byte aligned")
        if any(
            symbol.section_index == section_index + 1
            for symbol in packed.symbols
        ):
            raise NativeObjectError(
                "__pcc_stackmaps cannot define data symbols"
            )
        try:
            _count, scanned, _table_start, _table_count = _scan_stack_map_payload(
                packed.section_data(section_index)
            )
        except PreciseStackMapError as exc:
            raise NativeObjectError(
                f"invalid __pcc_stackmaps payload: {exc}"
            ) from exc
        relocation_by_offset: dict[int, int] = {}
        relocation_index = 0
        while relocation_index < section.relocation_count:
            entry = _packed_relocation_fields_at(
                packed,
                section,
                relocation_index,
            )
            relocation_by_offset[entry[0]] = relocation_index
            relocation_index += 1
        address_offsets = [start + 8 for _fid, start, _end in scanned]
        missing_address_offset = False
        for offset in address_offsets:
            if offset not in relocation_by_offset:
                missing_address_offset = True
                break
        if (
            len(relocation_by_offset) != section.relocation_count
            or len(relocation_by_offset) != len(address_offsets)
            or missing_address_offset
        ):
            raise NativeObjectError(
                "every stack-map function address needs exactly one relocation"
            )
        payload = packed.section_data(section_index)
        for offset in address_offsets:
            entry = _packed_relocation_fields_at(
                packed,
                section,
                relocation_by_offset[offset],
            )
            symbol_index, target_section_index, minuend_index = (
                _packed_relocation_indices(entry)
            )
            if (
                entry[2] != spec.ARM64_RELOC_UNSIGNED
                or entry[3]
                or entry[4] != 3
                or target_section_index is not None
                or minuend_index is not None
                or entry[5]
                or symbol_index is None
            ):
                raise NativeObjectError(
                    "stack-map function address needs a plain 64-bit "
                    "UNSIGNED symbol relocation"
                )
            if bytes(payload[offset:offset + 8]) != b"\0" * 8:
                raise NativeObjectError(
                    "relocatable stack-map function address must be zero"
                )
    elif section.sectname == "__pcc_stackmaps":
        raise NativeObjectError("__pcc_stackmaps must live in __DATA")


def is_native_object_bytes(data: object) -> bool:
    return isinstance(data, bytes) and data.startswith(MAGIC)


def _append_name(out: list[bytes], value: str) -> None:
    _validate_name(value, "object")
    raw = value.encode()
    out.append(_U32.pack(len(raw)))
    out.append(raw)


def _encode_index(value: int | None) -> int:
    if value is None:
        return _NONE_INDEX
    if not isinstance(value, int) or isinstance(value, bool):
        raise NativeObjectError("native object index must be an integer")
    if not 0 <= value < _NONE_INDEX:
        raise NativeObjectError("native object index exceeds uint32")
    return value


def _decode_index(value: int) -> int | None:
    return None if value == _NONE_INDEX else value


def _validate_count(value: int, context: str, *, allow_zero: bool = False) -> None:
    lower = 0 if allow_zero else 1
    if not lower <= value <= _MAX_COUNT:
        raise NativeObjectError(f"native object has invalid {context} count")


def _decode_ascii_name(raw: bytes, context: str) -> str:
    """Decode the wire's ASCII subset through pcc's UTF-8 codec.

    pcc-native ``bytes.decode`` deliberately owns UTF-8 today. ASCII is a
    strict subset, so reject every high byte before using the supported codec;
    this preserves the wire's exact non-ASCII diagnostic without requiring a
    second runtime codec in every compiled worker.
    """

    for byte in raw:
        if byte >= 0x80:
            raise NativeObjectError(f"non-ASCII {context} name")
    try:
        return raw.decode()
    except UnicodeDecodeError as exc:
        # The precheck makes this unreachable for a conforming UTF-8 decoder,
        # but keep the public decoder fail-closed if its implementation drifts.
        raise NativeObjectError(f"non-ASCII {context} name") from exc


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        if not isinstance(size, int) or size < 0:
            raise NativeObjectError("native object field has invalid size")
        end = self.offset + size
        if end < self.offset or end > len(self.data):
            raise NativeObjectError("truncated pcc-native object")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def unpack(self, layout: struct.Struct):
        raw = self.take(layout.size)
        return layout.unpack(raw)

    def name(self, context: str) -> str:
        length, = self.unpack(_U32)
        if not 0 < length <= _MAX_NAME_BYTES:
            raise NativeObjectError(f"invalid {context} name length")
        raw = self.take(length)
        value = _decode_ascii_name(raw, context)
        _validate_name(value, context)
        return value

    @property
    def at_end(self) -> bool:
        return self.offset == len(self.data)


class _SpanReader:
    """Bounds-checked reader that records payload spans instead of copying."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def skip(self, size: int) -> int:
        if not isinstance(size, int) or size < 0:
            raise NativeObjectError("native object field has invalid size")
        start = self.offset
        end = start + size
        if end < start or end > len(self.data):
            raise NativeObjectError("truncated pcc-native object")
        self.offset = end
        return start

    def unpack(self, layout: struct.Struct):
        start = self.skip(layout.size)
        return layout.unpack_from(self.data, start)

    def name(self, context: str) -> str:
        length, = self.unpack(_U32)
        if not 0 < length <= _MAX_NAME_BYTES:
            raise NativeObjectError(f"invalid {context} name length")
        start = self.skip(length)
        value = _decode_ascii_name(
            self.data[start:start + length],
            context,
        )
        _validate_name(value, context)
        return value

    @property
    def at_end(self) -> bool:
        return self.offset == len(self.data)


__all__ = [
    "MAGIC",
    "NativeObject",
    "NativeObjectError",
    "NativeObjectView",
    "PackedNativeObject",
    "PackedNativeSection",
    "PackedNativeSymbol",
    "NativeRelocation",
    "NativeSection",
    "NativeSymbol",
    "decode_native_object",
    "decode_packed_native_object",
    "encode_native_object",
    "encode_native_object_from_sections",
    "is_native_object_bytes",
]
