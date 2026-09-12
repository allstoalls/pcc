"""Owned HTTP transport and SHA-256 helpers authored in pcc-Python.

HTTPS remains dynamically backed by the system libcurl ABI.  Plain HTTP and
all file operations use the freestanding platform surface, while SHA-256 is
implemented here so the production runtime archive needs no hand-written C
semantic helper for package acquisition.
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
    define_global_i64_array,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    function_addr,
    global_addr,
    load_i8,
    load_i64,
    logical_shift_right_i64,
    mul_overflow_i64,
    null,
    open_file,
    open_readonly,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i64,
    strlen,
    tag_int,
    target_sys_platform,
    unlinkat,
    untag_int,
)


py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_data_ptr = extern("py_bytes_data_ptr", (c_ptr,), c_ptr)
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


define_global_i64_array(
    "pcc_sha256_round_constants",
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5,
    0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
    0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
    0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC,
    0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7,
    0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
    0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
    0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
    0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3,
    0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5,
    0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
    0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
    0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
)


def _rotr32(value: int, shift: int) -> int:
    value = value & 0xFFFFFFFF
    high: int = (value * (1 << (32 - shift))) & 0xFFFFFFFF
    return (logical_shift_right_i64(value, shift) | high) & 0xFFFFFFFF


def _sha256_transform(context, block) -> None:
    words = stack_alloc(512)
    index: int = 0
    while index < 16:
        offset: int = index * 4
        word: int = (
            ((load_i8(block, offset) & 255) * 0x1000000)
            | ((load_i8(block, offset + 1) & 255) * 0x10000)
            | ((load_i8(block, offset + 2) & 255) * 0x100)
            | (load_i8(block, offset + 3) & 255)
        )
        store_i64(words, index * 8, word)
        index = index + 1
    while index < 64:
        prior15: int = load_i64(words, (index - 15) * 8) & 0xFFFFFFFF
        prior2: int = load_i64(words, (index - 2) * 8) & 0xFFFFFFFF
        sigma0: int = (
            _rotr32(prior15, 7)
            ^ _rotr32(prior15, 18)
            ^ logical_shift_right_i64(prior15, 3)
        )
        sigma1: int = (
            _rotr32(prior2, 17)
            ^ _rotr32(prior2, 19)
            ^ logical_shift_right_i64(prior2, 10)
        )
        word = (
            load_i64(words, (index - 16) * 8)
            + sigma0
            + load_i64(words, (index - 7) * 8)
            + sigma1
        ) & 0xFFFFFFFF
        store_i64(words, index * 8, word)
        index = index + 1

    a: int = load_i64(context, 0) & 0xFFFFFFFF
    b: int = load_i64(context, 8) & 0xFFFFFFFF
    c: int = load_i64(context, 16) & 0xFFFFFFFF
    d: int = load_i64(context, 24) & 0xFFFFFFFF
    e: int = load_i64(context, 32) & 0xFFFFFFFF
    f: int = load_i64(context, 40) & 0xFFFFFFFF
    g: int = load_i64(context, 48) & 0xFFFFFFFF
    h: int = load_i64(context, 56) & 0xFFFFFFFF
    constants = global_addr("pcc_sha256_round_constants")
    index = 0
    while index < 64:
        sigma1 = _rotr32(e, 6) ^ _rotr32(e, 11) ^ _rotr32(e, 25)
        choice: int = (e & f) ^ ((~e) & g)
        temp1: int = (
            h
            + sigma1
            + choice
            + load_i64(constants, index * 8)
            + load_i64(words, index * 8)
        ) & 0xFFFFFFFF
        sigma0 = _rotr32(a, 2) ^ _rotr32(a, 13) ^ _rotr32(a, 22)
        majority: int = (a & b) ^ (a & c) ^ (b & c)
        temp2: int = (sigma0 + majority) & 0xFFFFFFFF
        h = g
        g = f
        f = e
        e = (d + temp1) & 0xFFFFFFFF
        d = c
        c = b
        b = a
        a = (temp1 + temp2) & 0xFFFFFFFF
        index = index + 1
    store_i64(context, 0, (load_i64(context, 0) + a) & 0xFFFFFFFF)
    store_i64(context, 8, (load_i64(context, 8) + b) & 0xFFFFFFFF)
    store_i64(context, 16, (load_i64(context, 16) + c) & 0xFFFFFFFF)
    store_i64(context, 24, (load_i64(context, 24) + d) & 0xFFFFFFFF)
    store_i64(context, 32, (load_i64(context, 32) + e) & 0xFFFFFFFF)
    store_i64(context, 40, (load_i64(context, 40) + f) & 0xFFFFFFFF)
    store_i64(context, 48, (load_i64(context, 48) + g) & 0xFFFFFFFF)
    store_i64(context, 56, (load_i64(context, 56) + h) & 0xFFFFFFFF)


def _sha256_init(context) -> None:
    store_i64(context, 0, 0x6A09E667)
    store_i64(context, 8, 0xBB67AE85)
    store_i64(context, 16, 0x3C6EF372)
    store_i64(context, 24, 0xA54FF53A)
    store_i64(context, 32, 0x510E527F)
    store_i64(context, 40, 0x9B05688C)
    store_i64(context, 48, 0x1F83D9AB)
    store_i64(context, 56, 0x5BE0CD19)
    store_i64(context, 64, 0)
    store_i64(context, 72, 0)


def _sha256_update(context, data, length: int) -> None:
    store_i64(context, 64, load_i64(context, 64) + length * 8)
    source_offset: int = 0
    while source_offset < length:
        block_length: int = load_i64(context, 72)
        room: int = 64 - block_length
        take: int = length - source_offset
        if take > room:
            take = room
        index: int = 0
        while index < take:
            store_i8(
                context,
                80 + block_length + index,
                load_i8(data, source_offset + index),
            )
            index = index + 1
        block_length = block_length + take
        store_i64(context, 72, block_length)
        source_offset = source_offset + take
        if block_length == 64:
            _sha256_transform(context, ptr_add(context, 80))
            store_i64(context, 72, 0)


def _sha256_final(context, digest) -> None:
    block_length: int = load_i64(context, 72)
    store_i8(context, 80 + block_length, 0x80)
    block_length = block_length + 1
    if block_length > 56:
        while block_length < 64:
            store_i8(context, 80 + block_length, 0)
            block_length = block_length + 1
        _sha256_transform(context, ptr_add(context, 80))
        block_length = 0
    while block_length < 56:
        store_i8(context, 80 + block_length, 0)
        block_length = block_length + 1
    bit_count: int = load_i64(context, 64)
    index: int = 0
    while index < 8:
        store_i8(
            context,
            80 + 63 - index,
            logical_shift_right_i64(bit_count, index * 8) & 255,
        )
        index = index + 1
    _sha256_transform(context, ptr_add(context, 80))
    index = 0
    while index < 8:
        value: int = load_i64(context, index * 8)
        store_i8(digest, index * 4, logical_shift_right_i64(value, 24) & 255)
        store_i8(digest, index * 4 + 1, logical_shift_right_i64(value, 16) & 255)
        store_i8(digest, index * 4 + 2, logical_shift_right_i64(value, 8) & 255)
        store_i8(digest, index * 4 + 3, value & 255)
        index = index + 1


def _sha256_file_hex_bounded(path_object, max_bytes: int):
    if max_bytes <= 0:
        return py_str_new(cstr(""), 0)
    path = py_str_utf8(path_object)
    fd: int = open_readonly(path)
    if fd < 0:
        return py_str_new(cstr(""), 0)
    context = stack_alloc(144)
    buffer = stack_alloc(32768)
    _sha256_init(context)
    failed: int = 0
    total: int = 0
    while True:
        remaining: int = max_bytes - total
        read_limit: int = 32768
        if remaining < read_limit:
            # One sentinel byte distinguishes an exact-bound artifact from an
            # oversized file without hashing or reading an unbounded suffix.
            read_limit = remaining + 1
        count: int = pcc_platform_read(fd, buffer, read_limit)
        if count < 0:
            if count == -4:
                continue
            failed = 1
            break
        if count == 0:
            break
        if count > remaining:
            failed = 1
            break
        total = total + count
        _sha256_update(context, buffer, count)
    pcc_platform_close(fd)
    if failed != 0:
        return py_str_new(cstr(""), 0)
    digest = stack_alloc(32)
    output = stack_alloc(65)
    _sha256_final(context, digest)
    index: int = 0
    while index < 32:
        value: int = load_i8(digest, index) & 255
        high: int = logical_shift_right_i64(value, 4) & 15
        low: int = value & 15
        if high < 10:
            store_i8(output, index * 2, 48 + high)
        else:
            store_i8(output, index * 2, 87 + high)
        if low < 10:
            store_i8(output, index * 2 + 1, 48 + low)
        else:
            store_i8(output, index * 2 + 1, 87 + low)
        index = index + 1
    store_i8(output, 64, 0)
    return py_str_new(output, 64)


@c_abi_export("py_sha256_bytes_digest")
def py_sha256_bytes_digest(data_object):
    """SHA-256 of a bytes-like object, as a 32-byte ``bytes``.

    The file entry points already drive this native transform; code signing
    needs the in-memory form.  Without it `macho_codesign` fell through to the
    pure-Python `pcc.py_stdlib.hashlib`, whose per-round boxed-integer
    arithmetic turned one 6.6 MiB image signature into ~28 minutes and 5 GiB
    of RSS under self-host.
    """
    if ptr_is_null(data_object):
        return null()
    length: int = py_bytes_len(data_object)
    payload = py_bytes_data_ptr(data_object)
    if ptr_is_null(payload) and length > 0:
        return null()
    context = stack_alloc(160)
    _sha256_init(context)
    if length > 0:
        _sha256_update(context, payload, length)
    digest = stack_alloc(32)
    _sha256_final(context, digest)
    return py_bytes_new(digest, 32)


@c_abi_export("py_sha256_file_hex")
def py_sha256_file_hex(path_object):
    return _sha256_file_hex_bounded(path_object, 0x7FFFFFFFFFFFFFFF)


@c_abi_export("py_sha256_file_hex_bounded")
def py_sha256_file_hex_bounded(path_object, max_bytes: int):
    return _sha256_file_hex_bounded(path_object, max_bytes)


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
