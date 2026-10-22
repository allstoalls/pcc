from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ci_macos_regression_gate", ROOT / "scripts/ci_macos_regression_gate.py",
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _reports(nodes):
    rows = [
        {"event": "start", "collect_only": False},
        {"event": "collected", "nodeids": nodes},
    ]
    rows += [{"event": "report", "nodeid": node, "when": when, "outcome": "passed"}
             for node in nodes for when in ("setup", "call", "teardown")]
    rows.append({"event": "finish", "exitstatus": 0,
                 "testscollected": len(nodes), "testsfailed": 0})
    return rows


def _write_reports(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_exact_passed_nodes_are_execution_evidence(tmp_path):
    nodes = ["sample.py::test_one", "sample.py::test_two[pcc0]"]
    path = tmp_path / "live.jsonl"
    _write_reports(path, _reports(nodes))
    assert gate.verify_execution_report(path, nodes) == nodes


@pytest.mark.parametrize("mutation", [
    "collect-only", "deselected", "missing-node", "skipped", "xfail",
    "no-call", "no-teardown", "no-finish", "failed-exit", "duplicate-call",
])
def test_incomplete_or_nonexecuted_gate_cannot_pass(tmp_path, mutation):
    nodes = ["sample.py::test_one"]
    rows = _reports(nodes)
    if mutation == "collect-only":
        rows[0]["collect_only"] = True
    elif mutation == "deselected":
        rows.insert(1, {"event": "deselected", "nodeids": nodes})
    elif mutation == "missing-node":
        rows[1]["nodeids"] = []
    elif mutation == "skipped":
        rows[3]["outcome"] = "skipped"
    elif mutation == "xfail":
        rows[3]["wasxfail"] = "expected failure"
    elif mutation == "no-call":
        del rows[3]
    elif mutation == "no-teardown":
        del rows[4]
    elif mutation == "no-finish":
        rows.pop()
    elif mutation == "failed-exit":
        rows[-1]["exitstatus"] = 1
    else:
        rows.insert(4, dict(rows[3]))
    path = tmp_path / "live.jsonl"
    _write_reports(path, rows)
    with pytest.raises(AssertionError):
        gate.verify_execution_report(path, nodes)


def test_receipts_are_complete_compressed_and_exclude_native_artifacts(tmp_path):
    work = tmp_path / "work" / "root-joins"
    work.mkdir(parents=True)
    original = "an exact failure traceback\n" * 100
    (work / "target.stderr").write_text(original)
    (work / "junit.xml").write_text("<testsuites />")
    (work / "program.out").write_bytes(b"native binary")
    (work / "program.ll").write_text("compiler IR")
    (work / "program.codegen.json").write_text("large compiler intermediate")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "libpy_runtime_pcc_py.a").write_bytes(b"archive")
    gate.collect_evidence(tmp_path)
    evidence = tmp_path / "evidence"
    assert gzip.decompress((evidence / "root-joins/target.stderr.gz").read_bytes()).decode() == original
    index = json.loads((evidence / "index.json").read_text())
    assert index["status"] == "COMPLETE"
    assert {row["source"] for row in index["files"]} == {
        "root-joins/target.stderr", "root-joins/junit.xml",
    }


def test_evidence_limit_is_explicit_and_fails_closed(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "target.stderr").write_text("failure must not be silently truncated")
    monkeypatch.setattr(gate, "EVIDENCE_LIMIT", 1)
    with pytest.raises(AssertionError, match="Evidence exceeded"):
        gate.collect_evidence(tmp_path)
    index = json.loads((tmp_path / "evidence/index.json").read_text())
    assert index["status"] == "LIMIT_EXCEEDED"
    assert index["files"][0]["status"] == "OMITTED_EVIDENCE_LIMIT"
    assert index["files"][0]["sha256"] == gate._sha256(work / "target.stderr")
    assert not list((tmp_path / "evidence").rglob("*.gz"))


def test_pytest_command_clears_marker_defaults_and_disables_xdist(tmp_path, monkeypatch):
    seen = []

    def command(arguments, directory, environment):
        seen.append(arguments)
        directory.mkdir(parents=True)
        _write_reports(directory / "live.jsonl", _reports(["test.py::test_native[pcc0]"]))
        return 0

    monkeypatch.setattr(gate, "_command", command)
    assert gate._pytest_gate("smoke", ["test.py::test_native[pcc0]"], tmp_path, {})
    args = seen[0]
    assert args[args.index("-o") + 1] == "addopts="
    assert "-x" in args and "-n0" in args
    assert "--collect-only" not in args and "-m" not in args[3:]


def _workflow_job(workflow, name):
    body = workflow.split("  " + name + ":\n", 1)[1]
    next_job = re.search(r"(?m)^  [A-Za-z0-9_-]+:\n", body)
    return body[:next_job.start()] if next_job else body


def test_workflow_keeps_stage1_configuration_and_evidence_separate():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    smoke = _workflow_job(workflow, "macos-pcc0-regressions")
    stage1 = _workflow_job(workflow, "pcc1-package-parity")
    assert "runs-on: macos-15" in smoke
    assert "timeout-minutes: 25" in smoke and "--timeout 1200" in smoke
    assert "--auto-tree-rss-ceiling-bytes 4294967296" in smoke
    assert "--min-tree-rss-bytes 2147483648" in smoke
    assert "--darwin-preflight-reserve-bytes 536870912" in smoke
    assert "path: build/macos-regressions/evidence/" in smoke
    assert "actions/download-artifact" not in smoke + stage1
    assert "timeout-minutes: 200" in stage1
    assert 'PCC_BOOTSTRAP_STAGE_TIMEOUT: "7200"' in stage1
    assert 'PCC_BOOTSTRAP_AUTO_TREE_RSS_CEILING_BYTES: "4294967296"' in stage1
    assert 'PCC_BOOTSTRAP_MIN_TREE_RSS_BYTES: "2147483648"' in stage1
    assert 'PCC_BOOTSTRAP_HOST_MEMORY_RESERVE_BYTES: "536870912"' in stage1
    assert "PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES:" not in stage1
    assert "name: Preserve macOS bootstrap evidence\n        if: always()" in stage1
    assert "PCC_WITH_THREADS" not in stage1
    assert "ci_macos_regression_gate" not in stage1
    assert "needs:" not in smoke
    assert "--profile float-doc" not in smoke + stage1
    assert "build/macos-float-doc" not in smoke + stage1
    for path in (
        "tests/python/test_os_error_constructors.py",
        "tests/python/test_native_open_errno.py",
        "tests/python/test_native_open_pathlike.py",
        "tests/python/test_owned_fdopen_provider.py",
    ):
        assert workflow.count('      - "' + path + '"') == 2


def test_oserror_gate_names_all_pcc0_native_programs_in_default_profile():
    assert "oserror" in gate.PROFILES["default"]
    assert dict(gate.GATES)["oserror"] == (
        "tests/python/test_os_error_constructors.py::"
        "test_os_error_constructor_native_five_gc[pcc0]",
        "tests/python/test_native_open_errno.py::"
        "test_open_errno_native_five_gc[pcc0]",
        "tests/python/test_native_open_pathlike.py::"
        "test_native_open_accepts_pathlike_like_cpython",
    )


def test_oserror_gate_rejects_missing_pathlike_execution(tmp_path):
    nodes = dict(gate.GATES)["oserror"]
    path = tmp_path / "live.jsonl"
    _write_reports(path, _reports(list(nodes[:-1])))
    with pytest.raises(AssertionError):
        gate.verify_execution_report(path, nodes)


def test_root_join_gate_names_the_five_original_pcc0_witnesses():
    nodes = dict(gate.GATES)["root-joins"]
    assert nodes == tuple(gate.OWNERSHIP_NODE + "[pcc0-" + name + "]" for name in (
        "typed-list-unbox", "typed-loop-unbox", "typed-comprehension-unbox",
        "original-direct-payload", "original-valuebox",
    ))


def test_failed_gate_stops_before_runtime_build_and_marks_remaining_unrun(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "darwin")
    monkeypatch.setattr(gate.platform, "machine", lambda: "arm64")
    monkeypatch.delenv("PCC_TEST_COMPILER", raising=False)
    calls = []

    def fail(name, nodes, out, environment):
        calls.append(name)
        raise AssertionError("original clock failure")

    monkeypatch.setattr(gate, "_pytest_gate", fail)
    with pytest.raises(AssertionError, match="original clock failure"):
        gate.run(tmp_path)
    assert calls == ["clock"]
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["status"] == "FAIL"
    assert receipt["profile"] == "default"
    assert receipt["scope"] == "host pcc0 self/off emitted execution; Stage1 is a separate job"
    assert receipt["clock_scope"] == "real host/native clock epoch; simulated suspend; emitted native worker-state reader"
    assert set(receipt["gates"]) == set(gate.PROFILES["default"])
    assert receipt["gates"]["clock"]["status"] == "FAIL"
    assert all(row["status"] == "NOT_RUN" for name, row in receipt["gates"].items() if name != "clock")
    assert receipt["gates"]["root-joins"]["expected_nodes"] == list(dict(gate.GATES)["root-joins"])
    assert receipt["gates"]["oserror"]["expected_nodes"] == list(dict(gate.GATES)["oserror"])


def test_command_receipt_and_native_child_receive_the_same_guard_budget(tmp_path, monkeypatch):
    environment = {
        "PCC_WORKER_TREE_BUDGET_BYTES": str(5 * 1024**3 // 2),
        "PCC_WORKER_TREE_STATE_PATH": str(tmp_path / "supervisor.worker-rss.tsv"),
    }
    seen = []

    def child(command, **kwargs):
        seen.append(kwargs["env"])
        return gate.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.subprocess, "run", child)
    assert gate._command(["native-worker"], tmp_path / "native", environment) == 0
    receipt = json.loads((tmp_path / "native/command.json").read_text())
    assert seen == [environment]
    assert receipt["worker_memory_guard"] == environment


FLOAT_DOC_NODES = {
    "float-protocol": (
        "tests/python/test_native_float_protocol.py::"
        "test_float_protocol_native_five_gc[pcc0]",
    ),
    "class-docstrings": (
        "tests/python/test_native_class_docstrings.py::"
        "test_class_docstrings_native_five_gc[pcc0]",
    ),
    "float-callback-movement": (
        "tests/python/test_native_float_callback_movement.py::"
        "test_canonical_float_callback_movement_gc4[pcc0]",
    ),
}


def _mock_macos(monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "darwin")
    monkeypatch.setattr(gate.platform, "machine", lambda: "arm64")
    monkeypatch.delenv("PCC_TEST_COMPILER", raising=False)


def _mock_runtime_build(monkeypatch, calls):
    # No PCC import, compilation, runtime provisioning or native execution.
    def command(arguments, directory, environment):
        assert arguments[1:3] == ["-m", "pcc.frontends.python.owned_runtime_build"]
        assert environment["PCC_WITH_THREADS"] == "1"
        assert environment["PCC_REFCOUNT_KIND"] == "atomic"
        assert environment["PCC_RUNTIME_BUILD"] == "owned"
        calls.append("runtime-build")
        archive = Path(arguments[arguments.index("--output") + 1])
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"mock archive, never executable")
        Path(str(archive) + ".provenance.json").write_text("{}")
        Path(str(archive) + ".capi_syms").write_text("mock_symbol\n")
        return 0

    def verify(archive, *, threads):
        assert threads is True
        return archive, {
            "target_triple": "arm64-apple-darwin", "member_count": 1,
            "members": [{"runtime_build_config": {"threads": True, "refcount": "atomic"},
                         "codegen_checksum": "mock-codegen"}],
        }

    monkeypatch.setattr(gate, "_command", command)
    monkeypatch.setitem(gate.sys.modules, "tests.runtime_fixture_provenance",
                        SimpleNamespace(_verified_test_runtime_archive=verify))


def test_profiles_preserve_default_order_and_bind_only_three_float_doc_nodes():
    assert gate.PROFILES == {
        "default": (
            "clock", "runtime-build", "gc-sweep", "oserror", "time", "native-worker-clock",
            "worker-handles", "root-joins",
        ),
        "float-doc": (
            "runtime-build", "float-protocol", "class-docstrings", "float-callback-movement",
        ),
        "indexed-split": (
            "indexed-worker-lifetime", "indexed-split", "indexed-handoff-closure",
        ),
    }
    assert {name: dict(gate.GATES)[name] for name in FLOAT_DOC_NODES} == FLOAT_DOC_NODES


def test_float_doc_profile_builds_own_runtime_and_executes_exact_nodes(tmp_path, monkeypatch):
    _mock_macos(monkeypatch)
    calls = []
    _mock_runtime_build(monkeypatch, calls)

    def execute(name, nodes, out, environment):
        calls.append(name)
        assert nodes == FLOAT_DOC_NODES[name]
        assert out == tmp_path
        archive = str(tmp_path / "runtime/libpy_runtime_pcc_py.a")
        assert environment["PCC_RUNTIME_ARCHIVE"] == archive
        assert environment["PCC_THREADED_RUNTIME_ARCHIVE"] == archive
        for key, value in {
            "PCC_WITH_THREADS": "1", "PCC_REFCOUNT_KIND": "atomic",
            "PCC_RUNTIME_BUILD": "owned", "PCC_RUNTIME_CC": "pcc",
            "PCC_RUNTIME_HIGH": "py", "PCC_PYTHON_IR_PASSES": "off",
            "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
            "PCC_IR_TO_OBJ_EMITTER": "pcc", "PCC_NO_AUTO_PCC1": "1",
            "PCC_TEST_COMPILER_STRICT": "1", "PCC_TEST_NO_NATIVE_PROVISIONING": "1",
        }.items():
            assert environment[key] == value
        assert "LC_ALL" not in environment
        return list(nodes)

    monkeypatch.setattr(gate, "_pytest_gate", execute)
    gate.run(tmp_path, profile="float-doc")
    assert calls == list(gate.PROFILES["float-doc"])
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["profile"] == "float-doc" and receipt["status"] == "PASS"
    assert set(receipt["gates"]) == set(gate.PROFILES["float-doc"])
    assert "clock_scope" not in receipt
    assert "without collector-selection telemetry" in receipt["collector_scope"]
    assert "actual GC4" in receipt["collector_scope"]
    for name, nodes in FLOAT_DOC_NODES.items():
        assert receipt["gates"][name] == {
            "status": "PASS", "expected_nodes": list(nodes), "executed_nodes": list(nodes),
        }
    identity = json.loads((tmp_path / "work/runtime-build/runtime-identity.json").read_text())
    assert identity["config"] == {"threads": True, "refcount": "atomic"}
    assert identity["archive_sha256"] == gate._sha256(tmp_path / "runtime/libpy_runtime_pcc_py.a")


@pytest.mark.parametrize("failed", ("runtime-build", *FLOAT_DOC_NODES))
def test_float_doc_failure_marks_later_gates_not_run(tmp_path, monkeypatch, failed):
    _mock_macos(monkeypatch)
    calls = []
    _mock_runtime_build(monkeypatch, calls)
    if failed == "runtime-build":
        def fail_build(*args):
            calls.append("runtime-build")
            return 1
        monkeypatch.setattr(gate, "_command", fail_build)

    def execute(name, nodes, out, environment):
        calls.append(name)
        if name == failed:
            raise AssertionError("selected float/doc gate failed")
        return list(nodes)

    monkeypatch.setattr(gate, "_pytest_gate", execute)
    with pytest.raises(AssertionError):
        gate.run(tmp_path, profile="float-doc")
    order = list(gate.PROFILES["float-doc"])
    index = order.index(failed)
    assert calls == order[:index + 1]
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["status"] == "FAIL"
    for position, name in enumerate(order):
        expected = "PASS" if position < index else "FAIL" if position == index else "NOT_RUN"
        assert receipt["gates"][name]["status"] == expected
        assert receipt["gates"][name]["expected_nodes"] == list(FLOAT_DOC_NODES.get(name, ()))
        if position >= index:
            assert "executed_nodes" not in receipt["gates"][name]


def test_unknown_profile_fails_before_platform_checks_or_any_work(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "platform", "not-darwin")
    out = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="unknown Mac regression profile"):
        gate.run(out, profile="unknown")
    assert not out.exists()


@pytest.mark.parametrize("collect", (False, True))
def test_cli_rejects_unknown_profile_before_run_or_collection(tmp_path, monkeypatch, collect):
    monkeypatch.setattr(gate, "run", lambda *args, **kwargs: pytest.fail("run was reached"))
    monkeypatch.setattr(gate, "collect_evidence", lambda *args: pytest.fail("collection was reached"))
    arguments = ["--out-dir", str(tmp_path / "absent"), "--profile", "unknown"]
    if collect:
        arguments.append("--collect-evidence")
    with pytest.raises(SystemExit) as error:
        gate.main(arguments)
    assert error.value.code == 2
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("profile", ("default", "float-doc", "indexed-split"))
def test_cli_selects_profile_without_changing_default(tmp_path, monkeypatch, profile):
    seen = []
    monkeypatch.setattr(gate, "run", lambda out, **kwargs: seen.append((out, kwargs)))
    arguments = ["--out-dir", str(tmp_path)]
    if profile != "default":
        arguments += ["--profile", profile]
    assert gate.main(arguments) == 0
    assert seen == [(tmp_path.resolve(), {"profile": profile})]


def test_movement_receipt_is_collected_complete(tmp_path):
    work = tmp_path / "work/float-callback-movement/tests"
    work.mkdir(parents=True)
    original = '{"status": "PASS", "gc_backend": 4}\n'
    (work / "float-callback-movement.json").write_text(original)
    (work / "program.out").write_bytes(b"native binary must remain on runner")
    gate.collect_evidence(tmp_path)
    evidence = tmp_path / "evidence"
    path = evidence / "float-callback-movement/tests/float-callback-movement.json.gz"
    assert gzip.decompress(path.read_bytes()).decode() == original
    index = json.loads((evidence / "index.json").read_text())
    assert index["status"] == "COMPLETE"
    assert [row["source"] for row in index["files"]] == [
        "float-callback-movement/tests/float-callback-movement.json",
    ]


def test_float_doc_workflow_is_independent_bounded_and_triggered():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    job = _workflow_job(workflow, "macos-pcc0-float-doc-regressions")
    assert "runs-on: macos-15" in job
    assert "timeout-minutes: 25" in job and "--timeout 1200" in job
    assert "--auto-tree-rss-ceiling-bytes 4294967296" in job
    assert "--min-tree-rss-bytes 2147483648" in job
    assert "--darwin-preflight-reserve-bytes 536870912" in job
    assert job.count("--out-dir build/macos-float-doc --profile float-doc") == 2
    assert "path: build/macos-float-doc/evidence/" in job
    assert "name: macos-pcc0-float-doc-receipts" in job
    assert "retention-days: 7" in job and "compression-level: 0" in job
    assert job.count("if: always()") == 2
    assert "needs:" not in job and "actions/download-artifact" not in job
    assert "env -u LC_ALL" not in job
    assert "build/macos-regressions" not in job and "build/bootstrap" not in job
    assert "--profile float-doc" not in _workflow_job(workflow, "macos-pcc0-regressions")
    assert "--profile float-doc" not in _workflow_job(workflow, "pcc1-package-parity")
    for name in FLOAT_DOC_NODES:
        path = FLOAT_DOC_NODES[name][0].split("::", 1)[0]
        assert workflow.count('      - "' + path + '"') == 2


INDEXED_SPLIT_NODES = {
    "indexed-worker-lifetime": (
        "tests/python/test_worker_resource_plan.py::"
        "test_failure_retires_running_peers_and_does_not_launch_tail",
        "tests/python/test_worker_resource_plan.py::"
        "test_progressive_retry_drains_peers_then_restores_concurrency",
        "tests/python/test_worker_resource_plan.py::"
        "test_ready_unknown_head_drains_peer_before_exclusive_calibration",
    ),
    "indexed-split": (
        "tests/python/test_indexed_process_split_real.py::"
        "test_real_split_and_unsplit_match_complete_objects",
    ),
    "indexed-handoff-closure": (
        "tests/python/test_indexed_handoff_closed_world.py::"
        "test_handoff_provider_closed_world_aarch64",
    ),
}


def test_indexed_split_profile_runs_exact_real_nodes_without_runtime_build(tmp_path, monkeypatch):
    _mock_macos(monkeypatch)
    calls = []
    monkeypatch.setattr(gate, "_command", lambda *args: pytest.fail("runtime build was reached"))
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "2147483648")
    monkeypatch.setenv("PCC_WORKER_TREE_STATE_PATH", str(tmp_path / "tree-state.tsv"))

    def execute(name, nodes, out, environment):
        calls.append(name)
        assert nodes == INDEXED_SPLIT_NODES[name]
        assert out == tmp_path
        assert environment["PCC_HOST_INDEXED_PROCESS_SPLIT"] == "0"
        assert environment["PCC_TEST_NO_NATIVE_PROVISIONING"] == "1"
        assert environment["PCC_NO_AUTO_PCC1"] == "1"
        assert environment["PCC_TEST_COMPILER_STRICT"] == "1"
        assert environment["PCC_WORKER_TREE_BUDGET_BYTES"] == "2147483648"
        assert environment["PCC_WORKER_TREE_STATE_PATH"] == str(tmp_path / "tree-state.tsv")
        return list(nodes)

    monkeypatch.setattr(gate, "_pytest_gate", execute)
    gate.run(tmp_path, profile="indexed-split")
    assert calls == list(gate.PROFILES["indexed-split"]) == list(INDEXED_SPLIT_NODES)
    assert {name: dict(gate.GATES)[name] for name in calls} == INDEXED_SPLIT_NODES
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["status"] == "PASS" and receipt["profile"] == "indexed-split"
    assert "no native helper execution, runtime or GC qualification" in receipt["scope"]
    assert "collector_scope" not in receipt and "clock_scope" not in receipt
    for name, nodes in INDEXED_SPLIT_NODES.items():
        assert receipt["gates"][name] == {
            "status": "PASS", "expected_nodes": list(nodes), "executed_nodes": list(nodes),
        }


@pytest.mark.parametrize("failed", tuple(INDEXED_SPLIT_NODES))
def test_indexed_split_failure_stops_later_gates(tmp_path, monkeypatch, failed):
    _mock_macos(monkeypatch)
    calls = []
    monkeypatch.setattr(gate, "_command", lambda *args: pytest.fail("runtime build was reached"))

    def execute(name, nodes, out, environment):
        calls.append(name)
        if name == failed:
            raise AssertionError("selected indexed gate failed")
        return list(nodes)

    monkeypatch.setattr(gate, "_pytest_gate", execute)
    with pytest.raises(AssertionError, match="selected indexed gate failed"):
        gate.run(tmp_path, profile="indexed-split")
    order = list(INDEXED_SPLIT_NODES)
    index = order.index(failed)
    assert calls == order[:index + 1]
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["status"] == "FAIL"
    for position, name in enumerate(order):
        expected = "PASS" if position < index else "FAIL" if position == index else "NOT_RUN"
        assert receipt["gates"][name]["status"] == expected
        if position >= index:
            assert "executed_nodes" not in receipt["gates"][name]


def test_indexed_receipts_are_retained_without_assembly_or_objects(tmp_path):
    work = tmp_path / "work/indexed-split/tests"
    work.mkdir(parents=True)
    names = ("indexed-process-split.json", "indexed-handoff-closed-world.json")
    for name in names:
        (work / name).write_text('{"status": "PASS"}\n')
    (work / "module.pco").write_bytes(b"owned object stays on runner")
    (work / "provider.ll").write_text("real IR stays on runner")
    gate.collect_evidence(tmp_path)
    index = json.loads((tmp_path / "evidence/index.json").read_text())
    assert index["status"] == "COMPLETE"
    assert {row["source"] for row in index["files"]} == {
        "indexed-split/tests/" + name for name in names
    }
    for name in names:
        actual = tmp_path / "evidence/indexed-split/tests" / (name + ".gz")
        assert gzip.decompress(actual.read_bytes()) == (work / name).read_bytes()


def test_indexed_split_workflow_is_independent_of_macos_stage1_opt_in():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    job = _workflow_job(workflow, "macos-pcc0-indexed-split")
    stage1 = _workflow_job(workflow, "pcc1-package-parity")
    other = _workflow_job(workflow, "other-platform-native-wheel")
    assert "runs-on: macos-15" in job
    assert "timeout-minutes: 25" in job and "--timeout 1200" in job
    assert "--auto-tree-rss-ceiling-bytes 4294967296" in job
    assert "--min-tree-rss-bytes 2147483648" in job
    assert "--darwin-preflight-reserve-bytes 536870912" in job
    assert job.count("--out-dir build/macos-indexed-split --profile indexed-split") == 2
    assert "path: build/macos-indexed-split/evidence/" in job
    assert "name: macos-pcc0-indexed-split-receipts" in job
    assert job.count("if: always()") == 2
    assert "needs:" not in job and "actions/download-artifact" not in job
    assert "env -u LC_ALL" not in job
    assert "needs:" not in stage1
    assert stage1.count('PCC_HOST_INDEXED_PROCESS_SPLIT: "1"') == 1
    assert ('      - name: Build current pcc1\n        env:\n'
            '          # Opt in only this Stage1 build; the bridge gate runs independently.\n'
            '          PCC_HOST_INDEXED_PROCESS_SPLIT: "1"\n'
            '          PCC_MACHO_LINK_PHASE_EVENTS: "1"\n        run: uv run') in stage1
    assert 'PCC_BOOTSTRAP_STAGE_TIMEOUT: "7200"' in stage1
    assert "timeout-minutes: 200" in stage1
    assert "PCC_HOST_INDEXED_PROCESS_SPLIT" not in other
    assert "needs:" not in other
    for name in ("indexed-split", "indexed-handoff-closure"):
        path = INDEXED_SPLIT_NODES[name][0].split("::", 1)[0]
        assert workflow.count('      - "' + path + '"') == 2



def _handoff_xdist_reports(directory, nodes):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "target.stdout").write_text(
        f"created: 6/6 workers\n6 workers [{len(nodes)} items]\n"
        "scheduling tests via LoadGroupScheduling\n")
    rows = _reports(nodes)
    rows[0].update(override_ini=[], inifile=str(ROOT / "pyproject.toml"),
                   markexpr="not integration", keyword="")
    # Model successful pytest9 subreports without conflating them with main tests.
    rows.insert(3, {"event": "report", "nodeid": nodes[0],
                    "when": "call", "outcome": "passed"})
    _write_reports(directory / "live.jsonl", rows)
    xml = ET.Element("testsuites")
    suite = ET.SubElement(xml, "testsuite", tests=str(len(nodes) + 1))
    for node in nodes:
        ET.SubElement(suite, "testcase", {
            "classname": "tests.python.test_pipeline_indexed_handoff.HandoffContractTests",
            "name": node.rsplit("::", 1)[1],
        })
    ET.ElementTree(xml).write(directory / "junit.xml")
    return rows, xml


def test_handoff_default_xdist_keeps_project_options_and_collects_real_evidence(tmp_path, monkeypatch):
    _mock_macos(monkeypatch)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n0 -o addopts=")
    monkeypatch.setenv("LC_ALL", "test-locale-must-not-propagate")
    monkeypatch.setattr(gate, "version", lambda name: "recorded-" + name)
    commands = []

    def command(arguments, directory, environment):
        commands.append(arguments)
        assert environment["PYTEST_ADDOPTS"] == ""
        assert "LC_ALL" not in environment
        assert environment["PCC_PYTEST_AUTO_CLEAN"] == "0"
        assert environment["PCC_NO_AUTO_PCC1"] == "1"
        assert environment["PCC_TEST_NO_NATIVE_PROVISIONING"] == "1"
        _handoff_xdist_reports(directory, gate._handoff_expected_nodes())
        return 0

    monkeypatch.setattr(gate, "_command", command)
    assert gate.main(["--out-dir", str(tmp_path), "--handoff-xdist"]) == 0
    args, = commands
    assert args[:6] == ["uv", "run", "--frozen", "pytest", "-x", "-vv"]
    assert args[-1] == "tests/python/test_pipeline_indexed_handoff.py"
    assert not any(arg in {"-o", "--override-ini", "-c", "--dist"}
                   or arg.startswith(("-n", "--numprocesses", "--dist=", "addopts=")) for arg in args)
    receipt = json.loads((tmp_path / "work/handoff-xdist/result.json").read_text())
    assert receipt["status"] == "PASS" and receipt["worker_count"] == 6
    assert receipt["scheduler"] == "loadgroup" and len(receipt["executed_nodes"]) == 29
    assert receipt["extra_successful_call_reports"] == 1
    assert receipt["test_sha256"] == gate._sha256(ROOT / gate.HANDOFF_TEST)
    assert receipt["uv_lock_sha256"] == gate._sha256(ROOT / "uv.lock")
    assert receipt["versions"] == {name: "recorded-" + name for name in ("pytest", "pytest-xdist", "execnet")}


@pytest.mark.parametrize("change", (
    "no-workers", "partial-collection", "wrong-scheduler", "override-defaults",
    "wrong-config", "collect-only", "missing-call", "missing-teardown", "failed-subreport",
    "xfail", "missing-finish", "duplicate-junit", "skipped-junit",
))
def test_handoff_default_xdist_rejects_missing_or_weakened_execution(tmp_path, change):
    nodes = [gate.HANDOFF_TEST + "::HandoffContractTests::test_one"]
    rows, xml = _handoff_xdist_reports(tmp_path, nodes)
    stdout = tmp_path / "target.stdout"
    if change == "no-workers":
        stdout.write_text(stdout.read_text().replace("created: 6/6 workers", "created: 5/5 workers"))
    elif change == "partial-collection":
        stdout.write_text(stdout.read_text().replace("6 workers [1 items]", "collecting: 5/6 workers"))
    elif change == "wrong-scheduler":
        stdout.write_text(stdout.read_text().replace("LoadGroupScheduling", "LoadScheduling"))
    elif change == "override-defaults":
        rows[0]["override_ini"] = ["addopts="]
    elif change == "wrong-config":
        rows[0]["inifile"] = "/dev/null"
    elif change == "collect-only":
        rows[0]["collect_only"] = True
    elif change == "missing-call":
        rows = [row for row in rows if row.get("when") != "call"]
    elif change == "missing-teardown":
        rows = [row for row in rows if row.get("when") != "teardown"]
    elif change == "failed-subreport":
        rows[3]["outcome"] = "failed"
    elif change == "xfail":
        rows[3]["wasxfail"] = "must not become a pass"
    elif change == "missing-finish":
        rows.pop()
    elif change == "duplicate-junit":
        ET.SubElement(xml[0], "testcase", dict(xml[0][0].attrib))
    else:
        ET.SubElement(xml[0][0], "skipped")
    _write_reports(tmp_path / "live.jsonl", rows)
    ET.ElementTree(xml).write(tmp_path / "junit.xml")
    with pytest.raises((AssertionError, ValueError)):
        gate.verify_handoff_xdist(tmp_path, nodes)


def test_handoff_default_xdist_preserves_failure_logs_and_receipt(tmp_path, monkeypatch):
    _mock_macos(monkeypatch)
    monkeypatch.setattr(gate, "version", lambda name: "recorded-" + name)
    monkeypatch.setattr(gate, "_command", lambda *args: 3)
    with pytest.raises(AssertionError, match="default-xdist pytest failed"):
        gate.run_handoff_xdist(tmp_path)
    receipt = json.loads((tmp_path / "work/handoff-xdist/result.json").read_text())
    assert receipt["status"] == "FAIL" and receipt["returncode"] == 3
    assert "executed_nodes" not in receipt


def test_handoff_default_xdist_workflow_is_bounded_without_stage1_dependency():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    job = _workflow_job(workflow, "macos-pcc0-indexed-split")
    step = job.split("      - name: Verify handoff contracts", 1)[1].split("      - name:", 1)[0]
    assert "--timeout 300" in step
    assert "--auto-tree-rss-ceiling-bytes 4294967296" in step
    assert "--min-tree-rss-bytes 2147483648" in step
    assert "--darwin-preflight-reserve-bytes 536870912" in step
    assert "--out-dir build/macos-indexed-split --handoff-xdist" in step
    assert "work/handoff-xdist-supervisor/result.json" in step
    assert job.index("--handoff-xdist") < job.index("--profile indexed-split")
    assert "timeout-minutes: 25" in job
    assert workflow.count('      - "tests/python/test_pipeline_indexed_handoff.py"') == 2
    assert "needs:" not in _workflow_job(workflow, "pcc1-package-parity")


def test_gc_sweep_gate_selects_twenty_nine_native_and_ten_host_cases():
    nodes = dict(gate.GATES)["gc-sweep"]
    expected = (
        "tests/python/test_gc_relocation_sweep_verdict.py::"
        "test_relocation_copy_clears_stale_sweep_verdict_backend4[pcc_python]",
        *("tests/python/test_gc_tracing_sweep_verdict_native.py::"
          f"test_pending_verdict_native[gc{backend}-{mode}]"
          for backend in (2, 1, 3, 4) for mode in (
              "live-transitive", "dead-self-cycle", "self-resurrection",
              "resurrected-finalizer-batch", "gc-start-callback", "nonpositive-budget",
              "partial-budget-finalizer-once",
          )),
    )
    assert gate.GC_SWEEP_NATIVE_NODES == expected and len(set(expected)) == 29
    host_names = (
        "test_live_pending_candidate_is_rechecked_before_finalizer_or_weakref",
        "test_dead_cycle_finalizes_before_clear_and_reclaims_once",
        "test_post_finalizer_recheck_preserves_resurrected_graph_and_weakref",
        "test_finalizers_keep_isolated_batch_semantics_when_one_resurrects_peer",
        "test_nonpositive_budget_does_not_mark_finalize_or_clear[0]",
        "test_nonpositive_budget_does_not_mark_finalize_or_clear[-1]",
        "test_budget_limits_reclamation_without_repeating_finalizers",
        "test_pinned_and_cext_finalizer_exclusions_are_preserved",
        "test_gc4_forwarding_shell_is_excluded_while_unrelated_dead_object_is_swept",
        "test_gc4_selected_but_unforwarded_candidate_still_finalizes_and_reclaims",
    )
    host = tuple("tests/python/test_gc_tracing_sweep_verdict.py::" + name for name in host_names)
    assert gate.GC_SWEEP_HOST_NODES == host and len(set(host)) == 10
    assert nodes == expected + host and len(set(nodes)) == 39
    assert gate.PROFILES["default"][:3] == ("clock", "runtime-build", "gc-sweep")
    assert "gc-sweep" not in gate.PROFILES["float-doc"] + gate.PROFILES["indexed-split"]


def _gc_sweep_reports(directory, nodes):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "target.stdout").write_text(
        f"created: 6/6 workers\n6 workers [{len(nodes)} items]\n"
        "scheduling tests via LoadGroupScheduling\n")
    rows = _reports(list(nodes))
    rows[0].update(override_ini=[], inifile=str(ROOT / "pyproject.toml"),
                   markexpr="integration or not integration", keyword="")
    _write_reports(directory / "live.jsonl", rows)
    return rows


def test_gc_sweep_command_keeps_default_xdist_and_owned_runtime(tmp_path, monkeypatch):
    nodes = dict(gate.GATES)["gc-sweep"]
    environment = {
        "PCC_RUNTIME_ARCHIVE": "/admitted/libpy_runtime_pcc_py.a",
        "PCC_THREADED_RUNTIME_ARCHIVE": "/admitted/libpy_runtime_pcc_py.a",
        "PCC_TEST_NO_NATIVE_PROVISIONING": "1",
        "PCC_WITH_THREADS": "1", "PCC_REFCOUNT_KIND": "atomic",
        "PCC_WORKER_TREE_BUDGET_BYTES": "2147483648",
        "PCC_WORKER_TREE_STATE_PATH": str(tmp_path / "tree-state.tsv"),
        "PYTEST_ADDOPTS": "-n0 -o addopts=",
    }
    seen = []

    def command(arguments, directory, actual_environment):
        seen.append(arguments)
        assert actual_environment == dict(environment, PYTEST_ADDOPTS="", PCC_PYTEST_AUTO_CLEAN="0")
        _gc_sweep_reports(directory, nodes)
        return 0

    monkeypatch.setattr(gate, "_command", command)
    assert gate._pytest_gate("gc-sweep", nodes, tmp_path, environment) == list(nodes)
    args, = seen
    assert args[:7] == [gate.sys.executable, "-m", "pytest", "-x", "-m",
                        "integration or not integration", "--color=no"]
    assert args[-39:] == list(nodes)
    assert not any(arg in {"-o", "--override-ini", "-c", "--dist", "--collect-only"}
                   or arg.startswith(("-n", "--numprocesses", "--dist=", "addopts=")) for arg in args)
    assert environment["PYTEST_ADDOPTS"] == "-n0 -o addopts="


@pytest.mark.parametrize("change", (
    "no-workers", "partial-collection", "wrong-scheduler", "override-defaults",
    "wrong-config", "wrong-markers",
))
def test_gc_sweep_rejects_weakened_xdist_defaults(tmp_path, change):
    nodes = list(dict(gate.GATES)["gc-sweep"])
    rows = _gc_sweep_reports(tmp_path, nodes)
    stdout = tmp_path / "target.stdout"
    if change == "no-workers":
        stdout.write_text(stdout.read_text().replace("created: 6/6 workers", "created: 5/5 workers"))
    elif change == "partial-collection":
        stdout.write_text(stdout.read_text().replace("6 workers [39 items]", "collecting: 5/6 workers"))
    elif change == "wrong-scheduler":
        stdout.write_text(stdout.read_text().replace("LoadGroupScheduling", "LoadScheduling"))
    elif change == "override-defaults":
        rows[0]["override_ini"] = ["addopts="]
    elif change == "wrong-config":
        rows[0]["inifile"] = "/dev/null"
    else:
        rows[0]["markexpr"] = "not integration"
    _write_reports(tmp_path / "live.jsonl", rows)
    with pytest.raises(AssertionError):
        gate.verify_gc_sweep_xdist(tmp_path, nodes)


@pytest.mark.parametrize("fail_gc", (False, True))
def test_default_profile_runs_gc_after_verified_runtime_and_stops_on_failure(tmp_path, monkeypatch, fail_gc):
    _mock_macos(monkeypatch)
    calls = []
    _mock_runtime_build(monkeypatch, calls)

    def execute(name, nodes, out, environment):
        calls.append(name)
        if name == "gc-sweep":
            assert calls == ["clock", "runtime-build", "gc-sweep"]
            archive = str(tmp_path / "runtime/libpy_runtime_pcc_py.a")
            assert environment["PCC_RUNTIME_ARCHIVE"] == archive
            assert environment["PCC_THREADED_RUNTIME_ARCHIVE"] == archive
            assert environment["PCC_WITH_THREADS"] == "1"
            assert environment["PCC_REFCOUNT_KIND"] == "atomic"
            assert environment["PCC_TEST_NO_NATIVE_PROVISIONING"] == "1"
            assert len(nodes) == 39
            if fail_gc:
                raise AssertionError("original GC sweep failure")
        return list(nodes)

    monkeypatch.setattr(gate, "_pytest_gate", execute)
    if fail_gc:
        with pytest.raises(AssertionError, match="original GC sweep failure"):
            gate.run(tmp_path)
        assert calls == ["clock", "runtime-build", "gc-sweep"]
    else:
        gate.run(tmp_path)
        assert calls == list(gate.PROFILES["default"])
    receipt = json.loads((tmp_path / "work/qualification.json").read_text())
    assert receipt["status"] == ("FAIL" if fail_gc else "PASS")
    assert receipt["gates"]["gc-sweep"]["status"] == receipt["status"]
    assert receipt["gates"]["gc-sweep"]["native_case_count"] == 29
    assert receipt["gates"]["gc-sweep"]["host_case_count"] == 10
    assert receipt["gates"]["runtime-build"]["status"] == "PASS"
    if fail_gc:
        assert all(receipt["gates"][name]["status"] == "NOT_RUN"
                   for name in gate.PROFILES["default"][3:])
        assert "executed_nodes" not in receipt["gates"]["gc-sweep"]
    else:
        assert receipt["gates"]["gc-sweep"]["executed_nodes"] == list(dict(gate.GATES)["gc-sweep"])


def test_gc_receipts_are_retained_without_native_artifacts(tmp_path):
    work = tmp_path / "work/gc-sweep/tests"
    work.mkdir(parents=True)
    names = ["build.json", *(f"gc{backend}-{mode}.json"
             for backend in (2, 1, 3, 4) for mode in gate.GC_SWEEP_MODES)]
    for name in names:
        (work / name).write_text('{"returncode": 0}\n')
    (work / "sweep_verdict.out").write_bytes(b"native executable stays on runner")
    gate.collect_evidence(tmp_path)
    index = json.loads((tmp_path / "evidence/index.json").read_text())
    assert index["status"] == "COMPLETE"
    assert {row["source"] for row in index["files"]} == {"gc-sweep/tests/" + name for name in names}
    for name in names:
        assert gzip.decompress((tmp_path / "evidence/gc-sweep/tests" / (name + ".gz")).read_bytes()) == (
            work / name).read_bytes()


def test_gc_workflow_triggers_cover_fixtures_and_keep_existing_job_budgets():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    for path in (
        "tests/python/test_gc_relocation_sweep_verdict.py",
        "tests/python/test_gc_tracing_sweep_verdict_native.py",
        "tests/fixtures/native/gc_tracing_pending_verdict.c",
        "tests/owned_runtime_c_fixture.py",
        "tests/python/test_gc_tracing_sweep_verdict.py",
        "tests/python/test_freestanding_gc_tracing_sweep_collector.py",
        "tests/runtime_fixture_provenance.py",
        "pcc/**",
    ):
        assert workflow.count('      - "' + path + '"') == 2
    job = _workflow_job(workflow, "macos-pcc0-regressions")
    assert "timeout-minutes: 25" in job and "--timeout 1200" in job
    assert "--auto-tree-rss-ceiling-bytes 4294967296" in job
    assert "--min-tree-rss-bytes 2147483648" in job
    assert "--darwin-preflight-reserve-bytes 536870912" in job
    assert job.count("--out-dir build/macos-regressions") == 2
    assert "--profile" not in job
