"""Shared freestanding forwarding and stable-identity substrate.

Backend 3 copy-oldification and Backend 4 colored relocation share this
pointer-only state machine.  Page selection, object copying, remap, and page
retirement deliberately live in their backend policy modules.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    calloc,
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
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_object_is_address_pinned = extern("pcc_gc_object_is_address_pinned", (c_ptr,), c_int64)

pcc_gc_index_slot_size = extern("pcc_gc_index_slot_size", (), c_int64)



py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_gc_generational_oldify_supported_tag = extern(
    "pcc_gc_generational_oldify_supported_tag", (c_int64,), c_int64
)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_zpage_owner_index_find = extern(
    "pcc_gc_zpage_owner_index_find", (c_ptr,), c_ptr
)
pcc_gc_forwarding_index_find = extern(
    "pcc_gc_forwarding_index_find", (c_ptr,), c_ptr
)
pcc_gc_forwarding_index_insert = extern(
    "pcc_gc_forwarding_index_insert", (c_ptr, c_ptr), c_int64
)
pcc_gc_forwarding_index_remove = extern(
    "pcc_gc_forwarding_index_remove", (c_ptr,), c_ptr
)
pcc_gc_forwarding_index_clear = extern(
    "pcc_gc_forwarding_index_clear", (), c_void
)
pcc_gc_forwarding_target_index_find = extern(
    "pcc_gc_forwarding_target_index_find", (c_ptr,), c_ptr
)
pcc_gc_forwarding_target_index_insert = extern(
    "pcc_gc_forwarding_target_index_insert", (c_ptr, c_ptr), c_int64
)
pcc_gc_forwarding_target_index_upsert = extern(
    "pcc_gc_forwarding_target_index_upsert", (c_ptr, c_ptr), c_int64
)
pcc_gc_forwarding_target_index_remove = extern(
    "pcc_gc_forwarding_target_index_remove", (c_ptr,), c_ptr
)
pcc_gc_forwarding_target_index_clear = extern(
    "pcc_gc_forwarding_target_index_clear", (), c_void
)
pcc_gc_identity_index_find = extern(
    "pcc_gc_identity_index_find", (c_ptr,), c_ptr
)
pcc_gc_identity_index_insert = extern(
    "pcc_gc_identity_index_insert", (c_ptr, c_ptr), c_int64
)
pcc_gc_identity_index_remove = extern(
    "pcc_gc_identity_index_remove", (c_ptr,), c_ptr
)
pcc_gc_identity_index_clear = extern("pcc_gc_identity_index_clear", (), c_void)
pcc_gc_forwarding_plan_index_capacity = extern(
    "pcc_gc_forwarding_plan_index_capacity", (c_int64, c_int64), c_int64
)
pcc_gc_forwarding_plan_index_commit = extern(
    "pcc_gc_forwarding_plan_index_commit",
    (c_int64, c_ptr, c_int64, c_int64), c_int64
)
pcc_gc_forwarding_plan_index_insert = extern(
    "pcc_gc_forwarding_plan_index_insert", (c_int64, c_ptr, c_ptr), c_int64
)


@c_abi_export("pcc_gc_forwarding_identity_graph_lock")
def _graph_lock() -> None:
    pcc_py_gc_minor_graph_lock()


@c_abi_export("pcc_gc_forwarding_identity_graph_unlock")
def _graph_unlock() -> None:
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_forwarding_set_head")
def _set_forwarding_head(head: c_ptr) -> None:
    global_store_ptr("pcc_gc_forwarding_head", head)


@c_abi_export("pcc_gc_forwarding_target_find")
def _forwarding_target_find(target: c_ptr) -> c_ptr:
    if ptr_is_null(target) != 0 or is_tagged_int(target) != 0:
        return null()
    return pcc_gc_forwarding_target_index_find(target)


@c_abi_export("pcc_gc_forwarding_target_prepare")
def _forwarding_target_prepare(target: c_ptr, node: c_ptr) -> c_ptr:
    if ptr_is_null(target) != 0 or is_tagged_int(target) != 0:
        return null()
    if ptr_is_null(node) != 0:
        return null()
    head = _forwarding_target_find(target)
    rc: i64 = 0
    if ptr_is_null(head) != 0:
        rc = pcc_gc_forwarding_target_index_insert(target, node)
    else:
        rc = pcc_gc_forwarding_target_index_upsert(target, node)
    if rc < 0:
        return null()
    if ptr_is_null(head) != 0:
        return node
    return head


@c_abi_export("pcc_gc_forwarding_target_attach_prepared")
def _forwarding_target_attach_prepared(node: c_ptr, prepared_head: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    old_head = prepared_head
    if ptr_eq(prepared_head, node) != 0:
        old_head = null()
    store_ptr(node, 32, old_head)
    store_ptr(node, 40, null())
    if ptr_is_null(old_head) == 0:
        store_ptr(old_head, 40, node)


@c_abi_export("pcc_gc_identity_find")
def _identity_find(obj: c_ptr) -> c_ptr:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return null()
    return pcc_gc_identity_index_find(obj)


@c_abi_export("pcc_gc_identity_set_head")
def _set_identity_head(head: c_ptr) -> None:
    global_store_ptr("pcc_gc_identity_head", head)


@c_abi_export("pcc_gc_identity_assign")
def _identity_assign(obj: c_ptr, stable_id: i64) -> i64:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0 or stable_id <= 0:
        return 0
    node = _identity_find(obj)
    if ptr_is_null(node) == 0:
        store_i64(node, 8, stable_id)
        return 1
    node = malloc(32)
    if ptr_is_null(node) != 0:
        return 0
    store_ptr(node, 0, obj)
    store_i64(node, 8, stable_id)
    old_head = global_load_ptr("pcc_gc_identity_head")
    store_ptr(node, 16, old_head)
    store_ptr(node, 24, null())
    if ptr_is_null(old_head) == 0:
        store_ptr(old_head, 24, node)
    _set_identity_head(node)
    if pcc_gc_identity_index_insert(obj, node) < 0:
        _set_identity_head(load_ptr(node, 16))
        nxt = load_ptr(node, 16)
        if ptr_is_null(nxt) == 0:
            store_ptr(nxt, 24, null())
        free(node)
        return 0
    return 1


@c_abi_export("pcc_gc_forwarding_zpage_node_for_owner")
def _zpage_node_for_owner(owner: c_ptr) -> c_ptr:
    obj_node = pcc_gc_object_index_find(owner)
    if ptr_is_null(obj_node) == 0 and load_i64(obj_node, 32) == 0:
        znode = load_ptr(obj_node, 48)
        if ptr_is_null(znode) == 0:
            return znode
    return pcc_gc_zpage_owner_index_find(owner)


@c_abi_export("pcc_gc_forwarding_list_head")
def pcc_gc_forwarding_head() -> c_ptr:
    return global_load_ptr("pcc_gc_forwarding_head")


@c_abi_export("pcc_gc_forwarding_find")
def pcc_gc_forwarding_find(from_obj: c_ptr) -> c_ptr:
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return null()
    return pcc_gc_forwarding_index_find(from_obj)


@c_abi_export("pcc_gc_forwarding_target_exists")
def pcc_gc_forwarding_target_exists(target: c_ptr) -> i64:
    if ptr_is_null(target) != 0 or is_tagged_int(target) != 0:
        return 0
    if ptr_is_null(_forwarding_target_find(target)) == 0:
        return 1
    return 0


@c_abi_export("pcc_gc_forwarding_target_unlink")
def pcc_gc_forwarding_target_unlink(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    target = load_ptr(node, 8)
    if ptr_is_null(target) != 0 or is_tagged_int(target) != 0:
        return
    prev = load_ptr(node, 40)
    nxt = load_ptr(node, 32)
    if ptr_is_null(prev) == 0:
        store_ptr(prev, 32, nxt)
    elif ptr_is_null(nxt) == 0:
        pcc_gc_forwarding_target_index_upsert(target, nxt)
    else:
        pcc_gc_forwarding_target_index_remove(target)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 40, prev)
    store_ptr(node, 32, null())
    store_ptr(node, 40, null())


@c_abi_export("pcc_gc_forwarding_unlink_main")
def pcc_gc_forwarding_unlink_main(node: c_ptr) -> None:
    if ptr_is_null(node) != 0:
        return
    prev = load_ptr(node, 24)
    nxt = load_ptr(node, 16)
    if ptr_is_null(prev) != 0:
        _set_forwarding_head(nxt)
    else:
        store_ptr(prev, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 24, prev)
    store_ptr(node, 16, null())
    store_ptr(node, 24, null())


@c_abi_export("pcc_gc_forwarding_clear_all")
def pcc_gc_forwarding_clear_all() -> None:
    node = pcc_gc_forwarding_head()
    _set_forwarding_head(null())
    pcc_gc_forwarding_index_clear()
    pcc_gc_forwarding_target_index_clear()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        py_decref(load_ptr(node, 8))
        free(node)
        node = nxt
    store_i32(global_addr("pcc_gc_forwarding_population"), 0, 0)


@c_abi_export("pcc_gc_identity_list_head")
def pcc_gc_identity_head() -> c_ptr:
    return global_load_ptr("pcc_gc_identity_head")


@c_abi_export("pcc_gc_identity_ensure")
def pcc_gc_identity_ensure(obj: c_ptr) -> c_ptr:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return null()
    node = _identity_find(obj)
    if ptr_is_null(node) == 0:
        return node
    node = malloc(32)
    if ptr_is_null(node) != 0:
        return null()
    stable_id: i64 = load_i32(global_addr("pcc_gc_next_object_id"), 0)
    if stable_id <= 0:
        stable_id: i64 = 1
    store_i32(global_addr("pcc_gc_next_object_id"), 0, stable_id + 1)
    store_ptr(node, 0, obj)
    store_i64(node, 8, stable_id)
    old_head = pcc_gc_identity_head()
    store_ptr(node, 16, old_head)
    store_ptr(node, 24, null())
    if ptr_is_null(old_head) == 0:
        store_ptr(old_head, 24, node)
    _set_identity_head(node)
    if pcc_gc_identity_index_insert(obj, node) < 0:
        _set_identity_head(load_ptr(node, 16))
        nxt = load_ptr(node, 16)
        if ptr_is_null(nxt) == 0:
            store_ptr(nxt, 24, null())
        free(node)
        return null()
    return node


@c_abi_export("pcc_gc_identity_detach")
def pcc_gc_identity_detach(obj: c_ptr) -> c_ptr:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return null()
    # Every indexed identity node is linked from the head, so an empty list
    # means an empty index: skip the probe every object free used to pay.
    if ptr_is_null(global_load_ptr("pcc_gc_identity_head")) != 0:
        return null()
    node = pcc_gc_identity_index_remove(obj)
    if ptr_is_null(node) != 0:
        return null()
    prev = load_ptr(node, 24)
    nxt = load_ptr(node, 16)
    if ptr_is_null(prev) != 0:
        _set_identity_head(nxt)
    else:
        store_ptr(prev, 16, nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 24, prev)
    store_ptr(node, 16, null())
    store_ptr(node, 24, null())
    return node


@c_abi_export("pcc_gc_identity_finish_detached")
def pcc_gc_identity_finish_detached(nodes: c_ptr) -> None:
    while ptr_is_null(nodes) == 0:
        node = nodes
        nodes = load_ptr(node, 16)
        store_ptr(node, 16, null())
        free(node)


@c_abi_export("pcc_gc_identity_remove")
def pcc_gc_identity_remove(obj: c_ptr) -> None:
    pcc_gc_identity_finish_detached(pcc_gc_identity_detach(obj))


@c_abi_export("pcc_gc_identity_clear_all")
def pcc_gc_identity_clear_all() -> None:
    node = pcc_gc_identity_head()
    _set_identity_head(null())
    pcc_gc_identity_index_clear()
    while ptr_is_null(node) == 0:
        nxt = load_ptr(node, 16)
        free(node)
        node = nxt


@c_abi_export("pcc_gc_backend4_slot_needs_resolve")
def pcc_gc_backend4_slot_needs_resolve(value: c_ptr) -> i64:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0
    if ptr_is_null(pcc_gc_forwarding_find(value)) == 0:
        return 1
    if pcc_gc_object_is_known_no_lock(value) != 0:
        if (load_i32(value, 12) & 2048) != 0:
            return 1
    return 0


@c_abi_export("pcc_gc_forwarding_install_plan_finish")
def pcc_gc_forwarding_install_plan_finish(plan: c_ptr) -> None:
    if ptr_is_null(plan) != 0:
        return
    free(load_ptr(plan, 0))
    free(load_ptr(plan, 8))
    free(load_ptr(plan, 16))
    free(load_ptr(plan, 24))
    free(load_ptr(plan, 40))
    free(load_ptr(plan, 56))
    free(plan)


@c_abi_export("pcc_gc_forwarding_install_plan_prepare")
def pcc_gc_forwarding_install_plan_prepare(
    from_obj: c_ptr, to_obj: c_ptr
) -> c_ptr:
    valid: i64 = 1
    identity_cap: i64 = -1
    forwarding_cap: i64 = -1
    target_cap: i64 = -1
    _graph_lock()
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if (
        (backend != 3 and backend != 4)
        or (
            backend == 4
            and load_i64(
                global_addr("pcc_gc_backend4_reseed_commit_owner"), 0
            ) != 0
        )
        or ptr_is_null(from_obj) != 0
        or ptr_is_null(to_obj) != 0
        or is_tagged_int(from_obj) != 0
        or is_tagged_int(to_obj) != 0
        or ptr_eq(from_obj, to_obj) != 0
        or pcc_gc_object_is_known_no_lock(from_obj) == 0
        or pcc_gc_object_is_known_no_lock(to_obj) == 0
        or pcc_gc_object_is_address_pinned(from_obj) != 0
        or ptr_is_null(pcc_gc_forwarding_find(from_obj)) == 0
        or ptr_is_null(_forwarding_target_find(to_obj)) == 0
    ):
        valid = 0
    else:
        identity_cap = pcc_gc_forwarding_plan_index_capacity(2, 2)
        forwarding_cap = pcc_gc_forwarding_plan_index_capacity(0, 1)
        target_cap = pcc_gc_forwarding_plan_index_capacity(1, 1)
        if identity_cap < 0 or forwarding_cap < 0 or target_cap < 0:
            valid = 0
    _graph_unlock()
    if valid == 0:
        return null()

    plan = calloc(1, 72)
    if ptr_is_null(plan) != 0:
        return null()
    store_ptr(plan, 0, calloc(1, 32))
    store_ptr(plan, 8, calloc(1, 32))
    store_ptr(plan, 16, calloc(1, 56))
    store_i64(plan, 32, identity_cap)
    store_i64(plan, 48, forwarding_cap)
    store_i64(plan, 64, target_cap)
    if identity_cap > 0:
        store_ptr(plan, 24, calloc(identity_cap, pcc_gc_index_slot_size()))
    if forwarding_cap > 0:
        store_ptr(plan, 40, calloc(forwarding_cap, pcc_gc_index_slot_size()))
    if target_cap > 0:
        store_ptr(plan, 56, calloc(target_cap, pcc_gc_index_slot_size()))
    if (
        ptr_is_null(load_ptr(plan, 0)) != 0
        or ptr_is_null(load_ptr(plan, 8)) != 0
        or ptr_is_null(load_ptr(plan, 16)) != 0
        or (identity_cap > 0 and ptr_is_null(load_ptr(plan, 24)) != 0)
        or (forwarding_cap > 0 and ptr_is_null(load_ptr(plan, 40)) != 0)
        or (target_cap > 0 and ptr_is_null(load_ptr(plan, 56)) != 0)
    ):
        pcc_gc_forwarding_install_plan_finish(plan)
        return null()
    return plan


@c_abi_export("pcc_gc_install_forwarding_preallocated_unlocked")
def pcc_gc_install_forwarding_preallocated_unlocked(
    from_obj: c_ptr, to_obj: c_ptr, plan: c_ptr
) -> i64:
    if (
        load_i32(global_addr("pcc_gc_backend_selected"), 0) == 4
        and (
            load_i64(
                global_addr("pcc_gc_backend4_relocation_reset_owner"), 0
            ) != 0
            or load_i64(
                global_addr("pcc_gc_backend4_reseed_commit_owner"), 0
            ) != 0
        )
    ):
        return -1
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend != 3 and backend != 4:
        return -1
    if (
        ptr_is_null(from_obj) != 0
        or ptr_is_null(to_obj) != 0
        or ptr_is_null(plan) != 0
        or is_tagged_int(from_obj) != 0
        or is_tagged_int(to_obj) != 0
        or ptr_eq(from_obj, to_obj) != 0
        or pcc_gc_object_is_known_no_lock(from_obj) == 0
        or pcc_gc_object_is_known_no_lock(to_obj) == 0
    ):
        return -1
    flags: i64 = load_i32(from_obj, 12)
    if pcc_gc_object_is_address_pinned(from_obj) != 0:
        rejects: i64 = load_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0)
        store_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0, rejects + 1)
        return -2
    if (
        ptr_is_null(pcc_gc_forwarding_find(from_obj)) == 0
        or ptr_is_null(_forwarding_target_find(to_obj)) == 0
    ):
        return -1
    if (
        pcc_gc_forwarding_plan_index_commit(
            2, ptr_add(plan, 24), load_i64(plan, 32), 2
        )
        < 0
        or pcc_gc_forwarding_plan_index_commit(
            0, ptr_add(plan, 40), load_i64(plan, 48), 1
        )
        < 0
        or pcc_gc_forwarding_plan_index_commit(
            1, ptr_add(plan, 56), load_i64(plan, 64), 1
        )
        < 0
    ):
        return -1

    from_identity = _identity_find(from_obj)
    if ptr_is_null(from_identity) != 0:
        from_identity = load_ptr(plan, 0)
        if ptr_is_null(from_identity) != 0:
            return -1
        stable_id: i64 = load_i32(global_addr("pcc_gc_next_object_id"), 0)
        if stable_id <= 0:
            stable_id = 1
        store_ptr(from_identity, 0, from_obj)
        store_i64(from_identity, 8, stable_id)
        old_identity_head = pcc_gc_identity_head()
        store_ptr(from_identity, 16, old_identity_head)
        store_ptr(from_identity, 24, null())
        if (
            pcc_gc_forwarding_plan_index_insert(
                2, from_obj, from_identity
            )
            != 1
        ):
            return -1
        store_i32(global_addr("pcc_gc_next_object_id"), 0, stable_id + 1)
        if ptr_is_null(old_identity_head) == 0:
            store_ptr(old_identity_head, 24, from_identity)
        _set_identity_head(from_identity)
        store_ptr(plan, 0, null())

    to_identity = _identity_find(to_obj)
    if ptr_is_null(to_identity) != 0:
        to_identity = load_ptr(plan, 8)
        if ptr_is_null(to_identity) != 0:
            return -1
        store_ptr(to_identity, 0, to_obj)
        store_i64(to_identity, 8, load_i64(from_identity, 8))
        old_identity_head = pcc_gc_identity_head()
        store_ptr(to_identity, 16, old_identity_head)
        store_ptr(to_identity, 24, null())
        if pcc_gc_forwarding_plan_index_insert(2, to_obj, to_identity) != 1:
            return -1
        if ptr_is_null(old_identity_head) == 0:
            store_ptr(old_identity_head, 24, to_identity)
        _set_identity_head(to_identity)
        store_ptr(plan, 8, null())
    else:
        store_i64(to_identity, 8, load_i64(from_identity, 8))

    node = load_ptr(plan, 16)
    if ptr_is_null(node) != 0:
        return -1
    store_ptr(node, 0, from_obj)
    store_ptr(node, 8, to_obj)
    old_head = pcc_gc_forwarding_head()
    store_ptr(node, 16, old_head)
    store_ptr(node, 24, null())
    store_ptr(node, 32, null())
    store_ptr(node, 40, null())
    store_ptr(node, 48, null())
    if pcc_gc_forwarding_plan_index_insert(0, from_obj, node) != 1:
        return -1
    if pcc_gc_forwarding_plan_index_insert(1, to_obj, node) != 1:
        pcc_gc_forwarding_index_remove(from_obj)
        return -1

    py_incref(to_obj)
    if ptr_is_null(old_head) == 0:
        store_ptr(old_head, 24, node)
    _set_forwarding_head(node)
    _forwarding_target_attach_prepared(node, node)
    store_ptr(plan, 16, null())
    population: i64 = load_i32(global_addr("pcc_gc_forwarding_population"), 0)
    store_i32(global_addr("pcc_gc_forwarding_population"), 0, population + 1)
    if backend == 4 and (flags & 65536) != 0:
        znode = _zpage_node_for_owner(from_obj)
        if ptr_is_null(znode) == 0:
            zpage = load_ptr(znode, 8)
            if ptr_is_null(zpage) == 0:
                store_i64(zpage, 96, load_i64(zpage, 96) + 1)
                store_ptr(node, 48, zpage)
    store_i32(from_obj, 12, flags | 2048)
    store_i32(to_obj, 12, load_i32(to_obj, 12) | 8192)
    forwards: i64 = load_i32(global_addr("pcc_gc_relocation_forwards"), 0)
    store_i32(global_addr("pcc_gc_relocation_forwards"), 0, forwards + 1)
    return 0


@c_abi_export("pcc_gc_install_forwarding_unlocked")
def pcc_gc_install_forwarding_unlocked(from_obj: c_ptr, to_obj: c_ptr) -> i64:
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if backend == 4 and load_i64(
        global_addr("pcc_gc_backend4_relocation_reset_owner"), 0
    ) != 0:
        return -1
    if backend == 4 and load_i64(
        global_addr("pcc_gc_backend4_reseed_commit_owner"), 0
    ) != 0:
        return -1
    if backend != 3 and backend != 4:
        return -1
    if ptr_is_null(from_obj) != 0 or ptr_is_null(to_obj) != 0:
        return -1
    if is_tagged_int(from_obj) != 0 or is_tagged_int(to_obj) != 0:
        return -1
    if ptr_eq(from_obj, to_obj) != 0:
        return -1
    if pcc_gc_object_is_known_no_lock(from_obj) == 0:
        return -1
    if pcc_gc_object_is_known_no_lock(to_obj) == 0:
        return -1
    flags: i64 = load_i32(from_obj, 12)
    if pcc_gc_object_is_address_pinned(from_obj) != 0:
        rejects: i64 = load_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0)
        store_i32(global_addr("pcc_gc_relocation_pin_rejects"), 0, rejects + 1)
        return -2
    from_identity = pcc_gc_identity_ensure(from_obj)
    if ptr_is_null(from_identity) != 0:
        return -1
    if _identity_assign(to_obj, load_i64(from_identity, 8)) == 0:
        return -1
    node = pcc_gc_forwarding_find(from_obj)
    if ptr_is_null(node) == 0:
        old_target = load_ptr(node, 8)
        if ptr_eq(old_target, to_obj) == 0:
            target_head = _forwarding_target_prepare(to_obj, node)
            if ptr_is_null(target_head) != 0:
                return -1
            py_incref(to_obj)
            pcc_gc_forwarding_target_unlink(node)
            store_ptr(node, 8, to_obj)
            _forwarding_target_attach_prepared(node, target_head)
            py_decref(old_target)
    else:
        node = malloc(56)
        if ptr_is_null(node) != 0:
            return -1
        py_incref(to_obj)
        store_ptr(node, 0, from_obj)
        store_ptr(node, 8, to_obj)
        store_ptr(node, 48, null())
        old_head = pcc_gc_forwarding_head()
        store_ptr(node, 16, old_head)
        store_ptr(node, 24, null())
        store_ptr(node, 32, null())
        store_ptr(node, 40, null())
        if ptr_is_null(old_head) == 0:
            store_ptr(old_head, 24, node)
        _set_forwarding_head(node)
        if pcc_gc_forwarding_index_insert(from_obj, node) < 0:
            _set_forwarding_head(load_ptr(node, 16))
            nxt = load_ptr(node, 16)
            if ptr_is_null(nxt) == 0:
                store_ptr(nxt, 24, null())
            py_decref(to_obj)
            free(node)
            return -1
        target_head = _forwarding_target_prepare(to_obj, node)
        if ptr_is_null(target_head) != 0:
            pcc_gc_forwarding_index_remove(from_obj)
            pcc_gc_forwarding_unlink_main(node)
            py_decref(to_obj)
            free(node)
            return -1
        _forwarding_target_attach_prepared(node, target_head)
        population: i64 = load_i32(global_addr("pcc_gc_forwarding_population"), 0)
        store_i32(global_addr("pcc_gc_forwarding_population"), 0, population + 1)
        if backend == 4 and (flags & 65536) != 0:
            znode = _zpage_node_for_owner(from_obj)
            if ptr_is_null(znode) == 0:
                zpage = load_ptr(znode, 8)
                if ptr_is_null(zpage) == 0:
                    store_i64(zpage, 96, load_i64(zpage, 96) + 1)
                    store_ptr(node, 48, zpage)
    store_i32(from_obj, 12, flags | 2048)
    store_i32(to_obj, 12, load_i32(to_obj, 12) | 8192)
    forwards: i64 = load_i32(global_addr("pcc_gc_relocation_forwards"), 0)
    store_i32(global_addr("pcc_gc_relocation_forwards"), 0, forwards + 1)
    return 0


@c_abi_export("pcc_gc_install_forwarding")
def pcc_gc_install_forwarding(from_obj: c_ptr, to_obj: c_ptr) -> i64:
    pcc_gc_config_ensure()
    _graph_lock()
    reject: i64 = 0
    backend: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if (
        ptr_is_null(from_obj) == 0
        and ptr_is_null(to_obj) == 0
        and is_tagged_int(from_obj) == 0
        and is_tagged_int(to_obj) == 0
        and pcc_gc_object_is_known_no_lock(from_obj) != 0
        and pcc_gc_object_is_known_no_lock(to_obj) != 0
    ):
        from_tag: i64 = load_i32(from_obj, 8)
        to_tag: i64 = load_i32(to_obj, 8)
        if backend == 3:
            if (
                from_tag != to_tag
                or pcc_gc_generational_oldify_supported_tag(from_tag) == 0
            ):
                reject = 1
        elif backend == 4:
            # These source kinds require relocation-copy commit adjuncts that
            # the historical raw public edge installer cannot provide:
            # pointer-keyed class attrs, weakref-list replacement,
            # continuation-root retargeting, or owned Py_buffer transfer.
            if (
                from_tag == abi_constant("object.type.class")
                or from_tag == abi_constant("object.type.weakref")
                or from_tag == abi_constant("object.type.continuation")
                or from_tag == abi_constant("object.type.memoryview")
                or from_tag == abi_constant("object.type.cpy_handle")
                or from_tag == abi_constant("object.type.thread")
                or from_tag == abi_constant("object.type.virtual_thread")
                or from_tag == abi_constant("object.type.vthread_channel")
            ):
                reject = 1
    rc: i64 = -3
    if reject == 0:
        rc = pcc_gc_install_forwarding_unlocked(from_obj, to_obj)
    _graph_unlock()
    return rc


@c_abi_export("pcc_gc_note_relocation_read_unlocked")
def _note_relocation_read_unlocked(obj: c_ptr) -> c_ptr:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return obj
    if pcc_gc_object_is_known_no_lock(obj) == 0:
        unknown_node = pcc_gc_forwarding_find(obj)
        if ptr_is_null(unknown_node) == 0:
            unknown_target = load_ptr(unknown_node, 8)
            if ptr_is_null(unknown_target) == 0:
                count: i64 = load_i32(
                    global_addr("pcc_gc_relocation_barrier_forwards"), 0
                )
                store_i32(
                    global_addr("pcc_gc_relocation_barrier_forwards"),
                    0,
                    count + 1,
                )
                return unknown_target
        return obj
    flags: i64 = load_i32(obj, 12)
    node = pcc_gc_forwarding_find(obj)
    if ptr_is_null(node) == 0:
        target = load_ptr(node, 8)
        if ptr_is_null(target) == 0:
            count2: i64 = load_i32(
                global_addr("pcc_gc_relocation_barrier_forwards"), 0
            )
            store_i32(
                global_addr("pcc_gc_relocation_barrier_forwards"), 0, count2 + 1
            )
            return target
    if (flags & 2048) != 0:
        store_i32(obj, 12, flags & ~2048)
    return obj


@c_abi_export("pcc_gc_note_relocation_read")
def pcc_gc_note_relocation_read(obj: c_ptr) -> c_ptr:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return obj
    # Non-moving backends cannot have anything to resolve, so the whole chain
    # below is the identity for them.  Two existing invariants make that
    # exact, not optimistic: pcc_gc_install_forwarding_unlocked refuses
    # outright unless the selected backend is 3 or 4, and pcc_gc_set_backend
    # refuses to leave 3/4 while a forwarding node or a nonzero forwarding
    # population still exists.  So under 0/1/2 pcc_gc_forwarding_find cannot
    # match and the old outcome was always `obj`.  The only work skipped is
    # clearing a stale RELOCATION_CANDIDATE hint, and no backend outside 3/4
    # reads that flag.  Without this gate the barrier paid a full provenance
    # lookup on every pointer read -- plus the graph lock whenever the pointer
    # was not a known object -- to answer a question the backend had already
    # answered.  pcc_gc_load_ptr has gated on the same global for the same
    # reason; this is the missing half of that pair.
    # A pre-config read of 0 is also safe: an install must itself observe 3/4
    # in this same global, so nothing can be forwarded before config runs.
    selected: i64 = load_i32(global_addr("pcc_gc_backend_selected"), 0)
    if selected != 3 and selected != 4:
        return obj
    if pcc_gc_object_is_known_no_lock(obj) != 0:
        if (load_i32(obj, 12) & 2048) == 0:
            return obj
    _graph_lock()
    resolved = _note_relocation_read_unlocked(obj)
    _graph_unlock()
    return resolved


@c_abi_export("pcc_gc_object_id")
def pcc_gc_object_id(obj: c_ptr) -> i64:
    pcc_gc_config_ensure()
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    _graph_lock()
    node = pcc_gc_identity_ensure(obj)
    if ptr_is_null(node) != 0:
        _graph_unlock()
        return 0
    stable_id: i64 = load_i64(node, 8)
    _graph_unlock()
    return stable_id


@c_abi_export("pcc_gc_backend4_forwarding_entries")
def pcc_gc_backend4_forwarding_entries() -> i64:
    _graph_lock()
    node = pcc_gc_forwarding_head()
    count: i64 = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 16)
    _graph_unlock()
    return count


@c_abi_export("pcc_gc_backend4_stable_id_entries")
def pcc_gc_backend4_stable_id_entries() -> i64:
    _graph_lock()
    node = pcc_gc_identity_head()
    count: i64 = 0
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 16)
    _graph_unlock()
    return count
