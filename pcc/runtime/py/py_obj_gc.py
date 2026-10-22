"""Phase 4c.7: pcc-Python port of py_obj_gc.c.

This mirrors the C runtime's default CPython-style backend: immediate
refcounting plus a side-table cycle collector for tracked containers.

PyGcNode layout (40 bytes):
    offset  0  obj       PyObject*
    offset  8  gc_refs   i64
    offset 16  reachable i32
    offset 24  prev      PyGcNode*
    offset 32  next      PyGcNode*
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int64, c_void
from pcc.unsafe import (
    global_addr,
    global_load_ptr,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
)


py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_load_ptr_extern = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)


def _append_referent(out, child) -> None:
    if ptr_is_null(child):
        return
    py_list_append(out, child)


def _py_obj_gc_visit_append_slot(slot, role: int, ctx) -> None:
    if role != 3:
        child = pcc_gc_load_ptr_extern(null(), slot)
        _append_referent(ctx, child)


def _append_referents_to(o, out) -> None:
    pcc_gc_visit_object_slots(o, _py_obj_gc_visit_append_slot, out)


@c_abi_export("py_gc_get_referents")
def py_gc_get_referents(o):
    out = py_list_new(0)
    _append_referents_to(o, out)
    return out


def _list_has_identical_item(lst, target) -> int:
    length: int = load_i64(lst, 16)
    items = load_ptr(lst, 32)
    i: int = 0
    while i < length:
        if ptr_eq(load_ptr(items, i * 8), target) != 0:
            return 1
        i = i + 1
    return 0


@c_abi_export("py_gc_get_objects")
def py_gc_get_objects():
    tracked = load_i32(global_addr("py_gc_tracked_count"), 0)
    n = global_load_ptr("py_gc_head")
    out = py_list_new(tracked)
    while ptr_is_null(n) == 0:
        obj = load_ptr(n, 0)
        if ptr_is_null(obj) == 0:
            py_list_append(out, obj)
        n = load_ptr(n, 32)
    return out


@c_abi_export("py_gc_get_referrers")
def py_gc_get_referrers(target):
    n = global_load_ptr("py_gc_head")
    out = py_list_new(0)
    while ptr_is_null(n) == 0:
        obj = load_ptr(n, 0)
        if ptr_is_null(obj) == 0:
            refs = py_gc_get_referents(obj)
            if _list_has_identical_item(refs, target) != 0:
                py_list_append(out, obj)
            py_decref(refs)
        n = load_ptr(n, 32)
    return out
