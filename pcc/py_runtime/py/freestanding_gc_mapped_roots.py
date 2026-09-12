"""One slot-visitor contract for runtime-owned GC roots."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    gc_backend_current,
    global_addr,
    global_load_ptr,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    unsigned_div_i64,
)


__pcc_freestanding__ = True


pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern(
    "pcc_py_gc_minor_graph_unlock", (), c_void
)
pcc_gc_root_slot_count_from_map = extern(
    "pcc_gc_root_slot_count_from_map", (c_ptr,), c_int64
)
pcc_gc_root_map_is_borrowed = extern(
    "pcc_gc_root_map_is_borrowed", (c_ptr,), c_int64
)
pcc_gc_mark_root_gray_if_known = extern(
    "pcc_gc_mark_root_gray_if_known", (c_ptr,), c_void
)
pcc_gc_promote_cached_frame_slot = extern("pcc_gc_promote_cached_frame_slot", (c_ptr, c_int64, c_ptr, c_int64), c_void)
pcc_gc_resolve_root_slot_unlocked = extern(
    "pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr
)
py_subs_exc_cache_slot = extern(
    "py_subs_exc_cache_slot", (c_int64,), c_ptr
)


@c_abi_export("pcc_gc_visit_mapped_root_slot")
def pcc_gc_visit_mapped_root_slot(
    slot_base,
    slot_offset: i64,
    stable_base,
    borrowed: i64,
    mode: i64,
    resolve: i64,
) -> i64:
    if mode == 1:  # gray
        root = load_ptr(slot_base, slot_offset)
        if resolve != 0:
            root = pcc_gc_resolve_root_slot_unlocked(slot_base, slot_offset)
        pcc_gc_mark_root_gray_if_known(root)
        return 0
    if mode == 2:  # promote
        pcc_gc_promote_cached_frame_slot(
            slot_base, slot_offset, stable_base, borrowed
        )
        return 0
    if mode == 3:  # rewrite
        before = load_ptr(slot_base, slot_offset)
        after = pcc_gc_resolve_root_slot_unlocked(slot_base, slot_offset)
        if ptr_eq(before, after) == 0:
            return 1
    return 0


@c_abi_export("pcc_gc_visit_mapped_root_slots")
def pcc_gc_visit_mapped_root_slots(
    root_count: i64,
    root_slots,
    stable_values,
    borrowed: i64,
    mode: i64,
    resolve: i64,
) -> i64:
    if root_count <= 0 or ptr_is_null(root_slots) != 0:
        return 0
    result: i64 = 0
    index: i64 = 0
    while index < root_count:
        slot_result: i64 = pcc_gc_visit_mapped_root_slot(
            root_slots,
            index * 8,
            stable_values,
            borrowed,
            mode,
            resolve,
        )
        if mode == 3:
            result = result + slot_result
        index = index + 1
    if mode == 3:
        return result
    return root_count


@c_abi_export("pcc_gc_visit_scheduler_root_slots")
def pcc_gc_visit_scheduler_root_slots(mode: i64, resolve: i64) -> i64:
    result: i64 = 0
    node = global_load_ptr("pcc_gc_scheduler_root_head")
    while ptr_is_null(node) == 0:
        slot = load_ptr(node, 0)
        if ptr_is_null(slot) == 0:
            slot_result: i64 = pcc_gc_visit_mapped_root_slot(
                slot, 0, null(), 0, mode, resolve
            )
            if mode == 3:
                result = result + slot_result
            else:
                result = result + 1
        node = load_ptr(node, 8)
    return result


@c_abi_export("pcc_gc_visit_builtin_exception_cache_slots")
def pcc_gc_visit_builtin_exception_cache_slots(mode: i64, resolve: i64) -> i64:
    result: i64 = pcc_gc_visit_mapped_root_slots(
        34,
        py_subs_exc_cache_slot(0),
        null(),
        0,
        mode,
        resolve,
    )
    index: i64 = 0
    slot = load_ptr(global_addr("pcc_builtin_type_root_slots"), index * 8)
    while ptr_is_null(slot) == 0:
        visited: i64 = pcc_gc_visit_mapped_root_slot(slot, 0, null(), 0, mode, resolve)
        result = result + (visited if mode == 3 else 1)
        index = index + 1
        slot = load_ptr(global_addr("pcc_builtin_type_root_slots"), index * 8)
    return result


@c_abi_export("pcc_gc_visit_registered_root_slots")
def pcc_gc_visit_registered_root_slots(mode: i64, resolve: i64) -> i64:
    result: i64 = 0
    frame = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(frame) == 0:
        stable_values = null()
        if mode == 2:
            stable_values = load_ptr(frame, 56)
        result = result + pcc_gc_visit_mapped_root_slots(
            load_i64(frame, 40),
            load_ptr(frame, 8),
            stable_values,
            load_i32(frame, 48) & 1,
            mode,
            resolve,
        )
        frame = load_ptr(frame, 16)

    continuation = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(continuation) == 0:
        stable_values = null()
        if mode == 2:
            stable_values = load_ptr(continuation, 40)
        result = result + pcc_gc_visit_mapped_root_slots(
            load_i64(continuation, 24),
            load_ptr(continuation, 8),
            stable_values,
            load_i32(continuation, 32),
            mode,
            resolve,
        )
        continuation = load_ptr(continuation, 16)

    result = result + pcc_gc_visit_scheduler_root_slots(mode, resolve)
    result = result + pcc_gc_visit_builtin_exception_cache_slots(mode, resolve)
    return result


@c_abi_export("pcc_gc_gray_mapped_roots")
def pcc_gc_gray_mapped_roots(frame_map, root_slots, resolve: i64) -> i64:
    return pcc_gc_visit_mapped_root_slots(
        pcc_gc_root_slot_count_from_map(frame_map),
        root_slots,
        null(),
        pcc_gc_root_map_is_borrowed(frame_map),
        1,
        resolve,
    )


@c_abi_export("pcc_gc_rewrite_mapped_roots")
def pcc_gc_rewrite_mapped_roots(frame_map, root_slots) -> i64:
    return pcc_gc_visit_mapped_root_slots(
        pcc_gc_root_slot_count_from_map(frame_map),
        root_slots,
        null(),
        pcc_gc_root_map_is_borrowed(frame_map),
        3,
        0,
    )


# pcc precise-stackmap v1.  The raw values live in the compiler-owned
# freestanding ABI table so the producer and this freestanding consumer cannot
# drift.  No host-side codec or runtime module global is needed here.


@c_abi_export("pcc_gc_stackmap_u32")
def _pcc_stackmap_u32(base, offset: i64) -> i64:
    return load_i32(base, offset) & 0xFFFFFFFF


@c_abi_export("pcc_gc_stackmap_range_fits")
def _pcc_stackmap_range_fits(
    offset: i64, count: i64, stride: i64, payload_size: i64
) -> i64:
    if offset < 0 or count < 0 or stride <= 0 or payload_size < 0:
        return 0
    if offset > payload_size:
        return 0
    if count > unsigned_div_i64(payload_size - offset, stride):
        return 0
    return 1


@c_abi_export("pcc_gc_stackmap_u64_strictly_after")
def _pcc_stackmap_u64_strictly_after(
    current: i64, previous: i64, have_previous: i64
) -> i64:
    """Compare signed i64 loads as the ABI's little-endian uint64 values."""
    if current == 0:
        return 0
    if have_previous == 0:
        return 1
    if previous >= 0:
        if current < 0:
            return 1
        return 1 if current > previous else 0
    if current >= 0:
        return 0
    return 1 if current > previous else 0


@c_abi_export("pcc_gc_stackmap_validate_location")
def _pcc_stackmap_validate_location(
    location,
    frame_size: i64,
    expected_frame_register: i64,
) -> i64:
    kind_flags_size: i64 = _pcc_stackmap_u32(location, 0)
    kind: i64 = kind_flags_size & 255
    flags: i64 = (kind_flags_size >> 8) & 255
    size: i64 = (kind_flags_size >> 16) & 65535
    register_base: i64 = _pcc_stackmap_u32(location, 4)
    register: i64 = register_base & 65535
    base_index: i64 = (register_base >> 16) & 65535
    stack_offset: i64 = load_i32(location, 8)
    extent: i64 = _pcc_stackmap_u32(location, 12)
    if (
        kind != abi_constant("stackmap.location.stack_indirect")
        or (flags & abi_constant("stackmap.location.managed")) == 0
        or (flags & ~3) != 0
        or size != 8
        or extent != 8
        or register != expected_frame_register
        or base_index != 65535
        or stack_offset >= 0
        or -stack_offset > frame_size
        or ((-stack_offset) & 7) != 0
    ):
        return 0
    return 1


@c_abi_export("pcc_gc_consume_precise_stackmap")
def pcc_gc_consume_precise_stackmap(
    payload,
    payload_size: i64,
    program_counter: i64,
    frame_pointer,
    expected_arch: i64,
    mode: i64,
) -> i64:
    """Validate one table and visit the exact record for ``program_counter``.

    This is the backend3/4 differential-consumer boundary.  The normal root
    walk remains the registered-slot protocol until identity gates prove the
    two sources equal.  backend0/1/2 return without reading or changing a slot.

    ``mode`` shares ``pcc_gc_visit_mapped_root_slot``: 1 marks, 3 rewrites.
    A negative result is a fail-closed ABI/PC error; a non-negative result is
    the number of locations visited (or rewritten for mode 3).
    """
    backend: i64 = gc_backend_current()
    if backend != 3 and backend != 4:
        return 0
    if (
        ptr_is_null(payload) != 0
        or ptr_is_null(frame_pointer) != 0
        or payload_size < abi_constant("stackmap.header_size")
        or (expected_arch != 1 and expected_arch != 2)
        or (mode != 1 and mode != 3)
    ):
        return -1
    if load_i64(payload, 0) != abi_constant("stackmap.magic_i64"):
        return -2
    version_arch_pointer: i64 = _pcc_stackmap_u32(payload, 8)
    if (
        (version_arch_pointer & 65535) != 2
        or ((version_arch_pointer >> 16) & 255) != expected_arch
        or ((version_arch_pointer >> 24) & 255) != 8
    ):
        return -3
    function_count: i64 = _pcc_stackmap_u32(payload, 12)
    if function_count > 1000000:
        return -4
    # v2: every record names a slice of one interned location table that
    # follows the last function, instead of carrying locations inline.
    table_count: i64 = _pcc_stackmap_u32(payload, 16)
    if table_count > 128000000 or _pcc_stackmap_u32(payload, 20) != 0:
        return -4

    # The interned table is the payload's tail, so its base is known before
    # the walk and each record can be checked as it is read.
    table_base: i64 = payload_size - table_count * abi_constant(
        "stackmap.location_size"
    )
    if table_base < abi_constant("stackmap.header_size"):
        return -9
    cursor: i64 = abi_constant("stackmap.header_size")
    target_location_index: i64 = 0
    function_index: i64 = 0
    previous_function_id: i64 = 0
    have_previous_function: i64 = 0
    target_record = null()
    target_frame_size: i64 = 0
    target_location_count: i64 = 0
    while function_index < function_count:
        if _pcc_stackmap_range_fits(
            cursor, 1, abi_constant("stackmap.function_size"), payload_size
        ) == 0:
            return -5
        function = ptr_add(payload, cursor)
        current_function_id: i64 = load_i64(function, 0)
        function_address: i64 = load_i64(function, 8)
        code_size: i64 = _pcc_stackmap_u32(function, 16)
        frame_size: i64 = _pcc_stackmap_u32(function, 20)
        record_count: i64 = _pcc_stackmap_u32(function, 24)
        function_flags: i64 = _pcc_stackmap_u32(function, 28)
        if (
            _pcc_stackmap_u64_strictly_after(
                current_function_id,
                previous_function_id,
                have_previous_function,
            ) == 0
            or function_address == 0
            or code_size <= 0
            or (frame_size & 15) != 0
            or function_flags != 0
        ):
            return -6
        previous_function_id = current_function_id
        have_previous_function: i64 = 1
        cursor = cursor + abi_constant("stackmap.function_size")
        previous_instruction_offset: i64 = -1
        record_index: i64 = 0
        while record_index < record_count:
            if _pcc_stackmap_range_fits(
                cursor, 1, abi_constant("stackmap.record_size"), payload_size
            ) == 0:
                return -7
            record = ptr_add(payload, cursor)
            instruction_offset: i64 = _pcc_stackmap_u32(record, 8)
            exceptional_offset: i64 = _pcc_stackmap_u32(record, 12)
            continuation_id: i64 = _pcc_stackmap_u32(record, 16)
            count_reserved: i64 = _pcc_stackmap_u32(record, 20)
            location_count: i64 = count_reserved & 65535
            record_shape: i64 = _pcc_stackmap_u32(record, 24)
            record_location_index: i64 = _pcc_stackmap_u32(record, 28)
            kind: i64 = record_shape & 255
            flags: i64 = (record_shape >> 8) & 255
            if (
                load_i64(record, 0) == 0
                or record_location_index + location_count > table_count
                or (count_reserved >> 16) != 0
                or (record_shape >> 16) != 0
                or instruction_offset <= previous_instruction_offset
                or instruction_offset >= code_size
                or kind < 1
                or kind > 5
                or (flags & ~3) != 0
                or (
                    exceptional_offset != abi_constant("stackmap.no_offset")
                    and exceptional_offset >= code_size
                )
                or (
                    ((flags & 1) != 0)
                    != (
                        exceptional_offset
                        != abi_constant("stackmap.no_offset")
                    )
                )
                or ((kind == 5) != (continuation_id != 0))
                or (((flags & 2) != 0) != (kind == 5))
            ):
                return -8
            previous_instruction_offset = instruction_offset
            cursor = cursor + abi_constant("stackmap.record_size")
            # Validate this record's slice of the shared table against THIS
            # function's frame size.  The table is shared across functions
            # with different frames, so it must be checked per referencing
            # record, exactly as the inline v1 locations were.
            location_position: i64 = 0
            while location_position < location_count:
                location = ptr_add(
                    payload,
                    table_base
                    + (record_location_index + location_position)
                    * abi_constant("stackmap.location_size"),
                )
                expected_frame_register: i64 = 29
                if expected_arch == 2:
                    expected_frame_register: i64 = 6
                if _pcc_stackmap_validate_location(
                    location, frame_size, expected_frame_register
                ) == 0:
                    return -10
                location_position = location_position + 1
            if program_counter == function_address + instruction_offset:
                if ptr_is_null(target_record) == 0:
                    return -11
                target_record = record
                target_frame_size = frame_size
                target_location_count = location_count
                target_location_index = record_location_index
            record_index = record_index + 1
        function_index = function_index + 1
    if cursor != table_base:
        return -12
    if ptr_is_null(target_record) != 0:
        return -13

    result: i64 = 0
    # v2: the matched record's locations live in the shared table, not
    # immediately after the record.
    locations = ptr_add(
        payload,
        table_base
        + target_location_index * abi_constant("stackmap.location_size"),
    )
    location_index: i64 = 0
    while location_index < target_location_count:
        location = ptr_add(
            locations,
            location_index * abi_constant("stackmap.location_size"),
        )
        if _pcc_stackmap_validate_location(
            location,
            target_frame_size,
            29 if expected_arch == 1 else 6,
        ) == 0:
            return -14
        flags: i64 = (_pcc_stackmap_u32(location, 0) >> 8) & 255
        borrowed: i64 = 1
        if (flags & abi_constant("stackmap.location.owned")) != 0:
            borrowed: i64 = 0
        result = result + pcc_gc_visit_mapped_root_slot(
            frame_pointer,
            load_i32(location, 8),
            null(),
            borrowed,
            mode,
            0,
        )
        if mode == 1:
            result = result + 1
        location_index = location_index + 1
    return result


@c_abi_export("pcc_gc_trace_continuation_roots")
def pcc_gc_trace_continuation_roots() -> i64:
    gc_backend_current()
    traced: i64 = 0
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(node) == 0:
        traced = traced + pcc_gc_gray_mapped_roots(
            load_ptr(node, 0), load_ptr(node, 8), 0
        )
        node = load_ptr(node, 16)
    pcc_py_gc_minor_graph_unlock()
    return traced


@c_abi_export("pcc_gc_rewrite_continuation_roots")
def pcc_gc_rewrite_continuation_roots() -> i64:
    gc_backend_current()
    rewritten: i64 = 0
    pcc_py_gc_minor_graph_lock()
    node = global_load_ptr("pcc_gc_continuation_root_head")
    while ptr_is_null(node) == 0:
        rewritten = rewritten + pcc_gc_rewrite_mapped_roots(
            load_ptr(node, 0), load_ptr(node, 8)
        )
        node = load_ptr(node, 16)
    pcc_py_gc_minor_graph_unlock()
    return rewritten
