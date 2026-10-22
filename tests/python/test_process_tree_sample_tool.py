from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "run_process_tree_sample.py"


def _load_tool_module():
    spec = importlib.util.spec_from_file_location(
        "pcc_process_tree_sample_test_module", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    scripts_path = str(TOOL.parent)
    sys.path.insert(0, scripts_path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_path)
    return module


def test_nested_session_cleanup_excludes_reused_pids_and_unrelated_sessions(monkeypatch):
    tool = _load_tool_module()
    identity = tool._ProcessIdentity
    observed = {
        10: identity(10, 1, (100, 0)),
        11: identity(11, 10, (110, 0)),
        12: identity(11, 11, (120, 0)),
        20: identity(20, 10, (200, 0)),
    }
    live = {
        10: identity(10, 1, (101, 0)),  # Reused root PID and session ID.
        12: identity(11, 1, (120, 0)),  # Proven surviving orphan.
        13: identity(11, 1, (130, 0)),  # Its session remains owned.
        20: identity(20, 1, (201, 0)),  # Same SID, different process start.
        21: identity(20, 1, (210, 0)),
        30: identity(30, 1, (300, 0)),
    }
    monkeypatch.setattr(tool, "_process_identity", live.get)
    assert tool._owned_session_pids(10, observed, live) == {12, 13}


def test_owned_rss_retains_orphans_and_rejects_stale_parent_pid(monkeypatch):
    tool = _load_tool_module()
    identity = tool._ProcessIdentity
    observed = {10: identity(10, 1, (100, 0))}
    live = {
        10: identity(10, 1, (100, 0)),
        11: identity(11, 10, (110, 0)),
        12: identity(11, 11, (120, 0)),
    }
    monkeypatch.setattr(tool, "_process_identity", live.get)
    rows = {10: (1, 100, "driver"), 11: (10, 200, "leader"),
            12: (11, 300, "resident")}
    assert tool._owned_tree_rows(rows, observed) == rows
    del live[11]
    live[12] = identity(11, 1, (120, 0))
    live[13] = identity(11, 12, (130, 0))
    live[20] = identity(20, 1, (200, 0))
    rows = {10: (1, 100, "driver"), 12: (1, 300, "resident"),
            13: (12, 400, "new owned child"),
            20: (10, 1000, "stale ps parent from a previous PID occupant")}
    owned = tool._owned_tree_rows(rows, observed)
    assert set(owned) == {10, 12, 13}
    assert sum(row[1] for row in owned.values()) == 800
    live[12] = identity(11, 1, (121, 0))
    del live[13]
    assert set(tool._owned_tree_rows(rows, observed)) == {10}


def test_reused_pid_is_readmitted_only_through_an_owned_kernel_parent(monkeypatch):
    """A new worker can receive the PID of a worker that exited earlier.

    macOS reused PID 7199 within one CI Stage1 build. The new worker was a
    child of the owned coordinator, but its PID had been observed with another
    start time, so it never entered the tree state and the coordinator aborted
    Stage1 with active_missing. Its fresh kernel parent proves ownership just
    as it does for a PID seen for the first time; a shared session does not.
    """
    tool = _load_tool_module()
    identity = tool._ProcessIdentity
    observed = {
        10: identity(10, 1, (100, 0)),
        11: identity(10, 10, (110, 0)),
        12: identity(10, 11, (120, 0)),
        14: identity(10, 11, (125, 0)),
    }
    live = {
        10: identity(10, 1, (100, 0)),
        11: identity(10, 10, (110, 0)),
        12: identity(10, 11, (130, 0)),  # New worker of the owned coordinator.
        14: identity(10, 1, (150, 0)),   # Reused PID in the session, parent 1.
        16: identity(10, 12, (160, 0)),  # Child of the new worker.
    }
    monkeypatch.setattr(tool, "_process_identity", live.get)
    rows = {10: (1, 100, "driver"), 11: (10, 200, "coordinator"),
            12: (11, 300, "new worker"), 14: (1, 400, "reused session PID"),
            16: (12, 500, "worker child")}
    owned = tool._owned_tree_rows(rows, observed)
    assert set(owned) == {10, 11, 12, 16}
    assert observed[12].start_time == (130, 0)
    assert observed[14].start_time == (125, 0)
    assert tool._owned_session_pids(10, observed, live) == {11, 12, 16}


def test_cleanup_never_signals_a_reaped_root_or_reused_descendant(monkeypatch):
    tool = _load_tool_module()
    identity = tool._ProcessIdentity
    observed = {
        10: identity(10, 1, (100, 0)),
        12: identity(10, 10, (120, 0)),
        13: identity(10, 10, (130, 0)),
    }
    live = {
        10: identity(10, 1, (101, 0)),
        12: identity(10, 1, (120, 0)),
        13: identity(10, 1, (131, 0)),
    }
    signaled = []

    class ReapedProcess:
        pid = 10
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(tool, "_process_identity", live.get)
    monkeypatch.setattr(tool, "_termination_process_states",
                        lambda _timeout: {pid: "Z" for pid in live})
    monkeypatch.setattr(tool, "_signal_owned_pid",
                        lambda pid, _identity, signum: signaled.append((pid, signum)))
    monkeypatch.setattr(tool.os, "killpg", lambda *_args: pytest.fail("stale PGID"))
    tool._terminate_owned_processes(ReapedProcess(), observed)
    assert signaled
    assert {pid for pid, _signum in signaled} == {12}


def test_signal_rechecks_start_identity_immediately_before_delivery(monkeypatch):
    tool = _load_tool_module()
    identity = tool._ProcessIdentity
    original = identity(10, 1, (100, 10))
    monkeypatch.setattr(tool, "_process_identity",
                        lambda _pid: identity(10, 1, (100, 11)))
    monkeypatch.setattr(tool.os, "kill", lambda *_args: pytest.fail("reused PID"))
    if hasattr(tool.os, "pidfd_open"):
        monkeypatch.setattr(tool.os, "pidfd_open", lambda *_args: 91)
        monkeypatch.setattr(tool.os, "close", lambda _fd: None)
        monkeypatch.setattr(tool.signal, "pidfd_send_signal",
                            lambda *_args: pytest.fail("reused pidfd"))
    tool._signal_owned_pid(10, original, signal.SIGTERM)


def test_linux_identity_parses_kernel_ticks_after_a_complex_process_name(monkeypatch):
    tool = _load_tool_module()
    fields = ["S", "10", "20", "30"] + ["0"] * 15 + ["987654321"]
    raw = "123 (a name with ) parentheses) " + " ".join(fields)
    monkeypatch.setattr(tool.sys, "platform", "linux")
    monkeypatch.setattr(tool.Path, "read_text", lambda _path: raw)
    assert tool._process_identity(123) == tool._ProcessIdentity(30, 10, (987654321, 0))


@pytest.mark.parametrize("reused_during_read", [False, True])
def test_darwin_identity_uses_both_kernel_start_time_fields(
    monkeypatch, reused_during_read,
):
    tool = _load_tool_module()
    calls = []

    def pidinfo(pid, flavor, arg, pointer, size):
        assert (pid, flavor, arg, size) == (123, 3, 0, 136)
        info = pointer._obj
        info.pid = pid
        info.ppid = 10
        info.start_sec = 1723456789
        info.start_usec = 100 + (int(reused_during_read) if calls else 0)
        calls.append(pid)
        return size

    monkeypatch.setattr(tool.sys, "platform", "darwin")
    monkeypatch.setattr(tool, "_DARWIN_PROC_PIDINFO", pidinfo)
    monkeypatch.setattr(tool.os, "getsid", lambda _pid: 99)
    identity = tool._process_identity(123)
    assert len(calls) == 2
    if reused_during_read:
        assert identity is None
    else:
        assert identity == tool._ProcessIdentity(99, 10, (1723456789, 100))


def test_process_tree_sampler_records_child_rss_and_completion(tmp_path: Path):
    result = tmp_path / "result.json"
    samples = tmp_path / "samples.tsv"
    stdout = tmp_path / "target.stdout"
    stderr = tmp_path / "target.stderr"
    child_code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.20)']); "
        "time.sleep(0.25); p.wait(); print('done')"
    )
    run = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--result",
            str(result),
            "--samples",
            str(samples),
            "--stdout",
            str(stdout),
            "--stderr",
            str(stderr),
            "--cwd",
            str(ROOT),
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--progress-interval",
            "1",
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            child_code,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    receipt = json.loads(result.read_text(encoding="utf-8"))
    assert receipt["status"] == "COMPLETE"
    assert receipt["returncode"] == 0
    assert isinstance(receipt["environment"], dict)
    assert receipt["environment"]["PATH"]
    assert receipt["sample_count"] >= 2
    assert receipt["peak_tree_rss_bytes"] > 0
    assert receipt["peak_process_count"] >= 2
    assert stdout.read_text(encoding="utf-8").strip() == "done"
    assert samples.read_text(encoding="utf-8").startswith(
        "elapsed_s\ttree_rss_bytes\tprocess_count"
    )


@pytest.mark.parametrize("nested_session", [False, True])
def test_parent_exit_cleans_an_orphaned_child_process_group(tmp_path: Path, nested_session):
    result = tmp_path / "result.json"
    stdout = tmp_path / "target.stdout"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        + ("start_new_session=True," if nested_session else "process_group=0,")
        + "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(p.pid,flush=True); time.sleep(0.25)"
    )
    child_pid = 0
    try:
        run = subprocess.run(
            [sys.executable, str(TOOL), "--result", str(result),
             "--samples", str(tmp_path / "samples.tsv"), "--stdout", str(stdout),
             "--stderr", str(tmp_path / "target.stderr"), "--cwd", str(ROOT),
             "--timeout", "5", "--interval", "0.02", "--no-performance-lock",
             "--", sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )
        child_pid = int(stdout.read_text().strip())
        assert run.returncode == 0, run.stdout + run.stderr
        receipt = json.loads(result.read_text())
        assert child_pid in receipt["post_exit_cleanup_pids"]
        state = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="],
                               capture_output=True, text=True, timeout=2).stdout.strip()
        assert not state or state.startswith("Z"), state
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize("nested_session", [False, True])
def test_resident_grandchild_stays_charged_after_worker_leader_is_reaped(
    tmp_path: Path, nested_session: bool,
):
    tool = _load_tool_module()
    resident = tmp_path / "resident.py"
    resident.write_text(textwrap.dedent("""\
        import os, signal, sys, time
        from pathlib import Path
        directory = Path(sys.argv[1])
        memory = bytearray(24 * 1024 * 1024)
        for offset in range(0, len(memory), 4096):
            memory[offset] = 1
        def terminate(*_args):
            (directory / 'terminated').write_text('resident cleaned')
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminate)
        (directory / 'resident.pid').write_text(str(os.getpid()))
        time.sleep(15)
        """), encoding="utf-8")
    leader = tmp_path / "leader.py"
    leader.write_text(textwrap.dedent("""\
        import subprocess, sys, time
        from pathlib import Path
        directory = Path(sys.argv[1])
        child = subprocess.Popen(
            [sys.executable, str(directory / 'resident.py'), str(directory)],
            process_group=0)
        while not (directory / 'leader-exit').exists():
            time.sleep(0.01)
        """), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent("""\
        import json, os, subprocess, sys, time
        from pathlib import Path
        directory = Path(sys.argv[1])
        leader = subprocess.Popen(
            [sys.executable, str(directory / 'leader.py'), str(directory)],
            start_new_session=sys.argv[2] == 'True')
        while not (directory / 'resident.pid').exists():
            time.sleep(0.01)
        resident_pid = int((directory / 'resident.pid').read_text())
        state_path = Path(os.environ['PCC_WORKER_TREE_STATE_PATH'])
        while True:
            if state_path.exists():
                rows = [line.split() for line in state_path.read_text().splitlines()[3:]]
                if any(int(row[0]) == resident_pid and int(row[2]) >= 24 * 1024 * 1024
                       for row in rows):
                    break
            time.sleep(0.01)
        (directory / 'leader-exit').touch()
        assert leader.wait(timeout=3) == 0
        (directory / 'reaped.json').write_text(json.dumps({
            'driver': os.getpid(), 'leader': leader.pid, 'resident': resident_pid,
            'reaped_at': time.monotonic()}))
        while not (directory / 'finish').exists():
            time.sleep(0.01)
        """), encoding="utf-8")
    result = tmp_path / "result.json"
    samples = tmp_path / "samples.tsv"
    state_path = Path(str(result) + ".worker-rss.tsv")
    guard = subprocess.Popen(
        [sys.executable, str(TOOL), "--result", str(result),
         "--samples", str(samples), "--stdout", str(tmp_path / "stdout"),
         "--stderr", str(tmp_path / "stderr"), "--cwd", str(ROOT),
         "--timeout", "10", "--interval", "0.02",
         "--max-tree-rss-bytes", str(128 * 1024 * 1024),
         "--no-performance-lock", "--", sys.executable, str(driver),
         str(tmp_path), str(nested_session)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    resident_identity = None
    resident_pid = 0
    try:
        deadline = time.monotonic() + 6
        ready_path = tmp_path / "reaped.json"
        while not ready_path.exists() and time.monotonic() < deadline:
            assert guard.poll() is None
            time.sleep(0.01)
        assert ready_path.exists(), "the worker leader must be observed and reaped"
        ready = json.loads(ready_path.read_text())
        resident_pid = ready["resident"]
        resident_identity = tool._process_identity(resident_pid)
        assert resident_identity is not None
        orphan_sample_times = set()
        while time.monotonic() < deadline and len(orphan_sample_times) < 3:
            state = state_path.read_text().splitlines()
            assert state[0] == "pcc.worker-tree-rss.v1"
            assert int(state[2]) == 128 * 1024 * 1024
            rows = {int(fields[0]): (int(fields[1]), int(fields[2]))
                    for fields in (line.split() for line in state[3:])}
            sampled_at = float(state[1])
            if sampled_at > ready["reaped_at"] and resident_pid in rows:
                assert rows[resident_pid][0] != ready["leader"]
                assert rows[resident_pid][1] >= 24 * 1024 * 1024
                assert ready["driver"] in rows
                assert ready["leader"] not in rows
                orphan_sample_times.add(sampled_at)
            time.sleep(0.02)
        assert len(orphan_sample_times) >= 3, "live orphan RSS vanished after leader reap"
        # The very same retained rows must feed the aggregate guard, not just
        # the scheduler's observation sidecar.
        sample = samples.read_text().splitlines()[-1].split("\t")
        assert int(sample[1]) >= 24 * 1024 * 1024
        assert int(sample[2]) >= 2
        assert int(sample[3]) == resident_pid
        (tmp_path / "finish").touch()
        output, errors = guard.communicate(timeout=5)
        assert guard.returncode == 0, output + errors
        receipt = json.loads(result.read_text())
        assert receipt["status"] == "COMPLETE"
        assert resident_pid in receipt["post_exit_cleanup_pids"]
        assert (tmp_path / "terminated").read_text() == "resident cleaned"
        state = subprocess.run(
            ["ps", "-p", str(resident_pid), "-o", "stat="],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        assert not state or state.startswith("Z"), state
    finally:
        (tmp_path / "finish").touch()
        if guard.poll() is None:
            guard.send_signal(signal.SIGINT)
            guard.communicate(timeout=6)
        if resident_identity is not None:
            tool._signal_owned_pid(resident_pid, resident_identity, signal.SIGKILL)


def test_process_tree_sampler_double_sigint_cleans_target_and_writes_receipt(
    tmp_path: Path,
):
    result = tmp_path / "result.json"
    samples = tmp_path / "samples.tsv"
    stdout = tmp_path / "target.stdout"
    stderr = tmp_path / "target.stderr"
    process = subprocess.Popen(
        [
            sys.executable,
            str(TOOL),
            "--result",
            str(result),
            "--samples",
            str(samples),
            "--stdout",
            str(stdout),
            "--stderr",
            str(stderr),
            "--cwd",
            str(ROOT),
            "--timeout",
            "30",
            "--interval",
            "0.02",
            "--progress-interval",
            "10",
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    target_pid = 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if stdout.exists() and stdout.read_text(encoding="utf-8").strip():
            target_pid = int(stdout.read_text(encoding="utf-8").strip())
            break
        time.sleep(0.02)
    assert target_pid > 0
    live_samples = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        live_samples = samples.read_text(encoding="utf-8").splitlines()
        if len(live_samples) >= 2:
            break
        time.sleep(0.02)
    assert len(live_samples) >= 2
    live_receipt = {}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        live_receipt = json.loads(result.read_text(encoding="utf-8"))
        if live_receipt.get("sample_count", 0) >= 1:
            break
        time.sleep(0.02)
    assert live_receipt["status"] == "RUNNING"
    assert live_receipt["sample_count"] >= 1

    process.send_signal(signal.SIGINT)
    time.sleep(0.02)
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass
    tool_stdout, tool_stderr = process.communicate(timeout=10)

    assert process.returncode == 130, tool_stdout + tool_stderr
    receipt = json.loads(result.read_text(encoding="utf-8"))
    assert receipt["status"] == "INTERRUPTED"
    with pytest.raises(ProcessLookupError):
        os.kill(target_pid, 0)


def test_process_tree_sampler_retries_one_transient_ps_timeout(
    tmp_path: Path,
    monkeypatch,
):
    tool = _load_tool_module()
    real_run = tool.subprocess.run
    calls = 0

    def one_timeout(command, *args, **kwargs):
        nonlocal calls
        if command and command[0] == "ps" and calls == 0:
            calls += 1
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(tool.subprocess, "run", one_timeout)
    args = tool._parser().parse_args(
        [
            "--result",
            str(tmp_path / "result.json"),
            "--samples",
            str(tmp_path / "samples.tsv"),
            "--stdout",
            str(tmp_path / "stdout"),
            "--stderr",
            str(tmp_path / "stderr"),
            "--cwd",
            str(ROOT),
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--progress-interval",
            "1",
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        ]
    )

    receipt = tool.run(args)

    assert receipt["status"] == "COMPLETE"
    assert receipt["returncode"] == 0
    assert receipt["process_table_retry_count"] == 1
    assert receipt["process_table_timeouts_s"] == [5.0, 20.0]


def test_imported_sampler_installs_and_restores_interrupt_handler(
    monkeypatch,
):
    tool = _load_tool_module()
    previous = object()
    events = []
    expected = {"status": "COMPLETE", "returncode": 0}

    monkeypatch.setattr(tool.signal, "getsignal", lambda _signum: previous)
    monkeypatch.setattr(
        tool.signal,
        "signal",
        lambda signum, handler: events.append((signum, handler)),
    )
    monkeypatch.setattr(tool, "_run", lambda _args: expected)

    assert tool.run(object()) is expected
    assert events == [
        (tool.signal.SIGINT, tool._request_interrupt),
        (tool.signal.SIGINT, previous),
    ]


def test_process_table_keeps_full_worker_command_and_manifest(monkeypatch):
    tool = _load_tool_module()

    class Result:
        returncode = 0
        stderr = ""
        stdout = (
            "  123   9 4096 /tmp/pcc1 --pcc-python-multi-codegen-worker "
            "/tmp/worker_17.manifest\n"
        )

    monkeypatch.setattr(tool.subprocess, "run", lambda *_args, **_kwargs: Result())

    table, retries = tool._process_table()

    assert retries == 0
    assert table[123] == (
        9,
        4096 * 1024,
        "/tmp/pcc1 --pcc-python-multi-codegen-worker /tmp/worker_17.manifest",
    )
    snapshot = tool._process_snapshot(table)
    assert snapshot[0]["manifest_paths"] == ["/tmp/worker_17.manifest"]


def test_safety_table_avoids_all_process_argv_and_queries_only_largest(monkeypatch):
    tool = _load_tool_module()
    commands = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = "  123   9 4096\n"

    def fake_run(command, *_args, **_kwargs):
        commands.append(command)
        if "-p" in command:
            result = Result()
            result.stdout = (
                "/tmp/pcc1 --pcc-python-multi-codegen-worker "
                "/tmp/worker_17.manifest\n"
            )
            return result
        return Result()

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    table, retries = tool._process_table(
        timeouts_s=tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S,
        include_command=False,
    )

    assert retries == 0
    assert table[123] == (9, 4096 * 1024, "")
    assert commands[0] == ["ps", "-Ao", "pid=,ppid=,rss="]
    command = tool._process_command(123)
    assert command.endswith("/tmp/worker_17.manifest")
    assert commands[1] == ["ps", "-ww", "-p", "123", "-o", "command="]
    snapshot = tool._process_snapshot(table, {123: command})
    assert snapshot[0]["manifest_paths"] == ["/tmp/worker_17.manifest"]


def test_safety_process_table_failure_uses_only_bounded_retries(monkeypatch):
    tool = _load_tool_module()
    observed = []

    def always_timeout(command, *args, **kwargs):
        observed.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(tool.subprocess, "run", always_timeout)

    with pytest.raises(tool.ProcessTreeSampleError) as raised:
        tool._process_table(timeouts_s=tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)

    assert observed == list(tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)
    assert sum(observed) <= 4.0
    assert raised.value.retry_count == len(tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert raised.value.__cause__.timeout == tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S[-1]


def test_preflight_rejection_persists_receipt_without_starting_target(
    tmp_path: Path,
    monkeypatch,
):
    tool = _load_tool_module()
    result = tmp_path / "result.json"

    def reject(**_kwargs):
        raise tool.ProcessTreeSampleError("insufficient reclaimable memory")

    monkeypatch.setattr(tool, "_darwin_resource_preflight", reject)
    monkeypatch.setattr(
        tool.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("target must not start after a failed preflight")
        ),
    )
    args = tool._parser().parse_args(
        [
            "--result",
            str(result),
            "--samples",
            str(tmp_path / "samples.tsv"),
            "--stdout",
            str(tmp_path / "stdout"),
            "--stderr",
            str(tmp_path / "stderr"),
            "--cwd",
            str(ROOT),
            "--timeout",
            "5",
            "--max-tree-rss-bytes",
            str(8 * 1024 * 1024 * 1024),
            "--darwin-preflight-reserve-bytes",
            str(8 * 1024 * 1024 * 1024),
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            "print('must-not-run')",
        ]
    )

    with pytest.raises(tool.ProcessTreeSampleError, match="reclaimable"):
        tool.run(args)

    receipt = json.loads(result.read_text(encoding="utf-8"))
    assert receipt["status"] == "PREFLIGHT_REJECTED"
    assert "insufficient reclaimable memory" in receipt["error"]


def test_performance_lock_rejection_records_no_started_target(tmp_path, monkeypatch):
    import contextlib

    tool = _load_tool_module()
    result = tmp_path / "result.json"
    started = []

    @contextlib.contextmanager
    def held_lock():
        raise tool.compile_ab.CompileABError("performance lock is already held")
        yield  # pragma: no cover

    def forbidden_start(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("a rejected lock must not launch the target")

    monkeypatch.setattr(tool.compile_ab, "_performance_lock", held_lock)
    monkeypatch.setattr(tool.subprocess, "Popen", forbidden_start)
    args = tool._parser().parse_args([
        "--result", str(result),
        "--samples", str(tmp_path / "samples.tsv"),
        "--stdout", str(tmp_path / "stdout"),
        "--stderr", str(tmp_path / "stderr"),
        "--cwd", str(ROOT),
        "--timeout", "5",
        "--", sys.executable, "-c", "print('must-not-run')",
    ])
    with pytest.raises(tool.ProcessTreeSampleError, match="performance lock"):
        tool.run(args)

    receipt = json.loads(result.read_text())
    assert receipt["status"] == "LOCK_REJECTED"
    assert receipt["completed_at_utc"] >= receipt["started_at_utc"]
    assert receipt["elapsed_s"] >= 0
    assert "CompileABError: performance lock is already held" in receipt["error"]
    assert "returncode" not in receipt
    assert not started
    assert not (tmp_path / "stdout").exists()
    assert not (tmp_path / "samples.tsv").exists()


def test_process_tree_sampler_persists_terminal_receipt_after_ps_retries_fail(
    tmp_path: Path,
    monkeypatch,
):
    tool = _load_tool_module()

    def always_timeout(command, *args, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(tool.subprocess, "run", always_timeout)
    result = tmp_path / "result.json"
    args = tool._parser().parse_args(
        [
            "--result",
            str(result),
            "--samples",
            str(tmp_path / "samples.tsv"),
            "--stdout",
            str(tmp_path / "stdout"),
            "--stderr",
            str(tmp_path / "stderr"),
            "--cwd",
            str(ROOT),
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--progress-interval",
            "1",
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        ]
    )

    with pytest.raises(tool.ProcessTreeSampleError):
        tool.run(args)

    receipt = json.loads(result.read_text(encoding="utf-8"))
    assert receipt["status"] == "SAMPLER_ERROR"
    assert receipt["process_table_retry_count"] == 2
    assert "bounded process-table retries" in receipt["error"]


def test_process_tree_sampler_memory_limit_cleans_target_and_records_receipt(
    tmp_path: Path,
):
    result = tmp_path / "result.json"
    samples = tmp_path / "samples.tsv"
    stdout = tmp_path / "target.stdout"
    stderr = tmp_path / "target.stderr"
    run = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--result",
            str(result),
            "--samples",
            str(samples),
            "--stdout",
            str(stdout),
            "--stderr",
            str(stderr),
            "--cwd",
            str(ROOT),
            "--timeout",
            "30",
            "--interval",
            "0.02",
            "--progress-interval",
            "10",
            "--max-tree-rss-bytes",
            "1",
            "--no-performance-lock",
            "--",
            sys.executable,
            "-c",
            "import os,time; print(os.getpid(), flush=True); time.sleep(30)",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert run.returncode == 125, run.stdout + run.stderr
    receipt = json.loads(result.read_text(encoding="utf-8"))
    assert receipt["status"] == "MEMORY_LIMIT"
    assert receipt["max_tree_rss_bytes"] == 1
    assert receipt["peak_tree_rss_bytes"] > 1
    assert receipt["largest_process_observed"]["command"]
    assert receipt["terminal_processes"]
    # A one-byte cap may stop the interpreter before it can print its PID.
    # The sampler already identifies the owned target before enforcing it.
    target_pid = receipt["largest_process_observed"]["pid"]
    assert target_pid in {row["pid"] for row in receipt["terminal_processes"]}
    printed_pid = stdout.read_text(encoding="utf-8").strip()
    if printed_pid:
        assert int(printed_pid) == target_pid
    with pytest.raises(ProcessLookupError):
        os.kill(target_pid, 0)


def _stub_preflight_memory(
    tool,
    monkeypatch,
    *,
    reclaimable_bytes: int,
    swap_used_bytes: int,
    swap_free_bytes: int,
    swap_total_bytes: int = 4 * 1024 * 1024 * 1024,
    disk_free_bytes: int = 500 * 1024 * 1024 * 1024,
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(tool, "_parse_vm_stat_reclaimable", lambda _raw: reclaimable_bytes)
    monkeypatch.setattr(
        tool,
        "_parse_swapusage",
        lambda _raw: (swap_total_bytes, swap_used_bytes, swap_free_bytes),
    )
    monkeypatch.setattr(
        tool.shutil, "disk_usage", lambda _p: type("D", (), {"free": disk_free_bytes})()
    )


_CAP = 8 * 1024 * 1024 * 1024
_RESERVE = 8 * 1024 * 1024 * 1024


def test_swap_pressure_waived_when_reclaimable_ram_is_ample(monkeypatch):
    """A 96 GiB-RAM / 4 GiB-swap host looks swap-pressured but is not starved.

    reclaimable 52 GiB >= 2x the 16 GiB required budget, so the tiny-swap
    pressure refusal is waived and the guarded tree is allowed.
    """
    tool = _load_tool_module()
    _stub_preflight_memory(
        tool,
        monkeypatch,
        reclaimable_bytes=52 * 1024 * 1024 * 1024,
        swap_used_bytes=int(2.8 * 1024 * 1024 * 1024),  # used*2 > 4 GiB total
        swap_free_bytes=int(1.2 * 1024 * 1024 * 1024),  # < 4 GiB
    )
    info = tool._darwin_resource_preflight(
        max_tree_rss_bytes=_CAP, reserve_bytes=_RESERVE
    )
    assert info["swap_pressure_waived_by_reclaimable"] is True


def test_swap_pressure_still_refuses_when_reclaimable_is_low(monkeypatch):
    """A genuinely memory-starved host (low reclaimable) still fails closed."""
    tool = _load_tool_module()
    _stub_preflight_memory(
        tool,
        monkeypatch,
        reclaimable_bytes=20 * 1024 * 1024 * 1024,  # < 2x the 16 GiB budget
        swap_used_bytes=int(2.8 * 1024 * 1024 * 1024),
        swap_free_bytes=int(1.2 * 1024 * 1024 * 1024),
    )
    with pytest.raises(tool.ProcessTreeSampleError, match="swap is already pressured"):
        tool._darwin_resource_preflight(max_tree_rss_bytes=_CAP, reserve_bytes=_RESERVE)


def test_reclaimable_hard_floor_still_fails_closed(monkeypatch):
    """The reclaimable < required hard floor is independent of the swap waiver."""
    tool = _load_tool_module()
    _stub_preflight_memory(
        tool,
        monkeypatch,
        reclaimable_bytes=4 * 1024 * 1024 * 1024,  # < 16 GiB required
        swap_used_bytes=0,
        swap_free_bytes=4 * 1024 * 1024 * 1024,
    )
    with pytest.raises(
        tool.ProcessTreeSampleError, match="insufficient reclaimable memory"
    ):
        tool._darwin_resource_preflight(max_tree_rss_bytes=_CAP, reserve_bytes=_RESERVE)


@pytest.mark.parametrize("nested_session", [False, True])
@pytest.mark.parametrize("child_action", ["flush", "ignore"])
def test_timeout_preserves_descendant_grace_and_cleans_tree(
    tmp_path: Path,
    nested_session: bool,
    child_action: str,
):
    # The supervisor exits immediately on SIGTERM; its child either needs time
    # to flush after that exit or ignores SIGTERM and must be killed at the bound.
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "directory = Path(sys.argv[1])\n"
        "action = sys.argv[2]\n"
        "def terminate(signum, frame):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    (directory / 'term').write_text('received')\n"
        "    if action == 'ignore':\n"
        "        return\n"
        "    time.sleep(0.3)\n"
        "    print('profile flushed', flush=True)\n"
        "    (directory / 'flushed').write_text('complete')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, terminate)\n"
        "os.write(int(sys.argv[3]), b'ready\\n')\n"
        "os.close(int(sys.argv[3]))\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "directory = Path(sys.argv[1])\n"
        "ready_read, ready_write = os.pipe()\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, str(directory / 'worker.py'), str(directory),\n"
        "     sys.argv[2], str(ready_write)],\n"
        "    start_new_session=sys.argv[3] == 'True', pass_fds=(ready_write,))\n"
        "os.close(ready_write)\n"
        "assert os.read(ready_read, 6) == b'ready\\n'\n"
        "os.close(ready_read)\n"
        "signal.signal(signal.SIGTERM, lambda *_: os._exit(0))\n"
        "(directory / 'ready.json').write_text(json.dumps([os.getpid(), child.pid]))\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    result = tmp_path / "result.json"
    command = [
        sys.executable, str(TOOL), "--result", str(result),
        "--samples", str(tmp_path / "samples.tsv"),
        "--stdout", str(tmp_path / "target.stdout"),
        "--stderr", str(tmp_path / "target.stderr"),
        "--cwd", str(ROOT), "--timeout", "1", "--interval", "0.02",
        "--max-tree-rss-bytes", str(128 * 1024 * 1024),
        "--no-performance-lock", "--", sys.executable, str(supervisor),
        str(tmp_path), child_action, str(nested_session),
    ]
    pids = []
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=8)
        pids = json.loads((tmp_path / "ready.json").read_text(encoding="utf-8"))
        assert run.returncode == 124, run.stdout + run.stderr
        receipt = json.loads(result.read_text(encoding="utf-8"))
        assert receipt["status"] == "TIMEOUT"
        assert receipt["returncode"] == 0  # The supervisor exited on SIGTERM.
        assert receipt["timeout_s"] == 1
        assert receipt["max_tree_rss_bytes"] == 128 * 1024 * 1024
        assert set(pids) <= {row["pid"] for row in receipt["terminal_processes"]}
        assert (tmp_path / "term").read_text(encoding="utf-8") == "received"
        assert receipt["elapsed_s"] < 4
        if child_action == "flush":
            assert (tmp_path / "flushed").read_text(encoding="utf-8") == "complete"
            assert (tmp_path / "target.stdout").read_text(encoding="utf-8") == (
                "profile flushed\n"
            )
            assert receipt["elapsed_s"] < 2.5  # Do not always spend all grace.
        else:
            assert not (tmp_path / "flushed").exists()
            assert receipt["elapsed_s"] >= 3
        for pid in pids:
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            assert not state or state.startswith("Z"), (pid, state)
    finally:
        ready = tmp_path / "ready.json"
        if ready.exists():
            pids = json.loads(ready.read_text(encoding="utf-8"))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("root_exited", [False, True])
def test_termination_returns_for_exited_child_and_excludes_unrelated_session(
    tmp_path: Path, root_exited: bool,
):
    tool = _load_tool_module()
    child_pid_file = tmp_path / "child.pid"
    process = subprocess.Popen(
        [
            sys.executable, "-c",
            "import subprocess, sys, time; from pathlib import Path; "
            "child = subprocess.Popen([sys.executable, '-c', 'pass'], "
            "start_new_session=True); child.wait(); "
            "Path(sys.argv[1]).write_text(str(child.pid)); "
            "time.sleep(0 if sys.argv[2] == 'True' else 10)",
            str(child_pid_file), str(root_exited),
        ],
        start_new_session=True,
    )
    root_identity = tool._process_identity(process.pid)
    assert root_identity is not None
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        if root_exited:
            process.wait(timeout=3)
        # Neither an exited child nor a reused PID with a different birth can
        # establish ownership, even when a recorded session number matches.
        observed_processes = {
            process.pid: root_identity,
            child_pid: tool._ProcessIdentity(child_pid, process.pid, (-1, 0)),
            unrelated.pid: tool._ProcessIdentity(process.pid, process.pid, (-1, 0)),
        }
        started = time.monotonic()
        tool._terminate_owned_processes(process, observed_processes)
        assert time.monotonic() - started < 1
        assert process.poll() is not None
        assert unrelated.poll() is None
    finally:
        for owned in (process, unrelated):
            if owned.poll() is None:
                owned.kill()
            owned.wait(timeout=3)


def _mac_ci_memory_observation(available=3 * 1024**3):
    # Synthetic availability on a 7 GB runner; failed CI recorded no exact value.
    return {
        "platform": "darwin", "reclaimable_bytes": available,
        "disk_free_bytes": 20 * 1024**3, "swap_total_bytes": 0,
        "swap_used_bytes": 0, "swap_free_bytes": 0,
    }


def _mac_ci_guard_args(tool, tmp_path, *, automatic=True):
    cap = ["--auto-tree-rss-ceiling-bytes", str(4 * 1024**3),
           "--min-tree-rss-bytes", str(2 * 1024**3)] if automatic else [
               "--max-tree-rss-bytes", str(4 * 1024**3)]
    return tool._parser().parse_args([
        "--result", str(tmp_path / "result.json"),
        "--samples", str(tmp_path / "samples.tsv"),
        "--stdout", str(tmp_path / "stdout"), "--stderr", str(tmp_path / "stderr"),
        "--cwd", str(tmp_path), "--timeout", "5", "--no-performance-lock",
        "--darwin-preflight-reserve-bytes", str(1024**3 // 2), *cap,
        "--", "synthetic-native-worker",
    ])


def test_mac_ci_guard_auto_selection_reaches_receipt_environment_and_state(tmp_path, monkeypatch):
    tool = _load_tool_module()
    observation = _mac_ci_memory_observation()
    monkeypatch.setattr(tool, "_host_memory_observation", lambda: observation)
    launched = []

    class Child:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

    def launch(command, **kwargs):
        launched.append(kwargs["env"])
        return Child()

    monkeypatch.setattr(tool.subprocess, "Popen", launch)
    monkeypatch.setattr(tool, "_process_identity", lambda pid: tool._ProcessIdentity(pid, 1, (1, 0)))
    tables = iter([({123: (1, 1024, "synthetic-native-worker")}, 0), ({}, 0)])
    monkeypatch.setattr(tool, "_timed_process_table",
                        lambda **kwargs: (*next(tables), tool.time.monotonic()))
    monkeypatch.setattr(tool, "_process_table", lambda **kwargs: next(tables))
    receipt = tool.run(_mac_ci_guard_args(tool, tmp_path))
    assert receipt["status"] == "COMPLETE" and receipt["returncode"] == 0
    budget = 5 * 1024**3 // 2
    assert receipt["max_tree_rss_bytes"] == budget
    assert receipt["memory_budget_selection"]["observation"] == observation
    assert receipt["resource_preflight"]["reserve_bytes"] == 1024**3 // 2
    assert len(launched) == 1
    assert launched[0]["PCC_WORKER_TREE_BUDGET_BYTES"] == str(budget)
    assert receipt["environment"]["PCC_WORKER_TREE_BUDGET_BYTES"] == str(budget)
    state = Path(launched[0]["PCC_WORKER_TREE_STATE_PATH"]).read_text().splitlines()
    assert state[0] == "pcc.worker-tree-rss.v1" and int(state[2]) == budget


@pytest.mark.parametrize("automatic,available,message", [
    (True, 2 * 1024**3, "minimum tree budget"),
    (False, 3 * 1024**3, "insufficient reclaimable"),
])
def test_mac_ci_guard_rejection_persists_observation_without_launch(
    tmp_path, monkeypatch, automatic, available, message,
):
    tool = _load_tool_module()
    observation = _mac_ci_memory_observation(available)
    monkeypatch.setattr(tool, "_host_memory_observation", lambda: observation)
    monkeypatch.setattr(tool, "_darwin_resource_observation", lambda: observation)

    def forbidden_launch(*args, **kwargs):
        pytest.fail("target launched after rejected memory admission")

    monkeypatch.setattr(tool.subprocess, "Popen", forbidden_launch)
    monkeypatch.setattr(tool, "_process_table", forbidden_launch)
    monkeypatch.setattr(tool, "_timed_process_table", forbidden_launch)
    with pytest.raises(tool.ProcessTreeSampleError, match=message):
        tool.run(_mac_ci_guard_args(tool, tmp_path, automatic=automatic))
    receipt = json.loads((tmp_path / "result.json").read_text())
    assert receipt["status"] == "PREFLIGHT_REJECTED"
    if automatic:
        assert receipt["memory_budget_selection"]["observation"] == observation
        assert receipt["memory_budget_selection"]["minimum_tree_rss_bytes"] == 2 * 1024**3
    else:
        assert receipt["resource_preflight"]["reclaimable_bytes"] == available
        assert receipt["resource_preflight"]["reserve_bytes"] == 1024**3 // 2
    assert not (tmp_path / "result.json.worker-rss.tsv").exists()


@pytest.mark.parametrize("first_timeout,successful_duration,identity_delay,timeouts", [
    (False, 0.25, 0.0, (1.0, 3.0)),
    (True, 0.25, 0.0, (1.0, 3.0)),
    (True, 2.25, 0.0, (1.0, 3.0)),
    (True, 0.25, 2.1, (1.0, 3.0)),
    (True, 0.25, 0.0, (0.5, 1.25)),
])
def test_guard_publishes_adopted_attempt_start_and_original_recovery_deadline(
    tmp_path, monkeypatch, first_timeout, successful_duration, identity_delay, timeouts,
):
    from types import SimpleNamespace
    from pcc.frontends.python import worker_resource_plan as policy
    from pcc.frontends.python.worker_resource_host_recovery import read_tree_recovery

    tool = _load_tool_module()
    clock = SimpleNamespace(now=100.0)
    clock.monotonic = lambda: clock.now
    attempts, launched, identities = [], [], []
    monkeypatch.setattr(tool, "time", clock)
    monkeypatch.setattr(policy, "time", clock)
    monkeypatch.setattr(tool, "_SAFETY_PROCESS_TABLE_TIMEOUTS_S", timeouts)

    def memory_observation():
        clock.now += 0.75  # Preflight time must consume the original deadline.
        return _mac_ci_memory_observation()

    def identity(pid):
        identities.append(pid)
        if len(identities) > 1:
            clock.now += identity_delay
        return tool._ProcessIdentity(pid, 1, (1, 0))

    def fake_run(command, **kwargs):
        assert command == ["ps", "-Ao", "pid=,ppid=,rss="]
        attempts.append((clock.now, kwargs["timeout"]))
        if first_timeout and len(attempts) == 1:
            clock.now += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        success_index = 2 if first_timeout else 1
        if len(attempts) == success_index:
            clock.now += successful_duration
            output = "123 1 64\n"
        else:
            output = ""  # Reaped root: unchanged pair-return cleanup path.
        return SimpleNamespace(returncode=0, stderr="", stdout=output)

    def launch(command, **kwargs):
        launched.append(dict(kwargs["env"]))
        return SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)

    def forbidden_cleanup(*args, **kwargs):
        pytest.fail("synthetic completed child must not invoke process cleanup")

    monkeypatch.setattr(tool, "_host_memory_observation", memory_observation)
    monkeypatch.setattr(tool, "_process_identity", identity)
    monkeypatch.setattr(tool, "_process_command", lambda pid: "synthetic")
    monkeypatch.setattr(tool, "_terminate_owned_processes", forbidden_cleanup)
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    monkeypatch.setattr(tool.subprocess, "Popen", launch)
    receipt = tool.run(_mac_ci_guard_args(tool, tmp_path))
    assert receipt["status"] == "COMPLETE"
    environment = launched[0]
    allowance, deadline = read_tree_recovery(environment[policy.TREE_RECOVERY_ENV])
    assert allowance == sum(timeouts) and deadline == 105.0
    assert receipt["process_table_timeouts_s"] == list(timeouts)
    assert receipt["process_table_retry_count"] == int(first_timeout)
    state_path = Path(environment[policy.TREE_STATE_ENV])
    rows = state_path.read_text().splitlines()
    adopted_start = 100.75 + (timeouts[0] if first_timeout else 0)
    assert float(rows[1]) == adopted_start
    assert attempts[int(first_timeout)][0] == adopted_start
    parsed = policy.read_tree_state(
        str(state_path), int(environment["PCC_WORKER_TREE_BUDGET_BYTES"]),
        123, [], include_owner=True,
    )
    # A successful slow ps or identity lookup may already be stale on publish.
    # The 4-second nominal retry sum does not grant a 4-second admission age.
    assert (parsed is None) == (successful_duration + identity_delay > 2.0)
    assert policy.STATE_MAX_AGE_SECONDS == 2.0


def test_stage1_clean_environment_forwards_recovery_only_with_guard_state():
    # Source-only proof: no harness import, build, runtime provisioning or
    # execution of extracted code is needed to inspect this direct forwarding.
    import ast

    path = ROOT / "scripts" / "run_pcc_stage1_build.py"
    tree = ast.parse(path.read_text())
    state_key, recovery_key = "PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_RECOVERY"
    guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and ast.unparse(node.test) == "os.environ.get('" + state_key + "')"]
    assert len(guards) == 1
    outer = guards[0]
    assert not outer.orelse and len(outer.body) == 2
    inner = outer.body[1]
    assert isinstance(inner, ast.If) and not inner.orelse
    assert ast.unparse(inner.test) == "'" + recovery_key + "' in os.environ"
    assert len(inner.body) == 1 and isinstance(inner.body[0], ast.Assign)
    assignment = inner.body[0]
    assert ast.unparse(assignment.targets[0]) == "env['" + recovery_key + "']"
    assert ast.unparse(assignment.value) == "os.environ['" + recovery_key + "']"
