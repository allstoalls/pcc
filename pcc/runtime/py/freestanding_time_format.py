"""Owned C-locale strftime and process-lifetime time-zone names.

Platform localtime owners supply offset, DST and zone name. No host calendar
or locale library participates in formatting.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, extern
from pcc.unsafe import (
    load_i8, load_i32, load_i64, load_ptr, store_i8, store_i64,
    store_ptr, ptr_add, ptr_is_null, stack_alloc, cstr,
    define_global_i8, define_global_ptr_null, global_addr, global_load_ptr, global_store_ptr,
    atomic_test_and_set, atomic_clear, malloc, null,
)

__pcc_freestanding__ = True

# Platform struct tm starts with tm_sec, tm_min, tm_hour (three C int fields).
TM_HOUR_OFFSET = 8

floor_div = extern("pcc_time_format_floor_div", (c_int64, c_int64), c_int64)
floor_mod = extern("pcc_time_format_floor_mod", (c_int64, c_int64), c_int64)
days_from_civil = extern("pcc_time_days_from_civil", (c_int64, c_int64, c_int64), c_int64)


define_global_i8("pcc_time_zone_lock", 0)
define_global_ptr_null("pcc_time_zone_names")


@c_abi_export("pcc_time_intern_zone")
def intern_zone(text: c_ptr, size: i64) -> c_ptr:
    # tm_zone pointers can outlive both localtime_r calls and their threads.
    # Keep one process-lifetime copy of each distinct abbreviation.
    while atomic_test_and_set(global_addr("pcc_time_zone_lock"), 0, "acquire"):
        pass
    record = global_load_ptr("pcc_time_zone_names")
    while not ptr_is_null(record):
        if load_i64(record, 8) == size:
            index: i64 = 0
            while index < size and load_i8(record, 16 + index) == load_i8(text, index):
                index = index + 1
            if index == size:
                atomic_clear(global_addr("pcc_time_zone_lock"), 0, "release")
                return ptr_add(record, 16)
        record = load_ptr(record, 0)
    record = malloc(size + 17)
    if ptr_is_null(record):
        atomic_clear(global_addr("pcc_time_zone_lock"), 0, "release")
        return null()
    store_ptr(record, 0, global_load_ptr("pcc_time_zone_names"))
    store_i64(record, 8, size)
    index: i64 = 0
    while index < size:
        store_i8(record, 16 + index, load_i8(text, index))
        index = index + 1
    store_i8(record, 16 + size, 0)
    global_store_ptr("pcc_time_zone_names", record)
    atomic_clear(global_addr("pcc_time_zone_lock"), 0, "release")
    return ptr_add(record, 16)


@c_abi_export("pcc_time_format_iso_weeks")
def iso_weeks(year: i64) -> i64:
    first: i64 = floor_mod(days_from_civil(year, 1, 1) + 3, 7) + 1
    leap: i64 = 1 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 0
    if first == 4 or (first == 3 and leap):
        return 53
    return 52


@c_abi_export("pcc_time_format_word")
def word(index: i64, month: i64, full: i64) -> c_ptr:
    if month:
        if not full:
            return ptr_add(cstr("Jan\0Feb\0Mar\0Apr\0May\0Jun\0Jul\0Aug\0Sep\0Oct\0Nov\0Dec\0"), index * 4)
        if index == 0: return cstr("January")
        if index == 1: return cstr("February")
        if index == 2: return cstr("March")
        if index == 3: return cstr("April")
        if index == 4: return cstr("May")
        if index == 5: return cstr("June")
        if index == 6: return cstr("July")
        if index == 7: return cstr("August")
        if index == 8: return cstr("September")
        if index == 9: return cstr("October")
        if index == 10: return cstr("November")
        return cstr("December")
    if not full:
        return ptr_add(cstr("Sun\0Mon\0Tue\0Wed\0Thu\0Fri\0Sat\0"), index * 4)
    if index == 0: return cstr("Sunday")
    if index == 1: return cstr("Monday")
    if index == 2: return cstr("Tuesday")
    if index == 3: return cstr("Wednesday")
    if index == 4: return cstr("Thursday")
    if index == 5: return cstr("Friday")
    return cstr("Saturday")


@c_abi_export("pcc_time_format_number")
def number(output: c_ptr, value: i64, width: i64, padding: i64) -> None:
    temp = stack_alloc(32)
    count: i64 = 0
    negative: i64 = 1 if value < 0 else 0
    if negative:
        value = 0 - value
    while True:
        store_i8(temp, count, 48 + value % 10)
        count = count + 1
        value = value // 10
        if value == 0:
            break
    out: i64 = 0
    if negative:
        store_i8(output, out, 45)
        out = out + 1
    while out + count < width:
        store_i8(output, out, padding)
        out = out + 1
    while count:
        count = count - 1
        store_i8(output, out, load_i8(temp, count))
        out = out + 1
    store_i8(output, out, 0)


@c_abi_export("strftime")
def strftime(output: c_ptr, capacity: i64, fmt: c_ptr, tm: c_ptr) -> i64:
    if capacity <= 0 or ptr_is_null(fmt) or ptr_is_null(tm):
        return 0
    month: i64 = load_i32(tm, 16)
    weekday: i64 = load_i32(tm, 24)
    if month < 0 or month > 11 or weekday < 0 or weekday > 6:
        return 0
    year: i64 = load_i32(tm, 20) + 1900
    position: i64 = 0
    out: i64 = 0
    temporary = stack_alloc(64)
    while load_i8(fmt, position) != 0:
        code: i64 = load_i8(fmt, position) & 255
        position = position + 1
        text = temporary
        store_i8(temporary, 0, code)
        store_i8(temporary, 1, 0)
        if code == 37:
            code = load_i8(fmt, position) & 255
            if code == 0:
                return 0
            position = position + 1
            if code == 69 or code == 79:
                code = load_i8(fmt, position) & 255
                position = position + 1
            value: i64 = -1
            width: i64 = 2
            padding: i64 = 48
            composite = cstr("")
            if code == 89: value = year; width = 4
            elif code == 121: value = floor_mod(year, 100)
            elif code == 67: value = floor_div(year, 100)
            elif code == 109: value = month + 1
            elif code == 100 or code == 101:
                value = load_i32(tm, 12)
                if code == 101: padding = 32
            elif code == 72: value = load_i32(tm, TM_HOUR_OFFSET)
            elif code == 73:
                value = load_i32(tm, TM_HOUR_OFFSET) % 12
                if value == 0: value = 12
            elif code == 77: value = load_i32(tm, 4)
            elif code == 83: value = load_i32(tm, 0)
            elif code == 106: value = load_i32(tm, 28) + 1; width = 3
            elif code == 119: value = weekday; width = 1
            elif code == 117: value = 7 if weekday == 0 else weekday; width = 1
            elif code == 85: value = (load_i32(tm, 28) + 7 - weekday) // 7
            elif code == 87: value = (load_i32(tm, 28) + 7 - (weekday + 6) % 7) // 7
            elif code == 71 or code == 103 or code == 86:
                iso_year: i64 = year
                iso_day: i64 = 7 if weekday == 0 else weekday
                week: i64 = (load_i32(tm, 28) + 11 - iso_day) // 7
                if week < 1:
                    iso_year = iso_year - 1
                    week = iso_weeks(iso_year)
                elif week > iso_weeks(iso_year):
                    iso_year = iso_year + 1
                    week = 1
                value = week if code == 86 else floor_mod(iso_year, 100) if code == 103 else iso_year
                width = 4 if code == 71 else 2
            elif code == 115:
                value = days_from_civil(year, month + 1, load_i32(tm, 12)) * 86400
                value = value + load_i32(tm, TM_HOUR_OFFSET) * 3600 + load_i32(tm, 4) * 60 + load_i32(tm, 0) - load_i64(tm, 40)
                width = 1
            elif code == 97 or code == 65: text = word(weekday, 0, 1 if code == 65 else 0)
            elif code == 98 or code == 104 or code == 66: text = word(month, 1, 1 if code == 66 else 0)
            elif code == 112: text = cstr("AM") if load_i32(tm, TM_HOUR_OFFSET) < 12 else cstr("PM")
            elif code == 90:
                text = load_ptr(tm, 48)
                if ptr_is_null(text): text = cstr("")
            elif code == 122:
                offset: i64 = load_i64(tm, 40)
                store_i8(temporary, 0, 45 if offset < 0 else 43)
                if offset < 0: offset = 0 - offset
                number(ptr_add(temporary, 1), offset // 3600, 2, 48)
                number(ptr_add(temporary, 3), (offset // 60) % 60, 2, 48)
            elif code == 99: composite = cstr("%a %b %e %H:%M:%S %Y")
            elif code == 120 or code == 68: composite = cstr("%m/%d/%y")
            elif code == 88 or code == 84: composite = cstr("%H:%M:%S")
            elif code == 70: composite = cstr("%Y-%m-%d")
            elif code == 82: composite = cstr("%H:%M")
            elif code == 114: composite = cstr("%I:%M:%S %p")
            elif code == 110: text = cstr("\n")
            elif code == 116: text = cstr("\t")
            elif code == 37: text = cstr("%")
            else:
                return 0
            if value != -1 or code == 89 or code == 67 or code == 71 or code == 115:
                number(temporary, value, width, padding)
            if load_i8(composite, 0) != 0:
                count: i64 = strftime(ptr_add(output, out), capacity - out, composite, tm)
                if count == 0:
                    return 0
                out = out + count
                continue
        index: i64 = 0
        while load_i8(text, index) != 0:
            if out + 1 >= capacity:
                return 0
            store_i8(output, out, load_i8(text, index))
            out = out + 1
            index = index + 1
    store_i8(output, out, 0)
    return out
