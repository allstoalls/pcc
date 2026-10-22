"""Freestanding wall clock, monotonic clock, and sleep primitives."""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    clock_gettime,
    load_i64,
    nanosleep,
    stack_alloc,
    store_i64,
    unsigned_div_i64,
    unsigned_rem_i64,
)

__pcc_freestanding__ = True


@c_abi_export("pcc_platform_wall_time_us")
def pcc_platform_wall_time_us() -> i64:
    value = stack_alloc(16)
    if clock_gettime(0, value) != 0:
        return 0
    return load_i64(value, 0) * 1000000 + unsigned_div_i64(load_i64(value, 8), 1000)


@c_abi_export("pcc_platform_monotonic_us")
def pcc_platform_monotonic_us() -> i64:
    value = stack_alloc(16)
    if clock_gettime(1, value) != 0:
        return 0
    return load_i64(value, 0) * 1000000 + unsigned_div_i64(load_i64(value, 8), 1000)


@c_abi_export("pcc_platform_wall_time_ns")
def pcc_platform_wall_time_ns() -> i64:
    value = stack_alloc(16)
    if clock_gettime(0, value) != 0:
        return 0
    return load_i64(value, 0) * 1000000000 + load_i64(value, 8)


@c_abi_export("pcc_platform_monotonic_ns")
def pcc_platform_monotonic_ns() -> i64:
    value = stack_alloc(16)
    if clock_gettime(1, value) != 0:
        return 0
    return load_i64(value, 0) * 1000000000 + load_i64(value, 8)


@c_abi_export("pcc_platform_sleep_ns")
def pcc_platform_sleep_ns(delay_ns: i64) -> i64:
    if delay_ns <= 0:
        return 0
    request = stack_alloc(16)
    remaining = stack_alloc(16)
    store_i64(request, 0, unsigned_div_i64(delay_ns, 1000000000))
    store_i64(request, 8, unsigned_rem_i64(delay_ns, 1000000000))
    attempts: i64 = 0
    while nanosleep(request, remaining) != 0:
        if attempts >= 64:
            return -1
        seconds = load_i64(remaining, 0)
        nanoseconds = load_i64(remaining, 8)
        if seconds < 0 or nanoseconds < 0 or nanoseconds >= 1000000000:
            return -1
        old_request = request
        request = remaining
        remaining = old_request
        attempts = attempts + 1
    return 0
