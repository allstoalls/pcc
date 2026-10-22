from pcc.diagnostics.gc_leak_finder import analyze_gc_events, analyze_gc_log_text


def test_gc_leak_finder_detects_growth():
    findings = analyze_gc_events([
        {"event": "alloc", "type": "PyList", "size": 4096},
        {"event": "free", "type": "PyList", "size": 1},
    ])
    assert any(f.kind == "allocation_growth" for f in findings)


def test_gc_leak_finder_detects_finalizer_exception_from_json_lines():
    findings = analyze_gc_log_text('{"event":"finalizer_exception","cause":"boom"}\n')
    assert findings[0].kind == "finalizer_exception"
