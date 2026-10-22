"""Freestanding TCP socket and fail-closed host resolver primitives."""

from pcc import i64
from pcc.extern import c_int32, c_abi_export
from pcc.unsafe import (
    close,
    cstr,
    fd_control,
    load_i8,
    load_i32,
    load_i64,
    logical_shift_right_i64,
    open_readonly,
    poll_fd,
    poll_readable_pair,
    ptr_add,
    ptr_is_null,
    read,
    socket_accept,
    socket_bind,
    socket_connect,
    socket_listen,
    socket_open,
    socket_peername,
    socket_recv,
    socket_send,
    socket_getsockopt,
    socket_setsockopt,
    socket_shutdown,
    socket_sockname,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    target_sys_platform,
    unsigned_div_i64,
    unsigned_rem_i64,
)


__pcc_freestanding__ = True


@c_abi_export("pcc_platform_socket_is_would_block")
def _is_would_block(result: i64) -> i64:
    # pcc.unsafe lowers failures to a negative errno on both Darwin and Linux.
    if result == -11 or result == -35:
        return 1
    return 0


@c_abi_export("pcc_platform_socket_is_connect_in_progress")
def _is_connect_in_progress(result: i64) -> i64:
    # Linux EINPROGRESS/EALREADY and Darwin EINPROGRESS/EALREADY.
    if result == -115 or result == -114 or result == -36 or result == -37:
        return 1
    if _is_would_block(result) != 0:
        return 1
    return 0


@c_abi_export("pcc_platform_socket_set_nonblocking")
def pcc_platform_socket_set_nonblocking(fd: i64) -> i64:
    """Keep one owned descriptor nonblocking without clobbering other flags."""
    flags: i64 = fd_control(fd, 3, 0)
    nonblocking: i64 = 2048
    if _is_darwin() != 0:
        nonblocking: i64 = 4
    if flags < 0:
        return flags
    if (flags & nonblocking) != 0:
        return 0
    return fd_control(fd, 4, flags | nonblocking)


@c_abi_export("pcc_platform_socket_is_space")
def _is_space(value: i64) -> i64:
    if value == 32 or value == 9:
        return 1
    if value == 10 or value == 13:
        return 1
    return 0


@c_abi_export("pcc_platform_socket_cstr_len")
def _cstr_len(value, limit: i64) -> i64:
    if ptr_is_null(value):
        return -1
    index: i64 = 0
    while index < limit:
        if load_i8(value, index) == 0:
            return index
        index = index + 1
    return -1


@c_abi_export("pcc_platform_socket_token_equals")
def _token_equals(value, token, start: i64, end: i64) -> i64:
    length = _cstr_len(value, 256)
    if length < 0 or length != end - start:
        return 0
    index: i64 = 0
    while index < length:
        if load_i8(value, index) != load_i8(token, start + index):
            return 0
        index = index + 1
    return 1


@c_abi_export("pcc_platform_socket_parse_decimal_span")
def _parse_decimal_span(value, start: i64, end: i64, limit: i64) -> i64:
    if start >= end:
        return -1
    result: i64 = 0
    index = start
    while index < end:
        digit = load_i8(value, index) - 48
        if digit < 0 or digit > 9:
            return -1
        result = result * 10 + digit
        if result > limit:
            return -1
        index = index + 1
    return result


@c_abi_export("pcc_platform_socket_parse_port")
def _parse_port(value) -> i64:
    length = _cstr_len(value, 16)
    if length <= 0:
        return -1
    return _parse_decimal_span(value, 0, length, 65535)


@c_abi_export("pcc_platform_socket_parse_ipv4_span")
def _parse_ipv4_span(value, start: i64, end: i64, output) -> i64:
    part: i64 = 0
    cursor = start
    while part < 4:
        token_end = cursor
        while token_end < end and load_i8(value, token_end) != 46:
            token_end = token_end + 1
        number = _parse_decimal_span(value, cursor, token_end, 255)
        if number < 0:
            return 0
        store_i8(output, part, number)
        part = part + 1
        if part == 4:
            if token_end != end:
                return 0
        else:
            if token_end >= end:
                return 0
            cursor = token_end + 1
    return 1


@c_abi_export("pcc_platform_socket_hex_digit")
def _hex_digit(value: i64) -> i64:
    if value >= 48 and value <= 57:
        return value - 48
    if value >= 65 and value <= 70:
        return value - 55
    if value >= 97 and value <= 102:
        return value - 87
    return -1


@c_abi_export("pcc_platform_socket_parse_hex_group")
def _parse_hex_group(value, start: i64, end: i64) -> i64:
    if start >= end or end - start > 4:
        return -1
    result: i64 = 0
    index = start
    while index < end:
        digit = _hex_digit(load_i8(value, index))
        if digit < 0:
            return -1
        result = result * 16 + digit
        index = index + 1
    return result


@c_abi_export("pcc_platform_socket_store_ipv6_group")
def _store_ipv6_group(output, group: i64, value: i64) -> None:
    store_i8(output, group * 2, logical_shift_right_i64(value, 8) & 255)
    store_i8(output, group * 2 + 1, value & 255)


@c_abi_export("pcc_platform_socket_parse_ipv6_span")
def _parse_ipv6_span(value, start: i64, end: i64, output) -> i64:
    index: i64 = 0
    while index < 16:
        store_i8(output, index, 0)
        index = index + 1

    compression: i64 = -1
    cursor = start
    while cursor + 1 < end:
        if load_i8(value, cursor) == 58 and load_i8(value, cursor + 1) == 58:
            if compression >= 0:
                return 0
            compression = cursor
            cursor = cursor + 2
        else:
            cursor = cursor + 1

    if compression < 0:
        group: i64 = 0
        cursor = start
        while cursor < end and group < 8:
            token_end = cursor
            while token_end < end and load_i8(value, token_end) != 58:
                token_end = token_end + 1
            parsed = _parse_hex_group(value, cursor, token_end)
            if parsed < 0:
                return 0
            _store_ipv6_group(output, group, parsed)
            group = group + 1
            if token_end == end:
                cursor = end
            else:
                cursor = token_end + 1
        if cursor != end or group != 8:
            return 0
        return 1

    left_count: i64 = 0
    cursor = start
    while cursor < compression:
        token_end = cursor
        while token_end < compression and load_i8(value, token_end) != 58:
            token_end = token_end + 1
        parsed = _parse_hex_group(value, cursor, token_end)
        if parsed < 0:
            return 0
        _store_ipv6_group(output, left_count, parsed)
        left_count = left_count + 1
        if token_end < compression:
            cursor = token_end + 1
        else:
            cursor = compression

    right_start = compression + 2
    right_count: i64 = 0
    cursor = right_start
    while cursor < end:
        token_end = cursor
        while token_end < end and load_i8(value, token_end) != 58:
            token_end = token_end + 1
        if _parse_hex_group(value, cursor, token_end) < 0:
            return 0
        right_count = right_count + 1
        if token_end < end:
            cursor = token_end + 1
        else:
            cursor = end
    if left_count + right_count >= 8:
        return 0

    group = 8 - right_count
    cursor = right_start
    while cursor < end:
        token_end = cursor
        while token_end < end and load_i8(value, token_end) != 58:
            token_end = token_end + 1
        parsed = _parse_hex_group(value, cursor, token_end)
        _store_ipv6_group(output, group, parsed)
        group = group + 1
        if token_end < end:
            cursor = token_end + 1
        else:
            cursor = end
    return 1


@c_abi_export("pcc_platform_socket_parse_numeric_span")
def _parse_numeric_span(value, start: i64, end: i64, address) -> i64:
    if _parse_ipv4_span(value, start, end, address) != 0:
        return 4
    if _parse_ipv6_span(value, start, end, address) != 0:
        return 6
    return 0


@c_abi_export("pcc_platform_socket_resolve_hosts")
def _resolve_hosts(host, address) -> i64:
    fd = open_readonly(cstr("/etc/hosts"))
    if fd < 0:
        return 0
    data = stack_alloc(65536)
    size = read(fd, data, 65535)
    close(fd)
    if size <= 0 or size >= 65535:
        return 0
    store_i8(data, size, 0)

    line_start: i64 = 0
    while line_start < size:
        line_end = line_start
        while line_end < size and load_i8(data, line_end) != 10:
            line_end = line_end + 1
        cursor = line_start
        while cursor < line_end and _is_space(load_i8(data, cursor)) != 0:
            cursor = cursor + 1
        address_start = cursor
        while (
            cursor < line_end
            and _is_space(load_i8(data, cursor)) == 0
            and load_i8(data, cursor) != 35
        ):
            cursor = cursor + 1
        address_end = cursor
        while cursor < line_end:
            while cursor < line_end and _is_space(load_i8(data, cursor)) != 0:
                cursor = cursor + 1
            if cursor >= line_end or load_i8(data, cursor) == 35:
                break
            token_start = cursor
            while (
                cursor < line_end
                and _is_space(load_i8(data, cursor)) == 0
                and load_i8(data, cursor) != 35
            ):
                cursor = cursor + 1
            if _token_equals(host, data, token_start, cursor) != 0:
                return _parse_numeric_span(data, address_start, address_end, address)
        line_start = line_end + 1
    return 0


@c_abi_export("pcc_platform_socket_read_bounded_config")
def _read_bounded_config(path, output, capacity: i64) -> i64:
    """Read one small platform configuration file through owned syscalls.

    The gateway uses this for resolver and hosts snapshots.  It deliberately
    avoids stdio and every libc resolver entrypoint; callers parse the bounded
    bytes in pcc-Python.  A regular-file read may complete synchronously, but
    it never occupies a carrier waiting on DNS or network I/O.
    """
    if ptr_is_null(output) or capacity <= 0:
        return -1
    if capacity > 65535:
        capacity: i64 = 65535
    fd = open_readonly(path)
    if fd < 0:
        return fd
    offset: i64 = 0
    interrupted: i64 = 0
    while offset < capacity:
        count: i64 = read(fd, ptr_add(output, offset), capacity - offset)
        if count > 0:
            offset = offset + count
            interrupted: i64 = 0
        elif count == 0:
            break
        elif count == -4 and interrupted < 8:
            interrupted = interrupted + 1
        else:
            close(fd)
            return count
    close(fd)
    return offset


@c_abi_export("pcc_platform_resolver_config_read")
def pcc_platform_resolver_config_read(output, capacity: i64) -> i64:
    return _read_bounded_config(cstr("/etc/resolv.conf"), output, capacity)


@c_abi_export("pcc_platform_hosts_config_read")
def pcc_platform_hosts_config_read(output, capacity: i64) -> i64:
    return _read_bounded_config(cstr("/etc/hosts"), output, capacity)


@c_abi_export("pcc_platform_random_u16")
def pcc_platform_random_u16() -> i64:
    """Read one nonzero DNS transaction seed without a host runtime."""
    fd = open_readonly(cstr("/dev/urandom"))
    if fd < 0:
        return fd
    data = stack_alloc(2)
    offset: i64 = 0
    attempts: i64 = 0
    while offset < 2 and attempts < 8:
        count: i64 = read(fd, ptr_add(data, offset), 2 - offset)
        if count > 0:
            offset = offset + count
        elif count != -4:
            close(fd)
            return count if count < 0 else -1
        attempts = attempts + 1
    close(fd)
    if offset != 2:
        return -1
    value: i64 = (load_i8(data, 0) & 255) * 256 + (load_i8(data, 1) & 255)
    if value == 0:
        value: i64 = 1
    return value


@c_abi_export("pcc_platform_socket_is_darwin")
def _is_darwin() -> i64:
    name = target_sys_platform()
    if load_i8(name, 0) == 100:
        return 1
    return 0


@c_abi_export("pcc_platform_resolve_tcp")
def pcc_platform_resolve_tcp(host, port_text, sockaddr) -> i64:
    host_len = _cstr_len(host, 256)
    port = _parse_port(port_text)
    if host_len <= 0 or port < 0:
        return -1
    address = stack_alloc(16)
    family = _parse_numeric_span(host, 0, host_len, address)
    if family == 0:
        family = _resolve_hosts(host, address)
    if family == 0:
        return -2

    index: i64 = 0
    while index < 28:
        store_i8(sockaddr, index, 0)
        index = index + 1
    if family == 4:
        if _is_darwin() != 0:
            store_i8(sockaddr, 0, 16)
            store_i8(sockaddr, 1, 2)
        else:
            store_i8(sockaddr, 0, 2)
        store_i8(sockaddr, 2, logical_shift_right_i64(port, 8) & 255)
        store_i8(sockaddr, 3, port & 255)
        index: i64 = 0
        while index < 4:
            store_i8(sockaddr, 4 + index, load_i8(address, index))
            index = index + 1
        return 16

    if _is_darwin() != 0:
        store_i8(sockaddr, 0, 28)
        store_i8(sockaddr, 1, 30)
    else:
        store_i8(sockaddr, 0, 10)
    store_i8(sockaddr, 2, logical_shift_right_i64(port, 8) & 255)
    store_i8(sockaddr, 3, port & 255)
    index: i64 = 0
    while index < 16:
        store_i8(sockaddr, 8 + index, load_i8(address, index))
        index = index + 1
    return 28


@c_abi_export("pcc_platform_tcp_connect")
def pcc_platform_tcp_connect(host, port_text) -> i64:
    """Compatibility connect that returns a connected, nonblocking socket.

    New gateway code uses ``pcc_platform_tcp_connect_start`` and parks on
    POLLOUT between observations.  This wrapper keeps older synchronous
    consumers working while preserving the descriptor's O_NONBLOCK flag.
    """
    output = stack_alloc(8)
    outcome: i64 = pcc_platform_tcp_connect_start(host, port_text, output)
    fd: i64 = load_i64(output, 0)
    if outcome == 3:
        return fd
    if outcome < 0:
        return outcome
    while outcome == 1:
        outcome = pcc_platform_socket_connect_observe(fd, -1)
        if outcome == -4:
            outcome: i64 = 1
    if outcome == 3:
        return fd
    close(fd)
    return outcome


@c_abi_export("pcc_platform_tcp_connect_start")
def pcc_platform_tcp_connect_start(host, port_text, output_fd) -> i64:
    """Start a nonblocking connect and return CONNECTED/WOULD_BLOCK/error.

    On either non-error outcome ``*output_fd`` owns the new descriptor.  A
    hard-error path closes it and stores ``-1``.  The split result avoids
    overloading a valid fd with an errno and is suitable for virtual-thread
    park/retry loops.
    """
    if ptr_is_null(output_fd):
        return -1
    store_i64(output_fd, 0, -1)
    address = stack_alloc(32)
    address_len = pcc_platform_resolve_tcp(host, port_text, address)
    if address_len < 0:
        return address_len
    family: i64 = 2
    if address_len == 28:
        if _is_darwin() != 0:
            family: i64 = 30
        else:
            family: i64 = 10
    fd = socket_open(family, 1, 0)
    if fd < 0:
        return fd
    result: i64 = pcc_platform_socket_set_nonblocking(fd)
    if result < 0:
        close(fd)
        return result
    store_i64(output_fd, 0, fd)
    result = socket_connect(fd, address, address_len)
    if result == 0:
        return 3
    if result == -4 or _is_connect_in_progress(result) != 0:
        return 1
    if result < 0:
        close(fd)
        store_i64(output_fd, 0, -1)
        return result
    return 3


@c_abi_export("pcc_platform_dns_connect_start")
def pcc_platform_dns_connect_start(
    protocol: i64, host, port_text, output_fd
) -> i64:
    """Open and connect one nonblocking DNS UDP or TCP descriptor.

    ``protocol`` is 0 for connected UDP and 1 for TCP.  The host must already
    be numeric at the gateway contract; ``pcc_platform_resolve_tcp`` performs
    the target sockaddr encoding and remains fail-closed for other names.
    Connected UDP is security-significant: the kernel filters inbound packets
    by the configured nameserver, allowing the Python driver to treat the
    descriptor owner as reply-peer provenance.
    """
    if ptr_is_null(output_fd) or (protocol != 0 and protocol != 1):
        return -1
    store_i64(output_fd, 0, -1)
    address = stack_alloc(32)
    address_len = pcc_platform_resolve_tcp(host, port_text, address)
    if address_len < 0:
        return address_len
    family: i64 = 2
    if address_len == 28:
        if _is_darwin() != 0:
            family: i64 = 30
        else:
            family: i64 = 10
    socket_type: i64 = 2
    if protocol == 1:
        socket_type: i64 = 1
    fd = socket_open(family, socket_type, 0)
    if fd < 0:
        return fd
    result: i64 = pcc_platform_socket_set_nonblocking(fd)
    if result < 0:
        close(fd)
        return result
    store_i64(output_fd, 0, fd)
    result = socket_connect(fd, address, address_len)
    if result == 0:
        return 3
    if result == -4 or _is_connect_in_progress(result) != 0:
        return 1
    close(fd)
    store_i64(output_fd, 0, -1)
    return result


@c_abi_export("pcc_platform_udp_connect_start")
def pcc_platform_udp_connect_start(host, port_text, output_fd) -> i64:
    """Named connected-UDP ABI used by the gateway DNS transport."""
    return pcc_platform_dns_connect_start(0, host, port_text, output_fd)


@c_abi_export("pcc_platform_socket_connect_observe")
def pcc_platform_socket_connect_observe(fd: i64, timeout_ms: i64) -> i64:
    """Observe connect completion and preserve the kernel's ``SO_ERROR``.

    ``timeout_ms`` is the caller's remaining deadline budget; zero performs a
    pure observation.  Writability alone does not mean that a nonblocking
    connect succeeded: refused and unreachable connections become writable as
    well.  Read ``SO_ERROR`` after poll and return the exact negative errno;
    the deadline-owning caller may retry EINTR without resetting its absolute
    budget.
    """
    ready: i64 = poll_fd(fd, 4, timeout_ms)
    if ready == 0:
        return 1
    if ready < 0:
        return ready
    value = stack_alloc(4)
    store_i32(value, 0, 0)
    level: i64 = 1
    option: i64 = 4
    if _is_darwin() != 0:
        level: i64 = 65535
        option: i64 = 4103
    length: i64 = socket_getsockopt(fd, level, option, value, 4)
    if length < 0:
        return length
    if length < 4:
        return -5
    pending_error: i64 = load_i32(value, 0)
    if pending_error == 0:
        return 3
    result: i64 = 0 - pending_error
    if _is_connect_in_progress(result) != 0:
        return 1
    return result


@c_abi_export("pcc_platform_tcp_listen_with_backlog")
def pcc_platform_tcp_listen_with_backlog(
    host, port_text, reuse_port: i64, backlog: i64
) -> i64:
    if backlog <= 0 or backlog > 65535:
        return -1
    address = stack_alloc(32)
    address_len: i64 = 0
    port = _parse_port(port_text)
    if port < 0:
        return -1
    if ptr_is_null(host) or _cstr_len(host, 256) == 0:
        index: i64 = 0
        while index < 16:
            store_i8(address, index, 0)
            index = index + 1
        if _is_darwin() != 0:
            store_i8(address, 0, 16)
            store_i8(address, 1, 2)
        else:
            store_i8(address, 0, 2)
        store_i8(address, 2, logical_shift_right_i64(port, 8) & 255)
        store_i8(address, 3, port & 255)
        address_len: i64 = 16
    else:
        address_len = pcc_platform_resolve_tcp(host, port_text, address)
    if address_len < 0:
        return address_len

    family: i64 = 2
    if address_len == 28:
        if _is_darwin() != 0:
            family: i64 = 30
        else:
            family: i64 = 10
    fd = socket_open(family, 1, 0)
    if fd < 0:
        return fd
    one = stack_alloc(4)
    store_i32(one, 0, 1)
    level: i64 = 1
    reuse_address_option: i64 = 2
    reuse_port_option: i64 = 15
    if _is_darwin() != 0:
        level: i64 = 65535
        reuse_address_option: i64 = 4
        reuse_port_option: i64 = 512
    if socket_setsockopt(fd, level, reuse_address_option, one, 4) < 0:
        close(fd)
        return -1
    if reuse_port != 0:
        if socket_setsockopt(fd, level, reuse_port_option, one, 4) < 0:
            close(fd)
            return -1
    if socket_bind(fd, address, address_len) < 0:
        close(fd)
        return -1
    if socket_listen(fd, backlog) < 0:
        close(fd)
        return -1
    if pcc_platform_socket_set_nonblocking(fd) < 0:
        close(fd)
        return -1
    return fd


@c_abi_export("pcc_platform_tcp_listen")
def pcc_platform_tcp_listen(host, port_text, reuse_port: i64) -> i64:
    """Compatibility ABI; new gateway callers pass an explicit backlog."""
    return pcc_platform_tcp_listen_with_backlog(
        host, port_text, reuse_port, 128
    )


@c_abi_export("pcc_platform_socket_send")
def pcc_platform_socket_send(fd: i64, buffer, size: i64, flags: i64) -> i64:
    return socket_send(fd, buffer, size, flags)


@c_abi_export("pcc_platform_socket_recv")
def pcc_platform_socket_recv(fd: i64, buffer, size: i64, flags: i64) -> i64:
    return socket_recv(fd, buffer, size, flags)


@c_abi_export("pcc_platform_socket_read_observe")
def pcc_platform_socket_read_observe(
    fd: i64, buffer, size: i64, flags: i64, output_count
) -> i64:
    """Try one caller-buffered read without waiting.

    Returns PROGRESS, EOF, WOULD_BLOCK, or the exact negative errno.  EINTR is
    intentionally surfaced so the deadline-owning caller can retry without
    accidentally resetting its budget.
    """
    if ptr_is_null(buffer) or ptr_is_null(output_count) or size <= 0:
        return -1
    store_i64(output_count, 0, 0)
    count: i64 = socket_recv(fd, buffer, size, flags)
    if count > 0:
        store_i64(output_count, 0, count)
        return 0
    if count == 0:
        return 2
    if _is_would_block(count) != 0:
        return 1
    return count


@c_abi_export("pcc_platform_socket_write_observe")
def pcc_platform_socket_write_observe(
    fd: i64, buffer, size: i64, flags: i64, output_count
) -> i64:
    """Try one caller-buffered write, preserving partial progress."""
    if ptr_is_null(buffer) or ptr_is_null(output_count) or size < 0:
        return -1
    store_i64(output_count, 0, 0)
    count: i64 = socket_send(fd, buffer, size, flags)
    if count >= 0:
        store_i64(output_count, 0, count)
        return 0
    if _is_would_block(count) != 0:
        return 1
    return count


@c_abi_export("pcc_platform_tcp_accept")
def pcc_platform_tcp_accept(fd: i64) -> i64:
    accepted: i64 = socket_accept(fd)
    if accepted < 0:
        return accepted
    if _is_darwin() != 0:
        one = stack_alloc(4)
        store_i32(one, 0, 1)
        socket_setsockopt(accepted, 65535, 4130, one, 4)
    if pcc_platform_socket_set_nonblocking(accepted) < 0:
        close(accepted)
        return -1
    return accepted


@c_abi_export("pcc_platform_tcp_accept_observe")
def pcc_platform_tcp_accept_observe(fd: i64, output_fd) -> i64:
    """Perform one nonblocking accept observation."""
    if ptr_is_null(output_fd):
        return -1
    store_i64(output_fd, 0, -1)
    accepted: i64 = pcc_platform_tcp_accept(fd)
    if accepted >= 0:
        store_i64(output_fd, 0, accepted)
        return 0
    if accepted == -4 or _is_would_block(accepted) != 0:
        return 1
    return accepted


@c_abi_export("pcc_platform_socket_shutdown")
def pcc_platform_socket_shutdown(fd: i64, how: i64) -> i64:
    return socket_shutdown(fd, how)


@c_abi_export("pcc_platform_socket_sockname")
def pcc_platform_socket_sockname(fd: i64, address, capacity: i64) -> i64:
    return socket_sockname(fd, address, capacity)


@c_abi_export("pcc_platform_socket_peername")
def pcc_platform_socket_peername(fd: i64, address, capacity: i64) -> i64:
    return socket_peername(fd, address, capacity)


@c_abi_export("pcc_platform_socket_append_address_byte")
def _append_address_byte(output, offset: i64, value: i64, capacity: i64) -> i64:
    if ptr_is_null(output) or offset < 0 or offset + 1 >= capacity:
        return -1
    store_i8(output, offset, value)
    store_i8(output, offset + 1, 0)
    return offset + 1


@c_abi_export("pcc_platform_socket_append_address_decimal")
def _append_address_decimal(
    output, offset: i64, value: i64, capacity: i64
) -> i64:
    if value < 0 or value > 255:
        return -1
    if value >= 100:
        offset = _append_address_byte(
            output, offset, 48 + unsigned_div_i64(value, 100), capacity
        )
        value = unsigned_rem_i64(value, 100)
        if offset < 0:
            return -1
        offset = _append_address_byte(
            output, offset, 48 + unsigned_div_i64(value, 10), capacity
        )
        if offset < 0:
            return -1
        return _append_address_byte(
            output, offset, 48 + unsigned_rem_i64(value, 10), capacity
        )
    if value >= 10:
        offset = _append_address_byte(
            output, offset, 48 + unsigned_div_i64(value, 10), capacity
        )
        if offset < 0:
            return -1
    return _append_address_byte(
        output, offset, 48 + unsigned_rem_i64(value, 10), capacity
    )


@c_abi_export("pcc_platform_socket_append_address_hex_group")
def _append_address_hex_group(
    output, offset: i64, value: i64, capacity: i64
) -> i64:
    shift: i64 = 12
    while shift >= 0:
        digit = logical_shift_right_i64(value, shift) & 15
        encoded = 48 + digit
        if digit >= 10:
            encoded = 87 + digit
        offset = _append_address_byte(output, offset, encoded, capacity)
        if offset < 0:
            return -1
        shift = shift - 4
    return offset


@c_abi_export("pcc_platform_socket_format_numeric_address")
def pcc_platform_socket_format_numeric_address(
    address, length: i64, output, capacity: i64
) -> i64:
    """Format one kernel sockaddr without libc name-service ownership.

    IPv6 is deliberately rendered as eight fixed-width hexadecimal groups.
    It is a valid numeric address, deterministic across platforms, and avoids
    importing getnameinfo/inet_ntop into the Linux zero-libc boundary.
    """
    if ptr_is_null(address) or ptr_is_null(output) or capacity <= 0:
        return -1
    store_i8(output, 0, 0)
    family = load_i8(address, 0) & 255
    if family != 2 and family != 10:
        family = load_i8(address, 1) & 255
    offset: i64 = 0
    if family == 2:
        if length < 8:
            return -1
        index: i64 = 0
        while index < 4:
            if index > 0:
                offset = _append_address_byte(output, offset, 46, capacity)
            if offset < 0:
                return -1
            offset = _append_address_decimal(
                output, offset, load_i8(address, 4 + index) & 255, capacity
            )
            if offset < 0:
                return -1
            index = index + 1
        return offset
    if family == 10 or family == 30:
        if length < 24:
            return -1
        index: i64 = 0
        while index < 8:
            if index > 0:
                offset = _append_address_byte(output, offset, 58, capacity)
            if offset < 0:
                return -1
            group = ((load_i8(address, 8 + index * 2) & 255) * 256) + (
                load_i8(address, 9 + index * 2) & 255
            )
            offset = _append_address_hex_group(
                output, offset, group, capacity
            )
            if offset < 0:
                return -1
            index = index + 1
        return offset
    return -97


@c_abi_export("pcc_platform_socket_peer_text")
def pcc_platform_socket_peer_text(fd: i64, output, capacity: i64) -> i64:
    """Return the peer's numeric host text or a negative platform error."""
    address = stack_alloc(128)
    length = socket_peername(fd, address, 128)
    if length < 0:
        return length
    return pcc_platform_socket_format_numeric_address(
        address, length, output, capacity
    )


@c_abi_export("pcc_platform_poll_fd")
def pcc_platform_poll_fd(fd: i64, events: i64, timeout_ms: i64) -> i64:
    return poll_fd(fd, events, timeout_ms)


@c_abi_export("pcc_platform_socket_ready_observe")
def pcc_platform_socket_ready_observe(
    fd: i64, events: i64, timeout_ms: i64, output_events
) -> i64:
    """Observe exact readiness within a caller-computed deadline budget."""
    if ptr_is_null(output_events):
        return -1
    store_i64(output_events, 0, 0)
    ready: i64 = poll_fd(fd, events, timeout_ms)
    if ready > 0:
        store_i64(output_events, 0, ready)
        return 0
    if ready == 0:
        return 1
    return ready


@c_abi_export("pcc_platform_poll_readable_pair")
def pcc_platform_poll_readable_pair(fd0: i64, fd1: i64, timeout_ms: i64) -> i64:
    return poll_readable_pair(fd0, fd1, timeout_ms)


# --- legacy count-only nonblocking recv --------------------------------
@c_abi_export("pcc_platform_socket_recv_nonblock")
def pcc_platform_socket_recv_nonblock(fd: i64, size: i64, flags: i64) -> i64:
    """Deprecated count-only observation with call-local scratch.

    Kept for ABI compatibility.  Gateway code must use
    ``pcc_platform_socket_read_observe`` so bytes land in connection-owned
    storage.  The old shared 1 KiB buffer was unsafe with multiple carriers;
    this local block cannot be observed by another call.
    """
    if size <= 0:
        return -1
    if size > 1024:
        size: i64 = 1024
    buf = stack_alloc(1024)
    n: i64 = socket_recv(fd, buf, size, flags)
    if n == -35 or n == -11:
        return -2  # WouldBlock
    return n
