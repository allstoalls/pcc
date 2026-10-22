"""Focused production-runtime coverage for the manual one-million gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNNER = REPO_ROOT / "scripts" / "run_vthread_1m_gate.py"
SPEC = importlib.util.spec_from_file_location("run_vthread_1m_gate", RUNNER)
assert SPEC is not None and SPEC.loader is not None
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def test_one_million_requires_explicit_manual_gate() -> None:
    assert R.manual_gate_enabled({}) is False
    assert R.manual_gate_enabled({R.MANUAL_ENV: "0"}) is False
    assert R.manual_gate_enabled({R.MANUAL_ENV: "1"}) is True


def test_gc3_malloc_ownership_is_explicit_after_minor_block_address_scan() -> None:
    py_oldification = (
        REPO_ROOT
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_generational_oldification.py"
    ).read_text(encoding="utf-8")

    assert '@c_abi_export("pcc_gc_generational_oldify_copy")' in py_oldification
    assert (
        "(new_flags & ~(128 | 4096 | 512 | 2048 | 262144)) | 256 | 262144"
        in py_oldification
    )


def test_small_production_runtime_matrix_is_real_and_balanced() -> None:
    manifest = R.run_gate(
        n=2_000,
        backends=(0, 1, 2, 3, 4),
        timer_n=200,
        io_n=20,
        build_timeout=240,
        backend_timeout=60,
    )
    assert manifest["mode"] == "real-runtime"
    assert manifest["status"] == "MEASURED"
    assert len(manifest["source_sha256"]) == 64
    assert len(manifest["runtime_archive_sha256"]) == 64
    assert manifest["backends"] == [0, 1, 2, 3, 4]
    assert len(manifest["results"]) == 5
    for backend, result in enumerate(manifest["results"]):
        assert result["backend"] == backend
        assert result["n"] == 2_000
        assert result["completed"] == 2_000
        assert result["scheduler_roots_final"] == 0
        assert result["ready_final"] == 0
        assert result["timer_final"] == 0
        assert result["io_final"] == 0
        assert result["peak_rss_bytes"] > 0
        assert result["throughput_vthreads_per_sec"] > 0
        assert result["resume_mean_ns"] > 0
        assert result["gc_pause_count"] >= 2
