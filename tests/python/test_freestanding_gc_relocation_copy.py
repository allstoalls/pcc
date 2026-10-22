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
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_copy.py"
FORWARDING_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_forwarding_identity.py"
)
INDEX_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_index_table.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_relocate_copy_preallocated_unlocked",
    "pcc_gc_relocate_copy",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_object_is_address_pinned",
    "free",
    "memmove",
    "pcc_gc_alloc",
    "pcc_gc_backend4_relocate_copy_supported_tag",
    "pcc_gc_backend4_zpage_find",
    "pcc_gc_backend4_zpage_detach_for_relocation",
    "pcc_gc_backend4_zpage_finish_relocation_detach",
    "pcc_gc_config_ensure",
    "pcc_gc_forwarding_find",
    "pcc_gc_forwarding_install_plan_finish",
    "pcc_gc_forwarding_install_plan_prepare",
    "pcc_gc_install_forwarding_preallocated_unlocked",
    "pcc_gc_memoryview_refresh_owned_buffer",
    "pcc_gc_object_known_size",
    "pcc_gc_relocation_member_count",
    "pcc_gc_relocation_member_forget",
    "pcc_gc_relocate_copy_payload_prepared_locked",
    "pcc_gc_relocation_payload_plan_finish",
    "pcc_gc_relocation_payload_plan_prepare",
    "pcc_gc_relocation_payload_plan_validate_locked",
    "pcc_gc_relocation_payload_raw_prepare",
    "pcc_gc_relocation_payload_raw_snapshot_locked",
    "pcc_gc_relocation_payload_raw_validate_locked",
    "pcc_gc_relocation_payload_slot_count_locked",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "py_decref",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend_selected",
    "pcc_gc_backend4_evacuated_bytes_count",
    "pcc_gc_backend4_evacuation_page_head",
    "pcc_gc_backend4_reseed_commit_owner",
    "pcc_gc_backend4_reseed_page_count_cursor",
    "pcc_gc_backend4_reseed_page_revision",
    "pcc_gc_backend4_reseed_relocation_cursor",
    "pcc_gc_backend4_reseed_relocation_revision",
    "pcc_gc_relocation_set_head",
    "py_class_attr_cache_epoch",
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


def test_relocation_copy_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_relocation_copy" in makefile
    assert "def _relocate_copy_unlocked(" not in managed
    assert 'pcc_gc_relocate_copy = extern(' in managed
    assert '_backend4_relocate_copy_unlocked = extern(' not in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_relocation_copy_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("relocation_copy_" + emitter + ".ll")
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

        source = tmp_path / "relocation_copy.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("relocation_copy_" + emitter + ".o")
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


def test_relocation_copy_preserves_transaction_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_backend4_relocate_copy_preallocated_unlocked"
    ] == (
        ("c_ptr", "c_int64", "c_ptr", "c_ptr", "c_ptr", "c_ptr"),
        "c_ptr",
    )
    body = strict.split(
        "def pcc_gc_backend4_relocate_copy_preallocated_unlocked", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocate_copy")', 1
    )[0]
    compact = "".join(body.split())

    assert 'load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4' in body
    assert "pcc_gc_forwarding_find(from_obj)" in body
    assert 'global_load_ptr("pcc_gc_relocation_set_head")' in body
    assert "ptr_eq(load_ptr(relocation_node, 0), from_obj)" in body
    assert "pcc_gc_backend4_relocate_copy_supported_tag(tag)" in body
    assert "known_size <= 0 or size > known_size" in body
    assert "to_residency: i64 = load_i32(to_obj, 12) & 331776" in body
    assert "memmove(to_obj, from_obj, size)" in body
    assert "pcc_gc_relocate_copy_payload_prepared_locked(" in body
    assert "from_obj, to_obj, tag, size, payload_plan" in body
    assert (
        "pcc_gc_install_forwarding_preallocated_unlocked("
        "from_obj,to_obj,forwarding_plan)"
    ) in compact
    assert "store_i64(to_obj, 0, load_i64(to_obj, 0) + outstanding)" in body
    assert "store_i32(from_obj, 12, load_i32(from_obj, 12) | 1)" in body
    assert "store_ptr(finish_plan, 0, null())" in body
    assert "store_ptr(finish_plan, 8, null())" in body
    assert 'global_store_ptr("pcc_gc_relocation_set_head", nxt)' in body
    assert "store_ptr(finish_plan, 0, relocation_node)" in body
    assert "pcc_gc_backend4_zpage_find(from_obj)" in body
    assert (
        'global_load_ptr("pcc_gc_backend4_evacuation_page_head")'
        in compact
    )
    assert (
        'global_store_ptr("pcc_gc_backend4_evacuation_page_head",next_page)'
        in compact
    )
    assert "store_ptr(finish_plan, 8, page_node)" in body
    for managed_helper in (
        "pcc_gc_backend4_evacuation_page_remove",
        "pcc_gc_backend4_relocation_set_contains_page",
        "pcc_gc_backend4_relocation_set_remove",
        "pcc_gc_backend4_zpage_page_for_owner",
    ):
        assert managed_helper not in body
    for forbidden in (
        "pcc_gc_alloc(",
        "pcc_thread_safepoint(",
        "py_decref(to_obj)",
    ):
        assert forbidden not in body
    assert "pcc_gc_backend4_zpage_detach_for_relocation(from_obj)" in body

    public = strict.split('def pcc_gc_relocate_copy(from_obj, size: i64):', 1)[1]
    assert "finish_plan = stack_alloc(24)" in public
    assert "store_ptr(finish_plan, 16, null())" in public
    assert (
        "from_obj,size,to_obj,payload_plan,forwarding_plan,finish_plan"
        in "".join(
            public.split(
                "pcc_gc_backend4_relocate_copy_preallocated_unlocked(", 1
            )[1].split()
        )
    )
    assert public.index("pcc_py_gc_minor_graph_unlock()") < public.index(
        "to_obj = pcc_gc_alloc(size, tag, (flags & ~10240) | 64)"
    )
    allocation = public.index("to_obj = pcc_gc_alloc")
    commit_lock = public.index("pcc_py_gc_minor_graph_lock()", allocation)
    commit_unlock = public.index("pcc_py_gc_minor_graph_unlock()", commit_lock)
    first_finish_free = public.index("free(detached)", commit_unlock)
    failure_decref = public.index("py_decref(to_obj)", commit_unlock)
    assert allocation < commit_lock < commit_unlock < first_finish_free
    assert commit_unlock < failure_decref


def test_relocation_copy_defers_source_zpage_free_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    zpage = (
        RUNTIME_DIR / "py" / "freestanding_gc_zpage_lifecycle.py"
    ).read_text(encoding="utf-8")

    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_backend4_zpage_detach_for_relocation"
    ] == (("c_ptr",), "c_ptr")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_backend4_zpage_finish_relocation_detach"
    ] == (("c_ptr",), "c_void")

    strict_commit = strict.split(
        "def pcc_gc_backend4_relocate_copy_preallocated_unlocked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy")', 1)[0]
    assert "if ptr_is_null(finish_plan) != 0:" in strict_commit
    assert "pcc_gc_backend4_zpage_detach_for_relocation(from_obj)" in strict_commit
    assert "pcc_gc_backend4_zpage_remove(from_obj)" not in strict_commit
    assert "free(" not in strict_commit

    strict_public = strict.split('@c_abi_export("pcc_gc_relocate_copy")', 1)[1]
    assert "finish_plan = stack_alloc(24)" in strict_public
    assert "store_ptr(finish_plan, 16, null())" in strict_public
    strict_unlock = strict_public.index(
        "pcc_py_gc_minor_graph_unlock()",
        strict_public.index("committed = null()"),
    )
    strict_finish = strict_public.index(
        "pcc_gc_backend4_zpage_finish_relocation_detach(detached)",
        strict_unlock,
    )
    assert strict_unlock < strict_finish

    strict_detach = zpage.split(
        'def pcc_gc_backend4_zpage_detach_for_relocation(owner)', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_finish_relocation_detach")', 1
    )[0]
    assert "free(" not in strict_detach
    assert "pcc_gc_backend4_zpage_node_release(" not in strict_detach
    strict_detach_finish = zpage.split(
        'def pcc_gc_backend4_zpage_finish_relocation_detach(node)', 1
    )[1].split('@c_abi_export(', 1)[0]
    assert "free(node)" in strict_detach_finish


def test_relocation_copy_preallocates_forwarding_indexes_outside_graph_lock(
) -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    forwarding = FORWARDING_SOURCE.read_text(encoding="utf-8")
    index_source = INDEX_SOURCE.read_text(encoding="utf-8")

    expected_signatures = {
        "pcc_gc_forwarding_install_plan_prepare": (
            ("c_ptr", "c_ptr"),
            "c_ptr",
        ),
        "pcc_gc_install_forwarding_preallocated_unlocked": (
            ("c_ptr", "c_ptr", "c_ptr"),
            "c_int64",
        ),
        "pcc_gc_forwarding_install_plan_finish": (("c_ptr",), "c_void"),
        "pcc_gc_forwarding_plan_index_capacity": (
            ("c_int64", "c_int64"),
            "c_int64",
        ),
        "pcc_gc_forwarding_plan_index_commit": (
            ("c_int64", "c_ptr", "c_int64", "c_int64"),
            "c_int64",
        ),
        "pcc_gc_forwarding_plan_index_insert": (
            ("c_int64", "c_ptr", "c_ptr"),
            "c_int64",
        ),
    }
    for symbol, signature in expected_signatures.items():
        assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == signature

    strict_commit = strict.split(
        "def pcc_gc_backend4_relocate_copy_preallocated_unlocked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy")', 1)[0]
    assert (
        "pcc_gc_install_forwarding_preallocated_unlocked("
        "from_obj,to_obj,forwarding_plan)"
    ) in "".join(strict_commit.split())
    assert "pcc_gc_install_forwarding_unlocked(" not in strict_commit

    strict_public = strict.split('@c_abi_export("pcc_gc_relocate_copy")', 1)[1]
    plan_prepare = strict_public.index(
        "pcc_gc_forwarding_install_plan_prepare(from_obj, to_obj)"
    )
    commit_lock = strict_public.index(
        "pcc_py_gc_minor_graph_lock()", plan_prepare
    )
    commit_unlock = strict_public.index(
        "pcc_py_gc_minor_graph_unlock()", commit_lock
    )
    plan_finish = strict_public.index(
        "pcc_gc_forwarding_install_plan_finish(forwarding_plan)",
        commit_unlock,
    )
    assert plan_prepare < commit_lock < commit_unlock < plan_finish

    strict_install = forwarding.split(
        '@c_abi_export("pcc_gc_install_forwarding_preallocated_unlocked")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    for forbidden in ("malloc(", "calloc(", "free(", "py_decref("):
        assert forbidden not in strict_install
    assert "pcc_gc_forwarding_plan_index_commit(" in strict_install
    assert "pcc_gc_forwarding_plan_index_insert(" in strict_install
    assert "pcc_gc_forwarding_index_remove(from_obj)" in strict_install

    strict_prepare = forwarding.split(
        '@c_abi_export("pcc_gc_forwarding_install_plan_prepare")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    prepare_unlock = strict_prepare.index("_graph_unlock()")
    assert prepare_unlock < strict_prepare.index("plan = calloc(1, 72)")
    assert "calloc(" not in strict_prepare[:prepare_unlock]

    strict_index_commit = index_source.split(
        '@c_abi_export("pcc_gc_forwarding_plan_index_commit")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    for forbidden in ("malloc(", "calloc(", "free("):
        assert forbidden not in strict_index_commit


def test_relocation_copy_prepares_slot_retains_outside_graph_lock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    payload = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
    ).read_text(encoding="utf-8")
    strict_obj = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    internal = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    public_header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )

    # The old strict unlocked entry allocated a destination and all payload
    # bookkeeping under whichever recursive graph-lock scope called it.  It
    # had no production caller, so the only supported copy entry is now the
    # public split transaction.
    assert 'c_abi_export("pcc_gc_backend4_relocate_copy_unlocked")' not in strict
    assert "def pcc_gc_backend4_relocate_copy_unlocked(" not in strict
    assert (
        "pcc_gc_backend4_relocate_copy_unlocked"
        not in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    )
    assert "pcc_gc_backend4_relocate_copy_unlocked" not in RUNTIME_SIGNATURES
    assert "_backend4_relocate_copy_unlocked = extern(" not in MANAGED_SOURCE.read_text(
        encoding="utf-8"
    )

    for symbol in (
        "pcc_gc_retain_plan_prepare_locked",
        "pcc_gc_retain_plan_finish",
    ):
        assert symbol in internal
        assert f'@c_abi_export("{symbol}")' in strict_obj
        assert symbol in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
        assert symbol not in RUNTIME_SIGNATURES
        assert symbol not in public_header

    assert "uint64_t opaque[7]" in internal
    assert (
        "pcc_gc_backend4_relocate_copy_preallocated_unlocked"
        in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    )
    assert (
        "pcc_gc_backend4_relocate_copy_preallocated_unlocked"
        not in RUNTIME_SIGNATURES
    )
    assert (
        "pcc_gc_backend4_relocate_copy_preallocated_unlocked"
        not in public_header
    )

    strict_slots = payload.split("def _relocate_copy_slots(", 1)[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_retire_count_slot")', 1
    )[0]
    assert "pcc_gc_retain_plan_prepare_locked(" in strict_slots
    assert "py_incref(" not in strict_slots
    assert strict_slots.index(
        'global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", ctx)'
    ) < strict_slots.index("pcc_gc_visit_object_slots(to_obj")
    assert strict_slots.index("pcc_gc_visit_object_slots(to_obj") < (
        strict_slots.index(
            'global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", null())'
        )
    )

    strict_finish_body = payload.split(
        "def _relocate_slot_pairs_dispose(ctx)", 1
    )[1].split('@c_abi_export("pcc_gc_relocation_payload_count_slot")', 1)[0]
    assert strict_finish_body.count("pcc_gc_retain_plan_finish(") == 1
    assert "while index < count" in strict_finish_body

    strict_commit = strict.split(
        "def pcc_gc_backend4_relocate_copy_preallocated_unlocked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy")', 1)[0]
    assert "pcc_gc_relocation_payload_plan_finish(" not in strict_commit

    strict_public = strict.split(
        '@c_abi_export("pcc_gc_relocate_copy")', 1
    )[1]
    first_unlock = strict_public.index("pcc_py_gc_minor_graph_unlock()")
    plan_prepare = strict_public.index("pcc_gc_relocation_payload_plan_prepare(")
    allocation = strict_public.index("to_obj = pcc_gc_alloc(")
    commit_lock = strict_public.index("pcc_py_gc_minor_graph_lock()", allocation)
    finish_plan_init = strict_public.index("store_ptr(finish_plan, 0, null())")
    validate = strict_public.index(
        "pcc_gc_relocation_payload_plan_validate_locked("
    )
    commit_unlock = strict_public.index(
        "pcc_py_gc_minor_graph_unlock()", commit_lock
    )
    plan_finish = strict_public.index(
        "pcc_gc_relocation_payload_plan_finish(", commit_unlock
    )
    assert strict_public.count("pcc_gc_relocation_payload_plan_prepare(") == 1
    assert (
        strict_public.count("pcc_gc_relocation_payload_plan_validate_locked(")
        == 1
    )
    assert strict_public.count("pcc_gc_relocation_payload_plan_finish(") == 4
    assert first_unlock < plan_prepare < finish_plan_init < commit_lock
    assert commit_lock < validate < commit_unlock
    assert commit_unlock < plan_finish


def test_relocation_copy_preallocates_type_specific_payload_outside_graph_lock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_backend = (
        RUNTIME_DIR / "py" / "py_gc_backend.py"
    ).read_text(encoding="utf-8")
    payload = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
    ).read_text(encoding="utf-8")

    strict_symbols = (
        "pcc_gc_relocation_payload_raw_snapshot_locked",
        "pcc_gc_relocation_payload_raw_prepare",
        "pcc_gc_relocation_payload_raw_validate_locked",
    )
    for symbol in strict_symbols:
        assert f'@c_abi_export("{symbol}")' in payload
        assert symbol in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
        assert symbol not in RUNTIME_SIGNATURES

    strict_raw_snapshot = payload.split(
        "def pcc_gc_relocation_payload_raw_snapshot_locked", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_payload_raw_prepare")', 1
    )[0]
    tag_guard = strict_raw_snapshot.index("if load_i32(from_obj, 8) != tag:")
    assert tag_guard < strict_raw_snapshot.index("memset(ptr_add(ctx, 56), 0, 360)")
    strict_dict_snapshot = strict_raw_snapshot.split(
        'if tag == abi_constant("object.type.dict"):', 1
    )[1].split('if tag == abi_constant("object.type.set"):', 1)[0]
    strict_set_snapshot = strict_raw_snapshot.split(
        'if tag == abi_constant("object.type.set"):', 1
    )[1].split('if tag == abi_constant("object.type.list"):', 1)[0]
    assert strict_dict_snapshot.index("if size < 56:") < (
        strict_dict_snapshot.index("load_i64(from_obj, 16)")
    )
    assert strict_set_snapshot.index("if size < 48:") < (
        strict_set_snapshot.index("load_i64(from_obj, 16)")
    )

    strict_prepared = payload.split(
        "def pcc_gc_relocate_copy_payload_prepared_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy_payload")', 1)[0]
    for forbidden in ("malloc(", "calloc(", "free("):
        assert forbidden not in strict_prepared
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" not in (
        strict_prepared
    )

    strict_raw_publish = payload.split(
        "def _relocate_raw_publish_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocation_payload_fail")', 1)[0]
    strict_span_publish = strict_backend.split(
        "def pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked(",
        1,
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_register_owner_payload_span")',
        1,
    )[0]
    for locked_publish in (strict_raw_publish, strict_span_publish):
        for forbidden in ("malloc(", "calloc(", "free("):
            assert forbidden not in locked_publish
    assert strict_raw_publish.index("memmove(buffer, load_ptr(descriptor, 0)") < (
        strict_raw_publish.index(
            "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked("
        )
    )
    assert strict_raw_publish.index(
        "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked("
    ) < strict_raw_publish.index(
        'if tag == abi_constant("object.type.continuation") and count > 0:'
    )

    strict_public = strict.split(
        '@c_abi_export("pcc_gc_relocate_copy")', 1
    )[1]
    plan = strict_public.index("pcc_gc_relocation_payload_plan_prepare(")
    snapshot_lock = strict_public.index("pcc_py_gc_minor_graph_lock()", plan)
    snapshot = strict_public.index(
        "pcc_gc_relocation_payload_raw_snapshot_locked("
    )
    snapshot_unlock = strict_public.index(
        "pcc_py_gc_minor_graph_unlock()", snapshot_lock
    )
    raw_prepare = strict_public.index("pcc_gc_relocation_payload_raw_prepare(")
    allocation = strict_public.index("to_obj = pcc_gc_alloc(")
    commit_lock = strict_public.index("pcc_py_gc_minor_graph_lock()", allocation)
    raw_validate = strict_public.index(
        "pcc_gc_relocation_payload_raw_validate_locked("
    )
    commit_unlock = strict_public.index(
        "pcc_py_gc_minor_graph_unlock()", commit_lock
    )
    finish = strict_public.index(
        "pcc_gc_relocation_payload_plan_finish(", commit_unlock
    )
    assert strict_public.count(
        "pcc_gc_relocation_payload_plan_finish(payload_plan)"
    ) == 4
    assert (
        plan
        < snapshot_lock
        < snapshot
        < snapshot_unlock
        < raw_prepare
        < allocation
        < commit_lock
        < raw_validate
        < commit_unlock
        < finish
    )


def test_relocation_copy_batch_publishes_bounded_fresh_target_spans() -> None:
    strict_backend = (
        RUNTIME_DIR / "py" / "py_gc_backend.py"
    ).read_text(encoding="utf-8")
    payload = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
    ).read_text(encoding="utf-8")

    symbol = "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked"
    assert symbol in FREESTANDING_GC_CROSS_OBJECT_SIGNATURES
    assert symbol not in RUNTIME_SIGNATURES

    strict_preflight = strict_backend.split(
        "def pcc_gc_backend4_zpage_payload_span_preflight_locked", 1
    )[1].split(
        f'@c_abi_export("{symbol}")', 1
    )[0]
    assert "ptr_is_null(load_ptr(node, 64)) == 0" in strict_preflight
    strict_batch = strict_backend.split(f"def {symbol}", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_register_owner_payload_span")',
        1,
    )[0]
    assert "span_count > 4" in strict_batch
    assert "ptr_is_null(load_ptr(node, 64)) == 0" in strict_batch
    assert "store_ptr(node, 64, span_head)" in strict_batch
    assert "while ptr_is_null(existing)" not in strict_batch

    strict_raw_publish = payload.split(
        "def _relocate_raw_publish_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocation_payload_fail")', 1)[0]
    assert strict_raw_publish.count(symbol + "(") == 1
    assert "span_tail = null()" in strict_raw_publish
    assert "store_ptr(span_tail, 40, span)" in strict_raw_publish
    assert strict_raw_publish.index("store_ptr(span_tail, 40, span)") < (
        strict_raw_publish.index("span_tail = span")
    )
    assert "register_owner_payload_span_preallocated" not in strict_raw_publish


def test_production_archive_has_one_relocation_copy_owner(
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
        assert ":freestanding_gc_relocation_copy.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]
