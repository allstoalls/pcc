"""Owned POSIX file locking for native Python's standard fcntl module."""
from __future__ import annotations

import sys
import operator
from pcc.extern import c_int64, c_obj, extern

if sys.platform == "win32":
    raise ImportError("No module named 'fcntl'")

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8
F_GETFL = 3
_native_flock = extern("py_fcntl_flock", (c_obj, c_obj, c_obj), c_obj)
_native_getfl = extern("py_fcntl_getfl", (c_int64, c_obj), c_int64)


def flock(fd, operation):
    """Lock the open file description; close/process exit releases its lock."""
    descriptor = fd
    if not isinstance(descriptor, int):
        try:
            method = fd.fileno
        except AttributeError:
            raise TypeError("argument must be an int, or have a fileno() method")
        descriptor = method()
        if not isinstance(descriptor, int):
            raise TypeError("fileno() returned a non-integer")
    # Keep the original object alive through the raw operation. A temporary
    # open(path) passed directly to flock must not close after fileno returns.
    error = _native_flock(descriptor, operation, fd)
    if error is not None:
        raise error
    return None


def fcntl(fd, cmd: int, arg=0):
    """Query descriptor flags; other commands and buffer forms are unsupported."""
    descriptor = fd
    if not isinstance(descriptor, int):
        try:
            method = fd.fileno
        except AttributeError:
            raise TypeError("argument must be an int, or have a fileno() method")
        descriptor = method()
        if not isinstance(descriptor, int):
            raise TypeError("fileno() returned a non-integer")
    if descriptor < 0:
        raise ValueError("file descriptor cannot be negative")
    if descriptor > 2147483647:
        raise OverflowError("Python int too large to convert to C int")
    command = operator.index(cmd)
    if command != F_GETFL:
        raise NotImplementedError("native fcntl.fcntl supports only F_GETFL")
    if isinstance(arg, (bytes, bytearray, memoryview)):
        raise NotImplementedError("native fcntl.fcntl buffer arguments are unsupported")
    argument = operator.index(arg)
    if argument < -2147483648 or argument > 2147483647:
        raise OverflowError("Python int too large to convert to C int")
    # Passing the original owner keeps a temporary file open through the
    # descriptor query, including when fd was provided via fileno().
    result = _native_getfl(descriptor, fd)
    if result < 0:
        raise OSError(-result, "fcntl F_GETFL failed")
    return result


def ioctl(fd, request: int, arg=0, mutate_flag: bool = True):
    raise NotImplementedError("fcntl.ioctl awaits a TIOC* extern binding")
