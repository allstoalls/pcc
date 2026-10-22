"""Bounded CPython row-packing checks; no compiler or native execution."""

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import weakref

import pytest

from pcc.backend import macho_exec
from pcc.backend import macho_host_relocations as host
from pcc.backend import macho_obj as obj
from pcc.backend import macho_spec as spec
from pcc.backend import native_object as native
from pcc.backend.precise_stackmap import CompactStackMapBudget, PreciseStackMapError


def _budget(limit=1_000_000):
    return CompactStackMapBudget(
        limit, 100, set_header_bytes=10, set_entry_bytes=2,
        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
    )


def _sections():
    return [
        obj.Section(
            sectname="__text", segname="__TEXT",
            data=b"\xc0\x03\x5f\xd6" * 4, align_log2=2,
            flags=obj.TEXT_SECTION_FLAGS,
            symbols=(obj.TextSymbol("_main", 0), obj.TextSymbol("_tail", 4)),
        ),
        obj.Section(
            sectname="__data", segname="__DATA", data=b"\0" * 32,
            relocations=(
                obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),
                obj.Relocation(
                    8, "_main", spec.ARM64_RELOC_SUBTRACTOR, False, 3,
                    minuend="_tail",
                ),
                obj.Relocation(16, "_main", spec.ARM64_RELOC_PAGE21, True, 2, 8),
                obj.Relocation(20, "_main", spec.ARM64_RELOC_PAGEOFF12, False, 2, 8),
                obj.Relocation(
                    24, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                    section=("__TEXT", "__text"), target_offset=4,
                ),
            ),
        ),
    ]


def test_budgeted_owned_constructor_packs_directly_without_scalar_arena(monkeypatch):
    expected = native.OwnedMergedSourceView(_sections())

    def no_arena(*_args, **_kwargs):
        raise AssertionError("host row packing allocated the six-scalar arena")

    monkeypatch.setattr(native, "CompilerIntArena", no_arena)
    budget = _budget()
    budget.retained_bytes = 17
    actual = native.OwnedMergedSourceView(_sections(), compact_budget=budget)
    assert actual._raw_relocations == expected._raw_relocations
    assert actual.sections() == expected.sections()
    assert actual.symbols() == expected.symbols()
    assert actual._payloads == expected._payloads
    assert budget.retained_bytes == 17
    assert budget.record_visits == 0


def test_budgeted_constructor_preserves_complete_executable_bytes():
    sections = _sections()
    sections[1] = replace(sections[1], relocations=(sections[1].relocations[0],))
    expected = macho_exec.link_prepared_executable(native.OwnedMergedSourceView(sections))
    budget = _budget()
    actual = macho_exec.link_prepared_executable(
        native.OwnedMergedSourceView(sections, compact_budget=budget),
        compact_budget=budget,
    )
    assert actual == expected
    assert budget.record_visits == 0


@pytest.mark.parametrize("relocations,message", [
    ((obj.Relocation(32, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),), "outside"),
    ((obj.Relocation(0, "_missing", spec.ARM64_RELOC_UNSIGNED, False, 3),), "unknown"),
    ((obj.Relocation(0, "_main", spec.ARM64_RELOC_BRANCH26, False, 2),), "requires"),
    ((obj.Relocation(0, "_main", spec.ARM64_RELOC_SUBTRACTOR, False, 3),), "following"),
    ((obj.Relocation(0, "_main", 255, False, 3),), "not differentially proven"),
    ((obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),
      obj.Relocation(0, "_tail", spec.ARM64_RELOC_UNSIGNED, False, 3)), "multiple"),
])
def test_existing_semantic_validation_still_precedes_host_packing(
    monkeypatch, relocations, message,
):
    def no_pack(*_args, **_kwargs):
        raise AssertionError("malformed source reached host row packing")

    monkeypatch.setattr(host, "pack_owned_final_link_rows", no_pack)
    sections = _sections()
    sections[1] = replace(sections[1], relocations=relocations)
    with pytest.raises(native.NativeObjectError, match=message):
        native.OwnedMergedSourceView(sections, compact_budget=_budget())


@pytest.mark.parametrize("implementation,budgeted", [
    ("cpython", False), ("pcc", False), ("pcc", True),
])
def test_native_and_unbudgeted_constructors_keep_original_arena(
    monkeypatch, implementation, budgeted,
):
    original = native.CompilerIntArena
    capacities = []

    def arena(capacity):
        capacities.append(capacity)
        return original(capacity)

    def no_host(*_args, **_kwargs):
        raise AssertionError("ordinary constructor called the CPython adapter")

    runtime = vars(sys.implementation) | {"name": implementation}
    with monkeypatch.context() as patch:
        patch.setattr(sys, "implementation", SimpleNamespace(**runtime))
        patch.setattr(native, "CompilerIntArena", arena)
        patch.setattr(host, "pack_owned_final_link_rows", no_host)
        native.OwnedMergedSourceView(
            _sections(), compact_budget=_budget() if budgeted else None,
        )
    assert capacities == [0, 48]


def test_host_row_adapter_is_excluded_from_native_import_closure():
    from pcc.frontends.python.pipeline_dependency_closure import (
        _package_import_targets,
    )
    from pcc.frontends.python.pipeline_import_scan import (
        _without_inactive_implementation_imports,
    )
    source = Path(native.__file__).read_text()
    marker = "from .macho_host_relocations import"
    assert marker in source
    assert marker not in _without_inactive_implementation_imports(
        source, implementation="pcc",
    )
    assert marker in _without_inactive_implementation_imports(
        source, implementation="cpython",
    )
    path = Path(native.__file__)
    dependencies = {
        name for _path, name in _package_import_targets(
            str(path), "pcc.backend.native_object", root_dir=str(path.parents[2]),
        )
    }
    assert "pcc.backend.macho_host_relocations" not in dependencies
    assert "pcc.backend.macho_obj" in dependencies
    assert "pcc.backend.self_backend_value_arena" in dependencies


def _one_row(**changes):
    return replace(
        obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),
        **changes,
    )


def _pack(relocations, count=1, *, budget=None, symbol_index=None, retained=0):
    return host._pack_final_link_rows(
        relocations, count,
        {"_main": 0} if symbol_index is None else symbol_index,
        {("__TEXT", "__text"): 1}, budget or _budget(), retained,
    )


def test_direct_writer_preserves_logical_order_and_atomic_pair_expansion():
    sections = _sections()
    expected = native.OwnedMergedSourceView(sections)
    rows = host._pack_final_link_rows(
        iter(sections[1].relocations), 8, expected._symbol_index,
        expected._section_index, _budget(), 0,
    )
    assert rows == expected._raw_relocations[1]
    unpacked = list(native._FINAL_LINK_RELOCATION.iter_unpack(rows))
    assert [row[0] for row in unpacked] == [0, 8, 8, 16, 16, 20, 20, 24]
    assert [row[-1] for row in unpacked] == [
        spec.ARM64_RELOC_UNSIGNED,
        spec.ARM64_RELOC_SUBTRACTOR, spec.ARM64_RELOC_UNSIGNED,
        spec.ARM64_RELOC_ADDEND, spec.ARM64_RELOC_PAGE21,
        spec.ARM64_RELOC_ADDEND, spec.ARM64_RELOC_PAGEOFF12,
        spec.ARM64_RELOC_UNSIGNED,
    ]


def test_generator_writer_does_not_retain_previous_relocation_objects():
    references = []

    class TrackedRelocation(obj.Relocation):
        __slots__ = ("__weakref__",)

    def fresh_rows():
        for offset in range(0, 32, 8):
            relocation = TrackedRelocation(
                offset, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3,
            )
            references.append(weakref.ref(relocation))
            # A for-loop can still own the preceding row until assignment.
            assert sum(reference() is not None for reference in references) <= 2
            yield relocation

    assert len(_pack(fresh_rows(), 4)) == 48
    assert all(reference() is None for reference in references)


def test_zero_rows_produce_exact_empty_bytes():
    assert _pack((), 0) == b""


@pytest.mark.parametrize("count", [-1, 1 << 32, True, 1.0])
def test_invalid_count_rejects_before_allocating(monkeypatch, count):
    def no_allocation(*_args, **_kwargs):
        raise AssertionError("invalid count reached bytearray allocation")

    monkeypatch.setattr(host, "bytearray", no_allocation, raising=False)
    with pytest.raises(native.NativeObjectError, match="count"):
        _pack((_one_row(),), count)


@pytest.mark.parametrize("count", [0, 2])
def test_row_count_mismatch_rejects_overflow_and_underfill(count):
    with pytest.raises(native.NativeObjectError, match="count changed"):
        _pack((_one_row(),), count)


@pytest.mark.parametrize("changes,symbol_index", [
    ({"offset": 1 << 32}, None),
    ({}, {"_main": 1 << 32}),
    ({"type": 256}, None),
    ({"length": 256}, None),
    ({"addend": 1 << 32}, None),
])
def test_numeric_field_overflow_cannot_escape_as_truncated_rows(changes, symbol_index):
    row = _one_row(**changes)
    with pytest.raises(native.NativeObjectError, match="field is out of range"):
        _pack((row,), 2 if row.addend else 1, symbol_index=symbol_index)


@pytest.mark.parametrize("mode", ["unfinished", "missing-prefix", "double-prefix"])
def test_incomplete_append_protocol_is_rejected(monkeypatch, mode):
    def malformed(writer, *_args):
        if mode == "missing-prefix":
            writer.append2(1, spec.ARM64_RELOC_UNSIGNED)
        else:
            writer.append4(0, 0, 0, 3)
            if mode == "double-prefix":
                writer.append4(0, 0, 0, 3)

    monkeypatch.setattr(host, "_append_final_link_relocation_fields", malformed)
    with pytest.raises(native.NativeObjectError, match="row|count changed"):
        _pack((_one_row(),))


class _RecordingBudget(CompactStackMapBudget):
    def __init__(self, limit=1_000_000):
        super().__init__(
            limit, 100, set_header_bytes=10, set_entry_bytes=2,
            integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
        )
        self.checks = []

    def check_peak(self, raw_bytes, **kwargs):
        self.checks.append((kwargs["label"], raw_bytes))
        return super().check_peak(raw_bytes, **kwargs)


def test_admission_precedes_allocation_and_freeze_and_counts_both_buffers(monkeypatch):
    probe = _RecordingBudget()
    probe.retained_bytes = 17
    _pack((_one_row(),), budget=probe, retained=101)
    allocation, freeze = [size for _label, size in probe.checks]
    assert freeze - allocation >= sys.getsizeof(b"x" * 12)
    assert probe.retained_bytes == 17
    allocations, freezes, buffers = [], [], []

    class TrackedBuffer(bytearray):
        pass

    def allocate(size):
        value = TrackedBuffer(size)
        allocations.append(size)
        buffers.append(weakref.ref(value))
        return value

    def freeze_bytes(value):
        freezes.append(len(value))
        return bytes(value)

    monkeypatch.setattr(host, "bytearray", allocate, raising=False)
    monkeypatch.setattr(host, "bytes", freeze_bytes, raising=False)
    for allowance, expected_allocations in ((allocation + 16, 0), (freeze + 16, 1)):
        budget = _budget(allowance)
        budget.retained_bytes = 17
        with pytest.raises(PreciseStackMapError, match="byte budget"):
            _pack((_one_row(),), budget=budget, retained=101)
        assert len(allocations) == expected_allocations
        assert freezes == []
        assert budget.retained_bytes == 17
        assert all(reference() is None for reference in buffers)
    budget = _budget(freeze + 17)
    budget.retained_bytes = 17
    assert len(_pack((_one_row(),), budget=budget, retained=101)) == 12
    assert freezes == [12]
    assert all(reference() is None for reference in buffers)


@pytest.mark.parametrize("failure", ["lookup", "iterator", "pack", "freeze"])
def test_writer_releases_mutable_buffer_even_when_traceback_is_retained(monkeypatch, failure):
    buffers = []

    class TrackedBuffer(bytearray):
        pass

    def allocate(size):
        result = TrackedBuffer(size)
        buffers.append(weakref.ref(result))
        return result

    def raising_rows():
        yield _one_row()
        raise RuntimeError("injected iterator failure")

    monkeypatch.setattr(host, "bytearray", allocate, raising=False)
    if failure == "lookup":
        rows, count, error = (_one_row(symbol="_missing"),), 1, KeyError
    elif failure == "iterator":
        rows, count, error = raising_rows(), 2, RuntimeError
    elif failure == "freeze":
        def failed_freeze(_buffer):
            # The real bytes allocator is a builtin: it does not add a
            # Python traceback frame that independently owns its argument.
            _buffer = None
            raise MemoryError("injected immutable allocation failure")

        monkeypatch.setattr(host, "bytes", failed_freeze, raising=False)
        rows, count, error = (_one_row(),), 1, MemoryError
    else:
        rows, count, error = (_one_row(offset=1 << 32),), 1, native.NativeObjectError
    budget = _budget()
    budget.retained_bytes = 19
    with pytest.raises(error) as retained_exception:
        _pack(rows, count, budget=budget)
    assert retained_exception.value.__traceback__ is not None
    assert buffers and all(reference() is None for reference in buffers)
    assert budget.retained_bytes == 19


def test_owner_estimate_charges_full_memoryview_exporter_and_source_fields():
    budget = _budget()
    exporter = b"x" * 4096
    view = memoryview(exporter)[1:3]
    assert host._model_bytes(view, budget) >= sys.getsizeof(view) + sys.getsizeof(exporter)
    row = _one_row()
    assert host._model_bytes(row, budget) >= sys.getsizeof(row) + sys.getsizeof(row.symbol)
    # Repeated references are conservatively charged, never treated as free
    # by equality of contents or an unproven owner-id guess.
    assert host._model_bytes((row, row), budget) >= 2 * host._model_bytes(row, budget)


def test_data_region_census_does_not_materialize_instance_dictionaries(monkeypatch):
    original = obj.DataInCodeRegion.__getattribute__

    def no_dictionary(self, name):
        if name == "__dict__":
            raise AssertionError("owner census materialized a region dictionary")
        return original(self, name)

    region = obj.DataInCodeRegion(0, 4, spec.DICE_KIND_DATA)
    monkeypatch.setattr(obj.DataInCodeRegion, "__getattribute__", no_dictionary)
    budget = _budget()
    assert host._model_bytes(region, budget) > sys.getsizeof(region) + sum(
        sys.getsizeof(value) for value in (region.offset, region.length, region.kind)
    )
    budget.allocation_quantum = 16
    expected = (
        budget._rounded(sys.getsizeof(region))
        + budget._rounded(host._REGION_DICT_HEADER)
        + 2 * budget._rounded(host._REGION_VALUES_BYTES)
        + sum(
            budget._rounded(sys.getsizeof(name))
            + budget._rounded(sys.getsizeof(getattr(region, name)))
            for name in ("offset", "length", "kind")
        )
    )
    assert host._model_bytes(region, budget) == expected
    sections = _sections()
    sections[0] = replace(sections[0], data_in_code=(region,))
    native.OwnedMergedSourceView(sections, compact_budget=budget)


def test_all_section_graphs_and_prior_row_buffers_remain_charged(monkeypatch):
    seen = []
    original = host._pack_final_link_rows

    def record(relocations, count, symbol_index, section_index, budget, retained):
        seen.append(retained)
        return original(relocations, count, symbol_index, section_index, budget, retained)

    monkeypatch.setattr(host, "_pack_final_link_rows", record)
    sections = _sections()
    native.OwnedMergedSourceView(sections, compact_budget=_budget())
    data_graph = host._model_bytes(sections[1], _budget())
    assert seen[0] >= data_graph  # Already live when the first, empty row table is packed.
    assert seen[1] == seen[0] + sys.getsizeof(b"")


def test_failed_later_table_releases_earlier_rows_with_traceback_retained(monkeypatch):
    released = []
    calls = []

    class TrackedRows(bytes):
        def __del__(self):
            released.append(True)

    def injected(*_args):
        calls.append(True)
        if len(calls) == 1:
            return TrackedRows(b"")
        raise RuntimeError("injected second-table failure")

    monkeypatch.setattr(host, "_pack_final_link_rows", injected)
    with pytest.raises(RuntimeError, match="second-table") as retained_exception:
        native.OwnedMergedSourceView(_sections(), compact_budget=_budget())
    assert retained_exception.value.__traceback__ is not None
    assert released == [True]
