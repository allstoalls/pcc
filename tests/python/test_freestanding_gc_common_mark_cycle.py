from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_common_mark_cycle.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_begin_mark_cycle",
    "pcc_gc_drain_all_gray_locked_slice",
    "pcc_gc_drain_all_gray_unlocked",
    "pcc_gc_finish_tracing_cycle",
    "pcc_gc_seed_roots",
    "pcc_gc_tracing_cycle_epoch_advance_unlocked",
    "pcc_gc_tracing_finish_claim_clear_unlocked",
    "pcc_gc_trace_mark_gray_if_known",
    "pcc_gc_trace_cext_referents_unlocked",
    "pcc_gc_trace_cext_slot_transaction",
    "pcc_gc_trace_referents",
    "pcc_gc_trace_slot",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_set_mark_color",
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_gray_count_decrement_acq_rel",
    "pcc_gc_gray_count_store_release",
    "pcc_gc_gray_count_increment_acq_rel",
    "pcc_gc_gray_current_roots",
    "pcc_gc_gray_refcount_external_roots",
    "pcc_gc_load_ptr",
    "pcc_gc_forwarding_index_find",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_object_node_is_active",
    "pcc_gc_prepare_object_list_mark",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_gc_visit_object_slots",
    "pcc_platform_abort",
    "py_incref",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend_selected",
    "pcc_gc_explicit_collect_active",
    "pcc_gc_mark_active",
    "pcc_gc_object_head",
    "pcc_gc_trace_cursor",
    "pcc_gc_trace_cext_pending_backend",
    "pcc_gc_trace_cext_pending_epoch",
    "pcc_gc_trace_cext_pending_obj",
    "pcc_gc_trace_extension_roots_backend",
    "pcc_gc_trace_extension_roots_epoch",
    "pcc_gc_trace_extension_roots_pending",
    "pcc_gc_tracing_cycle_epoch",
    "pcc_gc_tracing_finish_claim_backend",
    "pcc_gc_tracing_finish_claim_epoch",
    "pcc_gc_tracing_finish_commits",
    # A completed mark records the bytes it found reachable for pacing and
    # pays the allocation debt.
    "pcc_gc_tracing_marked_bytes",
    "pcc_gc_debt_bytes",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_common_mark_cycle_" + emitter + ".ll")
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

        source = tmp_path / "freestanding_gc_common_mark_cycle.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_common_mark_cycle_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_common_mark_cycle_has_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_common_mark_cycle" in makefile

    for name in (
        "_trace_referents",
        "_seed_roots",
        "_drain_all_gray_unlocked",
        "_begin_mark_cycle",
        "_finish_tracing_cycle",
    ):
        assert f"{name} = extern(" in managed
        assert f"def {name}(" not in managed

    assert "def _py_obj_visit_trace_slot(" not in managed
    assert "pcc_gc_visit_object_slots" in strict


def test_common_mark_cycle_preserves_root_and_termination_order():
    source = STRICT_SOURCE.read_text(encoding="utf-8")
    seed = source.split('@c_abi_export("pcc_gc_seed_roots")', 1)[1]
    seed = seed.split('@c_abi_export("pcc_gc_drain_all_gray_unlocked")', 1)[0]
    assert seed.index("pcc_gc_prepare_object_list_mark(") < seed.index(
        "pcc_gc_gray_refcount_external_roots()"
    )
    assert seed.index("pcc_gc_gray_refcount_external_roots()") < seed.index(
        "pcc_gc_gray_current_roots()"
    )

    finish = source.split('@c_abi_export("pcc_gc_finish_tracing_cycle")', 1)[1]
    assert "pcc_stop_the_world" not in finish
    assert "pcc_resume_world" not in finish
    assert "pcc_thread_safepoint" not in finish
    assert finish.index('global_addr("pcc_gc_tracing_finish_claim_epoch")') < (
        finish.index('global_addr("pcc_gc_tracing_cycle_epoch")')
    )
    assert finish.index('global_addr("pcc_gc_tracing_cycle_epoch")') < (
        finish.index('global_addr("pcc_gc_backend_selected")')
    ) < finish.index('global_addr("pcc_gc_mark_active")')
    assert "pcc_gc_gray_current_roots()" not in finish
    assert "pcc_gc_drain_all_gray" not in finish
    assert finish.index("flags | 1024") < finish.index(
        'global_store_ptr("pcc_gc_trace_cursor", null())'
    ) < finish.index('global_addr("pcc_gc_mark_active")', finish.index("flags | 1024"))
    assert 'store_i32(global_addr("pcc_gc_cycle_requested"), 0, 0)' not in finish
    assert finish.index('global_addr("pcc_gc_tracing_finish_commits")') < (
        finish.rindex("pcc_gc_tracing_finish_claim_clear_unlocked(")
    )


def test_referent_trace_does_not_regray_black_objects():
    source = STRICT_SOURCE.read_text(encoding="utf-8")
    mark = source.split(
        '@c_abi_export("pcc_gc_trace_mark_gray_if_known")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    trace_slot = source.split('@c_abi_export("pcc_gc_trace_slot")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]

    assert "if (flags & 32) == 0:" in mark
    black_guard = mark.split("if (flags & 32) == 0:", 1)[1]
    assert "pcc_gc_gray_count_increment_acq_rel()" in black_guard
    assert "pcc_gc_set_mark_color(obj, 16)" in black_guard
    assert "pcc_gc_trace_mark_gray_if_known(child)" in trace_slot
    assert "pcc_gc_mark_root_gray_if_known(child)" not in trace_slot


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_common_mark_cycle_object_has_exact_raw_closure(
    tmp_path: Path, emitter: str
):
    obj = _compile_object(tmp_path, emitter)
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


def test_production_archive_has_one_common_mark_cycle_owner(
    pcc_runtime_archive: Path,
):
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
        assert ":freestanding_gc_common_mark_cycle.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
