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
    RUNTIME_SIGNATURES,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_relocate_copy_payload",
    "pcc_gc_relocate_copy_payload_prepared_locked",
    "pcc_gc_relocation_payload_copy_slots",
    "pcc_gc_relocation_payload_clear_destination_owned",
    "pcc_gc_relocation_payload_count_slot",
    "pcc_gc_relocation_payload_fail",
    "pcc_gc_relocation_payload_finish",
    "pcc_gc_relocation_payload_from_slot",
    "pcc_gc_relocation_payload_retarget_continuation_root_slots",
    "pcc_gc_relocation_payload_retire_collect_slot",
    "pcc_gc_relocation_payload_retire_count_slot",
    "pcc_gc_relocation_payload_slot_pairs_dispose",
    "pcc_gc_relocation_payload_slot_pairs_prepare",
    "pcc_gc_relocation_payload_slot_count_locked",
    "pcc_gc_relocation_payload_plan_prepare",
    "pcc_gc_relocation_payload_plan_validate_locked",
    "pcc_gc_relocation_payload_plan_finish",
    "pcc_gc_relocation_payload_raw_add_descriptor",
    "pcc_gc_relocation_payload_raw_plan_finish",
    "pcc_gc_relocation_payload_raw_prepare",
    "pcc_gc_relocation_payload_raw_publish_locked",
    "pcc_gc_relocation_payload_raw_snapshot_locked",
    "pcc_gc_relocation_payload_raw_transfer_buffers",
    "pcc_gc_relocation_payload_raw_validate_locked",
    "pcc_gc_relocation_payload_to_slot",
    "pcc_gc_relocation_finish_source_payloads",
    "pcc_gc_relocation_retire_source_payload",
    "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish",
    "pcc_gc_relocation_retire_source_payload_into_finish",
    "pcc_gc_relocation_retire_source_payload_into_finish_impl",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    "malloc",
    "memmove",
    "memset",
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_backend4_remap_heal_slot",
    "pcc_gc_backend4_remembered_set_retarget_slot",
    "pcc_gc_backend4_source_side_table_plan_commit",
    "pcc_gc_backend4_source_side_table_plan_finish",
    "pcc_gc_backend4_source_side_table_plan_prepare",
    "pcc_gc_backend4_zpage_payload_span_preflight_locked",
    "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked",
    "pcc_gc_load_ptr",
    "pcc_gc_root_registry_note_mutation_locked",
    "pcc_gc_visit_object_slots",
    "pcc_py_gc_defer_tripwire",
    "pcc_gc_retain_plan_finish",
    "pcc_gc_retain_plan_prepare_locked",
    "py_decref",
    "py_mem_free",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend3_continuation_root_scan_cursor",
    "pcc_gc_backend3_frame_root_scan_slot",
    "pcc_gc_continuation_root_head",
    "pcc_gc_relocate_slot_pairs_ctx",
    "py_weakref_head",
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


def test_relocation_payload_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_relocation_payload" in makefile
    assert "def _relocate_copy_payload(" not in managed
    assert '_relocate_copy_payload = extern(' in managed
    assert "def _py_obj_visit_relocate_count_slot(" not in managed
    assert "def _py_obj_visit_relocate_from_slot(" not in managed
    assert "def _py_obj_visit_relocate_to_slot(" not in managed
    assert (
        "pcc_gc_relocation_retire_source_payload"
        in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    )
    for symbol in (
        "pcc_gc_backend4_source_side_table_plan_prepare",
        "pcc_gc_backend4_source_side_table_plan_commit",
        "pcc_gc_backend4_source_side_table_plan_finish",
        "pcc_gc_relocate_copy_payload_prepared_locked",
        "pcc_gc_relocation_payload_slot_count_locked",
        "pcc_gc_relocation_payload_plan_prepare",
        "pcc_gc_relocation_payload_plan_validate_locked",
        "pcc_gc_relocation_payload_plan_finish",
        "pcc_gc_relocation_payload_raw_snapshot_locked",
        "pcc_gc_relocation_payload_raw_prepare",
        "pcc_gc_relocation_payload_raw_validate_locked",
        "pcc_gc_backend4_zpage_payload_span_preflight_locked",
        "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked",
    ):
        assert symbol in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
        assert symbol not in RUNTIME_SIGNATURES
    assert "pcc_gc_relocation_retire_source_payload" not in RUNTIME_SIGNATURES


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_relocation_payload_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("relocation_payload_" + emitter + ".ll")
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

        source = tmp_path / "relocation_payload.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("relocation_payload_" + emitter + ".o")
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


def test_relocation_payload_copies_raw_storage_but_uses_shared_slot_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    plan_prepare = strict.split(
        "def pcc_gc_relocation_payload_plan_prepare", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_raw_add_descriptor")', 1
    )[0]
    assert "ctx = malloc(416)" in plan_prepare
    assert "memset(ctx, 0, 416)" in plan_prepare
    raw_descriptor = strict.split(
        "def _relocate_raw_add_descriptor", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_raw_snapshot_locked")', 1
    )[0]
    assert "count >= 4" in raw_descriptor
    assert "descriptor = ptr_add(ctx, 152 + count * 64)" in raw_descriptor
    raw_finish = strict.split("def _relocate_raw_plan_finish", 1)[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_slot_pairs_dispose")', 1
    )[0]
    assert "descriptor = ptr_add(ctx, 152 + index * 64)" in raw_finish
    assert "buffer = load_ptr(descriptor, 48)" in raw_finish
    assert "span_node = load_ptr(descriptor, 56)" in raw_finish
    raw_validate = strict.split(
        "def pcc_gc_relocation_payload_raw_validate_locked", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_plan_validate_locked")', 1
    )[0]
    assert "current = stack_alloc(416)" in raw_validate
    assert "current_descriptor = ptr_add(current, 152 + index * 64)" in raw_validate

    for tag in (
        "list",
        "dict",
        "tuple",
        "set",
        "class",
        "exc",
        "weakref",
        "thread",
        "task",
        "continuation",
        "virtual_thread",
    ):
        assert f'tag == abi_constant("object.type.{tag}")' in strict
    assert 'tag == abi_constant("object.type.instance")' in strict
    assert 'tag >= abi_constant("object.type.user_class_start")' in strict
    assert "pcc_gc_visit_object_slots(" in strict
    assert "pcc_gc_backend4_remap_heal_slot(from_slot, 0)" in strict
    assert "pcc_gc_backend4_remembered_set_retarget_slot(" in strict
    assert "pcc_gc_backend4_zpage_payload_span_preflight_locked(" in strict
    assert (
        "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked("
        in strict
    )
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" not in strict
    assert "_retarget_continuation_root_slots(" in strict
    assert "_py_obj_visit_covered_slots" not in strict

    payload = strict.split(
        "def pcc_gc_relocate_copy_payload_prepared_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy_payload")', 1)[0]
    assert "py_incref(" not in payload
    assert "pcc_gc_backend4_remembered_set_retarget_slot(" not in payload

    prepared = strict.split(
        "def pcc_gc_relocate_copy_payload_prepared_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy_payload")', 1)[0]
    assert "_relocate_slot_pairs_clear_destination(to_obj, ctx)" in prepared
    assert "_relocate_slot_pairs_dispose(ctx)" not in prepared
    legacy = strict.split(
        "def pcc_gc_relocate_copy_payload(from_obj, to_obj, tag: i64, size: i64)",
        1,
    )[1]
    assert legacy.count("pcc_gc_relocate_copy_payload_prepared_locked(") == 1
    assert legacy.count("_relocate_slot_pairs_dispose(ctx)") == 1
    assert legacy.index("pcc_gc_relocate_copy_payload_prepared_locked(") < (
        legacy.index("_relocate_slot_pairs_dispose(ctx)")
    )


def test_relocation_payload_retires_old_ownership_before_decref_reentry() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    count_callback = strict[
        strict.index("def _retire_count_owned_slot") :
        strict.index("def _retire_collect_owned_slot")
    ]
    retire = strict[
        strict.index("def _retire_source_payload_into_finish") :
        strict.index("def _retarget_continuation_root_slots")
    ]

    # ALL roles heal before OWNED filtering, so an instance's borrowed class
    # slot is current before the visitor reloads it to derive n_fields.
    assert count_callback.index("pcc_gc_backend4_remap_heal_slot(slot, 0)") < (
        count_callback.index("if role == 1")
    )
    # Allocation and exact two-pass validation precede ownership mutation.
    assert retire.index("records = malloc(count * 16)") < retire.index(
        "store_ptr(slot, 0, null())"
    )
    assert retire.index("load_i64(context, 8) != count") < retire.index(
        "store_ptr(slot, 0, null())"
    )
    # The opaque side plan performs all allocation and snapshot validation
    # before ownership mutation.  Commit happens only after the shell is inert,
    # removes all owner side metadata without decref, and precedes raw frees.
    assert retire.index(
        "pcc_gc_backend4_source_side_table_plan_prepare(from_obj)"
    ) < retire.index("store_ptr(slot, 0, null())")
    commit = retire.index(
        "pcc_gc_backend4_source_side_table_plan_commit(side_plan)"
    )
    assert retire.index("raw0 = items") < commit
    assert commit < retire.index("free(raw0)")
    finish = retire.index(
        "pcc_gc_backend4_source_side_table_plan_finish("
    )
    assert retire.index("free(raw3)") < finish
    # Every slot is NULL and each independent raw payload is detached before
    # the first decref can re-enter retirement.
    first_decref = retire.index("py_decref(")
    assert finish < first_decref
    assert retire.index("store_ptr(slot, 0, null())") < first_decref
    assert retire.index("store_ptr(from_obj, 24, null())") < first_decref
    assert retire.index("store_ptr(from_obj, 40, null())") < first_decref
    assert retire.index("store_ptr(from_obj, 32, null())") < first_decref
    assert "pcc_gc_unregister_continuation_root" not in retire
    assert "py_weakref" not in retire
    assert "pcc_gc_free_object_memory" not in retire


def test_source_side_table_plan_is_prepare_commit_finish_transaction() -> None:
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    prepare = managed[
        managed.index("def pcc_gc_backend4_source_side_table_plan_prepare") :
        managed.index("def pcc_gc_backend4_source_side_table_plan_commit")
    ]
    commit = managed[
        managed.index("def pcc_gc_backend4_source_side_table_plan_commit") :
        managed.index("def pcc_gc_backend4_source_side_table_plan_finish")
    ]
    finish = managed[
        managed.index("def pcc_gc_backend4_source_side_table_plan_finish") :
        managed.index("def _backend4_store_buffer_owner_pending")
    ]

    assert "malloc(32)" in prepare
    assert "malloc(count * 8)" in prepare
    assert "py_decref(" not in prepare
    assert "_backend4_remembered_set_remove(" not in prepare
    assert "_backend4_zpage_remove(" not in prepare
    assert "malloc(" not in commit
    assert "py_decref(" not in commit
    assert commit.index("_backend4_store_buffer_dec()") < commit.index(
        "_backend4_remembered_set_remove(owner)"
    )
    assert commit.index("_backend4_remembered_set_remove(owner)") < commit.index(
        "_backend4_zpage_remove(owner)"
    )
    assert "py_decref(" in finish


def test_production_archive_has_one_relocation_payload_owner(
    pcc_runtime_archive: Path,
) -> None:
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in (
        "pcc_gc_relocate_copy_payload",
        "pcc_gc_relocation_retire_source_payload",
        "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish",
    ):
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_relocation_payload.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
    for symbol in (
        "pcc_gc_backend4_source_side_table_plan_prepare",
        "pcc_gc_backend4_source_side_table_plan_commit",
        "pcc_gc_backend4_source_side_table_plan_finish",
    ):
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":py_gc_backend.o:" in owners[0]
