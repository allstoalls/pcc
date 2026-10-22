"""Backend 4 bounded object and page evacuation drains."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    global_addr,
    global_load_ptr,
    load_i32,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_backend4_remap_and_retire_stopped_world = extern(
    "pcc_gc_backend4_remap_and_retire_stopped_world", (), c_int64
)
pcc_gc_backend4_zpage_find = extern(
    "pcc_gc_backend4_zpage_find", (c_ptr,), c_ptr
)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_gc_object_known_size = extern(
    "pcc_gc_object_known_size", (c_ptr,), c_int64
)
pcc_gc_relocate_copy = extern(
    "pcc_gc_relocate_copy", (c_ptr, c_int64), c_ptr
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)


@c_abi_export("pcc_gc_relocation_drain_relocation_set_head")
def _relocation_set_head():
    return global_load_ptr("pcc_gc_relocation_set_head")


@c_abi_export("pcc_gc_relocation_drain_evacuation_page_head")
def _evacuation_page_head():
    return global_load_ptr("pcc_gc_backend4_evacuation_page_head")


@c_abi_export("pcc_gc_relocation_drain_note_incomplete_batch")
def _note_incomplete_batch(moved: i64) -> None:
    if moved <= 0 or ptr_is_null(_relocation_set_head()) != 0:
        return
    incomplete: i64 = load_i32(
        global_addr("pcc_gc_backend4_evacuation_incomplete_batches_count"), 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_evacuation_incomplete_batches_count"),
        0,
        incomplete + 1,
    )


@c_abi_export("pcc_gc_relocation_drain_selected")
def _relocate_selected(budget: i64) -> i64:
    if pcc_gc_backend() != 4 or budget <= 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) != 0:
        return 0
    moved: i64 = 0
    stalled: i64 = 0
    sources = stack_alloc(128)
    while moved < budget and stalled == 0:
        capacity: i64 = budget - moved
        if capacity > 16:
            capacity = 16
        captured: i64 = 0
        pcc_py_gc_minor_graph_lock()
        node = _relocation_set_head()
        while ptr_is_null(node) == 0 and captured < capacity:
            store_ptr(sources, captured * 8, load_ptr(node, 0))
            captured = captured + 1
            node = load_ptr(node, 8)
        pcc_py_gc_minor_graph_unlock()

        batch_moved: i64 = 0
        index: i64 = 0
        while index < captured:
            obj = load_ptr(sources, index * 8)
            to_obj = pcc_gc_relocate_copy(
                obj, pcc_gc_object_known_size(obj)
            )
            if ptr_is_null(to_obj) == 0:
                py_decref(to_obj)
                moved = moved + 1
                batch_moved = batch_moved + 1
            index = index + 1
        if captured == 16:
            pcc_thread_safepoint()
        if captured <= 0 or batch_moved <= 0:
            stalled = 1

    pcc_py_gc_minor_graph_lock()
    _note_incomplete_batch(moved)
    should_remap: i64 = 0
    if (
        ptr_is_null(_relocation_set_head()) != 0
        and load_i32(global_addr("pcc_gc_forwarding_population"), 0) > 0
    ):
        should_remap = 1
    pcc_py_gc_minor_graph_unlock()
    if should_remap != 0:
        pcc_gc_backend4_remap_and_retire_stopped_world()
    return moved


@c_abi_export("pcc_gc_backend4_evacuation_drain")
def pcc_gc_backend4_evacuation_drain(budget: i64) -> i64:
    pcc_gc_config_ensure()
    return _relocate_selected(budget)


@c_abi_export("pcc_gc_relocation_drain_selected_page")
def _relocate_selected_page(page) -> i64:
    if ptr_is_null(page) != 0:
        return 0
    moved: i64 = 0
    sources = stack_alloc(128)
    page_complete: i64 = 0
    stalled: i64 = 0
    while page_complete == 0 and stalled == 0:
        captured: i64 = 0
        examined: i64 = 0
        pcc_py_gc_minor_graph_lock()
        node = _relocation_set_head()
        while (
            ptr_is_null(node) == 0
            and examined < 16
            and captured < 16
        ):
            nxt = load_ptr(node, 8)
            obj = load_ptr(node, 0)
            examined = examined + 1
            znode = pcc_gc_backend4_zpage_find(obj)
            if (
                ptr_is_null(znode) == 0
                and ptr_eq(load_ptr(znode, 8), page) != 0
            ):
                store_ptr(sources, captured * 8, obj)
                captured = captured + 1
            node = nxt
        pcc_py_gc_minor_graph_unlock()

        batch_moved: i64 = 0
        index: i64 = 0
        while index < captured:
            obj = load_ptr(sources, index * 8)
            to_obj = pcc_gc_relocate_copy(
                obj, pcc_gc_object_known_size(obj)
            )
            if ptr_is_null(to_obj) == 0:
                py_decref(to_obj)
                moved = moved + 1
                batch_moved = batch_moved + 1
            index = index + 1
        if captured == 16:
            pcc_thread_safepoint()

        pcc_py_gc_minor_graph_lock()
        head = _evacuation_page_head()
        if ptr_is_null(head) != 0:
            page_complete = 1
        elif ptr_eq(load_ptr(head, 0), page) == 0:
            page_complete = 1
        pcc_py_gc_minor_graph_unlock()
        if captured <= 0 or batch_moved <= 0:
            stalled = 1
    return moved


@c_abi_export("pcc_gc_backend4_evacuation_page_drain")
def pcc_gc_backend4_evacuation_page_drain(page_budget: i64) -> i64:
    backend: i64 = pcc_gc_config_ensure()
    if backend != 4 or page_budget <= 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) != 0:
        return 0
    moved: i64 = 0
    pages: i64 = 0
    while pages < page_budget:
        pcc_py_gc_minor_graph_lock()
        head = _evacuation_page_head()
        if ptr_is_null(head) != 0:
            pcc_py_gc_minor_graph_unlock()
            break
        page = load_ptr(head, 0)
        pcc_py_gc_minor_graph_unlock()
        page_moved: i64 = _relocate_selected_page(page)
        if page_moved <= 0:
            break
        moved = moved + page_moved
        pages = pages + 1
    pcc_py_gc_minor_graph_lock()
    _note_incomplete_batch(moved)
    should_remap: i64 = 0
    if (
        ptr_is_null(_relocation_set_head()) != 0
        and load_i32(global_addr("pcc_gc_forwarding_population"), 0) > 0
    ):
        should_remap = 1
    pcc_py_gc_minor_graph_unlock()
    if should_remap != 0:
        pcc_gc_backend4_remap_and_retire_stopped_world()
    return moved
