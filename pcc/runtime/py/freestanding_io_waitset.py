"""Freestanding pcc-Python virtual-thread IO waitset."""

from pcc import i64
from pcc.extern import c_abi_export, c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    clock_gettime,
    define_global_i64,
    close,
    cstr,
    epoll_create1,
    epoll_ctl,
    epoll_wait,
    eventfd_create,
    frame_address,
    free,
    kevent_call,
    kqueue_create,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    logical_shift_right_i64,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    read,
    realloc,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    target_platform_machine,
    target_sys_platform,
    unsigned_div_i64,
    unsigned_rem_i64,
    write,
)
define_global_i64("pcc_io_outcome_ok", 0)
define_global_i64("pcc_io_outcome_more", 1)
define_global_i64("pcc_io_outcome_wouldblock", 2)




__pcc_freestanding__ = True


pcc_thread_blocking_enter = extern("pcc_thread_blocking_enter", (c_ptr,), c_int64)
pcc_thread_blocking_leave = extern("pcc_thread_blocking_leave", (c_int64,), c_void)


@c_abi_export("pcc_io_waitset_platform_is_darwin")
def _is_darwin() -> i64:
    platform = target_sys_platform()
    expected = cstr("darwin")
    index: i64 = 0
    while True:
        left: i64 = load_i8(platform, index) & 255
        right: i64 = load_i8(expected, index) & 255
        if left != right:
            return 0
        if left == 0:
            return 1
        index = index + 1
    return 0


@c_abi_export("pcc_io_waitset_platform_is_linux")
def _is_linux() -> i64:
    platform = target_sys_platform()
    expected = cstr("linux")
    index: i64 = 0
    while True:
        left: i64 = load_i8(platform, index) & 255
        right: i64 = load_i8(expected, index) & 255
        if left != right:
            return 0
        if left == 0:
            return 1
        index = index + 1
    return 0


@c_abi_export("pcc_io_waitset_target_is_linux_x86_64")
def _is_linux_x86_64() -> i64:
    if _is_linux() == 0:
        return 0
    machine = target_platform_machine()
    if load_i8(machine, 0) == 97:
        return 1  # compiler target registry admits arm64/aarch64 here
    expected = cstr("x86_64")
    index: i64 = 0
    while True:
        left: i64 = load_i8(machine, index) & 255
        right: i64 = load_i8(expected, index) & 255
        if left != right:
            return 0
        if left == 0:
            return 1
        index = index + 1
    return 0


@c_abi_export("pcc_io_waitset_find_slot")
def _find_slot(ws, fd: i64) -> i64:
    slots = load_ptr(ws, 8)
    length: i64 = load_i64(ws, 16)
    index: i64 = 0
    while index < length:
        slot = ptr_add(slots, index * 40)
        if load_i8(slot, 33) == 1 and load_i64(slot, 0) == fd:
            return index
        index = index + 1
    return -1


@c_abi_export("pcc_io_waitset_find_slot_generation")
def _find_slot_generation(ws, fd: i64, generation: i64) -> i64:
    index: i64 = _find_slot(ws, fd)
    if index < 0:
        return -1
    slot = ptr_add(load_ptr(ws, 8), index * 40)
    if (load_i32(slot, 36) & 2147483647) != generation:
        return -1
    return index


@c_abi_export("pcc_io_waitset_next_generation")
def _next_generation(ws) -> i64:
    generation: i64 = load_i32(ws, 76) & 2147483647
    if generation <= 0 or generation >= 2147483647:
        generation: i64 = 1
    else:
        generation = generation + 1
    store_i32(ws, 76, generation)
    return generation


@c_abi_export("pcc_io_waitset_epoll_update")
def _epoll_update(ws, slot, operation: i64) -> i64:
    fd: i64 = load_i64(slot, 0)
    interest: i64 = load_i64(slot, 8)
    generation: i64 = load_i32(slot, 36) & 2147483647
    events: i64 = 1073741824
    if (interest & 1) != 0:
        events = events | 1
    if (interest & 4) != 0:
        events = events | 4
    if load_i8(slot, 32) != 0:
        events = events | 2147483648
    token: i64 = generation * 4294967296 + (fd & 4294967295)
    result: i64 = epoll_ctl(load_i32(ws, 72), operation, fd, events, token)
    if operation == 2 and (result == -2 or result == -9):
        return 0
    if operation == 1 and result == -17:
        return epoll_ctl(load_i32(ws, 72), 3, fd, events, token)
    return result


@c_abi_export("pcc_io_waitset_reserve_slot")
def _reserve_slot(ws) -> i64:
    length: i64 = load_i64(ws, 16)
    capacity: i64 = load_i64(ws, 24)
    if length < capacity:
        return 0
    new_capacity: i64 = 8
    if capacity > 0:
        new_capacity = capacity * 2
    grown = realloc(load_ptr(ws, 8), new_capacity * 40)
    if ptr_is_null(grown) != 0:
        return -1
    store_ptr(ws, 8, grown)
    store_i64(ws, 24, new_capacity)
    return 0


@c_abi_export("pcc_io_waitset_reserve_output")
def _reserve_output(ws, need: i64) -> i64:
    if need < 1:
        need: i64 = 1
    if load_i64(ws, 48) < need:
        ready = realloc(load_ptr(ws, 40), need * 16)
        if ptr_is_null(ready) != 0:
            return -1
        store_ptr(ws, 40, ready)
        store_i64(ws, 48, need)
    if load_i64(ws, 64) < need:
        timed_out = realloc(load_ptr(ws, 56), need * 8)
        if ptr_is_null(timed_out) != 0:
            return -1
        store_ptr(ws, 56, timed_out)
        store_i64(ws, 64, need)
    return 0


@c_abi_export("pcc_io_waitset_compact")
def _compact(ws) -> None:
    slots = load_ptr(ws, 8)
    length: i64 = load_i64(ws, 16)
    read_index: i64 = 0
    write_index: i64 = 0
    while read_index < length:
        source = ptr_add(slots, read_index * 40)
        if load_i8(source, 33) == 1:
            if write_index != read_index:
                destination = ptr_add(slots, write_index * 40)
                memcpy_size: i64 = 40
                byte_index: i64 = 0
                while byte_index < memcpy_size:
                    store_i8(destination, byte_index, load_i8(source, byte_index))
                    byte_index = byte_index + 1
            write_index = write_index + 1
        read_index = read_index + 1
    store_i64(ws, 16, write_index)


@c_abi_export("pcc_io_waitset_write_kevent")
def _write_kevent(
    event, fd: i64, filter_value: i64, flags: i64, token: i64
) -> None:
    store_i64(event, 0, fd)
    filter_bits: i64 = filter_value & 65535
    store_i8(event, 8, filter_bits & 255)
    store_i8(event, 9, logical_shift_right_i64(filter_bits, 8) & 255)
    store_i8(event, 10, flags & 255)
    store_i8(event, 11, logical_shift_right_i64(flags, 8) & 255)
    store_i32(event, 12, 0)
    store_i64(event, 16, 0)
    store_i64(event, 24, token)


@c_abi_export("pcc_io_waitset_write_user_kevent")
def _write_user_kevent(event, flags: i64, filter_flags: i64) -> None:
    _write_kevent(event, 1, -10, flags, 0)
    store_i32(event, 12, filter_flags)


@c_abi_export("pcc_io_waitset_kqueue_update")
def _kqueue_update(
    ws, fd: i64, interest: i64, generation: i64, add: i64
) -> i64:
    changes = stack_alloc(64)
    count: i64 = 0
    flags: i64 = 2
    if add != 0:
        flags: i64 = 33
    token: i64 = generation * 4294967296 + (fd & 4294967295)
    if (interest & 1) != 0:
        _write_kevent(ptr_add(changes, count * 32), fd, -1, flags, token)
        count = count + 1
    if (interest & 4) != 0:
        _write_kevent(ptr_add(changes, count * 32), fd, -2, flags, token)
        count = count + 1
    if count == 0:
        return 0
    result: i64 = kevent_call(load_i32(ws, 72), changes, count, null(), 0, null())
    if result < 0:
        if add == 0 and result == -2:
            return 0
        return -1
    return 0


@c_abi_export("pcc_io_waitset_init")
def pcc_io_waitset_init(ws, backend: i64) -> i64:
    if ptr_is_null(ws) != 0:
        return -1
    memset(ws, 0, 88)
    store_i32(ws, 72, -1)
    store_i32(ws, 80, -1)
    store_i32(ws, 0, backend)
    if backend < 0 or backend > 2:
        return -1
    descriptor: i64 = -1
    if backend == 2:
        if _is_linux_x86_64() == 0:
            return -1
        descriptor = epoll_create1(524288)
        if descriptor < 0:
            return -1
        store_i32(ws, 72, descriptor)
        wake_descriptor: i64 = eventfd_create(0, 526336)
        if wake_descriptor < 0:
            close(descriptor)
            store_i32(ws, 72, -1)
            return -1
        if epoll_ctl(descriptor, 1, wake_descriptor, 1, 0) != 0:
            close(wake_descriptor)
            close(descriptor)
            store_i32(ws, 72, -1)
            return -1
        store_i32(ws, 80, wake_descriptor)
    if backend == 1:
        if _is_darwin() == 0:
            return -1
        descriptor = kqueue_create()
        if descriptor < 0:
            return -1
        store_i32(ws, 72, descriptor)
        wake_event = stack_alloc(32)
        _write_user_kevent(wake_event, 33, 0)
        if kevent_call(descriptor, wake_event, 1, null(), 0, null()) < 0:
            close(descriptor)
            store_i32(ws, 72, -1)
            return -1
    return 0


@c_abi_export("pcc_io_waitset_dispose")
def pcc_io_waitset_dispose(ws) -> None:
    if ptr_is_null(ws) != 0:
        return
    wake_descriptor: i64 = load_i32(ws, 80)
    if wake_descriptor >= 0:
        close(wake_descriptor)
    descriptor: i64 = load_i32(ws, 72)
    if descriptor >= 0:
        close(descriptor)
    free(load_ptr(ws, 8))
    free(load_ptr(ws, 40))
    free(load_ptr(ws, 56))
    store_ptr(ws, 8, null())
    store_ptr(ws, 40, null())
    store_ptr(ws, 56, null())
    store_i64(ws, 16, 0)
    store_i64(ws, 24, 0)
    store_i64(ws, 32, 0)
    store_i64(ws, 48, 0)
    store_i64(ws, 64, 0)
    store_i32(ws, 72, -1)
    store_i32(ws, 76, 0)
    store_i32(ws, 80, -1)
    store_i32(ws, 0, 0)


@c_abi_export("pcc_io_waitset_interrupt")
def pcc_io_waitset_interrupt(ws) -> i64:
    if ptr_is_null(ws) != 0:
        return -1
    backend: i64 = load_i32(ws, 0)
    if backend == 0:
        return 0
    if backend == 1:
        trigger = stack_alloc(32)
        _write_user_kevent(trigger, 0, 16777216)
        result: i64 = -4
        while result == -4:
            result = kevent_call(
                load_i32(ws, 72), trigger, 1, null(), 0, null()
            )
        if result < 0:
            return -1
        return 0
    if backend == 2:
        value = stack_alloc(8)
        store_i64(value, 0, 1)
        result: i64 = -4
        while result == -4:
            result = write(load_i32(ws, 80), value, 8)
        if result == 8 or result == -11:
            return 0
        return -1
    return -1


@c_abi_export("pcc_io_waitset_add")
def pcc_io_waitset_add(
    ws, fd: i64, interest: i64, deadline: i64, edge: i64
) -> i64:
    if ptr_is_null(ws) != 0:
        return -1
    index: i64 = _find_slot(ws, fd)
    is_new: i64 = 0
    old_interest: i64 = 0
    old_deadline: i64 = -1
    old_ready: i64 = 0
    old_edge: i64 = 0
    old_generation: i64 = 0
    if index < 0:
        if _reserve_slot(ws) != 0:
            return -1
        index = load_i64(ws, 16)
        store_i64(ws, 16, index + 1)
        slot = ptr_add(load_ptr(ws, 8), index * 40)
        store_i64(slot, 24, 0)
        store_i8(slot, 33, 1)
        store_i64(ws, 32, load_i64(ws, 32) + 1)
        is_new: i64 = 1
    else:
        slot = ptr_add(load_ptr(ws, 8), index * 40)
        old_interest = load_i64(slot, 8)
        old_deadline = load_i64(slot, 16)
        old_ready = load_i64(slot, 24)
        old_edge = load_i8(slot, 32)
        old_generation = load_i32(slot, 36)
        if load_i32(ws, 0) == 1:
            _kqueue_update(ws, fd, load_i64(slot, 8), old_generation, 0)
    store_i64(slot, 0, fd)
    store_i64(slot, 8, interest)
    store_i64(slot, 16, deadline)
    if edge != 0:
        store_i8(slot, 32, 1)
    else:
        store_i8(slot, 32, 0)
    store_i32(slot, 36, _next_generation(ws))
    backend: i64 = load_i32(ws, 0)
    if backend == 1 and _kqueue_update(
        ws, fd, interest, load_i32(slot, 36), 1
    ) != 0:
        if is_new != 0:
            store_i8(slot, 33, 0)
            store_i64(ws, 32, load_i64(ws, 32) - 1)
            _compact(ws)
        else:
            store_i64(slot, 8, old_interest)
            store_i64(slot, 16, old_deadline)
            store_i64(slot, 24, old_ready)
            store_i8(slot, 32, old_edge)
            store_i32(slot, 36, old_generation)
            _kqueue_update(
                ws, fd, old_interest, old_generation, 1
            )
        return -1
    if backend == 2:
        operation: i64 = 1
        if is_new == 0:
            operation: i64 = 3
        if _epoll_update(ws, slot, operation) != 0:
            if is_new != 0:
                store_i8(slot, 33, 0)
                store_i64(ws, 32, load_i64(ws, 32) - 1)
                _compact(ws)
            else:
                store_i64(slot, 8, old_interest)
                store_i64(slot, 16, old_deadline)
                store_i64(slot, 24, old_ready)
                store_i8(slot, 32, old_edge)
                store_i32(slot, 36, old_generation)
            return -1
    if is_new == 0:
        store_i8(slot, 33, 1)
    return 0


@c_abi_export("pcc_io_waitset_remove")
def pcc_io_waitset_remove(ws, fd: i64) -> i64:
    if ptr_is_null(ws) != 0:
        return 0
    index: i64 = _find_slot(ws, fd)
    if index < 0:
        return 0
    slot = ptr_add(load_ptr(ws, 8), index * 40)
    if load_i32(ws, 0) == 1:
        _kqueue_update(
            ws, fd, load_i64(slot, 8), load_i32(slot, 36), 0
        )
    elif load_i32(ws, 0) == 2:
        _epoll_update(ws, slot, 2)
    store_i8(slot, 33, 0)
    store_i64(ws, 32, load_i64(ws, 32) - 1)
    _compact(ws)
    return 1


@c_abi_export("pcc_io_waitset_count")
def pcc_io_waitset_count(ws) -> i64:
    if ptr_is_null(ws) != 0:
        return 0
    return load_i64(ws, 32)


@c_abi_export("pcc_io_waitset_set_ready")
def pcc_io_waitset_set_ready(ws, fd: i64, events: i64) -> None:
    if ptr_is_null(ws) != 0:
        return
    index: i64 = _find_slot(ws, fd)
    if index >= 0:
        slot = ptr_add(load_ptr(ws, 8), index * 40)
        store_i64(slot, 24, load_i64(slot, 24) | events)


@c_abi_export("pcc_io_waitset_clear_ready")
def pcc_io_waitset_clear_ready(ws, fd: i64) -> None:
    if ptr_is_null(ws) != 0:
        return
    index: i64 = _find_slot(ws, fd)
    if index >= 0:
        store_i64(ptr_add(load_ptr(ws, 8), index * 40), 24, 0)


@c_abi_export("pcc_io_waitset_monotonic_ms")
def _monotonic_ms() -> i64:
    value = stack_alloc(16)
    if clock_gettime(1, value) != 0:
        return -1
    return load_i64(value, 0) * 1000 + unsigned_div_i64(
        load_i64(value, 8), 1000000
    )


@c_abi_export("pcc_io_waitset_effective_deadline")
def _effective_deadline(ws, wait_deadline: i64, now: i64) -> i64:
    if load_i64(ws, 32) <= 0:
        return now
    deadline: i64 = wait_deadline
    slots = load_ptr(ws, 8)
    index: i64 = 0
    while index < load_i64(ws, 16):
        slot = ptr_add(slots, index * 40)
        if load_i8(slot, 33) == 1:
            hit: i64 = load_i64(slot, 24) & (load_i64(slot, 8) | 56)
            if hit != 0:
                return now
            candidate: i64 = load_i64(slot, 16)
            if candidate >= 0 and (deadline < 0 or candidate < deadline):
                deadline = candidate
        index = index + 1
    return deadline


@c_abi_export("pcc_io_waitset_remaining_ms")
def _remaining_ms(now: i64, deadline: i64) -> i64:
    if deadline < 0:
        return -1
    if deadline <= now:
        return 0
    remaining: i64 = deadline - now
    if remaining > 2147483647:
        return 2147483647
    return remaining


@c_abi_export("pcc_io_waitset_write_timeout")
def _write_timeout(timeout, timeout_ms: i64) -> None:
    store_i64(timeout, 0, unsigned_div_i64(timeout_ms, 1000))
    store_i64(timeout, 8, unsigned_rem_i64(timeout_ms, 1000) * 1000000)


@c_abi_export("pcc_io_waitset_wait_poll")
def _wait_poll(ws, now: i64, out) -> i64:
    if _reserve_output(ws, load_i64(ws, 32)) != 0:
        return -1
    ready_count: i64 = 0
    timeout_count: i64 = 0
    slots = load_ptr(ws, 8)
    index: i64 = 0
    while index < load_i64(ws, 16):
        slot = ptr_add(slots, index * 40)
        if load_i8(slot, 33) == 1:
            hit: i64 = load_i64(slot, 24) & (load_i64(slot, 8) | 56)
            deadline: i64 = load_i64(slot, 16)
            if hit != 0:
                event = ptr_add(load_ptr(ws, 40), ready_count * 16)
                store_i64(event, 0, load_i64(slot, 0))
                store_i64(event, 8, hit)
                ready_count = ready_count + 1
                store_i8(slot, 33, 0)
                store_i64(ws, 32, load_i64(ws, 32) - 1)
            elif deadline >= 0 and deadline <= now:
                store_i64(load_ptr(ws, 56), timeout_count * 8, load_i64(slot, 0))
                timeout_count = timeout_count + 1
                store_i8(slot, 33, 0)
                store_i64(ws, 32, load_i64(ws, 32) - 1)
        index = index + 1
    _compact(ws)
    store_ptr(out, 0, load_ptr(ws, 40))
    store_i64(out, 8, ready_count)
    store_ptr(out, 16, load_ptr(ws, 56))
    store_i64(out, 24, timeout_count)
    return 0


@c_abi_export("pcc_io_waitset_wait_discard")
def pcc_io_waitset_wait_discard(batch) -> None:
    if ptr_is_null(batch) != 0:
        return
    free(load_ptr(batch, 0))
    memset(batch, 0, 64)


@c_abi_export("pcc_io_waitset_wait_prepare")
def pcc_io_waitset_wait_prepare(
    ws, now: i64, wait_deadline: i64, batch
) -> i64:
    if ptr_is_null(ws) != 0 or ptr_is_null(batch) != 0:
        return -1
    memset(batch, 0, 64)
    backend: i64 = load_i32(ws, 0)
    if backend != 1 and backend != 2:
        return -1
    bounded_live: i64 = load_i64(ws, 32)
    if bounded_live < 1:
        bounded_live: i64 = 1
    if bounded_live > 256:
        bounded_live: i64 = 256
    event_capacity: i64 = bounded_live + 1
    event_size: i64 = 12
    if backend == 1:
        event_capacity = bounded_live * 2 + 1
        event_size: i64 = 32
    # Allocate output scratch before the live syscall can consume a kqueue
    # edge or disarm an epoll one-shot registration.  Registrations added
    # during the unlocked block are covered up to the maximum returned batch;
    # any larger timeout burst remains live for an immediate later drain.
    live_count: i64 = load_i64(ws, 32)
    if live_count > 9223372036854775807 - event_capacity:
        return -1
    output_need: i64 = live_count + event_capacity
    if _reserve_output(ws, output_need) != 0:
        return -1
    events = malloc(event_capacity * event_size)
    if ptr_is_null(events) != 0:
        return -1
    store_ptr(batch, 0, events)
    store_i64(batch, 8, event_capacity)
    store_i64(batch, 16, 0)
    store_i64(batch, 24, now)
    store_i64(batch, 32, _effective_deadline(ws, wait_deadline, now))
    store_i64(batch, 40, wait_deadline)
    store_i64(batch, 48, backend)
    store_i64(batch, 56, 0)
    return 0


@c_abi_export("pcc_io_waitset_wait_block")
def pcc_io_waitset_wait_block(ws, batch) -> i64:
    if (
        ptr_is_null(ws) != 0
        or ptr_is_null(batch) != 0
        or ptr_is_null(load_ptr(batch, 0)) != 0
    ):
        if ptr_is_null(batch) == 0:
            store_i64(batch, 56, -1)
        return -1
    backend: i64 = load_i64(batch, 48)
    if backend != load_i32(ws, 0) or (backend != 1 and backend != 2):
        store_i64(batch, 56, -1)
        return -1
    timeout = stack_alloc(16)
    event_count: i64 = -4
    while event_count == -4:
        current_now: i64 = load_i64(batch, 24)
        effective_deadline: i64 = load_i64(batch, 32)
        remaining: i64 = _remaining_ms(current_now, effective_deadline)
        # The kernel wait may last forever, so it runs exited, as condition
        # waits and joins do: a stop-the-world does not wait for this
        # carrier, which touches only the waitset and its event buffer here.
        entered: i64 = pcc_thread_blocking_enter(frame_address())
        if backend == 1:
            timeout_pointer = null()
            if remaining >= 0:
                _write_timeout(timeout, remaining)
                timeout_pointer = timeout
            event_count = kevent_call(
                load_i32(ws, 72),
                null(),
                0,
                load_ptr(batch, 0),
                load_i64(batch, 8),
                timeout_pointer,
            )
        else:
            event_count = epoll_wait(
                load_i32(ws, 72),
                load_ptr(batch, 0),
                load_i64(batch, 8),
                remaining,
            )
        pcc_thread_blocking_leave(entered)
        if event_count == -4:
            observed: i64 = _monotonic_ms()
            if observed >= 0:
                store_i64(batch, 24, observed)
            if (
                effective_deadline >= 0
                and load_i64(batch, 24) >= effective_deadline
            ):
                event_count: i64 = 0
    observed = _monotonic_ms()
    if observed >= 0:
        store_i64(batch, 24, observed)
    store_i64(batch, 16, event_count)
    if event_count < 0:
        store_i64(batch, 56, -1)
        return -1
    store_i64(batch, 56, 0)
    return 0


@c_abi_export("pcc_io_waitset_drain_epoll_interrupt")
def _drain_epoll_interrupt(ws) -> None:
    value = stack_alloc(8)
    result: i64 = -4
    while result == -4 or result == 8:
        result = read(load_i32(ws, 80), value, 8)


@c_abi_export("pcc_io_waitset_unregister_slot")
def _unregister_slot(ws, slot) -> None:
    backend: i64 = load_i32(ws, 0)
    if backend == 1:
        _kqueue_update(
            ws,
            load_i64(slot, 0),
            load_i64(slot, 8),
            load_i32(slot, 36),
            0,
        )
    elif backend == 2:
        _epoll_update(ws, slot, 2)


@c_abi_export("pcc_io_waitset_wait_finish")
def pcc_io_waitset_wait_finish(ws, batch, out) -> i64:
    if (
        ptr_is_null(ws) != 0
        or ptr_is_null(batch) != 0
        or ptr_is_null(out) != 0
    ):
        return -1
    store_ptr(out, 0, null())
    store_i64(out, 8, 0)
    store_ptr(out, 16, null())
    store_i64(out, 24, 0)
    if (
        load_i64(batch, 56) != 0
        or load_i64(batch, 16) < 0
        or load_i64(batch, 48) != load_i32(ws, 0)
    ):
        pcc_io_waitset_wait_discard(batch)
        return -1
    backend: i64 = load_i64(batch, 48)
    events = load_ptr(batch, 0)
    event_count: i64 = load_i64(batch, 16)
    event_index: i64 = 0
    if backend == 1:
        while event_index < event_count:
            event = ptr_add(events, event_index * 32)
            token: i64 = load_i64(event, 24)
            if token != 0:
                fd: i64 = token & 4294967295
                generation: i64 = (
                    logical_shift_right_i64(token, 32) & 2147483647
                )
                slot_index: i64 = _find_slot_generation(ws, fd, generation)
                if slot_index >= 0:
                    bits: i64 = 0
                    filter_bits: i64 = load_i32(event, 8) & 65535
                    if filter_bits == 65535:
                        bits = bits | 1
                    elif filter_bits == 65534:
                        bits = bits | 4
                    flags: i64 = (load_i8(event, 10) & 255) | (
                        (load_i8(event, 11) & 255) * 256
                    )
                    if (flags & 32768) != 0:
                        bits = bits | 16
                    if (flags & 16384) != 0:
                        bits = bits | 8
                    slot = ptr_add(load_ptr(ws, 8), slot_index * 40)
                    store_i64(slot, 24, load_i64(slot, 24) | bits)
            event_index = event_index + 1
    elif backend == 2:
        while event_index < event_count:
            event = ptr_add(events, event_index * 12)
            kernel_events: i64 = load_i32(event, 0)
            fd = load_i32(event, 4) & 4294967295
            generation = load_i32(event, 8) & 2147483647
            if generation != 0:
                slot_index = _find_slot_generation(ws, fd, generation)
                if slot_index >= 0:
                    bits = kernel_events & 29
                    if (kernel_events & 8192) != 0:
                        bits = bits | 16
                    slot = ptr_add(load_ptr(ws, 8), slot_index * 40)
                    store_i64(slot, 24, load_i64(slot, 24) | bits)
            event_index = event_index + 1
        _drain_epoll_interrupt(ws)
    else:
        pcc_io_waitset_wait_discard(batch)
        return -1

    current_now: i64 = load_i64(batch, 24)
    pcc_io_waitset_wait_discard(batch)
    finish_now: i64 = _monotonic_ms()
    if finish_now >= 0:
        current_now = finish_now
    ready_count: i64 = 0
    slots = load_ptr(ws, 8)
    index: i64 = 0
    while index < load_i64(ws, 16):
        slot = ptr_add(slots, index * 40)
        if load_i8(slot, 33) == 1:
            hit: i64 = load_i64(slot, 24) & (load_i64(slot, 8) | 56)
            if hit != 0:
                if ready_count >= load_i64(ws, 48):
                    index = index + 1
                    continue
                output_event = ptr_add(load_ptr(ws, 40), ready_count * 16)
                store_i64(output_event, 0, load_i64(slot, 0))
                store_i64(output_event, 8, hit)
                ready_count = ready_count + 1
                _unregister_slot(ws, slot)
                store_i8(slot, 33, 0)
                store_i64(ws, 32, load_i64(ws, 32) - 1)
        index = index + 1

    timeout_count: i64 = 0
    index: i64 = 0
    while index < load_i64(ws, 16):
        slot = ptr_add(slots, index * 40)
        if load_i8(slot, 33) == 1:
            deadline: i64 = load_i64(slot, 16)
            if deadline >= 0 and deadline <= current_now:
                if timeout_count >= load_i64(ws, 64):
                    index = index + 1
                    continue
                store_i64(
                    load_ptr(ws, 56), timeout_count * 8, load_i64(slot, 0)
                )
                timeout_count = timeout_count + 1
                _unregister_slot(ws, slot)
                store_i8(slot, 33, 0)
                store_i64(ws, 32, load_i64(ws, 32) - 1)
        index = index + 1
    _compact(ws)
    store_ptr(out, 0, load_ptr(ws, 40))
    store_i64(out, 8, ready_count)
    store_ptr(out, 16, load_ptr(ws, 56))
    store_i64(out, 24, timeout_count)
    return 0


@c_abi_export("pcc_io_waitset_wait_until")
def pcc_io_waitset_wait_until(
    ws, now: i64, wait_deadline: i64, out
) -> i64:
    if ptr_is_null(ws) != 0 or ptr_is_null(out) != 0:
        return -1
    store_ptr(out, 0, null())
    store_i64(out, 8, 0)
    store_ptr(out, 16, null())
    store_i64(out, 24, 0)
    backend: i64 = load_i32(ws, 0)
    if backend == 0:
        return _wait_poll(ws, now, out)
    if backend == 2 and _is_linux_x86_64() == 0:
        return -1
    if backend == 1 and _is_darwin() == 0:
        return -1
    batch = stack_alloc(64)
    if pcc_io_waitset_wait_prepare(ws, now, wait_deadline, batch) != 0:
        return -1
    pcc_io_waitset_wait_block(ws, batch)
    return pcc_io_waitset_wait_finish(ws, batch, out)


@c_abi_export("pcc_io_waitset_wait")
def pcc_io_waitset_wait(ws, now: i64, out) -> i64:
    return pcc_io_waitset_wait_until(ws, now, now, out)


@c_abi_typed_export("pcc_io_waitset_kqueue_available", "i32", ())
def pcc_io_waitset_kqueue_available() -> i64:
    return _is_darwin()


@c_abi_typed_export("pcc_io_waitset_real_kqueue_skip", "i32", ("ptr",))
def pcc_io_waitset_real_kqueue_skip(out) -> i64:
    if _is_darwin() != 0:
        return 0
    if ptr_is_null(out) == 0:
        store_ptr(out, 0, cstr("io_waitset.real_kqueue"))
        store_ptr(
            out,
            8,
            cstr(
                "real kqueue/kevent requires Darwin/BSD; this platform has no "
                "kqueue backend. Linux x86_64 uses the epoll backend; other "
                "targets use the explicitly labeled poll fallback."
            ),
        )
    return 1


@c_abi_typed_export("pcc_io_waitset_epoll_available", "i32", ())
def pcc_io_waitset_epoll_available() -> i64:
    """Whether compiler-owned live epoll is available for this target."""
    return _is_linux_x86_64()


@c_abi_typed_export("pcc_io_waitset_real_epoll_skip", "i32", ("ptr",))
def pcc_io_waitset_real_epoll_skip(out) -> i64:
    if _is_linux_x86_64() != 0:
        return 0
    if ptr_is_null(out) == 0:
        store_ptr(out, 0, cstr("io_waitset.real_epoll"))
        if _is_linux() != 0:
            store_ptr(
                out,
                8,
                cstr(
                    "compiler-owned epoll syscalls currently require the "
                    "Linux x86_64 self-backend target"
                ),
            )
        else:
            store_ptr(
                out,
                8,
                cstr("real epoll is a Linux-only readiness backend"),
            )
    return 1


@c_abi_export("pcc_io_waitset_backend_label")
def pcc_io_waitset_backend_label(backend: i64):
    if backend == 1:
        return cstr("kqueue")
    if backend == 2:
        return cstr("epoll")
    if backend == 0:
        return cstr("poll")
    return cstr("unknown")


@c_abi_export("pcc_io_waitset_default_backend")
def pcc_io_waitset_default_backend() -> i64:
    if _is_darwin() != 0:
        return 1
    if _is_linux_x86_64() != 0:
        return 2
    return 0


# --- iox-style non-blocking I/O semantics -----------------------------
# Non-blocking I/O semantics: an I/O observation is progress + control,
# never a naked failure.  Outcome codes:
#   PCC_IO_OUTCOME_OK        = 0  terminal success
#   PCC_IO_OUTCOME_MORE      = 1  non-failure progress; a successor observation
#                                 is expected (multi-shot)
#   PCC_IO_OUTCOME_WOULDBLOCK= 2  no progress right now; wait / yield / retry
#   PCC_IO_OUTCOME_ERR       = -1 failure (errno-style)
# IsWouldBlock / IsMore are the pcc equivalents of iox.IsWouldBlock /
# iox.IsMore and stay stable across wrappers.



@c_abi_export("pcc_io_is_wouldblock")
def pcc_io_is_wouldblock(outcome: i64) -> i64:
    if outcome == 2:
        return 1
    return 0


@c_abi_export("pcc_io_is_more")
def pcc_io_is_more(outcome: i64) -> i64:
    if outcome == 1:
        return 1
    return 0


@c_abi_export("pcc_io_outcome_label")
def pcc_io_outcome_label(outcome: i64) -> c_ptr:
    if outcome == 0:
        return cstr("ok")
    if outcome == 1:
        return cstr("more")
    if outcome == 2:
        return cstr("wouldblock")
    return cstr("err")
