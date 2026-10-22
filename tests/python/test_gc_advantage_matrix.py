"""Smoke coverage for the five-GC advantage benchmark surface.

The benchmark is a measurement tool, not a correctness oracle for collector
rankings.  This test only proves that the strict no-libpython self-backend
binary runs every encoded workload under every GC backend and emits the metrics
that the host runner documents.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from benchmarks.run_gc_advantage_matrix import CASES


REPO_ROOT = Path(__file__).absolute().parents[2]
PROGRAM = REPO_ROOT / "benchmarks" / "python" / "gc_advantage_matrix.py"


@pytest.fixture(scope="module")
def gc_advantage_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    outdir = tmp_path_factory.mktemp("gc_advantage_matrix")
    exe = outdir / "gc_advantage_matrix.out"
    compile_python(
        str(PROGRAM),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return exe


def _parse(stdout: str) -> dict[str, str]:
    row: dict[str, str] = {}
    for line in stdout.splitlines():
        if "," not in line:
            continue
        key, value = line.strip().split(",", 1)
        row[key] = value
    return row


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_gc_advantage_matrix_runs_all_backends(gc_advantage_binary, case, backend):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    result = subprocess.run(
        [
            str(gc_advantage_binary),
            case.mode,
            str(case.n),
            str(case.rounds),
            str(case.inner),
            str(case.collect_every),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = _parse(result.stdout)
    assert row["backend"] == backend
    assert int(row["result"]) > 0
    assert int(row["elapsed_us"]) >= 0
    assert int(row["max_pause_us"]) >= 0
    assert int(row["pause_count"]) >= 0
    assert int(row["rss_bytes"]) > 1024 * 1024
    assert int(row["heap_capacity_bytes"]) >= int(row["heap_bytes"]) >= 0
    assert int(row["reloc_forwards"]) >= 0
    assert int(row["zpage_capacity_bytes"]) >= int(row["zpage_used_bytes"]) >= 0
    assert int(row["zpage_span_bytes"]) >= int(row["zpage_capacity_bytes"]) >= 0
    assert int(row["zpage_allocated_bytes"]) >= int(row["zpage_used_bytes"]) >= 0
    assert int(row["zpage_reclaimable_gap_bytes"]) >= 0
    assert int(row["zpage_free_pages"]) >= 0
    assert int(row["zpage_free_span_bytes"]) >= int(row["zpage_free_capacity_bytes"]) >= 0
