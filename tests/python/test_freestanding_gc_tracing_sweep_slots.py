from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_sweep_slots.py"
BACKEND0_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_slots.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
COLLECTOR_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_backend0_collector.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend0_clear_referents",
    "pcc_gc_backend0_clear_slot",
    "pcc_gc_clear_container_metadata",
    "pcc_gc_tracing_clear_referents",
    "pcc_gc_tracing_clear_slot",
    "pcc_gc_tracing_clear_unreachable",
    "pcc_gc_tracing_is_sweep_candidate",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_object_is_address_pinned",
    "pcc_gc_backend0_is_unreachable",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_visit_object_slots",
    "py_decref",
    "py_weakref_invalidate",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_sweep_slots_" + emitter + ".ll")
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

        source = tmp_path / "freestanding_gc_sweep_slots.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_sweep_slots_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_sweep_slot_actions_have_one_strict_source_owner():
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    backend0 = BACKEND0_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    collector = COLLECTOR_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(backend0).isdisjoint(OWNED_SYMBOLS)
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_sweep_slots" in makefile
    assert "pcc_gc_backend0_clear_referents = extern(" in collector
    assert '_clear_unreachable = extern("pcc_gc_tracing_clear_unreachable"' in managed
    assert "def _clear_slot(" not in managed
    assert "def _clear_referents(" not in managed
    assert "def _clear_unreachable(" not in managed
    assert "def _clear_container_metadata(" not in backend0


def test_sweep_slot_actions_keep_distinct_candidate_policies_and_order():
    source = STRICT_SOURCE.read_text(encoding="utf-8")
    backend0 = source.split('@c_abi_export("pcc_gc_backend0_clear_slot")', 1)[1]
    backend0 = backend0.split("\n@c_abi_export", 1)[0]
    tracing = source.split('@c_abi_export("pcc_gc_tracing_clear_slot")', 1)[1]
    tracing = tracing.split("\n@c_abi_export", 1)[0]
    unreachable = source.split(
        '@c_abi_export("pcc_gc_tracing_clear_unreachable")', 1
    )[1]

    assert "pcc_gc_backend0_is_unreachable(child)" in backend0
    assert "pcc_gc_tracing_is_sweep_candidate(child)" not in backend0
    assert "pcc_gc_tracing_is_sweep_candidate(child)" in tracing
    assert "pcc_gc_backend0_is_unreachable(child)" not in tracing
    for body in (backend0, tracing):
        assert body.index("store_ptr(slot, 0, null())") < body.index("py_decref(child)")
    assert unreachable.index("py_weakref_invalidate(obj)") < unreachable.index(
        "pcc_gc_tracing_clear_referents(obj)"
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_sweep_slot_object_has_exact_raw_closure(tmp_path: Path, emitter: str):
    obj = _compile_object(tmp_path, emitter)
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


def test_production_archive_has_one_sweep_slot_owner(pcc_runtime_archive: Path):
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
        assert ":freestanding_gc_sweep_slots.o:" in owners[0]
