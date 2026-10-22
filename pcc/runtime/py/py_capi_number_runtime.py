"""pcc-Python owners for the no-libpython C-API number surface.

Replaces the PyNumber_* block of py_capi_shim.c.  All functions delegate to
the existing pcc-Python int/float ABIs (py_int_add/sub/mul/...,
py_float_from_f64/to_f64) or to pcc-Python-owned C-API siblings (PyErr_*,
PyLong_AsSsize_t, PyTuple_Pack).  The C-extension number slot dispatch
(cext_binary_number / cext_absolute) stays in the C shim for now and is
extern'd here.

Owned surface (stable C ABI names):

  PyNumber_Check, PyNumber_Long, PyNumber_Float, PyNumber_Index,
  PyNumber_Absolute, PyNumber_Negative, PyNumber_Positive, PyNumber_Invert,
  PyNumber_Add, PyNumber_Subtract, PyNumber_Multiply, PyNumber_Remainder,
  PyNumber_Divmod, PyNumber_Power, PyNumber_FloorDivide, PyNumber_TrueDivide,
  PyNumber_Lshift, PyNumber_Rshift, PyNumber_And, PyNumber_Xor, PyNumber_Or,
  PyNumber_AsSsize_t

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the number C-API contract:
  PY_EXC_TYPEERROR = 3, PY_EXC_VALUEERROR = 2
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BOOL, PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_LIST, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import c_abi_typed_export, c_double, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    call_ptr1,
    cstr,
    f64_bits,
    float_to_i64,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_ptr,
    malloc,
    memcpy,
    null,
    ptr_add,
    ptr_is_null,
    store_i8,
    strlen,
    untag_int,
)

py_int_add = extern("py_int_add", (c_ptr, c_ptr), c_ptr)
py_int_sub = extern("py_int_sub", (c_ptr, c_ptr), c_ptr)
py_int_mul = extern("py_int_mul", (c_ptr, c_ptr), c_ptr)
py_int_rem = extern("py_int_mod", (c_ptr, c_ptr), c_ptr)
py_int_pow = extern("py_int_pow", (c_ptr, c_ptr), c_ptr)
py_int_shl = extern("py_int_shl", (c_ptr, c_ptr), c_ptr)
py_int_shr = extern("py_int_shr", (c_ptr, c_ptr), c_ptr)
py_int_and = extern("py_int_and", (c_ptr, c_ptr), c_ptr)
py_int_xor = extern("py_int_xor", (c_ptr, c_ptr), c_ptr)
py_int_or = extern("py_int_or", (c_ptr, c_ptr), c_ptr)
py_int_neg = extern("py_int_neg", (c_ptr,), c_ptr)
py_int_truediv = extern("py_int_truediv", (c_ptr, c_ptr), c_ptr)
py_int_floordiv = extern("py_int_floordiv", (c_ptr, c_ptr), c_ptr)
py_int_mod = extern("py_int_mod", (c_ptr, c_ptr), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_obj_add = extern("py_obj_add", (c_ptr, c_ptr), c_ptr)
py_obj_divmod = extern("py_obj_divmod", (c_ptr, c_ptr), c_ptr)
py_str_repeat = extern("py_str_repeat", (c_ptr, c_ptr), c_ptr)
py_bytes_repeat = extern("py_bytes_repeat", (c_ptr, c_int64), c_ptr)
py_list_repeat = extern("py_list_repeat", (c_ptr, c_int64), c_ptr)
py_tuple_repeat = extern("py_tuple_repeat", (c_ptr, c_int64), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
PyErr_SetString = extern("PyErr_SetString", (c_ptr, c_ptr), c_void)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
PyLong_AsSsize_t = extern("PyLong_AsSsize_t", (c_ptr,), c_int64)
pcc_capi_cext_binary_number = extern(
    "pcc_capi_cext_binary_number", (c_ptr, c_ptr, c_int64), c_ptr
)
pcc_capi_cext_absolute = extern("pcc_capi_cext_absolute", (c_ptr,), c_ptr)
pcc_capi_cext_type_for_object = extern("pcc_capi_cext_type_for_object", (c_ptr,), c_ptr)
pcc_capi_type = extern("pcc_capi_type", (c_ptr,), c_ptr)
pow_c = extern("pow", (c_double, c_double), c_double)
fabs_c = extern("fabs", (c_double,), c_double)


def _py_none() -> c_ptr:
    return global_load_ptr("py_None")


def _py_true() -> c_ptr:
    return global_load_ptr("py_True")


def _is_intlike(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 1
    tag: int = load_i32(obj, 8)
    if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:  # PY_TYPE_INT / BOOL
        return 1
    return 0


def _call_int_conversion_slot(obj, slot, error_message) -> c_ptr:
    if ptr_is_null(slot):
        return null()
    result = call_ptr1(slot, obj)
    if ptr_is_null(result):
        py_runtime_error_if_unset(
            cstr("pcc_capi_call_int_conversion_slot"),
            cstr(
                "integer conversion slot returned NULL without setting an exception"
            ),
        )
        return null()
    if _is_intlike(result) == 0:
        py_decref(result)
        PyErr_SetString(py_exc_builtin_class(3), error_message)
        return null()
    return result


def _is_floatlike(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_FLOAT:  # PY_TYPE_FLOAT
        return 1
    return 0


def _is_numberlike(obj) -> int:
    if _is_intlike(obj) != 0:
        return 1
    if _is_floatlike(obj) != 0:
        return 1
    return 0


def _number_methods(obj) -> c_ptr:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return null()
    type_obj = pcc_capi_type(obj)
    if ptr_is_null(type_obj):
        return null()
    return load_ptr(type_obj, 104)  # tp_as_number


def _int_operand(obj) -> c_ptr:
    if not ptr_is_null(obj) and not is_tagged_int(obj) and load_i32(obj, 8) == PY_TYPE_BOOL:
        if obj == _py_true():
            return py_int_from_i64(1)
        return py_int_from_i64(0)
    return obj


def _numeric_error(op) -> c_ptr:
    if py_err_occurred() == 0:
        exc_type = py_exc_builtin_class(3)  # PY_EXC_TYPEERROR
        PyErr_SetString(exc_type, _concat_operand_error(op))
    return null()


def _concat_operand_error(op) -> c_ptr:
    # "unsupported operand type(s) for <op>" — build via a fixed prefix and
    # the operator string; no variadic printf on this path.
    prefix = cstr("unsupported operand type(s) for ")
    plen = strlen(prefix)
    olen = strlen(op)
    buf = malloc(plen + olen + 1)
    if ptr_is_null(buf):
        return cstr("unsupported operand type(s)")
    memcpy(buf, prefix, plen)
    memcpy(ptr_add(buf, plen), op, olen)
    store_i8(buf, plen + olen, 0)
    return buf


def _binary_int_result(left, right, op: int, op_name) -> c_ptr:
    # op is an opcode, not a function value: the library-mode port compiler
    # cannot pass extern functions as first-class values (they lower to
    # NameError stubs), so dispatch on an int here instead.
    a = _int_operand(left)
    b = _int_operand(right)
    result = null()
    if op == 0:
        result = py_int_add(a, b)
    elif op == 1:
        result = py_int_sub(a, b)
    elif op == 2:
        result = py_int_mul(a, b)
    elif op == 3:
        result = py_int_rem(a, b)
    elif op == 4:
        result = py_int_floordiv(a, b)
    elif op == 5:
        result = py_int_truediv(a, b)
    elif op == 6:
        result = py_int_shl(a, b)
    elif op == 7:
        result = py_int_shr(a, b)
    elif op == 8:
        result = py_int_and(a, b)
    elif op == 9:
        result = py_int_xor(a, b)
    elif op == 10:
        result = py_int_or(a, b)
    elif op == 11:
        result = py_int_pow(a, b)
    if ptr_is_null(result):
        return _numeric_error(op_name)
    return result


@c_abi_typed_export("PyNumber_Check", "i32", ("ptr",))
def PyNumber_Check(obj) -> int:
    if _is_numberlike(obj) != 0:
        return 1
    methods = _number_methods(obj)
    if ptr_is_null(methods):
        return 0
    if (
        not ptr_is_null(load_ptr(methods, 128))  # nb_int
        or not ptr_is_null(load_ptr(methods, 264))  # nb_index
        or not ptr_is_null(load_ptr(methods, 144))  # nb_float
        or not ptr_is_null(load_ptr(methods, 0))  # nb_add
    ):
        return 1
    return 0


@c_abi_typed_export("PyIndex_Check", "i32", ("ptr",))
def PyIndex_Check(obj) -> int:
    # Port owner of the number domain: mirror of the pre-migration shim
    # definition (intlike, else nb_index slot present). numpy resolves this
    # at dlopen; it must exist wherever the number domain lives.
    if _is_intlike(obj) != 0:
        return 1
    methods = _number_methods(obj)
    if ptr_is_null(methods):
        return 0
    if not ptr_is_null(load_ptr(methods, 264)):  # nb_index
        return 1
    return 0


@c_abi_typed_export("PyNumber_Index", "ptr", ("ptr",))
def PyNumber_Index(obj) -> c_ptr:
    if ptr_is_null(obj):
        _type_error(cstr("expected integer index"))
        return null()
    if not is_tagged_int(obj) and load_i32(obj, 8) == PY_TYPE_BOOL:  # PY_TYPE_BOOL
        if obj == _py_true():
            return py_int_from_i64(1)
        return py_int_from_i64(0)
    if _is_intlike(obj) == 0:
        methods = _number_methods(obj)
        if not ptr_is_null(methods) and not ptr_is_null(load_ptr(methods, 264)):
            return _call_int_conversion_slot(
                obj,
                load_ptr(methods, 264),
                cstr("__index__ returned a non-int"),
            )
        _type_error(cstr("expected integer index"))
        return null()
    py_incref(obj)
    return obj


@c_abi_typed_export("PyNumber_Long", "ptr", ("ptr",))
def PyNumber_Long(obj) -> c_ptr:
    if _is_intlike(obj) != 0:
        return PyNumber_Index(obj)
    if _is_floatlike(obj) != 0:
        value: float = py_float_to_f64(obj)
        if not _isfinite(value) or value < -9223372036854775808.0 or value > 9223372036854775807.0:
            _overflow_error(cstr("cannot convert float to integer"))
            return null()
        return py_int_from_i64(float_to_i64(value))
    methods = _number_methods(obj)
    if not ptr_is_null(methods):
        if not ptr_is_null(load_ptr(methods, 128)):
            return _call_int_conversion_slot(
                obj,
                load_ptr(methods, 128),
                cstr("__int__ returned a non-int"),
            )
        if not ptr_is_null(load_ptr(methods, 264)):
            return _call_int_conversion_slot(
                obj,
                load_ptr(methods, 264),
                cstr("__index__ returned a non-int"),
            )
    return _numeric_error(cstr("int()"))


@c_abi_typed_export("PyNumber_Float", "ptr", ("ptr",))
def PyNumber_Float(obj) -> c_ptr:
    if _is_floatlike(obj) != 0:
        py_incref(obj)
        return obj
    if _is_intlike(obj) != 0:
        return py_float_from_f64(py_float_to_f64(obj))
    return _numeric_error(cstr("float()"))


@c_abi_typed_export("PyNumber_Absolute", "ptr", ("ptr",))
def PyNumber_Absolute(obj) -> c_ptr:
    if _is_floatlike(obj) != 0:
        return py_float_from_f64(fabs_c(py_float_to_f64(obj)))
    if not ptr_is_null(pcc_capi_cext_type_for_object(obj)):
        return pcc_capi_cext_absolute(obj)
    if _is_intlike(obj) == 0:
        return _numeric_error(cstr("abs()"))
    operand = _int_operand(obj)
    if is_tagged_int(operand):
        value: int = untag_int(operand)
        if value < 0:
            return py_int_neg(operand)
        return py_int_from_i64(value)
    sign: int = load_i32(operand, 16)  # PyIntObject.sign
    if sign < 0:
        return py_int_neg(operand)
    py_incref(operand)
    return operand


@c_abi_typed_export("PyNumber_Negative", "ptr", ("ptr",))
def PyNumber_Negative(obj) -> c_ptr:
    if _is_floatlike(obj) != 0:
        # -x flips the sign bit (-0.0 for 0.0); 0.0 - x would not.
        value: float = py_float_to_f64(obj)
        return py_float_from_f64(-value)
    if _is_intlike(obj) != 0:
        operand = _int_operand(obj)
        result = py_int_neg(operand)
        if ptr_is_null(result):
            return _numeric_error(cstr("unary -"))
        return result
    return _numeric_error(cstr("unary -"))


@c_abi_typed_export("PyNumber_Positive", "ptr", ("ptr",))
def PyNumber_Positive(obj) -> c_ptr:
    if _is_floatlike(obj) != 0:
        py_incref(obj)
        return obj
    if _is_intlike(obj) != 0:
        return PyNumber_Index(obj)
    return _numeric_error(cstr("unary +"))


@c_abi_typed_export("PyNumber_Invert", "ptr", ("ptr",))
def PyNumber_Invert(obj) -> c_ptr:
    if _is_intlike(obj) == 0:
        return _numeric_error(cstr("~"))
    operand = _int_operand(obj)
    result = py_int_xor(operand, py_int_from_i64(-1))
    if ptr_is_null(result):
        return _numeric_error(cstr("~"))
    return result


@c_abi_typed_export("PyNumber_Add", "ptr", ("ptr", "ptr"))
def PyNumber_Add(left, right) -> c_ptr:
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
            return _numeric_error(cstr("+"))
        return py_float_from_f64(py_float_to_f64(left) + py_float_to_f64(right))
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 0, "+")
    if not ptr_is_null(pcc_capi_cext_type_for_object(left)) or not ptr_is_null(pcc_capi_cext_type_for_object(right)):
        return pcc_capi_cext_binary_number(left, right, 0)
    result = py_obj_add(left, right)
    if ptr_is_null(result):
        return _numeric_error(cstr("+"))
    return result


@c_abi_typed_export("PyNumber_Subtract", "ptr", ("ptr", "ptr"))
def PyNumber_Subtract(left, right) -> c_ptr:
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
            return _numeric_error(cstr("-"))
        return py_float_from_f64(py_float_to_f64(left) - py_float_to_f64(right))
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 1, "-")
    if not ptr_is_null(pcc_capi_cext_type_for_object(left)) or not ptr_is_null(pcc_capi_cext_type_for_object(right)):
        return pcc_capi_cext_binary_number(left, right, 1)
    return _numeric_error(cstr("-"))


@c_abi_typed_export("PyNumber_Multiply", "ptr", ("ptr", "ptr"))
def PyNumber_Multiply(left, right) -> c_ptr:
    repeat = _repeat_sequence(left, right)
    if not ptr_is_null(repeat) or py_err_occurred() != 0:
        return repeat
    repeat = _repeat_sequence(right, left)
    if not ptr_is_null(repeat) or py_err_occurred() != 0:
        return repeat
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
            return _numeric_error(cstr("*"))
        return py_float_from_f64(py_float_to_f64(left) * py_float_to_f64(right))
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 2, "*")
    if not ptr_is_null(pcc_capi_cext_type_for_object(left)) or not ptr_is_null(pcc_capi_cext_type_for_object(right)):
        return pcc_capi_cext_binary_number(left, right, 2)
    return _numeric_error(cstr("*"))


def _repeat_sequence(seq, count_obj) -> c_ptr:
    if ptr_is_null(seq) or ptr_is_null(count_obj) or is_tagged_int(seq):
        return null()
    tag: int = load_i32(seq, 8)
    if tag != PY_TYPE_STR and tag != PY_TYPE_BYTES and tag != PY_TYPE_BYTEARRAY and tag != PY_TYPE_LIST and tag != PY_TYPE_TUPLE:  # STR/BYTES/BYTEARRAY/LIST/TUPLE
        return null()
    if _is_intlike(count_obj) == 0:
        return null()
    n_obj = _int_operand(count_obj)
    count: int = PyLong_AsSsize_t(n_obj)
    if py_err_occurred() != 0:
        return null()
    if tag == PY_TYPE_STR:  # str
        return py_str_repeat(seq, n_obj)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:  # bytes/bytearray
        return py_bytes_repeat(seq, count)
    if tag == PY_TYPE_LIST:  # list
        return py_list_repeat(seq, count)
    if tag == PY_TYPE_TUPLE:  # tuple
        return py_tuple_repeat(seq, count)
    return null()


@c_abi_typed_export("PyNumber_Remainder", "ptr", ("ptr", "ptr"))
def PyNumber_Remainder(left, right) -> c_ptr:
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
            return _numeric_error(cstr("%"))
        divisor: float = py_float_to_f64(right)
        if divisor == 0.0:
            _zero_division_error()
            return null()
        return py_float_from_f64(py_float_to_f64(left) % divisor)
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 3, "%")
    if not ptr_is_null(pcc_capi_cext_type_for_object(left)) or not ptr_is_null(pcc_capi_cext_type_for_object(right)):
        return pcc_capi_cext_binary_number(left, right, 3)
    return _numeric_error(cstr("%"))


@c_abi_typed_export("PyNumber_FloorDivide", "ptr", ("ptr", "ptr"))
def PyNumber_FloorDivide(left, right) -> c_ptr:
    if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
        return _numeric_error(cstr("//"))
    divisor = py_float_to_f64(right)
    if divisor == 0.0:
        _zero_division_error()
        return null()
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        return py_float_from_f64(_floor(py_float_to_f64(left) / divisor))
    return _binary_int_result(left, right, 4, "//")


@c_abi_typed_export("PyNumber_TrueDivide", "ptr", ("ptr", "ptr"))
def PyNumber_TrueDivide(left, right) -> c_ptr:
    if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
        return _numeric_error(cstr("/"))
    divisor = py_float_to_f64(right)
    if divisor == 0.0:
        _zero_division_error()
        return null()
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        return py_float_from_f64(py_float_to_f64(left) / divisor)
    return _binary_int_result(left, right, 5, "/")


@c_abi_typed_export("PyNumber_Lshift", "ptr", ("ptr", "ptr"))
def PyNumber_Lshift(left, right) -> c_ptr:
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 6, "<<")
    return _numeric_error(cstr("<<"))


@c_abi_typed_export("PyNumber_Rshift", "ptr", ("ptr", "ptr"))
def PyNumber_Rshift(left, right) -> c_ptr:
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 7, ">>")
    return _numeric_error(cstr(">>"))


@c_abi_typed_export("PyNumber_And", "ptr", ("ptr", "ptr"))
def PyNumber_And(left, right) -> c_ptr:
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 8, "&")
    return _numeric_error(cstr("&"))


@c_abi_typed_export("PyNumber_Xor", "ptr", ("ptr", "ptr"))
def PyNumber_Xor(left, right) -> c_ptr:
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 9, "^")
    return _numeric_error(cstr("^"))


@c_abi_typed_export("PyNumber_Or", "ptr", ("ptr", "ptr"))
def PyNumber_Or(left, right) -> c_ptr:
    if _is_intlike(left) != 0 and _is_intlike(right) != 0:
        return _binary_int_result(left, right, 10, "|")
    return _numeric_error(cstr("|"))


@c_abi_typed_export("PyNumber_Power", "ptr", ("ptr", "ptr", "ptr"))
def PyNumber_Power(left, right, mod) -> c_ptr:
    if not ptr_is_null(mod) and mod != _py_none():
        _type_error(cstr("modular power is not supported"))
        return null()
    if _is_numberlike(left) == 0 or _is_numberlike(right) == 0:
        return _numeric_error(cstr("**"))
    if _is_floatlike(left) != 0 or _is_floatlike(right) != 0:
        return py_float_from_f64(pow_c(py_float_to_f64(left), py_float_to_f64(right)))
    return _binary_int_result(left, right, 11, "**")


@c_abi_typed_export("PyNumber_Divmod", "ptr", ("ptr", "ptr"))
def PyNumber_Divmod(o1, o2) -> c_ptr:
    return py_obj_divmod(o1, o2)


@c_abi_typed_export("PyNumber_AsSsize_t", "i64", ("ptr", "ptr"))
def PyNumber_AsSsize_t(obj, exc) -> int:
    index = PyNumber_Index(obj)
    if ptr_is_null(index):
        return -1
    value = PyLong_AsSsize_t(index)
    py_decref(index)
    if py_err_occurred() != 0 and not ptr_is_null(exc):
        PyErr_SetString(exc, cstr("cannot fit integer index into Py_ssize_t"))
    return value


# --- helpers -------------------------------------------------------


def _tuple_pack2(a, b) -> c_ptr:
    t = py_tuple_new(2)
    if ptr_is_null(t):
        return null()
    py_tuple_set_item(t, 0, a)
    py_tuple_set_item(t, 1, b)
    return t


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _zero_division_error() -> None:
    py_raise_owned(py_exc_new(9, cstr("division by zero")))  # PY_EXC_ZERODIVISIONERROR


def _overflow_error(message) -> None:
    py_raise_owned(py_exc_new(15, message))  # PY_EXC_OVERFLOWERROR


def _isfinite(value: float) -> int:
    bits: int = f64_bits(value)
    exp: int = (bits >> 52) & 0x7FF
    if exp == 0x7FF:
        return 0
    return 1


def _floor(value: float) -> float:
    # trunc toward zero then adjust for negatives
    trunc: float = float_to_i64(value)
    if value < 0.0 and trunc != value:
        return trunc - 1.0
    return trunc
