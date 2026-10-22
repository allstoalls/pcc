from __future__ import annotations

import json

import pytest

from pcc.diagnostics.compile_observability import ObservabilityOptions, ObservedCompileError, observed_compile
from pcc.diagnostics import DiagnosticSpan
from pcc.frontends.python.codegen.errors import CodegenDiagnosticError


def test_observed_compile_writes_profile_json(tmp_path):
    profile = tmp_path / "profile.json"
    def compile_fn(src, out):
        return f"{src}->{out}"
    result = observed_compile(
        compile_fn,
        "a.py", "a.out",
        options=ObservabilityOptions(profile_json=str(profile), phase="unit"),
    )
    assert result == "a.py->a.out"
    data = json.loads(profile.read_text(encoding="utf-8"))
    assert data["schema"] == "pcc.profile.v1"
    assert data["events"][0]["name"] == "unit"


def test_observed_compile_formats_json_error():
    def compile_fn():
        raise RuntimeError("boom")
    with pytest.raises(ObservedCompileError) as caught:
        observed_compile(
            compile_fn,
            options=ObservabilityOptions(diagnostic_format="json", phase="unit"),
        )
    data = json.loads(caught.value.formatted)
    assert data["diagnostics"][0]["code"] == "PCC-PY-COMPILE-001"
    assert data["diagnostics"][0]["phase"] == "unit"


def test_observed_compile_includes_input_path_span_in_json_error(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    def compile_fn(path):
        raise RuntimeError("boom")

    with pytest.raises(ObservedCompileError) as caught:
        observed_compile(
            compile_fn,
            str(src),
            options=ObservabilityOptions(diagnostic_format="json", phase="unit"),
        )

    data = json.loads(caught.value.formatted)
    span = data["diagnostics"][0]["span"]
    assert span == {
        "file": str(src),
        "line": 0,
        "col": 0,
        "end_line": 0,
        "end_col": 0,
    }


def test_observed_compile_prefers_codegen_diagnostic_span():
    precise = DiagnosticSpan("pkg/codegen.py", 7, 3, 7, 12)

    def compile_fn(path):
        raise CodegenDiagnosticError(
            "unsupported expression",
            precise,
            "ScaffoldUnsupportedError",
        )

    with pytest.raises(ObservedCompileError) as caught:
        observed_compile(
            compile_fn,
            "fallback.py",
            options=ObservabilityOptions(diagnostic_format="json", phase="codegen"),
        )

    diagnostic = json.loads(caught.value.formatted)["diagnostics"][0]
    assert diagnostic["span"] == precise.to_json()
    assert "exception_type=ScaffoldUnsupportedError" in diagnostic["notes"]
