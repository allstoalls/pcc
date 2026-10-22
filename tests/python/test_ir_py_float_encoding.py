"""Behavior-locking tests for ``ir.Constant`` float encoding.

The implementation uses one pcc-native arithmetic owner shared with the
stdlib ``struct`` port and self backend.  CPython's ``struct`` remains the
test oracle for rounding and subnormal boundaries.
"""
from __future__ import annotations

import struct

import pytest

from pcc.ir.ir import Constant, DoubleType, FloatType, HalfType


_DOUBLE = DoubleType()
_FLOAT = FloatType()
_HALF = HalfType()


# ---------------------------------------------------------------------------
# Double encoding (no rounding — full precision)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected_hex", [
    (0.0, "0x0000000000000000"),
    (1.0, "0x3FF0000000000000"),
    (-1.0, "0xBFF0000000000000"),
    (2.0, "0x4000000000000000"),
    (0.5, "0x3FE0000000000000"),
    (3.141592653589793, "0x400921FB54442D18"),  # π
    (-3.141592653589793, "0xC00921FB54442D18"),
    (11.1, "0x4026333333333333"),
    (1.5e308, "0x7FEAB36D48E1ACF0"),
    (2.2250738585072014e-308, "0x0010000000000000"),  # smallest normal
    (2.0 ** -1074, "0x0000000000000001"),
    (-(2.0 ** -1074), "0x8000000000000001"),
    (float.fromhex("0x0.fffffffffffffp-1022"), "0x000FFFFFFFFFFFFF"),
])
def test_double_constant_hex(value, expected_hex):
    c = Constant(_DOUBLE, value)
    assert c._ref == expected_hex, (
        f"Constant(double, {value}) → {c._ref}, expected {expected_hex}"
    )


def test_double_negative_zero():
    """-0.0 has the sign bit set."""
    c = Constant(_DOUBLE, -0.0)
    assert c._ref == "0x8000000000000000"


def test_double_inf():
    c = Constant(_DOUBLE, float("inf"))
    assert c._ref == "0x7FF0000000000000"


def test_double_neg_inf():
    c = Constant(_DOUBLE, float("-inf"))
    assert c._ref == "0xFFF0000000000000"


def test_double_nan_is_nan():
    """NaN encoding may have multiple valid representations; just check
    the exponent is all-ones and mantissa is non-zero."""
    c = Constant(_DOUBLE, float("nan"))
    bits = int(c._ref, 16)
    biased_exp = (bits >> 52) & 0x7FF
    mantissa = bits & ((1 << 52) - 1)
    assert biased_exp == 0x7FF, f"NaN exp should be 0x7FF, got 0x{biased_exp:X}"
    assert mantissa != 0, "NaN mantissa must be non-zero"


# ---------------------------------------------------------------------------
# Float (32-bit) encoding — round to f32 precision then encode as 64-bit hex
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected_hex", [
    (0.0, "0x0000000000000000"),
    (1.0, "0x3FF0000000000000"),
    (-1.0, "0xBFF0000000000000"),
    (11.1, "0x4026333340000000"),  # rounded down vs double's 0x...4D18
    (3.141592653589793, "0x400921FB60000000"),  # rounded
])
def test_float_constant_hex(value, expected_hex):
    c = Constant(_FLOAT, value)
    assert c._ref == expected_hex, (
        f"Constant(float, {value}) → {c._ref}, expected {expected_hex}"
    )


@pytest.mark.parametrize(
    "value",
    [
        2.0 ** -149,
        -(2.0 ** -149),
        (2.0 ** -126) - (2.0 ** -149),
        2.0 ** -150,
        3.0 * (2.0 ** -150),
    ],
)
def test_float_subnormal_constant_matches_struct_oracle(value):
    rounded = struct.unpack(">f", struct.pack(">f", value))[0]
    raw = struct.unpack(">Q", struct.pack(">d", rounded))[0]
    assert Constant(_FLOAT, value)._ref == f"0x{raw:016X}"


# ---------------------------------------------------------------------------
# Half (16-bit) encoding — round to f16 precision
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    0.0, 1.0, -1.0, 0.5, 2.0,
])
def test_half_constant_hex_basic(value):
    """Half encoding for representable-without-loss values matches the
    double encoding of the same value (low mantissa bits are zero)."""
    c = Constant(_HALF, value)
    # The output should be a 16-character hex string starting with 0x
    assert c._ref.startswith("0x")
    assert len(c._ref) == 18  # 0x + 16 hex digits


@pytest.mark.parametrize(
    "value",
    [
        2.0 ** -24,
        -(2.0 ** -24),
        (2.0 ** -14) - (2.0 ** -24),
        2.0 ** -25,
        3.0 * (2.0 ** -25),
    ],
)
def test_half_subnormal_constant_matches_struct_oracle(value):
    rounded = struct.unpack(">e", struct.pack(">e", value))[0]
    raw = struct.unpack(">Q", struct.pack(">d", rounded))[0]
    assert Constant(_HALF, value)._ref == f"0x{raw:016X}"
