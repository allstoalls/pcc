"""Native frame-root registry and GC3/GC4 thread-local node storage."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    define_thread_local_i32,
    define_thread_local_ptr_null,
    frame_address,
    free,
    gc_backend_current,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True

pcc_gc_index_slot_size = extern("pcc_gc_index_slot_size", (), c_int64)



define_thread_local_ptr_null("pcc_gc_frame_node_pool_heads")
define_thread_local_ptr_null("pcc_gc_frame_node_pool_counts")
# One cap spans every exact-size 0..16-slot bucket in this native thread.
define_thread_local_i32("pcc_gc_frame_node_pool_total", 0)


pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_cycle_requested_store_release = extern("pcc_gc_cycle_requested_store_release", (c_int64,), c_void)

pcc_gc_root_slot_count_from_map = extern("pcc_gc_root_slot_count_from_map", (c_ptr,), c_int64)
pcc_gc_root_map_is_borrowed = extern("pcc_gc_root_map_is_borrowed", (c_ptr,), c_int64)
pcc_gc_root_registry_note_mutation_locked = extern(
    "pcc_gc_root_registry_note_mutation_locked", (), c_void
)
pcc_gc_frame_index_find = extern("pcc_gc_frame_index_find", (c_ptr,), c_ptr)
pcc_gc_frame_index_plan_capacity = extern(
    "pcc_gc_frame_index_plan_capacity", (c_int64,), c_int64
)
pcc_gc_frame_index_plan_commit = extern("pcc_gc_frame_index_plan_commit", (c_ptr, c_int64, c_int64), c_int64)
pcc_gc_frame_index_replace_preallocated = extern(
    "pcc_gc_frame_index_replace_preallocated", (c_ptr, c_ptr), c_ptr
)
pcc_gc_frame_index_remove = extern("pcc_gc_frame_index_remove", (c_ptr,), c_ptr)
pcc_gc_stackmap_roots_mode = extern("pcc_gc_stackmap_roots_mode", (), c_int64)
pcc_thread_stack_high = extern("pcc_thread_stack_high", (), c_int64)


@c_abi_export("pcc_gc_frame_slots_walked")
def pcc_gc_frame_slots_walked(slots) -> i64:
    """Whether the precise stack walk roots this mapped frame's slots.

    Only the ``_mapped`` frame entries ask: compiled code that carries
    stack-map records for the frame. True while a published table makes the
    walk the root source and the slots live in a caller frame on this
    registered thread's stack; the walk then visits them for every
    collector and every mutator thread, so the frame needs no registry
    node. Otherwise (no table, a heap address) the frame registers.
    """
    if pcc_gc_stackmap_roots_mode() == 0:
        return 0
    address: i64 = ptr_to_int(slots)
    if address <= ptr_to_int(frame_address()):
        return 0
    high: i64 = pcc_thread_stack_high()
    if high == 0 or address >= high:
        return 0
    return 1


@c_abi_export("pcc_gc_frame_roots_disabled_fast")
def pcc_gc_frame_roots_disabled_fast() -> i64:
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 0:
        return 0
    if load_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0) != 0:
        return 0
    return 1


@c_abi_export("pcc_gc_should_track_frame_roots")
def pcc_gc_should_track_frame_roots() -> i64:
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 0:
        return 1
    return load_i32(global_addr("pcc_gc_backend0_frame_roots_enabled"), 0)


@c_abi_export("pcc_gc_frame_node_bucket")
def pcc_gc_frame_node_bucket(root_count: i64) -> i64:
    # Zero is a valid bucket for the allocator helper. Frame enter itself
    # remains allocation-free for a zero-root map.
    if root_count < 0 or root_count > 16:
        return -1
    return root_count


@c_abi_export("pcc_gc_frame_node_size")
def pcc_gc_frame_node_size(root_count: i64) -> i64:
    # The C oracle pins this mirrored prefix with a sizeof static assertion.
    return 64 + root_count * 8


@c_abi_export("pcc_gc_frame_node_pool_heads_get")
def pcc_gc_frame_node_pool_heads_get():
    heads = global_load_ptr("pcc_gc_frame_node_pool_heads")
    if ptr_is_null(heads) == 0:
        return heads
    heads = malloc(136)
    if ptr_is_null(heads) != 0:
        return heads
    memset(heads, 0, 136)
    global_store_ptr("pcc_gc_frame_node_pool_heads", heads)
    return heads


@c_abi_export("pcc_gc_frame_node_pool_counts_get")
def pcc_gc_frame_node_pool_counts_get():
    counts = global_load_ptr("pcc_gc_frame_node_pool_counts")
    if ptr_is_null(counts) == 0:
        return counts
    counts = malloc(136)
    if ptr_is_null(counts) != 0:
        return counts
    memset(counts, 0, 136)
    global_store_ptr("pcc_gc_frame_node_pool_counts", counts)
    return counts


@c_abi_export("pcc_gc_frame_node_alloc")
def pcc_gc_frame_node_alloc(root_count: i64):
    node_size: i64 = pcc_gc_frame_node_size(root_count)
    bucket: i64 = pcc_gc_frame_node_bucket(root_count)
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if (backend == 3 or backend == 4) and bucket >= 0:
        heads = pcc_gc_frame_node_pool_heads_get()
        counts = pcc_gc_frame_node_pool_counts_get()
        if ptr_is_null(heads) == 0 and ptr_is_null(counts) == 0:
            offset: i64 = bucket * 8
            head = load_ptr(heads, offset)
            if ptr_is_null(head) == 0:
                nxt = load_ptr(head, 16)
                store_ptr(heads, offset, nxt)
                count: i64 = load_i64(counts, offset)
                if count > 0:
                    store_i64(counts, offset, count - 1)
                total: i64 = load_i32(
                    global_addr("pcc_gc_frame_node_pool_total"), 0
                )
                if total > 0:
                    store_i32(
                        global_addr("pcc_gc_frame_node_pool_total"),
                        0,
                        total - 1,
                    )
                memset(head, 0, node_size)
                return head
    node = malloc(node_size)
    if ptr_is_null(node) == 0:
        memset(node, 0, node_size)
    return node


@c_abi_export("pcc_gc_frame_node_create")
def pcc_gc_frame_node_create(
    frame_map,
    slots,
    root_count: i64,
    extra_flags: i64,
):
    node = pcc_gc_frame_node_alloc(root_count)
    if ptr_is_null(node) != 0:
        return node
    store_ptr(node, 0, frame_map)
    store_ptr(node, 8, slots)
    store_ptr(node, 16, global_load_ptr("pcc_gc_frame_head"))
    store_ptr(node, 24, null())
    store_ptr(node, 32, null())
    store_i64(node, 40, root_count)
    store_i32(
        node,
        48,
        pcc_gc_root_map_is_borrowed(frame_map) | extra_flags,
    )
    store_ptr(node, 56, ptr_add(node, 64))
    return node


@c_abi_export("pcc_gc_frame_node_unlink")
def pcc_gc_frame_node_unlink(node) -> None:
    if ptr_is_null(node) != 0:
        return
    prev = load_ptr(node, 24)
    nxt = load_ptr(node, 16)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend3_frame_root_scan_cursor"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend3_frame_root_scan_cursor", nxt)
        store_i64(
            global_addr("pcc_gc_backend3_frame_root_scan_slot"), 0, 0
        )
    if ptr_is_null(prev) != 0:
        global_store_ptr("pcc_gc_frame_head", nxt)
    else:
        store_ptr(prev, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 24, prev)
    store_ptr(node, 16, null())
    store_ptr(node, 24, null())
    store_ptr(node, 32, null())
    pcc_gc_root_registry_note_mutation_locked()


@c_abi_export("pcc_gc_frame_node_release")
def pcc_gc_frame_node_release(node) -> None:
    if ptr_is_null(node) != 0:
        return
    root_count: i64 = load_i64(node, 40)
    bucket: i64 = pcc_gc_frame_node_bucket(root_count)
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if (backend != 3 and backend != 4) or bucket < 0:
        free(node)
        return
    heads = pcc_gc_frame_node_pool_heads_get()
    counts = pcc_gc_frame_node_pool_counts_get()
    if ptr_is_null(heads) != 0 or ptr_is_null(counts) != 0:
        free(node)
        return
    offset: i64 = bucket * 8
    count: i64 = load_i64(counts, offset)
    total: i64 = load_i32(global_addr("pcc_gc_frame_node_pool_total"), 0)
    if total >= 1024:
        free(node)
        return
    node_size: i64 = pcc_gc_frame_node_size(root_count)
    memset(node, 0, node_size)
    store_ptr(node, 16, load_ptr(heads, offset))
    store_ptr(heads, offset, node)
    store_i64(counts, offset, count + 1)
    store_i32(global_addr("pcc_gc_frame_node_pool_total"), 0, total + 1)


@c_abi_export("pcc_gc_frame_node_tls_pool_cached_count")
def pcc_gc_frame_node_tls_pool_cached_count() -> i64:
    return load_i32(global_addr("pcc_gc_frame_node_pool_total"), 0)


@c_abi_export("pcc_gc_frame_node_tls_pool_drain")
def pcc_gc_frame_node_tls_pool_drain() -> None:
    heads = global_load_ptr("pcc_gc_frame_node_pool_heads")
    counts = global_load_ptr("pcc_gc_frame_node_pool_counts")
    bucket: i64 = 0
    while bucket <= 16:
        offset: i64 = bucket * 8
        if ptr_is_null(heads) == 0:
            node = load_ptr(heads, offset)
            store_ptr(heads, offset, null())
            while ptr_is_null(node) == 0:
                nxt = load_ptr(node, 16)
                free(node)
                node = nxt
        if ptr_is_null(counts) == 0:
            store_i64(counts, offset, 0)
        bucket = bucket + 1
    if ptr_is_null(heads) == 0:
        free(heads)
        global_store_ptr("pcc_gc_frame_node_pool_heads", null())
    if ptr_is_null(counts) == 0:
        free(counts)
        global_store_ptr("pcc_gc_frame_node_pool_counts", null())
    store_i32(global_addr("pcc_gc_frame_node_pool_total"), 0, 0)


@c_abi_export("pcc_gc_note_frame_enter")
def pcc_gc_note_frame_enter(frame_map, slots) -> None:
    if pcc_gc_frame_roots_disabled_fast() != 0:
        return
    gc_backend_current()
    if pcc_gc_should_track_frame_roots() == 0:
        return
    if ptr_is_null(frame_map) != 0 or ptr_is_null(slots) != 0:
        return
    root_count: i64 = pcc_gc_root_slot_count_from_map(frame_map)
    if root_count <= 0:
        return
    node = pcc_gc_frame_node_create(frame_map, slots, root_count, 0)
    if ptr_is_null(node) != 0:
        return
    prepared = null()
    prepared_cap: i64 = 0
    prepared_slot = stack_alloc(8)
    while True:
        pcc_py_gc_minor_graph_lock()
        required: i64 = pcc_gc_frame_index_plan_capacity(1)
        if required < 0:
            pcc_py_gc_minor_graph_unlock()
            if ptr_is_null(prepared) == 0:
                free(prepared)
            pcc_gc_frame_node_release(node)
            return
        if required > 0 and (
            ptr_is_null(prepared) != 0 or prepared_cap < required
        ):
            pcc_py_gc_minor_graph_unlock()
            if ptr_is_null(prepared) == 0:
                free(prepared)
            prepared = malloc(required * pcc_gc_index_slot_size())
            if ptr_is_null(prepared) != 0:
                pcc_gc_frame_node_release(node)
                return
            memset(prepared, 0, required * pcc_gc_index_slot_size())
            prepared_cap = required
            continue
        if required > 0:
            store_ptr(prepared_slot, 0, prepared)
            if (
                pcc_gc_frame_index_plan_commit(
                    prepared_slot, prepared_cap, 1
                )
                < 0
            ):
                pcc_py_gc_minor_graph_unlock()
                free(prepared)
                prepared = null()
                prepared_cap = 0
                continue
            prepared = load_ptr(prepared_slot, 0)
        old_head = global_load_ptr("pcc_gc_frame_head")
        store_ptr(node, 16, old_head)
        if ptr_is_null(old_head) == 0:
            store_ptr(old_head, 24, node)
        global_store_ptr("pcc_gc_frame_head", node)
        pcc_gc_root_registry_note_mutation_locked()
        duplicate = pcc_gc_frame_index_replace_preallocated(slots, node)
        if ptr_eq(duplicate, node) != 0:
            pcc_gc_frame_node_unlink(node)
            pcc_py_gc_minor_graph_unlock()
            if ptr_is_null(prepared) == 0:
                free(prepared)
            pcc_gc_frame_node_release(node)
            return
        store_ptr(node, 32, duplicate)
        # A frame enter or leave genuinely is new tracing work for an incremental
        # marker, so requesting a cycle is right for a mutator.  It is wrong for the
        # collector itself: pcc_gc_collect stops the world and drains with
        # `while pcc_gc_step(...) != 0`, and every call inside that drain enters and
        # leaves a GC frame because this runtime is compiled pcc-Python.  Each such
        # transition re-armed pcc_gc_cycle_requested, so completing a cycle
        # immediately requested the next one and the drain never terminated -- the
        # gray count was measured oscillating 0 -> 3 -> 0 forever with no mutator
        # running, both halves reporting progress.  The C runtime never hits this
        # because its collector is C and registers no GC frames.  The world is
        # stopped for the whole explicit window, so a frame transition seen there is
        # always the collector's own.
        if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) == 0:
            pcc_gc_cycle_requested_store_release(1)
        pcc_py_gc_minor_graph_unlock()
        if ptr_is_null(prepared) == 0:
            free(prepared)
        return


@c_abi_export("pcc_gc_note_frame_enter_lifo")
def pcc_gc_note_frame_enter_lifo(frame_map, slots) -> None:
    if pcc_gc_frame_roots_disabled_fast() != 0:
        return
    gc_backend_current()
    if pcc_gc_should_track_frame_roots() == 0:
        return
    if ptr_is_null(frame_map) != 0 or ptr_is_null(slots) != 0:
        return
    root_count: i64 = pcc_gc_root_slot_count_from_map(frame_map)
    if root_count <= 0:
        return
    node = pcc_gc_frame_node_create(frame_map, slots, root_count, 2)
    if ptr_is_null(node) != 0:
        return
    pcc_py_gc_minor_graph_lock()
    old_head = global_load_ptr("pcc_gc_frame_head")
    store_ptr(node, 16, old_head)
    if ptr_is_null(old_head) == 0:
        store_ptr(old_head, 24, node)
    global_store_ptr("pcc_gc_frame_head", node)
    pcc_gc_root_registry_note_mutation_locked()
    # Collector-internal frame transitions must not re-arm the cycle; see the
    # note on the first of these four sites.
    if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) == 0:
        pcc_gc_cycle_requested_store_release(1)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_note_frame_leave_lifo")
def pcc_gc_note_frame_leave_lifo(slots) -> None:
    if pcc_gc_frame_roots_disabled_fast() != 0:
        return
    gc_backend_current()
    if pcc_gc_should_track_frame_roots() == 0 or ptr_is_null(slots) != 0:
        return
    if ptr_is_null(global_load_ptr("pcc_gc_frame_head")) != 0:
        # No registry node exists at all, so this frame (walked or not)
        # has none to unlink: skip the graph lock.
        return
    released = null()
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 8), slots) != 0 and (load_i32(node, 48) & 2) != 0:
            pcc_gc_frame_node_unlink(node)
            # Collector-internal frame transitions must not re-arm the cycle; see the
            # note on the first of these four sites.
            if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) == 0:
                pcc_gc_cycle_requested_store_release(1)
            released = node
            break
        node = load_ptr(node, 16)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_node_release(released)


@c_abi_export("pcc_gc_note_frame_leave")
def pcc_gc_note_frame_leave(slots) -> None:
    if pcc_gc_frame_roots_disabled_fast() != 0:
        return
    backend: i64 = gc_backend_current()
    if pcc_gc_should_track_frame_roots() == 0 or ptr_is_null(slots) != 0:
        return
    if ptr_is_null(global_load_ptr("pcc_gc_frame_head")) != 0:
        # No registry node exists (walked frames register none).
        return
    released = null()
    pcc_py_gc_minor_graph_lock()
    if backend == 0 and ptr_is_null(global_load_ptr("pcc_gc_frame_head")) != 0:
        pcc_py_gc_minor_graph_unlock()
        return
    indexed = pcc_gc_frame_index_find(slots)
    if ptr_is_null(indexed) != 0:
        pcc_py_gc_minor_graph_unlock()
        return
    if ptr_eq(load_ptr(indexed, 8), slots) != 0:
        duplicate = load_ptr(indexed, 32)
        pcc_gc_frame_node_unlink(indexed)
        if ptr_is_null(duplicate) == 0:
            pcc_gc_frame_index_replace_preallocated(slots, duplicate)
        else:
            pcc_gc_frame_index_remove(slots)
        released = indexed
        # Collector-internal frame transitions must not re-arm the cycle; see the
        # note on the first of these four sites.
        if load_i32(global_addr("pcc_gc_explicit_collect_active"), 0) == 0:
            pcc_gc_cycle_requested_store_release(1)
    else:
        pcc_gc_frame_index_remove(slots)
        pcc_gc_frame_index_replace_preallocated(load_ptr(indexed, 8), indexed)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_node_release(released)
