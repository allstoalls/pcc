"""Host-only controls for the existing root-join harness's diagnostic receipts.

All compiler and process calls are simulated. No native image is executed and
no runtime is built; the placeholder magic only exercises existing admission.
"""

from __future__ import annotations

import gzip
import itertools
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline
from scripts import ci_macos_regression_gate as gate
from tests.python import conftest as python_fixtures
from tests.python import owned_regression_support as support
from tests.python import process_timeout
from tests.python import test_valueclass_aggregate_ownership_native as harness


_ROOT_CASES = (
    "typed-list-unbox", "typed-loop-unbox", "typed-comprehension-unbox",
    "original-direct-payload", "original-valuebox",
)
_PROFILE = {
    "events": [
        {"name": "collect_relative_module_closure", "ms": 2},
        {"name": "type_infer", "ms": 3, "detail": "program"},
        {"name": "codegen", "ms": 5, "detail": "program"},
        {"name": "link", "ms": 7},
    ],
    "phase_totals_ms": {
        "collect_relative_module_closure": 2, "type_infer": 3,
        "codegen": 5, "link": 7,
    },
    "counters": {"multi_files": 4},
}


@pytest.fixture
def simulation(tmp_path, monkeypatch):
    directory = tmp_path / "work/root-joins/tests/case"
    directory.mkdir(parents=True)
    archive = tmp_path / "runtime.a"
    archive.write_bytes(b"host-only archive placeholder")
    Path(str(archive) + ".provenance.json").write_text("{}")
    native_compiler = tmp_path / "pcc1"
    native_compiler.write_bytes(b"\x7fELFhost-only compiler placeholder")
    state = SimpleNamespace(
        directory=directory, mode="pcc0", case_name=_ROOT_CASES[0],
        compile_error=None, invalid_profile=False, process_error=None,
        error_label=None, returncodes={}, wrong_collector=None,
        compile_calls=[], execution_calls=[], captures=[], profile=None,
    )
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("PCC_TEST_COMPILER", raising=False)
    monkeypatch.delenv("PCC_GC_BACKEND", raising=False)
    monkeypatch.setattr(harness, "codegen_checksum", lambda: "host-only-codegen")
    compile_ticks = iter((100, 160))
    execution_ticks = itertools.count(1000, 10)
    monkeypatch.setattr(harness, "time", SimpleNamespace(
        monotonic_ns=lambda: next(compile_ticks),
    ), raising=False)
    monkeypatch.setattr(support, "time", SimpleNamespace(
        monotonic_ns=lambda: next(execution_ticks),
    ), raising=False)

    def compile_program(source, output, *, backend, libpython_mode,
                        runtime_archive, ir_scaffold_mode="on", profile=None):
        state.compile_calls.append((source, output))
        assert (backend, libpython_mode, ir_scaffold_mode, runtime_archive) == (
            "self", "off", "on", str(archive),
        )
        state.profile = profile
        if profile is not None:
            profile.update(json.loads(json.dumps(_PROFILE)))
            if state.invalid_profile:
                profile["invalid"] = object()
        if state.compile_error is not None:
            raise state.compile_error
        Path(output).write_bytes(b"\x7fELFhost-only program placeholder")

    compile_program.__module__ = "pcc.frontends.python.pipeline"
    monkeypatch.setattr(pipeline, "compile_python", compile_program)

    def native_compile(command, *, env, timeout):
        assert timeout == 300
        assert command[0] == str(native_compiler)
        assert env["PCC_NO_AUTO_PCC1"] == "1"
        compile_program(
            command[-3], command[-1], backend="self", libpython_mode="off",
            runtime_archive=str(archive),
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(process_timeout, "run_process_group_timeout", native_compile)

    def execute(command, *, env, timeout):
        assert timeout == 10 and "LC_ALL" not in env
        backend = env.get("PCC_GC_BACKEND")
        label = "reference" if backend is None else "gc" + backend
        state.execution_calls.append((label, command, env))
        if backend is not None:
            assert command == [str(directory / "program.out")]
            assert env["PATH"] == ""
            assert env["PCC_HOST_PYTHON"] == "/nonexistent/host-python"
            assert env["PCC_HOST_PCC"] == "/nonexistent/host-pcc"
            observed = 99 if state.wrong_collector == int(backend) else int(backend)
            Path(env["PCC_LOG_FILE"]).write_text(json.dumps({
                "category": "gc", "event": "collect_start", "value1": observed,
            }) + "\n")
        else:
            assert command == [sys.executable, str(directory / "program.py")]
        if state.error_label == label:
            raise state.process_error
        case = next(case for case in harness.CASES if case.name == state.case_name)
        return subprocess.CompletedProcess(
            command, state.returncodes.get(label, 0),
            stdout=case.expected_stdout, stderr=case.required_stderr,
        )

    monkeypatch.setattr(support, "run_process_group_timeout", execute)

    def capture():
        state.captures.append(True)
        return SimpleNamespace(out="compiler output\n", err="compiler diagnostic\n")

    def invoke():
        requested_mode = "pcc1" if state.mode == "pcc1" else "pcc0"
        request = SimpleNamespace(
            node=SimpleNamespace(callspec=SimpleNamespace(params={
                "python_program_compiler": requested_mode,
            })),
            param=requested_mode,
            getfixturevalue=lambda name: native_compiler,
        )
        compiler = python_fixtures.python_program_compiler.__wrapped__(request)
        if state.mode in ("pcc1-routed", "unrecognized-pcc0"):
            # Match the native fixture's strict signature: profile is forbidden.
            def strict_compiler(source, output, *, backend, libpython_mode,
                                runtime_archive, ir_scaffold_mode="on"):
                return compile_program(
                    source, output, backend=backend, libpython_mode=libpython_mode,
                    runtime_archive=runtime_archive, ir_scaffold_mode=ir_scaffold_mode,
                )

            if state.mode == "pcc1-routed":
                strict_compiler.__module__ = "tests.pcc1_route"
                monkeypatch.setenv("PCC_TEST_COMPILER", str(native_compiler))
            compiler = strict_compiler
        case = next(case for case in harness.CASES if case.name == state.case_name)
        return harness.test_aggregate_ownership_native_five_gc(
            case, compiler, request, archive, directory,
            SimpleNamespace(readouterr=capture),
        )

    state.invoke = invoke
    state.receipt = lambda: json.loads((directory / "ownership-regression.json").read_text())
    return state


def _assert_timing(record, *, start=None, elapsed=None):
    assert all(type(record[key]) is int for key in ("start_ns", "end_ns", "elapsed_ns"))
    assert record["end_ns"] >= record["start_ns"]
    assert record["elapsed_ns"] == record["end_ns"] - record["start_ns"]
    if start is not None:
        assert record["start_ns"] == start
    if elapsed is not None:
        assert record["elapsed_ns"] == elapsed


@pytest.mark.parametrize("case_name", _ROOT_CASES)
def test_host_profile_and_each_existing_execution_are_recorded(simulation, case_name):
    simulation.case_name = case_name
    assert simulation.invoke() is None
    record = simulation.receipt()
    assert record["status"] == "PASS" and record["compiler_mode"] == "pcc0"
    assert record["compile_profile"] == _PROFILE == simulation.profile
    _assert_timing(record["compile_timing"], start=100, elapsed=60)
    assert len(simulation.compile_calls) == len(simulation.captures) == 1
    assert len(simulation.execution_calls) == 6
    for index, label in enumerate(("reference", *(f"gc{gc}" for gc in range(5)))):
        row = json.loads((simulation.directory / (label + ".json")).read_text())
        _assert_timing(row, start=1000 + index * 20, elapsed=10)
        assert row["returncode"] == 0
        if index:
            assert row["status"] == "PASS" and row["observed_gc"] == [index - 1]
            assert record["executions"][index - 1] == row
        else:
            assert record["reference"] == row


@pytest.mark.parametrize("mode", ("pcc1", "pcc1-routed", "unrecognized-pcc0"))
def test_non_host_compilers_receive_only_original_keywords(simulation, mode):
    simulation.mode = mode
    assert simulation.invoke() is None
    record = simulation.receipt()
    assert record["status"] == "PASS"
    assert "compile_profile" not in record and simulation.profile is None
    _assert_timing(record["compile_timing"], start=100, elapsed=60)
    assert len(simulation.compile_calls) == len(simulation.captures) == 1
    assert len(simulation.execution_calls) == 6


@pytest.mark.parametrize("error", (
    RuntimeError("original compiler failure"),
    subprocess.TimeoutExpired(["compiler"], 300),
), ids=("exception", "timeout"))
def test_compile_error_is_not_replaced_and_capture_still_runs(simulation, error):
    simulation.compile_error = error
    with pytest.raises(type(error)) as raised:
        simulation.invoke()
    assert raised.value is error
    record = simulation.receipt()
    assert record["status"] == "COMPILE_FAILED"
    assert record["compile_profile"] == _PROFILE
    _assert_timing(record["compile_timing"], start=100, elapsed=60)
    assert len(simulation.compile_calls) == len(simulation.captures) == 1
    assert [row[0] for row in simulation.execution_calls] == ["reference"]
    assert (simulation.directory / "compiler-wrapper.stdout").read_text() == "compiler output\n"
    assert (simulation.directory / "compiler-wrapper.stderr").read_text() == "compiler diagnostic\n"


@pytest.mark.parametrize("failure", (False, True), ids=("success", "compile-error"))
def test_unserializable_profile_is_explicit_diagnostic_loss(simulation, failure):
    simulation.invalid_profile = True
    error = RuntimeError("original compiler failure")
    if failure:
        simulation.compile_error = error
        with pytest.raises(RuntimeError) as raised:
            simulation.invoke()
        assert raised.value is error
    else:
        assert simulation.invoke() is None
    record = simulation.receipt()
    assert record["status"] == ("COMPILE_FAILED" if failure else "PASS")
    assert "compile_profile" not in record
    assert record["compile_profile_error"].startswith("TypeError:")
    _assert_timing(record["compile_timing"], start=100, elapsed=60)
    assert len(simulation.compile_calls) == len(simulation.captures) == 1


@pytest.mark.parametrize("label", ("reference", "gc2"))
def test_timeout_returncode_is_preserved_as_failure(simulation, label):
    simulation.returncodes[label] = 124
    with pytest.raises(AssertionError):
        simulation.invoke()
    record = simulation.receipt()
    assert record["status"] == ("REFERENCE_FAILED" if label == "reference" else "FAIL")
    row = json.loads((simulation.directory / (label + ".json")).read_text())
    assert row["returncode"] == 124
    _assert_timing(row, elapsed=10)
    if label == "reference":
        assert not simulation.compile_calls
    else:
        assert len(record["executions"]) == 5
        assert [row["status"] for row in record["executions"]] == ["PASS", "PASS", "FAIL", "PASS", "PASS"]


@pytest.mark.parametrize("label", ("reference", "gc2"))
def test_execution_exception_retains_the_existing_failure_path(simulation, label):
    error = subprocess.TimeoutExpired(["program"], 10, output=b"partial\n", stderr=b"timeout\n")
    simulation.error_label = label
    simulation.process_error = error
    expected_error = subprocess.TimeoutExpired if label == "reference" else AssertionError
    with pytest.raises(expected_error) as raised:
        simulation.invoke()
    record = simulation.receipt()
    if label == "reference":
        assert raised.value is error
        assert record["status"] == "REFERENCE_FAILED" and not simulation.compile_calls
        assert not (simulation.directory / "reference.json").exists()
    else:
        assert record["status"] == "FAIL" and len(record["executions"]) == 5
        row = record["executions"][2]
        assert row["status"] == "FAIL" and row["returncode"] is None
        assert row["stdout"] == "partial\n" and row["stderr"] == "timeout\n"
        assert "start_ns" not in row  # No completed execution result was returned.


def test_actual_collector_witness_is_still_required(simulation):
    simulation.wrong_collector = 2
    with pytest.raises(AssertionError):
        simulation.invoke()
    record = simulation.receipt()
    assert record["status"] == "FAIL" and len(record["executions"]) == 5
    assert record["executions"][2]["observed_gc"] == [99]
    assert record["executions"][2]["status"] == "FAIL"


def test_existing_evidence_allowlist_preserves_timing_and_profile(simulation, tmp_path):
    simulation.invoke()
    gate.collect_evidence(tmp_path)
    for name in ("ownership-regression.json", "reference.json", *(f"gc{gc}.json" for gc in range(5))):
        assert name in gate.RECEIPT_NAMES
        saved = tmp_path / "evidence/root-joins/tests/case" / (name + ".gz")
        assert gzip.decompress(saved.read_bytes()) == (simulation.directory / name).read_bytes()
    index = json.loads((tmp_path / "evidence/index.json").read_text())
    assert index["status"] == "COMPLETE"
    assert not any(row["source"].endswith("program.out") for row in index["files"])
