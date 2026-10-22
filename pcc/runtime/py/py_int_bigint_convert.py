"""Phase 4c: pcc-Python port of py_int_bigint_convert.c."""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export
from pcc.runtime.py.py_abi_constants import PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET
from pcc.unsafe import load_i32, ptr_is_null, store_i32


def _set_overflow(slot, value: int) -> None:
    if not ptr_is_null(slot):
        store_i32(slot, 0, value)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


@c_abi_export("py_bigint_to_i64")
def py_bigint_to_i64(b, overflow) -> int:
    _set_overflow(overflow, 0)
    sign: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0

    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits > 2:
        _set_overflow(overflow, 1)
        return 0
    if ndigits <= 0:
        return 0

    low: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4)

    if sign > 0:
        if high > 2147483647:
            _set_overflow(overflow, 1)
            return 0
        return high * 4294967296 + low

    if high > 2147483648:
        _set_overflow(overflow, 1)
        return 0
    if high == 2147483648:
        if low != 0:
            _set_overflow(overflow, 1)
            return 0
        min_i64: int = -9223372036854775807
        return min_i64 - 1
    return 0 - (high * 4294967296 + low)
