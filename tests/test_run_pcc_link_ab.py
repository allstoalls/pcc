from __future__ import annotations

import pytest

from scripts.run_pcc_link_ab import (
    LinkABError,
    _balanced_pair_order,
    _parse_env_assignments,
    _parse_competing_processes,
    _parse_time_metrics,
)


def test_link_ab_balances_pair_order() -> None:
    assert _balanced_pair_order(1) == ("control", "candidate")
    assert _balanced_pair_order(2) == ("candidate", "control")
    assert _balanced_pair_order(3) == ("control", "candidate")


def test_link_ab_parses_darwin_time_metrics() -> None:
    assert _parse_time_metrics(
        """real 17.07
user 16.19
sys 0.59
2049032192 maximum resident set size
187701520511 instructions retired
55412289008 cycles elapsed
2179057656 peak memory footprint
"""
    ) == {
        "real_s": 17.07,
        "user_s": 16.19,
        "sys_s": 0.59,
        "max_rss_bytes": 2049032192,
        "instructions": 187701520511,
        "cycles": 55412289008,
        "peak_footprint_bytes": 2179057656,
    }


def test_link_ab_rejects_incomplete_time_metrics() -> None:
    with pytest.raises(LinkABError, match="max_rss_bytes"):
        _parse_time_metrics("real 1.0\nuser 0.8\nsys 0.1\n")


def test_link_ab_parses_explicit_arm_environments() -> None:
    assert _parse_env_assignments(["PCC_FEATURE=off", "PYTHONHASHSEED=0"]) == {
        "PCC_FEATURE": "off",
        "PYTHONHASHSEED": "0",
    }
    with pytest.raises(LinkABError, match="NAME=VALUE"):
        _parse_env_assignments(["not-an-assignment"])


def test_link_ab_detects_only_competing_pcc_work() -> None:
    assert _parse_competing_processes(
        """ 12 /usr/bin/python ordinary.py
 13 /tmp/build/pcc1 --backend self input.py
 14 python scripts/bootstrap.py --stage 2
 15 /usr/bin/python worker.py --pcc-python-multi-codegen-worker x
 16 /tmp/output.macho --help
"""
    ) == [
        (13, "/tmp/build/pcc1 --backend self input.py"),
        (14, "python scripts/bootstrap.py --stage 2"),
        (15, "/usr/bin/python worker.py --pcc-python-multi-codegen-worker x"),
    ]
