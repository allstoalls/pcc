"""Behavioral safety contracts for the portable bootstrap driver.

``scripts/bootstrap.py`` replaced ``scripts/bootstrap.sh``.  These tests hold
the same contracts the shell text used to encode, but exercise the Python
functions instead of grepping a script:

* a failed stage must not leave a previous run's artifact in place,
* stage boundaries are restartable (``--from-stage``/``--stage``/``--reuse-stage1``),
* default stage execution is memory-guarded and time-bounded,
* a self-backend stage runs through the native deferred wrapper (Python, not
  ``/bin/bash``),
* the fixed point is bytes-or-fail on every format (no normalized acceptance),
* the removed LLVM oracle cannot be selected again.
"""

from __future__ import annotations

import pytest

from scripts import bootstrap


def _options(tmp_path, **environment):
    # The mocked compiler/receipt fixtures have a known guard cap; automatic
    # availability and platform reserve selection have separate fixtures.
    settings = {"PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out"),
                "PCC_WORKER_TREE_BUDGET_BYTES": str(1024**3), **environment}
    return bootstrap.validate_settings(bootstrap.Options(settings))


def test_default_backend_is_the_owned_self_backend():
    """No ``uname`` special case: the owned backend is the only backend."""

    assert bootstrap.Options({}).backend == "self"
    explicit = bootstrap.parse_args(["--backend", "self"], bootstrap.Options({}))
    assert explicit.backend == "self"
    assert explicit.backend_explicit is True


def test_clean_removes_the_output_directory(tmp_path):
    options = _options(tmp_path)
    options.out_dir.mkdir(parents=True, exist_ok=True)
    (options.out_dir / "pcc1").write_bytes(b"stage")
    assert bootstrap.main(["--clean", "--out-dir", str(options.out_dir)]) == 0
    assert not options.out_dir.exists()


def test_failed_stage_removes_a_stale_artifact(tmp_path, monkeypatch):
    options = _options(tmp_path)
    options.out_dir.mkdir(parents=True, exist_ok=True)
    stale = options.stage_output(1)
    stale.write_bytes(b"previous stage")

    monkeypatch.setattr(bootstrap, "_run_guarded", lambda *a, **k: (3, None))
    with pytest.raises(bootstrap.BootstrapError) as excinfo:
        bootstrap.run_stage(1, stale, ["python", "-m", "pcc"], options)

    assert excinfo.value.exit_code == 3
    assert not stale.exists(), "a failed stage left a stale artifact in place"


def test_missing_artifact_after_a_successful_command_fails_closed(
    tmp_path, monkeypatch
):
    options = _options(tmp_path)
    options.out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(bootstrap, "_run_guarded", lambda *a, **k: (0, None))
    with pytest.raises(bootstrap.BootstrapError) as excinfo:
        bootstrap.run_stage(1, options.stage_output(1), ["python", "-m", "pcc"], options)

    assert excinfo.value.exit_code == 127


def test_stage_boundaries_are_restartable(tmp_path):
    options = bootstrap.Options({})
    parsed = bootstrap.parse_args(
        ["--from-stage", "3", "--stage", "3", "--reuse-stage1"], options
    )
    assert parsed.start_stage == 3
    assert parsed.stage_limit == 3
    assert parsed.reuse_stage1 is True

    short = bootstrap.parse_args(["--start-stage", "2", "--stage", "2"], options)
    assert short.start_stage == 2 and short.stage_limit == 2


def test_chain_runs_only_the_requested_stages(tmp_path, monkeypatch):
    options = _options(tmp_path)
    options.start_stage = 2
    options.stage_limit = 2
    ran: list[int] = []
    monkeypatch.setattr(bootstrap, "cache_identity_environment", lambda *a: None)
    monkeypatch.setattr(
        bootstrap, "run_stage", lambda stage, out, cmd, opts: ran.append(stage)
    )

    assert bootstrap.run_chain(options) == 0
    assert ran == [2]


def test_reuse_stage1_skips_the_host_stage(tmp_path, monkeypatch):
    options = _options(tmp_path)
    options.reuse_stage1 = True
    options.out_dir.mkdir(parents=True, exist_ok=True)
    options.stage_output(1).write_bytes(b"existing pcc1")
    options.start_stage = 2
    options.stage_limit = 2
    ran: list[int] = []
    monkeypatch.setattr(bootstrap, "cache_identity_environment", lambda *a: None)
    monkeypatch.setattr(
        bootstrap, "run_stage", lambda stage, out, cmd, opts: ran.append(stage)
    )

    assert bootstrap.run_chain(options) == 0
    assert ran == [2]


def test_default_stage_execution_is_memory_guarded_and_bounded(tmp_path):
    options = _options(
        tmp_path,
        PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES="4294967296",
        PCC_BOOTSTRAP_STAGE_TIMEOUT="1234",
    )
    assert options.external_memory_guard == "0"
    guard_dir = options.out_dir / "stage2.process.guard"
    command = bootstrap._posix_guard_command(guard_dir, options, ["compiler"])

    assert command[1].endswith("run_process_tree_sample.py")
    assert command[command.index("--result") + 1] == str(guard_dir / "result.json")
    assert command[command.index("--max-tree-rss-bytes") + 1] == "4294967296"
    assert command[command.index("--timeout") + 1] == "1234"
    assert command[-2:] == ["--", "compiler"]


def test_two_hour_ci_stage_deadline_preserves_local_default_and_memory_guard(tmp_path):
    assert bootstrap.Options({}).stage_timeout == 1800
    assert bootstrap.SAFE_MAX_STAGE_TIMEOUT == 7200
    options = _options(
        tmp_path, PCC_BOOTSTRAP_STAGE_TIMEOUT="7200",
        PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES="4294967296",
    )
    command = bootstrap._posix_guard_command(options.out_dir / "guard", options, ["compiler"])
    assert command[command.index("--timeout") + 1] == "7200"
    assert command[command.index("--max-tree-rss-bytes") + 1] == "4294967296"


@pytest.mark.parametrize("timeout", ("0", "-1", "7201"))
def test_ci_stage_deadline_remains_positive_and_capped(tmp_path, timeout):
    with pytest.raises(bootstrap.BootstrapError, match="PCC_BOOTSTRAP_STAGE_TIMEOUT"):
        _options(tmp_path, PCC_BOOTSTRAP_STAGE_TIMEOUT=timeout)


def test_stage_smoke_uses_an_explicit_runtime_archive(tmp_path, monkeypatch):
    """A compiled stage cannot rebuild the runtime archive, so hand it one.

    Without an explicit archive the native compiler treats the archive as
    unproved, enters the host-Python rebuild path and fails closed with
    "no-libpython function unavailable: ..._acquire_runtime_build_lock" --
    the stage barrier then fails for a reason that has nothing to do with the
    freshly built compiler.
    """

    import subprocess as subprocess_module

    runtime = tmp_path / "libpy_runtime_pcc_py.a"
    runtime.write_bytes(b"archive")
    options = bootstrap.Options(
        {
            "PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out"),
            "PCC_RUNTIME_ARCHIVE": str(runtime),
        }
    )
    options.out_dir.mkdir(parents=True, exist_ok=True)
    stage_binary = tmp_path / "pcc1"
    stage_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stage_binary.chmod(0o755)
    seen: dict[str, str] = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs.get("env") or {})
        stderr = kwargs.get("stderr")
        if hasattr(stderr, "write"):
            stderr.write(b"")
        return subprocess_module.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    assert bootstrap.stage_exec_barrier(stage_binary, 1, options) == 0
    assert seen["PCC_RUNTIME_ARCHIVE"] == str(runtime)


@pytest.mark.parametrize(
    "program_returncode,program_stderr,expected",
    [
        (9, b"native program failed\n", 9),
        (0, b"pcc runtime: refcount operation on an unmanaged pointer\n", 97),
    ],
    ids=["execution-failed", "execution-ownership-failed"],
)
def test_stage_smoke_rejects_a_failed_emitted_program(
    tmp_path, monkeypatch, program_returncode, program_stderr, expected
):
    import subprocess as subprocess_module

    options = _options(tmp_path, PCC_BOOTSTRAP_STAGE_EXEC_DELAY="0")
    options.out_dir.mkdir(parents=True)
    stage_binary = tmp_path / "pcc1"
    commands = []
    emitted = None

    def fake_run(command, **kwargs):
        nonlocal emitted
        commands.append(list(command))
        if "-o" in command:
            emitted = command[command.index("-o") + 1]
        if list(command) == [emitted]:
            kwargs["stderr"].write(program_stderr)
            return subprocess_module.CompletedProcess(command, program_returncode)
        return subprocess_module.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    assert bootstrap.stage_exec_barrier(stage_binary, 1, options) == expected
    assert commands[-1] == [emitted]
    assert not list(options.out_dir.glob("stage-smoke.*"))


def test_smoke_archive_defaults_to_the_repo_runtime_archive():
    archive = bootstrap._smoke_runtime_archive(bootstrap.Options({}))
    assert archive.endswith("pcc/runtime/libpy_runtime_pcc_py.a")
    assert bootstrap._smoke_runtime_archive(
        bootstrap.Options({"PCC_RUNTIME_ARCHIVE": ""})
    ).endswith("pcc/runtime/libpy_runtime_pcc_py.a")


def test_self_stage_runs_through_the_python_native_deferred_wrapper(
    tmp_path, monkeypatch
):
    options = _options(tmp_path)
    options.out_dir.mkdir(parents=True, exist_ok=True)
    captured: list[list[str]] = []

    def fake_guarded(target, opts, stage, environment):
        captured.append(list(target))
        return 127, None

    monkeypatch.setattr(bootstrap, "_run_guarded", fake_guarded)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.run_stage(2, options.stage_output(2), ["pcc1"], options)

    wrapper = captured[0][1]
    assert wrapper.endswith("run_pcc_native_deferred.py")
    assert "/bin/bash" not in captured[0]


def test_llvm_oracle_backend_is_rejected():
    """The oracle route died with the llvmlite dependency; fail closed."""

    with pytest.raises(SystemExit) as excinfo:
        bootstrap.parse_args(["--backend", "llvm"], bootstrap.Options({}))
    assert excinfo.value.code == 2
    parsed = bootstrap.parse_args(["--backend", "self"], bootstrap.Options({}))
    assert parsed.backend == "self"


def _fixed_point_pair(tmp_path, left: bytes, right: bytes):
    options = bootstrap.Options({"PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out")})
    options.out_dir.mkdir(parents=True, exist_ok=True)
    options.stage_output(2).write_bytes(left)
    options.stage_output(3).write_bytes(right)
    return options


def test_fixed_point_accepts_only_identical_bytes(tmp_path, capsys):
    options = _fixed_point_pair(tmp_path, b"\xcf\xfa\xed\xfe stage", b"\xcf\xfa\xed\xfe stage")
    assert bootstrap.verify_fixed_point(options) == 0
    assert "byte-identical" in capsys.readouterr().out


def test_fixed_point_rejects_size_drift(tmp_path):
    options = _fixed_point_pair(tmp_path, b"a" * 16, b"a" * 17)
    assert bootstrap.verify_fixed_point(options) == 1


def test_fixed_point_rejects_same_size_drift_without_normalizing(tmp_path):
    """ELF build-id / PE timestamp / Mach-O UUID differences are failures."""

    options = _fixed_point_pair(
        tmp_path,
        b"\x7fELF" + b"\x00" * 12 + b"build-id-a",
        b"\x7fELF" + b"\x00" * 12 + b"build-id-b",
    )
    assert bootstrap.verify_fixed_point(options) == 2


def test_successful_stage_prints_the_cpu_split_next_to_wall_time(
    tmp_path, monkeypatch, capsys
):
    """A stage1 slowdown must be diagnosable as work vs host load."""

    options = _options(tmp_path)
    options.out_dir.mkdir(parents=True, exist_ok=True)

    def fake_guarded(target, opts, stage, environment):
        output = opts.stage_output(stage)
        output.write_bytes(b"stage binary")
        output.chmod(0o755)
        return 0, None

    monkeypatch.setattr(bootstrap, "_run_guarded", fake_guarded)
    monkeypatch.setattr(bootstrap, "stage_exec_barrier", lambda *a, **k: 0)
    monkeypatch.setattr(
        bootstrap, "_children_cpu_split", iter([(10.0, 1.0), (910.0, 61.0)]).__next__
    )

    bootstrap.run_stage(1, options.stage_output(1), ["python", "-m", "pcc"], options)

    out = capsys.readouterr().out
    assert "PCC_BOOTSTRAP_STAGE_RESULT stage=1" in out
    assert (
        "PCC_BOOTSTRAP_STAGE_CPU stage=1 user_ms=900000 sys_ms=60000 "
        "cpu_ms=960000" in out
    )
