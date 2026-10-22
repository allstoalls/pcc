"""One slot-visitor contract for runtime-owned GC roots."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    abi_constant,
    cstr,
    frame_address,
    gc_backend_current,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    free,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    realloc,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    store_ptr,
    unsigned_div_i64,
    write,
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
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_gc_pointer_is_managed = extern("pcc_gc_pointer_is_managed", (c_ptr,), c_int64)
pcc_thread_collect_stopped_frames = extern(
    "pcc_thread_collect_stopped_frames", (c_ptr, c_int64), c_int64
)
pcc_thread_live_count = extern("pcc_thread_live_count", (), c_int64)
pcc_thread_owns_stopped_world = extern("pcc_thread_owns_stopped_world", (), c_int64)
pcc_stop_the_world = extern("pcc_stop_the_world", (), c_int64)
pcc_resume_world = extern("pcc_resume_world", (), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_gc_backend0_mark_root_slot = extern(
    "pcc_gc_backend0_mark_root_slot", (c_ptr, c_int64), c_void
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
    if mode == 6:  # GC0 reachability (frame roots enabled)
        pcc_gc_backend0_mark_root_slot(slot_base, slot_offset)
        return 0
    if mode == 3:  # rewrite
        before = load_ptr(slot_base, slot_offset)
        after = pcc_gc_resolve_root_slot_unlocked(slot_base, slot_offset)
        if ptr_eq(before, after) == 0:
            return 1
    if mode == 5:  # verify: a mapped object that no registered root holds
        value = load_ptr(slot_base, slot_offset)
        if ptr_is_null(value) != 0 or pcc_gc_pointer_is_managed(value) == 0:
            return 0
        _pcc_stack_diff_note_mapped(value)
        if _pcc_registered_roots_hold(value) != 0:
            return 0
        return 1
    return 0


@c_abi_export("pcc_gc_stack_diff_note_mapped")
def _pcc_stack_diff_note_mapped(value) -> None:
    """Remember a mapped managed object for the reverse (coverage) check."""
    count: i64 = load_i64(global_addr("pcc_gc_stack_diff_count"), 0)
    capacity: i64 = load_i64(global_addr("pcc_gc_stack_diff_capacity"), 0)
    values = global_load_ptr("pcc_gc_stack_diff_values")
    if count >= capacity:
        grown: i64 = 256 if capacity == 0 else capacity * 2
        replacement = realloc(values, grown * 8)
        if ptr_is_null(replacement) != 0:
            return
        values = replacement
        global_store_ptr("pcc_gc_stack_diff_values", values)
        store_i64(global_addr("pcc_gc_stack_diff_capacity"), 0, grown)
    store_ptr(values, count * 8, value)
    store_i64(global_addr("pcc_gc_stack_diff_count"), 0, count + 1)


@c_abi_export("pcc_gc_stack_diff_mapped_holds")
def _pcc_stack_diff_mapped_holds(value) -> i64:
    count: i64 = load_i64(global_addr("pcc_gc_stack_diff_count"), 0)
    values = global_load_ptr("pcc_gc_stack_diff_values")
    found: i64 = 0
    index: i64 = 0
    while index < count and found == 0:
        if ptr_eq(load_ptr(values, index * 8), value) != 0:
            found = 1
        index = index + 1
    return found


@c_abi_export("pcc_gc_stack_diff_unmapped_registered")
def _pcc_stack_diff_unmapped_registered(low: i64, high: i64) -> i64:
    """Registered root slots inside this thread's walked stack whose managed
    object no mapped location holds: roots the map would miss."""
    missing: i64 = 0
    node = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(node) == 0:
        slots = load_ptr(node, 8)
        count: i64 = load_i64(node, 40)
        index: i64 = 0
        while index < count:
            address: i64 = ptr_to_int(slots) + index * 8
            if address >= low and address < high:
                value = load_ptr(slots, index * 8)
                if (
                    ptr_is_null(value) == 0
                    and pcc_gc_pointer_is_managed(value) != 0
                    and _pcc_stack_diff_mapped_holds(value) == 0
                ):
                    missing = missing + 1
            index = index + 1
        node = load_ptr(node, 16)
    return missing


@c_abi_export("pcc_gc_stack_diff_registered_in_range")
def _pcc_stack_diff_registered_in_range(low: i64, high: i64) -> i64:
    """Managed objects held by registered root slots inside the walked stack,
    so a zero ``unmapped`` count is known to cover something."""
    held: i64 = 0
    node = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(node) == 0:
        slots = load_ptr(node, 8)
        count: i64 = load_i64(node, 40)
        index: i64 = 0
        while index < count:
            address: i64 = ptr_to_int(slots) + index * 8
            if address >= low and address < high:
                value = load_ptr(slots, index * 8)
                if ptr_is_null(value) == 0 and pcc_gc_pointer_is_managed(value) != 0:
                    held = held + 1
            index = index + 1
        node = load_ptr(node, 16)
    return held


@c_abi_export("pcc_gc_registered_roots_hold")
def _pcc_registered_roots_hold(value) -> i64:
    """Whether some registered frame root slot holds ``value`` (linear scan)."""
    found: i64 = 0
    node = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(node) == 0 and found == 0:
        slots = load_ptr(node, 8)
        count: i64 = load_i64(node, 40)
        index: i64 = 0
        while index < count and found == 0:
            if ptr_eq(load_ptr(slots, index * 8), value) != 0:
                found = 1
            index = index + 1
        node = load_ptr(node, 16)
    return found


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
        65,  # PY_EXC_N_BUILTIN
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


@c_abi_export("pcc_gc_stackmap_roots_mode")
def _pcc_stackmap_roots_mode() -> i64:
    """1 (default): the precise stack walk roots the mapped stack frames of
    every mutator thread whenever the program published a valid table;
    2: the same with a per-visit report (PCC_GC_STACKMAP_ROOTS=report);
    0 (PCC_GC_STACKMAP_ROOTS=0): mapped frames register like the others.
    The mode is read once, before main's first frame enters, so every
    mapped frame enters and leaves under the same answer. Frames of code
    without records use the plain entries and always register."""
    setting: i64 = load_i32(global_addr("pcc_gc_stackmap_roots_setting"), 0)
    if setting < 0:
        setting = 1
        value = pcc_platform_getenv(cstr("PCC_GC_STACKMAP_ROOTS"))
        if ptr_is_null(value) == 0 and load_i8(value, 0) == 48 and load_i8(value, 1) == 0:
            setting = 0
        if ptr_is_null(value) == 0 and load_i8(value, 0) == 114:  # "report"
            setting = 2
        store_i32(global_addr("pcc_gc_stackmap_roots_setting"), 0, setting)
    if setting != 0 and _pcc_stackmap_index_ready() == 0:
        return 0
    return setting


@c_abi_export("pcc_gc_stackmap_visit_stack_roots")
def _pcc_stackmap_visit_stack_roots(mode: i64, resolve: i64, bounds) -> i64:
    """Visit every recorded frame of this thread's stack, from this frame up.

    Stores the walked range [this frame, outermost frame pointer) in bounds.
    """
    return _pcc_stackmap_visit_frames_from(frame_address(), mode, resolve, bounds)


@c_abi_export("pcc_gc_stackmap_visit_frames_from")
def _pcc_stackmap_visit_frames_from(frame, mode: i64, resolve: i64, bounds) -> i64:
    """Visit the recorded callers of ``frame`` up to the outermost frame."""
    low: i64 = ptr_to_int(frame)
    high: i64 = low
    visited: i64 = 0
    frames: i64 = 0
    done: i64 = 0
    while done == 0:
        caller = load_ptr(frame, 0)
        if ptr_is_null(caller) != 0 or frames >= 100000:
            done = 1
        else:
            return_address: i64 = load_i64(frame, 8) & 140737488355327
            result: i64 = pcc_gc_visit_precise_frame(return_address, caller, mode, resolve)
            if result > 0:
                visited = visited + result
            frames = frames + 1
            frame = caller
            high = ptr_to_int(caller)
    store_i64(bounds, 0, low)
    store_i64(bounds, 8, high)
    return visited


@c_abi_export("pcc_gc_visit_walked_stack_roots")
def pcc_gc_visit_walked_stack_roots(mode: i64, resolve: i64) -> i64:
    """Visit the mapped stack frames of every mutator thread with ``mode``.

    0 when no walk is active (no published table, or PCC_GC_STACKMAP_ROOTS=0):
    mapped frames then registered and their owners visit the registry.
    Another mutator's stack is stable only while it is parked or exited, so
    the visit owns a stopped world, stopping it for the walk when needed.
    """
    if _pcc_stackmap_roots_mode() == 0:
        return 0
    stopped_here: i64 = 0
    if pcc_thread_live_count() > 1 and pcc_thread_owns_stopped_world() == 0:
        if pcc_stop_the_world() != 0:
            pcc_platform_abort()
            return 0
        stopped_here = 1
    capacity: i64 = pcc_thread_live_count() + 1
    pairs = malloc(capacity * 16)
    if ptr_is_null(pairs):
        pcc_platform_abort()
        return 0
    others: i64 = 0
    if capacity > 2:
        others = pcc_thread_collect_stopped_frames(pairs, capacity)
        if others < 0:
            # A registered mutator kept running through the stop.
            pcc_platform_abort()
            return 0
    bounds = stack_alloc(16)
    mapped: i64 = _pcc_stackmap_visit_stack_roots(mode, resolve, bounds)
    other_index: i64 = 0
    while other_index < others:
        mapped = mapped + _pcc_stackmap_visit_frames_from(
            load_ptr(pairs, other_index * 16), mode, resolve, bounds
        )
        other_index = other_index + 1
    free(pairs)
    if stopped_here != 0:
        pcc_resume_world()
    return mapped


@c_abi_export("pcc_gc_visit_registered_root_slots")
def pcc_gc_visit_registered_root_slots(mode: i64, resolve: i64) -> i64:
    """Visit every frame root: walked stack frames, then the registry."""
    result: i64 = 0
    stack_mode: i64 = _pcc_stackmap_roots_mode()
    mapped: i64 = pcc_gc_visit_walked_stack_roots(mode, resolve)
    result = result + mapped
    registered: i64 = 0
    # Every registered frame is visited: mapped frames the walk roots never
    # register, and a registered frame (no records, a heap frame) is seen
    # only here.
    frame = global_load_ptr("pcc_gc_frame_head")
    while ptr_is_null(frame) == 0:
        stable_values = null()
        if mode == 2:
            stable_values = load_ptr(frame, 56)
        registered = registered + load_i64(frame, 40)
        result = result + pcc_gc_visit_mapped_root_slots(
            load_i64(frame, 40),
            load_ptr(frame, 8),
            stable_values,
            load_i32(frame, 48) & 1,
            mode,
            resolve,
        )
        frame = load_ptr(frame, 16)
    if stack_mode == 2:
        _pcc_write_text(cstr("pcc-stackmap-roots mode="), 24)
        _pcc_write_decimal(mode)
        _pcc_write_text(cstr(" mapped="), 8)
        _pcc_write_decimal(mapped)
        _pcc_write_text(cstr(" registered="), 12)
        _pcc_write_decimal(registered)
        _pcc_write_text(cstr("\n"), 1)

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


@c_abi_export("pcc_gc_register_precise_stackmap_table")
def pcc_gc_register_precise_stackmap_table(table) -> None:
    """Record the start of the linked stack-map table for the stack walk.

    Only a table-enabled executable's ``main`` calls this, with the
    linker-owned ``pcc_precise_stackmap_table``; otherwise the pointer stays
    null and no walk reads a table.
    """
    global_store_ptr("pcc_gc_precise_stackmap_table", table)


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
        or (mode != 1 and mode != 3 and mode != 5)
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


@c_abi_export("pcc_gc_stackmap_payload_size")
def _pcc_stackmap_payload_size(table) -> i64:
    """Bytes of a linked table: each function header is followed by its records,
    and the interned location table closes the payload."""
    cursor: i64 = abi_constant("stackmap.header_size")
    function_count: i64 = _pcc_stackmap_u32(table, 12)
    index: i64 = 0
    while index < function_count:
        record_count: i64 = _pcc_stackmap_u32(ptr_add(table, cursor), 24)
        cursor = (
            cursor
            + abi_constant("stackmap.function_size")
            + record_count * abi_constant("stackmap.record_size")
        )
        index = index + 1
    return cursor + _pcc_stackmap_u32(table, 16) * abi_constant(
        "stackmap.location_size"
    )


@c_abi_export("pcc_gc_stack_diff_write_text")
def _pcc_write_text(text, length: i64) -> None:
    write(2, text, length)


@c_abi_export("pcc_gc_stack_diff_write_decimal")
def _pcc_write_decimal(value: i64) -> None:
    buffer = stack_alloc(24)
    negative: i64 = 0
    if value < 0:
        negative = 1
        value = 0 - value
    position: i64 = 23
    store_i8(buffer, position, 0)
    if value == 0:
        position = position - 1
        store_i8(buffer, position, 48)
    while value > 0:
        position = position - 1
        store_i8(buffer, position, 48 + value % 10)
        value = value // 10
    if negative != 0:
        position = position - 1
        store_i8(buffer, position, 45)
    _pcc_write_text(ptr_add(buffer, position), 23 - position)


@c_abi_export("pcc_gc_stackmap_index_swap")
def _pcc_stackmap_index_swap(entries, left: i64, right: i64) -> None:
    word: i64 = 0
    while word < 3:
        left_offset: i64 = left * 24 + word * 8
        right_offset: i64 = right * 24 + word * 8
        value: i64 = load_i64(entries, left_offset)
        store_i64(entries, left_offset, load_i64(entries, right_offset))
        store_i64(entries, right_offset, value)
        word = word + 1


@c_abi_export("pcc_gc_stackmap_index_sift")
def _pcc_stackmap_index_sift(entries, start: i64, end: i64) -> None:
    root: i64 = start
    done: i64 = 0
    while done == 0:
        child: i64 = root * 2 + 1
        if child >= end:
            done = 1
        else:
            if child + 1 < end and load_i64(entries, (child + 1) * 24) > load_i64(entries, child * 24):
                child = child + 1
            if load_i64(entries, root * 24) < load_i64(entries, child * 24):
                _pcc_stackmap_index_swap(entries, root, child)
                root = child
            else:
                done = 1


@c_abi_export("pcc_gc_stackmap_runtime_table_valid")
def _pcc_stackmap_runtime_table_valid(table, arch: i64) -> i64:
    """Check a version 3 runtime table once, before it is indexed.

    Covers everything a frame visit reads: counts, function record ranges,
    strictly increasing pcs inside the code, live-set bounds, and every slot a
    record roots against its own function's frame size. Backend0/1/2 do not
    read the table, as with ``pcc_gc_consume_precise_stackmap``.
    """
    backend: i64 = gc_backend_current()
    if backend != 3 and backend != 4:
        return 0
    if load_i64(table, 0) != abi_constant("stackmap.magic_i64"):
        return 0
    version_arch_pointer: i64 = _pcc_stackmap_u32(table, 8)
    if (
        (version_arch_pointer & 65535) != 3
        or ((version_arch_pointer >> 16) & 255) != arch
        or (arch != 1 and arch != 2)
        or ((version_arch_pointer >> 24) & 255) != 8
    ):
        return 0
    function_count: i64 = _pcc_stackmap_u32(table, 12)
    record_count: i64 = _pcc_stackmap_u32(table, 16)
    live_set_count: i64 = _pcc_stackmap_u32(table, 20)
    location_count: i64 = _pcc_stackmap_u32(table, 24)
    rare_count: i64 = _pcc_stackmap_u32(table, 28)
    if (
        _pcc_stackmap_u32(table, 32) != 0
        or _pcc_stackmap_u32(table, 36) != 0
        or function_count > 1000000
        or record_count > 128000000
        or location_count > 128000000
        or live_set_count > record_count
        or rare_count > record_count
    ):
        return 0
    records_base: i64 = (
        abi_constant("stackmap.runtime.header_size")
        + function_count * abi_constant("stackmap.runtime.function_size")
    )
    live_sets_base: i64 = records_base + record_count * abi_constant("stackmap.runtime.record_size")
    locations_base: i64 = live_sets_base + live_set_count * abi_constant("stackmap.runtime.live_set_size")
    rare_base: i64 = locations_base + location_count * abi_constant("stackmap.runtime.location_size")
    live_set: i64 = 0
    while live_set < live_set_count:
        entry: i64 = live_sets_base + live_set * abi_constant("stackmap.runtime.live_set_size")
        if _pcc_stackmap_u32(table, entry) + _pcc_stackmap_u32(table, entry + 4) > location_count:
            return 0
        live_set = live_set + 1
    next_record: i64 = 0
    function_index: i64 = 0
    while function_index < function_count:
        function = ptr_add(
            table,
            abi_constant("stackmap.runtime.header_size")
            + function_index * abi_constant("stackmap.runtime.function_size"),
        )
        code_size: i64 = _pcc_stackmap_u32(function, 16)
        frame_size: i64 = _pcc_stackmap_u32(function, 20)
        first_record: i64 = _pcc_stackmap_u32(function, 24)
        function_records: i64 = _pcc_stackmap_u32(function, 28)
        if (
            load_i64(function, 8) == 0
            or code_size <= 0
            or (frame_size & 15) != 0
            or first_record != next_record
            or function_records > record_count - first_record
        ):
            return 0
        previous_pc: i64 = -1
        record_index: i64 = first_record
        while record_index < first_record + function_records:
            record: i64 = records_base + record_index * abi_constant("stackmap.runtime.record_size")
            pc: i64 = _pcc_stackmap_u32(table, record)
            record_live_set: i64 = _pcc_stackmap_u32(table, record + 4)
            if pc <= previous_pc or pc >= code_size or record_live_set >= live_set_count:
                return 0
            previous_pc = pc
            entry: i64 = live_sets_base + record_live_set * abi_constant("stackmap.runtime.live_set_size")
            location: i64 = _pcc_stackmap_u32(table, entry)
            location_end: i64 = location + _pcc_stackmap_u32(table, entry + 4)
            while location < location_end:
                word: i64 = load_i32(
                    table, locations_base + location * abi_constant("stackmap.runtime.location_size")
                )
                offset: i64 = word - (word & 1)
                if offset >= 0 or ((0 - offset) & 7) != 0 or 0 - offset > frame_size:
                    return 0
                location = location + 1
            record_index = record_index + 1
        next_record = first_record + function_records
        function_index = function_index + 1
    if next_record != record_count:
        return 0
    previous_rare: i64 = -1
    rare_index: i64 = 0
    while rare_index < rare_count:
        rare: i64 = rare_base + rare_index * abi_constant("stackmap.runtime.rare_size")
        rare_record: i64 = _pcc_stackmap_u32(table, rare)
        if rare_record <= previous_rare or rare_record >= record_count or (_pcc_stackmap_u32(table, rare + 4) >> 16) != 0:
            return 0
        previous_rare = rare_record
        rare_index = rare_index + 1
    return 1


@c_abi_export("pcc_gc_stackmap_visit_runtime_record")
def _pcc_stackmap_visit_runtime_record(
    table, function, target: i64, frame_pointer, mode: i64, resolve: i64
) -> i64:
    """Visit the version 3 record at code offset ``target`` of ``function``.

    A function's records are sorted by pc, so the record is found by binary
    search; its live set names the slots, each an int32 frame offset with the
    owned bit in bit 0. -13 when no record has that pc.
    """
    records_base: i64 = (
        abi_constant("stackmap.runtime.header_size")
        + _pcc_stackmap_u32(table, 12) * abi_constant("stackmap.runtime.function_size")
    )
    live_sets_base: i64 = records_base + _pcc_stackmap_u32(table, 16) * abi_constant(
        "stackmap.runtime.record_size"
    )
    locations_base: i64 = live_sets_base + _pcc_stackmap_u32(table, 20) * abi_constant(
        "stackmap.runtime.live_set_size"
    )
    low: i64 = _pcc_stackmap_u32(function, 24)
    end: i64 = low + _pcc_stackmap_u32(function, 28)
    high: i64 = end
    while low < high:
        middle: i64 = (low + high) // 2
        if _pcc_stackmap_u32(table, records_base + middle * abi_constant("stackmap.runtime.record_size")) < target:
            low = middle + 1
        else:
            high = middle
    record: i64 = records_base + low * abi_constant("stackmap.runtime.record_size")
    if low >= end or _pcc_stackmap_u32(table, record) != target:
        return -13
    entry: i64 = live_sets_base + _pcc_stackmap_u32(table, record + 4) * abi_constant(
        "stackmap.runtime.live_set_size"
    )
    location: i64 = _pcc_stackmap_u32(table, entry)
    location_end: i64 = location + _pcc_stackmap_u32(table, entry + 4)
    result: i64 = 0
    while location < location_end:
        word: i64 = load_i32(
            table, locations_base + location * abi_constant("stackmap.runtime.location_size")
        )
        owned: i64 = word & 1
        result = result + pcc_gc_visit_mapped_root_slot(
            frame_pointer, word - owned, null(), 1 - owned, mode, resolve
        )
        if mode == 1:
            result = result + 1
        location = location + 1
    return result


@c_abi_export("pcc_gc_stackmap_index_ready")
def _pcc_stackmap_index_ready() -> i64:
    """Validate the published table once and index its functions by address.

    Each index entry is (code start, code end, payload offset of the function
    header), sorted by start, so a return address finds its function by binary
    search instead of a scan of every function and record.
    """
    count: i64 = load_i64(global_addr("pcc_gc_stackmap_index_count"), 0)
    if count != 0:
        return 1 if count > 0 else 0
    table = global_load_ptr("pcc_gc_precise_stackmap_table")
    if ptr_is_null(table) != 0:
        return 0
    runtime_table: i64 = 0
    if (_pcc_stackmap_u32(table, 8) & 65535) == 3:
        runtime_table = 1
    arch: i64 = (_pcc_stackmap_u32(table, 8) >> 16) & 255
    function_count: i64 = _pcc_stackmap_u32(table, 12)
    valid: i64 = 0
    if function_count > 0 and runtime_table != 0:
        valid = _pcc_stackmap_runtime_table_valid(table, arch)
    if function_count > 0 and runtime_table == 0:
        # The consumer checks every function, record and location before it
        # looks for a pc; no function starts at 0, so "no record" (-13) is a
        # valid version 2 table.
        size: i64 = _pcc_stackmap_payload_size(table)
        if pcc_gc_consume_precise_stackmap(table, size, 0, table, arch, 1) == -13:
            valid = 1
            store_i64(
                global_addr("pcc_gc_stackmap_location_base"),
                0,
                size - _pcc_stackmap_u32(table, 16) * abi_constant("stackmap.location_size"),
            )
    entries = null()
    if valid != 0:
        entries = malloc(function_count * 24)
    if ptr_is_null(entries) != 0:
        store_i64(global_addr("pcc_gc_stackmap_index_count"), 0, -1)
        return 0
    cursor: i64 = abi_constant("stackmap.header_size")
    if runtime_table != 0:
        cursor = abi_constant("stackmap.runtime.header_size")
    index: i64 = 0
    while index < function_count:
        function = ptr_add(table, cursor)
        start: i64 = load_i64(function, 8)
        store_i64(entries, index * 24, start)
        store_i64(entries, index * 24 + 8, start + _pcc_stackmap_u32(function, 16))
        store_i64(entries, index * 24 + 16, cursor)
        if runtime_table != 0:
            cursor = cursor + abi_constant("stackmap.runtime.function_size")
        else:
            cursor = (
                cursor
                + abi_constant("stackmap.function_size")
                + _pcc_stackmap_u32(function, 24) * abi_constant("stackmap.record_size")
            )
        index = index + 1
    heap_start: i64 = function_count // 2 - 1
    while heap_start >= 0:
        _pcc_stackmap_index_sift(entries, heap_start, function_count)
        heap_start = heap_start - 1
    heap_end: i64 = function_count - 1
    while heap_end > 0:
        _pcc_stackmap_index_swap(entries, 0, heap_end)
        _pcc_stackmap_index_sift(entries, 0, heap_end)
        heap_end = heap_end - 1
    global_store_ptr("pcc_gc_stackmap_index", entries)
    store_i64(global_addr("pcc_gc_stackmap_index_count"), 0, function_count)
    return 1


@c_abi_export("pcc_gc_stackmap_find_function")
def _pcc_stackmap_find_function(program_counter: i64) -> i64:
    """Payload offset of the function whose code holds the pc, or -1."""
    entries = global_load_ptr("pcc_gc_stackmap_index")
    low: i64 = 0
    high: i64 = load_i64(global_addr("pcc_gc_stackmap_index_count"), 0)
    while low < high:
        middle: i64 = (low + high) // 2
        if load_i64(entries, middle * 24) <= program_counter:
            low = middle + 1
        else:
            high = middle
    if low == 0:
        return -1
    found: i64 = low - 1
    if program_counter >= load_i64(entries, found * 24 + 8):
        return -1
    return load_i64(entries, found * 24 + 16)


@c_abi_export("pcc_gc_visit_precise_frame")
def pcc_gc_visit_precise_frame(
    program_counter: i64, frame_pointer, mode: i64, resolve: i64
) -> i64:
    """Visit the frame's call record found through the address index.

    Same visits and result as ``pcc_gc_consume_precise_stackmap`` for that
    record, without revalidating the whole table on each frame. -13 when no
    function or record covers the pc, -1 when no valid table is published.
    """
    if _pcc_stackmap_index_ready() == 0:
        return -1
    function_offset: i64 = _pcc_stackmap_find_function(program_counter)
    if function_offset < 0:
        return -13
    table = global_load_ptr("pcc_gc_precise_stackmap_table")
    function = ptr_add(table, function_offset)
    target: i64 = program_counter - load_i64(function, 8)
    if (_pcc_stackmap_u32(table, 8) & 65535) == 3:
        return _pcc_stackmap_visit_runtime_record(
            table, function, target, frame_pointer, mode, resolve
        )
    record_count: i64 = _pcc_stackmap_u32(function, 24)
    record_index: i64 = 0
    record = null()
    while record_index < record_count and ptr_is_null(record) != 0:
        candidate = ptr_add(
            function,
            abi_constant("stackmap.function_size")
            + record_index * abi_constant("stackmap.record_size"),
        )
        if _pcc_stackmap_u32(candidate, 8) == target:
            record = candidate
        record_index = record_index + 1
    if ptr_is_null(record) != 0:
        return -13
    location_count: i64 = _pcc_stackmap_u32(record, 20) & 65535
    locations = ptr_add(
        table,
        load_i64(global_addr("pcc_gc_stackmap_location_base"), 0)
        + _pcc_stackmap_u32(record, 28) * abi_constant("stackmap.location_size"),
    )
    result: i64 = 0
    location_index: i64 = 0
    while location_index < location_count:
        location = ptr_add(locations, location_index * abi_constant("stackmap.location_size"))
        flags: i64 = (_pcc_stackmap_u32(location, 0) >> 8) & 255
        borrowed: i64 = 1
        if (flags & abi_constant("stackmap.location.owned")) != 0:
            borrowed = 0
        result = result + pcc_gc_visit_mapped_root_slot(
            frame_pointer, load_i32(location, 8), null(), borrowed, mode, resolve
        )
        if mode == 1:
            result = result + 1
        location_index = location_index + 1
    return result


@c_abi_export("pcc_gc_precise_stack_diff")
def pcc_gc_precise_stack_diff(report: i64) -> i64:
    """Compare this thread's stack-mapped roots with the registered slots.

    Walks the frame-pointer chain from this frame: each frame record holds
    the caller's frame pointer and the return address the caller's call
    safepoint is recorded at. For every frame with a record, counts mapped
    managed objects that no registered frame root holds (consumer mode 5).
    Frames without a record (foreign code) are skipped. Returns the
    mismatch count plus consumer errors, or -1 without a published table.
    """
    if _pcc_stackmap_index_ready() == 0:
        return -1
    store_i64(global_addr("pcc_gc_stack_diff_count"), 0, 0)
    frame = frame_address()
    low: i64 = ptr_to_int(frame)
    high: i64 = low
    frames: i64 = 0
    mapped: i64 = 0
    mismatches: i64 = 0
    errors: i64 = 0
    done: i64 = 0
    while done == 0:
        caller = load_ptr(frame, 0)
        if ptr_is_null(caller) != 0 or frames >= 100000:
            done = 1
        else:
            # The saved return address may carry a pointer-authentication
            # code above the user address bits.
            return_address: i64 = load_i64(frame, 8) & 140737488355327
            result: i64 = pcc_gc_visit_precise_frame(return_address, caller, 5, 0)
            if result >= 0:
                mapped = mapped + 1
                mismatches = mismatches + result
            elif result != -13:
                errors = errors + 1
            frames = frames + 1
            frame = caller
            high = ptr_to_int(caller)
    unmapped: i64 = _pcc_stack_diff_unmapped_registered(low, high)
    registered: i64 = _pcc_stack_diff_registered_in_range(low, high)
    if report != 0:
        _pcc_write_text(cstr("pcc-stackmap-diff frames="), 25)
        _pcc_write_decimal(frames)
        _pcc_write_text(cstr(" mapped="), 8)
        _pcc_write_decimal(mapped)
        _pcc_write_text(cstr(" mismatches="), 12)
        _pcc_write_decimal(mismatches)
        _pcc_write_text(cstr(" errors="), 8)
        _pcc_write_decimal(errors)
        _pcc_write_text(cstr(" registered="), 12)
        _pcc_write_decimal(registered)
        _pcc_write_text(cstr(" unmapped="), 10)
        _pcc_write_decimal(unmapped)
        _pcc_write_text(cstr("\n"), 1)
    return mismatches + errors + unmapped


@c_abi_export("pcc_gc_precise_stack_diff_if_requested")
def pcc_gc_precise_stack_diff_if_requested() -> None:
    """Run and report the differential walk when PCC_GC_STACKMAP_DIFF is set
    and the executable published a table."""
    if ptr_is_null(global_load_ptr("pcc_gc_precise_stackmap_table")) != 0:
        return
    if ptr_is_null(pcc_platform_getenv(cstr("PCC_GC_STACKMAP_DIFF"))) != 0:
        return
    pcc_gc_precise_stack_diff(1)


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
