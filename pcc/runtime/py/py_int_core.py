"""pcc-Python replacement for py_runtime/src/py_int_core.c.

This slice owns the int object boundary: tagged-int encode/decode,
small heap bignum allocation, canonical PyObject conversion, and the
double conversion used by true division. The heavier arithmetic still
lives in py_int.c for now.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, extern
from pcc.runtime.py.py_abi_constants import PYFLOATOBJECT_VALUE_OFFSET, PYINTOBJECT_DIGITS_OFFSET, PYINTOBJECT_NDIGITS_OFFSET, PYINTOBJECT_SIGN_OFFSET, PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_REFCOUNT_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_FLAG_GC_MALLOC_ALLOC, PY_OBJ_CMP_UNORDERED, PY_TYPE_BOOL, PY_TYPE_FLOAT, PY_TYPE_INT

pcc_gc_pointer_register = extern(
    "pcc_gc_pointer_register", (c_ptr,), c_int64
)
from pcc.unsafe import (
    free,
    global_load_ptr,
    is_tagged_int,
    load_f64,
    load_i32,
    malloc,
    null,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    tag_int,
    untag_int,
)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _store_u32(obj, offset: int, value: int) -> None:
    store_i32(obj, offset, value)


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


def _bigint_abs_cmp(a, b) -> int:
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


def _bigint_tagged_fit_value(b) -> int:
    sign: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0
    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return 0
    if ndigits > 2:
        return 0

    low: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4)

    if sign > 0:
        if high > 1073741823:
            return 0
        return high * 4294967296 + low

    if high > 1073741824:
        return 0
    if high == 1073741824:
        if low != 0:
            return 0
        return -4611686018427387904
    return 0 - (high * 4294967296 + low)


@c_abi_export("py_int_bit_length")
def py_int_bit_length(n) -> int:
    # int.bit_length(): bits to represent abs(value), 0 for 0. Exact for bignums:
    # (ndigits-1)*32 + bits in the top base-2^32 digit. Mirrors py_int_bit_length
    # in py_int_core.c. tagged-int path only sees i63-range values (negatable).
    if ptr_is_null(n) != 0:
        return 0
    if is_tagged_int(n) != 0:
        a: int = untag_int(n)
        if a < 0:
            a = 0 - a
        bits: int = 0
        while a > 0:
            bits = bits + 1
            a = a >> 1
        return bits
    tag: int = load_i32(n, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_INT:                            # PY_TYPE_INT bignum
        ndigits: int = load_i32(n, PYINTOBJECT_NDIGITS_OFFSET)
        if ndigits <= 0:
            return 0
        top: int = _load_u32(n, PYINTOBJECT_DIGITS_OFFSET + (ndigits - 1) * 4)
        top_bits: int = 0
        while top > 0:
            top_bits = top_bits + 1
            top = top >> 1
        return (ndigits - 1) * 32 + top_bits
    return 0


@c_abi_export("py_int_bit_count")
def py_int_bit_count(n) -> int:
    # int.bit_count(): number of set bits in abs(value), 0 for 0. CPython counts
    # the magnitude, so negatives match their absolute value:
    # (-255).bit_count() == 8. Exact for bignums: popcount each base-2^32 limb
    # (limbs store the magnitude; sign is separate). Mirrors py_int_bit_count in
    # py_int_core.c. tagged-int path only sees i63-range values (negatable).
    if ptr_is_null(n) != 0:
        return 0
    if is_tagged_int(n) != 0:
        a: int = untag_int(n)
        if a < 0:
            a = 0 - a
        bits: int = 0
        while a > 0:
            bits = bits + (a & 1)
            a = a >> 1
        return bits
    tag: int = load_i32(n, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_INT:                            # PY_TYPE_INT bignum
        ndigits: int = load_i32(n, PYINTOBJECT_NDIGITS_OFFSET)
        total: int = 0
        i: int = 0
        while i < ndigits:
            d: int = _load_u32(n, PYINTOBJECT_DIGITS_OFFSET + i * 4)
            while d > 0:
                total = total + (d & 1)
                d = d >> 1
            i = i + 1
        return total
    return 0


def _bigint_fits_tagged(b) -> bool:
    sign: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return True
    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return True
    if ndigits > 2:
        return False

    low: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4)

    if sign > 0:
        return high <= 1073741823
    if high < 1073741824:
        return True
    return high == 1073741824 and low == 0


def _bigint_i64_clamped(b) -> int:
    sign: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0
    ndigits: int = load_i32(b, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return 0
    if ndigits > 2:
        if sign < 0:
            return -9223372036854775807 - 1
        return 9223372036854775807

    low: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4)

    if sign > 0:
        if high > 2147483647:
            return 9223372036854775807
        return high * 4294967296 + low

    if high > 2147483648:
        return -9223372036854775807 - 1
    if high == 2147483648:
        if low != 0:
            return -9223372036854775807 - 1
        return -9223372036854775807 - 1
    return 0 - (high * 4294967296 + low)


def _bigint_bits(b, lo: int, count: int) -> int:
    # Bits lo .. lo + count - 1 of |b| as an integer (count <= 62).
    r: int = 0
    p: int = lo + count - 1
    while p >= lo:
        digit: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + (p >> 5) * 4)
        r = r * 2 + ((digit >> (p & 31)) & 1)
        p = p - 1
    return r


def _bigint_low_bits_nonzero(b, count: int) -> int:
    # 1 when any of the low ``count`` bits of |b| is set: the sticky bit of a
    # rounding that drops them.
    full: int = count >> 5
    i: int = 0
    while i < full:
        if load_i32(b, PYINTOBJECT_DIGITS_OFFSET + i * 4) != 0:
            return 1
        i = i + 1
    rem: int = count & 31
    if rem != 0:
        partial: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + full * 4)
        if (partial & ((1 << rem) - 1)) != 0:
            return 1
    return 0


@c_abi_export("py_bigint_alloc")
def py_bigint_alloc(ndigits: int):
    if ndigits < 0:
        ndigits = 0
    b = malloc(PYINTOBJECT_DIGITS_OFFSET + ndigits * 4)
    if ptr_is_null(b):
        return b
    store_i64(b, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(b, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_INT)
    store_i32(b, PYOBJECTHEADER_FLAGS_OFFSET, 0)
    store_i32(b, PYINTOBJECT_SIGN_OFFSET, 0)
    store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, ndigits)
    i: int = 0
    while i < ndigits:
        store_i32(b, PYINTOBJECT_DIGITS_OFFSET + i * 4, 0)
        i = i + 1
    return b


@c_abi_export("py_bigint_from_i64")
def py_bigint_from_i64(v: int):
    b = py_bigint_alloc(2)
    if ptr_is_null(b):
        return b
    if v == 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 0)
        store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, 0)
        return b

    min_i64: int = -9223372036854775807 - 1
    if v == min_i64:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, -1)
        _store_u32(b, PYINTOBJECT_DIGITS_OFFSET, 0)
        _store_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4, 2147483648)
        store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, 2)
        return b

    u: int = v
    if v < 0:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, -1)
        u = 0 - v
    else:
        store_i32(b, PYINTOBJECT_SIGN_OFFSET, 1)

    low: int = u & 4294967295
    high: int = u >> 32
    _store_u32(b, 24, low)
    _store_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4, high)
    if high != 0:
        store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, 2)
    else:
        store_i32(b, PYINTOBJECT_NDIGITS_OFFSET, 1)
    return b


@c_abi_export("py_bigint_to_pyobject")
def py_bigint_to_pyobject(b):
    if ptr_is_null(b):
        return b
    if _bigint_fits_tagged(b):
        v: int = _bigint_tagged_fit_value(b)
        free(b)
        return tag_int(v)
    store_i32(
        b,
        PYOBJECTHEADER_FLAGS_OFFSET,
        load_i32(b, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_MALLOC_ALLOC,
    )
    if pcc_gc_pointer_register(b) < 0:
        free(b)
        return null()
    return b


@c_abi_export("py_bigint_from_any")
def py_bigint_from_any(o):
    if is_tagged_int(o):
        return py_bigint_from_i64(untag_int(o))
    if ptr_is_null(o):
        return null()
    tag = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    # bool is-a int (CPython): True -> 1, False -> 0, so bool operands flow
    # through every int op (sum, +, *, ...) instead of failing as non-int.
    if tag == PY_TYPE_BOOL:
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            return py_bigint_from_i64(1)
        return py_bigint_from_i64(0)
    if tag != PY_TYPE_INT:
        return null()
    return _bigint_copy(o)


@c_abi_export("py_int_new_heap")
def py_int_new_heap(v: int):
    b = py_bigint_from_i64(v)
    if ptr_is_null(b):
        return b
    store_i32(
        b,
        PYOBJECTHEADER_FLAGS_OFFSET,
        load_i32(b, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_MALLOC_ALLOC,
    )
    if pcc_gc_pointer_register(b) < 0:
        free(b)
        return null()
    return b


@c_abi_export("py_int_value_i64")
def py_int_value_i64(o) -> int:
    if is_tagged_int(o):
        return untag_int(o)
    return _bigint_i64_clamped(o)


@c_abi_export("py_int_from_i64")
def py_int_from_i64(v: int):
    if v >= -4611686018427387904 and v <= 4611686018427387903:
        return tag_int(v)
    return py_bigint_to_pyobject(py_bigint_from_i64(v))


@c_abi_export("py_bigint_to_double")
def py_bigint_to_double(b) -> float:
    # Correctly rounded (half to even), as CPython's float(int).  The old
    # ``r * 2**32 + digit`` walk rounded after every digit, so
    # float(2**96 + 2**43 + 1) came out one ulp low.  Too large a value still
    # becomes inf here; CPython raises OverflowError.
    sign: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0.0
    nbits: int = py_int_bit_length(b)
    r: float = 0.0
    if nbits <= 63:
        # |b| < 2**63: a single int -> double conversion rounds it.
        low: int = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET)
        high: int = 0
        if load_i32(b, PYINTOBJECT_NDIGITS_OFFSET) >= 2:
            high = _load_u32(b, PYINTOBJECT_DIGITS_OFFSET + 4)
        r = float(high * 4294967296 + low)
    else:
        # The top 55 bits with the lowest one ORed with every bit below:
        # 53 mantissa bits, a guard bit and a sticky bit, which the one
        # int -> double conversion rounds half to even exactly.  Scaling back
        # by powers of two is exact until it overflows to inf.
        shift: int = nbits - 55
        top: int = _bigint_bits(b, shift, 55) | _bigint_low_bits_nonzero(b, shift)
        r = float(top)
        while shift >= 32:
            r = r * 4294967296.0
            shift = shift - 32
        while shift > 0:
            r = r * 2.0
            shift = shift - 1
    if sign < 0:
        return 0.0 - r
    return r


@c_abi_export("py_bigint_neg")
def py_bigint_neg(a):
    r = _bigint_copy(a)
    if ptr_is_null(r):
        return r
    store_i32(r, PYINTOBJECT_SIGN_OFFSET, 0 - load_i32(r, PYINTOBJECT_SIGN_OFFSET))
    return r


@c_abi_export("py_bigint_cmp")
def py_bigint_cmp(a, b) -> int:
    sa: int = load_i32(a, PYINTOBJECT_SIGN_OFFSET)
    sb: int = load_i32(b, PYINTOBJECT_SIGN_OFFSET)
    if sa != sb:
        if sa < sb:
            return -1
        return 1
    if sa == 0:
        return 0
    mag: int = _bigint_abs_cmp(a, b)
    if sa > 0:
        return mag
    return 0 - mag


@c_abi_export("py_int_cmp")
def py_int_cmp(a, b) -> int:
    if is_tagged_int(a) and is_tagged_int(b):
        av: int = untag_int(a)
        bv: int = untag_int(b)
        if av < bv:
            return -1
        if av > bv:
            return 1
        return 0
    ba = py_bigint_from_any(a)
    bb = py_bigint_from_any(b)
    r: int = 0
    if not ptr_is_null(ba) and not ptr_is_null(bb):
        r = py_bigint_cmp(ba, bb)
    free(ba)
    free(bb)
    return r


def _f64_cmp(a: float, f: float) -> int:
    if a < f:
        return -1
    if a > f:
        return 1
    if a != f:
        return PY_OBJ_CMP_UNORDERED
    return 0


def _i64_f64_cmp(v: int, f: float) -> int:
    # Rounding is monotone and f is a double, so float(v) < f proves v < f and
    # float(v) > f proves v > f; only a tie needs the exact check.
    d: float = float(v)
    if d < f:
        return -1
    if d > f:
        return 1
    if d != f:
        return PY_OBJ_CMP_UNORDERED
    # f == float(v) is an integer in [-2**63, 2**63].  2**63 is above every
    # i64 and must not reach int() (the conversion would overflow).
    if f >= 9223372036854775808.0:
        return -1
    t: int = int(f)
    if v < t:
        return -1
    if v > t:
        return 1
    return 0


def _bigint_abs_f64_cmp(b, x: float) -> int:
    # Order |b| >= 2**63 against a finite double x >= 0.  e is the bit length
    # of floor(x), found by exact power-of-two scaling down to [0.5, 1).
    e: int = 0
    while x >= 4294967296.0:
        x = x / 4294967296.0
        e = e + 32
    while x >= 1.0:
        x = x * 0.5
        e = e + 1
    nbits: int = py_int_bit_length(b)
    if nbits != e:
        if nbits < e:
            return -1
        return 1
    # Same bit length, at least 64, so the double was an integer: it is its
    # 53-bit significand m times 2**(e - 53).  Compare |b|'s top 53 bits,
    # then whatever |b| has below them.
    m: int = int(x * 9007199254740992.0)
    top: int = _bigint_bits(b, e - 53, 53)
    if top < m:
        return -1
    if top > m:
        return 1
    return _bigint_low_bits_nonzero(b, e - 53)


@c_abi_export("py_int_f64_cmp")
def py_int_f64_cmp(o, f: float) -> int:
    # Three-way compare of an int against a double by exact value, as
    # CPython's float_richcompare: -1 / 0 / 1, or PY_OBJ_CMP_UNORDERED when f
    # is NaN.  Converting the int to a double first rounded 2**53 + 1 onto
    # 2**53 and 10**30 onto 1e30, so those compared equal.
    if is_tagged_int(o):
        return _i64_f64_cmp(untag_int(o), f)
    if ptr_is_null(o):
        return _i64_f64_cmp(0, f)
    tag: int = load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_BOOL:
        if ptr_eq(o, global_load_ptr("py_True")) != 0:
            return _i64_f64_cmp(1, f)
        return _i64_f64_cmp(0, f)
    if tag != PY_TYPE_INT:
        # A float box reaching an int or dyn operand compares as a double,
        # as the py_float_to_f64 unbox this call replaced did.
        other: float = 0.0
        if tag == PY_TYPE_FLOAT:
            other = load_f64(o, PYFLOATOBJECT_VALUE_OFFSET)
        return _f64_cmp(other, f)
    sign: int = load_i32(o, PYINTOBJECT_SIGN_OFFSET)
    ndigits: int = load_i32(o, PYINTOBJECT_NDIGITS_OFFSET)
    if sign == 0 or ndigits <= 1:
        return _i64_f64_cmp(_bigint_i64_clamped(o), f)
    if ndigits == 2 and _load_u32(o, PYINTOBJECT_DIGITS_OFFSET + 4) < 2147483648:
        return _i64_f64_cmp(_bigint_i64_clamped(o), f)
    # |o| >= 2**63.
    if f != f:
        return PY_OBJ_CMP_UNORDERED
    fsign: int = 0
    if f > 0.0:
        fsign = 1
    elif f < 0.0:
        fsign = -1
    if sign != fsign:
        if sign < fsign:
            return -1
        return 1
    x: float = f
    if x < 0.0:
        x = 0.0 - x
    mag: int = -1  # every int is below an infinity
    if x <= 1.7976931348623157e308:
        mag = _bigint_abs_f64_cmp(o, x)
    if sign < 0:
        return 0 - mag
    return mag
