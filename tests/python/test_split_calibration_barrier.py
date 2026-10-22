"""Host-only admission controls; fake workers never launch a child or compiler.

These controls exercise the existing coordinator and process ownership helpers.
They do not qualify native admission, process-tree enforcement, or compilation.
"""

import subprocess

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy
from tests.python.test_host_indexed_process_split import (
    _mock_lifecycle,
    _run_mock_pool,
)


MIB = 1024 * 1024
AVAILABLE = 512 * MIB


def _task(index, *, estimate=0, inputs=(1,), execution_class=None, **fields):
    item = {
        "class": execution_class or "split:" + str(index),
        "inputs": list(inputs), "estimate_bytes": estimate,
        "report_path": "unused-" + str(index), "restartable": True,
        "calibrate_before_peers": True,
    }
    item.update(fields)
    return item


@pytest.mark.parametrize("pending", ([3, 2, 1, 0], [2, 0, 3, 1], [0, 1, 2, 3]))
def test_unknown_split_heads_use_original_index_not_pending_order(pending):
    tasks = [_task(0, estimate=8 * MIB), _task(1), _task(2), _task(3)]
    observations = []
    for index in (1, 2, 3):
        assert policy.choose_task(
            pending, tasks, observations, [], 4, AVAILABLE,
        ) == (index, AVAILABLE, True)
        pending = [item for item in pending if item != index]
        observations.append((tasks[index]["class"], tasks[index]["inputs"], MIB))
    assert policy.choose_task(pending, tasks, observations, [], 4, AVAILABLE) == (
        0, 8 * MIB, False,
    )


def test_known_peers_cannot_extend_an_unknown_heads_draining_wave():
    tasks = [_task(0), _task(1, estimate=8 * MIB)]
    for active in ([32 * MIB, 16 * MIB], [16 * MIB], [MIB]):
        tasks.append(_task(len(tasks), estimate=MIB))
        pending = list(reversed(range(len(tasks))))
        assert policy.choose_task(pending, tasks, [], active, 4, AVAILABLE) == (
            -1, 0, False,
        )
    assert policy.choose_task(pending, tasks, [], [], 4, AVAILABLE) == (
        0, AVAILABLE, True,
    )


@pytest.mark.parametrize("active,width,available", (
    ([MIB, MIB], 2, AVAILABLE),
    ([], 2, 0),
    ([], 2, -1),
))
def test_split_barrier_does_not_bypass_width_or_positive_capacity(active, width, available):
    assert policy.choose_task([0], [_task(0)], [], active, width, available) == (
        -1, 0, False,
    )


@pytest.mark.parametrize("marker", ("absent", False))
@pytest.mark.parametrize("case,expected", (
    ("known-before-unknown", (0, 8 * MIB, False)),
    ("unknown-before-known", (1, 8 * MIB, False)),
    ("first-pending-unknown", (1, AVAILABLE, True)),
    ("unknown-with-active", (-1, 0, False)),
    ("fitting-peer-with-active", (1, 8 * MIB, False)),
    ("oversized-explicit", (-1, 0, False)),
    ("guarded-borrowed-forecast", (0, policy.peak_reservation(AVAILABLE), True)),
    ("unguarded-borrowed-forecast", (-1, 0, False)),
    ("width-full", (-1, 0, False)),
    ("no-capacity", (-1, 0, False)),
))
def test_absent_or_false_marker_preserves_legacy_decisions(marker, case, expected):
    # Literal decisions from the original chooser, including its first-fit
    # preference for known demand over an earlier unknown pending entry.
    tasks = [_task(0), _task(1)]
    pending, observations, active = [0, 1], [], []
    width, available, guarded = 2, AVAILABLE, False
    if case == "known-before-unknown":
        tasks[0]["estimate_bytes"] = 8 * MIB
    elif case in ("unknown-before-known", "fitting-peer-with-active"):
        tasks[1]["estimate_bytes"] = 8 * MIB
        if case == "fitting-peer-with-active":
            active = [8 * MIB]
    elif case == "first-pending-unknown":
        pending = [1, 0]
    elif case == "unknown-with-active":
        active = [8 * MIB]
    elif case == "oversized-explicit":
        pending = [0]
        tasks[0]["estimate_bytes"] = AVAILABLE + 1
        guarded = True
    elif case in ("guarded-borrowed-forecast", "unguarded-borrowed-forecast"):
        pending = [0]
        observations = [(tasks[0]["class"], [1], AVAILABLE)]
        guarded = case == "guarded-borrowed-forecast"
    elif case == "width-full":
        active = [MIB, MIB]
    elif case == "no-capacity":
        available = 0
    for item in tasks:
        if marker == "absent":
            del item["calibrate_before_peers"]
        else:
            item["calibrate_before_peers"] = marker
    assert policy.choose_task(
        pending, tasks, observations, active, width, available,
        guarded_calibration=guarded,
    ) == expected


def test_existing_retry_priority_and_minimum_floor_precede_split_barrier():
    tasks = [_task(0), _task(1, estimate=8 * MIB,
                             retry_calibration=True, incomplete_peak_bytes=384 * MIB)]
    assert policy.choose_task([0, 1], tasks, [], [MIB], 2, AVAILABLE) == (-1, 0, False)
    demand = policy.peak_reservation(384 * MIB)
    assert policy.choose_task([0, 1], tasks, [], [], 2, AVAILABLE) == (1, demand, True)
    assert demand > AVAILABLE
    with pytest.raises(policy.WorkerMemoryError, match="estimated_minimum_budget_bytes"):
        policy.require_task_fits(1, demand, AVAILABLE, 768 * MIB)


@pytest.mark.parametrize("dimensions", (6, 47))
def test_coverage_is_recomputed_after_inputs_change_without_cached_known_state(dimensions):
    tasks = [_task(0, estimate=8 * MIB), _task(1, inputs=[1] * dimensions)]
    head = tasks[1]
    observations = [
        (head["class"], [2] * dimensions, 3 * MIB),
        (head["class"], [3] * dimensions, 7 * MIB),
        ("other-execution-class", [9] * dimensions, 300 * MIB),
    ]
    before = dict(head, inputs=list(head["inputs"]))
    assert policy.estimated_task_bytes(head, observations) == policy.peak_reservation(7 * MIB)
    assert policy.choose_task([0, 1], tasks, observations, [], 2, AVAILABLE) == (
        0, 8 * MIB, False,
    )
    assert head == before
    head["inputs"] = [1] * (dimensions - 1) + [4]
    changed = dict(head, inputs=list(head["inputs"]))
    assert policy.estimated_task_bytes(head, observations) == 0
    assert policy.choose_task([0, 1], tasks, observations, [MIB], 2, AVAILABLE) == (
        -1, 0, False,
    )
    assert policy.choose_task([0, 1], tasks, observations, [], 2, AVAILABLE) == (
        1, AVAILABLE, True,
    )
    assert head == changed


def _fake_processes(tmp_path, monkeypatch, tasks, order, *, poll_result, peak=None):
    """Mock the OS boundary, retaining original start/poll/stop/retire ownership."""
    state = {"starts": [], "stops": [], "retired": [], "events": [],
             "attempts": {}, "processes": {}, "polls": 0, "admissions": []}
    for index, item in enumerate(tasks):
        item["report_path"] = str(tmp_path / (str(index) + ".rss"))
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: MIB)
    monkeypatch.setattr(pool, "_HOST_WORKERS", {})
    monkeypatch.setattr(policy, "resource_task_order", lambda actual, guarded_calibration=False: (list(order), []))
    monkeypatch.setattr(pool, "_command_spec", lambda command: (["fake-worker", command], []))
    monkeypatch.setattr(pool.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(pool, "_resource_event",
                        lambda path, event, index, pid, reservation, available, peak:
                        state["admissions"].append((event, index, reservation, available)))

    class Process:
        def __init__(self, argv, *, env, **kwargs):
            self.index = int(argv[1])
            self.attempt = state["attempts"].get(self.index, 0) + 1
            state["attempts"][self.index] = self.attempt
            self.item = (self.index, self.attempt)
            self.pid = 8000 + len(state["processes"])
            self.token = env[policy.RESOURCE_TOKEN_ENV]
            self.returncode = None
            self.poll_count = 0
            state["processes"][self.pid] = self
            state["starts"].append(self.item)
            state["events"].append(("start", self.item))

        def poll(self):
            assert pool._HOST_WORKERS.get(self.pid) is self
            assert self.item not in state["retired"]
            state["polls"] += 1
            assert state["polls"] <= 80, "mock pool failed to make progress"
            self.poll_count += 1
            self.returncode = poll_result(self, state)
            return self.returncode

        def kill(self):
            assert pool._HOST_WORKERS.get(self.pid) is self
            assert self.item not in state["retired"]
            assert self.returncode is None
            self.returncode = -9
            state["stops"].append(self.item)
            state["events"].append(("stop", self.item))

        def wait(self, timeout):
            assert self.returncode is not None
            if self.pid not in pool._HOST_WORKERS:
                assert self.item not in state["retired"]
                state["retired"].append(self.item)
                state["events"].append(("retire", self.item))
            return self.returncode

    def report(path, pid, token=""):
        process = state["processes"][pid]
        assert path == tasks[process.index]["report_path"]
        assert token == process.token
        measured = MIB if peak is None else peak(process, state)
        return "complete", measured, measured

    monkeypatch.setattr(pool.subprocess, "Popen", Process)
    monkeypatch.setattr(pool.os, "killpg", lambda pid, number: state["processes"][pid].kill(),
                        raising=False)
    monkeypatch.setattr(policy, "read_worker_resource", report)
    return state


def _run_fake_pool(tasks, observations, *, width=2):
    pool.run_resource_worker_processes(
        [str(index) for index in range(len(tasks))], tasks, width, 512 * MIB,
        observations=observations,
    )
    assert pool._HOST_WORKERS == {}


def test_dependency_not_ready_head_is_excluded_by_actual_pool_readiness(tmp_path, monkeypatch):
    tasks = [_task(0, depends_on=1), _task(1, estimate=8 * MIB),
             _task(2, estimate=8 * MIB)]
    state = _fake_processes(
        tmp_path, monkeypatch, tasks, [0, 1, 2],
        poll_result=lambda process, state:
        None if process.index == 1 and process.poll_count < 3 else 0,
    )
    choose = policy.choose_task
    ready_calls = []

    def record_ready(ready, *args, **kwargs):
        ready_calls.append(list(ready))
        if (1, 1) not in state["retired"]:
            assert 0 not in ready
        return choose(ready, *args, **kwargs)

    monkeypatch.setattr(policy, "choose_task", record_ready)
    observations = []
    _run_fake_pool(tasks, observations)
    assert ready_calls[:2] == [[1, 2], [2]]
    assert state["starts"] == [(1, 1), (2, 1), (0, 1)]
    assert state["events"].index(("retire", (1, 1))) < state["events"].index(("start", (0, 1)))
    assert len(observations) == 3


def test_newly_ready_unknown_drains_existing_peer_before_known_tail(tmp_path, monkeypatch):
    tasks = [_task(0, estimate=8 * MIB), _task(1, depends_on=0)]
    tasks.extend(_task(index, estimate=8 * MIB) for index in range(2, 5))

    def result(process, state):
        if process.index == 0 and process.poll_count < 2:
            return None
        if process.index == 2 and process.poll_count < 3:
            return None
        return 0

    state = _fake_processes(tmp_path, monkeypatch, tasks, [0, 2, 3, 4, 1], poll_result=result)
    _run_fake_pool(tasks, [], width=3)
    assert state["starts"] == [(0, 1), (2, 1), (1, 1), (3, 1), (4, 1)]
    assert state["events"].index(("retire", (2, 1))) < state["events"].index(("start", (1, 1)))
    assert any(event == "backoff" for event, *_ in state["admissions"])


def test_successful_calibration_releases_barrier_and_restores_shared_admission(tmp_path, monkeypatch):
    tasks = [_task(index, inputs=[3 - index] * 6, execution_class="split:frontend")
             for index in range(3)]
    state = _fake_processes(
        tmp_path, monkeypatch, tasks, [0, 1, 2],
        poll_result=lambda process, state:
        None if process.index == 1 and process.poll_count == 1 else 0,
    )
    observations = []
    _run_fake_pool(tasks, observations)
    admissions = [(event, index) for event, index, *_ in state["admissions"]
                  if event in ("calibrate", "start")]
    assert admissions == [("calibrate", 0), ("start", 1), ("start", 2)]
    assert state["events"].index(("start", (2, 1))) < state["events"].index(("retire", (1, 1)))
    assert len(observations) == 3
    assert all(item["calibrate_before_peers"] is True for item in tasks)
    assert state["stops"] == []


@pytest.mark.parametrize("failure", ("calibrator", "dependency-with-live-peer"))
def test_failure_cleans_original_owned_processes_and_never_starts_tail(tmp_path, monkeypatch, failure):
    if failure == "calibrator":
        tasks = [_task(0), _task(1, estimate=8 * MIB)]
        order = [1, 0]
    else:
        tasks = [_task(0, estimate=8 * MIB), _task(1, depends_on=0),
                 _task(2, estimate=8 * MIB), _task(3, estimate=8 * MIB)]
        order = [0, 2, 3, 1]

    def result(process, state):
        if process.index == 0:
            return None if failure != "calibrator" and process.poll_count == 1 else 7
        return None

    state = _fake_processes(tmp_path, monkeypatch, tasks, order, poll_result=result)
    observations = []
    with pytest.raises(subprocess.CalledProcessError) as error:
        _run_fake_pool(tasks, observations, width=3)
    assert error.value.returncode == 7
    assert state["starts"] == ([(0, 1)] if failure == "calibrator" else [(0, 1), (2, 1)])
    assert state["stops"] == ([] if failure == "calibrator" else [(2, 1)])
    assert sorted(state["retired"]) == sorted(state["starts"])
    assert observations == []
    assert pool._HOST_WORKERS == {}


def test_cancelled_attempt_retries_before_unknown_head_without_deadlock_or_leaked_owner(tmp_path, monkeypatch):
    tasks = [_task(0, depends_on=1), _task(1, estimate=8 * MIB),
             _task(2, estimate=8 * MIB), _task(3, estimate=8 * MIB)]

    def result(process, state):
        if process.index == 1 and process.poll_count < 4:
            return None
        if process.item == (2, 1):
            return None
        return 0

    def peak(process, state):
        if process.index == 2:
            return (160 if process.attempt == 1 else 180) * MIB
        return MIB

    state = _fake_processes(tmp_path, monkeypatch, tasks, [1, 2, 3, 0],
                            poll_result=result, peak=peak)
    observations = []
    _run_fake_pool(tasks, observations)
    assert state["starts"] == [(1, 1), (2, 1), (2, 2), (0, 1), (3, 1)]
    assert state["stops"] == [(2, 1)]
    assert sorted(state["retired"]) == sorted(state["starts"])
    assert tasks[2]["incomplete_peak_bytes"] == 160 * MIB
    assert tasks[2]["retry_calibration"] is False
    assert tasks[2]["calibrate_before_peers"] is True
    attempts = [process for process in state["processes"].values() if process.index == 2]
    assert attempts[0].token != attempts[1].token
    assert [sample[2] for sample in observations if sample[0] == tasks[2]["class"]] == [180 * MIB]
    assert len(observations) == 4
    launches = [(event, index, demand, available)
                for event, index, demand, available in state["admissions"]
                if event in ("start", "calibrate")]
    assert launches[2] == ("start", 2, 383 * MIB, 383 * MIB)
    assert launches[3] == ("calibrate", 0, 383 * MIB, 383 * MIB)


@pytest.mark.parametrize("case", ("success", "worker-failure", "unverified", "cleanup-failure"))
def test_split_barrier_preserves_verified_handoff_retirement(tmp_path, monkeypatch, case):
    options = {"backend_exit": 7} if case == "worker-failure" else {}
    if case == "unverified":
        options["backend_report"] = "encode-complete"
    if case == "cleanup-failure":
        options["cleanup_error"] = True
    tasks, events, tokens, sidecar = _mock_lifecycle(tmp_path, monkeypatch, **options)
    for item in tasks:
        item["calibrate_before_peers"] = True
    observations = []
    if case == "success":
        _run_mock_pool(tasks, observations)
        assert events.index(("retire", 1)) < events.index(("cleanup", tokens[1]))
        assert events.index(("cleanup", tokens[1])) < events.index(("start", 2))
        assert not sidecar.exists()
        assert len(observations) == 3
    else:
        error = {"worker-failure": subprocess.CalledProcessError,
                 "unverified": policy.WorkerMemoryError, "cleanup-failure": OSError}[case]
        with pytest.raises(error):
            _run_mock_pool(tasks, observations)
        assert ("start", 2) not in events
        assert sidecar.exists()
        assert len(observations) == 1
        if case != "cleanup-failure":
            assert not any(event[0] == "cleanup" for event in events)
