"""Read-only root-registry introspection for the freestanding GC kernel."""

from pcc import i64
from pcc.extern import c_abi_export, c_void, extern
from pcc.unsafe import (
    gc_backend_current,
    global_load_ptr,
    load_i64,
    load_ptr,
    ptr_add,
    ptr_eq,
    ptr_is_null,
)


__pcc_freestanding__ = True


pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)


@c_abi_export("pcc_gc_scheduler_root_count")
def pcc_gc_scheduler_root_count() -> i64:
    gc_backend_current()
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_scheduler_root_head")
    count: i64 = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 8)
    pcc_py_gc_minor_graph_unlock()
    return count


@c_abi_export("pcc_gc_frame_root_slot_count")
def pcc_gc_frame_root_slot_count() -> i64:
    gc_backend_current()
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_frame_head")
    count: i64 = 0
    while ptr_is_null(node) == 0:
        count = count + load_i64(node, 40)
        node = load_ptr(node, 16)
    pcc_py_gc_minor_graph_unlock()
    return count


@c_abi_export("pcc_gc_continuation_root_slot_count")
def pcc_gc_continuation_root_slot_count() -> i64:
    gc_backend_current()
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_continuation_root_head")
    count: i64 = 0
    while ptr_is_null(node) == 0:
        count = count + load_i64(node, 24)
        node = load_ptr(node, 16)
    pcc_py_gc_minor_graph_unlock()
    return count


@c_abi_export("pcc_gc_coroutine_root_score")
def pcc_gc_coroutine_root_score() -> i64:
    return (
        pcc_gc_scheduler_root_count()
        + pcc_gc_frame_root_slot_count()
        + pcc_gc_continuation_root_slot_count()
    )


@c_abi_export("pcc_gc_root_slot_in_span")
def pcc_gc_root_slot_in_span(slot, slots, count: i64) -> i64:
    if ptr_is_null(slot) != 0 or ptr_is_null(slots) != 0 or count <= 0:
        return 0
    i: i64 = 0
    while i < count:
        if ptr_eq(ptr_add(slots, i * 8), slot) != 0:
            return 1
        i = i + 1
    return 0


@c_abi_export("pcc_gc_slot_is_runtime_root")
def pcc_gc_slot_is_runtime_root(slot) -> i64:
    gc_backend_current()
    if ptr_is_null(slot) != 0:
        return 0
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(node) == 0:
        if pcc_gc_root_slot_in_span(slot, load_ptr(node, 8), load_i64(node, 40)) != 0:
            pcc_py_gc_minor_graph_unlock()
            return 1
        node = load_ptr(node, 16)
    node = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(node) == 0:
        if pcc_gc_root_slot_in_span(slot, load_ptr(node, 8), load_i64(node, 24)) != 0:
            pcc_py_gc_minor_graph_unlock()
            return 1
        node = load_ptr(node, 16)
    node = global_load_ptr("pcc_gc_scheduler_root_head")
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 0), slot) != 0:
            pcc_py_gc_minor_graph_unlock()
            return 1
        node = load_ptr(node, 8)
    pcc_py_gc_minor_graph_unlock()
    return 0
