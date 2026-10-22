import json
import subprocess
import sys


def test_passes_explain_json():
    result = subprocess.run(
        [sys.executable, "scripts/pcc_passes_explain.py", "--format=json"],
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["schema"] == "pcc.diagnostics.pass_explain.v1"
    assert data["status"] == "UNKNOWN"
    assert data["ran"] == []


def test_passes_explain_preserves_observed_status_and_order(tmp_path):
    records = [
        {"event": "start", "module": "demo"},
        {"event": "pass", "module": "demo", "pass": "mem2reg", "status": "run", "elapsed_ms": 2},
        {"event": "pass", "module": "demo", "pass": "sroa", "status": "cache_hit"},
        {"event": "pass", "module": "demo", "pass": "adce", "status": "skip_budget"},
    ]
    path = tmp_path / "passes.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records))
    ran = subprocess.run([sys.executable, "scripts/pcc_passes_explain.py", "--format=json",
                          "--telemetry", str(path)], capture_output=True, text=True, timeout=10, check=True)
    data = json.loads(ran.stdout)
    assert data["passes"] == records[1:]
    assert data["ran"] == ["mem2reg"]
    assert data["skipped"] == ["adce"]
