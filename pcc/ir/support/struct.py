"""pcc-Python port of the stdlib ``struct`` module.

Minimal — covers exactly the format codes used by
``pcc/ir/ir.py`` so that ``ir.py`` can be compiled natively
in the bootstrap closure without pulling libpython for ``struct``
(Issue 11.C.2 → unblocks Phase 9.2).

Supported format codes:

  ===  ==========================  =====
  ``>d``  big-endian binary64 (double)   8 bytes
  ``>f``  big-endian binary32 (single)   4 bytes
  ``>e``  big-endian binary16 (half)     2 bytes
  ``>Q``  big-endian uint64              8 bytes
  ===  ==========================  =====

Anything else raises :class:`StructPortError` so callers see a clear
unsupported-format message rather than silently producing wrong
bytes. Add format codes here if a future module needs them.
"""
from __future__ import annotations


class StructPortError(ValueError):
    """Raised when this port encounters a format code it doesn't
    cover. Distinct from CPython's ``struct.error`` so callers can
    tell "pcc port limitation" apart from "real input error"."""


# IEEE 754 helpers live in ``pcc.ir.support._float_bits`` — shared with
# ``pcc/ir/ir.py`` which uses the int-form interface directly
# (skipping the bytes intermediate). Re-exported here for any caller
# that needs them alongside the bytes-API ``pack``/``unpack``.
from pcc.ir.support._float_bits import _bits_to_float16, _bits_to_float32, _bits_to_float64, _float16_to_bits, _float32_to_bits, _float64_to_bits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pack(fmt: str, value) -> bytes:
    """Pack ``value`` according to ``fmt`` (subset of CPython struct).

    Supported: ``>d`` (binary64), ``>f`` (binary32), ``>e`` (binary16),
    ``>Q`` (uint64-be).
    """
    if fmt == ">d":
        bits = _float64_to_bits(float(value))
        return bits.to_bytes(8, "big")
    if fmt == ">f":
        f = float(value)
        bits = _float32_to_bits(f)
        inf = 1e309
        if (
            f == f
            and f != inf
            and f != -inf
            and (bits & 0x7FFFFFFF) == 0x7F800000
        ):
            raise OverflowError("float too large to pack with f format")
        return bits.to_bytes(4, "big")
    if fmt == ">e":
        f = float(value)
        bits = _float16_to_bits(f)
        inf = 1e309
        if (
            f == f
            and f != inf
            and f != -inf
            and (bits & 0x7FFF) == 0x7C00
        ):
            raise OverflowError("float too large to pack with e format")
        return bits.to_bytes(2, "big")
    if fmt == ">Q":
        return int(value).to_bytes(8, "big")
    raise StructPortError(
        f"pcc.ir.support.struct doesn't support format {fmt!r} "
        f"(only '>d', '>f', '>e', '>Q')"
    )


def unpack(fmt: str, buf: bytes) -> tuple:
    """Unpack ``buf`` according to ``fmt`` and return a tuple."""
    if fmt == ">d":
        bits = int.from_bytes(buf, "big")
        return (_bits_to_float64(bits),)
    if fmt == ">f":
        return (_bits_to_float32(int.from_bytes(buf, "big")),)
    if fmt == ">e":
        return (_bits_to_float16(int.from_bytes(buf, "big")),)
    if fmt == ">Q":
        return (int.from_bytes(buf, "big"),)
    raise StructPortError(
        f"pcc.ir.support.struct doesn't support format {fmt!r} "
        f"(only '>d', '>f', '>e', '>Q')"
    )
