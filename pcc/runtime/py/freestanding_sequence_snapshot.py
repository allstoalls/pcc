"""Prepared list-to-tuple snapshot commit; no allocation or finish while locked."""

__pcc_freestanding__ = True

from pcc import (
    i64,
)
from pcc.extern import (
    c_abi_export,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    abi_constant,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    ptr_add,
    ptr_is_null,
    store_i64,
    store_ptr,
    null,
)

pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_root_copy_lease_prepare_locked = extern(
    "pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64
)

_SEQUENCE_SNAPSHOT_PLAN_BYTES: i64 = 256


@c_abi_export("pcc_list_snapshot_commit_slots")
def pcc_list_snapshot_commit_slots(
    source_slot: c_ptr, children: c_ptr, length: i64,
    plans: c_ptr, tokens: c_ptr,
) -> i64:
    """Copy a consistent list into distinct registered empty owning roots.

    The source list and its payload are leased. children names a registered
    owning root array of length slots; plan/token storage is zero initialized.
    An extra registered borrowed slot follows that array. It temporarily
    describes one still-owned source item and is cleared before unlocking.
    Source fields themselves are never passed to a borrowed-slot resolver.
    No source owner is healed or disposed here. Root copy plans are finished
    after outermost unlock, before any child root/token is retired.
    """
    status: i64 = -1
    pcc_py_gc_minor_graph_lock()
    source = load_ptr(source_slot, 0)
    borrowed = ptr_add(children, length * abi_constant("object.pointer.size"))
    if ptr_is_null(source) == 0 and is_tagged_int(source) == 0 and length >= 0 and ptr_is_null(children) == 0:
        if load_i32(source, abi_constant("object.header.type_tag_offset")) == abi_constant("object.type.list"):
            if length == 0 or (ptr_is_null(children) == 0 and ptr_is_null(plans) == 0 and ptr_is_null(tokens) == 0):
                status = -2
                if load_i64(source, abi_constant("object.list.length_offset")) == length:
                    items = load_ptr(source, abi_constant("object.list.items_offset"))
                    status = 0
                    if length > 0 and ptr_is_null(items) != 0:
                        status = -1
                    index: i64 = 0
                    while index < length and status == 0:
                        source_field = ptr_add(items, index * abi_constant("object.pointer.size"))
                        destination = ptr_add(children, index * abi_constant("object.pointer.size"))
                        if ptr_is_null(load_ptr(source_field, 0)) != 0:
                            status = -1
                        else:
                            store_ptr(borrowed, 0, load_ptr(source_field, 0))
                            token: i64 = pcc_gc_root_copy_lease_prepare_locked(
                                destination, borrowed, 1,
                                ptr_add(plans, index * _SEQUENCE_SNAPSHOT_PLAN_BYTES),
                            )
                            store_ptr(borrowed, 0, null())
                            if token < 0:
                                status = -1
                            else:
                                store_i64(tokens, index * abi_constant("object.pointer.size"), token)
                        index += 1
    pcc_py_gc_minor_graph_unlock()
    return status
