"""Pure host timing/policy/dispatch tests; no compiler or native invocation."""

import ast
import io
import sys
import types
from pathlib import Path

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import indexed_phase_timing as timing_module
from pcc.backend import target_objects
from pcc.backend.indexed_phase_timing import CoarsePhaseTiming, ModulePhaseTiming


TARGETS = (
    "arm64-apple-darwin23.6.0",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
    "x86_64-pc-windows-msvc",
)


class Probe:
    def __init__(self):
        self.ids = []
        self.starts = 0
        self.events = []

    def start(self):
        self.starts += 1
        return self.starts

    def add(self, phase, started):
        assert started > 0
        self.ids.append(phase)

    def begin_assembly(self, phase):
        token = self.start()
        self.events.append(("begin", phase, token))
        return token

    def complete_assembly(self, phase, token):
        self.events.append(("complete", phase, token))

    def begin(self, phase):
        token = self.start()
        self.events.append(("begin", phase, token))
        return token

    def complete(self, phase, token):
        self.events.append(("complete", phase, token))


def test_fixed_nanosecond_aggregates_keep_small_contributions(monkeypatch):
    ticks = iter((100, 107, 200, 211))
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", lambda: next(ticks))
    totals = ModulePhaseTiming("sample")
    totals.add(5, totals.start())
    totals.add(5, totals.start())
    assert totals.totals_ns == [0, 0, 0, 0, 0, 18, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert len(timing_module._PHASE_NAMES) == 16
    assert not totals.failed


@pytest.mark.parametrize("failure", ("start", "end", "backwards", "invalid-phase", "zero"))
def test_clock_failure_is_best_effort_and_disables_later_reads(monkeypatch, failure):
    calls = []

    def clock():
        calls.append(1)
        if failure == "zero":
            return 0
        if failure == "start" or (failure == "end" and len(calls) == 2):
            raise RuntimeError("clock unavailable")
        return 100 if len(calls) == 1 else (99 if failure == "backwards" else 120)

    monkeypatch.setattr(timing_module.time, "perf_counter_ns", clock)
    totals = ModulePhaseTiming("sample")
    started = totals.start()
    totals.add(16 if failure == "invalid-phase" else 0, started)
    assert totals.failed
    before = len(calls)
    assert totals.start() == 0
    totals.add(0, 100)
    assert len(calls) == before
    assert totals.totals_ns == [0] * 16


@pytest.mark.parametrize("complete", (False, True))
def test_report_is_once_bounded_and_marks_inclusive_fields(complete):
    totals = ModulePhaseTiming("sample")
    totals.target = TARGETS[0]
    totals.route = "indexed-native-sections"
    stream = io.StringIO()
    totals.report(stream, complete)
    totals.report(stream, complete)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert "module=sample target=" + TARGETS[0] in lines[0]
    assert "route=indexed-native-sections" in lines[0]
    assert "codegen_complete=" + str(int(complete)) in lines[0]
    assert "clock_ok=1" in lines[0]
    assert "backend_call_inclusive_ns=0" in lines[0]
    assert "function_emit_inclusive_ns=0" in lines[0]
    assert lines[0].count("_ns=") == 16
    assert set(vars(totals)) == {
        "module_name", "target", "route", "totals_ns", "failed", "reported",
    }


def test_reporting_failure_never_replaces_error_and_is_not_retried():
    calls = []

    class BadStream:
        def write(self, text):
            calls.append(text)
            raise OSError("diagnostic stream closed")

    totals = ModulePhaseTiming("sample")
    totals.report(BadStream(), False)
    totals.report(BadStream(), False)
    assert len(calls) == 1


def _stub(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("enabled", (False, True))
def test_indexed_dispatch_keeps_target_options_and_output_identity(monkeypatch, target, enabled):
    output = object()
    received = []
    totals = Probe() if enabled else None
    module = types.SimpleNamespace(triple=target)
    plans = [] if target == TARGETS[2] else None

    def arm(actual, **kwargs):
        received.append((actual, kwargs))
        return output

    def x86(text, **kwargs):
        assert text == ""
        received.append((kwargs.pop("module"), kwargs))
        return output

    _stub(monkeypatch, "pcc.backend.self_backend_aarch64_darwin", emit_aarch64_darwin_indexed_module=arm)
    _stub(monkeypatch, "pcc.backend.self_backend_x86_64_linux", _emit_x86_64_module=x86)
    result = target_objects.emit_indexed_assembly(
        module, optimize=False, stack_map_plans_out=plans, phase_timing=totals,
    )
    assert result is output
    assert len(received) == 1 and received[0][0] is module
    assert received[0][1]["phase_timing"] is totals
    if target in TARGETS[:2]:
        assert received[0][1]["optimize"] is False
    else:
        assert received[0][1]["windows"] == (target == TARGETS[3])
        assert received[0][1]["stack_map_plans_out"] is plans


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("enabled", (False, True))
def test_object_dispatch_preserves_calls_and_exact_return(monkeypatch, target, enabled):
    totals = Probe() if enabled else None
    calls = []
    obj = object()
    payload = b"unchanged-object"

    def assemble(text, *args, **kwargs):
        assert kwargs == ({"phase_timing": totals} if enabled and target in TARGETS[1:3] else {})
        assert text == "assembly"
        calls.append("assemble")
        return obj

    def encode(actual):
        assert actual is obj
        calls.append("encode")
        return payload

    def arm_assemble(text):
        assert text == "assembly"
        calls.append("assemble")
        return obj, ()

    def arm_encode(actual, *, undefined):
        assert undefined == ()
        return encode(actual)

    def coff(text, *, phase_timing=None):
        assert phase_timing is totals
        # The real COFF split is checked separately below.
        result = encode(assemble(text))
        if phase_timing is not None:
            phase_timing.add(9, phase_timing.start())
            phase_timing.add(10, phase_timing.start())
        return result

    _stub(monkeypatch, "pcc.backend.owned_elf_link", assemble=assemble)
    _stub(monkeypatch, "pcc.backend.elf_x86_64", emit_relocatable=encode)
    _stub(monkeypatch, "pcc.backend.arm64_asm_driver", assemble_file=arm_assemble)
    _stub(monkeypatch, "pcc.backend.native_object", encode_native_object_from_sections=arm_encode)
    _stub(monkeypatch, "pcc.backend.coff_x86_64", assemble_object=coff)
    result = target_objects.encode_assembly_object("assembly", target, phase_timing=totals)
    assert result is payload and calls == ["assemble", "encode"]
    if enabled:
        assert totals.ids == [9, 10]


@pytest.mark.parametrize("enabled", (False, True))
def test_packed_stackmap_route_keeps_consumption_and_symbols(monkeypatch, enabled):
    totals = Probe() if enabled else None
    plans = []
    symbol = object()
    label = object()
    assembled = object()
    encoded = b"packed"
    calls = []

    def assemble(text, actual_plans, **kwargs):
        assert text == "assembly" and actual_plans is plans
        expected = {
            "function_symbol": symbol, "block_label": label,
            "consume_stack_map_plans": True,
        }
        if enabled:
            expected["phase_timing"] = totals
        assert kwargs == expected
        calls.append("assemble")
        return assembled

    def encode(actual):
        assert actual is assembled
        calls.append("encode")
        return encoded

    _stub(monkeypatch, "pcc.backend.x86_64_asm_driver", assemble_file_with_stack_maps=assemble)
    _stub(monkeypatch, "pcc.backend.self_backend_x86_64_linux", _asm_symbol=symbol, _block_label=label)
    _stub(monkeypatch, "pcc.backend.elf_x86_64", emit_relocatable=encode)
    result = target_objects.encode_assembly_object(
        "assembly", TARGETS[2], stack_map_plans=plans,
        consume_stack_map_plans=True, phase_timing=totals,
    )
    assert result is encoded and calls == ["assemble", "encode"]
    if enabled:
        assert totals.ids == [9, 10]


@pytest.mark.parametrize("failure", ("assemble", "encode"))
@pytest.mark.parametrize("enabled", (False, True))
def test_object_error_identity_and_order_are_preserved(monkeypatch, failure, enabled):
    totals = Probe() if enabled else None
    error = ValueError("original object failure")
    calls = []
    obj = object()

    def assemble(text, target, **kwargs):
        assert kwargs == ({"phase_timing": totals} if enabled else {})
        calls.append("assemble")
        if failure == "assemble":
            raise error
        return obj

    def encode(actual):
        assert actual is obj
        calls.append("encode")
        raise error

    _stub(monkeypatch, "pcc.backend.owned_elf_link", assemble=assemble)
    _stub(monkeypatch, "pcc.backend.elf_x86_64", emit_relocatable=encode)
    with pytest.raises(ValueError) as caught:
        target_objects.encode_assembly_object("assembly", TARGETS[2], phase_timing=totals)
    assert caught.value is error
    assert calls == (["assemble"] if failure == "assemble" else ["assemble", "encode"])
    if enabled:
        assert totals.ids == ([] if failure == "assemble" else [9])


@pytest.mark.parametrize("enabled", (False, True))
def test_coff_boundary_is_ordinary_assemble_then_emit(monkeypatch, enabled):
    from pcc.backend import coff_x86_64

    totals = Probe() if enabled else None
    obj = object()
    payload = b"COFF"
    calls = []

    def assemble(text, **kwargs):
        assert kwargs == ({"phase_timing": totals} if enabled else {})
        assert text == "text"
        calls.append("assemble")
        return obj

    def emit(actual):
        assert actual is obj
        calls.append("emit")
        return payload

    monkeypatch.setattr(coff_x86_64, "assemble", assemble)
    monkeypatch.setattr(coff_x86_64, "emit_object", emit)
    assert coff_x86_64.assemble_object("text", phase_timing=totals) is payload
    assert calls == ["assemble", "emit"]
    if enabled:
        assert totals.ids == [9, 10]


@pytest.mark.parametrize("enabled", (False, True))
def test_unsupported_target_error_is_unchanged(enabled):
    totals = Probe() if enabled else None
    with pytest.raises(BackendUnavailable, match="^object emission target is unavailable: invalid$"):
        target_objects.encode_assembly_object("assembly", "invalid", phase_timing=totals)
    if enabled:
        assert totals.starts == 0 and totals.ids == []


def test_worker_reuses_existing_gate_and_reports_after_codegen_timer():
    source = Path(__file__).resolve().parents[2]
    text = (source / "pcc/frontends/python/pipeline_frontend_worker_execution.py").read_text()
    tree = ast.parse(text)
    gated = []
    reports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "worker_timing":
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ModulePhaseTiming" for n in ast.walk(node)):
                gated.append(node)
        if isinstance(node, ast.Try) and node.finalbody:
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "report" and isinstance(n.func.value, ast.Name) and n.func.value.id == "phase_timing" for n in node.finalbody for n in ast.walk(n)):
                reports.append(node)
    assert len(gated) == 1 and len(reports) == 1
    assert 'PCC_INDEXED_PHASE_TIMING' not in text
    assert 'codegen_ms = int((time.monotonic() - codegen_started) * 1000)' in ast.get_source_segment(text, reports[0])
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "report" for body in reports[0].body for n in ast.walk(body))


class DiagnosticAbort(BaseException):
    """A non-Exception fault, without interrupting pytest itself."""


class RecordingSink:
    def __init__(self, fail_at=None, error=None):
        self.lines = []
        self.flushes = 0
        self.fail_at = fail_at
        self.error = error

    def write(self, text):
        self.lines.append(text)
        if self.fail_at == "write":
            raise self.error

    def flush(self):
        self.flushes += 1
        if self.fail_at == "flush":
            raise self.error


def _events(sink):
    return [dict(part.split("=", 1) for part in line.split()[3:])
            for line in sink.lines]


def test_first_thirteen_field_indices_are_unchanged_and_children_are_appended():
    assert timing_module._PHASE_NAMES == (
        "frontend_generate_ns", "frontend_release_pre_ns", "backend_call_inclusive_ns",
        "prepare_verify_ns", "prepare_layout_ns", "stackmap_plan_ns",
        "function_emit_inclusive_ns", "backend_finalize_ns", "frontend_release_post_ns",
        "object_assemble_ns", "object_encode_ns", "object_write_ns", "function_setup_ns",
        "assembly_parse_ns", "assembly_measure_ns", "assembly_emit_finalize_ns",
    )


def test_assembly_receipts_are_six_flushed_bounded_sanitized_events(monkeypatch):
    sink = RecordingSink()
    ticks = iter((100, 103, 200, 205, 300, 307))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", lambda: next(ticks))
    totals = ModulePhaseTiming("pkg.module\n/secret path:=" + "z" * 200)
    for phase in (13, 14, 15):
        totals.complete_assembly(phase, totals.begin_assembly(phase))
    assert len(sink.lines) == sink.flushes == 6
    assert all(line.count("\n") == 1 and len(line) < 340 for line in sink.lines)
    rows = _events(sink)
    expected_module = "pkg.module__secret_path__" + "z" * (160 - 25)
    assert all(row["module"] == expected_module for row in rows)
    assert [(row["phase"], row["event"]) for row in rows] == [
        (phase, event) for phase in (
            "assembly_parse", "assembly_measure", "assembly_emit_finalize",
        ) for event in ("begin", "complete")
    ]
    assert all(row["parent"] == "object_assemble" and row["scope"] == "assembly"
               and row["clock_ok"] == "1" for row in rows)
    assert all("duration_ns" not in row for row in rows[::2])
    assert [row["duration_ns"] for row in rows[1::2]] == ["3", "5", "7"]
    assert totals.totals_ns == [0] * 13 + [3, 5, 7]
    assert set(vars(totals)) == {
        "module_name", "target", "route", "totals_ns", "failed", "reported",
    }


@pytest.mark.parametrize("family", ("assembly", "coordinator"))
@pytest.mark.parametrize("failure", ("start", "end", "zero", "backwards"))
@pytest.mark.parametrize("error_type", (RuntimeError, DiagnosticAbort))
def test_coarse_clock_faults_never_escape_or_read_again(monkeypatch, family, failure, error_type):
    calls = []
    sink = RecordingSink()

    def clock():
        calls.append(None)
        if failure == "start" or (failure == "end" and len(calls) == 2):
            raise error_type("clock failure")
        if failure == "zero":
            return 0
        return 100 if len(calls) == 1 else 99

    monkeypatch.setattr(timing_module.time, "perf_counter_ns", clock)
    monkeypatch.setattr(timing_module.sys, "stderr", sink)
    timer = ModulePhaseTiming("sample") if family == "assembly" else CoarsePhaseTiming()
    begin = timer.begin_assembly if family == "assembly" else timer.begin
    complete = timer.complete_assembly if family == "assembly" else timer.complete
    phase = 13 if family == "assembly" else "artifact_link"
    complete(phase, begin(phase))
    assert timer.failed
    before = len(calls)
    complete(phase, begin(phase))
    assert len(calls) == before and sink.flushes == 4
    rows = _events(sink)
    assert all(row["clock_ok"] == "0" and "duration_ns" not in row for row in rows[1:])
    if family == "assembly":
        assert timer.totals_ns == [0] * 16


@pytest.mark.parametrize("family", ("assembly", "coordinator"))
@pytest.mark.parametrize("failure", ("write", "flush"))
@pytest.mark.parametrize("error_type", (OSError, DiagnosticAbort))
def test_diagnostic_sink_faults_do_not_disable_real_timing(monkeypatch, family, failure, error_type):
    ticks = iter((11, 17))
    sink = RecordingSink(failure, error_type("closed diagnostic sink"))
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", lambda: next(ticks))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)
    timer = ModulePhaseTiming("sample") if family == "assembly" else CoarsePhaseTiming()
    if family == "assembly":
        timer.complete_assembly(13, timer.begin_assembly(13))
        assert timer.totals_ns[13] == 6
    else:
        timer.complete("artifact_link", timer.begin("artifact_link"))
    assert not timer.failed and len(sink.lines) == 2
    assert sink.flushes == (0 if failure == "write" else 2)


@pytest.mark.parametrize("error_type", (OSError, DiagnosticAbort))
def test_summary_sink_baseexception_is_once_and_preserves_active_error(error_type):
    original = DiagnosticAbort("compiler failure")
    sink = RecordingSink("write", error_type("sink failure"))
    totals = ModulePhaseTiming("sample")
    with pytest.raises(DiagnosticAbort) as caught:
        try:
            raise original
        finally:
            totals.report(sink, False)
            totals.report(sink, False)
    assert caught.value is original and len(sink.lines) == 1


@pytest.mark.parametrize("family", ("assembly", "coordinator"))
def test_unknown_boundaries_do_not_read_clock_or_write_diagnostics(monkeypatch, family):
    def forbidden():
        raise AssertionError("unknown boundary read a clock")

    sink = RecordingSink()
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", forbidden)
    monkeypatch.setattr(timing_module.sys, "stderr", sink)
    if family == "assembly":
        timer = ModulePhaseTiming("sample")
        for phase in (-1, 0, 12, 16):
            assert timer.begin_assembly(phase) == 0
            timer.complete_assembly(phase, 10)
    else:
        timer = CoarsePhaseTiming()
        assert timer.begin("untrusted/path\nlabel") == 0
        timer.complete("untrusted/path\nlabel", 10)
    assert sink.lines == [] and not timer.failed


def test_coordinator_vocabulary_is_fourteen_fixed_flushed_pairs(monkeypatch):
    labels = (
        "result_read", "runtime_verify", "artifact_link", "elf_stage_inputs",
        "elf_link_image", "elf_image_select_inputs", "elf_image_classify_layout",
        "elf_image_allocate_copy", "elf_image_resolve_relocate", "elf_image_finalize",
        "elf_verify_inputs", "elf_write_image",
        "executable_publish", "artifact_cleanup",
    )
    assert tuple(timing_module._COORDINATOR_PARENTS) == labels
    sink = RecordingSink()
    ticks = iter(range(100, 128))
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", lambda: next(ticks))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)
    timer = CoarsePhaseTiming()
    for label in labels:
        timer.complete(label, timer.begin(label))
    assert len(sink.lines) == sink.flushes == 28
    assert set(vars(timer)) == {"failed", "gc_unavailable", "gc_starts"}
    for index, row in enumerate(_events(sink)):
        label = labels[index // 2]
        assert row["scope"] == "coordinator" and row["phase"] == label
        expected_parent = ("elf_link_image" if 5 <= index // 2 <= 9 else
                           "artifact_link" if 3 <= index // 2 <= 12 else "none")
        assert row["parent"] == expected_parent
        assert row["event"] == ("begin" if index % 2 == 0 else "complete")
        assert row["clock_ok"] == "1" and "module" not in row
        assert row.get("duration_ns") == (None if index % 2 == 0 else "1")


@pytest.mark.parametrize("wrapper", (
    "assemble_file", "assemble_file_keeping_labels", "assemble_file_with_stack_maps",
    "assemble_file_with_stack_maps_keeping_labels",
))
@pytest.mark.parametrize("enabled", (False, True))
def test_x86_public_wrappers_preserve_exact_core_arguments(monkeypatch, wrapper, enabled):
    from pcc.backend import x86_64_asm_driver as driver

    timer = Probe() if enabled else None
    output, symbol, label = object(), object(), object()
    plans, keep = [], {"retained"}
    calls = []

    def core(*args, **kwargs):
        calls.append((args, kwargs))
        return output

    monkeypatch.setattr(driver, "_assemble_file", core)
    kwargs = {"phase_timing": timer}
    if "stack_maps" in wrapper:
        args = ("text", plans, keep) if wrapper.endswith("keeping_labels") else ("text", plans)
        kwargs.update(function_symbol=symbol, block_label=label, consume_stack_map_plans=True)
        expected = ("text", keep if wrapper.endswith("keeping_labels") else (), plans, symbol, label, True)
    else:
        args = ("text", keep) if wrapper.endswith("keeping_labels") else ("text",)
        expected = ("text", keep if wrapper.endswith("keeping_labels") else (), None, None, None)
    assert getattr(driver, wrapper)(*args, **kwargs) is output
    assert calls == [(expected, {"phase_timing": timer} if enabled else {})]


@pytest.mark.parametrize("target", (TARGETS[1], TARGETS[2]))
@pytest.mark.parametrize("enabled", (False, True))
def test_owned_elf_assemble_forwards_timer_only_to_x86(monkeypatch, target, enabled):
    from pcc.backend import owned_elf_link

    timer = Probe() if enabled else None
    result = object()
    calls = []

    def assemble(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    _stub(monkeypatch, "pcc.backend.x86_64_asm_driver", assemble_file=assemble)
    _stub(monkeypatch, "pcc.backend.arm64_elf_driver", assemble_file=assemble)
    assert owned_elf_link.assemble("text", target, phase_timing=timer) is result
    assert calls == [(("text",), {"phase_timing": timer} if enabled and target == TARGETS[2] else {})]


@pytest.mark.parametrize("packed", (False, True))
@pytest.mark.parametrize("enabled", (False, True))
def test_coff_plain_and_packed_routes_retain_markers_and_forward_timer(monkeypatch, packed, enabled):
    from pcc.backend import coff_x86_64

    timer = Probe() if enabled else None
    plans, symbol, label, output = [], object(), object(), object()
    procedures = [("f", [("action", ".seh_pushreg", "rbx")], "prolog", "end")]
    calls = []
    elf = types.SimpleNamespace(symbols=[object()], sections=[])

    def assemble(*args, **kwargs):
        expected_args = ("clean", plans, {"action", "prolog", "end"}) if packed else (
            "clean", {"action", "prolog", "end"},
        )
        expected_kwargs = ({"function_symbol": symbol, "block_label": label,
                            "consume_stack_map_plans": True} if packed else {})
        if enabled:
            expected_kwargs["phase_timing"] = timer
        assert args == expected_args and kwargs == expected_kwargs
        calls.append("assemble")
        return elf

    def unwind(sections, symbols, actual):
        assert sections == symbols == [] and actual is procedures
        calls.append("unwind")

    def final(sections, symbols):
        assert sections == symbols == ()
        calls.append("final")
        return output

    _stub(monkeypatch, "pcc.backend.x86_64_asm_driver",
          assemble_file_keeping_labels=assemble,
          assemble_file_with_stack_maps_keeping_labels=assemble)
    _stub(monkeypatch, "pcc.backend.self_backend_x86_64_linux", _asm_symbol=symbol, _block_label=label)
    monkeypatch.setattr(coff_x86_64, "_unwind_source", lambda text: ("clean", procedures))
    monkeypatch.setattr(coff_x86_64, "_append_unwind", unwind)
    monkeypatch.setattr(coff_x86_64, "CoffObject", final)
    assert coff_x86_64.assemble("text", stack_map_plans=plans if packed else None,
                               consume_stack_map_plans=True, phase_timing=timer) is output
    assert calls == ["assemble", "unwind", "final"]


@pytest.mark.parametrize("failure", (None, "parse", "measure", "finalize"))
@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("error_type", (ValueError, DiagnosticAbort))
def test_mock_assembly_boundaries_keep_result_and_exception_identity(monkeypatch, failure, enabled, error_type):
    from pcc.backend import x86_64_asm_driver as driver

    timer = Probe() if enabled else None
    calls, output, cache, null = [], object(), object(), types.SimpleNamespace(name="")
    error = error_type("original assembly operation")

    def operation(name):
        calls.append(name)
        if failure == name:
            raise error

    section = types.SimpleNamespace(name=".text", labels=[], type=1, flags=6, align=1, size=0)

    # The single pass places and encodes (13), finishing sections resolves
    # their label references (14), and publication builds the object (15).
    def parse(text, operand_cache):
        assert text == "source" and operand_cache is cache
        operation("parse")
        return {".text": section}, [".text"], {}, {}

    def measure(built, labels, symbols):
        assert built is section and labels == symbols == {}
        operation("measure")
        return b"", []

    def finalize(sections, symbols):
        assert [item[0] for item in sections] == [".text"] and symbols == (null,)
        operation("finalize")
        return output

    monkeypatch.setattr(driver, "_encode_file", parse)
    monkeypatch.setattr(driver, "_OperandParseCache", lambda: cache)
    monkeypatch.setattr(driver, "_finish_section", measure)
    monkeypatch.setattr(driver, "ElfSymbol", types.SimpleNamespace(null=lambda: null))
    monkeypatch.setattr(driver, "ElfSection", lambda *fields: fields)
    monkeypatch.setattr(driver, "ElfObject", finalize)
    monkeypatch.setattr(timing_module.time, "perf_counter_ns", lambda: pytest.fail("unexpected real clock"))
    if failure is None:
        assert driver._assemble_file("source", (), None, None, None, phase_timing=timer) is output
        assert calls == ["parse", "measure", "finalize"]
    else:
        with pytest.raises(error_type) as caught:
            driver._assemble_file("source", (), None, None, None, phase_timing=timer)
        assert caught.value is error
        assert calls == ["parse", "measure", "finalize"][:("parse", "measure", "finalize").index(failure) + 1]
    if enabled:
        expected = []
        for index, name in enumerate(calls):
            expected.append(("begin", 13 + index, index + 1))
            if name != failure:
                expected.append(("complete", 13 + index, index + 1))
        assert timer.events == expected


def test_assembly_timers_enclose_coarse_work_and_never_instruction_loops():
    source = Path(__file__).resolve().parents[2]
    tree = ast.parse((source / "pcc/backend/x86_64_asm_driver.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_assemble_file")
    boundaries = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("begin_assembly", "complete_assembly")]
    assert sorted((node.args[0].value, node.func.attr) for node in boundaries) == [
        (phase, method) for phase in (13, 14, 15) for method in ("begin_assembly", "complete_assembly")
    ]
    for node in ast.walk(function):
        if isinstance(node, (ast.For, ast.While, ast.comprehension)):
            assert not any(call in boundaries for call in ast.walk(node))
    starts = {node.args[0].value: node.lineno for node in boundaries if node.func.attr == "begin_assembly"}
    ends = {node.args[0].value: node.lineno for node in boundaries if node.func.attr == "complete_assembly"}
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_encode_file":
                assert starts[13] < node.lineno < ends[13]
            elif node.func.id in ("_finish_section", "build_x86_64_stack_map_payload"):
                assert starts[14] < node.lineno < ends[14]
            elif node.func.id in ("ElfSymbol", "ElfRelocation", "ElfSection", "ElfObject"):
                assert starts[15] < node.lineno < ends[15]
            # Every instruction is encoded inside the single pass.
            assert node.func.id != "encode_instruction"
    assert "assembly_emit_finalize_ns" in timing_module._PHASE_NAMES


def _source_function(relative, name):
    source = Path(__file__).resolve().parents[2]
    tree = ast.parse((source / relative).read_text())
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _has_phase(node, phase):
    return any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
               and call.func.attr == "begin" and call.args
               and isinstance(call.args[0], ast.Constant) and call.args[0].value == phase
               for call in ast.walk(node))


def _mocked_source_body(body, namespace):
    # Compile only the selected production control-flow statements. No pipeline
    # import, parser, emitter, subprocess, or real filesystem operation is used.
    function = ast.parse("def selected_control():\n    pass\n").body[0]
    function.body = body
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<mocked production control>", "exec"), namespace)
    return namespace["selected_control"]


_ELF_IMAGE_PHASES = (
    "elf_image_select_inputs", "elf_image_classify_layout", "elf_image_allocate_copy",
    "elf_image_resolve_relocate", "elf_image_finalize",
)


def _elf_image_timing_skeleton():
    # Read the production source, but execute only its six timing guards and
    # unchanged return. Every complete linker region is replaced by one fake.
    function = _source_function("pcc/backend/elf_x86_64.py", "_link_static_executable_image")
    args = function.args
    assert args.kwonlyargs[-1].arg == "coordinator_timing"
    assert isinstance(args.kw_defaults[-1], ast.Constant) and args.kw_defaults[-1].value is None
    assert [arg.arg for arg in args.kwonlyargs] == [
        "archives", "entry", "base_address", "archive_selections",
        "compact_archive_relocations", "_input_store", "coordinator_timing",
    ]
    assert isinstance(function.body[0], ast.Expr) and isinstance(function.body[0].value, ast.Constant)
    assert ast.unparse(function.body[1]) == "image_phase_started = 0"
    assert ast.unparse(function.body[-1]) == "return image"
    guards = []
    for index, node in enumerate(function.body):
        if isinstance(node, ast.If) and any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "coordinator_timing"
                and child.func.attr in ("begin", "complete")
                for child in ast.walk(node)):
            guards.append((index, node))
    assert len(guards) == 6 and guards[0][0] == 2
    assert guards[-1][0] == len(function.body) - 2
    expected_bodies = []
    for index in range(6):
        lines = ["if coordinator_timing is not None:"]
        if index:
            lines.append('    coordinator_timing.complete("' + _ELF_IMAGE_PHASES[index - 1]
                         + '", image_phase_started)')
        if index < 5:
            lines.append('    image_phase_started = coordinator_timing.begin("'
                         + _ELF_IMAGE_PHASES[index] + '")')
        expected_bodies.append(ast.parse("\n".join(lines)).body[0])
    for (_index, actual), expected in zip(guards, expected_bodies):
        assert ast.dump(actual, include_attributes=False) == ast.dump(expected, include_attributes=False)
    # Exact starts/ends establish five exhaustive, sequential spans, with both
    # original cache clears inside their original regions. No loop is timed.
    regions = [function.body[left[0] + 1:right[0]]
               for left, right in zip(guards, guards[1:])]
    assert all(regions)
    assert ast.unparse(regions[0][0].test) == "not objects"
    assert ast.unparse(regions[0][-1].test) == "entry not in definitions"
    assert ast.unparse(regions[1][0].target) == "alloc_sections"
    assert ast.unparse(regions[1][-1]) == "image_size = max(rx_file_end, rw_file_end, metadata_file_end, header_bytes)"
    assert ast.unparse(regions[2][0]) == "if _input_store is not None:\n    _input_store.clear()"
    assert ast.unparse(regions[2][-1].test) == "veneer_pools"
    assert ast.unparse(regions[3][0]) == "boundaries = {}"
    assert isinstance(regions[3][-1], ast.For) and ast.unparse(regions[3][-1].iter) == "alloc_sections"
    assert ast.unparse(regions[4][0]) == "if _input_store is not None:\n    _input_store.clear()"
    assert isinstance(regions[4][-1], ast.For) and ast.unparse(regions[4][-1].iter) == "program_headers"
    assert sum(map(len, regions)) + len(guards) + 3 == len(function.body)
    assert not any(isinstance(node, ast.Name) and node.id in ("CoarsePhaseTiming", "time", "sys")
                   for node in ast.walk(function))
    public = _source_function("pcc/backend/elf_x86_64.py", "link_static_executable")
    assert "coordinator_timing" not in ast.unparse(public)
    skeleton = [function.body[1]]
    for index, (_position, guard) in enumerate(guards):
        skeleton.append(guard)
        if index < 5:
            skeleton.append(ast.parse('operation("' + _ELF_IMAGE_PHASES[index] + '")').body[0])
    skeleton.append(function.body[-1])
    return skeleton


def test_elf_image_timing_has_five_exhaustive_top_level_spans():
    _elf_image_timing_skeleton()


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("failure", (None, *_ELF_IMAGE_PHASES))
@pytest.mark.parametrize("error_type", (ValueError, DiagnosticAbort))
@pytest.mark.parametrize("diagnostic_fault", (None, "clock", "write", "flush"))
def test_mock_elf_image_boundaries_keep_success_and_each_failure(
    monkeypatch, enabled, failure, error_type, diagnostic_fault,
):
    operations, reads = [], []
    sink = RecordingSink(diagnostic_fault, DiagnosticAbort("diagnostic failure"))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)

    def clock():
        assert enabled, "disabled final-image timing read a clock"
        reads.append(None)
        if diagnostic_fault == "clock":
            raise DiagnosticAbort("clock failure")
        return 100 + len(reads)

    monkeypatch.setattr(timing_module.time, "perf_counter_ns", clock)
    timer = CoarsePhaseTiming() if enabled else None
    error, cause, image = error_type("original final-image operation"), OSError("original cause"), object()

    def operation(phase):
        operations.append(phase)
        if phase == failure:
            raise error from cause

    invoke = _mocked_source_body(_elf_image_timing_skeleton(), dict(
        coordinator_timing=timer, operation=operation, image=image,
    ))
    if failure is None:
        assert invoke() is image
        expected_operations = list(_ELF_IMAGE_PHASES)
    else:
        with pytest.raises(error_type) as caught:
            invoke()
        assert caught.value is error and caught.value.__cause__ is cause
        assert caught.value.__suppress_context__
        expected_operations = list(_ELF_IMAGE_PHASES[:_ELF_IMAGE_PHASES.index(failure) + 1])
    assert operations == expected_operations
    expected_events = []
    if enabled:
        for phase in expected_operations:
            expected_events.append((phase, "begin"))
            if phase != failure:
                expected_events.append((phase, "complete"))
    rows = _events(sink)
    assert [(row["phase"], row["event"]) for row in rows] == expected_events
    assert all(row["parent"] == "elf_link_image" and row["scope"] == "coordinator" for row in rows)
    assert all("duration_ns" not in row for row in rows if row["event"] == "begin")
    assert len(reads) == (1 if enabled and diagnostic_fault == "clock" else len(expected_events))
    assert sink.flushes == (0 if diagnostic_fault == "write" else len(expected_events))
    if diagnostic_fault == "clock":
        assert all(row["clock_ok"] == "0" and "duration_ns" not in row for row in rows)
    else:
        assert all(row["clock_ok"] == "1" for row in rows)
        assert all(row["duration_ns"] == "1" for row in rows if row["event"] == "complete")


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("failure,secondary", (
    (None, None), ("stage", None), ("link", None), ("clear", None),
    ("verify", None), ("manifest_verify", None), ("parse", None), ("write", None), ("chmod", None),
    ("replace", None), ("cleanup", None), ("link", "clear"), ("write", "cleanup"),
))
@pytest.mark.parametrize("error_type", (ValueError, DiagnosticAbort))
@pytest.mark.parametrize("sink_failure", (False, True))
def test_mock_elf_tail_keeps_operation_and_cleanup_error_precedence(
    monkeypatch, enabled, failure, secondary, error_type, sink_failure,
):
    from pcc.backend import owned_elf_link as owner

    operations, clock_reads = [], []
    error = error_type("original operation")
    secondary_error = error_type("original cleanup")
    sink = RecordingSink("write" if sink_failure else None, DiagnosticAbort("sink"))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)

    def clock():
        assert enabled, "disabled instrumentation read a clock"
        clock_reads.append(None)
        return 100 + len(clock_reads)

    monkeypatch.setattr(timing_module.time, "perf_counter_ns", clock)
    timer = CoarsePhaseTiming() if enabled else None

    def operation(name):
        operations.append(name)
        if name == secondary:
            raise secondary_error
        if name == failure:
            raise error

    staged, thread, intrinsic, image = types.SimpleNamespace(machine=62), object(), object(), object()
    manifest_reads = []

    def manifest_hash(path):
        assert path == "inputs.manifest"
        operation("manifest_hash" if not manifest_reads else "manifest_verify")
        manifest_reads.append(path)
        return "frozen manifest identity"

    def read_manifest(path):
        assert path == "inputs.manifest"
        operation("manifest_read")
        return [("PCO", "object.o")]

    class Store:
        def __init__(self, target, archives):
            assert target == TARGETS[2] and archives == ["runtime.a"]
            operation("store")

        def stage(self, path, **kwargs):
            assert path == "object.o" and kwargs == {"kind": "PCO"}
            operation("stage")
            return staged

        def clear(self):
            operation("clear")

        def verify(self):
            operation("verify")

    def assemble(text, target):
        assert text == "intrinsic assembly" and target == TARGETS[2]
        operation("assemble")
        return intrinsic

    def link(inputs, **kwargs):
        assert inputs == [staged, thread, intrinsic]
        assert isinstance(kwargs["_input_store"], Store)
        expected = dict(entry="_start", archive_selections=None, _input_store=kwargs["_input_store"])
        if enabled:
            expected["coordinator_timing"] = timer
        assert kwargs == expected
        operation("link")
        return image

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    stream = Stream()

    def open_image(path, mode):
        assert path == "out.pcc-link.tmp" and mode == "wb"
        operation("open")
        return stream

    def write(actual, payload):
        assert actual is stream and payload is image
        operation("write")

    monkeypatch.setattr(owner, "ElfInputStore", Store)
    monkeypatch.setattr(owner, "file_sha256", manifest_hash)
    monkeypatch.setattr(owner, "read_internal_input_manifest", read_manifest)
    monkeypatch.setattr(owner, "_thread_pointer_object", lambda target: thread)
    monkeypatch.setattr(owner, "assemble", assemble)
    monkeypatch.setattr(owner, "_link_static_executable_image", link)
    monkeypatch.setattr(owner, "parse_static_executable", lambda actual: operation("parse"))
    monkeypatch.setattr(owner, "_write_image_chunks", write)
    monkeypatch.setattr(owner, "open", open_image, raising=False)
    monkeypatch.setattr(owner, "os", types.SimpleNamespace(
        path=types.SimpleNamespace(realpath=lambda path: path,
                                   exists=lambda path: failure == "cleanup" or secondary == "cleanup"),
        chmod=lambda path, mode: operation("chmod"),
        replace=lambda source, destination: operation("replace"),
        unlink=lambda path: operation("cleanup"),
    ))
    _stub(monkeypatch, "pcc.backend.linux_thread_intrinsics", assembly=lambda arm: "intrinsic assembly")
    kwargs = dict(target=TARGETS[2], output="out", manifest="inputs.manifest", archives=("runtime.a",))
    if enabled:
        kwargs["coordinator_timing"] = timer
    if failure is None:
        assert owner.link_inputs(**kwargs) is None
    else:
        with pytest.raises(error_type) as caught:
            owner.link_inputs(**kwargs)
        assert caught.value is (secondary_error if secondary else error)
    normal = ["manifest_hash", "manifest_read", "store", "stage", "assemble", "link", "clear",
              "verify", "manifest_verify", "parse", "open", "write", "chmod", "replace"]
    if failure is None:
        expected = normal
    elif failure == "cleanup":
        expected = normal + ["cleanup"]
    else:
        expected = normal[:normal.index(failure) + 1]
        if failure == "link":
            expected.append("clear")
        elif secondary == "cleanup":
            expected.append("cleanup")
    assert operations == expected
    expected_events = []
    if enabled:
        expected_events.append(("elf_stage_inputs", "begin"))
        if "assemble" in operations:
            expected_events += [("elf_stage_inputs", "complete"), ("elf_link_image", "begin")]
            if failure != "link":
                expected_events.append(("elf_link_image", "complete"))
            expected_events.append(("elf_verify_inputs", "begin"))
            if "parse" in operations and failure != "parse":
                expected_events += [("elf_verify_inputs", "complete"), ("elf_write_image", "begin")]
                if failure is None:
                    expected_events.append(("elf_write_image", "complete"))
    rows = _events(sink)
    assert [(row["phase"], row["event"]) for row in rows] == expected_events
    assert all("duration_ns" not in row for row in rows if row["event"] == "begin")
    assert len(clock_reads) == len(expected_events)


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("failure,secondary", (
    (None, None), ("runtime", None), ("link", None), ("cleanup", None), ("link", "cleanup"),
))
@pytest.mark.parametrize("error_type", (ValueError, DiagnosticAbort))
@pytest.mark.parametrize("sink_failure", (False, True))
def test_mock_direct_artifact_branch_keeps_runtime_link_cleanup_order(
    monkeypatch, enabled, failure, secondary, error_type, sink_failure,
):
    function = _source_function("pcc/frontends/python/pipeline.py", "compile_python_multi")
    branch = next(node for node in function.body if isinstance(node, ast.If) and _has_phase(node, "runtime_verify"))
    operations, ticks = [], []
    error, secondary_error = error_type("original operation"), error_type("original cleanup")
    sink = RecordingSink("write" if sink_failure else None, DiagnosticAbort("diagnostic"))
    monkeypatch.setattr(timing_module.sys, "stderr", sink)

    def clock():
        assert enabled
        ticks.append(None)
        return 100 + len(ticks)

    monkeypatch.setattr(timing_module.time, "perf_counter_ns", clock)
    timer = CoarsePhaseTiming() if enabled else None
    artifacts, runtime = [("sample", "PCO", "object")], object()
    object_inputs = ("extra.o",)

    def operation(name):
        operations.append(name)
        if name == secondary:
            raise secondary_error
        if name == failure:
            raise error

    def verify(actual, **kwargs):
        assert actual == "runtime" and kwargs == {"target_triple": None}
        operation("runtime")
        return runtime

    def link(*args, **kwargs):
        assert args == (artifacts, "out", runtime, False)
        expected = dict(needs_libpython=False, needs_native_extension_exports=False,
                        extra_link_inputs=object_inputs, extra_link_args=("flag",), profile=None)
        if enabled:
            expected["coordinator_timing"] = timer
        assert kwargs == expected
        operation("link")

    namespace = dict(
        emit_llvm_only=False, native_backend="self", libpython_mode="off", any_needs_libpython=False,
        any_needs_native_extension_exports=False, runtime_archive="runtime", target_triple=None,
        module_direct_artifacts=artifacts, module_ir_texts=[("sample", "")],
        object_inputs=object_inputs,
        total_ir_bytes_before_passes=7, out_path="out", verbose=False, link_args=["flag"],
        profile=None, total_start=0, direct_native_object_dir="work", coordinator_timing=timer,
        PyPipelineError=RuntimeError, _reject_mixed_extension_object_models=lambda **kwargs: None,
        _explicit_runtime_archive=verify, _validate_stage1_checkpoint_link=lambda *args: None,
        _profile_counter=lambda *args: None, _profile_end=lambda *args: None,
        _link_with_self_backend_direct_artifacts=link,
        _remove_direct_native_object_dir=lambda path: operation("cleanup"),
        os=types.SimpleNamespace(environ={}, path=types.SimpleNamespace(getsize=lambda path: 5)),
    )
    invoke = _mocked_source_body(branch.body, namespace)
    if failure is None:
        assert invoke() is None
    else:
        with pytest.raises(error_type) as caught:
            invoke()
        assert caught.value is (secondary_error if secondary else error)
    expected_operations = ["runtime"] if failure == "runtime" else ["runtime", "link", "cleanup"]
    assert operations == expected_operations
    expected_events = []
    if enabled:
        expected_events = [("runtime_verify", "begin")]
        if failure != "runtime":
            expected_events += [("runtime_verify", "complete"), ("artifact_link", "begin")]
            if failure != "link":
                expected_events.append(("artifact_link", "complete"))
            expected_events.append(("artifact_cleanup", "begin"))
            if failure != "cleanup" and secondary != "cleanup":
                expected_events.append(("artifact_cleanup", "complete"))
    assert [(row["phase"], row["event"]) for row in _events(sink)] == expected_events
    assert len(ticks) == len(expected_events)


@pytest.mark.parametrize("platform", ("linux", "darwin", "win32"))
@pytest.mark.parametrize("direct", (False, True))
@pytest.mark.parametrize("policy", (False, True))
@pytest.mark.parametrize("defer", ("", "PCC_DEFER_SELF_LINK_PLAN", "PCC_DEFER_FRONTEND_CODEGEN_PLAN"))
def test_coordinator_construction_uses_existing_linux_direct_policy_only(monkeypatch, platform, direct, policy, defer):
    function = _source_function("pcc/frontends/python/pipeline.py", "compile_python_multi")
    blocks = [node for node in ast.walk(function) if isinstance(node, ast.If)
              and isinstance(node.test, ast.Name) and node.test.id == "direct_emit"
              and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                      and call.func.id == "CoarsePhaseTiming" for call in ast.walk(node))]
    assert len(blocks) == 1
    calls, sentinel = [], object()

    def construct():
        calls.append("construct")
        return sentinel

    _stub(monkeypatch, "pcc.backend.indexed_phase_timing", CoarsePhaseTiming=construct)
    namespace = dict(direct_emit=direct, out_path="out", coordinator_timing=None,
                     _prepare_direct_native_object_dir=lambda path: "work",
                     _python_frontend_worker_timing_enabled=lambda: policy,
                     sys=types.SimpleNamespace(platform=platform),
                     os=types.SimpleNamespace(environ={defer: "plan"} if defer else {}))
    module = ast.Module(body=blocks, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<coordinator policy>", "exec"), namespace)
    expected = platform == "linux" and direct and policy and not defer
    assert calls == (["construct"] if expected else [])
    assert namespace["coordinator_timing"] is (sentinel if expected else None)
    assert "PCC_INDEXED_PHASE_TIMING" not in ast.unparse(blocks[0])


@pytest.mark.parametrize("failure", ("policy", "construct"))
def test_coordinator_setup_baseexception_cannot_change_build(monkeypatch, failure):
    function = _source_function("pcc/frontends/python/pipeline.py", "compile_python_multi")
    block = next(node for node in ast.walk(function) if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Name) and node.test.id == "direct_emit"
                 and "CoarsePhaseTiming" in ast.unparse(node))
    error = DiagnosticAbort("optional timing setup")

    def policy():
        if failure == "policy":
            raise error
        return True

    def construct():
        raise error

    _stub(monkeypatch, "pcc.backend.indexed_phase_timing", CoarsePhaseTiming=construct)
    namespace = dict(direct_emit=True, out_path="out", coordinator_timing=None,
                     _prepare_direct_native_object_dir=lambda path: "work",
                     _python_frontend_worker_timing_enabled=policy,
                     sys=types.SimpleNamespace(platform="linux"), os=types.SimpleNamespace(environ={}))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])),
                 "<coordinator setup failure>", "exec"), namespace)
    assert namespace["coordinator_timing"] is None


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("failure", (None, "read", "invalid"))
@pytest.mark.parametrize("error_type", (OSError, DiagnosticAbort))
def test_mock_result_reader_completes_only_after_valid_ordered_result(monkeypatch, enabled, failure, error_type):
    function = _source_function("pcc/frontends/python/pipeline_frontend_parallel.py", "compile_parallel_uncached")
    bodies = [node.body for node in ast.walk(function) if isinstance(getattr(node, "body", None), list)]
    matching = [(body, index) for body in bodies for index, statement in enumerate(body)
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and statement.value.value == 0
                and any(isinstance(target, ast.Name) and target.id == "result_started" for target in statement.targets)]
    assert len(matching) == 1
    body, start = matching[0]
    timer = Probe() if enabled else None
    error = error_type("read failed")
    operations = []

    class ResultStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            operations.append("read")
            if failure == "read":
                raise error
            return "invalid" if failure == "invalid" else "OK\t0\tsample\t0\t0\t7\tir\tPCO\tobject"

    def open_result(path, mode, **kwargs):
        assert path == "result" and mode == "r" and kwargs == {"encoding": "utf-8"}
        if enabled:
            assert timer.events == [("begin", "result_read", 1)]
        return ResultStream()

    namespace = dict(
        coordinator_timing=timer, profile=None, profile_begin=lambda value: 1,
        profile_end=lambda *args: None, profile_counter=lambda *args: None,
        result_paths=["result"], host_batch_limit=0, checkpoint_root="", module_names=["sample"],
        module_ir_by_index=[None], module_meta_by_index=[None], module_asm_by_index=[None],
        module_native_object_by_index=[None], action_state=None, action_root="",
        open=open_result, pipeline_error=ValueError,
        os=types.SimpleNamespace(path=types.SimpleNamespace(isfile=lambda path: path == "object")),
    )
    invoke = _mocked_source_body(body[start:], namespace)
    if failure is None:
        assert invoke() == ([("sample", "")], False, False, 7, [], None,
                            [("sample", "object")], [("sample", "PCO", "object")])
    else:
        with pytest.raises(error_type if failure == "read" else ValueError) as caught:
            invoke()
        if failure == "read":
            assert caught.value is error
        else:
            assert str(caught.value) == "invalid frontend worker result"
    assert operations == ["read"]
    if enabled:
        assert timer.events == ([("begin", "result_read", 1), ("complete", "result_read", 1)]
                                if failure is None else [("begin", "result_read", 1)])


@pytest.mark.parametrize("enabled", (False, True))
@pytest.mark.parametrize("failure", (None, "link", "publish"))
@pytest.mark.parametrize("error_type", (ValueError, DiagnosticAbort))
def test_mock_executable_publication_keeps_calls_and_failed_boundary(monkeypatch, enabled, failure, error_type):
    function = _source_function("pcc/frontends/python/pipeline.py", "_link_with_self_backend_direct_artifacts")
    timer = Probe() if enabled else None
    error = error_type("operation failed")
    operations = []

    class TemporaryDirectory:
        def __init__(self, **kwargs):
            assert kwargs == {"prefix": "pcc_py_direct_artifacts_"}

        def __enter__(self):
            return "temporary"

        def __exit__(self, *args):
            operations.append("temporary_cleanup")
            return False

    def link(*args, **kwargs):
        assert args == (["cc", "object", "extra.o", "-o", "out.tmp"], None, "out.tmp", "runtime", ("extra.o",), False)
        expected = dict(extra_link_args=("flag",), needs_libpython=False,
                        needs_native_extension_exports=False,
                        pcc_internal_input_manifest="temporary/internal-inputs.txt", link_profile_path=None)
        if enabled:
            expected["coordinator_timing"] = timer
        assert kwargs == expected
        operations.append("link")
        if failure == "link":
            raise error

    def publish(*args, **kwargs):
        assert args == ("out.tmp", "out", None) and kwargs == {"signature_owned_by_pcc": True}
        if enabled:
            assert timer.events == [("begin", "executable_publish", 1)]
        operations.append("publish")
        if failure == "publish":
            raise error

    namespace = dict(
        artifacts=[("sample", "PCO", "object")], out_path="out", runtime_archive="runtime",
        verbose=False, needs_libpython=False, needs_native_extension_exports=False,
        extra_link_inputs=("extra.o",), extra_link_args=("flag",), profile=None, coordinator_timing=timer,
        _macho_semantic_layout_enabled=lambda: False, _validate_pcc_self_link_surface=lambda **kwargs: None,
        _resolve_self_link_mode=lambda: "pcc", PyPipelineError=ValueError,
        _write_direct_input_manifest=lambda *args: operations.append("manifest"),
        _profile_begin=lambda *args: 0, _profile_end=lambda *args: None,
        _run_self_link_command=link, _finish_self_backend_executable=publish,
        tempfile=types.SimpleNamespace(TemporaryDirectory=TemporaryDirectory),
        sys=types.SimpleNamespace(platform="linux"),
        os=types.SimpleNamespace(environ={}, path=types.SimpleNamespace(
            abspath=lambda path: path, isfile=lambda path: True, join=lambda *parts: "/".join(parts))),
    )
    invoke = _mocked_source_body(function.body, namespace)
    if failure is None:
        assert invoke() is None
    else:
        with pytest.raises(error_type) as caught:
            invoke()
        assert caught.value is error
    assert operations == (["manifest", "link", "temporary_cleanup"] if failure == "link" else
                          ["manifest", "link", "publish", "temporary_cleanup"])
    if enabled:
        expected = [] if failure == "link" else [("begin", "executable_publish", 1)]
        if failure is None:
            expected.append(("complete", "executable_publish", 1))
        assert timer.events == expected


def _mocked_source_function(relative, name, namespace):
    function = _source_function(relative, name)
    module = ast.Module(body=[ast.parse("from __future__ import annotations").body[0], function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<mocked production function>", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("direct", (False, True))
@pytest.mark.parametrize("enabled", (False, True))
def test_parallel_direct_forwarding_preserves_untimed_exact_kwargs(direct, enabled):
    timer = Probe() if enabled else None
    output, paths, modules = object(), ["source"], ["sample"]
    calls = []

    def uncached(*args, **kwargs):
        calls.append((args, kwargs))
        return output

    def forbidden(*args, **kwargs):
        raise AssertionError("unselected cache or process operation")

    namespace = {"os": types.SimpleNamespace(environ={"PCC_DIRECT_INDEXED_KERNEL_EMIT": "1" if direct else "0"})}
    invoke = _mocked_source_function("pcc/frontends/python/pipeline_frontend_parallel.py", "compile_parallel", namespace)
    kwargs = dict(jobs=0, entry_module="sample", sibling_inits=[], libpython_mode="off",
                  ir_scaffold_mode="off", verbose=False, profile=None, artifact_dir="artifacts",
                  coordinator_timing=timer, compile_uncached=uncached)
    for name in ("can_spawn_worker", "worker_command_prefix", "plan_cache", "load_cache", "acquire_cache",
                 "wait_cache", "publish_cache", "release_cache", "host_python_command", "source_root",
                 "profile_begin", "profile_end", "profile_counter"):
        kwargs[name] = forbidden
    assert invoke(paths, modules, **kwargs) is output
    expected = dict(jobs=0, entry_module="sample", sibling_inits=[], libpython_mode="off",
                    ir_scaffold_mode="off", verbose=False, profile=None, artifact_dir="artifacts")
    if direct:
        expected["action_cache_plan"] = None
        if enabled:
            expected["coordinator_timing"] = timer
    assert calls == [((paths, modules), expected)]


@pytest.mark.parametrize("target", (TARGETS[1], TARGETS[2], TARGETS[3]))
@pytest.mark.parametrize("enabled", (False, True))
def test_linux_link_bridge_only_forwards_optional_timer_to_elf(monkeypatch, target, enabled):
    timer, calls = Probe() if enabled else None, []
    _stub(monkeypatch, "pcc.backend.owned_elf_link", link_inputs=lambda **kwargs: calls.append(("elf", kwargs)))
    _stub(monkeypatch, "pcc.backend.owned_pe_link", link_inputs=lambda **kwargs: calls.append(("pe", kwargs)))
    _stub(monkeypatch, "pcc.frontends.python.pipeline_targets", host_target_triple=lambda: target)
    namespace = {"SelfBackendLinkError": ValueError}
    invoke = _mocked_source_function("pcc/frontends/python/pipeline_self_backend_link.py", "run_link_command", namespace)
    invoke([], "assembly", "out", "runtime.a", ("extra.o", "extra.a"), False,
           target_triple=target, coordinator_timing=timer, pcc_asm_inputs=("other.s",),
           pcc_native_object_inputs=("object.o",),
           resolve_self_link_mode=lambda **kwargs: "pcc",
           validate_pcc_self_link_surface=lambda **kwargs: None,
           repo_root_for_link=None, host_python_command=None, build_pcc_link_command=None,
           log=None, join_strings=None)
    expected = dict(output="out", assembly=["assembly", "other.s"],
                    objects=["object.o", "extra.o"], archives=["runtime.a", "extra.a"], manifest="")
    if target != TARGETS[3]:
        expected["target"] = target
        if enabled:
            expected["coordinator_timing"] = timer
    assert calls == [("pe" if target == TARGETS[3] else "elf", expected)]
