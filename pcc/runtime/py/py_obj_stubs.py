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

from pcc.runtime.py.py_abi_constants import PY_TYPE_ELLIPSIS, PY_TYPE_NOTIMPLEMENTED
from pcc.runtime.py.py_abi_constants import (
    PY_FLAG_EXC_UNICODE_PAYLOAD,
    PY_FLAG_EXC_OS_PAYLOAD,
    PY_FLAG_GC_PINNED,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYMEMORYVIEWOBJECT_BASE_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_CLASS,
    PY_TYPE_COMPLEX,
    PY_TYPE_DICT,
    PY_TYPE_EXC,
    PY_TYPE_FLOAT,
    PY_TYPE_FUNC,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_LIST,
    PY_TYPE_MEMORYVIEW,
    PY_TYPE_NONE,
    PY_TYPE_SET,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
    PY_TYPE_USER_CLASS_START,
    PYCLASSOBJECT_NAME_OFFSET,
    PYINSTANCEOBJECT_CLS_OFFSET,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
)

from pcc.extern import extern, c_abi_export, c_ptr, c_double, c_int32, c_int64, c_void
from pcc.unsafe import (
    atomic_load_i64,
    atomic_rmw_i64,
    define_global_i32,
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
    memcpy,
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
    strlen,
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
py_slice_index_i64 = extern("py_slice_index_i64", (c_ptr, c_int64), c_int64)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_to_str_obj = extern("py_int_to_str_obj", (c_ptr,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_payload = extern("py_str_payload", (c_ptr,), c_ptr)
py_str_exact_copy = extern("py_str_exact_copy", (c_ptr,), c_ptr)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_user_str_dispatch = extern("py_user_str_dispatch", (c_ptr,), c_ptr)
pcc_capi_is_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_capi_cext_object_repr = extern("pcc_capi_cext_object_repr", (c_ptr,), c_ptr)
py_user_repr_dispatch = extern("py_user_repr_dispatch", (c_ptr,), c_ptr)
py_class_attrs_dict = extern("py_class_attrs_dict", (c_ptr, c_int64), c_ptr)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_type_builtin = extern("py_type_builtin", (c_ptr,), c_ptr)
py_obj_id = extern("py_obj_id", (c_ptr,), c_int64)
py_func_display_name = extern("py_func_display_name", (c_ptr,), c_ptr)
py_func_kind = extern("py_func_kind", (c_ptr,), c_int64)
py_bound_method_function = extern("py_bound_method_function", (c_ptr,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_isinstance = extern("py_isinstance", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_unicode_error_format = extern("py_unicode_error_format", (c_ptr, c_int64), c_ptr)
py_os_error_format = extern("py_os_error_format", (c_ptr, c_int64), c_ptr)
py_exc_repr = extern("py_exc_repr", (c_ptr,), c_ptr)
py_exc_get_args = extern("py_exc_get_args", (c_ptr,), c_ptr)
py_complex_repr = extern("py_complex_repr", (c_ptr,), c_ptr)
py_instance_getattr = extern("py_instance_getattr", (c_ptr, c_ptr), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_mem_alloc = extern("py_mem_alloc", (c_int64,), c_ptr)
py_mem_free = extern("py_mem_free", (c_ptr,), c_void)
pcc_errno_message_into = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
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
    if tag == PY_TYPE_BOOL:  # Header-only immutable singletons have no value payload.
        return 1.0 if ptr_eq(o, global_load_ptr("py_True")) != 0 else 0.0
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
    """float.__round__(ndigits) as a NEW float; OverflowError, as CPython's
    double_round, when a finite value rounds past the double range."""
    rounded: float = pcc_float_round_fixed_f64(v, ndigits)
    if rounded != 0.0 and rounded == rounded * 2.0 and (v == 0.0 or v != v * 2.0):
        py_raise_owned(py_exc_new(15, cstr("rounded value too large to represent")))
        return null()
    return py_float_from_f64(rounded)


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
        return 1.0 if ptr_eq(o, global_load_ptr("py_True")) != 0 else 0.0
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


def _f64_is_nan(x: float) -> int:
    if x != x:
        return 1
    return 0


def _f64_is_inf(x: float) -> int:
    if x != x:
        return 0
    if x - x != 0.0:
        return 1
    return 0


def _f64_abs(x: float) -> float:
    if x < 0.0:
        return 0.0 - x
    return x


def _f64_integer_kind(x: float) -> int:
    """For a finite ``x``: 0 when not an integer, 1 even integer, 2 odd."""
    if _f64_abs(x) >= 9007199254740992.0:
        # Every double at or above 2**53 is an even integer.
        return 1
    whole: int = float_to_i64(x)
    if float(whole) != x:
        return 0
    if (whole & 1) != 0:
        return 2
    return 1


def _float_pow_overflow():
    """CPython's ``PyErr_SetFromErrno(PyExc_OverflowError)`` for ERANGE."""
    message = stack_alloc(256)
    pcc_errno_message_into(34, message, 256)
    count: int = strlen(message)
    text = py_mem_alloc(count + 16)
    if ptr_is_null(text) != 0:
        py_raise_owned(py_exc_new(15, message))
        return null()
    memcpy(text, cstr("(34, '"), 6)
    memcpy(ptr_add(text, 6), message, count)
    store_i8(text, 6 + count, 39)
    store_i8(text, 7 + count, 41)
    store_i8(text, 8 + count, 0)
    error = py_exc_new(15, text)
    py_mem_free(text)
    if ptr_is_null(error) != 0:
        return null()
    args = py_tuple_new(2)
    if ptr_is_null(args) == 0:
        number = py_int_from_i64(34)
        py_tuple_set_item(args, 0, number)
        py_decref(number)
        words = py_str_new(message, count)
        py_tuple_set_item(args, 1, words)
        py_decref(words)
        py_obj_setattr(error, cstr("args"), args)
        py_decref(args)
    py_raise_owned(error)
    return null()


@c_abi_export("py_float_pow")
def py_float_pow(a, b):
    """CPython's float_pow for numeric operands, one of them a float.

    An int raised to a negative int is computed here too, as CPython's
    int_pow does: special values, the sign of zero, negative bases with
    fractional exponents (a complex result) and overflow follow CPython.
    """
    iv: float = py_float_to_f64(a)
    iw: float = py_float_to_f64(b)
    if py_err_occurred() != 0:
        return null()
    if iw == 0.0:
        return py_float_from_f64(1.0)
    if _f64_is_nan(iv) != 0:
        return py_float_from_f64(iv)
    if _f64_is_nan(iw) != 0:
        if iv == 1.0:
            return py_float_from_f64(1.0)
        return py_float_from_f64(iw)
    if _f64_is_inf(iw) != 0:
        magnitude: float = _f64_abs(iv)
        if magnitude == 1.0:
            return py_float_from_f64(1.0)
        if (iw > 0.0) == (magnitude > 1.0):
            return py_float_from_f64(_f64_abs(iw))
        return py_float_from_f64(0.0)
    if _f64_is_inf(iv) != 0:
        odd: int = _f64_integer_kind(iw)
        if iw > 0.0:
            if odd == 2:
                return py_float_from_f64(iv)
            return py_float_from_f64(_f64_abs(iv))
        if odd == 2 and iv < 0.0:
            return py_float_from_f64(0.0 * -1.0)
        return py_float_from_f64(0.0)
    if iv == 0.0:
        if iw < 0.0:
            py_raise_owned(py_exc_new(9, cstr("zero to a negative power")))
            return null()
        if _f64_integer_kind(iw) == 2:
            return py_float_from_f64(iv)
        return py_float_from_f64(0.0)
    negate: int = 0
    if iv < 0.0:
        kind: int = _f64_integer_kind(iw)
        if kind == 0:
            # A negative number to a fractional power is complex.
            return py_complex_pow(a, b)
        iv = 0.0 - iv
        if kind == 2:
            negate = 1
    if iv == 1.0:
        if negate != 0:
            return py_float_from_f64(-1.0)
        return py_float_from_f64(1.0)
    result: float = pow_c(iv, iw)
    if _f64_is_inf(result) != 0:
        return _float_pow_overflow()
    if negate != 0:
        result = 0.0 - result
    return py_float_from_f64(result)


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


# Conversion owns input, unwrapped payload base, element/index temporaries,
# output and suspended error independently, including aliases among them.
_BUFFER_SOURCE = 0
_BUFFER_LEAF = 1
_BUFFER_ITEM = 2
_BUFFER_INDEX = 3
_BUFFER_RESULT = 4
_BUFFER_ERROR = 5
_BUFFER_COUNT = 6
_BUFFER_BYTES = 8

define_global_i32("pcc_buffer_borrowed_map", -1)
define_global_i32("pcc_buffer_owned_map", 6)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_obj_index_slots = extern("py_obj_index_slots", (c_ptr, c_ptr), c_int64)


def _buffer_error(kind: int, message) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _buffer_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * _BUFFER_BYTES)
    token: int = load_i64(tokens, index * _BUFFER_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * _BUFFER_BYTES, -1)
    pcc_gc_store_root(slot, null())


def _buffer_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * _BUFFER_BYTES)
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        return _buffer_error(19, cstr("buffer conversion: out of memory"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * _BUFFER_BYTES, token)
    if token < 0:
        return _buffer_error(19, cstr("buffer conversion: cannot lease owner"))
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    if py_err_occurred() != 0:
        return -1
    return 0


def _buffer_copy(slots, tokens, index: int, source, borrowed: int) -> int:
    destination = ptr_add(slots, index * _BUFFER_BYTES)
    token: int = -1
    if borrowed != 0:
        token = pcc_gc_root_copy_borrowed_lease(destination, source)
    else:
        token = pcc_gc_root_copy_lease(destination, source)
    store_i64(tokens, index * _BUFFER_BYTES, token)
    if token < 0:
        return _buffer_error(19, cstr("buffer conversion: cannot retain owner"))
    return 0


def _buffer_new_raw(slots, tokens, data, count: int, mutable: int) -> int:
    result = ptr_add(slots, _BUFFER_RESULT * _BUFFER_BYTES)
    if mutable != 0:
        store_ptr(result, 0, _bytearray_new_raw(data, count))
    else:
        store_ptr(result, 0, py_bytes_new(data, count))
    return _buffer_adopt(slots, tokens, _BUFFER_RESULT)


def _buffer_count_fill(slots, tokens, mutable: int) -> int:
    source = ptr_add(slots, _BUFFER_SOURCE * _BUFFER_BYTES)
    index = ptr_add(slots, _BUFFER_INDEX * _BUFFER_BYTES)
    if py_obj_index_slots(source, index) != 0:
        return -1
    if _buffer_adopt(slots, tokens, _BUFFER_INDEX) != 0:
        return -1
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    count: int = py_int_to_i64(load_ptr(index, 0), overflow)
    if py_err_occurred() != 0:
        return -1
    if load_i32(overflow, 0) != 0:
        return _buffer_error(15, cstr("byte count does not fit in an index-sized integer"))
    if count < 0:
        return _buffer_error(2, cstr("negative count"))
    if count > 9223372036854775782:
        kind: int = 15
        if mutable != 0:
            kind = 19
        return _buffer_error(kind, cstr("byte count is too large"))
    status: int = _buffer_new_raw(slots, tokens, null(), count, mutable)
    if status == 0 and count > 0:
        result = load_ptr(slots, _BUFFER_RESULT * _BUFFER_BYTES)
        memset(ptr_add(result, PYBYTESOBJECT_DATA_OFFSET), 0, count)
    return status


def _buffer_sequence_fill(slots, tokens, mutable: int) -> int:
    source = ptr_add(slots, _BUFFER_SOURCE * _BUFFER_BYTES)
    item = ptr_add(slots, _BUFFER_ITEM * _BUFFER_BYTES)
    index = ptr_add(slots, _BUFFER_INDEX * _BUFFER_BYTES)
    tag: int = _type_of(load_ptr(source, 0))
    count: int = 0
    if tag == PY_TYPE_LIST:
        count = py_list_len(load_ptr(source, 0))
    else:
        count = py_tuple_len(load_ptr(source, 0))
    if py_err_occurred() != 0:
        return -1
    if count <= 0:
        return _buffer_new_raw(slots, tokens, null(), 0, mutable)
    data = py_mem_alloc(count)
    if ptr_is_null(data) != 0:
        return _buffer_error(19, cstr("byte sequence: out of memory"))
    status: int = 0
    current: int = 0
    overflow = stack_alloc(4)
    while current < count and status == 0:
        if tag == PY_TYPE_LIST:
            store_ptr(item, 0, py_list_get(load_ptr(source, 0), current))
        else:
            store_ptr(item, 0, py_tuple_get(load_ptr(source, 0), current))
        status = _buffer_adopt(slots, tokens, _BUFFER_ITEM)
        if status == 0:
            status = py_obj_index_slots(item, index)
        if status == 0:
            status = _buffer_adopt(slots, tokens, _BUFFER_INDEX)
        if status == 0:
            store_i32(overflow, 0, 0)
            byte: int = py_int_to_i64(load_ptr(index, 0), overflow)
            if py_err_occurred() != 0:
                status = -1
            elif load_i32(overflow, 0) != 0 or byte < 0 or byte > 255:
                status = _buffer_error(2, cstr("bytes must be in range(0, 256)"))
            else:
                store_i8(data, current, byte)
        if status == 0:
            _buffer_drop(slots, tokens, _BUFFER_INDEX)
            _buffer_drop(slots, tokens, _BUFFER_ITEM)
        current = current + 1
    if status == 0:
        status = _buffer_new_raw(slots, tokens, data, count, mutable)
    # Result/error remains in an owning root when freeing the unmanaged copy
    # buffer parks or runs diagnostics; a raw result never crosses this call.
    error = ptr_add(slots, _BUFFER_ERROR * _BUFFER_BYTES)
    py_tls_exc_swap_slot(error)
    py_mem_free(data)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    return status


def _buffer_payload_fill(slots, tokens, mutable: int) -> int:
    source = ptr_add(slots, _BUFFER_SOURCE * _BUFFER_BYTES)
    leaf = ptr_add(slots, _BUFFER_LEAF * _BUFFER_BYTES)
    item = ptr_add(slots, _BUFFER_ITEM * _BUFFER_BYTES)
    if _buffer_copy(slots, tokens, _BUFFER_LEAF, source, 0) != 0:
        return -1
    while _type_of(load_ptr(leaf, 0)) == PY_TYPE_MEMORYVIEW:
        # The current view's address is leased, so its actual managed base
        # field remains an authoritative source slot throughout the retain.
        base_slot = ptr_add(load_ptr(leaf, 0), PYMEMORYVIEWOBJECT_BASE_OFFSET)
        if _buffer_copy(slots, tokens, _BUFFER_ITEM, base_slot, 1) != 0:
            return -1
        if ptr_is_null(load_ptr(item, 0)) != 0:
            return _buffer_error(3, cstr("memoryview has no buffer owner"))
        _buffer_drop(slots, tokens, _BUFFER_LEAF)
        if pcc_gc_root_move(leaf, item) != 0:
            return _buffer_error(7, cstr("cannot transfer memoryview buffer owner"))
        token: int = load_i64(tokens, _BUFFER_ITEM * _BUFFER_BYTES)
        store_i64(tokens, _BUFFER_ITEM * _BUFFER_BYTES, -1)
        store_i64(tokens, _BUFFER_LEAF * _BUFFER_BYTES, token)
    # This is the payload's actual owner, not just an outer memoryview. Its
    # independent address lease covers data/length reads and allocating copy.
    current = load_ptr(leaf, 0)
    data = _bytes_data(current)
    count: int = py_bytes_len(load_ptr(leaf, 0))
    if py_err_occurred() != 0:
        return -1
    return _buffer_new_raw(slots, tokens, data, count, mutable)


def _buffer_fill(slots, tokens, mode: int) -> int:
    source = ptr_add(slots, _BUFFER_SOURCE * _BUFFER_BYTES)
    result = ptr_add(slots, _BUFFER_RESULT * _BUFFER_BYTES)
    if mode == 2:
        store_ptr(result, 0, pcc_gc_alloc(32, PY_TYPE_MEMORYVIEW, 0))
        if ptr_is_null(load_ptr(result, 0)) != 0:
            return _buffer_error(19, cstr("memoryview: out of memory"))
        # Finish the traceable empty layout before acquiring a parking lease.
        view = load_ptr(result, 0)
        store_i64(view, 0, 1)
        store_i32(view, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_MEMORYVIEW)
        store_ptr(view, PYMEMORYVIEWOBJECT_BASE_OFFSET, null())
        store_ptr(view, 24, null())
        if _buffer_adopt(slots, tokens, _BUFFER_RESULT) != 0:
            return -1
        view = load_ptr(result, 0)
        pcc_gc_store_ptr(view, ptr_add(view, PYMEMORYVIEWOBJECT_BASE_OFFSET), load_ptr(source, 0))
        if py_err_occurred() != 0:
            return -1
        pcc_gc_publish_initialized(load_ptr(result, 0))
        return 0
    if ptr_is_null(load_ptr(source, 0)) != 0:
        return _buffer_new_raw(slots, tokens, null(), 0, mode)
    tag: int = _type_of(load_ptr(source, 0))
    if tag == PY_TYPE_BYTES and mode == 0:
        # Exact immutable bytes retain identity with a separate result owner.
        return _buffer_copy(slots, tokens, _BUFFER_RESULT, source, 0)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return _buffer_count_fill(slots, tokens, mode)
    if tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE:
        return _buffer_sequence_fill(slots, tokens, mode)
    return _buffer_payload_fill(slots, tokens, mode)


def _buffer_convert_owned(obj, mode: int):
    borrowed = stack_alloc(_BUFFER_BYTES)
    store_ptr(borrowed, 0, obj)
    pcc_gc_frame_enter(global_addr("pcc_buffer_borrowed_map"), borrowed)
    slots = stack_alloc(_BUFFER_COUNT * _BUFFER_BYTES)
    tokens = stack_alloc(_BUFFER_COUNT * _BUFFER_BYTES)
    memset(slots, 0, _BUFFER_COUNT * _BUFFER_BYTES)
    index: int = 0
    while index < _BUFFER_COUNT:
        store_i64(tokens, index * _BUFFER_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_buffer_owned_map"), slots)
    status: int = _buffer_copy(slots, tokens, _BUFFER_SOURCE, borrowed, 1)
    if status == 0:
        status = _buffer_fill(slots, tokens, mode)
    result = ptr_add(slots, _BUFFER_RESULT * _BUFFER_BYTES)
    error = ptr_add(slots, _BUFFER_ERROR * _BUFFER_BYTES)
    py_tls_exc_swap_slot(error)
    store_ptr(borrowed, 0, null())
    if status != 0:
        _buffer_drop(slots, tokens, _BUFFER_RESULT)
    index = _BUFFER_INDEX
    while index >= 0:
        _buffer_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token = load_i64(tokens, _BUFFER_RESULT * _BUFFER_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(result, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


@c_abi_export("py_bytes_new")
def py_bytes_new(data, byte_len: int):
    if byte_len < 0:
        byte_len = 0
    p = pcc_gc_alloc(24 + byte_len + 1, PY_TYPE_BYTES, 0)
    if ptr_is_null(p):
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("bytes: out of memory")))
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_BYTES)  # PY_TYPE_BYTES
    store_i64(p, 16, byte_len)
    if not ptr_is_null(data) and byte_len > 0:
        memcpy(ptr_add(p, 24), data, byte_len)
    store_i8(p, 24 + byte_len, 0)
    return p


def _bytearray_new_raw(data, byte_len: int):
    if byte_len < 0:
        byte_len = 0
    p = pcc_gc_alloc(24 + byte_len + 1, PY_TYPE_BYTEARRAY, 0)
    if ptr_is_null(p):
        if py_err_occurred() == 0:
            py_raise_owned(py_exc_new(19, cstr("bytearray: out of memory")))
        return null()
    store_i64(p, 0, 1)
    store_i32(p, 8, PY_TYPE_BYTEARRAY)  # PY_TYPE_BYTEARRAY
    store_i64(p, 16, byte_len)
    if not ptr_is_null(data) and byte_len > 0:
        memcpy(ptr_add(p, 24), data, byte_len)
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


@c_abi_export("py_bytes_data_ptr")
def py_bytes_data_ptr(o):
    """Payload pointer of a bytes/bytearray/memoryview, or NULL.

    Exported so other runtime modules do not have to restate the object
    layout offset; `py_hash_runtime`'s SHA-256 entry points need it.
    """
    return _bytes_data(o)


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


def _byte_in_set(set_data, set_len: int, c: int) -> int:
    i: int = 0
    while i < set_len:
        if (load_i8(set_data, i) & 255) == c:
            return 1
        i = i + 1
    return 0


@c_abi_export("py_bytes_strip_chars")
def py_bytes_strip_chars(o, chars):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    cdata = _bytes_data(chars)
    if ptr_is_null(cdata):
        return null()
    n: int = py_bytes_len(o)
    cn: int = py_bytes_len(chars)
    lo: int = 0
    hi: int = n
    while lo < hi and _byte_in_set(cdata, cn, load_i8(data, lo) & 255) != 0:
        lo = lo + 1
    while hi > lo and _byte_in_set(cdata, cn, load_i8(data, hi - 1) & 255) != 0:
        hi = hi - 1
    return _bytes_new_same_family(o, ptr_add(data, lo), hi - lo)


@c_abi_export("py_bytes_lstrip_chars")
def py_bytes_lstrip_chars(o, chars):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    cdata = _bytes_data(chars)
    if ptr_is_null(cdata):
        return null()
    n: int = py_bytes_len(o)
    cn: int = py_bytes_len(chars)
    lo: int = 0
    while lo < n and _byte_in_set(cdata, cn, load_i8(data, lo) & 255) != 0:
        lo = lo + 1
    return _bytes_new_same_family(o, ptr_add(data, lo), n - lo)


@c_abi_export("py_bytes_rstrip_chars")
def py_bytes_rstrip_chars(o, chars):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    cdata = _bytes_data(chars)
    if ptr_is_null(cdata):
        return null()
    n: int = py_bytes_len(o)
    cn: int = py_bytes_len(chars)
    hi: int = n
    while hi > 0 and _byte_in_set(cdata, cn, load_i8(data, hi - 1) & 255) != 0:
        hi = hi - 1
    return _bytes_new_same_family(o, data, hi)


def _bytes_fill_byte(fill) -> int:
    # `ljust`/`rjust` pad with a single byte; the default is ASCII space.
    if ptr_is_null(fill):
        return 32
    data = _bytes_data(fill)
    if ptr_is_null(data):
        return 32
    if py_bytes_len(fill) != 1:
        return -1
    return load_i8(data, 0) & 255


@c_abi_export("py_bytes_ljust")
def py_bytes_ljust(o, width: int, fill):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    target: int = width
    pad: int = _bytes_fill_byte(fill)
    if pad < 0:
        py_raise_owned(py_exc_new(3, cstr("ljust fill must be a single byte")))
        return null()
    if target <= n:
        return _bytes_new_same_family(o, data, n)
    out = _bytes_new_same_family(o, null(), target)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    i: int = 0
    while i < n:
        store_i8(dst, i, load_i8(data, i))
        i = i + 1
    while i < target:
        store_i8(dst, i, pad)
        i = i + 1
    store_i8(dst, target, 0)
    return out


@c_abi_export("py_bytes_rjust")
def py_bytes_rjust(o, width: int, fill):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    target: int = width
    pad: int = _bytes_fill_byte(fill)
    if pad < 0:
        py_raise_owned(py_exc_new(3, cstr("rjust fill must be a single byte")))
        return null()
    if target <= n:
        return _bytes_new_same_family(o, data, n)
    out = _bytes_new_same_family(o, null(), target)
    if ptr_is_null(out):
        return null()
    dst = _bytes_data(out)
    if ptr_is_null(dst):
        return null()
    lead: int = target - n
    i: int = 0
    while i < lead:
        store_i8(dst, i, pad)
        i = i + 1
    j: int = 0
    while j < n:
        store_i8(dst, lead + j, load_i8(data, j))
        j = j + 1
    store_i8(dst, target, 0)
    return out


@c_abi_export("py_bytes_lstrip")
def py_bytes_lstrip(o):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    lo: int = 0
    while lo < n and _is_ascii_space(load_i8(data, lo) & 255) != 0:
        lo = lo + 1
    return _bytes_new_same_family(o, ptr_add(data, lo), n - lo)


@c_abi_export("py_bytes_rstrip")
def py_bytes_rstrip(o):
    data = _bytes_data(o)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(o)
    hi: int = n
    while hi > 0 and _is_ascii_space(load_i8(data, hi - 1) & 255) != 0:
        hi = hi - 1
    return _bytes_new_same_family(o, data, hi)


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
    mode: int = 0
    if as_bytearray != 0:
        mode = 1
    return _buffer_convert_owned(seq, mode)


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


def _raise_int_bytes(kind: int, message) -> None:
    py_raise_owned(py_exc_new(kind, message))


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


@c_abi_export("py_bytes_find_from")
def py_bytes_find_from(src, needle, start) -> int:
    begin: int = py_int_value_i64(start)
    return _bytes_find_from_offset(src, needle, begin)


@c_abi_export("py_bytes_find")
def py_bytes_find(src, needle) -> int:
    return _bytes_find_from_offset(src, needle, 0)


def _bytes_find_from_offset(src, needle, begin: int) -> int:
    return py_bytes_find_range(src, needle, begin, py_bytes_len(src))


@c_abi_export("py_bytes_find_range")
def py_bytes_find_range(src, needle, begin: int, end: int) -> int:
    # Search the borrowed payload directly. Copying a suffix per search makes
    # repeated searches quadratic in copied bytes and requires a separate owner.
    needle_data = _bytes_data(needle)
    byte: int = -1
    tag: int = _type_of(needle)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        byte = py_slice_index_i64(needle, 0)
        if byte < 0 or byte > 255:
            py_raise_owned(py_exc_new(2, cstr("byte must be in range(0, 256)")))
            return -1
    elif ptr_is_null(needle_data):
        py_raise_owned(py_exc_new(3, cstr("argument must be an integer or bytes-like object")))
        return -1
    data = _bytes_data(src)
    if ptr_is_null(data):
        return -1
    n: int = py_bytes_len(src)
    if begin < 0:
        begin = n + begin
        if begin < 0:
            begin = 0
    if end < 0:
        end = n + end
        if end < 0:
            end = 0
    if end > n:
        end = n
    if begin > n or begin > end:
        return -1

    if byte >= 0:
        i: int = begin
        while i < end:
            if (load_i8(data, i) & 255) == byte:
                return i
            i = i + 1
        return -1

    needle_n: int = py_bytes_len(needle)
    if needle_n == 0:
        return begin
    if needle_n > end - begin:
        return -1
    last: int = end - needle_n
    i = begin
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


@c_abi_export("py_bytes_split_max")
def py_bytes_split_max(src, sep, maxsplit):
    # `bytes.split(sep, maxsplit)`: stop after `maxsplit` cuts and keep the
    # rest as the final piece.  A negative `maxsplit` means unbounded, as in
    # CPython.  pcc1 needs it for `raw.split(b"\0", 1)[0]` in macho_spec.
    data = _bytes_data(src)
    if ptr_is_null(data):
        return null()
    n: int = py_bytes_len(src)
    sep_data = _bytes_data(sep)
    if ptr_is_null(sep_data):
        return null()
    sep_n: int = py_bytes_len(sep)
    if sep_n == 0:
        py_raise_owned(py_exc_new(2, cstr("empty separator")))
        return null()
    limit: int = py_int_value_i64(maxsplit)
    out = py_list_new(4)
    if ptr_is_null(out):
        return null()
    start: int = 0
    i: int = 0
    cuts: int = 0
    while i + sep_n <= n:
        if limit >= 0 and cuts >= limit:
            break
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
            cuts = cuts + 1
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
    mode: int = 0
    if mutable != 0:
        mode = 1
    return _buffer_convert_owned(o, mode)


@c_abi_export("py_bytearray_from_obj")
def py_bytearray_from_obj(o):
    return _buffer_convert_owned(o, 1)


@c_abi_export("py_bytes_from_obj")
def py_bytes_from_obj(o):
    return _buffer_convert_owned(o, 0)


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
            if py_err_occurred() != 0:
                return null()
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
    if an > 0:
        memcpy(dst, ad, an)
    if bn > 0:
        memcpy(ptr_add(dst, an), bd, bn)
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
    if an > 0:
        memcpy(dst, ad, an)
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
    if at > 0:
        memcpy(dst, ad, at)
    store_i8(dst, at, byte)
    if an > at:
        memcpy(ptr_add(dst, at + 1), ptr_add(ad, at), an - at)
    store_i8(dst, total, 0)
    return out


@c_abi_export("py_bytearray_clear")
def py_bytearray_clear(o) -> None:
    # bytearray.clear(): zero byte_len (offset 16) in place, keeping the
    # buffer and its NUL terminator.
    if ptr_is_null(o) != 0 or _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(py_exc_new(3, cstr("clear() requires a bytearray")))
        return
    data = _bytes_data(o)
    store_i64(o, 16, 0)
    if ptr_is_null(data) == 0:
        store_i8(data, 0, 0)


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
    return _buffer_convert_owned(o, 2)


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


# Text codecs share one incremental byte decoder. All input/output buffers in
# this primitive are native allocations, or views covered by a caller lease.
# Result record: native allocation at +0, byte length at +8, consumed input at
# +16. Ownership of the allocation transfers only after a zero status.
# An optional span list records every error-handler output unit so a caller
# can map decoded text back to input offsets; ignore emits no text and
# backslashreplace several characters per byte, so widths cannot be inferred.
_TEXT_SPANS_RECORDS = 0
_TEXT_SPANS_COUNT = 8
_TEXT_SPANS_CAPACITY = 16
_TEXT_SPAN_BYTES = 24
_TEXT_SPAN_OUT_START = 0
_TEXT_SPAN_OUT_LENGTH = 8
_TEXT_SPAN_RAW_END = 16
py_unicode_decode_error_from_buffer = extern(
    "py_unicode_decode_error_from_buffer",
    (c_ptr, c_int64, c_ptr, c_int64, c_int64, c_ptr), c_void,
)
py_text_decode_codec_id = extern("py_text_decode_codec_id", (c_ptr,), c_int64)
py_text_error_id = extern("py_text_error_id", (c_ptr,), c_int64)
py_text_encode_ids = extern("py_text_encode_ids", (c_ptr, c_int64, c_int64), c_ptr)


def _text_utf8_expected(first: int) -> int:
    if first < 128:
        return 1
    if first >= 194 and first <= 223:
        return 2
    if first >= 224 and first <= 239:
        return 3
    if first >= 240 and first <= 244:
        return 4
    return 0


def _text_decode_step(data, count: int, start: int, codec: int, mode: int, final: int) -> int:
    """Positive width, negative invalid-prefix width, or incomplete zero."""
    first: int = load_i8(data, start) & 255
    if first < 128 or codec == 2:
        return 1
    if codec == 1:
        return -1
    width: int = _text_utf8_expected(first)
    if width == 0:
        return -1
    offset: int = 1
    while offset < width:
        if start + offset >= count:
            if final == 0:
                return 0
            return -offset
        byte: int = load_i8(data, start + offset) & 255
        low: int = 128
        high: int = 191
        if offset == 1:
            if first == 224:
                low = 160
            elif first == 237:
                high = 159
                if mode == 4 and start + 2 < count:
                    last: int = load_i8(data, start + 2) & 255
                    if last >= 128 and last <= 191:
                        high = 191
            elif first == 240:
                low = 144
            elif first == 244:
                high = 143
        # CPython leaves a two-byte surrogate prefix undecided until the
        # third byte arrives, even for strict/ignore/replace error policies.
        if first == 237 and offset == 1 and byte >= 160 and byte <= 191:
            if final == 0 and start + 2 == count:
                return 0
        if byte < low or byte > high:
            return -offset
        offset = offset + 1
    return width


def _text_decode_failure(data, count: int, start: int, end: int, codec: int) -> None:
    encoding = cstr("utf-8")
    reason = cstr("invalid continuation byte")
    if codec == 1:
        encoding = cstr("ascii")
        reason = cstr("ordinal not in range(128)")
    elif _text_utf8_expected(load_i8(data, start) & 255) == 0:
        reason = cstr("invalid start byte")
    elif end == count:
        reason = cstr("unexpected end of data")
    py_unicode_decode_error_from_buffer(data, count, encoding, start, end, reason)


def _text_decode_record(spans, out_start: int, out_length: int, raw_end: int) -> int:
    if ptr_is_null(spans):
        return 0
    count: int = load_i64(spans, _TEXT_SPANS_COUNT)
    records = load_ptr(spans, _TEXT_SPANS_RECORDS)
    if count == load_i64(spans, _TEXT_SPANS_CAPACITY):
        capacity: int = 16 if count == 0 else count * 2
        grown = py_mem_alloc(capacity * _TEXT_SPAN_BYTES)
        if ptr_is_null(grown):
            return -1
        if count != 0:
            memcpy(grown, records, count * _TEXT_SPAN_BYTES)
            py_mem_free(records)
        records = grown
        store_ptr(spans, _TEXT_SPANS_RECORDS, records)
        store_i64(spans, _TEXT_SPANS_CAPACITY, capacity)
    entry = ptr_add(records, count * _TEXT_SPAN_BYTES)
    store_i64(entry, _TEXT_SPAN_OUT_START, out_start)
    store_i64(entry, _TEXT_SPAN_OUT_LENGTH, out_length)
    store_i64(entry, _TEXT_SPAN_RAW_END, raw_end)
    store_i64(spans, _TEXT_SPANS_COUNT, count + 1)
    return 0


@c_abi_export("py_text_decode_buffer")
def py_text_decode_buffer(data, count: int, codec: int, mode: int, final: int, result) -> int:
    return py_text_decode_buffer_spans(data, count, codec, mode, final, result, null())


@c_abi_export("py_text_decode_buffer_spans")
def py_text_decode_buffer_spans(data, count: int, codec: int, mode: int, final: int, result, spans) -> int:
    """Decode like py_text_decode_buffer; a non-null span list receives the
    error-handler units. The caller owns its records on every status."""
    store_ptr(result, 0, null())
    store_i64(result, 8, 0)
    store_i64(result, 16, 0)
    if codec < 0 or codec > 2:
        py_raise_owned(py_exc_new(13, cstr("unknown or unsupported native encoding")))
        return -1
    if count < 0 or count > 2305843009213693951:
        py_raise_owned(py_exc_new(15, cstr("decoded string is too long")))
        return -1
    output = py_mem_alloc(count * 4 + 1)
    if ptr_is_null(output):
        py_raise_owned(py_exc_new(19, cstr("cannot allocate decoded string")))
        return -1
    position: int = 0
    used: int = 0
    while position < count:
        width: int = _text_decode_step(data, count, position, codec, mode, final)
        if width == 0:
            break
        if width > 0:
            first: int = load_i8(data, position) & 255
            if codec == 2 and first >= 128:
                store_i8(output, used, 192 | (first >> 6))
                store_i8(output, used + 1, 128 | (first & 63))
                used = used + 2
            else:
                offset: int = 0
                while offset < width:
                    store_i8(output, used, load_i8(data, position + offset))
                    used = used + 1
                    offset = offset + 1
            position = position + width
        else:
            end: int = position - width
            recorded: int = 0
            if mode == 1:
                recorded = _text_decode_record(spans, used, 0, end)
            elif mode == 2:
                recorded = _text_decode_record(spans, used, 3, end)
                store_i8(output, used, 239)
                store_i8(output, used + 1, 191)
                store_i8(output, used + 2, 189)
                used = used + 3
            elif mode == 3:
                offset = position
                while offset < end and recorded == 0:
                    byte: int = load_i8(data, offset) & 255
                    recorded = _text_decode_record(spans, used, 3, offset + 1)
                    store_i8(output, used, 237)
                    store_i8(output, used + 1, 176 | (byte >> 6))
                    store_i8(output, used + 2, 128 | (byte & 63))
                    used = used + 3
                    offset = offset + 1
            elif mode == 5:
                offset = position
                while offset < end and recorded == 0:
                    byte = load_i8(data, offset) & 255
                    recorded = _text_decode_record(spans, used, 4, offset + 1)
                    store_i8(output, used, 92)
                    store_i8(output, used + 1, 120)
                    digit: int = byte >> 4
                    store_i8(output, used + 2, 48 + digit if digit < 10 else 87 + digit)
                    digit = byte & 15
                    store_i8(output, used + 3, 48 + digit if digit < 10 else 87 + digit)
                    used = used + 4
                    offset = offset + 1
            else:
                py_mem_free(output)
                if mode < 0:
                    py_raise_owned(py_exc_new(13, cstr("unknown error handler name")))
                elif mode == 6 or mode == 7:
                    # Encode-only handlers, as CPython's callback check reports.
                    py_raise_owned(py_exc_new(3, cstr("don't know how to handle UnicodeDecodeError in error callback")))
                else:
                    _text_decode_failure(data, count, position, end, codec)
                return -1
            if recorded != 0:
                py_mem_free(output)
                py_raise_owned(py_exc_new(19, cstr("cannot allocate decoder spans")))
                return -1
            position = end
    store_i8(output, used, 0)
    store_ptr(result, 0, output)
    store_i64(result, 8, used)
    store_i64(result, 16, position)
    return 0


@c_abi_export("py_text_decode")
def py_text_decode(data, count: int, codec: int, mode: int):
    decoded = stack_alloc(24)
    if py_text_decode_buffer(data, count, codec, mode, 1, decoded) != 0:
        return null()
    result = py_str_new(load_ptr(decoded, 0), load_i64(decoded, 8))
    py_mem_free(load_ptr(decoded, 0))
    return result


def _decode_operand_type_error(value) -> int:
    """CPython's "decoding to str: need a bytes-like object, list found"."""
    message = stack_alloc(160)
    prefix = cstr("decoding to str: need a bytes-like object, ")
    at: int = 0
    while load_i8(prefix, at) != 0:
        store_i8(message, at, load_i8(prefix, at))
        at = at + 1
    index: int = 0
    name = py_obj_type_name(value)
    if ptr_is_null(name) == 0:
        text = py_str_utf8(name)
        count: int = py_str_byte_len(name)
        if count > 100:
            count = 100
        while index < count:
            store_i8(message, at, load_i8(text, index))
            at = at + 1
            index = index + 1
        py_decref(name)
    suffix = cstr(" found")
    index = 0
    while load_i8(suffix, index) != 0:
        store_i8(message, at, load_i8(suffix, index))
        at = at + 1
        index = index + 1
    store_i8(message, at, 0)
    return _buffer_error(3, message)


def _bytes_decode_body(slots, tokens, codec: int, mode: int) -> int:
    source = ptr_add(slots, _BUFFER_SOURCE * _BUFFER_BYTES)
    value = load_ptr(source, 0)
    if ptr_is_null(value):
        return _buffer_error(3, cstr("decoding to str: need a bytes-like object"))
    if is_tagged_int(value):
        return _buffer_error(3, cstr("decoding to str: need a bytes-like object, int found"))
    tag: int = _type_of(value)
    if tag == PY_TYPE_STR:
        return _buffer_error(3, cstr("decoding str is not supported"))
    if tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY and tag != PY_TYPE_MEMORYVIEW:
        return _decode_operand_type_error(value)
    if codec < 0:
        codec = py_text_decode_codec_id(load_ptr(slots, _BUFFER_LEAF * _BUFFER_BYTES))
        if py_err_occurred() != 0:
            return -1
        mode = py_text_error_id(load_ptr(slots, _BUFFER_ITEM * _BUFFER_BYTES))
        if py_err_occurred() != 0:
            return -1
    # Snapshot a mutable or indirect buffer using its established leaf-owner
    # protocol. A memoryview's outer lease alone cannot protect its base.
    store_ptr(slots, _BUFFER_INDEX * _BUFFER_BYTES, _buffer_convert_owned(load_ptr(source, 0), 0))
    if _buffer_adopt(slots, tokens, _BUFFER_INDEX) != 0:
        return -1
    payload = load_ptr(slots, _BUFFER_INDEX * _BUFFER_BYTES)
    store_ptr(slots, _BUFFER_RESULT * _BUFFER_BYTES, py_text_decode(_bytes_data(payload), py_bytes_len(payload), codec, mode))
    if ptr_is_null(load_ptr(slots, _BUFFER_RESULT * _BUFFER_BYTES)):
        return -1
    return _buffer_adopt(slots, tokens, _BUFFER_RESULT)


def _bytes_decode_guarded(value, encoding, errors, codec: int, mode: int):
    borrowed = stack_alloc(3 * _BUFFER_BYTES)
    store_ptr(borrowed, 0, value)
    store_ptr(borrowed, 8, encoding)
    store_ptr(borrowed, 16, errors)
    pcc_gc_frame_enter(global_addr("pcc_text_decode_borrowed_map"), borrowed)
    slots = stack_alloc(_BUFFER_COUNT * _BUFFER_BYTES)
    tokens = stack_alloc(_BUFFER_COUNT * _BUFFER_BYTES)
    memset(slots, 0, _BUFFER_COUNT * _BUFFER_BYTES)
    index: int = 0
    while index < _BUFFER_COUNT:
        store_i64(tokens, index * _BUFFER_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_buffer_owned_map"), slots)
    status: int = 0
    index = 0
    while index < 3 and status == 0:
        status = _buffer_copy(slots, tokens, index, ptr_add(borrowed, index * _BUFFER_BYTES), 1)
        index = index + 1
    if status == 0:
        status = _bytes_decode_body(slots, tokens, codec, mode)
    result = ptr_add(slots, _BUFFER_RESULT * _BUFFER_BYTES)
    error = ptr_add(slots, _BUFFER_ERROR * _BUFFER_BYTES)
    py_tls_exc_swap_slot(error)
    memset(borrowed, 0, 3 * _BUFFER_BYTES)
    if status != 0:
        _buffer_drop(slots, tokens, _BUFFER_RESULT)
    index = _BUFFER_INDEX
    while index >= 0:
        _buffer_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_py_gc_minor_graph_lock()
    current = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if not ptr_is_null(current) and not is_tagged_int(current):
        prior = load_i32(current, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(current)
    pcc_py_gc_minor_graph_unlock()
    token: int = load_i64(tokens, _BUFFER_RESULT * _BUFFER_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(result, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


define_global_i32("pcc_text_decode_borrowed_map", -3)


@c_abi_export("py_bytes_decode")
def py_bytes_decode(o):
    return _bytes_decode_guarded(o, null(), null(), 0, 0)


@c_abi_export("py_bytes_decode_utf8_ignore")
def py_bytes_decode_utf8_ignore(o):
    return _bytes_decode_guarded(o, null(), null(), 0, 1)


@c_abi_export("py_bytes_decode_utf8_surrogateescape")
def py_bytes_decode_utf8_surrogateescape(o):
    return _bytes_decode_guarded(o, null(), null(), 0, 3)


@c_abi_export("py_bytes_decode_with_encoding")
def py_bytes_decode_with_encoding(o, encoding, errors):
    return _bytes_decode_guarded(o, encoding, errors, -1, 0)


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
            py_raise_owned(py_exc_new(2, cstr("slice step cannot be zero")))  # PY_EXC_VALUEERROR
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
    if la > 0:
        memcpy(dst, ad, la)
    if lb > 0:
        memcpy(ptr_add(dst, la), bd, lb)
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
            memcpy(ptr_add(dst, off), sd, sep_len)
            off = off + sep_len
        elem_len2: int = py_bytes_len(e)
        ed = _bytes_data(e)
        if elem_len2 > 0:
            memcpy(ptr_add(dst, off), ed, elem_len2)
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
    # Copy the source once, then double the filled prefix; each copy reads
    # only bytes already written, so source and destination never overlap.
    memcpy(dst, data, n)
    filled: int = n
    while filled < total:
        chunk: int = filled
        if chunk > total - filled:
            chunk = total - filled
        memcpy(ptr_add(dst, filled), dst, chunk)
        filled = filled + chunk
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


@c_abi_export("py_bytearray_set_slice")
def py_bytearray_set_slice(o, lo, hi, step, replacement) -> int:
    """``ba[lo:hi:step] = replacement``, in place.

    A bytearray keeps its bytes inline at offset 24 behind an exact-fit
    allocation (``_bytearray_new_raw``), so the stored length can shrink but
    cannot grow.  A step-1 replacement longer than the span it replaces would
    need a reallocation this slot cannot publish -- every other reference
    would keep pointing at the old object -- so it is refused loudly instead
    of silently truncating.  Equal-length and shrinking step-1 stores, and
    extended slices (which CPython requires to match exactly), are all exact.
    """
    if _type_of(o) != PY_TYPE_BYTEARRAY:
        py_raise_owned(
            py_exc_new(
                3, cstr("bytearray slice assignment target must be bytearray")
            )
        )  # PY_EXC_TYPEERROR
        return -1
    length: int = load_i64(o, 16)
    step_v: int = 1
    if _bytes_is_none_or_null(step) == 0:
        step_v = py_int_value_i64(step)
        if step_v == 0:
            py_raise_owned(
                py_exc_new(2, cstr("slice step cannot be zero"))
            )  # PY_EXC_VALUEERROR
            return -1

    src = _bytes_data(replacement)
    tmp = null()
    src_n: int = 0
    if ptr_is_null(src) != 0:
        tag: int = _type_of(replacement)
        if tag == PY_TYPE_LIST or tag == PY_TYPE_TUPLE:
            tmp = _bytes_from_int_sequence(replacement, 1)
            if py_err_occurred() != 0:
                return -1
            if ptr_is_null(tmp) == 0:
                src = _bytes_data(tmp)
                src_n = py_bytes_len(tmp)
        if ptr_is_null(src) != 0:
            if ptr_is_null(tmp) == 0:
                py_decref(tmp)
            py_raise_owned(
                py_exc_new(
                    3,
                    cstr("can assign only bytes-like or an int sequence"),
                )
            )  # PY_EXC_TYPEERROR
            return -1
    else:
        src_n = py_bytes_len(replacement)

    lo_v: int = _bytes_slice_lo(lo, length, step_v)
    hi_v: int = _bytes_slice_hi(hi, length, step_v)
    lo_v = _bytes_normalize_lo(lo_v, length, step_v)
    hi_v = _bytes_normalize_hi(hi, hi_v, length, step_v)

    if step_v == 1:
        span: int = hi_v - lo_v
        if span < 0:
            span = 0
        if src_n > span:
            if ptr_is_null(tmp) == 0:
                py_decref(tmp)
            py_raise_owned(
                py_exc_new(
                    11,
                    cstr(
                        "bytearray slice assignment cannot grow the object "
                        "in place"
                    ),
                )
            )  # PY_EXC_NOTIMPLEMENTEDERROR
            return -1
        i: int = 0
        while i < src_n:
            store_i8(o, 24 + lo_v + i, load_i8(src, i))
            i = i + 1
        if src_n < span:
            tail: int = length - hi_v
            j: int = 0
            while j < tail:
                store_i8(o, 24 + lo_v + src_n + j, load_i8(o, 24 + hi_v + j))
                j = j + 1
            new_len: int = length - (span - src_n)
            store_i64(o, 16, new_len)
            store_i8(o, 24 + new_len, 0)
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        return 0

    # Extended slice.  Walk the positions in slice order -- for a negative
    # step that is descending, and the replacement's first byte belongs at the
    # first position visited, not at the lowest index.
    count: int = 0
    pos: int = lo_v
    if step_v > 0:
        while pos < hi_v:
            count = count + 1
            pos = pos + step_v
    else:
        while pos > hi_v:
            count = count + 1
            pos = pos + step_v
    if src_n != count:
        if ptr_is_null(tmp) == 0:
            py_decref(tmp)
        py_raise_owned(
            py_exc_new(
                2,
                cstr("attempt to assign bytes of the wrong size to extended slice"),
            )
        )  # PY_EXC_VALUEERROR
        return -1
    pos = lo_v
    k: int = 0
    while k < count:
        if pos >= 0 and pos < length:
            store_i8(o, 24 + pos, load_i8(src, k))
        k = k + 1
        pos = pos + step_v
    if ptr_is_null(tmp) == 0:
        py_decref(tmp)
    return 0


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


def _repr_quote(src, byte_len: int) -> int:
    # CPython's unicode_repr / bytes_repr: single quotes unless the text has
    # a single quote and no double quote.  ("{'a': 1}" used to print as
    # '{\'a\': 1}'.)
    squote: int = 0
    dquote: int = 0
    i: int = 0
    while i < byte_len:
        c: int = _load_u8(src, i)
        if c == 39:
            squote = 1
        elif c == 34:
            dquote = 1
        i = i + 1
    if squote != 0 and dquote == 0:
        return 34
    return 39


def _obj_repr_str(o, escape_non_ascii: int):
    byte_len: int = load_i64(o, 16)
    src = ptr_add(o, 40)
    quote: int = _repr_quote(src, byte_len)
    # Worst case: every byte a 4-byte \xHH escape, or a 10-byte \UHHHHHHHH
    # escape per 4-byte character under ascii().
    out_len: int = 2 + byte_len * 4
    if escape_non_ascii != 0:
        out_len = 2 + byte_len * 10
    buf = py_mem_alloc(out_len + 1)
    if ptr_is_null(buf):
        return null()
    pos: int = 0
    store_i8(buf, pos, quote)
    pos = pos + 1
    i: int = 0
    while i < byte_len:
        c2: int = _load_u8(src, i)
        if c2 == 92:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, 92)
            pos = pos + 1
            i = i + 1
        elif c2 == quote:
            store_i8(buf, pos, 92)
            pos = pos + 1
            store_i8(buf, pos, quote)
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
        elif c2 < 32 or c2 == 127:
            # Control characters escape in repr() too, not only in ascii().
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
    store_i8(buf, pos, quote)
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
    quote: int = _repr_quote(data, n)
    pos: int = 0
    store_i8(buf, pos, 98)  # 'b'
    pos = pos + 1
    store_i8(buf, pos, quote)
    pos = pos + 1
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if c == 92:  # '\'
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, 92)
            pos = pos + 2
        elif c == quote:
            store_i8(buf, pos, 92)
            store_i8(buf, pos + 1, quote)
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
    store_i8(buf, pos, quote)
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
    if tag == PY_TYPE_ELLIPSIS:
        return py_str_new(cstr("Ellipsis"), 8)
    if tag == PY_TYPE_NOTIMPLEMENTED:
        return py_str_new(cstr("NotImplemented"), 14)
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


def _class_own_str_attr(cls, name):
    """NEW str ``name`` from the class's own namespace, or NULL."""
    attrs = py_class_attrs_dict(cls, 0)
    if ptr_is_null(attrs):
        return null()
    key = py_str_new(name, strlen(name))
    if ptr_is_null(key):
        return null()
    value = py_dict_get(attrs, key)
    py_decref(key)
    if ptr_is_null(value):
        if py_err_occurred() != 0:
            py_clear_exception()
        return null()
    if _type_of(value) != PY_TYPE_STR:
        py_decref(value)
        return null()
    return value


def _str_is(s, text) -> int:
    n: int = strlen(text)
    if py_str_byte_len(s) != n:
        return 0
    data = py_str_utf8(s)
    i: int = 0
    while i < n:
        if load_i8(data, i) != load_i8(text, i):
            return 0
        i = i + 1
    return 1


def _class_display_name(cls):
    """NEW str ``module.qualname`` as type.__repr__ shows it; builtins bare."""
    qual = _class_own_str_attr(cls, cstr("__qualname__"))
    if ptr_is_null(qual):
        name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
        if ptr_is_null(name):
            name = cstr("?")
        qual = py_str_new(name, strlen(name))
        if ptr_is_null(qual):
            return null()
    module = _class_own_str_attr(cls, cstr("__module__"))
    if ptr_is_null(module):
        return qual
    if _str_is(module, cstr("builtins")) != 0:
        py_decref(module)
        return qual
    return _cat_take(_cat_take(module, _str_lit1(46)), qual)  # '.'


def _identity_hex(o):
    """NEW str of ``hex(id(o))``."""
    value: int = py_obj_id(o)
    buf = stack_alloc(24)
    pos: int = 23
    store_i8(buf, pos, 0)
    if value == 0:
        pos = pos - 1
        store_i8(buf, pos, 48)
    while value != 0:
        digit: int = value & 15
        value = (value >> 4) & 0x0FFFFFFFFFFFFFFF
        pos = pos - 1
        if digit < 10:
            store_i8(buf, pos, 48 + digit)
        else:
            store_i8(buf, pos, 87 + digit)
    pos = pos - 1
    store_i8(buf, pos, 120)  # 'x'
    pos = pos - 1
    store_i8(buf, pos, 48)  # '0'
    return py_str_new(ptr_add(buf, pos), 23 - pos)


def _default_object_repr(o, tag: int):
    """CPython's default reprs for objects with no ``__repr__``: classes
    (``<class 'm.C'>``), functions (``<function f at 0x..>``) and
    ``object.__repr__`` (``<m.C object at 0x..>``).  NULL when ``o`` has no
    class to name."""
    if tag == PY_TYPE_CLASS:
        shown = _class_display_name(o)
        if ptr_is_null(shown):
            return null()
        head = py_str_new(cstr("<class '"), 8)
        return _cat_take(_cat_take(head, shown), py_str_new(cstr("'>"), 2))
    if tag == PY_TYPE_FUNC:
        kind: int = py_func_kind(o)
        if kind == 1:
            receiver = pcc_gc_load_ptr(o, ptr_add(o, 80))
            if ptr_is_null(receiver) != 0:
                head = py_str_new(cstr("<built-in function "), 19)
                head = _cat_take(head, py_func_display_name(o))
                return _cat_take(head, _str_lit1(62))  # '>'
            # <built-in method append of list object at 0x...>
            where = _identity_hex(receiver)
            owner = py_obj_type_name(receiver)
            head = py_str_new(cstr("<built-in method "), 17)
            head = _cat_take(head, py_func_display_name(o))
            head = _cat_take(head, py_str_new(cstr(" of "), 4))
            head = _cat_take(head, owner)
            head = _cat_take(head, py_str_new(cstr(" object at "), 11))
            return _cat_take(_cat_take(head, where), _str_lit1(62))  # '>'
        if kind == 2:
            # <bound method Cls.method of repr(self)>
            function = py_bound_method_function(o)
            name = null()
            if ptr_is_null(function) != 0:
                name = py_func_display_name(o)
            else:
                name = py_func_display_name(function)
                py_decref(function)
            shown = py_obj_repr(pcc_gc_load_ptr(o, ptr_add(o, 80)))
            if ptr_is_null(shown) != 0:
                py_decref(name)
                return null()
            head = py_str_new(cstr("<bound method "), 14)
            head = _cat_take(head, name)
            head = _cat_take(head, py_str_new(cstr(" of "), 4))
            head = _cat_take(head, shown)
            return _cat_take(head, _str_lit1(62))  # '>'
        head = py_str_new(cstr("<function "), 10)
        head = _cat_take(head, py_func_display_name(o))
        head = _cat_take(head, py_str_new(cstr(" at "), 4))
        return _cat_take(_cat_take(head, _identity_hex(o)), _str_lit1(62))  # '>'
    cls = null()
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls):
        return null()
    shown = _class_display_name(cls)
    if ptr_is_null(shown):
        return null()
    out = _cat_take(_str_lit1(60), shown)  # '<'
    out = _cat_take(out, py_str_new(cstr(" object at "), 11))
    return _cat_take(_cat_take(out, _identity_hex(o)), _str_lit1(62))  # '>'


@c_abi_export("py_obj_default_repr")
def py_obj_default_repr(o):
    """``object.__repr__(o)``: the default repr, never a user ``__repr__``."""
    if ptr_is_null(o):
        return null()
    return _default_object_repr(o, _type_of(o))


@c_abi_export("py_exc_base_str")
def py_exc_base_str(o, key_error: int):
    """BaseException.__str__ (``key_error`` 0) or KeyError.__str__ (1).

    From ``args``: "" for none, the item's str (KeyError: its repr) for one,
    the tuple's str otherwise. No user __str__ is consulted.
    """
    if ptr_is_null(o):
        return null()
    o = pcc_gc_note_relocation_read(o)
    if _type_of(o) == PY_TYPE_EXC:
        args = py_exc_get_args(o)
    else:
        args = py_instance_getattr(o, cstr("args"))
    if ptr_is_null(args):
        if py_err_occurred() != 0:
            py_clear_exception()
        return py_str_new(cstr(""), 0)
    text = null()
    if _type_of(args) == PY_TYPE_TUPLE:  # PY_TYPE_TUPLE
        n: int = py_tuple_len(args)
        if n == 0:
            text = py_str_new(cstr(""), 0)
        elif n == 1:
            item = py_tuple_get(args, 0)
            if key_error != 0:
                text = py_obj_repr(item)
            else:
                text = py_obj_str(item)
            py_decref(item)
        else:
            text = py_obj_repr(args)
    else:
        text = py_obj_str(args)
    py_decref(args)
    return text


@c_abi_export("py_exc_base_repr")
def py_exc_base_repr(o):
    """BaseException.__repr__: no user __repr__ is consulted."""
    if ptr_is_null(o):
        return null()
    if _type_of(o) == PY_TYPE_EXC:
        return py_exc_repr(o)
    return _exception_instance_repr(o)


def _exception_instance_repr(o):
    """BaseException.__repr__ for a user exception subclass instance with no
    __repr__: the class name, then repr(args[0]) in parentheses for one
    argument, else repr(args)."""
    cls = pcc_gc_load_ptr(o, ptr_add(o, PYINSTANCEOBJECT_CLS_OFFSET))
    if ptr_is_null(cls):
        return null()
    name = load_ptr(cls, PYCLASSOBJECT_NAME_OFFSET)
    if ptr_is_null(name):
        name = cstr("?")
    head = py_str_new(name, strlen(name))
    args = py_instance_getattr(o, cstr("args"))
    if ptr_is_null(args):
        if py_err_occurred() != 0:
            py_clear_exception()
        return _cat_take(head, py_str_new(cstr("()"), 2))
    if _type_of(args) == PY_TYPE_TUPLE and py_tuple_len(args) != 1:
        body = py_obj_repr(args)
        py_decref(args)
        return _cat_take(head, body)
    shown = args
    if _type_of(args) == PY_TYPE_TUPLE:
        shown = py_tuple_get(args, 0)
        py_decref(args)
    body = py_obj_repr(shown)
    py_decref(shown)
    out = _cat_take(head, _str_lit1(40))  # '('
    return _cat_take(_cat_take(out, body), _str_lit1(41))  # ')'


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
    if py_err_occurred() != 0:
        return null()
    payload = py_str_payload(o)
    if ptr_is_null(payload) == 0:
        exact = py_str_exact_copy(o)
        if ptr_is_null(exact) != 0:
            return null()
        pcc_gc_pin(exact)
        result = _obj_repr_str(exact, 0)
        pcc_gc_pin(result)
        pcc_gc_unpin(exact)
        py_decref(exact)
        pcc_gc_unpin(result)
        return result
    exc_base = py_exc_builtin_class(0)  # PY_EXC_BASE
    if not ptr_is_null(exc_base):
        if py_isinstance(o, exc_base) != 0:
            return _exception_instance_repr(o)
    return _default_object_repr(o, tag)


@c_abi_export("py_obj_ascii")
def py_obj_ascii(o):
    if ptr_is_null(o):
        return null()
    tag: int = _type_of(o)
    if tag == PY_TYPE_STR:
        return _obj_repr_str(o, 1)
    # Any other object: repr() with each non-ASCII character escaped, as by
    # the ascii codec's backslashreplace handler (containers included).
    text = py_obj_repr(o)
    if ptr_is_null(text):
        return null()
    data = py_str_utf8(text)
    count: int = py_str_byte_len(text)
    index: int = 0
    while index < count and (load_i8(data, index) & 255) < 128:
        index = index + 1
    if index == count:
        return text
    encoded = py_text_encode_ids(text, 1, 5)
    py_decref(text)
    if ptr_is_null(encoded):
        return null()
    result = py_str_new(_bytes_data(encoded), py_bytes_len(encoded))
    py_decref(encoded)
    return result


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
        if (load_i32(o, 12) & PY_FLAG_EXC_UNICODE_PAYLOAD) != 0:
            return py_unicode_error_format(o, 0)
        if (load_i32(o, 12) & PY_FLAG_EXC_OS_PAYLOAD) != 0:
            return py_os_error_format(o, 0)
        msg = py_exc_get_message(o)
        if not ptr_is_null(msg):
            # KeyError.__str__ is repr(key), not the bare key (CPython):
            # str(KeyError('x')) == "'x'".
            key_error: int = py_exc_matches(o, py_exc_builtin_class(4))  # PY_EXC_KEYERROR
            o = pcc_gc_note_relocation_read(o)
            msg = py_exc_get_message(o)
            if key_error != 0:
                return py_obj_repr(msg)
            if _type_of(msg) != PY_TYPE_STR:
                # ``str(ValueError(3))`` is ``'3'``: the argument's str.
                return py_obj_str(msg)
            py_incref(msg)
            return msg
        return py_str_new(cstr(""), 0)
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
    payload = py_str_payload(o)
    if ptr_is_null(payload) == 0:
        # __str__ overrides stay authoritative. The exact result has its
        # own owner; never expose an unleased view of the private backing.
        return py_str_exact_copy(o)
    # A user exception subclass instance with no __str__ uses BaseException
    # __str__: the message from ``args`` (args[0] if one, "" if none, the
    # tuple repr otherwise). super().__init__(*args) stores ``args``.
    exc_base = py_exc_builtin_class(0)  # PY_EXC_BASE
    if not ptr_is_null(exc_base):
        if py_isinstance(o, exc_base) != 0:
            # KeyError.__str__ shows one key as repr(key).
            return py_exc_base_str(o, py_isinstance(o, py_exc_builtin_class(4)))  # PY_EXC_KEYERROR
    # No user __str__: object.__str__ falls back to __repr__.
    return py_obj_repr(o)
