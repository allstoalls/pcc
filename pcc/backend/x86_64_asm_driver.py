"""Owned Intel-syntax file driver for the x86_64 Linux self backend.

The self emitter writes a deliberately finite GNU-style assembly dialect.
This module owns that complete file boundary: directives, sections, symbols,
data, instructions, and RELA records become an ``ElfObject`` without invoking
``as``/``cc``.  Any dialect growth fails closed and must be added together with
an encoder differential.

Every instruction has a PC-independent length: branches are always rel32 and
whatever else depends on the PC is a relocation.  One pass therefore places
and encodes each line at its final section offset.  References that may name
a label defined later are written as zeros and patched at the end: direct
branches to a local label of their own section and ``.long a - b`` differences.
"""

from __future__ import annotations

import struct
import re
from dataclasses import dataclass

from .elf_x86_64 import (
    R_X86_64_64,
    SHF_ALLOC,
    SHF_EXECINSTR,
    SHF_TLS,
    SHF_WRITE,
    SHT_NOBITS,
    SHT_PROGBITS,
    STB_GLOBAL,
    STB_LOCAL,
    STT_FUNC,
    STT_NOTYPE,
    STT_OBJECT,
    STT_TLS,
    ElfObject,
    ElfRelocation,
    ElfSection,
    ElfSymbol,
)
from .x86_64_encode import (
    X86EncodeError,
    _OperandParseCache,
    _is_branch_mnemonic,
    encode_instruction,
)


@dataclass(slots=True)
class _SymbolMeta:
    global_: bool = False
    type: int = STT_NOTYPE
    size: int | None = None


@dataclass(frozen=True, slots=True)
class _PendingRelocation:
    section_name: str
    offset: int
    symbol: str
    type: int
    addend: int


@dataclass(frozen=True, slots=True)
class _PendingDifference:
    offset: int
    left: str
    right: str
    width: int


_SECTION_SPECS = {
    # ELF sh_addralign is the maximum alignment actually requested inside the
    # section.  The emitter writes a .p2align before every function/global, so
    # begin at one rather than inventing an eight/sixteen-byte requirement
    # that GNU as would not put on a byte-only section.
    ".text": (SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, 1),
    ".rodata": (SHT_PROGBITS, SHF_ALLOC, 1),
    ".data": (SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 1),
    ".pcc_stackmaps": (SHT_PROGBITS, SHF_ALLOC, 1),
    ".tdata": (SHT_PROGBITS, SHF_ALLOC | SHF_WRITE | SHF_TLS, 1),
    ".tbss": (SHT_NOBITS, SHF_ALLOC | SHF_WRITE | SHF_TLS, 1),
    ".note.GNU-stack": (SHT_PROGBITS, 0, 1),
}


def _parse_int(text: str) -> int:
    try:
        return int(text.strip(), 0)
    except ValueError as exc:
        raise X86EncodeError(f"expected integer in directive, got {text!r}") from exc


def _align(value: int, log2: int) -> int:
    alignment = 1 << log2
    return (value + alignment - 1) & ~(alignment - 1)


def _data_items(text: str) -> list[str]:
    items = [item.strip() for item in text.split(",")]
    if not items or any(not item for item in items):
        raise X86EncodeError(f"empty data item list {text!r}")
    return items


def _integer_payload(value: int, width: int, *, owner: str) -> bytes:
    bits = width * 8
    if value < -(1 << (bits - 1)) or value > (1 << bits) - 1:
        raise X86EncodeError(
            f"{owner} value {value} does not fit {bits} bits"
        )
    return (value & ((1 << bits) - 1)).to_bytes(width, "little")


def _symbol_plus_addend(text: str) -> tuple[str, int]:
    item = text.strip()
    for index in range(1, len(item)):
        if item[index] not in "+-":
            continue
        symbol = item[:index].strip()
        if not symbol:
            break
        addend = _parse_int(item[index:])
        return symbol, addend
    if not item or any(char.isspace() for char in item):
        raise X86EncodeError(f"invalid symbol-valued data item {text!r}")
    return item, 0


def _section_from_directive(line: str) -> str:
    body = line[len(".section"):].strip()
    parts = [part.strip() for part in body.split(",")]
    if not parts or not parts[0]:
        raise X86EncodeError(f"bad section directive {line!r}")
    name = parts[0]
    initializer = name.startswith((".init_array.", ".fini_array.")) and name.rsplit(".", 1)[1].isdigit()
    if (name not in _SECTION_SPECS and not initializer) or len(parts) not in (1, 3):
        raise X86EncodeError(f"section shape not proven: {line!r}")
    if len(parts) == 1:
        return name
    section_type, flags, _alignment = (SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 8) if initializer else _SECTION_SPECS[name]
    expected_flags = ""
    for letter, bit in (("a", SHF_ALLOC), ("w", SHF_WRITE), ("x", SHF_EXECINSTR), ("T", SHF_TLS)):
        if flags & bit:
            expected_flags += letter
    expected_type = "@nobits" if section_type == SHT_NOBITS else "@progbits"
    spelled_flags = parts[1]
    if (
        not (spelled_flags.startswith('"') and spelled_flags.endswith('"'))
        or set(spelled_flags[1:-1]) != set(expected_flags)
        or parts[2] != expected_type
    ):
        raise X86EncodeError(f"section attributes not proven: {line!r}")
    return name


_ASSEMBLY_LINE_CHUNK_CHARS = 65536
_ASSEMBLY_LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _iter_assembly_lines(asm_text: str):
    """Keep splitlines semantics without retaining the whole line list."""
    position = 0
    length = len(asm_text)
    fragments: list[str] = []
    while position < length:
        end = min(position + _ASSEMBLY_LINE_CHUNK_CHARS, length)
        # Do not separate the two characters of one CRLF boundary.
        if end < length and asm_text[end - 1] == "\r" and asm_text[end] == "\n":
            end += 1
        chunk = asm_text[position:end]
        position = end
        for line in chunk.splitlines(True):
            complete = line[-1] in "\r\n"
            if not complete and line[-1] in _ASSEMBLY_LINE_BREAKS:
                # Match the executing runtime's splitlines contract even when
                # a less common separator happens to end a chunk.
                complete = line[-1].splitlines() == [""]
            if complete:
                if line.endswith("\r\n"):
                    line = line[:-2]
                else:
                    line = line[:-1]
                if fragments:
                    fragments.append(line)
                    yield "".join(fragments)
                    fragments = []
                else:
                    yield line
            else:
                fragments.append(line)
    if fragments:
        yield "".join(fragments)


_SECTION_PAYLOAD_CHUNK_BYTES = 8192


class _SectionPayload:
    """Publish section bytes without repeatedly copying their whole prefix."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.pending: list[bytes] = []
        self.pending_size = 0

    def _flush(self) -> None:
        if self.pending:
            self.chunks.append(b"".join(self.pending))
            self.pending.clear()
            self.pending_size = 0

    def append(self, raw: bytes) -> None:
        if not raw:
            return
        size = len(raw)
        if size >= _SECTION_PAYLOAD_CHUNK_BYTES:
            self._flush()
            self.chunks.append(raw)
            return
        self.pending.append(raw)
        self.pending_size += size
        if self.pending_size >= _SECTION_PAYLOAD_CHUNK_BYTES:
            self._flush()

    def finish(self) -> bytes:
        self._flush()
        result = b"".join(self.chunks)
        self.chunks.clear()
        return result


class _SectionBuild:
    """One output section while the single pass places and encodes it."""

    def __init__(self, name: str, section_type: int, flags: int, align: int) -> None:
        self.name = name
        self.type = section_type
        self.flags = flags
        self.align = align
        self.payload = _SectionPayload()
        self.size = 0
        # A zero fill of at least one payload chunk stays a length until the
        # section is finished: segments[i] holds the bytes before zero_runs[i].
        self.segments: list[bytes] = []
        self.zero_runs: list[int] = []
        # Label definitions in order; symbols are published section-major.
        self.labels: list[str] = []
        # Every relocation in offset order. The indexed entries come from
        # direct branches, which bind to a local label of this section.
        self.relocations: list[_PendingRelocation] = []
        self.branches: list[int] = []
        self.differences: list[_PendingDifference] = []


def _switch_section(sections: dict[str, _SectionBuild], order: list[str], name: str) -> _SectionBuild:
    spec = _SECTION_SPECS.get(name)
    if name.startswith((".init_array.", ".fini_array.")) and name.rsplit(".", 1)[1].isdigit():
        spec = (SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 8)
    if spec is None:
        raise X86EncodeError(f"section {name!r} is outside the ELF contract")
    section = sections.get(name)
    if section is None:
        section = _SectionBuild(name, spec[0], spec[1], spec[2])
        sections[name] = section
        order.append(name)
    return section


def _append_data(section: _SectionBuild, raw: bytes) -> None:
    if section.type == SHT_NOBITS and raw:
        raise X86EncodeError(f"NOBITS section {section.name!r} has file data")
    section.payload.append(raw)
    section.size += len(raw)


class X86ModuleBuilder:
    """One module's sections, symbols and labels, fed line by line.

    The text driver streams a whole assembly file through ``append_lines``;
    native emission feeds each function's lines as it finishes them, so no
    module-wide text is joined and split again. With ``unwind`` set, Win64
    ``.seh_`` directives become marker labels and unwind procedures exactly as
    the COFF writer's text pass named them.
    """

    def __init__(self, operand_cache: _OperandParseCache | None = None,
                 *, unwind: bool = False) -> None:
        self.sections: dict[str, _SectionBuild] = {}
        self.order: list[str] = []
        self.symbols: dict[str, _SymbolMeta] = {}
        self.labels: dict[str, tuple[str, int]] = {}
        self.sizes: dict[str, int] = {}
        # Encoding never resolves a label: every target is a relocation first.
        self.no_labels: dict[str, tuple[str, int]] = {}
        self.current: _SectionBuild | None = None
        self.saw_syntax = False
        self.operand_cache = _OperandParseCache() if operand_cache is None else operand_cache
        self.unwind = unwind
        # [name, actions, prologue-end label, end label] per .seh_proc, and
        # the count of lines the unwind marker names are numbered by.
        self.unwind_procedures: list[list] = []
        self.unwind_line_count = 0
        self.unwind_current: list | None = None

    def append_lines(self, lines) -> None:
        """Place and encode every line at its final section offset."""
        sections = self.sections
        order = self.order
        symbols = self.symbols
        labels = self.labels
        sizes = self.sizes
        no_labels = self.no_labels
        operand_cache = self.operand_cache
        current = self.current
        saw_syntax = self.saw_syntax
        unwind = self.unwind
        for raw_line in lines:
            if unwind:
                raw_line = self._unwind_line(raw_line)
            line = raw_line.strip()
            if not line:
                continue
            if line == ".intel_syntax noprefix":
                if saw_syntax or current is not None or symbols:
                    raise X86EncodeError(
                        ".intel_syntax noprefix must be the first file directive"
                    )
                saw_syntax = True
                continue
            if not saw_syntax:
                raise X86EncodeError(
                    "owned x86 assembly must begin with .intel_syntax noprefix"
                )
            if not line.startswith("."):
                if not line.endswith(":"):
                    if current is None or not current.flags & SHF_EXECINSTR:
                        raise X86EncodeError(f"instruction outside .text: {line!r}")
                    encoded = encode_instruction(
                        line, pc=current.size, labels=no_labels,
                        section_name=current.name, operand_cache=operand_cache,
                    )
                    if encoded.relocations:
                        branch = _is_branch_mnemonic(line.partition(" ")[0].lower())
                        for relocation in encoded.relocations:
                            if branch:
                                current.branches.append(len(current.relocations))
                            current.relocations.append(_PendingRelocation(
                                current.name, relocation.offset, relocation.symbol,
                                relocation.type, relocation.addend,
                            ))
                    current.payload.append(encoded.code)
                    current.size += len(encoded.code)
                    continue
            elif line == ".text":
                current = _switch_section(sections, order, ".text")
                continue
            elif line == ".data":
                current = _switch_section(sections, order, ".data")
                continue
            elif line.startswith(".section"):
                current = _switch_section(sections, order, _section_from_directive(line))
                continue
            elif line.startswith(".globl "):
                name = line.split(None, 1)[1].strip()
                if not name:
                    raise X86EncodeError(f"bad .globl directive {line!r}")
                meta = symbols.get(name)
                if meta is None:
                    meta = _SymbolMeta()
                    symbols[name] = meta
                meta.global_ = True
                continue
            elif line.startswith(".type "):
                body = line[len(".type "):]
                pieces = [part.strip() for part in body.split(",")]
                if len(pieces) != 2 or not pieces[0] or pieces[1] not in ("@function", "@object", "@tls_object"):
                    raise X86EncodeError(f"bad .type directive {line!r}")
                symbol_type = STT_FUNC if pieces[1] == "@function" else STT_TLS if pieces[1] == "@tls_object" else STT_OBJECT
                meta = symbols.get(pieces[0])
                if meta is None:
                    meta = _SymbolMeta()
                    symbols[pieces[0]] = meta
                if meta.type not in (STT_NOTYPE, symbol_type):
                    raise X86EncodeError(
                        f"conflicting .type directives for {pieces[0]!r}"
                    )
                meta.type = symbol_type
                continue
            elif line.startswith(".size "):
                if current is None:
                    raise X86EncodeError(".size before any section")
                body = line[len(".size "):]
                pieces = [part.strip() for part in body.split(",", 1)]
                if len(pieces) != 2:
                    raise X86EncodeError(f"bad .size directive {line!r}")
                meta = symbols.get(pieces[0])
                if meta is None:
                    meta = _SymbolMeta()
                    symbols[pieces[0]] = meta
                if pieces[1] == ".-" + pieces[0]:
                    start = labels.get(pieces[0])
                    if start is None or start[0] != current.name:
                        raise X86EncodeError(
                            f".size references non-local symbol {pieces[0]!r}"
                        )
                    computed_size = current.size - start[1]
                    if meta.size is not None and meta.size != computed_size:
                        raise X86EncodeError(
                            f"conflicting .size directives for {pieces[0]!r}"
                        )
                    if pieces[0] in sizes and sizes[pieces[0]] != computed_size:
                        raise X86EncodeError(
                            f"inconsistent location size for {pieces[0]!r}"
                        )
                    sizes[pieces[0]] = computed_size
                else:
                    size = _parse_int(pieces[1])
                    if size < 0:
                        raise X86EncodeError(f"negative symbol size in {line!r}")
                    if meta.size is not None and meta.size != size:
                        raise X86EncodeError(
                            f"conflicting .size directives for {pieces[0]!r}"
                        )
                    meta.size = size
                continue
            elif line.startswith(".p2align "):
                if current is None:
                    raise X86EncodeError(".p2align before any section")
                pieces = _data_items(line[len(".p2align "):])
                log2 = _parse_int(pieces[0])
                if log2 < 0 or log2 > 16:
                    raise X86EncodeError(f".p2align outside finite range: {line!r}")
                fill = _parse_int(pieces[1]) if len(pieces) == 2 else (
                    0x90 if current.flags & SHF_EXECINSTR else 0
                )
                if len(pieces) > 2 or not 0 <= fill <= 255:
                    raise X86EncodeError(f"bad .p2align fill: {line!r}")
                current.align = max(current.align, 1 << log2)
                padding = _align(current.size, log2) - current.size
                if current.type != SHT_NOBITS:
                    current.payload.append(bytes((fill,)) * padding)
                current.size += padding
                continue
            elif line.startswith((".byte ", ".short ", ".long ", ".quad ")):
                if current is None:
                    raise X86EncodeError("data directive before any section")
                directive, rest = line.split(None, 1)
                width = {".byte": 1, ".short": 2, ".long": 4, ".quad": 8}[directive]
                for item in _data_items(rest):
                    try:
                        value = int(item, 0)
                    except ValueError:
                        difference = re.fullmatch(r"([.$A-Za-z_][.$\w]*)\s*-\s*([.$A-Za-z_][.$\w]*)", item)
                        if difference:
                            if current.type == SHT_NOBITS:
                                raise X86EncodeError(f"NOBITS section {current.name!r} has file data")
                            # Either label may still be ahead; patch once all are known.
                            current.differences.append(_PendingDifference(
                                current.size, difference.group(1), difference.group(2), width,
                            ))
                            _append_data(current, b"\0" * width)
                            continue
                        if width != 8:
                            raise X86EncodeError(
                                f"symbol-valued {directive} is not proven: {line!r}"
                            )
                        symbol, addend = _symbol_plus_addend(item)
                        if current.type == SHT_NOBITS:
                            raise X86EncodeError(f"NOBITS section {current.name!r} has a relocation")
                        current.relocations.append(_PendingRelocation(
                            current.name, current.size, symbol, R_X86_64_64, addend,
                        ))
                        _append_data(current, b"\0" * 8)
                    else:
                        _append_data(current, _integer_payload(value, width, owner=directive))
                continue
            elif line.startswith((".float ", ".double ")):
                if current is None:
                    raise X86EncodeError("floating data before any section")
                directive, rest = line.split(None, 1)
                fmt = "<f" if directive == ".float" else "<d"
                for item in _data_items(rest):
                    try:
                        payload = struct.pack(fmt, float(item))
                    except (ValueError, OverflowError) as exc:
                        raise X86EncodeError(f"bad {directive} value {item!r}") from exc
                    _append_data(current, payload)
                continue
            elif line.startswith((".zero ", ".space ")):
                if current is None:
                    raise X86EncodeError("zero-fill before any section")
                size = _parse_int(line.split(None, 1)[1])
                if size < 0:
                    raise X86EncodeError(f"negative zero-fill {line!r}")
                if current.type != SHT_NOBITS:
                    if size >= _SECTION_PAYLOAD_CHUNK_BYTES:
                        current.segments.append(current.payload.finish())
                        current.zero_runs.append(size)
                        current.payload = _SectionPayload()
                    else:
                        current.payload.append(b"\0" * size)
                current.size += size
                continue
            elif not line.endswith(":"):
                raise X86EncodeError(f"directive not proven: {line!r}")
            if current is None:
                raise X86EncodeError(f"label before any section: {line!r}")
            name = line[:-1].strip()
            if not name:
                raise X86EncodeError("empty label")
            # Labels carry no metadata by themselves; only explicit declarations
            # need a metadata entry.
            if name in labels:
                raise X86EncodeError(f"duplicate assembly label {name!r}")
            labels[name] = (current.name, current.size)
            current.labels.append(name)
        self.current = current
        self.saw_syntax = saw_syntax

    def _unwind_line(self, raw_line: str) -> str:
        # Mirrors the COFF text pass: every kept line counts, a directive
        # other than .seh_proc becomes a numbered marker label, and .seh_proc
        # itself becomes an empty line, which placement skips.
        stripped = raw_line.strip()
        if not stripped.startswith(".seh_"):
            self.unwind_line_count += 1
            return raw_line
        op, _, args = stripped.partition(" ")
        if op == ".seh_proc":
            self.unwind_current = [args, [], "", ""]
            self.unwind_procedures.append(self.unwind_current)
            return ""
        current = self.unwind_current
        if current is None:
            raise X86EncodeError("unwind directive outside a function")
        label = ".Lpcc_seh_" + str(len(self.unwind_procedures)) + "_" + str(self.unwind_line_count)
        self.unwind_line_count += 1
        if op == ".seh_endprologue":
            current[2] = label
        elif op == ".seh_endproc":
            current[3] = label
            self.unwind_current = None
        else:
            current[1].append((label, op, args))
        return label + ":"

    def unwind_markers(self) -> set[str]:
        markers = set()
        for _name, actions, prolog_end, end in self.unwind_procedures:
            markers.update(label for label, _op, _args in actions)
            markers.update((prolog_end, end))
        return markers

    def finish_layout(self):
        if self.unwind_current is not None:
            raise X86EncodeError("unterminated unwind procedure")
        if not self.saw_syntax:
            raise X86EncodeError(
                "owned x86 assembly must begin with .intel_syntax noprefix"
            )
        # A computed size may precede the explicit declaration it must match.
        symbols = self.symbols
        for symbol, size in self.sizes.items():
            meta = symbols[symbol]
            if meta.size is not None and meta.size != size:
                raise X86EncodeError(f"conflicting .size directives for {symbol!r}")
            meta.size = size
        return self.sections, self.order, symbols, self.labels


def _encode_file(asm_text: str, operand_cache: _OperandParseCache):
    """Place and encode every line at its final section offset."""
    builder = X86ModuleBuilder(operand_cache)
    builder.append_lines(_iter_assembly_lines(asm_text))
    return builder.finish_layout()


def _finish_section(
    section: _SectionBuild,
    labels: dict[str, tuple[str, int]],
    symbols: dict[str, _SymbolMeta],
) -> tuple[bytes, list[_PendingRelocation]]:
    """Patch label references into the payload; keep the other relocations.

    GAS deliberately leaves calls to global definitions relocatable so the
    final static linker, rather than the assembler, owns symbol resolution.
    A direct branch to a local label of its own section is resolved here.
    Every reference is validated before the payload and its deferred zero
    fills are materialized, so a far branch fails without allocating them.
    """
    relocations = section.relocations
    branches = section.branches
    if not branches and not section.differences and not section.segments:
        return section.payload.finish(), relocations
    kept: list[_PendingRelocation] = []
    patch_offsets: list[int] = []
    patch_values: list[bytes] = []
    branch_cursor = 0
    index = 0
    while index < len(relocations):
        relocation = relocations[index]
        branch = branch_cursor < len(branches) and branches[branch_cursor] == index
        index += 1
        if branch:
            branch_cursor += 1
            location = labels.get(relocation.symbol)
            meta = symbols.get(relocation.symbol)
            if (
                location is not None and location[0] == section.name
                and (meta is None or not meta.global_)
            ):
                displacement = location[1] + relocation.addend - relocation.offset
                if displacement < -(1 << 31) or displacement >= (1 << 31):
                    raise X86EncodeError(
                        f"branch target {relocation.symbol!r} is outside rel32 range"
                    )
                patch_offsets.append(relocation.offset)
                patch_values.append((displacement & 0xFFFFFFFF).to_bytes(4, "little"))
                continue
        kept.append(relocation)
    for difference in section.differences:
        left = labels.get(difference.left)
        right = labels.get(difference.right)
        if left is None or right is None or left[0] != right[0]:
            raise X86EncodeError("symbol difference requires definitions in the same section: " + difference.left + " - " + difference.right)
        patch_offsets.append(difference.offset)
        patch_values.append(_integer_payload(left[1] - right[1], difference.width, owner="symbol difference"))
    tail = section.payload.finish()
    if section.segments:
        parts: list[bytes] = []
        segment = 0
        while segment < len(section.segments):
            parts.append(section.segments[segment])
            parts.append(bytes(section.zero_runs[segment]))
            segment += 1
        parts.append(tail)
        data = b"".join(parts)
        section.segments.clear()
    else:
        data = tail
    if not patch_offsets:
        return data, kept
    buffer = bytearray(data)
    patch = 0
    while patch < len(patch_offsets):
        offset = patch_offsets[patch]
        raw = patch_values[patch]
        buffer[offset:offset + len(raw)] = raw
        patch += 1
    return bytes(buffer), kept


def assemble_file(asm_text: str, *, phase_timing=None) -> ElfObject:
    """Assemble one complete self-backend file into a validated ElfObject."""
    if phase_timing is None:
        return assemble_file_keeping_labels(asm_text, ())
    return assemble_file_keeping_labels(asm_text, (), phase_timing=phase_timing)


def assemble_file_keeping_labels(asm_text: str, keep_labels, *, phase_timing=None) -> ElfObject:
    """``assemble_file``, also publishing the named unreferenced local labels.

    The COFF writer reads its unwind markers from .symtab.  No default
    argument: pcc's closed-world frontend compiles this module into pcc1.
    """
    if phase_timing is None:
        return _assemble_file(asm_text, keep_labels, None, None, None)
    return _assemble_file(
        asm_text, keep_labels, None, None, None, phase_timing=phase_timing,
    )


def assemble_file_with_stack_maps(
    asm_text: str, stack_map_plans, *, function_symbol, block_label,
    consume_stack_map_plans: bool = False, phase_timing=None,
) -> ElfObject:
    """Assemble code plus structured maps, retaining the ordinary ELF checks."""
    if phase_timing is None:
        return _assemble_file(
            asm_text, (), stack_map_plans, function_symbol, block_label,
            consume_stack_map_plans,
        )
    return _assemble_file(
        asm_text, (), stack_map_plans, function_symbol, block_label,
        consume_stack_map_plans, phase_timing=phase_timing,
    )


def assemble_file_with_stack_maps_keeping_labels(
    asm_text: str, stack_map_plans, keep_labels, *, function_symbol, block_label,
    consume_stack_map_plans: bool = False, phase_timing=None,
) -> ElfObject:
    """Assemble structured maps while retaining the requested local labels.

    COFF consumers retain the same unreferenced SEH marker labels as their
    text-only path; packed metadata still resolves at final machine offsets.
    """
    if phase_timing is None:
        return _assemble_file(
            asm_text, keep_labels, stack_map_plans, function_symbol, block_label,
            consume_stack_map_plans,
        )
    return _assemble_file(
        asm_text, keep_labels, stack_map_plans, function_symbol, block_label,
        consume_stack_map_plans, phase_timing=phase_timing,
    )


def assemble_builder(
    builder: X86ModuleBuilder, keep_labels, stack_map_plans, *, function_symbol,
    block_label, consume_stack_map_plans: bool = False, phase_timing=None,
) -> ElfObject:
    """The ELF object of a module streamed into ``builder``.

    Same checks, stack-map packing and symbol publication as the text entry
    points; no module text exists to place and encode again.
    """
    if phase_timing is None:
        return _assemble_file(
            "", keep_labels, stack_map_plans, function_symbol, block_label,
            consume_stack_map_plans, builder=builder,
        )
    return _assemble_file(
        "", keep_labels, stack_map_plans, function_symbol, block_label,
        consume_stack_map_plans, phase_timing=phase_timing, builder=builder,
    )


def _assemble_file(
    asm_text: str, keep_labels, stack_map_plans, function_symbol, block_label,
    consume_stack_map_plans: bool = False, phase_timing=None, builder=None,
) -> ElfObject:
    if consume_stack_map_plans and not isinstance(stack_map_plans, list):
        raise X86EncodeError("consuming stack maps require a mutable plan list")
    # Phase 13 is the single placing/encoding pass, 14 resolves labels into
    # stack maps and payloads, and 15 publishes symbols and relocations. A
    # streamed module was placed and encoded as emission fed it; phase 13
    # then only closes its layout.
    started = phase_timing.begin_assembly(13) if phase_timing is not None else 0
    if builder is None:
        sections, order, symbol_meta, labels = _encode_file(asm_text, _OperandParseCache())
    else:
        sections, order, symbol_meta, labels = builder.finish_layout()
    if phase_timing is not None:
        phase_timing.complete_assembly(13, started)
    started = phase_timing.begin_assembly(14) if phase_timing is not None else 0
    if stack_map_plans:
        from .self_backend_precise_stackmaps import build_x86_64_stack_map_payload

        stack_section = sections.get(".pcc_stackmaps")
        if stack_section is None or stack_section.size != 0:
            raise X86EncodeError("structured stack maps require an empty section marker")
        offsets = {
            name: location[1]
            for name, location in labels.items()
            if location[0] == ".text"
        }
        packed, function_relocations = build_x86_64_stack_map_payload(
            tuple(stack_map_plans), offsets,
            function_symbol=function_symbol, block_label=block_label,
        )
        stack_section.payload.append(packed)
        stack_section.size = len(packed)
        del packed
        del offsets
        if consume_stack_map_plans:
            # Final machine offsets and all root facts now live in the owned
            # section bytes.  The caller explicitly gives up these plans;
            # ordinary callers keep their original non-consuming behavior.
            stack_map_plans.clear()
        for offset, symbol in function_relocations:
            stack_section.relocations.append(_PendingRelocation(
                ".pcc_stackmaps", offset, symbol, R_X86_64_64, 0,
            ))
    # A .type declaration is also legal for an external reference. A size
    # describes a definition and still requires a label in this object.
    missing_definitions = sorted(name for name, meta in symbol_meta.items()
                                 if name not in labels and meta.size is not None)
    if missing_definitions:
        raise X86EncodeError(
            "assembly metadata names symbols without definitions: "
            + repr(missing_definitions)
        )
    section_payloads: dict[str, bytes] = {}
    section_relocations: dict[str, list[_PendingRelocation]] = {}
    for name in order:
        payload, relocations = _finish_section(sections[name], labels, symbol_meta)
        section_payloads[name] = payload
        section_relocations[name] = relocations
    if phase_timing is not None:
        phase_timing.complete_assembly(14, started)
        started = phase_timing.begin_assembly(15)

    section_indices = {name: index for index, name in enumerate(order, start=1)}
    referenced = {
        relocation.symbol
        for name in order
        for relocation in section_relocations[name]
    }
    local_symbols: list[ElfSymbol] = []
    global_symbols: list[ElfSymbol] = []
    for section_name in order:
        section = sections[section_name]
        for name in section.labels:
            offset = labels[name][1]
            meta = symbol_meta.get(name)
            # Undeclared labels use the metadata defaults without allocating a
            # record that is never inserted into the private symbol table.
            symbol_global = False if meta is None else meta.global_
            symbol_type = STT_NOTYPE if meta is None else meta.type
            symbol_size = None if meta is None else meta.size
            if (
                not symbol_global
                and symbol_type == STT_NOTYPE
                and symbol_size is None
                and name not in referenced
                and name not in keep_labels
            ):
                # Block/edge labels have already served the local branch
                # resolver.  GAS does not publish its temporary, unreferenced
                # labels into .symtab, and doing so here would add thousands of
                # irrelevant symbols to bootstrap objects.  Typed function/data
                # labels and any local relocation target remain materialized.
                continue
            if section.flags & SHF_TLS:
                symbol_type = STT_TLS
            elif symbol_type == STT_NOTYPE:
                symbol_type = STT_FUNC if section.flags & SHF_EXECINSTR else STT_OBJECT
            record = ElfSymbol(
                name,
                section_indices[section_name],
                offset,
                symbol_size or 0,
                STB_GLOBAL if symbol_global else STB_LOCAL,
                symbol_type,
            )
            (global_symbols if symbol_global else local_symbols).append(record)
    # Collect only undefined names. Copying the complete metadata/label maps
    # into sets creates several million-label tables just to subtract them.
    undefined_symbols = {name for name in referenced if name not in labels}
    for name in symbol_meta:
        if name not in labels:
            undefined_symbols.add(name)
    for name in sorted(undefined_symbols):
        meta = symbol_meta.get(name)
        global_symbols.append(ElfSymbol(
            name, 0, 0, 0, STB_GLOBAL,
            STT_NOTYPE if meta is None else meta.type,
        ))
    all_symbols = [ElfSymbol.null(), *local_symbols, *global_symbols]
    symbol_indices = {
        symbol.name: index
        for index, symbol in enumerate(all_symbols)
        if symbol.name
    }
    elf_sections: list[ElfSection] = []
    for name in order:
        section = sections[name]
        relocations: list[ElfRelocation] = []
        for relocation in section_relocations[name]:
            symbol_index = symbol_indices.get(relocation.symbol)
            if symbol_index is None:
                raise X86EncodeError(
                    f"relocation symbol {relocation.symbol!r} was not materialized"
                )
            relocations.append(ElfRelocation(
                relocation.offset,
                symbol_index,
                relocation.type,
                relocation.addend,
            ))
        elf_sections.append(ElfSection(
            name,
            section.type,
            section.flags,
            section.align,
            b"" if section.type == SHT_NOBITS else section_payloads[name],
            section.size if section.type == SHT_NOBITS else 0,
            tuple(relocations),
        ))
    result = ElfObject(tuple(elf_sections), tuple(all_symbols))
    if phase_timing is not None:
        phase_timing.complete_assembly(15, started)
    return result
