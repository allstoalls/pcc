"""Mandatory gate evidence must not turn collection or partial runs into passes."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.qualification.async_gateway import (
    _account,
    _execution_environment,
    _verify_source,
)


def test_collection_and_first_failure_leave_later_original_nodes_unrun():
    events = [
        {"event": "collected", "nodeids": ["first", "second", "third"]},
        {"event": "report", "nodeid": "first", "when": "call", "outcome": "passed"},
        {"event": "report", "nodeid": "second", "when": "call", "outcome": "failed"},
    ]
    rows = _account(events, ["first", "second", "third"])
    assert {node: row["state"] for node, row in rows.items()} == {
        "first": "PASS", "second": "FAIL", "third": "UNRUN",
    }


def test_teardown_errors_and_platform_skips_are_not_successful_execution():
    rows = _account([
        {"event": "report", "nodeid": "native", "when": "call", "outcome": "passed"},
        {"event": "report", "nodeid": "native", "when": "teardown", "outcome": "failed"},
        {"event": "report", "nodeid": "platform", "when": "setup", "outcome": "skipped"},
    ], ["native", "platform"])
    assert rows["native"]["state"] == "SETUP_OR_TEARDOWN_ERROR"
    assert rows["platform"]["state"] == "SKIP"


def test_source_inventory_rejects_changed_bytes_before_running_commands(tmp_path):
    source = tmp_path / "input.py"
    source.write_bytes(b"original\n")
    records = {"input.py": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                             "mode": 0o644, "kind": "file"}}
    _verify_source(tmp_path, records)
    source.write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="source identity mismatch"):
        _verify_source(tmp_path, records)


def test_absent_compiler_selectors_do_not_leak_from_previous_snapshot(monkeypatch, tmp_path):
    for variable in ("PCC_TEST_PCC", "PCC_TEST_PCC1", "PCC_CURRENT_PCC1",
                     "PCC_TEST_COMPILER", "PCC_THREADED_RUNTIME_ARCHIVE"):
        monkeypatch.setenv(variable, str(tmp_path / "stale"))
    args = SimpleNamespace(core=tmp_path, gateway=tmp_path, runtime_archive=tmp_path / "runtime.a",
                           source_manifest="identity.json", threads="1", refcount="atomic",
                           pcc0=None, pcc1=None)
    environment = _execution_environment(args)
    assert "PCC_TEST_PCC" not in environment
    assert "PCC_TEST_PCC1" not in environment
    assert "PCC_CURRENT_PCC1" not in environment
    assert "PCC_TEST_COMPILER" not in environment
    assert "PCC_THREADED_RUNTIME_ARCHIVE" not in environment
    assert environment["PCC_RUNTIME_ARCHIVE"] == str(args.runtime_archive)


def test_explicit_native_compiler_reaches_both_existing_selector_contracts(tmp_path):
    compiler = tmp_path / "pcc1"
    compiler.write_bytes(b"verified by the caller")
    args = SimpleNamespace(core=tmp_path, gateway=tmp_path, runtime_archive=tmp_path / "runtime.a",
                           source_manifest="identity.json", threads="1", refcount="atomic",
                           pcc0=None, pcc1=compiler)
    environment = _execution_environment(args)
    assert environment["PCC_TEST_PCC1"] == str(compiler)
    assert environment["PCC_CURRENT_PCC1"] == str(compiler)


def test_gateway_file_continues_untouched_nodes_without_retrying_failure(monkeypatch):
    from scripts.qualification import async_gateway

    calls = []

    def run(args, name, repo, nodes, environment):
        calls.append(list(nodes))
        cases = {node: {"state": "UNRUN"} for node in nodes}
        if "failed" in nodes:
            cases["failed"] = {"state": "FAIL"}
        else:
            cases.update({node: {"state": "PASS"} for node in nodes})
        return {"cases": cases, "watchdog": {"status": "COMPLETE"}}

    monkeypatch.setattr(async_gateway, "_run", run)
    rows = list(async_gateway._execute_file_nodes(None, "file", None,
                                                  ["failed", "later_a", "later_b"], {}))
    assert calls == [["failed", "later_a", "later_b"], ["later_a", "later_b"]]
    assert rows[0]["cases"]["failed"]["state"] == "FAIL"
    assert rows[1]["cases"]["later_b"]["state"] == "PASS"


def test_gateway_resource_stop_does_not_retry_unreported_nodes(monkeypatch):
    from scripts.qualification import async_gateway

    calls = []

    def run(args, name, repo, nodes, environment):
        calls.append(list(nodes))
        return {"cases": {"first": {"state": "PASS"}, "later": {"state": "UNRUN"}},
                "watchdog": {"status": "MEMORY_LIMIT"}}

    monkeypatch.setattr(async_gateway, "_run", run)
    rows = list(async_gateway._execute_file_nodes(None, "file", None,
                                                  ["first", "later"], {}))
    assert len(rows) == 1
    assert calls == [["first", "later"]]
    assert rows[0]["cases"]["later"]["state"] == "UNRUN"


@pytest.fixture
def run_gate_with_receipts(monkeypatch, tmp_path):
    """Exercise main's real accounting with synthetic process receipts only."""
    from scripts.qualification import async_gateway

    roots = {area: tmp_path / area for area in ("pcc", "pcc-gateway")}
    identities = {}
    for area, root in roots.items():
        root.mkdir()
        marker = root / "source.py"
        marker.write_bytes(area.encode())
        marker.chmod(0o644)
        identities[area] = {
            "source.py": {"sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                          "mode": 0o644, "kind": "file"},
        }
    manifest = tmp_path / "source-identities.json"
    manifest.write_text(json.dumps(identities))
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"unit-test input; no compiler or native execution")
    output = tmp_path / "output"
    planned = {
        "async": [async_gateway.ASYNC_FILE + "::test_original"],
        "gateway": ["tests/test_gateway.py::test_first",
                    "tests/test_gateway.py::test_last"],
    }

    def run_gate(*, failure_area=None, failure_collect=False,
                 supervisor_returncode=0, watchdog=None):
        calls = []

        def run(args, name, repo, nodes, environment, *, collect=False):
            area = "async" if repo == roots["pcc"] else "gateway"
            calls.append((area, collect, list(nodes)))
            events = [{"event": "report", "nodeid": node,
                       "when": "call", "outcome": "passed"} for node in nodes]
            failed_process = area == failure_area and collect == failure_collect
            return {
                "name": name, "collection_only": collect,
                "returncode": supervisor_returncode if failed_process else 0,
                "watchdog": watchdog if failed_process else {"status": "COMPLETE", "returncode": 0},
                "selected": planned[area] if collect else list(nodes),
                "deselected": [], "cases": {} if collect else _account(events, nodes),
            }

        monkeypatch.setattr(async_gateway, "_run", run)
        result = async_gateway.main([
            "--core", str(roots["pcc"]), "--gateway", str(roots["pcc-gateway"]),
            "--runtime-archive", str(runtime), "--source-manifest", str(manifest),
            "--output", str(output), "--threads", "1", "--refcount", "atomic",
        ])
        return result, json.loads((output / "summary.json").read_text()), calls, planned

    return run_gate


@pytest.mark.parametrize("failure_collect", [False, True], ids=["execution", "collection"])
@pytest.mark.parametrize("failure_area", ["async", "gateway"])
@pytest.mark.parametrize("supervisor_returncode,watchdog", [
    pytest.param(124, {"status": "TIMEOUT", "returncode": -15}, id="timeout"),
    pytest.param(125, {"status": "MEMORY_LIMIT", "returncode": -15}, id="memory-limit"),
    pytest.param(130, {"status": "INTERRUPTED", "returncode": -15}, id="interrupted"),
    pytest.param(0, {"status": "SAMPLER_ERROR", "returncode": 0}, id="sampler-error"),
    pytest.param(1, {"status": "COMPLETE", "returncode": 0}, id="supervisor-failure"),
    pytest.param(0, {"status": "COMPLETE", "returncode": 1}, id="child-failure"),
    pytest.param(1, {"status": "COMPLETE", "returncode": 1}, id="session-exit-failure"),
    pytest.param(0, {}, id="missing-watchdog"),
    pytest.param(0, {"returncode": 0}, id="missing-status"),
    pytest.param(0, {"status": "COMPLETE"}, id="missing-child-returncode"),
])
def test_main_rejects_failed_process_receipts_even_when_calls_pass(
    run_gate_with_receipts, failure_collect, failure_area, supervisor_returncode, watchdog,
):
    result, report, calls, planned = run_gate_with_receipts(
        failure_area=failure_area, failure_collect=failure_collect,
        supervisor_returncode=supervisor_returncode, watchdog=watchdog,
    )
    assert result == 1
    assert report["complete_execution"] is False
    assert report["source_stable"] is True
    assert report["final_cases"]
    assert all(case["state"] == "PASS" for case in report["final_cases"].values())
    if not failure_collect:
        assert set(report["final_cases"]) == set(planned["async"] + planned["gateway"])
        assert report["all_nodes_reported"] is True
        assert report["execution_processes_succeeded"] is False
    else:
        assert report["all_nodes_reported"] is False
    # A resource stop after the last call must not cause a retry.
    assert len(calls) == len({(area, collect) for area, collect, nodes in calls})


def test_main_accepts_complete_successful_processes_without_changing_selection(run_gate_with_receipts):
    result, report, calls, planned = run_gate_with_receipts()
    from scripts.qualification import async_gateway

    assert result == 0
    assert report["source_stable"] is True
    assert report["all_nodes_reported"] is True
    assert report["execution_processes_succeeded"] is True
    assert report["complete_execution"] is True
    assert set(report["final_cases"]) == set(planned["async"] + planned["gateway"])
    assert all(case["state"] == "PASS" for case in report["final_cases"].values())
    assert calls == [("async", True, [async_gateway.ASYNC_FILE]),
                     ("async", False, planned["async"]),
                     ("gateway", True, ["tests"]),
                     ("gateway", False, planned["gateway"])]
