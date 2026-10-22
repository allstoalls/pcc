"""pcc-Python replacement for most public py_int_* operation dispatch."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_double, c_int32, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import (
    PYFLOATOBJECT_VALUE_OFFSET,
    PYINTOBJECT_DIGITS_OFFSET,
    PYINTOBJECT_NDIGITS_OFFSET,
    PYINTOBJECT_SIGN_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_TYPE_BOOL,
    PY_TYPE_FLOAT,
    PY_TYPE_INT,
)
from pcc.unsafe import (
    cstr,
    free,
    global_load_ptr,
    is_tagged_int,
    logical_shift_left_i64,
    load_i32,
    load_ptr,
    malloc,
    null,
    ptr_eq,
    ptr_is_null,
    store_f64,
    store_i32,
    store_i64,
    untag_int,
)


py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_cmp = extern("py_int_cmp", (c_ptr, c_ptr), c_int64)
py_int_bit_length = extern("py_int_bit_length", (c_ptr,), c_int64)
py_bigint_from_any = extern("py_bigint_from_any", (c_ptr,), c_ptr)
py_bigint_to_pyobject = extern("py_bigint_to_pyobject", (c_ptr,), c_ptr)
py_bigint_add = extern("py_bigint_add", (c_ptr, c_ptr), c_ptr)
py_bigint_sub = extern("py_bigint_sub", (c_ptr, c_ptr), c_ptr)
py_bigint_mul = extern("py_bigint_mul", (c_ptr, c_ptr), c_ptr)
py_bigint_neg = extern("py_bigint_neg", (c_ptr,), c_ptr)
py_bigint_divmod = extern("py_bigint_divmod", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_bigint_pow = extern("py_bigint_pow", (c_ptr, c_ptr), c_ptr)
py_bigint_to_double = extern("py_bigint_to_double", (c_ptr,), c_double)
py_bigint_and = extern("py_bigint_and", (c_ptr, c_ptr), c_ptr)
py_bigint_or = extern("py_bigint_or", (c_ptr, c_ptr), c_ptr)
py_bigint_xor = extern("py_bigint_xor", (c_ptr, c_ptr), c_ptr)
py_bigint_shl = extern("py_bigint_shl", (c_ptr, c_int64), c_ptr)
py_bigint_shr = extern("py_bigint_shr", (c_ptr, c_int64), c_ptr)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pow_c = extern("pow", (c_double, c_double), c_double)
py_float_pow = extern("py_float_pow", (c_ptr, c_ptr), c_ptr)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)


def _load_u32(obj, offset: int) -> int:
    v: int = load_i32(obj, offset)
    if v < 0:
        v = v + 4294967296
    return v


def _both_tagged(a, b) -> bool:
    return is_tagged_int(a) and is_tagged_int(b)


def _wrap_bigint(b):
    if ptr_is_null(b):
        return null()
    return py_bigint_to_pyobject(b)


def _binary_bigint(a, b, op: int):
    ba = py_bigint_from_any(a)
    bb = py_bigint_from_any(b)
    if ptr_is_null(ba) or ptr_is_null(bb):
        free(ba)
        free(bb)
        return null()
    br = null()
    if op == 0:
        br = py_bigint_add(ba, bb)
    elif op == 1:
        br = py_bigint_sub(ba, bb)
    elif op == 2:
        br = py_bigint_mul(ba, bb)
    elif op == 3:
        br = py_bigint_and(ba, bb)
    elif op == 4:
        br = py_bigint_or(ba, bb)
    else:
        br = py_bigint_xor(ba, bb)
    free(ba)
    free(bb)
    return _wrap_bigint(br)


def _heap_int_fits_i64(o) -> bool:
    if ptr_is_null(o):
        return False
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_INT:
        return False
    sign: int = load_i32(o, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return True
    ndigits: int = load_i32(o, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return True
    if ndigits > 2:
        return False
    low: int = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET + 4)
    if sign > 0:
        return high <= 2147483647
    if high < 2147483648:
        return True
    return high == 2147483648 and low == 0


def _heap_int_i64_value(o) -> int:
    sign: int = load_i32(o, PYINTOBJECT_SIGN_OFFSET)
    if sign == 0:
        return 0
    ndigits: int = load_i32(o, PYINTOBJECT_NDIGITS_OFFSET)
    if ndigits <= 0:
        return 0
    low: int = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET)
    high: int = 0
    if ndigits == 2:
        high = _load_u32(o, PYINTOBJECT_DIGITS_OFFSET + 4)
    if sign > 0:
        return high * 4294967296 + low
    if high == 2147483648 and low == 0:
        return -9223372036854775807 - 1
    return 0 - (high * 4294967296 + low)


def _int_fits_i64(o) -> bool:
    if is_tagged_int(o):
        return True
    return _heap_int_fits_i64(o)


def _int_i64_value(o) -> int:
    if is_tagged_int(o):
        return untag_int(o)
    return _heap_int_i64_value(o)


def _int_to_double(o) -> float:
    if is_tagged_int(o):
        return float(untag_int(o))
    if load_i32(o, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        return 1.0 if ptr_eq(o, global_load_ptr("py_True")) != 0 else 0.0
    return py_bigint_to_double(o)


def _float_new(v: float):
    f = pcc_gc_alloc(24, PY_TYPE_FLOAT, 0)
    if ptr_is_null(f):
        return f
    store_f64(f, PYFLOATOBJECT_VALUE_OFFSET, v)
    return f


@c_abi_export("py_int_add")
def py_int_add(a, b):
    if _both_tagged(a, b):
        return py_int_from_i64(untag_int(a) + untag_int(b))
    return _binary_bigint(a, b, 0)


@c_abi_export("py_int_sub")
def py_int_sub(a, b):
    if _both_tagged(a, b):
        return py_int_from_i64(untag_int(a) - untag_int(b))
    return _binary_bigint(a, b, 1)


@c_abi_export("py_int_mul")
def py_int_mul(a, b):
    if _both_tagged(a, b):
        av: int = untag_int(a)
        bv: int = untag_int(b)
        if av >= -3037000499 and av <= 3037000499:
            if bv >= -3037000499 and bv <= 3037000499:
                return py_int_from_i64(av * bv)
    return _binary_bigint(a, b, 2)


@c_abi_export("py_int_neg")
def py_int_neg(a):
    if is_tagged_int(a):
        return py_int_from_i64(0 - untag_int(a))
    ba = py_bigint_from_any(a)
    if ptr_is_null(ba):
        return null()
    br = py_bigint_neg(ba)
    free(ba)
    return _wrap_bigint(br)


@c_abi_export("py_int_floordiv")
def py_int_floordiv(a, b):
    if _both_tagged(a, b):
        av: int = untag_int(a)
        bv: int = untag_int(b)
        if bv == 0:
            return null()
        # Tagged-int fast path. ``av`` and ``bv`` are in [-2^62, 2^62-1]
        # so ``av // bv`` cannot overflow i64.
        return py_int_from_i64(av // bv)
    ba = py_bigint_from_any(a)
    bb = py_bigint_from_any(b)
    if ptr_is_null(ba) or ptr_is_null(bb):
        free(ba)
        free(bb)
        return null()
    qslot = malloc(8)
    rslot = malloc(8)
    if ptr_is_null(qslot) or ptr_is_null(rslot):
        free(ba)
        free(bb)
        free(qslot)
        free(rslot)
        return null()
    ok: int = py_bigint_divmod(ba, bb, qslot, rslot)
    free(ba)
    free(bb)
    if ok != 0:
        free(qslot)
        free(rslot)
        return null()
    q = load_ptr(qslot, 0)
    r = load_ptr(rslot, 0)
    free(qslot)
    free(rslot)
    free(r)
    return _wrap_bigint(q)


@c_abi_export("py_int_mod")
def py_int_mod(a, b):
    if _both_tagged(a, b):
        av: int = untag_int(a)
        bv: int = untag_int(b)
        if bv == 0:
            return null()
        # Tagged-int fast path. ``a % b`` in pcc-Python under
        # ``pcc.unsafe`` lowers to a Python-semantics modulo (srem +
        # sign correction in codegen), so we get Python mod sign
        # convention directly.
        return py_int_from_i64(av % bv)
    ba = py_bigint_from_any(a)
    bb = py_bigint_from_any(b)
    if ptr_is_null(ba) or ptr_is_null(bb):
        free(ba)
        free(bb)
        return null()
    qslot = malloc(8)
    rslot = malloc(8)
    if ptr_is_null(qslot) or ptr_is_null(rslot):
        free(ba)
        free(bb)
        free(qslot)
        free(rslot)
        return null()
    ok: int = py_bigint_divmod(ba, bb, qslot, rslot)
    free(ba)
    free(bb)
    if ok != 0:
        free(qslot)
        free(rslot)
        return null()
    q = load_ptr(qslot, 0)
    r = load_ptr(rslot, 0)
    free(qslot)
    free(rslot)
    free(q)
    return _wrap_bigint(r)


@c_abi_export("py_int_truediv")
def py_int_truediv(a, b):
    if is_tagged_int(b):
        if untag_int(b) == 0:
            return null()
    elif load_i32(b, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        if ptr_eq(b, global_load_ptr("py_False")) != 0:
            return null()
    elif load_i32(b, PYINTOBJECT_SIGN_OFFSET) == 0:
        return null()
    return _float_new(_int_to_double(a) / _int_to_double(b))


@c_abi_export("py_int_pow")
def py_int_pow(a, b):
    # CPython's int_pow hands a negative exponent to float_pow, which owns
    # the zero-to-a-negative-power error, the sign rules and overflow.
    if _int_shift_sign(b) < 0:
        return py_float_pow(a, b)

    ba = py_bigint_from_any(a)
    bb = py_bigint_from_any(b)
    if ptr_is_null(ba) or ptr_is_null(bb):
        free(ba)
        free(bb)
        return null()
    br = py_bigint_pow(ba, bb)
    free(ba)
    free(bb)
    return _wrap_bigint(br)


@c_abi_export("py_int_and")
def py_int_and(a, b):
    if _both_tagged(a, b):
        return py_int_from_i64(untag_int(a) & untag_int(b))
    return _binary_bigint(a, b, 3)


@c_abi_export("py_int_or")
def py_int_or(a, b):
    if _both_tagged(a, b):
        return py_int_from_i64(untag_int(a) | untag_int(b))
    return _binary_bigint(a, b, 4)


@c_abi_export("py_int_xor")
def py_int_xor(a, b):
    if _both_tagged(a, b):
        return py_int_from_i64(untag_int(a) ^ untag_int(b))
    return _binary_bigint(a, b, 5)


def _int_shift_count(b) -> int:
    # Decode the sign before projecting the count. An out-of-i64 positive
    # count is represented by a saturation sentinel, not truncated: no int
    # with the signed-i32 limb count can survive that many right shifts or
    # admit that many left shifts. Negative counts always raise, even for 0.
    n: int = 0
    if is_tagged_int(b):
        n = untag_int(b)
    elif not ptr_is_null(b) and load_i32(b, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        if ptr_eq(b, global_load_ptr("py_True")):
            n = 1
    elif not ptr_is_null(b) and load_i32(b, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_INT:
        if load_i32(b, PYINTOBJECT_SIGN_OFFSET) < 0:
            n = -1
        elif _heap_int_fits_i64(b):
            n = _heap_int_i64_value(b)
        else:
            n = 9223372036854775807
    else:
        py_raise_owned(py_exc_new(3, cstr("shift count must be an integer")))
        return -1
    if n < 0:
        py_raise_owned(py_exc_new(2, cstr("negative shift count")))
        return -1
    return n


def _int_shift_sign(a) -> int:
    if is_tagged_int(a):
        value: int = untag_int(a)
        if value < 0:
            return -1
        if value > 0:
            return 1
        return 0
    if load_i32(a, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        if ptr_eq(a, global_load_ptr("py_True")):
            return 1
        return 0
    return load_i32(a, PYINTOBJECT_SIGN_OFFSET)


def _int_shift_allocation_failed():
    # Keep a specific error raised by the arithmetic or allocation path intact.
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(19, cstr("")))
    return null()


@c_abi_export("py_int_shl")
def py_int_shl(a, b):
    n: int = _int_shift_count(b)
    if n < 0:
        return null()
    if n == 0:
        if is_tagged_int(a):
            return a
        if load_i32(a, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
            return py_int_from_i64(_int_shift_sign(a))
        py_incref(a)
        return a
    if _int_shift_sign(a) == 0:
        return py_int_from_i64(0)
    if n == 9223372036854775807:
        py_raise_owned(py_exc_new(15, cstr("too many digits in integer")))
        return null()
    if n > 68719476672:
        # Distinguish Python's enormous-size overflow from this runtime's
        # smaller allocation capacity. CPython 3.15 on 64-bit targets uses
        # 30-bit reference digits, with MAX_LONG_DIGITS=(INT64_MAX-1)//30;
        # long_lshift1 reserves oldsize + n//30 + (n%30 != 0).
        # Our signed-i32 count of 32-bit limbs is only a storage limit.
        bits: int = py_int_bit_length(a)
        if bits == 0:
            # The only nonzero integer operand lacking an int header is True.
            bits = 1
        oldsize: int = (bits + 29) // 30
        shift_digits: int = n // 30
        if n % 30 != 0:
            shift_digits = shift_digits + 1
        if shift_digits > 307445734561825860 - oldsize:
            py_raise_owned(py_exc_new(15, cstr("too many digits in integer")))
            return null()
        return _int_shift_allocation_failed()
    if is_tagged_int(a) and n < 63:
        # This is the proven machine projection inside the bigint primitive:
        # n < 63 and the following bound check proves av * factor fits i64.
        # Spell it as an intrinsic so compiling the implementation cannot
        # recursively lower this operation back to py_int_shl.
        factor: int = logical_shift_left_i64(1, n)
        av: int = untag_int(a)
        limit: int = 9223372036854775807 // factor
        if av >= (0 - limit) and av <= limit:
            return py_int_from_i64(av * factor)
    ba = py_bigint_from_any(a)
    if ptr_is_null(ba):
        return _int_shift_allocation_failed()
    br = py_bigint_shl(ba, n)
    free(ba)
    result = _wrap_bigint(br)
    if ptr_is_null(result):
        return _int_shift_allocation_failed()
    return result


@c_abi_export("py_int_shr")
def py_int_shr(a, b):
    n: int = _int_shift_count(b)
    if n < 0:
        return null()
    if n == 0:
        if is_tagged_int(a):
            return a
        if load_i32(a, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
            return py_int_from_i64(_int_shift_sign(a))
        py_incref(a)
        return a
    # Every representable integer has at most INT32_MAX * 32 magnitude bits.
    if n >= 68719476704:
        if _int_shift_sign(a) < 0:
            return py_int_from_i64(-1)
        return py_int_from_i64(0)
    if is_tagged_int(a):
        av: int = untag_int(a)
        if n >= 63:
            if av < 0:
                return py_int_from_i64(-1)
            return py_int_from_i64(0)
        return py_int_from_i64(av >> n)
    ba = py_bigint_from_any(a)
    if ptr_is_null(ba):
        return _int_shift_allocation_failed()
    br = py_bigint_shr(ba, n)
    free(ba)
    result = _wrap_bigint(br)
    if ptr_is_null(result):
        return _int_shift_allocation_failed()
    return result


def _int_mod_inverse(base, modulus):
    """CPython's long_invmod: ``base``'s inverse modulo a positive modulus.

    Returns a NEW int in [0, modulus), or NULL with ValueError when the base
    shares a factor with the modulus.
    """
    zero = py_int_from_i64(0)
    one = py_int_from_i64(1)
    a = py_int_mod(base, modulus)
    n = modulus
    py_incref(n)
    b = py_int_from_i64(1)
    c = py_int_from_i64(0)
    failed: int = 0
    if ptr_is_null(zero) or ptr_is_null(one) or ptr_is_null(a) or ptr_is_null(b) or ptr_is_null(c):
        failed = 1
    while failed == 0 and py_int_cmp(n, zero) != 0:
        # (a, n) <- (n, a mod n); b and c carry a's Bezout coefficient.
        q = py_int_floordiv(a, n)
        r = py_int_mod(a, n)
        t = null()
        if not ptr_is_null(q):
            t = py_int_mul(q, c)
        s = null()
        if not ptr_is_null(t):
            s = py_int_sub(b, t)
        py_decref(q)
        py_decref(t)
        if ptr_is_null(r) or ptr_is_null(s):
            py_decref(r)
            py_decref(s)
            failed = 1
        else:
            py_decref(a)
            a = n
            n = r
            py_decref(b)
            b = c
            c = s
    inverse = null()
    if failed == 0:
        if py_int_cmp(a, one) != 0:
            py_raise_owned(py_exc_new(2, cstr("base is not invertible for the given modulus")))
        else:
            inverse = py_int_mod(b, modulus)
    py_decref(a)
    py_decref(n)
    py_decref(b)
    py_decref(c)
    py_decref(one)
    py_decref(zero)
    return inverse


@c_abi_export("py_int_pow_mod")
def py_int_pow_mod(base, exp, mod):
    """``pow(base, exp, mod)`` of ints, as CPython's long_pow.

    A zero modulus raises. Modulus 1 (or -1) answers 0 before the exponent
    is looked at. A negative exponent raises the base's modular inverse
    (ValueError when there is none) to the negated exponent. The result has
    the modulus's sign: a negative modulus gives a value in (mod, 0].
    """
    if ptr_is_null(base) or ptr_is_null(exp) or ptr_is_null(mod):
        return null()
    zero = py_int_from_i64(0)
    one = py_int_from_i64(1)
    if ptr_is_null(zero) or ptr_is_null(one):
        py_decref(one)
        py_decref(zero)
        return null()
    mod_sign: int = py_int_cmp(mod, zero)
    if mod_sign == 0:
        py_raise_owned(py_exc_new(2, cstr("pow() 3rd argument cannot be 0")))
        py_decref(one)
        py_decref(zero)
        return null()
    modulus = null()
    if mod_sign < 0:
        modulus = py_int_neg(mod)
    else:
        modulus = mod
        py_incref(modulus)
    if ptr_is_null(modulus):
        py_decref(one)
        py_decref(zero)
        return null()
    if py_int_cmp(modulus, one) == 0:
        py_decref(modulus)
        py_decref(one)
        return zero
    b = null()
    e = null()
    if py_int_cmp(exp, zero) < 0:
        e = py_int_neg(exp)
        if not ptr_is_null(e):
            b = _int_mod_inverse(base, modulus)
    else:
        e = exp
        py_incref(e)
        b = py_int_mod(base, modulus)
    result = null()
    if not ptr_is_null(b) and not ptr_is_null(e):
        result = py_int_from_i64(1)
    # Square-and-multiply, reducing every step: b ** e is never materialised.
    while not ptr_is_null(result) and py_int_cmp(e, zero) > 0:
        bit = py_int_and(e, one)
        if ptr_is_null(bit):
            py_decref(result)
            result = null()
        else:
            odd: int = py_int_cmp(bit, zero)
            py_decref(bit)
            if odd != 0:
                product = py_int_mul(result, b)
                reduced = null()
                if not ptr_is_null(product):
                    reduced = py_int_mod(product, modulus)
                py_decref(product)
                py_decref(result)
                result = reduced
            squared = null()
            if not ptr_is_null(result):
                squared = py_int_mul(b, b)
            next_base = null()
            if not ptr_is_null(squared):
                next_base = py_int_mod(squared, modulus)
            py_decref(squared)
            py_decref(b)
            b = next_base
            next_exp = null()
            if not ptr_is_null(b):
                next_exp = py_int_shr(e, one)
            py_decref(e)
            e = next_exp
            if ptr_is_null(b) or ptr_is_null(e):
                py_decref(result)
                result = null()
    py_decref(b)
    py_decref(e)
    if not ptr_is_null(result) and mod_sign < 0 and py_int_cmp(result, zero) != 0:
        shifted = py_int_sub(result, modulus)
        py_decref(result)
        result = shifted
    py_decref(modulus)
    py_decref(one)
    py_decref(zero)
    return result


@c_abi_export("py_int_isqrt")
def py_int_isqrt(n):
    if ptr_is_null(n):
        return null()
    zero = py_int_from_i64(0)
    if ptr_is_null(zero):
        return null()
    if py_int_cmp(n, zero) < 0:
        py_raise_owned(py_exc_new(2, cstr("isqrt() argument must be nonnegative")))
        py_decref(zero)
        return null()
    if py_int_cmp(n, zero) == 0:
        return zero

    one = py_int_from_i64(1)
    two = py_int_from_i64(2)
    if ptr_is_null(one) or ptr_is_null(two):
        py_decref(one)
        py_decref(two)
        py_decref(zero)
        return null()

    bits: int = py_int_bit_length(n)
    shift = py_int_from_i64((bits + 1) // 2)
    x = null()
    if not ptr_is_null(shift):
        x = py_int_shl(one, shift)
    py_decref(shift)
    if ptr_is_null(x):
        py_decref(one)
        py_decref(two)
        py_decref(zero)
        return null()

    while 1:
        q = py_int_floordiv(n, x)
        total = null()
        if not ptr_is_null(q):
            total = py_int_add(x, q)
        py_decref(q)
        y = null()
        if not ptr_is_null(total):
            y = py_int_floordiv(total, two)
        py_decref(total)
        if ptr_is_null(y):
            py_decref(x)
            x = null()
            break
        if py_int_cmp(y, x) >= 0:
            py_decref(y)
            break
        py_decref(x)
        x = y

    py_decref(one)
    py_decref(two)
    py_decref(zero)
    return x
