from __future__ import annotations

import json

from pcc.frontends.python import pipeline


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_classify_python_import_writes_import_route_jsonl(monkeypatch, tmp_path):
    log_path = tmp_path / "imports.jsonl"
    monkeypatch.setenv("PCC_LOG", "import")
    monkeypatch.setenv("PCC_LOG_FILE", str(log_path))

    assert pipeline._classify_python_import("sys") == "builtin_native_dispatch"
    assert (
        pipeline._classify_python_import("definitely_not_a_pcc_native_module")
        == "cpython_fallback"
    )

    events = _read_jsonl(log_path)
    assert [event["category"] for event in events] == ["import", "import"]
    assert [event["event"] for event in events] == ["route", "route"]
    assert events[0]["module"] == "sys"
    assert events[0]["classification"] == "builtin_native_dispatch"
    assert events[0]["native"] is True
    assert events[1]["classification"] == "cpython_fallback"
    assert events[1]["native"] is False
    assert events[1]["source"] == "missing_native_provider"


def test_classify_python_import_stays_silent_when_import_log_disabled(monkeypatch, tmp_path):
    log_path = tmp_path / "imports.jsonl"
    monkeypatch.delenv("PCC_LOG", raising=False)
    monkeypatch.setenv("PCC_LOG_FILE", str(log_path))

    assert pipeline._classify_python_import("sys") == "builtin_native_dispatch"
    assert not log_path.exists()


def test_import_route_log_uses_all_channel(monkeypatch, tmp_path):
    log_path = tmp_path / "imports.jsonl"
    monkeypatch.setenv("PCC_LOG", "gc,all")
    monkeypatch.setenv("PCC_LOG_FILE", str(log_path))

    assert pipeline._classify_python_import("typing") == "compile_time_only"
    events = _read_jsonl(log_path)
    assert events[0]["classification"] == "compile_time_only"
    assert events[0]["top"] == "typing"
