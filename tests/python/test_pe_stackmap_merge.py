"""Small PE byte/consumer controls; Windows native execution is a separate gate."""

from dataclasses import replace
import struct

import pytest

from pcc.backend import coff_x86_64 as coff
from pcc.backend import pe_x86_64 as pe
from pcc.backend import precise_stackmap as wire


def _pe_budget(*, work=1000, limit=10_000_000):
    from pcc.backend.pe_host_budget import PEHostBudget
    return PEHostBudget(limit, work, set_header_bytes=10, set_entry_bytes=2,
                        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1)


def test_explicit_pe_budget_preserves_bytes_above_aggregate_legacy_cap(monkeypatch):
    objects = [_object("pcc_windows_start"), _object("private_worker", external=False)]
    expected = pe.link_executable(objects)
    monkeypatch.setattr(wire, "MAX_RECORDS", 3)
    with pytest.raises(coff.CoffError, match="too many stack-map records"):
        pe.link_executable(objects)
    budget = _pe_budget(work=32)
    budget.retained_bytes = 19
    assert pe.link_executable(objects, compact_budget=budget) == expected
    assert budget.record_visits == 32  # 4 records, including the suffix planning pass.
    assert budget.retained_bytes == 19
    # The same budget retains cumulative work across a second operation.
    with pytest.raises(coff.CoffError, match="record work budget exceeded"):
        pe.link_executable(objects, compact_budget=budget)
    assert budget.record_visits == 32 and budget.retained_bytes == 19


def test_pe_budget_failure_keeps_retained_storage_and_charged_work():
    objects = [_object("pcc_windows_start"), _object("private_worker", external=False)]
    budget = _pe_budget(work=27)
    budget.retained_bytes = 23
    with pytest.raises(coff.CoffError, match="record work budget exceeded"):
        pe.link_executable(objects, compact_budget=budget)
    assert budget.record_visits == 26
    assert budget.retained_bytes == 23


def test_pe_allocation_failure_restores_storage_before_any_wire_work():
    budget = _pe_budget(limit=100)
    budget.retained_bytes = 17
    with pytest.raises(coff.CoffError, match="budget exceeded"):
        pe.link_executable([_object("pcc_windows_start")], compact_budget=budget)
    assert budget.retained_bytes == 17 and budget.record_visits == 0


def test_rejected_active_budget_keeps_its_existing_operation():
    budget = _pe_budget()
    budget.retained_bytes = 17
    budget.prepare((), ())
    owned = bytearray(b"owned by a different operation")
    budget.retain(owned)
    budget.charge_records(2)
    before = (budget.retained_bytes, dict(budget._buffers), dict(budget.metadata_owners))
    try:
        with pytest.raises(coff.CoffError, match="already owns an operation"):
            pe.link_executable([_object("pcc_windows_start")], compact_budget=budget)
        assert (budget.retained_bytes, budget._buffers, budget.metadata_owners) == before
        assert budget.record_visits == 2 and budget._initial_retained == 17
    finally:
        budget.finish()
    assert budget.retained_bytes == 17


def _object(name, *, roots=3, ordinary=b"", external=True):
    locations = tuple(wire.StackMapLocation(
        kind=wire.LOCATION_STACK_INDIRECT,
        flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
        register=6, base_index=wire.NO_BASE, offset=-8 * (index + 1),
    ) for index in range(roots))
    records = tuple(wire.SafepointRecord(
        safepoint_id=wire.safepoint_id(name, index, kind),
        instruction_offset=offset, kind=kind, locations=locations,
    ) for index, (offset, kind) in enumerate(((1, wire.SAFEPOINT_ENTRY),
                                             (4, wire.SAFEPOINT_CALL))))
    payload = wire.encode_stack_map(wire.PreciseStackMap(
        arch=wire.ARCH_X86_64,
        functions=(wire.FunctionStackMap(
            function_id=wire.function_id(name), function_address=0,
            code_size=16, frame_size=(roots * 8 + 15) & -16, records=records,
        ),),
    ))
    address = wire.function_address_offsets(payload)[0]
    sections = [
        coff.CoffSection(".text", b"\x90" * 15 + b"\xc3", 0x60000020, 16),
        coff.CoffSection(".pcc_stackmaps", payload, 0x40000040, 8,
                         (coff.CoffRelocation(address, 0, coff.ADDR64),)),
    ]
    if ordinary:
        sections.append(coff.CoffSection(".rodata", ordinary, 0x40000040, 1))
    obj = coff.CoffObject(tuple(sections), (coff.CoffSymbol(name, 1, 0, external, True),))
    return coff.parse_object(coff.emit_object(obj))


def _sections(image):
    optional = struct.unpack_from("<I", image, 60)[0] + 24
    count = struct.unpack_from("<H", image, optional - 18)[0]
    sections = {}
    for index in range(count):
        name, size, rva, _raw_size, raw = struct.unpack_from(
            "<8sIIII", image, optional + 240 + index * 40,
        )
        sections[name.rstrip(b"\0").decode()] = (rva, raw, size)
    return optional, sections


def _file_offset(sections, rva):
    for address, raw, size in sections.values():
        if address <= rva < address + size:
            return raw + rva - address
    raise AssertionError((rva, sections))


def _stack_table(image):
    _optional, sections = _sections(image)
    rva, raw, size = sections[".rdata"]
    data = image[raw:raw + size]
    assert data.startswith(wire.MAGIC)
    count, locations = struct.unpack_from("<II", data, 12)
    cursor = wire.HEADER_SIZE
    for _ in range(count):
        records = struct.unpack_from("<I", data, cursor + 24)[0]
        cursor += wire.FUNCTION_SIZE + records * wire.RECORD_SIZE
    return rva, data[:cursor + locations * wire.LOCATION_SIZE]


def test_pe_link_validates_retained_merged_and_final_payloads_without_extra_copy(monkeypatch):
    import sys

    objects = [_object("pcc_windows_start", ordinary=b"ordinary output stays live")]
    expected = pe.link_executable(objects)
    budget = _pe_budget()
    budget.retained_bytes = 19
    original = wire.validate_stack_map_payload
    validations = []

    def checked(payload, *, expected_arch=None, final_image=False, compact_budget=None):
        retained = compact_budget._buffers.get(id(payload)) if compact_budget is not None else None
        if retained is None:
            return original(payload, expected_arch=expected_arch, final_image=final_image,
                            compact_budget=compact_budget)
        assert retained[0] is payload and retained[1] == sys.getsizeof(payload)
        if final_image:
            # The original merged bytes and output section remain distinct
            # owners alongside the immutable address-bound snapshot.
            assert sum(type(row[0]) is bytes for row in budget._buffers.values()) == 2
            assert any(isinstance(row[0], bytearray) and wire.MAGIC in row[0]
                       for row in budget._buffers.values())
        validations.append(final_image)
        peak = budget.retained_bytes + budget._set_bytes(2) + budget.metadata_entry_bytes
        peak += budget._validation_cache_bytes(1, sys.getsizeof(set()))
        peak += budget._list_bytes(3) + 3 * budget.integer_bytes
        previous_limit = budget.limit_bytes
        budget.limit_bytes = peak
        try:
            original(payload, expected_arch=expected_arch, final_image=final_image,
                     compact_budget=compact_budget)
        finally:
            budget.limit_bytes = previous_limit

    monkeypatch.setattr(wire, "validate_stack_map_payload", checked)
    assert pe.link_executable(objects, compact_budget=budget) == expected
    assert validations == [False, True]
    assert budget.retained_bytes == 19 and not budget._buffers
    assert budget.record_visits == 16


def _dir64_sites(image):
    optional, sections = _sections(image)
    rva, size = struct.unpack_from("<II", image, optional + 112 + 5 * 8)
    cursor, end = _file_offset(sections, rva), _file_offset(sections, rva) + size
    sites = []
    while cursor < end:
        page, block_size = struct.unpack_from("<II", image, cursor)
        assert block_size >= 8 and block_size % 4 == 0
        for offset in range(cursor + 8, cursor + block_size, 2):
            word = struct.unpack_from("<H", image, offset)[0]
            if word:
                assert word >> 12 == 10
                sites.append(page + (word & 4095))
        cursor += block_size
    assert cursor == end
    return sites


@pytest.mark.parametrize("budgeted", (False, True))
def test_pe_interns_equal_root_sets_across_input_objects_and_keeps_local_targets(budgeted):
    ordinary = b"ordinary read-only bytes\0"
    first = _object("pcc_windows_start", roots=63, ordinary=ordinary)
    second = _object("private_worker", roots=63, external=False)
    inputs = (first, second)
    originals = tuple(coff.emit_object(obj) for obj in inputs)
    image = pe.link_executable(inputs, compact_budget=_pe_budget() if budgeted else None)
    optional, sections = _sections(image)
    table_rva, table = _stack_table(image)
    decoded = wire.decode_stack_map(table, expected_arch=wire.ARCH_X86_64, final_image=True)
    assert struct.unpack_from("<II", table, 12) == (2, 63)
    expected_size = wire.HEADER_SIZE + 2 * (wire.FUNCTION_SIZE + 2 * wire.RECORD_SIZE) + 63 * wire.LOCATION_SIZE
    assert len(table) == expected_size
    assert sum(len(obj.sections[1].data) for obj in inputs) - len(table) == wire.HEADER_SIZE + 63 * wire.LOCATION_SIZE
    assert originals == tuple(coff.emit_object(obj) for obj in inputs)
    rdata_rva, raw, size = sections[".rdata"]
    assert table_rva == rdata_rva
    assert image[raw:raw + size] == table + ordinary
    image_base = struct.unpack_from("<Q", image, optional + 24)[0]
    text_rva = sections[".text"][0]
    expected = {wire.function_id("pcc_windows_start"): image_base + text_rva,
                wire.function_id("private_worker"): image_base + text_rva + 16}
    assert {function.function_id: function.function_address for function in decoded.functions} == expected
    assert all(len(record.locations) == 63 for function in decoded.functions for record in function.records)
    # Every rewritten function address remains in the loader's DIR64 table.
    assert sorted(_dir64_sites(image)) == [table_rva + offset for offset in wire.function_address_offsets(table)]
    delta = 0x200000000
    rebased = bytearray(image)
    for site in _dir64_sites(image):
        offset = _file_offset(sections, site)
        value = struct.unpack_from("<Q", rebased, offset)[0]
        struct.pack_into("<Q", rebased, offset, value + delta)
    _, relocated_table = _stack_table(bytes(rebased))
    relocated = wire.decode_stack_map(relocated_table, expected_arch=wire.ARCH_X86_64, final_image=True)
    assert {function.function_id: function.function_address for function in relocated.functions} == {
        fid: address + delta for fid, address in expected.items()
    }


@pytest.mark.parametrize("budgeted", (False, True))
def test_merged_pe_table_uses_the_runtime_exact_pc_and_shared_location_indices(monkeypatch, budgeted):
    # Execute the real runtime consumer logic with bounded host memory adapters.
    # This checks its table/index contract, not native code generation or GC.
    from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
    from pcc.runtime.py import freestanding_gc_mapped_roots as runtime

    image = pe.link_executable([_object("pcc_windows_start"), _object("second")],
                               compact_budget=_pe_budget() if budgeted else None)
    _rva, table = _stack_table(image)
    decoded = wire.decode_stack_map(table, expected_arch=wire.ARCH_X86_64, final_image=True)
    visited = []
    frame = object()
    monkeypatch.setattr(runtime, "abi_constant", ABI_CONSTANTS.__getitem__)
    monkeypatch.setattr(runtime, "gc_backend_current", lambda: 3)
    monkeypatch.setattr(runtime, "null", lambda: None)
    monkeypatch.setattr(runtime, "ptr_is_null", lambda value: int(value is None))
    monkeypatch.setattr(runtime, "ptr_add", lambda pointer, offset: (pointer[0], pointer[1] + offset))
    monkeypatch.setattr(runtime, "load_i32", lambda pointer, offset: struct.unpack_from("<i", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "load_i64", lambda pointer, offset: struct.unpack_from("<q", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "unsigned_div_i64", lambda a, b: (a & ((1 << 64) - 1)) // (b & ((1 << 64) - 1)))

    def visit(base, offset, stable, borrowed, mode, resolve):
        visited.append((base, offset, borrowed, mode, resolve))
        return 0

    monkeypatch.setattr(runtime, "pcc_gc_visit_mapped_root_slot", visit)
    for function in decoded.functions:
        for record in function.records:
            visited.clear()
            pc = function.function_address + record.instruction_offset
            assert runtime.pcc_gc_consume_precise_stackmap((table, 0), len(table), pc, frame, 2, 1) == 3
            assert visited == [(frame, -8 * index, 0, 1, 0) for index in range(1, 4)]
            visited.clear()
            assert runtime.pcc_gc_consume_precise_stackmap((table, 0), len(table), pc + 1, frame, 2, 1) == -13
            assert visited == []


@pytest.mark.parametrize("malformation", (
    "missing-relocation", "duplicate-relocation", "wrong-kind", "nonzero-addend",
    "wrong-identity", "wrong-architecture", "undefined-target", "noncode-target",
    "out-of-range-function", "defined-map-symbol",
))
def test_pe_stackmap_merge_fails_closed_on_input_contract_violations(malformation):
    obj = _object("pcc_windows_start")
    section = obj.sections[1]
    relocation = section.relocations[0]
    data = bytearray(section.data)
    if malformation == "missing-relocation":
        section = replace(section, relocations=())
    elif malformation == "duplicate-relocation":
        section = replace(section, relocations=(relocation, relocation))
    elif malformation == "wrong-kind":
        section = replace(section, relocations=(replace(relocation, kind=coff.REL32),))
    elif malformation == "nonzero-addend":
        struct.pack_into("<Q", data, relocation.offset, 1)
        section = replace(section, data=bytes(data))
    elif malformation == "wrong-identity":
        struct.pack_into("<Q", data, wire.HEADER_SIZE, wire.function_id("other"))
        section = replace(section, data=bytes(data))
    elif malformation == "wrong-architecture":
        data[10] = wire.ARCH_AARCH64
        section = replace(section, data=bytes(data))
    elif malformation == "undefined-target":
        obj = replace(obj, symbols=obj.symbols + (coff.CoffSymbol("ExitProcess", 0),))
        section = replace(section, relocations=(replace(relocation, symbol=1),))
    elif malformation == "noncode-target":
        obj = replace(obj, sections=(replace(obj.sections[0], flags=0x40000040), section))
    elif malformation == "out-of-range-function":
        obj = replace(obj, symbols=(replace(obj.symbols[0], value=1),))
    else:
        obj = replace(obj, symbols=obj.symbols + (coff.CoffSymbol("map_alias", 2, 0, False),))
    obj = replace(obj, sections=(obj.sections[0], section))
    with pytest.raises(coff.CoffError, match="stack.map"):
        pe.link_executable([obj])


def test_pe_stackmap_merge_rejects_duplicate_function_identity():
    first = _object("pcc_windows_start")
    second = _object("private_worker", external=False)
    with pytest.raises(coff.CoffError, match="duplicate PE stack-map function id"):
        pe.link_executable([first, second, second])


@pytest.mark.parametrize("budgeted", (False, True))
def test_pe_stackmap_merge_includes_only_selected_archive_members(budgeted):
    from pcc.backend.ar_writer import write_archive

    first = _object("pcc_windows_start")
    first = replace(first, symbols=first.symbols + (coff.CoffSymbol("needed_worker", 0),))
    selected = _object("needed_worker")
    ignored = _object("pcc_windows_start")
    archive = write_archive([("selected.o", coff.emit_object(selected)),
                             ("unselected.o", coff.emit_object(ignored))])
    image = pe.link_executable([first], archives=[archive],
                               compact_budget=_pe_budget() if budgeted else None)
    _rva, table = _stack_table(image)
    decoded = wire.decode_stack_map(table, expected_arch=wire.ARCH_X86_64, final_image=True)
    assert {function.function_id for function in decoded.functions} == {
        wire.function_id("pcc_windows_start"), wire.function_id("needed_worker"),
    }
