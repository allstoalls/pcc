from io import StringIO

from pcc.diagnostics.runtime_log_env import RuntimeLogEvent, emit_runtime_event


def test_runtime_log_event_respects_channel(monkeypatch):
    out = StringIO()
    monkeypatch.setenv("PCC_LOG", "gc,alloc")
    emit_runtime_event(RuntimeLogEvent("collect", "gc", 1), channel="gc", stream=out)
    assert "collect" in out.getvalue()


def test_runtime_log_event_json(monkeypatch):
    out = StringIO()
    monkeypatch.setenv("PCC_LOG", "gc")
    monkeypatch.setenv("PCC_LOG_FORMAT", "json")
    emit_runtime_event(RuntimeLogEvent("collect", "gc", 1, type_name="PyList", size=8), channel="gc", stream=out)
    assert '"type": "PyList"' in out.getvalue()
