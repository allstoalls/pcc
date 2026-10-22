"""Freestanding process RSS sampling for Darwin and Linux."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import (
    close,
    cstr,
    darwin_current_rss_bytes,
    darwin_peak_rss_bytes,
    int_to_ptr,
    load_i8,
    memset,
    open_readonly,
    read,
    stack_alloc,
)


__pcc_freestanding__ = True


@c_abi_export("pcc_platform_rss_linux_proc_status_bytes")
def _linux_proc_status_bytes(want_peak: i64) -> i64:
    fd = open_readonly(cstr("/proc/self/status"))
    if fd < 0:
        return -1
    buffer = stack_alloc(32768)
    size = read(fd, buffer, 32768)
    close(fd)
    if size <= 0:
        return -1

    cursor: i64 = 0
    while cursor + 6 < size:
        matched: i64 = 0
        if load_i8(buffer, cursor) == 86 and load_i8(buffer, cursor + 1) == 109:
            if want_peak != 0:
                if (
                    load_i8(buffer, cursor + 2) == 72
                    and load_i8(buffer, cursor + 3) == 87
                    and load_i8(buffer, cursor + 4) == 77
                    and load_i8(buffer, cursor + 5) == 58
                ):
                    matched: i64 = 1
            else:
                if (
                    load_i8(buffer, cursor + 2) == 82
                    and load_i8(buffer, cursor + 3) == 83
                    and load_i8(buffer, cursor + 4) == 83
                    and load_i8(buffer, cursor + 5) == 58
                ):
                    matched: i64 = 1
        if matched != 0:
            number = cursor + 6
            while number < size and (
                load_i8(buffer, number) == 32 or load_i8(buffer, number) == 9
            ):
                number = number + 1
            value: i64 = 0
            digits: i64 = 0
            while number < size:
                byte = load_i8(buffer, number)
                if byte < 48 or byte > 57:
                    break
                value = value * 10 + byte - 48
                digits = digits + 1
                number = number + 1
            if digits != 0:
                return value * 1024
            return -1
        while cursor < size and load_i8(buffer, cursor) != 10:
            cursor = cursor + 1
        cursor = cursor + 1
    return -1


@c_abi_export("pcc_os_current_rss_bytes")
def pcc_os_current_rss_bytes() -> i64:
    value = darwin_current_rss_bytes()
    if value >= 0:
        return value
    return _linux_proc_status_bytes(0)


@c_abi_export("pcc_os_peak_rss_bytes")
def pcc_os_peak_rss_bytes() -> i64:
    value = darwin_peak_rss_bytes()
    if value >= 0:
        return value
    return _linux_proc_status_bytes(1)


@c_abi_export("pcc_worker_resource_replace")
def pcc_worker_resource_replace(source: c_ptr, destination: c_ptr) -> i64:
    # Only the Windows publisher uses this extended-rename contract. Other
    # targets keep their existing py_os_replace path, never a host fallback.
    return -38


@c_abi_export("pcc_worker_resource_replace_detailed")
def pcc_worker_resource_replace_detailed(source: c_ptr, destination: c_ptr,
                                         diagnostic_address: i64) -> i64:
    # Same unsupported-operation result as the original platform stub.
    memset(int_to_ptr(diagnostic_address), 0, 24)
    return -38
