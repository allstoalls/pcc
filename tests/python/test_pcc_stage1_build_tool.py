from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


TOOL = Path(__file__).absolute().parents[2] / "scripts" / "run_pcc_stage1_build.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("pcc_stage1_build_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage1_jobs_auto_resolves_from_the_unified_budget_formula() -> None:
    tool = _load_tool()
    gib = 1024**3
    # Explicit numeric jobs stay authoritative.
    assert tool._resolve_frontend_jobs("2", 64 * gib) == 2
    # auto derives from the shared budget formula: (8 GiB - 1 GiB reserve)
    # // 2 GiB measured host worker peak = 3.
    assert tool._resolve_frontend_jobs("auto", 8 * gib) == 3
    # A wide budget is cpu/hard-cap bound, never more than 10.
    assert tool._resolve_frontend_jobs("auto", 64 * gib) <= 10
    assert tool._resolve_frontend_jobs("auto", 64 * gib) >= 1
    # Unknown budget (0) keeps the historical cpu/10 behavior.
    assert 1 <= tool._resolve_frontend_jobs("auto", 0) <= 10
    with pytest.raises(ValueError):
        tool._resolve_frontend_jobs("fast", 8 * gib)


def test_stage1_default_memory_budget_names_its_ceiling() -> None:
    tool = _load_tool()
    explicit = tool._host_memory_budget_bytes(123)
    assert explicit == 123
    derived = tool._host_memory_budget_bytes(0)
    # Half of physical memory, or 0 when the probe is unavailable.
    assert derived >= 0


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
    ).with_threads == 0
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
        with_threads=0,
        direct_indexed_emit=True,
    )

    assert environment == {
        "PCC_WITH_THREADS": "0",
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
        environment, with_threads=0, python_ir_passes=control.python_ir_passes,
    )
    assert environment["PCC_PYTHON_IR_PASSES"] == "off"


def test_stage1_function_smoke_exercises_compile_and_runtime() -> None:
    tool = _load_tool()

    assert "def add(" in tool.FUNCTION_SMOKE_SOURCE
    assert "print(add(20, 22))" in tool.FUNCTION_SMOKE_SOURCE


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
