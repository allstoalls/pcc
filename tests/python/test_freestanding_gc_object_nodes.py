from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
    FREESTANDING_GC_RUNTIME_GLOBALS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend3_young_link_head",
    "pcc_gc_backend3_young_list_head",
    "pcc_gc_backend3_young_rebuild",
    "pcc_gc_backend3_young_set_head",
    "pcc_gc_backend3_young_unlink",
    "pcc_gc_backend3_promotion_unlink",
    "pcc_gc_live_bytes_subtract",
    "pcc_gc_object_known_size",
    "pcc_gc_object_list_head",
    "pcc_gc_object_node_alloc",
    "pcc_gc_object_node_clear_promotion_state",
    "pcc_gc_object_node_freeing",
    "pcc_gc_object_node_finish_detached",
    "pcc_gc_object_node_gc_refs",
    "pcc_gc_object_node_minor_block",
    "pcc_gc_object_node_next",
    "pcc_gc_object_node_plan_requires_prepare",
    "pcc_gc_object_node_prepare",
    "pcc_gc_object_node_prev",
    "pcc_gc_object_node_release",
    "pcc_gc_object_node_set_freeing",
    "pcc_gc_object_node_set_gc_refs",
    "pcc_gc_object_node_set_next",
    "pcc_gc_object_node_set_prev",
    "pcc_gc_object_node_set_young_next",
    "pcc_gc_object_node_set_young_prev",
    "pcc_gc_object_node_set_zpage",
    "pcc_gc_object_node_size",
    "pcc_gc_object_node_take_prepared",
    "pcc_gc_object_node_unlink",
    "pcc_gc_object_node_young_next",
    "pcc_gc_object_node_young_prev",
    "pcc_gc_object_node_zpage",
    "pcc_gc_object_set_list_head",
    "pcc_gc_trace_cursor_load",
    "pcc_gc_trace_cursor_store",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_platform_abort",
    "free",
    "malloc",
    "pcc_gc_object_index_find",
    "pcc_gc_object_node_is_active",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend3_remembered_scan_cursor",
    "pcc_gc_backend3_promotion_head",
    "pcc_gc_backend3_promotion_tail",
    "pcc_gc_backend3_young_head",
    "pcc_gc_live_bytes",
    "pcc_gc_object_head",
    "pcc_gc_object_list_revision",
    "pcc_gc_object_node_free_count",
    "pcc_gc_object_node_free_head",
    "pcc_gc_backend4_reset_object_cursor",
    "pcc_gc_trace_cursor",
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


def test_object_nodes_have_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_object_nodes" in makefile
    for old_name in (
        "_object_head",
        "_set_object_head",
        "_trace_cursor",
        "_set_trace_cursor",
        "_backend3_young_head",
        "_set_backend3_young_head",
        "_object_node_size",
        "_object_node_next",
        "_set_object_node_next",
        "_object_node_minor_block",
        "_object_node_freeing",
        "_set_object_node_freeing",
        "_object_node_prev",
        "_set_object_node_prev",
        "_object_node_zpage",
        "_set_object_node_zpage",
        "_object_node_gc_refs",
        "_set_object_node_gc_refs",
        "_object_node_young_next",
        "_set_object_node_young_next",
        "_object_node_young_prev",
        "_set_object_node_young_prev",
        "_object_node_alloc",
        "_object_node_release",
        "_unlink_object_node",
        "_backend3_young_link_head",
        "_backend3_young_unlink",
        "_backend3_young_rebuild",
        "_object_known_size",
        "_live_bytes_subtract",
    ):
        assert f"def {old_name}(" not in managed
    assert '_object_node_prepare = extern("pcc_gc_object_node_prepare"' in managed
    assert '_object_node_plan_requires_prepare = extern(' in managed
    assert '_object_node_take_prepared = extern(' in managed
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_node_prepare"
    ] == ((), "c_ptr")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_node_plan_requires_prepare"
    ] == ((), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_node_take_prepared"
    ] == (("c_ptr",), "c_ptr")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_index_plan_capacity"
    ] == (("c_int64",), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_index_plan_commit"
    ] == (("c_ptr", "c_int64", "c_int64"), "c_int64")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_object_index_insert_preallocated"
    ] == (("c_ptr", "c_ptr"), "c_int64")
    assert '_backend3_young_unlink = extern(' in managed


def test_object_registration_prepares_node_and_index_before_graph_lock() -> None:
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    strict_registration = managed.split(
        "def pcc_gc_note_object_allocated_sized", 1
    )[1].split('@c_abi_export("pcc_gc_note_object_allocated")', 1)[0]

    for registration, prepare, lock, unlock in ((
            strict_registration,
            "_object_node_prepare()",
            "_object_graph_lock()",
            "_object_graph_unlock()",
        ),):
        prepare_at = registration.index(prepare)
        assert registration[:prepare_at].rfind(unlock) > (
            registration[:prepare_at].rfind(lock)
        )
        assert "object_node_plan_requires_prepare" in registration
        assert "pcc_gc_object_index_plan_capacity" in registration
        assert "pcc_gc_object_index_plan_commit" in registration
        assert "pcc_gc_object_index_insert_preallocated" in registration
        assert "pcc_gc_object_index_insert(o, n)" not in registration


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_object_nodes_have_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("object_nodes_" + emitter + ".ll")
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

        source = tmp_path / "object_nodes.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("object_nodes_" + emitter + ".o")
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


def test_object_nodes_preserve_pool_list_and_young_invariants() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    release = _export_body(strict, "pcc_gc_object_node_release")
    assert "if count >= 8192:" in release
    assert release.index("if count >= 8192:") < release.index("free(node)")

    unlink = _export_body(strict, "pcc_gc_object_node_unlink")
    assert unlink.index("pcc_gc_trace_cursor_load()") < unlink.index(
        "pcc_gc_backend3_young_unlink(node)"
    )
    assert unlink.index("pcc_gc_backend3_young_unlink(node)") < unlink.index(
        "pcc_gc_object_set_list_head(nxt)"
    )

    rebuild = _export_body(strict, "pcc_gc_backend3_young_rebuild")
    assert rebuild.index("pcc_gc_object_node_is_active(node)") < rebuild.index(
        "load_i32(obj, 12) & 128"
    )
    assert rebuild.index("load_i32(obj, 12) & 128") < rebuild.index(
        "pcc_gc_backend3_young_link_head(node)"
    )

    known_size = _export_body(strict, "pcc_gc_object_known_size")
    assert known_size.index("pcc_gc_object_index_find(obj)") < known_size.index(
        "pcc_gc_object_node_freeing(node) == 0"
    )
    assert known_size.index("pcc_gc_object_node_freeing(node) == 0") < (
        known_size.index("pcc_gc_object_node_size(node)")
    )

    live = _export_body(strict, "pcc_gc_live_bytes_subtract")
    assert "if size >= live:" in live
    assert 'store_i64(global_addr("pcc_gc_live_bytes"), 0, 0)' in live


def test_production_archive_has_one_object_node_owner(
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
        assert ":freestanding_gc_object_nodes.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
