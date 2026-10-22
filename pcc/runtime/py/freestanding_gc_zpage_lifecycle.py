"""Backend 4 ZPage cache, unlink, and owner-removal lifecycle."""

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
    null,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend4_zpage_clear_active_page = extern(
    "pcc_gc_backend4_zpage_clear_active_page", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_node_release = extern(
    "pcc_gc_backend4_zpage_node_release", (c_ptr,), c_void
)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_dealloc_cascade_active = extern(
    "pcc_dealloc_cascade_active", (), c_int64
)
pcc_dealloc_trash_pending = extern(
    "pcc_dealloc_trash_pending", (), c_int64
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_object_known_size = extern(
    "pcc_gc_object_known_size", (c_ptr,), c_int64
)
pcc_gc_object_node_freeing = extern(
    "pcc_gc_object_node_freeing", (c_ptr,), c_int64
)
pcc_gc_object_node_zpage = extern(
    "pcc_gc_object_node_zpage", (c_ptr,), c_ptr
)
pcc_gc_object_node_set_zpage = extern(
    "pcc_gc_object_node_set_zpage", (c_ptr, c_ptr), c_void
)
pcc_gc_zpage_owner_index_find = extern(
    "pcc_gc_zpage_owner_index_find", (c_ptr,), c_ptr
)
pcc_gc_zpage_owner_index_remove = extern(
    "pcc_gc_zpage_owner_index_remove", (c_ptr,), c_ptr
)


@c_abi_export("pcc_gc_backend4_free_page_count_for_class")
def pcc_gc_backend4_free_page_count_for_class(page_class: i64) -> i64:
    page = global_load_ptr("pcc_gc_backend4_free_page_head")
    count: i64 = 0
    while ptr_is_null(page) == 0:
        if load_i32(page, 24) == page_class:
            count = count + 1
        page = load_ptr(page, 56)
    return count


@c_abi_export("pcc_gc_backend4_free_page_limit_for_class")
def pcc_gc_backend4_free_page_limit_for_class(page_class: i64) -> i64:
    if page_class == 0:
        return 8
    if page_class == 1:
        return 4
    return 0


@c_abi_export("pcc_gc_backend4_zpage_clear_reusable_state")
def pcc_gc_backend4_zpage_clear_reusable_state(page) -> None:
    if ptr_is_null(page) != 0:
        return
    store_ptr(page, 0, null())
    store_i64(page, 8, 0)
    store_i64(page, 32, 0)
    store_i64(page, 40, 0)
    store_i64(page, 48, 0)
    store_i64(page, 64, 0)
    store_i64(page, 88, 0)
    store_i64(page, 96, 0)
    if load_i32(page, 104) == 1:
        # A page still counted for the post-drain sweep leaves that count.
        counter = global_addr("pcc_gc_backend4_deferred_recycle_pages")
        counted: i64 = load_i64(counter, 0)
        if counted > 0:
            store_i64(counter, 0, counted - 1)
    store_i32(page, 104, 0)
    store_i32(page, 108, 0)
    store_ptr(page, 112, null())
    card: i64 = 0
    while card < 32:
        store_i64(page, 120 + card * 8, 0)
        card = card + 1


@c_abi_export("pcc_gc_backend4_zpage_cache")
def pcc_gc_backend4_zpage_cache(page) -> None:
    if ptr_is_null(page) != 0:
        return
    pcc_gc_backend4_zpage_clear_active_page(page)
    pcc_gc_backend4_zpage_clear_reusable_state(page)
    store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_free_page_head"))
    global_store_ptr("pcc_gc_backend4_free_page_head", page)


@c_abi_export("pcc_gc_backend4_zpage_destroy")
def pcc_gc_backend4_zpage_destroy(page) -> None:
    if ptr_is_null(page) != 0:
        return
    pcc_gc_backend4_zpage_clear_active_page(page)
    # Physical release is deliberately delayed by forwarding retirement:
    # parked -> retained -> released spans cross two complete remap epochs.
    # This list is quarantine, not a permanent ownership sink.
    pcc_gc_backend4_zpage_clear_reusable_state(page)
    store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_retained_page_head"))
    global_store_ptr("pcc_gc_backend4_retained_page_head", page)


@c_abi_export("pcc_gc_backend4_zpage_recycle")
def pcc_gc_backend4_zpage_recycle(page) -> None:
    if ptr_is_null(page) != 0:
        return
    pcc_gc_backend4_zpage_clear_active_page(page)
    page_class: i64 = load_i32(page, 24)
    if page_class > 1:
        pcc_gc_backend4_zpage_destroy(page)
        return
    limit: i64 = pcc_gc_backend4_free_page_limit_for_class(page_class)
    if (
        limit <= 0
        or pcc_gc_backend4_free_page_count_for_class(page_class) >= limit
    ):
        pcc_gc_backend4_zpage_destroy(page)
        return
    pcc_gc_backend4_zpage_cache(page)


@c_abi_export("pcc_gc_backend4_sweep_deferred_recycles")
def pcc_gc_backend4_sweep_deferred_recycles() -> None:
    # Complete page recycles deferred during a dealloc trash cascade.
    # Pages whose count hit zero while pcc_dealloc_cascade_active() carry
    # the deferred flag (page+104) and stay on the live page list so no
    # allocation path can reset their span while trash-queued objects
    # still live in it. Called from pcc_dealloc_with_trash once the
    # top-level drain has emptied the queue. The global deferred-page
    # counter makes the no-deferral case O(1): walking the whole live
    # page list per top-level cascade dominated the GC4 longrun profile.
    # Only flag 1 is counted. Flag 2 pages still carry forwarding entries;
    # forwarding retirement completes them, so a sweep never walks the page
    # list for pages it cannot complete (a pcc1 GC4 compile once held
    # hundreds of those and walked ~7000 pages per cascade).
    # Every thread's allocation and free paths change the page list and the
    # counter under the graph lock, so the sweep runs under it as well, and
    # only once no thread is inside a cascade: one that began after the
    # caller left its own completes the sweep when it drains. The unlocked
    # peek keeps the no-deferral case O(1); a page deferred after it belongs
    # to a cascade that sweeps at its end.
    counter = global_addr("pcc_gc_backend4_deferred_recycle_pages")
    if load_i64(counter, 0) <= 0:
        return
    pcc_py_gc_minor_graph_lock()
    swept: i64 = 0
    remaining: i64 = 0
    page = null()
    if pcc_dealloc_cascade_active() == 0:
        swept = 1
        remaining = load_i64(counter, 0)
        page = global_load_ptr("pcc_gc_backend4_page_head")
    while ptr_is_null(page) == 0 and remaining > 0:
        nxt = load_ptr(page, 56)
        if load_i32(page, 104) == 1:
            if load_i64(page, 96) > 0:
                store_i32(page, 104, 2)
                remaining = remaining - 1
            elif load_i64(page, 32) > 0:
                # Live again: the free path defers or recycles it once it
                # empties, so it is no longer pending here.
                store_i32(page, 104, 0)
                remaining = remaining - 1
            elif load_i64(page, 88) <= 0:
                store_i32(page, 104, 0)
                remaining = remaining - 1
                pcc_gc_backend4_zpage_unlink_page(page)
                pcc_gc_backend4_zpage_recycle(page)
        page = nxt
    if swept != 0:
        store_i64(counter, 0, remaining)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_backend4_zpage_page_head")
def pcc_gc_backend4_zpage_page_head(page):
    if ptr_is_null(page) != 0:
        return null()
    return load_ptr(page, 112)


@c_abi_export("pcc_gc_backend4_zpage_set_page_head")
def pcc_gc_backend4_zpage_set_page_head(page, head) -> None:
    if ptr_is_null(page) != 0:
        return
    store_ptr(page, 112, head)
    if ptr_is_null(head) == 0:
        store_ptr(page, 0, load_ptr(head, 0))
    else:
        store_ptr(page, 0, null())


@c_abi_export("pcc_gc_backend4_zpage_unlink_node")
def pcc_gc_backend4_zpage_unlink_node(node) -> None:
    if ptr_is_null(node) != 0:
        return
    pcc_gc_zpage_owner_index_remove(load_ptr(node, 0))
    prev = load_ptr(node, 40)
    nxt = load_ptr(node, 16)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend4_selector_scan_cursor"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend4_selector_scan_cursor", nxt)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend4_selector_scan_best"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend4_selector_scan_best", null())
        store_i64(
            global_addr("pcc_gc_backend4_selector_scan_best_score"), 0, -1
        )
        store_i32(
            global_addr("pcc_gc_backend4_selector_scan_restart"), 0, 1
        )
    page_next = load_ptr(node, 48)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend4_selector_page_cursor"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend4_selector_page_cursor", page_next)
    if ptr_eq(
        global_load_ptr("pcc_gc_backend4_selector_page_seed"), node
    ) != 0:
        global_store_ptr("pcc_gc_backend4_selector_page_seed", null())
        store_i32(
            global_addr("pcc_gc_backend4_selector_page_seed_pending"), 0, 0
        )
    if ptr_is_null(prev) != 0:
        if ptr_eq(global_load_ptr("pcc_gc_backend4_zpage_head"), node) != 0:
            global_store_ptr("pcc_gc_backend4_zpage_head", nxt)
    else:
        store_ptr(prev, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 40, prev)
    page = load_ptr(node, 8)
    page_prev = load_ptr(node, 56)
    if ptr_is_null(page) == 0:
        if ptr_is_null(page_prev) != 0:
            pcc_gc_backend4_zpage_set_page_head(page, page_next)
        else:
            store_ptr(page_prev, 48, page_next)
        if ptr_is_null(page_next) == 0:
            store_ptr(page_next, 56, page_prev)
        if ptr_is_null(page_prev) == 0:
            head = pcc_gc_backend4_zpage_page_head(page)
            if ptr_is_null(head) == 0:
                store_ptr(page, 0, load_ptr(head, 0))
            else:
                store_ptr(page, 0, null())
    store_ptr(node, 16, null())
    store_ptr(node, 40, null())
    store_ptr(node, 48, null())
    store_ptr(node, 56, null())


@c_abi_export("pcc_gc_backend4_zpage_find")
def pcc_gc_backend4_zpage_find(owner):
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return null()
    obj_node = pcc_gc_object_index_find(owner)
    if ptr_is_null(obj_node) == 0:
        if pcc_gc_object_node_freeing(obj_node) == 0:
            znode = pcc_gc_object_node_zpage(obj_node)
            if ptr_is_null(znode) == 0:
                return znode
    return pcc_gc_zpage_owner_index_find(owner)


@c_abi_export("pcc_gc_backend4_zpage_unlink_page")
def pcc_gc_backend4_zpage_unlink_page(page) -> None:
    if ptr_is_null(page) != 0:
        return
    prev = null()
    cur = global_load_ptr("pcc_gc_backend4_page_head")
    while ptr_is_null(cur) == 0:
        nxt = load_ptr(cur, 56)
        if ptr_eq(cur, page) != 0:
            if ptr_is_null(prev) != 0:
                global_store_ptr("pcc_gc_backend4_page_head", nxt)
            else:
                store_ptr(prev, 56, nxt)
            return
        prev = cur
        cur = nxt


@c_abi_export("pcc_gc_backend4_zpage_find_owner_for_page")
def pcc_gc_backend4_zpage_find_owner_for_page(page):
    if ptr_is_null(page) != 0:
        return null()
    node = pcc_gc_backend4_zpage_page_head(page)
    if ptr_is_null(node) != 0:
        return null()
    return load_ptr(node, 0)


@c_abi_export("pcc_gc_backend4_zpage_remove_payload_spans")
def pcc_gc_backend4_zpage_remove_payload_spans(owner_node) -> None:
    if ptr_is_null(owner_node) != 0:
        return
    node = load_ptr(owner_node, 64)
    while ptr_is_null(node) == 0:
        page = load_ptr(node, 32)
        size: i64 = load_i64(node, 16)
        offset: i64 = load_i64(node, 24)
        if ptr_is_null(page) == 0 and size > 0 and offset >= 0:
            allocated: i64 = load_i64(page, 64)
            if offset >= 0 and allocated == offset + size:
                store_i64(page, 64, offset)
            used: i64 = load_i64(page, 8)
            if used >= size:
                store_i64(page, 8, used - size)
            else:
                store_i64(page, 8, 0)
        node = load_ptr(node, 40)
    _zpage_free_detached_payload_spans(owner_node)


@c_abi_export("pcc_gc_backend4_zpage_free_detached_payload_spans")
def _zpage_free_detached_payload_spans(owner_node) -> None:
    if ptr_is_null(owner_node) != 0:
        return
    node = load_ptr(owner_node, 64)
    store_ptr(owner_node, 64, null())
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 40)
        free(node)
        node = nxt


@c_abi_export("pcc_gc_backend4_zpage_remove_payload_span_base")
def pcc_gc_backend4_zpage_remove_payload_span_base(owner_node, base) -> i64:
    if ptr_is_null(owner_node) != 0 or ptr_is_null(base) != 0:
        return 0
    removed: i64 = 0
    prev = null()
    node = load_ptr(owner_node, 64)
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 40)
        if ptr_eq(load_ptr(node, 8), base) == 0:
            prev = node
            node = nxt
            continue
        if ptr_is_null(prev) != 0:
            store_ptr(owner_node, 64, nxt)
        else:
            store_ptr(prev, 40, nxt)
        page = load_ptr(node, 32)
        size: i64 = load_i64(node, 16)
        offset: i64 = load_i64(node, 24)
        if ptr_is_null(page) == 0 and size > 0 and offset >= 0:
            used: i64 = load_i64(page, 8)
            if used >= size:
                store_i64(page, 8, used - size)
            else:
                store_i64(page, 8, 0)
        free(node)
        removed = removed + 1
        node = nxt
    return removed


@c_abi_export("pcc_gc_backend4_zpage_detach_for_relocation")
def pcc_gc_backend4_zpage_detach_for_relocation(owner):
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return null()
    obj_node = pcc_gc_object_index_find(owner)
    node = null()
    if ptr_is_null(obj_node) == 0:
        node = pcc_gc_object_node_zpage(obj_node)
        pcc_gc_object_node_set_zpage(obj_node, null())
    indexed = pcc_gc_zpage_owner_index_find(owner)
    if ptr_is_null(node) != 0:
        node = indexed
    if ptr_is_null(node) != 0:
        # Both indexes missing means the owner is not tracked (any more):
        # every linked node is reachable through the object index or the
        # zpage owner index, so the historical full node-list fallback
        # scan could only ever confirm the miss. It turned deep dealloc
        # cascades quadratic — each object's second zpage_remove (from
        # the free path, after note_object_freeing already unlinked the
        # node) walked every remaining live node for nothing.
        return null()
    page = load_ptr(node, 8)
    pcc_gc_backend4_zpage_unlink_node(node)
    if ptr_is_null(page) == 0:
        span = load_ptr(node, 64)
        while ptr_is_null(span) == 0:
            span_page = load_ptr(span, 32)
            span_size: i64 = load_i64(span, 16)
            span_offset: i64 = load_i64(span, 24)
            if ptr_is_null(span_page) == 0 and span_size > 0 and span_offset >= 0:
                span_allocated: i64 = load_i64(span_page, 64)
                if (
                    span_offset >= 0
                    and span_allocated == span_offset + span_size
                ):
                    store_i64(span_page, 64, span_offset)
                span_used: i64 = load_i64(span_page, 8)
                if span_used >= span_size:
                    store_i64(span_page, 8, span_used - span_size)
                else:
                    store_i64(span_page, 8, 0)
            span = load_ptr(span, 40)
        size: i64 = load_i64(node, 32)
        if size <= 0:
            size = pcc_gc_object_known_size(owner)
        # Object and payload reservations are made consecutively at the
        # virtual bump tail.  Payload removal above already rewinds its own
        # consecutive tail spans; finish that transaction by reclaiming the
        # now-exposed, aligned owner reservation as well.  Without this final
        # O(1) rewind, every short-lived container leaves its header-sized
        # hole behind even when its complete owner+payload bundle was the
        # page tail.  Keep non-tail holes untouched: later live or pending
        # allocations may still occupy the bytes above them.  A forwarding
        # shell also keeps its physical header reserved until remap retirement.
        offset: i64 = load_i64(node, 24)
        alloc_size: i64 = (size + 7) & -8
        allocated: i64 = load_i64(page, 64)
        if (
            size > 0
            and offset >= 0
            and allocated == offset + alloc_size
            and load_i64(page, 96) <= 0
            # A queued death (on any thread) means this object's delayed
            # freeing note may have run when it was merely enqueued, before
            # its fields were deallocated.  Keep its physical owner bytes
            # reserved until that drain; with nothing queued anywhere the
            # fast rewind is safe.
            and pcc_dealloc_trash_pending() == 0
        ):
            store_i64(page, 64, offset)
        used: i64 = load_i64(page, 8)
        if size > 0 and used >= size:
            store_i64(page, 8, used - size)
        elif size > 0:
            store_i64(page, 8, 0)
        count: i64 = load_i64(page, 32)
        if count > 0:
            count = count - 1
            store_i64(page, 32, count)
        if ptr_eq(load_ptr(page, 0), owner) != 0:
            store_ptr(
                page, 0, pcc_gc_backend4_zpage_find_owner_for_page(page)
            )
        pending: i64 = load_i64(page, 88)
        if count <= 0 and pending <= 0:
            # Defer the recycle while a dealloc trash cascade is active:
            # this page may still own objects sitting in the trash queue
            # (their accounting was decremented above before their dealloc
            # ran). Recycling now would let a same-cascade allocation
            # (e.g. inside a __del__ handler) reset the span under them.
            # The deferred flag (page+104) is completed either by the
            # forwarding-retirement path or by
            # pcc_gc_backend4_sweep_deferred_recycles after the drain.
            if (
                load_i64(page, 96) <= 0
                and pcc_dealloc_cascade_active() == 0
            ):
                pcc_gc_backend4_zpage_unlink_page(page)
                pcc_gc_backend4_zpage_recycle(page)
            else:
                counter = global_addr(
                    "pcc_gc_backend4_deferred_recycle_pages"
                )
                flag: i64 = load_i32(page, 104)
                if load_i64(page, 96) > 0:
                    # Forwarding retirement completes a page that still
                    # carries forwarding entries; keep it out of the count.
                    if flag == 1:
                        counted: i64 = load_i64(counter, 0)
                        if counted > 0:
                            store_i64(counter, 0, counted - 1)
                    store_i32(page, 104, 2)
                else:
                    if flag != 1:
                        store_i64(counter, 0, load_i64(counter, 0) + 1)
                    store_i32(page, 104, 1)
                pcc_gc_backend4_zpage_clear_active_page(page)
    return node


@c_abi_export("pcc_gc_backend4_zpage_finish_relocation_detach")
def pcc_gc_backend4_zpage_finish_relocation_detach(node) -> None:
    if ptr_is_null(node) != 0:
        return
    _zpage_free_detached_payload_spans(node)
    free(node)


@c_abi_export("pcc_gc_backend4_zpage_remove")
def pcc_gc_backend4_zpage_remove(owner) -> None:
    node = pcc_gc_backend4_zpage_detach_for_relocation(owner)
    if ptr_is_null(node) != 0:
        return
    _zpage_free_detached_payload_spans(node)
    pcc_gc_backend4_zpage_node_release(node)
