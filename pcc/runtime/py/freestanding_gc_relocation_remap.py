"""Backend 4 relocation eligibility and shared-slot remapping."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_gc_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
pcc_gc_generational_oldify_supported_tag = extern(
    "pcc_gc_generational_oldify_supported_tag", (c_int64,), c_int64
)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_gc_memoryview_refresh_owned_buffer = extern(
    "pcc_gc_memoryview_refresh_owned_buffer", (c_ptr,), c_int64
)
pcc_py_gc_minor_graph_lock = extern(
    "pcc_py_gc_minor_graph_lock", (), c_void
)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)


@c_abi_export("pcc_gc_backend4_relocate_copy_supported_tag")
def pcc_gc_backend4_relocate_copy_supported_tag(tag: i64) -> i64:
    if tag == abi_constant("object.type.property"):
        return 1
    if tag == abi_constant("object.type.classmethod"):
        return 1
    if tag == abi_constant("object.type.staticmethod"):
        return 1
    if tag == abi_constant("object.type.memoryview"):
        return 1
    if tag == abi_constant("object.type.func"):
        return 1
    if tag == abi_constant("object.type.iter"):
        return 1
    if tag == abi_constant("object.type.gen"):
        return 1
    if tag == abi_constant("object.type.coroutine"):
        return 1
    if tag == abi_constant("object.type.continuation"):
        return 1
    if tag == abi_constant("object.type.exc"):
        return 1
    if tag == abi_constant("object.type.class"):
        return 1
    if tag == abi_constant("object.type.weakref"):
        return 1
    if tag == abi_constant("object.type.thread"):
        return 1
    if tag == abi_constant("object.type.list"):
        return 1
    if tag == abi_constant("object.type.dict"):
        return 1
    if tag == abi_constant("object.type.tuple"):
        return 1
    if tag == abi_constant("object.type.set"):
        return 1
    if tag == abi_constant("object.type.task"):
        return 1
    if tag == abi_constant("object.type.virtual_thread"):
        return 1
    if tag == abi_constant("object.type.vthread_channel"):
        return 1
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0
    if (
        tag == abi_constant("object.type.instance")
        or tag >= abi_constant("object.type.user_class_start")
    ):
        return 1
    return pcc_gc_generational_oldify_supported_tag(tag)


@c_abi_export("pcc_gc_backend4_remap_heal_slot")
def pcc_gc_backend4_remap_heal_slot(base, offset: i64) -> None:
    value = load_ptr(base, offset)
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return
    if (
        load_i32(value, abi_constant("object.header.flags_offset")) & 2048
    ) == 0:  # PCC_OBJ_FLAG_FORWARDED
        return
    node = pcc_gc_forwarding_find(value)
    if ptr_is_null(node) != 0:
        return
    target = load_ptr(node, 8)
    if ptr_is_null(target) != 0:
        return
    # Count-on-NEW: the slot's reference is already represented by target.
    store_ptr(base, offset, target)


@c_abi_export("pcc_gc_backend4_remap_slot")
def pcc_gc_backend4_remap_slot(slot, role: i64, context) -> None:
    pcc_gc_backend4_remap_heal_slot(slot, 0)


@c_abi_export("pcc_gc_backend4_remap_cext_ctx_valid")
def _pcc_gc_backend4_remap_cext_ctx_valid(context) -> i64:
    obj = load_ptr(context, 0)
    if (
        load_i32(global_addr("pcc_gc_backend4_remap_active"), 0) == 0
        or load_i64(global_addr("pcc_gc_backend4_remap_epoch"), 0)
        != load_i64(context, 8)
        or ptr_eq(
            global_load_ptr("pcc_gc_backend4_remap_pending_obj"), obj
        ) == 0
        or load_i32(global_addr("pcc_gc_backend_selected"), 0) != 4
        or load_i64(global_addr("pcc_gc_object_list_revision"), 0)
        != load_i64(context, 16)
        or ptr_eq(global_load_ptr("pcc_gc_forwarding_head"), load_ptr(context, 24))
        == 0
        or load_i32(global_addr("pcc_gc_forwarding_population"), 0)
        != load_i64(context, 32)
        or load_i64(global_addr("pcc_gc_backend4_reseed_page_revision"), 0)
        != load_i64(context, 40)
        or load_i64(
            global_addr("pcc_gc_backend4_reseed_relocation_revision"), 0
        ) != load_i64(context, 48)
    ):
        return 0
    return 1


@c_abi_export("pcc_gc_backend4_remap_cext_slot_transaction")
def pcc_gc_backend4_remap_cext_slot_transaction(
    slot, role: i64, context
) -> None:
    if ptr_is_null(slot) != 0 or ptr_is_null(context) != 0:
        return
    pcc_py_gc_minor_graph_lock()
    if _pcc_gc_backend4_remap_cext_ctx_valid(context) != 0:
        pcc_gc_backend4_remap_heal_slot(slot, 0)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_backend4_remap_cext_referents_unlocked")
def pcc_gc_backend4_remap_cext_referents_unlocked(obj, context) -> None:
    pcc_gc_visit_object_slots(
        obj, pcc_gc_backend4_remap_cext_slot_transaction, context
    )


@c_abi_export("pcc_gc_backend4_remap_referents")
def pcc_gc_backend4_remap_referents(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    # A dying object (count reached zero; flag 524288) is being torn down
    # by its releasing thread, possibly parked at a safepoint inside its
    # deallocation: its slots may already name freed children. No collector
    # pass reads them; the releasing thread finishes the object.
    if (load_i32(obj, abi_constant("object.header.flags_offset")) & 524288) != 0:
        return
    pcc_gc_visit_object_slots(obj, pcc_gc_backend4_remap_slot, null())
    if load_i32(
        obj, abi_constant("object.header.type_tag_offset")
    ) == abi_constant("object.type.memoryview"):
        # Py_buffer.obj/buf are derived raw aliases, not owning GC slots.
        pcc_gc_memoryview_refresh_owned_buffer(obj)
