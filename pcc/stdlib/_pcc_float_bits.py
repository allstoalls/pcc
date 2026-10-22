"""IEEE 754 bit patterns for the ``struct`` provider's ``e``/``f``/``d`` codes.

A stdlib provider cannot import compiler packages (a compiled program's module
closure has no ``pcc.ir.support``), so this is the provider-side copy of
``pcc/ir/support/_float_bits.py``'s conversions; keep the two in step.  Integer and
float arithmetic plus ``math.copysign`` only, never float rendering.
"""
from __future__ import annotations

import math


def _sign_bit(f: float) -> int:
    return 1 if math.copysign(1.0, f) < 0.0 else 0


def _float64_to_bits(f: float):
    """Return the IEEE 754 binary64 bit pattern of ``f`` as uint64.

    Deliberately unannotated: the pattern of a negative double is >= 2**63,
    and in pcc's own raw-int build an ``-> int`` return is an i64 lane.  The
    lane unboxed such a pattern to 0 (every negative double constant pcc1
    rendered became +0.0); it now raises OverflowError.  The unannotated
    return keeps the exact int.
    """
    sign = _sign_bit(f)
    if f != f:
        return (sign << 63) | 0x7FF8000000000000
    inf = 1e309
    if f == inf or f == -inf:
        return (sign << 63) | 0x7FF0000000000000
    if f == 0.0:
        return sign << 63
    if f < 0.0:
        f = -f
    exp = 0
    while f >= 2.0:
        f = f * 0.5
        exp += 1
    while f < 1.0:
        f = f * 2.0
        exp -= 1
    mantissa_bits = int((f - 1.0) * 4503599627370496.0)
    significand = (1 << 52) | mantissa_bits
    biased_exp = exp + 1023
    if biased_exp <= 0:
        # The input is already a binary64 value, so scaling it by powers of
        # two above is exact.  A subnormal simply drops the implicit leading
        # bit into the fraction field; no second rounding is required.
        return (sign << 63) | (significand >> (1 - biased_exp))
    if biased_exp >= 0x7FF:
        return (sign << 63) | 0x7FF0000000000000
    return (sign << 63) | (biased_exp << 52) | mantissa_bits


def _bits_to_float64(bits) -> float:
    # ``bits`` is unannotated for the same reason as ``_float64_to_bits``'s
    # return: a uint64 pattern does not fit the i64 lane.  Both that and the
    # signed i64 spelling of the pattern decode correctly below.
    sign = (bits >> 63) & 1
    biased_exp = (bits >> 52) & 0x7FF
    mantissa = bits & ((1 << 52) - 1)
    if biased_exp == 0x7FF:
        inf = 1e309
        if mantissa == 0:
            return -inf if sign else inf
        nan = inf - inf
        return -nan if sign else nan
    if biased_exp == 0:
        if mantissa == 0:
            return -0.0 if sign else 0.0
        f = mantissa / 4503599627370496.0
        # fraction * 2**-1022.  Chunk exact powers of two to keep this helper
        # independent of an accurately parsed 5e-324 literal.
        for _ in range(102):
            f = f * 0.0009765625
        f = f * 0.25
        return -f if sign else f
    m_frac = 1.0 + mantissa / 4503599627370496.0
    exp = biased_exp - 1023
    f = m_frac
    if exp >= 0:
        for _ in range(exp):
            f = f * 2.0
    else:
        for _ in range(-exp):
            f = f * 0.5
    return -f if sign else f


def _round_shift_to_even(value: int, shift: int) -> int:
    if shift > 53:
        return 0
    kept = value >> shift
    halfway = 1 << (shift - 1)
    remainder = value & ((1 << shift) - 1)
    if remainder > halfway or (remainder == halfway and (kept & 1) != 0):
        kept += 1
    return kept


def _narrow_float_to_bits(
    f: float,
    fraction_bits: int,
    exponent_bias: int,
    maximum_exponent: int,
    sign_shift: int,
) -> int:
    bits = _float64_to_bits(f)
    sign = (bits >> 63) & 1
    biased_exp = (bits >> 52) & 0x7FF
    mantissa = bits & ((1 << 52) - 1)
    target_sign = sign << sign_shift
    if biased_exp == 0x7FF:
        if mantissa == 0:
            return target_sign | (maximum_exponent << fraction_bits)
        return (
            target_sign
            | (maximum_exponent << fraction_bits)
            | (1 << (fraction_bits - 1))
        )
    if biased_exp == 0:
        # Every binary64 subnormal is below half the minimum binary32/16
        # subnormal.  Preserve only its sign when narrowing.
        return target_sign

    target_exp = biased_exp - 1023 + exponent_bias
    significand = (1 << 52) | mantissa
    if target_exp <= 0:
        kept = _round_shift_to_even(
            significand,
            52 - fraction_bits + 1 - target_exp,
        )
        if kept >= (1 << fraction_bits):
            # Rounding the largest subnormal can carry into minimum normal.
            return target_sign | (1 << fraction_bits)
        return target_sign | kept

    kept = _round_shift_to_even(significand, 52 - fraction_bits)
    if kept >= (1 << (fraction_bits + 1)):
        kept = kept >> 1
        target_exp += 1
    if target_exp >= maximum_exponent:
        return target_sign | (maximum_exponent << fraction_bits)
    return (
        target_sign
        | (target_exp << fraction_bits)
        | (kept & ((1 << fraction_bits) - 1))
    )


def _narrow_bits_to_float64(
    bits: int,
    fraction_bits: int,
    exponent_bias: int,
    maximum_exponent: int,
    sign_shift: int,
) -> float:
    sign = (bits >> sign_shift) & 1
    exponent = (bits >> fraction_bits) & maximum_exponent
    fraction = bits & ((1 << fraction_bits) - 1)
    inf = 1e309
    if exponent == maximum_exponent:
        if fraction == 0:
            return -inf if sign else inf
        nan = inf - inf
        return -nan if sign else nan
    if exponent == 0:
        if fraction == 0:
            return -0.0 if sign else 0.0
        significand = fraction
        scale = 1 - exponent_bias - fraction_bits
    else:
        significand = (1 << fraction_bits) | fraction
        scale = exponent - exponent_bias - fraction_bits
    value = float(significand)
    while scale > 0:
        value = value * 2.0
        scale -= 1
    while scale < 0:
        value = value * 0.5
        scale += 1
    return -value if sign else value


def _float32_to_bits(f: float) -> int:
    return _narrow_float_to_bits(f, 23, 127, 255, 31)


def _float16_to_bits(f: float) -> int:
    return _narrow_float_to_bits(f, 10, 15, 31, 15)


def _bits_to_float32(bits: int) -> float:
    return _narrow_bits_to_float64(bits, 23, 127, 255, 31)


def _bits_to_float16(bits: int) -> float:
    return _narrow_bits_to_float64(bits, 10, 15, 31, 15)
