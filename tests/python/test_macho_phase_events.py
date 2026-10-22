"""Opt-in phase evidence without profiler hooks or retained linker owners."""

import io
import json
import os
from pathlib import Path
import re
import sys
import weakref

import pytest

from pcc.backend import macho_codesign as codesign
from pcc.backend import macho_exec, macho_host_output as output
from pcc.backend import macho_host_phase as events, macho_host_prepare as host
from pcc.backend import native_object as native, precise_stackmap as maps, stackmap_host_budget
from pcc.frontends.python import pipeline_self_backend_link as pipeline
from tests.python.test_macho_owned_output import _stack_inputs, _tracked_buffers
from tests.python.test_macho_windowed_parity import _prepared_state
from tests.python.test_macho_windowed_prepare import _budget, _write_symbol_inputs


PREFIX = "PCC_MACHO_LINK_PHASE "


class _Stderr(io.StringIO):
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.writes = 0
        self.flushes = 0
        self.persisted = ""

    def write(self, value):
        self.writes += 1
        if self.failure == "write":
            raise OSError("diagnostic write failed")
        return super().write(value)

    def flush(self):
        self.flushes += 1
        if self.failure == "flush":
            raise OSError("diagnostic flush failed")
        self.persisted = self.getvalue()


def _rows(stream):
    return [json.loads(line[len(PREFIX):]) for line in stream.persisted.splitlines()
            if line.startswith(PREFIX)]


def _sink(monkeypatch, failure=None):
    stream = _Stderr(failure)
    monkeypatch.setattr(events.sys, "stderr", stream)
    monkeypatch.setenv("PCC_MACHO_LINK_PHASE_EVENTS", "1")
    return stream, events.phase_callback_from_environment()


def _pipeline(paths, destination, profile=None):
    pipeline._owned_macho_link_in_process(
        asm_path=None, pcc_asm_inputs=(), pcc_native_object_inputs=tuple(paths),
        runtime_archive=None, extra_link_inputs=(), tmp_out_path=str(destination),
        link_profile_path=None if profile is None else str(profile),
    )


@pytest.mark.parametrize("setting", (None, "", "0", "true", "yes", "2"))
def test_phase_events_are_disabled_unless_explicitly_one(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("PCC_MACHO_LINK_PHASE_EVENTS", raising=False)
    else:
        monkeypatch.setenv("PCC_MACHO_LINK_PHASE_EVENTS", setting)
    assert events.phase_callback_from_environment() is None


def test_sink_is_bounded_scalar_closure_and_flushes_each_event(monkeypatch):
    stream, callback = _sink(monkeypatch)
    assert set(callback.__code__.co_freevars) == {"pid", "sequence", "failed"}
    assert all(type(cell.cell_contents) in (int, bool) for cell in callback.__closure__)
    callback("link_begin")
    callback("link_end")
    rows = _rows(stream)
    assert stream.writes == stream.flushes == 2
    assert [row["sequence"] for row in rows] == [0, 1]
    assert [row["event"] for row in rows] == ["link_begin", "link_end"]
    assert all(set(row) == {"schema", "pid", "sequence", "monotonic_ns", "event"}
               for row in rows)
    assert all(row["schema"] == "pcc.macho-link-phase.v1" and row["pid"] == os.getpid()
               for row in rows)
    assert rows[0]["monotonic_ns"] <= rows[1]["monotonic_ns"]


@pytest.mark.parametrize("failure", ("write", "flush"))
def test_failed_sink_stops_without_replacing_work_exception(monkeypatch, failure):
    stream, callback = _sink(monkeypatch, failure)
    callback("link_begin")
    callback("link_end")
    assert stream.writes == 1
    assert stream.flushes == (failure == "flush")
    assert all(type(cell.cell_contents) in (int, bool) for cell in callback.__closure__)
    original = ValueError("original compiler failure")
    with pytest.raises(ValueError) as caught:
        try:
            raise original
        finally:
            callback("cleanup")
    assert caught.value is original


def test_private_callback_and_factory_failures_are_fail_open(monkeypatch):
    def failed(*_args):
        raise MemoryError("diagnostic allocation")
    events.emit_phase(failed, "event")
    monkeypatch.setenv("PCC_MACHO_LINK_PHASE_EVENTS", "1")
    monkeypatch.setattr(events.os, "getpid", failed)
    assert events.phase_callback_from_environment() is None


@pytest.mark.parametrize("enabled", (False, True))
def test_owned_bytes_addresses_and_public_signature_events_are_unchanged(
    tmp_path, monkeypatch, enabled,
):
    paths = _stack_inputs(tmp_path, records=32, data_bytes=64)
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
        compact_budget=_budget(50_000_000),
    )
    stream, callback = _sink(monkeypatch)
    callback = callback if enabled else None
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(
        paths, compact_budget=budget, _phase_callback=callback,
    )]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    public_events = []

    def public_phase(event):
        public_events.append(event)
        assert source() is None
        assert len([owner for owner in owners if not owner.closed]) == 1

    actual = output.link_owned_executable(
        pending.pop(), compact_budget=budget, phase_callback=public_phase,
        _phase_callback=callback,
    )
    assert actual == expected
    assert public_events == ["sign_begin", "sign_end"]
    assert budget.retained_bytes == 137 and source() is None
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    names = [row["event"] for row in _rows(stream)]
    assert ("stackmap_validate_begin" in names) is enabled
    assert ("stackmap_validate_end" in names) is enabled


@pytest.mark.parametrize("failure", (None, "write", "flush", "factory"))
def test_real_pipeline_events_preserve_owned_route_image_and_profile(
    tmp_path, monkeypatch, failure,
):
    paths = _stack_inputs(tmp_path, records=32, data_bytes=64)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget",
                        lambda: _budget(50_000_000))
    monkeypatch.delenv("PCC_MACHO_LINK_PHASE_EVENTS", raising=False)
    _pipeline(paths, tmp_path / "off")
    stream, _callback = _sink(monkeypatch, failure)
    if failure == "factory":
        def failed_factory():
            raise MemoryError("diagnostic factory")
        monkeypatch.setattr(events, "phase_callback_from_environment", failed_factory)
    trace, profile = sys.gettrace(), sys.getprofile()
    monitoring = [(sys.monitoring.get_tool(index), sys.monitoring.get_events(index))
                  for index in range(6)]
    supported = output._owned_output_supported()
    _pipeline(paths, tmp_path / "on", tmp_path / "profile.json")
    assert (tmp_path / "on").read_bytes() == (tmp_path / "off").read_bytes()
    assert output._owned_output_supported() is supported is True
    assert sys.gettrace() is trace and sys.getprofile() is profile
    assert monitoring == [(sys.monitoring.get_tool(index), sys.monitoring.get_events(index))
                          for index in range(6)]
    receipt = json.loads((tmp_path / "profile.json").read_text())
    assert receipt["schema"] == "pcc.macho-link-profile.v1"
    assert set(receipt["phases_ms"]) == {
        "assemble_pool", "decode_pco", "prepare_link", "sign", "validate", "write",
    }
    if failure:
        assert stream.writes == (failure != "factory")
        return
    all_names = [row["event"] for row in _rows(stream)]
    names = [name for name in all_names
             if not name.startswith(("section_rows_", "section_payload_"))]
    assert names == [
        "link_begin", "host_budget_available", "prepare_inputs_begin",
        "input_scan_begin", "input_scan_end", "placement_begin", "placement_end",
        "input_detach_begin", "input_detach_end", "merge_index_begin", "merge_index_end",
        "stackmap_merge_begin", "stackmap_merge_end", "final_layout_begin", "final_layout_end",
        "section_joins_begin", "section_joins_end", "source_view_begin", "source_view_end",
        "prepare_inputs_end", "owned_output_selected", "output_model_begin", "output_model_end",
        "output_prepare_begin", "relocations_begin", "relocations_end",
        "stackmap_validate_begin", "stackmap_payload_validate_begin", "stackmap_payload_validate_end",
        "stackmap_thaw_begin", "stackmap_thaw_end", "stackmap_validate_end", "fixups_begin", "fixups_end",
        "symbols_begin", "symbols_end", "output_prepare_end", "output_spool_begin",
        "output_spool_end", "final_backing_begin", "final_backing_end", "sign_begin", "sign_end",
        "signature_verify_begin", "signature_verify_end", "write_begin", "write_end", "link_end",
    ]
    assert stream.writes == stream.flushes == len(all_names)


@pytest.mark.parametrize("failure", ("validator", "sign"))
@pytest.mark.parametrize("sink_failure", (None, "write", "flush"))
@pytest.mark.parametrize("table_format", (None, "2", "3"))
def test_failure_phase_is_durable_and_does_not_mask_original_error(
    tmp_path, monkeypatch, failure, sink_failure, table_format,
):
    if table_format is None:
        monkeypatch.delenv(maps.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, table_format)
    paths = _stack_inputs(tmp_path, records=32, data_bytes=64)
    stream, callback = _sink(monkeypatch, sink_failure)
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    original = ValueError("original " + failure + " failure")
    public_events = []
    validated_versions = []

    def failed(*_args, **_kwargs):
        if failure == "validator":
            validated_versions.append(maps._payload_version(_args[0]))
        if sink_failure is None:
            assert _rows(stream)[-1]["event"] == (
                "stackmap_payload_validate_begin" if failure == "validator" else "sign_begin"
            )
        raise original

    if failure == "validator":
        # The final host validator dispatches v3 directly to the runtime
        # validator, bypassing the v2 public entry. Inject at the boundary
        # whose failure/cleanup contract this test exercises for either format.
        monkeypatch.setattr(output.host_validation, "validate_stack_map_payload", failed)
    else:
        monkeypatch.setattr(codesign, "build_signature", failed)
    with pytest.raises(ValueError) as caught:
        output.link_owned_executable(
            pending.pop(), compact_budget=budget, phase_callback=public_events.append,
            _phase_callback=callback,
        )
    assert caught.value is original
    assert validated_versions == (
        [maps.VERSION if table_format == "2" else maps.RUNTIME_TABLE_VERSION]
        if failure == "validator" else []
    )
    assert all(owner.closed for owner in owners)
    assert budget.retained_bytes == 137 and source() is None
    assert public_events == ([] if failure == "validator" else ["sign_begin", "sign_end"])
    if sink_failure is None:
        names = [row["event"] for row in _rows(stream)]
        assert ("stackmap_validate_end" not in names if failure == "validator"
                else "sign_end" not in names)


def test_phase_adapter_stays_out_of_native_import_closure():
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets
    from pcc.frontends.python.pipeline_import_scan import _without_inactive_implementation_imports
    path = Path(pipeline.__file__)
    source = path.read_text()
    marker = "from pcc.backend.macho_host_phase import"
    assert marker in source
    assert marker not in _without_inactive_implementation_imports(source, implementation="pcc")
    assert marker in _without_inactive_implementation_imports(source, implementation="cpython")
    dependencies = {name for _path, name in _package_import_targets(
        str(path), pipeline.__name__, root_dir=str(Path(__file__).resolve().parents[2]),
    )}
    assert "pcc.backend.macho_host_phase" not in dependencies


@pytest.mark.parametrize("event", ("stackmap_payload_validate_end", "stackmap_thaw_begin"))
@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_phase_callback_cannot_open_an_instrumented_thaw_window(
    tmp_path, monkeypatch, event, instrument,
):
    paths = _stack_inputs(tmp_path, count=1, records=32, data_bytes=64)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    owners = _tracked_buffers(monkeypatch)
    prior_trace, prior_profile = sys.gettrace(), sys.getprofile()

    def hook(_frame, _event, _arg):
        return hook

    def phase(name):
        if name == event:
            if instrument == "trace":
                sys.settrace(hook)
            else:
                sys.setprofile(hook)

    try:
        with pytest.raises(output.LinkError, match="uninstrumented"):
            output.link_owned_executable(pending.pop(), compact_budget=budget, _phase_callback=phase)
    finally:
        sys.settrace(prior_trace)
        sys.setprofile(prior_profile)
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert max(len(owner.addresses) for owner in owners) == 3  # snapshot, never thaw


def test_profile_write_failure_has_no_successful_link_end(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, records=8, data_bytes=64)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget",
                        lambda: _budget(50_000_000))
    stream, _callback = _sink(monkeypatch)
    with pytest.raises(pipeline.SelfBackendLinkError, match="FileNotFoundError"):
        _pipeline(paths, tmp_path / "program", tmp_path / "missing" / "profile.json")
    names = [row["event"] for row in _rows(stream)]
    assert names[-1] == "write_end" and "link_end" not in names


def test_unbudgeted_reference_route_is_identified(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, records=8, data_bytes=64)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget", lambda: None)
    monkeypatch.delenv("PCC_MACHO_LINK_PHASE_EVENTS", raising=False)
    _pipeline(paths, tmp_path / "off")
    stream, _callback = _sink(monkeypatch)
    _pipeline(paths, tmp_path / "on")
    assert (tmp_path / "off").read_bytes() == (tmp_path / "on").read_bytes()
    names = [row["event"] for row in _rows(stream)]
    assert names[:6] == ["link_begin", "host_budget_unavailable", "reference_prepare_begin",
                         "reference_prepare_end", "reference_link_begin", "reference_link_end"]
    assert names[-1] == "link_end"


def test_native_ir_omits_optional_host_event_callback():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    source = '''def run(value):
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        phase_events = None
        try:
            from unavailable_host_adapter import phase_callback_from_environment
            phase_events = phase_callback_from_environment()
        except BaseException:
            phase_events = None
    import sys as execution_runtime
    if execution_runtime.implementation.name == "cpython":
        if phase_events is not None:
            phase_events("link_begin")
    return value + 1
'''
    module = infer_module(parse_and_lift(source, "phase_boundary.py", "phase_boundary"))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._strict_no_libpython = True
    result = str(generator.generate())
    assert "unavailable_host_adapter" not in result
    definition = re.search(r"^define[^\n]*@user_phase_boundary_run\([^\n]*\) \{", result, re.MULTILINE)
    assert definition is not None
    body = result[definition.start():].split("\n}\n", 1)[0]
    assert not re.search(r"\bcall[^\n]*phase_callback_from_environment", body)
    assert "phase_events.obj.call" not in body
    assert "@py_cpy_" not in body


def test_ci_enables_events_only_for_the_existing_mac_stage1_build():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pcc1-package-parity.yml").read_text()
    marker = '          PCC_MACHO_LINK_PHASE_EVENTS: "1"'
    assert workflow.count(marker) == 1
    mac = workflow.split("  pcc1-package-parity:\n", 1)[1]
    build = mac.split("      - name: Build current pcc1\n", 1)[1].split("      - name:", 1)[0]
    assert marker in build and 'PCC_HOST_INDEXED_PROCESS_SPLIT: "1"' in build
    assert "--phase stage1" in build
    assert "build/bootstrap/stage*.process.*/target.stderr" in mac
    assert 'PCC_BOOTSTRAP_STAGE_TIMEOUT: "7200"' in mac
    assert 'PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES: "4294967296"' in mac
    assert 'PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES: "2147483648"' in mac
    assert 'PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES: "536870912"' in mac


def test_ci_selects_phase_tests_on_mac_with_default_xdist_and_existing_receipts():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pcc1-package-parity.yml").read_text()
    path = "tests/python/test_macho_phase_events.py"
    assert workflow.count('      - "' + path + '"') == 2
    focused = workflow.split("      - name: Verify Windows global addressing and PE relocations\n", 1)[1]
    focused = focused.split("      - name:", 1)[0]
    assert "timeout-minutes: 2" in focused
    assert "${{ matrix.platform == 'macos-arm64' && '" + path + "' || '' }}" in focused
    assert "--junitxml=build/worker-prior-focused-host.xml" in focused
    originals = (
        "test_windows_symbol_immediates.py", "test_windows_global_memory_scratch.py",
        "test_windows_pe_absolute_relocations.py", "test_pe_stackmap_merge.py",
        "test_pe_host_budget.py", "test_pe_image_size_limit.py",
    )
    assert all("tests/python/" + name in focused for name in originals)
    assert not any(flag in focused for flag in ("-n0", "--noconftest", "addopts=", "--dist="))
    assert "build/worker-prior-preflight/**/*.xml" in workflow
    assert "            build/worker-prior-focused-host.xml\n" in workflow


def test_focused_junit_publication_preserves_the_gate_fresh_output_root(tmp_path):
    from _pytest.junitxml import LogXML

    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pcc1-package-parity.yml").read_text()
    preflight = workflow.split("  worker-prior-preflight:\n", 1)[1].split("\n  windows-rss-publication:", 1)[0]
    focused = preflight.split("      - name: Verify Windows global addressing and PE relocations\n", 1)[1]
    focused = focused.split("      - name:", 1)[0]
    junit_path = re.search(r"--junitxml=(\S+)", focused).group(1)
    output_roots = set(re.findall(r"--out-dir (\S+) --phase preflight", preflight))
    assert output_roots == {"build/worker-prior-preflight"}
    out = tmp_path / output_roots.pop()
    destination = tmp_path / junit_path
    assert not destination.is_relative_to(out)
    assert "            " + junit_path + "\n" in preflight

    def publish(path):
        # Exercise pytest's actual directory-creation behavior. A path-only
        # assertion missed this ordering interaction in the original change.
        xml = LogXML(str(path), prefix=None)
        xml.pytest_sessionstart()
        xml.pytest_sessionfinish()
        assert path.is_file()

    publish(destination)
    phase = "preflight"
    # Preserve the gate's existing precondition, including its error text.
    assert not out.exists(), phase + " requires a fresh output root"

    # Reproduce the old placement with the same real publisher: it creates
    # the forbidden root before the gate, so the unchanged precondition fails.
    out = tmp_path / "old-placement" / "build" / "worker-prior-preflight"
    publish(out / "focused-host.xml")
    with pytest.raises(AssertionError, match="preflight requires a fresh output root"):
        assert not out.exists(), phase + " requires a fresh output root"


@pytest.mark.parametrize("consuming", (False, True))
@pytest.mark.parametrize("stackmaps", (False, True))
def test_join_construction_indexes_retire_without_mutating_aliases_or_ledger(
    tmp_path, monkeypatch, consuming, stackmaps,
):
    paths = (_stack_inputs(tmp_path, count=3, records=8, data_bytes=64) if stackmaps
             else _write_symbol_inputs(tmp_path, count=3, symbols=8))
    expected = _prepared_state(macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        _consume_inputs=True, _source_view=True, compact_budget=_budget(50_000_000),
    ))
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: consuming)
    aliases, snapshots, charges, retired = [], [], [], []
    names = ("stack_offsets", "stack_symbols", "symbol_indices", "section_indices")

    def phase(event, **_details):
        before = "stackmap_merge_end" if stackmaps else "merge_index_end"
        if event not in (before, "final_layout_begin"):
            return
        # Inspect only this test's stopped production caller. Do not retain the
        # frame/local mapping across the next ownership-sensitive operation.
        frame = sys._getframe(2)
        assert frame.f_code.co_name == "_merge_paths"
        local = frame.f_locals
        try:
            charges.append(local["ledger"].retained)
            if event == before:
                for name in names:
                    if name in local:
                        value = local[name]
                        aliases.append(value)
                        snapshots.append(dict(value) if isinstance(value, dict) else tuple(value))
            else:
                retired.append(all(local[name] is None for name in names))
        finally:
            del frame, local

    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget, _phase_callback=phase)
    assert retired == [True] and len(charges) == 2 and charges[0] == charges[1]
    assert all(alias == snapshot for alias, snapshot in zip(aliases, snapshots))
    assert _prepared_state(prepared) == expected
    assert budget.retained_bytes == 137


@pytest.mark.parametrize("consuming", (False, True))
def test_section_join_events_report_existing_scalar_sizes_and_selected_branches(
    tmp_path, monkeypatch, consuming,
):
    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: consuming)
    stream, callback = _sink(monkeypatch)
    prepared = host.prepare_executable_from_paths(
        paths, compact_budget=_budget(50_000_000), _phase_callback=callback,
    )
    state = _prepared_state(prepared)
    selected = [row for row in _rows(stream) if "section_index" in row]
    assert len(selected) == 4 * len(state[0])
    expected_fields = {"schema", "pid", "sequence", "monotonic_ns", "event", "section_index",
                       "segment", "section", "size_bytes", "parts", "consuming"}
    for index, section in enumerate(state[0]):
        rows = selected[index * 4:(index + 1) * 4]
        assert [row["event"] for row in rows] == [
            "section_rows_begin", "section_rows_end", "section_payload_begin", "section_payload_end",
        ]
        for row in rows:
            assert set(row) == expected_fields
            assert row["section_index"] == index
            assert (row["segment"], row["section"]) == (section["segname_str"], section["sectname_str"])
            assert all(type(value) in (str, int, bool) for value in row.values())
        assert rows[0]["size_bytes"] == rows[1]["size_bytes"] == len(state[3][index])
        assert rows[2]["size_bytes"] == rows[3]["size_bytes"] == len(state[2][index])
        assert rows[0]["parts"] == rows[1]["parts"]
        assert rows[2]["parts"] == rows[3]["parts"]
        assert rows[0]["consuming"] is (consuming and rows[0]["parts"] > 1)
        assert rows[2]["consuming"] is (consuming and rows[2]["parts"] > 1)
    assert all(type(cell.cell_contents) in (int, bool) for cell in callback.__closure__)
    assert stream.writes == stream.flushes == len(_rows(stream))


@pytest.mark.parametrize("point", ("section_rows_begin", "section_rows_end", "section_payload_begin"))
@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_section_join_callbacks_cannot_open_an_instrumented_consuming_window(
    tmp_path, monkeypatch, point, instrument,
):
    from pcc.backend import macho_host_join as join, macho_link

    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    original = host._consume_parts
    calls = []

    def consume(parts, ledger, **kwargs):
        calls.append((join._join_instrumentation_absent(), len(parts)))
        return original(parts, ledger, **kwargs)

    monkeypatch.setattr(host, "_consume_parts", consume)
    previous_trace, previous_profile = sys.gettrace(), sys.getprofile()
    reached = []

    def hook(_frame, _event, _arg):
        return hook

    def phase(event, **details):
        # __data has multi-part rows and payload; changing a hook after the
        # earlier boolean selection must still hit the helper's refusal.
        if event == point and details.get("section") == "__data":
            reached.append(event)
            getattr(sys, "set" + instrument)(hook)

    try:
        with pytest.raises(macho_link.LinkError, match="uninstrumented host"):
            host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                               _phase_callback=phase)
    finally:
        sys.settrace(previous_trace)
        sys.setprofile(previous_profile)
    assert reached == [point] and calls[-1] == (False, 3)


def test_section_event_adapter_preserves_legacy_sink_and_original_exception():
    old_events = []
    events.emit_section_phase(old_events.append, "section_rows_begin", 0,
                              ("__TEXT", "__text"), 16, 2, True)
    events.emit_phase(old_events.append, "section_joins_end")
    assert old_events == ["section_joins_end"]

    def failed(*_args, **_kwargs):
        raise MemoryError("section diagnostic failed")

    original = ValueError("original join failure")
    with pytest.raises(ValueError) as caught:
        try:
            raise original
        finally:
            events.emit_section_phase(failed, "section_rows_end", 0,
                                      ("__TEXT", "__text"), 16, 2, True)
    assert caught.value is original


@pytest.mark.parametrize("point", ("section_rows_begin", "section_payload_begin"))
def test_join_failure_keeps_its_scalar_begin_without_a_false_completion(tmp_path, monkeypatch, point):
    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    stream, callback = _sink(monkeypatch)
    consume = host._consume_parts
    original = ValueError("original section join failure")

    def failed(parts, ledger, **kwargs):
        last = _rows(stream)[-1]
        if last["event"] == point and last["section"] == "__data":
            raise original
        return consume(parts, ledger, **kwargs)

    monkeypatch.setattr(host, "_consume_parts", failed)
    with pytest.raises(ValueError) as caught:
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                           _phase_callback=callback)
    assert caught.value is original
    rows = _rows(stream)
    assert rows[-1]["event"] == point and rows[-1]["section"] == "__data"
    assert rows[-1]["consuming"] is True
    assert "section_joins_end" not in [row["event"] for row in rows]


def test_section_diagnostics_never_run_inside_a_mutable_join_export(tmp_path, monkeypatch):
    from pcc.backend import macho_host_join as join
    from tests.python.test_macho_consuming_join import _track_buffers

    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    owners = _track_buffers(monkeypatch)
    stream, callback = _sink(monkeypatch)
    joined = []
    original = host._consume_parts

    def consume(parts, ledger, **kwargs):
        result = original(parts, ledger, **kwargs)
        joined.append(len(result))
        return result

    def phase(event, **details):
        if event.startswith(("section_rows_", "section_payload_")):
            assert all(owner.closed for owner in owners)
        callback(event, **details)

    monkeypatch.setattr(host, "_consume_parts", consume)
    host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                       _phase_callback=phase)
    assert joined and all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert len([row for row in _rows(stream) if "section_index" in row]) == 12
    assert join._join_instrumentation_absent()


def test_single_part_section_target_reports_consuming_patch_branch(tmp_path, monkeypatch):
    from tests.python.test_macho_consuming_join import _section_targets
    from tests.python.test_macho_windowed_parity import _write_pcos

    paths = _write_pcos(tmp_path, _section_targets())
    stream, callback = _sink(monkeypatch)
    host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                       _phase_callback=callback)
    rows = [row for row in _rows(stream)
            if row["event"].startswith("section_payload_") and row["section"] == "__data"]
    assert [row["event"] for row in rows] == ["section_payload_begin", "section_payload_end"]
    assert all(row["parts"] == 1 and row["consuming"] is True for row in rows)


# Keep the bounded-storage regressions with this independent focused suite;
# the legacy source-shape suites deliberately lock their original test counts.
def _staged_row_bytes(count=1, seed=7):
    return native._FINAL_LINK_RELOCATION.pack(seed, seed + 1, 0, 3, 1, 0) * count


def _staged_rows(count=1, budget=None):
    from pcc.backend import macho_host_join as join
    ledger = host._Ledger(budget or _budget(20_000_000))
    store = join._StagedRelocationRows(count, ledger.budget, ledger.keep)
    return store, ledger


def _staged_file(monkeypatch, mode=None):
    from pcc.backend import macho_host_join as join
    original = join.tempfile.TemporaryFile
    files = []

    class File:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
            self.calls = []
            self.mode = mode
            files.append(self)

        def fileno(self):
            return self.inner.fileno()

        def seek(self, *args):
            result = self.inner.seek(*args)
            return result + 1 if self.mode == "seek" else result

        def write(self, value):
            self.calls.append(("write", len(value)))
            if self.mode == "write":
                return 0
            with value[:97] as part:
                return self.inner.write(part)

        def readinto(self, value):
            self.calls.append(("read", len(value)))
            if self.mode == "read":
                return 0
            if self.mode == "read-close":
                child = value[:]
                raise OSError("original row read failure")
            with value[:83] as part:
                return self.inner.readinto(part)

        def close(self):
            self.inner.close()
            if self.mode in ("close", "read-close"):
                raise OSError("secondary row close failure")

    monkeypatch.setattr(join.tempfile, "TemporaryFile", lambda **_kwargs: File())
    return files


@pytest.mark.parametrize("counts", ((), (0,), (1,), (6000, 2, 8000)))
def test_staged_rows_exact_bytes_one_backing_and_bounded_io(monkeypatch, counts):
    from pcc.backend import macho_host_join as join
    from tests.python.test_macho_consuming_join import _track_buffers
    files = _staged_file(monkeypatch)
    owners = _track_buffers(monkeypatch)
    store, ledger = _staged_rows(len(counts))
    metadata = store.metadata_bytes
    charges = []
    for count in counts:
        charges.append(store.write_owned(_staged_row_bytes(count)))
    assert store.seal() is store
    assert store.logical_bytes == sum(counts) * native._FINAL_LINK_RELOCATION.size
    assert store.max_window_bytes == max(charges, default=0)
    assert ledger.retained == metadata
    for index, count in enumerate(counts):
        checks = []
        def check():
            assert store.active_bytes == 0
            assert all(owner.closed for owner in owners)
            checks.append(index)
            ledger.check(store.max_window_bytes)
        with store.window(index, check=check) as raw:
            assert type(raw) is bytes and raw == _staged_row_bytes(count)
            assert store.size(index) == len(raw)
            assert store.row_count(index) == count
            assert store.active_bytes == store.backing_bytes(index) == charges[index]
            raw = None
        assert checks == [index] and store.active_bytes == 0
    assert len(owners) == len(counts)
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert max((size for file in files for _, size in file.calls), default=0) <= join._BLOCK
    store.close()
    store.close()
    assert files[0].inner.closed and store.stream is None and store.rows == []
    assert store._replays == [] and store.public_copy_bytes == 0


def test_staged_rows_reject_input_aliases_without_retaining_payload():
    store, _ledger = _staged_rows()
    raw = _staged_row_bytes(10)
    with pytest.raises(maps.PreciseStackMapError, match="exclusively owned") as caught:
        store.write_owned(raw)
    assert store.stream is None and store.rows == []
    trace = caught.value.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_name == "write_owned":
            assert trace.tb_frame.f_locals["payload"] is None
        trace = trace.tb_next


@pytest.mark.parametrize("alias_kind", ("bytes", "view", "iterator"))
def test_staged_rows_reject_escaped_window_aliases(alias_kind):
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(10))
    store.seal()
    aliases = []
    with pytest.raises(maps.PreciseStackMapError, match="escaped aliases"):
        with store.window(0, check=lambda: None) as raw:
            aliases.append(raw if alias_kind == "bytes" else (
                memoryview(raw) if alias_kind == "view" else native._FINAL_LINK_RELOCATION.iter_unpack(raw)
            ))
            raw = None
    assert store.stream is None and store.active_bytes > 0
    aliases.clear()
    store.close()
    assert store.active_bytes > 0  # external lifetimes cannot earn retirement credit


def test_staged_rows_clear_generators_before_exclusive_retirement():
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(10))
    store.seal()
    with store.window(0, check=lambda: None) as raw:
        rows = native._FINAL_LINK_RELOCATION.iter_unpack(raw)
        assert next(rows)[0] == 7
        rows = raw = None
    assert store.active_bytes == 0
    store.close()


def test_staged_rows_concurrent_and_reentrant_windows_are_rejected():
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes())
    store.seal()
    def check():
        with pytest.raises(maps.PreciseStackMapError, match="already active"):
            with store.window(0, check=lambda: None):
                pytest.fail("reentrant window was admitted")
    with store.window(0, check=check) as raw:
        with pytest.raises(maps.PreciseStackMapError, match="already active"):
            with store.window(0, check=lambda: None):
                pytest.fail("concurrent window was admitted")
        with pytest.raises(maps.PreciseStackMapError, match="already active"):
            store.copy(0, check=lambda: None)
        raw = None
    assert store.active_bytes == 0
    store.close()


@pytest.mark.parametrize("failure", ("read", "seek", "extent", "offset", "hash"))
def test_staged_rows_fail_closed_on_corrupt_storage(monkeypatch, failure):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    if failure in ("read", "seek"):
        files[0].mode = failure
    elif failure == "extent":
        files[0].inner.truncate(store.logical_bytes - 1)
    elif failure == "offset":
        _offset, size, count, digest = store.rows[0]
        store.rows[0] = (1, size, count, digest)
    else:
        files[0].inner.seek(0)
        files[0].inner.write(b"x")
    with pytest.raises(maps.PreciseStackMapError):
        with store.window(0, check=lambda: None):
            pytest.fail("corrupt rows were published")
    assert files[0].inner.closed and store.stream is None and store.rows == []


@pytest.mark.parametrize("failure", ("write", "close", "read-close"))
def test_staged_rows_fail_closed_without_masking_original_io_error(monkeypatch, failure):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    if failure == "write":
        files[0].mode = "write"
        with pytest.raises(maps.PreciseStackMapError, match="short .* write"):
            store.write_owned(_staged_row_bytes(30))
    else:
        store.write_owned(_staged_row_bytes(30))
        store.seal()
        files[0].mode = failure
        if failure == "close":
            with pytest.raises(maps.PreciseStackMapError, match="secondary row close"):
                store.close()
        else:
            with pytest.raises(maps.PreciseStackMapError, match="original row read"):
                with store.window(0, check=lambda: None):
                    pytest.fail("failed read was published")
    assert files[0].inner.closed and store.stream is None and store.rows == []


def test_staged_rows_callback_budget_is_checked_before_allocating(monkeypatch):
    from tests.python.test_macho_consuming_join import _track_buffers
    owners = _track_buffers(monkeypatch)
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    original = maps.PreciseStackMapError("caller row window budget exceeded")
    def check():
        raise original
    with pytest.raises(maps.PreciseStackMapError) as caught:
        with store.window(0, check=check):
            pytest.fail("over-budget window was published")
    assert caught.value is original and not owners and store.active_bytes == 0
    assert store.stream is None


def test_staged_rows_descriptor_admission_precedes_file_allocation(monkeypatch):
    files = _staged_file(monkeypatch)
    from pcc.backend import macho_host_join as join
    def admit(_amount, **_kwargs):
        raise maps.PreciseStackMapError("descriptor caller budget exceeded")
    with pytest.raises(maps.PreciseStackMapError, match="descriptor caller budget"):
        join._StagedRelocationRows(1, _budget(20_000_000), admit)
    assert not files


def test_staged_rows_public_copy_is_charged_without_consuming_replays():
    store, ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    backing = store.backing_bytes(0)
    admissions = []
    def check():
        admissions.append(store.public_copy_bytes)
        ledger.check(store.public_copy_bytes + backing)
    first = store.copy(0, check=check)
    second = store.copy(0, check=check)
    assert first == second == _staged_row_bytes(30) and first is not second
    assert admissions == [0, backing]
    assert store.public_copy_bytes == 2 * backing and store.active_bytes == 0
    assert store._replays == [0]
    for _ in range(8):
        with store.window(0, check=lambda: None) as raw:
            assert raw == first
            raw = None
    with pytest.raises(maps.PreciseStackMapError, match="replay budget"):
        with store.window(0, check=lambda: None):
            pytest.fail("unbounded replay was admitted")
    assert store.stream is None and store.public_copy_bytes == 2 * backing


def test_staged_rows_original_compiler_error_survives_alias_and_close_failure(monkeypatch):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    original = ValueError("original compiler error")
    aliases = []
    with pytest.raises(ValueError) as caught:
        with store.window(0, check=lambda: None) as raw:
            aliases.append(raw)
            raw = None
            files[0].mode = "close"
            raise original
    assert caught.value is original and files[0].inner.closed
    assert store.stream is None and store.rows == [] and store.active_bytes > 0
    aliases.clear()



def test_staged_rows_escaped_export_refuses_freeze_without_copy(monkeypatch):
    from pcc.backend import macho_host_join as join
    original = join.io.BytesIO
    owners, exports = [], []
    class Escaping(original):
        def __init__(self, value):
            super().__init__(value)
            owners.append(self)
        def getbuffer(self):
            view = super().getbuffer()
            exports.append(view[:])
            return view
        def getvalue(self):
            pytest.fail("escaped export reached copying freeze")
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    monkeypatch.setattr(join.io, "BytesIO", Escaping)
    try:
        with pytest.raises(BufferError):
            with store.window(0, check=lambda: None):
                pytest.fail("escaped export was published")
        assert store.stream is None and store.rows == [] and store.active_bytes > 0
    finally:
        for export in exports:
            export.release()
        exports.clear()
        for owner in owners:
            owner.close()


@pytest.mark.parametrize("failure", ("missing", "extra", "unaligned", "closed"))
def test_staged_rows_seal_write_and_close_state_are_fail_closed(failure):
    store, _ledger = _staged_rows()
    if failure == "missing":
        action = store.seal
    elif failure == "extra":
        store.write_owned(_staged_row_bytes())
        action = lambda: store.write_owned(_staged_row_bytes())
    elif failure == "unaligned":
        action = lambda: store.write_owned(bytes(bytearray(b"invalid")))
    else:
        store.write_owned(_staged_row_bytes())
        store.seal()
        store.close()
        def action():
            with store.window(0, check=lambda: None):
                pytest.fail("closed storage was read")
    with pytest.raises(maps.PreciseStackMapError):
        action()
    assert store.stream is None and store.rows == []
    store.close()


def test_staged_rows_public_copy_keeps_prior_charge_on_refused_budget():
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    copy = store.copy(0, check=lambda: None)
    prior = store.public_copy_bytes
    original = maps.PreciseStackMapError("public copy caller budget exceeded")
    def check():
        assert store.public_copy_bytes == prior
        raise original
    with pytest.raises(maps.PreciseStackMapError) as caught:
        store.copy(0, check=check)
    assert caught.value is original
    assert copy == _staged_row_bytes(30) and store.public_copy_bytes == prior
    assert store.active_bytes == 0 and store.stream is None


def test_staged_rows_check_cannot_enable_instrumentation_before_allocation(monkeypatch):
    from pcc.backend import macho_host_join as join
    from tests.python.test_macho_consuming_join import _track_buffers
    owners = _track_buffers(monkeypatch)
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(30))
    store.seal()
    def check():
        monkeypatch.setattr(join, "_join_instrumentation_absent", lambda: False)
    with pytest.raises(maps.PreciseStackMapError, match="uninstrumented host"):
        with store.window(0, check=check):
            pytest.fail("instrumented storage was read")
    assert not owners and store.stream is None and store.active_bytes == 0



def test_staged_rows_refusal_tracebacks_do_not_keep_admission_closures():
    from pcc.backend import macho_host_join as join
    with pytest.raises(maps.PreciseStackMapError) as invalid:
        join._StagedRelocationRows(-1, _budget(20_000_000), lambda *_args, **_kwargs: None)
    store, _ledger = _staged_rows()
    store.write_owned(_staged_row_bytes())
    store.seal()
    errors = [invalid.value]
    with store.window(0, check=lambda: None) as raw:
        with pytest.raises(maps.PreciseStackMapError) as window_error:
            with store.window(0, check=lambda: None):
                pytest.fail("reentrant window was admitted")
        with pytest.raises(maps.PreciseStackMapError) as copy_error:
            store.copy(0, check=lambda: None)
        errors.extend((window_error.value, copy_error.value))
        raw = None
    for error in errors:
        trace = error.__traceback__
        while trace:
            if trace.tb_frame.f_code.co_filename == join.__file__:
                local = trace.tb_frame.f_locals
                assert local.get("check") is None and local.get("admit") is None
            trace = trace.tb_next
    store.close()


# Private row-store integration lives in this already selected Mac-focused
# suite. The ordinary bytes API remains the independent format oracle.
def _capture_row_stores(monkeypatch):
    stores = []
    original = host._StagedRelocationRows.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        stores.append(self)
    monkeypatch.setattr(host._StagedRelocationRows, "__init__", initialize)
    return stores


def test_staged_source_public_accessors_preserve_rows_without_caching(tmp_path):
    from tests.python.test_macho_windowed_parity import _companion_inputs, _write_pcos
    paths = _write_pcos(tmp_path, _companion_inputs())
    expected = host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000))
    actual = host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000), _staged_rows=True)
    store = actual._host_row_store
    try:
        assert _prepared_state(actual) == _prepared_state(expected)
        assert actual._raw_relocations == [b""] * len(actual.sections())
        assert store.active_bytes == 0 and store.public_copy_bytes > store.logical_bytes
        assert all(count <= 4 for count in store._replays)
        assert not hasattr(store, "__iter__")
    finally:
        host.close_staged_source_rows(actual)
    assert store.stream is None
    with pytest.raises(native.NativeObjectError, match="does not belong"):
        actual.compact_relocation_rows(actual.sections()[0])


@pytest.mark.parametrize("fixture", ("companions", "stackmaps"))
def test_staged_source_owned_output_matches_bytes_and_signature(tmp_path, monkeypatch, fixture):
    from tests.python.test_macho_windowed_parity import _companion_inputs, _write_pcos
    paths = (_write_pcos(tmp_path, _companion_inputs()) if fixture == "companions"
             else _stack_inputs(tmp_path, count=3, records=32, data_bytes=64))
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
        compact_budget=_budget(50_000_000),
    )
    stores = _capture_row_stores(monkeypatch)
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    source = weakref.ref(pending[0])
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected and source() is None
    signature = codesign.parse_signature(actual)
    assert codesign.build_signature(memoryview(actual)[:signature.dataoff], identifier=b"pcc-linked",
                                    exec_seg_base=signature.exec_seg_base, exec_seg_limit=signature.exec_seg_limit,
                                    exec_seg_flags=signature.exec_seg_flags) == actual[signature.dataoff:]
    assert budget.retained_bytes == 137
    assert len(stores) == 1 and stores[0].stream is None and stores[0].active_bytes == 0


@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_staged_source_initial_instrumentation_keeps_ordinary_fallback(tmp_path, monkeypatch, instrument):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget", lambda: _budget(50_000_000))
    _pipeline(paths, tmp_path / "expected")
    stores = _capture_row_stores(monkeypatch)
    setter = sys.settrace if instrument == "trace" else sys.setprofile
    previous = sys.gettrace() if instrument == "trace" else sys.getprofile()
    def hook(_frame, _event, _arg):
        return hook
    try:
        setter(hook)
        ordinary = host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000))
        assert not hasattr(ordinary, "_host_row_store")
        with pytest.raises(output.LinkError, match="uninstrumented"):
            host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000), _staged_rows=True)
        _pipeline(paths, tmp_path / "instrumented")
    finally:
        setter(previous)
    assert stores == []
    assert (tmp_path / "instrumented").read_bytes() == (tmp_path / "expected").read_bytes()


@pytest.mark.parametrize("instrument", ("trace", "profile"))
@pytest.mark.parametrize("event", ("section_payload_end", "source_view_begin", "source_view_end", "relocations_begin"))
def test_staged_source_midflight_instrumentation_closes_without_fallback(tmp_path, monkeypatch, instrument, event):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget", lambda: _budget(50_000_000))
    monkeypatch.setenv("PCC_MACHO_LINK_PHASE_EVENTS", "1")
    stores = _capture_row_stores(monkeypatch)
    setter = sys.settrace if instrument == "trace" else sys.setprofile
    previous = sys.gettrace() if instrument == "trace" else sys.getprofile()
    def hook(_frame, _event, _arg):
        return hook
    def phase(name, **_fields):
        if name == event:
            setter(hook)
    monkeypatch.setattr(events, "phase_callback_from_environment", lambda: phase)
    try:
        with pytest.raises(pipeline.SelfBackendLinkError, match="uninstrumented|instrumentation|eligibility changed"):
            _pipeline(paths, tmp_path / "rejected")
    finally:
        setter(previous)
    assert not (tmp_path / "rejected").exists()
    assert stores and all(store.stream is None for store in stores)


@pytest.mark.parametrize("failure", ("source_validation", "missing_entry", "public_copy", "reference_link"))
def test_staged_source_failure_closes_private_owner(tmp_path, monkeypatch, failure):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    stores = _capture_row_stores(monkeypatch)
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    if failure == "source_validation":
        original = ValueError("original source validator failure")
        def fail(*_args, **_kwargs):
            raise original
        monkeypatch.setattr(native, "_validate_section", fail)
        with pytest.raises(ValueError) as caught:
            host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
        assert caught.value is original
    else:
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
        if failure == "public_copy":
            copy = pending[0].compact_relocation_rows(pending[0].sections()[0])
            assert type(copy) is bytes
        with pytest.raises(output.LinkError):
            if failure == "reference_link":
                macho_exec.link_prepared_executable(pending.pop(), compact_budget=budget)
            else:
                output.link_owned_executable(pending.pop(), compact_budget=budget,
                                             entry="_missing" if failure == "missing_entry" else "_main")
    assert stores and all(store.stream is None for store in stores)
    assert budget.retained_bytes == 137


def test_staged_source_keeps_duplicate_offset_validation_and_closes(tmp_path, monkeypatch):
    from pcc.backend.macho_obj import Section, TextSymbol
    from pcc.backend import macho_spec as spec
    stores = _capture_row_stores(monkeypatch)
    budget = _budget(50_000_000)
    store = host._StagedRelocationRows(1, budget, lambda *_args, **_kwargs: None)
    store.write_owned(native._FINAL_LINK_RELOCATION.pack(0, 0, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED) * 2)
    store.seal()
    section = Section("__data", "__DATA", data=b"\0" * 16,
                      symbols=(TextSymbol("_main", 8),), align_log2=3)
    with pytest.raises(native.NativeObjectError, match="multiple relocation"):
        native.OwnedMergedSourceView([section], compact_budget=budget, _row_storage=store)
    assert store.stream is None


def test_staged_source_host_helpers_stay_out_of_native_import_closure():
    from pcc.frontends.python.pipeline_import_scan import _without_inactive_implementation_imports
    for module in (native, pipeline, macho_exec):
        source = Path(module.__file__).read_text()
        stripped = _without_inactive_implementation_imports(source, implementation="pcc")
        assert "from .macho_host_prepare import copy_staged_relocation_rows" not in stripped
        assert "from .macho_host_prepare import copy_staged_section_data" not in stripped
        assert "from pcc.backend.macho_host_prepare import close_staged_source_rows" not in stripped
        assert "_host_payload_store" not in stripped
        assert "_owned_output.retire_payloads" not in stripped


@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_staged_rows_window_retirement_rechecks_instrumentation(monkeypatch, instrument):
    from pcc.backend import macho_host_join as join
    store, ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(2))
    store.seal()
    with pytest.raises(maps.PreciseStackMapError, match="instrumentation changed"):
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as rows:
            monkeypatch.setattr(join.sys, "get" + instrument, lambda: object())
            rows = None
    assert store.stream is None and store.active_bytes > 0



def test_staged_rows_outer_handled_exception_does_not_hide_alias_refusal():
    store, ledger = _staged_rows()
    store.write_owned(_staged_row_bytes(2))
    store.seal()
    escaped = []
    try:
        raise ValueError("already handled")
    except ValueError:
        with pytest.raises(maps.PreciseStackMapError, match="escaped aliases"):
            with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as rows:
                escaped.append(rows)
                rows = None
    assert store.stream is None and store.active_bytes > 0
    assert escaped[0] == _staged_row_bytes(2)


def test_staged_owned_link_inside_handled_exception_preserves_image(tmp_path):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
        compact_budget=_budget(50_000_000),
    )
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    try:
        raise ValueError("already handled")
    except ValueError:
        actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected


class _FailingRowClose:
    def __init__(self, stream):
        self.stream = stream
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def close(self):
        self.stream.close()
        raise OSError("secondary row close failure")


def test_staged_rows_normal_close_inside_handled_exception_is_not_suppressed():
    store, _ledger = _staged_rows()
    store.stream = _FailingRowClose(store.stream)
    try:
        raise ValueError("already handled")
    except ValueError:
        with pytest.raises(maps.PreciseStackMapError, match="secondary row close failure"):
            store.close()
    assert store.stream is None


@pytest.mark.parametrize("primary_failure", (False, True))
def test_staged_owned_close_distinguishes_current_error_from_handled_outer_error(tmp_path, monkeypatch, primary_failure):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    store = pending[0]._host_row_store
    store.stream = _FailingRowClose(store.stream)
    original = ValueError("current relocation failure")
    if primary_failure:
        def fail(*_args, **_kwargs):
            raise original
        monkeypatch.setattr(output._OwnedOutput, "reserve_fixup", fail)
    try:
        raise ValueError("already handled")
    except ValueError:
        with pytest.raises(ValueError if primary_failure else maps.PreciseStackMapError) as caught:
            output.link_owned_executable(pending.pop(), compact_budget=budget)
        if primary_failure:
            assert caught.value is original
        else:
            assert isinstance(caught.value, maps.PreciseStackMapError)
            assert "secondary row close failure" in str(caught.value)
    assert store.stream is None


def test_staged_owned_iterator_close_does_not_mask_apply_failure(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    store = pending[0]._host_row_store
    indices = native.OwnedMergedSourceView._iter_section_relocation_indices
    class BadClose:
        def __init__(self, generator):
            self.generator = generator
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.generator)
        def close(self):
            self.generator.close()
            raise OSError("secondary iterator close failure")
    def wrapped(self, rows, ordered):
        generator = indices(self, rows, ordered)
        return BadClose(generator) if ordered and rows else generator
    original = ValueError("current relocation failure")
    def fail(*_args, **_kwargs):
        raise original
    monkeypatch.setattr(native.OwnedMergedSourceView, "_iter_section_relocation_indices", wrapped)
    monkeypatch.setattr(output._OwnedOutput, "reserve_fixup", fail)
    with pytest.raises(ValueError) as caught:
        output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert caught.value is original and store.stream is None


@pytest.mark.parametrize("handler_offset", (16, 24))
def test_staged_unwind_early_return_closes_window_inside_handled_exception(tmp_path, handler_offset):
    from pcc.backend import macho_obj as obj
    from tests.python.test_macho_link_reports import _unwind
    from tests.python.test_macho_windowed_parity import _write_pcos
    value = native.NativeObject.from_sections([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 2,
                    flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                    symbols=(obj.TextSymbol("_main", 0), obj.TextSymbol("_handler", 4))),
        _unwind(0, handler_offset),
    ])
    paths = _write_pcos(tmp_path, [value])
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
        compact_budget=_budget(50_000_000),
    )
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    store = pending[0]._host_row_store
    try:
        raise ValueError("already handled")
    except ValueError:
        actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected and store.stream is None and store.active_bytes == 0


def test_staged_native_ir_prunes_window_cleanup_and_exception_adapters():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    source = '''def run(owner):
    rows = relocations = window = error = None
    try:
        import sys as execution_runtime
        if execution_runtime.implementation.name == "cpython":
            if owner is not None:
                from unavailable_row_store import open_window
                window = open_window(owner)
                rows = window.__enter__()
        relocations = [1, 2, 3]
        for index in relocations:
            if index == 2:
                return index
        return 0
    except BaseException:
        import sys as execution_runtime
        if execution_runtime.implementation.name == "cpython":
            if window is not None:
                error = execution_runtime.exc_info()
        raise
    finally:
        import sys as execution_runtime
        if execution_runtime.implementation.name == "cpython":
            if window is not None:
                try:
                    relocations.close()
                finally:
                    rows = relocations = None
                    if error is None:
                        window.__exit__(None, None, None)
                    else:
                        window.__exit__(*error)
                    window = error = None
'''
    module = infer_module(parse_and_lift(source, "row_boundary.py", "row_boundary"))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._strict_no_libpython = True
    result = str(generator.generate())
    assert "unavailable_row_store" not in result
    # Unused alloca debug names may survive pruning; no host call may.
    assert not re.search(r"\bcall[^\n]*open_window", result)
    definition = re.search(r"^define[^\n]*@user_row_boundary_run\([^\n]*\) \{", result, re.MULTILINE)
    assert definition is not None
    body = result[definition.start():].split("\n}\n", 1)[0]
    assert "@py_cpy_" not in body and "__exit__" not in body and "exc_info" not in body
    assert "@py_list_get" in body


def test_staged_source_budget_counts_resident_window_and_refuses_unadmitted_copy(tmp_path):
    from pcc.backend.macho_host_relocations import _model_bytes
    from tests.python.test_macho_windowed_parity import _companion_inputs, _write_pcos
    paths = _write_pcos(tmp_path, _companion_inputs())
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    source = host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
    store = source._host_row_store
    ordinary_fields = sum(_model_bytes(getattr(source, name), budget) for name in output._SOURCE_FIELDS)
    payloads = source._host_payload_store
    assert output._source_bytes(source, budget) == (
        ordinary_fields + store.metadata_bytes + store.max_window_bytes
        + payloads.metadata_bytes + _model_bytes(source._host_payload_layout, budget)
    )
    assert macho_exec._compact_source_raw_bytes(source) == sum(len(data) for data in source._payloads)
    assert store.logical_bytes > max(store.size(index) for index in range(store.count))
    section = max(source.sections(), key=lambda row: row["nreloc"])
    index = section["_pcc_source_index"]
    budget.limit_bytes = budget.retained_bytes + source._host_row_copy_bound + store.backing_bytes(index) - 1
    try:
        with pytest.raises(maps.PreciseStackMapError):
            source.compact_relocation_rows(section)
        assert store.public_copy_bytes == 0 and store.active_bytes == 0
        assert store.stream is None and budget.retained_bytes == 137
    finally:
        host.close_staged_source_rows(source)


@pytest.mark.parametrize(("boundary", "return_target"), (
    pytest.param("source_view_end", "emit_phase", id="source-end-emit-return"),
    pytest.param("section_joins_end", "_merge_paths", id="joins-end-merge-return"),
    pytest.param("source_view_end", "prepare_executable_from_paths", id="source-end-prepare-return"),
))
def test_staged_prepare_phase_return_hook_error_closes_undelivered_owner(tmp_path, monkeypatch, boundary, return_target):
    paths = _stack_inputs(tmp_path, count=1, records=4, data_bytes=8)
    stores = _capture_row_stores(monkeypatch)
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    class HookFailure(BaseException):
        pass
    original = HookFailure(boundary + " " + return_target + " return hook failed")
    previous = sys.getprofile()
    target = getattr(events if return_target == "emit_phase" else host, return_target).__code__
    def hook(frame, event, _arg):
        if event == "return" and frame.f_code is target:
            sys.setprofile(None)
            raise original
    def phase(event):
        if event == boundary:
            sys.setprofile(hook)
    try:
        with pytest.raises(HookFailure) as caught:
            host.prepare_executable_from_paths(paths, compact_budget=budget,
                                               _staged_rows=True, _phase_callback=phase)
        assert caught.value is original
    finally:
        sys.setprofile(previous)
    # Keep both the original exception and metadata alias live while checking:
    # finalization/destructor timing is not the cleanup contract.
    assert stores and all(store.stream is None for store in stores)
    assert budget.retained_bytes == 137
    trace = original.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_name in ("_merge_paths", "prepare_executable_from_paths"):
            assert trace.tb_frame.f_locals.get("result") is None
            assert trace.tb_frame.f_locals.get("row_storage") is None
        trace = trace.tb_next


@pytest.mark.parametrize("counts", ((), (0,), (1,), (6000, 2, 8000)))
def test_staged_row_parts_preserve_order_without_allocating_a_join(monkeypatch, counts):
    from pcc.backend import macho_host_join as join
    from tests.python.test_macho_consuming_join import _track_buffers
    files = _staged_file(monkeypatch)
    buffers = _track_buffers(monkeypatch)
    store, ledger = _staged_rows()
    parts = [_staged_row_bytes(count, seed=index + 7) for index, count in enumerate(counts)]
    expected = b"".join(_staged_row_bytes(count, seed=index + 7)
                        for index, count in enumerate(counts))
    charge = sum(ledger.immutable(len(part)) for part in parts)
    retired, targets = store.write_owned_parts(parts)
    assert retired == charge and targets is False and parts == []
    assert buffers == [] and store.logical_bytes == len(expected)
    store.seal()
    with store.window(0, check=lambda: None) as raw:
        assert raw == expected
        raw = None
    assert len(buffers) == 1 and buffers[0].closed
    assert all(len(set(buffer.addresses)) <= 1 for buffer in buffers)
    assert max((size for file in files for _, size in file.calls), default=0) <= join._BLOCK
    store.close()
    assert files[0].inner.closed


@pytest.mark.parametrize("target", (False, True))
def test_staged_row_parts_classify_section_targets_but_not_addend_companions(target):
    from pcc.backend import macho_spec as spec
    store, _ledger = _staged_rows()
    parts = [native._FINAL_LINK_RELOCATION.pack(0, 5, 0, 2, 0, spec.ARM64_RELOC_ADDEND),
             native._FINAL_LINK_RELOCATION.pack(0, 0, 1, 2, 1, spec.ARM64_RELOC_PAGE21)]
    if target:
        parts.append(native._FINAL_LINK_RELOCATION.pack(8, 1, 0, 3, 0, spec.ARM64_RELOC_UNSIGNED))
    try:
        assert store.write_owned_parts(parts)[1] is target
        store.seal()
        assert store.row_count(0) == 2 + target
    finally:
        store.close()


@pytest.mark.parametrize("failure", ("alias", "duplicate", "alignment", "mutable"))
def test_staged_row_parts_validate_complete_transfer_before_writing(monkeypatch, failure):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    alias = _staged_row_bytes(3) if failure in ("alias", "duplicate") else None
    parts = [_staged_row_bytes(2)]
    parts.extend([alias, alias] if failure == "duplicate" else [alias] if failure == "alias"
                 else [b"bad"] if failure == "alignment" else [bytearray(12)])
    with pytest.raises(maps.PreciseStackMapError, match="exclusively owned|extent changed") as caught:
        store.write_owned_parts(parts)
    assert parts == [] and store.stream is None and files[0].calls == []
    trace = caught.value.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_name == "write_owned_parts":
            assert all(trace.tb_frame.f_locals[name] is None
                       for name in ("payload", "source", "part", "digest", "parts"))
        trace = trace.tb_next


@pytest.mark.parametrize("failure", ("short", "write-close", "extent"))
def test_staged_row_parts_failed_io_closes_and_preserves_original_error(monkeypatch, failure):
    files = _staged_file(monkeypatch, mode="write" if failure == "short" else
                         "close" if failure == "write-close" else None)
    store, _ledger = _staged_rows()
    original_write = files[0].write
    if failure != "short":
        def fail(value):
            if failure == "write-close":
                child = value[:]
                raise OSError("original row parts write failure")
            original_write(value)
            return len(value)  # falsely claims the complete write
        files[0].write = fail
    with pytest.raises(maps.PreciseStackMapError, match="short|original row parts write|extent changed") as caught:
        store.write_owned_parts([_staged_row_bytes(30), _staged_row_bytes(40)])
    assert "secondary row close" not in str(caught.value)
    assert store.stream is None and files[0].inner.closed


def test_staged_row_parts_midwrite_alias_refuses_retirement(monkeypatch):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    write = files[0].write
    escaped = []
    def escape(value):
        if not escaped:
            escaped.append(value.obj)
        return write(value)
    files[0].write = escape
    parts = [_staged_row_bytes(30), _staged_row_bytes(40)]
    with pytest.raises(maps.PreciseStackMapError, match="exclusively owned"):
        store.write_owned_parts(parts)
    assert escaped and parts == [] and store.stream is None
    assert store.logical_bytes == 0 and files[0].inner.closed


@pytest.mark.parametrize("counts", ((), (0,), (1,)))
def test_staged_row_parts_recheck_instrumentation_after_final_extent(monkeypatch, counts):
    files = _staged_file(monkeypatch)
    store, _ledger = _staged_rows()
    fileno = files[0].fileno
    calls = []
    previous = sys.getprofile()
    def hook(_frame, _event, _arg):
        return hook
    def instrument():
        calls.append(True)
        if len(calls) == 2:
            sys.setprofile(hook)
        return fileno()
    files[0].fileno = instrument
    try:
        with pytest.raises(maps.PreciseStackMapError, match="instrumentation changed"):
            store.write_owned_parts([_staged_row_bytes(count) for count in counts])
    finally:
        sys.setprofile(previous)
    assert store.stream is None and store.logical_bytes == 0 and files[0].inner.closed


@pytest.mark.parametrize("fixture", ("companions", "stackmaps"))
def test_staged_preparation_seals_all_rows_before_first_payload_join(tmp_path, monkeypatch, fixture):
    from tests.python.test_macho_windowed_parity import _companion_inputs, _write_pcos
    paths = (_write_pcos(tmp_path, _companion_inputs()) if fixture == "companions"
             else _stack_inputs(tmp_path, count=3, records=32, data_bytes=64))
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
        compact_budget=_budget(50_000_000),
    )
    stores = _capture_row_stores(monkeypatch)
    original = host._StagedSectionPayloads._read
    joins, phases = [], []
    def consume(self, index, check, patch):
        store, = stores
        assert store.sealed and store.appended == store.count
        # Validation borrows exactly this section's row replay while its one
        # payload is checked; final output obtains a fresh exclusive backing.
        assert store.active_bytes in (0, store.backing_bytes(index))
        joins.append(index)
        return original(self, index, check, patch)
    def phase(event, **fields):
        phases.append((event, fields))
    monkeypatch.setattr(host._StagedSectionPayloads, "_read", consume)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget,
                                                 _staged_rows=True, _phase_callback=phase)]
    assert joins
    last_row = max(index for index, (event, _) in enumerate(phases) if event == "section_rows_end")
    first_payload = next(index for index, (event, _) in enumerate(phases) if event == "section_payload_begin")
    assert last_row < first_payload
    row_events = [fields for event, fields in phases if event == "section_rows_end"]
    assert [fields["section_index"] for fields in row_events] == list(range(stores[0].count))
    assert all(fields["consuming"] is True for fields in row_events)
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected
    assert stores[0].stream is None


def test_staged_preparation_replays_targets_after_stackmap_resize(tmp_path, monkeypatch):
    from pcc.backend import macho_spec as spec
    from tests.python.test_macho_consuming_join import _section_targets
    from tests.python.test_macho_windowed_parity import _write_pcos
    paths = _write_pcos(tmp_path, _section_targets())
    expected = _prepared_state(host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)))
    stores = _capture_row_stores(monkeypatch)
    read = host._StagedRelocationRows._read
    early_reads, joined = [], []
    def replay(self, index, check):
        if not joined:
            early_reads.append(index)
        return read(self, index, check)
    def phase(event, **_fields):
        if event == "section_joins_end":
            joined.append(True)
    monkeypatch.setattr(host._StagedRelocationRows, "_read", replay)
    source = host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                                _staged_rows=True, _phase_callback=phase)
    try:
        assert _prepared_state(source) == expected
        targets = [index for index, rows in enumerate(expected[3])
                   if any(not row[4] and row[5] != spec.ARM64_RELOC_ADDEND
                          for row in native._FINAL_LINK_RELOCATION.iter_unpack(rows))]
        assert targets and early_reads == []
        assert stores[0].active_bytes == 0
    finally:
        host.close_staged_source_rows(source)


@pytest.mark.parametrize("close_failure", (False, True))
def test_staged_preparation_partial_spool_failure_never_starts_payloads(tmp_path, monkeypatch, close_failure):
    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    stores = _capture_row_stores(monkeypatch)
    write = host._StagedRelocationRows.write_owned_parts
    original = OSError("original partial pre-spool failure")
    class FailedClose:
        def __init__(self, stream):
            self.stream = stream
        def close(self):
            self.stream.close()
            raise OSError("secondary pre-spool close failure")
    def fail(self, parts):
        if self.appended:
            if close_failure:
                self.stream = FailedClose(self.stream)
            raise original
        return write(self, parts)
    monkeypatch.setattr(host._StagedRelocationRows, "write_owned_parts", fail)
    monkeypatch.setattr(host, "_consume_parts", lambda *_args, **_kwargs: pytest.fail("payload join after failed spool"))
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    with pytest.raises(OSError) as caught:
        host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
    assert caught.value is original and stores[0].stream is None
    assert budget.retained_bytes == 137


@pytest.mark.parametrize("failure", ("budget", "read", "overflow"))
def test_staged_preparation_target_replay_failure_retires_rows_and_payloads(tmp_path, monkeypatch, failure):
    from tests.python.test_macho_consuming_join import _section_targets, _track_buffers
    from tests.python.test_macho_windowed_parity import _write_pcos
    paths = _write_pcos(tmp_path, _section_targets())
    stores = _capture_row_stores(monkeypatch)
    buffers = _track_buffers(monkeypatch)
    checked = []
    if failure == "budget":
        check = host._Ledger.check
        def refuse(self, extra=0, *, label="Mach-O input window"):
            if label == "Mach-O compact row validation replay":
                assert extra == 0 and stores[0].active_bytes == 0
                checked.append(len(buffers))
                raise maps.PreciseStackMapError("injected target row replay budget refusal")
            return check(self, extra, label=label)
        monkeypatch.setattr(host._Ledger, "check", refuse)
        message = "injected target row replay budget refusal"
    elif failure == "read":
        def fail(*_args, **_kwargs):
            raise OSError("injected target row replay read failure")
        monkeypatch.setattr(host._StagedRelocationRows, "_read", fail)
        message = "injected target row replay read failure"
    else:
        patch = host._patch_section_targets
        def overflow(data, *args):
            data[:8] = b"\xff" * 8
            patch(data, *args)
        monkeypatch.setattr(host, "_patch_section_targets", overflow)
        message = "section-target value does not fit relocation width"
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    expected_error = OSError if failure == "read" else output.LinkError
    with pytest.raises(expected_error, match=message) as caught:
        host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)
    assert stores and stores[0].stream is None and stores[0].active_bytes == 0
    assert all(buffer.closed for buffer in buffers) and budget.retained_bytes == 137
    if failure == "budget":
        assert checked == [len(buffers)]  # no replay backing was allocated
    trace = caught.value.__traceback__
    while trace:
        if trace.tb_frame.f_code.co_name == "_merge_paths":
            assert trace.tb_frame.f_locals["row_storage"] is None
            assert trace.tb_frame.f_locals["row_targets"] is None
        trace = trace.tb_next


@pytest.mark.parametrize("instrument", ("trace", "profile"))
@pytest.mark.parametrize("point", ("section_rows_begin", "section_rows_end"))
def test_staged_preparation_instrumentation_during_prespool_fails_closed(tmp_path, monkeypatch, instrument, point):
    paths = _stack_inputs(tmp_path, count=3, records=8, data_bytes=64)
    stores = _capture_row_stores(monkeypatch)
    setter = sys.settrace if instrument == "trace" else sys.setprofile
    previous = sys.gettrace() if instrument == "trace" else sys.getprofile()
    def hook(_frame, _event, _arg):
        return hook
    def phase(event, **_fields):
        if event == point:
            setter(hook)
    try:
        with pytest.raises(output.LinkError, match="uninstrumented|instrumentation"):
            host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000),
                                               _staged_rows=True, _phase_callback=phase)
    finally:
        setter(previous)
    assert stores and all(store.stream is None for store in stores)
