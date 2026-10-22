from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pcc.diagnostics.bootstrap_profile_report import (
    build_bootstrap_profile_report,
    format_bootstrap_profile_report,
)


def _write_stage(profile_dir: Path, stage: int, phases: dict[str, int]) -> None:
    total = phases.get(
        "compile_python_multi_total",
        phases.get("compile_python_total", sum(phases.values())),
    )
    payload = {
        "schema": "pcc.profile.v1",
        "total_ms": total,
        "phase_totals_ms": phases,
        "counters": {"multi_files": 3 + stage},
        "events": [],
    }
    (profile_dir / f"stage{stage}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_stage_result(
    profile_dir: Path,
    stage: int,
    *,
    compile_wall_ms: int,
    publish_barrier_ms: int,
    wall_ms: int,
    compile_user_ms: int = 0,
    compile_sys_ms: int = 0,
) -> None:
    payload = {
        "schema": "pcc.bootstrap_stage_result.v1",
        "stage": stage,
        "backend": "self",
        "output": f"/tmp/pcc{stage}",
        "compile_wall_ms": compile_wall_ms,
        "compile_user_ms": compile_user_ms,
        "compile_sys_ms": compile_sys_ms,
        "publish_barrier_ms": publish_barrier_ms,
        "wall_ms": wall_ms,
        "returncode": 0,
    }
    (profile_dir / f"stage{stage}.result.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_bootstrap_profile_report_summarizes_stage_profiles_and_wall_log(tmp_path):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    _write_stage(
        profile_dir,
        1,
        {
            "compile_python_multi_total": 1000,
            "multi_codegen_layer1": 400,
            "link_native": 300,
        },
    )
    _write_stage_result(
        profile_dir,
        1,
        compile_wall_ms=1100,
        compile_user_ms=900,
        compile_sys_ms=120,
        publish_barrier_ms=25,
        wall_ms=1125,
    )
    _write_stage_result(
        profile_dir,
        3,
        compile_wall_ms=300,
        compile_user_ms=5,
        compile_sys_ms=10,
        publish_barrier_ms=0,
        wall_ms=350,
    )
    stage3_result = json.loads((profile_dir / "stage3.result.json").read_text(encoding="utf-8"))
    stage3_result["returncode"] = 139
    (profile_dir / "stage3.result.json").write_text(
        json.dumps(stage3_result),
        encoding="utf-8",
    )
    _write_stage(
        profile_dir,
        2,
        {
            "compile_python_multi_total": 2000,
            "multi_codegen_layer1": 900,
            "link_native": 500,
        },
    )
    log = tmp_path / "bootstrap.log"
    log.write_text(
        "\n".join([
            "PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=1200 output=/tmp/pcc1",
            "PCC_BOOTSTRAP_STAGE_RESULT stage=2 elapsed_ms=2300 output=/tmp/pcc2",
            "",
        ]),
        encoding="utf-8",
    )

    report = build_bootstrap_profile_report(profile_dir, log_path=log, top=2)

    assert report["schema"] == "pcc.diagnostics.bootstrap_profile_report.v1"
    assert report["stage_count"] == 3
    assert report["total_wall_ms"] == 3850
    assert report["total_compile_wall_ms"] == 1400
    assert report["total_compile_user_ms"] == 905
    assert report["total_compile_sys_ms"] == 130
    assert report["total_publish_barrier_ms"] == 25
    assert report["total_compiler_profile_ms"] == 3000
    assert report["totals_by_phase_ms"]["multi_codegen_layer1"] == 1300
    assert report["stages"][0]["compile_wall_ms"] == 1100
    assert report["stages"][0]["compile_user_ms"] == 900
    assert report["stages"][0]["compile_sys_ms"] == 120
    assert report["stages"][0]["publish_barrier_ms"] == 25
    assert report["stages"][0]["unprofiled_wall_ms"] == 200
    assert report["stages"][1]["wall_ms"] == 2300
    assert report["stages"][1]["dominant_phase"]["name"] == "compile_python_multi_total"
    assert report["stages"][2]["returncode"] == 139
    assert report["stages"][2]["compiler_profile_ms"] == 0
    assert "profile" not in report["stages"][2]

    text = format_bootstrap_profile_report(report)
    assert "pcc bootstrap profile report" in text
    assert "total_compile_wall_ms: 1400" in text
    assert "total_compile_user_ms: 905" in text
    assert "user_ms" in text
    assert "barrier_ms" in text
    assert "multi_codegen_layer1: 1300 ms" in text


def test_bootstrap_profile_report_script_json(tmp_path):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    _write_stage(
        profile_dir,
        1,
        {
            "compile_python_total": 100,
            "codegen_layer1": 40,
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/bootstrap_profile_report.py",
            str(profile_dir),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "pcc.diagnostics.bootstrap_profile_report.v1"
    assert payload["stage_count"] == 1
    assert payload["total_compiler_profile_ms"] == 100
