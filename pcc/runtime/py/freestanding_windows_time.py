"""Windows localtime through the named dynamic-time-zone system ABI."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int64, extern
from pcc.unsafe import (
    stack_alloc, load_i8, load_i32, load_i64, store_i8, ptr_add, ptr_is_null,
    null, global_load_ptr, global_store_ptr, define_thread_local_ptr_null, free, cstr,
)

__pcc_freestanding__ = True

GetDynamicTimeZoneInformation = extern("GetDynamicTimeZoneInformation", (c_ptr,), c_int)
SystemTimeToTzSpecificLocalTimeEx = extern("SystemTimeToTzSpecificLocalTimeEx", (c_ptr, c_ptr, c_ptr), c_int)
breakdown = extern("pcc_time_breakdown", (c_int64, c_int64, c_int64, c_ptr, c_ptr), c_int64)
days_from_civil = extern("pcc_time_days_from_civil", (c_int64, c_int64, c_int64), c_int64)
utf8 = extern("pcc_win_utf8", (c_ptr,), c_ptr)

intern_zone = extern("pcc_time_intern_zone", (c_ptr, c_int64), c_ptr)


@c_abi_export("pcc_windows_time_put16")
def put16(buffer: c_ptr, offset: i64, value: i64) -> None:
    store_i8(buffer, offset, value & 255)
    store_i8(buffer, offset + 1, (value >> 8) & 255)


@c_abi_export("pcc_windows_time_get16")
def get16(buffer: c_ptr, offset: i64) -> i64:
    return (load_i8(buffer, offset) & 255) | ((load_i8(buffer, offset + 1) & 255) << 8)


@c_abi_export("localtime_r")
def localtime_r(clock: c_ptr, output: c_ptr) -> c_ptr:
    timestamp: i64 = load_i64(clock, 0)
    utc_tm = stack_alloc(64)
    if breakdown(timestamp, 0, 0, cstr("UTC"), utc_tm) != 0:
        return null()
    year: i64 = load_i32(utc_tm, 20) + 1900
    if year < 1601 or year > 30827:
        return null()
    utc = stack_alloc(16)
    local = stack_alloc(16)
    put16(utc, 0, year)
    put16(utc, 2, load_i32(utc_tm, 16) + 1)
    put16(utc, 4, load_i32(utc_tm, 24))
    put16(utc, 6, load_i32(utc_tm, 12))
    put16(utc, 8, load_i32(utc_tm, 8))
    put16(utc, 10, load_i32(utc_tm, 4))
    put16(utc, 12, load_i32(utc_tm, 0))
    put16(utc, 14, 0)
    timezone = stack_alloc(432)
    if GetDynamicTimeZoneInformation(timezone) == -1:
        return null()
    if not SystemTimeToTzSpecificLocalTimeEx(timezone, utc, local):
        return null()
    local_seconds: i64 = days_from_civil(get16(local, 0), get16(local, 2), get16(local, 6)) * 86400
    local_seconds = local_seconds + get16(local, 8) * 3600 + get16(local, 10) * 60 + get16(local, 12)
    offset: i64 = local_seconds - timestamp
    daylight: i64 = 0
    if load_i32(timezone, 84) != load_i32(timezone, 168):
        if offset == 0 - (load_i32(timezone, 0) + load_i32(timezone, 168)) * 60:
            daylight = 1
    label = utf8(ptr_add(timezone, 88 if daylight else 4))
    if ptr_is_null(label):
        return null()
    size: i64 = 0
    while load_i8(label, size) != 0:
        size = size + 1
    stable = intern_zone(label, size)
    free(label)
    if ptr_is_null(stable):
        return null()
    if breakdown(timestamp, offset, daylight, stable, output) == 0:
        return output
    return null()
