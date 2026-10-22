"""pcc-Python owners for the GC object-slot visit surface.

Replaces the pcc_capi_visit_slot / visit_cext_object_slots /
visit_cext_object_slots_i64 + the PccCapiObjectSlotVisitCtx + the
pcc_capi_visit_cext_object_slot_ref sentinel block of py_capi_shim.c.
The freestanding GC drives these to walk C-extension object slots.

PccCapiObjectSlotVisitCtx: visit@0 (fn ptr), ctx@8.
visitproc: int(*)(PyObject*, void*); traverseproc: int(*)(PyObject*, visitproc, void*);
PyObjSlotVisitor: void(*)(PyObject**, int32_t, void*).

Owned surface (stable C ABI names):

  pcc_capi_visit_slot, pcc_capi_visit_cext_object_slots,
  pcc_capi_visit_cext_object_slots_i64, pcc_capi_visit_cext_object_slot_ref
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_CEXT_TAG_BASE,
)
from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_i64_ptr2,
    call_i64_ptr3,
    call_void_ptr_i64_ptr,
    function_addr,
    global_addr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_ptr,
)

pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_capi_cext_type_count = extern("pcc_capi_cext_type_count", (), c_int32)

@c_abi_typed_export("pcc_capi_visit_cext_object_slot_ref", "i32", ("ptr", "ptr"))
def pcc_capi_visit_cext_object_slot_ref(obj, arg) -> int:
    return 0


@c_abi_typed_export("pcc_capi_visit_slot", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_visit_slot(slot, visit, arg) -> int:
    if ptr_is_null(slot) or ptr_is_null(visit):
        return 0
    if ptr_eq(visit, function_addr("pcc_capi_visit_cext_object_slot_ref")):
        visit_ctx = arg
        if ptr_is_null(visit_ctx) or ptr_is_null(load_ptr(visit_ctx, 0)):
            return 0
        call_void_ptr_i64_ptr(
            load_ptr(visit_ctx, 0), slot, (1), load_ptr(visit_ctx, 8)
        )
        return 0
    obj = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(obj):
        return 0
    return call_i64_ptr2(visit, obj, arg)


@c_abi_typed_export("pcc_capi_visit_cext_object_slots", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_visit_cext_object_slots(o, visit, ctx) -> int:
    if ptr_is_null(o) or is_tagged_int(o) or ptr_is_null(visit):
        return 0
    tag: int = load_i32(o, 8)
    offset = tag - (PY_TYPE_CEXT_TAG_BASE)
    count: int = load_i32(global_addr("pcc_capi_cext_type_count"), 0)
    if offset < 0 or offset >= count:
        return 0
    table = global_addr("pcc_capi_cext_types")
    type_obj = load_ptr(ptr_add(table, offset * 8), 0)
    if ptr_is_null(type_obj):
        return 1
    traverse = load_ptr(type_obj, (192))
    if ptr_is_null(traverse):
        return 1
    visit_ctx = stack_alloc(16)
    store_ptr(visit_ctx, 0, visit)
    store_ptr(visit_ctx, 8, ctx)
    call_i64_ptr3(
        traverse,
        o,
        function_addr("pcc_capi_visit_cext_object_slot_ref"),
        visit_ctx,
    )
    return 1


@c_abi_typed_export(
    "pcc_capi_visit_cext_object_slot_i64_adapter",
    "void",
    ("ptr", "i64", "ptr"),
)
def pcc_capi_visit_cext_object_slot_i64_adapter(
    slot, role: int, arg
) -> None:
    if ptr_is_null(arg) or ptr_is_null(load_ptr(arg, 0)):
        return
    call_void_ptr_i64_ptr(
        load_ptr(arg, 0), slot, role, load_ptr(arg, 8)
    )


@c_abi_typed_export("pcc_capi_visit_cext_object_slots_i64", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_visit_cext_object_slots_i64(o, visit, ctx) -> int:
    if ptr_is_null(visit):
        return 0
    visit_ctx = stack_alloc(16)
    store_ptr(visit_ctx, 0, visit)
    store_ptr(visit_ctx, 8, ctx)
    return pcc_capi_visit_cext_object_slots(
        o,
        function_addr("pcc_capi_visit_cext_object_slot_i64_adapter"),
        visit_ctx,
    )
