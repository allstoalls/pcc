"""Cold native ordering changes no admission rule or completed-sample boundary."""

import copy
import itertools
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 ** 2
LEGACY_LAUNCHES = [2, 15, 7, 9, 13, 10, 1, 12, 8, 14, 11, 0, 6, 5, 3, 4]


def test_existing_focused_host_ci_selects_this_regression_with_normal_defaults():
    workflow = (Path(__file__).resolve().parents[2]
                / ".github/workflows/pcc1-package-parity.yml").read_text()
    path = "tests/python/test_native_cold_cover_order.py"
    assert workflow.count('      - "' + path + '"\n') == 2
    command = next(line for line in workflow.splitlines()
                   if "--junitxml=build/worker-prior-focused-host.xml" in line)
    assert "python -m pytest -x -q " in command and path in command.split()
    assert "--noconftest" not in command and "addopts=" not in command
    assert " -n" not in command and "--dist" not in command


def _task(source, ast, index, export=451735):
    return {
        "class": "private-native-codegen-invocation",
        "inputs": [source, source, ast, ast, export, 1],
        "estimate_bytes": 0, "report_path": "unused", "restartable": True,
        "diagnostic_phase": "codegen", "diagnostic_indices": [index],
        "diagnostic_modules": ["module" + str(index)],
    }


def _inventory():
    # Source/AST byte pairs from the completed frontend admission records in
    # the 591 ARM prior receipt. No module name or fixture identity selects
    # the production policy. This models ordering, not compiler time or RSS.
    pairs = [
        (12271, 626699), (8320, 92700), (145, 976), (38227, 1091637),
        (33914, 1057900), (30687, 836534), (33715, 981201), (2306, 81417),
        (7909, 258568), (2556, 74176), (4381, 120063), (13357, 619430),
        (8415, 250468), (2384, 69465), (39236, 622672), (448, 1574),
    ]
    return [_task(source, ast, index) for index, (source, ast) in enumerate(pairs)]


def _ordered(tasks, native=True, guarded=True):
    return policy.resource_task_order(
        tasks, guarded_calibration=guarded, native_cold_cover=native,
    )


def _run_pool(tmp_path, monkeypatch, *, native=True, guarded=True,
              observations=None, available_mib=1024, fail=False,
              incomplete=False, stale=False, growth=False):
    tasks = _inventory()
    before = copy.deepcopy(tasks)
    observations = [] if observations is None else observations
    initial_samples = len(observations)
    clock = [0.0]
    records = {}
    active = set()
    events = []
    attempts = {}
    stopped = []
    trace = tmp_path / "admission.tsv"
    for index, task in enumerate(tasks):
        task["report_path"] = str(tmp_path / ("rss" + str(index)))
    monkeypatch.delenv(policy.TREE_RECOVERY_ENV, raising=False)
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")
    if guarded:
        monkeypatch.setenv(policy.TREE_STATE_ENV, str(tmp_path / "guard-state"))
    else:
        monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    monkeypatch.setattr(pool.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pool.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: MIB)

    def peak(pid):
        record = records[pid]
        if growth and record["index"] == 4 and clock[0] >= record["end"] - .08:
            return 400 * MIB
        return 64 * MIB

    def state(path, budget, owner, pids, not_before=0.0, include_owner=False, diagnostic=None):
        assert include_owner and not_before <= clock[0]
        assert set(pids) <= active
        if not path or stale:
            return None
        return 0, {owner: MIB, **{pid: peak(pid) for pid in pids}}

    def start(specs, index):
        assert specs[index][0] == ["command" + str(index)]
        assert len(active) < 4
        # A live/incomplete worker cannot create a covering observation.
        if active:
            assert policy.estimated_task_bytes(tasks[index], observations) > 0
        pid = 100 + len(records)
        attempts[index] = attempts.get(index, 0) + 1
        records[pid] = {"index": index, "end": clock[0] + .12}
        active.add(pid)
        events.append(("start", index, pid, clock[0], len(active)))
        return pid

    def poll(pid):
        if clock[0] < records[pid]["end"]:
            return pool._WORKER_RUNNING
        return 7 if fail and records[pid]["index"] == 3 else 0

    def report(path, pid, token):
        assert token and path == tasks[records[pid]["index"]]["report_path"]
        complete = clock[0] >= records[pid]["end"]
        phase = "complete" if complete and not incomplete else "live"
        return phase, peak(pid), peak(pid)

    def retire(pid):
        active.remove(pid)
        events.append(("retire", records[pid]["index"], pid, clock[0], len(active)))

    def stop(pid):
        active.remove(pid)
        stopped.append(pid)
        events.append(("stop", records[pid]["index"], pid, clock[0], len(active)))

    monkeypatch.setattr(policy, "read_tree_state", state)
    monkeypatch.setattr(policy, "read_worker_resource", report)
    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", retire)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    error = None
    with monkeypatch.context() as runtime:
        runtime.setattr(sys.implementation, "name", "pcc" if native else "cpython")
        try:
            pool.run_resource_worker_processes(
                ["command" + str(i) for i in range(len(tasks))], tasks, 4,
                MIB + policy.RSS_HEADROOM_BYTES + available_mib * MIB,
                observations=observations, trace_path=str(trace),
            )
        except (policy.WorkerMemoryError, subprocess.CalledProcessError) as caught:
            error = caught
    assert not active
    rows = [line.split("\t") for line in trace.read_text().splitlines()] if trace.exists() else []
    for index, item in enumerate(tasks):
        assert item["inputs"] == before[index]["inputs"]
        assert item["diagnostic_indices"] == [index]
    return tasks, rows, events, attempts, observations[initial_samples:], stopped, error


def test_native_cold_sparse_pool_reuses_only_two_retired_covering_samples(tmp_path, monkeypatch):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(tmp_path, monkeypatch)
    assert error is None and not stopped
    assert attempts == {index: 1 for index in range(16)}
    assert [int(row[1]) for row in rows if row[0] == "calibrate"] == [3, 14]
    first_retire = next(i for i, row in enumerate(rows) if row[:2] == ["retire", "3"])
    starts = [i for i, row in enumerate(rows) if row[0] == "start"]
    assert len(starts) == 14 and min(starts) > first_retire
    assert max(event[4] for event in events) == 4
    assert len(observations) == 16
    assert all(int(rows[i][3]) == policy.peak_reservation(64 * MIB) for i in starts)
    assert policy.estimated_task_bytes(tasks[14], [observations[0]]) == 0


@pytest.mark.parametrize("native,guarded", [(False, True), (True, False), (False, False)])
def test_host_or_unguarded_pool_keeps_the_original_twelve_calibrations(tmp_path, monkeypatch, native, guarded):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, native=native, guarded=guarded,
    )
    assert error is None and not stopped and len(observations) == 16
    assert len([row for row in rows if row[0] == "calibrate"]) == 12
    assert max(event[4] for event in events) == 1
    # The existing chooser admits known item 11 before unknown item 0 in
    # their shared band; static pending order alone is not launch order.
    assert [int(row[1]) for row in rows if row[0] in ("calibrate", "start")] == LEGACY_LAUNCHES


@pytest.mark.parametrize("same_class", [False, True])
def test_pool_only_treats_its_own_class_observations_as_warm(tmp_path, monkeypatch, same_class):
    tasks = _inventory()
    sample = (tasks[0]["class"] if same_class else "other-invocation", [50000] * 4 + [451735, 1], 64 * MIB)
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, observations=[sample],
    )
    assert error is None and not stopped
    launches = [int(row[1]) for row in rows if row[0] in ("calibrate", "start")]
    if same_class:
        assert launches == LEGACY_LAUNCHES
        assert launches[0] != 3
    else:
        assert [int(row[1]) for row in rows if row[0] == "calibrate"] == [3, 14]


@pytest.mark.parametrize("sample", [(), ("other-invocation",), ("other-invocation", [], 1, None, None)])
def test_invalid_observation_keeps_the_existing_error_contract(tmp_path, monkeypatch, sample):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, observations=[sample],
    )
    assert isinstance(error, policy.WorkerMemoryError)
    assert str(error) == "worker observation shape differs"
    assert not attempts and not events and not observations


@pytest.mark.parametrize("fault", ["failure", "incomplete", "stale"])
def test_invalid_completion_or_guard_cannot_admit_peers(tmp_path, monkeypatch, fault):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, fail=fault == "failure", incomplete=fault == "incomplete", stale=fault == "stale",
    )
    assert error is not None and not observations
    assert attempts == ({} if fault == "stale" else {3: 1})
    if fault == "failure":
        assert isinstance(error, subprocess.CalledProcessError) and error.returncode == 7
    elif fault == "incomplete":
        assert "did not publish final peak RSS" in str(error)
    else:
        assert "missing, stale, or incompatible" in str(error)


def test_covering_forecast_above_budget_still_requires_exclusive_calibration(tmp_path, monkeypatch):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, available_mib=190,
    )
    assert error is None and not stopped and len(observations) == 16
    assert max(event[4] for event in events) == 1
    assert len([row for row in rows if row[0] == "calibrate"]) == 16
    assert not [row for row in rows if row[0] == "start"]
    assert policy.estimated_task_bytes(tasks[0], observations) == policy.peak_reservation(64 * MIB) > 190 * MIB


def test_live_growth_keeps_cancelled_attempt_out_of_completed_samples(tmp_path, monkeypatch):
    tasks, rows, events, attempts, observations, stopped, error = _run_pool(
        tmp_path, monkeypatch, available_mib=600, growth=True,
    )
    assert error is None and len(stopped) == 1 and len(observations) == 16
    cancelled = [int(row[1]) for row in rows if row[0] == "cancel"]
    assert len(cancelled) == 1 and attempts[cancelled[0]] == 2
    assert all(count == 1 for index, count in attempts.items() if index != cancelled[0])
    assert tasks[cancelled[0]]["incomplete_peak_bytes"] == 64 * MIB
    retry = [i for i, row in enumerate(rows) if row[:2] == ["start", str(cancelled[0])]][-1]
    grown_retire = next(i for i, row in enumerate(rows) if row[:2] == ["retire", "4"])
    assert retry > grown_retire
    assert policy.estimated_task_bytes(tasks[0], observations) >= policy.peak_reservation(400 * MIB)


def test_native_flag_changes_only_cold_guarded_sparse_order_without_mutation():
    tasks = _inventory()
    before = copy.deepcopy(tasks)
    old = policy.resource_task_order(tasks, guarded_calibration=True)
    assert _ordered(tasks, native=False) == old
    assert _ordered(tasks, guarded=False) == policy.resource_task_order(tasks)
    order, bands = _ordered(tasks)
    assert bands == []
    assert order == sorted(range(len(tasks)), key=lambda i: (-sum(tasks[i]["inputs"]), i))
    assert order[0] == 3 and tasks == before
    assert all(policy.estimated_task_bytes(item, []) == 0 for item in tasks)


@pytest.mark.parametrize("change", ["estimate", "incomplete", "retry", "barrier", "non-int", "phase", "class", "batch", "dependency", "lazy", "exports", "model"])
def test_ineligible_inventory_preserves_baseline_order(change):
    tasks = _inventory()
    if change == "estimate": tasks[0]["estimate_bytes"] = MIB
    elif change == "incomplete": tasks[0]["incomplete_peak_bytes"] = MIB
    elif change == "retry": tasks[0]["retry_calibration"] = True
    elif change == "barrier": tasks[0]["calibrate_before_peers"] = True
    elif change == "non-int":
        tasks[0]["inputs"][0] = float(tasks[0]["inputs"][0])
        tasks[0]["inputs"][1] = tasks[0]["inputs"][0]
    elif change == "phase": tasks[0]["diagnostic_phase"] = "summary"
    elif change == "class": tasks[0]["class"] = "other-invocation"
    elif change == "batch": tasks[0]["diagnostic_indices"] = [0, 1]
    elif change == "dependency": tasks[0]["depends_on"] = 1
    elif change == "lazy": tasks[0]["input_path"] = "not-ready"
    elif change == "exports": tasks[0]["inputs"][4] += 1
    else: tasks[0]["startup_prior_model"] = "host-codegen-v1"
    assert _ordered(tasks) == policy.resource_task_order(tasks, guarded_calibration=True)


def test_parallel_cohort_keeps_its_existing_small_first_order():
    tasks = [_task(source, ast, i) for i, (source, ast) in enumerate([(8, 7), (7, 7), (8, 6), (40, 40)])]
    old = policy.resource_task_order(tasks, guarded_calibration=True)
    assert old[1] and _ordered(tasks) == old


def test_covered_task_is_counted_once_even_with_two_covering_peers():
    tasks = [_task(source, ast, i) for i, (source, ast) in enumerate([(9, 6), (6, 9), (6, 6), (40, 40)])]
    old = policy.resource_task_order(tasks, guarded_calibration=True)
    assert old[1]
    assert _ordered(tasks) == ([3, 0, 1, 2], [])


def test_duplicate_peers_are_both_covered_and_keep_old_order():
    tasks = [_task(source, ast, i) for i, (source, ast) in enumerate([(8, 7), (8, 7), (40, 40)])]
    assert _ordered(tasks) == policy.resource_task_order(tasks, guarded_calibration=True)


def test_third_incomparable_maximum_preserves_original_cohorts():
    tasks = [_task(source, ast, i) for i, (source, ast) in enumerate([(40, 20), (30, 30), (20, 40), (6, 6), (6, 5)])]
    old = policy.resource_task_order(tasks, guarded_calibration=True)
    assert old[1] and _ordered(tasks) == old


def test_existing_singleton_cover_optimization_is_unchanged():
    tasks = [_task(value, value, i) for i, value in enumerate([1, 4, 16, 64, 256])]
    assert _ordered(tasks) == policy.resource_task_order(tasks, guarded_calibration=True) == ([4, 3, 2, 1, 0], [])


def test_sparse_recognition_matches_quadratic_reference_with_ties_and_permutations():
    # All 9**4 ordered inventories include duplicates and every permutation.
    # This reference directly counts undominated vectors and distinct covered
    # tasks; it does not reuse the production prefix/frontier algorithm.
    grid = list(itertools.product((1, 2, 3), repeat=2))
    for pairs in itertools.product(grid, repeat=4):
        tasks = [_task(source, ast, index) for index, (source, ast) in enumerate(pairs)]
        pending = sorted(range(4), key=lambda index: (-sum(tasks[index]["inputs"]), index))
        bands = []
        for source, ast in pairs:
            size = source + ast
            bits = size.bit_length()
            bands.append(2 * bits + int(4 * size >= 3 * (1 << bits)))
        maxima = {
            tuple(task["inputs"]) for task in tasks
            if not any(other["inputs"] != task["inputs"]
                       and policy.input_envelope_covers(other["inputs"], task["inputs"])
                       for other in tasks)
        }
        sparse = all(
            sum(any(peer != index and bands[peer] == band
                    and policy.input_envelope_covers(tasks[peer]["inputs"], tasks[index]["inputs"])
                    for peer in range(4))
                for index in range(4) if bands[index] == band) <= 1
            for band in set(bands)
        )
        expected = len(maxima) <= 2 and len(maxima) < len(tasks) and sparse
        assert policy._sparse_native_cover_order(tasks, pending, bands) == expected, pairs
