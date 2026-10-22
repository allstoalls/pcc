"""Candidate-aware owned-slot clearing shared by cycle and tracing GC."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.runtime.py.py_abi_constants import PY_TYPE_DICT, PY_TYPE_LIST, PY_TYPE_SET, PY_TYPE_TUPLE, PY_TYPE_VTHREAD_CHANNEL
from pcc.unsafe import (
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_is_null,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True

pcc_gc_object_is_address_pinned = extern("pcc_gc_object_is_address_pinned", (c_ptr,), c_int64)


pcc_gc_backend0_is_unreachable = extern(
    "pcc_gc_backend0_is_unreachable", (c_ptr,), c_int64
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_weakref_invalidate = extern("py_weakref_invalidate", (c_ptr,), c_void)


@c_abi_export("pcc_gc_tracing_is_sweep_candidate")
def pcc_gc_tracing_is_sweep_candidate(obj) -> i64:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    if pcc_gc_object_is_known_no_lock(obj) == 0:
        return 0
    flags: i64 = load_i32(obj, 12)
    return 1 if (flags & 1024) != 0 and pcc_gc_object_is_address_pinned(obj) == 0 else 0


@c_abi_export("pcc_gc_backend0_clear_slot")
def pcc_gc_backend0_clear_slot(slot, role: i64, context) -> None:
    if role != 1:  # only owned references are cleared/decref'd
        return
    child = load_ptr(slot, 0)
    store_ptr(slot, 0, null())
    if ptr_is_null(child) != 0 or is_tagged_int(child) != 0:
        return
    if pcc_gc_backend0_is_unreachable(child) != 0:
        return
    py_decref(child)


@c_abi_export("pcc_gc_tracing_clear_slot")
def pcc_gc_tracing_clear_slot(slot, role: i64, context) -> None:
    if role != 1:  # only owned references are cleared/decref'd
        return
    child = load_ptr(slot, 0)
    store_ptr(slot, 0, null())
    if ptr_is_null(child) != 0 or is_tagged_int(child) != 0:
        return
    if pcc_gc_tracing_is_sweep_candidate(child) != 0:
        return
    py_decref(child)


@c_abi_export("pcc_gc_clear_container_metadata")
def pcc_gc_clear_container_metadata(obj, tag: i64) -> None:
    if tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE:
        store_i64(obj, 16, 0)
        return
    if tag == PY_TYPE_DICT:
        entries = load_ptr(obj, 40)
        if ptr_is_null(entries) == 0:
            used: i64 = load_i64(obj, 48)
            index: i64 = 0
            while index < used:
                store_i64(entries, index * 24, 0)
                index = index + 1
        indices = load_ptr(obj, 32)
        if ptr_is_null(indices) == 0:
            capacity: i64 = load_i64(obj, 24)
            index: i64 = 0
            while index < capacity:
                store_i64(indices, index * 8, -1)
                index = index + 1
        store_i64(obj, 16, 0)
        store_i64(obj, 48, 0)
        return
    if tag == PY_TYPE_SET:
        entries = load_ptr(obj, 40)
        if ptr_is_null(entries) == 0:
            capacity = load_i64(obj, 24)
            index: i64 = 0
            while index < capacity:
                store_ptr(entries, index * 16 + 8, null())
                store_i64(entries, index * 16, 0)
                index = index + 1
        store_i64(obj, 16, 0)
        store_i64(obj, 32, 0)
        return
    if tag == PY_TYPE_VTHREAD_CHANNEL:
        kind: i64 = load_i64(obj, 16)
        if kind == 0:
            store_i64(obj, 32, 0)
            store_i64(obj, 40, 0)
            store_i64(obj, 48, 0)
        elif kind == 1 or kind == 2:
            store_i64(obj, 32, 1)


@c_abi_export("pcc_gc_backend0_clear_referents")
def pcc_gc_backend0_clear_referents(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    tag: i64 = load_i32(obj, 8)
    handled: i64 = pcc_gc_visit_object_slots(
        obj, pcc_gc_backend0_clear_slot, null()
    )
    if handled != 0:
        pcc_gc_clear_container_metadata(obj, tag)


@c_abi_export("pcc_gc_tracing_clear_referents")
def pcc_gc_tracing_clear_referents(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    tag: i64 = load_i32(obj, 8)
    handled: i64 = pcc_gc_visit_object_slots(
        obj, pcc_gc_tracing_clear_slot, null()
    )
    if handled != 0:
        pcc_gc_clear_container_metadata(obj, tag)


@c_abi_export("pcc_gc_tracing_clear_unreachable")
def pcc_gc_tracing_clear_unreachable(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if pcc_gc_object_is_address_pinned(obj) != 0:
        return
    py_weakref_invalidate(obj)
    pcc_gc_tracing_clear_referents(obj)
