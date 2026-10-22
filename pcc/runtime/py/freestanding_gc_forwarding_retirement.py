"""Backend 4 one-epoch forwarding and source-page retirement."""

from pcc import i64
from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    free,
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


py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_dealloc_cascade_active = extern(
    "pcc_dealloc_cascade_active", (), c_int64
)
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_gc_backend4_remap_referents = extern(
    "pcc_gc_backend4_remap_referents", (c_ptr,), c_void
)
pcc_gc_backend4_remap_cext_referents_unlocked = extern("pcc_gc_backend4_remap_cext_referents_unlocked", (c_ptr, c_ptr), c_void)
pcc_gc_backend4_remap_cext_ctx_valid = extern(
    "pcc_gc_backend4_remap_cext_ctx_valid", (c_ptr,), c_int64
)
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_py_gc_minor_graph_lock = extern(
    "pcc_py_gc_minor_graph_lock", (), c_void
)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_thread_owns_stopped_world = extern(
    "pcc_thread_owns_stopped_world", (), c_int64
)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_gc_backend4_zpage_clear_active_page = extern(
    "pcc_gc_backend4_zpage_clear_active_page", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_destroy = extern(
    "pcc_gc_backend4_zpage_destroy", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_find_page_for_addr = extern(
    "pcc_gc_backend4_zpage_find_page_for_addr", (c_ptr, c_int64), c_ptr
)
pcc_gc_backend4_zpage_unlink_page = extern(
    "pcc_gc_backend4_zpage_unlink_page", (c_ptr,), c_void
)
pcc_gc_forwarding_index_remove = extern(
    "pcc_gc_forwarding_index_remove", (c_ptr,), c_ptr
)
pcc_gc_forwarding_list_head = extern(
    "pcc_gc_forwarding_list_head", (), c_ptr
)
pcc_gc_forwarding_target_index_remove = extern(
    "pcc_gc_forwarding_target_index_remove", (c_ptr,), c_ptr
)
pcc_gc_forwarding_target_unlink = extern(
    "pcc_gc_forwarding_target_unlink", (c_ptr,), c_void
)
pcc_gc_forwarding_unlink_main = extern(
    "pcc_gc_forwarding_unlink_main", (c_ptr,), c_void
)
pcc_gc_identity_detach = extern("pcc_gc_identity_detach", (c_ptr,), c_ptr)
pcc_gc_identity_finish_detached = extern(
    "pcc_gc_identity_finish_detached", (c_ptr,), c_void
)
pcc_gc_managed_pointer_index_remove = extern(
    "pcc_gc_managed_pointer_index_remove", (c_ptr,), c_int64
)
pcc_gc_granule_object_retire = extern(
    "pcc_gc_granule_object_retire", (c_ptr,), c_int64
)
pcc_gc_granule_is_object_start = extern(
    "pcc_gc_granule_is_object_start", (c_ptr,), c_int64
)
pcc_gc_relocation_retire_source_payload_into_finish = extern(
    "pcc_gc_relocation_retire_source_payload_into_finish", (c_ptr, c_ptr), c_int64
)
pcc_gc_relocation_retire_source_payload_for_target_death_into_finish = extern("pcc_gc_relocation_retire_source_payload_for_target_death_into_finish", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_gc_relocation_finish_source_payloads = extern(
    "pcc_gc_relocation_finish_source_payloads", (c_ptr,), c_void
)
pcc_py_gc_defer_tripwire = extern(
    "pcc_py_gc_defer_tripwire", (c_ptr, c_ptr, c_int32), c_void
)
pcc_gc_live_bytes_subtract = extern(
    "pcc_gc_live_bytes_subtract", (c_int64,), c_void
)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_object_index_remove = extern(
    "pcc_gc_object_index_remove", (c_ptr,), c_ptr
)
pcc_gc_object_list_head = extern("pcc_gc_object_list_head", (), c_ptr)
pcc_gc_object_node_freeing = extern(
    "pcc_gc_object_node_freeing", (c_ptr,), c_int64
)
pcc_gc_object_node_finish_detached = extern(
    "pcc_gc_object_node_finish_detached", (c_ptr,), c_void
)
pcc_gc_object_node_size = extern(
    "pcc_gc_object_node_size", (c_ptr,), c_int64
)
pcc_gc_object_node_unlink = extern(
    "pcc_gc_object_node_unlink", (c_ptr,), c_void
)
pcc_gc_visit_registered_root_slots = extern(
    "pcc_gc_visit_registered_root_slots", (c_int64, c_int64), c_int64
)


@c_abi_export("pcc_gc_backend4_park_page")
def pcc_gc_backend4_park_page(page) -> None:
    if ptr_is_null(page) != 0:
        return
    pcc_gc_backend4_zpage_clear_active_page(page)
    store_i32(page, 108, 0)
    store_ptr(page, 112, null())
    store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_parked_head"))
    global_store_ptr("pcc_gc_backend4_parked_head", page)


@c_abi_export("pcc_gc_backend4_drain_parked_pages")
def pcc_gc_backend4_drain_parked_pages() -> None:
    page = global_load_ptr("pcc_gc_backend4_parked_head")
    global_store_ptr("pcc_gc_backend4_parked_head", null())
    while ptr_is_null(page) == 0:
        nxt = load_ptr(page, 56)
        store_ptr(page, 56, null())
        pcc_gc_backend4_zpage_destroy(page)
        page = nxt


@c_abi_export("pcc_gc_backend4_release_retained_pages_unlocked")
def _release_retained_pages() -> c_ptr:
    # A source page is first parked when its final forwarding entry retires,
    # then moved to the retained list by the next remap.  Only the following
    # remap may release its physical span: by then two complete root/referent
    # rewrite epochs have passed, so a legitimate value surviving either
    # safepoint must have been reloaded from an updateable slot.  Keep an
    # invariant-violating page quarantined instead of risking a use-after-free.
    page = global_load_ptr("pcc_gc_backend4_retained_page_head")
    released_pages = null()
    global_store_ptr("pcc_gc_backend4_retained_page_head", null())
    while ptr_is_null(page) == 0:
        nxt = load_ptr(page, 56)
        store_ptr(page, 56, null())
        if (
            load_i64(page, 32) > 0
            or load_i64(page, 88) > 0
            or load_i64(page, 96) > 0
        ):
            store_ptr(
                page,
                56,
                global_load_ptr("pcc_gc_backend4_retained_page_head"),
            )
            global_store_ptr("pcc_gc_backend4_retained_page_head", page)
        else:
            store_ptr(page, 56, released_pages)
            released_pages = page
        page = nxt
    return released_pages


@c_abi_export("pcc_gc_backend4_finish_retained_page_releases")
def pcc_gc_backend4_finish_retained_page_releases(pages: c_ptr) -> None:
    while ptr_is_null(pages) == 0:
        page = pages
        pages = load_ptr(page, 56)
        store_ptr(page, 56, null())
        span = load_ptr(page, 72)
        if ptr_is_null(span) == 0:
            store_ptr(page, 72, null())
            free(span)
        free(page)


@c_abi_export("pcc_gc_backend4_note_forwarding_removed_on_page")
def pcc_gc_backend4_note_forwarding_removed_on_page(page) -> None:
    if ptr_is_null(page) != 0:
        return
    fwd: i64 = load_i64(page, 96)
    if fwd > 0:
        fwd = fwd - 1
        store_i64(page, 96, fwd)
    flag: i64 = load_i32(page, 104)
    if (
        flag != 0
        and fwd <= 0
        and load_i64(page, 32) <= 0
        and load_i64(page, 88) <= 0
    ):
        counter = global_addr("pcc_gc_backend4_deferred_recycle_pages")
        if pcc_dealloc_cascade_active() == 0:
            # Only flag 1 pages are in the deferred-page counter; flag 2
            # (deferred on forwarding) was never counted.
            store_i32(page, 104, 0)
            if flag == 1:
                remaining: i64 = load_i64(counter, 0)
                if remaining > 0:
                    store_i64(counter, 0, remaining - 1)
            pcc_gc_backend4_zpage_unlink_page(page)
            pcc_gc_backend4_park_page(page)
        elif flag == 2:
            # While a trash cascade is active the page may still own queued
            # objects; hand it to the post-drain sweep
            # (pcc_gc_backend4_sweep_deferred_recycles), which counts flag 1.
            store_i32(page, 104, 1)
            store_i64(counter, 0, load_i64(counter, 0) + 1)


@c_abi_export("pcc_gc_backend4_zpage_note_forwarding_removed")
def pcc_gc_backend4_zpage_note_forwarding_removed(from_obj) -> None:
    if pcc_gc_backend() != 4:
        return
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return
    if (load_i32(from_obj, 12) & 65536) == 0:
        return
    pcc_gc_backend4_note_forwarding_removed_on_page(
        pcc_gc_backend4_zpage_find_page_for_addr(from_obj, 16)
    )


@c_abi_export("pcc_gc_forwarding_detach")
def pcc_gc_forwarding_detach(from_obj):
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return null()
    node = pcc_gc_forwarding_index_remove(from_obj)
    if ptr_is_null(node) != 0:
        return null()
    pcc_gc_forwarding_target_unlink(node)
    pcc_gc_forwarding_unlink_main(node)
    from_page = load_ptr(node, 48)
    population: i64 = load_i32(global_addr("pcc_gc_forwarding_population"), 0)
    if population > 0:
        store_i32(
            global_addr("pcc_gc_forwarding_population"), 0, population - 1
        )
    if ptr_is_null(from_page) == 0:
        pcc_gc_backend4_note_forwarding_removed_on_page(from_page)
    else:
        pcc_gc_backend4_zpage_note_forwarding_removed(from_obj)
    return node


@c_abi_export("pcc_gc_forwarding_finish_detached")
def pcc_gc_forwarding_finish_detached(nodes) -> None:
    while ptr_is_null(nodes) == 0:
        node = nodes
        nodes = load_ptr(node, 16)
        store_ptr(node, 16, null())
        target = load_ptr(node, 8)
        py_decref(target)
        free(node)


@c_abi_export("pcc_gc_forwarding_finish_dead_targets")
def pcc_gc_forwarding_finish_dead_targets(nodes) -> None:
    while ptr_is_null(nodes) == 0:
        node = nodes
        nodes = load_ptr(node, 16)
        store_ptr(node, 16, null())
        # Target death already consumed the target's logical reference.
        store_ptr(node, 8, null())
        free(node)


@c_abi_export("pcc_gc_backend4_finish_remap_retirement")
def pcc_gc_backend4_finish_remap_retirement(finish) -> None:
    if ptr_is_null(finish) != 0:
        return
    released_pages = load_ptr(finish, 0)
    forwardings = load_ptr(finish, 8)
    identities = load_ptr(finish, 16)
    object_nodes = load_ptr(finish, 24)
    payload_plans = load_ptr(finish, 32)
    dead_targets = load_ptr(finish, 40)
    store_ptr(finish, 0, null())
    store_ptr(finish, 8, null())
    store_ptr(finish, 16, null())
    store_ptr(finish, 24, null())
    store_ptr(finish, 32, null())
    store_ptr(finish, 40, null())
    pcc_gc_backend4_finish_retained_page_releases(released_pages)
    pcc_gc_relocation_finish_source_payloads(payload_plans)
    pcc_gc_forwarding_finish_detached(forwardings)
    pcc_gc_forwarding_finish_dead_targets(dead_targets)
    pcc_gc_identity_finish_detached(identities)
    pcc_gc_object_node_finish_detached(object_nodes)


@c_abi_export("pcc_gc_forwarding_detach_into_finish")
def pcc_gc_forwarding_detach_into_finish(from_obj, finish) -> None:
    if ptr_is_null(finish) != 0:
        return
    dead = pcc_gc_forwarding_detach(from_obj)
    if ptr_is_null(dead) != 0:
        return
    store_ptr(dead, 16, load_ptr(finish, 8))
    store_ptr(finish, 8, dead)


@c_abi_export("pcc_gc_forwarding_remove")
def pcc_gc_forwarding_remove(from_obj) -> None:
    pcc_gc_forwarding_finish_detached(pcc_gc_forwarding_detach(from_obj))


@c_abi_export("pcc_gc_retire_forwarded_source_into_finish_unlocked")
def _retire_forwarded_source_into_finish(from_obj, finish) -> None:
    if (
        ptr_is_null(from_obj) != 0
        or is_tagged_int(from_obj) != 0
        or ptr_is_null(finish) != 0
    ):
        return
    lease_node = pcc_gc_object_index_find(from_obj)
    if ptr_is_null(lease_node) == 0 and load_i64(lease_node, 80) != 0:
        pcc_py_gc_defer_tripwire(
            cstr("forwarded source retains a foreign-address lease"),
            cstr("pcc/runtime/py/freestanding_gc_forwarding_retirement.py"),
            324,
        )
        return
    identity = pcc_gc_identity_detach(from_obj)
    if ptr_is_null(identity) == 0:
        store_ptr(identity, 16, load_ptr(finish, 16))
        store_ptr(finish, 16, identity)
    # A moving source that fell back to an ordinary object-family slab owns a
    # LIVE granule marker even though it may never have entered the exact set.
    # Retire that marker at the same semantic lifecycle boundary.  Non-slab
    # minor/zpage/large/foreign sources retain the exact-set removal.
    granule_was_live: i64 = pcc_gc_granule_is_object_start(from_obj)
    retire_result: i64 = pcc_gc_granule_object_retire(from_obj)
    if retire_result < 0 or (retire_result > 0 and granule_was_live != 1):
        pcc_py_gc_defer_tripwire(
            cstr("forwarded-source granule retirement invariant violated"),
            cstr("pcc/runtime/py/freestanding_gc_forwarding_retirement.py"),
            232,
        )
        return
    if retire_result == 0:
        pcc_gc_managed_pointer_index_remove(from_obj)
    dead = pcc_gc_object_index_find(from_obj)
    if ptr_is_null(dead) != 0:
        scan = pcc_gc_object_list_head()
        while ptr_is_null(scan) == 0:
            if ptr_eq(load_ptr(scan, 0), from_obj) != 0:
                dead = scan
                break
            scan = load_ptr(scan, 16)
    if ptr_is_null(dead) != 0:
        return
    size: i64 = pcc_gc_object_node_size(dead)
    if pcc_gc_object_node_freeing(dead) == 0 and size > 0:
        pcc_gc_live_bytes_subtract(size)
    pcc_gc_object_index_remove(from_obj)
    pcc_gc_object_node_unlink(dead)
    store_ptr(dead, 16, load_ptr(finish, 24))
    store_ptr(finish, 24, dead)


@c_abi_export("pcc_gc_retire_forwarded_source_unlocked")
def _retire_forwarded_source(from_obj) -> None:
    finish = stack_alloc(48)
    store_ptr(finish, 0, null())
    store_ptr(finish, 8, null())
    store_ptr(finish, 16, null())
    store_ptr(finish, 24, null())
    store_ptr(finish, 32, null())
    store_ptr(finish, 40, null())
    _retire_forwarded_source_into_finish(from_obj, finish)
    pcc_gc_backend4_finish_remap_retirement(finish)


@c_abi_export("pcc_gc_forwarding_remove_target")
def pcc_gc_forwarding_remove_target(target, finish) -> None:
    if (
        ptr_is_null(target) != 0
        or is_tagged_int(target) != 0
        or ptr_is_null(finish) != 0
    ):
        return
    # Detach the reverse index before cleanup so a decref reentry cannot walk
    # the same dying target twice.  The source index/main edge and flags remain
    # live for healing; preparation failure is unconditional fail-stop, not a
    # recoverable rollback of the whole forwarding transaction.
    node = pcc_gc_forwarding_target_index_remove(target)
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 32)
        from_obj = load_ptr(node, 0)
        if pcc_gc_backend() == 4:
            if (
                pcc_gc_relocation_retire_source_payload_for_target_death_into_finish(
                    from_obj, target, finish
                )
                == 0
            ):
                pcc_py_gc_defer_tripwire(
                    cstr(
                        "forwarded-source payload retirement failed before target teardown"
                    ),
                    cstr(
                        "pcc/runtime/py/freestanding_gc_forwarding_retirement.py"
                    ),
                    272,
                )
                return
        pcc_gc_forwarding_index_remove(from_obj)
        pcc_gc_forwarding_unlink_main(node)
        # A target can die before the usual two-epoch remap retirement.  Its
        # old shell must leave the object index immediately or stale pointers
        # remain classified as live managed objects.
        _retire_forwarded_source_into_finish(from_obj, finish)
        store_ptr(node, 32, null())
        store_ptr(node, 40, null())
        from_page = load_ptr(node, 48)
        store_ptr(node, 8, null())
        store_ptr(node, 16, load_ptr(finish, 40))
        store_ptr(finish, 40, node)
        population: i64 = load_i32(
            global_addr("pcc_gc_forwarding_population"), 0
        )
        if population > 0:
            store_i32(
                global_addr("pcc_gc_forwarding_population"),
                0,
                population - 1,
            )
        if ptr_is_null(from_page) == 0:
            pcc_gc_backend4_note_forwarding_removed_on_page(from_page)
        else:
            pcc_gc_backend4_zpage_note_forwarding_removed(from_obj)
        node = nxt


@c_abi_export("pcc_gc_backend4_remap_and_retire_stopped_world")
def pcc_gc_backend4_remap_and_retire_stopped_world() -> i64:
    owns_stopped_world: i64 = pcc_thread_owns_stopped_world()
    acquired_stopped_world: i64 = 0
    if owns_stopped_world == 0:
        if pcc_stop_the_world() != 0:
            return 0
        acquired_stopped_world = 1

    finish = stack_alloc(48)
    offset: i64 = 0
    while offset < 48:
        store_ptr(finish, offset, null())
        offset = offset + 8
    context = stack_alloc(56)
    offset = 0
    while offset < 56:
        store_i64(context, offset, 0)
        offset = offset + 8
    cursor = null()
    valid: i64 = 0
    pcc_py_gc_minor_graph_lock()
    epoch: i64 = load_i64(global_addr("pcc_gc_backend4_remap_epoch"), 0)
    if (
        load_i32(global_addr("pcc_gc_backend_selected"), 0) == 4
        and ptr_is_null(global_load_ptr("pcc_gc_relocation_set_head")) != 0
        and load_i32(global_addr("pcc_gc_forwarding_population"), 0) > 0
        and load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) == 0
        and epoch < 9223372036854775807
    ):
        epoch = epoch + 1
        store_i64(global_addr("pcc_gc_backend4_remap_epoch"), 0, epoch)
        store_i32(global_addr("pcc_gc_backend4_remap_active"), 0, 1)
        global_store_ptr("pcc_gc_backend4_remap_pending_obj", null())
        store_i64(context, 8, epoch)
        store_i64(
            context,
            16,
            load_i64(global_addr("pcc_gc_object_list_revision"), 0),
        )
        store_ptr(context, 24, global_load_ptr("pcc_gc_forwarding_head"))
        store_i64(
            context,
            32,
            load_i32(global_addr("pcc_gc_forwarding_population"), 0),
        )
        store_i64(
            context,
            40,
            load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            ),
        )
        store_i64(
            context,
            48,
            load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            ),
        )
        cursor = pcc_gc_object_list_head()
        valid = 1
    pcc_py_gc_minor_graph_unlock()

    if valid == 0:
        if acquired_stopped_world != 0:
            pcc_resume_world()
        pcc_gc_backend4_finish_remap_retirement(finish)
        return 0

    while valid != 0 and ptr_is_null(cursor) == 0:
        pcc_py_gc_minor_graph_lock()
        if (
            load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) == 0
            or load_i64(global_addr("pcc_gc_backend4_remap_epoch"), 0)
            != load_i64(context, 8)
            or ptr_is_null(
                global_load_ptr("pcc_gc_backend4_remap_pending_obj")
            ) == 0
            or load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4
            or load_i64(global_addr("pcc_gc_object_list_revision"), 0)
            != load_i64(context, 16)
            or ptr_eq(
                global_load_ptr("pcc_gc_forwarding_head"),
                load_ptr(context, 24),
            ) == 0
            or load_i32(global_addr("pcc_gc_forwarding_population"), 0)
            != load_i64(context, 32)
            or load_i64(
                global_addr("pcc_gc_backend4_reseed_page_revision"), 0
            ) != load_i64(context, 40)
            or load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
            ) != load_i64(context, 48)
        ):
            valid = 0
        while valid != 0 and ptr_is_null(cursor) == 0:
            node = cursor
            cursor = load_ptr(node, 16)
            if load_i64(node, 32) != 0:
                continue
            obj = load_ptr(node, 0)
            if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
                continue
            if pcc_capi_is_cext_type_tag(load_i32(obj, 8)) == 0:
                continue
            py_incref(obj)
            store_ptr(context, 0, obj)
            global_store_ptr("pcc_gc_backend4_remap_pending_obj", obj)
            break
        pcc_py_gc_minor_graph_unlock()
        obj = load_ptr(context, 0)
        if valid == 0 or ptr_is_null(obj) != 0:
            break
        pcc_gc_backend4_remap_cext_referents_unlocked(obj, context)
        pcc_py_gc_minor_graph_lock()
        valid = pcc_gc_backend4_remap_cext_ctx_valid(context)
        if ptr_eq(
            global_load_ptr("pcc_gc_backend4_remap_pending_obj"), obj
        ) != 0:
            global_store_ptr("pcc_gc_backend4_remap_pending_obj", null())
        pcc_py_gc_minor_graph_unlock()
        py_decref(obj)
        store_ptr(context, 0, null())

    pcc_py_gc_minor_graph_lock()
    before: i64 = load_i32(global_addr("pcc_gc_forwarding_population"), 0)
    if (
        valid != 0
        and ptr_is_null(cursor) != 0
        and load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) != 0
        and load_i64(global_addr("pcc_gc_backend4_remap_epoch"), 0)
        == load_i64(context, 8)
        and ptr_is_null(
            global_load_ptr("pcc_gc_backend4_remap_pending_obj")
        ) != 0
        and load_i32(global_addr("pcc_gc_backend_selected"), 0) == 4
        and ptr_is_null(global_load_ptr("pcc_gc_relocation_set_head")) != 0
        and load_i64(global_addr("pcc_gc_object_list_revision"), 0)
        == load_i64(context, 16)
        and ptr_eq(
            global_load_ptr("pcc_gc_forwarding_head"),
            load_ptr(context, 24),
        ) != 0
        and load_i32(global_addr("pcc_gc_forwarding_population"), 0)
        == load_i64(context, 32)
        and load_i64(
            global_addr("pcc_gc_backend4_reseed_page_revision"), 0
        ) == load_i64(context, 40)
        and load_i64(
            global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
        ) == load_i64(context, 48)
    ):
        pcc_gc_backend4_remap_and_retire_unlocked(finish)
    else:
        valid = 0
    after: i64 = load_i32(global_addr("pcc_gc_forwarding_population"), 0)
    global_store_ptr("pcc_gc_backend4_remap_pending_obj", null())
    store_i32(global_addr("pcc_gc_backend4_remap_active"), 0, 0)
    pcc_py_gc_minor_graph_unlock()

    if acquired_stopped_world != 0:
        pcc_resume_world()
    pcc_gc_backend4_finish_remap_retirement(finish)
    if valid == 0:
        return 0
    if before > after:
        return before - after
    if before > 0:
        return 1
    return 0


@c_abi_export("pcc_gc_backend4_remap_and_retire_unlocked")
def pcc_gc_backend4_remap_and_retire_unlocked(finish) -> None:
    if ptr_is_null(finish) != 0:
        return
    if pcc_gc_backend() != 4:
        return
    # Two-epoch quarantine: release the prior retained generation before
    # parked pages from the immediately preceding remap enter that generation.
    store_ptr(finish, 0, _release_retained_pages())
    pcc_gc_backend4_drain_parked_pages()
    if ptr_is_null(pcc_gc_forwarding_list_head()) != 0:
        return
    node = pcc_gc_object_list_head()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        if load_i64(node, 32) == 0:
            obj = load_ptr(node, 0)
            if pcc_capi_is_cext_type_tag(load_i32(obj, 8)) == 0:
                pcc_gc_backend4_remap_referents(obj)
        node = nxt
    pcc_gc_visit_registered_root_slots(3, 0)

    fwd = pcc_gc_forwarding_list_head()
    while ptr_is_null(fwd) == 0:
        nxt = load_ptr(fwd, 16)
        old = load_ptr(fwd, 0)
        if ptr_is_null(old) != 0 or is_tagged_int(old) != 0:
            pcc_gc_forwarding_target_unlink(fwd)
            pcc_gc_forwarding_unlink_main(fwd)
            py_decref(load_ptr(fwd, 8))
            free(fwd)
            population: i64 = load_i32(
                global_addr("pcc_gc_forwarding_population"), 0
            )
            if population > 0:
                store_i32(
                    global_addr("pcc_gc_forwarding_population"),
                    0,
                    population - 1,
                )
            fwd = nxt
            continue
        old_flags: i64 = load_i32(old, 12)
        if (old_flags & 131072) == 0:
            store_i32(old, 12, old_flags | 131072)
            fwd = nxt
            continue
        if (
            pcc_gc_relocation_retire_source_payload_into_finish(old, finish)
            == 0
        ):
            pcc_py_gc_defer_tripwire(
                cstr(
                    "forwarded-source payload retirement failed before normal teardown"
                ),
                cstr(
                    "pcc/runtime/py/freestanding_gc_forwarding_retirement.py"
                ),
                354,
            )
            return
        store_i32(old, 12, old_flags & ~(2048 | 131072))
        _retire_forwarded_source_into_finish(old, finish)
        dead = pcc_gc_forwarding_detach(old)
        if ptr_is_null(dead) == 0:
            store_ptr(dead, 16, load_ptr(finish, 8))
            store_ptr(finish, 8, dead)
        fwd = nxt
