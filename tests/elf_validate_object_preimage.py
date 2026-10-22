"""Frozen add0f7ab validator source for host-only differential controls.

The width helper and record classes come from the supplied module namespace.
This oracle must remain independent of the candidate validator's source.
"""

ORIGINAL_VALIDATE_OBJECT_SOURCE = r'''def _validate_object(obj: ElfObject) -> None:
    if obj.machine not in (EM_X86_64, EM_AARCH64):
        raise ElfError("unsupported ELF machine: " + str(obj.machine))
    if not obj.symbols or obj.symbols[0] != ElfSymbol.null():
        raise ElfError("ELF object symbol zero must be the canonical null symbol")
    if len(obj.sections) > _MAX_SECTIONS or len(obj.symbols) > _MAX_SYMBOLS:
        raise ElfError("ELF object exceeds the finite section/symbol limit")
    names: set[str] = set()
    saw_global = False
    for index, section in enumerate(obj.sections, start=1):
        if not section.name or "\0" in section.name or section.name in names:
            raise ElfError(f"invalid or duplicate section name {section.name!r}")
        names.add(section.name)
        if section.type not in (SHT_PROGBITS, SHT_NOBITS, SHT_NOTE):
            raise ElfError(
                f"section {section.name!r} has unsupported type {section.type}"
            )
        if section.align <= 0 or section.align & (section.align - 1):
            raise ElfError(f"section {section.name!r} has invalid alignment")
        if section.type == SHT_NOBITS:
            if section.data or section.mem_size < 0:
                raise ElfError(f"NOBITS section {section.name!r} carries file data")
        elif section.mem_size not in (0, len(section.data)):
            raise ElfError(f"file-backed section {section.name!r} has size drift")
        for relocation in section.relocations:
            if relocation.symbol_index < 0 or relocation.symbol_index >= len(obj.symbols):
                raise ElfError(
                    f"section {section.name!r} relocation has invalid symbol index"
                )
            width = _relocation_width(relocation.type, obj.machine)
            if relocation.offset < 0 or relocation.offset + width > len(section.data):
                raise ElfError(
                    f"section {section.name!r} relocation is outside its payload"
                )
    for index, symbol in enumerate(obj.symbols):
        if index == 0:
            continue
        if symbol.binding not in (STB_LOCAL, STB_GLOBAL, STB_WEAK):
            raise ElfError(f"symbol {symbol.name!r} has unsupported binding")
        if symbol.type not in (STT_NOTYPE, STT_OBJECT, STT_FUNC, STT_SECTION, STT_TLS):
            raise ElfError(f"symbol {symbol.name!r} has unsupported type")
        if symbol.visibility not in (STV_DEFAULT, STV_HIDDEN):
            raise ElfError(f"symbol {symbol.name!r} has unsupported visibility")
        if symbol.binding == STB_LOCAL:
            if saw_global:
                raise ElfError("local symbols must precede global symbols")
        else:
            saw_global = True
        if symbol.section_index == SHN_COMMON:
            raise ElfError(f"COMMON symbol {symbol.name!r} is not supported")
        if symbol.section_index not in (SHN_UNDEF, SHN_ABS) and not (
            1 <= symbol.section_index <= len(obj.sections)
        ):
            raise ElfError(f"symbol {symbol.name!r} has invalid section index")
        if symbol.section_index == SHN_UNDEF and symbol.value != 0:
            raise ElfError(f"undefined symbol {symbol.name!r} has a value")
        if 1 <= symbol.section_index <= len(obj.sections):
            section = obj.sections[symbol.section_index - 1]
            if symbol.value < 0 or symbol.value + symbol.size > section.size:
                raise ElfError(f"symbol {symbol.name!r} is outside its section")
            if symbol.type == STT_TLS and not (section.flags & SHF_TLS):
                raise ElfError(f"TLS symbol {symbol.name!r} is outside a TLS section")

    for section in obj.sections:
        if section.name != ".pcc_stackmaps":
            continue
        if (
            section.type != SHT_PROGBITS
            or section.flags != SHF_ALLOC
            or section.align < 8
        ):
            raise ElfError(
                ".pcc_stackmaps must be an 8-byte-aligned read-only alloc section"
            )
        try:
            address_offsets = function_address_offsets(section.data)
            validate_stack_map_payload(
                section.data,
                expected_arch=ARCH_AARCH64 if obj.machine == EM_AARCH64 else ARCH_X86_64,
                final_image=False,
            )
        except PreciseStackMapError as exc:
            raise ElfError(f"invalid .pcc_stackmaps payload: {exc}") from exc
        relocation_by_offset = {
            relocation.offset: relocation for relocation in section.relocations
        }
        if len(relocation_by_offset) != len(section.relocations):
            raise ElfError("duplicate .pcc_stackmaps relocation offset")
        if set(relocation_by_offset) != set(address_offsets):
            raise ElfError(
                "every stack-map function address needs exactly one relocation"
            )
        for address_offset in address_offsets:
            relocation = relocation_by_offset[address_offset]
            if relocation.type != (257 if obj.machine == EM_AARCH64 else R_X86_64_64) or relocation.addend != 0:
                raise ElfError(
                    "stack-map function address needs a plain R_X86_64_64 relocation"
                )
            if section.data[address_offset:address_offset + 8] != b"\0" * 8:
                raise ElfError(
                    "relocatable stack-map function address must be zero"
                )
        for address_offset in address_offsets:
            relocation = relocation_by_offset[address_offset]
            symbol = obj.symbols[relocation.symbol_index]
            if symbol.section_index in (SHN_UNDEF, SHN_ABS):
                raise ElfError("stack-map function target must be section-defined")
            # The validated function header immediately precedes its address
            # relocation. Read only that header: decoding the entire map here
            # allocates one object per safepoint and referenced location even
            # though the identity check consumes none of those objects.
            stackmap_function_id = _STACK_MAP_FUNCTION.unpack_from(
                section.data, address_offset - 8
            )[0]
            if stackmap_function_id != function_id(symbol.name):
                raise ElfError(
                    "stack-map function id does not match its relocation symbol"
                )

'''


def original_validator(elf):
    namespace = dict(vars(elf))
    exec(compile(ORIGINAL_VALIDATE_OBJECT_SOURCE,
                 '<add0f7ab-original-elf-validator>', 'exec'), namespace)
    return namespace['_validate_object']
