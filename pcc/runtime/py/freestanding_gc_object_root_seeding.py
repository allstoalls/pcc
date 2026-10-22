"""Object-list mark preparation and current-root graying primitives."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    ptr_is_null,
    store_i32,
)


__pcc_freestanding__ = True


pcc_gc_object_is_address_pinned = extern("pcc_gc_object_is_address_pinned", (c_ptr,), c_int64)


pcc_gc_gray_count_store_release = extern(
    "pcc_gc_gray_count_store_release", (c_int64,), c_void
)
pcc_gc_mark_root_gray_if_known = extern(
    "pcc_gc_mark_root_gray_if_known", (c_ptr,), c_void
)
pcc_gc_visit_registered_root_slots = extern(
    "pcc_gc_visit_registered_root_slots", (c_int64, c_int64), c_int64
)


@c_abi_export("pcc_gc_prepare_object_list_mark")
def pcc_gc_prepare_object_list_mark(explicit_collect: i64) -> None:
    pcc_gc_gray_count_store_release(0)
    node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if load_i64(node, 32) != 0:
            node = nxt
            continue
        obj = load_ptr(node, 0)
        if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
            node = nxt
            continue
        flags: i64 = load_i32(obj, 12)
        if (flags & 16384) != 0 and explicit_collect == 0:
            store_i32(obj, 12, (flags & ~(56 | 16384)) | 32)
        else:
            store_i32(obj, 12, (flags & ~(56 | 16384)) | 8)
        node = nxt


@c_abi_export("pcc_gc_gray_current_roots")
def pcc_gc_gray_current_roots() -> None:
    node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if load_i64(node, 32) != 0:
            node = nxt
            continue
        obj = load_ptr(node, 0)
        if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
            node = nxt
            continue
        flags: i64 = load_i32(obj, 12)
        if pcc_gc_object_is_address_pinned(obj) != 0:
            pcc_gc_mark_root_gray_if_known(obj)
        node = nxt

    pcc_gc_visit_registered_root_slots(1, 1)
