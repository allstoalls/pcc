"""Small host processes exercise package capture and separate-session cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from scripts import run_process_tree_sample as sampler
from tests.python import process_timeout


pytestmark = [
    pytest.mark.pcc_gate(probe=lambda: os.name == "posix"),
    pytest.mark.xdist_group("package-process-timeout"),
]


def _handlers():
    return {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}


def test_package_process_capture_preserves_success_and_nonzero(tmp_path):
    before = _handlers()
    for returncode in (0, 7):
        evidence = tmp_path / str(returncode)
        command = [
            sys.executable, "-c",
            "import os, sys; print(os.getcwd()); print(os.environ['PACKAGE_TOKEN']); "
            "print(sys.argv[1]); print('stderr marker', file=sys.stderr); "
            "sys.exit(int(sys.argv[2]))",
            "literal argument with spaces", str(returncode),
        ]
        environment = dict(os.environ, PACKAGE_TOKEN="unchanged environment")
        result = process_timeout.run_process_tree_timeout(
            command, evidence=evidence, timeout=3, cwd=tmp_path, env=environment,
        )
        assert result.args == command
        assert result.returncode == returncode
        assert result.stdout.splitlines() == [
            str(tmp_path), "unchanged environment", "literal argument with spaces",
        ]
        assert result.stderr == "stderr marker\n"
        assert (evidence / "stdout.log").read_text() == result.stdout
        assert (evidence / "stderr.log").read_text() == result.stderr
        receipt = json.loads((evidence / "result.json").read_text())
        assert receipt["returncode"] == returncode
        assert receipt["cleanup_errors"] == []
    assert _handlers() == before


def test_package_process_timeout_cleans_separate_session_and_retains_output(tmp_path):
    before = _handlers()
    descendant = textwrap.dedent("""
        import json, os, signal, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print('descendant stdout', flush=True)
        print('descendant stderr', file=sys.stderr, flush=True)
        with open(sys.argv[1], 'w') as stream:
            json.dump({'pid': os.getpid(), 'pgid': os.getpgrp()}, stream)
        time.sleep(10)
    """)
    command = [sys.executable, "-c", textwrap.dedent(f"""
        import json, os, subprocess, sys, time
        child = subprocess.Popen(
            [sys.executable, '-c', {descendant!r}, sys.argv[1]],
            start_new_session=True,
        )
        print('parent stdout', flush=True)
        print('parent stderr', file=sys.stderr, flush=True)
        with open(sys.argv[2], 'w') as stream:
            json.dump({{'pid': os.getpid(), 'pgid': os.getpgrp()}}, stream)
        child.wait()
    """), str(tmp_path / "descendant.json"), str(tmp_path / "parent.json")]
    evidence = tmp_path / "timeout"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        process_timeout.run_process_tree_timeout(
            command, evidence=evidence, timeout=1, check=True,
        )
    assert time.monotonic() - started < 6
    assert raised.value.cmd == command
    assert raised.value.timeout == 1
    assert b"parent stdout\n" in raised.value.output
    assert b"descendant stdout\n" in raised.value.output
    assert b"parent stderr\n" in raised.value.stderr
    assert b"descendant stderr\n" in raised.value.stderr
    assert (evidence / "stdout.log").read_bytes() == raised.value.output
    assert (evidence / "stderr.log").read_bytes() == raised.value.stderr
    parent = json.loads((tmp_path / "parent.json").read_text())
    child = json.loads((tmp_path / "descendant.json").read_text())
    assert parent["pgid"] != child["pgid"] == child["pid"]
    # The helper must stop both processes before returning. An orphan may
    # remain a zombie until the OS reaps it; that is no longer a live worker.
    for pid in (parent["pid"], child["pid"]):
        if sampler._process_identity(pid) is not None:
            states = sampler._termination_process_states(0.25)
            assert states is not None
            assert pid not in states or states[pid].startswith("Z")
    receipt = json.loads((evidence / "result.json").read_text())
    assert receipt["status"] == "TIMEOUT"
    assert receipt["timeout_s"] == 1
    assert receipt["cleanup_errors"] == []
    assert _handlers() == before


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_package_process_output_limit_is_bounded_and_fails(tmp_path, stream):
    evidence = tmp_path / "overflow"
    descriptor = 1 if stream == "stdout" else 2
    with pytest.raises(RuntimeError, match="exceeded 4096 captured bytes"):
        process_timeout.run_process_tree_timeout(
            [sys.executable, "-c", f"import os, time; os.write({descriptor}, b'x' * 131072); time.sleep(10)"],
            evidence=evidence, timeout=3, max_output_bytes=4096,
        )
    assert (evidence / (stream + ".log")).stat().st_size == 4096
    receipt = json.loads((evidence / "result.json").read_text())
    assert receipt["status"] == "FAILED"
    assert receipt["max_output_bytes_per_stream"] == 4096
    assert all(size <= 4096 for size in receipt["captured_bytes"].values())


@pytest.mark.parametrize("error", [RuntimeError("original failure"), KeyboardInterrupt()])
def test_package_process_cleanup_error_does_not_mask_original(tmp_path, monkeypatch, error):
    before = _handlers()
    observe = sampler._owned_process_pids
    terminate = sampler._terminate_owned_processes
    injected = False

    def observe_once(*args):
        nonlocal injected
        if not injected:
            injected = True
            raise error
        return observe(*args)

    def terminate_then_fail(*args):
        terminate(*args)
        raise RuntimeError("cleanup reporting failed")

    monkeypatch.setattr(sampler, "_owned_process_pids", observe_once)
    monkeypatch.setattr(sampler, "_terminate_owned_processes", terminate_then_fail)
    evidence = tmp_path / "original-error"
    with pytest.raises(type(error)) as raised:
        process_timeout.run_process_tree_timeout(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            evidence=evidence, timeout=3,
        )
    assert raised.value is error
    receipt = json.loads((evidence / "result.json").read_text())
    assert receipt["status"] == "FAILED"
    assert receipt["cleanup_errors"] == ["RuntimeError: cleanup reporting failed"]
    assert _handlers() == before


def test_package_process_check_preserves_called_process_error(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as raised:
        process_timeout.run_process_tree_timeout(
            [sys.executable, "-c", "import sys; print('bad plan'); print('failure detail', file=sys.stderr); sys.exit(9)"],
            evidence=tmp_path / "nonzero", timeout=3, check=True,
        )
    assert raised.value.returncode == 9
    assert raised.value.stdout == "bad plan\n"
    assert raised.value.stderr == "failure detail\n"


def _fake_tree_process(monkeypatch):
    """Real capture pipes, no child and no real PID/signal targets."""
    class Process:
        pid = 424242
        returncode = None

        def __init__(self):
            self.writers = []
            for name in ("stdout", "stderr"):
                reader, writer = os.pipe()
                setattr(self, name, os.fdopen(reader, "rb"))
                self.writers.append(writer)

        def poll(self):
            return self.returncode

    process = Process()

    def terminate(*_args):
        for writer in process.writers:
            os.close(writer)
        process.writers.clear()
        process.returncode = -signal.SIGTERM

    monkeypatch.setattr(process_timeout.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(sampler, "_process_identity", lambda _pid: sampler._ProcessIdentity(424242, 0, (1, 0)))
    monkeypatch.setattr(sampler, "_termination_process_states", lambda _timeout: {})
    monkeypatch.setattr(sampler, "_owned_process_pids", lambda *_args: set())
    monkeypatch.setattr(sampler, "_terminate_owned_processes", terminate)
    return process


def test_package_deadline_expiring_during_telemetry_is_timeout(tmp_path, monkeypatch):
    _fake_tree_process(monkeypatch)
    clock = [10.0]
    observations = []

    def observe(timeout):
        observations.append(timeout)
        if len(observations) == 1:
            clock[0] += timeout
            return None
        return {}

    monkeypatch.setattr(sampler, "_termination_process_states", observe)
    with monkeypatch.context() as timing:
        timing.setattr(time, "monotonic", lambda: clock[0])
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            process_timeout.run_process_tree_timeout(
                ["fake-owned-child"], evidence=tmp_path / "deadline", timeout=0.125,
            )
    assert observations[0] == 0.125
    assert raised.value.timeout == 0.125
    assert raised.value.output == raised.value.stderr == b""


@pytest.mark.parametrize("kind", ["pipe", "log"])
@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired(["fake-owned-child"], 3), KeyboardInterrupt(), SystemExit(143),
])
def test_package_teardown_errors_preserve_original_and_attempt_both_streams(
    tmp_path, monkeypatch, kind, error,
):
    process = _fake_tree_process(monkeypatch)
    closed = []
    before = _handlers()

    class BrokenClose:
        def __init__(self, wrapped, name):
            self.wrapped = wrapped
            self.name = name

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            self.wrapped.close()
            closed.append(self.name)
            raise OSError(f"{self.name} close failed")

    if kind == "pipe":
        for name in ("stdout", "stderr"):
            setattr(process, name, BrokenClose(getattr(process, name), name))
    else:
        original_open = Path.open

        def open_log(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return BrokenClose(stream, path.stem) if path.suffix == ".log" else stream

        monkeypatch.setattr(Path, "open", open_log)

    first = True

    def observe(*_args):
        nonlocal first
        if first:
            first = False
            raise error
        return set()

    monkeypatch.setattr(sampler, "_owned_process_pids", observe)
    evidence = tmp_path / "teardown"
    with pytest.raises(type(error)) as raised:
        process_timeout.run_process_tree_timeout(
            ["fake-owned-child"], evidence=evidence, timeout=3,
        )
    assert raised.value is error
    assert sorted(closed) == ["stderr", "stdout"]
    assert _handlers() == before
    receipt = json.loads((evidence / "result.json").read_text())
    assert receipt["cleanup_errors"]
    assert receipt["error"].startswith(type(error).__name__ + ":")
