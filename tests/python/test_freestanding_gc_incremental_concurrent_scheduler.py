from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_incremental_concurrent_scheduler.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
DISPATCHER_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_barrier_dispatcher.py"
)
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_cms_note_alloc",
    "pcc_gc_cms_stop_worker",
    "pcc_gc_complete_claimed_tracing_cycle",
    "pcc_gc_complete_mark_cycle_seed",
    "pcc_gc_incremental_concurrent_step",
    "pcc_gc_drain_all_gray_stopped_world_py",
    "pcc_gc_incremental_maybe_auto_step",
    "pcc_gc_note_alloc",
    "pcc_gc_record_explicit_pause",
    "pcc_gc_tracing_budget_from_debt",
    "pcc_gc_tracing_debt_threshold",
    "pcc_gc_tracing_discharge_debt",
    "pcc_gc_tracing_gray_final_extension_root",
    "pcc_gc_tracing_gray_extension_root",
    "pcc_gc_tracing_record_pause",
    "pcc_gc_tracing_step_cycle",
    "pcc_gc_trace_cext_complete_context",
    # GC4's automatic step lives here as well.
    "pcc_gc_backend4_maybe_auto_step",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_is_cms_worker",
    # pcc_gc_backend4_maybe_auto_step: refused inside no-park or graph-lock
    # scopes, then a GC4 step.
    "pcc_gc_step",
    "pcc_py_gc_minor_graph_lock_depth",
    "pcc_thread_no_park_depth",
    "pcc_gc_set_mark_color",
    "pcc_capi_is_cext_type_tag",
    "pcc_capi_visit_extension_module_state_roots",
    "pcc_gc_begin_mark_cycle",
    "pcc_gc_config_ensure",
    "pcc_gc_finish_tracing_cycle",
    "pcc_gc_drain_all_gray_locked_slice",
    "pcc_gc_gray_current_roots",
    "pcc_gc_gray_count_decrement_acq_rel",
    "pcc_gc_gray_count_load_acquire",
    "pcc_gc_maybe_start_cms_worker",
    "pcc_gc_seed_roots",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_tracing_finish_claim_clear_unlocked",
    "pcc_gc_trace_mark_gray_if_known",
    "pcc_gc_trace_cext_referents_unlocked",
    "pcc_gc_trace_referents",
    "pcc_platform_monotonic_us",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_resume_world",
    "pcc_stop_the_world",
    "pcc_thread_owns_stopped_world",
    "pcc_threads_enabled",
    "pcc_thread_join",
    "py_decref",
    "py_incref",
}
RAW_GLOBAL_IMPORTS = {
    # pcc_gc_backend4_maybe_auto_step's admission and reseed ownership.
    "pcc_gc_backend4_auto_step_pending",
    "pcc_gc_backend4_relocation_reset_owner",
    "pcc_gc_backend4_remap_active",
    "pcc_gc_backend4_reseed_commit_owner",
    "pcc_gc_backend4_reseed_page_count_owner",
    "pcc_gc_explicit_collect_active",
    # Mutator assists pause while gc.disable() or threshold0 == 0 holds, and
    # one that completes the mark has the next gate sweep it.
    "py_gc_enabled",
    "py_gc_threshold0",
    "pcc_gc_tracing_finish_commits",
    "pcc_gc_backend_selected",
    "pcc_gc_config_initialized",
    "pcc_gc_cms_mutator_assists",
    "pcc_gc_cms_queue_pushes",
    "pcc_gc_cms_worker_started",
    "pcc_gc_cms_worker_handle",
    "pcc_gc_cms_worker_stop_requested",
    "pcc_gc_cycle_requested",
    "pcc_gc_debt_bytes",
    "pcc_gc_debt_threshold_override",
    "pcc_gc_in_auto_step",
    "pcc_gc_mark_active",
    "pcc_gc_metric_alloc",
    "pcc_gc_metric_max_pause_us",
    "pcc_gc_metric_pause_count",
    "pcc_gc_metric_pause_hist0",
    "pcc_gc_metric_pause_hist1",
    "pcc_gc_metric_pause_hist2",
    "pcc_gc_metric_pause_hist3",
    "pcc_gc_metric_pause_sum_us",
    "pcc_gc_metric_step",
    "pcc_gc_object_head",
    "pcc_gc_pause",
    "pcc_gc_stepmul",
    "pcc_gc_trace_cursor",
    "pcc_gc_trace_cext_pending_backend",
    "pcc_gc_trace_cext_pending_epoch",
    "pcc_gc_trace_cext_pending_obj",
    "pcc_gc_trace_extension_roots_backend",
    "pcc_gc_trace_extension_roots_epoch",
    "pcc_gc_trace_extension_roots_pending",
    "pcc_gc_tracing_cycle_epoch",
    # The pacing threshold applies the pause to the heap marked.
    "pcc_gc_tracing_marked_bytes",
    "pcc_gc_tracing_finish_claim_backend",
    "pcc_gc_tracing_finish_claim_epoch",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _literal_global_imports() -> set[str]:
    globals_: set[str] = set()
    tree = ast.parse(STRICT_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"global_addr", "global_load_ptr", "global_store_ptr"}:
            continue
        if not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            globals_.add(value.value)
    return globals_


def _export_body(source: str, symbol: str) -> str:
    return source.split(f'@c_abi_export("{symbol}")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]


def test_incremental_concurrent_scheduler_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_incremental_concurrent_scheduler" in makefile
    for old_name in (
        "_debt_threshold",
        "_budget_from_debt",
        "_discharge_debt",
        "_record_pause",
        "_maybe_auto_step",
        "_stop_cms_worker",
        "_note_cms_alloc",
        "_step_tracing",
    ):
        assert f"def {old_name}(" not in managed
    assert '_step_tracing = extern("pcc_gc_tracing_step_cycle"' in managed
    assert "_step_incremental_concurrent = extern(" in managed
    assert '"pcc_gc_incremental_concurrent_step", (c_int64,), c_int64' in managed


def test_initial_trace_extension_traversal_runs_outside_graph_lock_source() -> None:

    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_step = _export_body(strict, "pcc_gc_tracing_step_cycle")
    assert strict_step.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_step.index("pcc_capi_visit_extension_module_state_roots(")
    )
    assert "pcc_gc_trace_extension_roots_pending" in strict_step
    assert (
        '@c_abi_export("pcc_gc_tracing_gray_extension_root")' in strict
    )


def test_final_trace_extension_traversal_precedes_locked_cut_source() -> None:

    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_complete = _export_body(
        strict, "pcc_gc_complete_claimed_tracing_cycle"
    )
    assert strict_complete.index("pcc_py_gc_minor_graph_unlock()") < (
        strict_complete.index("pcc_capi_visit_extension_module_state_roots(")
    )
    assert strict_complete.index(
        "pcc_capi_visit_extension_module_state_roots("
    ) < strict_complete.index("pcc_gc_finish_tracing_cycle(")
    assert (
        '@c_abi_export("pcc_gc_tracing_gray_final_extension_root")'
        in strict
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_incremental_concurrent_scheduler_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("scheduler_" + emitter + ".ll")
    pipeline.compile_python(
        str(STRICT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "scheduler.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("scheduler_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _literal_global_imports() == RAW_GLOBAL_IMPORTS
    assert RAW_GLOBAL_IMPORTS <= FREESTANDING_GC_RUNTIME_GLOBALS

    undefined_result = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined_result.returncode == 0, (
        undefined_result.stdout + undefined_result.stderr
    )
    undefined = {
        line.split()[-1].lstrip("_")
        for line in undefined_result.stdout.splitlines()
        if line.strip()
    }
    assert undefined == RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == OWNED_SYMBOLS


def test_incremental_concurrent_scheduler_preserves_bounded_policy_order() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    assert "pcc_gc_tracing_sweep_unreachable" not in strict
    assert "pcc_gc_collect_tracing" not in strict

    step = _export_body(strict, "pcc_gc_incremental_concurrent_step")
    assert step.index('global_addr("pcc_gc_metric_step")') < step.index(
        "pcc_gc_tracing_step_cycle(budget)"
    )
    assert step.index("pcc_gc_tracing_step_cycle(budget)") < step.index(
        "pcc_gc_tracing_discharge_debt(processed)"
    )
    assert step.index("pcc_gc_tracing_discharge_debt(processed)") < step.index(
        'store_i64(global_addr("pcc_gc_debt_bytes"), 0, 0)'
    )
    assert step.index('store_i64(global_addr("pcc_gc_debt_bytes"), 0, 0)') < (
        step.index("pcc_gc_tracing_record_pause(")
    )

    # GC1 and GC2 hand allocation debt to the collector thread (d58306406):
    # an allocating mutator no longer runs GC1's automatic step itself. GC4
    # records the debt atomically and flags its automatic step.
    note_alloc = _export_body(strict, "pcc_gc_note_alloc")
    assert note_alloc.index('global_addr("pcc_gc_metric_alloc")') < note_alloc.index(
        "if backend == 1 or backend == 2:"
    )
    assert note_alloc.index("pcc_gc_maybe_start_cms_worker()") < note_alloc.index(
        "pcc_gc_cms_note_alloc(bytes)"
    )
    assert "pcc_gc_incremental_maybe_auto_step()" not in note_alloc
    assert note_alloc.index('global_addr("pcc_gc_debt_bytes")') < note_alloc.index(
        'global_addr("pcc_gc_backend4_auto_step_pending")'
    )

    dispatch = DISPATCHER_SOURCE.read_text(encoding="utf-8")
    public_step = dispatch.split('@c_abi_export("pcc_gc_step")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    assert public_step.index("if backend == 1 or backend == 2:") < (
        public_step.index('global_addr("pcc_gc_metric_step")')
    )
    assert "return pcc_gc_incremental_concurrent_step(budget)" in public_step
    assert "processed = processed + pcc_gc_tracing_step_cycle" in public_step


def test_tracing_finisher_claim_stops_only_after_graph_unlock() -> None:
    source = STRICT_SOURCE.read_text(encoding="utf-8")
    complete = _export_body(source, "pcc_gc_complete_claimed_tracing_cycle")
    complete_signature = complete.split(") -> i64:", 1)[0]
    assert "claim_epoch: i64" in complete_signature
    assert "claim_backend: i64" in complete_signature
    assert complete.index("pcc_thread_owns_stopped_world()") < complete.index(
        "if owns_stopped_world == 0:"
    ) < complete.index("pcc_stop_the_world()")
    stop_failure = complete.split("if pcc_stop_the_world() != 0:", 1)[1].split(
        "acquired_stopped_world = 1", 1
    )[0]
    assert stop_failure.index("pcc_py_gc_minor_graph_lock()") < (
        stop_failure.index("pcc_gc_tracing_finish_claim_clear_unlocked(")
    ) < stop_failure.index("pcc_py_gc_minor_graph_unlock()") < (
        stop_failure.index("return 0")
    )
    for forbidden in (
        "pcc_gc_finish_tracing_cycle",
        "pcc_resume_world",
        "pcc_gc_mark_active",
        "pcc_gc_trace_cursor",
        "pcc_gc_gray_count",
    ):
        assert forbidden not in stop_failure
    finish_call = complete.index("pcc_gc_finish_tracing_cycle(")
    assert "claim_epoch, claim_backend" in complete[finish_call : finish_call + 130]
    assert complete.index("acquired_stopped_world = 1") < complete.index(
        "pcc_py_gc_minor_graph_lock()",
        complete.index("acquired_stopped_world = 1"),
    ) < finish_call < complete.index(
        "pcc_py_gc_minor_graph_unlock()",
        finish_call,
    ) < complete.index("pcc_resume_world()")

    step = _export_body(source, "pcc_gc_tracing_step_cycle")
    assert "pcc_stop_the_world" not in step
    assert "pcc_resume_world" not in step
    assert step.index(
        'global_addr("pcc_gc_tracing_finish_claim_backend")'
    ) < step.index(
        'global_addr("pcc_gc_tracing_finish_claim_epoch")',
        step.index('global_addr("pcc_gc_tracing_finish_claim_backend")'),
    )
    assert step.index("pcc_py_gc_minor_graph_unlock()", step.rindex("claim_epoch =")) < (
        step.index(
            "pcc_gc_complete_claimed_tracing_cycle(claim_epoch, claim_backend)"
        )
    )


def test_production_archive_has_one_incremental_concurrent_scheduler_owner(
    pcc_runtime_archive: Path,
) -> None:
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_incremental_concurrent_scheduler.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
