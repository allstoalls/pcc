"""pcc-Python replacement for py_runtime/src/py_int_addsub.c."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int64, c_ptr
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import load_i32, ptr_is_null, store_i32


py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _store_u32(obj, offset: int, value: int) -> None:
    store_i32(obj, offset, value)


def _normalize(b) -> None:
    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    while ndigits > 0 and _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + (ndigits - 1) * 4) == 0:
        ndigits = ndigits - 1
    store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, ndigits)
    if ndigits == 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 0)
    elif load_i32(b, PYINTOBJECT_SIGN_OFFSET) == 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 1)


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


def _abs_add(a, b, sign_if_nonzero: int):
    la: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    lb: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    lr: int = lb + 1
    if la > lb:
        lr = la + 1
    r = py_bigint_alloc(lr)
    if ptr_is_null(r):
        return r
    carry: int = 0
    i: int = 0
    while i < lr:
        av: int = 0
        bv: int = 0
        if i < la:
            av = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        if i < lb:
            bv = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        total: int = av + bv + carry
        _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, total & 4294967295)
        carry = total >> 32
        i = i + 1
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, sign_if_nonzero)
    _normalize(r)
    return r


def _abs_sub(a, b, sign_if_nonzero: int):
    la: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    lb: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    r = py_bigint_alloc(la)
    if ptr_is_null(r):
        return r
    borrow: int = 0
    i: int = 0
    while i < la:
        av: int = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        bv: int = 0
        if i < lb:
            bv = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + i * 4)
        diff: int = av - bv - borrow
        if diff < 0:
            diff = diff + 4294967296
            borrow = 1
        else:
            borrow = 0
        _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + i * 4, diff & 4294967295)
        i = i + 1
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, sign_if_nonzero)
    _normalize(r)
    return r


@c_abi_export("py_bigint_add")
def py_bigint_add(a, b):
    sa: int = load_i32(a, PYINTOBJECT_SIGN_OFFSET)
    sb: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sa == 0:
        return _bigint_copy(b)
    if sb == 0:
        return _bigint_copy(a)
    if sa == sb:
        return _abs_add(a, b, sa)
    c: int = _abs_cmp(a, b)
    if c == 0:
        return py_bigint_alloc(0)
    if c > 0:
        return _abs_sub(a, b, sa)
    return _abs_sub(b, a, sb)


@c_abi_export("py_bigint_sub")
def py_bigint_sub(a, b):
    sa: int = load_i32(a, PYINTOBJECT_SIGN_OFFSET)
    sb: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sb == 0:
        return _bigint_copy(a)
    if sa == 0:
        r = _bigint_copy(b)
        if ptr_is_null(r):
            return r
        store_i32(r, PYINTOBJECT_SIGN_OFFSET, 0 - load_i32(r, PYINTOBJECT_SIGN_OFFSET))
        return r
    if sa != sb:
        return _abs_add(a, b, sa)
    c: int = _abs_cmp(a, b)
    if c == 0:
        return py_bigint_alloc(0)
    if c > 0:
        return _abs_sub(a, b, sa)
    return _abs_sub(b, a, 0 - sa)
