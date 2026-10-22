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
    RUNTIME_DIR / "py" / "freestanding_gc_generational_remembered_owners.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"
SCHEDULER_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_generational_scheduler.py"
)

OWNED_SYMBOLS = {
    "pcc_gc_backend3_clear_remembered_owners",
    "pcc_gc_backend3_drain_remembered_owners",
    "pcc_gc_backend3_finish_detached_remembered_owners",
    "pcc_gc_backend3_remember_owner",
    "pcc_gc_backend3_remembered_scan_probe_config",
    "pcc_gc_backend3_remembered_owner_list_head",
    "pcc_gc_backend3_remembered_owner_list_set_head",
    "pcc_gc_backend3_scan_remembered_owners",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    "malloc",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_object_list_head",
    "pcc_gc_object_node_is_active",
    "pcc_gc_object_node_next",
    "pcc_gc_trace_referents_for_promotion",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend3_remembered_owner_allocation_limit",
    "pcc_gc_backend3_remembered_overflow",
    "pcc_gc_backend3_remembered_owner_head",
    "pcc_gc_backend3_remembered_scan_cursor",
    "pcc_gc_backend3_remembered_scan_revision",
    "pcc_gc_object_list_revision",
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


def test_generational_remembered_owners_have_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_generational_remembered_owners" in makefile
    assert "def _backend3_remember_owner(" not in managed
    assert "def _backend3_clear_remembered_owners(" not in managed
    assert "def _backend3_scan_remembered_owners(" not in managed
    assert "def _backend3_drain_remembered_owners(" not in managed
    assert '_backend3_remember_owner = extern(' in managed
    scheduler = SCHEDULER_SOURCE.read_text(encoding="utf-8")
    assert 'pcc_gc_backend3_drain_remembered_owners = extern(' in scheduler
    assert '_backend3_finish_detached_remembered_owners = extern(' in managed
    assert 'pcc_gc_backend3_finish_detached_remembered_owners = extern(' in scheduler
    assert '_trace_referents_for_promotion = extern(' in managed
    assert '@c_abi_export("pcc_gc_trace_referents_for_promotion")' not in managed


def test_remembered_owner_node_retirement_finishes_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    scheduler = SCHEDULER_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")

    finish_symbol = "pcc_gc_backend3_finish_detached_remembered_owners"
    assert finish_symbol in strict
    assert finish_symbol in scheduler
    assert finish_symbol in managed

    strict_clear = _export_body(
        strict, "pcc_gc_backend3_clear_remembered_owners"
    )
    strict_drain = _export_body(
        strict, "pcc_gc_backend3_drain_remembered_owners"
    )
    assert "free(" not in strict_clear
    assert "free(" not in strict_drain

    strict_step = _export_body(scheduler, "pcc_gc_generational_step")
    strict_drain_at = strict_step.index(
        "pcc_gc_backend3_drain_remembered_owners"
    )
    strict_unlock_at = strict_step.index(
        "pcc_py_gc_minor_graph_unlock()", strict_drain_at
    )
    assert strict_unlock_at < strict_step.index(finish_symbol, strict_unlock_at)

    managed_telemetry = _export_body(managed, "pcc_gc_telemetry_reset")
    managed_clear_at = managed_telemetry.index(
        "_backend3_clear_remembered_owners"
    )
    managed_unlock_at = managed_telemetry.index(
        "_object_graph_unlock()", managed_clear_at
    )
    assert managed_unlock_at < managed_telemetry.index(
        "_backend3_finish_detached_remembered_owners", managed_unlock_at
    )


def test_generational_locked_step_caps_work_without_safepointing() -> None:
    strict_owners = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_scheduler = SCHEDULER_SOURCE.read_text(encoding="utf-8")

    strict_scan = _export_body(
        strict_owners, "pcc_gc_backend3_scan_remembered_owners"
    )
    strict_drain = _export_body(
        strict_owners, "pcc_gc_backend3_drain_remembered_owners"
    )
    assert "pcc_thread_safepoint" not in strict_scan
    assert "pcc_thread_safepoint" not in strict_drain

    strict_step = _export_body(strict_scheduler, "pcc_gc_generational_step")
    strict_unlock_at = strict_step.index("pcc_py_gc_minor_graph_unlock()")
    assert "pcc_thread_safepoint" not in strict_step[:strict_unlock_at]
    assert "batch_budget" in strict_step
    assert "batch_budget = 16" in strict_step
    assert strict_unlock_at < strict_step.index(
        "pcc_thread_safepoint", strict_unlock_at
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_generational_remembered_owners_have_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("generational_remembered_owners_" + emitter + ".ll")
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

        source = tmp_path / "generational_remembered_owners.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("generational_remembered_owners_" + emitter + ".o")
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


def test_generational_remembered_owners_preserve_overflow_and_budget_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    remember = _export_body(strict, "pcc_gc_backend3_remember_owner")
    clear = _export_body(strict, "pcc_gc_backend3_clear_remembered_owners")
    finish = _export_body(
        strict, "pcc_gc_backend3_finish_detached_remembered_owners"
    )
    scan = _export_body(strict, "pcc_gc_backend3_scan_remembered_owners")
    drain = _export_body(strict, "pcc_gc_backend3_drain_remembered_owners")

    allocation_failure = remember.split("if ptr_is_null(node) != 0:", 1)[1].split(
        "return", 1
    )[0]
    assert allocation_failure.index("pcc_gc_backend3_remembered_overflow") < (
        allocation_failure.index("owner_flags | 512")
    )
    assert clear.index("pcc_gc_backend3_remembered_owner_list_set_head(null())") < (
        clear.index("return node")
    )
    assert clear.index("pcc_gc_backend3_remembered_overflow") < clear.index(
        "return node"
    )
    assert "free(" not in clear
    assert "free(head)" in finish
    assert "pcc_gc_object_node_is_active(node)" in scan
    assert "local_examined < remaining_budget" in scan
    assert 'global_load_ptr("pcc_gc_backend3_remembered_scan_cursor")' in scan
    assert 'global_addr("pcc_gc_object_list_revision")' in scan
    assert 'global_addr("pcc_gc_backend3_remembered_scan_revision")' in scan
    assert "local_examined = local_examined + 1" in scan
    assert "return local_examined" in scan
    assert scan.index("pcc_gc_trace_referents_for_promotion(owner)") < scan.index(
        "flags & ~512"
    )
    overflow = drain.split(
        'global_addr("pcc_gc_backend3_remembered_overflow")', 1
    )[1].split("while", 1)[0]
    assert "pcc_gc_backend3_clear_remembered_owners()" in overflow
    assert "pcc_gc_backend3_scan_remembered_owners(" in overflow
    assert "remaining_budget" in overflow
    assert 'global_load_ptr("pcc_gc_backend3_remembered_scan_cursor")' in overflow
    assert "local_processed < remaining_budget" in drain
    assert drain.index("store_ptr(detached_out, 0, node)") < drain.index(
        "pcc_gc_object_is_known_no_lock(owner)"
    )


def test_generational_remembered_overflow_scan_has_restartable_cursors() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    object_nodes = (
        RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py"
    ).read_text(encoding="utf-8")
    state = (RUNTIME_DIR / "py" / "freestanding_gc_state.py").read_text(
        encoding="utf-8"
    )

    strict_remember = _export_body(strict, "pcc_gc_backend3_remember_owner")
    strict_scan = _export_body(
        strict, "pcc_gc_backend3_scan_remembered_owners"
    )
    strict_unlink = _export_body(object_nodes, "pcc_gc_object_node_unlink")

    assert "pcc_gc_backend3_remembered_owner_allocation_limit" in strict_remember
    assert "pcc_gc_backend3_remembered_scan_cursor" in strict_remember
    assert "pcc_gc_backend3_remembered_scan_revision" in strict_remember
    assert "pcc_gc_object_list_revision" in strict_scan
    assert "local_examined < remaining_budget" in strict_scan
    assert strict_unlink.index("pcc_gc_backend3_remembered_scan_cursor") < (
        strict_unlink.index("pcc_gc_backend3_young_unlink(node)")
    )
    assert "pcc_gc_object_list_revision" in strict_unlink

    for declaration in (
        'define_global_i64("pcc_gc_object_list_revision", 0)',
        'define_global_i64("pcc_gc_backend3_remembered_scan_revision", 0)',
        'define_global_i64("pcc_gc_backend3_remembered_owner_allocation_limit", -1)',
        'define_global_ptr_null("pcc_gc_backend3_remembered_scan_cursor")',
    ):
        assert declaration in state


def test_production_archive_has_one_generational_remembered_owner_provider(
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
        assert ":freestanding_gc_generational_remembered_owners.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
