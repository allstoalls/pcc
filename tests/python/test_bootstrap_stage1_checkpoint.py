"""Bounded host tests: no native compiler, runtime build or performance lane."""

from __future__ import annotations

import copy
import importlib._bootstrap_external
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import bootstrap
from scripts import bootstrap_stage1_checkpoint as checkpoint


def _options(tmp_path, **env):
    options = bootstrap.Options({"PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out"),
                                 "PCC_RUNTIME_ARCHIVE": str(tmp_path / "runtime.a"), **env})
    options.stage1_checkpoint = tmp_path / "checkpoint"
    return options


@pytest.fixture
def supported_host(monkeypatch):
    """The host check comes first and is x86_64-Linux-only; the route rules
    after it are pure, so present the supported host to reach them anywhere."""
    monkeypatch.setattr(checkpoint, "sys", SimpleNamespace(
        implementation=SimpleNamespace(name="cpython"), platform="linux",
    ))
    monkeypatch.setattr(checkpoint, "platform", SimpleNamespace(machine=lambda: "x86_64"))


def _payload():
    return {"stage": 1, "owner": "cpython", "producer_role": "host-pcc0",
            "target": checkpoint.TARGET, "backend": "self", "command": ["python", "-m", "pcc"],
            "runtime": {"archive": {"sha256": "a" * 64}}, "compiler_sources": []}


def _terminal_guard(path, status="TIMEOUT", elapsed=2.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "pcc.process_tree_sample.v1", "status": status,
                               "elapsed_s": elapsed, "returncode": 124 if status == "TIMEOUT" else 0}))


def _started(root, attempt_id, utc="2026-10-06T08:00:00+00:00"):
    path = root / "attempts" / (attempt_id + ".started.json")
    guard = root / "attempts" / attempt_id / "guard/result.json"
    checkpoint.write_immutable_json(path, {"attempt_id": attempt_id, "started_at_utc": utc,
                                        "guard_result": str(guard)})
    return guard


def test_cli_checkpoint_is_default_off_and_explicit(tmp_path):
    assert bootstrap.parse_args([], bootstrap.Options({})).stage1_checkpoint is None
    options = bootstrap.parse_args(["--stage", "1", "--stage1-checkpoint", str(tmp_path)], bootstrap.Options({}))
    assert options.stage1_checkpoint == tmp_path


@pytest.mark.parametrize("args", [["--reuse-stage1"], ["--from-stage", "2"], ["--clean"]])
def test_cli_rejects_ambiguous_checkpoint_routes(tmp_path, args):
    with pytest.raises(bootstrap.BootstrapError, match="cannot combine"):
        bootstrap.parse_args(["--stage1-checkpoint", str(tmp_path), *args], bootstrap.Options({}))


@pytest.mark.parametrize("setting,value,diagnostic", [
    ("PCC_RUNTIME_ARCHIVE", "", "explicit validated"),
    ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0", "requires PCC_DIRECT"),
    ("PCC_DIRECT_INDEXED_NATIVE_OBJECT", "0", "native-object"),
    ("PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD", "1", "normal lock/watchdog"),
    ("PCC_BOOTSTRAP_STAGE_TIMEOUT", "2401", "safe resource"),
    ("PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS", "1", "safe resource"),
    ("PCC_BOOTSTRAP_PYTHON_LIBPYTHON", "on", "libpython off"),
    ("PCC_RUNTIME_BUILD", "make", "owned runtime"),
    ("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", "1", "memory-expanding"),
    ("PCC_TEXT_INDEXED_KERNEL_EMIT", "true", "memory-expanding"),
    ("PCC_DIRECT_INDEXED_SIDECAR", "on", "memory-expanding"),
])
def test_checkpoint_route_restrictions(tmp_path, supported_host, setting, value, diagnostic):
    options = _options(tmp_path, **{setting: value})
    with pytest.raises(checkpoint.CheckpointError, match=diagnostic):
        checkpoint.validate_route(options, [sys.executable, "-m", "pcc"], bootstrap.stage_environment(1, options))


def test_checkpoint_requires_frontend_release(tmp_path, supported_host):
    options = _options(tmp_path)
    environment = bootstrap.stage_environment(1, options)
    assert environment["PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND"] == "1"
    environment["PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND"] = "0"
    with pytest.raises(checkpoint.CheckpointError, match="RELEASE_FRONTEND"):
        checkpoint.validate_route(options, [sys.executable, "-m", "pcc"], environment)


@pytest.mark.parametrize("value", ["random", "-1", "4294967296", "not-an-integer", "\u0661"])
def test_checkpoint_rejects_unknown_or_invalid_hash_seed(value):
    with pytest.raises(checkpoint.CheckpointError, match="deterministic PYTHONHASHSEED"):
        checkpoint.resolve_hash_seed({"PYTHONHASHSEED": value})


@pytest.mark.parametrize("environment,expected", [({}, "0"), ({"PYTHONHASHSEED": ""}, "0"),
                                                  ({"PYTHONHASHSEED": "42"}, "42")])
def test_checkpoint_hash_seed_is_resolved_without_affecting_default_route(tmp_path, environment, expected):
    assert checkpoint.resolve_hash_seed(environment) == expected
    default = bootstrap.stage_environment(1, bootstrap.Options(environment))
    assert default.get("PYTHONHASHSEED") == environment.get("PYTHONHASHSEED")


def test_stage_and_native_smoke_isolate_checkpoint_environment(tmp_path, monkeypatch):
    controls = {"PCC_STAGE1_CHECKPOINT_" + name: value for name, value in {
        "DIR": str(tmp_path), "BUILD": "a" * 64, "STAGE": "1", "ATTEMPT": "first"}.items()}
    options = _options(tmp_path, **controls, PCC_BOOTSTRAP_STAGE_EXEC_DELAY="0")
    options.checkpoint_attempt = object()
    assert all(bootstrap.stage_environment(1, options)[key] == value for key, value in controls.items())
    for stage in (2, 3):
        assert not any(key.startswith(checkpoint.PREFIX) for key in bootstrap.stage_environment(stage, options))
    options.checkpoint_attempt = None
    assert not any(key.startswith(checkpoint.PREFIX) for key in bootstrap.stage_environment(1, options))
    options.out_dir.mkdir()
    environments = []

    def run(command, **kwargs):
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    assert bootstrap.stage_exec_barrier(tmp_path / "pcc1", 1, options) == 0
    assert len(environments) == 3
    assert all(not any(key.startswith(checkpoint.PREFIX) for key in env) for env in environments)


@pytest.mark.parametrize("relative", [
    "pcc/backend/emitter.py", "pcc/runtime/src/runtime.c", "pcc/runtime/include/runtime.h",
    "pcc/stdlib/os.py", "scripts/bootstrap.py", "scripts/bootstrap_stage1_checkpoint.py",
    "utils/table.json", "uv.lock", ".python-version", "pyproject.toml",
])
def test_source_identity_rejects_same_path_changed_bytes(tmp_path, relative):
    source = tmp_path / "source"
    path = source / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    root = tmp_path / "store"
    root.mkdir()
    original = {"compiler_sources": checkpoint.source_inventory(source)}
    checkpoint.ensure_build(root, original)
    path.write_bytes(b"modified")
    changed = {"compiler_sources": checkpoint.source_inventory(source)}
    with pytest.raises(checkpoint.CheckpointError, match="compiler_sources"):
        checkpoint.ensure_build(root, changed)


def test_dependency_inventory_hashes_extensions_and_package_metadata(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    extension = imports / "example.so"
    extension.write_bytes(b"old library")
    metadata = imports / "example.dist-info/METADATA"
    metadata.parent.mkdir()
    metadata.write_bytes(b"metadata")
    first = checkpoint._dependency_inventory([str(imports)], tmp_path / "source")
    extension.write_bytes(b"new library")
    second = checkpoint._dependency_inventory([str(imports)], tmp_path / "source")
    assert first != second
    assert len(first) == 2


def test_dependency_inventory_rejects_unrecorded_directory_alias(tmp_path):
    imports = tmp_path / "imports"
    imports.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (imports / "package").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(checkpoint.CheckpointError, match="symlinked dependency"):
        checkpoint._dependency_inventory([str(imports)], tmp_path / "source")


def test_settings_preserve_semantic_order_unset_and_secret_changes():
    env = {"PCC_UNKNOWN_TOKEN": "do-not-persist-this", "OTHER_SECRET": "nor-this",
           "PCC_PYTHON_IR_PASSES": "a,b", "PYTHONHASHSEED": "17"}
    original = checkpoint.settings_identity(env)
    encoded = checkpoint.canonical_bytes(original)
    assert b"do-not-persist-this" not in encoded and b"nor-this" not in encoded
    for name, value in (("PCC_UNKNOWN_TOKEN", "changed"), ("PCC_PYTHON_IR_PASSES", "b,a"),
                        ("PCC_WITH_THREADS", "1"), ("PCC_REFCOUNT_KIND", "plain"),
                        ("PCC_PY_FRONTEND_JOBS", "2"), ("PCC_GC_BACKEND", "generational")):
        assert checkpoint.settings_identity({**env, name: value}) != original
    assert checkpoint.settings_identity({}) != checkpoint.settings_identity({"PYTHONHASHSEED": ""})
    assert checkpoint.settings_identity(env) == checkpoint.settings_identity({**env, "PCC_STAGE1_CHECKPOINT_ATTEMPT": "other"})


def test_watchdog_redaction_keeps_default_off_behavior(monkeypatch):
    monkeypatch.syspath_prepend(str(bootstrap.SCRIPTS))
    import run_process_tree_sample as watchdog
    env = {"PCC_UNKNOWN_TOKEN": "do-not-persist-this", "PCC_PY_FRONTEND_JOBS": "2", "OTHER_SECRET": "secret"}
    assert watchdog._recorded_environment(env)["PCC_UNKNOWN_TOKEN"] == "do-not-persist-this"
    protected = watchdog._recorded_environment({**env, "PCC_STAGE1_CHECKPOINT_STAGE": "1"})
    assert protected["PCC_UNKNOWN_TOKEN"].startswith("sha256:")
    assert protected["PCC_PY_FRONTEND_JOBS"] == "2"
    assert "OTHER_SECRET" not in protected
    assert "do-not-persist-this" not in json.dumps(protected)


def test_build_identity_is_canonical_immutable_and_self_authenticating(tmp_path):
    payload = {"stage": 1, "source": "caf\u00e9"}
    identity = checkpoint.ensure_build(tmp_path, payload)
    assert identity == checkpoint.digest(payload)
    before = (tmp_path / "build.json").read_bytes()
    assert checkpoint.ensure_build(tmp_path, {"source": "caf\u00e9", "stage": 1}) == identity
    assert (tmp_path / "build.json").read_bytes() == before
    with pytest.raises(FileExistsError):
        checkpoint.write_immutable_json(tmp_path / "build.json", {"replace": True})
    tampered = json.loads(before)
    tampered["payload"]["stage"] = 2
    (tmp_path / "build.json").write_text(json.dumps(tampered))
    with pytest.raises(checkpoint.CheckpointError, match="invalid checkpoint build"):
        checkpoint.ensure_build(tmp_path, payload)


@pytest.mark.parametrize("body", ['{"stage":1,"stage":2}', '{"wall":NaN}'])
def test_checkpoint_json_rejects_duplicate_and_nonfinite_fields(tmp_path, body):
    path = tmp_path / "bad.json"
    path.write_text(body)
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.read_json(path)


def test_attempt_accounting_retains_failures_and_gaps(tmp_path):
    for name, wall, cpu, status, utc in (
        ("one", 3000, 2200, "TIMEOUT", "2026-10-06T08:00:03+00:00"),
        ("two", 5000, 4100, "COMPLETE", "2026-10-06T08:01:05+00:00"),
    ):
        guard = _started(tmp_path, name)
        _terminal_guard(guard, status="TIMEOUT" if name == "one" else "COMPLETE")
        checkpoint.write_immutable_json(tmp_path / "attempts" / (name + ".finished.json"), {
            "active_wall_ms": wall, "total_cpu_ms": cpu, "status": status,
            "finished_at_utc": utc, "guard_launch_requested": True,
            "total_cpu_exact": True})
    checkpoint.check_prior_attempts(tmp_path)
    result = checkpoint.summarize_attempts(tmp_path)
    assert result["attempt_count"] == 2
    assert result["cumulative_active_wall_ms"] == 8000
    assert result["cumulative_cpu_ms"] is None
    assert result["cumulative_cpu_ms_lower_bound"] == 6300
    assert result["unknown_cpu_intervals"] is True
    assert result["first_start_to_last_finish_wall_ms"] == 65000
    assert result["uninterrupted_performance_acceptance"] is False


# exercises the POSIX process-tree guard
@pytest.mark.pcc_gate(probe=lambda: not (os.name != "posix"))
@pytest.mark.parametrize("mode", ["waited", "timeout", "orphan"])
def test_real_guard_cpu_accounting_does_not_claim_unreaped_descendants(tmp_path, mode):
    """A terminal guard does not prove that descendant CPU reached the launcher."""
    child_receipt = tmp_path / "child-cpu.json"
    worker = tmp_path / "cpu_worker.py"
    worker.write_text("""import json
import os
import resource
import sys
import time
from pathlib import Path
started = time.process_time()
while time.process_time() - started < 3.0:
    pass
usage = resource.getrusage(resource.RUSAGE_SELF)
Path(sys.argv[1]).write_text(json.dumps({
    "pid": os.getpid(), "session": os.getsid(0),
    "cpu_ms": (usage.ru_utime + usage.ru_stime) * 1000,
}))
if sys.argv[2] != "waited":
    time.sleep(30)
""")
    target = tmp_path / "cpu_parent.py"
    target.write_text("""import subprocess
import sys
import time
from pathlib import Path
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])
if sys.argv[3] == "waited":
    raise SystemExit(child.wait(timeout=15))
while not Path(sys.argv[2]).is_file():
    time.sleep(0.01)
if sys.argv[3] == "timeout":
    time.sleep(30)
else:
    # Give the guard time to observe the child before its parent exits.
    time.sleep(0.35)
""")
    options = _options(tmp_path)
    attempt = checkpoint.Attempt(
        options.stage1_checkpoint, "a" * 64, options, [sys.executable], {}, _payload(),
    )
    attempt.request_guard_launch()
    guard_dir = attempt.guard_dir
    guard_tool = Path(bootstrap.__file__).with_name("run_process_tree_sample.py")
    output = tmp_path / "probe-output"
    output.write_bytes(b"host accounting probe")
    try:
        run = subprocess.run(
            [sys.executable, str(guard_tool), "--result", str(guard_dir / "result.json"),
             "--samples", str(guard_dir / "samples.tsv"),
             "--stdout", str(guard_dir / "target.stdout"),
             "--stderr", str(guard_dir / "target.stderr"), "--cwd", str(tmp_path),
             "--timeout", "6", "--interval", "0.1", "--max-tree-rss-bytes", "134217728",
             "--no-performance-lock", "--", sys.executable, str(target),
             str(worker), str(child_receipt), mode],
            capture_output=True, text=True, timeout=20,
        )
        assert run.returncode == (124 if mode == "timeout" else 0), run.stdout + run.stderr
        guard = checkpoint.read_json(guard_dir / "result.json")
        expected_status = "TIMEOUT" if mode == "timeout" else "COMPLETE"
        assert guard["status"] == expected_status
        summary = attempt.finish(expected_status, run.returncode, output, modules={})
        finished = checkpoint.read_json(
            options.stage1_checkpoint / "attempts" / (attempt.id + ".finished.json")
        )
        child = json.loads(child_receipt.read_text())
        measured_cpu = finished.get("total_cpu_ms_lower_bound", finished["total_cpu_ms"])
        # Preserve independent evidence, not a CPU estimate derived from RSS.
        (tmp_path / "cpu-proof.json").write_text(json.dumps({
            "mode": mode, "child": child, "finished": finished, "summary": summary,
        }, indent=2))
        # The worker must outspend the guard, launcher and pytest overhead
        # (about 0.9 s on a macOS host), or "less than the worker" proves nothing.
        assert child["cpu_ms"] >= 3000
        if mode == "waited":
            assert measured_cpu >= child["cpu_ms"]
            assert not guard.get("post_exit_cleanup_pids")
        else:
            # Missing CPU is observable: the worker alone used more than the
            # launcher's entire charged CPU, despite a terminal guard receipt.
            assert measured_cpu < child["cpu_ms"]
            state = subprocess.run(
                ["ps", "-p", str(child["pid"]), "-o", "stat="],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            assert not state or state.startswith("Z"), state
            if mode == "orphan":
                assert child["pid"] in guard["post_exit_cleanup_pids"]
        assert finished["active_wall_exact"] is True
        assert finished["total_cpu_exact"] is False
        assert finished["total_cpu_ms"] is None
        assert finished["total_cpu_ms_lower_bound"] == measured_cpu
        assert summary["cumulative_cpu_ms"] is None
        assert summary["cumulative_cpu_ms_lower_bound"] == measured_cpu
        assert summary["unknown_cpu_intervals"] is True
        checkpoint.check_prior_attempts(options.stage1_checkpoint)
    finally:
        if child_receipt.exists():
            child = json.loads(child_receipt.read_text())
            try:
                command = subprocess.run(
                    ["ps", "-p", str(child["pid"]), "-o", "command="],
                    capture_output=True, text=True, timeout=2,
                )
                expected = [sys.executable, str(worker), str(child_receipt), mode]
                if (command.returncode == 0
                        and shlex.split(command.stdout.strip()) == expected
                        and os.getsid(child["pid"]) == child["session"]):
                    os.kill(child["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, ValueError):
                pass


def test_lost_launcher_terminal_guard_is_only_a_lower_bound(tmp_path):
    guard = _started(tmp_path, "lost")
    _terminal_guard(guard, elapsed=7.25)
    checkpoint.check_prior_attempts(tmp_path)
    result = checkpoint.summarize_attempts(tmp_path)
    assert result["cumulative_active_wall_ms_lower_bound"] == 7250
    assert result["cumulative_active_wall_ms"] is None
    assert result["cumulative_cpu_ms"] is None
    assert result["unknown_wall_intervals"] and result["unknown_cpu_intervals"]


@pytest.mark.parametrize("status", [None, "RUNNING", "INVENTED_SUCCESS"])
def test_unresolved_guard_blocks_resume_and_never_improves_timing(tmp_path, status):
    guard = _started(tmp_path, "lost")
    if status:
        _terminal_guard(guard, status=status, elapsed=999)
    with pytest.raises(checkpoint.CheckpointError, match="unresolved watchdog"):
        checkpoint.check_prior_attempts(tmp_path)
    result = checkpoint.summarize_attempts(tmp_path)
    assert result["cumulative_active_wall_ms_lower_bound"] == 0
    assert result["cumulative_active_wall_ms"] is None


def test_proven_prelaunch_failure_allows_retry(tmp_path):
    _started(tmp_path, "not-launched")
    checkpoint.write_immutable_json(tmp_path / "attempts/not-launched.finished.json", {
        "guard_launch_requested": False, "status": "FAILED"})
    checkpoint.check_prior_attempts(tmp_path)


def test_guard_path_escape_is_rejected(tmp_path):
    root = tmp_path / "store"
    guard = _started(root, "escape")
    path = root / "attempts/escape.started.json"
    started = checkpoint.read_json(path)
    started["guard_result"] = str(tmp_path / "other/result.json")
    path.write_text(json.dumps(started))
    with pytest.raises(checkpoint.CheckpointError, match="escapes"):
        checkpoint.check_prior_attempts(root)


def test_launcher_retains_failed_attempt_and_publishes_resumed_only_after_smoke(tmp_path, monkeypatch, capsys):
    options = _options(tmp_path)
    options.out_dir.mkdir()
    monkeypatch.setattr(checkpoint, "build_identity", lambda *args: _payload())
    monkeypatch.setattr(checkpoint, "_module_summary", lambda *args: {
        "module_count": 2, "unique_completed_modules": 2, "missing_modules": 0,
        "rejected_modules": 0, "modules": [{"object_sha256": "b" * 64}]})
    observations = []
    results = iter((124, 0, 124))

    def guarded(target, selected, stage, environment):
        code = next(results)
        attempt = selected.checkpoint_attempt
        assert (options.stage1_checkpoint / "build.json").is_file()
        assert (options.stage1_checkpoint / "attempts" / (attempt.id + ".started.json")).is_file()
        assert environment["PCC_STAGE1_CHECKPOINT_ATTEMPT"] == attempt.id
        assert environment["PYTHONHASHSEED"] == "0"
        assert selected.env["PYTHONHASHSEED"] == "0"
        assert attempt.environment["PYTHONHASHSEED"] == "0"
        assert selected.env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert Path(environment["PYTHONPYCACHEPREFIX"]) == attempt.directory / "pycache"
        assert not list(Path(environment["PYTHONPYCACHEPREFIX"]).iterdir())
        observations.append((selected.profile_dir, selected.checkpoint_guard_dir))
        attempt.request_guard_launch()
        _terminal_guard(selected.checkpoint_guard_dir / "result.json", "TIMEOUT" if code else "COMPLETE")
        if code == 0:
            options.stage_output(1).write_bytes(b"simulated native executable")
            options.stage_output(1).chmod(0o755)
        return code, selected.checkpoint_guard_dir

    smoke_calls = []
    monkeypatch.setattr(bootstrap, "_run_guarded", guarded)
    monkeypatch.setattr(bootstrap, "stage_exec_barrier", lambda *args: smoke_calls.append(args) or 0)
    command = [sys.executable, "-m", "pcc"]
    with pytest.raises(bootstrap.BootstrapError) as failed:
        bootstrap.run_stage(1, options.stage_output(1), command, options)
    assert failed.value.exit_code == 124
    assert not (options.stage1_checkpoint / "final.json").exists()
    assert "PCC_BOOTSTRAP_STAGE_RESULT" not in capsys.readouterr().out
    bootstrap.run_stage(1, options.stage_output(1), command, options)
    final = checkpoint.read_json(options.stage1_checkpoint / "final.json")
    assert final["status"] == "complete_resumed"
    assert final["accounting"]["attempt_count"] == 2
    assert len(smoke_calls) == 1
    assert len(set(str(profile) for profile, guard in observations)) == 2
    assert all(Path(profile, "stage1.result.json").is_file() and guard.exists() for profile, guard in observations)
    assert all(row["status"] in ("TIMEOUT", "COMPLETE") for row in final["accounting"]["attempts"])
    live_output = capsys.readouterr().out
    assert "checkpoint=complete_resumed" in live_output
    assert "PCC_BOOTSTRAP_STAGE_CHECKPOINT_RESULT stage=1" in live_output
    from scripts.run_self_backend_bootstrap_gate import (
        _check_stage_elapsed_threshold,
        _parse_stage_elapsed_seconds,
    )
    from pcc.diagnostics.bootstrap_profile_report import _parse_stage_results
    assert _parse_stage_elapsed_seconds(live_output) == ()
    assert not _check_stage_elapsed_threshold([
        SimpleNamespace(backend="self", stage_elapsed_seconds=_parse_stage_elapsed_seconds(live_output))
    ], max_stage_elapsed=2700)
    live_log = tmp_path / "checkpoint.log"
    live_log.write_text(live_output)
    assert _parse_stage_results(live_log) == {}
    assert "PCC_STAGE1_CHECKPOINT_START previous_attempts=1" in live_output
    assert "prior_cumulative_active_wall_ms=" in live_output
    assert "current_attempt_limit_s=1800 uninterrupted_performance_acceptance=0" in live_output
    preserved = options.stage1_checkpoint / "attempts" / final["attempt_id"] / "final.json"
    assert preserved.exists()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.run_stage(1, options.stage_output(1), command, options)
    assert not (options.stage1_checkpoint / "final.json").exists()
    assert preserved.exists()
    assert "PCC_BOOTSTRAP_STAGE_RESULT" not in capsys.readouterr().out


def test_build_mismatch_fails_before_guard_or_stale_output_removal(tmp_path, monkeypatch):
    options = _options(tmp_path)
    options.out_dir.mkdir()
    options.stage_output(1).write_bytes(b"previous")
    root = options.stage1_checkpoint
    root.mkdir()
    checkpoint.ensure_build(root, _payload())
    changed = copy.deepcopy(_payload())
    changed["compiler_sources"] = [{"sha256": "changed"}]
    monkeypatch.setattr(checkpoint, "build_identity", lambda *args: changed)
    monkeypatch.setattr(bootstrap, "_run_guarded", lambda *args: pytest.fail("guard started for wrong identity"))
    with pytest.raises(bootstrap.BootstrapError, match="identity mismatch"):
        bootstrap.run_stage(1, options.stage_output(1), [sys.executable, "-m", "pcc"], options)
    assert options.stage_output(1).read_bytes() == b"previous"


# Hashes the real host interpreter's ELF libraries: only an x86_64 Linux
# CPython host can supply them (the checkpoint's only supported host).
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_selected_host_probe_hashes_real_interpreter_libraries_and_validates_elf(tmp_path, monkeypatch):
    """The archive admission is stubbed; actual host/ELF identity work executes."""
    from pcc.backend import elf_x86_64 as elf
    from pcc.backend.ar_writer import write_archive
    from pcc.driver import paths
    from pcc.frontends.python import owned_runtime_build

    data = elf.emit_relocatable(elf.ElfObject((elf.ElfSection(
        ".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR, 16, b"\xc3",
    ),), (elf.ElfSymbol.null(), elf.ElfSymbol("entry", 1, 0, 1, elf.STB_GLOBAL, elf.STT_FUNC))))
    archive = tmp_path / "runtime.a"
    archive.write_bytes(write_archive([("runtime.o", data)]))
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    prefix = tmp_path / "probe-pycache"
    prefix.mkdir()
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(prefix))
    monkeypatch.setattr(paths, "host_python_command", lambda *args: sys.executable)
    calls = []

    def admitted(runtime_dir, target, *, explicit_archive):
        calls.append((runtime_dir, target, explicit_archive))
        return explicit_archive

    monkeypatch.setattr(owned_runtime_build, "ensure_target_runtime", admitted)
    monkeypatch.setattr(checkpoint, "_dependency_inventory", lambda *args: [])
    result = checkpoint.probe_identity(str(bootstrap.ROOT))
    assert len(calls) == 1
    assert result["host_interpreter"]["executable"]["realpath"] == str(Path(sys.executable).resolve())
    assert result["host_interpreter"]["shared_libraries"]
    assert result["runtime"]["archive"]["sha256"] == checkpoint.file_identity(archive)["sha256"]
    assert result["runtime"]["members"][0]["name"] == "runtime.o"
    Path(str(archive) + ".target").write_text("linux:aarch64:other-target\n")
    with pytest.raises(checkpoint.CheckpointError, match="target stamp mismatch"):
        checkpoint.probe_identity(str(bootstrap.ROOT))


def test_actual_selected_host_import_roots_have_a_bounded_complete_inventory():
    roots = list(dict.fromkeys(str(Path(path or ".").resolve()) for path in sys.path))
    records = checkpoint._dependency_inventory(roots, bootstrap.ROOT)
    assert any(record.get("path", "").endswith("json/__init__.py") for record in records)
    assert all(record.get("sha256") or record["kind"] == "absent" for record in records)


def test_caught_launcher_failure_with_unresolved_guard_keeps_unknown_intervals(tmp_path, monkeypatch):
    options = _options(tmp_path)
    monkeypatch.setattr(checkpoint, "_module_summary", lambda *args: {})
    attempt = checkpoint.Attempt(tmp_path, "a" * 64, options, ["python", "-m", "pcc"], {}, _payload())
    attempt.request_guard_launch()
    _terminal_guard(attempt.guard_dir / "result.json", "RUNNING", elapsed=1)
    summary = attempt.finish("INTERRUPTED", 130, tmp_path / "missing-output")
    assert summary["cumulative_active_wall_ms"] is None
    assert summary["cumulative_cpu_ms"] is None
    assert summary["unknown_wall_intervals"] and summary["unknown_cpu_intervals"]


def test_attempt_clock_includes_module_validation_and_labels_publication_boundary(tmp_path, monkeypatch):
    attempt = checkpoint.Attempt(tmp_path, "a" * 64, _options(tmp_path), ["python", "-m", "pcc"], {}, _payload())
    clock = [10.0]
    attempt.started = clock[0]
    attempt.cpu_started = (0.0, 0.0)
    monkeypatch.setattr(checkpoint.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(checkpoint, "_cpu", lambda: (clock[0] - 10, 0.0))

    def summarize(*args):
        clock[0] += 3
        return {"unique_completed_modules": 1}

    monkeypatch.setattr(checkpoint, "_module_summary", summarize)
    result = attempt.finish("FAILED", 1, tmp_path / "missing")
    assert result["cumulative_active_wall_ms"] == 3000
    assert result["cumulative_cpu_ms"] == 3000
    finished = checkpoint.read_json(tmp_path / "attempts" / (attempt.id + ".finished.json"))
    assert finished["measurement_end"].endswith("before_ledger_publication")


def test_inputs_changing_during_compile_fail_before_smoke_and_success(tmp_path, monkeypatch, capsys):
    options = _options(tmp_path)
    options.out_dir.mkdir()
    payload = _payload()
    monkeypatch.setattr(checkpoint, "build_identity", lambda *args: copy.deepcopy(payload))
    monkeypatch.setattr(checkpoint, "_module_summary", lambda *args: {})

    def guarded(target, selected, stage, environment):
        selected.checkpoint_attempt.request_guard_launch()
        _terminal_guard(selected.checkpoint_guard_dir / "result.json", "COMPLETE")
        output = options.stage_output(1)
        output.write_bytes(b"simulated executable")
        output.chmod(0o755)
        payload["compiler_sources"] = [{"sha256": "changed during compile"}]
        return 0, selected.checkpoint_guard_dir

    monkeypatch.setattr(bootstrap, "_run_guarded", guarded)
    monkeypatch.setattr(bootstrap, "stage_exec_barrier", lambda *args: pytest.fail("smoke ran after drift"))
    with pytest.raises(bootstrap.BootstrapError, match="inputs changed during attempt"):
        bootstrap.run_stage(1, options.stage_output(1), [sys.executable, "-m", "pcc"], options)
    assert not (options.stage1_checkpoint / "final.json").exists()
    assert "PCC_BOOTSTRAP_STAGE_RESULT" not in capsys.readouterr().out


def test_fresh_prefix_ignores_eligible_stale_cpython_cache_and_stays_empty(tmp_path):
    source = tmp_path / "stale_probe.py"
    source.write_text("VALUE = 'source'\n")
    code = compile("VALUE = 'cached'\n", str(source), "exec")
    payload = importlib._bootstrap_external._code_to_timestamp_pyc(
        code, int(source.stat().st_mtime), source.stat().st_size)
    cache = tmp_path / "__pycache__" / ("stale_probe." + sys.implementation.cache_tag + ".pyc")
    cache.parent.mkdir()
    cache.write_bytes(payload)
    environment = dict(os.environ)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONOPTIMIZE", None)
    environment["PYTHONPATH"] = str(tmp_path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-B", "-c", "import stale_probe; print(stale_probe.VALUE)"]
    baseline = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True,
                              text=True, check=True, timeout=15)
    assert baseline.stdout.strip() == "cached", "fixture was not an eligible existing cache"
    prefix = tmp_path / "fresh-owned-cache"
    prefix.mkdir()
    selected = checkpoint.fresh_bytecode_environment(environment, prefix)
    actual = subprocess.run(command, cwd=tmp_path, env=selected, capture_output=True,
                            text=True, check=True, timeout=15)
    assert actual.stdout.strip() == "source"
    assert not list(prefix.iterdir())
    assert cache.read_bytes() == payload


# build_identity records the Linux CPU affinity (os.sched_getaffinity).
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_probe_uses_fresh_prefixes_without_identity_path_drift(tmp_path, monkeypatch):
    options = _options(tmp_path)
    options.repo_root = tmp_path / "source"
    options.repo_root.mkdir()
    seen = []

    def probe(command, **kwargs):
        environment = kwargs["env"]
        prefix = Path(environment["PYTHONPYCACHEPREFIX"])
        assert prefix.is_dir() and not list(prefix.iterdir())
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert not prefix.is_relative_to(options.repo_root)
        seen.append(prefix)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "target": checkpoint.TARGET, "host_interpreter": {}, "dependencies": [], "runtime": {},
        }))

    monkeypatch.setattr(checkpoint.subprocess, "run", probe)
    first_env = bootstrap.stage_environment(1, options)
    first_env.update(PYTHONPYCACHEPREFIX="/old/cache-one", PYTHONDONTWRITEBYTECODE="0")
    first = checkpoint.build_identity(options, [sys.executable, "-m", "pcc"], first_env)
    second = checkpoint.build_identity(options, [sys.executable, "-m", "pcc"], {
        **first_env, "PYTHONPYCACHEPREFIX": "/old/cache-two"})
    assert first == second
    assert first["bytecode_policy"] == checkpoint.BYTECODE_POLICY
    assert seen[0] != seen[1] and all(not prefix.exists() for prefix in seen)
    assert all(str(prefix) not in json.dumps(first) for prefix in seen)


@pytest.mark.parametrize("location", ["source", "dependency"])
def test_sourceless_legacy_bytecode_is_rejected(tmp_path, location):
    directory = tmp_path / ("pcc" if location == "source" else "imports")
    directory.mkdir()
    (directory / "untracked.pyc").write_bytes(b"legacy bytecode")
    with pytest.raises(checkpoint.CheckpointError, match="sourceless bytecode"):
        if location == "source":
            checkpoint.source_inventory(tmp_path)
        else:
            checkpoint._dependency_inventory([str(directory)], tmp_path / "source")


@pytest.mark.parametrize("status", ["complete_cold", "complete_resumed"])
def test_checkpoint_success_records_cannot_enter_ordinary_timing_parsers(tmp_path, status):
    from scripts.run_self_backend_bootstrap_gate import _parse_stage_elapsed_seconds
    from pcc.diagnostics.bootstrap_profile_report import _parse_stage_results
    ordinary = "PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=2800000 output=/build/pcc1 rc=0\n"
    checkpoint_line = ("PCC_BOOTSTRAP_STAGE_CHECKPOINT_RESULT stage=1 elapsed_ms=123 "
                       "output=/build/pcc1 rc=0 checkpoint=" + status + "\n")
    assert _parse_stage_elapsed_seconds(checkpoint_line) == ()
    assert _parse_stage_elapsed_seconds(ordinary + checkpoint_line) == ((1, 2800.0),)
    log = tmp_path / "bootstrap.log"
    log.write_text(ordinary + checkpoint_line)
    assert _parse_stage_results(log)[1]["wall_ms"] == 2800000


def _mock_identity_probe(monkeypatch):
    def probe(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "target": checkpoint.TARGET, "host_interpreter": {}, "dependencies": [], "runtime": {},
        }))
    monkeypatch.setattr(checkpoint.subprocess, "run", probe)


# Exercises build_identity, which records the Linux CPU affinity.
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
@pytest.mark.parametrize("setting", ["PCC_LOG_FILE", "PCC_HOIST_PROFILE_PATH"])
def test_real_diagnostic_writers_do_not_invalidate_checkpoint_identity(tmp_path, monkeypatch, setting):
    from pcc.frontends.python.pipeline_dependency_closure import _pcc_emit_import_log
    from pcc.frontends.python.codegen.hoist_analysis import write_hoist_profile
    options = _options(tmp_path)
    options.repo_root = tmp_path / "source"
    options.repo_root.mkdir()
    output = tmp_path / "diagnostic.log"
    environment = bootstrap.stage_environment(1, options)
    environment.update({setting: str(output), "PCC_LOG": "import"})
    monkeypatch.setenv(setting, str(output))
    monkeypatch.setenv("PCC_LOG", "import")
    _mock_identity_probe(monkeypatch)
    command = [sys.executable, "-m", "pcc"]
    initial = checkpoint.build_identity(options, command, environment)
    for iteration in (1, 2):
        if setting == "PCC_LOG_FILE":
            _pcc_emit_import_log(module="test" + str(iteration), classification="native", source="fixture.py")
        else:
            write_hoist_profile(True, str(output), {"compute_free_names_calls": iteration})
        assert output.is_file() and output.stat().st_size
        assert checkpoint.build_identity(options, command, environment) == initial


# Exercises build_identity, which records the Linux CPU affinity.
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
@pytest.mark.parametrize("setting", sorted(checkpoint.OUTPUT_PATH_SETTINGS))
def test_only_classified_output_file_contents_are_ignored(tmp_path, monkeypatch, setting):
    options = _options(tmp_path)
    options.repo_root = tmp_path / "source"
    options.repo_root.mkdir()
    output = tmp_path / "diagnostic-destination"
    environment = {**bootstrap.stage_environment(1, options), setting: str(output)}
    _mock_identity_probe(monkeypatch)
    command = [sys.executable, "-m", "pcc"]
    initial = checkpoint.build_identity(options, command, environment)
    output.write_text("existing diagnostic data")
    assert checkpoint.build_identity(options, command, environment) == initial
    output.write_text("updated diagnostic data")
    assert checkpoint.build_identity(options, command, environment) == initial
    # Even known output destination choices remain explicit config identity.
    changed_setting = {**environment, setting: str(tmp_path / "another-destination")}
    assert checkpoint.build_identity(options, command, changed_setting) != initial


# Exercises build_identity, which records the Linux CPU affinity.
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
@pytest.mark.parametrize("setting", ["PCC_UNKNOWN_PROFILE_INPUT", "PCC_UNKNOWN_SOURCE_FILE", "PCC_RUNTIME_ARCHIVE"])
def test_real_or_unknown_semantic_input_files_still_invalidate_identity(tmp_path, monkeypatch, setting):
    options = _options(tmp_path)
    options.repo_root = tmp_path / "source"
    options.repo_root.mkdir()
    source = tmp_path / "input-data"
    source.write_text("first input")
    environment = {**bootstrap.stage_environment(1, options), setting: str(source)}
    _mock_identity_probe(monkeypatch)
    command = [sys.executable, "-m", "pcc"]
    initial = checkpoint.build_identity(options, command, environment)
    source.write_text("changed input")
    assert checkpoint.build_identity(options, command, environment) != initial
