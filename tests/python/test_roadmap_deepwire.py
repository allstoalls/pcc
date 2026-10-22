from __future__ import annotations

import json


def test_roadmap_deepwire_installed_on_package_import():
    import pcc
    from pcc.frontends.python import pipeline
    from pcc.driver import cli_core

    assert hasattr(pcc, "__dir__")
    assert getattr(pipeline.compile_python, "_pcc_profiled", False)
    assert getattr(cli_core, "_pcc_deepwire_installed", False)


def test_cli_core_filters_observability_options_without_rejecting():
    from pcc.driver import cli_core

    parsed, status, err = cli_core.parse_cli_args([
        "--diagnostic-format=json",
        "--profile-json", "/tmp/pcc-profile.json",
        "--explain-fallback",
        "--passes=explain",
        "--explain-cache",
        "prog.py",
        "-o", "prog.out",
    ])
    assert status == 0, err
    assert parsed is not None
    assert parsed[0] == "prog.py"


def test_profile_recorder_phase_shape(tmp_path):
    from pcc.diagnostics.profile_events import ProfileRecorder, write_profile_json

    profile = tmp_path / "profile.json"
    recorder = ProfileRecorder()
    with recorder.phase("parse"):
        pass
    write_profile_json(str(profile), recorder)
    data = json.loads(profile.read_text(encoding="utf-8"))
    assert data["schema"] == "pcc.profile.v1"
    assert "parse" in data["phase_totals_ms"]
