"""Directive-level driver: a full self-backend .s file -> macho_obj sections.

LINK-P1-MACHO-OBJ-SWITCH, the layer above `arm64_encode`: the instruction
encoder covers the emitter's whole vocabulary, and the object writer covers
the section set — this module parses the emitter's *file* dialect (directives,
labels, and data items) and produces the `Section` list that
`NativeObject.from_sections` consumes inside pcc, or that
`macho_obj.emit_object` materialises at an external Mach-O boundary. With it,
a self-backend .s reaches either representation with no system assembler in
the path.

The dialect is the self backend's, not gas: `.section seg,sect[,attrs]`,
`.p2align`, symbol visibility, scalar data items (a `.quad` may name a symbol
± offset, becoming an UNSIGNED relocation), data-in-code regions, labels,
and `.subsections_via_symbols`. Anything else fails closed — the emitter
gaining a directive must extend this driver and its differential test, not
get silently mis-assembled.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import macho_spec as spec
from .arm64_encode import EncodeError, PackedAArch64TextBuilder, validate_emitted_label_name
from .macho_obj import (
    COMPACT_UNWIND_SECTION_FLAGS,
    CSTRING_SECTION_FLAGS,
    DATA_SECTION_FLAGS,
    EH_FRAME_SECTION_FLAGS,
    MOD_INIT_SECTION_FLAGS,
    PCC_STACKMAP_SECTION_FLAGS,
    Relocation,
    Section,
    TextSymbol,
    TEXT_SECTION_FLAGS,
)
from .self_backend_value_arena import CompilerIntArena

_SECTION_FLAGS = {
    ("__TEXT", "__text", "regular,pure_instructions"): TEXT_SECTION_FLAGS,
    ("__TEXT", "__cstring", "cstring_literals"): CSTRING_SECTION_FLAGS,
    ("__TEXT", "__const", ""): DATA_SECTION_FLAGS,
    ("__DATA", "__data", ""): DATA_SECTION_FLAGS,
    ("__DATA", "__const", ""): DATA_SECTION_FLAGS,
    ("__DATA", "__thread_data", "thread_local_regular"):
        spec.S_THREAD_LOCAL_REGULAR,
    ("__DATA", "__thread_vars", "thread_local_variables"):
        spec.S_THREAD_LOCAL_VARIABLES,
    ("__DATA", "__mod_init_func", "mod_init_funcs"):
        MOD_INIT_SECTION_FLAGS,
    ("__DATA", "__pcc_stackmaps", "regular"):
        PCC_STACKMAP_SECTION_FLAGS,
    # ELF-only symbol metadata carried through the owned section transport.
    # The ELF adapter consumes it; no TLS storage is allocated by this section.
    ("__PCC", "__tls_refs", ""): DATA_SECTION_FLAGS,
    ("__LD", "__compact_unwind", "regular,debug"):
        COMPACT_UNWIND_SECTION_FLAGS,
    ("__DWARF", "__debug_abbrev", "regular,debug"): spec.S_ATTR_DEBUG,
    ("__DWARF", "__debug_info", "regular,debug"): spec.S_ATTR_DEBUG,
    ("__DWARF", "__debug_line", "regular,debug"): spec.S_ATTR_DEBUG,
    ("__TEXT", "__eh_frame",
     "coalesced,no_toc+strip_static_syms+live_support"):
        EH_FRAME_SECTION_FLAGS,
}


@dataclass
class _SectionBuffer:
    segname: str
    sectname: str
    flags: int
    align_log2: int = 0
    # Data sections accumulate chunks and join once: pcc's bytearray +=
    # allocates a replacement buffer (PY-P0-BYTEARRAY-INPLACE-IDENTITY-
    # MUTATION), so per-item appends were O(n^2) under pcc1.
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    # Keep one native text builder across repeated .section declarations so
    # branches can cross them without retaining a second instruction-line list.
    text_builder: PackedAArch64TextBuilder | None = None
    # Text validation historically followed complete directive validation.
    # Retain only its first failure until section finalization, never its input.
    text_error: EncodeError | None = None
    symbols: list[TextSymbol] = field(default_factory=list)
    relocations: list[Relocation] = field(default_factory=list)

    @property
    def is_text(self) -> bool:
        return bool(self.flags & spec.S_ATTR_PURE_INSTRUCTIONS)

    def append(self, raw: bytes) -> None:
        self.chunks.append(raw)
        self.size += len(raw)

    def append_text_line(self, line: str) -> None:
        if self.text_error is not None:
            return
        assert self.text_builder is not None
        try:
            self.text_builder.append_line(line)
        except EncodeError as exc:
            self.text_error = exc


@dataclass(frozen=True, slots=True)
class StructuredAArch64Module:
    """Incremental structured-emitter transport consumed by this driver.

    ``line_chunks`` is the exact compatibility/oracle lane for instruction and
    directive families not migrated yet. ``structured_sections`` carries
    final payload/relocation families and must never duplicate a line-owned
    section.  One shell is created per module; records stay in their packed
    arenas or final byte payloads rather than becoming transport objects.
    """

    line_chunks: list[str]
    structured_sections: tuple[Section, ...] = ()
    encoded_line_records: CompilerIntArena | None = None
    structured_symbol_names: tuple[str, ...] = ()
    structured_instruction_count: int = 0
    fallback_instruction_count: int = 0
    structured_unscaled_count: int = 0
    structured_move_count: int = 0
    structured_call_count: int = 0
    direct_instruction_count: int = 0
    native_finalized: bool = False
    native_undefined: tuple[str, ...] = ()
    # Counted residual encoder adapter. Normal supported PCO inputs must keep
    # this empty; it is separate from direct and transient text-encoded words.
    fallback_instruction_lines: tuple[str, ...] = ()
    # Explicit word/label records replayed through native fragment handles.
    native_fragment_record_count: int = 0

    def assemble_sections(self) -> tuple[list[Section], list[str]]:
        if self.native_finalized:
            return list(self.structured_sections), list(self.native_undefined)
        return assemble_lines(
            self.line_chunks, self.structured_sections,
            self.encoded_line_records, self.structured_symbol_names,
        )


def _parse_int(tok: str) -> int | None:
    t = tok.strip()
    try:
        return int(t, 0)
    except ValueError:
        return None


def _split_data_items(rest: str) -> list[str]:
    return [item.strip() for item in rest.split(",") if item.strip()]


class AArch64ModuleBuilder:
    """One directive/section owner shared by streaming emission and text APIs.

    Each text section owns a packed text builder; data/symbol/section tables
    retain their named phase lifetime. No module instruction list is retained.
    """

    def __init__(
        self, structured_symbol_names: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.buffers: dict[tuple[str, str], _SectionBuffer] = {}
        self.order: list[tuple[str, str]] = []
        self.globl: set[str] = set()
        self.private_extern: set[str] = set()
        self.current: _SectionBuffer | None = None
        self.structured_symbol_names = structured_symbol_names
        self.text_layout_finalized: bool = False
        self.closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        for buf in self.buffers.values():
            if buf.text_builder is not None:
                buf.text_builder.close()
        self.closed = True

    def append_encoded(self, word: int, relocation_kind: int, symbol_id: int) -> None:
        if self.closed:
            raise EncodeError("module builder is closed")
        current = self.current
        if current is None or not current.is_text:
            self.close()
            raise EncodeError("structured instruction outside __TEXT,__text")
        if current.text_error is None:
            assert current.text_builder is not None
            try:
                current.text_builder.append_encoded(word, relocation_kind, symbol_id)
            except EncodeError as exc:
                current.text_error = exc
            except Exception:
                self.close()
                raise

    def append_chunk(self, raw: str) -> None:
        if self.closed:
            raise EncodeError("module builder is closed")
        try:
            if "\n" in raw or "\r" in raw:
                for line in raw.splitlines():
                    self._append_line(line)
            else:
                self._append_line(raw)
        except Exception:
            self.close()
            raise

    def append_label(self, name: str) -> None:
        if self.closed:
            raise EncodeError("module builder is closed")
        try:
            validate_emitted_label_name(name)
            self._define_label(name)
        except Exception:
            self.close()
            raise

    def _define_label(self, name: str) -> None:
        current = self.current
        if current is None:
            raise EncodeError(f"label {name!r} before any .section")
        if current.is_text:
            if current.text_error is None:
                assert current.text_builder is not None
                try:
                    current.text_builder.append_label(name)
                except EncodeError as exc:
                    current.text_error = exc
            return
        offset: int = current.size
        current.symbols.append(TextSymbol(
            name, offset,
            external=(name in self.globl or name in self.private_extern),
            private_external=(name in self.private_extern),
        ))

    def finalize_text_layout(self) -> None:
        """Freeze every text section before serializing offset-based metadata."""
        if self.closed:
            raise EncodeError("module builder is closed")
        for buffer in self.buffers.values():
            if buffer.text_error is not None:
                raise buffer.text_error
            if buffer.text_builder is not None:
                buffer.text_builder.finalize_layout()
        self.text_layout_finalized = True

    def text_label_offsets(self) -> dict[str, int]:
        buffer = self.buffers.get(("__TEXT", "__text"))
        if buffer is None or buffer.text_builder is None:
            return {}
        if buffer.text_error is not None:
            raise buffer.text_error
        return buffer.text_builder.labels

    def text_size(self) -> int:
        buffer = self.buffers.get(("__TEXT", "__text"))
        if buffer is None or buffer.text_builder is None:
            return 0
        if buffer.text_error is not None:
            raise buffer.text_error
        return buffer.text_builder.pc

    def _pad(self, buf: _SectionBuffer, align_log2: int) -> None:
        mask = (1 << align_log2) - 1
        padding = (-buf.size) & mask
        if padding:
            buf.append(b"\0" * padding)

    def _append_line(self, raw: str) -> None:
        line = raw.split(";")[0].strip()
        if not line:
            return
        directive_name = line.split(None, 1)[0]

        if line.startswith(".section"):
            parts = [p.strip() for p in line[len(".section"):].strip().split(",")]
            if len(parts) < 2:
                raise EncodeError(f"bad .section line {line!r}")
            seg, sect = parts[0], parts[1]
            attrs = ",".join(parts[2:])
            key = (seg, sect)
            if key not in self.buffers:
                flags = _SECTION_FLAGS.get((seg, sect, attrs))
                if seg == "__PCC" and not attrs and sect.startswith(("__init", "__fini")) and sect[6:].isdigit():
                    flags = DATA_SECTION_FLAGS
                if flags is None:
                    raise EncodeError(
                        f"section {seg},{sect} with attrs {attrs!r} not proven"
                    )
                self.buffers[key] = _SectionBuffer(seg, sect, flags)
                self.order.append(key)
            self.current = self.buffers[key]
            if self.current.is_text and self.text_layout_finalized:
                raise EncodeError("text layout is finalized")
            if self.current.is_text and self.current.text_builder is None:
                self.current.text_builder = PackedAArch64TextBuilder(
                    structured_symbol_names=self.structured_symbol_names,
                )
            return

        if line.startswith(".p2align"):
            if self.current is None:
                raise EncodeError(".p2align before any .section")
            n = _parse_int(line.split()[1])
            if n is None or n < 0 or n > 4:
                raise EncodeError(f"bad .p2align {line!r}")
            self.current.align_log2 = max(self.current.align_log2, n)
            if self.current.is_text:
                if n > 2:
                    raise EncodeError(".p2align > 2 in __text not proven")
                # The instruction assembler owns the running mixed
                # instruction/data offset. Usually this is a no-op; inside a
                # data region it may need to materialize padding bytes.
                self.current.append_text_line(line)
            else:
                self._pad(self.current, n)
            return

        if line.startswith(".globl"):
            self.globl.add(line.split()[1].strip())
            return

        if line.startswith(".private_extern"):
            self.private_extern.add(line.split()[1].strip())
            return

        if line == ".subsections_via_symbols":
            return  # macho_obj always sets the header flag

        if directive_name in (".data_region", ".end_data_region"):
            if self.current is None or not self.current.is_text:
                raise EncodeError(
                    f"data-region marker outside __text: {line!r}"
                )
            self.current.append_text_line(line)
            return

        if directive_name in (
            ".quad", ".long", ".short", ".byte", ".space",
            ".float", ".double",
        ):
            if self.current is None:
                raise EncodeError(f"data directive outside a data section: {line!r}")
            if self.current.is_text:
                self.current.append_text_line(line)
                return
            directive, _, rest = line.partition(" ")
            if directive == ".space":
                count = _parse_int(rest)
                if count is None or count < 0:
                    raise EncodeError(f"bad .space {line!r}")
                self.current.append(b"\0" * count)
                return
            if directive in (".float", ".double"):
                fmt = "<f" if directive == ".float" else "<d"
                for item in _split_data_items(rest):
                    try:
                        self.current.append(struct.pack(fmt, float(item)))
                    except (ValueError, OverflowError) as exc:
                        raise EncodeError(f"bad {directive} item {item!r}") from exc
                return
            width = {
                ".quad": 8, ".long": 4, ".short": 2, ".byte": 1,
            }[directive]
            for item in _split_data_items(rest):
                value = _parse_int(item)
                if value is not None:
                    self.current.append(
                        (value & (1 << width * 8) - 1).to_bytes(width, "little")
                    )
                    continue
                # symbol [± offset] — only proven at pointer width (UNSIGNED)
                if width != 8:
                    raise EncodeError(
                        f"symbol-valued {directive} not proven: {line!r}"
                    )
                symbol, offset = item, 0
                for sep in ("+", "-"):
                    if sep in item[1:]:
                        symbol, _, off_tok = item.partition(sep)
                        offset = _parse_int(off_tok)
                        if offset is None:
                            raise EncodeError(f"bad .quad item {item!r}")
                        if sep == "-":
                            offset = -offset
                        break
                symbol = symbol.strip()
                # DWARF address operands are packed and need not align.
                # The object writer already validates these data relocations.
                if self.current.size % 8 != 0 and not self.current.flags & spec.S_ATTR_DEBUG:
                    raise EncodeError(
                        f".quad {symbol} at unaligned offset {self.current.size}"
                    )
                self.current.relocations.append(Relocation(
                    self.current.size, symbol,
                    spec.ARM64_RELOC_UNSIGNED, pcrel=False, length=3,
                ))
                # UNSIGNED embeds its addend in the pointer bytes
                self.current.append(struct.pack("<q", offset))
            return

        # A one-token "name:" line is a label even when the name starts with
        # a dot: ELF symbols carry no "_" prefix, so module globals such as
        # ``.modvar.<mod>.<name>`` reach this driver as dot-leading labels.
        if line.startswith(".") and not (line.endswith(":") and len(line.split()) == 1):
            raise EncodeError(f"directive {line.split()[0]!r} not proven")

        if line.endswith(":"):
            name = line[:-1].strip()
            if self.current is None:
                raise EncodeError(f"label {name!r} before any .section")
            if self.current.is_text:
                # Text input retains the encoder's comment normalization;
                # typed labels enter through append_label without parsing.
                self.current.append_text_line(line)
                return
            # Assembler-local L labels in data are padding-free markers; the
            # emitter does not use them, so any data label is a symbol.
            self._define_label(name)
            return

        # instruction line
        if self.current is None or not self.current.is_text:
            raise EncodeError(f"instruction outside __text: {line!r}")
        self.current.append_text_line(line)


    def finish(self, structured_sections=()) -> tuple[list[Section], list[str]]:
        try:
            if self.closed:
                raise EncodeError("module builder is closed")
            return self._finish_sections(structured_sections)
        finally:
            self.close()

    def _finish_sections(self, structured_sections) -> tuple[list[Section], list[str]]:
        # as(1) orders sections by segment (__TEXT before __DATA) regardless of
        # where the .section directives appear; within a segment, first
        # appearance wins. Match it — section order decides vmaddr layout and
        # every symbol's n_sect/n_value, so this is load-bearing, not cosmetic.
        seg_rank = {"__TEXT": 0, "__DATA": 1, "__LD": 2}
        appearance = {key: i for i, key in enumerate(self.order)}
        self.order.sort(key=lambda key: (seg_rank.get(key[0], 2), appearance[key]))

        # Assemble each text buffer as one unit (branches may cross .section
        # re-declarations of the same text section, never different sections).
        sections: list[Section] = []
        undefined: set[str] = set()
        defined_names: set[str] = set()
        for key in self.order:
            buf = self.buffers[key]
            if buf.is_text:
                if buf.text_error is not None:
                    raise buf.text_error
                assert buf.text_builder is not None
                assembled = buf.text_builder.finish()
                symbols = tuple(
                    TextSymbol(
                        name,
                        off,
                        external=(name in self.globl or name in self.private_extern),
                        private_external=(name in self.private_extern),
                    )
                    for name, off in assembled.labels.items()
                    if not name.startswith("L")  # assembler-local, not symbols
                )
                sections.append(Section(
                    sectname=buf.sectname, segname=buf.segname,
                    data=assembled.code,
                    align_log2=max(buf.align_log2, 2),
                    flags=buf.flags,
                    symbols=symbols,
                    relocations=tuple(assembled.relocations),
                    data_in_code=tuple(assembled.data_in_code),
                ))
                undefined.update(assembled.undefined)
                defined_names.update(s.name for s in symbols)

            else:
                symbols = tuple(
                    TextSymbol(
                        symbol.name,
                        symbol.offset,
                        external=(
                            symbol.name in self.globl
                            or symbol.name in self.private_extern
                        ),
                        private_external=(symbol.name in self.private_extern),
                    )
                    for symbol in buf.symbols
                )
                sections.append(Section(
                    sectname=buf.sectname, segname=buf.segname,
                    data=b"".join(buf.chunks),
                    align_log2=buf.align_log2,
                    flags=buf.flags,
                    symbols=symbols,
                    relocations=tuple(buf.relocations),
                ))
                undefined.update(
                    r.symbol for r in buf.relocations
                )
                defined_names.update(s.name for s in symbols)
        existing_section_keys = {
            (section.segname, section.sectname) for section in sections
        }
        for section in structured_sections:
            if not isinstance(section, Section):
                raise EncodeError("structured assembly section is not a Section")
            key = (section.segname, section.sectname)
            if key in existing_section_keys:
                raise EncodeError(
                    "structured assembly duplicates section "
                    + section.segname
                    + ","
                    + section.sectname
                )
            existing_section_keys.add(key)
            sections.append(section)
            for relocation in section.relocations:
                if relocation.symbol:
                    undefined.add(relocation.symbol)
            for symbol in section.symbols:
                defined_names.add(symbol.name)
        # Parsed sections already have the correct stable intra-segment order.
        # Structured sections are emitted later and therefore sort after parsed
        # peers in the same segment, while __DATA still precedes __LD.
        sections.sort(key=lambda section: seg_rank.get(section.segname, 2))

        missing_private = self.private_extern - defined_names
        if missing_private:
            raise EncodeError(
                f"private-extern symbols were not defined: {sorted(missing_private)!r}"
            )

        # as(1) represents compact-unwind and DWARF pointers to functions defined
        # by this object as section-target relocations, independent of the
        # function symbol's visibility.  Undefined personality/LSDA references
        # remain symbol relocations.  Encoding a defined function's section and
        # offset also keeps the row valid if a local symbol is stripped later.
        defined_targets: dict[str, tuple[tuple[str, str], int, int]] = {}
        for section in sections:
            key = (section.segname, section.sectname)
            for symbol in section.symbols:
                defined_targets[symbol.name] = (
                    key, symbol.offset, section.vm_size,
                )
        normalized_sections: list[Section] = []
        for section in sections:
            if ((section.segname, section.sectname) != ("__LD", "__compact_unwind")
                    and not (section.segname == "__DWARF" and section.flags & spec.S_ATTR_DEBUG)):
                normalized_sections.append(section)
                continue
            payload = bytearray(section.data)
            relocations: list[Relocation] = []
            for relocation in section.relocations:
                target = defined_targets.get(relocation.symbol)
                if (
                    target is None
                    or relocation.type != spec.ARM64_RELOC_UNSIGNED
                    or relocation.pcrel
                    or relocation.length != 3
                ):
                    relocations.append(relocation)
                    continue
                target_section, symbol_offset, target_size = target
                embedded = int.from_bytes(
                    payload[relocation.offset:relocation.offset + 8], "little"
                )
                if embedded & (1 << 63):
                    embedded -= 1 << 64
                target_offset = symbol_offset + embedded
                if not 0 <= target_offset < target_size:
                    raise EncodeError(
                        "debug/unwind local function relocation resolves "
                        f"outside {target_section[0]},{target_section[1]}"
                    )
                clear_index = 0
                while clear_index < 8:
                    payload[relocation.offset + clear_index] = 0
                    clear_index += 1
                relocations.append(Relocation(
                    relocation.offset,
                    "",
                    relocation.type,
                    pcrel=False,
                    length=3,
                    section=target_section,
                    target_offset=target_offset,
                ))
            normalized_sections.append(Section(
                sectname=section.sectname,
                segname=section.segname,
                data=bytes(payload),
                align_log2=section.align_log2,
                flags=section.flags,
                symbols=section.symbols,
                relocations=tuple(relocations),
                zerofill_size=section.zerofill_size,
                data_in_code=section.data_in_code,
            ))
        sections = normalized_sections

        return sections, sorted(undefined - defined_names)



def _append_assembly_lines(
    builder: AArch64ModuleBuilder,
    asm_lines: list[str],
    encoded_line_records: CompilerIntArena | None = None,
) -> None:
    if encoded_line_records is not None and len(encoded_line_records) % 4:
        raise EncodeError(
            "structured instruction records need line/word/relocation/symbol"
        )
    record_count = 0 if encoded_line_records is None else len(encoded_line_records) // 4
    record_index = 0
    next_chunk_index = 0
    # Preserve chunk-index preflight before any directive/text validation.
    while record_index < record_count:
        assert encoded_line_records is not None
        encoded_index = encoded_line_records.get_unchecked(record_index * 4)
        if next_chunk_index >= len(asm_lines):
            raise EncodeError("structured instruction index exceeds assembly input")
        if encoded_index < next_chunk_index:
            raise EncodeError("structured instruction indices are not ordered")
        if encoded_index >= len(asm_lines):
            raise EncodeError("structured instruction index exceeds assembly input")
        next_chunk_index = encoded_index + 1
        record_index += 1
    record_index = 0
    chunk_index = 0
    while chunk_index < len(asm_lines):
        if record_index < record_count:
            assert encoded_line_records is not None
            offset = record_index * 4
            if encoded_line_records.get_unchecked(offset) == chunk_index:
                builder.append_encoded(
                    encoded_line_records.get_unchecked(offset + 1),
                    encoded_line_records.get_unchecked(offset + 2),
                    encoded_line_records.get_unchecked(offset + 3),
                )
                record_index += 1
                chunk_index += 1
                continue
        builder.append_chunk(asm_lines[chunk_index])
        chunk_index += 1


def measure_text_layout(
    asm_lines: list[str],
    encoded_line_records: CompilerIntArena | None = None,
    structured_symbol_names: list[str] | tuple[str, ...] = (),
) -> tuple[dict[str, int], int]:
    """Use the canonical final layout for metadata in the line adapter lane.

    No code-changing pass may run between this measurement and assembly.
    Structured word records and inline data use the same parser and relaxation
    as final section emission; instruction-count arithmetic is not sufficient.
    """
    builder = AArch64ModuleBuilder(structured_symbol_names)
    try:
        _append_assembly_lines(builder, asm_lines, encoded_line_records)
        builder.finalize_text_layout()
        return dict(builder.text_label_offsets()), builder.text_size()
    finally:
        builder.close()


def assemble_lines(
    asm_lines: list[str],
    structured_sections=(),
    encoded_line_records: CompilerIntArena | None = None,
    structured_symbol_names: list[str] | tuple[str, ...] = (),
) -> tuple[list[Section], list[str]]:
    """Line adapter over the module builder; the caller owns the encoded arena."""
    builder = AArch64ModuleBuilder(structured_symbol_names)
    try:
        _append_assembly_lines(builder, asm_lines, encoded_line_records)
        return builder.finish(structured_sections)
    finally:
        builder.close()


def assemble_file(asm_text: str) -> tuple[list[Section], list[str]]:
    """Compatibility string projection for the full self-backend dialect."""

    return assemble_lines(asm_text.splitlines())
