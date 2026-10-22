"""pcc-Python replacement for the residual py_runtime/src/py_int.c."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int64, c_ptr
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import free, load_i32, ptr_is_null, store_i32, store_ptr


py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)
py_bigint_from_i64 = extern("py_bigint_from_i64", (c_int64,), c_ptr)
py_bigint_add = extern("py_bigint_add", (c_ptr, c_ptr), c_ptr)
py_bigint_sub = extern("py_bigint_sub", (c_ptr, c_ptr), c_ptr)
py_bigint_shl = extern("py_bigint_shl", (c_ptr, c_int64), c_ptr)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _copy_abs(a):
    ndigits: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    r = py_bigint_alloc(ndigits)
    if ptr_is_null(r):
        return r
    if ndigits == 0 or load_i32(a, PYINTOBJECT_SIGN_OFFSET) == 0:
        store_i32(r, PYINTOBJECT_SIGN_OFFSET, 0)
        store_i32(r, PYINTOBJECT_NDIGITS_OFFSET, 0)
        return r
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, 1)
    i: int = 0
    while i < ndigits:
        store_i32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, load_i32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4))
        i = i + 1
    return r


def _abs_cmp(a, b) -> int:
    na: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    nb: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    if na != nb:
        if na < nb:
            return -1
        return 1
    i: int = na - 1
    while i >= 0:
        av: int = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        bv: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        if av != bv:
            if av < bv:
                return -1
            return 1
        i = i - 1
    return 0


def _bit_length_mag(a) -> int:
    ndigits: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return 0
    top: int = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + (ndigits - 1) * 4)
    bits: int = (ndigits - 1) * 32
    while top > 0:
        bits = bits + 1
        top = top >> 1
    return bits


def _one_shifted(bits: int):
    one = py_bigint_from_i64(1)
    if ptr_is_null(one):
        return one
    r = py_bigint_shl(one, bits)
    free(one)
    return r


@c_abi_export("py_bigint_divmod")
def py_bigint_divmod(a, b, q_out, r_out) -> int:
    if load_i32(b, PYINTOBJECT_SIGN_OFFSET) == 0:
        return -1

    q = py_bigint_alloc(0)
    if ptr_is_null(q):
        return -1
    r = _copy_abs(a)
    if ptr_is_null(r):
        free(q)
        return -1
    bpos = _copy_abs(b)
    if ptr_is_null(bpos):
        free(q)
        free(r)
        return -1

    b_bits: int = _bit_length_mag(bpos)
    while _abs_cmp(r, bpos) >= 0:
        shift: int = _bit_length_mag(r) - b_bits
        scaled_b = py_bigint_shl(bpos, shift)
        if ptr_is_null(scaled_b):
            free(q)
            free(r)
            free(bpos)
            return -1
        if _abs_cmp(scaled_b, r) > 0:
            free(scaled_b)
            shift = shift - 1
            scaled_b = py_bigint_shl(bpos, shift)
            if ptr_is_null(scaled_b):
                free(q)
                free(r)
                free(bpos)
                return -1

        r2 = py_bigint_sub(r, scaled_b)
        free(r)
        free(scaled_b)
        if ptr_is_null(r2):
            free(q)
            free(bpos)
            return -1
        r = r2

        term = _one_shifted(shift)
        if ptr_is_null(term):
            free(q)
            free(r)
            free(bpos)
            return -1
        q2 = py_bigint_add(q, term)
        free(q)
        free(term)
        if ptr_is_null(q2):
            free(r)
            free(bpos)
            return -1
        q = q2

    free(bpos)

    sa: int = load_i32(a, PYINTOBJECT_SIGN_OFFSET)
    sb: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sa != 0:
        if sa == sb:
            store_i32(q, PYINTOBJECT_SIGN_OFFSET, 1)
        else:
            store_i32(q, PYINTOBJECT_SIGN_OFFSET, -1)
        if load_i32(q, PYINTOBJECT_NDIGITS_OFFSET) == 0:
            store_i32(q, PYINTOBJECT_SIGN_OFFSET, 0)

        if load_i32(r, PYINTOBJECT_NDIGITS_OFFSET) == 0:
            store_i32(r, PYINTOBJECT_SIGN_OFFSET, 0)
        else:
            store_i32(r, PYINTOBJECT_SIGN_OFFSET, sa)

    if sa * sb < 0 and load_i32(r, PYINTOBJECT_NDIGITS_OFFSET) > 0:
        one = py_bigint_from_i64(1)
        if ptr_is_null(one):
            free(q)
            free(r)
            return -1
        q2 = py_bigint_sub(q, one)
        free(one)
        free(q)
        if ptr_is_null(q2):
            free(r)
            return -1
        q = q2

        r2 = py_bigint_add(r, b)
        free(r)
        if ptr_is_null(r2):
            free(q)
            return -1
        r = r2

    store_ptr(q_out, 0, q)
    store_ptr(r_out, 0, r)
    return 0
