"""One raw object-layout and GC slot-role contract."""

from pcc import i64
from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, extern
from pcc.unsafe import (
    abi_constant,
    atomic_load_i64,
    call_void_ptr_i64_ptr,
    cstr,
    global_load_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    memset,
    ptr_add,
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
pcc_capi_visit_cext_object_slots_i64 = extern(
    "pcc_capi_visit_cext_object_slots_i64",
    (c_ptr, c_ptr, c_ptr),
    c_int32
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)


@c_abi_export("pcc_gc_memoryview_initialize_owned_buffer")
def pcc_gc_memoryview_initialize_owned_buffer(obj, buffer_view) -> i64:
    # Rebuild a memoryview's non-owning Py_buffer aliases from base@16.
    if ptr_is_null(obj) != 0 or ptr_is_null(buffer_view) != 0:
        return 0
    if is_tagged_int(obj) != 0 or load_i32(
        obj, abi_constant("object.header.type_tag_offset")
    ) != abi_constant("object.type.memoryview"):
        return 0
    base = pcc_gc_load_ptr(
        obj, ptr_add(obj, abi_constant("object.memoryview.base_offset"))
    )
    if ptr_is_null(base) != 0 or is_tagged_int(base) != 0:
        return 0

    exporter = base
    exporter_tag: i64 = load_i32(
        exporter, abi_constant("object.header.type_tag_offset")
    )
    while exporter_tag == abi_constant("object.type.memoryview"):
        exporter = pcc_gc_load_ptr(
            exporter,
            ptr_add(exporter, abi_constant("object.memoryview.base_offset")),
        )
        if ptr_is_null(exporter) != 0 or is_tagged_int(exporter) != 0:
            return 0
        exporter_tag = load_i32(
            exporter, abi_constant("object.header.type_tag_offset")
        )
    if exporter_tag != abi_constant(
        "object.type.bytes"
    ) and exporter_tag != abi_constant("object.type.bytearray"):
        return 0

    length: i64 = load_i64(
        exporter, abi_constant("object.bytes.byte_len_offset")
    )
    memset(buffer_view, 0, 96)
    store_ptr(
        buffer_view,
        0,
        ptr_add(exporter, abi_constant("object.bytes.data_offset")),
    )  # buf
    store_ptr(buffer_view, 8, base)  # derived alias; base@16 owns it
    store_i64(buffer_view, 16, length)
    store_i64(buffer_view, 24, 1)  # itemsize
    if exporter_tag == abi_constant("object.type.bytes"):
        store_i32(buffer_view, 32, 1)  # readonly
    else:
        store_i32(buffer_view, 32, 0)
    store_i32(buffer_view, 36, 1)  # ndim
    store_ptr(buffer_view, 40, cstr("B"))
    store_ptr(buffer_view, 48, ptr_add(buffer_view, 80))
    store_ptr(buffer_view, 56, ptr_add(buffer_view, 88))
    store_ptr(buffer_view, 64, int_to_ptr(0))  # suboffsets
    store_ptr(buffer_view, 72, int_to_ptr(0))  # internal
    store_i64(buffer_view, 80, length)  # inline shape
    store_i64(buffer_view, 88, 1)  # inline stride
    return 1


@c_abi_export("pcc_gc_memoryview_refresh_owned_buffer")
def pcc_gc_memoryview_refresh_owned_buffer(obj) -> i64:
    if ptr_is_null(obj) != 0 or is_tagged_int(obj) != 0:
        return 0
    if load_i32(
        obj, abi_constant("object.header.type_tag_offset")
    ) != abi_constant("object.type.memoryview"):
        return 0
    bits: i64 = atomic_load_i64(obj, 24, "acquire")
    if bits == 0:
        return 0
    return pcc_gc_memoryview_initialize_owned_buffer(obj, int_to_ptr(bits))


@c_abi_export("pcc_gc_object_slots_visit_slot")
def _visit_slot(
    slot_base,
    slot_offset: i64,
    role: i64,
    visitor,
    context,
) -> None:
    call_void_ptr_i64_ptr(
        visitor, ptr_add(slot_base, slot_offset), role, context
    )


@c_abi_export("pcc_gc_object_slots_visit_core_container_slots")
def _visit_core_container_slots(o, visitor, context) -> i64:
    tag: i64 = load_i32(o, abi_constant("object.header.type_tag_offset"))
    if tag == abi_constant("object.type.list"):
        length: i64 = load_i64(o, abi_constant("object.list.length_offset"))
        items = load_ptr(o, abi_constant("object.list.items_offset"))
        if ptr_is_null(items) == 0:
            index: i64 = 0
            while index < length:
                _visit_slot(items, index * 8, 1, visitor, context)
                index = index + 1
        return 1
    if tag == abi_constant("object.type.tuple"):
        length = load_i64(o, abi_constant("object.tuple.length_offset"))
        index: i64 = 0
        while index < length:
            _visit_slot(
                o,
                abi_constant("object.tuple.items_offset") + index * 8,
                1,
                visitor,
                context,
            )
            index = index + 1
        return 1
    if tag == abi_constant("object.type.dict"):
        entries = load_ptr(o, abi_constant("object.dict.entries_offset"))
        if ptr_is_null(entries) == 0:
            used: i64 = load_i64(
                o, abi_constant("object.dict.entries_used_offset")
            )
            index: i64 = 0
            while index < used:
                offset: i64 = index * abi_constant("object.dict_entry.size")
                if ptr_is_null(
                    load_ptr(
                        entries,
                        offset + abi_constant("object.dict_entry.key_offset"),
                    )
                ) == 0:
                    _visit_slot(
                        entries,
                        offset + abi_constant("object.dict_entry.key_offset"),
                        1,
                        visitor,
                        context,
                    )
                    _visit_slot(
                        entries,
                        offset + abi_constant("object.dict_entry.value_offset"),
                        1,
                        visitor,
                        context,
                    )
                index = index + 1
        return 1
    if tag == abi_constant("object.type.set"):
        entries = load_ptr(o, 40)
        if ptr_is_null(entries) == 0:
            dummy = global_load_ptr("py_set_dummy")
            capacity: i64 = load_i64(o, 24)
            index: i64 = 0
            while index < capacity:
                key = load_ptr(entries, index * 16 + 8)
                if ptr_is_null(key) == 0 and ptr_eq(key, dummy) == 0:
                    _visit_slot(entries, index * 16 + 8, 1, visitor, context)
                index = index + 1
        return 1
    if tag == abi_constant("object.type.vthread_channel"):
        kind: i64 = load_i64(
            o, abi_constant("object.vthread_channel.core.kind_offset")
        )
        if kind == 0:
            capacity = load_i64(
                o, abi_constant("object.vthread_channel.core.capacity_offset")
            )
            if capacity < 0 or capacity > 1048576:
                return 1
            index: i64 = 0
            while index < capacity:
                _visit_slot(
                    o,
                    abi_constant("object.vthread_channel.core.items_offset")
                    + index * abi_constant("object.pointer.size"),
                    1,
                    visitor,
                    context,
                )
                index = index + 1
            return 1
        if kind == 1 or kind == 2:
            _visit_slot(
                o,
                abi_constant("object.vthread_channel.endpoint.core_offset"),
                1,
                visitor,
                context,
            )
        return 1
    return 0


@c_abi_export("pcc_gc_object_slots_visit_fixed_owner_slots")
def _visit_fixed_owner_slots(o, visitor, context) -> i64:
    tag: i64 = load_i32(o, abi_constant("object.header.type_tag_offset"))
    if tag == abi_constant("object.type.func"):
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 32, 1, visitor, context)
        _visit_slot(o, 40, 1, visitor, context)
        _visit_slot(o, 64, 1, visitor, context)
        _visit_slot(o, 80, 1, visitor, context)
        _visit_slot(o, 88, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.iter"):
        _visit_slot(o, 16, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.gen"):
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 48, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.coroutine"):
        _visit_slot(o, 32, 1, visitor, context)
        _visit_slot(o, 40, 1, visitor, context)
        _visit_slot(o, 48, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.task"):
        _visit_slot(o, 16, 1, visitor, context)
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 32, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.virtual_thread"):
        _visit_slot(o, 16, 1, visitor, context)
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 72, 1, visitor, context)
        _visit_slot(o, 112, 1, visitor, context)
        _visit_slot(
            o,
            abi_constant("object.virtual_thread.channel_owner_a_offset"),
            1,
            visitor,
            context,
        )
        _visit_slot(
            o,
            abi_constant("object.virtual_thread.channel_owner_b_offset"),
            1,
            visitor,
            context,
        )
        _visit_slot(
            o,
            abi_constant("object.virtual_thread.channel_value_offset"),
            1,
            visitor,
            context,
        )
        return 1
    if tag == abi_constant("object.type.exc"):
        _visit_slot(o, 16, 1, visitor, context)
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 32, 1, visitor, context)
        _visit_slot(o, 40, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.property"):
        _visit_slot(
            o, abi_constant("object.property.fget_offset"), 1, visitor, context
        )
        _visit_slot(
            o, abi_constant("object.property.fset_offset"), 1, visitor, context
        )
        _visit_slot(
            o, abi_constant("object.property.fdel_offset"), 1, visitor, context
        )
        return 1
    if tag == abi_constant("object.type.classmethod"):
        _visit_slot(
            o,
            abi_constant("object.classmethod.func_offset"),
            1,
            visitor,
            context,
        )
        return 1
    if tag == abi_constant("object.type.staticmethod"):
        _visit_slot(
            o,
            abi_constant("object.staticmethod.func_offset"),
            1,
            visitor,
            context,
        )
        return 1
    if tag == abi_constant("object.type.memoryview"):
        _visit_slot(o, 16, 1, visitor, context)
        return 1
    if tag == abi_constant("object.type.thread"):
        _visit_slot(o, 24, 1, visitor, context)
        _visit_slot(o, 32, 1, visitor, context)
        _visit_slot(o, 40, 1, visitor, context)
        return 1
    return 0


@c_abi_export("pcc_gc_object_slots_visit_weakref_slots")
def _visit_weakref_slots(o, visitor, context) -> i64:
    if load_i32(
        o, abi_constant("object.header.type_tag_offset")
    ) != abi_constant("object.type.weakref"):
        return 0
    _visit_slot(o, 16, 3, visitor, context)
    _visit_slot(o, 24, 1, visitor, context)
    return 1


@c_abi_export("pcc_gc_object_slots_visit_continuation_slots")
def _visit_continuation_slots(o, visitor, context) -> i64:
    if load_i32(
        o, abi_constant("object.header.type_tag_offset")
    ) != abi_constant("object.type.continuation"):
        return 0
    chunk = load_ptr(o, 24)
    if ptr_is_null(chunk) == 0:
        slots = load_ptr(chunk, 16)
        if ptr_is_null(slots) == 0:
            count: i64 = load_i64(chunk, 8)
            index: i64 = 0
            while index < count:
                _visit_slot(slots, index * 8, 1, visitor, context)
                index = index + 1
    return 1


@c_abi_export("pcc_gc_object_slots_visit_class_slots")
def _visit_class_slots(o, visitor, context) -> i64:
    if load_i32(
        o, abi_constant("object.header.type_tag_offset")
    ) != abi_constant("object.type.class"):
        return 0
    count: i64 = load_i32(o, abi_constant("object.class.n_bases_offset"))
    slots = load_ptr(o, abi_constant("object.class.bases_offset"))
    index: i64 = 0
    if ptr_is_null(slots) == 0:
        while index < count:
            _visit_slot(
                slots,
                index * abi_constant("object.pointer.size"),
                2,
                visitor,
                context,
            )
            index = index + 1
    count = load_i32(o, abi_constant("object.class.n_mro_offset"))
    slots = load_ptr(o, abi_constant("object.class.mro_offset"))
    index: i64 = 0
    if ptr_is_null(slots) == 0:
        while index < count:
            _visit_slot(
                slots,
                index * abi_constant("object.pointer.size"),
                2,
                visitor,
                context,
            )
            index = index + 1
    count = load_i32(o, abi_constant("object.class.n_methods_offset"))
    slots = load_ptr(o, abi_constant("object.class.methods_offset"))
    index: i64 = 0
    if ptr_is_null(slots) == 0:
        while index < count:
            _visit_slot(
                slots,
                index * abi_constant("object.class_method.size")
                + abi_constant("object.class_method.func_offset"),
                3,
                visitor,
                context,
            )
            index = index + 1
    _visit_slot(
        o,
        abi_constant("object.class.del_method_offset"),
        3,
        visitor,
        context,
    )
    _visit_slot(
        o,
        abi_constant("object.class.attrs_offset"),
        1,
        visitor,
        context,
    )
    _visit_slot(
        o,
        abi_constant("object.class.metaclass_offset"),
        1,
        visitor,
        context,
    )
    return 1


@c_abi_export("pcc_gc_object_slots_visit_instance_slots")
def _visit_instance_slots(o, visitor, context) -> i64:
    tag: i64 = load_i32(o, abi_constant("object.header.type_tag_offset"))
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0
    if (
        tag != abi_constant("object.type.instance")
        and tag != abi_constant("object.type.valuebox")
        and tag < abi_constant("object.type.user_class_start")
    ):
        return 0
    cls = load_ptr(o, abi_constant("object.instance.cls_offset"))
    if ptr_is_null(cls) != 0:
        return 1
    _visit_slot(
        o,
        abi_constant("object.instance.cls_offset"),
        2,
        visitor,
        context,
    )
    cls = load_ptr(o, abi_constant("object.instance.cls_offset"))
    if ptr_is_null(cls) != 0:
        return 1
    # Class instance_size describes every physical owning slot, including
    # the unchanged dynamic owner and appended immutable builtin payloads.
    count: i64 = (load_i32(cls, abi_constant("object.class.instance_size_offset")) - abi_constant("object.instance.fields_offset")) // abi_constant("object.pointer.size")
    minimum: i64 = load_i32(cls, abi_constant("object.class.n_fields_offset")) + 1
    if minimum < 1:
        minimum = 1
    if count < minimum:
        count = minimum
    index: i64 = 0
    while index < count:
        _visit_slot(o, abi_constant("object.instance.fields_offset") + index * abi_constant("object.pointer.size"), 1, visitor, context)
        index = index + 1

    return 1


@c_abi_export("pcc_gc_object_slots_has_no_pointer_slots")
def _has_no_pointer_slots(o) -> i64:
    tag: i64 = load_i32(o, abi_constant("object.header.type_tag_offset"))
    if (
        tag == abi_constant("object.type.none")
        or tag == abi_constant("object.type.bool")
        or tag == abi_constant("object.type.int")
        or tag == abi_constant("object.type.float")
        or tag == abi_constant("object.type.str")
    ):
        return 1
    if (
        tag == abi_constant("object.type.complex")
        or tag == abi_constant("object.type.bytes")
        or tag == abi_constant("object.type.bytearray")
        or tag == abi_constant("object.type.file")
        or tag == abi_constant("object.type.cpy_handle")
    ):
        return 1
    if (
        tag == abi_constant("object.type.thread_lock")
        or tag == abi_constant("object.type.thread_rlock")
        or tag == abi_constant("object.type.thread_event")
        or tag == abi_constant("object.type.thread_condition")
        or tag == abi_constant("object.type.thread_semaphore")
    ):
        return 1
    return 0


@c_abi_export("pcc_gc_visit_object_slots_slice")
def pcc_gc_visit_object_slots_slice(
    o,
    cursor: i64,
    limit: i64,
    visitor,
    context,
    state_out,
) -> i64:
    """Visit a bounded current-layout slice without retaining raw slots."""
    if ptr_is_null(state_out) != 0:
        return 0
    store_i64(state_out, 0, -1)
    store_i64(state_out, 8, 0)
    if (
        ptr_is_null(o) != 0
        or is_tagged_int(o) != 0
        or ptr_is_null(visitor) != 0
        or cursor < 0
        or limit <= 0
    ):
        return 0
    tag: i64 = load_i32(o, abi_constant("object.header.type_tag_offset"))
    if tag < abi_constant("object.type.none"):
        return 0
    if _has_no_pointer_slots(o) != 0:
        return 1
    if pcc_capi_is_cext_type_tag(tag) != 0:
        return 0

    total: i64 = 0
    family: i64 = 0
    if tag == abi_constant("object.type.list"):
        total = load_i64(o, abi_constant("object.list.length_offset"))
        family = 1
    elif tag == abi_constant("object.type.tuple"):
        total = load_i64(o, abi_constant("object.tuple.length_offset"))
        family = 2
    elif tag == abi_constant("object.type.dict"):
        total = load_i64(
            o, abi_constant("object.dict.entries_used_offset")
        ) * 2
        family = 3
    elif tag == abi_constant("object.type.set"):
        total = load_i64(o, 24)
        family = 4
    elif tag == abi_constant("object.type.vthread_channel"):
        kind: i64 = load_i64(
            o, abi_constant("object.vthread_channel.core.kind_offset")
        )
        if kind == 0:
            total = load_i64(
                o, abi_constant("object.vthread_channel.core.capacity_offset")
            )
            if total > 1048576:
                total = 0
            family = 5
        elif kind == 1 or kind == 2:
            total = 1
            family = 6
        else:
            return 1
    elif tag == abi_constant("object.type.func"):
        total = 6
        family = 7
    elif tag == abi_constant("object.type.iter"):
        total = 1
        family = 8
    elif tag == abi_constant("object.type.gen"):
        total = 2
        family = 9
    elif tag == abi_constant("object.type.coroutine"):
        total = 3
        family = 10
    elif tag == abi_constant("object.type.task"):
        total = 3
        family = 11
    elif tag == abi_constant("object.type.virtual_thread"):
        total = 7
        family = 12
    elif tag == abi_constant("object.type.exc"):
        total = 4
        family = 13
    elif tag == abi_constant("object.type.property"):
        total = 3
        family = 14
    elif tag == abi_constant("object.type.classmethod"):
        total = 1
        family = 15
    elif tag == abi_constant("object.type.staticmethod"):
        total = 1
        family = 16
    elif tag == abi_constant("object.type.memoryview"):
        total = 1
        family = 17
    elif tag == abi_constant("object.type.thread"):
        total = 3
        family = 18
    elif tag == abi_constant("object.type.weakref"):
        total = 2
        family = 19
    elif tag == abi_constant("object.type.continuation"):
        chunk = load_ptr(o, 24)
        if ptr_is_null(chunk) == 0 and ptr_is_null(load_ptr(chunk, 16)) == 0:
            total = load_i64(chunk, 8)
        family = 20
    elif tag == abi_constant("object.type.class"):
        n_bases: i64 = load_i32(o, abi_constant("object.class.n_bases_offset"))
        n_mro: i64 = load_i32(o, abi_constant("object.class.n_mro_offset"))
        n_methods: i64 = load_i32(
            o, abi_constant("object.class.n_methods_offset")
        )
        if n_bases < 0:
            n_bases = 0
        if n_mro < 0:
            n_mro = 0
        if n_methods < 0:
            n_methods = 0
        total = n_bases + n_mro + n_methods + 3
        family = 21
    elif (
        tag == abi_constant("object.type.instance")
        or tag == abi_constant("object.type.valuebox")
        or tag >= abi_constant("object.type.user_class_start")
    ):
        cls = load_ptr(o, abi_constant("object.instance.cls_offset"))
        if ptr_is_null(cls) != 0:
            return 1
        if load_i32(
            cls, abi_constant("object.header.type_tag_offset")
        ) != abi_constant("object.type.class"):
            return 1
        n_fields: i64 = load_i32(
            cls, abi_constant("object.class.n_fields_offset")
        )
        if n_fields < 0:
            n_fields = 0
        count: i64 = (load_i32(cls, abi_constant("object.class.instance_size_offset")) - abi_constant("object.instance.fields_offset")) // abi_constant("object.pointer.size")
        if count < n_fields + 1:
            count = n_fields + 1
        total = 1 + count
        family = 22
    else:
        return 0
    if total < 0:
        total = 0

    examined: i64 = 0
    while cursor < total and examined < limit:
        slot_base = o
        slot_offset: i64 = 0
        role: i64 = 1
        present: i64 = 1
        if family == 1:
            slot_base = load_ptr(o, abi_constant("object.list.items_offset"))
            slot_offset = cursor * 8
            if ptr_is_null(slot_base) != 0:
                present = 0
        elif family == 2:
            slot_offset = abi_constant("object.tuple.items_offset") + cursor * 8
        elif family == 3:
            slot_base = load_ptr(o, abi_constant("object.dict.entries_offset"))
            entry_index: i64 = cursor // 2
            entry_offset: i64 = entry_index * abi_constant("object.dict_entry.size")
            if ptr_is_null(slot_base) != 0 or ptr_is_null(
                load_ptr(
                    slot_base,
                    entry_offset + abi_constant("object.dict_entry.key_offset"),
                )
            ) != 0:
                present = 0
            elif cursor % 2 == 0:
                slot_offset = entry_offset + abi_constant(
                    "object.dict_entry.key_offset"
                )
            else:
                slot_offset = entry_offset + abi_constant(
                    "object.dict_entry.value_offset"
                )
        elif family == 4:
            slot_base = load_ptr(o, 40)
            slot_offset = cursor * 16 + 8
            if ptr_is_null(slot_base) != 0:
                present = 0
            else:
                key = load_ptr(slot_base, slot_offset)
                if ptr_is_null(key) != 0 or ptr_eq(
                    key, global_load_ptr("py_set_dummy")
                ) != 0:
                    present = 0
        elif family == 5:
            slot_offset = (
                abi_constant("object.vthread_channel.core.items_offset")
                + cursor * abi_constant("object.pointer.size")
            )
        elif family == 6:
            slot_offset = abi_constant(
                "object.vthread_channel.endpoint.core_offset"
            )
        elif family == 7:
            if cursor == 0:
                slot_offset = 24
            elif cursor == 1:
                slot_offset = 32
            elif cursor == 2:
                slot_offset = 40
            elif cursor == 3:
                slot_offset = 64
            elif cursor == 4:
                slot_offset = 80
            else:
                slot_offset = 88
        elif family == 8:
            slot_offset = 16
        elif family == 9:
            if cursor == 0:
                slot_offset = 24
            else:
                slot_offset = 48
        elif family == 10:
            slot_offset = 32 + cursor * 8
        elif family == 11:
            slot_offset = 16 + cursor * 8
        elif family == 12:
            if cursor == 0:
                slot_offset = 16
            elif cursor == 1:
                slot_offset = 24
            elif cursor == 2:
                slot_offset = 72
            elif cursor == 3:
                slot_offset = 112
            elif cursor == 4:
                slot_offset = abi_constant(
                    "object.virtual_thread.channel_owner_a_offset"
                )
            elif cursor == 5:
                slot_offset = abi_constant(
                    "object.virtual_thread.channel_owner_b_offset"
                )
            else:
                slot_offset = abi_constant(
                    "object.virtual_thread.channel_value_offset"
                )
        elif family == 13:
            slot_offset = 16 + cursor * 8
        elif family == 14:
            if cursor == 0:
                slot_offset = abi_constant("object.property.fget_offset")
            elif cursor == 1:
                slot_offset = abi_constant("object.property.fset_offset")
            else:
                slot_offset = abi_constant("object.property.fdel_offset")
        elif family == 15:
            slot_offset = abi_constant("object.classmethod.func_offset")
        elif family == 16:
            slot_offset = abi_constant("object.staticmethod.func_offset")
        elif family == 17:
            slot_offset = 16
        elif family == 18:
            slot_offset = 24 + cursor * 8
        elif family == 19:
            slot_offset = 16 + cursor * 8
            if cursor == 0:
                role = 3
        elif family == 20:
            chunk = load_ptr(o, 24)
            if ptr_is_null(chunk) != 0:
                present = 0
            else:
                slot_base = load_ptr(chunk, 16)
                slot_offset = cursor * 8
                if ptr_is_null(slot_base) != 0:
                    present = 0
        elif family == 21:
            n_bases = load_i32(o, abi_constant("object.class.n_bases_offset"))
            n_mro = load_i32(o, abi_constant("object.class.n_mro_offset"))
            n_methods = load_i32(
                o, abi_constant("object.class.n_methods_offset")
            )
            if n_bases < 0:
                n_bases = 0
            if n_mro < 0:
                n_mro = 0
            if n_methods < 0:
                n_methods = 0
            if cursor < n_bases:
                slot_base = load_ptr(o, abi_constant("object.class.bases_offset"))
                slot_offset = cursor * 8
                role = 2
            elif cursor < n_bases + n_mro:
                slot_base = load_ptr(o, abi_constant("object.class.mro_offset"))
                slot_offset = (cursor - n_bases) * 8
                role = 2
            elif cursor < n_bases + n_mro + n_methods:
                slot_base = load_ptr(o, abi_constant("object.class.methods_offset"))
                slot_offset = (
                    (cursor - n_bases - n_mro)
                    * abi_constant("object.class_method.size")
                    + abi_constant("object.class_method.func_offset")
                )
                role = 3
            elif cursor == n_bases + n_mro + n_methods:
                slot_offset = abi_constant("object.class.del_method_offset")
                role = 3
            elif cursor == n_bases + n_mro + n_methods + 1:
                slot_offset = abi_constant("object.class.attrs_offset")
            else:
                slot_offset = abi_constant("object.class.metaclass_offset")
                role = 1
            if ptr_is_null(slot_base) != 0:
                present = 0
        else:
            if cursor == 0:
                slot_offset = abi_constant("object.instance.cls_offset")
                role = 2
            else:
                slot_offset = (
                    abi_constant("object.instance.fields_offset")
                    + (cursor - 1) * abi_constant("object.pointer.size")
                )
        if present != 0:
            _visit_slot(slot_base, slot_offset, role, visitor, context)
        cursor = cursor + 1
        examined = examined + 1
    if cursor < total:
        store_i64(state_out, 0, cursor)
    store_i64(state_out, 8, examined)
    return 1


@c_abi_export("pcc_gc_visit_object_slots")
def pcc_gc_visit_object_slots(o, visitor, context) -> i64:
    if ptr_is_null(o) != 0 or is_tagged_int(o) != 0:
        return 0
    state = stack_alloc(16)
    handled: i64 = pcc_gc_visit_object_slots_slice(
        o, 0, 9223372036854775807, visitor, context, state
    )
    if handled != 0:
        return handled
    cext_handled: i64 = pcc_capi_visit_cext_object_slots_i64(
        o, visitor, context
    )
    if cext_handled != 0:
        return cext_handled
    return 0
