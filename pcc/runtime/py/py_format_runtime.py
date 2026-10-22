"""pcc-Python owner for object, numeric, and percent formatting ABIs.

The host-C oracle is ``src/py_format.c``.  This module deliberately builds
text with pcc-owned buffers and the freestanding stdio numeric formatter; it
does not replace the C helper with a call back into ``snprintf``.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PY_FLAG_EXC_UNICODE_PAYLOAD,
    PY_FLAG_EXC_OS_PAYLOAD,
    PYCLASSOBJECT_NAME_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_COMPLEX,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
)

from pcc.extern import c_abi_export, c_double, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_i64_i64_ptr,
    cstr,
    define_global_ptr_null,
    define_global_i32,
    global_addr,
    memset,
    f64_bits,
    f64_signbit,
    free,
    global_load_ptr,
    is_tagged_int,
    load_f64,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    realloc,
    stack_alloc,
    store_f64,
    store_i8,
    store_i64,
    store_ptr,
    strlen,
    untag_int,
)


define_global_ptr_null("py_format_cpy_object_hook")

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_get_message = extern("py_exc_get_message", (c_ptr,), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_obj_ascii = extern("py_obj_ascii", (c_ptr,), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_user_special_dispatch = extern(
    "py_user_special_dispatch",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64, c_ptr, c_ptr), c_ptr,
)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_bigint_to_double = extern("py_bigint_to_double", (c_ptr,), c_double)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_bigint_to_cstr = extern("py_bigint_to_cstr", (c_ptr,), c_ptr)
py_bigint_to_base_cstr = extern("py_bigint_to_base_cstr", (c_ptr, c_int32, c_int32), c_ptr)
py_chr_from_i64 = extern("py_chr_from_i64", (c_int64,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_int_from_f64_exact = extern("py_int_from_f64_exact", (c_double,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_payload = extern("py_str_payload", (c_ptr,), c_ptr)
py_str_exact_copy = extern("py_str_exact_copy", (c_ptr,), c_ptr)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_str_check = extern("py_str_check", (c_ptr,), c_int64)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytearray_from_obj = extern("py_bytearray_from_obj", (c_ptr,), c_ptr)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_call = extern("py_func_call", (c_ptr, c_ptr), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_note_relocation_read = extern("pcc_gc_note_relocation_read", (c_ptr,), c_ptr)
pcc_stdio_format_float_raw = extern(
    "pcc_stdio_format_float_raw",
    (c_ptr, c_double, c_int64, c_int64, c_int64, c_int64, c_int64),
    c_int64,
)
pcc_stdio_float_raw_capacity = extern(
    "pcc_stdio_float_raw_capacity", (c_int64,), c_int64
)
pcc_stdio_float_exact_digits = extern(
    "pcc_stdio_float_exact_digits", (c_int64, c_ptr, c_ptr), c_int64
)
pcc_stdio_float_round_digits = extern(
    "pcc_stdio_float_round_digits", (c_ptr, c_int64, c_ptr, c_int64), c_int64
)
strtod_c = extern("strtod", (c_ptr, c_ptr), c_double)
pow_c = extern("pow", (c_double, c_double), c_double)
rint_c = extern("rint", (c_double,), c_double)
hypot_c = extern("hypot", (c_double, c_double), c_double)


def _type_of(obj) -> int:
    if is_tagged_int(obj) != 0:
        return PY_TYPE_INT
    if ptr_is_null(obj) != 0:
        return -1
    return load_i32(obj, 8)


def _format_require_result(result, helper_name, message):
    if ptr_is_null(result) != 0:
        py_runtime_error_if_unset(helper_name, message)
    return result


def _buffer_new(capacity: int):
    if capacity < 64:
        capacity = 64
    state = malloc(24)
    if ptr_is_null(state) != 0:
        return null()
    data = malloc(capacity + 1)
    if ptr_is_null(data) != 0:
        free(state)
        return null()
    store_ptr(state, 0, data)
    store_i64(state, 8, 0)
    store_i64(state, 16, capacity)
    store_i8(data, 0, 0)
    return state


def _buffer_free(state) -> None:
    if ptr_is_null(state) != 0:
        return
    data = load_ptr(state, 0)
    if ptr_is_null(data) == 0:
        free(data)
    free(state)


def _buffer_reserve(state, extra: int) -> int:
    if ptr_is_null(state) != 0 or extra < 0:
        return -1
    length: int = load_i64(state, 8)
    capacity: int = load_i64(state, 16)
    need: int = length + extra
    if need <= capacity:
        return 0
    next_capacity: int = capacity
    if next_capacity < 64:
        next_capacity = 64
    while next_capacity < need:
        if next_capacity > 4611686018427387903:
            return -1
        next_capacity = next_capacity * 2
    data = realloc(load_ptr(state, 0), next_capacity + 1)
    if ptr_is_null(data) != 0:
        return -1
    store_ptr(state, 0, data)
    store_i64(state, 16, next_capacity)
    return 0


def _buffer_append(state, source, count: int) -> int:
    if count <= 0:
        return 0
    if ptr_is_null(source) != 0 or _buffer_reserve(state, count) != 0:
        return -1
    data = load_ptr(state, 0)
    length: int = load_i64(state, 8)
    i: int = 0
    while i < count:
        store_i8(data, length + i, load_i8(source, i))
        i = i + 1
    length = length + count
    store_i64(state, 8, length)
    store_i8(data, length, 0)
    return 0


def _buffer_char(state, value: int) -> int:
    if _buffer_reserve(state, 1) != 0:
        return -1
    data = load_ptr(state, 0)
    length: int = load_i64(state, 8)
    store_i8(data, length, value)
    store_i64(state, 8, length + 1)
    store_i8(data, length + 1, 0)
    return 0


def _buffer_repeat(state, value: int, count: int) -> int:
    if count <= 0:
        return 0
    if _buffer_reserve(state, count) != 0:
        return -1
    data = load_ptr(state, 0)
    length: int = load_i64(state, 8)
    i: int = 0
    while i < count:
        store_i8(data, length + i, value)
        i = i + 1
    store_i64(state, 8, length + count)
    store_i8(data, length + count, 0)
    return 0


def _buffer_string(state):
    if ptr_is_null(state) != 0:
        return null()
    return py_str_new(load_ptr(state, 0), load_i64(state, 8))


def _buffer_bytes(state):
    if ptr_is_null(state) != 0:
        return null()
    return py_bytes_new(load_ptr(state, 0), load_i64(state, 8))


def _append_pystr(state, value) -> int:
    if ptr_is_null(value) != 0 or _type_of(value) != PY_TYPE_STR:
        return -1
    return _buffer_append(state, py_str_utf8(value), py_str_byte_len(value))


def _complex_real(value) -> float:
    if ptr_is_null(value) != 0:
        return 0.0
    if is_tagged_int(value) != 0:
        return float(untag_int(value))
    tag: int = load_i32(value, 8)
    if tag == PY_TYPE_COMPLEX or tag == PY_TYPE_FLOAT:
        return load_f64(value, 16)
    if tag == PY_TYPE_INT:
        return py_bigint_to_double(value)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(value, global_load_ptr("py_True")) != 0:
            return 1.0
    return 0.0


def _complex_imag(value) -> float:
    if ptr_is_null(value) != 0 or is_tagged_int(value) != 0:
        return 0.0
    if load_i32(value, 8) == PY_TYPE_COMPLEX:
        return load_f64(value, 24)
    return 0.0


@c_abi_export("py_format_try_cpy_object_into_fd")
def py_format_try_cpy_object_into_fd(fd: int, obj, tag: int) -> int:
    hook = global_load_ptr("py_format_cpy_object_hook")
    if ptr_is_null(hook) != 0 or ptr_is_null(obj) != 0:
        return 0
    if tag >= PY_TYPE_NONE and tag <= 1023:
        return 0
    return call_i64_i64_ptr(hook, fd, obj)


def _float_text_has_underscore(text) -> int:
    index: int = 0
    ch: int = load_i8(text, 0)
    while ch != 0:
        if ch == 95:
            return 1
        index = index + 1
        ch = load_i8(text, index)
    return 0


def _float_is_digit(ch: int) -> int:
    if ch >= 48 and ch <= 57:
        return 1
    return 0


def _float_text_without_underscores(text):
    """A malloc'd copy without PEP 515 underscores, or NULL when one is not
    between two digits (or the copy cannot be allocated)."""
    length: int = 0
    while load_i8(text, length) != 0:
        length = length + 1
    copy = malloc(length + 1)
    if ptr_is_null(copy) != 0:
        py_raise_owned(py_exc_new(7, cstr("float text conversion allocation failed")))
        return null()
    count: int = 0
    index: int = 0
    valid: int = 1
    while index < length and valid != 0:
        ch: int = load_i8(text, index)
        if ch == 95:
            if (index == 0 or _float_is_digit(load_i8(text, index - 1)) == 0
                    or _float_is_digit(load_i8(text, index + 1)) == 0):
                valid = 0
        else:
            store_i8(copy, count, ch)
            count = count + 1
        index = index + 1
    if valid == 0:
        free(copy)
        return null()
    store_i8(copy, count, 0)
    return copy


@c_abi_export("py_float_value_of")
def py_float_value_of(value) -> float:
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        if load_i32(value, 8) == PY_TYPE_STR:
            text = py_str_utf8(value)
            while (
                load_i8(text, 0) == 32
                or load_i8(text, 0) == 9
                or load_i8(text, 0) == 10
                or load_i8(text, 0) == 13
                or load_i8(text, 0) == 11
                or load_i8(text, 0) == 12
            ):
                text = ptr_add(text, 1)
            # PEP 515: one underscore between two digits. Parse a copy
            # without them; any other underscore is a ValueError.
            scratch = null()
            if _float_text_has_underscore(text) != 0:
                scratch = _float_text_without_underscores(text)
                if ptr_is_null(scratch) != 0:
                    if py_err_occurred() == 0:
                        py_raise_owned(py_exc_new(2, cstr("could not convert string to float")))
                    return 0.0
                text = scratch
            end_slot = stack_alloc(8)
            store_ptr(end_slot, 0, null())
            parsed: float = strtod_c(text, end_slot)
            end = load_ptr(end_slot, 0)
            if ptr_eq(end, text) != 0:
                if ptr_is_null(scratch) == 0:
                    free(scratch)
                py_raise_owned(py_exc_new(2, cstr("could not convert string to float")))
                return 0.0
            while (
                load_i8(end, 0) == 32
                or load_i8(end, 0) == 9
                or load_i8(end, 0) == 10
                or load_i8(end, 0) == 13
                or load_i8(end, 0) == 11
                or load_i8(end, 0) == 12
            ):
                end = ptr_add(end, 1)
            trailing: int = load_i8(end, 0)
            if ptr_is_null(scratch) == 0:
                free(scratch)
            if trailing != 0:
                py_raise_owned(py_exc_new(2, cstr("could not convert string to float")))
                return 0.0
            return parsed
    tag: int = _type_of(value)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL or tag == PY_TYPE_FLOAT:
        return py_float_to_f64(value)
    scalar = stack_alloc(8)
    handled = stack_alloc(8)
    store_i64(scalar, 0, 0)
    store_i64(handled, 0, 0)
    py_user_special_dispatch(
        value, cstr("__float__"), null(), null(), 0, 8, scalar, handled,
    )
    if py_err_occurred() != 0:
        return 0.0
    if load_i64(handled, 0) == 0:
        py_raise_owned(py_exc_new(3, cstr("float() argument must be a string or a real number")))
        return 0.0
    return load_f64(scalar, 0)


@c_abi_export("pcc_float_round_fixed_f64")
def pcc_float_round_fixed_f64(value: float, ndigits: int) -> float:
    """CPython's float.__round__ for an integer ``ndigits`` (floatobject.c).

    NaN and the infinities round to themselves. Above NDIGITS_MAX (323) the
    value is returned and below NDIGITS_MIN (-308) a zero of its sign.
    Otherwise double_round: _Py_dg_dtoa mode 3 rounds the exact binary value
    half to even at 10**-ndigits and the digits are read back correctly
    rounded. A finite value rounding past the double range comes back
    infinite; the caller raises "rounded value too large to represent".
    """
    if value != value:
        return value
    if value != 0.0 and value == value * 2.0:
        return value
    if ndigits > 323:
        return value
    if ndigits < -308:
        return 0.0 * value
    negative: int = f64_signbit(value)
    absolute: float = value
    if negative != 0:
        absolute = 0.0 - value
    digits = stack_alloc(800)
    meta = stack_alloc(8)
    count: int = _float_dtoa(absolute, 3, ndigits, digits, meta)
    # double_round's "%s0%se%d": the sign, a leading 0, the digits and the
    # exponent decpt - count.
    text = stack_alloc(840)
    position: int = 0
    if negative != 0:
        store_i8(text, position, 45)
        position = position + 1
    store_i8(text, position, 48)
    position = position + 1
    index: int = 0
    while index < count:
        store_i8(text, position, load_i8(digits, index))
        position = position + 1
        index = index + 1
    store_i8(text, position, 101)
    position = position + 1
    exponent: int = load_i64(meta, 0) - count
    if exponent < 0:
        store_i8(text, position, 45)
        position = position + 1
        exponent = 0 - exponent
    scale: int = 1
    while scale * 10 <= exponent:
        scale = scale * 10
    while scale > 0:
        store_i8(text, position, 48 + (exponent // scale) % 10)
        position = position + 1
        scale = scale // 10
    store_i8(text, position, 0)
    return strtod_c(text, null())


@c_abi_export("py_complex_sub")
def py_complex_sub(left, right):
    return py_complex_new(
        _complex_real(left) - _complex_real(right),
        _complex_imag(left) - _complex_imag(right),
    )


@c_abi_export("py_complex_mul")
def py_complex_mul(left, right):
    ar: float = _complex_real(left)
    ai: float = _complex_imag(left)
    br: float = _complex_real(right)
    bi: float = _complex_imag(right)
    return py_complex_new(ar * br - ai * bi, ar * bi + ai * br)


@c_abi_export("py_complex_div")
def py_complex_div(left, right):
    ar: float = _complex_real(left)
    ai: float = _complex_imag(left)
    br: float = _complex_real(right)
    bi: float = _complex_imag(right)
    denominator: float = br * br + bi * bi
    if denominator == 0.0:
        error = py_exc_new(9, cstr("division by zero"))
        py_raise(error)
        if ptr_is_null(error) == 0:
            py_decref(error)
        return null()
    return py_complex_new(
        (ar * br + ai * bi) / denominator,
        (ai * br - ar * bi) / denominator,
    )


@c_abi_export("py_complex_neg")
def py_complex_neg(value):
    # Negation flips each part's sign bit: -0j is (-0-0j). 0.0 - x would
    # turn a zero part into +0.0.
    real: float = _complex_real(value)
    imag: float = _complex_imag(value)
    return py_complex_new(-real, -imag)


@c_abi_export("py_complex_conjugate")
def py_complex_conjugate(value):
    # (0j).conjugate() is -0j: the imaginary sign bit flips.
    imag: float = _complex_imag(value)
    return py_complex_new(_complex_real(value), -imag)


@c_abi_export("py_complex_abs")
def py_complex_abs(value):
    return py_float_from_f64(hypot_c(_complex_real(value), _complex_imag(value)))


py_complex_new = extern("py_complex_new", (c_double, c_double), c_ptr)


# complex() construction, as CPython's complex_new_impl. Each operand is
# read into an f64 pair of this scratch layout.
_COMPLEX_REAL_RE = 0
_COMPLEX_REAL_IM = 8
_COMPLEX_IMAG_RE = 16
_COMPLEX_IMAG_IM = 24
_COMPLEX_PARTS_BYTES = 32
# _complex_part: a real number, a complex (whose imaginary part takes part in
# the combination), or no number at all.
_COMPLEX_PART_REAL = 1
_COMPLEX_PART_COMPLEX = 2


def _complex_space(ch: int) -> int:
    if ch == 32 or (ch >= 9 and ch <= 13):
        return 1
    return 0


def _complex_skip_space(text):
    while _complex_space(load_i8(text, 0)) != 0:
        text = ptr_add(text, 1)
    return text


def _complex_text_float(text, end_slot) -> float:
    """One float token at ``text``, its end stored in ``end_slot`` (``text``
    itself when there is none). CPython's float parser takes neither a
    hexadecimal float nor a ``nan(...)`` payload, which strtod accepts."""
    store_ptr(end_slot, 0, text)
    at: int = 0
    first: int = load_i8(text, 0)
    if first == 43 or first == 45:
        at = 1
    lead: int = load_i8(text, at)
    if lead == 48 and (load_i8(text, at + 1) == 120 or load_i8(text, at + 1) == 88):
        return 0.0
    value: float = strtod_c(text, end_slot)
    if (lead == 110 or lead == 78) and ptr_eq(load_ptr(end_slot, 0), text) == 0:
        store_ptr(end_slot, 0, ptr_add(text, at + 3))
    return value


def _complex_parse_text(text, parts) -> int:
    """complex_from_string_inner: 1 with the parts stored, 0 when malformed.

    Forms: <float>, <float>j, <float><signed-float>j, <float><sign>j,
    <sign>j and j, optionally parenthesised and surrounded by whitespace.
    """
    end_slot = stack_alloc(C_POINTER_SIZE)
    at = _complex_skip_space(text)
    bracket: int = 0
    if load_i8(at, 0) == 40:
        bracket = 1
        at = _complex_skip_space(ptr_add(at, 1))
    real: float = 0.0
    imag: float = 0.0
    number: float = _complex_text_float(at, end_slot)
    end = load_ptr(end_slot, 0)
    ch: int = 0
    if ptr_eq(end, at) == 0:
        at = end
        ch = load_i8(at, 0)
        if ch == 43 or ch == 45:
            real = number
            number = _complex_text_float(at, end_slot)
            end = load_ptr(end_slot, 0)
            if ptr_eq(end, at) == 0:
                imag = number
                at = end
            else:
                imag = 1.0 if ch == 43 else -1.0
                at = ptr_add(at, 1)
            ch = load_i8(at, 0)
            if ch != 106 and ch != 74:
                return 0
            at = ptr_add(at, 1)
        elif ch == 106 or ch == 74:
            imag = number
            at = ptr_add(at, 1)
        else:
            real = number
    else:
        ch = load_i8(at, 0)
        imag = 1.0
        if ch == 43 or ch == 45:
            if ch == 45:
                imag = -1.0
            at = ptr_add(at, 1)
        ch = load_i8(at, 0)
        if ch != 106 and ch != 74:
            return 0
        at = ptr_add(at, 1)
    at = _complex_skip_space(at)
    if bracket != 0:
        if load_i8(at, 0) != 41:
            return 0
        at = _complex_skip_space(ptr_add(at, 1))
    if load_i8(at, 0) != 0:
        return 0
    store_f64(parts, _COMPLEX_REAL_RE, real)
    store_f64(parts, _COMPLEX_IMAG_RE, imag)
    return 1


def _complex_append_type_name(message, at: int, value) -> int:
    name = py_obj_type_name(value)
    if ptr_is_null(name) == 0:
        text = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 100:
            count = 100
        index: int = 0
        while index < count:
            store_i8(message, at, load_i8(text, index))
            at = at + 1
            index = index + 1
        py_decref(name)
    return at


def _complex_type_error(prefix, value) -> None:
    """TypeError: ``prefix`` followed by ``value``'s type name."""
    message = stack_alloc(200)
    at: int = 0
    while load_i8(prefix, at) != 0:
        store_i8(message, at, load_i8(prefix, at))
        at = at + 1
    at = _complex_append_type_name(message, at, value)
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))


def _complex_text_error(value, malformed: int) -> None:
    """complex(str)'s ValueError: "complex() arg is a malformed string", or
    for a misplaced PEP 515 underscore "could not convert string to complex:
    <repr>"."""
    if malformed != 0:
        py_raise_owned(py_exc_new(2, cstr("complex() arg is a malformed string")))
        return
    shown = py_obj_repr(value)
    if ptr_is_null(shown) != 0:
        return
    prefix = cstr("could not convert string to complex: ")
    prefix_length: int = strlen(prefix)
    length: int = py_str_byte_len(shown)
    message = malloc(prefix_length + length + 1)
    if ptr_is_null(message) != 0:
        py_decref(shown)
        py_raise_owned(py_exc_new(19, cstr("cannot allocate complex() error message")))
        return
    index: int = 0
    while index < prefix_length:
        store_i8(message, index, load_i8(prefix, index))
        index = index + 1
    text = py_str_utf8(shown)
    index = 0
    while index < length:
        store_i8(message, prefix_length + index, load_i8(text, index))
        index = index + 1
    store_i8(message, prefix_length + length, 0)
    py_decref(shown)
    py_raise_owned(py_exc_new(2, message))
    free(message)


def _complex_from_text(value):
    """complex(str): a NEW complex, or NULL with ValueError raised."""
    text = py_str_utf8(value)
    if strlen(text) != py_str_byte_len(value):
        _complex_text_error(value, 1)
        return null()
    scratch = null()
    if _float_text_has_underscore(text) != 0:
        scratch = _float_text_without_underscores(text)
        if ptr_is_null(scratch) != 0:
            if py_err_occurred() == 0:
                _complex_text_error(value, 0)
            return null()
        text = scratch
    parts = stack_alloc(_COMPLEX_PARTS_BYTES)
    parsed: int = _complex_parse_text(text, parts)
    if ptr_is_null(scratch) == 0:
        free(scratch)
    if parsed == 0:
        _complex_text_error(value, 1)
        return null()
    return py_complex_new(load_f64(parts, _COMPLEX_REAL_RE), load_f64(parts, _COMPLEX_IMAG_RE))


def _complex_special(value):
    """``value.__complex__()``: a NEW complex, or NULL -- with the exception
    set on failure, without one when the type defines no __complex__."""
    scalar = stack_alloc(8)
    handled = stack_alloc(8)
    store_i64(scalar, 0, 0)
    store_i64(handled, 0, 0)
    result = py_user_special_dispatch(
        value, cstr("__complex__"), null(), null(), 0, 0, scalar, handled,
    )
    if py_err_occurred() != 0:
        if ptr_is_null(result) == 0:
            py_decref(result)
        return null()
    if load_i64(handled, 0) == 0:
        return null()
    if ptr_is_null(result) != 0:
        py_runtime_error_if_unset(
            cstr("__complex__"), cstr("__complex__ returned NULL without an exception"),
        )
        return null()
    if is_tagged_int(result) == 0 and load_i32(result, 8) == PY_TYPE_COMPLEX:
        return result
    # CPython: "Bad.__complex__() must return a complex, not int". The
    # result's type name is copied out before the result is released.
    message = stack_alloc(260)
    at: int = 0
    result_name = py_obj_type_name(result)
    py_decref(result)
    at = _complex_append_type_name(message, at, value)
    suffix = cstr(".__complex__() must return a complex, not ")
    index: int = 0
    while load_i8(suffix, index) != 0:
        store_i8(message, at, load_i8(suffix, index))
        at = at + 1
        index = index + 1
    if ptr_is_null(result_name) == 0:
        text = py_str_utf8(result_name)
        count: int = py_str_byte_len(result_name)
        if count > 100:
            count = 100
        index = 0
        while index < count:
            store_i8(message, at, load_i8(text, index))
            at = at + 1
            index = index + 1
        py_decref(result_name)
    store_i8(message, at, 0)
    py_raise_owned(py_exc_new(3, message))
    return null()


def _complex_part(value, parts, offset: int, special: int) -> int:
    """Read one complex() operand into the f64 pair at ``offset``.

    _COMPLEX_PART_REAL or _COMPLEX_PART_COMPLEX, 0 when ``value`` is no
    number (nothing raised), -1 with the exception set. ``special`` admits
    __complex__, which CPython consults for the first operand only; a str
    is never a number here.
    """
    store_f64(parts, offset, 0.0)
    store_f64(parts, offset + 8, 0.0)
    tag: int = _type_of(value)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        number: float = _int_to_double(value)
        if py_err_occurred() != 0:
            return -1
        store_f64(parts, offset, number)
        return _COMPLEX_PART_REAL
    if tag == PY_TYPE_FLOAT:
        store_f64(parts, offset, load_f64(value, 16))
        return _COMPLEX_PART_REAL
    if tag == PY_TYPE_COMPLEX:
        store_f64(parts, offset, load_f64(value, 16))
        store_f64(parts, offset + 8, load_f64(value, 24))
        return _COMPLEX_PART_COMPLEX
    if tag == PY_TYPE_STR:
        return 0
    if special != 0:
        converted = _complex_special(value)
        if ptr_is_null(converted) == 0:
            store_f64(parts, offset, load_f64(converted, 16))
            store_f64(parts, offset + 8, load_f64(converted, 24))
            py_decref(converted)
            return _COMPLEX_PART_COMPLEX
        if py_err_occurred() != 0:
            return -1
    scalar = stack_alloc(8)
    handled = stack_alloc(8)
    store_i64(scalar, 0, 0)
    store_i64(handled, 0, 0)
    # Conversion 8: __float__, falling back to __index__, into an f64.
    py_user_special_dispatch(value, cstr("__float__"), null(), null(), 0, 8, scalar, handled)
    if py_err_occurred() != 0:
        return -1
    if load_i64(handled, 0) == 0:
        return 0
    store_f64(parts, offset, load_f64(scalar, 0))
    return _COMPLEX_PART_REAL


@c_abi_export("py_complex_from_object")
def py_complex_from_object(value):
    """complex(x): a NEW complex, or NULL with the exception set. A NULL
    ``value`` is the omitted argument."""
    if ptr_is_null(value) != 0:
        return py_complex_new(0.0, 0.0)
    tag: int = _type_of(value)
    if tag == PY_TYPE_COMPLEX:
        py_incref(value)
        return value
    if tag == PY_TYPE_STR:
        return _complex_from_text(value)
    parts = stack_alloc(_COMPLEX_PARTS_BYTES)
    status: int = _complex_part(value, parts, _COMPLEX_REAL_RE, 1)
    if status < 0:
        return null()
    if status == 0:
        _complex_type_error(cstr("complex() argument must be a string or a number, not "), value)
        return null()
    return py_complex_new(load_f64(parts, _COMPLEX_REAL_RE), load_f64(parts, _COMPLEX_REAL_IM))


@c_abi_export("py_complex_from_parts")
def py_complex_from_parts(real, imag):
    """complex(real, imag) = real + imag*1j, where either part may itself be
    complex. A NULL ``real`` is the omitted 0; a NULL ``imag`` is the
    one-argument form."""
    if ptr_is_null(imag) != 0:
        return py_complex_from_object(real)
    parts = stack_alloc(_COMPLEX_PARTS_BYTES)
    store_f64(parts, _COMPLEX_REAL_RE, 0.0)
    store_f64(parts, _COMPLEX_REAL_IM, 0.0)
    real_kind: int = _COMPLEX_PART_REAL
    if ptr_is_null(real) == 0:
        real_kind = _complex_part(real, parts, _COMPLEX_REAL_RE, 1)
    if real_kind < 0:
        return null()
    if real_kind == 0:
        _complex_type_error(cstr("complex() argument 'real' must be a real number, not "), real)
        return null()
    imag_kind: int = _complex_part(imag, parts, _COMPLEX_IMAG_RE, 0)
    if imag_kind < 0:
        return null()
    if imag_kind == 0:
        _complex_type_error(cstr("complex() argument 'imag' must be a real number, not "), imag)
        return null()
    # Only a complex part contributes its imaginary component, so a real
    # -0.0 imaginary operand keeps its sign.
    result_real: float = load_f64(parts, _COMPLEX_REAL_RE)
    result_imag: float = load_f64(parts, _COMPLEX_IMAG_RE)
    if imag_kind == _COMPLEX_PART_COMPLEX:
        result_real = result_real - load_f64(parts, _COMPLEX_IMAG_IM)
    if real_kind == _COMPLEX_PART_COMPLEX:
        result_imag = result_imag + load_f64(parts, _COMPLEX_REAL_IM)
    return py_complex_new(result_real, result_imag)


def _find_byte(text, value: int) -> int:
    i: int = 0
    while load_i8(text, i) != 0:
        if (load_i8(text, i) & 255) == value:
            return i
        i = i + 1
    return -1


def _parse_decimal_exponent(text, start: int) -> int:
    sign: int = 1
    if load_i8(text, start) == 45:
        sign = -1
        start = start + 1
    elif load_i8(text, start) == 43:
        start = start + 1
    value: int = 0
    while load_i8(text, start) >= 48 and load_i8(text, start) <= 57:
        value = value * 10 + load_i8(text, start) - 48
        start = start + 1
    return value * sign


def _pow10_i64(exponent: int) -> int:
    """A bounded power of ten used only for at most 17 repr digits."""
    result: int = 1
    while exponent > 0:
        result = result * 10
        exponent = exponent - 1
    return result


def _write_exact_digits(output, position: int, digits: int, count: int) -> int:
    """Write exactly ``count`` decimal digits, including leading zeroes."""
    end: int = position + count
    cursor: int = end
    while cursor > position:
        cursor = cursor - 1
        store_i8(output, cursor, 48 + (digits % 10))
        digits = digits // 10
    return end


def _write_exact_exponent(output, position: int, exponent: int) -> int:
    store_i8(output, position, 101)
    position = position + 1
    if exponent < 0:
        store_i8(output, position, 45)
        exponent = 0 - exponent
    else:
        store_i8(output, position, 43)
    position = position + 1
    if exponent >= 100:
        return _write_exact_digits(output, position, exponent, 3)
    return _write_exact_digits(output, position, exponent, 2)


def _write_exact_scientific(
    output,
    negative: int,
    digits: int,
    significant: int,
    exponent: int,
) -> int:
    position: int = 0
    if negative != 0:
        store_i8(output, position, 45)
        position = position + 1
    divisor: int = _pow10_i64(significant - 1)
    store_i8(output, position, 48 + (digits // divisor))
    position = position + 1
    if significant > 1:
        store_i8(output, position, 46)
        position = position + 1
        position = _write_exact_digits(
            output,
            position,
            digits % divisor,
            significant - 1,
        )
    position = _write_exact_exponent(output, position, exponent)
    store_i8(output, position, 0)
    return position


def _write_exact_float_text(
    output,
    negative: int,
    digits: int,
    significant: int,
    exponent: int,
) -> int:
    if exponent < -4 or exponent >= 16:
        return _write_exact_scientific(
            output,
            negative,
            digits,
            significant,
            exponent,
        )

    position: int = 0
    if negative != 0:
        store_i8(output, position, 45)
        position = position + 1
    integer_count: int = exponent + 1
    if integer_count <= 0:
        store_i8(output, position, 48)
        store_i8(output, position + 1, 46)
        position = position + 2
        zeroes: int = 0 - integer_count
        while zeroes > 0:
            store_i8(output, position, 48)
            position = position + 1
            zeroes = zeroes - 1
        position = _write_exact_digits(
            output,
            position,
            digits,
            significant,
        )
    elif significant <= integer_count:
        position = _write_exact_digits(
            output,
            position,
            digits,
            significant,
        )
        zeroes = integer_count - significant
        while zeroes > 0:
            store_i8(output, position, 48)
            position = position + 1
            zeroes = zeroes - 1
        store_i8(output, position, 46)
        store_i8(output, position + 1, 48)
        position = position + 2
    else:
        fractional_count: int = significant - integer_count
        divisor = _pow10_i64(fractional_count)
        position = _write_exact_digits(
            output,
            position,
            digits // divisor,
            integer_count,
        )
        store_i8(output, position, 46)
        position = position + 1
        position = _write_exact_digits(
            output,
            position,
            digits % divisor,
            fractional_count,
        )
    store_i8(output, position, 0)
    return position


def _float_shortest_digits(absolute: float, meta) -> int:
    """Shortest round-tripping digits of a finite, positive double.

    Returns them as an integer of ``meta[0]`` significant digits and stores
    the decimal exponent of the first digit at ``meta[8]``.  Each candidate is
    the exact expansion rounded half to even to one more digit; strtod is only
    the acceptance oracle.  (This used to rebuild the value as a bignum
    fraction whose denominator reached 1 << 1074.)
    """
    exact = stack_alloc(800)
    work = stack_alloc(800)
    point_box = stack_alloc(8)
    total: int = pcc_stdio_float_exact_digits(f64_bits(absolute), exact, point_box)
    point: int = load_i64(point_box, 0)
    candidate = stack_alloc(96)
    significant: int = 1
    digits: int = 0
    exponent: int = point - 1
    found: int = 0
    while significant <= 17 and found == 0:
        index: int = 0
        while index < total:
            store_i8(work, index, load_i8(exact, index))
            index = index + 1
        store_i64(point_box, 0, point)
        count: int = pcc_stdio_float_round_digits(work, total, point_box, significant)
        digits = 0
        index = 0
        while index < significant:
            digit: int = 0
            if index < count:
                digit = load_i8(work, index)
            digits = digits * 10 + digit
            index = index + 1
        exponent = load_i64(point_box, 0) - 1
        _write_exact_scientific(candidate, 0, digits, significant, exponent)
        if strtod_c(candidate, null()) == absolute:
            found = 1
        else:
            significant = significant + 1
    if significant > 17:
        significant = 17
    store_i64(meta, 0, significant)
    store_i64(meta, 8, exponent)
    return digits


@c_abi_export("py_float_repr_shortest")
def py_float_repr_shortest(value_obj):
    value: float = py_float_to_f64(value_obj)
    if value != value:
        return py_str_new(cstr("nan"), 3)
    if value != 0.0 and value == value * 2.0:
        if value < 0.0:
            return py_str_new(cstr("-inf"), 4)
        return py_str_new(cstr("inf"), 3)
    if value == 0.0:
        if f64_signbit(value) != 0:
            return py_str_new(cstr("-0.0"), 4)
        return py_str_new(cstr("0.0"), 3)

    negative: int = 0
    absolute: float = value
    if value < 0.0:
        negative = 1
        absolute = 0.0 - value
    meta = stack_alloc(16)
    digits: int = _float_shortest_digits(absolute, meta)
    output = stack_alloc(800)
    length: int = _write_exact_float_text(
        output,
        negative,
        digits,
        load_i64(meta, 0),
        load_i64(meta, 8),
    )
    return py_str_new(output, length)


def _append_complex_component(state, value: float) -> int:
    boxed = py_float_from_f64(value)
    if ptr_is_null(boxed) != 0:
        return _buffer_char(state, 48)
    rendered = py_float_repr_shortest(boxed)
    py_decref(boxed)
    if ptr_is_null(rendered) != 0:
        return _buffer_char(state, 48)
    text = py_str_utf8(rendered)
    length: int = py_str_byte_len(rendered)
    if length >= 2:
        if load_i8(text, length - 2) == 46 and load_i8(text, length - 1) == 48:
            length = length - 2
    rc: int = _buffer_append(state, text, length)
    py_decref(rendered)
    return rc


@c_abi_export("py_complex_repr")
def py_complex_repr(value):
    if ptr_is_null(value) != 0 or _type_of(value) != PY_TYPE_COMPLEX:
        return null()
    real: float = load_f64(value, 16)
    imag: float = load_f64(value, 24)
    state = _buffer_new(64)
    if ptr_is_null(state) != 0:
        return null()
    if real == 0.0 and f64_signbit(real) == 0:
        _append_complex_component(state, imag)
        _buffer_char(state, 106)
    else:
        _buffer_char(state, 40)
        _append_complex_component(state, real)
        if imag < 0.0 or (imag == 0.0 and f64_signbit(imag) != 0):
            _buffer_char(state, 45)
            imag = 0.0 - imag
        else:
            _buffer_char(state, 43)
        _append_complex_component(state, imag)
        _buffer_char(state, 106)
        _buffer_char(state, 41)
    result = _buffer_string(state)
    _buffer_free(state)
    return result


@c_abi_export("py_exc_repr")
def py_exc_repr(value):
    value = pcc_gc_note_relocation_read(value)
    if ptr_is_null(value) != 0 or _type_of(value) != PY_TYPE_EXC:
        return null()
    if (load_i32(value, 12) & PY_FLAG_EXC_UNICODE_PAYLOAD) != 0:
        return py_unicode_error_format(value, 1)
    if (load_i32(value, 12) & PY_FLAG_EXC_OS_PAYLOAD) != 0:
        return py_os_error_format(value, 1)
    cls = pcc_gc_load_ptr(value, ptr_add(value, 16))
    name = cstr("Exception")
    if ptr_is_null(cls) == 0:
        candidate = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(candidate) == 0:
            name = candidate
    state = _buffer_new(64)
    if ptr_is_null(state) != 0:
        return null()
    _buffer_append(state, name, strlen(name))
    _buffer_char(state, 40)
    # Buffer helpers may park; get the borrowed argument only after them and
    # consume it once in py_obj_repr, without keeping it across conversion.
    message = py_exc_get_message(pcc_gc_note_relocation_read(value))
    if ptr_is_null(message) == 0:
        rendered = py_obj_repr(message)
        if ptr_is_null(rendered) != 0:
            _buffer_free(state)
            return null()
        _append_pystr(state, rendered)
        py_decref(rendered)
    _buffer_char(state, 41)
    result = _buffer_string(state)
    _buffer_free(state)
    return result


def _parse_digits(text, position: int) -> int:
    value: int = 0
    while load_i8(text, position) >= 48 and load_i8(text, position) <= 57:
        value = value * 10 + load_i8(text, position) - 48
        position = position + 1
    return value


def _skip_digits(text, position: int) -> int:
    while load_i8(text, position) >= 48 and load_i8(text, position) <= 57:
        position = position + 1
    return position


# --- format() mini-language (CPython Python/formatter_unicode.c) -----------
#
# A parsed spec is CPython's InternalFormatSpec in i64 slots.  The fill
# character is a byte range of the spec text (-1: a space).
_SPEC_FILL = 0
_SPEC_FILL_LEN = 8
_SPEC_ALIGN = 16
_SPEC_SIGN = 24
_SPEC_NO_NEG_0 = 32
_SPEC_ALT = 40
_SPEC_WIDTH = 48
_SPEC_GROUPING = 56
_SPEC_PRECISION = 64
_SPEC_FRAC_GROUPING = 72
_SPEC_TYPE = 80
_SPEC_BYTES = 88

# The pieces of a formatted number (CPython's NumberFieldWidths inputs):
# [sign][prefix][digits][.][fraction][tail], e.g. "-0x" "ff", or "1" "." "5"
# "e+10".  Pointer slots hold byte ranges; lengths are bytes (all ASCII except
# a %c tail, whose character count is kept separately).
_NUM_NEGATIVE = 0
_NUM_PREFIX = 8
_NUM_PREFIX_LEN = 16
_NUM_DIGITS = 24
_NUM_DIGITS_LEN = 32
_NUM_UPPER = 40
_NUM_DECIMAL = 48
_NUM_FRAC = 56
_NUM_FRAC_LEN = 64
_NUM_TAIL = 72
_NUM_TAIL_LEN = 80
_NUM_TAIL_CHARS = 88
_NUM_GROUP = 96
_NUM_SEPARATOR = 104
_NUM_FRAC_SEPARATOR = 112
_NUM_BYTES = 120


def _utf8_width(lead: int) -> int:
    byte: int = lead & 255
    if byte >= 240:
        return 4
    if byte >= 224:
        return 3
    if byte >= 192:
        return 2
    return 1


def _utf8_decode(text, position: int, width: int) -> int:
    first: int = load_i8(text, position) & 255
    if width == 1:
        return first
    second: int = load_i8(text, position + 1) & 63
    if width == 2:
        return ((first & 31) << 6) | second
    third: int = load_i8(text, position + 2) & 63
    if width == 3:
        return ((first & 15) << 12) | (second << 6) | third
    fourth: int = load_i8(text, position + 3) & 63
    return ((first & 7) << 18) | (second << 12) | (third << 6) | fourth


def _utf8_char_count(text, length: int) -> int:
    count: int = 0
    index: int = 0
    while index < length:
        if (load_i8(text, index) & 192) != 128:
            count = count + 1
        index = index + 1
    return count


def _utf8_prefix_bytes(text, length: int, chars: int) -> int:
    # Byte length of the first ``chars`` characters of ``text``.
    seen: int = 0
    index: int = 0
    while index < length:
        if (load_i8(text, index) & 192) != 128:
            if seen == chars:
                return index
            seen = seen + 1
        index = index + 1
    return length


def _buffer_cstr(state, text) -> int:
    return _buffer_append(state, text, strlen(text))


def _buffer_fill(state, fill, fill_len: int, count: int) -> int:
    if count <= 0:
        return 0
    if fill_len == 1:
        return _buffer_repeat(state, load_i8(fill, 0), count)
    index: int = 0
    while index < count:
        if _buffer_append(state, fill, fill_len) != 0:
            return -1
        index = index + 1
    return 0


def _buffer_code_label(state, code: int) -> None:
    # CPython names a presentation type as 'c', or '\xNN' outside ASCII.
    _buffer_char(state, 39)
    if code > 32 and code < 128:
        _buffer_char(state, code)
    else:
        _buffer_char(state, 92)
        _buffer_char(state, 120)
        hex_digits = stack_alloc(8)
        count: int = 0
        value: int = code
        while value != 0 or count == 0:
            digit: int = value & 15
            if digit < 10:
                store_i8(hex_digits, count, 48 + digit)
            else:
                store_i8(hex_digits, count, 87 + digit)
            value = value >> 4
            count = count + 1
        while count > 0:
            count = count - 1
            _buffer_char(state, load_i8(hex_digits, count))
    _buffer_char(state, 39)


def _raise_buffer_error(state, kind: int):
    # Raise ``kind`` with the buffer's (NUL-terminated) text; frees it.
    if ptr_is_null(state) == 0:
        py_raise_owned(py_exc_new(kind, load_ptr(state, 0)))
        _buffer_free(state)
    return null()


def _raise_format_error(message):
    py_raise_owned(py_exc_new(2, message))
    return null()


def _raise_unknown_code(code: int, type_name):
    # "Unknown format code 'q' for object of type 'int'"
    state = _buffer_new(96)
    _buffer_cstr(state, cstr("Unknown format code "))
    _buffer_code_label(state, code)
    _buffer_cstr(state, cstr(" for object of type '"))
    _buffer_cstr(state, type_name)
    _buffer_char(state, 39)
    return _raise_buffer_error(state, 2)


def _is_align_byte(byte: int) -> int:
    if byte == 60 or byte == 62 or byte == 61 or byte == 94:
        return 1
    return 0


def _is_digit_byte(byte: int) -> int:
    if byte >= 48 and byte <= 57:
        return 1
    return 0


def _spec_fail(message) -> int:
    py_raise_owned(py_exc_new(2, message))
    return -1


def _parse_format_spec(spec, length: int, parsed, default_type: int, default_align: int, type_name) -> int:
    # CPython parse_internal_render_format_spec: 0, or -1 with ValueError set.
    store_i64(parsed, _SPEC_FILL, -1)
    store_i64(parsed, _SPEC_FILL_LEN, 1)
    store_i64(parsed, _SPEC_ALIGN, default_align)
    store_i64(parsed, _SPEC_SIGN, 0)
    store_i64(parsed, _SPEC_NO_NEG_0, 0)
    store_i64(parsed, _SPEC_ALT, 0)
    store_i64(parsed, _SPEC_WIDTH, -1)
    store_i64(parsed, _SPEC_GROUPING, 0)
    store_i64(parsed, _SPEC_PRECISION, -1)
    store_i64(parsed, _SPEC_FRAC_GROUPING, 0)
    store_i64(parsed, _SPEC_TYPE, default_type)
    position: int = 0
    fill_given: int = 0
    align_given: int = 0
    if length > 0:
        first_width: int = _utf8_width(load_i8(spec, 0))
        if first_width < length and _is_align_byte(load_i8(spec, first_width)) != 0:
            store_i64(parsed, _SPEC_FILL, 0)
            store_i64(parsed, _SPEC_FILL_LEN, first_width)
            store_i64(parsed, _SPEC_ALIGN, load_i8(spec, first_width))
            fill_given = 1
            align_given = 1
            position = first_width + 1
        elif _is_align_byte(load_i8(spec, 0)) != 0:
            store_i64(parsed, _SPEC_ALIGN, load_i8(spec, 0))
            align_given = 1
            position = 1
    if position < length:
        sign: int = load_i8(spec, position)
        if sign == 43 or sign == 45 or sign == 32:
            store_i64(parsed, _SPEC_SIGN, sign)
            position = position + 1
    if position < length and load_i8(spec, position) == 122:
        store_i64(parsed, _SPEC_NO_NEG_0, 1)
        position = position + 1
    if position < length and load_i8(spec, position) == 35:
        store_i64(parsed, _SPEC_ALT, 1)
        position = position + 1
    if fill_given == 0 and position < length and load_i8(spec, position) == 48:
        # Sign-aware zero padding: '0' becomes the fill, and '=' the
        # alignment unless one was given (numbers default to '>').
        store_i64(parsed, _SPEC_FILL, position)
        store_i64(parsed, _SPEC_FILL_LEN, 1)
        if align_given == 0 and default_align == 62:
            store_i64(parsed, _SPEC_ALIGN, 61)
        position = position + 1
    width_start: int = position
    width: int = 0
    while position < length and _is_digit_byte(load_i8(spec, position)) != 0:
        if width > 100000000000:
            return _spec_fail(cstr("Too many decimal digits in format string"))
        width = width * 10 + load_i8(spec, position) - 48
        position = position + 1
    if position > width_start:
        store_i64(parsed, _SPEC_WIDTH, width)
    grouping: int = 0
    if position < length and load_i8(spec, position) == 44:
        grouping = 44
        position = position + 1
    if position < length and load_i8(spec, position) == 95:
        if grouping != 0:
            return _spec_fail(cstr("Cannot specify both ',' and '_'."))
        grouping = 95
        position = position + 1
    if position < length and load_i8(spec, position) == 44 and grouping == 95:
        return _spec_fail(cstr("Cannot specify both ',' and '_'."))
    store_i64(parsed, _SPEC_GROUPING, grouping)
    if position < length and load_i8(spec, position) == 46:
        position = position + 1
        precision_start: int = position
        precision: int = 0
        while position < length and _is_digit_byte(load_i8(spec, position)) != 0:
            if precision > 100000000000:
                return _spec_fail(cstr("Too many decimal digits in format string"))
            precision = precision * 10 + load_i8(spec, position) - 48
            position = position + 1
        if position > precision_start:
            store_i64(parsed, _SPEC_PRECISION, precision)
        frac: int = 0
        if position < length and load_i8(spec, position) == 44:
            frac = 44
            position = position + 1
        if position < length and load_i8(spec, position) == 95:
            if frac != 0:
                return _spec_fail(cstr("Cannot specify both ',' and '_'."))
            frac = 95
            position = position + 1
        if position < length and load_i8(spec, position) == 44 and frac == 95:
            return _spec_fail(cstr("Cannot specify both ',' and '_'."))
        if position == precision_start:
            return _spec_fail(cstr("Format specifier missing precision"))
        store_i64(parsed, _SPEC_FRAC_GROUPING, frac)
    if position < length:
        code_width: int = _utf8_width(load_i8(spec, position))
        if position + code_width < length:
            state = _buffer_new(length + 64)
            _buffer_cstr(state, cstr("Invalid format specifier '"))
            _buffer_append(state, spec, length)
            _buffer_cstr(state, cstr("' for object of type '"))
            _buffer_cstr(state, type_name)
            _buffer_char(state, 39)
            _raise_buffer_error(state, 2)
            return -1
        store_i64(parsed, _SPEC_TYPE, _utf8_decode(spec, position, code_width))
    if grouping != 0:
        code: int = load_i64(parsed, _SPEC_TYPE)
        allowed: int = 0
        if (
            code == 0
            or code == 100
            or code == 101
            or code == 102
            or code == 103
            or code == 69
            or code == 70
            or code == 71
            or code == 37
        ):
            allowed = 1
        elif grouping == 95 and (code == 98 or code == 111 or code == 120 or code == 88):
            # Underscores group bin/oct/hex digits by four (PEP 515).
            allowed = 1
        if allowed == 0:
            state = _buffer_new(64)
            _buffer_cstr(state, cstr("Cannot specify '"))
            _buffer_char(state, grouping)
            _buffer_cstr(state, cstr("' with "))
            _buffer_code_label(state, code)
            _buffer_char(state, 46)
            _raise_buffer_error(state, 2)
            return -1
    return 0


def _group_digits(digits, count: int, min_width: int, group: int, separator: int, upper: int, meta):
    # CPython _PyUnicode_InsertThousandsGrouping, written right to left.  With
    # sign-aware zero padding ``min_width`` asks for leading zeros, which are
    # grouped too ("0,001,234").  Returns a malloc'd buffer; the text is at
    # meta[0] for meta[8] bytes.
    if min_width < 0:
        min_width = 0
    capacity: int = 2 * (count + min_width) + 8
    out = malloc(capacity)
    if ptr_is_null(out) != 0:
        return out
    write: int = capacity
    remaining: int = count
    source: int = count
    use_separator: int = 0
    finished: int = 0
    while finished == 0:
        size: int = remaining
        if min_width > size:
            size = min_width
        if size < 1:
            size = 1
        if group > 0 and size > group:
            size = group
        zeros: int = size - remaining
        if zeros < 0:
            zeros = 0
        chars: int = remaining
        if chars > size:
            chars = size
        if use_separator != 0:
            write = write - 1
            store_i8(out, write, separator)
        index: int = 0
        while index < chars:
            write = write - 1
            source = source - 1
            byte: int = load_i8(digits, source)
            if upper != 0 and byte >= 97 and byte <= 102:
                byte = byte - 32
            store_i8(out, write, byte)
            index = index + 1
        index = 0
        while index < zeros:
            write = write - 1
            store_i8(out, write, 48)
            index = index + 1
        use_separator = 1
        remaining = remaining - chars
        min_width = min_width - size
        if group <= 0:
            finished = 1
        elif remaining <= 0 and min_width <= 0:
            finished = 1
        else:
            min_width = min_width - 1
    store_i64(meta, 0, write)
    store_i64(meta, 8, capacity - write)
    return out


def _layout_number(parsed, spec, parts):
    # CPython calc_number_widths + fill_number:
    # [lpad][sign][prefix][spad][grouped digits][.][fraction][tail][rpad]
    sign_option: int = load_i64(parsed, _SPEC_SIGN)
    sign_char: int = 0
    if load_i64(parts, _NUM_NEGATIVE) != 0:
        sign_char = 45
    elif sign_option == 43:
        sign_char = 43
    elif sign_option == 32:
        sign_char = 32
    sign_count: int = 0
    if sign_char != 0:
        sign_count = 1
    prefix_len: int = load_i64(parts, _NUM_PREFIX_LEN)
    decimal: int = load_i64(parts, _NUM_DECIMAL)
    frac_len: int = load_i64(parts, _NUM_FRAC_LEN)
    frac_separator: int = load_i64(parts, _NUM_FRAC_SEPARATOR)
    frac_chars: int = frac_len
    if frac_separator != 0 and frac_len > 0:
        frac_chars = frac_len + (frac_len - 1) // 3
    tail_chars: int = load_i64(parts, _NUM_TAIL_CHARS)
    other: int = sign_count + prefix_len + decimal + frac_chars + tail_chars
    width: int = load_i64(parsed, _SPEC_WIDTH)
    align: int = load_i64(parsed, _SPEC_ALIGN)
    fill_offset: int = load_i64(parsed, _SPEC_FILL)
    fill_len: int = load_i64(parsed, _SPEC_FILL_LEN)
    fill = cstr(" ")
    if fill_offset >= 0:
        fill = ptr_add(spec, fill_offset)
    min_width: int = 0
    if fill_len == 1 and load_i8(fill, 0) == 48 and align == 61:
        min_width = width - other
    digit_len: int = load_i64(parts, _NUM_DIGITS_LEN)
    grouped_meta = stack_alloc(16)
    store_i64(grouped_meta, 0, 0)
    store_i64(grouped_meta, 8, 0)
    grouped = null()
    if digit_len > 0:
        # Only %c and inf/nan have no digits; CPython pads those with the
        # fill instead of inserting zeros.
        grouped = _group_digits(
            load_ptr(parts, _NUM_DIGITS),
            digit_len,
            min_width,
            load_i64(parts, _NUM_GROUP),
            load_i64(parts, _NUM_SEPARATOR),
            load_i64(parts, _NUM_UPPER),
            grouped_meta,
        )
        if ptr_is_null(grouped) != 0:
            py_raise_owned(py_exc_new(19, cstr("out of memory")))
            return null()
    grouped_len: int = load_i64(grouped_meta, 8)
    padding: int = width - (other + grouped_len)
    left: int = 0
    middle: int = 0
    right: int = 0
    if padding > 0:
        if align == 60:
            right = padding
        elif align == 94:
            left = padding // 2
            right = padding - left
        elif align == 61:
            middle = padding
        else:
            left = padding
    state = _buffer_new(other + grouped_len + load_i64(parts, _NUM_TAIL_LEN) + (left + middle + right) * fill_len + 8)
    if ptr_is_null(state) != 0:
        if ptr_is_null(grouped) == 0:
            free(grouped)
        return null()
    _buffer_fill(state, fill, fill_len, left)
    if sign_char != 0:
        _buffer_char(state, sign_char)
    if prefix_len > 0:
        _buffer_append(state, load_ptr(parts, _NUM_PREFIX), prefix_len)
    _buffer_fill(state, fill, fill_len, middle)
    if grouped_len > 0:
        _buffer_append(state, ptr_add(grouped, load_i64(grouped_meta, 0)), grouped_len)
    if decimal != 0:
        _buffer_char(state, 46)
    if frac_len > 0:
        frac = load_ptr(parts, _NUM_FRAC)
        index: int = 0
        while index < frac_len:
            # 3.14+: fraction digits group left to right ("567,8").
            if frac_separator != 0 and index > 0 and index % 3 == 0:
                _buffer_char(state, frac_separator)
            _buffer_char(state, load_i8(frac, index))
            index = index + 1
    tail_len: int = load_i64(parts, _NUM_TAIL_LEN)
    if tail_len > 0:
        _buffer_append(state, load_ptr(parts, _NUM_TAIL), tail_len)
    _buffer_fill(state, fill, fill_len, right)
    if ptr_is_null(grouped) == 0:
        free(grouped)
    result = _buffer_string(state)
    _buffer_free(state)
    return result


def _clear_number_parts(parts) -> None:
    offset: int = 0
    while offset < _NUM_BYTES:
        store_i64(parts, offset, 0)
        offset = offset + 8


def _int_digit_text(value, base: int, meta):
    # |value| in ``base`` (lowercase) for a tagged, bool or heap int.  Returns
    # a malloc'd NUL-terminated buffer; meta[0] = negative, meta[8] = offset
    # of the first digit, meta[16] = digit count.
    small: int = 0
    use_big: int = 0
    if is_tagged_int(value) != 0:
        small = untag_int(value)
    elif _type_of(value) == PY_TYPE_BOOL:
        if ptr_eq(value, global_load_ptr("py_True")) != 0:
            small = 1
    else:
        overflow = stack_alloc(8)
        store_i64(overflow, 0, 0)
        small = py_int_to_i64(value, overflow)
        min_i64: int = -9223372036854775807
        min_i64 = min_i64 - 1
        if load_i32(overflow, 0) != 0 or small == min_i64:
            use_big = 1
    negative: int = 0
    if use_big != 0:
        text = null()
        if base == 10:
            text = py_bigint_to_cstr(value)
        else:
            text = py_bigint_to_base_cstr(value, base, 120)
        if ptr_is_null(text) != 0:
            return text
        if load_i8(text, 0) == 45:
            negative = 1
        start: int = negative
        if base != 10:
            start = start + 2
        store_i64(meta, 0, negative)
        store_i64(meta, 8, start)
        store_i64(meta, 16, strlen(text) - start)
        return text
    buffer = malloc(72)
    if ptr_is_null(buffer) != 0:
        return buffer
    magnitude: int = small
    if small < 0:
        negative = 1
        magnitude = 0 - small
    write: int = 71
    store_i8(buffer, write, 0)
    done: int = 0
    while done == 0:
        write = write - 1
        digit: int = magnitude % base
        if digit < 10:
            store_i8(buffer, write, 48 + digit)
        else:
            store_i8(buffer, write, 87 + digit)
        magnitude = magnitude // base
        if magnitude == 0:
            done = 1
    store_i64(meta, 0, negative)
    store_i64(meta, 8, write)
    store_i64(meta, 16, 71 - write)
    return buffer


def _int_to_double(value) -> float:
    # PyNumber_Float for an int or bool: OverflowError when out of range.
    if is_tagged_int(value) != 0:
        return float(untag_int(value))
    if _type_of(value) == PY_TYPE_BOOL:
        if ptr_eq(value, global_load_ptr("py_True")) != 0:
            return 1.0
        return 0.0
    converted: float = py_bigint_to_double(value)
    if converted != 0.0 and converted == converted * 2.0:
        py_raise_owned(py_exc_new(15, cstr("int too large to convert to float")))
        return 0.0
    return converted


def _is_float_code(code: int) -> int:
    if code == 101 or code == 69 or code == 102 or code == 70 or code == 103 or code == 71 or code == 37:
        return 1
    return 0


def _format_int_value(value, spec, length: int, type_name):
    # int.__format__ / bool.__format__ with a non-empty spec.
    parsed = stack_alloc(_SPEC_BYTES)
    if _parse_format_spec(spec, length, parsed, 100, 62, type_name) != 0:
        return null()
    code: int = load_i64(parsed, _SPEC_TYPE)
    if _is_float_code(code) != 0:
        converted: float = _int_to_double(value)
        if py_err_occurred() != 0:
            return null()
        return _format_float_parsed(converted, parsed, spec)
    base: int = 0
    if code == 100 or code == 110:
        base = 10
    elif code == 120 or code == 88:
        base = 16
    elif code == 111:
        base = 8
    elif code == 98:
        base = 2
    elif code != 99:
        return _raise_unknown_code(code, type_name)
    if load_i64(parsed, _SPEC_PRECISION) != -1:
        return _raise_format_error(cstr("Precision not allowed in integer format specifier"))
    if load_i64(parsed, _SPEC_NO_NEG_0) != 0:
        return _raise_format_error(
            cstr("Negative zero coercion (z) not allowed in integer format specifier")
        )
    parts = stack_alloc(_NUM_BYTES)
    _clear_number_parts(parts)
    if code == 99:
        if load_i64(parsed, _SPEC_SIGN) != 0:
            return _raise_format_error(cstr("Sign not allowed with integer format specifier 'c'"))
        if load_i64(parsed, _SPEC_ALT) != 0:
            return _raise_format_error(
                cstr("Alternate form (#) not allowed with integer format specifier 'c'")
            )
        point: int = 0
        if is_tagged_int(value) != 0:
            point = untag_int(value)
        elif _type_of(value) == PY_TYPE_BOOL:
            if ptr_eq(value, global_load_ptr("py_True")) != 0:
                point = 1
        else:
            overflow = stack_alloc(8)
            store_i64(overflow, 0, 0)
            point = py_int_to_i64(value, overflow)
            if load_i32(overflow, 0) != 0:
                py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C long")))
                return null()
        if point < 0 or point > 1114111:
            py_raise_owned(py_exc_new(15, cstr("%c arg not in range(0x110000)")))
            return null()
        character = py_chr_from_i64(point)
        if ptr_is_null(character) != 0:
            return null()
        store_ptr(parts, _NUM_TAIL, py_str_utf8(character))
        store_i64(parts, _NUM_TAIL_LEN, py_str_byte_len(character))
        store_i64(parts, _NUM_TAIL_CHARS, 1)
        result = _layout_number(parsed, spec, parts)
        py_decref(character)
        return result
    meta = stack_alloc(24)
    text = _int_digit_text(value, base, meta)
    if ptr_is_null(text) != 0:
        py_raise_owned(py_exc_new(19, cstr("out of memory")))
        return null()
    store_i64(parts, _NUM_NEGATIVE, load_i64(meta, 0))
    store_ptr(parts, _NUM_DIGITS, ptr_add(text, load_i64(meta, 8)))
    store_i64(parts, _NUM_DIGITS_LEN, load_i64(meta, 16))
    if code == 88:
        store_i64(parts, _NUM_UPPER, 1)
    if load_i64(parsed, _SPEC_ALT) != 0 and base != 10:
        prefix = cstr("0b")
        if code == 120:
            prefix = cstr("0x")
        elif code == 88:
            prefix = cstr("0X")
        elif code == 111:
            prefix = cstr("0o")
        store_ptr(parts, _NUM_PREFIX, prefix)
        store_i64(parts, _NUM_PREFIX_LEN, 2)
    grouping: int = load_i64(parsed, _SPEC_GROUPING)
    if grouping != 0:
        store_i64(parts, _NUM_SEPARATOR, grouping)
        if base == 10:
            store_i64(parts, _NUM_GROUP, 3)
        else:
            store_i64(parts, _NUM_GROUP, 4)
    result = _layout_number(parsed, spec, parts)
    free(text)
    return result


def _float_dtoa(absolute: float, mode: int, ndigits: int, digits, meta) -> int:
    # CPython's _Py_dg_dtoa for a finite, non-negative double: ASCII digits
    # with no trailing zeros into ``digits`` (800 bytes), the decimal point
    # position at meta[0]; returns the digit count.  Mode 0 is the shortest
    # round-tripping string, mode 2 ``ndigits`` significant digits (at least
    # one) and mode 3 ``ndigits`` digits past the decimal point.
    if absolute == 0.0:
        store_i8(digits, 0, 48)
        store_i64(meta, 0, 1)
        return 1
    count: int = 0
    if mode == 0:
        shortest = stack_alloc(16)
        value: int = _float_shortest_digits(absolute, shortest)
        significant: int = load_i64(shortest, 0)
        store_i64(meta, 0, load_i64(shortest, 8) + 1)
        position: int = significant
        while position > 0:
            position = position - 1
            store_i8(digits, position, 48 + value % 10)
            value = value // 10
        count = significant
    else:
        total: int = pcc_stdio_float_exact_digits(f64_bits(absolute), digits, meta)
        requested: int = ndigits
        if mode == 3:
            requested = load_i64(meta, 0) + ndigits
        elif requested < 1:
            requested = 1
        count = pcc_stdio_float_round_digits(digits, total, meta, requested)
        if count == 0:
            # Rounded away entirely: dtoa reports no digits with the point
            # just past the requested position.
            store_i64(meta, 0, 0 - ndigits)
        index: int = 0
        while index < count:
            store_i8(digits, index, 48 + load_i8(digits, index))
            index = index + 1
    while count > 0 and load_i8(digits, count - 1) == 48:
        count = count - 1
    return count


def _format_float_short(
    state,
    value: float,
    code: int,
    mode: int,
    precision: int,
    always_sign: int,
    add_dot_0: int,
    alt: int,
    no_neg_0: int,
    upper: int,
) -> int:
    # CPython pystrtod.c format_float_short: append the text of ``value``.
    # ``code`` is 'e', 'f', 'g' or 'r'; ``precision`` is already adjusted the
    # way PyOS_double_to_string does it ('e' + 1, 'g' at least 1).
    negative: int = f64_signbit(value)
    if value != value:
        if always_sign != 0:
            _buffer_char(state, 43)
        if upper != 0:
            return _buffer_cstr(state, cstr("NAN"))
        return _buffer_cstr(state, cstr("nan"))
    absolute: float = value
    if negative != 0:
        absolute = 0.0 - value
    if absolute != 0.0 and absolute == absolute * 2.0:
        if negative != 0:
            _buffer_char(state, 45)
        elif always_sign != 0:
            _buffer_char(state, 43)
        if upper != 0:
            return _buffer_cstr(state, cstr("INF"))
        return _buffer_cstr(state, cstr("inf"))
    digits = stack_alloc(800)
    meta = stack_alloc(8)
    count: int = _float_dtoa(absolute, mode, precision, digits, meta)
    decpt: int = load_i64(meta, 0)
    if no_neg_0 != 0 and negative != 0:
        if count == 0 or (count == 1 and load_i8(digits, 0) == 48):
            negative = 0
    vdigits_end: int = count
    use_exp: int = 0
    if code == 101:
        use_exp = 1
        vdigits_end = precision
    elif code == 102:
        vdigits_end = decpt + precision
    elif code == 103:
        limit: int = precision
        if add_dot_0 != 0:
            limit = precision - 1
        if decpt <= -4 or decpt > limit:
            use_exp = 1
        if alt != 0:
            vdigits_end = precision
    elif decpt <= -4 or decpt > 16:
        use_exp = 1
    exponent: int = 0
    if use_exp != 0:
        exponent = decpt - 1
        decpt = 1
    vdigits_start: int = 0
    if decpt <= 0:
        vdigits_start = decpt - 1
    if use_exp == 0 and add_dot_0 != 0:
        if vdigits_end <= decpt:
            vdigits_end = decpt + 1
    elif vdigits_end < decpt:
        vdigits_end = decpt
    if negative != 0:
        _buffer_char(state, 45)
    elif always_sign != 0:
        _buffer_char(state, 43)
    if decpt <= 0:
        _buffer_repeat(state, 48, decpt - vdigits_start)
        _buffer_char(state, 46)
        _buffer_repeat(state, 48, 0 - decpt)
    else:
        _buffer_repeat(state, 48, 0 - vdigits_start)
    if decpt > 0 and decpt <= count:
        _buffer_append(state, digits, decpt)
        _buffer_char(state, 46)
        _buffer_append(state, ptr_add(digits, decpt), count - decpt)
    else:
        _buffer_append(state, digits, count)
    if count < decpt:
        _buffer_repeat(state, 48, decpt - count)
        _buffer_char(state, 46)
        _buffer_repeat(state, 48, vdigits_end - decpt)
    else:
        _buffer_repeat(state, 48, vdigits_end - count)
    # A trailing decimal point goes unless '#'.
    length: int = load_i64(state, 8)
    data = load_ptr(state, 0)
    if alt == 0 and length > 0 and load_i8(data, length - 1) == 46:
        store_i64(state, 8, length - 1)
        store_i8(data, length - 1, 0)
    if use_exp != 0:
        if upper != 0:
            _buffer_char(state, 69)
        else:
            _buffer_char(state, 101)
        if exponent < 0:
            _buffer_char(state, 45)
            exponent = 0 - exponent
        else:
            _buffer_char(state, 43)
        if exponent >= 100:
            _buffer_char(state, 48 + exponent // 100)
            exponent = exponent % 100
            _buffer_char(state, 48 + exponent // 10)
        else:
            _buffer_char(state, 48 + exponent // 10)
        _buffer_char(state, 48 + exponent % 10)
    return 0


def _float_text(state, value: float, code: int, precision: int, always_sign: int, add_dot_0: int, alt: int, no_neg_0: int) -> int:
    # PyOS_double_to_string: ``code`` is e/E/f/F/g/G/r.
    upper: int = 0
    if code == 69 or code == 70 or code == 71:
        upper = 1
        code = code + 32
    mode: int = 2
    if code == 101:
        precision = precision + 1
    elif code == 102:
        mode = 3
    elif code == 103:
        if precision == 0:
            precision = 1
    else:
        mode = 0
        precision = 0
    return _format_float_short(
        state, value, code, mode, precision, always_sign, add_dot_0, alt, no_neg_0, upper
    )


def _format_float_parsed(value: float, parsed, spec):
    # CPython format_float_internal on an already-parsed spec.
    code: int = load_i64(parsed, _SPEC_TYPE)
    precision: int = load_i64(parsed, _SPEC_PRECISION)
    add_dot_0: int = 0
    default_precision: int = 6
    if code == 0:
        add_dot_0 = 1
        code = 114
        default_precision = 0
    if code == 110:
        code = 103
    add_pct: int = 0
    if code == 37:
        code = 102
        value = value * 100.0
        add_pct = 1
    if precision < 0:
        precision = default_precision
    elif code == 114:
        code = 103
    text = _buffer_new(precision + 64)
    if ptr_is_null(text) != 0:
        return null()
    _float_text(
        text,
        value,
        code,
        precision,
        0,
        add_dot_0,
        load_i64(parsed, _SPEC_ALT),
        load_i64(parsed, _SPEC_NO_NEG_0),
    )
    if add_pct != 0:
        _buffer_char(text, 37)
    # Split "[-]digits[.fraction]tail" (parse_number).
    data = load_ptr(text, 0)
    length: int = load_i64(text, 8)
    parts = stack_alloc(_NUM_BYTES)
    _clear_number_parts(parts)
    position: int = 0
    if length > 0 and load_i8(data, 0) == 45:
        store_i64(parts, _NUM_NEGATIVE, 1)
        position = 1
    digits_start: int = position
    while position < length and _is_digit_byte(load_i8(data, position)) != 0:
        position = position + 1
    store_ptr(parts, _NUM_DIGITS, ptr_add(data, digits_start))
    store_i64(parts, _NUM_DIGITS_LEN, position - digits_start)
    if position < length and load_i8(data, position) == 46:
        store_i64(parts, _NUM_DECIMAL, 1)
        position = position + 1
    frac_start: int = position
    while position < length and _is_digit_byte(load_i8(data, position)) != 0:
        position = position + 1
    store_ptr(parts, _NUM_FRAC, ptr_add(data, frac_start))
    store_i64(parts, _NUM_FRAC_LEN, position - frac_start)
    store_ptr(parts, _NUM_TAIL, ptr_add(data, position))
    store_i64(parts, _NUM_TAIL_LEN, length - position)
    store_i64(parts, _NUM_TAIL_CHARS, length - position)
    grouping: int = load_i64(parsed, _SPEC_GROUPING)
    if grouping != 0 and load_i64(parsed, _SPEC_TYPE) != 110:
        store_i64(parts, _NUM_GROUP, 3)
        store_i64(parts, _NUM_SEPARATOR, grouping)
    store_i64(parts, _NUM_FRAC_SEPARATOR, load_i64(parsed, _SPEC_FRAC_GROUPING))
    result = _layout_number(parsed, spec, parts)
    _buffer_free(text)
    return result


def _format_float_value(value, spec, length: int):
    parsed = stack_alloc(_SPEC_BYTES)
    if _parse_format_spec(spec, length, parsed, 0, 62, cstr("float")) != 0:
        return null()
    code: int = load_i64(parsed, _SPEC_TYPE)
    if code != 0 and code != 110 and _is_float_code(code) == 0:
        return _raise_unknown_code(code, cstr("float"))
    return _format_float_parsed(py_float_to_f64(value), parsed, spec)


def _format_str_value(value, spec, length: int):
    # str.__format__ (format_string_internal).
    parsed = stack_alloc(_SPEC_BYTES)
    if _parse_format_spec(spec, length, parsed, 115, 60, cstr("str")) != 0:
        return null()
    code: int = load_i64(parsed, _SPEC_TYPE)
    if code != 115:
        return _raise_unknown_code(code, cstr("str"))
    sign: int = load_i64(parsed, _SPEC_SIGN)
    if sign == 32:
        return _raise_format_error(cstr("Space not allowed in string format specifier"))
    if sign != 0:
        return _raise_format_error(cstr("Sign not allowed in string format specifier"))
    if load_i64(parsed, _SPEC_NO_NEG_0) != 0:
        return _raise_format_error(
            cstr("Negative zero coercion (z) not allowed in string format specifier")
        )
    if load_i64(parsed, _SPEC_ALT) != 0:
        return _raise_format_error(cstr("Alternate form (#) not allowed in string format specifier"))
    align: int = load_i64(parsed, _SPEC_ALIGN)
    if align == 61:
        return _raise_format_error(cstr("'=' alignment not allowed in string format specifier"))
    text = py_str_utf8(value)
    byte_len: int = py_str_byte_len(value)
    chars: int = _utf8_char_count(text, byte_len)
    precision: int = load_i64(parsed, _SPEC_PRECISION)
    if precision >= 0 and chars > precision:
        byte_len = _utf8_prefix_bytes(text, byte_len, precision)
        chars = precision
    width: int = load_i64(parsed, _SPEC_WIDTH)
    total: int = chars
    if width > chars:
        total = width
    left: int = 0
    if align == 62:
        left = total - chars
    elif align == 94:
        left = (total - chars) // 2
    right: int = total - chars - left
    fill_offset: int = load_i64(parsed, _SPEC_FILL)
    fill_len: int = load_i64(parsed, _SPEC_FILL_LEN)
    fill = cstr(" ")
    if fill_offset >= 0:
        fill = ptr_add(spec, fill_offset)
    state = _buffer_new(byte_len + (left + right) * fill_len + 8)
    if ptr_is_null(state) != 0:
        return null()
    _buffer_fill(state, fill, fill_len, left)
    _buffer_append(state, text, byte_len)
    _buffer_fill(state, fill, fill_len, right)
    result = _buffer_string(state)
    _buffer_free(state)
    return result


# Custom __format__ keeps every callback owner in one registered frame.
_FORMAT_METHOD = 0
_FORMAT_ARGS = 1
_FORMAT_SPEC = 2
_FORMAT_RESULT = 3
_FORMAT_ERROR = 4
_FORMAT_VALUE = 5
_FORMAT_SLOT_COUNT = 6

define_global_i32("pcc_format_callback_borrowed_map", -2)
define_global_i32("pcc_format_callback_owned_map", _FORMAT_SLOT_COUNT)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)


def _format_callback_adopt(slots: c_ptr, tokens: c_ptr, index: int) -> int:
    # The caller stores NEW before this first potentially parking operation.
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("format callback"), cstr("result owner lease failed"))
        return -1
    store_i64(tokens, offset, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _format_callback_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, offset, 0)
    pcc_gc_store_root(slot, null())


def _format_callback_body(slots: c_ptr, tokens: c_ptr, borrowed: c_ptr) -> int:
    # Inputs are borrowed; copy their actual registered slots before lookup.
    # The method does not exist until its empty owning frame is registered.
    token: int = pcc_gc_root_copy_borrowed_lease(
        ptr_add(slots, _FORMAT_VALUE * C_POINTER_SIZE), borrowed,
    )
    if token < 0:
        py_runtime_error_if_unset(cstr("format callback"), cstr("format value owner copy failed"))
        return -1
    store_i64(tokens, _FORMAT_VALUE * C_POINTER_SIZE, token)
    spec_slot = ptr_add(slots, _FORMAT_SPEC * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(borrowed, C_POINTER_SIZE)) == 0:
        token = pcc_gc_root_copy_borrowed_lease(spec_slot, ptr_add(borrowed, C_POINTER_SIZE))
        if token < 0:
            py_runtime_error_if_unset(cstr("format callback"), cstr("format spec owner copy failed"))
            return -1
        store_i64(tokens, _FORMAT_SPEC * C_POINTER_SIZE, token)
    store_ptr(slots, _FORMAT_METHOD * C_POINTER_SIZE, py_obj_getattr(
        load_ptr(slots, _FORMAT_VALUE * C_POINTER_SIZE), cstr("__format__"),
    ))
    if _format_callback_adopt(slots, tokens, _FORMAT_METHOD) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _FORMAT_METHOD * C_POINTER_SIZE)) != 0:
        # Preserve the existing missing-method fallback semantics.
        if py_err_occurred() != 0:
            py_clear_exception()
        store_ptr(slots, _FORMAT_RESULT * C_POINTER_SIZE, _format_value(
            load_ptr(slots, _FORMAT_VALUE * C_POINTER_SIZE), load_ptr(spec_slot, 0),
        ))
        return _format_callback_adopt(slots, tokens, _FORMAT_RESULT)
    if ptr_is_null(load_ptr(spec_slot, 0)) != 0:
        store_ptr(spec_slot, 0, py_str_new(cstr(""), 0))
        if _format_callback_adopt(slots, tokens, _FORMAT_SPEC) != 0:
            return -1
        if ptr_is_null(load_ptr(spec_slot, 0)) != 0:
            _format_require_result(null(), cstr("py_str_new"), cstr("format callback could not allocate an empty format spec"))
            return -1
    store_ptr(slots, _FORMAT_ARGS * C_POINTER_SIZE, py_tuple_new(1))
    if _format_callback_adopt(slots, tokens, _FORMAT_ARGS) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _FORMAT_ARGS * C_POINTER_SIZE)) != 0:
        _format_require_result(null(), cstr("py_tuple_new"), cstr("format callback argument tuple allocation failed"))
        return -1
    py_tuple_set_item(load_ptr(slots, _FORMAT_ARGS * C_POINTER_SIZE), 0, load_ptr(spec_slot, 0))
    if py_err_occurred() != 0:
        return -1
    store_ptr(slots, _FORMAT_RESULT * C_POINTER_SIZE, py_obj_call(
        load_ptr(slots, _FORMAT_METHOD * C_POINTER_SIZE),
        load_ptr(slots, _FORMAT_ARGS * C_POINTER_SIZE), global_load_ptr("py_None"),
    ))
    if _format_callback_adopt(slots, tokens, _FORMAT_RESULT) != 0:
        return -1
    if ptr_is_null(load_ptr(slots, _FORMAT_RESULT * C_POINTER_SIZE)) != 0:
        _format_require_result(null(), cstr("__format__"), cstr("format callback returned NULL without setting an exception"))
        return -1
    if py_str_check(load_ptr(slots, _FORMAT_RESULT * C_POINTER_SIZE)) == 0:
        py_raise_owned(py_exc_new(3, cstr("__format__ must return a str")))
        return -1
    return 0


def _call_object_format(value, spec):
    # Register incoming addresses before any callback or owner-copy operation.
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, value)
    store_ptr(borrowed, C_POINTER_SIZE, spec)
    pcc_gc_frame_enter(global_addr("pcc_format_callback_borrowed_map"), borrowed)
    slots = stack_alloc(_FORMAT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FORMAT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FORMAT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FORMAT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_format_callback_owned_map"), slots)
    status: int = _format_callback_body(slots, tokens, borrowed)
    # Preserve the original exception while decrefs can invoke arbitrary code.
    py_tls_exc_swap_slot(ptr_add(slots, _FORMAT_ERROR * C_POINTER_SIZE))
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, C_POINTER_SIZE, null())
    _format_callback_drop(slots, tokens, _FORMAT_ARGS)
    _format_callback_drop(slots, tokens, _FORMAT_SPEC)
    _format_callback_drop(slots, tokens, _FORMAT_METHOD)
    _format_callback_drop(slots, tokens, _FORMAT_VALUE)
    if status != 0:
        _format_callback_drop(slots, tokens, _FORMAT_RESULT)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _FORMAT_ERROR * C_POINTER_SIZE))
    result_slot = ptr_add(slots, _FORMAT_RESULT * C_POINTER_SIZE)
    # Only the terminal raw ABI transfer uses the existing temporary pin.
    # The counted result owner remains registered through all disposal above.
    prior: int = _unicode_format_pin(result_slot)
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, _FORMAT_RESULT * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


@c_abi_export("py_builtin_format")
def py_builtin_format(value, spec):
    """``format(value, format_spec)``: the spec is a str or str subclass."""
    if ptr_is_null(value) != 0 or ptr_is_null(spec) != 0:
        return null()
    if _type_of(spec) != PY_TYPE_STR and ptr_is_null(py_str_payload(spec)) != 0:
        state = _buffer_new(96)
        _buffer_cstr(state, cstr("format() argument 2 must be str, not "))
        if ptr_eq(spec, global_load_ptr("py_None")) != 0:
            # Argument Clinic names None itself, not its type.
            _buffer_cstr(state, cstr("None"))
        else:
            name = py_obj_type_name(spec)
            if ptr_is_null(name) == 0:
                _append_pystr(state, name)
                py_decref(name)
        return _raise_buffer_error(state, 3)
    return py_obj_format(value, spec)


@c_abi_export("py_obj_format")
def py_obj_format(value, spec):
    if ptr_is_null(value) != 0:
        return null()
    tag: int = _type_of(value)
    if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL and tag != PY_TYPE_FLOAT and tag != PY_TYPE_STR:
        return _call_object_format(value, spec)
    return _format_value(value, spec)


def _format_value(value, spec):
    # Ordinary scalar formatting and the existing object fallback.
    tag: int = _type_of(value)
    text = cstr("")
    length: int = 0
    none_obj = global_load_ptr("py_None")
    if ptr_is_null(spec) == 0 and ptr_eq(spec, none_obj) == 0 and _type_of(spec) == PY_TYPE_STR:
        text = py_str_utf8(spec)
        length = py_str_byte_len(spec)
    if length == 0:
        return py_obj_str(value)
    if tag == PY_TYPE_INT:
        return _format_int_value(value, text, length, cstr("int"))
    if tag == PY_TYPE_BOOL:
        return _format_int_value(value, text, length, cstr("bool"))
    if tag == PY_TYPE_FLOAT:
        return _format_float_value(value, text, length)
    if tag == PY_TYPE_STR:
        return _format_str_value(value, text, length)
    payload = py_str_payload(value)
    if ptr_is_null(payload) == 0:
        exact = py_str_exact_copy(value)
        if ptr_is_null(exact) != 0:
            return null()
        pcc_gc_pin(exact)
        result = _format_str_value(exact, text, length)
        pcc_gc_pin(result)
        pcc_gc_unpin(exact)
        py_decref(exact)
        pcc_gc_unpin(result)
        return result
    # object.__format__ rejects any non-empty spec.
    state = _buffer_new(96)
    _buffer_cstr(state, cstr("unsupported format string passed to "))
    name = py_obj_type_name(value)
    if ptr_is_null(name) == 0:
        _append_pystr(state, name)
        py_decref(name)
    _buffer_cstr(state, cstr(".__format__"))
    return _raise_buffer_error(state, 3)


# --- printf-style % formatting (CPython 3.15 unicodeobject.c / bytesobject.c)

_PCT_LJUST = 1
_PCT_SIGN = 2
_PCT_BLANK = 4
_PCT_ALT = 8
_PCT_ZERO = 16

# Percent-format conversions share an explicit layout in every nested frame.
# Both incoming values are borrowed from live caller owners; a returned NEW
# value is always stored into one of the registered owning slots first.
_PERCENT_INPUT = 0
_PERCENT_OTHER = 1
_PERCENT_ARGUMENT = 2
_PERCENT_TEMP = 3
_PERCENT_METHOD = 4
_PERCENT_CALL_ARGS = 5
_PERCENT_RESULT = 6
_PERCENT_ERROR = 7
_PERCENT_SLOT_COUNT = 8

define_global_i32("pcc_percent_borrowed_map", -2)
define_global_i32("pcc_percent_owned_map", _PERCENT_SLOT_COUNT)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
py_dict_get_default_slots = extern(
    "py_dict_get_default_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)


def _percent_frame_copy_inputs(slots: c_ptr, tokens: c_ptr, borrowed: c_ptr) -> int:
    index: int = 0
    while index < 2:
        offset: int = index * C_POINTER_SIZE
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, offset), ptr_add(borrowed, offset),
        )
        if token < 0:
            py_runtime_error_if_unset(cstr("percent format"), cstr("input owner copy failed"))
            return -1
        store_i64(tokens, offset, token)
        index = index + 1
    return 0


def _percent_copy(slots: c_ptr, tokens: c_ptr, index: int, source: c_ptr) -> int:
    offset: int = index * C_POINTER_SIZE
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, offset), source)
    if token < 0:
        py_runtime_error_if_unset(cstr("percent format"), cstr("argument owner copy failed"))
        return -1
    store_i64(tokens, offset, token)
    return 0


def _percent_drop(slots: c_ptr, tokens: c_ptr, index: int) -> None:
    # A conversion error survives finalizers from any temporary retirement.
    py_tls_exc_swap_slot(ptr_add(slots, _PERCENT_ERROR * C_POINTER_SIZE))
    _format_callback_drop(slots, tokens, index)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _PERCENT_ERROR * C_POINTER_SIZE))


def _percent_frame_finish(slots: c_ptr, tokens: c_ptr, borrowed: c_ptr, keep: int):
    py_tls_exc_swap_slot(ptr_add(slots, _PERCENT_ERROR * C_POINTER_SIZE))
    store_ptr(borrowed, 0, null())
    store_ptr(borrowed, C_POINTER_SIZE, null())
    index: int = _PERCENT_SLOT_COUNT - 1
    while index >= 0:
        if index != _PERCENT_ERROR and index != keep:
            _format_callback_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _PERCENT_ERROR * C_POINTER_SIZE))
    if keep < 0:
        pcc_gc_frame_leave(slots)
        pcc_gc_frame_leave(borrowed)
        return null()
    result_slot = ptr_add(slots, keep * C_POINTER_SIZE)
    prior: int = _unicode_format_pin(result_slot)
    if pcc_gc_foreign_lease_release(result_slot, load_i64(tokens, keep * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result_slot, prior)


def _percent_next_argument_slots(slots: c_ptr, tokens: c_ptr, cursor: c_ptr) -> int:
    arguments = load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE)
    if load_i64(cursor, _ARG_TUPLE) != 0:
        index: int = load_i64(cursor, _ARG_INDEX)
        count: int = py_tuple_len(arguments)
        if index >= count:
            _percent_not_enough(count)
            return -1
        # The input tuple is leased and immutable. Its traced owning field is
        # copied under the root-copy transaction before any conversion parks.
        if _percent_copy(slots, tokens, _PERCENT_ARGUMENT,
                ptr_add(arguments, PYTUPLEOBJECT_ITEMS_OFFSET + index * C_POINTER_SIZE)) != 0:
            return -1
        store_i64(cursor, _ARG_INDEX, index + 1)
        store_i64(cursor, _ARG_LABEL, 1)
        store_i64(cursor, _ARG_NUMBER, index + 1)
    else:
        if load_i64(cursor, _ARG_SINGLE_USED) != 0:
            _percent_not_enough(1)
            return -1
        if _percent_copy(slots, tokens, _PERCENT_ARGUMENT,
                ptr_add(slots, _PERCENT_INPUT * C_POINTER_SIZE)) != 0:
            return -1
        store_i64(cursor, _ARG_SINGLE_USED, 1)
        store_i64(cursor, _ARG_LABEL, 0)
    store_i64(cursor, _ARG_OWNED, 1)
    return 0


def _percent_release_argument_slots(slots: c_ptr, tokens: c_ptr, cursor: c_ptr) -> None:
    _percent_drop(slots, tokens, _PERCENT_ARGUMENT)
    store_i64(cursor, _ARG_OWNED, 0)


def _percent_mapping_argument_slots(slots: c_ptr, tokens: c_ptr, data: c_ptr,
                                    key_start: int, key_length: int, bytes_key: int) -> int:
    key_slot = ptr_add(slots, _PERCENT_TEMP * C_POINTER_SIZE)
    if bytes_key != 0:
        store_ptr(key_slot, 0, py_bytes_new(ptr_add(data, key_start), key_length))
    else:
        store_ptr(key_slot, 0, py_str_new(ptr_add(data, key_start), key_length))
    if _format_callback_adopt(slots, tokens, _PERCENT_TEMP) != 0:
        return -1
    if ptr_is_null(load_ptr(key_slot, 0)) != 0:
        return -1
    # An empty registered owner supplies the old silent-NULL miss default.
    # The actual dict probe publishes its protected value directly to ARGUMENT.
    status: int = py_dict_get_default_slots(
        ptr_add(slots, _PERCENT_INPUT * C_POINTER_SIZE), key_slot,
        ptr_add(slots, _PERCENT_METHOD * C_POINTER_SIZE),
        ptr_add(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE),
    )
    if _format_callback_adopt(slots, tokens, _PERCENT_ARGUMENT) != 0:
        status = -1
    if status == 0 and ptr_is_null(load_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE)) != 0:
        py_raise_owned(py_exc_new_with_value(4, load_ptr(key_slot, 0)))
        status = -1
    _percent_drop(slots, tokens, _PERCENT_TEMP)
    return status


# Argument cursor slots.  The label names the argument in error messages:
# 0 = the single non-tuple argument, 1 = tuple position, 2 = mapping key.
_ARG_INDEX = 0
_ARG_TUPLE = 8
_ARG_OWNED = 16
_ARG_LABEL = 24
_ARG_NUMBER = 32
_ARG_KEY = 40
_ARG_KEY_LEN = 48
_ARG_SINGLE_USED = 56
_ARG_BYTES = 64


def _buffer_decimal(state, value: int) -> None:
    if value < 0:
        _buffer_char(state, 45)
        value = 0 - value
    digits = stack_alloc(24)
    count: int = 0
    done: int = 0
    while done == 0:
        store_i8(digits, count, 48 + value % 10)
        value = value // 10
        count = count + 1
        if value == 0:
            done = 1
    while count > 0:
        count = count - 1
        _buffer_char(state, load_i8(digits, count))


def _append_type_name(state, value) -> None:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, value)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        _append_type_name_body(state, load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), slots, tokens)
    _percent_frame_finish(slots, tokens, borrowed, -1)


def _append_type_name_body(state, value, slots: c_ptr, tokens: c_ptr) -> None:
    store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_obj_type_name(value))
    if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
        return
    name = load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)
    if ptr_is_null(name) == 0:
        _append_pystr(state, name)


def _percent_not_enough(count: int) -> None:
    state = _buffer_new(64)
    _buffer_cstr(state, cstr("not enough arguments for format string (got "))
    _buffer_decimal(state, count)
    _buffer_char(state, 41)
    _raise_buffer_error(state, 3)


def _percent_error_at(message, position: int) -> int:
    # ValueError "<message> at position N" (N = the '%' offset).
    state = _buffer_new(96)
    _buffer_cstr(state, message)
    _buffer_cstr(state, cstr(" at position "))
    _buffer_decimal(state, position)
    _raise_buffer_error(state, 2)
    return -1


def _percent_arg_error(kind: int, data, cursor, message) -> int:
    # Raise ``kind`` with "format argument N: <message>"; frees ``message``.
    state = _buffer_new(96 + load_i64(message, 8))
    _buffer_cstr(state, cstr("format argument"))
    label: int = load_i64(cursor, _ARG_LABEL)
    if label == 1:
        _buffer_char(state, 32)
        _buffer_decimal(state, load_i64(cursor, _ARG_NUMBER))
    elif label == 2:
        _buffer_cstr(state, cstr(" '"))
        _buffer_append(
            state,
            ptr_add(data, load_i64(cursor, _ARG_KEY)),
            load_i64(cursor, _ARG_KEY_LEN),
        )
        _buffer_char(state, 39)
    _buffer_cstr(state, cstr(": "))
    _buffer_append(state, load_ptr(message, 0), load_i64(message, 8))
    _buffer_free(message)
    _raise_buffer_error(state, kind)
    return -1


def _percent_type_error(data, cursor, conversion: int, requirement, value) -> int:
    # "format argument 2: %x requires an integer, not float"
    message = _buffer_new(96)
    _buffer_char(message, 37)
    _buffer_char(message, conversion)
    _buffer_cstr(message, cstr(" requires "))
    _buffer_cstr(message, requirement)
    _buffer_cstr(message, cstr(", not "))
    _append_type_name(message, value)
    return _percent_arg_error(3, data, cursor, message)








def _percent_star(arguments, cursor: c_ptr, data, spec, slot: int) -> int:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, arguments)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        status = _percent_star_body(load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), cursor, data, spec, slot, slots, tokens)
    _percent_frame_finish(slots, tokens, borrowed, -1)
    return status


def _percent_star_body(arguments, cursor: c_ptr, data, spec, slot: int, slots: c_ptr, tokens: c_ptr) -> int:
    # A '*' width (slot 8) or precision (slot 16) from the next argument.
    if _percent_next_argument_slots(slots, tokens, cursor) != 0:
        return -1
    argument = load_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE)
    tag: int = _type_of(argument)
    if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
        message = _buffer_new(64)
        _buffer_cstr(message, cstr("* requires int, not "))
        _append_type_name(message, argument)
        _percent_release_argument_slots(slots, tokens, cursor)
        return _percent_arg_error(3, data, cursor, message)
    value: int = 0
    too_big: int = 0
    if tag == PY_TYPE_BOOL:
        if ptr_eq(argument, global_load_ptr("py_True")) != 0:
            value = 1
    else:
        overflow = stack_alloc(8)
        store_i64(overflow, 0, 0)
        value = py_int_to_i64(argument, overflow)
        if load_i32(overflow, 0) != 0:
            too_big = 1
        elif slot == 16 and (value > 2147483647 or value < -2147483648):
            too_big = 1
    _percent_release_argument_slots(slots, tokens, cursor)
    if too_big != 0:
        message = _buffer_new(32)
        if slot == 8:
            _buffer_cstr(message, cstr("too big for width"))
        else:
            _buffer_cstr(message, cstr("too big for precision"))
        return _percent_arg_error(15, data, cursor, message)
    store_i64(spec, slot, value)
    return 0


def _percent_output_number(output, text, length: int, flags: int, width: int, conversion: int) -> int:
    # unicode_format_arg_output for a numeric conversion: the sign (and a
    # '#' base prefix) precede zero padding but follow space padding.
    fill: int = 32
    if (flags & _PCT_ZERO) != 0:
        fill = 48
    position: int = 0
    sign_char: int = 0
    has_sign: int = 1
    first: int = 0
    if length > 0:
        first = load_i8(text, 0)
    if first == 45 or first == 43:
        sign_char = first
        length = length - 1
        position = 1
    elif (flags & _PCT_SIGN) != 0:
        sign_char = 43
    elif (flags & _PCT_BLANK) != 0:
        sign_char = 32
    else:
        has_sign = 0
    if width < length:
        width = length
    if has_sign != 0:
        if fill != 32:
            _buffer_char(output, sign_char)
        if width > length:
            width = width - 1
    prefixed: int = 0
    if (flags & _PCT_ALT) != 0 and (conversion == 120 or conversion == 88 or conversion == 111):
        prefixed = 1
        if fill != 32:
            _buffer_append(output, ptr_add(text, position), 2)
            position = position + 2
        width = width - 2
        if width < 0:
            width = 0
        length = length - 2
    if width > length and (flags & _PCT_LJUST) == 0:
        _buffer_repeat(output, fill, width - length)
        width = length
    if fill == 32:
        if has_sign != 0:
            _buffer_char(output, sign_char)
        if prefixed != 0:
            _buffer_append(output, ptr_add(text, position), 2)
            position = position + 2
    _buffer_append(output, ptr_add(text, position), length)
    if width > length:
        _buffer_repeat(output, 32, width - length)
    return 0


def _percent_output_text(output, text, byte_len: int, flags: int, width: int, precision: int, count_chars: int) -> int:
    # %s / %r / %a / %c: precision truncates, width pads with spaces; both
    # count characters in str formatting and bytes in bytes formatting.
    length: int = byte_len
    if count_chars != 0:
        length = _utf8_char_count(text, byte_len)
    if precision >= 0 and length > precision:
        if count_chars != 0:
            byte_len = _utf8_prefix_bytes(text, byte_len, precision)
        else:
            byte_len = precision
        length = precision
    padding: int = 0
    if width > length:
        padding = width - length
    if (flags & _PCT_LJUST) == 0:
        _buffer_repeat(output, 32, padding)
    _buffer_append(output, text, byte_len)
    if (flags & _PCT_LJUST) != 0:
        _buffer_repeat(output, 32, padding)
    return 0


def _percent_integer(output, argument, data, cursor, conversion: int, flags: int, width: int, precision: int) -> int:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, argument)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        status = _percent_integer_body(output, load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), data, cursor, conversion, flags, width, precision, slots, tokens)
    _percent_frame_finish(slots, tokens, borrowed, -1)
    return status


def _percent_integer_body(output, argument, data, cursor, conversion: int, flags: int, width: int, precision: int, slots: c_ptr, tokens: c_ptr) -> int:
    # mainformatlong + _PyUnicode_FormatLong.
    hexish: int = 0
    if conversion == 120 or conversion == 88 or conversion == 111:
        hexish = 1
    tag: int = _type_of(argument)
    value = argument
    owned: int = 0
    if tag == PY_TYPE_FLOAT and hexish == 0:
        store_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE,
                  py_int_from_f64_exact(py_float_to_f64(argument)))
        if _format_callback_adopt(slots, tokens, _PERCENT_TEMP) != 0:
            return -1
        value = load_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE)
        if ptr_is_null(value) != 0:
            return -1
        owned = 1
    elif tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
        if hexish != 0:
            return _percent_type_error(data, cursor, conversion, cstr("an integer"), argument)
        return _percent_type_error(data, cursor, conversion, cstr("a real number"), argument)
    base: int = 10
    if conversion == 120 or conversion == 88:
        base = 16
    elif conversion == 111:
        base = 8
    meta = stack_alloc(24)
    digits_text = _int_digit_text(value, base, meta)
    if owned != 0:
        _percent_drop(slots, tokens, _PERCENT_TEMP)
    if ptr_is_null(digits_text) != 0:
        py_raise_owned(py_exc_new(19, cstr("out of memory")))
        return -1
    count: int = load_i64(meta, 16)
    body = _buffer_new(count + precision + 8)
    if load_i64(meta, 0) != 0:
        _buffer_char(body, 45)
    if (flags & _PCT_ALT) != 0 and base != 10:
        _buffer_char(body, 48)
        if conversion == 111:
            _buffer_char(body, 111)
        else:
            _buffer_char(body, conversion)
    if precision > count:
        _buffer_repeat(body, 48, precision - count)
    source = ptr_add(digits_text, load_i64(meta, 8))
    index: int = 0
    while index < count:
        byte: int = load_i8(source, index)
        if conversion == 88 and byte >= 97 and byte <= 102:
            byte = byte - 32
        _buffer_char(body, byte)
        index = index + 1
    free(digits_text)
    _percent_output_number(output, load_ptr(body, 0), load_i64(body, 8), flags, width, conversion)
    _buffer_free(body)
    return 0


def _percent_float(output, argument, data, cursor, conversion: int, flags: int, width: int, precision: int) -> int:
    # formatfloat: PyOS_double_to_string(x, conversion, precision, alt).
    tag: int = _type_of(argument)
    value: float = 0.0
    if tag == PY_TYPE_FLOAT:
        value = py_float_to_f64(argument)
    elif tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        value = _int_to_double(argument)
        if py_err_occurred() != 0:
            return -1
    else:
        return _percent_type_error(data, cursor, conversion, cstr("a real number"), argument)
    if precision < 0:
        precision = 6
    body = _buffer_new(precision + 64)
    alt: int = 0
    if (flags & _PCT_ALT) != 0:
        alt = 1
    _float_text(body, value, conversion, precision, 0, 0, alt, 0)
    _percent_output_number(output, load_ptr(body, 0), load_i64(body, 8), flags, width, conversion)
    _buffer_free(body)
    return 0


def _percent_char(output, argument, data, cursor, flags: int, width: int, bytes_mode: int) -> int:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, argument)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        status = _percent_char_body(output, load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), data, cursor, flags, width, bytes_mode, slots, tokens)
    _percent_frame_finish(slots, tokens, borrowed, -1)
    return status


def _percent_char_body(output, argument, data, cursor, flags: int, width: int, bytes_mode: int, slots: c_ptr, tokens: c_ptr) -> int:
    tag: int = _type_of(argument)
    if bytes_mode != 0:
        if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
            store_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE, py_bytes_from_obj(argument))
            if _format_callback_adopt(slots, tokens, _PERCENT_TEMP) != 0:
                return -1
            copied = load_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE)
            if ptr_is_null(copied) != 0:
                return -1
            payload = _bytes_payload(copied)
            payload_length: int = _bytes_payload_length(copied)
            if payload_length != 1:
                message = _buffer_new(96)
                _buffer_cstr(
                    message,
                    cstr("%c requires an integer in range(256) or a single byte, not a bytes object of length "),
                )
                _buffer_decimal(message, payload_length)
                return _percent_arg_error(3, data, cursor, message)
            return _percent_output_text(output, payload, 1, flags, width, -1, 0)
        if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
            return _percent_type_error(
                data, cursor, 99, cstr("an integer in range(256) or a single byte"), argument
            )
        overflow = stack_alloc(8)
        store_i64(overflow, 0, 0)
        byte_value: int = py_int_to_i64(argument, overflow)
        if load_i32(overflow, 0) != 0 or byte_value < 0 or byte_value > 255:
            message = _buffer_new(48)
            _buffer_cstr(message, cstr("%c argument not in range(256)"))
            return _percent_arg_error(15, data, cursor, message)
        one = stack_alloc(1)
        store_i8(one, 0, byte_value)
        return _percent_output_text(output, one, 1, flags, width, -1, 0)
    if tag == PY_TYPE_STR:
        text = py_str_utf8(argument)
        byte_len: int = py_str_byte_len(argument)
        chars: int = _utf8_char_count(text, byte_len)
        if chars != 1:
            message = _buffer_new(96)
            _buffer_cstr(
                message,
                cstr("%c requires an integer or a unicode character, not a string of length "),
            )
            _buffer_decimal(message, chars)
            return _percent_arg_error(3, data, cursor, message)
        return _percent_output_text(output, text, byte_len, flags, width, -1, 1)
    if tag != PY_TYPE_INT and tag != PY_TYPE_BOOL:
        return _percent_type_error(data, cursor, 99, cstr("an integer or a unicode character"), argument)
    overflow2 = stack_alloc(8)
    store_i64(overflow2, 0, 0)
    point: int = py_int_to_i64(argument, overflow2)
    if load_i32(overflow2, 0) != 0 or point < 0 or point > 1114111:
        message = _buffer_new(48)
        _buffer_cstr(message, cstr("%c argument not in range(0x110000)"))
        return _percent_arg_error(15, data, cursor, message)
    store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_chr_from_i64(point))
    if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
        return -1
    character = load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)
    if ptr_is_null(character) != 0:
        return -1
    rc: int = _percent_output_text(
        output, py_str_utf8(character), py_str_byte_len(character), flags, width, -1, 1
    )
    _percent_drop(slots, tokens, _PERCENT_RESULT)
    return rc


def _percent_text(output, argument, data, cursor, conversion: int, flags: int, width: int, precision: int, bytes_mode: int) -> int:
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, argument)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        status = _percent_text_body(output, load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), data, cursor, conversion, flags, width, precision, bytes_mode, slots, tokens)
    _percent_frame_finish(slots, tokens, borrowed, -1)
    return status


def _percent_text_body(output, argument, data, cursor, conversion: int, flags: int, width: int, precision: int, bytes_mode: int, slots: c_ptr, tokens: c_ptr) -> int:
    if bytes_mode != 0 and (conversion == 115 or conversion == 98):
        argument_tag: int = _type_of(argument)
        if argument_tag == PY_TYPE_BYTES or argument_tag == PY_TYPE_BYTEARRAY or argument_tag == PY_TYPE_MEMORYVIEW:
            # Materialize through the rooted buffer constructor. A view owner
            # alone cannot keep an interior base-object address from moving.
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_bytes_from_obj(argument))
            if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
                return -1
            copied = load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)
            if ptr_is_null(copied) != 0:
                return -1
            return _percent_output_text(
                output, _bytes_payload(copied), _bytes_payload_length(copied), flags, width, precision, 0
            )
        # Only an object with __bytes__ converts; bytes(5) would be five NULs.
        converted = null()
        tag: int = _type_of(argument)
        if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
            store_ptr(slots, _PERCENT_METHOD * C_POINTER_SIZE,
                      py_obj_getattr(argument, cstr("__bytes__")))
            if _format_callback_adopt(slots, tokens, _PERCENT_METHOD) != 0:
                return -1
            method = load_ptr(slots, _PERCENT_METHOD * C_POINTER_SIZE)
            if ptr_is_null(method) == 0:
                store_ptr(slots, _PERCENT_CALL_ARGS * C_POINTER_SIZE, py_tuple_new(0))
                if _format_callback_adopt(slots, tokens, _PERCENT_CALL_ARGS) != 0:
                    return -1
                if ptr_is_null(load_ptr(slots, _PERCENT_CALL_ARGS * C_POINTER_SIZE)) != 0:
                    return -1
                store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_obj_call(
                    load_ptr(slots, _PERCENT_METHOD * C_POINTER_SIZE),
                    load_ptr(slots, _PERCENT_CALL_ARGS * C_POINTER_SIZE), global_load_ptr("py_None"),
                ))
                if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
                    return -1
                _percent_drop(slots, tokens, _PERCENT_CALL_ARGS)
                _percent_drop(slots, tokens, _PERCENT_METHOD)
                converted = load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)
                if ptr_is_null(converted) != 0:
                    return -1
            elif py_err_occurred() != 0:
                py_clear_exception()
        converted_tag: int = -1
        if ptr_is_null(converted) == 0:
            converted_tag = _type_of(converted)
        if converted_tag != PY_TYPE_BYTES and converted_tag != PY_TYPE_BYTEARRAY and converted_tag != PY_TYPE_MEMORYVIEW:
            if ptr_is_null(converted) == 0:
                _percent_drop(slots, tokens, _PERCENT_RESULT)
            message = _buffer_new(96)
            _buffer_cstr(
                message,
                cstr("%b requires a bytes-like object, or an object that implements __bytes__, not "),
            )
            _append_type_name(message, argument)
            return _percent_arg_error(3, data, cursor, message)
        # Preserve the existing accepted bytes-like custom-result set, but
        # never traverse an unleased memoryview base before taking its copy.
        store_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE, py_bytes_from_obj(converted))
        if _format_callback_adopt(slots, tokens, _PERCENT_TEMP) != 0:
            return -1
        copied_result = load_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE)
        if ptr_is_null(copied_result) != 0:
            return -1
        rc: int = _percent_output_text(
            output,
            _bytes_payload(copied_result),
            _bytes_payload_length(copied_result),
            flags,
            width,
            precision,
            0,
        )
        _percent_drop(slots, tokens, _PERCENT_RESULT)
        return rc
    if conversion == 115 and _type_of(argument) == PY_TYPE_STR:
        if _percent_copy(slots, tokens, _PERCENT_RESULT,
                ptr_add(slots, _PERCENT_INPUT * C_POINTER_SIZE)) != 0:
            return -1
    else:
        if conversion == 115:
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_obj_str(argument))
        elif conversion == 114 and bytes_mode == 0:
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_obj_repr(argument))
        else:
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, py_obj_ascii(argument))
        if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
            return -1
    rendered = load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)
    if ptr_is_null(rendered) != 0 or _type_of(rendered) != PY_TYPE_STR:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(3, cstr("format argument cannot be converted to string")))
        return -1
    rc2: int = _percent_output_text(
        output,
        py_str_utf8(rendered),
        py_str_byte_len(rendered),
        flags,
        width,
        precision,
        1 - bytes_mode,
    )
    _percent_drop(slots, tokens, _PERCENT_RESULT)
    return rc2


def _bytes_payload(value):
    tag: int = _type_of(value)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return ptr_add(value, 24)
    if tag == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(value, ptr_add(value, 16))
        return _bytes_payload(base)
    return null()


def _bytes_payload_length(value) -> int:
    tag: int = _type_of(value)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return load_i64(value, 16)
    if tag == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(value, ptr_add(value, 16))
        return _bytes_payload_length(base)
    return -1





def _format_percent(data: c_ptr, length: int, arguments, bytes_mode: int):
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, arguments)
    store_ptr(borrowed, C_POINTER_SIZE, null())
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    if status == 0:
        status = _format_percent_body(data, length, load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), bytes_mode, slots, tokens)
    keep: int = -1
    if status == 0:
        keep = _PERCENT_RESULT
    return _percent_frame_finish(slots, tokens, borrowed, keep)


def _format_percent_body(data: c_ptr, length: int, arguments, bytes_mode: int, slots: c_ptr, tokens: c_ptr):
    output = _buffer_new(length + 64)
    if ptr_is_null(output) != 0:
        py_raise_owned(py_exc_new(19, cstr("out of memory")))
        return -1
    cursor = stack_alloc(_ARG_BYTES)
    offset: int = 0
    while offset < _ARG_BYTES:
        store_i64(cursor, offset, 0)
        offset = offset + 8
    spec = stack_alloc(24)
    arguments_tag: int = _type_of(arguments)
    is_mapping: int = 0
    if arguments_tag == PY_TYPE_DICT:
        is_mapping = 1
    if arguments_tag == PY_TYPE_TUPLE:
        store_i64(cursor, _ARG_TUPLE, 1)
    used_key: int = 0
    failed: int = 0
    position: int = 0
    while position < length and failed == 0:
        byte: int = load_i8(data, position)
        if byte != 37:
            run: int = position
            while run < length and load_i8(data, run) != 37:
                run = run + 1
            _buffer_append(output, ptr_add(data, position), run - position)
            position = run
            continue
        percent: int = position
        position = position + 1
        if position < length and load_i8(data, position) == 37:
            _buffer_char(output, 37)
            position = position + 1
            continue
        argument = null()
        keyed: int = 0
        if position < length and load_i8(data, position) == 40:
            if is_mapping == 0:
                message = _buffer_new(64)
                _buffer_cstr(message, cstr("format requires a mapping, not "))
                _append_type_name(message, arguments)
                _raise_buffer_error(message, 3)
                failed = 1
                break
            depth: int = 1
            key_start: int = position + 1
            scan: int = key_start
            while scan < length and depth > 0:
                key_byte: int = load_i8(data, scan)
                if key_byte == 40:
                    depth = depth + 1
                elif key_byte == 41:
                    depth = depth - 1
                scan = scan + 1
            if depth > 0:
                _percent_error_at(cstr("stray % or incomplete format key"), percent)
                failed = 1
                break
            if _percent_mapping_argument_slots(
                    slots, tokens, data, key_start, scan - 1 - key_start, bytes_mode) != 0:
                failed = 1
                break
            argument = load_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE)
            store_i64(cursor, _ARG_OWNED, 1)
            store_i64(cursor, _ARG_LABEL, 2)
            store_i64(cursor, _ARG_KEY, key_start)
            store_i64(cursor, _ARG_KEY_LEN, scan - 1 - key_start)
            keyed = 1
            used_key = 1
            position = scan
        flags: int = 0
        scanning: int = 1
        while scanning != 0 and position < length:
            flag_byte: int = load_i8(data, position)
            if flag_byte == 45:
                flags = flags | _PCT_LJUST
            elif flag_byte == 43:
                flags = flags | _PCT_SIGN
            elif flag_byte == 32:
                flags = flags | _PCT_BLANK
            elif flag_byte == 35:
                flags = flags | _PCT_ALT
            elif flag_byte == 48:
                flags = flags | _PCT_ZERO
            else:
                scanning = 0
            if scanning != 0:
                position = position + 1
        store_i64(spec, 8, -1)
        store_i64(spec, 16, -1)
        if position < length and load_i8(data, position) == 42:
            if keyed != 0:
                _percent_release_argument_slots(slots, tokens, cursor)
                _percent_error_at(cstr("* cannot be used with a parenthesised mapping key"), percent)
                failed = 1
                break
            if _percent_star(arguments, cursor, data, spec, 8) != 0:
                failed = 1
                break
            if load_i64(spec, 8) < 0:
                flags = flags | _PCT_LJUST
                store_i64(spec, 8, 0 - load_i64(spec, 8))
            position = position + 1
        elif position < length and _is_digit_byte(load_i8(data, position)) != 0:
            store_i64(spec, 8, _parse_digits(data, position))
            position = _skip_digits(data, position)
        if position < length and load_i8(data, position) == 46:
            position = position + 1
            if position < length and load_i8(data, position) == 42:
                if keyed != 0:
                    _percent_release_argument_slots(slots, tokens, cursor)
                    _percent_error_at(cstr("* cannot be used with a parenthesised mapping key"), percent)
                    failed = 1
                    break
                if _percent_star(arguments, cursor, data, spec, 16) != 0:
                    failed = 1
                    break
                if load_i64(spec, 16) < 0:
                    store_i64(spec, 16, 0)
                position = position + 1
            else:
                store_i64(spec, 16, _parse_digits(data, position))
                position = _skip_digits(data, position)
        if position < length:
            modifier: int = load_i8(data, position)
            if modifier == 104 or modifier == 108 or modifier == 76:
                position = position + 1
        if position >= length:
            _percent_release_argument_slots(slots, tokens, cursor)
            _percent_error_at(cstr("stray %"), percent)
            failed = 1
            break
        conversion: int = load_i8(data, position)
        position = position + 1
        if keyed == 0:
            if is_mapping != 0 and used_key != 0:
                _percent_error_at(cstr("format requires a parenthesised mapping key"), percent)
                failed = 1
                break
            if _percent_next_argument_slots(slots, tokens, cursor) != 0:
                failed = 1
                break
            argument = load_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE)
        width: int = load_i64(spec, 8)
        precision: int = load_i64(spec, 16)
        rc: int = 0
        if (
            conversion == 115
            or conversion == 114
            or conversion == 97
            or (bytes_mode != 0 and conversion == 98)
        ):
            rc = _percent_text(output, argument, data, cursor, conversion, flags, width, precision, bytes_mode)
        elif (
            conversion == 100
            or conversion == 105
            or conversion == 117
            or conversion == 120
            or conversion == 88
            or conversion == 111
        ):
            rc = _percent_integer(output, argument, data, cursor, conversion, flags, width, precision)
        elif (
            conversion == 101
            or conversion == 69
            or conversion == 102
            or conversion == 70
            or conversion == 103
            or conversion == 71
        ):
            rc = _percent_float(output, argument, data, cursor, conversion, flags, width, precision)
        elif conversion == 99:
            rc = _percent_char(output, argument, data, cursor, flags, width, bytes_mode)
        else:
            message = _buffer_new(64)
            _buffer_cstr(message, cstr("unsupported format %"))
            _buffer_char(message, conversion)
            _buffer_cstr(message, cstr(" at position "))
            _buffer_decimal(message, percent)
            _raise_buffer_error(message, 2)
            rc = -1
        _percent_release_argument_slots(slots, tokens, cursor)
        if rc != 0:
            failed = 1
    if failed == 0 and is_mapping == 0:
        required: int = 0
        given: int = 0
        if load_i64(cursor, _ARG_TUPLE) != 0:
            required = load_i64(cursor, _ARG_INDEX)
            given = py_tuple_len(arguments)
        elif load_i64(cursor, _ARG_SINGLE_USED) == 0:
            given = 1
        if required < given:
            message = _buffer_new(96)
            _buffer_cstr(message, cstr("not all arguments converted during "))
            if bytes_mode != 0:
                _buffer_cstr(message, cstr("bytes formatting (required "))
            else:
                _buffer_cstr(message, cstr("string formatting (required "))
            _buffer_decimal(message, required)
            _buffer_cstr(message, cstr(", got "))
            _buffer_decimal(message, given)
            _buffer_char(message, 41)
            _raise_buffer_error(message, 3)
            failed = 1
    if failed == 0:
        if bytes_mode != 0:
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, _buffer_bytes(output))
        else:
            store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, _buffer_string(output))
        if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
            failed = 1
    _buffer_free(output)
    return -1 if failed != 0 else 0


def _percent_format_entry(format_obj, arguments, bytes_mode: int):
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, format_obj)
    store_ptr(borrowed, C_POINTER_SIZE, arguments)
    pcc_gc_frame_enter(global_addr("pcc_percent_borrowed_map"), borrowed)
    slots = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _PERCENT_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_percent_owned_map"), slots)
    status: int = _percent_frame_copy_inputs(slots, tokens, borrowed)
    keep: int = -1
    if status == 0:
        keep = _percent_format_entry_body(load_ptr(slots, _PERCENT_INPUT * C_POINTER_SIZE), load_ptr(slots, _PERCENT_OTHER * C_POINTER_SIZE), bytes_mode, slots, tokens)
    return _percent_frame_finish(slots, tokens, borrowed, keep)


def _percent_format_entry_body(format_obj, arguments, bytes_mode: int,
                               slots: c_ptr, tokens: c_ptr) -> int:
    format_tag: int = _type_of(format_obj)
    if bytes_mode == 0:
        if ptr_is_null(format_obj) != 0 or format_tag != PY_TYPE_STR:
            py_raise_owned(py_exc_new(3, cstr("left operand of % must be str")))
            return -1
        store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, _format_percent(
            py_str_utf8(format_obj), py_str_byte_len(format_obj), arguments, 0,
        ))
    else:
        if format_tag != PY_TYPE_BYTES and format_tag != PY_TYPE_BYTEARRAY:
            py_raise_owned(py_exc_new(3, cstr("left operand of % must be bytes or bytearray")))
            return -1
        if format_tag == PY_TYPE_BYTEARRAY:
            store_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE, py_bytes_from_obj(format_obj))
            if _format_callback_adopt(slots, tokens, _PERCENT_ARGUMENT) != 0:
                return -1
            format_obj = load_ptr(slots, _PERCENT_ARGUMENT * C_POINTER_SIZE)
            if ptr_is_null(format_obj) != 0:
                return -1
        store_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE, _format_percent(
            ptr_add(format_obj, 24), load_i64(format_obj, 16), arguments, 1,
        ))
    if _format_callback_adopt(slots, tokens, _PERCENT_RESULT) != 0:
        return -1
    if bytes_mode != 0 and format_tag == PY_TYPE_BYTEARRAY:
        if ptr_is_null(load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)) == 0:
            store_ptr(slots, _PERCENT_TEMP * C_POINTER_SIZE,
                      py_bytearray_from_obj(load_ptr(slots, _PERCENT_RESULT * C_POINTER_SIZE)))
            if _format_callback_adopt(slots, tokens, _PERCENT_TEMP) != 0:
                return -1
            return _PERCENT_TEMP
    return _PERCENT_RESULT


@c_abi_export("py_str_mod")
def py_str_mod(format_obj, arguments):
    return _percent_format_entry(format_obj, arguments, 0)


@c_abi_export("py_bytes_mod")
def py_bytes_mod(format_obj, arguments):
    return _percent_format_entry(format_obj, arguments, 1)


py_bytes_len = extern("py_bytes_len", (c_ptr,), c_int64)
py_bytes_data_ptr = extern("py_bytes_data_ptr", (c_ptr,), c_ptr)
py_unicode_error_get_field = extern("py_unicode_error_get_field", (c_ptr, c_int64), c_ptr)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_str_ord_at_i64 = extern("py_str_ord_at_i64", (c_ptr, c_int64), c_int64)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
define_global_i32("pcc_unicode_format_borrowed_map", -1)
define_global_i32("pcc_unicode_format_owned_map", 8)


def _unicode_format_pin(slot) -> int:
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), slot)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _unicode_format_append(state, slots, index: int, repr_mode: int) -> int:
    source_slot = ptr_add(slots, index * 8)
    source_pin: int = _unicode_format_pin(source_slot)
    value = load_ptr(source_slot, 0)
    if repr_mode != 0:
        rendered = py_obj_repr(value)
    else:
        rendered = py_obj_str(value)
    store_ptr(slots, 40, rendered)
    store_ptr(source_slot, 0, pcc_gc_take_pinned_slot(source_slot, source_pin))
    if ptr_is_null(rendered):
        return -1
    slot = ptr_add(slots, 40)
    prior: int = _unicode_format_pin(slot)
    rc: int = _append_pystr(state, load_ptr(slot, 0))
    store_ptr(slot, 0, pcc_gc_take_pinned_slot(slot, prior))
    pcc_gc_store_root(slot, null())
    return rc


def _unicode_format_decimal(state, number: int) -> None:
    if number == -9223372036854775807 - 1:
        _buffer_cstr(state, cstr("-9223372036854775808"))
    else:
        _buffer_decimal(state, number)


def _unicode_format_repr(state, slots, type_tag: int) -> int:
    _buffer_cstr(state, cstr("UnicodeDecodeError") if type_tag == 58 else cstr("UnicodeEncodeError"))
    _buffer_char(state, 40)
    args_slot = ptr_add(slots, 56)
    count: int = py_tuple_len(pcc_gc_load_ptr(null(), args_slot))
    i: int = 0
    while i < count:
        if i != 0:
            _buffer_cstr(state, cstr(", "))
        prior: int = _unicode_format_pin(args_slot)
        store_ptr(slots, 0, py_tuple_get(load_ptr(args_slot, 0), i))
        store_ptr(args_slot, 0, pcc_gc_take_pinned_slot(args_slot, prior))
        if ptr_is_null(load_ptr(slots, 0)) or _unicode_format_append(state, slots, 0, 1) != 0:
            return -1
        pcc_gc_store_root(slots, null())
        i = i + 1
    _buffer_char(state, 41)
    return 0


def _unicode_format_body(state, slots, repr_mode: int, type_tag: int) -> int:
    if repr_mode != 0:
        return _unicode_format_repr(state, slots, type_tag)
    source = pcc_gc_load_ptr(null(), ptr_add(slots, 8))
    if type_tag == 58:
        if _type_of(source) != PY_TYPE_BYTES:
            py_raise_owned(py_exc_new(3, cstr("UnicodeError 'object' attribute must be a bytes")))
            return -1
    elif _type_of(source) != PY_TYPE_STR:
        py_raise_owned(py_exc_new(3, cstr("UnicodeError 'object' attribute must be a string")))
        return -1
    start: int = py_int_value_i64(pcc_gc_load_ptr(null(), ptr_add(slots, 16)))
    end: int = py_int_value_i64(pcc_gc_load_ptr(null(), ptr_add(slots, 24)))
    source_slot = ptr_add(slots, 8)
    source_pin: int = _unicode_format_pin(source_slot)
    length: int = 0
    if type_tag == 58:
        length = py_bytes_len(load_ptr(source_slot, 0))
    else:
        length = py_str_len(load_ptr(source_slot, 0))
    code: int = -1
    if start >= 0 and start < length and end == start + 1:
        if type_tag == 58:
            code = load_i8(py_bytes_data_ptr(load_ptr(source_slot, 0)), start) & 255
        else:
            code = py_str_ord_at_i64(load_ptr(source_slot, 0), start)
    store_ptr(source_slot, 0, pcc_gc_take_pinned_slot(source_slot, source_pin))
    _buffer_char(state, 39)
    if _unicode_format_append(state, slots, 0, 0) != 0:
        return -1
    if code >= 0:
        width: int = 2
        if type_tag == 58:
            _buffer_cstr(state, cstr("' codec can't decode byte 0x"))
        else:
            _buffer_cstr(state, cstr("' codec can't encode character '\\"))
        if type_tag == 58:
            width = 2
        elif code <= 255:
            _buffer_char(state, 120)
        elif code <= 65535:
            _buffer_char(state, 117)
            width = 4
        else:
            _buffer_char(state, 85)
            width = 8
        shift: int = (width - 1) * 4
        while shift >= 0:
            digit: int = (code >> shift) & 15
            _buffer_char(state, 48 + digit if digit < 10 else 87 + digit)
            shift = shift - 4
        _buffer_cstr(state, cstr(" in position ") if type_tag == 58 else cstr("' in position "))
        _unicode_format_decimal(state, start)
    else:
        if type_tag == 58:
            _buffer_cstr(state, cstr("' codec can't decode bytes in position "))
        else:
            _buffer_cstr(state, cstr("' codec can't encode characters in position "))
        _unicode_format_decimal(state, start)
        _buffer_char(state, 45)
        # CPython formats this descriptor through Py_ssize_t, including the
        # wrap at its minimum. Spell it explicitly instead of signed overflow.
        if end == -9223372036854775807 - 1:
            _buffer_cstr(state, cstr("9223372036854775807"))
        else:
            _unicode_format_decimal(state, end - 1)
    _buffer_cstr(state, cstr(": "))
    return _unicode_format_append(state, slots, 4, 0)


@c_abi_export("py_unicode_error_format")
def py_unicode_error_format(value, repr_mode: int):
    borrowed = stack_alloc(8)
    store_ptr(borrowed, 0, value)
    pcc_gc_frame_enter(global_addr("pcc_unicode_format_borrowed_map"), borrowed)
    slots = stack_alloc(64)
    memset(slots, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_format_owned_map"), slots)
    store_ptr(slots, 48, py_unicode_error_get_field(pcc_gc_load_ptr(null(), borrowed), 6))
    type_tag: int = py_int_value_i64(pcc_gc_load_ptr(null(), ptr_add(slots, 48)))
    pcc_gc_store_root(ptr_add(slots, 48), null())
    if repr_mode != 0:
        store_ptr(slots, 56, py_unicode_error_get_field(pcc_gc_load_ptr(null(), borrowed), 0))
    else:
        i: int = 0
        while i < 5:
            store_ptr(slots, i * 8, py_unicode_error_get_field(pcc_gc_load_ptr(null(), borrowed), i + 1))
            i = i + 1
    state = _buffer_new(128)
    if ptr_is_null(state) == 0:
        if _unicode_format_body(state, slots, repr_mode, type_tag) == 0:
            store_ptr(slots, 48, _buffer_string(state))
        _buffer_free(state)
    prior: int = _unicode_format_pin(ptr_add(slots, 48))
    i = 0
    while i < 8:
        if i != 6:
            pcc_gc_store_root(ptr_add(slots, i * 8), null())
        i = i + 1
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 48), prior)


py_os_error_get_field = extern("py_os_error_get_field", (c_ptr, c_int64), c_ptr)
py_os_error_fields_present = extern("py_os_error_fields_present", (c_ptr,), c_int64)


def _os_error_format_body(state, slots, repr_mode: int, present: int) -> int:
    args_slot = ptr_add(slots, 56)
    count: int = py_tuple_len(pcc_gc_load_ptr(null(), args_slot))
    if repr_mode != 0:
        if _unicode_format_append(state, slots, 4, 0) != 0:
            return -1
        _buffer_char(state, 40)
        i: int = 0
        while i < count:
            if i != 0:
                _buffer_cstr(state, cstr(", "))
            prior: int = _unicode_format_pin(args_slot)
            store_ptr(slots, 0, py_tuple_get(load_ptr(args_slot, 0), i))
            store_ptr(args_slot, 0, pcc_gc_take_pinned_slot(args_slot, prior))
            if ptr_is_null(load_ptr(slots, 0)) or _unicode_format_append(state, slots, 0, 1) != 0:
                return -1
            pcc_gc_store_root(slots, null())
            i = i + 1
        _buffer_char(state, 41)
        return 0
    has_name: int = present & 8
    structured: int = has_name
    if (present & 6) == 6:
        structured = 1
    if structured != 0:
        _buffer_cstr(state, cstr("[Errno "))
        if _unicode_format_append(state, slots, 0, 0) != 0:
            return -1
        _buffer_cstr(state, cstr("] "))
        if _unicode_format_append(state, slots, 1, 0) != 0:
            return -1
        if has_name != 0:
            _buffer_cstr(state, cstr(": "))
            if _unicode_format_append(state, slots, 2, 1) != 0:
                return -1
            if (present & 16) != 0:
                _buffer_cstr(state, cstr(" -> "))
                if _unicode_format_append(state, slots, 3, 1) != 0:
                    return -1
        return 0
    if count == 0:
        return 0
    if count > 1:
        return _unicode_format_append(state, slots, 7, 1)
    prior = _unicode_format_pin(args_slot)
    store_ptr(slots, 32, py_tuple_get(load_ptr(args_slot, 0), 0))
    store_ptr(args_slot, 0, pcc_gc_take_pinned_slot(args_slot, prior))
    return _unicode_format_append(state, slots, 4, 0)


@c_abi_export("py_os_error_format")
def py_os_error_format(value, repr_mode: int):
    borrowed = stack_alloc(8)
    store_ptr(borrowed, 0, value)
    pcc_gc_frame_enter(global_addr("pcc_unicode_format_borrowed_map"), borrowed)
    slots = stack_alloc(64)
    memset(slots, 0, 64)
    pcc_gc_frame_enter(global_addr("pcc_unicode_format_owned_map"), slots)
    present: int = py_os_error_fields_present(pcc_gc_load_ptr(null(), borrowed))
    store_ptr(slots, 56, py_os_error_get_field(pcc_gc_load_ptr(null(), borrowed), 0))
    if repr_mode != 0:
        pcc_py_gc_minor_graph_lock()
        error = pcc_gc_load_ptr(null(), borrowed)
        cls = pcc_gc_load_ptr(error, ptr_add(error, 16))
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        pcc_py_gc_minor_graph_unlock()
        # The class remains rooted through the exception; its C-string name
        # is separately allocated and stable even if the class itself moves.
        store_ptr(slots, 32, py_str_new(name, strlen(name)))
    else:
        i: int = 0
        while i < 4:
            store_ptr(slots, i * 8, py_os_error_get_field(pcc_gc_load_ptr(null(), borrowed), i + 1))
            i = i + 1
    state = _buffer_new(128)
    if ptr_is_null(state) == 0:
        if _os_error_format_body(state, slots, repr_mode, present) == 0:
            store_ptr(slots, 48, _buffer_string(state))
        _buffer_free(state)
    prior: int = _unicode_format_pin(ptr_add(slots, 48))
    i = 0
    while i < 8:
        if i != 6:
            pcc_gc_store_root(ptr_add(slots, i * 8), null())
        i = i + 1
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 48), prior)
