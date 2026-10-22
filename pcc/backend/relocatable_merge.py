"""Owned relocatable links for the ELF and COFF target object models.

Keep translation units separate until symbol indices and section placements
are known. In particular, a precise stack-map section contains one table,
not a byte concatenation of the tables emitted by its input units.
"""

from .precise_stackmap import (
    ARCH_AARCH64,
    ARCH_X86_64,
    FUNCTION_SIZE,
    RECORD_SIZE,
    _scan_stack_map_payload,
    function_address_offsets,
    function_id,
    merge_stack_map_payloads,
    scoped_stable_id,
    validate_stack_map_payload,
)


def _local_name(name, oi, si, occupied):
    candidate = name + ".__pcc_object_" + str(oi) + "_" + str(si)
    while candidate in occupied:
        candidate += "_"
    occupied.add(candidate)
    return candidate


def _stack_table(inputs, arch, error):
    payloads = []
    targets = {}
    for data, records in inputs:
        validate_stack_map_payload(data, expected_arch=arch, final_image=False)
        offsets = function_address_offsets(data)
        by_offset = {offset: (symbol, old, name) for offset, symbol, old, name in records}
        if len(by_offset) != len(records) or set(by_offset) != set(offsets):
            raise error("stack-map addresses and relocations disagree")
        scoped = bytearray(data)
        _count, spans, _table_start, _table_count = _scan_stack_map_payload(data)
        for offset, span in zip(offsets, spans):
            symbol, old_name, name = by_offset[offset]
            if data[offset:offset + 8] != b"\0" * 8:
                raise error("relocatable stack-map address must be zero")
            if span[0] != function_id(old_name):
                raise error("stack-map function id differs from target symbol")
            identity = function_id(name)
            if identity in targets:
                raise error("duplicate stable function id in stack-map inputs")
            targets[identity] = symbol
            if old_name != name:
                start = span[1]
                scoped[start:start + 8] = identity.to_bytes(8, "little")
                # v2 has a 32-byte function header followed by fixed-size
                # 32-byte safepoint records. Scope those IDs too: uniqueness
                # is across the entire table, not just within a function.
                record_count = (span[2] - start - FUNCTION_SIZE) // RECORD_SIZE
                for ordinal in range(record_count):
                    position = start + FUNCTION_SIZE + ordinal * RECORD_SIZE
                    old_id = int.from_bytes(data[position:position + 8], "little")
                    record_id = scoped_stable_id(
                        "relocatable-safepoint", name, str(old_id)
                    )
                    scoped[position:position + 8] = record_id.to_bytes(8, "little")
        payloads.append(bytes(scoped))
    payload, offsets = merge_stack_map_payloads(tuple(payloads))
    validate_stack_map_payload(payload, expected_arch=arch, final_image=False)
    return payload, [(offset, targets[fid]) for fid, offset in offsets]


def merge_elf_objects(objects):
    """Return one ET_REL model, preserving unresolved and weak symbols."""
    from .elf_x86_64 import (
        EM_AARCH64, ElfError, ElfObject, ElfRelocation, ElfSection, ElfSymbol,
        SHN_ABS, SHN_UNDEF, SHT_NOBITS, STB_GLOBAL, STB_LOCAL, STB_WEAK,
        _global_state,
    )

    objects = list(objects)
    if not objects:
        raise ElfError("relocatable ELF link requires an input")
    machine = objects[0].machine
    if any(obj.machine != machine for obj in objects):
        raise ElfError("cannot mix ELF machines in a relocatable link")
    definitions, _undefined = _global_state(objects)
    groups = {}
    placements = {}
    # Rows: section prototype, chunk list, logical length, max alignment.
    rows = []
    for oi, obj in enumerate(objects):
        for si, sec in enumerate(obj.sections, 1):
            if sec.name not in groups:
                groups[sec.name] = len(rows)
                rows.append([sec, [], 0, sec.align])
            index = groups[sec.name]
            row = rows[index]
            first = row[0]
            if (first.type, first.flags) != (sec.type, sec.flags):
                raise ElfError("incompatible ELF sections: " + sec.name)
            row[3] = max(row[3], sec.align)
            if sec.name == ".pcc_stackmaps":
                placements[(oi, si)] = (index + 1, 0)
                continue
            base = (row[2] + sec.align - 1) & -sec.align
            placements[(oi, si)] = (index + 1, base)
            if sec.type != SHT_NOBITS:
                row[1].append(b"\0" * (base - row[2]))
                row[1].append(sec.data)
            row[2] = base + sec.size

    # Every symbol gets an explicit input -> output index. Section symbols
    # retain their own rebased value, so section-relative RELA addends need
    # no special rewriting and locals with the same name stay distinct.
    symbols = [ElfSymbol.null()]
    symbol_map = {}
    definition_map = {}
    globals_by_name = {}
    global_order = []
    occupied = {s.name for obj in objects for s in obj.symbols if s.name}
    name_counts = {}
    for obj in objects:
        for sym in obj.symbols:
            if sym.section_index != SHN_UNDEF:
                name_counts[sym.name] = name_counts.get(sym.name, 0) + 1

    def rebase(oi, symbol):
        section, value = symbol.section_index, symbol.value
        if section not in (SHN_UNDEF, SHN_ABS):
            if objects[oi].sections[section - 1].name == ".pcc_stackmaps":
                raise ElfError("stack-map sections must not define symbols")
            section, base = placements[(oi, section)]
            value += base
        return ElfSymbol(symbol.name, section, value, symbol.size,
                         symbol.binding, symbol.type, symbol.visibility)

    for oi, obj in enumerate(objects):
        symbol_map[(oi, 0)] = 0
        for si, sym in enumerate(obj.symbols[1:], 1):
            if sym.binding == STB_LOCAL:
                symbol_map[(oi, si)] = len(symbols)
                definition_map[(oi, si)] = len(symbols)
                name = _local_name(sym.name, oi, si, occupied) if sym.name and name_counts.get(sym.name, 0) > 1 else sym.name
                symbols.append(rebase(oi, ElfSymbol(name, sym.section_index, sym.value,
                                                   sym.size, sym.binding, sym.type, sym.visibility)))
            else:
                if not sym.name:
                    raise ElfError("external ELF symbol must have a name")
                if sym.name not in globals_by_name:
                    globals_by_name[sym.name] = (oi, si)
                    global_order.append(sym.name)
                elif sym.binding == STB_GLOBAL:
                    previous = globals_by_name[sym.name]
                    if objects[previous[0]].symbols[previous[1]].binding == STB_WEAK:
                        globals_by_name[sym.name] = (oi, si)
                if sym.section_index != SHN_UNDEF and definitions.get(sym.name) != (oi, si):
                    # Keep the physical code of an overridden weak function
                    # addressable for its own metadata. Ordinary references
                    # still bind to the prevailing global definition.
                    name = _local_name(sym.name, oi, si, occupied)
                    definition_map[(oi, si)] = len(symbols)
                    symbols.append(rebase(oi, ElfSymbol(name, sym.section_index, sym.value,
                                                       sym.size, STB_LOCAL, sym.type, sym.visibility)))
    global_indices = {}
    for name in global_order:
        oi, si = definitions.get(name, globals_by_name[name])
        global_indices[name] = len(symbols)
        definition_map[(oi, si)] = len(symbols)
        symbols.append(rebase(oi, objects[oi].symbols[si]))
    for oi, obj in enumerate(objects):
        for si, sym in enumerate(obj.symbols[1:], 1):
            if sym.binding != STB_LOCAL:
                symbol_map[(oi, si)] = global_indices[sym.name]

    relocations = [[] for _row in rows]
    stack_inputs = []
    stack_kind = 257 if machine == EM_AARCH64 else 1
    for oi, obj in enumerate(objects):
        for si, sec in enumerate(obj.sections, 1):
            index, base = placements[(oi, si)]
            if sec.name == ".pcc_stackmaps":
                records = []
                for reloc in sec.relocations:
                    if reloc.type != stack_kind or reloc.addend:
                        raise ElfError("stack-map address needs a plain absolute relocation")
                    symbol = definition_map[(oi, reloc.symbol_index)]
                    records.append((reloc.offset, symbol, obj.symbols[reloc.symbol_index].name,
                                    symbols[symbol].name))
                stack_inputs.append((sec.data, records))
            else:
                for reloc in sec.relocations:
                    relocations[index - 1].append(ElfRelocation(
                        base + reloc.offset, symbol_map[(oi, reloc.symbol_index)],
                        reloc.type, reloc.addend,
                    ))
    if stack_inputs:
        data, addresses = _stack_table(
            stack_inputs, ARCH_AARCH64 if machine == EM_AARCH64 else ARCH_X86_64,
            ElfError,
        )
        index = groups[".pcc_stackmaps"]
        rows[index][1] = [data]
        rows[index][2] = len(data)
        relocations[index] = [ElfRelocation(offset, symbol, stack_kind)
                              for offset, symbol in addresses]
    sections = []
    for index, row in enumerate(rows):
        sec, chunks, size, align = row
        sections.append(ElfSection(
            sec.name, sec.type, sec.flags, align, b"".join(chunks),
            size if sec.type == SHT_NOBITS else 0, tuple(relocations[index]),
        ))
    return ElfObject(tuple(sections), tuple(symbols), machine)


def merge_coff_objects(objects):
    """Return one AMD64 COFF model with rebased implicit-addend relocations."""
    from .coff_x86_64 import (
        ADDR64, CoffError, CoffObject, CoffRelocation, CoffSection, CoffSymbol,
    )

    objects = list(objects)
    if not objects:
        raise CoffError("relocatable COFF link requires an input")
    groups = {}
    rows = []
    placements = {}
    definitions = {}
    references = {}
    global_order = []
    for oi, obj in enumerate(objects):
        for si, sec in enumerate(obj.sections, 1):
            # File alignment/overflow-relocation flags are serialization
            # details; permissions and section semantics must still agree.
            flags = sec.flags & ~0x01F00000
            if sec.name not in groups:
                groups[sec.name] = len(rows)
                rows.append([sec.name, [], flags, sec.align, 0])
            index = groups[sec.name]
            row = rows[index]
            if row[2] != flags:
                raise CoffError("incompatible COFF sections: " + sec.name)
            row[3] = max(row[3], sec.align)
            if sec.name == ".pcc_stackmaps":
                placements[(oi, si)] = (index + 1, 0)
                continue
            base = (row[4] + sec.align - 1) & -sec.align
            row[1].extend((b"\0" * (base - row[4]), sec.data))
            row[4] = base + len(sec.data)
            placements[(oi, si)] = (index + 1, base)
        for si, sym in enumerate(obj.symbols):
            if not sym.external:
                continue
            if sym.name not in references:
                references[sym.name] = (oi, si)
                global_order.append(sym.name)
            if sym.section:
                if sym.name in definitions:
                    raise CoffError("duplicate COFF definition: " + sym.name)
                definitions[sym.name] = (oi, si)

    def rebase(oi, sym):
        section, value = sym.section, sym.value
        if section:
            if objects[oi].sections[section - 1].name == ".pcc_stackmaps":
                raise CoffError("stack-map sections must not define symbols")
            section, base = placements[(oi, section)]
            value += base
        return CoffSymbol(sym.name, section, value, sym.external, sym.function)

    symbols = []
    symbol_map = {}
    occupied = {s.name for obj in objects for s in obj.symbols if s.name}
    name_counts = {}
    for obj in objects:
        for sym in obj.symbols:
            if sym.section:
                name_counts[sym.name] = name_counts.get(sym.name, 0) + 1
    for oi, obj in enumerate(objects):
        for si, sym in enumerate(obj.symbols):
            if not sym.external:
                symbol_map[(oi, si)] = len(symbols)
                name = _local_name(sym.name, oi, si, occupied) if sym.name and name_counts.get(sym.name, 0) > 1 else sym.name
                symbols.append(rebase(oi, CoffSymbol(name, sym.section, sym.value, False, sym.function)))
    global_indices = {}
    for name in global_order:
        oi, si = definitions.get(name, references[name])
        global_indices[name] = len(symbols)
        symbols.append(rebase(oi, objects[oi].symbols[si]))
    for oi, obj in enumerate(objects):
        for si, sym in enumerate(obj.symbols):
            if sym.external:
                symbol_map[(oi, si)] = global_indices[sym.name]

    relocations = [[] for _row in rows]
    stack_inputs = []
    for oi, obj in enumerate(objects):
        for si, sec in enumerate(obj.sections, 1):
            index, base = placements[(oi, si)]
            if sec.name == ".pcc_stackmaps":
                records = []
                for reloc in sec.relocations:
                    if reloc.kind != ADDR64:
                        raise CoffError("stack-map address needs an ADDR64 relocation")
                    symbol = symbol_map[(oi, reloc.symbol)]
                    if not symbols[symbol].section:
                        raise CoffError("stack-map function must be section-defined")
                    records.append((reloc.offset, symbol, obj.symbols[reloc.symbol].name,
                                    symbols[symbol].name))
                stack_inputs.append((sec.data, records))
            else:
                for reloc in sec.relocations:
                    relocations[index - 1].append(CoffRelocation(
                        base + reloc.offset, symbol_map[(oi, reloc.symbol)], reloc.kind,
                    ))
    if stack_inputs:
        data, addresses = _stack_table(stack_inputs, ARCH_X86_64, CoffError)
        index = groups[".pcc_stackmaps"]
        rows[index][1] = [data]
        relocations[index] = [CoffRelocation(offset, symbol, ADDR64)
                              for offset, symbol in addresses]
    sections = tuple(CoffSection(name, b"".join(chunks), flags, align,
                                 tuple(relocations[index]))
                     for index, (name, chunks, flags, align, _size) in enumerate(rows))
    return CoffObject(sections, tuple(symbols))
