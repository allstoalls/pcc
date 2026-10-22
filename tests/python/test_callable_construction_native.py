"""Unchanged constructor fixtures compiled by host pcc0, then run natively.

Explicit source/config/target/codegen-matched threaded/atomic runtime required.
There is no provisioning or pcc1 claim. Select with -m integration -n0 -x;
compilation is once per source per worker, with separate GC0..4 execution nodes.
The three fixtures without explicit collection remain default-only evidence.
Artifacts live under pytest's constructor-native directory; retain --basetemp.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.process_timeout import run_process_group_timeout
from tests.python.constructor_native_support import _same_source_imports


pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests/fixtures/native/constructor"
_DEFAULT_CASES = ("core", "semantics", "exception_order")
_GC_CASES = (
    "capture_lifetime", "default_lifetime", "kwonly_lifetime",
    "generator_lifetime", "lifetime", "expression_failures", "default_exception",
)
_HELPER = "tests.python.constructor_native_support"
# exec keeps the native child in the timeout helper's process group. Raw files
# preserve bytes; the timeout helper's decoded/synthetic output is kept separately.
_RAW_CAPTURE = """
import os, sys
for fd, path in ((1, sys.argv[1]), (2, sys.argv[2])):
    with open(path, 'xb', buffering=0) as stream:
        os.dup2(stream.fileno(), fd)
os.execve(sys.argv[3], sys.argv[3:], dict(os.environ))
"""


def _save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _environment(archive):
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    cooperative = {
        "PCC_WORKER_TREE_BUDGET_BYTES", "PCC_WORKER_TREE_STATE_PATH",
        "PCC_WORKER_RESOURCE_REPORT", "PCC_WORKER_RESOURCE_TOKEN",
        "PCC_OUTER_PARALLELISM",
    }
    for name in tuple(environment):
        if name.startswith("PCC_") and name not in cooperative:
            del environment[name]
    environment.update(
        PCC_RUNTIME_ARCHIVE=str(archive), PCC_WITH_THREADS="1",
        PCC_REFCOUNT_KIND="atomic", PCC_SOURCE_ROOT=str(_ROOT),
        PCC_TEST_NO_NATIVE_PROVISIONING="1", PCC_NO_AUTO_PCC1="1",
        PCC_RUNTIME_CC="pcc", PCC_RUNTIME_HIGH="py", PCC_SELF_LINK="pcc",
        PCC_SELF_OBJ="pcc", PCC_IR_TO_OBJ_EMITTER="pcc",
        PCC_PY_FRONTEND_JOBS="1", PCC_PY_FRONTEND_SUMMARY_JOBS="1",
        PCC_SELF_BACKEND_JOBS="1", PCC_MACHO_LINK_JOBS="1",
        PCC_DISABLE_PY_RUN_CACHE="1", PCC_PY_FRONTEND_IR_CACHE="0",
        PCC_SELF_BACKEND_OBJECT_CACHE="0", PYTHONDONTWRITEBYTECODE="1",
        PCC_HOST_PYTHON="/nonexistent/host-python",
        PCC_HOST_PCC="/nonexistent/host-pcc", PATH="",
    )
    return environment


def _run(directory, label, command, environment, timeout):
    stdout = directory / (label + ".stdout")
    stderr = directory / (label + ".stderr")
    launch = [sys.executable, "-E", "-B", "-u", "-c", _RAW_CAPTURE,
              str(stdout), str(stderr), *command]
    record = {"status": "RUNNING", "command": command, "launcher": launch,
              "stdout_path": str(stdout), "stderr_path": str(stderr)}
    record_path = directory / (label + ".process.json")
    _save(record_path, record)
    try:
        result = run_process_group_timeout(
            launch, env=environment, cwd=_ROOT, timeout=timeout,
        )
    except BaseException as error:
        record.update(status="SUPERVISOR_FAILED", error=repr(error))
        _save(record_path, record)
        raise
    (directory / (label + ".supervisor.stdout")).write_text(result.stdout, encoding="utf-8")
    (directory / (label + ".supervisor.stderr")).write_text(result.stderr, encoding="utf-8")
    record.update(status="TIMEOUT" if result.returncode == 124 else "FINISHED",
                  returncode=result.returncode)
    for stream in (stdout, stderr):
        if stream.is_file():
            record[stream.suffix[1:] + "_sha256"] = _sha(stream)
    _save(record_path, record)
    return record


@pytest.fixture(scope="module")
def constructor_artifacts(tmp_path_factory):
    directory = tmp_path_factory.mktemp("constructor-native")
    # Predeclare every native attempt as UNRUN. -x, setup errors and interrupts
    # cannot accidentally turn the remaining collector cases into passes.
    for name in _DEFAULT_CASES + _GC_CASES:
        case_dir = directory / name
        case_dir.mkdir()
        requested = (None,) if name in _DEFAULT_CASES else tuple(range(5))
        _save(case_dir / "receipt.json", {
            "schema": "pcc.constructor-native.v1", "fixture": name,
            "compiler": "host-pcc0", "native_pcc1": "UNRUN",
            "source": str(_FIXTURES / ("constructor_" + name + ".py")),
            "reference": {"status": "UNRUN"}, "compile": {"status": "UNRUN"},
            "executions": [{"requested_backend": value, "status": "UNRUN",
                            "observed_backends": []} for value in requested],
        })
    _save(directory / "parent-imports.json", _same_source_imports(_ROOT))
    return directory


@pytest.fixture(scope="module")
def constructor_program(request, constructor_artifacts):
    name = request.param
    source = _FIXTURES / ("constructor_" + name + ".py")
    directory = constructor_artifacts / name
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    binary = directory / ("program.exe" if os.name == "nt" else "program")
    try:
        assert not os.environ.get("PCC_TEST_COMPILER"), (
            "This audited wrapper selects host pcc0; unset PCC_TEST_COMPILER"
        )
        selected = os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE", "").strip()
        assert selected, "Set PCC_THREADED_RUNTIME_ARCHIVE to a matched threaded/atomic archive"
        archive = Path(selected).resolve(strict=True)
        assert source.absolute() == source.resolve(strict=True), "Do not relocate/symlink the fixture"
        receipt.update(source_sha256=_sha(source), runtime_archive=str(archive),
                       runtime_sha256=_sha(archive), wrapper_sha256=_sha(Path(__file__)))
        environment = _environment(archive)
        receipt["effective_pcc_environment"] = {
            key: value for key, value in environment.items() if key.startswith("PCC_")
        }
        receipt["compile"] = {"status": "RUNNING"}
        _save(receipt_path, receipt)
        compile_result = _run(directory, "compile", [
            sys.executable, "-E", "-B", "-u", "-m", _HELPER,
            str(source), str(binary), str(archive), str(directory),
            "1" if name in ("generator_lifetime", "lifetime") else "0",
        ], environment, 300)
        worker_path = directory / "compile-receipt.json"
        worker = json.loads(worker_path.read_text(encoding="utf-8")) if worker_path.is_file() else {}
        receipt["compile"] = dict(compile_result, worker=worker)
        receipt["compile"]["status"] = (
            "READY" if compile_result["returncode"] == 0 and worker.get("status") == "READY"
            else "TIMEOUT" if compile_result["returncode"] == 124
            else worker["status"] if worker.get("status", "").endswith("_FAILED")
            else "COMPILE_PROCESS_FAILED"
        )
        _save(receipt_path, receipt)
        assert receipt["compile"]["status"] == "READY", receipt["compile"]
        receipt["binary_sha256"] = _sha(binary)
        receipt["reference"] = _run(directory, "reference", [
            sys.executable, "-E", "-B", str(source),
        ], environment, 30)
        expected = ("CONSTRUCTOR_" + name.upper() + "_OK\n").encode()
        assert receipt["reference"]["returncode"] == 0, receipt["reference"]
        assert (directory / "reference.stdout").read_bytes() == expected
        assert (directory / "reference.stderr").read_bytes() == b""
        receipt["reference"]["status"] = "PASS"
        _save(receipt_path, receipt)
        return source, binary, archive, directory, environment
    except BaseException as error:
        receipt["setup_error"] = repr(error)
        if receipt["compile"]["status"] == "UNRUN":
            receipt["compile"]["status"] = "ADMISSION_FAILED"
        elif receipt["compile"]["status"] == "RUNNING":
            receipt["compile"]["status"] = "COMPILE_SUPERVISOR_FAILED"
        elif receipt["compile"]["status"] == "READY":
            receipt["reference"]["status"] = "REFERENCE_FAILED"
        for execution in receipt["executions"]:
            execution["blocked_by"] = "fixture setup failed; see compile/reference evidence"
        _save(receipt_path, receipt)
        raise


def _assert_integrity(source, binary, archive, receipt):
    assert _sha(source) == receipt["source_sha256"], "Source drift"
    assert _sha(archive) == receipt["runtime_sha256"], "Runtime drift"
    assert _sha(binary) == receipt["binary_sha256"], "Binary drift"
    worker = receipt["compile"]["worker"]
    for suffix, key in ((".provenance.json", "runtime_provenance_sha256"),
                        (".capi_syms", "runtime_capi_sha256")):
        assert _sha(Path(str(archive) + suffix)) == worker[key], "Runtime sidecar drift"
    for row in worker["link_inputs"]:
        assert _sha(Path(row["path"])) == row["sha256"], "Retained linker IR drift"
    _same_source_imports(_ROOT)


def _assert_execution(program, collector):
    source, binary, archive, directory, environment = program
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    execution = next(row for row in receipt["executions"] if row["requested_backend"] == collector)
    label = "default" if collector is None else "gc" + str(collector)
    log = directory / (label + ".gc.jsonl")
    environment = dict(environment, PCC_LOG="gc", PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log))
    if collector is not None:
        environment["PCC_GC_BACKEND"] = str(collector)
    execution.update(status="RUNNING", gc_log=str(log))
    _save(receipt_path, receipt)
    try:
        _assert_integrity(source, binary, archive, receipt)
        result = _run(directory, label, [str(binary)], environment, 60)
        execution.update(result, process_status=result["status"])
        events = parse_log_lines(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else []
        collections = [event for event in events if event.fields.get("category") == "gc"
                       and event.event in ("collect_start", "collect_stop", "collect_end")]
        observed = {event.fields.get("value1") for event in collections}
        starts = {event.fields.get("value1") for event in collections if event.event == "collect_start"}
        stops = {event.fields.get("value1") for event in collections if event.event in ("collect_stop", "collect_end")}
        execution["observed_starts"] = sorted(starts, key=repr)
        execution["observed_stops"] = sorted(stops, key=repr)
        execution["observed_backends"] = sorted(observed, key=repr)
        execution["gc_log_sha256"] = _sha(log) if log.is_file() else None
        execution["collection_witness_required"] = collector is not None
        _save(receipt_path, receipt)
        assert result["returncode"] == 0, execution
        assert (directory / (label + ".stdout")).read_bytes() == (directory / "reference.stdout").read_bytes(), execution
        assert (directory / (label + ".stderr")).read_bytes() == (directory / "reference.stderr").read_bytes(), execution
        if collector is not None:
            assert all(type(value) is int for value in observed), execution
            assert observed == starts == stops == {collector}, execution
        _assert_integrity(source, binary, archive, receipt)
        execution["status"] = "PASS" if collector is not None else "PASS_DEFAULT_ONLY"
    except BaseException as error:
        status = "TIMEOUT" if execution.get("returncode") == 124 else "FAILED"
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            status = "INTERRUPTED"
        execution.update(status=status, error=repr(error))
        raise
    finally:
        _save(receipt_path, receipt)


@pytest.mark.parametrize("constructor_program", _DEFAULT_CASES, indirect=True, scope="module")
def test_constructor_default(constructor_program):
    _assert_execution(constructor_program, None)


@pytest.mark.parametrize("constructor_program", _GC_CASES, indirect=True, scope="module")
@pytest.mark.parametrize("collector", range(5), ids=lambda value: "gc" + str(value))
def test_constructor_observed_gc(constructor_program, collector):
    _assert_execution(constructor_program, collector)
