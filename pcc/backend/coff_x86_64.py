"""Owned AMD64 COFF objects and Windows unwind metadata.

Internal x86 assembly is encoded by pcc's existing encoder. This adapter
serializes standard COFF, including explicit addends, long names and extended
relocation counts. It never invokes an assembler or an import-library tool.
"""

import struct
from dataclasses import dataclass

ADDR64 = 1
ADDR32 = 2
ADDR32NB = 3
REL32 = 4
SECTION = 10
SECREL = 11


class CoffError(ValueError):
    pass


@dataclass(frozen=True)
class CoffSymbol:
    name: str
    section: int
    value: int = 0
    external: bool = True
    function: bool = False


@dataclass(frozen=True)
class CoffRelocation:
    offset: int
    symbol: int
    kind: int


@dataclass(frozen=True)
class CoffSection:
    name: str
    data: bytes
    flags: int
    align: int = 1
    relocations: tuple = ()


@dataclass(frozen=True)
class CoffObject:
    sections: tuple
    symbols: tuple


def _width(kind):
    if kind == ADDR64:
        return 8
    if kind == SECTION:
        return 2
    if kind in (ADDR32, ADDR32NB, SECREL) or 4 <= kind <= 9:
        return 4
    raise CoffError("unsupported AMD64 COFF relocation: " + str(kind))


_COFF_CHUNK_BYTES = 8192


def _coff_section_payload(data) -> bytes:
    if type(data) is bytes:
        return data
    # Match bytearray.extend's accepted buffers/sequences and snapshot timing.
    # Production sections already own exact bytes, so they need no copy here.
    copied = bytearray()
    copied.extend(data)
    return bytes(copied)


def _append_coff_chunk(chunks: list, pending: list, pending_size: int, raw: bytes) -> int:
    """Bound small-record owners without copying the growing object prefix.

    Native bytearray.extend allocates and copies its entire previous payload.
    Small records are copied into one bounded batch, then the final image.
    """
    size = len(raw)
    if not size:
        return pending_size
    if size >= _COFF_CHUNK_BYTES:
        if pending:
            chunks.append(b"".join(pending))
            pending.clear()
        chunks.append(raw)
        return 0
    pending.append(raw)
    pending_size += size
    if pending_size >= _COFF_CHUNK_BYTES:
        chunks.append(b"".join(pending))
        pending.clear()
        return 0
    return pending_size


def emit_object(obj: CoffObject) -> bytes:
    strings = bytearray(b"\0\0\0\0")
    string_chunks = [strings]
    string_pending = []
    string_pending_size = 0
    string_size = 4
    names = {}
    for name in [sec.name for sec in obj.sections] + [sym.name for sym in obj.symbols]:
        if "\0" in name:
            raise CoffError("NUL in COFF name")
        if len(name.encode("utf-8")) > 8 and name not in names:
            names[name] = string_size
            raw_name = name.encode("utf-8") + b"\0"
            string_pending_size = _append_coff_chunk(
                string_chunks, string_pending, string_pending_size, raw_name,
            )
            string_size += len(raw_name)
    struct.pack_into("<I", strings, 0, string_size)
    if string_pending:
        string_chunks.append(b"".join(string_pending))
        string_pending.clear()
    headers = bytearray(b"\0" * (20 + len(obj.sections) * 40))
    chunks = [headers]
    pending = []
    pending_size = 0
    size = len(headers)
    for index, sec in enumerate(obj.sections):
        if sec.align <= 0 or sec.align & (sec.align - 1) or sec.align > 8192:
            raise CoffError("invalid COFF section alignment")
        raw = size
        payload = _coff_section_payload(sec.data)
        pending_size = _append_coff_chunk(chunks, pending, pending_size, payload)
        size += len(payload)
        reloc = size
        count = len(sec.relocations)
        flags = sec.flags | ((sec.align.bit_length()) << 20)
        if count >= 65535:
            flags |= 0x01000000
            pending_size = _append_coff_chunk(
                chunks, pending, pending_size, struct.pack("<IIH", count + 1, 0, 0),
            )
            size += 10
        for record in sec.relocations:
            width = _width(record.kind)
            if record.offset < 0 or record.offset + width > len(sec.data):
                raise CoffError("COFF relocation exceeds section")
            if not 0 <= record.symbol < len(obj.symbols):
                raise CoffError("COFF relocation symbol out of range")
            pending_size = _append_coff_chunk(
                chunks, pending, pending_size,
                struct.pack("<IIH", record.offset, record.symbol, record.kind),
            )
            size += 10
        encoded = ("/" + str(names[sec.name])).encode() if sec.name in names else sec.name.encode()
        struct.pack_into("<8sIIIIIIHHI", headers, 20 + index * 40, encoded,
                         0, 0, len(sec.data), raw, reloc if count else 0, 0,
                         min(count, 65535), 0, flags)
    symoff = size
    for symbol in obj.symbols:
        encoded = (struct.pack("<II", 0, names[symbol.name]) if symbol.name in names
                   else symbol.name.encode().ljust(8, b"\0"))
        pending_size = _append_coff_chunk(
            chunks, pending, pending_size,
            struct.pack("<8sIhHBB", encoded, symbol.value, symbol.section,
                        0x20 if symbol.function else 0,
                        2 if symbol.external else 3, 0),
        )
    if pending:
        chunks.append(b"".join(pending))
        pending.clear()
    chunks.extend(string_chunks)
    string_chunks.clear()
    struct.pack_into("<HHIIIHH", headers, 0, 0x8664, len(obj.sections), 0,
                     symoff, len(obj.symbols), 0, 0)
    return b"".join(chunks)


def parse_object(data: bytes) -> CoffObject:
    if len(data) < 20:
        raise CoffError("truncated COFF header")
    machine, count, _stamp, symoff, nsyms, optional, _flags = struct.unpack_from("<HHIIIHH", data)
    if machine != 0x8664 or optional or count > 32767 or 20 + count * 40 > len(data):
        raise CoffError("unsupported AMD64 COFF header")
    stroff = symoff + nsyms * 18
    if symoff < 20 + count * 40 or stroff + 4 > len(data):
        raise CoffError("COFF symbol table outside file")
    strlen = struct.unpack_from("<I", data, stroff)[0]
    if strlen < 4 or stroff + strlen > len(data):
        raise CoffError("truncated COFF string table")
    def string(offset):
        if offset < 4 or offset >= strlen:
            raise CoffError("COFF string offset outside table")
        end = data.find(b"\0", stroff + offset, stroff + strlen)
        if end < 0:
            raise CoffError("unterminated COFF name")
        return data[stroff + offset:end].decode("utf-8")
    symbols = []
    symbol_map = {}
    index = 0
    while index < nsyms:
        name, value, section, kind, storage, aux = struct.unpack_from("<8sIhHBB", data, symoff + index * 18)
        name = string(struct.unpack_from("<I", name, 4)[0]) if name[:4] == b"\0" * 4 else name.rstrip(b"\0").decode()
        if storage not in (2, 3) or section < 0 or section > count:
            raise CoffError("unsupported COFF symbol linkage: " + name)
        if index + aux >= nsyms:
            raise CoffError("truncated auxiliary symbol")
        symbol_map[index] = len(symbols)
        symbols.append(CoffSymbol(name, section, value, storage == 2, bool(kind & 0x20)))
        index += 1 + aux
    sections = []
    for index in range(count):
        name, _vs, _va, size, raw, relptr, _line, nrel, _nl, flags = struct.unpack_from("<8sIIIIIIHHI", data, 20 + index * 40)
        name = name.rstrip(b"\0").decode()
        if name.startswith("/"):
            name = string(int(name[1:]))
        if flags & 0x1000:
            raise CoffError("COMDAT selection is not implemented")
        if raw + size > len(data):
            raise CoffError("COFF section outside file")
        skip = 0
        if flags & 0x01000000:
            if nrel != 65535 or relptr + 10 > len(data):
                raise CoffError("malformed COFF relocation overflow")
            nrel = struct.unpack_from("<I", data, relptr)[0]
            skip = 1
        if relptr + nrel * 10 > len(data):
            raise CoffError("truncated COFF relocation table")
        records = []
        for ordinal in range(skip, nrel):
            offset, symbol, kind = struct.unpack_from("<IIH", data, relptr + ordinal * 10)
            if symbol not in symbol_map or offset + _width(kind) > size:
                raise CoffError("invalid COFF relocation")
            records.append(CoffRelocation(offset, symbol_map[symbol], kind))
        log = (flags >> 20) & 15
        sections.append(CoffSection(name, data[raw:raw + size], flags & ~0x01F00000,
                                    1 << (log - 1) if log else 1, tuple(records)))
    return CoffObject(tuple(sections), tuple(symbols))


def _unwind_source(text):
    lines = []
    procedures = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(".seh_"):
            lines.append(line)
            continue
        op, _, args = stripped.partition(" ")
        if op == ".seh_proc":
            current = [args, [], "", ""]
            procedures.append(current)
            continue
        if current is None:
            raise CoffError("unwind directive outside a function")
        label = ".Lpcc_seh_" + str(len(procedures)) + "_" + str(len(lines))
        lines.append(label + ":")
        if op == ".seh_endprologue":
            current[2] = label
        elif op == ".seh_endproc":
            current[3] = label
            current = None
        else:
            current[1].append((label, op, args))
    if current is not None:
        raise CoffError("unterminated unwind procedure")
    return "\n".join(lines), procedures


def assemble(
    text: str, *, stack_map_plans=None, consume_stack_map_plans=False,
    phase_timing=None,
) -> CoffObject:
    from .x86_64_asm_driver import assemble_file_keeping_labels
    clean, procedures = _unwind_source(text)
    # The unwind markers are unreferenced local labels, which the assembler
    # otherwise leaves out of .symtab; _append_unwind reads their offsets.
    markers = set()
    for _name, actions, prolog_end, end in procedures:
        markers.update(label for label, _op, _args in actions)
        markers.update((prolog_end, end))
    if stack_map_plans is None:
        if phase_timing is None:
            elf = assemble_file_keeping_labels(clean, markers)
        else:
            elf = assemble_file_keeping_labels(clean, markers, phase_timing=phase_timing)
    else:
        from .x86_64_asm_driver import assemble_file_with_stack_maps_keeping_labels
        from .self_backend_x86_64_linux import _asm_symbol, _block_label

        # Reuse the owned target-final packer before the unchanged ELF-to-COFF
        # relocation conversion. SEH labels remain available to _append_unwind.
        if phase_timing is None:
            elf = assemble_file_with_stack_maps_keeping_labels(
                clean, stack_map_plans, markers,
                function_symbol=_asm_symbol, block_label=_block_label,
                consume_stack_map_plans=consume_stack_map_plans,
            )
        else:
            elf = assemble_file_with_stack_maps_keeping_labels(
                clean, stack_map_plans, markers,
                function_symbol=_asm_symbol, block_label=_block_label,
                consume_stack_map_plans=consume_stack_map_plans,
                phase_timing=phase_timing,
            )
    return _coff_from_elf(elf, procedures)


def assemble_builder(
    builder, *, stack_map_plans=None, consume_stack_map_plans=False,
    phase_timing=None,
) -> CoffObject:
    """The COFF object of a module streamed into an unwind-aware builder.

    The builder named its unwind markers and procedures while lines arrived,
    as the text route's directive pass does, so no module text is rebuilt.
    """
    from .x86_64_asm_driver import assemble_builder as assemble_elf
    from .self_backend_x86_64_linux import _asm_symbol, _block_label

    markers = builder.unwind_markers()
    if phase_timing is None:
        elf = assemble_elf(
            builder, markers, stack_map_plans,
            function_symbol=_asm_symbol, block_label=_block_label,
            consume_stack_map_plans=consume_stack_map_plans,
        )
    else:
        elf = assemble_elf(
            builder, markers, stack_map_plans,
            function_symbol=_asm_symbol, block_label=_block_label,
            consume_stack_map_plans=consume_stack_map_plans,
            phase_timing=phase_timing,
        )
    return _coff_from_elf(elf, builder.unwind_procedures)


def _coff_from_elf(elf, procedures) -> CoffObject:
    from .elf_x86_64 import SHF_EXECINSTR, SHF_WRITE, SHF_TLS, STB_LOCAL, STT_FUNC
    symbols = [CoffSymbol(sym.name, sym.section_index, sym.value,
                          sym.binding != STB_LOCAL, sym.type == STT_FUNC) for sym in elf.symbols[1:]]
    sections = []
    for sec in elf.sections:
        name = ".tls$" + ("B" if sec.name == ".tbss" else "A") if sec.flags & SHF_TLS else sec.name
        if sec.name.startswith(".init_array."):
            name = ".pccinit$" + sec.name.rsplit(".", 1)[1]
        elif sec.name.startswith(".fini_array."):
            name = ".pccfini$" + sec.name.rsplit(".", 1)[1]
        flags = (0x60000020 if sec.flags & SHF_EXECINSTR else
                 0xC0000040 if sec.flags & SHF_WRITE else 0x40000040)
        sections.append(CoffSection(name, sec.data or b"\0" * sec.mem_size, flags, sec.align))
    got = bytearray()
    got_relocs = []
    got_slots = {}
    converted = []
    got_section = len(sections) + 1
    for sec, source in zip(sections, elf.sections):
        payload = bytearray(sec.data)
        records = []
        for r in source.relocations:
            symbol = r.symbol_index - 1
            addend = r.addend
            if r.type in (9, 22, 41, 42):
                key = (symbol, r.type == 22)
                if key not in got_slots:
                    slot = len(got)
                    got.extend(b"\0" * 8)
                    got_relocs.append(CoffRelocation(slot, symbol, SECREL if r.type == 22 else ADDR64))
                    got_slots[key] = len(symbols)
                    symbols.append(CoffSymbol(".Lpcc_got_" + str(slot), got_section, slot, False))
                symbol = got_slots[key]
                kind, addend = REL32, addend + 4
            elif r.type in (2, 4):
                kind, addend = REL32, addend + 4
            elif r.type == 1:
                kind = ADDR64
            elif r.type == 23:
                # ELF's local-exec offset becomes the Windows module TLS
                # offset. The 32-bit MOV zero-extends the unsigned SECREL.
                kind = SECREL
            elif r.type in (10, 11):
                kind = ADDR32
            else:
                raise CoffError("unmapped x86 internal relocation: " + str(r.type))
            width = _width(kind)
            payload[r.offset:r.offset + width] = (addend & ((1 << (width * 8)) - 1)).to_bytes(width, "little")
            records.append(CoffRelocation(r.offset, symbol, kind))
        converted.append(CoffSection(sec.name, bytes(payload), sec.flags, sec.align, tuple(records)))
    if got:
        converted.append(CoffSection(".rdata$got", bytes(got), 0x40000040, 8, tuple(got_relocs)))
    if procedures:
        _append_unwind(converted, symbols, procedures)
    return CoffObject(tuple(converted), tuple(symbols))


def _append_unwind(sections, symbols, procedures):
    by_name = {sym.name: (index, sym) for index, sym in enumerate(symbols)}
    xdata = bytearray()
    pdata = bytearray()
    relocations = []
    xindex = len(sections) + 1
    registers = {"rax": 0, "rcx": 1, "rdx": 2, "rbx": 3, "rsp": 4, "rbp": 5,
                 "rsi": 6, "rdi": 7, "r8": 8, "r9": 9, "r10": 10, "r11": 11,
                 "r12": 12, "r13": 13, "r14": 14, "r15": 15}
    for name, actions, prolog_end, end in procedures:
        begin_index, begin = by_name[name]
        if not prolog_end or not end:
            raise CoffError("incomplete unwind data for " + name)
        prolog_size = by_name[prolog_end][1].value - begin.value
        if not 0 <= prolog_size <= 255:
            raise CoffError("Win64 prologue exceeds unwind encoding")
        codes = bytearray()
        frame = 0
        for label, op, args in reversed(actions):
            offset = by_name[label][1].value - begin.value
            fields = [part.strip() for part in args.split(",")]
            if op == ".seh_pushreg":
                codes.extend(bytes((offset, registers[fields[0]] << 4)))
            elif op == ".seh_stackalloc":
                size = int(fields[0])
                if size <= 128:
                    codes.extend(bytes((offset, (((size - 8) // 8) << 4) | 2)))
                elif size // 8 <= 65535:
                    codes.extend(bytes((offset, 1)) + struct.pack("<H", size // 8))
                else:
                    codes.extend(bytes((offset, 17)) + struct.pack("<I", size))
            elif op == ".seh_setframe":
                frame = registers[fields[0]] | ((int(fields[1]) // 16) << 4)
                codes.extend(bytes((offset, 3)))
            elif op in (".seh_savereg", ".seh_savexmm"):
                xmm = op == ".seh_savexmm"
                register = int(fields[0][3:]) if xmm else registers[fields[0]]
                scaled = int(fields[1]) // (16 if xmm else 8)
                if scaled > 65535:
                    codes.extend(bytes((offset, (register << 4) | (9 if xmm else 5))) + struct.pack("<I", int(fields[1])))
                else:
                    codes.extend(bytes((offset, (register << 4) | (8 if xmm else 4))) + struct.pack("<H", scaled))
            else:
                raise CoffError("unsupported unwind directive " + op)
        count = len(codes) // 2
        if count > 255:
            raise CoffError("too many unwind slots")
        xdata.extend(b"\0" * ((-len(xdata)) & 3))
        unwind_symbol = len(symbols)
        symbols.append(CoffSymbol(".Lunwind_" + name, xindex, len(xdata), False))
        xdata.extend(bytes((1, prolog_size, count, frame)) + codes)
        if count & 1:
            xdata.extend(b"\0\0")
        base = len(pdata)
        pdata.extend(b"\0" * 12)
        for offset, symbol in ((0, begin_index), (4, by_name[end][0]), (8, unwind_symbol)):
            relocations.append(CoffRelocation(base + offset, symbol, ADDR32NB))
    sections.append(CoffSection(".xdata", bytes(xdata), 0x40000040, 4))
    sections.append(CoffSection(".pdata", bytes(pdata), 0x40000040, 4, tuple(relocations)))


def assemble_builder_object(
    builder, *, stack_map_plans=None, consume_stack_map_plans=False,
    phase_timing=None,
) -> bytes:
    started = phase_timing.start() if phase_timing is not None else 0
    if phase_timing is None:
        obj = assemble_builder(
            builder, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=consume_stack_map_plans,
        )
    else:
        obj = assemble_builder(
            builder, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=consume_stack_map_plans,
            phase_timing=phase_timing,
        )
        phase_timing.add(9, started)
        started = phase_timing.start()
    encoded = emit_object(obj)
    if phase_timing is not None:
        phase_timing.add(10, started)
    return encoded


def assemble_object(
    text: str, *, stack_map_plans=None, consume_stack_map_plans=False,
    phase_timing=None,
) -> bytes:
    started = phase_timing.start() if phase_timing is not None else 0
    if stack_map_plans is None:
        if phase_timing is None:
            obj = assemble(text)
        else:
            obj = assemble(text, phase_timing=phase_timing)
    elif phase_timing is None:
        obj = assemble(
            text, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=consume_stack_map_plans,
        )
    else:
        obj = assemble(
            text, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=consume_stack_map_plans,
            phase_timing=phase_timing,
        )
    if phase_timing is not None:
        phase_timing.add(9, started)
        started = phase_timing.start()
    encoded = emit_object(obj)
    if phase_timing is not None:
        phase_timing.add(10, started)
    return encoded
