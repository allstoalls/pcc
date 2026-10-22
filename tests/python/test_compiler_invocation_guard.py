import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("compiler_guard", ROOT / "scripts" / "compiler_invocation_guard.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _pidfds_available():
    if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    descriptor = None
    try:
        descriptor = os.pidfd_open(os.getpid(), 0)
        signal.pidfd_send_signal(descriptor, 0)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


pytestmark = pytest.mark.pcc_gate(probe=_pidfds_available)


def new_guard(tmp_path):
    helper = MODULE.load_module(ROOT / "scripts" / "run_process_tree_sample.py", "tree_helper")
    return MODULE.CompilerGuard(helper, [sys.executable], tmp_path / "evidence")


def is_live(pid):
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True, timeout=2,
    ).stdout.strip()
    return bool(state and not state.startswith("Z"))


def fixture_sources(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent("""
        import os, signal, sys, time
        from pathlib import Path
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(sys.argv[1]).write_text(str(os.getpid()))
        while True:
            time.sleep(0.02)
    """))
    compiler = tmp_path / "compiler.py"
    compiler.write_text(textwrap.dedent("""
        import json, os, subprocess, sys, time
        from pathlib import Path
        directory = Path(sys.argv[1])
        child = subprocess.Popen([sys.executable, str(directory / 'worker.py'),
                                  str(directory / 'worker.pid')], process_group=0)
        deadline = time.monotonic() + 3
        while not (directory / 'worker.pid').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (directory / 'worker.pid').exists()
        (directory / 'births.json').write_text(json.dumps([os.getpid(), child.pid]))
        if sys.argv[2] == 'exit':
            raise SystemExit(0)
        if sys.argv[2] == 'failure':
            raise SystemExit(7)
        while True:
            time.sleep(0.02)
    """))
    return compiler


@pytest.mark.parametrize("mode", ["exit", "failure", "timeout"])
def test_real_independent_worker_is_gone_before_next_identity(tmp_path, mode):
    guard = new_guard(tmp_path)
    compiler = fixture_sources(tmp_path)
    stranger = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True)
    stranger_identity = guard.helper._process_identity(stranger.pid)
    command = [sys.executable, str(compiler), str(tmp_path), mode]
    try:
        if mode == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                guard.run(command, compiler=Path(sys.executable).resolve(), timeout=0.5)
        else:
            result = guard.run(command, compiler=Path(sys.executable).resolve(), timeout=5)
            assert result.returncode == (7 if mode == "failure" else 0)
        worker_pid = int((tmp_path / "worker.pid").read_text())
        # This is the following identity operation, after run() returns/raises.
        assert not is_live(worker_pid)
        (tmp_path / "identity-complete").write_text("previous invocation has no live worker")
        assert stranger.poll() is None
        assert guard.helper._process_identity(stranger.pid).start_time == stranger_identity.start_time
        receipt = json.loads((tmp_path / "evidence/call-0001/receipt.json").read_text())
        assert receipt["cleanup_verified"]
        assert receipt["max_tree_rss_bytes"] == 2147483648
        assert str(stranger.pid) not in receipt["owned_processes"]
        assert receipt["command"] == command
        assert receipt["compiler_sha256"] == MODULE.digest(sys.executable)
        assert receipt["status"] == ("TIMEOUT" if mode == "timeout" else "COMPLETE")
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait(timeout=3)
        # Cleanup only verified births recorded by this guard, never bare PID guesses.
        receipt_path = tmp_path / "evidence/call-0001/receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            for pid, start in receipt.get("owned_processes", {}).items():
                identity = guard.helper._process_identity(int(pid))
                if identity is not None and identity.start_time == tuple(start):
                    guard.helper._signal_owned_pid(int(pid), identity, signal.SIGKILL)


def test_capture_environment_and_real_identity_are_preserved(tmp_path):
    guard = new_guard(tmp_path)
    environment = dict(os.environ, PCC_WORKER_TREE_STATE_PATH="/private/outer-state",
                       PCC_WORKER_TREE_BUDGET_BYTES="2147483648",
                       PCC_WORKER_TREE_RECOVERY="unchanged outer recovery")
    code = ("import json,os,sys;print(json.dumps({key:os.environ[key] for key in "
            + repr(MODULE.TRANSPORT_KEYS) + "}));sys.stderr.write('diagnostic\\r\\n')")
    result = guard.run([sys.executable, "-c", code], compiler=Path(sys.executable).resolve(),
                       timeout=300, env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {key: environment[key] for key in MODULE.TRANSPORT_KEYS}
    assert result.stderr == "diagnostic\n"
    receipt = json.loads((tmp_path / "evidence/call-0001/receipt.json").read_text())
    assert receipt["timeout_s"] == 300
    assert receipt["inherited_worker_transport"] == json.loads(result.stdout)


def test_noncompiler_commands_still_use_original_runner(tmp_path):
    guard = new_guard(tmp_path)
    calls = []
    original = SimpleNamespace(run_command=lambda *args, **kwargs: calls.append((args, kwargs)) or "original")
    guard.install(original)
    assert original.run_command(["unrelated-program"], timeout=60, capture_output=True) == "original"
    assert len(calls) == 1 and guard.calls == 0
    with pytest.raises(ValueError, match="300"):
        original.run_command([sys.executable, "-c", "pass"], timeout=301)
    with pytest.raises(ValueError, match="executable overrides"):
        original.run_command(["unlisted-argv-zero"], executable=sys.executable, timeout=300)
    assert len(calls) == 1
    assert guard.calls == 0


def test_helper_import_does_not_replace_benchmark_lock_owner(tmp_path, monkeypatch):
    original = SimpleNamespace(REPO_ROOT="real benchmark source")
    monkeypatch.setitem(sys.modules, "run_pcc_compile_ab", original)
    guard = new_guard(tmp_path)
    assert sys.modules["run_pcc_compile_ab"] is original
    assert guard.helper.compile_ab is not original
    assert guard.helper.compile_ab.__name__.startswith("_pcc_invocation_guard_helpers_")


def test_relative_path_search_uses_child_cwd(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/compiler").symlink_to(Path(sys.executable).resolve())
    assert MODULE.resolve_executable(["compiler", "--version"], {
        "cwd": str(tmp_path), "env": {"PATH": "bin"},
    }) == Path(sys.executable).resolve()


@pytest.mark.parametrize("environment", [{}, {"PCC_WORKER_TREE_BUDGET_BYTES": "4294967296"}])
def test_required_outer_guard_rejects_missing_or_expanded_cap(tmp_path, environment):
    guard = new_guard(tmp_path)
    guard.outer_guard_required = True
    with pytest.raises(RuntimeError, match="outer 2 GiB"):
        guard.run([sys.executable, "-c", "raise SystemExit(99)"],
                  compiler=Path(sys.executable).resolve(), timeout=300, env=environment)
    assert guard.calls == 0


@pytest.mark.parametrize("kind", ["valid", "header-only", "wrong-parent", "stale"])
def test_outer_snapshot_must_cover_this_live_wrapper(tmp_path, kind):
    snapshot = tmp_path / "outer.tsv"
    sampled_at = time.monotonic() - (3 if kind == "stale" else 0)
    lines = ["pcc.worker-tree-rss.v1", str(sampled_at), "2147483648"]
    if kind != "header-only":
        parent = os.getppid() + (1 if kind == "wrong-parent" else 0)
        lines.append(f"{os.getpid()}\t{parent}\t4096")
    snapshot.write_text("\n".join(lines) + "\n")
    environment = {
        "PCC_WORKER_TREE_STATE_PATH": str(snapshot),
        "PCC_WORKER_TREE_BUDGET_BYTES": "2147483648",
        "PCC_WORKER_TREE_RECOVERY": f"pcc.worker-tree-recovery.v1\t1.0,3.0\t4.0\t{time.monotonic() + 10}",
    }
    if kind == "valid":
        MODULE.require_outer_guard(environment)
    else:
        with pytest.raises(RuntimeError, match="snapshot"):
            MODULE.require_outer_guard(environment)


@pytest.mark.parametrize("policy", ["1.0,3.0\t4.0\tinf", "nan\t4.0\t1e10", "1,3\t8\t1e10"])
def test_outer_recovery_rejects_nonfinite_or_mismatched_bounds(tmp_path, policy):
    with pytest.raises(RuntimeError, match="deadline"):
        MODULE.require_outer_guard({
            "PCC_WORKER_TREE_STATE_PATH": str(tmp_path / "unused"),
            "PCC_WORKER_TREE_BUDGET_BYTES": "2147483648",
            "PCC_WORKER_TREE_RECOVERY": "pcc.worker-tree-recovery.v1\t" + policy,
        })


def test_reused_identity_is_never_signaled_by_cleanup(tmp_path, monkeypatch):
    guard = new_guard(tmp_path)
    original = guard.helper._ProcessIdentity(11, 1, (100, 0))
    reused = guard.helper._ProcessIdentity(11, 1, (200, 0))
    monkeypatch.setattr(guard.helper, "_process_identity", lambda pid: reused)
    descriptor = os.pidfd_open(os.getpid(), 0)
    monkeypatch.setattr(MODULE.os, "pidfd_open", lambda *_args: descriptor)
    monkeypatch.setattr(MODULE.signal, "pidfd_send_signal", lambda *_args: pytest.fail("reused process handle was signaled"))
    monkeypatch.setattr(MODULE.os, "kill", lambda *_args: pytest.fail("unowned PID was signaled"))
    assert not guard._signal(1000000000, original, signal.SIGKILL)


def test_final_fork_after_sample_keeps_root_anchor_until_cleanup(tmp_path, monkeypatch):
    guard = new_guard(tmp_path)
    fixture_sources(tmp_path)
    parent = tmp_path / "late-parent.py"
    parent.write_text(textwrap.dedent("""
        import os, subprocess, sys, time
        from pathlib import Path
        directory = Path(sys.argv[1])
        (directory / 'root.pid').write_text(str(os.getpid()))
        while not (directory / 'spawn-now').exists():
            time.sleep(0.005)
        subprocess.Popen([sys.executable, str(directory / 'worker.py'),
                          str(directory / 'worker.pid')], process_group=0)
    """))
    original = guard.helper._timed_process_table
    fired = []

    def final_fork(**kwargs):
        deadline = time.monotonic() + 3
        while not (tmp_path / "root.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        result = original(**kwargs)
        if not fired:
            fired.append(True)
            (tmp_path / "spawn-now").touch()
            pid = int((tmp_path / "root.pid").read_text())
            while time.monotonic() < deadline:
                exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and (tmp_path / "worker.pid").exists():
                    break
                time.sleep(0.005)
            assert exited is not None
        return result

    monkeypatch.setattr(guard.helper, "_timed_process_table", final_fork)
    result = guard.run([sys.executable, str(parent), str(tmp_path)],
                       compiler=Path(sys.executable).resolve(), timeout=5)
    assert result.returncode == 0
    assert not is_live(int((tmp_path / "worker.pid").read_text()))


def test_sigterm_during_cleanup_is_not_returned_as_success(tmp_path, monkeypatch):
    guard = new_guard(tmp_path)
    original = guard._cleanup_barrier

    def cleanup(process, observed):
        os.kill(os.getpid(), signal.SIGTERM)
        return original(process, observed)

    monkeypatch.setattr(guard, "_cleanup_barrier", cleanup)
    with pytest.raises(SystemExit) as error:
        guard.run([sys.executable, "-c", "pass"], compiler=Path(sys.executable).resolve(), timeout=5)
    assert error.value.code == 143
    receipt = json.loads((tmp_path / "evidence/call-0001/receipt.json").read_text())
    assert receipt["status"] == "INTERRUPTED" and receipt["cleanup_verified"]


def test_final_sampler_deadline_preserves_timeout_exception(tmp_path, monkeypatch):
    guard = new_guard(tmp_path)

    def exhausted(**kwargs):
        time.sleep(kwargs["timeouts_s"][0] + 0.01)
        raise guard.helper.ProcessTreeSampleError("last sample exhausted execution deadline")

    monkeypatch.setattr(guard.helper, "_timed_process_table", exhausted)
    with pytest.raises(subprocess.TimeoutExpired):
        guard.run([sys.executable, "-c", "import time;time.sleep(10)"],
                  compiler=Path(sys.executable).resolve(), timeout=0.1)
    receipt = json.loads((tmp_path / "evidence/call-0001/receipt.json").read_text())
    assert receipt["status"] == "TIMEOUT" and receipt["cleanup_verified"]
    assert "last sample" in receipt["deadline_sampler_error"]


def test_launcher_preserves_script_compiler_identity(tmp_path):
    fixture_sources(tmp_path)
    (tmp_path / "processes.py").write_text(
        "import subprocess\n"
        "def run_command(command, **kwargs):\n"
        "    return subprocess.run(command, **kwargs)\n")
    benchmark = tmp_path / "benchmark.py"
    benchmark.write_text(textwrap.dedent("""
        import hashlib, json, subprocess, sys
        from pathlib import Path
        from processes import run_command
        directory = Path(sys.argv[1])
        actual = sys.executable
        expected = hashlib.sha256(Path(actual).read_bytes()).hexdigest()
        result = run_command([actual, str(directory / 'compiler.py'), str(directory), 'exit'], timeout=300)
        assert result.returncode == 0
        worker_pid = int((directory / 'worker.pid').read_text())
        state = subprocess.run(['ps', '-p', str(worker_pid), '-o', 'stat='],
                               text=True, capture_output=True, timeout=2).stdout.strip()
        assert not state or state.startswith('Z'), state
        (directory / 'identity.json').write_text(json.dumps({'compiler': actual, 'sha256': expected}))
    """))
    command = [
        sys.executable, str(ROOT / "scripts" / "compiler_invocation_guard.py"),
        "--compiler", sys.executable, "--evidence", str(tmp_path / "launcher-evidence"),
        "--", str(benchmark), str(tmp_path),
    ]
    # The production CLI requires the unchanged outer total-tree guard.
    run = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "run_process_tree_sample.py"),
        "--result", str(tmp_path / "outer.json"),
        "--samples", str(tmp_path / "outer.tsv"),
        "--stdout", str(tmp_path / "outer.stdout"),
        "--stderr", str(tmp_path / "outer.stderr"),
        "--cwd", str(ROOT), "--timeout", "10", "--interval", "0.05",
        "--max-tree-rss-bytes", "2147483648", "--no-performance-lock", "--", *command,
    ], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
    identity = json.loads((tmp_path / "identity.json").read_text())
    assert identity == {"compiler": sys.executable, "sha256": MODULE.digest(sys.executable)}
    launcher = json.loads((tmp_path / "launcher-evidence/launcher.json").read_text())
    assert launcher["guarded_invocations"] == 1
    outer = json.loads((tmp_path / "outer.json").read_text())
    assert outer["status"] == "COMPLETE" and outer["returncode"] == 0
    receipt = json.loads((tmp_path / "launcher-evidence/call-0001/receipt.json").read_text())
    assert receipt["inherited_worker_transport"]["PCC_WORKER_TREE_STATE_PATH"] == str(tmp_path / "outer.json.worker-rss.tsv")
