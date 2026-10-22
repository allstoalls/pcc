"""Backend 4 single-use relocation copy transaction."""
from pcc import i64
from pcc.runtime.py.py_abi_constants import PY_TYPE_CLASS, PY_TYPE_MEMORYVIEW

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_rmw_i32,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    memmove,
    null,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_object_is_address_pinned = extern("pcc_gc_object_is_address_pinned", (c_ptr,), c_int64)


pcc_gc_alloc = extern(
    "pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr
)
pcc_gc_backend4_relocate_copy_supported_tag = extern(
    "pcc_gc_backend4_relocate_copy_supported_tag", (c_int64,), c_int64
)
pcc_gc_backend4_zpage_find = extern(
    "pcc_gc_backend4_zpage_find", (c_ptr,), c_ptr
)
pcc_gc_backend4_zpage_detach_for_relocation = extern(
    "pcc_gc_backend4_zpage_detach_for_relocation", (c_ptr,), c_ptr
)
pcc_gc_backend4_zpage_finish_relocation_detach = extern(
    "pcc_gc_backend4_zpage_finish_relocation_detach", (c_ptr,), c_void
)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_gc_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
pcc_gc_forwarding_install_plan_prepare = extern(
    "pcc_gc_forwarding_install_plan_prepare", (c_ptr, c_ptr), c_ptr
)
pcc_gc_install_forwarding_preallocated_unlocked = extern(
    "pcc_gc_install_forwarding_preallocated_unlocked",
    (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_gc_forwarding_install_plan_finish = extern(
    "pcc_gc_forwarding_install_plan_finish", (c_ptr,), c_void
)
pcc_gc_relocation_member_count = extern(
    "pcc_gc_relocation_member_count", (c_ptr,), c_int64
)
pcc_gc_relocation_member_forget = extern(
    "pcc_gc_relocation_member_forget", (c_ptr,), c_void
)
pcc_gc_object_known_size = extern(
    "pcc_gc_object_known_size", (c_ptr,), c_int64
)
pcc_gc_relocate_copy_payload_prepared_locked = extern(
    "pcc_gc_relocate_copy_payload_prepared_locked",
    (c_ptr, c_ptr, c_int64, c_int64, c_ptr),
    c_int64
)
pcc_gc_relocation_payload_slot_count_locked = extern(
    "pcc_gc_relocation_payload_slot_count_locked", (c_ptr,), c_int64
)
pcc_gc_relocation_payload_plan_prepare = extern(
    "pcc_gc_relocation_payload_plan_prepare", (c_int64,), c_ptr
)
pcc_gc_relocation_payload_raw_snapshot_locked = extern(
    "pcc_gc_relocation_payload_raw_snapshot_locked",
    (c_ptr, c_int64, c_int64, c_ptr),
    c_int64
)
pcc_gc_relocation_payload_raw_prepare = extern(
    "pcc_gc_relocation_payload_raw_prepare", (c_ptr,), c_int64
)
pcc_gc_relocation_payload_raw_validate_locked = extern(
    "pcc_gc_relocation_payload_raw_validate_locked",
    (c_ptr, c_ptr, c_int64, c_int64, c_ptr),
    c_int64
)
pcc_gc_relocation_payload_plan_validate_locked = extern(
    "pcc_gc_relocation_payload_plan_validate_locked",
    (c_ptr, c_ptr, c_int64, c_ptr),
    c_int64
)
pcc_gc_relocation_payload_plan_finish = extern(
    "pcc_gc_relocation_payload_plan_finish", (c_ptr,), c_void
)
pcc_gc_memoryview_refresh_owned_buffer = extern(
    "pcc_gc_memoryview_refresh_owned_buffer", (c_ptr,), c_int64
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)


@c_abi_export("pcc_gc_backend4_relocate_copy_preallocated_unlocked")
def pcc_gc_backend4_relocate_copy_preallocated_unlocked(
    from_obj, size: i64, to_obj, payload_plan, forwarding_plan, finish_plan
):
    if ptr_is_null(finish_plan) != 0:
        return null()
    store_ptr(finish_plan, 0, null())
    store_ptr(finish_plan, 8, null())
    store_ptr(finish_plan, 16, null())
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4:
        return null()
    if (
        ptr_is_null(from_obj) != 0
        or is_tagged_int(from_obj) != 0
        or ptr_is_null(to_obj) != 0
        or is_tagged_int(to_obj) != 0
    ):
        return null()
    if size < 16:
        return null()
    if ptr_is_null(pcc_gc_forwarding_find(from_obj)) == 0:
        return null()
    relocation_node = global_load_ptr("pcc_gc_relocation_set_head")
    while (
        ptr_is_null(relocation_node) == 0
        and ptr_eq(load_ptr(relocation_node, 0), from_obj) == 0
    ):
        relocation_node = load_ptr(relocation_node, 8)
    if ptr_is_null(relocation_node) != 0:
        return null()
    flags: i64 = load_i32(from_obj, 12)
    if (flags & 524288) != 0 or pcc_gc_object_is_address_pinned(from_obj) != 0:
        return null()
    tag: i64 = load_i32(from_obj, 8)
    if pcc_gc_backend4_relocate_copy_supported_tag(tag) == 0:
        return null()
    known_size: i64 = pcc_gc_object_known_size(from_obj)
    if known_size <= 0 or size > known_size:
        return null()
    if (
        pcc_gc_object_known_size(to_obj) < size
        or load_i32(to_obj, 8) != tag
        or (load_i32(to_obj, 12) & 64) == 0
    ):
        return null()
    # The header copy clobbers allocation-origin flags.  Preserve the
    # destination residency so chained relocation cannot undercount the page
    # that physically owns the replacement object.
    to_residency: i64 = load_i32(to_obj, 12) & 331776
    memmove(to_obj, from_obj, size)
    store_i64(to_obj, 0, 1)
    new_flags: i64 = load_i32(to_obj, 12)
    # SWEEP_CANDIDATE (1024) is a finished-cycle "was unreachable"
    # verdict, not residency: relocation proves the value is live memory
    # being kept. Carrying it onto the copy lets a later no-re-mark
    # sweep (pcc_gc_collect_tracing consumes pending candidates
    # verbatim) run PASS-0 __del__ on a reachable object. The shell
    # stays sweep-visible until remap retirement, so its own stale
    # verdict dies with the old identity too.
    store_i32(to_obj, 12, (new_flags & ~343040) | to_residency)
    if (
        pcc_gc_relocate_copy_payload_prepared_locked(
            from_obj, to_obj, tag, size, payload_plan
        )
        == 0
    ):
        return null()
    if (
        pcc_gc_install_forwarding_preallocated_unlocked(
            from_obj, to_obj, forwarding_plan
        )
        != 0
    ):
        return null()
    # Forwarding is the commit point.  Before it succeeds the source still
    # owns its original sweep verdict so every rollback path remains honest.
    store_i32(from_obj, 12, load_i32(from_obj, 12) & ~1024)
    # The finite instance-field cache keys raw class addresses.  Invalidate it
    # after forwarding succeeds and before the old shell can be retired and
    # its address reused.  Relocation is rare, so this stays off the hot path.
    if tag == PY_TYPE_CLASS:
        atomic_rmw_i32(
            "add", global_addr("py_class_attr_cache_epoch"), 0, 1, "release"
        )
    if tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        # Commit the raw-allocation ownership transfer only after forwarding
        # itself cannot fail.  The payload phase deliberately left to_obj's
        # field NULL so all preceding rollback paths remain dealloc-safe.
        owned_buffer = load_ptr(from_obj, 24)
        store_ptr(to_obj, 24, owned_buffer)
        store_ptr(from_obj, 24, null())
        pcc_gc_memoryview_refresh_owned_buffer(to_obj)
    # Count-on-NEW: move the source copy's complete outstanding count onto the
    # replacement and leave the source as an immortal forwarding shell until
    # page retirement after a later remap epoch.
    outstanding: i64 = load_i64(from_obj, 0)
    if outstanding > 0:
        store_i64(to_obj, 0, load_i64(to_obj, 0) + outstanding)
    store_i32(from_obj, 12, load_i32(from_obj, 12) | 1)
    from_page = null()
    from_znode = pcc_gc_backend4_zpage_find(from_obj)
    if ptr_is_null(from_znode) == 0:
        from_page = load_ptr(from_znode, 8)
    evacuated: i64 = load_i32(
        global_addr("pcc_gc_backend4_evacuated_bytes_count"), 0
    )
    store_i32(
        global_addr("pcc_gc_backend4_evacuated_bytes_count"),
        0,
        evacuated + size,
    )
    prev = null()
    relocation_node = global_load_ptr("pcc_gc_relocation_set_head")
    while ptr_is_null(relocation_node) == 0:
        nxt = load_ptr(relocation_node, 8)
        if ptr_eq(load_ptr(relocation_node, 0), from_obj) != 0:
            if ptr_eq(
                global_load_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor"
                ),
                relocation_node,
            ) != 0:
                global_store_ptr(
                    "pcc_gc_backend4_reseed_relocation_cursor", nxt
                )
            relocation_revision: i64 = load_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"),
                0,
            )
            store_i64(
                global_addr("pcc_gc_backend4_reseed_relocation_revision"),
                0,
                relocation_revision + 1,
            )
            if ptr_is_null(prev) != 0:
                global_store_ptr("pcc_gc_relocation_set_head", nxt)
            else:
                store_ptr(prev, 8, nxt)
            pcc_gc_relocation_member_forget(from_obj)
            store_ptr(relocation_node, 8, null())
            store_ptr(finish_plan, 0, relocation_node)
            break
        prev = relocation_node
        relocation_node = nxt
    if ptr_is_null(from_page) == 0:
        page_has_candidate: i64 = 0
        membership_complete: i64 = 1
        remaining: i64 = pcc_gc_relocation_member_count(from_obj)
        if remaining < 0:
            membership_complete = 0
        elif remaining > 0:
            # Only one entry was removed. The source is still attached until
            # after this decision, so a duplicate keeps its page selected.
            page_has_candidate = 1
        else:
            # Exact membership lets a negative query inspect this page's
            # owners, rather than every unrelated relocation entry. Flags
            # are not membership: read barriers can clear candidate bits.
            page_owner = load_ptr(from_page, 112)
            while (
                ptr_is_null(page_owner) == 0
                and page_has_candidate == 0
                and membership_complete != 0
            ):
                owner = load_ptr(page_owner, 0)
                if ptr_eq(owner, from_obj) == 0:
                    remaining = pcc_gc_relocation_member_count(owner)
                    if remaining < 0:
                        membership_complete = 0
                    elif remaining > 0:
                        # Keep the original discoverability predicate even
                        # if an owner index is missing a page-linked owner.
                        scan_znode = pcc_gc_backend4_zpage_find(owner)
                        if (
                            ptr_is_null(scan_znode) == 0
                            and ptr_eq(load_ptr(scan_znode, 8), from_page) != 0
                        ):
                            page_has_candidate = 1
                page_owner = load_ptr(page_owner, 48)
        if membership_complete == 0:
            # A failed index insertion or inconsistent count makes the whole
            # epoch unknown. The authoritative list remains the fallback.
            scan = global_load_ptr("pcc_gc_relocation_set_head")
            while ptr_is_null(scan) == 0 and page_has_candidate == 0:
                scan_znode = pcc_gc_backend4_zpage_find(load_ptr(scan, 0))
                if (
                    ptr_is_null(scan_znode) == 0
                    and ptr_eq(load_ptr(scan_znode, 8), from_page) != 0
                ):
                    page_has_candidate = 1
                scan = load_ptr(scan, 8)
        if page_has_candidate == 0:
            prev_page = null()
            page_node = global_load_ptr(
                "pcc_gc_backend4_evacuation_page_head"
            )
            while ptr_is_null(page_node) == 0:
                next_page = load_ptr(page_node, 8)
                if ptr_eq(load_ptr(page_node, 0), from_page) != 0:
                    if ptr_eq(
                        global_load_ptr(
                            "pcc_gc_backend4_reseed_page_count_cursor"
                        ),
                        page_node,
                    ) != 0:
                        global_store_ptr(
                            "pcc_gc_backend4_reseed_page_count_cursor",
                            next_page,
                        )
                    revision: i64 = load_i64(
                        global_addr("pcc_gc_backend4_reseed_page_revision"),
                        0,
                    )
                    store_i64(
                        global_addr("pcc_gc_backend4_reseed_page_revision"),
                        0,
                        revision + 1,
                    )
                    if ptr_is_null(prev_page) != 0:
                        global_store_ptr(
                            "pcc_gc_backend4_evacuation_page_head", next_page
                        )
                    else:
                        store_ptr(prev_page, 8, next_page)
                    store_i32(from_page, 108, 0)
                    store_ptr(page_node, 8, null())
                    store_ptr(finish_plan, 8, page_node)
                    break
                prev_page = page_node
                page_node = next_page
    store_ptr(
        finish_plan,
        16,
        pcc_gc_backend4_zpage_detach_for_relocation(from_obj),
    )
    return to_obj


@c_abi_export("pcc_gc_relocate_copy")
def pcc_gc_relocate_copy(from_obj, size: i64):
    pcc_gc_config_ensure()
    pcc_py_gc_minor_graph_lock()
    eligible: i64 = 1
    flags: i64 = 0
    tag: i64 = 0
    slot_count: i64 = -1
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4:
        eligible = 0
    elif load_i64(
        global_addr("pcc_gc_backend4_reseed_commit_owner"), 0
    ) != 0:
        eligible = 0
    elif ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        eligible = 0
    elif size < 16:
        eligible = 0
    elif ptr_is_null(pcc_gc_forwarding_find(from_obj)) == 0:
        eligible = 0
    else:
        relocation_node = global_load_ptr("pcc_gc_relocation_set_head")
        while (
            ptr_is_null(relocation_node) == 0
            and ptr_eq(load_ptr(relocation_node, 0), from_obj) == 0
        ):
            relocation_node = load_ptr(relocation_node, 8)
        if ptr_is_null(relocation_node) != 0:
            eligible = 0
        else:
            flags = load_i32(from_obj, 12)
            tag = load_i32(from_obj, 8)
            if (flags & 524288) != 0 or pcc_gc_object_is_address_pinned(from_obj) != 0:
                eligible = 0
            elif pcc_gc_backend4_relocate_copy_supported_tag(tag) == 0:
                eligible = 0
            else:
                known_size: i64 = pcc_gc_object_known_size(from_obj)
                if known_size <= 0 or size > known_size:
                    eligible = 0
                else:
                    slot_count = pcc_gc_relocation_payload_slot_count_locked(
                        from_obj
                    )
                    if slot_count < 0:
                        eligible = 0
    pcc_py_gc_minor_graph_unlock()
    if eligible == 0:
        return null()

    payload_plan = pcc_gc_relocation_payload_plan_prepare(slot_count)
    if ptr_is_null(payload_plan) != 0:
        return null()
    pcc_py_gc_minor_graph_lock()
    raw_snapshot: i64 = pcc_gc_relocation_payload_raw_snapshot_locked(
        from_obj, tag, size, payload_plan
    )
    pcc_py_gc_minor_graph_unlock()
    if raw_snapshot == 0 or pcc_gc_relocation_payload_raw_prepare(
        payload_plan
    ) == 0:
        pcc_gc_relocation_payload_plan_finish(payload_plan)
        return null()
    to_obj = pcc_gc_alloc(size, tag, (flags & ~10240) | 64)
    if ptr_is_null(to_obj) != 0:
        pcc_gc_relocation_payload_plan_finish(payload_plan)
        return null()
    forwarding_plan = pcc_gc_forwarding_install_plan_prepare(from_obj, to_obj)
    if ptr_is_null(forwarding_plan) != 0:
        pcc_gc_relocation_payload_plan_finish(payload_plan)
        py_decref(to_obj)
        return null()
    finish_plan = stack_alloc(24)
    store_ptr(finish_plan, 0, null())
    store_ptr(finish_plan, 8, null())
    store_ptr(finish_plan, 16, null())
    pcc_py_gc_minor_graph_lock()
    valid_plan: i64 = pcc_gc_relocation_payload_plan_validate_locked(
        from_obj, to_obj, size, payload_plan
    )
    if valid_plan != 0:
        valid_plan = pcc_gc_relocation_payload_raw_validate_locked(
            from_obj, to_obj, tag, size, payload_plan
        )
    committed = null()
    if valid_plan != 0:
        committed = pcc_gc_backend4_relocate_copy_preallocated_unlocked(
            from_obj,
            size,
            to_obj,
            payload_plan,
            forwarding_plan,
            finish_plan,
        )
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_relocation_payload_plan_finish(payload_plan)
    detached = load_ptr(finish_plan, 0)
    if ptr_is_null(detached) == 0:
        free(detached)
    detached = load_ptr(finish_plan, 8)
    if ptr_is_null(detached) == 0:
        free(detached)
    detached = load_ptr(finish_plan, 16)
    pcc_gc_backend4_zpage_finish_relocation_detach(detached)
    pcc_gc_forwarding_install_plan_finish(forwarding_plan)
    if ptr_is_null(committed) != 0:
        py_decref(to_obj)
    return committed
