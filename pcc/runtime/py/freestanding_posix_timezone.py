"""POSIX TZ rule evaluation used by the TZif v2/v3/v4 footer."""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, extern
from pcc.unsafe import load_i8, load_i32, load_i64, store_i64, store_ptr, ptr_add, stack_alloc, cstr

__pcc_freestanding__ = True

days_from_civil = extern("pcc_time_days_from_civil", (c_int64, c_int64, c_int64), c_int64)
breakdown = extern("pcc_time_breakdown", (c_int64, c_int64, c_int64, c_ptr, c_ptr), c_int64)


@c_abi_export("pcc_posix_timezone_leap")
def leap(year: i64) -> i64:
    if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        return 1
    return 0


@c_abi_export("pcc_posix_timezone_read_number")
def read_number(text: c_ptr, pos: i64, output: c_ptr) -> i64:
    value: i64 = 0
    start: i64 = pos
    while True:
        byte: i64 = load_i8(text, pos)
        if byte < 48 or byte > 57:
            break
        value = value * 10 + byte - 48
        if value > 1000000:
            return -1
        pos = pos + 1
    if pos == start:
        return -1
    store_i64(output, 0, value)
    return pos


@c_abi_export("pcc_posix_timezone_name")
def name(text: c_ptr, pos: i64, output: c_ptr) -> i64:
    start: i64 = pos
    if load_i8(text, pos) == 60:
        pos = pos + 1
        start = pos
        while load_i8(text, pos) != 62:
            if load_i8(text, pos) == 0:
                return -1
            pos = pos + 1
        size: i64 = pos - start
        pos = pos + 1
    else:
        while True:
            byte: i64 = load_i8(text, pos)
            if not (65 <= byte <= 90 or 97 <= byte <= 122):
                break
            pos = pos + 1
        size: i64 = pos - start
    if size < 3:
        return -1
    store_ptr(output, 0, ptr_add(text, start))
    store_i64(output, 8, size)
    return pos


@c_abi_export("pcc_posix_timezone_offset")
def offset(text: c_ptr, pos: i64, output: c_ptr) -> i64:
    sign: i64 = 1
    if load_i8(text, pos) == 45:
        sign = -1
        pos = pos + 1
    elif load_i8(text, pos) == 43:
        pos = pos + 1
    temporary = stack_alloc(8)
    pos = read_number(text, pos, temporary)
    if pos < 0:
        return -1
    hours: i64 = load_i64(temporary, 0)
    if hours > 167:
        return -1
    value: i64 = hours * 3600
    scale: i64 = 60
    while scale and load_i8(text, pos) == 58:
        pos = read_number(text, pos + 1, temporary)
        if pos < 0 or load_i64(temporary, 0) > 59:
            return -1
        value = value + load_i64(temporary, 0) * scale
        scale = 1 if scale == 60 else 0
    store_i64(output, 0, value * sign)
    return pos


@c_abi_export("pcc_posix_timezone_rule")
def rule(text: c_ptr, pos: i64, year: i64, output: c_ptr) -> i64:
    temporary = stack_alloc(8)
    day: i64 = 0
    if load_i8(text, pos) == 77:
        pos = read_number(text, pos + 1, temporary)
        if pos < 0:
            return -1
        month: i64 = load_i64(temporary, 0)
        if month < 1 or month > 12 or load_i8(text, pos) != 46:
            return -1
        pos = read_number(text, pos + 1, temporary)
        if pos < 0:
            return -1
        week: i64 = load_i64(temporary, 0)
        if week < 1 or week > 5 or load_i8(text, pos) != 46:
            return -1
        pos = read_number(text, pos + 1, temporary)
        if pos < 0:
            return -1
        weekday: i64 = load_i64(temporary, 0)
        if weekday > 6:
            return -1
        first: i64 = days_from_civil(year, month, 1)
        next_month: i64 = days_from_civil(year + 1, 1, 1) if month == 12 else days_from_civil(year, month + 1, 1)
        first_weekday: i64 = (first + 4) % 7
        if first_weekday < 0: first_weekday = first_weekday + 7
        dom: i64 = 1 + (weekday - first_weekday + 7) % 7 + (week - 1) * 7
        if dom > next_month - first:
            dom = dom - 7
        day = first - days_from_civil(year, 1, 1) + dom - 1
    else:
        julian: i64 = 1 if load_i8(text, pos) == 74 else 0
        pos = read_number(text, pos + julian, temporary)
        if pos < 0:
            return -1
        day = load_i64(temporary, 0)
        if day > 365 or (julian and day == 0):
            return -1
        if julian:
            day = day - 1
            if leap(year) and day >= 59:
                day = day + 1
    seconds: i64 = 7200
    if load_i8(text, pos) == 47:
        pos = offset(text, pos + 1, temporary)
        if pos < 0:
            return -1
        seconds = load_i64(temporary, 0)
    store_i64(output, 0, (days_from_civil(year, 1, 1) + day) * 86400 + seconds)
    return pos


@c_abi_export("pcc_time_posix_zone")
def posix_zone(text: c_ptr, timestamp: i64, output: c_ptr) -> i64:
    # output: offset@0, DST@8, abbreviation pointer@16, length@24.
    standard = stack_alloc(16)
    daylight = stack_alloc(16)
    value = stack_alloc(8)
    pos: i64 = name(text, 0, standard)
    if pos < 0:
        return -1
    pos = offset(text, pos, value)
    if pos < 0:
        return -1
    standard_offset: i64 = 0 - load_i64(value, 0)
    selected_offset: i64 = standard_offset
    selected_name = standard
    dst: i64 = 0
    if load_i8(text, pos) != 0:
        pos = name(text, pos, daylight)
        if pos < 0:
            return -1
        daylight_offset: i64 = standard_offset + 3600
        if load_i8(text, pos) != 44:
            pos = offset(text, pos, value)
            if pos < 0:
                return -1
            daylight_offset = 0 - load_i64(value, 0)
        if load_i8(text, pos) != 44:
            return -1
        tm = stack_alloc(64)
        calendar_status: i64 = breakdown(timestamp, standard_offset, 0, cstr(""), tm)
        if calendar_status != 0:
            return calendar_status
        year: i64 = load_i32(tm, 20) + 1900
        pos = rule(text, pos + 1, year, value)
        if pos < 0 or load_i8(text, pos) != 44:
            return -1
        begin: i64 = load_i64(value, 0) - standard_offset
        pos = rule(text, pos + 1, year, value)
        if pos < 0 or load_i8(text, pos) != 0:
            return -1
        end: i64 = load_i64(value, 0) - daylight_offset
        if (begin < end and begin <= timestamp < end) or (begin > end and (timestamp >= begin or timestamp < end)):
            dst = 1
            selected_offset = daylight_offset
            selected_name = daylight
    store_i64(output, 0, selected_offset)
    store_i64(output, 8, dst)
    from pcc.unsafe import load_ptr
    store_ptr(output, 16, load_ptr(selected_name, 0))
    store_i64(output, 24, load_i64(selected_name, 8))
    return 0
