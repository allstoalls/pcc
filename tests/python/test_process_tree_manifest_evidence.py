"""Bounded watchdog evidence; no compiler build or generated program required."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/run_process_tree_sample.py"
FRONTEND = "--pcc-python-multi-codegen-worker"
BACKEND = "--pcc-self-backend-emit-batch-worker"
FRONTEND_TEXT = (
    "pcc.frontends.python.codegen_worker.v4\n"
    "/tmp/result.tsv\n/tmp/ir\n/tmp/exports\ncodegen\n/tmp/ast\n"
    "main\noff\non\n0\n0\n1\n0\tos.walk\t/source/os.py\n1\n0\n"
)
BACKEND_TEXT = (
    "pcc.self_backend.emit_batch.v1\n"
    "/tmp/os_walk.ll\n/tmp/result.tsv\n/tmp/os_walk.o\npcc\n"
)


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("manifest_evidence_guard", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOL.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(TOOL.parent))
    return module


def _manifest(tmp_path, *, frontend=True, index=0, text=None):
    directory = tmp_path / ("pcc_frontends_python_workers_fixture" if frontend else "pcc_py_self_fixture")
    directory.mkdir(mode=0o700, exist_ok=True)
    name = f"worker_{index}.manifest" if frontend else f"self_backend_emit_oversized_{index}.manifest"
    path = directory / name
    path.write_text(text if text is not None else FRONTEND_TEXT if frontend else BACKEND_TEXT)
    return path


def _fake_processes(tool, monkeypatch, tmp_path, paths, *, flag=FRONTEND, argv=None):
    identity = tool._ProcessIdentity(10, 1, (100, 0))
    observed = {pid: identity for pid in range(10, 10 + len(paths))}
    processes = [{"pid": pid, "manifest_paths": [str(path)]}
                 for pid, path in zip(observed, paths)]
    commands = {
        f"/proc/{pid}/cmdline": argv if argv is not None else
        b"/compiler/pcc1\0" + os.fsencode(flag) + b"\0" + os.fsencode(path) + b"\0"
        for pid, path in zip(observed, paths)
    }
    original_open = builtins.open

    def open_command(path, *args, **kwargs):
        if str(path) in commands:
            return io.BytesIO(commands[str(path)])
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(tool, "open", open_command, raising=False)
    monkeypatch.setattr(tool.sys, "platform", "linux")
    monkeypatch.setattr(tool, "_process_identity", observed.get)
    monkeypatch.setattr(tool.tempfile, "gettempdir", lambda: str(tmp_path))
    return processes, observed


@pytest.mark.parametrize("frontend", [False, True])
def test_retains_only_versioned_manifest_text_and_identity(tool, monkeypatch, tmp_path, frontend):
    path = _manifest(tmp_path, frontend=frontend)
    flag = FRONTEND if frontend else BACKEND
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, [path], flag=flag)
    evidence = tool._capture_worker_manifests(processes, observed)
    row, = evidence["files"]
    assert row["status"] == "captured"
    assert row["worker_flag"] == flag
    assert row["process_start_time"] == [100, 0]
    assert row["text"] == path.read_text()
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["source_bytes"] == row["retained_bytes"] == evidence["read_bytes"]
    assert row["truncated"] is False
    assert evidence["complete_inventory"] is False
    assert evidence["scope"] == "observed_workers_before_guard_cleanup"


@pytest.mark.parametrize("version", range(1, 6))
def test_all_current_frontend_protocol_headers_are_retained(tool, tmp_path, version):
    text = FRONTEND_TEXT.replace(".v4\n", f".v{version}\n")
    path = _manifest(tmp_path, text=text)
    row = tool._read_worker_manifest(str(path), FRONTEND, tmp_path, 4096)
    assert row["status"] == "captured" and row["text"] == text


@pytest.mark.parametrize("kind", [
    "outside", "relative", "traversal", "directory_symlink", "file_symlink",
    "ancestor_symlink", "hardlink", "fifo", "directory", "public_directory", "header",
])
def test_rejects_unsafe_paths_and_files(tool, tmp_path, kind):
    path = _manifest(tmp_path)
    secret = tmp_path / "private.txt"
    secret.write_text("SECRET MUST NEVER BE RETAINED")
    root = tmp_path
    if kind == "outside":
        path = secret
    elif kind == "relative":
        path = path.relative_to(tmp_path)
    elif kind == "traversal":
        path = path.parent / ".." / path.parent.name / path.name
    elif kind == "directory_symlink":
        real = tmp_path / "elsewhere"
        path.parent.rename(real)
        path.parent.symlink_to(real, target_is_directory=True)
    elif kind == "file_symlink":
        path.unlink()
        path.symlink_to(secret)
    elif kind == "ancestor_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias
        path = alias / path.relative_to(tmp_path)
    elif kind == "hardlink":
        path.unlink()
        os.link(secret, path)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "public_directory":
        path.parent.chmod(0o755)
    elif kind == "header":
        path.write_text(secret.read_text())
    row = tool._read_worker_manifest(str(path), FRONTEND, root, 4096)
    assert row["status"] != "captured", row
    assert "text" not in row and "SECRET" not in json.dumps(row)


@pytest.mark.parametrize("wrong_owner", ["directory", "file"])
def test_rejects_other_owners(tool, monkeypatch, tmp_path, wrong_owner):
    path = _manifest(tmp_path)
    original = tool.os.fstat

    def fstat(fd):
        info = original(fd)
        wanted = path.parent.stat() if wrong_owner == "directory" else path.stat()
        if info.st_ino == wanted.st_ino:
            return SimpleNamespace(st_uid=info.st_uid + 1, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(tool.os, "fstat", fstat)
    assert tool._read_worker_manifest(str(path), FRONTEND, tmp_path, 4096)["status"] == "rejected_" + wrong_owner


def test_missing_and_changing_files_do_not_escape(tool, monkeypatch, tmp_path):
    path = _manifest(tmp_path)
    original = tool.os.read

    def read(fd, limit):
        data = original(fd, limit)
        path.write_text(FRONTEND_TEXT + "changed\n")
        return data

    monkeypatch.setattr(tool.os, "read", read)
    row = tool._read_worker_manifest(str(path), FRONTEND, tmp_path, 4096)
    assert row["status"] == "changed_during_read" and "text" not in row
    path.unlink()
    assert tool._read_worker_manifest(str(path), FRONTEND, tmp_path, 4096)["status"] == "unavailable"


@pytest.mark.parametrize("argv", [
    b"pcc1\0--unrelated\0file.manifest\0",
    b"pcc1 --pcc-python-multi-codegen-worker /tmp/private.txt\0",
    b"pcc1\0--pcc-python-multi-codegen-worker\0/tmp/private.txt",
    b"x" * (64 * 1024 + 1) + b"\0",
], ids=["flag_removed", "quoted_process_name", "incomplete_argv", "oversized_argv"])
def test_stale_or_ambiguous_commands_do_not_authorize_reads(tool, monkeypatch, tmp_path, argv):
    path = _manifest(tmp_path)
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, [path], argv=argv)
    monkeypatch.setattr(tool, "_read_worker_manifest", lambda *_: pytest.fail("untrusted argv"))
    row, = tool._capture_worker_manifests(processes, observed)["files"]
    assert row["status"] in ("not_manifest_worker", "unavailable_argv")


@pytest.mark.parametrize("change_at", [0, 1, 2])
def test_process_identity_revalidated_around_argv_and_contents(tool, monkeypatch, tmp_path, change_at):
    path = _manifest(tmp_path)
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, [path])
    values = iter([observed[10]] * change_at + [None])
    monkeypatch.setattr(tool, "_process_identity", lambda _: next(values))
    row, = tool._capture_worker_manifests(processes, observed)["files"]
    assert row["status"] != "captured" and "text" not in row


def test_unknown_process_is_never_opened(tool, monkeypatch, tmp_path):
    path = _manifest(tmp_path)
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, [path])
    monkeypatch.setattr(tool, "open", lambda *_: pytest.fail("unowned process"))
    assert tool._capture_worker_manifests(processes, {})["files"][0]["status"] == "process_unavailable"


def test_file_total_byte_and_count_caps_are_enforced(tool, monkeypatch, tmp_path):
    paths = [_manifest(tmp_path, index=index, text=FRONTEND_TEXT + "a" * (300 * 1024)) for index in range(20)]
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, paths)
    evidence = tool._capture_worker_manifests(processes, observed)
    assert len(evidence["files"]) == 16 and evidence["file_limit_reached"]
    captured = [row for row in evidence["files"] if row["status"] == "captured"]
    assert len(captured) == 4
    assert all(row["retained_bytes"] == 256 * 1024 and row["truncated"] for row in captured)
    assert evidence["read_bytes"] == evidence["retained_bytes"] == 1024 * 1024
    assert all(row["status"] == "total_byte_limit" for row in evidence["files"][4:])


def test_duplicate_paths_do_not_repeat_contents(tool, monkeypatch, tmp_path):
    path = _manifest(tmp_path)
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, [path, path])
    evidence = tool._capture_worker_manifests(processes, observed)
    assert evidence["files"][1]["status"] == "duplicate"
    assert evidence["files"][1]["duplicate_of"] == 0
    assert evidence["read_bytes"] == len(path.read_bytes())


def test_unknown_header_reads_still_consume_total_budget(tool, monkeypatch, tmp_path):
    paths = [_manifest(tmp_path, index=i, text="secret" * 50000) for i in range(5)]
    processes, observed = _fake_processes(tool, monkeypatch, tmp_path, paths)
    evidence = tool._capture_worker_manifests(processes, observed)
    assert evidence["read_bytes"] == 1024 * 1024 and evidence["retained_bytes"] == 0
    assert evidence["files"][-1]["status"] == "total_byte_limit"
    assert "secret" not in json.dumps(evidence)


def test_utf8_boundary_is_explicit_and_digest_matches_retained_text(tool, tmp_path):
    path = _manifest(tmp_path, text=FRONTEND_TEXT + "éé")
    limit = len(FRONTEND_TEXT.encode()) + 1
    row = tool._read_worker_manifest(str(path), FRONTEND, tmp_path, limit)
    assert row["text"] == FRONTEND_TEXT and row["truncated"]
    assert row["sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
    assert row["read_bytes"] == limit


def test_invalid_utf8_at_truncation_boundary_is_rejected(tool, tmp_path):
    path = _manifest(tmp_path)
    path.write_bytes(FRONTEND_TEXT.encode() + b"\xffmore")
    row = tool._read_worker_manifest(str(path), FRONTEND, tmp_path, len(FRONTEND_TEXT.encode()) + 1)
    assert row["status"] == "rejected_encoding" and "text" not in row


def test_darwin_is_explicitly_unsupported_without_argv_probe(tool, monkeypatch):
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    monkeypatch.setattr(tool, "open", lambda *_: pytest.fail("unsupported argv"), raising=False)
    assert tool._capture_worker_manifests([], {})["status"] == "unsupported_platform"


def test_candidates_are_only_known_manifest_workers(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "open", lambda *_: pytest.fail("unobserved argv"), raising=False)
    evidence = tool._capture_worker_manifests([{"pid": 10, "manifest_paths": []}], {})
    assert evidence["candidate_count"] == 0 and evidence["files"] == []


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux"))
@pytest.mark.parametrize("frontend", [False, True])
def test_timeout_retains_manifest_before_worker_removes_workspace(tmp_path, frontend):
    if not sys.platform.startswith("linux"):
        pytest.fail("structured worker argv capture requires Linux")
    private = tmp_path / "temporary"
    private.mkdir()
    path = _manifest(private, frontend=frontend)
    original = path.read_text()
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, shutil, signal, sys, time\n"
        "from pathlib import Path\n"
        "manifest = Path(sys.argv[-1])\n"
        "def stop(*args):\n"
        "    shutil.rmtree(manifest.parent)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "print(os.getpid(), flush=True)\n"
        "while True: time.sleep(0.01)\n"
    )
    result = tmp_path / "result.json"
    command = [
        sys.executable, str(TOOL), "--result", str(result),
        "--samples", str(tmp_path / "samples.tsv"), "--stdout", str(tmp_path / "target.stdout"),
        "--stderr", str(tmp_path / "target.stderr"), "--cwd", str(ROOT),
        "--timeout", "0.6", "--interval", "0.02", "--max-tree-rss-bytes", str(128 * 1024 * 1024),
        "--no-performance-lock", "--", sys.executable, str(worker),
        FRONTEND if frontend else BACKEND, str(path),
    ]
    run = subprocess.run(command, env=dict(os.environ, TMPDIR=str(private)),
                         capture_output=True, text=True, timeout=8)
    assert run.returncode == 124, run.stdout + run.stderr
    receipt = json.loads(result.read_text())
    assert receipt["status"] == "TIMEOUT" and receipt["returncode"] == 0
    assert receipt["timeout_s"] == 0.6 and receipt["max_tree_rss_bytes"] == 128 * 1024 * 1024
    evidence = receipt["worker_manifest_evidence"]
    row, = [row for row in evidence["files"] if row["status"] == "captured"]
    assert row["text"] == original and row["source_path"] == str(path)
    assert row["pid"] == int((tmp_path / "target.stdout").read_text())
    assert not path.parent.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(row["pid"], 0)


def test_capture_exception_does_not_prevent_timeout_cleanup(tool, monkeypatch, tmp_path):
    def fail(*_, **_kwargs):
        raise OSError("injected evidence failure")

    monkeypatch.setattr(tool, "_capture_worker_manifests", fail)
    args = tool._parser().parse_args([
        "--result", str(tmp_path / "result.json"), "--samples", str(tmp_path / "samples.tsv"),
        "--stdout", str(tmp_path / "target.stdout"), "--stderr", str(tmp_path / "target.stderr"),
        "--timeout", "0.2", "--interval", "0.02", "--no-performance-lock", "--",
        sys.executable, "-c", "import os,time;print(os.getpid(),flush=True);time.sleep(30)",
    ])
    receipt = tool.run(args)
    assert receipt["status"] == "TIMEOUT" and receipt["returncode"] == -signal.SIGTERM
    assert receipt["worker_manifest_evidence"] == {"status": "capture_error", "error": "OSError"}
    pid = int((tmp_path / "target.stdout").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_ci_uploads_the_receipt_without_new_artifact_globs():
    workflow = (ROOT / ".github/workflows/pcc1-package-parity.yml").read_text()
    assert "build/worker-prior-gate/**/*.json" in workflow
    assert "build/worker-prior-preflight/**/*.json" in workflow


def _terminal_argv_fixture(tool, monkeypatch, children, *, clock=None):
    """Only synthetic identities and BytesIO stand in for process inspection."""
    parent = tool._ProcessIdentity(10, 1, (100, 0))
    observed = {10: parent}
    rows = [{"pid": 10, "ppid": 1, "rss_bytes": 1000,
             "command": "synthetic-parent", "manifest_paths": []}]
    commands, reads = {}, []
    for pid, raw in children.items():
        observed[pid] = tool._ProcessIdentity(10, 10, (pid * 10, 0))
        rows.append({"pid": pid, "ppid": 10, "rss_bytes": 100,
                     "command": "", "manifest_paths": []})
        commands[f"/proc/{pid}/cmdline"] = raw
    if clock is None:
        clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(tool, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(tool.sys, "platform", "linux")
    monkeypatch.setattr(tool, "_process_identity", observed.get)

    class ArgvStream(io.BytesIO):
        def read(self, limit):
            reads.append(limit)
            assert limit == 64 * 1024 + 1
            return super().read(limit)

    def open_argv(path, mode):
        assert mode == "rb" and str(path) in commands, "unexpected process or file read"
        raw = commands[str(path)]
        if isinstance(raw, Exception):
            raise raw
        return ArgvStream(raw)

    monkeypatch.setattr(tool, "open", open_argv, raising=False)
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: pytest.fail("diagnostic subprocess"))
    monkeypatch.setattr(tool.subprocess, "Popen", lambda *_a, **_k: pytest.fail("target subprocess"))
    return rows, observed, commands, reads, clock


def test_terminal_argv_enriches_nonlargest_child_before_manifest_capture(tool, monkeypatch, tmp_path):
    path = _manifest(tmp_path, frontend=False)
    raw = (b"/synthetic-driver\0" + ("é" * 5000).encode() + b"\0"
           + os.fsencode(BACKEND) + b"\0" + os.fsencode(path) + b"\0")
    rows, observed, _commands, reads, _clock = _terminal_argv_fixture(tool, monkeypatch, {20: raw})
    monkeypatch.setattr(tool.tempfile, "gettempdir", lambda: str(tmp_path))
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert reads == [65537] and evidence["attempt_count"] == 1
    assert evidence["command_byte_scope"] == "newly_captured_command_text_only"
    assert rows[1]["command_truncated"] and len(rows[1]["command"].encode()) <= 4096
    assert BACKEND not in rows[1]["command"]
    assert rows[1]["manifest_paths"] == [str(path)]
    captured, = tool._capture_worker_manifests(rows, observed, verified_argv=verified)["files"]
    assert captured["status"] == "captured" and captured["text"] == BACKEND_TEXT
    assert captured["worker_flag"] == BACKEND and reads == [65537]


def test_terminal_argv_bounds_all_cleanup_attempts_reads_and_new_command_text(tool, monkeypatch):
    raw = b"synthetic\0" + b"a" * 40000 + b"\0"
    rows, observed, _commands, reads, _clock = _terminal_argv_fixture(
        tool, monkeypatch, {pid: raw for pid in range(20, 40)},
    )
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert len(reads) == len(verified) == evidence["attempt_count"] == 16
    assert evidence["unattempted_count"] == 4 and evidence["attempt_limit_reached"]
    assert evidence["read_bytes"] == 16 * len(raw) <= 16 * 65537
    assert evidence["retained_command_bytes"] == 65536
    assert all(len(row["command"].encode()) <= 4096 for row in rows[1:])
    assert sum(bool(row["command"]) for row in rows[1:]) == 16
    # Supplying this pass's results must never start fallback argv reads.
    rows[-1]["manifest_paths"] = ["/tmp/not-read.manifest"]
    tool._capture_worker_manifests(rows, observed, verified_argv=verified)
    assert len(reads) == 16


def test_terminal_argv_admission_budget_stops_new_reads_after_slow_read(tool, monkeypatch):
    rows, observed, commands, reads, clock = _terminal_argv_fixture(
        tool, monkeypatch, {pid: b"synthetic\0" for pid in range(20, 40)},
    )

    class SlowRead(io.BytesIO):
        def read(self, limit):
            reads.append(limit)
            clock.now += 0.3  # A kernel read is not claimed to be interruptible.
            return super().read(limit)

    monkeypatch.setattr(tool, "open", lambda path, _mode: SlowRead(commands[str(path)]))
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert list(verified) == [20] and reads == [65537]
    assert evidence["admission_budget_exhausted"] and evidence["unattempted_count"] == 19
    assert not evidence["attempt_limit_reached"]
    assert evidence["deadline_semantics"] == "soft_read_admission_not_syscall_interruption"


@pytest.mark.parametrize("change", ["child_before", "parent_before", "child_during", "parent_during", "parent_row"])
def test_terminal_argv_rejects_changed_child_and_parent_births(tool, monkeypatch, change):
    rows, observed, commands, reads, _clock = _terminal_argv_fixture(tool, monkeypatch, {20: b"child\0"})
    live = dict(observed)
    target = 20 if change.startswith("child") else 10
    stale = tool._ProcessIdentity(10, 10 if target == 20 else 1, (999, 0))
    if change.endswith("before"):
        live[target] = stale
    elif change == "parent_row":
        rows[1]["ppid"] = 99

    class ChangedRead(io.BytesIO):
        def read(self, limit):
            reads.append(limit)
            if change.endswith("during"):
                live[target] = stale
            return super().read(limit)

    monkeypatch.setattr(tool, "_process_identity", live.get)
    monkeypatch.setattr(tool, "open", lambda path, _mode: ChangedRead(commands[str(path)]))
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert not verified and rows[1]["command"] == ""
    assert len(reads) == int(change.endswith("during"))
    assert not evidence["retained_command_bytes"]


@pytest.mark.parametrize("raw", [b"incomplete", b"x" * 65538, OSError("unavailable")],
                         ids=["incomplete", "oversized", "unavailable"])
def test_terminal_argv_unavailable_inputs_remain_partial(tool, monkeypatch, raw):
    rows, observed, _commands, reads, _clock = _terminal_argv_fixture(tool, monkeypatch, {20: raw})
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert not verified and rows[1]["command"] == "" and not evidence["complete_inventory"]
    assert evidence["entries"][0]["status"] == "unavailable_argv"
    assert not reads or reads == [65537]


def test_terminal_argv_ordinary_manifest_argument_does_not_authorize_file_read(tool, monkeypatch):
    rows, observed, _commands, reads, _clock = _terminal_argv_fixture(
        tool, monkeypatch, {20: b"synthetic\0--unrelated\0/tmp/private.manifest\0"},
    )
    _evidence, verified = tool._capture_terminal_argv(rows, observed)
    monkeypatch.setattr(tool, "_read_worker_manifest", lambda *_: pytest.fail("ordinary argv is not a protocol"))
    assert rows[1]["manifest_paths"] == []
    assert tool._capture_worker_manifests(rows, observed, verified_argv=verified)["files"] == []
    assert reads == [65537]


def test_terminal_argv_preserves_known_manifest_root_scope_without_duplicate_read(tool, monkeypatch, tmp_path):
    path = _manifest(tmp_path)
    rows, observed = _fake_processes(tool, monkeypatch, tmp_path, [path])
    rows[0].update(ppid=1, command="existing command", rss_bytes=1000)
    evidence, verified = tool._capture_terminal_argv(rows, observed)
    assert evidence["attempt_count"] == 1 and evidence["retained_command_bytes"] == 0
    monkeypatch.setattr(tool, "open", lambda *_: pytest.fail("duplicate argv lookup"))
    captured, = tool._capture_worker_manifests(rows, observed, verified_argv=verified)["files"]
    assert captured["status"] == "captured" and rows[0]["command"] == "existing command"
    # A known manifest worker can outlive an observed parent. Its original
    # child-only scope must not acquire the new unknown-child parent rule.
    observed[1] = tool._ProcessIdentity(1, 0, (1, 0))
    processes, original = _fake_processes(tool, monkeypatch, tmp_path, [path])
    original.update(observed)
    monkeypatch.setattr(tool, "_process_identity", lambda pid: original.get(pid) if pid != 1 else None)
    _evidence, verified = tool._capture_terminal_argv(processes, original)
    assert verified[10].parent_identity is None
    captured, = tool._capture_worker_manifests(processes, original, verified_argv=verified)["files"]
    assert captured["status"] == "captured"


def test_terminal_argv_nonlinux_uses_no_lookup(tool, monkeypatch):
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    monkeypatch.setattr(tool, "_process_identity", lambda *_: pytest.fail("unsupported identity lookup"))
    monkeypatch.setattr(tool, "open", lambda *_: pytest.fail("unsupported argv lookup"), raising=False)
    evidence, verified = tool._capture_terminal_argv([], {})
    assert evidence["status"] == "unsupported_platform" and not verified


@pytest.mark.parametrize("outcome", ["TIMEOUT", "MEMORY_LIMIT", "INTERRUPTED", "SAMPLER_ERROR", "COMPLETE"])
@pytest.mark.parametrize("capture_error", [False, True])
def test_terminal_argv_cleanup_preserves_guard_status_with_mock_processes(
    tool, monkeypatch, tmp_path, outcome, capture_error,
):
    """Drive the real guard control flow, with no OS process or proc lookup."""
    clock = SimpleNamespace(now=100.0)
    clock.monotonic = lambda: clock.now
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    monkeypatch.setattr(tool, "time", clock)
    monkeypatch.setattr(tool.sys, "platform", "linux")
    identities = {10: tool._ProcessIdentity(10, 1, (100, 0)),
                  20: tool._ProcessIdentity(10, 10, (200, 0))}
    monkeypatch.setattr(tool, "_process_identity", identities.get)
    monkeypatch.setattr(tool, "_process_command", lambda pid: "synthetic-parent")
    samples, cleanup, captures = [], [], []
    child = SimpleNamespace(pid=10, returncode=None, poll=lambda: None)

    def launch(*_args, **_kwargs):
        return child

    def sample(**kwargs):
        assert kwargs["include_command"] is False
        samples.append(True)
        if outcome == "SAMPLER_ERROR":
            raise tool.ProcessTreeSampleError("injected sampling failure")
        if outcome == "INTERRUPTED":
            tool._INTERRUPT_REQUESTED = True
        clock.now += 0.2
        if outcome == "COMPLETE":
            child.returncode = 0
            child.poll = lambda: 0
        return {10: (1, 1000, "synthetic-parent"), 20: (10, 100, "")}, 0, clock.now

    def capture(rows, observed):
        captures.append([row["pid"] for row in rows])
        if capture_error:
            raise OSError("injected argv failure")
        return {"status": "collected", "scope": "mock-only"}, {}

    def terminate(process, observed):
        cleanup.append((process.pid, sorted(observed)))
        process.returncode = 0 if outcome == "COMPLETE" else -signal.SIGTERM

    def state_file_open(path, mode, **kwargs):
        assert Path(path) == tmp_path / "result.json.worker-rss.tsv.tmp" and mode == "w", "unexpected proc or file read"
        return builtins.open(path, mode, **kwargs)

    monkeypatch.setattr(tool.subprocess, "Popen", launch)
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: pytest.fail("unexpected subprocess"))
    monkeypatch.setattr(tool, "open", state_file_open, raising=False)
    monkeypatch.setattr(tool, "_timed_process_table", sample)
    monkeypatch.setattr(tool, "_process_table", lambda **_kwargs: ({10: (1, 1000, ""), 20: (10, 100, "")}, 0))
    monkeypatch.setattr(tool, "_capture_terminal_argv", capture)
    monkeypatch.setattr(tool, "_capture_worker_manifests", lambda *_a, **_k: {"status": "collected"})
    monkeypatch.setattr(tool, "_terminate_owned_processes", terminate)
    result = tmp_path / "result.json"
    cap = 100 if outcome == "MEMORY_LIMIT" else 100000
    args = tool._parser().parse_args([
        "--result", str(result), "--samples", str(tmp_path / "samples.tsv"),
        "--stdout", str(tmp_path / "stdout"), "--stderr", str(tmp_path / "stderr"),
        "--cwd", str(tmp_path), "--timeout", "0.1", "--interval", "0.02",
        "--max-tree-rss-bytes", str(cap), "--no-performance-lock", "--", "synthetic-driver",
    ])
    if outcome == "SAMPLER_ERROR":
        with pytest.raises(tool.ProcessTreeSampleError, match="injected sampling failure"):
            tool.run(args)
        receipt = json.loads(result.read_text())
    else:
        receipt = tool.run(args)
    assert receipt["status"] == outcome and receipt["max_tree_rss_bytes"] == cap
    assert receipt["timeout_s"] == 0.1 and receipt["interval_s"] == 0.02
    assert len(cleanup) == len(captures) == len(samples) == 1
    assert receipt["returncode"] == (0 if outcome == "COMPLETE" else -signal.SIGTERM)
    assert receipt["terminal_argv_evidence"]["status"] == ("capture_error" if capture_error else "collected")
