"""Host policy/source tests; synthetic receipts are NOT native execution."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from scripts import ci_native_ordering_gate as gate
from scripts.ci_worker_size_prior_gate import pytest_command
from tests.python import test_native_ordering_semantics as native


ROOT = Path(__file__).resolve().parents[1]


def _receipt_model():
    runtime = {"path": "/model/multi-collector.a", "hashes": {"": "model"}}
    record = {
        "reference": {"returncode": 0, "stdout": "semantic output\n", "stderr": ""},
        "executions": [
            {
                "requested_gc_backend": backend, "observed_gc_backend": backend,
                "runtime_before": copy.deepcopy(runtime),
                "runtime_after": copy.deepcopy(runtime),
                "returncode": 0, "stderr": "",
                "stdout": f"PCC_ORDERING_GC {backend}\nsemantic output\n",
            }
            for backend in range(5)
        ],
    }
    return runtime, record


def test_exact_native_node_uses_existing_guard_and_default_selection(tmp_path):
    assert gate.NODE == (
        "tests/python/test_native_ordering_semantics.py::test_native_ordering_matches_cpython"
    )
    assert gate.TIMEOUT_S == 300
    command = pytest_command(tmp_path, [gate.NODE], integration=False)
    assert "-x" in command and command[-1] == gate.NODE
    assert "-m" not in command[3:]  # Python's own "-m pytest" is not a mark expression.
    assert not any(arg in command for arg in ("-n0", "-n", "--dist", "-o", "--override-ini"))
    tree = ast.parse((ROOT / "scripts/ci_native_ordering_gate.py").read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "run_pytest"
    ]
    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords}
    assert isinstance(keywords["integration"], ast.Constant)
    assert keywords["integration"].value is False
    assert isinstance(keywords["timeout"], ast.Name) and keywords["timeout"].id == "TIMEOUT_S"
    assert not any(
        isinstance(node, ast.Name) and node.id in ("prepare_runtime", "native_pcc1_compiler")
        for node in ast.walk(tree)
    )


def test_workflow_reuses_terminal_stage1_without_hiding_missing_admission():
    text = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    marker = "      - name: Verify native ordering with the existing runtime\n"
    assert text.count(marker) == 1
    before, step = text.split(marker)
    step = step.split("\n      - name:", 1)[0]
    assert "Build Stage1 and run the five-collector native canary" in before
    assert "steps.platform_qualification.outcome == 'success'" in step
    assert "steps.platform_qualification.outcome == 'failure'" in step
    assert "steps.platform_runtime.outputs.archive" in step
    assert "steps.platform_runtime.outputs.ready == 'true'" not in step
    assert "timeout-minutes: 6" in step
    assert 'PCC_TEST_NO_NATIVE_PROVISIONING: "1"' in step
    assert 'PCC_NO_AUTO_PCC1: "1"' in step
    assert "continue-on-error" not in step
    assert "scripts/ci_native_ordering_gate.py --out-dir build/worker-prior-gate" in step
    assert "--phase stage1" not in step and "owned_runtime_build" not in step
    for name in (
        "scripts/ci_native_ordering_gate.py",
        "tests/test_ci_native_ordering_gate.py",
        "tests/python/test_native_ordering_semantics.py",
        "tests/python/test_user_ordering_exhaustion.py",
    ):
        assert text.count(f'      - "{name}"') == 2


def test_source_fingerprints_include_the_changed_owners_and_probe():
    assert set(native.SOURCE_INPUTS) <= set(gate.INPUTS)
    assert set(native._source_fingerprints()) == set(native.SOURCE_INPUTS)
    assert native.NATIVE_PROGRAM == native.COLLECTOR_PROBE + "\n" + native.PROGRAM
    assert 'extern("pcc_gc_backend", (), c_int64)' in native.COLLECTOR_PROBE
    assert "pcc_gc_set_backend" not in native.COLLECTOR_PROBE
    source = (ROOT / "tests/python/test_native_ordering_semantics.py").read_text()
    tree = ast.parse(source)
    test = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "test_native_ordering_matches_cpython"
    )
    assert test.decorator_list == []  # The selected peer fixture is not integration-marked.
    collector_loop = next(
        node for node in ast.walk(test)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name)
        and node.target.id == "backend"
    )
    calls = [
        node.func.id for node in ast.walk(collector_loop)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("runtime_identity") == 2
    assert "_verify_collector_output" in calls


@pytest.mark.parametrize("backend", range(5))
def test_collector_output_preserves_every_semantic_line(backend):
    expected = "first\n\nlast\n"
    assert native._verify_collector_output(
        f"PCC_ORDERING_GC {backend}\n" + expected, backend, expected,
    ) == backend


@pytest.mark.parametrize("stdout", [
    "semantic output\n",
    "PCC_ORDERING_GC 0\nsemantic output\n",
    "PCC_ORDERING_GC 1\nwrong\n",
    "PCC_ORDERING_GC 1\nsemantic output\nextra\n",
    "PCC_ORDERING_GC 1",
])
def test_invalid_collector_or_semantic_output_fails_closed(stdout):
    with pytest.raises(AssertionError):
        native._verify_collector_output(stdout, 1, "semantic output\n")


def test_synthetic_receipt_requires_all_five_observed_backends():
    runtime, record = _receipt_model()
    assert len(gate.verify_execution_rows(record, runtime)) == 5


@pytest.mark.parametrize("corruption", [
    "missing", "duplicate", "gc0-only", "stale-before", "stale-after",
    "exit", "stderr", "semantic", "observed-bool",
])
def test_synthetic_bad_native_receipt_is_rejected(corruption):
    runtime, record = _receipt_model()
    rows = record["executions"]
    if corruption == "missing":
        rows.pop()
    elif corruption == "duplicate":
        rows[-1]["requested_gc_backend"] = 3
    elif corruption == "gc0-only":
        for row in rows:
            row["observed_gc_backend"] = 0
            row["stdout"] = "PCC_ORDERING_GC 0\nsemantic output\n"
    elif corruption in ("stale-before", "stale-after"):
        key = "runtime_" + corruption.removeprefix("stale-")
        rows[2][key]["hashes"][""] = "different"
    elif corruption == "exit":
        rows[2]["returncode"] = 1
    elif corruption == "stderr":
        rows[2]["stderr"] = "unexpected diagnostic"
    elif corruption == "semantic":
        rows[2]["stdout"] += "unexpected semantic output\n"
    else:
        rows[1]["observed_gc_backend"] = True
    with pytest.raises(AssertionError):
        gate.verify_execution_rows(record, runtime)


def test_missing_runtime_is_a_gate_failure_not_provisioning_or_skip(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    with pytest.raises(AssertionError, match="admitted Stage1 runtime"):
        gate.run(tmp_path)
    record = json.loads((tmp_path / "native-ordering.json").read_text())
    assert record["status"] == "FAIL"
    assert "program" not in record
    with pytest.raises(AssertionError, match="admitted source-matched runtime"):
        native.test_native_ordering_matches_cpython(tmp_path)
