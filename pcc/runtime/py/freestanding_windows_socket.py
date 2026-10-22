"""Winsock adapter preserving pcc's POSIX-shaped socket primitive contract."""

from pcc import i64
from pcc.extern import c_abi_export, c_int, c_int64, c_ptr, extern
from pcc.unsafe import (
    null, ptr_is_null, ptr_to_int, int_to_ptr, ptr_add, stack_alloc,
    load_i8, load_i32, store_i8, store_i32, store_i64, define_global_i32,
    global_addr, atomic_load_i32, atomic_store_i32, atomic_cas_i32,
)

__pcc_freestanding__ = True

WSAStartup = extern("WSAStartup", (c_int, c_ptr), c_int)
WSAGetLastError = extern("WSAGetLastError", (), c_int)
socket = extern("socket", (c_int, c_int, c_int), c_int64)
connect = extern("connect", (c_int64, c_ptr, c_int), c_int)
bind = extern("bind", (c_int64, c_ptr, c_int), c_int)
listen = extern("listen", (c_int64, c_int), c_int)
accept = extern("accept", (c_int64, c_ptr, c_ptr), c_int64)
shutdown = extern("shutdown", (c_int64, c_int), c_int)
send = extern("send", (c_int64, c_ptr, c_int, c_int), c_int)
recv = extern("recv", (c_int64, c_ptr, c_int, c_int), c_int)
setsockopt = extern("setsockopt", (c_int64, c_int, c_int, c_ptr, c_int), c_int)
getsockopt = extern("getsockopt", (c_int64, c_int, c_int, c_ptr, c_ptr), c_int)
getsockname = extern("getsockname", (c_int64, c_ptr, c_ptr), c_int)
getpeername = extern("getpeername", (c_int64, c_ptr, c_ptr), c_int)
ioctlsocket = extern("ioctlsocket", (c_int64, c_int, c_ptr), c_int)
WSAPoll = extern("WSAPoll", (c_ptr, c_int, c_int), c_int)
handle = extern("pcc_win_fd_handle", (c_int64,), c_ptr)
register = extern("pcc_win_socket_register", (c_int64,), c_int64)

define_global_i32("pcc_win_winsock_state", 0)


@c_abi_export("pcc_windows_socket_ensure")
def ensure() -> i64:
    state: i64 = atomic_load_i32(global_addr("pcc_win_winsock_state"), 0, "acquire")
    if state == 2:
        return 0
    previous: i64 = atomic_cas_i32(global_addr("pcc_win_winsock_state"), 0, 0, 1, "acq_rel", "acquire")
    if previous == 0:
        storage = stack_alloc(512)
        status: i64 = WSAStartup(514, storage)
        atomic_store_i32(global_addr("pcc_win_winsock_state"), 0, 2 if status == 0 else -1, "release")
        if status == 0:
            return 0
        return -5
    while atomic_load_i32(global_addr("pcc_win_winsock_state"), 0, "acquire") == 1:
        pass
    if atomic_load_i32(global_addr("pcc_win_winsock_state"), 0, "acquire") == 2:
        return 0
    return -5


@c_abi_export("pcc_windows_socket_failure")
def failure() -> i64:
    code: i64 = WSAGetLastError()
    if code == 10035:
        return -11
    if code == 10036:
        return -115
    if code == 10037:
        return -114
    if code == 10038:
        return -88
    if code == 10048:
        return -98
    if code == 10054:
        return -104
    if code == 10060:
        return -110
    if code == 10061:
        return -111
    if code == 10013:
        return -13
    return -22


@c_abi_export("pcc_windows_socket_raw")
def raw(fd: i64) -> i64:
    return ptr_to_int(handle(fd))


@c_abi_export("pcc_windows_socket_option_number")
def option_number(level: i64, option: i64) -> i64:
    if level != 1:
        if level == 41 and option == 26:
            return 27
        return option
    if option == 2:
        return 4
    if option == 3:
        return 4104
    if option == 4:
        return 4103
    if option == 7:
        return 4097
    if option == 8:
        return 4098
    if option == 9:
        return 8
    return -1


@c_abi_export("pcc_win_socket_open")
def socket_open(family: i64, kind: i64, protocol: i64) -> i64:
    if ensure() != 0:
        return -5
    value: i64 = socket(23 if family == 10 else family, kind, protocol)
    if value == -1:
        return failure()
    return register(value)


@c_abi_export("pcc_windows_socket_address_call")
def address_call(fd: i64, address: c_ptr, size: i64, bind_mode: i64) -> i64:
    if size < 2 or size > 128:
        return -22
    copied = stack_alloc(128)
    index: i64 = 0
    while index < size:
        store_i8(copied, index, load_i8(address, index))
        index = index + 1
    if load_i8(copied, 0) == 10 and load_i8(copied, 1) == 0:
        store_i8(copied, 0, 23)
    status: i64 = bind(raw(fd), copied, size) if bind_mode else connect(raw(fd), copied, size)
    if status == -1:
        return failure()
    return status


@c_abi_export("pcc_win_socket_connect")
def socket_connect(fd: i64, address: c_ptr, size: i64) -> i64:
    return address_call(fd, address, size, 0)


@c_abi_export("pcc_win_socket_bind")
def socket_bind(fd: i64, address: c_ptr, size: i64) -> i64:
    return address_call(fd, address, size, 1)


@c_abi_export("pcc_win_socket_listen")
def socket_listen(fd: i64, backlog: i64) -> i64:
    status: i64 = listen(raw(fd), backlog)
    if status == -1:
        return failure()
    return status


@c_abi_export("pcc_win_socket_accept")
def socket_accept(fd: i64) -> i64:
    value: i64 = accept(raw(fd), null(), null())
    if value == -1:
        return failure()
    return register(value)


@c_abi_export("pcc_win_socket_shutdown")
def socket_shutdown(fd: i64, how: i64) -> i64:
    status: i64 = shutdown(raw(fd), how)
    if status == -1:
        return failure()
    return status


@c_abi_export("pcc_win_socket_send")
def socket_send(fd: i64, buffer: c_ptr, size: i64, flags: i64) -> i64:
    if size < 0:
        return -22
    if size > 2147483647:
        size = 2147483647
    result: i64 = send(raw(fd), buffer, size, flags & -16385)
    if result == -1:
        return failure()
    return result


@c_abi_export("pcc_win_socket_recv")
def socket_recv(fd: i64, buffer: c_ptr, size: i64, flags: i64) -> i64:
    if size < 0:
        return -22
    if size > 2147483647:
        size = 2147483647
    result: i64 = recv(raw(fd), buffer, size, flags)
    if result == -1:
        return failure()
    return result


@c_abi_export("pcc_win_socket_setsockopt")
def socket_setsockopt(fd: i64, level: i64, option: i64, value: c_ptr, size: i64) -> i64:
    number: i64 = option_number(level, option)
    if number < 0:
        return -92
    result: i64 = setsockopt(raw(fd), 65535 if level == 1 else level, number, value, size)
    if result == -1:
        return failure()
    return 0


@c_abi_export("pcc_win_socket_getsockopt")
def socket_getsockopt(fd: i64, level: i64, option: i64, value: c_ptr, capacity: i64) -> i64:
    number: i64 = option_number(level, option)
    if number < 0:
        return -92
    size = stack_alloc(4)
    store_i32(size, 0, capacity)
    result: i64 = getsockopt(raw(fd), 65535 if level == 1 else level, number, value, size)
    if result == -1:
        return failure()
    return load_i32(size, 0)


@c_abi_export("pcc_windows_socket_name_call")
def name_call(fd: i64, address: c_ptr, capacity: i64, peer: i64) -> i64:
    size = stack_alloc(4)
    store_i32(size, 0, capacity)
    status: i64 = getpeername(raw(fd), address, size) if peer else getsockname(raw(fd), address, size)
    if status == -1:
        return failure()
    if load_i8(address, 0) == 23:
        store_i8(address, 0, 10)
    return load_i32(size, 0)


@c_abi_export("pcc_win_socket_sockname")
def socket_sockname(fd: i64, address: c_ptr, capacity: i64) -> i64:
    return name_call(fd, address, capacity, 0)


@c_abi_export("pcc_win_socket_peername")
def socket_peername(fd: i64, address: c_ptr, capacity: i64) -> i64:
    return name_call(fd, address, capacity, 1)


@c_abi_export("pcc_win_socket_nonblocking")
def nonblocking(fd: i64, enabled: i64) -> i64:
    value = stack_alloc(4)
    store_i32(value, 0, enabled)
    if ioctlsocket(raw(fd), 2147772030, value) == -1:
        return failure()
    return 0


@c_abi_export("pcc_win_socket_poll")
def socket_poll(fd: i64, events: i64, timeout: i64) -> i64:
    record = stack_alloc(16)
    store_i64(record, 0, raw(fd))
    requested: i64 = 0
    if events & 1:
        requested = requested | 256
    if events & 4:
        requested = requested | 16
    store_i32(record, 8, requested)
    status: i64 = WSAPoll(record, 1, timeout)
    if status == -1:
        return failure()
    revents: i64 = load_i8(record, 10) | (load_i8(record, 11) * 256)
    result: i64 = 0
    if revents & 256:
        result = result | 1
    if revents & 16:
        result = result | 4
    if revents & 1:
        result = result | 8
    if revents & 2:
        result = result | 16
    if revents & 4:
        result = result | 32
    return result
