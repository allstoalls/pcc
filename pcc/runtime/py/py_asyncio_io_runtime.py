"""Native asyncio socket/relay helpers authored in pcc-Python."""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_MEMORYVIEW, PY_TYPE_STR
from pcc.unsafe import (
    cstr,
    define_global_i64,
    free,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i64,
)


py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_platform_tcp_listen = extern(
    "pcc_platform_tcp_listen", (c_ptr, c_ptr, c_int64), c_int64
)
pcc_platform_tcp_listen_with_backlog = extern(
    "pcc_platform_tcp_listen_with_backlog",
    (c_ptr, c_ptr, c_int64, c_int64),
    c_int64,
)
pcc_platform_tcp_accept = extern("pcc_platform_tcp_accept", (c_int64,), c_int64)
pcc_platform_tcp_accept_observe = extern(
    "pcc_platform_tcp_accept_observe", (c_int64, c_ptr), c_int64
)
pcc_platform_tcp_connect = extern(
    "pcc_platform_tcp_connect", (c_ptr, c_ptr), c_int64
)
pcc_platform_tcp_connect_start = extern(
    "pcc_platform_tcp_connect_start", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_platform_socket_connect_observe = extern(
    "pcc_platform_socket_connect_observe", (c_int64, c_int64), c_int64
)
pcc_platform_socket_read_observe = extern(
    "pcc_platform_socket_read_observe",
    (c_int64, c_ptr, c_int64, c_int64, c_ptr),
    c_int64,
)
pcc_platform_socket_write_observe = extern(
    "pcc_platform_socket_write_observe",
    (c_int64, c_ptr, c_int64, c_int64, c_ptr),
    c_int64,
)
pcc_platform_socket_send = extern(
    "pcc_platform_socket_send", (c_int64, c_ptr, c_int64, c_int64), c_int64
)
pcc_platform_socket_recv = extern(
    "pcc_platform_socket_recv", (c_int64, c_ptr, c_int64, c_int64), c_int64
)
pcc_platform_socket_shutdown = extern(
    "pcc_platform_socket_shutdown", (c_int64, c_int64), c_int64
)
pcc_platform_socket_sockname = extern(
    "pcc_platform_socket_sockname", (c_int64, c_ptr, c_int64), c_int64
)
pcc_platform_socket_peername = extern(
    "pcc_platform_socket_peername", (c_int64, c_ptr, c_int64), c_int64
)
pcc_platform_poll_fd = extern(
    "pcc_platform_poll_fd", (c_int64, c_int64, c_int64), c_int64
)
pcc_platform_poll_readable_pair = extern(
    "pcc_platform_poll_readable_pair", (c_int64, c_int64, c_int64), c_int64
)
pcc_platform_close = extern("pcc_platform_close", (c_int64,), c_int64)
pcc_runtime_monotonic_us = extern("pcc_runtime_monotonic_us", (), c_int64)
py_virtual_thread_io_resource_register = extern(
    "py_virtual_thread_io_resource_register", (c_int64,), c_int64
)
py_virtual_thread_io_resource_operation_begin = extern(
    "py_virtual_thread_io_resource_operation_begin",
    (c_int64, c_int64),
    c_int64,
)
py_virtual_thread_io_resource_close_begin = extern(
    "py_virtual_thread_io_resource_close_begin", (c_int64,), c_int64
)
py_virtual_thread_io_resource_operation_end = extern(
    "py_virtual_thread_io_resource_operation_end", (), c_void
)
pcc_io_waitset_kqueue_available = extern(
    "pcc_io_waitset_kqueue_available", (), c_int32
)
pcc_io_waitset_epoll_available = extern(
    "pcc_io_waitset_epoll_available", (), c_int32
)


define_global_i64("pcc_asyncio_relay_step_last_progress", 0)


def _none():
    return global_load_ptr("py_None")


def _owned_none():
    value = _none()
    py_incref(value)
    return value


def _raise_oserror(message) -> None:
    # py_runtime.h::PY_EXC_OSERROR.  Keep the pcc-Python mirror aligned with
    # the C oracle; 9 is ZeroDivisionError, not OSError.
    py_raise_owned(py_exc_new(14, message))


def _register_tcp_fd(fd: int, message) -> int:
    if fd < 0 or py_virtual_thread_io_resource_register(fd) <= 0:
        if fd >= 0:
            pcc_platform_close(fd)
        _raise_oserror(message)
        return -1
    return 0


def _type_tag(value) -> int:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return -1
    return load_i32(value, 8)


def _host_cstr(host):
    if ptr_is_null(host) != 0 or host == _none() or _type_tag(host) != PY_TYPE_STR:
        return null()
    text = py_str_utf8(host)
    if ptr_is_null(text) != 0 or load_i8(text, 0) == 0:
        return null()
    return text


def _copy_cstr(source, destination, capacity: int) -> int:
    index: int = 0
    while load_i8(source, index) != 0:
        if index + 1 >= capacity:
            return -1
        store_i8(destination, index, load_i8(source, index))
        index = index + 1
    store_i8(destination, index, 0)
    return 0


def _write_decimal(value: int, output, capacity: int) -> int:
    if value < 0:
        return -1
    scratch = stack_alloc(24)
    count: int = 0
    if value == 0:
        store_i8(scratch, 0, 48)
        count = 1
    while value > 0:
        store_i8(scratch, count, 48 + value % 10)
        count = count + 1
        value = value // 10
    if count + 1 > capacity:
        return -1
    index: int = 0
    while index < count:
        store_i8(output, index, load_i8(scratch, count - index - 1))
        index = index + 1
    store_i8(output, count, 0)
    return count


def _port_cstr(port, output, capacity: int) -> int:
    if ptr_is_null(port) != 0 or port == _none():
        return _write_decimal(0, output, capacity)
    if _type_tag(port) == PY_TYPE_STR:
        text = py_str_utf8(port)
        if ptr_is_null(text) != 0 or load_i8(text, 0) == 0:
            return -1
        return _copy_cstr(text, output, capacity)
    value: int = py_int_value_i64(port)
    if value < 0 or value > 65535:
        return -1
    return _write_decimal(value, output, capacity)


def _fd_value(value) -> int:
    if ptr_is_null(value) != 0 or value == _none():
        return -1
    return py_int_value_i64(value)


def _bytes_data(value):
    tag: int = _type_tag(value)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return ptr_add(value, 24)
    if tag == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(value, ptr_add(value, 16))
        return _bytes_data(base)
    if tag == PY_TYPE_STR:
        return py_str_utf8(value)
    return null()


def _bytes_length(value) -> int:
    tag: int = _type_tag(value)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return load_i64(value, 16)
    if tag == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(value, ptr_add(value, 16))
        return _bytes_length(base)
    if tag == PY_TYPE_STR:
        return py_str_byte_len(value)
    return -1


def _is_retryable(error: int) -> int:
    if error == -4 or error == -11 or error == -35:
        return 1
    return 0


def _send_all_raw(fd: int, data, length: int) -> int:
    sent: int = 0
    while sent < length:
        count: int = pcc_platform_socket_send(fd, ptr_add(data, sent), length - sent, 0)
        if count < 0:
            if count == -4:
                continue
            if count == -11 or count == -35:
                if pcc_platform_poll_fd(fd, 4, -1) < 0:
                    return -1
                continue
            return -1
        if count == 0:
            return -1
        sent = sent + count
    return sent


def _close_unique4(fd0: int, fd1: int, fd2: int, fd3: int) -> None:
    if fd0 >= 0:
        pcc_platform_close(fd0)
    if fd1 >= 0 and fd1 != fd0:
        pcc_platform_close(fd1)
    if fd2 >= 0 and fd2 != fd0 and fd2 != fd1:
        pcc_platform_close(fd2)
    if fd3 >= 0 and fd3 != fd0 and fd3 != fd1 and fd3 != fd2:
        pcc_platform_close(fd3)


def _drain_direction(fd_input: int, fd_output: int, buffer) -> int:
    chunks: int = 0
    made_progress: int = 0
    while chunks < 32:
        if chunks > 0:
            ready: int = pcc_platform_poll_fd(fd_input, 1, 0)
            if ready <= 0:
                if ready < 0:
                    pcc_platform_socket_shutdown(fd_output, 1)
                    return made_progress * 2
                break
        count: int = pcc_platform_socket_recv(fd_input, buffer, 65536, 0)
        if count > 0:
            made_progress = 1
            chunks = chunks + 1
            if _send_all_raw(fd_output, buffer, count) < 0:
                pcc_platform_socket_shutdown(fd_input, 0)
                pcc_platform_socket_shutdown(fd_output, 1)
                return made_progress * 2
        elif count == 0:
            pcc_platform_socket_shutdown(fd_output, 1)
            return made_progress * 2
        elif count == -4:
            continue
        elif count == -11 or count == -35:
            break
        else:
            pcc_platform_socket_shutdown(fd_output, 1)
            return made_progress * 2
    return 1 | (made_progress * 2)


@c_abi_export("py_asyncio_tcp_listen")
def py_asyncio_tcp_listen(host_object, port_object, reuse_port: int):
    port = stack_alloc(32)
    if _port_cstr(port_object, port, 32) < 0:
        _raise_oserror(cstr("invalid TCP listen port"))
        return null()
    fd: int = pcc_platform_tcp_listen(_host_cstr(host_object), port, reuse_port)
    if fd < 0:
        _raise_oserror(cstr("TCP listen failed"))
        return null()
    return py_int_from_i64(fd)


@c_abi_export("py_asyncio_tcp_accept")
def py_asyncio_tcp_accept(listen_fd_object):
    listen_fd: int = _fd_value(listen_fd_object)
    if listen_fd < 0:
        _raise_oserror(cstr("invalid TCP listen fd"))
        return null()
    while True:
        fd: int = pcc_platform_tcp_accept(listen_fd)
        if fd >= 0:
            return py_int_from_i64(fd)
        if fd == -4:
            continue
        if fd == -11 or fd == -35:
            return _owned_none()
        _raise_oserror(cstr("TCP accept failed"))
        return null()
    return null()


@c_abi_export("py_asyncio_tcp_connect")
def py_asyncio_tcp_connect(host_object, port_object):
    port = stack_alloc(32)
    host = _host_cstr(host_object)
    if ptr_is_null(host) != 0:
        host = cstr("127.0.0.1")
    if _port_cstr(port_object, port, 32) < 0:
        _raise_oserror(cstr("invalid TCP connect port"))
        return null()
    fd: int = pcc_platform_tcp_connect(host, port)
    if fd < 0:
        _raise_oserror(cstr("TCP connect failed"))
        return null()
    return py_int_from_i64(fd)


@c_abi_export("py_virtual_thread_tcp_listen")
def py_virtual_thread_tcp_listen(
    host_object, port_object, backlog: int
) -> int:
    """Create one nonblocking listener for the sequential vthread API."""
    if backlog <= 0 or backlog > 65535:
        _raise_oserror(cstr("invalid TCP listen backlog"))
        return -1
    port = stack_alloc(32)
    if _port_cstr(port_object, port, 32) < 0:
        _raise_oserror(cstr("invalid TCP listen port"))
        return -1
    fd = pcc_platform_tcp_listen_with_backlog(
        _host_cstr(host_object), port, 0, backlog
    )
    if fd < 0:
        _raise_oserror(cstr("TCP listen failed"))
        return -1
    if _register_tcp_fd(fd, cstr("TCP listen registration failed")) != 0:
        return -1
    return fd


@c_abi_export("py_virtual_thread_tcp_accept_observe")
def py_virtual_thread_tcp_accept_observe(
    listener_fd: int, generation: int, output_fd
) -> int:
    """Try one nonblocking accept; WOULD_BLOCK is a normal status."""
    if ptr_is_null(output_fd) != 0:
        _raise_oserror(cstr("invalid TCP accept output"))
        return -1
    store_i64(output_fd, 0, -1)
    if py_virtual_thread_io_resource_operation_begin(
        listener_fd, generation
    ) != 0:
        return -1
    while True:
        status = pcc_platform_tcp_accept_observe(listener_fd, output_fd)
        if status != -4:
            py_virtual_thread_io_resource_operation_end()
            if status < 0:
                _raise_oserror(cstr("TCP accept failed"))
            return status
    return -1


@c_abi_export("py_virtual_thread_tcp_register_accepted")
def py_virtual_thread_tcp_register_accepted(fd: int) -> int:
    return _register_tcp_fd(fd, cstr("TCP accept registration failed"))


@c_abi_export("py_virtual_thread_tcp_connect_start")
def py_virtual_thread_tcp_connect_start(
    host_object, port_object, output_fd
) -> int:
    """Start one nonblocking connect and retain its fd in ``output_fd``."""
    if ptr_is_null(output_fd) != 0:
        _raise_oserror(cstr("invalid TCP connect output"))
        return -1
    store_i64(output_fd, 0, -1)
    port = stack_alloc(32)
    host = _host_cstr(host_object)
    if ptr_is_null(host) != 0:
        host = cstr("127.0.0.1")
    if _port_cstr(port_object, port, 32) < 0:
        _raise_oserror(cstr("invalid TCP connect port"))
        return -1
    status = pcc_platform_tcp_connect_start(host, port, output_fd)
    if status < 0:
        _raise_oserror(cstr("TCP connect failed"))
    elif _register_tcp_fd(
        load_i64(output_fd, 0), cstr("TCP connect registration failed")
    ) != 0:
        store_i64(output_fd, 0, -1)
        return -1
    return status


@c_abi_export("py_virtual_thread_tcp_connect_observe")
def py_virtual_thread_tcp_connect_observe(fd: int, generation: int) -> int:
    """Observe SO_ERROR after writability; retry EINTR without parking."""
    if py_virtual_thread_io_resource_operation_begin(fd, generation) != 0:
        return -1
    while True:
        status = pcc_platform_socket_connect_observe(fd, 0)
        if status != -4:
            py_virtual_thread_io_resource_operation_end()
            if status < 0:
                _raise_oserror(cstr("TCP connect completion failed"))
            return status
    return -1


@c_abi_export("py_virtual_thread_tcp_recv_observe")
def py_virtual_thread_tcp_recv_observe(
    fd: int, generation: int, max_bytes: int, output_status
):
    """Return owned bytes for progress/EOF and NULL for WOULD_BLOCK/error."""
    if ptr_is_null(output_status) != 0 or max_bytes <= 0 or max_bytes > 1048576:
        _raise_oserror(cstr("invalid TCP recv size"))
        return null()
    store_i64(output_status, 0, -1)
    buffer = malloc(max_bytes)
    if ptr_is_null(buffer) != 0:
        _raise_oserror(cstr("TCP recv allocation failed"))
        return null()
    count_slot = stack_alloc(8)
    if py_virtual_thread_io_resource_operation_begin(fd, generation) != 0:
        free(buffer)
        return null()
    while True:
        status = pcc_platform_socket_read_observe(
            fd, buffer, max_bytes, 0, count_slot
        )
        if status != -4:
            break
    py_virtual_thread_io_resource_operation_end()
    if status == 1:
        free(buffer)
        store_i64(output_status, 0, 1)
        return null()
    if status < 0:
        free(buffer)
        _raise_oserror(cstr("TCP recv failed"))
        return null()
    count = load_i64(count_slot, 0)
    result = py_bytes_new(buffer, count)
    free(buffer)
    if ptr_is_null(result) != 0:
        return null()
    store_i64(output_status, 0, status)
    return result


@c_abi_export("py_virtual_thread_tcp_send_observe")
def py_virtual_thread_tcp_send_observe(
    fd: int, generation: int, data_object, offset: int, output_count
) -> int:
    """Try one nonblocking send while preserving partial progress."""
    if ptr_is_null(output_count) != 0:
        _raise_oserror(cstr("invalid TCP send count output"))
        return -1
    tag = _type_tag(data_object)
    if tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY:
        _raise_oserror(cstr("TCP send requires bytes"))
        return -1
    if py_virtual_thread_io_resource_operation_begin(fd, generation) != 0:
        return -1
    length = _bytes_length(data_object)
    data = _bytes_data(data_object)
    if length < 0 or ptr_is_null(data) != 0 or offset < 0 or offset > length:
        py_virtual_thread_io_resource_operation_end()
        _raise_oserror(cstr("TCP send requires bytes and a valid offset"))
        return -1
    store_i64(output_count, 0, 0)
    if offset == length:
        py_virtual_thread_io_resource_operation_end()
        return 0
    while True:
        status = pcc_platform_socket_write_observe(
            fd, ptr_add(data, offset), length - offset, 0, output_count
        )
        if status != -4:
            py_virtual_thread_io_resource_operation_end()
            if status < 0:
                _raise_oserror(cstr("TCP send failed"))
            elif status == 0 and load_i64(output_count, 0) == 0:
                _raise_oserror(cstr("TCP send made no progress"))
                return -1
            return status
    return -1


@c_abi_export("py_virtual_thread_tcp_close")
def py_virtual_thread_tcp_close(fd: int) -> int:
    if py_virtual_thread_io_resource_close_begin(fd) != 0:
        _raise_oserror(cstr("TCP descriptor is not open"))
        return -1
    result = pcc_platform_close(fd)
    py_virtual_thread_io_resource_operation_end()
    if result < 0:
        _raise_oserror(cstr("TCP close failed"))
        return -1
    return 0


@c_abi_export("py_virtual_thread_tcp_close_quiet")
def py_virtual_thread_tcp_close_quiet(fd: int) -> int:
    if py_virtual_thread_io_resource_close_begin(fd) != 0:
        return -1
    result = pcc_platform_close(fd)
    py_virtual_thread_io_resource_operation_end()
    return result


@c_abi_export("py_virtual_thread_tcp_deadline")
def py_virtual_thread_tcp_deadline(timeout_ms: int) -> int:
    if timeout_ms < -1:
        _raise_oserror(cstr("invalid TCP timeout"))
        return -2
    if timeout_ms < 0:
        return -1
    now_ms = pcc_runtime_monotonic_us() // 1000
    if timeout_ms > 9223372036854775807 - now_ms:
        _raise_oserror(cstr("TCP timeout is too large"))
        return -2
    return now_ms + timeout_ms


@c_abi_export("py_virtual_thread_tcp_remaining")
def py_virtual_thread_tcp_remaining(deadline_ms: int) -> int:
    if deadline_ms < 0:
        return -1
    now_ms = pcc_runtime_monotonic_us() // 1000
    if now_ms >= deadline_ms:
        return 0
    return deadline_ms - now_ms


@c_abi_export("py_virtual_thread_tcp_raise_timeout")
def py_virtual_thread_tcp_raise_timeout() -> int:
    _raise_oserror(cstr("TCP operation timed out"))
    return -1


@c_abi_export("py_asyncio_fd_recv")
def py_asyncio_fd_recv(fd_object, max_bytes: int):
    fd: int = _fd_value(fd_object)
    if fd < 0:
        _raise_oserror(cstr("invalid TCP recv fd"))
        return null()
    if max_bytes <= 0:
        max_bytes = 65536
    if max_bytes > 1048576:
        max_bytes = 1048576
    buffer = malloc(max_bytes)
    if ptr_is_null(buffer) != 0:
        _raise_oserror(cstr("TCP recv allocation failed"))
        return null()
    while True:
        count: int = pcc_platform_socket_recv(fd, buffer, max_bytes, 0)
        if count >= 0:
            result = py_bytes_new(buffer, count)
            free(buffer)
            return result
        if count == -4:
            continue
        if count == -11 or count == -35:
            # An accepted socket inherits O_NONBLOCK from its listener on
            # Darwin, so a read that outruns the peer sees EAGAIN.  Wait for
            # readability as _send_all_raw does for writability; failing here
            # made ``await reader.readexactly()`` depend on packet timing.
            if pcc_platform_poll_fd(fd, 1, -1) >= 0:
                continue
        free(buffer)
        _raise_oserror(cstr("TCP recv failed"))
        return null()
    return null()


@c_abi_export("py_asyncio_fd_send_all")
def py_asyncio_fd_send_all(fd_object, data_object) -> int:
    fd: int = _fd_value(fd_object)
    data = _bytes_data(data_object)
    length: int = _bytes_length(data_object)
    if fd < 0:
        _raise_oserror(cstr("invalid TCP send fd"))
        return -1
    if ptr_is_null(data) != 0 or length < 0:
        _raise_oserror(cstr("TCP send expects bytes-like data"))
        return -1
    sent: int = _send_all_raw(fd, data, length)
    if sent < 0:
        _raise_oserror(cstr("TCP send failed"))
        return -1
    return sent


@c_abi_export("py_asyncio_fd_relay")
def py_asyncio_fd_relay(fd1_in_object, fd1_out_object, fd2_in_object, fd2_out_object) -> int:
    fd1_in: int = _fd_value(fd1_in_object)
    fd1_out: int = _fd_value(fd1_out_object)
    fd2_in: int = _fd_value(fd2_in_object)
    fd2_out: int = _fd_value(fd2_out_object)
    if fd1_in < 0 or fd1_out < 0 or fd2_in < 0 or fd2_out < 0:
        _raise_oserror(cstr("invalid TCP relay fd"))
        return -1
    active1: int = 1
    active2: int = 1
    buffer = stack_alloc(65536)
    while active1 != 0 or active2 != 0:
        poll1: int = fd1_in
        poll2: int = fd2_in
        if active1 == 0:
            poll1 = -1
        if active2 == 0:
            poll2 = -1
        ready: int = pcc_platform_poll_readable_pair(poll1, poll2, -1)
        if ready < 0:
            if ready == -4:
                continue
            _close_unique4(fd1_in, fd1_out, fd2_in, fd2_out)
            _raise_oserror(cstr("TCP relay poll failed"))
            return -1
        if active1 != 0 and (ready & 1) != 0:
            active1 = _drain_direction(fd1_in, fd1_out, buffer) & 1
        if active2 != 0 and (ready & 2) != 0:
            active2 = _drain_direction(fd2_in, fd2_out, buffer) & 1
    _close_unique4(fd1_in, fd1_out, fd2_in, fd2_out)
    return 0


@c_abi_export("py_asyncio_fd_relay_step")
def py_asyncio_fd_relay_step(
    fd1_in_object,
    fd1_out_object,
    fd2_in_object,
    fd2_out_object,
    active_mask_object,
):
    store_i64(global_addr("pcc_asyncio_relay_step_last_progress"), 0, 0)
    fd1_in: int = _fd_value(fd1_in_object)
    fd1_out: int = _fd_value(fd1_out_object)
    fd2_in: int = _fd_value(fd2_in_object)
    fd2_out: int = _fd_value(fd2_out_object)
    active_mask: int = py_int_value_i64(active_mask_object)
    active1: int = 1 if (active_mask & 1) != 0 else 0
    active2: int = 1 if (active_mask & 2) != 0 else 0
    if (
        fd1_in < 0
        or fd1_out < 0
        or fd2_in < 0
        or fd2_out < 0
        or (active1 == 0 and active2 == 0)
    ):
        _close_unique4(fd1_in, fd1_out, fd2_in, fd2_out)
        return _owned_none()
    poll1: int = fd1_in if active1 != 0 else -1
    poll2: int = fd2_in if active2 != 0 else -1
    ready: int = pcc_platform_poll_readable_pair(poll1, poll2, 0)
    if ready < 0:
        if ready == -4:
            return py_int_from_i64(active1 | (active2 * 2))
        _close_unique4(fd1_in, fd1_out, fd2_in, fd2_out)
        return _owned_none()
    if ready == 0:
        return py_int_from_i64(active1 | (active2 * 2))
    buffer = stack_alloc(65536)
    made_progress: int = 0
    old_active1: int = active1
    old_active2: int = active2
    if active1 != 0 and (ready & 1) != 0:
        result1: int = _drain_direction(fd1_in, fd1_out, buffer)
        active1 = result1 & 1
        if (result1 & 2) != 0:
            made_progress = 1
    if active2 != 0 and (ready & 2) != 0:
        result2: int = _drain_direction(fd2_in, fd2_out, buffer)
        active2 = result2 & 1
        if (result2 & 2) != 0:
            made_progress = 1
    if (old_active1 != 0 and active1 == 0) or (old_active2 != 0 and active2 == 0):
        active1 = 0
        active2 = 0
    new_mask: int = active1 | (active2 * 2)
    if new_mask == 0:
        _close_unique4(fd1_in, fd1_out, fd2_in, fd2_out)
        return _owned_none()
    if made_progress != 0:
        store_i64(global_addr("pcc_asyncio_relay_step_last_progress"), 0, 1)
        new_mask = new_mask | 4
    return py_int_from_i64(new_mask)


@c_abi_export("py_asyncio_fd_relay_step_last_progress")
def py_asyncio_fd_relay_step_last_progress():
    if load_i64(global_addr("pcc_asyncio_relay_step_last_progress"), 0) != 0:
        value = global_load_ptr("py_True")
        py_incref(value)
        return value
    return _owned_none()


@c_abi_export("py_asyncio_fd_close")
def py_asyncio_fd_close(fd_object) -> int:
    fd: int = _fd_value(fd_object)
    if fd >= 0:
        return pcc_platform_close(fd)
    return 0


def _append_byte(output, offset: int, value: int, capacity: int) -> int:
    if offset + 1 >= capacity:
        return -1
    store_i8(output, offset, value)
    store_i8(output, offset + 1, 0)
    return offset + 1


def _append_decimal(output, offset: int, value: int, capacity: int) -> int:
    scratch = stack_alloc(24)
    length: int = _write_decimal(value, scratch, 24)
    if length < 0 or offset + length + 1 > capacity:
        return -1
    index: int = 0
    while index < length:
        store_i8(output, offset + index, load_i8(scratch, index))
        index = index + 1
    store_i8(output, offset + length, 0)
    return offset + length


def _append_hex_group(output, offset: int, value: int, capacity: int) -> int:
    scratch = stack_alloc(4)
    count: int = 0
    if value == 0:
        return _append_byte(output, offset, 48, capacity)
    while value > 0:
        digit: int = value & 15
        if digit < 10:
            store_i8(scratch, count, 48 + digit)
        else:
            store_i8(scratch, count, 87 + digit)
        count = count + 1
        value = value // 16
    while count > 0:
        count = count - 1
        offset = _append_byte(output, offset, load_i8(scratch, count), capacity)
        if offset < 0:
            return -1
    return offset


def _address_tuple(address, length: int):
    family: int = load_i8(address, 0) & 255
    if family != 2 and family != 10:
        family = load_i8(address, 1) & 255
    port: int = ((load_i8(address, 2) & 255) * 256) + (load_i8(address, 3) & 255)
    text = stack_alloc(80)
    offset: int = 0
    if family == 2:
        index: int = 0
        while index < 4:
            if index > 0:
                offset = _append_byte(text, offset, 46, 80)
            offset = _append_decimal(text, offset, load_i8(address, 4 + index) & 255, 80)
            index = index + 1
    elif family == 10 or family == 30:
        index = 0
        while index < 8:
            if index > 0:
                offset = _append_byte(text, offset, 58, 80)
            group: int = ((load_i8(address, 8 + index * 2) & 255) * 256) + (
                load_i8(address, 9 + index * 2) & 255
            )
            offset = _append_hex_group(text, offset, group, 80)
            index = index + 1
    else:
        return py_tuple_new(0)
    if offset < 0:
        return py_tuple_new(0)
    result = py_tuple_new(2)
    if ptr_is_null(result) != 0:
        return null()
    py_tuple_set_item(result, 0, py_str_new(text, offset))
    py_tuple_set_item(result, 1, py_int_from_i64(port))
    return result


def _fd_name(fd_object, peer: int):
    fd: int = _fd_value(fd_object)
    if fd < 0:
        return py_tuple_new(0)
    address = stack_alloc(128)
    length: int = 0
    if peer != 0:
        length = pcc_platform_socket_peername(fd, address, 128)
    else:
        length = pcc_platform_socket_sockname(fd, address, 128)
    if length < 0:
        return py_tuple_new(0)
    return _address_tuple(address, length)


@c_abi_export("py_asyncio_fd_sockname")
def py_asyncio_fd_sockname(fd_object):
    return _fd_name(fd_object, 0)


@c_abi_export("py_asyncio_fd_peername")
def py_asyncio_fd_peername(fd_object):
    return _fd_name(fd_object, 1)


@c_abi_export("py_asyncio_io_waitset_backend")
def py_asyncio_io_waitset_backend():
    if pcc_io_waitset_epoll_available() != 0:
        return py_str_new(cstr("epoll"), 5)
    if pcc_io_waitset_kqueue_available() != 0:
        return py_str_new(cstr("kqueue"), 6)
    return py_str_new(cstr("poll"), 4)
