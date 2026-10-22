from __future__ import annotations

"""pcc-native IEEE-754 helpers for the self backend.

The self emitter cannot depend on CPython's ``struct`` module: pcc1 must be
able to execute it without libpython.  The canonical arithmetic conversions
live in ``pcc.ir.support._float_bits`` so the stdlib port, owned IR builder, and
self backend cannot drift independently.
"""

from pcc.ir.support._float_bits import _bits_to_float32, _bits_to_float64, _float32_to_bits, _float64_to_bits


def float64_to_bits(value: float):
    # uint64, so unannotated: see ``_float64_to_bits``.
    return _float64_to_bits(value)


def bits_to_float64(bits) -> float:
    return _bits_to_float64(bits)


def u64_as_i64(bits) -> int:
    """The signed i64 with the same 64 bits as the uint64 ``bits``.

    Register materialization (``emit_const_to_reg*``) takes an i64-lane
    ``value``; a double's pattern at or above 2**63 must be passed in its
    two's-complement spelling, which the movz/movk chunking already handles.
    """
    if bits >= (1 << 63):
        return bits - (1 << 64)
    return bits


def float32_to_bits(value: float) -> int:
    return _float32_to_bits(value)


def bits_to_float32(bits: int) -> float:
    return _bits_to_float32(bits)
