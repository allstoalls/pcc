"""G-P3-LONGRUN slice 3: bounded smoke tier for the churn workload.

Compiles `benchmarks/python/longrun_churn.py` once (strict
no-libpython self-backend) and runs a SHORT bounded window on every
GC backend (0..4), asserting clean exit, well-formed CSV samples,
non-negative telemetry, live RSS, and no corruption sentinel. The
minutes-scale tier is manual (scripts/run_test_gates.py --gate gc-longrun, future) — never
default pytest. No cross-backend performance assertions here: smoke
checks the MEASUREMENT SURFACE, not collector behavior.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[2]
WORKLOADS = {
    "churn": (REPO_ROOT / "benchmarks" / "python" / "longrun_churn.py", "600"),
    "growshrink": (
        REPO_ROOT / "benchmarks" / "python" / "longrun_growshrink.py",
        "12",
    ),
    "finalizers": (
        REPO_ROOT / "benchmarks" / "python" / "longrun_finalizers.py",
        "300",
    ),
    "pointer_mutator": (
        REPO_ROOT / "benchmarks" / "python" / "longrun_pointer_mutator.py",
        "600",
    ),
}


@pytest.fixture(scope="module")
def longrun_binaries(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    out = {}
    base = tmp_path_factory.mktemp("longrun")
    for name, (src, bound) in WORKLOADS.items():
        exe = base / f"longrun_{name}"
        compile_python(
            str(src),
            str(exe),
            libpython_mode="off",
            ir_scaffold_mode="on",
            backend="self",
        )
        out[name] = (exe, bound)
    return out


@pytest.mark.parametrize("workload", ["churn", "growshrink", "finalizers", "pointer_mutator"])
@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_longrun_smoke_all_backends(longrun_binaries, backend, workload):
    binary, bound = longrun_binaries[workload]
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    result = subprocess.run(
        [str(binary), bound],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [l for l in result.stdout.splitlines() if l]
    assert "corrupt" not in result.stdout
    assert lines[-1].startswith("done,")
    samples = [l for l in lines if not l.startswith("done,")]
    assert len(samples) >= 3
    for line in samples:
        parts = line.split(",")
        if workload == "churn":
            assert len(parts) == 17, line
            elapsed, rss, peak, p_n, p_sum, p_max = (
                int(x) for x in parts[:6]
            )
            pause_hist = [int(x) for x in parts[6:10]]
            ops = int(parts[10])
            heap_in_use = int(parts[11])
            heap_capacity = int(parts[12])
            zpage_capacity, zpage_used, zpage_span, zpage_free_capacity = (
                int(x) for x in parts[13:17]
            )
            assert sum(pause_hist) == p_n, line
            assert zpage_capacity >= 0 and zpage_used >= 0, line
            assert zpage_span >= zpage_used, line
            assert zpage_free_capacity >= 0, line
        else:
            assert len(parts) in (9, 10), line
            elapsed, rss, peak, p_n, p_sum, p_max, ops = (
                int(x) for x in parts[:7]
            )
            heap_in_use = int(parts[7])
            heap_capacity = int(parts[8])
        if len(parts) == 10:
            # finalizer-canary gap: small constant only (the loop
            # variable legitimately keeps the last object alive)
            assert int(parts[9]) <= 2, line
        assert elapsed >= 0
        assert rss > 1024 * 1024
        assert peak >= 0
        assert p_n >= 0 and p_sum >= 0 and p_max >= 0
        assert ops > 0
        # fragmentation surface (backends 0-3 axis): allocator-held
        # bytes bound the in-use bytes from above
        assert heap_in_use > 0, line
        assert heap_capacity >= heap_in_use, line
    # ops strictly increase across samples
    ops_index = 10 if workload == "churn" else 6
    ops_series = [int(l.split(",")[ops_index]) for l in samples]
    assert ops_series == sorted(ops_series)
