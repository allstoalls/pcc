"""ELF/COFF multi-unit object emission and retained relocation contracts."""

import struct

import pytest

from pcc.backend.precise_stackmap import decode_stack_map, function_address_offsets, function_id
from pcc.backend.relocatable_merge import merge_coff_objects, merge_elf_objects


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


TARGETS = ("aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu",
           "x86_64-pc-windows-msvc")


@pytest.mark.parametrize("target", TARGETS)
def test_c_multi_unit_object_contains_one_stack_map_and_resolved_cross_unit_symbol(tmp_path, target):
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator

    units = [
        ("first.c", f'''target triple = "{target}"
@first_data = internal global i64 17, align 8
define i64 @first() {{
entry:
  %value = load i64, ptr @first_data
  ret i64 %value
}}
''', "i64", {}),
        ("second.c", f'''target triple = "{target}"
declare i64 @first()
@second_data = internal global i64 25, align 32
define i64 @second() {{
entry:
  %a = call i64 @first()
  %b = load i64, ptr @second_data
  %r = add i64 %a, %b
  ret i64 %r
}}
''', "i64", {}),
    ]
    output = tmp_path / "combined.o"
    # Prepared IR owns the target even if the evaluator was created using
    # the host default (e.g. a Mac host emitting a Linux relocatable).
    evaluator = CEvaluator(backend="self")
    evaluator.emit_compiled_units(units, emit_obj=str(output), optimize=False)
    if "windows" in target:
        from pcc.backend.coff_x86_64 import parse_object
        obj = parse_object(output.read_bytes())
        functions = [s for s in obj.symbols if s.external and s.name in ("first", "second")]
        assert len(functions) == 2 and all(s.section for s in functions)
        symbol_indices = {i for i, s in enumerate(obj.symbols) if s in functions}
        address_symbols = lambda sec: {r.symbol for r in sec.relocations}
        assert len(next(s for s in obj.sections if s.name == ".pdata").data) == 24
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable
        obj = parse_relocatable(output.read_bytes())
        functions = [s for s in obj.symbols if s.name in ("first", "second")]
        assert len(functions) == 2 and all(s.section_index for s in functions)
        symbol_indices = {i for i, s in enumerate(obj.symbols) if s in functions}
        address_symbols = lambda sec: {r.symbol_index for r in sec.relocations}
    sections = [s for s in obj.sections if s.name == ".pcc_stackmaps"]
    assert len(sections) == 1
    table = decode_stack_map(sections[0].data, final_image=False)
    assert table.arch == (1 if target.startswith("aarch64") else 2)
    assert {f.function_id for f in table.functions} == {function_id("first"), function_id("second")}
    assert address_symbols(sections[0]) == symbol_indices
    assert {r.offset for r in sections[0].relocations} == set(function_address_offsets(sections[0].data))


def test_elf_merge_rebases_section_symbols_nobits_tls_and_preserves_undefined():
    from pcc.backend.elf_x86_64 import (
        ElfObject, ElfRelocation, ElfSection, ElfSymbol, SHT_PROGBITS, SHT_NOBITS,
        SHF_ALLOC, SHF_WRITE, SHF_TLS, STB_LOCAL, STB_GLOBAL, STT_SECTION, STT_OBJECT,
        STT_TLS, STT_NOTYPE, emit_relocatable, parse_relocatable,
    )

    def unit(name, size, align):
        return ElfObject((
            ElfSection(".data", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, align, b"\0" * 8,
                       relocations=(ElfRelocation(0, 1, 1, 3),)),
            ElfSection(".bss", SHT_NOBITS, SHF_ALLOC | SHF_WRITE, align, mem_size=size),
            ElfSection(".tbss", SHT_NOBITS, SHF_ALLOC | SHF_WRITE | SHF_TLS, align, mem_size=size),
        ), (ElfSymbol.null(), ElfSymbol("", 2, 0, 0, STB_LOCAL, STT_SECTION),
            ElfSymbol(name, 2, 0, size, STB_GLOBAL, STT_OBJECT),
            ElfSymbol(name + "_tls", 3, 0, size, STB_GLOBAL, STT_TLS),
            ElfSymbol("later", 0, 0, 0, STB_GLOBAL, STT_NOTYPE)))

    merged = parse_relocatable(emit_relocatable(merge_elf_objects([
        unit("first", 3, 8), unit("second", 7, 32),
    ])))
    by_name = {s.name: s for s in merged.symbols if s.name}
    assert by_name["first"].value == 0
    assert by_name["second"].value == 32
    assert by_name["second_tls"].value == 32
    assert sum(s.name == "later" for s in merged.symbols) == 1
    assert by_name["later"].section_index == 0
    assert next(s for s in merged.sections if s.name == ".bss").mem_size == 39
    data = next(s for s in merged.sections if s.name == ".data")
    assert [(r.offset, r.addend, merged.symbols[r.symbol_index].value)
            for r in data.relocations] == [(0, 3, 0), (32, 3, 32)]


def test_coff_merge_preserves_local_identity_and_implicit_addends():
    from pcc.backend.coff_x86_64 import (
        CoffObject, CoffSection, CoffSymbol, CoffRelocation, ADDR64, REL32,
        emit_object, parse_object,
    )

    def unit(name, alignment):
        return CoffObject((CoffSection(".data", struct.pack("<Q", 5), 0xC0000040,
                                       alignment, (CoffRelocation(0, 0, ADDR64),)),
                           CoffSection(".text", b"\0" * 4, 0x60000020, 4,
                                       (CoffRelocation(0, 1, REL32),))),
                          (CoffSymbol("local", 1, 0, False),
                           CoffSymbol("entry", 2 if name == "first" else 0),
                           CoffSymbol(name, 1)))

    obj = parse_object(emit_object(merge_coff_objects([unit("first", 8), unit("second", 32)])))
    locals_ = [s for s in obj.symbols if not s.external and s.name.startswith("local.")]
    assert [s.value for s in locals_] == [0, 32]
    assert len({s.name for s in locals_}) == 2
    data = next(s for s in obj.sections if s.name == ".data")
    assert [r.offset for r in data.relocations] == [0, 32]
    assert [struct.unpack_from("<Q", data.data, r.offset)[0] for r in data.relocations] == [5, 5]
    assert len({r.symbol for r in data.relocations}) == 2
    entry = next(i for i, s in enumerate(obj.symbols) if s.name == "entry")
    text = next(s for s in obj.sections if s.name == ".text")
    assert [r.symbol for r in text.relocations] == [entry, entry]


def test_elf_merge_rejects_machine_mismatch_and_duplicate_definitions():
    from pcc.backend.elf_x86_64 import ElfError, ElfObject, ElfSymbol, STB_GLOBAL, STT_NOTYPE, SHN_ABS
    a = ElfObject((), (ElfSymbol.null(), ElfSymbol("same", SHN_ABS, 1, 0, STB_GLOBAL, STT_NOTYPE)))
    with pytest.raises(ElfError, match="duplicate strong"):
        merge_elf_objects([a, a])
    with pytest.raises(ElfError, match="mix ELF machines"):
        merge_elf_objects([a, ElfObject((), (ElfSymbol.null(),), 183)])


def test_stackmap_payload_merge_preserves_architecture_and_rejects_mixing():
    from pcc.backend.precise_stackmap import (
        PreciseStackMap, PreciseStackMapError, encode_stack_map, merge_stack_map_payloads,
    )
    arm = encode_stack_map(PreciseStackMap(1, ()), final_image=False)
    x64 = encode_stack_map(PreciseStackMap(2, ()), final_image=False)
    assert decode_stack_map(merge_stack_map_payloads((x64, x64))[0], final_image=False).arch == 2
    assert merge_stack_map_payloads((arm,))[0] == arm
    with pytest.raises(PreciseStackMapError, match="different targets"):
        merge_stack_map_payloads((arm, x64))


def _duplicate_function_map():
    from pcc.backend.precise_stackmap import (
        PreciseStackMap, FunctionStackMap, SafepointRecord, SAFEPOINT_ENTRY,
        encode_stack_map, safepoint_id,
    )
    entry = SafepointRecord(safepoint_id("helper", 0, SAFEPOINT_ENTRY), 0,
                            SAFEPOINT_ENTRY, ())
    return encode_stack_map(PreciseStackMap(2, (
        FunctionStackMap(function_id("helper"), 0, 4, 16, (entry,)),
    )), final_image=False)


@pytest.mark.parametrize("bindings", [(0, 0), (2, 1), (2, 2)])
def test_elf_maps_preserve_distinct_local_or_overridden_weak_code(bindings):
    from pcc.backend.elf_x86_64 import ElfObject, ElfSection, ElfSymbol, ElfRelocation
    payload = _duplicate_function_map()
    offset = function_address_offsets(payload)[0]

    def unit(binding):
        return ElfObject((ElfSection(".text", 1, 6, 4, b"\xc3\x90\x90\x90"),
                          ElfSection(".pcc_stackmaps", 1, 2, 8, payload,
                                     relocations=(ElfRelocation(offset, 1, 1),))),
                         (ElfSymbol.null(), ElfSymbol("helper", 1, 0, 4, binding, 2)))

    obj = merge_elf_objects([unit(bindings[0]), unit(bindings[1])])
    metadata = next(s for s in obj.sections if s.name == ".pcc_stackmaps")
    table = decode_stack_map(metadata.data, final_image=False)
    assert len(table.functions) == 2
    assert len({f.function_id for f in table.functions}) == 2
    assert len({r.safepoint_id for f in table.functions for r in f.records}) == 2
    mapped = [obj.symbols[r.symbol_index] for r in metadata.relocations]
    assert {s.value for s in mapped} == {0, 4}
    assert {f.function_id for f in table.functions} == {function_id(s.name) for s in mapped}
    if bindings != (0, 0):
        visible = [s for s in obj.symbols if s.name == "helper"]
        assert len(visible) == 1
        assert visible[0].value == (4 if bindings == (2, 1) else 0)


def test_coff_maps_scope_same_named_local_functions():
    from pcc.backend.coff_x86_64 import CoffObject, CoffSection, CoffSymbol, CoffRelocation
    payload = _duplicate_function_map()
    offset = function_address_offsets(payload)[0]
    unit = CoffObject((CoffSection(".text", b"\xc3\x90\x90\x90", 0x60000020, 4),
                       CoffSection(".pcc_stackmaps", payload, 0x40000040, 8,
                                   (CoffRelocation(offset, 0, 1),))),
                      (CoffSymbol("helper", 1, 0, False, True),))
    obj = merge_coff_objects([unit, unit])
    metadata = next(s for s in obj.sections if s.name == ".pcc_stackmaps")
    table = decode_stack_map(metadata.data, final_image=False)
    assert len(table.functions) == 2
    assert len({r.safepoint_id for f in table.functions for r in f.records}) == 2
    assert {obj.symbols[r.symbol].value for r in metadata.relocations} == {0, 4}
