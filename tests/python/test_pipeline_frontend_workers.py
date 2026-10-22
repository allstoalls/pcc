"""Behavior contracts for extracted frontend worker policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_frontend_workers


def test_frontend_job_budget_and_overrides_match_the_facade(monkeypatch):
    assert pipeline_frontend_workers.frontend_jobs(111, "auto", 64) == 10
    assert pipeline_frontend_workers.frontend_jobs(3, "20", 1) == 3
    assert pipeline_frontend_workers.frontend_jobs(111, "off", 64) == 1
    assert pipeline_frontend_workers.frontend_jobs(1, "8", 64) == 1
    assert pipeline_frontend_workers.numeric_jobs_override("5") is True
    assert pipeline_frontend_workers.numeric_jobs_override("auto") is False

    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "3")
    assert pipeline._python_frontend_jobs(9) == 3


def test_compiled_native_auto_width_is_memory_bounded(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_frontend_workers, "_coordinator_rss_bytes", lambda: 128 * 1024**2)
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    assert pipeline_frontend_workers.compiled_native_auto_jobs(10) == 2
    assert pipeline_frontend_workers.compiled_native_auto_jobs(1) == 1
    # A stated budget never widens the compiled risk cap, only shrinks it.
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(64 * 1024**3))
    assert pipeline_frontend_workers.compiled_native_auto_jobs(10) == 2
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(4 * 1024**3))
    assert pipeline_frontend_workers.compiled_native_auto_jobs(10) == 1


def test_compiled_light_widths_use_their_measured_memory_class(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_frontend_workers, "_coordinator_rss_bytes", lambda: 128 * 1024**2)
    workers = pipeline_frontend_workers
    gib = 1024**3
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    assert workers.compiled_native_export_jobs(10) == 2
    assert workers.compiled_native_summary_jobs(10) == 2

    # Only bytes above the 3 GiB coordinator reserve go to 512 MiB light
    # workers: the 8 GiB production envelope admits ten. Codegen keeps its
    # independent width-two risk contract.
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * gib))
    assert workers.compiled_native_export_jobs(10) == 10
    assert workers.compiled_native_summary_jobs(10) == 10
    assert workers.compiled_native_auto_jobs(10) == 2
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(6 * gib))
    assert workers.compiled_native_export_jobs(10) == 6
    assert workers.compiled_native_summary_jobs(10) == 6
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(12 * gib))
    assert workers.compiled_native_export_jobs(10) == 10
    assert workers.compiled_native_summary_jobs(10) == 10
    assert workers.compiled_native_export_jobs(5) == 5
    assert workers.compiled_native_summary_jobs(5) == 5
    assert workers.compiled_native_auto_jobs(10) == 2

    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(3 * gib))
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one"):
        workers.compiled_native_export_jobs(10)
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one"):
        workers.compiled_native_summary_jobs(10)


def test_native_preload_width_respects_compiler_tree_budget(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_frontend_workers, "_coordinator_rss_bytes", lambda: 128 * 1024**2)
    workers = pipeline_frontend_workers
    gib = 1024**3
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * gib))
    assert workers.compiled_native_preload_jobs(6) == 2
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(10 * gib))
    assert workers.compiled_native_preload_jobs(6) == 3
    assert workers.compiled_native_preload_jobs(1) == 1


def test_budget_jobs_unifies_cpu_memory_and_risk_cap() -> None:
    workers = pipeline_frontend_workers
    host_peak = workers.HOST_SOURCE_WORKER_PEAK_BYTES
    gib = 1024**3
    # cpu-bound: plenty of memory, few cores.
    assert workers.budget_jobs(4, 64 * gib, host_peak, 10) == 4
    # memory-bound: (8 GiB - 1 GiB reserve) // 2 GiB host peak = 3.
    assert workers.budget_jobs(10, 8 * gib, host_peak, 10) == 3
    # hard risk cap binds last.
    assert (
        workers.budget_jobs(
            10, 64 * gib, workers.COMPILED_SAFE_WORKER_PEAK_BYTES, 2
        )
        == 2
    )
    # unknown budget (0) falls back to cpu within the cap.
    assert workers.budget_jobs(6, 0, host_peak, 10) == 6
    # A known budget that cannot hold one worker is an explicit rejection.
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one"):
        workers.budget_jobs(10, 1, host_peak, 10)


def test_host_auto_jobs_are_a_cpu_ceiling_before_phase_admission(monkeypatch) -> None:
    workers = pipeline_frontend_workers
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    assert workers.frontend_jobs(111, "auto", 64) == 10
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    assert workers.frontend_jobs(111, "auto", 64) == 10
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(32 * 1024**3))
    assert workers.frontend_jobs(111, "auto", 64) == 10
    # numeric override stays authoritative regardless of the budget.
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(4 * 1024**3))
    assert workers.frontend_jobs(111, "6", 64) == 6


def test_worker_modes_and_magic_are_pure_policy(tmp_path: Path):
    assert pipeline_frontend_workers.worker_timing_enabled(" YES ") is True
    assert pipeline_frontend_workers.ast_wire_enabled("on") is True
    assert pipeline_frontend_workers.worker_env_prefix(
        timing_enabled=False
    ) == "PCC_PY_FRONTEND_JOBS=1"

    script = tmp_path / "worker"
    script.write_bytes(b"#!/bin/sh\n")
    assert pipeline_frontend_workers.is_native_worker_executable(
        str(script)
    ) is False
    executable = tmp_path / "pcc1"
    executable.write_bytes(b"\xcf\xfa\xed\xfe")
    assert pipeline_frontend_workers.is_native_worker_executable(
        str(executable)
    ) is True
    assert pipeline_frontend_workers.select_native_worker_executable(
        ("python3", str(script), str(executable)),
        native_predicate=pipeline_frontend_workers.is_native_worker_executable,
    ) == str(executable)


def test_chunking_is_balanced_stable_and_native_workers_are_one_module_each(
    tmp_path: Path,
):
    sources = []
    for index, size in enumerate((100, 50, 20, 10)):
        source = tmp_path / ("m" + str(index) + ".py")
        source.write_text("x" * size, encoding="utf-8")
        sources.append(str(source))
    chunks = pipeline_frontend_workers.codegen_chunks(sources, 2)
    assert sorted(index for chunk in chunks for index in chunk) == [0, 1, 2, 3]
    assert chunks == pipeline_frontend_workers.codegen_chunks(sources, 2)

    native_worker = tmp_path / "pcc1"
    native_worker.write_bytes(b"\xcf\xfa\xed\xfe")
    assert pipeline_frontend_workers.codegen_chunk_count(
        4,
        2,
        [str(native_worker)],
        native_predicate=pipeline_frontend_workers.is_native_worker_executable,
    ) == 4
    assert pipeline_frontend_workers.codegen_chunk_count(
        4,
        2,
        ["python3"],
        native_predicate=lambda _path: False,
    ) == 4

    assert pipeline_frontend_workers.codegen_chunk_count(
        111,
        2,
        ["python3"],
        native_predicate=lambda _path: False,
    ) == 111


def test_codegen_lanes_extract_oversized_sources_largest_first(tmp_path: Path):
    sizes = (20, 220_000, 210_000, 30)
    sources = []
    for index, size in enumerate(sizes):
        source = tmp_path / ("lane" + str(index) + ".py")
        source.write_text("x" * size, encoding="utf-8")
        sources.append(str(source))

    oversized, safe = (
        pipeline_frontend_workers.split_codegen_chunks_by_source_size(
            sources,
            [[0, 1], [2, 3]],
        )
    )

    assert oversized == [[1], [2]]
    assert safe == [[0], [3]]


def test_codegen_lanes_include_large_ast_sidecars_in_memory_weight(
    tmp_path: Path,
) -> None:
    sources = []
    ast_dir = tmp_path / "ast"
    ast_dir.mkdir()
    for index in range(3):
        source = tmp_path / ("sidecar" + str(index) + ".py")
        source.write_text("x = 1\n", encoding="utf-8")
        sources.append(str(source))
        (ast_dir / ("module_" + str(index) + ".json")).write_bytes(
            b"x" * (6_100_000 if index == 1 else 20)
        )

    oversized, safe = (
        pipeline_frontend_workers.split_codegen_chunks_by_source_size(
            sources,
            [[0, 1], [2]],
            sidecar_dir=str(ast_dir),
            sidecar_threshold_bytes=6_000_000,
        )
    )

    assert oversized == [[1]]
    assert safe == [[0], [2]]


def test_codegen_lane_exports_are_available_to_native_pcc1() -> None:
    from pcc.frontends.python.codegen.layer1_support import (
        _default_native_module_exports,
    )

    exports = _default_native_module_exports(
        "pcc.frontends.python.pipeline_frontend_workers"
    )
    assert exports is not None
    worker_exports = exports["pcc.frontends.python.pipeline_frontend_workers"]
    assert "split_codegen_chunks_by_source_size" in worker_exports
    assert "compiled_native_auto_jobs" in worker_exports
    assert "compiled_native_export_jobs" in worker_exports
    assert "compiled_native_summary_jobs" in worker_exports
    assert "compiled_native_summary_plan" in worker_exports
    assert "compiled_native_worker_budget" in worker_exports
    assert worker_exports["SOURCE_WORKER_AUTO_SAFE_JOBS"]["value"] == 2
    assert worker_exports["SOURCE_WORKER_AST_OVERSIZED_BYTES"]["value"] == 6_000_000



def test_parallel_frontend_imports_lane_policy_as_static_symbols() -> None:
    """Compiled pcc1 must not require attributes on a partial module object."""
    from pcc.frontends.python import pipeline_frontend_parallel

    assert (
        pipeline_frontend_parallel._split_codegen_chunks_by_source_size
        is pipeline_frontend_workers.split_codegen_chunks_by_source_size
    )
    assert (
        pipeline_frontend_parallel._SOURCE_WORKER_AUTO_SAFE_JOBS
        == pipeline_frontend_workers.SOURCE_WORKER_AUTO_SAFE_JOBS
    )
    assert (
        pipeline_frontend_parallel._compiled_native_export_jobs
        is pipeline_frontend_workers.compiled_native_export_jobs
    )
    assert (
        pipeline_frontend_parallel._compiled_native_summary_jobs
        is pipeline_frontend_workers.compiled_native_summary_jobs
    )


def test_worker_manifest_v4_round_trips_and_rejects_truncation(tmp_path: Path):
    manifest_path = tmp_path / "worker.manifest"
    pipeline_frontend_workers.write_worker_manifest(
        str(manifest_path),
        str(tmp_path / "result"),
        str(tmp_path / "ir"),
        str(tmp_path / "exports.json"),
        str(tmp_path / "ast"),
        ["/src/a.py", "/src/b.py"],
        ["pkg.a", "pkg.b"],
        [1],
        entry_module="pkg.a",
        sibling_inits=("pkg.b",),
        libpython_mode="off",
        ir_scaffold_mode="on",
        verbose=True,
        job_kind="export",
    )
    manifest = pipeline_frontend_workers.read_worker_manifest(str(manifest_path))
    assert manifest["job_kind"] == "export"
    assert manifest["src_paths"] == ["/src/a.py", "/src/b.py"]
    assert manifest["module_names"] == ["pkg.a", "pkg.b"]
    assert manifest["assigned_indices"] == [1]
    assert manifest["sibling_inits"] == ("pkg.b",)

    manifest_path.write_text(
        pipeline_frontend_workers.WORKER_MANIFEST_V4 + "\nonly-one-field\n",
        encoding="utf-8",
    )
    with pytest.raises(
        pipeline_frontend_workers.FrontendWorkerContractError,
        match="truncated or malformed",
    ):
        pipeline_frontend_workers.read_worker_manifest(str(manifest_path))


def test_worker_ir_error_and_shell_text_contracts(tmp_path: Path):
    ir_path = tmp_path / "module.ll"
    ir_path.write_text("define i32 @f() { ret i32 0 }\n", encoding="utf-8")
    assert "define i32" in pipeline_frontend_workers.read_worker_ir(
        str(ir_path), "pkg.module"
    )
    ir_path.write_text("", encoding="utf-8")
    with pytest.raises(
        pipeline_frontend_workers.FrontendWorkerContractError,
        match="empty LLVM IR",
    ):
        pipeline_frontend_workers.read_worker_ir(str(ir_path), "pkg.module")

    error_path = tmp_path / "result"
    pipeline_frontend_workers.write_worker_error(
        str(error_path), "first\tsecond\nthird"
    )
    assert error_path.read_text(encoding="utf-8") == "ERR\tfirst second third\n"
    assert pipeline_frontend_workers.shell_quote_arg("plain/path") == "plain/path"
    assert pipeline_frontend_workers.shell_quote_arg("a'b") == "'a'\"'\"'b'"


def test_resident_driver_reserve_replaces_stale_phase_floor(monkeypatch):
    workers = pipeline_frontend_workers
    observed = 4425498624  # observed GC1 coordinator at the 8 GiB tree stop
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: observed)
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    reserve = workers._resident_coordinator_reserve(3 * 1024**3, observed)
    assert reserve == observed + workers.WORKER_COORDINATOR_RSS_HEADROOM_BYTES
    expected = (8 * 1024**3 - reserve) // workers.COMPILED_EXPORT_WORKER_PEAK_BYTES
    assert workers.compiled_native_export_jobs(10) == expected == 7
    assert workers.compiled_native_export_jobs(1) == 1


def test_budgeted_native_admission_rejects_unknown_rss(monkeypatch):
    workers = pipeline_frontend_workers
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    for unknown in (-1, 0):
        monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: unknown)
        with pytest.raises(workers.FrontendWorkerContractError, match="RSS.*unavailable"):
            workers.compiled_native_export_jobs(10)
        with pytest.raises(workers.FrontendWorkerContractError, match="RSS.*unavailable"):
            workers.compiled_native_auto_jobs(1)


def test_memory_boundary_includes_resident_reserve_and_one_full_worker():
    workers = pipeline_frontend_workers
    reserve, peak = 4 * 1024**3, 1252589568
    assert workers._phase_budget_jobs(10, reserve + peak, peak, reserve, 10) == 1
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one"):
        workers._phase_budget_jobs(10, reserve + peak - 1, peak, reserve, 10)
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one"):
        workers._phase_budget_jobs(1, reserve, peak, reserve, 10)


def test_no_budget_keeps_the_explicit_unmeasured_width_policy(monkeypatch):
    workers = pipeline_frontend_workers
    monkeypatch.delenv("PCC_WORKER_TREE_BUDGET_BYTES", raising=False)
    def do_not_sample():
        raise AssertionError("unbudgeted policy must not invent a resident budget")
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", do_not_sample)
    assert workers.compiled_native_export_jobs(10) == 2
    assert workers.compiled_native_summary_jobs(10) == 2


def test_summary_model_covers_observed_peaks_with_explicit_margin():
    import json
    samples = json.loads((Path(__file__).parents[1] / "data" / "summary_worker_memory_samples.json").read_text())
    for sample in samples["samples"]:
        reserve = pipeline_frontend_workers.summary_worker_peak_bytes(
            sample["ast_bytes"], sample["export_bytes"], sample["collector"],
        )
        required = (sample["observed_peak_bytes"] * 5 + 3) // 4 + 128 * 1024**2
        assert reserve >= required, sample


def test_summary_batches_split_before_one_batch_exceeds_available_bytes():
    workers = pipeline_frontend_workers
    sizes = [8_000_000, 8_000_000, 8_000_000]
    capacity = workers.summary_worker_peak_bytes(16_000_000, 2_381_604, 1)
    assert workers._summary_memory_chunks(sizes, 2_381_604, 1, capacity) == [[0, 1], [2]]
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one module"):
        workers._summary_memory_chunks(sizes, 2_381_604, 1,
            workers.summary_worker_peak_bytes(8_000_000, 2_381_604, 1) - 1)
    assert workers._summary_memory_chunks([1] * 17, 0, 0, -1) == [list(range(8)), list(range(8, 16)), [16]]


def test_summary_plan_uses_native_collector_projection_and_measured_driver(monkeypatch):
    workers = pipeline_frontend_workers
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 4425498624)
    monkeypatch.setattr(workers, "_worker_collector", lambda: 1)
    monkeypatch.setenv("PCC_GC_BACKEND", "0")  # must not override the sampled selector
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    batch = [3253019] * 7 + [3253021]
    chunks, jobs, receipt = workers.compiled_native_summary_plan(10, batch * 10, 2381604)
    assert chunks == [list(range(i, i + 8)) for i in range(0, 80, 8)]
    assert jobs == 2
    assert receipt["collector"] == 1
    assert receipt["coordinator_rss_bytes"] == 4425498624
    assert receipt["worker_peak_reservation_bytes"] >= (1252589568 * 5 + 3) // 4 + 128 * 1024**2
    assert receipt["pool"] == "fixed-width"


def test_summary_file_size_observation_does_not_read_payload(monkeypatch):
    from pcc.frontends.python import pipeline_frontend_parallel
    class SeekOnly:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def seek(self, offset, whence): assert (offset, whence) == (0, 2)
        def tell(self): return 2**32 + 64
        def read(self, *args): raise AssertionError("must not read input payload")
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: SeekOnly())
    assert pipeline_frontend_parallel._summary_input_size_bytes("artifact") == 2**32 + 64


def test_summary_command_environment_uses_the_admission_snapshot(monkeypatch):
    from pcc.frontends.python.worker_process_pool import _command_spec
    workers = pipeline_frontend_workers
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 128 * 1024**2)
    monkeypatch.setattr(workers, "_worker_collector", lambda: 1)
    monkeypatch.setenv("PCC_GC_BACKEND", "0")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * 1024**3))
    _chunks, _jobs, admission = workers.compiled_native_summary_plan(1, [1024], 1024)
    monkeypatch.setattr(workers, "_worker_collector", lambda: 4)
    argv, vector = _command_spec("PCC_GC_BACKEND=4 " + admission["worker_env"] + " child")
    assert argv == ["child"]
    assert dict(item.split("=", 1) for item in vector)["PCC_GC_BACKEND"] == "1"


@pytest.mark.parametrize("root_count", [0, 1, 15])
def test_small_preload_roots_do_not_reserve_a_child(monkeypatch, tmp_path, root_count):
    _assert_preload_without_spawn(monkeypatch, tmp_path, root_count, False, False)


def test_preload_without_work_directory_does_not_reserve_a_child(monkeypatch, tmp_path):
    _assert_preload_without_spawn(monkeypatch, tmp_path, 20, True, False)


def test_preload_clamped_to_one_job_does_not_reserve_a_child(monkeypatch, tmp_path):
    _assert_preload_without_spawn(monkeypatch, tmp_path, 20, False, True)


def _assert_preload_without_spawn(monkeypatch, tmp_path, root_count, no_work_dir, one_job):
    from pcc.frontends.python import type_infer
    roots = ["root" + str(i) for i in range(root_count)]
    expected = {name: {"owner": name} for name in roots}
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(4 * 1024**3))
    monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", "6")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1" if one_job else "auto")
    monkeypatch.setattr(pipeline, "_python_frontend_worker_command_prefix", lambda: ["native-worker"])
    monkeypatch.setattr(pipeline, "_is_native_worker_executable", lambda _path: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("serial preload must not observe RSS, reserve, or spawn children")
    monkeypatch.setattr(pipeline_frontend_workers, "_coordinator_rss_bytes", forbidden)
    monkeypatch.setattr(pipeline_frontend_workers, "compiled_native_preload_jobs", forbidden)
    monkeypatch.setattr(pipeline, "_preload_deltas_in_workers", forbidden)
    monkeypatch.setattr(type_infer, "preload_root_delta", lambda _exports, root, _global: expected[root])
    def build_index(_exports, root_deltas=None):
        return expected if root_deltas is None else root_deltas(roots, {})
    monkeypatch.setattr(type_infer, "build_unique_external_class_preload_index", build_index)
    actual = pipeline._build_unique_external_class_preload_index({}, "" if no_work_dir else str(tmp_path))
    assert actual == expected


def test_departing_coordinator_and_empty_safe_lane_do_not_reserve_workers(monkeypatch):
    from pcc.frontends.python import pipeline_frontend_parallel as parallel
    def forbidden(_jobs):
        raise AssertionError("no child co-resides with this owner")
    monkeypatch.setattr(parallel, "_compiled_native_auto_jobs", forbidden)
    assert parallel._codegen_safe_worker_jobs(10, True, 3, "/new/plan") == 2
    assert parallel._codegen_safe_worker_jobs(2, True, 0, "") == 2
    assert parallel._codegen_safe_worker_jobs(2, True, 0, "/new/plan") == 2
    assert parallel._codegen_safe_worker_jobs(10, False, 3, "") == 10


def test_direct_safe_workers_still_reserve_from_their_actual_owner(monkeypatch):
    from pcc.frontends.python import pipeline_frontend_parallel as parallel
    called = []
    monkeypatch.setattr(parallel, "_compiled_native_auto_jobs", lambda jobs: called.append(jobs) or 1)
    assert parallel._codegen_safe_worker_jobs(2, True, 3, "") == 1
    assert called == [2]


def test_deferred_execution_samples_driver_after_input_preparation(monkeypatch):
    from pcc.frontends.python import deferred_frontend_schedule as scheduler
    workers = pipeline_frontend_workers
    gib = 1024**3
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(8 * gib))
    monkeypatch.setenv("PCC_FRONTEND_PCO_CHAIN", "1")
    monkeypatch.setenv("PCC_GC_BACKEND", "1")
    monkeypatch.setattr(scheduler, "parallel_cpu_budget", lambda: 8)
    current = [6 * gib]  # deliberately too large departing/preparation state
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: current[0])
    def prepared(_commands, _manifests):
        current[0] = 128 * 1024**2
        return [512 * 1024**2], [0]
    def prepared_tasks(_commands):
        prepared(_commands, [])
        return [{"class": "native:frontend", "inputs": [1024], "estimate_bytes": 0,
                 "report_path": "resource.rss", "restartable": True}]
    monkeypatch.setattr(scheduler, "resource_tasks_for_commands", prepared_tasks)
    calls = []
    def launch(*args, **kwargs):
        assert current[0] == 128 * 1024**2
        calls.append(args)
    monkeypatch.setattr(scheduler, "run_resource_worker_processes", launch)
    scheduler.run_frontend_pco_commands(["compile"], ["manifest"], ["emit"], ["sidecar"], 0, 1)
    assert len(calls) == 1
    assert calls[0][-2:] == (8, 8 * gib)  # actual owner RSS is charged inside the shared pool
    assert calls[0][1][1]["depends_on"] == 0


def test_empty_deferred_work_does_not_query_memory_or_spawn(monkeypatch):
    from pcc.frontends.python import deferred_frontend_schedule as scheduler
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", "1")
    def forbidden(*args, **kwargs):
        raise AssertionError("empty work must not request a budget or spawn")
    monkeypatch.setattr(scheduler, "compiled_native_worker_budget", forbidden)
    monkeypatch.setattr(scheduler, "compiled_native_auto_jobs", forbidden)
    monkeypatch.setattr(scheduler, "run_worker_processes", forbidden)
    scheduler.run_frontend_commands([], [], 0, 0)
    scheduler.run_pco_commands([], [], 0, 0)
    scheduler.run_frontend_pco_commands([], [], [], [], 0, 0)


@pytest.mark.parametrize("root_count", [16, 20])
@pytest.mark.parametrize("frontend_jobs,expected_jobs", [("auto", 6), ("3", 3)])
def test_budgeted_preload_jobs_are_a_cpu_ceiling_for_measured_admission(
    monkeypatch, tmp_path, root_count, frontend_jobs, expected_jobs,
):
    from pcc.frontends.python import type_infer
    workers = pipeline_frontend_workers
    roots = ["root" + str(i) for i in range(root_count)]
    expected = {name: {"owner": name} for name in roots}
    observed = []
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(4 * 1024**3))
    monkeypatch.setenv("PCC_PRELOAD_DELTA_JOBS", "6")
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", frontend_jobs)
    monkeypatch.setattr(pipeline, "_python_frontend_worker_command_prefix", lambda: ["native-worker"])
    monkeypatch.setattr(pipeline, "_is_native_worker_executable", lambda _path: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("budgeted preload must measure the spawning owner after preparing inputs")
    monkeypatch.setattr(workers, "compiled_native_preload_jobs", forbidden)
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", forbidden)
    def spawn(exports, assigned_roots, prefix, jobs, work_dir):
        observed.append((assigned_roots, prefix, jobs, work_dir))
        return expected
    monkeypatch.setattr(pipeline, "_preload_deltas_in_workers", spawn)
    monkeypatch.setattr(type_infer, "preload_root_delta", forbidden)
    monkeypatch.setattr(type_infer, "build_unique_external_class_preload_index",
                        lambda _exports, root_deltas=None: root_deltas(roots, {}))
    assert pipeline._build_unique_external_class_preload_index({}, str(tmp_path)) == expected
    assert observed == [(roots, ["native-worker"], expected_jobs, str(tmp_path))]


def test_worker_only_summary_still_rejects_insufficient_memory(monkeypatch):
    workers = pipeline_frontend_workers
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(3 * 1024**3))
    monkeypatch.setattr(workers, "_coordinator_rss_bytes", lambda: 128 * 1024**2)
    monkeypatch.setattr(workers, "_worker_collector", lambda: 1)
    with pytest.raises(workers.FrontendWorkerContractError, match="cannot admit one module"):
        workers.compiled_native_summary_plan(10, [1024], 1024)
