"""Owned Gregorian calendar arithmetic shared by every platform runtime.

This module exports only PCC calendar ABIs. Platform localtime and strftime
owners remain separate, so Darwin continues to use libSystem's formatter.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import store_i32, store_i64, store_ptr

__pcc_freestanding__ = True

# Platform struct tm starts with tm_sec, tm_min, tm_hour (three C int fields).
TM_HOUR_OFFSET = 8


@c_abi_export("pcc_time_format_floor_div")
def floor_div(value: i64, divisor: i64) -> i64:
    # pcc.i64 uses machine signed division (truncation). Gregorian arithmetic
    # needs floor division; divide only nonnegative operands in either branch.
    if value >= 0:
        return value // divisor
    return -1 - ((-1 - value) // divisor)


@c_abi_export("pcc_time_format_floor_mod")
def floor_mod(value: i64, divisor: i64) -> i64:
    return value - floor_div(value, divisor) * divisor


@c_abi_export("pcc_time_days_from_civil")
def days_from_civil(year: i64, month: i64, day: i64) -> i64:
    year = year - (1 if month <= 2 else 0)
    era: i64 = floor_div(year, 400)
    yoe: i64 = year - era * 400
    adjusted: i64 = month + (9 if month <= 2 else -3)
    doy: i64 = (153 * adjusted + 2) // 5 + day - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


@c_abi_export("pcc_time_breakdown")
def breakdown(seconds: i64, offset: i64, daylight: i64, zone: c_ptr, output: c_ptr) -> i64:
    # Avoid wrapping at the explicit i64 platform boundary before checking
    # the representable struct-tm year. Python timestamps remain arbitrary ints.
    if offset > 0 and seconds > 9223372036854775807 - offset:
        return -75
    if offset < 0 and seconds < -9223372036854775808 - offset:
        return -75
    adjusted: i64 = seconds + offset
    days: i64 = floor_div(adjusted, 86400)
    clock: i64 = floor_mod(adjusted, 86400)
    z: i64 = days + 719468
    era: i64 = floor_div(z, 146097)
    doe: i64 = z - era * 146097
    yoe: i64 = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year: i64 = yoe + era * 400
    doy: i64 = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp: i64 = (5 * doy + 2) // 153
    day: i64 = doy - (153 * mp + 2) // 5 + 1
    month: i64 = mp + (3 if mp < 10 else -9)
    year = year + (1 if month <= 2 else 0)
    if year - 1900 < -2147483648 or year - 1900 > 2147483647:
        return -75
    store_i32(output, 0, clock % 60)
    store_i32(output, 4, (clock // 60) % 60)
    store_i32(output, TM_HOUR_OFFSET, clock // 3600)
    store_i32(output, 12, day)
    store_i32(output, 16, month - 1)
    store_i32(output, 20, year - 1900)
    store_i32(output, 24, floor_mod(days + 4, 7))
    store_i32(output, 28, days - days_from_civil(year, 1, 1))
    store_i32(output, 32, daylight)
    store_i64(output, 40, offset)
    store_ptr(output, 48, zone)
    return 0
