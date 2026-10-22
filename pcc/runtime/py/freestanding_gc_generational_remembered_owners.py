"""Backend 3 remembered-owner queue and budgeted overflow scanning."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_load_i64,
    atomic_store_i64,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_object_list_head = extern("pcc_gc_object_list_head", (), c_ptr)
pcc_gc_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)
pcc_gc_object_node_next = extern("pcc_gc_object_node_next", (c_ptr,), c_ptr)
pcc_gc_trace_referents_for_promotion = extern(
    "pcc_gc_trace_referents_for_promotion", (c_ptr,), c_void
)
@c_abi_export("pcc_gc_backend3_remembered_owner_list_head")
def pcc_gc_backend3_remembered_owner_list_head() -> c_ptr:
    return global_load_ptr("pcc_gc_backend3_remembered_owner_head")


@c_abi_export("pcc_gc_backend3_remembered_owner_list_set_head")
def pcc_gc_backend3_remembered_owner_list_set_head(head: c_ptr) -> None:
    global_store_ptr("pcc_gc_backend3_remembered_owner_head", head)


@c_abi_export("pcc_gc_backend3_remember_owner")
def pcc_gc_backend3_remember_owner(owner: c_ptr, owner_flags: i64) -> None:
    if ptr_is_null(owner) != 0:
        return
    if is_tagged_int(owner) != 0:
        return
    if (owner_flags & 512) != 0:
        return
    allocation_limit: i64 = atomic_load_i64(
        global_addr("pcc_gc_backend3_remembered_owner_allocation_limit"),
        0,
        "acquire",
    )
    node = null()
    if allocation_limit != 0:
        node = malloc(16)
        if ptr_is_null(node) == 0 and allocation_limit > 0:
            atomic_store_i64(
                global_addr(
                    "pcc_gc_backend3_remembered_owner_allocation_limit"
                ),
                0,
                allocation_limit - 1,
                "release",
            )
    if ptr_is_null(node) != 0:
        store_i32(global_addr("pcc_gc_backend3_remembered_overflow"), 0, 1)
        global_store_ptr("pcc_gc_backend3_remembered_scan_cursor", null())
        store_i64(
            global_addr("pcc_gc_backend3_remembered_scan_revision"), 0, 0
        )
        store_i32(owner, 12, owner_flags | 512)
        return
    store_ptr(node, 0, owner)
    store_ptr(node, 8, pcc_gc_backend3_remembered_owner_list_head())
    pcc_gc_backend3_remembered_owner_list_set_head(node)
    store_i32(owner, 12, owner_flags | 512)


@c_abi_export("pcc_gc_backend3_clear_remembered_owners")
def pcc_gc_backend3_clear_remembered_owners() -> c_ptr:
    node = pcc_gc_backend3_remembered_owner_list_head()
    pcc_gc_backend3_remembered_owner_list_set_head(null())
    store_i32(global_addr("pcc_gc_backend3_remembered_overflow"), 0, 0)
    global_store_ptr("pcc_gc_backend3_remembered_scan_cursor", null())
    store_i64(global_addr("pcc_gc_backend3_remembered_scan_revision"), 0, 0)
    return node


@c_abi_export("pcc_gc_backend3_remembered_scan_probe_config")
def pcc_gc_backend3_remembered_scan_probe_config(
    allocation_limit: i64,
) -> None:
    atomic_store_i64(
        global_addr("pcc_gc_backend3_remembered_owner_allocation_limit"),
        0,
        allocation_limit,
        "release",
    )


@c_abi_export("pcc_gc_backend3_finish_detached_remembered_owners")
def pcc_gc_backend3_finish_detached_remembered_owners(head: c_ptr) -> None:
    while ptr_is_null(head) == 0:
        nxt = load_ptr(head, 8)
        free(head)
        head = nxt


@c_abi_export("pcc_gc_backend3_scan_remembered_owners")
def pcc_gc_backend3_scan_remembered_owners(remaining_budget: i64) -> i64:
    local_examined: i64 = 0
    object_revision: i64 = load_i64(
        global_addr("pcc_gc_object_list_revision"), 0
    )
    scan_revision: i64 = load_i64(
        global_addr("pcc_gc_backend3_remembered_scan_revision"), 0
    )
    if scan_revision != object_revision:
        global_store_ptr(
            "pcc_gc_backend3_remembered_scan_cursor",
            pcc_gc_object_list_head(),
        )
        store_i64(
            global_addr("pcc_gc_backend3_remembered_scan_revision"),
            0,
            object_revision,
        )
    node = global_load_ptr("pcc_gc_backend3_remembered_scan_cursor")
    while ptr_is_null(node) == 0 and local_examined < remaining_budget:
        nxt = pcc_gc_object_node_next(node)
        global_store_ptr("pcc_gc_backend3_remembered_scan_cursor", nxt)
        local_examined = local_examined + 1
        if pcc_gc_object_node_is_active(node) == 0:
            node = nxt
            continue
        owner = load_ptr(node, 0)
        flags: i64 = load_i32(owner, 12)
        if (flags & 512) != 0:
            pcc_gc_trace_referents_for_promotion(owner)
            store_i32(owner, 12, flags & ~512)
        node = nxt
    return local_examined


@c_abi_export("pcc_gc_backend3_drain_remembered_owners")
def pcc_gc_backend3_drain_remembered_owners(
    remaining_budget: i64, detached_out: c_ptr
) -> i64:
    if ptr_is_null(detached_out) != 0:
        return 0
    if ptr_is_null(load_ptr(detached_out, 0)) == 0:
        return 0
    local_processed: i64 = 0
    if load_i32(global_addr("pcc_gc_backend3_remembered_overflow"), 0) != 0:
        if ptr_is_null(
            global_load_ptr("pcc_gc_backend3_remembered_scan_cursor")
        ) != 0:
            store_ptr(
                detached_out, 0, pcc_gc_backend3_clear_remembered_owners()
            )
            store_i32(
                global_addr("pcc_gc_backend3_remembered_overflow"), 0, 1
            )
            global_store_ptr(
                "pcc_gc_backend3_remembered_scan_cursor",
                pcc_gc_object_list_head(),
            )
            store_i64(
                global_addr("pcc_gc_backend3_remembered_scan_revision"),
                0,
                load_i64(global_addr("pcc_gc_object_list_revision"), 0),
            )
        local_processed = pcc_gc_backend3_scan_remembered_owners(
            remaining_budget
        )
        if ptr_is_null(
            global_load_ptr("pcc_gc_backend3_remembered_scan_cursor")
        ) != 0:
            store_i32(
                global_addr("pcc_gc_backend3_remembered_overflow"), 0, 0
            )
            store_i64(
                global_addr("pcc_gc_backend3_remembered_scan_revision"), 0, 0
            )
        return local_processed
    while (
        ptr_is_null(pcc_gc_backend3_remembered_owner_list_head()) == 0
        and local_processed < remaining_budget
    ):
        node = pcc_gc_backend3_remembered_owner_list_head()
        pcc_gc_backend3_remembered_owner_list_set_head(load_ptr(node, 8))
        owner = load_ptr(node, 0)
        store_ptr(node, 8, load_ptr(detached_out, 0))
        store_ptr(detached_out, 0, node)
        if pcc_gc_object_is_known_no_lock(owner) == 0:
            continue
        flags: i64 = load_i32(owner, 12)
        if (flags & 512) == 0:
            continue
        pcc_gc_trace_referents_for_promotion(owner)
        store_i32(owner, 12, flags & ~512)
        local_processed = local_processed + 1
    return local_processed
