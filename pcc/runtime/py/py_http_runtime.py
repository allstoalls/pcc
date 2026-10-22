"""Owned HTTP transport authored in pcc-Python.

HTTPS uses the system libcurl ABI. Plain HTTP and file operations use the
freestanding platform surface. Hashing lives in py_hash_runtime.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_i64_ptr1,
    call_variadic_i32_ptr_i32_i64,
    call_variadic_i32_ptr_i32_ptr,
    call_ptr0,
    call_void_ptr1,
    cstr,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    function_addr,
    load_i8,
    mul_overflow_i64,
    null,
    open_file,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    tag_int,
    target_sys_platform,
    unlinkat,
    untag_int,
)


py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
pcc_platform_read = extern(
    "pcc_platform_read", (c_int64, c_ptr, c_int64), c_int64
)
pcc_platform_write = extern(
    "pcc_platform_write", (c_int64, c_ptr, c_int64), c_int64
)
pcc_platform_close = extern("pcc_platform_close", (c_int64,), c_int64)
pcc_platform_tcp_connect = extern(
    "pcc_platform_tcp_connect", (c_ptr, c_ptr), c_int64
)
pcc_platform_socket_send = extern(
    "pcc_platform_socket_send", (c_int64, c_ptr, c_int64, c_int64), c_int64
)
pcc_platform_socket_recv = extern(
    "pcc_platform_socket_recv", (c_int64, c_ptr, c_int64, c_int64), c_int64
)


def _starts_with(value, prefix) -> int:
    index: int = 0
    while load_i8(prefix, index) != 0:
        if load_i8(value, index) != load_i8(prefix, index):
            return 0
        index = index + 1
    return 1


def _is_darwin() -> int:
    return _starts_with(target_sys_platform(), cstr("darwin"))


def _open_system_libcurl():
    handle = null()
    if _is_darwin() != 0:
        handle = dynamic_library_open(cstr("/usr/lib/libcurl.4.dylib"))
        if ptr_is_null(handle) != 0:
            handle = dynamic_library_open(cstr("libcurl.4.dylib"))
        if ptr_is_null(handle) != 0:
            handle = dynamic_library_open(cstr("libcurl.dylib"))
    else:
        handle = dynamic_library_open(cstr("libcurl.so.4"))
        if ptr_is_null(handle) != 0:
            handle = dynamic_library_open(cstr("libcurl.so"))
    return handle


def _write_all(fd: int, data, length: int) -> int:
    offset: int = 0
    while offset < length:
        count: int = pcc_platform_write(fd, ptr_add(data, offset), length - offset)
        if count < 0:
            if count == -4:
                continue
            return -1
        if count == 0:
            return -1
        offset = offset + count
    return 0


@c_abi_export("pcc_http_curl_write")
def _curl_write(data, size: int, count: int, stream) -> int:
    if mul_overflow_i64(size, count):
        return 0
    total: int = size * count
    if _write_all(untag_int(stream), data, total) != 0:
        return 0
    return total


def _curl_set_ptr(setopt, curl, option: int, value) -> int:
    # curl_easy_setopt is variadic: on Apple arm64 the trailing argument
    # must go on the stack, so the fixed-prototype helper made every
    # option (CURLOPT_URL included) read garbage and HTTPS never worked.
    # CURLcode/CURLoption are 32-bit enums; only a long-valued trailing
    # option uses the 64-bit integer lane.
    return call_variadic_i32_ptr_i32_ptr(setopt, curl, option, value)


def _curl_set_i64(setopt, curl, option: int, value: int) -> int:
    return call_variadic_i32_ptr_i32_i64(setopt, curl, option, value)


def _remove_file(path) -> None:
    unlinkat(path, 0)


def _download_with_system_libcurl(url, destination) -> int:
    library = _open_system_libcurl()
    if ptr_is_null(library) != 0:
        return -10
    easy_init = dynamic_library_symbol(library, cstr("curl_easy_init"))
    easy_setopt = dynamic_library_symbol(library, cstr("curl_easy_setopt"))
    easy_perform = dynamic_library_symbol(library, cstr("curl_easy_perform"))
    easy_cleanup = dynamic_library_symbol(library, cstr("curl_easy_cleanup"))
    if (
        ptr_is_null(easy_init) != 0
        or ptr_is_null(easy_setopt) != 0
        or ptr_is_null(easy_perform) != 0
        or ptr_is_null(easy_cleanup) != 0
    ):
        dynamic_library_close(library)
        return -11
    fd: int = open_file(destination, 1, 1)
    if fd < 0:
        dynamic_library_close(library)
        return -12
    curl = call_ptr0(easy_init)
    if ptr_is_null(curl) != 0:
        pcc_platform_close(fd)
        dynamic_library_close(library)
        return -13
    configured: int = 1
    if _curl_set_ptr(easy_setopt, curl, 10002, url) != 0:
        configured = 0
    if _curl_set_ptr(easy_setopt, curl, 10001, tag_int(fd)) != 0:
        configured = 0
    if (
        _curl_set_ptr(
            easy_setopt,
            curl,
            20011,
            function_addr("pcc_http_curl_write"),
        )
        != 0
    ):
        configured = 0
    if _curl_set_ptr(easy_setopt, curl, 10018, cstr("pcc-owned-acquire/1")) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 52, 1) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 45, 1) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 78, 20) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 19, 1024) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 20, 30) != 0:
        configured = 0
    if _curl_set_i64(easy_setopt, curl, 99, 1) != 0:
        configured = 0
    result: int = -1
    if configured != 0:
        result = call_i64_ptr1(easy_perform, curl)
    call_void_ptr1(easy_cleanup, curl)
    pcc_platform_close(fd)
    dynamic_library_close(library)
    if result != 0:
        _remove_file(destination)
        return -14
    return 0


def _parse_http_url(url, host, port, path) -> int:
    if _starts_with(url, cstr("http://")) == 0:
        return -1
    cursor: int = 7
    authority_start: int = cursor
    colon: int = -1
    while load_i8(url, cursor) != 0 and load_i8(url, cursor) != 47:
        if load_i8(url, cursor) == 58 and colon < 0:
            colon = cursor
        cursor = cursor + 1
    authority_end: int = cursor
    host_end: int = authority_end
    if colon >= 0:
        host_end = colon
    host_length: int = host_end - authority_start
    if host_length <= 0 or host_length >= 512:
        return -1
    index: int = 0
    while index < host_length:
        store_i8(host, index, load_i8(url, authority_start + index))
        index = index + 1
    store_i8(host, host_length, 0)
    if colon >= 0:
        port_length: int = authority_end - colon - 1
        if port_length <= 0 or port_length >= 32:
            return -1
        index = 0
        while index < port_length:
            store_i8(port, index, load_i8(url, colon + 1 + index))
            index = index + 1
        store_i8(port, port_length, 0)
    else:
        store_i8(port, 0, 56)
        store_i8(port, 1, 48)
        store_i8(port, 2, 0)
    path_length: int = 0
    if load_i8(url, cursor) == 0:
        store_i8(path, 0, 47)
        store_i8(path, 1, 0)
        return 0
    while load_i8(url, cursor + path_length) != 0:
        if path_length + 1 >= 4096:
            return -1
        store_i8(path, path_length, load_i8(url, cursor + path_length))
        path_length = path_length + 1
    store_i8(path, path_length, 0)
    return 0


def _request_append(destination, offset: int, source, capacity: int) -> int:
    index: int = 0
    while load_i8(source, index) != 0:
        if offset + index + 1 >= capacity:
            return -1
        store_i8(destination, offset + index, load_i8(source, index))
        index = index + 1
    store_i8(destination, offset + index, 0)
    return offset + index


def _send_all(fd: int, data, length: int) -> int:
    offset: int = 0
    while offset < length:
        count: int = pcc_platform_socket_send(fd, ptr_add(data, offset), length - offset, 0)
        if count < 0:
            if count == -4:
                continue
            return -1
        if count == 0:
            return -1
        offset = offset + count
    return 0


def _status_ok(header) -> int:
    if _starts_with(header, cstr("HTTP/1.0 200")) != 0:
        return 1
    if _starts_with(header, cstr("HTTP/1.1 200")) != 0:
        return 1
    return 0


def _download_plain_http(url, destination) -> int:
    host = stack_alloc(512)
    port = stack_alloc(32)
    path = stack_alloc(4096)
    if _parse_http_url(url, host, port, path) != 0:
        return -2
    socket_fd: int = pcc_platform_tcp_connect(host, port)
    if socket_fd < 0:
        return -3
    request = stack_alloc(8192)
    length: int = _request_append(request, 0, cstr("GET "), 8192)
    if length >= 0:
        length = _request_append(request, length, path, 8192)
    if length >= 0:
        length = _request_append(request, length, cstr(" HTTP/1.0\r\nHost: "), 8192)
    if length >= 0:
        length = _request_append(request, length, host, 8192)
    if length >= 0:
        length = _request_append(
            request,
            length,
            cstr("\r\nConnection: close\r\nUser-Agent: pcc/1\r\n\r\n"),
            8192,
        )
    if length <= 0:
        pcc_platform_close(socket_fd)
        return -4
    if _send_all(socket_fd, request, length) != 0:
        pcc_platform_close(socket_fd)
        return -5
    output_fd: int = open_file(destination, 1, 1)
    if output_fd < 0:
        pcc_platform_close(socket_fd)
        return -6
    buffer = stack_alloc(8192)
    header = stack_alloc(65536)
    header_length: int = 0
    header_done: int = 0
    status_ok: int = 0
    result: int = 0
    while True:
        count = pcc_platform_socket_recv(socket_fd, buffer, 8192, 0)
        if count < 0:
            if count == -4:
                continue
            result = -7
            break
        if count == 0:
            break
        offset: int = 0
        if header_done == 0:
            while offset < count:
                if header_length + 1 >= 65536:
                    result = -7
                    break
                byte: int = load_i8(buffer, offset)
                store_i8(header, header_length, byte)
                header_length = header_length + 1
                store_i8(header, header_length, 0)
                offset = offset + 1
                if (
                    header_length >= 4
                    and load_i8(header, header_length - 4) == 13
                    and load_i8(header, header_length - 3) == 10
                    and load_i8(header, header_length - 2) == 13
                    and load_i8(header, header_length - 1) == 10
                ):
                    header_done = 1
                    status_ok = _status_ok(header)
                    break
            if result != 0:
                break
            if header_done == 0:
                continue
        if offset < count:
            if _write_all(output_fd, ptr_add(buffer, offset), count - offset) != 0:
                result = -8
                break
    pcc_platform_close(output_fd)
    pcc_platform_close(socket_fd)
    if result != 0:
        _remove_file(destination)
        return result
    if status_ok == 0:
        _remove_file(destination)
        return -9
    return 0


@c_abi_export("py_http_download_to_file")
def py_http_download_to_file(url_object, destination_object) -> int:
    url = py_str_utf8(url_object)
    destination = py_str_utf8(destination_object)
    if _starts_with(url, cstr("https://")) != 0:
        return _download_with_system_libcurl(url, destination)
    if _starts_with(url, cstr("http://")) != 0:
        curl_result: int = _download_with_system_libcurl(url, destination)
        if curl_result == 0:
            return 0
        return _download_plain_http(url, destination)
    return -2
