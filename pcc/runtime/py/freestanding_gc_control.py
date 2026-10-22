"""Raw public GC control and introspection ABI.

This module owns the state-only portion of ``py_obj_gc`` without depending on
managed Python objects or collector graph traversal.  The state definitions
live in ``freestanding_gc_state.py``; this object provides their stable public
C ABI.
"""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    global_addr,
    is_tagged_int,
    load_i32,
    ptr_is_null,
    store_i32,
)


__pcc_freestanding__ = True


@c_abi_export("py_gc_init")
def py_gc_init() -> None:
    store_i32(global_addr("py_gc_enabled"), 0, 1)


@c_abi_export("py_gc_enable")
def py_gc_enable() -> None:
    store_i32(global_addr("py_gc_enabled"), 0, 1)


@c_abi_export("py_gc_disable")
def py_gc_disable() -> None:
    store_i32(global_addr("py_gc_enabled"), 0, 0)


@c_abi_export("py_gc_is_enabled")
def py_gc_is_enabled() -> i64:
    if load_i32(global_addr("py_gc_enabled"), 0) != 0:
        return 1
    return 0


@c_abi_export("py_gc_is_tracked")
def py_gc_is_tracked(o) -> i64:
    if ptr_is_null(o):
        return 0
    if is_tagged_int(o):
        return 0
    flags: i64 = load_i32(o, 12)
    if (flags & 2) != 0:
        return 1
    return 0


@c_abi_export("py_gc_get_count")
def py_gc_get_count(generation: i64) -> i64:
    if generation == 0:
        return load_i32(global_addr("py_gc_tracked_count"), 0)
    return 0


@c_abi_export("py_gc_get_threshold")
def py_gc_get_threshold(generation: i64) -> i64:
    if generation == 0:
        return load_i32(global_addr("py_gc_threshold0"), 0)
    if generation == 1:
        return load_i32(global_addr("py_gc_threshold1"), 0)
    if generation == 2:
        return load_i32(global_addr("py_gc_threshold2"), 0)
    return 0


@c_abi_export("py_gc_set_threshold")
def py_gc_set_threshold(gen0: i64, gen1: i64, gen2: i64) -> None:
    if gen0 >= 0:
        store_i32(global_addr("py_gc_threshold0"), 0, gen0)
    if gen1 >= 0:
        store_i32(global_addr("py_gc_threshold1"), 0, gen1)
    if gen2 >= 0:
        store_i32(global_addr("py_gc_threshold2"), 0, gen2)


@c_abi_export("py_gc_freeze")
def py_gc_freeze() -> None:
    tracked: i64 = load_i32(global_addr("py_gc_tracked_count"), 0)
    if tracked <= 0:
        tracked: i64 = 1
    store_i32(global_addr("py_gc_freeze_count"), 0, tracked)


@c_abi_export("py_gc_unfreeze")
def py_gc_unfreeze() -> None:
    store_i32(global_addr("py_gc_freeze_count"), 0, 0)


@c_abi_export("py_gc_get_freeze_count")
def py_gc_get_freeze_count() -> i64:
    return load_i32(global_addr("py_gc_freeze_count"), 0)
