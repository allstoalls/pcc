"""Raw object-node pool, object list, and Backend 3 young worklist."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
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
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_platform_abort = extern("pcc_platform_abort", (), c_void)

# Internal object-node tail: i64 foreign-address lease count at byte 80.
# This is not a PyObjectHeader field. Live promotion state never resets it.

pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)


@c_abi_export("pcc_gc_object_list_head")
def pcc_gc_object_list_head() -> c_ptr:
    return global_load_ptr("pcc_gc_object_head")


@c_abi_export("pcc_gc_object_set_list_head")
def pcc_gc_object_set_list_head(head: c_ptr) -> None:
    global_store_ptr("pcc_gc_object_head", head)
    revision: i64 = load_i64(global_addr("pcc_gc_object_list_revision"), 0)
    if revision == 9223372036854775807:
        revision = 1
    else:
        revision = revision + 1
    store_i64(global_addr("pcc_gc_object_list_revision"), 0, revision)


@c_abi_export("pcc_gc_trace_cursor_load")
def pcc_gc_trace_cursor_load() -> c_ptr:
    return global_load_ptr("pcc_gc_trace_cursor")


@c_abi_export("pcc_gc_trace_cursor_store")
def pcc_gc_trace_cursor_store(node: c_ptr) -> None:
    global_store_ptr("pcc_gc_trace_cursor", node)


@c_abi_export("pcc_gc_backend3_young_list_head")
def pcc_gc_backend3_young_list_head() -> c_ptr:
    return global_load_ptr("pcc_gc_backend3_young_head")


@c_abi_export("pcc_gc_backend3_young_set_head")
def pcc_gc_backend3_young_set_head(node: c_ptr) -> None:
    global_store_ptr("pcc_gc_backend3_young_head", node)


@c_abi_export("pcc_gc_object_node_size")
def pcc_gc_object_node_size(node: c_ptr) -> i64:
    return load_i64(node, 8)


@c_abi_export("pcc_gc_object_node_next")
def pcc_gc_object_node_next(node: c_ptr) -> c_ptr:
    return load_ptr(node, 16)


@c_abi_export("pcc_gc_object_node_set_next")
def pcc_gc_object_node_set_next(node: c_ptr, nxt: c_ptr) -> None:
    store_ptr(node, 16, nxt)


@c_abi_export("pcc_gc_object_node_minor_block")
def pcc_gc_object_node_minor_block(node: c_ptr) -> c_ptr:
    return load_ptr(node, 24)


@c_abi_export("pcc_gc_object_node_freeing")
def pcc_gc_object_node_freeing(node: c_ptr) -> i64:
    return load_i64(node, 32)


@c_abi_export("pcc_gc_object_node_set_freeing")
def pcc_gc_object_node_set_freeing(node: c_ptr, freeing: i64) -> None:
    store_i64(node, 32, freeing)


@c_abi_export("pcc_gc_object_node_prev")
def pcc_gc_object_node_prev(node: c_ptr) -> c_ptr:
    return load_ptr(node, 40)


@c_abi_export("pcc_gc_object_node_set_prev")
def pcc_gc_object_node_set_prev(node: c_ptr, prev: c_ptr) -> None:
    store_ptr(node, 40, prev)


@c_abi_export("pcc_gc_object_node_zpage")
def pcc_gc_object_node_zpage(node: c_ptr) -> c_ptr:
    return load_ptr(node, 48)


@c_abi_export("pcc_gc_object_node_set_zpage")
def pcc_gc_object_node_set_zpage(node: c_ptr, zpage_node: c_ptr) -> None:
    store_ptr(node, 48, zpage_node)


@c_abi_export("pcc_gc_object_node_gc_refs")
def pcc_gc_object_node_gc_refs(node: c_ptr) -> i64:
    return load_i64(node, 56)


@c_abi_export("pcc_gc_object_node_set_gc_refs")
def pcc_gc_object_node_set_gc_refs(node: c_ptr, value: i64) -> None:
    store_i64(node, 56, value)


@c_abi_export("pcc_gc_object_node_young_next")
def pcc_gc_object_node_young_next(node: c_ptr) -> c_ptr:
    return load_ptr(node, 64)


@c_abi_export("pcc_gc_object_node_set_young_next")
def pcc_gc_object_node_set_young_next(node: c_ptr, nxt: c_ptr) -> None:
    store_ptr(node, 64, nxt)


@c_abi_export("pcc_gc_object_node_young_prev")
def pcc_gc_object_node_young_prev(node: c_ptr) -> c_ptr:
    return load_ptr(node, 72)


@c_abi_export("pcc_gc_object_node_set_young_prev")
def pcc_gc_object_node_set_young_prev(node: c_ptr, prev: c_ptr) -> None:
    store_ptr(node, 72, prev)


@c_abi_export("pcc_gc_object_node_clear_promotion_state")
def _clear_promotion_state(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    store_ptr(node, 64, null())
    store_ptr(node, 72, null())
    store_i64(node, 56, 0)


@c_abi_export("pcc_gc_backend3_promotion_unlink")
def _promotion_unlink(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    obj = load_ptr(node, 0)
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if (load_i32(obj, 12) & 128) != 0:
        return
    prev = load_ptr(node, 72)
    nxt = load_ptr(node, 64)
    if (
        ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_head"), node) == 0
        and ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_tail"), node) == 0
        and ptr_is_null(prev) != 0
        and ptr_is_null(nxt) != 0
    ):
        return
    if ptr_is_null(prev) == 0:
        store_ptr(prev, 64, nxt)
    elif ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_head"), node) != 0:
        global_store_ptr("pcc_gc_backend3_promotion_head", nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 72, prev)
    elif ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_tail"), node) != 0:
        global_store_ptr("pcc_gc_backend3_promotion_tail", prev)
    _clear_promotion_state(node)


@c_abi_export("pcc_gc_object_node_alloc")
def pcc_gc_object_node_alloc() -> c_ptr:
    head = global_load_ptr("pcc_gc_object_node_free_head")
    if ptr_is_null(head) == 0:
        global_store_ptr("pcc_gc_object_node_free_head", load_ptr(head, 16))
        count: i64 = load_i32(global_addr("pcc_gc_object_node_free_count"), 0)
        if count > 0:
            store_i32(global_addr("pcc_gc_object_node_free_count"), 0, count - 1)
        if load_i64(head, 80) != 0:
            pcc_platform_abort()
            return null()
        _clear_promotion_state(head)
        return head
    node = malloc(88)
    if ptr_is_null(node) == 0:
        store_i64(node, 80, 0)
    _clear_promotion_state(node)
    return node


@c_abi_export("pcc_gc_object_node_prepare")
def pcc_gc_object_node_prepare() -> c_ptr:
    node = malloc(88)
    if ptr_is_null(node) == 0:
        store_i64(node, 80, 0)
    return node


@c_abi_export("pcc_gc_object_node_plan_requires_prepare")
def pcc_gc_object_node_plan_requires_prepare() -> i64:
    if ptr_is_null(global_load_ptr("pcc_gc_object_node_free_head")) != 0:
        return 1
    return 0


@c_abi_export("pcc_gc_object_node_take_prepared")
def pcc_gc_object_node_take_prepared(prepared_io: c_ptr) -> c_ptr:
    if ptr_is_null(prepared_io) != 0:
        return null()
    head = global_load_ptr("pcc_gc_object_node_free_head")
    if ptr_is_null(head) == 0:
        global_store_ptr("pcc_gc_object_node_free_head", load_ptr(head, 16))
        count: i64 = load_i32(global_addr("pcc_gc_object_node_free_count"), 0)
        if count > 0:
            store_i32(global_addr("pcc_gc_object_node_free_count"), 0, count - 1)
        if load_i64(head, 80) != 0:
            pcc_platform_abort()
            return null()
        _clear_promotion_state(head)
        return head
    node = load_ptr(prepared_io, 0)
    store_ptr(prepared_io, 0, null())
    if ptr_is_null(node) == 0 and load_i64(node, 80) != 0:
        pcc_platform_abort()
        return null()
    _clear_promotion_state(node)
    return node


@c_abi_export("pcc_gc_object_node_release")
def pcc_gc_object_node_release(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    if load_i64(node, 80) != 0:
        pcc_platform_abort()
        return
    _clear_promotion_state(node)
    count: i64 = load_i32(global_addr("pcc_gc_object_node_free_count"), 0)
    if count >= 8192:
        free(node)
        return
    store_ptr(node, 16, global_load_ptr("pcc_gc_object_node_free_head"))
    global_store_ptr("pcc_gc_object_node_free_head", node)
    store_i32(global_addr("pcc_gc_object_node_free_count"), 0, count + 1)


@c_abi_export("pcc_gc_object_node_finish_detached")
def pcc_gc_object_node_finish_detached(nodes: c_ptr) -> None:
    while ptr_is_null(nodes) == 0:
        node = nodes
        nodes = load_ptr(node, 16)
        if load_i64(node, 80) != 0:
            pcc_platform_abort()
            return
        store_ptr(node, 16, null())
        free(node)


@c_abi_export("pcc_gc_backend3_young_link_head")
def pcc_gc_backend3_young_link_head(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    head = pcc_gc_backend3_young_list_head()
    pcc_gc_object_node_set_young_prev(node, null())
    pcc_gc_object_node_set_young_next(node, head)
    if ptr_is_null(head) == 0:
        pcc_gc_object_node_set_young_prev(head, node)
    pcc_gc_backend3_young_set_head(node)


@c_abi_export("pcc_gc_backend3_young_unlink")
def pcc_gc_backend3_young_unlink(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    prev = pcc_gc_object_node_young_prev(node)
    nxt = pcc_gc_object_node_young_next(node)
    if ptr_is_null(prev) == 0:
        pcc_gc_object_node_set_young_next(prev, nxt)
    elif ptr_eq(pcc_gc_backend3_young_list_head(), node) != 0:
        pcc_gc_backend3_young_set_head(nxt)
    else:
        pcc_gc_object_node_set_young_next(node, null())
        pcc_gc_object_node_set_young_prev(node, null())
        return
    if ptr_is_null(nxt) == 0:
        pcc_gc_object_node_set_young_prev(nxt, prev)
    pcc_gc_object_node_set_young_next(node, null())
    pcc_gc_object_node_set_young_prev(node, null())


@c_abi_export("pcc_gc_object_node_unlink")
def pcc_gc_object_node_unlink(node: c_ptr) -> None:
    prev = pcc_gc_object_node_prev(node)
    nxt = pcc_gc_object_node_next(node)
    if ptr_eq(pcc_gc_trace_cursor_load(), node) != 0:
        pcc_gc_trace_cursor_store(nxt)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend4_reset_object_cursor"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend4_reset_object_cursor", nxt)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend3_remembered_scan_cursor"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend3_remembered_scan_cursor", nxt)
    _promotion_unlink(node)
    pcc_gc_backend3_young_unlink(node)
    if ptr_is_null(prev) != 0:
        pcc_gc_object_set_list_head(nxt)
    else:
        pcc_gc_object_node_set_next(prev, nxt)
        revision: i64 = load_i64(
            global_addr("pcc_gc_object_list_revision"), 0
        )
        if revision == 9223372036854775807:
            revision = 1
        else:
            revision = revision + 1
        store_i64(global_addr("pcc_gc_object_list_revision"), 0, revision)
    if ptr_is_null(nxt) == 0:
        pcc_gc_object_node_set_prev(nxt, prev)


@c_abi_export("pcc_gc_backend3_young_rebuild")
def pcc_gc_backend3_young_rebuild() -> None:
    pcc_gc_backend3_young_set_head(null())
    node = pcc_gc_object_list_head()
    while ptr_is_null(node) == 0:
        pcc_gc_object_node_set_young_next(node, null())
        pcc_gc_object_node_set_young_prev(node, null())
        if pcc_gc_object_node_is_active(node) != 0:
            obj = load_ptr(node, 0)
            if (load_i32(obj, 12) & 128) != 0:
                pcc_gc_backend3_young_link_head(node)
        node = pcc_gc_object_node_next(node)


@c_abi_export("pcc_gc_object_known_size")
def pcc_gc_object_known_size(obj: c_ptr) -> i64:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    node = pcc_gc_object_index_find(obj)
    if ptr_is_null(node) == 0 and pcc_gc_object_node_freeing(node) == 0:
        return pcc_gc_object_node_size(node)
    return 0


@c_abi_export("pcc_gc_live_bytes_subtract")
def pcc_gc_live_bytes_subtract(size: i64) -> None:
    if size <= 0:
        return
    live: i64 = load_i64(global_addr("pcc_gc_live_bytes"), 0)
    if size >= live:
        store_i64(global_addr("pcc_gc_live_bytes"), 0, 0)
    else:
        store_i64(global_addr("pcc_gc_live_bytes"), 0, live - size)
