"""Backend 4 ZPage allocation and owner-registration transactions."""

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
    memset,
    null,
    ptr_add,
    ptr_diff,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend4_evacuation_page_find = extern(
    "pcc_gc_backend4_evacuation_page_find", (c_ptr,), c_ptr
)
pcc_gc_backend4_zpage_active_page = extern(
    "pcc_gc_backend4_zpage_active_page", (c_int64, c_int64), c_ptr
)
pcc_gc_backend4_zpage_clear_active_page = extern(
    "pcc_gc_backend4_zpage_clear_active_page", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_find_page_for_addr = extern(
    "pcc_gc_backend4_zpage_find_page_for_addr", (c_ptr, c_int64), c_ptr
)
pcc_gc_backend4_zpage_find_reusable_page = extern(
    "pcc_gc_backend4_zpage_find_reusable_page", (c_ptr, c_int64), c_ptr
)
pcc_gc_backend4_zpage_find_reusable_page_for_gen = extern(
    "pcc_gc_backend4_zpage_find_reusable_page_for_gen", (c_int64, c_int64), c_ptr
)
pcc_gc_backend4_zpage_link_node = extern(
    "pcc_gc_backend4_zpage_link_node", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_link_node_preallocated = extern(
    "pcc_gc_backend4_zpage_link_node_preallocated", (c_ptr,), c_int64
)
pcc_gc_backend4_zpage_node_alloc = extern(
    "pcc_gc_backend4_zpage_node_alloc", (), c_ptr
)
pcc_gc_backend4_zpage_node_release = extern(
    "pcc_gc_backend4_zpage_node_release", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_node_take_prepared = extern(
    "pcc_gc_backend4_zpage_node_take_prepared", (c_ptr,), c_ptr
)
pcc_gc_backend4_zpage_pop_free_page = extern(
    "pcc_gc_backend4_zpage_pop_free_page", (c_int64,), c_ptr
)
pcc_gc_backend4_zpage_reset = extern(
    "pcc_gc_backend4_zpage_reset", (c_ptr, c_ptr, c_int64), c_void
)
pcc_gc_backend4_zpage_set_active_page = extern(
    "pcc_gc_backend4_zpage_set_active_page", (c_ptr,), c_void
)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)


@c_abi_export("pcc_gc_backend4_try_zpage_alloc")
def pcc_gc_backend4_try_zpage_alloc(size: i64, flags: i64):
    backend: i64 = 0
    if load_i32(global_addr("pcc_gc_config_initialized"), 0) == 0:
        backend = pcc_gc_config_ensure()
    else:
        backend = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend != 4 or size < 16:
        return null()
    alloc_size: i64 = (size + 7) & -8
    generation: i64 = 1
    if (flags & 256) != 0:
        generation: i64 = 2
    wanted_class: i64 = 2
    if size <= 4096:
        wanted_class: i64 = 0
    elif size <= 65536:
        wanted_class: i64 = 1

    # A detached free page stays private while reset allocates/clears its span.
    # Revalidate under the graph lock before publishing it; a fresh page that
    # loses the race is freed after unlock, while a detached page is recached.
    prepared_page = null()
    prepared_from_free: i64 = 0
    while 1:
        unused_fresh_page = null()
        pcc_py_gc_minor_graph_lock()
        if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4:
            pcc_py_gc_minor_graph_unlock()
            if ptr_is_null(prepared_page) == 0:
                free(load_ptr(prepared_page, 72))
                free(prepared_page)
            return null()

        page = null()
        active = pcc_gc_backend4_zpage_active_page(wanted_class, generation)
        if ptr_is_null(active) == 0:
            capacity: i64 = load_i64(active, 16)
            allocated: i64 = load_i64(active, 64)
            if (
                load_i32(active, 24) == wanted_class
                and load_i32(active, 28) == generation
                and capacity - allocated >= alloc_size
            ):
                evacuation_head = global_load_ptr(
                    "pcc_gc_backend4_evacuation_page_head"
                )
                if (
                    ptr_is_null(evacuation_head) != 0
                    or ptr_is_null(
                        pcc_gc_backend4_evacuation_page_find(active)
                    )
                    != 0
                ):
                    page = active
            if ptr_is_null(page) != 0:
                pcc_gc_backend4_zpage_clear_active_page(active)
        if ptr_is_null(page) != 0:
            page = pcc_gc_backend4_zpage_find_reusable_page_for_gen(
                size, generation
            )
        if ptr_is_null(page) == 0 and ptr_is_null(prepared_page) == 0:
            if prepared_from_free != 0:
                store_ptr(
                    prepared_page,
                    56,
                    global_load_ptr("pcc_gc_backend4_free_page_head"),
                )
                global_store_ptr(
                    "pcc_gc_backend4_free_page_head", prepared_page
                )
            else:
                unused_fresh_page = prepared_page
            prepared_page = null()
            prepared_from_free = 0
        if ptr_is_null(page) != 0 and ptr_is_null(prepared_page) == 0:
            page = prepared_page
            prepared_page = null()
            prepared_from_free = 0
            span = load_ptr(page, 72)
            capacity = load_i64(page, 16)
            span_capacity: i64 = load_i64(page, 80)
            if ptr_is_null(span) != 0 or span_capacity < capacity:
                pcc_py_gc_minor_graph_unlock()
                free(span)
                free(page)
                return null()
            store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_page_head"))
            global_store_ptr("pcc_gc_backend4_page_head", page)
        if ptr_is_null(page) == 0:
            span = load_ptr(page, 72)
            capacity = load_i64(page, 16)
            span_capacity = load_i64(page, 80)
            allocated = load_i64(page, 64)
            if (
                ptr_is_null(span) != 0
                or span_capacity < capacity
                or allocated < 0
                or capacity - allocated < alloc_size
            ):
                pcc_py_gc_minor_graph_unlock()
                if ptr_is_null(unused_fresh_page) == 0:
                    free(load_ptr(unused_fresh_page, 72))
                    free(unused_fresh_page)
                return null()
            obj = ptr_add(span, allocated)
            # This reservation and pending count make the unlocked zero-fill
            # invisible to selection/recycling until object tracking commits.
            store_i64(page, 64, allocated + alloc_size)
            store_i64(page, 88, load_i64(page, 88) + 1)
            pcc_gc_backend4_zpage_set_active_page(page)
            pcc_py_gc_minor_graph_unlock()
            if ptr_is_null(unused_fresh_page) == 0:
                free(load_ptr(unused_fresh_page, 72))
                free(unused_fresh_page)
            memset(obj, 0, alloc_size)
            return obj

        page = pcc_gc_backend4_zpage_pop_free_page(size)
        if ptr_is_null(page) == 0:
            pcc_py_gc_minor_graph_unlock()
            pcc_gc_backend4_zpage_reset(page, null(), size)
            store_i32(page, 28, generation)
            span = load_ptr(page, 72)
            if (
                ptr_is_null(span) != 0
                or load_i64(page, 80) < load_i64(page, 16)
            ):
                free(span)
                free(page)
                return null()
            prepared_page = page
            prepared_from_free = 1
        else:
            pcc_py_gc_minor_graph_unlock()
            page = malloc(376)
            if ptr_is_null(page) != 0:
                return null()
            memset(page, 0, 120)
            pcc_gc_backend4_zpage_reset(page, null(), size)
            store_i32(page, 28, generation)
            span = load_ptr(page, 72)
            if (
                ptr_is_null(span) != 0
                or load_i64(page, 80) < load_i64(page, 16)
            ):
                free(span)
                free(page)
                return null()
            prepared_page = page
            prepared_from_free = 0


@c_abi_export("pcc_gc_backend4_zpage_track_alloc")
def pcc_gc_backend4_zpage_track_alloc(owner, size: i64):
    if ptr_is_null(owner) != 0 or is_tagged_int(owner) != 0:
        return null()
    node = pcc_gc_backend4_zpage_node_alloc()
    if ptr_is_null(node) != 0:
        return null()
    page = null()
    existing_offset: i64 = -1
    if (load_i32(owner, 12) & 65536) != 0:
        page = pcc_gc_backend4_zpage_find_page_for_addr(owner, size)
        if ptr_is_null(page) == 0:
            span = load_ptr(page, 72)
            existing_offset = ptr_diff(owner, span)
    if ptr_is_null(page) != 0:
        page = pcc_gc_backend4_zpage_find_reusable_page(owner, size)
    if ptr_is_null(page) != 0:
        page = pcc_gc_backend4_zpage_pop_free_page(size)
    if ptr_is_null(page) != 0:
        page = malloc(376)
        if ptr_is_null(page) != 0:
            pcc_gc_backend4_zpage_node_release(node)
            return null()
        memset(page, 0, 120)
    if existing_offset < 0 and load_i64(page, 32) <= 0:
        pcc_gc_backend4_zpage_reset(page, owner, size)
        store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_page_head"))
        global_store_ptr("pcc_gc_backend4_page_head", page)
    store_ptr(node, 0, owner)
    store_ptr(node, 8, page)
    allocated: i64 = load_i64(page, 64)
    if existing_offset >= 0:
        pending: i64 = load_i64(page, 88)
        if pending > 0:
            store_i64(page, 88, pending - 1)
        store_i64(node, 24, existing_offset)
    else:
        store_i64(node, 24, allocated)
    store_i64(node, 32, size)
    store_ptr(node, 64, null())
    store_i64(node, 72, 0)
    if existing_offset < 0:
        store_i64(page, 64, allocated + ((size + 7) & -8))
    store_i64(page, 8, load_i64(page, 8) + size)
    store_i64(page, 32, load_i64(page, 32) + 1)
    pcc_gc_backend4_zpage_set_active_page(page)
    pcc_gc_backend4_zpage_link_node(node)
    return node


@c_abi_export("pcc_gc_backend4_zpage_track_page_prepare")
def pcc_gc_backend4_zpage_track_page_prepare(page, owner, size: i64):
    if size <= 0:
        return null()
    if ptr_is_null(page) != 0:
        page = malloc(376)
        if ptr_is_null(page) != 0:
            return null()
        memset(page, 0, 120)
    pcc_gc_backend4_zpage_reset(page, owner, size)
    span = load_ptr(page, 72)
    if ptr_is_null(span) != 0 or load_i64(page, 80) < load_i64(page, 16):
        free(span)
        free(page)
        return null()
    return page


@c_abi_export("pcc_gc_backend4_zpage_track_alloc_preallocated")
def pcc_gc_backend4_zpage_track_alloc_preallocated(
    owner,
    size: i64,
    prepared_node_io,
    prepared_page_io,
    prepared_page_from_free: i64,
):
    if (
        ptr_is_null(owner) != 0
        or is_tagged_int(owner) != 0
        or ptr_is_null(prepared_node_io) != 0
        or ptr_is_null(prepared_page_io) != 0
    ):
        return null()
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4:
        return null()
    page = null()
    existing_offset: i64 = -1
    if (load_i32(owner, 12) & 65536) != 0:
        page = pcc_gc_backend4_zpage_find_page_for_addr(owner, size)
        if ptr_is_null(page) == 0:
            existing_offset = ptr_diff(owner, load_ptr(page, 72))
    if ptr_is_null(page) != 0:
        page = pcc_gc_backend4_zpage_find_reusable_page(owner, size)
    prepared_page = load_ptr(prepared_page_io, 0)
    uses_prepared_page: i64 = 0
    if ptr_is_null(page) != 0:
        if ptr_is_null(prepared_page) != 0:
            return null()
        page = prepared_page
        uses_prepared_page = 1
    elif ptr_is_null(prepared_page) == 0 and prepared_page_from_free != 0:
        store_ptr(
            prepared_page,
            56,
            global_load_ptr("pcc_gc_backend4_free_page_head"),
        )
        global_store_ptr("pcc_gc_backend4_free_page_head", prepared_page)
        store_ptr(prepared_page_io, 0, null())

    node = pcc_gc_backend4_zpage_node_take_prepared(prepared_node_io)
    if ptr_is_null(node) != 0:
        return null()
    store_ptr(node, 0, owner)
    store_ptr(node, 8, page)
    allocated: i64 = load_i64(page, 64)
    if existing_offset >= 0:
        store_i64(node, 24, existing_offset)
    else:
        store_i64(node, 24, allocated)
    store_i64(node, 32, size)
    store_ptr(node, 64, null())
    store_i64(node, 72, 0)
    if pcc_gc_backend4_zpage_link_node_preallocated(node) < 0:
        if ptr_is_null(load_ptr(prepared_node_io, 0)) != 0:
            store_ptr(prepared_node_io, 0, node)
        else:
            pcc_gc_backend4_zpage_node_release(node)
        return null()
    if uses_prepared_page != 0:
        store_ptr(prepared_page_io, 0, null())
        store_ptr(page, 56, global_load_ptr("pcc_gc_backend4_page_head"))
        global_store_ptr("pcc_gc_backend4_page_head", page)
    if existing_offset >= 0:
        pending: i64 = load_i64(page, 88)
        if pending > 0:
            store_i64(page, 88, pending - 1)
    else:
        store_i64(page, 64, allocated + ((size + 7) & -8))
    store_i64(page, 8, load_i64(page, 8) + size)
    store_i64(page, 32, load_i64(page, 32) + 1)
    pcc_gc_backend4_zpage_set_active_page(page)
    return node
