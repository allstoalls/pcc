from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_forwarding_retirement.py"
STRICT_PAYLOAD_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
)
STRICT_DRAIN_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_relocation_drain.py"
STRICT_DISPATCHER_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_barrier_dispatcher.py"
)
STRICT_IDENTITY_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_forwarding_identity.py"
STRICT_OBJECT_NODES_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
RUNTIME_ABI_SOURCE = REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "runtime_abi.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_drain_parked_pages",
    "pcc_gc_backend4_finish_remap_retirement",
    "pcc_gc_backend4_finish_retained_page_releases",
    "pcc_gc_backend4_note_forwarding_removed_on_page",
    "pcc_gc_backend4_park_page",
    "pcc_gc_backend4_remap_and_retire_unlocked",
    "pcc_gc_backend4_remap_and_retire_stopped_world",
    "pcc_gc_backend4_zpage_note_forwarding_removed",
    "pcc_gc_forwarding_remove",
    "pcc_gc_forwarding_remove_target",
}
LOCAL_HELPER_SYMBOLS = {
    "pcc_gc_backend4_release_retained_pages_unlocked",
    "pcc_gc_forwarding_detach",
    "pcc_gc_forwarding_detach_into_finish",
    "pcc_gc_forwarding_finish_detached",
    "pcc_gc_forwarding_finish_dead_targets",
    "pcc_gc_retire_forwarded_source_into_finish_unlocked",
    "pcc_gc_retire_forwarded_source_unlocked",
}
DEFINED_SYMBOLS = OWNED_SYMBOLS | LOCAL_HELPER_SYMBOLS
RAW_FUNCTION_IMPORTS = {
    "free",
    "pcc_dealloc_cascade_active",
    "pcc_gc_backend",
    "pcc_gc_backend4_remap_referents",
    "pcc_gc_backend4_remap_cext_ctx_valid",
    "pcc_gc_backend4_remap_cext_referents_unlocked",
    "pcc_capi_is_cext_type_tag",
    "pcc_gc_backend4_zpage_clear_active_page",
    "pcc_gc_backend4_zpage_destroy",
    "pcc_gc_backend4_zpage_find_page_for_addr",
    "pcc_gc_backend4_zpage_unlink_page",
    "pcc_gc_forwarding_index_remove",
    "pcc_gc_forwarding_list_head",
    "pcc_gc_forwarding_target_index_remove",
    "pcc_gc_forwarding_target_unlink",
    "pcc_gc_forwarding_unlink_main",
    "pcc_gc_granule_is_object_start",
    "pcc_gc_granule_object_retire",
    "pcc_gc_identity_detach",
    "pcc_gc_identity_finish_detached",
    "pcc_gc_live_bytes_subtract",
    "pcc_gc_managed_pointer_index_remove",
    "pcc_gc_object_index_find",
    "pcc_gc_object_index_remove",
    "pcc_gc_object_list_head",
    "pcc_gc_object_node_freeing",
    "pcc_gc_object_node_finish_detached",
    "pcc_gc_object_node_size",
    "pcc_gc_object_node_unlink",
    "pcc_gc_relocation_finish_source_payloads",
    "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish",
    "pcc_gc_relocation_retire_source_payload_into_finish",
    "pcc_gc_visit_registered_root_slots",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "pcc_py_gc_defer_tripwire",
    "pcc_resume_world",
    "pcc_stop_the_world",
    "pcc_thread_owns_stopped_world",
    "py_decref",
    "py_incref",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend4_deferred_recycle_pages",
    "pcc_gc_backend4_remap_active",
    "pcc_gc_backend4_remap_epoch",
    "pcc_gc_backend4_remap_pending_obj",
    "pcc_gc_backend4_parked_head",
    "pcc_gc_backend4_reseed_page_revision",
    "pcc_gc_backend4_reseed_relocation_revision",
    "pcc_gc_backend4_retained_page_head",
    "pcc_gc_backend_selected",
    "pcc_gc_forwarding_head",
    "pcc_gc_forwarding_population",
    "pcc_gc_object_list_revision",
    "pcc_gc_relocation_set_head",
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


def test_forwarding_retirement_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == DEFINED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(DEFINED_SYMBOLS)
    assert "freestanding_gc_forwarding_retirement" in makefile
    locally_routed = {
        "pcc_gc_backend4_remap_and_retire_unlocked",
        "pcc_gc_backend4_remap_and_retire_stopped_world",
    }
    for symbol in OWNED_SYMBOLS - locally_routed:
        assert f'"{symbol}"' in managed
        assert f'@c_abi_export("{symbol}")' not in managed
    routed_sources = (
        STRICT_DRAIN_SOURCE.read_text(encoding="utf-8")
        + STRICT_DISPATCHER_SOURCE.read_text(encoding="utf-8")
        + strict
    )
    for symbol in locally_routed:
        assert symbol in routed_sources


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_forwarding_retirement_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("forwarding_retirement_" + emitter + ".ll")
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

        source = tmp_path / "forwarding_retirement.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("forwarding_retirement_" + emitter + ".o")
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
    assert defined == DEFINED_SYMBOLS


def test_forwarding_retirement_preserves_one_epoch_and_park_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    assert "old_flags & 131072" in strict
    assert "old_flags | 131072" in strict
    assert "old_flags & ~(2048 | 131072)" in strict
    assert "pcc_gc_visit_registered_root_slots(3, 0)" in strict
    assert "pcc_gc_backend4_drain_parked_pages()" in strict
    assert 'global_store_ptr("pcc_gc_backend4_parked_head", page)' in strict
    assert "load_i64(page, 96)" in strict
    assert "pcc_gc_backend4_zpage_unlink_page(page)" in strict
    assert "pcc_gc_backend4_park_page(page)" in strict
    assert "if ptr_eq(load_ptr(scan, 0), from_obj) != 0:" in strict
    assert "_retire_forwarded_source_into_finish(old, finish)" in strict

    target_path = strict[
        strict.index("def pcc_gc_forwarding_remove_target") :
        strict.index("def pcc_gc_backend4_remap_and_retire_unlocked")
    ]
    assert target_path.index(
        "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish("
    ) < target_path.index("pcc_gc_forwarding_index_remove(from_obj)")
    normal_retirement = strict[
        strict.index("old_flags: i64") :
    ]
    assert normal_retirement.index(
        "pcc_gc_relocation_retire_source_payload_into_finish(old, finish)"
    ) < normal_retirement.index(
        "store_i32(old, 12, old_flags & ~(2048 | 131072))"
    )
    assert normal_retirement.index(
        "pcc_gc_relocation_retire_source_payload_into_finish(old, finish)"
    ) < normal_retirement.index("pcc_gc_forwarding_detach(old)")


def test_forwarding_retirement_counts_only_sweep_deferred_pages() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    start = strict.index("def pcc_gc_backend4_note_forwarding_removed_on_page(")
    note = strict[start:strict.index("\n\n@c_abi_export(", start)]
    # Outside a cascade the page is parked; only flag 1 leaves the counter.
    assert note.index("if pcc_dealloc_cascade_active() == 0:") < note.index("if flag == 1:")
    # Inside one, a page deferred on forwarding (2) moves to the sweep (1).
    assert note.index("elif flag == 2:") < note.index("store_i32(page, 104, 1)")
    assert "store_i64(counter, 0, load_i64(counter, 0) + 1)" in note


def test_forwarding_retirement_releases_only_after_two_remap_epochs() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")

    release_start = strict.index("def _release_retained_pages")
    release_end = strict.index(
        '@c_abi_export("pcc_gc_backend4_finish_retained_page_releases")'
    )
    remap_start = strict.index("def pcc_gc_backend4_remap_and_retire_unlocked")
    remap = strict[remap_start:]
    assert remap.index("_release_retained_pages()") < remap.index(
        "pcc_gc_backend4_drain_parked_pages()"
    )
    release = strict[release_start:release_end]
    assert 'global_store_ptr("pcc_gc_backend4_retained_page_head", null())' in release
    assert "load_i64(page, 32) > 0" in release
    assert "load_i64(page, 88) > 0" in release
    assert "load_i64(page, 96) > 0" in release
    assert "free(span)" not in release
    assert "free(page)" not in release


def test_forwarding_retirement_defers_retained_page_free_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    drain = STRICT_DRAIN_SOURCE.read_text(encoding="utf-8")
    dispatcher = STRICT_DISPATCHER_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    release = strict.split("def _release_retained_pages()", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_finish_retained_page_releases")', 1
    )[0]
    assert "free(" not in release
    assert "return released_pages" in release
    finish = strict.split(
        '@c_abi_export("pcc_gc_backend4_finish_retained_page_releases")', 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert finish.index("free(span)") < finish.index("free(page)")
    remap = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[1]
    assert remap.index("store_ptr(finish, 0, _release_retained_pages())") < remap.index(
        "pcc_gc_backend4_drain_parked_pages()"
    )
    assert "return released_pages" not in remap

    strict_object = drain.split("def _relocate_selected(budget: i64)", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_evacuation_drain")', 1
    )[0]
    strict_page = drain.split(
        "def pcc_gc_backend4_evacuation_page_drain(page_budget: i64)", 1
    )[1]
    strict_step = dispatcher.split('@c_abi_export("pcc_gc_step")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    strict_wrapper = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    assert strict_wrapper.index("pcc_resume_world()") < strict_wrapper.index(
        "pcc_gc_backend4_finish_remap_retirement(finish)"
    )
    for caller in (strict_object, strict_page):
        unlock = caller.rindex(
            "pcc_py_gc_minor_graph_unlock()", 0,
            caller.index("pcc_gc_backend4_remap_and_retire_stopped_world()"),
        )
        remap_call = caller.index(
            "pcc_gc_backend4_remap_and_retire_stopped_world()", unlock
        )
        assert unlock < remap_call
    for caller in (strict_step,):
        assert "pcc_gc_backend4_remap_and_retire_stopped_world()" in caller

    assert (
        '"pcc_gc_backend4_remap_and_retire_unlocked": (_VOID, [_PTR], False)'
        in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_remap_and_retire_stopped_world": (_I64, [], False)'
        in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_retained_page_releases": '
        "(_VOID, [_PTR], False)" in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_remap_and_retire_unlocked": (("c_ptr",), "c_void")'
        in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_retained_page_releases": '
        '(("c_ptr",), "c_void")' in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_remap_retirement": (_VOID, [_PTR], False)'
        in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_remap_retirement": '
        '(("c_ptr",), "c_void")' in runtime_abi
    )


def test_forwarding_retirement_defers_normal_remap_target_decref_after_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    drain = STRICT_DRAIN_SOURCE.read_text(encoding="utf-8")
    dispatcher = STRICT_DISPATCHER_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    strict_remap = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[1]
    assert "pcc_gc_forwarding_remove(old)" not in strict_remap
    assert "dead = pcc_gc_forwarding_detach(old)" in strict_remap
    assert "store_ptr(dead, 16, load_ptr(finish, 8))" in strict_remap
    assert "store_ptr(finish, 8, dead)" in strict_remap

    strict_detach = strict.split("def pcc_gc_forwarding_detach(from_obj)", 1)[
        1
    ].split("\n@c_abi_export", 1)[0]
    assert "py_decref(" not in strict_detach
    assert "free(" not in strict_detach
    assert "return node" in strict_detach
    strict_edge_finish = strict.split(
        "def pcc_gc_forwarding_finish_detached(nodes)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert strict_edge_finish.index("py_decref(target)") < strict_edge_finish.index(
        "free(node)"
    )
    strict_finish = strict.split(
        "def pcc_gc_backend4_finish_remap_retirement(finish)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_backend4_finish_retained_page_releases(" in strict_finish
    assert "pcc_gc_forwarding_finish_detached(" in strict_finish

    strict_object = drain.split("def _relocate_selected(budget: i64)", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_evacuation_drain")', 1
    )[0]
    strict_page = drain.split(
        "def pcc_gc_backend4_evacuation_page_drain(page_budget: i64)", 1
    )[1]
    strict_step = dispatcher.split('@c_abi_export("pcc_gc_step")', 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    for caller in (strict_object, strict_page, strict_step):
        assert "pcc_gc_backend4_remap_and_retire_stopped_world()" in caller
    strict_wrapper = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    strict_plan = strict_wrapper.index("finish = stack_alloc(48)")
    strict_remap_call = strict_wrapper.index(
        "pcc_gc_backend4_remap_and_retire_unlocked(finish)"
    )
    strict_unlock = strict_wrapper.index(
        "pcc_py_gc_minor_graph_unlock()", strict_remap_call
    )
    strict_finish_call = strict_wrapper.index(
        "pcc_gc_backend4_finish_remap_retirement(finish)", strict_unlock
    )
    assert strict_plan < strict_remap_call < strict_unlock < strict_finish_call

    assert (
        '"pcc_gc_backend4_remap_and_retire_unlocked": '
        "(_VOID, [_PTR], False)" in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_remap_retirement": '
        "(_VOID, [_PTR], False)" in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_remap_and_retire_unlocked": '
        '(("c_ptr",), "c_void")' in runtime_abi
    )
    assert (
        '"pcc_gc_backend4_finish_remap_retirement": '
        '(("c_ptr",), "c_void")' in runtime_abi
    )


def test_forwarding_retirement_defers_normal_remap_metadata_node_free_after_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    identity = STRICT_IDENTITY_SOURCE.read_text(encoding="utf-8")
    object_nodes = STRICT_OBJECT_NODES_SOURCE.read_text(encoding="utf-8")
    drain = STRICT_DRAIN_SOURCE.read_text(encoding="utf-8")
    dispatcher = STRICT_DISPATCHER_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    strict_remap = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[1]
    assert "_retire_forwarded_source(old)" not in strict_remap
    assert "_retire_forwarded_source_into_finish(old, finish)" in strict_remap
    strict_metadata = strict.split(
        "def _retire_forwarded_source_into_finish(from_obj, finish)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_identity_remove(" not in strict_metadata
    assert "pcc_gc_object_node_release(" not in strict_metadata
    assert "free(" not in strict_metadata
    assert "identity = pcc_gc_identity_detach(from_obj)" in strict_metadata
    assert "store_ptr(identity, 16, load_ptr(finish, 16))" in strict_metadata
    assert "store_ptr(finish, 16, identity)" in strict_metadata
    assert "store_ptr(dead, 16, load_ptr(finish, 24))" in strict_metadata
    assert "store_ptr(finish, 24, dead)" in strict_metadata

    strict_finish = strict.split(
        "def pcc_gc_backend4_finish_remap_retirement(finish)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_identity_finish_detached(identities)" in strict_finish
    assert "pcc_gc_object_node_finish_detached(object_nodes)" in strict_finish
    identity_detach = identity.split("def pcc_gc_identity_detach(obj: c_ptr)", 1)[
        1
    ].split("\n@c_abi_export", 1)[0]
    assert "free(" not in identity_detach
    identity_finish = identity.split(
        "def pcc_gc_identity_finish_detached(nodes: c_ptr)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "free(node)" in identity_finish
    object_finish = object_nodes.split(
        "def pcc_gc_object_node_finish_detached(nodes: c_ptr)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "free(node)" in object_finish

    strict_wrapper = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    plan = strict_wrapper.index("finish = stack_alloc(48)")
    assert plan < strict_wrapper.index("pcc_py_gc_minor_graph_lock()", plan)

    for symbol in (
        "pcc_gc_identity_detach",
        "pcc_gc_identity_finish_detached",
        "pcc_gc_object_node_finish_detached",
    ):
        assert symbol in runtime_abi


def test_forwarding_retirement_defers_normal_remap_payload_finish_after_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    payload = STRICT_PAYLOAD_SOURCE.read_text(encoding="utf-8")
    drain = STRICT_DRAIN_SOURCE.read_text(encoding="utf-8")
    dispatcher = STRICT_DISPATCHER_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    strict_remap = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[1]
    assert "pcc_gc_relocation_retire_source_payload(old)" not in strict_remap
    assert (
        "pcc_gc_relocation_retire_source_payload_into_finish(old, finish)"
        in strict_remap
    )
    strict_finish = strict.split(
        "def pcc_gc_backend4_finish_remap_retirement(finish)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    payload_finish_call = strict_finish.index(
        "pcc_gc_relocation_finish_source_payloads(payload_plans)"
    )
    forwarding_finish_call = strict_finish.index(
        "pcc_gc_forwarding_finish_detached(forwardings)"
    )
    assert payload_finish_call < forwarding_finish_call

    strict_payload_commit = payload.split(
        "def _retire_source_payload_into_finish(", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_finish_source_payloads")', 1
    )[0]
    assert "pcc_gc_backend4_source_side_table_plan_commit(side_plan)" in (
        strict_payload_commit
    )
    assert "free(raw0)" not in strict_payload_commit
    assert "py_mem_free(owned_buffer)" not in strict_payload_commit
    assert "pcc_gc_backend4_source_side_table_plan_finish(side_plan)" not in (
        strict_payload_commit
    )
    assert "py_decref(" not in strict_payload_commit
    assert "store_ptr(context, 88, load_ptr(finish, 32))" in strict_payload_commit
    assert "store_ptr(finish, 32, context)" in strict_payload_commit

    strict_payload_finish = payload.split(
        "def pcc_gc_relocation_finish_source_payloads(plans)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "free(raw0)" in strict_payload_finish
    assert "py_mem_free(owned_buffer)" in strict_payload_finish
    side_finish = strict_payload_finish.index(
        "pcc_gc_backend4_source_side_table_plan_finish("
    )
    slot_decref = strict_payload_finish.index("py_decref(")
    assert side_finish < slot_decref
    assert strict_payload_finish.index("free(raw3)") < side_finish

    strict_wrapper = strict.split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")', 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    plan = strict_wrapper.index("finish = stack_alloc(48)")
    assert plan < strict_wrapper.index("pcc_py_gc_minor_graph_lock()", plan)

    for symbol in (
        "pcc_gc_relocation_retire_source_payload_into_finish",
        "pcc_gc_relocation_finish_source_payloads",
    ):
        assert symbol in runtime_abi


def test_target_death_detaches_edge_before_deferred_payload_finish() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    payload = STRICT_PAYLOAD_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    strict_target = strict.split(
        "def pcc_gc_forwarding_remove_target(target, finish)", 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")', 1
    )[0]
    prepare = strict_target.index(
        "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish("
    )
    source_index = strict_target.index("pcc_gc_forwarding_index_remove(from_obj)")
    main_unlink = strict_target.index("pcc_gc_forwarding_unlink_main(node)")
    metadata = strict_target.index(
        "_retire_forwarded_source_into_finish(from_obj, finish)"
    )
    chain = strict_target.index("store_ptr(finish, 40, node)")
    assert prepare < source_index < main_unlink < metadata < chain
    assert "pcc_gc_relocation_retire_source_payload(from_obj)" not in strict_target
    assert "_retire_forwarded_source(from_obj)" not in strict_target
    assert "free(node)" not in strict_target

    strict_payload = payload.split(
        "def _retire_source_payload_into_finish(from_obj, finish, decref_exclusion)",
        1,
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_retire_source_payload_into_finish")', 1
    )[0]
    assert "store_ptr(context, 80, decref_exclusion)" in strict_payload
    strict_payload_finish = payload.split(
        "def pcc_gc_relocation_finish_source_payloads(plans)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert (
        "pcc_gc_backend4_source_side_table_plan_finish("
        "side_plan, decref_exclusion)" in strict_payload_finish
    )
    assert "ptr_eq(value, decref_exclusion) == 0" in strict_payload_finish

    strict_finish = strict.split(
        "def pcc_gc_backend4_finish_remap_retirement(finish)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_forwarding_finish_dead_targets(dead_targets)" in strict_finish
    assert strict_finish.index(
        "pcc_gc_relocation_finish_source_payloads(payload_plans)"
    ) < strict_finish.index("pcc_gc_forwarding_finish_dead_targets(dead_targets)")

    strict_note = managed.split("def pcc_gc_note_object_freeing(o)", 1)[1].split(
        '\n@c_abi_export("pcc_gc_reset_relocation_set")', 1
    )[0]
    plan = strict_note.index("finish = stack_alloc(48)")
    lock = strict_note.index("_object_graph_lock()", plan)
    target_remove = strict_note.index("_forwarding_remove_target(o, finish)", lock)
    unlock = strict_note.index("_object_graph_unlock()", target_remove)
    finish = strict_note.index("_backend4_finish_remap_retirement(finish)", unlock)
    assert plan < lock < target_remove < unlock < finish

    assert (
        '"pcc_gc_forwarding_remove_target": (("c_ptr", "c_ptr"), "c_void")'
        in runtime_abi
    )
    assert "pcc_gc_relocation_retire_source_payload_for_target_death_into_finish" in (
        runtime_abi
    )


def test_source_death_defers_live_target_decref_after_graph_unlock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    runtime_abi = RUNTIME_ABI_SOURCE.read_text(encoding="utf-8")

    strict_detach = strict.split(
        "def pcc_gc_forwarding_detach_into_finish(from_obj, finish)", 1
    )[1].split('@c_abi_export("pcc_gc_forwarding_remove")', 1)[0]
    detach = strict_detach.index("pcc_gc_forwarding_detach(from_obj)")
    chain = strict_detach.index("store_ptr(finish, 8, dead)")
    assert detach < chain
    assert "py_decref(" not in strict_detach
    assert "free(" not in strict_detach

    strict_note = managed.split("def pcc_gc_note_object_freeing(o)", 1)[1].split(
        '\n@c_abi_export("pcc_gc_reset_relocation_set")', 1
    )[0]
    plan = strict_note.index("finish = stack_alloc(48)")
    lock = strict_note.index("_object_graph_lock()", plan)
    detach = strict_note.index("_forwarding_detach_into_finish(o, finish)", lock)
    unlock = strict_note.index("_object_graph_unlock()", detach)
    finish = strict_note.index("_backend4_finish_remap_retirement(finish)", unlock)
    assert plan < lock < detach < unlock < finish
    assert "_forwarding_remove(o)" not in strict_note

    assert (
        '"pcc_gc_forwarding_detach_into_finish": '
        '(("c_ptr", "c_ptr"), "c_void")' in runtime_abi
    )


def test_production_archive_has_one_forwarding_retirement_owner(
    pcc_runtime_archive: Path,
) -> None:
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in DEFINED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_forwarding_retirement.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]


def _link_forwarding_retirement_probe(
    tmp_path: Path, name: str, archive: Path
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                PyObject *root_a = 0;
                PyObject *root_b = 0;
                pcc_gc_scheduler_root_register(&root_a);
                pcc_gc_scheduler_root_register(&root_b);
                PyObject *child = py_str_new("x", 1);
                PyObject *a = py_list_new(0);
                PyObject *b = py_list_new(0);
                if (child == 0 || a == 0 || b == 0) return 3;
                pcc_gc_pin(child);
                py_list_append(a, child);
                long long child_before =
                    (long long)((PyObjectHeader *)child)->refcount;
                pcc_gc_store_root(&root_a, a);
                pcc_gc_store_root(&root_b, b);
                pcc_gc_release(a);
                pcc_gc_release(b);

                pcc_gc_reset_relocation_set();
                if (pcc_gc_select_relocation_set(8) != 2) return 4;
                if (pcc_gc_backend4_evacuation_page_drain(1) != 2) return 5;
                long long child_after_copy =
                    (long long)((PyObjectHeader *)child)->refcount;
                printf("%lld\n", (long long)pcc_gc_backend4_forwarding_entries());

                (void)pcc_gc_step(256);
                long long child_after_retire =
                    (long long)((PyObjectHeader *)child)->refcount;
                printf("%lld\n", (long long)pcc_gc_backend4_forwarding_entries());
                (void)pcc_gc_step(256);
                printf("%lld\n", (long long)pcc_gc_backend4_forwarding_entries());
                (void)pcc_gc_step(256);
                printf("%lld,%lld\n",
                       (long long)pcc_gc_backend4_forwarding_entries(),
                       (long long)pcc_gc_backend4_verify_no_old_addresses());
                if (pcc_gc_load_ptr(0, &root_a) == a) return 6;
                if (pcc_gc_load_ptr(0, &root_b) == b) return 7;

                pcc_gc_store_root(&root_a, 0);
                pcc_gc_store_root(&root_b, 0);
                long long child_after_roots =
                    (long long)((PyObjectHeader *)child)->refcount;
                printf("%lld,%lld,%lld,%lld\n",
                       child_before,
                       child_after_copy,
                       child_after_retire,
                       child_after_roots);
                pcc_gc_scheduler_root_unregister(&root_a);
                pcc_gc_scheduler_root_unregister(&root_b);
                pcc_gc_unpin(child);
                pcc_gc_release(child);
                return 0;
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _link_target_death_payload_probe(
    tmp_path: Path, name: str, archive: Path
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdio.h>
            #include <string.h>

            extern void pcc_gc_note_object_freeing(PyObject *obj);

            static int run_control(void) {
                PyObject *child = py_str_new("child", 5);
                PyObject *source = py_list_new(0);
                PyObject *target = py_list_new(0);
                if (child == 0 || source == 0 || target == 0) return 10;
                py_list_append(source, child);
                long long before = (long long)py_header(child)->refcount;
                if (pcc_gc_install_forwarding(source, target) != 0) return 12;

                py_header(target)->refcount = 0;
                py_header(target)->flags |= PY_FLAG_GC_DEALLOCATING;
                pcc_gc_note_object_freeing(target);

                PyListObject *source_list = (PyListObject *)source;
                long long after = (long long)py_header(child)->refcount;
                printf(
                    "%lld,%lld,%lld,%d,%lld,%lld\n",
                    (long long)py_header(target)->refcount,
                    (long long)pcc_gc_backend4_forwarding_entries(),
                    (long long)source_list->length,
                    source_list->items == 0,
                    before,
                    after
                );
                return py_header(target)->refcount == 0
                    && pcc_gc_backend4_forwarding_entries() == 0
                    && source_list->length == 0
                    && source_list->items == 0
                    && before == 2
                    && after == 1
                    ? 0 : 13;
            }

            static int run_self(void) {
                PyObject *source = py_list_new(0);
                PyObject *target = py_list_new(0);
                if (source == 0 || target == 0) return 20;
                py_list_append(source, source);
                if (pcc_gc_install_forwarding(source, target) != 0) return 22;

                py_header(target)->refcount = 0;
                py_header(target)->flags |= PY_FLAG_GC_DEALLOCATING;
                pcc_gc_note_object_freeing(target);

                PyListObject *source_list = (PyListObject *)source;
                printf(
                    "%lld,%lld,%lld,%d\n",
                    (long long)py_header(target)->refcount,
                    (long long)pcc_gc_backend4_forwarding_entries(),
                    (long long)source_list->length,
                    source_list->items == 0
                );
                return py_header(target)->refcount == 0
                    && pcc_gc_backend4_forwarding_entries() == 0
                    && source_list->length == 0
                    && source_list->items == 0
                    ? 0 : 23;
            }

            int main(int argc, char **argv) {
                if (argc != 2) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 3;
                }
                return strcmp(argv[1], "self") == 0
                    ? run_self() : run_control();
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _link_source_death_target_finish_probe(
    tmp_path: Path, name: str, archive: Path
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdio.h>
            #include <string.h>

            extern void pcc_gc_note_object_freeing(PyObject *obj);

            static int run_control(void) {
                PyObject *child = py_str_new("child", 5);
                PyObject *source = py_list_new(0);
                PyObject *target = py_list_new(0);
                if (child == 0 || source == 0 || target == 0) return 10;
                py_list_append(target, child);
                if (pcc_gc_install_forwarding(source, target) != 0) return 11;
                long long before = (long long)py_header(target)->refcount;
                pcc_gc_note_object_freeing(source);
                long long after = (long long)py_header(target)->refcount;
                long long child_live = (long long)py_header(child)->refcount;
                long long forwardings = pcc_gc_backend4_forwarding_entries();
                py_decref(target);
                long long child_dead = (long long)py_header(child)->refcount;
                printf(
                    "%lld,%lld,%lld,%lld,%lld\n",
                    before,
                    after,
                    child_live,
                    child_dead,
                    forwardings
                );
                return before == 2 && after == 1 && child_live == 2
                    && child_dead == 1 && forwardings == 0 ? 0 : 12;
            }

            static int run_last_owner(void) {
                PyObject *child = py_str_new("child", 5);
                PyObject *source = py_list_new(0);
                PyObject *target = py_list_new(0);
                if (child == 0 || source == 0 || target == 0) return 20;
                py_list_append(target, child);
                if (pcc_gc_install_forwarding(source, target) != 0) return 21;
                py_decref(target);
                pcc_gc_note_object_freeing(source);
                printf(
                    "%lld,%lld\n",
                    (long long)pcc_gc_backend4_forwarding_entries(),
                    (long long)py_header(child)->refcount
                );
                return pcc_gc_backend4_forwarding_entries() == 0
                    && py_header(child)->refcount == 1 ? 0 : 22;
            }

            int main(int argc, char **argv) {
                if (argc != 2) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 3;
                }
                return strcmp(argv[1], "last") == 0
                    ? run_last_owner() : run_control();
            }
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_target_death_payload_finish_handles_owned_self_reference(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    archive = pcc_runtime_archive
    executable = _link_target_death_payload_probe(
        tmp_path, "target_death_payload_pcc_python", archive
    )
    control = subprocess.run(
        [str(executable), "control"], capture_output=True, text=True, timeout=30
    )
    assert control.returncode == 0, control.stdout + control.stderr
    assert control.stdout == "0,0,0,1,2,1\n"
    self_ref = subprocess.run(
        [str(executable), "self"], capture_output=True, text=True, timeout=30
    )
    assert self_ref.returncode == 0, self_ref.stdout + self_ref.stderr
    assert self_ref.stdout == "0,0,0,1\n"


def test_source_death_finish_handles_last_owned_target_after_detach(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    archive = pcc_runtime_archive
    executable = _link_source_death_target_finish_probe(
        tmp_path, "source_death_target_pcc_python", archive
    )
    control = subprocess.run(
        [str(executable), "control"], capture_output=True, text=True, timeout=30
    )
    assert control.returncode == 0, control.stdout + control.stderr
    assert control.stdout == "2,1,2,1,0\n"
    last = subprocess.run(
        [str(executable), "last"], capture_output=True, text=True, timeout=30
    )
    assert last.returncode == 0, last.stdout + last.stderr
    assert last.stdout == "0,1\n"


def test_forwarding_retirement_across_three_remap_epochs(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    implementation = _link_forwarding_retirement_probe(
        tmp_path, "forwarding_retirement_pcc_python", pcc_runtime_archive
    )
    result = subprocess.run(
        [str(implementation)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # What the retired C runtime oracle printed.
    assert result.stdout == "2\n0\n0\n0,1\n2,3,2,1\n"
