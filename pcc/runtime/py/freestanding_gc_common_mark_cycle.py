"""Raw tracing mark-cycle shared by GC backends 1 through 4."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_rmw_i64,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
)


__pcc_freestanding__ = True


pcc_gc_gray_count_decrement_acq_rel = extern(
    "pcc_gc_gray_count_decrement_acq_rel", (), c_void
)
pcc_gc_gray_count_increment_acq_rel = extern(
    "pcc_gc_gray_count_increment_acq_rel", (), c_void
)
pcc_gc_gray_count_store_release = extern(
    "pcc_gc_gray_count_store_release", (c_int64,), c_void
)
pcc_gc_gray_current_roots = extern("pcc_gc_gray_current_roots", (), c_void)
pcc_gc_gray_refcount_external_roots = extern(
    "pcc_gc_gray_refcount_external_roots", (), c_void
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_forwarding_index_find = extern(
    "pcc_gc_forwarding_index_find", (c_ptr,), c_ptr
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_prepare_object_list_mark = extern(
    "pcc_gc_prepare_object_list_mark", (c_int64,), c_void
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_gc_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_gc_set_mark_color = extern("pcc_gc_set_mark_color", (c_ptr, c_int64), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


@c_abi_export("pcc_gc_trace_mark_gray_if_known")
def pcc_gc_trace_mark_gray_if_known(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    forwarding = pcc_gc_forwarding_index_find(obj)
    if ptr_is_null(forwarding) == 0:
        resolved = load_ptr(forwarding, 8)
        if ptr_is_null(resolved) == 0:
            if ptr_eq(resolved, obj) == 0:
                obj = resolved
    if pcc_gc_object_is_known_no_lock(obj) == 0:
        return
    flags: i64 = load_i32(obj, 12)
    if (flags & 32) == 0:
        if (flags & 16) == 0:
            pcc_gc_gray_count_increment_acq_rel()
        pcc_gc_set_mark_color(obj, 16)


@c_abi_export("pcc_gc_trace_slot")
def pcc_gc_trace_slot(slot, role: i64, context) -> None:
    if role == 3:  # borrowed update-only metadata is not a graph edge
        return
    child = pcc_gc_load_ptr(null(), slot)
    pcc_gc_trace_mark_gray_if_known(child)


@c_abi_export("pcc_gc_trace_cext_slot_transaction")
def pcc_gc_trace_cext_slot_transaction(slot, role: i64, context) -> None:
    if role == 3 or ptr_is_null(context) != 0:
        return
    obj = load_ptr(context, 0)
    epoch: i64 = load_i64(context, 8)
    backend: i64 = load_i64(context, 16)
    pcc_py_gc_minor_graph_lock()
    if (
        ptr_eq(global_load_ptr("pcc_gc_trace_cext_pending_obj"), obj) != 0
        and load_i64(global_addr("pcc_gc_trace_cext_pending_epoch"), 0)
        == epoch
        and load_i64(global_addr("pcc_gc_trace_cext_pending_backend"), 0)
        == backend
        and load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0) == epoch
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) == backend
        and load_i32(global_addr("pcc_gc_mark_active"), 0) != 0
    ):
        pcc_gc_trace_slot(slot, role, null())
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_trace_cext_referents_unlocked")
def pcc_gc_trace_cext_referents_unlocked(obj, context) -> None:
    pcc_gc_visit_object_slots(
        obj, pcc_gc_trace_cext_slot_transaction, context
    )


@c_abi_export("pcc_gc_trace_referents")
def pcc_gc_trace_referents(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    # A dying object (count reached zero; flag 524288) is being torn down
    # by its releasing thread, possibly parked at a safepoint inside its
    # deallocation: its slots may already name freed children. No collector
    # pass reads them; the releasing thread finishes the object.
    if (load_i32(obj, 12) & 524288) != 0:
        return
    pcc_gc_visit_object_slots(obj, pcc_gc_trace_slot, null())


@c_abi_export("pcc_gc_seed_roots")
def pcc_gc_seed_roots() -> None:
    explicit_collect: i64 = load_i32(
        global_addr("pcc_gc_explicit_collect_active"),
        0,
    )
    pcc_gc_prepare_object_list_mark(explicit_collect)
    pcc_gc_gray_refcount_external_roots()
    pcc_gc_gray_current_roots()


@c_abi_export("pcc_gc_drain_all_gray_unlocked")
def pcc_gc_drain_all_gray_unlocked() -> i64:
    processed: i64 = 0
    while True:
        local_processed: i64 = 0
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
            if (flags & 16) != 0:
                pcc_gc_trace_referents(obj)
                pcc_gc_gray_count_decrement_acq_rel()
                pcc_gc_set_mark_color(obj, 32)
                local_processed = local_processed + 1
                processed = processed + 1
            node = nxt
        if local_processed == 0:
            break
    return processed


@c_abi_export("pcc_gc_drain_all_gray_locked_slice")
def pcc_gc_drain_all_gray_locked_slice() -> i64:
    processed: i64 = 0
    while True:
        local_processed: i64 = 0
        node = global_load_ptr("pcc_gc_object_head")
        while ptr_is_null(node) == 0:
            nxt = load_ptr(node, 16)
            if pcc_gc_object_node_is_active(node) == 0:
                node = nxt
                continue
            obj = load_ptr(node, 0)
            flags: i64 = load_i32(obj, 12)
            if (flags & 16) != 0:
                if pcc_capi_is_cext_type_tag(load_i32(obj, 8)) != 0:
                    if ptr_is_null(
                        global_load_ptr("pcc_gc_trace_cext_pending_obj")
                    ) != 0:
                        py_incref(obj)
                        global_store_ptr(
                            "pcc_gc_trace_cext_pending_obj", obj
                        )
                        store_i64(
                            global_addr("pcc_gc_trace_cext_pending_epoch"),
                            0,
                            load_i64(
                                global_addr("pcc_gc_tracing_cycle_epoch"), 0
                            ),
                        )
                        store_i64(
                            global_addr("pcc_gc_trace_cext_pending_backend"),
                            0,
                            load_i32(
                                global_addr("pcc_gc_backend_selected"), 0
                            ),
                        )
                        return processed + 1
                pcc_gc_trace_referents(obj)
                pcc_gc_gray_count_decrement_acq_rel()
                pcc_gc_set_mark_color(obj, 32)
                local_processed = local_processed + 1
                processed = processed + 1
            node = nxt
        if local_processed == 0:
            break
    return processed


@c_abi_export("pcc_gc_begin_mark_cycle")
def pcc_gc_begin_mark_cycle() -> None:
    if load_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0
    ) != 0:
        return
    if pcc_gc_tracing_cycle_epoch_advance_unlocked() == 0:
        return
    store_i64(
        global_addr("pcc_gc_trace_extension_roots_epoch"),
        0,
        load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0),
    )
    store_i64(
        global_addr("pcc_gc_trace_extension_roots_backend"),
        0,
        load_i32(global_addr("pcc_gc_backend_selected"), 0),
    )
    store_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0, 4
    )


@c_abi_export("pcc_gc_tracing_cycle_epoch_advance_unlocked")
def pcc_gc_tracing_cycle_epoch_advance_unlocked() -> i64:
    current: i64 = load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
    if current < 0:
        pcc_platform_abort()
        return 0
    if current == 9223372036854775807:
        pcc_platform_abort()
        return 0
    next_epoch: i64 = current + 1
    store_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0, next_epoch)
    return next_epoch


@c_abi_export("pcc_gc_tracing_finish_claim_clear_unlocked")
def pcc_gc_tracing_finish_claim_clear_unlocked(
    claim_epoch: i64, claim_backend: i64
) -> None:
    if (
        load_i64(global_addr("pcc_gc_tracing_finish_claim_epoch"), 0)
        != claim_epoch
        or load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        )
        != claim_backend
    ):
        return
    store_i64(global_addr("pcc_gc_tracing_finish_claim_epoch"), 0, 0)
    store_i64(global_addr("pcc_gc_tracing_finish_claim_backend"), 0, -1)


@c_abi_export("pcc_gc_finish_tracing_cycle")
def pcc_gc_finish_tracing_cycle(
    claim_epoch: i64, claim_backend: i64
) -> i64:
    if (
        load_i64(global_addr("pcc_gc_tracing_finish_claim_epoch"), 0)
        != claim_epoch
        or load_i64(
            global_addr("pcc_gc_tracing_finish_claim_backend"), 0
        )
        != claim_backend
    ):
        return 0
    if (
        load_i64(global_addr("pcc_gc_tracing_cycle_epoch"), 0)
        != claim_epoch
        or load_i32(global_addr("pcc_gc_backend_selected"), 0)
        != claim_backend
        or load_i32(global_addr("pcc_gc_mark_active"), 0) == 0
    ):
        pcc_gc_tracing_finish_claim_clear_unlocked(
            claim_epoch, claim_backend
        )
        return 0
    commits: i64 = load_i64(
        global_addr("pcc_gc_tracing_finish_commits"), 0
    )
    if commits < 0 or commits == 9223372036854775807:
        pcc_platform_abort()
        return 0
    marked_bytes: i64 = 0
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
        if (flags & 8) != 0:
            store_i32(obj, 12, flags | 1024)
        else:
            store_i32(obj, 12, flags & ~1024)
            # The node records the object's size at offset 8.
            marked_bytes = marked_bytes + load_i64(node, 8)
        node = nxt
    store_i64(global_addr("pcc_gc_tracing_marked_bytes"), 0, marked_bytes)
    # A completed cycle pays the allocation debt, as in Lua and Go: the next
    # begins after another threshold of allocation. Paid only by traced
    # work, one large allocation into a small heap kept GC4 stepping at
    # every gate.
    atomic_rmw_i64("xchg", global_addr("pcc_gc_debt_bytes"), 0, 0, "acq_rel")
    global_store_ptr("pcc_gc_trace_cursor", null())
    # The scheduler owns the release-store helper for this shared counter.
    # Its ABI is already part of the common tracing surface.
    pcc_gc_gray_count_store_release(0)
    store_i32(global_addr("pcc_gc_mark_active"), 0, 0)
    store_i32(
        global_addr("pcc_gc_trace_extension_roots_pending"), 0, 0
    )
    store_i64(global_addr("pcc_gc_trace_extension_roots_epoch"), 0, 0)
    store_i64(global_addr("pcc_gc_trace_extension_roots_backend"), 0, -1)
    # Preserve cycle_requested: roots/barriers/reset may have published work
    # while this claimant was waiting for the stopped-world cut.
    store_i64(
        global_addr("pcc_gc_tracing_finish_commits"), 0, commits + 1
    )
    pcc_gc_tracing_finish_claim_clear_unlocked(claim_epoch, claim_backend)
    return 1
