"""Host-only integration contracts for file-owned source payload windows."""

from dataclasses import replace
import io
import sys

import pytest

from pcc.backend import (
    macho_codesign as codesign,
    macho_exec,
    macho_host_output as output,
    macho_host_payload as payloads,
    macho_host_prepare as host,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
    precise_stackmap as maps,
)
from tests.python.test_macho_consuming_join import _section_targets
from tests.python.test_macho_owned_output import _factory_budget, _stack_inputs
from tests.python.test_macho_windowed_parity import _write_pcos


def _data(size=4096, *, index=0):
    return obj.Section(
        "__data" + str(index), "__DATA", data=bytes([index + 1]) * size,
        align_log2=3, symbols=(obj.TextSymbol("_main", 0),) if not index else (),
    )


def _row():
    return native._FINAL_LINK_RELOCATION.pack(0, 0, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED)


def _stage(builders, budget, row_builders=None):
    """Use real admission, authenticated anonymous stores and detached metadata.

    Builders avoid retaining an aggregate Section/payload graph in the test.
    The production factory is unchanged; tests may lower its cap only after
    all original parts have been written and retired.
    """
    ledger = host._Ledger(budget)
    rows = host._StagedRelocationRows(len(builders), budget, ledger.keep)
    store = payloads._StagedSectionPayloads(len(builders), budget, ledger.keep)
    descriptors = []
    try:
        for index, build in enumerate(builders):
            section = build()
            descriptors.append(host._StagedSourceSection(
                section.sectname, section.segname, len(section.data),
                section.align_log2, section.flags, section.symbols,
                section.data_in_code, section.zerofill_size, False,
            ))
            # Copy a possible interned builder literal into exclusively owned
            # bytes, then retire the authoring Section before spool admission.
            parts = [bytes(bytearray(section.data))]
            section = None
            ledger.keep(sum(ledger.immutable(len(part)) for part in parts))
            ledger.retained -= store.write_owned_parts(parts)
            rows.write_owned(row_builders[index]() if row_builders else b"")
        return descriptors, rows.seal(), store.seal()
    except BaseException:
        rows.close()
        store.close()
        raise


def _source(staged, budget):
    descriptors, rows, store = staged
    return native.OwnedMergedSourceView(
        descriptors, compact_budget=budget,
        _row_storage=rows, _payload_storage=store,
    )


def _simple_source(budget):
    return _source(_stage((lambda: _data(),), budget, (_row,)), budget)


def _signature_matches(image, budget):
    signature = codesign.parse_signature(image)
    output.check_signature_verification(image, signature, budget)
    with memoryview(image) as whole, whole[:signature.dataoff] as prefix:
        expected = codesign.build_signature(
            prefix, identifier=signature.identifier,
            exec_seg_base=signature.exec_seg_base,
            exec_seg_limit=signature.exec_seg_limit,
            exec_seg_flags=signature.exec_seg_flags,
        )
    assert image[signature.dataoff:] == expected


def test_source_validation_fits_one_window_below_aggregate_payload_bytes(monkeypatch):
    budget = _factory_budget()
    budget.retained_bytes = 137
    staged = _stage(tuple(lambda i=i: _data(65536, index=i) for i in range(8)), budget)
    descriptors, rows, store = staged
    assert store.logical_bytes == 8 * 65536
    assert all(type(section) is host._StagedSourceSection for section in descriptors)
    assert all(not hasattr(section, "data") for section in descriptors)
    budget.limit_bytes = 256 * 1024
    assert store.max_window_bytes < budget.limit_bytes < store.logical_bytes
    metadata_calls, payload_calls, admissions = [], [], []
    names = native._validate_source_section_names
    section_rows = native._final_section_rows
    validate = native._validate_section
    check = budget.check_peak

    def metadata(section, *args):
        assert not hasattr(section, "data")
        assert store.read_bytes == store.active_bytes == 0
        metadata_calls.append(section.sectname)
        return names(section, *args)

    def layout(sections, data, *args, **kwargs):
        assert data is None
        assert kwargs["payload_sizes"] == (65536,) * 8
        assert store.read_bytes == store.active_bytes == 0
        return section_rows(sections, data, *args, **kwargs)

    def checked(section, **kwargs):
        assert type(section.data) is bytes and len(section.data) == 65536
        assert store.active_bytes == store.backing_bytes(len(payload_calls))
        assert budget.retained_bytes > store.active_bytes
        payload_calls.append(section.sectname)
        return validate(section, **kwargs)

    def admitted(raw, *, records=0, entries=0, label="stack-map"):
        admissions.append((label, raw + budget.retained_bytes))
        return check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(native, "_validate_source_section_names", metadata)
    monkeypatch.setattr(native, "_final_section_rows", layout)
    monkeypatch.setattr(native, "_validate_section", checked)
    monkeypatch.setattr(budget, "check_peak", admitted)
    source = _source(staged, budget)
    try:
        assert metadata_calls == payload_calls == ["__data" + str(i) for i in range(8)]
        assert len([label for label, _ in admissions
                    if label == "Mach-O source payload validation replay"]) == 8
        assert max(amount for _, amount in admissions) < budget.limit_bytes
        assert source._source_sections == ()
        assert source._payloads == [b""] * 8
        assert source._raw_relocations == [b""] * 8
        assert store.read_bytes == store.logical_bytes and store.consumed == 0
        assert store.active_bytes == rows.active_bytes == 0
        assert budget.retained_bytes == 137
    finally:
        host.close_staged_source_rows(source)


def test_source_replay_refusal_precedes_payload_allocation_and_closes_both_stores(monkeypatch):
    budget = _factory_budget()
    budget.retained_bytes = 137
    staged = _stage((lambda: _data(4097),), budget)
    _, rows, store = staged
    files = (rows.stream, store.stream)
    allocations = []
    original_buffer, check = io.BytesIO, budget.check_peak

    class Buffer(original_buffer):
        def __init__(self, initial):
            allocations.append(len(initial))
            super().__init__(initial)

    def reject(raw, *, records=0, entries=0, label="stack-map"):
        if label == "Mach-O source payload validation replay":
            budget.limit_bytes = raw + budget.retained_bytes - 1
        return check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(payloads.io, "BytesIO", Buffer)
    monkeypatch.setattr(budget, "check_peak", reject)
    with pytest.raises(maps.PreciseStackMapError, match="source payload validation replay"):
        _source(staged, budget)
    assert allocations == [0]  # only the preceding empty row replay
    assert store.read_bytes == 0
    assert all(file.closed for file in files)
    assert rows.stream is store.stream is None
    assert budget.retained_bytes == 137


def test_source_recounts_surviving_metadata_before_opening_payload_window(monkeypatch):
    budget = _factory_budget()

    def section():
        return obj.Section(
            "__data", "__DATA", data=b"\0" * (1024 * 1024), align_log2=3,
            symbols=(obj.TextSymbol("_main", 0),) + tuple(
                obj.TextSymbol("_alias" + str(index), 0) for index in range(399)
            ),
        )

    staged = _stage((section,), budget)
    store = staged[2]
    backing = store.backing_bytes(0)
    budget.limit_bytes = 2 * 1024 * 1024
    admissions, check = [], budget.check_peak

    def admitted(raw, *, records=0, entries=0, label="stack-map"):
        admissions.append((label, raw + budget.retained_bytes))
        return check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(budget, "check_peak", admitted)
    source = _source(staged, budget)
    try:
        construction, = [amount for label, amount in admissions
                         if label == "Mach-O retained preparation owners"]
        surviving, = [amount for label, amount in admissions
                      if label == "Mach-O staged source surviving metadata"]
        cache_admission, = [amount for label, amount in admissions
                            if label == "Mach-O staged symbol-name cache"]
        replay, = [amount for label, amount in admissions
                   if label == "Mach-O source payload validation replay"]
        # The cache borrows the canonical strings but adds a tuple after the
        # surviving-metadata census. Size its pointer storage independently.
        cache_bytes = budget._rounded(sys.getsizeof((None,) * len(source.symbols())))
        assert construction < budget.limit_bytes < construction + backing
        assert surviving + cache_bytes <= cache_admission < construction
        assert replay == surviving + cache_bytes + backing < budget.limit_bytes
        assert max(amount for _, amount in admissions) < budget.limit_bytes
        assert len(source.symbols()) == 400
        assert source._payloads == [b""]
        assert store.read_bytes == 1024 * 1024 and store.active_bytes == 0
    finally:
        host.close_staged_source_rows(source)


def test_public_payload_copies_are_fresh_charged_and_prevent_owned_consume():
    budget = _factory_budget()
    source = _simple_source(budget)
    store, rows = source._host_payload_store, source._host_row_store
    files = (store.stream, rows.stream)
    section = source.sections()[0]
    first = source.section_data(section)
    second = source.section_data(section)
    assert type(first) is type(second) is bytes
    assert first == second and first is not second
    assert store.public_copy_bytes == 2 * store.backing_bytes(0)
    assert source._payloads == [b""] and store.consumed == 0
    with pytest.raises(native.NativeObjectError, match="transfer requires owned output"):
        source.take_section_data(section)
    pending = [source]
    source = None
    with pytest.raises(macho_exec.LinkError, match="cannot consume staged payloads with public copies"):
        output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert all(file.closed for file in files)
    assert first == second == b"\x01" * 4096


def test_owned_transfers_retire_prior_backings_and_refuse_next_before_allocation(monkeypatch):
    budget = _factory_budget()
    budget.retained_bytes = 137
    builders = (
        lambda: obj.Section(
            "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", align_log2=2,
            flags=obj.TEXT_SECTION_FLAGS, symbols=(obj.TextSymbol("_main", 0),),
        ),
        lambda: _data(4097, index=1),
        lambda: _data(8193, index=2),
    )
    source = _source(_stage(builders, budget), budget)
    store, rows = source._host_payload_store, source._host_row_store
    files = (store.stream, rows.stream)
    backings = tuple(store.backing_bytes(index) for index in range(3))
    prior_reads = store.read_bytes
    take, check, original_buffer = output._OwnedOutput.take, budget.check_peak, io.BytesIO
    states, snapshots, admissions, buffers = [], [], [], []

    class Buffer(original_buffer):
        def __init__(self, initial):
            self.initial_size = len(initial)
            super().__init__(initial)
            buffers.append(self)

    def tracked(state, current, section, zerofill):
        index = section["_pcc_source_index"]
        if not states:
            states.append(state)
        assert state is states[0]
        snapshots.append((index, state.known_bytes(), state.extra_raw))
        assert state.extra_raw == snapshots[0][2]
        assert not state.views and not state.buffers
        assert all(buffer.closed for buffer in buffers)
        if index == 2:
            budget.limit_bytes = state.known_bytes() + backings[index] + budget.retained_bytes - 1
        result = take(state, current, section, zerofill)
        assert state.extra_raw == snapshots[-1][2] + backings[index]
        return result

    def admitted(raw, *, records=0, entries=0, label="stack-map"):
        if label == "Mach-O owned source payload transfer":
            index, known, _extra = snapshots[-1]
            assert raw == known + backings[index]
            admissions.append(raw + budget.retained_bytes)
        return check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(output._OwnedOutput, "take", tracked)
    monkeypatch.setattr(budget, "check_peak", admitted)
    monkeypatch.setattr(payloads.io, "BytesIO", Buffer)
    pending = [source]
    source = None
    with pytest.raises(macho_exec.LinkError, match="Mach-O owned source payload transfer"):
        output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert [index for index, _known, _extra in snapshots] == [0, 1, 2]
    assert admissions == [known + backings[index] + 137
                          for index, known, _extra in snapshots]
    assert admissions[-1] == budget.limit_bytes + 1
    assert all(amount <= budget.limit_bytes for amount in admissions[:-1])
    sizes = [buffer.initial_size for buffer in buffers]
    assert sizes.count(4) == sizes.count(4097) == 2
    assert 8193 not in sizes
    assert store.consumed == 2 and store.read_bytes - prior_reads == 4 + 4097
    assert states[0].extra_raw == snapshots[0][2]
    assert not states[0].views and not states[0].buffers
    assert all(buffer.closed for buffer in buffers)
    assert all(file.closed for file in files)
    assert rows.stream is store.stream is None
    assert budget.retained_bytes == 137


@pytest.mark.parametrize("first_kind", ("payload", "row"))
def test_source_accessor_budget_refusal_closes_both_files_in_either_direction(first_kind):
    budget = _factory_budget()
    source = _simple_source(budget)
    store, rows = source._host_payload_store, source._host_row_store
    files = (store.stream, rows.stream)
    section = source.sections()[0]
    if first_kind == "payload":
        escaped = source.section_data(section)
        accessor = source.compact_relocation_rows
        backing, label = rows.backing_bytes(0), "public staged row copy"
        bound = source._host_row_copy_bound
    else:
        escaped = source.compact_relocation_rows(section)
        accessor = source.section_data
        backing, label = store.backing_bytes(0), "public staged payload copy"
        bound = source._host_payload_copy_bound
    public = store.public_copy_bytes + rows.public_copy_bytes
    assert public > 0
    budget.limit_bytes = bound + public + backing - 1
    with pytest.raises(maps.PreciseStackMapError, match=label):
        accessor(section)
    assert escaped
    assert all(file.closed for file in files)
    assert rows.stream is store.stream is None
    assert source._host_payload_store is source._host_row_store is None
    assert source._payloads == source._raw_relocations == []


def test_staged_malformed_stackmap_preserves_canonical_validation_error(monkeypatch):
    def text():
        return obj.Section(
            "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", align_log2=2,
            flags=obj.TEXT_SECTION_FLAGS, symbols=(obj.TextSymbol("_main", 0),),
        )

    def malformed():
        return obj.Section(
            "__pcc_stackmaps", "__DATA", data=b"bad stack-map wire data" * 4,
            align_log2=3, flags=obj.PCC_STACKMAP_SECTION_FLAGS,
        )

    with pytest.raises(native.NativeObjectError) as ordinary:
        native._validate_source_sections((text(), malformed()), ())
    assert str(ordinary.value) == "invalid __pcc_stackmaps payload: bad pcc stack-map magic"
    budget = _factory_budget()
    staged = _stage((text, malformed), budget)
    files = (staged[1].stream, staged[2].stream)
    scan, visits = obj._scan_stack_map_section, []

    def checked(data, **kwargs):
        visits.append(len(data))
        assert type(data) is bytes and data.startswith(b"bad")
        assert kwargs["compact_budget"].payload is data
        return scan(data, **kwargs)

    monkeypatch.setattr(obj, "_scan_stack_map_section", checked)
    with pytest.raises(native.NativeObjectError) as actual:
        _source(staged, budget)
    assert str(actual.value) == str(ordinary.value)
    assert visits == [len(b"bad stack-map wire data" * 4)]
    assert all(file.closed for file in files)


def test_source_validation_payload_alias_fails_closed_for_both_stores(monkeypatch):
    budget = _factory_budget()
    budget.retained_bytes = 137
    staged = _stage((lambda: _data(),), budget, (_row,))
    files = (staged[1].stream, staged[2].stream)
    validate, escaped = native._validate_section, []

    def retain(section, **kwargs):
        escaped.append(section.data)
        return validate(section, **kwargs)

    monkeypatch.setattr(native, "_validate_section", retain)
    try:
        with pytest.raises(maps.PreciseStackMapError, match="payload window has escaped aliases"):
            _source(staged, budget)
        assert escaped == [b"\x01" * 4096]
        assert all(file.closed for file in files)
        assert staged[1].stream is staged[2].stream is None
        assert budget.retained_bytes == 137
    finally:
        escaped.clear()


def test_staged_unknown_symbol_reaches_canonical_relocation_validation(monkeypatch):
    unknown = obj.Relocation(0, "_missing", spec.ARM64_RELOC_UNSIGNED, False, 3)
    with pytest.raises(native.NativeObjectError) as ordinary:
        native._validate_source_sections((replace(_data(), relocations=(unknown,)),), ())
    budget = _factory_budget()
    staged = _stage((lambda: _data(),), budget, (_row,))
    files = (staged[1].stream, staged[2].stream)
    logical, validate, visits = host._logical_staged_rows, native._validate_source_section_relocations, []

    def unknown_rows(*args):
        # The authentic compact row is valid. Inject at the logical factory
        # boundary to prove the staged path does not bypass name validation.
        for relocation in logical(*args):
            yield replace(relocation, symbol="_missing")

    def checked(section, *args):
        visits.append(section.sectname)
        return validate(section, *args)

    monkeypatch.setattr(host, "_logical_staged_rows", unknown_rows)
    monkeypatch.setattr(native, "_validate_source_section_relocations", checked)
    with pytest.raises(native.NativeObjectError) as actual:
        _source(staged, budget)
    assert str(actual.value) == str(ordinary.value) == "relocation targets unknown symbol '_missing'"
    assert visits == ["__data0"]
    assert all(file.closed for file in files)


def test_owned_image_and_signature_keep_later_payload_after_dropped_descriptor(monkeypatch):
    builders = (
        lambda: obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", align_log2=2,
                    flags=obj.TEXT_SECTION_FLAGS, symbols=(obj.TextSymbol("_main", 0),)),
        lambda: obj.Section("__compact_unwind", "__LD", data=b"\0" * 32, align_log2=3,
                    flags=obj.COMPACT_UNWIND_SECTION_FLAGS,
                    relocations=(obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),)),
        lambda: obj.Section("__data", "__DATA", data=b"\0" * 16, align_log2=3,
                    relocations=(obj.Relocation(8, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),)),
    )
    expected = macho_exec.link_prepared_executable(
        native.OwnedMergedSourceView(tuple(build() for build in builders)),
        compact_budget=_factory_budget(),
    )
    budget = _factory_budget()
    source = _source(_stage(builders, budget, (
        lambda: b"", _row,
        lambda: native._FINAL_LINK_RELOCATION.pack(8, 0, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
    )), budget)
    store = source._host_payload_store
    payload_file = store.stream
    section_names = [section["sectname_str"] for section in source.sections()]
    dropped, later = section_names.index("__compact_unwind"), section_names.index("__data")
    assert dropped < later
    take, indices, phases = payloads._StagedSectionPayloads.take, [], []

    def tracked(store, index, **kwargs):
        indices.append(index)
        return take(store, index, **kwargs)

    monkeypatch.setattr(payloads._StagedSectionPayloads, "take", tracked)
    def phase(event, **_fields):
        phases.append(event)
        if event == "relocations_end":
            assert store.stream is None and payload_file.closed
            assert store.consumed == 2

    pending = [source]
    source = None
    actual = output.link_owned_executable(
        pending.pop(), compact_budget=budget, _phase_callback=phase,
    )
    assert actual == expected
    assert dropped not in indices and later in indices
    assert indices == [0, later]
    assert "relocations_begin" in phases
    assert "__compact_unwind" not in {
        section["sectname_str"] for section in spec.parse_object(actual).sections()
    }
    _signature_matches(actual, budget)


def test_section_target_normalization_starts_fresh_for_validation_copy_and_final_replay(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _section_targets())
    patch, calls, phase = host._patch_section_targets, [], ["validation"]

    def tracked(data, rows, addresses, order):
        fields = [(offset, 1 << length, addresses[order[number - 1]])
                  for offset, number, _pc, length, external, kind
                  in native._FINAL_LINK_RELOCATION.iter_unpack(rows)
                  if not external and kind != spec.ARM64_RELOC_ADDEND]
        before = tuple(int.from_bytes(data[offset:offset + width], "little")
                       for offset, width, _base in fields)
        patch(data, rows, addresses, order)
        after = tuple(int.from_bytes(data[offset:offset + width], "little")
                      for offset, width, _base in fields)
        assert after == tuple(value + field[2] for value, field in zip(before, fields))
        calls.append((phase[0], before, after))

    monkeypatch.setattr(host, "_patch_section_targets", tracked)
    budget = _factory_budget()
    source = host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
    target = next(section for section in source.sections() if section["sectname_str"] == "__data")
    phase[0] = "copy"
    first, second = source.section_data(target), source.section_data(target)
    assert first == second and first is not second
    host.close_staged_source_rows(source)
    source = None
    phase[0] = "fresh validation"
    fresh_budget = _factory_budget()
    source = host.prepare_executable_from_paths(paths, compact_budget=fresh_budget, _staged_rows=True)
    target = next(section for section in source.sections() if section["sectname_str"] == "__data")
    phase[0] = "final replay"
    # This intentionally unresolved target is a preparation contract. Exercise
    # the same final owner transfer used by executable linking without asking
    # the linker to accept an unsupported unresolved section-target relocation.
    state = output._OwnedOutput(source, fresh_budget, ())
    actual = None
    try:
        actual = state.take(source, target, False)
        assert bytes(actual) == first
        assert source._host_payload_store.consumed == 1
    finally:
        actual = None
        state.close()
        host.close_staged_source_rows(source)
    assert [point for point, _, _ in calls] == [
        "validation", "copy", "copy", "fresh validation", "final replay",
    ]
    assert all(before == calls[0][1] and after == calls[0][2] for _, before, after in calls)
    assert calls[0][1] != calls[0][2]


def test_staged_source_stackmap_record_visits_are_bounded_through_final_replay(tmp_path, monkeypatch):
    # The three walks below are the v2 compact-budget contract, not v3
    # runtime-table validation accounting. Select the measured format.
    monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, "2")
    records, count = 32, 2
    paths = _stack_inputs(tmp_path, count=count, records=records, data_bytes=64)
    budget = _factory_budget()
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    preparation_visits = budget.record_visits
    # Merge rewrites each record once. Source-view structural validation and
    # fresh backing replay must not introduce additional record walks.
    assert preparation_visits == records * count
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    # Final-image validation performs one structural and one semantic pass.
    assert budget.record_visits == 3 * records * count
    _signature_matches(actual, budget)
