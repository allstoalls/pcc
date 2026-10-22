"""Backend 3 scalar copy-oldification orchestration."""
from pcc import i64
from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_COMPLEX, PY_TYPE_CPY_HANDLE, PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_STR

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    free,
    global_addr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memmove,
    null,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_object_is_address_pinned = extern("pcc_gc_object_is_address_pinned", (c_ptr,), c_int64)


py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_cpy_handle_move_owned_ref = extern(
    "pcc_cpy_handle_move_owned_ref", (c_ptr, c_ptr), c_void
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_object_index_insert = extern(
    "pcc_gc_object_index_insert", (c_ptr, c_ptr), c_int64
)
pcc_gc_object_index_remove = extern("pcc_gc_object_index_remove", (c_ptr,), c_ptr)
pcc_gc_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
pcc_gc_install_forwarding_unlocked = extern(
    "pcc_gc_install_forwarding_unlocked", (c_ptr, c_ptr), c_int64
)
pcc_gc_identity_remove = extern("pcc_gc_identity_remove", (c_ptr,), c_void)
pcc_gc_relocate_copy_payload = extern(
    "pcc_gc_relocate_copy_payload", (c_ptr, c_ptr, c_int64, c_int64), c_int64
)
pcc_gc_object_known_size = extern("pcc_gc_object_known_size", (c_ptr,), c_int64)
pcc_gc_object_node_alloc = extern("pcc_gc_object_node_alloc", (), c_ptr)
pcc_gc_object_node_release = extern(
    "pcc_gc_object_node_release", (c_ptr,), c_void
)
pcc_gc_object_node_unlink = extern("pcc_gc_object_node_unlink", (c_ptr,), c_void)
pcc_gc_object_list_head = extern("pcc_gc_object_list_head", (), c_ptr)
pcc_gc_object_set_list_head = extern(
    "pcc_gc_object_set_list_head", (c_ptr,), c_void
)
pcc_gc_object_node_set_prev = extern(
    "pcc_gc_object_node_set_prev", (c_ptr, c_ptr), c_void
)
pcc_gc_object_node_freeing = extern(
    "pcc_gc_object_node_freeing", (c_ptr,), c_int64
)
pcc_gc_object_node_set_freeing = extern(
    "pcc_gc_object_node_set_freeing", (c_ptr, c_int64), c_void
)
pcc_gc_object_node_size = extern("pcc_gc_object_node_size", (c_ptr,), c_int64)
pcc_gc_object_node_set_gc_refs = extern(
    "pcc_gc_object_node_set_gc_refs", (c_ptr, c_int64), c_void
)
pcc_gc_object_node_set_young_next = extern(
    "pcc_gc_object_node_set_young_next", (c_ptr, c_ptr), c_void
)
pcc_gc_object_node_set_young_prev = extern(
    "pcc_gc_object_node_set_young_prev", (c_ptr, c_ptr), c_void
)
pcc_gc_backend3_young_unlink = extern(
    "pcc_gc_backend3_young_unlink", (c_ptr,), c_void
)
pcc_gc_live_bytes_subtract = extern(
    "pcc_gc_live_bytes_subtract", (c_int64,), c_void
)


@c_abi_export("pcc_gc_generational_oldify_supported_tag")
def pcc_gc_generational_oldify_supported_tag(tag: i64) -> i64:
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        return 1
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return 1
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        return 1
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        return 1
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        return 1
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        return 1
    if tag == PY_TYPE_CPY_HANDLE:  # PY_TYPE_CPY_HANDLE
        return 1
    return 0


@c_abi_export("pcc_gc_generational_mark_forwarded_source_inactive")
def pcc_gc_generational_mark_forwarded_source_inactive(from_obj: c_ptr) -> None:
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return
    node = pcc_gc_object_index_find(from_obj)
    if ptr_is_null(node) != 0:
        return
    if pcc_gc_object_node_freeing(node) != 0:
        return
    pcc_gc_live_bytes_subtract(pcc_gc_object_node_size(node))
    pcc_gc_object_node_set_freeing(node, 1)


@c_abi_export("pcc_gc_generational_oldify_copy")
def pcc_gc_generational_oldify_copy(from_obj: c_ptr) -> c_ptr:
    if load_i32(global_addr("pcc_gc_backend_selected"), 0) != 3:
        return null()
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return null()
    if pcc_gc_object_is_known_no_lock(from_obj) == 0:
        unknown = pcc_gc_forwarding_find(from_obj)
        if ptr_is_null(unknown) == 0:
            return load_ptr(unknown, 8)
        return null()

    flags: i64 = load_i32(from_obj, 12)
    existing = pcc_gc_forwarding_find(from_obj)
    if ptr_is_null(existing) == 0:
        target = load_ptr(existing, 8)
        if ptr_is_null(target) == 0:
            return target
    if (flags & 128) == 0 or pcc_gc_object_is_address_pinned(from_obj) != 0:
        return null()

    tag: i64 = load_i32(from_obj, 8)
    if pcc_gc_generational_oldify_supported_tag(tag) == 0:
        return null()
    size: i64 = pcc_gc_object_known_size(from_obj)
    if size < 16:
        return null()

    to_obj = malloc(size)
    if ptr_is_null(to_obj) != 0:
        return null()
    memmove(to_obj, from_obj, size)
    store_i64(to_obj, 0, 1)
    new_flags: i64 = load_i32(to_obj, 12)
    # Same stale-verdict rule as the C oracle: the header copy inherits
    # SWEEP_CANDIDATE (1024) from a finished cycle that predates
    # promotion, and a pending-candidate sweep would run PASS-0 __del__
    # on the live promoted copy. Promotion proves liveness.
    store_i32(
        to_obj,
        12,
        (
            new_flags
            & ~(128 | 4096 | 512 | 2048 | 262144 | 1024)
        )
        | 256
        | 262144,
    )
    if pcc_gc_relocate_copy_payload(from_obj, to_obj, tag, size) == 0:
        py_decref(to_obj)
        return null()
    store_i64(to_obj, 0, 0)

    node = pcc_gc_object_node_alloc()
    if ptr_is_null(node) != 0:
        free(to_obj)
        return null()
    old_head = pcc_gc_object_list_head()
    store_ptr(node, 0, to_obj)
    store_i64(node, 8, size)
    store_ptr(node, 16, old_head)
    store_ptr(node, 24, null())
    store_i64(node, 32, 0)
    store_ptr(node, 40, null())
    store_ptr(node, 48, null())
    pcc_gc_object_node_set_gc_refs(node, 0)
    pcc_gc_object_node_set_young_next(node, null())
    pcc_gc_object_node_set_young_prev(node, null())
    if ptr_is_null(old_head) == 0:
        pcc_gc_object_node_set_prev(old_head, node)
    pcc_gc_object_set_list_head(node)
    if pcc_gc_object_index_insert(to_obj, node) < 0:
        pcc_gc_object_node_unlink(node)
        pcc_gc_object_node_release(node)
        free(to_obj)
        return null()
    live: i64 = load_i64(global_addr("pcc_gc_live_bytes"), 0)
    store_i64(global_addr("pcc_gc_live_bytes"), 0, live + size)

    moved_cpy_ref: i64 = 0
    if tag == PY_TYPE_CPY_HANDLE:
        moved_cpy_ref = 1
    if moved_cpy_ref != 0:
        pcc_cpy_handle_move_owned_ref(from_obj, to_obj)
    if pcc_gc_install_forwarding_unlocked(from_obj, to_obj) != 0:
        if moved_cpy_ref != 0:
            pcc_cpy_handle_move_owned_ref(to_obj, from_obj)
        pcc_gc_object_index_remove(to_obj)
        pcc_gc_object_node_unlink(node)
        pcc_gc_object_node_release(node)
        pcc_gc_live_bytes_subtract(size)
        pcc_gc_identity_remove(to_obj)
        free(to_obj)
        return null()

    pcc_gc_backend3_young_unlink(pcc_gc_object_index_find(from_obj))
    pcc_gc_generational_mark_forwarded_source_inactive(from_obj)
    store_i32(from_obj, 12, (load_i32(from_obj, 12) & ~128) | 256)
    return to_obj
