"""pcc-Python replacement for py_runtime/src/py_int_bitwise.c."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int64, c_ptr
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import calloc, free, load_i32, null, ptr_is_null, store_i32


py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _store_u32(obj, offset: int, value: int) -> None:
    store_i32(obj, offset, value)


def _arr_load_u32(arr, idx: int) -> int:
    return _load_u32(arr, idx * 4)


def _arr_store_u32(arr, idx: int, value: int) -> None:
    _store_u32(arr, idx * 4, value)


def _normalize(b) -> None:
    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    while ndigits > 0 and _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + (ndigits - 1) * 4) == 0:
        ndigits = ndigits - 1
    store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, ndigits)
    if ndigits == 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 0)
    elif load_i32(b, PYINTOBJECT_SIGN_OFFSET) == 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 1)


def _encode_twos(src, out, n: int) -> None:
    ndigits: int = load_i32(src, PYINTOBJECT_NDIGITS_OFFSET)
    if load_i32(src, PYINTOBJECT_SIGN_OFFSET) >= 0:
        i: int = 0
        while i < ndigits:
            _arr_store_u32(out, i, _load_u32(src, PYINTOBJECT_DIGITS_OFFSET + i * 4))
            i = i + 1
        return

    carry: int = 0
    i = 0
    while i < n:
        d: int = 0
        if i < ndigits:
            d = _load_u32(src, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        inv: int = 4294967295 - d
        if i == 0:
            inv = inv + 1
        inv = inv + carry
        _arr_store_u32(out, i, inv & 4294967295)
        carry = inv >> 32
        i = i + 1


def _bitop(a, b, op: int):
    na: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    nb: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    n: int = nb + 1
    if na > nb:
        n = na + 1

    sa = calloc(n, 4)
    sb = calloc(n, 4)
    if ptr_is_null(sa) or ptr_is_null(sb):
        free(sa)
        free(sb)
        return null()

    _encode_twos(a, sa, n)
    _encode_twos(b, sb, n)

    sr = calloc(n, 4)
    if ptr_is_null(sr):
        free(sa)
        free(sb)
        return null()

    i: int = 0
    while i < n:
        av: int = _arr_load_u32(sa, i)
        bv: int = _arr_load_u32(sb, i)
        rv: int = 0
        if op == 0:
            rv = av & bv
        elif op == 1:
            rv = av | bv
        else:
            rv = av ^ bv
        _arr_store_u32(sr, i, rv)
        i = i + 1

    free(sa)
    free(sb)

    r = py_bigint_alloc(n)
    if ptr_is_null(r):
        free(sr)
        return r

    result_negative: bool = (_arr_load_u32(sr, n - 1) & 2147483648) != 0
    if not result_negative:
        i = 0
        while i < n:
            _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, _arr_load_u32(sr, i))
            i = i + 1
        store_i32(r, PYINTOBJECT_SIGN_OFFSET, 1)
    else:
        carry: int = 1
        i = 0
        while i < n:
            inv = 4294967295 - _arr_load_u32(sr, i) + carry
            _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, inv & 4294967295)
            carry = inv >> 32
            i = i + 1
        store_i32(r, PYINTOBJECT_SIGN_OFFSET, -1)

    free(sr)
    _normalize(r)
    return r


@c_abi_export("py_bigint_and")
def py_bigint_and(a, b):
    return _bitop(a, b, 0)


@c_abi_export("py_bigint_or")
def py_bigint_or(a, b):
    return _bitop(a, b, 1)


@c_abi_export("py_bigint_xor")
def py_bigint_xor(a, b):
    return _bitop(a, b, 2)
