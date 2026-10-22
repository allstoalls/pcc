"""One guard snapshot authorizes live continuation, never future RSS safety."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy
from pcc.frontends.python import worker_resource_host_recovery as host_recovery


@pytest.fixture(autouse=True)
def _legacy_recovery_environment(monkeypatch):
    # Pure virtual clocks must not inherit a real enclosing guard's deadline.
    # Cases opting in below install their own immutable synthetic envelope.
    monkeypatch.delenv(policy.TREE_RECOVERY_ENV, raising=False)


MIB = 1024 * 1024


def _snapshot(path, *, stamp=100.0, budget=1000, owner=11, owner_rss=100,
              worker=12, worker_parent=11, worker_rss=200, descendant=True):
    rows = [f"10\t0\t50", f"{owner}\t10\t{owner_rss}"]
    if worker_rss is not None:
        rows.append(f"{worker}\t{worker_parent}\t{worker_rss}")
    if descendant:
        rows.append(f"13\t{worker}\t30")
    path.write_text(policy.TREE_STATE_SCHEMA + "\n" + str(stamp) + "\n"
                    + str(budget) + "\n" + "\n".join(rows) + "\n")


def test_current_owner_is_an_opt_in_from_the_same_single_read(tmp_path, monkeypatch):
    state = tmp_path / "tree"
    _snapshot(state)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: 100.5))
    assert policy.read_tree_state(str(state), 1000, 11, [12]) == (50, {12: 230})
    opened = []
    actual_open = open

    def counted_open(*args, **kwargs):
        opened.append(args[0])
        return actual_open(*args, **kwargs)

    monkeypatch.setattr(policy, "open", counted_open, raising=False)
    detail = {}
    assert policy.read_tree_state(str(state), 1000, 11, [12], include_owner=True, diagnostic=detail) == (
        50, {11: 100, 12: 230},
    )
    assert opened == [str(state)]
    assert detail["reason"] == "accepted"
    assert (detail["owner_parent"], detail["owner_rss"], detail["observed_budget"]) == (10, 100, 1000)


@pytest.mark.parametrize("change", [
    {"worker_rss": None}, {"worker_rss": 0}, {"worker_parent": 10},
    {"worker": 11}, {"owner_rss": 0}, {"owner": 14},
    {"stamp": 97.0}, {"stamp": 101.0}, {"budget": 999},
])
def test_continuation_requires_fresh_positive_owned_roots(tmp_path, monkeypatch, change):
    state = tmp_path / "tree"
    _snapshot(state, **change)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: 100.5))
    assert policy.read_tree_state(str(state), 1000, 11, [12], include_owner=True) is None


def test_missing_worker_stays_outside_and_cannot_authorize_continuation(tmp_path, monkeypatch):
    state = tmp_path / "tree"
    _snapshot(state, worker_rss=None)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: 100.5))
    # The old projection already charges this orphan to outside RSS. The
    # opt-in path additionally requires the active root itself to be measured.
    assert policy.read_tree_state(str(state), 1000, 11, [12]) == (80, {12: 0})
    assert policy.read_tree_state(str(state), 1000, 11, [12], include_owner=True) is None
    assert policy.read_tree_state(str(state), 1000, 11, [11], include_owner=True) is None


def _run_mock_pool(tmp_path, monkeypatch, *, budget, owner_peak, owner_current,
                   worker_peak, worker_current, outside=32 * MIB, peer=False,
                   with_state=True):
    """Run the real policy/report parsers; replace only time and processes."""
    owner_pid = os.getpid()
    tree = tmp_path / "tree"
    trace = tmp_path / "events"
    live, all_workers, stopped, reaped, observations = {}, {}, [], [], []
    tasks = [{"class": "current-rss-first", "inputs": [2], "estimate_bytes": 0,
              "report_path": str(tmp_path / "rss0"), "restartable": True,
              "calibrate_before_peers": True}]
    if peer:
        tasks.append({"class": "current-rss-peer", "inputs": [1],
                      "estimate_bytes": 64 * MIB, "report_path": str(tmp_path / "rss1"),
                      "restartable": True})

    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds
            write_tree()

    clock = Clock()

    def write_tree():
        rows = [f"{owner_pid + 500}\t0\t{outside}",
                f"{owner_pid}\t{owner_pid + 500}\t{owner_current}"]
        for pid, item in live.items():
            current = worker_current if item["index"] == 0 else 16 * MIB
            rows.append(f"{pid}\t{owner_pid}\t{current}")
        tree.write_text(policy.TREE_STATE_SCHEMA + "\n" + str(clock.now) + "\n"
                        + str(budget) + "\n" + "\n".join(rows) + "\n")

    def report(item, phase):
        peak = worker_peak if item["index"] == 0 else 16 * MIB
        # CPython's resource publisher uses its high-water reading in both
        # report fields. Actual current RSS comes only from the guard file.
        with open(tasks[item["index"]]["report_path"], "w") as stream:
            stream.write(policy.RESOURCE_REPORT_SCHEMA + "\n" + str(item["pid"])
                         + "\n" + phase + "\n" + str(peak) + "\n" + str(peak)
                         + "\n" + item["token"] + "\n")

    def start(specs, index):
        pid = owner_pid + 100 + index
        token = next(value.split("=", 1)[1] for value in specs[index][1]
                     if value.startswith(policy.RESOURCE_TOKEN_ENV + "="))
        assert len(token) == 64
        item = {"pid": pid, "index": index, "polls": 0, "token": token}
        live[pid] = all_workers[pid] = item
        report(item, "assemble-complete")
        write_tree()
        return pid

    def poll(pid):
        item = all_workers[pid]
        item["polls"] += 1
        if item["polls"] < 3:
            return pool._WORKER_RUNNING
        report(item, "complete")
        return 0

    def retire(pid):
        reaped.append(pid)
        live.pop(pid)
        write_tree()

    def stop(pid):
        stopped.append(pid)
        live.pop(pid)

    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: owner_peak)
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(pool, "time", clock)
    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", retire)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")
    if with_state:
        monkeypatch.setenv(policy.TREE_STATE_ENV, str(tree))
    else:
        monkeypatch.delenv(policy.TREE_STATE_ENV, raising=False)
    write_tree()
    failure = None
    try:
        pool.run_resource_worker_processes(
            ["unused-command" for _ in tasks], tasks, 2, budget,
            observations=observations, trace_path=str(trace),
        )
    except policy.WorkerMemoryError as error:
        failure = str(error)
    events = [line.split("\t") for line in trace.read_text().splitlines()] if trace.exists() else []
    assert not live
    return failure, observations, events, stopped, reaped, tasks


@pytest.mark.parametrize("owner_peak,owner_current,worker_current", [
    (192 * MIB, 64 * MIB, 400 * MIB),
    (192 * MIB, 192 * MIB, 250 * MIB),
])
def test_nonoverlapping_peaks_do_not_stop_safe_exclusive_work(
    tmp_path, monkeypatch, owner_peak, owner_current, worker_current,
):
    failure, observations, events, stopped, reaped, tasks = _run_mock_pool(
        tmp_path, monkeypatch, budget=640 * MIB, owner_peak=owner_peak,
        owner_current=owner_current, worker_peak=400 * MIB,
        worker_current=worker_current, peer=True,
    )
    assert failure is None and not stopped and len(reaped) == 2
    first_retire = next(row for row in events if row[:2] == ["retire", "0"])
    assert int(first_retire[3]) == policy.peak_reservation(400 * MIB) == 628 * MIB
    assert int(first_retire[5]) == observations[0][2] == 400 * MIB
    assert events[0][0] == "calibrate"
    peer_start = next(row for row in events if row[1] == "1" and row[0] in ("start", "calibrate"))
    assert events.index(first_retire) < events.index(peer_start)
    assert not any(row[0] == "cancel" for row in events)
    # The correction does not fit cancelled peaks or relax a later retry's
    # same-task lower bound. A new run with that actual floor still refuses.
    tasks[0]["incomplete_peak_bytes"] = 400 * MIB
    assert policy.minimum_task_bytes(tasks[0]) == 400 * MIB
    with pytest.raises(policy.WorkerMemoryError, match="cannot admit"):
        policy.require_task_fits(0, policy.minimum_task_bytes(tasks[0]), 288 * MIB, 640 * MIB)


@pytest.mark.parametrize("extra,accepted", [(0, True), (1, False), (2 * MIB, False)])
def test_simultaneous_limit_preserves_the_full_headroom(tmp_path, monkeypatch, extra, accepted):
    failure, observations, events, stopped, reaped, _tasks = _run_mock_pool(
        tmp_path, monkeypatch, budget=512 * MIB, owner_peak=64 * MIB,
        owner_current=64 * MIB, worker_peak=300 * MIB,
        worker_current=288 * MIB + extra,
    )
    if accepted:
        assert failure is None and len(reaped) == 1 and not stopped
        assert observations[0][2] == 300 * MIB
    else:
        assert "live worker current subtree exceeds safe worker space" in failure
        assert "current_owner_rss_bytes=" + str(64 * MIB) in failure
        assert "current_worker_subtree_rss_bytes=" + str(288 * MIB + extra) in failure
        assert "available_worker_bytes=" + str(288 * MIB) in failure
        assert len(stopped) == 1 and not reaped and observations == []
        assert not any(row[0] == "retire" for row in events)


def test_current_crossing_is_checked_even_when_padded_forecast_fits(tmp_path, monkeypatch):
    # A native current-owner getter may be below an earlier accepted guard
    # sample. This case would be missed by checking only forecast promotion.
    assert policy.peak_reservation(225 * MIB) < 640 * MIB - 64 * MIB - 32 * MIB - 128 * MIB
    failure, observations, _events, stopped, reaped, _tasks = _run_mock_pool(
        tmp_path, monkeypatch, budget=640 * MIB, owner_peak=64 * MIB,
        owner_current=256 * MIB, worker_peak=225 * MIB, worker_current=225 * MIB,
    )
    assert "live worker current subtree exceeds safe worker space" in failure
    assert "available_worker_bytes=" + str(224 * MIB) in failure
    assert len(stopped) == 1 and not observations and not reaped


def test_no_guard_does_not_receive_current_rss_relief(tmp_path, monkeypatch):
    failure, observations, _events, stopped, reaped, _tasks = _run_mock_pool(
        tmp_path, monkeypatch, budget=640 * MIB, owner_peak=192 * MIB,
        owner_current=64 * MIB, worker_peak=400 * MIB, worker_current=250 * MIB,
        with_state=False,
    )
    assert "exclusive calibration exceeded safe worker space" in failure
    assert len(stopped) == 1 and not observations and not reaped


def test_exhausted_historical_owner_reserve_still_blocks_admission(tmp_path, monkeypatch):
    failure, observations, events, stopped, reaped, _tasks = _run_mock_pool(
        tmp_path, monkeypatch, budget=640 * MIB, owner_peak=500 * MIB,
        owner_current=64 * MIB, worker_peak=100 * MIB, worker_current=100 * MIB,
    )
    assert "worker memory budget cannot admit one task" in failure
    assert not events and not stopped and not reaped and not observations


@pytest.mark.parametrize("case,initial_time,initial_gap,start_delay,gaps,finish_after,fails", [
    ("slow-start-publication-lag", 100.0, 0.0, 3.0, ((0.0, 0.5),), 1.0, False),
    ("slow-start-continuous-outage", 100.0, 0.0, 3.0, ((0.0, 10.0),), 9.0, True),
    ("recovery-resets-outage", 100.0, 0.0, 0.0, ((0.0, 1.25), (1.75, 3.0)), 3.5, False),
    ("first-read-unavailable-at-zero", 0.0, 0.5, 0.0, (), 0.5, False),
    ("continuous-outage-at-zero", 0.0, 10.0, 0.0, (), 0.5, True),
])
def test_unavailable_window_starts_at_first_rejected_snapshot(
    tmp_path, monkeypatch, case, initial_time, initial_gap, start_delay,
    gaps, finish_after, fails,
):
    # Use the real file parser, but virtual time/processes. Unlike the eager
    # mock above, a freshly launched child can be absent from a valid guard
    # snapshot until the next publication. None still authorizes no admission.
    owner = os.getpid()
    child = owner + 100
    budget = 512 * MIB
    tree = tmp_path / "tree"
    report_path = tmp_path / "worker.rss"
    clock = SimpleNamespace(now=initial_time)
    live = {}
    started, stopped, reaped, rejected, accepted, observations = [], [], [], [], [], []
    last_publication = [initial_time - 1.0]

    def publish_tree(force=False):
        if not force and clock.now - last_publication[0] < 0.25:
            return
        last_publication[0] = clock.now
        if clock.now - initial_time < initial_gap:
            tree.write_text("no usable snapshot yet\n")
            return
        rows = [f"{owner}\t0\t{64 * MIB}"]
        if live:
            elapsed = clock.now - live["started"]
            if not any(low <= elapsed < high for low, high in gaps):
                rows.append(f"{child}\t{owner}\t{16 * MIB}")
        tree.write_text(policy.TREE_STATE_SCHEMA + "\n" + str(clock.now) + "\n"
                        + str(budget) + "\n" + "\n".join(rows) + "\n")

    def sleep(seconds):
        clock.now += seconds
        assert clock.now - initial_time < 15.0, "outage timer failed to terminate"
        publish_tree()

    def publish_report(phase):
        report_path.write_text(policy.RESOURCE_REPORT_SCHEMA + "\n" + str(child)
                               + "\n" + phase + "\n" + str(16 * MIB) + "\n"
                               + str(16 * MIB) + "\n" + live["token"] + "\n")

    def start(specs, index):
        assert index == 0 and not live and not started
        # Model work since the last accepted observation, then a fresh guard
        # sample which precedes discovery of this new direct child.
        clock.now += start_delay
        token = next(value.split("=", 1)[1] for value in specs[index][1]
                     if value.startswith(policy.RESOURCE_TOKEN_ENV + "="))
        live.update(started=clock.now, token=token)
        started.append(clock.now)
        publish_report("started")
        publish_tree(force=True)
        return child

    def poll(pid):
        assert pid == child and live
        if clock.now - live["started"] < finish_after:
            return pool._WORKER_RUNNING
        publish_report("complete")
        return 0

    def retire(pid):
        assert pid == child
        reaped.append(pid)
        live.clear()

    def stop(pid):
        assert pid == child
        stopped.append(pid)
        live.clear()

    original_reader = policy.read_tree_state

    def read_state(*args, **kwargs):
        result = original_reader(*args, **kwargs)
        (rejected if result is None else accepted).append(clock.now)
        return result

    clock.monotonic = lambda: clock.now
    clock.sleep = sleep
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(pool, "time", clock)
    monkeypatch.setattr(policy, "read_tree_state", read_state)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 64 * MIB)
    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", retire)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    monkeypatch.setenv(policy.TREE_STATE_ENV, str(tree))
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")
    publish_tree(force=True)
    failure = None
    try:
        pool.run_resource_worker_processes(
            ["unused-command"], [{"class": "publication-lag", "inputs": [1],
                                  "estimate_bytes": 64 * MIB,
                                  "report_path": str(report_path)}],
            1, budget, observations=observations,
        )
    except policy.WorkerMemoryError as error:
        failure = str(error)
    assert rejected and not live, case
    if fails:
        prefix = "worker tree RSS state is missing, stale, or incompatible; last_rejected_snapshot="
        assert failure.startswith(prefix)
        detail_text, active_text = failure[len(prefix):].split("; active_tasks=", 1)
        detail = ast.literal_eval(detail_text)
        assert detail["reason"] == ("active_missing" if started else "header")
        assert detail["expected_budget"] == budget and detail["owner_pid"] == owner
        assert ast.literal_eval(active_text) == ([(0, child)] if started else [])
        assert policy.STATE_MAX_AGE_SECONDS <= clock.now - rejected[0] < 2.03
        assert not observations and not reaped
        assert stopped == ([child] if started else [])
    else:
        assert failure is None and reaped == [child] and not stopped
        assert len(observations) == 1 and observations[0][2] == 16 * MIB
        assert any(at > rejected[0] for at in accepted)
        if initial_gap:
            assert started[0] >= initial_time + initial_gap


@pytest.mark.parametrize("case,reason", [
    ("empty-path", "empty_path"), ("missing-file", "read"),
    ("short-header", "header"), ("wrong-schema", "header"),
    ("timestamp", "timestamp"), ("clock", "clock"),
    ("stale", "age"), ("future", "age"), ("barrier", "before_barrier"),
    ("budget-integer", "budget"), ("budget-mismatch", "budget"),
    ("row-fields", "row_fields"), ("row-integer", "row_integer"),
    ("negative-pid", "row_identity"), ("negative-parent", "row_identity"),
    ("negative-rss", "row_identity"), ("duplicate-pid", "row_identity"),
    ("owner-missing", "owner_missing"), ("owner-rss", "owner_rss"),
    ("active-owner", "active_owner"), ("active-missing", "active_missing"),
    ("active-parent", "active_parent"), ("active-rss", "active_rss"),
])
def test_tree_rejection_diagnostic_preserves_each_old_rejection(tmp_path, monkeypatch, case, reason):
    state = tmp_path / "state"
    _snapshot(state)
    lines = state.read_text().splitlines()
    path, owner, active, barrier = str(state), 11, [12], 0.0
    if case == "empty-path":
        path = ""
    elif case == "missing-file":
        path = str(tmp_path / "missing")
    elif case == "short-header":
        lines = lines[:3]
    elif case == "wrong-schema":
        lines[0] = "incompatible-schema"
    elif case == "timestamp":
        lines[1] = "not-a-number"
    elif case == "stale":
        lines[1] = "97.0"
    elif case == "future":
        lines[1] = "101.0"
    elif case == "barrier":
        barrier = 100.25
    elif case == "budget-integer":
        lines[2] = "not-an-integer"
    elif case == "budget-mismatch":
        lines[2] = "999"
    elif case == "row-fields":
        lines[3] = "10\t0"
    elif case == "row-integer":
        lines[3] = "10\t0\tunknown"
    elif case == "negative-pid":
        lines[3] = "-1\t0\t50"
    elif case == "negative-parent":
        lines[3] = "10\t-1\t50"
    elif case == "negative-rss":
        lines[3] = "10\t0\t-1"
    elif case == "duplicate-pid":
        lines.append(lines[3])
    elif case == "owner-missing":
        owner = 99
    elif case == "owner-rss":
        lines[4] = "11\t10\t0"
    elif case == "active-owner":
        active = [11]
    elif case == "active-missing":
        active = [99]
    elif case == "active-parent":
        lines[5] = "12\t10\t200"
    elif case == "active-rss":
        lines[5] = "12\t11\t0"
    state.write_text("\n".join(lines) + "\n")

    def monotonic():
        if case == "clock":
            raise ValueError("clock unavailable")
        return 100.5

    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=monotonic))
    # Supplying no optional output retains the old return contract.
    assert policy.read_tree_state(path, 1000, owner, active, barrier, True) is None
    detail = {"old": "must be cleared"}
    assert policy.read_tree_state(path, 1000, owner, active, barrier, True, detail) is None
    assert "old" not in detail and detail["reason"] == reason
    assert detail["expected_budget"] == 1000 and detail["owner_pid"] == owner
    assert detail["not_before"] == barrier
    if case in ("stale", "future", "barrier"):
        assert detail["checked_at"] == 100.5
        assert detail["age"] == 100.5 - float(lines[1])
        assert detail["sampled_at"] == float(lines[1])
    if case.startswith("active-"):
        assert (detail["owner_parent"], detail["owner_rss"]) == (10, 100)
        assert detail["active_pid"] == active[0] and detail["observed_budget"] == 1000
    if case == "active-parent":
        assert (detail["active_parent"], detail["active_rss"]) == (10, 200)
    if case == "active-rss":
        assert (detail["active_parent"], detail["active_rss"]) == (11, 0)


def test_rejection_evidence_is_from_the_single_read_even_if_file_is_replaced(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _snapshot(state, stamp=97.0)
    opened = []
    actual_open = open

    def read_once(*args, **kwargs):
        opened.append(args[0])
        return actual_open(*args, **kwargs)

    def replace_after_read():
        _snapshot(state, stamp=100.0)
        return 100.5

    monkeypatch.setattr(policy, "open", read_once, raising=False)
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=replace_after_read))
    detail = {}
    assert policy.read_tree_state(str(state), 1000, 11, [12], include_owner=True, diagnostic=detail) is None
    assert opened == [str(state)]
    assert detail["reason"] == "age" and detail["sampled_at"] == 97.0 and detail["age"] == 3.5
    assert state.read_text().splitlines()[1] == "100.0"


@pytest.mark.parametrize("case", ["accepted", "rejected", "unexpected-error"])
def test_optional_evidence_failure_cannot_change_reader_outcome(tmp_path, monkeypatch, case):
    state = tmp_path / "state"
    _snapshot(state, stamp=97.0 if case == "rejected" else 100.0)
    original = LookupError("original clock failure")

    def monotonic():
        if case == "unexpected-error":
            raise original
        return 100.5

    class BrokenOutput:
        def clear(self):
            raise RuntimeError("optional evidence failed")

    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=monotonic))
    if case == "unexpected-error":
        with pytest.raises(LookupError) as caught:
            policy.read_tree_state(str(state), 1000, 11, [12], diagnostic=BrokenOutput())
        assert caught.value is original
    else:
        expected = (50, {12: 230}) if case == "accepted" else None
        assert policy.read_tree_state(str(state), 1000, 11, [12], diagnostic=BrokenOutput()) == expected


@pytest.mark.parametrize("frozen_exists", [False, True])
def test_original_stale_guard_wrapper_forwards_optional_diagnostic(frozen_exists):
    # Compile only the original embedded wrapper definition, never its real
    # process driver. This keeps its path substitution and old/default call
    # arguments intact while checking the new output is forwarded by identity.
    tree = ast.parse(Path(__file__).with_name("test_worker_resource_plan.py").read_text())
    driver = next(node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and "def read_state(path, tree_budget, owner_pid" in node.value)
    wrapper = next(node for node in ast.walk(ast.parse(driver))
                   if isinstance(node, ast.FunctionDef) and node.name == "read_state")
    calls = []
    result = object()

    class Snapshot:
        def exists(self):
            return frozen_exists

        def __str__(self):
            return "frozen.tsv"

    def original_read(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    namespace = {"frozen": Snapshot(), "original_read": original_read}
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, "original-stale-guard-wrapper", "exec"), namespace)
    output = {}
    assert namespace["read_state"]("live.tsv", 1000, 11, [12], 100.0, True, diagnostic=output) is result
    assert namespace["read_state"]("live.tsv", 1000, 11, [12]) is result
    path = "frozen.tsv" if frozen_exists else "live.tsv"
    assert calls == [
        ((path, 1000, 11, [12], 100.0, True), {"diagnostic": output}),
        ((path, 1000, 11, [12], 0.0, False), {"diagnostic": None}),
    ]
    assert calls[0][1]["diagnostic"] is output


def _recovery_envelope(allowance="4.0", deadline="120.0", schedule="1.0,3.0"):
    return "pcc.worker-tree-recovery.v1\t" + schedule + "\t" + allowance + "\t" + deadline


def test_missing_recovery_policy_preserves_legacy_two_seconds():
    assert host_recovery.read_tree_recovery(None) == (2.0, -1.0)
    assert policy.STATE_MAX_AGE_SECONDS == 2.0
    assert host_recovery.read_tree_recovery(_recovery_envelope()) == (4.0, 120.0)
    assert host_recovery.read_tree_recovery(_recovery_envelope("1.75", "103.0", "0.5,1.25")) == (1.75, 103.0)


@pytest.mark.parametrize("raw", [
    "", "4", "pcc.worker-tree-recovery.v2\t1,3\t4\t120",
    _recovery_envelope("-1"), _recovery_envelope("0"),
    _recovery_envelope("nan"), _recovery_envelope("inf"), _recovery_envelope("1e999"),
    _recovery_envelope("5"), _recovery_envelope("4", "nan"),
    _recovery_envelope("4", "inf"), _recovery_envelope("4", "0"),
    _recovery_envelope("4", "-1"), _recovery_envelope(schedule="0,4"),
    _recovery_envelope(schedule="-1,5"), _recovery_envelope(schedule="nan,3"),
    _recovery_envelope(schedule="1e308,1e308"), _recovery_envelope(schedule=""),
])
def test_invalid_recovery_policy_fails_closed_before_launch(tmp_path, monkeypatch, raw):
    monkeypatch.setenv(policy.TREE_STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(policy.TREE_RECOVERY_ENV, raw)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid policy must not launch or observe a process")

    monkeypatch.setattr(pool, "_start_resource_worker", forbidden)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", forbidden)
    with pytest.raises(policy.WorkerMemoryError, match="invalid worker tree recovery policy"):
        pool.run_resource_worker_processes(
            ["never-executed"], [{"class": "invalid-policy", "inputs": [1],
                                  "estimate_bytes": 64 * MIB,
                                  "report_path": str(tmp_path / "rss")}],
            1, 512 * MIB, observations=[],
        )


@pytest.mark.parametrize("case,deadline,frames,fails", [
    ("fresh-after-expiry", "120", [(100.01, "missing", 32, False),
                                    (104.02, "valid", 16, False)], True),
    ("reason-changes-do-not-reset", "120", [(100.01, "missing", 32, False),
        (101.0, "parent", 100, False), (102.0, "zero", 64, False),
        (103.0, "budget", 16, False), (104.02, "valid", 16, False)], True),
    ("complete-fresh-resets", "120", [(100.01, "missing", 100, False),
        (103.0, "valid", 16, False), (103.01, "missing", 16, False),
        (106.5, "valid", 16, False), (106.51, "valid", 16, True)], False),
    ("reap-barrier-does-not-reset", "120", [(100.01, "missing", 100, False),
        (103.0, "before-barrier", 16, True), (104.02, "valid", 16, False)], True),
    ("original-deadline-wins", "102", [(100.01, "missing", 100, False),
                                       (102.0, "valid", 16, False)], True),
    ("original-deadline-already-passed", "99", [], True),
    ("legacy-late-fresh-retains-old-rule", "120", [(100.01, "missing", 100, False),
        (104.02, "valid", 16, False), (104.03, "valid", 16, True)], False),
    ("failure-during-outage", "120", [(100.01, "missing", 100, False),
                                     (101.0, "missing", 16, "failure")], True),
])
def test_configured_recovery_requires_complete_in_window_inventory(
    tmp_path, monkeypatch, case, deadline, frames, fails,
):
    owner, budget = os.getpid(), 1024 * MIB
    tree, trace = tmp_path / "tree", tmp_path / "trace"
    tasks = [{"class": "recovery", "inputs": [2 - index],
              "estimate_bytes": 200 * MIB,
              "report_path": str(tmp_path / ("rss" + str(index)))} for index in range(2)]
    clock = SimpleNamespace(now=100.0)
    remaining = list(frames)
    live, starts, stopped, reaped, observations, reads = {}, [], [], [], [], []
    frame = ["valid", 16, False]

    def publish_tree(kind):
        rows = [f"{owner}\t0\t{64 * MIB}"]
        if kind != "missing":
            for pid in live:
                parent = owner + 1 if kind == "parent" else owner
                rss = 0 if kind == "zero" else 16 * MIB
                rows.append(f"{pid}\t{parent}\t{rss}")
        stamp = clock.now - (0.001 if kind == "before-barrier" else 0)
        observed_budget = budget - 1 if kind == "budget" else budget
        tree.write_text(policy.TREE_STATE_SCHEMA + "\n" + str(stamp) + "\n"
                        + str(observed_budget) + "\n" + "\n".join(rows) + "\n")

    def report(pid, phase):
        item = live[pid]
        Path(tasks[item["index"]]["report_path"]).write_text(
            policy.RESOURCE_REPORT_SCHEMA + "\n" + str(pid) + "\n" + phase
            + "\n" + str(16 * MIB) + "\n" + str(frame[1] * MIB)
            + "\n" + item["token"] + "\n",
        )

    def start(specs, index):
        assert reads[-1][1] == "accepted"
        pid = owner + 100 + index
        live[pid] = {"index": index, "token": next(
            value.split("=", 1)[1] for value in specs[index][1]
            if value.startswith(policy.RESOURCE_TOKEN_ENV + "="))}
        starts.append((index, clock.now))
        report(pid, "started")
        return pid

    def sleep(_seconds):
        assert remaining, "fake recovery run exhausted its bounded frames"
        clock.now, kind, peak, complete = remaining.pop(0)
        frame[:] = [kind, peak, complete]
        for pid in live:
            report(pid, "running")
        publish_tree(kind)
        # Mutating the environment after capture cannot renew either bound.
        monkeypatch.setenv(policy.TREE_RECOVERY_ENV, _recovery_envelope("100", "999", "50,50"))

    def poll(pid):
        if frame[2] == "failure":
            return 7
        if frame[2]:
            report(pid, "complete")
            return 0
        return pool._WORKER_RUNNING

    def retire(pid):
        reaped.append(pid)
        live.pop(pid)

    def stop(pid):
        stopped.append(pid)
        live.pop(pid)

    original_reader = policy.read_tree_state

    def read_state(*args, **kwargs):
        detail = kwargs.pop("diagnostic", None)
        if detail is None:
            detail = {}
        result = original_reader(*args, diagnostic=detail, **kwargs)
        reads.append((clock.now, detail["reason"]))
        return result

    clock.monotonic, clock.sleep = lambda: clock.now, sleep
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(pool, "time", clock)
    monkeypatch.setattr(policy, "read_tree_state", read_state)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 64 * MIB)
    monkeypatch.setattr(pool, "_start_resource_worker", start)
    monkeypatch.setattr(pool, "_poll_resource_worker", poll)
    monkeypatch.setattr(pool, "_retire_resource_worker", retire)
    monkeypatch.setattr(pool, "_stop_resource_worker", stop)
    monkeypatch.setenv(policy.TREE_STATE_ENV, str(tree))
    if case != "legacy-late-fresh-retains-old-rule":
        monkeypatch.setenv(policy.TREE_RECOVERY_ENV, _recovery_envelope(deadline=deadline))
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")
    publish_tree("valid")
    failure = None
    try:
        pool.run_resource_worker_processes(
            ["never-executed-0", "never-executed-1"], tasks, 2, budget,
            observations=observations, trace_path=str(trace),
        )
    except (policy.WorkerMemoryError, pool.subprocess.CalledProcessError) as exc:
        failure = str(exc)
        if case == "failure-during-outage":
            assert isinstance(exc, pool.subprocess.CalledProcessError) and exc.returncode == 7
    assert not live
    assert bool(failure) == fails, (case, failure)
    if fails:
        if case != "failure-during-outage":
            assert "worker tree RSS state is missing, stale, or incompatible" in failure
        assert [index for index, _ in starts] == ([] if deadline == "99" else [0])
        assert not remaining
        if case == "reason-changes-do-not-reset":
            assert {reason for _, reason in reads} >= {
                "active_missing", "active_parent", "active_rss", "budget", "accepted"}
        if case == "reap-barrier-does-not-reset":
            assert reaped == [owner + 100] and not stopped
            assert len(observations) == 1 and observations[0][2] == 100 * MIB
            assert any(reason == "before_barrier" for _, reason in reads)
    else:
        assert starts == [(0, 100.0), (1, 104.02 if case == "legacy-late-fresh-retains-old-rule" else 103.0)]
        assert reaped == [owner + 100, owner + 101] and not stopped
        assert len(observations) == 2 and observations[0][2] == 100 * MIB
    if trace.exists():
        events = [row.split("\t") for row in trace.read_text().splitlines()]
        if case == "failure-during-outage":
            budget_event = next(row for row in events if row[0] == "failure-budget")
            assert int(budget_event[3]) == policy.peak_reservation(100 * MIB)
            assert int(budget_event[4]) == 0
            assert not observations and not stopped and reaped == [owner + 100]
        for event in events:
            if event[:2] == ["retire", "0"]:
                assert int(event[3]) == policy.peak_reservation(100 * MIB)


def test_host_recovery_helper_is_excluded_from_native_import_closure():
    from pcc.frontends.python import pipeline_import_scan as scan

    source = Path(pool.__file__).read_text()
    marker = "pcc.frontends.python.worker_resource_host_recovery"
    assert marker in source
    assert marker not in scan._without_inactive_runtime_imports(source)
    assert marker in scan._without_inactive_implementation_imports(source, implementation="cpython")
    shared = ast.parse(Path(policy.__file__).read_text())
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "read_tree_recovery"
                   for node in shared.body)


@pytest.mark.parametrize("change", ["compound", "rebound", "global"])
def test_unproven_recovery_guard_keeps_host_dependency(change):
    from pcc.frontends.python import pipeline_import_scan as scan

    source = Path(pool.__file__).read_text()
    marker = "pcc.frontends.python.worker_resource_host_recovery"
    if change == "compound":
        source = source.replace('if execution_runtime.implementation.name == "cpython":',
                                'if execution_runtime.implementation.name == "cpython" and enabled:')
    elif change == "rebound":
        source = source.replace("    import sys as execution_runtime\n",
                                "    import sys as execution_runtime\n    execution_runtime = other\n")
    else:
        source = "import sys as execution_runtime\n" + source.replace("    import sys as execution_runtime\n", "")
    assert marker in scan._without_inactive_runtime_imports(source)
