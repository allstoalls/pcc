"""pcc-Python scalar-number owners for the no-libpython C-API surface."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BOOL, PY_TYPE_FLOAT, PY_TYPE_INT

from pcc.extern import (
    c_abi_typed_export,
    c_double,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import cstr, is_tagged_int, load_i32, ptr_is_null
from pcc.unsafe import (
    cstr,
    f64_bits,
    float_to_i64,
    global_load_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    null,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    unsigned_greater_i64,
    untag_int,
    wrapping_mul_i64,
)


py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)


def _raise_scalar(kind: int, message) -> None:
    py_raise_owned(py_exc_new(kind, message))


def _bool_scalar(obj) -> int:
    if ptr_eq(obj, global_load_ptr("py_True")):
        return 1
    if ptr_eq(obj, global_load_ptr("py_False")):
        return 0
    return -1


def _signed_i64(obj) -> int:
    bool_value: int = _bool_scalar(obj)
    if bool_value >= 0:
        return bool_value
    overflow = stack_alloc(4)
    store_i32(overflow, 0, 0)
    value: int = py_int_to_i64(obj, overflow)
    if load_i32(overflow, 0) != 0:
        _raise_scalar(15, cstr("integer conversion overflow"))
        return -1
    return value


def _unsigned_object(value: int):
    if not unsigned_greater_i64(value, 9223372036854775807):
        return py_int_from_i64(value)
    result = py_bigint_alloc(2)
    if ptr_is_null(result):
        return result
    low: int = value & 4294967295
    high: int = (value >> 32) & 4294967295
    store_i32(result, 16, 1)
    store_i32(result, 20, 2)
    store_i32(result, 24, low)
    store_i32(result, 28, high)
    return result


def _unsigned_i64_into(obj, output, mask: int) -> int:
    if ptr_is_null(output):
        return 0
    if ptr_is_null(obj):
        _raise_scalar(3, cstr("expected int"))
        return 0
    if is_tagged_int(obj):
        value: int = untag_int(obj)
        if mask == 0 and value < 0:
            _raise_scalar(15, cstr("can't convert negative int to unsigned"))
            return 0
        store_i64(output, 0, value)
        return 1
    type_tag: int = load_i32(obj, 8)
    if type_tag == PY_TYPE_BOOL:
        store_i64(output, 0, _bool_scalar(obj))
        return 1
    if type_tag != PY_TYPE_INT:
        _raise_scalar(3, cstr("expected int"))
        return 0
    ndigits: int = load_i32(obj, 20)
    if ndigits > 2 and mask == 0:
        _raise_scalar(15, cstr("integer conversion overflow"))
        return 0
    low: int = 0
    high: int = 0
    if ndigits > 0:
        low = load_i32(obj, 24)
        if low < 0:
            low = low + 4294967296
    if ndigits > 1:
        high = load_i32(obj, 28)
        if high < 0:
            high = high + 4294967296
    raw: int = wrapping_mul_i64(high, 4294967296) + low
    if load_i32(obj, 16) < 0:
        if mask == 0:
            _raise_scalar(15, cstr("can't convert negative int to unsigned"))
            return 0
        raw = 0 - raw
    store_i64(output, 0, raw)
    return 1


def _unsigned_i64(obj, mask: int) -> int:
    output = stack_alloc(8)
    store_i64(output, 0, 0)
    if _unsigned_i64_into(obj, output, mask) == 0:
        return -1
    return load_i64(output, 0)


@c_abi_typed_export("PyBool_FromLong", "ptr", ("i64",))
def PyBool_FromLong(value: int):
    result = py_bool_from_bit(value != 0)
    py_incref(result)
    return result


@c_abi_typed_export("PyBool_Check", "i32", ("ptr",))
def PyBool_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_BOOL:
        return 1
    return 0


@c_abi_typed_export("PyFloat_FromDouble", "ptr", ("f64",))
def PyFloat_FromDouble(value: float):
    return py_float_from_f64(value)


@c_abi_typed_export("PyFloat_AsDouble", "f64", ("ptr",))
def PyFloat_AsDouble(obj) -> float:
    if ptr_is_null(obj):
        py_raise_owned(py_exc_new(3, cstr("expected float-compatible object")))
        return -1.0
    if not is_tagged_int(obj):
        type_tag: int = load_i32(obj, 8)
        if type_tag != PY_TYPE_FLOAT and type_tag != PY_TYPE_INT and type_tag != PY_TYPE_BOOL:
            py_raise_owned(py_exc_new(3, cstr("expected float-compatible object")))
            return -1.0
    return py_float_to_f64(obj)


@c_abi_typed_export("PyFloat_Check", "i32", ("ptr",))
def PyFloat_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_FLOAT:
        return 1
    return 0


@c_abi_typed_export("PyFloat_CheckExact", "i32", ("ptr",))
def PyFloat_CheckExact(obj) -> int:
    return PyFloat_Check(obj)


@c_abi_typed_export("PyLong_FromLong", "ptr", ("i64",))
def PyLong_FromLong(value: int):
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromLongLong", "ptr", ("i64",))
def PyLong_FromLongLong(value: int):
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromInt32", "ptr", ("i32",))
def PyLong_FromInt32(value: int):
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromInt64", "ptr", ("i64",))
def PyLong_FromInt64(value: int):
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromSsize_t", "ptr", ("i64",))
def PyLong_FromSsize_t(value: int):
    return py_int_from_i64(value)


@c_abi_typed_export("PyLong_FromDouble", "ptr", ("f64",))
def PyLong_FromDouble(value: float):
    magnitude_bits: int = f64_bits(value) & 9223372036854775807
    if unsigned_greater_i64(magnitude_bits, 9218868437227405312):
        _raise_scalar(2, cstr("cannot convert NaN to integer"))
        return null()
    if value < -9223372036854775808.0 or value >= 9223372036854775808.0:
        _raise_scalar(15, cstr("integer conversion overflow"))
        return null()
    return py_int_from_i64(float_to_i64(value))


@c_abi_typed_export("PyLong_AsLong", "i64", ("ptr",))
def PyLong_AsLong(obj) -> int:
    return _signed_i64(obj)


@c_abi_typed_export("PyLong_AsInt", "i32", ("ptr",))
def PyLong_AsInt(obj) -> int:
    value: int = _signed_i64(obj)
    if py_err_occurred() != 0:
        return -1
    if value < -2147483648 or value > 2147483647:
        _raise_scalar(15, cstr("integer conversion overflow"))
        return -1
    return value


@c_abi_typed_export("PyLong_AsInt32", "i32", ("ptr", "ptr"))
def PyLong_AsInt32(obj, output) -> int:
    if ptr_is_null(output):
        _raise_scalar(3, cstr("NULL int32 output pointer"))
        return -1
    value: int = PyLong_AsInt(obj)
    if py_err_occurred() != 0:
        return -1
    store_i32(output, 0, value)
    return 0


@c_abi_typed_export("PyLong_AsInt64", "i32", ("ptr", "ptr"))
def PyLong_AsInt64(obj, output) -> int:
    if ptr_is_null(output):
        _raise_scalar(3, cstr("NULL int64 output pointer"))
        return -1
    value: int = _signed_i64(obj)
    if py_err_occurred() != 0:
        return -1
    store_i64(output, 0, value)
    return 0


@c_abi_typed_export("PyLong_AsLongAndOverflow", "i64", ("ptr", "ptr"))
def PyLong_AsLongAndOverflow(obj, overflow) -> int:
    bool_value: int = _bool_scalar(obj)
    if bool_value >= 0:
        if not ptr_is_null(overflow):
            store_i32(overflow, 0, 0)
        return bool_value
    local_overflow = stack_alloc(4)
    store_i32(local_overflow, 0, 0)
    value: int = py_int_to_i64(obj, local_overflow)
    direction: int = 0
    if load_i32(local_overflow, 0) != 0:
        direction = 1
        if (
            not ptr_is_null(obj)
            and not is_tagged_int(obj)
            and load_i32(obj, 8) == PY_TYPE_INT
            and load_i32(obj, 16) < 0
        ):
            direction = -1
    if not ptr_is_null(overflow):
        store_i32(overflow, 0, direction)
    if direction != 0:
        return -1
    return value


@c_abi_typed_export("PyLong_AsLongLong", "i64", ("ptr",))
def PyLong_AsLongLong(obj) -> int:
    return _signed_i64(obj)


@c_abi_typed_export("PyLong_AsLongLongAndOverflow", "i64", ("ptr", "ptr"))
def PyLong_AsLongLongAndOverflow(obj, overflow_out) -> int:
    # CPython contract: on i64 overflow set *overflow to +1/-1 and return -1
    # WITHOUT raising; otherwise *overflow = 0 and return the value. An
    # overflowing pcc int is always a heap bignum (tagged ints fit i64), so
    # the overflow direction is the bignum sign at offset 16.
    if not ptr_is_null(overflow_out):
        store_i32(overflow_out, 0, 0)
    bool_value: int = _bool_scalar(obj)
    if bool_value >= 0:
        return bool_value
    flag = stack_alloc(4)
    store_i32(flag, 0, 0)
    value: int = py_int_to_i64(obj, flag)
    if load_i32(flag, 0) != 0:
        sign: int = load_i32(obj, 16)
        if not ptr_is_null(overflow_out):
            if sign < 0:
                store_i32(overflow_out, 0, -1)
            else:
                store_i32(overflow_out, 0, 1)
        return -1
    return value


@c_abi_typed_export("PyLong_AsDouble", "f64", ("ptr",))
def PyLong_AsDouble(obj) -> float:
    if ptr_is_null(obj):
        _raise_scalar(3, cstr("expected int-compatible object"))
        return -1.0
    if not is_tagged_int(obj):
        type_tag: int = load_i32(obj, 8)
        if type_tag != PY_TYPE_INT and type_tag != PY_TYPE_BOOL:
            _raise_scalar(3, cstr("expected int-compatible object"))
            return -1.0
    return py_float_to_f64(obj)


@c_abi_typed_export("PyLong_AsSsize_t", "i64", ("ptr",))
def PyLong_AsSsize_t(obj) -> int:
    return _signed_i64(obj)


@c_abi_typed_export("PyLong_Check", "i32", ("ptr",))
def PyLong_Check(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 1
    type_tag: int = load_i32(obj, 8)
    if type_tag == PY_TYPE_INT or type_tag == PY_TYPE_BOOL:
        return 1
    return 0


@c_abi_typed_export("PyLong_CheckExact", "i32", ("ptr",))
def PyLong_CheckExact(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 1
    if load_i32(obj, 8) == PY_TYPE_INT:
        return 1
    return 0


@c_abi_typed_export("PyLong_IsZero", "i32", ("ptr",))
def PyLong_IsZero(obj) -> int:
    if PyLong_Check(obj) == 0:
        _raise_scalar(3, cstr("expected int"))
        return -1
    bool_value: int = _bool_scalar(obj)
    if bool_value >= 0:
        if bool_value == 0:
            return 1
        return 0
    if is_tagged_int(obj):
        if untag_int(obj) == 0:
            return 1
        return 0
    if load_i32(obj, 16) == 0 or load_i32(obj, 20) == 0:
        return 1
    return 0


@c_abi_typed_export("PyLong_FromUnsignedLong", "ptr", ("u64",))
def PyLong_FromUnsignedLong(value: int):
    return _unsigned_object(value)


@c_abi_typed_export("PyLong_FromUnsignedLongLong", "ptr", ("u64",))
def PyLong_FromUnsignedLongLong(value: int):
    return _unsigned_object(value)


@c_abi_typed_export("PyLong_FromUInt32", "ptr", ("u32",))
def PyLong_FromUInt32(value: int):
    return _unsigned_object(value & 4294967295)


@c_abi_typed_export("PyLong_FromUInt64", "ptr", ("u64",))
def PyLong_FromUInt64(value: int):
    return _unsigned_object(value)


@c_abi_typed_export("pcc_py_long_from_void_ptr", "ptr", ("ptr",))
def pcc_py_long_from_void_ptr(value):
    return _unsigned_object(ptr_to_int(value))


@c_abi_typed_export("PyLong_FromSize_t", "ptr", ("u64",))
def PyLong_FromSize_t(value: int):
    return _unsigned_object(value)


@c_abi_typed_export("PyLong_AsUInt32", "i32", ("ptr", "ptr"))
def PyLong_AsUInt32(obj, output) -> int:
    if ptr_is_null(output):
        _raise_scalar(3, cstr("NULL uint32 output pointer"))
        return -1
    value: int = _unsigned_i64(obj, 0)
    if py_err_occurred() != 0:
        return -1
    if unsigned_greater_i64(value, 4294967295):
        _raise_scalar(15, cstr("integer conversion overflow"))
        return -1
    store_i32(output, 0, value)
    return 0


@c_abi_typed_export("PyLong_AsUInt64", "i32", ("ptr", "ptr"))
def PyLong_AsUInt64(obj, output) -> int:
    if ptr_is_null(output):
        _raise_scalar(3, cstr("NULL uint64 output pointer"))
        return -1
    value: int = _unsigned_i64(obj, 0)
    if py_err_occurred() != 0:
        return -1
    store_i64(output, 0, value)
    return 0


@c_abi_typed_export("PyLong_AsUnsignedLong", "u64", ("ptr",))
def PyLong_AsUnsignedLong(obj) -> int:
    return _unsigned_i64(obj, 0)


@c_abi_typed_export("PyLong_AsUnsignedLongLong", "u64", ("ptr",))
def PyLong_AsUnsignedLongLong(obj) -> int:
    return _unsigned_i64(obj, 0)


@c_abi_typed_export("PyLong_AsUnsignedLongLongMask", "u64", ("ptr",))
def PyLong_AsUnsignedLongLongMask(obj) -> int:
    return _unsigned_i64(obj, 1)


@c_abi_typed_export("pcc_py_long_as_void_ptr", "ptr", ("ptr",))
def pcc_py_long_as_void_ptr(obj):
    value: int = _unsigned_i64(obj, 0)
    if py_err_occurred() != 0:
        return null()
    return int_to_ptr(value)


@c_abi_typed_export("PyLong_FromVoidPtr", "ptr", ("ptr",))
def PyLong_FromVoidPtr(value):
    return pcc_py_long_from_void_ptr(value)


@c_abi_typed_export("PyLong_AsVoidPtr", "ptr", ("ptr",))
def PyLong_AsVoidPtr(obj):
    return pcc_py_long_as_void_ptr(obj)


@c_abi_typed_export("PyLong_AsSize_t", "u64", ("ptr",))
def PyLong_AsSize_t(obj) -> int:
    return _unsigned_i64(obj, 0)
