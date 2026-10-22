"""Host-only packed spool metadata: bytes, work, owners and refusal edges."""

import io
import struct
import sys

import pytest

from pcc.backend import macho_host_stackmap as spool
from pcc.backend import macho_host_stackmap_index as packed
from pcc.backend import precise_stackmap as wire
from tests.python.test_pe_stackmap_suffix_sharing import _payload, _root


def _budget(limit=100_000_000, *, metadata=None):
    word = struct.calcsize("P")
    quantum = 2 * word
    rounded = lambda value: (value + quantum - 1) // quantum * quantum
    integer = rounded(max(sys.getsizeof((1 << 64) - 1), sys.getsizeof(-(1 << 63))))
    entry = (64 * word + rounded(sys.getsizeof((0, 0, 0)))
             + 3 * integer + rounded(sys.getsizeof(b"")))
    return wire.CompactStackMapBudget(
        limit, 3 * (limit // wire.RECORD_SIZE),
        set_header_bytes=rounded(sys.getsizeof(set())), set_entry_bytes=2 * word,
        integer_bytes=integer, metadata_entry_bytes=entry if metadata is None else metadata,
        allocation_quantum=quantum,
    )


def _tables():
    budget = _budget()
    work = packed._IndexWork(budget)
    checks = []
    root = source = None

    def admit(extra, label):
        owned = root.owned_bytes() + source.owned_bytes()
        checks.append((label, owned, extra))
        budget.check_peak(owned + extra, label=label)

    root = packed._RootIndex(budget, work, admit)
    source = packed._SourceIndex(budget, work, admit)
    return root, source, work, checks


def test_single_insertion_counts_actual_operations_without_rehash():
    root, source, work, checks = _tables()
    disk = io.BytesIO(b"r" * wire.LOCATION_SIZE)
    spool_work = spool._SpoolWork(_budget())
    spool_work.comparison_limit = 4
    scratch = bytearray(spool._BLOCK_BYTES)
    digest = bytes(32)  # A zero digest is valid; zero ordinal is the sentinel.
    with memoryview(disk.getvalue()) as candidate:
        assert root.find(digest, 1, candidate, disk, scratch, spool_work) == (-1, -1)
        root.append(digest, 1, 0, -1)
        assert root.find(digest, 1, candidate, disk, scratch, spool_work)[0] == 0
    assert source.find(1) == (-1, -1)
    source.append(1, 0, -1)
    assert source.find(1)[0] == 0
    assert (work.operations, work.steps, work.max_probe) == (6, 4, 1)
    assert work.root_copies == work.rehash_entries == work.source_scans == 0
    assert work.allocated_bytes == 192 + 128
    assert [row[0] for row in checks] == [
        "spool root rows grow", "spool root index grow", "spool source index grow",
    ]
    root.close()
    source.close()
    assert root.rows is root.index is source.buffer is None


def test_bounded_index_scratch_fits_existing_fixed_reserve():
    root, source, work, _checks = _tables()
    budget = _budget()
    rounded = budget._rounded
    # Overlap of up to eight unpack/argument tuples; 64 scalar temporaries
    # (including uint128 mask-before products); eight parent/child views;
    # table/work owners; four
    # closure functions and sixteen cells/iterators. Payload backing headers
    # are separately included by owned_bytes(), never hidden in this reserve.
    cell = (lambda value: lambda: value)(0).__closure__[0]
    assert rounded(sys.getsizeof((1 << 128) - 1)) <= budget.integer_bytes
    cell_or_iterator = max(sys.getsizeof(cell), sys.getsizeof(iter(range(1))))
    with memoryview(b"") as view:
        fixed = (sum(rounded(sys.getsizeof(owner)) for owner in (root, source, work))
                 + 8 * rounded(sys.getsizeof((0,) * 10))
                 + 64 * budget.integer_bytes + 8 * rounded(sys.getsizeof(view))
                 + 4 * rounded(sys.getsizeof(lambda: None))
                 + 16 * rounded(cell_or_iterator))
    assert fixed <= spool._FIXED_BYTES - 2 * spool._BLOCK_BYTES
    root.close()
    source.close()


def test_packed_merge_retires_before_owned_output_and_matches_eager_image(tmp_path, monkeypatch):
    import weakref
    from pcc.backend import macho_exec, macho_host_output as output
    from pcc.backend import macho_host_prepare as host, macho_codesign as codesign
    from pcc.backend import macho_obj as obj, macho_spec as spec, native_object as native
    from tests.python.test_macho_owned_output import _factory_budget, _tracked_buffers

    objects, paths = [], []
    for unit in range(2):
        name = "_main" if unit == 0 else "_other"
        records = []
        for ordinal in range(40):
            number = ordinal + 1 if unit == 0 else 40 - ordinal
            base = wire.StackMapLocation(
                wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
                register=29, offset=-8 * number,
            )
            derived = wire.StackMapLocation(
                wire.LOCATION_STACK_INDIRECT,
                wire.LOCATION_MANAGED | wire.LOCATION_OWNED | wire.LOCATION_DERIVED
                | wire.LOCATION_RELOAD_REQUIRED,
                register=29, offset=-8 * (number + 40), base_index=0,
            )
            records.append(wire.SafepointRecord(
                (unit + 1) * 1000 + ordinal, ordinal * 4,
                wire.SAFEPOINT_CONTINUATION, (base, derived),
                flags=wire.RECORD_HAS_EXCEPTION_EDGE | wire.RECORD_SUSPENDED,
                exceptional_offset=ordinal * 4 + 1, continuation_id=ordinal + 1,
            ))
        payload = wire.encode_stack_map(wire.PreciseStackMap(
            wire.ARCH_AARCH64,
            (wire.FunctionStackMap(unit + 1, 0, 160, 1024, tuple(records)),),
        ))
        address, = wire.function_address_offsets(payload)
        value = native.NativeObject.from_sections([
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 40,
                        flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                        symbols=(obj.TextSymbol(name, 0),)),
            obj.Section("__pcc_stackmaps", "__DATA", data=payload, align_log2=3,
                        relocations=(obj.Relocation(address, name,
                                     spec.ARM64_RELOC_UNSIGNED, False, 3),)),
        ])
        objects.append(value)
        path = tmp_path / (str(unit) + ".pco")
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    expected = macho_exec.link_prepared_executable(
        macho_exec.prepare_executable_object(objects, compact_budget=_factory_budget()),
        compact_budget=_factory_budget(),
    )
    objects.clear()
    value = payload = records = None

    tables = []
    for cls in (spool._RootIndex, spool._SourceIndex):
        original_init = cls.__init__

        def observe(self, *args, _init=original_init):
            _init(self, *args)
            tables.append(self)

        monkeypatch.setattr(cls, "__init__", observe)
    owners = _tracked_buffers(monkeypatch)
    budget = _factory_budget()
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    assert len(tables) == 3
    root, first, second = tables
    assert root.rows is root.index is first.buffer is second.buffer is None
    assert all(table.admit is None for table in tables)
    assert root.work.root_copies > 0 and root.work.rehash_entries > 0
    assert root.work.source_scans > 0
    source = weakref.ref(pending[0])
    events = []

    def phase(event):
        events.append(event)
        assert source() is None
        assert all(table.admit is None for table in tables)

    actual = output.link_owned_executable(pending.pop(), compact_budget=budget,
                                         phase_callback=phase)
    assert actual == expected and budget.retained_bytes == 137
    assert events == ["sign_begin", "sign_end"]
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    signature = codesign.parse_signature(actual)
    with memoryview(actual) as whole, whole[:signature.dataoff] as prefix:
        assert codesign.build_signature(
            prefix, identifier=signature.identifier,
            exec_seg_base=signature.exec_seg_base,
            exec_seg_limit=signature.exec_seg_limit,
            exec_seg_flags=signature.exec_seg_flags,
        ) == actual[signature.dataoff:]


def test_source_preserves_full_index_count_key_and_high_unsigned_fields():
    root, source, _work, _checks = _tables()
    keys = [(index << 16) | count
            for index in (0, 1, 1 << 15, 1 << 31, 0xFFFFFFFF)
            for count in (1, 3, 65535)]
    expected = {key: (0 if number == 0 else 0xFFFFFFFF - number)
                for number, key in enumerate(keys)}
    for key, destination in expected.items():
        value, slot = source.find(key)
        assert value == -1
        source.append(key, destination, slot)
    for key, destination in expected.items():
        assert source.find(key)[0] == destination
    assert source.used == len(expected)
    root.close()
    source.close()


@pytest.mark.parametrize("key", (0, 65536, 1 << 48, -1))
def test_source_rejects_nonrepresentable_or_empty_keys(key):
    root, source, _work, _checks = _tables()
    with pytest.raises(wire.PreciseStackMapError, match="source index field"):
        source.append(key, 0, -1)
    assert source.buffer is None
    root.close()
    source.close()


def test_root_fifo_comparisons_survive_wrapped_clusters_and_every_resize(monkeypatch):
    monkeypatch.setattr(packed, "_root_hash", lambda words: 7)
    root, source, index_work, checks = _tables()
    disk = io.BytesIO()
    scratch = bytearray(spool._BLOCK_BYTES)
    comparisons = []

    class Observed(spool._SpoolWork):
        def charge_comparison(self, count):
            comparisons.append(count)
            super().charge_comparison(count)

    spool_work = Observed(_budget())
    spool_work.comparison_limit = 4 * sum(range(1, 41)) * 2
    digest = bytes(32)
    destinations = []
    for count in range(1, 41):
        value = bytes([count]) * (count * wire.LOCATION_SIZE)
        before = len(comparisons)
        with memoryview(value) as candidate:
            destination, slot = root.find(digest, count, candidate, disk, scratch, spool_work)
        assert destination == -1
        assert comparisons[before:] == [1] * (count - 1)
        destination = len(disk.getvalue()) // wire.LOCATION_SIZE
        destinations.append(destination)
        disk.seek(0, 2)
        disk.write(value)
        before = len(comparisons)
        root.append(digest, count, destination, slot)
        assert len(comparisons) == before  # miss insertion/rehash never compares twice
    for count in (40, 1, 17, 5, 32, 4, 9):
        before = len(comparisons)
        with memoryview(bytes([count]) * (count * wire.LOCATION_SIZE)) as candidate:
            destination, _ = root.find(digest, count, candidate, disk, scratch, spool_work)
        assert destination == destinations[count - 1]
        assert comparisons[before:] == [1] * (count - 1) + [count]
    assert root.capacity == 64 and root.used == 40
    assert index_work.root_copies == index_work.rehash_entries == 4 + 8 + 16 + 32
    assert index_work.source_scans == 0
    assert sum(label == "spool root index grow" for label, *_ in checks) == 5
    assert index_work.steps <= 16 * (index_work.operations + 1)
    root.close()
    source.close()


@pytest.mark.parametrize("changed_word", (1, 2, 3))
def test_hash_collision_checks_every_digest_word_before_root_comparison(monkeypatch, changed_word):
    monkeypatch.setattr(packed, "_root_hash", lambda words: 7)
    root, source, _work, _checks = _tables()
    disk = io.BytesIO(b"a" * wire.LOCATION_SIZE)
    scratch = bytearray(spool._BLOCK_BYTES)
    spool_work = spool._SpoolWork(_budget())
    spool_work.comparison_limit = 4
    with memoryview(b"a" * wire.LOCATION_SIZE) as candidate:
        _destination, slot = root.find(bytes(32), 1, candidate, disk, scratch, spool_work)
    root.append(bytes(32), 1, 0, slot)
    words = [0, 0, 0, 0]
    words[changed_word] = 1
    with memoryview(b"b" * wire.LOCATION_SIZE) as candidate:
        assert root.find(packed._WORDS.pack(*words), 1, candidate, disk, scratch, spool_work)[0] == -1
    assert spool_work.compared_locations == spool_work.read == 0
    root.close()
    source.close()


def test_same_count_high_index_pressure_has_bounded_actual_work():
    root, source, work, _checks = _tables()
    count = 2048
    for index in range(count):
        key = (index << 16) | 3
        destination, slot = source.find(key)
        assert destination == -1
        source.append(key, index, slot)
    moved = sum(1 << power for power in range(2, 11))
    assert moved == 2044
    assert work.operations == 2 * count + moved
    assert work.rehash_entries == moved and work.source_scans == 2 * moved
    assert work.root_copies == 0
    assert work.allocated_bytes == packed._SOURCE.size * (8192 - 8)
    assert work.steps < 16 * (work.operations + 1)
    assert work.max_probe < 64
    for index in range(count):
        assert source.find((index << 16) | 3)[0] == index
    assert work.operations == 3 * count + moved
    root.close()
    source.close()


def test_adversarial_hash_clustering_refuses_with_a_separate_index_limit(monkeypatch):
    monkeypatch.setattr(packed, "_mix64", lambda value: 0)
    root, source, work, _checks = _tables()
    with pytest.raises(wire.PreciseStackMapError, match="index probe work budget"):
        for index in range(1000):
            key = (index << 16) | 1
            _value, slot = source.find(key)
            source.append(key, index, slot)
    assert 4 < source.used < 1000
    assert work.steps <= 16 * (work.operations + 1)
    assert work.operations < work.operation_limit
    assert work.allocated_bytes <= work.allocation_limit
    root.close()
    source.close()


def _merge(payloads, budget):
    owned = [bytes(memoryview(value)) for value in payloads]
    result = spool.merge_owned_stack_map_payloads(owned, compact_budget=budget, single_buffer=True)
    assert owned == [None] * len(owned)
    return result


def _merge_frame():
    frame = sys._getframe(1)
    while frame.f_code is not spool.merge_owned_stack_map_payloads.__code__:
        frame = frame.f_back
    return frame


def test_each_grow_admits_every_live_old_owner_and_releases_old_rows_first():
    payloads = tuple(_payload(tuple((_root(number),) for number in range(1, 35)), fid=fid)
                     for fid in (2, 1))
    budget = _budget()
    original = budget.check_peak
    retired_rows = []
    labels = []

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if label.endswith(" grow"):
            frame = _merge_frame()
            state = frame.f_locals
            root, source = state["root_index"], state["source_index_cache"]
            grow = sys._getframe(1).f_back.f_back
            assert grow.f_code in (packed._RootIndex._grow_rows.__code__,
                                   packed._RootIndex._grow_index.__code__,
                                   packed._SourceIndex._grow.__code__)
            assert grow.f_locals["new"] is None  # admission precedes allocation
            extra = sys._getframe(1).f_locals["extra"]
            assert raw == (state["input_bytes"] + spool._FIXED_BYTES + extra
                           + root.owned_bytes() + source.owned_bytes())
            assert entries == state["base_entries"] + 4 * len(state["functions"]) + len(state["scanned"])
            if label == "spool root rows grow" and root.rows is not None:
                retired_rows.append(root.rows)
            if label == "spool root index grow" and retired_rows:
                old = retired_rows[-1]
                # Only this test's list/local/temporary references remain:
                # compare with a control held the same way, since the count
                # getrefcount reports for those differs between CPython 3.13
                # and 3.14+.
                control_rows = [bytearray()]
                control = control_rows[-1]
                assert sys.getrefcount(old) == sys.getrefcount(control)
                del control_rows, control
                old.append(0)  # no live exported view remains either
            labels.append(label)
            del frame, state, root, source, grow
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = check
    actual = _merge(payloads, budget)
    assert actual == wire.merge_stack_map_payloads(payloads, compact_budget=_budget())
    assert set(labels) == {"spool root rows grow", "spool root index grow", "spool source index grow"}


@pytest.mark.parametrize("label", ("spool root rows grow", "spool root index grow", "spool source index grow"))
@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("metadata", (768, 784))
def test_each_new_admission_exact_boundary(label, delta, metadata):
    payloads = (_payload(tuple((_root(number),) for number in range(1, 18))),)
    budget = _budget(metadata=metadata)
    original = budget.check_peak
    checks = []

    def record(raw, *, records=0, entries=0, label="stack-map"):
        if label.endswith(" grow"):
            checks.append((label, raw + budget.retained_bytes + entries * budget.metadata_entry_bytes))
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = record
    expected = _merge(payloads, budget)
    required = max(value for current, value in checks if current == label)
    budget = _budget(metadata=metadata)
    original = budget.check_peak

    def limited(raw, *, records=0, entries=0, label="stack-map"):
        previous = budget.limit_bytes
        if label == target:
            budget.limit_bytes = required + delta
        try:
            return original(raw, records=records, entries=entries, label=label)
        finally:
            budget.limit_bytes = previous

    target = label
    budget.check_peak = limited
    if delta < 0:
        with pytest.raises(wire.PreciseStackMapError, match=label):
            _merge(payloads, budget)
        assert "label=" + label in budget.last_rejection
    else:
        assert _merge(payloads, budget) == expected


@pytest.mark.parametrize("label", ("spool root rows grow", "spool root index grow", "spool source index grow"))
def test_saved_mid_grow_failure_retires_replacements_and_all_table_owners(monkeypatch, label):
    tables = []
    for kind in (packed._RootIndex, packed._SourceIndex):
        original_init = kind.__init__

        def remember(self, *args, original=original_init):
            original(self, *args)
            tables.append(self)

        monkeypatch.setattr(kind, "__init__", remember)
    budget = _budget()
    original = budget.check_peak
    armed = False

    def check(raw, *, records=0, entries=0, label="stack-map"):
        nonlocal armed
        if label == target:
            grow = sys._getframe(1).f_back.f_back
            armed = grow.f_locals["old"] is not None
            del grow
        return original(raw, records=records, entries=entries, label=label)

    target = label
    budget.check_peak = check
    method = "copy_rows" if label == "spool root rows grow" else "probe"
    original_work = getattr(packed._IndexWork, method)

    def fail(work, count):
        if armed:
            raise RuntimeError("injected mid-grow failure")
        return original_work(work, count)

    monkeypatch.setattr(packed._IndexWork, method, fail)
    owned = [_payload(tuple((_root(number),) for number in range(1, 18)))]
    with pytest.raises(RuntimeError, match="injected mid-grow") as caught:
        spool.merge_owned_stack_map_payloads(owned, compact_budget=budget, single_buffer=True)
    assert owned[0] is not None  # an incomplete source remains the caller's owner
    for table in tables:
        assert table.admit is None and table.owned_bytes() == 0
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == packed.__file__:
            for value in trace.tb_frame.f_locals.values():
                assert not isinstance(value, bytearray)
                if isinstance(value, memoryview):
                    with pytest.raises(ValueError, match="released memoryview"):
                        _ = value.obj
        trace = trace.tb_next
