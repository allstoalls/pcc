"""pcc.stdlib.ctypes — narrow ``ctypes`` skeleton.

pcc uses CPython's real ``ctypes`` at runtime for two purposes:

  1. ``CDLL`` + ``CFUNCTYPE`` around the MCJIT-produced shared
     library (``api.py``, ``evaluater/c_evaluator.py``).
  2. ``c_int``, ``c_char_p``, ``POINTER``, etc. as the argtype /
     restype annotations on the loaded functions.

P6C.1's ``pcc.extern`` system supersedes all of this for self-host:
after the extern binding lands, the evaluator talks to LLVM directly
via ``pcc.ir`` and loads symbols with ``dlopen``/``dlsym``.
The legacy ``ctypes`` callsites stay in the audit surface only as a
removal target, not a reimplementation target.

This stub exports the minimal names used by the callers as plain
placeholders. Any actual call raises NotImplementedError so the
self-host build fails loudly if the ``ctypes`` path ever runs.
"""
from __future__ import annotations


# ---- primitive type tokens --------------------------------------------------
#
# Callers use these only as *annotation values* for ``argtypes`` /
# ``restype`` on function pointers. They don't instantiate them, so a
# simple singleton sentinel per type is enough.

class _CTypeSentinel:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"<ctypes.{self._name}>"

    def __call__(self, *args, **kwargs):
        raise NotImplementedError(
            f"ctypes.{self._name}() value construction is out of scope; "
            "use pcc.extern for self-host builds"
        )


c_bool = _CTypeSentinel("c_bool")
c_char = _CTypeSentinel("c_char")
c_wchar = _CTypeSentinel("c_wchar")
c_byte = _CTypeSentinel("c_byte")
c_ubyte = _CTypeSentinel("c_ubyte")
c_short = _CTypeSentinel("c_short")
c_ushort = _CTypeSentinel("c_ushort")
c_int = _CTypeSentinel("c_int")
c_uint = _CTypeSentinel("c_uint")
c_long = _CTypeSentinel("c_long")
c_ulong = _CTypeSentinel("c_ulong")
c_longlong = _CTypeSentinel("c_longlong")
c_ulonglong = _CTypeSentinel("c_ulonglong")
c_int8 = _CTypeSentinel("c_int8")
c_int16 = _CTypeSentinel("c_int16")
c_int32 = _CTypeSentinel("c_int32")
c_int64 = _CTypeSentinel("c_int64")
c_uint8 = _CTypeSentinel("c_uint8")
c_uint16 = _CTypeSentinel("c_uint16")
c_uint32 = _CTypeSentinel("c_uint32")
c_uint64 = _CTypeSentinel("c_uint64")
c_size_t = _CTypeSentinel("c_size_t")
c_ssize_t = _CTypeSentinel("c_ssize_t")
c_float = _CTypeSentinel("c_float")
c_double = _CTypeSentinel("c_double")
c_longdouble = _CTypeSentinel("c_longdouble")
c_char_p = _CTypeSentinel("c_char_p")
c_wchar_p = _CTypeSentinel("c_wchar_p")
c_void_p = _CTypeSentinel("c_void_p")


def POINTER(ctype):
    """Return a pointer-type token for ``ctype``. Pure marker; no
    runtime pointer is materialised by this stub."""
    return _CTypeSentinel(f"POINTER({getattr(ctype, '_name', 'X')})")


def pointer(obj):
    raise NotImplementedError(
        "ctypes.pointer needs real value boxing; use pcc.extern instead"
    )


def byref(obj, offset: int = 0):
    raise NotImplementedError(
        "ctypes.byref needs real value boxing; use pcc.extern instead"
    )


def cast(obj, ctype):
    raise NotImplementedError(
        "ctypes.cast needs real value boxing; use pcc.extern instead"
    )


def addressof(obj) -> int:
    raise NotImplementedError(
        "ctypes.addressof needs real value boxing; use pcc.extern instead"
    )


def sizeof(ctype) -> int:
    raise NotImplementedError(
        "ctypes.sizeof needs layout metadata; use pcc.extern sizes instead"
    )


def string_at(address, size: int = -1) -> bytes:
    raise NotImplementedError(
        "ctypes.string_at needs extern memcpy + dlsym"
    )


def memmove(dst, src, count: int):
    raise NotImplementedError(
        "ctypes.memmove needs an extern memmove binding"
    )


# ---- FFI loaders ------------------------------------------------------------


class CDLL:
    """Stub for ``ctypes.CDLL``. The self-host evaluator uses
    ``pcc.extern`` + ``pcc.ir`` instead; this class exists only
    so that the audit's import graph resolves."""

    def __init__(self, name, mode: int = 0, handle=None,
                 use_errno: bool = False, use_last_error: bool = False,
                 winmode: int | None = None) -> None:
        raise NotImplementedError(
            "ctypes.CDLL is superseded by pcc.extern + pcc.ir "
            "for self-host builds"
        )


class PyDLL(CDLL):
    pass


class WinDLL(CDLL):
    pass


class OleDLL(CDLL):
    pass


def CFUNCTYPE(restype, *argtypes, use_errno: bool = False,
              use_last_error: bool = False):
    raise NotImplementedError(
        "ctypes.CFUNCTYPE is superseded by pcc.extern for self-host builds"
    )


def PYFUNCTYPE(restype, *argtypes):
    raise NotImplementedError(
        "ctypes.PYFUNCTYPE is superseded by pcc.extern for self-host builds"
    )


class Structure:
    _fields_: list = []


class Union:
    _fields_: list = []


class Array:
    _type_ = None
    _length_ = 0


# ---- error surface ----------------------------------------------------------


class ArgumentError(Exception):
    pass


def get_errno() -> int:
    return 0


def set_errno(value: int) -> int:
    return 0


def get_last_error() -> int:
    return 0


def set_last_error(value: int) -> int:
    return 0
