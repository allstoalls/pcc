"""Freestanding C-locale ``strtol``/``strtoul`` owners.

The production Python runtime exposes these names to C-API consumers, but a
Linux zero-libc artifact cannot delegate them to glibc.  This module owns the
portable scanner and leaves only errno storage at the compiler/runtime machine
boundary.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_abi_typed_export, c_int32, c_void, extern
from pcc.unsafe import (
    load_i8,
    null,
    ptr_add,
    ptr_is_null,
    store_ptr,
    unsigned_div_i64,
    unsigned_greater_i64,
    unsigned_rem_i64,
    wrapping_mul_i64,
)

__pcc_freestanding__ = True


pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)


@c_abi_export("pcc_strtoint_digit")
def _digit(byte: i64) -> i64:
    byte = byte & 255
    if byte >= 48 and byte <= 57:
        return byte - 48
    if byte >= 65 and byte <= 90:
        return byte - 55
    if byte >= 97 and byte <= 122:
        return byte - 87
    return -1


@c_abi_export("pcc_strtoint_is_space")
def _is_space(byte: i64) -> i64:
    byte = byte & 255
    if byte == 32 or byte == 9 or byte == 10:
        return 1
    if byte == 11 or byte == 12 or byte == 13:
        return 1
    return 0


@c_abi_export("pcc_strtoint_store_end")
def _store_end(endptr, value) -> None:
    if not ptr_is_null(endptr):
        store_ptr(endptr, 0, value)


@c_abi_export("pcc_strtoint_parse")
def _parse(text, endptr, base: i64, unsigned_mode: i64) -> i64:
    original = text
    if ptr_is_null(text):
        _store_end(endptr, null())
        return 0
    if base != 0 and (base < 2 or base > 36):
        pcc_errno_set(22)  # EINVAL
        _store_end(endptr, original)
        return 0

    cursor = text
    while _is_space(load_i8(cursor, 0)) != 0:
        cursor = ptr_add(cursor, 1)
    negative: i64 = 0
    if load_i8(cursor, 0) == 45:
        negative: i64 = 1
        cursor = ptr_add(cursor, 1)
    elif load_i8(cursor, 0) == 43:
        cursor = ptr_add(cursor, 1)

    if base == 0:
        base: i64 = 10
        if load_i8(cursor, 0) == 48:
            base: i64 = 8
            marker: i64 = load_i8(cursor, 1) & 255
            if marker == 120 or marker == 88:
                next_digit: i64 = _digit(load_i8(cursor, 2))
                if next_digit >= 0 and next_digit < 16:
                    base: i64 = 16
                    cursor = ptr_add(cursor, 2)
    elif base == 16:
        if load_i8(cursor, 0) == 48:
            marker = load_i8(cursor, 1) & 255
            if marker == 120 or marker == 88:
                next_digit = _digit(load_i8(cursor, 2))
                if next_digit >= 0 and next_digit < 16:
                    cursor = ptr_add(cursor, 2)

    limit: i64 = -1
    if unsigned_mode == 0:
        limit: i64 = 9223372036854775807
        if negative != 0:
            limit: i64 = -9223372036854775808
    cutoff: i64 = unsigned_div_i64(limit, base)
    cutlim: i64 = unsigned_rem_i64(limit, base)
    value: i64 = 0
    digits: i64 = 0
    overflow: i64 = 0
    while True:
        digit: i64 = _digit(load_i8(cursor, 0))
        if digit < 0 or digit >= base:
            break
        if overflow == 0:
            if unsigned_greater_i64(value, cutoff) or (
                value == cutoff and digit > cutlim
            ):
                overflow: i64 = 1
            else:
                value = wrapping_mul_i64(value, base) + digit
        digits = digits + 1
        cursor = ptr_add(cursor, 1)

    if digits == 0:
        _store_end(endptr, original)
        return 0
    _store_end(endptr, cursor)
    if overflow != 0:
        pcc_errno_set(34)  # ERANGE
        if unsigned_mode != 0:
            return -1
        if negative != 0:
            return -9223372036854775808
        return 9223372036854775807
    if negative != 0:
        return 0 - value
    return value


@c_abi_typed_export("strtol", "i64", ("ptr", "ptr", "i32"))
def strtol(text, endptr, base: i64) -> i64:
    return _parse(text, endptr, base, 0)


@c_abi_typed_export("strtoul", "i64", ("ptr", "ptr", "i32"))
def strtoul(text, endptr, base: i64) -> i64:
    return _parse(text, endptr, base, 1)
