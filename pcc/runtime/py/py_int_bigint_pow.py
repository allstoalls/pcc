"""pcc-Python replacement for py_runtime/src/py_int_bigint_pow.c."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int64, c_ptr
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import free, load_i32, null, ptr_is_null, store_i32


py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)
py_bigint_from_i64 = extern("py_bigint_from_i64", (c_int64,), c_ptr)
py_bigint_mul = extern("py_bigint_mul", (c_ptr, c_ptr), c_ptr)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _bigint_copy(a):
    ndigits: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    r = py_bigint_alloc(ndigits)
    if ptr_is_null(r):
        return r
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, load_i32(a, PYINTOBJECT_SIGN_OFFSET))
    i: int = 0
    while i < ndigits:
        store_i32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, load_i32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4))
        i = i + 1
    return r


@c_abi_export("py_bigint_pow")
def py_bigint_pow(base, exp):
    if load_i32(exp, PYINTOBJECT_SIGN_OFFSET) < 0:
        return null()
    if load_i32(exp, PYINTOBJECT_SIGN_OFFSET) == 0:
        return py_bigint_from_i64(1)

    top_digit: int = load_i32(exp, PYINTOBJECT_NDIGITS_OFFSET) - 1
    top: int = _load_u32(exp, PYINTOBJECT_DIGITS_OFFSET + top_digit * 4)
    top_bit: int = 31
    while top_bit >= 0 and (top & (1 << top_bit)) == 0:
        top_bit = top_bit - 1

    result = _bigint_copy(base)
    if ptr_is_null(result):
        return result

    di: int = top_digit
    bit: int = top_bit - 1
    while di >= 0:
        digit: int = _load_u32(exp, PYINTOBJECT_DIGITS_OFFSET + di * 4)
        while bit >= 0:
            sq = py_bigint_mul(result, result)
            free(result)
            if ptr_is_null(sq):
                return null()
            result = sq
            if (digit & (1 << bit)) != 0:
                m = py_bigint_mul(result, base)
                free(result)
                if ptr_is_null(m):
                    return null()
                result = m
            bit = bit - 1
        di = di - 1
        bit = 31
    return result
