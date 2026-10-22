"""Readable oracle for the production ``py_complex_pow`` implementation.

The production pcc-Python archive owns the ABI in ``py_obj_stubs.py`` alongside
the other complex arithmetic helpers.  ``src/py_complex_pow.c`` remains the
host-C/pcc-C oracle implementation; this ordinary-Python transcription remains
a directly readable algorithm oracle for both implementations.

The pure-Python transcription below exists purely as a readable reference of
the algorithm (which mirrors CPython ``Objects/complexobject.c::_Py_c_pow`` +
``c_powi`` / ``c_powu`` / ``_Py_c_quot``) so a future maintainer can audit the C
against a runnable oracle. Keep it in sync with the C file.
"""
from __future__ import annotations

__pcc_runtime_port__ = True

import math


def _c_prod(a, b):
    return (a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0])


def _c_quot(a, b):
    abs_breal = abs(b[0])
    abs_bimag = abs(b[1])
    if abs_breal >= abs_bimag:
        if abs_breal == 0.0:
            return (0.0, 0.0)
        ratio = b[1] / b[0]
        denom = b[0] + b[1] * ratio
        return ((a[0] + a[1] * ratio) / denom, (a[1] - a[0] * ratio) / denom)
    ratio = b[0] / b[1]
    denom = b[0] * ratio + b[1]
    return ((a[0] * ratio + a[1]) / denom, (a[1] * ratio - a[0]) / denom)


def _c_powu(x, n):
    r = (1.0, 0.0)
    p = x
    mask = 1
    while mask > 0 and n >= mask:
        if n & mask:
            r = _c_prod(r, p)
        mask <<= 1
        p = _c_prod(p, p)
    return r


def _c_powi(x, n):
    if n > 0:
        return _c_powu(x, n)
    return _c_quot((1.0, 0.0), _c_powu(x, -n))


def complex_pow(base, ex):
    """``base ** ex`` for ``(real, imag)`` pairs; reference for py_complex_pow.c.

    Raises ``ZeroDivisionError`` for ``0 ** (negative or complex power)``.
    """
    if ex[0] == 0.0 and ex[1] == 0.0:
        return (1.0, 0.0)
    if base[0] == 0.0 and base[1] == 0.0:
        if ex[1] != 0.0 or ex[0] < 0.0:
            raise ZeroDivisionError("zero to a negative or complex power")
        return (0.0, 0.0)
    if ex[1] == 0.0 and ex[0] == math.floor(ex[0]) and abs(ex[0]) <= 100.0:
        return _c_powi(base, int(ex[0]))
    vabs = math.hypot(base[0], base[1])
    length = math.pow(vabs, ex[0])
    at = math.atan2(base[1], base[0])
    phase = at * ex[0]
    if ex[1] != 0.0:
        length /= math.exp(at * ex[1])
        phase += ex[1] * math.log(vabs)
    return (length * math.cos(phase), length * math.sin(phase))
