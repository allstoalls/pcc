from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_remap.py"
STRICT_FORWARDING_RETIREMENT = (
    RUNTIME_DIR / "py" / "freestanding_gc_forwarding_retirement.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_relocate_copy_supported_tag",
    "pcc_gc_backend4_remap_heal_slot",
    "pcc_gc_backend4_remap_cext_ctx_valid",
    "pcc_gc_backend4_remap_cext_referents_unlocked",
    "pcc_gc_backend4_remap_cext_slot_transaction",
    "pcc_gc_backend4_remap_referents",
    "pcc_gc_backend4_remap_slot",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_forwarding_find",
    "pcc_gc_generational_oldify_supported_tag",
    "pcc_gc_memoryview_refresh_owned_buffer",
    "pcc_gc_visit_object_slots",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_gc_backend4_remap_active",
    "pcc_gc_backend4_remap_epoch",
    "pcc_gc_backend4_remap_pending_obj",
    "pcc_gc_backend4_reseed_page_revision",
    "pcc_gc_backend4_reseed_relocation_revision",
    "pcc_gc_backend_selected",
    "pcc_gc_forwarding_head",
    "pcc_gc_forwarding_population",
    "pcc_gc_object_list_revision",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _export_body(source: str, symbol: str) -> str:
    return source.split(f'@c_abi_export("{symbol}")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]


def test_relocation_remap_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_relocation_remap" in makefile
    assert "def _colored_relocate_copy_supported_tag(" not in managed
    assert "def _remap_heal_slot(" not in managed
    assert "def _remap_referents(" not in managed
    assert '_colored_relocate_copy_supported_tag = extern(' in managed
    assert '_remap_heal_slot = extern(' in managed
    assert '_remap_referents = extern(' in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_relocation_remap_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("relocation_remap_" + emitter + ".ll")
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

        source = tmp_path / "relocation_remap.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("relocation_remap_" + emitter + ".o")
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


def test_relocation_remap_uses_shared_slot_contract_and_one_epoch_flags() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    retirement = STRICT_FORWARDING_RETIREMENT.read_text(encoding="utf-8")
    remap = _export_body(strict, "pcc_gc_backend4_remap_referents")
    heal = _export_body(strict, "pcc_gc_backend4_remap_heal_slot")
    retire = _export_body(
        retirement, "pcc_gc_backend4_remap_and_retire_unlocked"
    )

    assert "pcc_gc_visit_object_slots(" in remap
    assert "pcc_gc_backend4_remap_slot" in remap
    assert "pcc_gc_forwarding_find(value)" in heal
    assert "store_ptr(base, offset, target)" in heal
    assert "pcc_capi_is_cext_type_tag(load_i32(obj, 8)) == 0" in retire
    assert "pcc_gc_backend4_remap_referents(obj)" in retire
    assert "old_flags & 131072" in retire
    assert "old_flags | 131072" in retire
    assert "old_flags & ~(2048 | 131072)" in retire


def test_production_archive_has_one_relocation_remap_owner(
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
        assert ":freestanding_gc_relocation_remap.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
