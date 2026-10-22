from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from scripts import bootstrap


BOOTSTRAP = Path(__file__).absolute().parents[2] / "scripts" / "bootstrap.py"
STAGE_AB = Path(__file__).absolute().parents[2] / "scripts" / "run_pcc_stage_ab.py"


def _load_stage_ab():
    spec = importlib.util.spec_from_file_location("pcc_stage_ab", STAGE_AB)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_stage_result_declares_metric_scope_and_pairing_contract(
    tmp_path, monkeypatch
) -> None:
    # The fake stage writes stage_output(1); without an isolated out dir that
    # is the checkout's real build/bootstrap/pcc1.
    options = bootstrap.Options({
        "PCC_BOOTSTRAP_PROFILE_DIR": str(tmp_path),
        "PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out"),
    })
    options.out_dir.mkdir(parents=True, exist_ok=True)

    def fake_guarded(target, opts, stage, environment):
        output = opts.stage_output(stage)
        output.write_bytes(b"stage binary")
        output.chmod(0o755)
        return 0, None

    monkeypatch.setattr(bootstrap, "_run_guarded", fake_guarded)
    monkeypatch.setattr(bootstrap, "stage_exec_barrier", lambda *a, **k: 0)

    bootstrap.run_stage(1, options.stage_output(1), ["python", "-m", "pcc"], options)

    payload = json.loads((tmp_path / "stage1.result.json").read_text(encoding="utf-8"))
    scopes = payload["metric_scopes"]
    assert scopes["compile_user_ms"] == "timed_command_plus_waited_children_cpu"
    assert scopes["compile_sys_ms"] == "timed_command_plus_waited_children_cpu"
    assert scopes["wall_ms"] == "end_to_end_elapsed_including_publish_barrier"
    contract = payload["comparison_contract"]
    assert contract["wall_metric_role"] == "paired_end_to_end_observation"
    assert contract["required_comparison"] == (
        "adjacent_alternating_same_environment_pairs"
    )
    assert contract["single_wall_verdict_allowed"] is False
    assert payload["schema"] == "pcc.bootstrap_stage_result.v1"


def test_bootstrap_defaults_to_safe_auto_lanes_and_rejects_wide_override() -> None:
    options = bootstrap.Options({})
    assert options.py_frontend_jobs == "auto"
    assert options.stage1_py_frontend_jobs == "auto"
    assert options.self_backend_jobs == "2"
    assert options.macho_link_jobs == "8"
    assert bootstrap.SAFE_MAX_JOBS == 2
    assert bootstrap.SAFE_MAX_LINK_JOBS == 8
    assert bootstrap.SAFE_MAX_TREE_RSS_BYTES == 17179869184
    # The tree budget default equals the safe maximum: measured stage1 peaks
    # sat at 5.8 GiB of the old 8 GiB cap, so the cap was never the bound --
    # but four workers need the headroom the safe maximum already sanctioned.
    assert options.max_tree_rss_bytes == 17179869184
    # 600 s left under 20% margin over a healthy local Stage1 (518 s) and
    # cannot fit a 3-core CI runner; the watchdog stops runaways only.
    assert options.stage_timeout == 1800
    # Raised for two-hour CI Stage1 builds (0198e2052).
    assert bootstrap.SAFE_MAX_STAGE_TIMEOUT == 7200
    assert options.external_memory_guard == "0"
    assert options.in_process_codegen == "0"
    assert options.defer_frontend_codegen == "1"
    assert options.defer_self_link == "1"
    assert options.runtime_cc == "pcc"
    assert options.runtime_high == "py"
    assert options.python_libpython == "off"

    with pytest.raises(
        bootstrap.BootstrapError, match="unsafe bootstrap worker budget"
    ):
        bootstrap.validate_settings(
            bootstrap.Options({"PCC_BOOTSTRAP_PY_FRONTEND_JOBS": "4"})
        )
    explicit = bootstrap.validate_settings(
        bootstrap.Options(
            {
                "PCC_BOOTSTRAP_PY_FRONTEND_JOBS": "4",
                "PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS": "1",
            }
        )
    )
    assert explicit.py_frontend_jobs == "4"

    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    environment["PCC_BOOTSTRAP_PY_FRONTEND_JOBS"] = "4"
    rejected = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--help"],
        cwd=BOOTSTRAP.parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert rejected.returncode == 2
    assert "unsafe bootstrap worker budget" in rejected.stderr

    environment["PCC_BOOTSTRAP_UNSAFE_HIGH_MEMORY_JOBS"] = "1"
    explicit_run = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--help"],
        cwd=BOOTSTRAP.parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert explicit_run.returncode == 0, explicit_run.stderr


def test_stage1_receipt_marks_local_hardware_counters_diagnostic_only() -> None:
    source = (
        Path(__file__).absolute().parents[2]
        / "scripts"
        / "run_pcc_stage1_build.py"
    ).read_text(encoding="utf-8")

    assert '"instructions": "coordinator_only_diagnostic"' in source
    assert '"cycles": "coordinator_only_diagnostic"' in source
    assert '"hardware_counters_allowed_for_stage_verdict": False' in source


def test_stage_pair_order_is_adjacent_and_balanced() -> None:
    tool = _load_stage_ab()

    assert tool.pair_order(1) == ("baseline", "candidate")
    assert tool.pair_order(2) == ("candidate", "baseline")
    assert tool.pair_order(3) == ("baseline", "candidate")
    with pytest.raises(ValueError, match="positive"):
        tool.pair_order(0)
    parsed = tool._parser().parse_args(
        [
            "--baseline-source",
            "/tmp/baseline",
            "--candidate-source",
            "/tmp/candidate",
            "--runtime-archive",
            "/tmp/runtime.a",
            "--output-dir",
            "/tmp/output",
            "--pairs",
            "1",
            "--first-pair-index",
            "2",
        ]
    )
    assert parsed.pairs == 1
    assert parsed.first_pair_index == 2
    assert parsed.frontend_jobs == 2
    assert parsed.self_backend_jobs == 2
    assert parsed.max_tree_rss_bytes == 8 * 1024 * 1024 * 1024
    tool._validate_resource_limits(parsed)

    parsed.max_tree_rss_bytes = 16 * 1024 * 1024 * 1024
    tool._validate_resource_limits(parsed)
    parsed.max_tree_rss_bytes += 1
    with pytest.raises(tool.StageABError, match="16 GiB"):
        tool._validate_resource_limits(parsed)
    parsed.max_tree_rss_bytes = 8 * 1024 * 1024 * 1024

    parsed.frontend_jobs = 3
    with pytest.raises(tool.StageABError, match="frontend jobs"):
        tool._validate_resource_limits(parsed)


def test_stage_pair_summary_uses_tree_cpu_without_automatic_verdict() -> None:
    tool = _load_stage_ab()

    def stage1(wall: float, cpu: float, rss: int):
        return {
            "result": {"metrics": {"wall_s": wall, "cpu_s": cpu}},
            "process": {"peak_tree_rss_bytes": rss},
        }

    pairs = [
        {
            "baseline": {"stage1": stage1(100.0, 500.0, 1000)},
            "candidate": {"stage1": stage1(110.0, 400.0, 900)},
        },
        {
            "baseline": {"stage1": stage1(120.0, 520.0, 1100)},
            "candidate": {"stage1": stage1(105.0, 410.0, 950)},
        },
    ]

    summary = tool.summarize_pairs(pairs)

    assert summary["verdict"] == "MEASURED_NO_AUTOMATIC_ACCEPTANCE"
    assert summary["comparison_contract"]["primary_compute_metric"] == "cpu_s"
    stage = summary["stages"]["stage1"]
    assert stage["medians"]["baseline"]["cpu_s"] == pytest.approx(510.0)
    assert stage["medians"]["candidate"]["cpu_s"] == pytest.approx(405.0)
    assert stage["paired_median_candidate_over_baseline"]["cpu_s"] < 0.8
    assert stage["paired_candidate_over_baseline"]["wall_s"] == [1.1, 0.875]


def test_stage2_runner_uses_auto_oversized_lane_and_two_backend_workers() -> None:
    tool = _load_stage_ab()

    environment = tool._stage2_environment_overrides(
        pair_index=7,
        arm="candidate",
        self_backend_jobs=2,
        max_tree_rss_bytes=10 * 1024 * 1024 * 1024,
    )

    assert environment["PCC_BOOTSTRAP_PY_FRONTEND_JOBS"] == "auto"
    assert environment["PCC_PY_FRONTEND_JOBS"] == "auto"
    assert environment["PCC_SELF_BACKEND_JOBS"] == "2"
    assert environment["PCC_MACHO_LINK_JOBS"] == "8"
    assert environment["PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD"] == "1"
    assert environment["PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES"] == str(
        10 * 1024 * 1024 * 1024
    )


def test_stage_failure_summary_names_largest_worker_manifest() -> None:
    tool = _load_stage_ab()

    summary = tool._process_failure_summary(
        {
            "status": "MEMORY_LIMIT",
            "returncode": -15,
            "peak_tree_rss_bytes": 9_000_000_000,
            "largest_process_observed": {
                "pid": 42,
                "rss_bytes": 7_000_000_000,
                "command": "pcc1 --worker /tmp/worker_9.manifest",
                "manifest_paths": ["/tmp/worker_9.manifest"],
            },
        }
    )

    assert "status=MEMORY_LIMIT" in summary
    assert "largest_pid=42" in summary
    assert "manifests=/tmp/worker_9.manifest" in summary


def test_stage_pair_environment_normalizes_arm_private_paths(tmp_path) -> None:
    tool = _load_stage_ab()
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"
    first_source = first_output / "source-snapshot"
    second_source = second_output / "source-snapshot"
    first = {
        "PCC_SOURCE_ROOT": str(first_source),
        "PCC_RUNTIME_ARCHIVE": str(first_output / "runtime-bundle" / "runtime.a"),
        "PYTHONPYCACHEPREFIX": str(first_output / "private-state" / "pycache"),
    }
    second = {
        "PCC_SOURCE_ROOT": str(second_source),
        "PCC_RUNTIME_ARCHIVE": str(second_output / "runtime-bundle" / "runtime.a"),
        "PYTHONPYCACHEPREFIX": str(second_output / "private-state" / "pycache"),
    }

    assert tool.normalize_arm_environment(
        first, output_root=first_output, source_root=first_source
    ) == tool.normalize_arm_environment(
        second, output_root=second_output, source_root=second_source
    )
