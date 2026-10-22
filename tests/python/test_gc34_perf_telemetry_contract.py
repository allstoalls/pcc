"""G-P0-GCPERF telemetry contract for GC #3/#4 reporting.

This is a measurement-surface guard, not a collector-speed assertion. It pins
the mode labels and counters that future GC #3/#4 optimization work needs for
same-host baselines: GC #3 collector work steps and GC #4 heap/zpage pressure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.run_gc_advantage_matrix import CASES, METRICS, _run_case


REPO_ROOT = Path(__file__).absolute().parents[2]
PROGRAM = REPO_ROOT / "benchmarks" / "python" / "gc_advantage_matrix.py"

pytestmark = pytest.mark.xdist_group(name="gc_perf_serial")


REQUIRED_GC34_METRICS = {
    "elapsed_us",
    "max_pause_us",
    "pause_count",
    "pause_sum_us",
    "work_steps",
    "rss_bytes",
    "heap_bytes",
    "heap_capacity_bytes",
    "reloc_forwards",
    "reloc_barriers",
    "evacuated_bytes",
    "zpage_count",
    "zpage_capacity_bytes",
    "zpage_used_bytes",
    "zpage_allocated_bytes",
    "zpage_reclaimable_gap_bytes",
    "zpage_span_bytes",
    "zpage_free_pages",
    "zpage_free_capacity_bytes",
    "zpage_free_span_bytes",
}


TARGET_CASES = [
    case
    for case in CASES
    if case.name
    in {
        "gc3_generational_high_frequency_collect",
        "gc4_colored_low_total_pause",
    }
]


@pytest.fixture(scope="module")
def gc_advantage_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    outdir = tmp_path_factory.mktemp("gc34_perf_telemetry")
    exe = outdir / "gc_advantage_matrix.out"
    compile_python(
        str(PROGRAM),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return exe


def test_gc34_metrics_are_reported_by_matrix_runner():
    assert set(METRICS) >= REQUIRED_GC34_METRICS


def test_gc34_target_cases_stay_mode_labeled():
    assert len(TARGET_CASES) == 2
    by_backend = {case.target_gc: case for case in TARGET_CASES}
    assert set(by_backend) == {3, 4}
    assert by_backend[3].mode == "node_churn"
    assert by_backend[3].target_metric == "elapsed_us"
    assert by_backend[4].mode == "node_churn"
    assert by_backend[4].target_metric == "pause_sum_us"


@pytest.mark.parametrize("case", TARGET_CASES, ids=[case.name for case in TARGET_CASES])
def test_gc34_focused_telemetry_rows_are_stable(gc_advantage_binary, case):
    row = _run_case(gc_advantage_binary, case, case.target_gc, reps=1, timeout=90)

    assert row["case"] == case.name
    assert row["target_gc"] == case.target_gc
    assert row["target_metric"] == case.target_metric
    assert row["backend"] == case.target_gc
    assert row["mode"] == case.mode
    assert row["claim"] == case.claim
    assert row["samples"]

    med = row["medians"]
    assert set(med) >= REQUIRED_GC34_METRICS
    assert int(med["elapsed_us"]) >= 0
    assert int(med["max_pause_us"]) >= 0
    assert int(med["pause_count"]) >= 0
    assert int(med["pause_sum_us"]) >= int(med["max_pause_us"])
    assert int(med["work_steps"]) >= 0
    assert int(med["rss_bytes"]) > 1024 * 1024
    assert int(med["heap_capacity_bytes"]) >= int(med["heap_bytes"]) >= 0
    assert int(med["reloc_forwards"]) >= 0
    assert int(med["reloc_barriers"]) >= 0
    assert int(med["evacuated_bytes"]) >= 0
    assert int(med["zpage_capacity_bytes"]) >= int(med["zpage_used_bytes"]) >= 0
    assert int(med["zpage_span_bytes"]) >= int(med["zpage_capacity_bytes"]) >= 0
    assert int(med["zpage_allocated_bytes"]) >= int(med["zpage_used_bytes"]) >= 0
    assert int(med["zpage_reclaimable_gap_bytes"]) >= 0
    assert int(med["zpage_free_pages"]) >= 0
    assert int(med["zpage_free_span_bytes"]) >= int(med["zpage_free_capacity_bytes"]) >= 0

    # Derived pressure baselines are intentionally non-ranking fields. They
    # expose current bookkeeping/RSS shape without asserting a speed win.
    heap_pressure = int(med["heap_capacity_bytes"]) - int(med["heap_bytes"])
    zpage_retained_gap = int(med["zpage_span_bytes"]) - int(med["zpage_used_bytes"])
    assert heap_pressure >= 0
    assert zpage_retained_gap >= 0

    for sample in row["samples"]:
        assert sample["backend"] == case.target_gc
        assert sample["result"] > 0
        assert set(sample) >= REQUIRED_GC34_METRICS | {"backend", "result"}


def test_gc34_contract_does_not_claim_completion_or_equivalence():
    """Keep this slice framed as telemetry only."""
    for case in TARGET_CASES:
        assert "equivalence" not in case.claim.lower()
        assert "complete" not in case.claim.lower()
