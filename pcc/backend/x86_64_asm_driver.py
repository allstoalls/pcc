"""Owned Intel-syntax file driver for the x86_64 Linux self backend.

The self emitter writes a deliberately finite GNU-style assembly dialect.
This module owns that complete file boundary: directives, sections, symbols,
data, instructions, and RELA records become an ``ElfObject`` without invoking
``as``/``cc``.  Any dialect growth fails closed and must be added together with
an encoder differential.
"""

from __future__ import annotations

import struct
import re
from dataclasses import dataclass, field

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
    encode_instruction,
)


@dataclass(frozen=True)
class _Align:
    log2: int
    fill: int


@dataclass(frozen=True)
class _Label:
    name: str


@dataclass(frozen=True)
class _Data:
    payload: bytes


@dataclass(frozen=True)
class _Zero:
    size: int


@dataclass(frozen=True)
class _SymbolData:
    symbol: str
    addend: int


@dataclass(frozen=True)
class _SymbolDifference:
    left: str
    right: str
    width: int


@dataclass(frozen=True)
class _Instruction:
    text: str


@dataclass(frozen=True)
class _SizeHere:
    symbol: str


@dataclass
class _SectionPlan:
    name: str
    type: int
    flags: int
    align: int
    entries: list[object] = field(default_factory=list)


@dataclass
class _SymbolMeta:
    global_: bool = False
    type: int = STT_NOTYPE
    size: int | None = None


@dataclass(frozen=True)
class _PendingRelocation:
    section_name: str
    offset: int
    symbol: str
    type: int
    addend: int


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
    if name not in _SECTION_SPECS or len(parts) not in (1, 3):
        raise X86EncodeError(f"section shape not proven: {line!r}")
    if len(parts) == 1:
        return name
    section_type, flags, _alignment = _SECTION_SPECS[name]
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


def _parse_file(asm_text: str):
    plans: dict[str, _SectionPlan] = {}
    order: list[str] = []
    symbols: dict[str, _SymbolMeta] = {}
    current: _SectionPlan | None = None
    saw_syntax = False

    def switch(name: str) -> None:
        nonlocal current
        spec = _SECTION_SPECS.get(name)
        if spec is None:
            raise X86EncodeError(f"section {name!r} is outside the ELF contract")
        if name not in plans:
            plans[name] = _SectionPlan(name, spec[0], spec[1], spec[2])
            order.append(name)
        current = plans[name]

    for raw_line in asm_text.splitlines():
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
        if line == ".text":
            switch(".text")
            continue
        if line == ".data":
            switch(".data")
            continue
        if line.startswith(".section"):
            switch(_section_from_directive(line))
            continue
        if line.startswith(".globl "):
            name = line.split(None, 1)[1].strip()
            if not name:
                raise X86EncodeError(f"bad .globl directive {line!r}")
            symbols.setdefault(name, _SymbolMeta()).global_ = True
            continue
        if line.startswith(".type "):
            body = line[len(".type "):]
            pieces = [part.strip() for part in body.split(",")]
            if len(pieces) != 2 or pieces[1] not in ("@function", "@object"):
                raise X86EncodeError(f"bad .type directive {line!r}")
            symbol_type = STT_FUNC if pieces[1] == "@function" else STT_OBJECT
            meta = symbols.setdefault(pieces[0], _SymbolMeta())
            if meta.type not in (STT_NOTYPE, symbol_type):
                raise X86EncodeError(
                    f"conflicting .type directives for {pieces[0]!r}"
                )
            meta.type = symbol_type
            continue
        if line.startswith(".size "):
            if current is None:
                raise X86EncodeError(".size before any section")
            body = line[len(".size "):]
            pieces = [part.strip() for part in body.split(",", 1)]
            if len(pieces) != 2:
                raise X86EncodeError(f"bad .size directive {line!r}")
            meta = symbols.setdefault(pieces[0], _SymbolMeta())
            if pieces[1] == ".-" + pieces[0]:
                current.entries.append(_SizeHere(pieces[0]))
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
        if line.startswith(".p2align "):
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
            current.entries.append(_Align(log2, fill))
            continue
        if line.startswith((".byte ", ".short ", ".long ", ".quad ")):
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
                        current.entries.append(_SymbolDifference(difference.group(1), difference.group(2), width))
                        continue
                    if width != 8:
                        raise X86EncodeError(
                            f"symbol-valued {directive} is not proven: {line!r}"
                        )
                    symbol, addend = _symbol_plus_addend(item)
                    current.entries.append(_SymbolData(symbol, addend))
                else:
                    current.entries.append(_Data(_integer_payload(
                        value,
                        width,
                        owner=directive,
                    )))
            continue
        if line.startswith((".float ", ".double ")):
            if current is None:
                raise X86EncodeError("floating data before any section")
            directive, rest = line.split(None, 1)
            fmt = "<f" if directive == ".float" else "<d"
            for item in _data_items(rest):
                try:
                    payload = struct.pack(fmt, float(item))
                except (ValueError, OverflowError) as exc:
                    raise X86EncodeError(f"bad {directive} value {item!r}") from exc
                current.entries.append(_Data(payload))
            continue
        if line.startswith((".zero ", ".space ")):
            if current is None:
                raise X86EncodeError("zero-fill before any section")
            size = _parse_int(line.split(None, 1)[1])
            if size < 0:
                raise X86EncodeError(f"negative zero-fill {line!r}")
            current.entries.append(_Zero(size))
            continue
        if line.startswith(".") and not line.endswith(":"):
            raise X86EncodeError(f"directive not proven: {line!r}")
        if line.endswith(":"):
            if current is None:
                raise X86EncodeError(f"label before any section: {line!r}")
            name = line[:-1].strip()
            if not name:
                raise X86EncodeError("empty label")
            symbols.setdefault(name, _SymbolMeta())
            current.entries.append(_Label(name))
            continue
        if current is None or not current.flags & SHF_EXECINSTR:
            raise X86EncodeError(f"instruction outside .text: {line!r}")
        current.entries.append(_Instruction(line))
    if not saw_syntax:
        raise X86EncodeError(
            "owned x86 assembly must begin with .intel_syntax noprefix"
        )
    return plans, order, symbols


def _measure_sections(plans, order, symbols):
    labels: dict[str, tuple[str, int]] = {}
    sizes: dict[str, int] = {}
    section_sizes: dict[str, int] = {}
    for name in order:
        plan = plans[name]
        offset = 0
        for entry in plan.entries:
            if isinstance(entry, _Align):
                offset = _align(offset, entry.log2)
            elif isinstance(entry, _Label):
                if entry.name in labels:
                    raise X86EncodeError(f"duplicate assembly label {entry.name!r}")
                labels[entry.name] = (name, offset)
            elif isinstance(entry, _Data):
                if plan.type == SHT_NOBITS and entry.payload:
                    raise X86EncodeError(f"NOBITS section {name!r} has file data")
                offset += len(entry.payload)
            elif isinstance(entry, _Zero):
                offset += entry.size
            elif isinstance(entry, _SymbolData):
                if plan.type == SHT_NOBITS:
                    raise X86EncodeError(f"NOBITS section {name!r} has a relocation")
                offset += 8
            elif isinstance(entry, _SymbolDifference):
                if plan.type == SHT_NOBITS:
                    raise X86EncodeError(f"NOBITS section {name!r} has file data")
                offset += entry.width
            elif isinstance(entry, _Instruction):
                encoded = encode_instruction(
                    entry.text, pc=offset, labels={}, section_name=name,
                )
                offset += len(encoded.code)
            elif isinstance(entry, _SizeHere):
                start = labels.get(entry.symbol)
                if start is None or start[0] != name:
                    raise X86EncodeError(
                        f".size references non-local symbol {entry.symbol!r}"
                    )
                computed_size = offset - start[1]
                existing_size = symbols.setdefault(
                    entry.symbol, _SymbolMeta()
                ).size
                if existing_size is not None and existing_size != computed_size:
                    raise X86EncodeError(
                        f"conflicting .size directives for {entry.symbol!r}"
                    )
                if entry.symbol in sizes and sizes[entry.symbol] != computed_size:
                    raise X86EncodeError(
                        f"inconsistent location size for {entry.symbol!r}"
                    )
                sizes[entry.symbol] = computed_size
            else:
                raise X86EncodeError("unknown assembly plan entry")
        section_sizes[name] = offset
    for symbol, size in sizes.items():
        symbols.setdefault(symbol, _SymbolMeta()).size = size
    return labels, section_sizes


def assemble_file(asm_text: str) -> ElfObject:
    """Assemble one complete self-backend file into a validated ElfObject."""
    plans, order, symbol_meta = _parse_file(asm_text)
    labels, measured_sizes = _measure_sections(plans, order, symbol_meta)
    missing_definitions = sorted(set(symbol_meta) - set(labels))
    if missing_definitions:
        raise X86EncodeError(
            "assembly metadata names symbols without definitions: "
            + repr(missing_definitions)
        )
    # GAS deliberately leaves calls to global definitions relocatable so the
    # final static linker, rather than the assembler, owns symbol resolution.
    # Resolve assembler-local labels here; expose globals through RELA even
    # when their definition happens to share this object.
    local_branch_labels = {
        name: location
        for name, location in labels.items()
        if not symbol_meta.get(name, _SymbolMeta()).global_
    }
    pending: list[_PendingRelocation] = []
    section_payloads: dict[str, bytes] = {}
    for name in order:
        plan = plans[name]
        payload = bytearray()
        memory_size = 0
        for entry in plan.entries:
            if isinstance(entry, _Align):
                target = _align(memory_size, entry.log2)
                padding = target - memory_size
                if plan.type != SHT_NOBITS:
                    payload.extend(bytes((entry.fill,)) * padding)
                memory_size = target
            elif isinstance(entry, (_Label, _SizeHere)):
                continue
            elif isinstance(entry, _Data):
                if plan.type != SHT_NOBITS:
                    payload.extend(entry.payload)
                memory_size += len(entry.payload)
            elif isinstance(entry, _Zero):
                if plan.type != SHT_NOBITS:
                    payload.extend(b"\0" * entry.size)
                memory_size += entry.size
            elif isinstance(entry, _SymbolData):
                pending.append(_PendingRelocation(
                    name, memory_size, entry.symbol, R_X86_64_64, entry.addend,
                ))
                payload.extend(b"\0" * 8)
                memory_size += 8
            elif isinstance(entry, _SymbolDifference):
                left = labels.get(entry.left)
                right = labels.get(entry.right)
                if left is None or right is None or left[0] != right[0]:
                    raise X86EncodeError("symbol difference requires definitions in the same section: " + entry.left + " - " + entry.right)
                payload.extend(_integer_payload(left[1] - right[1], entry.width, owner="symbol difference"))
                memory_size += entry.width
            elif isinstance(entry, _Instruction):
                encoded = encode_instruction(
                    entry.text,
                    pc=memory_size,
                    labels=local_branch_labels,
                    section_name=name,
                )
                payload.extend(encoded.code)
                memory_size += len(encoded.code)
                for relocation in encoded.relocations:
                    pending.append(_PendingRelocation(
                        name,
                        relocation.offset,
                        relocation.symbol,
                        relocation.type,
                        relocation.addend,
                    ))
            else:
                raise X86EncodeError("unknown assembly plan entry")
        if memory_size != measured_sizes[name]:
            raise X86EncodeError(
                f"x86 assembly pass size drift in {name}: "
                f"{memory_size} != {measured_sizes[name]}"
            )
        section_payloads[name] = bytes(payload)

    section_indices = {name: index for index, name in enumerate(order, start=1)}
    referenced = {relocation.symbol for relocation in pending}
    local_symbols: list[ElfSymbol] = []
    global_symbols: list[ElfSymbol] = []
    for name, (section_name, offset) in labels.items():
        meta = symbol_meta.get(name, _SymbolMeta())
        if (
            not meta.global_
            and meta.type == STT_NOTYPE
            and meta.size is None
            and name not in referenced
        ):
            # Block/edge labels have already served the two-pass local branch
            # resolver.  GAS does not publish its temporary, unreferenced
            # labels into .symtab, and doing so here would add thousands of
            # irrelevant symbols to bootstrap objects.  Typed function/data
            # labels and any local relocation target remain materialized.
            continue
        section = plans[section_name]
        symbol_type = meta.type
        if section.flags & SHF_TLS:
            symbol_type = STT_TLS
        elif symbol_type == STT_NOTYPE:
            symbol_type = STT_FUNC if section.flags & SHF_EXECINSTR else STT_OBJECT
        record = ElfSymbol(
            name,
            section_indices[section_name],
            offset,
            meta.size or 0,
            STB_GLOBAL if meta.global_ else STB_LOCAL,
            symbol_type,
        )
        (global_symbols if meta.global_ else local_symbols).append(record)
    for name in sorted(referenced - set(labels)):
        global_symbols.append(ElfSymbol(
            name, 0, 0, 0, STB_GLOBAL, STT_NOTYPE,
        ))
    all_symbols = [ElfSymbol.null(), *local_symbols, *global_symbols]
    symbol_indices = {
        symbol.name: index
        for index, symbol in enumerate(all_symbols)
        if symbol.name
    }
    relocations_by_section: dict[str, list[ElfRelocation]] = {
        name: [] for name in order
    }
    for relocation in pending:
        symbol_index = symbol_indices.get(relocation.symbol)
        if symbol_index is None:
            raise X86EncodeError(
                f"relocation symbol {relocation.symbol!r} was not materialized"
            )
        relocations_by_section[relocation.section_name].append(ElfRelocation(
            relocation.offset,
            symbol_index,
            relocation.type,
            relocation.addend,
        ))
    sections: list[ElfSection] = []
    for name in order:
        plan = plans[name]
        payload = section_payloads[name]
        sections.append(ElfSection(
            name,
            plan.type,
            plan.flags,
            plan.align,
            b"" if plan.type == SHT_NOBITS else payload,
            measured_sizes[name] if plan.type == SHT_NOBITS else 0,
            tuple(relocations_by_section[name]),
        ))
    return ElfObject(tuple(sections), tuple(all_symbols))
