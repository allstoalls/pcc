"""Refcount external-root scan over the raw GC object list."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_void, extern
from pcc.unsafe import (
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    ptr_is_null,
    store_i64,
)


__pcc_freestanding__ = True


pcc_gc_mark_root_gray_if_known = extern(
    "pcc_gc_mark_root_gray_if_known", (c_ptr,), c_void
)
pcc_gc_subtract_referent_refs = extern(
    "pcc_gc_subtract_referent_refs", (c_ptr,), c_void
)


@c_abi_export("pcc_gc_object_node_is_active")
def pcc_gc_object_node_is_active(node) -> i64:
    if ptr_is_null(node) != 0 or load_i64(node, 32) != 0:
        return 0
    obj = load_ptr(node, 0)
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    if (load_i32(obj, 12) & 524288) != 0:
        return 0
    return 1


@c_abi_export("pcc_gc_gray_refcount_external_roots")
def pcc_gc_gray_refcount_external_roots() -> None:
    node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if pcc_gc_object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            store_i64(node, 56, load_i64(obj, 0))
        node = nxt

    node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if pcc_gc_object_node_is_active(node) != 0:
            pcc_gc_subtract_referent_refs(load_ptr(node, 0))
        node = nxt

    node = global_load_ptr("pcc_gc_object_head")
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if pcc_gc_object_node_is_active(node) != 0 and load_i64(node, 56) > 0:
            pcc_gc_mark_root_gray_if_known(load_ptr(node, 0))
        node = nxt
