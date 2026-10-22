"""Frozen be20e4e final-image linker for CI-only differential controls.

The candidate classification block must never regenerate this reference.
Loading this helper executes only when an explicitly selected test requests it.
"""

ORIGINAL_LINK_IMAGE_SOURCE = r'''def _link_static_executable_image(
    objects: list[ElfObject],
    *,
    archives: list[bytes] = (),
    entry: str = "_start",
    base_address: int = _BASE,
    archive_selections: list[tuple[int, str]] | None = None,
    compact_archive_relocations: bool = False,
    _input_store=None,
) -> bytearray:
    """Build a private mutable image, fully validated before publication."""
    if not objects:
        raise ElfError("static ELF link requires at least one explicit object")
    if base_address < 0x10000 or base_address % _PAGE:
        raise ElfError("static ELF base address must be page-aligned and >= 0x10000")
    objects = list(objects)
    machine = objects[0].machine
    boundary_names = ("__init_array_start", "__init_array_end", "__fini_array_start", "__fini_array_end")
    boundary_symbols = [ElfSymbol.null()]
    for name in boundary_names:
        boundary_symbols.append(ElfSymbol(name, SHN_ABS, 0, 0, STB_GLOBAL, STT_NOTYPE))
    objects.append(ElfObject((), tuple(boundary_symbols), machine))
    definitions, undefined = _global_state(objects)
    archive_inputs = archives if _input_store is None else _input_store.archives
    for archive_index, archive_data in enumerate(archive_inputs):
        if entry not in definitions:
            undefined.add(entry)
        selected_names = [] if archive_selections is not None else None
        selected, _remaining = select_archive_members(
            (read_archive(archive_data, compact_relocations=compact_archive_relocations)
             if _input_store is None else _input_store.read_archive(archive_index)),
            undefined,
            already_defined=set(definitions),
            selected_names=selected_names,
        )
        if archive_selections is not None:
            archive_selections.extend((archive_index, name) for name in selected_names)
        objects.extend(selected)
        definitions, undefined = _global_state(objects)
    if any(obj.machine != machine for obj in objects):
        raise ElfError("cannot mix ELF machines in a link")
    if undefined:
        raise ElfError("undefined static ELF symbols: " + ", ".join(sorted(undefined)))
    if entry not in definitions:
        raise ElfError(f"entry symbol {entry!r} is not defined")

    alloc_sections: list[tuple[int, int, ElfSection]] = []
    for object_index, obj in enumerate(objects):
        for section_index, section in enumerate(obj.sections, start=1):
            if not section.flags & SHF_ALLOC:
                if section.relocations:
                    raise ElfError(f"non-alloc section {section.name!r} carries relocations")
                if section.name not in (".note.GNU-stack", ".comment", ".llvm_addrsig"):
                    raise ElfError(f"non-alloc section {section.name!r} is not proven")
                continue
            if section.type == SHT_NOTE:
                raise ElfError(f"allocated NOTE section {section.name!r} is not proven")
            if section.type == SHT_NOBITS and not section.flags & SHF_WRITE:
                raise ElfError(f"NOBITS section {section.name!r} must be writable")
            if section.flags & SHF_TLS and not section.flags & SHF_WRITE:
                raise ElfError(f"TLS section {section.name!r} must be writable")
            alloc_sections.append((object_index, section_index, section))

    # Reserve by input identity and RELA addend, never symbol name. Do not
    # resolve branch symbols during classification: a later bad symbol must
    # not displace the first relocation failure in application order.
    branch_slots: dict[tuple[int, int], dict[tuple[int, int, int], int]] = {}
    got_identities: list[tuple[tuple[str, object], str]] = []
    got_seen: set[tuple[tuple[str, object], str]] = set()
    got_reloc_types = {
        R_X86_64_GOTPCREL,
        R_X86_64_GOTTPOFF,
        R_X86_64_GOTPCRELX,
        R_X86_64_REX_GOTPCRELX,
    }
    if machine == EM_AARCH64:
        got_reloc_types = {311, 312, 541, 542}
    for object_index, section_index, section in alloc_sections:
        for relocation in section.relocations:
            if (machine == EM_AARCH64 and relocation.type == 283
                    and section.flags & SHF_EXECINSTR
                    and not section.flags & SHF_WRITE):
                slots = branch_slots.setdefault((object_index, section_index), {})
                branch_key = (object_index, relocation.symbol_index, relocation.addend)
                if branch_key not in slots:
                    slots[branch_key] = len(slots)
            if relocation.type not in got_reloc_types:
                continue
            identity = _symbol_identity(
                objects, definitions, object_index, relocation.symbol_index
            )
            kind = "tpoff" if relocation.type in ((541, 542) if machine == EM_AARCH64 else (R_X86_64_GOTTPOFF,)) else "address"
            key = (identity, kind)
            if key not in got_seen:
                got_seen.add(key)
                got_identities.append(key)

    metadata_sections = [
        item for item in alloc_sections
        if item[2].name == ".pcc_stackmaps"
    ]
    rx_sections = [
        item for item in alloc_sections
        if not item[2].flags & SHF_WRITE
        and item[2].name != ".pcc_stackmaps"
    ]
    if machine == EM_AARCH64:
        rx_sections.sort(key=lambda item: not bool(item[2].flags & SHF_EXECINSTR))
    rw_file_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_WRITE
        and not item[2].flags & SHF_TLS
        and item[2].type != SHT_NOBITS
    ]
    rw_file_sections.sort(key=lambda item: (
        1 if item[2].name.startswith(".init_array") else 2 if item[2].name.startswith(".fini_array") else 0,
        item[2].name if item[2].name.startswith((".init_array", ".fini_array")) else "",
    ))
    tdata_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_TLS and item[2].type != SHT_NOBITS
    ]
    tbss_sections = [
        item for item in alloc_sections
        if item[2].flags & SHF_TLS and item[2].type == SHT_NOBITS
    ]
    bss_sections = [
        item for item in alloc_sections
        if not item[2].flags & SHF_TLS and item[2].type == SHT_NOBITS
    ]
    if any(item[2].flags & SHF_EXECINSTR for item in rw_file_sections + tdata_sections + tbss_sections + bss_sections):
        raise ElfError("writable executable ELF sections are not supported")

    has_rw = bool(rw_file_sections or tdata_sections or tbss_sections or bss_sections or got_identities)
    has_tls = bool(tdata_sections or tbss_sections)
    phnum = 1 + int(has_rw) + int(has_tls) + int(bool(metadata_sections))
    header_bytes = _ELF_HEADER.size + phnum * _PROGRAM_HEADER.size
    placements: dict[tuple[int, int], _Placement] = {}

    branch_pools: dict[tuple[int, int], tuple[int, int]] = {}
    veneer_pools: list[tuple[int, int]] = []
    veneer_padding: list[tuple[int, int]] = []
    if branch_slots:
        from .elf_aarch64_relocations import veneer_pool_end
    cursor = _align(header_bytes, _PAGE)
    for object_index, section_index, section in rx_sections:
        section_key = (object_index, section_index)
        slot_count = len(branch_slots.get(section_key, {}))
        if slot_count:
            before_pool = _align(cursor, 8)
            veneer_padding.append((cursor, before_pool))
            veneer_pools.append((before_pool, slot_count))
            cursor = veneer_pool_end(before_pool, slot_count)
        aligned = _align(cursor, section.align)
        if branch_slots and section.flags & SHF_EXECINSTR:
            veneer_padding.append((cursor, aligned))
        cursor = aligned
        placements[section_key] = _Placement(cursor, base_address + cursor)
        cursor += section.size
        if slot_count:
            after_pool = _align(cursor, 8)
            veneer_padding.append((cursor, after_pool))
            veneer_pools.append((after_pool, slot_count))
            cursor = veneer_pool_end(after_pool, slot_count)
            branch_pools[section_key] = (before_pool, after_pool)
    rx_file_end = cursor

    rw_start = _align(rx_file_end, _PAGE) if has_rw else 0
    cursor = rw_start
    for object_index, section_index, section in rw_file_sections:
        cursor = _align(cursor, section.align)
        placements[(object_index, section_index)] = _Placement(cursor, base_address + cursor)
        cursor += section.size
    got_offset = _align(cursor, 8)
    got_address = base_address + got_offset
    cursor = got_offset + len(got_identities) * 8

    tls_align = max((item[2].align for item in tdata_sections + tbss_sections), default=1)
    tls_file_start = _align(cursor, tls_align) if has_tls else 0
    if has_tls:
        cursor = tls_file_start
    for object_index, section_index, section in tdata_sections:
        cursor = _align(cursor, section.align)
        placements[(object_index, section_index)] = _Placement(cursor, base_address + cursor)
        cursor += section.size
    tls_file_end = cursor
    memory_cursor = cursor
    for object_index, section_index, section in tbss_sections:
        memory_cursor = _align(memory_cursor, section.align)
        placements[(object_index, section_index)] = _Placement(
            tls_file_end, base_address + memory_cursor
        )
        memory_cursor += section.size
    tls_memory_end = memory_cursor
    for object_index, section_index, section in bss_sections:
        memory_cursor = _align(memory_cursor, section.align)
        placements[(object_index, section_index)] = _Placement(
            cursor, base_address + memory_cursor
        )
        memory_cursor += section.size
    rw_file_end = cursor
    rw_memory_end = memory_cursor

    metadata_file_start = 0
    metadata_file_end = 0
    metadata_address_start = 0
    if metadata_sections:
        # Metadata is read-only and may be much larger than the signed PC-relative
        # window. Keep code, constants and writable globals together first.
        # File and virtual positions differ when the RW segment ends in BSS.
        metadata_align = max(_PAGE, max(item[2].align for item in metadata_sections))
        metadata_file_start = _align(max(rx_file_end, rw_file_end), metadata_align)
        metadata_address_start = _align(max(rx_file_end, rw_memory_end), metadata_align)
        cursor = metadata_file_start
        address_cursor = metadata_address_start
        for object_index, section_index, section in metadata_sections:
            cursor = _align(cursor, section.align)
            address_cursor = _align(address_cursor, section.align)
            placements[(object_index, section_index)] = _Placement(
                cursor, base_address + address_cursor,
            )
            cursor += section.size
            address_cursor += section.size
        metadata_file_end = cursor
    image_size = max(rx_file_end, rw_file_end, metadata_file_end, header_bytes)
    if _input_store is not None:
        _input_store.clear()
    image = bytearray(image_size)
    for object_index, section_index, section in alloc_sections:
        if section.type == SHT_NOBITS:
            continue
        placement = placements[(object_index, section_index)]
        image[placement.file_offset:placement.file_offset + section.size] = section.data

    if veneer_pools:
        from .elf_aarch64_relocations import initialize_veneer_pool
        for padding_start, padding_end in veneer_padding:
            # Complete instructions only; a pre-existing partial instruction
            # at a section end is not made executable by linker padding.
            padding_start = _align(padding_start, 4)
            for padding_offset in range(padding_start, padding_end - 3, 4):
                struct.pack_into("<I", image, padding_offset, 0xD503201F)
        for pool_start, slot_count in veneer_pools:
            initialize_veneer_pool(image, pool_start, slot_count)

    boundaries = {}
    for family in ("init", "fini"):
        selected = [item for item in alloc_sections if item[2].name.startswith("." + family + "_array")]
        begin = min((placements[(oi, si)].address for oi, si, sec in selected), default=0)
        end = max((placements[(oi, si)].address + sec.size for oi, si, sec in selected), default=0)
        boundaries["__" + family + "_array_start"] = begin
        boundaries["__" + family + "_array_end"] = end

    def resolve_symbol(object_index: int, symbol_index: int) -> tuple[int, ElfSymbol]:
        identity = _symbol_identity(objects, definitions, object_index, symbol_index)
        if identity[0] == "weak-zero":
            return 0, objects[object_index].symbols[symbol_index]
        target_object, target_symbol = (
            identity[1] if identity[0] == "local" else definitions[str(identity[1])]
        )
        symbol = objects[target_object].symbols[target_symbol]
        if symbol.section_index == SHN_ABS:
            return boundaries.get(symbol.name, symbol.value), symbol
        if symbol.section_index == SHN_UNDEF:
            return 0, symbol
        placement = placements.get((target_object, symbol.section_index))
        if placement is None:
            raise ElfError(f"symbol {symbol.name!r} is defined in a non-alloc section")
        return placement.address + symbol.value, symbol

    tls_tp = base_address + tls_file_start + _align(tls_memory_end - tls_file_start, tls_align) if has_tls else 0

    def tls_offset(object_index: int, symbol_index: int) -> int:
        address, symbol = resolve_symbol(object_index, symbol_index)
        if symbol.type != STT_TLS:
            raise ElfError(f"TLS relocation targets non-TLS symbol {symbol.name!r}")
        if not has_tls:
            raise ElfError("TLS relocation exists without a PT_TLS image")
        if machine == EM_AARCH64:
            return address - (base_address + tls_file_start) + _align(16, tls_align)
        return address - tls_tp

    got_offsets = {identity: index * 8 for index, identity in enumerate(got_identities)}
    for (identity, kind), relative in got_offsets.items():
        if identity[0] == "weak-zero":
            if kind == "tpoff":
                raise ElfError("GOTTPOFF cannot target an undefined weak symbol")
            value = 0
        else:
            target_object, target_symbol = (
                identity[1] if identity[0] == "local" else definitions[str(identity[1])]
            )
            symbol = objects[target_object].symbols[target_symbol]
            if kind == "tpoff":
                value = tls_offset(target_object, target_symbol)
            else:
                if symbol.type == STT_TLS:
                    raise ElfError(
                        f"ordinary GOT relocation targets TLS symbol {symbol.name!r}"
                    )
                value = resolve_symbol(target_object, target_symbol)[0]
        _write_int(image, got_offset + relative, value, 64, value < 0)

    for object_index, section_index, section in alloc_sections:
        if section.type == SHT_NOBITS and section.relocations:
            raise ElfError(f"NOBITS section {section.name!r} carries relocations")
        placement = placements[(object_index, section_index)]
        for relocation in section.relocations:
            if relocation.type == R_X86_64_NONE:
                continue
            patch = placement.file_offset + relocation.offset
            place = placement.address + relocation.offset
            symbol_address, _symbol = resolve_symbol(
                object_index, relocation.symbol_index
            )
            if machine == EM_AARCH64:
                from .elf_aarch64_relocations import (
                    apply_call26_with_veneer,
                    apply_relocation,
                    veneer_slot_offset,
                )
                target = symbol_address
                if relocation.type in (311, 312, 541, 542):
                    identity = _symbol_identity(objects, definitions, object_index, relocation.symbol_index)
                    kind = "tpoff" if relocation.type in (541, 542) else "address"
                    target = got_address + got_offsets[(identity, kind)]
                elif relocation.type in (549, 551):
                    target = tls_offset(object_index, relocation.symbol_index)
                try:
                    if (relocation.type == 283 and _symbol.type == STT_FUNC
                            and _symbol.section_index != SHN_UNDEF
                            and not -(1 << 27) <= target + relocation.addend - place < (1 << 27)
                            and (object_index, section_index) in branch_pools):
                        slot_index = branch_slots[(object_index, section_index)][
                            (object_index, relocation.symbol_index, relocation.addend)
                        ]
                        pool_starts = branch_pools[(object_index, section_index)]
                        veneer_offsets = tuple(veneer_slot_offset(pool, slot_index)
                                               for pool in pool_starts)
                        apply_call26_with_veneer(image, patch,
                                                target + relocation.addend, place,
                                                veneer_offsets)
                    else:
                        apply_relocation(image, patch, relocation.type,
                                         target + relocation.addend, place)
                except ValueError as exc:
                    raise ElfError(str(exc) + _aarch64_relocation_error_context(
                        object_index, section_index, section.name,
                        relocation.offset, relocation.type,
                        relocation.symbol_index,
                        objects[object_index].symbols[relocation.symbol_index].name,
                        _symbol.name, symbol_address, relocation.addend, target, place,
                        placement.file_offset, placement.address,
                        section.size, image_size,
                    )) from exc
            elif relocation.type == R_X86_64_64:
                _write_int(image, patch, symbol_address + relocation.addend, 64, False)
            elif relocation.type in (R_X86_64_PC32, R_X86_64_PLT32):
                _write_int(image, patch, symbol_address + relocation.addend - place, 32, True)
            elif relocation.type == R_X86_64_32:
                _write_int(image, patch, symbol_address + relocation.addend, 32, False)
            elif relocation.type == R_X86_64_32S:
                _write_int(image, patch, symbol_address + relocation.addend, 32, True)
            elif relocation.type in (
                R_X86_64_GOTPCREL,
                R_X86_64_GOTPCRELX,
                R_X86_64_REX_GOTPCRELX,
                R_X86_64_GOTTPOFF,
            ):
                identity = _symbol_identity(
                    objects, definitions, object_index, relocation.symbol_index
                )
                kind = (
                    "tpoff"
                    if relocation.type == R_X86_64_GOTTPOFF
                    else "address"
                )
                slot = got_address + got_offsets[(identity, kind)]
                _write_int(image, patch, slot + relocation.addend - place, 32, True)
            elif relocation.type == R_X86_64_TPOFF32:
                value = tls_offset(object_index, relocation.symbol_index)
                _write_int(image, patch, value + relocation.addend, 32, True)
            else:
                # _relocation_width made this unreachable unless a newly
                # accepted writer relocation was not deliberately linked.
                raise ElfError(f"relocation type {relocation.type} has no link rule")

    if _input_store is not None:
        _input_store.clear()
    # Validate the exact bytes that will become executable metadata after all
    # function-address relocations have been applied.  The final ET_EXEC drops
    # section headers, so this is the last owned publication boundary at which
    # a malformed or unresolved PC table can still be attributed precisely.
    for object_index, section_index, section in alloc_sections:
        if section.name != ".pcc_stackmaps":
            continue
        placement = placements[(object_index, section_index)]
        payload = bytes(
            image[
                placement.file_offset:
                placement.file_offset + section.size
            ]
        )
        try:
            validate_stack_map_payload(
                payload,
                expected_arch=ARCH_AARCH64 if machine == EM_AARCH64 else ARCH_X86_64,
                final_image=True,
            )
        except PreciseStackMapError as exc:
            raise ElfError(
                f"invalid final .pcc_stackmaps payload: {exc}"
            ) from exc

    entry_object, entry_symbol = definitions[entry]
    entry_record = objects[entry_object].symbols[entry_symbol]
    if entry_record.section_index in (SHN_UNDEF, SHN_ABS):
        raise ElfError(f"entry symbol {entry!r} is not section-defined")
    entry_section = objects[entry_object].sections[entry_record.section_index - 1]
    if not entry_section.flags & SHF_EXECINSTR:
        raise ElfError(f"entry symbol {entry!r} is not in executable code")
    entry_address, _ = resolve_symbol(entry_object, entry_symbol)
    program_headers: list[bytes] = [
        _PROGRAM_HEADER.pack(
            PT_LOAD, PF_R | PF_X, 0, base_address, base_address,
            rx_file_end, rx_file_end, _PAGE,
        )
    ]
    if has_rw:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_LOAD, PF_R | PF_W, rw_start, base_address + rw_start,
            base_address + rw_start, rw_file_end - rw_start,
            rw_memory_end - rw_start, _PAGE,
        ))
    if has_tls:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_TLS, PF_R, tls_file_start, base_address + tls_file_start,
            base_address + tls_file_start, tls_file_end - tls_file_start,
            tls_memory_end - tls_file_start, tls_align,
        ))
    if metadata_sections:
        program_headers.append(_PROGRAM_HEADER.pack(
            PT_LOAD, PF_R, metadata_file_start,
            base_address + metadata_address_start,
            base_address + metadata_address_start,
            metadata_file_end - metadata_file_start,
            metadata_file_end - metadata_file_start,
            _PAGE,
        ))
    ident = ELF_MAGIC + bytes((ELFCLASS64, ELFDATA2LSB, EV_CURRENT, ELFOSABI_SYSV))
    ident += b"\0" * (16 - len(ident))
    image[:_ELF_HEADER.size] = _ELF_HEADER.pack(
        ident, ET_EXEC, machine, EV_CURRENT, entry_address,
        _ELF_HEADER.size, 0, 0, _ELF_HEADER.size,
        _PROGRAM_HEADER.size, len(program_headers), 0, 0, 0,
    )
    phoff = _ELF_HEADER.size
    for program_header in program_headers:
        image[phoff:phoff + _PROGRAM_HEADER.size] = program_header
        phoff += _PROGRAM_HEADER.size
    return image
'''


def original_linker(elf):
    namespace = dict(vars(elf))
    exec(compile(ORIGINAL_LINK_IMAGE_SOURCE,
                 "<be20e4e ELF final-image reference>", "exec"), namespace)
    return namespace["_link_static_executable_image"]
