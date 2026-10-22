"""Freestanding TLS and graph-lock substrate for the pcc-Python runtime."""

from pcc import i64
from pcc.extern import (
    c_abi_export,
    c_abi_typed_export,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_store_i32,
    define_global_i32,
    define_thread_local_i32,
    define_thread_local_ptr_null,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i32,
    null,
    ptr_is_null,
    store_i32,
    thread_safepoint,
)


__pcc_freestanding__ = True


define_thread_local_ptr_null("g_tls_pcc_py_gc_minor_current")
define_thread_local_ptr_null("g_tls_pcc_py_gc_pending_minor_block")
define_thread_local_i32("g_tls_pcc_py_gc_minor_graph_lock_depth", 0)
define_global_i32("g_pcc_py_gc_minor_graph_lock", 0)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_thread_no_park_enter = extern("pcc_thread_no_park_enter", (), c_void)
pcc_thread_no_park_exit = extern("pcc_thread_no_park_exit", (), c_void)


define_thread_local_ptr_null("g_tls_pcc_py_gc_deferred_tripwire_message")
define_thread_local_ptr_null("g_tls_pcc_py_gc_deferred_tripwire_file")
define_thread_local_i32("g_tls_pcc_py_gc_deferred_tripwire_line", 0)
pcc_runtime_tripwire_fail = extern(
    "pcc_runtime_tripwire_fail", (c_ptr, c_ptr, c_int32), c_void
)


@c_abi_typed_export(
    "pcc_py_gc_defer_tripwire", "void", ("ptr", "ptr", "i32")
)
def pcc_py_gc_defer_tripwire(msg, file, line: i64) -> None:
    """Mirror of py_gc_backend.c's deferred slot: a graph-lock owner records
    its first invariant violation here instead of entering the runtime
    log/abort sink while locked; the outermost unlock reports it."""
    if (
        ptr_is_null(
            global_load_ptr("g_tls_pcc_py_gc_deferred_tripwire_message")
        )
        == 0
    ):
        return
    global_store_ptr("g_tls_pcc_py_gc_deferred_tripwire_message", msg)
    global_store_ptr("g_tls_pcc_py_gc_deferred_tripwire_file", file)
    store_i32(global_addr("g_tls_pcc_py_gc_deferred_tripwire_line"), 0, line)


@c_abi_export("pcc_py_gc_finish_deferred_tripwire")
def _finish_deferred_tripwire() -> None:
    msg = global_load_ptr("g_tls_pcc_py_gc_deferred_tripwire_message")
    if ptr_is_null(msg) != 0:
        return
    file = global_load_ptr("g_tls_pcc_py_gc_deferred_tripwire_file")
    line: i64 = load_i32(
        global_addr("g_tls_pcc_py_gc_deferred_tripwire_line"), 0
    )
    global_store_ptr("g_tls_pcc_py_gc_deferred_tripwire_message", null())
    global_store_ptr("g_tls_pcc_py_gc_deferred_tripwire_file", null())
    store_i32(global_addr("g_tls_pcc_py_gc_deferred_tripwire_line"), 0, 0)
    pcc_runtime_tripwire_fail(msg, file, line)


@c_abi_typed_export(
    "pcc_gc_tripwire_defer_or_fail", "i32", ("ptr", "ptr", "i32")
)
def pcc_gc_tripwire_defer_or_fail(msg, file, line: i64) -> i64:
    """Strict-archive owner of py_gc_backend.c's cross-TU mixed-tripwire
    seam.  C helper files that have no port (py_cpy_handle.c) are compiled
    into the production archive while py_gc_backend.o is replaced by this
    port, so the seam must be resolvable from here too.  Same contract as
    the C oracle: a graph-lock owner records the first violation and returns
    1 so the caller bails, an unlocked caller enters the fatal sink."""
    if pcc_threads_enabled() == 0:
        pcc_runtime_tripwire_fail(msg, file, line)
        return 0
    depth: i64 = load_i32(
        global_addr("g_tls_pcc_py_gc_minor_graph_lock_depth"), 0
    )
    if depth > 0:
        pcc_py_gc_defer_tripwire(msg, file, line)
        return 1
    pcc_runtime_tripwire_fail(msg, file, line)
    return 0


@c_abi_export("pcc_py_gc_minor_current_get")
def pcc_py_gc_minor_current_get() -> c_ptr:
    return global_load_ptr("g_tls_pcc_py_gc_minor_current")


@c_abi_export("pcc_py_gc_minor_current_set")
def pcc_py_gc_minor_current_set(block: c_ptr) -> None:
    global_store_ptr("g_tls_pcc_py_gc_minor_current", block)


@c_abi_export("pcc_py_gc_pending_minor_block_get")
def pcc_py_gc_pending_minor_block_get() -> c_ptr:
    return global_load_ptr("g_tls_pcc_py_gc_pending_minor_block")


@c_abi_export("pcc_py_gc_pending_minor_block_set")
def pcc_py_gc_pending_minor_block_set(block: c_ptr) -> None:
    global_store_ptr("g_tls_pcc_py_gc_pending_minor_block", block)


@c_abi_export("pcc_py_gc_minor_graph_lock_depth")
def pcc_py_gc_minor_graph_lock_depth() -> i64:
    """This thread's graph-lock nesting depth (0 when the lock is elided)."""
    if pcc_threads_enabled() == 0:
        return 0
    return load_i32(global_addr("g_tls_pcc_py_gc_minor_graph_lock_depth"), 0)


@c_abi_export("pcc_py_gc_minor_graph_lock")
def pcc_py_gc_minor_graph_lock() -> None:
    # Mirror parity with the C oracle's compile-time elision
    # (py_runtime_high_substrate.c: `#if !PCC_WITH_THREADS return;`).  Under
    # the threads-off kernel no second thread can exist, so the lock's mutual
    # exclusion is vacuous; the pthread kernel module returns non-zero and
    # keeps the full body live for threaded builds.
    if pcc_threads_enabled() == 0:
        return
    # Take the thread-local's address once.  Every `global_addr` on a
    # thread-local is a `_tlv_get_addr` call on Darwin, and this pair was the
    # single hottest leaf in a `pcc1 -> pcc2` frontend worker (~261 of ~403
    # tlv samples).  The re-entrant fast path below is the common case and
    # used to pay for two lookups where one does.
    depth_slot = global_addr("g_tls_pcc_py_gc_minor_graph_lock_depth")
    depth: i64 = load_i32(depth_slot, 0)
    if depth > 0:
        store_i32(depth_slot, 0, depth + 1)
        return

    if pcc_current_thread_id() <= 0:
        pcc_platform_abort()
        return

    acquired: i64 = 0
    while acquired == 0:
        old: i64 = atomic_cas_i32(
            global_addr("g_pcc_py_gc_minor_graph_lock"),
            0,
            0,
            1,
            "acq_rel",
            "acquire",
        )
        if old == 0:
            acquired: i64 = 1
        else:
            thread_safepoint()
    pcc_thread_no_park_enter()
    store_i32(depth_slot, 0, 1)


@c_abi_export("pcc_py_gc_minor_graph_unlock")
def pcc_py_gc_minor_graph_unlock() -> None:
    if pcc_threads_enabled() == 0:
        _finish_deferred_tripwire()
        return
    depth_slot = global_addr("g_tls_pcc_py_gc_minor_graph_lock_depth")
    depth: i64 = load_i32(depth_slot, 0)
    if depth <= 0:
        return
    depth = depth - 1
    store_i32(depth_slot, 0, depth)
    if depth == 0:
        # Physical release first, then the pending fatal report (mirror of
        # pcc_gc_graph_unlock's finish-after-release ordering).
        atomic_store_i32(
            global_addr("g_pcc_py_gc_minor_graph_lock"),
            0,
            0,
            "release",
        )
        _finish_deferred_tripwire()
        pcc_thread_no_park_exit()
