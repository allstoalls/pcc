"""Host-only sparse stack index controls; no native code is executed.

This standalone file is not registered in the fixed-count CI inventories.
"""

from pathlib import Path
import sys

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
from tests.python.test_macho_windowed_prepare import _budget


def _models():
    text = ("__TEXT", "__text")
    other = ("__TEXT", "__other")
    symbols = [
        obj.TextSymbol("_late", 12),
        obj.TextSymbol("_local_late", 8, external=False),
        obj.TextSymbol("_first_alias", 4),
        obj.TextSymbol("_second_alias", 4),
        obj.TextSymbol("_local_first", 4, external=False),
        obj.TextSymbol("_first", 0),
    ]
    extra = [obj.TextSymbol("_other_external", 0),
             obj.TextSymbol("_other_local", 0, external=False)]
    merged = {
        text: macho_link._MergedSection(*text, obj.TEXT_SECTION_FLAGS, symbols=symbols),
        other: macho_link._MergedSection(*other, obj.TEXT_SECTION_FLAGS, symbols=extra),
    }
    return merged, [other, text], ["_z_undefined", "_a_undefined"]


@pytest.mark.parametrize("names", [
    (), ("_first",), ("_second_alias",),
    ("_local_first", "_other_external", "_z_undefined"),
    ("_second_alias", "_first_alias", "_other_local", "_a_undefined"),
    ("_missing", "_missing", "_first", "_first"),
    ("_z_undefined", "_a_undefined", "_local_late", "_late"),
])
def test_sparse_numbers_match_the_full_canonical_rows(names):
    merged, order, unresolved = _models()
    before = {key: list(value.symbols) for key, value in merged.items()}
    # Create equal but separately owned strings to check the chosen borrowed
    # owner, not interning or accidental identity with merged symbol names.
    targets = {index: ("!" + name)[1:] for index, name in enumerate(names)}
    budget = _budget(10_000_000)
    budget.retained_bytes = 1234
    ledger = host._Ledger(budget, retained=5678)
    result = host._canonical_stack_symbol_indices(merged, order, unresolved, targets, ledger)
    rows = native._final_symbol_rows([
        obj.Section(key[1], key[0], symbols=tuple(merged[key].symbols)) for key in order
    ], unresolved, {"_z_undefined"}, [0] * len(order))
    assert rows[-1]["name"] == "_z_undefined"
    assert rows[-1]["n_desc"] == spec.N_WEAK_REF
    full = {symbol["name"]: index for index, symbol in enumerate(rows)}
    assert result == {name: full[name] for name in names if name in full}
    assert all(any(name is source for source in targets.values()) for name in result)
    assert {key: list(value.symbols) for key, value in merged.items()} == before
    assert ledger.retained == 5678 + ledger.rounded(sys.getsizeof(result)) + ledger.integers(len(result))
    assert budget.retained_bytes == 1234
    assert list(targets.values()) == list(names)


@pytest.mark.parametrize("function_count", [10, 128])
def test_dense_function_targets_preserve_the_full_index_budget(function_count):
    merged, order, unresolved = _models()
    targets = {index: "_second_alias" for index in range(function_count)}
    full_count = sum(len(merged[key].symbols) for key in order) + len(unresolved)
    sizing = host._Ledger(_budget())
    limit = 123 + sizing.mapping(full_count) + sizing.sequence(full_count)
    ledger = host._Ledger(_budget(limit), retained=123)
    result = host._canonical_stack_symbol_indices(merged, order, unresolved, targets, ledger)
    expected = host._canonical_symbol_indices(
        merged, order, unresolved, host._Ledger(_budget(limit), retained=123),
    )
    assert result == expected and result["_second_alias"] == 6
    assert len(result) == full_count
    assert ledger.retained == 123 + ledger.rounded(sys.getsizeof(result)) + ledger.integers(full_count)
    if function_count > full_count:
        assert 123 + ledger.mapping(function_count) + ledger.sequence(full_count) > limit


def test_sparse_index_keeps_full_sort_scratch_and_prior_retention(monkeypatch):
    merged, order, unresolved = _models()
    targets = {1: "_first", 2: "_first"}
    budget = _budget(1_000_000)
    budget.retained_bytes = 37
    ledger = host._Ledger(budget, retained=123)
    checks = []
    original = budget.check_peak

    def check(raw, **kwargs):
        checks.append((raw, kwargs))
        return original(raw, **kwargs)

    monkeypatch.setattr(budget, "check_peak", check)
    host._canonical_stack_symbol_indices(merged, order, unresolved, targets, ledger)
    count = sum(len(merged[key].symbols) for key in order) + len(unresolved)
    assert checks == [(123 + ledger.mapping(len(targets)) + ledger.sequence(count),
                       {"label": "Mach-O canonical stack symbol index"})]
    assert budget.retained_bytes == 37


def test_sparse_refusal_precedes_wanted_table_and_sort(monkeypatch):
    merged, order, unresolved = _models()

    class Targets(dict):
        def values(self):
            pytest.fail("wanted names accessed before admission")

    def forbidden(*args, **kwargs):
        pytest.fail("symbol sorting began before admission")

    monkeypatch.setattr(host, "sorted", forbidden, raising=False)
    ledger = host._Ledger(_budget(1), retained=0)
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O canonical stack symbol index"):
        host._canonical_stack_symbol_indices(merged, order, unresolved, Targets({1: "_first"}), ledger)
    assert ledger.retained == 0


def test_missing_target_keeps_original_packing_key_error():
    merged, order, unresolved = _models()
    ledger = host._Ledger(_budget(1_000_000))
    sparse = host._canonical_stack_symbol_indices(
        merged, order, unresolved, {1: "_absent", 2: "_absent"}, ledger,
    )
    assert sparse == {}
    full = host._canonical_symbol_indices(merged, order, unresolved, host._Ledger(_budget()))
    rows = [obj.Relocation(8, "_absent", spec.ARM64_RELOC_UNSIGNED, False, 3)]
    for indices in (full, sparse):
        with pytest.raises(KeyError) as caught:
            host._pack_final_link_rows(rows, 1, indices, {}, _budget(), 0)
        assert caught.value.args == ("_absent",)


def test_real_sparse_allocation_fits_when_full_map_does_not():
    key = ("__TEXT", "__text")
    symbols = [obj.TextSymbol("_symbol" + str(index), index * 4,
                              external=bool(index % 2)) for index in range(1024)]
    merged = {key: macho_link._MergedSection(*key, obj.TEXT_SECTION_FLAGS, symbols=symbols)}
    limit = 100_000
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O canonical symbol index"):
        host._canonical_symbol_indices(merged, [key], [], host._Ledger(_budget(limit)))
    ledger = host._Ledger(_budget(limit))
    result = host._canonical_stack_symbol_indices(
        merged, [key], [], {1: "_symbol0", 2: "_symbol1023"}, ledger,
    )
    assert result == {"_symbol0": 0, "_symbol1023": 1023}
    full = host._canonical_symbol_indices(merged, [key], [], host._Ledger(_budget(2_000_000)))
    assert len(result) == 2 and len(full) == 1024
    assert sys.getsizeof(result) < sys.getsizeof(full)
    assert ledger.retained < 1000


def _write_sparse_stack_inputs(tmp_path, *, aliases_per_target=1):
    paths = []
    for index in range(3):
        names = ["_main" if index == offset == 0 else f"_unit{index}_{offset}"
                 for offset in range(96)]
        symbols = [obj.TextSymbol(name, offset * 4, external=offset % 3 != 1)
                   for offset, name in enumerate(names)]
        symbols[1] = obj.TextSymbol("_shared_local", 4, external=False)
        symbols.append(obj.TextSymbol(f"_alias{index}", 4))
        targets = sorted((maps.function_id(f"unit{index}-{label}-{alias}"), name)
                         for label, name in (("local", "_shared_local"), ("alias", f"_alias{index}"))
                         for alias in range(aliases_per_target))
        functions = tuple(maps.FunctionStackMap(
            function_id=function_id, function_address=0,
            code_size=4, frame_size=0, records=(),
        ) for function_id, _name in targets)
        payload = maps.encode_stack_map(maps.PreciseStackMap(arch=maps.ARCH_AARCH64, functions=functions))
        offsets = maps.function_address_offsets(payload)
        sections = [
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 96,
                        flags=obj.TEXT_SECTION_FLAGS, align_log2=2, symbols=tuple(symbols)),
            obj.Section("__data", "__DATA", data=b"\0" * 8, align_log2=3,
                        relocations=(obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),)),
            obj.Section("__pcc_stackmaps", "__DATA", data=payload, align_log2=3,
                        relocations=tuple(obj.Relocation(offset, name, spec.ARM64_RELOC_UNSIGNED, False, 3)
                                          for offset, name in zip(offsets, (name for _function_id, name in targets)))),
        ]
        unit = native.NativeObject.from_sections(sections, undefined=() if index == 0 else ("_main",))
        path = tmp_path / f"sparse{index}.pco"
        path.write_bytes(native.encode_native_object(unit))
        paths.append(str(path))
    return paths


@pytest.mark.parametrize("single_buffer", [False, True])
@pytest.mark.parametrize("aliases_per_target", [1, 50])
def test_full_image_parity_selects_bounded_second_index(tmp_path, monkeypatch, single_buffer, aliases_per_target):
    paths = _write_sparse_stack_inputs(tmp_path, aliases_per_target=aliases_per_target)
    full_calls, sparse_calls = [], []
    full = host._canonical_symbol_indices
    sparse = host._canonical_stack_symbol_indices

    def full_index(*args):
        result = full(*args)
        full_calls.append(len(result))
        return result

    def sparse_index(*args):
        result = sparse(*args)
        sparse_calls.append(len(result))
        return result

    monkeypatch.setattr(host, "_canonical_symbol_indices", full_index)
    monkeypatch.setattr(host, "_canonical_stack_symbol_indices", sparse_index)
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: single_buffer)
    budget = _budget(20_000_000)
    budget.retained_bytes = 1024
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    eager = macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        _consume_inputs=True, _source_view=True, compact_budget=_budget(20_000_000),
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=_budget(20_000_000))
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    if aliases_per_target == 1:
        assert full_calls == [291] and sparse_calls == [6]
    else:
        assert full_calls == [291, 291] and sparse_calls == [291]
    assert budget.retained_bytes == 1024
