"""Shared generational slot promotion and stable-root rewriting."""
from pcc import i64
from pcc.runtime.py.py_abi_constants import PY_TYPE_MEMORYVIEW

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_load_i64,
    atomic_rmw_i32,
    atomic_store_i64,
    cstr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_diff,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_gc_backend3_young_unlink = extern(
    "pcc_gc_backend3_young_unlink", (c_ptr,), c_void
)
pcc_gc_backend4_zpage_note_owner_promoted = extern(
    "pcc_gc_backend4_zpage_note_owner_promoted", (c_ptr,), c_void
)
pcc_gc_config_ensure = extern("pcc_gc_config_ensure", (), c_int64)
pcc_py_gc_defer_tripwire = extern(
    "pcc_py_gc_defer_tripwire", (c_ptr, c_ptr, c_int32), c_void
)
pcc_gc_forwarding_find = extern("pcc_gc_forwarding_find", (c_ptr,), c_ptr)
pcc_gc_generational_oldify_copy = extern(
    "pcc_gc_generational_oldify_copy", (c_ptr,), c_ptr
)
pcc_gc_object_index_find = extern("pcc_gc_object_index_find", (c_ptr,), c_ptr)
pcc_gc_object_node_is_active = extern(
    "pcc_gc_object_node_is_active", (c_ptr,), c_int64
)
pcc_gc_object_is_known_no_lock = extern(
    "pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64
)
pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_gc_visit_object_slots_slice = extern("pcc_gc_visit_object_slots_slice", (c_ptr, c_int64, c_int64, c_ptr, c_ptr, c_ptr), c_int64)
pcc_gc_memoryview_refresh_owned_buffer = extern(
    "pcc_gc_memoryview_refresh_owned_buffer", (c_ptr,), c_int64
)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)


@c_abi_export("pcc_gc_generational_pointer_can_have_header")
def pcc_gc_generational_pointer_can_have_header(obj) -> i64:
    return pcc_gc_pointer_is_managed(obj)


@c_abi_export("pcc_gc_generational_promote_young_if_known")
def pcc_gc_generational_promote_young_if_known(obj) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if pcc_gc_object_is_known_no_lock(obj) == 0:
        return
    flags: i64 = load_i32(obj, 12)
    if (flags & 128) == 0:
        return
    oldified = pcc_gc_generational_oldify_copy(obj)
    if ptr_is_null(oldified) == 0:
        pcc_gc_trace_referents_for_promotion(oldified)
        return
    backend: i64 = pcc_gc_config_ensure()
    if backend == 3 and (load_i32(obj, 12) & 4096) != 0:
        pcc_gc_backend3_young_unlink(pcc_gc_object_index_find(obj))
        promoted_flags: i64 = load_i32(obj, 12)
        store_i32(obj, 12, (promoted_flags & ~(128 | 512)) | 256)
        pcc_gc_trace_referents_for_promotion(obj)
        return
    promoted_flags: i64 = load_i32(obj, 12)
    if (promoted_flags & 256) != 0:
        # A YOUNG object can never already be OLD: promoting it again would
        # corrupt the young list and (GC4) carry YOUNG into the next bit.
        pcc_py_gc_defer_tripwire(
            cstr(
                "pcc_gc_promote_young_object: promoting a YOUNG object already"
                " marked OLD (young->old generation invariant violated)"
            ),
            cstr("pcc/runtime/py/freestanding_gc_generational_promotion.py"),
            99,
        )
        return
    pcc_gc_backend3_young_unlink(pcc_gc_object_index_find(obj))
    if backend == 4 and (promoted_flags & 256) == 0:
        # YOUNG and OLD are adjacent bits.  On the valid GC4 transition,
        # adding YOUNG atomically clears it, carries into OLD, and preserves
        # concurrently published unrelated header flags.
        atomic_rmw_i32("add", obj, 12, 128, "acq_rel")
        pcc_gc_backend4_zpage_note_owner_promoted(obj)
        return
    store_i32(obj, 12, (promoted_flags & ~128) | 256)
    if backend == 3:
        pcc_gc_trace_referents_for_promotion(obj)


@c_abi_export("pcc_gc_generational_promote_owned_slot_mode")
def pcc_gc_generational_promote_owned_slot_mode(
    slot_base, slot_offset: i64, recurse: i64
) -> None:
    child = load_ptr(slot_base, slot_offset)
    if pcc_gc_generational_pointer_can_have_header(child) == 0:
        return
    if (
        pcc_gc_object_is_known_no_lock(child) == 0
        and ptr_is_null(pcc_gc_forwarding_find(child)) != 0
    ):
        return
    child_flags: i64 = load_i32(child, 12)
    if (child_flags & (128 | 2048)) == 0:
        return
    oldified = pcc_gc_generational_oldify_copy(child)
    if ptr_is_null(oldified) == 0 and ptr_eq(oldified, child) == 0:
        py_incref(oldified)
        store_ptr(slot_base, slot_offset, oldified)
        pcc_gc_trace_referents_for_promotion(oldified)
        py_decref(child)
        return
    if recurse == 0:
        return
    pcc_gc_generational_promote_young_if_known(child)


@c_abi_export("pcc_gc_generational_promote_borrowed_slot_mode")
def pcc_gc_generational_promote_borrowed_slot_mode(
    slot_base, slot_offset: i64, recurse: i64
) -> None:
    child = load_ptr(slot_base, slot_offset)
    if pcc_gc_generational_pointer_can_have_header(child) == 0:
        return
    if (
        pcc_gc_object_is_known_no_lock(child) == 0
        and ptr_is_null(pcc_gc_forwarding_find(child)) != 0
    ):
        return
    child_flags: i64 = load_i32(child, 12)
    if (child_flags & (128 | 2048)) == 0:
        return
    oldified = pcc_gc_generational_oldify_copy(child)
    if ptr_is_null(oldified) == 0 and ptr_eq(oldified, child) == 0:
        store_ptr(slot_base, slot_offset, oldified)
        pcc_gc_trace_referents_for_promotion(oldified)
        return
    if recurse == 0:
        return
    pcc_gc_generational_promote_young_if_known(child)


@c_abi_export("pcc_gc_generational_promote_slot")
def pcc_gc_generational_promote_slot(slot, role: i64, context) -> None:
    if role == 1:
        pcc_gc_generational_promote_owned_slot_mode(slot, 0, 1)
    else:
        pcc_gc_generational_promote_borrowed_slot_mode(slot, 0, 1)


@c_abi_export("pcc_gc_generational_promote_shallow_slot")
def pcc_gc_generational_promote_shallow_slot(slot, role: i64, context) -> None:
    if role == 1:
        pcc_gc_generational_promote_owned_slot_mode(slot, 0, 0)
    else:
        pcc_gc_generational_promote_borrowed_slot_mode(slot, 0, 0)


@c_abi_export("pcc_gc_backend3_promotion_worklist_unlink")
def _promotion_unlink(node) -> None:
    if ptr_is_null(node) != 0:
        return
    obj = load_ptr(node, 0)
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if (load_i32(obj, 12) & 128) != 0:
        return
    prev = load_ptr(node, 72)
    nxt = load_ptr(node, 64)
    if (
        ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_head"), node) == 0
        and ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_tail"), node) == 0
        and ptr_is_null(prev) != 0
        and ptr_is_null(nxt) != 0
    ):
        return
    if ptr_is_null(prev) == 0:
        store_ptr(prev, 64, nxt)
    elif ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_head"), node) != 0:
        global_store_ptr("pcc_gc_backend3_promotion_head", nxt)
    if ptr_is_null(nxt) == 0:
        store_ptr(nxt, 72, prev)
    elif ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_tail"), node) != 0:
        global_store_ptr("pcc_gc_backend3_promotion_tail", prev)
    store_ptr(node, 64, null())
    store_ptr(node, 72, null())
    store_i64(node, 56, 0)


@c_abi_export("pcc_gc_backend3_enqueue_promotion_owner")
def _enqueue_promotion_owner(obj) -> None:
    backend: i64 = pcc_gc_config_ensure()
    if backend != 3 and backend != 4:
        return
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    node = pcc_gc_object_index_find(obj)
    if ptr_is_null(node) != 0 or pcc_gc_object_node_is_active(node) == 0:
        return
    if ptr_eq(load_ptr(node, 0), obj) == 0:
        return
    if (load_i32(obj, 12) & 128) != 0:
        return
    if (
        ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_head"), node) != 0
        or ptr_eq(global_load_ptr("pcc_gc_backend3_promotion_tail"), node) != 0
        or ptr_is_null(load_ptr(node, 64)) == 0
        or ptr_is_null(load_ptr(node, 72)) == 0
    ):
        return
    tail = global_load_ptr("pcc_gc_backend3_promotion_tail")
    store_ptr(node, 64, null())
    store_ptr(node, 72, tail)
    store_i64(node, 56, 0)
    store_i64(
        global_addr("pcc_gc_backend3_promotion_revision"),
        0,
        load_i64(global_addr("pcc_gc_object_list_revision"), 0),
    )
    if ptr_is_null(tail) == 0:
        store_ptr(tail, 64, node)
    else:
        global_store_ptr("pcc_gc_backend3_promotion_head", node)
    global_store_ptr("pcc_gc_backend3_promotion_tail", node)


@c_abi_export("pcc_gc_backend3_promote_cext_slot_transaction")
def _promote_cext_slot_transaction(slot, role: i64, context) -> None:
    pcc_py_gc_minor_graph_lock()
    pcc_gc_generational_promote_slot(slot, role, context)
    pcc_py_gc_minor_graph_unlock()


@c_abi_export("pcc_gc_backend3_promote_cext_owner_referents")
def _promote_cext_owner_referents(obj) -> None:
    pcc_gc_visit_object_slots(
        obj, _promote_cext_slot_transaction, null()
    )


@c_abi_export("pcc_gc_backend3_promotion_probe_config")
def pcc_gc_backend3_promotion_probe_config(pause: i64) -> None:
    atomic_store_i64(
        global_addr("pcc_gc_backend3_promotion_probe_state_value"),
        0,
        0,
        "release",
    )
    atomic_store_i64(
        global_addr("pcc_gc_backend3_promotion_probe_pause"),
        0,
        pause,
        "release",
    )


@c_abi_export("pcc_gc_backend3_promotion_probe_state")
def pcc_gc_backend3_promotion_probe_state() -> i64:
    return atomic_load_i64(
        global_addr("pcc_gc_backend3_promotion_probe_state_value"),
        0,
        "acquire",
    )


@c_abi_export("pcc_gc_backend3_drain_promotion_worklist")
def pcc_gc_backend3_drain_promotion_worklist(budget: i64) -> i64:
    if budget <= 0:
        return 0
    state = stack_alloc(16)
    total_examined: i64 = 0
    while total_examined < budget:
        batch_limit: i64 = budget - total_examined
        if batch_limit > 16:
            batch_limit = 16
        batch_examined: i64 = 0
        more_work: i64 = 0
        callback_owner = null()
        pcc_py_gc_minor_graph_lock()
        while (
            ptr_is_null(global_load_ptr("pcc_gc_backend3_promotion_head")) == 0
            and batch_examined < batch_limit
        ):
            node = global_load_ptr("pcc_gc_backend3_promotion_head")
            obj = load_ptr(node, 0)
            if (
                pcc_gc_object_node_is_active(node) == 0
                or ptr_eq(pcc_gc_object_index_find(obj), node) == 0
            ):
                _promotion_unlink(node)
                batch_examined = batch_examined + 1
                total_examined = total_examined + 1
                continue
            revision: i64 = load_i64(
                global_addr("pcc_gc_object_list_revision"), 0
            )
            if load_i64(
                global_addr("pcc_gc_backend3_promotion_revision"), 0
            ) != revision:
                store_i64(
                    global_addr("pcc_gc_backend3_promotion_revision"),
                    0,
                    revision,
                )
            store_i64(state, 0, -1)
            store_i64(state, 8, 0)
            handled: i64 = pcc_gc_visit_object_slots_slice(
                obj,
                load_i64(node, 56),
                batch_limit - batch_examined,
                pcc_gc_generational_promote_slot,
                null(),
                state,
            )
            if handled == 0:
                py_incref(obj)
                callback_owner = obj
                _promotion_unlink(node)
                store_i64(state, 0, -1)
                store_i64(state, 8, 1)
            examined: i64 = load_i64(state, 8)
            if examined <= 0:
                examined = 1
                store_i64(state, 0, -1)
            batch_examined = batch_examined + examined
            total_examined = total_examined + examined
            next_cursor: i64 = load_i64(state, 0)
            if next_cursor < 0:
                _promotion_unlink(node)
            else:
                store_i64(node, 56, next_cursor)
                store_i64(
                    global_addr("pcc_gc_backend3_promotion_revision"),
                    0,
                    revision,
                )
            if ptr_is_null(callback_owner) == 0:
                break
        if ptr_is_null(global_load_ptr("pcc_gc_backend3_promotion_head")) == 0:
            more_work = 1
        pcc_py_gc_minor_graph_unlock()
        if ptr_is_null(callback_owner) == 0:
            _promote_cext_owner_referents(callback_owner)
            py_decref(callback_owner)
            pcc_py_gc_minor_graph_lock()
            if ptr_is_null(
                global_load_ptr("pcc_gc_backend3_promotion_head")
            ) == 0:
                more_work = 1
            else:
                more_work = 0
            pcc_py_gc_minor_graph_unlock()
        if (
            more_work != 0
            and total_examined >= 16
            and atomic_load_i64(
                global_addr("pcc_gc_backend3_promotion_probe_pause"),
                0,
                "acquire",
            ) != 0
        ):
            atomic_store_i64(
                global_addr("pcc_gc_backend3_promotion_probe_state_value"),
                0,
                1,
                "release",
            )
            while atomic_load_i64(
                global_addr("pcc_gc_backend3_promotion_probe_pause"),
                0,
                "acquire",
            ) != 0:
                pcc_thread_safepoint()
            atomic_store_i64(
                global_addr("pcc_gc_backend3_promotion_probe_state_value"),
                0,
                2,
                "release",
            )
        if batch_examined > 0:
            pcc_thread_safepoint()
        if batch_examined == 0 or more_work == 0:
            break
    return total_examined


@c_abi_export("pcc_gc_trace_referents_for_promotion_mode")
def pcc_gc_trace_referents_for_promotion_mode(obj, recurse: i64) -> None:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return
    if recurse != 0:
        _enqueue_promotion_owner(obj)
    else:
        pcc_gc_visit_object_slots(
            obj, pcc_gc_generational_promote_shallow_slot, null()
        )
    if load_i32(obj, 8) == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        # Promotion may replace base@16; refresh the non-owning Py_buffer
        # aliases after the shared slot visitor has completed.
        pcc_gc_memoryview_refresh_owned_buffer(obj)


@c_abi_export("pcc_gc_trace_referents_for_promotion")
def pcc_gc_trace_referents_for_promotion(obj) -> None:
    pcc_gc_trace_referents_for_promotion_mode(obj, 1)


@c_abi_export("pcc_gc_generational_root_slot_value_is_stable")
def pcc_gc_generational_root_slot_value_is_stable(value) -> i64:
    if pcc_gc_generational_pointer_can_have_header(value) == 0:
        return 1
    if pcc_gc_object_is_known_no_lock(value) == 0:
        if ptr_is_null(pcc_gc_forwarding_find(value)) != 0:
            return 1
        return 0
    flags: i64 = load_i32(value, 12)
    if (flags & (128 | 2048)) == 0:
        return 1
    return 0


@c_abi_export("pcc_gc_promote_cached_frame_slot")
def pcc_gc_promote_cached_frame_slot(
    slot_base, slot_offset: i64, stable_base, borrowed: i64
) -> None:
    before = load_ptr(slot_base, slot_offset)
    if ptr_is_null(stable_base) == 0:
        if ptr_eq(load_ptr(stable_base, slot_offset), before) != 0:
            return
    if borrowed != 0:
        pcc_gc_generational_promote_borrowed_slot_mode(slot_base, slot_offset, 1)
    else:
        pcc_gc_generational_promote_owned_slot_mode(slot_base, slot_offset, 1)
    if ptr_is_null(stable_base) != 0:
        return
    after = load_ptr(slot_base, slot_offset)
    if pcc_gc_generational_root_slot_value_is_stable(after) != 0:
        store_ptr(stable_base, slot_offset, after)
    else:
        store_ptr(stable_base, slot_offset, null())
