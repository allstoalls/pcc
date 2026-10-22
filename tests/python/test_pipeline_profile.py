from __future__ import annotations

from pcc.diagnostics.profile_events import ProfileRecorder
from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_profile
from pcc.frontends.python.pipeline_profile import (
    ROADMAP_PHASES,
    run_profiled_phase,
    seed_expected_phase_counters,
)


def test_pipeline_profile_records_named_phase():
    rec = ProfileRecorder()
    result = run_profiled_phase(rec, "parse", lambda x: x + 1, 41)
    assert result.name == "parse"
    assert result.value == 42
    data = rec.to_json()
    assert data["events"][0]["name"] == "parse"


def test_pipeline_profile_exposes_all_roadmap_phase_names():
    rec = ProfileRecorder()
    seed_expected_phase_counters(rec)
    for name in ROADMAP_PHASES:
        assert "phase.expected." + name in rec.counters


def test_dictionary_profile_accumulates_events_totals_and_counters(monkeypatch):
    ticks = iter((1.0, 2.0, 3.0, 5.0))
    monkeypatch.setattr(pipeline_profile.time, "monotonic", lambda: next(ticks))
    profile = {}

    first = pipeline_profile.profile_begin(profile)
    pipeline_profile.profile_end(profile, "parse", first, detail="first")
    second = pipeline_profile.profile_begin(profile)
    pipeline_profile.profile_end(profile, "parse", second)
    pipeline_profile.profile_counter(profile, "modules", 2)

    assert profile["events"] == [
        {"name": "parse", "ms": 1000, "detail": "first"},
        {"name": "parse", "ms": 2000},
    ]
    assert profile["phase_totals_ms"] == {"parse": 3000}
    assert profile["counters"] == {"modules": 2}


def test_pipeline_facade_reexports_dictionary_profile_helpers():
    assert pipeline._profile_begin is pipeline_profile.profile_begin
    assert pipeline._profile_end is pipeline_profile.profile_end
    assert pipeline._profile_counter is pipeline_profile.profile_counter
    assert pipeline._profiled_gc_collect is pipeline_profile.profiled_gc_collect
