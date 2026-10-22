from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


TOOL = Path(__file__).absolute().parents[2] / "scripts" / "run_pcc_stage1_build.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("pcc_stage1_build_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage1_jobs_auto_is_only_the_cpu_ceiling() -> None:
    tool = _load_tool()
    gib = 1024**3
    # Explicit numeric jobs stay authoritative.
    assert tool._resolve_frontend_jobs("2", 64 * gib) == 2
    # Phase admission uses real inputs and the spawning process's RSS later.
    assert tool._resolve_frontend_jobs("auto", 8 * gib) == tool._resolve_frontend_jobs("auto", 6 * gib)
    # A wide budget is cpu/hard-cap bound, never more than 10.
    assert tool._resolve_frontend_jobs("auto", 64 * gib) <= 10
    assert tool._resolve_frontend_jobs("auto", 64 * gib) >= 1
    # Unknown budget (0) keeps the historical cpu/10 behavior.
    assert 1 <= tool._resolve_frontend_jobs("auto", 0) <= 10
    with pytest.raises(ValueError):
        tool._resolve_frontend_jobs("fast", 8 * gib)


def test_stage1_default_memory_budget_names_its_ceiling(monkeypatch) -> None:
    from scripts import run_process_tree_sample as guard

    tool = _load_tool()
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    monkeypatch.setattr(guard, "_host_memory_observation", lambda: {
        "platform": "linux", "available_bytes": 6 * 1024**3,
        "cgroup_limits_verified": True,
    })
    explicit = tool._host_memory_budget_bytes(123)
    assert explicit == 123
    derived = tool._host_memory_budget_bytes(0)
    assert derived == 3 * 1024**3


def test_stage1_default_adopts_guard_and_rejects_conflicting_explicit_cap(monkeypatch):
    tool = _load_tool()
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(6 * 1024**3))
    assert tool._host_memory_budget_bytes(0) == 6 * 1024**3
    assert tool._host_memory_budget_bytes(6 * 1024**3) == 6 * 1024**3
    with pytest.raises(ValueError, match="restart under a matching guard"):
        tool._host_memory_budget_bytes(4 * 1024**3)


def test_stage1_thread_mode_is_explicit_and_receipt_bound() -> None:
    tool = _load_tool()
    environment: dict[str, str] = {}

    tool._set_stage1_build_modes(environment, with_threads=1)
    assert environment == {
        "PCC_WITH_THREADS": "1", "PCC_PYTHON_IR_PASSES": "default",
    }
    assert tool._parser().parse_args(
        [
            "--arm",
            "candidate",
            "--source-root",
            "/tmp/source",
            "--runtime-archive",
            "/tmp/runtime.a",
            "--output-dir",
            "/tmp/output",
        ]
    ).with_threads == 1
    assert tool._parser().parse_args(
        [
            "--arm",
            "candidate",
            "--source-root",
            "/tmp/source",
            "--runtime-archive",
            "/tmp/runtime.a",
            "--output-dir",
            "/tmp/output",
            "--with-threads",
            "1",
        ]
    ).with_threads == 1

    with pytest.raises(ValueError, match="thread mode"):
        tool._set_stage1_build_modes(environment, with_threads=2)


def test_stage1_direct_indexed_mode_is_explicit_and_receipt_bound() -> None:
    tool = _load_tool()
    environment: dict[str, str] = {}

    tool._set_stage1_build_modes(
        environment,
        with_threads=1,
        direct_indexed_emit=True,
    )

    assert environment == {
        "PCC_WITH_THREADS": "1",
        "PCC_PYTHON_IR_PASSES": "default",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
        "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
    }
    common = [
        "--arm",
        "candidate",
        "--source-root",
        "/tmp/source",
        "--runtime-archive",
        "/tmp/runtime.a",
        "--output-dir",
        "/tmp/output",
    ]
    assert not tool._parser().parse_args(common).direct_indexed_emit
    assert tool._parser().parse_args(
        common + ["--direct-indexed-emit"]
    ).direct_indexed_emit
    assert tool._parser().parse_args(common).performance_lock
    assert not tool._parser().parse_args(
        common + ["--no-performance-lock"]
    ).performance_lock
    assert tool._parser().parse_args(common).python_ir_passes == "default"
    control = tool._parser().parse_args(common + ["--python-ir-passes", "off"])
    tool._set_stage1_build_modes(
        environment, with_threads=1, python_ir_passes=control.python_ir_passes,
    )
    assert environment["PCC_PYTHON_IR_PASSES"] == "off"


def test_stage1_function_smoke_expects_what_cpython_prints(tmp_path: Path) -> None:
    tool = _load_tool()
    source = tmp_path / "smoke.py"
    source.write_text(tool.FUNCTION_SMOKE_SOURCE, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == tool.FUNCTION_SMOKE_EXPECTED_STDOUT


class _ScriptedAB:
    """``_run_process`` stand-in: the compile step creates the program."""

    class CompileABError(RuntimeError):
        pass

    def __init__(self, compile_rc=0, creates_program=True, run_rc=0, run_stdout="42\n"):
        self.commands = []
        self._compile = (compile_rc, creates_program)
        self._run = (run_rc, run_stdout)

    def _run_process(self, command, *, timeout, env, cwd):
        self.commands.append(list(command))
        if len(self.commands) == 1:
            returncode, creates_program = self._compile
            if creates_program:
                Path(command[-1]).write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, "c-out", "c-err")
        returncode, stdout = self._run
        return subprocess.CompletedProcess(command, returncode, stdout, "r-err")


def _function_smoke(tool, tmp_path: Path, ab: _ScriptedAB) -> None:
    work = tmp_path / "work"
    logs = tmp_path / "logs"
    work.mkdir()
    logs.mkdir()
    tool.run_function_smoke(
        tmp_path / "pcc1", work_dir=work, output_dir=logs, env={}, timeout=5, ab=ab,
    )


def test_stage1_function_smoke_compiles_then_runs_the_program(tmp_path: Path) -> None:
    tool = _load_tool()
    ab = _ScriptedAB()

    _function_smoke(tool, tmp_path, ab)

    work = tmp_path / "work"
    assert ab.commands == [
        [str(tmp_path / "pcc1"), "--backend", "self", "--python-libpython", "off",
         "--ir-scaffold", "on", str(work / "stage1_function_smoke.py"),
         "-o", str(work / "stage1_function_smoke")],
        [str(work / "stage1_function_smoke")],
    ]
    logs = tmp_path / "logs"
    assert (logs / "function-smoke-compile.stderr").read_text() == "c-err"
    assert (logs / "function-smoke-run.stdout").read_text() == "42\n"


@pytest.mark.parametrize(
    "ab_args",
    [
        {"compile_rc": 1},
        {"creates_program": False},
        {"run_rc": 3},
        {"run_stdout": "41\n"},
        {"run_stdout": "42"},
    ],
    ids=["compile-fails", "no-program", "run-fails", "wrong-value", "no-newline"],
)
def test_stage1_function_smoke_rejects_every_failure(tmp_path: Path, ab_args) -> None:
    tool = _load_tool()

    with pytest.raises(_ScriptedAB.CompileABError):
        _function_smoke(tool, tmp_path, _ScriptedAB(**ab_args))


@pytest.mark.integration
def test_stage1_function_smoke_passes_on_native_pcc1(
    tmp_path: Path, native_pcc1_compiler, pcc_runtime_archive,
) -> None:
    tool = _load_tool()
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        PCC_RUNTIME_ARCHIVE=str(pcc_runtime_archive),
        PCC_RUNTIME_CC="/usr/bin/false",
        PCC_HOST_PYTHON="/usr/bin/false",
        PCC_HOST_PCC="/usr/bin/false",
        PCC_NO_AUTO_PCC1="1",
    )
    work = tmp_path / "work"
    logs = tmp_path / "logs"
    work.mkdir()
    logs.mkdir()

    tool.run_function_smoke(
        native_pcc1_compiler, work_dir=work, output_dir=logs, env=env,
        timeout=300, ab=tool._load_ab_tool(),
    )

    assert (logs / "function-smoke-run.stdout").read_text() == "42\n"


def test_stage1_metric_contract_separates_tree_cpu_from_local_counters() -> None:
    tool = _load_tool()

    assert tool.STAGE1_METRIC_SCOPES == {
        "wall_s": "end_to_end_elapsed",
        "user_s": "timed_command_plus_waited_children_cpu",
        "system_s": "timed_command_plus_waited_children_cpu",
        "cpu_s": "timed_command_plus_waited_children_cpu_sum",
        "instructions": "coordinator_only_diagnostic",
        "cycles": "coordinator_only_diagnostic",
        "max_rss_bytes": "nonadditive_process_max_not_tree_sum",
        "peak_footprint_bytes": "coordinator_only_not_tree_sum",
    }
    assert tool.STAGE1_COMPARISON_CONTRACT == {
        "primary_compute_metric": "cpu_s",
        "wall_metric_role": "paired_end_to_end_observation",
        "required_comparison": "adjacent_alternating_same_environment_pairs",
        "single_wall_verdict_allowed": False,
        "hardware_counters_allowed_for_stage_verdict": False,
    }


def test_stage1_snapshot_canonicalizes_symlinked_parent(tmp_path: Path) -> None:
    tool = _load_tool()
    source = tmp_path / "source"
    source_file = source / "pcc" / "__main__.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("print(1)\n", encoding="utf-8")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    observed = []

    class FakeAB:
        @staticmethod
        def _copy_frozen(src, dst, _label):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

        @staticmethod
        def sha256_path(_path):
            return "digest"

        @staticmethod
        def _seal_source_snapshot(_root, _label):
            return None

        @staticmethod
        def _verify_source_snapshot(root, *_args, **_kwargs):
            observed.append(root)

    tool._snapshot_sources(
        source,
        {"files": {"pcc/__main__.py": "digest"}},
        alias_parent / "snapshot",
        FakeAB,
    )

    assert observed == [(real_parent / "snapshot").resolve()]
