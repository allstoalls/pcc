"""Backend-0 raw tracked-object list and side-table ownership.

The side table itself is owned by ``freestanding_gc_index_table.py``.  This
module owns the public track/untrack transition and the one shared list-unlink
rule used by both untracking and cycle collection.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_clear,
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_store_i32,
    atomic_store_i64,
    atomic_test_and_set,
    define_global_i8,
    free,
    gc_backend_current,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_ptr,
    malloc,
    null,
    ptr_is_null,
    ptr_diff,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_current_native_thread_token = extern(
    "pcc_current_native_thread_token", (), c_ptr
)
py_gc_index_insert = extern("py_gc_index_insert", (c_ptr, c_ptr), c_int64)
py_gc_index_remove = extern("py_gc_index_remove", (c_ptr,), c_ptr)


define_global_i8("pcc_py_gc_table_lock", 0)


@c_abi_export("pcc_gc_default_table_lock")
def pcc_gc_default_table_lock() -> None:
    lock = global_addr("pcc_py_gc_table_lock")
    while atomic_test_and_set(lock, 0, "acquire") != 0:
        pcc_thread_safepoint()
    atomic_store_i64(
        global_addr("pcc_gc_table_lock_owner_token"),
        0,
        ptr_diff(pcc_current_native_thread_token(), null()),
        "release",
    )


@c_abi_export("pcc_gc_default_table_unlock")
def pcc_gc_default_table_unlock() -> None:
    atomic_store_i64(
        global_addr("pcc_gc_table_lock_owner_token"), 0, 0, "release"
    )
    atomic_clear(global_addr("pcc_py_gc_table_lock"), 0, "release")


@c_abi_export("pcc_gc_default_unlink_tracked_node")
def pcc_gc_default_unlink_tracked_node(node) -> None:
    if ptr_is_null(node):
        return
    previous = load_ptr(node, 24)
    next_node = load_ptr(node, 32)
    if ptr_is_null(previous) == 0:
        store_ptr(previous, 32, next_node)
    else:
        global_store_ptr("py_gc_head", next_node)
    if ptr_is_null(next_node) == 0:
        store_ptr(next_node, 24, previous)
    store_ptr(node, 24, null())
    store_ptr(node, 32, null())
    count_slot = global_addr("py_gc_tracked_count")
    count: i64 = load_i32(count_slot, 0)
    store_i32(count_slot, 0, count - 1)


@c_abi_export("pcc_gc_default_drain_deferred_nodes")
def pcc_gc_default_drain_deferred_nodes() -> None:
    node = global_load_ptr("pcc_gc_deferred_node_free_head")
    global_store_ptr("pcc_gc_deferred_node_free_head", null())
    while ptr_is_null(node) == 0:
        next_node = load_ptr(node, 32)
        free(node)
        node = next_node


@c_abi_export("pcc_gc_tracked_node_recycle")
def _tracked_node_recycle(node) -> None:
    # Caller holds the table lock.
    count_slot = global_addr("pcc_gc_tracked_node_pool_count")
    count: i64 = load_i32(count_slot, 0)
    if count >= 4096:
        free(node)
        return
    store_ptr(node, 0, null())
    store_ptr(node, 24, null())
    store_ptr(node, 32, global_load_ptr("pcc_gc_tracked_node_pool"))
    global_store_ptr("pcc_gc_tracked_node_pool", node)
    store_i32(count_slot, 0, count + 1)


@c_abi_export("pcc_gc_tracked_node_pool_cached_count")
def pcc_gc_tracked_node_pool_cached_count() -> i64:
    pcc_gc_default_table_lock()
    count: i64 = load_i32(global_addr("pcc_gc_tracked_node_pool_count"), 0)
    pcc_gc_default_table_unlock()
    return count


@c_abi_export("pcc_gc_tracked_node_pool_drain")
def pcc_gc_tracked_node_pool_drain() -> None:
    pcc_gc_default_table_lock()
    node = global_load_ptr("pcc_gc_tracked_node_pool")
    global_store_ptr("pcc_gc_tracked_node_pool", null())
    store_i32(global_addr("pcc_gc_tracked_node_pool_count"), 0, 0)
    while ptr_is_null(node) == 0:
        next_node = load_ptr(node, 32)
        free(node)
        node = next_node
    pcc_gc_default_table_unlock()


@c_abi_export("py_gc_track")
def py_gc_track(obj) -> None:
    if ptr_is_null(obj):
        return
    if is_tagged_int(obj):
        return
    if pcc_threads_enabled() != 0 and gc_backend_current() == 4:
        return
    owner_token: i64 = atomic_load_i64(
        global_addr("pcc_gc_table_lock_owner_token"), 0, "acquire"
    )
    collector_owns_lock: i64 = 0
    # Only a held lock has an owner to compare; the thread identity is a
    # thread-local lookup, so the unowned common case skips it.
    if owner_token != 0:
        if owner_token == ptr_diff(pcc_current_native_thread_token(), null()):
            collector_owns_lock: i64 = 1
    if collector_owns_lock == 0:
        pcc_gc_default_table_lock()
    # The table lock owns TRACKED and its node, not color/pin/lifetime bits.
    # Change only TRACKED so concurrent header updates cannot be overwritten.
    flags: i64 = load_i32(obj, 12)
    if (flags & 2) != 0:
        if collector_owns_lock == 0:
            pcc_gc_default_table_unlock()
        return
    node = global_load_ptr("pcc_gc_tracked_node_pool")
    if ptr_is_null(node) == 0:
        global_store_ptr("pcc_gc_tracked_node_pool", load_ptr(node, 32))
        count_slot = global_addr("pcc_gc_tracked_node_pool_count")
        count: i64 = load_i32(count_slot, 0)
        if count > 0:
            store_i32(count_slot, 0, count - 1)
    else:
        node = malloc(40)
    if ptr_is_null(node):
        if collector_owns_lock == 0:
            pcc_gc_default_table_unlock()
        return
    inserted: i64 = py_gc_index_insert(obj, node)
    if inserted == 0:
        _tracked_node_recycle(node)
        atomic_rmw_i32("or", obj, 12, 2, "acq_rel")
        if collector_owns_lock == 0:
            pcc_gc_default_table_unlock()
        return
    if inserted < 0:
        _tracked_node_recycle(node)
        if collector_owns_lock == 0:
            pcc_gc_default_table_unlock()
        return
    store_ptr(node, 0, obj)
    store_i64(node, 8, 0)
    store_i32(node, 16, 0)
    store_ptr(node, 24, null())
    head = global_load_ptr("py_gc_head")
    store_ptr(node, 32, head)
    if ptr_is_null(head) == 0:
        store_ptr(head, 24, node)
    global_store_ptr("py_gc_head", node)
    count_slot = global_addr("py_gc_tracked_count")
    count: i64 = load_i32(count_slot, 0)
    tracked: i64 = count + 1
    store_i32(count_slot, 0, tracked)
    atomic_rmw_i32("or", obj, 12, 2, "acq_rel")
    if collector_owns_lock == 0:
        pcc_gc_default_table_unlock()
    # GC0 and GC3 collect cycles automatically once tracking has grown by
    # max(threshold0, the survivors of the last collection): CPython's young
    # threshold, bounded by heap doubling because each collection scans every
    # tracked object. As in CPython 3.12+ this only schedules the collection;
    # the next application gate of compiled code runs it (pcc_gc_safepoint).
    # gc.disable() and threshold0 == 0 keep it off. GC1/GC2's collector
    # thread begins its marks on the same growth; GC4 steps from allocation
    # debt.
    backend: i64 = gc_backend_current()
    if backend != 0 and backend != 3:
        return
    threshold: i64 = load_i32(global_addr("py_gc_threshold0"), 0)
    if threshold <= 0 or load_i32(global_addr("py_gc_enabled"), 0) == 0:
        return
    live: i64 = atomic_load_i64(
        global_addr("pcc_gc_tracked_after_collect"), 0, "relaxed"
    )
    growth: i64 = threshold
    if live > growth:
        growth = live
    if tracked - live >= growth:
        atomic_store_i32(
            global_addr("pcc_gc_backend4_auto_step_pending"), 0, 1, "release"
        )


@c_abi_export("py_gc_untrack")
def py_gc_untrack(obj) -> None:
    if ptr_is_null(obj):
        return
    if is_tagged_int(obj):
        return
    if pcc_threads_enabled() != 0 and gc_backend_current() == 4:
        return
    owner_token: i64 = atomic_load_i64(
        global_addr("pcc_gc_table_lock_owner_token"), 0, "acquire"
    )
    collector_owns_lock: i64 = 0
    # Only a held lock has an owner to compare; the thread identity is a
    # thread-local lookup, so the unowned common case skips it.
    if owner_token != 0:
        if owner_token == ptr_diff(pcc_current_native_thread_token(), null()):
            collector_owns_lock: i64 = 1
    if collector_owns_lock == 0:
        pcc_gc_default_table_lock()
    flags: i64 = load_i32(obj, 12)
    if (flags & 2) == 0:
        if collector_owns_lock == 0:
            pcc_gc_default_table_unlock()
        return
    node = py_gc_index_remove(obj)
    if ptr_is_null(node) == 0:
        pcc_gc_default_unlink_tracked_node(node)
        if collector_owns_lock != 0:
            store_ptr(node, 0, null())
            store_ptr(node, 24, null())
            deferred = global_load_ptr("pcc_gc_deferred_node_free_head")
            store_ptr(node, 32, deferred)
            global_store_ptr("pcc_gc_deferred_node_free_head", node)
        else:
            _tracked_node_recycle(node)
    atomic_rmw_i32("and", obj, 12, ~2, "acq_rel")
    if collector_owns_lock == 0:
        pcc_gc_default_table_unlock()
