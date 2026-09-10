from __future__ import annotations

"""pcc-native IEEE-754 helpers for the self backend.

The self emitter cannot depend on CPython's ``struct`` module: pcc1 must be
able to execute it without libpython.  The canonical arithmetic conversions
live in ``pcc.stdlib._float_bits`` so the stdlib port, LLVM-C IR builder, and
self backend cannot drift independently.
"""

from pcc.stdlib._float_bits import (
    _bits_to_float32,
    _bits_to_float64,
    _float32_to_bits,
    _float64_to_bits,
)


def float64_to_bits(value: float) -> int:
    return _float64_to_bits(value)


def bits_to_float64(bits: int) -> float:
    return _bits_to_float64(bits)


def float32_to_bits(value: float) -> int:
    return _float32_to_bits(value)


def bits_to_float32(bits: int) -> float:
    return _bits_to_float32(bits)
