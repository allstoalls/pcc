from __future__ import annotations

import pytest

from pcc.diagnostics.refcount_strategy_matrix import all_strategies, by_kind, make_env, validate_matrix


def test_refcount_strategy_matrix_covers_four_kinds():
    validate_matrix()
    assert [s.kind for s in all_strategies()] == [0, 1, 2, 3]


def test_refcount_strategy_build_envs_are_explicit():
    assert make_env(0, with_threads=False) == {
        "PCC_WITH_THREADS": "1",
        "PCC_REFCOUNT_KIND": "0",
    }
    assert make_env(2, with_threads=True)["PCC_REFCOUNT_KIND"] == "2"


def test_threaded_strategies_reject_no_thread_env():
    with pytest.raises(ValueError):
        make_env(1, with_threads=False)
    assert by_kind(2).header_layout == "pep703-biased-header"
    assert by_kind(3).header_layout == "deferred-queue"
