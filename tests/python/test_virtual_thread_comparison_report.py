from __future__ import annotations

import json
import subprocess
import sys

from pcc.diagnostics.virtual_thread_comparison import (
    build_virtual_thread_comparison_report,
    format_virtual_thread_comparison_report,
    parse_probe_output,
    sample_probe_data,
)


def test_virtual_thread_comparison_report_normalizes_probe_rows(tmp_path):
    probe_output = "\n".join(
        [
            "workload_iterations=4",
            (
                "row name=coroutine_thunk operations=4 wall_us=400 "
                "rss_before_kb=100 rss_after_kb=101 gc_pause_us=10 pin_events=0"
            ),
            (
                "row name=pcc_virtual_thread operations=4 wall_us=800 "
                "rss_before_kb=101 rss_after_kb=103 gc_pause_us=20 pin_events=2"
            ),
            (
                "row name=os_thread operations=4 wall_us=4000 "
                "rss_before_kb=103 rss_after_kb=112 gc_pause_us=30 pin_events=0"
            ),
        ]
    )
    profile_dir = tmp_path / "bootstrap-profile"
    profile_dir.mkdir()
    (profile_dir / "stage1.result.json").write_text(
        json.dumps(
            {
                "schema": "pcc.bootstrap_stage_result.v1",
                "stage": 1,
                "backend": "self",
                "output": "/tmp/pcc1",
                "compile_wall_ms": 100,
                "publish_barrier_ms": 7,
                "wall_ms": 120,
                "returncode": 0,
            }
        ),
        encoding="utf-8",
    )
    (profile_dir / "stage1.json").write_text(
        json.dumps(
            {
                "schema": "pcc.profile.v1",
                "total_ms": 95,
                "phase_totals_ms": {"multi_codegen_layer1": 60},
                "counters": {},
                "events": [],
            }
        ),
        encoding="utf-8",
    )

    report = build_virtual_thread_comparison_report(
        parse_probe_output(probe_output),
        bootstrap_profile_dir=profile_dir,
    )

    assert report["schema"] == "pcc.diagnostics.virtual_thread_comparison.v1"
    assert report["iterations"] == 4
    assert report["verdict"]["comparison_gate_complete"] is True
    assert report["verdict"]["production_virtual_threads"] is True
    assert "generated stackless virtual threads" in report["verdict"]["production_scope"]
    rows = {row["name"]: row for row in report["rows"]}
    assert rows["coroutine_thunk"]["latency_us_per_op"] == 100.0
    assert rows["pcc_virtual_thread"]["pinning_rate_per_1k_ops"] == 500.0
    assert rows["os_thread"]["rss_delta_kb"] == 9
    assert report["bootstrap"]["total_wall_ms"] == 120
    assert report["bootstrap"]["total_publish_barrier_ms"] == 7

    text = format_virtual_thread_comparison_report(report)
    assert "pcc virtual-thread comparison report" in text
    assert "coroutine_thunk" in text
    assert "pcc_virtual_thread" in text
    assert "os_thread" in text
    assert "production_virtual_threads: true" in text
    assert "limitations:" in text
    assert "bootstrap impact" in text


def test_virtual_thread_comparison_script_dry_run_json():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/virtual_thread_comparison.py",
            "--dry-run",
            "--format",
            "json",
            "--iterations",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "pcc.diagnostics.virtual_thread_comparison.v1"
    assert payload["verdict"]["comparison_gate_complete"] is True
    assert payload["verdict"]["production_virtual_threads"] is True
    assert {row["name"] for row in payload["rows"]} == {
        "coroutine_thunk",
        "pcc_virtual_thread",
        "os_thread",
    }


def test_virtual_thread_comparison_sample_data_uses_requested_iterations():
    payload = sample_probe_data(7)

    assert payload["iterations"] == 7
    assert {row["operations"] for row in payload["rows"]} == {7}
