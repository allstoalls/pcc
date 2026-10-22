from __future__ import annotations

import json
import subprocess
import sys

from pcc.diagnostics.runtime_report import build_runtime_report, format_runtime_report


def test_runtime_report_distinguishes_production_gated_backends():
    report = build_runtime_report()
    payload = report.to_json()
    statuses = {cap["name"]: cap["status"] for cap in payload["capabilities"]}
    assert statuses["gc.backend.0.refcount-cycle"] == "production"
    assert statuses["gc.backend.1.incremental-tricolor"] == "production-gated"
    assert statuses["gc.backend.2.concurrent-mark-sweep"] == "production-gated"
    assert statuses["gc.backend.3.generational-minor-major"] == "production-gated"
    assert statuses["gc.backend.4.colored-relocating"] == "production-gated"
    assert statuses["threading.native"] == "partial"


def test_runtime_report_text_and_json_output():
    text = format_runtime_report("text")
    assert "pcc runtime report" in text
    assert "gc.backend.0.refcount-cycle" in text
    payload = json.loads(format_runtime_report("json"))
    assert payload["schema"] == "pcc.diagnostics.runtime_report.v1"


def test_runtime_report_script_json(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/pcc_runtime_report.py", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "pcc.diagnostics.runtime_report.v1"
