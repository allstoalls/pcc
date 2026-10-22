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
STRICT_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_generational_oldification.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_generational_mark_forwarded_source_inactive",
    "pcc_gc_generational_oldify_copy",
    "pcc_gc_generational_oldify_supported_tag",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_object_is_address_pinned",
    "free",
    "malloc",
    "memmove",
    "pcc_gc_backend3_young_unlink",
    "pcc_gc_forwarding_find",
    "pcc_gc_identity_remove",
    "pcc_gc_install_forwarding_unlocked",
    "pcc_gc_live_bytes_subtract",
    "pcc_gc_object_index_find",
    "pcc_gc_object_index_insert",
    "pcc_gc_object_index_remove",
    "pcc_gc_object_is_known_no_lock",
    "pcc_gc_object_known_size",
    "pcc_gc_object_list_head",
    "pcc_gc_object_node_alloc",
    "pcc_gc_object_node_freeing",
    "pcc_gc_object_node_release",
    "pcc_gc_object_node_set_freeing",
    "pcc_gc_object_node_set_gc_refs",
    "pcc_gc_object_node_set_prev",
    "pcc_gc_object_node_set_young_next",
    "pcc_gc_object_node_set_young_prev",
    "pcc_gc_object_node_size",
    "pcc_gc_object_node_unlink",
    "pcc_gc_object_set_list_head",
    "pcc_gc_relocate_copy_payload",
    "pcc_cpy_handle_move_owned_ref",
    "py_decref",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend_selected",
    "pcc_gc_live_bytes",
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


def test_generational_oldification_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_generational_oldification" in makefile
    assert "def _generational_oldify_copy(" not in managed
    assert "def _mark_forwarded_source_inactive(" not in managed
    assert "def _relocate_copy_supported_tag(" not in managed
    assert '_generational_oldify_copy = extern(' in managed
    assert '_relocate_copy_payload = extern(' in managed
    assert '@c_abi_export("pcc_gc_relocate_copy_payload")' not in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_generational_oldification_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("generational_oldification_" + emitter + ".ll")
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

        source = tmp_path / "generational_oldification.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("generational_oldification_" + emitter + ".o")
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


def test_generational_oldification_preserves_registration_and_rollback_order() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    oldify = _export_body(strict, "pcc_gc_generational_oldify_copy")

    assert oldify.index("pcc_gc_object_index_insert(to_obj, node)") < oldify.index(
        'global_addr("pcc_gc_live_bytes")'
    )
    assert oldify.index('global_addr("pcc_gc_live_bytes")') < oldify.index(
        "pcc_gc_install_forwarding_unlocked(from_obj, to_obj)"
    )
    failure = oldify.split(
        "if pcc_gc_install_forwarding_unlocked(from_obj, to_obj) != 0:", 1
    )[1].split("pcc_gc_backend3_young_unlink", 1)[0]
    assert failure.index("pcc_gc_object_index_remove(to_obj)") < failure.index(
        "pcc_gc_object_node_unlink(node)"
    )
    assert failure.index("pcc_gc_object_node_unlink(node)") < failure.index(
        "pcc_gc_live_bytes_subtract(size)"
    )
    assert failure.index("pcc_gc_live_bytes_subtract(size)") < failure.index(
        "pcc_gc_identity_remove(to_obj)"
    )
    assert failure.index("pcc_gc_identity_remove(to_obj)") < failure.index(
        "free(to_obj)"
    )

    assert oldify.index("pcc_gc_install_forwarding_unlocked(from_obj, to_obj)") < (
        oldify.index("pcc_gc_backend3_young_unlink")
    )
    assert oldify.index("pcc_gc_backend3_young_unlink") < oldify.index(
        "pcc_gc_generational_mark_forwarded_source_inactive(from_obj)"
    )


def test_generational_cpy_handle_oldification_moves_one_foreign_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_oldify = _export_body(strict, "pcc_gc_generational_oldify_copy")

    assert "PY_TYPE_CPY_HANDLE" in strict
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_cpy_handle_move_owned_ref"
    ] == (("c_ptr", "c_ptr"), "c_void")
    for body, forward_move, rollback_move in ((
            strict_oldify,
            "pcc_cpy_handle_move_owned_ref(from_obj, to_obj)",
            "pcc_cpy_handle_move_owned_ref(to_obj, from_obj)",
        ),):
        payload = body.index("pcc_gc_relocate_copy_payload")
        forward = body.index(forward_move)
        install = body.index("pcc_gc_install_forwarding_unlocked")
        rollback = body.index(rollback_move)
        assert payload < forward < install < rollback


def test_production_archive_has_one_generational_oldification_owner(
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
        assert ":freestanding_gc_generational_oldification.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
