"""Real self-spawn policy witness shared by CPython and owned native builds.

The outer runner owns the unchanged hard tree cap. This component deliberately
uses local driver/child accounting, as the existing resource acceptance driver
does. Synthetic byte inputs exercise the empirical policy, not a fake compiler
handoff. Native execution must use this same emitted executable for children.
"""

import os
import shlex
import sys
import time

from pcc.extern import c_int64, extern
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 * 1024
_raw_poll = extern("pcc_worker_process_poll", (c_int64,), c_int64)


def read(root, name):
    with open(root + "/" + name, "r", encoding="utf-8") as stream:
        return stream.read()


def write(root, name, value):
    temporary = root + "/" + name + ".tmp." + str(os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(value)
    os.replace(temporary, root + "/" + name)


def exists(root, name):
    return os.path.exists(root + "/" + name)


def wait_file(root, name):
    # Peer markers and cancellation depend on another process being scheduled.
    # On loaded hosted runners the GC4 growth case missed the former 6 s
    # (Linux x86_64 in 3 of 5 runs, Windows in run 38423410429); 15 s passed
    # in runs 38400558239 and 38405592470. Stay below the outer runner's 20 s
    # process-group timeout, which owns hard failure.
    deadline = time.monotonic() + 15.0
    while True:
        if exists(root, name):
            try:
                return read(root, name)
            except PermissionError:
                # Windows can briefly deny a newly published marker read.
                # Retry within the same deadline; persistent denial is fatal.
                if time.monotonic() >= deadline:
                    raise
        else:
            assert time.monotonic() < deadline, "missing worker marker: " + name
        time.sleep(0.005)


def events(root):
    if not exists(root, "admission.tsv"):
        return []
    text = read(root, "admission.tsv")
    lines = text.splitlines()
    if not text.endswith("\n"):
        lines = lines[:-1]
    return [line.split("\t") for line in lines]


def wait_event(root, event, index):
    # Same bound and reason as wait_file.
    deadline = time.monotonic() + 15.0
    while not any(row[:2] == [event, str(index)] for row in events(root)):
        assert time.monotonic() < deadline, "missing admission event: " + event
        time.sleep(0.005)


def allocate(size):
    payload = bytearray(size)
    for offset in range(0, size, 4096):
        payload[offset] = 1
    payload[size - 1] = 123
    return payload


def check_owner():
    assert sys.implementation.name == os.environ["PCC_TEST_RESOURCE_OWNER"]
    assert workers._worker_collector() == int(os.environ["PCC_GC_BACKEND"])


def child(root, case, index, growth):
    check_owner()
    count = "attempts" + str(index)
    attempt = int(read(root, count)) + 1 if exists(root, count) else 1
    write(root, count, str(attempt))
    token = os.environ[policy.RESOURCE_TOKEN_ENV]
    record = (str(os.getpid()) + "\t" + token + "\t"
              + sys.implementation.name + "\t" + str(workers._worker_collector()))
    write(root, "started." + str(index) + "." + str(attempt), record)
    payload = allocate(4 * MIB)
    policy.publish_worker_resource("started")
    if case == "width":
        for peer in range(3):
            wait_file(root, "started." + str(peer) + ".1")
        write(root, "overlap" + str(index), "all-three-started")
        for peer in range(3):
            wait_file(root, "overlap" + str(peer))
    elif index == 0:
        wait_file(root, "started.1.1")
        wait_file(root, "peer-report-ready")
        payload = allocate(growth)
        policy.publish_worker_resource("leader-grown")
        wait_event(root, "cancel", 1)
    elif index == 1 and attempt == 1:
        write(root, "peer-report-ready", "ready")
        wait_file(root, "this-cancelled-attempt-must-not-complete")
    elif index == 1:
        payload = allocate(growth)
        policy.publish_worker_resource("retry-grown")
    else:
        # A dependency is released only after exit, a matching final report,
        # and retirement. Reading this trace does not grant readiness.
        wait_event(root, "retire", 1)
    assert payload[0] == 1 and payload[len(payload) - 1] == 123
    policy.publish_worker_resource("complete")
    with open(os.environ[policy.RESOURCE_REPORT_ENV], "r", encoding="utf-8") as stream:
        final_report = stream.read()
    write(root, "complete." + str(index) + "." + str(attempt), final_report)


def make_task(root, index, source_bytes):
    return {
        "report_path": root + "/rss" + str(index), "estimate_bytes": 0,
        "inputs": [source_bytes, source_bytes, 0, 0, 0, 1],
        "class": "prior-component:" + str(index), "restartable": True,
        "startup_prior_model": "host-export-v1",
    }


def command(root, case, index, growth):
    _windows_stage1_phase(root, "abspath")
    executable = os.path.abspath(sys.argv[0])
    _windows_stage1_phase(root, "argv")
    prefix = [executable] if sys.implementation.name == "pcc" else [sys.executable, "-B", executable]
    argv = prefix + ["child", root, case, str(index), str(growth)]
    _windows_stage1_phase(root, "quote")
    return " ".join([shlex.quote(value) for value in argv])


def verify_model(root):
    # Execute the new normalization and four-field sample shape in emitted
    # code too. These explicit synthetic inputs are policy boundary tests.
    small = make_task(root, 0, 1)
    large = make_task(root, 0, 100 * MIB)
    observed = policy.completed_task_observation(large, 600 * MIB)
    assert policy.estimated_task_bytes(small, [observed]) == policy.task_startup_prior_bytes(small)
    observed = policy.completed_task_observation(small, 600 * MIB)
    assert policy.estimated_task_bytes(small, [observed]) == policy.peak_reservation(600 * MIB)
    assert policy.task_startup_prior_bytes(small) == 343933128
    assert policy.task_startup_prior_bytes(large) == 21315452928
    # Independent exact literal: ceil(21315452928 * 920649728 / 343933128).
    # Its intermediate numerator is 19624065940360003584 (> 2**64).
    # Do not compute the oracle with the same potentially lowered expression.
    assert policy.estimated_task_bytes(large, [observed]) == 57057795085
    small["incomplete_peak_bytes"] = 700 * MIB
    assert policy.minimum_task_bytes(small) == 700 * MIB
    assert policy.estimated_task_bytes(small, [observed]) == policy.peak_reservation(700 * MIB)
    malformed = (large["class"], large["inputs"], MIB, (large["startup_prior_model"], 0))
    try:
        policy.estimated_task_bytes(large, [malformed])
    except policy.WorkerMemoryError:
        pass
    else:
        raise AssertionError("zero observation prior was accepted")


def _windows_stage1_phase(root, boundary):
    # One bounded synthetic witness for the existing Windows pcc0/GC0/width
    # execution only. The parent harness supplies the exact activation scope.
    if sys.platform == "win32" and os.environ.get("PCC_TEST_WINDOWS_STAGE1_PHASE", "") == "1":
        with open(root + "/windows-stage1-phase", "w", encoding="utf-8") as stream:
            stream.write(boundary + "\n")


def run_case(root, case):
    _windows_stage1_phase(root, "owner")
    check_owner()
    _windows_stage1_phase(root, "model")
    verify_model(root)
    assert not os.environ.get(policy.TREE_STATE_ENV, "")
    _windows_stage1_phase(root, "rss-current")
    owner = workers._coordinator_rss_bytes()
    _windows_stage1_phase(root, "rss-peak")
    startup = max(owner, workers._worker_peak_rss_bytes())
    # Reserve a measured startup envelope so the changed policy, rather than
    # fork/exec inherited high-water variance, determines initial admission.
    working = max(160 * MIB, startup + 64 * MIB)
    source_bytes = (working - 160 * MIB + 159) // 160
    sizes = ([source_bytes, source_bytes + 128 * 1024, source_bytes + 128 * 1024]
             if case == "width" else [source_bytes + 1, source_bytes, source_bytes])
    tasks = [make_task(root, index, sizes[index]) for index in range(3)]
    priors = [policy.task_startup_prior_bytes(item) for item in tasks]
    if case == "growth":
        tasks[2]["depends_on"] = 1
    width = 3 if case == "width" else 2
    available = sum(priors) + 64 * MIB if case == "width" else sum(priors[:2]) + 64 * MIB
    budget = owner + policy.RSS_HEADROOM_BYTES + available
    outer_budget = int(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "0") or "0")
    assert outer_budget > 0, "an enclosing hard process-tree budget is required"
    assert budget <= outer_budget, "component working set does not fit the unchanged outer cap"
    growth = working + 128 * MIB
    _windows_stage1_phase(root, "command")
    commands = [command(root, case, index, growth) for index in range(3)]
    _windows_stage1_phase(root, "budget")
    write(root, "budget.tsv", str(budget) + "\t" + str(outer_budget))
    observations = []
    pool.run_resource_worker_processes(
        commands, tasks, width, budget, observations=observations,
        trace_path=root + "/admission.tsv",
    )
    rows = events(root)
    assert rows and all(len(row) == 7 for row in rows)
    assert not any(row[0] in ("calibrate", "failed", "unverified-retire") for row in rows)
    starts = [row for row in rows if row[0] == "start"]
    retires = [row for row in rows if row[0] == "retire"]
    cancels = [row for row in rows if row[0] == "cancel"]
    assert len(retires) == 3 and len(observations) == 3
    if case == "width":
        assert [int(row[1]) for row in starts] == [1, 2, 0]
        assert not cancels
        assert max(float(row[6]) for row in starts) < min(float(row[6]) for row in retires)
        assert sum(int(row[3]) for row in starts) <= min(int(row[4]) for row in starts)
        attempts = [1, 1, 1]
    else:
        assert [int(row[1]) for row in starts] == [0, 1, 1, 2]
        assert len(cancels) == 1 and cancels[0][1] == "1"
        retry = starts[2]
        leader_end = next(row for row in retires if row[1] == "0")
        peer_end = next(row for row in retires if row[1] == "1")
        assert float(leader_end[6]) < float(retry[6])
        assert int(retry[3]) == int(retry[4])
        assert float(peer_end[6]) < float(starts[3][6])
        assert int(leader_end[3]) > priors[0]
        assert not exists(root, "complete.1.1")
        assert tasks[1]["retry_calibration"] is False
        assert tasks[1]["incomplete_peak_bytes"] == int(cancels[0][5]) > 0
        assert tasks[1]["estimate_bytes"] == 0
        attempts = [1, 2, 1]
    pids = []
    tokens = []
    for index in range(3):
        assert int(read(root, "attempts" + str(index))) == attempts[index]
        for attempt in range(1, attempts[index] + 1):
            record = read(root, "started." + str(index) + "." + str(attempt)).split("\t")
            pid, token = int(record[0]), record[1]
            assert pid > 0 and len(token) == 64
            assert record[2:] == [sys.implementation.name, str(workers._worker_collector())]
            pids.append(pid)
            tokens.append(token)
            if sys.implementation.name == "pcc":
                # The owned wait boundary reports no remaining waitable child
                # after poll/stop and retire. No POSIX-only kill(0) is used.
                assert _raw_poll(pid) == 127
            if attempt == attempts[index]:
                report = policy.read_worker_resource(root + "/rss" + str(index), pid, token)
                assert report is not None and report[0] == "complete"
                assert report[2] >= report[1] > 0
                samples = [sample[2] for sample in observations if sample[0] == tasks[index]["class"]]
                assert samples == [report[2]]
    assert len(set(pids)) == len(pids) and len(set(tokens)) == len(tokens)
    assert pool._HOST_WORKERS == {}
    assert not pool._NATIVE_LIVE_WORKERS and pool._NATIVE_COMPLETED_WORKERS == {}
    write(root, "complete", sys.implementation.name + "\t" + str(workers._worker_collector()))
    print("WORKER_SIZE_PRIOR_OK " + case)


def main():
    if sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    else:
        assert sys.argv[1] in ("width", "growth")
        run_case(sys.argv[2], sys.argv[1])


main()
