"""Only work that will run may consume the compiled-worker budget."""

from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_self_backend_config as config


@pytest.fixture
def cached_emit(tmp_path, monkeypatch):
    ir_texts = [
        'target triple = "arm64-apple-darwin23.6.0"\n; first\n',
        'target triple = "arm64-apple-darwin23.6.0"\n; second\n',
    ]
    cache_calls = []
    profile = {}
    monkeypatch.delenv(config.SELF_BACKEND_JOBS_ENV, raising=False)
    monkeypatch.delenv(config.SELF_BACKEND_SPLIT_THRESHOLD_BYTES_ENV, raising=False)
    monkeypatch.setattr(config, "parallel_cpu_budget", lambda: 8)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 1)
    # Leave 256 MiB, less than the 512 MiB safe emit-worker class.
    monkeypatch.setenv(
        workers.WORKER_TREE_BUDGET_ENV,
        str(workers.WORKER_COORDINATOR_RESERVE_BYTES + (256 << 20)),
    )
    monkeypatch.setattr(pipeline, "_python_frontend_worker_executable", lambda: "/unused/pcc1")

    def unexpected_work(*_args, **_kwargs):
        raise AssertionError("no compiler worker or in-process emitter may run")

    monkeypatch.setattr(pipeline, "_run_python_frontend_worker_commands", unexpected_work)
    monkeypatch.setattr(pipeline, "_run_self_backend_emit_worker_pool", unexpected_work)
    monkeypatch.setattr(pipeline, "_emit_self_asm_in_process", unexpected_work)
    monkeypatch.setattr(pipeline, "_publish_self_backend_object_cache", lambda *_args: True)
    monkeypatch.setattr(pipeline, "_maintain_self_backend_object_cache", lambda *_args: None)

    def emit(cache_status, *, split_large_modules, item_count=2):
        texts = ir_texts[:item_count]
        statuses = [cache_status] * len(texts) if isinstance(cache_status, str) else cache_status

        def plan_cache(items, target, _cc, _tmp_dir):
            cache_calls.append(list(items))
            assert target == "self-aarch64-darwin-v0"
            for index, (result_path, object_path, ir_path) in enumerate(items):
                assert Path(ir_path).read_text(encoding="utf-8") == texts[index]
                if statuses[index] == "hit":
                    Path(object_path).write_bytes(b"cached-object")
                    Path(result_path).write_text(
                        target + "\n" + object_path + "\nhit\n", encoding="utf-8",
                    )
            return [("", status) for status in statuses]

        monkeypatch.setattr(pipeline, "_plan_self_backend_object_cache", plan_cache)
        return pipeline._emit_self_objects_many_in_process(
            texts, str(tmp_path), "cc", split_large_modules=split_large_modules,
            profile=profile, internal_link=True,
        )

    return emit, cache_calls, profile


@pytest.mark.parametrize("split_large_modules", [False, True])
def test_all_small_cache_hits_need_no_worker_budget(cached_emit, split_large_modules):
    emit, cache_calls, profile = cached_emit
    results = emit("hit", split_large_modules=split_large_modules)

    assert len(cache_calls) == 1
    assert results == [
        ("self-aarch64-darwin-v0", item[1]) for item in cache_calls[0]
    ]
    assert all(Path(path).read_bytes() == b"cached-object" for _target, path in results)
    counters = profile["counters"]
    assert counters["link_self_native_split_jobs"] == 0
    assert counters["link_self_native_emit_jobs"] == 0
    assert counters["link_self_native_emit_pool_processes"] == 0
    assert counters["link_self_native_object_cache_hits"] == 2
    assert counters["link_self_native_large_ir_job_cap"] == 0


def test_small_cache_misses_still_require_worker_budget(cached_emit):
    emit, cache_calls, _profile = cached_emit
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one compiled emit worker"):
        emit("miss", split_large_modules=True)
    assert len(cache_calls) == 1


@pytest.mark.parametrize("split_large_modules", [False, True])
@pytest.mark.parametrize("statuses", [("hit", "miss"), ("miss", "hit")])
def test_one_remaining_miss_preserves_batch_admission(cached_emit, statuses, split_large_modules):
    emit, cache_calls, _profile = cached_emit
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one compiled emit worker"):
        emit(statuses, split_large_modules=split_large_modules)
    assert len(cache_calls) == 1


@pytest.mark.parametrize("policy", ["singleton", "explicit", "unbudgeted", "one_cpu"])
def test_existing_serial_or_explicit_admission_is_preserved(cached_emit, monkeypatch, policy):
    emit, cache_calls, profile = cached_emit
    pool_calls = []

    def run_pool(_prefix, items, _cc, _tmp_dir, label, width, _fresh, **_kwargs):
        pool_calls.append((list(items), label, width))
        for result_path, object_path, _ir_path in items:
            Path(object_path).write_bytes(b"emitted-object")
            Path(result_path).write_text(
                "self-aarch64-darwin-v0\n" + object_path + "\nmiss\n", encoding="utf-8",
            )
        return len(items)

    monkeypatch.setattr(pipeline, "_run_self_backend_emit_worker_pool", run_pool)
    if policy == "explicit":
        monkeypatch.setenv(config.SELF_BACKEND_JOBS_ENV, "2")
    elif policy == "unbudgeted":
        monkeypatch.delenv(workers.WORKER_TREE_BUDGET_ENV)
    elif policy == "one_cpu":
        monkeypatch.setattr(config, "parallel_cpu_budget", lambda: 1)
    statuses = ("miss",) if policy == "singleton" else ("hit", "miss")
    results = emit(statuses, split_large_modules=True, item_count=len(statuses))

    assert len(cache_calls) == 1
    assert pool_calls == [([cache_calls[0][-1]], "small", 1)]
    assert [Path(path).read_bytes() for _target, path in results] == (
        [b"emitted-object"] if policy == "singleton" else [b"cached-object", b"emitted-object"]
    )
    assert profile["counters"]["link_self_native_emit_pool_processes"] == 1


def test_split_work_still_computes_its_admission(cached_emit, monkeypatch):
    emit, cache_calls, _profile = cached_emit
    monkeypatch.setattr(pipeline, "_self_backend_split_threshold_bytes", lambda: 1)

    def reject_split(inputs, *, native_worker):
        assert len(inputs) == 2 and native_worker
        raise workers.FrontendWorkerContractError("split admission")

    monkeypatch.setattr(pipeline, "_self_backend_jobs_for_ir_texts", reject_split)
    with pytest.raises(workers.FrontendWorkerContractError, match="split admission"):
        emit("hit", split_large_modules=True)
    assert cache_calls == []
