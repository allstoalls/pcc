from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_generational_promotion.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend3_drain_promotion_worklist",
    "pcc_gc_backend3_enqueue_promotion_owner",
    "pcc_gc_backend3_promote_cext_owner_referents",
    "pcc_gc_backend3_promote_cext_slot_transaction",
    "pcc_gc_backend3_promotion_probe_config",
    "pcc_gc_backend3_promotion_probe_state",
    "pcc_gc_backend3_promotion_worklist_unlink",
    "pcc_gc_generational_promote_borrowed_slot_mode",
    "pcc_gc_generational_promote_owned_slot_mode",
    "pcc_gc_generational_promote_shallow_slot",
    "pcc_gc_generational_promote_slot",
    "pcc_gc_generational_promote_young_if_known",
    "pcc_gc_generational_pointer_can_have_header",
    "pcc_gc_generational_root_slot_value_is_stable",
    "pcc_gc_promote_cached_frame_slot",
    "pcc_gc_trace_referents_for_promotion",
    "pcc_gc_trace_referents_for_promotion_mode",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_backend3_promotion_revision",
    "pcc_gc_backend3_promotion_head",
    "pcc_gc_backend3_promotion_probe_pause",
    "pcc_gc_backend3_promotion_probe_state_value",
    "pcc_gc_backend3_promotion_tail",
    "pcc_gc_backend3_young_unlink",
    "pcc_gc_backend4_zpage_note_owner_promoted",
    "pcc_gc_config_ensure",
    "pcc_gc_forwarding_find",
    "pcc_gc_generational_oldify_copy",
    "pcc_gc_memoryview_refresh_owned_buffer",
    "pcc_gc_object_index_find",
    "pcc_gc_object_list_revision",
    "pcc_gc_object_node_is_active",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_pointer_is_managed",
    "pcc_gc_visit_object_slots",
    "pcc_gc_visit_object_slots_slice",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_thread_safepoint",
    "py_decref",
    "py_incref",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _export_body(source: str, symbol: str) -> str:
    return source.split(f'@c_abi_export("{symbol}")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]


def test_generational_promotion_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_generational_promotion" in makefile
    assert "def _promote_young_if_known(" not in managed
    assert "def _promote_young_slot_mode(" not in managed
    assert "def _promote_young_borrowed_slot_mode(" not in managed
    assert "def _root_slot_value_is_stable(" not in managed
    assert "def _trace_referents_for_promotion_mode(" not in managed
    assert "def _trace_referents_for_promotion(" not in managed
    assert "def _promote_cached_frame_slot(" not in managed
    assert '_promote_young_if_known = extern(' in managed
    assert '_promote_young_slot_mode = extern(' in managed
    assert '_trace_referents_for_promotion = extern(' in managed
    assert '@c_abi_export("pcc_gc_backend4_zpage_note_owner_promoted")' in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_generational_promotion_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("generational_promotion_" + emitter + ".ll")
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

        source = tmp_path / "generational_promotion.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("generational_promotion_" + emitter + ".o")
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


def test_generational_promotion_preserves_owned_borrowed_and_stable_root_contracts() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    owned = _export_body(strict, "pcc_gc_generational_promote_owned_slot_mode")
    borrowed = _export_body(
        strict, "pcc_gc_generational_promote_borrowed_slot_mode"
    )
    cached = _export_body(strict, "pcc_gc_promote_cached_frame_slot")
    promote = _export_body(strict, "pcc_gc_generational_promote_young_if_known")
    trace = _export_body(strict, "pcc_gc_trace_referents_for_promotion_mode")

    assert owned.index("py_incref(oldified)") < owned.index(
        "store_ptr(slot_base, slot_offset, oldified)"
    )
    assert owned.index("store_ptr(slot_base, slot_offset, oldified)") < owned.index(
        "py_decref(child)"
    )
    assert "py_incref(oldified)" not in borrowed
    assert "py_decref(child)" not in borrowed
    assert "if recurse == 0:" in owned
    assert "if recurse == 0:" in borrowed
    assert "pcc_gc_generational_promote_young_if_known(child)" in owned
    assert "pcc_gc_generational_promote_young_if_known(child)" in borrowed
    assert "pcc_gc_visit_object_slots(" in trace
    assert "tag ==" not in trace
    assert "pcc_gc_backend4_zpage_note_owner_promoted(obj)" in promote
    backend4 = promote.split(
        "if backend == 4 and (promoted_flags & 256) == 0:", 1
    )[1].split("store_i32(obj, 12", 1)[0]
    assert 'atomic_rmw_i32("add", obj, 12, 128, "acq_rel")' in backend4
    assert promote.index("pcc_gc_backend3_young_unlink") < promote.index(
        'atomic_rmw_i32("add", obj, 12, 128, "acq_rel")'
    )
    assert backend4.index(
        'atomic_rmw_i32("add", obj, 12, 128, "acq_rel")'
    ) < backend4.index("pcc_gc_backend4_zpage_note_owner_promoted(obj)")

    stale_guard = cached.split("if ptr_is_null(stable_base) == 0:", 1)[1].split(
        "if borrowed != 0:", 1
    )[0]
    assert "ptr_eq(load_ptr(stable_base, slot_offset), before)" in stale_guard
    assert "return" in stale_guard
    assert "pcc_gc_generational_root_slot_value_is_stable(after)" in cached
    assert "store_ptr(stable_base, slot_offset, null())" in cached


def test_production_archive_has_one_generational_promotion_owner(
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
        assert ":freestanding_gc_generational_promotion.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
