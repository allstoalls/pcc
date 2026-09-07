"""Phase 4c.2: pcc-Python replacement for py_runtime/src/py_obj_stubs.c.

Contains:
  py_float_*      : Phase 3 stubs (return NULL / 0.0)
  py_obj_repr     : Phase 3 stub (return NULL)
  py_obj_str      : real implementation — dispatches on type tag

Layout offsets (mirroring PyObjectHeader in py_internal.h):
    0  refcount (int64)
    8  type_tag (int32)
    12 flags    (int32)

Tagged ints use the generated ``PY_TYPE_INT`` semantic tag.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_COMPLEX,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
)

from pcc.extern import extern, c_abi_export, c_ptr, c_double, c_int32, c_int64, c_void
from pcc.unsafe import (
    atomic_load_i64,
    atomic_rmw_i64,
    define_global_i64_array,
    global_addr,
    global_load_ptr,
    cstr,
    float_to_i64,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_f64,
    load_ptr,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i8,
    store_i64,
    store_f64,
    store_ptr,
    untag_int,
)

define_global_i64_array("pcc_guarded_loop_counters", 0, 0, 0, 0, 0, 0)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_exc_get_message = extern("py_exc_get_message", (c_ptr,), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_add = extern("py_int_add", (c_ptr, c_ptr), c_ptr)
py_int_mul = extern("py_int_mul", (c_ptr, c_ptr), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_to_str_obj = extern("py_int_to_str_obj", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_user_str_dispatch = extern("py_user_str_dispatch", (c_ptr,), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_capi_cext_object_repr = extern("pcc_capi_cext_object_repr", (c_ptr,), c_ptr)
py_user_repr_dispatch = extern("py_user_repr_dispatch", (c_ptr,), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_isinstance = extern("py_isinstance", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_repr = extern("py_exc_repr", (c_ptr,), c_ptr)
py_complex_repr = extern("py_complex_repr", (c_ptr,), c_ptr)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_mem_alloc = extern("py_mem_alloc", (c_int64,), c_ptr)
py_mem_free = extern("py_mem_free", (c_ptr,), c_void)
py_bigint_to_double = extern("py_bigint_to_double", (c_ptr,), c_double)
py_float_repr_shortest = extern("py_float_repr_shortest", (c_ptr,), c_ptr)
pcc_float_round_fixed_f64 = extern(
    "pcc_float_round_fixed_f64", (c_double, c_int64), c_double
)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_note_relocation_read = extern("pcc_gc_note_relocation_read", (c_ptr,), c_ptr)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
hypot_c = extern("hypot", (c_double, c_double), c_double)
pow_c = extern("pow", (c_double, c_double), c_double)
atan2_c = extern("atan2", (c_double, c_double), c_double)
exp_c = extern("exp", (c_double,), c_double)
log_c = extern("log", (c_double,), c_double)
cos_c = extern("cos", (c_double,), c_double)
sin_c = extern("sin", (c_double,), c_double)
strtod_c = extern("strtod", (c_ptr, c_ptr), c_double)
scalbn_c = extern("scalbn", (c_double, c_int32), c_double)


def _type_of(obj) -> int:
    # Offsets and type-tag literals inlined to avoid module-level
    # runtime-initialized globals (which require a main() and conflict
    # with library linkage). See py_internal.h / PY_TYPE_* enum.
    if is_tagged_int(obj):
        return PY_TYPE_INT  # PY_TYPE_INT
    return load_i32(obj, 8)  # PyObjectHeader.type_tag


@c_abi_export("py_float_from_f64")
def py_float_from_f64(v: float):
    # PyFloatObject layout (24 bytes):
    #   0   refcount (i64)
    #   8   type_tag (i32) = PY_TYPE_FLOAT (3)
    #   12  flags    (i32)
    #   16  value    (f64)
    p = pcc_gc_alloc(24, PY_TYPE_FLOAT, 0)
    if ptr_is_null(p):
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_FLOAT)
    store_f64(p, 16, v)
    return p


@c_abi_export("py_float_to_f64")
def py_float_to_f64(o) -> float:
    if ptr_is_null(o):
        return 0.0
    if is_tagged_int(o):
        i: int = untag_int(o)
        return float(i)
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return load_f64(o, 16)
    if tag == PY_TYPE_INT:  # PY_TYPE_INT (bignum)
        return py_bigint_to_double(o)
    if tag == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        return float(load_i32(o, 16))
    return 0.0


@c_abi_export("py_float_is_integer")
def py_float_is_integer(o) -> int:
    # float.is_integer(): finite and no fractional part. Mirrors
    # py_float_is_integer in py_obj_stubs.c (avoids math.h: |v|>=2^53 is always
    # integral; otherwise fits int64 so the truncate round-trip is exact).
    v: float = py_float_to_f64(o)
    if v != v:  # nan
        return 0
    if v != 0.0 and v == v * 2.0:  # +/-inf
        return 0
    a: float = v
    if a < 0.0:
        a = 0.0 - a
    if a >= 9007199254740992.0:  # >= 2^53
        return 1
    iv: int = int(v)
    if v == float(iv):
        return 1
    return 0


@c_abi_export("py_float_add")
def py_float_add(a, b):
    # float + numeric -> float, matching CPython float.__add__/__radd__. This is
    # the generic-object add path used when either operand is a float (e.g. a
    # boxed result of true-division: ``obj.attr / n + m``). py_float_to_f64
    # coerces int / bool / float to a double; a non-numeric operand returns
    # null so the caller surfaces the error instead of a wrong number. Was an
    # unimplemented stub (TODO phase3) -> float arithmetic via py_obj_add
    # silently produced null in DEFAULT mode.
    at: int = _type_of(a)
    bt: int = _type_of(b)
    a_num: int = 0
    if at == PY_TYPE_BOOL or at == PY_TYPE_INT or at == PY_TYPE_FLOAT:
        a_num = 1
    b_num: int = 0
    if bt == PY_TYPE_BOOL or bt == PY_TYPE_INT or bt == PY_TYPE_FLOAT:
        b_num = 1
    if a_num == 1 and b_num == 1:
        return py_float_from_f64(py_float_to_f64(a) + py_float_to_f64(b))
    return null()


@c_abi_export("py_float_sub")
def py_float_sub(a, b):
    # float - numeric -> float (mirrors py_float_add). Non-numeric -> null.
    at: int = _type_of(a)
    bt: int = _type_of(b)
    a_num: int = 0
    if at == PY_TYPE_BOOL or at == PY_TYPE_INT or at == PY_TYPE_FLOAT:
        a_num = 1
    b_num: int = 0
    if bt == PY_TYPE_BOOL or bt == PY_TYPE_INT or bt == PY_TYPE_FLOAT:
        b_num = 1
    if a_num == 1 and b_num == 1:
        return py_float_from_f64(py_float_to_f64(a) - py_float_to_f64(b))
    return null()


@c_abi_export("py_float_mul")
def py_float_mul(a, b):
    # float * numeric -> float (mirrors py_float_add). Non-numeric -> null.
    at: int = _type_of(a)
    bt: int = _type_of(b)
    a_num: int = 0
    if at == PY_TYPE_BOOL or at == PY_TYPE_INT or at == PY_TYPE_FLOAT:
        a_num = 1
    b_num: int = 0
    if bt == PY_TYPE_BOOL or bt == PY_TYPE_INT or bt == PY_TYPE_FLOAT:
        b_num = 1
    if a_num == 1 and b_num == 1:
        return py_float_from_f64(py_float_to_f64(a) * py_float_to_f64(b))
    return null()


@c_abi_export("py_float_round_ndigits")
def py_float_round_ndigits(v: float, ndigits: int):
    return py_float_from_f64(pcc_float_round_fixed_f64(v, ndigits))


def _store_u64_decimal(buf, pos: int, value: int) -> int:
    if value == 0:
        store_i8(buf, pos, 48)
        return pos + 1
    tmp = py_mem_alloc(32)
    if ptr_is_null(tmp):
        return pos
    n: int = 0
    v: int = value
    while v > 0:
        digit: int = v % 10
        store_i8(tmp, n, 48 + digit)
        n = n + 1
        v = v // 10
    i: int = n - 1
    while i >= 0:
        store_i8(buf, pos, load_i8(tmp, i))
        pos = pos + 1
        i = i - 1
    py_mem_free(tmp)
    return pos


@c_abi_export("py_float_format_fixed")
def py_float_format_fixed(o, precision: int):
    if precision < 0:
        precision = 6
    if precision > 9:
        precision = 9
    v: float = py_float_to_f64(o)
    neg: int = 0
    if v < 0.0:
        neg = 1
        v = 0.0 - v
    scale: int = 1
    i: int = 0
    while i < precision:
        scale = scale * 10
        i = i + 1
    scaled: int = int(v * float(scale) + 0.5)
    whole: int = scaled // scale
    frac: int = scaled % scale
    buf = py_mem_alloc(96)
    if ptr_is_null(buf):
        return null()
    pos: int = 0
    if neg != 0:
        store_i8(buf, pos, 45)
        pos = pos + 1
    pos = _store_u64_decimal(buf, pos, whole)
    if precision > 0:
        store_i8(buf, pos, 46)
        pos = pos + 1
        div: int = scale // 10
        while div > 0:
            digit: int = (frac // div) % 10
            store_i8(buf, pos, 48 + digit)
            pos = pos + 1
            div = div // 10
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


@c_abi_export("py_complex_new")
def py_complex_new(real: float, imag: float):
    # PyComplexObject layout: header + real@16 + imag@24.
    p = pcc_gc_alloc(32, PY_TYPE_COMPLEX, 0)
    if ptr_is_null(p):
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_COMPLEX)  # PY_TYPE_COMPLEX
    store_f64(p, 16, real)
    store_f64(p, 24, imag)
    return p


def _complex_real_part(o) -> float:
    if ptr_is_null(o):
        return 0.0
    if is_tagged_int(o):
        i: int = untag_int(o)
        return float(i)
    tag: int = load_i32(o, 8)
    if tag == PY_TYPE_COMPLEX:
        return load_f64(o, 16)
    if tag == PY_TYPE_FLOAT:
        return load_f64(o, 16)
    if tag == PY_TYPE_INT:
        return py_bigint_to_double(o)
    if tag == PY_TYPE_BOOL:
        return float(load_i32(o, 16))
    return 0.0


def _complex_imag_part(o) -> float:
    if ptr_is_null(o):
        return 0.0
    if is_tagged_int(o):
        return 0.0
    if load_i32(o, 8) == PY_TYPE_COMPLEX:
        return load_f64(o, 24)
    return 0.0


@c_abi_export("py_complex_real")
def py_complex_real(o):
    return py_float_from_f64(_complex_real_part(o))


@c_abi_export("py_complex_imag")
def py_complex_imag(o):
    return py_float_from_f64(_complex_imag_part(o))


@c_abi_export("py_complex_add")
def py_complex_add(a, b):
    return py_complex_new(
        _complex_real_part(a) + _complex_real_part(b),
        _complex_imag_part(a) + _complex_imag_part(b),
    )


@c_abi_export("py_complex_pow")
def py_complex_pow(a, b):
    """Complex exponentiation matching CPython's small-int and polar paths."""
    base_real: float = _complex_real_part(a)
    base_imag: float = _complex_imag_part(a)
    exponent_real: float = _complex_real_part(b)
    exponent_imag: float = _complex_imag_part(b)

    if exponent_real == 0.0 and exponent_imag == 0.0:
        return py_complex_new(1.0, 0.0)

    if base_real == 0.0 and base_imag == 0.0:
        if exponent_imag != 0.0 or exponent_real < 0.0:
            error = py_exc_new(
                9, cstr("zero to a negative or complex power")
            )
            py_raise(error)
            if ptr_is_null(error) == 0:
                py_decref(error)
            return null()
        return py_complex_new(0.0, 0.0)

    exponent_abs: float = exponent_real
    if exponent_abs < 0.0:
        exponent_abs = 0.0 - exponent_abs
    if exponent_imag == 0.0 and exponent_abs <= 100.0:
        integer_exponent: int = float_to_i64(exponent_real)
        if float(integer_exponent) == exponent_real:
            positive_exponent: int = integer_exponent
            reciprocal: int = 0
            if positive_exponent < 0:
                positive_exponent = 0 - positive_exponent
                reciprocal = 1

            result_real: float = 1.0
            result_imag: float = 0.0
            power_real: float = base_real
            power_imag: float = base_imag
            mask: int = 1
            while mask > 0 and positive_exponent >= mask:
                if positive_exponent & mask:
                    next_real: float = (
                        result_real * power_real
                        - result_imag * power_imag
                    )
                    next_imag: float = (
                        result_real * power_imag
                        + result_imag * power_real
                    )
                    result_real = next_real
                    result_imag = next_imag
                mask = mask << 1
                square_real: float = (
                    power_real * power_real - power_imag * power_imag
                )
                square_imag: float = 2.0 * power_real * power_imag
                power_real = square_real
                power_imag = square_imag

            if reciprocal != 0:
                abs_real: float = result_real
                if abs_real < 0.0:
                    abs_real = 0.0 - abs_real
                abs_imag: float = result_imag
                if abs_imag < 0.0:
                    abs_imag = 0.0 - abs_imag
                quotient_real: float = 0.0
                quotient_imag: float = 0.0
                if abs_real >= abs_imag:
                    if abs_real != 0.0:
                        ratio: float = result_imag / result_real
                        denominator: float = (
                            result_real + result_imag * ratio
                        )
                        quotient_real = 1.0 / denominator
                        quotient_imag = (0.0 - ratio) / denominator
                else:
                    ratio2: float = result_real / result_imag
                    denominator2: float = (
                        result_real * ratio2 + result_imag
                    )
                    quotient_real = ratio2 / denominator2
                    quotient_imag = -1.0 / denominator2
                result_real = quotient_real
                result_imag = quotient_imag
            return py_complex_new(result_real, result_imag)

    magnitude: float = hypot_c(base_real, base_imag)
    length: float = pow_c(magnitude, exponent_real)
    angle: float = atan2_c(base_imag, base_real)
    phase: float = angle * exponent_real
    if exponent_imag != 0.0:
        length = length / exp_c(angle * exponent_imag)
        phase = phase + exponent_imag * log_c(magnitude)
    return py_complex_new(length * cos_c(phase), length * sin_c(phase))


def _fromhex_is_space(value: int) -> int:
    if value == 32 or value == 9 or value == 10:
        return 1
    if value == 13 or value == 11 or value == 12:
        return 1
    return 0


def _fromhex_digit(value: int) -> int:
    if value >= 48 and value <= 57:
        return value - 48
    if value >= 97 and value <= 102:
        return value - 97 + 10
    if value >= 65 and value <= 70:
        return value - 65 + 10
    return -1


def _fromhex_ascii_lower(value: int) -> int:
    if value >= 65 and value <= 90:
        return value - 65 + 97
    return value


def _fromhex_matches(data, pos: int, length: int, word, word_len: int) -> int:
    if pos + word_len > length:
        return 0
    i: int = 0
    while i < word_len:
        if _fromhex_ascii_lower(load_i8(data, pos + i)) != load_i8(word, i):
            return 0
        i = i + 1
    return 1


def _fromhex_raise(kind: int, message) -> None:
    error = py_exc_new(kind, message)
    py_raise(error)
    if ptr_is_null(error) == 0:
        py_decref(error)


@c_abi_export("py_float_fromhex")
def py_float_fromhex(text):
    """Parse CPython-compatible hexadecimal float syntax without libpython."""
    if ptr_is_null(text) != 0 or is_tagged_int(text) or _type_of(text) != PY_TYPE_STR:
        _fromhex_raise(3, cstr("float.fromhex() argument must be str"))
        return null()
    data = py_str_utf8(text)
    if ptr_is_null(data) != 0:
        _fromhex_raise(2, cstr("invalid hexadecimal floating-point string"))
        return null()
    length: int = py_str_byte_len(text)

    pos: int = 0
    while pos < length and _fromhex_is_space(load_i8(data, pos)) != 0:
        pos = pos + 1
    sign: float = 1.0
    if pos < length and load_i8(data, pos) == 43:
        pos = pos + 1
    elif pos < length and load_i8(data, pos) == 45:
        sign = -1.0
        pos = pos + 1

    special_len: int = 0
    special_kind: int = 0
    if _fromhex_matches(data, pos, length, cstr("infinity"), 8) != 0:
        special_len = 8
        special_kind = 1
    elif _fromhex_matches(data, pos, length, cstr("inf"), 3) != 0:
        special_len = 3
        special_kind = 1
    elif _fromhex_matches(data, pos, length, cstr("nan"), 3) != 0:
        special_len = 3
        special_kind = 2
    if special_len > 0:
        tail: int = pos + special_len
        while tail < length and _fromhex_is_space(load_i8(data, tail)) != 0:
            tail = tail + 1
        if tail != length:
            _fromhex_raise(2, cstr("invalid hexadecimal floating-point string"))
            return null()
        end_slot = stack_alloc(8)
        store_ptr(end_slot, 0, null())
        special_value: float = 0.0
        if special_kind == 1:
            special_value = strtod_c(cstr("inf"), end_slot)
            special_value = sign * special_value
        else:
            special_value = strtod_c(cstr("nan"), end_slot)
        return py_float_from_f64(special_value)

    if pos + 1 < length and load_i8(data, pos) == 48:
        marker: int = load_i8(data, pos + 1)
        if marker == 120 or marker == 88:
            pos = pos + 2
    coefficient_start: int = pos
    integer_digits: int = 0
    while pos < length and _fromhex_digit(load_i8(data, pos)) >= 0:
        integer_digits = integer_digits + 1
        pos = pos + 1
    coefficient_dot: int = pos
    fraction_digits: int = 0
    had_dot: int = 0
    if pos < length and load_i8(data, pos) == 46:
        had_dot = 1
        pos = pos + 1
        while pos < length and _fromhex_digit(load_i8(data, pos)) >= 0:
            fraction_digits = fraction_digits + 1
            pos = pos + 1
    coefficient_end: int = pos
    total_digits: int = integer_digits + fraction_digits
    if total_digits == 0:
        _fromhex_raise(2, cstr("invalid hexadecimal floating-point string"))
        return null()

    exponent: int = 0
    if pos < length:
        exponent_marker: int = load_i8(data, pos)
        if exponent_marker == 112 or exponent_marker == 80:
            pos = pos + 1
            exponent_sign: int = 1
            if pos < length and load_i8(data, pos) == 43:
                pos = pos + 1
            elif pos < length and load_i8(data, pos) == 45:
                exponent_sign = -1
                pos = pos + 1
            if pos >= length:
                _fromhex_raise(
                    2, cstr("invalid hexadecimal floating-point string")
                )
                return null()
            first_exponent_digit: int = load_i8(data, pos)
            if first_exponent_digit < 48 or first_exponent_digit > 57:
                _fromhex_raise(
                    2, cstr("invalid hexadecimal floating-point string")
                )
                return null()
            exponent_value: int = 0
            exponent_overflow: int = 0
            while pos < length:
                digit_byte: int = load_i8(data, pos)
                if digit_byte < 48 or digit_byte > 57:
                    break
                if exponent_value < 100000000:
                    exponent_value = exponent_value * 10 + digit_byte - 48
                else:
                    exponent_overflow = 1
                pos = pos + 1
            if exponent_overflow != 0:
                if exponent_sign < 0:
                    exponent = -1000000000
                else:
                    exponent = 1000000000
            else:
                exponent = exponent_sign * exponent_value

    while pos < length and _fromhex_is_space(load_i8(data, pos)) != 0:
        pos = pos + 1
    if pos != length:
        _fromhex_raise(2, cstr("invalid hexadecimal floating-point string"))
        return null()

    # Locate the first nonzero hexadecimal digit.  Only its leading-bit
    # width plus the number of remaining nibbles is needed to determine the
    # unbiased binary exponent; arbitrarily long tails are reduced to one
    # sticky bit below.
    first_digit_ordinal: int = -1
    first_digit_value: int = 0
    digit_ordinal: int = 0
    source: int = coefficient_start
    while source < coefficient_end:
        source_byte: int = load_i8(data, source)
        if source_byte != 46:
            source_digit: int = _fromhex_digit(source_byte)
            if first_digit_ordinal < 0 and source_digit != 0:
                first_digit_ordinal = digit_ordinal
                first_digit_value = source_digit
            digit_ordinal = digit_ordinal + 1
        source = source + 1
    if first_digit_ordinal < 0:
        return py_float_from_f64(sign * 0.0)

    first_digit_bits: int = 1
    if first_digit_value >= 8:
        first_digit_bits = 4
    elif first_digit_value >= 4:
        first_digit_bits = 3
    elif first_digit_value >= 2:
        first_digit_bits = 2
    significant_bits: int = (
        first_digit_bits
        + (total_digits - first_digit_ordinal - 1) * 4
    )
    top_exponent: int = (
        significant_bits - 1 + exponent - fraction_digits * 4
    )
    if top_exponent > 1023:
        _fromhex_raise(
            15, cstr("hexadecimal value too large to represent as a float")
        )
        return null()
    if top_exponent < -1075:
        return py_float_from_f64(sign * 0.0)

    precision: int = 53
    if top_exponent < -1022:
        precision = top_exponent + 1075
    keep_bits: int = precision + 1
    accumulated: int = 0
    collected: int = 0
    sticky: int = 0
    started: int = 0
    source = coefficient_start
    while source < coefficient_end:
        source_byte = load_i8(data, source)
        if source_byte != 46:
            source_digit = _fromhex_digit(source_byte)
            bit_index: int = 3
            while bit_index >= 0:
                bit: int = (source_digit >> bit_index) & 1
                if started == 0:
                    if bit != 0:
                        started = 1
                    else:
                        bit_index = bit_index - 1
                        continue
                if collected < keep_bits:
                    accumulated = (accumulated << 1) | bit
                    collected = collected + 1
                elif bit != 0:
                    sticky = 1
                bit_index = bit_index - 1
        source = source + 1

    significand: int = accumulated
    if significant_bits <= precision:
        significand = significand << (precision - significant_bits)
    else:
        guard: int = significand & 1
        significand = significand >> 1
        if guard != 0 and (sticky != 0 or (significand & 1) != 0):
            significand = significand + 1
        if precision > 0 and significand == (1 << precision):
            significand = significand >> 1
            top_exponent = top_exponent + 1
            if top_exponent > 1023:
                _fromhex_raise(
                    15,
                    cstr("hexadecimal value too large to represent as a float"),
                )
                return null()
    if significand == 0:
        return py_float_from_f64(sign * 0.0)
    scale_exponent: int = top_exponent - (precision - 1)
    parsed: float = scalbn_c(float(significand), scale_exponent)
    return py_float_from_f64(sign * parsed)


@c_abi_export("py_bytes_new")
def py_bytes_new(data, byte_len: int):
    if byte_len < 0:
        byte_len = 0
    p = pcc_gc_alloc(24 + byte_len + 1, PY_TYPE_BYTES, 0)
    if ptr_is_null(p):
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_BYTES)  # PY_TYPE_BYTES
    store_i64(p, 16, byte_len)
    if not ptr_is_null(data):
        i: int = 0
        while i < byte_len:
            store_i8(p, 24 + i, load_i8(data, i))
            i = i + 1
    store_i8(p, 24 + byte_len, 0)
    return p


def _bytearray_new_raw(data, byte_len: int):
    if byte_len < 0:
        byte_len = 0
    p = pcc_gc_alloc(24 + byte_len + 1, PY_TYPE_BYTEARRAY, 0)
    if ptr_is_null(p):
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_BYTEARRAY)  # PY_TYPE_BYTEARRAY
    store_i64(p, 16, byte_len)
    if not ptr_is_null(data):
        i: int = 0
        while i < byte_len:
            store_i8(p, 24 + i, load_i8(data, i))
            i = i + 1
    store_i8(p, 24 + byte_len, 0)
    return p


def _bytes_data(obj):
    tag: int = _type_of(obj)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return ptr_add(obj, 24)
    if tag == PY_TYPE_MEMORYVIEW:
        base = pcc_gc_load_ptr(obj, ptr_add(obj, 16))
        return _bytes_data(base)
    return null()


@c_abi_export("py_bytes_hex")
def py_bytes_hex(o):
    # bytes.hex(): lowercase two-hex-digits-per-byte string. Mirrors
    # py_bytes_hex in py_bytes.c. Frontend routes only bytes/bytearray here.
    if ptr_is_null(o) != 0:
        return null()
    n: int = load_i64(o, 16)
    data = _bytes_data(o)
    buf = py_mem_alloc(n * 2 + 1)
    if ptr_is_null(buf) != 0:
        return null()
    i: int = 0
    pos: int = 0
    while i < n:
        c: int = load_i8(data, i) & 0xFF
        hi: int = (c >> 4) & 0xF
        lo: int = c & 0xF
        if hi < 10:
            store_i8(buf, pos, 48 + hi)
        else:
            store_i8(buf, pos, 87 + hi)  # 'a'-10 == 87
        if lo < 10:
            store_i8(buf, pos + 1, 48 + lo)
        else:
            store_i8(buf, pos + 1, 87 + lo)
        pos = pos + 2
        i = i + 1
    out = py_str_new(buf, n * 2)
    py_mem_free(buf)
    return out


@c_abi_export("py_bytes_upper")
def py_bytes_upper(o):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    out = _bytes_new_same_family(o, null(), n)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if c >= 97 and c <= 122:
            c = c - 32
        store_i8(dst, i, c)
        i = i + 1
    store_i8(dst, n, 0)
    return out


# Mirror of py_bytes_lower / py_bytes_strip in src/py_bytes.c (cc tier). This is
# the pcc-Python port tier (default no-libpython); both must stay in sync.
def _is_ascii_space(c: int) -> int:
    if c == 32 or c == 9 or c == 10:
        return 1
    if c == 13 or c == 11 or c == 12:
        return 1
    return 0


@c_abi_export("py_bytes_lower")
def py_bytes_lower(o):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    out = _bytes_new_same_family(o, null(), n)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if c >= 65 and c <= 90:  # A-Z -> a-z
            c = c + 32
        store_i8(dst, i, c)
        i = i + 1
    store_i8(dst, n, 0)
    return out


@c_abi_export("py_bytes_strip")
def py_bytes_strip(o):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    lo: int = 0
    hi: int = n
    while lo < hi and _is_ascii_space(load_i8(data, lo) & 255) != 0:
        lo = lo + 1
    while hi > lo and _is_ascii_space(load_i8(data, hi - 1) & 255) != 0:
        hi = hi - 1
    return _bytes_new_same_family(o, ptr_add(data, lo), hi - lo)


def _byte_from_obj(obj) -> int:
    if ptr_is_null(obj):
        return -1
    if is_tagged_int(obj):
        return untag_int(obj)
    tag: int = _type_of(obj)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(obj, global_load_ptr("py_True")) != 0:
            return 1
        if ptr_eq(obj, global_load_ptr("py_False")) != 0:
            return 0
        return -1
    if tag == PY_TYPE_INT:
        return py_int_value_i64(obj)
    return -1


def _bytes_from_int_sequence(seq, as_bytearray: int):
    tag: int = _type_of(seq)
    if tag == PY_TYPE_LIST:
        n: int = py_list_len(seq)
    elif tag == PY_TYPE_TUPLE:
        n: int = py_tuple_len(seq)
    else:
        return null()
    if n <= 0:
        return (
            _bytearray_new_raw(null(), 0) if as_bytearray else py_bytes_new(null(), 0)
        )
    tmp = py_mem_alloc(n)
    if ptr_is_null(tmp):
        return null()
    i: int = 0
    while i < n:
        if tag == PY_TYPE_LIST:
            item = py_list_get(seq, i)
        else:
            item = py_tuple_get(seq, i)
        if ptr_is_null(item):
            py_mem_free(tmp)
            return null()
        byte: int = _byte_from_obj(item)
        py_decref(item)
        if byte < 0 or byte > 255:
            py_mem_free(tmp)
            return null()
        store_i8(tmp, i, byte)
        i = i + 1
    out = _bytearray_new_raw(tmp, n) if as_bytearray != 0 else py_bytes_new(tmp, n)
    py_mem_free(tmp)
    return out


def _bytes_is_none_or_null(obj) -> int:
    if ptr_is_null(obj):
        return 1
    if ptr_eq(obj, global_load_ptr("py_None")) != 0:
        return 1
    return 0


def _bytes_slice_count(lo: int, hi: int, step: int) -> int:
    count: int = 0
    if step > 0:
        i: int = lo
        while i < hi:
            count = count + 1
            i = i + step
    else:
        i2: int = lo
        while i2 > hi:
            count = count + 1
            i2 = i2 + step
    return count


def _bytes_slice_lo(obj, length: int, step: int) -> int:
    if _bytes_is_none_or_null(obj) != 0:
        if step > 0:
            return 0
        return length - 1
    return py_int_value_i64(obj)


def _bytes_slice_hi(obj, length: int, step: int) -> int:
    if _bytes_is_none_or_null(obj) != 0:
        if step > 0:
            return length
        return -1
    return py_int_value_i64(obj)


def _bytes_normalize_lo(lo: int, length: int, step: int) -> int:
    result: int = lo
    if step > 0:
        if result < 0:
            result = result + length
            if result < 0:
                result = 0
        if result > length:
            result = length
    else:
        if result < 0:
            result = result + length
            if result < 0:
                result = -1
        if result >= length:
            result = length - 1
    return result


def _bytes_normalize_hi(hi_obj, hi: int, length: int, step: int) -> int:
    result: int = hi
    if step > 0:
        if result < 0:
            result = result + length
            if result < 0:
                result = 0
        if result > length:
            result = length
    else:
        if result < 0:
            if _bytes_is_none_or_null(hi_obj) != 0:
                result = -1
            else:
                result = result + length
                if result < 0:
                    result = -1
        if result >= length:
            result = length - 1
    return result


def _bytes_new_same_family(src, data, byte_len: int):
    if _type_of(src) == PY_TYPE_BYTEARRAY:
        return _bytearray_new_raw(data, byte_len)
    return py_bytes_new(data, byte_len)


@c_abi_export("py_bytes_len")
def py_bytes_len(o) -> int:
    tag: int = _type_of(o)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
        return load_i64(o, 16)
    if tag == PY_TYPE_MEMORYVIEW:
        return py_bytes_len(load_ptr(o, 16))
    return 0


@c_abi_export("py_guarded_loop_counter_add")
def py_guarded_loop_counter_add(counter: int, delta: int) -> int:
    if counter < 0 or counter >= 6:
        return -1
    slot = ptr_add(global_addr("pcc_guarded_loop_counters"), counter * 8)
    return atomic_rmw_i64("add", slot, 0, delta, "relaxed") + delta


@c_abi_export("py_guarded_loop_counter_get")
def py_guarded_loop_counter_get(counter: int) -> int:
    if counter < 0 or counter >= 6:
        return -1
    slot = ptr_add(global_addr("pcc_guarded_loop_counters"), counter * 8)
    return atomic_load_i64(slot, 0, "relaxed")


@c_abi_export("py_i64_buffer_new")
def py_i64_buffer_new(element_count: int):
    if element_count < 1 or element_count > 1048576:
        _raise_int_bytes(
            2,
            cstr("pcc.i64_buffer length must be between 1 and 1048576"),
        )
        return null()
    byte_len: int = element_count * 8
    out = py_bytes_new(null(), byte_len)
    if ptr_is_null(out):
        _raise_int_bytes(19, cstr("unable to allocate pcc.i64_buffer"))
        return null()
    data = _bytes_data(out)
    i: int = 0
    while i < byte_len:
        store_i8(data, i, 0)
        i = i + 1
    return out


@c_abi_export("py_i64_buffer_set_item")
def py_i64_buffer_set_item(buffer, index: int, value) -> int:
    if _type_of(buffer) != PY_TYPE_BYTES:
        _raise_int_bytes(3, cstr("signed-i64 buffer must be exact bytes"))
        return -1
    byte_len: int = py_bytes_len(buffer)
    if (byte_len & 7) != 0:
        _raise_int_bytes(
            2,
            cstr("signed-i64 buffer byte length must be divisible by 8"),
        )
        return -1
    count: int = byte_len // 8
    if index < 0 or index >= count:
        _raise_int_bytes(
            5,
            cstr("pcc.i64_buffer assignment index out of range"),
        )
        return -1
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    integer: int = py_int_to_i64(value, overflow)
    if load_i32(overflow, 0) != 0:
        _raise_int_bytes(
            15,
            cstr("pcc.i64_buffer element does not fit signed i64"),
        )
        return -1
    data = _bytes_data(buffer)
    shift: int = 0
    while shift < 8:
        store_i8(data, index * 8 + shift, (integer >> (shift * 8)) & 255)
        shift = shift + 1
    return 0


@c_abi_export("py_i64_buffer_get_item")
def py_i64_buffer_get_item(buffer, index: int):
    tag: int = _type_of(buffer)
    if tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY and tag != PY_TYPE_MEMORYVIEW:
        _raise_int_bytes(3, cstr("signed-i64 buffer must be bytes-like"))
        return null()
    byte_len: int = py_bytes_len(buffer)
    if (byte_len & 7) != 0:
        _raise_int_bytes(
            2,
            cstr("signed-i64 buffer byte length must be divisible by 8"),
        )
        return null()
    count: int = byte_len // 8
    if index < 0 or index >= count:
        _raise_int_bytes(5, cstr("pcc.i64_buffer index out of range"))
        return null()
    data = _bytes_data(buffer)
    value: int = load_i8(data, index * 8 + 7) & 255
    if value >= 128:
        value = value - 256
    part: int = 6
    while part >= 0:
        value = value * 256 + (load_i8(data, index * 8 + part) & 255)
        part = part - 1
    return py_int_from_i64(value)


@c_abi_export("py_i64_buffer_data")
def py_i64_buffer_data(buffer):
    if _type_of(buffer) != PY_TYPE_BYTES or (py_bytes_len(buffer) & 7) != 0:
        return null()
    return _bytes_data(buffer)


@c_abi_export("py_i64_buffer_layout_version")
def py_i64_buffer_layout_version(buffer) -> int:
    if ptr_is_null(py_i64_buffer_data(buffer)):
        return 0
    return 1


@c_abi_export("py_i64_buffer_version")
def py_i64_buffer_version(buffer) -> int:
    if ptr_is_null(py_i64_buffer_data(buffer)):
        return 0
    return 1


@c_abi_export("py_i64_buffer_dot_scalar")
def py_i64_buffer_dot_scalar(left, right, expected_count: int):
    left_tag: int = _type_of(left)
    right_tag: int = _type_of(right)
    if (
        (left_tag != PY_TYPE_BYTES and left_tag != PY_TYPE_BYTEARRAY and left_tag != PY_TYPE_MEMORYVIEW)
        or (right_tag != PY_TYPE_BYTES and right_tag != PY_TYPE_BYTEARRAY and right_tag != PY_TYPE_MEMORYVIEW)
    ):
        _raise_int_bytes(3, cstr("signed-i64 buffer must be bytes-like"))
        return null()
    left_bytes: int = py_bytes_len(left)
    right_bytes: int = py_bytes_len(right)
    if (left_bytes & 7) != 0 or (right_bytes & 7) != 0:
        _raise_int_bytes(
            2,
            cstr("signed-i64 buffer byte length must be divisible by 8"),
        )
        return null()
    if left_bytes // 8 != expected_count or right_bytes // 8 != expected_count:
        _raise_int_bytes(
            2,
            cstr("guarded_i64_dot buffers must match the declared length"),
        )
        return null()
    accumulator = py_int_from_i64(0)
    if ptr_is_null(accumulator):
        return null()
    index: int = 0
    while index < expected_count:
        left_value = py_i64_buffer_get_item(left, index)
        if ptr_is_null(left_value):
            py_decref(accumulator)
            return null()
        right_value = py_i64_buffer_get_item(right, index)
        if ptr_is_null(right_value):
            py_decref(left_value)
            py_decref(accumulator)
            return null()
        product = py_int_mul(left_value, right_value)
        py_decref(left_value)
        py_decref(right_value)
        if ptr_is_null(product):
            py_decref(accumulator)
            return null()
        updated = py_int_add(accumulator, product)
        py_decref(product)
        py_decref(accumulator)
        if ptr_is_null(updated):
            return null()
        accumulator = updated
        index = index + 1
    return accumulator


@c_abi_export("py_bytes_find")
def py_bytes_find(src, needle) -> int:
    data = _bytes_data(src)
    if ptr_is_null(data):
        return -1
    n: int = py_bytes_len(src)

    byte: int = _byte_from_obj(needle)
    if byte >= 0:
        if byte > 255:
            return -1
        i: int = 0
        while i < n:
            if (load_i8(data, i) & 255) == byte:
                return i
            i = i + 1
        return -1

    needle_data = _bytes_data(needle)
    if ptr_is_null(needle_data):
        return -1
    needle_n: int = py_bytes_len(needle)
    if needle_n == 0:
        return 0
    if needle_n > n:
        return -1
    last: int = n - needle_n
    i = 0
    while i <= last:
        if load_i8(data, i) == load_i8(needle_data, 0):
            same: int = 1
            j: int = 0
            while j < needle_n:
                if load_i8(data, i + j) != load_i8(needle_data, j):
                    same = 0
                    break
                j = j + 1
            if same != 0:
                return i
        i = i + 1
    return -1


@c_abi_export("py_bytes_rfind")
def py_bytes_rfind(src, needle) -> int:
    # Mirror of py_bytes_rfind in src/py_bytes.c: highest match index, backward.
    data = _bytes_data(src)
    if ptr_is_null(data):
        return -1
    n: int = py_bytes_len(src)

    byte: int = _byte_from_obj(needle)
    if byte >= 0:
        if byte > 255:
            return -1
        i: int = n - 1
        while i >= 0:
            if (load_i8(data, i) & 255) == byte:
                return i
            i = i - 1
        return -1

    needle_data = _bytes_data(needle)
    if ptr_is_null(needle_data):
        return -1
    needle_n: int = py_bytes_len(needle)
    if needle_n == 0:
        return n
    if needle_n > n:
        return -1
    i = n - needle_n
    while i >= 0:
        if load_i8(data, i) == load_i8(needle_data, 0):
            same: int = 1
            j: int = 0
            while j < needle_n:
                if load_i8(data, i + j) != load_i8(needle_data, j):
                    same = 0
                    break
                j = j + 1
            if same != 0:
                return i
        i = i - 1
    return -1


@c_abi_export("py_bytes_count")
def py_bytes_count(src, needle) -> int:
    # Mirror of py_bytes_count in src/py_bytes.c: number of non-overlapping
    # occurrences of the sub-bytes (or single byte value). Empty sub-bytes
    # counts len+1 positions; a match advances past the whole needle. Returns
    # 0 on a bad receiver / out-of-range byte, matching find/rfind's
    # non-raising style so the frontend needs no py_err_occurred() check.
    data = _bytes_data(src)
    if ptr_is_null(data):
        return 0
    n: int = py_bytes_len(src)

    byte: int = _byte_from_obj(needle)
    if byte >= 0:
        if byte > 255:
            return 0
        count: int = 0
        i: int = 0
        while i < n:
            if (load_i8(data, i) & 255) == byte:
                count = count + 1
            i = i + 1
        return count

    needle_data = _bytes_data(needle)
    if ptr_is_null(needle_data):
        return 0
    needle_n: int = py_bytes_len(needle)
    if needle_n == 0:
        return n + 1
    if needle_n > n:
        return 0
    count = 0
    last: int = n - needle_n
    i = 0
    while i <= last:
        if load_i8(data, i) == load_i8(needle_data, 0):
            same: int = 1
            j: int = 0
            while j < needle_n:
                if load_i8(data, i + j) != load_i8(needle_data, j):
                    same = 0
                    break
                j = j + 1
            if same != 0:
                count = count + 1
                i = i + needle_n
            else:
                i = i + 1
        else:
            i = i + 1
    return count


@c_abi_export("py_bytes_split")
def py_bytes_split(src, sep):
    # Mirror of py_bytes_split in src/py_bytes.c: same-family pieces between each
    # occurrence of the non-empty separator. py_list_append increfs, so each
    # owned part is decref'd after appending.
    data = _bytes_data(src)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(src)
    sep_data = _bytes_data(sep)
    if ptr_is_null(sep_data):
        return null()
    sep_n: int = py_bytes_len(sep)
    if sep_n == 0:
        py_raise_owned(py_exc_new(2, cstr("empty separator")))  # PY_EXC_VALUEERROR
        return null()
    out = py_list_new(4)
    if ptr_is_null(out):
        return null()
    start: int = 0
    i: int = 0
    while i + sep_n <= n:
        match: int = 0
        if load_i8(data, i) == load_i8(sep_data, 0):
            match = 1
            j: int = 0
            while j < sep_n:
                if load_i8(data, i + j) != load_i8(sep_data, j):
                    match = 0
                    break
                j = j + 1
        if match != 0:
            part = _bytes_new_same_family(src, ptr_add(data, start), i - start)
            py_list_append(out, part)
            py_decref(part)
            i = i + sep_n
            start = i
        else:
            i = i + 1
    tail = _bytes_new_same_family(src, ptr_add(data, start), n - start)
    py_list_append(out, tail)
    py_decref(tail)
    return out


@c_abi_export("py_bytes_partition")
def py_bytes_partition(src, sep):
    # Mirror of py_bytes_partition in src/py_bytes.c: (before, sep, after) on the
    # first occurrence, else (copy-of-whole, b'', b''). set_item increfs -> decref.
    data = _bytes_data(src)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(src)
    sep_data = _bytes_data(sep)
    if ptr_is_null(sep_data):
        return null()
    sep_n: int = py_bytes_len(sep)
    found: int = -1
    if sep_n > 0:
        i: int = 0
        while i + sep_n <= n:
            match: int = 0
            if load_i8(data, i) == load_i8(sep_data, 0):
                match = 1
                j: int = 0
                while j < sep_n:
                    if load_i8(data, i + j) != load_i8(sep_data, j):
                        match = 0
                        break
                    j = j + 1
            if match != 0:
                found = i
                break
            i = i + 1
    t = py_tuple_new(3)
    if ptr_is_null(t):
        return null()
    if found < 0:
        whole = _bytes_new_same_family(src, data, n)
        e1 = _bytes_new_same_family(src, null(), 0)
        e2 = _bytes_new_same_family(src, null(), 0)
        py_tuple_set_item(t, 0, whole)
        py_decref(whole)
        py_tuple_set_item(t, 1, e1)
        py_decref(e1)
        py_tuple_set_item(t, 2, e2)
        py_decref(e2)
    else:
        before = _bytes_new_same_family(src, data, found)
        mid = _bytes_new_same_family(src, ptr_add(data, found), sep_n)
        after = _bytes_new_same_family(
            src, ptr_add(data, found + sep_n), n - found - sep_n
        )
        py_tuple_set_item(t, 0, before)
        py_decref(before)
        py_tuple_set_item(t, 1, mid)
        py_decref(mid)
        py_tuple_set_item(t, 2, after)
        py_decref(after)
    return t


def _bytes_from_integer_count(o, mutable: int):
    count: int = 0
    if _type_of(o) == PY_TYPE_BOOL:
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            count = 1
    else:
        overflow = stack_alloc(4)
        store_i32(overflow, 0, 0)
        count = py_int_to_i64(o, overflow)
        if load_i32(overflow, 0) != 0:
            py_raise_owned(py_exc_new(15, cstr("byte count does not fit in an index-sized integer")))
            return null()
    if count < 0:
        py_raise_owned(py_exc_new(2, cstr("negative count")))
        return null()
    # Both byte layouts have a 24-byte header plus one trailing NUL. Do not
    # let allocation-size arithmetic wrap before entering the allocator.
    if count > 9223372036854775782:
        error: int = 15
        if mutable != 0:
            error = 19
        py_raise_owned(py_exc_new(error, cstr("byte count is too large")))
        return null()
    result = null()
    if mutable != 0:
        result = _bytearray_new_raw(null(), count)
    else:
        result = py_bytes_new(null(), count)
    if ptr_is_null(result) != 0:
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("byte allocation failed")))
        return null()
    if count > 0:
        memset(ptr_add(result, 24), 0, count)
    return result


@c_abi_export("py_bytearray_from_obj")
def py_bytearray_from_obj(o):
    if ptr_is_null(o):
        return _bytearray_new_raw(null(), 0)
    if _type_of(o) == PY_TYPE_INT or _type_of(o) == PY_TYPE_BOOL:
        return _bytes_from_integer_count(o, 1)
    if _type_of(o) == PY_TYPE_LIST or _type_of(o) == PY_TYPE_TUPLE:
        out = _bytes_from_int_sequence(o, 1)
        if not ptr_is_null(out):
            return out
    return _bytearray_new_raw(_bytes_data(o), py_bytes_len(o))


@c_abi_export("py_bytes_from_obj")
def py_bytes_from_obj(o):
    if ptr_is_null(o):
        return py_bytes_new(null(), 0)
    if _type_of(o) == PY_TYPE_INT or _type_of(o) == PY_TYPE_BOOL:
        return _bytes_from_integer_count(o, 0)
    if _type_of(o) == PY_TYPE_LIST or _type_of(o) == PY_TYPE_TUPLE:
        out = _bytes_from_int_sequence(o, 0)
        if not ptr_is_null(out):
            return out
    return py_bytes_new(_bytes_data(o), py_bytes_len(o))


@c_abi_export("py_bytearray_extend")
def py_bytearray_extend(o, iterable):
    if ptr_is_null(o) != 0 or _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("bytearray.extend target must be bytearray")))
        return null()
    if ptr_is_null(iterable) != 0:
        py_raise_owned(py_exc_new(3, cstr("bytearray.extend argument must be iterable")))
        return null()

    an: int = py_bytes_len(o)
    ad = _bytes_data(o)
    bn: int = 0
    bd = _bytes_data(iterable)
    tmp = null()
    if ptr_is_null(bd) != 0:
        tag: int = _type_of(iterable)
        if tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE:
            tmp = _bytes_from_int_sequence(iterable, 1)
            if ptr_is_null(tmp) == 0:
                bd = _bytes_data(tmp)
                bn = py_bytes_len(tmp)
        if ptr_is_null(bd) != 0:
            if ptr_is_null(tmp) == 0:
                py_decref(tmp)
            py_raise_owned(
                py_exc_new(
                    3,
                    cstr(
                        "bytearray.extend argument must be bytes-like or an int sequence"
                    ),
                )
            )
            return null()
    else:
        bn = py_bytes_len(iterable)

    if ptr_is_null(ad) != 0 or an < 0 or bn < 0:
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        py_raise_owned(
            py_exc_new(
                3,
                cstr("bytearray.extend argument must be bytes-like or an int sequence"),
            )
        )
        return null()
    if an > 9223372036854775807 - bn:
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        py_raise_owned(py_exc_new(15, cstr("bytearray too large")))
        return null()

    total: int = an + bn
    out = _bytearray_new_raw(null(), total)
    if ptr_is_null(out) != 0:
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst) != 0:
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        return null()
    i: int = 0
    while i < an:
        store_i8(dst, i, load_i8(ad, i))
        i = i + 1
    j: int = 0
    while j < bn:
        store_i8(dst, an + j, load_i8(bd, j))
        j = j + 1
    store_i8(dst, total, 0)
    if ptr_is_null(tmp) == 0:
        py_decref(tmp)
    return out


@c_abi_export("py_bytearray_append")
def py_bytearray_append(o, item):
    if ptr_is_null(o) != 0 or _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("bytearray.append target must be bytearray")))
        return null()
    if ptr_is_null(item) != 0 or _type_of(item) != PY_TYPE_INT:
        py_raise_owned(
            py_exc_new(3, cstr("'object' object cannot be interpreted as an integer"))
        )
        return null()
    byte: int = py_int_value_i64(item)
    if byte < 0 or byte > 255:
        py_raise_owned(py_exc_new(2, cstr("byte must be in range(0, 256)")))
        return null()

    an: int = py_bytes_len(o)
    ad = _bytes_data(o)
    if ptr_is_null(ad) != 0 or an < 0:
        py_raise_owned(py_exc_new(3, cstr("bytearray.append target must be bytearray")))
        return null()
    if an > 9223372036854775806:
        py_raise_owned(py_exc_new(15, cstr("bytearray too large")))
        return null()

    total: int = an + 1
    out = _bytearray_new_raw(null(), total)
    if ptr_is_null(out) != 0:
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst) != 0:
        return null()
    i: int = 0
    while i < an:
        store_i8(dst, i, load_i8(ad, i))
        i = i + 1
    store_i8(dst, an, byte)
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytearray_insert")
def py_bytearray_insert(o, index, item):
    # bytearray.insert(index, byte): grow by one, shifting the tail right, and
    # store ``byte`` at the CPython-clamped index (negative adds len, then
    # clamp into [0, len]). Inline data[] has no spare room, so growth rebuilds
    # a fresh object; the frontend re-binds the target (same as append).
    if ptr_is_null(o) != 0 or _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("bytearray.insert target must be bytearray")))
        return null()
    if ptr_is_null(index) != 0 or _type_of(index) != PY_TYPE_INT:
        py_raise_owned(
            py_exc_new(3, cstr("'object' object cannot be interpreted as an integer"))
        )
        return null()
    if ptr_is_null(item) != 0 or _type_of(item) != PY_TYPE_INT:
        py_raise_owned(
            py_exc_new(3, cstr("'object' object cannot be interpreted as an integer"))
        )
        return null()
    byte: int = py_int_value_i64(item)
    if byte < 0 or byte > 255:
        py_raise_owned(py_exc_new(2, cstr("byte must be in range(0, 256)")))
        return null()

    an: int = py_bytes_len(o)
    ad = _bytes_data(o)
    if ptr_is_null(ad) != 0 or an < 0:
        py_raise_owned(py_exc_new(3, cstr("bytearray.insert target must be bytearray")))
        return null()
    if an > 9223372036854775806:
        py_raise_owned(py_exc_new(15, cstr("bytearray too large")))
        return null()

    at: int = py_int_value_i64(index)
    if at < 0:
        at = at + an
        if at < 0:
            at = 0
    if at > an:
        at = an

    total: int = an + 1
    out = _bytearray_new_raw(null(), total)
    if ptr_is_null(out) != 0:
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst) != 0:
        return null()
    i: int = 0
    while i < at:
        store_i8(dst, i, load_i8(ad, i))
        i = i + 1
    store_i8(dst, at, byte)
    j: int = at
    while j < an:
        store_i8(dst, j + 1, load_i8(ad, j))
        j = j + 1
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytearray_pop")
def py_bytearray_pop(o, index):
    # bytearray.pop([index]): remove and return the byte at ``index`` (default
    # last) as an int. Shrinking never needs more room, so mutate in place
    # (shift the tail down + decrement byte_len at offset 16). None/non-int
    # index means "last element" (pop() == pop(-1)).
    if ptr_is_null(o) != 0 or _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("pop() requires a bytearray")))
        return null()
    length: int = py_bytes_len(o)
    if length <= 0:
        py_raise_owned(py_exc_new(5, cstr("pop from empty bytearray")))
        return null()

    at: int = length - 1
    if ptr_is_null(index) == 0 and _type_of(index) == PY_TYPE_INT:
        at = py_int_value_i64(index)
        if at < 0:
            at = at + length
    if at < 0 or at >= length:
        py_raise_owned(py_exc_new(5, cstr("pop index out of range")))
        return null()

    data = _bytes_data(o)
    if ptr_is_null(data) != 0:
        py_raise_owned(py_exc_new(3, cstr("pop() requires a bytearray")))
        return null()
    byte: int = load_i8(data, at) & 255
    k: int = at
    while k < length - 1:
        store_i8(data, k, load_i8(data, k + 1))
        k = k + 1
    store_i64(o, 16, length - 1)
    store_i8(data, length - 1, 0)
    return py_int_from_i64(byte)


@c_abi_export("py_memoryview_new")
def py_memoryview_new(o):
    # PyMemoryViewObject prefix:
    #   header@0, base@16, per-memoryview-owned Py_buffer allocation@24.
    # The buffer allocation is populated lazily by
    # pcc_PyMemoryView_GET_BUFFER so the core object constructor does not
    # depend on the C-API module during archive extraction.
    p = pcc_gc_alloc(32, PY_TYPE_MEMORYVIEW, 0)
    if ptr_is_null(p):
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_MEMORYVIEW)  # PY_TYPE_MEMORYVIEW
    store_ptr(p, 16, null())
    store_ptr(p, 24, null())
    pcc_gc_store_ptr(p, ptr_add(p, 16), o)
    pcc_gc_publish_initialized(p)
    return p


@c_abi_export("py_dealloc_memoryview")
def py_dealloc_memoryview(o) -> None:
    if ptr_is_null(o):
        return
    buffer_view = load_ptr(o, 24)
    if not ptr_is_null(buffer_view):
        # The embedded Py_buffer.obj is a derived/borrowed alias of base;
        # freeing this raw allocation must not decref it a second time.
        store_ptr(o, 24, null())
        py_mem_free(buffer_view)
    base = pcc_gc_load_ptr(o, ptr_add(o, 16))
    if not ptr_is_null(base):
        py_decref(base)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_bytes_decode")
def py_bytes_decode(o):
    return py_str_new(_bytes_data(o), py_bytes_len(o))


def _utf8_cont(c: int) -> int:
    if (c & 192) == 128:
        return 1
    return 0


def _utf8_valid_width(data, n: int, i: int) -> int:
    c: int = load_i8(data, i) & 255
    if c < 128:
        return 1
    if c >= 194 and c <= 223:
        if i + 1 < n and _utf8_cont(load_i8(data, i + 1) & 255) != 0:
            return 2
        return 0
    if c == 224:
        c1: int = load_i8(data, i + 1) & 255 if i + 1 < n else 0
        if (
            i + 2 < n
            and c1 >= 160
            and c1 <= 191
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
        ):
            return 3
        return 0
    if c >= 225 and c <= 236:
        if (
            i + 2 < n
            and _utf8_cont(load_i8(data, i + 1) & 255) != 0
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
        ):
            return 3
        return 0
    if c == 237:
        c2: int = load_i8(data, i + 1) & 255 if i + 1 < n else 0
        if (
            i + 2 < n
            and c2 >= 128
            and c2 <= 159
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
        ):
            return 3
        return 0
    if c >= 238 and c <= 239:
        if (
            i + 2 < n
            and _utf8_cont(load_i8(data, i + 1) & 255) != 0
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
        ):
            return 3
        return 0
    if c == 240:
        c3: int = load_i8(data, i + 1) & 255 if i + 1 < n else 0
        if (
            i + 3 < n
            and c3 >= 144
            and c3 <= 191
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
            and _utf8_cont(load_i8(data, i + 3) & 255) != 0
        ):
            return 4
        return 0
    if c >= 241 and c <= 243:
        if (
            i + 3 < n
            and _utf8_cont(load_i8(data, i + 1) & 255) != 0
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
            and _utf8_cont(load_i8(data, i + 3) & 255) != 0
        ):
            return 4
        return 0
    if c == 244:
        c4: int = load_i8(data, i + 1) & 255 if i + 1 < n else 0
        if (
            i + 3 < n
            and c4 >= 128
            and c4 <= 143
            and _utf8_cont(load_i8(data, i + 2) & 255) != 0
            and _utf8_cont(load_i8(data, i + 3) & 255) != 0
        ):
            return 4
    return 0


@c_abi_export("py_bytes_decode_utf8_ignore")
def py_bytes_decode_utf8_ignore(o):
    data = _bytes_data(o)
    n: int = py_bytes_len(o)
    if ptr_is_null(data) or n <= 0:
        return py_str_new(null(), 0)
    tmp = py_mem_alloc(n)
    if ptr_is_null(tmp):
        return null()
    out_n: int = 0
    i: int = 0
    while i < n:
        width: int = _utf8_valid_width(data, n, i)
        if width <= 0:
            i = i + 1
        else:
            j: int = 0
            while j < width:
                store_i8(tmp, out_n, load_i8(data, i + j))
                out_n = out_n + 1
                j = j + 1
            i = i + width
    out = py_str_new(tmp, out_n)
    py_mem_free(tmp)
    return out


def _ascii_lower(c: int) -> int:
    if c >= 65 and c <= 90:
        return c + 32
    return c


def _str_is_utf8_name(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj) or _type_of(obj) != PY_TYPE_STR:
        return 0
    data = py_str_utf8(obj)
    n: int = py_str_byte_len(obj)
    if n == 4:
        if (
            _ascii_lower(load_i8(data, 0) & 255) == 117
            and _ascii_lower(load_i8(data, 1) & 255) == 116
            and _ascii_lower(load_i8(data, 2) & 255) == 102
            and load_i8(data, 3) == 56
        ):
            return 1
    if n == 5:
        sep: int = load_i8(data, 3) & 255
        if (
            _ascii_lower(load_i8(data, 0) & 255) == 117
            and _ascii_lower(load_i8(data, 1) & 255) == 116
            and _ascii_lower(load_i8(data, 2) & 255) == 102
            and (sep == 45 or sep == 95)
            and load_i8(data, 4) == 56
        ):
            return 1
    return 0


def _str_is_errors_name(obj, ignore: int) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj) or _type_of(obj) != PY_TYPE_STR:
        return 0
    data = py_str_utf8(obj)
    if py_str_byte_len(obj) != 6:
        return 0
    if ignore != 0:
        expected0: int = 105
        expected1: int = 103
        expected2: int = 110
        expected3: int = 111
        expected4: int = 114
        expected5: int = 101
    else:
        expected0 = 115
        expected1 = 116
        expected2 = 114
        expected3 = 105
        expected4 = 99
        expected5 = 116
    if (
        _ascii_lower(load_i8(data, 0) & 255) == expected0
        and _ascii_lower(load_i8(data, 1) & 255) == expected1
        and _ascii_lower(load_i8(data, 2) & 255) == expected2
        and _ascii_lower(load_i8(data, 3) & 255) == expected3
        and _ascii_lower(load_i8(data, 4) & 255) == expected4
        and _ascii_lower(load_i8(data, 5) & 255) == expected5
    ):
        return 1
    return 0


@c_abi_export("py_bytes_decode_with_encoding")
def py_bytes_decode_with_encoding(o, encoding, errors):
    if (
        ptr_is_null(o)
        or is_tagged_int(o)
        or (_type_of(o) != PY_TYPE_BYTES and _type_of(o) != PY_TYPE_BYTEARRAY and _type_of(o) != PY_TYPE_MEMORYVIEW)
    ):
        py_raise_owned(py_exc_new(3, cstr("decoding to str: need bytes-like object")))
        return null()
    if _str_is_utf8_name(encoding) == 0:
        py_raise_owned(py_exc_new(13, cstr("pcc-native bytes decode supports utf-8 only")))
        return null()
    if (
        ptr_is_null(errors)
        or ptr_eq(errors, global_load_ptr("py_None")) != 0
        or _str_is_errors_name(errors, 0) != 0
    ):
        return py_bytes_decode(o)
    if _str_is_errors_name(errors, 1) != 0:
        return py_bytes_decode_utf8_ignore(o)
    py_raise_owned(py_exc_new(13, cstr("unsupported pcc-native bytes decode errors mode")))
    return null()


@c_abi_export("py_bytes_getitem")
def py_bytes_getitem(o, k):
    i: int = py_int_value_i64(k)
    n: int = py_bytes_len(o)
    data = _bytes_data(o)
    if i < 0:
        i = i + n
    if i < 0 or i >= n or ptr_is_null(data):
        py_raise_owned(py_exc_new(5, cstr("bytes index out of range")))  # PY_EXC_INDEXERROR
        return null()
    return py_int_from_i64(load_i8(data, i) & 255)


@c_abi_export("py_bytes_slice")
def py_bytes_slice(o, lo, hi, step):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    length: int = py_bytes_len(o)
    step_v: int = 1
    if _bytes_is_none_or_null(step) == 0:
        step_v = py_int_value_i64(step)
        if step_v == 0:
            return null()

    lo_v: int = _bytes_slice_lo(lo, length, step_v)
    hi_v: int = _bytes_slice_hi(hi, length, step_v)
    lo_v = _bytes_normalize_lo(lo_v, length, step_v)
    hi_v = _bytes_normalize_hi(hi, hi_v, length, step_v)

    count: int = _bytes_slice_count(lo_v, hi_v, step_v)
    if count <= 0:
        return _bytes_new_same_family(o, null(), 0)
    if step_v == 1:
        return _bytes_new_same_family(o, ptr_add(data, lo_v), count)

    tmp = py_mem_alloc(count)
    if ptr_is_null(tmp):
        return null()
    j: int = 0
    if step_v > 0:
        i: int = lo_v
        while i < hi_v:
            store_i8(tmp, j, load_i8(data, i))
            j = j + 1
            i = i + step_v
    else:
        i2: int = lo_v
        while i2 > hi_v:
            if i2 < 0 or i2 >= length:
                break
            store_i8(tmp, j, load_i8(data, i2))
            j = j + 1
            i2 = i2 + step_v
    out = _bytes_new_same_family(o, tmp, j)
    py_mem_free(tmp)
    return out


@c_abi_export("py_bytes_concat")
def py_bytes_concat(a, b):
    if ptr_is_null(a) or ptr_is_null(b):
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if not (at == PY_TYPE_BYTES or at == PY_TYPE_BYTEARRAY):
        return null()
    if not (bt == PY_TYPE_BYTES or bt == PY_TYPE_BYTEARRAY):
        return null()
    la: int = py_bytes_len(a)
    lb: int = py_bytes_len(b)
    if la < 0 or lb < 0:
        return null()
    if la > 9223372036854775807 - lb:
        return null()
    total: int = la + lb
    out = _bytes_new_same_family(a, null(), total)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    ad = _bytes_data(a)
    bd = _bytes_data(b)
    if ptr_is_null(dst) or ptr_is_null(ad) or ptr_is_null(bd):
        return null()
    i: int = 0
    while i < la:
        store_i8(dst, i, load_i8(ad, i))
        i = i + 1
    j: int = 0
    while j < lb:
        store_i8(dst, la + j, load_i8(bd, j))
        j = j + 1
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytes_join")
def py_bytes_join(sep, lst):
    # bytes.join / bytearray.join over a list or tuple of bytes-like items.
    # Mirrors py_bytes_join in py_bytes.c.  Result takes the separator's
    # family.  Items that are not bytes/bytearray raise TypeError.  This is
    # the O(n) bulk-building primitive (chunks + one join) that replaces
    # quadratic bytearray += under pcc1.
    if ptr_is_null(sep) or ptr_is_null(lst):
        return null()
    sep = pcc_gc_note_relocation_read(sep)
    lst = pcc_gc_note_relocation_read(lst)
    st: int = _type_of(sep)
    if not (st == PY_TYPE_BYTES or st == PY_TYPE_BYTEARRAY):
        py_raise_owned(py_exc_new(3, cstr("bytes.join receiver must be bytes-like")))
        return null()
    sequence_tag: int = _type_of(lst)
    if sequence_tag != PY_TYPE_LIST and sequence_tag != PY_TYPE_TUPLE:
        py_raise_owned(py_exc_new(3, cstr("bytes.join argument must be a list or tuple")))
        return null()
    length: int = 0
    if sequence_tag == PY_TYPE_LIST:
        length = load_i64(lst, PYLISTOBJECT_LENGTH_OFFSET)
    else:
        length = load_i64(lst, PYTUPLEOBJECT_LEN_OFFSET)
    if length == 0:
        return _bytes_new_same_family(sep, null(), 0)
    sep_len: int = py_bytes_len(sep)
    items = null()
    if sequence_tag == PY_TYPE_LIST:
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    else:
        items = ptr_add(lst, PYTUPLEOBJECT_ITEMS_OFFSET)
    total: int = 0
    i: int = 0
    while i < length:
        e = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        if ptr_is_null(e) != 0:
            return null()
        tag: int = _type_of(e)
        if tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY:
            py_raise_owned(py_exc_new(3, cstr("sequence item: expected a bytes-like object")))
            return null()
        if i > 0:
            if total > 9223372036854775807 - sep_len:
                return null()
            total = total + sep_len
        elem_len: int = py_bytes_len(e)
        if total > 9223372036854775807 - elem_len:
            return null()
        total = total + elem_len
        i = i + 1
    out = _bytes_new_same_family(sep, null(), total)
    if ptr_is_null(out):
        return null()
    # Result allocation may run a relocating collector: refresh the rooted
    # inputs and the list items pointer before copying payloads.
    sep = pcc_gc_note_relocation_read(sep)
    lst = pcc_gc_note_relocation_read(lst)
    if sequence_tag == PY_TYPE_LIST:
        items = load_ptr(lst, PYLISTOBJECT_ITEMS_OFFSET)
    else:
        items = ptr_add(lst, PYTUPLEOBJECT_ITEMS_OFFSET)
    dst = _bytes_data(out)
    sd = _bytes_data(sep)
    off: int = 0
    i = 0
    while i < length:
        e = pcc_gc_load_ptr(lst, ptr_add(items, i * 8))
        if i > 0 and sep_len > 0:
            k: int = 0
            while k < sep_len:
                store_i8(dst, off + k, load_i8(sd, k))
                k = k + 1
            off = off + sep_len
        elem_len2: int = py_bytes_len(e)
        ed = _bytes_data(e)
        j: int = 0
        while j < elem_len2:
            store_i8(dst, off + j, load_i8(ed, j))
            j = j + 1
        off = off + elem_len2
        i = i + 1
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytes_repeat")
def py_bytes_repeat(src, count: int):
    if ptr_is_null(src):
        return null()
    tag: int = _type_of(src)
    if not (tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY):
        return null()
    n: int = py_bytes_len(src)
    data = _bytes_data(src)
    if ptr_is_null(data):
        return null()
    if count <= 0 or n == 0:
        return _bytes_new_same_family(src, null(), 0)
    if count > 9223372036854775807 // n:
        return null()
    total: int = count * n
    out = _bytes_new_same_family(src, null(), total)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    k: int = 0
    while k < count:
        i: int = 0
        while i < n:
            store_i8(dst, k * n + i, load_i8(data, i))
            i = i + 1
        k = k + 1
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytes_maketrans")
def py_bytes_maketrans(x, y):
    xd = _bytes_data(x)
    yd = _bytes_data(y)
    if ptr_is_null(xd) or ptr_is_null(yd):
        py_raise_owned(py_exc_new(3, cstr("maketrans arguments must be bytes-like")))
        return null()
    xn: int = py_bytes_len(x)
    yn: int = py_bytes_len(y)
    if xn != yn:
        py_raise_owned(py_exc_new(2, cstr("maketrans arguments must have the same length")))
        return null()
    table = py_mem_alloc(256)
    if ptr_is_null(table):
        return null()
    i: int = 0
    while i < 256:
        store_i8(table, i, i)
        i = i + 1
    j: int = 0
    while j < xn:
        src_byte: int = load_i8(xd, j) & 255
        dst_byte: int = load_i8(yd, j) & 255
        store_i8(table, src_byte, dst_byte)
        j = j + 1
    out = py_bytes_new(table, 256)
    py_mem_free(table)
    return out


@c_abi_export("py_bytes_translate")
def py_bytes_translate(src, table):
    data = _bytes_data(src)
    mapping = _bytes_data(table)
    if ptr_is_null(data) or ptr_is_null(mapping):
        py_raise_owned(py_exc_new(3, cstr("translate arguments must be bytes-like")))
        return null()
    n: int = py_bytes_len(src)
    table_n: int = py_bytes_len(table)
    if table_n != 256:
        py_raise_owned(py_exc_new(2, cstr("translation table must be 256 characters long")))
        return null()
    out = _bytes_new_same_family(src, null(), n)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        store_i8(dst, i, load_i8(mapping, c) & 255)
        i = i + 1
    store_i8(dst, n, 0)
    return out


def _hex_value(c: int) -> int:
    if c >= 48 and c <= 57:
        return c - 48
    if c >= 97 and c <= 102:
        return c - 87
    if c >= 65 and c <= 70:
        return c - 55
    return -1


def _hex_space(c: int) -> int:
    if c == 32 or c == 9 or c == 10 or c == 13 or c == 11 or c == 12:
        return 1
    return 0


@c_abi_export("py_bytes_fromhex")
def py_bytes_fromhex(text):
    data = null()
    n: int = 0
    tag: int = _type_of(text)
    if tag == PY_TYPE_STR:
        data = py_str_utf8(text)
        n = py_str_byte_len(text)
    elif tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        data = _bytes_data(text)
        n = py_bytes_len(text)
    else:
        py_raise_owned(py_exc_new(3, cstr("fromhex() argument must be str or bytes-like")))
        return null()
    if ptr_is_null(data):
        return null()
    tmp = py_mem_alloc(n // 2 + 1)
    if ptr_is_null(tmp):
        return null()
    out_n: int = 0
    have_hi: int = 0
    hi: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if _hex_space(c) != 0:
            i = i + 1
            continue
        v: int = _hex_value(c)
        if v < 0:
            py_mem_free(tmp)
            py_raise_owned(
                py_exc_new(2, cstr("non-hexadecimal number found in fromhex() arg"))
            )
            return null()
        if have_hi == 0:
            hi = v
            have_hi = 1
        else:
            store_i8(tmp, out_n, (hi << 4) | v)
            out_n = out_n + 1
            have_hi = 0
        i = i + 1
    if have_hi != 0:
        py_mem_free(tmp)
        py_raise_owned(py_exc_new(2, cstr("non-hexadecimal number found in fromhex() arg")))
        return null()
    out = py_bytes_new(tmp, out_n)
    py_mem_free(tmp)
    return out


@c_abi_export("py_bytes_replace")
def py_bytes_replace(src, old, new_value):
    data = _bytes_data(src)
    old_data = _bytes_data(old)
    new_data = _bytes_data(new_value)
    if ptr_is_null(data) or ptr_is_null(old_data) or ptr_is_null(new_data):
        py_raise_owned(py_exc_new(3, cstr("replace arguments must be bytes-like")))
        return null()
    n: int = py_bytes_len(src)
    old_n: int = py_bytes_len(old)
    new_n: int = py_bytes_len(new_value)
    if old_n <= 0:
        return _bytes_new_same_family(src, data, n)
    matches: int = 0
    i: int = 0
    while i <= n - old_n:
        same: int = 1
        j: int = 0
        while j < old_n:
            if load_i8(data, i + j) != load_i8(old_data, j):
                same = 0
                break
            j = j + 1
        if same != 0:
            matches = matches + 1
            i = i + old_n
        else:
            i = i + 1
    if matches == 0:
        return _bytes_new_same_family(src, data, n)
    out_n: int = n + matches * (new_n - old_n)
    out = _bytes_new_same_family(src, null(), out_n)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    read_i: int = 0
    pos: int = 0
    while read_i < n:
        found: int = 0
        if read_i <= n - old_n:
            found = 1
            k: int = 0
            while k < old_n:
                if load_i8(data, read_i + k) != load_i8(old_data, k):
                    found = 0
                    break
                k = k + 1
        if found != 0:
            m: int = 0
            while m < new_n:
                store_i8(dst, pos + m, load_i8(new_data, m))
                m = m + 1
            pos = pos + new_n
            read_i = read_i + old_n
        else:
            store_i8(dst, pos, load_i8(data, read_i))
            pos = pos + 1
            read_i = read_i + 1
    store_i8(dst, out_n, 0)
    return out


@c_abi_export("py_bytearray_setitem")
def py_bytearray_setitem(o, k, v) -> int:
    if _type_of(o) != PY_TYPE_BYTEARRAY:
        return -1
    i: int = py_int_value_i64(k)
    byte: int = py_int_value_i64(v)
    n: int = load_i64(o, 16)
    if i < 0:
        i = i + n
    if i < 0 or i >= n:
        py_raise_owned(py_exc_new(5, cstr("bytearray index out of range")))
        return -1
    if byte < 0 or byte > 255:
        py_raise_owned(py_exc_new(2, cstr("byte must be in range(0, 256)")))
        return -1
    store_i8(o, 24 + i, byte)
    return 0


def _bytearray_delete_selected(o, lo: int, hi: int, step: int, length: int) -> int:
    write: int = 0
    read: int = 0
    while read < length:
        selected: int = 0
        if step > 0:
            if read >= lo and read < hi and ((read - lo) % step) == 0:
                selected = 1
        else:
            neg_step: int = -step
            if read <= lo and read > hi and ((lo - read) % neg_step) == 0:
                selected = 1
        if selected == 0:
            store_i8(o, 24 + write, load_i8(o, 24 + read))
            write = write + 1
        read = read + 1
    store_i64(o, 16, write)
    store_i8(o, 24 + write, 0)
    return 0


@c_abi_export("py_bytearray_del_slice")
def py_bytearray_del_slice(o, lo, hi, step) -> int:
    if _type_of(o) != PY_TYPE_BYTEARRAY:
        return -1
    length: int = load_i64(o, 16)
    step_v: int = 1
    if _bytes_is_none_or_null(step) == 0:
        step_v = py_int_value_i64(step)
        if step_v == 0:
            return -1

    lo_v: int = _bytes_slice_lo(lo, length, step_v)
    hi_v: int = _bytes_slice_hi(hi, length, step_v)
    lo_v = _bytes_normalize_lo(lo_v, length, step_v)
    hi_v = _bytes_normalize_hi(hi, hi_v, length, step_v)

    if step_v == 1:
        if hi_v <= lo_v:
            return 0
        tail: int = length - hi_v
        i: int = 0
        while i < tail:
            store_i8(o, 24 + lo_v + i, load_i8(o, 24 + hi_v + i))
            i = i + 1
        new_len: int = length - (hi_v - lo_v)
        store_i64(o, 16, new_len)
        store_i8(o, 24 + new_len, 0)
        return 0
    return _bytearray_delete_selected(o, lo_v, hi_v, step_v, length)


def _hex_digit(v: int) -> int:
    if v < 10:
        return 48 + v
    return 97 + (v - 10)


def _load_u8(p, offset: int) -> int:
    return load_i8(p, offset) & 255


def _append_hex_escape(buf, pos: int, prefix: int, value: int, digits: int) -> int:
    store_i8(buf, pos, 92)
    pos = pos + 1
    store_i8(buf, pos, prefix)
    pos = pos + 1
    shift: int = (digits - 1) * 4
    while shift >= 0:
        store_i8(buf, pos, _hex_digit((value >> shift) & 15))
        pos = pos + 1
        shift = shift - 4
    return pos


def _obj_repr_str(o, escape_non_ascii: int):
    byte_len: int = load_i64(o, 16)
    src = ptr_add(o, 40)
    out_len: int = 2
    i: int = 0
    if escape_non_ascii != 0:
        out_len = out_len + byte_len * 10
    else:
        while i < byte_len:
            c: int = _load_u8(src, i)
            if c == 92 or c == 39 or c == 10 or c == 13 or c == 9:
                out_len = out_len + 2
            else:
                out_len = out_len + 1
            i = i + 1
    buf = py_mem_alloc(out_len + 1)
    if ptr_is_null(buf):
        return null()
    pos: int = 0
    store_i8(buf, pos, 39)
    pos = pos + 1
    i = 0
    while i < byte_len:
        c2: int = _load_u8(src, i)
        if c2 == 92:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 92)
            pos = pos + 1
            i = i + 1
        elif c2 == 39:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 39)
            pos = pos + 1
            i = i + 1
        elif c2 == 10:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 110)
            pos = pos + 1
            i = i + 1
        elif c2 == 13:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 114)
            pos = pos + 1
            i = i + 1
        elif c2 == 9:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 116)
            pos = pos + 1
            i = i + 1
        elif escape_non_ascii != 0 and (c2 < 32 or c2 == 127):
            pos = _append_hex_escape(buf, pos, 120, c2, 2)
            i = i + 1
        elif escape_non_ascii != 0 and c2 >= 128:
            cp: int = c2
            next_i: int = i + 1
            if (
                (c2 & 224) == 192
                and i + 1 < byte_len
                and (_load_u8(src, i + 1) & 192) == 128
            ):
                cp = ((c2 & 31) << 6) | (_load_u8(src, i + 1) & 63)
                next_i = i + 2
            elif (
                (c2 & 240) == 224
                and i + 2 < byte_len
                and (_load_u8(src, i + 1) & 192) == 128
                and (_load_u8(src, i + 2) & 192) == 128
            ):
                cp = (
                    ((c2 & 15) << 12)
                    | ((_load_u8(src, i + 1) & 63) << 6)
                    | (_load_u8(src, i + 2) & 63)
                )
                next_i = i + 3
            elif (
                (c2 & 248) == 240
                and i + 3 < byte_len
                and (_load_u8(src, i + 1) & 192) == 128
                and (_load_u8(src, i + 2) & 192) == 128
                and (_load_u8(src, i + 3) & 192) == 128
            ):
                cp = (
                    ((c2 & 7) << 18)
                    | ((_load_u8(src, i + 1) & 63) << 12)
                    | ((_load_u8(src, i + 2) & 63) << 6)
                    | (_load_u8(src, i + 3) & 63)
                )
                next_i = i + 4
            if cp <= 255:
                pos = _append_hex_escape(buf, pos, 120, cp, 2)
            elif cp <= 65535:
                pos = _append_hex_escape(buf, pos, 117, cp, 4)
            else:
                pos = _append_hex_escape(buf, pos, 85, cp, 8)
            i = next_i
        else:
            store_i8(buf, pos, c2)
            pos = pos + 1
            i = i + 1
    store_i8(buf, pos, 39)
    pos = pos + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


def _str_lit1(b0: int):
    buf = py_mem_alloc(2)
    store_i8(buf, 0, b0)
    store_i8(buf, 1, 0)
    out = py_str_new(buf, 1)
    py_mem_free(buf)
    return out


def _str_lit2(b0: int, b1: int):
    buf = py_mem_alloc(3)
    store_i8(buf, 0, b0)
    store_i8(buf, 1, b1)
    store_i8(buf, 2, 0)
    out = py_str_new(buf, 2)
    py_mem_free(buf)
    return out


def _str_lit3(b0: int, b1: int, b2: int):
    buf = py_mem_alloc(4)
    store_i8(buf, 0, b0)
    store_i8(buf, 1, b1)
    store_i8(buf, 2, b2)
    store_i8(buf, 3, 0)
    out = py_str_new(buf, 3)
    py_mem_free(buf)
    return out


def _str_lit4(b0: int, b1: int, b2: int, b3: int):
    buf = py_mem_alloc(5)
    store_i8(buf, 0, b0)
    store_i8(buf, 1, b1)
    store_i8(buf, 2, b2)
    store_i8(buf, 3, b3)
    store_i8(buf, 4, 0)
    out = py_str_new(buf, 4)
    py_mem_free(buf)
    return out


def _str_false():
    buf = py_mem_alloc(6)
    store_i8(buf, 0, 70)
    store_i8(buf, 1, 97)
    store_i8(buf, 2, 108)
    store_i8(buf, 3, 115)
    store_i8(buf, 4, 101)
    store_i8(buf, 5, 0)
    out = py_str_new(buf, 5)
    py_mem_free(buf)
    return out


def _cat_take(acc, piece):
    # acc and piece are both owned; concat, release both, return new owned str.
    out = py_str_concat(acc, piece)
    py_decref(acc)
    py_decref(piece)
    return out


def _elem_repr(item):
    # Element repr for container formatting; never returns null (so concat is
    # always safe).  Unsupported element types render as '?'.
    r = py_obj_repr(item)
    if ptr_is_null(r):
        return _str_lit1(63)  # '?'
    return r


def _format_list_str(o):
    acc = _str_lit1(91)  # '['
    n: int = py_list_len(o)
    i: int = 0
    while i < n:
        if i > 0:
            acc = _cat_take(acc, _str_lit2(44, 32))  # ', '
        acc = _cat_take(acc, _elem_repr(py_list_get(o, i)))
        i = i + 1
    acc = _cat_take(acc, _str_lit1(93))  # ']'
    return acc


def _format_tuple_str(o):
    acc = _str_lit1(40)  # '('
    n: int = py_tuple_len(o)
    i: int = 0
    while i < n:
        if i > 0:
            acc = _cat_take(acc, _str_lit2(44, 32))  # ', '
        acc = _cat_take(acc, _elem_repr(py_tuple_get(o, i)))
        i = i + 1
    if n == 1:
        acc = _cat_take(acc, _str_lit1(44))  # trailing ','
    acc = _cat_take(acc, _str_lit1(41))  # ')'
    return acc


def _format_dict_str(o):
    # PyDictObject: entries ptr @40, entries_used @48.  DictEntry (24 bytes):
    # key @8 (NULL = dead), value @16.  Borrowed key/value via the GC barrier.
    acc = _str_lit1(123)  # '{'
    entries = load_ptr(o, 40)
    entries_used: int = load_i64(o, 48)
    emitted: int = 0
    i: int = 0
    while i < entries_used:
        entry = ptr_add(entries, i * 24)
        if ptr_is_null(load_ptr(entry, 8)) == 0:
            if emitted > 0:
                acc = _cat_take(acc, _str_lit2(44, 32))  # ', '
            acc = _cat_take(acc, _elem_repr(pcc_gc_load_ptr(o, ptr_add(entry, 8))))
            acc = _cat_take(acc, _str_lit2(58, 32))  # ': '
            acc = _cat_take(acc, _elem_repr(pcc_gc_load_ptr(o, ptr_add(entry, 16))))
            emitted = emitted + 1
        i = i + 1
    acc = _cat_take(acc, _str_lit1(125))  # '}'
    return acc


def _format_set_str(o):
    # PySetObject: size @16, capacity @24, entries ptr @40.
    # SetEntry (16 bytes): key @8 (NULL empty, py_set_dummy tombstone).
    size: int = load_i64(o, 16)
    if size == 0:
        buf = py_mem_alloc(6)
        store_i8(buf, 0, 115)  # 's'
        store_i8(buf, 1, 101)  # 'e'
        store_i8(buf, 2, 116)  # 't'
        store_i8(buf, 3, 40)  # '('
        store_i8(buf, 4, 41)  # ')'
        store_i8(buf, 5, 0)
        out = py_str_new(buf, 5)
        py_mem_free(buf)
        return out
    acc = _str_lit1(123)  # '{'
    entries = load_ptr(o, 40)
    dummy = global_load_ptr("py_set_dummy")
    cap: int = load_i64(o, 24)
    emitted: int = 0
    i: int = 0
    while i < cap:
        key = load_ptr(entries, i * 16 + 8)
        if ptr_is_null(key) == 0:
            if ptr_eq(key, dummy) == 0:
                if emitted > 0:
                    acc = _cat_take(acc, _str_lit2(44, 32))  # ', '
                acc = _cat_take(
                    acc, _elem_repr(pcc_gc_load_ptr(o, ptr_add(entries, i * 16 + 8)))
                )
                emitted = emitted + 1
        i = i + 1
    acc = _cat_take(acc, _str_lit1(125))  # '}'
    return acc


def _format_bytes_str(o):
    # bytes repr: b'...' with \\ \' \n \r \t and \xNN escapes.  Data @24,
    # byte_len @16.  Mirrors py_print_fmt.py::_format_bytes but builds a PyStr.
    n: int = load_i64(o, 16)
    data = ptr_add(o, 24)
    buf = py_mem_alloc(n * 4 + 8)
    if ptr_is_null(buf):
        return null()
    pos: int = 0
    store_i8(buf, pos, 98)  # 'b'
    pos = pos + 1
    store_i8(buf, pos, 39)  # "'"
    pos = pos + 1
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if c == 92:  # '\'
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 92)
            pos = pos + 2
        elif c == 39:  # "'"
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 39)
            pos = pos + 2
        elif c == 10:
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 110)  # \n
            pos = pos + 2
        elif c == 13:
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 114)  # \r
            pos = pos + 2
        elif c == 9:
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 116)  # \t
            pos = pos + 2
        elif c < 32 or c >= 127:
            # bytes repr: only printable ASCII (32..126) raw; control, DEL and
            # all high bytes (>=128) escape as \xNN. (c == 127 missed 128..255,
            # so b'\xcf\x80' printed the raw UTF-8 char.)
            pos = _append_hex_escape(buf, pos, 120, c, 2)  # \xNN
        else:
            store_i8(buf, pos, c)
            pos = pos + 1
        i = i + 1
    store_i8(buf, pos, 39)  # "'"
    pos = pos + 1
    store_i8(buf, pos, 0)
    out = py_str_new(buf, pos)
    py_mem_free(buf)
    return out


def _format_bytearray_str(o):
    # bytearray repr: bytearray(b'...'). Reuse the bytes inner repr and wrap it
    # in "bytearray(" + ... + ")". py_str_concat builds a new string and leaves
    # its operands borrowed, so each intermediate is decref'd here.
    inner = _format_bytes_str(o)
    if ptr_is_null(inner):
        return null()
    pre = py_str_new(cstr("bytearray("), 10)
    mid = py_str_concat(pre, inner)
    suf = py_str_new(cstr(")"), 1)
    out = py_str_concat(mid, suf)
    py_decref(inner)
    py_decref(pre)
    py_decref(mid)
    py_decref(suf)
    return out


def _float_str(o):
    # str/repr of a float: CPython shortest-round-trip repr via the shared C
    # helper py_float_repr_shortest (handles inf/nan and the trailing ".0"
    # internally). Replaces the old fixed-6-decimal path (py_float_format_fixed
    # + manual trailing-zero strip), which produced "3.333333" for 10/3 rather
    # than CPython's "3.3333333333333335".
    return py_float_repr_shortest(o)


def _format_builtin_str(o, tag: int):
    # Shared str/repr for the builtin non-scalar tags the inline dispatch in
    # py_obj_str / py_obj_repr does not handle directly.  Returns null when the
    # tag is not one of these (caller falls back to user dispatch).
    if tag == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return _float_str(o)
    if tag == PY_TYPE_NONE:  # PY_TYPE_NONE
        return _str_lit4(78, 111, 110, 101)  # 'None'
    if tag == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            return _str_lit4(84, 114, 117, 101)  # 'True'
        return _str_false()
    if tag == PY_TYPE_LIST:  # PY_TYPE_LIST
        return _format_list_str(o)
    if tag == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        return _format_tuple_str(o)
    if tag == PY_TYPE_DICT:  # PY_TYPE_DICT
        return _format_dict_str(o)
    if tag == PY_TYPE_SET:  # PY_TYPE_SET
        return _format_set_str(o)
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        return _format_bytes_str(o)
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        return _format_bytearray_str(o)
    return null()


@c_abi_export("py_obj_repr")
def py_obj_repr(o):
    if ptr_is_null(o):
        return null()
    tag: int = _type_of(o)
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        return _obj_repr_str(o, 0)
    if tag == PY_TYPE_NONE or tag == PY_TYPE_BOOL or tag == PY_TYPE_INT:
        return py_obj_str(o)
    built = _format_builtin_str(o, tag)
    if not ptr_is_null(built):
        return built
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        # repr(exc) == ClassName(repr(arg)); shared C helper (py_format.c).
        return py_exc_repr(o)
    if tag == PY_TYPE_COMPLEX:  # PY_TYPE_COMPLEX
        return py_complex_repr(o)
    dunder = py_user_repr_dispatch(o)
    if not ptr_is_null(dunder):
        return dunder
    return null()


@c_abi_export("py_obj_ascii")
def py_obj_ascii(o):
    if ptr_is_null(o):
        return null()
    tag: int = _type_of(o)
    if tag == PY_TYPE_STR:
        return _obj_repr_str(o, 1)
    return py_obj_repr(o)


@c_abi_export("py_obj_str")
def py_obj_str(o):
    if ptr_is_null(o):
        return null()
    o = pcc_gc_note_relocation_read(o)
    tag: int = _type_of(o)
    if tag == PY_TYPE_STR:  # PY_TYPE_STR
        py_incref(o)
        return o
    if tag == PY_TYPE_INT:  # PY_TYPE_INT
        return py_int_to_str_obj(o)
    if tag == PY_TYPE_EXC:  # PY_TYPE_EXC
        msg = py_exc_get_message(o)
        if not ptr_is_null(msg):
            # KeyError.__str__ is repr(key), not the bare key (CPython):
            # str(KeyError('x')) == "'x'".
            if py_exc_matches(o, py_exc_builtin_class(4)) != 0:  # PY_EXC_KEYERROR
                return py_obj_repr(msg)
            py_incref(msg)
            return msg
        return null()
    built = _format_builtin_str(o, tag)
    if not ptr_is_null(built):
        return built
    # A cext object (numpy scalar/ndarray) has no Python __str__; its text
    # comes from tp_repr. Only the print formatter had this fallback, so
    # print(x) worked while str(x) returned NULL and concatenation rendered
    # "<null>".
    if pcc_capi_is_cext_type_tag(tag) != 0:
        cext = pcc_capi_cext_object_repr(o)
        if not ptr_is_null(cext):
            return cext
    dunder = py_user_str_dispatch(o)
    if not ptr_is_null(dunder):
        return dunder
    if py_err_occurred() != 0:
        return null()
    # A user exception subclass instance with no __str__ uses BaseException
    # __str__: the message from ``args`` (args[0] if one, "" if none, the
    # tuple repr otherwise). super().__init__(*args) stores ``args``.
    exc_base = py_exc_builtin_class(0)  # PY_EXC_BASE
    if not ptr_is_null(exc_base):
        if py_isinstance(o, exc_base) != 0:
            args = py_instance_getattr(o, cstr("args"))
            if ptr_is_null(args):
                if py_err_occurred() != 0:
                    py_clear_exception()
                return py_str_new(cstr(""), 0)
            if _type_of(args) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
                n: int = py_tuple_len(args)
                if n == 0:
                    return py_str_new(cstr(""), 0)
                if n == 1:
                    return py_obj_str(py_tuple_get(args, 0))
                return py_obj_repr(args)
            return py_obj_str(args)
    # No user __str__: object.__str__ falls back to __repr__.
    return py_obj_repr(o)
