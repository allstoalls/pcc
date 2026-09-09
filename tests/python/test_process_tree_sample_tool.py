from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
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


def test_parent_exit_cleans_an_orphaned_child_process_group(tmp_path: Path):
    result = tmp_path / "result.json"
    stdout = tmp_path / "target.stdout"
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "process_group=0,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(p.pid,flush=True)"
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


def test_safety_process_table_failure_does_not_take_slow_retry(monkeypatch):
    tool = _load_tool_module()
    observed = []

    def always_timeout(command, *args, **kwargs):
        observed.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(tool.subprocess, "run", always_timeout)

    with pytest.raises(tool.ProcessTreeSampleError) as raised:
        tool._process_table(timeouts_s=tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)

    assert observed == list(tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)
    assert raised.value.retry_count == len(tool._SAFETY_PROCESS_TABLE_TIMEOUTS_S)


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
    target_pid = int(stdout.read_text(encoding="utf-8").strip())
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
