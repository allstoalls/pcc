"""Backend 4 raw-payload copying through the shared object-slot contract."""

from pcc import i64
from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    cstr,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memmove,
    memset,
    null,
    ptr_add,
    ptr_diff,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_gc_backend4_remap_heal_slot = extern(
    "pcc_gc_backend4_remap_heal_slot", (c_ptr, c_int64), c_void
)
pcc_gc_backend4_remembered_set_retarget_slot = extern(
    "pcc_gc_backend4_remembered_set_retarget_slot", (c_ptr, c_ptr, c_ptr, c_ptr), c_void
)
pcc_gc_backend4_zpage_payload_span_preflight_locked = extern(
    "pcc_gc_backend4_zpage_payload_span_preflight_locked",
    (c_ptr, c_int64),
    c_int64
)
pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked = extern(
    "pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked",
    (c_ptr, c_ptr, c_int64, c_int64),
    c_int64
)
pcc_gc_backend4_source_side_table_plan_prepare = extern(
    "pcc_gc_backend4_source_side_table_plan_prepare", (c_ptr,), c_ptr
)
pcc_gc_backend4_source_side_table_plan_commit = extern(
    "pcc_gc_backend4_source_side_table_plan_commit", (c_ptr,), c_int64
)
pcc_gc_backend4_source_side_table_plan_finish = extern(
    "pcc_gc_backend4_source_side_table_plan_finish", (c_ptr, c_ptr), c_void
)
pcc_py_gc_defer_tripwire = extern(
    "pcc_py_gc_defer_tripwire", (c_ptr, c_ptr, c_int32), c_void
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_visit_object_slots = extern(
    "pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64
)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_mem_free = extern("py_mem_free", (c_ptr,), c_void)
pcc_gc_retain_plan_prepare_locked = extern(
    "pcc_gc_retain_plan_prepare_locked", (c_ptr, c_ptr), c_ptr
)
pcc_gc_retain_plan_finish = extern(
    "pcc_gc_retain_plan_finish", (c_ptr,), c_void
)
pcc_gc_root_registry_note_mutation_locked = extern(
    "pcc_gc_root_registry_note_mutation_locked", (), c_void
)


@c_abi_export("pcc_gc_relocation_payload_raw_plan_finish")
def _relocate_raw_plan_finish(ctx) -> None:
    if ptr_is_null(ctx) != 0:
        return
    count: i64 = load_i64(ctx, 72)
    index: i64 = 0
    while index < count and index < 4:
        descriptor = ptr_add(ctx, 152 + index * 64)
        buffer = load_ptr(descriptor, 48)
        if ptr_is_null(buffer) == 0:
            free(buffer)
            store_ptr(descriptor, 48, null())
        span_node = load_ptr(descriptor, 56)
        if ptr_is_null(span_node) == 0:
            free(span_node)
            store_ptr(descriptor, 56, null())
        index = index + 1
    store_i64(ctx, 80, 0)


@c_abi_export("pcc_gc_relocation_payload_slot_pairs_dispose")
def _relocate_slot_pairs_dispose(ctx) -> None:
    if ptr_is_null(ctx) != 0:
        return
    _relocate_raw_plan_finish(ctx)
    entries = load_ptr(ctx, 16)
    if ptr_is_null(entries) == 0:
        count: i64 = load_i64(ctx, 24)
        index: i64 = 0
        while index < count:
            pcc_gc_retain_plan_finish(ptr_add(entries, index * 80 + 24))
            index = index + 1
        free(entries)
    if ptr_eq(global_load_ptr("pcc_gc_relocate_slot_pairs_ctx"), ctx) != 0:
        global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", null())
    free(ctx)


@c_abi_export("pcc_gc_relocation_payload_count_slot")
def _relocate_count_slot(slot, role: i64, context) -> None:
    ctx = context
    if ptr_is_null(ctx) != 0:
        ctx = global_load_ptr("pcc_gc_relocate_slot_pairs_ctx")
    if ptr_is_null(ctx) != 0:
        return
    count: i64 = load_i64(ctx, 24)
    if count >= 9223372036854775807:
        store_i32(ctx, 40, 0)
        return
    store_i64(ctx, 24, count + 1)


@c_abi_export("pcc_gc_relocation_payload_from_slot")
def _relocate_from_slot(slot, role: i64, context) -> None:
    ctx = global_load_ptr("pcc_gc_relocate_slot_pairs_ctx")
    if ptr_is_null(ctx) != 0:
        return
    index: i64 = load_i64(ctx, 32)
    count: i64 = load_i64(ctx, 24)
    if index >= count:
        store_i32(ctx, 40, 0)
        return
    entries = load_ptr(ctx, 16)
    entry = ptr_add(entries, index * 80)
    store_ptr(entry, 0, slot)
    store_i32(entry, 8, role)
    store_i64(ctx, 32, index + 1)


@c_abi_export("pcc_gc_relocation_payload_to_slot")
def _relocate_to_slot(slot, role: i64, context) -> None:
    ctx = global_load_ptr("pcc_gc_relocate_slot_pairs_ctx")
    if ptr_is_null(ctx) != 0:
        return
    index: i64 = load_i64(ctx, 32)
    count: i64 = load_i64(ctx, 24)
    if index >= count:
        store_i32(ctx, 40, 0)
        return
    entries = load_ptr(ctx, 16)
    entry = ptr_add(entries, index * 80)
    if load_i32(entry, 8) != role:
        store_i32(ctx, 40, 0)
    store_ptr(entry, 16, slot)
    store_i64(ctx, 32, index + 1)


@c_abi_export("pcc_gc_relocation_payload_slot_count_locked")
def pcc_gc_relocation_payload_slot_count_locked(from_obj) -> i64:
    if ptr_is_null(from_obj) != 0 or is_tagged_int(from_obj) != 0:
        return -1
    count_ctx = stack_alloc(56)
    memset(count_ctx, 0, 56)
    store_i32(count_ctx, 40, 1)
    if pcc_gc_visit_object_slots(from_obj, _relocate_count_slot, count_ctx) == 0:
        return -1
    if load_i32(count_ctx, 40) == 0:
        return -1
    return load_i64(count_ctx, 24)


@c_abi_export("pcc_gc_relocation_payload_plan_prepare")
def pcc_gc_relocation_payload_plan_prepare(count: i64):
    if count < 0 or count > 115292150460684697:
        return null()
    ctx = malloc(416)
    if ptr_is_null(ctx) != 0:
        return null()
    memset(ctx, 0, 416)
    store_i64(ctx, 24, count)
    store_i32(ctx, 40, 1)
    if count > 0:
        entries = malloc(count * 80)
        if ptr_is_null(entries) != 0:
            _relocate_slot_pairs_dispose(ctx)
            return null()
        memset(entries, 0, count * 80)
        store_ptr(ctx, 16, entries)
    return ctx


@c_abi_export("pcc_gc_relocation_payload_raw_add_descriptor")
def _relocate_raw_add_descriptor(
    ctx,
    source,
    alloc_bytes: i64,
    copy_bytes: i64,
    destination_offset: i64,
    span_bytes: i64,
    zero_fill: i64,
) -> i64:
    if ptr_is_null(ctx) != 0:
        return 0
    count: i64 = load_i64(ctx, 72)
    if (
        count < 0
        or count >= 4
        or alloc_bytes <= 0
        or copy_bytes < 0
        or copy_bytes > alloc_bytes
        or span_bytes < 0
        or span_bytes > alloc_bytes
    ):
        return 0
    descriptor = ptr_add(ctx, 152 + count * 64)
    store_ptr(descriptor, 0, source)
    store_i64(descriptor, 8, alloc_bytes)
    store_i64(descriptor, 16, copy_bytes)
    store_i64(descriptor, 24, destination_offset)
    store_i64(descriptor, 32, span_bytes)
    store_i64(descriptor, 40, zero_fill)
    store_i64(ctx, 72, count + 1)
    return 1


@c_abi_export("pcc_gc_relocation_payload_raw_snapshot_locked")
def pcc_gc_relocation_payload_raw_snapshot_locked(
    from_obj, tag: i64, size: i64, ctx
) -> i64:
    if (
        ptr_is_null(from_obj) != 0
        or is_tagged_int(from_obj) != 0
        or ptr_is_null(ctx) != 0
        or size < 16
    ):
        return 0
    if load_i32(from_obj, 8) != tag:
        return 0
    memset(ptr_add(ctx, 56), 0, 360)
    store_i64(ctx, 56, tag)
    store_i64(ctx, 64, size)

    if tag == abi_constant("object.type.continuation"):
        if size < 48:
            return 0
        chunk = load_ptr(from_obj, 24)
        store_i64(ctx, 104, load_i64(from_obj, 32))
        if ptr_is_null(chunk) != 0:
            return 1
        slot_count: i64 = load_i64(chunk, 8)
        slots = load_ptr(chunk, 16)
        if slot_count < 0 or slot_count > 1152921504606846975:
            return 0
        if slot_count > 0 and ptr_is_null(slots) != 0:
            return 0
        store_i64(ctx, 88, load_i32(chunk, 0))
        store_i64(ctx, 96, slot_count)
        if _relocate_raw_add_descriptor(
            ctx, chunk, 24, 0, 24, 0, 1
        ) == 0:
            return 0
        if slot_count > 0 and _relocate_raw_add_descriptor(
            ctx,
            slots,
            slot_count * 8,
            slot_count * 8,
            -1,
            slot_count * 8,
            1,
        ) == 0:
            return 0
        return 1

    if tag == abi_constant("object.type.exc"):
        if size < 64:
            return 0
        traceback = load_ptr(from_obj, 48)
        n_frames: i64 = load_i32(from_obj, 56)
        cap_frames: i64 = load_i32(from_obj, 60)
        if n_frames < 0 or cap_frames < 0 or n_frames > cap_frames:
            return 0
        if cap_frames > 288230376151711743:
            return 0
        if cap_frames > 0 and ptr_is_null(traceback) != 0:
            return 0
        store_i64(ctx, 88, n_frames)
        store_i64(ctx, 96, cap_frames)
        if cap_frames > 0 and _relocate_raw_add_descriptor(
            ctx, traceback, cap_frames * 32, cap_frames * 32, 48, 0, 0
        ) == 0:
            return 0
        return 1

    if tag == abi_constant("object.type.class"):
        if size < abi_constant("object.class.size"):
            return 0
        n_bases: i64 = load_i32(
            from_obj, abi_constant("object.class.n_bases_offset")
        )
        n_mro: i64 = load_i32(
            from_obj, abi_constant("object.class.n_mro_offset")
        )
        n_methods: i64 = load_i32(
            from_obj, abi_constant("object.class.n_methods_offset")
        )
        n_fields: i64 = load_i32(
            from_obj, abi_constant("object.class.n_fields_offset")
        )
        if n_bases < 0 or n_mro < 0 or n_methods < 0 or n_fields < 0:
            return 0
        if n_bases > 1152921504606846975 or n_mro > 1152921504606846975:
            return 0
        if n_methods > 576460752303423487 or n_fields > 1152921504606846975:
            return 0
        store_i64(ctx, 88, n_bases)
        store_i64(ctx, 96, n_mro)
        store_i64(ctx, 104, n_methods)
        store_i64(ctx, 112, n_fields)
        if n_bases > 0:
            bases = load_ptr(from_obj, abi_constant("object.class.bases_offset"))
            if ptr_is_null(bases) != 0 or _relocate_raw_add_descriptor(
                ctx,
                bases,
                n_bases * 8,
                n_bases * 8,
                abi_constant("object.class.bases_offset"),
                n_bases * 8,
                0,
            ) == 0:
                return 0
        if n_mro > 0:
            mro = load_ptr(from_obj, abi_constant("object.class.mro_offset"))
            if ptr_is_null(mro) != 0 or _relocate_raw_add_descriptor(
                ctx,
                mro,
                n_mro * 8,
                n_mro * 8,
                abi_constant("object.class.mro_offset"),
                n_mro * 8,
                0,
            ) == 0:
                return 0
        if n_methods > 0:
            methods = load_ptr(
                from_obj, abi_constant("object.class.methods_offset")
            )
            method_bytes: i64 = n_methods * abi_constant("object.class_method.size")
            if ptr_is_null(methods) != 0 or _relocate_raw_add_descriptor(
                ctx,
                methods,
                method_bytes,
                method_bytes,
                abi_constant("object.class.methods_offset"),
                method_bytes,
                0,
            ) == 0:
                return 0
        if n_fields > 0:
            field_names = load_ptr(
                from_obj, abi_constant("object.class.field_names_offset")
            )
            if ptr_is_null(field_names) != 0 or _relocate_raw_add_descriptor(
                ctx,
                field_names,
                n_fields * 8,
                n_fields * 8,
                abi_constant("object.class.field_names_offset"),
                0,
                0,
            ) == 0:
                return 0
        return 1

    if tag == abi_constant("object.type.dict"):
        if size < 56:
            return 0
        dict_size: i64 = load_i64(from_obj, 16)
        capacity: i64 = load_i64(from_obj, 24)
        entries_used: i64 = load_i64(from_obj, 48)
        if (
            dict_size < 0
            or capacity < 0
            or entries_used < 0
            or entries_used > capacity
            or dict_size > entries_used
        ):
            return 0
        store_i64(ctx, 88, dict_size)
        store_i64(ctx, 96, capacity)
        store_i64(ctx, 104, entries_used)
        if capacity == 0:
            return 1
        indices = load_ptr(from_obj, 32)
        entries = load_ptr(from_obj, 40)
        if (
            ptr_is_null(indices) != 0
            or ptr_is_null(entries) != 0
            or capacity > 384307168202282325
        ):
            return 0
        if _relocate_raw_add_descriptor(
            ctx, indices, capacity * 8, capacity * 8, 32, 0, 0
        ) == 0:
            return 0
        if _relocate_raw_add_descriptor(
            ctx, entries, capacity * 24, capacity * 24, 40, capacity * 24, 0
        ) == 0:
            return 0
        return 1

    if tag == abi_constant("object.type.set"):
        if size < 48:
            return 0
        set_size: i64 = load_i64(from_obj, 16)
        capacity: i64 = load_i64(from_obj, 24)
        fill: i64 = load_i64(from_obj, 32)
        if capacity < 0:
            return 0
        store_i64(ctx, 88, set_size)
        store_i64(ctx, 96, capacity)
        store_i64(ctx, 104, fill)
        if capacity == 0:
            return 1
        entries = load_ptr(from_obj, 40)
        if ptr_is_null(entries) != 0 or capacity > 576460752303423487:
            return 0
        return _relocate_raw_add_descriptor(
            ctx, entries, capacity * 16, capacity * 16, 40, capacity * 16, 0
        )

    if tag == abi_constant("object.type.list"):
        if size < 40:
            return 0
        length: i64 = load_i64(from_obj, 16)
        capacity: i64 = load_i64(from_obj, 24)
        if length < 0 or capacity < length or capacity > 1152921504606846975:
            return 0
        store_i64(ctx, 88, length)
        store_i64(ctx, 96, capacity)
        if capacity == 0:
            return 1
        items = load_ptr(from_obj, 32)
        if ptr_is_null(items) != 0:
            return 0
        return _relocate_raw_add_descriptor(
            ctx, items, capacity * 8, length * 8, 32, capacity * 8, 1
        )

    return 1


@c_abi_export("pcc_gc_relocation_payload_raw_prepare")
def pcc_gc_relocation_payload_raw_prepare(ctx) -> i64:
    if ptr_is_null(ctx) != 0:
        return 0
    count: i64 = load_i64(ctx, 72)
    index: i64 = 0
    while index < count:
        descriptor = ptr_add(ctx, 152 + index * 64)
        alloc_bytes: i64 = load_i64(descriptor, 8)
        buffer = malloc(alloc_bytes)
        if ptr_is_null(buffer) != 0:
            return 0
        if load_i64(descriptor, 40) != 0:
            memset(buffer, 0, alloc_bytes)
        store_ptr(descriptor, 48, buffer)
        if load_i64(descriptor, 32) > 0:
            span_node = malloc(48)
            if ptr_is_null(span_node) != 0:
                return 0
            memset(span_node, 0, 48)
            store_ptr(descriptor, 56, span_node)
        index = index + 1
    store_i64(ctx, 80, 1)
    return 1


@c_abi_export("pcc_gc_relocation_payload_raw_validate_locked")
def pcc_gc_relocation_payload_raw_validate_locked(
    from_obj, to_obj, tag: i64, size: i64, ctx
) -> i64:
    if ptr_is_null(ctx) != 0 or load_i64(ctx, 80) == 0:
        return 0
    current = stack_alloc(416)
    memset(current, 0, 416)
    if pcc_gc_relocation_payload_raw_snapshot_locked(
        from_obj, tag, size, current
    ) == 0:
        return 0
    if load_i64(current, 56) != load_i64(ctx, 56):
        return 0
    if load_i64(current, 64) != load_i64(ctx, 64):
        return 0
    current_count: i64 = load_i64(current, 72)
    if current_count != load_i64(ctx, 72):
        return 0
    scalar_index: i64 = 0
    while scalar_index < 8:
        if load_i64(current, 88 + scalar_index * 8) != load_i64(
            ctx, 88 + scalar_index * 8
        ):
            return 0
        scalar_index = scalar_index + 1
    total_span_bytes: i64 = 0
    index: i64 = 0
    while index < current_count:
        current_descriptor = ptr_add(current, 152 + index * 64)
        saved_descriptor = ptr_add(ctx, 152 + index * 64)
        if ptr_eq(
            load_ptr(current_descriptor, 0), load_ptr(saved_descriptor, 0)
        ) == 0:
            return 0
        field: i64 = 8
        while field <= 40:
            if load_i64(current_descriptor, field) != load_i64(
                saved_descriptor, field
            ):
                return 0
            field = field + 8
        span_bytes: i64 = load_i64(current_descriptor, 32)
        if span_bytes > 9223372036854775807 - total_span_bytes:
            return 0
        total_span_bytes = total_span_bytes + span_bytes
        index = index + 1
    return pcc_gc_backend4_zpage_payload_span_preflight_locked(
        to_obj, total_span_bytes
    )


@c_abi_export("pcc_gc_relocation_payload_plan_validate_locked")
def pcc_gc_relocation_payload_plan_validate_locked(
    from_obj, to_obj, size: i64, ctx
) -> i64:
    if (
        ptr_is_null(from_obj) != 0
        or ptr_is_null(to_obj) != 0
        or is_tagged_int(from_obj) != 0
        or is_tagged_int(to_obj) != 0
        or ptr_is_null(ctx) != 0
        or size < 0
    ):
        return 0
    store_ptr(ctx, 0, from_obj)
    store_ptr(ctx, 8, to_obj)
    store_i64(ctx, 48, size)
    store_i32(ctx, 40, 1)
    global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", ctx)
    store_i64(ctx, 32, 0)
    if pcc_gc_visit_object_slots(from_obj, _relocate_from_slot, null()) == 0:
        global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", null())
        return 0
    global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", null())
    count: i64 = load_i64(ctx, 24)
    if load_i64(ctx, 32) != count or load_i32(ctx, 40) == 0:
        return 0
    return 1


@c_abi_export("pcc_gc_relocation_payload_plan_finish")
def pcc_gc_relocation_payload_plan_finish(ctx) -> None:
    _relocate_slot_pairs_dispose(ctx)


@c_abi_export("pcc_gc_relocation_payload_slot_pairs_prepare")
def _relocate_slot_pairs_prepare(from_obj, to_obj, size: i64):
    count: i64 = pcc_gc_relocation_payload_slot_count_locked(from_obj)
    ctx = pcc_gc_relocation_payload_plan_prepare(count)
    if ptr_is_null(ctx) != 0:
        return null()
    tag: i64 = load_i32(from_obj, 8)
    if pcc_gc_relocation_payload_raw_snapshot_locked(
        from_obj, tag, size, ctx
    ) == 0:
        _relocate_slot_pairs_dispose(ctx)
        return null()
    if pcc_gc_relocation_payload_raw_prepare(ctx) == 0:
        _relocate_slot_pairs_dispose(ctx)
        return null()
    if pcc_gc_relocation_payload_plan_validate_locked(
        from_obj, to_obj, size, ctx
    ) == 0:
        _relocate_slot_pairs_dispose(ctx)
        return null()
    if pcc_gc_relocation_payload_raw_validate_locked(
        from_obj, to_obj, tag, size, ctx
    ) == 0:
        _relocate_slot_pairs_dispose(ctx)
        return null()
    return ctx


@c_abi_export("pcc_gc_relocation_payload_clear_destination_owned")
def _relocate_slot_pairs_clear_destination(to_obj, ctx) -> None:
    if ptr_is_null(to_obj) != 0 or ptr_is_null(ctx) != 0:
        return
    from_obj = load_ptr(ctx, 0)
    object_size: i64 = load_i64(ctx, 48)
    entries = load_ptr(ctx, 16)
    count: i64 = load_i64(ctx, 24)
    index: i64 = 0
    while index < count:
        entry = ptr_add(entries, index * 80)
        from_slot = load_ptr(entry, 0)
        role: i64 = load_i32(entry, 8)
        inline_offset: i64 = ptr_diff(from_slot, from_obj)
        if role == 1 and inline_offset >= 0 and inline_offset + 8 <= object_size:
            store_ptr(to_obj, inline_offset, null())
        index = index + 1


@c_abi_export("pcc_gc_relocation_payload_copy_slots")
def _relocate_copy_slots(from_obj, to_obj, ctx) -> i64:
    if (
        ptr_is_null(from_obj) != 0
        or ptr_is_null(to_obj) != 0
        or ptr_is_null(ctx) != 0
    ):
        return 0
    count: i64 = load_i64(ctx, 24)
    store_i64(ctx, 32, 0)
    store_i32(ctx, 40, 1)
    global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", ctx)
    visited: i64 = pcc_gc_visit_object_slots(to_obj, _relocate_to_slot, null())
    global_store_ptr("pcc_gc_relocate_slot_pairs_ctx", null())
    if visited == 0:
        return 0
    if load_i64(ctx, 32) != count or load_i32(ctx, 40) == 0:
        return 0
    entries = load_ptr(ctx, 16)
    index: i64 = 0
    while index < count:
        entry = ptr_add(entries, index * 80)
        from_slot = load_ptr(entry, 0)
        role: i64 = load_i32(entry, 8)
        to_slot = load_ptr(entry, 16)
        pcc_gc_backend4_remap_heal_slot(from_slot, 0)
        value = load_ptr(from_slot, 0)
        if ptr_eq(value, from_obj) != 0:
            value = to_obj
        if role == 1:  # _PY_OBJ_SLOT_OWNED
            value = pcc_gc_retain_plan_prepare_locked(
                ptr_add(entry, 24), value
            )
        store_ptr(to_slot, 0, value)
        pcc_gc_backend4_remembered_set_retarget_slot(
            from_obj, to_obj, from_slot, to_slot
        )
        index = index + 1
    return 1


@c_abi_export("pcc_gc_relocation_payload_retire_count_slot")
def _retire_count_owned_slot(slot, role: i64, context) -> None:
    if ptr_is_null(context) != 0:
        return
    # Healing is count-neutral and must include borrowed roles.  In
    # particular an instance visitor reloads its role-2 class slot before it
    # derives the number of owned field slots.
    pcc_gc_backend4_remap_heal_slot(slot, 0)
    if role == 1:  # _PY_OBJ_SLOT_OWNED
        count: i64 = load_i64(context, 0)
        if count >= 9223372036854775807:
            store_i32(context, 24, 0)
            return
        store_i64(context, 0, count + 1)


@c_abi_export("pcc_gc_relocation_payload_retire_collect_slot")
def _retire_collect_owned_slot(slot, role: i64, context) -> None:
    if ptr_is_null(context) != 0:
        return
    pcc_gc_backend4_remap_heal_slot(slot, 0)
    if role != 1:  # _PY_OBJ_SLOT_OWNED
        return
    index: i64 = load_i64(context, 8)
    count: i64 = load_i64(context, 0)
    if index >= count:
        store_i32(context, 24, 0)
        return
    records = load_ptr(context, 16)
    store_ptr(records, index * 16, slot)
    store_i64(context, 8, index + 1)


@c_abi_export("pcc_gc_relocation_retire_source_payload_into_finish_impl")
def _retire_source_payload_into_finish(from_obj, finish, decref_exclusion) -> i64:
    """Drop only the old relocation copy's payload ownership.

    The caller holds the GC graph lock and keeps the source forwarding edge
    and relocation flags live through this call.  In particular, the saved
    values below remain stable while their source slots are detached.  Do not
    turn this into a normal deallocator: finalizers, weakrefs, continuation
    registration, the object header, and its page have separate owners.
    """
    if (
        ptr_is_null(from_obj) != 0
        or is_tagged_int(from_obj) != 0
        or ptr_is_null(finish) != 0
    ):
        return 0
    tag: i64 = load_i32(from_obj, 8)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0

    # The first visitor may perform count-neutral forwarding heals, including
    # borrowed roles needed to discover the complete slot shape.  Every
    # allocation and both passes still finish before any ownership or raw
    # payload mutation, so failure leaves that payload intact.
    context = malloc(96)
    if ptr_is_null(context) != 0:
        return 0
    memset(context, 0, 96)
    store_i32(context, 24, 1)
    if pcc_gc_visit_object_slots(
        from_obj, _retire_count_owned_slot, context
    ) == 0:
        free(context)
        return 0
    count: i64 = load_i64(context, 0)
    if (
        load_i32(context, 24) == 0
        or count < 0
        or count > 576460752303423487
    ):
        free(context)
        return 0
    records = null()
    if count > 0:
        records = malloc(count * 16)
        if ptr_is_null(records) != 0:
            free(context)
            return 0
        memset(records, 0, count * 16)
        store_ptr(context, 16, records)
    if pcc_gc_visit_object_slots(
        from_obj, _retire_collect_owned_slot, context
    ) == 0:
        free(records)
        free(context)
        return 0
    if load_i64(context, 8) != count or load_i32(context, 24) == 0:
        free(records)
        free(context)
        return 0
    # Prepare a stable snapshot of every store-buffer-owned reference before
    # source mutation.  The opaque plan performs all allocation and a second
    # exact pass here; commit below is allocation- and decref-free.
    side_plan = pcc_gc_backend4_source_side_table_plan_prepare(from_obj)
    if ptr_is_null(side_plan) != 0:
        free(records)
        free(context)
        return 0
    store_ptr(context, 32, side_plan)

    # Heal and detach every OWNED slot without running decref.  The record's
    # value cell is an ownership token protected by the caller-held graph lock;
    # no moving collection can relocate it before the final decref loop.
    index: i64 = 0
    while index < count:
        record = ptr_add(records, index * 16)
        slot = load_ptr(record, 0)
        store_ptr(record, 8, load_ptr(slot, 0))
        store_ptr(slot, 0, null())
        index = index + 1

    # Detach and zero every independently allocated source payload, retaining
    # its raw bases locally.  Nothing is freed until owner side tables and
    # zpage metadata are invisible, so commit cannot observe dangling slots.
    raw0 = null()
    raw1 = null()
    raw2 = null()
    raw3 = null()
    owned_buffer = null()
    if tag == abi_constant("object.type.continuation"):
        chunk = load_ptr(from_obj, 24)
        store_ptr(from_obj, 24, null())
        if ptr_is_null(chunk) == 0:
            slots = load_ptr(chunk, 16)
            store_i32(chunk, 0, 0)
            store_i32(chunk, 4, 0)
            store_i64(chunk, 8, 0)
            store_ptr(chunk, 16, null())
            raw0 = slots
            raw1 = chunk
    elif tag == abi_constant("object.type.exc"):
        traceback = load_ptr(from_obj, 48)
        store_ptr(from_obj, 48, null())
        store_i32(from_obj, 56, 0)
        store_i32(from_obj, 60, 0)
        raw0 = traceback
    elif tag == abi_constant("object.type.class"):
        bases = load_ptr(from_obj, abi_constant("object.class.bases_offset"))
        mro = load_ptr(from_obj, abi_constant("object.class.mro_offset"))
        methods = load_ptr(from_obj, abi_constant("object.class.methods_offset"))
        field_names = load_ptr(
            from_obj, abi_constant("object.class.field_names_offset")
        )
        store_i32(from_obj, abi_constant("object.class.n_bases_offset"), 0)
        store_ptr(from_obj, abi_constant("object.class.bases_offset"), null())
        store_i32(from_obj, abi_constant("object.class.n_mro_offset"), 0)
        store_ptr(from_obj, abi_constant("object.class.mro_offset"), null())
        store_i32(from_obj, abi_constant("object.class.n_methods_offset"), 0)
        store_ptr(from_obj, abi_constant("object.class.methods_offset"), null())
        store_i32(from_obj, abi_constant("object.class.n_fields_offset"), 0)
        store_ptr(
            from_obj, abi_constant("object.class.field_names_offset"), null()
        )
        raw0 = bases
        raw1 = mro
        raw2 = methods
        raw3 = field_names
    elif tag == abi_constant("object.type.dict"):
        indices = load_ptr(from_obj, 32)
        dict_entries = load_ptr(from_obj, 40)
        store_i64(from_obj, 16, 0)
        store_i64(from_obj, 24, 0)
        store_ptr(from_obj, 32, null())
        store_ptr(from_obj, 40, null())
        store_i64(from_obj, 48, 0)
        raw0 = dict_entries
        raw1 = indices
    elif tag == abi_constant("object.type.set"):
        set_entries = load_ptr(from_obj, 40)
        store_i64(from_obj, 16, 0)
        store_i64(from_obj, 24, 0)
        store_i64(from_obj, 32, 0)
        store_ptr(from_obj, 40, null())
        raw0 = set_entries
    elif tag == abi_constant("object.type.tuple"):
        store_i64(from_obj, 16, 0)
    elif tag == abi_constant("object.type.list"):
        items = load_ptr(from_obj, 32)
        store_i64(from_obj, 16, 0)
        store_i64(from_obj, 24, 0)
        store_ptr(from_obj, 32, null())
        raw0 = items
    elif tag == abi_constant("object.type.memoryview"):
        # Relocate-copy transfers this PyMem_Malloc-owned Py_buffer to the
        # target and leaves NULL here.  The raw internal forwarding seam has
        # no such provenance guarantee, so retirement defensively consumes a
        # still-live source allocation through its real allocator owner.
        owned_buffer = load_ptr(from_obj, 24)
        store_ptr(from_obj, 24, null())
    # All remaining supported types have only inline or borrowed raw state.

    # The source is now inert.  Commit makes all owner store/remembered/card
    # entries invisible and removes the complete zpage owner/span accounting
    # bundle, without allocation or decref.  Relocate-copy already removed the
    # zpage, while public direct forwarding exercises the live removal path.
    if pcc_gc_backend4_source_side_table_plan_commit(side_plan) == 0:
        pcc_py_gc_defer_tripwire(
            cstr("source side-table commit failed after payload detachment"),
            cstr(
                "pcc/runtime/py/freestanding_gc_relocation_payload.py"
            ),
            374,
        )
        return 0
    store_ptr(context, 40, raw0)
    store_ptr(context, 48, raw1)
    store_ptr(context, 56, raw2)
    store_ptr(context, 64, raw3)
    store_ptr(context, 72, owned_buffer)
    store_ptr(context, 80, decref_exclusion)
    store_ptr(context, 88, load_ptr(finish, 32))
    store_ptr(finish, 32, context)
    return 1


@c_abi_export("pcc_gc_relocation_retire_source_payload_into_finish")
def pcc_gc_relocation_retire_source_payload_into_finish(
    from_obj, finish
) -> i64:
    return _retire_source_payload_into_finish(from_obj, finish, null())


@c_abi_export("pcc_gc_relocation_retire_source_payload_for_target_death_into_finish")
def pcc_gc_relocation_retire_source_payload_for_target_death_into_finish(
    from_obj, target, finish
) -> i64:
    if ptr_is_null(target) != 0 or is_tagged_int(target) != 0:
        return 0
    return _retire_source_payload_into_finish(from_obj, finish, target)


@c_abi_export("pcc_gc_relocation_finish_source_payloads")
def pcc_gc_relocation_finish_source_payloads(plans) -> None:
    while ptr_is_null(plans) == 0:
        context = plans
        plans = load_ptr(context, 88)
        store_ptr(context, 88, null())
        records = load_ptr(context, 16)
        count: i64 = load_i64(context, 0)
        side_plan = load_ptr(context, 32)
        raw0 = load_ptr(context, 40)
        raw1 = load_ptr(context, 48)
        raw2 = load_ptr(context, 56)
        raw3 = load_ptr(context, 64)
        owned_buffer = load_ptr(context, 72)
        decref_exclusion = load_ptr(context, 80)
        store_ptr(context, 16, null())
        store_ptr(context, 32, null())
        store_ptr(context, 40, null())
        store_ptr(context, 48, null())
        store_ptr(context, 56, null())
        store_ptr(context, 64, null())
        store_ptr(context, 72, null())
        store_ptr(context, 80, null())
        free(raw0)
        free(raw1)
        free(raw2)
        free(raw3)
        py_mem_free(owned_buffer)

        # Store-buffer references are released only after raw storage and all
        # owner metadata are gone. Any decref reentry sees an inert source and
        # no owner side-table entries. Source-slot ownership is released last.
        pcc_gc_backend4_source_side_table_plan_finish(side_plan, decref_exclusion)
        index: i64 = 0
        while index < count:
            value = load_ptr(records, index * 16 + 8)
            if (
                ptr_is_null(decref_exclusion) != 0
                or ptr_eq(value, decref_exclusion) == 0
            ):
                py_decref(value)
            index = index + 1
        free(records)
        free(context)


@c_abi_export("pcc_gc_relocation_retire_source_payload")
def pcc_gc_relocation_retire_source_payload(from_obj) -> i64:
    finish = stack_alloc(48)
    store_ptr(finish, 0, null())
    store_ptr(finish, 8, null())
    store_ptr(finish, 16, null())
    store_ptr(finish, 24, null())
    store_ptr(finish, 32, null())
    store_ptr(finish, 40, null())
    if pcc_gc_relocation_retire_source_payload_into_finish(from_obj, finish) == 0:
        return 0
    pcc_gc_relocation_finish_source_payloads(load_ptr(finish, 32))
    return 1


@c_abi_export("pcc_gc_relocation_payload_retarget_continuation_root_slots")
def _retarget_continuation_root_slots(from_slots, from_map, to_slots, to_map) -> None:
    if ptr_is_null(from_slots) != 0 or ptr_is_null(to_slots) != 0:
        return
    if ptr_is_null(to_map) != 0:
        return
    changed: i64 = 0
    node = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(node) == 0:
        if ptr_eq(load_ptr(node, 8), from_slots) != 0:
            if ptr_is_null(from_map) != 0 or ptr_eq(load_ptr(node, 0), from_map) != 0:
                store_ptr(node, 0, to_map)
                store_ptr(node, 8, to_slots)
                if ptr_eq(
                    global_load_ptr(
                        "pcc_gc_backend3_continuation_root_scan_cursor"
                    ),
                    node,
                ) != 0:
                    store_i64(
                        global_addr("pcc_gc_backend3_frame_root_scan_slot"),
                        0,
                        0,
                    )
                changed = 1
        node = load_ptr(node, 16)
    if changed != 0:
        pcc_gc_root_registry_note_mutation_locked()


@c_abi_export("pcc_gc_relocation_payload_raw_transfer_buffers")
def _relocate_raw_transfer_buffers(ctx) -> None:
    count: i64 = load_i64(ctx, 72)
    index: i64 = 0
    while index < count:
        store_ptr(ctx, 152 + index * 64 + 48, null())
        index = index + 1


@c_abi_export("pcc_gc_relocation_payload_raw_publish_locked")
def _relocate_raw_publish_locked(to_obj, ctx) -> i64:
    if ptr_is_null(to_obj) != 0 or ptr_is_null(ctx) != 0:
        return 0
    if load_i64(ctx, 80) == 0:
        return 0
    tag: i64 = load_i64(ctx, 56)
    count: i64 = load_i64(ctx, 72)

    if tag == abi_constant("object.type.continuation"):
        store_ptr(to_obj, 24, null())
    elif tag == abi_constant("object.type.exc"):
        store_ptr(to_obj, 48, null())
        store_i32(to_obj, 56, 0)
        store_i32(to_obj, 60, 0)
    elif tag == abi_constant("object.type.class"):
        store_i32(to_obj, abi_constant("object.class.n_bases_offset"), 0)
        store_ptr(to_obj, abi_constant("object.class.bases_offset"), null())
        store_i32(to_obj, abi_constant("object.class.n_mro_offset"), 0)
        store_ptr(to_obj, abi_constant("object.class.mro_offset"), null())
        store_i32(to_obj, abi_constant("object.class.n_methods_offset"), 0)
        store_ptr(to_obj, abi_constant("object.class.methods_offset"), null())
        store_i32(to_obj, abi_constant("object.class.n_fields_offset"), 0)
        store_ptr(to_obj, abi_constant("object.class.field_names_offset"), null())
        store_ptr(to_obj, abi_constant("object.class.attrs_offset"), null())
    elif tag == abi_constant("object.type.dict"):
        store_i64(to_obj, 16, 0)
        store_i64(to_obj, 24, 0)
        store_ptr(to_obj, 32, null())
        store_ptr(to_obj, 40, null())
        store_i64(to_obj, 48, 0)
    elif tag == abi_constant("object.type.set"):
        store_i64(to_obj, 16, 0)
        store_i64(to_obj, 24, 0)
        store_i64(to_obj, 32, 0)
        store_ptr(to_obj, 40, null())
    elif tag == abi_constant("object.type.list"):
        store_i64(to_obj, 16, 0)
        store_i64(to_obj, 24, 0)
        store_ptr(to_obj, 32, null())

    span_head = null()
    span_tail = null()
    span_count: i64 = 0
    total_span_bytes: i64 = 0
    index: i64 = 0
    while index < count:
        descriptor = ptr_add(ctx, 152 + index * 64)
        buffer = load_ptr(descriptor, 48)
        if ptr_is_null(buffer) != 0:
            return 0
        copy_bytes: i64 = load_i64(descriptor, 16)
        if copy_bytes > 0:
            memmove(buffer, load_ptr(descriptor, 0), copy_bytes)
        span_bytes: i64 = load_i64(descriptor, 32)
        if span_bytes > 0:
            span = load_ptr(descriptor, 56)
            if (
                ptr_is_null(span) != 0
                or span_bytes > 9223372036854775807 - total_span_bytes
            ):
                return 0
            memset(span, 0, 48)
            store_ptr(span, 8, buffer)
            store_i64(span, 16, span_bytes)
            if ptr_is_null(span_tail) == 0:
                store_ptr(span_tail, 40, span)
            else:
                span_head = span
            span_tail = span
            span_count = span_count + 1
            total_span_bytes = total_span_bytes + span_bytes
        index = index + 1
    if span_count > 0:
        if pcc_gc_backend4_zpage_publish_relocation_payload_spans_locked(
            to_obj, span_head, span_count, total_span_bytes
        ) == 0:
            return 0
        index = 0
        while index < count:
            descriptor = ptr_add(ctx, 152 + index * 64)
            if load_i64(descriptor, 32) > 0:
                store_ptr(descriptor, 56, null())
            index = index + 1

    if tag == abi_constant("object.type.continuation") and count > 0:
        chunk = load_ptr(ctx, 152 + 48)
        store_i32(chunk, 0, load_i64(ctx, 88))
        store_i32(chunk, 4, 0)
        store_i64(chunk, 8, load_i64(ctx, 96))
        if count > 1:
            store_ptr(chunk, 16, load_ptr(ctx, 152 + 64 + 48))
        else:
            store_ptr(chunk, 16, null())
        store_ptr(to_obj, 24, chunk)
    elif tag != abi_constant("object.type.continuation"):
        index = 0
        while index < count:
            descriptor = ptr_add(ctx, 152 + index * 64)
            store_ptr(
                to_obj,
                load_i64(descriptor, 24),
                load_ptr(descriptor, 48),
            )
            index = index + 1

    if tag == abi_constant("object.type.exc"):
        store_i32(to_obj, 56, load_i64(ctx, 88))
        store_i32(to_obj, 60, load_i64(ctx, 96))
    elif tag == abi_constant("object.type.class"):
        store_i32(
            to_obj, abi_constant("object.class.n_bases_offset"), load_i64(ctx, 88)
        )
        store_i32(
            to_obj, abi_constant("object.class.n_mro_offset"), load_i64(ctx, 96)
        )
        store_i32(
            to_obj,
            abi_constant("object.class.n_methods_offset"),
            load_i64(ctx, 104),
        )
        store_i32(
            to_obj,
            abi_constant("object.class.n_fields_offset"),
            load_i64(ctx, 112),
        )
    elif tag == abi_constant("object.type.dict"):
        store_i64(to_obj, 16, load_i64(ctx, 88))
        store_i64(to_obj, 24, load_i64(ctx, 96))
        store_i64(to_obj, 48, load_i64(ctx, 104))
    elif tag == abi_constant("object.type.set"):
        store_i64(to_obj, 16, load_i64(ctx, 88))
        store_i64(to_obj, 24, load_i64(ctx, 96))
        store_i64(to_obj, 32, load_i64(ctx, 104))
    elif tag == abi_constant("object.type.list"):
        store_i64(to_obj, 16, load_i64(ctx, 88))
        store_i64(to_obj, 24, load_i64(ctx, 96))

    _relocate_raw_transfer_buffers(ctx)
    return 1


@c_abi_export("pcc_gc_relocation_payload_fail")
def _relocate_copy_payload_fail(ctx) -> i64:
    # The caller owns the split slot-retain plan.  Backend 4 finishes it only
    # after releasing the graph lock; the legacy GC3 wrapper finishes it in
    # its existing outer holder until that later A3b slice is split as well.
    return 0


@c_abi_export("pcc_gc_relocation_payload_finish")
def _relocate_copy_payload_finish(
    from_obj,
    to_obj,
    tag: i64,
    ctx,
    continuation_src_chunk,
    continuation_dst_chunk,
    continuation_mounted: i64,
) -> i64:
    if _relocate_copy_slots(from_obj, to_obj, ctx) == 0:
        return _relocate_copy_payload_fail(ctx)
    if tag == abi_constant("object.type.weakref"):
        prev = load_ptr(from_obj, 32)
        nxt = load_ptr(from_obj, 40)
        store_ptr(to_obj, 32, prev)
        store_ptr(to_obj, 40, nxt)
        if ptr_is_null(prev) != 0:
            global_store_ptr("py_weakref_head", to_obj)
        else:
            store_ptr(prev, 40, to_obj)
        if ptr_is_null(nxt) == 0:
            store_ptr(nxt, 32, to_obj)
        store_ptr(from_obj, 32, from_obj)
        store_ptr(from_obj, 40, null())
    if (
        tag == abi_constant("object.type.continuation")
        and ptr_is_null(continuation_src_chunk) == 0
        and continuation_mounted == 0
    ):
        _retarget_continuation_root_slots(
            load_ptr(continuation_src_chunk, 16),
            continuation_src_chunk,
            load_ptr(continuation_dst_chunk, 16),
            continuation_dst_chunk,
        )
    return 1


@c_abi_export("pcc_gc_relocate_copy_payload_prepared_locked")
def pcc_gc_relocate_copy_payload_prepared_locked(
    from_obj, to_obj, tag: i64, size: i64, ctx
) -> i64:
    if ptr_is_null(ctx) != 0:
        return 0
    _relocate_slot_pairs_clear_destination(to_obj, ctx)
    continuation_src_chunk = null()
    continuation_dst_chunk = null()
    if _relocate_raw_publish_locked(to_obj, ctx) == 0:
        return _relocate_copy_payload_fail(ctx)
    if tag == abi_constant("object.type.continuation"):
        continuation_src_chunk = load_ptr(from_obj, 24)
        continuation_dst_chunk = load_ptr(to_obj, 24)
        return _relocate_copy_payload_finish(
            from_obj,
            to_obj,
            tag,
            ctx,
            continuation_src_chunk,
            continuation_dst_chunk,
            load_i64(from_obj, 32),
        )

    if tag == abi_constant("object.type.weakref"):
        store_ptr(to_obj, 32, null())
        store_ptr(to_obj, 40, null())
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.thread"):
        handle = load_ptr(from_obj, 16)
        if ptr_is_null(handle) == 0:
            return _relocate_copy_payload_fail(ctx)
        store_ptr(to_obj, 16, null())
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.task"):
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.virtual_thread"):
        queued: i64 = load_i64(
            from_obj, abi_constant("object.virtual_thread.queued_offset")
        )
        # Rollback must never observe raw scheduler entries copied from the
        # source, even when the active-wait validation below rejects moving it.
        store_i64(
            to_obj, abi_constant("object.virtual_thread.queued_offset"), 0
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.timer_entry_offset"),
            null(),
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.io_entry_offset"),
            null(),
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.join_waiters_offset"),
            null(),
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.join_wait_tail_offset"),
            null(),
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.join_entry_offset"),
            null(),
        )
        store_i64(
            to_obj, abi_constant("object.virtual_thread.wait_kind_offset"), 0
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.channel_arm_a_offset"),
            null(),
        )
        store_ptr(
            to_obj,
            abi_constant("object.virtual_thread.channel_arm_b_offset"),
            null(),
        )
        if queued != 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj, abi_constant("object.virtual_thread.timer_entry_offset")
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj, abi_constant("object.virtual_thread.io_entry_offset")
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj, abi_constant("object.virtual_thread.join_waiters_offset")
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj, abi_constant("object.virtual_thread.join_wait_tail_offset")
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj, abi_constant("object.virtual_thread.join_entry_offset")
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj,
            abi_constant("object.virtual_thread.channel_arm_a_offset"),
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(load_ptr(
            from_obj,
            abi_constant("object.virtual_thread.channel_arm_b_offset"),
        )) == 0:
            return _relocate_copy_payload_fail(ctx)
        if load_i64(
            from_obj, abi_constant("object.virtual_thread.wait_kind_offset")
        ) != 0:
            return _relocate_copy_payload_fail(ctx)
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.vthread_channel"):
        kind: i64 = load_i64(
            from_obj, abi_constant("object.vthread_channel.core.kind_offset")
        )
        if kind == 0:
            store_ptr(
                to_obj,
                abi_constant("object.vthread_channel.core.send_head_offset"),
                null(),
            )
            store_ptr(
                to_obj,
                abi_constant("object.vthread_channel.core.send_tail_offset"),
                null(),
            )
            store_ptr(
                to_obj,
                abi_constant("object.vthread_channel.core.recv_head_offset"),
                null(),
            )
            store_ptr(
                to_obj,
                abi_constant("object.vthread_channel.core.recv_tail_offset"),
                null(),
            )
            capacity: i64 = load_i64(
                from_obj,
                abi_constant("object.vthread_channel.core.capacity_offset"),
            )
            core_size: i64 = abi_constant("object.vthread_channel.core.size")
            if capacity < 0 or capacity > 1048576:
                return _relocate_copy_payload_fail(ctx)
            if size < core_size + capacity * abi_constant("object.pointer.size"):
                return _relocate_copy_payload_fail(ctx)
            if ptr_is_null(load_ptr(
                from_obj,
                abi_constant("object.vthread_channel.core.send_head_offset"),
            )) == 0:
                return _relocate_copy_payload_fail(ctx)
            if ptr_is_null(load_ptr(
                from_obj,
                abi_constant("object.vthread_channel.core.send_tail_offset"),
            )) == 0:
                return _relocate_copy_payload_fail(ctx)
            if ptr_is_null(load_ptr(
                from_obj,
                abi_constant("object.vthread_channel.core.recv_head_offset"),
            )) == 0:
                return _relocate_copy_payload_fail(ctx)
            if ptr_is_null(load_ptr(
                from_obj,
                abi_constant("object.vthread_channel.core.recv_tail_offset"),
            )) == 0:
                return _relocate_copy_payload_fail(ctx)
            if load_i64(
                from_obj,
                abi_constant("object.vthread_channel.core.flags_offset"),
            ) != 0:
                return _relocate_copy_payload_fail(ctx)
        elif kind == 1 or kind == 2:
            if size < abi_constant("object.vthread_channel.endpoint.size"):
                return _relocate_copy_payload_fail(ctx)
        else:
            return _relocate_copy_payload_fail(ctx)
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.memoryview"):
        # The raw Py_buffer allocation is single-owner state, not a GC slot.
        # Keep it on the source until the forwarding transaction commits;
        # otherwise a later install-forwarding failure would either double
        # free it through the destination or leave the source dangling.
        store_ptr(to_obj, 24, null())
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if pcc_capi_is_cext_type_tag(tag) != 0:
        return _relocate_copy_payload_fail(ctx)

    if (
        tag == abi_constant("object.type.instance")
        or tag == abi_constant("object.type.valuebox")
        or tag >= abi_constant("object.type.user_class_start")
    ):
        cls = pcc_gc_load_ptr(
            from_obj,
            ptr_add(from_obj, abi_constant("object.instance.cls_offset")),
        )
        if size < abi_constant("object.instance.size"):
            return _relocate_copy_payload_fail(ctx)
        if ptr_is_null(cls) != 0:
            return _relocate_copy_payload_fail(ctx)
        if load_i32(
            cls, abi_constant("object.header.type_tag_offset")
        ) != abi_constant("object.type.class"):
            return _relocate_copy_payload_fail(ctx)
        n_fields: i64 = load_i32(
            cls, abi_constant("object.class.n_fields_offset")
        )
        if n_fields < 0:
            n_fields: i64 = 0
        # Match allocation and both slot enumerators: include the reserved
        # owner and any appended immutable builtin payload owners.
        n_slots: i64 = (load_i32(cls, abi_constant("object.class.instance_size_offset")) - abi_constant("object.instance.fields_offset")) // abi_constant("object.pointer.size")
        if n_slots < n_fields + 1:
            n_slots = n_fields + 1
        if n_slots < 0:
            return _relocate_copy_payload_fail(ctx)
        if size < (
            abi_constant("object.instance.fields_offset")
            + n_slots * abi_constant("object.pointer.size")
        ):
            return _relocate_copy_payload_fail(ctx)
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    if tag == abi_constant("object.type.tuple"):
        length: i64 = load_i64(from_obj, 16)
        if length < 0:
            return _relocate_copy_payload_fail(ctx)
        if size < 24 + length * 8:
            return _relocate_copy_payload_fail(ctx)
        return _relocate_copy_payload_finish(
            from_obj, to_obj, tag, ctx, null(), null(), 0
        )

    return _relocate_copy_payload_finish(from_obj, to_obj, tag, ctx, null(), null(), 0)


@c_abi_export("pcc_gc_relocate_copy_payload")
def pcc_gc_relocate_copy_payload(from_obj, to_obj, tag: i64, size: i64) -> i64:
    # GC3 oldification still owns its enclosing graph-lock holder.  Keep this
    # compatibility wrapper explicit until that later A3b slice can prepare
    # the shared plan before entering its generational transaction.
    ctx = _relocate_slot_pairs_prepare(from_obj, to_obj, size)
    if ptr_is_null(ctx) != 0:
        return 0
    result: i64 = pcc_gc_relocate_copy_payload_prepared_locked(
        from_obj, to_obj, tag, size, ctx
    )
    _relocate_slot_pairs_dispose(ctx)
    return result
