"""Focused ownership contracts for self-backend pipeline configuration."""

from __future__ import annotations

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_self_backend_config as config


def test_pipeline_self_backend_config_facade_has_one_owner():
    assert pipeline._self_backend_jobs is config.jobs
    assert pipeline._self_backend_jobs_for_ir_texts is config.jobs_for_ir_texts
    assert pipeline._self_backend_jobs_for_input_sizes is config.jobs_for_input_sizes
    assert pipeline._self_backend_oversized_admission is config.compiled_native_oversized_admission
    assert pipeline._self_backend_skip_ll_temp is config.skip_ll_temp
    assert (
        pipeline._self_backend_split_large_modules_enabled
        is config.split_large_modules_enabled
    )
    assert pipeline._self_backend_split_threshold_bytes is config.split_threshold_bytes
    assert pipeline._self_backend_split_shard_bytes is config.split_shard_bytes
    assert pipeline._split_self_backend_large_ir_modules is config.split_large_ir_modules


def test_native_large_inputs_are_capped_not_serialized(monkeypatch):
    """A large input bounds the lane; it no longer collapses the whole batch.

    The old rule returned 1 as soon as ANY input crossed the split threshold,
    so one large module serialized every object in the batch -- measured at
    434 s in a cold stage1 emit phase against a 35 s baseline.  The cap is kept
    so a host with little memory, or a future regression that makes a single
    module expensive again, is still protected; only the collapse to 1 is gone.
    """
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV, raising=False)
    monkeypatch.delenv(config.SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV, raising=False)

    many_inputs = [2_000_000] + [1] * 63
    unbounded = config.jobs_for_input_sizes(many_inputs, native_worker=False)
    bounded = config.jobs_for_input_sizes(many_inputs, native_worker=True)

    assert bounded <= config.COMPILED_NATIVE_SAFE_JOBS
    assert bounded >= 1
    # The cap must actually bind on a batch wide enough to want more workers,
    # otherwise this test would pass with the cap removed entirely.
    if unbounded > config.COMPILED_NATIVE_SAFE_JOBS:
        assert bounded == config.COMPILED_NATIVE_SAFE_JOBS
        assert bounded < unbounded

    # An explicit job count still wins over the cap.
    monkeypatch.setenv(config.SELF_BACKEND_JOBS_ENV, "2")
    assert config.jobs_for_input_sizes(
        [2_000_000, 1],
        native_worker=True,
    ) == 2


def test_compiled_native_small_inputs_do_not_inherit_stage1_width(monkeypatch):
    # Without a declared tree budget no outer guard enforces an estimate.
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV, raising=False)
    monkeypatch.delenv(workers.WORKER_TREE_BUDGET_ENV, raising=False)
    monkeypatch.setattr(config, "parallel_cpu_budget", lambda: 12)

    assert config.jobs_for_input_sizes([1] * 20, native_worker=False) == 8
    assert config.jobs_for_input_sizes([1] * 20, native_worker=True) == 2


def test_compiled_native_emit_width_follows_a_declared_tree_budget(monkeypatch):
    """A declared tree budget admits compiled emit workers by their measured class.

    The budget is charged for the resident coordinator first (its RSS plus
    headroom, never below the configured floor), then one
    ``COMPILED_EMIT_SAFE_WORKER_PEAK_BYTES`` per worker, under the CPU bound.
    """
    gib = 1 << 30
    peak = config.COMPILED_EMIT_SAFE_WORKER_PEAK_BYTES
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV, raising=False)
    monkeypatch.setattr(config, "parallel_cpu_budget", lambda: 12)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: gib // 2)

    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(16 * gib))
    assert config.jobs_for_input_sizes([1] * 20, native_worker=True) == config.SELF_BACKEND_DEFAULT_JOBS
    assert config.jobs_for_input_sizes([1] * 3, native_worker=True) == 3

    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(4 * gib))
    expected = (4 * gib - workers.WORKER_COORDINATOR_RESERVE_BYTES) // peak
    assert 1 < expected < config.SELF_BACKEND_DEFAULT_JOBS
    assert config.jobs_for_input_sizes([1] * 20, native_worker=True) == expected

    resident = 3 * gib
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: resident)
    expected = (4 * gib - resident - workers.WORKER_COORDINATOR_RSS_HEADROOM_BYTES) // peak
    assert config.jobs_for_input_sizes([1] * 20, native_worker=True) == expected

    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 4 * gib - peak)
    with pytest.raises(workers.FrontendWorkerContractError):
        config.jobs_for_input_sizes([1] * 20, native_worker=True)

    # An explicit job count still wins, and host workers keep their own width.
    monkeypatch.setenv(config.SELF_BACKEND_JOBS_ENV, "5")
    assert config.jobs_for_input_sizes([1] * 20, native_worker=True) == 5
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV)
    assert config.jobs_for_input_sizes([1] * 20, native_worker=False) == config.SELF_BACKEND_DEFAULT_JOBS


def test_compiled_native_oversized_admission_follows_a_declared_tree_budget(monkeypatch):
    """Oversized compiled emit waves widen only under a declared tree budget.

    Each admitted worker is charged its measured base plus a per-input-byte
    peak, beside the resident coordinator; without a budget the caller keeps
    its fixed waves.
    """
    gib = 1 << 30
    base = config.COMPILED_EMIT_WORKER_BASE_BYTES
    per_byte = config.COMPILED_EMIT_PEAK_BYTES_PER_INPUT_BYTE
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV, raising=False)
    monkeypatch.setattr(config, "parallel_cpu_budget", lambda: 12)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: gib // 2)

    monkeypatch.delenv(workers.WORKER_TREE_BUDGET_ENV, raising=False)
    assert config.compiled_native_oversized_admission(5) is None

    monkeypatch.setenv(workers.WORKER_TREE_BUDGET_ENV, str(16 * gib))
    available = 16 * gib - workers.WORKER_COORDINATOR_RESERVE_BYTES
    assert config.compiled_native_oversized_admission(5) == (5, (available - 5 * base) // per_byte)
    width, cap = config.compiled_native_oversized_admission(20)
    assert width == config.SELF_BACKEND_DEFAULT_JOBS
    assert cap == (available - width * base) // per_byte

    # Too little room for even one modeled oversized input: serial waves.
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 16 * gib - 2 * base)
    width, cap = config.compiled_native_oversized_admission(5)
    assert width == 1 and cap < config.split_threshold_bytes()

    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 16 * gib)
    with pytest.raises(workers.FrontendWorkerContractError):
        config.compiled_native_oversized_admission(5)
