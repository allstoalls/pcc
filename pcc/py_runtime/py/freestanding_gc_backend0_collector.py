"""Backend-0 stop-the-world cycle-collector orchestration.

Object geometry is owned by ``freestanding_gc_object_slots.py`` and the
subtract/mark/clear actions are owned by ``freestanding_gc_backend0_slots.py``.
This module owns only the raw backend-0 collection state machine.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


py_dealloc_int = extern("py_dealloc_int", (c_ptr,), c_void)
py_dealloc_float = extern("py_dealloc_float", (c_ptr,), c_void)
py_dealloc_str = extern("py_dealloc_str", (c_ptr,), c_void)
py_dealloc_list = extern("py_dealloc_list", (c_ptr,), c_void)
py_dealloc_tuple = extern("py_dealloc_tuple", (c_ptr,), c_void)
py_dealloc_dict = extern("py_dealloc_dict", (c_ptr,), c_void)
py_dealloc_set = extern("py_dealloc_set", (c_ptr,), c_void)
py_dealloc_func = extern("py_dealloc_func", (c_ptr,), c_void)
py_class_dealloc = extern("py_class_dealloc", (c_ptr,), c_void)
py_instance_dealloc = extern("py_instance_dealloc", (c_ptr,), c_void)
py_descriptor_dealloc = extern("py_descriptor_dealloc", (c_ptr,), c_void)
py_dealloc_exc = extern("py_dealloc_exc", (c_ptr,), c_void)
py_dealloc_iter = extern("py_dealloc_iter", (c_ptr,), c_void)
py_dealloc_gen = extern("py_dealloc_gen", (c_ptr,), c_void)
py_dealloc_coroutine = extern("py_dealloc_coroutine", (c_ptr,), c_void)
py_dealloc_continuation = extern("py_dealloc_continuation", (c_ptr,), c_void)
py_dealloc_task = extern("py_dealloc_task", (c_ptr,), c_void)
py_dealloc_virtual_thread = extern("py_dealloc_virtual_thread", (c_ptr,), c_void)
py_dealloc_vthread_channel = extern(
    "py_dealloc_vthread_channel", (c_ptr,), c_void
)
py_dealloc_memoryview = extern("py_dealloc_memoryview", (c_ptr,), c_void)
py_dealloc_weakref = extern("py_dealloc_weakref", (c_ptr,), c_void)
py_dealloc_generic = extern("py_dealloc_generic", (c_ptr,), c_void)
py_user_del_dispatch = extern("py_user_del_dispatch", (c_ptr,), c_void)
py_weakref_invalidate = extern("py_weakref_invalidate", (c_ptr,), c_void)
pcc_gc_note_object_freeing = extern(
    "pcc_gc_note_object_freeing", (c_ptr,), c_void
)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_gc_trace_continuation_roots = extern(
    "pcc_gc_trace_continuation_roots", (), c_int64
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_capi_dealloc_cext_object = extern("pcc_capi_dealloc_cext_object", (c_ptr, c_int64), c_int64)
py_gc_index_remove = extern("py_gc_index_remove", (c_ptr,), c_ptr)
pcc_gc_default_unlink_tracked_node = extern(
    "pcc_gc_default_unlink_tracked_node", (c_ptr,), c_void
)
pcc_gc_default_drain_deferred_nodes = extern(
    "pcc_gc_default_drain_deferred_nodes", (), c_void
)
pcc_gc_default_table_lock = extern("pcc_gc_default_table_lock", (), c_void)
pcc_gc_default_table_unlock = extern(
    "pcc_gc_default_table_unlock", (), c_void
)
pcc_gc_backend0_visit_subtract = extern(
    "pcc_gc_backend0_visit_subtract", (c_ptr,), c_void
)
pcc_gc_backend0_mark_reachable = extern(
    "pcc_gc_backend0_mark_reachable", (c_ptr,), c_void
)
pcc_gc_backend0_clear_referents = extern(
    "pcc_gc_backend0_clear_referents", (c_ptr,), c_void
)


@c_abi_export("pcc_gc_backend0_mapped_root_count")
def _mapped_root_count(frame_map) -> i64:
    if ptr_is_null(frame_map) != 0:
        return 0
    root_count: i64 = load_i32(frame_map, 0)
    if root_count == -2147483648:
        return 0
    if root_count < 0:
        root_count = 0 - root_count
    if root_count > 100000:
        return 0
    return root_count


@c_abi_export("pcc_gc_backend0_mark_root_slot")
def _mark_root_slot(slot_base, slot_offset: i64) -> None:
    if ptr_is_null(slot_base) != 0:
        return
    child = pcc_gc_load_ptr(null(), ptr_add(slot_base, slot_offset))
    pcc_gc_backend0_mark_reachable(child)


@c_abi_export("pcc_gc_backend0_mark_root_slots")
def _mark_root_slots(root_slots, root_count: i64) -> i64:
    if ptr_is_null(root_slots) != 0:
        return 0
    if root_count <= 0 or root_count > 100000:
        return 0
    i: i64 = 0
    while i < root_count:
        _mark_root_slot(root_slots, i * 8)
        i = i + 1
    return root_count


@c_abi_export("pcc_gc_backend0_visit_mapped_root_slots")
def _visit_mapped_root_slots(frame_map, root_slots) -> i64:
    root_count: i64 = _mapped_root_count(frame_map)
    return _mark_root_slots(root_slots, root_count)


@c_abi_export("pcc_gc_backend0_visit_scheduler_root_slots")
def _visit_scheduler_root_slots() -> i64:
    visited: i64 = 0
    node = global_load_ptr("pcc_gc_scheduler_root_head")
    while ptr_is_null(node) == 0:
        slot = load_ptr(node, 0)
        if ptr_is_null(slot) == 0:
            _mark_root_slot(slot, 0)
            visited = visited + 1
        node = load_ptr(node, 8)
    return visited


@c_abi_export("pcc_gc_backend0_mark_runtime_roots")
def _mark_runtime_roots() -> None:
    node = global_load_ptr("py_gc_head")
    while ptr_is_null(node) == 0:
        obj = load_ptr(node, 0)
        if ptr_is_null(obj) == 0 and is_tagged_int(obj) == 0:
            if (load_i32(obj, 12) & 64) != 0:
                pcc_gc_backend0_mark_reachable(obj)
        node = load_ptr(node, 32)

    root_slots = global_load_ptr("pcc_gc_root_slots")
    if ptr_is_null(root_slots) == 0:
        _mark_root_slots(
            root_slots,
            load_i32(global_addr("pcc_gc_root_count"), 0),
        )

    frame = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(frame) == 0:
        frame_map = load_ptr(frame, 0)
        slots = load_ptr(frame, 8)
        if ptr_is_null(frame_map) == 0:
            _visit_mapped_root_slots(frame_map, slots)
        frame = load_ptr(frame, 16)

    continuation = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(continuation) == 0:
        frame_map = load_ptr(continuation, 0)
        slots = load_ptr(continuation, 8)
        if ptr_is_null(frame_map) == 0:
            _visit_mapped_root_slots(frame_map, slots)
        continuation = load_ptr(continuation, 16)

    pcc_gc_trace_continuation_roots()
    _visit_scheduler_root_slots()


@c_abi_export("pcc_gc_backend0_recompute_reachability")
def _recompute_reachability() -> None:
    node = global_load_ptr("py_gc_head")
    while ptr_is_null(node) == 0:
        obj = load_ptr(node, 0)
        store_i64(node, 8, load_i64(obj, 0))
        store_i32(node, 16, 0)
        node = load_ptr(node, 32)

    node = global_load_ptr("py_gc_head")
    while ptr_is_null(node) == 0:
        pcc_gc_backend0_visit_subtract(load_ptr(node, 0))
        node = load_ptr(node, 32)

    node = global_load_ptr("py_gc_head")
    while ptr_is_null(node) == 0:
        if load_i64(node, 8) > 0:
            pcc_gc_backend0_mark_reachable(load_ptr(node, 0))
        node = load_ptr(node, 32)
    _mark_runtime_roots()


@c_abi_export("pcc_gc_backend0_maybe_finalize_unreachable")
def _maybe_finalize_unreachable(unreachable, count: i64) -> i64:
    finalized: i64 = 0
    i: i64 = 0
    while i < count:
        node = load_ptr(unreachable, i * 8)
        obj = null()
        if ptr_is_null(node) == 0:
            obj = load_ptr(node, 0)
        if ptr_is_null(obj) == 0 and is_tagged_int(obj) == 0:
            tag: i64 = load_i32(obj, 8)
            if (
                (
                    tag == abi_constant("object.type.instance")
                    or tag == abi_constant("object.type.gen")
                    or tag >= abi_constant("object.type.user_class_start")
                )
                and pcc_capi_is_cext_type_tag(tag) == 0
            ):
                flags_before: i64 = load_i32(obj, 12)
                py_user_del_dispatch(obj)
                if (flags_before & 4) == 0:
                    obj_after = load_ptr(node, 0)
                    if ptr_is_null(obj_after) != 0:
                        finalized: i64 = 1
                    elif (load_i32(obj_after, 12) & 4) != 0:
                        finalized: i64 = 1
        i = i + 1
    return finalized


@c_abi_export("pcc_gc_backend0_dealloc_unreachable")
def _dealloc_unreachable(obj) -> None:
    tag: i64 = load_i32(obj, 8)
    if tag == abi_constant("object.type.int"):
        py_dealloc_int(obj)
    elif tag == abi_constant("object.type.float"):
        py_dealloc_float(obj)
    elif tag == abi_constant("object.type.str"):
        py_dealloc_str(obj)
    elif tag == abi_constant("object.type.list"):
        py_dealloc_list(obj)
    elif tag == abi_constant("object.type.tuple"):
        py_dealloc_tuple(obj)
    elif tag == abi_constant("object.type.dict"):
        py_dealloc_dict(obj)
    elif tag == abi_constant("object.type.set"):
        py_dealloc_set(obj)
    elif tag == abi_constant("object.type.func"):
        py_dealloc_func(obj)
    elif tag == abi_constant("object.type.class"):
        py_class_dealloc(obj)
    elif tag == abi_constant("object.type.instance"):
        py_instance_dealloc(obj)
    elif tag == abi_constant("object.type.exc"):
        py_dealloc_exc(obj)
    elif tag == abi_constant("object.type.iter"):
        py_dealloc_iter(obj)
    elif tag == abi_constant("object.type.gen"):
        py_dealloc_gen(obj)
    elif tag == abi_constant("object.type.coroutine"):
        py_dealloc_coroutine(obj)
    elif tag == abi_constant("object.type.continuation"):
        py_dealloc_continuation(obj)
    elif tag == abi_constant("object.type.task"):
        py_dealloc_task(obj)
    elif tag == abi_constant("object.type.virtual_thread"):
        py_dealloc_virtual_thread(obj)
    elif tag == abi_constant("object.type.vthread_channel"):
        py_dealloc_vthread_channel(obj)
    elif tag == abi_constant("object.type.memoryview"):
        py_dealloc_memoryview(obj)
    elif tag == abi_constant("object.type.weakref"):
        py_dealloc_weakref(obj)
    elif (
        tag == abi_constant("object.type.property")
        or tag == abi_constant("object.type.classmethod")
        or tag == abi_constant("object.type.staticmethod")
    ):
        py_descriptor_dealloc(obj)
    elif pcc_capi_dealloc_cext_object(obj, tag) != 0:
        return
    elif tag >= abi_constant("object.type.user_class_start"):
        py_instance_dealloc(obj)
    else:
        py_dealloc_generic(obj)


@c_abi_export("py_gc_collect")
def py_gc_collect() -> i64:
    collecting_slot = global_addr("py_gc_collecting")
    if load_i32(collecting_slot, 0) != 0:
        return 0
    stw: i64 = pcc_stop_the_world()
    while stw != 0:
        pcc_thread_safepoint()
        stw = pcc_stop_the_world()
    pcc_gc_default_table_lock()
    store_i32(collecting_slot, 0, 1)

    tracked: i64 = load_i32(global_addr("py_gc_tracked_count"), 0)
    if tracked <= 0:
        pcc_gc_default_drain_deferred_nodes()
        store_i32(collecting_slot, 0, 0)
        pcc_gc_default_table_unlock()
        pcc_resume_world()
        return 0

    unreachable = malloc(tracked * 8)
    if ptr_is_null(unreachable):
        pcc_gc_default_drain_deferred_nodes()
        store_i32(collecting_slot, 0, 0)
        pcc_gc_default_table_unlock()
        pcc_resume_world()
        return 0

    _recompute_reachability()

    count: i64 = 0
    node = global_load_ptr("py_gc_head")
    while ptr_is_null(node) == 0:
        if load_i32(node, 16) == 0:
            # rc==0 belongs to a py_decref parked before untrack.  Genuine
            # cycle garbage has a positive internal refcount.
            obj_n = load_ptr(node, 0)
            if load_i64(obj_n, 0) > 0:
                store_ptr(unreachable, count * 8, node)
                count = count + 1
        node = load_ptr(node, 32)

    if _maybe_finalize_unreachable(unreachable, count) != 0:
        _recompute_reachability()

    i: i64 = 0
    while i < count:
        node = load_ptr(unreachable, i * 8)
        if (
            ptr_is_null(node) == 0
            and ptr_is_null(load_ptr(node, 0)) == 0
            and load_i32(node, 16) == 0
        ):
            obj = load_ptr(node, 0)
            py_weakref_invalidate(obj)
            pcc_gc_backend0_clear_referents(obj)
        i = i + 1

    i: i64 = 0
    collected: i64 = 0
    while i < count:
        node = load_ptr(unreachable, i * 8)
        if (
            ptr_is_null(node) == 0
            and ptr_is_null(load_ptr(node, 0)) == 0
            and load_i32(node, 16) == 0
        ):
            obj = load_ptr(node, 0)
            pcc_gc_default_unlink_tracked_node(node)
            py_gc_index_remove(obj)
            flags: i64 = load_i32(obj, 12)
            store_i32(obj, 12, flags & ~2)
            store_i64(obj, 0, 0)
            free(node)
            pcc_gc_note_object_freeing(obj)
            _dealloc_unreachable(obj)
            collected = collected + 1
        i = i + 1

    free(unreachable)
    pcc_gc_default_drain_deferred_nodes()
    store_i32(collecting_slot, 0, 0)
    pcc_gc_default_table_unlock()
    pcc_resume_world()
    return collected
