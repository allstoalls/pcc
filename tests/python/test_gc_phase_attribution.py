"""Bounded phase-accounting behavior and native closed-world wiring.

Fake counters test reporting, not collector performance. Native execution of
wide runtime accounting lives in test_gc_phase_counter_widths.py.
"""

import ast
import io
import re
from pathlib import Path

import pytest

from pcc.backend import indexed_phase_timing as timing


class DiagnosticAbort(BaseException):
    pass


@pytest.fixture
def counters(monkeypatch):
    values = {metric: 100 + metric for metric in timing._GC_COUNTER_IDS}
    values["backend"] = 4
    monkeypatch.setattr(timing.sys.implementation, "name", "pcc")
    monkeypatch.setattr(timing, "_pcc_gc_backend", lambda: values["backend"])
    monkeypatch.setattr(timing, "_pcc_gc_telemetry", lambda metric: values[metric])
    return values


def rows(sink):
    return [dict(part.split("=", 1) for part in line.split()[3:])
            for line in sink.getvalue().splitlines()]


def test_process_counters_follow_existing_nested_phase_boundaries(monkeypatch, counters):
    sink = io.StringIO()
    monkeypatch.setattr(timing.sys, "stderr", sink)
    ticks = iter((100, 200, 300, 400))
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: next(ticks))
    timer = timing.CoarsePhaseTiming()
    outer = timer.begin("elf_link_image")
    for metric in timing._GC_COUNTER_IDS[:8]:
        counters[metric] += 3
    inner = timer.begin("elf_image_resolve_relocate")
    for metric in timing._GC_COUNTER_IDS[:8]:
        counters[metric] += 5
    counters[7] = 1 << 34
    counters[42] = 2  # A current-size gauge may legitimately decrease.
    timer.complete("elf_image_resolve_relocate", inner)
    for metric in timing._GC_COUNTER_IDS[:8]:
        counters[metric] += 7
    timer.complete("elf_link_image", outer)
    events = rows(sink)
    assert len(events) == 4 and timer.gc_starts == {}
    assert all(row["gc_available"] == "1" for row in events)
    assert all("gc_work_steps_delta" not in row for row in events[:2])
    for row, delta, duration in ((events[2], 5, 100), (events[3], 15, 300)):
        assert row["gc_scope"] == "process"
        assert row["gc_snapshot"] == "non_atomic"
        assert row["gc_recorded_work"] == "inclusive"
        assert row["gc_event_counter_bits"] == "32"
        assert row["gc_accounting_counter_bits"] == "64"
        assert row["gc_wide_delta_ok"] == row["gc_event_delta_ok"] == "1"
        assert row["duration_ns"] == str(duration)
        for name in timing._GC_DELTA_NAMES:
            assert row["gc_" + name + "_delta"] == str(delta)
        assert row["gc_max_recorded_pause_us_since_reset_end"] == str(1 << 34)
        assert row["gc_store_buffer_entries_end"] == "2"
        assert not any("max" in name and name.endswith("_delta") for name in row)


def test_host_does_not_query_or_substitute_collectors(monkeypatch):
    def forbidden(*args):
        raise AssertionError("host code must not query native telemetry")

    monkeypatch.setattr(timing, "_pcc_gc_backend", forbidden)
    monkeypatch.setattr(timing, "_pcc_gc_telemetry", forbidden)
    sink = io.StringIO()
    monkeypatch.setattr(timing.sys, "stderr", sink)
    timer = timing.CoarsePhaseTiming()
    for _ in range(2):
        timer.complete("artifact_link", timer.begin("artifact_link"))
    assert timer.gc_unavailable and timer.gc_starts == {}
    assert all(row["gc_available"] == "0" for row in rows(sink))
    assert not timer.failed


@pytest.mark.parametrize("error_type", (RuntimeError, DiagnosticAbort))
def test_unavailable_counter_disables_only_gc_diagnostics(monkeypatch, counters, error_type):
    reads = []

    def broken(metric):
        reads.append(metric)
        raise error_type("counter unavailable")

    monkeypatch.setattr(timing, "_pcc_gc_telemetry", broken)
    sink = io.StringIO()
    monkeypatch.setattr(timing.sys, "stderr", sink)
    timer = timing.CoarsePhaseTiming()
    for _ in range(2):
        timer.complete("artifact_link", timer.begin("artifact_link"))
    assert len(reads) == 1 and timer.gc_unavailable and not timer.failed
    assert len(rows(sink)) == 4
    assert all(row["gc_available"] == "0" for row in rows(sink))
    assert all("duration_ns" in row for row in rows(sink)[1::2])


@pytest.mark.parametrize("metric,end", ((0, -1), (15, 3), (1, -2147483648)))
def test_wrapped_event_counters_do_not_hide_wide_accounting(counters, metric, end):
    timer = timing.CoarsePhaseTiming()
    timer._gc_begin("artifact_link", 10)
    counters[metric] = end
    counters[5] += 1 << 33
    counters[32] += 1 << 32
    counters[33] += 1 << 34
    row = dict(part.split("=", 1) for part in timer._gc_complete("artifact_link", 10))
    assert row["gc_wide_delta_ok"] == "1" and row["gc_event_delta_ok"] == "0"
    assert row["gc_work_steps_delta"] == str(1 << 33)
    assert row["gc_recorded_pause_count_delta"] == str(1 << 32)
    assert row["gc_recorded_pause_us_delta"] == str(1 << 34)
    assert "gc_allocations_delta" not in row and "gc_relocation_forwards_delta" not in row


@pytest.mark.parametrize("change", ("backend", "token", "missing", "reset"))
def test_unmatched_or_reset_snapshots_never_fabricate_deltas(counters, change):
    timer = timing.CoarsePhaseTiming()
    if change != "missing":
        timer._gc_begin("artifact_link", 10)
    if change == "backend":
        counters["backend"] = 0
    if change == "reset":
        counters[5] = counters[32] = counters[33] = 0
    row = dict(part.split("=", 1) for part in timer._gc_complete(
        "artifact_link", 11 if change == "token" else 10,
    ))
    assert row["gc_wide_delta_ok"] == "0"
    assert "gc_work_steps_delta" not in row and "gc_recorded_pause_us_delta" not in row
    assert timer.gc_starts == {}


def test_snapshot_reads_fixed_scalar_metrics_without_reset_or_inventory(counters):
    assert timing._GC_COUNTER_IDS == (5, 32, 33, 0, 1, 2, 3, 15, 7, 42, 57, 59, 60, 66, 68, 70)
    tree = ast.parse(Path(timing.__file__).read_text())
    native_calls = {node.func.id for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id.startswith("_pcc_gc_")}
    assert native_calls == {"_pcc_gc_backend", "_pcc_gc_telemetry"}
    assert "gc.get_stats" not in Path(timing.__file__).read_text()
    assert timing.CoarsePhaseTiming()._gc_snapshot() == [
        counters["backend"], *(counters[metric] for metric in timing._GC_COUNTER_IDS),
    ]


def test_native_closed_world_exports_new_scalar_fields_and_methods():
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    name = "pcc.backend.indexed_phase_timing"
    _, exports, _ = build_closed_world_context([str(Path(timing.__file__))], [name])
    info = exports[name]["CoarsePhaseTiming"]
    assert set(info["field_names"]) == {"failed", "gc_unavailable", "gc_starts"}
    assert {"__init__", "begin", "complete", "_gc_snapshot", "_gc_begin", "_gc_complete"} <= {method["name"] for method in info["methods"]}
    source = ast.parse(Path(timing.__file__).read_text())
    bindings = {node.target.id: node.value for node in source.body
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES
    for alias, symbol in (("_pcc_gc_backend", "pcc_gc_backend"),
                          ("_pcc_gc_telemetry", "pcc_gc_telemetry")):
        declaration = bindings[alias]
        assert declaration.func.id == "extern" and declaration.args[0].value == symbol
        assert str(RUNTIME_SIGNATURES[symbol][0]) == "i64"



def test_snapshot_native_lowering_calls_owned_runtime_directly(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "phase.ll"
    compile_python(str(Path(timing.__file__)), str(output), emit_llvm_only=True,
                   libpython_mode="off", python_library=True,
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    direct_backend = bool(re.search(r'call i64 (?:\([^\n]*?\) )?@"?pcc_gc_backend"?\(', text))
    direct_telemetry = bool(re.search(r'call i64 (?:\([^\n]*?\) )?@"?pcc_gc_telemetry"?\(', text))
    assert direct_backend, "native module must call the process backend getter directly"
    assert direct_telemetry, "native module must call the i64 telemetry ABI directly"
    imported_host = "@PyImport_ImportModule" in text
    assert not imported_host, "native diagnostic must not import a host module"
    from tests.integration.test_worker_size_prior_closed_world import require_definition

    for method in ("__init__", "_gc_snapshot", "_gc_begin", "_gc_complete", "begin", "complete"):
        require_definition(text, "user_pcc_backend_indexed_phase_timing_CoarsePhaseTiming_" + method)
    require_definition(text, "user_pcc_backend_indexed_phase_timing__write_event")
    bridge_calls = bool(re.search(r'\b(?:call|invoke)\b[^\n]*@"?py_cpy_', text))
    assert not bridge_calls, "native diagnostics must not call a CPython bridge"


def test_normal_ci_selects_bounded_phase_checks_without_new_jobs():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pcc1-package-parity.yml").read_text()
    host_command = next(line for line in workflow.splitlines()
                        if "run: uv run" in line and "test_stage2_export_capture.py" in line)
    for name in ("test_gc_phase_attribution.py", "test_gc_phase_counter_widths.py"):
        path = "tests/python/" + name
        assert path in host_command.split("${{", 1)[0]
        assert workflow.count('      - "' + path + '"') == 2
    assert 'PCC_PY_FRONTEND_WORKER_TIMING: "1"' in workflow
    assert " -n0" not in host_command and "--dist=" not in host_command
