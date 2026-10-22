"""Full pcc1 -> pcc2 -> pcc3 bootstrap gate for GC backend 3."""

from __future__ import annotations

import pytest

from tests.python.test_pcc_bootstrap_full import (
    bootstrap_gc_parallel_slots,
    run_full_three_stage_bootstrap_self_gc,
    shared_stage1_pcc1,
)

pytestmark = pytest.mark.integration


def test_full_three_stage_bootstrap_self_gc3(request) -> None:
    from tests.python.test_bootstrap_gate_baseline import _is_macos_arm64
    if not _is_macos_arm64():
        from pathlib import Path
        from scripts.bootstrap_platform import run_from_shared
        root = Path(__file__).resolve().parents[3]
        receipt = run_from_shared("3", root / "build" / "platform-bootstrap" / "gc3", run_id=request.getfixturevalue("testrun_uid"))
        assert receipt["qualified"] and receipt["fixed_point"]
        assert [stage["stage"] for stage in receipt["stages"]] == [1, 2, 3]
        return
    run_full_three_stage_bootstrap_self_gc(
        "3",
        request.getfixturevalue("shared_stage1_pcc1"),
        request.getfixturevalue("pcc_runtime_archive"),
        parallel_slots=request.getfixturevalue("bootstrap_gc_parallel_slots"),
    )
