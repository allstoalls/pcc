"""Bounded host ownership and complete-image parity for the address index.

These tests execute CPython preparation and inspect emitted arm64 Mach-O
bytes. They do not execute native code or qualify a full Mac closure.
"""

from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import weakref

import pytest

from pcc.backend import (
    macho_exec,
    macho_host_prepare as host,
    macho_link,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
    precise_stackmap as maps,
)
from pcc.backend.ar_writer import write_archive


def _budget(limit=32 * 1024 * 1024):
    word = struct.calcsize("P")
    rounded = lambda n: (n + 2 * word - 1) // (2 * word) * (2 * word)
    integer = rounded(max(sys.getsizeof((1 << 64) - 1), sys.getsizeof(-(1 << 63))))
    return maps.CompactStackMapBudget(
        limit, 1_000_000, set_header_bytes=rounded(sys.getsizeof(set())),
        set_entry_bytes=2 * word, integer_bytes=integer,
        metadata_entry_bytes=64 * word + rounded(sys.getsizeof((0, 0, 0)))
        + 3 * integer + rounded(sys.getsizeof(b"")),
        allocation_quantum=2 * word,
    )


def _completed_bytes(result, ledger):
    return (ledger.rounded(sys.getsizeof(result))
            + len(result) * ledger.rounded(sys.getsizeof((None, None))))


def _dynamic_name(value):
    result = value.encode().decode()
    # Intern a different equal object: the source name itself must not be
    # accidentally protected by CPython's intern table during this test.
    interned = sys.intern(value.encode().decode())
    assert result == interned and result is not interned
    return result


def test_section_target_index_rejects_before_entry_iteration():
    class UnreadSymbols:
        def __len__(self):
            return 19

        def __iter__(self):
            pytest.fail("address entries were visited before admission")

    merged = {("__TEXT", "__text"): SimpleNamespace(symbols=UnreadSymbols())}
    budget = _budget()
    budget.retained_bytes = 1234
    ledger = host._Ledger(budget, retained=5678)
    reservation = ledger.mapping(19)
    budget.limit_bytes = budget.retained_bytes + ledger.retained + reservation - 1
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O section-target symbol index"):
        host._section_target_symbol_index(merged, ledger)
    assert ledger.retained == 5678
    assert budget.retained_bytes == 1234


@pytest.mark.parametrize("empty_sections", (False, True))
def test_empty_section_target_index_preserves_existing_balances(empty_sections):
    merged = {("__TEXT", "__text"): SimpleNamespace(symbols=[])} if empty_sections else {}
    budget = _budget()
    budget.retained_bytes = 321
    ledger = host._Ledger(budget, retained=9876)
    result = host._section_target_symbol_index(merged, ledger)
    assert result == {}
    assert ledger.retained == 9876 + ledger.rounded(sys.getsizeof(result))
    assert budget.retained_bytes == 321


@pytest.mark.parametrize("first_external", (False, True))
def test_completed_index_charges_distinct_keys_and_borrows_source_identity(
    monkeypatch, first_external,
):
    keys = [(_dynamic_name("__TEXT"), _dynamic_name("__text")),
            (_dynamic_name("__DATA"), _dynamic_name("__data"))]
    symbols = []
    for section in range(2):
        offset = int("1000003")
        assert offset is not int(str(offset))
        symbols.append([
            obj.TextSymbol(_dynamic_name(f"_later_{section}"), int("1000091")),
            obj.TextSymbol(_dynamic_name(f"_first_{section}"), offset,
                           external=first_external),
            obj.TextSymbol(_dynamic_name(f"_alias_{section}"), int("1000003"),
                           external=not first_external),
        ])
    merged = {key: SimpleNamespace(symbols=rows) for key, rows in zip(keys, symbols)}
    budget = _budget()
    budget.retained_bytes = 1234
    owners = host._model_bytes(keys, budget) + sum(host._model_bytes(rows, budget) for rows in symbols)
    ledger = host._Ledger(budget, retained=5678 + owners)
    before = ledger.retained
    canonical = host._canonical_symbol_indices(merged, keys, (), host._Ledger(_budget()))
    if first_external:
        assert canonical[symbols[0][2].name] < canonical[symbols[0][1].name]
    original_keep = host._Ledger.keep
    admitted = []

    def keep(self, amount, *, label="Mach-O retained preparation owners"):
        if label == "Mach-O section-target symbol index":
            admitted.append((self.retained, amount))
        return original_keep(self, amount, label=label)

    monkeypatch.setattr(host._Ledger, "keep", keep)
    result = host._section_target_symbol_index(merged, ledger)
    assert admitted == [(before, ledger.mapping(6))]
    assert len(result) == 4
    assert list(result) == [(key, symbol.offset) for key, rows in zip(keys, symbols)
                            for symbol in rows[:2]]
    for address, name in result.items():
        key, offset = address
        first = next(symbol for symbol in merged[key].symbols if symbol.offset == offset)
        assert any(key is source_key for source_key in keys)
        assert offset is first.offset
        assert name is first.name
    assert ledger.retained == before + _completed_bytes(result, ledger)
    assert _completed_bytes(result, ledger) < ledger.mapping(6)
    assert budget.retained_bytes == 1234
    assert [symbol.name for symbol in symbols[0]] == ["_later_0", "_first_0", "_alias_0"]


def _inputs(tmp_path, *, section_targets=True):
    paths = []
    for index in range(3):
        text_symbols = [obj.TextSymbol("_main" if index == 0 else f"_unit{index}", 0),
                        obj.TextSymbol(f"_external_first_{index}", 512),
                        obj.TextSymbol("_shared_local", 512, external=False)]
        data_symbols = [obj.TextSymbol(f"_data_first_{index}", 512),
                        obj.TextSymbol(f"_data_alias_{index}", 512, external=False)]
        rows = [obj.Relocation(16, "_shared_local", spec.ARM64_RELOC_UNSIGNED, False, 3),
                obj.Relocation(24, "_needed", spec.ARM64_RELOC_UNSIGNED, False, 3)]
        if section_targets:
            rows[:0] = [
                obj.Relocation(0, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                               section=("__TEXT", "__text"), target_offset=512),
                obj.Relocation(8, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                               section=("__DATA", "__data"), target_offset=512),
            ]
        value = native.NativeObject.from_sections([
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 160,
                        flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                        symbols=tuple(text_symbols)),
            obj.Section("__data", "__DATA", data=b"\0" * 640, align_log2=3,
                        symbols=tuple(data_symbols), relocations=tuple(rows)),
        ], undefined=("_needed",))
        path = tmp_path / f"index-{index}.pco"
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    archive = write_archive([
        (name + ".o", obj.emit_object([
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6",
                        flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                        symbols=(obj.TextSymbol(name, 0),)),
        ])) for name in ("_needed", "_unused")
    ])
    return paths, archive


def test_no_section_targets_do_not_construct_or_charge_index(tmp_path, monkeypatch):
    paths, archive = _inputs(tmp_path, section_targets=False)

    def forbidden(*args, **kwargs):
        pytest.fail("address index was constructed without section targets")

    monkeypatch.setattr(host, "_section_target_symbol_index", forbidden)
    budget = _budget()
    budget.retained_bytes = 1234
    result = host.prepare_executable_from_paths(paths, archives=(archive,), compact_budget=budget)
    assert result and budget.retained_bytes == 1234


def test_only_index_reservation_changes_across_second_pass_windows(tmp_path, monkeypatch):
    paths, archive = _inputs(tmp_path)
    archive_path = tmp_path / "retained.a"
    archive_path.write_bytes(archive)
    original_index, original_read = host._section_target_symbol_index, host._read_window
    results = []
    for legacy in (True, False):
        windows, checks, index_charge = [], [], []
        budget = _budget()
        budget.retained_bytes = 1234
        original_check = budget.check_peak

        def check(raw_bytes, records=0, entries=0, *, label=""):
            checks.append((label, raw_bytes))
            return original_check(raw_bytes, records=records, entries=entries, label=label)

        def index(merged, ledger):
            reserved = ledger.mapping(sum(len(target.symbols) for target in merged.values()))
            if legacy:
                # Exact prior construction accounting, as a labeled test
                # reference. Production executes the new helper below.
                ledger.keep(reserved, label="Mach-O section-target symbol index")
                result = {}
                for key, target in merged.items():
                    for symbol in target.symbols:
                        result.setdefault((key, symbol.offset), symbol.name)
            else:
                result = original_index(merged, ledger)
            index_charge.append((reserved, _completed_bytes(result, ledger)))
            return result

        def read(path, ledger, expected_size=None, expected_digest=None):
            if expected_digest is not None:
                windows.append(ledger.retained)
            return original_read(path, ledger, expected_size, expected_digest)

        with monkeypatch.context() as patch:
            patch.setattr(budget, "check_peak", check)
            patch.setattr(host, "_section_target_symbol_index", index)
            patch.setattr(host, "_read_window", read)
            prepared = host.prepare_executable_from_paths(
                paths, archive_paths=(str(archive_path),), compact_budget=budget,
            )
            image = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
        assert budget.retained_bytes == 1234
        results.append((windows, checks, index_charge, image))
    baseline, candidate = results
    assert baseline[2] == candidate[2] and len(candidate[2]) == 1
    reserved, completed = candidate[2][0]
    assert reserved > completed
    assert len(baseline[0]) == len(candidate[0]) == len(paths)
    assert [old - new for old, new in zip(baseline[0], candidate[0])] == [reserved - completed] * len(paths)
    boundary = next(i for i, row in enumerate(baseline[1])
                    if row[0] == "Mach-O section-target symbol index") + 1
    assert baseline[1][:boundary] == candidate[1][:boundary]
    assert baseline[3] == candidate[3]


@pytest.mark.parametrize("single_buffer", (False, True))
@pytest.mark.parametrize("archive_mode", ("bytes", "paths"))
def test_index_survives_descriptor_retirement_and_matches_complete_eager_image(
    tmp_path, monkeypatch, single_buffer, archive_mode,
):
    paths, archive = _inputs(tmp_path)
    archive_args = {"archives": (archive,)}
    if archive_mode == "paths":
        archive_path = tmp_path / "fixture.a"
        archive_path.write_bytes(archive)
        archive_args = {"archive_paths": (str(archive_path),)}
    eager = macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        archives=(archive,), _consume_inputs=True, _source_view=True, compact_budget=_budget(),
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=_budget())
    original_descriptor, original_read = host._read_descriptor, host._read_window
    original_index = host._section_target_symbol_index
    original_section, original_symbol = native.PackedNativeSection, native.PackedNativeSymbol
    original_descriptor_type = host._InputDescriptor
    groups, descriptor_refs, observations, charges = [], [], [], []

    class TrackedSection(original_section):
        pass

    class TrackedSymbol(original_symbol):
        pass

    class TrackedDescriptor(original_descriptor_type):
        pass

    def descriptor(*args):
        result = original_descriptor(*args)
        descriptor_refs.append(weakref.ref(result))
        groups.append([weakref.ref(owner) for owner in (*result.sections, *result.symbols)])
        return result

    def build_index(merged, ledger):
        before = ledger.retained
        result = original_index(merged, ledger)
        completed = _completed_bytes(result, ledger)
        assert ledger.retained == before + completed
        assert len(result) < sum(len(target.symbols) for target in merged.values())
        charges.append((before, completed, ledger.retained))
        return result

    def read(path, ledger, expected_size=None, expected_digest=None):
        if expected_digest is not None:
            state = sys._getframe(1).f_locals
            current = state["index"]
            assert all(ref() is None for ref in descriptor_refs[:current])
            assert all(ref() is None for group in groups[:current] for ref in group)
            result, merged, order = state["symbol_at"], state["merged"], state["order"]
            assert charges and result
            if current == 0:
                assert ledger.retained == charges[0][2]
                # Archive selection has retired its aggregate buffers;
                # selected member descriptors retain their detached bytes.
                assert state["archive_buffers"] == []
            else:
                assert all(mapping == {} for mapping in state["local_renames"][:current])
            assert state["descriptors"][current] is not None
            for (key, offset), name in result.items():
                first = next(symbol for symbol in merged[key].symbols if symbol.offset == offset)
                assert any(key is item for item in order)
                assert offset is first.offset and name is first.name
            observations.append(current)
        return original_read(path, ledger, expected_size, expected_digest)

    monkeypatch.setattr(native, "PackedNativeSection", TrackedSection)
    monkeypatch.setattr(native, "PackedNativeSymbol", TrackedSymbol)
    monkeypatch.setattr(host, "_InputDescriptor", TrackedDescriptor)
    monkeypatch.setattr(host, "_read_descriptor", descriptor)
    monkeypatch.setattr(host, "_read_window", read)
    monkeypatch.setattr(host, "_section_target_symbol_index", build_index)
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: single_buffer)
    budget = _budget()
    budget.retained_bytes = 1234
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget, **archive_args)
    assert budget.retained_bytes == 1234
    assert observations == list(range(len(paths)))
    assert all(ref() is None for ref in descriptor_refs)
    assert all(ref() is None for group in groups for ref in group)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    assert budget.retained_bytes == 1234
    names = [symbol["name"] for symbol in prepared.symbols()]
    assert "_needed" in names and "_unused" not in names
    assert {"_shared_local", "_shared_local$link1", "_shared_local$link2"}.issubset(names)
    for section in prepared.sections():
        if section["sectname_str"] == "__data":
            rows = list(prepared.iter_relocations(section))
            owners = {row["r_address"]: names[row["r_symbolnum"]] for row in rows}
            for index in range(len(paths)):
                local = "_shared_local" + (f"$link{index}" if index else "")
                assert owners[640 * index] == local
                assert owners[640 * index + 8] == f"_data_alias_{index}"
