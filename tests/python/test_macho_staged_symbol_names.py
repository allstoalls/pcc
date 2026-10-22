"""Bounded host-only canonical-name reuse during staged validation."""

import sys

import pytest

from pcc.backend import (
    macho_exec,
    macho_host_output,
    macho_host_prepare as host,
    macho_link,
    macho_spec as spec,
    native_object as native,
)
from tests.python.test_macho_windowed_parity import (
    _budget,
    _companion_inputs,
    _write_pcos,
)


def test_staged_replays_share_one_canonical_name_tuple(tmp_path, monkeypatch):
    inputs = _companion_inputs()
    paths = _write_pcos(tmp_path, inputs)
    expected_budget = _budget()
    expected_view = macho_exec.prepare_executable_object(
        inputs, _consume_inputs=True, _source_view=True, compact_budget=expected_budget,
    )
    expected = macho_exec.link_prepared_executable(
        expected_view, compact_budget=expected_budget,
    )
    original_rows = host._logical_staged_rows
    original_name = macho_link._relocation_symbol_name
    original_check = host._Ledger.check
    caches, fallbacks, charges = [], [], {}

    def observed_rows(data, sections, symbols, names=None):
        assert type(names) is tuple
        assert len(names) == len(symbols)
        assert all(name is symbol["name"] for name, symbol in zip(names, symbols))
        caches.append(names)
        return original_rows(data, sections, symbols, names)

    def observed_name(*args, **kwargs):
        fallbacks.append(kwargs["context"])
        return original_name(*args, **kwargs)

    def observed_check(self, extra=0, *, label="Mach-O input window"):
        if label == "Mach-O staged source surviving metadata":
            charges["metadata"] = self.retained
        elif label == "Mach-O staged symbol-name cache":
            assert sys._getframe(1).f_locals["symbol_names"] is None
            charges["admitted"] = extra
        elif label == "Mach-O compact row validation replay":
            charges.setdefault("replay", self.retained)
        return original_check(self, extra, label=label)

    def observe_source(event):
        if event == "source_view_begin":
            monkeypatch.setattr(host, "_logical_staged_rows", observed_rows)
            monkeypatch.setattr(macho_link, "_relocation_symbol_name", observed_name)
            monkeypatch.setattr(host._Ledger, "check", observed_check)

    budget = _budget()
    budget.retained_bytes = 1232
    view = host.prepare_executable_from_paths(
        paths, compact_budget=budget, _staged_rows=True, _phase_callback=observe_source,
    )
    assert len(caches) == 3 * len(view.sections())
    assert all(cache is caches[0] for cache in caches)
    # __eh_frame's shape validator does not consume its iterator: only the
    # counting and semantic passes fold each of its two SUBTRACTOR pairs.
    assert fallbacks.count("SUBTRACTOR relocation") == 4
    assert fallbacks.count("SUBTRACTOR minuend relocation") == 4
    assert len(fallbacks) == 8  # Ordinary extern rows no longer resolve per replay.
    tuple_bytes = budget._rounded(sys.getsizeof(caches[0]))
    assert charges["replay"] == charges["metadata"] + tuple_bytes
    assert charges["admitted"] >= tuple_bytes
    assert not any(value is caches[0] for value in vars(view).values())
    assert budget.retained_bytes == 1232
    monkeypatch.setattr(macho_link, "_relocation_symbol_name", original_name)
    pending = [view]
    view = None
    actual = macho_host_output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected
    assert budget.retained_bytes == 1232


def test_staged_name_cache_refusal_precedes_allocation_and_closes_stores(
    tmp_path, monkeypatch,
):
    paths = _write_pcos(tmp_path, _companion_inputs())
    original_check = host._Ledger.check
    original_rows = host._StagedRelocationRows.close
    original_payloads = host._StagedSectionPayloads.close
    closed, admitted = [], []

    def reject_cache(self, extra=0, *, label="Mach-O input window"):
        if label == "Mach-O staged symbol-name cache":
            assert sys._getframe(1).f_locals["symbol_names"] is None
            admitted.append(extra)
            self.budget.limit_bytes = self.budget.retained_bytes + self.retained + extra - 1
        return original_check(self, extra, label=label)

    def close_rows(self, **kwargs):
        original_rows(self, **kwargs)
        closed.append(("rows", self.stream is None, self.active_bytes))

    def close_payloads(self, **kwargs):
        original_payloads(self, **kwargs)
        closed.append(("payloads", self.stream is None, self.active_bytes))

    monkeypatch.setattr(host._Ledger, "check", reject_cache)
    monkeypatch.setattr(host._StagedRelocationRows, "close", close_rows)
    monkeypatch.setattr(host._StagedSectionPayloads, "close", close_payloads)
    budget = _budget()
    budget.retained_bytes = 1232
    with pytest.raises(macho_link.LinkError, match="Mach-O staged symbol-name cache") as error:
        host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
    assert admitted and admitted[0] > 0
    assert "byte budget exceeded" in str(error.value)
    assert {kind for kind, _, _ in closed} == {"rows", "payloads"}
    assert all(is_closed and active == 0 for _, is_closed, active in closed)
    assert budget.retained_bytes == 1232


def test_staged_name_cache_matches_uncached_canonical_rows():
    symbols = [
        {"name": "Llocal$link0", "n_type": spec.N_SECT},
        {"name": "_main", "n_type": spec.N_SECT | spec.N_EXT},
        {"name": "_optional", "n_type": spec.N_UNDF | spec.N_EXT,
         "n_desc": spec.N_WEAK_REF},
    ]
    sections = [{"segname_str": "__TEXT", "sectname_str": "__text"}]
    rows = (
        (0, 0, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
        (8, 2, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
        (16, 7, 0, 3, 0, spec.ARM64_RELOC_ADDEND),
        (16, 1, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
        (24, 1, 0, 3, 0, spec.ARM64_RELOC_UNSIGNED),
        (32, 0, 0, 3, 1, spec.ARM64_RELOC_SUBTRACTOR),
        (32, 1, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED),
    )
    raw = b"".join(native._FINAL_LINK_RELOCATION.pack(*row) for row in rows)
    expected = list(host._logical_staged_rows(raw, sections, symbols))
    actual = list(host._logical_staged_rows(
        raw, sections, symbols, tuple(symbol["name"] for symbol in symbols),
    ))
    assert actual == expected
    assert [row.symbol for row in actual] == [
        "Llocal$link0", "_optional", "_main", "", "Llocal$link0",
    ]
    assert actual[2].addend == 7
    assert actual[3].section == ("__TEXT", "__text")
    assert actual[4].minuend == "_main"


@pytest.mark.parametrize("symbol_index", (0, 2))
def test_staged_name_cache_keeps_default_and_out_of_range_fallback(symbol_index):
    symbols = [{"name": "_main", "n_type": spec.N_SECT | spec.N_EXT}]
    raw = native._FINAL_LINK_RELOCATION.pack(
        0, symbol_index, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED,
    )
    outcomes = []
    for names in (None, (), (symbols[0]["name"],)):
        try:
            outcomes.append(list(host._logical_staged_rows(raw, [], symbols, names)))
        except macho_link.LinkError as error:
            outcomes.append(str(error))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    if symbol_index:
        assert outcomes[0] == "extern relocation names symbol index 2, but the input has 1 symbols"
    else:
        assert outcomes[0][0].symbol == "_main"


@pytest.mark.parametrize("malformation", ("symbol", "minuend", "width", "trailing"))
def test_staged_name_cache_preserves_subtractor_errors(malformation):
    symbols = [{"name": "_main", "n_type": spec.N_SECT | spec.N_EXT}]
    raw = native._FINAL_LINK_RELOCATION.pack(
        0, 2 if malformation == "symbol" else 0, 0, 3, 1, spec.ARM64_RELOC_SUBTRACTOR,
    )
    if malformation != "trailing":
        raw += native._FINAL_LINK_RELOCATION.pack(
            0, 2 if malformation == "minuend" else 0, 0,
            2 if malformation == "width" else 3, 1, spec.ARM64_RELOC_UNSIGNED,
        )
    errors = []
    for names in (None, (symbols[0]["name"],)):
        with pytest.raises(macho_link.LinkError) as error:
            list(host._logical_staged_rows(raw, [], symbols, names))
        errors.append(str(error.value))
    assert errors[0] == errors[1]
    assert "SUBTRACTOR" in errors[0]
