"""One real process/admission contract for CPython and owned native execution.

The enclosing test runner owns the hard process-tree cap. These deliberately
small scenarios use the scheduler's documented local owner/worker accounting;
they do not replace or raise that outer cap. Reservations derived from observed
startup RSS are estimates, never claims of a sufficient full-task budget.
Native children self-spawn this PCC-emitted executable. CPython is a separate
reference owner. This component test does not prove pcc1-to-Stage2 compiler
worker dispatch; that still requires the real compiler workflow.
"""

import os
import shlex
import subprocess
import sys
import time

from pcc.extern import c_int, c_int64, c_ptr, extern
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import worker_process_pool as pool
from pcc.frontends.python import worker_resource_plan as policy


MIB = 1024 ** 2
PAYLOAD_BYTES = 48 * MIB
WAIT_SECONDS = 3.0
_setpgid = extern("setpgid", (c_int, c_int), c_int)
# Raw ABI probes deliberately bypass the controller's ownership inventory.
# extern declarations lower at their call sites; they are not runtime module
# attributes that can be fetched from pool.
_raw_worker_start = extern("pcc_worker_process_start", (c_ptr, c_int64), c_int64)
_raw_worker_poll = extern("pcc_worker_process_poll", (c_int64,), c_int64)
_raw_worker_stop = extern("pcc_worker_process_stop", (c_int64,), c_int64)


def read(root, name):
    with open(root + "/" + name, "r", encoding="utf-8") as stream:
        return stream.read()


def write(root, name, text):
    temporary = root + "/" + name + ".tmp." + str(os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, root + "/" + name)


def exists(root, name):
    return os.path.exists(root + "/" + name)


def wait_for(root, name):
    deadline = time.monotonic() + WAIT_SECONDS
    while not exists(root, name):
        assert time.monotonic() < deadline, "missing marker: " + name
        time.sleep(0.005)
    return read(root, name)


def assert_alive(pid):
    os.kill(pid, 0)


def assert_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError("worker remains alive after retirement: " + str(pid))


def owner_name():
    return sys.implementation.name


def native_owner():
    return owner_name() == "pcc"


def check_owner():
    assert owner_name() == os.environ["PCC_TEST_RESOURCE_OWNER"]
    assert workers._worker_collector() == int(os.environ["PCC_GC_BACKEND"])


def child_command(root, mode, index):
    if native_owner():
        argv = [os.path.abspath(sys.argv[0])]
    else:
        argv = [sys.executable, "-B", os.path.abspath(sys.argv[0])]
    argv.extend(["child", root, mode, str(index)])
    return " ".join([shlex.quote(value) for value in argv])


def task(root, index, estimate=0, inputs=1):
    return {"report_path": root + "/rss" + str(index),
            "estimate_bytes": estimate, "inputs": [inputs],
            "class": owner_name() + ":resource-acceptance",
            "restartable": True}


def empty_inventory():
    assert pool._HOST_WORKERS == {}
    assert not pool._NATIVE_LIVE_WORKERS
    assert pool._NATIVE_COMPLETED_WORKERS == {}


def events(root, complete=True):
    text = read(root, "admission.tsv")
    lines = text.splitlines()
    if not complete and not text.endswith("\n"):
        lines = lines[:-1]
    rows = [line.split("\t") for line in lines]
    assert all(len(row) == 7 for row in rows)
    return rows


def event_positions(rows, kind, index):
    return [position for position, row in enumerate(rows)
            if row[0] == kind and row[1] == str(index)]


def start_record(root, index, attempt):
    return read(root, "started." + str(index) + "." + str(attempt)).split("\t")


def check_attempt(root, index, attempt, completed=True):
    record = start_record(root, index, attempt)
    pid = int(record[0])
    assert record[1] == owner_name()
    assert int(record[2]) == workers._worker_collector()
    assert len(record[3]) == 64
    assert_gone(pid)
    if completed:
        report = policy.read_worker_resource(root + "/rss" + str(index), pid, record[3])
        assert report is not None and report[0] == "complete"
        assert report[2] >= report[1] > 0
    return record


def record_budget(root, budget, owner_rss, observations):
    # A measured lower bound is not a prediction that all future work fits.
    largest_peak = max([sample[2] for sample in observations] or [0])
    lower_bound = owner_rss + policy.RSS_HEADROOM_BYTES
    if largest_peak:
        lower_bound += policy.peak_reservation(largest_peak)
    write(root, "budget.tsv", str(budget) + "\t" + str(owner_rss) + "\t"
          + str(largest_peak) + "\t" + str(lower_bound)
          + "\tmeasured-lower-bound-not-a-sufficient-budget\n")


def run_tasks(root, commands, tasks, width, budget, observations):
    write(root, "configured-budget", str(budget))
    pool.run_resource_worker_processes(
        commands, tasks, width, budget, observations=observations,
        trace_path=root + "/admission.tsv",
    )
    assert read(root, "configured-budget") == str(budget)


def allocate(size):
    payload = bytearray(size)
    # Touch every page: a lazy zero-filled allocation is not RSS evidence.
    for offset in range(0, size, 4096):
        payload[offset] = 1
    payload[size - 1] = 123
    return payload


def publish_complete(payload):
    assert payload[0] == 1 and payload[len(payload) - 1] == 123
    policy.publish_worker_resource("complete")


def mark_started(root, index):
    count_name = "starts" + str(index)
    previous = read(root, count_name) if exists(root, count_name) else ""
    attempt = len(previous) + 1
    write(root, count_name, previous + "x")
    record = (str(os.getpid()) + "\t" + owner_name() + "\t"
              + str(workers._worker_collector()) + "\t"
              + str(os.environ.get(policy.RESOURCE_TOKEN_ENV, "") or ""))
    write(root, "started." + str(index) + "." + str(attempt), record)
    write(root, "ready" + str(index), str(os.getpid()))
    return attempt


def confirm_overlap(root, index, peers):
    pids = []
    for peer in peers:
        pid = int(wait_for(root, "ready" + str(peer)))
        assert_alive(pid)
        pids.append(pid)
    write(root, "overlap" + str(index), " ".join([str(pid) for pid in pids]))


def join_process_group(group):
    if sys.implementation.name == "pcc":
        # Darwin uses the supported libSystem ABI; Linux's owned libc leaf
        # supplies the same signed pid_t/result contract with kernel calls.
        assert _setpgid(0, group) == 0
    else:
        os.setpgid(0, group)


def child(root, mode, index):
    check_owner()
    attempt = mark_started(root, index)
    if mode == "leader":
        wait_for(root, "sentinel-joined")
        return
    if mode == "sentinel":
        leader = int(wait_for(root, "ready0"))
        join_process_group(leader)
        write(root, "sentinel-joined", str(os.getpid()))
        deadline = time.monotonic() + 6.0
        while not exists(root, "sentinel-exit"):
            assert time.monotonic() < deadline, "sentinel was not released"
            for probe in ("unowned", "reaped"):
                if exists(root, "probe-" + probe):
                    write(root, "ack-" + probe, str(os.getpid()))
            time.sleep(0.005)
        return
    if mode == "fail":
        peer = int(wait_for(root, "ready0"))
        assert_alive(peer)
        write(root, "failure-overlap", str(peer) + " " + str(os.getpid()))
        sys.exit(7)
    payload = allocate(PAYLOAD_BYTES if mode == "grow" else 4 * MIB)
    if mode == "grow" and attempt == 1:
        confirm_overlap(root, index, [0, 1])
        wait_for(root, "overlap" + str(1 - index))
    policy.publish_worker_resource("allocated")
    if mode == "calibrated" and index != 0:
        confirm_overlap(root, index, [1, 2, 3])
        for peer in (1, 2, 3):
            wait_for(root, "overlap" + str(peer))
    elif mode == "grow" and attempt == 1:
        deadline = time.monotonic() + WAIT_SECONDS
        if index == 0:
            while not any(row[0] == "cancel" and row[1] == "1" for row in events(root, False)):
                assert time.monotonic() < deadline, "overlapping demand did not cancel peer"
                time.sleep(0.005)
        else:
            # The shared controller must stop this real, still-live attempt.
            while time.monotonic() < deadline:
                time.sleep(0.005)
            raise AssertionError("underestimated peer was not cancelled")
    elif mode == "slow":
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(0.005)
        raise AssertionError("failed peer did not clean the slow worker")
    time.sleep(0.05)
    publish_complete(payload)


def oversized(root):
    owner = workers._coordinator_rss_bytes()
    budget = owner + policy.RSS_HEADROOM_BYTES + 64 * MIB
    item = task(root, 0, estimate=2 * budget)
    try:
        run_tasks(root, [child_command(root, "normal", 0)], [item], 2, budget, [])
    except policy.WorkerMemoryError as error:
        message = str(error)
        assert "estimated_minimum_budget_bytes=" in message
        assert "not a guaranteed full-task peak" in message
        write(root, "refusal", message)
    else:
        raise AssertionError("oversized task was admitted")
    assert not exists(root, "starts0")
    assert read(root, "configured-budget") == str(budget)
    empty_inventory()
    record_budget(root, budget, owner, [])


def calibration(root):
    owner = workers._coordinator_rss_bytes()
    # fork/exec may retain the parent's startup high-water RSS. Preserve the
    # measured-parent +32MiB contract used by the host cleanup regression.
    startup = policy.peak_reservation(owner + 32 * MIB)
    budget = owner + policy.RSS_HEADROOM_BYTES + 3 * startup + 128 * MIB
    tasks = [task(root, index, inputs=4 - index) for index in range(4)]
    observations = []
    run_tasks(root, [child_command(root, "calibrated", index) for index in range(4)],
              tasks, 3, budget, observations)
    rows = events(root)
    assert rows[0][0:2] == ["calibrate", "0"]
    assert rows[1][0:2] == ["retire", "0"]
    assert [row[0] for row in rows[2:5]] == ["start", "start", "start"]
    pids = [int(check_attempt(root, index, 1)[0]) for index in range(4)]
    for index in (1, 2, 3):
        assert [int(value) for value in read(root, "overlap" + str(index)).split()] == pids[1:]
    assert len(observations) == 4 and all(sample[2] > 0 for sample in observations)
    assert all(read(root, "starts" + str(index)) == "x" for index in range(4))
    empty_inventory()
    record_budget(root, budget, owner, observations)


def growth(root):
    owner = workers._coordinator_rss_bytes()
    available = policy.peak_reservation(owner + PAYLOAD_BYTES) + 32 * MIB
    budget = owner + policy.RSS_HEADROOM_BYTES + available
    tasks = [task(root, index, estimate=8 * MIB, inputs=2 - index) for index in range(2)]
    observations = []
    run_tasks(root, [child_command(root, "grow", index) for index in range(2)],
              tasks, 2, budget, observations)
    rows = events(root)
    cancelled = [row for row in rows if row[0] == "cancel"]
    assert len(cancelled) == 1 and cancelled[0][1] == "1"
    starts = event_positions(rows, "start", 1)
    retires = event_positions(rows, "retire", 0)
    assert len(starts) == 2 and len(retires) == 1
    assert starts[0] < retires[0] < starts[1]
    assert float(cancelled[0][6]) > float(rows[starts[0]][6])
    first = check_attempt(root, 0, 1)
    cancelled_attempt = check_attempt(root, 1, 1, completed=False)
    retry = check_attempt(root, 1, 2)
    assert cancelled_attempt[3] != retry[3]
    assert cancelled[0][2] == cancelled_attempt[0]
    assert read(root, "starts0") == "x" and read(root, "starts1") == "xx"
    for index in (0, 1):
        assert read(root, "overlap" + str(index)).split() == [first[0], cancelled_attempt[0]]
    assert len(observations) == 2
    assert all(sample[2] >= PAYLOAD_BYTES for sample in observations)
    assert read(root, "configured-budget") == str(budget)
    empty_inventory()
    record_budget(root, budget, owner, observations)


def failure(root):
    owner = workers._coordinator_rss_bytes()
    startup = policy.peak_reservation(owner + 32 * MIB)
    budget = owner + policy.RSS_HEADROOM_BYTES + 2 * startup + 32 * MIB
    tasks = [task(root, index, estimate=startup, inputs=3 - index) for index in range(3)]
    commands = [child_command(root, mode, index)
                for index, mode in enumerate(("slow", "fail", "normal"))]
    started = time.monotonic()
    try:
        run_tasks(root, commands, tasks, 2, budget, [])
    except subprocess.CalledProcessError as error:
        assert error.returncode == 7
        assert error.cmd == commands[1]
    else:
        raise AssertionError("worker failure was ignored")
    assert time.monotonic() - started < 4.0
    assert not exists(root, "starts2")
    assert read(root, "starts0") == read(root, "starts1") == "x"
    overlap = [int(value) for value in read(root, "failure-overlap").split()]
    assert len(overlap) == 2 and overlap[0] != overlap[1]
    assert overlap == [int(start_record(root, index, 1)[0]) for index in range(2)]
    for pid in overlap:
        assert_gone(pid)
    rows = events(root)
    assert len(event_positions(rows, "failed", 1)) == 1
    assert not event_positions(rows, "start", 2)
    assert read(root, "configured-budget") == str(budget)
    empty_inventory()
    record_budget(root, budget, owner, [])


def expect_unowned_poll(pid):
    try:
        pool._poll_resource_worker(pid)
    except ValueError as error:
        assert native_owner() and "unowned" in str(error)
    except KeyError:
        assert not native_owner()
    else:
        raise AssertionError("an unowned PID was polled")


def spawn_failure(root):
    owner = workers._coordinator_rss_bytes()
    budget = owner + policy.RSS_HEADROOM_BYTES + 256 * MIB
    missing = root + "/missing-executable"
    try:
        run_tasks(root, [shlex.quote(missing)], [task(root, 0, estimate=8 * MIB)], 1, budget, [])
    except subprocess.CalledProcessError as error:
        assert native_owner() and error.returncode == 127
    except OSError:
        assert not native_owner()
    else:
        raise AssertionError("missing executable was launched")
    try:
        pool._command_spec("")
    except ValueError:
        pass
    else:
        raise AssertionError("empty command accepted")
    if native_owner():
        assert _raw_worker_start([], 0) == -1
        assert _raw_worker_start([], -1) == -1
        assert _raw_worker_poll(0) == 127
        assert _raw_worker_stop(0) == -1
    expect_unowned_poll(0)
    pool._stop_resource_worker(0)
    # The driver is its own process-group leader. A mistaken group signal to
    # this unowned PID prevents the final marker and is observable outside it.
    pool._stop_resource_worker(os.getpid())
    expect_unowned_poll(os.getpid())
    assert not exists(root, "starts0")
    empty_inventory()
    record_budget(root, budget, owner, [])


def wait_owned(pid):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        result = pool._poll_resource_worker(pid)
        if result != pool._WORKER_RUNNING:
            return result
        assert time.monotonic() < deadline, "owned child did not exit"
        time.sleep(0.005)


def raw_poll(pid, process):
    if native_owner():
        return _raw_worker_poll(pid)
    result = process.poll()
    return pool._WORKER_RUNNING if result is None else result


def wait_raw(pid, process):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        result = raw_poll(pid, process)
        if result != pool._WORKER_RUNNING:
            return result
        assert time.monotonic() < deadline, "unowned sentinel did not exit"
        time.sleep(0.005)


def handles(root):
    specs = [pool._command_spec(child_command(root, mode, index))
             for index, mode in enumerate(("leader", "sentinel"))]
    leader = pool._start_resource_worker(specs, 0)
    assert leader > 0
    sentinel = -1
    process = None
    sentinel_reaped = False
    try:
        assert int(wait_for(root, "ready0")) == leader
        assert pool._poll_resource_worker(leader) == pool._WORKER_RUNNING
        try:
            pool._retire_resource_worker(leader)
        except ValueError as error:
            assert "live" in str(error)
        else:
            raise AssertionError("live child retired")
        if native_owner():
            # Real owned ABI, deliberately outside the shared pool inventory.
            sentinel = _raw_worker_start(specs, 1)
        else:
            argv, vector = specs[1]
            environment = dict(item.split("=", 1) for item in vector)
            process = subprocess.Popen(argv, env=environment, process_group=0)
            sentinel = process.pid
        assert sentinel > 0 and int(wait_for(root, "sentinel-joined")) == sentinel
        pool._stop_resource_worker(sentinel)
        write(root, "probe-unowned", "go")
        assert int(wait_for(root, "ack-unowned")) == sentinel
        assert_alive(sentinel)
        assert wait_owned(leader) == 0
        # A second actual waitpid after the terminal result would return 127.
        # The controller must instead return its cached terminal result.
        assert pool._poll_resource_worker(leader) == 0
        assert pool._poll_resource_worker(leader) == 0
        pool._retire_resource_worker(leader)
        expect_unowned_poll(leader)
        pool._stop_resource_worker(leader)
        pool._stop_resource_worker(leader)
        # Sentinel still belongs to leader's process group after leader was
        # reaped. Any erroneous post-reap kill(-leader, 9) kills this witness.
        write(root, "probe-reaped", "go")
        assert int(wait_for(root, "ack-reaped")) == sentinel
        assert_alive(sentinel)
        write(root, "sentinel-exit", "go")
        assert wait_raw(sentinel, process) == 0
        sentinel_reaped = True
        assert_gone(leader)
        assert_gone(sentinel)
        empty_inventory()
        write(root, "handles", str(leader) + " " + str(sentinel) + " cached-terminal-no-post-reap-signal")
    finally:
        try:
            if sentinel > 0 and not sentinel_reaped:
                status = raw_poll(sentinel, process)
                if status == pool._WORKER_RUNNING:
                    # Cleanup does not use stop(-sentinel): it joined another
                    # process group. Cooperative exit and its own deadline
                    # remain independent of the helper being tested.
                    write(root, "sentinel-exit", "cleanup")
                    wait_raw(sentinel, process)
        finally:
            pool._stop_resource_worker(leader)


def main():
    check_owner()
    if sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        return
    case = sys.argv[1]
    root = sys.argv[2]
    assert not os.environ.get(policy.TREE_STATE_ENV, "")
    if case == "oversized":
        oversized(root)
    elif case == "calibration":
        calibration(root)
    elif case == "growth":
        growth(root)
    elif case == "failure":
        failure(root)
    elif case == "spawn_failure":
        spawn_failure(root)
    elif case == "handles":
        handles(root)
    else:
        raise AssertionError("unknown resource scenario")
    write(root, "complete", owner_name() + "\t" + str(workers._worker_collector()))
    print("RESOURCE_ACCEPTANCE_OK " + case)


main()
