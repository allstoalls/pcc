from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_collector.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_obj_gc.py"
MAKEFILE = RUNTIME_DIR / "Makefile"
OWNED_SYMBOLS = {
    "pcc_gc_backend0_dealloc_unreachable",
    "pcc_gc_backend0_mapped_root_count",
    "pcc_gc_backend0_mark_root_slot",
    "pcc_gc_backend0_mark_root_slots",
    "pcc_gc_backend0_mark_runtime_roots",
    "pcc_gc_backend0_maybe_finalize_unreachable",
    "pcc_gc_backend0_recompute_reachability",
    "pcc_gc_backend0_visit_mapped_root_slots",
    "pcc_gc_backend0_visit_scheduler_root_slots",
    "py_gc_collect",
}
RAW_IMPORTS = {
    "free",
    "malloc",
    "pcc_capi_dealloc_cext_object",
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_backend0_clear_referents",
    "pcc_gc_backend0_mark_reachable",
    "pcc_gc_backend0_visit_subtract",
    "pcc_gc_continuation_root_head",
    "pcc_gc_default_drain_deferred_nodes",
    "pcc_gc_default_table_lock",
    "pcc_gc_default_table_unlock",
    "pcc_gc_default_unlink_tracked_node",
    "pcc_gc_frame_head",
    "pcc_gc_load_ptr",
    "pcc_gc_note_object_freeing",
    "pcc_gc_root_count",
    "pcc_gc_root_slots",
    "pcc_gc_scheduler_root_head",
    "pcc_gc_trace_continuation_roots",
    # Every mutator's mapped stack frames (a13c5b4a1).
    "pcc_gc_visit_walked_stack_roots",
    "pcc_resume_world",
    "pcc_stop_the_world",
    "pcc_thread_safepoint",
    "py_class_dealloc",
    "py_descriptor_dealloc",
    "py_dealloc_continuation",
    "py_dealloc_coroutine",
    "py_dealloc_dict",
    "py_dealloc_exc",
    "py_dealloc_float",
    "py_dealloc_func",
    "py_dealloc_gen",
    "py_dealloc_generic",
    "py_dealloc_int",
    "py_dealloc_iter",
    "py_dealloc_list",
    "py_dealloc_memoryview",
    "py_dealloc_set",
    "py_dealloc_str",
    "py_dealloc_task",
    "py_dealloc_tuple",
    "py_dealloc_virtual_thread",
    "py_dealloc_vthread_channel",
    "py_dealloc_weakref",
    "py_gc_collecting",
    "py_gc_head",
    "py_gc_index_remove",
    "py_gc_tracked_count",
    "py_instance_dealloc",
    "py_user_del_dispatch",
    "py_weakref_invalidate",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_backend0_collector_" + emitter + ".ll")
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

        source = tmp_path / "freestanding_gc_backend0_collector.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_backend0_collector_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_backend0_collector_has_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert "py_gc_collect" not in _exported_symbols(managed)
    assert "freestanding_gc_backend0_collector" in makefile

    # High-level inspection remains managed; raw collection does not.
    assert _exported_symbols(managed) == {
        "py_gc_get_objects",
        "py_gc_get_referents",
        "py_gc_get_referrers",
    }
    assert "py_list_new = extern(" in managed
    assert "py_list_new = extern(" not in strict
    assert "py_list_append = extern(" not in strict


def test_backend0_collector_preserves_ordering_and_graph_contract():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    collect = strict.split('@c_abi_export("py_gc_collect")', 1)[1]

    assert collect.index("pcc_stop_the_world()") < collect.index(
        "pcc_gc_default_table_lock()"
    )
    assert collect.index("pcc_gc_default_table_lock()") < collect.index(
        'store_i32(collecting_slot, 0, 1)'
    )
    assert "pcc_gc_backend0_visit_subtract(" in strict
    assert "pcc_gc_backend0_mark_reachable(" in strict
    assert "pcc_gc_backend0_clear_referents(obj)" in collect
    assert collect.index("_maybe_finalize_unreachable(") < collect.index(
        "py_weakref_invalidate(obj)"
    )
    assert collect.index("py_weakref_invalidate(obj)") < collect.index(
        "pcc_gc_backend0_clear_referents(obj)"
    )
    assert collect.index("pcc_gc_default_drain_deferred_nodes()") < collect.index(
        'store_i32(collecting_slot, 0, 0)'
    )
    assert collect.index('store_i32(collecting_slot, 0, 0)') < collect.index(
        "pcc_gc_default_table_unlock()"
    )
    assert collect.index("pcc_gc_default_table_unlock()") < collect.index(
        "pcc_resume_world()"
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_backend0_collector_compiles_as_strict_object(tmp_path: Path, emitter: str):
    obj = _compile_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    assert {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    } == RAW_IMPORTS

    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    owners = [
        line
        for line in symbols.stdout.splitlines()
        if line.strip()
        and line.split()[-1].lstrip("_") == "py_gc_collect"
        and " U " not in line
    ]
    assert len(owners) == 1, owners
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == OWNED_SYMBOLS


def test_production_archive_uniquely_owns_backend0_collector(
    pcc_runtime_archive: Path,
):
    symbols = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    owners = [
        line
        for line in symbols.stdout.splitlines()
        if line.strip()
        and line.split()[-1].lstrip("_") == "py_gc_collect"
        and " U " not in line
    ]
    assert len(owners) == 1, owners
    assert ":freestanding_gc_backend0_collector.o:" in owners[0]
    assert ":py_obj_gc.o:" not in owners[0]
