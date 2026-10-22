"""Owned Windows file-region locking; other CRT surfaces fail explicitly."""
from __future__ import annotations

import sys
from pcc.extern import c_obj, extern


if sys.platform != "win32":
    raise ImportError("No module named 'msvcrt'")


LK_UNLCK = 0
LK_LOCK = 1
LK_NBLCK = 2
LK_RLCK = 3
LK_NBRLCK = 4


def _unowned():
    raise NotImplementedError(
        "msvcrt descriptor and console operations are not runtime-owned"
    )


_native_locking = extern("py_msvcrt_locking", (c_obj, c_obj, c_obj), c_obj)


def locking(fd, mode, nbytes):
    error = _native_locking(fd, mode, nbytes)
    if error is not None:
        raise error
    return None


def setmode(fd, flags):
    _unowned()


def open_osfhandle(handle, flags):
    _unowned()


def get_osfhandle(fd):
    _unowned()


def kbhit():
    _unowned()


def getch():
    _unowned()


def getwch():
    _unowned()


def putch(char):
    _unowned()


def putwch(char):
    _unowned()


__all__ = [
    "LK_UNLCK",
    "LK_LOCK",
    "LK_NBLCK",
    "LK_RLCK",
    "LK_NBRLCK",
    "locking",
    "setmode",
    "open_osfhandle",
    "get_osfhandle",
    "kbhit",
    "getch",
    "getwch",
    "putch",
    "putwch",
]
