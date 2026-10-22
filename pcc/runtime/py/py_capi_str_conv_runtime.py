"""pcc-Python owners for the no-libpython string->number conversions.

Replaces PyFloat_FromString / PyLong_FromUnicodeObject in py_capi_shim.c,
which used libc strtod / strtoll.  The pcc-Python ports implement the parsers
directly (no libc dependency): a decimal/exponent float parser using i64
mantissa accumulation + f64 scaling, and an integer parser for base 0/2/8/10/16.

Owned surface (stable C ABI names):

  PyFloat_FromString, PyLong_FromUnicodeObject
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_STR

from pcc.extern import c_abi_typed_export, c_double, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    i64_to_float,
    is_tagged_int,
    load_i32,
    load_i8,
    null,
    ptr_is_null,
    stack_alloc,
    store_i64,
)

py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
PyUnicode_AsUTF8 = extern("PyUnicode_AsUTF8", (c_ptr,), c_ptr)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _digit_value(c: int) -> int:
    if 48 <= c <= 57:  # 0-9
        return c - 48
    if 65 <= c <= 70:  # A-F
        return c - 55
    if 97 <= c <= 102:  # a-f
        return c - 87
    return -1


def _parse_int(s, base: int, end_ptr) -> c_ptr:
    # Returns PyInt object or NULL; sets end_ptr to the position after digits.
    i: int = 0
    c: int = load_i8(s, 0)
    neg: int = 0
    if c == 45:  # '-'
        neg = 1
        i = 1
    elif c == 43:  # '+'
        i = 1
    start: int = i
    if base == 0:
        c2: int = load_i8(s, i)
        if c2 == 48:  # '0'
            c3: int = load_i8(s, i + 1)
            if c3 == 120 or c3 == 88:  # 0x X
                base = 16
                i += 2
            elif c3 == 111 or c3 == 79:  # 0o O
                base = 8
                i += 2
            elif c3 == 98 or c3 == 66:  # 0b B
                base = 2
                i += 2
            else:
                base = 8
        else:
            base = 10
    elif base == 16:
        c2: int = load_i8(s, i)
        if c2 == 48 and (load_i8(s, i + 1) == 120 or load_i8(s, i + 1) == 88):
            i += 2  # skip 0x/0X prefix (strtoll semantics for explicit base 16)
    value: int = 0
    digits: int = 0
    while True:
        c4: int = load_i8(s, i)
        d = _digit_value(c4)
        if d < 0 or d >= base:
            break
        value = value * base + d
        digits += 1
        i += 1
    store_i64(end_ptr, 0, i)
    if digits == 0:
        return null()
    if neg != 0:
        value = -value
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromUnicodeObject", "ptr", ("ptr", "i32"))
def PyLong_FromUnicodeObject(u, base: int) -> c_ptr:
    if ptr_is_null(u):
        _type_error(cstr("PyLong_FromUnicodeObject requires a str"))
        return null()
    if is_tagged_int(u):
        _type_error(cstr("PyLong_FromUnicodeObject requires a str"))
        return null()
    if load_i32(u, 8) != PY_TYPE_STR:  # PY_TYPE_STR
        _type_error(cstr("PyLong_FromUnicodeObject requires a str"))
        return null()
    s = py_str_utf8(u)
    if ptr_is_null(s):
        return null()
    end_slot = stack_alloc(8)
    store_i64(end_slot, 0, 0)
    result = _parse_int(s, base, end_slot)
    if ptr_is_null(result):
        _value_error(cstr("invalid literal for int()"))
        return null()
    return result


def _parse_float(s) -> c_ptr:
    # Manual decimal/exponent float parser -> f64 (no libc strtod).
    i: int = 0
    c: int = load_i8(s, 0)
    neg: int = 0
    if c == 45:
        neg = 1
        i = 1
    elif c == 43:
        i = 1
    int_part: int = 0
    digits: int = 0
    while True:
        c2: int = load_i8(s, i)
        d = _digit_value(c2)
        if d < 0:
            break
        int_part = int_part * 10 + d
        digits += 1
        i += 1
    frac_part: int = 0
    frac_len: int = 0
    if load_i8(s, i) == 46:  # '.'
        i += 1
        while True:
            c3: int = load_i8(s, i)
            d = _digit_value(c3)
            if d < 0:
                break
            frac_part = frac_part * 10 + d
            frac_len += 1
            i += 1
    exp: int = 0
    if load_i8(s, i) == 101 or load_i8(s, i) == 69:  # e E
        i += 1
        exp_neg: int = 0
        c4: int = load_i8(s, i)
        if c4 == 45:
            exp_neg = 1
            i += 1
        elif c4 == 43:
            i += 1
        exp_digits: int = 0
        while True:
            c5: int = load_i8(s, i)
            d = _digit_value(c5)
            if d < 0:
                break
            exp = exp * 10 + d
            exp_digits += 1
            i += 1
        if exp_neg != 0:
            exp = -exp
    if digits == 0 and frac_len == 0:
        return null()
    # value = (int_part + frac_part / 10^frac_len) * 10^exp
    mantissa: float = 0.0
    k: int = 0
    while k < frac_len:
        mantissa = mantissa * 0.1
        k += 1
    mantissa = i64_to_float(frac_part) * mantissa
    mantissa = mantissa + i64_to_float(int_part)
    e: int = 0
    while e < exp:
        mantissa = mantissa * 10.0
        e += 1
    while e > exp:
        mantissa = mantissa * 0.1
        e -= 1
    if neg != 0:
        mantissa = 0.0 - mantissa
    return py_float_from_f64(mantissa)


@c_abi_typed_export("PyFloat_FromString", "ptr", ("ptr",))
def PyFloat_FromString(text) -> c_ptr:
    if ptr_is_null(text):
        _type_error(cstr("PyFloat_FromString requires a str"))
        return null()
    if is_tagged_int(text):
        _type_error(cstr("PyFloat_FromString requires a str"))
        return null()
    if load_i32(text, 8) != PY_TYPE_STR:
        _type_error(cstr("PyFloat_FromString requires a str"))
        return null()
    s = py_str_utf8(text)
    if ptr_is_null(s):
        return null()
    result = _parse_float(s)
    if ptr_is_null(result):
        _value_error(cstr("could not convert string to float"))
        return null()
    return result
