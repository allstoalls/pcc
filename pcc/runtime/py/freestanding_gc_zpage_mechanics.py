"""Backend 4 ZPage page selection, reset, and node-list mechanics."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, extern
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
    memset,
    null,
    ptr_diff,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend4_evacuation_page_find = extern(
    "pcc_gc_backend4_evacuation_page_find", (c_ptr,), c_ptr
)
pcc_gc_zpage_owner_index_upsert = extern(
    "pcc_gc_zpage_owner_index_upsert", (c_ptr, c_ptr), c_int64
)
pcc_gc_zpage_owner_index_upsert_preallocated = extern(
    "pcc_gc_zpage_owner_index_upsert_preallocated", (c_ptr, c_ptr), c_int64
)


@c_abi_export("pcc_gc_backend4_zpage_active_page")
def pcc_gc_backend4_zpage_active_page(page_class: i64, generation: i64):
    if page_class == 0:
        if generation == 2:
            return global_load_ptr("pcc_gc_backend4_active_small_old_page")
        return global_load_ptr("pcc_gc_backend4_active_small_young_page")
    if page_class == 1:
        if generation == 2:
            return global_load_ptr("pcc_gc_backend4_active_medium_old_page")
        return global_load_ptr("pcc_gc_backend4_active_medium_young_page")
    return null()


@c_abi_export("pcc_gc_backend4_zpage_set_active_page")
def pcc_gc_backend4_zpage_set_active_page(page) -> None:
    if ptr_is_null(page) != 0:
        return
    page_class: i64 = load_i32(page, 24)
    generation: i64 = load_i32(page, 28)
    if page_class == 0:
        if generation == 2:
            global_store_ptr("pcc_gc_backend4_active_small_old_page", page)
        else:
            global_store_ptr("pcc_gc_backend4_active_small_young_page", page)
    elif page_class == 1:
        if generation == 2:
            global_store_ptr("pcc_gc_backend4_active_medium_old_page", page)
        else:
            global_store_ptr("pcc_gc_backend4_active_medium_young_page", page)


@c_abi_export("pcc_gc_backend4_zpage_clear_active_page")
def pcc_gc_backend4_zpage_clear_active_page(page) -> None:
    if ptr_is_null(page) != 0:
        return
    cur = global_load_ptr("pcc_gc_backend4_active_small_young_page")
    if ptr_eq(cur, page) != 0:
        global_store_ptr("pcc_gc_backend4_active_small_young_page", null())
    cur = global_load_ptr("pcc_gc_backend4_active_small_old_page")
    if ptr_eq(cur, page) != 0:
        global_store_ptr("pcc_gc_backend4_active_small_old_page", null())
    cur = global_load_ptr("pcc_gc_backend4_active_medium_young_page")
    if ptr_eq(cur, page) != 0:
        global_store_ptr("pcc_gc_backend4_active_medium_young_page", null())
    cur = global_load_ptr("pcc_gc_backend4_active_medium_old_page")
    if ptr_eq(cur, page) != 0:
        global_store_ptr("pcc_gc_backend4_active_medium_old_page", null())


@c_abi_export("pcc_gc_backend4_zpage_find_reusable_page_for_gen")
def pcc_gc_backend4_zpage_find_reusable_page_for_gen(
    size: i64, generation: i64
):
    if size <= 0 or size > 65536:
        return null()
    wanted_class: i64 = 0
    if size > 4096:
        wanted_class: i64 = 1
    alloc_size: i64 = (size + 7) & -8
    active = pcc_gc_backend4_zpage_active_page(wanted_class, generation)
    evacuation_head = global_load_ptr("pcc_gc_backend4_evacuation_page_head")
    if ptr_is_null(active) == 0:
        capacity: i64 = load_i64(active, 16)
        allocated: i64 = load_i64(active, 64)
        if (
            load_i32(active, 24) == wanted_class
            and load_i32(active, 28) == generation
            and capacity - allocated >= alloc_size
        ):
            if (
                ptr_is_null(evacuation_head) != 0
                or ptr_is_null(pcc_gc_backend4_evacuation_page_find(active)) != 0
            ):
                return active
    pcc_gc_backend4_zpage_clear_active_page(active)
    return null()


@c_abi_export("pcc_gc_backend4_zpage_find_reusable_page")
def pcc_gc_backend4_zpage_find_reusable_page(owner, size: i64):
    if size <= 0 or size > 65536:
        return null()
    wanted_class: i64 = 0
    if size > 4096:
        wanted_class: i64 = 1
    wanted_generation: i64 = 1
    if ptr_is_null(owner) == 0 and is_tagged_int(owner) == 0:
        if (load_i32(owner, 12) & 256) != 0:
            wanted_generation: i64 = 2
    alloc_size: i64 = (size + 7) & -8
    active = pcc_gc_backend4_zpage_active_page(wanted_class, wanted_generation)
    evacuation_head = global_load_ptr("pcc_gc_backend4_evacuation_page_head")
    if ptr_is_null(active) == 0:
        capacity: i64 = load_i64(active, 16)
        allocated: i64 = load_i64(active, 64)
        if (
            load_i32(active, 24) == wanted_class
            and load_i32(active, 28) == wanted_generation
            and capacity - allocated >= alloc_size
        ):
            if (
                ptr_is_null(evacuation_head) != 0
                or ptr_is_null(pcc_gc_backend4_evacuation_page_find(active)) != 0
            ):
                return active
    pcc_gc_backend4_zpage_clear_active_page(active)
    return null()


@c_abi_export("pcc_gc_backend4_zpage_pop_free_page")
def pcc_gc_backend4_zpage_pop_free_page(size: i64):
    if size <= 0 or size > 65536:
        return null()
    wanted_class: i64 = 0
    wanted_capacity: i64 = 4096
    if size > 4096:
        wanted_class: i64 = 1
        wanted_capacity: i64 = 65536
    prev = null()
    page = global_load_ptr("pcc_gc_backend4_free_page_head")
    while ptr_is_null(page) == 0:
        nxt = load_ptr(page, 56)
        if (
            load_i32(page, 24) == wanted_class
            and load_i64(page, 16) == wanted_capacity
        ):
            if ptr_is_null(prev) != 0:
                global_store_ptr("pcc_gc_backend4_free_page_head", nxt)
            else:
                store_ptr(prev, 56, nxt)
            store_ptr(page, 56, null())
            return page
        prev = page
        page = nxt
    return null()


@c_abi_export("pcc_gc_backend4_zpage_reset")
def pcc_gc_backend4_zpage_reset(page, owner, size: i64) -> None:
    if ptr_is_null(page) != 0:
        return
    capacity: i64 = 4096
    page_class: i64 = 0
    if size > 4096:
        capacity: i64 = 65536
        page_class: i64 = 1
    if size > 65536:
        page_class: i64 = 2
        capacity = (size + 65535) & -65536
    generation: i64 = 1
    if ptr_is_null(owner) == 0 and is_tagged_int(owner) == 0:
        if (load_i32(owner, 12) & 256) != 0:
            generation: i64 = 2
    store_ptr(page, 0, owner)
    store_i64(page, 8, 0)
    store_i64(page, 16, capacity)
    store_i32(page, 24, page_class)
    store_i32(page, 28, generation)
    store_i64(page, 32, 0)
    store_i64(page, 40, 0)
    store_i64(page, 48, 0)
    store_i64(page, 64, 0)
    store_i64(page, 88, 0)
    store_i64(page, 96, 0)
    store_i32(page, 104, 0)
    store_i32(page, 108, 0)
    store_ptr(page, 112, null())
    # 64 i32 remembered-card refcounts at +120 (page+48 counts non-zero ones).
    card: i64 = 0
    while card < 32:
        store_i64(page, 120 + card * 8, 0)
        card = card + 1
    span = load_ptr(page, 72)
    span_capacity: i64 = load_i64(page, 80)
    if ptr_is_null(span) != 0 or span_capacity < capacity:
        span = malloc(capacity + 256)
        store_ptr(page, 72, span)
        if ptr_is_null(span) != 0:
            store_i64(page, 80, 0)
        else:
            store_i64(page, 80, capacity)
    if ptr_is_null(span) == 0 and capacity > 0:
        memset(span, 0, capacity)


@c_abi_export("pcc_gc_backend4_zpage_node_alloc")
def pcc_gc_backend4_zpage_node_alloc():
    head = global_load_ptr("pcc_gc_backend4_zpage_node_free_head")
    if ptr_is_null(head) == 0:
        nxt = load_ptr(head, 16)
        global_store_ptr("pcc_gc_backend4_zpage_node_free_head", nxt)
        count: i64 = load_i32(
            global_addr("pcc_gc_backend4_zpage_node_free_count"), 0
        )
        if count > 0:
            store_i32(
                global_addr("pcc_gc_backend4_zpage_node_free_count"),
                0,
                count - 1,
            )
        return head
    return malloc(80)


@c_abi_export("pcc_gc_backend4_zpage_node_prepare")
def pcc_gc_backend4_zpage_node_prepare():
    return malloc(80)


@c_abi_export("pcc_gc_backend4_zpage_node_plan_requires_prepare")
def pcc_gc_backend4_zpage_node_plan_requires_prepare() -> i64:
    if ptr_is_null(
        global_load_ptr("pcc_gc_backend4_zpage_node_free_head")
    ) != 0:
        return 1
    return 0


@c_abi_export("pcc_gc_backend4_zpage_node_take_prepared")
def pcc_gc_backend4_zpage_node_take_prepared(prepared_io):
    head = global_load_ptr("pcc_gc_backend4_zpage_node_free_head")
    if ptr_is_null(head) == 0:
        nxt = load_ptr(head, 16)
        global_store_ptr("pcc_gc_backend4_zpage_node_free_head", nxt)
        count: i64 = load_i32(
            global_addr("pcc_gc_backend4_zpage_node_free_count"), 0
        )
        if count > 0:
            store_i32(
                global_addr("pcc_gc_backend4_zpage_node_free_count"),
                0,
                count - 1,
            )
        return head
    if ptr_is_null(prepared_io) != 0:
        return null()
    prepared = load_ptr(prepared_io, 0)
    if ptr_is_null(prepared) != 0:
        return null()
    store_ptr(prepared_io, 0, null())
    return prepared


@c_abi_export("pcc_gc_backend4_zpage_node_release")
def pcc_gc_backend4_zpage_node_release(node) -> None:
    if ptr_is_null(node) != 0:
        return
    count: i64 = load_i32(
        global_addr("pcc_gc_backend4_zpage_node_free_count"), 0
    )
    if count >= 8192:
        free(node)
        return
    store_ptr(node, 16, global_load_ptr("pcc_gc_backend4_zpage_node_free_head"))
    global_store_ptr("pcc_gc_backend4_zpage_node_free_head", node)
    store_i32(global_addr("pcc_gc_backend4_zpage_node_free_count"), 0, count + 1)


@c_abi_export("pcc_gc_backend4_zpage_link_node")
def pcc_gc_backend4_zpage_link_node(node) -> None:
    if ptr_is_null(node) != 0:
        return
    page = load_ptr(node, 8)
    store_ptr(node, 40, null())
    nxt = global_load_ptr("pcc_gc_backend4_zpage_head")
    store_ptr(node, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 40, node)
    global_store_ptr("pcc_gc_backend4_zpage_head", node)
    pcc_gc_zpage_owner_index_upsert(load_ptr(node, 0), node)
    page_head = null()
    if ptr_is_null(page) == 0:
        page_head = load_ptr(page, 112)
    store_ptr(node, 56, null())
    store_ptr(node, 48, page_head)
    if ptr_is_null(page_head) == 0:
        store_ptr(page_head, 56, node)
    if ptr_is_null(page) == 0:
        store_ptr(page, 112, node)
        store_ptr(page, 0, load_ptr(node, 0))


@c_abi_export("pcc_gc_backend4_zpage_link_node_preallocated")
def pcc_gc_backend4_zpage_link_node_preallocated(node) -> i64:
    if ptr_is_null(node) != 0:
        return -1
    if (
        pcc_gc_zpage_owner_index_upsert_preallocated(
            load_ptr(node, 0), node
        )
        < 0
    ):
        return -1
    page = load_ptr(node, 8)
    store_ptr(node, 40, null())
    nxt = global_load_ptr("pcc_gc_backend4_zpage_head")
    store_ptr(node, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 40, node)
    global_store_ptr("pcc_gc_backend4_zpage_head", node)
    page_head = null()
    if ptr_is_null(page) == 0:
        page_head = load_ptr(page, 112)
    store_ptr(node, 56, null())
    store_ptr(node, 48, page_head)
    if ptr_is_null(page_head) == 0:
        store_ptr(page_head, 56, node)
    if ptr_is_null(page) == 0:
        store_ptr(page, 112, node)
        store_ptr(page, 0, load_ptr(node, 0))
    return 1


@c_abi_export("pcc_gc_backend4_zpage_find_page_for_addr")
def pcc_gc_backend4_zpage_find_page_for_addr(ptr, size: i64):
    if ptr_is_null(ptr) != 0 or size <= 0:
        return null()
    alloc_size: i64 = (size + 7) & -8
    wanted_class: i64 = 0
    if size > 4096:
        wanted_class: i64 = 1
    if size > 65536:
        wanted_class: i64 = 2
    if wanted_class < 2:
        active = pcc_gc_backend4_zpage_active_page(wanted_class, 1)
        if ptr_is_null(active) == 0:
            span = load_ptr(active, 72)
            span_capacity: i64 = load_i64(active, 80)
            if ptr_is_null(span) == 0 and span_capacity > 0:
                delta: i64 = ptr_diff(ptr, span)
                if delta >= 0 and delta + alloc_size <= span_capacity:
                    return active
        active = pcc_gc_backend4_zpage_active_page(wanted_class, 2)
        if ptr_is_null(active) == 0:
            span = load_ptr(active, 72)
            span_capacity = load_i64(active, 80)
            if ptr_is_null(span) == 0 and span_capacity > 0:
                delta = ptr_diff(ptr, span)
                if delta >= 0 and delta + alloc_size <= span_capacity:
                    return active
    page = global_load_ptr("pcc_gc_backend4_page_head")
    while ptr_is_null(page) == 0:
        span = load_ptr(page, 72)
        span_capacity = load_i64(page, 80)
        if ptr_is_null(span) == 0 and span_capacity > 0:
            delta = ptr_diff(ptr, span)
            if delta >= 0 and delta + alloc_size <= span_capacity:
                return page
        page = load_ptr(page, 56)
    return null()
