"""pcc-Python port of py_os_substrate.c.

The stat helpers delegate ABI-sensitive ``struct stat`` reads to
pcc.unsafe intrinsics. The runtime source stays portable and does not encode
Darwin/Linux field offsets directly.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW, PY_TYPE_STR,
)

from pcc.extern import (
    extern,
    c_abi_export,
    c_double,
    c_int32,
    c_int64,
    c_ptr,
    c_size_t,
    c_void,
)
from pcc.unsafe import (
    cstr,
    define_global_i32,
    define_global_ptr_null,
    frame_address,
    free,
    global_load_ptr,
    global_store_ptr,
    global_addr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    realloc,
    stack_alloc,
    store_i64,
    store_ptr,
    strlen,
    target_platform_machine,
    target_sys_platform,
)

localtime_r = extern("localtime_r", (c_ptr, c_ptr), c_ptr)
strftime = extern("strftime", (c_ptr, c_size_t, c_ptr, c_ptr), c_size_t)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
pcc_runtime_now_us = extern("pcc_platform_wall_time_us", (), c_int64)
pcc_runtime_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
pcc_platform_monotonic_ns = extern("pcc_platform_monotonic_ns", (), c_int64)
pcc_platform_wall_time_ns = extern("pcc_platform_wall_time_ns", (), c_int64)
pcc_platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)
pcc_thread_blocking_enter = extern("pcc_thread_blocking_enter", (c_ptr,), c_int64)
pcc_thread_blocking_leave = extern("pcc_thread_blocking_leave", (c_int64,), c_void)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
arc4random_buf = extern("arc4random_buf", (c_ptr, c_size_t), c_void)
read_sys = extern("pcc_platform_read", (c_int64, c_ptr, c_int64), c_int64)
write_sys = extern("pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64)
pcc_platform_access = extern(
    "pcc_platform_access", (c_ptr, c_int64), c_int64
)
pcc_platform_getcwd = extern(
    "pcc_platform_getcwd", (c_ptr, c_int64), c_ptr
)
pcc_platform_stat_kind = extern(
    "pcc_platform_stat_kind", (c_ptr,), c_int64
)
pcc_platform_stat_mtime = extern(
    "pcc_platform_stat_mtime", (c_ptr,), c_double
)
pcc_platform_realpath = extern(
    "pcc_platform_realpath", (c_ptr, c_ptr, c_int64), c_ptr
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
py_program_argv = extern("py_program_argv", (c_int64,), c_ptr)
py_program_mode = extern("py_program_mode", (), c_int32)

py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_errno_get = extern("pcc_errno_get", (), c_int32)
pcc_errno_exception_kind = extern("pcc_errno_exception_kind", (c_int32,), c_int64)
pcc_errno_message_into = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)

_ERRNO_EXCEPTION = 0
_ERRNO_NUMBER = 1
_ERRNO_MESSAGE = 2
_ERRNO_ARGS = 3
_ERRNO_PENDING = 4
_ERRNO_SLOT_COUNT = 5
define_global_i32("pcc_errno_error_frame_map", _ERRNO_SLOT_COUNT)


def _errno_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)):
        if not py_err_occurred():
            py_raise_owned(py_exc_new(19, cstr("OSError allocation failed")))
        return -1
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        if not py_err_occurred():
            py_raise_owned(py_exc_new(7, cstr("OSError ownership lease failed")))
        return -1
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return -1 if py_err_occurred() else 0


def _errno_error_body(slots, tokens, number: int, message) -> None:
    # The existing walk/file error builders use the same owning-slot and
    # counted-lease protocol while constructing errno, strerror and args.
    kind: int = pcc_errno_exception_kind(number)
    store_ptr(slots, _ERRNO_EXCEPTION * C_POINTER_SIZE, py_exc_new(kind, message))
    if _errno_adopt(slots, tokens, _ERRNO_EXCEPTION) != 0:
        return
    store_ptr(slots, _ERRNO_NUMBER * C_POINTER_SIZE, py_int_from_i64(number))
    if _errno_adopt(slots, tokens, _ERRNO_NUMBER) != 0:
        return
    store_ptr(slots, _ERRNO_MESSAGE * C_POINTER_SIZE, py_str_new(message, strlen(message)))
    if _errno_adopt(slots, tokens, _ERRNO_MESSAGE) != 0:
        return
    store_ptr(slots, _ERRNO_ARGS * C_POINTER_SIZE, py_tuple_new(2))
    if _errno_adopt(slots, tokens, _ERRNO_ARGS) != 0:
        return
    py_tuple_set_item(load_ptr(slots, _ERRNO_ARGS * C_POINTER_SIZE), 0,
                      load_ptr(slots, _ERRNO_NUMBER * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _ERRNO_ARGS * C_POINTER_SIZE), 1,
                      load_ptr(slots, _ERRNO_MESSAGE * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _ERRNO_EXCEPTION * C_POINTER_SIZE), cstr("errno"),
                       load_ptr(slots, _ERRNO_NUMBER * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _ERRNO_EXCEPTION * C_POINTER_SIZE), cstr("strerror"),
                       load_ptr(slots, _ERRNO_MESSAGE * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _ERRNO_EXCEPTION * C_POINTER_SIZE), cstr("args"),
                       load_ptr(slots, _ERRNO_ARGS * C_POINTER_SIZE))
    if not py_err_occurred():
        error = load_ptr(slots, _ERRNO_EXCEPTION * C_POINTER_SIZE)
        py_incref(error)
        py_raise_owned(error)


def _errno_os_error(number: int) -> None:
    """Raise CPython's errno-selected OSError subclass with (errno, strerror)."""
    if number <= 0:
        py_raise_owned(py_exc_new(7, cstr("platform failure did not publish errno")))
        return
    slots = stack_alloc(_ERRNO_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_ERRNO_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _ERRNO_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _ERRNO_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_errno_error_frame_map"), slots)
    message = stack_alloc(256)
    pcc_errno_message_into(number, message, 256)
    _errno_error_body(slots, tokens, number, message)
    pending = ptr_add(slots, _ERRNO_PENDING * C_POINTER_SIZE)
    py_tls_exc_swap_slot(pending)
    index: int = _ERRNO_ARGS
    while index >= _ERRNO_EXCEPTION:
        slot = ptr_add(slots, index * C_POINTER_SIZE)
        token: int = load_i64(tokens, index * C_POINTER_SIZE)
        if pcc_gc_foreign_lease_release(slot, token) != 0:
            pcc_platform_abort()
            return
        pcc_gc_store_root(slot, null())
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(pending)
    pcc_gc_frame_leave(slots)


@c_abi_export("py_os_raise_errno")
def py_os_raise_errno(number: int) -> None:
    """Raise OSError for ``number`` without a filename, as an os call does."""
    _errno_os_error(number)


def _platform_failure_errno(result: int) -> int:
    # Darwin libSystem calls return -1 and publish errno; Linux raw syscalls
    # return the negated errno. Read errno before anything else can run.
    if result == -1 and load_i8(target_sys_platform(), 0) == 100:
        return pcc_errno_get()
    return 0 - result


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


define_global_ptr_null("py_path_getcwd_buf")
define_global_ptr_null("py_path_realpath_buf")


@c_abi_export("py_path_stat_kind")
def py_path_stat_kind(p) -> int:
    return pcc_platform_stat_kind(p)


@c_abi_export("py_path_stat_mtime")
def py_path_stat_mtime(p) -> float:
    return pcc_platform_stat_mtime(p)


@c_abi_export("py_time_monotonic")
def py_time_monotonic():
    return py_float_from_f64(pcc_runtime_monotonic_us() * 0.000001)


@c_abi_export("py_time_perf_counter")
def py_time_perf_counter():
    return py_float_from_f64(pcc_runtime_monotonic_us() * 0.000001)


@c_abi_export("py_time_time")
def py_time_time():
    return py_float_from_f64(pcc_runtime_now_us() * 0.000001)


@c_abi_export("py_time_perf_counter_ns")
def py_time_perf_counter_ns():
    return py_int_from_i64(pcc_platform_monotonic_ns())


@c_abi_export("py_time_monotonic_ns")
def py_time_monotonic_ns():
    return py_int_from_i64(pcc_platform_monotonic_ns())


@c_abi_export("py_time_time_ns")
def py_time_time_ns():
    return py_int_from_i64(pcc_platform_wall_time_ns())


@c_abi_export("py_time_sleep")
def py_time_sleep(delay):
    """``time.sleep(seconds)``.

    There was no native lowering for it, so any module calling ``time.sleep``
    resolved the call through CPython -- which fail-closes the whole enclosing
    function under ``--python-libpython=off``. asyncio's event loop uses it for
    its idle wait, so a loop that ever went idle died with "no-libpython
    function unavailable: asyncio._run_once".

    Accepts an int or a float the way CPython does. A zero delay is a no-op and
    a negative one is a ValueError, both matching CPython -- an earlier version
    silently accepted a negative delay, which a differential run against
    CPython caught.
    """
    seconds: float = py_float_to_f64(delay)
    if seconds < 0.0:
        py_raise_owned(
            py_exc_new(2, cstr("sleep length must be non-negative"))
        )  # PY_EXC_VALUEERROR
        return null()
    if seconds > 0.0:
        # The sleep runs exited (Go's entersyscall): a collector walks this
        # thread's stack from this frame instead of waiting until it wakes.
        entered: int = pcc_thread_blocking_enter(frame_address())
        pcc_platform_sleep_ns(int(seconds * 1000000000.0))
        pcc_thread_blocking_leave(entered)
    return global_load_ptr("py_None")


@c_abi_export("py_time_strftime")
def py_time_strftime(fmt):
    fmt_str = py_obj_str(fmt)
    if ptr_is_null(fmt_str):
        return null()
    raw_fmt = py_str_utf8(fmt_str)
    if ptr_is_null(raw_fmt):
        py_decref(fmt_str)
        return py_str_new(null(), 0)
    now_buf = malloc(8)
    tm_buf = malloc(128)
    out_buf = malloc(256)
    if ptr_is_null(now_buf) or ptr_is_null(tm_buf) or ptr_is_null(out_buf):
        free(now_buf)
        free(tm_buf)
        free(out_buf)
        py_decref(fmt_str)
        return null()
    store_i64(now_buf, 0, pcc_runtime_now_us() // 1000000)
    if ptr_is_null(localtime_r(now_buf, tm_buf)):
        free(now_buf)
        free(tm_buf)
        free(out_buf)
        py_decref(fmt_str)
        return py_str_new(null(), 0)
    n: int = strftime(out_buf, 256, raw_fmt, tm_buf)
    out = py_str_new(out_buf, n)
    free(now_buf)
    free(tm_buf)
    free(out_buf)
    py_decref(fmt_str)
    return out


@c_abi_export("py_sys_stdin_readline")
def py_sys_stdin_readline():
    """sys.stdin.readline(): the next whole line with its newline, '' at EOF.

    Descriptor 0 is read one byte at a time, so this unbuffered reader never
    consumes input past the newline that another reader would see.
    """
    capacity: int = 256
    buf = malloc(capacity)
    if ptr_is_null(buf):
        py_raise_owned(py_exc_new(19, cstr("could not allocate the stdin line buffer")))
        return null()
    pos: int = 0
    reading: int = 1
    while reading != 0:
        if pos == capacity:
            grown = realloc(buf, capacity * 2)
            if ptr_is_null(grown):
                free(buf)
                py_raise_owned(py_exc_new(19, cstr("could not grow the stdin line buffer")))
                return null()
            buf = grown
            capacity = capacity * 2
        got: int = read_sys(0, ptr_add(buf, pos), 1)
        if got < 0:
            number: int = _platform_failure_errno(got)
            if number != 4:
                # EINTR (4 on Darwin and Linux) retries; others raise OSError.
                free(buf)
                _errno_os_error(number)
                return null()
        elif got == 0:
            reading = 0
        else:
            pos = pos + 1
            if load_i8(buf, pos - 1) == 10:
                reading = 0
    out = py_str_new(buf, pos)
    free(buf)
    return out


@c_abi_export("py_os_urandom")
def py_os_urandom(n_obj):
    n: int = py_int_value_i64(n_obj)
    if n < 0:
        return null()
    out = py_bytes_new(null(), n)
    if ptr_is_null(out):
        return null()
    if n > 0:
        arc4random_buf(ptr_add(out, 24), n)
    return out


@c_abi_export("py_path_getcwd")
def py_path_getcwd():
    buf = global_load_ptr("py_path_getcwd_buf")
    if ptr_is_null(buf):
        buf = malloc(8192)
        if ptr_is_null(buf):
            py_raise_owned(py_exc_new(19, cstr("could not allocate current directory buffer")))
            return null()
        global_store_ptr("py_path_getcwd_buf", buf)
    result = pcc_platform_getcwd(buf, 8192)
    if ptr_is_null(result):
        # The platform leaf publishes errno before returning NULL. Capture
        # it before formatting, allocation or any other operating-system call.
        number: int = pcc_errno_get()
        _errno_os_error(number)
        return null()
    return buf


@c_abi_export("py_path_realpath")
def py_path_realpath(p):
    if ptr_is_null(p):
        return null()
    buf = global_load_ptr("py_path_realpath_buf")
    if ptr_is_null(buf):
        buf = malloc(8192)
        if ptr_is_null(buf):
            return null()
        global_store_ptr("py_path_realpath_buf", buf)
    result = pcc_platform_realpath(p, buf, 8192)
    if ptr_is_null(result):
        return null()
    return buf


def _str_from_cstr(p):
    if ptr_is_null(p):
        return null()
    return py_str_new(p, strlen(p))


@c_abi_export("py_sys_platform_str")
def py_sys_platform_str():
    return _str_from_cstr(target_sys_platform())


@c_abi_export("py_platform_machine_str")
def py_platform_machine_str():
    return _str_from_cstr(target_platform_machine())


@c_abi_export("py_platform_release_str")
def py_platform_release_str():
    return py_str_new(cstr("0"), 1)


@c_abi_export("py_os_getcwd_str")
def py_os_getcwd_str():
    path = py_path_getcwd()
    if ptr_is_null(path):
        return null()
    result = _str_from_cstr(path)
    if ptr_is_null(result) and not py_err_occurred():
        py_raise_owned(py_exc_new(19, cstr("could not allocate current directory string")))
    return result


@c_abi_export("py_sys_path_list")
def py_sys_path_list():
    path0 = null()
    mode: int = py_program_mode()
    if mode == 3 or mode == 4:
        path0 = py_str_new(null(), 0)
    elif mode == 1:
        raw = py_program_argv(0)
        if not ptr_is_null(raw):
            resolved = py_path_realpath(raw)
            if not ptr_is_null(resolved):
                i: int = 0
                last_slash: int = -1
                while load_i8(resolved, i) != 0:
                    if load_i8(resolved, i) == 47:
                        last_slash = i
                    i += 1
                if last_slash >= 0:
                    length: int = last_slash
                    if last_slash == 0:
                        length = 1
                    path0 = py_str_new(resolved, length)
    if ptr_is_null(path0):
        path0 = py_os_getcwd_str()
    if ptr_is_null(path0):
        path0 = py_str_new(null(), 0)
    lst = py_list_new(0)
    py_list_append(lst, path0)
    py_decref(path0)
    return lst


@c_abi_export("py_os_access")
def py_os_access(path, mode: int) -> int:
    if ptr_is_null(path):
        return 0
    owned = py_obj_str(path)
    if ptr_is_null(owned):
        return 0
    raw = py_str_utf8(owned)
    ok: int = 0
    if not ptr_is_null(raw):
        if pcc_platform_access(raw, mode) == 0:
            ok = 1
    py_decref(owned)
    return ok


@c_abi_export("py_os_write")
def py_os_write(fd: int, data) -> int:
    if ptr_is_null(data):
        return -1
    ptr = null()
    length = 0
    curr = data
    while not ptr_is_null(curr):
        tag = _type_of(curr)
        if tag == PY_TYPE_STR:  # PY_TYPE_STR
            ptr = py_str_utf8(curr)
            length = py_str_byte_len(curr)
            break
        elif tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
            ptr = ptr_add(curr, 24)
            length = load_i64(curr, 16)
            break
        elif tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
            ptr = ptr_add(curr, 24)
            length = load_i64(curr, 16)
            break
        elif tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
            # base pointer is at offset 16
            curr = pcc_gc_load_ptr(curr, ptr_add(curr, 16))
        else:
            return -1

    if ptr_is_null(ptr) or length < 0:
        return -1
    return write_sys(fd, ptr, length)
