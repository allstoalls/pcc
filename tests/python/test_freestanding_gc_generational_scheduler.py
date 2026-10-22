from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_generational_scheduler.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
BARRIER_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_barrier_dispatcher.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend3_frame_root_scan_reset_locked",
    "pcc_gc_backend3_scheduler_root_scan_reset_locked",
    "pcc_gc_backend4_step_generation_aging",
    "pcc_gc_backend4_step_remembered_roots",
    "pcc_gc_generational_promote_frame_roots",
    "pcc_gc_generational_promote_scheduler_roots",
    "pcc_gc_generational_promote_extension_module_state_root",
    "pcc_gc_generational_promote_tls_exception_root",
    "pcc_gc_generational_step",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    # The managed runtime's builtin type root table (slots past the 65 exception
    # classes are scanned from it).
    "pcc_builtin_type_root_slots",
    "pcc_capi_visit_extension_module_state_roots",
    "pcc_gc_backend3_continuation_root_scan_cursor",
    # The remembered-roots drain attributes medium nodes another thread
    # enqueued (983254526).
    "pcc_current_thread_id",
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries_count",
    "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes_count",
    "pcc_gc_backend4_store_buffer_drain_batches_count",
    "pcc_gc_backend4_store_buffer_drained_entries_count",
    "pcc_gc_backend4_store_buffer_entries_count",
    "pcc_gc_backend4_store_buffer_full_batches_count",
    "pcc_gc_backend4_store_buffer_head",
    "pcc_gc_backend4_store_buffer_incomplete_drains_count",
    "pcc_gc_backend4_store_buffer_max_batch_size_count",
    "pcc_gc_backend4_store_buffer_medium_count",
    "pcc_gc_backend4_store_buffer_medium_flushed_entries_count",
    "pcc_gc_backend4_store_buffer_medium_flushes_count",
    "pcc_gc_backend4_store_buffer_medium_full_flushes_count",
    "pcc_gc_backend4_store_buffer_medium_head",
    "pcc_gc_backend4_young_promotions",
    "pcc_gc_backend3_frame_root_scan_cursor",
    "pcc_gc_backend3_frame_root_scan_phase",
    "pcc_gc_backend3_frame_root_scan_slot",
    "pcc_gc_backend3_drain_remembered_owners",
    "pcc_gc_backend3_drain_promotion_worklist",
    "pcc_gc_backend3_finish_detached_remembered_owners",
    "pcc_gc_backend3_scheduler_root_scan_cursor",
    "pcc_gc_backend3_scheduler_root_scan_phase",
    "pcc_gc_backend3_scheduler_root_scan_slot",
    "pcc_gc_backend3_young_link_head",
    "pcc_gc_backend3_young_list_head",
    "pcc_gc_backend3_young_unlink",
    "pcc_gc_forwarding_find",
    "pcc_gc_continuation_root_head",
    "pcc_gc_frame_head",
    "pcc_gc_generational_oldify_copy",
    "pcc_gc_generational_promote_owned_slot_mode",
    "pcc_gc_generational_promote_young_if_known",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_object_node_is_active",
    "pcc_gc_root_registry_revision",
    "pcc_gc_scheduler_root_head",
    "pcc_gc_store_buffer_member_forget",
    "pcc_gc_trace_referents_for_promotion",
    "pcc_gc_visit_mapped_root_slot",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_thread_safepoint",
    "py_decref",
    "py_incref",
    "py_tls_exc_get",
    "py_tls_exc_set",
    "py_subs_exc_cache_slot",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _export_body(source: str, symbol: str) -> str:
    return source.split(f'@c_abi_export("{symbol}")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]


def test_generational_scheduler_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    barrier = BARRIER_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_generational_scheduler" in makefile
    assert "def _promote_frame_roots(" not in managed
    assert "def _promote_tls_exception_root(" not in managed
    assert "def _step_generational_promotion(" not in managed
    assert '_step_generational_promotion = extern(' in managed
    assert "_step_generational_promotion(1024, 0)" in managed
    assert "pcc_gc_generational_step(budget, 1)" in barrier


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_generational_scheduler_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("generational_scheduler_" + emitter + ".ll")
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

        source = tmp_path / "generational_scheduler.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("generational_scheduler_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

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
    assert undefined == RAW_FUNCTION_IMPORTS

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


def test_generational_scheduler_preserves_root_order_budget_and_retry_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    tls = _export_body(strict, "pcc_gc_generational_promote_tls_exception_root")
    step = _export_body(strict, "pcc_gc_generational_step")

    assert tls.index("py_incref(oldified)") < tls.index("py_tls_exc_set(oldified)")
    assert "py_decref(current)" not in tls
    assert tls.index("py_tls_exc_set(oldified)") < tls.index(
        "store_ptr(cleanup_out, 0, current)"
    )
    assert "pcc_gc_generational_promote_young_if_known(current)" in tls

    assert step.index("pcc_gc_generational_promote_frame_roots") < step.index(
        "pcc_gc_generational_promote_scheduler_roots(batch_budget)"
    )
    assert step.index("pcc_gc_generational_promote_scheduler_roots") < step.index(
        "pcc_py_gc_minor_graph_lock()"
    )
    assert step.index("pcc_py_gc_minor_graph_lock()") < step.index(
        "pcc_gc_generational_promote_tls_exception_root(tls_cleanup)"
    )
    assert step.index("pcc_gc_generational_promote_tls_exception_root") < step.index(
        "pcc_gc_backend3_drain_remembered_owners"
    )
    assert "local_processed < batch_budget" in step
    assert "pcc_gc_backend3_young_unlink(node)" in step
    assert "pcc_gc_backend3_young_link_head(node)" in step
    assert "break" in step
    assert step.rindex("pcc_py_gc_minor_graph_unlock()") < step.rindex(
        "return local_processed"
    )


def test_generational_registered_root_walks_bound_each_graph_lock_tenure() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_frame = _export_body(
        strict, "pcc_gc_generational_promote_frame_roots"
    )
    strict_scheduler = _export_body(
        strict, "pcc_gc_generational_promote_scheduler_roots"
    )
    strict_step = _export_body(strict, "pcc_gc_generational_step")

    for body in (strict_frame, strict_scheduler):
        assert "while examined < remaining_budget:" in body
        assert body.index("pcc_py_gc_minor_graph_lock()") < body.index(
            "while examined < remaining_budget:"
        )
        assert body.index("while examined < remaining_budget:") < body.index(
            "pcc_py_gc_minor_graph_unlock()"
        )
        assert "pcc_gc_root_registry_revision" in body

    strict_first_lock = strict_step.index("pcc_py_gc_minor_graph_lock()")
    assert strict_step.index(
        "pcc_gc_generational_promote_frame_roots(batch_budget)"
    ) < strict_first_lock
    assert strict_step.index(
        "pcc_gc_generational_promote_scheduler_roots(batch_budget)"
    ) < strict_first_lock

    state = (RUNTIME_DIR / "py" / "freestanding_gc_state.py").read_text(
        encoding="utf-8"
    )
    root_registry = (
        RUNTIME_DIR / "py" / "freestanding_gc_root_registry.py"
    ).read_text(encoding="utf-8")
    frame_registry = (
        RUNTIME_DIR / "py" / "freestanding_gc_frame_registry.py"
    ).read_text(encoding="utf-8")
    assert 'define_global_i64("pcc_gc_root_registry_revision", 0)' in state
    assert "pcc_gc_root_registry_note_mutation_locked()" in root_registry
    assert "pcc_gc_root_registry_note_mutation_locked()" in frame_registry
    strict_retarget = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
    ).read_text(encoding="utf-8").split(
        "def _retarget_continuation_root_slots", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_root_registry_note_mutation_locked()" in strict_retarget


def test_tls_exception_oldification_cleanup_finishes_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    strict_tls = _export_body(
        strict, "pcc_gc_generational_promote_tls_exception_root"
    )
    strict_step = _export_body(strict, "pcc_gc_generational_step")
    assert "cleanup_out: c_ptr" in strict_tls
    assert "py_decref(" not in strict_tls
    assert strict_tls.index("py_tls_exc_set(oldified)") < strict_tls.index(
        "store_ptr(cleanup_out, 0, current)"
    )
    strict_unlock = strict_step.rindex("pcc_py_gc_minor_graph_unlock()")
    strict_finish = strict_step.index(
        "pcc_gc_backend3_finish_detached_remembered_owners", strict_unlock
    )
    strict_decref = strict_step.index("py_decref(tls_cleanup_value)")
    assert strict_unlock < strict_finish < strict_decref


def test_extension_module_traverse_runs_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_capi_visit_extension_module_state_roots"
    ] == (("c_ptr", "c_ptr"), "c_void")

    strict_callback = _export_body(
        strict, "pcc_gc_generational_promote_extension_module_state_root"
    )
    strict_step = _export_body(strict, "pcc_gc_generational_step")
    assert strict_callback.index("pcc_py_gc_minor_graph_lock()") < (
        strict_callback.index("pcc_gc_generational_promote_young_if_known(root)")
    )
    assert strict_callback.index("pcc_gc_generational_promote_young_if_known(root)") < (
        strict_callback.index("pcc_py_gc_minor_graph_unlock()")
    )
    assert strict_step.rindex("pcc_py_gc_minor_graph_unlock()") < strict_step.index(
        "pcc_capi_visit_extension_module_state_roots("
    )


def test_production_archive_has_one_generational_scheduler_owner(
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
        assert ":freestanding_gc_generational_scheduler.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
