"""Modulo dispatch split out from py_obj_ops_dispatch.

Keep ``py_obj_mod`` in its own archive member so programs that need generic
truthy/add/getitem dispatch do not also pull the string-formatting closure via
``py_str_mod``.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY,
    PY_TYPE_BYTES,
    PY_TYPE_FLOAT,
    PY_TYPE_INSTANCE,
    PY_TYPE_INT,
    PY_TYPE_STR,
    PY_TYPE_USER_CLASS_START,
)
from pcc.extern import (
    c_abi_export,
    c_double,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    cstr,
    f64_signbit,
    is_tagged_int,
    load_i32,
    null,
    ptr_is_null,
)


py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_int_mod = extern("py_int_mod", (c_ptr, c_ptr), c_ptr)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
fmod_c = extern("fmod", (c_double, c_double), c_double)
pcc_numeric_float_from_bits = extern("pcc_numeric_float_from_bits", (c_int64,), c_double)
py_str_mod = extern("py_str_mod", (c_ptr, c_ptr), c_ptr)
py_bytes_mod = extern("py_bytes_mod", (c_ptr, c_ptr), c_ptr)
py_user_binop_dispatch = extern("py_user_binop_dispatch", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_raise_binary_type_error = extern("py_raise_binary_type_error", (c_ptr, c_ptr, c_ptr), c_ptr)


def _type_of(o) -> int:
    if is_tagged_int(o) != 0:
        return PY_TYPE_INT
    return load_i32(o, 8)


@c_abi_export("py_obj_mod")
def py_obj_mod(a, b):
    if ptr_is_null(a) != 0 or ptr_is_null(b) != 0:
        py_raise_owned(py_exc_new(3, cstr("unsupported operand type(s) for %")))
        return null()
    at: int = _type_of(a)
    bt: int = _type_of(b)
    if at == PY_TYPE_STR:
        return py_str_mod(a, b)
    if at == PY_TYPE_BYTES or at == PY_TYPE_BYTEARRAY:            # PY_TYPE_BYTES / PY_TYPE_BYTEARRAY
        return py_bytes_mod(a, b)
    if (at == PY_TYPE_INT or at == PY_TYPE_BOOL) and (bt == PY_TYPE_INT or bt == PY_TYPE_BOOL):
        return py_int_mod(a, b)
    a_numeric: int = 1 if at == PY_TYPE_INT or at == PY_TYPE_BOOL or at == PY_TYPE_FLOAT else 0
    b_numeric: int = 1 if bt == PY_TYPE_INT or bt == PY_TYPE_BOOL or bt == PY_TYPE_FLOAT else 0
    if a_numeric != 0 and b_numeric != 0:
        # Inputs remain borrowed from the caller's counted address leases.
        # Conversion creates only scalar temporaries; the sole NEW object is
        # returned after a nonparking NULL test. Only the NULL branch may
        # call an error helper, so a successful NEW result crosses no poll.
        divisor: float = py_float_to_f64(b)
        if divisor == 0.0:
            # CPython 3.14+: every zero divisor reads "division by zero".
            py_raise_owned(py_exc_new(9, cstr("division by zero")))
            return null()
        dividend: float = py_float_to_f64(a)
        remainder: float = fmod_c(dividend, divisor)
        if remainder != 0.0:
            if (remainder < 0.0) != (divisor < 0.0):
                remainder = remainder + divisor
        elif f64_signbit(divisor) != 0:
            remainder = pcc_numeric_float_from_bits(-9223372036854775808)
        else:
            remainder = 0.0
        result = py_float_from_f64(remainder)
        if ptr_is_null(result) != 0:
            if py_err_occurred() == 0:
                py_raise_owned(py_exc_new(19, cstr("float modulo result allocation failed")))
            return null()
        return result
    if (
        at == PY_TYPE_INSTANCE
        or at >= PY_TYPE_USER_CLASS_START
        or bt == PY_TYPE_INSTANCE
        or bt >= PY_TYPE_USER_CLASS_START
    ):
        return py_user_binop_dispatch(
            a,
            b,
            cstr("__mod__"),
            cstr("__rmod__"),
            cstr("unsupported operand type(s) for %"),
        )
    return py_raise_binary_type_error(cstr("unsupported operand type(s) for %"), a, b)
