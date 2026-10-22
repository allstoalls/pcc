"""Raw backend-0 cycle-collector actions over the shared slot contract."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    free,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
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


pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
py_gc_index_find = extern("py_gc_index_find", (c_ptr,), c_ptr)


@c_abi_export("pcc_gc_backend0_is_unreachable")
def pcc_gc_backend0_is_unreachable(o) -> i64:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    node = py_gc_index_find(o)
    if ptr_is_null(node) != 0:
        return 0
    if load_i32(node, 16) == 0:
        return 1
    return 0


@c_abi_export("pcc_gc_backend0_subtract_slot")
def pcc_gc_backend0_subtract_slot(slot, role: i64, context) -> None:
    if role == 3:  # borrowed update-only metadata is not a graph edge
        return
    child = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(child) != 0 or is_tagged_int(child) != 0:
        return
    node = py_gc_index_find(child)
    if ptr_is_null(node) == 0:
        store_i64(node, 8, load_i64(node, 8) - 1)


@c_abi_export("pcc_gc_backend0_visit_subtract")
def pcc_gc_backend0_visit_subtract(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    pcc_gc_visit_object_slots(o, pcc_gc_backend0_subtract_slot, null())


# Mark worklist in pcc_gc_backend0_mark_reachable's frame: marked objects
# whose slots are still to scan. It starts in the frame and moves to the heap
# once it fills.
_MARK_ITEMS = 0
_MARK_COUNT = 8
_MARK_CAPACITY = 16
_MARK_INLINE = 24
_MARK_HEADER_BYTES = 32
_MARK_INLINE_CAPACITY = 64


@c_abi_export("pcc_gc_backend0_mark_reachable")
def pcc_gc_backend0_mark_reachable(o) -> None:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return
    node = py_gc_index_find(o)
    if ptr_is_null(node) != 0 or load_i32(node, 16) != 0:
        return
    store_i32(node, 16, 1)
    # Iterative, as CPython's collector: one recursion per reference level
    # overflowed the C stack on a 50,000-deep linked list.
    worklist = stack_alloc(_MARK_HEADER_BYTES)
    inline = stack_alloc(_MARK_INLINE_CAPACITY * 8)
    store_ptr(worklist, _MARK_ITEMS, inline)
    store_i64(worklist, _MARK_COUNT, 0)
    store_i64(worklist, _MARK_CAPACITY, _MARK_INLINE_CAPACITY)
    store_ptr(worklist, _MARK_INLINE, inline)
    pcc_gc_visit_object_slots(o, pcc_gc_backend0_mark_slot, worklist)
    count: i64 = load_i64(worklist, _MARK_COUNT)
    while count > 0:
        count = count - 1
        store_i64(worklist, _MARK_COUNT, count)
        item = load_ptr(load_ptr(worklist, _MARK_ITEMS), count * 8)
        pcc_gc_visit_object_slots(item, pcc_gc_backend0_mark_slot, worklist)
        count = load_i64(worklist, _MARK_COUNT)
    items = load_ptr(worklist, _MARK_ITEMS)
    if ptr_eq(items, inline) == 0:
        free(items)


@c_abi_export("pcc_gc_backend0_mark_slot")
def pcc_gc_backend0_mark_slot(slot, role: i64, context) -> None:
    if role == 3:
        return
    child = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(context) != 0:
        pcc_gc_backend0_mark_reachable(child)
        return
    if ptr_is_null(child) != 0 or is_tagged_int(child) != 0:
        return
    node = py_gc_index_find(child)
    if ptr_is_null(node) != 0 or load_i32(node, 16) != 0:
        return
    store_i32(node, 16, 1)
    count: i64 = load_i64(context, _MARK_COUNT)
    capacity: i64 = load_i64(context, _MARK_CAPACITY)
    if count == capacity:
        grown = malloc(capacity * 16)
        if ptr_is_null(grown) != 0:
            # No room to defer it: scan it now, as the recursive walk did.
            pcc_gc_visit_object_slots(child, pcc_gc_backend0_mark_slot, context)
            return
        items = load_ptr(context, _MARK_ITEMS)
        memmove(grown, items, capacity * 8)
        if ptr_eq(items, load_ptr(context, _MARK_INLINE)) == 0:
            free(items)
        store_ptr(context, _MARK_ITEMS, grown)
        store_i64(context, _MARK_CAPACITY, capacity * 2)
    store_ptr(load_ptr(context, _MARK_ITEMS), count * 8, child)
    store_i64(context, _MARK_COUNT, count + 1)
