"""pcc-Python replacement for py_runtime/src/py_int_shift.c."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import (
    cstr,
    free,
    load_i32,
    logical_shift_left_i64,
    null,
    ptr_is_null,
    store_i32,
)


py_bigint_alloc = extern("py_bigint_alloc", (c_int64,), c_ptr)
py_bigint_from_i64 = extern("py_bigint_from_i64", (c_int64,), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)


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


def _shl_digits_and_bits(a, ndigits_shift: int, bit_shift: int):
    if load_i32(a, PYINTOBJECT_SIGN_OFFSET) == 0:
        return py_bigint_alloc(0)
    src_len: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    r = py_bigint_alloc(src_len + ndigits_shift + 1)
    if ptr_is_null(r):
        return r
    carry: int = 0
    i: int = 0
    factor: int = logical_shift_left_i64(1, bit_shift)
    while i < src_len:
        cur: int = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4) * factor + carry
        _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + (i + ndigits_shift) * 4, cur & 4294967295)
        carry = cur >> 32
        i = i + 1
    _store_u32(r, PYINTOBJECT_DIGITS_OFFSET + (src_len + ndigits_shift) * 4, carry)
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, load_i32(a, PYINTOBJECT_SIGN_OFFSET))
    _normalize(r)
    return r


@c_abi_export("py_bigint_shl")
def py_bigint_shl(a, bits: int):
    if load_i32(a, PYINTOBJECT_SIGN_OFFSET) == 0 or bits == 0:
        return _bigint_copy(a)
    nd: int = bits // 32
    nb: int = bits % 32
    src_len: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)
    # ndigits is stored in a signed i32. Check capacity before addition,
    # allocation, or store_i32 can wrap it into an invalid object layout.
    # This is PCC allocation inability, not Python's enormous-count overflow;
    # the public shift entry classifies the latter before entering this kernel.
    extra: int = 0
    if nb != 0:
        extra = 1
    if nd > 2147483647 - src_len - extra:
        py_raise_owned(py_exc_new(19, cstr("")))
        return null()
    if nb == 0:
        r = py_bigint_alloc(src_len + nd)
        if ptr_is_null(r):
            return r
        i: int = 0
        while i < src_len:
            store_i32(r, PYINTOBJECT_DIGITS_OFFSET + (i + nd) * 4, load_i32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4))
            i = i + 1
        store_i32(r, PYINTOBJECT_SIGN_OFFSET, load_i32(a, PYINTOBJECT_SIGN_OFFSET))
        _normalize(r)
        return r
    return _shl_digits_and_bits(a, nd, nb)


@c_abi_export("py_bigint_shr")
def py_bigint_shr(a, bits: int):
    if load_i32(a, PYINTOBJECT_SIGN_OFFSET) == 0:
        return py_bigint_alloc(0)
    nd: int = bits // 32
    nb: int = bits % 32
    src_len: int = load_i32(a, PYINTOBJECT_NDIGITS_OFFSET)

    if nd >= src_len:
        if load_i32(a, PYINTOBJECT_SIGN_OFFSET) < 0:
            return py_bigint_from_i64(-1)
        return py_bigint_alloc(0)

    new_len: int = src_len - nd
    mag = py_bigint_alloc(new_len)
    if ptr_is_null(mag):
        return mag

    i: int = 0
    while i < new_len:
        low: int = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + (i + nd) * 4)
        high: int = 0
        if i + nd + 1 < src_len:
            high = _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + (i + nd + 1) * 4)
        cur: int = low
        if nb != 0:
            cur = (
                (low >> nb)
                | (high * logical_shift_left_i64(1, 32 - nb))
            )
        _store_u32(mag, PYINTOBJECT_DIGITS_OFFSET + i * 4, cur & 4294967295)
        i = i + 1
    store_i32(mag, PYINTOBJECT_SIGN_OFFSET, 1)
    _normalize(mag)

    if load_i32(a, PYINTOBJECT_SIGN_OFFSET) > 0:
        return mag

    tail_nonzero: bool = False
    if nb != 0:
        mask: int = logical_shift_left_i64(1, nb) - 1
        if (_load_u32(a, PYINTOBJECT_DIGITS_OFFSET + nd * 4) & mask) != 0:
            tail_nonzero = True
    i = 0
    while i < nd and not tail_nonzero:
        if _load_u32(a, PYINTOBJECT_DIGITS_OFFSET + i * 4) != 0:
            tail_nonzero = True
        i = i + 1

    if tail_nonzero:
        carry: int = 1
        i = 0
        while i < load_i32(mag, PYINTOBJECT_NDIGITS_OFFSET) and carry != 0:
            cur = _load_u32(mag, PYINTOBJECT_DIGITS_OFFSET + i * 4) + carry
            _store_u32(mag, PYINTOBJECT_DIGITS_OFFSET + i * 4, cur & 4294967295)
            carry = cur >> 32
            i = i + 1
        if carry != 0:
            old_len: int = load_i32(mag, PYINTOBJECT_NDIGITS_OFFSET)
            grow = py_bigint_alloc(old_len + 1)
            if ptr_is_null(grow):
                free(mag)
                return grow
            i = 0
            while i < old_len:
                store_i32(grow, PYINTOBJECT_DIGITS_OFFSET + i * 4, load_i32(mag, PYINTOBJECT_DIGITS_OFFSET + i * 4))
                i = i + 1
            _store_u32(grow, PYINTOBJECT_DIGITS_OFFSET + old_len * 4, carry)
            store_i32(grow, PYINTOBJECT_SIGN_OFFSET, 1)
            _normalize(grow)
            free(mag)
            mag = grow

    store_i32(mag, PYINTOBJECT_SIGN_OFFSET, -1)
    _normalize(mag)
    return mag
