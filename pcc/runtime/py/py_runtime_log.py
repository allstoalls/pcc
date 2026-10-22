"""Runtime event logging authored in pcc-Python."""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i32,
    atomic_load_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    close,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    define_thread_local_i32,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i8,
    load_i32,
    load_i64,
    null,
    open_file,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_ptr,
    thread_safepoint,
    unsigned_div_i64,
    unsigned_rem_i64,
    write,
)


strlen = extern("strlen", (c_ptr,), c_int64)
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_platform_wall_time_us = extern("pcc_platform_wall_time_us", (), c_int64)
pcc_platform_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
pcc_thread_no_park_depth = extern("pcc_thread_no_park_depth", (), c_int64)


define_global_i32("pcc_log_init_state", 0)
define_global_i32("pcc_log_mask", 0)
define_global_i32("pcc_log_json", 0)
define_global_ptr_null("pcc_log_file_path")
define_global_i32("pcc_diagnostics_runtime_log_fast_state", -1)
define_global_i32("pcc_log_write_lock", 0)
define_thread_local_i32("pcc_log_emitting", 0)
define_global_i64("pcc_log_thread_trace_dropped", 0)


def _cstr_equal(lhs: c_ptr, rhs: c_ptr) -> int:
    if ptr_is_null(lhs) or ptr_is_null(rhs):
        return 0
    index: int = 0
    while load_i8(lhs, index) == load_i8(rhs, index):
        if load_i8(lhs, index) == 0:
            return 1
        index = index + 1
    return 0


def _token_equal(start: c_ptr, length: int, expected: c_ptr) -> int:
    if strlen(expected) != length:
        return 0
    index: int = 0
    while index < length:
        if load_i8(start, index) != load_i8(expected, index):
            return 0
        index = index + 1
    return 1


def _token_enabled(tokens: c_ptr, category: c_ptr) -> int:
    if ptr_is_null(tokens) or load_i8(tokens, 0) == 0 or ptr_is_null(category):
        return 0
    offset: int = 0
    while load_i8(tokens, offset) != 0:
        while (
            load_i8(tokens, offset) == 44
            or load_i8(tokens, offset) == 32
            or load_i8(tokens, offset) == 9
        ):
            offset = offset + 1
        start: int = offset
        while (
            load_i8(tokens, offset) != 0
            and load_i8(tokens, offset) != 44
            and load_i8(tokens, offset) != 32
            and load_i8(tokens, offset) != 9
        ):
            offset = offset + 1
        length: int = offset - start
        if _token_equal(ptr_add(tokens, start), length, cstr("all")):
            return 1
        if _token_equal(ptr_add(tokens, start), length, category):
            return 1
        while (
            load_i8(tokens, offset) != 0
            and load_i8(tokens, offset) != 44
        ):
            offset = offset + 1
        if load_i8(tokens, offset) == 44:
            offset = offset + 1
    return 0


def _parse_tokens(tokens: c_ptr) -> int:
    if ptr_is_null(tokens) or load_i8(tokens, 0) == 0:
        return 0
    if _cstr_equal(tokens, cstr("1")) or _cstr_equal(tokens, cstr("all")):
        return -1
    mask: int = 0
    if _token_enabled(tokens, cstr("alloc")):
        mask = mask | 1
    if _token_enabled(tokens, cstr("gc")):
        mask = mask | 2
    if _token_enabled(tokens, cstr("refcount")):
        mask = mask | 4
    if _token_enabled(tokens, cstr("weakref")):
        mask = mask | 8
    if _token_enabled(tokens, cstr("finalizer")):
        mask = mask | 16
    if _token_enabled(tokens, cstr("exception")):
        mask = mask | 32
    if _token_enabled(tokens, cstr("dispatch")):
        mask = mask | 64
    if _token_enabled(tokens, cstr("runtime")):
        mask = mask | 128
    if _token_enabled(tokens, cstr("thread")):
        mask = mask | 256
    return mask


def _init_once() -> None:
    state = global_addr("pcc_log_init_state")
    if atomic_load_i32(state, 0, "acquire") == 2:
        return
    if atomic_cas_i32(state, 0, 0, 1, "acq_rel", "acquire") == 0:
        # Environment ownership is initialized lazily and may allocate while
        # answering the first getenv().  Allocation itself emits a runtime-log
        # event, so suppress the fast event path while this thread owns log
        # initialization; otherwise that event re-enters _init_once(), sees
        # state == 1, and waits forever for its own initialization to finish.
        # The configured fast state is published again below.
        atomic_store_i32(
            global_addr("pcc_diagnostics_runtime_log_fast_state"), 0, 0, "release"
        )
        mask: int = _parse_tokens(pcc_platform_getenv(cstr("PCC_LOG")))
        store_i32(global_addr("pcc_log_mask"), 0, mask)
        fmt = pcc_platform_getenv(cstr("PCC_LOG_FORMAT"))
        if not ptr_is_null(fmt) and _cstr_equal(fmt, cstr("json")):
            store_i32(global_addr("pcc_log_json"), 0, 1)
        else:
            store_i32(global_addr("pcc_log_json"), 0, 0)
        global_store_ptr(
            "pcc_log_file_path", pcc_platform_getenv(cstr("PCC_LOG_FILE"))
        )
        if mask == 0:
            atomic_store_i32(
                global_addr("pcc_diagnostics_runtime_log_fast_state"), 0, 0, "release"
            )
        else:
            atomic_store_i32(
                global_addr("pcc_diagnostics_runtime_log_fast_state"), 0, 1, "release"
            )
        atomic_store_i32(state, 0, 2, "release")
        return
    while atomic_load_i32(state, 0, "acquire") != 2:
        thread_safepoint()


def _category_mask(category: c_ptr) -> int:
    if ptr_is_null(category):
        return 0
    if _cstr_equal(category, cstr("alloc")):
        return 1
    if _cstr_equal(category, cstr("gc")):
        return 2
    if _cstr_equal(category, cstr("refcount")):
        return 4
    if _cstr_equal(category, cstr("weakref")):
        return 8
    if _cstr_equal(category, cstr("finalizer")):
        return 16
    if _cstr_equal(category, cstr("exception")):
        return 32
    if _cstr_equal(category, cstr("dispatch")):
        return 64
    if _cstr_equal(category, cstr("runtime")):
        return 128
    if _cstr_equal(category, cstr("thread")):
        return 256
    return 0


@c_abi_export("pcc_runtime_now_us")
def pcc_runtime_now_us() -> int:
    return pcc_platform_wall_time_us()


@c_abi_export("pcc_runtime_monotonic_us")
def pcc_runtime_monotonic_us() -> int:
    return pcc_platform_monotonic_us()


@c_abi_export("pcc_runtime_sleep_ns")
def pcc_runtime_sleep_ns(delay_ns: int) -> int:
    return pcc_platform_sleep_ns(delay_ns)


@c_abi_export("pcc_diagnostics_runtime_log_enabled")
def pcc_diagnostics_runtime_log_enabled(category: c_ptr) -> int:
    # First registration may wait for a stopped-world owner.  It must happen
    # before initialization or the write lock can be owned by this thread.
    pcc_current_thread_id()
    _init_once()
    return load_i32(global_addr("pcc_log_mask"), 0) & _category_mask(category)


def _code_enabled(category: int) -> int:
    pcc_current_thread_id()
    _init_once()
    mask: int = load_i32(global_addr("pcc_log_mask"), 0)
    if category == 1:
        return mask & 1
    if category == 2:
        return mask & 2
    if category == 3:
        return mask & 4
    if category == 4:
        return mask & 8
    if category == 5:
        return mask & 16
    if category == 6:
        return mask & 32
    if category == 7:
        return mask & 64
    if category == 9:
        return mask & 256
    return mask & 128


def _write_lock_acquire() -> None:
    while atomic_cas_i32(
        global_addr("pcc_log_write_lock"), 0, 0, 1, "acq_rel", "acquire"
    ) != 0:
        thread_safepoint()


def _write_lock_release() -> None:
    atomic_store_i32(global_addr("pcc_log_write_lock"), 0, 0, "release")


def _open_stream(should_close: c_ptr) -> int:
    store_i32(should_close, 0, 0)
    path = global_load_ptr("pcc_log_file_path")
    if (
        ptr_is_null(path)
        or load_i8(path, 0) == 0
        or _cstr_equal(path, cstr("-"))
    ):
        return 2
    # Owned platform append open: no heap-backed FILE or stdio lock.  This
    # path may block in OS I/O, but cannot allocate, register or safepoint.
    stream = open_file(path, 1, 2)
    if stream < 0:
        return 2
    store_i32(should_close, 0, 1)
    return stream


def _write_text(stream: int, text: c_ptr) -> None:
    if ptr_is_null(text):
        return
    length: int = strlen(text)
    offset: int = 0
    while offset < length:
        written: int = write(stream, ptr_add(text, offset), length - offset)
        if written == -4:
            continue
        if written <= 0:
            return
        offset = offset + written


def _write_i64(stream: int, value: int) -> None:
    buffer = stack_alloc(32)
    end: int = 31
    store_i8(buffer, end, 0)
    negative: int = 0
    magnitude: int = value
    if value < 0:
        negative = 1
        magnitude = 0 - value
    if magnitude == 0:
        end = end - 1
        store_i8(buffer, end, 48)
    while magnitude != 0:
        end = end - 1
        store_i8(buffer, end, 48 + unsigned_rem_i64(magnitude, 10))
        magnitude = unsigned_div_i64(magnitude, 10)
    if negative:
        end = end - 1
        store_i8(buffer, end, 45)
    _write_text(stream, ptr_add(buffer, end))


def _write_pointer(stream: int, value: c_ptr) -> None:
    if ptr_is_null(value):
        _write_text(stream, cstr("0x0"))
        return
    pointer_slot = stack_alloc(8)
    store_ptr(pointer_slot, 0, value)
    bits: int = load_i64(pointer_slot, 0)
    buffer = stack_alloc(32)
    end: int = 31
    store_i8(buffer, end, 0)
    while bits != 0:
        digit: int = unsigned_rem_i64(bits, 16)
        end = end - 1
        if digit < 10:
            store_i8(buffer, end, 48 + digit)
        else:
            store_i8(buffer, end, 87 + digit)
        bits = unsigned_div_i64(bits, 16)
    end = end - 1
    store_i8(buffer, end, 120)
    end = end - 1
    store_i8(buffer, end, 48)
    _write_text(stream, ptr_add(buffer, end))


def _category_from_code(category: int) -> c_ptr:
    if category == 1:
        return cstr("alloc")
    if category == 2:
        return cstr("gc")
    if category == 3:
        return cstr("refcount")
    if category == 4:
        return cstr("weakref")
    if category == 5:
        return cstr("finalizer")
    if category == 6:
        return cstr("exception")
    if category == 7:
        return cstr("dispatch")
    if category == 9:
        return cstr("thread")
    return cstr("runtime")


def _event_from_code(category: int, event: int) -> c_ptr:
    if category == 1:
        if event == 1:
            return cstr("alloc_request")
        if event == 2:
            return cstr("alloc_object")
        return cstr("alloc_event")
    if category == 2:
        if event == 1:
            return cstr("collect_start")
        if event == 2:
            return cstr("collect_stop")
        if event == 3:
            return cstr("store_ptr")
        return cstr("gc_event")
    if category == 3:
        if event == 1:
            return cstr("incref")
        if event == 2:
            return cstr("decref")
        if event == 3:
            return cstr("free")
        return cstr("refcount_event")
    if category == 4:
        if event == 1:
            return cstr("new")
        if event == 2:
            return cstr("invalidate")
        if event == 3:
            return cstr("callback")
        if event == 4:
            return cstr("dealloc")
        return cstr("weakref_event")
    if category == 5:
        if event == 1:
            return cstr("lookup")
        if event == 2:
            return cstr("call")
        if event == 3:
            return cstr("done")
        if event == 4:
            return cstr("skipped")
        return cstr("finalizer_event")
    if category == 6:
        if event == 1:
            return cstr("alloc")
        if event == 2:
            return cstr("new")
        if event == 3:
            return cstr("raise")
        if event == 4:
            return cstr("clear")
        if event == 5:
            return cstr("set_cause")
        if event == 6:
            return cstr("set_context")
        if event == 7:
            return cstr("dealloc")
        if event == 8:
            return cstr("new_with_value")
        if event == 9:
            return cstr("new_with_class")
        return cstr("exception_event")
    if category == 7:
        if event == 1:
            return cstr("getitem")
        if event == 2:
            return cstr("slice")
        if event == 3:
            return cstr("setitem")
        if event == 4:
            return cstr("delitem")
        if event == 5:
            return cstr("getattr")
        if event == 6:
            return cstr("setattr")
        if event == 7:
            return cstr("delattr")
        if event == 8:
            return cstr("call")
        if event == 9:
            return cstr("isinstance")
        if event == 10:
            # py_obj_call reached its fall-through: no dispatch branch
            # matched, so it returns NULL with no exception set and the
            # caller invents a message. value0 carries the type tag.
            return cstr("call_unmatched")
        return cstr("dispatch_event")
    if category == 9:
        if event == 1:
            return cstr("start")
        if event == 2:
            return cstr("enter")
        if event == 3:
            return cstr("exit")
        if event == 4:
            return cstr("join")
        if event == 5:
            return cstr("joined")
        if event == 6:
            return cstr("start_failed")
        if event == 7:
            return cstr("join_failed")
        if event == 8:
            return cstr("detach")
        if event == 9:
            return cstr("scheduler_lock_request")
        if event == 10:
            return cstr("scheduler_acquired_deferred")
        if event == 11:
            return cstr("scheduler_lock_released")
        if event == 12:
            return cstr("scheduler_lock_failed")
        if event == 13:
            return cstr("safepoint_stop_observed")
        if event == 14:
            return cstr("safepoint_suspend_deferred")
        if event == 15:
            return cstr("safepoint_resume_deferred")
        if event == 16:
            return cstr("stop_world_request")
        if event == 17:
            return cstr("world_stopped")
        if event == 18:
            return cstr("stop_world_nested")
        if event == 19:
            return cstr("resume_world_request")
        if event == 20:
            return cstr("world_resumed")
        if event == 21:
            return cstr("resume_world_nested")
        if event == 22:
            return cstr("stop_world_failed")
        if event == 23:
            return cstr("resume_world_failed")
        if event == 24:
            return cstr("trace_dropped")
        if event == 25:
            return cstr("safepoint_epochs_omitted")
        return cstr("thread_event")
    return cstr("event")


def _write_event_unlocked(
    category: c_ptr,
    event: c_ptr,
    value0: int,
    value1: int,
    pointer: c_ptr,
    thread_id: int,
) -> None:
    # Caller owns only the log sink lock. All helpers below use raw stack,
    # integer, clock and file primitives: no allocation or safepoint calls.
    close_slot = stack_alloc(4)
    stream = _open_stream(close_slot)
    timestamp: int = unsigned_div_i64(pcc_runtime_now_us(), 1000000)
    category_text = category
    if ptr_is_null(category_text):
        category_text = cstr("")
    event_text = event
    if ptr_is_null(event_text):
        event_text = cstr("")
    if load_i32(global_addr("pcc_log_json"), 0) != 0:
        _write_text(stream, cstr("{\"schema\":\"pcc.diagnostics.runtime_log.v1\",\"ts\":"))
        _write_i64(stream, timestamp)
        _write_text(stream, cstr(",\"thread\":"))
        _write_i64(stream, thread_id)
        _write_text(stream, cstr(",\"category\":\""))
        _write_text(stream, category_text)
        _write_text(stream, cstr("\",\"event\":\""))
        _write_text(stream, event_text)
        _write_text(stream, cstr("\",\"value0\":"))
        _write_i64(stream, value0)
        _write_text(stream, cstr(",\"value1\":"))
        _write_i64(stream, value1)
        _write_text(stream, cstr(",\"ptr\":\""))
        _write_pointer(stream, pointer)
        _write_text(stream, cstr("\"}\n"))
    else:
        _write_text(stream, cstr("[pcc."))
        if ptr_is_null(category):
            _write_text(stream, cstr("log"))
        else:
            _write_text(stream, category)
        _write_text(stream, cstr("] ts="))
        _write_i64(stream, timestamp)
        _write_text(stream, cstr(" thread="))
        _write_i64(stream, thread_id)
        _write_text(stream, cstr(" event="))
        _write_text(stream, event_text)
        _write_text(stream, cstr(" value0="))
        _write_i64(stream, value0)
        _write_text(stream, cstr(" value1="))
        _write_i64(stream, value1)
        _write_text(stream, cstr(" ptr="))
        _write_pointer(stream, pointer)
        _write_text(stream, cstr("\n"))
    if load_i32(close_slot, 0) != 0:
        close(stream)


def _note_thread_trace_drop_count(count: int) -> None:
    # Loss is explicit when recursive diagnostics or sink contention would
    # otherwise make tracing itself wait on the runtime it is diagnosing.
    if atomic_load_i32(global_addr("pcc_log_init_state"), 0, "acquire") == 2:
        if load_i32(global_addr("pcc_log_mask"), 0) & 256:
            atomic_rmw_i64(
                "add", global_addr("pcc_log_thread_trace_dropped"), 0, count, "relaxed"
            )


def _note_thread_trace_drop() -> None:
    _note_thread_trace_drop_count(1)


def _append_trace_text(buffer: c_ptr, offset: int, limit: int, text: c_ptr) -> int:
    if offset < 0 or offset > limit:
        return -1
    length: int = strlen(text)
    if length < 0 or length > limit - offset:
        return -1
    index: int = 0
    while index < length:
        store_i8(buffer, offset + index, load_i8(text, index))
        index = index + 1
    return offset + length


def _append_trace_i64(buffer: c_ptr, offset: int, limit: int, value: int) -> int:
    digits = stack_alloc(32)
    end: int = 31
    store_i8(digits, end, 0)
    negative: int = 0
    magnitude: int = value
    if value < 0:
        negative = 1
        magnitude = 0 - value
    if magnitude == 0:
        end = end - 1
        store_i8(digits, end, 48)
    while magnitude != 0:
        end = end - 1
        store_i8(digits, end, 48 + unsigned_rem_i64(magnitude, 10))
        magnitude = unsigned_div_i64(magnitude, 10)
    if negative:
        end = end - 1
        store_i8(digits, end, 45)
    return _append_trace_text(buffer, offset, limit, ptr_add(digits, end))


def _append_trace_pointer(buffer: c_ptr, offset: int, limit: int, value: c_ptr) -> int:
    if ptr_is_null(value):
        return _append_trace_text(buffer, offset, limit, cstr("0x0"))
    slot = stack_alloc(8)
    store_ptr(slot, 0, value)
    bits: int = load_i64(slot, 0)
    digits = stack_alloc(32)
    end: int = 31
    store_i8(digits, end, 0)
    while bits != 0:
        digit: int = unsigned_rem_i64(bits, 16)
        end = end - 1
        if digit < 10:
            store_i8(digits, end, 48 + digit)
        else:
            store_i8(digits, end, 87 + digit)
        bits = unsigned_div_i64(bits, 16)
    end = end - 1
    store_i8(digits, end, 120)
    end = end - 1
    store_i8(digits, end, 48)
    return _append_trace_text(buffer, offset, limit, ptr_add(digits, end))


def _append_thread_record(
    buffer: c_ptr, offset: int, limit: int, event: int,
    value0: int, value1: int, pointer: c_ptr, thread_id: int,
) -> int:
    timestamp: int = unsigned_div_i64(pcc_runtime_now_us(), 1000000)
    event_text = _event_from_code(9, event)
    if load_i32(global_addr("pcc_log_json"), 0) != 0:
        offset = _append_trace_text(buffer, offset, limit, cstr("{\"schema\":\"pcc.diagnostics.runtime_log.v1\",\"ts\":"))
        offset = _append_trace_i64(buffer, offset, limit, timestamp)
        offset = _append_trace_text(buffer, offset, limit, cstr(",\"thread\":"))
        offset = _append_trace_i64(buffer, offset, limit, thread_id)
        offset = _append_trace_text(buffer, offset, limit, cstr(",\"category\":\"thread\",\"event\":\""))
        offset = _append_trace_text(buffer, offset, limit, event_text)
        offset = _append_trace_text(buffer, offset, limit, cstr("\",\"value0\":"))
        offset = _append_trace_i64(buffer, offset, limit, value0)
        offset = _append_trace_text(buffer, offset, limit, cstr(",\"value1\":"))
        offset = _append_trace_i64(buffer, offset, limit, value1)
        offset = _append_trace_text(buffer, offset, limit, cstr(",\"ptr\":\""))
        offset = _append_trace_pointer(buffer, offset, limit, pointer)
        return _append_trace_text(buffer, offset, limit, cstr("\"}\n"))
    offset = _append_trace_text(buffer, offset, limit, cstr("[pcc.thread] ts="))
    offset = _append_trace_i64(buffer, offset, limit, timestamp)
    offset = _append_trace_text(buffer, offset, limit, cstr(" thread="))
    offset = _append_trace_i64(buffer, offset, limit, thread_id)
    offset = _append_trace_text(buffer, offset, limit, cstr(" event="))
    offset = _append_trace_text(buffer, offset, limit, event_text)
    offset = _append_trace_text(buffer, offset, limit, cstr(" value0="))
    offset = _append_trace_i64(buffer, offset, limit, value0)
    offset = _append_trace_text(buffer, offset, limit, cstr(" value1="))
    offset = _append_trace_i64(buffer, offset, limit, value1)
    offset = _append_trace_text(buffer, offset, limit, cstr(" ptr="))
    offset = _append_trace_pointer(buffer, offset, limit, pointer)
    return _append_trace_text(buffer, offset, limit, cstr("\n"))


def _write_trace_bytes(stream: int, buffer: c_ptr, length: int) -> int:
    offset: int = 0
    while offset < length:
        written: int = write(stream, ptr_add(buffer, offset), length - offset)
        if written == -4:
            continue
        if written <= 0 or written > length - offset:
            return 0
        offset = offset + written
    return 1


def _write_thread_batch_unlocked(
    thread_id: int, first_event: int, second_event: int,
    value0: int, value1: int, pointer: c_ptr,
) -> None:
    # Fixed thread event names plus bounded integers/pointers fit below 512
    # bytes per record. Build the complete one/two-record delivery and its
    # pending loss notice before any I/O, while owning only the sink lock.
    # A normal full write commits the whole batch in one syscall. Short writes
    # retry the exact suffix; fatal I/O or termination between partial writes
    # retains the sink's existing possible-prefix/error semantics.
    capacity: int = 1536
    buffer = stack_alloc(1536)
    count: int = 1
    offset: int = _append_thread_record(
        buffer, 0, capacity, first_event, value0, value1, pointer, thread_id
    )
    if second_event != 0:
        count = 2
        offset = _append_thread_record(
            buffer, offset, capacity, second_event, value0, value1, pointer, thread_id
        )
    dropped: int = atomic_rmw_i64(
        "xchg", global_addr("pcc_log_thread_trace_dropped"), 0, 0, "acq_rel"
    )
    if dropped > 0:
        offset = _append_thread_record(
            buffer, offset, capacity, 24, dropped, 0, null(), thread_id
        )
    if offset < 0:
        _note_thread_trace_drop_count(count + dropped)
        return
    close_slot = stack_alloc(4)
    stream = _open_stream(close_slot)
    if _write_trace_bytes(stream, buffer, offset) == 0:
        _note_thread_trace_drop_count(count + dropped)
    if load_i32(close_slot, 0) != 0:
        close(stream)
    # Recursive/concurrent losses arriving during this write remain counted
    # for the next delivery; do not start a vulnerable trailing fragment.


def _write_thread_trace_drops(thread_id: int) -> None:
    dropped: int = atomic_rmw_i64(
        "xchg", global_addr("pcc_log_thread_trace_dropped"), 0, 0, "acq_rel"
    )
    if dropped > 0:
        _write_event_unlocked(
            cstr("thread"), cstr("trace_dropped"), dropped, 0, null(), thread_id
        )


@c_abi_export("pcc_diagnostics_runtime_log_event")
def pcc_diagnostics_runtime_log_event(
    category: c_ptr,
    event: c_ptr,
    value0: int,
    value1: int,
    pointer: c_ptr,
) -> None:
    if load_i32(global_addr("pcc_log_emitting"), 0) != 0:
        return
    store_i32(global_addr("pcc_log_emitting"), 0, 1)
    if pcc_diagnostics_runtime_log_enabled(category) == 0:
        store_i32(global_addr("pcc_log_emitting"), 0, 0)
        return
    # Newcomer admission precedes sink ownership. Raw sink helpers cannot
    # park a lock holder; recursive calls never wait on their own sink lock.
    thread_id: int = pcc_current_thread_id()
    _write_lock_acquire()
    _write_event_unlocked(category, event, value0, value1, pointer, thread_id)
    _write_thread_trace_drops(thread_id)
    _write_lock_release()
    store_i32(global_addr("pcc_log_emitting"), 0, 0)


def _thread_transition_event(
    event: int, value0: int, value1: int, pointer: c_ptr
) -> None:
    # Called only outside scheduler/world/state locks and before unregister.
    # Acquisition is nonwaiting; OS file I/O can still block. These are
    # diagnostics, so contention is reported as loss, never runtime parking.
    if load_i32(global_addr("pcc_log_emitting"), 0) != 0 or pcc_thread_no_park_depth() != 0:
        _note_thread_trace_drop()
        return
    store_i32(global_addr("pcc_log_emitting"), 0, 1)
    if _code_enabled(9) == 0:
        store_i32(global_addr("pcc_log_emitting"), 0, 0)
        return
    thread_id: int = pcc_current_thread_id()
    if atomic_cas_i32(
        global_addr("pcc_log_write_lock"), 0, 0, 1, "acq_rel", "acquire"
    ) != 0:
        _note_thread_trace_drop()
        store_i32(global_addr("pcc_log_emitting"), 0, 0)
        return
    _write_thread_batch_unlocked(thread_id, event, 0, value0, value1, pointer)
    _write_lock_release()
    store_i32(global_addr("pcc_log_emitting"), 0, 0)


@c_abi_export("pcc_diagnostics_runtime_log_suspension_pair")
def pcc_diagnostics_runtime_log_suspension_pair(
    thread_id: int, epoch: int, waits: int
) -> None:
    # Internal completed-suspension boundary, not a general trace API. The
    # kernel captured the identity before suspension and calls only after
    # releasing its world lock. It has already proved that this caller may
    # park with no scheduler lock/no-park lease. Never initialize logging or
    # register here; doing either could change that suspension/teardown state.
    if atomic_load_i32(
        global_addr("pcc_diagnostics_runtime_log_fast_state"), 0, "relaxed"
    ) == 0:
        return
    if atomic_load_i32(global_addr("pcc_log_init_state"), 0, "acquire") != 2:
        return
    if load_i32(global_addr("pcc_log_mask"), 0) & 256 == 0:
        return
    if (
        thread_id <= 0
        or epoch <= 0
        or waits <= 0
        or load_i32(global_addr("pcc_log_emitting"), 0) != 0
        or pcc_thread_no_park_depth() != 0
    ):
        _note_thread_trace_drop_count(2)
        return
    # Only this proven post-suspension boundary may wait for the sink. While
    # waiting it owns no sink/runtime lock and retains explicit safepoints.
    # A nested safepoint sees emitting=1 and drops its whole pair; it never
    # recursively waits on this operation. A sink holder cannot park or
    # allocate: its emitted closure is raw and implicit polls are disabled.
    store_i32(global_addr("pcc_log_emitting"), 0, 1)
    _write_lock_acquire()
    # Contention cannot split the pair; normal I/O commits complete records
    # together, including any pending drop notice, before releasing the sink.
    _write_thread_batch_unlocked(thread_id, 14, 15, epoch, waits, null())
    _write_lock_release()
    store_i32(global_addr("pcc_log_emitting"), 0, 0)


@c_abi_export("pcc_diagnostics_runtime_log_event_code")
def pcc_diagnostics_runtime_log_event_code(
    category: int,
    event: int,
    value0: int,
    value1: int,
    pointer: c_ptr,
) -> None:
    if atomic_load_i32(
        global_addr("pcc_diagnostics_runtime_log_fast_state"), 0, "relaxed"
    ) == 0:
        return
    if (
        atomic_load_i32(global_addr("pcc_log_init_state"), 0, "relaxed") == 2
        and load_i32(global_addr("pcc_log_mask"), 0) == 0
    ):
        return
    if category == 9 and event >= 9:
        _thread_transition_event(event, value0, value1, pointer)
        return
    if load_i32(global_addr("pcc_log_emitting"), 0) != 0:
        if category == 9:
            _note_thread_trace_drop()
        return
    if _code_enabled(category) == 0:
        return
    pcc_diagnostics_runtime_log_event(
        _category_from_code(category),
        _event_from_code(category, event),
        value0,
        value1,
        pointer,
    )


def _append_text(buffer: c_ptr, offset: int, limit: int, text: c_ptr) -> int:
    if ptr_is_null(text):
        return offset
    index: int = 0
    while load_i8(text, index) != 0 and offset + 1 < limit:
        store_i8(buffer, offset, load_i8(text, index))
        offset = offset + 1
        index = index + 1
    return offset


@c_abi_export("pcc_runtime_tripwire_fail")
def pcc_runtime_tripwire_fail(message: c_ptr, file: c_ptr, line: int) -> None:
    buffer = stack_alloc(512)
    offset: int = _append_text(buffer, 0, 512, cstr("TRIPWIRE "))
    if ptr_is_null(file):
        offset = _append_text(buffer, offset, 512, cstr("?"))
    else:
        offset = _append_text(buffer, offset, 512, file)
    offset = _append_text(buffer, offset, 512, cstr(": "))
    if ptr_is_null(message):
        offset = _append_text(buffer, offset, 512, cstr("invariant violated"))
    else:
        offset = _append_text(buffer, offset, 512, message)
    store_i8(buffer, offset, 0)
    pcc_diagnostics_runtime_log_event(cstr("runtime"), buffer, line, 0, null())
    pcc_platform_abort()
