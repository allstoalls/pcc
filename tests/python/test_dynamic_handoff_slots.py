"""Dynamic module capacity uses the existing byte-admission and retry policy."""

import subprocess

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_indexed_handoff as handoff
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 * 1024


def _scenario(tmp_path, monkeypatch, frontend, backend, width, available=1000):
    tasks = []
    for position, (fe, be) in enumerate(zip(frontend, backend)):
        owner = 2 * position
        tasks.extend([
            {"class": "frontend-" + str(position), "inputs": [1] * 6,
             "estimate_bytes": fe * MIB, "startup_prior_model": "host-indexed-frontend-v1",
             "report_path": str(tmp_path / (str(owner) + ".rss")),
             "restartable": True, "depends_on": -1, "handoff_slot": owner},
            {"class": "backend-" + str(position), "inputs": [0] * 47,
             "estimate_bytes": be * MIB,
             "report_path": str(tmp_path / (str(owner + 1) + ".rss")),
             "restartable": True, "depends_on": owner, "handoff_slot": owner,
             "handoff_request": "request-" + str(position), "input_ready": False},
        ])
    state = {
        "tasks": tasks, "events": [], "attempts": {}, "tokens": {}, "pids": {},
        "live": set(), "slots": set(), "sealed": set(), "outputs": set(),
        "released": set(), "cancelled": [], "observations": [], "maximum_slots": 0,
        "retry_occupancy": [],
        "polls": 0, "poll": lambda index, attempt: 0,
        "report": lambda index, attempt: ("complete", MIB, MIB),
        "spawn_failure": -1, "cleanup_failure": -1,
    }
    monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: MIB)
    monkeypatch.setattr(pool, "_command_spec", lambda command: (["mock-worker"], []))
    monkeypatch.setattr(pool.time, "sleep", lambda seconds: None)

    def start(specs, index):
        attempt = state["attempts"].get(index, 0) + 1
        state["attempts"][index] = attempt
        state["events"].append(("start", index, attempt))
        if index == state["spawn_failure"]:
            return -1
        pid = 10000 + index * 10 + attempt
        env = dict(entry.split("=", 1) for entry in specs[index][1])
        state["tokens"][index, attempt] = env[policy.RESOURCE_TOKEN_ENV]
        state["pids"][pid] = index, attempt
        if attempt > 1:
            assert not state["live"], "retry must be exclusive"
            state["retry_occupancy"].append(len(state["slots"]))
        state["live"].add(pid)
        owner = tasks[index]["handoff_slot"]
        if index == owner:
            if attempt == 1:
                assert owner not in state["slots"]
                assert len(state["slots"]) < width
                state["slots"].add(owner)
            else:
                assert owner in state["slots"]
        else:
            assert owner in state["slots"] and owner in state["sealed"]
            assert index not in state["outputs"]
            assert env[handoff.ENV_SEAL] == "a" * 64
            state["outputs"].add(index)
        state["maximum_slots"] = max(state["maximum_slots"], len(state["slots"]))
        return pid

    def poll(pid):
        state["polls"] += 1
        assert state["polls"] < 1000, state["events"]
        index, attempt = state["pids"][pid]
        result = state["poll"](index, attempt)
        if result == 0 and index == tasks[index]["handoff_slot"]:
            state["sealed"].add(index)
        return result

    def report(path, pid, token=""):
        index, attempt = state["pids"][pid]
        assert token == state["tokens"][index, attempt]
        return state["report"](index, attempt)

    def retire(pid):
        index, attempt = state["pids"][pid]
        state["live"].remove(pid)
        state["events"].append(("retire", index, attempt))

    def stop(pid):
        index, attempt = state["pids"][pid]
        state["live"].remove(pid)
        state["cancelled"].append((index, attempt))
        state["events"].append(("cancel", index, attempt))
        assert tasks[index]["handoff_slot"] in state["slots"]

    def prepare(task, token):
        owner = task["handoff_slot"]
        assert owner in state["slots"] and owner in state["sealed"]
        assert token == state["tokens"][owner, state["attempts"][owner]]
        assert ("retire", owner, state["attempts"][owner]) in state["events"]
        task.update(input_ready=True, inputs=[1] * 47,
                    handoff_seal_sha256="a" * 64, source_identity="b" * 64)

    def reset(task):
        owner = task["handoff_slot"]
        assert owner in state["slots"] and owner in state["sealed"]
        state["outputs"].discard(owner + 1)

    def cleanup(task, token):
        owner = task["handoff_slot"]
        index = owner + 1
        attempt = state["attempts"][index]
        assert token == state["tokens"][index, attempt]
        assert ("retire", index, attempt) in state["events"]
        assert owner in state["slots"] and owner in state["sealed"]
        assert index in state["outputs"]
        if index == state["cleanup_failure"]:
            raise OSError("mock handoff cleanup failed")
        state["sealed"].remove(owner)
        state["slots"].remove(owner)
        state["released"].add(index)
        state["events"].append(("release", index, attempt))

    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", retire)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    monkeypatch.setattr(policy, "read_worker_resource", report)
    monkeypatch.setattr(handoff, "prepare_handoff_task", prepare)
    monkeypatch.setattr(handoff, "reset_handoff_output", reset)
    monkeypatch.setattr(handoff, "retire_handoff_task", cleanup)

    def run():
        pool.run_resource_worker_processes(
            ["mock"] * len(tasks), tasks, width,
            (available + 129) * MIB, observations=state["observations"],
        )

    state["run"] = run
    return state


def test_dynamic_slots_reach_fitting_frontends_beyond_blocked_fixed_heads(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, [900, 600, 550, 300, 290, 290],
                      [650, 400, 400, 200, 200, 200], 3)
    # Keep the large BE alive while three smaller modules fully pass through.
    # Both neighboring large FEs exceed its 350 MiB residual, but fit alone.
    state["poll"] = lambda index, attempt: (
        pool._WORKER_RUNNING if index == 1 and not {7, 9, 11} <= state["released"] else 0
    )
    state["run"]()
    events = state["events"]
    assert [event[1] for event in events if event[0] == "start"][:4] == [0, 1, 6, 8]
    assert events.index(("release", 7, 1)) < events.index(("start", 10, 1))
    assert events.index(("release", 11, 1)) < events.index(("start", 2, 1))
    assert events.index(("start", 2, 1)) < events.index(("start", 4, 1))
    assert state["maximum_slots"] == 3
    assert not state["slots"] and not state["sealed"] and not state["live"]
    assert len(state["observations"]) == 12


def test_no_residual_fit_waits_for_retirement_then_runs_largest_frontend(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, [900, 600, 550], [650, 400, 400], 3)
    seen = []

    def poll(index, attempt):
        if index == 1:
            seen.append(index)
            if len(seen) < 3:
                return pool._WORKER_RUNNING
        return 0

    state["poll"] = poll
    state["run"]()
    events = state["events"]
    assert len(seen) == 3
    assert events.index(("release", 1, 1)) < events.index(("start", 2, 1))
    assert events.index(("start", 2, 1)) < events.index(("start", 4, 1))
    assert not state["slots"] and len(state["observations"]) == 6


@pytest.mark.parametrize("failure", ["exit", "unverified", "missing", "cleanup", "fe-spawn", "be-spawn"])
def test_failure_never_releases_slot_or_starts_another_module(tmp_path, monkeypatch, failure):
    state = _scenario(tmp_path, monkeypatch, [400, 300], [350, 200], 1)
    expected = policy.WorkerMemoryError
    if failure == "exit":
        state["poll"] = lambda index, attempt: 7 if index == 1 else 0
        expected = subprocess.CalledProcessError
    elif failure == "unverified":
        state["report"] = lambda index, attempt: ("encoding" if index == 1 else "complete", MIB, MIB)
    elif failure == "missing":
        state["report"] = lambda index, attempt: None if index == 1 else ("complete", MIB, MIB)
    elif failure == "cleanup":
        state["cleanup_failure"] = 1
        expected = OSError
    else:
        state["spawn_failure"] = 0 if failure == "fe-spawn" else 1
        expected = subprocess.CalledProcessError
    with pytest.raises(expected):
        state["run"]()
    assert not any(event[0] == "release" or event[:2] == ("start", 2) for event in state["events"])
    assert len(state["observations"]) == (0 if failure == "fe-spawn" else 1)
    assert state["slots"] == (set() if failure == "fe-spawn" else {0})
    assert not state["live"]


@pytest.mark.parametrize("cancel_phase", ["frontend", "backend"])
def test_cancelled_attempt_keeps_slot_through_one_exclusive_retry(tmp_path, monkeypatch, cancel_phase):
    if cancel_phase == "frontend":
        state = _scenario(tmp_path, monkeypatch, [600, 300], [500, 200], 2)
        leader, cancelled, release, completions = 0, 2, 3, 4
    else:
        state = _scenario(tmp_path, monkeypatch, [900, 600, 550, 300], [650, 400, 400, 200], 2)
        leader, cancelled, release, completions = 1, 7, 7, 8

    def poll(index, attempt):
        if index == leader and not state["cancelled"]:
            return pool._WORKER_RUNNING
        if index == cancelled and attempt == 1:
            return pool._WORKER_RUNNING
        return 0

    def report(index, attempt):
        peak = 250 * MIB if (index, attempt) == (cancelled, 1) else MIB
        return "complete", peak, peak

    state["poll"] = poll
    state["report"] = report
    state["run"]()
    events = state["events"]
    assert state["cancelled"] == [(cancelled, 1)]
    assert state["tokens"][cancelled, 1] != state["tokens"][cancelled, 2]
    assert events.index(("retire", leader, 1)) < events.index(("start", cancelled, 2))
    if cancel_phase == "frontend":
        # The leader FE has finished but its BE has not consumed the slot.
        # A retry must reuse its own slot even when every slot is occupied.
        assert state["retry_occupancy"] == [2]
        assert events.index(("start", cancelled, 2)) < events.index(("start", 1, 1))
    else:
        assert events.index(("start", cancelled, 2)) < events.index(("start", 2, 1))
    assert [event for event in events if event[:2] == ("release", release)] == [
        ("release", release, 2 if cancel_phase == "backend" else 1),
    ]
    assert state["maximum_slots"] == 2 and not state["slots"]
    task = state["tasks"][cancelled]
    assert task["incomplete_peak_bytes"] == 250 * MIB
    assert task["retry_calibration"] is False
    assert len([sample for sample in state["observations"] if sample[0] == task["class"]]) == 1
    assert len(state["observations"]) == completions


@pytest.mark.parametrize("damage", ["bool", "negative", "outside", "missing-be", "missing-fe",
                                     "duplicate", "chained-fe", "wrong-dependency", "no-handoff"])
def test_malformed_slot_inventory_fails_before_any_worker(tmp_path, monkeypatch, damage):
    state = _scenario(tmp_path, monkeypatch, [400, 300], [350, 200], 2)
    tasks = state["tasks"]
    if damage in ("bool", "negative", "outside"):
        tasks[0]["handoff_slot"] = {"bool": True, "negative": -1, "outside": len(tasks)}[damage]
    elif damage == "missing-be":
        del tasks[1]["handoff_slot"]
    elif damage == "missing-fe":
        del tasks[0]["handoff_slot"]
    elif damage == "duplicate":
        tasks[3]["handoff_slot"] = 0
    elif damage == "chained-fe":
        tasks[2]["depends_on"] = 1
    elif damage == "wrong-dependency":
        tasks[1]["depends_on"] = 2
    else:
        del tasks[1]["handoff_request"]
    with pytest.raises(policy.WorkerMemoryError, match="handoff slot"):
        state["run"]()
    assert not state["events"] and not state["observations"]


def test_oversized_modeled_head_still_drains_then_calibrates_exclusively():
    tasks = [
        {"class": "large", "inputs": [1] * 6, "estimate_bytes": 1200 * MIB,
         "startup_prior_model": "host-indexed-frontend-v1", "handoff_slot": 0},
        {"class": "small", "inputs": [1] * 6, "estimate_bytes": 300 * MIB,
         "startup_prior_model": "host-indexed-frontend-v1", "handoff_slot": 1},
    ]
    assert policy.choose_task([0, 1], tasks, [], [100 * MIB], 3, 1000 * MIB,
                              guarded_calibration=True) == (-1, 0, False)
    assert policy.choose_task([0, 1], tasks, [], [], 3, 1000 * MIB,
                              guarded_calibration=True) == (0, 1200 * MIB, True)


def test_legacy_dependency_inventory_needs_no_slot_metadata():
    tasks = [{"depends_on": -1}, {"depends_on": 0}, {"depends_on": 1}]
    policy.validate_handoff_slots(tasks)
