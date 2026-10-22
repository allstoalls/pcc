"""Small staged-source replay and error-priority regressions."""

from dataclasses import replace

import pytest

from pcc.backend import (
    macho_host_prepare as host,
    macho_link,
    macho_obj as obj,
    native_object as native,
)
from tests.python.test_macho_windowed_parity import _budget, _companion_inputs, _write_pcos


def _unresolved_section_target_inputs(*, two_targets=False):
    """Keep a real section target rather than rewriting it to Ltail at 12."""
    main, helper = _companion_inputs()
    sections, undefined = main.to_sections()
    pointers = next(index for index, section in enumerate(sections)
                    if section.sectname == "__data")
    rows = tuple(
        replace(row, offset=0, target_offset=4)
        if row.section == ("__TEXT", "__text") else
        replace(row, symbol="", section=("__TEXT", "__text"), target_offset=8)
        if two_targets and row.symbol == "Ltail" else row
        if row.offset != 0 else replace(row, offset=24)
        for row in sections[pointers].relocations
    )
    sections[pointers] = replace(sections[pointers], relocations=rows)
    return [native.NativeObject.from_sections(sections, undefined=undefined), helper]


def test_staged_source_replays_each_section_three_times(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _unresolved_section_target_inputs())
    original = host._logical_staged_rows
    calls = []

    def counted(*args):
        calls.append(None)
        return original(*args)

    monkeypatch.setattr(host, "_logical_staged_rows", counted)
    budget = _budget()
    view = host.prepare_executable_from_paths(
        paths, compact_budget=budget, _staged_rows=True,
    )
    assert len(calls) == 3 * len(view.sections())
    assert budget.retained_bytes == 0


@pytest.mark.parametrize(
    ("semantic_section", "expected_error", "later_visited"),
    (
        (None, "compact section target is not object-relative", None),
        ("__data", "injected semantic error in __data", None),
        ("__pcc_stackmaps", "compact section target is not object-relative", False),
    ),
)
def test_staged_target_error_keeps_section_semantic_priority(
    tmp_path, monkeypatch, semantic_section, expected_error, later_visited,
):
    paths = _write_pcos(tmp_path, _unresolved_section_target_inputs())
    original_target = macho_link._normalized_section_target_value
    original_semantic = native._validate_source_section_relocations
    visited = []

    def bad_target(*args):
        value = original_target(*args)
        if args[-1] == 1:
            return value + 1
        return value

    def after_semantic(section, *args):
        original_semantic(section, *args)
        visited.append(section.sectname)
        if section.sectname == semantic_section:
            raise native.NativeObjectError(
                f"injected semantic error in {section.sectname}"
            )

    if semantic_section is not None:
        monkeypatch.setattr(native, "_validate_source_section_relocations", after_semantic)

    def install_bad_target(event):
        if event == "source_view_begin":
            monkeypatch.setattr(macho_link, "_normalized_section_target_value", bad_target)

    budget = _budget()
    with pytest.raises(macho_link.LinkError, match=expected_error):
        host.prepare_executable_from_paths(
            paths, compact_budget=budget, _staged_rows=True,
            _phase_callback=install_bad_target,
        )
    assert budget.retained_bytes == 0
    if later_visited is False:
        assert "__data" in visited
        assert "__pcc_stackmaps" not in visited


def test_staged_target_normalizer_failure_keeps_first_message(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _unresolved_section_target_inputs(two_targets=True))
    original_target = macho_link._normalized_section_target_value
    seen = []

    def bad_target(*args):
        original_target(*args)
        seen.append(args[0])
        raise native.NativeObjectError(f"injected target failure {len(seen)}")

    def install_bad_target(event):
        if event == "source_view_begin":
            monkeypatch.setattr(macho_link, "_normalized_section_target_value", bad_target)

    budget = _budget()
    with pytest.raises(macho_link.LinkError, match="injected target failure 1") as error:
        host.prepare_executable_from_paths(
            paths, compact_budget=budget, _staged_rows=True,
            _phase_callback=install_bad_target,
        )
    assert "failure 2" not in str(error.value)
    assert len(seen) == 1  # first failure suppresses later target checks
    assert budget.retained_bytes == 0


def test_staged_semantic_early_exit_closes_generator_and_storage(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _unresolved_section_target_inputs(two_targets=True))
    original_semantic = native._validate_relocation
    original_target = macho_link._normalized_section_target_value
    original_rows = host._logical_staged_rows
    original_row_close = host._StagedRelocationRows.close
    original_payload_close = host._StagedSectionPayloads.close
    opened = []
    closed = []
    storage_closed = []
    targets_seen = []

    def observed_rows(*args):
        token = object()
        opened.append(token)
        try:
            yield from original_rows(*args)
        finally:
            closed.append(token)

    def fail_during_semantic(section, relocation, *args):
        if (section.sectname == "__data" and relocation.section is not None
                and targets_seen):
            raise obj.MachOEmitError("injected early semantic failure")
        return original_semantic(section, relocation, *args)

    def bad_target(*args):
        targets_seen.append(args[0])
        return original_target(*args) + 1

    def row_close(self, **kwargs):
        original_row_close(self, **kwargs)
        storage_closed.append(("rows", self.stream is None, self.active_bytes))

    def payload_close(self, **kwargs):
        original_payload_close(self, **kwargs)
        storage_closed.append(("payload", self.stream is None, self.active_bytes))

    def install_failure(event):
        if event == "source_view_begin":
            monkeypatch.setattr(native, "_validate_relocation", fail_during_semantic)
            monkeypatch.setattr(macho_link, "_normalized_section_target_value", bad_target)

    monkeypatch.setattr(host, "_logical_staged_rows", observed_rows)
    monkeypatch.setattr(host._StagedRelocationRows, "close", row_close)
    monkeypatch.setattr(host._StagedSectionPayloads, "close", payload_close)
    budget = _budget()
    with pytest.raises(macho_link.LinkError, match="injected early semantic failure"):
        host.prepare_executable_from_paths(
            paths, compact_budget=budget, _staged_rows=True,
            _phase_callback=install_failure,
        )
    assert opened and closed == opened
    assert targets_seen  # earlier target failure loses to later same-section semantic failure
    assert {row[0] for row in storage_closed} == {"rows", "payload"}
    assert all(closed for _, closed, _ in storage_closed)
    assert {kind: active for kind, _, active in storage_closed} == {
        "rows": 0, "payload": 0,
    }
    assert budget.retained_bytes == 0
