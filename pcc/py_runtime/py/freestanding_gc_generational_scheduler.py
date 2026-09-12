"""Backend 3/4 root promotion and budgeted young-list scheduling."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_load_i32,
    atomic_rmw_i32,
    free,
    function_addr,
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
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend3_drain_remembered_owners = extern(
    "pcc_gc_backend3_drain_remembered_owners",
    (c_int64, c_ptr),
    c_int64
)
pcc_capi_visit_extension_module_state_roots = extern(
    "pcc_capi_visit_extension_module_state_roots", (c_ptr, c_ptr), c_void
)
pcc_gc_backend3_finish_detached_remembered_owners = extern(
    "pcc_gc_backend3_finish_detached_remembered_owners", (c_ptr,), c_void
)
pcc_gc_backend3_drain_promotion_worklist = extern("pcc_gc_backend3_drain_promotion_worklist", (c_int64,), c_int64)
pcc_gc_backend3_young_link_head = extern(
    "pcc_gc_backend3_young_link_head", (c_ptr,), c_void
)
pcc_gc_backend3_young_list_head = extern(
    "pcc_gc_backend3_young_list_head", (), c_ptr
)
pcc_gc_backend3_young_unlink = extern(
    "pcc_gc_backend3_young_unlink", (c_ptr,), c_void
)
pcc_gc_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
pcc_gc_generational_oldify_copy = extern(
    "pcc_gc_generational_oldify_copy", (c_ptr,), c_ptr
)
pcc_gc_generational_promote_young_if_known = extern(
    "pcc_gc_generational_promote_young_if_known", (c_ptr,), c_void
)
pcc_gc_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_generational_promote_owned_slot_mode = extern(
    "pcc_gc_generational_promote_owned_slot_mode",
    (c_ptr, c_int64, c_int64),
    c_void
)
pcc_gc_trace_referents_for_promotion = extern(
    "pcc_gc_trace_referents_for_promotion", (c_ptr,), c_void
)
pcc_gc_visit_mapped_root_slot = extern(
    "pcc_gc_visit_mapped_root_slot",
    (c_ptr, c_int64, c_ptr, c_int64, c_int64, c_int64),
    c_int64
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_tls_exc_get = extern("py_tls_exc_get", (), c_ptr)
py_tls_exc_set = extern("py_tls_exc_set", (c_ptr,), c_void)
py_subs_exc_cache_slot = extern(
    "py_subs_exc_cache_slot", (c_int64,), c_ptr
)


@c_abi_export("pcc_gc_backend3_frame_root_scan_reset_locked")
def _reset_frame_root_scan() -> None:
    store_i32(global_addr("pcc_gc_backend3_frame_root_scan_phase"), 0, 0)
    store_i64(global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0, -1)
    global_store_ptr("pcc_gc_backend3_frame_root_scan_cursor", null())
    global_store_ptr(
        "pcc_gc_backend3_continuation_root_scan_cursor", null()
    )


@c_abi_export("pcc_gc_backend3_scheduler_root_scan_reset_locked")
def _reset_scheduler_root_scan() -> None:
    store_i32(
        global_addr("pcc_gc_backend3_scheduler_root_scan_phase"), 0, 0
    )
    store_i64(
        global_addr("pcc_gc_backend3_scheduler_root_scan_slot"), 0, -1
    )
    global_store_ptr("pcc_gc_backend3_scheduler_root_scan_cursor", null())


@c_abi_export("pcc_gc_generational_promote_frame_roots")
def pcc_gc_generational_promote_frame_roots(remaining_budget: i64) -> None:
    if remaining_budget <= 0:
        return
    pcc_py_gc_minor_graph_lock()
    revision: i64 = 0
    examined: i64 = 0
    while examined < remaining_budget:
        phase: i64 = load_i32(
            global_addr("pcc_gc_backend3_frame_root_scan_phase"), 0
        )
        slot_index: i64 = load_i64(
            global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0
        )
        if phase == 0:
            if slot_index < 0:
                global_store_ptr(
                    "pcc_gc_backend3_frame_root_scan_cursor",
                    global_load_ptr("pcc_gc_frame_head"),
                )
                store_i64(
                    global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                    0,
                    0,
                )
                slot_index = 0
            frame = global_load_ptr("pcc_gc_backend3_frame_root_scan_cursor")
            if ptr_is_null(frame) != 0:
                store_i32(
                    global_addr("pcc_gc_backend3_frame_root_scan_phase"),
                    0,
                    1,
                )
                store_i64(
                    global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                    0,
                    -1,
                )
                continue
            root_count: i64 = load_i64(frame, 40)
            if slot_index >= root_count:
                global_store_ptr(
                    "pcc_gc_backend3_frame_root_scan_cursor",
                    load_ptr(frame, 16),
                )
                store_i64(
                    global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                    0,
                    0,
                )
                continue
            revision_before: i64 = load_i64(
                global_addr("pcc_gc_root_registry_revision"), 0
            )
            pcc_gc_visit_mapped_root_slot(
                load_ptr(frame, 8),
                slot_index * 8,
                load_ptr(frame, 56),
                load_i32(frame, 48) & 1,
                2,
                0,
            )
            examined = examined + 1
            revision = load_i64(
                global_addr("pcc_gc_root_registry_revision"), 0
            )
            if revision != revision_before:
                continue
            store_i64(
                global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                0,
                slot_index + 1,
            )
            if slot_index + 1 >= root_count:
                global_store_ptr(
                    "pcc_gc_backend3_frame_root_scan_cursor",
                    load_ptr(frame, 16),
                )
                store_i64(
                    global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                    0,
                    0,
                )
            continue

        if slot_index < 0:
            global_store_ptr(
                "pcc_gc_backend3_continuation_root_scan_cursor",
                global_load_ptr("pcc_gc_continuation_root_head"),
            )
            store_i64(
                global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0, 0
            )
            slot_index = 0
        continuation = global_load_ptr(
            "pcc_gc_backend3_continuation_root_scan_cursor"
        )
        if ptr_is_null(continuation) != 0:
            _reset_frame_root_scan()
            break
        root_count = load_i64(continuation, 24)
        if slot_index >= root_count:
            global_store_ptr(
                "pcc_gc_backend3_continuation_root_scan_cursor",
                load_ptr(continuation, 16),
            )
            store_i64(
                global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0, 0
            )
            continue
        revision_before = load_i64(
            global_addr("pcc_gc_root_registry_revision"), 0
        )
        pcc_gc_visit_mapped_root_slot(
            load_ptr(continuation, 8),
            slot_index * 8,
            load_ptr(continuation, 40),
            load_i32(continuation, 32),
            2,
            0,
        )
        examined = examined + 1
        revision = load_i64(
            global_addr("pcc_gc_root_registry_revision"), 0
        )
        if revision != revision_before:
            continue
        store_i64(
            global_addr("pcc_gc_backend3_frame_root_scan_slot"),
            0,
            slot_index + 1,
        )
        if slot_index + 1 >= root_count:
            global_store_ptr(
                "pcc_gc_backend3_continuation_root_scan_cursor",
                load_ptr(continuation, 16),
            )
            store_i64(
                global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0, 0
            )
    pcc_py_gc_minor_graph_unlock()
    if examined < remaining_budget:
        pcc_gc_backend3_drain_promotion_worklist(
            remaining_budget - examined
        )


@c_abi_export("pcc_gc_generational_promote_scheduler_roots")
def pcc_gc_generational_promote_scheduler_roots(
    remaining_budget: i64,
) -> None:
    if remaining_budget <= 0:
        return
    pcc_py_gc_minor_graph_lock()
    revision: i64 = 0
    examined: i64 = 0
    while examined < remaining_budget:
        phase: i64 = load_i32(
            global_addr("pcc_gc_backend3_scheduler_root_scan_phase"), 0
        )
        slot_index: i64 = load_i64(
            global_addr("pcc_gc_backend3_scheduler_root_scan_slot"), 0
        )
        if phase == 0:
            if slot_index < 0:
                global_store_ptr(
                    "pcc_gc_backend3_scheduler_root_scan_cursor",
                    global_load_ptr("pcc_gc_scheduler_root_head"),
                )
                store_i64(
                    global_addr("pcc_gc_backend3_scheduler_root_scan_slot"),
                    0,
                    0,
                )
            node = global_load_ptr(
                "pcc_gc_backend3_scheduler_root_scan_cursor"
            )
            if ptr_is_null(node) != 0:
                store_i32(
                    global_addr("pcc_gc_backend3_scheduler_root_scan_phase"),
                    0,
                    1,
                )
                store_i64(
                    global_addr("pcc_gc_backend3_scheduler_root_scan_slot"),
                    0,
                    0,
                )
                continue
            next_node = load_ptr(node, 8)
            revision_before: i64 = load_i64(
                global_addr("pcc_gc_root_registry_revision"), 0
            )
            slot = load_ptr(node, 0)
            if ptr_is_null(slot) == 0:
                pcc_gc_visit_mapped_root_slot(slot, 0, null(), 0, 2, 0)
            examined = examined + 1
            revision = load_i64(
                global_addr("pcc_gc_root_registry_revision"), 0
            )
            if revision != revision_before:
                continue
            global_store_ptr(
                "pcc_gc_backend3_scheduler_root_scan_cursor", next_node
            )
            continue

        slot = null()
        if slot_index < 34:
            slot = py_subs_exc_cache_slot(slot_index)
        else:
            slot = load_ptr(global_addr("pcc_builtin_type_root_slots"), (slot_index - 34) * 8)
        if ptr_is_null(slot) != 0:
            _reset_scheduler_root_scan()
            break
        revision_before = load_i64(
            global_addr("pcc_gc_root_registry_revision"), 0
        )
        if ptr_is_null(slot) == 0:
            pcc_gc_visit_mapped_root_slot(slot, 0, null(), 0, 2, 0)
        examined = examined + 1
        revision = load_i64(
            global_addr("pcc_gc_root_registry_revision"), 0
        )
        if revision != revision_before:
            continue
        store_i64(
            global_addr("pcc_gc_backend3_scheduler_root_scan_slot"),
            0,
            slot_index + 1,
        )
    pcc_py_gc_minor_graph_unlock()
    if examined < remaining_budget:
        pcc_gc_backend3_drain_promotion_worklist(
            remaining_budget - examined
        )


@c_abi_export("pcc_gc_generational_promote_tls_exception_root")
def pcc_gc_generational_promote_tls_exception_root(cleanup_out: c_ptr) -> None:
    if ptr_is_null(cleanup_out) != 0:
        return
    if ptr_is_null(load_ptr(cleanup_out, 0)) == 0:
        return
    current = py_tls_exc_get()
    if ptr_is_null(current) != 0 or is_tagged_int(current) != 0:
        return
    oldified = pcc_gc_generational_oldify_copy(current)
    if ptr_is_null(oldified) == 0:
        if ptr_eq(oldified, current) == 0:
            py_incref(oldified)
            py_tls_exc_set(oldified)
            pcc_gc_trace_referents_for_promotion(oldified)
            store_ptr(cleanup_out, 0, current)
            return
    pcc_gc_generational_promote_young_if_known(current)


@c_abi_export("pcc_gc_generational_promote_extension_module_state_root")
def pcc_gc_generational_promote_extension_module_state_root(
    root: c_ptr, ctx: c_ptr
) -> None:
    if ptr_is_null(root) != 0 or is_tagged_int(root) != 0:
        return
    pcc_py_gc_minor_graph_lock()
    pcc_gc_generational_promote_young_if_known(root)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_backend3_drain_promotion_worklist(16)


@c_abi_export("pcc_gc_generational_step")
def pcc_gc_generational_step(
    remaining_budget: i64, promote_all_young: i64
) -> i64:
    if remaining_budget <= 0:
        return 0
    batch_budget: i64 = remaining_budget
    if batch_budget > 16:
        batch_budget = 16
    detached_remembered = stack_alloc(8)
    store_ptr(detached_remembered, 0, null())
    tls_cleanup = stack_alloc(8)
    store_ptr(tls_cleanup, 0, null())
    pcc_gc_generational_promote_frame_roots(batch_budget)
    pcc_gc_generational_promote_scheduler_roots(batch_budget)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_generational_promote_tls_exception_root(tls_cleanup)
    local_processed: i64 = 0
    local_processed = local_processed + pcc_gc_backend3_drain_remembered_owners(
        batch_budget - local_processed, detached_remembered
    )
    if promote_all_young != 0:
        while (
            ptr_is_null(pcc_gc_backend3_young_list_head()) == 0
            and local_processed < batch_budget
        ):
            node = pcc_gc_backend3_young_list_head()
            pcc_gc_backend3_young_unlink(node)
            if pcc_gc_object_node_is_active(node) == 0:
                continue
            obj = load_ptr(node, 0)
            flags: i64 = load_i32(obj, 12)
            if (flags & 128) == 0:
                continue
            if ptr_is_null(pcc_gc_forwarding_find(obj)) == 0:
                continue
            pcc_gc_generational_promote_young_if_known(obj)
            after_flags: i64 = load_i32(obj, 12)
            if (
                (after_flags & 128) == 0
                or ptr_is_null(pcc_gc_forwarding_find(obj)) == 0
            ):
                local_processed = local_processed + 1
            else:
                pcc_gc_backend3_young_link_head(node)
                break
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_backend3_finish_detached_remembered_owners(
        load_ptr(detached_remembered, 0)
    )
    tls_cleanup_value = load_ptr(tls_cleanup, 0)
    if ptr_is_null(tls_cleanup_value) == 0:
        py_decref(tls_cleanup_value)
    if local_processed < remaining_budget:
        local_processed = local_processed + (
            pcc_gc_backend3_drain_promotion_worklist(
                remaining_budget - local_processed
            )
        )
    pcc_capi_visit_extension_module_state_roots(
        function_addr("pcc_gc_generational_promote_extension_module_state_root"),
        null(),
    )
    if local_processed > 0:
        pcc_thread_safepoint()
    return local_processed


@c_abi_export("pcc_gc_backend4_step_remembered_roots")
def pcc_gc_backend4_step_remembered_roots(remaining_budget: i64) -> i64:
    """Drain one GC4 remembered batch with callback-capable cleanup unlocked."""
    if remaining_budget <= 0:
        return 0
    batch_limit: i64 = remaining_budget
    if batch_limit > 8:
        batch_limit = 8
    cleanup = null()
    local_drained: i64 = 0

    pcc_py_gc_minor_graph_lock()
    medium = load_ptr(
        global_addr("pcc_gc_backend4_store_buffer_medium_head"), 0
    )
    medium_count: i64 = load_i32(
        global_addr("pcc_gc_backend4_store_buffer_medium_count"), 0
    )
    if ptr_is_null(medium) == 0:
        store_ptr(
            global_addr("pcc_gc_backend4_store_buffer_medium_head"),
            0,
            null(),
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_medium_count"), 0, 0
        )
        tail = medium
        tail_next = load_ptr(tail, 24)
        while ptr_is_null(tail_next) == 0:
            tail = tail_next
            tail_next = load_ptr(tail, 24)
        store_ptr(
            tail,
            24,
            load_ptr(global_addr("pcc_gc_backend4_store_buffer_head"), 0),
        )
        store_ptr(
            global_addr("pcc_gc_backend4_store_buffer_head"), 0, medium
        )
        flushes: i64 = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_medium_flushes_count"),
            0,
            flushes + 1,
        )
        flushed: i64 = load_i32(
            global_addr(
                "pcc_gc_backend4_store_buffer_medium_flushed_entries_count"
            ),
            0,
        )
        store_i32(
            global_addr(
                "pcc_gc_backend4_store_buffer_medium_flushed_entries_count"
            ),
            0,
            flushed + medium_count,
        )
        if medium_count >= 32:
            full_flushes: i64 = load_i32(
                global_addr(
                    "pcc_gc_backend4_store_buffer_medium_full_flushes_count"
                ),
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_store_buffer_medium_full_flushes_count"
                ),
                0,
                full_flushes + 1,
            )

    node = load_ptr(global_addr("pcc_gc_backend4_store_buffer_head"), 0)
    while ptr_is_null(node) == 0 and local_drained < batch_limit:
        nxt = load_ptr(node, 24)
        store_ptr(global_addr("pcc_gc_backend4_store_buffer_head"), 0, nxt)
        owner = load_ptr(node, 0)
        slot = load_ptr(node, 8)
        value = load_ptr(node, 16)
        store_ptr(node, 24, cleanup)
        cleanup = node
        pending: i64 = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0
        )
        if pending > 0:
            store_i32(
                global_addr("pcc_gc_backend4_store_buffer_entries_count"),
                0,
                pending - 1,
            )
        local_drained = local_drained + 1
        node = nxt
        if pcc_gc_object_is_known_no_lock(owner) == 0:
            continue
        flags: i64 = load_i32(owner, 12)
        if (flags & 512) == 0:
            continue
        pcc_gc_generational_promote_young_if_known(value)
        if ptr_is_null(slot) == 0:
            pcc_gc_generational_promote_owned_slot_mode(slot, 0, 1)
        else:
            pcc_gc_trace_referents_for_promotion(owner)

        owner_pending: i64 = 0
        pending_node = load_ptr(
            global_addr("pcc_gc_backend4_store_buffer_medium_head"), 0
        )
        while ptr_is_null(pending_node) == 0:
            if ptr_eq(load_ptr(pending_node, 0), owner) != 0:
                owner_pending = 1
                break
            pending_node = load_ptr(pending_node, 24)
        if owner_pending == 0:
            pending_node = load_ptr(
                global_addr("pcc_gc_backend4_store_buffer_head"), 0
            )
            while ptr_is_null(pending_node) == 0:
                if ptr_eq(load_ptr(pending_node, 0), owner) != 0:
                    owner_pending = 1
                    break
                pending_node = load_ptr(pending_node, 24)
        if owner_pending == 0:
            atomic_rmw_i32("and", owner, 12, -513, "acq_rel")

    if local_drained > 0:
        batches: i64 = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_drain_batches_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_drain_batches_count"),
            0,
            batches + 1,
        )
        drained_entries: i64 = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_drained_entries_count"), 0
        )
        store_i32(
            global_addr("pcc_gc_backend4_store_buffer_drained_entries_count"),
            0,
            drained_entries + local_drained,
        )
        max_batch: i64 = load_i32(
            global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"), 0
        )
        if local_drained > max_batch:
            store_i32(
                global_addr("pcc_gc_backend4_store_buffer_max_batch_size_count"),
                0,
                local_drained,
            )
        if local_drained >= 8:
            full_batches: i64 = load_i32(
                global_addr("pcc_gc_backend4_store_buffer_full_batches_count"), 0
            )
            store_i32(
                global_addr("pcc_gc_backend4_store_buffer_full_batches_count"),
                0,
                full_batches + 1,
            )
        if (
            load_i32(
                global_addr("pcc_gc_backend4_store_buffer_entries_count"), 0
            )
            > 0
        ):
            incomplete: i64 = load_i32(
                global_addr(
                    "pcc_gc_backend4_store_buffer_incomplete_drains_count"
                ),
                0,
            )
            store_i32(
                global_addr(
                    "pcc_gc_backend4_store_buffer_incomplete_drains_count"
                ),
                0,
                incomplete + 1,
            )
    pcc_py_gc_minor_graph_unlock()

    while ptr_is_null(cleanup) == 0:
        nxt = load_ptr(cleanup, 24)
        value = load_ptr(cleanup, 16)
        free(cleanup)
        py_decref(value)
        cleanup = nxt
    promotion_examined: i64 = 0
    if local_drained < remaining_budget:
        promotion_examined = pcc_gc_backend3_drain_promotion_worklist(
            remaining_budget - local_drained
        )
    if local_drained > 0:
        pcc_thread_safepoint()
    return local_drained + promotion_examined


@c_abi_export("pcc_gc_backend4_step_generation_aging")
def pcc_gc_backend4_step_generation_aging(remaining_budget: i64) -> i64:
    """Age pending GC4 young nodes in poll-free graph-lock tenures."""
    if remaining_budget <= 0:
        return 0
    total_examined: i64 = 0
    while total_examined < remaining_budget:
        batch_limit: i64 = remaining_budget - total_examined
        if batch_limit > 16:
            batch_limit = 16
        batch_examined: i64 = 0
        more_work: i64 = 0
        pcc_py_gc_minor_graph_lock()
        while (
            ptr_is_null(pcc_gc_backend3_young_list_head()) == 0
            and batch_examined < batch_limit
        ):
            node = pcc_gc_backend3_young_list_head()
            pcc_gc_backend3_young_unlink(node)
            batch_examined = batch_examined + 1
            total_examined = total_examined + 1
            if pcc_gc_object_node_is_active(node) == 0:
                continue
            obj = load_ptr(node, 0)
            flags: i64 = atomic_load_i32(obj, 12, "acquire")
            if (flags & 128) == 0 or (flags & 256) != 0:
                continue
            # YOUNG (0x80) and OLD (0x100) are adjacent.  Adding 0x80
            # atomically clears YOUNG, carries into OLD, and preserves all
            # unrelated header bits.
            atomic_rmw_i32("add", obj, 12, 128, "acq_rel")
            zpage_node = load_ptr(node, 48)
            if ptr_is_null(zpage_node) == 0:
                page = load_ptr(zpage_node, 8)
                if ptr_is_null(page) == 0:
                    store_i32(page, 28, 2)
            atomic_rmw_i32(
                "add",
                global_addr("pcc_gc_backend4_young_promotions"),
                0,
                1,
                "acq_rel",
            )
        if ptr_is_null(pcc_gc_backend3_young_list_head()) == 0:
            more_work = 1
        pcc_py_gc_minor_graph_unlock()
        if batch_examined > 0:
            pcc_thread_safepoint()
        if batch_examined == 0 or more_work == 0:
            break
    return total_examined
