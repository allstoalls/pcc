"""pcc.stdlib.sys — skeleton replacement for ``sys``.

Surface covers what pcc's own source uses: ``argv``, ``exit``,
``stderr.write``, ``platform``, ``byteorder``, ``version_info.major``. Everything
else is out of scope for P6C.4.
"""

from __future__ import annotations

import sys as _native_sys

from pcc.extern import c_int, c_int64, c_obj, extern
from pcc.driver.python_target import PYTHON_TARGET_FULL_VERSION, PYTHON_TARGET_MAJOR, PYTHON_TARGET_MINOR, PYTHON_TARGET_MICRO

# libc glue.
exit_c: "extern" = extern("exit", (c_int,))  # noreturn; treat as void
_recursion_limit_c: "extern" = extern("py_sys_getrecursionlimit", (), c_int64)
_recursion_set_c: "extern" = extern("py_sys_setrecursionlimit", (c_int64,), c_int64)
_index_value = extern("py_obj_index", (c_obj,), c_obj)
_error_pending = extern("py_err_occurred", (), c_int64)

# Use the compiler's kernel-backed argv projection to construct real pcc
# strings. Publishing this module must not expose raw extern ``char *`` values
# as Python objects.
argv = _native_sys.argv


def exit(code: int = 0) -> None:
    """``sys.exit(code)`` — calls ``libc exit(code)``, does not return."""
    exit_c(code)


class _VersionInfo:
    def __init__(self, major: int, minor: int, micro: int) -> None:
        self.major = major
        self.minor = minor
        self.micro = micro

    def __iter__(self):
        return iter((self.major, self.minor, self.micro))


class _Implementation:
    def __init__(self, name: str) -> None:
        self.name = name


class _Stream:
    def __init__(self, name: str) -> None:
        self.name = name

    def write(self, text: str) -> int:
        if self.name == "stdout":
            return _native_sys.stdout.write(text)
        return _native_sys.stderr.write(text)

    def flush(self) -> None:
        if self.name == "stdout":
            _native_sys.stdout.flush()
        else:
            _native_sys.stderr.flush()


# Runtime version introspection agrees with the compiler's version guards.
# A package-selection override does not change the executing language target.
version_info: _VersionInfo = _VersionInfo(
    PYTHON_TARGET_MAJOR, PYTHON_TARGET_MINOR, PYTHON_TARGET_MICRO
)
version: str = PYTHON_TARGET_FULL_VERSION + " (pcc self-host)"
implementation: _Implementation = _Implementation("pcc")
stdout: _Stream = _Stream("stdout")
stderr: _Stream = _Stream("stderr")


# pcc only ships darwin/linux at the moment. The real platform is
# stamped in by the build via an environment variable or a generated
# constant file; this default is the development fallback.
platform: str = "darwin"
byteorder: str = "little"


def getdefaultencoding() -> str:
    return "utf-8"


def intern(value: str) -> str:
    return value


def getrecursionlimit() -> int:
    """Return the active interpreter's enforced recursion limit."""
    if _native_sys.implementation.name != "pcc":
        return _native_sys.getrecursionlimit()
    return _recursion_limit_c()


def setrecursionlimit(limit, /) -> None:
    """Update the native activation limit without a host interpreter fallback."""
    if _native_sys.implementation.name != "pcc":
        return _native_sys.setrecursionlimit(limit)
    value = _index_value(limit)
    if _error_pending():
        raise
    if value > 2147483647 or value < -2147483648:
        raise OverflowError("Python int too large to convert to C int")
    _recursion_set_c(value)
    if _error_pending():
        raise
