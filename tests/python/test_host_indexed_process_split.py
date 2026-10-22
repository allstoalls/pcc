"""Mock-only contracts for the bounded host indexed-process split.

These tests do not launch children or invoke a compiler. Integration/parity
qualification remains separate from these coordinator lifecycle contracts.
"""

from pathlib import Path
import shlex
import subprocess

import pytest

from pcc.frontends.python import pipeline_frontend_indexed_stage as stage
from pcc.frontends.python import pipeline_frontend_worker_execution as execution
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_indexed_handoff as handoff
from pcc.frontends.python import pipeline_targets as targets
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 * 1024


def _enabled_environment():
    return {
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
        "PCC_WORKER_TREE_BUDGET_BYTES": str(512 * MIB),
    }


def _require(env, **changes):
    options = dict(checkpoint_root="", action_cache_plan=None,
                   libpython_mode="off", artifact_dir="/private/artifacts")
    options.update(changes)
    stage.require_host_indexed_split(env, **options)


def test_host_split_requires_explicit_zero_fallback():
    env = _enabled_environment()
    _require(env)
    for value in ("", "0", "off"):
        env["PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"] = value
        with pytest.raises(ValueError, match="ZERO_FALLBACK|zero.fallback"):
            _require(env)


@pytest.mark.parametrize("name,value", (
    ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "1"),
    ("PCC_TEXT_INDEXED_KERNEL_EMIT", "1"),
    ("PCC_DIRECT_INDEXED_SIDECAR", "1"),
    ("PCC_PY_FRONTEND_IN_PROCESS_CODEGEN", "1"),
    ("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "0"),
    ("PCC_DEFER_FRONTEND_CODEGEN_PLAN", "/private/plan"),
    ("PCC_WORKER_TREE_BUDGET_BYTES", "0"),
))
def test_host_split_rejects_incompatible_routes(name, value):
    env = _enabled_environment()
    env[name] = value
    with pytest.raises(ValueError):
        _require(env)


@pytest.mark.parametrize("changes", (
    {"checkpoint_root": "/private/checkpoint"},
    {"action_cache_plan": {}},
    {"libpython_mode": "on"},
    {"artifact_dir": ""},
))
def test_host_split_rejects_incompatible_frontend_contracts(changes):
    with pytest.raises(ValueError):
        _require(_enabled_environment(), **changes)


def _stage_fixture(tmp_path, monkeypatch, count=5, order=None):
    root = tmp_path.resolve()
    ir_dir = root / "ir"
    ir_dir.mkdir()
    manifests, results, commands, names, chunks, frontend_tasks = [], [], [], [], [], []
    for index in range(count):
        manifest = root / ("worker_" + str(index) + ".manifest")
        manifest.write_text("manifest " + str(index), encoding="utf-8")
        result = root / ("worker_" + str(index) + ".tsv")
        sidecar = ir_dir / ("module_" + str(index) + ".direct.pidx")
        ir_path = ir_dir / ("module_" + str(index) + ".ll")
        name = "fixture.module_" + str(index)
        result.write_text("\t".join(("OK", str(index), name, "0", "0", "0",
                                    str(ir_path), "PIDX", str(sidecar))) + "\n",
                          encoding="utf-8")
        manifests.append(str(manifest))
        results.append(str(result))
        commands.append("fixture-frontend " + str(index))
        names.append(name)
        chunks.append([index])
        frontend_tasks.append({
            "class": "host-source-owner", "inputs": [index + 1] * 6,
            "estimate_bytes": 0, "source_identity": "identity-" + str(index),
            "report_path": str(manifest) + ".rss", "restartable": True,
            "diagnostic_phase": "codegen", "diagnostic_modules": [name],
            "diagnostic_indices": [index],
        })
    planned_order = list(range(count)) if order is None else list(order)
    requests = {}
    monkeypatch.setattr(workers, "resource_tasks_for_commands",
                        lambda actual: [dict(item) for item in frontend_tasks])
    monkeypatch.setattr(policy, "resource_task_order", lambda tasks, guarded_calibration=False,
                        native_cold_cover=False: (planned_order, []))
    monkeypatch.setattr(execution, "_direct_owned_pass_names", lambda name: ["mem2reg"])
    monkeypatch.setattr(targets, "host_target_triple", lambda: "arm64-apple-darwin")
    monkeypatch.setattr(handoff, "write_request",
                        lambda path, fields: requests.update({path: dict(fields)}))
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(512 * MIB))
    return dict(commands=commands, manifests=manifests, results=results,
                chunks=chunks, names=names, ir_dir=str(ir_dir), requests=requests,
                frontend_tasks=frontend_tasks, order=planned_order)


def _run_stage(fixture, width):
    stage.run_host_indexed_stage(
        fixture["commands"], fixture["manifests"], fixture["results"],
        fixture["chunks"], fixture["names"], fixture["ir_dir"],
        ["host-python", "-m", "pcc"], width, "FIXTURE_MODE=host", shlex.quote,
    )


def test_stage_plan_preserves_module_identity_and_pairs_dynamic_slots(tmp_path, monkeypatch):
    fixture = _stage_fixture(tmp_path, monkeypatch, order=[3, 1, 4, 0, 2])
    captured = {}

    def capture(commands, tasks, width, budget, **kwargs):
        captured.update(commands=commands, tasks=tasks, width=width, budget=budget)
        for request in fixture["requests"].values():
            Path(request["output_path"]).write_bytes(b"mock packed object")

    monkeypatch.setattr(pool, "run_resource_worker_processes", capture)
    _run_stage(fixture, 2)
    assert captured["width"] == 2
    assert captured["budget"] == 512 * MIB
    assert len(captured["commands"]) == len(captured["tasks"]) == 10
    tasks = captured["tasks"]
    for position, original_index in enumerate(fixture["order"]):
        frontend, backend = tasks[2 * position:2 * position + 2]
        assert frontend["diagnostic_indices"] == backend["diagnostic_indices"] == [original_index]
        assert frontend["depends_on"] == -1
        assert backend["depends_on"] == 2 * position
        assert frontend["handoff_slot"] == backend["handoff_slot"] == 2 * position
        assert frontend["calibrate_before_peers"] is True
        assert backend["calibrate_before_peers"] is True
        assert "calibrate_before_peers" not in fixture["frontend_tasks"][original_index]
        assert frontend["source_identity"] == "identity-" + str(original_index)
        assert backend["handoff_source_identity"] == frontend["source_identity"]
        assert backend["inputs"] == [0] * 47
        assert backend["estimate_bytes"] == 0
        assert "input_cost" not in backend
        request = fixture["requests"][backend["handoff_request"]]
        assert request["index"] == original_index
        assert request["passes"] == ["mem2reg"]
        assert request["target"] == "arm64-apple-darwin"
        assert fixture["commands"][original_index] in captured["commands"][2 * position]
        assert "--pcc-self-backend-indexed-emit-worker" in captured["commands"][2 * position + 1]
    # Only own-FE edges remain; the pool owns the bounded dynamic capacity.
    policy.validate_handoff_slots(tasks)
    for index, path in enumerate(fixture["results"]):
        fields = Path(path).read_text(encoding="utf-8").strip().split("\t")
        assert fields[:3] == ["OK", str(index), fixture["names"][index]]
        assert fields[-2:] == ["PCO", str(Path(fixture["ir_dir"]) /
                                            ("module_" + str(index) + ".direct.pco"))]


def test_stage_does_not_accept_missing_backend_output(tmp_path, monkeypatch):
    fixture = _stage_fixture(tmp_path, monkeypatch, count=1)
    monkeypatch.setattr(pool, "run_resource_worker_processes", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="publication differs"):
        _run_stage(fixture, 1)
    assert "\tPIDX\t" in Path(fixture["results"][0]).read_text(encoding="utf-8")


def _mock_lifecycle(tmp_path, monkeypatch, *, backend_exit=0, cleanup_error=False,
                    backend_report="complete"):
    events = []
    tokens, task_by_pid = {}, {}
    sidecar = tmp_path / "module.direct.pidx"
    sidecar.write_bytes(b"sealed input")
    tasks = [
        {"class": "frontend", "inputs": [1], "estimate_bytes": 0,
         "report_path": str(tmp_path / "fe.rss"), "restartable": True},
        {"class": "backend", "inputs": [0] * 47, "estimate_bytes": 0,
         "report_path": str(tmp_path / "be.rss"), "restartable": True,
         "depends_on": 0, "handoff_request": "coordinator-request", "input_ready": False},
        {"class": "frontend", "inputs": [1], "estimate_bytes": 0,
         "report_path": str(tmp_path / "next.rss"), "restartable": True, "depends_on": 1},
    ]
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: MIB)
    monkeypatch.setattr(policy, "resource_task_order", lambda actual, guarded_calibration=False,
                        native_cold_cover=False: ([0, 1, 2], []))
    monkeypatch.setattr(pool, "_command_spec", lambda command: (["mock-worker"], []))
    monkeypatch.setattr(pool.time, "sleep", lambda seconds: None)

    def start(specs, index):
        pid = 5000 + index
        env = dict(entry.split("=", 1) for entry in specs[index][1])
        tokens[index] = env[policy.RESOURCE_TOKEN_ENV]
        task_by_pid[pid] = index
        if index == 1:
            assert sidecar.exists()
            assert env[handoff.ENV_SEAL] == "a" * 64
        if index == 2:
            assert not sidecar.exists()
            assert ("cleanup", tokens[1]) in events
        events.append(("start", index))
        return pid

    def poll(pid):
        index = task_by_pid[pid]
        events.append(("poll", index))
        return backend_exit if index == 1 else 0

    def read(path, pid, token=""):
        index = task_by_pid[pid]
        assert token == tokens[index]
        events.append(("report", index))
        return (backend_report if index == 1 else "complete", MIB, MIB)

    def prepare(task, frontend_token):
        assert frontend_token == tokens[0]
        assert ("retire", 0) in events
        events.append(("prepare", frontend_token))
        task.update(input_ready=True, inputs=[1] * 47,
                    handoff_seal_sha256="a" * 64, source_identity="b" * 64)

    def reset(task):
        assert task["input_ready"] is True
        assert sidecar.exists()
        events.append(("reset", 1))

    def cleanup(task, backend_token):
        assert backend_token == tokens[1]
        assert ("retire", 1) in events
        assert events[-1] == ("report", 1)
        events.append(("cleanup", backend_token))
        if cleanup_error:
            raise OSError("mock cleanup blocked")
        sidecar.unlink()

    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker",
                        lambda pid: events.append(("retire", task_by_pid[pid])))
    monkeypatch.setattr(pool, "_stop_resource_worker",
                        lambda pid: events.append(("stop", task_by_pid[pid])))
    monkeypatch.setattr(policy, "read_worker_resource", read)
    monkeypatch.setattr(handoff, "prepare_handoff_task", prepare)
    monkeypatch.setattr(handoff, "reset_handoff_output", reset)
    monkeypatch.setattr(handoff, "retire_handoff_task", cleanup)
    return tasks, events, tokens, sidecar


def _run_mock_pool(tasks, observations):
    pool.run_resource_worker_processes(
        ["mock"] * len(tasks), tasks, 1, 512 * MIB, observations=observations,
    )


def test_pool_removes_handoff_only_after_verified_backend_retirement(tmp_path, monkeypatch):
    tasks, events, tokens, sidecar = _mock_lifecycle(tmp_path, monkeypatch)
    observations = []
    _run_mock_pool(tasks, observations)
    assert events.index(("retire", 1)) < events.index(("cleanup", tokens[1]))
    assert events.index(("cleanup", tokens[1])) < events.index(("start", 2))
    assert not sidecar.exists()
    assert len(observations) == 3
    assert all(sample[2] == MIB for sample in observations)


def test_cleanup_failure_blocks_successor_and_backend_observation(tmp_path, monkeypatch):
    tasks, events, _tokens, sidecar = _mock_lifecycle(tmp_path, monkeypatch, cleanup_error=True)
    observations = []
    with pytest.raises(OSError, match="cleanup blocked"):
        _run_mock_pool(tasks, observations)
    assert ("start", 2) not in events
    assert sidecar.exists()
    assert len(observations) == 1


def test_failed_backend_keeps_retry_input_and_blocks_successor(tmp_path, monkeypatch):
    tasks, events, _tokens, sidecar = _mock_lifecycle(tmp_path, monkeypatch, backend_exit=7)
    observations = []
    with pytest.raises(subprocess.CalledProcessError) as error:
        _run_mock_pool(tasks, observations)
    assert error.value.returncode == 7
    assert not any(event[0] == "cleanup" for event in events)
    assert ("start", 2) not in events
    assert sidecar.exists()
    assert len(observations) == 1


def test_unverified_backend_completion_never_consumes_handoff(tmp_path, monkeypatch):
    tasks, events, _tokens, sidecar = _mock_lifecycle(
        tmp_path, monkeypatch, backend_report="encode-complete",
    )
    observations = []
    with pytest.raises(policy.WorkerMemoryError, match="final peak RSS"):
        _run_mock_pool(tasks, observations)
    assert not any(event[0] == "cleanup" for event in events)
    assert ("start", 2) not in events
    assert sidecar.exists()
    assert len(observations) == 1


def test_pressure_retry_preserves_published_handoff_until_verified_retirement(tmp_path, monkeypatch):
    """A live 'complete' report is insufficient to consume a retry's input."""
    sidecar = tmp_path / "module.pidx"
    sidecar.write_bytes(b"immutable sealed input")
    output = tmp_path / "module.pco"
    tasks = [
        {"class": "frontend", "inputs": [1], "estimate_bytes": MIB,
         "report_path": str(tmp_path / "fe.rss"), "restartable": True},
        {"class": "backend", "inputs": [0] * 47, "estimate_bytes": 0,
         "report_path": str(tmp_path / "be.rss"), "restartable": True,
         "depends_on": 0, "handoff_request": "request", "input_ready": False},
        {"class": "peer", "inputs": [1], "estimate_bytes": 80 * MIB,
         "report_path": str(tmp_path / "peer.rss"), "restartable": True},
        {"class": "next-frontend", "inputs": [1], "estimate_bytes": MIB,
         "report_path": str(tmp_path / "next.rss"), "restartable": True,
         "depends_on": 1},
    ]
    starts, resets, cancellations, cleanups = [], [], [], []
    attempts, pid_items, tokens = {}, {}, {}
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: MIB)
    monkeypatch.setattr(policy, "resource_task_order", lambda actual, guarded_calibration=False,
                        native_cold_cover=False: ([0, 2, 1, 3], []))
    monkeypatch.setattr(pool, "_command_spec", lambda command: (["mock-worker"], []))
    monkeypatch.setattr(pool.time, "sleep", lambda seconds: None)

    def start(specs, index):
        attempts[index] = attempts.get(index, 0) + 1
        attempt = attempts[index]
        pid = 6000 + index * 10 + attempt
        pid_items[pid] = (index, attempt)
        env = dict(entry.split("=", 1) for entry in specs[index][1])
        tokens[(index, attempt)] = env[policy.RESOURCE_TOKEN_ENV]
        starts.append((index, attempt))
        if index == 1:
            assert sidecar.read_bytes() == b"immutable sealed input"
            assert not output.exists()
            output.write_bytes(b"published before retirement")
        if index == 3:
            assert cleanups == [(1, 2)]
            assert not sidecar.exists()
        return pid

    def poll(pid):
        index, attempt = pid_items[pid]
        if index == 2 and not cancellations:
            return pool._WORKER_RUNNING
        if index == 1 and attempt == 1:
            return pool._WORKER_RUNNING
        return 0

    def report(path, pid, token=""):
        index, attempt = pid_items[pid]
        assert token == tokens[(index, attempt)]
        peak = 160 * MIB if (index, attempt) == (1, 1) else MIB
        return "complete", peak, peak

    def prepare(task, frontend_token):
        assert frontend_token == tokens[(0, 1)]
        task.update(input_ready=True, inputs=[1] * 47,
                    handoff_seal_sha256="a" * 64, source_identity="b" * 64)

    def reset(task):
        assert sidecar.exists()
        resets.append(attempts.get(1, 0) + 1)
        if output.exists():
            output.unlink()

    def stop(pid):
        item = pid_items[pid]
        cancellations.append(item)
        assert item == (1, 1)
        assert sidecar.exists() and output.exists()

    def cleanup(task, backend_token):
        assert backend_token == tokens[(1, 2)]
        assert sidecar.exists() and output.exists()
        cleanups.append((1, 2))
        sidecar.unlink()

    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", lambda pid: None)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    monkeypatch.setattr(policy, "read_worker_resource", report)
    monkeypatch.setattr(handoff, "prepare_handoff_task", prepare)
    monkeypatch.setattr(handoff, "reset_handoff_output", reset)
    monkeypatch.setattr(handoff, "retire_handoff_task", cleanup)
    observations = [("backend", [1] * 47, MIB)]
    pool.run_resource_worker_processes(["mock"] * 4, tasks, 2, 512 * MIB,
                                      observations=observations)
    assert cancellations == [(1, 1)]
    assert resets == [1, 2]
    assert cleanups == [(1, 2)]
    assert tokens[(1, 1)] != tokens[(1, 2)]
    assert starts == [(0, 1), (2, 1), (1, 1), (1, 2), (3, 1)]
    assert tasks[1]["incomplete_peak_bytes"] == 160 * MIB
    assert sum(sample[0] == "backend" for sample in observations) == 2


def test_actual_shape_preserves_maximum_observation_and_class_scope():
    inputs = [1] * 47
    task = {"class": "host|arm64|passes=off|backend", "inputs": inputs,
            "estimate_bytes": 0}
    observations = [(task["class"], [2] * 47, 3 * MIB),
                    (task["class"], [3] * 47, 7 * MIB),
                    ("native|arm64|passes=off|backend", [9] * 47, 100 * MIB),
                    ("host|x86|passes=off|backend", [9] * 47, 200 * MIB)]
    assert policy.estimated_task_bytes(task, observations) == policy.peak_reservation(7 * MIB)
    uncovered = list(inputs)
    uncovered[46] = 4
    task["inputs"] = uncovered
    assert policy.estimated_task_bytes(task, observations) == 0
